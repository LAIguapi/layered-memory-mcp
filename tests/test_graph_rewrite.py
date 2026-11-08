"""Explicit group rewrites use only neutral, disposable graph fixtures."""

import asyncio
import copy
import hashlib
import importlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from test_v361_graph_application import config, fake_embeddings, _commit_a, _node, T0

from layered_memory_mcp import graph_application, graph_projection, server
from layered_memory_mcp.graph_journal import MemoryEventJournal
from layered_memory_mcp.graph_models import GraphScope, NodeStatus
from layered_memory_mcp.graph_runtime_assembly import journal_readonly_session
from layered_memory_mcp.graph_write import SemanticWriteRequest


def rewrite_api():
    try:
        return importlib.import_module("layered_memory_mcp.graph_rewrite")
    except ImportError:
        pytest.fail("explicit semantic rewrite behavior is unavailable")


def request_for(api, request_key="rewrite_schedule", **kwargs):
    return api.SemanticRewriteRequest(
        targets=[api.RewriteTarget(
            old_node_ids=["claim_index_nightly"],
            node=_node("claim_hourly", "Neutral hourly schedule."),
            surface=api.SectionRef(domain="retrieval", section="New schedule"),
        )],
        occurred_at=T0 + timedelta(seconds=10),
        request_key=request_key,
        **kwargs,
    )


def test_inspection_discovers_exact_owner_without_body(config):
    _commit_a(config)
    result = rewrite_api().inspect_semantic_surfaces(config, "retrieval")
    assert result["success"] is True
    assert result["surfaces"][0]["node_id"] == "claim_index_nightly"
    assert "body" not in result["surfaces"][0]


def test_group_rewrite_retires_old_and_publishes_new(config):
    _commit_a(config)
    api = rewrite_api()
    result = api.commit_semantic_rewrite(config, request_for(api))
    assert result.committed is True
    assert result.projections_pending == []


def test_snapshot_head_precondition_rejects_stale_decision(config):
    _commit_a(config)
    api = rewrite_api()
    result = api.commit_semantic_rewrite(config, request_for(
        api, preconditions=api.RewritePreconditions(expected_head_position=0),
    ))
    assert result.status == "graph_rewrite_rejected_preconditions"
    assert result.committed is False


def journal_state(config):
    with journal_readonly_session(config, require=True) as assembly:
        return assembly.journal.head_position(), assembly.journal.read_events()


def tree_state(root):
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in [root, *sorted(root.rglob("*"))]
    }


def payload_state(root):
    return {name: value[1] for name, value in tree_state(root).items()
            if value[1] is not None and not name.endswith(".publish.lock")}


def seed_second(config, *, domain="retrieval", section="Cache", occurrence=0):
    return graph_application.commit_semantic_write(
        config,
        SemanticWriteRequest(node=_node("claim_cache", "Neutral cache schedule."),
                             occurred_at=T0 + timedelta(seconds=1)),
        graph_projection.ProjectionBinding(domain=domain, section=section,
                                           occurrence=occurrence, body="Neutral cache schedule."),
    )


def group_request(api):
    request = request_for(api)
    request.targets.append(api.RewriteTarget(
        old_node_ids=["claim_cache"], node=_node("claim_cache_new", "Neutral replacement cache."),
        surface=api.SectionRef(domain="retrieval", section="New cache"),
    ))
    return request


def full_preconditions(api, config):
    view = api.inspect_semantic_surfaces(config, "retrieval")
    return api.RewritePreconditions(
        expected_head_position=view["head_position"],
        expected_node_hashes={row["node_id"]: row["node_sha256"] for row in view["surfaces"]},
        expected_body_hashes={row["entry_id"]: row["binding_body_sha256"] for row in view["surfaces"]},
        expected_entry_ids=[row["entry_id"] for row in view["surfaces"]],
    )


def test_n_to_one_keeps_history_and_removes_every_old_section(config):
    _commit_a(config)
    assert seed_second(config).ok
    api = rewrite_api()
    request = request_for(api, preconditions=full_preconditions(api, config))
    request.targets[0].old_node_ids.append("claim_cache")
    commit = api.commit_semantic_rewrite(config, request)
    assert commit.ok
    assert set(commit.superseded_ids) == {"claim_cache", "claim_index_nightly"}
    assert (Path(config.knowledge_dir) / "retrieval.md").read_text().strip() == (
        "## New schedule\n\nNeutral hourly schedule."
    )
    from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore

    replay = InMemoryGraphStore()
    replay.rebuild(journal_state(config)[1])
    nodes, _edges = graph_application._materialize(replay)
    assert nodes["claim_index_nightly"].status == NodeStatus.SUPERSEDED
    assert nodes["claim_cache"].status == NodeStatus.SUPERSEDED


def test_group_uses_one_append_and_one_replace_per_domain(config, monkeypatch):
    _commit_a(config)
    seed_second(config)
    api = rewrite_api()
    appends = []
    replacements = []
    append = MemoryEventJournal.append
    replace = graph_projection.replace_document

    def counted_append(self, *args, **kwargs):
        appends.append(args)
        return append(self, *args, **kwargs)

    def counted_replace(path, content):
        replacements.append(path)
        return replace(path, content)

    monkeypatch.setattr(MemoryEventJournal, "append", counted_append)
    monkeypatch.setattr(graph_projection, "replace_document", counted_replace)
    commit = api.commit_semantic_rewrite(config, group_request(api))
    assert commit.ok
    assert len(appends) == 1
    assert replacements == [Path(config.knowledge_dir) / "retrieval.md"]
    view = api.inspect_semantic_surfaces(config, "retrieval")
    assert {row["node_id"] for row in view["surfaces"]} == {"claim_hourly", "claim_cache_new"}


@pytest.mark.parametrize("fault", ["unknown", "duplicate_old", "duplicate_new", "same_surface",
                                    "scope", "request_scope", "history", "traversal", "existing_new"])
def test_any_invalid_target_has_zero_events_or_projection_changes(config, fault):
    _commit_a(config)
    seed_second(config)
    api = rewrite_api()
    request = group_request(api)
    target = request.targets[1]
    if fault == "unknown":
        target.old_node_ids = ["missing"]
    elif fault == "duplicate_old":
        target.old_node_ids = request.targets[0].old_node_ids
    elif fault == "duplicate_new":
        target.node = request.targets[0].node
    elif fault == "same_surface":
        target.surface = request.targets[0].surface
    elif fault == "scope":
        target.node.scope = GraphScope(namespace="foreign")
    elif fault == "request_scope":
        request.scope = GraphScope(namespace="foreign")
    elif fault == "history":
        target.node.status = NodeStatus.SUPERSEDED
    elif fault == "traversal":
        target.surface.domain = "../foreign"
    elif fault == "existing_new":
        target.node.id = "claim_cache"
    before = payload_state(Path(config.home))
    events = journal_state(config)
    result = api.commit_semantic_rewrite(config, request)
    assert not result.committed
    assert journal_state(config) == events
    assert payload_state(Path(config.home)) == before


@pytest.mark.parametrize("fault", ["node", "body", "head", "entry", "unrelated_node"])
def test_bad_snapshot_is_distinguishable_and_has_zero_events(config, fault):
    _commit_a(config)
    api = rewrite_api()
    conditions = full_preconditions(api, config)
    if fault == "node":
        conditions.expected_node_hashes["claim_index_nightly"] = "0" * 64
    elif fault == "body":
        conditions.expected_body_hashes[conditions.expected_entry_ids[0]] = "0" * 64
    elif fault == "head":
        conditions.expected_head_position = 0
    elif fault == "entry":
        conditions.expected_entry_ids = []
    elif fault == "duplicate_entry":
        conditions.expected_entry_ids *= 2
    elif fault == "unrelated_node":
        conditions.expected_node_hashes["unrelated"] = "0" * 64
    before = journal_state(config)
    result = api.commit_semantic_rewrite(config, request_for(api, preconditions=conditions))
    expected = "graph_rewrite_rejected_entry_set" if "entry" in fault else "graph_rewrite_rejected_preconditions"
    assert result.status == expected
    assert journal_state(config) == before


def test_duplicate_entry_precondition_members_use_set_semantics(config):
    _commit_a(config)
    api = rewrite_api()
    conditions = full_preconditions(api, config)
    declared = list(conditions.expected_entry_ids)
    conditions.expected_entry_ids = declared[::-1] + declared[:1]
    result = api.commit_semantic_rewrite(config, request_for(api, preconditions=conditions))
    assert result.committed is True


def test_legacy_mode_refuses_before_touching_disk(config):
    _commit_a(config)
    api = rewrite_api()
    legacy = copy.copy(config)
    legacy.graph_mode = "legacy"
    before = tree_state(Path(config.home))
    result = api.commit_semantic_rewrite(legacy, request_for(api, request_key="legacy_refusal"))
    assert result.committed is False
    assert result.errors
    assert tree_state(Path(config.home)) == before


def test_empty_old_set_is_rejected_without_events_or_projection(config):
    _commit_a(config)
    api = rewrite_api()
    request = request_for(api)
    request.targets[0].old_node_ids = []
    before_journal = journal_state(config)
    before_payload = payload_state(Path(config.home))
    result = api.commit_semantic_rewrite(config, request)
    assert result.committed is False
    assert result.status == "graph_write_rejected_invalid_replay"
    assert journal_state(config) == before_journal
    assert payload_state(Path(config.home)) == before_payload


def test_cas_check_and_append_share_the_publication_lock(config, monkeypatch):
    _commit_a(config)
    api = rewrite_api()
    request = request_for(api, preconditions=full_preconditions(api, config))
    locked = []
    observed = []
    original_lock = graph_application._publication_lock
    original_hash = api.canonical_node_sha256
    original_append = MemoryEventJournal.append

    @contextmanager
    def tracked_lock(config):
        with original_lock(config):
            locked.append(True)
            try:
                yield
            finally:
                locked.pop()

    def tracked_hash(node):
        assert locked
        observed.append("cas")
        return original_hash(node)

    def tracked_append(self, *args, **kwargs):
        assert locked
        observed.append("append")
        return original_append(self, *args, **kwargs)

    monkeypatch.setattr(graph_application, "_publication_lock", tracked_lock)
    monkeypatch.setattr(api, "canonical_node_sha256", tracked_hash)
    monkeypatch.setattr(MemoryEventJournal, "append", tracked_append)
    assert api.commit_semantic_rewrite(config, request).ok
    assert observed == ["cas", "append"]


@pytest.mark.parametrize("fault", ["edited", "deleted_section", "deleted_file"])
def test_external_surface_changes_are_not_overwritten(config, fault):
    _commit_a(config)
    api = rewrite_api()
    path = Path(config.knowledge_dir) / "retrieval.md"
    if fault == "edited":
        path.write_text("## Index maintenance\n\nExternal neutral edit.\n")
    elif fault == "deleted_section":
        path.write_text("## Unrelated section\n\nNeutral text.\n")
    else:
        path.unlink()
    before = payload_state(Path(config.home))
    events = journal_state(config)
    result = api.commit_semantic_rewrite(config, request_for(api))
    assert result.status == graph_application.GRAPH_COMMIT_REJECTED_DRIFT
    assert journal_state(config) == events
    assert payload_state(Path(config.home)) == before


def test_two_writers_for_the_same_old_node_have_one_winner(config):
    _commit_a(config)
    api = rewrite_api()
    first = request_for(api)
    second = first.model_copy(deep=True)
    second.request_key = "another_writer"
    second.targets[0].node.id = "claim_other_writer"
    barrier = threading.Barrier(2)

    def commit(request):
        barrier.wait(timeout=10)
        return api.commit_semantic_rewrite(config, request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(commit, [first, second]))
    assert sum(result.committed for result in results) == 1
    assert sum(not result.committed for result in results) == 1


@pytest.mark.parametrize("outage", ["before_replace", "after_replace", "vector"])
def test_pending_retry_converges_before_rechecking_its_own_cas(config, monkeypatch, outage):
    _commit_a(config)
    api = rewrite_api()
    request = request_for(api, preconditions=full_preconditions(api, config))
    original_replace = graph_projection.replace_document

    def unavailable(*args, **kwargs):
        if outage == "after_replace":
            original_replace(*args, **kwargs)
        raise OSError("neutral simulated outage")

    with monkeypatch.context() as patch:
        operation = "publish_vector" if outage == "vector" else "replace_document"
        patch.setattr(graph_projection, operation, unavailable)
        first = api.commit_semantic_rewrite(config, request)
    assert first.committed
    assert first.projections_pending
    head = journal_state(config)[0]
    second = api.commit_semantic_rewrite(config, request)
    assert second.ok and second.duplicate
    assert second.head_position == head
    before = payload_state(Path(config.home))
    third = api.commit_semantic_rewrite(config, request)
    assert third.ok and third.duplicate
    assert payload_state(Path(config.home)) == before


def test_request_identity_refuses_changed_preconditions_and_payload(config):
    _commit_a(config)
    api = rewrite_api()
    request = request_for(api)
    assert api.commit_semantic_rewrite(config, request).ok
    head = journal_state(config)[0]
    request.preconditions = api.RewritePreconditions(expected_head_position=0)
    result = api.commit_semantic_rewrite(config, request)
    assert result.status == graph_application.GRAPH_COMMIT_REJECTED_REQUEST
    assert journal_state(config)[0] == head


@pytest.mark.parametrize("domain", ["retrieval", "absent", "../outside", "/absolute", "bad\x00name"])
def test_inspection_is_strictly_zero_write_in_success_and_failure(config, domain):
    _commit_a(config)
    api = rewrite_api()
    before = tree_state(Path(config.home))
    api.inspect_semantic_surfaces(config, domain)
    api.inspect_semantic_surfaces(config, domain, include_body=True)
    assert tree_state(Path(config.home)) == before


@pytest.mark.parametrize("journal_kind", ["missing", "directory", "malformed", "bad_path"])
def test_inspection_cannot_create_or_repair_a_missing_or_bad_database(config, journal_kind):
    api = rewrite_api()
    path = Path(config.graph_journal_path)
    if journal_kind == "directory":
        path.mkdir()
    elif journal_kind == "malformed":
        path.write_text("Not a database.")
    elif journal_kind == "bad_path":
        config.graph_journal_path = str(path) + "\x00invalid"
    before = tree_state(Path(config.home))
    result = api.inspect_semantic_surfaces(config, "retrieval")
    assert not result["success"]
    assert set(result) == {"success", "error"}
    assert tree_state(Path(config.home)) == before


def test_inspection_filters_domains_and_foreign_scopes_and_refuses_symlinks(config, tmp_path):
    _commit_a(config)
    seed_second(config, domain="other")
    api = rewrite_api()
    view = api.inspect_semantic_surfaces(config, "retrieval", include_body=True)
    assert len(view["surfaces"]) == 1
    row = view["surfaces"][0]
    assert row["node_sha256"] == hashlib.sha256(_node(
        "claim_index_nightly", row["body"],
    ).to_json().encode("utf-8")).hexdigest()
    config.namespace = "narrowed"
    foreign = _node("foreign", "Neutral foreign content.")
    foreign.scope = GraphScope(namespace="foreign")
    committed = graph_application.commit_semantic_write(
        config, SemanticWriteRequest(node=foreign, occurred_at=T0 + timedelta(seconds=2)),
        graph_projection.ProjectionBinding(domain="foreign", section="Foreign", body=foreign.content),
    )
    assert committed.ok
    assert api.inspect_semantic_surfaces(config, "foreign")["surfaces"] == []
    outside = tmp_path / "outside.md"
    outside.write_text("Neutral isolated content.")
    (Path(config.knowledge_dir) / "linked.md").symlink_to(outside)
    before = tree_state(Path(config.home))
    assert not api.inspect_semantic_surfaces(config, "linked", include_body=True)["success"]
    assert tree_state(Path(config.home)) == before


def test_empty_rewrite_refuses_without_opening_anything(config):
    api = rewrite_api()
    before = tree_state(Path(config.home))
    result = api.commit_semantic_rewrite(config, api.SemanticRewriteRequest(targets=[], occurred_at=T0))
    assert result.status == "graph_rewrite_rejected_empty"
    assert tree_state(Path(config.home)) == before


def call_tool(awaitable):
    return json.loads(asyncio.run(awaitable))


def test_registered_mcp_tools_accept_real_contract_and_return_real_commits(config, monkeypatch):
    _commit_a(config)
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", None)

    def no_telemetry(*args, **kwargs):
        pytest.fail("inspection must not write access telemetry")

    monkeypatch.setattr(server, "_record_access", no_telemetry)
    api = rewrite_api()
    tools = asyncio.run(server.mcp._list_tools())
    names = {tool.name for tool in tools}
    assert {"inspect_semantic_surfaces", "apply_semantic_rewrite"} <= names
    tool = next(tool for tool in tools if tool.name == "inspect_semantic_surfaces")
    assert tool.parameters["required"] == ["domain"]
    before = tree_state(Path(config.home))
    inspected = call_tool(tool.fn(domain="retrieval"))
    assert inspected["success"] and "body" not in inspected["surfaces"][0]
    assert tree_state(Path(config.home)) == before
    rewrite_tool = next(tool for tool in tools if tool.name == "apply_semantic_rewrite")
    commit = call_tool(rewrite_tool.fn(semantic_rewrite_request=request_for(api).to_dict()))
    assert commit["success"] and commit["graph"]["committed"]
    assert commit["graph"]["projections_pending"] == []


@pytest.mark.parametrize("payload", [None, [], {"private-neutral-input": "must-not-echo"},
                                     {"targets": "must-not-echo"}])
def test_mcp_rewrite_validation_is_bounded_and_redacted(config, monkeypatch, payload):
    monkeypatch.setattr(server, "_config", config)
    before = tree_state(Path(config.home))
    result = call_tool(server.apply_semantic_rewrite(payload))
    assert result["error"] == "invalid_semantic_rewrite_request"
    assert "must-not-echo" not in json.dumps(result)
    assert tree_state(Path(config.home)) == before


@pytest.mark.parametrize("mode", ["legacy", "graph_shadow", "graph_recall"])
def test_mcp_rewrite_refuses_nonwrite_modes_before_opening_storage(config, monkeypatch, mode):
    config.graph_mode = mode
    monkeypatch.setattr(server, "_config", config)
    before = tree_state(Path(config.home))
    result = call_tool(server.apply_semantic_rewrite(request_for(rewrite_api()).to_dict()))
    assert result["error"] == "graph_rewrite_requires_write_mode"
    assert tree_state(Path(config.home)) == before


def test_cold_mcp_inspection_does_not_initialize_config_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setenv("LAYERED_MEMORY_HOME", str(tmp_path / "never-initialized"))
    before = tree_state(tmp_path)
    result = call_tool(server.inspect_semantic_surfaces("neutral"))
    assert not result["success"]
    assert tree_state(tmp_path) == before


def test_rewrite_retires_all_duplicate_heading_occurrences(config):
    _commit_a(config)
    assert seed_second(config, section="Index maintenance", occurrence=1).ok
    api = rewrite_api()
    inspected = api.inspect_semantic_surfaces(config, "retrieval")
    assert {row["occurrence"] for row in inspected["surfaces"]} == {0, 1}
    request = request_for(api)
    request.targets[0].old_node_ids.append("claim_cache")
    result = api.commit_semantic_rewrite(config, request)
    assert result.ok
    text = (Path(config.knowledge_dir) / "retrieval.md").read_text()
    assert "Index maintenance" not in text
    assert "Neutral cache schedule." not in text


@pytest.mark.parametrize("fault", ["sparse_occurrence", "heading_whitespace", "embedded_h2", "renumber_sibling"])
def test_unrepresentable_final_bindings_are_refused_before_append(config, fault):
    _commit_a(config)
    api = rewrite_api()
    request = request_for(api)
    if fault == "sparse_occurrence":
        request.targets[0].surface.occurrence = 3
    elif fault == "heading_whitespace":
        request.targets[0].surface.section = " Padded "
    elif fault == "embedded_h2":
        request.targets[0].node.content = "Neutral text.\n\n## Unexpected\n\nNeutral second section."
    else:
        assert seed_second(config, section="Index maintenance", occurrence=1).ok
    before = payload_state(Path(config.home))
    head = journal_state(config)[0]
    result = api.commit_semantic_rewrite(config, request)
    assert not result.committed
    assert result.status == graph_application.GRAPH_COMMIT_REJECTED_BINDING
    assert journal_state(config)[0] == head
    assert payload_state(Path(config.home)) == before


def test_cas_uses_a_real_publication_lock_even_when_journal_parent_is_missing(config, monkeypatch):
    api = rewrite_api()
    config.graph_journal_path = Path(config.home) / "uncreated" / "journal.db"
    acquired = []
    original = graph_application.FileLock

    def observed_lock(*args, **kwargs):
        acquired.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(graph_application, "FileLock", observed_lock)
    request = request_for(api, preconditions=api.RewritePreconditions(expected_head_position=99))
    result = api.commit_semantic_rewrite(config, request)
    assert result.status == "graph_rewrite_rejected_preconditions"
    assert len(acquired) == 1


def test_inspection_never_creates_wal_sidecars_or_changes_directory_mtime(config):
    _commit_a(config)
    with sqlite3.connect(config.graph_journal_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    before = tree_state(Path(config.home))
    result = rewrite_api().inspect_semantic_surfaces(config, "retrieval")
    assert tree_state(Path(config.home)) == before
    assert not result["success"]


def test_inspection_does_not_follow_a_domain_alias_to_another_domain(config):
    _commit_a(config)
    seed_second(config, domain="other", section="Index maintenance")
    path = Path(config.knowledge_dir) / "retrieval.md"
    path.unlink()
    path.symlink_to(path.with_name("other.md"))
    before = tree_state(Path(config.home))
    result = rewrite_api().inspect_semantic_surfaces(config, "retrieval", include_body=True)
    assert not result["success"]
    assert tree_state(Path(config.home)) == before


def test_multi_domain_pending_group_retries_only_unpublished_domains(config, monkeypatch):
    _commit_a(config)
    seed_second(config, domain="zeta")
    api = rewrite_api()
    request = group_request(api)
    request.targets[1].surface.domain = "zeta"
    request.preconditions = api.RewritePreconditions(expected_head_position=journal_state(config)[0])
    replacements = []
    appends = []
    original_replace = graph_projection.replace_document
    original_append = MemoryEventJournal.append
    failing = [True]

    def replace(path, content):
        replacements.append(path.name)
        if path.name == "zeta.md" and failing:
            raise OSError("neutral second-domain outage")
        return original_replace(path, content)

    def append(self, *args, **kwargs):
        appends.append(args[0])
        return original_append(self, *args, **kwargs)

    monkeypatch.setattr(graph_projection, "replace_document", replace)
    monkeypatch.setattr(MemoryEventJournal, "append", append)
    first = api.commit_semantic_rewrite(config, request)
    assert first.committed and first.projections_pending
    assert replacements == ["retrieval.md", "zeta.md"]
    failing.clear()
    second = api.commit_semantic_rewrite(config, request)
    assert second.ok and second.duplicate
    assert replacements == ["retrieval.md", "zeta.md", "zeta.md"]
    assert len(appends) == 1
    assert second.head_position == first.head_position


@pytest.mark.parametrize("fault", ["targets_limit", "bytes_limit", "domain_traversal"])
def test_registered_rewrite_tool_enforces_transport_boundaries(config, monkeypatch, fault):
    monkeypatch.setattr(server, "_config", config)
    api = rewrite_api()
    payload = request_for(api).to_dict()
    if fault == "targets_limit":
        payload["targets"] *= 101
    elif fault == "bytes_limit":
        payload["padding"] = "x" * (1024 * 1024)
    else:
        payload["targets"][0]["surface"]["domain"] = "../outside"
    tool = next(tool for tool in asyncio.run(server.mcp._list_tools())
                if tool.name == "apply_semantic_rewrite")
    before = tree_state(Path(config.home))
    result = call_tool(tool.fn(semantic_rewrite_request=payload))
    assert result["error"] == "invalid_semantic_rewrite_request"
    assert "padding" not in json.dumps(result)
    assert tree_state(Path(config.home)) == before


def test_registered_inspection_body_opt_in_and_rewrite_pending_response(config, monkeypatch):
    _commit_a(config)
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", None)
    api = rewrite_api()
    tools = {tool.name: tool for tool in asyncio.run(server.mcp._list_tools())}
    before = tree_state(Path(config.home))
    inspected = call_tool(tools["inspect_semantic_surfaces"].fn(domain="retrieval", include_body=True))
    assert inspected["surfaces"][0]["body"]
    assert tree_state(Path(config.home)) == before
    request = request_for(api, preconditions=full_preconditions(api, config))

    def unavailable(*args, **kwargs):
        raise OSError("neutral vector outage")

    with monkeypatch.context() as patch:
        patch.setattr(graph_projection, "publish_vector", unavailable)
        first = call_tool(tools["apply_semantic_rewrite"].fn(semantic_rewrite_request=request.to_dict()))
    assert first["success"] and first["graph"]["committed"]
    assert first["graph"]["projections_pending"] and first["warning"]
    second = call_tool(tools["apply_semantic_rewrite"].fn(semantic_rewrite_request=request.to_dict()))
    assert second["success"] and second["graph"]["duplicate"]
    assert not second["graph"]["projections_pending"]

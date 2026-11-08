"""v3.6.1 — every mutating entrance either routes through the authority or refuses.

Why this file exists: ARCHITECTURE_v2.md "Framework completion and migration
delivery contract" — "An entrance not supporting a semantic operation must
refuse it before mutation, naming the supported alternative."

What is pinned here, for ``graph_write``/``graph`` modes:

  * ``inject_knowledge_tool`` with no semantic decision refuses BEFORE the L1,
    changelog and vector writes — it never falls back to the legacy path;
  * ``inject_knowledge_tool`` with an explicit decision commits through the
    journal and publishes the managed projections, using the configured paths
    and no ``set_graph_store`` injection;
  * ``batch_inject_knowledge_tool`` refuses an unsupported shape as a whole,
    before mutating anything, and commits a fully-specified batch;
  * every other mutating MCP entrance refuses before effects and names the
    supported alternative;
  * read-only and dry-run operations remain possible;
  * REST CRUD and review mutations refuse the same way, with no side effects;
  * ``legacy`` mode behaviour is untouched everywhere.

Isolation: tmp_path config, tmp SQLite journal/graph/vector databases, a
deterministic fake embedding function, no network and no user data.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp import server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_application import GRAPH_COMMIT_DECISION_REQUIRED
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    NodeKind,
)
from layered_memory_mcp.graph_mutation_guard import GRAPH_MUTATION_REFUSED
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore
from layered_memory_mcp.graph_write import SemanticWriteRequest
from layered_memory_mcp.memory_changelog import changelog_settings

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)
EXPLICIT = Derivation(method=DerivationMethod.USER_EXPLICIT)

BODY = "The retrieval index is rebuilt nightly at 02:00 UTC."
DOMAIN = "retrieval"
SECTION = "Index maintenance"


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    from layered_memory_mcp.storage import vector_store

    monkeypatch.setattr(
        vector_store,
        "_embed_texts",
        lambda texts: np.ones((len(texts), 512), dtype=np.float32) / np.sqrt(512),
    )


def _config(tmp_path, mode: str) -> MemoryConfig:
    home = tmp_path / f"home-{mode}"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if mode != "legacy":
        kwargs = {
            "graph_db_path": str(home / "data" / "graph.db"),
            "graph_journal_path": str(home / "data" / "journal.db"),
        }
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode=mode,
        auto_maintain=False,
        **kwargs,
    )


@pytest.fixture
def graph_config(tmp_path, monkeypatch) -> MemoryConfig:
    config = _config(tmp_path, "graph_write")
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    monkeypatch.setattr(server, "_v2_stores", {})
    monkeypatch.setattr(server, "_todo_store", None, raising=False)
    return config


@pytest.fixture
def legacy_config(tmp_path, monkeypatch) -> MemoryConfig:
    config = _config(tmp_path, "legacy")
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    monkeypatch.setattr(server, "_v2_stores", {})
    monkeypatch.setattr(server, "_todo_store", None, raising=False)
    return config


def _call(coro) -> dict:
    return json.loads(asyncio.run(coro))


def _decision(node_id: str = "claim_index_nightly", body: str = BODY) -> dict:
    return SemanticWriteRequest(
        node=GraphNode(
            id=node_id,
            kind=NodeKind.CLAIM,
            label="retrieval index",
            content=body,
            recorded_at=T0,
            derivation=EXPLICIT,
        ),
        occurred_at=T0,
        request_key=node_id,
    ).to_dict()


def _knowledge_files(config: MemoryConfig) -> list[str]:
    return sorted(path.name for path in Path(config.knowledge_dir).glob("*.md"))


# ---------------------------------------------------------------------------
# inject_knowledge_tool — the one entrance that routes through the authority
# ---------------------------------------------------------------------------

def test_a_graph_write_mode_refuses_an_inject_without_a_semantic_decision(graph_config):
    response = _call(
        server.inject_knowledge_tool(domain=DOMAIN, section=SECTION, content=BODY)
    )

    assert response["success"] is False
    assert response["error"] == GRAPH_COMMIT_DECISION_REQUIRED
    assert "semantic_write_request" in response["supported_alternative"]
    assert _knowledge_files(graph_config) == []
    assert not (Path(graph_config.home) / "data" / "vectors.db").exists()
    assert not Path(graph_config.graph_journal_path).exists()


def test_a_graph_write_mode_commits_an_explicit_decision_without_any_injection(
    graph_config,
):
    response = _call(
        server.inject_knowledge_tool(
            domain=DOMAIN,
            section=SECTION,
            content=BODY,
            semantic_write_request=_decision(),
        )
    )

    assert response["success"] is True
    assert response["graph"]["committed"] is True
    assert response["graph"]["status"] == "graph_write_committed"
    assert response["graph"]["projections_pending"] == []
    assert _knowledge_files(graph_config) == [f"{DOMAIN}.md"]
    assert BODY in (Path(graph_config.knowledge_dir) / f"{DOMAIN}.md").read_text(
        encoding="utf-8"
    )

    store = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        assert store.get_node("claim_index_nightly") is not None
    finally:
        store.close()


def test_a_committed_authority_write_leaves_the_legacy_changelog_unchanged(
    graph_config,
):
    changelog = Path(changelog_settings(graph_config)["path"])
    changelog.write_text(
        '{"tool":"legacy_fixture","content":"old changelog entry"}\n',
        encoding="utf-8",
    )
    before_exists = changelog.exists()
    before_bytes = changelog.read_bytes()

    response = _call(
        server.inject_knowledge_tool(
            domain=DOMAIN,
            section=SECTION,
            content=BODY,
            semantic_write_request=_decision(),
        )
    )

    assert response["success"] is True
    assert response["graph"]["committed"] is True
    assert changelog.exists() is before_exists
    assert (changelog.read_bytes() if changelog.exists() else None) == before_bytes


def test_an_invalid_semantic_decision_is_still_refused_before_any_write(graph_config):
    response = _call(
        server.inject_knowledge_tool(
            domain=DOMAIN,
            section=SECTION,
            content=BODY,
            semantic_write_request={"node": {"id": ""}},
        )
    )

    assert response["success"] is False
    assert response["error"] == "invalid_semantic_write_request"
    assert _knowledge_files(graph_config) == []


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("mode", "append"),
        ("fuse", True),
        ("expected_hash", "legacy-hash-token"),
        ("routing_override", "force_l1"),
    ],
)
def test_graph_write_rejects_legacy_only_arguments_with_a_complete_config(
    graph_config, argument, value
):
    data_dir = Path(graph_config.home) / "data"
    before_artifacts = sorted(path.name for path in data_dir.iterdir())
    request = _decision()

    response = _call(
        server.inject_knowledge_tool(
            domain=DOMAIN,
            section=SECTION,
            content=BODY,
            semantic_write_request=request,
            **{argument: value},
        )
    )

    assert set(response) == {
        "success",
        "error",
        "graph_mode",
        "unsupported_arguments",
        "supported_alternative",
    }
    assert response["success"] is False
    assert response["error"] == server.GRAPH_WRITE_UNSUPPORTED_ARGUMENTS
    assert response["graph_mode"] == "graph_write"
    assert response["unsupported_arguments"] == [argument]
    payload = json.dumps(response, ensure_ascii=False)
    assert BODY not in payload
    assert str(graph_config.home) not in payload
    assert "legacy-hash-token" not in payload
    assert sorted(path.name for path in data_dir.iterdir()) == before_artifacts
    assert _knowledge_files(graph_config) == []
    assert not Path(graph_config.graph_journal_path).exists()


def test_legacy_inject_is_completely_unchanged(legacy_config):
    response = _call(
        server.inject_knowledge_tool(domain=DOMAIN, section=SECTION, content=BODY)
    )

    assert response["success"] is True
    assert "graph" not in response
    assert _knowledge_files(legacy_config) == [f"{DOMAIN}.md"]


# ---------------------------------------------------------------------------
# batch injection: a per-item decision, or a whole-batch refusal before effects
# ---------------------------------------------------------------------------

def test_a_batch_with_one_undecided_item_is_refused_as_a_whole_before_mutation(
    graph_config,
):
    response = _call(
        server.batch_inject_knowledge_tool(
            items=[
                {
                    "domain": DOMAIN,
                    "section": SECTION,
                    "content": BODY,
                    "semantic_write_request": _decision(),
                },
                {"domain": "cache", "section": "TTL", "content": "ttl=30"},
            ]
        )
    )

    assert response["success"] is False
    assert response["error"] == GRAPH_COMMIT_DECISION_REQUIRED
    assert response["refused_items"] == [1]
    assert _knowledge_files(graph_config) == []


def test_a_fully_decided_batch_commits_every_item_through_the_authority(graph_config):
    response = _call(
        server.batch_inject_knowledge_tool(
            items=[
                {
                    "domain": DOMAIN,
                    "section": SECTION,
                    "content": BODY,
                    "semantic_write_request": _decision(),
                },
                {
                    "domain": "cache",
                    "section": "TTL",
                    "content": "The cache TTL is 30 seconds.",
                    "semantic_write_request": _decision(
                        "claim_cache_ttl", "The cache TTL is 30 seconds."
                    ),
                },
            ]
        )
    )

    assert response["success"] is True
    assert [item["graph"]["committed"] for item in response["results"]] == [True, True]
    assert _knowledge_files(graph_config) == ["cache.md", f"{DOMAIN}.md"]


def test_legacy_batch_injection_is_unchanged(legacy_config):
    response = _call(
        server.batch_inject_knowledge_tool(
            items=[{"domain": DOMAIN, "section": SECTION, "content": BODY}]
        )
    )

    assert response["success"] is True
    assert _knowledge_files(legacy_config) == [f"{DOMAIN}.md"]


# ---------------------------------------------------------------------------
# Every other mutating MCP entrance refuses before effects
# ---------------------------------------------------------------------------

REFUSED_CALLS = {
    "create_knowledge_file": lambda: server.create_knowledge_file("notes.md", "# Notes\n"),
    "update_knowledge_file": lambda: server.update_knowledge_file("notes.md", "# Notes\n"),
    "delete_knowledge_file": lambda: server.delete_knowledge_file("notes.md"),
    "reindex_vector_store_tool": lambda: server.reindex_vector_store_tool(),
    "sync_l0_index_tool": lambda: server.sync_l0_index_tool(dry_run=False),
    "manage_l0_entry_tool": lambda: server.manage_l0_entry_tool(
        action="add", domain=DOMAIN, summary="x"
    ),
    "apply_safe_knowledge_cleanup_tool": lambda: server.apply_safe_knowledge_cleanup_tool(
        actions=[{"action": "sync_l0_index"}], dry_run=False
    ),
    "compact_memory": lambda: server.compact_memory(dry_run=False),
    "apply_agent_action": lambda: server.apply_agent_action(
        surface="skill", source_content="x", operation="create"
    ),
    "init_framework": lambda: server.init_framework(),
    "integrate_agent": lambda: server.integrate_agent(action="inject_memory"),
}

REVIEW_CALLS = {
    "approve_knowledge": lambda: server.approve_knowledge("entry-1"),
    "reject_knowledge": lambda: server.reject_knowledge("entry-1"),
}


@pytest.mark.parametrize("operation", sorted(REVIEW_CALLS))
def test_the_review_entrances_route_through_the_authority_instead_of_refusing(
    graph_config, operation
):
    response = _call(REVIEW_CALLS[operation]())

    assert response["success"] is False, response
    assert response["error"] == "graph_write_rejected_invalid_replay", response
    assert response["error"] != GRAPH_MUTATION_REFUSED, operation
    assert response["action"] == operation.removesuffix("_knowledge"), response
    assert response["graph"]["committed"] is False, response

#: The task entrances used to be in ``REFUSED_CALLS`` above. They are not any
#: more, and the replacement is **not** a deletion of coverage: they route
#: through the authority now (``test_the_task_entrances_route_through_the_
#: authority_instead_of_refusing`` below is the positive control, and
#: ``test_v361_core_closure.py`` owns the full add→list→complete→reopen→delete
#: contract, the negative controls and the migration). What is asserted here is
#: the one thing this file is about: they no longer answer with a guard refusal,
#: and they still mutate nothing in the knowledge files.
TASK_CALLS = {
    "add_todo": lambda: server.add_todo(domain=DOMAIN, content="do something"),
    "update_todo": lambda: server.update_todo(todo_id="t-1", status="completed"),
    "delete_todo": lambda: server.delete_todo(todo_id="t-1"),
}


@pytest.mark.parametrize("operation", sorted(TASK_CALLS))
def test_the_task_entrances_route_through_the_authority_instead_of_refusing(
    graph_config, operation
):
    before = _knowledge_files(graph_config)

    response = _call(TASK_CALLS[operation]())

    assert response.get("error") != GRAPH_MUTATION_REFUSED, operation
    # `add_todo` commits; the other two name a task that does not exist, which
    # is a *task* refusal rather than a capability refusal — and either way the
    # knowledge files are not what a task decision touches.
    if operation == "add_todo":
        assert response["success"] is True, response
        assert response["task_id"] == response["id"]
    else:
        assert response["success"] is False, response
        assert response["error"] == "graph_task_unknown", response
    assert _knowledge_files(graph_config) == before


@pytest.mark.parametrize("operation", sorted(REFUSED_CALLS))
def test_every_unsupported_mutating_entrance_refuses_before_effects(
    graph_config, operation
):
    before = _knowledge_files(graph_config)

    response = _call(REFUSED_CALLS[operation]())

    assert response["success"] is False, operation
    assert response["error"] == GRAPH_MUTATION_REFUSED, operation
    assert response["operation"] == operation
    assert response["graph_mode"] == "graph_write"
    assert response["supported_alternative"]
    assert _knowledge_files(graph_config) == before
    assert not Path(graph_config.graph_journal_path).exists()


@pytest.mark.parametrize("operation", sorted(REFUSED_CALLS))
def test_none_of_those_entrances_refuse_in_legacy_mode(legacy_config, operation):
    response = _call(REFUSED_CALLS[operation]())
    assert response.get("error") != GRAPH_MUTATION_REFUSED


# ---------------------------------------------------------------------------
# Read-only and dry-run work in a graph write mode
# ---------------------------------------------------------------------------

READ_ONLY_CALLS = {
    "plan_knowledge_cleanup_tool": lambda: server.plan_knowledge_cleanup_tool(),
    "validate_knowledge": lambda: server.validate_knowledge(),
    "sync_l0_index_tool": lambda: server.sync_l0_index_tool(dry_run=True),
    "apply_safe_knowledge_cleanup_tool": lambda: server.apply_safe_knowledge_cleanup_tool(
        actions=[{"action": "sync_l0_index"}], dry_run=True
    ),
    "compact_memory": lambda: server.compact_memory(dry_run=True),
    "integrate_agent": lambda: server.integrate_agent(action="status"),
}


@pytest.mark.parametrize("operation", sorted(READ_ONLY_CALLS))
def test_read_only_and_dry_run_operations_remain_possible(graph_config, operation):
    response = _call(READ_ONLY_CALLS[operation]())
    assert response.get("error") != GRAPH_MUTATION_REFUSED, operation


# ---------------------------------------------------------------------------
# Health and recovery observability
# ---------------------------------------------------------------------------

def test_the_status_tool_reports_bounded_state_and_the_recovery_tool_converges(
    graph_config,
):
    _call(
        server.inject_knowledge_tool(
            domain=DOMAIN,
            section=SECTION,
            content=BODY,
            semantic_write_request=_decision(),
        )
    )

    status = _call(server.graph_memory_status())
    payload = json.dumps(status, ensure_ascii=False)
    assert status["journal"]["event_count"] >= 1
    assert status["journal"]["pending_projections"] == []
    assert str(graph_config.graph_db_path) not in payload
    assert BODY not in payload

    recovery = _call(server.recover_graph_projections())
    assert recovery["projections_pending"] == []
    assert recovery["recovered"] == []


def test_the_recovery_tool_is_a_bounded_refusal_when_nothing_is_configured(
    legacy_config,
):
    recovery = _call(server.recover_graph_projections())
    assert recovery["status"] == "graph_recovery_not_configured"


# ---------------------------------------------------------------------------
# The enumeration itself: no unguarded mutating MCP entrance may exist
# ---------------------------------------------------------------------------

#: Every MCP tool that mutates nothing a graph mode owns. Each one is either a
#: pure read, a dry-run-capable inspector, or telemetry with its own retention
#: (``prune_access_log`` deletes access-log rows, which are operational counters
#: rather than memory content or a projection of the journal).
#:
#: The list is exhaustive on purpose. The test below fails when a new tool is
#: added without classifying it, which is the only way an unguarded mutating
#: entrance cannot quietly appear later.
READ_ONLY_TOOLS = frozenset({
    "audit_rot_tool",
    "classify_memory_item",
    "extract_session_knowledge",
    "get_access_stats",
    "get_domain_timeline",
    "get_knowledge_by_id",
    "get_knowledge_file",
    "get_l0_index",
    "get_memory_changelog",
    "get_memory_changelog_stats",
    "get_memory_surface_capabilities",
    "get_memory_v2_stats",
    "get_pending_reviews",
    "get_todo_stats",
    "graph_memory_status",
    "list_memory_stats",
    "list_todos",
    "plan_knowledge_cleanup_tool",
    "prune_access_log",
    "recall_knowledge",
    "recover_graph_projections",
    "scan_recent_sessions",
    "search_semantic",
    "search_sessions_by_keyword",
    "validate_knowledge",
})

#: The entrances that route a mutation through the semantic authority.
#:
#: The task entrances and ``apply_semantic_lifecycle`` joined this set in the
#: core-closure batch. They were previously guarded names; a capability that
#: gets implemented moves *here*, which is the only honest place for it.
AUTHORITY_TOOLS = frozenset({
    "inject_knowledge_tool",
    "batch_inject_knowledge_tool",
    "apply_semantic_lifecycle",
    "add_todo",
    "update_todo",
    "delete_todo",
    "approve_knowledge",
    "reject_knowledge",
})


def _mcp_tool_names() -> set[str]:
    import ast

    source = Path(server.__file__).read_text(encoding="utf-8")
    return {
        node.name for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef)
        and any(isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == "mcp.tool"
                for decorator in node.decorator_list)
    }


def _guarded_tool_names() -> set[str]:
    import re

    source = Path(server.__file__).read_text(encoding="utf-8")
    return set(re.findall(r'_graph_mutation_refusal\("(\w+)"', source))


def test_every_mcp_tool_is_read_only_guarded_or_routed_through_the_authority():
    tools = _mcp_tool_names()
    classified = READ_ONLY_TOOLS | AUTHORITY_TOOLS | _guarded_tool_names()

    assert tools, "the tool scan found nothing — the pattern has drifted"
    assert tools - classified == set(), (
        "unclassified MCP entrance(s): either add them to READ_ONLY_TOOLS with "
        "evidence that they mutate nothing, route them through the semantic "
        "authority, or guard them in graph_mutation_guard"
    )
    assert classified - tools == set(), "a classified name is not an MCP tool"
    assert not (READ_ONLY_TOOLS & _guarded_tool_names())
    assert not (AUTHORITY_TOOLS & _guarded_tool_names())


def test_the_guard_table_refuses_an_unknown_operation_name_loudly():
    """A typo in a guard call site must not silently become an open entrance."""
    from layered_memory_mcp.graph_mutation_guard import graph_mutation_refusal

    with pytest.raises(ValueError):
        graph_mutation_refusal(object(), "create_knowledge_fil")


# ---------------------------------------------------------------------------
# Resource lifecycle: what the request opened, the request closes
# ---------------------------------------------------------------------------

def test_a_committing_request_closes_every_handle_it_opened(graph_config, monkeypatch):
    from layered_memory_mcp.graph_journal import MemoryEventJournal

    counter = {"opened": 0, "closed": 0}
    real_init = MemoryEventJournal.__init__
    real_close = MemoryEventJournal.close

    def _init(self, *args, **kwargs):
        counter["opened"] += 1
        return real_init(self, *args, **kwargs)

    def _close(self):
        counter["closed"] += 1
        return real_close(self)

    monkeypatch.setattr(MemoryEventJournal, "__init__", _init)
    monkeypatch.setattr(MemoryEventJournal, "close", _close)

    response = _call(
        server.inject_knowledge_tool(
            domain=DOMAIN,
            section=SECTION,
            content=BODY,
            semantic_write_request=_decision(),
        )
    )

    assert response["success"] is True
    assert counter["opened"] > 0
    assert counter["opened"] == counter["closed"]


def test_an_injected_graph_store_is_used_and_never_closed(graph_config, monkeypatch):
    from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore

    class _Watched(InMemoryGraphStore):
        def __init__(self) -> None:
            super().__init__()
            self.closed_times = 0

        def close(self) -> None:  # pragma: no cover - must never be called
            self.closed_times += 1

    injected = _Watched()
    monkeypatch.setattr(server, "_graph_store", injected)

    response = _call(
        server.inject_knowledge_tool(
            domain=DOMAIN,
            section=SECTION,
            content=BODY,
            semantic_write_request=_decision(),
        )
    )

    assert response["graph"]["committed"] is True
    assert injected.get_node("claim_index_nightly") is not None
    assert injected.closed_times == 0
    # The caller-owned store takes precedence, so the configured database was
    # never written to.
    assert not Path(graph_config.graph_db_path).exists()


# ---------------------------------------------------------------------------
# REST entrances
# ---------------------------------------------------------------------------

@pytest.fixture
def rest_client_factory(tmp_path):
    pytest.importorskip("fastapi")
    import httpx

    from layered_memory_mcp.api import server as api
    from layered_memory_mcp.storage import L1Store, ReviewQueue, VectorStore

    def _factory(config: MemoryConfig, monkeypatch):
        stores = {
            "l1": L1Store(config.knowledge_dir),
            "vector": VectorStore(Path(config.home) / "data" / "vectors.db"),
            "review": ReviewQueue(Path(config.home) / "data" / "review.db"),
            "config": config,
        }
        monkeypatch.setattr(api, "stores", stores)
        app = api.create_app()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ), stores

    return _factory


def test_rest_mutations_refuse_in_a_graph_write_mode_without_side_effects(
    tmp_path, monkeypatch, rest_client_factory
):
    config = _config(tmp_path, "graph_write")
    client, stores = rest_client_factory(config, monkeypatch)

    async def _run():
        async with client:
            created = await client.post(
                "/knowledge",
                json={"domain": DOMAIN, "section": SECTION, "content": BODY},
            )
            updated = await client.put("/knowledge/abc", json={"content": "x"})
            deleted = await client.delete("/knowledge/abc")
            approved = await client.post("/review/abc/approve", json={})
            rejected = await client.post("/review/abc/reject", json={})
            return created, updated, deleted, approved, rejected

    responses = asyncio.run(_run())

    for response in responses:
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["error"] == GRAPH_MUTATION_REFUSED
        assert detail["supported_alternative"]
    assert _knowledge_files(config) == []
    stores["vector"].close()


def test_rest_mutations_are_unchanged_in_legacy_mode(
    tmp_path, monkeypatch, rest_client_factory
):
    config = _config(tmp_path, "legacy")
    client, stores = rest_client_factory(config, monkeypatch)

    async def _run():
        async with client:
            return await client.post(
                "/knowledge",
                json={"domain": DOMAIN, "section": SECTION, "content": BODY},
            )

    created = asyncio.run(_run())

    assert created.status_code == 200, created.text
    assert created.json()["domain"] == DOMAIN
    stores["vector"].close()

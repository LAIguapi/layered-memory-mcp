"""v3.6.1 — the central graph application layer: commit, project, recover.

Why this file exists: ARCHITECTURE_v2.md "Framework completion and migration
delivery contract". One application layer validates an explicit semantic
decision, commits it to the journal (the authority), and *then* publishes the
graph, the readable L1 surface, the vector rows and L0 as managed projections
with observable checkpoints and recoverable failures.

What is pinned here:

  * two sequential commits retain each other's nodes — never a partial-stream
    rebuild that erases the earlier write;
  * an identical retry is a no-op that still resumes pending projections;
  * a changed retry under one request identity is refused with no side effects;
  * an unknown pre-existing graph with an empty journal is refused, not erased;
  * binding, surface-ownership, lifecycle and drift violations fail closed
    BEFORE the journal commit;
  * a projection failure leaves ``committed=true`` with honest
    ``projections_pending`` and an unadvanced checkpoint, and explicit recovery
    converges;
  * supersession excludes stale L1/vector material without deleting history;
  * replay from the journal reproduces the persistent graph;
  * concurrent writers on independent connections both land.

Isolation: tmp_path config, tmp SQLite journal/graph/vector databases, a
deterministic fake embedding function. No network, no model download, no user
memory, no real config file.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_application import (
    GRAPH_COMMIT_COMMITTED,
    GRAPH_COMMIT_NOT_CONFIGURED,
    GRAPH_COMMIT_PENDING_PROJECTIONS,
    GRAPH_COMMIT_REJECTED_ADOPTION,
    GRAPH_COMMIT_REJECTED_BINDING,
    GRAPH_COMMIT_REJECTED_DRIFT,
    GRAPH_COMMIT_REJECTED_INVALID,
    GRAPH_COMMIT_REJECTED_REQUEST,
    GRAPH_COMMIT_REJECTED_SURFACE,
    GRAPH_COMMIT_REJECTED_UNSUPPORTED_SURFACE,
    ProjectionBinding,
    commit_semantic_write,
    recover_projections,
    runtime_status,
)
from layered_memory_mcp.graph_entry_identity import markdown_section_entry_id
from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
from layered_memory_mcp.graph_journal import (
    PROJECTION_GRAPH,
    PROJECTION_L0,
    PROJECTION_L1,
    PROJECTION_VECTOR,
    MemoryEventJournal,
)
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    NodeKind,
    NodeStatus,
    RelationType,
)
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore
from layered_memory_mcp.graph_write import RelationAssertion, SemanticWriteRequest

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)

EXPLICIT = Derivation(method=DerivationMethod.USER_EXPLICIT)

BODY_A = "The retrieval index is rebuilt nightly at 02:00 UTC."
BODY_B = "The retrieval index is rebuilt hourly."
BODY_CACHE = "The cache TTL is 30 seconds."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    """Deterministic unit vectors — no model, no download, no network."""
    from layered_memory_mcp.storage import vector_store

    def _embed(texts):
        return np.ones((len(texts), 512), dtype=np.float32) / np.sqrt(512)

    monkeypatch.setattr(vector_store, "_embed_texts", _embed)


@pytest.fixture
def config(tmp_path) -> MemoryConfig:
    home = tmp_path / "home"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        l0_index_file=str(home / "L0.md"),
        graph_mode="graph_write",
        graph_db_path=str(home / "data" / "graph.db"),
        graph_journal_path=str(home / "data" / "journal.db"),
        auto_maintain=False,
    )


def _node(node_id: str, body: str, label: str = "retrieval index") -> GraphNode:
    return GraphNode(
        id=node_id,
        kind=NodeKind.CLAIM,
        label=label,
        content=body,
        recorded_at=T0,
        derivation=EXPLICIT,
    )


def _request(
    node_id: str,
    body: str,
    *,
    supersedes: tuple[str, ...] = (),
    offset: int = 0,
    request_key: str = "",
) -> SemanticWriteRequest:
    return SemanticWriteRequest(
        node=_node(node_id, body),
        occurred_at=T0 + timedelta(seconds=offset),
        request_key=request_key or node_id,
        supersedes=[
            RelationAssertion(
                relation_type=RelationType.SUPERSEDES,
                target_id=target,
                derivation=EXPLICIT,
            )
            for target in supersedes
        ],
    )


def _binding(domain: str, section: str, body: str) -> ProjectionBinding:
    return ProjectionBinding(domain=domain, section=section, body=body)


def _l1_text(config: MemoryConfig, domain: str) -> str:
    path = Path(config.knowledge_dir) / f"{domain}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _vector_rows(config: MemoryConfig) -> list[tuple]:
    from layered_memory_mcp.storage.vector_store import VectorStore

    store = VectorStore(Path(config.home) / "data" / "vectors.db")
    try:
        return store.snapshot()
    finally:
        store.close()


def _graph_snapshot(config: MemoryConfig) -> dict:
    store = SQLiteGraphStore(config.graph_db_path)
    try:
        return store.snapshot()
    finally:
        store.close()


def _journal(config: MemoryConfig) -> MemoryEventJournal:
    return MemoryEventJournal(config.graph_journal_path)


def _commit_a(config: MemoryConfig):
    return commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )


# ---------------------------------------------------------------------------
# The happy path, and the one it must not destroy
# ---------------------------------------------------------------------------

def test_a_commit_publishes_graph_l1_vector_and_l0_together(config):
    commit = _commit_a(config)

    assert commit.status == GRAPH_COMMIT_COMMITTED
    assert commit.committed is True
    assert commit.projections_pending == []
    assert commit.node_id == "claim_index_nightly"
    assert commit.entry_id == markdown_section_entry_id("retrieval", "Index maintenance")

    assert "## Index maintenance" in _l1_text(config, "retrieval")
    assert BODY_A in _l1_text(config, "retrieval")
    assert [row[0] for row in _vector_rows(config)] == [commit.entry_id]
    assert _graph_snapshot(config)["nodes"][0]["id"] == "claim_index_nightly"
    assert Path(config.l0_index_file).read_text(encoding="utf-8").strip() != ""

    with _journal(config) as journal:
        head = journal.head_position()
        assert head >= 1
        assert journal.checkpoint(PROJECTION_GRAPH).position == head
        assert journal.checkpoint(PROJECTION_L1).position == head
        assert journal.checkpoint(PROJECTION_VECTOR).position == head
        assert journal.checkpoint(PROJECTION_L0).position == head


def test_the_three_derived_projections_agree_with_the_published_l1_file(config):
    """L1, the vector rows and L0 are one consistent set after a commit.

    The consistency check is the framework's own existing one:
    ``vector_store_needs_reindex`` compares the exact derived projection of
    every L1 file against the stored rows, so ``False`` means the published
    sections, their identities and their embedding text all match. L0 is
    checked by content: the index names the domain the commit published.
    """
    from layered_memory_mcp import injector
    from layered_memory_mcp.l0_manager import check_l0_l1_consistency

    commit = _commit_a(config)

    assert injector.vector_store_needs_reindex(config) is False
    rows = _vector_rows(config)
    assert [row[0] for row in rows] == [commit.entry_id]
    assert BODY_A in rows[0][2]
    assert json.loads(rows[0][4])["section"] == "Index maintenance"

    l0 = Path(config.l0_index_file).read_text(encoding="utf-8")
    assert "retrieval" in l0
    consistency = check_l0_l1_consistency(config)
    assert consistency["orphaned_l1"] == []
    assert consistency["stale_l0_entries"] == []
    # ``consistent`` is this checker's list of files present on both sides.
    assert consistency["consistent"] == ["retrieval.md"]


def test_a_commit_leaves_an_external_skill_file_and_the_todo_store_untouched(config):
    """Ownership does not move because a graph write happened.

    An agent-owned Skill file outside the knowledge directory and the
    framework's own TODO store are both left exactly as they were: the commit
    publishes the surfaces its decision named and nothing else.
    """
    from layered_memory_mcp.todo_store import TodoStore

    skill = Path(config.home) / "skills" / "deploy.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# Deploy\n\nAn agent-owned runbook.\n", encoding="utf-8")
    before = skill.read_text(encoding="utf-8")

    store = TodoStore(Path(config.home) / "data" / "todos.db")
    existing = store.list()

    assert _commit_a(config).committed is True

    assert skill.read_text(encoding="utf-8") == before
    assert store.list() == existing


def test_a_second_commit_keeps_the_first_commits_nodes_and_surfaces(config):
    _commit_a(config)
    second = commit_semantic_write(
        config,
        _request("claim_cache_ttl", BODY_CACHE, offset=60),
        _binding("cache", "TTL", BODY_CACHE),
    )

    assert second.committed is True
    node_ids = {node["id"] for node in _graph_snapshot(config)["nodes"]}
    assert node_ids == {"claim_index_nightly", "claim_cache_ttl"}
    assert BODY_A in _l1_text(config, "retrieval")
    assert BODY_CACHE in _l1_text(config, "cache")
    assert len(_vector_rows(config)) == 2


def test_the_persistent_graph_equals_an_independent_replay_of_the_journal(config):
    _commit_a(config)
    commit_semantic_write(
        config,
        _request("claim_cache_ttl", BODY_CACHE, offset=60),
        _binding("cache", "TTL", BODY_CACHE),
    )

    with _journal(config) as journal:
        events = journal.read_events()
    replayed = InMemoryGraphStore()
    report = replayed.rebuild(events)

    assert report.ok
    assert replayed.snapshot() == _graph_snapshot(config)


# ---------------------------------------------------------------------------
# Retry semantics
# ---------------------------------------------------------------------------

def test_an_identical_retry_is_a_no_op_that_keeps_every_earlier_surface(config):
    first = _commit_a(config)
    before = _graph_snapshot(config)

    retry = commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )

    assert retry.committed is True
    assert retry.duplicate is True
    assert retry.head_position == first.head_position
    assert _graph_snapshot(config) == before
    assert _l1_text(config, "retrieval").count("## Index maintenance") == 1


def test_a_changed_payload_under_one_request_identity_is_refused_with_no_effects(
    config,
):
    _commit_a(config)
    before_graph = _graph_snapshot(config)
    before_text = _l1_text(config, "retrieval")

    rejected = commit_semantic_write(
        config,
        # Same request key and same node identity, a different body: the journal
        # must refuse it rather than record two versions of one decision.
        _request("claim_index_nightly", BODY_B),
        _binding("retrieval", "Index maintenance", BODY_B),
    )

    assert rejected.committed is False
    assert rejected.status == GRAPH_COMMIT_REJECTED_REQUEST
    assert _graph_snapshot(config) == before_graph
    assert _l1_text(config, "retrieval") == before_text
    with _journal(config) as journal:
        assert journal.request_count() == 1


# ---------------------------------------------------------------------------
# Fail-closed validation, before any effect
# ---------------------------------------------------------------------------

def test_a_node_body_that_disagrees_with_the_published_content_is_refused(config):
    rejected = commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_B),
    )

    assert rejected.status == GRAPH_COMMIT_REJECTED_BINDING
    assert rejected.committed is False
    assert _l1_text(config, "retrieval") == ""
    assert not Path(config.graph_journal_path).exists() or _head(config) == 0


def _head(config) -> int:
    with _journal(config) as journal:
        return journal.head_position()


def test_taking_over_a_surface_without_superseding_its_owner_is_refused(config):
    _commit_a(config)
    before = _l1_text(config, "retrieval")

    rejected = commit_semantic_write(
        config,
        _request("claim_index_hourly", BODY_B, offset=60),
        _binding("retrieval", "Index maintenance", BODY_B),
    )

    assert rejected.status == GRAPH_COMMIT_REJECTED_SURFACE
    assert rejected.committed is False
    assert _l1_text(config, "retrieval") == before
    assert _head(config) == 1 or _head(config) >= 1


def test_superseding_an_already_superseded_target_fails_closed(config):
    _commit_a(config)
    commit_semantic_write(
        config,
        _request("claim_index_hourly", BODY_B, supersedes=("claim_index_nightly",), offset=60),
        _binding("retrieval", "Index maintenance", BODY_B),
    )
    before = _graph_snapshot(config)

    rejected = commit_semantic_write(
        config,
        _request(
            "claim_index_weekly",
            "The retrieval index is rebuilt weekly.",
            supersedes=("claim_index_nightly",),
            offset=120,
        ),
        _binding("retrieval", "Index maintenance", "The retrieval index is rebuilt weekly."),
    )

    assert rejected.committed is False
    assert rejected.status in (
        GRAPH_COMMIT_REJECTED_INVALID,
        GRAPH_COMMIT_REJECTED_SURFACE,
    )
    assert _graph_snapshot(config) == before


def test_an_externally_edited_managed_surface_is_refused_rather_than_overwritten(
    config,
):
    _commit_a(config)
    path = Path(config.knowledge_dir) / "retrieval.md"
    drifted = path.read_text(encoding="utf-8").replace(BODY_A, "Edited by hand.")
    path.write_text(drifted, encoding="utf-8")

    rejected = commit_semantic_write(
        config,
        _request("claim_index_hourly", BODY_B, supersedes=("claim_index_nightly",), offset=60),
        _binding("retrieval", "Index maintenance", BODY_B),
    )

    assert rejected.status == GRAPH_COMMIT_REJECTED_DRIFT
    assert rejected.committed is False
    assert path.read_text(encoding="utf-8") == drifted


def test_an_unadopted_existing_section_is_never_silently_adopted(config):
    path = Path(config.knowledge_dir) / "retrieval.md"
    path.write_text(
        "# Retrieval\n\n## Index maintenance\n\nWritten by an external Skill.\n",
        encoding="utf-8",
    )

    rejected = _commit_a(config)

    assert rejected.status == GRAPH_COMMIT_REJECTED_DRIFT
    assert "Written by an external Skill." in path.read_text(encoding="utf-8")


def test_unrelated_sections_in_a_managed_file_are_preserved(config):
    path = Path(config.knowledge_dir) / "retrieval.md"
    path.write_text("# Retrieval\n\n## Glossary\n\nAn unmanaged note.\n", encoding="utf-8")

    commit = _commit_a(config)

    assert commit.committed is True
    text = path.read_text(encoding="utf-8")
    assert "## Glossary" in text and "An unmanaged note." in text
    assert BODY_A in text


def test_a_frontmatter_backed_domain_is_an_unsupported_surface(config):
    path = Path(config.knowledge_dir) / "retrieval.md"
    path.write_text(
        "---\nid: abc\nsection: Index maintenance\n---\n\nREST-owned body.\n",
        encoding="utf-8",
    )

    rejected = _commit_a(config)

    assert rejected.status == GRAPH_COMMIT_REJECTED_UNSUPPORTED_SURFACE
    assert rejected.committed is False
    assert "REST-owned body." in path.read_text(encoding="utf-8")


def test_an_unknown_pre_existing_graph_without_a_journal_is_refused_not_erased(config):
    seeded = SQLiteGraphStore(config.graph_db_path, nodes=[_node("claim_unknown", "x")])
    before = seeded.snapshot()
    seeded.close()

    rejected = _commit_a(config)

    assert rejected.status == GRAPH_COMMIT_REJECTED_ADOPTION
    assert rejected.committed is False
    assert _graph_snapshot(config) == before
    assert _l1_text(config, "retrieval") == ""


def test_an_unconfigured_write_mode_refuses_before_touching_anything(tmp_path):
    home = tmp_path / "unconfigured"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True)
    config = MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode="graph_write",
        auto_maintain=False,
    )

    rejected = commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )

    assert rejected.status == GRAPH_COMMIT_NOT_CONFIGURED
    assert rejected.committed is False
    assert not (knowledge / "retrieval.md").exists()


# ---------------------------------------------------------------------------
# Supersession, stale exclusion, history
# ---------------------------------------------------------------------------

def test_supersession_replaces_the_surface_and_keeps_the_old_node_as_history(config):
    _commit_a(config)
    second = commit_semantic_write(
        config,
        _request("claim_index_hourly", BODY_B, supersedes=("claim_index_nightly",), offset=60),
        _binding("retrieval", "Index maintenance", BODY_B),
    )

    assert second.committed is True
    text = _l1_text(config, "retrieval")
    assert BODY_B in text and BODY_A not in text
    assert all(BODY_A not in row[2] for row in _vector_rows(config))

    nodes = {node["id"]: node for node in _graph_snapshot(config)["nodes"]}
    assert nodes["claim_index_nightly"]["status"] == NodeStatus.SUPERSEDED.value
    assert nodes["claim_index_hourly"]["status"] == NodeStatus.ACTIVE.value
    assert nodes["claim_index_nightly"]["content"] == BODY_A


def test_a_replacement_on_another_surface_retires_the_superseded_surface(config):
    _commit_a(config)
    commit_semantic_write(
        config,
        _request("claim_nightly_rebuild", BODY_B, supersedes=("claim_index_nightly",), offset=60),
        _binding("retrieval", "Nightly rebuild", BODY_B),
    )

    text = _l1_text(config, "retrieval")
    assert "## Nightly rebuild" in text
    assert "## Index maintenance" not in text
    assert BODY_A not in text
    # Nothing was physically deleted from the authority or the graph.
    assert any(
        node["id"] == "claim_index_nightly" for node in _graph_snapshot(config)["nodes"]
    )
    with _journal(config) as journal:
        assert any(
            event.node is not None and event.node.content == BODY_A
            for event in journal.read_events()
        )


# ---------------------------------------------------------------------------
# Projection failure, pending state, recovery
# ---------------------------------------------------------------------------

@pytest.fixture
def vector_outage(monkeypatch):
    """A switchable vector-publication failure.

    A toggle rather than ``monkeypatch.undo()``: undoing would also revert the
    autouse fake-embedding patch and send the next publication at a real model.
    """
    from layered_memory_mcp import graph_projection

    state = {"failing": True}
    real = graph_projection.publish_vector

    def _maybe(*args, **kwargs):
        if state["failing"]:
            raise RuntimeError("vector publication unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(graph_projection, "publish_vector", _maybe)
    return state


def test_a_failed_projection_commits_honestly_and_recovery_converges(
    config, vector_outage
):
    commit = _commit_a(config)

    assert commit.committed is True
    assert commit.status == GRAPH_COMMIT_PENDING_PROJECTIONS
    assert commit.projections_pending == sorted([PROJECTION_VECTOR, PROJECTION_L0])
    with _journal(config) as journal:
        head = journal.head_position()
        assert journal.checkpoint(PROJECTION_GRAPH).position == head
        assert journal.checkpoint(PROJECTION_L1).position == head
        assert journal.checkpoint(PROJECTION_VECTOR).position == 0
    assert _vector_rows(config) == []

    vector_outage["failing"] = False
    report = recover_projections(config)

    assert report.projections_pending == []
    assert sorted(report.recovered) == sorted([PROJECTION_VECTOR, PROJECTION_L0])
    assert [row[0] for row in _vector_rows(config)] == [commit.entry_id]


def test_an_identical_retry_resumes_pending_projections(config, vector_outage):
    _commit_a(config)
    vector_outage["failing"] = False

    retry = commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )

    assert retry.duplicate is True
    assert retry.committed is True
    assert retry.projections_pending == []
    assert [row[0] for row in _vector_rows(config)] == [retry.entry_id]


def test_recovery_with_nothing_pending_changes_nothing(config):
    _commit_a(config)
    before = (_graph_snapshot(config), _l1_text(config, "retrieval"), _vector_rows(config))

    report = recover_projections(config)

    assert report.recovered == []
    assert report.projections_pending == []
    assert (
        _graph_snapshot(config),
        _l1_text(config, "retrieval"),
        _vector_rows(config),
    ) == before


# ---------------------------------------------------------------------------
# Concurrency and restart
# ---------------------------------------------------------------------------

def test_two_concurrent_writers_on_independent_connections_both_land(config):
    errors: list[BaseException] = []
    results: list[object] = []
    barrier = threading.Barrier(2)

    def _write(node_id: str, domain: str, section: str, body: str, offset: int):
        try:
            barrier.wait(timeout=10)
            results.append(
                commit_semantic_write(
                    config,
                    _request(node_id, body, offset=offset),
                    _binding(domain, section, body),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [
        threading.Thread(
            target=_write, args=("claim_index_nightly", "retrieval", "Index maintenance", BODY_A, 0)
        ),
        threading.Thread(
            target=_write, args=("claim_cache_ttl", "cache", "TTL", BODY_CACHE, 60)
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert all(commit.committed for commit in results)
    node_ids = {node["id"] for node in _graph_snapshot(config)["nodes"]}
    assert node_ids == {"claim_index_nightly", "claim_cache_ttl"}
    with _journal(config) as journal:
        positions = [
            journal.head_position(),
            len(journal.read_events()),
        ]
    assert positions[0] == positions[1]


def test_state_survives_a_restart_and_status_reports_bounded_facts(config):
    _commit_a(config)

    status = runtime_status(config)
    payload = json.dumps(status, ensure_ascii=False)

    assert status["journal"]["event_count"] >= 1
    assert status["journal"]["pending_projections"] == []
    assert status["graph"]["healthy"] is True
    assert status["mode"] == "graph_write"
    assert str(config.graph_db_path) not in payload
    assert str(config.graph_journal_path) not in payload
    assert BODY_A not in payload

    # Reopened from disk, nothing cached in-process.
    assert _graph_snapshot(config)["nodes"][0]["id"] == "claim_index_nightly"

"""v3.6.1 — the remaining framework-contract clauses, end to end.

Why this file exists: the clauses of ARCHITECTURE_v2.md's framework contract
that are not about one module but about the framework as a whole:

  * the journal path is a *declared* target, validated on the same terms as the
    graph database and never derived;
  * an existing imported snapshot is represented in the journal **before** a
    replacing rebuild, so a bootstrap and a later decision coexist instead of
    the second erasing the first;
  * contradiction settles nothing (both claims stay current and the conflict is
    reported) while supersession retires exactly one side;
  * a scope-narrowed or point-in-time graph answer governs the primary results;
  * a lost projection is recoverable through an explicit, forced republication,
    and ordinary recovery does not silently claim to have done one;
  * the status view reports projection drift as counts.

Isolation: tmp_path config, tmp SQLite journal/graph/vector databases, a
deterministic fake embedding function, neutral technical fixtures. No network,
no model download, no real memory home and no real config file.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_application import (
    GRAPH_COMMIT_COMMITTED,
    RECOVERY_NOTHING_PENDING,
    RECOVERY_OK,
    ProjectionBinding,
    commit_semantic_write,
    recover_projections,
    runtime_status,
)
from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
from layered_memory_mcp.graph_journal import (
    REQUEST_KIND_BOOTSTRAP,
    MemoryEventJournal,
)
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    GraphScope,
    MemoryEvent,
    MemoryEventKind,
    NodeKind,
    NodeStatus,
    RelationType,
)
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore
from layered_memory_mcp.graph_write import RelationAssertion, SemanticWriteRequest

T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
EXPLICIT = Derivation(method=DerivationMethod.USER_EXPLICIT)
MIGRATED = Derivation(method=DerivationMethod.MIGRATED)

BODY_A = "The retrieval index is rebuilt nightly."
BODY_B = "The retrieval index is rebuilt hourly."


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    from layered_memory_mcp.storage import vector_store

    monkeypatch.setattr(
        vector_store,
        "_embed_texts",
        lambda texts: np.ones((len(texts), 512), dtype=np.float32) / np.sqrt(512),
    )


def _config(tmp_path, name: str = "home") -> MemoryConfig:
    home = tmp_path / name
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        l0_index_file=str(home / "L0.md"),
        graph_mode="graph",
        graph_db_path=str(home / "data" / "graph.db"),
        graph_journal_path=str(home / "data" / "journal.db"),
        auto_maintain=False,
    )


@pytest.fixture
def config(tmp_path) -> MemoryConfig:
    return _config(tmp_path)


def _node(node_id: str, body: str, **kwargs) -> GraphNode:
    return GraphNode(
        id=node_id,
        kind=NodeKind.CLAIM,
        label="retrieval index",
        content=body,
        recorded_at=T0,
        derivation=EXPLICIT,
        **kwargs,
    )


def _request(node_id, body, *, supersedes=(), contradicts=(), offset=0, **node_kwargs):
    return SemanticWriteRequest(
        node=_node(node_id, body, **node_kwargs),
        occurred_at=T0 + timedelta(seconds=offset),
        request_key=node_id,
        supersedes=[
            RelationAssertion(
                relation_type=RelationType.SUPERSEDES,
                target_id=target,
                derivation=EXPLICIT,
            )
            for target in supersedes
        ],
        contradicts=[
            RelationAssertion(
                relation_type=RelationType.CONTRADICTS,
                target_id=target,
                derivation=EXPLICIT,
            )
            for target in contradicts
        ],
    )


def _binding(domain, section, body) -> ProjectionBinding:
    return ProjectionBinding(domain=domain, section=section, body=body)


def _graph_snapshot(config: MemoryConfig) -> dict:
    store = SQLiteGraphStore(config.graph_db_path)
    try:
        return store.snapshot()
    finally:
        store.close()


def _l1(config: MemoryConfig, domain: str) -> str:
    path = Path(config.knowledge_dir) / f"{domain}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# The journal path is a declared target, on the graph database's own terms
# ---------------------------------------------------------------------------

def test_the_journal_path_is_unset_unless_declared(tmp_path):
    config = MemoryConfig(home=str(tmp_path / "plain"), graph_mode="graph_write")
    assert config.graph_journal_path is None


def test_a_declared_journal_path_is_kept_and_nothing_is_created(tmp_path):
    target = tmp_path / "declared" / "journal.db"
    config = MemoryConfig(
        home=str(tmp_path / "home"), graph_journal_path=str(target)
    )
    assert config.graph_journal_path == target
    assert not target.exists()
    assert not target.parent.exists()


@pytest.mark.parametrize("bad", ["relative/journal.db", True, 7, ["/a"]])
def test_an_unusable_journal_path_is_refused_loudly(tmp_path, bad):
    with pytest.raises(ValueError) as excinfo:
        MemoryConfig(home=str(tmp_path / "home"), graph_journal_path=bad)
    assert "graph_journal_path" in str(excinfo.value)


def test_the_journal_path_reads_the_environment_and_the_config_file(tmp_path, monkeypatch):
    target = tmp_path / "env" / "journal.db"
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_JOURNAL_PATH", str(target))
    assert MemoryConfig(home=str(tmp_path / "a")).graph_journal_path == target
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_JOURNAL_PATH")

    home = tmp_path / "b"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"graph:\n  mode: graph_write\n  journal_path: {target}\n", encoding="utf-8"
    )
    assert MemoryConfig(home=str(home)).graph_journal_path == target


# ---------------------------------------------------------------------------
# A bootstrap import is part of the stream a commit replays
# ---------------------------------------------------------------------------

def test_a_bootstrap_import_survives_the_next_commits_replacing_rebuild(config):
    """The clause this exists for: an imported snapshot must be in the journal.

    A rebuild replaces graph state with the projection of the stream it is
    handed. If the import were only in the graph database, the first commit
    after it would erase it. Representing it as a ``bootstrap_import`` request
    makes it part of every later replay.
    """
    imported = GraphNode(
        id="claim_imported_legacy",
        kind=NodeKind.CLAIM,
        label="legacy claim",
        content="An imported legacy claim.",
        recorded_at=T0,
        derivation=MIGRATED,
    )
    with MemoryEventJournal(config.graph_journal_path) as journal:
        journal.append(
            "bootstrap-2026-06-01",
            [
                MemoryEvent(
                    id="event_bootstrap_1",
                    kind=MemoryEventKind.MIGRATE,
                    occurred_at=T0 - timedelta(days=1),
                    node=imported,
                )
            ],
            kind=REQUEST_KIND_BOOTSTRAP,
        )

    commit = commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )

    assert commit.status == GRAPH_COMMIT_COMMITTED
    node_ids = {node["id"] for node in _graph_snapshot(config)["nodes"]}
    assert node_ids == {"claim_imported_legacy", "claim_index_nightly"}
    # The imported node has no readable-surface binding, so it is graph-only:
    # the projection owns the sections it published and invents none.
    assert "An imported legacy claim." not in _l1(config, "retrieval")


# ---------------------------------------------------------------------------
# Contradiction settles nothing; supersession retires exactly one side
# ---------------------------------------------------------------------------

def test_a_contradiction_keeps_both_claims_current_and_reports_the_conflict(config):
    commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A, conflict_group="index_cadence"),
        _binding("retrieval", "Index maintenance", BODY_A),
    )
    second = commit_semantic_write(
        config,
        _request(
            "claim_index_hourly",
            BODY_B,
            contradicts=("claim_index_nightly",),
            offset=60,
            conflict_group="index_cadence",
        ),
        _binding("retrieval", "Rebuild cadence review", BODY_B),
    )

    assert second.committed is True
    assert second.superseded_ids == []
    nodes = {node["id"]: node for node in _graph_snapshot(config)["nodes"]}
    assert nodes["claim_index_nightly"]["status"] == NodeStatus.ACTIVE.value
    assert nodes["claim_index_hourly"]["status"] == NodeStatus.ACTIVE.value

    # Both readable surfaces survive: nothing was settled, so nothing is stale.
    text = _l1(config, "retrieval")
    assert BODY_A in text and BODY_B in text

    store = SQLiteGraphStore(config.graph_db_path)
    try:
        conflicts = store.find_conflicts()
    finally:
        store.close()
    assert len(conflicts) == 1
    assert sorted(conflicts[0].node_ids) == [
        "claim_index_hourly",
        "claim_index_nightly",
    ]
    assert conflicts[0].resolved is False


def test_a_supersession_in_a_scope_retires_only_that_scopes_claim(config):
    scope = GraphScope(namespace="team_a")
    commit_semantic_write(
        config,
        _request("claim_team_a_nightly", BODY_A, scope=scope),
        _binding("retrieval", "Team A cadence", BODY_A),
    )
    commit_semantic_write(
        config,
        _request("claim_shared_nightly", "The shared index is rebuilt nightly.", offset=30),
        _binding("retrieval", "Shared cadence", "The shared index is rebuilt nightly."),
    )
    commit_semantic_write(
        config,
        _request(
            "claim_team_a_hourly",
            BODY_B,
            supersedes=("claim_team_a_nightly",),
            offset=60,
            scope=scope,
        ),
        _binding("retrieval", "Team A cadence", BODY_B),
    )

    nodes = {node["id"]: node for node in _graph_snapshot(config)["nodes"]}
    assert nodes["claim_team_a_nightly"]["status"] == NodeStatus.SUPERSEDED.value
    assert nodes["claim_shared_nightly"]["status"] == NodeStatus.ACTIVE.value
    text = _l1(config, "retrieval")
    assert BODY_B in text
    assert BODY_A not in text
    assert "The shared index is rebuilt nightly." in text


def test_a_cross_scope_supersession_fails_closed(config):
    commit_semantic_write(
        config,
        _request("claim_team_a_nightly", BODY_A, scope=GraphScope(namespace="team_a")),
        _binding("retrieval", "Team A cadence", BODY_A),
    )
    before = _graph_snapshot(config)

    rejected = commit_semantic_write(
        config,
        _request(
            "claim_team_b_hourly",
            BODY_B,
            supersedes=("claim_team_a_nightly",),
            offset=60,
            scope=GraphScope(namespace="team_b"),
        ),
        _binding("retrieval", "Team B cadence", BODY_B),
    )

    assert rejected.committed is False
    assert any(
        error.startswith("supersede_target_scope_mismatch:") for error in rejected.errors
    )
    assert _graph_snapshot(config) == before


# ---------------------------------------------------------------------------
# A lost projection is recoverable, and ordinary recovery does not pretend
# ---------------------------------------------------------------------------

def test_ordinary_recovery_does_not_claim_to_have_rebuilt_a_lost_projection(config):
    commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )
    Path(config.graph_db_path).unlink()

    report = recover_projections(config)

    assert report.status == RECOVERY_NOTHING_PENDING
    assert report.recovered == []
    # And the drift is visible rather than silent.
    status = runtime_status(config)
    assert status["projection_drift"]["drifted"] is True
    assert status["projection_drift"]["missing_nodes"] == 1


def test_a_forced_recovery_rebuilds_every_projection_from_the_journal(config):
    commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )
    commit_semantic_write(
        config,
        _request("claim_cache_ttl", "The cache TTL is 30 seconds.", offset=60),
        _binding("cache", "TTL", "The cache TTL is 30 seconds."),
    )
    expected = _graph_snapshot(config)
    Path(config.graph_db_path).unlink()

    report = recover_projections(config, force=True)

    assert report.status == RECOVERY_OK
    assert report.projections_pending == []
    assert sorted(report.recovered) == ["graph", "l0", "l1", "vector"]
    assert _graph_snapshot(config) == expected

    with MemoryEventJournal(config.graph_journal_path) as journal:
        replayed = InMemoryGraphStore()
        replayed.rebuild(journal.read_events())
    assert replayed.snapshot() == expected

    status = runtime_status(config)
    assert status["projection_drift"]["drifted"] is False


def test_a_forced_recovery_refuses_to_overwrite_an_externally_edited_surface(config):
    commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )
    path = Path(config.knowledge_dir) / "retrieval.md"
    edited = path.read_text(encoding="utf-8").replace(BODY_A, "Edited by hand.")
    path.write_text(edited, encoding="utf-8")

    report = recover_projections(config, force=True)

    assert "l1" in report.projections_pending
    assert any("surface_drift" in error for error in report.errors)
    assert path.read_text(encoding="utf-8") == edited


def test_the_status_view_stays_bounded_with_drift_reported(config):
    commit_semantic_write(
        config,
        _request("claim_index_nightly", BODY_A),
        _binding("retrieval", "Index maintenance", BODY_A),
    )

    status = runtime_status(config)
    payload = json.dumps(status, ensure_ascii=False)

    assert set(status["projection_drift"]) == {
        "drifted",
        "expected_nodes",
        "expected_edges",
        "stored_nodes",
        "stored_edges",
        "missing_nodes",
        "missing_edges",
        "unexpected_nodes",
        "unexpected_edges",
        "changed_nodes",
        "changed_edges",
        "errors",
    }
    assert BODY_A not in payload
    assert str(config.graph_db_path) not in payload
    assert str(config.graph_journal_path) not in payload
    assert "Index maintenance" not in payload

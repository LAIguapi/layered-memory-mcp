"""v3.6.1 — the graph store's read-only entrance, and the observation-only reads.

Why this file exists: the journal already had a separate inspection path, but
three *read* paths still reached a store through a writable one. ``runtime_status``
opened the graph database with the committer's entrance, which establishes a
database and its schema when they are absent — so asking "is the projection
healthy?" on a system that never published created the projection. And
``graph_search._authority_view`` / ``graph_application.pending_surface_entry_ids``
consulted the *authority* through the writable journal path, so an ordinary
recall on a legacy system left a journal behind.

What is pinned here:

  * a missing graph database stays missing — no file, no directory, no sibling
    artefact — and is *reported*, not created;
  * a file that is not a graph, and a store layout this process does not speak,
    are each refused with their own distinguishable refusal and left untouched;
  * an existing graph is fully readable through the inspection handle and
    unchanged by reading it: identical bytes, identical modification time;
  * an inspection handle refuses every mutating method by name, and the
    connection underneath refuses a write even if one slipped past;
  * ``runtime_status`` creates neither store, reports a *deleted* projection as
    the drift it is rather than recreating it, and is repeatable;
  * the writable paths still do all of it on purpose: a commit creates the
    journal and the graph, and a forced recovery recreates a deleted one;
  * the two observation-only journal reads answer exactly as before while
    creating nothing.

Isolation: every database is under pytest's ``tmp_path``, with a deterministic
fake embedding function. No config file, no memory home, no network, no model
download and no user data is touched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp import graph_projection, graph_search
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_application import (
    PROJECTION_STORE_ABSENT,
    ProjectionBinding,
    commit_semantic_write,
    pending_surface_entry_ids,
    recover_projections,
    runtime_status,
)
from layered_memory_mcp.graph_journal import (
    MemoryEventJournal,
    SurfaceRecord,
)
from layered_memory_mcp.graph_models import (
    GRAPH_SCHEMA_VERSION,
    Derivation,
    DerivationMethod,
    GraphNode,
    MemoryEvent,
    MemoryEventKind,
    NodeKind,
)
from layered_memory_mcp.graph_runtime import GraphModeDecision
from layered_memory_mcp.graph_runtime_assembly import (
    GRAPH_STORE_NOT_CONFIGURED,
    GRAPH_STORE_NOT_INITIALIZED,
    GRAPH_STORE_NOT_PRESENT,
    GRAPH_STORE_NOT_REQUESTED,
    GRAPH_STORE_OPEN_FAILED,
    GRAPH_STORE_OPENED,
    GraphStoreReadOnlyError,
    diagnose_graph_runtime,
    graph_store_readonly_session,
    open_graph_store,
    open_graph_store_readonly,
)
from layered_memory_mcp.graph_sqlite_store import (
    STORE_SCHEMA_VERSION,
    SQLiteGraphStore,
)
from layered_memory_mcp.graph_write import SemanticWriteRequest

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)

EXPLICIT = Derivation(method=DerivationMethod.USER_EXPLICIT)

BODY = "The retrieval index is rebuilt nightly at 02:00 UTC."

ENTRY_ID = "mdsec1_abc"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    """Deterministic unit vectors — no model, no download, no network."""
    from layered_memory_mcp.storage import vector_store

    def _embed(texts):
        return np.ones((len(texts), 512), dtype=np.float32) / np.sqrt(512)

    monkeypatch.setattr(vector_store, "_embed_texts", _embed)


@pytest.fixture
def data_dir(tmp_path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def config(tmp_path, data_dir) -> MemoryConfig:
    """A graph-write config whose two databases deliberately do not exist yet."""
    home = tmp_path / "home"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        l0_index_file=str(home / "L0.md"),
        graph_mode="graph_write",
        graph_db_path=str(data_dir / "graph.db"),
        graph_journal_path=str(data_dir / "journal.db"),
        auto_maintain=False,
    )


def _node(node_id: str = "claim_a") -> GraphNode:
    return GraphNode(
        id=node_id,
        kind=NodeKind.CLAIM,
        label="retrieval index",
        content=BODY,
        recorded_at=T0,
        derivation=EXPLICIT,
    )


def _request(node_id: str = "claim_a") -> SemanticWriteRequest:
    return SemanticWriteRequest(
        node=_node(node_id), occurred_at=T0, request_key=node_id
    )


def _binding() -> ProjectionBinding:
    return ProjectionBinding(
        domain="retrieval", section="Index maintenance", body=BODY
    )


def _populate_graph(database: Path) -> None:
    """One node, written by the ordinary writable path."""
    store = SQLiteGraphStore(database, nodes=[_node()])
    store.close()


def _populate_journal(database: Path, *, surface: bool = True) -> str:
    """One committed, *unpublished* decision carrying a readable binding.

    No checkpoint is advanced, so the surface it binds is exactly what
    ``stale_surface_entry_ids`` must report as pending.
    """
    binding = _binding()
    event = MemoryEvent(
        id="event_a1",
        kind=MemoryEventKind.INJECT,
        occurred_at=T0 + timedelta(microseconds=1),
        node=_node(),
        metadata={
            graph_projection.PROJECTION_METADATA_KEY: (
                graph_projection.binding_metadata(binding)
            )
        },
    )
    with MemoryEventJournal(database) as journal:
        journal.append("request-a", [event])
        if surface:
            journal.record_surface(
                SurfaceRecord(
                    entry_id=binding.entry_id,
                    domain=binding.domain,
                    section=binding.section,
                    occurrence=0,
                    node_id="claim_a",
                    body_sha256="a" * 64,
                    position=1,
                )
            )
    return binding.entry_id


class _Fingerprint:
    """Everything about a database file that an observer must not change."""

    def __init__(self, database: Path) -> None:
        stat = database.stat()
        self.digest = hashlib.sha256(database.read_bytes()).hexdigest()
        self.size = stat.st_size
        self.mtime_ns = stat.st_mtime_ns
        self.siblings = sorted(
            entry.name
            for entry in database.parent.iterdir()
            if entry.name.startswith(database.name)
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Fingerprint) and self.__dict__ == other.__dict__

    def __repr__(self) -> str:  # pragma: no cover - failure output only
        return f"_Fingerprint({self.__dict__})"


# ---------------------------------------------------------------------------
# A missing graph database stays missing
# ---------------------------------------------------------------------------

def test_the_read_only_graph_assembly_opens_nothing_unless_asked(config):
    assembly = open_graph_store_readonly(config)
    try:
        assert assembly.report.status == GRAPH_STORE_NOT_REQUESTED
        assert assembly.report.path_configured is True
        assert assembly.report.read_only is True
        assert assembly.store is None
    finally:
        assembly.close()

    assert not Path(config.graph_db_path).exists()


def test_the_read_only_graph_assembly_reports_an_absent_database(config):
    database = Path(config.graph_db_path)

    with graph_store_readonly_session(config, require=True) as assembly:
        assert assembly.report.status == GRAPH_STORE_NOT_PRESENT
        assert assembly.report.path_configured is True
        assert assembly.report.store_opened is False
        assert assembly.report.read_only is True
        assert assembly.report.errors == [GRAPH_STORE_NOT_PRESENT]
        assert assembly.report.healthy is False
        assert assembly.available is False
        assert assembly.store is None

    assert not database.exists()
    assert sorted(entry.name for entry in database.parent.iterdir()) == []


def test_a_missing_parent_directory_is_reported_and_never_created(tmp_path):
    absent = tmp_path / "absent"
    config = MemoryConfig(
        home=str(tmp_path / "home"),
        graph_mode="graph",
        graph_db_path=str(absent / "graph.db"),
        auto_maintain=False,
    )

    with graph_store_readonly_session(config, require=True) as assembly:
        assert assembly.report.status == GRAPH_STORE_NOT_PRESENT
        assert assembly.store is None

    assert not absent.exists()


def test_the_read_only_graph_assembly_reports_an_unconfigured_store(tmp_path):
    bare = MemoryConfig(home=str(tmp_path / "home"), auto_maintain=False)

    with graph_store_readonly_session(bare, require=True) as assembly:
        assert assembly.report.status == GRAPH_STORE_NOT_CONFIGURED
        assert assembly.report.path_configured is False
        assert assembly.report.read_only is True
        assert assembly.store is None


def test_a_file_that_is_not_a_graph_is_reported_rather_than_initialized(
    config, data_dir
):
    database = Path(config.graph_db_path)
    database.write_bytes(b"")
    foreign = data_dir / "other.db"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")

    for target in (database, foreign):
        before = _Fingerprint(target)
        probe = MemoryConfig(
            home=config.home,
            knowledge_dir=config.knowledge_dir,
            graph_mode="graph",
            graph_db_path=str(target),
            auto_maintain=False,
        )

        with graph_store_readonly_session(probe, require=True) as assembly:
            assert assembly.report.status == GRAPH_STORE_NOT_INITIALIZED
            assert assembly.report.errors == [GRAPH_STORE_NOT_INITIALIZED]
            assert assembly.store is None

        assert _Fingerprint(target) == before


def test_a_newer_store_layout_is_refused_by_the_read_only_path_too(config):
    database = Path(config.graph_db_path)
    _populate_graph(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE graph_meta SET value = ? WHERE key = 'store_schema_version'",
            (str(STORE_SCHEMA_VERSION + 1),),
        )
    before = _Fingerprint(database)

    with graph_store_readonly_session(config, require=True) as assembly:
        assert assembly.report.status == GRAPH_STORE_OPEN_FAILED
        assert assembly.report.errors == [
            f"{GRAPH_STORE_OPEN_FAILED}:StoreSchemaError"
        ]
        assert assembly.store is None

    assert _Fingerprint(database) == before
    # §15: the refusal names a class, never the path or the backend's message.
    assert str(database) not in json.dumps(
        assembly.report.model_dump(mode="json"), ensure_ascii=False
    )


# ---------------------------------------------------------------------------
# An existing graph is readable, and unchanged by reading it
# ---------------------------------------------------------------------------

def test_the_read_only_graph_assembly_reads_everything_and_changes_nothing(config):
    database = Path(config.graph_db_path)
    _populate_graph(database)
    before = _Fingerprint(database)

    with graph_store_readonly_session(config, require=True) as assembly:
        report = assembly.report
        store = assembly.store
        assert report.status == GRAPH_STORE_OPENED
        assert report.store_opened is True
        assert report.read_only is True
        assert report.healthy is True
        assert report.node_count == 1
        assert report.edge_count == 0
        assert report.graph_schema_version == GRAPH_SCHEMA_VERSION
        assert report.store_schema_version == STORE_SCHEMA_VERSION
        # An inspection handle cannot rebuild, and says so rather than
        # advertising a capability it refuses.
        assert report.supports_rebuild is False
        assert report.errors == []
        assert assembly.available is True

        assert store is not None
        assert store.read_only is True
        assert store.get_node("claim_a") is not None
        assert [node["id"] for node in store.snapshot()["nodes"]] == ["claim_a"]
        audit = store.audit_drift([_node()], [])
        assert audit.drifted is False
        # The writable seeding write bumped it; inspection reads it back.
        assert store.revision() == 1

    assert _Fingerprint(database) == before


def test_every_mutating_method_is_refused_by_name_on_an_inspection_handle(config):
    database = Path(config.graph_db_path)
    _populate_graph(database)
    before = _Fingerprint(database)

    with graph_store_readonly_session(config, require=True) as assembly:
        store = assembly.store
        assert store is not None
        with pytest.raises(GraphStoreReadOnlyError):
            store.create_node(_node("claim_b"))
        with pytest.raises(GraphStoreReadOnlyError):
            store.append_edge(None)
        with pytest.raises(GraphStoreReadOnlyError):
            store.rebuild([])
        # And the connection underneath refuses a write even if a statement
        # ever slipped past the refusals above (``PRAGMA query_only``).
        with pytest.raises(sqlite3.OperationalError):
            store._connection.execute("CREATE TABLE sneaky (id TEXT)")

    assert _Fingerprint(database) == before


def test_the_fingerprint_used_above_does_notice_a_write(config):
    """Guard for the guards: an unchanged fingerprint has to mean something."""
    database = Path(config.graph_db_path)
    _populate_graph(database)
    before = _Fingerprint(database)

    store = SQLiteGraphStore(database)
    try:
        store.create_node(_node("claim_b"))
    finally:
        store.close()

    assert _Fingerprint(database) != before


# ---------------------------------------------------------------------------
# runtime_status: the diagnostic that must not be able to change its subject
# ---------------------------------------------------------------------------

def test_runtime_status_reports_both_stores_absent_and_creates_neither(
    config, data_dir
):
    status = runtime_status(config)

    assert status["graph"]["status"] == GRAPH_STORE_NOT_PRESENT
    assert status["graph"]["path_configured"] is True
    assert status["graph"]["store_opened"] is False
    assert status["graph"]["healthy"] is False
    assert status["graph"]["errors"] == [GRAPH_STORE_NOT_PRESENT]
    # Nothing expected, nothing missing: an unpublished system is not drifted.
    assert status["projection_drift"]["drifted"] is False
    assert status["projection_drift"]["errors"] == []

    assert sorted(entry.name for entry in data_dir.iterdir()) == []
    payload = json.dumps(status, ensure_ascii=False)
    assert str(data_dir) not in payload
    assert BODY not in payload


def test_runtime_status_leaves_both_existing_databases_unchanged(config):
    commit = commit_semantic_write(config, _request(), _binding())
    assert commit.ok is True
    graph_db = Path(config.graph_db_path)
    journal_db = Path(config.graph_journal_path)
    graph_before = _Fingerprint(graph_db)
    journal_before = _Fingerprint(journal_db)

    status = runtime_status(config)

    assert status["graph"]["status"] == GRAPH_STORE_OPENED
    assert status["graph"]["healthy"] is True
    assert status["graph"]["node_count"] == 1
    assert status["projection_drift"]["drifted"] is False
    assert status["projection_drift"]["errors"] == []
    assert status["journal"]["event_count"] == 1

    for _attempt in range(3):
        runtime_status(config)

    assert _Fingerprint(graph_db) == graph_before
    assert _Fingerprint(journal_db) == journal_before


def test_runtime_status_reports_a_deleted_graph_as_drift_without_recreating_it(
    config,
):
    assert commit_semantic_write(config, _request(), _binding()).ok is True
    graph_db = Path(config.graph_db_path)
    graph_db.unlink()
    journal_before = _Fingerprint(Path(config.graph_journal_path))

    status = runtime_status(config)

    assert status["graph"]["status"] == GRAPH_STORE_NOT_PRESENT
    drift = status["projection_drift"]
    assert drift["drifted"] is True
    assert drift["expected_nodes"] == 1
    assert drift["stored_nodes"] == 0
    assert drift["missing_nodes"] == 1
    assert drift["errors"] == [PROJECTION_STORE_ABSENT]

    # Reported, not repaired, and not quietly brought back as an empty graph.
    assert not graph_db.exists()
    assert _Fingerprint(Path(config.graph_journal_path)) == journal_before


# ---------------------------------------------------------------------------
# The writable paths still publish, which is what makes the above a choice
# ---------------------------------------------------------------------------

def test_a_commit_still_creates_the_journal_and_the_graph_database(config):
    graph_db = Path(config.graph_db_path)
    journal_db = Path(config.graph_journal_path)
    assert not graph_db.exists()
    assert not journal_db.exists()

    commit = commit_semantic_write(config, _request(), _binding())

    assert commit.ok is True
    assert graph_db.is_file()
    assert journal_db.is_file()
    store = SQLiteGraphStore(graph_db)
    try:
        assert [node["id"] for node in store.snapshot()["nodes"]] == ["claim_a"]
    finally:
        store.close()


def test_the_writable_assembly_still_creates_a_missing_graph_database(config):
    database = Path(config.graph_db_path)
    assert not database.exists()

    assembly = open_graph_store(config, require=True)
    try:
        assert assembly.report.status == GRAPH_STORE_OPENED
        assert assembly.report.read_only is False
        assert assembly.report.supports_rebuild is True
        assert assembly.store is not None
    finally:
        assembly.close()

    assert database.is_file()


def test_a_forced_recovery_still_recreates_a_deleted_graph_database(config):
    assert commit_semantic_write(config, _request(), _binding()).ok is True
    graph_db = Path(config.graph_db_path)
    graph_db.unlink()

    report = recover_projections(config, force=True)

    assert report.projections_pending == []
    assert "graph" in report.recovered
    assert graph_db.is_file()
    store = SQLiteGraphStore(graph_db)
    try:
        assert [node["id"] for node in store.snapshot()["nodes"]] == ["claim_a"]
    finally:
        store.close()
    assert runtime_status(config)["projection_drift"]["drifted"] is False


# ---------------------------------------------------------------------------
# The two observation-only journal reads
# ---------------------------------------------------------------------------

def test_pending_surface_entry_ids_creates_no_journal(config, data_dir):
    assert pending_surface_entry_ids(config) == set()

    assert not Path(config.graph_journal_path).exists()
    assert sorted(entry.name for entry in data_dir.iterdir()) == []


def test_pending_surface_entry_ids_reads_an_existing_journal_untouched(config):
    database = Path(config.graph_journal_path)
    entry_id = _populate_journal(database)
    before = _Fingerprint(database)

    assert pending_surface_entry_ids(config) == {entry_id}

    assert _Fingerprint(database) == before


def test_the_recall_authority_view_creates_no_journal(config, data_dir):
    mode = GraphModeDecision.from_config(config)

    assert graph_search._authority_view(config, mode) == ({}, set())

    assert not Path(config.graph_journal_path).exists()
    assert sorted(entry.name for entry in data_dir.iterdir()) == []


def test_the_recall_authority_view_reads_an_existing_journal_untouched(config):
    database = Path(config.graph_journal_path)
    entry_id = _populate_journal(database)
    before = _Fingerprint(database)

    # ``graph_write`` does not let the graph decide a read, so the stale half
    # stays empty — the pre-existing rule, unchanged by the read-only open.
    surfaces, pending = graph_search._authority_view(
        config, GraphModeDecision.from_config(config)
    )
    assert surfaces == {entry_id: "claim_a"}
    assert pending == set()

    # ``graph`` does, and then the unpublished surface is reported as stale.
    reading = MemoryConfig(
        home=config.home,
        knowledge_dir=config.knowledge_dir,
        graph_mode="graph",
        graph_db_path=config.graph_db_path,
        graph_journal_path=config.graph_journal_path,
        auto_maintain=False,
    )
    surfaces, pending = graph_search._authority_view(
        reading, GraphModeDecision.from_config(reading)
    )
    assert surfaces == {entry_id: "claim_a"}
    assert pending == {entry_id}

    assert _Fingerprint(database) == before


def test_a_graph_mode_recall_leaves_an_absent_journal_absent(tmp_path):
    """The end-to-end read: one recall, no authority and no graph created."""
    home = tmp_path / "recall-home"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    config = MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode="graph",
        graph_journal_path=str(data / "journal.db"),
        auto_maintain=False,
    )
    rows = [{"id": ENTRY_ID, "domain": "retrieval", "text": BODY, "score": 0.8}]

    outcome = graph_search.apply_graph_recall(config, rows, identity_key="id")

    # The legacy answer survives untouched, and nothing was brought into being.
    assert [row["id"] for row in outcome.results] == [ENTRY_ID]
    assert outcome.excluded == 0
    assert not Path(config.graph_journal_path).exists()
    assert sorted(entry.name for entry in data.iterdir()) == []


# ---------------------------------------------------------------------------
# The last two read paths that still reached a store through the writable
# entrance: the recall seam's own store open, and the operator diagnostic.
# ---------------------------------------------------------------------------

def _recall_config(tmp_path: Path, data: Path) -> MemoryConfig:
    """A graph-recall config whose graph database is *declared but absent*."""
    home = tmp_path / "recall-graph-home"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode="graph",
        graph_db_path=str(data / "graph.db"),
        graph_journal_path=str(data / "journal.db"),
        auto_maintain=False,
    )


def test_a_graph_mode_recall_leaves_an_absent_graph_database_absent(tmp_path):
    """A recall must not be the thing that creates the graph it consults."""
    data = tmp_path / "recall-data"
    data.mkdir(parents=True, exist_ok=True)
    config = _recall_config(tmp_path, data)
    rows = [{"id": ENTRY_ID, "domain": "retrieval", "text": BODY, "score": 0.8}]

    outcome = graph_search.apply_graph_recall(config, rows, identity_key="id")

    # The legacy answer survives, and the absent graph is *named* in the block.
    assert [row["id"] for row in outcome.results] == [ENTRY_ID]
    assert outcome.excluded == 0
    assert outcome.block is not None
    assert GRAPH_STORE_NOT_PRESENT in outcome.block["warnings"]
    assert outcome.block["authoritative"] is False
    # No database, no schema, no journal, no sibling artefact, no directory.
    assert not Path(config.graph_db_path).exists()
    assert not Path(config.graph_journal_path).exists()
    assert sorted(entry.name for entry in data.iterdir()) == []


def test_a_graph_mode_recall_leaves_an_existing_graph_database_unchanged(tmp_path):
    data = tmp_path / "recall-data"
    data.mkdir(parents=True, exist_ok=True)
    config = _recall_config(tmp_path, data)
    database = Path(config.graph_db_path)
    _populate_graph(database)
    before = _Fingerprint(database)
    rows = [
        {
            "id": ENTRY_ID,
            "domain": "retrieval",
            "text": BODY,
            "score": 0.8,
            "metadata": {"graph_node_id": "claim_a"},
        }
    ]

    outcome = graph_search.apply_graph_recall(config, rows, identity_key="id")

    # The graph answered over the row it knows, and changed nothing doing it.
    assert [row["id"] for row in outcome.results] == [ENTRY_ID]
    assert outcome.block is not None
    assert outcome.block["mapped_results"] == 1
    assert GRAPH_STORE_NOT_PRESENT not in outcome.block["warnings"]
    assert _Fingerprint(database) == before


def test_diagnose_graph_runtime_reports_an_absent_graph_and_creates_nothing(
    config, data_dir
):
    report = diagnose_graph_runtime(config, require=True)

    assert report["status"] == GRAPH_STORE_NOT_PRESENT
    assert report["fallback_reason"] == GRAPH_STORE_NOT_PRESENT
    assert report["assembly"]["path_configured"] is True
    assert report["assembly"]["store_opened"] is False
    assert report["assembly"]["healthy"] is False
    assert report["assembly"]["errors"] == [GRAPH_STORE_NOT_PRESENT]

    assert not Path(config.graph_db_path).exists()
    assert sorted(entry.name for entry in data_dir.iterdir()) == []
    payload = json.dumps(report, ensure_ascii=False)
    assert str(data_dir) not in payload


def test_diagnose_graph_runtime_leaves_an_existing_graph_database_unchanged(config):
    database = Path(config.graph_db_path)
    _populate_graph(database)
    before = _Fingerprint(database)

    report = diagnose_graph_runtime(
        config, [{"file": ENTRY_ID, "metadata": {"graph_node_id": "claim_a"}}],
        require=True,
    )

    assert report["assembly"]["status"] == GRAPH_STORE_OPENED
    assert report["assembly"]["healthy"] is True
    assert report["assembly"]["node_count"] == 1
    # The inspection handle cannot rebuild, which is how this report shows
    # *which* entrance ran.
    assert report["assembly"]["supports_rebuild"] is False
    assert report["mapping"]["mapped_results"] == 1

    for _attempt in range(3):
        diagnose_graph_runtime(config, require=True)

    assert _Fingerprint(database) == before

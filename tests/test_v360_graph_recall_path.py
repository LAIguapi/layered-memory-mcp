"""v3.6.0 Batch L-B — request-scoped persistent graph recall through the MCP seam.

Why this file exists: ARCHITECTURE_v2.md §12 Batch L-B. The Batch I assembly
(``graph_store_session``) had no caller: the server could only ever use a store
somebody injected by hand, so the persistent adapter was never reached from a
real entry point. L-B gives it exactly one caller — the existing recall seam, in
an explicit graph mode, with no injected store — and nothing else.

These tests pin:

  * **Legacy is untouched.** In ``legacy`` mode no store is opened, no graph
    operation runs, and no ``graph`` key appears — even with a database
    configured and populated.
  * **The injected store wins and is never closed.** ``set_graph_store`` stays
    the caller-owned seam: a non-None store takes precedence, the configured
    path is not opened at all, and the server never closes what it did not open.
  * **One request, one lifecycle.** Without an injected store a graph-enabled
    request opens the configured database, uses it through the existing recall
    seam, and closes it — on success *and* on error. No pool, no singleton, no
    startup hook, no second open.
  * **Every degradation stays bounded.** An unconfigured path, a missing parent
    directory, a failing open and a raising query all preserve the legacy
    answer and report a named fallback: no path, no body, no exception message.
    A missing parent directory is never created.
  * **The write path still refuses.** A semantic write in a write-enabled mode
    is still refused for want of a complete ``MemoryEvent`` stream, and a
    prepopulated graph is left exactly as it was.

Isolation: every database and knowledge directory is under pytest's
``tmp_path``, every config is explicit, the vector store is a fake and the graph
fixture is written explicitly. No embedding model is loaded, no network is used,
and nothing reads the real memory home, the real ``config.yaml``, MCP state or
any user memory data.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from layered_memory_mcp import graph_runtime, graph_runtime_assembly, server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphEdge,
    GraphNode,
    NodeKind,
    RelationType,
    make_edge_id,
)
from layered_memory_mcp.graph_recall import (
    GRAPH_STATUS_OK,
    GRAPH_STATUS_PARTIAL,
    GRAPH_STATUS_UNAVAILABLE,
)
from layered_memory_mcp.graph_runtime import GRAPH_SEED_ID_KEY
from layered_memory_mcp.graph_runtime_assembly import (
    GRAPH_STORE_NOT_CONFIGURED,
    GRAPH_STORE_NOT_PRESENT,
    ReadOnlyGraphStore,
)
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore

T0 = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)

CLAIM_ID = "claim_index_rebuild_nightly"
ENTITY_ID = "entity_retrieval_index"
ROW_ID = "mdsec1_fixture_retrieval_index_maintenance"

CLAIM_FILE = "retrieval.md"


# ---------------------------------------------------------------------------
# Explicit, neutral fixtures
# ---------------------------------------------------------------------------

def _nodes() -> list[GraphNode]:
    migrated = Derivation(method=DerivationMethod.MIGRATED)
    return [
        GraphNode(
            id=CLAIM_ID,
            kind=NodeKind.CLAIM,
            label="The retrieval index is rebuilt nightly.",
            recorded_at=T0,
            derivation=migrated,
        ),
        GraphNode(
            id=ENTITY_ID,
            kind=NodeKind.ENTITY,
            label="retrieval index",
            recorded_at=T0,
            derivation=migrated,
        ),
    ]


def _edges() -> list[GraphEdge]:
    return [
        GraphEdge(
            id=make_edge_id(RelationType.MENTIONS, CLAIM_ID, ENTITY_ID),
            relation_type=RelationType.MENTIONS,
            source_id=CLAIM_ID,
            target_id=ENTITY_ID,
            recorded_at=T0,
            derivation=Derivation(method=DerivationMethod.MIGRATED),
        )
    ]


def _populated_database(tmp_path: Path) -> Path:
    """An explicit tmp SQLite graph, written and closed before any recall."""
    parent = tmp_path / "graph-data"
    parent.mkdir(parents=True, exist_ok=True)
    database = parent / "graph.db"
    writer = SQLiteGraphStore(database, nodes=_nodes(), edges=_edges())
    writer.close()
    return database


def _config(tmp_path: Path, mode: str, database: Path | None = None) -> MemoryConfig:
    home = tmp_path / f"home-{mode}"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / CLAIM_FILE).write_text(
        "# Retrieval\n\n## Index maintenance\n\nThe retrieval index is rebuilt.\n",
        encoding="utf-8",
    )
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode=mode,
        graph_db_path=str(database) if database is not None else None,
    )


def _semantic_row(**kwargs) -> dict:
    """One row shaped exactly as ``VectorStore.search`` already returns it."""
    row = {
        "id": ROW_ID,
        "domain": "retrieval",
        "text": "The retrieval index rebuild runs nightly.",
        "metadata": {
            "type": "fact",
            "confidence": 0.9,
            "tags": [],
            "section": "Index maintenance",
            GRAPH_SEED_ID_KEY: CLAIM_ID,
        },
        "score": 0.8123,
    }
    row.update(kwargs)
    return row


class _FakeVectorStore:
    """The injected vector store — no embedding model, no database, no disk."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def search(self, query, top_n=5, domain=None):
        return deepcopy(self.rows)


class _OpenLedger:
    """Counts every SQLite graph open and close the request performs."""

    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0


@pytest.fixture
def ledger(monkeypatch) -> _OpenLedger:
    """Instrument the assembly's own backend, not a replacement for it.

    **Both** of its constructors. v3.6.1 moved the recall seam onto the
    inspection entrance, whose ``ReadOnlyGraphStore`` deliberately does not call
    ``SQLiteGraphStore.__init__`` — that constructor establishes the schema,
    which is the one thing a read must not do. Counting only the writable
    constructor would therefore have read "zero opens" for a request that opened
    a connection, and the lifecycle assertions below would have passed by
    measuring nothing.
    """
    counter = _OpenLedger()
    real_init = SQLiteGraphStore.__init__
    real_readonly_init = ReadOnlyGraphStore.__init__
    real_close = SQLiteGraphStore.close

    def counted_init(self, *args, **kwargs):
        counter.opened += 1
        return real_init(self, *args, **kwargs)

    def counted_readonly_init(self, *args, **kwargs):
        counter.opened += 1
        return real_readonly_init(self, *args, **kwargs)

    def counted_close(self):
        counter.closed += 1
        return real_close(self)

    monkeypatch.setattr(SQLiteGraphStore, "__init__", counted_init)
    monkeypatch.setattr(ReadOnlyGraphStore, "__init__", counted_readonly_init)
    monkeypatch.setattr(SQLiteGraphStore, "close", counted_close)
    return counter


def _semantic_response(monkeypatch, config, store=None, rows=None) -> dict:
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", store)
    monkeypatch.setattr(
        server,
        "_v2_stores",
        {"vector": _FakeVectorStore([_semantic_row()] if rows is None else rows)},
    )
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    return json.loads(asyncio.run(server.search_semantic(query="index rebuild")))


def _recall_response(monkeypatch, config, store=None) -> dict:
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", store)
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    return json.loads(asyncio.run(server.recall_knowledge(keyword="index")))


def _refuse_any_assembly(monkeypatch) -> None:
    """Make reaching the persistent assembly at all a loud failure."""

    def _refuse(*args, **kwargs):
        raise AssertionError("this path must not reach the persistent assembly")

    monkeypatch.setattr(graph_runtime_assembly, "graph_store_session", _refuse)
    monkeypatch.setattr(graph_runtime_assembly, "open_graph_store", _refuse)
    # Both entrances, since v3.6.1: the recall seam reaches the store through
    # the inspection one, so refusing only the writable pair would no longer
    # guard the path these tests say is never taken.
    monkeypatch.setattr(
        graph_runtime_assembly, "graph_store_readonly_session", _refuse
    )
    monkeypatch.setattr(
        graph_runtime_assembly, "open_graph_store_readonly", _refuse
    )


# ---------------------------------------------------------------------------
# Legacy: no assembly, no open, no block
# ---------------------------------------------------------------------------

def test_legacy_mode_opens_nothing_even_with_a_populated_database(
    monkeypatch, tmp_path, ledger
):
    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0  # the fixture write itself is not a recall
    config = _config(tmp_path, "legacy", database)
    _refuse_any_assembly(monkeypatch)

    response = _semantic_response(monkeypatch, config)

    assert "graph" not in response
    assert response["results"] == [_semantic_row()]
    assert (ledger.opened, ledger.closed) == (0, 0)


def test_legacy_recall_reaches_no_graph_runtime_and_no_assembly(
    monkeypatch, tmp_path, ledger
):
    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "legacy", database)
    _refuse_any_assembly(monkeypatch)

    def _refuse(*args, **kwargs):
        raise AssertionError("legacy mode must not reach the graph runtime")

    monkeypatch.setattr(graph_runtime, "run_graph_assisted_recall", _refuse)
    monkeypatch.setattr(graph_runtime, "build_seed_mapping", _refuse)

    response = _recall_response(monkeypatch, config)

    assert "graph" not in response
    assert response["success"] is True
    assert (ledger.opened, ledger.closed) == (0, 0)


# ---------------------------------------------------------------------------
# The injected store stays caller-owned
# ---------------------------------------------------------------------------

def test_an_injected_store_takes_precedence_and_is_never_closed(
    monkeypatch, tmp_path, ledger
):
    from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore

    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "graph_recall", database)
    # The configured path must not even be looked at while an override exists.
    _refuse_any_assembly(monkeypatch)

    injected = InMemoryGraphStore(nodes=_nodes(), edges=_edges())
    response = _semantic_response(monkeypatch, config, store=injected)

    assert response["graph"]["status"] in (GRAPH_STATUS_OK, GRAPH_STATUS_PARTIAL)
    assert response["graph"]["mapped_results"] == 1
    assert (ledger.opened, ledger.closed) == (0, 0)
    # Still usable afterwards: the server closed nothing it was handed.
    assert injected.get_node(CLAIM_ID) is not None


def test_clearing_the_injection_hands_the_request_back_to_the_configured_path(
    monkeypatch, tmp_path, ledger
):
    from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore

    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "graph_recall", database)

    server.set_graph_store(InMemoryGraphStore(nodes=_nodes(), edges=_edges()))
    try:
        assert server._graph_store is not None
    finally:
        server.set_graph_store(None)
    assert server._graph_store is None

    response = _semantic_response(monkeypatch, config, store=None)

    assert response["graph"]["mapped_results"] == 1
    assert (ledger.opened, ledger.closed) == (1, 1)


# ---------------------------------------------------------------------------
# Request-scoped persistent recall
# ---------------------------------------------------------------------------

def test_a_graph_mode_opens_the_configured_database_once_and_closes_it(
    monkeypatch, tmp_path, ledger
):
    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "graph_recall", database)

    response = _semantic_response(monkeypatch, config)

    block = response["graph"]
    assert block["status"] in (GRAPH_STATUS_OK, GRAPH_STATUS_PARTIAL)
    assert block["authoritative"] is True
    assert block["mapped_results"] == 1
    assert block["results"][0]["file"] == ROW_ID
    assert CLAIM_ID in block["results"][0]["graph_nodes"]
    # The legacy answer is still the legacy answer.
    assert response["results"] == [_semantic_row()]
    assert (ledger.opened, ledger.closed) == (1, 1)


def test_the_recall_entry_point_uses_the_same_request_scoped_lifecycle(
    monkeypatch, tmp_path, ledger
):
    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "graph_recall", database)

    response = _recall_response(monkeypatch, config)

    assert response["success"] is True
    assert response["results"]
    assert response["graph"]["mode"] == "graph_recall"
    assert (ledger.opened, ledger.closed) == (1, 1)


def test_two_requests_do_not_share_a_connection(monkeypatch, tmp_path, ledger):
    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "graph_recall", database)

    _semantic_response(monkeypatch, config)
    _semantic_response(monkeypatch, config)

    assert (ledger.opened, ledger.closed) == (2, 2)


def test_shadow_mode_observes_the_persistent_graph_without_claiming_authority(
    monkeypatch, tmp_path, ledger
):
    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "shadow", database)

    response = _semantic_response(monkeypatch, config)

    block = response["graph"]
    assert block["authoritative"] is False
    assert block["graph_assisted"] is True
    assert block["comparison"]["identical"] is True
    assert (ledger.opened, ledger.closed) == (1, 1)


def test_the_session_closes_when_the_graph_seam_raises(monkeypatch, tmp_path, ledger):
    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "graph_recall", database)

    def _raise(*args, **kwargs):
        raise RuntimeError("/tmp/not-a-real-path: backend diagnostic")

    monkeypatch.setattr(graph_runtime, "run_graph_assisted_recall", _raise)

    response = _semantic_response(monkeypatch, config)
    block = response["graph"]

    assert response["success"] is True
    assert response["results"] == [_semantic_row()]
    assert block["status"] == GRAPH_STATUS_UNAVAILABLE
    assert "graph_runtime_failed:RuntimeError" in block["warnings"]
    assert (ledger.opened, ledger.closed) == (1, 1)
    serialized = json.dumps(block, ensure_ascii=False)
    assert "/tmp/not-a-real-path" not in serialized
    assert "backend diagnostic" not in serialized


def test_a_query_error_preserves_the_legacy_answer_and_closes_the_store(
    monkeypatch, tmp_path, ledger
):
    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "graph_recall", database)

    def _raise_traversal(self, *args, **kwargs):
        raise RuntimeError("traversal unavailable")

    monkeypatch.setattr(SQLiteGraphStore, "traverse", _raise_traversal)

    response = _semantic_response(monkeypatch, config)

    assert response["success"] is True
    assert response["results"] == [_semantic_row()]
    assert response["graph"]["authoritative"] is False
    assert (ledger.opened, ledger.closed) == (1, 1)
    assert "traversal unavailable" not in json.dumps(response["graph"])


# ---------------------------------------------------------------------------
# Bounded fallbacks: nothing configured, nothing openable
# ---------------------------------------------------------------------------

def test_an_unconfigured_path_keeps_the_old_no_store_fallback(
    monkeypatch, tmp_path, ledger
):
    config = _config(tmp_path, "graph", database=None)

    response = _semantic_response(monkeypatch, config)
    block = response["graph"]

    assert response["results"] == [_semantic_row()]
    assert block["status"] == GRAPH_STATUS_UNAVAILABLE
    assert block["authoritative"] is False
    assert GRAPH_STORE_NOT_CONFIGURED in block["warnings"]
    assert (ledger.opened, ledger.closed) == (0, 0)


def test_a_missing_parent_directory_is_refused_and_never_created(
    monkeypatch, tmp_path, ledger
):
    """v3.6.1: the same refusal, named one step earlier.

    The recall seam now opens the store through the inspection entrance, which
    refuses an absent *file* by name before SQLite is asked for anything — so a
    missing parent directory reports ``graph_store_not_present`` rather than the
    backend's ``graph_store_open_failed:StoreUnavailableError``. What this test
    exists to pin is unchanged and now true of the absent file as well: the
    legacy answer survives, the fallback is named, and nothing is created.
    """
    absent = tmp_path / "absent-parent" / "graph.db"
    config = _config(tmp_path, "graph_recall", absent)
    ledger.opened = ledger.closed = 0

    response = _semantic_response(monkeypatch, config)
    block = response["graph"]

    assert response["success"] is True
    assert response["results"] == [_semantic_row()]
    assert block["status"] == GRAPH_STATUS_UNAVAILABLE
    assert GRAPH_STORE_NOT_PRESENT in block["warnings"]
    assert not absent.parent.exists()
    assert not absent.exists()
    assert (ledger.opened, ledger.closed) == (0, 0)


def test_a_failed_open_leaks_neither_path_nor_exception_message(
    monkeypatch, tmp_path, ledger
):
    absent = tmp_path / "absent-parent" / "graph.db"
    config = _config(tmp_path, "graph_recall", absent)

    response = _semantic_response(monkeypatch, config)
    serialized = json.dumps(response["graph"], ensure_ascii=False)

    assert "absent-parent" not in serialized
    assert str(tmp_path) not in serialized
    assert "does not exist" not in serialized
    assert "The retrieval index rebuild runs nightly." not in serialized


def test_a_corrupt_database_file_degrades_visibly_without_a_leak(
    monkeypatch, tmp_path, ledger
):
    parent = tmp_path / "graph-data"
    parent.mkdir(parents=True, exist_ok=True)
    database = parent / "graph.db"
    database.write_bytes(b"this is not a sqlite database")
    config = _config(tmp_path, "graph_recall", database)
    ledger.opened = ledger.closed = 0

    response = _semantic_response(monkeypatch, config)
    block = response["graph"]

    assert response["success"] is True
    assert response["results"] == [_semantic_row()]
    assert block["status"] == GRAPH_STATUS_UNAVAILABLE
    assert block["authoritative"] is False
    serialized = json.dumps(block, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "not a database" not in serialized
    # Opened or refused, the request still left nothing behind.
    assert ledger.opened == ledger.closed


def test_the_graph_block_carries_no_memory_body(monkeypatch, tmp_path, ledger):
    database = _populated_database(tmp_path)
    config = _config(tmp_path, "graph", database)

    response = _semantic_response(monkeypatch, config)
    serialized = json.dumps(response["graph"], ensure_ascii=False)

    assert "The retrieval index rebuild runs nightly." not in serialized
    assert "The retrieval index is rebuilt nightly." not in serialized
    assert "Index maintenance" not in serialized
    assert str(tmp_path) not in serialized


def test_the_block_stays_additive_over_the_persistent_path(monkeypatch, tmp_path):
    database = _populated_database(tmp_path)
    legacy = _semantic_response(monkeypatch, _config(tmp_path, "legacy", database))

    for mode in ("shadow", "graph_recall", "graph"):
        enabled = _semantic_response(monkeypatch, _config(tmp_path, mode, database))
        assert {k: v for k, v in enabled.items() if k != "graph"} == legacy


# ---------------------------------------------------------------------------
# The write path with no declared authority: still a refusal, still no mutation
# ---------------------------------------------------------------------------

def test_a_semantic_write_without_a_journal_is_refused_and_leaves_the_graph_intact(
    monkeypatch, tmp_path, ledger
):
    """v3.6.1 intentional contract change, same guarantee.

    The refusal reason moved from "this seam holds no complete event stream" to
    "this deployment declared no ``graph_journal_path``" — ``_config`` here
    declares only the graph database. What the test exists to pin is unchanged:
    a refused semantic write reaches no graph operation, opens nothing, and
    leaves a prepopulated graph byte-identical.
    """
    from layered_memory_mcp.graph_application import GRAPH_COMMIT_NOT_CONFIGURED
    from layered_memory_mcp.graph_write import SemanticWriteRequest

    database = _populated_database(tmp_path)
    ledger.opened = ledger.closed = 0
    config = _config(tmp_path, "graph", database)

    from layered_memory_mcp.graph_models import MemoryClass, SourceRef

    request = SemanticWriteRequest(
        node=GraphNode(
            id="claim_index_rebuild_hourly",
            kind=NodeKind.CLAIM,
            label="Retrieval index rebuild cadence",
            content="The retrieval index rebuild cadence is hourly.",
            memory_class=MemoryClass.FACT,
            recorded_at=T0,
            source_refs=[SourceRef(episode_id="episode_build_0101")],
        ),
        occurred_at=T0,
    )
    commit = server._graph_authority_write(
        config, request, "retrieval", "Index maintenance",
        "The retrieval index rebuild cadence is hourly.",
    )
    block = commit["graph"]

    assert commit["success"] is False
    assert block["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert block["committed"] is False
    # No assembly, no open, and the prepopulated graph is untouched.
    assert (ledger.opened, ledger.closed) == (0, 0)
    reader = SQLiteGraphStore(database)
    try:
        health = reader.health()
        assert health.node_count == len(_nodes())
        assert health.edge_count == len(_edges())
    finally:
        reader.close()

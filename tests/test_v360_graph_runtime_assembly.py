"""v3.6.0 Batch I-A — explicit production assembly of the persistent graph.

Why this file exists: ARCHITECTURE_v2.md §12 Batch I scope items 1 and 4 require
that a configured, *isolated* SQLite graph store can be initialized, reopened,
health-checked and reported on — while the default behaviour stays ``legacy``,
nothing is inferred from an agent's private memory path, and the diagnostics
leak neither a host path nor memory content.

These tests pin:

  * **An unset path stays unset.** With no graph database configured, the
    assembly guesses nothing — not from ``home``, not from ``knowledge_dir``,
    not from the agent memory path — and constructs no backend.
  * **Explicit, and only on request.** A store is built only when the caller
    asks for one *and* a path was configured. A configured path alone opens
    nothing, so importing or constructing a config can never touch a database.
  * **A real lifecycle.** With an existing parent directory the store
    initializes, closes, reopens and reports health; a missing parent directory
    is refused through ``SQLiteGraphStore``'s own rule and is never created.
  * **Bounded, honest diagnostics.** Counts, statuses and labels only: no host
    path, no memory body, no configuration dump, no backend exception message.
    The diagnostic reads the graph and the mapping; it rebuilds nothing and
    writes nothing.

Isolation: every path here is under pytest's ``tmp_path`` and every config is
built with an explicit home. Nothing reads the real memory home, the real
``config.yaml``, MCP state or any user memory data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    NodeKind,
)
from layered_memory_mcp.graph_runtime_assembly import (
    GRAPH_FALLBACK_NONE,
    GRAPH_SEEDS_UNMAPPED,
    GRAPH_STORE_NOT_CONFIGURED,
    GRAPH_STORE_NOT_REQUESTED,
    GRAPH_STORE_OPENED,
    GRAPH_STORE_OPEN_FAILED,
    GRAPH_STORE_UNHEALTHY,
    diagnose_graph_runtime,
    graph_assembly_payload,
    graph_store_session,
    open_graph_store,
    resolve_graph_database_path,
)
from layered_memory_mcp.graph_sqlite_store import SQLITE_GRAPH_BACKEND, SQLiteGraphStore

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path, **kwargs) -> MemoryConfig:
    home = tmp_path / "graph-home"
    home.mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), **kwargs)


def _database(tmp_path: Path) -> Path:
    """An explicit database path whose parent directory already exists."""
    parent = tmp_path / "graph-data"
    parent.mkdir(parents=True, exist_ok=True)
    return parent / "graph.db"


def _node(node_id: str = "claim_seed_0001") -> GraphNode:
    return GraphNode(
        id=node_id,
        kind=NodeKind.CLAIM,
        label="Retrieval index rebuild cadence",
        content="The retrieval index rebuild runs nightly.",
        recorded_at=T0,
        derivation=Derivation(method=DerivationMethod.USER_EXPLICIT, generated_at=T0),
    )


# ---------------------------------------------------------------------------
# An unconfigured path guesses nothing
# ---------------------------------------------------------------------------

def test_an_unset_path_resolves_to_none(tmp_path):
    assert resolve_graph_database_path(_config(tmp_path)) is None


def test_nothing_is_derived_from_home_knowledge_or_agent_memory(tmp_path):
    config = _config(tmp_path, graph_mode="graph")

    assert config.graph_db_path is None
    assert resolve_graph_database_path(config) is None
    # Not one database-shaped file appeared anywhere under the framework home.
    assert not list(Path(config.home).rglob("*.db"))


def test_requiring_a_store_without_a_path_is_a_named_refusal(tmp_path):
    assembly = open_graph_store(_config(tmp_path, graph_mode="graph"), require=True)

    assert assembly.store is None
    assert assembly.available is False
    assert assembly.report.status == GRAPH_STORE_NOT_CONFIGURED
    assert assembly.report.path_configured is False
    assert "graph_store_not_configured:no_path" in assembly.report.warnings


def test_a_configured_path_alone_opens_nothing(tmp_path):
    database = _database(tmp_path)
    config = _config(tmp_path, graph_mode="graph", graph_db_path=str(database))

    assembly = open_graph_store(config)

    assert assembly.store is None
    assert assembly.report.status == GRAPH_STORE_NOT_REQUESTED
    assert assembly.report.path_configured is True
    assert not database.exists()


def test_the_default_legacy_config_assembles_no_store(tmp_path):
    config = _config(tmp_path)

    with graph_store_session(config) as assembly:
        assert assembly.store is None
        assert assembly.report.mode == "legacy"
        assert assembly.report.status == GRAPH_STORE_NOT_REQUESTED


# ---------------------------------------------------------------------------
# The explicit lifecycle
# ---------------------------------------------------------------------------

def test_an_explicit_path_initializes_and_reports_health(tmp_path):
    database = _database(tmp_path)
    config = _config(tmp_path, graph_mode="graph", graph_db_path=str(database))

    with graph_store_session(config, require=True) as assembly:
        assert isinstance(assembly.store, SQLiteGraphStore)
        report = assembly.report
        assert report.status == GRAPH_STORE_OPENED
        assert report.backend == SQLITE_GRAPH_BACKEND
        assert report.healthy is True
        assert report.node_count == 0
        assert report.edge_count == 0
        assert report.supports_rebuild is True
        assert report.errors == []
    assert database.exists()


def test_the_session_closes_the_store_it_opened(tmp_path):
    config = _config(
        tmp_path, graph_mode="graph", graph_db_path=str(_database(tmp_path))
    )

    with graph_store_session(config, require=True) as assembly:
        store = assembly.store
        assert store.closed is False
    assert store.closed is True


def test_a_reopened_store_still_holds_what_was_written(tmp_path):
    config = _config(
        tmp_path, graph_mode="graph", graph_db_path=str(_database(tmp_path))
    )

    with graph_store_session(config, require=True) as first:
        first.store.create_node(_node())

    with graph_store_session(config, require=True) as second:
        assert second.report.node_count == 1
        assert second.report.healthy is True
        assert second.store.get_node("claim_seed_0001") is not None


def test_a_missing_parent_directory_is_refused_and_never_created(tmp_path):
    parent = tmp_path / "uncreated-parent"
    config = _config(
        tmp_path, graph_mode="graph", graph_db_path=str(parent / "graph.db")
    )

    assembly = open_graph_store(config, require=True)

    assert assembly.store is None
    assert assembly.report.status == GRAPH_STORE_OPEN_FAILED
    assert assembly.report.errors == ["graph_store_open_failed:StoreUnavailableError"]
    assert not parent.exists()


def test_the_assembly_never_rebuilds_the_graph(tmp_path, monkeypatch):
    config = _config(
        tmp_path, graph_mode="graph", graph_db_path=str(_database(tmp_path))
    )

    def _refuse(*args, **kwargs):
        raise AssertionError("assembly must never rebuild a graph")

    monkeypatch.setattr(SQLiteGraphStore, "rebuild", _refuse)
    with graph_store_session(config, require=True) as assembly:
        # The capability is *reported*, never exercised.
        assert assembly.report.supports_rebuild is True


def test_opening_under_the_legacy_mode_is_allowed_but_flagged(tmp_path):
    config = _config(tmp_path, graph_db_path=str(_database(tmp_path)))

    with graph_store_session(config, require=True) as assembly:
        assert assembly.report.mode == "legacy"
        assert assembly.report.status == GRAPH_STORE_OPENED
        assert "graph_mode_legacy" in assembly.report.warnings


def test_an_unhealthy_store_is_reported_rather_than_raised(tmp_path):
    database = _database(tmp_path)
    config = _config(tmp_path, graph_mode="graph", graph_db_path=str(database))

    # A dangling edge is the reference store's own health error; writing it
    # through a rebuild is the one supported way to produce one.
    with SQLiteGraphStore(database) as store:
        store.create_node(_node())
    import sqlite3

    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE graph_nodes SET payload = ? WHERE id = ?",
            ("{not json", "claim_seed_0001"),
        )

    with graph_store_session(config, require=True) as assembly:
        assert assembly.report.status == GRAPH_STORE_OPENED
        assert assembly.report.healthy is False
        assert assembly.report.errors == ["corrupt_payload:claim_seed_0001"]


# ---------------------------------------------------------------------------
# Diagnostics (§12 Batch I scope item 4)
# ---------------------------------------------------------------------------

_RESULTS = [
    {"file": "retrieval/one", "metadata": {"graph_node_id": "claim_seed_0001"}},
    {"file": "retrieval/two", "metadata": {"section": "no graph identity yet"}},
    {"file": "retrieval/three", "metadata": {"graph_node_id": ""}},
]


def test_diagnostics_report_mapping_coverage_as_counts_only(tmp_path):
    report = diagnose_graph_runtime(_config(tmp_path), _RESULTS)

    assert report["mapping"] == {
        "results": 3,
        "mapped_results": 1,
        "unmapped_results": 2,
        "rejected_ids": 1,
        "warnings": [
            "graph_ids_rejected:1",
            "graph_metadata_without_ids:1",
            "unmapped_results:2",
        ],
    }


def test_diagnostics_name_the_fallback_reason(tmp_path):
    database = _database(tmp_path)

    unconfigured = diagnose_graph_runtime(_config(tmp_path), _RESULTS, require=True)
    assert unconfigured["fallback_reason"] == GRAPH_STORE_NOT_CONFIGURED

    configured = _config(
        tmp_path, graph_mode="graph", graph_db_path=str(database)
    )
    assert (
        diagnose_graph_runtime(configured, _RESULTS)["fallback_reason"]
        == GRAPH_STORE_NOT_REQUESTED
    )

    healthy = diagnose_graph_runtime(configured, _RESULTS, require=True)
    assert healthy["fallback_reason"] == GRAPH_FALLBACK_NONE
    assert healthy["assembly"]["status"] == GRAPH_STORE_OPENED


def test_a_healthy_graph_with_no_mapped_seed_is_its_own_fallback_reason(tmp_path):
    config = _config(
        tmp_path, graph_mode="graph", graph_db_path=str(_database(tmp_path))
    )

    report = diagnose_graph_runtime(config, _RESULTS[1:], require=True)

    assert report["assembly"]["healthy"] is True
    assert report["fallback_reason"] == GRAPH_SEEDS_UNMAPPED


def test_an_unhealthy_graph_is_its_own_fallback_reason(tmp_path):
    database = _database(tmp_path)
    config = _config(tmp_path, graph_mode="graph", graph_db_path=str(database))
    with SQLiteGraphStore(database) as store:
        store.create_node(_node())
    import sqlite3

    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE graph_nodes SET payload = ? WHERE id = ?",
            ("{not json", "claim_seed_0001"),
        )

    report = diagnose_graph_runtime(config, _RESULTS, require=True)
    assert report["fallback_reason"] == GRAPH_STORE_UNHEALTHY


def test_diagnostics_open_nothing_unless_asked(tmp_path):
    database = _database(tmp_path)
    config = _config(tmp_path, graph_mode="graph", graph_db_path=str(database))

    report = diagnose_graph_runtime(config, _RESULTS)

    assert report["assembly"]["status"] == GRAPH_STORE_NOT_REQUESTED
    assert report["assembly"]["path_configured"] is True
    assert not database.exists()


def test_diagnostics_change_no_graph_state(tmp_path):
    database = _database(tmp_path)
    config = _config(tmp_path, graph_mode="graph", graph_db_path=str(database))
    with SQLiteGraphStore(database) as store:
        store.create_node(_node())
        before = store.snapshot()

    diagnose_graph_runtime(config, _RESULTS, require=True)

    with SQLiteGraphStore(database) as store:
        assert store.snapshot() == before


def test_the_report_leaks_no_path_body_or_exception_message(tmp_path):
    parent = tmp_path / "uncreated-parent"
    config = _config(
        tmp_path, graph_mode="graph", graph_db_path=str(parent / "graph.db")
    )

    serialized = json.dumps(
        diagnose_graph_runtime(config, _RESULTS, require=True), ensure_ascii=False
    )

    assert str(tmp_path) not in serialized
    assert "graph.db" not in serialized
    assert "uncreated-parent" not in serialized
    # The backend's own refusal message, verbatim, must not ride along.
    assert "does not exist" not in serialized
    assert "never creates directories" not in serialized


def test_the_assembly_payload_is_a_fixed_bounded_shape(tmp_path):
    config = _config(
        tmp_path, graph_mode="graph", graph_db_path=str(_database(tmp_path))
    )

    with graph_store_session(config, require=True) as assembly:
        payload = graph_assembly_payload(assembly.report)

    assert set(payload) == {
        "mode",
        "backend",
        "status",
        "path_configured",
        "store_opened",
        "healthy",
        "node_count",
        "edge_count",
        "graph_schema_version",
        "store_schema_version",
        "supports_rebuild",
        "errors",
        "warnings",
    }
    assert json.dumps(payload)  # plain JSON types only


def test_diagnostics_run_without_any_results(tmp_path):
    report = diagnose_graph_runtime(_config(tmp_path))

    assert report["mapping"]["results"] == 0
    assert report["mapping"]["mapped_results"] == 0


def test_a_duck_typed_config_without_a_graph_path_is_not_guessed_at():
    """A test double, a per-request override — anything with the attributes."""

    class _Bare:
        graph_mode = "graph"
        graph_recall_enabled = True

    assembly = open_graph_store(_Bare(), require=True)
    assert assembly.store is None
    assert assembly.report.status == GRAPH_STORE_NOT_CONFIGURED


def test_a_non_path_configuration_value_is_refused_visibly():
    class _Bogus:
        graph_mode = "graph"
        graph_db_path = 7

    assembly = open_graph_store(_Bogus(), require=True)
    assert assembly.store is None
    assert assembly.report.status == GRAPH_STORE_NOT_CONFIGURED
    assert "graph_db_path_invalid" in assembly.report.warnings

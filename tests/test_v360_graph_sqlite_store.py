"""v3.6.0 Batch E — the embedded, persistent SQLite memory-graph store.

Why this file exists: ARCHITECTURE_v2.md §9.2 step 2 and §12 Batch E require a
real, *persistent*, backend-neutral implementation of the §9.1 ``GraphStore``
contract. Batch C proved the contract implementable in process memory; this
batch proves the same semantics survive a round trip through an embedded
database — and, just as importantly, that they are the *same* semantics: a
second, hand-rolled traversal/conflict/provenance rule set would drift from the
reference store the moment either side changed.

These tests pin what makes an embedded backend trustworthy:

  * **It is the contract.** All nine §9.1 operations, with the documented
    semantics, over a database rather than a dict.
  * **It persists.** Close the store, reopen the same file, and the graph is
    still there, byte-identical.
  * **It agrees with the reference.** On one fixed fixture graph, traversal,
    conflicts, provenance and snapshots match
    :class:`~layered_memory_mcp.graph_fixture_store.InMemoryGraphStore` exactly.
  * **It is deterministic.** Ordering never depends on what SQLite felt like
    returning, and a rebuild of one event stream is byte-stable across runs.
  * **It is honest.** A rebuild that fails leaves the old graph intact; drift
    between the projection and an expected source is reported as drift, not as
    health; a closed or corrupted store says so instead of answering blankly.

Isolation: every test uses ``":memory:"`` or pytest's ``tmp_path``. Nothing here
reads or writes a real memory path, a config file or MCP state, and no test
depends on a graph database package being installed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
from layered_memory_mcp.graph_models import (
    GRAPH_SCHEMA_VERSION,
    Derivation,
    DerivationMethod,
    EdgeStatus,
    GraphEdge,
    GraphNode,
    GraphScope,
    MemoryClass,
    MemoryEvent,
    MemoryEventKind,
    NodeKind,
    NodeStatus,
    RelationType,
    SourceRef,
    make_edge_id,
)
from layered_memory_mcp.graph_sqlite_store import (
    META_BACKEND,
    META_GRAPH_SCHEMA_VERSION,
    META_STORE_SCHEMA_VERSION,
    SQLITE_GRAPH_BACKEND,
    STORE_SCHEMA_VERSION,
    CorruptPayloadError,
    EdgeConflictError,
    GraphStoreClosedError,
    InvalidPayloadError,
    MissingEndpointError,
    SQLiteGraphStore,
    StaleRebuildError,
    StoreSchemaError,
    StoreUnavailableError,
    UnknownNodeError,
)
from layered_memory_mcp.graph_store import (
    GraphStore,
    TraversalFilters,
    missing_graph_store_operations,
)

# --- neutral fixture vocabulary ---------------------------------------------
# Deliberately the same vocabulary Batch C's fixture store tests use — a
# retrieval index and its rebuild cadence — so the parity assertions below
# compare the two backends on one identical graph.

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)

EPISODE_ID = "episode_build_0007"
EVIDENCE_ID = "evidence_build_0007_0002"
ENTITY_ID = "entity_retrieval_index"
NEW_CLAIM_ID = "claim_index_rebuild_nightly"
OLD_CLAIM_ID = "claim_index_rebuild_weekly"
POLICY_ID = "policy_index_freshness"
EXPERIENCE_ID = "experience_index_staleness"


def _source_ref(**kwargs) -> SourceRef:
    data = {
        "episode_id": EPISODE_ID,
        "evidence_id": EVIDENCE_ID,
        "source_file": "knowledge/retrieval.md",
        "source_section": "Index maintenance",
    }
    data.update(kwargs)
    return SourceRef(**data)


def _derivation(**kwargs) -> Derivation:
    data = {"method": DerivationMethod.USER_EXPLICIT, "generated_at": T0}
    data.update(kwargs)
    return Derivation(**data)


def _claim(**kwargs) -> GraphNode:
    data = {
        "id": NEW_CLAIM_ID,
        "kind": NodeKind.CLAIM,
        "label": "Retrieval index rebuild cadence",
        "content": "The retrieval index rebuild runs nightly.",
        "memory_class": MemoryClass.FACT,
        "confidence": 0.8,
        "recorded_at": T0,
        "source_refs": [_source_ref()],
    }
    data.update(kwargs)
    return GraphNode(**data)


def _old_claim(**kwargs) -> GraphNode:
    data = {
        "id": OLD_CLAIM_ID,
        "kind": NodeKind.CLAIM,
        "label": "Retrieval index rebuild cadence",
        "content": "The retrieval index rebuild runs weekly.",
        "memory_class": MemoryClass.FACT,
        "recorded_at": T0,
        "source_refs": [_source_ref()],
    }
    data.update(kwargs)
    return GraphNode(**data)


def _episode() -> GraphNode:
    return GraphNode(
        id=EPISODE_ID, kind=NodeKind.EPISODE, label="build session 0007", recorded_at=T0
    )


def _evidence() -> GraphNode:
    return GraphNode(
        id=EVIDENCE_ID,
        kind=NodeKind.EVIDENCE,
        label="retrieval.md / Index maintenance",
        content="The index rebuild job is scheduled nightly at 02:00.",
        recorded_at=T0,
    )


def _entity() -> GraphNode:
    return GraphNode(
        id=ENTITY_ID, kind=NodeKind.ENTITY, label="retrieval index", recorded_at=T0
    )


def _policy() -> GraphNode:
    return GraphNode(
        id=POLICY_ID,
        kind=NodeKind.POLICY,
        label="Index freshness policy",
        content="A retrieval answer must not be served from a stale index.",
        memory_class=MemoryClass.POLICY,
        recorded_at=T0,
        derivation=_derivation(),
    )


def _experience() -> GraphNode:
    return GraphNode(
        id=EXPERIENCE_ID,
        kind=NodeKind.EXPERIENCE,
        label="Stale index hides recent writes",
        memory_class=MemoryClass.EXPERIENCE,
        recorded_at=T0,
        derivation=_derivation(method=DerivationMethod.SUMMARIZED),
    )


def _typed_edge(
    relation: RelationType, source: str, target: str, **kwargs
) -> GraphEdge:
    """An edge whose ID is derived from its triple, as a rebuild would build it."""
    data = {
        "id": make_edge_id(relation, source, target),
        "relation_type": relation,
        "source_id": source,
        "target_id": target,
        "recorded_at": T0,
    }
    if relation in (
        RelationType.SUPPORTS,
        RelationType.CONTRADICTS,
        RelationType.SUPERSEDES,
    ):
        data["evidence_refs"] = [_source_ref()]
    data.update(kwargs)
    return GraphEdge(**data)


def _graph_nodes() -> list[GraphNode]:
    """The one fixture graph both backends are compared on."""
    return [
        _claim(),
        _old_claim(status=NodeStatus.SUPERSEDED),
        _evidence(),
        _episode(),
        _entity(),
        _policy(),
        _experience(),
    ]


def _graph_edges() -> list[GraphEdge]:
    return [
        _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID),
        _typed_edge(RelationType.OBSERVED_IN, NEW_CLAIM_ID, EPISODE_ID),
        _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID),
        _typed_edge(RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID),
        _typed_edge(RelationType.ABOUT, OLD_CLAIM_ID, ENTITY_ID),
        _typed_edge(RelationType.MENTIONS, EPISODE_ID, ENTITY_ID),
        _typed_edge(RelationType.APPLIES_TO, POLICY_ID, ENTITY_ID),
        _typed_edge(RelationType.RELATED_TO, NEW_CLAIM_ID, POLICY_ID),
        _typed_edge(RelationType.DERIVED_FROM, EXPERIENCE_ID, NEW_CLAIM_ID),
        _typed_edge(RelationType.EVIDENCED_BY, EXPERIENCE_ID, EVIDENCE_ID),
    ]


def _sqlite_graph(database=":memory:") -> SQLiteGraphStore:
    return SQLiteGraphStore(database, nodes=_graph_nodes(), edges=_graph_edges())


def _reference_graph() -> InMemoryGraphStore:
    return InMemoryGraphStore(nodes=_graph_nodes(), edges=_graph_edges())


# --- A. the contract, the schema and the lifecycle ---------------------------

def test_store_is_a_complete_graph_store():
    with SQLiteGraphStore(":memory:") as store:
        assert missing_graph_store_operations(store) == []
        assert isinstance(store, GraphStore)


def test_empty_store_is_healthy_and_empty():
    with SQLiteGraphStore(":memory:") as store:
        health = store.health()
        assert health.backend == SQLITE_GRAPH_BACKEND
        assert health.healthy is True
        assert (health.node_count, health.edge_count) == (0, 0)
        assert health.schema_version == GRAPH_SCHEMA_VERSION


def test_schema_initialization_records_both_versions_and_is_idempotent(tmp_path):
    """Reopening an existing database must not rewrite or re-stamp it."""
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database) as store:
        store.create_node(_claim())
        assert store.schema_info() == {
            META_BACKEND: SQLITE_GRAPH_BACKEND,
            META_STORE_SCHEMA_VERSION: STORE_SCHEMA_VERSION,
            META_GRAPH_SCHEMA_VERSION: GRAPH_SCHEMA_VERSION,
        }

    with SQLiteGraphStore(database) as reopened:
        assert reopened.schema_info()[META_STORE_SCHEMA_VERSION] == (
            STORE_SCHEMA_VERSION
        )
        assert reopened.health().node_count == 1
        assert reopened.get_node(NEW_CLAIM_ID) == _claim()


def test_a_database_written_by_another_store_layout_is_refused(tmp_path):
    """§9.3: reading an unknown layout with today's code is silent data loss."""
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database):
        pass
    with sqlite3.connect(database) as raw:
        raw.execute(
            "UPDATE graph_meta SET value = ? WHERE key = ?",
            (str(STORE_SCHEMA_VERSION + 1), META_STORE_SCHEMA_VERSION),
        )

    with pytest.raises(StoreSchemaError) as excinfo:
        SQLiteGraphStore(database)
    assert str(STORE_SCHEMA_VERSION + 1) in str(excinfo.value)


def test_opening_never_creates_a_directory(tmp_path):
    """A graph backend that mkdir's its way to a path can land in a real home."""
    missing = tmp_path / "not-created" / "graph.sqlite3"
    with pytest.raises(StoreUnavailableError) as excinfo:
        SQLiteGraphStore(missing)
    assert "not-created" in str(excinfo.value)
    assert not missing.parent.exists()


def test_the_graph_survives_closing_and_reopening_the_file(tmp_path):
    """The whole point of an embedded backend: it outlives the process."""
    database = tmp_path / "graph.sqlite3"
    with _sqlite_graph(database) as store:
        before = store.snapshot()
    with SQLiteGraphStore(database) as reopened:
        assert reopened.snapshot() == before
        assert reopened.health().healthy is True


def test_close_is_idempotent_and_every_operation_then_refuses():
    store = _sqlite_graph()
    store.close()
    store.close()
    assert store.closed is True

    for operation in (
        lambda: store.get_node(NEW_CLAIM_ID),
        lambda: store.get_edges(NEW_CLAIM_ID),
        lambda: store.traverse([NEW_CLAIM_ID]),
        lambda: store.find_conflicts(),
        lambda: store.get_provenance(NEW_CLAIM_ID),
        lambda: store.create_node(_claim()),
        lambda: store.append_edge(_graph_edges()[0]),
        lambda: store.rebuild([]),
        lambda: store.snapshot(),
        lambda: store.schema_info(),
    ):
        with pytest.raises(GraphStoreClosedError):
            operation()


def test_health_stays_answerable_and_honest_after_close():
    """§10.3: health is what a caller asks when something else already failed,
    so it must not raise — and 0/0 would read as "an empty graph"."""
    store = _sqlite_graph()
    store.close()
    health = store.health()
    assert health.healthy is False
    assert health.errors == ["store_closed"]
    assert health.backend == SQLITE_GRAPH_BACKEND


def test_a_borrowed_connection_is_left_open_with_its_own_isolation_level():
    connection = sqlite3.connect(":memory:")
    original = connection.isolation_level
    store = SQLiteGraphStore(connection, nodes=[_claim()])
    store.close()

    assert connection.isolation_level == original
    # Still usable by its owner, which is the point of not closing it.
    count = connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    assert count == 1
    connection.close()


def test_context_manager_closes_the_store_even_when_the_body_raises():
    store = SQLiteGraphStore(":memory:")
    with pytest.raises(ZeroDivisionError):
        with store:
            1 / 0
    assert store.closed is True


# --- B. node and edge round-trip --------------------------------------------

def test_create_node_stores_and_returns_the_node():
    with SQLiteGraphStore(":memory:") as store:
        claim = _claim()
        assert store.create_node(claim) == claim
        assert store.get_node(NEW_CLAIM_ID) == claim
        assert store.health().node_count == 1


def test_get_node_returns_none_for_an_unknown_id():
    with SQLiteGraphStore(":memory:", nodes=[_claim()]) as store:
        assert store.get_node("claim_not_stored") is None


def test_repeating_an_identical_create_is_idempotent():
    """A rerun of an import must not duplicate a row (§9.1 upsert)."""
    with SQLiteGraphStore(":memory:") as store:
        store.create_node(_claim())
        before = store.snapshot()
        store.create_node(_claim())
        assert store.snapshot() == before
        assert store.health().node_count == 1


def test_create_node_with_conflicting_content_replaces_by_id():
    with SQLiteGraphStore(":memory:", nodes=[_claim()]) as store:
        revised = _claim(content="The retrieval index rebuild runs hourly.", version=2)
        store.create_node(revised)
        assert store.get_node(NEW_CLAIM_ID) == revised
        assert store.health().node_count == 1


def test_create_node_keeps_the_payloads_own_schema_version(tmp_path):
    """An upsert may not quietly restamp an old payload with today's version:
    the mismatch is exactly what health has to keep reporting."""
    database = tmp_path / "graph.sqlite3"
    future = _claim(schema_version=GRAPH_SCHEMA_VERSION + 1)
    with SQLiteGraphStore(database, nodes=[future]) as store:
        assert store.get_node(NEW_CLAIM_ID).schema_version == (
            GRAPH_SCHEMA_VERSION + 1
        )
        health = store.health()
        assert health.healthy is False
        assert (
            f"schema_version_mismatch:{NEW_CLAIM_ID}:{GRAPH_SCHEMA_VERSION + 1}"
            in health.errors
        )


def test_create_node_refuses_anything_that_is_not_a_graph_node():
    """§2: an illegal payload is refused, not laundered into the database."""
    with SQLiteGraphStore(":memory:") as store:
        for illegal in ({"id": "claim_x", "kind": "claim"}, "claim_x", None, 7):
            with pytest.raises(TypeError):
                store.create_node(illegal)
        assert store.health().node_count == 0


def test_all_node_fields_survive_the_round_trip_byte_for_byte():
    rich = _claim(
        scope=GraphScope(user_id="u1", agent_id="a1", namespace="retrieval"),
        status=NodeStatus.PENDING,
        valid_from=T0,
        valid_to=T2,
        derivation=_derivation(method=DerivationMethod.MIGRATED, model="m-1"),
        conflict_group="conflict_index_cadence",
        tags=["Retrieval", "index"],
        attributes={"cron": "0 2 * * *", "retries": 3, "nested": {"a": [1, 2]}},
        version=4,
    )
    with SQLiteGraphStore(":memory:", nodes=[rich]) as store:
        read_back = store.get_node(NEW_CLAIM_ID)
        assert read_back.to_json() == rich.to_json()
        assert read_back.scope.namespace == "retrieval"
        assert read_back.derivation.method is DerivationMethod.MIGRATED
        assert read_back.attributes["nested"] == {"a": [1, 2]}
        assert read_back.tags == ["retrieval", "index"]


def test_a_stored_payload_is_revalidated_and_never_returned_as_raw_json(tmp_path):
    """§2: arbitrary JSON must not come back dressed as a trusted GraphNode."""
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database, nodes=[_claim()]):
        pass
    with sqlite3.connect(database) as raw:
        raw.execute(
            "UPDATE graph_nodes SET payload = ? WHERE id = ?",
            (json.dumps({"id": NEW_CLAIM_ID, "kind": "not_a_kind"}), NEW_CLAIM_ID),
        )

    with SQLiteGraphStore(database) as store:
        with pytest.raises(CorruptPayloadError) as excinfo:
            store.get_node(NEW_CLAIM_ID)
        assert NEW_CLAIM_ID in str(excinfo.value)
        # health reports it instead of raising, and keeps the count honest.
        health = store.health()
        assert health.errors == [f"corrupt_payload:{NEW_CLAIM_ID}"]
        assert health.node_count == 1


def test_append_edge_stores_and_returns_the_edge():
    edge = _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID)
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _evidence()]) as store:
        assert store.append_edge(edge) == edge
        assert store.get_edges(NEW_CLAIM_ID, direction="outgoing") == [edge]
        assert store.health().edge_count == 1


def test_repeating_an_identical_append_produces_no_second_row():
    edge = _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID)
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _evidence()]) as store:
        store.append_edge(edge)
        before = store.snapshot()
        store.append_edge(edge)
        assert store.snapshot() == before
        assert store.health().edge_count == 1


def test_reasserting_one_edge_id_with_a_different_relation_is_refused():
    """§6.2, via the reference store's own withdrawal rule: an edge is never
    edited in place, so a silent overwrite would erase an asserted relation."""
    edge = _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID)
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _evidence()]) as store:
        store.append_edge(edge)
        with pytest.raises(EdgeConflictError):
            store.append_edge(edge.model_copy(update={"confidence": 0.99}))
        assert store.get_edges(NEW_CLAIM_ID)[0].confidence == 0.5
        assert store.health().edge_count == 1


def test_an_active_edge_may_be_withdrawn_in_place_but_not_restored():
    edge = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID)
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _entity()]) as store:
        store.append_edge(edge)
        withdrawn = store.append_edge(edge.model_copy(update={"status": "retracted"}))
        assert withdrawn.status is EdgeStatus.RETRACTED
        assert store.get_edges(NEW_CLAIM_ID)[0].status is EdgeStatus.RETRACTED
        assert store.health().edge_count == 1

        with pytest.raises(EdgeConflictError):
            store.append_edge(edge)
        assert store.get_edges(NEW_CLAIM_ID)[0].status is EdgeStatus.RETRACTED


def test_an_edge_whose_endpoint_is_not_stored_is_refused():
    """A deliberate single assertion knows both ends; a dangling one is a bug."""
    with SQLiteGraphStore(":memory:", nodes=[_claim()]) as store:
        with pytest.raises(MissingEndpointError) as excinfo:
            store.append_edge(
                _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, "entity_not_stored")
            )
        assert "entity_not_stored" in str(excinfo.value)
        assert store.health().edge_count == 0
        assert store.health().healthy is True


def test_append_edge_refuses_anything_that_is_not_a_graph_edge():
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _evidence()]) as store:
        with pytest.raises(TypeError):
            store.append_edge({"id": "edge_x"})
        assert store.health().edge_count == 0


def test_a_failed_write_leaves_no_half_row_behind():
    """Explicit transaction boundaries: a refusal rolls back, it does not
    leave an edge the caller was told was rejected."""
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _entity()]) as store:
        edge = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID)
        store.append_edge(edge)
        snapshot = store.snapshot()
        with pytest.raises(EdgeConflictError):
            store.append_edge(edge.model_copy(update={"confidence": 0.1}))
        with pytest.raises(MissingEndpointError):
            store.append_edge(
                _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, "entity_absent")
            )
        assert store.snapshot() == snapshot


def test_get_edges_separates_direction_in_a_stable_order():
    with _sqlite_graph() as store:
        outgoing = store.get_edges(NEW_CLAIM_ID, direction="outgoing")
        incoming = store.get_edges(NEW_CLAIM_ID, direction="incoming")
        both = store.get_edges(NEW_CLAIM_ID, direction="both")

    assert [e.relation_type.value for e in outgoing] == [
        "ABOUT",
        "EVIDENCED_BY",
        "OBSERVED_IN",
        "RELATED_TO",
        "SUPERSEDES",
    ]
    assert [e.relation_type.value for e in incoming] == ["DERIVED_FROM"]
    assert len(both) == len(outgoing) + len(incoming)
    assert both == sorted(
        both, key=lambda e: (e.relation_type.value, e.source_id, e.target_id, e.id)
    )


def test_get_edges_order_does_not_depend_on_insertion_or_sqlite(tmp_path):
    forward = _sqlite_graph(tmp_path / "forward.sqlite3")
    backward = SQLiteGraphStore(
        tmp_path / "backward.sqlite3",
        nodes=list(reversed(_graph_nodes())),
        edges=list(reversed(_graph_edges())),
    )
    with forward, backward:
        assert [e.id for e in backward.get_edges(NEW_CLAIM_ID)] == [
            e.id for e in forward.get_edges(NEW_CLAIM_ID)
        ]
        # And the same order the reference store documents.
        assert [e.id for e in forward.get_edges(NEW_CLAIM_ID)] == [
            e.id for e in _reference_graph().get_edges(NEW_CLAIM_ID)
        ]


def test_get_edges_narrows_by_relation_type():
    with _sqlite_graph() as store:
        found = store.get_edges(
            NEW_CLAIM_ID,
            relation_types=[RelationType.EVIDENCED_BY, RelationType.SUPERSEDES],
        )
        assert [e.relation_type.value for e in found] == [
            "EVIDENCED_BY",
            "SUPERSEDES",
        ]
        assert store.get_edges(NEW_CLAIM_ID, relation_types=[RelationType.BLOCKS]) == []
        # An empty sequence means "no narrowing", exactly like None.
        assert store.get_edges(NEW_CLAIM_ID, relation_types=[]) == store.get_edges(
            NEW_CLAIM_ID
        )


def test_get_edges_includes_withdrawn_edges_and_unknown_nodes_are_empty():
    with _sqlite_graph() as store:
        about = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID)
        store.append_edge(about.model_copy(update={"status": EdgeStatus.RETRACTED}))
        assert "retracted" in [e.status.value for e in store.get_edges(NEW_CLAIM_ID)]
        assert store.get_edges("claim_not_stored") == []


def test_get_edges_rejects_an_unknown_direction():
    with _sqlite_graph() as store:
        with pytest.raises(ValueError):
            store.get_edges(NEW_CLAIM_ID, direction="sideways")


def test_sql_shaped_identifiers_are_stored_as_plain_values(tmp_path):
    """A SQL fragment in an ID or an attribute is data, never a statement."""
    database = tmp_path / "graph.sqlite3"
    hostile_id = "entity_'); DROP TABLE graph_nodes; --"
    hostile = GraphNode(
        id=hostile_id,
        kind=NodeKind.ENTITY,
        label="1 OR 1=1",
        attributes={"note": "'; DELETE FROM graph_edges WHERE 1=1; --"},
        recorded_at=T0,
    )
    edge = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, hostile_id)
    with SQLiteGraphStore(database, nodes=[_claim(), hostile], edges=[edge]) as store:
        assert store.get_node(hostile_id) == hostile
        assert store.get_edges(hostile_id, direction="incoming") == [edge]
        assert (store.health().node_count, store.health().edge_count) == (2, 1)
        assert store.health().healthy is True

    # The tables are still there, which is what a successful injection would
    # have changed.
    with sqlite3.connect(database) as raw:
        tables = {
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"graph_nodes", "graph_edges", "graph_meta"} <= tables


def test_an_unvalidated_model_copy_is_normalized_not_stored_raw():
    """`model_copy(update=...)` skips validation, so a caller can hold an edge
    whose status is the *string* "retracted". The write boundary revalidates:
    what is merely unnormalized is normalized, so the row the schema describes
    and the payload the next read parses can never disagree."""
    edge = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID)
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _entity()]) as store:
        unvalidated = edge.model_copy(update={"status": "expired"})
        assert unvalidated.status == "expired"  # not an EdgeStatus yet
        assert store.append_edge(unvalidated).status is EdgeStatus.EXPIRED
        assert store.get_edges(NEW_CLAIM_ID)[0].status is EdgeStatus.EXPIRED


def test_an_illegal_unvalidated_payload_is_refused_rather_than_stored():
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _entity()]) as store:
        with pytest.raises(InvalidPayloadError):
            store.create_node(_claim().model_copy(update={"confidence": 7.5}))
        with pytest.raises(InvalidPayloadError):
            store.append_edge(
                _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID).model_copy(
                    update={"valid_from": T2, "valid_to": T0}
                )
            )
        assert store.get_node(NEW_CLAIM_ID).confidence == 0.8
        assert store.health().edge_count == 0


# --- C. traversal, provenance and conflicts: parity with the reference -------

def test_the_two_backends_hold_the_same_graph():
    with _sqlite_graph() as store:
        assert store.snapshot() == _reference_graph().snapshot()


def test_traverse_expands_one_hop_in_both_directions_by_default():
    with _sqlite_graph() as store:
        result = store.traverse([NEW_CLAIM_ID])
    assert result.seed_ids == [NEW_CLAIM_ID]
    assert [n.id for n in result.nodes] == [
        NEW_CLAIM_ID,       # hop 0
        ENTITY_ID,          # hop 1, outgoing ABOUT
        EPISODE_ID,         # hop 1, outgoing OBSERVED_IN
        EVIDENCE_ID,        # hop 1, outgoing EVIDENCED_BY
        EXPERIENCE_ID,      # hop 1, incoming DERIVED_FROM
    ]
    assert result.truncated is False
    assert result.to_json() == _reference_graph().traverse([NEW_CLAIM_ID]).to_json()


def test_traverse_defaults_leave_out_historical_state_and_weak_relations():
    """§8.2 and §6.2: a superseded claim is history, and RELATED_TO alone
    cannot justify pulling a node into context."""
    with _sqlite_graph() as store:
        result = store.traverse([NEW_CLAIM_ID])
    assert OLD_CLAIM_ID not in {n.id for n in result.nodes}
    assert POLICY_ID not in {n.id for n in result.nodes}
    assert result.warnings == ["weak_relation_excluded:RELATED_TO"]


def test_traverse_with_zero_hops_returns_only_the_seeds():
    with _sqlite_graph() as store:
        result = store.traverse([NEW_CLAIM_ID, EVIDENCE_ID], max_hops=0)
    assert [n.id for n in result.nodes] == [NEW_CLAIM_ID, EVIDENCE_ID]
    assert [p.node_ids for p in result.paths] == [[NEW_CLAIM_ID], [EVIDENCE_ID]]
    assert [e.relation_type.value for e in result.edges] == ["EVIDENCED_BY"]


def test_traverse_reaches_a_second_hop_with_explaining_paths():
    with _sqlite_graph() as store:
        result = store.traverse([NEW_CLAIM_ID], max_hops=2)
    assert [n.id for n in result.nodes] == [
        NEW_CLAIM_ID,
        ENTITY_ID,
        EPISODE_ID,
        EVIDENCE_ID,
        EXPERIENCE_ID,
        POLICY_ID,          # hop 2, via the entity, not via RELATED_TO
    ]
    policy_path = next(p for p in result.paths if p.node_ids[-1] == POLICY_ID)
    assert policy_path.node_ids == [NEW_CLAIM_ID, ENTITY_ID, POLICY_ID]
    assert policy_path.hops == 2
    assert RelationType.RELATED_TO not in {e.relation_type for e in result.edges}
    # Every returned node explains itself exactly once.
    endpoints = [p.node_ids[-1] for p in result.paths]
    assert sorted(endpoints) == sorted(n.id for n in result.nodes)
    assert len(endpoints) == len(set(endpoints))


def test_traverse_narrows_by_relation_type_kind_and_memory_class():
    with _sqlite_graph() as store:
        by_relation = store.traverse(
            [NEW_CLAIM_ID],
            filters=TraversalFilters(relation_types=[RelationType.EVIDENCED_BY]),
        )
        assert [n.id for n in by_relation.nodes] == [NEW_CLAIM_ID, EVIDENCE_ID]
        assert by_relation.warnings == []

        by_kind = store.traverse(
            [NEW_CLAIM_ID],
            max_hops=2,
            filters=TraversalFilters(node_kinds=[NodeKind.CLAIM, NodeKind.EVIDENCE]),
        )
        assert [n.id for n in by_kind.nodes] == [NEW_CLAIM_ID, EVIDENCE_ID]

        by_class = store.traverse(
            [NEW_CLAIM_ID],
            max_hops=2,
            filters=TraversalFilters(memory_classes=[MemoryClass.FACT]),
        )
        assert [n.id for n in by_class.nodes] == [NEW_CLAIM_ID]


def test_traverse_can_be_widened_to_historical_state_and_withdrawn_edges():
    """"What did this replace?" is a legitimate query (§7.4)."""
    with _sqlite_graph() as store:
        widened = store.traverse(
            [NEW_CLAIM_ID],
            filters=TraversalFilters(
                node_statuses=[NodeStatus.ACTIVE, NodeStatus.SUPERSEDED],
                relation_types=[RelationType.SUPERSEDES],
            ),
        )
        assert [n.id for n in widened.nodes] == [NEW_CLAIM_ID, OLD_CLAIM_ID]
        assert [e.relation_type.value for e in widened.edges] == ["SUPERSEDES"]

        # An empty status list means "no constraint on this dimension".
        unconstrained = store.traverse(
            [NEW_CLAIM_ID],
            filters=TraversalFilters(
                node_statuses=[], relation_types=[RelationType.SUPERSEDES]
            ),
        )
        assert [n.id for n in unconstrained.nodes] == [NEW_CLAIM_ID, OLD_CLAIM_ID]

        about = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID)
        store.append_edge(about.model_copy(update={"status": EdgeStatus.RETRACTED}))
        assert ENTITY_ID not in {n.id for n in store.traverse([NEW_CLAIM_ID]).nodes}
        reopened = store.traverse(
            [NEW_CLAIM_ID],
            filters=TraversalFilters(
                edge_statuses=[EdgeStatus.ACTIVE, EdgeStatus.RETRACTED]
            ),
        )
        assert ENTITY_ID in {n.id for n in reopened.nodes}


def test_traverse_includes_weak_relations_when_asked():
    with _sqlite_graph() as store:
        result = store.traverse(
            [NEW_CLAIM_ID], filters=TraversalFilters(include_weak_relations=True)
        )
    assert POLICY_ID in {n.id for n in result.nodes}
    assert next(p for p in result.paths if p.node_ids[-1] == POLICY_ID).hops == 1
    assert result.warnings == []


def test_traverse_applies_scope_before_returning_nodes():
    """§15: an unscoped node does not leak into a narrowed scope."""
    scope = GraphScope(user_id="u1", namespace="retrieval")
    nodes = [
        _claim(scope=scope),
        _entity().model_copy(update={"scope": scope}),
        _evidence(),
    ]
    edges = [
        _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID),
        _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID),
    ]
    with SQLiteGraphStore(":memory:", nodes=nodes, edges=edges) as store:
        matched = store.traverse(
            [NEW_CLAIM_ID], filters=TraversalFilters(scope=GraphScope(user_id="u1"))
        )
        assert [n.id for n in matched.nodes] == [NEW_CLAIM_ID, ENTITY_ID]

        other = store.traverse(
            [NEW_CLAIM_ID], filters=TraversalFilters(scope=GraphScope(user_id="u2"))
        )
        assert other.nodes == []
        assert other.warnings == [f"excluded_seed:{NEW_CLAIM_ID}"]


def test_traverse_is_a_point_in_time_query_when_as_of_is_set():
    old = _old_claim(status=NodeStatus.SUPERSEDED, valid_from=T0, valid_to=T1)
    new = _claim(valid_from=T1)
    supersedes = _typed_edge(
        RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID, valid_from=T1
    )
    historical = TraversalFilters(
        node_statuses=[NodeStatus.ACTIVE, NodeStatus.SUPERSEDED],
        relation_types=[RelationType.SUPERSEDES],
    )
    seeds = [NEW_CLAIM_ID, OLD_CLAIM_ID]
    with SQLiteGraphStore(":memory:", nodes=[old, new], edges=[supersedes]) as store:
        at_t0 = store.traverse(
            seeds, filters=historical.model_copy(update={"as_of": T0})
        )
        assert [n.id for n in at_t0.nodes] == [OLD_CLAIM_ID]
        assert at_t0.warnings == [f"excluded_seed:{NEW_CLAIM_ID}"]

        at_t1 = store.traverse(
            seeds, filters=historical.model_copy(update={"as_of": T1})
        )
        assert [n.id for n in at_t1.nodes] == [NEW_CLAIM_ID, OLD_CLAIM_ID]
        assert at_t1.warnings == []

        at_t2 = store.traverse(
            seeds, filters=historical.model_copy(update={"as_of": T2})
        )
        assert [n.id for n in at_t2.nodes] == [NEW_CLAIM_ID]
        assert at_t2.warnings == [f"excluded_seed:{OLD_CLAIM_ID}"]


def test_traverse_reports_its_budget_instead_of_silently_shrinking():
    with _sqlite_graph() as store:
        result = store.traverse(
            [NEW_CLAIM_ID], max_hops=2, filters=TraversalFilters(max_nodes=3)
        )
    assert len(result.nodes) == 3
    assert result.truncated is True
    assert "truncated:max_nodes" in result.warnings


def test_traverse_reports_unknown_seeds_rather_than_returning_empty():
    """§10.3: an empty traversal must not be indistinguishable from a miss."""
    with _sqlite_graph() as store:
        result = store.traverse(["claim_not_stored"])
    assert (result.nodes, result.edges) == ([], [])
    assert result.warnings == ["unknown_seed:claim_not_stored"]


def test_traverse_rejects_a_negative_hop_budget():
    with _sqlite_graph() as store:
        with pytest.raises(ValueError):
            store.traverse([NEW_CLAIM_ID], max_hops=-1)


def test_traversal_output_does_not_depend_on_insertion_order(tmp_path):
    forward = _sqlite_graph(tmp_path / "forward.sqlite3")
    backward = SQLiteGraphStore(
        tmp_path / "backward.sqlite3",
        nodes=list(reversed(_graph_nodes())),
        edges=list(reversed(_graph_edges())),
    )
    with forward, backward:
        assert (
            forward.traverse([NEW_CLAIM_ID], max_hops=2).to_json()
            == backward.traverse([NEW_CLAIM_ID], max_hops=2).to_json()
        )


def test_provenance_matches_the_reference_store_exactly():
    with _sqlite_graph() as store:
        for node_id in (NEW_CLAIM_ID, EXPERIENCE_ID, POLICY_ID, EVIDENCE_ID):
            assert (
                store.get_provenance(node_id).to_json()
                == _reference_graph().get_provenance(node_id).to_json()
            )

        record = store.get_provenance(NEW_CLAIM_ID)
        assert record.evidence_ids == [EVIDENCE_ID]
        assert record.episode_ids == [EPISODE_ID]
        assert record.warnings == []
        assert record.resolved is True


def test_provenance_reports_unresolvable_references_instead_of_inventing_them():
    """§10.2: an unresolvable reference is a warning, never a quiet drop, and
    never a `resolved` record."""
    orphan = _claim(
        id="claim_orphan_refs",
        source_refs=[
            SourceRef(episode_id="episode_absent", evidence_id="evidence_absent")
        ],
    )
    with SQLiteGraphStore(":memory:", nodes=[orphan]) as store:
        record = store.get_provenance("claim_orphan_refs")
    assert record.evidence_ids == ["evidence_absent"]
    assert record.episode_ids == ["episode_absent"]
    assert record.warnings == [
        "unresolved_episode:episode_absent",
        "unresolved_evidence:evidence_absent",
    ]
    assert record.resolved is False


def test_provenance_reports_a_withdrawn_evidence_link():
    with _sqlite_graph() as store:
        evidenced = _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID)
        store.append_edge(
            evidenced.model_copy(update={"status": EdgeStatus.RETRACTED})
        )
        record = store.get_provenance(NEW_CLAIM_ID)
    assert f"withdrawn_evidence:{EVIDENCE_ID}" in record.warnings
    assert record.resolved is False
    assert record.source_refs[0].evidence_id == EVIDENCE_ID


def test_provenance_of_an_unknown_node_raises_rather_than_answering_blankly():
    with _sqlite_graph() as store:
        with pytest.raises(UnknownNodeError) as excinfo:
            store.get_provenance("claim_not_stored")
    assert "claim_not_stored" in str(excinfo.value)


CONFLICT_GROUP = "conflict_index_cadence"


def _contradiction_fixture() -> tuple[list[GraphNode], list[GraphEdge]]:
    """Two live claims about one cadence, explicitly marked as contradicting."""
    nodes = [
        _claim(conflict_group=CONFLICT_GROUP),
        _old_claim(conflict_group=CONFLICT_GROUP),
        _entity(),
        _evidence(),
    ]
    edges = [
        _typed_edge(RelationType.CONTRADICTS, NEW_CLAIM_ID, OLD_CLAIM_ID),
        _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID),
        _typed_edge(RelationType.ABOUT, OLD_CLAIM_ID, ENTITY_ID),
    ]
    return nodes, edges


def test_find_conflicts_matches_the_reference_store_exactly():
    nodes, edges = _contradiction_fixture()
    reference = InMemoryGraphStore(nodes=nodes, edges=edges)
    with SQLiteGraphStore(":memory:", nodes=nodes, edges=edges) as store:
        found = store.find_conflicts()
        assert [r.to_json() for r in found] == [
            r.to_json() for r in reference.find_conflicts()
        ]
        assert len(found) == 1
        assert found[0].node_ids == sorted([NEW_CLAIM_ID, OLD_CLAIM_ID])
        assert found[0].conflict_group == CONFLICT_GROUP
        assert found[0].resolved is False

        # Narrowing by scope and by aboutness behaves the same way too.
        assert store.find_conflicts(entity_ids=[ENTITY_ID]) == found
        assert store.find_conflicts(entity_ids=["entity_unrelated"]) == []
        assert store.find_conflicts(scope=GraphScope(user_id="u9")) == []


def test_a_conflict_settled_by_a_status_transition_stops_being_reported():
    """§8.3: a conflict is resolved by a state transition, not by write order."""
    nodes, edges = _contradiction_fixture()
    with SQLiteGraphStore(":memory:", nodes=nodes, edges=edges) as store:
        assert len(store.find_conflicts()) == 1
        store.create_node(
            _old_claim(conflict_group=CONFLICT_GROUP, status=NodeStatus.SUPERSEDED)
        )
        assert store.find_conflicts() == []


# --- D. rebuild: staging, atomicity, idempotency -----------------------------

def _event(
    event_id: str,
    kind: MemoryEventKind = MemoryEventKind.INJECT,
    *,
    at: datetime = T0,
    node: GraphNode | None = None,
    edge: GraphEdge | None = None,
    target_id: str | None = None,
) -> MemoryEvent:
    return MemoryEvent(
        id=event_id,
        kind=kind,
        occurred_at=at,
        node=node,
        edge=edge,
        target_id=target_id,
    )


def _event_stream() -> list[MemoryEvent]:
    """One small, complete ledger: four nodes, two relations, one transition."""
    return [
        _event("ev_0001", node=_episode()),
        _event("ev_0002", node=_evidence()),
        _event("ev_0003", node=_entity()),
        _event("ev_0004", node=_old_claim()),
        _event("ev_0005", node=_claim()),
        _event(
            "ev_0006",
            edge=_typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID),
        ),
        _event(
            "ev_0007",
            edge=_typed_edge(RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID),
        ),
        _event(
            "ev_0008",
            MemoryEventKind.SUPERSEDE,
            at=T1,
            target_id=OLD_CLAIM_ID,
        ),
    ]


def test_rebuild_projects_the_stream_and_reports_truthfully():
    with SQLiteGraphStore(":memory:") as store:
        report = store.rebuild(_event_stream())
        assert report.ok is True
        assert report.events_read == 8
        assert report.nodes_written == 5
        assert report.edges_written == 2
        assert report.skipped == 0
        assert report.errors == []
        assert (store.health().node_count, store.health().edge_count) == (5, 2)
        # The transition in the ledger actually landed.
        assert store.get_node(OLD_CLAIM_ID).status is NodeStatus.SUPERSEDED


def test_rebuild_matches_the_reference_store_node_for_node():
    reference = InMemoryGraphStore()
    reference_report = reference.rebuild(_event_stream())
    with SQLiteGraphStore(":memory:") as store:
        assert store.rebuild(_event_stream()) == reference_report
        assert store.snapshot() == reference.snapshot()


def test_rebuilding_the_same_stream_twice_is_byte_identical(tmp_path):
    """§10.2: idempotent, with no duplicated rows and the same report."""
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database) as store:
        first_report = store.rebuild(_event_stream())
        first_snapshot = store.snapshot()
        second_report = store.rebuild(list(reversed(_event_stream())))

        assert second_report == first_report
        assert store.snapshot() == first_snapshot
        assert (store.health().node_count, store.health().edge_count) == (5, 2)

    # And it is still the same graph after the process would have ended.
    with SQLiteGraphStore(database) as reopened:
        assert reopened.snapshot() == first_snapshot


def test_rebuild_replaces_the_current_graph_rather_than_merging_into_it():
    """Nothing outside the event stream survives a rebuild."""
    with _sqlite_graph() as store:
        assert store.health().node_count == 7
        store.rebuild([_event("ev_only", node=_entity())])
        assert store.snapshot()["nodes"] == [_entity().to_dict()]
        assert store.snapshot()["edges"] == []
        assert store.health().healthy is True


def test_rebuild_from_an_empty_stream_empties_the_graph():
    with _sqlite_graph() as store:
        report = store.rebuild([])
        assert (report.events_read, report.nodes_written, report.edges_written) == (
            0,
            0,
            0,
        )
        assert store.snapshot() == {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "nodes": [],
            "edges": [],
        }


def test_rebuild_keeps_and_reports_a_dangling_edge_instead_of_dropping_it():
    """§10.2: a dangling reference is reported and the rebuild is fail-closed."""
    dangling = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, "entity_not_in_stream")
    stream = [_event("ev_0001", node=_claim()), _event("ev_0002", edge=dangling)]
    with SQLiteGraphStore(":memory:") as store:
        report = store.rebuild(stream)
        assert report.ok is False
        assert report.errors == [
            f"unresolved_edge_endpoint:{dangling.id}:entity_not_in_stream"
        ]
        assert report.edges_written == 1

        health = store.health()
        assert health.healthy is True
        assert health.node_count == 0
        assert health.edge_count == 0
        assert store.snapshot()["edges"] == []


def test_rebuild_reports_a_replayed_duplicate_without_writing_it_twice():
    stream = _event_stream() + [_event("ev_0005", node=_claim())]
    with SQLiteGraphStore(":memory:") as store:
        report = store.rebuild(stream)
        assert report.events_read == 9
        assert report.nodes_written == 5
        assert report.skipped == 1
        assert report.warnings == ["duplicate_event:ev_0005"]
        assert report.ok is True


def test_rebuild_reports_one_id_written_two_different_ways():
    stream = [
        _event("ev_0001", node=_claim()),
        _event("ev_0002", at=T1, node=_claim(content="The rebuild runs hourly.")),
    ]
    with SQLiteGraphStore(":memory:") as store:
        report = store.rebuild(stream)
        assert report.errors == [f"conflicting_node:{NEW_CLAIM_ID}"]
        assert report.ok is False
        assert store.snapshot()["nodes"] == []
        assert store.snapshot()["edges"] == []
        assert store.health().healthy is True


class _FlakyConnection(sqlite3.Connection):
    """A connection that fails one statement, to prove the rollback is real.

    Subclassing is how a test injects a storage failure without patching the
    store: ``sqlite3.Connection`` is a C type whose methods cannot be
    monkeypatched, and the store accepts a borrowed connection by contract.
    """

    fail_on: str | None = None

    def executemany(self, sql, parameters):  # type: ignore[override]
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("simulated storage failure mid-rebuild")
        return super().executemany(sql, parameters)


def test_a_failed_rebuild_leaves_the_previous_graph_exactly_as_it_was(tmp_path):
    """The staging/replace split, under the only condition that proves it: a
    failure *after* the delete and the node inserts have already run. The old
    graph has to come back whole — not empty, and not half-replaced."""
    database = tmp_path / "graph.sqlite3"
    connection = sqlite3.connect(database, factory=_FlakyConnection)
    store = SQLiteGraphStore(connection, nodes=_graph_nodes(), edges=_graph_edges())
    before = store.snapshot()
    before_health = store.health()

    connection.fail_on = "INSERT INTO graph_edges"
    with pytest.raises(RuntimeError, match="simulated storage failure"):
        store.rebuild(_event_stream())

    connection.fail_on = None
    assert store.snapshot() == before
    assert store.health() == before_health
    store.close()
    connection.close()

    # And the rollback reached the file, not just this handle.
    with SQLiteGraphStore(database) as reopened:
        assert reopened.snapshot() == before
        assert reopened.health().healthy is True


def test_a_rebuild_that_fails_before_the_write_touches_nothing(monkeypatch, tmp_path):
    """The staged rows are built *before* the transaction opens, so the window
    in which the current graph is gone is as short as it can be."""
    from layered_memory_mcp import graph_sqlite_store as module

    database = tmp_path / "graph.sqlite3"
    with _sqlite_graph(database) as store:
        before = store.snapshot()

        def _explode(edge):
            raise RuntimeError("simulated staging failure")

        monkeypatch.setattr(module, "_edge_row", _explode)
        with pytest.raises(RuntimeError, match="simulated staging failure"):
            store.rebuild(_event_stream())

        monkeypatch.undo()
        assert store.snapshot() == before


# --- D2. the publish boundary: a stale rebuild must not lose a newer commit --
#
# The window the staging/replace split necessarily opens: the candidate is
# built from a stream *before* the write lock is taken, so between staging and
# publishing another connection can commit a node the candidate does not
# contain. Publishing anyway would delete it — a lost update that no error, no
# health check and no drift audit would attribute to the rebuild.


class _PublishBarrierConnection(sqlite3.Connection):
    """A connection that runs a one-shot hook at the publish boundary.

    ``rebuild`` stages its candidate and *then* opens the write transaction, so
    the single ``BEGIN IMMEDIATE`` statement is exactly the line between "this
    candidate describes the graph" and "this candidate replaces the graph". The
    hook fires once, immediately before that statement is sent, which puts a
    concurrent commit in that window deterministically: no threads, no sleeps
    and nothing that depends on interpreter scheduling. Subclassing is the same
    technique ``_FlakyConnection`` uses, and for the same reason — the store
    accepts a borrowed connection by contract.
    """

    at_publish = None

    def execute(self, sql, *parameters):  # type: ignore[override]
        if sql == "BEGIN IMMEDIATE" and self.at_publish is not None:
            hook, self.at_publish = self.at_publish, None
            hook()
        return super().execute(sql, *parameters)


def test_a_stale_rebuild_refuses_rather_than_deleting_a_newer_commit(tmp_path):
    """The whole point: the newer commit is still there afterwards."""
    database = tmp_path / "graph.sqlite3"
    publisher = sqlite3.connect(database, factory=_PublishBarrierConnection)
    store = SQLiteGraphStore(publisher)
    store.rebuild(_event_stream())
    projected = store.snapshot()

    # A different writer entirely, on its own connection to the same file.
    other = SQLiteGraphStore(database)
    newer = _policy()
    landed: dict[str, int] = {}

    def _another_writer_commits():
        other.create_node(newer)
        landed["revision"] = other.revision()

    publisher.at_publish = _another_writer_commits
    with pytest.raises(StaleRebuildError) as excinfo:
        store.rebuild(_event_stream())

    # The barrier really did fire inside the staging window, and the refusal
    # names both revisions rather than failing anonymously.
    assert landed["revision"] > 0
    assert str(landed["revision"]) in str(excinfo.value)

    # 1. The newer node survived — on the rebuilding handle, on the writer's
    #    handle, and on the file after both are gone.
    assert store.get_node(POLICY_ID) == newer
    assert other.get_node(POLICY_ID) == newer

    # 2. Nothing was partially replaced: the rejected candidate deleted none of
    #    the rows it was about to rewrite.
    after = store.snapshot()
    assert after["edges"] == projected["edges"]
    assert after["nodes"] == sorted(
        [*projected["nodes"], newer.to_dict()], key=lambda payload: payload["id"]
    )

    # 3. And the rejected publish moved no checkpoint of its own either.
    assert store.revision() == landed["revision"]
    assert store.health().healthy is True

    store.close()
    other.close()
    publisher.close()
    with SQLiteGraphStore(database) as reopened:
        assert reopened.snapshot() == after
        assert reopened.health().healthy is True


def test_a_rebuild_re_derived_after_the_refusal_publishes_cleanly(tmp_path):
    """Refusing is only half of it: the retry has to be able to succeed.

    The safe retry is the one that re-derives the candidate from a ledger that
    now describes the newer commit — not a replay of the same stale stream.
    """
    database = tmp_path / "graph.sqlite3"
    publisher = sqlite3.connect(database, factory=_PublishBarrierConnection)
    with SQLiteGraphStore(publisher) as store:
        store.rebuild(_event_stream())
        with SQLiteGraphStore(database) as other:
            publisher.at_publish = lambda: other.create_node(_policy())
            with pytest.raises(StaleRebuildError):
                store.rebuild(_event_stream())

        # Replaying the same stale stream is refused again, deliberately: the
        # candidate is unchanged, so it is still the one that loses the node.
        publisher.at_publish = None
        extended = [*_event_stream(), _event("ev_0009", at=T2, node=_policy())]
        report = store.rebuild(extended)

        assert report.ok is True
        assert report.nodes_written == 6
        assert store.get_node(POLICY_ID) == _policy()
        assert store.health().healthy is True


def test_an_explicit_expected_revision_closes_the_callers_own_window(tmp_path):
    """A caller that read its ledger earlier owns a wider window than the one
    ``rebuild`` can see, and ``expected_revision`` is how it names it."""
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database) as store:
        store.rebuild(_event_stream())
        read_at = store.revision()

        # ... the caller plans. Meanwhile somebody commits.
        with SQLiteGraphStore(database) as other:
            other.create_node(_policy())

        with pytest.raises(StaleRebuildError):
            store.rebuild(_event_stream(), expected_revision=read_at)

        # Refused with nothing touched, exactly as the implicit window is.
        assert store.get_node(POLICY_ID) == _policy()
        assert store.revision() == read_at + 1

        # And the unchanged-window case still publishes.
        assert store.rebuild(_event_stream(), expected_revision=read_at + 1).ok


def test_a_refused_rebuild_moves_the_revision_only_when_it_published(tmp_path):
    """Fail-closed on an invalid candidate is unchanged — and still inert.

    The malformed-ledger refusal stays a *report*, not an exception, because a
    caller distinguishes "your ledger does not project" from "the graph moved
    under you"; neither may leave the counter or the rows disturbed.
    """
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database) as store:
        store.rebuild(_event_stream())
        published_at = store.revision()
        before = store.snapshot()

        dangling = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, "entity_not_in_stream")
        report = store.rebuild(
            [_event("ev_0001", node=_claim()), _event("ev_0002", edge=dangling)]
        )

        assert report.ok is False
        assert report.errors == [
            f"unresolved_edge_endpoint:{dangling.id}:entity_not_in_stream"
        ]
        assert store.snapshot() == before
        assert store.revision() == published_at

        # A write that does commit moves it, which is what the compare reads.
        store.create_node(_policy())
        assert store.revision() == published_at + 1


# --- E. drift: the projection vs. the source it claims to project ------------

def test_a_store_that_matches_its_source_reports_no_drift():
    with _sqlite_graph() as store:
        report = store.audit_drift(_graph_nodes(), _graph_edges())
    assert report.drifted is False
    assert report.backend == SQLITE_GRAPH_BACKEND
    assert (report.expected_nodes, report.stored_nodes) == (7, 7)
    assert (report.expected_edges, report.stored_edges) == (10, 10)
    assert report.errors == []


def test_drift_names_missing_nodes_and_edges():
    """The projection is behind its source, or a write was lost."""
    with SQLiteGraphStore(":memory:", nodes=[_claim(), _evidence()]) as store:
        report = store.audit_drift(_graph_nodes(), _graph_edges())
    assert report.missing_nodes == sorted(
        [ENTITY_ID, EPISODE_ID, EXPERIENCE_ID, OLD_CLAIM_ID, POLICY_ID]
    )
    assert report.missing_edges == sorted(e.id for e in _graph_edges())
    assert report.unexpected_nodes == []
    assert report.drifted is True


def test_drift_names_nodes_and_edges_the_source_no_longer_has():
    """What a rebuild that was never run looks like."""
    with _sqlite_graph() as store:
        report = store.audit_drift([_claim()], [])
    assert report.unexpected_nodes == sorted(
        [ENTITY_ID, EPISODE_ID, EVIDENCE_ID, EXPERIENCE_ID, OLD_CLAIM_ID, POLICY_ID]
    )
    assert report.unexpected_edges == sorted(e.id for e in _graph_edges())
    assert report.missing_nodes == []
    assert report.changed_nodes == []


def test_drift_names_a_payload_that_differs_byte_for_byte():
    with _sqlite_graph() as store:
        expected_nodes = [
            _claim(content="The retrieval index rebuild runs hourly."),
            *_graph_nodes()[1:],
        ]
        expected_edges = list(_graph_edges())
        expected_edges[0] = expected_edges[0].model_copy(
            update={"confidence": 0.99}
        )
        report = store.audit_drift(expected_nodes, expected_edges)

    assert report.changed_nodes == [NEW_CLAIM_ID]
    assert report.changed_edges == [expected_edges[0].id]
    assert report.missing_nodes == []
    assert report.unexpected_nodes == []
    assert report.drifted is True


def test_drift_reports_a_payload_schema_version_it_was_not_expecting():
    future = _claim(schema_version=GRAPH_SCHEMA_VERSION + 1)
    with SQLiteGraphStore(":memory:", nodes=[future]) as store:
        report = store.audit_drift([future])
    assert report.schema_mismatches == [
        f"schema_version_mismatch:{NEW_CLAIM_ID}:{GRAPH_SCHEMA_VERSION + 1}"
    ]
    # The payload itself matches, so this is a *schema* finding, not a diff.
    assert report.changed_nodes == []
    assert report.drifted is True


def test_drift_reports_a_database_recorded_under_another_graph_schema(tmp_path):
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database, nodes=[_claim()]):
        pass
    with sqlite3.connect(database) as raw:
        raw.execute(
            "UPDATE graph_meta SET value = ? WHERE key = ?",
            (str(GRAPH_SCHEMA_VERSION + 1), META_GRAPH_SCHEMA_VERSION),
        )

    with SQLiteGraphStore(database) as store:
        report = store.audit_drift([_claim()])
        assert report.schema_mismatches == [
            f"schema_version_mismatch:store:{GRAPH_SCHEMA_VERSION + 1}"
        ]
        assert report.drifted is True
        # Health sees the same mismatch, from its own angle.
        assert (
            f"schema_version_mismatch:store:{GRAPH_SCHEMA_VERSION + 1}"
            in store.health().errors
        )


def test_an_audit_that_could_not_read_everything_does_not_read_as_clean(tmp_path):
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database, nodes=[_claim()]):
        pass
    with sqlite3.connect(database) as raw:
        raw.execute(
            "UPDATE graph_nodes SET payload = ? WHERE id = ?",
            ("{not json", NEW_CLAIM_ID),
        )

    with SQLiteGraphStore(database) as store:
        report = store.audit_drift([_claim()])
    assert report.errors == [f"corrupt_payload:{NEW_CLAIM_ID}"]
    assert report.drifted is True


def test_audit_drift_never_raises_on_a_closed_store():
    store = _sqlite_graph()
    store.close()
    report = store.audit_drift(_graph_nodes(), _graph_edges())
    assert report.errors == ["store_closed"]
    assert report.drifted is True
    assert report.stored_nodes == 0


def test_drift_is_not_health():
    """A store can be perfectly healthy and completely stale. Conflating the
    two would let a green health check vouch for a stale projection."""
    with _sqlite_graph() as store:
        assert store.health().healthy is True
        assert store.audit_drift([_claim()], []).drifted is True


def test_drift_findings_are_sorted_and_reproducible(tmp_path):
    forward = _sqlite_graph(tmp_path / "forward.sqlite3")
    backward = SQLiteGraphStore(
        tmp_path / "backward.sqlite3",
        nodes=list(reversed(_graph_nodes())),
        edges=list(reversed(_graph_edges())),
    )
    with forward, backward:
        expected = ([_claim(), _policy()], [_graph_edges()[0]])
        first = forward.audit_drift(*expected)
        second = backward.audit_drift(*expected)
        assert first.to_json() == second.to_json()
        for findings in (
            first.missing_nodes,
            first.missing_edges,
            first.unexpected_nodes,
            first.unexpected_edges,
            first.changed_nodes,
            first.changed_edges,
            first.schema_mismatches,
        ):
            assert findings == sorted(findings)


def test_drift_after_a_rebuild_shows_what_the_new_ledger_dropped():
    """The realistic use: audit the persisted projection against the ledger it
    was supposed to have been built from."""
    reference = InMemoryGraphStore()
    reference.rebuild(_event_stream())
    with SQLiteGraphStore(":memory:") as store:
        store.rebuild(_event_stream())
        clean = store.audit_drift(
            [GraphNode.model_validate(n) for n in reference.snapshot()["nodes"]],
            [GraphEdge.model_validate(e) for e in reference.snapshot()["edges"]],
        )
        assert clean.drifted is False

        store.rebuild(_event_stream()[:2])
        stale = store.audit_drift(
            [GraphNode.model_validate(n) for n in reference.snapshot()["nodes"]],
            [GraphEdge.model_validate(e) for e in reference.snapshot()["edges"]],
        )
        assert stale.missing_nodes == sorted([ENTITY_ID, NEW_CLAIM_ID, OLD_CLAIM_ID])
        assert len(stale.missing_edges) == 2
        assert stale.unexpected_nodes == []


# --- F. isolation: nothing is cached, nothing is shared ----------------------

def test_a_returned_model_cannot_reach_back_into_the_store():
    with SQLiteGraphStore(":memory:", nodes=[_claim()]) as store:
        returned = store.get_node(NEW_CLAIM_ID)
        returned.content = "mutated after the read"
        returned.tags.append("mutated")
        assert store.get_node(NEW_CLAIM_ID).content == (
            "The retrieval index rebuild runs nightly."
        )
        assert store.get_node(NEW_CLAIM_ID).tags == []


def test_a_second_store_over_one_file_never_serves_a_stale_graph(tmp_path):
    """Nothing is memoized across calls, so two handles on one database cannot
    disagree about what it holds."""
    database = tmp_path / "graph.sqlite3"
    with SQLiteGraphStore(database, nodes=[_claim()]) as writer:
        with SQLiteGraphStore(database) as reader:
            assert reader.get_node(ENTITY_ID) is None
            writer.create_node(_entity())
            assert reader.get_node(ENTITY_ID) == _entity()
            assert reader.health().node_count == 2


def test_two_databases_never_share_state(tmp_path):
    first = SQLiteGraphStore(tmp_path / "first.sqlite3", nodes=[_claim()])
    second = SQLiteGraphStore(tmp_path / "second.sqlite3")
    with first, second:
        assert second.get_node(NEW_CLAIM_ID) is None
        assert second.health().node_count == 0

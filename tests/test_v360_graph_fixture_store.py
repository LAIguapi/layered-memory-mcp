"""v3.6.0 Batch C — the deterministic in-memory fixture graph store.

Why this file exists: ARCHITECTURE_v2.md §9.2 step 1 and §12 Batch C require a
real, shippable implementation of the §9.1 ``GraphStore`` contract that keeps
everything in memory. Batch B only proved the contract was *implementable*
(with a throwaway ``_FixtureStore`` living inside its own test file); every
later batch — the legacy importer (D), the embedded backend (E), graph-assisted
recall (F) — needs one agreed reference implementation to compare against.

These tests pin the properties that make such a reference worth having:

  * **It is the contract.** All nine §9.1 operations, with the documented
    semantics: upsert by node ID, append-only edges, ``None`` for an unknown
    node, filters applied during expansion, idempotent rebuild.
  * **It is deterministic.** Same nodes and edges in, same bytes out —
    regardless of insertion order. Every returned collection has one documented
    sort order, so a fixture may assert on it.
  * **It is isolated.** Two stores never share state, and a caller holding a
    returned model cannot reach back into the store through it.
  * **It is honest.** A truncated traversal says so, an unresolvable provenance
    reference becomes a warning, an unresolved contradiction is reported rather
    than settled by write order, and a rebuild names its duplicates and its
    dangling references instead of dropping them.

Nothing here touches the hot path, a persistent backend or real memory data.
Fixture data is neutral technical vocabulary: a retrieval index and its
rebuild cadence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from layered_memory_mcp.graph_fixture_store import (
    FIXTURE_GRAPH_BACKEND,
    EdgeConflictError,
    InMemoryGraphStore,
    UnknownNodeError,
)
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
from layered_memory_mcp.graph_store import (
    GraphStore,
    TraversalFilters,
    missing_graph_store_operations,
)

# --- neutral fixture vocabulary ---------------------------------------------

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)

EPISODE_ID = "episode_build_0007"
EVIDENCE_ID = "evidence_build_0007_0002"
ENTITY_ID = "entity_retrieval_index"
NEW_CLAIM_ID = "claim_index_rebuild_nightly"
OLD_CLAIM_ID = "claim_index_rebuild_weekly"


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


def _episode() -> GraphNode:
    return GraphNode(
        id=EPISODE_ID,
        kind=NodeKind.EPISODE,
        label="build session 0007",
        recorded_at=T0,
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
        id=ENTITY_ID,
        kind=NodeKind.ENTITY,
        label="retrieval index",
        recorded_at=T0,
    )


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


def _edge(**kwargs) -> GraphEdge:
    data = {
        "id": "edge_claim_evidenced",
        "relation_type": RelationType.EVIDENCED_BY,
        "source_id": NEW_CLAIM_ID,
        "target_id": EVIDENCE_ID,
        "recorded_at": T0,
    }
    data.update(kwargs)
    return GraphEdge(**data)


def _typed_edge(relation: RelationType, source: str, target: str, **kwargs) -> GraphEdge:
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


# --- A. the contract ---------------------------------------------------------

def test_store_is_a_complete_graph_store():
    store = InMemoryGraphStore()
    assert missing_graph_store_operations(store) == []
    assert isinstance(store, GraphStore)


def test_empty_store_is_healthy_and_empty():
    health = InMemoryGraphStore().health()
    assert health.backend == FIXTURE_GRAPH_BACKEND
    assert health.healthy is True
    assert (health.node_count, health.edge_count) == (0, 0)
    assert health.schema_version == GRAPH_SCHEMA_VERSION


# --- B. create_node / get_node ----------------------------------------------

def test_create_node_stores_and_returns_the_node():
    store = InMemoryGraphStore()
    claim = _claim()
    stored = store.create_node(claim)
    assert stored == claim
    assert store.get_node(NEW_CLAIM_ID) == claim
    assert store.health().node_count == 1


def test_get_node_returns_none_for_an_unknown_id():
    store = InMemoryGraphStore()
    store.create_node(_claim())
    assert store.get_node("claim_not_stored") is None


def test_repeating_an_identical_create_is_idempotent():
    """A rerun of an import must not duplicate a node (§9.1 upsert)."""
    store = InMemoryGraphStore()
    store.create_node(_claim())
    before = store.snapshot()
    store.create_node(_claim())
    assert store.snapshot() == before
    assert store.health().node_count == 1


def test_create_node_with_conflicting_content_replaces_by_id():
    """Upsert is keyed on ID: the later write wins, deterministically."""
    store = InMemoryGraphStore()
    store.create_node(_claim())
    revised = _claim(content="The retrieval index rebuild runs hourly.", version=2)
    store.create_node(revised)
    assert store.get_node(NEW_CLAIM_ID) == revised
    assert store.health().node_count == 1


def test_stored_nodes_are_copies_so_a_caller_cannot_mutate_the_store():
    store = InMemoryGraphStore()
    claim = _claim()
    store.create_node(claim)

    claim.content = "mutated after the write"
    returned = store.get_node(NEW_CLAIM_ID)
    returned.label = "mutated after the read"

    assert store.get_node(NEW_CLAIM_ID).content == (
        "The retrieval index rebuild runs nightly."
    )
    assert store.get_node(NEW_CLAIM_ID).label == "Retrieval index rebuild cadence"


def test_two_stores_never_share_state():
    first = InMemoryGraphStore()
    second = InMemoryGraphStore()
    first.create_node(_claim())
    assert second.get_node(NEW_CLAIM_ID) is None
    assert second.health().node_count == 0


def test_a_store_can_be_seeded_from_explicit_fixture_data():
    store = InMemoryGraphStore(nodes=[_claim(), _evidence()], edges=[_edge()])
    assert store.get_node(NEW_CLAIM_ID) is not None
    assert (store.health().node_count, store.health().edge_count) == (2, 1)


# --- C. deterministic snapshot ----------------------------------------------

def test_snapshot_is_independent_of_insertion_order():
    forward = InMemoryGraphStore()
    for node in (_episode(), _evidence(), _claim()):
        forward.create_node(node)

    backward = InMemoryGraphStore()
    for node in (_claim(), _evidence(), _episode()):
        backward.create_node(node)

    assert forward.snapshot() == backward.snapshot()
    assert [n["id"] for n in forward.snapshot()["nodes"]] == sorted(
        [EPISODE_ID, EVIDENCE_ID, NEW_CLAIM_ID]
    )


def test_snapshot_leaks_no_python_objects():
    store = InMemoryGraphStore(nodes=[_claim(), _evidence()], edges=[_edge()])
    snapshot = store.snapshot()
    assert snapshot["schema_version"] == GRAPH_SCHEMA_VERSION
    for payload in snapshot["nodes"] + snapshot["edges"]:
        for value in payload.values():
            assert not isinstance(value, (NodeKind, NodeStatus, datetime))
    assert snapshot["nodes"][0]["kind"] in {"claim", "evidence"}


def test_snapshot_is_a_copy_not_a_live_view():
    store = InMemoryGraphStore(nodes=[_claim()])
    snapshot = store.snapshot()
    snapshot["nodes"].clear()
    assert len(store.snapshot()["nodes"]) == 1


def test_scope_and_derivation_survive_a_write_unchanged():
    scoped = _claim(
        scope=GraphScope(user_id="u1", namespace="retrieval"),
        source_refs=[],
        derivation=_derivation(method=DerivationMethod.MIGRATED),
    )
    store = InMemoryGraphStore()
    store.create_node(scoped)
    read_back = store.get_node(NEW_CLAIM_ID)
    assert read_back.scope.namespace == "retrieval"
    assert read_back.derivation.method is DerivationMethod.MIGRATED
    assert read_back.to_json() == scoped.to_json()


# --- D. append_edge is append-only ------------------------------------------

def test_append_edge_stores_and_returns_the_edge():
    store = InMemoryGraphStore()
    edge = store.append_edge(_edge())
    assert edge == _edge()
    assert store.health().edge_count == 1
    assert store.get_edges(NEW_CLAIM_ID, direction="outgoing") == [_edge()]


def test_repeating_an_identical_append_is_idempotent():
    store = InMemoryGraphStore()
    store.append_edge(_edge())
    before = store.snapshot()
    store.append_edge(_edge())
    assert store.snapshot() == before
    assert store.health().edge_count == 1


def test_reasserting_one_edge_id_with_a_different_relation_is_refused():
    """§6.2: an edge is never edited in place. A changed relation is a new
    edge, so silently overwriting one would erase an asserted relation."""
    store = InMemoryGraphStore()
    store.append_edge(_edge())
    with pytest.raises(EdgeConflictError) as excinfo:
        store.append_edge(_edge(relation_type=RelationType.MENTIONS))
    assert "edge_claim_evidenced" in str(excinfo.value)
    assert store.get_edges(NEW_CLAIM_ID)[0].relation_type is RelationType.EVIDENCED_BY


def test_reasserting_one_edge_id_with_different_provenance_is_refused():
    store = InMemoryGraphStore()
    store.append_edge(_edge())
    with pytest.raises(EdgeConflictError):
        store.append_edge(_edge(evidence_refs=[_source_ref()]))


def test_an_active_edge_may_be_withdrawn_in_place():
    """Withdrawal is the one lifecycle move an edge has: EdgeStatus carries no
    `superseded`, so a retraction has to land on the existing assertion."""
    store = InMemoryGraphStore()
    store.append_edge(_edge())
    withdrawn = store.append_edge(_edge(status=EdgeStatus.RETRACTED))
    assert withdrawn.status is EdgeStatus.RETRACTED
    assert store.get_edges(NEW_CLAIM_ID)[0].status is EdgeStatus.RETRACTED
    assert store.health().edge_count == 1


def test_a_withdrawn_edge_cannot_be_reasserted_or_rewithdrawn():
    store = InMemoryGraphStore()
    store.append_edge(_edge(status=EdgeStatus.RETRACTED))
    with pytest.raises(EdgeConflictError):
        store.append_edge(_edge(status=EdgeStatus.ACTIVE))
    with pytest.raises(EdgeConflictError):
        store.append_edge(_edge(status=EdgeStatus.EXPIRED))
    assert store.get_edges(NEW_CLAIM_ID)[0].status is EdgeStatus.RETRACTED


def test_a_distinct_discriminator_makes_a_second_assertion_of_one_triple():
    """The documented way to re-assert a withdrawn relation (§make_edge_id)."""
    store = InMemoryGraphStore()
    first = _typed_edge(RelationType.APPLIES_TO, NEW_CLAIM_ID, ENTITY_ID)
    store.append_edge(first)
    store.append_edge(first.model_copy(update={"status": EdgeStatus.RETRACTED}))
    again = _typed_edge(
        RelationType.APPLIES_TO,
        NEW_CLAIM_ID,
        ENTITY_ID,
        id=make_edge_id(
            RelationType.APPLIES_TO, NEW_CLAIM_ID, ENTITY_ID, discriminator="2026-03"
        ),
        valid_from=T2,
    )
    store.append_edge(again)
    statuses = sorted(e.status.value for e in store.get_edges(NEW_CLAIM_ID))
    assert statuses == ["active", "retracted"]


# --- E. get_edges ------------------------------------------------------------

def _fixture_nodes() -> list[GraphNode]:
    return [_claim(), _old_claim(), _evidence(), _episode(), _entity()]


def _fixture_edges() -> list[GraphEdge]:
    return [
        _typed_edge(RelationType.OBSERVED_IN, NEW_CLAIM_ID, EPISODE_ID),
        _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID),
        _typed_edge(RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID),
        _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID),
        _typed_edge(RelationType.MENTIONS, EPISODE_ID, ENTITY_ID),
        _typed_edge(RelationType.DERIVED_FROM, OLD_CLAIM_ID, NEW_CLAIM_ID),
    ]


def _edge_fixture_store() -> InMemoryGraphStore:
    """A small graph whose edges span several relation types and directions."""
    return InMemoryGraphStore(nodes=_fixture_nodes(), edges=_fixture_edges())


def test_get_edges_separates_direction():
    store = _edge_fixture_store()
    outgoing = store.get_edges(NEW_CLAIM_ID, direction="outgoing")
    incoming = store.get_edges(NEW_CLAIM_ID, direction="incoming")
    both = store.get_edges(NEW_CLAIM_ID, direction="both")

    assert [e.relation_type.value for e in outgoing] == [
        "ABOUT",
        "EVIDENCED_BY",
        "OBSERVED_IN",
        "SUPERSEDES",
    ]
    assert [e.relation_type.value for e in incoming] == ["DERIVED_FROM"]
    assert len(both) == len(outgoing) + len(incoming)
    assert both == sorted(both, key=lambda e: (e.relation_type.value, e.source_id, e.id))


def test_get_edges_order_is_independent_of_insertion_order():
    forward = _edge_fixture_store()
    reversed_store = InMemoryGraphStore(
        nodes=list(reversed(_fixture_nodes())),
        edges=list(reversed(_fixture_edges())),
    )
    assert [e.id for e in reversed_store.get_edges(NEW_CLAIM_ID)] == [
        e.id for e in forward.get_edges(NEW_CLAIM_ID)
    ]


def test_get_edges_narrows_by_relation_type():
    store = _edge_fixture_store()
    found = store.get_edges(
        NEW_CLAIM_ID,
        relation_types=[RelationType.EVIDENCED_BY, RelationType.SUPERSEDES],
    )
    assert [e.relation_type.value for e in found] == ["EVIDENCED_BY", "SUPERSEDES"]
    assert store.get_edges(NEW_CLAIM_ID, relation_types=[RelationType.BLOCKS]) == []
    # An empty sequence means "no narrowing", exactly like None.
    assert store.get_edges(NEW_CLAIM_ID, relation_types=[]) == store.get_edges(
        NEW_CLAIM_ID
    )


def test_get_edges_includes_withdrawn_edges():
    """`get_edges` is the raw relation reader; status filtering belongs to
    `traverse`, which is where the hard filters live (§7.3 step 4)."""
    store = _edge_fixture_store()
    store.append_edge(
        _typed_edge(
            RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID, status=EdgeStatus.RETRACTED
        )
    )
    statuses = [e.status.value for e in store.get_edges(NEW_CLAIM_ID)]
    assert "retracted" in statuses


def test_get_edges_of_an_unknown_or_isolated_node_is_empty():
    store = _edge_fixture_store()
    assert store.get_edges("claim_not_stored") == []
    store.create_node(GraphNode(id="entity_scheduler", kind=NodeKind.ENTITY))
    assert store.get_edges("entity_scheduler") == []


def test_get_edges_returns_copies():
    store = _edge_fixture_store()
    found = store.get_edges(NEW_CLAIM_ID)
    found[0].confidence = 0.01
    assert store.get_edges(NEW_CLAIM_ID)[0].confidence != 0.01


def test_get_edges_rejects_an_unknown_direction():
    store = _edge_fixture_store()
    with pytest.raises(ValueError):
        store.get_edges(NEW_CLAIM_ID, direction="sideways")


# --- F. traverse -------------------------------------------------------------

POLICY_ID = "policy_index_freshness"
EXPERIENCE_ID = "experience_index_staleness"


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


def _traversal_nodes() -> list[GraphNode]:
    return [
        _claim(),
        _old_claim(status=NodeStatus.SUPERSEDED),
        _evidence(),
        _episode(),
        _entity(),
        _policy(),
        _experience(),
    ]


def _traversal_edges() -> list[GraphEdge]:
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


def _traversal_store() -> InMemoryGraphStore:
    return InMemoryGraphStore(nodes=_traversal_nodes(), edges=_traversal_edges())


def test_traverse_expands_one_hop_in_both_directions_by_default():
    result = _traversal_store().traverse([NEW_CLAIM_ID])
    assert result.seed_ids == [NEW_CLAIM_ID]
    assert [n.id for n in result.nodes] == [
        NEW_CLAIM_ID,       # hop 0
        ENTITY_ID,          # hop 1, outgoing ABOUT
        EPISODE_ID,         # hop 1, outgoing OBSERVED_IN
        EVIDENCE_ID,        # hop 1, outgoing EVIDENCED_BY
        EXPERIENCE_ID,      # hop 1, incoming DERIVED_FROM
    ]
    assert result.truncated is False


def test_traverse_defaults_leave_out_historical_state_and_weak_relations():
    """§8.2 and §6.2: a superseded claim is history, and RELATED_TO alone
    cannot justify pulling a node into context."""
    result = _traversal_store().traverse([NEW_CLAIM_ID])
    assert OLD_CLAIM_ID not in {n.id for n in result.nodes}
    assert POLICY_ID not in {n.id for n in result.nodes}
    assert result.warnings == ["weak_relation_excluded:RELATED_TO"]


def test_traverse_with_zero_hops_returns_only_the_seeds():
    result = _traversal_store().traverse([NEW_CLAIM_ID, EVIDENCE_ID], max_hops=0)
    assert [n.id for n in result.nodes] == [NEW_CLAIM_ID, EVIDENCE_ID]
    assert [p.node_ids for p in result.paths] == [[NEW_CLAIM_ID], [EVIDENCE_ID]]
    # The induced subgraph over the returned nodes is still reported.
    assert [e.relation_type.value for e in result.edges] == ["EVIDENCED_BY"]


def test_traverse_reaches_a_second_hop_without_using_the_weak_edge():
    result = _traversal_store().traverse([NEW_CLAIM_ID], max_hops=2)
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


def test_every_returned_node_has_exactly_one_explaining_path():
    result = _traversal_store().traverse([NEW_CLAIM_ID], max_hops=2)
    endpoints = [p.node_ids[-1] for p in result.paths]
    assert sorted(endpoints) == sorted(n.id for n in result.nodes)
    assert len(endpoints) == len(set(endpoints))
    assert result.paths == sorted(
        result.paths, key=lambda p: (p.hops, p.node_ids, p.edge_ids)
    )


def test_traversal_output_is_independent_of_insertion_order():
    forward = _traversal_store()
    backward = InMemoryGraphStore(
        nodes=list(reversed(_traversal_nodes())),
        edges=list(reversed(_traversal_edges())),
    )
    assert forward.traverse([NEW_CLAIM_ID], max_hops=2).to_json() == backward.traverse(
        [NEW_CLAIM_ID], max_hops=2
    ).to_json()


def test_traverse_deduplicates_seeds_but_keeps_the_caller_order():
    result = _traversal_store().traverse(
        [EVIDENCE_ID, NEW_CLAIM_ID, EVIDENCE_ID], max_hops=0
    )
    assert result.seed_ids == [EVIDENCE_ID, NEW_CLAIM_ID]


def test_traverse_narrows_by_relation_type():
    filters = TraversalFilters(relation_types=[RelationType.EVIDENCED_BY])
    result = _traversal_store().traverse([NEW_CLAIM_ID], filters=filters)
    assert [n.id for n in result.nodes] == [NEW_CLAIM_ID, EVIDENCE_ID]
    # Nothing weak was considered, so nothing is warned about.
    assert result.warnings == []


def test_traverse_narrows_by_node_kind_and_memory_class():
    store = _traversal_store()
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


def test_traverse_can_be_widened_to_historical_state_on_purpose():
    """"What did this replace?" is a legitimate query (§7.4)."""
    filters = TraversalFilters(
        node_statuses=[NodeStatus.ACTIVE, NodeStatus.SUPERSEDED],
        relation_types=[RelationType.SUPERSEDES],
    )
    result = _traversal_store().traverse([NEW_CLAIM_ID], filters=filters)
    assert [n.id for n in result.nodes] == [NEW_CLAIM_ID, OLD_CLAIM_ID]
    assert [e.relation_type.value for e in result.edges] == ["SUPERSEDES"]


def test_an_empty_status_filter_means_no_status_constraint():
    filters = TraversalFilters(node_statuses=[], relation_types=[RelationType.SUPERSEDES])
    result = _traversal_store().traverse([NEW_CLAIM_ID], filters=filters)
    assert [n.id for n in result.nodes] == [NEW_CLAIM_ID, OLD_CLAIM_ID]


def test_traverse_skips_withdrawn_edges_until_asked_for_them():
    store = _traversal_store()
    about = _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID)
    store.append_edge(about.model_copy(update={"status": EdgeStatus.RETRACTED}))

    default = store.traverse([NEW_CLAIM_ID])
    assert ENTITY_ID not in {n.id for n in default.nodes}

    widened = store.traverse(
        [NEW_CLAIM_ID],
        filters=TraversalFilters(
            edge_statuses=[EdgeStatus.ACTIVE, EdgeStatus.RETRACTED]
        ),
    )
    assert ENTITY_ID in {n.id for n in widened.nodes}


def test_traverse_includes_weak_relations_when_asked():
    filters = TraversalFilters(include_weak_relations=True)
    result = _traversal_store().traverse([NEW_CLAIM_ID], filters=filters)
    assert POLICY_ID in {n.id for n in result.nodes}
    weak_path = next(p for p in result.paths if p.node_ids[-1] == POLICY_ID)
    assert weak_path.hops == 1
    assert result.warnings == []


def test_traverse_applies_scope_before_returning_nodes():
    """§15: scope filters run before anything is returned, and an unscoped node
    does not leak into a narrowed scope (GraphScope.matches)."""
    scoped_claim = _claim(scope=GraphScope(user_id="u1", namespace="retrieval"))
    scoped_entity = _entity().model_copy(
        update={"scope": GraphScope(user_id="u1", namespace="retrieval")}
    )
    store = InMemoryGraphStore(
        nodes=[scoped_claim, scoped_entity, _evidence()],
        edges=[
            _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID),
            _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID),
        ],
    )
    result = store.traverse(
        [NEW_CLAIM_ID],
        filters=TraversalFilters(scope=GraphScope(user_id="u1")),
    )
    assert [n.id for n in result.nodes] == [NEW_CLAIM_ID, ENTITY_ID]

    other_scope = store.traverse(
        [NEW_CLAIM_ID],
        filters=TraversalFilters(scope=GraphScope(user_id="u2")),
    )
    assert other_scope.nodes == []
    assert other_scope.warnings == [f"excluded_seed:{NEW_CLAIM_ID}"]


def test_traverse_is_a_point_in_time_query_when_as_of_is_set():
    """Validity is checked independently of status, so a point-in-time query
    over historical state is still a time query, not a status query."""
    old = _old_claim(status=NodeStatus.SUPERSEDED, valid_from=T0, valid_to=T1)
    new = _claim(valid_from=T1)
    supersedes = _typed_edge(
        RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID, valid_from=T1
    )
    store = InMemoryGraphStore(nodes=[old, new], edges=[supersedes])
    historical = TraversalFilters(
        node_statuses=[NodeStatus.ACTIVE, NodeStatus.SUPERSEDED],
        relation_types=[RelationType.SUPERSEDES],
    )

    seeds = [NEW_CLAIM_ID, OLD_CLAIM_ID]

    at_t0 = store.traverse(seeds, filters=historical.model_copy(update={"as_of": T0}))
    assert [n.id for n in at_t0.nodes] == [OLD_CLAIM_ID]
    assert at_t0.warnings == [f"excluded_seed:{NEW_CLAIM_ID}"]

    at_t1 = store.traverse(seeds, filters=historical.model_copy(update={"as_of": T1}))
    assert [n.id for n in at_t1.nodes] == [NEW_CLAIM_ID, OLD_CLAIM_ID]
    assert at_t1.warnings == []

    at_t2 = store.traverse(seeds, filters=historical.model_copy(update={"as_of": T2}))
    assert [n.id for n in at_t2.nodes] == [NEW_CLAIM_ID]
    assert at_t2.warnings == [f"excluded_seed:{OLD_CLAIM_ID}"]


def test_traverse_reports_its_budget_instead_of_silently_shrinking():
    result = _traversal_store().traverse(
        [NEW_CLAIM_ID], max_hops=2, filters=TraversalFilters(max_nodes=3)
    )
    assert len(result.nodes) == 3
    assert result.truncated is True
    assert "truncated:max_nodes" in result.warnings


def test_traverse_reports_unknown_seeds_rather_than_returning_empty():
    """§10.3: an empty traversal must not be indistinguishable from a miss."""
    result = _traversal_store().traverse(["claim_not_stored"])
    assert result.nodes == []
    assert result.edges == []
    assert result.warnings == ["unknown_seed:claim_not_stored"]


def test_traverse_reports_an_edge_pointing_at_a_missing_node():
    store = _traversal_store()
    store.append_edge(
        _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, "entity_not_stored")
    )
    result = store.traverse([NEW_CLAIM_ID])
    assert "missing_node:entity_not_stored" in result.warnings
    assert "entity_not_stored" not in {n.id for n in result.nodes}


def test_traverse_warnings_are_sorted_and_deduplicated():
    store = _traversal_store()
    store.append_edge(_typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, "entity_absent"))
    store.append_edge(
        _typed_edge(RelationType.RELATED_TO, NEW_CLAIM_ID, EXPERIENCE_ID)
    )
    result = store.traverse(["claim_not_stored", NEW_CLAIM_ID])
    assert result.warnings == sorted(result.warnings)
    assert len(result.warnings) == len(set(result.warnings))
    assert result.warnings == [
        "missing_node:entity_absent",
        "unknown_seed:claim_not_stored",
        "weak_relation_excluded:RELATED_TO",
    ]


def test_traverse_returns_copies():
    store = _traversal_store()
    result = store.traverse([NEW_CLAIM_ID])
    result.nodes[0].content = "mutated"
    assert store.get_node(NEW_CLAIM_ID).content != "mutated"


def test_traverse_rejects_a_negative_hop_budget():
    with pytest.raises(ValueError):
        _traversal_store().traverse([NEW_CLAIM_ID], max_hops=-1)


# --- G. get_provenance -------------------------------------------------------

def test_provenance_collects_refs_edges_and_derivation():
    record = _traversal_store().get_provenance(NEW_CLAIM_ID)
    assert record.node_id == NEW_CLAIM_ID
    assert record.node_version == 1
    assert record.source_refs[0].source_section == "Index maintenance"
    assert record.evidence_ids == [EVIDENCE_ID]
    assert record.episode_ids == [EPISODE_ID]
    assert record.derived_from_ids == []
    assert record.warnings == []
    assert record.resolved is True


def test_provenance_follows_derived_from_edges():
    record = _traversal_store().get_provenance(EXPERIENCE_ID)
    assert record.derived_from_ids == [NEW_CLAIM_ID]
    assert record.evidence_ids == [EVIDENCE_ID]
    assert record.derivation.method is DerivationMethod.SUMMARIZED
    assert record.resolved is True


def test_provenance_does_not_double_count_an_edge_and_a_source_ref():
    """The EVIDENCED_BY edge and the source ref name the same evidence node."""
    record = _traversal_store().get_provenance(NEW_CLAIM_ID)
    assert record.evidence_ids == sorted(set(record.evidence_ids))
    assert len(record.evidence_ids) == 1


def test_provenance_reports_an_unresolvable_reference_instead_of_inventing_one():
    """§10.2: unresolvable references are reported, never dropped."""
    orphan = _claim(
        id="claim_orphan_refs",
        source_refs=[
            SourceRef(episode_id="episode_absent", evidence_id="evidence_absent")
        ],
    )
    store = InMemoryGraphStore(nodes=[orphan])
    record = store.get_provenance("claim_orphan_refs")
    assert record.evidence_ids == ["evidence_absent"]
    assert record.episode_ids == ["episode_absent"]
    assert record.warnings == [
        "unresolved_episode:episode_absent",
        "unresolved_evidence:evidence_absent",
    ]
    assert record.resolved is False


def test_provenance_reports_a_reference_of_the_wrong_kind():
    """An importer wiring a claim as its own evidence is ambiguous, not fine."""
    store = InMemoryGraphStore(
        nodes=[_claim(), _old_claim()],
        edges=[_typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, OLD_CLAIM_ID)],
    )
    record = store.get_provenance(NEW_CLAIM_ID)
    assert OLD_CLAIM_ID in record.evidence_ids
    assert f"unexpected_evidence_kind:{OLD_CLAIM_ID}:claim" in record.warnings
    assert record.resolved is False


def test_provenance_reports_a_withdrawn_evidence_link():
    store = _traversal_store()
    evidenced = _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID)
    store.append_edge(evidenced.model_copy(update={"status": EdgeStatus.RETRACTED}))
    record = store.get_provenance(NEW_CLAIM_ID)
    assert f"withdrawn_evidence:{EVIDENCE_ID}" in record.warnings
    assert record.resolved is False
    # The source ref still names it, so the reference itself is not lost.
    assert record.source_refs[0].evidence_id == EVIDENCE_ID


def test_provenance_of_an_unknown_node_raises_rather_than_answering_blankly():
    with pytest.raises(UnknownNodeError) as excinfo:
        _traversal_store().get_provenance("claim_not_stored")
    assert "claim_not_stored" in str(excinfo.value)


def test_provenance_output_is_independent_of_insertion_order():
    forward = _traversal_store()
    backward = InMemoryGraphStore(
        nodes=list(reversed(_traversal_nodes())),
        edges=list(reversed(_traversal_edges())),
    )
    assert (
        forward.get_provenance(NEW_CLAIM_ID).to_json()
        == backward.get_provenance(NEW_CLAIM_ID).to_json()
    )


# --- H. find_conflicts -------------------------------------------------------

CONFLICT_GROUP = "conflict_index_cadence"


def _contradiction_store(**claim_overrides) -> InMemoryGraphStore:
    """Two live claims about one cadence, explicitly marked as contradicting."""
    return InMemoryGraphStore(
        nodes=[
            _claim(conflict_group=CONFLICT_GROUP, **claim_overrides),
            _old_claim(conflict_group=CONFLICT_GROUP),
            _entity(),
        ],
        edges=[
            _typed_edge(RelationType.CONTRADICTS, NEW_CLAIM_ID, OLD_CLAIM_ID),
            _typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID),
            _typed_edge(RelationType.ABOUT, OLD_CLAIM_ID, ENTITY_ID),
        ],
    )


def test_an_active_contradiction_is_reported_unresolved():
    """§8.3: a contradiction is never settled by whichever node was written
    last, so both sides and their link come back."""
    reports = _contradiction_store().find_conflicts()
    assert len(reports) == 1
    report = reports[0]
    assert report.node_ids == sorted([NEW_CLAIM_ID, OLD_CLAIM_ID])
    assert report.relation_type is RelationType.CONTRADICTS
    assert report.conflict_group == CONFLICT_GROUP
    assert report.resolved is False
    assert report.edge_ids == [
        make_edge_id(RelationType.CONTRADICTS, NEW_CLAIM_ID, OLD_CLAIM_ID)
    ]
    assert "unresolved=" in report.detail


def test_a_shared_conflict_group_is_a_conflict_without_an_edge():
    """An importer may only know that two claims were grouped (§8.3)."""
    store = InMemoryGraphStore(
        nodes=[
            _claim(conflict_group=CONFLICT_GROUP),
            _old_claim(conflict_group=CONFLICT_GROUP),
        ]
    )
    reports = store.find_conflicts()
    assert [r.node_ids for r in reports] == [sorted([NEW_CLAIM_ID, OLD_CLAIM_ID])]
    assert reports[0].edge_ids == []


def test_an_edge_and_a_group_describing_one_conflict_are_one_report():
    assert len(_contradiction_store().find_conflicts()) == 1


def test_three_grouped_claims_are_one_conflict_with_three_sides():
    third = _claim(
        id="claim_index_rebuild_hourly",
        content="The retrieval index rebuild runs hourly.",
        conflict_group=CONFLICT_GROUP,
    )
    store = _contradiction_store()
    store.create_node(third)
    reports = store.find_conflicts()
    assert len(reports) == 1
    assert reports[0].node_ids == sorted(
        [NEW_CLAIM_ID, OLD_CLAIM_ID, "claim_index_rebuild_hourly"]
    )


def test_a_conflict_whose_loser_is_superseded_is_not_reported():
    store = InMemoryGraphStore(
        nodes=[
            _claim(conflict_group=CONFLICT_GROUP),
            _old_claim(conflict_group=CONFLICT_GROUP, status=NodeStatus.SUPERSEDED),
        ],
        edges=[_typed_edge(RelationType.CONTRADICTS, NEW_CLAIM_ID, OLD_CLAIM_ID)],
    )
    assert store.find_conflicts() == []


def test_an_active_supersedes_edge_settles_a_conflict_even_if_status_lags():
    """§8.3: a state transition backed by stronger evidence resolves the
    conflict; the lagging status is a projection problem, not a contradiction."""
    store = _contradiction_store()
    store.append_edge(
        _typed_edge(RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID)
    )
    assert store.find_conflicts() == []


def test_a_withdrawn_contradiction_edge_no_longer_conflicts():
    store = InMemoryGraphStore(
        nodes=[_claim(), _old_claim()],
        edges=[
            _typed_edge(
                RelationType.CONTRADICTS,
                NEW_CLAIM_ID,
                OLD_CLAIM_ID,
                status=EdgeStatus.RETRACTED,
            )
        ],
    )
    assert store.find_conflicts() == []


def test_a_pending_side_does_not_contest_a_current_claim():
    store = InMemoryGraphStore(
        nodes=[
            _claim(conflict_group=CONFLICT_GROUP),
            _old_claim(conflict_group=CONFLICT_GROUP, status=NodeStatus.PENDING),
        ]
    )
    assert store.find_conflicts() == []


def test_a_contradiction_pointing_at_a_missing_node_is_still_reported():
    """An unknown side cannot be assumed settled (§10.2)."""
    store = InMemoryGraphStore(
        nodes=[_claim()],
        edges=[
            _typed_edge(RelationType.CONTRADICTS, NEW_CLAIM_ID, "claim_not_stored")
        ],
    )
    reports = store.find_conflicts()
    assert len(reports) == 1
    assert reports[0].node_ids == sorted([NEW_CLAIM_ID, "claim_not_stored"])
    assert "missing_node:claim_not_stored" in reports[0].detail
    assert reports[0].resolved is False


def test_a_contradiction_between_two_missing_nodes_is_still_reported():
    """Nothing is known about either side, which is the opposite of settled."""
    store = InMemoryGraphStore(
        nodes=[_claim()],
        edges=[
            GraphEdge(
                id="edge_absent_contradiction",
                relation_type=RelationType.CONTRADICTS,
                source_id="claim_absent_a",
                target_id="claim_absent_b",
                recorded_at=T0,
                evidence_refs=[_source_ref()],
            )
        ],
    )
    reports = store.find_conflicts()
    assert [r.node_ids for r in reports] == [["claim_absent_a", "claim_absent_b"]]
    assert reports[0].scope is None
    assert reports[0].conflict_group is None
    assert reports[0].detail.endswith(
        "missing_node:claim_absent_a; missing_node:claim_absent_b"
    )


def test_find_conflicts_narrows_by_scope():
    scope = GraphScope(user_id="u1", namespace="retrieval")
    store = _contradiction_store(scope=scope)
    assert store.find_conflicts(scope=GraphScope(user_id="u1"))
    assert store.find_conflicts(scope=GraphScope(user_id="u2")) == []
    reports = store.find_conflicts(scope=GraphScope(user_id="u1"))
    # Only one side carries the scope, so the report claims no single scope.
    assert reports[0].scope is None


def test_find_conflicts_reports_a_single_shared_scope():
    scope = GraphScope(user_id="u1", namespace="retrieval")
    store = InMemoryGraphStore(
        nodes=[
            _claim(conflict_group=CONFLICT_GROUP, scope=scope),
            _old_claim(conflict_group=CONFLICT_GROUP, scope=scope),
        ]
    )
    assert store.find_conflicts()[0].scope == scope


def test_find_conflicts_narrows_by_entity():
    store = _contradiction_store()
    assert store.find_conflicts(entity_ids=[ENTITY_ID])
    assert store.find_conflicts(entity_ids=["entity_scheduler"]) == []
    assert store.find_conflicts(entity_ids=[NEW_CLAIM_ID])


def test_find_conflicts_is_empty_on_a_graph_without_contradictions():
    assert _traversal_store().find_conflicts() == []
    assert InMemoryGraphStore().find_conflicts() == []


def test_conflict_reports_are_sorted_and_insertion_order_independent():
    other_group = "conflict_index_scope"
    extra = [
        _claim(
            id="claim_index_covers_sessions",
            content="The retrieval index covers session transcripts.",
            conflict_group=other_group,
        ),
        _claim(
            id="claim_index_skips_sessions",
            content="The retrieval index skips session transcripts.",
            conflict_group=other_group,
        ),
    ]
    forward = _contradiction_store()
    for node in extra:
        forward.create_node(node)
    backward = InMemoryGraphStore(
        nodes=list(reversed(extra)) + [_old_claim(conflict_group=CONFLICT_GROUP),
                                       _claim(conflict_group=CONFLICT_GROUP)],
        edges=[_typed_edge(RelationType.CONTRADICTS, NEW_CLAIM_ID, OLD_CLAIM_ID)],
    )
    forward_reports = forward.find_conflicts()
    assert [r.node_ids for r in forward_reports] == sorted(
        r.node_ids for r in forward_reports
    )
    assert [r.to_json() for r in forward_reports] == [
        r.to_json() for r in backward.find_conflicts()
    ]


# --- I. rebuild --------------------------------------------------------------

def _event(kind: MemoryEventKind, event_id: str, at: datetime, **kwargs) -> MemoryEvent:
    return MemoryEvent(id=event_id, kind=kind, occurred_at=at, **kwargs)


def _ledger() -> list[MemoryEvent]:
    """A small append-only ledger: three sources, a pending claim that gets
    approved, and a revision that supersedes it."""
    return [
        _event(MemoryEventKind.INJECT, "event_0001", T0, node=_episode()),
        _event(MemoryEventKind.INJECT, "event_0002", T0, node=_evidence()),
        _event(MemoryEventKind.INJECT, "event_0003", T0, node=_entity()),
        _event(
            MemoryEventKind.INJECT,
            "event_0004",
            T0,
            node=_old_claim(status=NodeStatus.PENDING),
        ),
        _event(
            MemoryEventKind.APPROVE,
            "event_0005",
            T0 + timedelta(hours=1),
            target_id=OLD_CLAIM_ID,
        ),
        _event(
            MemoryEventKind.INJECT,
            "event_0006",
            T0 + timedelta(hours=1),
            edge=_typed_edge(RelationType.EVIDENCED_BY, OLD_CLAIM_ID, EVIDENCE_ID),
        ),
        _event(MemoryEventKind.INJECT, "event_0007", T1, node=_claim()),
        _event(
            MemoryEventKind.INJECT,
            "event_0008",
            T1,
            edge=_typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID),
        ),
        _event(
            MemoryEventKind.SUPERSEDE,
            "event_0009",
            T1 + timedelta(hours=1),
            target_id=OLD_CLAIM_ID,
            edge=_typed_edge(RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID),
        ),
    ]


def test_rebuild_projects_the_whole_graph_from_the_event_stream():
    store = InMemoryGraphStore()
    report = store.rebuild(_ledger())
    assert report.events_read == 9
    assert report.nodes_written == 5
    assert report.edges_written == 3
    assert report.skipped == 0
    assert report.errors == []
    assert report.warnings == []
    assert report.ok is True
    assert store.get_node(OLD_CLAIM_ID).status is NodeStatus.SUPERSEDED
    assert store.get_node(NEW_CLAIM_ID).status is NodeStatus.ACTIVE
    assert store.health().healthy is True


def test_rebuild_is_idempotent():
    """§10.2: the same stream must project to the same graph, every time."""
    store = InMemoryGraphStore()
    first = store.rebuild(_ledger())
    snapshot = store.snapshot()
    second = store.rebuild(_ledger())
    assert store.snapshot() == snapshot
    assert second.to_json() == first.to_json()


def test_rebuild_is_independent_of_the_order_events_arrive_in():
    """Events carry `occurred_at`, so a reordered export is the same ledger."""
    straight = InMemoryGraphStore()
    straight.rebuild(_ledger())
    shuffled = InMemoryGraphStore()
    stream = _ledger()
    stream = stream[4:] + stream[:4][::-1]
    shuffled.rebuild(stream)
    assert shuffled.snapshot() == straight.snapshot()


def test_rebuild_replaces_state_rather_than_merging_into_it():
    store = InMemoryGraphStore(nodes=[_policy()])
    store.rebuild(_ledger())
    assert store.get_node(POLICY_ID) is None
    assert store.health().node_count == 5


def test_rebuilding_from_an_empty_stream_empties_the_graph():
    store = InMemoryGraphStore(nodes=_traversal_nodes(), edges=_traversal_edges())
    report = store.rebuild([])
    assert report.ok is True
    assert (report.nodes_written, report.edges_written, report.events_read) == (0, 0, 0)
    assert store.snapshot()["nodes"] == []


def test_rebuild_reports_a_duplicate_write_of_one_node():
    stream = _ledger() + [
        _event(MemoryEventKind.MIGRATE, "event_0010", T2, node=_claim())
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert report.warnings == [f"duplicate_node:{NEW_CLAIM_ID}"]
    assert report.skipped == 1
    assert report.nodes_written == 5
    assert report.ok is True


def test_rebuild_reports_a_duplicate_write_of_one_edge():
    stream = _ledger() + [
        _event(
            MemoryEventKind.MIGRATE,
            "event_0010",
            T2,
            edge=_typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID),
        )
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert report.warnings == [
        "duplicate_edge:"
        + make_edge_id(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID)
    ]
    assert report.skipped == 1


def test_rebuild_reports_two_events_writing_one_node_id_differently():
    """§10.2: ambiguity is reported, not quietly settled. The later event in
    ledger order wins, so the outcome is still deterministic."""
    conflicting = _claim(content="The retrieval index rebuild runs hourly.")
    stream = _ledger() + [
        _event(MemoryEventKind.MIGRATE, "event_0010", T2, node=conflicting)
    ]
    store = InMemoryGraphStore()
    report = store.rebuild(stream)
    assert report.errors == [f"conflicting_node:{NEW_CLAIM_ID}"]
    assert report.ok is False
    assert store.get_node(NEW_CLAIM_ID).content == (
        "The retrieval index rebuild runs hourly."
    )


def test_rebuild_reports_two_events_asserting_one_edge_id_differently():
    stream = _ledger() + [
        _event(
            MemoryEventKind.MIGRATE,
            "event_0010",
            T2,
            edge=_typed_edge(
                RelationType.EVIDENCED_BY,
                NEW_CLAIM_ID,
                EVIDENCE_ID,
                status=EdgeStatus.RETRACTED,
            ),
        )
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert report.errors == [
        "conflicting_edge:"
        + make_edge_id(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID)
    ]


def test_rebuild_never_raises_on_a_conflicting_stream():
    """Unlike `append_edge`, a rebuild reports: it is handed a whole ledger it
    did not author, and refusing the lot would lose the good events too."""
    stream = [
        _event(MemoryEventKind.INJECT, "event_0001", T0, node=_evidence()),
        _event(MemoryEventKind.INJECT, "event_0002", T0, node=_claim()),
        _event(
            MemoryEventKind.INJECT,
            "event_0003",
            T0,
            edge=_typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID),
        ),
        _event(
            MemoryEventKind.INJECT,
            "event_0004",
            T1,
            edge=_typed_edge(
                RelationType.EVIDENCED_BY,
                NEW_CLAIM_ID,
                EVIDENCE_ID,
                status=EdgeStatus.RETRACTED,
            ),
        ),
    ]
    store = InMemoryGraphStore()
    report = store.rebuild(stream)
    assert report.ok is False
    assert store.health().node_count == 2


def test_rebuild_reports_a_repeated_event_id():
    stream = _ledger()
    stream.append(stream[0].model_copy(deep=True))
    report = InMemoryGraphStore().rebuild(stream)
    assert report.warnings == ["duplicate_event:event_0001"]
    assert report.events_read == 10
    assert report.skipped == 1


def test_rebuild_reports_one_event_id_carrying_two_different_payloads():
    stream = _ledger()
    stream.append(
        _event(MemoryEventKind.INJECT, "event_0001", T0, node=_policy())
    )
    report = InMemoryGraphStore().rebuild(stream)
    assert "conflicting_event:event_0001" in report.errors
    assert report.ok is False


def test_rebuild_reports_an_edge_whose_endpoint_was_never_projected():
    stream = _ledger() + [
        _event(
            MemoryEventKind.INJECT,
            "event_0010",
            T2,
            edge=_typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, "entity_not_projected"),
        )
    ]
    store = InMemoryGraphStore()
    report = store.rebuild(stream)
    edge_id = make_edge_id(RelationType.ABOUT, NEW_CLAIM_ID, "entity_not_projected")
    assert report.errors == [
        f"unresolved_edge_endpoint:{edge_id}:entity_not_projected"
    ]
    assert report.ok is False
    # Kept, not dropped: dropping it would hide the broken reference.
    assert report.edges_written == 4
    health = store.health()
    assert health.healthy is False
    assert f"dangling_edge:{edge_id}:entity_not_projected" in health.errors


def test_rebuild_warns_about_a_source_reference_it_cannot_resolve():
    stream = [
        _event(
            MemoryEventKind.MIGRATE,
            "event_0001",
            T0,
            node=_claim(source_refs=[SourceRef(episode_id="episode_absent")]),
        )
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert report.warnings == [
        f"unresolved_source_ref:{NEW_CLAIM_ID}:episode_absent"
    ]
    assert report.ok is True


def test_rebuild_applies_the_lifecycle_transitions():
    stream = [
        _event(
            MemoryEventKind.INJECT,
            "event_0001",
            T0,
            node=_claim(status=NodeStatus.PENDING),
        ),
        _event(MemoryEventKind.APPROVE, "event_0002", T1, target_id=NEW_CLAIM_ID),
        _event(MemoryEventKind.RETRACT, "event_0003", T2, target_id=NEW_CLAIM_ID),
    ]
    store = InMemoryGraphStore()
    report = store.rebuild(stream)
    assert store.get_node(NEW_CLAIM_ID).status is NodeStatus.RETRACTED
    assert report.ok is True

    restored = store.rebuild(
        stream
        + [
            _event(
                MemoryEventKind.RESTORE,
                "event_0004",
                T2 + timedelta(hours=1),
                target_id=NEW_CLAIM_ID,
            )
        ]
    )
    assert store.get_node(NEW_CLAIM_ID).status is NodeStatus.ACTIVE
    assert restored.ok is True


def test_rebuild_can_retract_an_edge_through_an_event():
    edge = _typed_edge(RelationType.EVIDENCED_BY, NEW_CLAIM_ID, EVIDENCE_ID)
    stream = _ledger() + [
        _event(MemoryEventKind.RETRACT, "event_0010", T2, target_id=edge.id)
    ]
    store = InMemoryGraphStore()
    report = store.rebuild(stream)
    assert report.ok is True
    assert store.get_edges(NEW_CLAIM_ID, relation_types=[RelationType.EVIDENCED_BY])[
        0
    ].status is EdgeStatus.RETRACTED


def test_rebuild_reports_a_transition_whose_target_does_not_exist():
    stream = _ledger() + [
        _event(
            MemoryEventKind.RETRACT, "event_0010", T2, target_id="claim_not_projected"
        )
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert report.errors == ["unknown_target:event_0010:claim_not_projected"]
    assert report.skipped == 1


def test_rebuild_reports_a_transition_that_does_not_apply():
    """An approve of something that was never pending changes nothing, and
    saying so is the difference between a replayed ledger and a wrong one."""
    stream = _ledger() + [
        _event(MemoryEventKind.APPROVE, "event_0010", T2, target_id=NEW_CLAIM_ID)
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert report.warnings == [
        f"ignored_transition:approve:{NEW_CLAIM_ID}:active"
    ]
    assert report.skipped == 1
    assert report.ok is True


def test_rebuild_reports_a_transition_event_without_a_target():
    stream = [
        _event(MemoryEventKind.INJECT, "event_0001", T0, node=_claim()),
        _event(MemoryEventKind.APPROVE, "event_0002", T1, node=_claim()),
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert "missing_target:event_0002:approve" in report.errors


def test_rebuild_reports_an_event_with_nothing_to_project():
    stream = [
        _event(MemoryEventKind.INJECT, "event_0001", T0, node=_claim()),
        _event(MemoryEventKind.INJECT, "event_0002", T1, target_id=NEW_CLAIM_ID),
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert "nothing_to_project:event_0002:inject" in report.warnings
    assert report.skipped == 1
    assert report.ok is True


def test_rebuild_reports_are_sorted_and_deduplicated():
    stream = _ledger() + [
        _event(MemoryEventKind.MIGRATE, "event_0010", T2, node=_claim()),
        _event(MemoryEventKind.MIGRATE, "event_0011", T2, node=_claim()),
        _event(
            MemoryEventKind.RETRACT, "event_0012", T2, target_id="claim_not_projected"
        ),
        _event(
            MemoryEventKind.APPROVE, "event_0013", T2, target_id="claim_not_projected"
        ),
    ]
    report = InMemoryGraphStore().rebuild(stream)
    assert report.warnings == sorted(report.warnings)
    assert report.errors == sorted(report.errors)
    assert len(report.warnings) == len(set(report.warnings))
    assert report.warnings == [f"duplicate_node:{NEW_CLAIM_ID}"]
    assert report.errors == [
        "unknown_target:event_0012:claim_not_projected",
        "unknown_target:event_0013:claim_not_projected",
    ]


def test_rebuild_does_not_keep_a_reference_to_the_caller_s_events():
    stream = _ledger()
    store = InMemoryGraphStore()
    store.rebuild(stream)
    stream[6].node.content = "mutated after the rebuild"
    assert store.get_node(NEW_CLAIM_ID).content == (
        "The retrieval index rebuild runs nightly."
    )


# --- J. health ---------------------------------------------------------------

def test_health_counts_a_populated_graph():
    store = InMemoryGraphStore(nodes=_traversal_nodes(), edges=_traversal_edges())
    health = store.health()
    assert (health.node_count, health.edge_count) == (7, 10)
    assert health.healthy is True
    assert health.backend == FIXTURE_GRAPH_BACKEND


def test_health_reports_a_dangling_edge_rather_than_a_green_light():
    store = InMemoryGraphStore(nodes=[_claim()])
    store.append_edge(_typed_edge(RelationType.ABOUT, NEW_CLAIM_ID, "entity_absent"))
    health = store.health()
    assert health.healthy is False
    assert any(e.startswith("dangling_edge:") for e in health.errors)
    assert health.errors == sorted(health.errors)


def test_health_reports_a_foreign_schema_version():
    """§10.3: a backend must not report a green light and a broken schema."""
    store = InMemoryGraphStore(nodes=[_claim(schema_version=GRAPH_SCHEMA_VERSION + 1)])
    health = store.health()
    assert health.healthy is False
    assert health.errors == [
        f"schema_version_mismatch:{NEW_CLAIM_ID}:{GRAPH_SCHEMA_VERSION + 1}"
    ]


def test_health_leaks_no_python_objects():
    store = InMemoryGraphStore(nodes=_traversal_nodes(), edges=_traversal_edges())
    payload = store.health().to_dict()
    assert all(not isinstance(v, datetime) for v in payload.values())
    assert payload["backend"] == FIXTURE_GRAPH_BACKEND

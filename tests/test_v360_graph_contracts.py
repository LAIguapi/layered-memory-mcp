"""v3.6.0 Batch B — backend-neutral memory graph domain contracts.

Why this file exists: ARCHITECTURE_v2.md §6 declares a typed, evidence-backed
temporal memory graph, and §9.1 declares a small vendor-independent
``GraphStore`` contract. Phase 1 ("semantic foundation") must land that
vocabulary *before* any importer, backend or graph-aware recall exists, so the
later batches have something to agree on.

These tests pin the properties that make the contract worth having:

  * **The vocabulary is complete.** Every node kind in §6.1 and every relation
    type in §6.2 exists, with the spelling the architecture uses.
  * **State is well formed.** Confidence stays in [0,1], a validity interval
    cannot run backwards, identifiers are non-empty and single-line, and an
    edge cannot point a node at itself.
  * **Remembered claims carry provenance.** A Claim/Experience/Policy/Procedure
    needs source refs or an explicit derivation record (§3.1 goal 4); so does a
    strong assertion edge (SUPPORTS/CONTRADICTS/SUPERSEDES). An Entity is an
    identity node and stays cheap to create.
  * **Serialization is deterministic and dumb.** ``to_dict`` leaks no Enum and
    no ``datetime`` object, and two equal models serialize byte-identically —
    which is what later fixture/rebuild assertions rest on.
  * **Timestamps stay timezone-aware.** An offset that was given is preserved;
    a naive one is pinned to UTC rather than left ambiguous.
  * **The store contract is checkable.** All nine §9.1 operations are declared,
    an implementation that omits one is detectable, and the result shapes are
    usable without a backend.

Nothing here touches the hot path, a persistent backend, or real memory data;
fixture data is neutral technical vocabulary.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from enum import Enum

import pytest
from pydantic import ValidationError

from layered_memory_mcp.graph_models import (
    CURRENT_NODE_STATUSES,
    GRAPH_SCHEMA_VERSION,
    HISTORICAL_NODE_STATUSES,
    PROVENANCE_REQUIRED_NODE_KINDS,
    PROVENANCE_REQUIRED_RELATIONS,
    WEAK_RELATIONS,
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
    ensure_aware,
    make_edge_id,
    make_node_id,
)
from layered_memory_mcp.graph_store import (
    REQUIRED_GRAPH_STORE_OPERATIONS,
    ConflictReport,
    GraphHealth,
    GraphPath,
    GraphStore,
    ProvenanceRecord,
    RebuildReport,
    TraversalFilters,
    TraversalResult,
    missing_graph_store_operations,
)

# --- neutral fixture vocabulary ---------------------------------------------

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)

EPISODE_ID = "episode_sess_0001"
EVIDENCE_ID = "evidence_sess_0001_0003"
ENTITY_ID = "entity_retrieval_index"
CLAIM_ID = "claim_index_rebuild_nightly"
OLD_CLAIM_ID = "claim_index_rebuild_weekly"


def _source_ref(**kwargs) -> SourceRef:
    data = {
        "episode_id": EPISODE_ID,
        "evidence_id": EVIDENCE_ID,
        "source_file": "knowledge/retrieval.md",
        "source_section": "Index maintenance",
        "quote_hash": "sha256:0f1e2d3c",
    }
    data.update(kwargs)
    return SourceRef(**data)


def _derivation(**kwargs) -> Derivation:
    data = {"method": DerivationMethod.USER_EXPLICIT, "generated_at": T0}
    data.update(kwargs)
    return Derivation(**data)


def _node(**kwargs) -> GraphNode:
    data = {
        "id": CLAIM_ID,
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


def _edge(**kwargs) -> GraphEdge:
    data = {
        "id": "edge_claim_evidenced",
        "relation_type": RelationType.EVIDENCED_BY,
        "source_id": CLAIM_ID,
        "target_id": EVIDENCE_ID,
        "recorded_at": T0,
    }
    data.update(kwargs)
    return GraphEdge(**data)


def _enum_or_datetime_leaks(value) -> list[str]:
    """Paths inside a serialized payload that still hold a Python object."""
    leaks: list[str] = []

    def _walk(obj, path: str) -> None:
        if isinstance(obj, (Enum, datetime)):
            leaks.append(f"{path}={type(obj).__name__}")
        elif isinstance(obj, dict):
            for key, item in obj.items():
                if isinstance(key, (Enum, datetime)):
                    leaks.append(f"{path}.<key>={type(key).__name__}")
                _walk(item, f"{path}.{key}")
        elif isinstance(obj, (list, tuple)):
            for index, item in enumerate(obj):
                _walk(item, f"{path}[{index}]")

    _walk(value, "$")
    return leaks


# --- A. vocabulary -----------------------------------------------------------

def test_node_kinds_cover_architecture_table():
    assert {k.value for k in NodeKind} == {
        "episode",
        "evidence",
        "entity",
        "claim",
        "event",
        "experience",
        "procedure",
        "policy",
        "task",
        "community",
    }


def test_relation_types_cover_architecture_table():
    assert {r.value for r in RelationType} >= {
        "EVIDENCED_BY",
        "DERIVED_FROM",
        "ABOUT",
        "MENTIONS",
        "SUPPORTS",
        "CONTRADICTS",
        "SUPERSEDES",
        "APPLIES_TO",
        "IMPLEMENTS",
        "OBSERVED_IN",
        "BLOCKS",
        "RELATED_TO",
        "PART_OF",
    }


def test_lifecycle_and_memory_class_vocabulary():
    assert {s.value for s in NodeStatus} == {
        "active",
        "pending",
        "superseded",
        "retracted",
        "expired",
        "archived",
    }
    assert {s.value for s in EdgeStatus} == {"active", "retracted", "expired"}
    assert {c.value for c in MemoryClass} == {
        "fact",
        "observation",
        "experience",
        "procedure",
        "belief",
        "policy",
    }
    assert {m.value for m in DerivationMethod} == {
        "user_explicit",
        "extracted",
        "inferred",
        "summarized",
        "migrated",
    }


def test_status_partitions_are_disjoint_and_exhaustive():
    """A superseded claim must not be injectable as current state (§8.2)."""
    assert NodeStatus.ACTIVE in CURRENT_NODE_STATUSES
    assert set(CURRENT_NODE_STATUSES).isdisjoint(HISTORICAL_NODE_STATUSES)
    for historical in ("superseded", "retracted", "expired", "archived"):
        assert NodeStatus(historical) in HISTORICAL_NODE_STATUSES


def test_weak_relation_is_only_related_to():
    """§6.2: RELATED_TO is intentionally weak; the strong ones are not."""
    assert set(WEAK_RELATIONS) == {RelationType.RELATED_TO}
    assert set(PROVENANCE_REQUIRED_RELATIONS).isdisjoint(WEAK_RELATIONS)
    assert set(PROVENANCE_REQUIRED_RELATIONS) >= {
        RelationType.SUPPORTS,
        RelationType.CONTRADICTS,
        RelationType.SUPERSEDES,
    }


def test_unknown_kinds_are_rejected():
    with pytest.raises(ValidationError):
        _node(kind="belief_node")
    with pytest.raises(ValidationError):
        _edge(relation_type="SORT_OF_LIKE")
    with pytest.raises(ValidationError):
        _node(status="deleted")
    with pytest.raises(ValidationError):
        _edge(status="superseded")  # an edge has no superseded state


# --- B. valid construction and serialization ---------------------------------

def test_node_defaults_are_conservative():
    node = GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, label="retrieval index")
    assert node.status is NodeStatus.ACTIVE
    assert node.version == 1
    assert node.confidence == pytest.approx(0.5)
    assert node.valid_from is None and node.valid_to is None
    assert node.recorded_at.tzinfo is not None
    assert node.source_refs == []
    assert node.derivation is None
    assert node.schema_version == GRAPH_SCHEMA_VERSION


def test_node_serialization_leaks_no_python_objects():
    node = _node(
        scope=GraphScope(namespace="retrieval"),
        valid_from=T0,
        valid_to=T1,
        derivation=_derivation(),
        conflict_group="conflict_index_cadence",
        tags=["retrieval", "index"],
        attributes={"section_count": 2},
    )
    payload = node.to_dict()

    assert _enum_or_datetime_leaks(payload) == []
    assert payload["kind"] == "claim"
    assert payload["status"] == "active"
    assert payload["memory_class"] == "fact"
    assert payload["derivation"]["method"] == "user_explicit"
    assert payload["valid_from"].startswith("2026-01-05T09:00:00")
    assert payload["source_refs"][0]["evidence_id"] == EVIDENCE_ID
    assert json.loads(node.to_json()) == payload


def test_edge_serialization_leaks_no_python_objects():
    edge = _edge(
        relation_type=RelationType.SUPERSEDES,
        source_id=CLAIM_ID,
        target_id=OLD_CLAIM_ID,
        confidence=1.0,
        valid_from=T0,
        evidence_refs=[_source_ref()],
    )
    payload = edge.to_dict()

    assert _enum_or_datetime_leaks(payload) == []
    assert payload["relation_type"] == "SUPERSEDES"
    assert payload["status"] == "active"
    assert payload["source_id"] == CLAIM_ID
    assert payload["target_id"] == OLD_CLAIM_ID
    assert payload["evidence_refs"][0]["episode_id"] == EPISODE_ID


def test_serialization_is_deterministic_for_equal_models():
    first = _node(tags=["index", "retrieval"], attributes={"b": 1, "a": 2})
    second = _node(tags=["index", "retrieval"], attributes={"a": 2, "b": 1})
    assert first.to_json() == second.to_json()
    # Stable across repeated calls, too — fixture assertions depend on it.
    assert first.to_json() == first.to_json()


def test_node_json_round_trips_through_the_model():
    node = _node(valid_from=T0, valid_to=T1, derivation=_derivation())
    restored = GraphNode(**json.loads(node.to_json()))
    assert restored == node


def test_unknown_fields_are_rejected_rather_than_silently_kept():
    with pytest.raises(ValidationError):
        _node(kinda="claim")
    with pytest.raises(ValidationError):
        _edge(relation="SUPPORTS")


# --- C. confidence -----------------------------------------------------------

@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0, -1.0])
def test_confidence_outside_unit_interval_is_rejected(bad):
    with pytest.raises(ValidationError):
        _node(confidence=bad)
    with pytest.raises(ValidationError):
        _edge(confidence=bad)


@pytest.mark.parametrize("good", [0.0, 0.5, 1.0])
def test_confidence_bounds_are_inclusive(good):
    assert _node(confidence=good).confidence == pytest.approx(good)
    assert _edge(confidence=good).confidence == pytest.approx(good)


# --- D. temporal validity ----------------------------------------------------

def test_valid_to_before_valid_from_is_rejected():
    with pytest.raises(ValidationError):
        _node(valid_from=T1, valid_to=T0)
    with pytest.raises(ValidationError):
        _edge(valid_from=T1, valid_to=T0)


def test_zero_length_and_open_ended_intervals_are_allowed():
    assert _node(valid_from=T0, valid_to=T0).valid_to == T0
    assert _node(valid_from=T0).valid_to is None
    assert _node(valid_to=T1).valid_from is None


def test_naive_timestamps_are_pinned_to_utc_and_offsets_preserved():
    naive = datetime(2026, 1, 5, 9, 0, 0)
    node = _node(recorded_at=naive, valid_from=naive)
    assert node.recorded_at.tzinfo is not None
    assert node.recorded_at == T0
    assert node.valid_from == T0

    offset = timezone(timedelta(hours=2))
    aware = datetime(2026, 1, 5, 11, 0, 0, tzinfo=offset)
    kept = _node(recorded_at=aware)
    assert kept.recorded_at.utcoffset() == timedelta(hours=2)
    assert kept.to_dict()["recorded_at"].endswith("+02:00")


def test_ensure_aware_helper():
    assert ensure_aware(None) is None
    assert ensure_aware(datetime(2026, 1, 5, 9, 0, 0)) == T0
    assert ensure_aware(T0) is T0


def test_node_validity_and_currency_checks():
    node = _node(valid_from=T0, valid_to=T1)
    assert node.is_valid_at(T0) is True
    assert node.is_valid_at(T1) is True
    assert node.is_valid_at(T0 - timedelta(days=1)) is False
    assert node.is_valid_at(T1 + timedelta(days=1)) is False
    assert node.is_current(T0) is True

    superseded = _node(valid_from=T0, valid_to=T1, status=NodeStatus.SUPERSEDED)
    assert superseded.is_valid_at(T0) is True
    assert superseded.is_current(T0) is False


def test_edge_activity_check():
    edge = _edge(valid_from=T0, valid_to=T1)
    assert edge.is_active_at(T0) is True
    assert edge.is_active_at(T1 + timedelta(seconds=1)) is False
    assert _edge(status=EdgeStatus.RETRACTED).is_active_at(T0) is False


# --- E. identifiers ----------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "\n", "\t "])
def test_empty_node_id_is_rejected(bad):
    with pytest.raises(ValidationError):
        _node(id=bad)


@pytest.mark.parametrize("field", ["id", "source_id", "target_id"])
def test_empty_edge_identifiers_are_rejected(field):
    with pytest.raises(ValidationError):
        _edge(**{field: "  "})


@pytest.mark.parametrize(
    "bad", ["claim\nid", "claim\rid", "claim\r\nid", "claim id\n"]
)
def test_newlines_in_identifiers_are_rejected(bad):
    with pytest.raises(ValidationError):
        _node(id=bad)
    with pytest.raises(ValidationError):
        _edge(source_id=bad)
    with pytest.raises(ValidationError):
        _node(conflict_group=bad)
    with pytest.raises(ValidationError):
        _node(label=bad)
    with pytest.raises(ValidationError):
        _node(scope=GraphScope(namespace=bad))


def test_identifiers_are_stripped_not_rejected_for_surrounding_space():
    assert _node(id=f"  {CLAIM_ID} ").id == CLAIM_ID
    assert _edge(target_id=f" {EVIDENCE_ID}  ").target_id == EVIDENCE_ID


def test_optional_identifiers_normalize_blank_to_none():
    node = _node(conflict_group="  ", scope=GraphScope(user_id=" ", namespace="ops"))
    assert node.conflict_group is None
    assert node.scope.user_id is None
    assert node.scope.namespace == "ops"


def test_content_may_contain_newlines():
    """Only identifiers are single-line; a body is free text."""
    node = _node(content="line one\nline two")
    assert "\n" in node.content


def test_self_referencing_edge_is_rejected():
    with pytest.raises(ValidationError):
        _edge(source_id=CLAIM_ID, target_id=CLAIM_ID)


# --- F. scope ----------------------------------------------------------------

def test_scope_matching_ignores_unset_filter_fields():
    scope = GraphScope(user_id="u1", agent_id="a1", namespace="retrieval")
    assert scope.matches(GraphScope()) is True
    assert scope.matches(GraphScope(namespace="retrieval")) is True
    assert scope.matches(GraphScope(namespace="todo")) is False
    assert scope.matches(GraphScope(user_id="u1", agent_id="a1")) is True
    assert scope.matches(GraphScope(user_id="u2")) is False


def test_empty_scope_does_not_match_a_narrow_filter():
    assert GraphScope().matches(GraphScope(namespace="retrieval")) is False
    assert GraphScope().matches(GraphScope()) is True


# --- G. provenance -----------------------------------------------------------

def test_claim_group_requires_source_refs_or_derivation():
    assert set(PROVENANCE_REQUIRED_NODE_KINDS) == {
        NodeKind.CLAIM,
        NodeKind.EXPERIENCE,
        NodeKind.POLICY,
        NodeKind.PROCEDURE,
    }
    for kind in PROVENANCE_REQUIRED_NODE_KINDS:
        with pytest.raises(ValidationError):
            _node(id=f"{kind.value}_unbacked", kind=kind, source_refs=[])


def test_source_refs_alone_satisfy_provenance():
    node = _node(source_refs=[_source_ref()], derivation=None)
    assert node.source_refs[0].episode_id == EPISODE_ID


def test_derivation_alone_satisfies_provenance():
    node = _node(
        source_refs=[],
        derivation=_derivation(method=DerivationMethod.MIGRATED),
    )
    assert node.derivation.method is DerivationMethod.MIGRATED


def test_identity_and_source_nodes_need_no_provenance():
    """An Entity is an identity node; requiring evidence would block it."""
    for node_id, kind in (
        (ENTITY_ID, NodeKind.ENTITY),
        (EPISODE_ID, NodeKind.EPISODE),
        (EVIDENCE_ID, NodeKind.EVIDENCE),
        ("event_index_rebuild_failed", NodeKind.EVENT),
        ("task_backfill_index", NodeKind.TASK),
        ("community_retrieval_cluster", NodeKind.COMMUNITY),
    ):
        node = GraphNode(id=node_id, kind=kind, label=node_id)
        assert node.source_refs == []


def test_strong_assertion_edges_require_provenance():
    for relation in PROVENANCE_REQUIRED_RELATIONS:
        with pytest.raises(ValidationError):
            _edge(
                relation_type=relation,
                source_id=CLAIM_ID,
                target_id=OLD_CLAIM_ID,
                evidence_refs=[],
            )
        backed = _edge(
            relation_type=relation,
            source_id=CLAIM_ID,
            target_id=OLD_CLAIM_ID,
            evidence_refs=[_source_ref()],
        )
        assert backed.evidence_refs[0].evidence_id == EVIDENCE_ID
        derived = _edge(
            relation_type=relation,
            source_id=CLAIM_ID,
            target_id=OLD_CLAIM_ID,
            derivation=_derivation(method=DerivationMethod.INFERRED),
        )
        assert derived.derivation.method is DerivationMethod.INFERRED


def test_structural_and_weak_edges_need_no_provenance_but_may_carry_it():
    for relation in (
        RelationType.RELATED_TO,
        RelationType.ABOUT,
        RelationType.MENTIONS,
        RelationType.OBSERVED_IN,
        RelationType.EVIDENCED_BY,
        RelationType.DERIVED_FROM,
        RelationType.PART_OF,
        RelationType.BLOCKS,
        RelationType.APPLIES_TO,
        RelationType.IMPLEMENTS,
    ):
        edge = _edge(relation_type=relation, evidence_refs=[])
        assert edge.evidence_refs == []
        assert _edge(
            relation_type=relation, evidence_refs=[_source_ref()]
        ).evidence_refs


def test_provenance_and_strength_predicates_match_the_constants():
    """The predicates a traversal filter will read must agree with the policy
    constants, or `include_weak_relations` would filter the wrong edges."""
    for kind in NodeKind:
        node = GraphNode(
            id=f"{kind.value}_predicate",
            kind=kind,
            derivation=_derivation(),
        )
        assert node.requires_provenance() is (kind in PROVENANCE_REQUIRED_NODE_KINDS)

    for relation in RelationType:
        edge = _edge(relation_type=relation, derivation=_derivation())
        assert edge.is_weak() is (relation in WEAK_RELATIONS)
        assert edge.requires_provenance() is (
            relation in PROVENANCE_REQUIRED_RELATIONS
        )


def test_source_ref_needs_at_least_one_locator():
    with pytest.raises(ValidationError):
        SourceRef()
    with pytest.raises(ValidationError):
        SourceRef(quote_hash="sha256:0f1e2d3c")  # a hash alone locates nothing
    assert SourceRef(episode_id=EPISODE_ID).episode_id == EPISODE_ID
    assert SourceRef(evidence_id=EVIDENCE_ID).evidence_id == EVIDENCE_ID
    assert SourceRef(source_file="knowledge/retrieval.md").source_section is None


def test_source_ref_rejects_newlines_in_locators():
    with pytest.raises(ValidationError):
        SourceRef(episode_id="episode\n0001")
    with pytest.raises(ValidationError):
        SourceRef(source_file="knowledge/retrieval.md\nknowledge/other.md")


def test_derivation_records_how_the_memory_came_to_exist():
    derivation = Derivation(
        method=DerivationMethod.SUMMARIZED,
        model="local-summarizer",
        prompt_version="v3",
        generated_at=datetime(2026, 1, 5, 9, 0, 0),
    )
    assert derivation.generated_at == T0
    payload = derivation.to_dict()
    assert _enum_or_datetime_leaks(payload) == []
    assert payload == {
        "method": "summarized",
        "model": "local-summarizer",
        "prompt_version": "v3",
        "generated_at": "2026-01-05T09:00:00+00:00",
    }


def test_derivation_requires_a_known_method():
    with pytest.raises(ValidationError):
        Derivation(method="guessed")
    with pytest.raises(ValidationError):
        Derivation()


# --- H. memory events (rebuild input) ----------------------------------------

def test_memory_event_carries_graph_state_to_project():
    event = MemoryEvent(
        id="event_0001",
        kind=MemoryEventKind.INJECT,
        occurred_at=T0,
        node=_node(),
    )
    assert event.kind is MemoryEventKind.INJECT
    payload = event.to_dict()
    assert _enum_or_datetime_leaks(payload) == []
    assert payload["kind"] == "inject"
    assert payload["node"]["id"] == CLAIM_ID
    assert payload["edge"] is None


def test_memory_event_must_reference_something():
    with pytest.raises(ValidationError):
        MemoryEvent(id="event_0002", kind=MemoryEventKind.APPROVE, occurred_at=T0)
    referenced = MemoryEvent(
        id="event_0002",
        kind=MemoryEventKind.APPROVE,
        occurred_at=T0,
        target_id=CLAIM_ID,
    )
    assert referenced.target_id == CLAIM_ID


def test_memory_event_kinds_cover_the_lifecycle():
    assert {k.value for k in MemoryEventKind} == {
        "inject",
        "revise",
        "supersede",
        "retract",
        "approve",
        "restore",
        "migrate",
    }


# --- I. stable identity helpers ----------------------------------------------

def test_node_ids_are_stable_for_the_same_source_identity():
    """§10.2: a rebuild must produce stable IDs for the same source identity."""
    first = make_node_id(NodeKind.CLAIM, "retrieval.md", "Index maintenance")
    again = make_node_id("claim", "retrieval.md", "Index maintenance")
    assert first == again
    assert first.startswith("claim_")
    assert "\n" not in first
    assert first != make_node_id(NodeKind.CLAIM, "retrieval.md", "Other section")
    assert first != make_node_id(NodeKind.ENTITY, "retrieval.md", "Index maintenance")


def test_node_id_helper_needs_a_source_identity():
    with pytest.raises(ValueError):
        make_node_id(NodeKind.CLAIM)
    with pytest.raises(ValueError):
        make_node_id(NodeKind.CLAIM, "   ")


def test_edge_ids_are_stable_and_discriminated():
    first = make_edge_id(RelationType.SUPERSEDES, CLAIM_ID, OLD_CLAIM_ID)
    assert first == make_edge_id("SUPERSEDES", CLAIM_ID, OLD_CLAIM_ID)
    assert first != make_edge_id(RelationType.SUPPORTS, CLAIM_ID, OLD_CLAIM_ID)
    assert first != make_edge_id(RelationType.SUPERSEDES, OLD_CLAIM_ID, CLAIM_ID)
    assert first != make_edge_id(
        RelationType.SUPERSEDES, CLAIM_ID, OLD_CLAIM_ID, discriminator="2026-01"
    )
    assert GraphEdge(
        id=first,
        relation_type=RelationType.SUPERSEDES,
        source_id=CLAIM_ID,
        target_id=OLD_CLAIM_ID,
        evidence_refs=[_source_ref()],
    ).id == first


# --- J. GraphStore contract --------------------------------------------------

ARCHITECTURE_OPERATIONS = (
    "create_node",
    "append_edge",
    "get_node",
    "get_edges",
    "traverse",
    "find_conflicts",
    "get_provenance",
    "rebuild",
    "health",
)


def test_contract_declares_exactly_the_architecture_operations():
    assert set(REQUIRED_GRAPH_STORE_OPERATIONS) == set(ARCHITECTURE_OPERATIONS)
    for operation in ARCHITECTURE_OPERATIONS:
        assert callable(getattr(GraphStore, operation, None)), operation


def test_contract_is_vendor_neutral():
    """§9.3: the semantic contract must survive a backend swap, so no backend
    may appear in it — not even transitively through a storage module."""
    import inspect

    from layered_memory_mcp import graph_models, graph_store

    vendor = ("kuzu", "neo4j", "networkx", "sqlite3", "graphiti", "mem0")
    for module in (graph_models, graph_store):
        source = inspect.getsource(module)
        for name in vendor:
            assert f"import {name}" not in source, f"{module.__name__}: {name}"
            assert not hasattr(module, name), f"{module.__name__}: {name}"
        # Nor may it reach back into the existing storage/server hot path.
        assert "from .storage" not in source
        assert "from .server" not in source


def test_incomplete_implementation_is_detectable():
    class PartialStore:
        def create_node(self, node):
            return node

        def health(self):
            return GraphHealth(backend="partial")

    missing = missing_graph_store_operations(PartialStore())
    assert set(missing) == set(ARCHITECTURE_OPERATIONS) - {"create_node", "health"}
    assert missing == sorted(missing)
    assert not isinstance(PartialStore(), GraphStore)


def test_complete_implementation_satisfies_the_contract():
    store = _FixtureStore()
    assert missing_graph_store_operations(store) == []
    assert isinstance(store, GraphStore)


def test_contract_is_usable_without_a_backend():
    store = _FixtureStore()
    claim = _node()
    evidence = GraphNode(id=EVIDENCE_ID, kind=NodeKind.EVIDENCE, label="excerpt")
    store.create_node(evidence)
    store.create_node(claim)
    edge = store.append_edge(_edge())

    assert store.get_node(CLAIM_ID) == claim
    assert store.get_node("claim_missing") is None
    assert store.get_edges(CLAIM_ID, direction="outgoing") == [edge]
    assert store.get_edges(EVIDENCE_ID, direction="outgoing") == []
    assert store.get_edges(EVIDENCE_ID, direction="incoming") == [edge]
    assert store.get_edges(
        CLAIM_ID, relation_types=[RelationType.SUPPORTS], direction="both"
    ) == []

    result = store.traverse([CLAIM_ID], max_hops=1, filters=TraversalFilters())
    assert [n.id for n in result.nodes] == [CLAIM_ID, EVIDENCE_ID]
    assert result.paths[0].node_ids == [CLAIM_ID, EVIDENCE_ID]
    assert result.truncated is False

    provenance = store.get_provenance(CLAIM_ID)
    assert provenance.node_id == CLAIM_ID
    assert provenance.evidence_ids == [EVIDENCE_ID]
    assert provenance.source_refs[0].episode_id == EPISODE_ID

    assert store.find_conflicts(GraphScope(), [ENTITY_ID]) == []
    report = store.rebuild([MemoryEvent(id="event_0001", kind=MemoryEventKind.INJECT, occurred_at=T0, node=claim)])
    assert report.nodes_written == 1
    health = store.health()
    assert health.healthy is True
    assert _enum_or_datetime_leaks(health.to_dict()) == []


# --- K. result shapes --------------------------------------------------------

def test_traversal_filters_default_to_current_in_scope_state():
    filters = TraversalFilters()
    assert filters.node_statuses == list(CURRENT_NODE_STATUSES)
    assert filters.edge_statuses == [EdgeStatus.ACTIVE]
    assert filters.include_weak_relations is False
    assert filters.relation_types == []
    assert filters.as_of is None
    assert _enum_or_datetime_leaks(filters.to_dict()) == []
    assert filters.to_dict()["node_statuses"] == ["active"]


def test_traversal_filters_normalize_naive_as_of():
    filters = TraversalFilters(as_of=datetime(2026, 1, 5, 9, 0, 0))
    assert filters.as_of == T0


def test_graph_path_must_be_a_connected_chain():
    path = GraphPath(node_ids=[CLAIM_ID, EVIDENCE_ID], edge_ids=["edge_claim_evidenced"])
    assert path.hops == 1
    assert GraphPath(node_ids=[CLAIM_ID]).hops == 0
    with pytest.raises(ValidationError):
        GraphPath(node_ids=[CLAIM_ID, EVIDENCE_ID], edge_ids=[])
    with pytest.raises(ValidationError):
        GraphPath(node_ids=[])


def test_conflict_report_needs_two_sides_and_stays_unresolved_by_default():
    report = ConflictReport(
        node_ids=[CLAIM_ID, OLD_CLAIM_ID],
        conflict_group="conflict_index_cadence",
    )
    assert report.resolved is False
    assert report.relation_type is RelationType.CONTRADICTS
    assert _enum_or_datetime_leaks(report.to_dict()) == []
    with pytest.raises(ValidationError):
        ConflictReport(node_ids=[CLAIM_ID])


def test_provenance_record_reports_unresolved_references():
    record = ProvenanceRecord(
        node_id=CLAIM_ID,
        node_version=2,
        source_refs=[_source_ref()],
        derivation=_derivation(),
        evidence_ids=[EVIDENCE_ID],
        episode_ids=[EPISODE_ID],
        warnings=["unresolved_evidence:evidence_missing"],
    )
    assert record.resolved is False
    assert _enum_or_datetime_leaks(record.to_dict()) == []
    assert ProvenanceRecord(node_id=CLAIM_ID).resolved is True


def test_rebuild_report_counts_and_health_defaults():
    report = RebuildReport(events_read=3, nodes_written=2, edges_written=1)
    assert report.ok is True
    assert RebuildReport(errors=["duplicate_node_id:claim_x"]).ok is False

    health = GraphHealth(backend="fixture", node_count=2, edge_count=1)
    assert health.healthy is True
    assert health.schema_version == GRAPH_SCHEMA_VERSION
    assert GraphHealth(backend="fixture", errors=["schema_missing"]).healthy is False


# --- a minimal in-test implementation of the contract ------------------------
# Deliberately not shipped: Batch C owns the deterministic fixture store. This
# exists only to prove the Batch B contract is implementable and typed.

class _FixtureStore:
    """Smallest thing that honours the §9.1 contract, for contract tests only."""

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []

    def create_node(self, node: GraphNode) -> GraphNode:
        self.nodes[node.id] = node
        return node

    def append_edge(self, edge: GraphEdge) -> GraphEdge:
        self.edges.append(edge)
        return edge

    def get_node(self, node_id: str) -> GraphNode | None:
        return self.nodes.get(node_id)

    def get_edges(
        self,
        node_id: str,
        relation_types: list[RelationType] | None = None,
        direction: str = "both",
    ) -> list[GraphEdge]:
        allowed = set(relation_types or [])
        found = []
        for edge in self.edges:
            if allowed and edge.relation_type not in allowed:
                continue
            if direction in ("outgoing", "both") and edge.source_id == node_id:
                found.append(edge)
            elif direction in ("incoming", "both") and edge.target_id == node_id:
                found.append(edge)
        return found

    def traverse(
        self,
        seed_ids: list[str],
        max_hops: int = 1,
        filters: TraversalFilters | None = None,
    ) -> TraversalResult:
        nodes = [self.nodes[s] for s in seed_ids if s in self.nodes]
        paths = []
        for seed in seed_ids:
            for edge in self.get_edges(seed, direction="outgoing"):
                target = self.nodes.get(edge.target_id)
                if target is None:
                    continue
                nodes.append(target)
                paths.append(GraphPath(node_ids=[seed, target.id], edge_ids=[edge.id]))
        return TraversalResult(seed_ids=list(seed_ids), nodes=nodes, edges=list(self.edges), paths=paths)

    def find_conflicts(
        self, scope: GraphScope | None = None, entity_ids: list[str] | None = None
    ) -> list[ConflictReport]:
        return []

    def get_provenance(self, node_id: str) -> ProvenanceRecord:
        node = self.nodes[node_id]
        evidence_ids = [
            edge.target_id
            for edge in self.get_edges(
                node_id, relation_types=[RelationType.EVIDENCED_BY], direction="outgoing"
            )
        ]
        return ProvenanceRecord(
            node_id=node.id,
            node_version=node.version,
            source_refs=list(node.source_refs),
            derivation=node.derivation,
            evidence_ids=evidence_ids,
        )

    def rebuild(self, source_events: list[MemoryEvent]) -> RebuildReport:
        nodes = edges = 0
        for event in source_events:
            if event.node is not None:
                self.create_node(event.node)
                nodes += 1
            if event.edge is not None:
                self.append_edge(event.edge)
                edges += 1
        return RebuildReport(
            events_read=len(source_events), nodes_written=nodes, edges_written=edges
        )

    def health(self) -> GraphHealth:
        return GraphHealth(
            backend="fixture",
            node_count=len(self.nodes),
            edge_count=len(self.edges),
        )

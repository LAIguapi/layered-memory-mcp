"""v3.6.0 Batch G — the backend-neutral semantic graph write seam.

Why this file exists: ARCHITECTURE_v2.md §8.1 requires one semantic write path —
"no direct independent writes to graph and vector stores are permitted" — and
§12 Batch G requires supersession, contradiction and projection events to be
testable *before* anything touches the default MCP write path. Nothing here is a
runtime cutover: the module under test reads no file, no config and no real
store, and it imports neither ``injector``, ``recall``, ``server`` nor any
storage module.

These tests pin the properties that make such a seam safe to build on:

  * **The decision is the caller's.** The seam consumes an explicit semantic
    write decision — this node, these targets, this relation type — and never
    infers one. No text comparison, no similarity, no entity matching, no
    automatic candidate discovery.
  * **Supersession is two effects, never one** (§8.2). A ``SUPERSEDES`` edge
    *and* a ``SUPERSEDE`` lifecycle event, so a rebuild cannot end with an
    asserted replacement whose target is still active.
  * **Contradiction settles nothing** (§8.3). A ``CONTRADICTS`` edge is
    asserted, both claims stay active, and the conflict stays reportable until a
    separate explicit supersession decision arrives.
  * **Identity is derived, never minted.** Event and edge IDs are hashes of the
    request/node/target identity — no UUID, no wall clock, no insertion order —
    so replanning the same request is byte-identical and projecting it twice is
    a no-op.
  * **Nothing is written behind the store's back.** The projection calls exactly
    one store operation, ``rebuild``, with the caller's own source events plus
    the generated ones; an invalid request never reaches the store at all.
  * **It is honest** (§10.3). A missing target is an error, a target that cannot
    take the transition is a warning, and a rebuild that reports errors is
    returned with those errors rather than as a clean write.

Fixture data is neutral technical vocabulary: a retrieval index, its rebuild
cadence and the build session that recorded it. No private memory bodies, no
credentials, no host paths.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from layered_memory_mcp import graph_write
from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
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
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore
from layered_memory_mcp.graph_store import RebuildReport
from layered_memory_mcp.graph_write import (
    SEMANTIC_WRITE_EVENT_NAMESPACE,
    SEMANTIC_WRITE_EVENT_PREFIX,
    SEMANTIC_WRITE_OPERATIONS,
    RelationAssertion,
    RelationDecision,
    SemanticWriteError,
    SemanticWritePlan,
    SemanticWriteProjection,
    SemanticWriteRequest,
    plan_semantic_write,
    project_semantic_write,
)

# --- neutral fixture vocabulary ---------------------------------------------

T0 = datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=14)

NAMESPACE = "retrieval_fixture"
CONFLICT_GROUP = "conflict_index_rebuild_cadence"

EPISODE_ID = "episode_build_0042"
EVIDENCE_ID = "evidence_build_0042_0001"
ENTITY_ID = "entity_retrieval_index"
OLD_CLAIM_ID = "claim_index_rebuild_weekly"
NEW_CLAIM_ID = "claim_index_rebuild_nightly"
RIVAL_CLAIM_ID = "claim_index_rebuild_hourly"
UNKNOWN_CLAIM_ID = "claim_index_rebuild_unknown"

# The identity separator and digest length graph_models documents for its own
# generated IDs. Re-derived here rather than imported, so the test pins the
# scheme instead of agreeing with whatever the module happens to do.
_SEPARATOR = "\x1f"
_DIGEST_LENGTH = 16


def _scope() -> GraphScope:
    return GraphScope(namespace=NAMESPACE)


def _source_ref(**kwargs) -> SourceRef:
    data = {
        "episode_id": EPISODE_ID,
        "evidence_id": EVIDENCE_ID,
        "source_file": "knowledge/retrieval.md",
        "source_section": "Index maintenance",
    }
    data.update(kwargs)
    return SourceRef(**data)


def _derivation() -> Derivation:
    return Derivation(method=DerivationMethod.USER_EXPLICIT, generated_at=T1)


def _node(kind: NodeKind, node_id: str, **kwargs) -> GraphNode:
    data = {
        "id": node_id,
        "kind": kind,
        "scope": _scope(),
        "recorded_at": T0,
    }
    data.update(kwargs)
    return GraphNode(**data)


def _episode() -> GraphNode:
    return _node(NodeKind.EPISODE, EPISODE_ID, label="Index maintenance review")


def _evidence() -> GraphNode:
    return _node(
        NodeKind.EVIDENCE,
        EVIDENCE_ID,
        label="Index maintenance, paragraph 2",
        source_refs=[_source_ref(evidence_id=None)],
    )


def _entity() -> GraphNode:
    return _node(NodeKind.ENTITY, ENTITY_ID, label="retrieval index")


def _claim(node_id: str, content: str, **kwargs) -> GraphNode:
    data = {
        "label": content,
        "content": content,
        "memory_class": MemoryClass.FACT,
        "confidence": 0.8,
        "source_refs": [_source_ref()],
        "conflict_group": CONFLICT_GROUP,
    }
    data.update(kwargs)
    return _node(NodeKind.CLAIM, node_id, **data)


def _old_claim(**kwargs) -> GraphNode:
    return _claim(
        OLD_CLAIM_ID, "The retrieval index is rebuilt weekly.", **kwargs
    )


def _rival_claim(**kwargs) -> GraphNode:
    return _claim(
        RIVAL_CLAIM_ID, "The retrieval index is rebuilt hourly.", **kwargs
    )


def _new_claim(**kwargs) -> GraphNode:
    data = {"recorded_at": T1}
    data.update(kwargs)
    return _claim(
        NEW_CLAIM_ID, "The retrieval index is rebuilt nightly.", **data
    )


def _event(kind: MemoryEventKind, event_id: str, at: datetime, **kwargs) -> MemoryEvent:
    return MemoryEvent(id=event_id, kind=kind, occurred_at=at, **kwargs)


def _source_events() -> list[MemoryEvent]:
    """The explicit prior ledger: three sources and two active claims."""
    return [
        _event(MemoryEventKind.INJECT, "source_0001", T0, node=_episode()),
        _event(MemoryEventKind.INJECT, "source_0002", T0, node=_evidence()),
        _event(MemoryEventKind.INJECT, "source_0003", T0, node=_entity()),
        _event(MemoryEventKind.INJECT, "source_0004", T0, node=_old_claim()),
        _event(MemoryEventKind.INJECT, "source_0005", T0, node=_rival_claim()),
    ]


def _existing_nodes() -> list[GraphNode]:
    return [_episode(), _evidence(), _entity(), _old_claim(), _rival_claim()]


def _supersedes(target: str = OLD_CLAIM_ID, **kwargs) -> RelationAssertion:
    data = {
        "relation_type": RelationType.SUPERSEDES,
        "target_id": target,
        "confidence": 0.9,
        "evidence_refs": [_source_ref()],
    }
    data.update(kwargs)
    return RelationAssertion(**data)


def _contradicts(target: str = RIVAL_CLAIM_ID, **kwargs) -> RelationAssertion:
    data = {
        "relation_type": RelationType.CONTRADICTS,
        "target_id": target,
        "confidence": 0.7,
        "derivation": _derivation(),
    }
    data.update(kwargs)
    return RelationAssertion(**data)


def _about(target: str = ENTITY_ID, **kwargs) -> RelationAssertion:
    data = {"relation_type": RelationType.ABOUT, "target_id": target}
    data.update(kwargs)
    return RelationAssertion(**data)


def _evidenced_by(target: str = EVIDENCE_ID, **kwargs) -> RelationAssertion:
    data = {"relation_type": RelationType.EVIDENCED_BY, "target_id": target}
    data.update(kwargs)
    return RelationAssertion(**data)


def _request(**kwargs) -> SemanticWriteRequest:
    data = {"node": _new_claim(), "occurred_at": T1}
    data.update(kwargs)
    return SemanticWriteRequest(**data)


def _expected_event_id(
    role: str, subject: str, *, request_key: str = NEW_CLAIM_ID
) -> str:
    """Re-derive the documented event identity scheme independently.

    ``event_<sha256(namespace, request_key, role, subject)[:16]>`` — a hash of
    the *decision*, with no ordinal, no timestamp and no counter in it, so
    adding a second target cannot renumber the first one's event.
    """
    digest = hashlib.sha256(
        _SEPARATOR.join(
            [SEMANTIC_WRITE_EVENT_NAMESPACE, request_key, role, subject]
        ).encode("utf-8")
    ).hexdigest()
    return f"{SEMANTIC_WRITE_EVENT_PREFIX}{digest[:_DIGEST_LENGTH]}"


def _plan(**kwargs) -> SemanticWritePlan:
    """Plan a request against the explicit fixture node material."""
    return plan_semantic_write(_request(**kwargs), existing_nodes=_existing_nodes())


def _assert_plain_json(value, path: str = "$") -> None:
    """Fail if ``value`` holds anything that is not a plain JSON type."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), f"{path}: non-string key {key!r}"
            _assert_plain_json(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_plain_json(item, f"{path}[{index}]")
    else:
        assert value is None or isinstance(
            value, (str, int, float, bool)
        ), f"{path}: {type(value).__name__} is not a plain JSON type"


# --- test doubles ------------------------------------------------------------

class _RecordingStore:
    """A full :class:`GraphStore` that records which operations were reached.

    It implements the other write operations on purpose: the point of
    ``test_projection_reaches_only_rebuild`` is that the seam does not call them
    even though it could.
    """

    def __init__(self, inner: InMemoryGraphStore | None = None) -> None:
        self._inner = inner or InMemoryGraphStore()
        self.calls: list[str] = []

    def create_node(self, node):            # pragma: no cover - must not run
        self.calls.append("create_node")
        return self._inner.create_node(node)

    def append_edge(self, edge):            # pragma: no cover - must not run
        self.calls.append("append_edge")
        return self._inner.append_edge(edge)

    def get_node(self, node_id):
        self.calls.append("get_node")
        return self._inner.get_node(node_id)

    def get_edges(self, node_id, relation_types=None, direction="both"):
        self.calls.append("get_edges")
        return self._inner.get_edges(node_id, relation_types, direction)

    def traverse(self, seed_ids, max_hops=1, filters=None):
        self.calls.append("traverse")
        return self._inner.traverse(seed_ids, max_hops, filters)

    def find_conflicts(self, scope=None, entity_ids=None):
        self.calls.append("find_conflicts")
        return self._inner.find_conflicts(scope, entity_ids)

    def get_provenance(self, node_id):
        self.calls.append("get_provenance")
        return self._inner.get_provenance(node_id)

    def rebuild(self, source_events):
        self.calls.append("rebuild")
        return self._inner.rebuild(source_events)

    def health(self):
        self.calls.append("health")
        return self._inner.health()


class _RaisingRebuildStore(InMemoryGraphStore):
    """A store whose projection backend is unavailable."""

    def rebuild(self, source_events) -> RebuildReport:
        raise RuntimeError("projection backend unavailable")


# ===========================================================================
# A. The explicit decision: request validation
# ===========================================================================

def test_a_minimal_request_only_needs_a_node_and_a_timestamp():
    request = _request()
    assert request.node.id == NEW_CLAIM_ID
    assert request.occurred_at == T1
    # The stable identity namespace/key default to the node's own identity.
    assert request.request_key == NEW_CLAIM_ID
    assert request.event_namespace == SEMANTIC_WRITE_EVENT_NAMESPACE
    assert request.assertions == []
    assert request.supersedes == []
    assert request.contradicts == []


def test_a_caller_supplied_request_key_is_used_as_the_event_namespace_key():
    request = _request(request_key="write_0042")
    plan = plan_semantic_write(request, existing_nodes=_existing_nodes())
    assert plan.request_key == "write_0042"
    assert plan.node_event_id == _expected_event_id(
        "node", NEW_CLAIM_ID, request_key="write_0042"
    )


def test_a_node_without_provenance_is_still_refused_by_the_graph_model():
    """The seam does not weaken :class:`GraphNode` validation (§6.3)."""
    with pytest.raises(ValueError):
        _request(node=_new_claim(source_refs=[], derivation=None))


def test_self_targeting_is_rejected():
    for field in ("assertions", "supersedes", "contradicts"):
        assertion = {
            "assertions": _about(target=NEW_CLAIM_ID),
            "supersedes": _supersedes(target=NEW_CLAIM_ID),
            "contradicts": _contradicts(target=NEW_CLAIM_ID),
        }[field]
        with pytest.raises(ValueError, match="itself"):
            _request(**{field: [assertion]})


def test_a_blank_target_id_is_rejected():
    with pytest.raises(ValueError):
        _supersedes(target="   ")


def test_a_target_id_with_a_newline_is_rejected():
    with pytest.raises(ValueError):
        _supersedes(target="claim_index\nrebuild")


def test_a_duplicate_supersession_target_is_rejected():
    with pytest.raises(ValueError, match="declared twice"):
        _request(supersedes=[_supersedes(), _supersedes()])


def test_one_target_may_not_be_both_superseded_and_contradicted():
    """Requirement §8.3: the ambiguous declaration is refused, not guessed."""
    with pytest.raises(ValueError, match="superseded and contradicted"):
        _request(
            supersedes=[_supersedes(target=OLD_CLAIM_ID)],
            contradicts=[_contradicts(target=OLD_CLAIM_ID)],
        )


def test_a_supersession_declared_with_another_relation_type_is_rejected():
    with pytest.raises(ValueError, match="SUPERSEDES"):
        _request(supersedes=[_about(target=OLD_CLAIM_ID)])


def test_a_contradiction_declared_with_another_relation_type_is_rejected():
    with pytest.raises(ValueError, match="CONTRADICTS"):
        _request(contradicts=[_about(target=RIVAL_CLAIM_ID)])


def test_a_plain_assertion_may_not_smuggle_in_a_supersession():
    """A ``SUPERSEDES`` edge without its lifecycle event is exactly the state
    §8.2 forbids, so the only way to declare one is the ``supersedes`` list."""
    with pytest.raises(ValueError, match="supersedes"):
        _request(assertions=[_supersedes(target=OLD_CLAIM_ID)])


def test_a_plain_assertion_may_not_smuggle_in_a_contradiction():
    with pytest.raises(ValueError, match="contradicts"):
        _request(assertions=[_contradicts(target=RIVAL_CLAIM_ID)])


def test_supersession_without_provenance_is_rejected():
    with pytest.raises(ValueError, match="evidence"):
        _supersedes(evidence_refs=[])


def test_contradiction_without_provenance_is_rejected():
    with pytest.raises(ValueError, match="evidence"):
        _contradicts(derivation=None)


def test_a_weak_assertion_needs_no_provenance():
    """``PROVENANCE_REQUIRED_RELATIONS`` is the model's list, not a wider one."""
    assert _about().evidence_refs == []
    assert _about().derivation is None


def test_one_relation_triple_may_not_be_asserted_twice():
    with pytest.raises(ValueError, match="declared twice"):
        _request(assertions=[_about(), _about()])


def test_one_relation_triple_may_be_asserted_twice_with_a_discriminator():
    """Two genuinely distinct assertions of a triple need distinct edge IDs."""
    plan = _plan(
        assertions=[
            _about(valid_to=T1),
            _about(valid_from=T1, discriminator="after_review"),
        ]
    )
    edge_ids = [decision.edge_id for decision in plan.relations]
    assert len(set(edge_ids)) == 2


def test_an_unknown_relation_type_is_rejected():
    with pytest.raises(ValueError):
        RelationAssertion(relation_type="ENDORSES", target_id=OLD_CLAIM_ID)


def test_an_assertion_with_an_inverted_validity_window_is_rejected():
    with pytest.raises(ValueError, match="valid_to"):
        _about(valid_from=T1, valid_to=T0)


# ===========================================================================
# B. Minimal explicit injection
# ===========================================================================

def test_a_minimal_write_plans_exactly_one_inject_event():
    plan = _plan()
    assert len(plan.events) == 1
    event = plan.events[0]
    assert event.kind is MemoryEventKind.INJECT
    assert event.occurred_at == T1
    assert event.node is not None
    assert event.edge is None
    assert event.target_id is None
    assert plan.relations == []
    assert plan.superseded_ids == []
    assert plan.contradicted_ids == []
    assert plan.warnings == []


def test_the_new_node_payload_is_carried_through_untouched():
    """The seam does not re-derive, re-time or re-score the caller's node."""
    node = _new_claim()
    plan = plan_semantic_write(
        _request(node=node), existing_nodes=_existing_nodes()
    )
    assert plan.events[0].node.to_json() == node.to_json()
    assert plan.node_id == node.id


def test_the_node_event_id_is_a_hash_of_the_write_decision():
    plan = _plan()
    assert plan.events[0].id == _expected_event_id("node", NEW_CLAIM_ID)
    assert plan.node_event_id == plan.events[0].id


def test_nothing_is_inferred_from_the_node_content():
    """§3.2: no extraction, no entity matching, no automatic candidates.

    The fixture node names an entity that exists in the supplied material and
    carries a source ref to a stored evidence node, and the plan still contains
    no ``ABOUT`` and no ``EVIDENCED_BY`` edge, because the caller did not ask
    for one.
    """
    plan = _plan()
    assert [event.edge for event in plan.events] == [None]


def test_the_event_scope_comes_from_the_node_when_the_request_sets_none():
    assert _plan().events[0].scope == _scope()


def test_an_explicit_request_scope_overrides_the_node_scope():
    other = GraphScope(namespace="indexing_fixture", user_id="u1")
    plan = _plan(scope=other)
    assert plan.events[0].scope == other


# ===========================================================================
# C. Supersession: an edge *and* a lifecycle transition (§8.2)
# ===========================================================================

def test_supersession_emits_a_superseding_edge_and_a_lifecycle_event():
    plan = _plan(supersedes=[_supersedes()])
    assert [event.kind for event in plan.events] == [
        MemoryEventKind.INJECT,      # the new claim
        MemoryEventKind.INJECT,      # the SUPERSEDES edge
        MemoryEventKind.SUPERSEDE,   # the old claim's lifecycle transition
    ]

    edge_event, transition = plan.events[1], plan.events[2]
    edge = edge_event.edge
    assert edge.relation_type is RelationType.SUPERSEDES
    assert edge.source_id == NEW_CLAIM_ID
    assert edge.target_id == OLD_CLAIM_ID
    assert edge.evidence_refs == [_source_ref()]
    assert edge.confidence == 0.9
    assert edge.id == make_edge_id(
        RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID
    )

    assert transition.target_id == OLD_CLAIM_ID
    assert transition.node is None
    assert transition.edge is None

    assert plan.superseded_ids == [OLD_CLAIM_ID]
    assert plan.contradicted_ids == []
    assert plan.warnings == []


def test_the_supersession_decision_is_reported_explicitly():
    plan = _plan(supersedes=[_supersedes()])
    assert plan.relations == [
        RelationDecision(
            relation_type=RelationType.SUPERSEDES,
            target_id=OLD_CLAIM_ID,
            edge_id=make_edge_id(
                RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID
            ),
            event_id=_expected_event_id(
                "edge",
                make_edge_id(RelationType.SUPERSEDES, NEW_CLAIM_ID, OLD_CLAIM_ID),
            ),
            transition_event_id=_expected_event_id("supersede", OLD_CLAIM_ID),
        )
    ]
    assert plan.relation_counts == {"SUPERSEDES": 1}


def test_generated_events_sort_after_the_node_that_they_depend_on():
    """The rebuild applies events in ``(occurred_at, id)`` order, so node
    creation has to be *first* by timestamp rather than by list position: a
    deterministic one-microsecond-per-event sequence after the request
    timestamp, which never depends on the caller's declaration order."""
    plan = _plan(
        supersedes=[_supersedes()],
        contradicts=[_contradicts()],
        assertions=[_about(), _evidenced_by()],
    )
    offsets = [
        (event.occurred_at - T1) // timedelta(microseconds=1)
        for event in plan.events
    ]
    assert offsets == [0, 1, 2, 3, 4, 5]
    assert plan.events == sorted(plan.events, key=lambda e: (e.occurred_at, e.id))
    # The node is created first; its transition targets come last.
    assert plan.events[0].node is not None
    assert plan.events[-1].kind is MemoryEventKind.SUPERSEDE


def test_rebuild_leaves_a_superseded_target_superseded():
    store = InMemoryGraphStore()
    projection = project_semantic_write(
        store, _source_events(), _request(supersedes=[_supersedes()])
    )
    assert projection.report.errors == []
    assert projection.ok is True
    assert store.get_node(OLD_CLAIM_ID).status is NodeStatus.SUPERSEDED
    assert store.get_node(NEW_CLAIM_ID).status is NodeStatus.ACTIVE
    assert store.health().healthy is True
    # The old claim stays queryable as history rather than being deleted.
    assert store.get_node(OLD_CLAIM_ID).content.endswith("rebuilt weekly.")


def test_a_pending_target_can_also_be_superseded():
    source = [
        event
        for event in _source_events()
        if event.id != "source_0004"
    ] + [
        _event(
            MemoryEventKind.INJECT,
            "source_0006",
            T0,
            node=_old_claim(status=NodeStatus.PENDING),
        )
    ]
    store = InMemoryGraphStore()
    projection = project_semantic_write(
        store, source, _request(supersedes=[_supersedes()])
    )
    assert projection.report.errors == []
    assert store.get_node(OLD_CLAIM_ID).status is NodeStatus.SUPERSEDED


def test_superseding_a_target_that_cannot_take_the_transition_warns():
    """An already-superseded target is reported, not forced: the rebuild would
    ignore the transition, and a silent plan would hide that."""
    nodes = [
        _old_claim(status=NodeStatus.SUPERSEDED) if node.id == OLD_CLAIM_ID else node
        for node in _existing_nodes()
    ]
    plan = plan_semantic_write(
        _request(supersedes=[_supersedes()]), existing_nodes=nodes
    )
    assert plan.warnings == [
        f"supersede_target_not_current:{OLD_CLAIM_ID}:superseded"
    ]
    # The decision still stands — the caller asked for it — so the edge is there.
    assert plan.superseded_ids == [OLD_CLAIM_ID]


def test_a_missing_target_is_an_error_not_a_dangling_write():
    with pytest.raises(ValueError, match=UNKNOWN_CLAIM_ID):
        plan_semantic_write(
            _request(supersedes=[_supersedes(target=UNKNOWN_CLAIM_ID)]),
            existing_nodes=_existing_nodes(),
        )


def test_a_missing_plain_assertion_target_is_an_error_too():
    with pytest.raises(ValueError, match=UNKNOWN_CLAIM_ID):
        plan_semantic_write(
            _request(assertions=[_about(target=UNKNOWN_CLAIM_ID)]),
            existing_nodes=_existing_nodes(),
        )


def test_target_existence_is_only_checked_against_supplied_material():
    """Without explicit node material there is nothing to check against, and
    saying so is more honest than pretending the targets were verified."""
    plan = plan_semantic_write(_request(supersedes=[_supersedes()]))
    assert plan.warnings == ["target_existence_unchecked:1"]
    assert plan.superseded_ids == [OLD_CLAIM_ID]


def test_a_mismatched_existing_node_mapping_key_is_rejected():
    with pytest.raises(ValueError, match="does not match"):
        plan_semantic_write(
            _request(), existing_nodes={UNKNOWN_CLAIM_ID: _old_claim()}
        )


def test_an_unusual_supersession_source_kind_is_reported():
    plan = plan_semantic_write(
        _request(
            node=_node(
                NodeKind.EVENT,
                "event_index_rebuild_started",
                label="index rebuild started",
            ),
            request_key="write_event_0001",
            supersedes=[_supersedes()],
        ),
        existing_nodes=_existing_nodes(),
    )
    assert "unexpected_supersession_source_kind:event" in plan.warnings


# ===========================================================================
# D. Contradiction: an edge only, and nothing settled (§8.3)
# ===========================================================================

def test_contradiction_emits_one_edge_and_no_lifecycle_transition():
    plan = _plan(contradicts=[_contradicts()])
    assert [event.kind for event in plan.events] == [
        MemoryEventKind.INJECT,
        MemoryEventKind.INJECT,
    ]
    edge = plan.events[1].edge
    assert edge.relation_type is RelationType.CONTRADICTS
    assert edge.source_id == NEW_CLAIM_ID
    assert edge.target_id == RIVAL_CLAIM_ID
    assert edge.derivation == _derivation()
    assert plan.contradicted_ids == [RIVAL_CLAIM_ID]
    assert plan.superseded_ids == []
    assert plan.relations[0].transition_event_id is None
    assert plan.warnings == []


def test_both_contradicting_claims_remain_active_after_projection():
    store = InMemoryGraphStore()
    project_semantic_write(
        store, _source_events(), _request(contradicts=[_contradicts()])
    )
    assert store.get_node(NEW_CLAIM_ID).status is NodeStatus.ACTIVE
    assert store.get_node(RIVAL_CLAIM_ID).status is NodeStatus.ACTIVE


def test_the_contradiction_stays_reported_as_unresolved():
    store = InMemoryGraphStore()
    project_semantic_write(
        store, _source_events(), _request(contradicts=[_contradicts()])
    )
    reports = store.find_conflicts()
    assert reports, "an asserted contradiction must stay reportable"
    contesting = [
        report for report in reports if NEW_CLAIM_ID in report.node_ids
    ]
    assert contesting and contesting[0].resolved is False


def test_a_contradiction_can_be_settled_only_by_a_separate_supersession():
    """§8.3: a contradiction is never resolved by write order. Naming the other
    claim for supersession *as well* is a second, explicit decision."""
    store = InMemoryGraphStore()
    project_semantic_write(
        store,
        _source_events(),
        _request(
            supersedes=[_supersedes(target=RIVAL_CLAIM_ID)],
            contradicts=[_contradicts(target=OLD_CLAIM_ID)],
        ),
    )
    assert store.get_node(RIVAL_CLAIM_ID).status is NodeStatus.SUPERSEDED
    assert store.get_node(OLD_CLAIM_ID).status is NodeStatus.ACTIVE


def test_a_contradiction_without_a_conflict_group_is_reported():
    plan = plan_semantic_write(
        _request(
            node=_new_claim(conflict_group=None), contradicts=[_contradicts()]
        ),
        existing_nodes=_existing_nodes(),
    )
    assert f"contradiction_without_conflict_group:{NEW_CLAIM_ID}" in plan.warnings


def test_a_conflict_group_mismatch_is_reported():
    plan = plan_semantic_write(
        _request(
            node=_new_claim(conflict_group="conflict_index_shard_count"),
            contradicts=[_contradicts()],
        ),
        existing_nodes=_existing_nodes(),
    )
    assert f"conflict_group_mismatch:{RIVAL_CLAIM_ID}" in plan.warnings


def test_contradicting_a_node_that_is_not_a_claim_is_reported():
    plan = _plan(contradicts=[_contradicts(target=ENTITY_ID)])
    assert f"unexpected_contradiction_kind:{ENTITY_ID}:entity" in plan.warnings


# ===========================================================================
# E. Additional explicit assertions are preserved verbatim
# ===========================================================================

def test_additional_assertions_keep_their_relation_type_and_payload():
    plan = _plan(
        assertions=[
            _evidenced_by(),
            _about(),
            RelationAssertion(
                relation_type=RelationType.DERIVED_FROM,
                target_id=EPISODE_ID,
                confidence=0.6,
                valid_from=T1,
                attributes={"step": "index_maintenance_review"},
            ),
        ]
    )
    edges = [event.edge for event in plan.events if event.edge is not None]
    assert [(edge.relation_type, edge.target_id) for edge in edges] == [
        (RelationType.ABOUT, ENTITY_ID),
        (RelationType.DERIVED_FROM, EPISODE_ID),
        (RelationType.EVIDENCED_BY, EVIDENCE_ID),
    ]
    derived = edges[1]
    assert derived.confidence == 0.6
    assert derived.valid_from == T1
    assert derived.attributes == {"step": "index_maintenance_review"}
    assert derived.recorded_at == T1
    assert plan.relation_counts == {
        "ABOUT": 1,
        "DERIVED_FROM": 1,
        "EVIDENCED_BY": 1,
    }


def test_an_already_asserted_edge_is_reported_rather_than_silently_replaced():
    existing = GraphEdge(
        id=make_edge_id(RelationType.ABOUT, NEW_CLAIM_ID, ENTITY_ID),
        relation_type=RelationType.ABOUT,
        source_id=NEW_CLAIM_ID,
        target_id=ENTITY_ID,
        recorded_at=T0,
    )
    plan = plan_semantic_write(
        _request(assertions=[_about()]),
        existing_nodes=_existing_nodes(),
        existing_edges=[existing],
    )
    assert plan.warnings == [f"edge_already_asserted:{existing.id}"]


def test_a_node_that_already_exists_is_reported():
    plan = plan_semantic_write(
        _request(node=_old_claim()),
        existing_nodes=_existing_nodes(),
    )
    assert plan.warnings == [f"node_already_present:{OLD_CLAIM_ID}"]


# ===========================================================================
# F. Determinism
# ===========================================================================

def test_replanning_the_same_request_is_byte_identical():
    request = _request(
        supersedes=[_supersedes()],
        contradicts=[_contradicts()],
        assertions=[_about(), _evidenced_by()],
    )
    first = plan_semantic_write(request, existing_nodes=_existing_nodes())
    second = plan_semantic_write(request, existing_nodes=_existing_nodes())
    assert second.to_json() == first.to_json()


def test_two_equal_requests_built_separately_plan_identically():
    """Nothing in the plan may come from a UUID, a clock or an object address."""
    assert (
        _plan(supersedes=[_supersedes()]).to_json()
        == _plan(supersedes=[_supersedes()]).to_json()
    )


def test_the_plan_does_not_depend_on_declaration_order():
    forward = _plan(
        assertions=[_about(), _evidenced_by()],
        supersedes=[_supersedes(), _supersedes(target=RIVAL_CLAIM_ID)],
    )
    backward = _plan(
        assertions=[_evidenced_by(), _about()],
        supersedes=[_supersedes(target=RIVAL_CLAIM_ID), _supersedes()],
    )
    assert backward.to_json() == forward.to_json()


def test_the_plan_does_not_depend_on_the_node_mapping_insertion_order():
    request = _request(supersedes=[_supersedes()], assertions=[_about()])
    forward = plan_semantic_write(
        request, existing_nodes={node.id: node for node in _existing_nodes()}
    )
    backward = plan_semantic_write(
        request,
        existing_nodes={
            node.id: node for node in reversed(_existing_nodes())
        },
    )
    assert backward.to_json() == forward.to_json()


def test_adding_a_second_target_does_not_renumber_the_first_ones_event_id():
    one = _plan(supersedes=[_supersedes()])
    two = _plan(supersedes=[_supersedes(), _supersedes(target=RIVAL_CLAIM_ID)])
    assert one.relations[0].event_id in {
        decision.event_id for decision in two.relations
    }
    assert one.node_event_id == two.node_event_id


def test_the_plan_serializes_to_deterministic_plain_json():
    plan = _plan(
        supersedes=[_supersedes()],
        contradicts=[_contradicts()],
        assertions=[_about()],
    )
    payload = plan.to_dict()
    _assert_plain_json(payload)
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        json.loads(plan.to_json()), sort_keys=True
    )


def test_every_generated_event_serializes_to_plain_json():
    plan = _plan(supersedes=[_supersedes()], contradicts=[_contradicts()])
    for event in plan.events:
        _assert_plain_json(event.to_dict())


def test_generated_events_round_trip_through_the_graph_model():
    plan = _plan(supersedes=[_supersedes()], contradicts=[_contradicts()])
    for event in plan.events:
        assert MemoryEvent.model_validate(json.loads(event.to_json())) == event


# ===========================================================================
# G. Projection through the store's own rebuild contract
# ===========================================================================

def test_projection_preserves_the_explicit_source_event_stream():
    store = InMemoryGraphStore()
    source = _source_events()
    projection = project_semantic_write(
        store, source, _request(supersedes=[_supersedes()], assertions=[_about()])
    )
    assert projection.source_events_read == len(source)
    assert projection.report.events_read == len(source) + len(projection.plan.events)
    for node in _existing_nodes():
        assert store.get_node(node.id) is not None
    assert projection.events_projected == projection.report.events_read


def test_projection_reaches_only_rebuild():
    store = _RecordingStore()
    project_semantic_write(
        store,
        _source_events(),
        _request(supersedes=[_supersedes()], contradicts=[_contradicts()]),
    )
    assert store.calls == ["rebuild"]
    assert SEMANTIC_WRITE_OPERATIONS == ("rebuild",)


def test_an_invalid_request_never_reaches_the_store():
    store = _RecordingStore()
    with pytest.raises(ValueError):
        project_semantic_write(
            store,
            _source_events(),
            _request(supersedes=[_supersedes(target=UNKNOWN_CLAIM_ID)]),
        )
    assert store.calls == []


def test_a_store_without_rebuild_is_refused():
    class _ReadOnly:
        def get_node(self, node_id):        # pragma: no cover - never reached
            return None

    with pytest.raises(ValueError, match="rebuild"):
        project_semantic_write(_ReadOnly(), _source_events(), _request())


def test_projection_derives_target_existence_from_the_source_events():
    """No explicit mapping needed: the stream the caller is projecting *is* the
    material, so a missing target is still an error rather than a warning."""
    store = _RecordingStore()
    with pytest.raises(ValueError, match=UNKNOWN_CLAIM_ID):
        project_semantic_write(
            store,
            _source_events(),
            _request(contradicts=[_contradicts(target=UNKNOWN_CLAIM_ID)]),
        )
    assert store.calls == []
    # ...and a target the stream does carry is accepted without a warning.
    projection = project_semantic_write(
        InMemoryGraphStore(), _source_events(), _request(contradicts=[_contradicts()])
    )
    assert projection.plan.warnings == []


def test_projection_is_idempotent():
    request = _request(supersedes=[_supersedes()], contradicts=[_contradicts()])
    store = InMemoryGraphStore()
    first = project_semantic_write(store, _source_events(), request)
    snapshot = store.snapshot()
    second = project_semantic_write(store, _source_events(), request)
    assert store.snapshot() == snapshot
    assert second.to_json() == first.to_json()


def test_projecting_a_stream_that_already_contains_the_write_changes_nothing():
    request = _request(supersedes=[_supersedes()])
    store = InMemoryGraphStore()
    first = project_semantic_write(store, _source_events(), request)
    snapshot = store.snapshot()

    replayed = project_semantic_write(
        store, [*_source_events(), *first.plan.events], request
    )
    assert store.snapshot() == snapshot
    assert replayed.report.errors == []
    assert any(
        warning.startswith("duplicate_event:")
        for warning in replayed.report.warnings
    )


def test_a_rebuild_that_reports_errors_is_returned_with_them():
    """§10.3: a projection over a broken ledger is not a clean write."""
    dangling = GraphEdge(
        id=make_edge_id(RelationType.ABOUT, OLD_CLAIM_ID, UNKNOWN_CLAIM_ID),
        relation_type=RelationType.ABOUT,
        source_id=OLD_CLAIM_ID,
        target_id=UNKNOWN_CLAIM_ID,
        recorded_at=T0,
    )
    source = [
        *_source_events(),
        _event(MemoryEventKind.INJECT, "source_0009", T0, edge=dangling),
    ]
    store = InMemoryGraphStore()
    projection = project_semantic_write(
        store, source, _request(supersedes=[_supersedes()])
    )
    assert projection.ok is False
    assert any(
        error.startswith("unresolved_edge_endpoint:")
        for error in projection.report.errors
    )
    assert store.health().healthy is False


def test_a_raising_rebuild_is_surfaced_rather_than_reported_as_success():
    with pytest.raises(SemanticWriteError, match="RuntimeError"):
        project_semantic_write(
            _RaisingRebuildStore(), _source_events(), _request()
        )


def test_the_projection_result_serializes_to_plain_json():
    projection = project_semantic_write(
        InMemoryGraphStore(),
        _source_events(),
        _request(supersedes=[_supersedes()]),
    )
    assert isinstance(projection, SemanticWriteProjection)
    _assert_plain_json(projection.to_dict())


def test_two_backends_project_the_same_write_identically():
    request = _request(supersedes=[_supersedes()], contradicts=[_contradicts()])
    reference = InMemoryGraphStore()
    reference_report = project_semantic_write(reference, _source_events(), request)
    with SQLiteGraphStore(":memory:") as embedded:
        embedded_report = project_semantic_write(
            embedded, _source_events(), request
        )
        assert embedded_report.to_json() == reference_report.to_json()
        assert (
            embedded.get_node(OLD_CLAIM_ID).to_json()
            == reference.get_node(OLD_CLAIM_ID).to_json()
        )
        assert embedded.health().healthy is True


# ===========================================================================
# H. Isolation: no filesystem, no config, no hot path
# ===========================================================================

#: Everything :mod:`layered_memory_mcp.graph_write` is allowed to import. The
#: list is the boundary §12 Batch G sets: the graph domain, the store contract,
#: and the standard library. No storage module, no config, no MCP entrypoint,
#: and no third-party dependency other than the pydantic the models already use.
_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "datetime",
    "hashlib",
    "pydantic",
    "typing",
    ".graph_models",
    ".graph_store",
}


def _declared_imports(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_the_write_seam_imports_nothing_outside_the_graph_boundary():
    declared = _declared_imports(Path(graph_write.__file__))
    assert declared <= _ALLOWED_IMPORTS, sorted(declared - _ALLOWED_IMPORTS)


def test_planning_and_projection_open_no_file(monkeypatch):
    import builtins
    import pathlib

    def _refuse(*args, **kwargs):
        raise AssertionError("the semantic write seam must not open any file")

    monkeypatch.setattr(builtins, "open", _refuse)
    monkeypatch.setattr(pathlib.Path, "open", _refuse)

    project_semantic_write(
        InMemoryGraphStore(),
        _source_events(),
        _request(supersedes=[_supersedes()], contradicts=[_contradicts()]),
    )


def test_planning_reads_no_environment_variable(monkeypatch):
    import os

    def _refuse(*args, **kwargs):
        raise AssertionError("the semantic write seam must not read the env")

    monkeypatch.setattr(os.environ, "get", _refuse)
    _plan(supersedes=[_supersedes()], contradicts=[_contradicts()])

"""The semantic graph write seam — one explicit decision, one event sequence.

WHAT THIS MODULE IS
-------------------
The backend-neutral write half of ``ARCHITECTURE_v2.md`` §8: it turns an
*explicitly supplied* semantic write decision — this new node, these superseded
targets, these contradicted targets, these additional relations — into the
deterministic, append-only :class:`~layered_memory_mcp.graph_models.MemoryEvent`
sequence that §4 projects graph state from, and optionally projects that
sequence through a supplied :class:`~layered_memory_mcp.graph_store.GraphStore`
using its existing ``rebuild()`` contract.

Two public steps, deliberately separable:

* :func:`plan_semantic_write` — pure. Validates the decision and returns a
  :class:`SemanticWritePlan`: the events, the relation decisions, and an honest
  warning list. It touches nothing.
* :func:`project_semantic_write` — applies a plan by calling exactly one store
  operation, ``rebuild``, over the caller's own source events plus the generated
  ones, and returns the plan beside the store's own
  :class:`~layered_memory_mcp.graph_store.RebuildReport`.

WHAT IT IS NOT
--------------
- NOT wired into the default write path. Nothing imports this module yet (§12
  Batch G validates write semantics; the flagged cutover is Batch H), and it
  imports neither ``injector``, ``recall``, ``server`` nor any storage module.
- NOT an extractor, an entity resolver or a conflict detector. There is no text
  comparison, no similarity, no LLM call and no candidate discovery anywhere in
  this file (§3.2). The caller supplies the semantics and the exact target IDs;
  a future extraction/decision layer may *call* this seam, and will then own
  that judgement where it can be reviewed.
- NOT a second write surface. ``create_node`` and ``append_edge`` are never
  called: the only way graph state changes here is a projection of an event
  stream, which is what keeps the ledger the single source of truth (§8.1).
- NOT a discovery mechanism. It reads no file, no config, no environment and no
  global state. Target existence is checked *only* against node material the
  caller passed in (or, in :func:`project_semantic_write`, against the source
  event stream the caller is projecting).

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Supersession is two effects, never one** (§8.2). A supersession decision
   emits both ``new --SUPERSEDES--> old`` *and* a ``SUPERSEDE`` lifecycle event
   for the old node, so a projection cannot end with an asserted replacement
   whose target is still active. Nothing is deleted: the old claim keeps its
   payload and its evidence and stays queryable as history.
2. **Contradiction settles nothing** (§8.3). A contradiction decision emits a
   ``CONTRADICTS`` edge and no lifecycle event, so both claims stay active and
   the conflict stays reportable. Retiring one side takes a *separate*,
   explicit supersession decision in the same request.
3. **Identity is derived, never minted.** Event IDs are
   ``event_<sha256(namespace, request_key, role, subject)[:16]>`` and edge IDs
   come from :func:`~layered_memory_mcp.graph_models.make_edge_id`. No UUID, no
   clock reading, no object address and no list position goes into an ID, so
   replanning the same request is byte-identical and projecting it twice is a
   no-op.
4. **Order is a timestamp, not a list position.** ``rebuild`` applies events in
   ``(occurred_at, id)`` order, so the node that the generated edges and
   transitions depend on has to sort *first* by time. The plan therefore lays
   its events out as a one-microsecond-per-event sequence after the request
   timestamp — node at ``occurred_at``, each generated edge and then each
   transition one microsecond later, in a sorted order that never depends on
   the caller's declaration order or on a mapping's insertion order.
5. **Honesty over convenience** (§10.3). An input a projection could not carry
   safely is a :class:`ValueError` — a missing target, a self-target, an
   ambiguous declaration, a relation type that does not match its decision. A
   fact the caller should know but that does not make the write unsafe is a
   sorted warning: a target that cannot take the transition, a node or edge
   that is already present, a contradiction without a ``conflict_group``.
   Nothing invalid is hidden behind a warning, and a failed plan never reaches
   the store.

PRIVACY
-------
Warnings and event metadata carry graph IDs, relation type names, node kinds and
lifecycle statuses only — never a node body, a quote, a source path or a store
diagnostic (§15). The new node's payload is passed through exactly as the caller
built it; this module never adds a source ref, an evidence node or a derivation
record the caller did not supply.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any, Iterable, Mapping, Sequence

from pydantic import Field, field_validator, model_validator

from .graph_models import (
    PROVENANCE_REQUIRED_RELATIONS,
    Derivation,
    GraphEdge,
    GraphModel,
    GraphNode,
    GraphScope,
    MemoryEvent,
    MemoryEventKind,
    NodeKind,
    NodeStatus,
    RelationType,
    SourceRef,
    Timestamp,
    clean_identifier,
    clean_text_line,
    make_edge_id,
)
from .graph_store import GraphStore, RebuildReport

#: Default identity namespace for generated event IDs. Part of the hashed
#: identity, so a caller running two independent write pipelines over the same
#: node identities can keep their ledgers apart by changing it.
SEMANTIC_WRITE_EVENT_NAMESPACE = "semantic_write"

#: Prefix on a generated event ID, so an ID is recognizable in a fixture diff.
SEMANTIC_WRITE_EVENT_PREFIX = "event_"

#: The only store operation this seam calls. Deliberately a *subset* of
#: :data:`~layered_memory_mcp.graph_store.REQUIRED_GRAPH_STORE_OPERATIONS`:
#: "did the write path mutate the graph outside the ledger?" is then answerable
#: by inspection rather than by convention (§8.1).
SEMANTIC_WRITE_OPERATIONS: tuple[str, ...] = ("rebuild",)

#: The relations that are a *decision about another memory* rather than a plain
#: assertion, and therefore have their own request field: a ``SUPERSEDES`` edge
#: without its lifecycle event, or a ``CONTRADICTS`` edge smuggled in as an
#: ordinary assertion, are exactly the half-applied states §8.2/§8.3 forbid.
DECISION_RELATIONS: tuple[RelationType, ...] = (
    RelationType.SUPERSEDES,
    RelationType.CONTRADICTS,
)

#: Node kinds that may supersede another memory (§6.2 ``SUPERSEDES``: "New
#: Claim/Policy/Procedure -> Old node"). ``EXPERIENCE`` is included because a
#: revised lesson replaces its earlier form the same way. Anything else is
#: *reported*, not refused — the caller owns the semantics, and an unusual
#: source kind is a review signal rather than a corruption.
SUPERSESSION_SOURCE_KINDS: tuple[NodeKind, ...] = (
    NodeKind.CLAIM,
    NodeKind.POLICY,
    NodeKind.PROCEDURE,
    NodeKind.EXPERIENCE,
)

#: The kinds §6.2 gives ``CONTRADICTS`` ("Claim -> Claim"). Also reported rather
#: than refused.
CONTRADICTION_KINDS: tuple[NodeKind, ...] = (NodeKind.CLAIM,)

#: The statuses a ``SUPERSEDE`` transition applies from (§8.2). A target outside
#: them keeps its decision — the caller asked for it — but earns a warning,
#: because the projection will report an ``ignored_transition`` for it.
SUPERSEDABLE_STATUSES: tuple[NodeStatus, ...] = (
    NodeStatus.ACTIVE,
    NodeStatus.PENDING,
)

#: Separator for hashed identity parts, matching ``graph_models``: not a legal
#: character in an identifier, so two different part lists cannot collide.
_IDENTITY_SEPARATOR = "\x1f"

#: Hex characters kept from an event ID digest. 64 bits, as for node/edge IDs.
_ID_DIGEST_LENGTH = 16

#: The three roles an event plays in one semantic write. Part of the hashed
#: event identity, so the node event, the edge event and the transition event
#: for one decision can never collide.
ROLE_NODE = "node"
ROLE_EDGE = "edge"
ROLE_SUPERSEDE = "supersede"


class SemanticWriteError(RuntimeError):
    """A projection failure a caller must not read as a successful write.

    Raised only when the supplied store's ``rebuild`` itself failed. A rebuild
    that *reports* problems is not an exception: its
    :class:`~layered_memory_mcp.graph_store.RebuildReport` comes back with its
    errors intact (see :attr:`SemanticWriteProjection.ok`), because losing the
    good events with the bad is the §10.2 behavior this seam inherits.
    """


def _semantic_event_id(
    namespace: str, request_key: str, role: str, subject: str
) -> str:
    """Build a stable event ID from the write decision's identity.

    The parts are the *decision*, never its position: adding a second
    supersession target cannot renumber the first one's event, and replaying a
    request produces the same IDs on every run (§10.2). ``role`` keeps the node
    event, an edge event and a transition event distinct even when they share a
    subject.
    """
    digest = hashlib.sha256(
        _IDENTITY_SEPARATOR.join([namespace, request_key, role, subject]).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{SEMANTIC_WRITE_EVENT_PREFIX}{digest[:_ID_DIGEST_LENGTH]}"


# ---------------------------------------------------------------------------
# The decision shapes
# ---------------------------------------------------------------------------

class RelationAssertion(GraphModel):
    """One explicit edge the caller decided to assert from the new node.

    The same shape serves all three request fields, and ``relation_type`` is
    required in every one of them: a supersession declared as ``RELATED_TO``
    would project a weak edge where a lifecycle decision was meant, so
    :class:`SemanticWriteRequest` checks that the type matches the field it was
    declared in rather than rewriting it.

    ``discriminator`` is passed through to
    :func:`~layered_memory_mcp.graph_models.make_edge_id`, which is how two
    genuinely distinct assertions of one triple — typically different validity
    windows — get distinct edge IDs instead of colliding.

    Provenance is checked here, at declaration time, with the model's own
    :data:`~layered_memory_mcp.graph_models.PROVENANCE_REQUIRED_RELATIONS` list:
    a strong assertion that cannot say why it was made is refused before any
    event exists.
    """

    relation_type: RelationType
    target_id: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    valid_from: Timestamp | None = None
    valid_to: Timestamp | None = None
    evidence_refs: list[SourceRef] = Field(default_factory=list)
    derivation: Derivation | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    discriminator: str = ""

    @field_validator("target_id")
    @classmethod
    def _clean_target(cls, value: str) -> str:
        return clean_identifier(value, "target_id")

    @field_validator("discriminator")
    @classmethod
    def _clean_discriminator(cls, value: str) -> str:
        return clean_text_line(value, "discriminator")

    @model_validator(mode="after")
    def _check_state(self) -> RelationAssertion:
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        if self.relation_type in PROVENANCE_REQUIRED_RELATIONS and not (
            self.evidence_refs or self.derivation
        ):
            raise ValueError(
                f"a {self.relation_type.value} assertion requires at least one "
                "evidence ref or an explicit derivation record"
            )
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        """The triple that must be unique within one request."""
        return (self.relation_type.value, self.target_id, self.discriminator)


class SemanticWriteRequest(GraphModel):
    """One explicit semantic write decision, validated before anything runs.

    Fields:

    * ``node`` — the new semantic node, already built and already provenance-
      valid by :class:`~layered_memory_mcp.graph_models.GraphNode`'s own rules.
      It is carried through untouched.
    * ``occurred_at`` — when the decision was made. It is the ledger position of
      the whole write; the generated events sit at it and in the microseconds
      after it.
    * ``request_key`` — the caller-supplied stable key the event IDs are hashed
      from. Defaults to the node's ID, which is already stable for a source
      identity; pass a write/episode key instead when one node is written by two
      distinguishable decisions.
    * ``event_namespace`` — identity namespace for the same hash.
    * ``scope`` — the scope stamped on the generated events. Defaults to the
      node's own scope, so events do not silently widen it.
    * ``assertions`` — additional explicit edges from the new node. Their
      relation type is preserved exactly; nothing is inferred and nothing is
      added.
    * ``supersedes`` / ``contradicts`` — the lifecycle decisions, each entry a
      :class:`RelationAssertion` whose relation type must match its field.

    What it refuses (all :class:`ValueError`): a blank or multi-line target, a
    target that is the new node itself, one triple declared twice, one target
    declared both superseded and contradicted, a relation type that does not
    match the field it was declared in, and a ``SUPERSEDES``/``CONTRADICTS``
    edge smuggled into ``assertions`` — where it would project without its
    lifecycle or conflict meaning.
    """

    node: GraphNode
    occurred_at: Timestamp
    request_key: str = ""
    event_namespace: str = SEMANTIC_WRITE_EVENT_NAMESPACE
    scope: GraphScope | None = None
    assertions: list[RelationAssertion] = Field(default_factory=list)
    supersedes: list[RelationAssertion] = Field(default_factory=list)
    contradicts: list[RelationAssertion] = Field(default_factory=list)

    @field_validator("event_namespace")
    @classmethod
    def _clean_namespace(cls, value: str) -> str:
        return clean_identifier(value, "event_namespace")

    @model_validator(mode="after")
    def _check_decision(self) -> SemanticWriteRequest:
        self.request_key = (
            clean_identifier(self.request_key, "request_key")
            if self.request_key.strip()
            else self.node.id
        )

        for assertion in self.supersedes:
            if assertion.relation_type is not RelationType.SUPERSEDES:
                raise ValueError(
                    "a supersession decision must be declared as SUPERSEDES, "
                    f"got {assertion.relation_type.value}"
                )
        for assertion in self.contradicts:
            if assertion.relation_type is not RelationType.CONTRADICTS:
                raise ValueError(
                    "a contradiction decision must be declared as CONTRADICTS, "
                    f"got {assertion.relation_type.value}"
                )
        for assertion in self.assertions:
            if assertion.relation_type not in DECISION_RELATIONS:
                continue
            if assertion.relation_type is RelationType.SUPERSEDES:
                raise ValueError(
                    "a SUPERSEDES edge must be declared in the 'supersedes' "
                    "field, so the lifecycle transition is emitted with it"
                )
            raise ValueError(
                "a CONTRADICTS edge must be declared in the 'contradicts' "
                "field, so the conflict is recorded as unresolved"
            )

        seen: set[tuple[str, str, str]] = set()
        for assertion in self.declared:
            if assertion.target_id == self.node.id:
                raise ValueError(
                    "a semantic write may not assert a relation from the new "
                    "node to itself"
                )
            if assertion.identity in seen:
                raise ValueError(
                    f"relation {assertion.relation_type.value} -> "
                    f"{assertion.target_id} is declared twice; pass a "
                    "discriminator if two distinct assertions are meant"
                )
            seen.add(assertion.identity)

        both = {a.target_id for a in self.supersedes} & {
            a.target_id for a in self.contradicts
        }
        if both:
            raise ValueError(
                "a target may not be both superseded and contradicted by one "
                f"write: {', '.join(sorted(both))}"
            )
        return self

    @property
    def declared(self) -> list[RelationAssertion]:
        """Every declared assertion, in field order (not projection order)."""
        return [*self.assertions, *self.supersedes, *self.contradicts]

    @property
    def target_ids(self) -> list[str]:
        """Every explicitly named target, sorted and deduplicated."""
        return sorted({assertion.target_id for assertion in self.declared})


# ---------------------------------------------------------------------------
# The plan shapes
# ---------------------------------------------------------------------------

class RelationDecision(GraphModel):
    """What one declared assertion became, named by stable IDs.

    ``transition_event_id`` is set only for a supersession: it is the proof that
    the §8.2 pair was emitted together, and its absence on a contradiction is
    the proof that nothing was settled (§8.3).
    """

    relation_type: RelationType
    target_id: str
    edge_id: str
    event_id: str
    transition_event_id: str | None = None


class SemanticWritePlan(GraphModel):
    """The deterministic event sequence one write decision projects to.

    ``events`` is already in ``(occurred_at, id)`` order — the order
    ``rebuild`` will apply them in — with the node first and the lifecycle
    transitions last. Every collection is sorted by a stable graph identifier,
    so :meth:`~layered_memory_mcp.graph_models.GraphModel.to_json` is a valid
    fixture assertion.

    ``warnings`` is the honest remainder: things the caller should see that do
    not make the write unsafe. Anything that *would* make it unsafe raised
    instead, so an empty ``warnings`` never has to be read as "probably fine".
    """

    request_key: str
    node_id: str
    node_event_id: str
    occurred_at: Timestamp
    events: list[MemoryEvent] = Field(default_factory=list)
    relations: list[RelationDecision] = Field(default_factory=list)
    superseded_ids: list[str] = Field(default_factory=list)
    contradicted_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def event_ids(self) -> list[str]:
        return [event.id for event in self.events]

    @property
    def edge_ids(self) -> list[str]:
        return [decision.edge_id for decision in self.relations]

    @property
    def transition_event_ids(self) -> list[str]:
        return [
            decision.transition_event_id
            for decision in self.relations
            if decision.transition_event_id is not None
        ]

    @property
    def relation_counts(self) -> dict[str, int]:
        """How many edges per relation type, by relation type name."""
        counts: dict[str, int] = {}
        for decision in self.relations:
            name = decision.relation_type.value
            counts[name] = counts.get(name, 0) + 1
        return {name: counts[name] for name in sorted(counts)}


class SemanticWriteProjection(GraphModel):
    """A plan and what the store's own ``rebuild`` made of it.

    The report is the store's, verbatim: :attr:`ok` is false whenever the
    rebuild named an error, so a projection over a broken ledger cannot read as
    a clean write (§10.3).
    """

    plan: SemanticWritePlan
    report: RebuildReport
    #: How many explicit source events the caller supplied, before the generated
    #: ones were appended. The source stream is always projected with the write.
    source_events_read: int = Field(default=0, ge=0)

    @property
    def ok(self) -> bool:
        return self.report.ok

    @property
    def events_projected(self) -> int:
        return self.source_events_read + len(self.plan.events)


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------

def plan_semantic_write(
    request: SemanticWriteRequest,
    *,
    existing_nodes: Mapping[str, GraphNode] | Iterable[GraphNode] | None = None,
    existing_edges: Mapping[str, GraphEdge] | Iterable[GraphEdge] | None = None,
) -> SemanticWritePlan:
    """Turn one explicit write decision into its event sequence (§4, §8).

    Emits, in this order:

    1. one ``INJECT`` event carrying the new node, at ``request.occurred_at``;
    2. one ``INJECT`` event per declared assertion — including the explicit
       ``SUPERSEDES`` and ``CONTRADICTS`` edges — sorted by
       ``(relation type, target, discriminator)``, one microsecond apart;
    3. one ``SUPERSEDE`` event per superseded target, sorted by target ID, after
       every edge event.

    The microsecond sequence is what makes the stream safe to hand to
    ``rebuild``, which applies events by ``(occurred_at, id)``: the node exists
    in the projection before the edges that reference it and before the
    transitions, without depending on the caller's list order.

    Args:
        request: The explicit decision. Already validated by its own model.
        existing_nodes: The node material to check targets against — a mapping
            keyed by node ID or any iterable of nodes. This is the *only* way
            targets are verified; no store is read, no file is opened and
            nothing is discovered. ``None`` means "no material supplied", which
            is reported as ``target_existence_unchecked:<count>`` rather than
            silently treated as verified.
        existing_edges: Edges already asserted, used only to report
            ``edge_already_asserted:<id>``.

    Returns:
        A :class:`SemanticWritePlan` whose events are in projection order.

    Raises:
        ValueError: For a caller mistake a projection could not carry safely — a
            target that is not in the supplied material, a duplicated or
            mismatched ``existing_nodes`` key, or any of the declaration
            problems :class:`SemanticWriteRequest` refuses.
    """
    nodes = _normalize_nodes(existing_nodes)
    edges = _normalize_edges(existing_edges)
    node = request.node
    warnings: set[str] = set()

    _check_targets(request, nodes, warnings)
    _check_lifecycle(request, nodes, warnings)

    scope = request.scope if request.scope is not None else node.scope
    node_event_id = _semantic_event_id(
        request.event_namespace, request.request_key, ROLE_NODE, node.id
    )
    events: list[MemoryEvent] = [
        MemoryEvent(
            id=node_event_id,
            kind=MemoryEventKind.INJECT,
            occurred_at=request.occurred_at,
            scope=scope,
            node=node.model_copy(deep=True),
            metadata=_metadata(request, ROLE_NODE),
        )
    ]

    relations: list[RelationDecision] = []
    transitions: dict[str, str] = {}
    ordinal = 0

    for assertion in sorted(request.declared, key=lambda a: a.identity):
        edge = _build_edge(request, assertion)
        if edge.id in edges:
            warnings.add(f"edge_already_asserted:{edge.id}")
        ordinal += 1
        event_id = _semantic_event_id(
            request.event_namespace, request.request_key, ROLE_EDGE, edge.id
        )
        events.append(
            MemoryEvent(
                id=event_id,
                kind=MemoryEventKind.INJECT,
                occurred_at=request.occurred_at + timedelta(microseconds=ordinal),
                scope=scope,
                edge=edge,
                metadata=_metadata(request, ROLE_EDGE),
            )
        )
        transition_event_id: str | None = None
        if assertion.relation_type is RelationType.SUPERSEDES:
            transition_event_id = _semantic_event_id(
                request.event_namespace,
                request.request_key,
                ROLE_SUPERSEDE,
                assertion.target_id,
            )
            transitions[assertion.target_id] = transition_event_id
        relations.append(
            RelationDecision(
                relation_type=assertion.relation_type,
                target_id=assertion.target_id,
                edge_id=edge.id,
                event_id=event_id,
                transition_event_id=transition_event_id,
            )
        )

    for target_id in sorted(transitions):
        ordinal += 1
        events.append(
            MemoryEvent(
                id=transitions[target_id],
                kind=MemoryEventKind.SUPERSEDE,
                occurred_at=request.occurred_at + timedelta(microseconds=ordinal),
                scope=scope,
                target_id=target_id,
                metadata={
                    **_metadata(request, ROLE_SUPERSEDE),
                    "superseded_by": node.id,
                },
            )
        )

    return SemanticWritePlan(
        request_key=request.request_key,
        node_id=node.id,
        node_event_id=node_event_id,
        occurred_at=request.occurred_at,
        events=events,
        relations=relations,
        superseded_ids=sorted(a.target_id for a in request.supersedes),
        contradicted_ids=sorted(a.target_id for a in request.contradicts),
        warnings=sorted(warnings),
    )


def _metadata(request: SemanticWriteRequest, role: str) -> dict[str, Any]:
    """Event bookkeeping: which decision produced it, and what part it plays.

    IDs and role names only — never a node body or a source path (§15).
    """
    return {"request_key": request.request_key, "role": role}


def _build_edge(
    request: SemanticWriteRequest, assertion: RelationAssertion
) -> GraphEdge:
    """Build the edge one assertion asserts, preserving it exactly.

    The relation type, confidence, validity window, provenance and attributes
    are the caller's; only the ID, the endpoints and ``recorded_at`` are
    derived — from the triple, the new node and the request timestamp, so the
    payload carries no clock reading.
    """
    declared = assertion.model_copy(deep=True)
    return GraphEdge(
        id=make_edge_id(
            declared.relation_type,
            request.node.id,
            declared.target_id,
            discriminator=declared.discriminator,
        ),
        relation_type=declared.relation_type,
        source_id=request.node.id,
        target_id=declared.target_id,
        confidence=declared.confidence,
        valid_from=declared.valid_from,
        valid_to=declared.valid_to,
        recorded_at=request.occurred_at,
        evidence_refs=declared.evidence_refs,
        derivation=declared.derivation,
        attributes=declared.attributes,
    )


# ---------------------------------------------------------------------------
# Validation against explicitly supplied material
# ---------------------------------------------------------------------------

def _normalize_nodes(
    existing_nodes: Mapping[str, GraphNode] | Iterable[GraphNode] | None,
) -> dict[str, GraphNode] | None:
    """Index supplied node material by ID, refusing an ambiguous mapping.

    A mapping key that disagrees with its node's own ID, and the same ID listed
    twice, are both refused: either would make the answer depend on which entry
    happened to win, which is exactly the insertion-order dependence the plan
    may not have.
    """
    if existing_nodes is None:
        return None
    indexed: dict[str, GraphNode] = {}
    if isinstance(existing_nodes, Mapping):
        for key, node in existing_nodes.items():
            if key != node.id:
                raise ValueError(
                    f"existing_nodes key {key!r} does not match the node's own "
                    f"id {node.id!r}"
                )
            indexed[node.id] = node
        return indexed
    for node in existing_nodes:
        if node.id in indexed:
            raise ValueError(f"existing_nodes lists node {node.id!r} twice")
        indexed[node.id] = node
    return indexed


def _normalize_edges(
    existing_edges: Mapping[str, GraphEdge] | Iterable[GraphEdge] | None,
) -> dict[str, GraphEdge]:
    """Index supplied edge material by ID. Absent material is simply empty."""
    if existing_edges is None:
        return {}
    if isinstance(existing_edges, Mapping):
        for key, edge in existing_edges.items():
            if key != edge.id:
                raise ValueError(
                    f"existing_edges key {key!r} does not match the edge's own "
                    f"id {edge.id!r}"
                )
        return dict(existing_edges)
    return {edge.id: edge for edge in existing_edges}


def _check_targets(
    request: SemanticWriteRequest,
    nodes: dict[str, GraphNode] | None,
    warnings: set[str],
) -> None:
    """Refuse a write whose targets are not in the supplied material.

    A missing target is an error rather than a dangling-but-successful write:
    the projection would keep the edge and report an unresolved endpoint, which
    leaves the graph permanently unhealthy for something the caller could have
    fixed here.
    """
    targets = request.target_ids
    if nodes is None:
        if targets:
            warnings.add(f"target_existence_unchecked:{len(targets)}")
        return
    missing = [target for target in targets if target not in nodes]
    if missing:
        raise ValueError(
            "semantic write targets are not present in the supplied node "
            f"material: {', '.join(missing)}"
        )
    if request.node.id in nodes:
        warnings.add(f"node_already_present:{request.node.id}")


def _check_lifecycle(
    request: SemanticWriteRequest,
    nodes: dict[str, GraphNode] | None,
    warnings: set[str],
) -> None:
    """Report §8.2/§8.3 oddities without refusing the caller's decision.

    Each of these is a review signal, not a corruption: an unusual source kind,
    a target that cannot take the transition, a contradiction with no
    ``conflict_group`` to file it under, and a contradiction between claims
    filed under *different* groups — which would hide the conflict from a
    group-based scan.
    """
    node = request.node
    if request.supersedes and node.kind not in SUPERSESSION_SOURCE_KINDS:
        warnings.add(f"unexpected_supersession_source_kind:{node.kind.value}")
    if request.contradicts:
        if not node.conflict_group:
            warnings.add(f"contradiction_without_conflict_group:{node.id}")
        if node.kind not in CONTRADICTION_KINDS:
            warnings.add(
                f"unexpected_contradiction_kind:{node.id}:{node.kind.value}"
            )
    if nodes is None:
        return
    for assertion in request.supersedes:
        status = nodes[assertion.target_id].status
        if status not in SUPERSEDABLE_STATUSES:
            warnings.add(
                f"supersede_target_not_current:{assertion.target_id}:"
                f"{status.value}"
            )
    for assertion in request.contradicts:
        target = nodes[assertion.target_id]
        if target.kind not in CONTRADICTION_KINDS:
            warnings.add(
                f"unexpected_contradiction_kind:{target.id}:{target.kind.value}"
            )
        elif node.conflict_group and target.conflict_group != node.conflict_group:
            warnings.add(f"conflict_group_mismatch:{target.id}")


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def project_semantic_write(
    store: GraphStore,
    source_events: Sequence[MemoryEvent],
    request: SemanticWriteRequest,
    *,
    existing_nodes: Mapping[str, GraphNode] | Iterable[GraphNode] | None = None,
    existing_edges: Mapping[str, GraphEdge] | Iterable[GraphEdge] | None = None,
) -> SemanticWriteProjection:
    """Project one semantic write through the store's own ``rebuild`` (§8.1).

    The explicit source stream is projected *with* the write — ``rebuild`` is a
    rebuild, not a merge, so anything left out of the stream would be dropped
    from the graph. That also makes the call idempotent: the same source events
    and the same request produce the same graph and the same report every time,
    and a stream that already contains this write projects to exactly the same
    state.

    Target existence is checked against the material the stream itself carries
    (the node payloads of the supplied events), optionally widened by
    ``existing_nodes``/``existing_edges``. Nothing else is consulted: no store
    read, no file, no config.

    Args:
        store: The graph to project into. Only ``rebuild`` is called.
        source_events: The caller's existing append-only event stream.
        request: The explicit write decision.
        existing_nodes: Extra node material for target validation, merged over
            what the stream carries.
        existing_edges: Extra edge material for the already-asserted warning.

    Returns:
        A :class:`SemanticWriteProjection` holding the plan, the store's own
        report, and how many source events were projected with the write.

    Raises:
        ValueError: For a store without ``rebuild``, or any planning error. An
            invalid plan never reaches the store.
        SemanticWriteError: If ``rebuild`` itself raised. A rebuild that
            *reports* errors is returned, not raised.
    """
    _require_write_surface(store)

    events = list(source_events)
    nodes, edges = _material_from_events(events)
    if existing_nodes is not None:
        nodes.update(_normalize_nodes(existing_nodes) or {})
    if existing_edges is not None:
        edges.update(_normalize_edges(existing_edges))

    plan = plan_semantic_write(
        request, existing_nodes=nodes, existing_edges=edges
    )

    try:
        report = store.rebuild([*events, *plan.events])
    except Exception as exc:
        # The class name only: a backend message may name a database file (§15).
        raise SemanticWriteError(
            f"graph rebuild failed: {type(exc).__name__}"
        ) from exc

    return SemanticWriteProjection(
        plan=plan, report=report, source_events_read=len(events)
    )


def _require_write_surface(store: object) -> None:
    """Refuse a store that cannot project an event stream."""
    missing = sorted(
        name
        for name in SEMANTIC_WRITE_OPERATIONS
        if not callable(getattr(store, name, None))
    )
    if missing:
        raise ValueError(
            "graph store is missing the operations the semantic write seam "
            f"needs: {', '.join(missing)}"
        )


def _material_from_events(
    events: Sequence[MemoryEvent],
) -> tuple[dict[str, GraphNode], dict[str, GraphEdge]]:
    """Index the nodes and edges an explicit event stream carries.

    Read in ledger order — ``(occurred_at, id)``, the order ``rebuild`` itself
    uses — so a re-exported, reshuffled stream yields the same material. Only
    the payloads are indexed: a lifecycle event in the stream is *not* replayed
    here, so a target's status is read as the stream last wrote it and the
    store's own rebuild report stays the authority on whether a transition
    applied.
    """
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    for event in sorted(events, key=lambda e: (e.occurred_at, e.id)):
        if event.node is not None:
            nodes[event.node.id] = event.node
        if event.edge is not None:
            edges[event.edge.id] = event.edge
    return nodes, edges


__all__ = [
    "CONTRADICTION_KINDS",
    "DECISION_RELATIONS",
    "ROLE_EDGE",
    "ROLE_NODE",
    "ROLE_SUPERSEDE",
    "SEMANTIC_WRITE_EVENT_NAMESPACE",
    "SEMANTIC_WRITE_EVENT_PREFIX",
    "SEMANTIC_WRITE_OPERATIONS",
    "SUPERSEDABLE_STATUSES",
    "SUPERSESSION_SOURCE_KINDS",
    "RelationAssertion",
    "RelationDecision",
    "SemanticWriteError",
    "SemanticWritePlan",
    "SemanticWriteProjection",
    "SemanticWriteRequest",
    "plan_semantic_write",
    "project_semantic_write",
]

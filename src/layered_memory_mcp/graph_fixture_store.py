"""The deterministic in-memory memory-graph store (ARCHITECTURE_v2.md §9.2).

WHAT THIS MODULE IS
-------------------
The reference implementation of the
:class:`~layered_memory_mcp.graph_store.GraphStore` contract, holding
everything in process memory. It is the thing every later
batch compares against: the legacy importer (§12 Batch D) projects into it, the
embedded backend (Batch E) must agree with it node for node, and graph-assisted
recall (Batch F) is developed against it before a backend exists.

WHAT IT IS NOT
--------------
- NOT persistent. Nothing here reads or writes a file, a database, a config or
  real memory data; a store only ever contains what was handed to it.
- NOT wired into the hot path. No MCP tool, recall path or storage module
  imports it, and it imports neither ``server`` nor ``storage``.
- NOT a legacy importer, a feature flag or a graph-aware recall path. Those are
  Batch D and later.

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Determinism.** The same nodes and edges produce the same bytes regardless
   of insertion order: every returned collection has exactly one documented
   sort order, and :meth:`InMemoryGraphStore.snapshot` is comparable across
   stores and runs. No random IDs, no ``id()``-derived ordering, no reliance on
   dict insertion order for anything a caller can observe.
2. **Isolation.** State lives on the instance, never on the class or the
   module, and every value crossing the boundary is a deep copy — so a caller
   that mutates a returned node cannot reach back into the store.
3. **Honesty over convenience** (§10.3). A budget-limited traversal sets
   ``truncated``; an unresolvable provenance reference becomes a warning; an
   unresolved contradiction is reported rather than settled by write order; a
   rebuild names its duplicates, its conflicts and its dangling references.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

from .graph_models import (
    CURRENT_NODE_STATUSES,
    GRAPH_SCHEMA_VERSION,
    HISTORICAL_NODE_STATUSES,
    EdgeStatus,
    GraphEdge,
    GraphNode,
    GraphScope,
    MemoryEvent,
    MemoryEventKind,
    NodeKind,
    NodeStatus,
    RelationType,
)
from .graph_store import (
    ConflictReport,
    EdgeDirection,
    GraphHealth,
    GraphPath,
    ProvenanceRecord,
    RebuildReport,
    TraversalFilters,
    TraversalResult,
)

#: The name this implementation reports through
#: :attr:`~layered_memory_mcp.graph_store.GraphHealth.backend`. Callers that log
#: or branch on the backend should compare against this constant rather than a
#: literal.
FIXTURE_GRAPH_BACKEND = "in_memory_fixture"

#: Which way an edge may be read. Kept as data so an unknown direction is a
#: loud refusal rather than a silently empty result.
_EDGE_DIRECTIONS: tuple[str, ...] = ("outgoing", "incoming", "both")

#: The only lifecycle move an existing edge assertion may make in place: an
#: active relation can be withdrawn. ``EdgeStatus`` has no ``superseded`` state
#: (§6.2), so a *replacement* relation is a new edge — see
#: :func:`~layered_memory_mcp.graph_models.make_edge_id`'s ``discriminator``.
#: A withdrawal is terminal: re-asserting or re-withdrawing a withdrawn edge
#: would rewrite history, so it is refused.
_EDGE_WITHDRAWALS: dict[EdgeStatus, tuple[EdgeStatus, ...]] = {
    EdgeStatus.ACTIVE: (EdgeStatus.RETRACTED, EdgeStatus.EXPIRED),
}

#: The relations that make a node "about" an entity, for the ``entity_ids``
#: narrowing of :meth:`InMemoryGraphStore.find_conflicts`. ``MENTIONS`` is
#: excluded: an episode that happens to name an entity is not a statement about
#: it, and conflating the two would widen every conflict query to a whole
#: session.
_ABOUTNESS_RELATIONS: tuple[RelationType, ...] = (
    RelationType.ABOUT,
    RelationType.APPLIES_TO,
)

#: The §8.2 lifecycle table, as data: event kind -> (resulting node status, the
#: statuses it may be applied from). A transition that does not apply is
#: *reported*, never forced, so a replayed ledger cannot quietly invent a state
#: the original transitions never produced. ``retract`` is the only kind that
#: also applies to an edge (§6.2: an edge is asserted or withdrawn).
_NODE_TRANSITIONS: dict[
    MemoryEventKind, tuple[NodeStatus, tuple[NodeStatus, ...]]
] = {
    MemoryEventKind.APPROVE: (NodeStatus.ACTIVE, (NodeStatus.PENDING,)),
    MemoryEventKind.SUPERSEDE: (
        NodeStatus.SUPERSEDED,
        (NodeStatus.ACTIVE, NodeStatus.PENDING),
    ),
    MemoryEventKind.RETRACT: (
        NodeStatus.RETRACTED,
        tuple(s for s in NodeStatus if s is not NodeStatus.RETRACTED),
    ),
    MemoryEventKind.RESTORE: (NodeStatus.ACTIVE, tuple(HISTORICAL_NODE_STATUSES)),
}

#: What :meth:`InMemoryGraphStore.health` says about itself. A caller comparing
#: backends should not have to read code to learn this one is volatile.
_HEALTH_DETAIL = "deterministic in-memory fixture graph; nothing is persisted"


class GraphStoreError(RuntimeError):
    """Base class for a refusal this store raises instead of guessing."""


class UnknownNodeError(GraphStoreError):
    """A node was asked about by ID and is not stored.

    :meth:`InMemoryGraphStore.get_node` answers ``None`` instead, because "not
    stored" is a normal read result. An operation that can only *describe* an
    existing node — :meth:`~InMemoryGraphStore.get_provenance` — has no honest
    empty answer: a blank provenance record for a node that does not exist
    would read exactly like one for a node with no evidence.
    """


class EdgeConflictError(GraphStoreError):
    """An append would have rewritten an existing edge assertion in place.

    Raised rather than resolved, because "whichever call came last wins" is
    exactly the silent overwrite an append-only relation ledger exists to
    prevent (§8.3 applies the same rule to contradicting claims).
    """


def _edge_sort_key(edge: GraphEdge) -> tuple[str, str, str, str]:
    """The one stable order edges are returned in, everywhere in this module.

    Relation type first, because a caller reading a node's edges is almost
    always grouping by relation; then the endpoints; then the ID, which breaks
    every remaining tie because it is unique.
    """
    return (
        edge.relation_type.value,
        edge.source_id,
        edge.target_id,
        edge.id,
    )


def _schema_mismatches(
    nodes: dict[str, GraphNode], edges: dict[str, GraphEdge]
) -> list[str]:
    """Payloads written under a graph schema this process does not speak.

    Reported rather than migrated: silently reading a future payload with
    today's model is how a schema evolution loses a field (§9.3).
    """
    return [
        f"schema_version_mismatch:{key}:{item.schema_version}"
        for source in (nodes, edges)
        for key, item in sorted(source.items())
        if item.schema_version != GRAPH_SCHEMA_VERSION
    ]


def _covers(
    valid_from: datetime | None, valid_to: datetime | None, at: datetime
) -> bool:
    """Whether a validity window covers ``at``, inclusive on both ends.

    The same rule :meth:`GraphNode.is_valid_at` applies, spelled out here for
    edges: :meth:`GraphEdge.is_active_at` folds status into the answer, which
    would make a point-in-time query over *withdrawn* relations impossible —
    and "what did this look like before it was retracted" is exactly a
    point-in-time query (§7.4).
    """
    if valid_from is not None and at < valid_from:
        return False
    if valid_to is not None and at > valid_to:
        return False
    return True


class InMemoryGraphStore:
    """A deterministic, process-local :class:`GraphStore` implementation.

    ``nodes`` and ``edges`` seed the store with explicit fixture data; they are
    applied through the normal write operations, so a seeded store is
    indistinguishable from one built by calling them.
    """

    def __init__(
        self,
        nodes: Iterable[GraphNode] = (),
        edges: Iterable[GraphEdge] = (),
    ) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        for node in nodes:
            self.create_node(node)
        for edge in edges:
            self.append_edge(edge)

    # -- writes --------------------------------------------------------------

    def create_node(self, node: GraphNode) -> GraphNode:
        """Insert or replace ``node`` by ID and return the stored value.

        An upsert, as §9.1 specifies: node IDs are stable for a source identity
        (:func:`~layered_memory_mcp.graph_models.make_node_id`), so re-running
        an import must update one node rather than accumulate copies. A
        re-write with an identical payload is therefore a no-op, and one with a
        different payload replaces it — last write wins, which is deterministic
        for a given call order. Reporting a *stream* that writes one ID twice is
        :meth:`rebuild`'s job, where the whole input is visible at once.
        """
        stored = node.model_copy(deep=True)
        self._nodes[stored.id] = stored
        return stored.model_copy(deep=True)

    def append_edge(self, edge: GraphEdge) -> GraphEdge:
        """Append ``edge`` and return the stored value.

        Append-only (§6.2), which makes three cases out of one call:

        * a new edge ID is stored as given;
        * an identical re-append is a no-op, so replaying an import stream is
          idempotent;
        * a re-append that *changes* the assertion is refused with
          :class:`EdgeConflictError` — except for a withdrawal
          (``active`` → ``retracted``/``expired``), the one transition an
          existing assertion is allowed to make in place.

        A genuinely new assertion of the same triple needs its own ID; pass a
        ``discriminator`` to :func:`~layered_memory_mcp.graph_models.make_edge_id`.
        """
        incoming = edge.model_copy(deep=True)
        existing = self._edges.get(incoming.id)
        if existing is not None and existing != incoming:
            self._check_withdrawal(existing, incoming)
        self._edges[incoming.id] = incoming
        return incoming.model_copy(deep=True)

    @staticmethod
    def _check_withdrawal(existing: GraphEdge, incoming: GraphEdge) -> None:
        """Raise unless ``incoming`` is ``existing`` with a legal withdrawal."""
        rest = incoming.model_copy(update={"status": existing.status})
        if rest != existing:
            raise EdgeConflictError(
                f"edge {incoming.id!r} is already asserted as "
                f"{existing.relation_type.value} "
                f"{existing.source_id} -> {existing.target_id}; an edge is "
                "never edited in place. Append a new edge ID (see "
                "make_edge_id's discriminator) and withdraw this one."
            )
        allowed = _EDGE_WITHDRAWALS.get(existing.status, ())
        if incoming.status not in allowed:
            raise EdgeConflictError(
                f"edge {incoming.id!r} cannot move from "
                f"{existing.status.value} to {incoming.status.value}; "
                "a withdrawal is terminal, and restoring a relation is a new "
                "assertion with its own ID."
            )

    # -- reads ---------------------------------------------------------------

    def get_node(self, node_id: str) -> GraphNode | None:
        """Return the node with ``node_id``, or ``None`` if it is not stored.

        "Not stored" is a normal answer (§9.1); this store can always read, so
        it has no reason to raise here.
        """
        stored = self._nodes.get(node_id)
        return None if stored is None else stored.model_copy(deep=True)

    def get_edges(
        self,
        node_id: str,
        relation_types: Sequence[RelationType] | None = None,
        direction: EdgeDirection = "both",
    ) -> list[GraphEdge]:
        """Return ``node_id``'s edges, optionally narrowed by type/direction.

        The raw relation reader: it returns withdrawn and expired edges too,
        because status and validity are *traversal* filters (§7.3 step 4) and a
        caller asking "what was ever asserted about this node" must be able to
        see a retraction. Order is :func:`_edge_sort_key`, so it does not depend
        on insertion order. An unknown ``node_id`` yields ``[]`` — it is
        indistinguishable from a stored node with no edges, and neither is an
        error.
        """
        if direction not in _EDGE_DIRECTIONS:
            raise ValueError(
                f"direction must be one of {_EDGE_DIRECTIONS}, got {direction!r}"
            )
        allowed = set(relation_types or ())
        found = [
            edge
            for edge in self._edges.values()
            if (not allowed or edge.relation_type in allowed)
            and self._touches(edge, node_id, direction)
        ]
        return [
            edge.model_copy(deep=True)
            for edge in sorted(found, key=_edge_sort_key)
        ]

    @staticmethod
    def _touches(edge: GraphEdge, node_id: str, direction: str) -> bool:
        if direction in ("outgoing", "both") and edge.source_id == node_id:
            return True
        if direction in ("incoming", "both") and edge.target_id == node_id:
            return True
        return False

    def traverse(
        self,
        seed_ids: Sequence[str],
        max_hops: int = 1,
        filters: TraversalFilters | None = None,
    ) -> TraversalResult:
        """Expand from ``seed_ids`` under hard filters, bounded by ``max_hops``.

        A breadth-first expansion in **both** directions — the contract takes
        no direction, and relation-aware context needs "what is this evidence
        for" as much as "what is this claim about".

        Filters apply *during* expansion (§7.3 step 4), never afterwards, so a
        filtered-out node cannot act as a stepping stone: with the defaults, a
        superseded claim neither comes back nor leads anywhere. Hard-filter
        semantics in one place:

        * an empty list means "no constraint on this dimension", which is how
          ``node_statuses=[]`` asks for every lifecycle state;
        * ``scope`` is matched with :meth:`GraphScope.matches`, so an unscoped
          node does not leak into a narrowed scope;
        * ``as_of`` is a point-in-time filter on validity windows only, kept
          independent of status so history can be queried at a past instant;
        * a weak ``RELATED_TO`` edge is skipped unless
          ``include_weak_relations``, and the skip is *reported* rather than
          silent.

        Ordering is total and insertion-independent: nodes by (hop distance,
        ID), edges by :func:`_edge_sort_key`, paths by (hops, node IDs, edge
        IDs), warnings sorted and deduplicated.

        Honesty (§10.3): ``nodes`` empty with ``warnings`` empty means "nothing
        matched". An unknown seed, a seed the filters excluded, an edge
        pointing at a node that is not stored, and a hit ``max_nodes`` budget
        each produce a warning; the budget also sets ``truncated``.
        ``seed_ids`` echoes the deduplicated *request*, including seeds that
        did not survive, so the result still says what was asked for.

        ``edges`` is the induced subgraph over the returned nodes: every
        filter-passing edge whose *both* endpoints came back. That is a
        superset of the edges the paths use, and it is what makes the result
        renderable as a subgraph.
        """
        if max_hops < 0:
            raise ValueError(f"max_hops must not be negative, got {max_hops}")
        filters = filters or TraversalFilters()
        warnings: set[str] = set()

        seeds = list(dict.fromkeys(seed_ids))
        hops: dict[str, int] = {}
        paths: dict[str, GraphPath] = {}
        truncated = False

        for seed in seeds:
            node = self._nodes.get(seed)
            if node is None:
                warnings.add(f"unknown_seed:{seed}")
                continue
            if not self._node_matches(node, filters):
                warnings.add(f"excluded_seed:{seed}")
                continue
            if len(hops) >= filters.max_nodes:
                truncated = True
                warnings.add("truncated:max_nodes")
                break
            hops[seed] = 0
            paths[seed] = GraphPath(node_ids=[seed])

        frontier = sorted(hops)
        for hop in range(max_hops):
            if truncated or not frontier:
                break
            discovered: list[str] = []
            for node_id in frontier:
                for edge in self._incident_edges(node_id):
                    if not self._edge_matches(edge, filters, warnings):
                        continue
                    other = (
                        edge.target_id
                        if edge.source_id == node_id
                        else edge.source_id
                    )
                    neighbour = self._nodes.get(other)
                    if neighbour is None:
                        warnings.add(f"missing_node:{other}")
                        continue
                    if other in hops or not self._node_matches(neighbour, filters):
                        continue
                    if len(hops) >= filters.max_nodes:
                        truncated = True
                        warnings.add("truncated:max_nodes")
                        break
                    hops[other] = hop + 1
                    paths[other] = GraphPath(
                        node_ids=[*paths[node_id].node_ids, other],
                        edge_ids=[*paths[node_id].edge_ids, edge.id],
                    )
                    discovered.append(other)
                if truncated:
                    break
            frontier = sorted(discovered)

        induced: dict[str, GraphEdge] = {}
        for node_id in sorted(hops):
            for edge in self._incident_edges(node_id):
                if edge.id in induced or not self._edge_matches(
                    edge, filters, warnings
                ):
                    continue
                for endpoint in (edge.source_id, edge.target_id):
                    if endpoint not in self._nodes:
                        warnings.add(f"missing_node:{endpoint}")
                if edge.source_id in hops and edge.target_id in hops:
                    induced[edge.id] = edge

        return TraversalResult(
            seed_ids=seeds,
            nodes=[
                self._nodes[node_id].model_copy(deep=True)
                for node_id in sorted(hops, key=lambda n: (hops[n], n))
            ],
            edges=[
                edge.model_copy(deep=True)
                for edge in sorted(induced.values(), key=_edge_sort_key)
            ],
            paths=sorted(
                paths.values(),
                key=lambda p: (p.hops, p.node_ids, p.edge_ids),
            ),
            truncated=truncated,
            warnings=sorted(warnings),
        )

    def _incident_edges(self, node_id: str) -> list[GraphEdge]:
        """``node_id``'s edges in both directions, in the one stable order."""
        return sorted(
            (
                edge
                for edge in self._edges.values()
                if node_id in (edge.source_id, edge.target_id)
            ),
            key=_edge_sort_key,
        )

    @staticmethod
    def _node_matches(node: GraphNode, filters: TraversalFilters) -> bool:
        if filters.node_kinds and node.kind not in filters.node_kinds:
            return False
        if filters.node_statuses and node.status not in filters.node_statuses:
            return False
        if (
            filters.memory_classes
            and node.memory_class not in filters.memory_classes
        ):
            return False
        if filters.scope is not None and not node.scope.matches(filters.scope):
            return False
        if filters.as_of is not None and not node.is_valid_at(filters.as_of):
            return False
        return True

    @staticmethod
    def _edge_matches(
        edge: GraphEdge, filters: TraversalFilters, warnings: set[str]
    ) -> bool:
        """Whether ``edge`` may be expanded, recording a weak-relation skip.

        The relation-type narrowing is checked first on purpose: an edge the
        caller already excluded by type should not also be reported as a
        weak-relation skip.
        """
        if (
            filters.relation_types
            and edge.relation_type not in filters.relation_types
        ):
            return False
        if edge.is_weak() and not filters.include_weak_relations:
            warnings.add(f"weak_relation_excluded:{edge.relation_type.value}")
            return False
        if filters.edge_statuses and edge.status not in filters.edge_statuses:
            return False
        if filters.as_of is not None and not _covers(
            edge.valid_from, edge.valid_to, filters.as_of
        ):
            return False
        return True

    def find_conflicts(
        self,
        scope: GraphScope | None = None,
        entity_ids: Sequence[str] | None = None,
    ) -> list[ConflictReport]:
        """Return unresolved contradictions in ``scope`` / about ``entity_ids``.

        Two nodes contradict each other if an **active** ``CONTRADICTS`` edge
        joins them or they share a ``conflict_group`` (§8.3 names both
        mechanisms, and an importer may know only the group). Contradiction is
        treated as transitive for reporting purposes: one report per connected
        component, so three mutually-grouped claims are one three-sided
        conflict rather than three overlapping pairs.

        A side stops contesting the component when it is no longer current —
        ``superseded``/``retracted``/``expired``/``archived``/``pending`` — or
        when another member supersedes it through an active ``SUPERSEDES``
        edge. That second rule is deliberate: §8.3 resolves a conflict through
        a state transition backed by evidence, and a status that has not caught
        up with the transition is a projection lag, not a live contradiction.
        A side that is *not stored* keeps contesting, because an unknown state
        must not be read as a settled one.

        Only components with at least two contesting sides come back —
        "unresolved" is what §9.1 asks for — so ``resolved`` is always
        ``False`` here. Resolved history stays queryable through
        :meth:`traverse` with a widened status filter.

        ``node_ids`` lists *every* side including the settled ones (§8.3:
        preserve both claims and their evidence); ``detail`` names which sides
        are still contesting, and any side that could not be resolved.
        """
        wanted_entities = set(entity_ids or ())
        reports: list[ConflictReport] = []

        for members, edge_ids in self._contradiction_components():
            stored = [m for m in members if m in self._nodes]
            missing = [m for m in members if m not in self._nodes]
            contesting = sorted(
                [m for m in stored if not self._is_settled(m, members)] + missing
            )
            if len(contesting) < 2:
                continue
            if scope is not None and not any(
                self._nodes[m].scope.matches(scope) for m in stored
            ):
                continue
            if wanted_entities and not any(
                self._is_about(m, wanted_entities) for m in stored
            ):
                continue

            groups = {
                self._nodes[m].conflict_group
                for m in stored
                if self._nodes[m].conflict_group
            }
            scopes = {self._nodes[m].scope.to_json() for m in stored}
            detail = "; ".join(
                [f"unresolved={','.join(contesting)}"]
                + [f"missing_node:{m}" for m in missing]
            )
            reports.append(
                ConflictReport(
                    node_ids=members,
                    conflict_group=groups.pop() if len(groups) == 1 else None,
                    scope=(
                        self._nodes[stored[0]].scope.model_copy(deep=True)
                        if len(scopes) == 1 and stored
                        else None
                    ),
                    edge_ids=edge_ids,
                    relation_type=RelationType.CONTRADICTS,
                    resolved=False,
                    detail=detail,
                )
            )

        return sorted(reports, key=lambda r: (r.node_ids, r.conflict_group or ""))

    def _contradiction_components(self) -> list[tuple[list[str], list[str]]]:
        """Group contradicting node IDs into components, with their edge IDs.

        A union-find over the two contradiction signals. The smaller ID always
        becomes the root, so the grouping — and therefore every report built
        from it — does not depend on insertion order.
        """
        parent: dict[str, str] = {}

        def root(node_id: str) -> str:
            parent.setdefault(node_id, node_id)
            while parent[node_id] != node_id:
                parent[node_id] = parent[parent[node_id]]
                node_id = parent[node_id]
            return node_id

        def merge(left: str, right: str) -> None:
            left_root, right_root = root(left), root(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        contradiction_edges = [
            edge
            for edge in sorted(self._edges.values(), key=_edge_sort_key)
            if edge.relation_type is RelationType.CONTRADICTS
            and edge.status is EdgeStatus.ACTIVE
        ]
        for edge in contradiction_edges:
            merge(edge.source_id, edge.target_id)

        by_group: dict[str, list[str]] = {}
        for node_id in sorted(self._nodes):
            group = self._nodes[node_id].conflict_group
            if group:
                by_group.setdefault(group, []).append(node_id)
        for grouped in by_group.values():
            for node_id in grouped[1:]:
                merge(grouped[0], node_id)

        members: dict[str, list[str]] = {}
        for node_id in sorted(parent):
            members.setdefault(root(node_id), []).append(node_id)
        edges_by_component: dict[str, list[str]] = {}
        for edge in contradiction_edges:
            edges_by_component.setdefault(root(edge.source_id), []).append(edge.id)

        return [
            (members[key], sorted(edges_by_component.get(key, [])))
            for key in sorted(members)
            if len(members[key]) > 1
        ]

    def _is_settled(self, node_id: str, members: Sequence[str]) -> bool:
        """Whether ``node_id`` has stopped contesting its conflict component."""
        node = self._nodes[node_id]
        if node.status not in CURRENT_NODE_STATUSES:
            return True
        return any(
            edge.relation_type is RelationType.SUPERSEDES
            and edge.status is EdgeStatus.ACTIVE
            and edge.target_id == node_id
            and edge.source_id in members
            for edge in self._edges.values()
        )

    def _is_about(self, node_id: str, entity_ids: set[str]) -> bool:
        if node_id in entity_ids:
            return True
        return any(
            edge.relation_type in _ABOUTNESS_RELATIONS
            and edge.status is EdgeStatus.ACTIVE
            and edge.source_id == node_id
            and edge.target_id in entity_ids
            for edge in self._edges.values()
        )

    def get_provenance(self, node_id: str) -> ProvenanceRecord:
        """Return the evidence and derivation lineage behind ``node_id``.

        Two sources are merged: the node's own ``source_refs``/``derivation``,
        and its active provenance edges (``EVIDENCED_BY``, ``OBSERVED_IN``,
        ``DERIVED_FROM``). Nothing is invented and nothing is dropped — every
        reference that cannot be resolved, resolves to the wrong kind of node,
        or has been withdrawn becomes a ``warning``, which is what clears
        :attr:`ProvenanceRecord.resolved`:

        * ``unresolved_evidence|episode|derived_from:<id>`` — named, not stored;
        * ``unexpected_evidence|episode_kind:<id>:<kind>`` — stored, but not the
          kind the relation promises, so the lineage is ambiguous;
        * ``withdrawn_evidence:<id>`` — the evidence edge was retracted, so the
          claim's support is weaker than its reference list suggests.

        Only edge *status* is considered, not a point in time: this answers
        "what backs this node", which has no ``as_of`` in the §9.1 contract.
        """
        node = self._nodes.get(node_id)
        if node is None:
            raise UnknownNodeError(
                f"node {node_id!r} is not stored, so it has no provenance; "
                "use get_node to test for existence."
            )

        warnings: set[str] = set()
        evidence_ids = {
            ref.evidence_id for ref in node.source_refs if ref.evidence_id
        }
        episode_ids = {ref.episode_id for ref in node.source_refs if ref.episode_id}
        derived_from_ids: set[str] = set()

        for edge in self._incident_edges(node.id):
            if edge.source_id != node.id:
                continue
            if edge.relation_type is RelationType.EVIDENCED_BY:
                if edge.status is EdgeStatus.ACTIVE:
                    evidence_ids.add(edge.target_id)
                else:
                    warnings.add(f"withdrawn_evidence:{edge.target_id}")
            elif edge.status is not EdgeStatus.ACTIVE:
                continue
            elif edge.relation_type is RelationType.OBSERVED_IN:
                episode_ids.add(edge.target_id)
            elif edge.relation_type is RelationType.DERIVED_FROM:
                derived_from_ids.add(edge.target_id)

        self._check_refs(evidence_ids, "evidence", NodeKind.EVIDENCE, warnings)
        self._check_refs(episode_ids, "episode", NodeKind.EPISODE, warnings)
        # DERIVED_FROM may legitimately point at an Episode, a Claim or an
        # Event (§6.2), so only existence is checkable here.
        self._check_refs(derived_from_ids, "derived_from", None, warnings)

        return ProvenanceRecord(
            node_id=node.id,
            node_version=node.version,
            source_refs=[ref.model_copy(deep=True) for ref in node.source_refs],
            derivation=(
                None
                if node.derivation is None
                else node.derivation.model_copy(deep=True)
            ),
            evidence_ids=sorted(evidence_ids),
            episode_ids=sorted(episode_ids),
            derived_from_ids=sorted(derived_from_ids),
            warnings=sorted(warnings),
        )

    def _check_refs(
        self,
        ids: Iterable[str],
        label: str,
        expected: NodeKind | None,
        warnings: set[str],
    ) -> None:
        for ref_id in ids:
            target = self._nodes.get(ref_id)
            if target is None:
                warnings.add(f"unresolved_{label}:{ref_id}")
            elif expected is not None and target.kind is not expected:
                warnings.add(
                    f"unexpected_{label}_kind:{ref_id}:{target.kind.value}"
                )

    def rebuild(self, source_events: Sequence[MemoryEvent]) -> RebuildReport:
        """Project graph state from an append-only event stream, idempotently.

        A **rebuild**, not a merge: the projection is built in isolation and
        then replaces the store's contents, which is what makes it idempotent
        (§10.2) and what lets a caller rebuild into a store that already holds
        a different graph. Nothing outside ``source_events`` survives.

        Events are applied in ledger order — ``(occurred_at, id)`` — so a
        stream whose file order was shuffled by an export projects the same
        graph. Node and edge payloads are taken as given, so the same source
        identity produces the same ID on every run.

        It **reports instead of raising**, unlike :meth:`append_edge`: a
        rebuild is handed a whole ledger it did not author, so every input
        problem is named (§10.2) in the returned report. An invalid candidate
        is fail-closed: the candidate is never installed, and the previous
        projection remains byte-for-byte unchanged.

        * ``errors`` (which clear :attr:`RebuildReport.ok`) —
          ``conflicting_node|edge:<id>`` for one ID written two different ways,
          ``conflicting_event:<id>`` for one event ID carrying two payloads,
          ``unresolved_edge_endpoint:<edge>:<node>`` for an edge the stream
          never projected a node for, ``unknown_target:<event>:<id>`` and
          ``missing_target:<event>:<kind>`` for an inapplicable transition, and
          ``schema_version_mismatch:<id>:<version>``;
        * ``warnings`` — ``duplicate_node|edge|event:<id>`` for a benign
          replay, ``unresolved_source_ref:<node>:<ref>`` for provenance that
          points outside this stream, ``ignored_transition:...`` and
          ``nothing_to_project:...`` for events that changed nothing.

        A dangling edge is reported and rejected. Persisting it would make a
        broken reference part of the authoritative projection; callers that
        need to inspect the malformed candidate can use the report without
        replacing the last known-good graph.

        ``nodes_written``/``edges_written`` are the size of the resulting
        projection, and ``skipped`` counts the events that changed nothing —
        so a second identical rebuild returns an identical report.
        """
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        errors: set[str] = set()
        warnings: set[str] = set()
        skipped = 0

        ordered: list[MemoryEvent] = []
        seen: dict[str, MemoryEvent] = {}
        for event in sorted(source_events, key=lambda e: (e.occurred_at, e.id)):
            prior = seen.get(event.id)
            if prior is not None:
                if prior == event:
                    warnings.add(f"duplicate_event:{event.id}")
                    skipped += 1
                    continue
                errors.add(f"conflicting_event:{event.id}")
            seen[event.id] = event
            ordered.append(event)

        for event in ordered:
            changed = False
            if event.node is not None:
                changed |= self._project_node(event.node, nodes, errors, warnings)
            if event.edge is not None:
                changed |= self._project_edge(event.edge, edges, errors, warnings)
            if event.kind in _NODE_TRANSITIONS:
                changed |= self._project_transition(
                    event, nodes, edges, errors, warnings
                )
            elif event.node is None and event.edge is None:
                warnings.add(f"nothing_to_project:{event.id}:{event.kind.value}")
            if not changed:
                skipped += 1

        for edge_id in sorted(edges):
            edge = edges[edge_id]
            for endpoint in (edge.source_id, edge.target_id):
                if endpoint not in nodes:
                    errors.add(f"unresolved_edge_endpoint:{edge_id}:{endpoint}")
        for node_id in sorted(nodes):
            for ref in nodes[node_id].source_refs:
                for ref_id in (ref.episode_id, ref.evidence_id):
                    if ref_id and ref_id not in nodes:
                        warnings.add(f"unresolved_source_ref:{node_id}:{ref_id}")
        errors.update(_schema_mismatches(nodes, edges))

        report = RebuildReport(
            events_read=len(source_events),
            nodes_written=len(nodes),
            edges_written=len(edges),
            skipped=skipped,
            errors=sorted(errors),
            warnings=sorted(warnings),
        )
        if report.ok:
            self._nodes = nodes
            self._edges = edges
        return report

    @staticmethod
    def _project_node(
        node: GraphNode,
        nodes: dict[str, GraphNode],
        errors: set[str],
        warnings: set[str],
    ) -> bool:
        """Write ``node`` into the projection; report a duplicate or a clash."""
        incoming = node.model_copy(deep=True)
        prior = nodes.get(incoming.id)
        if prior is not None:
            if prior == incoming:
                warnings.add(f"duplicate_node:{incoming.id}")
                return False
            errors.add(f"conflicting_node:{incoming.id}")
        nodes[incoming.id] = incoming
        return True

    @staticmethod
    def _project_edge(
        edge: GraphEdge,
        edges: dict[str, GraphEdge],
        errors: set[str],
        warnings: set[str],
    ) -> bool:
        incoming = edge.model_copy(deep=True)
        prior = edges.get(incoming.id)
        if prior is not None:
            if prior == incoming:
                warnings.add(f"duplicate_edge:{incoming.id}")
                return False
            errors.add(f"conflicting_edge:{incoming.id}")
        edges[incoming.id] = incoming
        return True

    @staticmethod
    def _project_transition(
        event: MemoryEvent,
        nodes: dict[str, GraphNode],
        edges: dict[str, GraphEdge],
        errors: set[str],
        warnings: set[str],
    ) -> bool:
        """Apply one §8.2 lifecycle transition to the projection."""
        target = event.target_id
        if not target:
            errors.add(f"missing_target:{event.id}:{event.kind.value}")
            return False

        new_status, applies_from = _NODE_TRANSITIONS[event.kind]
        node = nodes.get(target)
        if node is not None:
            if node.status not in applies_from:
                warnings.add(
                    f"ignored_transition:{event.kind.value}:{target}:"
                    f"{node.status.value}"
                )
                return False
            nodes[target] = node.model_copy(update={"status": new_status})
            return True

        edge = edges.get(target)
        if edge is not None:
            if event.kind is not MemoryEventKind.RETRACT:
                errors.add(
                    f"unexpected_edge_target:{event.id}:{target}:{event.kind.value}"
                )
                return False
            if edge.status is not EdgeStatus.ACTIVE:
                warnings.add(
                    f"ignored_transition:retract:{target}:{edge.status.value}"
                )
                return False
            edges[target] = edge.model_copy(update={"status": EdgeStatus.RETRACTED})
            return True

        errors.add(f"unknown_target:{event.id}:{target}")
        return False

    # -- health and fixtures -------------------------------------------------

    def health(self) -> GraphHealth:
        """Report whether the graph can answer queries. Never raises.

        An in-memory store cannot be "down", so the integrity of what it holds
        is the only thing worth reporting — and §10.3 forbids a green light
        next to a broken schema. Two problems clear ``healthy``:

        * ``dangling_edge:<edge>:<node>`` — a relation whose endpoint is not
          stored. :meth:`rebuild` keeps such an edge on purpose, so health is
          where that stays visible rather than fading away.
        * ``schema_version_mismatch:<id>:<version>`` — a payload written under
          a different graph schema than this process understands.

        The scan is wrapped: a caller reaches for ``health`` precisely when
        something else already failed, so it reports its own failure as an
        error instead of raising a second one.
        """
        errors: list[str] = []
        try:
            for edge_id in sorted(self._edges):
                edge = self._edges[edge_id]
                for endpoint in (edge.source_id, edge.target_id):
                    if endpoint not in self._nodes:
                        errors.append(f"dangling_edge:{edge_id}:{endpoint}")
            errors.extend(_schema_mismatches(self._nodes, self._edges))
        except Exception as exc:  # pragma: no cover - defensive, must not raise
            errors.append(f"health_check_failed:{type(exc).__name__}")
        return GraphHealth(
            backend=FIXTURE_GRAPH_BACKEND,
            node_count=len(self._nodes),
            edge_count=len(self._edges),
            detail=_HEALTH_DETAIL,
            errors=sorted(errors),
        )

    def snapshot(self) -> dict[str, Any]:
        """Return the whole store as plain JSON types, in one stable order.

        Nodes and edges are sorted by ID, so two stores holding the same graph
        compare equal no matter how they were built. The result is freshly
        built, so mutating it cannot affect the store.
        """
        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "nodes": [self._nodes[k].to_dict() for k in sorted(self._nodes)],
            "edges": [self._edges[k].to_dict() for k in sorted(self._edges)],
        }

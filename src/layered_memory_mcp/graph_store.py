"""The backend-neutral memory graph store contract (ARCHITECTURE_v2.md §9.1).

WHAT THIS MODULE IS
-------------------
Nine operations and the typed shapes they exchange. That is the whole surface a
graph backend has to implement, and the whole surface the rest of the framework
is allowed to depend on.

The contract is deliberately smaller than a general graph ORM: it exposes only
what memory semantics and retrieval need — create, append, read, traverse under
hard filters, surface conflicts, explain provenance, rebuild, and report health.
No Cypher, no query builder, no vendor type (§9.3), so the same contract
survives an embedded-to-server migration, a schema evolution, an export/import,
a full rebuild, and a graph outage with fallback retrieval.

WHAT IT IS NOT
--------------
- NOT an implementation. The deterministic in-memory implementation is
  :class:`~layered_memory_mcp.graph_fixture_store.InMemoryGraphStore` (§12
  Batch C); the embedded backend is a later batch (§12 Batch E).
- NOT a transaction manager or a migration tool.
- NOT a search engine. Fuzzy wording matching stays with the existing
  BM25/vector retrieval (§7.1), which feeds ``traverse`` its seed IDs.

WHY A PROTOCOL AND NOT AN ABC
-----------------------------
An implementation should not have to inherit from the framework to be usable —
an adapter wrapping a third-party handle is a plain object. The protocol is
``runtime_checkable``, and :func:`missing_graph_store_operations` names exactly
what an incomplete implementation is missing, so "does this thing satisfy the
contract?" is a question a test can answer instead of a convention.

A note on honesty under failure (§10.3): an implementation that cannot answer
must raise or report it through :meth:`GraphStore.health`. Returning an empty
traversal is indistinguishable from "nothing matched", which would let a graph
outage look like a memory with no conflicts and no superseded facts.
"""

from __future__ import annotations

from typing import Literal, Protocol, Sequence, runtime_checkable

from pydantic import Field, model_validator

from .graph_models import (
    CURRENT_NODE_STATUSES,
    GRAPH_SCHEMA_VERSION,
    Derivation,
    EdgeStatus,
    GraphEdge,
    GraphModel,
    GraphNode,
    GraphScope,
    MemoryClass,
    MemoryEvent,
    NodeKind,
    NodeStatus,
    RelationType,
    SourceRef,
    Timestamp,
    clean_identifier,
)

#: Which way an edge has to point to be returned by :meth:`GraphStore.get_edges`.
EdgeDirection = Literal["outgoing", "incoming", "both"]

#: The §9.1 operation set. Kept as data so an implementation, an adapter test or
#: a capability report can check completeness without duck-typing by hand.
REQUIRED_GRAPH_STORE_OPERATIONS: tuple[str, ...] = (
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


# ---------------------------------------------------------------------------
# Query and result shapes
# ---------------------------------------------------------------------------

class TraversalFilters(GraphModel):
    """Hard filters applied during expansion, not after ranking (§7.3 step 4).

    The defaults are the conservative reading of the architecture: current
    status only, active edges only, and weak ``RELATED_TO`` relations excluded,
    because a weak relation cannot by itself justify pulling a node into
    context (§6.2). Widening is explicit — a historical or "what did this
    replace" query opts into superseded state by naming it.

    ``as_of`` turns the traversal into a point-in-time query: nodes and edges
    whose validity window does not cover it are out, regardless of status.
    """

    relation_types: list[RelationType] = Field(default_factory=list)
    node_kinds: list[NodeKind] = Field(default_factory=list)
    node_statuses: list[NodeStatus] = Field(
        default_factory=lambda: list(CURRENT_NODE_STATUSES)
    )
    edge_statuses: list[EdgeStatus] = Field(
        default_factory=lambda: [EdgeStatus.ACTIVE]
    )
    memory_classes: list[MemoryClass] = Field(default_factory=list)
    scope: GraphScope | None = None
    as_of: Timestamp | None = None
    include_weak_relations: bool = False
    #: Expansion budget. A bounded traversal is a correctness property here, not
    #: a performance tweak: unbounded expansion from a popular entity returns
    #: the whole graph, and high degree is explicitly not evidence (§7.3).
    max_nodes: int = Field(default=200, ge=1)


class GraphPath(GraphModel):
    """One seed-to-node route, returned so a result can explain itself (§3.1.8).

    A path is a chain: ``edge_ids[i]`` connects ``node_ids[i]`` to
    ``node_ids[i + 1]``. The length relation is enforced, because a path whose
    edges do not line up with its nodes cannot be rendered as an explanation.
    """

    node_ids: list[str] = Field(min_length=1)
    edge_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_chain(self) -> GraphPath:
        for index, node_id in enumerate(self.node_ids):
            clean_identifier(node_id, f"node_ids[{index}]")
        for index, edge_id in enumerate(self.edge_ids):
            clean_identifier(edge_id, f"edge_ids[{index}]")
        if len(self.edge_ids) != len(self.node_ids) - 1:
            raise ValueError(
                "a path needs exactly one edge between consecutive nodes "
                f"(got {len(self.node_ids)} nodes, {len(self.edge_ids)} edges)"
            )
        return self

    @property
    def hops(self) -> int:
        return len(self.edge_ids)


class TraversalResult(GraphModel):
    """What a bounded expansion found, plus how honest the answer is.

    ``truncated`` and ``warnings`` exist so a budget-limited or partially
    resolvable traversal cannot be mistaken for a complete one.
    """

    seed_ids: list[str] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    paths: list[GraphPath] = Field(default_factory=list)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class ProvenanceRecord(GraphModel):
    """Why a node is in the graph: its refs, its derivation, its source nodes.

    ``warnings`` carries unresolvable references (a missing evidence node, an
    ambiguous source) rather than dropping them, which is what §10.2 requires
    of a rebuild. ``resolved`` is derived from it, so an incomplete provenance
    answer cannot read as a clean one.
    """

    node_id: str
    node_version: int = Field(default=1, ge=1)
    source_refs: list[SourceRef] = Field(default_factory=list)
    derivation: Derivation | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    episode_ids: list[str] = Field(default_factory=list)
    derived_from_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_node_id(self) -> ProvenanceRecord:
        clean_identifier(self.node_id, "node_id")
        return self

    @property
    def resolved(self) -> bool:
        return not self.warnings


class ConflictReport(GraphModel):
    """An unresolved contradiction, reported rather than silently settled (§8.3).

    A conflict has at least two sides — "whichever was written last" is not a
    resolution — and ``resolved`` defaults to false, so recall can attach a
    warning instead of presenting a contested claim as settled.
    """

    node_ids: list[str] = Field(min_length=2)
    conflict_group: str | None = None
    scope: GraphScope | None = None
    edge_ids: list[str] = Field(default_factory=list)
    relation_type: RelationType = RelationType.CONTRADICTS
    resolved: bool = False
    detail: str = ""

    @model_validator(mode="after")
    def _check_ids(self) -> ConflictReport:
        for index, node_id in enumerate(self.node_ids):
            clean_identifier(node_id, f"node_ids[{index}]")
        return self


class RebuildReport(GraphModel):
    """Outcome of projecting the graph from an event stream (§10.2).

    Counts plus the things that did not fit. ``errors`` must name what was
    unresolvable or ambiguous; a rebuild that silently dropped a reference is
    indistinguishable from one that had nothing to drop.
    """

    events_read: int = Field(default=0, ge=0)
    nodes_written: int = Field(default=0, ge=0)
    edges_written: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class GraphHealth(GraphModel):
    """Whether the graph can be trusted to answer right now (§10.3).

    ``errors`` drives ``healthy``, so a backend cannot report a green light and
    a broken schema at the same time. A caller that sees an unhealthy graph
    falls back to vector/BM25 and then L0/L1 — with reduced, honest behavior
    rather than fabricated success.
    """

    backend: str
    node_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)
    schema_version: int = GRAPH_SCHEMA_VERSION
    detail: str = ""
    errors: list[str] = Field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

@runtime_checkable
class GraphStore(Protocol):
    """What a memory graph backend must implement (§9.1).

    Semantics every implementation owes the caller:

    - ``create_node`` is an upsert keyed on ``node.id``. IDs are stable for a
      source identity, so a rerun of an import must not duplicate nodes.
    - ``append_edge`` is append-only. A relation is never edited in place; a
      changed relation is a new edge plus a retraction of the old one.
    - ``get_node`` returns ``None`` for an unknown ID. "Not stored" is a normal
      answer; "cannot read" must raise.
    - ``get_edges`` returns edges in a stable order so fixtures can assert on it.
    - ``traverse`` applies ``filters`` *during* expansion and reports
      ``truncated`` when it hits the budget.
    - ``find_conflicts`` returns unresolved contradictions; an empty list means
      none were found, which is why an unreadable graph must raise instead.
    - ``get_provenance`` never invents a reference: an unresolvable one goes in
      ``warnings``.
    - ``rebuild`` is idempotent for the same event stream and produces the same
      IDs for the same source identities.
    - ``health`` never raises; it is the thing a caller asks when something
      else already failed.
    """

    def create_node(self, node: GraphNode) -> GraphNode:
        """Insert or replace ``node`` by ID and return the stored value."""
        ...

    def append_edge(self, edge: GraphEdge) -> GraphEdge:
        """Append ``edge`` and return the stored value."""
        ...

    def get_node(self, node_id: str) -> GraphNode | None:
        """Return the node with ``node_id``, or ``None`` if it is not stored."""
        ...

    def get_edges(
        self,
        node_id: str,
        relation_types: Sequence[RelationType] | None = None,
        direction: EdgeDirection = "both",
    ) -> list[GraphEdge]:
        """Return ``node_id``'s edges, optionally narrowed by type/direction."""
        ...

    def traverse(
        self,
        seed_ids: Sequence[str],
        max_hops: int = 1,
        filters: TraversalFilters | None = None,
    ) -> TraversalResult:
        """Expand from ``seed_ids`` under hard filters, bounded by ``max_hops``."""
        ...

    def find_conflicts(
        self,
        scope: GraphScope | None = None,
        entity_ids: Sequence[str] | None = None,
    ) -> list[ConflictReport]:
        """Return unresolved contradictions in ``scope`` / about ``entity_ids``."""
        ...

    def get_provenance(self, node_id: str) -> ProvenanceRecord:
        """Return the evidence and derivation lineage behind ``node_id``."""
        ...

    def rebuild(self, source_events: Sequence[MemoryEvent]) -> RebuildReport:
        """Project graph state from an append-only event stream, idempotently."""
        ...

    def health(self) -> GraphHealth:
        """Report whether the graph can answer queries. Must not raise."""
        ...


def missing_graph_store_operations(candidate: object) -> list[str]:
    """Names from :data:`REQUIRED_GRAPH_STORE_OPERATIONS` ``candidate`` lacks.

    Sorted, so the result is stable enough to assert on. An empty list means the
    object is shaped like a :class:`GraphStore`; it does not promise the
    *semantics* documented above, which is what the behavioral tests are for.
    """
    return sorted(
        name
        for name in REQUIRED_GRAPH_STORE_OPERATIONS
        if not callable(getattr(candidate, name, None))
    )

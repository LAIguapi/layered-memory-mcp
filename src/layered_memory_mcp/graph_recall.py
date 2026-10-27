"""Graph-assisted recall — the seam between existing seeds and the memory graph.

WHAT THIS MODULE IS
-------------------
Steps 3 to 7 of the ``ARCHITECTURE_v2.md`` §7.3 unified recall pipeline, as one
backend-neutral, separately testable function: take the results an existing
BM25/vector/lexical search already produced, expand them through a
:class:`~layered_memory_mcp.graph_store.GraphStore` under hard filters, backfill
provenance, surface conflicts, and say honestly how much of that worked.

Steps 1 and 2 (query understanding, seed retrieval) stay where they are. This
module never searches: it is handed seeds, and it is handed the mapping from a
seed's legacy identity to its graph node IDs.

WHAT IT IS NOT
--------------
- NOT wired into the default recall path. Nothing imports this module yet (§12
  Batch F is explicitly not a cutover; the flagged cutover is Batch H), and it
  imports neither ``recall``, ``injector``, ``server`` nor any storage module.
- NOT a search engine. The graph is a hard semantic *filter*, *expansion* and
  *explanation* layer over seeds that something else ranked. No graph score is
  invented, and nothing is fused into the caller's score — §3.2 non-goal 4:
  graph degree is not relevance.
- NOT a writer. The layer asks a store for exactly four read operations
  (:data:`GRAPH_RECALL_OPERATIONS`); ``create_node``, ``append_edge`` and
  ``rebuild`` are not even required to be present.
- NOT a discovery mechanism. It reads no file, resolves no path, loads no L1
  document and guesses no entity. The only seed-to-graph mapping it uses is the
  one the caller passed in, which is what makes the result reproducible.

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Seeds survive.** Every supplied result comes back, in the caller's original
   ranking, with its payload untouched under
   :attr:`GraphRecallResult.result`. Graph material is purely additive: a result
   with no mapping, a seed the filters excluded, a failed traversal and an
   unresolvable provenance record all still carry their original result.
2. **Determinism.** The answer does not depend on the insertion order of the
   mapping or on the order of a mapped node-ID list: result order is the seed
   ranking, and every graph collection is sorted by stable graph identifiers.
   :meth:`~layered_memory_mcp.graph_models.GraphModel.to_json` is therefore a
   valid fixture assertion.
3. **Hard filters are the store's.** Status, validity/``as_of``, scope, relation
   type and the weak-relation rule are enforced *during* expansion by
   :meth:`GraphStore.traverse` (§7.3 step 4). This module never re-adds a node
   the traversal excluded — not even a seed node it asked for.
4. **Honesty over convenience** (§10.3). A missing store, an unhealthy graph, a
   raising traversal, a truncated expansion, an unmapped result, an unresolvable
   provenance reference and an unresolved contradiction are each reported
   through :attr:`GraphRecallReport.status` and ``warnings``. A graph outage
   never returns an empty successful answer, and it never raises: the caller
   falls back to its own seed results, which are right there in the report.

PRIVACY
-------
Warnings carry graph node IDs, relation type names and exception *class* names
only — never an exception message, a store diagnostic string, a source path or a
memory body (§15). A backend that names a file in its health errors cannot leak
it through this layer: only the error's label is surfaced.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import Field, field_validator

from .graph_models import (
    GraphEdge,
    GraphModel,
    GraphNode,
    clean_identifier,
)
from .graph_store import (
    ConflictReport,
    GraphPath,
    GraphStore,
    ProvenanceRecord,
    TraversalFilters,
)

#: The only store operations this layer calls. Sorted, and deliberately a
#: *subset* of :data:`~layered_memory_mcp.graph_store.REQUIRED_GRAPH_STORE_OPERATIONS`:
#: Batch F is read-only, so a store handed to it is not even asked for a write
#: operation, and "did recall mutate the graph?" is answerable by inspection
#: rather than by convention.
GRAPH_RECALL_OPERATIONS: tuple[str, ...] = (
    "find_conflicts",
    "get_provenance",
    "health",
    "traverse",
)

#: The §7.3 step 3 budget: "one or two hops by allowed relation types". A deeper
#: request is refused rather than quietly clamped, because a caller asking for
#: five hops has a different model of the cost than this layer does.
MAX_GRAPH_RECALL_HOPS = 2

#: Graph assistance ran for every eligible seed and reported nothing to qualify.
GRAPH_STATUS_OK = "graph_ok"
#: Graph assistance ran, but something is incomplete — see ``warnings``.
GRAPH_STATUS_PARTIAL = "graph_partial"
#: No usable graph: absent, unhealthy, or every traversal failed. Seeds only.
GRAPH_STATUS_UNAVAILABLE = "graph_unavailable"
#: A usable graph that was never consulted, because no result had a mapping.
GRAPH_STATUS_UNUSED = "graph_unused"

GraphRecallStatus = Literal[
    "graph_ok",
    "graph_partial",
    "graph_unavailable",
    "graph_unused",
]


def _unique_node_ids(values: Iterable[str]) -> list[str]:
    """Clean, deduplicate and sort node IDs.

    Sorting is what makes the answer independent of the order a caller happened
    to list a result's seed nodes in, which is a determinism requirement rather
    than a cosmetic one.
    """
    return sorted({clean_identifier(value, "seed node id") for value in values})


def _error_label(error: str) -> str:
    """The leading label of a store diagnostic, without its payload.

    Store health errors are shaped ``label:detail...`` and the detail may name a
    database file or a host path. The label alone says *what kind* of problem
    the graph has, which is all a recall warning needs (§15).
    """
    return error.split(":", 1)[0]


# ---------------------------------------------------------------------------
# Input and output shapes
# ---------------------------------------------------------------------------

class RecallSeed(GraphModel):
    """One existing recall result offered to graph assistance.

    ``file`` is the legacy result identity — the key the existing recall path
    already uses and the key ``seed_node_ids_by_result`` is looked up by.
    ``result`` is the original payload, carried through untouched so a caller
    that falls back loses nothing. ``seed_node_ids`` is the optional inline form
    of the mapping; it is merged with the lookup mapping, and an empty list
    means "this result has no known graph identity", not "expand everything".
    """

    file: str
    score: float = 0.0
    result: dict[str, Any] = Field(default_factory=dict)
    seed_node_ids: list[str] = Field(default_factory=list)

    @field_validator("file")
    @classmethod
    def _clean_file(cls, value: str) -> str:
        return clean_identifier(value, "file")

    @field_validator("seed_node_ids")
    @classmethod
    def _clean_seed_node_ids(cls, value: list[str]) -> list[str]:
        return _unique_node_ids(value)

    @classmethod
    def from_result(
        cls,
        result: Mapping[str, Any],
        seed_node_ids: Sequence[str] = (),
    ) -> RecallSeed:
        """Build a seed from an existing recall result payload.

        Only ``file`` and ``score`` are read out of the payload; everything else
        is preserved verbatim, so an added payload key needs no change here. The
        payload is deep-copied, so a caller mutating its own dict afterwards
        cannot change the report.
        """
        if not isinstance(result, Mapping):
            raise ValueError(
                "a recall result must be a mapping or a RecallSeed, got "
                f"{type(result).__name__}"
            )
        identity = result.get("file")
        if not isinstance(identity, str):
            raise ValueError(
                "a recall result needs a non-empty 'file' identity to be "
                "mapped to graph nodes"
            )
        raw_score = result.get("score", 0.0)
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise ValueError(
                f"recall result {identity!r} has a non-numeric 'score'"
            )
        return cls(
            file=identity,
            score=float(raw_score),
            result=deepcopy(dict(result)),
            seed_node_ids=list(seed_node_ids),
        )


class GraphRecallResult(GraphModel):
    """One original recall result plus the graph material attached to it.

    The original payload stays under ``result``, unmodified — nothing is
    rewritten, removed or re-scored, so an existing consumer can read this field
    and behave exactly as before.

    Everything else is additive explanation (§3.1 goal 8):

    * ``graph_node_ids`` — the seed node IDs that were *requested* for this
      result. It is an echo of the caller's mapping, not a claim that those
      nodes survived the filters; compare with ``graph_nodes``.
    * ``graph_nodes`` / ``graph_edges`` — what the bounded traversal returned,
      sorted by node/edge ID. The edges are the induced subgraph over the
      returned nodes, so the pair renders as a subgraph.
    * ``graph_paths`` — one seed-to-node route per returned node, with stable
      edge IDs, so the result can explain why each node is here.
    * ``provenance`` — :class:`ProvenanceRecord` per returned node that has any
      lineage, sorted by node ID. A record keeps its own ``warnings``; nothing
      is fabricated for a node whose provenance could not be resolved.
    * ``conflicts`` — unresolved contradictions involving the returned nodes,
      reported rather than settled (§8.3).
    * ``truncated`` / ``warnings`` — this result's share of the honesty budget.
    """

    rank: int = Field(default=0, ge=0)
    file: str
    score: float = 0.0
    result: dict[str, Any] = Field(default_factory=dict)

    graph_node_ids: list[str] = Field(default_factory=list)
    graph_nodes: list[GraphNode] = Field(default_factory=list)
    graph_edges: list[GraphEdge] = Field(default_factory=list)
    graph_paths: list[GraphPath] = Field(default_factory=list)
    provenance: list[ProvenanceRecord] = Field(default_factory=list)
    conflicts: list[ConflictReport] = Field(default_factory=list)

    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)

    @field_validator("file")
    @classmethod
    def _clean_file(cls, value: str) -> str:
        return clean_identifier(value, "file")

    @property
    def graph_assisted(self) -> bool:
        """Whether the graph actually contributed anything to this result."""
        return bool(self.graph_nodes)


class GraphRecallReport(GraphModel):
    """The whole graph-assisted answer, in the caller's original ranking.

    ``status`` is the one field a caller must read before trusting the graph
    material: it distinguishes a complete graph-assisted answer from a partial
    one, from a graph that was never consulted, and from a graph outage whose
    results are the untouched seeds.
    """

    status: GraphRecallStatus = GRAPH_STATUS_UNUSED
    #: The backend name taken from :meth:`GraphStore.health`, or ``""`` when no
    #: store was supplied or health could not be read.
    backend: str = ""
    max_hops: int = Field(default=1, ge=0)
    results: list[GraphRecallResult] = Field(default_factory=list)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)

    @property
    def graph_assisted(self) -> bool:
        """Whether any result carries graph material."""
        return any(result.graph_assisted for result in self.results)

    @property
    def preserved_results(self) -> list[dict[str, Any]]:
        """The original recall payloads, in the original ranking.

        The fallback path for a caller that does not care about graph material:
        it is correct to use regardless of ``status``.
        """
        return [result.result for result in self.results]


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------

def expand_recall_with_graph(
    seeds: Sequence[RecallSeed | Mapping[str, Any]],
    store: GraphStore | None = None,
    *,
    seed_node_ids_by_result: Mapping[str, Sequence[str]] | None = None,
    max_hops: int = 1,
    filters: TraversalFilters | None = None,
    top_n: int | None = None,
    include_provenance: bool = True,
    include_conflicts: bool = True,
) -> GraphRecallReport:
    """Expand existing recall results through the memory graph (§7.3 steps 3-7).

    Args:
        seeds: The results an existing search produced, already ranked. Each is
            a :class:`RecallSeed` or a recall result mapping with a ``file``
            identity (see :meth:`RecallSeed.from_result`). The order is taken as
            the ranking and is preserved.
        store: The graph to expand through, or ``None``. ``None`` is a supported
            answer, not an error: the report then carries the untouched seeds
            and ``graph_unavailable``.
        seed_node_ids_by_result: Mapping from a result's ``file`` identity to its
            graph seed node IDs, merged with each seed's inline
            ``seed_node_ids``. This is the *only* seed-to-graph mapping used —
            nothing is discovered, scanned or guessed.
        max_hops: Expansion depth, ``0`` to :data:`MAX_GRAPH_RECALL_HOPS`.
        filters: Hard traversal filters. The default
            :class:`TraversalFilters` is the conservative reading of §7.3 step
            4: current status only, active edges only, weak relations excluded.
            ``filters.scope`` is also used to narrow the conflict scan.
        top_n: Optional cap on how many *top-ranked* mapped results are
            expanded. It never drops a result: beyond the cap a result still
            comes back with its payload and its requested node IDs, and the
            report warns ``graph_expansion_limited:<count>``.
        include_provenance: Whether to backfill evidence (§7.3 step 5).
        include_conflicts: Whether to scan for unresolved contradictions.

    Returns:
        A :class:`GraphRecallReport` whose ``results`` are the supplied seeds in
        the supplied order.

    Raises:
        ValueError: For a caller mistake that cannot be interpreted — an
            out-of-range ``max_hops`` or ``top_n``, a result without a ``file``
            identity, two results sharing one identity, a blank node ID, a
            mapping key matching no result, or a store missing a
            :data:`GRAPH_RECALL_OPERATIONS` method. A *graph* failure is never
            raised; it is reported.
    """
    if max_hops < 0 or max_hops > MAX_GRAPH_RECALL_HOPS:
        raise ValueError(
            f"max_hops must be between 0 and {MAX_GRAPH_RECALL_HOPS} "
            f"(§7.3 step 3), got {max_hops}"
        )
    if top_n is not None and top_n < 1:
        raise ValueError(f"top_n must be at least 1 when given, got {top_n}")

    normalized = _normalize_seeds(seeds, seed_node_ids_by_result)
    if store is not None:
        _require_read_surface(store)

    effective_filters = filters or TraversalFilters()
    warnings: set[str] = set()

    mapped = [seed for seed in normalized if seed.seed_node_ids]
    unmapped_count = len(normalized) - len(mapped)
    if unmapped_count:
        # A count, not a list of identities: which results lack a mapping is
        # already visible per result as an empty ``graph_node_ids``, and a
        # result identity is a path-shaped string.
        warnings.add(f"unmapped_results:{unmapped_count}")

    expandable = mapped if top_n is None else mapped[:top_n]
    if len(expandable) < len(mapped):
        warnings.add(f"graph_expansion_limited:{len(mapped) - len(expandable)}")
    eligible = {seed.file for seed in expandable}

    # -- the three ways the graph contributes nothing ------------------------
    if store is None:
        warnings.add("graph_unavailable:no_store")
        return _fallback_report(
            normalized, GRAPH_STATUS_UNAVAILABLE, "", max_hops, warnings
        )

    backend, health_errors = _probe(store)
    if health_errors:
        warnings.update(health_errors)
        return _fallback_report(
            normalized, GRAPH_STATUS_UNAVAILABLE, backend, max_hops, warnings
        )

    if not expandable:
        warnings.add("no_graph_seeds")
        return _fallback_report(
            normalized, GRAPH_STATUS_UNUSED, backend, max_hops, warnings
        )

    # -- expansion -----------------------------------------------------------
    provenance_cache: dict[str, ProvenanceRecord | None] = {}
    results: list[GraphRecallResult] = []
    traversals = 0

    for rank, seed in enumerate(normalized):
        if seed.file not in eligible:
            results.append(_plain_result(rank, seed))
            continue
        result, succeeded = _expand_one(
            rank,
            seed,
            store,
            effective_filters,
            max_hops,
            include_provenance,
            include_conflicts,
            provenance_cache,
        )
        traversals += int(succeeded)
        warnings.update(result.warnings)
        results.append(result)

    if traversals == 0:
        # Every traversal we attempted failed, so there is no graph answer at
        # all — saying "partial" would overstate it.
        status: GraphRecallStatus = GRAPH_STATUS_UNAVAILABLE
    elif warnings:
        status = GRAPH_STATUS_PARTIAL
    else:
        status = GRAPH_STATUS_OK

    return GraphRecallReport(
        status=status,
        backend=backend,
        max_hops=max_hops,
        results=results,
        truncated=any(result.truncated for result in results),
        warnings=sorted(warnings),
    )


# ---------------------------------------------------------------------------
# Normalization and preconditions
# ---------------------------------------------------------------------------

def _normalize_seeds(
    seeds: Sequence[RecallSeed | Mapping[str, Any]],
    mapping: Mapping[str, Sequence[str]] | None,
) -> list[RecallSeed]:
    """Turn the caller's input into seeds with their merged node IDs.

    A duplicated result identity and a mapping key that names no result are both
    refused: the mapping is keyed on identity, so a duplicate is ambiguous, and
    an unmatched key is how a mapping bug (an absolute key where a relative one
    was expected) would otherwise become an invisible no-op.
    """
    lookup = dict(mapping or {})
    normalized: list[RecallSeed] = []
    seen: set[str] = set()

    for item in seeds:
        seed = item if isinstance(item, RecallSeed) else RecallSeed.from_result(item)
        if seed.file in seen:
            raise ValueError(
                "two recall results share one identity, so the seed mapping is "
                "ambiguous: duplicate result identity"
            )
        seen.add(seed.file)
        normalized.append(seed)

    unmatched = [key for key in lookup if key not in seen]
    if unmatched:
        raise ValueError(
            f"seed_node_ids_by_result names {len(unmatched)} result "
            "identities that are not among the supplied seeds"
        )

    return [
        seed.model_copy(
            update={
                "seed_node_ids": _unique_node_ids(
                    [*seed.seed_node_ids, *lookup.get(seed.file, ())]
                )
            }
        )
        for seed in normalized
    ]


def _require_read_surface(store: object) -> None:
    """Refuse a store that cannot answer the four read operations."""
    missing = sorted(
        name
        for name in GRAPH_RECALL_OPERATIONS
        if not callable(getattr(store, name, None))
    )
    if missing:
        raise ValueError(
            "graph store is missing the read operations graph-assisted recall "
            f"needs: {', '.join(missing)}"
        )


def _probe(store: GraphStore) -> tuple[str, list[str]]:
    """Return ``(backend, errors)`` for the graph's current trustworthiness.

    :meth:`GraphStore.health` is documented never to raise, which is exactly why
    a raising one has to be caught here: this is the call a caller makes when
    something else already failed, and it must not be the thing that breaks
    recall.
    """
    try:
        health = store.health()
    except Exception as exc:
        return "", [f"graph_unavailable:health_failed:{type(exc).__name__}"]
    if health.healthy:
        return health.backend, []
    return health.backend, [
        "graph_unavailable:unhealthy",
        *{f"unhealthy_graph:{_error_label(error)}" for error in health.errors},
    ]


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------

def _plain_result(rank: int, seed: RecallSeed) -> GraphRecallResult:
    """A result with no graph material — the seed, preserved and nothing else.

    ``graph_node_ids`` still echoes what was requested, so a result that was
    mapped but not expanded (beyond ``top_n``, or during an outage) is
    distinguishable from one that had no graph identity at all.
    """
    return GraphRecallResult(
        rank=rank,
        file=seed.file,
        score=seed.score,
        result=deepcopy(seed.result),
        graph_node_ids=list(seed.seed_node_ids),
    )


def _fallback_report(
    seeds: Sequence[RecallSeed],
    status: GraphRecallStatus,
    backend: str,
    max_hops: int,
    warnings: set[str],
) -> GraphRecallReport:
    """The original seed results, unchanged, with an explicit reason."""
    return GraphRecallReport(
        status=status,
        backend=backend,
        max_hops=max_hops,
        results=[_plain_result(rank, seed) for rank, seed in enumerate(seeds)],
        truncated=False,
        warnings=sorted(warnings),
    )


def _expand_one(
    rank: int,
    seed: RecallSeed,
    store: GraphStore,
    filters: TraversalFilters,
    max_hops: int,
    include_provenance: bool,
    include_conflicts: bool,
    provenance_cache: dict[str, ProvenanceRecord | None],
) -> tuple[GraphRecallResult, bool]:
    """Expand one seed. Returns the result and whether the traversal succeeded.

    A traversal failure is reported, never raised (§10.3, §7.4 "graph
    unavailable"): the result keeps its original payload and its requested node
    IDs, and the caller can still answer from the seeds.
    """
    warnings: set[str] = set()
    try:
        traversal = store.traverse(
            seed.seed_node_ids, max_hops=max_hops, filters=filters
        )
    except Exception as exc:
        warnings.add(f"graph_traversal_failed:{type(exc).__name__}")
        result = _plain_result(rank, seed)
        return result.model_copy(update={"warnings": sorted(warnings)}), False

    warnings.update(traversal.warnings)

    # Sorted by stable graph identifier rather than kept in the backend's own
    # order, so two stores holding the same graph produce the same bytes.
    nodes = sorted(traversal.nodes, key=lambda node: node.id)
    edges = sorted(traversal.edges, key=lambda edge: edge.id)
    paths = sorted(
        traversal.paths, key=lambda path: (path.hops, path.node_ids, path.edge_ids)
    )
    node_ids = [node.id for node in nodes]

    provenance: list[ProvenanceRecord] = []
    if include_provenance and node_ids:
        provenance = _backfill_provenance(store, node_ids, provenance_cache, warnings)

    conflicts: list[ConflictReport] = []
    if include_conflicts and node_ids:
        conflicts = _scan_conflicts(store, node_ids, filters, warnings)

    return (
        GraphRecallResult(
            rank=rank,
            file=seed.file,
            score=seed.score,
            result=deepcopy(seed.result),
            graph_node_ids=list(seed.seed_node_ids),
            graph_nodes=nodes,
            graph_edges=edges,
            graph_paths=paths,
            provenance=provenance,
            conflicts=conflicts,
            truncated=traversal.truncated,
            warnings=sorted(warnings),
        ),
        True,
    )


def _backfill_provenance(
    store: GraphStore,
    node_ids: Sequence[str],
    cache: dict[str, ProvenanceRecord | None],
    warnings: set[str],
) -> list[ProvenanceRecord]:
    """Attach evidence lineage to the returned nodes (§7.3 step 5).

    Additive and honest: a record is included exactly as the store reported it,
    with its own ``warnings`` intact. Two kinds of gap are surfaced instead of
    being papered over:

    * ``provenance_unresolved:<node>`` — the store answered, but could not
      resolve part of the lineage. Nothing is invented to fill it.
    * ``provenance_unavailable:<node>`` — the lookup itself failed, so this node
      has *no* record rather than an empty-looking one.

    A node with no lineage at all (an ``Entity`` is an identity node, not a
    remembered statement) contributes no record: an empty record would carry no
    information, and the node is listed in ``graph_nodes`` either way.
    """
    records: list[ProvenanceRecord] = []
    for node_id in node_ids:
        if node_id not in cache:
            try:
                cache[node_id] = store.get_provenance(node_id)
            except Exception:
                cache[node_id] = None
        record = cache[node_id]
        if record is None:
            warnings.add(f"provenance_unavailable:{node_id}")
            continue
        if not record.resolved:
            warnings.add(f"provenance_unresolved:{node_id}")
        if _has_lineage(record):
            records.append(record.model_copy(deep=True))
    return sorted(records, key=lambda record: record.node_id)


def _has_lineage(record: ProvenanceRecord) -> bool:
    return bool(
        record.source_refs
        or record.derivation
        or record.evidence_ids
        or record.episode_ids
        or record.derived_from_ids
        or record.warnings
    )


def _scan_conflicts(
    store: GraphStore,
    node_ids: Sequence[str],
    filters: TraversalFilters,
    warnings: set[str],
) -> list[ConflictReport]:
    """Surface unresolved contradictions touching the returned nodes (§8.3).

    The scan is narrowed to the nodes this result actually returned, in the
    traversal's own scope, so a conflict elsewhere in the graph does not
    discredit an unrelated answer. Each report also raises a warning keyed by
    its lexicographically smallest side — a key that is stable across backends,
    because the contract does not fix the order of ``node_ids``.

    A failing scan becomes ``conflicts_unavailable:<Error>``: "no conflicts
    found" and "could not look" must not read the same.
    """
    try:
        reports = store.find_conflicts(scope=filters.scope, entity_ids=node_ids)
    except Exception as exc:
        warnings.add(f"conflicts_unavailable:{type(exc).__name__}")
        return []
    for report in reports:
        if not report.resolved:
            warnings.add(f"unresolved_conflict:{min(report.node_ids)}")
    return sorted(
        (report.model_copy(deep=True) for report in reports),
        key=lambda report: (report.node_ids, report.conflict_group or ""),
    )


__all__ = [
    "GRAPH_RECALL_OPERATIONS",
    "GRAPH_STATUS_OK",
    "GRAPH_STATUS_PARTIAL",
    "GRAPH_STATUS_UNAVAILABLE",
    "GRAPH_STATUS_UNUSED",
    "MAX_GRAPH_RECALL_HOPS",
    "GraphRecallReport",
    "GraphRecallResult",
    "GraphRecallStatus",
    "RecallSeed",
    "expand_recall_with_graph",
]

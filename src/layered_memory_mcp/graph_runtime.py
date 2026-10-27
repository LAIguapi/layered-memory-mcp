"""The runtime seam between the existing memory paths and the memory graph.

WHAT THIS MODULE IS
-------------------
``ARCHITECTURE_v2.md`` §12 Batch H: the production-side adapter that lets the
already-tested, backend-neutral graph seams be *reached* from the recall and
write paths — under one configured mode, with an explicitly injected store, and
without the graph becoming a second source of truth.

Four responsibilities, each separately testable:

* :func:`build_seed_mapping` — turn existing vector-result metadata into the
  explicit ``seed_node_ids_by_result`` mapping
  :func:`~layered_memory_mcp.graph_recall.expand_recall_with_graph` requires.
* :func:`run_graph_assisted_recall` — call that seam when, and only when, the
  mode and the injected store permit it, and report what happened.
* :func:`run_semantic_graph_write` — project an *explicitly supplied*
  :class:`~layered_memory_mcp.graph_write.SemanticWriteRequest` through
  :func:`~layered_memory_mcp.graph_write.project_semantic_write`, keeping the
  legacy L1/changelog outcome and the graph outcome strictly separate.
* :func:`compare_recall_results` — the opt-in shadow observation: a
  deterministic record of how the two answers differ, deciding nothing.

WHAT IT IS NOT
--------------
- NOT a second truth source. The legacy ranked payloads come back in the legacy
  order in every mode and every failure state; graph material is additive.
- NOT a migration, a backfill or a startup hook. No backend is constructed here:
  a store arrives as an argument or there is no graph (``graph_unavailable``).
- NOT a configuration reader. It never touches ``os.environ``, a home directory,
  a config file or a user memory path, and it imports neither ``config``,
  ``server``, ``injector``, ``recall`` nor any storage module. The mode arrives
  as a :class:`GraphModeDecision`; :meth:`GraphModeDecision.from_config` reads a
  config's own predicates duck-typed, so the mode strings live in exactly one
  place (``config.GRAPH_MODES``).
- NOT an extractor or a decision layer. No claim extraction, entity resolution,
  contradiction detection or semantic judgement is derived from raw content
  here; a graph write happens only because a caller built the request.
- NOT a provenance ledger. ``record_memory_change`` is never called and no
  second event log is created: the ledger stays the one the caller passes to
  :func:`run_semantic_graph_write`.

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Identity comes from metadata, never from inference.** Only the explicit
   :data:`GRAPH_SEED_ID_KEY` / :data:`GRAPH_SEED_IDS_KEY` metadata keys produce a
   graph identity. A filename, a domain, a heading, a body or a hash never does.
   A row with no mapping is a counted, visible fallback.
2. **Legacy survives.** With the graph disabled, absent, unhealthy, raising or
   unmapped, :attr:`GraphRecallRuntimeReport.results` is the original payload
   list in the original order.
3. **The mode is the only switch.** ``legacy`` reaches no graph operation at all;
   ``shadow`` may plan and read but may never commit or claim authority; only
   ``graph_write``/``graph`` may project a write, and only with both an explicit
   request and an explicit store.
4. **The two write paths are reported separately.** A graph projection failure
   leaves :attr:`GraphWriteOutcome.legacy_write_ok` exactly as the caller
   reported it: nothing here rolls back, or claims to roll back, an L1 write.
5. **Honesty over convenience** (§10.3). Every degradation is a status plus a
   sorted, bounded warning label — never an empty successful-looking answer.

PRIVACY
-------
Reports carry graph IDs, result identities, counts, statuses, relation type names
and exception *class* names only — never a memory body, a section payload, a
store diagnostic string or a host path (§15). The comparison record is
deliberately identity-and-count shaped for the same reason.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pydantic import Field, field_validator, model_validator

from .graph_models import (
    GraphEdge,
    GraphModel,
    GraphNode,
    MemoryEvent,
    clean_identifier,
)
from .graph_recall import (
    GraphRecallReport,
    RecallSeed,
    expand_recall_with_graph,
)
from .graph_store import GraphStore, TraversalFilters
from .graph_write import (
    SemanticWriteError,
    SemanticWritePlan,
    SemanticWriteProjection,
    SemanticWriteRequest,
    plan_semantic_write,
    project_semantic_write,
)

#: Metadata key holding one graph node ID for a vector result. The canonical
#: form: one row, one semantic node.
GRAPH_SEED_ID_KEY = "graph_node_id"

#: Metadata key holding several graph node IDs for one vector result, for a row
#: that projected to more than one node.
GRAPH_SEED_IDS_KEY = "graph_node_ids"

#: The metadata container on a vector result (``storage.vector_store.search``
#: already returns a parsed JSON dict under this key).
GRAPH_METADATA_KEY = "metadata"

#: The result identity the mapping is keyed by — the same key
#: :meth:`~layered_memory_mcp.graph_recall.RecallSeed.from_result` reads. A
#: display title is deliberately *not* usable as a key: two files can share one.
GRAPH_RESULT_IDENTITY_KEY = "file"

#: The mode forbids graph writes. Nothing was planned and nothing was reached.
GRAPH_WRITE_NOT_ENABLED = "graph_write_not_enabled"

#: No explicit :class:`~layered_memory_mcp.graph_write.SemanticWriteRequest` was
#: supplied, so there was no decision to project. A successful legacy write is
#: *not* a decision (§3.4).
GRAPH_WRITE_NO_REQUEST = "graph_write_no_request"

#: The mode allows a graph write but no store was injected. Visibly not
#: configured, rather than a guessed backend.
GRAPH_WRITE_NOT_CONFIGURED = "graph_write_not_configured"

#: Shadow: the decision was planned and validated, and deliberately not applied.
GRAPH_WRITE_PREPARED = "graph_write_prepared"

#: The plan reached the store's own ``rebuild``. Read
#: :attr:`GraphWriteOutcome.committed` for whether that rebuild was clean.
GRAPH_WRITE_PROJECTED = "graph_write_projected"

#: The store's ``rebuild`` itself failed. The legacy write is untouched.
GRAPH_WRITE_FAILED = "graph_write_failed"

#: Status for "the graph was never consulted because the mode forbids it".
#: Deliberately distinct from
#: :data:`~layered_memory_mcp.graph_recall.GRAPH_STATUS_UNAVAILABLE`: "switched
#: off" and "broken" must not read the same in an operator's report.
GRAPH_STATUS_DISABLED = "graph_disabled"


# ---------------------------------------------------------------------------
# The mode decision
# ---------------------------------------------------------------------------

class GraphModeDecision(GraphModel):
    """The resolved graph integration decision, as data.

    The mode *strings* live in ``config.GRAPH_MODES``; this module only ever
    sees the three predicates the config derived from them, so adding a mode
    cannot leave a stale string comparison behind in the runtime seam.

    ``shadow_enabled`` is mutually exclusive with the two authoritative flags:
    shadow means the graph is observed, so a decision claiming both would be
    exactly the half-enabled state Batch H must not be able to reach.
    """

    mode: str
    recall_enabled: bool = False
    write_enabled: bool = False
    shadow_enabled: bool = False

    @field_validator("mode")
    @classmethod
    def _clean_mode(cls, value: str) -> str:
        return clean_identifier(value, "mode")

    @model_validator(mode="after")
    def _check_exclusive(self) -> GraphModeDecision:
        if self.shadow_enabled and (self.recall_enabled or self.write_enabled):
            raise ValueError(
                "a shadow decision may not also be authoritative: shadow "
                "observes the graph, it never decides the answer or the write"
            )
        return self

    @classmethod
    def from_config(cls, config: Any) -> GraphModeDecision:
        """Read a :class:`~layered_memory_mcp.config.MemoryConfig`'s predicates.

        Duck-typed on purpose: this module does not import the config layer, and
        a caller may pass any object exposing the same four attributes (a test
        double, a future per-request override). Missing attributes resolve to the
        conservative answer — legacy.
        """
        return cls(
            mode=str(getattr(config, "graph_mode", "legacy") or "legacy"),
            recall_enabled=bool(getattr(config, "graph_recall_enabled", False)),
            write_enabled=bool(getattr(config, "graph_write_enabled", False)),
            shadow_enabled=bool(getattr(config, "graph_shadow_enabled", False)),
        )

    @property
    def observes_recall(self) -> bool:
        """Whether graph-assisted recall may *run* (authoritative or observed)."""
        return self.recall_enabled or self.shadow_enabled

    @property
    def prepares_write(self) -> bool:
        """Whether a semantic write may be planned (committed only if enabled)."""
        return self.write_enabled or self.shadow_enabled


#: The default decision: the old paths, nothing else.
LEGACY_MODE_DECISION = GraphModeDecision(mode="legacy")


# ---------------------------------------------------------------------------
# Seed mapping (§3.3)
# ---------------------------------------------------------------------------

class SeedMapping(GraphModel):
    """The explicit result-identity → graph-node-IDs mapping, plus its gaps.

    ``seed_node_ids_by_result`` is exactly what
    :func:`~layered_memory_mcp.graph_recall.expand_recall_with_graph` takes as
    ``seed_node_ids_by_result``; results that contributed nothing are simply
    absent from it (that seam refuses a mapping key naming no result, so an
    empty list is never inserted).

    The counters and ``warnings`` are the honest remainder: how many rows have
    no graph identity yet, and why. Counts rather than identity lists, except
    where the identity itself is the actionable fact.
    """

    seed_node_ids_by_result: dict[str, list[str]] = Field(default_factory=dict)
    mapped_results: int = Field(default=0, ge=0)
    unmapped_results: int = Field(default=0, ge=0)
    rejected_ids: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)

    @property
    def total_node_ids(self) -> int:
        return sum(len(ids) for ids in self.seed_node_ids_by_result.values())


def build_seed_mapping(results: Sequence[Any]) -> SeedMapping:
    """Derive graph seed IDs from explicit vector-result metadata only.

    The rules, all deterministic and all tested:

    * a scalar :data:`GRAPH_SEED_ID_KEY` becomes a one-item list;
    * a list/tuple :data:`GRAPH_SEED_IDS_KEY` becomes a de-duplicated list in
      first-occurrence order, appended after the scalar;
    * an ID must be a non-blank, single-line ``str``; anything else — a number,
      a bool, ``None``, a nested container, a blank or a wrapped string — is
      ignored and counted as ``graph_ids_rejected``, never coerced into an ID;
    * a non-sequence (or a bare string) under the list key is one rejected entry,
      not a character-by-character sequence;
    * absent metadata, non-mapping metadata and metadata without either key each
      map nothing and raise their own counted warning;
    * two rows sharing one ``file`` identity merge into one entry, in row order,
      and are counted — the mapping is keyed by identity, so a duplicate cannot
      be represented twice. (The recall seam separately refuses *results* that
      share an identity; this helper stays deterministic rather than raising.)

    Nothing is inferred from a filename, a domain, a heading, a body or a hash,
    and nothing is read from disk, a store, the environment or a memory path.
    The input is never mutated.
    """
    mapping: dict[str, list[str]] = {}
    without_identity = 0
    metadata_absent = 0
    metadata_not_mapping = 0
    metadata_without_ids = 0
    rejected = 0
    duplicates = 0
    unmapped = 0

    for result in results:
        identity = _result_identity(result)
        if identity is None:
            without_identity += 1
            continue

        metadata = result.get(GRAPH_METADATA_KEY, None)
        if metadata is None and GRAPH_METADATA_KEY not in result:
            metadata_absent += 1
            unmapped += 1
            continue
        if not isinstance(metadata, Mapping):
            metadata_not_mapping += 1
            unmapped += 1
            continue
        if GRAPH_SEED_ID_KEY not in metadata and GRAPH_SEED_IDS_KEY not in metadata:
            metadata_without_ids += 1
            unmapped += 1
            continue

        node_ids, bad = _graph_ids_from_metadata(metadata)
        rejected += bad
        if not node_ids:
            unmapped += 1
            continue
        if identity in mapping:
            duplicates += 1
            existing = mapping[identity]
            mapping[identity] = existing + [
                node_id for node_id in node_ids if node_id not in existing
            ]
            continue
        mapping[identity] = node_ids

    warnings: list[str] = []
    for label, count in (
        ("results_without_identity", without_identity),
        ("graph_metadata_absent", metadata_absent),
        ("graph_metadata_not_a_mapping", metadata_not_mapping),
        ("graph_metadata_without_ids", metadata_without_ids),
        ("graph_ids_rejected", rejected),
        ("duplicate_result_identities", duplicates),
        ("unmapped_results", unmapped),
    ):
        if count:
            warnings.append(f"{label}:{count}")

    return SeedMapping(
        seed_node_ids_by_result=mapping,
        mapped_results=len(mapping),
        unmapped_results=unmapped,
        rejected_ids=rejected,
        warnings=sorted(warnings),
    )


def _result_identity(result: Any) -> str | None:
    """The legacy result identity, or ``None`` when there is no usable one.

    ``None`` rather than a raise: a malformed row is a reportable gap at the
    orchestration layer, and dropping the *mapping* for it still preserves the
    row itself in the recall answer.
    """
    if not isinstance(result, Mapping):
        return None
    return _clean_seed_id(result.get(GRAPH_RESULT_IDENTITY_KEY))


def _graph_ids_from_metadata(metadata: Mapping[str, Any]) -> tuple[list[str], int]:
    """Return ``(node_ids, rejected_count)`` for one result's metadata."""
    node_ids: list[str] = []
    rejected = 0

    if GRAPH_SEED_ID_KEY in metadata:
        cleaned = _clean_seed_id(metadata[GRAPH_SEED_ID_KEY])
        if cleaned is None:
            rejected += 1
        else:
            node_ids.append(cleaned)

    if GRAPH_SEED_IDS_KEY in metadata:
        raw = metadata[GRAPH_SEED_IDS_KEY]
        if isinstance(raw, (list, tuple)):
            for value in raw:
                cleaned = _clean_seed_id(value)
                if cleaned is None:
                    rejected += 1
                elif cleaned not in node_ids:
                    node_ids.append(cleaned)
        else:
            # A bare string here is a caller mistake, not a character sequence.
            rejected += 1

    return node_ids, rejected


def _clean_seed_id(value: Any) -> str | None:
    """A graph ID is a non-blank single-line ``str``, or it is not an ID.

    ``bool`` is excluded explicitly even though it is not a ``str``: coercing any
    non-string to an identifier is exactly how a graph would end up holding
    ``"True"`` as a node ID.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    if "\n" in value or "\r" in value:
        return None
    text = value.strip()
    return text or None


# ---------------------------------------------------------------------------
# Graph-assisted recall (§3.2)
# ---------------------------------------------------------------------------

class GraphRecallRuntimeReport(GraphModel):
    """One recall answer, plus what the configured mode let the graph add.

    ``results`` is the contract an existing consumer reads: the legacy ranked
    payloads, in the legacy order, deep-copied. It is correct to use in every
    mode and every failure state — which is what makes the mode reversible.

    ``report`` is the graph seam's own
    :class:`~layered_memory_mcp.graph_recall.GraphRecallReport`, or ``None`` when
    the mode forbade consulting the graph at all. ``graph_authoritative`` is the
    one field that says whether the graph material may *decide* anything: it is
    false in ``legacy``, false in ``shadow`` (observation only) and false in an
    authoritative mode whose graph turned out to be unusable.
    """

    mode: str
    status: str = GRAPH_STATUS_DISABLED
    backend: str = ""
    graph_authoritative: bool = False
    results: list[dict[str, Any]] = Field(default_factory=list)
    seed_mapping: SeedMapping = Field(default_factory=SeedMapping)
    report: GraphRecallReport | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def graph_assisted(self) -> bool:
        """Whether any result actually carries graph material."""
        return self.report is not None and self.report.graph_assisted

    @property
    def graph_results(self) -> list[Any]:
        """The per-result graph material, or an empty list when there is none."""
        return list(self.report.results) if self.report is not None else []


def run_graph_assisted_recall(
    results: Sequence[Any],
    store: GraphStore | None = None,
    *,
    mode: GraphModeDecision = LEGACY_MODE_DECISION,
    max_hops: int = 1,
    filters: TraversalFilters | None = None,
    top_n: int | None = None,
    include_provenance: bool = True,
    include_conflicts: bool = True,
) -> GraphRecallRuntimeReport:
    """Expand already-ranked legacy results through the graph, if the mode allows.

    The adapter does four things and no more: read the explicit metadata mapping,
    decide from the mode whether the graph may be consulted, hand the seam the
    seeds and the mapping, and report. It computes no score, changes no ranking
    and drops no result.

    Args:
        results: The ranked legacy result payloads, in ranking order. Each must
            be a mapping with the ``file`` identity the existing recall path
            already emits.
        store: The graph to expand through, injected by the runtime assembly.
            ``None`` is a supported answer — ``graph_unavailable:no_store`` — not
            an error, so a mode can be enabled before a backend exists.
        mode: The resolved :class:`GraphModeDecision`. Defaults to legacy, so a
            caller that forgets it gets the old behaviour rather than the graph.
        max_hops: Expansion depth, passed through to the seam (0-2).
        filters: Hard traversal filters, passed through unchanged.
        top_n: Optional cap on how many top-ranked mapped results are expanded.
        include_provenance: Whether to backfill evidence lineage.
        include_conflicts: Whether to scan for unresolved contradictions.

    Returns:
        A :class:`GraphRecallRuntimeReport` whose ``results`` are always the
        legacy payloads in the legacy order.

    Raises:
        ValueError: Only for a caller-contract mistake the graph seam itself
            defines — a result that is not a mapping, a result without a ``file``
            identity, two results sharing one identity, an out-of-range
            ``max_hops``/``top_n``, or a store missing a read operation. A
            *graph* failure (absent, unhealthy, raising, unmapped) is never
            raised: it is reported and the legacy answer is preserved.
    """
    preserved = _preserved_results(results)

    if not mode.observes_recall:
        # Legacy: not one graph operation is reached, not even health().
        return GraphRecallRuntimeReport(
            mode=mode.mode,
            status=GRAPH_STATUS_DISABLED,
            results=preserved,
        )

    mapping = build_seed_mapping(preserved)
    report = expand_recall_with_graph(
        [RecallSeed.from_result(result) for result in preserved],
        store,
        seed_node_ids_by_result=mapping.seed_node_ids_by_result,
        max_hops=max_hops,
        filters=filters,
        top_n=top_n,
        include_provenance=include_provenance,
        include_conflicts=include_conflicts,
    )

    return GraphRecallRuntimeReport(
        mode=mode.mode,
        status=report.status,
        backend=report.backend,
        # Authority is the mode's to grant and the graph's to earn: an
        # authoritative mode over an unusable graph still answers from the seeds.
        graph_authoritative=bool(mode.recall_enabled and report.graph_assisted),
        # The seam preserves order and payload; this is that list, not a re-rank.
        results=report.preserved_results,
        seed_mapping=mapping,
        report=report,
        warnings=sorted({*mapping.warnings, *report.warnings}),
    )


def _preserved_results(results: Sequence[Any]) -> list[dict[str, Any]]:
    """Deep-copy the legacy payloads, refusing a row that is not a mapping.

    The copy is what stops a caller mutating its own result dicts from changing
    a report it already received (and vice versa).
    """
    preserved: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError(
                "a recall result must be a mapping, got "
                f"{type(result).__name__}"
            )
        preserved.append(deepcopy(dict(result)))
    return preserved


def graph_recall_payload(report: GraphRecallRuntimeReport) -> dict[str, Any]:
    """The additive, bounded MCP response block for one graph-assisted recall.

    Deliberately *additive*: the existing response fields are untouched and this
    goes beside them, so a consumer that ignores it behaves exactly as before.

    Deliberately *bounded* (§15): result identities, graph IDs, counts, statuses
    and warning labels only. No node body, no section text, no provenance quote
    and no store diagnostic — the block is safe to log, and a graph-enabled
    response cannot become a second copy of the user's memory.

    Only graph-assisted results appear under ``results``; a row that was not
    expanded is already fully described by the legacy payload beside it.
    """
    entries: list[dict[str, Any]] = []
    for result in report.graph_results:
        if not result.graph_assisted:
            continue
        entries.append(
            {
                "file": result.file,
                "graph_node_ids": list(result.graph_node_ids),
                "graph_nodes": [node.id for node in result.graph_nodes],
                "graph_edges": [edge.id for edge in result.graph_edges],
                "provenance_records": len(result.provenance),
                "conflicts": len(result.conflicts),
                "truncated": result.truncated,
                "warnings": list(result.warnings),
            }
        )

    return {
        "mode": report.mode,
        "status": report.status,
        "backend": report.backend,
        "authoritative": report.graph_authoritative,
        "graph_assisted": report.graph_assisted,
        "max_hops": report.report.max_hops if report.report is not None else 0,
        "mapped_results": report.seed_mapping.mapped_results,
        "unmapped_results": report.seed_mapping.unmapped_results,
        "rejected_ids": report.seed_mapping.rejected_ids,
        "truncated": report.report.truncated if report.report is not None else False,
        "warnings": list(report.warnings),
        "results": entries,
    }


# ---------------------------------------------------------------------------
# Semantic graph writes (§3.4)
# ---------------------------------------------------------------------------

class GraphWriteOutcome(GraphModel):
    """What the configured mode let one explicit semantic decision become.

    The two paths are deliberately two fields. ``legacy_write_ok`` is the
    caller's own L1/changelog outcome, carried through *verbatim*: nothing here
    rolls back, re-runs or reinterprets it, because the legacy path is still the
    authoritative one in this batch. ``committed`` is the graph side alone, and
    it is true only when a projection actually reached the store and that store's
    own rebuild reported no error.

    ``plan`` is present whenever a decision was validated (shadow included);
    ``projection`` only when the store was actually asked to rebuild.
    """

    mode: str
    status: str = GRAPH_WRITE_NOT_ENABLED
    legacy_write_ok: bool = True
    plan: SemanticWritePlan | None = None
    projection: SemanticWriteProjection | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def committed(self) -> bool:
        """Whether graph state actually changed cleanly. Never inferred."""
        return (
            self.status == GRAPH_WRITE_PROJECTED
            and self.projection is not None
            and self.projection.ok
        )

    @property
    def node_id(self) -> str:
        return self.plan.node_id if self.plan is not None else ""


def run_semantic_graph_write(
    *,
    mode: GraphModeDecision = LEGACY_MODE_DECISION,
    request: SemanticWriteRequest | None = None,
    store: GraphStore | None = None,
    source_events: Sequence[MemoryEvent] = (),
    legacy_write_ok: bool = True,
    existing_nodes: Mapping[str, GraphNode] | Sequence[GraphNode] | None = None,
    existing_edges: Mapping[str, GraphEdge] | Sequence[GraphEdge] | None = None,
) -> GraphWriteOutcome:
    """Project an explicitly supplied semantic write decision, if the mode allows.

    What this function deliberately does *not* do: extract a claim, resolve an
    entity, detect a contradiction, decide a supersession, or infer anything
    whatsoever from a piece of ``content``. A graph write exists here only
    because a caller built a
    :class:`~layered_memory_mcp.graph_write.SemanticWriteRequest` and handed it
    over, which is what keeps that judgement reviewable where it is made.

    It also never touches the legacy provenance record: ``record_memory_change``
    is not called and no second ledger is created — ``source_events`` is the
    caller's own append-only stream and stays the only one.

    The mode matrix:

    * ``legacy`` — nothing is planned and no store operation is reached, even
      when a request and a store are both supplied.
    * ``shadow`` — the decision is planned and validated and the plan is
      reported; the store is *not* asked to rebuild, so no graph state changes.
    * ``graph_write`` / ``graph`` — the plan is projected through
      :func:`~layered_memory_mcp.graph_write.project_semantic_write`, and only
      when both an explicit request and an explicit store exist. Either missing
      is a visible ``no_request`` / ``not_configured`` status, never a guess.

    Args:
        mode: The resolved :class:`GraphModeDecision`. Defaults to legacy.
        request: The explicit decision, or ``None`` for "there is none".
        store: The graph to project into, injected by the runtime assembly.
        source_events: The caller's existing event stream, projected with the
            write (``rebuild`` is a rebuild, not a merge).
        legacy_write_ok: Whether the authoritative legacy write succeeded. Only
            reported — it never gates or triggers the graph path.
        existing_nodes: Extra node material for target validation.
        existing_edges: Extra edge material for the already-asserted warning.

    Returns:
        A :class:`GraphWriteOutcome` whose two paths are reported separately.

    Raises:
        ValueError: For a caller-contract mistake the write seam defines — a
            target that is not in the supplied material, an ambiguous
            declaration, a store missing ``rebuild``. An invalid plan never
            reaches the store.
    """
    warnings: list[str] = []
    if not legacy_write_ok:
        # Visible, because a graph projection beside a failed L1 write is a
        # divergence a reviewer must see — but it is the caller's call, not ours.
        warnings.append("legacy_write_not_ok")

    def _outcome(status: str, **fields: Any) -> GraphWriteOutcome:
        return GraphWriteOutcome(
            mode=mode.mode,
            status=status,
            legacy_write_ok=legacy_write_ok,
            warnings=sorted({*warnings, *fields.pop("extra_warnings", ())}),
            **fields,
        )

    if not mode.prepares_write:
        return _outcome(GRAPH_WRITE_NOT_ENABLED)

    if request is None:
        return _outcome(GRAPH_WRITE_NO_REQUEST)

    if mode.shadow_enabled:
        # Planning is pure: it reads the supplied material and nothing else.
        plan = plan_semantic_write(
            request,
            existing_nodes=_material_nodes(source_events, existing_nodes),
            existing_edges=_material_edges(source_events, existing_edges),
        )
        return _outcome(
            GRAPH_WRITE_PREPARED,
            plan=plan,
            extra_warnings=["graph_write_shadow_only", *plan.warnings],
        )

    if store is None:
        return _outcome(
            GRAPH_WRITE_NOT_CONFIGURED,
            extra_warnings=["graph_write_not_configured:no_store"],
        )

    events = list(source_events)
    try:
        projection = project_semantic_write(
            store,
            events,
            request,
            existing_nodes=existing_nodes,
            existing_edges=existing_edges,
        )
    except SemanticWriteError as exc:
        # The rebuild failed. The L1 write stays exactly as the caller reported
        # it: this layer has no transaction spanning both, and pretending
        # otherwise would be the §10.3 dishonesty the whole seam avoids.
        plan = plan_semantic_write(
            request,
            existing_nodes=_material_nodes(events, existing_nodes),
            existing_edges=_material_edges(events, existing_edges),
        )
        return _outcome(
            GRAPH_WRITE_FAILED,
            plan=plan,
            extra_warnings=[f"graph_write_failed:{_failure_label(exc)}"],
        )

    extra = list(projection.plan.warnings)
    if not projection.ok:
        extra.append(f"graph_write_reported_errors:{len(projection.report.errors)}")
    return _outcome(
        GRAPH_WRITE_PROJECTED,
        plan=projection.plan,
        projection=projection,
        extra_warnings=extra,
    )


def _failure_label(exc: BaseException) -> str:
    """The class name of what actually broke — never a backend message (§15)."""
    cause = exc.__cause__
    return type(cause if cause is not None else exc).__name__


def _material_nodes(
    source_events: Sequence[MemoryEvent],
    existing_nodes: Mapping[str, GraphNode] | Sequence[GraphNode] | None,
) -> dict[str, GraphNode]:
    """Node material for a *planning-only* call, from the explicit inputs.

    The same rule :func:`~layered_memory_mcp.graph_write.project_semantic_write`
    applies: targets are checked against the stream the caller is projecting,
    widened by whatever extra material the caller named. No store is read.
    """
    nodes: dict[str, GraphNode] = {}
    for event in sorted(source_events, key=lambda e: (e.occurred_at, e.id)):
        if event.node is not None:
            nodes[event.node.id] = event.node
    if existing_nodes is None:
        return nodes
    extra = (
        existing_nodes.values()
        if isinstance(existing_nodes, Mapping)
        else existing_nodes
    )
    for node in extra:
        nodes[node.id] = node
    return nodes


def _material_edges(
    source_events: Sequence[MemoryEvent],
    existing_edges: Mapping[str, GraphEdge] | Sequence[GraphEdge] | None,
) -> dict[str, GraphEdge]:
    """Edge material for a planning-only call, from the explicit inputs."""
    edges: dict[str, GraphEdge] = {}
    for event in sorted(source_events, key=lambda e: (e.occurred_at, e.id)):
        if event.edge is not None:
            edges[event.edge.id] = event.edge
    if existing_edges is None:
        return edges
    extra = (
        existing_edges.values()
        if isinstance(existing_edges, Mapping)
        else existing_edges
    )
    for edge in extra:
        edges[edge.id] = edge
    return edges


def graph_write_payload(outcome: GraphWriteOutcome) -> dict[str, Any]:
    """The additive, bounded report block for one semantic graph write.

    IDs, counts, relation type names, statuses and warning labels only — never
    the node body the caller wrote, never a source path, never a store
    diagnostic (§15). The legacy outcome stays a separate field so a reader
    cannot mistake one path's success for the other's.
    """
    plan = outcome.plan
    projection = outcome.projection
    return {
        "mode": outcome.mode,
        "status": outcome.status,
        "legacy_write_ok": outcome.legacy_write_ok,
        "committed": outcome.committed,
        "node_id": outcome.node_id,
        "event_ids": list(plan.event_ids) if plan is not None else [],
        "edge_ids": list(plan.edge_ids) if plan is not None else [],
        "superseded_ids": list(plan.superseded_ids) if plan is not None else [],
        "contradicted_ids": list(plan.contradicted_ids) if plan is not None else [],
        "relation_counts": plan.relation_counts if plan is not None else {},
        "events_projected": (
            projection.events_projected if projection is not None else 0
        ),
        "rebuild_errors": (
            len(projection.report.errors) if projection is not None else 0
        ),
        "warnings": list(outcome.warnings),
    }


# ---------------------------------------------------------------------------
# Shadow comparison (§3.5)
# ---------------------------------------------------------------------------

class RecallComparison(GraphModel):
    """A deterministic record of how two recall answers differ.

    What it deliberately does not have: a verdict. There is no "winner", no
    "preferred" and no score — deciding which answer is right is a review
    decision made with shadow evidence, not something a comparison helper may
    quietly make on the hot path.

    ``identical`` is about *result identity*: same identities, same order, same
    count. Added graph material does not break it, because graph material is
    additive by construction; it is reported separately through
    ``graph_assisted_results`` and a ``graph_material_added:<n>`` difference.
    """

    mode: str = ""
    status: str = ""
    old_count: int = Field(default=0, ge=0)
    new_count: int = Field(default=0, ge=0)
    identical: bool = True
    order_preserved: bool = True
    dropped_identities: list[str] = Field(default_factory=list)
    added_identities: list[str] = Field(default_factory=list)
    graph_assisted_results: int = Field(default=0, ge=0)
    differences: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def compare_recall_results(
    old_results: Sequence[Any],
    new: GraphRecallRuntimeReport | GraphRecallReport | Sequence[Any],
) -> RecallComparison:
    """Compare the legacy answer with the graph-assisted one. Decide nothing.

    Opt-in by construction: nothing in the recall path calls this, so a legacy
    response is unaffected whether or not it exists.

    Args:
        old_results: The legacy ranked payloads.
        new: A :class:`GraphRecallRuntimeReport`, a bare
            :class:`~layered_memory_mcp.graph_recall.GraphRecallReport`, or a
            plain list of result payloads.

    Returns:
        A :class:`RecallComparison` built only from identities, order, counts,
        statuses and bounded labels — never a memory body, so the record is safe
        to log or sample (§15). Neither input is read destructively or mutated.
    """
    mode, status, new_results, graph_assisted = _comparison_subject(new)

    old_ids, old_missing = _comparison_identities(old_results)
    new_ids, new_missing = _comparison_identities(new_results)

    dropped = sorted(set(old_ids) - set(new_ids))
    added = sorted(set(new_ids) - set(old_ids))
    common = set(old_ids) & set(new_ids)
    # Order is compared over the identities both sides have, so a dropped row
    # does not also read as a re-ranking.
    order_preserved = [i for i in old_ids if i in common] == [
        i for i in new_ids if i in common
    ]

    differences: list[str] = []
    if len(old_ids) != len(new_ids):
        differences.append("count_changed")
    if dropped:
        differences.append(f"identities_dropped:{len(dropped)}")
    if added:
        differences.append(f"identities_added:{len(added)}")
    if not order_preserved:
        differences.append("order_changed")
    if graph_assisted:
        differences.append(f"graph_material_added:{graph_assisted}")

    warnings: list[str] = []
    if old_missing or new_missing:
        warnings.append(f"results_without_identity:{old_missing + new_missing}")

    return RecallComparison(
        mode=mode,
        status=status,
        old_count=len(old_ids),
        new_count=len(new_ids),
        identical=not dropped and not added and order_preserved
        and len(old_ids) == len(new_ids),
        order_preserved=order_preserved,
        dropped_identities=dropped,
        added_identities=added,
        graph_assisted_results=graph_assisted,
        differences=sorted(differences),
        warnings=sorted(warnings),
    )


def _comparison_subject(
    new: GraphRecallRuntimeReport | GraphRecallReport | Sequence[Any],
) -> tuple[str, str, Sequence[Any], int]:
    """Normalize the "new" side to ``(mode, status, results, graph_assisted)``."""
    if isinstance(new, GraphRecallRuntimeReport):
        return (
            new.mode,
            new.status,
            new.results,
            sum(1 for result in new.graph_results if result.graph_assisted),
        )
    if isinstance(new, GraphRecallReport):
        return (
            "",
            new.status,
            new.preserved_results,
            sum(1 for result in new.results if result.graph_assisted),
        )
    return "", "", new, 0


def _comparison_identities(results: Sequence[Any]) -> tuple[list[str], int]:
    """Identities in order, plus how many rows had none to compare by."""
    identities: list[str] = []
    missing = 0
    for result in results:
        identity = _result_identity(result)
        if identity is None:
            missing += 1
            continue
        identities.append(identity)
    return identities, missing


__all__ = [
    "GRAPH_METADATA_KEY",
    "GRAPH_RESULT_IDENTITY_KEY",
    "GRAPH_SEED_ID_KEY",
    "GRAPH_SEED_IDS_KEY",
    "GRAPH_STATUS_DISABLED",
    "GRAPH_WRITE_FAILED",
    "GRAPH_WRITE_NOT_CONFIGURED",
    "GRAPH_WRITE_NOT_ENABLED",
    "GRAPH_WRITE_NO_REQUEST",
    "GRAPH_WRITE_PREPARED",
    "GRAPH_WRITE_PROJECTED",
    "LEGACY_MODE_DECISION",
    "GraphModeDecision",
    "GraphRecallRuntimeReport",
    "GraphWriteOutcome",
    "RecallComparison",
    "SeedMapping",
    "build_seed_mapping",
    "compare_recall_results",
    "graph_recall_payload",
    "graph_write_payload",
    "run_graph_assisted_recall",
    "run_semantic_graph_write",
]

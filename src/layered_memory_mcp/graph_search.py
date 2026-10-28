"""One graph recall policy, shared by every read transport.

WHAT THIS MODULE IS
-------------------
ARCHITECTURE_v2.md framework contract: "Unify graph use for MCP
``search_semantic``, MCP ``recall_knowledge``, and REST ``/knowledge/search``.
Carry stable entry/seed identity from the actual producer … A healthy graph's
temporal/lifecycle/scope filters must affect the returned primary result, not
just an optional sidecar."

:func:`apply_graph_recall` is that policy, in one place:

1. **Identity from the producer.** A vector row's identity is its own ``id`` —
   which, for ordinary Markdown, *is* the versioned projection entry ID the
   published surface carries. A keyword result's identity is the exact,
   occurrence-aware list of H2 entry IDs its matched sections produced
   (:func:`keyword_section_seeds`). Neither is guessed from a heading, a body
   or a filename.
2. **Owner from the managed registry, then the legacy rule.** A surface the
   journal published is owned by the node the registry names. Everything else
   falls back to the existing deterministic entry-ID → claim-ID derivation, so
   an unmigrated store behaves exactly as before.
3. **The graph governs the primary answer.** When the graph answered, a result
   whose seed nodes did not survive the traversal filters — superseded,
   retracted, expired, out of its validity window, out of scope — is removed
   from the returned results, not merely annotated.
   The filters themselves are :func:`recall_policy`, resolved once per read from
   the configuration: *current-time validity* (``as_of`` = now, so a claim whose
   window has not opened or has already closed is not current) and the
   *configured namespace union* (own ∪ ``shared`` ∪ unscoped, with ``shared``
   itself meaning no isolation). Both apply to expansions as well as to seeds,
   and both live here rather than in a transport.
4. **Pending projections are stale, and say so.** When the journal holds
   accepted events the readable projections have not published, the rows those
   events replace are excluded even on the unavailable-graph fallback. The
   alternative is answering with a fact the authority has already replaced.
5. **Legacy survives everything else.** Legacy mode returns before any graph
   work, an unmapped row is never dropped, and a safe graph outage preserves
   the documented payload exactly.

WHAT IT IS NOT
--------------
- NOT a re-ranker. Order and scores are the producer's; this layer only removes
  rows the graph says are not current, and says how many it removed.
- NOT a second runtime. The expansion is
  :func:`~layered_memory_mcp.graph_runtime.run_graph_assisted_recall` and the
  store lifecycle is the existing assembly session, reused rather than restated.
- NOT a transport. It returns data; building a response is the caller's job,
  and the block it returns has the same field names everywhere.

PRIVACY (§15)
-------------
The block carries identities, counts, statuses and bounded labels, exactly as
``graph_recall_payload`` already did, plus the exclusion counts. No body, no
path, no backend message.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from . import graph_runtime, graph_runtime_assembly
from .graph_recall import GRAPH_STATUS_OK, GRAPH_STATUS_PARTIAL
from .graph_store import TraversalFilters

__all__ = [
    "GRAPH_EXCLUDED_LIFECYCLE",
    "GRAPH_EXCLUDED_PENDING",
    "GRAPH_EXCLUDED_SCOPE",
    "KEYWORD_SECTION_KEY",
    "SHARED_NAMESPACE",
    "GraphSearchOutcome",
    "RecallPolicy",
    "ResultBinding",
    "apply_graph_recall",
    "keyword_section_seeds",
    "recall_policy",
    "result_bindings",
]

#: The graph answered and this row's seed nodes did not survive its
#: lifecycle/temporal/scope filters.
GRAPH_EXCLUDED_LIFECYCLE = "graph_excluded_lifecycle"

#: The graph answered and this row's seed nodes all belong to a namespace the
#: configured one may not read.
GRAPH_EXCLUDED_SCOPE = "graph_excluded_scope"

#: The authority holds an accepted decision for this row's surface that the
#: readable projections have not published yet.
GRAPH_EXCLUDED_PENDING = "graph_excluded_pending_projection"

#: The key a keyword recall result carries its exact matched-section identities
#: under. Produced by ``recall.recall``; read here and nowhere else.
KEYWORD_SECTION_KEY = "section_entries"

#: ``config.namespace``'s own name for "no isolation". ``MemoryConfig`` treats
#: it as the legacy single-tenant mode — the knowledge root itself rather than a
#: subdirectory, and no separate shared directory beside it — so a graph read
#: under it is not narrowed either.
SHARED_NAMESPACE = "shared"


# ---------------------------------------------------------------------------
# The hard filters, resolved once from the configuration
# ---------------------------------------------------------------------------

class RecallPolicy:
    """What the configuration says a read may see, for every transport.

    Two halves, because the store contract can express one of them and not the
    other:

    * :attr:`filters` — :class:`~layered_memory_mcp.graph_store.TraversalFilters`
      the store applies *during* expansion (§7.3 step 4). The defaults already
      mean "current status, active edges, no weak relations"; what was missing
      is ``as_of``, so a claim whose validity window has not opened yet
      (``valid_from`` in the future) or has already closed was answered as
      current. The instant is resolved once per call, so every seed and every
      expansion in one answer is screened against the same clock.

    * :attr:`namespaces` — the namespaces this configuration may read, or
      ``None`` for "no isolation". This one cannot be a ``TraversalFilters``
      field: ``filters.scope`` is a single :class:`GraphScope` matched with
      :meth:`GraphScope.matches`, which is exact-equality per set dimension and
      excludes an unscoped node. The configured reading is a *union* —

        own namespace ∪ ``shared`` ∪ unscoped —

      so it is screened in :func:`apply_graph_recall` instead, over the nodes
      the traversal returned, seeds and expansions alike. See
      :func:`_in_namespace` for why "unscoped" is in that union rather than out
      of it.

    The deliberate limitation of screening rather than filtering during
    expansion: a node outside the namespace union could in principle act as a
    stepping stone to a node inside it. At the one hop this layer asks for
    (:func:`_graph_report` keeps ``run_graph_assisted_recall``'s ``max_hops=1``)
    there *are* no intermediate nodes, so nothing out of namespace can carry the
    traversal anywhere. Widening the depth here would need the union expressed
    in the store contract first.
    """

    __slots__ = ("filters", "namespaces")

    def __init__(
        self, filters: TraversalFilters, namespaces: frozenset[str] | None
    ) -> None:
        self.filters = filters
        self.namespaces = namespaces


def recall_policy(config: Any, *, now: datetime | None = None) -> RecallPolicy:
    """Resolve one read's hard filters from the configuration.

    Duck-typed on ``namespace`` alone, like every other config read in the graph
    layer: a config object without one (a test double, a future per-request
    override) resolves to the conservative *non-narrowing* answer, because
    inventing a namespace would hide material the caller never asked to hide.

    ``now`` exists so a unit test can pin the instant; the transports do not
    pass it, and the clock is read exactly once per recall.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    return RecallPolicy(TraversalFilters(as_of=moment), _configured_namespaces(config))


def _configured_namespaces(config: Any) -> frozenset[str] | None:
    """The namespaces a read may see, or ``None`` when nothing is narrowed.

    The same rule ``MemoryConfig.knowledge_dirs`` applies to the readable
    surfaces: a configured namespace searches its own material *and* the shared
    material, and the ``shared`` namespace itself is the legacy no-isolation
    mode that searches everything.
    """
    raw = getattr(config, "namespace", None)
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or name == SHARED_NAMESPACE:
        return None
    return frozenset({name, SHARED_NAMESPACE})


def _in_namespace(node: Any, namespaces: frozenset[str] | None) -> bool:
    """Whether ``node`` is readable under the configured namespace union.

    An **unscoped** node — ``scope.namespace is None`` — is treated as global
    legacy material and is readable from every namespace. That is the explicit
    choice this layer makes, and it is the opposite of
    :meth:`GraphScope.matches`, which refuses an unscoped node under a narrowed
    filter so that a graph-wide memory has to be asked for deliberately.

    The reason for the difference is what the two are used for. ``matches`` is a
    query the caller wrote; this is a *configuration* applied to every read on
    the user's behalf. Nothing in the framework stamps ``config.namespace`` onto
    the nodes it writes — a node's scope comes from the explicit decision that
    created it, and the legacy importer sets none at all — so reading "unscoped"
    as "not mine" would make every pre-namespace node invisible the moment a
    namespace was configured. Losing that material silently is the worse of the
    two failures; a node that must stay inside one namespace says so by naming
    it, which this rule then honours.
    """
    if namespaces is None:
        return True
    scope = getattr(node, "scope", None)
    namespace = getattr(scope, "namespace", None)
    return namespace is None or namespace in namespaces


# ---------------------------------------------------------------------------
# Producers of exact source identity
# ---------------------------------------------------------------------------

def keyword_section_seeds(domain: str, raw: str, keyword: str) -> list[dict[str, Any]]:
    """The exact H2 sections of ``raw`` that contain ``keyword``, with identity.

    Occurrence is assigned by the *canonical* projection rule — the ``n``-th
    appearance of an identical label in the document, counted over every bodyful
    H2, not over the matches — so a matched section's entry ID is byte-identical
    to the one the vector projection minted for the same section.

    Returns ``[{"section", "occurrence", "entry_id"}]`` in document order. A
    section whose label cannot produce a stable identity is skipped rather than
    given a guessed one; an empty list simply means this file matched on text
    the H2 projection does not cover (its preamble, a deeper heading).
    """
    from .graph_entry_identity import ProjectionIdentityError, markdown_section_entry_id
    from .injector import _bodyful_sections

    needle = keyword.lower()
    seen: dict[str, int] = {}
    seeds: list[dict[str, Any]] = []
    for section, body in _bodyful_sections(raw):
        occurrence = seen.get(section, 0)
        seen[section] = occurrence + 1
        if needle not in section.lower() and needle not in body.lower():
            continue
        try:
            entry_id = markdown_section_entry_id(domain, section, occurrence)
        except ProjectionIdentityError:
            continue
        seeds.append(
            {"section": section, "occurrence": occurrence, "entry_id": entry_id}
        )
    return seeds


class ResultBinding:
    """One producer result, with the identities the graph layer may use for it.

    Deliberately a plain object: it holds a reference to the caller's own result
    payload, which is not data to be serialized into a report.
    """

    __slots__ = ("identity", "entry_ids", "node_ids", "result")

    def __init__(
        self,
        identity: str,
        result: Mapping[str, Any],
        entry_ids: Sequence[str],
        node_ids: Sequence[str],
    ) -> None:
        self.identity = identity
        self.result = result
        self.entry_ids = list(entry_ids)
        self.node_ids = list(node_ids)

    @property
    def mapped(self) -> bool:
        return bool(self.node_ids)


def _node_ids_for(
    entry_ids: Sequence[str],
    metadata: Any,
    surface_node_ids: Mapping[str, str],
    *,
    derivable: bool,
) -> list[str]:
    """Owning node IDs for one result, in one fixed order of authority.

    1. **The managed surface registry.** A section the journal published names
       its own current owner, and that owner outranks anything derived: a
       replacement claim owns the section its predecessor used to.
    2. **The row's own declared graph identity.** The metadata the vector
       producer already writes (``graph_node_id``/``graph_node_ids``). Honoured
       exactly as the existing mapping honoured it, so an unmigrated store
       behaves as before.
    3. **The deterministic entry-ID derivation**, and only when ``derivable``:
       the producer handed over exact source identities of its own (a keyword
       result's matched ``section_entries``), so applying the same rule the
       vector producer applies to an entry ID reproduces what that row would
       carry. A vector row is *not* derivable: its metadata is the producer's
       own statement about it, and an absent graph key there means the producer
       declined to map the row — second-guessing that would turn a counted
       unmapped fallback into an invented mapping.

    Never a text match, a filename or a body similarity.
    """
    from .graph_seed_metadata import SeedIdentityError, graph_node_id_for_entry_id

    managed = [
        surface_node_ids[entry_id]
        for entry_id in entry_ids
        if entry_id in surface_node_ids
    ]
    if managed:
        return list(dict.fromkeys(managed))

    declared: list[str] = []
    if isinstance(metadata, Mapping):
        scalar = metadata.get(graph_runtime.GRAPH_SEED_ID_KEY)
        if isinstance(scalar, str) and scalar.strip():
            declared.append(scalar.strip())
        listed = metadata.get(graph_runtime.GRAPH_SEED_IDS_KEY)
        if isinstance(listed, (list, tuple)):
            declared.extend(
                value.strip()
                for value in listed
                if isinstance(value, str) and value.strip()
            )
    if declared or not derivable:
        return list(dict.fromkeys(declared))

    derived: list[str] = []
    for entry_id in entry_ids:
        try:
            derived.append(graph_node_id_for_entry_id(entry_id))
        except SeedIdentityError:
            continue
    return list(dict.fromkeys(derived))


def result_bindings(
    results: Sequence[Any],
    *,
    identity_key: str,
    surface_node_ids: Mapping[str, str],
) -> tuple[list[ResultBinding | None], int]:
    """One entry per result, in order: its binding, or ``None``.

    ``identity_key`` is the key the producer's own stable row identity lives
    under — ``id`` for a vector row, ``file`` for a keyword result. A row whose
    identity is unusable gets ``None`` and is counted: it stays in the answer,
    it is never dropped, and it is never given a fabricated identity.

    Positional alignment is the contract here. Returning only the usable
    bindings would make the graph layer unable to put the other rows back,
    which is how one malformed row would cost every other row its result.
    """
    bindings: list[ResultBinding | None] = []
    without_identity = 0
    for result in results or ():
        if not isinstance(result, Mapping):
            bindings.append(None)
            without_identity += 1
            continue
        raw_identity = result.get(identity_key)
        if not isinstance(raw_identity, str) or not raw_identity.strip():
            bindings.append(None)
            without_identity += 1
            continue
        identity = raw_identity.strip()

        sections = result.get(KEYWORD_SECTION_KEY)
        derivable = isinstance(sections, (list, tuple))
        if derivable:
            entry_ids = [
                str(item["entry_id"])
                for item in sections
                if isinstance(item, Mapping) and isinstance(item.get("entry_id"), str)
            ]
        else:
            # A vector row's own id *is* the projection entry identity, which is
            # what makes the managed-surface lookup exact — but the row's own
            # metadata stays the producer's statement about its graph identity.
            entry_ids = [identity]

        bindings.append(
            ResultBinding(
                identity,
                result,
                entry_ids,
                _node_ids_for(
                    entry_ids,
                    result.get("metadata"),
                    surface_node_ids,
                    derivable=derivable,
                ),
            )
        )
    return bindings, without_identity


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

class GraphSearchOutcome:
    """The filtered primary results, and the bounded graph block beside them."""

    __slots__ = ("results", "block")

    def __init__(self, results: list[Any], block: dict[str, Any] | None) -> None:
        self.results = results
        self.block = block

    @property
    def excluded(self) -> int:
        return 0 if self.block is None else int(self.block.get("excluded_results", 0))


def apply_graph_recall(
    config: Any,
    results: Sequence[Any],
    *,
    identity_key: str,
    store: Any | None = None,
) -> GraphSearchOutcome:
    """Apply the shared graph recall policy to one producer's results.

    The hard filters come from :func:`recall_policy` — current-time validity and
    the configured namespace union — so every transport applies the same clock
    and the same scope, to expansions as well as to the primary rows.

    Args:
        config: The configuration carrying the mode, the namespace and the
            declared paths.
        results: The producer's ranked results. Never mutated; the returned
            list holds the same objects, minus the ones the graph excluded.
        identity_key: ``"id"`` for vector rows, ``"file"`` for keyword results.
        store: An optional caller-injected graph store. It takes precedence over
            the configured path and is never closed here.

    Returns:
        A :class:`GraphSearchOutcome`. In ``legacy`` mode it is the original
        results and ``None`` — no graph operation is reached and no key is
        added.
    """
    mode = graph_runtime.GraphModeDecision.from_config(config)
    if not mode.observes_recall:
        return GraphSearchOutcome(list(results or ()), None)

    # The configured hard filters, resolved once: one clock and one namespace
    # union for every seed and every expansion in this answer.
    policy = recall_policy(config)

    # One journal open per request, for both things the authority knows about a
    # row: which node owns its surface, and whether an accepted write has
    # already replaced it.
    surface_node_ids, pending = _authority_view(config, mode)
    bindings, without_identity = result_bindings(
        results, identity_key=identity_key, surface_node_ids=surface_node_ids
    )

    seed_rows = [
        {
            "file": binding.identity,
            "score": _numeric(binding.result.get("score")),
            "metadata": {graph_runtime.GRAPH_SEED_IDS_KEY: binding.node_ids},
        }
        for binding in bindings
        if binding is not None
    ]
    report = _graph_report(config, seed_rows, mode, store, policy.filters)

    # The namespace half of the policy, applied to everything the traversal
    # returned before anything is read off it.
    report, out_of_namespace = _screen_namespaces(report, mode, policy.namespaces)

    surviving, filtered_out = _seed_verdicts(report)
    governs = mode.recall_enabled and report.status in _ANSWERED_STATUSES

    kept: list[Any] = []
    reasons: set[str] = set()
    excluded = 0
    for result, binding in zip(results or (), bindings):
        if binding is None:
            # No usable identity: the graph has nothing to say about this row,
            # so it keeps its legacy place in the answer.
            kept.append(result)
            continue
        if pending and any(entry_id in pending for entry_id in binding.entry_ids):
            reasons.add(GRAPH_EXCLUDED_PENDING)
            excluded += 1
            continue
        reason = (
            _exclusion_reason(binding, surviving, filtered_out, out_of_namespace)
            if governs and binding.mapped
            else None
        )
        if reason is not None:
            reasons.add(reason)
            excluded += 1
            continue
        kept.append(binding.result)

    block = graph_runtime.graph_recall_payload(report)
    block["excluded_results"] = excluded
    block["exclusion_reasons"] = sorted(reasons)
    if without_identity:
        block["warnings"] = sorted(
            {*block.get("warnings", ()), f"results_without_identity:{without_identity}"}
        )
    if mode.shadow_enabled:
        block["comparison"] = graph_runtime.compare_recall_results(
            seed_rows, report
        ).to_dict()
    return GraphSearchOutcome(kept, block)


#: The two statuses that mean "the graph actually answered". Anything else is an
#: outage or an unused graph, and an outage may not remove a legacy result.
_ANSWERED_STATUSES = (GRAPH_STATUS_OK, GRAPH_STATUS_PARTIAL)


def _numeric(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


#: The traversal warning that means "this seed is in the graph and your hard
#: filters removed it" — as opposed to ``unknown_seed``, which means the graph
#: has never heard of it and therefore has nothing to say about the row.
_EXCLUDED_SEED_PREFIX = "excluded_seed:"


def _seed_verdicts(report: Any) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Per-result ``(surviving node IDs, filter-excluded seed IDs)``.

    The distinction is the whole safety property of this layer: a seed the graph
    *knows* and deliberately filtered out is a stale row, while a seed the graph
    has never stored is simply outside its knowledge and must keep its legacy
    result. Collapsing the two would silently delete every unmigrated row the
    moment a graph mode was enabled.
    """
    surviving: dict[str, set[str]] = {}
    filtered_out: dict[str, set[str]] = {}
    for result in report.graph_results:
        surviving[result.file] = {node.id for node in result.graph_nodes}
        filtered_out[result.file] = {
            warning[len(_EXCLUDED_SEED_PREFIX) :]
            for warning in result.warnings
            if warning.startswith(_EXCLUDED_SEED_PREFIX)
        }
    return surviving, filtered_out


def _exclusion_reason(
    binding: ResultBinding,
    surviving: Mapping[str, set[str]],
    filtered_out: Mapping[str, set[str]],
    out_of_namespace: Mapping[str, set[str]],
) -> str | None:
    """Why this row is not current, or ``None`` when it is.

    One surviving node is enough to keep the row: a file whose matched sections
    include a current one is still a current answer.

    The two exclusion reasons are disjoint by construction — a node the store's
    own filters removed never reaches the namespace screen, because the screen
    only ever sees nodes the traversal returned — so the reported reason is the
    thing that actually removed the row rather than a guess between them.
    """
    wanted = set(binding.node_ids)
    if wanted & surviving.get(binding.identity, set()):
        return None
    if wanted & out_of_namespace.get(binding.identity, set()):
        return GRAPH_EXCLUDED_SCOPE
    if wanted & filtered_out.get(binding.identity, set()):
        return GRAPH_EXCLUDED_LIFECYCLE
    return None


#: One per-result warning label, carrying how many nodes the namespace screen
#: removed. A count, not identities: the identities are out of this namespace,
#: which is the one thing the answer may not carry.
_SCOPE_EXCLUDED_PREFIX = "scope_excluded_nodes:"


def _screen_namespaces(
    report: Any, mode: Any, namespaces: frozenset[str] | None
) -> tuple[Any, dict[str, set[str]]]:
    """Remove out-of-namespace material from a graph answer, seeds and expansions.

    Returns the screened report and ``identity -> removed node IDs``, which is
    what lets :func:`_exclusion_reason` tell "out of your namespace" apart from
    "the graph never heard of it" — the second must keep its legacy row.

    The whole per-result subgraph goes with a removed node, not just the node:
    an edge keeps only endpoints that survived, a path only survives intact, a
    provenance record belongs to a node that is still here, and a conflict is
    reported only while a readable node is still part of it. A half-edge or an
    orphan path would not render, and a sidecar naming a node the caller may not
    read would leak exactly what the screen is for.

    Screening runs in every graph-observing mode, ``shadow`` included: shadow
    may not *decide* the answer, but it may not publish another namespace's node
    IDs either. Deciding stays with the caller's ``governs`` check.
    """
    inner = getattr(report, "report", None)
    if namespaces is None or inner is None or not inner.results:
        return report, {}

    removed: dict[str, set[str]] = {}
    results = []
    for result in inner.results:
        kept_nodes = [
            node for node in result.graph_nodes if _in_namespace(node, namespaces)
        ]
        if len(kept_nodes) == len(result.graph_nodes):
            results.append(result)
            continue
        kept_ids = {node.id for node in kept_nodes}
        dropped = {node.id for node in result.graph_nodes} - kept_ids
        removed[result.file] = dropped
        results.append(
            result.model_copy(
                update={
                    "graph_nodes": kept_nodes,
                    "graph_edges": [
                        edge
                        for edge in result.graph_edges
                        if edge.source_id in kept_ids and edge.target_id in kept_ids
                    ],
                    "graph_paths": [
                        path
                        for path in result.graph_paths
                        if all(node_id in kept_ids for node_id in path.node_ids)
                    ],
                    "provenance": [
                        record
                        for record in result.provenance
                        if record.node_id in kept_ids
                    ],
                    "conflicts": [
                        conflict
                        for conflict in result.conflicts
                        if kept_ids.intersection(conflict.node_ids)
                    ],
                    "warnings": sorted(
                        {
                            *result.warnings,
                            f"{_SCOPE_EXCLUDED_PREFIX}{len(dropped)}",
                        }
                    ),
                }
            )
        )

    if not removed:
        return report, {}

    screened = inner.model_copy(
        update={
            "results": results,
            "warnings": sorted(
                {
                    *inner.warnings,
                    f"{_SCOPE_EXCLUDED_PREFIX}{sum(len(ids) for ids in removed.values())}",
                }
            ),
        }
    )
    return (
        report.model_copy(
            update={
                "report": screened,
                # Authority is the graph's to earn, and it is earned over the
                # material this configuration may actually read.
                "graph_authoritative": bool(
                    mode.recall_enabled and screened.graph_assisted
                ),
                "warnings": sorted({*report.warnings, *screened.warnings}),
            }
        ),
        removed,
    )


def _graph_report(
    config: Any,
    seed_rows: Sequence[Any],
    mode: Any,
    store: Any | None,
    filters: TraversalFilters,
):
    """Run the existing recall seam, over an injected or a request-scoped store.

    ``filters`` are the configured hard filters, applied by the store *during*
    expansion (§7.3 step 4) rather than to the answer afterwards, so a node the
    clock excluded cannot act as a stepping stone either. ``max_hops`` is left at
    the seam's own default of 1 — see :class:`RecallPolicy` for why the namespace
    screen depends on that.

    A request-scoped store is opened through
    :func:`~layered_memory_mcp.graph_runtime_assembly.graph_store_readonly_session`
    for the same reason :func:`_authority_view` opens the journal through its
    inspection entrance: a read may consult a graph, and may not be the thing
    that establishes one. A declared-but-absent database is therefore reported
    as ``graph_store_not_present`` in the block's warnings and the legacy answer
    stands — where the committer's entrance would have created the file and its
    schema, which is how a search on a never-published system used to leave a
    graph behind.

    Never raises: recall is the hot read path, so a caller-contract error in the
    graph seam is reported as an outage and the legacy answer survives. Only the
    exception *class* is carried — a store diagnostic may name a database file.
    """
    from .graph_recall import GRAPH_STATUS_UNAVAILABLE

    def _run(handle: Any) -> Any:
        try:
            return graph_runtime.run_graph_assisted_recall(
                seed_rows, handle, mode=mode, filters=filters
            )
        except Exception as exc:  # noqa: BLE001 — an outage, not a tool failure
            return graph_runtime.GraphRecallRuntimeReport(
                mode=mode.mode,
                status=GRAPH_STATUS_UNAVAILABLE,
                results=[dict(row) for row in seed_rows],
                warnings=[f"graph_runtime_failed:{type(exc).__name__}"],
            )

    if store is not None:
        # Caller-owned: used, reported, left open.
        return _run(store)

    with graph_runtime_assembly.graph_store_readonly_session(
        config, require=True, mode=mode
    ) as assembly:
        report = _run(assembly.store)
        assembly_report = assembly.report

    if assembly_report.status == graph_runtime_assembly.GRAPH_STORE_OPENED:
        return report
    return report.model_copy(
        update={
            "warnings": sorted(
                {*report.warnings, assembly_report.status, *assembly_report.errors}
            )
        }
    )


def _authority_view(config: Any, mode: Any) -> tuple[dict[str, str], set[str]]:
    """``(entry_id -> owning node_id, entry_ids with an unpublished write)``.

    Both halves come from one journal open: the managed surface registry, and
    the surfaces an accepted-but-unpublished decision has already replaced. The
    stale half only applies where the graph may decide the answer — in
    ``shadow`` the graph decides nothing, so the legacy answer stands.

    An unconfigured or unreadable journal answers ``({}, set())``: the legacy
    derivation then applies to every row and nothing is excluded, which is
    exactly the pre-journal behaviour. An *absent* journal now reaches that
    same answer without being created: this is a read, so it opens the journal
    through
    :func:`~layered_memory_mcp.graph_runtime_assembly.journal_readonly_session`
    — a recall must not establish the authority it consults, which is how a
    search on a legacy system used to leave a journal behind.
    """
    from .graph_application import stale_surface_entry_ids

    try:
        with graph_runtime_assembly.journal_readonly_session(
            config, require=True
        ) as assembly:
            journal = assembly.journal
            if journal is None:
                return {}, set()
            surfaces = journal.surface_node_ids()
            stale = (
                stale_surface_entry_ids(journal) if mode.recall_enabled else set()
            )
            return surfaces, stale
    except Exception:  # noqa: BLE001 — a registry outage may not break recall
        return {}, set()

"""Measured shadow evaluation of graph-assisted recall (ARCHITECTURE_v2.md §12 J).

WHAT THIS MODULE IS
-------------------
Batch J: the instrument that runs graph-assisted recall *beside* the existing
legacy answer on an explicit fixture corpus and writes down what happened —
result-identity differences, stale-fact exposure, provenance coverage, fallback
rate and latency. It produces the acceptance *inputs* for a later cutover
decision, from measured repository data rather than a vendor benchmark.

It is evaluation infrastructure. Nothing in a production or default path imports
it, and switching it on changes no answer anybody receives.

WHAT IT IS NOT
--------------
- NOT a second recall implementation. The graph side is
  :func:`~layered_memory_mcp.graph_recall.expand_recall_with_graph` and the
  comparison is
  :func:`~layered_memory_mcp.graph_runtime.compare_recall_results`; this module
  orchestrates and counts, and computes no ranking, score or traversal of its
  own.
- NOT a judge. There is no verdict, no threshold, no "passed", no "preferred"
  and no approval anywhere in its output. A cutover is a review decision taken
  with these numbers in hand, not something a measurement helper may make.
- NOT a discovery mechanism. A case's result-to-node mapping and its expected-ID
  ground truth arrive as explicit arguments. Nothing is inferred from a filename,
  a body, a heading or a hash, and no file, environment variable, configuration
  or memory path is read.
- NOT a writer. Only the four read operations
  (:data:`~layered_memory_mcp.graph_recall.GRAPH_RECALL_OPERATIONS`) the recall
  seam needs are ever reached, so "did the evaluation mutate the graph?" is
  answerable by inspection.
- NOT a sampler of live data. A store arrives as an argument or there is no
  graph; no backend is constructed, probed or auto-discovered here.

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Ground truth is supplied, never guessed.** Stale exposure is scored against
   :attr:`ShadowEvaluationCase.expected_current_node_ids` and provenance against
   :attr:`ShadowEvaluationCase.expected_provenance_node_ids`. A case that supplies
   neither is reported as *not measured* — with no denominator invented and no
   missing provenance counted as covered (§10.3).
2. **Legacy survival is measured, not assumed.** Every case records the
   identity/order/count comparison plus
   :attr:`ShadowCaseMeasurement.legacy_payloads_preserved`, computed against the
   case's own pristine copy of the payloads.
3. **Determinism apart from the clock.** Every collection is sorted by stable
   identifier, and :meth:`ShadowCaseMeasurement.structural_to_json` /
   :meth:`ShadowEvaluationReport.structural_to_json` exclude the measured
   latency, so repeating a corpus is a valid byte comparison.
4. **Bounded parameters, refused rather than clamped.** ``max_hops`` and
   ``top_n`` are validated against the recall seam's own limits at construction;
   an out-of-range value raises instead of being silently narrowed.
5. **Degradation is a status, never an exception.** A missing, unhealthy or
   raising graph becomes a fallback with a bounded reason label.

PRIVACY
-------
Output carries case names, result identities, graph node IDs, counts, statuses
and the bounded warning labels the graph seams already emit — never a memory
body, a source path, a store diagnostic string or an exception message (§15).
Provenance coverage is scored over evidence/episode/derivation *node IDs* only,
so a source file named in a :class:`~layered_memory_mcp.graph_models.SourceRef`
cannot reach a measurement.
"""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from pydantic import Field

from .graph_models import GraphModel, clean_identifier
from .graph_recall import (
    GRAPH_STATUS_UNAVAILABLE,
    GRAPH_STATUS_UNUSED,
    MAX_GRAPH_RECALL_HOPS,
    GraphRecallReport,
    GraphRecallResult,
    RecallSeed,
    expand_recall_with_graph,
)
from .graph_runtime import RecallComparison, compare_recall_results
from .graph_store import GraphStore, ProvenanceRecord, TraversalFilters

#: The case was graph-assisted: at least one result carries graph material.
FALLBACK_NONE = ""

#: No usable graph at all — absent, unhealthy, or every traversal failed.
FALLBACK_GRAPH_UNAVAILABLE = "graph_unavailable"

#: A usable graph nothing asked about: no result carried a seed mapping.
FALLBACK_GRAPH_UNUSED = "graph_unused"

#: The graph answered and contributed nothing — every mapped seed was filtered
#: out or unknown. Deliberately distinct from the two above: "asked and got
#: nothing" is a different operational fact from "could not ask".
FALLBACK_NO_GRAPH_MATERIAL = "no_graph_material"

#: Emitted when a case supplied no expected-current-ID set. Stale exposure is
#: then unmeasurable, and an unmeasured metric must not read as a clean one.
NOT_MEASURED_STALE = "stale_exposure_not_measured"

#: Emitted when a case supplied no expected-provenance-ID set.
NOT_MEASURED_PROVENANCE = "provenance_coverage_not_measured"

#: The fraction :func:`_percentile` reports beside min/mean/max. One tail
#: number, chosen because a recall seam's cost shows up in its slow cases.
LATENCY_PERCENTILE = 0.95

#: The backend names a measurement may repeat verbatim: the labels this
#: project's own store implementations report. Spelled out rather than imported
#: because the store modules sit outside this module's import boundary, and a
#: label is cheaper to keep in step than a dependency is to justify.
SAFE_BACKEND_LABELS = frozenset({"in_memory_fixture", "sqlite_embedded"})

#: What any other backend name is reported as. ``GraphHealth.backend`` is a
#: store-supplied string, so an unknown backend may hand back a connection
#: string or an absolute on-disk location; a measurement names backends as
#: labels and may never carry a host path (§15), so an unrecognized one is
#: bounded to this instead of passed through.
SAFE_BACKEND_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Inputs: an explicit case and explicit bounded parameters
# ---------------------------------------------------------------------------

def _clean_ids(values: Iterable[str], label: str) -> tuple[str, ...]:
    """Clean, deduplicate and sort an explicit ID set."""
    return tuple(sorted({clean_identifier(value, label) for value in values}))


def _optional_ids(
    values: Sequence[str] | None, label: str
) -> tuple[str, ...] | None:
    """``None`` stays ``None``: "no ground truth" is not "an empty expectation"."""
    return None if values is None else _clean_ids(values, label)


@dataclass(frozen=True)
class ShadowEvaluationCase:
    """One evaluation case: a legacy answer, a graph, and the fixture truth.

    Frozen and normalized at construction, so a case is a stable fixture that
    two runs of the evaluator see identically. ``legacy_results`` is deep-copied
    on the way in, which is what makes
    :attr:`ShadowCaseMeasurement.legacy_payloads_preserved` a real check rather
    than a comparison against a list the evaluator might have been handed back.

    Args:
        case_id: The case's stable name. It appears in output, so it is a label,
            not a path or a memory excerpt.
        legacy_results: The ranked legacy payloads, in ranking order. Each needs
            the ``file`` identity the existing recall path already emits.
        store: The graph to expand through, or ``None``. ``None`` is a supported
            case — it measures the no-graph fallback — not an error.
        seed_node_ids_by_result: The *explicit* result-identity → graph-node-IDs
            mapping. The only seed-to-graph mapping used; nothing is inferred.
        expected_current_node_ids: The node IDs this fixture considers current
            and in scope. Anything else that comes back is counted as stale
            exposure. ``None`` means "no ground truth supplied", which is
            reported as unmeasured rather than scored as clean.
        expected_provenance_node_ids: The evidence/episode/derivation node IDs
            graph assistance is expected to resolve. The denominator of the
            coverage metric. ``None`` is again "not measured".
    """

    case_id: str
    legacy_results: Sequence[Mapping[str, Any]] = ()
    store: GraphStore | None = None
    seed_node_ids_by_result: Mapping[str, Sequence[str]] = field(
        default_factory=dict
    )
    expected_current_node_ids: Sequence[str] | None = None
    expected_provenance_node_ids: Sequence[str] | None = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "case_id", clean_identifier(self.case_id, "case_id"))

        results: list[dict[str, Any]] = []
        for index, result in enumerate(self.legacy_results):
            if not isinstance(result, Mapping):
                raise ValueError(
                    f"legacy_results[{index}] must be a mapping, got "
                    f"{type(result).__name__}"
                )
            results.append(deepcopy(dict(result)))
        set_(self, "legacy_results", tuple(results))

        set_(
            self,
            "seed_node_ids_by_result",
            {
                clean_identifier(key, "seed mapping key"): _clean_ids(
                    ids, "seed node id"
                )
                for key, ids in self.seed_node_ids_by_result.items()
            },
        )
        set_(
            self,
            "expected_current_node_ids",
            _optional_ids(self.expected_current_node_ids, "expected current node id"),
        )
        set_(
            self,
            "expected_provenance_node_ids",
            _optional_ids(
                self.expected_provenance_node_ids, "expected provenance node id"
            ),
        )


@dataclass(frozen=True)
class ShadowEvaluationSettings:
    """The bounded recall parameters one evaluation run uses.

    Validated against the recall seam's own limits *here*, at construction, so an
    out-of-range request is refused once and loudly instead of being clamped into
    a measurement that silently answers a different question than it was asked.
    """

    max_hops: int = 1
    top_n: int | None = None
    filters: TraversalFilters | None = None
    include_provenance: bool = True
    include_conflicts: bool = True

    def __post_init__(self) -> None:
        if self.max_hops < 0 or self.max_hops > MAX_GRAPH_RECALL_HOPS:
            raise ValueError(
                f"max_hops must be between 0 and {MAX_GRAPH_RECALL_HOPS} "
                f"(the §7.3 step 3 budget), got {self.max_hops}"
            )
        if self.top_n is not None and self.top_n < 1:
            raise ValueError(
                f"top_n must be at least 1 when given, got {self.top_n}"
            )


#: One hop, default filters, provenance and conflicts on — the conservative
#: reading of §7.3 the recall seam itself defaults to.
DEFAULT_SHADOW_SETTINGS = ShadowEvaluationSettings()


# ---------------------------------------------------------------------------
# Outputs: measurements, never judgements
# ---------------------------------------------------------------------------

class LatencySummary(GraphModel):
    """Aggregate wall-clock cost of the measured graph-assisted calls.

    Measured with :func:`time.perf_counter`, a monotonic clock, so a system
    clock adjustment cannot produce a negative duration. ``p95_ms`` is the
    nearest-rank percentile of the sorted samples, which needs no interpolation
    and is therefore reproducible for a given sample set.
    """

    count: int = Field(default=0, ge=0)
    min_ms: float = Field(default=0.0, ge=0.0)
    mean_ms: float = Field(default=0.0, ge=0.0)
    p95_ms: float = Field(default=0.0, ge=0.0)
    max_ms: float = Field(default=0.0, ge=0.0)


class ShadowCaseMeasurement(GraphModel):
    """What one case measured. Every field is an observation, not a judgement.

    The two coverage metrics each carry their own ``*_measured`` flag: a case
    with no ground truth reports zero *and* says so, because "nothing was stale"
    and "nobody checked" must not read the same.
    """

    case_id: str

    #: The graph seam's own status — ``graph_ok`` / ``graph_partial`` /
    #: ``graph_unavailable`` / ``graph_unused``.
    status: str
    backend: str = ""
    max_hops: int = Field(default=0, ge=0)

    # -- result identity (does the legacy answer survive?) -------------------
    legacy_result_count: int = Field(default=0, ge=0)
    graph_result_count: int = Field(default=0, ge=0)
    legacy_payloads_preserved: bool = True
    comparison: RecallComparison = Field(default_factory=RecallComparison)

    # -- what the graph added ------------------------------------------------
    graph_node_ids: list[str] = Field(default_factory=list)
    graph_node_count: int = Field(default=0, ge=0)
    mapped_results: int = Field(default=0, ge=0)
    unmapped_results: int = Field(default=0, ge=0)
    truncated: bool = False

    # -- stale exposure ------------------------------------------------------
    stale_exposure_measured: bool = False
    expected_current_node_count: int = Field(default=0, ge=0)
    stale_node_ids: list[str] = Field(default_factory=list)
    stale_node_count: int = Field(default=0, ge=0)

    # -- evidence / provenance coverage --------------------------------------
    provenance_coverage_measured: bool = False
    provenance_expected: int = Field(default=0, ge=0)
    provenance_covered: int = Field(default=0, ge=0)
    provenance_missing_ids: list[str] = Field(default_factory=list)
    provenance_coverage_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    provenance_records: int = Field(default=0, ge=0)

    # -- fallback ------------------------------------------------------------
    fallback: bool = False
    fallback_reason: str = FALLBACK_NONE

    # -- cost ----------------------------------------------------------------
    latency_ms: float = Field(default=0.0, ge=0.0)

    warnings: list[str] = Field(default_factory=list)

    def structural_to_json(self) -> str:
        """The deterministic part of this measurement, as sorted JSON.

        Latency is excluded on purpose: it is a genuine measurement and it is
        genuinely not reproducible, so folding it into the determinism check
        would make that check meaningless.
        """
        return json.dumps(
            self.model_dump(mode="json", exclude={"latency_ms"}),
            sort_keys=True,
            ensure_ascii=False,
        )


class ShadowEvaluationReport(GraphModel):
    """The corpus-level measurement: counts, rates, and the per-case detail.

    Deliberately verdict-free. :meth:`acceptance_inputs` names the numbers a
    cutover review needs; choosing thresholds for them, and deciding whether
    they are met, stays with the review.
    """

    case_count: int = Field(default=0, ge=0)
    status_counts: dict[str, int] = Field(default_factory=dict)
    cases: list[ShadowCaseMeasurement] = Field(default_factory=list)

    # -- result identity -----------------------------------------------------
    legacy_result_count: int = Field(default=0, ge=0)
    graph_assisted_results: int = Field(default=0, ge=0)
    cases_with_identical_results: int = Field(default=0, ge=0)
    cases_with_preserved_payloads: int = Field(default=0, ge=0)
    dropped_identities: int = Field(default=0, ge=0)
    added_identities: int = Field(default=0, ge=0)
    cases_with_order_change: int = Field(default=0, ge=0)

    # -- graph material ------------------------------------------------------
    graph_node_count: int = Field(default=0, ge=0)
    unmapped_results: int = Field(default=0, ge=0)
    truncated_cases: int = Field(default=0, ge=0)

    # -- stale exposure ------------------------------------------------------
    stale_measured_cases: int = Field(default=0, ge=0)
    stale_node_count: int = Field(default=0, ge=0)
    cases_with_stale_exposure: int = Field(default=0, ge=0)

    # -- provenance coverage -------------------------------------------------
    provenance_measured_cases: int = Field(default=0, ge=0)
    provenance_expected: int = Field(default=0, ge=0)
    provenance_covered: int = Field(default=0, ge=0)
    provenance_coverage_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    # -- fallback ------------------------------------------------------------
    fallback_cases: int = Field(default=0, ge=0)
    fallback_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    fallback_reasons: dict[str, int] = Field(default_factory=dict)

    # -- cost ----------------------------------------------------------------
    latency: LatencySummary = Field(default_factory=LatencySummary)

    warnings: list[str] = Field(default_factory=list)

    def structural_to_json(self) -> str:
        """The deterministic part of the whole report, as sorted JSON."""
        return json.dumps(
            self.model_dump(
                mode="json",
                exclude={
                    "latency": True,
                    "cases": {"__all__": {"latency_ms"}},
                },
            ),
            sort_keys=True,
            ensure_ascii=False,
        )

    def acceptance_inputs(self) -> dict[str, Any]:
        """The bounded numbers a cutover review reads. No verdict is implied.

        Returning the inputs rather than a decision is the point of the batch:
        thresholds are set from measured repository data in review, and this
        helper must not be able to pre-empt that by scoring itself.
        """
        return {
            "case_count": self.case_count,
            "status_counts": dict(self.status_counts),
            "cases_with_identical_results": self.cases_with_identical_results,
            "cases_with_preserved_payloads": self.cases_with_preserved_payloads,
            "dropped_identities": self.dropped_identities,
            "added_identities": self.added_identities,
            "cases_with_order_change": self.cases_with_order_change,
            "graph_assisted_results": self.graph_assisted_results,
            "graph_node_count": self.graph_node_count,
            "stale_measured_cases": self.stale_measured_cases,
            "stale_node_count": self.stale_node_count,
            "cases_with_stale_exposure": self.cases_with_stale_exposure,
            "provenance_measured_cases": self.provenance_measured_cases,
            "provenance_expected": self.provenance_expected,
            "provenance_covered": self.provenance_covered,
            "provenance_coverage_rate": self.provenance_coverage_rate,
            "fallback_cases": self.fallback_cases,
            "fallback_rate": self.fallback_rate,
            "fallback_reasons": dict(self.fallback_reasons),
            "latency": self.latency.to_dict(),
        }


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------

def evaluate_shadow_case(
    case: ShadowEvaluationCase,
    settings: ShadowEvaluationSettings = DEFAULT_SHADOW_SETTINGS,
) -> ShadowCaseMeasurement:
    """Run graph-assisted recall beside one legacy answer and measure both.

    The legacy answer is the case's own payload list; the graph-assisted answer
    is whatever :func:`~layered_memory_mcp.graph_recall.expand_recall_with_graph`
    returns for the same seeds under the case's explicit mapping. Nothing is
    re-ranked, re-scored or decided here.

    Args:
        case: The explicit fixture case, including its injected store (or
            ``None``) and its ground-truth ID sets.
        settings: The bounded recall parameters. Already validated.

    Returns:
        A :class:`ShadowCaseMeasurement`.

    Raises:
        ValueError: Only for a caller-contract mistake the recall seam defines —
            a result without a ``file`` identity, two results sharing one
            identity, a mapping key naming no result, or a store missing a read
            operation. A *graph* failure is never raised: it is measured as a
            fallback.
    """
    legacy_results = list(case.legacy_results)
    seeds = [RecallSeed.from_result(result) for result in legacy_results]

    started = time.perf_counter()
    report = expand_recall_with_graph(
        seeds,
        case.store,
        seed_node_ids_by_result=case.seed_node_ids_by_result,
        max_hops=settings.max_hops,
        filters=settings.filters,
        top_n=settings.top_n,
        include_provenance=settings.include_provenance,
        include_conflicts=settings.include_conflicts,
    )
    latency_ms = max(0.0, (time.perf_counter() - started) * 1000.0)

    comparison = compare_recall_results(legacy_results, report)
    node_ids = _returned_node_ids(report)
    graph_results = [r for r in report.results if r.graph_assisted]

    warnings: set[str] = set(report.warnings)
    stale = _measure_stale(case, node_ids, warnings)
    provenance = _measure_provenance(case, report, warnings)
    fallback_reason = _fallback_reason(report, graph_results)

    return ShadowCaseMeasurement(
        case_id=case.case_id,
        status=report.status,
        backend=_safe_backend(report.backend),
        max_hops=report.max_hops,
        legacy_result_count=len(legacy_results),
        graph_result_count=len(graph_results),
        legacy_payloads_preserved=report.preserved_results == legacy_results,
        comparison=comparison,
        graph_node_ids=node_ids,
        graph_node_count=len(node_ids),
        mapped_results=sum(1 for r in report.results if r.graph_node_ids),
        unmapped_results=sum(1 for r in report.results if not r.graph_node_ids),
        truncated=report.truncated,
        fallback=fallback_reason != FALLBACK_NONE,
        fallback_reason=fallback_reason,
        latency_ms=latency_ms,
        warnings=sorted(warnings),
        **stale,
        **provenance,
    )


def evaluate_shadow_corpus(
    cases: Sequence[ShadowEvaluationCase],
    settings: ShadowEvaluationSettings = DEFAULT_SHADOW_SETTINGS,
) -> ShadowEvaluationReport:
    """Evaluate a corpus of cases and aggregate the measurements.

    Cases are evaluated in the order given and reported in that order: the
    corpus is an explicit fixture list, and re-sorting it would make a report
    harder to read against the fixture that produced it. Every *aggregate* is
    order-independent.

    An empty corpus is a valid answer, not an error: zero cases, zero rates and
    an empty latency summary — nothing is divided by zero and nothing is
    extrapolated from no data.
    """
    measurements = [evaluate_shadow_case(case, settings) for case in cases]

    provenance_expected = sum(m.provenance_expected for m in measurements)
    provenance_covered = sum(m.provenance_covered for m in measurements)
    fallback_cases = sum(1 for m in measurements if m.fallback)

    status_counts: dict[str, int] = {}
    for measurement in measurements:
        status_counts[measurement.status] = (
            status_counts.get(measurement.status, 0) + 1
        )

    fallback_reasons: dict[str, int] = {}
    for measurement in measurements:
        if not measurement.fallback:
            continue
        reason = measurement.fallback_reason
        fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1

    return ShadowEvaluationReport(
        case_count=len(measurements),
        status_counts=dict(sorted(status_counts.items())),
        cases=measurements,
        legacy_result_count=sum(m.legacy_result_count for m in measurements),
        graph_assisted_results=sum(m.graph_result_count for m in measurements),
        cases_with_identical_results=sum(
            1 for m in measurements if m.comparison.identical
        ),
        cases_with_preserved_payloads=sum(
            1 for m in measurements if m.legacy_payloads_preserved
        ),
        dropped_identities=sum(
            len(m.comparison.dropped_identities) for m in measurements
        ),
        added_identities=sum(
            len(m.comparison.added_identities) for m in measurements
        ),
        cases_with_order_change=sum(
            1 for m in measurements if not m.comparison.order_preserved
        ),
        graph_node_count=sum(m.graph_node_count for m in measurements),
        unmapped_results=sum(m.unmapped_results for m in measurements),
        truncated_cases=sum(1 for m in measurements if m.truncated),
        stale_measured_cases=sum(
            1 for m in measurements if m.stale_exposure_measured
        ),
        stale_node_count=sum(m.stale_node_count for m in measurements),
        cases_with_stale_exposure=sum(
            1 for m in measurements if m.stale_node_count
        ),
        provenance_measured_cases=sum(
            1 for m in measurements if m.provenance_coverage_measured
        ),
        provenance_expected=provenance_expected,
        provenance_covered=provenance_covered,
        provenance_coverage_rate=_rate(provenance_covered, provenance_expected),
        fallback_cases=fallback_cases,
        fallback_rate=_rate(fallback_cases, len(measurements)),
        fallback_reasons=dict(sorted(fallback_reasons.items())),
        latency=_latency_summary([m.latency_ms for m in measurements]),
        warnings=sorted({warning for m in measurements for warning in m.warnings}),
    )


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _safe_backend(backend: str) -> str:
    """Bound a store-reported backend name to a known label.

    ``""`` stays ``""``: "no graph was consulted" is a different fact from "a
    graph answered under a name this module does not recognize", and an empty
    label cannot leak anything. Everything else is either one of
    :data:`SAFE_BACKEND_LABELS` — which an acceptance review reads, so those
    stay stable and legible — or :data:`SAFE_BACKEND_UNKNOWN`. Nothing is
    sanitized in place: a path is replaced, not stripped of its separators,
    because a stripped path is still a host location.
    """
    if not backend:
        return ""
    return backend if backend in SAFE_BACKEND_LABELS else SAFE_BACKEND_UNKNOWN


def _returned_node_ids(report: GraphRecallReport) -> list[str]:
    """Every distinct graph node the answer actually exposed, sorted.

    The *returned* nodes, not the requested ones: a seed the hard filters
    excluded was never exposed, so counting it as exposure would overstate the
    risk the stale-exposure metric exists to measure.
    """
    return sorted(
        {node.id for result in report.results for node in result.graph_nodes}
    )


def _measure_stale(
    case: ShadowEvaluationCase, node_ids: Sequence[str], warnings: set[str]
) -> dict[str, Any]:
    """Score returned nodes against the fixture's expected-current set."""
    expected = case.expected_current_node_ids
    if expected is None:
        warnings.add(NOT_MEASURED_STALE)
        return {
            "stale_exposure_measured": False,
            "expected_current_node_count": 0,
            "stale_node_ids": [],
            "stale_node_count": 0,
        }
    stale = sorted(set(node_ids) - set(expected))
    return {
        "stale_exposure_measured": True,
        "expected_current_node_count": len(expected),
        "stale_node_ids": stale,
        "stale_node_count": len(stale),
    }


def _measure_provenance(
    case: ShadowEvaluationCase, report: GraphRecallReport, warnings: set[str]
) -> dict[str, Any]:
    """Score resolved lineage against the fixture's expected-provenance set.

    An expected ID counts as covered only when a returned provenance record
    *references* it and no returned record *flagged* it. A store names an
    unresolvable, mis-kinded or withdrawn reference in the record's own
    ``warnings`` (§10.2) in one of the six shapes :func:`_flagged_ids` reads —
    so a reference the graph could not stand behind is excluded rather than
    counted, which is what stops missing provenance from reading as covered.
    """
    records = [
        record for result in report.results for record in result.provenance
    ]
    expected = case.expected_provenance_node_ids
    if expected is None:
        warnings.add(NOT_MEASURED_PROVENANCE)
        return {
            "provenance_coverage_measured": False,
            "provenance_expected": 0,
            "provenance_covered": 0,
            "provenance_missing_ids": [],
            "provenance_coverage_rate": 0.0,
            "provenance_records": len(records),
        }

    referenced: set[str] = set()
    flagged: set[str] = set()
    for record in records:
        referenced |= _referenced_ids(record)
        flagged |= _flagged_ids(record)

    resolved = referenced - flagged
    covered = sorted(set(expected) & resolved)
    missing = sorted(set(expected) - resolved)
    return {
        "provenance_coverage_measured": True,
        "provenance_expected": len(expected),
        "provenance_covered": len(covered),
        "provenance_missing_ids": missing,
        "provenance_coverage_rate": _rate(len(covered), len(expected)),
        "provenance_records": len(records),
    }


def _referenced_ids(record: ProvenanceRecord) -> set[str]:
    """The lineage *node IDs* one record names.

    Deliberately only the ID lists: a :class:`SourceRef` may also name a source
    file and a section, and those are exactly what an evaluation output may not
    carry (§15). ``get_provenance`` already folds a ref's episode/evidence IDs
    into these lists, so nothing measurable is lost.
    """
    return {
        *record.evidence_ids,
        *record.episode_ids,
        *record.derived_from_ids,
    }


#: The provenance-warning labels ``get_provenance`` emits (§10.2), mapped to
#: whether a trailing ``:<kind>`` field follows the node ID. These six shapes
#: are the whole vocabulary :func:`_flagged_ids` will read.
_FLAG_WARNING_LABELS: Mapping[str, bool] = {
    "unresolved_evidence": False,
    "unresolved_episode": False,
    "unresolved_derived_from": False,
    "unexpected_evidence_kind": True,
    "unexpected_episode_kind": True,
    "withdrawn_evidence": False,
}


def _flagged_ids(record: ProvenanceRecord) -> set[str]:
    """The node IDs a record's warnings named as unresolvable, mis-kinded or
    withdrawn.

    Parsed against the six exact shapes above rather than by splitting on every
    colon, because two of them end in a node *kind*:
    ``unexpected_evidence_kind:<id>:<kind>``. A kind label is also a legal
    identifier — ``episode`` is both — so reading the trailing field as an ID
    would flag a node the graph resolved perfectly well and deduct it from
    provenance coverage, understating the metric for reasons nothing in the
    output would explain.

    A label this module does not know contributes nothing, and neither does a
    known label whose shape does not fit: a warning that cannot be parsed is
    not evidence about any ID, and guessing one would put a fabricated node ID
    into a measurement.
    """
    flagged: set[str] = set()
    for warning in record.warnings:
        parts = warning.split(":")
        label = parts[0] if parts else ""
        expects_kind = _FLAG_WARNING_LABELS.get(label)
        if expects_kind is None:
            continue
        expected_parts = 3 if expects_kind else 2
        if len(parts) != expected_parts or not parts[1] or (
            expects_kind and not parts[2]
        ):
            continue
        flagged.add(parts[1])
    return flagged


def _fallback_reason(
    report: GraphRecallReport, graph_results: Sequence[GraphRecallResult]
) -> str:
    """Why this case fell back to the legacy answer, or :data:`FALLBACK_NONE`.

    The three reasons are kept apart because they call for different actions: a
    broken graph is an outage, an unused graph is a missing mapping, and a graph
    that answered with nothing is a filter or a fixture question.
    """
    if report.status == GRAPH_STATUS_UNAVAILABLE:
        return FALLBACK_GRAPH_UNAVAILABLE
    if report.status == GRAPH_STATUS_UNUSED:
        return FALLBACK_GRAPH_UNUSED
    if not graph_results:
        return FALLBACK_NO_GRAPH_MATERIAL
    return FALLBACK_NONE


def _rate(numerator: int, denominator: int) -> float:
    """A ratio, or ``0.0`` when there is nothing to divide by.

    Zero rather than one: an empty denominator means nothing was measured, and
    the ``*_measured`` flags beside every rate are what say so.
    """
    return numerator / denominator if denominator else 0.0


def _latency_summary(samples: Sequence[float]) -> LatencySummary:
    """Aggregate measured durations. No sample set is extrapolated from."""
    if not samples:
        return LatencySummary()
    ordered = sorted(max(0.0, sample) for sample in samples)
    return LatencySummary(
        count=len(ordered),
        min_ms=ordered[0],
        mean_ms=sum(ordered) / len(ordered),
        p95_ms=_percentile(ordered, LATENCY_PERCENTILE),
        max_ms=ordered[-1],
    )


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of an already-sorted, non-empty sample list."""
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


__all__ = [
    "DEFAULT_SHADOW_SETTINGS",
    "FALLBACK_GRAPH_UNAVAILABLE",
    "FALLBACK_GRAPH_UNUSED",
    "FALLBACK_NONE",
    "FALLBACK_NO_GRAPH_MATERIAL",
    "LATENCY_PERCENTILE",
    "NOT_MEASURED_PROVENANCE",
    "NOT_MEASURED_STALE",
    "SAFE_BACKEND_LABELS",
    "SAFE_BACKEND_UNKNOWN",
    "LatencySummary",
    "ShadowCaseMeasurement",
    "ShadowEvaluationCase",
    "ShadowEvaluationReport",
    "ShadowEvaluationSettings",
    "evaluate_shadow_case",
    "evaluate_shadow_corpus",
]

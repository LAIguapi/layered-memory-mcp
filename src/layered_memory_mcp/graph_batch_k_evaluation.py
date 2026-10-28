"""Separated evaluation and persistent recall parity (ARCHITECTURE_v2.md §12 K).

WHAT THIS MODULE IS
-------------------
Batch K's two instruments, and nothing else:

* **K-A — separated shadow evaluation.** :func:`evaluate_separated_corpus` runs
  the Batch J evaluator over a corpus whose cases each carry an *explicitly
  declared* class — :data:`CASE_CLASS_NORMAL` or :data:`CASE_CLASS_FALLBACK` —
  and aggregates the two classes apart. The reason is arithmetic: averaging a
  healthy graph-assisted case together with an absent, raising or unmapped one
  yields a "provenance coverage" figure that is really a fallback rate wearing
  a retrieval-quality label. A fallback rate is an operational reliability
  input; provenance coverage is only meaningful over normal-path cases that
  supplied a denominator.
* **K-B — persistent recall parity.** :func:`run_persistent_parity_check`
  writes an explicit fixture (nodes/edges *or* an event stream) into a
  :class:`~layered_memory_mcp.graph_sqlite_store.SQLiteGraphStore` at a
  caller-supplied database target, **closes** that store, opens a new one on
  the same database, and runs the existing recall seam
  (:func:`~layered_memory_mcp.graph_runtime.run_graph_assisted_recall`) against
  it under an explicit recall-enabled
  :class:`~layered_memory_mcp.graph_runtime.GraphModeDecision`. The answer is
  compared with the one the Batch C reference store gives for the same inputs,
  on bounded semantic observations only.

PUBLIC API
----------
K-A: :data:`CASE_CLASS_NORMAL`, :data:`CASE_CLASS_FALLBACK`,
:data:`CASE_CLASSES`, :data:`CLASSIFICATION_DISAGREEMENT`,
:class:`ClassifiedShadowCase`, :class:`ClassifiedCaseMeasurement`,
:class:`ClassAggregate`, :class:`SeparatedEvaluationReport`,
:func:`evaluate_separated_corpus`.

K-B: :data:`FIXTURE_SOURCE_MATERIAL`, :data:`FIXTURE_SOURCE_EVENTS`,
:data:`PARITY_COMPARED_FIELDS`, :data:`PARITY_UNCOMPARED_FIELDS`,
:class:`GraphParityObservation`, :class:`PersistentParityReport`,
:func:`run_persistent_parity_check`.

WHAT IT IS NOT
--------------
- NOT a third evaluator. Every metric comes from
  :func:`~layered_memory_mcp.graph_shadow_evaluation.evaluate_shadow_corpus`,
  every recall from the Batch F/H seams, and every backend label from the
  Batch J sanitizer. This module groups, re-reports and compares; it computes
  no ranking, coverage rule or traversal of its own.
- NOT a judge. There is no verdict, threshold, approval, readiness claim or
  cutover decision in any output. K-C owns the gate, and a measurement helper
  that could score itself would pre-empt the review that is supposed to set
  those numbers from measured repository data.
- NOT a classifier. A case's class arrives as an argument. Nothing is inferred
  from a case id, a store type, a measurement or a filename, and a label
  outside :data:`CASE_CLASSES` is refused rather than guessed at.
- NOT a discovery mechanism. ``database`` is required, explicit and
  keyword-only; no directory is created, no path is derived from a home
  directory, a config, an environment variable or a default, and no
  configuration, MCP state or memory file is read.
- NOT a writer *during evaluation*. The only writes anywhere are the ones that
  build the caller's own temporary fixture database before recall begins; the
  recall seam itself reaches only the four read operations
  (:data:`~layered_memory_mcp.graph_recall.GRAPH_RECALL_OPERATIONS`), and both
  graphs are snapshotted before and after so "did the comparison mutate
  anything?" is answered as data.

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Classification is declared, and ambiguity is refused.** A class label must
   be exactly one of the two; a case id may appear once, in one class. A
   declaration that *disagrees* with the measured fallback state is reported —
   :attr:`SeparatedEvaluationReport.classification_disagreements` plus a
   bounded ``classification_disagreement:<case_id>`` warning — and never
   silently reclassified, because quietly moving a regressed case between
   classes is exactly how a corpus stops measuring what it claims to.
2. **The classes do not contaminate each other.** Each aggregate is computed
   from its own class's cases only: its own status counts, its own legacy
   preservation, its own stale and provenance denominators, its own fallback
   rate and its own latency summary.
3. **Per-case measurements survive.** The aggregates are a summary, not a
   replacement: every
   :class:`~layered_memory_mcp.graph_shadow_evaluation.ShadowCaseMeasurement`
   is kept, in corpus order, beside the class it was declared under.
4. **The persistent store really is the one answering.** The writing store is
   closed before recall, the reopened store's own ``health`` reports the
   ``sqlite_embedded`` backend, and the mode handed to the seam must have
   ``recall_enabled`` set — a legacy or shadow-only decision is refused rather
   than quietly producing a comparison that never reached the graph.
5. **Latency is recorded, never equated.** A persistent read costs more than an
   in-memory one by construction, so ``latency_ms`` is measured for both sides
   and excluded from the comparison, as is ``backend`` — the two adapters
   report different labels by design, and that is not drift.

PRIVACY (§15)
-------------
Every report carries case ids, result identities, graph node/edge ids, counts,
rates, statuses, booleans and the bounded warning labels the graph seams
already emit. Backend names pass through the Batch J sanitizer
(:data:`~layered_memory_mcp.graph_shadow_evaluation.SAFE_BACKEND_LABELS`), so a
store reporting a connection string or an on-disk location cannot leak it. No
memory body, source path, section title, store diagnostic, exception message or
database path reaches any output — ``database`` is reported only as the boolean
:attr:`PersistentParityReport.database_file_present`. A refusal raised *by the
store* (a missing parent directory) is an exception, not report output, and
keeps the store's own wording.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field

from .graph_fixture_store import InMemoryGraphStore
from .graph_models import (
    GraphEdge,
    GraphModel,
    GraphNode,
    MemoryEvent,
    clean_identifier,
)
from .graph_runtime import (
    GRAPH_RESULT_IDENTITY_KEY,
    GraphModeDecision,
    GraphRecallRuntimeReport,
    run_graph_assisted_recall,
)
#: ``_safe_backend`` is module-private by naming and shared on purpose: it is
#: *the* rule for what a measurement may call a backend (§15), and restating it
#: here is how two evaluation modules would start disagreeing about which store
#: names are safe to repeat verbatim.
from .graph_shadow_evaluation import (
    DEFAULT_SHADOW_SETTINGS,
    SAFE_BACKEND_LABELS,
    LatencySummary,
    ShadowCaseMeasurement,
    ShadowEvaluationCase,
    ShadowEvaluationReport,
    ShadowEvaluationSettings,
    _safe_backend,
    evaluate_shadow_corpus,
)
from .graph_sqlite_store import SQLiteGraphStore
from .graph_store import TraversalFilters

# ---------------------------------------------------------------------------
# K-A: the two declared case classes
# ---------------------------------------------------------------------------

#: A healthy graph, valid seed metadata, current and in-scope nodes, resolvable
#: provenance. These cases measure retrieval *quality* — graph material, stale
#: exposure, provenance coverage, legacy preservation, latency.
CASE_CLASS_NORMAL = "normal_path"

#: An absent, unhealthy, raising, unmapped or no-material graph. These cases
#: measure *reliability* — that the legacy answer survives, that the fallback
#: reason is specific and bounded, and nothing else.
CASE_CLASS_FALLBACK = "fallback_path"

#: The whole classification vocabulary, sorted. A label outside it is refused:
#: "roughly normal" is not a class, and a third bucket invented at runtime would
#: make the two aggregates stop adding up to the corpus.
CASE_CLASSES: tuple[str, ...] = (CASE_CLASS_FALLBACK, CASE_CLASS_NORMAL)

CaseClass = Literal["fallback_path", "normal_path"]

#: Warning label prefix for a case whose declared class disagrees with its
#: measured fallback state. Reported rather than raised: a fixture that
#: regressed into a fallback is *evidence*, and refusing the whole corpus would
#: withhold the measurement exactly when somebody needs to read it.
CLASSIFICATION_DISAGREEMENT = "classification_disagreement"


@dataclass(frozen=True)
class ClassifiedShadowCase:
    """One Batch J case plus the class a reviewer declared it to be in.

    Frozen and validated at construction, so a corpus is a stable fixture. The
    class is never derived from the case: a store type, a case id or a
    measurement could all be made to *suggest* one, and a suggestion is how a
    corpus quietly stops containing the controls it was built to contain.

    Args:
        case: The explicit
            :class:`~layered_memory_mcp.graph_shadow_evaluation.ShadowEvaluationCase`.
        case_class: Exactly :data:`CASE_CLASS_NORMAL` or
            :data:`CASE_CLASS_FALLBACK`. Surrounding whitespace is trimmed;
            anything else — a non-string, a blank, a near-miss spelling, a
            different case — raises :class:`ValueError`.
    """

    case: ShadowEvaluationCase
    case_class: str

    def __post_init__(self) -> None:
        if not isinstance(self.case, ShadowEvaluationCase):
            raise ValueError(
                "case must be a ShadowEvaluationCase, got "
                f"{type(self.case).__name__}"
            )
        if not isinstance(self.case_class, str):
            raise ValueError(
                "case_class must be one of "
                f"{CASE_CLASSES}, got {type(self.case_class).__name__}; "
                "a classification is declared, never coerced"
            )
        label = clean_identifier(self.case_class, "case_class")
        if label not in CASE_CLASSES:
            raise ValueError(
                f"case_class must be one of {CASE_CLASSES}, got {label!r}; "
                "an ambiguous classification is refused rather than guessed"
            )
        object.__setattr__(self, "case_class", label)

    @property
    def case_id(self) -> str:
        """The wrapped case's own id — the key both classes are keyed by."""
        return self.case.case_id


# ---------------------------------------------------------------------------
# K-A: outputs
# ---------------------------------------------------------------------------

class ClassifiedCaseMeasurement(GraphModel):
    """One case's Batch J measurement, beside the class it was declared under.

    ``classification_consistent`` is the only derived field: whether the
    declaration agrees with what the case actually measured (a fallback-path
    case measures a fallback; a normal-path case does not). It is an
    observation about the fixture, not a verdict about the graph.
    """

    case_class: str
    case_id: str
    classification_consistent: bool = True
    measurement: ShadowCaseMeasurement

    def structural_to_json(self) -> str:
        """The deterministic part of this entry, as sorted JSON."""
        return json.dumps(
            self.model_dump(
                mode="json", exclude={"measurement": {"latency_ms"}}
            ),
            sort_keys=True,
            ensure_ascii=False,
        )


class ClassAggregate(GraphModel):
    """One class's own totals. Every field is an observation, not a judgement.

    Deliberately a *complete* re-report of the Batch J corpus metrics rather
    than a selected few: the point of separating the classes is that the same
    questions get asked of each one with its own denominator, so leaving a
    metric out of one class would reintroduce the mixing this batch exists to
    remove.

    An empty class is a valid answer — zero cases, zero rates, an empty latency
    summary — and is reported rather than omitted, so a corpus with no fallback
    controls is visibly a corpus with no fallback controls.
    """

    case_class: str = ""
    case_count: int = Field(default=0, ge=0)
    #: The class's case ids, in corpus order. The one list here that is not
    #: order-independent, and the reason it is kept: an aggregate has to be
    #: readable against the fixture that produced it.
    case_ids: list[str] = Field(default_factory=list)
    status_counts: dict[str, int] = Field(default_factory=dict)

    # -- legacy identity, order and payload survival -------------------------
    legacy_result_count: int = Field(default=0, ge=0)
    cases_with_identical_results: int = Field(default=0, ge=0)
    cases_with_preserved_payloads: int = Field(default=0, ge=0)
    cases_with_order_change: int = Field(default=0, ge=0)
    dropped_identities: int = Field(default=0, ge=0)
    added_identities: int = Field(default=0, ge=0)

    # -- graph material ------------------------------------------------------
    graph_assisted_results: int = Field(default=0, ge=0)
    graph_node_count: int = Field(default=0, ge=0)
    unmapped_results: int = Field(default=0, ge=0)
    truncated_cases: int = Field(default=0, ge=0)

    # -- stale exposure ------------------------------------------------------
    stale_measured_cases: int = Field(default=0, ge=0)
    stale_node_count: int = Field(default=0, ge=0)
    cases_with_stale_exposure: int = Field(default=0, ge=0)

    # -- provenance coverage, with its own denominator -----------------------
    provenance_measured_cases: int = Field(default=0, ge=0)
    provenance_expected: int = Field(default=0, ge=0)
    provenance_covered: int = Field(default=0, ge=0)
    provenance_coverage_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    provenance_records: int = Field(default=0, ge=0)

    # -- fallback ------------------------------------------------------------
    fallback_cases: int = Field(default=0, ge=0)
    fallback_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    fallback_reasons: dict[str, int] = Field(default_factory=dict)

    # -- cost ----------------------------------------------------------------
    latency: LatencySummary = Field(default_factory=LatencySummary)

    warnings: list[str] = Field(default_factory=list)


class SeparatedEvaluationReport(GraphModel):
    """The corpus measured as two classes, plus every per-case measurement.

    Verdict-free by construction: :meth:`separated_measurements` names the
    numbers a cutover review reads *per class*, and deciding whether any of
    them is good enough stays with that review (§12 Batch K-C).
    """

    case_count: int = Field(default=0, ge=0)
    #: How many cases each class was declared to hold. Always carries both
    #: labels, including a zero, so the denominators are never implicit.
    class_counts: dict[str, int] = Field(default_factory=dict)
    normal_path: ClassAggregate = Field(default_factory=ClassAggregate)
    fallback_path: ClassAggregate = Field(default_factory=ClassAggregate)
    #: Every measurement, in corpus order, with its declared class.
    cases: list[ClassifiedCaseMeasurement] = Field(default_factory=list)
    #: Case ids whose declaration disagreed with their measurement. Named, not
    #: reclassified.
    classification_disagreements: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def aggregate_for(self, case_class: str) -> ClassAggregate:
        """The aggregate for one declared class.

        Raises:
            ValueError: For any label outside :data:`CASE_CLASSES` — the same
                refusal :class:`ClassifiedShadowCase` makes, so a reader cannot
                ask for a class the corpus could not have contained.
        """
        if case_class == CASE_CLASS_NORMAL:
            return self.normal_path
        if case_class == CASE_CLASS_FALLBACK:
            return self.fallback_path
        raise ValueError(
            f"case_class must be one of {CASE_CLASSES}, got {case_class!r}"
        )

    def structural_to_json(self) -> str:
        """The deterministic part of the whole report, as sorted JSON.

        The measured latencies are excluded on purpose: they are genuine
        measurements and genuinely not reproducible, so folding them into a
        determinism check would make that check meaningless.
        """
        return json.dumps(
            self.model_dump(
                mode="json",
                exclude={
                    "normal_path": {"latency"},
                    "fallback_path": {"latency"},
                    "cases": {"__all__": {"measurement": {"latency_ms"}}},
                },
            ),
            sort_keys=True,
            ensure_ascii=False,
        )

    def separated_measurements(self) -> dict[str, Any]:
        """The per-class numbers a cutover review reads. No verdict is implied.

        Returning the two classes side by side, each with its own denominators,
        is the whole deliverable of K-A: a reader cannot accidentally average
        them, and nothing here states whether either class is acceptable.
        """
        return {
            "case_count": self.case_count,
            "class_counts": dict(self.class_counts),
            "classification_disagreements": list(self.classification_disagreements),
            CASE_CLASS_NORMAL: self.normal_path.to_dict(),
            CASE_CLASS_FALLBACK: self.fallback_path.to_dict(),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# K-A: the separated evaluator
# ---------------------------------------------------------------------------

def evaluate_separated_corpus(
    cases: Sequence[ClassifiedShadowCase],
    settings: ShadowEvaluationSettings = DEFAULT_SHADOW_SETTINGS,
) -> SeparatedEvaluationReport:
    """Evaluate a classified corpus, aggregating the two classes separately.

    Each class's cases are handed to
    :func:`~layered_memory_mcp.graph_shadow_evaluation.evaluate_shadow_corpus`
    as a corpus of their own — which is what makes every rate, denominator and
    latency summary a within-class number rather than a filtered view of a
    mixed one — and the per-case measurements are then re-assembled in the
    order the caller supplied.

    Args:
        cases: The explicitly classified fixture cases. Order is preserved in
            :attr:`SeparatedEvaluationReport.cases`; every aggregate is
            order-independent.
        settings: The bounded Batch J recall parameters, already validated.
            One settings object for the whole corpus, so the two classes are
            measured under identical conditions.

    Returns:
        A :class:`SeparatedEvaluationReport`.

    Raises:
        ValueError: For an unclassified or wrongly classified entry, or for a
            case id appearing more than once — one id in two classes makes the
            attribution ambiguous, and an ambiguous classification is refused
            rather than guessed. A *graph* failure is never raised: it is
            measured as a fallback.
    """
    classified = list(cases)
    for index, item in enumerate(classified):
        if not isinstance(item, ClassifiedShadowCase):
            raise ValueError(
                f"cases[{index}] must be a ClassifiedShadowCase, got "
                f"{type(item).__name__}; a case reaches this evaluator with its "
                "class already declared"
            )

    declared: dict[str, str] = {}
    for item in classified:
        if item.case_id in declared:
            raise ValueError(
                f"case id {item.case_id!r} is declared more than once "
                f"({declared[item.case_id]!r} and {item.case_class!r}); one "
                "case belongs to exactly one class"
            )
        declared[item.case_id] = item.case_class

    reports: dict[str, ShadowEvaluationReport] = {
        case_class: evaluate_shadow_corpus(
            [item.case for item in classified if item.case_class == case_class],
            settings,
        )
        for case_class in CASE_CLASSES
    }
    measurements: dict[str, ShadowCaseMeasurement] = {
        measurement.case_id: measurement
        for report in reports.values()
        for measurement in report.cases
    }

    entries: list[ClassifiedCaseMeasurement] = []
    disagreements: list[str] = []
    for item in classified:
        measurement = measurements[item.case_id]
        consistent = (item.case_class == CASE_CLASS_FALLBACK) == measurement.fallback
        if not consistent:
            disagreements.append(item.case_id)
        entries.append(
            ClassifiedCaseMeasurement(
                case_class=item.case_class,
                case_id=item.case_id,
                classification_consistent=consistent,
                measurement=measurement,
            )
        )

    aggregates = {
        case_class: _class_aggregate(case_class, reports[case_class])
        for case_class in CASE_CLASSES
    }
    return SeparatedEvaluationReport(
        case_count=len(classified),
        class_counts={
            case_class: aggregates[case_class].case_count
            for case_class in CASE_CLASSES
        },
        normal_path=aggregates[CASE_CLASS_NORMAL],
        fallback_path=aggregates[CASE_CLASS_FALLBACK],
        cases=entries,
        classification_disagreements=disagreements,
        warnings=sorted(
            {
                *(
                    warning
                    for report in reports.values()
                    for warning in report.warnings
                ),
                *(
                    f"{CLASSIFICATION_DISAGREEMENT}:{case_id}"
                    for case_id in disagreements
                ),
            }
        ),
    )


def _class_aggregate(
    case_class: str, report: ShadowEvaluationReport
) -> ClassAggregate:
    """Re-report one class's Batch J corpus report as a class aggregate.

    A re-report, not a recomputation: every number below is the one the Batch J
    evaluator already produced for this class's cases, which is what keeps the
    two batches from drifting on what "coverage" or "preserved" means.
    """
    return ClassAggregate(
        case_class=case_class,
        case_count=report.case_count,
        case_ids=[measurement.case_id for measurement in report.cases],
        status_counts=dict(report.status_counts),
        legacy_result_count=report.legacy_result_count,
        cases_with_identical_results=report.cases_with_identical_results,
        cases_with_preserved_payloads=report.cases_with_preserved_payloads,
        cases_with_order_change=report.cases_with_order_change,
        dropped_identities=report.dropped_identities,
        added_identities=report.added_identities,
        graph_assisted_results=report.graph_assisted_results,
        graph_node_count=report.graph_node_count,
        unmapped_results=report.unmapped_results,
        truncated_cases=report.truncated_cases,
        stale_measured_cases=report.stale_measured_cases,
        stale_node_count=report.stale_node_count,
        cases_with_stale_exposure=report.cases_with_stale_exposure,
        provenance_measured_cases=report.provenance_measured_cases,
        provenance_expected=report.provenance_expected,
        provenance_covered=report.provenance_covered,
        provenance_coverage_rate=report.provenance_coverage_rate,
        provenance_records=sum(
            measurement.provenance_records for measurement in report.cases
        ),
        fallback_cases=report.fallback_cases,
        fallback_rate=report.fallback_rate,
        fallback_reasons=dict(report.fallback_reasons),
        latency=report.latency,
        warnings=list(report.warnings),
    )


# ---------------------------------------------------------------------------
# K-B: persistent recall parity
# ---------------------------------------------------------------------------

#: The fixture arrived as explicit nodes and edges.
FIXTURE_SOURCE_MATERIAL = "nodes_and_edges"

#: The fixture arrived as an explicit append-only
#: :class:`~layered_memory_mcp.graph_models.MemoryEvent` stream, projected
#: through each store's own ``rebuild``.
FIXTURE_SOURCE_EVENTS = "event_stream"

#: The database target this helper refuses: an in-process SQLite database
#: cannot be closed and reopened, so a parity check over it would silently
#: compare an empty graph and answer the wrong question.
_IN_MEMORY_TARGET = ":memory:"


class GraphParityObservation(GraphModel):
    """One recall answer, reduced to bounded semantic observations.

    Everything here is an id, a count, a status, a boolean or a bounded label —
    the shape a privacy review can read (§15) and a fixture can assert on. The
    node bodies, source refs, paths, provenance quotes and store diagnostics
    the seams hold are deliberately not represented at all, so a parity
    comparison cannot become a second copy of the graph.
    """

    #: The bounded backend label, through the Batch J sanitizer. Recorded, and
    #: excluded from the comparison: the two adapters report different names by
    #: design.
    backend: str = ""
    mode: str = ""
    status: str = ""
    graph_assisted: bool = False
    graph_node_ids: list[str] = Field(default_factory=list)
    graph_edge_ids: list[str] = Field(default_factory=list)
    provenance_record_count: int = Field(default=0, ge=0)
    provenance_warnings: list[str] = Field(default_factory=list)
    conflict_count: int = Field(default=0, ge=0)
    #: The legacy result identities, in the legacy ranking order — so identity,
    #: order and count are one comparison.
    legacy_identities: list[str] = Field(default_factory=list)
    legacy_payloads_preserved: bool = True
    mapped_results: int = Field(default=0, ge=0)
    unmapped_results: int = Field(default=0, ge=0)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    #: Measured with :func:`time.perf_counter`. Recorded for the §12 K-C
    #: "p95 latency separately for in-memory and SQLite fixtures" input, and
    #: never compared for equality.
    latency_ms: float = Field(default=0.0, ge=0.0)


#: The two observation fields a persistent-vs-reference comparison must *not*
#: require to be equal, each for a documented reason rather than convenience.
PARITY_UNCOMPARED_FIELDS: tuple[str, ...] = ("backend", "latency_ms")

#: Everything else. Derived subtractively from the model's own fields, so a
#: field added to :class:`GraphParityObservation` is compared by default — the
#: safe direction for a parity check, and one that cannot silently narrow.
PARITY_COMPARED_FIELDS: tuple[str, ...] = tuple(
    sorted(set(GraphParityObservation.model_fields) - set(PARITY_UNCOMPARED_FIELDS))
)


class PersistentParityReport(GraphModel):
    """What the reopened persistent store and the reference store each observed.

    ``observations_match`` is an equality measurement over
    :data:`PARITY_COMPARED_FIELDS`, in the same spirit as
    :attr:`~layered_memory_mcp.graph_runtime.RecallComparison.identical`: it
    says whether the two bounded answers agree, not whether either is good
    enough for anything.
    """

    mode: str = ""
    recall_enabled: bool = False
    #: :data:`FIXTURE_SOURCE_MATERIAL` or :data:`FIXTURE_SOURCE_EVENTS`.
    fixture_source: str = ""
    #: Errors the event-stream rebuild reported, counted. ``0`` for a
    #: nodes/edges fixture, which is written through the normal write path.
    rebuild_errors: int = Field(default=0, ge=0)

    # -- the close/reopen evidence -------------------------------------------
    #: The store that wrote the fixture reported itself closed before recall.
    write_store_closed: bool = False
    #: A *new* store was opened on the same database for recall.
    reopened: bool = False
    #: The database target exists as a file. A boolean, never the path (§15).
    database_file_present: bool = False

    reference: GraphParityObservation = Field(
        default_factory=GraphParityObservation
    )
    persistent: GraphParityObservation = Field(
        default_factory=GraphParityObservation
    )

    compared_fields: list[str] = Field(default_factory=list)
    differing_fields: list[str] = Field(default_factory=list)
    observations_match: bool = False
    #: Both reported backend labels are known ones rather than pass-through
    #: strings — the §15 check, as data.
    backend_labels_bounded: bool = False

    #: Each graph's snapshot was byte-identical before and after recall.
    reference_snapshot_unchanged: bool = True
    persistent_snapshot_unchanged: bool = True

    warnings: list[str] = Field(default_factory=list)

    def structural_to_json(self) -> str:
        """The deterministic part of the report, as sorted JSON (no latency)."""
        return json.dumps(
            self.model_dump(
                mode="json",
                exclude={
                    "reference": {"latency_ms"},
                    "persistent": {"latency_ms"},
                },
            ),
            sort_keys=True,
            ensure_ascii=False,
        )

    def parity_measurements(self) -> dict[str, Any]:
        """The bounded numbers a cutover review reads. No verdict is implied."""
        return {
            "mode": self.mode,
            "fixture_source": self.fixture_source,
            "write_store_closed": self.write_store_closed,
            "reopened": self.reopened,
            "database_file_present": self.database_file_present,
            "rebuild_errors": self.rebuild_errors,
            "compared_fields": list(self.compared_fields),
            "differing_fields": list(self.differing_fields),
            "observations_match": self.observations_match,
            "backend_labels_bounded": self.backend_labels_bounded,
            "reference_snapshot_unchanged": self.reference_snapshot_unchanged,
            "persistent_snapshot_unchanged": self.persistent_snapshot_unchanged,
            "reference": self.reference.to_dict(),
            "persistent": self.persistent.to_dict(),
            "warnings": list(self.warnings),
        }


def run_persistent_parity_check(
    *,
    database: str | Path,
    legacy_results: Sequence[Mapping[str, Any]],
    mode: GraphModeDecision,
    nodes: Sequence[GraphNode] | None = None,
    edges: Sequence[GraphEdge] | None = None,
    source_events: Sequence[MemoryEvent] | None = None,
    max_hops: int = 1,
    filters: TraversalFilters | None = None,
    top_n: int | None = None,
    include_provenance: bool = True,
    include_conflicts: bool = True,
) -> PersistentParityReport:
    """Run graph-assisted recall against a reopened SQLite graph and compare.

    The sequence, and every step of it is the point:

    1. Build the Batch C reference store from the explicit fixture.
    2. Write the same fixture into a :class:`SQLiteGraphStore` at ``database``.
    3. **Close** that store, so nothing in process memory can answer for it.
    4. Open a *new* store on the same database.
    5. Run :func:`~layered_memory_mcp.graph_runtime.run_graph_assisted_recall`
       — the existing seam, unmodified — against each store under the same
       explicit ``mode``, snapshotting both graphs before and after.
    6. Reduce each answer to a :class:`GraphParityObservation` and compare the
       two on :data:`PARITY_COMPARED_FIELDS`.

    Args:
        database: The database target, required and explicit. A path (``str``
            or :class:`~pathlib.Path`) whose parent directory already exists;
            nothing is created, derived or discovered here, and
            ``":memory:"`` is refused because it cannot be reopened.
        legacy_results: The ranked legacy payloads, in ranking order. Graph
            identity comes from their own explicit metadata, exactly as the
            runtime seam reads it — nothing is inferred from a filename.
        mode: The resolved decision. Must have ``recall_enabled`` set: this
            helper exists to verify that the seam *runs* against the persistent
            adapter, so a mode that forbids recall is refused rather than
            producing a comparison of two untouched graphs.
        nodes: Explicit fixture nodes. Supply these and/or ``edges``, or
            ``source_events`` — never both kinds.
        edges: Explicit fixture edges.
        source_events: An explicit append-only event stream, projected through
            each store's own ``rebuild``.
        max_hops: Expansion depth, passed to the seam unchanged.
        filters: Hard traversal filters, passed to the seam unchanged.
        top_n: Optional cap on how many top-ranked mapped results are expanded.
        include_provenance: Whether to backfill evidence lineage.
        include_conflicts: Whether to scan for unresolved contradictions.

    Returns:
        A :class:`PersistentParityReport`.

    Raises:
        ValueError: For a caller-contract mistake — a blank or in-memory
            database target, a mode that is not recall-enabled, both fixture
            kinds supplied, or neither.
        GraphStoreError: As the store itself raises it, unchanged — notably
            when ``database``'s parent directory does not exist, because this
            helper never creates one.
    """
    target = _parity_database(database)
    if not mode.recall_enabled:
        raise ValueError(
            "run_persistent_parity_check needs a recall-enabled "
            f"GraphModeDecision; mode {mode.mode!r} would reach no graph at "
            "all, so there would be nothing to compare"
        )

    has_material = nodes is not None or edges is not None
    has_events = source_events is not None
    if has_material and has_events:
        raise ValueError(
            "supply either an explicit nodes/edges fixture or an explicit "
            "event stream, not both: two sources of truth for one graph make "
            "the comparison ambiguous"
        )
    if not has_material and not has_events:
        raise ValueError(
            "supply an explicit nodes/edges fixture or an explicit event "
            "stream; this helper never discovers graph material"
        )

    expected = [deepcopy(dict(result)) for result in legacy_results]
    warnings: list[str] = []
    rebuild_errors = 0

    # -- 1. the reference graph ---------------------------------------------
    if has_events:
        reference = InMemoryGraphStore()
        rebuild_errors = len(reference.rebuild(list(source_events or ())).errors)
    else:
        reference = InMemoryGraphStore(nodes=nodes or (), edges=edges or ())

    # -- 2/3. the persistent graph, written and then closed -----------------
    if has_events:
        writer = SQLiteGraphStore(target)
        try:
            writer.rebuild(list(source_events or ()))
        finally:
            writer.close()
    else:
        writer = SQLiteGraphStore(target, nodes=nodes or (), edges=edges or ())
        writer.close()
    write_store_closed = writer.closed

    # -- 4/5. reopen and run the existing seam against each store -----------
    persistent = SQLiteGraphStore(target)
    reopened = not persistent.closed
    try:
        reference_observation, reference_unchanged = _observe(
            reference,
            expected,
            mode=mode,
            max_hops=max_hops,
            filters=filters,
            top_n=top_n,
            include_provenance=include_provenance,
            include_conflicts=include_conflicts,
        )
        persistent_observation, persistent_unchanged = _observe(
            persistent,
            expected,
            mode=mode,
            max_hops=max_hops,
            filters=filters,
            top_n=top_n,
            include_provenance=include_provenance,
            include_conflicts=include_conflicts,
        )
    finally:
        persistent.close()

    # -- 6. compare the bounded observations --------------------------------
    differing = [
        name
        for name in PARITY_COMPARED_FIELDS
        if getattr(reference_observation, name)
        != getattr(persistent_observation, name)
    ]

    if rebuild_errors:
        warnings.append(f"event_stream_rebuild_errors:{rebuild_errors}")
    if differing:
        warnings.append(f"parity_differences:{len(differing)}")
    if not reference_unchanged:
        warnings.append("graph_mutated_during_recall:reference")
    if not persistent_unchanged:
        warnings.append("graph_mutated_during_recall:persistent")
    if not write_store_closed or not reopened:
        warnings.append("persistent_store_not_reopened")

    return PersistentParityReport(
        mode=mode.mode,
        recall_enabled=mode.recall_enabled,
        fixture_source=(
            FIXTURE_SOURCE_EVENTS if has_events else FIXTURE_SOURCE_MATERIAL
        ),
        rebuild_errors=rebuild_errors,
        write_store_closed=write_store_closed,
        reopened=reopened,
        database_file_present=Path(target).is_file(),
        reference=reference_observation,
        persistent=persistent_observation,
        compared_fields=list(PARITY_COMPARED_FIELDS),
        differing_fields=differing,
        observations_match=not differing,
        backend_labels_bounded=all(
            observation.backend in SAFE_BACKEND_LABELS
            for observation in (reference_observation, persistent_observation)
        ),
        reference_snapshot_unchanged=reference_unchanged,
        persistent_snapshot_unchanged=persistent_unchanged,
        warnings=sorted(set(warnings)),
    )


def _parity_database(database: str | Path) -> str:
    """Validate the explicit database target. Nothing is created or derived.

    Two refusals, both about the question this helper is asked rather than
    about taste: a blank target names no database, and ``":memory:"`` cannot
    survive the close/reopen that is the whole point of K-B — reopening it
    yields an empty graph, and an empty graph would compare equal to nothing in
    a way that looks like success.
    """
    if isinstance(database, Path):
        target = str(database)
    elif isinstance(database, str):
        target = database
    else:
        raise ValueError(
            "database must be a str or Path naming a database file, got "
            f"{type(database).__name__}"
        )
    stripped = target.strip()
    if not stripped:
        raise ValueError(
            "database must name a database file; this helper has no default "
            "target and never derives one"
        )
    if stripped == _IN_MEMORY_TARGET:
        raise ValueError(
            f"{_IN_MEMORY_TARGET!r} cannot be closed and reopened, so it "
            "cannot answer the persistent-parity question; supply a database "
            "file under a directory that already exists"
        )
    return stripped


def _observe(
    store: InMemoryGraphStore | SQLiteGraphStore,
    expected: Sequence[Mapping[str, Any]],
    *,
    mode: GraphModeDecision,
    max_hops: int,
    filters: TraversalFilters | None,
    top_n: int | None,
    include_provenance: bool,
    include_conflicts: bool,
) -> tuple[GraphParityObservation, bool]:
    """Run the recall seam against one store and reduce the answer.

    Returns the bounded observation and whether the store's own snapshot was
    byte-identical before and after — the read-only claim, measured rather than
    asserted.
    """
    before = store.snapshot()
    started = time.perf_counter()
    runtime = run_graph_assisted_recall(
        list(expected),
        store,
        mode=mode,
        max_hops=max_hops,
        filters=filters,
        top_n=top_n,
        include_provenance=include_provenance,
        include_conflicts=include_conflicts,
    )
    latency_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    return (
        _observation(runtime, expected, latency_ms),
        store.snapshot() == before,
    )


def _observation(
    runtime: GraphRecallRuntimeReport,
    expected: Sequence[Mapping[str, Any]],
    latency_ms: float,
) -> GraphParityObservation:
    """Reduce one runtime recall report to bounded semantic observations."""
    results = runtime.graph_results
    provenance = [record for result in results for record in result.provenance]
    return GraphParityObservation(
        backend=_safe_backend(runtime.backend),
        mode=runtime.mode,
        status=runtime.status,
        graph_assisted=runtime.graph_assisted,
        graph_node_ids=sorted(
            {node.id for result in results for node in result.graph_nodes}
        ),
        graph_edge_ids=sorted(
            {edge.id for result in results for edge in result.graph_edges}
        ),
        provenance_record_count=len(provenance),
        provenance_warnings=sorted(
            {warning for record in provenance for warning in record.warnings}
        ),
        conflict_count=sum(len(result.conflicts) for result in results),
        legacy_identities=[
            str(result.get(GRAPH_RESULT_IDENTITY_KEY, ""))
            for result in runtime.results
        ],
        legacy_payloads_preserved=runtime.results == list(expected),
        mapped_results=runtime.seed_mapping.mapped_results,
        unmapped_results=runtime.seed_mapping.unmapped_results,
        truncated=runtime.report.truncated if runtime.report is not None else False,
        warnings=list(runtime.warnings),
        latency_ms=latency_ms,
    )


__all__ = [
    "CASE_CLASSES",
    "CASE_CLASS_FALLBACK",
    "CASE_CLASS_NORMAL",
    "CLASSIFICATION_DISAGREEMENT",
    "FIXTURE_SOURCE_EVENTS",
    "FIXTURE_SOURCE_MATERIAL",
    "PARITY_COMPARED_FIELDS",
    "PARITY_UNCOMPARED_FIELDS",
    "CaseClass",
    "ClassAggregate",
    "ClassifiedCaseMeasurement",
    "ClassifiedShadowCase",
    "GraphParityObservation",
    "PersistentParityReport",
    "SeparatedEvaluationReport",
    "evaluate_separated_corpus",
    "run_persistent_parity_check",
]

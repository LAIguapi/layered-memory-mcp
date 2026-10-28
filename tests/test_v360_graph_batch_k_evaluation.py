"""v3.6.0 Batch K — separated evaluation and persistent recall parity.

Why this file exists: ARCHITECTURE_v2.md §12 Batch K names two ways the Batch J
measurement could mislead a cutover review, and asks for the instruments that
close them.

  * **K-A — a mixed corpus is one misleading number.** Averaging a healthy
    graph-assisted case together with an absent, raising or unmapped one
    produces a "provenance coverage" figure that is really a fallback rate in
    disguise. The two classes have to be declared explicitly and aggregated
    apart, with positive *and* negative controls for stale exposure and
    provenance gaps rather than an all-clean fixture.
  * **K-B — an in-memory seam is not evidence about the persistent runtime.**
    The same explicit cases are written into a temporary ``SQLiteGraphStore``,
    the store is **closed**, a new one is opened on the same database, and the
    existing graph-recall seam runs against *that* — compared to the in-memory
    reference on bounded semantic observations only.

What these tests pin:

  * **Classification is declared, never guessed.** Anything other than the two
    explicit labels is refused, a duplicated case id is refused, and a
    declaration that disagrees with the measurement is *reported* rather than
    silently reclassified.
  * **The two classes never contaminate each other.** The normal class's
    provenance coverage is computed over normal-path denominators alone; the
    fallback rate stays an operational number in the fallback class.
  * **Nothing is decided.** No verdict, threshold, approval or cutover field
    exists anywhere in the public surface.
  * **The persistent adapter is really the one under test.** The writing store
    is closed, the database file exists on disk, the reopened store reports the
    ``sqlite_embedded`` backend, and recall runs under an explicit
    recall-enabled ``GraphModeDecision``.
  * **Legacy is untouched.** Under ``legacy`` the reopened persistent store
    receives zero operations and the legacy payloads come back unchanged.
  * **Read-only, and bounded.** Not one write operation is reached on either
    backend, both graphs stay byte-identical, and no memory body, source path,
    database path or backend diagnostic reaches any report.

Isolation: every store is an in-memory fixture or a database under pytest's
``tmp_path``. No production configuration, MCP state or user memory data is
read, nothing is auto-discovered, and no directory is created.
"""

from __future__ import annotations

import ast
import inspect
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from layered_memory_mcp import graph_batch_k_evaluation
from layered_memory_mcp.graph_batch_k_evaluation import (
    CASE_CLASSES,
    CASE_CLASS_FALLBACK,
    CASE_CLASS_NORMAL,
    CLASSIFICATION_DISAGREEMENT,
    FIXTURE_SOURCE_EVENTS,
    FIXTURE_SOURCE_MATERIAL,
    PARITY_COMPARED_FIELDS,
    PARITY_UNCOMPARED_FIELDS,
    ClassAggregate,
    ClassifiedShadowCase,
    GraphParityObservation,
    PersistentParityReport,
    SeparatedEvaluationReport,
    evaluate_separated_corpus,
    run_persistent_parity_check,
)
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
from layered_memory_mcp.graph_recall import (
    GRAPH_STATUS_OK,
    GRAPH_STATUS_PARTIAL,
    GRAPH_STATUS_UNAVAILABLE,
    GRAPH_STATUS_UNUSED,
)
from layered_memory_mcp.graph_runtime import (
    GRAPH_SEED_ID_KEY,
    GRAPH_STATUS_DISABLED,
    LEGACY_MODE_DECISION,
    GraphModeDecision,
    run_graph_assisted_recall,
)
from layered_memory_mcp.graph_shadow_evaluation import (
    FALLBACK_GRAPH_UNAVAILABLE,
    FALLBACK_GRAPH_UNUSED,
    FALLBACK_NONE,
    FALLBACK_NO_GRAPH_MATERIAL,
    NOT_MEASURED_PROVENANCE,
    LatencySummary,
    ShadowCaseMeasurement,
    ShadowEvaluationCase,
    ShadowEvaluationSettings,
    evaluate_shadow_corpus,
)
from layered_memory_mcp.graph_sqlite_store import (
    SQLITE_GRAPH_BACKEND,
    GraphStoreError,
    SQLiteGraphStore,
)
from layered_memory_mcp.graph_store import GraphHealth, TraversalFilters

# ---------------------------------------------------------------------------
# One small, explicit, neutral fixture
#
# Deliberately not an all-clean graph: it carries a healthy current claim with
# evidence and episode context *and* a superseded control that must stay hidden
# under the default filters and become visible when they are widened. A metric
# that cannot fire proves nothing.
# ---------------------------------------------------------------------------

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)

NAMESPACE = "batch_k_fixture"

EPISODE_ID = "episode_batch_k_0101"
EVIDENCE_ID = "evidence_batch_k_0101_0001"
ENTITY_ID = "entity_build_cache"
CURRENT_CLAIM_ID = "claim_cache_warmed_nightly"
SUPERSEDED_CLAIM_ID = "claim_cache_warmed_weekly"
ABSENT_CLAIM_ID = "claim_not_in_this_graph"

CLAIM_FILE = "build-cache.md"
ENTITY_FILE = "caching.md"
UNMAPPED_FILE = "observability.md"

#: A memory body that must never reach any Batch K report.
CLAIM_BODY = "The build cache is warmed nightly."
#: A source path that must never reach any Batch K report.
SOURCE_PATH = "knowledge/build-cache.md"
#: A section title that must never reach any Batch K report.
SECTION_TITLE = "Cache maintenance"
#: A store diagnostic naming a location. It must never reach a report either.
BACKEND_DIAGNOSTIC_PATH = "/tmp/not-a-real-fixture-path/graph.db"

#: What the fixture considers current and in scope at ``T1``.
EXPECTED_CURRENT_IDS = (CURRENT_CLAIM_ID, ENTITY_ID, EVIDENCE_ID, EPISODE_ID)
#: The lineage graph assistance is expected to resolve for the current claim.
EXPECTED_PROVENANCE_IDS = (EVIDENCE_ID, EPISODE_ID)


def _scope() -> GraphScope:
    return GraphScope(namespace=NAMESPACE)


def _source_ref() -> SourceRef:
    return SourceRef(
        episode_id=EPISODE_ID,
        evidence_id=EVIDENCE_ID,
        source_file=SOURCE_PATH,
        source_section=SECTION_TITLE,
    )


def _edge(relation: RelationType, source: str, target: str) -> GraphEdge:
    data = {
        "id": make_edge_id(relation, source, target),
        "relation_type": relation,
        "source_id": source,
        "target_id": target,
        "recorded_at": T0,
    }
    if relation is RelationType.SUPERSEDES:
        data["evidence_refs"] = [_source_ref()]
    return GraphEdge(**data)


def _fixture_nodes() -> list[GraphNode]:
    return [
        GraphNode(id=EPISODE_ID, kind=NodeKind.EPISODE,
                  label="build session 0101", scope=_scope(), recorded_at=T0),
        GraphNode(id=EVIDENCE_ID, kind=NodeKind.EVIDENCE,
                  label="cache maintenance note", content=CLAIM_BODY,
                  scope=_scope(), recorded_at=T0),
        GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, label="build cache",
                  scope=_scope(), recorded_at=T0),
        GraphNode(id=CURRENT_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Build cache warming cadence", content=CLAIM_BODY,
                  memory_class=MemoryClass.FACT, scope=_scope(),
                  recorded_at=T1, valid_from=T1, source_refs=[_source_ref()]),
        # The negative control: current validity window, historical status.
        GraphNode(id=SUPERSEDED_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Build cache warming cadence",
                  content="The build cache is warmed weekly.",
                  memory_class=MemoryClass.FACT, status=NodeStatus.SUPERSEDED,
                  scope=_scope(), recorded_at=T0, valid_from=T0, valid_to=T1,
                  derivation=Derivation(method=DerivationMethod.MIGRATED,
                                        generated_at=T0)),
    ]


def _fixture_edges() -> list[GraphEdge]:
    return [
        _edge(RelationType.ABOUT, CURRENT_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.ABOUT, SUPERSEDED_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.EVIDENCED_BY, CURRENT_CLAIM_ID, EVIDENCE_ID),
        _edge(RelationType.OBSERVED_IN, CURRENT_CLAIM_ID, EPISODE_ID),
        _edge(RelationType.SUPERSEDES, CURRENT_CLAIM_ID, SUPERSEDED_CLAIM_ID),
    ]


def _fixture_store() -> InMemoryGraphStore:
    return InMemoryGraphStore(nodes=_fixture_nodes(), edges=_fixture_edges())


def _fixture_events() -> list[MemoryEvent]:
    """The same graph as an explicit append-only event stream.

    One event per projected item, so the stream is readable in a diff and the
    endpoint/provenance resolution rules are exercised over the whole ledger.
    """
    events: list[MemoryEvent] = []
    for index, node in enumerate(_fixture_nodes()):
        events.append(
            MemoryEvent(
                id=f"event_node_{index:02d}",
                kind=MemoryEventKind.INJECT,
                occurred_at=T0 + timedelta(minutes=index),
                node=node,
            )
        )
    for index, edge in enumerate(_fixture_edges()):
        events.append(
            MemoryEvent(
                id=f"event_edge_{index:02d}",
                kind=MemoryEventKind.INJECT,
                occurred_at=T1 + timedelta(minutes=index),
                edge=edge,
            )
        )
    return events


def _filters() -> TraversalFilters:
    """Hard filters pinned to the fixture's own scope and instant."""
    return TraversalFilters(scope=_scope(), as_of=T1)


def _settings(**kwargs) -> ShadowEvaluationSettings:
    data = {"max_hops": 1, "filters": _filters()}
    data.update(kwargs)
    return ShadowEvaluationSettings(**data)


def _graph_mode() -> GraphModeDecision:
    """An explicit, authoritative recall mode — never read from a config."""
    return GraphModeDecision(mode="graph_recall", recall_enabled=True)


# --- legacy recall payloads --------------------------------------------------

def _claim_row(**kwargs) -> dict:
    """A legacy recall payload carrying its explicit graph identity metadata."""
    data = {
        "file": CLAIM_FILE,
        "score": 9.5,
        "matched_sections": 2,
        "sections": [{"title": SECTION_TITLE, "content": CLAIM_BODY}],
        "metadata": {GRAPH_SEED_ID_KEY: CURRENT_CLAIM_ID},
    }
    data.update(kwargs)
    return data


def _entity_row(**kwargs) -> dict:
    data = {
        "file": ENTITY_FILE,
        "score": 4.25,
        "matched_sections": 1,
        "metadata": {GRAPH_SEED_ID_KEY: ENTITY_ID},
    }
    data.update(kwargs)
    return data


def _unmapped_row(**kwargs) -> dict:
    """A legacy row with no graph identity at all."""
    data = {"file": UNMAPPED_FILE, "score": 1.0, "matched_sections": 0}
    data.update(kwargs)
    return data


# --- stores that record, degrade or raise ------------------------------------

class _RecordingStore:
    """Delegates the four read operations and records which were reached.

    The write operations are implemented *and* fail loudly: the property under
    test is that evaluation never calls them even though it could.
    """

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[str] = []

    def traverse(self, *args, **kwargs):
        self.calls.append("traverse")
        return self.inner.traverse(*args, **kwargs)

    def get_provenance(self, *args, **kwargs):
        self.calls.append("get_provenance")
        return self.inner.get_provenance(*args, **kwargs)

    def find_conflicts(self, *args, **kwargs):
        self.calls.append("find_conflicts")
        return self.inner.find_conflicts(*args, **kwargs)

    def health(self, *args, **kwargs):
        self.calls.append("health")
        return self.inner.health(*args, **kwargs)

    def get_node(self, *args, **kwargs):
        self.calls.append("get_node")
        return self.inner.get_node(*args, **kwargs)

    def get_edges(self, *args, **kwargs):
        self.calls.append("get_edges")
        return self.inner.get_edges(*args, **kwargs)

    def create_node(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("batch K evaluation must not write to the graph")

    def append_edge(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("batch K evaluation must not write to the graph")

    def rebuild(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("batch K evaluation must not write to the graph")


class _RaisingTraversalStore(_RecordingStore):
    def __init__(self, inner=None):
        super().__init__(inner or _fixture_store())

    def traverse(self, *args, **kwargs):
        self.calls.append("traverse")
        raise RuntimeError(f"backend diagnostic naming {BACKEND_DIAGNOSTIC_PATH}")


class _UnhealthyStore(_RecordingStore):
    def __init__(self, inner=None):
        super().__init__(inner or _fixture_store())

    def health(self, *args, **kwargs):
        self.calls.append("health")
        return GraphHealth(
            backend="in_memory_fixture",
            errors=[f"ledger_unreadable:{BACKEND_DIAGNOSTIC_PATH}"],
        )


READ_OPERATIONS = {"health", "traverse", "get_provenance", "find_conflicts"}


# --- the classified corpus ---------------------------------------------------

def _normal_case(**kwargs) -> ShadowEvaluationCase:
    data = {
        "case_id": "normal_healthy_claim",
        "legacy_results": [_claim_row()],
        "store": _fixture_store(),
        "seed_node_ids_by_result": {CLAIM_FILE: [CURRENT_CLAIM_ID]},
        "expected_current_node_ids": EXPECTED_CURRENT_IDS,
        "expected_provenance_node_ids": EXPECTED_PROVENANCE_IDS,
    }
    data.update(kwargs)
    return ShadowEvaluationCase(**data)


def _normal_mixed_case(**kwargs) -> ShadowEvaluationCase:
    """Two mapped rows and one row with no graph identity, in ranking order."""
    data = {
        "case_id": "normal_mixed_ranking",
        "legacy_results": [_claim_row(), _entity_row(), _unmapped_row()],
        "store": _fixture_store(),
        "seed_node_ids_by_result": {
            CLAIM_FILE: [CURRENT_CLAIM_ID],
            ENTITY_FILE: [ENTITY_ID],
        },
        "expected_current_node_ids": EXPECTED_CURRENT_IDS,
        "expected_provenance_node_ids": EXPECTED_PROVENANCE_IDS,
    }
    data.update(kwargs)
    return ShadowEvaluationCase(**data)


def _fallback_case(case_id: str, **kwargs) -> ShadowEvaluationCase:
    data = {
        "case_id": case_id,
        "legacy_results": [_claim_row()],
        "store": _fixture_store(),
        "seed_node_ids_by_result": {CLAIM_FILE: [CURRENT_CLAIM_ID]},
        "expected_current_node_ids": EXPECTED_CURRENT_IDS,
        "expected_provenance_node_ids": EXPECTED_PROVENANCE_IDS,
    }
    data.update(kwargs)
    return ShadowEvaluationCase(**data)


def _normal(case: ShadowEvaluationCase) -> ClassifiedShadowCase:
    return ClassifiedShadowCase(case=case, case_class=CASE_CLASS_NORMAL)


def _fallback(case: ShadowEvaluationCase) -> ClassifiedShadowCase:
    return ClassifiedShadowCase(case=case, case_class=CASE_CLASS_FALLBACK)


def _corpus() -> list[ClassifiedShadowCase]:
    """Two normal-path cases and four fallback cases, declared explicitly.

    The four fallbacks cover every reason the seam distinguishes: an absent
    graph, a raising one, an unhealthy one, a usable graph nothing mapped to,
    and a mapped seed the graph does not hold.
    """
    return [
        _normal(_normal_case()),
        _normal(_normal_mixed_case()),
        _fallback(_fallback_case("fallback_absent_graph", store=None)),
        _fallback(
            _fallback_case(
                "fallback_raising_traversal", store=_RaisingTraversalStore()
            )
        ),
        _fallback(
            _fallback_case(
                "fallback_unmapped_results",
                legacy_results=[_unmapped_row()],
                seed_node_ids_by_result={},
            )
        ),
        _fallback(
            _fallback_case(
                "fallback_no_graph_material",
                seed_node_ids_by_result={CLAIM_FILE: [ABSENT_CLAIM_ID]},
            )
        ),
    ]


# ---------------------------------------------------------------------------
# K-A: classification is declared, and ambiguity is refused
# ---------------------------------------------------------------------------

def test_the_two_case_classes_are_the_only_declared_labels():
    assert set(CASE_CLASSES) == {CASE_CLASS_NORMAL, CASE_CLASS_FALLBACK}
    assert CASE_CLASS_NORMAL == "normal_path"
    assert CASE_CLASS_FALLBACK == "fallback_path"


@pytest.mark.parametrize(
    "label",
    ["", "   ", "normal", "fallback", "NORMAL_PATH", "both", "unknown", "\n"],
)
def test_a_case_class_outside_the_two_labels_is_refused(label):
    with pytest.raises(ValueError):
        ClassifiedShadowCase(case=_normal_case(), case_class=label)


@pytest.mark.parametrize("label", [None, 1, True, ["normal_path"]])
def test_a_non_string_case_class_is_refused_rather_than_coerced(label):
    with pytest.raises(ValueError):
        ClassifiedShadowCase(case=_normal_case(), case_class=label)


def test_a_declared_class_is_normalized_but_never_guessed():
    classified = ClassifiedShadowCase(
        case=_normal_case(), case_class=f"  {CASE_CLASS_NORMAL} "
    )

    assert classified.case_class == CASE_CLASS_NORMAL
    assert classified.case_id == "normal_healthy_claim"


def test_a_classified_case_must_wrap_a_shadow_evaluation_case():
    with pytest.raises(ValueError):
        ClassifiedShadowCase(case="normal_healthy_claim", case_class=CASE_CLASS_NORMAL)


def test_an_unclassified_case_cannot_reach_the_evaluator():
    with pytest.raises(ValueError):
        evaluate_separated_corpus([_normal_case()], settings=_settings())


def test_one_case_id_declared_in_two_classes_is_refused_as_ambiguous():
    corpus = [
        _normal(_normal_case()),
        _fallback(_fallback_case("normal_healthy_claim", store=None)),
    ]

    with pytest.raises(ValueError):
        evaluate_separated_corpus(corpus, settings=_settings())


def test_a_case_id_repeated_inside_one_class_is_refused():
    corpus = [_normal(_normal_case()), _normal(_normal_case())]

    with pytest.raises(ValueError):
        evaluate_separated_corpus(corpus, settings=_settings())


def test_a_declaration_that_disagrees_with_the_measurement_is_reported():
    """A mis-declared case is named, not quietly moved into the other class."""
    corpus = [
        _normal(_normal_case()),
        # Declared normal, but its graph is absent: it will measure a fallback.
        _normal(_fallback_case("mis_declared_absent_graph", store=None)),
    ]

    report = evaluate_separated_corpus(corpus, settings=_settings())

    assert report.classification_disagreements == ["mis_declared_absent_graph"]
    assert (
        f"{CLASSIFICATION_DISAGREEMENT}:mis_declared_absent_graph"
        in report.warnings
    )
    # It stays where it was declared — nothing was reclassified by guesswork.
    assert report.normal_path.case_count == 2
    assert report.fallback_path.case_count == 0
    assert [case.classification_consistent for case in report.cases] == [True, False]


def test_a_consistent_corpus_reports_no_classification_disagreement():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    assert report.classification_disagreements == []
    assert all(case.classification_consistent for case in report.cases)
    assert not any(
        warning.startswith(CLASSIFICATION_DISAGREEMENT)
        for warning in report.warnings
    )


# ---------------------------------------------------------------------------
# K-A: the two classes are aggregated apart
# ---------------------------------------------------------------------------

def test_the_corpus_is_aggregated_into_two_separate_classes():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    assert isinstance(report, SeparatedEvaluationReport)
    assert report.case_count == 6
    assert report.class_counts == {CASE_CLASS_FALLBACK: 4, CASE_CLASS_NORMAL: 2}

    assert isinstance(report.normal_path, ClassAggregate)
    assert report.normal_path.case_class == CASE_CLASS_NORMAL
    assert report.normal_path.case_ids == [
        "normal_healthy_claim",
        "normal_mixed_ranking",
    ]
    assert report.fallback_path.case_class == CASE_CLASS_FALLBACK
    assert report.fallback_path.case_ids == [
        "fallback_absent_graph",
        "fallback_raising_traversal",
        "fallback_unmapped_results",
        "fallback_no_graph_material",
    ]
    assert report.aggregate_for(CASE_CLASS_NORMAL) is report.normal_path
    assert report.aggregate_for(CASE_CLASS_FALLBACK) is report.fallback_path


def test_an_unknown_class_cannot_be_looked_up_in_the_report():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    with pytest.raises(ValueError):
        report.aggregate_for("some_other_class")


def test_each_class_counts_its_own_statuses():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    assert report.normal_path.status_counts == {
        GRAPH_STATUS_OK: 1,
        GRAPH_STATUS_PARTIAL: 1,
    }
    assert report.fallback_path.status_counts == {
        GRAPH_STATUS_PARTIAL: 1,
        GRAPH_STATUS_UNAVAILABLE: 2,
        GRAPH_STATUS_UNUSED: 1,
    }
    assert sum(report.normal_path.status_counts.values()) == 2
    assert sum(report.fallback_path.status_counts.values()) == 4


def test_a_fallback_rate_is_never_mixed_into_normal_provenance_quality():
    """The whole point of K-A, stated as one comparison.

    The same six cases, measured as one corpus, report a provenance coverage of
    one third — a number produced by four graph outages, not by a retrieval
    quality. Separated, the normal class reports full coverage with its own
    denominator and the fallback class reports the outage rate as the
    operational fact it is.
    """
    separated = evaluate_separated_corpus(_corpus(), settings=_settings())
    mixed = evaluate_shadow_corpus(
        [item.case for item in _corpus()], settings=_settings()
    )

    assert separated.normal_path.fallback_cases == 0
    assert separated.normal_path.fallback_rate == 0.0
    assert separated.normal_path.fallback_reasons == {}
    assert separated.normal_path.provenance_expected == 4
    assert separated.normal_path.provenance_covered == 4
    assert separated.normal_path.provenance_coverage_rate == 1.0

    assert separated.fallback_path.fallback_cases == 4
    assert separated.fallback_path.fallback_rate == 1.0
    assert separated.fallback_path.provenance_expected == 8
    assert separated.fallback_path.provenance_covered == 0
    assert separated.fallback_path.provenance_coverage_rate == 0.0

    # The misleading single number the separation exists to prevent.
    assert mixed.provenance_coverage_rate == pytest.approx(1 / 3)
    assert mixed.fallback_rate == pytest.approx(4 / 6)


def test_each_class_names_its_own_fallback_reasons():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    assert report.fallback_path.fallback_reasons == {
        FALLBACK_GRAPH_UNAVAILABLE: 2,
        FALLBACK_GRAPH_UNUSED: 1,
        FALLBACK_NO_GRAPH_MATERIAL: 1,
    }
    assert sum(report.fallback_path.fallback_reasons.values()) == 4
    assert FALLBACK_NONE not in report.fallback_path.fallback_reasons


def test_both_classes_prove_the_legacy_answer_survived():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    for aggregate, cases in (
        (report.normal_path, 2),
        (report.fallback_path, 4),
    ):
        assert aggregate.cases_with_identical_results == cases
        assert aggregate.cases_with_preserved_payloads == cases
        assert aggregate.dropped_identities == 0
        assert aggregate.added_identities == 0
        assert aggregate.cases_with_order_change == 0

    assert report.normal_path.legacy_result_count == 4
    assert report.fallback_path.legacy_result_count == 4


def test_graph_material_is_counted_in_the_normal_class_only():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    assert report.normal_path.graph_assisted_results == 3
    assert report.normal_path.graph_node_count > 0
    assert report.normal_path.unmapped_results == 1

    assert report.fallback_path.graph_assisted_results == 0
    assert report.fallback_path.graph_node_count == 0


def test_the_stale_exposure_control_is_negative_by_default():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    assert report.normal_path.stale_measured_cases == 2
    assert report.normal_path.stale_node_count == 0
    assert report.normal_path.cases_with_stale_exposure == 0


def test_the_stale_exposure_control_fires_when_the_status_filter_is_widened():
    """A metric that cannot fire is not a measurement."""
    widened = _settings(
        filters=TraversalFilters(scope=_scope(), as_of=T1, node_statuses=[])
    )

    report = evaluate_separated_corpus(_corpus(), settings=widened)

    assert report.normal_path.cases_with_stale_exposure == 2
    assert report.normal_path.stale_node_count == 2
    assert SUPERSEDED_CLAIM_ID in report.cases[0].measurement.stale_node_ids
    # The fallback class has no graph material, so it exposes nothing either way.
    assert report.fallback_path.stale_node_count == 0


def test_the_provenance_denominator_counts_only_cases_that_supplied_one():
    corpus = [
        _normal(_normal_case()),
        _normal(
            _normal_case(
                case_id="normal_without_provenance_truth",
                expected_provenance_node_ids=None,
            )
        ),
    ]

    report = evaluate_separated_corpus(corpus, settings=_settings())

    assert report.normal_path.case_count == 2
    assert report.normal_path.provenance_measured_cases == 1
    assert report.normal_path.provenance_expected == 2
    assert report.normal_path.provenance_covered == 2
    assert report.normal_path.provenance_coverage_rate == 1.0
    assert NOT_MEASURED_PROVENANCE in report.normal_path.warnings


def test_the_provenance_gap_control_is_visible_in_the_normal_class():
    """A positive control: an expected lineage ID nothing resolves is missing."""
    absent_evidence = "evidence_batch_k_0101_0002"
    corpus = [
        _normal(
            _normal_case(
                case_id="normal_with_provenance_gap",
                expected_provenance_node_ids=(*EXPECTED_PROVENANCE_IDS,
                                              absent_evidence),
            )
        )
    ]

    report = evaluate_separated_corpus(corpus, settings=_settings())

    assert report.normal_path.provenance_expected == 3
    assert report.normal_path.provenance_covered == 2
    assert report.normal_path.provenance_coverage_rate == pytest.approx(2 / 3)
    assert report.cases[0].measurement.provenance_missing_ids == [absent_evidence]


def test_each_class_summarizes_its_own_latency():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    for aggregate, expected in (
        (report.normal_path, 2),
        (report.fallback_path, 4),
    ):
        latency = aggregate.latency
        assert isinstance(latency, LatencySummary)
        assert latency.count == expected
        assert latency.min_ms >= 0.0
        assert latency.min_ms <= latency.mean_ms <= latency.max_ms
        assert latency.min_ms <= latency.p95_ms <= latency.max_ms


def test_every_per_case_measurement_is_preserved_in_corpus_order():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())

    assert [case.case_id for case in report.cases] == [
        "normal_healthy_claim",
        "normal_mixed_ranking",
        "fallback_absent_graph",
        "fallback_raising_traversal",
        "fallback_unmapped_results",
        "fallback_no_graph_material",
    ]
    assert [case.case_class for case in report.cases] == [
        CASE_CLASS_NORMAL,
        CASE_CLASS_NORMAL,
        CASE_CLASS_FALLBACK,
        CASE_CLASS_FALLBACK,
        CASE_CLASS_FALLBACK,
        CASE_CLASS_FALLBACK,
    ]
    first = report.cases[0].measurement
    assert isinstance(first, ShadowCaseMeasurement)
    assert first.status == GRAPH_STATUS_OK
    assert first.graph_node_ids == sorted(EXPECTED_CURRENT_IDS)
    assert first.provenance_coverage_rate == 1.0
    assert report.cases[2].measurement.fallback_reason == FALLBACK_GRAPH_UNAVAILABLE


def test_an_empty_corpus_measures_nothing_and_divides_by_nothing():
    report = evaluate_separated_corpus([], settings=_settings())

    assert report.case_count == 0
    assert report.cases == []
    assert report.class_counts == {CASE_CLASS_FALLBACK: 0, CASE_CLASS_NORMAL: 0}
    for aggregate in (report.normal_path, report.fallback_path):
        assert aggregate.case_count == 0
        assert aggregate.status_counts == {}
        assert aggregate.fallback_rate == 0.0
        assert aggregate.provenance_expected == 0
        assert aggregate.provenance_coverage_rate == 0.0
        assert aggregate.latency.count == 0
    assert report.warnings == []
    assert report.classification_disagreements == []


def test_a_single_class_corpus_leaves_the_other_class_empty():
    report = evaluate_separated_corpus(
        [_normal(_normal_case())], settings=_settings()
    )

    assert report.normal_path.case_count == 1
    assert report.fallback_path.case_count == 0
    assert report.fallback_path.fallback_rate == 0.0
    assert report.fallback_path.latency.count == 0


def test_the_separated_measurements_report_both_classes_with_denominators():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())
    measurements = report.separated_measurements()

    assert measurements["case_count"] == 6
    assert measurements["class_counts"] == {
        CASE_CLASS_FALLBACK: 4,
        CASE_CLASS_NORMAL: 2,
    }
    normal = measurements[CASE_CLASS_NORMAL]
    fallback = measurements[CASE_CLASS_FALLBACK]
    assert normal["provenance_expected"] == 4
    assert normal["provenance_covered"] == 4
    assert normal["fallback_rate"] == 0.0
    assert fallback["fallback_rate"] == 1.0
    assert fallback["provenance_expected"] == 8
    assert "latency" in normal and "latency" in fallback


# ---------------------------------------------------------------------------
# Measurement only: no verdict anywhere in the public surface
# ---------------------------------------------------------------------------

#: Words a measurement helper may not name: each one would be a decision this
#: batch is explicitly not allowed to make (§12 Batch K-C owns the gate).
FORBIDDEN_DECISION_WORDS = (
    "verdict",
    "passed",
    "approve",
    "accept",
    "threshold",
    "winner",
    "cutover",
    "ready",
    "gate",
    "score",
)


def _public_names(obj) -> set[str]:
    return {name for name in dir(obj) if not name.startswith("_")}


def _name_tokens(name: str) -> list[str]:
    """Split a snake_case or CamelCase name into lowercase word tokens.

    Tokens rather than substrings: ``aggregate`` contains ``gate``, and a
    substring scan would flag every aggregate in the module while saying
    nothing about whether a decision is being made.
    """
    spaced = "".join(
        f" {character}" if character.isupper() else character for character in name
    )
    return [token for token in spaced.replace("_", " ").lower().split() if token]


def _decision_offenders(names: set[str]) -> list[str]:
    return sorted(
        name
        for name in names
        if any(
            token.startswith(word)
            for token in _name_tokens(name)
            for word in FORBIDDEN_DECISION_WORDS
        )
    )


def test_no_batch_k_output_names_a_verdict_threshold_or_cutover():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())
    measurements = report.separated_measurements()

    surface = (
        _public_names(report)
        | _public_names(report.normal_path)
        | _public_names(report.cases[0])
        | set(measurements)
        | set(measurements[CASE_CLASS_NORMAL])
        | set(graph_batch_k_evaluation.__all__)
    )

    assert _decision_offenders(surface) == []


def test_the_parity_surface_names_no_verdict_either(tmp_path):
    report = _parity(tmp_path)

    surface = (
        _public_names(report)
        | _public_names(report.persistent)
        | set(report.parity_measurements())
    )

    assert _decision_offenders(surface) == []


# ---------------------------------------------------------------------------
# Bounded output (§15)
# ---------------------------------------------------------------------------

def test_no_separated_report_carries_a_memory_body_or_a_source_path():
    report = evaluate_separated_corpus(_corpus(), settings=_settings())
    serialized = report.to_json()

    assert CLAIM_BODY not in serialized
    assert SOURCE_PATH not in serialized
    assert SECTION_TITLE not in serialized
    assert BACKEND_DIAGNOSTIC_PATH not in serialized
    assert "backend diagnostic" not in serialized


def test_an_unhealthy_graph_is_a_bounded_fallback_measurement():
    corpus = [
        _fallback(
            _fallback_case("fallback_unhealthy_graph", store=_UnhealthyStore())
        )
    ]

    report = evaluate_separated_corpus(corpus, settings=_settings())

    assert report.fallback_path.status_counts == {GRAPH_STATUS_UNAVAILABLE: 1}
    assert report.fallback_path.fallback_reasons == {FALLBACK_GRAPH_UNAVAILABLE: 1}
    assert BACKEND_DIAGNOSTIC_PATH not in report.to_json()
    assert any(
        warning.startswith("unhealthy_graph:")
        for warning in report.fallback_path.warnings
    )


def test_the_backend_label_sanitizer_is_the_existing_evaluators_own():
    """Reused, not restated: one place decides what a backend may be called."""
    from layered_memory_mcp import graph_shadow_evaluation

    assert (
        graph_batch_k_evaluation._safe_backend
        is graph_shadow_evaluation._safe_backend
    )


def test_no_parity_report_carries_a_database_path_or_a_memory_body(tmp_path):
    report = _parity(tmp_path)
    serialized = report.to_json()

    assert str(tmp_path) not in serialized
    assert "graph.sqlite3" not in serialized
    assert "sqlite3" not in serialized.replace(SQLITE_GRAPH_BACKEND, "")
    assert CLAIM_BODY not in serialized
    assert SOURCE_PATH not in serialized
    assert SECTION_TITLE not in serialized


# ---------------------------------------------------------------------------
# K-B: the persistent adapter, closed and reopened
# ---------------------------------------------------------------------------

def _parity(tmp_path, name: str = "graph.sqlite3", **kwargs) -> PersistentParityReport:
    """Run the parity check with an explicitly supplied database target."""
    data = {
        "database": tmp_path / name,
        "legacy_results": [_claim_row()],
        "mode": _graph_mode(),
        "nodes": _fixture_nodes(),
        "edges": _fixture_edges(),
        "filters": _filters(),
    }
    data.update(kwargs)
    return run_persistent_parity_check(**data)


def test_the_writing_store_is_closed_and_a_new_one_is_opened(tmp_path):
    database = tmp_path / "graph.sqlite3"

    report = _parity(tmp_path)

    assert report.write_store_closed is True
    assert report.reopened is True
    assert report.database_file_present is True
    # The graph really is on disk, not in the process that wrote it.
    assert database.is_file()
    assert database.stat().st_size > 0


def test_recall_runs_against_the_persistent_adapter_not_the_fixture(tmp_path):
    report = _parity(tmp_path)

    assert report.persistent.backend == SQLITE_GRAPH_BACKEND
    assert report.reference.backend == "in_memory_fixture"
    assert report.backend_labels_bounded is True
    assert report.fixture_source == FIXTURE_SOURCE_MATERIAL


def test_the_reopened_sqlite_graph_matches_the_in_memory_reference(tmp_path):
    report = _parity(tmp_path)

    assert isinstance(report.persistent, GraphParityObservation)
    assert report.differing_fields == []
    assert report.observations_match is True

    # The comparison is only worth making if the graph actually answered.
    assert report.persistent.status == GRAPH_STATUS_OK
    assert report.persistent.graph_assisted is True
    assert report.persistent.graph_node_ids == sorted(EXPECTED_CURRENT_IDS)
    assert report.persistent.graph_edge_ids
    assert report.persistent.provenance_record_count >= 1
    assert report.persistent.graph_node_ids == report.reference.graph_node_ids
    assert report.persistent.graph_edge_ids == report.reference.graph_edge_ids


def test_the_persistent_answer_preserves_the_legacy_payloads_and_order(tmp_path):
    rows = [_claim_row(), _entity_row(), _unmapped_row()]

    report = _parity(tmp_path, legacy_results=rows)

    assert report.persistent.legacy_identities == [
        CLAIM_FILE,
        ENTITY_FILE,
        UNMAPPED_FILE,
    ]
    assert report.persistent.legacy_payloads_preserved is True
    assert report.reference.legacy_payloads_preserved is True
    assert report.persistent.unmapped_results == 1
    assert report.observations_match is True


def test_latency_is_recorded_for_both_backends_and_never_compared(tmp_path):
    report = _parity(tmp_path)

    assert "latency_ms" in PARITY_UNCOMPARED_FIELDS
    assert "latency_ms" not in PARITY_COMPARED_FIELDS
    assert report.compared_fields == list(PARITY_COMPARED_FIELDS)
    assert report.reference.latency_ms >= 0.0
    assert report.persistent.latency_ms >= 0.0
    assert "latency_ms" not in report.structural_to_json()
    assert "latency_ms" in report.to_json()


def test_the_comparison_covers_every_bounded_field_but_the_two_excluded_ones():
    """A silently narrowed comparison is how a parity claim becomes vacuous."""
    assert set(PARITY_COMPARED_FIELDS) | set(PARITY_UNCOMPARED_FIELDS) == set(
        GraphParityObservation.model_fields
    )
    assert not set(PARITY_COMPARED_FIELDS) & set(PARITY_UNCOMPARED_FIELDS)
    for required in (
        "status",
        "graph_node_ids",
        "graph_edge_ids",
        "provenance_record_count",
        "provenance_warnings",
        "legacy_identities",
        "legacy_payloads_preserved",
    ):
        assert required in PARITY_COMPARED_FIELDS


#: A node only the diverted reference store holds. It must show up as a
#: difference, or the comparison is not comparing anything.
EXTRA_ENTITY_ID = "entity_reference_only"


def test_the_comparison_fires_when_the_two_graphs_genuinely_differ(
    tmp_path, monkeypatch
):
    """A parity check that cannot report a difference is not a check.

    The persistent side is built from the supplied fixture as always; the
    reference side is diverted to a store holding one extra one-hop neighbour,
    so the two answers must disagree on exactly the semantic fields the
    comparison names.
    """

    class _DivergentReferenceStore(InMemoryGraphStore):
        def __init__(self, nodes=(), edges=()):
            extra_node = GraphNode(
                id=EXTRA_ENTITY_ID, kind=NodeKind.ENTITY,
                label="reference only", scope=_scope(), recorded_at=T0,
            )
            super().__init__(
                nodes=[*nodes, extra_node],
                edges=[
                    *edges,
                    _edge(RelationType.ABOUT, CURRENT_CLAIM_ID, EXTRA_ENTITY_ID),
                ],
            )

    monkeypatch.setattr(
        graph_batch_k_evaluation, "InMemoryGraphStore", _DivergentReferenceStore
    )

    report = _parity(tmp_path)

    assert report.observations_match is False
    assert "graph_node_ids" in report.differing_fields
    assert "graph_edge_ids" in report.differing_fields
    assert EXTRA_ENTITY_ID in report.reference.graph_node_ids
    assert EXTRA_ENTITY_ID not in report.persistent.graph_node_ids
    assert f"parity_differences:{len(report.differing_fields)}" in report.warnings


def test_the_backend_label_is_recorded_but_excluded_from_the_comparison(tmp_path):
    report = _parity(tmp_path)

    assert "backend" in PARITY_UNCOMPARED_FIELDS
    assert "backend" not in PARITY_COMPARED_FIELDS
    # The two adapters report different labels by design, and that is not drift.
    assert report.persistent.backend != report.reference.backend
    assert report.observations_match is True


def test_a_fallback_case_degrades_identically_on_both_backends(tmp_path):
    report = _parity(tmp_path, legacy_results=[_unmapped_row()])

    assert report.persistent.status == GRAPH_STATUS_UNUSED
    assert report.reference.status == GRAPH_STATUS_UNUSED
    assert report.persistent.graph_assisted is False
    assert report.persistent.graph_node_ids == []
    assert report.persistent.legacy_payloads_preserved is True
    assert report.observations_match is True
    assert report.differing_fields == []


def test_an_explicit_event_stream_projects_the_same_graph_on_both_backends(tmp_path):
    report = _parity(
        tmp_path,
        nodes=None,
        edges=None,
        source_events=_fixture_events(),
    )

    assert report.fixture_source == FIXTURE_SOURCE_EVENTS
    assert report.rebuild_errors == 0
    assert report.observations_match is True
    assert report.persistent.graph_node_ids == sorted(EXPECTED_CURRENT_IDS)
    assert report.persistent.status == GRAPH_STATUS_OK


def test_the_parity_check_leaves_both_graphs_byte_identical(tmp_path):
    database = tmp_path / "graph.sqlite3"

    report = _parity(tmp_path)

    assert report.reference_snapshot_unchanged is True
    assert report.persistent_snapshot_unchanged is True

    # And the database still holds exactly the reference graph afterwards.
    reopened = SQLiteGraphStore(database)
    try:
        assert reopened.snapshot() == _fixture_store().snapshot()
    finally:
        reopened.close()


def test_the_parity_check_reaches_only_read_operations_on_the_persistent_store(
    tmp_path,
):
    """The seam's read-only contract, verified against the real adapter."""
    database = tmp_path / "graph.sqlite3"
    writer = SQLiteGraphStore(database, nodes=_fixture_nodes(), edges=_fixture_edges())
    writer.close()

    reopened = SQLiteGraphStore(database)
    recorder = _RecordingStore(reopened)
    try:
        runtime = run_graph_assisted_recall(
            [_claim_row()], recorder, mode=_graph_mode(), filters=_filters()
        )
    finally:
        reopened.close()

    assert recorder.calls, "the seam must actually have consulted the graph"
    assert set(recorder.calls) <= READ_OPERATIONS, sorted(
        set(recorder.calls) - READ_OPERATIONS
    )
    assert runtime.status == GRAPH_STATUS_OK
    assert runtime.backend == SQLITE_GRAPH_BACKEND


def test_repeating_the_parity_check_produces_identical_structural_output(tmp_path):
    first = _parity(tmp_path, name="first.sqlite3")
    second = _parity(tmp_path, name="second.sqlite3")

    assert first.structural_to_json() == second.structural_to_json()


# ---------------------------------------------------------------------------
# K-B: explicit inputs only — no inference, no directory, no implicit mode
# ---------------------------------------------------------------------------

def test_the_parity_helper_has_no_default_database_target():
    parameters = inspect.signature(run_persistent_parity_check).parameters

    database = parameters["database"]
    assert database.kind is inspect.Parameter.KEYWORD_ONLY
    assert database.default is inspect.Parameter.empty
    assert parameters["mode"].default is inspect.Parameter.empty
    assert parameters["legacy_results"].default is inspect.Parameter.empty


def test_the_parity_helper_creates_no_directory(tmp_path):
    missing = tmp_path / "not-created"

    with pytest.raises(GraphStoreError):
        run_persistent_parity_check(
            database=missing / "graph.sqlite3",
            legacy_results=[_claim_row()],
            mode=_graph_mode(),
            nodes=_fixture_nodes(),
            edges=_fixture_edges(),
            filters=_filters(),
        )

    assert not missing.exists()


@pytest.mark.parametrize("database", [":memory:", "  :memory:  "])
def test_an_in_memory_database_target_is_refused(database, tmp_path):
    """A store that cannot be reopened cannot answer the K-B question."""
    with pytest.raises(ValueError):
        _parity(tmp_path, database=database)


@pytest.mark.parametrize("database", ["", "   "])
def test_a_blank_database_target_is_refused(database, tmp_path):
    with pytest.raises(ValueError):
        _parity(tmp_path, database=database)


def test_supplying_both_a_fixture_and_an_event_stream_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _parity(tmp_path, source_events=_fixture_events())


def test_supplying_neither_a_fixture_nor_an_event_stream_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _parity(tmp_path, nodes=None, edges=None)


def test_an_empty_but_explicit_fixture_is_a_supported_input(tmp_path):
    """Explicitly empty is not the same as unsupplied."""
    report = _parity(tmp_path, nodes=[], edges=[])

    assert report.fixture_source == FIXTURE_SOURCE_MATERIAL
    assert report.persistent.graph_node_ids == []
    assert report.observations_match is True


@pytest.mark.parametrize(
    "mode",
    [
        LEGACY_MODE_DECISION,
        GraphModeDecision(mode="shadow", shadow_enabled=True),
        GraphModeDecision(mode="graph_write", write_enabled=True),
    ],
)
def test_the_parity_helper_requires_an_explicit_recall_enabled_mode(mode, tmp_path):
    with pytest.raises(ValueError):
        _parity(tmp_path, mode=mode)


def test_the_parity_report_names_the_mode_it_actually_ran_under(tmp_path):
    report = _parity(tmp_path)

    assert report.mode == "graph_recall"
    assert report.recall_enabled is True
    assert report.persistent.mode == "graph_recall"
    assert report.reference.mode == "graph_recall"


# ---------------------------------------------------------------------------
# Legacy stays legacy: zero graph access, unchanged answer
# ---------------------------------------------------------------------------

def test_legacy_mode_reaches_no_operation_on_the_reopened_persistent_store(tmp_path):
    database = tmp_path / "graph.sqlite3"
    writer = SQLiteGraphStore(database, nodes=_fixture_nodes(), edges=_fixture_edges())
    writer.close()

    reopened = SQLiteGraphStore(database)
    recorder = _RecordingStore(reopened)
    rows = [_claim_row(), _unmapped_row()]
    try:
        runtime = run_graph_assisted_recall(
            rows, recorder, mode=LEGACY_MODE_DECISION, filters=_filters()
        )
    finally:
        reopened.close()

    assert recorder.calls == []
    assert runtime.status == GRAPH_STATUS_DISABLED
    assert runtime.backend == ""
    assert runtime.report is None
    assert runtime.graph_authoritative is False
    # The legacy answer is the legacy answer, byte for byte and in order.
    assert runtime.results == rows


# ---------------------------------------------------------------------------
# Read-only: not one write operation is reached by the evaluation
# ---------------------------------------------------------------------------

def test_separated_evaluation_reaches_only_graph_read_operations():
    recorder = _RecordingStore(_fixture_store())

    evaluate_separated_corpus(
        [_normal(_normal_case(store=recorder))], settings=_settings()
    )

    assert recorder.calls, "the evaluation must actually have consulted the graph"
    assert set(recorder.calls) <= READ_OPERATIONS, sorted(
        set(recorder.calls) - READ_OPERATIONS
    )


def test_separated_evaluation_leaves_the_graph_byte_identical():
    inner = _fixture_store()
    before = inner.snapshot()

    evaluate_separated_corpus(
        [
            _normal(_normal_case(store=_RecordingStore(inner))),
            _normal(_normal_mixed_case(store=_RecordingStore(inner))),
        ],
        settings=_settings(),
    )

    assert inner.snapshot() == before


# ---------------------------------------------------------------------------
# Determinism of the structural answer
# ---------------------------------------------------------------------------

def test_repeating_the_separated_corpus_produces_identical_structural_output():
    first = evaluate_separated_corpus(_corpus(), settings=_settings())
    second = evaluate_separated_corpus(_corpus(), settings=_settings())

    assert first.structural_to_json() == second.structural_to_json()
    assert "latency_ms" not in first.structural_to_json()
    assert "latency" in first.to_json()


def test_the_declared_class_order_does_not_change_either_aggregate():
    forward = _corpus()
    shuffled = [forward[2], forward[0], forward[5], forward[1], forward[4], forward[3]]

    first = evaluate_separated_corpus(forward, settings=_settings())
    second = evaluate_separated_corpus(shuffled, settings=_settings())

    assert (
        first.normal_path.model_dump(mode="json", exclude={"latency"})
        == second.normal_path.model_dump(mode="json", exclude={"latency"})
    )
    assert (
        first.fallback_path.model_dump(
            mode="json", exclude={"latency", "case_ids"}
        )
        == second.fallback_path.model_dump(
            mode="json", exclude={"latency", "case_ids"}
        )
    )


def test_the_evaluator_never_mutates_the_classified_cases_it_was_given():
    corpus = _corpus()
    before = deepcopy([item.case.legacy_results for item in corpus])

    evaluate_separated_corpus(corpus, settings=_settings())

    assert [item.case.legacy_results for item in corpus] == before
    assert [item.case_class for item in corpus] == [
        CASE_CLASS_NORMAL,
        CASE_CLASS_NORMAL,
        CASE_CLASS_FALLBACK,
        CASE_CLASS_FALLBACK,
        CASE_CLASS_FALLBACK,
        CASE_CLASS_FALLBACK,
    ]


# ---------------------------------------------------------------------------
# Explicit inputs only: nothing ambient, nothing discovered
# ---------------------------------------------------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "copy",
    "dataclasses",
    "json",
    "pathlib",
    "time",
    "typing",
    "pydantic",
    ".graph_fixture_store",
    ".graph_models",
    ".graph_recall",
    ".graph_runtime",
    ".graph_shadow_evaluation",
    ".graph_sqlite_store",
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


def test_the_batch_k_module_imports_nothing_outside_the_graph_boundary():
    declared = _declared_imports(Path(graph_batch_k_evaluation.__file__))

    assert declared <= _ALLOWED_IMPORTS, sorted(declared - _ALLOWED_IMPORTS)
    # Named explicitly: these are the modules a measurement helper must not
    # reach, because reaching them is how it would find real memory data.
    for forbidden in ("config", "server", "injector", "recall", "storage"):
        assert not any(name.endswith(forbidden) for name in declared)


def test_the_separated_evaluation_reads_no_environment_variable(monkeypatch):
    import os

    def _refuse(*args, **kwargs):
        raise AssertionError("batch K evaluation must not read the environment")

    monkeypatch.setattr(os.environ, "get", _refuse)

    evaluate_separated_corpus(_corpus(), settings=_settings())


def test_the_separated_evaluation_opens_no_file(monkeypatch):
    import builtins
    import pathlib

    def _refuse(*args, **kwargs):
        raise AssertionError("batch K evaluation must not open a file")

    monkeypatch.setattr(builtins, "open", _refuse)
    monkeypatch.setattr(pathlib.Path, "open", _refuse)

    evaluate_separated_corpus(_corpus(), settings=_settings())

"""v3.6.0 Batch J — measured shadow evaluation of graph-assisted recall.

Why this file exists: ARCHITECTURE_v2.md §12 Batch J is the *measurement* batch.
Batches B-I proved the graph seams behave; what is unproven is whether running
graph-assisted recall beside the legacy answer is worth switching on. That is a
question about numbers — stale exposure, provenance coverage, fallback rate,
latency — and a number nobody measured is not an acceptance input.

These tests pin the properties that make the measurement trustworthy:

  * **It measures against fixture truth, never against a guess.** Stale exposure
    and provenance coverage are scored against explicitly supplied expected-ID
    sets; with no ground truth supplied, the evaluator says "not measured"
    instead of inventing a denominator.
  * **Legacy survives, and that survival is itself measured.** Every case
    records the identity/order/count comparison from ``compare_recall_results``
    plus whether the legacy payloads came back byte-identical.
  * **Degradation is visible, never silent.** A missing, unhealthy or raising
    graph becomes a fallback status with a bounded label — never an exception, a
    backend diagnostic, a host path or a memory body.
  * **Nothing is decided.** The evaluator reports acceptance *inputs*. There is
    no verdict, no threshold and no approval anywhere in its output.
  * **It is read-only.** Not one graph write operation is reached, even when the
    injected store implements them.
  * **It is deterministic apart from the clock.** Repeating a corpus produces
    byte-identical structural output; latency is measured, bounded and excluded
    from that comparison.

Fixture data is neutral technical vocabulary: a retrieval index, its rebuild
cadence and the build session that recorded it. Stores are in-memory fixtures.
No host path, no production configuration and no user memory data is read.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from layered_memory_mcp import graph_shadow_evaluation
from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphEdge,
    GraphNode,
    GraphScope,
    MemoryClass,
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
    MAX_GRAPH_RECALL_HOPS,
)
from layered_memory_mcp.graph_shadow_evaluation import (
    FALLBACK_GRAPH_UNAVAILABLE,
    FALLBACK_GRAPH_UNUSED,
    FALLBACK_NONE,
    FALLBACK_NO_GRAPH_MATERIAL,
    NOT_MEASURED_PROVENANCE,
    NOT_MEASURED_STALE,
    LatencySummary,
    ShadowCaseMeasurement,
    ShadowEvaluationCase,
    ShadowEvaluationReport,
    ShadowEvaluationSettings,
    evaluate_shadow_case,
    evaluate_shadow_corpus,
)
from layered_memory_mcp.graph_store import GraphHealth, TraversalFilters

# --- neutral fixture vocabulary ---------------------------------------------

T0 = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)

NAMESPACE = "shadow_eval_fixture"
OTHER_NAMESPACE = "shadow_eval_other"

EPISODE_ID = "episode_eval_0101"
EVIDENCE_ID = "evidence_eval_0101_0001"
ENTITY_ID = "entity_retrieval_index"
CURRENT_CLAIM_ID = "claim_rebuild_nightly"
SUPERSEDED_CLAIM_ID = "claim_rebuild_weekly"
FUTURE_CLAIM_ID = "claim_rebuild_hourly"
OUT_OF_SCOPE_CLAIM_ID = "claim_rebuild_other_namespace"

CLAIM_FILE = "retrieval.md"
ENTITY_FILE = "indexing.md"
UNMAPPED_FILE = "observability.md"

#: A body string that must never reach evaluation output.
CLAIM_BODY = "The retrieval index rebuild runs nightly."
#: A source path that must never reach evaluation output.
SOURCE_PATH = "knowledge/retrieval.md"

#: The node IDs the healthy fixture considers current and in scope at ``T1``.
EXPECTED_CURRENT_IDS = (CURRENT_CLAIM_ID, ENTITY_ID, EVIDENCE_ID, EPISODE_ID)
#: The lineage the healthy fixture expects graph assistance to resolve.
EXPECTED_PROVENANCE_IDS = (EVIDENCE_ID, EPISODE_ID)


def _scope(namespace: str | None = NAMESPACE) -> GraphScope:
    return GraphScope(namespace=namespace)


def _source_ref(**kwargs) -> SourceRef:
    data = {
        "episode_id": EPISODE_ID,
        "evidence_id": EVIDENCE_ID,
        "source_file": SOURCE_PATH,
        "source_section": "Index maintenance",
    }
    data.update(kwargs)
    return SourceRef(**data)


def _edge(relation: RelationType, source: str, target: str, **kwargs) -> GraphEdge:
    data = {
        "id": make_edge_id(relation, source, target),
        "relation_type": relation,
        "source_id": source,
        "target_id": target,
        "recorded_at": T0,
    }
    if relation in (RelationType.SUPPORTS, RelationType.SUPERSEDES):
        data["evidence_refs"] = [_source_ref()]
    data.update(kwargs)
    return GraphEdge(**data)


def _fixture_store() -> InMemoryGraphStore:
    """One current claim with evidence, plus three nodes that must stay hidden.

    The three are hidden by three *different* hard filters, so a single
    regression cannot pass the stale-exposure assertion by accident:
    ``superseded`` by lifecycle status, ``hourly`` by the ``as_of`` validity
    window, and the other-namespace claim by scope.
    """
    nodes = [
        GraphNode(id=EPISODE_ID, kind=NodeKind.EPISODE, label="build session 0101",
                  scope=_scope(), recorded_at=T0),
        GraphNode(id=EVIDENCE_ID, kind=NodeKind.EVIDENCE,
                  label="retrieval index maintenance",
                  content=CLAIM_BODY, scope=_scope(), recorded_at=T0),
        GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, label="retrieval index",
                  scope=_scope(), recorded_at=T0),
        GraphNode(id=CURRENT_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Retrieval index rebuild cadence", content=CLAIM_BODY,
                  memory_class=MemoryClass.FACT, scope=_scope(),
                  recorded_at=T1, valid_from=T1, source_refs=[_source_ref()]),
        GraphNode(id=SUPERSEDED_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Retrieval index rebuild cadence",
                  content="The retrieval index rebuild runs weekly.",
                  memory_class=MemoryClass.FACT, status=NodeStatus.SUPERSEDED,
                  scope=_scope(), recorded_at=T0, valid_from=T0, valid_to=T1,
                  derivation=Derivation(method=DerivationMethod.MIGRATED,
                                        generated_at=T0)),
        GraphNode(id=FUTURE_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Retrieval index rebuild cadence",
                  content="The retrieval index rebuild runs hourly.",
                  memory_class=MemoryClass.FACT, scope=_scope(),
                  recorded_at=T2, valid_from=T2,
                  derivation=Derivation(method=DerivationMethod.EXTRACTED,
                                        generated_at=T2)),
        GraphNode(id=OUT_OF_SCOPE_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Retrieval index rebuild cadence",
                  content="A different namespace rebuilds on demand.",
                  memory_class=MemoryClass.FACT, scope=_scope(OTHER_NAMESPACE),
                  recorded_at=T0, valid_from=T0,
                  derivation=Derivation(method=DerivationMethod.EXTRACTED,
                                        generated_at=T0)),
    ]
    edges = [
        _edge(RelationType.ABOUT, CURRENT_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.ABOUT, SUPERSEDED_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.EVIDENCED_BY, CURRENT_CLAIM_ID, EVIDENCE_ID),
        _edge(RelationType.OBSERVED_IN, CURRENT_CLAIM_ID, EPISODE_ID),
        _edge(RelationType.SUPERSEDES, CURRENT_CLAIM_ID, SUPERSEDED_CLAIM_ID),
        _edge(RelationType.SUPPORTS, FUTURE_CLAIM_ID, CURRENT_CLAIM_ID),
        _edge(RelationType.SUPPORTS, OUT_OF_SCOPE_CLAIM_ID, CURRENT_CLAIM_ID),
    ]
    return InMemoryGraphStore(nodes=nodes, edges=edges)


def _claim_result(**kwargs) -> dict:
    """A legacy recall payload, shaped as ``recall`` already emits one."""
    data = {
        "file": CLAIM_FILE,
        "score": 9.5,
        "matched_sections": 2,
        "sections": [{"title": "Index maintenance", "content": CLAIM_BODY}],
        "wiki_links": [],
    }
    data.update(kwargs)
    return data


def _entity_result(**kwargs) -> dict:
    data = {
        "file": ENTITY_FILE,
        "score": 4.25,
        "matched_sections": 1,
        "sections": [],
        "wiki_links": [],
    }
    data.update(kwargs)
    return data


def _unmapped_result(**kwargs) -> dict:
    """A legacy row that has no graph identity — the normal fallback condition."""
    data = {"file": UNMAPPED_FILE, "score": 1.0, "matched_sections": 0}
    data.update(kwargs)
    return data


def _settings(**kwargs) -> ShadowEvaluationSettings:
    """Bounded recall parameters pinned to the fixture's scope and instant."""
    data = {
        "max_hops": 1,
        "filters": TraversalFilters(scope=_scope(), as_of=T1),
    }
    data.update(kwargs)
    return ShadowEvaluationSettings(**data)


def _healthy_case(**kwargs) -> ShadowEvaluationCase:
    data = {
        "case_id": "healthy_current_claim",
        "legacy_results": [_claim_result()],
        "store": _fixture_store(),
        "seed_node_ids_by_result": {CLAIM_FILE: [CURRENT_CLAIM_ID]},
        "expected_current_node_ids": EXPECTED_CURRENT_IDS,
        "expected_provenance_node_ids": EXPECTED_PROVENANCE_IDS,
    }
    data.update(kwargs)
    return ShadowEvaluationCase(**data)


# ---------------------------------------------------------------------------
# A healthy current claim: covered provenance, no stale exposure
# ---------------------------------------------------------------------------

def test_a_healthy_case_covers_provenance_and_exposes_no_stale_node():
    measurement = evaluate_shadow_case(_healthy_case(), settings=_settings())

    assert isinstance(measurement, ShadowCaseMeasurement)
    assert measurement.case_id == "healthy_current_claim"
    assert measurement.status == GRAPH_STATUS_OK
    assert measurement.fallback is False
    assert measurement.fallback_reason == FALLBACK_NONE

    # The current claim and its one-hop context came back...
    assert measurement.graph_node_ids == sorted(EXPECTED_CURRENT_IDS)
    assert measurement.graph_node_count == len(EXPECTED_CURRENT_IDS)
    assert measurement.graph_result_count == 1

    # ...and the three differently-filtered nodes did not.
    assert measurement.stale_node_ids == []
    assert measurement.stale_node_count == 0
    assert measurement.stale_exposure_measured is True

    assert measurement.provenance_expected == 2
    assert measurement.provenance_covered == 2
    assert measurement.provenance_missing_ids == []
    assert measurement.provenance_coverage_rate == 1.0


def test_the_three_filtered_nodes_are_named_and_stay_hidden():
    """The stale-exposure claim is only worth making if the nodes exist."""
    store = _fixture_store()
    assert store.get_node(SUPERSEDED_CLAIM_ID) is not None
    assert store.get_node(FUTURE_CLAIM_ID) is not None
    assert store.get_node(OUT_OF_SCOPE_CLAIM_ID) is not None

    measurement = evaluate_shadow_case(
        _healthy_case(store=store), settings=_settings()
    )

    hidden = {SUPERSEDED_CLAIM_ID, FUTURE_CLAIM_ID, OUT_OF_SCOPE_CLAIM_ID}
    assert hidden.isdisjoint(measurement.graph_node_ids)
    assert measurement.stale_node_count == 0


def test_stale_exposure_is_detected_when_the_status_filter_is_widened():
    """The metric must be able to fire, or a zero proves nothing."""
    widened = _settings(
        filters=TraversalFilters(scope=_scope(), as_of=T1, node_statuses=[])
    )

    measurement = evaluate_shadow_case(_healthy_case(), settings=widened)

    assert SUPERSEDED_CLAIM_ID in measurement.graph_node_ids
    assert measurement.stale_node_ids == [SUPERSEDED_CLAIM_ID]
    assert measurement.stale_node_count == 1
    assert measurement.stale_exposure_measured is True


def test_an_expected_node_set_narrower_than_the_answer_counts_every_extra():
    measurement = evaluate_shadow_case(
        _healthy_case(expected_current_node_ids=(CURRENT_CLAIM_ID,)),
        settings=_settings(),
    )

    assert measurement.expected_current_node_count == 1
    assert measurement.stale_node_ids == sorted(
        {ENTITY_ID, EVIDENCE_ID, EPISODE_ID}
    )
    assert measurement.stale_node_count == 3


def test_without_ground_truth_stale_exposure_is_unmeasured_not_clean():
    measurement = evaluate_shadow_case(
        _healthy_case(expected_current_node_ids=None), settings=_settings()
    )

    assert measurement.stale_exposure_measured is False
    assert measurement.stale_node_count == 0
    assert measurement.expected_current_node_count == 0
    assert NOT_MEASURED_STALE in measurement.warnings


# ---------------------------------------------------------------------------
# Evidence / provenance coverage (§7.3 step 5)
# ---------------------------------------------------------------------------

MISSING_EVIDENCE_ID = "evidence_eval_0101_0002"


def _unresolved_provenance_store() -> InMemoryGraphStore:
    """A claim whose evidence reference names a node the graph does not hold.

    No ``EVIDENCED_BY`` edge, so the graph stays *healthy* — the gap is in the
    lineage, which is exactly the condition the coverage metric must not round
    up to "covered".
    """
    nodes = [
        GraphNode(id=EPISODE_ID, kind=NodeKind.EPISODE, label="build session 0101",
                  scope=_scope(), recorded_at=T0),
        GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, label="retrieval index",
                  scope=_scope(), recorded_at=T0),
        GraphNode(id=CURRENT_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Retrieval index rebuild cadence", content=CLAIM_BODY,
                  memory_class=MemoryClass.FACT, scope=_scope(),
                  recorded_at=T1, valid_from=T1,
                  source_refs=[_source_ref(evidence_id=MISSING_EVIDENCE_ID)]),
    ]
    edges = [
        _edge(RelationType.ABOUT, CURRENT_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.OBSERVED_IN, CURRENT_CLAIM_ID, EPISODE_ID),
    ]
    return InMemoryGraphStore(nodes=nodes, edges=edges)


def test_provenance_the_graph_could_not_resolve_is_not_counted_as_covered():
    case = _healthy_case(
        case_id="unresolved_evidence",
        store=_unresolved_provenance_store(),
        expected_current_node_ids=(CURRENT_CLAIM_ID, ENTITY_ID, EPISODE_ID),
        expected_provenance_node_ids=(MISSING_EVIDENCE_ID, EPISODE_ID),
    )

    measurement = evaluate_shadow_case(case, settings=_settings())

    assert measurement.provenance_coverage_measured is True
    assert measurement.provenance_expected == 2
    assert measurement.provenance_covered == 1
    assert measurement.provenance_missing_ids == [MISSING_EVIDENCE_ID]
    assert measurement.provenance_coverage_rate == 0.5
    assert measurement.stale_node_count == 0


def test_an_expected_provenance_id_nothing_references_is_missing():
    case = _healthy_case(
        expected_provenance_node_ids=(
            EVIDENCE_ID,
            EPISODE_ID,
            MISSING_EVIDENCE_ID,
        )
    )

    measurement = evaluate_shadow_case(case, settings=_settings())

    assert measurement.provenance_expected == 3
    assert measurement.provenance_covered == 2
    assert measurement.provenance_missing_ids == [MISSING_EVIDENCE_ID]
    assert measurement.provenance_coverage_rate == pytest.approx(2 / 3)


def test_without_ground_truth_provenance_coverage_is_unmeasured():
    measurement = evaluate_shadow_case(
        _healthy_case(expected_provenance_node_ids=None), settings=_settings()
    )

    assert measurement.provenance_coverage_measured is False
    assert measurement.provenance_expected == 0
    assert measurement.provenance_covered == 0
    assert measurement.provenance_coverage_rate == 0.0
    assert NOT_MEASURED_PROVENANCE in measurement.warnings
    # The records were still seen; only the scoring was declined.
    assert measurement.provenance_records == 1


# ---------------------------------------------------------------------------
# Legacy identity, order and payload survival
# ---------------------------------------------------------------------------

def _mixed_case(**kwargs) -> ShadowEvaluationCase:
    """Two mapped rows and one row with no graph identity, in ranking order."""
    data = {
        "case_id": "mixed_ranking",
        "legacy_results": [_claim_result(), _entity_result(), _unmapped_result()],
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


def test_no_legacy_identity_is_dropped_added_or_reordered():
    measurement = evaluate_shadow_case(_mixed_case(), settings=_settings())

    assert measurement.legacy_result_count == 3
    assert measurement.comparison.old_count == 3
    assert measurement.comparison.new_count == 3
    assert measurement.comparison.dropped_identities == []
    assert measurement.comparison.added_identities == []
    assert measurement.comparison.order_preserved is True
    assert measurement.comparison.identical is True


def test_added_graph_material_is_reported_without_breaking_identity():
    measurement = evaluate_shadow_case(_mixed_case(), settings=_settings())

    assert measurement.comparison.identical is True
    assert any(
        difference.startswith("graph_material_added:")
        for difference in measurement.comparison.differences
    )


def test_the_legacy_payloads_come_back_byte_identical():
    case = _mixed_case()
    measurement = evaluate_shadow_case(case, settings=_settings())

    assert measurement.legacy_payloads_preserved is True


def test_the_case_keeps_its_own_copy_of_the_legacy_payloads():
    results = [_claim_result()]
    case = _healthy_case(legacy_results=results)
    results[0]["score"] = -1.0

    measurement = evaluate_shadow_case(case, settings=_settings())

    assert case.legacy_results[0]["score"] == 9.5
    assert measurement.legacy_payloads_preserved is True


# ---------------------------------------------------------------------------
# Unmapped results: counted, never invented
# ---------------------------------------------------------------------------

def test_an_unmapped_result_is_counted_without_inventing_a_graph_identity():
    measurement = evaluate_shadow_case(_mixed_case(), settings=_settings())

    assert measurement.mapped_results == 2
    assert measurement.unmapped_results == 1
    assert measurement.graph_result_count == 2
    # Nothing was derived from the unmapped row's filename.
    assert not any(UNMAPPED_FILE in node_id for node_id in measurement.graph_node_ids)
    assert measurement.fallback is False


def test_a_corpus_row_with_no_mapping_at_all_is_an_unused_graph_fallback():
    case = ShadowEvaluationCase(
        case_id="no_mapping_at_all",
        legacy_results=[_unmapped_result()],
        store=_fixture_store(),
        expected_current_node_ids=(),
        expected_provenance_node_ids=(),
    )

    measurement = evaluate_shadow_case(case, settings=_settings())

    assert measurement.status == GRAPH_STATUS_UNUSED
    assert measurement.fallback is True
    assert measurement.fallback_reason == FALLBACK_GRAPH_UNUSED
    assert measurement.graph_node_ids == []
    assert measurement.unmapped_results == 1
    assert measurement.legacy_payloads_preserved is True


def test_a_mapped_seed_the_graph_does_not_hold_yields_no_graph_material():
    case = _healthy_case(
        case_id="seed_not_in_graph",
        seed_node_ids_by_result={CLAIM_FILE: ["claim_not_in_this_graph"]},
    )

    measurement = evaluate_shadow_case(case, settings=_settings())

    assert measurement.status == GRAPH_STATUS_PARTIAL
    assert measurement.graph_node_ids == []
    assert measurement.fallback is True
    assert measurement.fallback_reason == FALLBACK_NO_GRAPH_MATERIAL
    assert measurement.mapped_results == 1
    assert measurement.legacy_payloads_preserved is True


def test_a_case_with_no_legacy_results_measures_an_unused_graph():
    case = ShadowEvaluationCase(
        case_id="empty_legacy_answer",
        legacy_results=[],
        store=_fixture_store(),
        expected_current_node_ids=(),
        expected_provenance_node_ids=(),
    )

    measurement = evaluate_shadow_case(case, settings=_settings())

    assert measurement.legacy_result_count == 0
    assert measurement.status == GRAPH_STATUS_UNUSED
    assert measurement.fallback_reason == FALLBACK_GRAPH_UNUSED
    assert measurement.comparison.identical is True


# ---------------------------------------------------------------------------
# A missing, unhealthy or raising graph: a status, never an exception
# ---------------------------------------------------------------------------

#: A store diagnostic that names a path. It must never reach the measurement.
BACKEND_DIAGNOSTIC_PATH = "/tmp/not-a-real-fixture-path/graph.db"


class _RecordingStore:
    """A full store that records which operations the evaluator reached.

    It implements the write operations too: the point is that evaluation never
    calls them even though it could.
    """

    def __init__(self, inner: InMemoryGraphStore | None = None):
        self.inner = inner or _fixture_store()
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
        raise AssertionError("shadow evaluation must not write to the graph")

    def append_edge(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("shadow evaluation must not write to the graph")

    def rebuild(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("shadow evaluation must not write to the graph")


class _UnhealthyStore(_RecordingStore):
    def health(self, *args, **kwargs):
        self.calls.append("health")
        return GraphHealth(
            backend="in_memory_fixture",
            errors=[f"ledger_unreadable:{BACKEND_DIAGNOSTIC_PATH}"],
        )


class _RaisingTraversalStore(_RecordingStore):
    def traverse(self, *args, **kwargs):
        self.calls.append("traverse")
        raise RuntimeError(f"backend diagnostic naming {BACKEND_DIAGNOSTIC_PATH}")


def test_a_missing_graph_is_a_visible_fallback_not_an_error():
    measurement = evaluate_shadow_case(
        _healthy_case(case_id="no_store", store=None), settings=_settings()
    )

    assert measurement.status == GRAPH_STATUS_UNAVAILABLE
    assert measurement.fallback is True
    assert measurement.fallback_reason == FALLBACK_GRAPH_UNAVAILABLE
    assert measurement.backend == ""
    assert measurement.graph_node_ids == []
    assert measurement.legacy_payloads_preserved is True
    assert "graph_unavailable:no_store" in measurement.warnings


def test_an_unhealthy_graph_is_a_fallback_and_leaks_no_diagnostic():
    store = _UnhealthyStore()

    measurement = evaluate_shadow_case(
        _healthy_case(case_id="unhealthy_graph", store=store), settings=_settings()
    )

    assert measurement.status == GRAPH_STATUS_UNAVAILABLE
    assert measurement.fallback_reason == FALLBACK_GRAPH_UNAVAILABLE
    assert "traverse" not in store.calls
    assert BACKEND_DIAGNOSTIC_PATH not in measurement.to_json()
    assert any(
        warning.startswith("unhealthy_graph:") for warning in measurement.warnings
    )


def test_a_raising_traversal_is_a_fallback_and_leaks_no_exception_message():
    store = _RaisingTraversalStore()

    measurement = evaluate_shadow_case(
        _healthy_case(case_id="raising_graph", store=store), settings=_settings()
    )

    assert measurement.status == GRAPH_STATUS_UNAVAILABLE
    assert measurement.fallback_reason == FALLBACK_GRAPH_UNAVAILABLE
    assert "graph_traversal_failed:RuntimeError" in measurement.warnings
    assert BACKEND_DIAGNOSTIC_PATH not in measurement.to_json()
    assert "backend diagnostic" not in measurement.to_json()


def test_no_measurement_carries_a_memory_body_or_a_source_path():
    report = evaluate_shadow_corpus(_corpus(), settings=_settings())
    serialized = report.to_json()

    assert CLAIM_BODY not in serialized
    assert SOURCE_PATH not in serialized
    assert BACKEND_DIAGNOSTIC_PATH not in serialized
    assert "Index maintenance" not in serialized


# ---------------------------------------------------------------------------
# Bounded backend labels and bounded warning parsing
#
# Two things the evaluator takes from a seam verbatim, and must not: a backend
# name, which an unknown store may report as a connection string or an on-disk
# location, and a provenance warning, whose trailing field is a node *kind* in
# two of the six shapes the store emits. The expected values below are written
# as literals on purpose — they are the wire values an acceptance review and a
# privacy check read, so pinning them to a module constant would let a rename
# move them silently.
# ---------------------------------------------------------------------------

#: Backend strings a store could hand back that name an internal location.
#: None of them may reach a measurement, in whole or in part.
PATH_SHAPED_BACKENDS = (
    "/root/.local/share/layered-memory/graph.db",
    "sqlite:////root/.local/share/layered-memory/graph.db",
    "C:\\Users\\someone\\AppData\\layered-memory\\graph.db",
)


class _ReportedBackendStore(_RecordingStore):
    """A healthy fixture store that reports an arbitrary backend name."""

    def __init__(self, backend: str, inner: InMemoryGraphStore | None = None):
        super().__init__(inner)
        self.reported_backend = backend

    def health(self, *args, **kwargs):
        self.calls.append("health")
        return GraphHealth(backend=self.reported_backend)


@pytest.mark.parametrize("backend", PATH_SHAPED_BACKENDS)
def test_an_unknown_backend_name_is_bounded_and_leaks_no_internal_path(backend):
    measurement = evaluate_shadow_case(
        _healthy_case(
            case_id="path_shaped_backend", store=_ReportedBackendStore(backend)
        ),
        settings=_settings(),
    )

    assert measurement.backend == "unknown"
    assert "/" not in measurement.backend
    assert "\\" not in measurement.backend
    assert backend not in measurement.to_json()
    # Not even a fragment of the path survives.
    assert "graph.db" not in measurement.to_json()
    assert "layered-memory" not in measurement.to_json()

    # The two defined implementation labels stay stable and readable...
    for defined in ("in_memory_fixture", "sqlite_embedded"):
        named = evaluate_shadow_case(
            _healthy_case(
                case_id="defined_backend", store=_ReportedBackendStore(defined)
            ),
            settings=_settings(),
        )
        assert named.backend == defined

    # ...and no graph at all still reports no backend, not "unknown".
    absent = evaluate_shadow_case(
        _healthy_case(case_id="no_store", store=None), settings=_settings()
    )
    assert absent.backend == ""


#: A node whose own ID spells a :class:`NodeKind` value. ``episode`` is both a
#: legal identifier and the kind label an ``unexpected_*_kind`` warning ends in.
KIND_SHAPED_EPISODE_ID = "episode"

#: Stored as an Episode but referenced as *evidence*, so ``get_provenance``
#: reports ``unexpected_evidence_kind:<this id>:episode``.
MISKINDED_EVIDENCE_ID = "episode_eval_0202"


def _kind_collision_store() -> InMemoryGraphStore:
    """A claim with one good episode reference and one mis-kinded evidence one.

    No edges: the traversal returns the seed claim alone, so the only thing
    under test is how the claim's provenance warning is read.
    """
    nodes = [
        GraphNode(id=KIND_SHAPED_EPISODE_ID, kind=NodeKind.EPISODE,
                  label="build session 0202", scope=_scope(), recorded_at=T0),
        GraphNode(id=MISKINDED_EVIDENCE_ID, kind=NodeKind.EPISODE,
                  label="build session 0203", scope=_scope(), recorded_at=T0),
        GraphNode(id=CURRENT_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Retrieval index rebuild cadence", content=CLAIM_BODY,
                  memory_class=MemoryClass.FACT, scope=_scope(),
                  recorded_at=T1, valid_from=T1,
                  source_refs=[_source_ref(
                      episode_id=KIND_SHAPED_EPISODE_ID,
                      evidence_id=MISKINDED_EVIDENCE_ID)]),
    ]
    return InMemoryGraphStore(nodes=nodes, edges=[])


def test_a_warning_kind_fragment_is_not_read_as_a_flagged_node_id():
    store = _kind_collision_store()

    # The premise: the store flags the mis-kinded reference, and the warning's
    # trailing field is the kind ``episode`` — which is also a stored node ID.
    record = store.get_provenance(CURRENT_CLAIM_ID)
    assert record.warnings == [
        f"unexpected_evidence_kind:{MISKINDED_EVIDENCE_ID}:"
        f"{NodeKind.EPISODE.value}"
    ]
    assert KIND_SHAPED_EPISODE_ID in record.episode_ids

    measurement = evaluate_shadow_case(
        _healthy_case(
            case_id="kind_fragment_collision",
            store=store,
            expected_current_node_ids=(CURRENT_CLAIM_ID,),
            expected_provenance_node_ids=(KIND_SHAPED_EPISODE_ID,),
        ),
        settings=_settings(),
    )

    # The episode the graph *did* resolve is covered: only the node ID the
    # warning named is deducted, never the kind it ends with.
    assert measurement.provenance_coverage_measured is True
    assert measurement.provenance_expected == 1
    assert measurement.provenance_covered == 1
    assert measurement.provenance_missing_ids == []
    assert measurement.provenance_coverage_rate == 1.0

    # The gap is still visible, and nothing stale was exposed.
    assert f"provenance_unresolved:{CURRENT_CLAIM_ID}" in measurement.warnings
    assert measurement.stale_node_count == 0


# ---------------------------------------------------------------------------
# Partial and truncated answers are not clean answers
# ---------------------------------------------------------------------------

def test_a_truncated_expansion_is_reported_as_partial_and_truncated():
    truncating = _settings(
        filters=TraversalFilters(scope=_scope(), as_of=T1, max_nodes=1)
    )

    measurement = evaluate_shadow_case(_healthy_case(), settings=truncating)

    assert measurement.status == GRAPH_STATUS_PARTIAL
    assert measurement.truncated is True
    assert measurement.graph_node_ids == [CURRENT_CLAIM_ID]
    assert "truncated:max_nodes" in measurement.warnings
    assert measurement.fallback is False


def test_a_top_n_limited_corpus_row_is_partial_and_keeps_every_result():
    measurement = evaluate_shadow_case(
        _mixed_case(), settings=_settings(top_n=1)
    )

    assert measurement.status == GRAPH_STATUS_PARTIAL
    assert measurement.graph_result_count == 1
    assert measurement.legacy_result_count == 3
    assert measurement.comparison.identical is True
    assert "graph_expansion_limited:1" in measurement.warnings


def test_an_unresolved_provenance_case_is_partial_rather_than_ok():
    case = _healthy_case(
        case_id="unresolved_evidence",
        store=_unresolved_provenance_store(),
        expected_current_node_ids=(CURRENT_CLAIM_ID, ENTITY_ID, EPISODE_ID),
        expected_provenance_node_ids=(MISSING_EVIDENCE_ID, EPISODE_ID),
    )

    measurement = evaluate_shadow_case(case, settings=_settings())

    assert measurement.status == GRAPH_STATUS_PARTIAL
    assert f"provenance_unresolved:{CURRENT_CLAIM_ID}" in measurement.warnings


# ---------------------------------------------------------------------------
# Bounded parameters: refused, never clamped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("max_hops", [-1, MAX_GRAPH_RECALL_HOPS + 1, 5])
def test_an_out_of_range_hop_budget_is_refused_not_clamped(max_hops):
    with pytest.raises(ValueError):
        ShadowEvaluationSettings(max_hops=max_hops)


@pytest.mark.parametrize("top_n", [0, -3])
def test_an_out_of_range_top_n_is_refused_not_clamped(top_n):
    with pytest.raises(ValueError):
        ShadowEvaluationSettings(top_n=top_n)


def test_the_hop_budget_is_used_as_given_up_to_the_seam_limit():
    measurement = evaluate_shadow_case(
        _healthy_case(), settings=_settings(max_hops=MAX_GRAPH_RECALL_HOPS)
    )

    assert measurement.max_hops == MAX_GRAPH_RECALL_HOPS


def test_a_zero_hop_budget_returns_the_seed_node_only():
    measurement = evaluate_shadow_case(
        _healthy_case(), settings=_settings(max_hops=0)
    )

    assert measurement.max_hops == 0
    assert measurement.graph_node_ids == [CURRENT_CLAIM_ID]


# ---------------------------------------------------------------------------
# Read-only: not one write operation is reached
# ---------------------------------------------------------------------------

_READ_OPERATIONS = {"health", "traverse", "get_provenance", "find_conflicts"}


def test_evaluation_reaches_only_graph_read_operations():
    store = _RecordingStore()

    evaluate_shadow_case(_healthy_case(store=store), settings=_settings())

    assert store.calls, "the evaluation must actually have consulted the graph"
    assert set(store.calls) <= _READ_OPERATIONS, sorted(
        set(store.calls) - _READ_OPERATIONS
    )


def test_evaluation_leaves_the_graph_byte_identical():
    inner = _fixture_store()
    before = inner.snapshot()
    store = _RecordingStore(inner)

    evaluate_shadow_corpus(
        [_healthy_case(store=store), _mixed_case(store=store)],
        settings=_settings(),
    )

    assert inner.snapshot() == before


# ---------------------------------------------------------------------------
# The corpus: counts, rates and an empty answer
# ---------------------------------------------------------------------------

def _corpus() -> list[ShadowEvaluationCase]:
    """A fixed, isolated corpus covering ok, partial and both fallback shapes."""
    return [
        _healthy_case(),
        _mixed_case(),
        _healthy_case(case_id="no_store", store=None),
        _healthy_case(case_id="raising_graph", store=_RaisingTraversalStore()),
        ShadowEvaluationCase(
            case_id="no_mapping_at_all",
            legacy_results=[_unmapped_result()],
            store=_fixture_store(),
            expected_current_node_ids=(),
            expected_provenance_node_ids=(),
        ),
    ]


def test_the_corpus_report_counts_cases_and_statuses():
    report = evaluate_shadow_corpus(_corpus(), settings=_settings())

    assert isinstance(report, ShadowEvaluationReport)
    assert report.case_count == 5
    assert len(report.cases) == 5
    assert [case.case_id for case in report.cases] == [
        "healthy_current_claim",
        "mixed_ranking",
        "no_store",
        "raising_graph",
        "no_mapping_at_all",
    ]
    assert sum(report.status_counts.values()) == 5
    assert report.status_counts[GRAPH_STATUS_OK] == 1
    assert report.status_counts[GRAPH_STATUS_PARTIAL] == 1
    assert report.status_counts[GRAPH_STATUS_UNAVAILABLE] == 2
    assert report.status_counts[GRAPH_STATUS_UNUSED] == 1


def test_the_corpus_report_states_the_fallback_rate_and_its_reasons():
    report = evaluate_shadow_corpus(_corpus(), settings=_settings())

    assert report.fallback_cases == 3
    assert report.fallback_rate == pytest.approx(3 / 5)
    assert report.fallback_reasons == {
        FALLBACK_GRAPH_UNAVAILABLE: 2,
        FALLBACK_GRAPH_UNUSED: 1,
    }


def test_the_corpus_report_proves_every_legacy_answer_survived():
    report = evaluate_shadow_corpus(_corpus(), settings=_settings())

    assert report.dropped_identities == 0
    assert report.added_identities == 0
    assert report.cases_with_order_change == 0
    assert report.cases_with_identical_results == 5
    assert report.cases_with_preserved_payloads == 5
    assert report.legacy_result_count == 7


def test_the_corpus_report_aggregates_stale_and_provenance_coverage():
    report = evaluate_shadow_corpus(_corpus(), settings=_settings())

    assert report.stale_measured_cases == 5
    assert report.stale_node_count == 0
    assert report.cases_with_stale_exposure == 0

    # Four cases expect the evidence/episode pair, one expects nothing. The two
    # graph outages cover none of it — a fallback is not quietly full coverage.
    assert report.provenance_measured_cases == 5
    assert report.provenance_expected == 8
    assert report.provenance_covered == 4
    assert report.provenance_coverage_rate == 0.5


def test_an_empty_corpus_measures_nothing_and_divides_by_nothing():
    report = evaluate_shadow_corpus([], settings=_settings())

    assert report.case_count == 0
    assert report.cases == []
    assert report.status_counts == {}
    assert report.fallback_cases == 0
    assert report.fallback_rate == 0.0
    assert report.fallback_reasons == {}
    assert report.provenance_expected == 0
    assert report.provenance_coverage_rate == 0.0
    assert report.latency.count == 0
    assert report.warnings == []


def test_the_report_states_acceptance_inputs_and_claims_no_verdict():
    report = evaluate_shadow_corpus(_corpus(), settings=_settings())
    inputs = report.acceptance_inputs()

    assert inputs["case_count"] == 5
    assert inputs["fallback_cases"] == 3
    assert inputs["provenance_coverage_rate"] == 0.5
    assert "latency" in inputs

    # No verdict, no threshold, no approval — anywhere in the public surface.
    forbidden = ("verdict", "passed", "approved", "accept_", "threshold", "winner")
    surface = set(dir(report)) | set(inputs)
    assert not [
        name
        for name in surface
        if not name.startswith("_")
        and name != "acceptance_inputs"
        and any(word in name for word in forbidden)
    ]


# ---------------------------------------------------------------------------
# Latency: measured, bounded, never asserted as a threshold
# ---------------------------------------------------------------------------

def test_every_case_reports_a_non_negative_latency():
    report = evaluate_shadow_corpus(_corpus(), settings=_settings())

    assert all(case.latency_ms >= 0.0 for case in report.cases)


def test_the_latency_summary_is_internally_consistent():
    report = evaluate_shadow_corpus(_corpus(), settings=_settings())
    latency = report.latency

    assert isinstance(latency, LatencySummary)
    assert latency.count == report.case_count
    assert latency.min_ms >= 0.0
    assert latency.min_ms <= latency.mean_ms <= latency.max_ms
    assert latency.min_ms <= latency.p95_ms <= latency.max_ms


def test_a_single_case_corpus_reports_that_one_sample_everywhere():
    report = evaluate_shadow_corpus([_healthy_case()], settings=_settings())
    only = report.cases[0].latency_ms

    assert report.latency.count == 1
    assert report.latency.min_ms == only
    assert report.latency.mean_ms == only
    assert report.latency.p95_ms == only
    assert report.latency.max_ms == only


# ---------------------------------------------------------------------------
# Determinism of the structural answer
# ---------------------------------------------------------------------------

def test_repeating_a_corpus_produces_identical_structural_output():
    first = evaluate_shadow_corpus(_corpus(), settings=_settings())
    second = evaluate_shadow_corpus(_corpus(), settings=_settings())

    assert first.structural_to_json() == second.structural_to_json()


def test_a_case_measurement_is_structurally_reproducible():
    first = evaluate_shadow_case(_healthy_case(), settings=_settings())
    second = evaluate_shadow_case(_healthy_case(), settings=_settings())

    assert first.structural_to_json() == second.structural_to_json()


def test_the_structural_answer_excludes_latency_and_the_full_answer_keeps_it():
    measurement = evaluate_shadow_case(_healthy_case(), settings=_settings())

    assert "latency_ms" not in measurement.structural_to_json()
    assert "latency_ms" in measurement.to_json()


def test_the_mapping_order_does_not_change_the_measurement():
    forward = _mixed_case(
        seed_node_ids_by_result={
            CLAIM_FILE: [CURRENT_CLAIM_ID],
            ENTITY_FILE: [ENTITY_ID],
        }
    )
    reversed_keys = _mixed_case(
        seed_node_ids_by_result={
            ENTITY_FILE: [ENTITY_ID],
            CLAIM_FILE: [CURRENT_CLAIM_ID],
        }
    )

    assert (
        evaluate_shadow_case(forward, settings=_settings()).structural_to_json()
        == evaluate_shadow_case(
            reversed_keys, settings=_settings()
        ).structural_to_json()
    )


# ---------------------------------------------------------------------------
# Explicit inputs only: nothing is inferred, discovered or ambient
# ---------------------------------------------------------------------------

def test_a_mapping_key_naming_no_result_is_refused():
    case = _healthy_case(
        seed_node_ids_by_result={"not-a-supplied-result.md": [CURRENT_CLAIM_ID]}
    )

    with pytest.raises(ValueError):
        evaluate_shadow_case(case, settings=_settings())


def test_a_blank_case_id_is_refused():
    with pytest.raises(ValueError):
        ShadowEvaluationCase(case_id="   ")


def test_a_legacy_result_that_is_not_a_mapping_is_refused():
    with pytest.raises(ValueError):
        ShadowEvaluationCase(case_id="bad_row", legacy_results=["nonsense"])


def test_the_case_normalizes_its_explicit_id_sets():
    case = _healthy_case(
        expected_current_node_ids=[f" {ENTITY_ID} ", ENTITY_ID, CURRENT_CLAIM_ID],
        expected_provenance_node_ids=[EPISODE_ID, EPISODE_ID],
    )

    assert case.expected_current_node_ids == tuple(
        sorted({ENTITY_ID, CURRENT_CLAIM_ID})
    )
    assert case.expected_provenance_node_ids == (EPISODE_ID,)


_ALLOWED_IMPORTS = {
    "__future__",
    "copy",
    "dataclasses",
    "json",
    "math",
    "time",
    "typing",
    "pydantic",
    ".graph_models",
    ".graph_recall",
    ".graph_runtime",
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


def test_the_evaluator_imports_nothing_outside_the_graph_boundary():
    declared = _declared_imports(Path(graph_shadow_evaluation.__file__))
    assert declared <= _ALLOWED_IMPORTS, sorted(declared - _ALLOWED_IMPORTS)


def test_the_evaluator_reads_no_environment_variable(monkeypatch):
    import os

    def _refuse(*args, **kwargs):
        raise AssertionError("shadow evaluation must not read the environment")

    monkeypatch.setattr(os.environ, "get", _refuse)

    evaluate_shadow_corpus(_corpus(), settings=_settings())


def test_the_evaluator_opens_no_file(monkeypatch):
    import builtins
    import pathlib

    def _refuse(*args, **kwargs):
        raise AssertionError("shadow evaluation must not open a file")

    monkeypatch.setattr(builtins, "open", _refuse)
    monkeypatch.setattr(pathlib.Path, "open", _refuse)

    evaluate_shadow_corpus(_corpus(), settings=_settings())


def test_the_evaluator_never_mutates_the_case_it_was_given():
    case = _mixed_case()
    before = deepcopy(case.legacy_results)

    evaluate_shadow_case(case, settings=_settings())

    assert case.legacy_results == before
    assert case.seed_node_ids_by_result == {
        CLAIM_FILE: (CURRENT_CLAIM_ID,),
        ENTITY_FILE: (ENTITY_ID,),
    }

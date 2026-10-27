"""v3.6.0 Batch H — the runtime seam between the old paths and the graph.

Why this file exists: ARCHITECTURE_v2.md §12 Batch H is the first *integration*
batch. The B–G modules are already proven backend-neutral; what is unproven is
the production-side adapter that decides, per configured mode, whether the graph
is consulted at all — and whether the old answer survives when it is.

These tests pin the properties that make the integration reversible:

  * **The mapping is read, never inferred.** Graph seed IDs come only from
    explicit vector-result metadata keys. A filename, a domain, a heading or a
    body never becomes a graph identity, and a result with no metadata is a
    visible, counted fallback rather than a guess.
  * **Legacy survives every mode.** The original ranked payloads come back in
    the original order — with the graph disabled, with no store, with an
    unhealthy store, with a raising traversal and with no mapping at all.
  * **The mode decides, in one place.** ``legacy`` never reaches the graph,
    ``shadow`` prepares and reports without committing anything or claiming
    authority, and only ``graph_write``/``graph`` may project an explicitly
    supplied semantic decision into an explicitly supplied store.
  * **The two write paths are reported separately.** A graph projection failure
    never rolls back, or claims to roll back, a successful L1 write.
  * **Comparison observes; it never arbitrates.** The shadow comparison records
    identity, order, count and status differences deterministically, decides
    nothing and mutates neither input.
  * **The adapter has no ambient state.** It reads no environment variable,
    opens no file and imports neither config, server, injector nor storage.

Fixture data is neutral technical vocabulary: a retrieval index, its rebuild
cadence and the build session that recorded it. No private memory bodies, no
host paths, no user memory data.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from layered_memory_mcp import graph_runtime
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
    GRAPH_SEED_IDS_KEY,
    GRAPH_STATUS_DISABLED,
    GRAPH_WRITE_FAILED,
    GRAPH_WRITE_NO_REQUEST,
    GRAPH_WRITE_NOT_CONFIGURED,
    GRAPH_WRITE_NOT_ENABLED,
    GRAPH_WRITE_PREPARED,
    GRAPH_WRITE_PROJECTED,
    GraphModeDecision,
    GraphRecallRuntimeReport,
    GraphWriteOutcome,
    RecallComparison,
    SeedMapping,
    build_seed_mapping,
    compare_recall_results,
    graph_recall_payload,
    graph_write_payload,
    run_graph_assisted_recall,
    run_semantic_graph_write,
)
from layered_memory_mcp.graph_store import GraphHealth, RebuildReport, TraversalFilters
from layered_memory_mcp.graph_write import RelationAssertion, SemanticWriteRequest

# --- neutral fixture vocabulary ---------------------------------------------

T0 = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)

NAMESPACE = "retrieval_fixture"

EPISODE_ID = "episode_build_0101"
EVIDENCE_ID = "evidence_build_0101_0001"
ENTITY_ID = "entity_retrieval_index"
ACTIVE_CLAIM_ID = "claim_index_rebuild_nightly"
SUPERSEDED_CLAIM_ID = "claim_index_rebuild_weekly"

CLAIM_FILE = "retrieval.md"
ENTITY_FILE = "indexing.md"
UNMAPPED_FILE = "observability.md"


def _claim_result(**kwargs) -> dict:
    """A legacy recall payload, shaped as ``recall`` already emits one."""
    data = {
        "file": CLAIM_FILE,
        "score": 9.5,
        "matched_sections": 2,
        "sections": [{"title": "Index maintenance", "content": "# Index"}],
        "wiki_links": [],
        "metadata": {GRAPH_SEED_ID_KEY: ACTIVE_CLAIM_ID},
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
        "metadata": {GRAPH_SEED_IDS_KEY: [ENTITY_ID]},
    }
    data.update(kwargs)
    return data


def _unmapped_result(**kwargs) -> dict:
    """A legacy row that predates graph IDs — the normal fallback condition."""
    data = {"file": UNMAPPED_FILE, "score": 1.0, "matched_sections": 0}
    data.update(kwargs)
    return data


# ---------------------------------------------------------------------------
# Seed mapping: explicit metadata only (§3.3)
# ---------------------------------------------------------------------------

def test_a_scalar_graph_node_id_becomes_a_one_item_list():
    mapping = build_seed_mapping([_claim_result()])

    assert isinstance(mapping, SeedMapping)
    assert mapping.seed_node_ids_by_result == {CLAIM_FILE: [ACTIVE_CLAIM_ID]}
    assert mapping.mapped_results == 1
    assert mapping.unmapped_results == 0
    assert mapping.warnings == []


def test_a_list_of_graph_node_ids_is_deduplicated_in_first_occurrence_order():
    result = _entity_result(
        metadata={
            GRAPH_SEED_IDS_KEY: [
                SUPERSEDED_CLAIM_ID,
                ACTIVE_CLAIM_ID,
                SUPERSEDED_CLAIM_ID,
                f"  {ENTITY_ID}  ",
            ]
        }
    )
    mapping = build_seed_mapping([result])

    assert mapping.seed_node_ids_by_result == {
        ENTITY_FILE: [SUPERSEDED_CLAIM_ID, ACTIVE_CLAIM_ID, ENTITY_ID]
    }


def test_both_metadata_keys_merge_with_the_scalar_first():
    result = _claim_result(
        metadata={
            GRAPH_SEED_ID_KEY: ACTIVE_CLAIM_ID,
            GRAPH_SEED_IDS_KEY: [ENTITY_ID, ACTIVE_CLAIM_ID],
        }
    )
    mapping = build_seed_mapping([result])

    assert mapping.seed_node_ids_by_result == {
        CLAIM_FILE: [ACTIVE_CLAIM_ID, ENTITY_ID]
    }


def test_the_mapping_key_is_the_result_file_identity_not_a_title():
    result = _claim_result(title="Retrieval index rebuild cadence")
    mapping = build_seed_mapping([result])

    assert list(mapping.seed_node_ids_by_result) == [CLAIM_FILE]


def test_absent_metadata_maps_nothing_and_is_counted():
    mapping = build_seed_mapping([_unmapped_result()])

    assert mapping.seed_node_ids_by_result == {}
    assert mapping.mapped_results == 0
    assert mapping.unmapped_results == 1
    assert "graph_metadata_absent:1" in mapping.warnings
    assert "unmapped_results:1" in mapping.warnings


def test_non_dict_metadata_maps_nothing_and_is_counted():
    mapping = build_seed_mapping([_claim_result(metadata="graph_node_id=x")])

    assert mapping.seed_node_ids_by_result == {}
    assert "graph_metadata_not_a_mapping:1" in mapping.warnings


def test_metadata_without_any_graph_key_maps_nothing_and_is_counted():
    mapping = build_seed_mapping([_claim_result(metadata={"domain": "retrieval"})])

    assert mapping.seed_node_ids_by_result == {}
    assert "graph_metadata_without_ids:1" in mapping.warnings


@pytest.mark.parametrize(
    "value",
    ["", "   ", "node\nid", 7, 7.5, True, None, {"id": ACTIVE_CLAIM_ID}, [ENTITY_ID]],
)
def test_a_non_identifier_graph_id_is_rejected_never_coerced(value):
    mapping = build_seed_mapping(
        [_claim_result(metadata={GRAPH_SEED_IDS_KEY: [value, ACTIVE_CLAIM_ID]})]
    )

    assert mapping.seed_node_ids_by_result == {CLAIM_FILE: [ACTIVE_CLAIM_ID]}
    assert "graph_ids_rejected:1" in mapping.warnings


def test_a_non_sequence_under_the_list_key_is_rejected():
    mapping = build_seed_mapping(
        [_claim_result(metadata={GRAPH_SEED_IDS_KEY: ACTIVE_CLAIM_ID})]
    )

    assert mapping.seed_node_ids_by_result == {}
    assert "graph_ids_rejected:1" in mapping.warnings


def test_a_result_without_a_usable_identity_is_counted_not_mapped():
    mapping = build_seed_mapping(
        [{"score": 1.0, "metadata": {GRAPH_SEED_ID_KEY: ACTIVE_CLAIM_ID}}, "nonsense"]
    )

    assert mapping.seed_node_ids_by_result == {}
    assert "results_without_identity:2" in mapping.warnings


def test_the_mapping_never_mutates_the_results_it_read():
    result = _claim_result()
    before = repr(result)
    build_seed_mapping([result])
    assert repr(result) == before


def test_the_mapping_is_deterministic_and_independent_of_key_order():
    forward = build_seed_mapping(
        [_claim_result(metadata={
            GRAPH_SEED_ID_KEY: ACTIVE_CLAIM_ID,
            GRAPH_SEED_IDS_KEY: [ENTITY_ID],
        })]
    )
    reversed_keys = build_seed_mapping(
        [_claim_result(metadata={
            GRAPH_SEED_IDS_KEY: [ENTITY_ID],
            GRAPH_SEED_ID_KEY: ACTIVE_CLAIM_ID,
        })]
    )
    assert forward.to_json() == reversed_keys.to_json()


def test_an_empty_result_list_maps_nothing_without_warning():
    mapping = build_seed_mapping([])

    assert mapping.seed_node_ids_by_result == {}
    assert mapping.warnings == []
    assert mapping.mapped_results == 0


def test_a_duplicate_identity_keeps_one_merged_entry():
    """Two rows of one file are one graph identity, not an ambiguous mapping."""
    mapping = build_seed_mapping(
        [
            _claim_result(metadata={GRAPH_SEED_ID_KEY: ACTIVE_CLAIM_ID}),
            _claim_result(metadata={GRAPH_SEED_ID_KEY: ENTITY_ID}),
        ]
    )

    assert mapping.seed_node_ids_by_result == {
        CLAIM_FILE: [ACTIVE_CLAIM_ID, ENTITY_ID]
    }
    assert "duplicate_result_identities:1" in mapping.warnings


# ---------------------------------------------------------------------------
# The mode decision (§3.1): one switch, no scattered string comparisons
# ---------------------------------------------------------------------------

def test_a_shadow_decision_may_not_also_be_authoritative():
    with pytest.raises(ValueError):
        GraphModeDecision(
            mode="shadow", recall_enabled=True, shadow_enabled=True
        )


def test_the_legacy_decision_observes_nothing():
    decision = GraphModeDecision(mode="legacy")

    assert decision.recall_enabled is False
    assert decision.write_enabled is False
    assert decision.shadow_enabled is False
    assert decision.observes_recall is False
    assert decision.prepares_write is False


@pytest.mark.parametrize(
    "mode,observes_recall,prepares_write",
    [
        ("legacy", False, False),
        ("shadow", True, True),
        ("graph_recall", True, False),
        ("graph_write", False, True),
        ("graph", True, True),
    ],
)
def test_from_config_reads_the_predicates_not_the_mode_string(
    tmp_path, mode, observes_recall, prepares_write
):
    from layered_memory_mcp.config import MemoryConfig

    config = MemoryConfig(home=str(tmp_path / "graph-home"), graph_mode=mode)
    decision = GraphModeDecision.from_config(config)

    assert decision.mode == mode
    assert decision.recall_enabled is config.graph_recall_enabled
    assert decision.write_enabled is config.graph_write_enabled
    assert decision.shadow_enabled is config.graph_shadow_enabled
    assert decision.observes_recall is observes_recall
    assert decision.prepares_write is prepares_write


def test_the_disabled_status_is_distinguishable_from_an_outage():
    assert GRAPH_STATUS_DISABLED != GRAPH_STATUS_UNAVAILABLE


# ---------------------------------------------------------------------------
# Graph-assisted recall: the mode matrix and the fallbacks (§3.2)
# ---------------------------------------------------------------------------

LEGACY = GraphModeDecision(mode="legacy")
SHADOW = GraphModeDecision(mode="shadow", shadow_enabled=True)
RECALL_MODE = GraphModeDecision(mode="graph_recall", recall_enabled=True)
WRITE_MODE = GraphModeDecision(mode="graph_write", write_enabled=True)
GRAPH_MODE = GraphModeDecision(mode="graph", recall_enabled=True, write_enabled=True)


def _scope(namespace: str | None = NAMESPACE) -> GraphScope:
    return GraphScope(namespace=namespace)


def _source_ref(**kwargs) -> SourceRef:
    data = {
        "episode_id": EPISODE_ID,
        "evidence_id": EVIDENCE_ID,
        "source_file": "knowledge/retrieval.md",
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
    """A small healthy graph: one entity, one current claim, one superseded."""
    nodes = [
        GraphNode(id=EPISODE_ID, kind=NodeKind.EPISODE, label="build session 0101",
                  recorded_at=T0),
        GraphNode(id=EVIDENCE_ID, kind=NodeKind.EVIDENCE,
                  label="retrieval.md / Index maintenance",
                  content="The index rebuild job is scheduled nightly.",
                  recorded_at=T0),
        GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, label="retrieval index",
                  scope=_scope(), recorded_at=T0),
        GraphNode(id=ACTIVE_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Retrieval index rebuild cadence",
                  content="The retrieval index rebuild runs nightly.",
                  memory_class=MemoryClass.FACT, scope=_scope(),
                  recorded_at=T1, valid_from=T1, source_refs=[_source_ref()]),
        GraphNode(id=SUPERSEDED_CLAIM_ID, kind=NodeKind.CLAIM,
                  label="Retrieval index rebuild cadence",
                  content="The retrieval index rebuild runs weekly.",
                  memory_class=MemoryClass.FACT, status=NodeStatus.SUPERSEDED,
                  scope=_scope(), recorded_at=T0, valid_from=T0, valid_to=T1,
                  derivation=Derivation(method=DerivationMethod.MIGRATED,
                                        generated_at=T0)),
    ]
    edges = [
        _edge(RelationType.ABOUT, ACTIVE_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.ABOUT, SUPERSEDED_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.SUPERSEDES, ACTIVE_CLAIM_ID, SUPERSEDED_CLAIM_ID),
        _edge(RelationType.EVIDENCED_BY, ACTIVE_CLAIM_ID, EVIDENCE_ID),
        _edge(RelationType.OBSERVED_IN, ACTIVE_CLAIM_ID, EPISODE_ID),
    ]
    return InMemoryGraphStore(nodes=nodes, edges=edges)


class _RecordingStore:
    """A full store that records which operations the adapter reached.

    It implements the write operations too: the point is that the recall
    adapter never calls them even when it could.
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
        raise AssertionError("graph-assisted recall must not write")

    def append_edge(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("graph-assisted recall must not write")

    def rebuild(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("graph-assisted recall must not write")


class _UnhealthyStore(_RecordingStore):
    def health(self, *args, **kwargs):
        self.calls.append("health")
        # ``healthy`` is derived from ``errors`` — a backend cannot claim a
        # green light and a broken ledger at once.
        return GraphHealth(
            backend="in_memory_fixture",
            errors=["ledger_unreadable:/tmp/not-a-real-path/graph.db"],
        )


class _RaisingTraversalStore(_RecordingStore):
    def traverse(self, *args, **kwargs):
        self.calls.append("traverse")
        raise RuntimeError("backend diagnostic naming /tmp/not-a-real-path/graph.db")


def _mapped_results() -> list[dict]:
    """Two mapped rows and one legacy row with no graph identity yet."""
    return [
        _claim_result(metadata={GRAPH_SEED_ID_KEY: ACTIVE_CLAIM_ID}),
        _entity_result(metadata={GRAPH_SEED_IDS_KEY: [ENTITY_ID]}),
        _unmapped_result(),
    ]


def _identities(results) -> list[str]:
    return [result["file"] for result in results]


# -- legacy mode --------------------------------------------------------------

def test_legacy_mode_reaches_no_graph_operation_at_all():
    store = _RecordingStore()
    results = _mapped_results()

    report = run_graph_assisted_recall(results, store, mode=LEGACY)

    assert isinstance(report, GraphRecallRuntimeReport)
    assert report.status == GRAPH_STATUS_DISABLED
    assert store.calls == []
    assert report.report is None
    assert report.graph_authoritative is False
    assert report.graph_assisted is False
    assert report.results == results
    assert report.seed_mapping.seed_node_ids_by_result == {}


def test_legacy_mode_returns_copies_so_a_caller_cannot_reach_back():
    results = _mapped_results()
    report = run_graph_assisted_recall(results, _RecordingStore(), mode=LEGACY)

    report.results[0]["score"] = -1.0
    assert results[0]["score"] == 9.5


def test_legacy_mode_needs_no_store_and_no_mode_argument():
    results = _mapped_results()
    report = run_graph_assisted_recall(results)

    assert report.status == GRAPH_STATUS_DISABLED
    assert _identities(report.results) == _identities(results)


# -- graph-enabled modes ------------------------------------------------------

def test_graph_recall_mode_expands_through_the_explicit_mapping():
    store = _RecordingStore()
    results = _mapped_results()

    report = run_graph_assisted_recall(results, store, mode=RECALL_MODE)

    assert report.status in (GRAPH_STATUS_OK, GRAPH_STATUS_PARTIAL)
    assert report.graph_authoritative is True
    assert report.graph_assisted is True
    assert "traverse" in store.calls
    assert report.seed_mapping.seed_node_ids_by_result == {
        CLAIM_FILE: [ACTIVE_CLAIM_ID],
        ENTITY_FILE: [ENTITY_ID],
    }
    # The mapped claim carries graph material; the unmapped row does not.
    by_file = {r.file: r for r in report.report.results}
    assert ACTIVE_CLAIM_ID in [n.id for n in by_file[CLAIM_FILE].graph_nodes]
    assert by_file[UNMAPPED_FILE].graph_nodes == []


def test_the_legacy_order_and_payload_survive_graph_expansion():
    results = _mapped_results()
    expected = deepcopy(results)

    report = run_graph_assisted_recall(results, _RecordingStore(), mode=GRAPH_MODE)

    assert report.results == expected
    assert _identities(report.results) == [CLAIM_FILE, ENTITY_FILE, UNMAPPED_FILE]
    assert len(report.results) == len(results)


def test_a_superseded_claim_is_filtered_out_by_the_store_not_re_added():
    report = run_graph_assisted_recall(
        [_claim_result(metadata={GRAPH_SEED_IDS_KEY: [ENTITY_ID]})],
        _fixture_store(),
        mode=RECALL_MODE,
    )
    node_ids = [n.id for n in report.report.results[0].graph_nodes]
    assert ACTIVE_CLAIM_ID in node_ids
    assert SUPERSEDED_CLAIM_ID not in node_ids


def test_shadow_mode_runs_the_graph_but_claims_no_authority():
    store = _RecordingStore()
    results = _mapped_results()

    report = run_graph_assisted_recall(results, store, mode=SHADOW)

    assert "traverse" in store.calls
    assert report.graph_authoritative is False
    assert report.graph_assisted is True
    assert report.results == results


def test_write_only_mode_leaves_recall_legacy():
    store = _RecordingStore()
    report = run_graph_assisted_recall(_mapped_results(), store, mode=WRITE_MODE)

    assert report.status == GRAPH_STATUS_DISABLED
    assert store.calls == []


def test_no_write_operation_is_ever_reached():
    store = _RecordingStore()
    run_graph_assisted_recall(_mapped_results(), store, mode=GRAPH_MODE)

    assert not {"create_node", "append_edge", "rebuild"} & set(store.calls)


def test_max_hops_and_filters_are_passed_through_not_reinvented():
    report = run_graph_assisted_recall(
        _mapped_results(),
        _fixture_store(),
        mode=RECALL_MODE,
        max_hops=2,
        filters=TraversalFilters(scope=_scope()),
        top_n=1,
    )
    assert report.report.max_hops == 2
    assert any(w.startswith("graph_expansion_limited:") for w in report.warnings)


# -- the fallbacks ------------------------------------------------------------

def test_no_store_preserves_the_legacy_answer():
    results = _mapped_results()
    report = run_graph_assisted_recall(results, None, mode=GRAPH_MODE)

    assert report.status == GRAPH_STATUS_UNAVAILABLE
    assert report.graph_authoritative is False
    assert report.results == results
    assert "graph_unavailable:no_store" in report.warnings


def test_an_unhealthy_graph_preserves_the_legacy_answer_without_leaking_a_path():
    results = _mapped_results()
    report = run_graph_assisted_recall(results, _UnhealthyStore(), mode=RECALL_MODE)

    assert report.status == GRAPH_STATUS_UNAVAILABLE
    assert report.graph_authoritative is False
    assert report.results == results
    assert "graph_unavailable:unhealthy" in report.warnings
    assert "not-a-real-path" not in " ".join(report.warnings)


def test_a_raising_traversal_preserves_the_legacy_answer_and_never_raises():
    results = _mapped_results()
    report = run_graph_assisted_recall(
        results, _RaisingTraversalStore(), mode=RECALL_MODE
    )

    assert report.status == GRAPH_STATUS_UNAVAILABLE
    assert report.results == results
    assert "graph_traversal_failed:RuntimeError" in report.warnings
    assert "not-a-real-path" not in " ".join(report.warnings)


def test_rows_with_no_graph_identity_are_a_visible_fallback_not_a_guess():
    results = [_unmapped_result(), _claim_result(metadata={"domain": "retrieval"})]
    report = run_graph_assisted_recall(results, _fixture_store(), mode=RECALL_MODE)

    assert report.status == GRAPH_STATUS_UNUSED
    assert report.graph_assisted is False
    assert report.results == results
    assert "graph_metadata_absent:1" in report.warnings
    assert "graph_metadata_without_ids:1" in report.warnings
    assert "no_graph_seeds" in report.warnings


def test_malformed_metadata_does_not_stop_the_other_rows_from_expanding():
    results = [
        _claim_result(metadata="graph_node_id=claim"),
        _entity_result(metadata={GRAPH_SEED_IDS_KEY: [ENTITY_ID]}),
    ]
    report = run_graph_assisted_recall(results, _fixture_store(), mode=RECALL_MODE)

    assert report.graph_assisted is True
    assert report.results == results
    assert "graph_metadata_not_a_mapping:1" in report.warnings


def test_an_empty_legacy_answer_stays_empty():
    report = run_graph_assisted_recall([], _fixture_store(), mode=GRAPH_MODE)

    assert report.results == []
    assert report.status in (GRAPH_STATUS_UNUSED, GRAPH_STATUS_UNAVAILABLE)


def test_a_non_mapping_result_is_an_explicit_caller_error():
    with pytest.raises(ValueError):
        run_graph_assisted_recall(["not a result"], _fixture_store(), mode=RECALL_MODE)


def test_two_results_sharing_one_identity_stay_an_explicit_caller_error():
    """The graph seam's own contract is not softened by the adapter."""
    with pytest.raises(ValueError):
        run_graph_assisted_recall(
            [_claim_result(), _claim_result()], _fixture_store(), mode=RECALL_MODE
        )


# -- the MCP-facing payload ---------------------------------------------------

def test_the_payload_is_additive_bounded_and_carries_no_memory_body():
    import json

    report = run_graph_assisted_recall(
        _mapped_results(), _fixture_store(), mode=GRAPH_MODE
    )
    payload = graph_recall_payload(report)
    blob = json.dumps(payload, ensure_ascii=False)

    assert payload["mode"] == "graph"
    assert payload["status"] == report.status
    assert payload["authoritative"] is True
    assert payload["unmapped_results"] == 1
    assert ACTIVE_CLAIM_ID in payload["results"][0]["graph_nodes"]
    # Identities, IDs, counts and statuses only — never a remembered statement.
    assert "rebuild runs nightly" not in blob
    assert "Index maintenance" not in blob
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        graph_recall_payload(report), sort_keys=True
    )


def test_the_payload_of_a_legacy_report_says_disabled_without_graph_fields():
    payload = graph_recall_payload(
        run_graph_assisted_recall(_mapped_results(), _fixture_store(), mode=LEGACY)
    )

    assert payload["status"] == GRAPH_STATUS_DISABLED
    assert payload["results"] == []
    assert payload["authoritative"] is False



# ---------------------------------------------------------------------------
# Semantic graph writes: explicit decision only (§3.4)
# ---------------------------------------------------------------------------

OLD_CLAIM_ID = "claim_index_rebuild_monthly"
NEW_CLAIM_ID = ACTIVE_CLAIM_ID


def _event(kind: MemoryEventKind, event_id: str, occurred_at, **kwargs) -> MemoryEvent:
    data = {
        "id": event_id,
        "kind": kind,
        "occurred_at": occurred_at,
        "scope": _scope(),
    }
    data.update(kwargs)
    return MemoryEvent(**data)


def _old_claim() -> GraphNode:
    return GraphNode(
        id=OLD_CLAIM_ID,
        kind=NodeKind.CLAIM,
        label="Retrieval index rebuild cadence",
        content="The retrieval index rebuild runs monthly.",
        memory_class=MemoryClass.FACT,
        scope=_scope(),
        recorded_at=T0,
        source_refs=[_source_ref()],
    )


def _new_claim() -> GraphNode:
    return GraphNode(
        id=NEW_CLAIM_ID,
        kind=NodeKind.CLAIM,
        label="Retrieval index rebuild cadence",
        content="The retrieval index rebuild runs nightly.",
        memory_class=MemoryClass.FACT,
        scope=_scope(),
        recorded_at=T1,
        source_refs=[_source_ref()],
    )


def _source_events() -> list[MemoryEvent]:
    """The caller's own append-only stream — the one and only ledger."""
    return [
        _event(MemoryEventKind.INJECT, "source_0001", T0,
               node=GraphNode(id=EPISODE_ID, kind=NodeKind.EPISODE,
                              label="build session 0101", scope=_scope(),
                              recorded_at=T0)),
        _event(MemoryEventKind.INJECT, "source_0002", T0,
               node=GraphNode(id=EVIDENCE_ID, kind=NodeKind.EVIDENCE,
                              label="retrieval.md / Index maintenance",
                              content="The rebuild job is scheduled monthly.",
                              scope=_scope(), recorded_at=T0)),
        _event(MemoryEventKind.INJECT, "source_0003", T0,
               node=GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY,
                              label="retrieval index", scope=_scope(),
                              recorded_at=T0)),
        _event(MemoryEventKind.INJECT, "source_0004", T0, node=_old_claim()),
    ]


def _supersedes() -> RelationAssertion:
    return RelationAssertion(
        relation_type=RelationType.SUPERSEDES,
        target_id=OLD_CLAIM_ID,
        evidence_refs=[_source_ref()],
    )


def _request(**kwargs) -> SemanticWriteRequest:
    data = {
        "node": _new_claim(),
        "occurred_at": T1,
        "supersedes": [_supersedes()],
    }
    data.update(kwargs)
    return SemanticWriteRequest(**data)


class _WriteRecordingStore:
    """A store that records every operation, including the write ones."""

    def __init__(self):
        self.inner = InMemoryGraphStore()
        self.calls: list[str] = []

    def __getattr__(self, name):
        inner_attr = getattr(self.inner, name)
        if not callable(inner_attr):
            return inner_attr

        def _recorded(*args, **kwargs):
            self.calls.append(name)
            return inner_attr(*args, **kwargs)

        return _recorded


class _RaisingRebuildStore:
    def __init__(self):
        self.calls: list[str] = []

    def rebuild(self, *args, **kwargs):
        self.calls.append("rebuild")
        raise RuntimeError("backend diagnostic naming /tmp/not-a-real-path/graph.db")


class _ReportingErrorsStore:
    def __init__(self):
        self.calls: list[str] = []

    def rebuild(self, source_events, *args, **kwargs):
        self.calls.append("rebuild")
        return RebuildReport(
            events_read=len(source_events),
            errors=["dangling_reference:claim_index_rebuild_unknown"],
        )


# -- legacy and shadow --------------------------------------------------------

def test_legacy_mode_never_reaches_the_graph_write_surface():
    store = _WriteRecordingStore()

    outcome = run_semantic_graph_write(
        mode=LEGACY,
        request=_request(),
        store=store,
        source_events=_source_events(),
    )

    assert isinstance(outcome, GraphWriteOutcome)
    assert outcome.status == GRAPH_WRITE_NOT_ENABLED
    assert store.calls == []
    assert outcome.plan is None
    assert outcome.projection is None
    assert outcome.committed is False
    assert outcome.legacy_write_ok is True


def test_shadow_mode_plans_without_committing_anything():
    store = _WriteRecordingStore()

    outcome = run_semantic_graph_write(
        mode=SHADOW,
        request=_request(),
        store=store,
        source_events=_source_events(),
    )

    assert outcome.status == GRAPH_WRITE_PREPARED
    assert outcome.plan is not None
    assert outcome.projection is None
    assert outcome.committed is False
    assert store.calls == []
    assert "graph_write_shadow_only" in outcome.warnings
    # The §8.2 pair is planned together: an edge and a lifecycle transition.
    assert outcome.plan.superseded_ids == [OLD_CLAIM_ID]
    assert outcome.plan.transition_event_ids


def test_shadow_planning_is_deterministic():
    first = run_semantic_graph_write(
        mode=SHADOW, request=_request(), source_events=_source_events()
    )
    second = run_semantic_graph_write(
        mode=SHADOW, request=_request(), source_events=_source_events()
    )
    assert first.plan.to_json() == second.plan.to_json()


# -- nothing happens without an explicit decision -----------------------------

@pytest.mark.parametrize("mode", [SHADOW, WRITE_MODE, GRAPH_MODE])
def test_no_request_means_no_graph_write_in_any_mode(mode):
    store = _WriteRecordingStore()

    outcome = run_semantic_graph_write(
        mode=mode, store=store, source_events=_source_events()
    )

    assert outcome.status == GRAPH_WRITE_NO_REQUEST
    assert outcome.plan is None
    assert store.calls == []
    assert outcome.committed is False


def test_a_successful_legacy_write_alone_commits_nothing_to_the_graph():
    """§3.4: no graph write may happen merely because inject_knowledge worked."""
    store = _WriteRecordingStore()

    outcome = run_semantic_graph_write(
        mode=GRAPH_MODE, store=store, legacy_write_ok=True
    )

    assert outcome.status == GRAPH_WRITE_NO_REQUEST
    assert store.calls == []
    assert outcome.legacy_write_ok is True


def test_an_enabled_mode_without_a_store_is_visibly_not_configured():
    outcome = run_semantic_graph_write(
        mode=WRITE_MODE, request=_request(), source_events=_source_events()
    )

    assert outcome.status == GRAPH_WRITE_NOT_CONFIGURED
    assert outcome.projection is None
    assert outcome.committed is False
    assert "graph_write_not_configured:no_store" in outcome.warnings


# -- the enabled path ---------------------------------------------------------

def test_graph_write_mode_projects_the_explicit_decision():
    store = InMemoryGraphStore()

    outcome = run_semantic_graph_write(
        mode=WRITE_MODE,
        request=_request(),
        store=store,
        source_events=_source_events(),
    )

    assert outcome.status == GRAPH_WRITE_PROJECTED
    assert outcome.committed is True
    assert outcome.projection is not None
    assert outcome.projection.report.errors == []
    # Supersession is two effects: the new claim is active, the old one history.
    assert store.get_node(NEW_CLAIM_ID).status is NodeStatus.ACTIVE
    assert store.get_node(OLD_CLAIM_ID).status is NodeStatus.SUPERSEDED
    # Nothing is deleted — the old claim keeps its payload.
    assert store.get_node(OLD_CLAIM_ID).content.endswith("runs monthly.")


def test_projection_uses_only_rebuild_never_a_second_write_surface():
    store = _WriteRecordingStore()

    run_semantic_graph_write(
        mode=GRAPH_MODE,
        request=_request(),
        store=store,
        source_events=_source_events(),
    )

    assert set(store.calls) == {"rebuild"}


def test_projecting_the_same_decision_twice_is_a_no_op():
    store = InMemoryGraphStore()
    first = run_semantic_graph_write(
        mode=GRAPH_MODE, request=_request(), store=store,
        source_events=_source_events(),
    )
    snapshot = store.snapshot()
    second = run_semantic_graph_write(
        mode=GRAPH_MODE, request=_request(), store=store,
        source_events=_source_events(),
    )

    assert first.plan.to_json() == second.plan.to_json()
    assert store.snapshot() == snapshot


# -- the two paths are reported separately ------------------------------------

def test_a_failed_projection_never_rolls_back_a_successful_l1_write():
    store = _RaisingRebuildStore()

    outcome = run_semantic_graph_write(
        mode=GRAPH_MODE,
        request=_request(),
        store=store,
        source_events=_source_events(),
        legacy_write_ok=True,
    )

    assert outcome.status == GRAPH_WRITE_FAILED
    assert outcome.committed is False
    assert outcome.legacy_write_ok is True
    assert "graph_write_failed:RuntimeError" in outcome.warnings
    assert "not-a-real-path" not in " ".join(outcome.warnings)
    assert outcome.plan is not None


def test_a_rebuild_that_reports_errors_is_not_a_clean_write():
    outcome = run_semantic_graph_write(
        mode=GRAPH_MODE,
        request=_request(),
        store=_ReportingErrorsStore(),
        source_events=_source_events(),
    )

    assert outcome.status == GRAPH_WRITE_PROJECTED
    assert outcome.committed is False
    assert outcome.projection.ok is False
    assert "graph_write_reported_errors:1" in outcome.warnings


def test_a_failed_legacy_write_is_carried_through_not_reinterpreted():
    outcome = run_semantic_graph_write(
        mode=GRAPH_MODE,
        request=_request(),
        store=InMemoryGraphStore(),
        source_events=_source_events(),
        legacy_write_ok=False,
    )

    assert outcome.legacy_write_ok is False
    assert outcome.status == GRAPH_WRITE_PROJECTED
    assert "legacy_write_not_ok" in outcome.warnings


def test_a_planning_error_stays_an_explicit_caller_error():
    """A target the stream does not carry is a caller mistake, not a warning."""
    store = InMemoryGraphStore()
    with pytest.raises(ValueError):
        run_semantic_graph_write(
            mode=GRAPH_MODE,
            request=_request(
                supersedes=[
                    RelationAssertion(
                        relation_type=RelationType.SUPERSEDES,
                        target_id="claim_index_rebuild_unknown",
                        evidence_refs=[_source_ref()],
                    )
                ]
            ),
            store=store,
            source_events=_source_events(),
        )
    assert store.snapshot()["nodes"] == []


def test_the_write_outcome_payload_is_bounded_and_carries_no_memory_body():
    import json

    outcome = run_semantic_graph_write(
        mode=GRAPH_MODE,
        request=_request(),
        store=InMemoryGraphStore(),
        source_events=_source_events(),
    )
    payload = graph_write_payload(outcome)
    blob = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == GRAPH_WRITE_PROJECTED
    assert payload["committed"] is True
    assert payload["node_id"] == NEW_CLAIM_ID
    assert payload["superseded_ids"] == [OLD_CLAIM_ID]
    assert "runs nightly" not in blob
    assert "Index maintenance" not in blob


# ---------------------------------------------------------------------------
# Shadow comparison: observation, never arbitration (§3.5)
# ---------------------------------------------------------------------------

def _graph_report(results=None, **kwargs):
    return run_graph_assisted_recall(
        results if results is not None else _mapped_results(),
        kwargs.pop("store", _fixture_store()),
        mode=kwargs.pop("mode", RECALL_MODE),
        **kwargs,
    )


def test_identical_answers_compare_as_identical():
    old = _mapped_results()
    comparison = compare_recall_results(old, _graph_report(deepcopy(old)))

    assert isinstance(comparison, RecallComparison)
    assert comparison.identical is True
    assert comparison.order_preserved is True
    assert comparison.old_count == comparison.new_count == 3
    assert comparison.dropped_identities == []
    assert comparison.added_identities == []


def test_a_reordered_answer_is_recorded_not_judged():
    old = _mapped_results()
    reordered = [old[1], old[0], old[2]]

    comparison = compare_recall_results(old, reordered)

    assert comparison.order_preserved is False
    assert comparison.identical is False
    assert "order_changed" in comparison.differences
    assert comparison.dropped_identities == []
    assert comparison.added_identities == []


def test_added_graph_material_is_recorded_without_changing_the_identities():
    old = _mapped_results()
    report = _graph_report(deepcopy(old))

    comparison = compare_recall_results(old, report)

    assert comparison.graph_assisted_results >= 1
    assert comparison.identical is True
    assert any(d.startswith("graph_material_added:") for d in comparison.differences)


def test_a_dropped_identity_is_named():
    old = _mapped_results()
    comparison = compare_recall_results(old, old[:2])

    assert comparison.dropped_identities == [UNMAPPED_FILE]
    assert comparison.added_identities == []
    assert comparison.identical is False
    assert "identities_dropped:1" in comparison.differences
    assert "count_changed" in comparison.differences


def test_an_added_identity_is_named():
    old = _mapped_results()[:2]
    comparison = compare_recall_results(old, _mapped_results())

    assert comparison.added_identities == [UNMAPPED_FILE]
    assert comparison.dropped_identities == []
    assert "identities_added:1" in comparison.differences


def test_a_graph_outage_compares_as_an_unchanged_fallback():
    old = _mapped_results()
    report = run_graph_assisted_recall(deepcopy(old), None, mode=GRAPH_MODE)

    comparison = compare_recall_results(old, report)

    assert comparison.status == GRAPH_STATUS_UNAVAILABLE
    assert comparison.identical is True
    assert comparison.graph_assisted_results == 0
    assert comparison.differences == []


def test_comparing_against_a_legacy_report_says_disabled_and_identical():
    old = _mapped_results()
    report = run_graph_assisted_recall(deepcopy(old), _fixture_store(), mode=LEGACY)

    comparison = compare_recall_results(old, report)

    assert comparison.status == GRAPH_STATUS_DISABLED
    assert comparison.identical is True
    assert comparison.differences == []


def test_the_comparison_mutates_neither_input():
    old = _mapped_results()
    report = _graph_report(deepcopy(old))
    before_old, before_new = repr(old), report.to_json()

    compare_recall_results(old, report)

    assert repr(old) == before_old
    assert report.to_json() == before_new


def test_the_comparison_is_deterministic_and_carries_no_memory_body():
    old = _mapped_results()
    report = _graph_report(deepcopy(old))

    first = compare_recall_results(old, report)
    second = compare_recall_results(old, report)

    assert first.to_json() == second.to_json()
    blob = first.to_json()
    assert "rebuild runs nightly" not in blob
    assert "# Index" not in blob


def test_the_comparison_never_names_a_winner():
    """It records differences; deciding which answer is right is not its job."""
    assert not {
        field
        for field in RecallComparison.model_fields
        if field
        in {"correct", "winner", "preferred", "better", "chosen", "authoritative"}
    }


def test_a_row_without_an_identity_is_counted_not_silently_skipped():
    comparison = compare_recall_results(
        [{"score": 1.0}], [{"score": 1.0}]
    )

    assert "results_without_identity:2" in comparison.warnings


# ---------------------------------------------------------------------------
# Boundary: no ambient state, no production path (§6)
# ---------------------------------------------------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "copy",
    "typing",
    "pydantic",
    ".graph_models",
    ".graph_recall",
    ".graph_store",
    ".graph_write",
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


def test_the_runtime_seam_imports_nothing_outside_the_graph_boundary():
    declared = _declared_imports(Path(graph_runtime.__file__))
    assert declared <= _ALLOWED_IMPORTS, sorted(declared - _ALLOWED_IMPORTS)


def test_the_runtime_seam_never_records_a_legacy_changelog_entry():
    """Batch G's distinction must stay explicit: MemoryEvent is not a changelog.

    Asserted over the *code*, not the text: the docstring is allowed (and meant)
    to say that ``record_memory_change`` is never called.
    """
    tree = ast.parse(Path(graph_runtime.__file__).read_text(encoding="utf-8"))
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)

    assert "record_memory_change" not in referenced
    assert not {name for name in referenced if "changelog" in name}


def test_the_runtime_seam_reads_no_environment_variable(monkeypatch):
    import os

    def _refuse(*args, **kwargs):
        raise AssertionError("the runtime seam must not read the environment")

    monkeypatch.setattr(os.environ, "get", _refuse)

    run_graph_assisted_recall(_mapped_results(), _fixture_store(), mode=GRAPH_MODE)
    run_semantic_graph_write(
        mode=GRAPH_MODE,
        request=_request(),
        store=InMemoryGraphStore(),
        source_events=_source_events(),
    )


def test_the_runtime_seam_opens_no_file(monkeypatch):
    import builtins
    import pathlib

    def _refuse(*args, **kwargs):
        raise AssertionError("the runtime seam must not open any file")

    monkeypatch.setattr(builtins, "open", _refuse)
    monkeypatch.setattr(pathlib.Path, "open", _refuse)

    report = run_graph_assisted_recall(
        _mapped_results(), _fixture_store(), mode=GRAPH_MODE
    )
    compare_recall_results(_mapped_results(), report)
    run_semantic_graph_write(
        mode=GRAPH_MODE,
        request=_request(),
        store=InMemoryGraphStore(),
        source_events=_source_events(),
    )


# ---------------------------------------------------------------------------
# MCP wiring: additive in graph modes, byte-compatible in legacy
# ---------------------------------------------------------------------------

def _server_config(tmp_path, mode: str):
    """An explicit test config: tmp home, tmp knowledge dir, chosen mode."""
    from layered_memory_mcp.config import MemoryConfig

    home = tmp_path / f"server-{mode}"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / CLAIM_FILE).write_text(
        "# Retrieval\n\n## Index maintenance\n\nThe retrieval index is rebuilt.\n",
        encoding="utf-8",
    )
    (knowledge / UNMAPPED_FILE).write_text(
        "# Observability\n\n## Index dashboards\n\nThe retrieval index has a board.\n",
        encoding="utf-8",
    )
    return MemoryConfig(
        home=str(home), knowledge_dir=str(knowledge), graph_mode=mode
    )


def _recall_response(monkeypatch, tmp_path, mode: str, store=None, fake_recall=None):
    import asyncio
    import json

    from layered_memory_mcp import server

    monkeypatch.setattr(server, "_config", _server_config(tmp_path, mode))
    monkeypatch.setattr(server, "_graph_store", store)
    if fake_recall is not None:
        monkeypatch.setattr(server, "recall", fake_recall)
    monkeypatch.setattr(server, "_record_access", lambda events: None)

    return json.loads(asyncio.run(server.recall_knowledge(keyword="index")))


def test_legacy_mode_leaves_the_mcp_response_exactly_as_before(monkeypatch, tmp_path):
    response = _recall_response(monkeypatch, tmp_path, "legacy", store=_fixture_store())

    assert response["success"] is True
    assert "graph" not in response
    assert response["results"]


def test_a_graph_mode_adds_a_graph_block_without_touching_the_old_fields(
    monkeypatch, tmp_path
):
    legacy = _recall_response(monkeypatch, tmp_path, "legacy")
    enabled = _recall_response(
        monkeypatch, tmp_path, "graph_recall", store=_fixture_store()
    )

    assert enabled["results"] == legacy["results"]
    assert enabled["matched_files"] == legacy["matched_files"]
    assert enabled["graph"]["mode"] == "graph_recall"
    # Today's L1 rows carry no graph ID yet: an honest, counted fallback.
    assert enabled["graph"]["status"] == GRAPH_STATUS_UNUSED
    assert enabled["graph"]["unmapped_results"] == len(legacy["results"])


def test_the_graph_block_reports_an_outage_when_no_store_is_injected(
    monkeypatch, tmp_path
):
    response = _recall_response(monkeypatch, tmp_path, "graph", store=None)

    assert response["graph"]["status"] == GRAPH_STATUS_UNAVAILABLE
    assert response["graph"]["authoritative"] is False
    assert response["results"]


def test_explicit_metadata_reaches_the_graph_through_the_mcp_path(
    monkeypatch, tmp_path
):
    def _fake_recall(keyword, knowledge_dir, top_n, search_mode):
        return {
            "success": True,
            "keyword": keyword,
            "search_mode": search_mode,
            "total_files": 1,
            "matched_files": 1,
            "results": [_claim_result(metadata={GRAPH_SEED_ID_KEY: ENTITY_ID})],
        }

    response = _recall_response(
        monkeypatch, tmp_path, "graph_recall",
        store=_fixture_store(), fake_recall=_fake_recall,
    )

    assert response["graph"]["authoritative"] is True
    assert response["graph"]["mapped_results"] == 1
    assert response["graph"]["results"][0]["file"] == CLAIM_FILE
    assert ENTITY_ID in response["graph"]["results"][0]["graph_nodes"]
    # Still the legacy payload, untouched.
    assert response["results"][0]["score"] == 9.5


def test_shadow_mode_adds_the_comparison_and_claims_no_authority(
    monkeypatch, tmp_path
):
    response = _recall_response(
        monkeypatch, tmp_path, "shadow", store=_fixture_store()
    )

    assert response["graph"]["authoritative"] is False
    assert response["graph"]["comparison"]["identical"] is True
    assert response["graph"]["comparison"]["dropped_identities"] == []


def test_a_broken_injected_store_never_breaks_the_mcp_recall_path(
    monkeypatch, tmp_path
):
    class _NotAStore:
        pass

    response = _recall_response(
        monkeypatch, tmp_path, "graph_recall", store=_NotAStore()
    )

    assert response["success"] is True
    assert response["results"]
    assert response["graph"]["status"] == GRAPH_STATUS_UNAVAILABLE
    assert "graph_runtime_failed:ValueError" in response["graph"]["warnings"]


def test_the_graph_store_is_injected_explicitly_and_defaults_to_none():
    from layered_memory_mcp import server

    assert server._graph_store is None
    store = _fixture_store()
    try:
        server.set_graph_store(store)
        assert server._graph_store is store
    finally:
        server.set_graph_store(None)
    assert server._graph_store is None

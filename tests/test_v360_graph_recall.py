"""v3.6.0 Batch F — backend-neutral graph-assisted recall.

Why this file exists: ARCHITECTURE_v2.md §11 Phase 4 and §12 Batch F require the
seam where existing BM25/vector seeds meet the memory graph — steps 3 to 7 of
the §7.3 unified recall pipeline (bounded expansion, hard filtering, evidence
backfill, explainable result) — to be testable *before* anything touches the
default MCP recall path. Nothing here is a runtime cutover: no module under test
reads a config, a knowledge directory or a real store, and the existing recall
path is not imported.

These tests pin the properties that make such a seam safe to build on:

  * **Seeds survive.** A healthy graph is additive. A result with no graph
    mapping, a seed the filters excluded, a failed traversal and a missing
    provenance record each keep the original recall payload intact.
  * **The mapping is explicit.** Only the ``seed_node_ids_by_result`` the caller
    supplied is used — no filename scanning, no path discovery, no entity
    guessing — and the answer does not depend on that mapping's insertion order.
  * **The graph is a filter, not a search engine.** Superseded, out-of-window
    and out-of-scope nodes stay out, and the orchestrator never re-adds what the
    store's traversal contract excluded.
  * **It is honest** (§10.3). Truncation, weak-relation skips, unresolved
    provenance, unresolved conflicts, an unhealthy graph and a raising traversal
    are all *reported*; none of them is allowed to look like a clean, complete,
    graph-assisted answer.
  * **It is read-only.** The layer requires exactly four read operations, and a
    recording store proves no write operation is reached.

Fixture data is neutral technical vocabulary: a retrieval index, its rebuild
cadence and the build session that recorded it. No private memory bodies, no
credentials, no host paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

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
    GRAPH_RECALL_OPERATIONS,
    GRAPH_STATUS_OK,
    GRAPH_STATUS_PARTIAL,
    GRAPH_STATUS_UNAVAILABLE,
    GRAPH_STATUS_UNUSED,
    MAX_GRAPH_RECALL_HOPS,
    GraphRecallReport,
    RecallSeed,
    expand_recall_with_graph,
)
from layered_memory_mcp.graph_store import GraphHealth, TraversalFilters

# --- neutral fixture vocabulary ---------------------------------------------

T0 = datetime(2026, 2, 2, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)

NAMESPACE = "retrieval_fixture"
OTHER_NAMESPACE = "indexing_fixture"

EPISODE_ID = "episode_build_0007"
EVIDENCE_ID = "evidence_build_0007_0002"
MISSING_EVIDENCE_ID = "evidence_build_0007_9999"
ENTITY_ID = "entity_retrieval_index"
OTHER_ENTITY_ID = "entity_build_pipeline"
ACTIVE_CLAIM_ID = "claim_index_rebuild_nightly"
SUPERSEDED_CLAIM_ID = "claim_index_rebuild_weekly"
FUTURE_CLAIM_ID = "claim_index_rebuild_hourly"
OTHER_SCOPE_CLAIM_ID = "claim_index_shard_count"

CLAIM_FILE = "retrieval.md"
ENTITY_FILE = "indexing.md"
UNMAPPED_FILE = "observability.md"


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


def _migrated() -> Derivation:
    return Derivation(method=DerivationMethod.MIGRATED, generated_at=T0)


def _edge(relation: RelationType, source: str, target: str, **kwargs) -> GraphEdge:
    """An edge whose ID is derived from its triple, as a rebuild would build it."""
    data = {
        "id": make_edge_id(relation, source, target),
        "relation_type": relation,
        "source_id": source,
        "target_id": target,
        "recorded_at": T0,
    }
    if relation in (
        RelationType.SUPPORTS,
        RelationType.CONTRADICTS,
        RelationType.SUPERSEDES,
    ):
        data["evidence_refs"] = [_source_ref()]
    data.update(kwargs)
    return GraphEdge(**data)


def _fixture_store() -> InMemoryGraphStore:
    """A small, healthy graph: one entity with four claims about it.

    One claim is current, one is superseded, one is only valid in the future,
    and one belongs to another namespace — so a single one-hop expansion from
    the entity exercises every hard filter the store contract applies.
    """
    nodes = [
        GraphNode(
            id=EPISODE_ID,
            kind=NodeKind.EPISODE,
            label="build session 0007",
            recorded_at=T0,
        ),
        GraphNode(
            id=EVIDENCE_ID,
            kind=NodeKind.EVIDENCE,
            label="retrieval.md / Index maintenance",
            content="The index rebuild job is scheduled nightly at 02:00.",
            recorded_at=T0,
        ),
        GraphNode(
            id=ENTITY_ID,
            kind=NodeKind.ENTITY,
            label="retrieval index",
            scope=_scope(),
            recorded_at=T0,
        ),
        GraphNode(
            id=ACTIVE_CLAIM_ID,
            kind=NodeKind.CLAIM,
            label="Retrieval index rebuild cadence",
            content="The retrieval index rebuild runs nightly.",
            memory_class=MemoryClass.FACT,
            confidence=0.8,
            scope=_scope(),
            recorded_at=T1,
            valid_from=T1,
            source_refs=[_source_ref()],
        ),
        GraphNode(
            id=SUPERSEDED_CLAIM_ID,
            kind=NodeKind.CLAIM,
            label="Retrieval index rebuild cadence",
            content="The retrieval index rebuild runs weekly.",
            memory_class=MemoryClass.FACT,
            status=NodeStatus.SUPERSEDED,
            scope=_scope(),
            recorded_at=T0,
            valid_from=T0,
            valid_to=T1,
            source_refs=[_source_ref()],
        ),
        GraphNode(
            id=FUTURE_CLAIM_ID,
            kind=NodeKind.CLAIM,
            label="Retrieval index rebuild cadence",
            content="The retrieval index rebuild runs hourly.",
            memory_class=MemoryClass.FACT,
            scope=_scope(),
            recorded_at=T0,
            valid_from=T2,
            derivation=_migrated(),
        ),
        GraphNode(
            id=OTHER_SCOPE_CLAIM_ID,
            kind=NodeKind.CLAIM,
            label="Retrieval index shard count",
            content="The retrieval index uses four shards.",
            memory_class=MemoryClass.FACT,
            scope=_scope(OTHER_NAMESPACE),
            recorded_at=T0,
            derivation=_migrated(),
        ),
    ]
    edges = [
        _edge(RelationType.ABOUT, ACTIVE_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.ABOUT, SUPERSEDED_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.ABOUT, FUTURE_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.ABOUT, OTHER_SCOPE_CLAIM_ID, ENTITY_ID),
        _edge(RelationType.SUPERSEDES, ACTIVE_CLAIM_ID, SUPERSEDED_CLAIM_ID),
        _edge(RelationType.EVIDENCED_BY, ACTIVE_CLAIM_ID, EVIDENCE_ID),
        _edge(RelationType.OBSERVED_IN, ACTIVE_CLAIM_ID, EPISODE_ID),
    ]
    return InMemoryGraphStore(nodes=nodes, edges=edges)


def _claim_result(**kwargs) -> dict:
    """An existing recall result payload, shaped as ``recall`` already emits."""
    data = {
        "file": CLAIM_FILE,
        "score": 9.5,
        "matched_sections": 2,
        "sections": [{"heading": "Index maintenance", "line": 12}],
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
    data = {"file": UNMAPPED_FILE, "score": 1.0, "matched_sections": 0}
    data.update(kwargs)
    return data


def _node_ids(result) -> list[str]:
    return [node.id for node in result.graph_nodes]


# --- test doubles ------------------------------------------------------------

class _RecordingStore:
    """A full :class:`GraphStore` that records which operations were reached.

    Deliberately implements the write operations too: the point of
    ``test_no_write_operation_is_ever_reached`` is that the orchestrator does
    not call them even when it *could*.
    """

    def __init__(self, inner: InMemoryGraphStore) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def create_node(self, node):            # pragma: no cover - must not run
        self.calls.append("create_node")
        return self._inner.create_node(node)

    def append_edge(self, edge):            # pragma: no cover - must not run
        self.calls.append("append_edge")
        return self._inner.append_edge(edge)

    def rebuild(self, source_events):       # pragma: no cover - must not run
        self.calls.append("rebuild")
        return self._inner.rebuild(source_events)

    def get_node(self, node_id):
        self.calls.append("get_node")
        return self._inner.get_node(node_id)

    def get_edges(self, node_id, relation_types=None, direction="both"):
        self.calls.append("get_edges")
        return self._inner.get_edges(node_id, relation_types, direction)

    def traverse(self, seed_ids, max_hops=1, filters=None):
        self.calls.append("traverse")
        return self._inner.traverse(seed_ids, max_hops, filters)

    def find_conflicts(self, scope=None, entity_ids=None):
        self.calls.append("find_conflicts")
        return self._inner.find_conflicts(scope, entity_ids)

    def get_provenance(self, node_id):
        self.calls.append("get_provenance")
        return self._inner.get_provenance(node_id)

    def health(self):
        self.calls.append("health")
        return self._inner.health()


class _UnhealthyStore(InMemoryGraphStore):
    """A store that reports a broken graph through ``health`` (§10.3)."""

    def health(self) -> GraphHealth:
        base = super().health()
        return base.model_copy(
            update={"errors": ["dangling_edge:edge_0001:claim_not_stored"]}
        )


class _RaisingHealthStore(InMemoryGraphStore):
    """A store whose ``health`` breaks its own contract and raises."""

    def health(self) -> GraphHealth:
        raise RuntimeError("health probe failed")


class _RaisingTraversalStore(InMemoryGraphStore):
    def traverse(self, seed_ids, max_hops=1, filters=None):
        raise RuntimeError("traversal backend unavailable")


class _RaisingProvenanceStore(InMemoryGraphStore):
    def get_provenance(self, node_id):
        raise RuntimeError("provenance backend unavailable")


class _RaisingConflictStore(InMemoryGraphStore):
    def find_conflicts(self, scope=None, entity_ids=None):
        raise RuntimeError("conflict scan unavailable")


# ===========================================================================
# A. The read-only contract and caller-argument validation
# ===========================================================================

def test_the_layer_requires_only_read_operations():
    """A read-only batch must not even *ask* for a write operation (§12 F)."""
    assert GRAPH_RECALL_OPERATIONS == (
        "find_conflicts",
        "get_provenance",
        "health",
        "traverse",
    )
    for mutation in ("create_node", "append_edge", "rebuild"):
        assert mutation not in GRAPH_RECALL_OPERATIONS


def test_hops_are_bounded_by_the_architecture_limit():
    assert MAX_GRAPH_RECALL_HOPS == 2


@pytest.mark.parametrize("max_hops", [-1, MAX_GRAPH_RECALL_HOPS + 1])
def test_an_out_of_range_hop_count_is_refused(max_hops):
    with pytest.raises(ValueError, match="max_hops"):
        expand_recall_with_graph(
            [_claim_result()], _fixture_store(), max_hops=max_hops
        )


def test_a_non_positive_top_n_is_refused():
    with pytest.raises(ValueError, match="top_n"):
        expand_recall_with_graph([_claim_result()], _fixture_store(), top_n=0)


def test_a_result_without_a_file_identity_is_refused():
    with pytest.raises(ValueError, match="file"):
        expand_recall_with_graph([{"score": 1.0}], _fixture_store())


def test_a_duplicated_result_identity_is_refused():
    """The mapping is keyed on result identity, so a duplicate is ambiguous."""
    with pytest.raises(ValueError, match="duplicate"):
        expand_recall_with_graph(
            [_claim_result(), _claim_result(score=3.0)], _fixture_store()
        )


def test_a_mapping_key_that_names_no_result_is_refused():
    """A mapping that does not line up with the seeds is a caller bug, not a
    silent no-op: that is how an absolute-vs-relative key mismatch hides."""
    with pytest.raises(ValueError, match="seed_node_ids_by_result"):
        expand_recall_with_graph(
            [_claim_result()],
            _fixture_store(),
            seed_node_ids_by_result={"not-a-seed.md": [ENTITY_ID]},
        )


def test_a_blank_seed_node_id_is_refused():
    with pytest.raises(ValueError):
        expand_recall_with_graph(
            [_claim_result()],
            _fixture_store(),
            seed_node_ids_by_result={CLAIM_FILE: ["  "]},
        )


def test_a_store_without_the_read_surface_is_refused():
    class _Partial:
        def health(self):               # pragma: no cover - never reached
            raise AssertionError

        def traverse(self, *a, **k):    # pragma: no cover - never reached
            raise AssertionError

    with pytest.raises(ValueError, match="find_conflicts"):
        expand_recall_with_graph([_claim_result()], _Partial())


# ===========================================================================
# B. Fallback — the graph may be absent, broken or silent
# ===========================================================================

def test_no_store_preserves_the_original_results_and_says_so():
    report = expand_recall_with_graph(
        [_claim_result(), _entity_result()], None
    )
    assert report.status == GRAPH_STATUS_UNAVAILABLE
    assert report.graph_assisted is False
    assert report.backend == ""
    assert report.preserved_results == [_claim_result(), _entity_result()]
    assert [r.file for r in report.results] == [CLAIM_FILE, ENTITY_FILE]
    assert all(r.graph_nodes == [] for r in report.results)
    assert "graph_unavailable:no_store" in report.warnings


def test_an_empty_mapping_preserves_the_original_results_and_says_so():
    report = expand_recall_with_graph(
        [_claim_result(), _entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={},
    )
    assert report.status == GRAPH_STATUS_UNUSED
    assert report.graph_assisted is False
    assert report.preserved_results == [_claim_result(), _entity_result()]
    assert "no_graph_seeds" in report.warnings
    assert "unmapped_results:2" in report.warnings


def test_an_unhealthy_graph_falls_back_without_pretending_success():
    report = expand_recall_with_graph(
        [_claim_result()],
        _UnhealthyStore(),
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
    )
    assert report.status == GRAPH_STATUS_UNAVAILABLE
    assert report.graph_assisted is False
    assert report.preserved_results == [_claim_result()]
    assert "graph_unavailable:unhealthy" in report.warnings
    # The error *label* is surfaced; the raw error string is not echoed, so a
    # backend that names a file in its diagnostics cannot leak it here.
    assert "unhealthy_graph:dangling_edge" in report.warnings
    assert not any("edge_0001" in w for w in report.warnings)


def test_a_raising_health_probe_falls_back_instead_of_propagating():
    report = expand_recall_with_graph(
        [_claim_result()],
        _RaisingHealthStore(),
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
    )
    assert report.status == GRAPH_STATUS_UNAVAILABLE
    assert report.preserved_results == [_claim_result()]
    assert "graph_unavailable:health_failed:RuntimeError" in report.warnings


def test_a_raising_traversal_falls_back_without_losing_results():
    store = _RaisingTraversalStore(nodes=[GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY)])
    report = expand_recall_with_graph(
        [_claim_result(), _entity_result()],
        store,
        seed_node_ids_by_result={
            CLAIM_FILE: [ACTIVE_CLAIM_ID],
            ENTITY_FILE: [ENTITY_ID],
        },
    )
    assert report.status == GRAPH_STATUS_UNAVAILABLE
    assert report.graph_assisted is False
    assert report.preserved_results == [_claim_result(), _entity_result()]
    assert "graph_traversal_failed:RuntimeError" in report.warnings
    assert all(r.graph_nodes == [] for r in report.results)


def test_a_raising_provenance_lookup_becomes_a_warning():
    store = _RaisingProvenanceStore(
        nodes=[GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, scope=_scope())]
    )
    report = expand_recall_with_graph(
        [_entity_result()],
        store,
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    assert report.status == GRAPH_STATUS_PARTIAL
    assert _node_ids(report.results[0]) == [ENTITY_ID]
    assert report.results[0].provenance == []
    assert f"provenance_unavailable:{ENTITY_ID}" in report.warnings


def test_a_raising_conflict_scan_becomes_a_warning():
    store = _RaisingConflictStore(
        nodes=[GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, scope=_scope())]
    )
    report = expand_recall_with_graph(
        [_entity_result()],
        store,
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    assert report.status == GRAPH_STATUS_PARTIAL
    assert _node_ids(report.results[0]) == [ENTITY_ID]
    assert report.results[0].conflicts == []
    assert "conflicts_unavailable:RuntimeError" in report.warnings


# ===========================================================================
# C. Bounded expansion and hard filtering
# ===========================================================================

def test_one_seed_expands_one_hop_to_the_current_related_claim():
    report = expand_recall_with_graph(
        [_entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
        max_hops=1,
    )
    assert report.status == GRAPH_STATUS_OK
    assert report.graph_assisted is True
    assert report.truncated is False
    assert report.warnings == []

    result = report.results[0]
    assert result.graph_node_ids == [ENTITY_ID]
    # Current claim in, superseded claim out — the graph decided, not the score.
    assert ACTIVE_CLAIM_ID in _node_ids(result)
    assert SUPERSEDED_CLAIM_ID not in _node_ids(result)
    assert result.result == _entity_result()


def test_zero_hops_returns_only_the_mapped_seed_nodes():
    report = expand_recall_with_graph(
        [_entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
        max_hops=0,
    )
    assert _node_ids(report.results[0]) == [ENTITY_ID]
    assert report.results[0].graph_edges == []


def test_a_superseded_node_is_excluded_by_the_default_status_filter():
    report = expand_recall_with_graph(
        [_entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    assert _node_ids(report.results[0]) == [
        FUTURE_CLAIM_ID,
        ACTIVE_CLAIM_ID,
        OTHER_SCOPE_CLAIM_ID,
        ENTITY_ID,
    ]


def test_an_out_of_window_node_is_excluded_by_as_of():
    report = expand_recall_with_graph(
        [_entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
        filters=TraversalFilters(as_of=T1),
    )
    node_ids = _node_ids(report.results[0])
    assert ACTIVE_CLAIM_ID in node_ids
    assert FUTURE_CLAIM_ID not in node_ids       # valid_from is still ahead of T1
    assert SUPERSEDED_CLAIM_ID not in node_ids   # superseded regardless of window


def test_a_wrong_scope_node_is_excluded_by_the_scope_filter():
    report = expand_recall_with_graph(
        [_entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
        filters=TraversalFilters(scope=_scope()),
    )
    node_ids = _node_ids(report.results[0])
    assert ACTIVE_CLAIM_ID in node_ids
    assert OTHER_SCOPE_CLAIM_ID not in node_ids


def test_a_filtered_seed_is_not_re_added_to_the_result():
    """The orchestrator may echo what was *requested*; it may not resurrect a
    node the store's hard filters removed (§7.3 step 4)."""
    report = expand_recall_with_graph(
        [_claim_result()],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: [SUPERSEDED_CLAIM_ID]},
    )
    result = report.results[0]
    assert result.graph_node_ids == [SUPERSEDED_CLAIM_ID]
    assert result.graph_nodes == []
    assert result.result == _claim_result()
    assert f"excluded_seed:{SUPERSEDED_CLAIM_ID}" in report.warnings
    assert report.status == GRAPH_STATUS_PARTIAL


def test_an_unknown_seed_node_id_is_reported_not_guessed():
    report = expand_recall_with_graph(
        [_claim_result()],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: ["claim_not_stored"]},
    )
    assert report.results[0].graph_nodes == []
    assert report.results[0].result == _claim_result()
    assert "unknown_seed:claim_not_stored" in report.warnings


def test_a_bounded_traversal_exposes_truncation_rather_than_hiding_it():
    report = expand_recall_with_graph(
        [_entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
        filters=TraversalFilters(max_nodes=1),
    )
    assert report.status == GRAPH_STATUS_PARTIAL
    assert report.truncated is True
    assert report.results[0].truncated is True
    assert _node_ids(report.results[0]) == [ENTITY_ID]
    assert "truncated:max_nodes" in report.warnings


def test_a_weak_relation_is_not_followed_by_default():
    store = InMemoryGraphStore(
        nodes=[
            GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, scope=_scope()),
            GraphNode(id=OTHER_ENTITY_ID, kind=NodeKind.ENTITY, scope=_scope()),
        ],
        edges=[_edge(RelationType.RELATED_TO, ENTITY_ID, OTHER_ENTITY_ID)],
    )
    report = expand_recall_with_graph(
        [_entity_result()],
        store,
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    assert _node_ids(report.results[0]) == [ENTITY_ID]
    assert "weak_relation_excluded:RELATED_TO" in report.warnings
    assert report.status == GRAPH_STATUS_PARTIAL


def test_a_relation_type_filter_narrows_the_expansion():
    report = expand_recall_with_graph(
        [_claim_result()],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
        filters=TraversalFilters(relation_types=[RelationType.EVIDENCED_BY]),
    )
    assert _node_ids(report.results[0]) == [ACTIVE_CLAIM_ID, EVIDENCE_ID]


# ===========================================================================
# D. Seed preservation, ordering and limits
# ===========================================================================

def test_every_seed_stays_represented_even_without_a_graph_mapping():
    report = expand_recall_with_graph(
        [_claim_result(), _entity_result(), _unmapped_result()],
        _fixture_store(),
        seed_node_ids_by_result={
            CLAIM_FILE: [ACTIVE_CLAIM_ID],
            ENTITY_FILE: [ENTITY_ID],
        },
    )
    assert [r.file for r in report.results] == [
        CLAIM_FILE,
        ENTITY_FILE,
        UNMAPPED_FILE,
    ]
    assert report.preserved_results == [
        _claim_result(),
        _entity_result(),
        _unmapped_result(),
    ]
    unmapped = report.results[-1]
    assert unmapped.graph_node_ids == []
    assert unmapped.graph_nodes == []
    assert unmapped.graph_assisted is False
    assert "unmapped_results:1" in report.warnings
    assert report.status == GRAPH_STATUS_PARTIAL


def test_results_keep_the_original_seed_ranking():
    report = expand_recall_with_graph(
        [_claim_result(), _entity_result(), _unmapped_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    assert [r.rank for r in report.results] == [0, 1, 2]
    assert [r.file for r in report.results] == [
        CLAIM_FILE,
        ENTITY_FILE,
        UNMAPPED_FILE,
    ]
    assert [r.score for r in report.results] == [9.5, 4.25, 1.0]


def test_the_answer_does_not_depend_on_mapping_insertion_order():
    forward = expand_recall_with_graph(
        [_claim_result(), _entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={
            CLAIM_FILE: [ACTIVE_CLAIM_ID, ENTITY_ID],
            ENTITY_FILE: [ENTITY_ID],
        },
    )
    backward = expand_recall_with_graph(
        [_claim_result(), _entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={
            ENTITY_FILE: [ENTITY_ID],
            CLAIM_FILE: [ENTITY_ID, ACTIVE_CLAIM_ID],
        },
    )
    assert forward.to_json() == backward.to_json()
    assert forward.results[0].graph_node_ids == [ACTIVE_CLAIM_ID, ENTITY_ID]


def test_an_inline_seed_mapping_and_the_lookup_mapping_are_merged():
    report = expand_recall_with_graph(
        [RecallSeed.from_result(_claim_result(), [ACTIVE_CLAIM_ID])],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: [ENTITY_ID]},
    )
    assert report.results[0].graph_node_ids == [ACTIVE_CLAIM_ID, ENTITY_ID]


def test_top_n_bounds_the_expansion_without_dropping_a_seed():
    report = expand_recall_with_graph(
        [_claim_result(), _entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={
            CLAIM_FILE: [ACTIVE_CLAIM_ID],
            ENTITY_FILE: [ENTITY_ID],
        },
        top_n=1,
    )
    assert [r.file for r in report.results] == [CLAIM_FILE, ENTITY_FILE]
    assert report.results[0].graph_nodes != []
    assert report.results[1].graph_nodes == []
    assert report.results[1].graph_node_ids == [ENTITY_ID]
    assert report.results[1].result == _entity_result()
    assert "graph_expansion_limited:1" in report.warnings
    assert report.status == GRAPH_STATUS_PARTIAL


def test_no_seeds_at_all_is_an_empty_but_honest_answer():
    report = expand_recall_with_graph([], _fixture_store())
    assert report.results == []
    assert report.status == GRAPH_STATUS_UNUSED
    assert "no_graph_seeds" in report.warnings


# ===========================================================================
# E. Explainability — paths, evidence, conflicts
# ===========================================================================

def test_every_returned_node_has_a_path_back_to_its_seed():
    report = expand_recall_with_graph(
        [_entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    result = report.results[0]
    assert len(result.graph_paths) == len(result.graph_nodes)
    for path in result.graph_paths:
        assert path.node_ids[0] == ENTITY_ID
        assert path.hops <= 1
    reached = {path.node_ids[-1] for path in result.graph_paths}
    assert reached == set(_node_ids(result))
    # Stable edge IDs, so the explanation can be rendered and compared.
    one_hop = [p for p in result.graph_paths if p.hops == 1]
    assert all(p.edge_ids for p in one_hop)


def test_the_returned_edges_are_the_induced_subgraph():
    report = expand_recall_with_graph(
        [_claim_result()],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
    )
    result = report.results[0]
    returned = set(_node_ids(result))
    assert returned == {ACTIVE_CLAIM_ID, ENTITY_ID, EVIDENCE_ID, EPISODE_ID}
    assert {e.relation_type for e in result.graph_edges} == {
        RelationType.ABOUT,
        RelationType.EVIDENCED_BY,
        RelationType.OBSERVED_IN,
    }
    for edge in result.graph_edges:
        assert edge.source_id in returned and edge.target_id in returned


def test_evidence_is_backfilled_for_resolved_nodes():
    report = expand_recall_with_graph(
        [_claim_result()],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
    )
    records = {p.node_id: p for p in report.results[0].provenance}
    claim_provenance = records[ACTIVE_CLAIM_ID]
    assert claim_provenance.resolved is True
    assert claim_provenance.evidence_ids == [EVIDENCE_ID]
    assert claim_provenance.episode_ids == [EPISODE_ID]
    assert claim_provenance.source_refs == [_source_ref()]
    assert claim_provenance.warnings == []
    assert report.status == GRAPH_STATUS_OK


def test_provenance_records_are_ordered_by_node_id():
    report = expand_recall_with_graph(
        [_entity_result()],
        _fixture_store(),
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    node_ids = [p.node_id for p in report.results[0].provenance]
    assert node_ids == sorted(node_ids)
    # An identity node with nothing behind it contributes no empty record.
    assert ENTITY_ID not in node_ids
    assert ACTIVE_CLAIM_ID in node_ids


def test_unresolved_provenance_is_warned_about_and_never_invented():
    store = InMemoryGraphStore(
        nodes=[
            GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, scope=_scope()),
            GraphNode(
                id=ACTIVE_CLAIM_ID,
                kind=NodeKind.CLAIM,
                label="Retrieval index rebuild cadence",
                scope=_scope(),
                source_refs=[SourceRef(evidence_id=MISSING_EVIDENCE_ID)],
            ),
        ],
        edges=[_edge(RelationType.ABOUT, ACTIVE_CLAIM_ID, ENTITY_ID)],
    )
    report = expand_recall_with_graph(
        [_entity_result()],
        store,
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    record = {p.node_id: p for p in report.results[0].provenance}[ACTIVE_CLAIM_ID]
    assert record.resolved is False
    assert record.warnings == [f"unresolved_evidence:{MISSING_EVIDENCE_ID}"]
    assert record.evidence_ids == [MISSING_EVIDENCE_ID]
    # Nothing was fabricated to stand in for the missing evidence.
    assert record.source_refs == [SourceRef(evidence_id=MISSING_EVIDENCE_ID)]
    assert f"provenance_unresolved:{ACTIVE_CLAIM_ID}" in report.warnings
    assert report.status == GRAPH_STATUS_PARTIAL


def test_an_unresolved_conflict_is_visible_not_settled():
    contested = "claim_index_rebuild_on_demand"
    store = InMemoryGraphStore(
        nodes=[
            GraphNode(id=ENTITY_ID, kind=NodeKind.ENTITY, scope=_scope()),
            GraphNode(
                id=ACTIVE_CLAIM_ID,
                kind=NodeKind.CLAIM,
                label="Retrieval index rebuild cadence",
                content="The retrieval index rebuild runs nightly.",
                scope=_scope(),
                conflict_group="index_rebuild_cadence",
                derivation=_migrated(),
            ),
            GraphNode(
                id=contested,
                kind=NodeKind.CLAIM,
                label="Retrieval index rebuild cadence",
                content="The retrieval index rebuild runs on demand only.",
                scope=_scope(),
                conflict_group="index_rebuild_cadence",
                derivation=_migrated(),
            ),
        ],
        edges=[
            _edge(RelationType.ABOUT, ACTIVE_CLAIM_ID, ENTITY_ID),
            _edge(RelationType.ABOUT, contested, ENTITY_ID),
        ],
    )
    report = expand_recall_with_graph(
        [_entity_result()],
        store,
        seed_node_ids_by_result={ENTITY_FILE: [ENTITY_ID]},
    )
    conflicts = report.results[0].conflicts
    assert len(conflicts) == 1
    assert conflicts[0].node_ids == [ACTIVE_CLAIM_ID, contested]
    assert conflicts[0].resolved is False
    assert conflicts[0].conflict_group == "index_rebuild_cadence"
    assert f"unresolved_conflict:{ACTIVE_CLAIM_ID}" in report.warnings
    assert report.status == GRAPH_STATUS_PARTIAL


def test_conflict_and_provenance_lookups_can_be_switched_off():
    report = expand_recall_with_graph(
        [_claim_result()],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
        include_provenance=False,
        include_conflicts=False,
    )
    assert report.results[0].graph_nodes != []
    assert report.results[0].provenance == []
    assert report.results[0].conflicts == []


# ===========================================================================
# F. Read-only, serialization and payload compatibility
# ===========================================================================

def test_no_write_operation_is_ever_reached():
    store = _RecordingStore(_fixture_store())
    expand_recall_with_graph(
        [_claim_result(), _entity_result(), _unmapped_result()],
        store,
        seed_node_ids_by_result={
            CLAIM_FILE: [ACTIVE_CLAIM_ID],
            ENTITY_FILE: [ENTITY_ID],
        },
    )
    assert set(store.calls) <= set(GRAPH_RECALL_OPERATIONS)
    for mutation in ("create_node", "append_edge", "rebuild"):
        assert mutation not in store.calls


def test_the_fixture_graph_is_unchanged_by_a_recall():
    store = _fixture_store()
    before = store.snapshot()
    expand_recall_with_graph(
        [_claim_result()],
        store,
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
    )
    assert store.snapshot() == before


def test_serialization_is_json_friendly_and_deterministic():
    report = expand_recall_with_graph(
        [_claim_result(), _entity_result(), _unmapped_result()],
        _fixture_store(),
        seed_node_ids_by_result={
            CLAIM_FILE: [ACTIVE_CLAIM_ID],
            ENTITY_FILE: [ENTITY_ID],
        },
    )
    payload = report.to_dict()
    json.dumps(payload, sort_keys=True)          # must not raise

    def _walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert isinstance(key, str)
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        else:
            assert isinstance(value, (str, int, float, bool, type(None))), value

    _walk(payload)
    assert report.to_json() == GraphRecallReport(**payload).to_json()


def test_the_original_payload_survives_unchanged():
    original = _claim_result()
    report = expand_recall_with_graph(
        [original],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
    )
    assert report.results[0].result == _claim_result()
    # The caller's own dict is neither read later nor shared with the report.
    original["score"] = -1.0
    original["sections"].clear()
    assert report.results[0].result == _claim_result()
    assert report.results[0].score == 9.5


def test_a_recall_seed_can_be_built_from_an_existing_result():
    seed = RecallSeed.from_result(_claim_result(), [ACTIVE_CLAIM_ID])
    assert seed.file == CLAIM_FILE
    assert seed.score == 9.5
    assert seed.seed_node_ids == [ACTIVE_CLAIM_ID]
    assert seed.result == _claim_result()


def test_graph_scores_are_not_fused_into_the_seed_score():
    """§7.3: the graph filters and explains; it does not invent a new score."""
    report = expand_recall_with_graph(
        [_claim_result()],
        _fixture_store(),
        seed_node_ids_by_result={CLAIM_FILE: [ACTIVE_CLAIM_ID]},
    )
    assert report.results[0].score == _claim_result()["score"]
    assert "graph_score" not in report.results[0].to_dict()

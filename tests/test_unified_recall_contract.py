"""Acceptance contract for the opt-in unified recall engine."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from layered_memory_mcp.unified_recall import (
    GraphSearchAdapter,
    L0RouteAdapter,
    L1StoreHydrator,
    UnifiedRecallEngine,
    build_unified_engine,
)


class Channel:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def search(self, query, top_n=5, domain=None):
        if self.error:
            raise self.error
        return self.rows[:top_n]


class Router:
    def __init__(self, route=None, error=None):
        self.route_value = route
        self.error = error

    def route(self, query):
        if self.error:
            raise self.error
        return self.route_value


class Graph:
    def __init__(self, keep=None, expanded=None):
        self.keep = keep
        self.expanded = expanded or []

    def filter_and_expand(self, candidates, query=None, route=None):
        keep = set(self.keep) if self.keep is not None else {
            candidate["entry_id"] for candidate in candidates
        }
        return {
            "candidates": [candidate for candidate in candidates if candidate["entry_id"] in keep],
            "expanded": self.expanded,
        }


class Hydrator:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def hydrate(self, entry_ids):
        self.calls.append(list(entry_ids))
        return {entry_id: self.rows[entry_id] for entry_id in entry_ids if entry_id in self.rows}


def _row(entry_id, score, **extra):
    return {"entry_id": entry_id, "score": score, **extra}


def _engine(vector, bm25, *, router=None, graph=None, hydrator=None):
    return UnifiedRecallEngine(
        vector=vector,
        bm25=bm25,
        router=router,
        graph=graph,
        l1=hydrator,
    )


def test_a_happy_path_unions_filters_and_hydrates_by_identity():
    engine = _engine(
        Channel([_row("valid-vector", 0.9), _row("invalid", 0.8)]),
        Channel([_row("valid-lexical", 2.0), _row("valid-vector", 1.0)]),
        router=Router(["knowledge"]),
        graph=Graph(keep={"valid-vector", "valid-lexical"}),
        hydrator=Hydrator({
            "valid-vector": {"content": "vector truth"},
            "valid-lexical": {"content": "lexical truth"},
        }),
    )

    result = engine.retrieve("truth", top_n=5)

    assert [row["entry_id"] for row in result["results"]] == [
        "valid-vector",
        "valid-lexical",
    ]
    assert result["trace"]["stage_order"] == [
        "route", "vector", "bm25", "identity_merge", "graph", "hydration", "fusion"
    ]
    assert result["trace"]["stages"]["hydration"]["occurred"] is True


@pytest.mark.parametrize("failed, surviving", [("vector", "lexical"), ("bm25", "vector")])
def test_b_channel_failure_preserves_other_channel_and_is_observable(failed, surviving):
    vector = Channel([_row("vector-id", 0.9)], RuntimeError("embedding down")) if failed == "vector" else Channel([_row("vector-id", 0.9)])
    bm25 = Channel([_row("lexical-id", 2.0)], RuntimeError("index down")) if failed == "bm25" else Channel([_row("lexical-id", 2.0)])
    result = _engine(vector, bm25, graph=Graph(), hydrator=Hydrator({"vector-id": {}, "lexical-id": {}})).retrieve("q")

    assert result["results"]
    assert result["trace"]["degraded"] is True
    assert any(failed in reason for reason in result["trace"]["fallback_reasons"])
    assert any(row["entry_id"].startswith(surviving) for row in result["results"])


def test_d_l0_empty_or_failure_is_soft_and_observable():
    result = _engine(
        Channel([_row("entry", 1.0)]), Channel(), router=Router(error=RuntimeError("route failed")),
        graph=Graph(), hydrator=Hydrator({"entry": {}}),
    ).retrieve("q")

    assert [row["entry_id"] for row in result["results"]] == ["entry"]
    assert result["trace"]["stages"]["route"]["degraded"] is True


def test_e_graph_can_reject_everything_without_l1_leakage():
    hydrator = Hydrator({"invalid": {"content": "must not leak"}})
    result = _engine(Channel([_row("invalid", 1.0)]), Channel(), graph=Graph(keep=set()), hydrator=hydrator).retrieve("q")

    assert result["results"] == []
    assert hydrator.calls == []
    assert "graph_rejected_all" in result["trace"]["fallback_reasons"]


def test_f_missing_l1_identity_is_dropped():
    result = _engine(Channel([_row("missing", 1.0)]), Channel(), graph=Graph(), hydrator=Hydrator({})).retrieve("q")

    assert result["results"] == []
    assert result["trace"]["stages"]["hydration"]["missing_count"] == 1


def test_g_duplicate_identity_has_stable_provenance():
    result = _engine(
        Channel([_row("same", 0.8)]), Channel([_row("same", 3.0)]),
        graph=Graph(), hydrator=Hydrator({"same": {"content": "one"}}),
    ).retrieve("q")

    assert [row["entry_id"] for row in result["results"]] == ["same"]
    assert result["results"][0]["provenance"] == ["bm25", "vector"]


def test_g_duplicate_identity_uses_each_sources_own_rank_for_rrf():
    result = _engine(
        Channel([_row("other", 0.9), _row("same", 0.8)]),
        Channel([_row("same", 3.0)]),
        graph=Graph(), hydrator=Hydrator({"other": {}, "same": {}}),
    ).retrieve("q", top_n=5)

    same = next(row for row in result["results"] if row["entry_id"] == "same")
    assert same["score"] == round(1 / 62 + 1 / 61, 8)
    assert result["trace"]["stages"]["identity_merge"]["source_ranks"]["same"] == {
        "bm25": 1,
        "vector": 2,
    }


def test_h_determinism_and_serializable_trace():
    engine = _engine(
        Channel([_row("b", 1.0), _row("a", 1.0)]),
        Channel([_row("a", 1.0), _row("b", 1.0)]),
        graph=Graph(), hydrator=Hydrator({"a": {}, "b": {}}),
    )
    first = engine.retrieve("q")
    second = engine.retrieve("q")

    assert first == second
    json.dumps(first)


def test_i_compatibility_is_explicitly_opt_in():
    from layered_memory_mcp import recall

    assert callable(recall.recall)
    assert hasattr(UnifiedRecallEngine, "retrieve")


def test_j_fixture_hydrator_is_only_called_after_graph():
    events = []

    class OrderedGraph(Graph):
        def filter_and_expand(self, candidates, query=None, route=None):
            events.append("graph")
            return super().filter_and_expand(candidates, query=query, route=route)

    class OrderedHydrator(Hydrator):
        def hydrate(self, entry_ids):
            events.append("l1")
            return super().hydrate(entry_ids)

    _engine(
        Channel([_row("entry", 1.0)]), Channel(), graph=OrderedGraph(),
        hydrator=OrderedHydrator({"entry": {}}),
    ).retrieve("q")
    assert events == ["graph", "l1"]


def test_production_l0_adapter_empty_route_is_soft(tmp_path):
    engine = _engine(
        Channel([_row("entry", 1.0)]), Channel(),
        router=L0RouteAdapter(tmp_path / "missing-l0.md"),
        graph=Graph(), hydrator=Hydrator({"entry": {}}),
    )

    result = engine.retrieve("q")

    assert [row["entry_id"] for row in result["results"]] == ["entry"]
    assert result["trace"]["stages"]["route"]["reason"] == "empty_route"


def test_production_l0_adapter_routes_contiguous_cjk_query(tmp_path):
    index = tmp_path / "L0.md"
    index.write_text(
        "[L0] retrieval: 向量语义与关键词检索 → knowledge/retrieval.md\n"
        "[L0] deployment: 服务运行与部署维护 → knowledge/deployment.md\n",
        encoding="utf-8",
    )

    routes = L0RouteAdapter(index).route("向量检索")

    assert routes
    assert routes[0]["line"].startswith("[L0] retrieval:")


def test_production_l0_adapter_mixed_query_keeps_ascii_behavior(tmp_path):
    index = tmp_path / "L0.md"
    index.write_text(
        "[L0] layered-memory: layered-memory — md, config → knowledge/layered-memory.md\n",
        encoding="utf-8",
    )

    routes = L0RouteAdapter(index).route("layered memory")

    assert routes
    assert routes[0]["score"] == 1.0


def test_production_builder_wires_l0_router(tmp_path):
    engine = build_unified_engine(
        SimpleNamespace(l0_index_file=tmp_path / "L0.md"),
        vector_store=Channel(),
        l1_store=Hydrator({}),
    )
    assert isinstance(engine.router, L0RouteAdapter)


def test_production_graph_adapter_uses_engine_candidate_rows():
    rows = [{"id": "entry", "entry_id": "entry", "score": 1.0, "metadata": {}}]

    result = GraphSearchAdapter(SimpleNamespace()).filter_and_expand(rows)

    assert result["candidates"] == rows
    assert result["expanded"] == []


def test_l1_hydrator_reads_frontmatter_entry_by_identity():
    class Store:
        def read(self, domain):
            return ({"id": "entry", "section": "Section"}, "frontmatter body")

    candidate = SimpleNamespace(entry_id="entry", payload={"domain": "domain"})
    result = L1StoreHydrator(Store()).hydrate(["entry"], [candidate])

    assert result["entry"]["content"] == "frontmatter body"
    assert result["entry"]["domain"] == "domain"


def test_l1_hydrator_reads_legacy_section_by_domain_and_section():
    class Store:
        def read(self, domain):
            return None, """# Domain\n\n## First\nfirst body\n\n## Target\ntarget body\n"""

    candidate = SimpleNamespace(
        entry_id="legacy-random-id",
        payload={"domain": "domain", "metadata": {"section": "Target"}},
    )
    result = L1StoreHydrator(Store()).hydrate(["legacy-random-id"], [candidate])

    assert result["legacy-random-id"] == {
        "id": "legacy-random-id",
        "entry_id": "legacy-random-id",
        "domain": "domain",
        "section": "Target",
        "content": "target body",
        "text": "target body",
    }


def test_l1_hydrator_does_not_guess_duplicate_legacy_sections():
    class Store:
        def read(self, domain):
            return None, "## Target\nfirst\n\n## Target\nsecond\n"

    candidate = SimpleNamespace(
        entry_id="legacy-random-id",
        payload={"domain": "domain", "metadata": {"section": "Target"}},
    )

    assert L1StoreHydrator(Store()).hydrate(["legacy-random-id"], [candidate]) == {}


def test_l1_hydrator_uses_candidate_metadata_for_legacy_section():
    class Store:
        def read(self, domain):
            return None, "## Target\ntarget body\n"

    candidate = SimpleNamespace(
        entry_id="legacy-random-id",
        payload={"domain": "domain", "metadata": {"section": "Other"}},
    )

    assert L1StoreHydrator(Store()).hydrate(["legacy-random-id"], [candidate]) == {}

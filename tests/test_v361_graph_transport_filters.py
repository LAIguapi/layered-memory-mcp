"""v3.6.1 — current-time validity and configured-namespace filtering, per transport.

Why this file exists: two independently verified defects in the unified graph
recall path, both of which only show up when the *actual* entrances are driven.

1. **The REST search route was unreachable.** ``GET /knowledge/search`` was
   declared after ``GET /knowledge/{entry_id}``, and FastAPI matches routes in
   declaration order, so a real ASGI request for ``/knowledge/search`` entered
   ``get_knowledge`` with ``entry_id="search"`` and answered 404. The search
   implementation — graph policy included — was never reached. Pinned here
   through a real ASGI request, plus a source-order assertion that runs even
   where FastAPI is not installed, because the defect *is* the declaration
   order and nothing else.

2. **The shared recall layer ran the graph without filters.** It called
   ``run_graph_assisted_recall`` with no ``TraversalFilters``, so the default
   "current status only" was the only thing that applied: a claim whose
   validity window has not opened yet (``valid_from=2099``), a claim whose
   window has closed, and a claim belonging to a *foreign* namespace were all
   returned as current by all three entrances.

What is pinned, across MCP ``search_semantic``, MCP ``recall_knowledge`` and
REST ``/knowledge/search``:

  * active in the configured namespace survives;
  * superseded is removed (lifecycle);
  * not-yet-valid and no-longer-valid are removed (current-time validity);
  * a foreign namespace is removed (scope);
  * ``shared`` and *unscoped* (legacy global) material survives a narrowed
    namespace — the documented behaviour, so enabling a namespace cannot
    silently delete every pre-namespace node;
  * ``namespace: shared`` is the legacy no-isolation mode and filters nothing;
  * expansions are screened on the same terms as seeds;
  * an unmapped row, ``legacy`` mode and a safe unavailable graph are untouched.

Isolation: tmp_path config, a tmp SQLite graph database, neutral technical
fixtures, a deterministic fake vector store and no embedding model, no network
and no user data. The shared policy is never monkeypatched: every transport
assertion comes from the real entrance over the real configured store.
"""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from layered_memory_mcp import server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_entry_identity import markdown_section_entry_id
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphEdge,
    GraphNode,
    GraphScope,
    NodeKind,
    NodeStatus,
    RelationType,
)
from layered_memory_mcp.graph_runtime import GRAPH_SEED_ID_KEY
from layered_memory_mcp.graph_search import (
    GRAPH_EXCLUDED_LIFECYCLE,
    GRAPH_EXCLUDED_SCOPE,
)
from layered_memory_mcp.graph_seed_metadata import graph_node_id_for_entry_id
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore

MIGRATED = Derivation(method=DerivationMethod.MIGRATED)

NAMESPACE = "expected"
SECTION = "Rebuild cadence"
KEYWORD = "rebuild"

PAST_FROM = datetime(2020, 1, 1, tzinfo=timezone.utc)
PAST_TO = datetime(2021, 1, 1, tzinfo=timezone.utc)
FUTURE_FROM = datetime(2099, 1, 1, tzinfo=timezone.utc)
RECORDED = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The seven scopes, as one table
# ---------------------------------------------------------------------------

#: ``domain -> (status, namespace, valid_from, valid_to, survives)``. One H2
#: section per domain file, so every keyword result is its own row and a
#: surviving section cannot carry an excluded one back into the answer.
SCOPES: dict[str, tuple[NodeStatus, str | None, datetime | None, datetime | None, bool]] = {
    "active": (NodeStatus.ACTIVE, NAMESPACE, None, None, True),
    "superseded": (NodeStatus.SUPERSEDED, NAMESPACE, None, None, False),
    "future": (NodeStatus.ACTIVE, NAMESPACE, FUTURE_FROM, None, False),
    "expired": (NodeStatus.ACTIVE, NAMESPACE, PAST_FROM, PAST_TO, False),
    "foreign": (NodeStatus.ACTIVE, "alien", None, None, False),
    "shared": (NodeStatus.ACTIVE, "shared", None, None, True),
    "unscoped": (NodeStatus.ACTIVE, None, None, None, True),
}

#: A node reachable in one hop from ``active``, in the configured namespace.
NEIGHBOUR_NODE = "claim_cadence_neighbour"
#: A node reachable in one hop from ``active``, in a foreign namespace.
FOREIGN_NEIGHBOUR_NODE = "claim_cadence_alien_neighbour"

UNMAPPED_ROW = "unmapped-row"


def entry_id(domain: str) -> str:
    return markdown_section_entry_id(domain, SECTION, 0)


def node_id(domain: str) -> str:
    return graph_node_id_for_entry_id(entry_id(domain))


SURVIVING_DOMAINS = sorted(d for d, spec in SCOPES.items() if spec[4])
EXCLUDED_DOMAINS = sorted(d for d, spec in SCOPES.items() if not spec[4])


def _claim(identifier: str, label: str, *, namespace, status, valid_from, valid_to):
    return GraphNode(
        id=identifier,
        kind=NodeKind.CLAIM,
        label=label,
        content=f"The {label} index rebuild cadence.",
        scope=GraphScope(namespace=namespace),
        status=status,
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=RECORDED,
        derivation=MIGRATED,
    )


def _nodes() -> list[GraphNode]:
    nodes = [
        _claim(
            node_id(domain),
            domain,
            namespace=namespace,
            status=status,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        for domain, (status, namespace, valid_from, valid_to, _keep) in SCOPES.items()
    ]
    nodes.append(
        _claim(
            NEIGHBOUR_NODE,
            "neighbour",
            namespace=NAMESPACE,
            status=NodeStatus.ACTIVE,
            valid_from=None,
            valid_to=None,
        )
    )
    nodes.append(
        _claim(
            FOREIGN_NEIGHBOUR_NODE,
            "alien neighbour",
            namespace="alien",
            status=NodeStatus.ACTIVE,
            valid_from=None,
            valid_to=None,
        )
    )
    return nodes


def _edges() -> list[GraphEdge]:
    return [
        GraphEdge(
            id=f"edge_{target}",
            relation_type=RelationType.SUPPORTS,
            source_id=node_id("active"),
            target_id=target,
            recorded_at=RECORDED,
            derivation=MIGRATED,
        )
        for target in (NEIGHBOUR_NODE, FOREIGN_NEIGHBOUR_NODE)
    ]


@pytest.fixture
def graph_database(tmp_path) -> Path:
    parent = tmp_path / "graph-data"
    parent.mkdir(parents=True, exist_ok=True)
    database = parent / "graph.db"
    SQLiteGraphStore(database, nodes=_nodes(), edges=_edges()).close()
    return database


# ---------------------------------------------------------------------------
# Config and producers
# ---------------------------------------------------------------------------

def _config(
    tmp_path: Path,
    mode: str,
    *,
    namespace: str = NAMESPACE,
    database: Path | None = None,
) -> MemoryConfig:
    home = tmp_path / f"home-{mode}-{namespace}"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    config = MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        namespace=namespace,
        graph_mode=mode,
        graph_db_path=str(database) if database is not None else None,
        auto_maintain=False,
    )
    for domain in SCOPES:
        (Path(config.knowledge_dir) / f"{domain}.md").write_text(
            f"# {domain.title()}\n\n"
            f"## {SECTION}\n\nThe {domain} index {KEYWORD} cadence.\n",
            encoding="utf-8",
        )
    return config


def _rows() -> list[dict]:
    rows = [
        {
            "id": entry_id(domain),
            "domain": domain,
            "text": f"The {domain} index {KEYWORD} cadence.",
            "metadata": {
                "type": "fact",
                "section": SECTION,
                GRAPH_SEED_ID_KEY: node_id(domain),
            },
            "score": 0.8,
        }
        for domain in SCOPES
    ]
    rows.append(
        {
            "id": UNMAPPED_ROW,
            "domain": "glossary",
            "text": "index glossary",
            "metadata": {"type": "fact", "section": "Glossary"},
            "score": 0.1,
        }
    )
    return rows


class _FakeVectorStore:
    """Deterministic retrieval: the fixed row set, in a fixed order. No model."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def search(self, query, top_n=5, domain=None):
        return deepcopy(self.rows)


def _semantic(monkeypatch, config: MemoryConfig) -> dict:
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", None)
    monkeypatch.setattr(server, "_v2_stores", {"vector": _FakeVectorStore(_rows())})
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    return json.loads(asyncio.run(server.search_semantic(query=KEYWORD, top_n=20)))


def _recall(monkeypatch, config: MemoryConfig) -> dict:
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", None)
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    return json.loads(asyncio.run(server.recall_knowledge(keyword=KEYWORD, top_n=20)))


def _rest_get(config: MemoryConfig, monkeypatch, url: str, **params):
    """One real ASGI request against the real application, as built in production."""
    pytest.importorskip("fastapi")
    import httpx

    from layered_memory_mcp.api import server as api

    monkeypatch.setattr(
        api, "stores", {"vector": _FakeVectorStore(_rows()), "config": config}
    )
    app = api.create_app()

    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(url, params=params)

    return asyncio.run(_run())


def _rest_search(config: MemoryConfig, monkeypatch):
    return _rest_get(config, monkeypatch, "/knowledge/search", q=KEYWORD, top_n=20)


def _api_source() -> str:
    """The REST transport's source, read without importing FastAPI."""
    import layered_memory_mcp

    path = Path(layered_memory_mcp.__file__).parent / "api" / "server.py"
    return path.read_text(encoding="utf-8")


def _ids(response_results) -> list[str]:
    return [row["id"] for row in response_results]


def _files(response_results) -> list[str]:
    return [row["file"] for row in response_results]


# ---------------------------------------------------------------------------
# Defect 1 — the REST search route must be reachable
# ---------------------------------------------------------------------------

def _declared_get_paths() -> list[str]:
    """Every ``@app.get`` path in the REST transport, in declaration order."""
    import ast

    paths: list[str] = []
    for node in ast.walk(ast.parse(_api_source())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "get"
                and isinstance(target.value, ast.Name)
                and target.value.id == "app"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                paths.append(decorator.args[0].value)
    return paths


def test_the_rest_search_route_is_declared_before_every_knowledge_id_route():
    """The defect is the declaration order, so the order is what is pinned.

    FastAPI matches in declaration order: with ``/knowledge/{entry_id}`` first,
    a request for ``/knowledge/search`` is an ID lookup for the entry called
    "search", and the search implementation is dead code. Asserted over the
    production source so it holds even where FastAPI is not installed, and over
    *every* parameterized sibling rather than one literal, so a second
    ``/knowledge/{...}`` GET cannot reintroduce the shadow later.
    """
    paths = _declared_get_paths()
    assert "/knowledge/search" in paths, paths

    search = paths.index("/knowledge/search")
    shadowing = [
        (index, path)
        for index, path in enumerate(paths)
        if index < search and re.fullmatch(r"/knowledge/\{[^/]+\}", path)
    ]

    assert shadowing == [], (
        f"GET /knowledge/search is shadowed by {shadowing}: FastAPI matches "
        "routes in declaration order, so the literal path must come first"
    )


def test_a_real_asgi_request_for_knowledge_search_reaches_the_search_route(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, "legacy")

    response = _rest_search(config, monkeypatch)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, list)
    assert _ids(payload) == _ids(_rows())


def test_the_rest_id_lookup_still_answers_and_still_404s(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import httpx

    from layered_memory_mcp.api import server as api
    from layered_memory_mcp.models import KnowledgeEntry, SourceInfo, SourceType
    from layered_memory_mcp.storage import L1Store

    config = _config(tmp_path, "legacy", namespace="shared")
    l1 = L1Store(config.knowledge_dir)
    entry = KnowledgeEntry(
        domain="lookup",
        section="Rebuild cadence",
        content="The lookup index rebuild cadence.",
        source=SourceInfo(type=SourceType.MANUAL, extracted_by="test"),
    )
    assert l1.write(entry).get("success")

    monkeypatch.setattr(
        api,
        "stores",
        {"l1": l1, "vector": _FakeVectorStore(_rows()), "config": config},
    )
    app = api.create_app()

    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return (
                await client.get(f"/knowledge/{entry.id}"),
                await client.get("/knowledge/no-such-entry"),
            )

    found, missing = asyncio.run(_run())

    assert found.status_code == 200, found.text
    assert found.json()["id"] == entry.id
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Defect 2 — one validity/namespace policy, enforced at every entrance
# ---------------------------------------------------------------------------

def test_semantic_search_returns_only_current_in_namespace_rows(
    tmp_path, monkeypatch, graph_database
):
    config = _config(tmp_path, "graph_recall", database=graph_database)

    response = _semantic(monkeypatch, config)

    ids = _ids(response["results"])
    assert ids == [entry_id(d) for d in SCOPES if SCOPES[d][4]] + [UNMAPPED_ROW]
    for domain in EXCLUDED_DOMAINS:
        assert entry_id(domain) not in ids, domain
    assert response["graph"]["excluded_results"] == len(EXCLUDED_DOMAINS)
    assert set(response["graph"]["exclusion_reasons"]) == {
        GRAPH_EXCLUDED_LIFECYCLE,
        GRAPH_EXCLUDED_SCOPE,
    }


def test_keyword_recall_returns_only_current_in_namespace_files(
    tmp_path, monkeypatch, graph_database
):
    config = _config(tmp_path, "graph_recall", database=graph_database)

    response = _recall(monkeypatch, config)

    files = sorted(_files(response["results"]))
    assert files == [f"{domain}.md" for domain in SURVIVING_DOMAINS]
    assert response["matched_files"] == len(SURVIVING_DOMAINS)
    assert response["graph"]["excluded_results"] == len(EXCLUDED_DOMAINS)


def test_rest_knowledge_search_returns_only_current_in_namespace_rows(
    tmp_path, monkeypatch, graph_database
):
    config = _config(tmp_path, "graph_recall", database=graph_database)

    response = _rest_search(config, monkeypatch)

    assert response.status_code == 200, response.text
    ids = _ids(response.json())
    assert ids == [entry_id(d) for d in SCOPES if SCOPES[d][4]] + [UNMAPPED_ROW]


# Each entrance is asserted in its own test rather than three in one: a REST
# test skips where FastAPI is absent, and sharing a test with the two MCP
# entrances would take their evidence down with it.

@pytest.mark.parametrize("domain", EXCLUDED_DOMAINS)
def test_every_excluded_scope_is_excluded_at_both_mcp_entrances(
    tmp_path, monkeypatch, graph_database, domain
):
    config = _config(tmp_path, "graph_recall", database=graph_database)

    assert entry_id(domain) not in _ids(_semantic(monkeypatch, config)["results"])
    assert f"{domain}.md" not in _files(_recall(monkeypatch, config)["results"])


@pytest.mark.parametrize("domain", EXCLUDED_DOMAINS)
def test_every_excluded_scope_is_excluded_over_rest(
    tmp_path, monkeypatch, graph_database, domain
):
    config = _config(tmp_path, "graph_recall", database=graph_database)

    rest = _rest_search(config, monkeypatch)
    assert entry_id(domain) not in _ids(rest.json())


@pytest.mark.parametrize("domain", SURVIVING_DOMAINS)
def test_own_shared_and_unscoped_material_survives_at_both_mcp_entrances(
    tmp_path, monkeypatch, graph_database, domain
):
    """The documented reading of an unscoped node: legacy *global* material.

    ``GraphScope.matches`` would refuse an unscoped node under a narrowed
    filter, and nothing in the framework stamps a namespace onto the nodes it
    writes — so treating "unscoped" as "not mine" would delete the entire
    pre-namespace graph the moment a namespace was configured.
    """
    config = _config(tmp_path, "graph_recall", database=graph_database)

    assert entry_id(domain) in _ids(_semantic(monkeypatch, config)["results"])
    assert f"{domain}.md" in _files(_recall(monkeypatch, config)["results"])


@pytest.mark.parametrize("domain", SURVIVING_DOMAINS)
def test_own_shared_and_unscoped_material_survives_over_rest(
    tmp_path, monkeypatch, graph_database, domain
):
    config = _config(tmp_path, "graph_recall", database=graph_database)

    rest = _rest_search(config, monkeypatch)
    assert entry_id(domain) in _ids(rest.json())


def test_an_unmapped_row_is_never_removed_by_the_namespace_policy(
    tmp_path, monkeypatch, graph_database
):
    config = _config(tmp_path, "graph_recall", database=graph_database)

    assert UNMAPPED_ROW in _ids(_semantic(monkeypatch, config)["results"])


# ---------------------------------------------------------------------------
# Namespace semantics: shared is the legacy no-isolation mode
# ---------------------------------------------------------------------------

def test_the_shared_namespace_isolates_nothing(tmp_path, monkeypatch, graph_database):
    config = _config(tmp_path, "graph_recall", namespace="shared", database=graph_database)

    response = _semantic(monkeypatch, config)

    ids = _ids(response["results"])
    # Scope no longer removes anything — the foreign namespace is visible again.
    assert entry_id("foreign") in ids
    assert GRAPH_EXCLUDED_SCOPE not in response["graph"]["exclusion_reasons"]
    # Lifecycle and current-time validity still apply: they are not scope.
    for domain in ("superseded", "future", "expired"):
        assert entry_id(domain) not in ids, domain


def test_the_shared_namespace_isolates_nothing_over_rest(
    tmp_path, monkeypatch, graph_database
):
    config = _config(tmp_path, "graph_recall", namespace="shared", database=graph_database)

    response = _rest_search(config, monkeypatch)

    assert entry_id("foreign") in _ids(response.json())


# ---------------------------------------------------------------------------
# Expansions are screened on the same terms as seeds
# ---------------------------------------------------------------------------

def test_an_expansion_into_a_foreign_namespace_is_screened_out(
    tmp_path, monkeypatch, graph_database
):
    config = _config(tmp_path, "graph_recall", database=graph_database)

    response = _semantic(monkeypatch, config)

    entries = {item["file"]: item for item in response["graph"]["results"]}
    active = entries[entry_id("active")]
    assert NEIGHBOUR_NODE in active["graph_nodes"]
    assert FOREIGN_NEIGHBOUR_NODE not in active["graph_nodes"]
    # The edge went with its endpoint: a half-edge is not a subgraph.
    assert f"edge_{FOREIGN_NEIGHBOUR_NODE}" not in active["graph_edges"]
    assert f"edge_{NEIGHBOUR_NODE}" in active["graph_edges"]
    assert FOREIGN_NEIGHBOUR_NODE not in json.dumps(response["graph"])


def test_the_shared_namespace_keeps_the_foreign_expansion(
    tmp_path, monkeypatch, graph_database
):
    config = _config(tmp_path, "graph_recall", namespace="shared", database=graph_database)

    response = _semantic(monkeypatch, config)

    entries = {item["file"]: item for item in response["graph"]["results"]}
    assert FOREIGN_NEIGHBOUR_NODE in entries[entry_id("active")]["graph_nodes"]


# ---------------------------------------------------------------------------
# The fallbacks the filters may not break
# ---------------------------------------------------------------------------

def test_legacy_mode_filters_nothing_and_adds_no_key(tmp_path, monkeypatch, graph_database):
    config = _config(tmp_path, "legacy", database=graph_database)

    semantic = _semantic(monkeypatch, config)
    keyword = _recall(monkeypatch, config)

    assert "graph" not in semantic
    assert "graph" not in keyword
    assert _ids(semantic["results"]) == _ids(_rows())
    assert sorted(_files(keyword["results"])) == sorted(
        f"{domain}.md" for domain in SCOPES
    )


def test_a_safe_unavailable_graph_still_preserves_the_legacy_payload(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, "graph_recall", database=None)

    response = _semantic(monkeypatch, config)

    assert _ids(response["results"]) == _ids(_rows())
    assert response["graph"]["status"] == "graph_unavailable"
    assert response["graph"]["excluded_results"] == 0


def test_shadow_mode_observes_and_decides_nothing(tmp_path, monkeypatch, graph_database):
    config = _config(tmp_path, "shadow", database=graph_database)

    response = _semantic(monkeypatch, config)

    assert _ids(response["results"]) == _ids(_rows())
    assert response["graph"]["excluded_results"] == 0
    assert response["graph"]["authoritative"] is False


# ---------------------------------------------------------------------------
# The policy itself: what the configured namespace resolves to
# ---------------------------------------------------------------------------

class _NamespaceOnly:
    def __init__(self, namespace) -> None:
        self.namespace = namespace


@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        (NAMESPACE, frozenset({NAMESPACE, "shared"})),
        ("shared", None),
        ("", None),
        (None, None),
    ],
)
def test_the_configured_namespace_resolves_to_the_documented_union(namespace, expected):
    from layered_memory_mcp.graph_search import recall_policy

    assert recall_policy(_NamespaceOnly(namespace)).namespaces == expected


def test_a_config_without_a_namespace_narrows_nothing():
    """Conservative by default: an unknown config may not hide material."""
    from layered_memory_mcp.graph_search import recall_policy

    assert recall_policy(object()).namespaces is None


def test_the_policy_pins_one_instant_for_the_whole_read():
    from layered_memory_mcp.graph_search import recall_policy

    pinned = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    policy = recall_policy(_NamespaceOnly(NAMESPACE), now=pinned)

    assert policy.filters.as_of == pinned
    # The store-side scope stays unset on purpose: a single GraphScope cannot
    # express "own ∪ shared ∪ unscoped", so the union is screened in the layer.
    assert policy.filters.scope is None


def test_the_default_policy_reads_the_current_instant():
    from layered_memory_mcp.graph_search import recall_policy

    before = datetime.now(timezone.utc)
    policy = recall_policy(_NamespaceOnly(NAMESPACE))

    assert before <= policy.filters.as_of <= datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The policy lives in the shared layer, not in a transport
# ---------------------------------------------------------------------------

def test_no_transport_builds_its_own_filters():
    """A copy of this policy in a transport is how the three drift apart."""
    sources = {
        "server": Path(server.__file__).read_text(encoding="utf-8"),
        "api.server": _api_source(),
    }
    for name, source in sources.items():
        assert not re.search(r"\bTraversalFilters\b", source), name
        assert not re.search(r"\bGraphScope\b", source), name
        assert not re.search(r"\bas_of\b", source), name

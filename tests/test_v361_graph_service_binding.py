"""Normal factories must consume the complete binding, not adapter-only paths."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from layered_memory_mcp import graph_migration, injector, memory_compactor, promotion, server
from layered_memory_mcp.api import server as http_server
from layered_memory_mcp.dashboard_plugin import plugin_api
from layered_memory_mcp.storage import VectorStore
from layered_memory_mcp.graph_generation_runtime import (
    STATUS_CUTOVER_APPLIED, STATUS_RESTORE_APPLIED, activate_generation, restore_generation,
)
from test_v361_graph_config_binding import activated, assert_bound, config_for
from test_v361_graph_entrances import _decision
from test_v361_graph_migration import fake_embeddings, tree_state


BODY = "Archive checks run every Tuesday before the batch loader starts."
DOMAIN = "operations"
SECTION = "Archive checks"


@pytest.fixture
def factories(monkeypatch, activated):
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_v2_stores", {})
    monkeypatch.setattr(server, "_todo_store", None)
    monkeypatch.setattr(server, "_access_log_store", None)
    monkeypatch.setattr(plugin_api, "_config", None)
    monkeypatch.setattr(plugin_api, "_v2_stores", {})
    monkeypatch.setattr(plugin_api, "_todo_store", None)
    monkeypatch.setattr(http_server, "stores", {})
    monkeypatch.setattr(VectorStore, "warmup", lambda self: True)
    yield
    for factory in (server, plugin_api):
        for store in factory._v2_stores.values():
            if hasattr(store, "close"):
                store.close()


@pytest.mark.parametrize("factory", [server, plugin_api])
def test_mcp_dashboard_factories_preserve_auxiliary_paths(activated, factories, factory):
    home, source, generation, pointer = activated
    before = tree_state(source)
    stores = factory._get_v2_stores()
    assert_bound(factory._get_config(), home, generation, pointer)
    assert stores["vector"].db_path == generation / "data" / "vectors.db"
    assert stores["l1"].knowledge_dir == generation / "knowledge"
    assert stores["review"].db_path == home / "data" / "review_queue.db"
    assert factory._get_todo_store().db_path == home / "data" / "todos.db"
    if factory is server:
        assert factory._get_access_log().db_path == home / "data" / "access_log.db"
    else:
        l0 = asyncio.run(factory.get_l0_index())
        assert l0["source"] == str(generation / "L0.md")
        assert l0["total"] > 0
        consistency = asyncio.run(factory.get_consistency())
        assert consistency["missing_in_vector"] == []
    assert not (home / "data" / "vectors.db").exists()
    assert tree_state(source) == before


def test_normal_mcp_write_reopen_and_http_recall(activated, factories, monkeypatch):
    home, source, generation, pointer = activated
    before = tree_state(source)
    stores = server._get_v2_stores()
    assert stores["vector"].db_path == generation / "data" / "vectors.db"
    response = json.loads(asyncio.run(server.inject_knowledge_tool(
        domain=DOMAIN, section=SECTION, content=BODY,
        semantic_write_request=_decision("claim_archive_checks", BODY),
    )))
    assert response["success"] is True, response
    assert response["graph"]["projections_pending"] == []
    stores["vector"].close()
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_v2_stores", {})
    reopened = server._get_v2_stores()
    assert any(BODY in hit["text"] for hit in reopened["vector"].search(BODY, domain=DOMAIN))
    recalled = json.loads(asyncio.run(server.recall_knowledge(keyword="Archive checks")))
    assert BODY in json.dumps(recalled)
    with TestClient(http_server.create_app()) as client:
        assert_bound(http_server.api_config(), home, generation, pointer)
        assert http_server.stores["vector"].db_path == generation / "data" / "vectors.db"
        assert http_server.stores["l1"].knowledge_dir == generation / "knowledge"
        assert http_server.stores["review"].db_path == home / "data" / "review_queue.db"
        response = client.get("/knowledge/search", params={"q": BODY, "domain": DOMAIN})
        assert response.status_code == 200, response.text
        assert any(BODY in hit["text"] for hit in response.json())
    assert BODY in (generation / "knowledge" / f"{DOMAIN}.md").read_text(encoding="utf-8")
    assert SECTION in (generation / "L0.md").read_text(encoding="utf-8")
    for relative, query, expected in (
        ("graph.db", "SELECT COUNT(*) FROM graph_nodes WHERE id='claim_archive_checks'", 1),
        ("journal.db", "SELECT COUNT(*) FROM memory_events", 2),
        ("data/vectors.db", "SELECT COUNT(*) FROM vectors WHERE domain='operations'", 1),
    ):
        with sqlite3.connect(f"file:{generation / relative}?mode=ro", uri=True) as connection:
            assert connection.execute(query).fetchone()[0] >= expected
    assert not (home / "data" / "vectors.db").exists()
    assert tree_state(source) == before

    reopened["vector"].close()
    second_destination = pointer.parent / "second"
    report = graph_migration.apply_migration(source=source, destination=second_destination)
    assert report["status"] == graph_migration.STATUS_APPLIED
    report = activate_generation(pointer=pointer, generation=second_destination / "current", apply=True)
    assert report["status"] == STATUS_CUTOVER_APPLIED
    with TestClient(http_server.create_app()) as client:
        response = client.get("/knowledge/search", params={"q": BODY, "domain": DOMAIN})
        assert response.status_code == 200
        assert response.json() == []
    assert restore_generation(pointer=pointer, apply=True)["status"] == STATUS_RESTORE_APPLIED
    with TestClient(http_server.create_app()) as client:
        assert_bound(http_server.api_config(), home, generation, pointer)
        response = client.get("/knowledge/search", params={"q": BODY, "domain": DOMAIN})
        assert response.status_code == 200
        assert any(BODY in hit["text"] for hit in response.json())
    assert tree_state(source) == before


def test_bound_vector_maintenance_and_readers_do_not_touch_home_decoys(activated):
    home, _, generation, _ = activated
    config = config_for(home)
    decoy = build_decoy(home)
    before = tree_state(decoy)
    assert injector.vector_store_needs_reindex(config) is False
    report = injector.calibrate_thresholds(config, persist=False)
    assert report["sections"] == 3
    duplicates = promotion.scan_cross_domain_duplicates(config, threshold=0)
    assert duplicates["success"] is True
    assert duplicates["total"] > 0
    assert all("decoy" not in (item["domain_a"], item["domain_b"]) for item in duplicates["duplicates"])
    with sqlite3.connect(config.vector_db_path) as connection:
        connection.execute(
            "INSERT INTO vectors (id, domain, text, vector, metadata) "
            "SELECT 'duplicate', domain, text, vector, metadata FROM vectors LIMIT 1"
        )
    assert injector.vector_store_needs_reindex(config) is True
    report = memory_compactor.dedup_l1_knowledge(config)
    assert report["vector_pruned"] == 1
    assert injector.vector_store_needs_reindex(config) is False
    rebuilt = injector.reindex_vector_store(config, calibrate=False)
    assert rebuilt["success"] is True, rebuilt
    assert rebuilt["sections"] == 3
    assert config.vector_db_path == generation / "data" / "vectors.db"
    assert tree_state(decoy) == before


def build_decoy(home):
    data = home / "data"
    result = injector.sync_to_vector_store(data, "decoy", "## Decoy\n\nUnrelated fixture only.", replace_domain=True)
    assert result["success"] is True
    return data


def test_parameterized_vector_helpers_keep_legacy_and_explicit_targets_separate(activated):
    home, _, _, _ = activated
    config = config_for(home)
    decoy = build_decoy(home)
    before = tree_state(decoy)
    raw = f"## {SECTION}\n\n{BODY}\n"
    result = injector.sync_to_vector_store(
        decoy, DOMAIN, raw, replace_domain=True, vector_db_path=config.vector_db_path,
    )
    assert result["success"] is True
    with VectorStore(config.vector_db_path) as store:
        assert any(BODY in hit["text"] for hit in store.search(BODY, domain=DOMAIN))
    result = injector.remove_from_vector_store(decoy, DOMAIN, vector_db_path=config.vector_db_path)
    assert result["removed"] == 1
    assert tree_state(decoy) == before
    assert injector.remove_from_vector_store(decoy, "decoy")["removed"] == 1


def test_semantic_helper_reads_bound_vectors_and_full_l1_body(activated):
    home, _, _, _ = activated
    config = config_for(home)
    build_decoy(home)
    body = "The retrieval index is rebuilt nightly by the scheduler."
    result = injector._semantic_dedup(body, home / "data", "retrieval", config)
    assert result is not None
    section = result["semantic"]["hit_section"]
    assert result["semantic"]["old_body"] == injector._read_section_body(
        home / "data", "retrieval", section, config,
    )
    assert result["semantic"]["old_body"]
    assert injector._read_section_body(home / "data", "retrieval", section) is None


def test_unbound_semantic_helper_preserves_its_explicit_legacy_path(activated, monkeypatch):
    home, source, _, _ = activated
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER")
    config = config_for(home, graph_mode="legacy")
    body = "The retrieval index is rebuilt nightly by the scheduler."
    assert injector._read_section_body(
        source / "data", "retrieval", "Index maintenance", config,
    ) == body
    result = injector._semantic_dedup(body, source / "data", "retrieval", config)
    assert result is not None
    assert result["semantic"]["old_body"]
    assert not config.vector_db_path.exists()


def test_dashboard_health_keeps_the_factory_binding(activated, factories, monkeypatch):
    _, _, generation, pointer = activated
    plugin_api._get_v2_stores()
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", str(pointer.with_name("missing.json")))
    health = asyncio.run(plugin_api.get_health())
    assert health["vector"]["db_path"] == str(generation / "data" / "vectors.db")
    assert health["consistency"]["l0_size"] == (generation / "L0.md").stat().st_size


@pytest.mark.parametrize("factory", ["mcp", "http", "dashboard"])
def test_invalid_pointer_reaches_no_service_constructor(activated, factories, monkeypatch, factory):
    from layered_memory_mcp.storage import vector_store

    home, _, _, pointer = activated
    pointer.unlink()
    before = tree_state(home.parent)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid pointer reached mkdir/store/model construction")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(VectorStore, "__init__", forbidden)
    monkeypatch.setattr(vector_store, "_get_embedder", forbidden)
    with pytest.raises(ValueError, match="graph_active_pointer"):
        if factory == "http":
            async def start():
                async with http_server.lifespan(http_server.create_app()):
                    pytest.fail("invalid pointer entered HTTP lifespan")
            asyncio.run(start())
        else:
            (server if factory == "mcp" else plugin_api)._get_v2_stores()
    assert tree_state(home.parent) == before

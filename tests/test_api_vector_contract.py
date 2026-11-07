"""Optional HTTP transports use the same derived-vector contracts.

The mutating routes below are legacy-contract entrances: under the ``graph``
rollout default ``POST``/``PUT``/``DELETE /knowledge`` refuse before mutating
and name the semantic write instead, because the vector rows are a projection
of the journal there (``graph_mutation_guard.REFUSED_OPERATIONS``). That
refusal is covered by ``test_v361_graph_entrances.py``; each test here that
drives a legacy REST write asks for the rollback mode by name.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

pytest.importorskip("fastapi")
import httpx

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp import injector
from layered_memory_mcp.storage import L1Store, ReviewQueue, VectorStore
from layered_memory_mcp.storage import vector_store


@pytest.fixture
def cfg(tmp_path, monkeypatch, legacy_graph_mode):
    # ``legacy_graph_mode`` is a *dependency* rather than a usefixtures mark so
    # the ordering is guaranteed by construction: the mode has to be chosen
    # before this ``MemoryConfig`` resolves it, not merely before the test body.
    monkeypatch.setattr(vector_store, "_embed_texts", lambda texts:
                        np.ones((len(texts), 512), dtype=np.float32) / np.sqrt(512))
    config = MemoryConfig(home=str(tmp_path / "store"), auto_maintain=False)
    assert config.graph_mode == legacy_graph_mode
    return config


@pytest.mark.asyncio
async def test_rest_create_update_keep_summary_heading_and_identity(cfg, monkeypatch):
    from layered_memory_mcp.api import server as api

    stores = {"l1": L1Store(cfg.knowledge_dir), "vector": VectorStore(cfg.home / "data" / "vectors.db"),
              "review": ReviewQueue(cfg.home / "data" / "review.db")}
    monkeypatch.setattr(api, "stores", stores)
    app = api.create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/knowledge", json={
            "domain": "example", "section": "Release process — 2026-10-01",
            "summary": "Keep version 3.5 and timeout 30.", "content": "size=12",
        })
        assert created.status_code == 200, created.text
        entry_id = created.json()["id"]
        assert injector.vector_store_needs_reindex(cfg) is False
        updated = await client.put(f"/knowledge/{entry_id}", json={"content": "size=24"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["id"] == entry_id
        row, = stores["vector"].snapshot()
        assert row[2] == "Release process\nKeep version 3.5 and timeout 30.\nsize=24"
        assert json.loads(row[4])["section"] == "Release process — 2026-10-01"
        assert injector.vector_store_needs_reindex(cfg) is False
        renamed = await client.put(f"/knowledge/{entry_id}", json={
            "section": "Release process — 2026-10-02", "tags": ["updated"],
        })
        assert renamed.status_code == 200, renamed.text
        same_text, = stores["vector"].snapshot()
        assert same_text[2] == row[2]
        assert json.loads(same_text[4])["section"] == "Release process — 2026-10-02"
        assert json.loads(same_text[4])["tags"] == ["updated"]
        assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.asyncio
async def test_dashboard_rebuild_uses_atomic_section_projection(cfg, monkeypatch):
    from layered_memory_mcp.dashboard_plugin import plugin_api as dashboard

    monkeypatch.setattr(dashboard, "_get_config", lambda: cfg)
    store = VectorStore(cfg.home / "data" / "vectors.db")
    monkeypatch.setattr(dashboard, "_get_v2_stores", lambda: {"vector": store})
    (cfg.knowledge_dir / "example.md").write_text(
        "## Release process — 2026-10-01\nsize=12\n\n## Cache\nttl=30\n")
    preview = await dashboard.rebuild_vectors_endpoint(dashboard.RebuildVectorsRequest(dry_run=True))
    assert preview["dry_run"] and store.snapshot() == []
    result = await dashboard.rebuild_vectors_endpoint(dashboard.RebuildVectorsRequest(dry_run=False))
    assert result["indexed"] == 2
    assert injector.vector_store_needs_reindex(cfg) is False
    before = store.snapshot()

    def fail(texts):
        raise RuntimeError("embedding offline")

    monkeypatch.setattr(vector_store, "_embed_texts", fail)
    failed = await dashboard.rebuild_vectors_endpoint(dashboard.RebuildVectorsRequest(dry_run=False))
    assert failed["success"] is False
    assert "embedding offline" in failed["error"]
    assert store.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_write", [False, True])
async def test_rest_delayed_writer_uses_current_source(cfg, monkeypatch, newer_write):
    from layered_memory_mcp.api import server as api

    l1 = L1Store(cfg.knowledge_dir)
    store = VectorStore(cfg.home / "data" / "vectors.db")
    monkeypatch.setattr(api, "stores", {"l1": l1, "vector": store,
                                        "review": ReviewQueue(cfg.home / "data" / "review.db")})
    write = l1.write

    def delayed(entry, **kwargs):
        result = write(entry, **kwargs)
        if newer_write:
            newer = entry.model_copy(update={"content": "size=24"})
            assert write(newer)["success"]
            assert injector.reindex_vector_store(cfg, calibrate=False)["success"]
        return result

    monkeypatch.setattr(l1, "write", delayed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.create_app()),
                                base_url="http://test") as client:
        result = await client.post("/knowledge", json={"domain": "example", "section": "Pool",
                                                      "summary": "Pool settings", "content": "size=12"})
    assert result.status_code == 200, result.text
    row, = store.snapshot()
    assert ("size=24" if newer_write else "size=12") in row[2]
    assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_reindex", [False, True])
async def test_rest_embedding_overlaps_reindex(cfg, monkeypatch, concurrent_reindex):
    from layered_memory_mcp.api import server as api

    l1 = L1Store(cfg.knowledge_dir)
    store = VectorStore(cfg.home / "data" / "vectors.db")
    monkeypatch.setattr(api, "stores", {"l1": l1, "vector": store,
                                        "review": ReviewQueue(cfg.home / "data" / "review.db")})
    embed = vector_store._embed_texts
    preparing, released = threading.Event(), threading.Event()

    def overlapping_embed(texts):
        if threading.current_thread().name == threading.main_thread().name:
            preparing.set()
            assert released.wait(5)
        return embed(texts)

    monkeypatch.setattr(vector_store, "_embed_texts", overlapping_embed)

    def newer():
        assert preparing.wait(5)
        try:
            if concurrent_reindex:
                path = cfg.knowledge_dir / "example.md"
                path.write_text(path.read_text().replace("size=12", "size=24"))
                assert injector.reindex_vector_store(cfg, calibrate=False)["success"]
            return store.snapshot()
        finally:
            released.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(newer)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.create_app()),
                                    base_url="http://test") as client:
            result = await client.post("/knowledge", json={"domain": "example", "section": "Pool",
                                                          "content": "size=12"})
        committed = future.result(timeout=5)
    if concurrent_reindex:
        assert result.status_code == 503, result.text
        assert "L1 write succeeded" in result.text
        assert store.snapshot() == committed
    else:
        assert result.status_code == 200, result.text
    assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.asyncio
async def test_repeated_rest_post_keeps_current_identity_and_delete_leaves_no_orphan(cfg, monkeypatch):
    from layered_memory_mcp.api import server as api
    from layered_memory_mcp import maintenance

    l1 = L1Store(cfg.knowledge_dir)
    store = VectorStore(cfg.home / "data" / "vectors.db")
    monkeypatch.setattr(api, "stores", {"l1": l1, "vector": store,
                                        "review": ReviewQueue(cfg.home / "data" / "review.db")})
    payload = {"domain": "example", "section": "Stable heading", "summary": "Authored", "content": "size=12"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.create_app()),
                                base_url="http://test") as client:
        ids = []
        for _ in range(2):
            result = await client.post("/knowledge", json=payload)
            assert result.status_code == 200, result.text
            current_id = result.json()["id"]
            ids.append(current_id)
            row, = store.snapshot()
            assert row[0] == l1.read("example")[0]["id"] == current_id
            assert (await client.get(f"/knowledge/{current_id}")).json()["id"] == current_id
            assert injector.vector_store_needs_reindex(cfg) is False
        assert ids[0] != ids[1]
        assert (await client.get(f"/knowledge/{ids[0]}")).status_code == 404
        assert maintenance.maintain_vectors(cfg)["status"] == "healthy"
        deleted = await client.delete(f"/knowledge/{ids[1]}")
        assert deleted.status_code == 200, deleted.text
        assert l1.list_domains() == [] and store.snapshot() == []
        assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_maintenance", [False, True])
async def test_real_rest_put_overlaps_maintenance_thread(cfg, monkeypatch, concurrent_maintenance):
    from layered_memory_mcp.api import server as api
    from layered_memory_mcp import maintenance, memory_compactor

    l1 = L1Store(cfg.knowledge_dir)
    store = VectorStore(cfg.home / "data" / "vectors.db")
    monkeypatch.setattr(api, "stores", {"l1": l1, "vector": store,
                                        "review": ReviewQueue(cfg.home / "data" / "review.db")})
    monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", lambda config: {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.create_app()),
                                base_url="http://test") as client:
        created = await client.post("/knowledge", json={"domain": "example", "section": "Pool",
                                                       "content": "size=12"})
        assert created.status_code == 200, created.text
        entry_id = created.json()["id"]
        preparing, release = threading.Event(), threading.Event()
        embed = vector_store._embed_texts

        def paused(texts):
            if threading.current_thread() is threading.main_thread():
                preparing.set()
                assert release.wait(5)
            return embed(texts)

        monkeypatch.setattr(vector_store, "_embed_texts", paused)

        def maintain():
            assert preparing.wait(5)
            try:
                if concurrent_maintenance:
                    path = cfg.knowledge_dir / "example.md"
                    path.write_text(path.read_text().replace("size=24", "size=36"))
                    result = maintenance.maintenance_task(cfg)["vector_maintenance"]
                    assert result["status"] == "repaired", result
                return store.snapshot()
            finally:
                release.set()

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="real-maintenance") as pool:
            future = pool.submit(maintain)
            updated = await client.put(f"/knowledge/{entry_id}", json={"content": "size=24"})
            committed = future.result(timeout=5)
        assert updated.status_code == (503 if concurrent_maintenance else 200), updated.text
        if concurrent_maintenance:
            assert store.snapshot() == committed
        row, = store.snapshot()
        assert row[0] == entry_id
        assert ("size=36" if concurrent_maintenance else "size=24") in row[2]
        assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.asyncio
async def test_rest_shutdown_closes_observer_even_when_application_raises(cfg, monkeypatch):
    import sqlite3
    from layered_memory_mcp.api import server as api

    monkeypatch.setattr(api, "stores", {})
    monkeypatch.setattr(api, "store_dirs",
                        lambda config=None: (cfg.knowledge_dir, cfg.home / "data"))
    with pytest.raises(RuntimeError, match="application failed"):
        async with api.lifespan(api.create_app()):
            observer = api.stores["vector"]._cache_observer
            assert observer.execute("SELECT 1").fetchone()[0] == 1
            raise RuntimeError("application failed")
    assert api.stores == {}
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observer.execute("SELECT 1")

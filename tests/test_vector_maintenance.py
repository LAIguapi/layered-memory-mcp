"""Derived-vector consistency, failure preservation and maintenance regressions.

Most of this file is mode-independent: projection identity, locking, source
validation and store lifecycle behave the same whoever owns the write path, and
those tests run under the ``graph`` rollout default.

Two groups do not, and each one asks for ``legacy_graph_mode`` by name:

* ``maintenance.maintain_vectors`` — the unattended twin of
  ``reindex_vector_store_tool``. Under the graph default it is a *reported
  suspension* (``graph_mutation_guard.MAINTENANCE_JOBS``), because rebuilding
  the vector rows from the L1 files they were projected onto would overwrite a
  projection the semantic journal owns;
* the whole-file MCP writers ``create_knowledge_file`` / ``update_knowledge_file``,
  which the graph default refuses before mutating.

Both suspensions/refusals are covered in graph mode by
``test_v361_graph_maintenance_bypass.py`` and ``test_v361_graph_entrances.py``.
"""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp import injector
from layered_memory_mcp.storage import vector_store


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    home = tmp_path / "store"
    (home / "knowledge").mkdir(parents=True)
    monkeypatch.setattr(
        vector_store, "_embed_texts",
        lambda texts: np.ones((len(texts), vector_store.EMBED_DIM), dtype=np.float32)
        / np.sqrt(vector_store.EMBED_DIM),
    )
    return MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"),
                        auto_maintain=False)


def write_source(cfg, text, domain="example"):
    path = cfg.knowledge_dir / f"{domain}.md"
    path.write_text(text, encoding="utf-8")
    return path


def rows(cfg):
    db = cfg.home / "data" / "vectors.db"
    if not db.exists():
        return []
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT id, domain, text, vector, metadata FROM vectors ORDER BY id"
        ).fetchall()


def rebuild(cfg):
    result = injector.reindex_vector_store(cfg, calibrate=False)
    assert result["success"], result
    return result


@pytest.mark.parametrize("text", ["", "# Empty\n", "# Empty\n\n## Heading\n\n", "## Heading\n"])
def test_zero_body_sections_are_healthy_without_database(cfg, text):
    write_source(cfg, text)
    assert injector.vector_store_needs_reindex(cfg) is False
    assert not (cfg.home / "data" / "vectors.db").exists()


def test_missing_then_consistent_with_empty_domain(cfg):
    write_source(cfg, "# Knowledge\n\n## Pool\nsize=12\n")
    write_source(cfg, "# Reserved\n\n## Pending\n", "reserved")
    assert injector.vector_store_needs_reindex(cfg) is True
    rebuild(cfg)
    assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.parametrize("change", ["body", "heading", "section", "extra", "orphan", "all_deleted"])
def test_exact_projection_detects_stale_equal_count_and_orphans(cfg, change):
    path = write_source(cfg, "## Pool\nsize=12\n\n## Cache\nttl=30\n")
    rebuild(cfg)
    assert injector.vector_store_needs_reindex(cfg) is False
    if change == "body":
        path.write_text("## Pool\nsize=24\n\n## Cache\nttl=30\n", encoding="utf-8")
    elif change == "heading":
        path.write_text("## Pool renamed\nsize=12\n\n## Cache\nttl=30\n", encoding="utf-8")
    elif change == "section":
        path.write_text("## Pool\nsize=12\n\n## Queue\nlimit=8\n", encoding="utf-8")
    elif change == "extra":
        path.write_text("## Pool\nsize=12\n", encoding="utf-8")
    elif change == "orphan":
        path.rename(path.with_name("renamed.md"))
    else:
        path.unlink()
    assert injector.vector_store_needs_reindex(cfg) is True
    rebuild(cfg)
    assert injector.vector_store_needs_reindex(cfg) is False


def test_inspection_is_read_only(cfg):
    write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)
    db = cfg.home / "data" / "vectors.db"
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    assert injector.vector_store_needs_reindex(cfg) is False
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_corrupt_database_is_not_reported_healthy(cfg):
    write_source(cfg, "## Pool\nsize=12\n")
    (cfg.home / "data").mkdir()
    (cfg.home / "data" / "vectors.db").write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        injector.vector_store_needs_reindex(cfg)


def test_unreadable_source_is_not_reported_healthy(cfg, monkeypatch):
    path = write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)
    real_read = Path.read_text

    def read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("source unreadable")
        return real_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError, match="source unreadable"):
        injector.vector_store_needs_reindex(cfg)


@pytest.mark.parametrize("operation", ["domain", "full"])
def test_failed_embedding_preserves_all_committed_vectors(cfg, monkeypatch, operation):
    write_source(cfg, "## Pool\nsize=12\n")
    write_source(cfg, "## Cache\nttl=30\n", "other")
    rebuild(cfg)
    before = rows(cfg)
    write_source(cfg, "## Pool\nsize=24\n\n## New\nlimit=8\n")

    def fail(texts):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(vector_store, "_embed_texts", fail)
    if operation == "domain":
        result = injector.sync_to_vector_store(
            cfg.home / "data", "example", (cfg.knowledge_dir / "example.md").read_text(),
            replace_domain=True,
        )
    else:
        result = injector.reindex_vector_store(cfg, calibrate=False)
    assert result["success"] is False, result
    assert rows(cfg) == before


def test_duplicate_section_multiplicity_converges(cfg):
    write_source(cfg, "## Pool\nsize=12\n\n## Pool\nsize=12\n")
    result = rebuild(cfg)
    assert result["sections"] == len(rows(cfg)) == 2
    assert injector.vector_store_needs_reindex(cfg) is False


def test_embedding_heading_keeps_display_metadata_and_original_source(cfg):
    text = "# Knowledge\n\n## Release process — 2026-10-01\nKeep exact parameters: n=12.\n"
    path = write_source(cfg, text)
    rebuild(cfg)
    row = rows(cfg)[0]
    assert row[2] == "Release process\nKeep exact parameters: n=12."
    assert json.loads(row[4])["section"] == "Release process — 2026-10-01"
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("concurrent_write", [False, True])
@pytest.mark.parametrize("operation", ["domain", "full"])
def test_publication_detects_concurrent_writer(cfg, monkeypatch, concurrent_write, operation):
    path = write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)
    preparing = threading.Event()
    release = threading.Event()
    embed = vector_store._embed_texts

    def blocked_embed(texts):
        if threading.current_thread().name.startswith("maintenance-test"):
            preparing.set()
            assert release.wait(5), "test must release preparation"
        return embed(texts)

    monkeypatch.setattr(vector_store, "_embed_texts", blocked_embed)

    def prepare():
        if operation == "full":
            return injector.reindex_vector_store(cfg, calibrate=False)
        return injector.sync_to_vector_store(
            cfg.home / "data", "example", path.read_text(),
            replace_domain=True, source_path=path,
        )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="maintenance-test") as pool:
        future = pool.submit(prepare)
        try:
            assert preparing.wait(5)
            if concurrent_write:
                path.write_text("## Pool\nsize=24\n", encoding="utf-8")
                writer = injector.sync_to_vector_store(
                    cfg.home / "data", "example", path.read_text(),
                    replace_domain=True, source_path=path,
                )
                assert writer["success"], writer
            committed = rows(cfg)
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result["success"] is (not concurrent_write), result
    if concurrent_write:
        assert rows(cfg) == committed
        assert "size=24" in rows(cfg)[0][2]
    assert injector.vector_store_needs_reindex(cfg) is False


def test_delayed_sync_rereads_source_instead_of_overwriting_newer_writer(cfg):
    path = write_source(cfg, "## Pool\nsize=12\n")
    old_snapshot = path.read_text()
    rebuild(cfg)
    path.write_text("## Pool\nsize=24\n", encoding="utf-8")
    rebuild(cfg)
    result = injector.sync_to_vector_store(
        cfg.home / "data", "example", old_snapshot,
        replace_domain=True, source_path=path,
    )
    assert result["success"], result
    assert "size=24" in rows(cfg)[0][2]
    assert injector.vector_store_needs_reindex(cfg) is False


def test_source_change_before_writer_sync_aborts_reindex(cfg, monkeypatch):
    path = write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)
    before = rows(cfg)
    embed = vector_store._embed_texts

    def change_source(texts):
        path.write_text("## Pool\nsize=24\n", encoding="utf-8")
        return embed(texts)

    monkeypatch.setattr(vector_store, "_embed_texts", change_source)
    result = injector.reindex_vector_store(cfg, calibrate=False)
    assert result["success"] is False
    assert rows(cfg) == before
    assert injector.vector_store_needs_reindex(cfg) is True


def test_existing_reader_sees_replacement_from_another_store(cfg):
    write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)
    reader = vector_store.VectorStore(cfg.home / "data" / "vectors.db")
    assert "size=12" in reader.search("pool")[0]["text"]
    write_source(cfg, "## Pool\nsize=24\n")
    rebuild(cfg)
    assert "size=24" in reader.search("pool")[0]["text"]


@pytest.mark.usefixtures("legacy_graph_mode")  # whole-file writers; see module docstring
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "inject"])
async def test_mcp_writers_pass_current_source_to_sync(cfg, monkeypatch, operation):
    from layered_memory_mcp import server

    server._config = cfg
    if operation != "create":
        write_source(cfg, "## Pool\nsize=12\n")
    sync = injector.sync_to_vector_store
    seen = []

    def capture(*args, **kwargs):
        seen.append(kwargs.get("source_path"))
        return sync(*args, **kwargs)

    monkeypatch.setattr(server, "sync_to_vector_store", capture)
    monkeypatch.setattr(injector, "sync_to_vector_store", capture)
    if operation == "create":
        result = json.loads(await server.create_knowledge_file("example.md", "## Pool\nsize=24\n"))
    elif operation == "update":
        result = json.loads(await server.update_knowledge_file("example.md", "## Pool\nsize=24\n"))
    else:
        result = injector.inject_knowledge(cfg, "example", "Cache", "ttl=30", mode="append")
    assert result["success"], result
    assert seen == [cfg.knowledge_dir / "example.md"]
    assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.usefixtures("legacy_graph_mode")  # maintain_vectors; see module docstring
def test_maintenance_repairs_once_then_healthy_ticks_do_not_embed(cfg, monkeypatch):
    from layered_memory_mcp import maintenance, memory_compactor

    monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", lambda config: {})
    write_source(cfg, "## Pool\nsize=12\n")
    result = maintenance.maintenance_task(cfg)
    assert result["vector_maintenance"]["status"] == "repaired"
    assert injector.vector_store_needs_reindex(cfg) is False
    monkeypatch.setattr(vector_store, "_embed_texts", lambda texts: pytest.fail("healthy tick must not embed"))
    assert maintenance.maintenance_task(cfg)["vector_maintenance"]["status"] == "rate_limited"
    state = cfg.home / "data" / "vector_maintenance.json"
    checkpoint = json.loads(state.read_text())
    checkpoint["next_attempt"] = 0
    state.write_text(json.dumps(checkpoint))
    assert maintenance.maintenance_task(cfg)["vector_maintenance"]["status"] == "healthy"


@pytest.mark.usefixtures("legacy_graph_mode")  # maintain_vectors; see module docstring
def test_maintenance_failure_backoff_and_retry_preserve_old_rows(cfg, monkeypatch):
    from layered_memory_mcp import maintenance, memory_compactor

    monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", lambda config: {})
    path = write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)
    before = rows(cfg)
    path.write_text("## Pool\nsize=24\n")
    embed = vector_store._embed_texts

    def fail(texts):
        raise RuntimeError("model offline")

    monkeypatch.setattr(vector_store, "_embed_texts", fail)
    first = maintenance.maintenance_task(cfg)["vector_maintenance"]
    assert first["status"] == "error"
    assert "model offline" in first["error"]
    assert rows(cfg) == before
    second = maintenance.maintenance_task(cfg)["vector_maintenance"]
    assert second["status"] == "rate_limited"
    assert second["last_status"] == "error"
    state = cfg.home / "data" / "vector_maintenance.json"
    checkpoint = json.loads(state.read_text())
    checkpoint["next_attempt"] = 0
    state.write_text(json.dumps(checkpoint))
    monkeypatch.setattr(vector_store, "_embed_texts", embed)
    retry = maintenance.maintenance_task(cfg)["vector_maintenance"]
    assert retry["status"] == "repaired"
    assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.usefixtures("legacy_graph_mode")  # maintain_vectors; see module docstring
def test_maintenance_inspection_error_is_explicit_and_disabled_is_noop(cfg, monkeypatch):
    from layered_memory_mcp import maintenance, memory_compactor

    monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", lambda config: {})

    def fail(config):
        raise PermissionError("inspection forbidden")

    monkeypatch.setattr(injector, "vector_store_needs_reindex", fail)
    result = maintenance.maintenance_task(cfg)["vector_maintenance"]
    assert result["status"] == "error"
    assert "inspection forbidden" in result["error"]
    cfg.maintenance_enabled = False
    assert maintenance.maintenance_task(cfg)["vector_maintenance"]["status"] == "disabled"


@pytest.mark.usefixtures("legacy_graph_mode")  # maintain_vectors; see module docstring
def test_maintenance_overlap_is_deferred(cfg, monkeypatch):
    from filelock import FileLock
    from layered_memory_mcp import maintenance, memory_compactor

    monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", lambda config: {})
    (cfg.home / "data").mkdir(exist_ok=True)
    with FileLock(str(cfg.home / "data" / "vector_maintenance.lock")):
        assert maintenance.maintenance_task(cfg)["vector_maintenance"]["status"] == "busy"


def test_frontmatter_entry_contract_preserves_summary_across_create_update_reindex(cfg):
    """Exercise the exact L1Store.write + VectorStore.add contract used by REST."""
    from layered_memory_mcp.models import KnowledgeEntry
    from layered_memory_mcp.storage import L1Store

    l1 = L1Store(cfg.knowledge_dir)
    store = vector_store.VectorStore(cfg.home / "data" / "vectors.db")
    entry = KnowledgeEntry(domain="example", section="Release process — 2026-10-01",
                           summary="Keep version 3.5 and timeout 30 in the authored summary.",
                           content="No H2 is required by the frontmatter entry contract.")
    for body in (entry.content, "Updated body retains n=12 and timeout=30."):
        entry.content = body
        assert l1.write(entry)["success"]
        store.add(entry)
        row = rows(cfg)[0]
        assert row[2] == f"Release process\n{entry.summary}\n{body}"
        assert json.loads(row[4])["section"] == entry.section
        assert injector.vector_store_needs_reindex(cfg) is False
        rebuild(cfg)
        assert rows(cfg)[0][2] == row[2]
        assert injector.vector_store_needs_reindex(cfg) is False
        meta, current = l1.read("example")
        assert meta["section"] == entry.section
        assert meta["summary"] == entry.summary
        assert current.strip() == body
        loaded = KnowledgeEntry.from_markdown((cfg.knowledge_dir / "example.md").read_text())
        assert loaded.id == entry.id
        entry = loaded


def test_namespace_empty_override_and_crlf_projection(cfg):
    namespaced = MemoryConfig(home=str(cfg.home), namespace="tenant")
    shared = namespaced.knowledge_dirs[1]
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "example.md").write_text("## Shared\nbody\n")
    (namespaced.knowledge_dir / "example.md").write_text("# Empty override\n")
    assert injector.vector_store_needs_reindex(namespaced) is False
    path = namespaced.knowledge_dir / "example.md"
    path.write_bytes(b"# Notes\r\n\r\n## Pool\r\nsize=12\r\n")
    rebuild(namespaced)
    assert injector.vector_store_needs_reindex(namespaced) is False


def test_sql_publication_failure_rolls_back_deleted_rows(cfg):
    write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)
    before = rows(cfg)
    write_source(cfg, "## Pool\nsize=24\n\n## Cache\nttl=30\n")
    with sqlite3.connect(cfg.home / "data" / "vectors.db") as conn:
        conn.execute("""CREATE TRIGGER reject_cache BEFORE INSERT ON vectors
                        WHEN NEW.text LIKE 'Cache%'
                        BEGIN SELECT RAISE(ABORT, 'publication failed'); END""")
    result = injector.reindex_vector_store(cfg, calibrate=False)
    assert result["success"] is False
    assert "publication failed" in result["error"]
    assert rows(cfg) == before


def test_arbitrary_domain_pull_scan_does_not_expand_write_scan(cfg, monkeypatch):
    from layered_memory_mcp import promotion

    path = write_source(cfg, "".join(f"## Pool {i}\nconnection size={i}\n\n" for i in range(5)),
                        "project-zeta")
    assert not promotion.is_watched_domain(cfg, "project-zeta")
    result = promotion.scan_split_candidates(cfg)
    assert result["success"] and result["scanned"] == 1
    assert [candidate["domain"] for candidate in result["candidates"]] == ["project-zeta"]
    monkeypatch.setattr(promotion, "_embed_sections", lambda texts:
                        pytest.fail("ordinary writes must not inherit the broad pull scope"))
    assert promotion.detect_promotion_candidate(cfg, "project-zeta", path) is None


@pytest.mark.usefixtures("legacy_graph_mode")  # maintain_vectors; see module docstring
@pytest.mark.parametrize("interval,expected", [(0, 1800), (1, 30), (120, 120),
                                               (float("nan"), 1800), (float("inf"), 1800)])
def test_loop_and_vector_retry_share_interval_resolution(cfg, monkeypatch, interval, expected):
    from layered_memory_mcp import maintenance

    cfg.maintenance_tick_seconds = interval
    monkeypatch.setattr(maintenance.MaintenanceLoop, "start", lambda self: True)
    monkeypatch.setattr(maintenance.time, "time", lambda: 1000)
    loop = maintenance.start_maintenance(cfg)
    report = maintenance.maintain_vectors(cfg)
    assert loop._interval == expected
    assert report["next_attempt"] == 1000 + expected


@pytest.mark.usefixtures("legacy_graph_mode")  # maintain_vectors; see module docstring
def test_maintenance_projects_changes_from_same_tick_compaction(cfg, monkeypatch):
    from layered_memory_mcp import maintenance, memory_compactor

    write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)

    def compact(config):
        write_source(config, "## Pool\nsize=24\n")
        return {"changed": True}

    monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", compact)
    result = maintenance.maintenance_task(cfg)
    assert result["vector_maintenance"]["status"] == "repaired"
    assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.usefixtures("legacy_graph_mode")  # whole-file writers; see module docstring
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_mcp_file_writes_surface_vector_failure(cfg, monkeypatch, operation, caplog):
    from layered_memory_mcp import server

    monkeypatch.setattr(server, "_config", cfg)
    if operation == "update":
        write_source(cfg, "## Pool\nsize=12\n")
        rebuild(cfg)
    before = rows(cfg)

    def fail(texts):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(vector_store, "_embed_texts", fail)
    tool = server.create_knowledge_file if operation == "create" else server.update_knowledge_file
    result = json.loads(await tool("example.md", "## Pool\nsize=24\n"))
    assert result["success"] is True  # The source file write did succeed.
    assert result["vector_sync_ok"] is False
    assert result["vector_sync_error"] == "vector_sync_failed"
    assert "model unavailable" not in json.dumps(result) + caplog.text
    assert rows(cfg) == before


@pytest.mark.parametrize("writer", ["inject", "frontmatter"])
def test_waiting_writer_and_new_writer_use_the_same_lock_inode(cfg, monkeypatch, writer):
    """A waiter may acquire the old lock before the preceding writer exits."""
    from filelock import FileLock, Timeout
    from layered_memory_mcp.models import KnowledgeEntry
    from layered_memory_mcp.storage import L1Store

    path = write_source(cfg, "## Pool\nsize=12\n")
    lock_path = path.with_suffix(".md.lock")
    released, waiter_acquired = threading.Event(), threading.Event()
    release = FileLock.release

    def pause_after_release(lock, *args, **kwargs):
        release(lock, *args, **kwargs)
        if threading.current_thread().name.startswith("first-writer") and not released.is_set():
            released.set()
            assert waiter_acquired.wait(5)

    monkeypatch.setattr(FileLock, "release", pause_after_release)

    def first_writer():
        if writer == "inject":
            return injector._execute_write(path, path.name, "Cache", "ttl=30", "append", None, cfg)
        return L1Store(cfg.knowledge_dir).write(
            KnowledgeEntry(domain="example", section="Pool", content="size=24"))

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-writer") as pool:
        future = pool.submit(first_writer)
        assert released.wait(5)
        with FileLock(str(lock_path), timeout=2):
            waiter_acquired.set()
            assert future.result(timeout=5)["success"]
            with pytest.raises(Timeout):
                with FileLock(str(lock_path), timeout=0):
                    pytest.fail("a new inode let a third writer bypass the waiting writer")


@pytest.mark.parametrize("replacement", ["domain", "full"])
@pytest.mark.parametrize("concurrent_write", [False, True])
def test_slow_add_cannot_overwrite_newer_replacement(cfg, monkeypatch, replacement, concurrent_write):
    from layered_memory_mcp.models import KnowledgeEntry
    from layered_memory_mcp.storage import L1Store

    l1 = L1Store(cfg.knowledge_dir)
    entry = KnowledgeEntry(domain="example", section="Pool", content="size=12")
    assert l1.write(entry)["success"]
    rebuild(cfg)
    entry.content = "size=24"
    assert l1.write(entry)["success"]
    store = vector_store.VectorStore(cfg.home / "data" / "vectors.db")
    preparing, release = threading.Event(), threading.Event()
    embed = vector_store._embed_texts

    def paused(texts):
        if threading.current_thread().name.startswith("slow-add"):
            preparing.set()
            assert release.wait(5)
        return embed(texts)

    monkeypatch.setattr(vector_store, "_embed_texts", paused)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-add") as pool:
        future = pool.submit(store.add, entry.model_copy(deep=True))
        try:
            assert preparing.wait(5)
            if concurrent_write:
                entry.content = "size=36"
                assert l1.write(entry)["success"]
                if replacement == "full":
                    rebuild(cfg)
                else:
                    path = cfg.knowledge_dir / "example.md"
                    result = injector.sync_to_vector_store(
                        cfg.home / "data", "example", path.read_text(),
                        replace_domain=True, source_path=path,
                    )
                    assert result["success"], result
            committed = rows(cfg)
        finally:
            release.set()
        if concurrent_write:
            with pytest.raises(RuntimeError, match="changed during preparation"):
                future.result(timeout=5)
            assert rows(cfg) == committed
        else:
            future.result(timeout=5)
    row, = rows(cfg)
    assert row[0] == entry.id
    assert ("size=36" if concurrent_write else "size=24") in row[2]
    assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.parametrize("changed", [False, True])
def test_add_validates_source_even_before_newer_vectors_exist(cfg, monkeypatch, changed):
    from layered_memory_mcp.models import KnowledgeEntry
    from layered_memory_mcp.storage import L1Store

    entry = KnowledgeEntry(domain="example", section="Pool", content="size=12")
    l1 = L1Store(cfg.knowledge_dir)
    assert l1.write(entry)["success"]
    rebuild(cfg)
    before = rows(cfg)
    entry.content = "size=24"
    assert l1.write(entry)["success"]
    path = cfg.knowledge_dir / "example.md"
    expected_source = path.read_text()
    embed = vector_store._embed_texts

    def prepare(texts):
        if changed:
            newer = entry.model_copy(update={"content": "size=36"})
            assert l1.write(newer)["success"]
        return embed(texts)

    def validate_source():
        if path.read_text() != expected_source:
            raise RuntimeError("source changed during preparation")

    monkeypatch.setattr(vector_store, "_embed_texts", prepare)
    store = vector_store.VectorStore(cfg.home / "data" / "vectors.db")
    if changed:
        with pytest.raises(RuntimeError, match="source changed"):
            store.add(entry, validate_source=validate_source)
        assert rows(cfg) == before
        assert injector.vector_store_needs_reindex(cfg) is True
    else:
        store.add(entry, validate_source=validate_source)
        assert injector.vector_store_needs_reindex(cfg) is False


@pytest.mark.usefixtures("legacy_graph_mode")  # maintain_vectors; see module docstring
@pytest.mark.parametrize("frontmatter", [False, True])
def test_stable_frontmatter_id_drift_is_repaired_without_random_h2_false_alarm(cfg, frontmatter):
    from layered_memory_mcp import maintenance
    from layered_memory_mcp.models import KnowledgeEntry
    from layered_memory_mcp.storage import L1Store

    if frontmatter:
        entry = KnowledgeEntry(domain="example", section="Pool", summary="Authored", content="size=12")
        l1 = L1Store(cfg.knowledge_dir)
        assert l1.write(entry)["success"]
    else:
        write_source(cfg, "## Pool\nsize=12\n")
    rebuild(cfg)
    old_id = rows(cfg)[0][0]
    assert injector.vector_store_needs_reindex(cfg) is False
    if frontmatter:
        entry.id = "replacement-identifier"
        assert l1.write(entry)["success"]
        assert injector.vector_store_needs_reindex(cfg) is True
        assert maintenance.maintain_vectors(cfg)["status"] == "repaired"
        assert rows(cfg)[0][0] == entry.id != old_id
    else:
        rebuild(cfg)
        assert rows(cfg)[0][0] == old_id  # H2 IDs are derived deterministically from the heading, so rebuilds reproduce them.
        assert maintenance.maintain_vectors(cfg)["status"] == "healthy"
    assert injector.vector_store_needs_reindex(cfg) is False


def test_vector_store_close_is_idempotent_and_context_closes(cfg):
    store = vector_store.VectorStore(cfg.home / "data" / "vectors.db")
    observer = store._cache_observer
    store.close()
    store.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observer.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="closed"):
        store.search("pool")
    with pytest.raises(RuntimeError, match="closed"):
        store.snapshot()
    with vector_store.VectorStore(cfg.home / "data" / "vectors.db") as other:
        another_observer = other._cache_observer
        assert other.snapshot() == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        another_observer.execute("SELECT 1")


@pytest.mark.parametrize("operation", ["sync", "reindex"])
@pytest.mark.parametrize("fails", [False, True])
def test_short_lived_vector_operations_close_observer_even_on_failure(cfg, monkeypatch, operation, fails):
    path = write_source(cfg, "## Pool\nsize=12\n")
    instances = []
    initialize = vector_store.VectorStore.__init__

    def capture(store, *args, **kwargs):
        initialize(store, *args, **kwargs)
        instances.append(store)

    monkeypatch.setattr(vector_store.VectorStore, "__init__", capture)
    if fails:
        def fail(texts):
            raise RuntimeError("embedding offline")
        monkeypatch.setattr(vector_store, "_embed_texts", fail)
    if operation == "sync":
        result = injector.sync_to_vector_store(cfg.home / "data", "example", path.read_text(),
                                              replace_domain=True, source_path=path)
    else:
        result = injector.reindex_vector_store(cfg, calibrate=False)
    assert result["success"] is (not fails)
    assert instances
    for instance in instances:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            instance._cache_observer.execute("SELECT 1")

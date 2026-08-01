"""v3.1.0 semantic-upsert write-path tests.

Root cause fixed here: inject_knowledge's dedup used difflib.SequenceMatcher on
"new content vs whole file", which (a) skipped comparison entirely once a file
grew (15:1 length pre-filter) and (b) is char-level LCS that can't see a Chinese
reword. Net effect: knowledge that should UPDATE an existing section was blindly
appended, so files snowballed and knowledge evolution ("two systems → three
systems") accumulated instead of replacing.

The fix rewires _check_dedup onto the already-existing section-level bge-small-zh
vector store (VectorStore.search), with deterministic cosine bands:
    >= 0.95 skip / >= 0.72 defer_fusion / >= 0.55 merge / else append,
plus a passive cross-domain duplicate scan and a full reindex capability.

These tests require the bge-small-zh model (fastembed). It loads in ~1s once
cached; the model is already present in this environment.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.injector import (
    inject_knowledge,
    reindex_vector_store,
    vector_store_needs_reindex,
    _check_dedup,
    _resolve_action,
)


# --- Skip cleanly if the embedding model can't be loaded (offline CI) --------

def _model_available() -> bool:
    try:
        from layered_memory_mcp.storage.vector_store import _embed_texts
        v = _embed_texts(["探针"])
        return v.shape[0] == 1
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _model_available(),
    reason="bge-small-zh embedding model unavailable (offline)",
)


def _mk_config(tmp_path):
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"))


# A neutral technical knowledge pair whose reword lands in the "same knowledge"
# band (~0.88 measured). Kept generic so the open-source test suite reveals
# nothing about any user's domain.
_ORIGINAL = (
    "应用启动时会先加载全局配置文件，解析其中的数据库连接串和缓存地址，"
    "然后初始化连接池等待请求。"
)
_REWORD = (
    "服务启动阶段先读取配置文件，从中解析出数据库连接信息与缓存服务器地址，"
    "接着建立连接池准备接收请求。"
)
_UNRELATED = (
    "前端页面的国际化方案采用按语言拆分的资源文件，"
    "运行时根据浏览器语言标签动态加载对应文案。"
)


# --- Core: same-knowledge reword defers fusion instead of appending ----------

def test_reword_triggers_deferred_fusion(tmp_path):
    cfg = _mk_config(tmp_path)
    # Seed a section.
    r1 = inject_knowledge(cfg, "boot", "框架总览", _ORIGINAL, mode="upsert")
    assert r1["success"] and r1["action"] in ("created", "appended")

    # Reword the same knowledge → must NOT append; must defer fusion.
    r2 = inject_knowledge(cfg, "boot", "框架补充", _REWORD, mode="upsert")
    assert r2["action"] == "deferred_fusion", r2
    assert r2["needs_fusion"] is True
    fusion = r2["fusion"]
    assert fusion["hit_section"] == "框架总览"
    assert fusion["old_body"] and _ORIGINAL[:8] in fusion["old_body"]
    assert fusion["new_content"] == _REWORD
    assert fusion["cosine"] >= 0.72

    # The file must still contain only ONE section (nothing was written).
    text = (Path(cfg.home) / "knowledge" / "boot.md").read_text(encoding="utf-8")
    assert text.count("## ") == 1, "deferred fusion must not write anything"


def test_fusion_writeback_replaces_section(tmp_path):
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "boot", "框架总览", _ORIGINAL, mode="upsert")

    r = inject_knowledge(cfg, "boot", "框架补充", _REWORD, mode="upsert")
    assert r["action"] == "deferred_fusion"
    hit = r["fusion"]["hit_section"]

    fused = _ORIGINAL + "\n补充：" + _REWORD
    r3 = inject_knowledge(
        cfg, "boot", hit, fused, mode="upsert", fuse=True
    )
    assert r3["success"] and r3["action"] == "replaced", r3

    text = (Path(cfg.home) / "knowledge" / "boot.md").read_text(encoding="utf-8")
    # Still one section, now carrying the fused body.
    assert text.count("## 框架总览") == 1
    assert "补充：" in text
    # No duplicate '框架补充' section was created.
    assert "## 框架补充" not in text


# --- Unrelated content appends as a genuinely new section --------------------

def test_unrelated_content_appends(tmp_path):
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "boot", "框架总览", _ORIGINAL, mode="upsert")

    r = inject_knowledge(cfg, "boot", "国际化", _UNRELATED, mode="upsert")
    assert r["action"] in ("appended", "section_created"), r
    assert not r.get("needs_fusion")

    text = (Path(cfg.home) / "knowledge" / "boot.md").read_text(encoding="utf-8")
    assert "## 框架总览" in text and "## 国际化" in text


# --- Exact duplicate still short-circuits (Layer 1 preserved) ----------------

def test_exact_duplicate_still_skipped(tmp_path):
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "boot", "框架总览", _ORIGINAL, mode="upsert")
    r = inject_knowledge(cfg, "boot", "again", _ORIGINAL, mode="upsert")
    assert r["action"] == "skipped"
    assert r["dedup"]["match_kind"] == "exact"


# --- append mode still force-appends (backward compat) -----------------------

def test_append_mode_forces_append_on_reword(tmp_path):
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "boot", "框架总览", _ORIGINAL, mode="upsert")
    r = inject_knowledge(cfg, "boot", "框架总览", _REWORD, mode="append")
    # append never defers fusion; a reword (not near-verbatim) is added.
    assert r["action"] in ("appended", "section_created"), r
    assert not r.get("needs_fusion")


# --- Cross-domain duplicate: write path stays silent (v3.1.2 slimming) -------

def test_cross_domain_duplicate_not_hinted_on_write_path(tmp_path):
    """v3.1.2: the write path no longer runs a global cross-file scan.

    Previously every inject searched all namespaces + shared and emitted
    cross_domain_hints / shared_hit_hint / cross_namespace_hints. That heavy,
    best-effort global op was REMOVED from the write path (Task 2) — cross-
    library duplicate detection is now a PULL operation (reconcile). So a
    cross-domain near-duplicate must NOT surface any of those hints on write;
    the write in the fresh domain still succeeds and stays independent.
    """
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "svc_notes", "启动流程", _ORIGINAL, mode="upsert")

    r = inject_knowledge(cfg, "svc_config", "启动说明", _REWORD, mode="upsert")
    assert r["success"]
    # None of the removed cross-library hint keys may appear on the write path.
    assert "cross_domain_hints" not in r, r
    assert "shared_hit_hint" not in r, r
    assert "cross_namespace_hints" not in r, r
    # Both files still exist independently — no cross-file auto-merge.
    kdir = Path(cfg.home) / "knowledge"
    assert (kdir / "svc_notes.md").exists()
    assert (kdir / "svc_config.md").exists()

    # And the reconcile-side scan DOES find the cross-file duplicate (moved home).
    from layered_memory_mcp.injector import reindex_vector_store
    from layered_memory_mcp.promotion import scan_cross_domain_duplicates
    reindex_vector_store(cfg)
    dups = scan_cross_domain_duplicates(cfg)
    assert dups["success"]
    pair_domains = {
        frozenset((d["domain_a"], d["domain_b"])) for d in dups["duplicates"]
    }
    assert frozenset(("svc_notes", "svc_config")) in pair_domains, dups


# --- Reindex correctness -----------------------------------------------------

def test_reindex_rebuilds_from_markdown(tmp_path):
    cfg = _mk_config(tmp_path)
    kdir = Path(cfg.home) / "knowledge"
    # Write a multi-section file directly (simulate external/legacy files whose
    # vectors were never synced).
    (kdir / "infra.md").write_text(
        "# infra\n\n## 代理\n用 clash 分流。\n\n## 网络\nWSL2 端口转发。\n",
        encoding="utf-8",
    )
    (kdir / "misc.md").write_text(
        "# misc\n\n## 杂项\n一些随手记。\n",
        encoding="utf-8",
    )

    # Vector store is empty / missing coverage → needs reindex.
    assert vector_store_needs_reindex(cfg) is True

    res = reindex_vector_store(cfg)
    assert res["success"], res
    assert res["files"] == 2
    assert res["domains"] == 2
    assert res["sections"] == 3  # 代理 + 网络 + 杂项

    # After reindex the coverage check is satisfied.
    assert vector_store_needs_reindex(cfg) is False

    # And a subsequent semantic search finds the reindexed section.
    from layered_memory_mcp.storage.vector_store import VectorStore
    store = VectorStore(Path(cfg.home) / "data" / "vectors.db")
    hits = store.search("clash 代理分流", top_n=3, domain="infra")
    assert hits and hits[0]["score"] > 0.5


def test_reindex_drops_orphans(tmp_path):
    cfg = _mk_config(tmp_path)
    kdir = Path(cfg.home) / "knowledge"
    (kdir / "a.md").write_text("# a\n\n## s1\nbody one.\n", encoding="utf-8")
    reindex_vector_store(cfg)

    # Remove the file, reindex → its vectors must be gone (orphan reaped).
    (kdir / "a.md").unlink()
    res = reindex_vector_store(cfg)
    assert res["success"]
    from layered_memory_mcp.storage.vector_store import VectorStore
    store = VectorStore(Path(cfg.home) / "data" / "vectors.db")
    assert store.stats()["total_entries"] == 0


# --- Graceful degradation: no vector store → falls back, never crashes --------

def test_dedup_falls_back_when_no_vector_store(tmp_path):
    cfg = _mk_config(tmp_path)
    kdir = Path(cfg.home) / "knowledge"
    (kdir / "x.md").write_text("# x\n\n## s\n" + _ORIGINAL + "\n", encoding="utf-8")
    # data_dir has no vectors.db yet → semantic layer returns None → fuzzy path.
    res = _check_dedup(
        _ORIGINAL,
        str(kdir),
        0.7,
        data_dir=str(Path(cfg.home) / "data"),
        domain="x",
        config=cfg,
    )
    # Exact layer should catch the verbatim copy regardless.
    assert res["match_kind"] == "exact"
    assert res["suggestion"] == "skip"


# --- _resolve_action mapping for semantic suggestions ------------------------

def test_resolve_action_semantic_bands():
    # defer_fusion in upsert → deferred_fusion; in merge → merged.
    d = {"similar_found": True, "similarity": 0.8, "match_kind": "semantic",
         "suggestion": "defer_fusion"}
    assert _resolve_action("upsert", d) == "deferred_fusion"
    assert _resolve_action("merge", d) == "merged"
    assert _resolve_action("append", d) == "appended"

    skip = {"similar_found": True, "similarity": 0.97, "match_kind": "semantic",
            "suggestion": "skip"}
    assert _resolve_action("upsert", skip) == "skipped"
    assert _resolve_action("append", skip) == "skipped"

    merge = {"similar_found": True, "similarity": 0.6, "match_kind": "semantic",
             "suggestion": "merge"}
    assert _resolve_action("upsert", merge) == "merged"

    app = {"similar_found": False, "similarity": 0.3, "match_kind": "semantic",
           "suggestion": "append"}
    assert _resolve_action("upsert", app) == "created"

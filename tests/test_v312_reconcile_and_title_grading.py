"""v3.1.2 — write-path slimming + content-only semantic fusion + reconcile tools.

Third-round convergence tests. Design decision "B" (content-is-king, judgment
deferred to the agent):

  The write path does NOT try to decide whether two similar-but-maybe-distinct
  sections are truly the same knowledge — that is a semantic judgment the
  framework can't make reliably (a difflib title compare misjudges headings that
  differ by one key word). Instead, ANY in-domain fuse-band hit (cos >= fuse
  threshold) is DEFERRED: the framework hands both bodies to the calling agent,
  which reads them and decides during fusion whether to actually merge or keep
  them separate. No title heuristic, no second magic threshold.

Also covers write-path slimming (no cross-library scan on writes) and the active
reconcile library functions (suggestion-only, never mutate).

Requires the bge-small-zh model (fastembed). Bodies are kept >30 chars to clear
the short-text guard (SEM_SHORT_TEXT_CHARS), below which the semantic layer is
intentionally skipped. Cosines below are measured against the live model:
    reworded same knowledge   ~0.88  (fuse band  → defer)
    unrelated topics          ~0.42  (below merge → append)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.injector import (
    inject_knowledge,
    reindex_vector_store,
    calibrate_thresholds,
)
from layered_memory_mcp.promotion import (
    scan_cross_domain_duplicates,
    scan_split_candidates,
)


def _model_available() -> bool:
    try:
        from layered_memory_mcp.storage.vector_store import _embed_texts
        return _embed_texts(["probe"]).shape[0] == 1
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


# --- Neutral, generic technical knowledge. All bodies > 30 chars. ------------

# Fuse band (~0.88): the same fact reworded → defer fusion (hand both to agent).
_STARTUP_ORIG = (
    "应用启动时会先加载全局配置文件，解析其中的数据库连接串和缓存地址，然后初始化连接池等待请求"
)
_STARTUP_REWORD = (
    "服务启动阶段先读取配置文件，从中解析出数据库连接信息与缓存服务器地址，接着建立连接池准备接收请求"
)

# Unrelated (~0.42): different topics entirely → append as a new section.
_POOL = "数据库连接池的最大连接数配置为五十，空闲连接超时时间设为六十秒，用于应对高并发访问场景"
_I18N = "前端页面的国际化方案采用按语言拆分的资源文件，运行时根据浏览器语言标签动态加载对应文案"


# --- 1. Content-only semantic fusion (decision B) ----------------------------

def test_reworded_knowledge_defers_fusion_regardless_of_title(tmp_path):
    """~0.88 reword with a DIFFERENT section title must defer fusion — the
    framework hands both bodies to the agent regardless of the heading."""
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "boot", "启动流程", _STARTUP_ORIG, mode="upsert")
    r = inject_knowledge(cfg, "boot", "启动说明", _STARTUP_REWORD, mode="upsert")
    assert r["action"] == "deferred_fusion", r
    f = r["fusion"]
    assert f["hit_section"] == "启动流程"
    # The agent gets BOTH bodies to judge/merge — the whole point of decision B.
    assert f["old_body"] and _STARTUP_ORIG[:10] in f["old_body"]
    assert f["new_content"] == _STARTUP_REWORD
    assert f["cosine"] >= 0.72
    # Nothing written yet — the file still holds a single section.
    text = (Path(cfg.home) / "knowledge" / "boot.md").read_text(encoding="utf-8")
    assert text.count("## ") == 1, "deferred fusion must not write anything yet"


def test_unrelated_appends_new_section(tmp_path):
    """~0.42 unrelated knowledge appends as its own new section (no fusion)."""
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "infra", "连接池", _POOL, mode="upsert")
    r = inject_knowledge(cfg, "infra", "国际化", _I18N, mode="upsert")
    assert r["action"] != "deferred_fusion"
    text = (Path(cfg.home) / "knowledge" / "infra.md").read_text(encoding="utf-8")
    assert text.count("## ") == 2


# --- 2. Write-path slimming: no cross-library hints on the write path ---------

def test_write_path_emits_no_cross_library_hints(tmp_path):
    """After slimming, an ordinary inject must NOT carry the old cross-library
    hint fields — that global analysis moved to the active reconcile path."""
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "boot", "启动流程", _STARTUP_ORIG, mode="upsert")
    r = inject_knowledge(cfg, "infra", "连接池", _POOL, mode="upsert")
    for stale in ("cross_domain_hints", "shared_hit_hint",
                  "cross_namespace_hints", "title_diff_hint"):
        assert stale not in r, f"write path should no longer emit {stale}"


# --- 3. Active reconcile tools return suggestion-only data --------------------

def test_reconcile_cross_dup_finds_cross_file_duplicate(tmp_path):
    """The active cross-file duplicate scan surfaces a duplicate that the
    slimmed (in-domain-only) write path intentionally stays silent about."""
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "svc_a", "启动", _STARTUP_ORIG, mode="upsert")
    inject_knowledge(cfg, "svc_b", "启动", _STARTUP_REWORD, mode="upsert")
    reindex_vector_store(config=cfg, drop_existing=True)
    out = scan_cross_domain_duplicates(cfg, threshold=0.7)
    assert out.get("success", True) is not False, out
    pairs = out.get("pairs") or out.get("duplicates") or []
    assert len(pairs) >= 1, f"expected a cross-file duplicate pair, got {out}"


def test_reconcile_calibrate_returns_report(tmp_path):
    """calibrate_thresholds returns a structured report (thresholds or an
    explicit fallback reason) — never silently a magic number."""
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "boot", "启动流程", _STARTUP_ORIG, mode="upsert")
    inject_knowledge(cfg, "infra", "连接池", _POOL, mode="upsert")
    reindex_vector_store(config=cfg, drop_existing=True)
    rep = calibrate_thresholds(cfg)
    assert isinstance(rep, dict)
    assert ("thresholds" in rep or "reason" in rep
            or "fallback" in rep or "sample" in str(rep).lower()), rep


def test_reconcile_split_returns_list(tmp_path):
    """scan_split_candidates returns a structured (possibly empty) candidate list,
    never mutating anything."""
    cfg = _mk_config(tmp_path)
    inject_knowledge(cfg, "misc", "启动", _STARTUP_ORIG, mode="upsert")
    reindex_vector_store(config=cfg, drop_existing=True)
    out = scan_split_candidates(cfg)
    assert isinstance(out, dict)
    assert "candidates" in out

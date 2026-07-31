"""v2.10.0 Promotion Detector tests.

Covers the 7 cases required by the design doc
(.hermes/plans/promotion-detector.md, "测试要求"):

  1. domain outside watch list → None (zero-cost path).
  2. misc with 4+ sections but semantically scattered → None (no false positive).
  3. misc with 3+ semantically clustered sections → candidate, sensible
     suggested_domain.
  4. section count < min → None.
  5. detect raising internally → inject primary write still succeeds (fault
     isolation).
  6. audit_rot hit → findings.promotion_candidates structured correctly.
  7. promotion_enabled=False → whole chain skipped.

Embeddings are mocked (deterministic unit vectors) so the tests are fast and
never depend on the bge model download. One dedicated test uses a stub that
routes similar headings to the same vector and scattered ones to orthogonal
vectors — this exercises the real single-link clustering code path.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from layered_memory_mcp import promotion
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.promotion import detect_promotion_candidate


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_agent_memory(tmp_path, monkeypatch):
    """Point the agent-memory adapter at a throwaway file so inject()'s
    auto-maintain dual-write can never touch the real ~/.hermes MEMORY.md."""
    mem = tmp_path / "agent-memory.md"
    mem.write_text("", encoding="utf-8")
    monkeypatch.setenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", str(mem))
    return mem


def _mk_config(tmp_path, **overrides):
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(home / "knowledge"),
        **overrides,
    )


def _write_file(cfg, name, sections):
    """Write an L1 markdown file with the given (heading, body) sections."""
    fp = Path(cfg.knowledge_dir) / name
    parts = [f"# {name.removesuffix('.md')}\n"]
    for heading, body in sections:
        parts.append(f"\n## {heading}\n\n{body}\n")
    fp.write_text("".join(parts), encoding="utf-8")
    return fp


# Deterministic fake embedder. Assigns a fixed unit vector per "topic keyword"
# found in the text; texts sharing a keyword get identical vectors (cosine=1),
# distinct keywords get orthogonal basis vectors (cosine=0). This drives the
# REAL clustering code without loading bge.
_TOPIC_BASIS = {
    "database": 0,
    "ssh": 1,
    "docker": 2,
    "auth": 3,
    "caching": 4,
    "queue": 5,
}


def _fake_embed(texts):
    dim = 512
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for i, t in enumerate(texts):
        low = t.lower()
        assigned = False
        for kw, axis in _TOPIC_BASIS.items():
            if kw in low:
                out[i, axis] = 1.0
                assigned = True
                break
        if not assigned:
            # Unique orthogonal axis per un-topiced text → never clusters.
            out[i, 100 + i] = 1.0
    return out


@pytest.fixture
def fake_embed(monkeypatch):
    """Patch the vector-store embedder used by promotion._embed_sections."""
    monkeypatch.setattr(
        "layered_memory_mcp.storage.vector_store._embed_texts", _fake_embed
    )
    return _fake_embed


# ---------------------------------------------------------------------------
# Case 1 — domain outside watch list → None (zero-cost skip)
# ---------------------------------------------------------------------------

def test_case1_domain_outside_watch_returns_none(tmp_path, fake_embed):
    cfg = _mk_config(tmp_path)  # watch defaults to ["misc"]
    fp = _write_file(cfg, "infra.md", [
        ("database 数据源", "database 相关内容一"),
        ("database 连接池", "database 相关内容二"),
        ("database 回收", "database 相关内容三"),
        ("database 索引", "database 相关内容四"),
    ])
    # Even though infra.md has a strong database cluster, infra is NOT watched.
    assert detect_promotion_candidate(cfg, "infra", fp) is None


def test_case1_zero_cost_skips_embedding(tmp_path, monkeypatch):
    """Out-of-scope domain must not even call the embedder (true zero-cost)."""
    cfg = _mk_config(tmp_path)
    fp = _write_file(cfg, "infra.md", [
        ("database a", "x"), ("database b", "y"),
        ("database c", "z"), ("database d", "w"),
    ])

    called = {"n": 0}

    def _boom(texts):
        called["n"] += 1
        raise AssertionError("embedder must not be called for out-of-scope domain")

    monkeypatch.setattr(
        "layered_memory_mcp.storage.vector_store._embed_texts", _boom
    )
    assert detect_promotion_candidate(cfg, "infra", fp) is None
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Case 2 — misc with 4+ sections but scattered → None (no false positive)
# ---------------------------------------------------------------------------

def test_case2_scattered_sections_no_candidate(tmp_path, fake_embed):
    cfg = _mk_config(tmp_path)
    fp = _write_file(cfg, "misc.md", [
        ("ssh 隧道", "ssh 配置内容"),
        ("docker 部署", "docker 配置内容"),
        ("auth 记录", "auth 相关内容"),
        ("caching 收藏", "caching 相关内容"),
    ])
    # 4 distinct topics, each its own orthogonal vector → no cluster of >=3.
    assert detect_promotion_candidate(cfg, "misc", fp) is None


# ---------------------------------------------------------------------------
# Case 3 — misc with 3+ clustered sections → candidate w/ sensible domain
# ---------------------------------------------------------------------------

def test_case3_clustered_sections_yield_candidate(tmp_path, fake_embed):
    cfg = _mk_config(tmp_path)
    fp = _write_file(cfg, "misc.md", [
        ("database 数据源", "database 数据来源与清洗"),
        ("database 连接池", "database 连接池参数"),
        ("database 迁移结果", "database 迁移收益曲线"),
        ("docker 部署", "无关的 docker 内容"),
    ])
    result = detect_promotion_candidate(cfg, "misc", fp)
    assert result is not None
    assert result["watch_domain"] == "misc"
    assert result["cluster_size"] == 3
    assert result["file_section_count"] == 4
    assert result["suggested_domain"] == "database"  # common leading token
    assert len(result["cluster_sections"]) == 3
    assert all("database" in s.lower() for s in result["cluster_sections"])
    assert "misc" in result["hint"]
    assert "database" in result["hint"]


def test_case3_accepts_domain_with_md_suffix(tmp_path, fake_embed):
    """domain passed as 'misc.md' must still match the 'misc' watch entry."""
    cfg = _mk_config(tmp_path)
    fp = _write_file(cfg, "misc.md", [
        ("database 一", "database a"), ("database 二", "database b"),
        ("database 三", "database c"), ("ssh 杂项", "ssh x"),
    ])
    result = detect_promotion_candidate(cfg, "misc.md", fp)
    assert result is not None
    assert result["suggested_domain"] == "database"


# ---------------------------------------------------------------------------
# Case 4 — section count < min → None
# ---------------------------------------------------------------------------

def test_case4_too_few_sections_returns_none(tmp_path, fake_embed):
    cfg = _mk_config(tmp_path)  # promotion_min_sections defaults to 4
    fp = _write_file(cfg, "misc.md", [
        ("database 一", "database a"),
        ("database 二", "database b"),
        ("database 三", "database c"),
    ])
    # Only 3 sections < min_sections(4) → skip before clustering.
    assert detect_promotion_candidate(cfg, "misc", fp) is None


def test_case4_min_sections_configurable(tmp_path, fake_embed):
    """Lowering promotion_min_sections lets a 3-section file be scanned."""
    cfg = _mk_config(tmp_path, promotion_min_sections=3)
    fp = _write_file(cfg, "misc.md", [
        ("database 一", "database a"),
        ("database 二", "database b"),
        ("database 三", "database c"),
    ])
    result = detect_promotion_candidate(cfg, "misc", fp)
    assert result is not None
    assert result["cluster_size"] == 3


# ---------------------------------------------------------------------------
# Case 5 — detect raising internally → inject primary write still succeeds
# ---------------------------------------------------------------------------

def test_case5_detect_exception_does_not_break_inject(tmp_path, monkeypatch):
    from layered_memory_mcp import injector

    cfg = _mk_config(tmp_path)

    # Force the promotion detector to explode.
    def _explode(*args, **kwargs):
        raise RuntimeError("boom in detector")

    monkeypatch.setattr(
        "layered_memory_mcp.promotion.detect_promotion_candidate", _explode
    )

    # Primary write must still succeed despite the detector blowing up.
    r = injector.inject_knowledge(cfg, "misc", "某主题", "一些内容", mode="append")
    assert r["success"] is True
    # No promotion key should have leaked through the failure.
    assert "promotion" not in (r.get("auto_maintain") or {})


def test_case5_detect_returns_none_on_internal_error(tmp_path, monkeypatch):
    """detect_promotion_candidate itself swallows errors and returns None."""
    cfg = _mk_config(tmp_path)
    fp = _write_file(cfg, "misc.md", [
        ("database 一", "database a"), ("database 二", "database b"),
        ("database 三", "database c"), ("database 四", "database d"),
    ])

    def _boom(texts):
        raise RuntimeError("embed exploded")

    monkeypatch.setattr(
        "layered_memory_mcp.storage.vector_store._embed_texts", _boom
    )
    # Embedding failure → graceful None, no raise.
    assert detect_promotion_candidate(cfg, "misc", fp) is None


# ---------------------------------------------------------------------------
# Case 6 — audit_rot hit → findings.promotion_candidates structured correctly
# ---------------------------------------------------------------------------

def test_case6_audit_rot_surfaces_promotion_candidate(tmp_path, fake_embed):
    from layered_memory_mcp.rot_auditor import audit_rot

    cfg = _mk_config(tmp_path)
    _write_file(cfg, "misc.md", [
        ("database 数据源", "database 数据来源"),
        ("database 连接池", "database 连接池参数"),
        ("database 迁移", "database 迁移结果"),
        ("ssh 杂记", "ssh 无关内容"),
    ])
    report = audit_rot(cfg)
    assert report["success"] is True

    cands = report["findings"]["promotion_candidates"]
    assert isinstance(cands, list)
    assert len(cands) == 1
    c = cands[0]
    assert c["watch_domain"] == "misc"
    assert c["cluster_size"] == 3
    assert c["suggested_domain"] == "database"

    # summary count + recommendation must reflect the hit.
    assert report["summary"]["promotion_candidates"] == 1
    assert any("promotion candidate" in r for r in report["recommendations"])
    # Health score docked lightly (2 pts) but not zeroed.
    assert report["health_score"] == 98


def test_case6_audit_rot_no_candidate_when_clean(tmp_path, fake_embed):
    from layered_memory_mcp.rot_auditor import audit_rot

    cfg = _mk_config(tmp_path)
    _write_file(cfg, "misc.md", [
        ("ssh 隧道", "ssh 内容"),
        ("docker 部署", "docker 内容"),
        ("auth 记录", "auth 内容"),
        ("caching 收藏", "caching 内容"),
    ])
    report = audit_rot(cfg)
    assert report["findings"]["promotion_candidates"] == []
    assert report["summary"]["promotion_candidates"] == 0


# ---------------------------------------------------------------------------
# Case 7 — promotion_enabled=False → whole chain skipped
# ---------------------------------------------------------------------------

def test_case7_disabled_skips_detection(tmp_path, monkeypatch):
    cfg = _mk_config(tmp_path, promotion_enabled=False)
    fp = _write_file(cfg, "misc.md", [
        ("database 一", "database a"), ("database 二", "database b"),
        ("database 三", "database c"), ("database 四", "database d"),
    ])

    def _boom(texts):
        raise AssertionError("embedder must not run when promotion disabled")

    monkeypatch.setattr(
        "layered_memory_mcp.storage.vector_store._embed_texts", _boom
    )
    assert detect_promotion_candidate(cfg, "misc", fp) is None


def test_case7_disabled_skips_in_audit(tmp_path, fake_embed):
    from layered_memory_mcp.rot_auditor import audit_rot

    cfg = _mk_config(tmp_path, promotion_enabled=False)
    _write_file(cfg, "misc.md", [
        ("database 一", "database a"), ("database 二", "database b"),
        ("database 三", "database c"), ("database 四", "database d"),
    ])
    report = audit_rot(cfg)
    # Feature disabled → empty list, no docking.
    assert report["findings"]["promotion_candidates"] == []
    assert report["health_score"] == 100


# ---------------------------------------------------------------------------
# End-to-end integration: inject into misc surfaces the promotion suggestion
# ---------------------------------------------------------------------------

def test_integration_inject_attaches_promotion(tmp_path, fake_embed):
    from layered_memory_mcp import injector

    cfg = _mk_config(tmp_path)
    # Seed misc.md with 3 database sections + 1 unrelated already present.
    _write_file(cfg, "misc.md", [
        ("database 数据源", "database 数据来源"),
        ("database 连接池", "database 连接池参数"),
        ("ssh 杂记", "ssh 无关内容"),
    ])
    # Now inject a 4th section (another database one) → file reaches 4 sections
    # and the database cluster hits size 3.
    r = injector.inject_knowledge(
        cfg, "misc", "database 迁移", "database 迁移收益曲线", mode="append"
    )
    assert r["success"] is True
    maint = r.get("auto_maintain") or {}
    promo = maint.get("promotion")
    assert promo is not None
    assert promo["watch_domain"] == "misc"
    assert promo["suggested_domain"] == "database"
    assert promo["cluster_size"] == 3


def test_integration_normal_domain_no_promotion_key(tmp_path, fake_embed):
    """Writing to a non-watched domain must not add a promotion key."""
    from layered_memory_mcp import injector

    cfg = _mk_config(tmp_path)
    r = injector.inject_knowledge(cfg, "infra", "网络", "一些内容", mode="append")
    assert r["success"] is True
    assert "promotion" not in (r.get("auto_maintain") or {})


# ---------------------------------------------------------------------------
# Unit: suggested-domain derivation + config plumbing
# ---------------------------------------------------------------------------

def test_suggest_domain_common_prefix():
    headings = ["database 数据源", "database 连接池", "database 迁移"]
    assert promotion._suggest_domain_name(headings) == "database"


def test_suggest_domain_frequent_token():
    headings = ["primary database", "replica database", "cache database"]
    # No common leading token, but 'database' repeats → picked by frequency.
    assert promotion._suggest_domain_name(headings) == "database"


def test_suggest_domain_fallback_topic():
    headings = ["独一", "无二", "各异"]
    assert promotion._suggest_domain_name(headings) == "topic"


def test_config_promotion_defaults(tmp_path):
    cfg = _mk_config(tmp_path)
    assert cfg.promotion_enabled is True
    assert cfg.promotion_watch_domains == ["misc"]
    assert cfg.promotion_min_sections == 4
    assert cfg.promotion_cluster_threshold == 0.60
    assert cfg.promotion_min_cluster_size == 3


def test_config_promotion_threshold_range_validated(tmp_path):
    with pytest.raises(ValueError):
        _mk_config(tmp_path, promotion_cluster_threshold=1.5)

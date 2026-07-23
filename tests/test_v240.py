"""Tests for v2.4.0 — summariser punctuation fix + rot auditor."""

import os
import tempfile
from pathlib import Path

import pytest

from layered_memory_mcp.injector import _summarize_for_l0


# ---------------------------------------------------------------------------
# P2 fix: _summarize_for_l0 must preserve snake_case identifiers
# ---------------------------------------------------------------------------

def test_summarize_preserves_snake_case():
    assert "enabled_toolsets" in _summarize_for_l0("Cron 用 enabled_toolsets 白名单")
    assert "fallback_providers" in _summarize_for_l0("支持 fallback_providers 链式降级")


def test_summarize_preserves_path_underscores():
    out = _summarize_for_l0("读 ~/.config/api-token.txt 文件 some_var_name")
    assert "api-token.txt" in out
    assert "some_var_name" in out


def test_summarize_still_strips_emphasis_underscores():
    # paired _italic_ emphasis should still be removed
    assert _summarize_for_l0("这是 _强调_ 文本") == "这是 强调 文本"


def test_summarize_strips_bold_and_code():
    assert _summarize_for_l0("**粗体** 和 `代码`") == "粗体 和 代码"


def test_summarize_skips_headings():
    out = _summarize_for_l0("# 标题\n\n实际内容在这里")
    assert out == "实际内容在这里"


# ---------------------------------------------------------------------------
# Rot auditor
# ---------------------------------------------------------------------------

class _Cfg:
    """Minimal config stub exposing knowledge_dirs."""
    def __init__(self, kdir):
        self.knowledge_dirs = [Path(kdir)]
        self.knowledge_dir = Path(kdir)


@pytest.fixture
def kb(tmp_path):
    d = tmp_path / "knowledge"
    d.mkdir()
    return d


def _audit(kb):
    from layered_memory_mcp.rot_auditor import audit_rot
    return audit_rot(_Cfg(str(kb)))


def test_audit_clean_kb_scores_high(kb):
    (kb / "a.md").write_text(
        "# a\n\n## 配置说明\n\n这是一段正常的知识内容，描述了某个功能的配置方式。\n",
        encoding="utf-8",
    )
    r = _audit(kb)
    assert r["success"]
    assert r["health_score"] == 100
    assert r["summary"]["oversized"] == 0
    assert r["summary"]["garbled_heading"] == 0


def test_audit_detects_oversized(kb):
    (kb / "big.md").write_text("# big\n\n## s\n\n" + ("x" * 5000), encoding="utf-8")
    r = _audit(kb)
    assert r["summary"]["oversized"] == 1
    assert r["findings"]["oversized"][0]["file"] == "big.md"


def test_audit_detects_garbled_heading(kb):
    garbled = "## enabledtoolsets白名单时若skill依赖某MCP工具必须显式加mcpserver否则被静默过滤"
    (kb / "g.md").write_text(f"# g\n\n{garbled}\n\n内容\n", encoding="utf-8")
    r = _audit(kb)
    assert r["summary"]["garbled_heading"] >= 1


def test_audit_normal_heading_not_garbled(kb):
    # A heading with spaces / punctuation must NOT be flagged
    (kb / "n.md").write_text(
        "# n\n\n## TranslateBooksWithLLMs — 术语一致性导向的翻译工具\n\n内容\n",
        encoding="utf-8",
    )
    r = _audit(kb)
    assert r["summary"]["garbled_heading"] == 0


def test_audit_stale_requires_marker_and_expired_date(kb):
    # transient marker + past date → stale
    (kb / "s.md").write_text(
        "# s\n\n## 临时方案\n\n下次执行 2020-01-01，待测试效果。\n",
        encoding="utf-8",
    )
    r = _audit(kb)
    assert r["summary"]["stale"] >= 1


def test_audit_todo_without_date_not_stale(kb):
    # standing TODO list without an expired date must NOT be flagged
    (kb / "t.md").write_text(
        "# t\n\n## 待实施功能\n\n- TODO: 多线程采集\n- TODO: 数据质量校验\n",
        encoding="utf-8",
    )
    r = _audit(kb)
    assert r["summary"]["stale"] == 0


def test_audit_detects_cross_file_duplicate(kb):
    body = ("service-api 永久只在远程云端运行，localhost 是死端口，"
            "脚本访问数据必须走远程或 MCP，绝不连 localhost 端口。")
    (kb / "x.md").write_text(f"# x\n\n## 运行环境\n\n{body}\n", encoding="utf-8")
    (kb / "y.md").write_text(f"# y\n\n## 部署铁律\n\n{body}\n", encoding="utf-8")
    r = _audit(kb)
    assert r["summary"]["cross_file_duplicate"] >= 1


def test_audit_detects_same_file_duplicate(kb):
    # The "append but never merge" / dual-write rot: two near-identical
    # sections inside the SAME file.
    body = ("Cron job 用 enabled_toolsets 白名单时若 skill 依赖 MCP 工具必须显式加 "
            "mcp-server 否则被静默过滤导致 Agent 降级且报告误写为 MCP 不可用。")
    (kb / "z.md").write_text(
        f"# z\n\n## 白名单坑 A\n\n{body}\n\n## 白名单坑 B\n\n{body}\n",
        encoding="utf-8",
    )
    r = _audit(kb)
    assert r["summary"]["same_file_duplicate"] >= 1
    # and it must NOT be miscounted as cross-file
    assert r["summary"]["cross_file_duplicate"] == 0


def test_audit_report_shape(kb):
    (kb / "a.md").write_text("# a\n\n## s\n\n正常内容描述\n", encoding="utf-8")
    r = _audit(kb)
    assert set(r["findings"].keys()) == {
        "oversized", "garbled_heading", "stale",
        "cross_file_duplicate", "same_file_duplicate",
        "promotion_candidates",
    }
    assert isinstance(r["recommendations"], list)
    assert 0 <= r["health_score"] <= 100

"""v3.3.2 — rot-audit performance/recall + L0 index generation robustness.

Three defects found in production on 2026-10-06:

  Bug 1  The same-file duplicate scan in ``audit_rot`` was an O(n²) loop calling
         ``SequenceMatcher.ratio()`` on full section bodies with no prefilter.
         On the live library (791 sections → ~312k pairs) the MCP call hit the
         60s client timeout every time, so the health score was unobtainable.

  Bug 2  The same scan only compared whole bodies. A re-appended *shorter* copy
         of a section (identical heading, body much shorter) landed below the
         similarity threshold and was never reported — which is exactly how two
         copies of one section were sitting in ``layered-memory.md`` unnoticed.

  Bug 3  ``l0_manager._generate_hermes_index`` did
         ``gd.get("keywords", "").strip()`` on an *optional* regex group. A line
         with no ``→ keywords`` part yields ``None`` (the default applies only
         to a missing key), so a single malformed L1 file crashed L0 index
         generation for every write tool.

All fixtures use neutral placeholder content (no business data).
"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path

import pytest

from layered_memory_mcp import l0_manager, rot_auditor
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.rot_auditor import (
    CROSS_DUP_SIMILARITY,
    _heading_skeleton,
    _length_gate,
)


def _audit(tmp_path: Path, files: dict[str, str]) -> dict:
    """Write ``files`` into a sandbox knowledge dir and audit it."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (kdir / name).write_text(text, encoding="utf-8")
    config = MemoryConfig(home=str(tmp_path / "home"), knowledge_dir=str(kdir))
    return rot_auditor.audit_rot(config)


# ---------------------------------------------------------------------------
# Bug 1 — the length gate must never discard a pair that could match
# ---------------------------------------------------------------------------

class TestLengthGate:
    @pytest.mark.parametrize(
        "la,lb,expected",
        [
            (100, 100, True),      # bound 1.00
            (100, 110, True),      # bound 0.95
            (200, 243, True),      # bound 0.90
            (177, 256, False),     # bound 0.82 — the boundary case, just below
            (40, 60, False),       # bound 0.80
            (10, 1000, False),     # bound 0.02
            (40, 200, False),      # bound 0.33
            (0, 50, False),        # degenerate
        ],
    )
    def test_gate_matches_the_ratio_upper_bound(self, la, lb, expected):
        assert _length_gate(la, lb) is expected

    def test_gate_is_exact_never_discards_a_true_match(self):
        """The bound is 2*min/(la+lb); ratio() can never exceed it."""
        for la, lb in ((40, 60), (100, 100), (37, 91)):
            body_a = "中" * la
            body_b = "中" * lb
            real = SequenceMatcher(None, body_a, body_b).ratio()
            if real >= CROSS_DUP_SIMILARITY:
                assert _length_gate(la, lb), f"gate wrongly discarded a {real:.2f} match"


# ---------------------------------------------------------------------------
# Bug 2 — heading skeletons collapse the "same topic logged twice" shape
# ---------------------------------------------------------------------------

class TestHeadingSkeleton:
    @pytest.mark.parametrize(
        "left,right",
        [
            (
                "⚠️ pytest 套件无隔离，直接污染 prod MEMORY.md（2026-10-01 实测）",
                "⚠️ pytest 套件无隔离，直接污染 prod MEMORY.md",
            ),
            ("核验纪律（2026-07-16，含实证）", "核验纪律"),
            ("v3.3.0 memory_mode", "v3.3.1 memory_mode"),
            ("线上部署纠偏（2026-07-26·editable陷阱）", "线上部署纠偏"),
        ],
    )
    def test_same_topic_with_date_suffix_and_qualifier_collapses(self, left, right):
        assert _heading_skeleton(left) == _heading_skeleton(right)

    @pytest.mark.parametrize(
        "left,right",
        [
            ("数据库选型", "网络排查"),
            ("核验纪律", "发布流程"),
        ],
    )
    def test_unrelated_headings_stay_distinct(self, left, right):
        assert _heading_skeleton(left) != _heading_skeleton(right)

    def test_empty_heading_yields_empty_skeleton(self):
        assert _heading_skeleton("") == ""


# ---------------------------------------------------------------------------
# Bug 2 (cont.) — audit recall
# ---------------------------------------------------------------------------

class TestAuditRecall:
    def test_flags_shorter_reappended_copy_of_the_same_section(self, tmp_path):
        """The shape that was missed: same heading, body much shorter.

        Both bodies must clear the auditor's 40-char section floor, yet differ
        enough in length that body similarity alone stays below the threshold —
        that is precisely the gap the heading-skeleton route closes.
        """
        long_body = "细节描述。" * 100
        short_body = "同一主题的简短复述。长度明显小于原件。" * 3
        report = _audit(tmp_path, {
            "topic.md": (
                "## 部署口径（2026-10-01 实测）\n" + long_body + "\n\n"
                "## 部署口径\n" + short_body + "\n"
            )
        })

        dupes = report["findings"]["same_file_duplicate"]
        assert len(dupes) == 1, dupes
        assert dupes[0]["reason"] == "same heading skeleton"
        assert dupes[0]["similarity"] is None

    def test_flags_identical_body_copies(self, tmp_path):
        body = "完全相同的一段正文，用于验证近重复主体的检测路径。" * 3
        report = _audit(tmp_path, {
            "topic.md": f"## 甲主题\n{body}\n\n## 乙主题\n{body}\n"
        })

        dupes = report["findings"]["same_file_duplicate"]
        assert len(dupes) == 1, dupes
        assert dupes[0]["similarity"] and dupes[0]["similarity"] >= CROSS_DUP_SIMILARITY
        assert "near-identical body" in dupes[0]["reason"]

    def test_same_heading_in_different_files_is_not_flagged(self, tmp_path):
        """Negative control: a generic heading repeated across files is legitimate."""
        report = _audit(tmp_path, {
            "a.md": "## 基础环境\n这是文件 A 的内容，讲的是完全不同的东西。\n",
            "b.md": "## 基础环境\n这是文件 B 的内容，与 A 毫无交集的描述。\n",
        })

        assert report["findings"]["same_file_duplicate"] == []
        assert report["findings"]["cross_file_duplicate"] == []

    def test_unrelated_sections_are_not_flagged(self, tmp_path):
        """Negative control: distinct topics, distinct lengths."""
        report = _audit(tmp_path, {
            "topic.md": (
                "## 数据库选型\n短期内的权衡记录，无关另一段。\n\n"
                "## 网络排查\n" + "另一段主题完全不同的内容。" * 20 + "\n"
            )
        })

        assert report["findings"]["same_file_duplicate"] == []


# ---------------------------------------------------------------------------
# Bug 1 (cont.) — the prefilter must actually skip the expensive ratio()
# ---------------------------------------------------------------------------

class TestAuditPrefilter:
    def test_ratio_is_never_called_when_lengths_cannot_match(self, tmp_path, monkeypatch):
        """Every pair is length-incompatible, so ratio() must not run at all.

        Counting calls makes the optimisation deterministic to assert — a wall
        clock test would be flaky, and 'it felt fast' is not evidence.
        """
        calls = {"ratio": 0}

        class _CountingMatcher(SequenceMatcher):
            def ratio(self):
                calls["ratio"] += 1
                return super().ratio()

        monkeypatch.setattr(rot_auditor, "SequenceMatcher", _CountingMatcher)

        # Exponentially growing lengths (40, 58, 84, 122, 177, 256) so that EVERY
        # pairwise bound stays <= 0.817, i.e. below the 0.82 threshold.
        lengths = [40, 58, 84, 122, 177, 256]
        sections = [
            f"## 主题{i}\n" + "中" * length for i, length in enumerate(lengths)
        ]
        report = _audit(tmp_path, {"many.md": "\n\n".join(sections) + "\n"})

        assert report["findings"]["same_file_duplicate"] == []
        assert calls["ratio"] == 0, f"ratio() ran {calls['ratio']}x despite the gate"


# ---------------------------------------------------------------------------
# Bug 3 — L0 index generation must survive a line without keywords
# ---------------------------------------------------------------------------

class TestIndexGenerationRobustness:
    def test_line_without_keywords_does_not_crash(self, tmp_path, monkeypatch):
        """Exactly the live failure: one L1 file with no keywords took down
        L0 generation for inject/create/update/sync alike."""
        monkeypatch.setattr(
            l0_manager, "generate_l0_index",
            lambda knowledge_dir: "[no-keywords.md] 只有标题没有关键词",
        )

        lines = l0_manager._generate_hermes_index(str(tmp_path), {})

        assert lines, "index generation returned nothing"
        assert any("no-keywords" in line for line in lines), lines

    def test_wellformed_line_still_renders_with_title_and_keywords(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            l0_manager, "generate_l0_index",
            lambda knowledge_dir: "[topic.md] 主题标题 → 关键词一, 关键词二",
        )

        lines = l0_manager._generate_hermes_index(str(tmp_path), {})

        assert lines and "关键词一" in lines[0] and "topic.md" in lines[0]

"""v3.3.3 — stale scoring must not convict retrospective notes.

The live library scored 6 "stale" sections, all six false positives: the old
detector flagged a section when a marker from ``_TRANSIENT_MARKERS`` (which
included the scope qualifiers 临时 / 暂时) appeared anywhere in the heading or
leading two lines AND any past date appeared in the same text. Every dated
lesson — "（2026-09-11 实测）", "（2026-07-16，含实证）" — therefore tripped it,
costing 18 points of a 56-point score for nothing.

A section can only be *overdue* if it carries a promise: the fix keeps only
forward-looking markers, and requires the date to sit on the same line as that
marker, so an unrelated observation date nearby cannot convict it.

All fixtures use neutral placeholder content (no business data).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.rot_auditor import _PENDING_MARKERS, audit_rot


def _stale(tmp_path: Path, text: str) -> list[dict]:
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "topic.md").write_text(text, encoding="utf-8")
    report = audit_rot(MemoryConfig(home=str(tmp_path / "home"), knowledge_dir=str(kdir)))
    return report["findings"]["stale"]


class TestRetrospectiveNotesAreNotStale:
    """The six live false positives, in their general shape."""

    def test_scope_qualifier_with_observation_date_is_not_stale(self, tmp_path):
        stale = _stale(tmp_path, (
            "## 临时缓存目录会污染后续步骤\n"
            "这是一条带观察日期的回溯性教训（2026-09-11 实测）。\n"
        ))
        assert stale == []

    def test_dated_lesson_without_any_promise_is_not_stale(self, tmp_path):
        stale = _stale(tmp_path, "## 核验纪律（2026-07-16，含实证）\n正文记录当时的做法与依据。\n")
        assert stale == []

    def test_scope_qualifiers_are_not_in_the_marker_list(self):
        """Lock the removal: these describe scope, not a pending action."""
        assert "临时" not in _PENDING_MARKERS
        assert "暂时" not in _PENDING_MARKERS

    def test_promise_with_unrelated_past_date_on_another_line_is_not_stale(self, tmp_path):
        """The date must belong to the promise itself."""
        stale = _stale(tmp_path, (
            "## 待实施的重构\n"
            "背景记录（2026-08-01 实测）。\n"
            "后续再评估。\n"
        ))
        assert stale == []


class TestForwardLookingPromises:
    def test_overdue_promise_on_the_same_line_is_stale(self, tmp_path):
        past = "2020-01-01"
        stale = _stale(tmp_path, f"## 发布检查\n下次执行 {past} 的巡检尚未落地。\n")
        assert len(stale) == 1, stale
        assert "pending" in stale[0]["reason"]
        assert past in stale[0]["reason"]

    def test_promise_with_a_future_date_is_not_stale(self, tmp_path):
        future = "2999-12-31"
        stale = _stale(tmp_path, f"## 发布检查\n下次执行 {future}。\n")
        assert stale == []

    def test_promise_without_any_date_is_not_stale(self, tmp_path):
        """A standing TODO with no deadline cannot expire."""
        stale = _stale(tmp_path, "## 待办事项\n尚未处理，稍后补上。\n")
        assert stale == []

    def test_marker_inside_body_lead_is_considered(self, tmp_path):
        """The first two body lines are in scope, not just the heading."""
        past = "2020-06-01"
        stale = _stale(tmp_path, f"## 例行巡检\n下次执行 {past}。\n其余内容。\n")
        assert len(stale) == 1, stale

    def test_boundary_today_is_not_expired(self, tmp_path):
        today = date.today().isoformat()
        stale = _stale(tmp_path, f"## 例行巡检\n下次执行 {today}。\n")
        assert stale == []

"""v3.3.4 — two detector-quality fixes found while auditing the live library.

1. Stale (P3): a line that records the *discharge* of its own promise
   (``… TODO dd1eb34c 已完成（2026-07-26）``) carries both a pending marker and a
   past date, yet it is history. It was the last false-positive class left after
   v3.3.3.
2. Promotion naming: the suggested domain name came back as ``2026`` because a
   bare year, repeated across dated headings, won the frequency vote — the
   tokenizer splits ``2026-07-30`` into ``2026`` / ``07`` / ``30``.

All fixtures use neutral placeholder content.
"""

from __future__ import annotations

from pathlib import Path

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.promotion import _is_noise_token, _suggest_domain_name
from layered_memory_mcp.rot_auditor import audit_rot


def _stale(tmp_path: Path, text: str) -> list[dict]:
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "topic.md").write_text(text, encoding="utf-8")
    report = audit_rot(MemoryConfig(home=str(tmp_path / "home"), knowledge_dir=str(kdir)))
    return report["findings"]["stale"]


class TestDischargedPromisesAreNotStale:
    def test_completed_todo_with_past_date_is_not_stale(self, tmp_path):
        """The live shape that produced the last false positive."""
        stale = _stale(tmp_path, (
            "## 部署纠偏\n"
            "⚠️ v2.11.0 已于 2026-07-26 升级生效（TODO dd1eb34c 已完成，详见下节）。\n"
        ))
        assert stale == []

    def test_archived_migration_is_not_stale(self, tmp_path):
        stale = _stale(tmp_path, "## 迁移收尾\nTODO 2020-01-01 已解决。\n")
        assert stale == []

    def test_unfinished_promise_is_still_stale(self, tmp_path):
        """Regression: '尚未完成' must NOT be swallowed by the completion guard.

        '尚未完成' contains 完成 but not the marker 已完成.
        """
        stale = _stale(tmp_path, "## 待验证项\n尚未完成 2020-01-01 的验证。\n")
        assert len(stale) == 1, stale
        assert "pending" in stale[0]["reason"]

    def test_pending_marker_still_flagged_alongside_completion_mention(self, tmp_path):
        """A still-open line is flagged even if another line records a finish."""
        stale = _stale(tmp_path, (
            "## 待办清理\n"
            "上次那件事 2020-01-01 已完成。\n"
            "尚未 2020-02-02 处理第二件事。\n"
        ))
        assert len(stale) == 1, stale
        assert "2020-02-02" in stale[0]["reason"]


class TestPromotionNamingIgnoresNumericTokens:
    def test_bare_digits_are_noise(self):
        for tok in ("2026", "07", "30", "0", "12345"):
            assert _is_noise_token(tok) is True

    def test_words_and_cjk_are_not_noise(self):
        for tok in ("database", "数据库", "v3", "2026x"):
            assert _is_noise_token(tok) is False

    def test_dated_headings_do_not_yield_a_year_domain(self):
        """The live failure: 29 dated headings suggested '2026.md'."""
        headings = [
            "用词文风审查观（2026-07-30）",
            "工作风格（2026-08-04）",
            "用户求职状态（2026-08）",
            "收尾类决定（2026-09-17）",
        ]
        suggested = _suggest_domain_name(headings)
        assert not _is_noise_token(suggested), suggested
        assert suggested == "topic", suggested

    def test_repeated_topic_word_still_wins(self):
        """Filtering numbers must not disable strategy 2 for real topics."""
        headings = [
            "database pooling (2026-01-01)",
            "database migration (2026-02-02)",
        ]
        assert _suggest_domain_name(headings) == "database"

    def test_leading_year_does_not_win_strategy_1(self):
        headings = ["2026 release plan", "2026 release notes"]
        suggested = _suggest_domain_name(headings)
        assert suggested == "release", suggested

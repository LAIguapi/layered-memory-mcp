"""v3.3.7 — same-skeleton families on the write side.

The read-side auditor could already report a "family" of sections that share a
heading skeleton (dates / issue numbers / parentheticals stripped): that is how
four issues of a periodical column were found piled up in one file. Cosine
similarity cannot see this by construction — each member's heading carries a
fresh date, and the members' bodies differ.

This version gives the writer the same eyes. When a write would add another
member to such a family, nothing is written: the whole family is handed back with
a ``deferred_consolidate`` action and an optimistic-lock hash, and the caller must
rewrite the family as ONE section ("current understanding + provenance line")
before re-submitting with ``fuse=True``.

All fixtures use neutral placeholder content (no business data).
"""

from __future__ import annotations

import pytest

from layered_memory_mcp import rot_auditor
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.heading import heading_skeleton
from layered_memory_mcp.injector import (
    _family_hash,
    _heading_family,
    _split_raw,
    inject_knowledge,
)

# Neutral periodic-topic fixture: one recurring topic, two dated instances.
TOPIC_A = "例行巡检 2026-09-22 期（磁盘）"
TOPIC_B = "例行巡检 2026-10-03 期（内存）"
TOPIC_C = "例行巡检 2026-10-17 期（网络）"
BODY_A = "磁盘水位阈值 85%，超过先清理临时目录再告警。"
BODY_B = "内存增长主要来自缓存，先看缓存命中率再决定是否扩容。"
BODY_C = "网络抖动先查对端，本端指标正常时不要改配置。"


def _mk_config(tmp_path, **kw) -> MemoryConfig:
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"), **kw)


def _sections(cfg, domain: str) -> list[str]:
    _preamble, sections = _split_raw((cfg.knowledge_dir / f"{domain}.md").read_text("utf-8"))
    return [h[2:].strip() for h, _b in sections]


def _seed_family(tmp_path, bodies=(BODY_A, BODY_B)) -> MemoryConfig:
    """Write two same-skeleton sections with the gate off (a pre-existing family)."""
    off = _mk_config(tmp_path, consolidate_enabled=False)
    sections = [TOPIC_A, TOPIC_B, TOPIC_C]
    for section, body in zip(sections, bodies):
        inject_knowledge(off, "ops", section, body, mode="upsert")
    enabled = _mk_config(tmp_path)
    return enabled


class TestSkeletonIsShared:
    def test_read_and_write_use_one_function(self):
        assert rot_auditor._heading_skeleton is heading_skeleton

    def test_periodic_headings_collapse(self):
        assert heading_skeleton(TOPIC_A) == heading_skeleton(TOPIC_B) == "例行巡检期"
        assert heading_skeleton(BODY_A) != heading_skeleton(TOPIC_A)

    def test_split_raw_is_byte_faithful(self, tmp_path):
        raw = "# ops\n\n> note\n\n## A 2026-01-01\n\nbody a\n\n## B\n\nbody b\n"
        preamble, sections = _split_raw(raw)
        assert preamble + "".join(h + b for h, b in sections) == raw
        assert [h for h, _b in sections] == ["## A 2026-01-01", "## B"]


class TestFamilyGate:
    def test_second_member_defers_consolidation(self, tmp_path):
        cfg = _seed_family(tmp_path, bodies=(BODY_A,))
        assert _sections(cfg, "ops") == [TOPIC_A]

        result = inject_knowledge(cfg, "ops", TOPIC_B, BODY_B, mode="upsert")

        assert result["action"] == "deferred_consolidate", result
        assert result["family_size"] == 1
        assert result["would_be_family_size"] == 2
        assert result["expected_hash"] == _family_hash(_heading_family(
            (cfg.knowledge_dir / "ops.md").read_text("utf-8"), TOPIC_B
        ))
        assert [m["heading"] for m in result["family"]] == [TOPIC_A]
        assert result["new_content"] == BODY_B
        # Nothing written: the family is still one member.
        assert _sections(cfg, "ops") == [TOPIC_A]

    def test_gate_off_writes_as_before(self, tmp_path):
        """Negative control: with the feature disabled nothing is intercepted."""
        cfg = _mk_config(tmp_path, consolidate_enabled=False)
        inject_knowledge(cfg, "ops", TOPIC_A, BODY_A, mode="upsert")
        result = inject_knowledge(cfg, "ops", TOPIC_B, BODY_B, mode="upsert")

        assert result["action"] != "deferred_consolidate"
        assert _sections(cfg, "ops") == [TOPIC_A, TOPIC_B]

    def test_min_family_three_waits_for_a_third(self, tmp_path):
        cfg = _mk_config(tmp_path, consolidate_enabled=False, consolidate_min_family=3)
        inject_knowledge(cfg, "ops", TOPIC_A, BODY_A, mode="upsert")
        cfg = _mk_config(tmp_path, consolidate_min_family=3)

        first = inject_knowledge(cfg, "ops", TOPIC_B, BODY_B, mode="upsert")
        assert first["action"] != "deferred_consolidate"
        assert len(_sections(cfg, "ops")) == 2

        second = inject_knowledge(cfg, "ops", TOPIC_C, BODY_C, mode="upsert")
        assert second["action"] == "deferred_consolidate", second
        assert second["would_be_family_size"] == 3

    def test_exact_heading_update_is_not_a_new_member(self, tmp_path):
        cfg = _seed_family(tmp_path, bodies=(BODY_A,))
        result = inject_knowledge(cfg, "ops", TOPIC_A, "修订后的正文。", mode="upsert")
        assert result["action"] != "deferred_consolidate", result
        assert _sections(cfg, "ops") == [TOPIC_A]


class TestWriteback:
    def _handshake(self, cfg):
        deferred = inject_knowledge(cfg, "ops", TOPIC_C, BODY_C, mode="upsert")
        assert deferred["action"] == "deferred_consolidate"
        return deferred

    def test_collapses_the_family_into_one_section(self, tmp_path):
        cfg = _seed_family(tmp_path)
        assert len(_sections(cfg, "ops")) == 2
        deferred = self._handshake(cfg)

        consolidated = "例行巡检：磁盘先看水位、内存先看缓存命中率；出处：09-22/10-03 两期。"
        result = inject_knowledge(
            cfg, "ops", TOPIC_C, consolidated,
            mode="upsert", fuse=True, expected_hash=deferred["expected_hash"],
        )

        assert result["action"] == "consolidated", result
        # Net effect: the family's 2 members became 1 section (1 deleted, 1 rewritten).
        assert result["family_size_before"] == 2
        assert result["sections_removed"] == 1
        headings = _sections(cfg, "ops")
        assert headings == [TOPIC_C]
        assert heading_skeleton(headings[0]) == "例行巡检期"

    def test_single_member_family_can_always_be_consolidated(self, tmp_path):
        """The ceiling compares against the replaced family, so it starts at 2.

        Regression for a deadlock found by running the handshake against the live
        service: with one existing member, the merged body is naturally longer
        than that member, so a ceiling applied here would refuse every first
        consolidation while still blocking the write.
        """
        cfg = _seed_family(tmp_path, bodies=(BODY_A,))
        deferred = inject_knowledge(cfg, "ops", TOPIC_B, BODY_B, mode="upsert")
        assert deferred["action"] == "deferred_consolidate"

        merged = f"例行巡检：{BODY_A}{BODY_B}出处：两期。"
        assert len(merged) > len(BODY_A), "the merged body is longer than the member it replaces"

        result = inject_knowledge(
            cfg, "ops", TOPIC_B, merged,
            mode="upsert", fuse=True, expected_hash=deferred["expected_hash"],
        )
        assert result["action"] == "consolidated", result
        assert _sections(cfg, "ops") == [TOPIC_B]

    def test_lazy_writeback_is_refused(self, tmp_path):
        """Re-stating the family in a new shape is not consolidation."""
        cfg = _seed_family(tmp_path)
        deferred = self._handshake(cfg)
        bloated = (BODY_A + "\n" + BODY_B + "\n" + BODY_C) * 3

        result = inject_knowledge(
            cfg, "ops", TOPIC_C, bloated,
            mode="upsert", fuse=True, expected_hash=deferred["expected_hash"],
        )

        assert result["action"] == "consolidate_refused", result
        assert result["ratio"] >= result["ceiling"]
        assert len(_sections(cfg, "ops")) == 2, "a refused write must leave the family intact"

    def test_wrong_hash_is_not_treated_as_consolidation(self, tmp_path):
        cfg = _seed_family(tmp_path)
        result = inject_knowledge(
            cfg, "ops", TOPIC_C, "some body",
            mode="upsert", fuse=True, expected_hash="deadbeef",
        )
        assert result["action"] == "fuse_conflict", result
        assert len(_sections(cfg, "ops")) == 2

    def test_stale_family_is_not_consolidated(self, tmp_path):
        """The family changed between defer and write-back → re-defer required."""
        cfg = _seed_family(tmp_path)
        deferred = self._handshake(cfg)
        # Someone else appends another member after the handshake was issued.
        inject_knowledge(
            _mk_config(tmp_path, consolidate_enabled=False),
            "ops", "例行巡检 2026-11-01 期（进程）", "进程数异常先看最近发布的版本。",
            mode="upsert",
        )

        result = inject_knowledge(
            cfg, "ops", TOPIC_C, "例行巡检：合并结论；出处：三期。",
            mode="upsert", fuse=True, expected_hash=deferred["expected_hash"],
        )
        assert result["action"] != "consolidated", result
        assert len(_sections(cfg, "ops")) == 3


class TestAppendIsDeprecated:
    def test_append_reports_the_override(self, tmp_path):
        cfg = _mk_config(tmp_path)
        result = inject_knowledge(cfg, "ops", "单次事实", "内容。", mode="append")
        assert result["mode_deprecated"] == "append"
        assert "upsert" in result["mode_note"]

    def test_append_cannot_grow_a_family(self, tmp_path):
        cfg = _seed_family(tmp_path, bodies=(BODY_A,))
        result = inject_knowledge(cfg, "ops", TOPIC_B, BODY_B, mode="append")
        assert result["action"] == "deferred_consolidate", result
        assert result["mode_deprecated"] == "append"
        assert _sections(cfg, "ops") == [TOPIC_A]

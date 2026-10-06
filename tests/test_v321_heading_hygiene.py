"""v3.2.1 — heading hygiene regression tests.

Two real bugs found in the wild (knowledge base health_score had decayed to
32/100, with 9 garbled headings and 10 stub headings in the live library):

  Bug 1  _suggest_migration cleaned headings with an ALLOW-LIST regex
         (``[^a-zA-Z0-9\\u4e00-\\u9fff\\s\\-]``, then ``[^\\w\\s\\-·‧...]``).
         It deleted ':' '/' '.' and every CJK punctuation mark, so
         "/var/cache/documents/" became "varcachedocuments"
         and Chinese clauses welded into one unbroken run — precisely the
         P2 "garbled_heading" pathology the rot auditor kept reporting.

  Bug 2  When an entry had no L0 tag, the fallback took the body's first 40
         characters as the heading. Every reworded re-injection of the same
         knowledge produced a slightly different stub heading sitting next to
         the real one. The zero-LLM core must not guess titles.

These tests pin the fixed behaviour.
"""

import pytest

from layered_memory_mcp.memory_compactor import (
    MAX_SECTION_TITLE_CHARS,
    _clean_heading,
    _suggest_migration,
    _truncate_on_boundary,
)


# --- Bug 1: punctuation and paths must survive heading derivation ------------

class TestHeadingPreservesContent:
    @pytest.mark.parametrize(
        "entry,must_contain",
        [
            (
                "[L0] infra: 用户上传的文件缓存在 /var/cache/documents/",
                "/var/cache/documents",
            ),
            (
                "[L0] dev: DB 选型：PG:5432 vs Redis:6379",
                "PG:5432",
            ),
            (
                "[L0] strix: 职责边界：用户定 strix 只管挖，下游交付由 Agent 负责",
                "，",
            ),
            (
                "[L0] agent: 用户设定的硬约束（体量上限、时间范围）属红线",
                "（",
            ),
        ],
    )
    def test_path_and_punctuation_survive(self, entry, must_contain):
        section = _suggest_migration(entry)["section"]
        assert must_contain in section, f"heading lost content: {section!r}"

    def test_path_is_not_welded_into_one_run(self):
        """The exact regression: separators stripped -> unreadable run."""
        section = _suggest_migration(
            "[L0] infra: 用户上传的文件缓存在 /var/cache/documents/"
        )["section"]
        assert "varcachedocuments" not in section

    def test_markdown_structure_chars_are_still_stripped(self):
        """Heading-breaking characters must not leak into an ATX heading."""
        cleaned = _clean_heading("## some **bold** `code` [link]")
        for ch in "#*`[]":
            assert ch not in cleaned

    def test_newlines_never_leak_into_heading(self):
        cleaned = _clean_heading("first line\nsecond line")
        assert "\n" not in cleaned and "\r" not in cleaned
        assert cleaned == "first line second line"

    def test_clean_heading_empty_input_returns_empty(self):
        assert _clean_heading("") == ""
        assert _clean_heading("   ") == ""
        assert _clean_heading("***") == ""


# --- Boundary-aware truncation ------------------------------------------------

class TestTruncateOnBoundary:
    def test_short_text_untouched(self):
        assert _truncate_on_boundary("短标题") == "短标题"

    def test_respects_max_chars(self):
        long = "字" * 200
        assert len(_truncate_on_boundary(long)) <= MAX_SECTION_TITLE_CHARS

    def test_prefers_clause_boundary_over_hard_cut(self):
        text = "这是一个比较长的第一个子句内容，这是第二个子句用来触发截断"
        out = _truncate_on_boundary(text, 20)
        # The comma sits past the halfway mark, so cut there rather than
        # slicing mid-clause.
        assert out == "这是一个比较长的第一个子句内容"

    def test_boundary_too_early_falls_back_to_hard_cut(self):
        """A boundary in the first half would throw away too much title."""
        text = "短，" + "字" * 100
        out = _truncate_on_boundary(text, 20)
        assert len(out) == 20

    def test_hard_cut_when_no_usable_boundary(self):
        text = "字" * 100
        out = _truncate_on_boundary(text, 10)
        assert len(out) == 10


# --- Bug 2: never fabricate a heading from the body ---------------------------

class TestNoFabricatedHeadings:
    @pytest.mark.parametrize(
        "entry",
        [
            "用户上传的文件缓存在 /var/cache/documents/，读取时走这个路径",
            "Configure the proxy server for deployment",
            "Something completely unrelated to anything at all",
        ],
    )
    def test_untagged_entry_requests_a_title(self, entry):
        result = _suggest_migration(entry)
        assert result["needs_title"] is True
        assert result["section"] == ""

    def test_untagged_entry_does_not_slice_the_body(self):
        """The old fallback was first_line[:40] — assert it is gone."""
        body = "这是一段很长的正文内容用来验证不会再被切成标题了绝对不可以这样做"
        result = _suggest_migration(body)
        assert body[:40] not in result["section"]
        assert result["section"] == ""

    def test_tagged_entry_still_gets_a_title(self):
        """Regression guard: the fix must not break the happy path."""
        result = _suggest_migration("[L0] infra: proxy config details here")
        assert result["needs_title"] is False
        assert result["section"]
        assert result["domain"] == "infra"

    def test_reworded_duplicates_no_longer_mint_rival_stubs(self):
        """Two rewordings of one fact previously produced two stub headings."""
        a = _suggest_migration("缓存目录配置在 /var/cache/documents 下面，注意权限")
        b = _suggest_migration("缓存目录的配置位于 /var/cache/documents，需要注意权限问题")
        assert a["needs_title"] and b["needs_title"]
        assert a["section"] == b["section"] == ""


# --- compact_memory must refuse rather than write a stub ----------------------

class TestCompactRefusesUntitledEntries:
    def test_untitled_entry_is_reported_not_written(self, tmp_path):
        from layered_memory_mcp.config import MemoryConfig
        from layered_memory_mcp.memory_compactor import compact_memory

        knowledge = tmp_path / "knowledge"
        knowledge.mkdir()
        mem = tmp_path / "MEMORY.md"
        mem.write_text(
            "[L0] infra: pointer → knowledge/infra.md\n"
            "§\n"
            "一条没有 L0 标签的详细知识，放在 memory 里属于 bloat，应该被迁走但无法命名\n",
            encoding="utf-8",
        )
        cfg = MemoryConfig(home=str(tmp_path), knowledge_dir=str(knowledge))

        result = compact_memory(cfg, memory_path=str(mem), dry_run=False)

        assert result["success"]
        # Refused, not migrated.
        assert result["migrated_count"] == 0
        assert result["error_count"] == 1
        err = result["errors"][0]
        assert err.get("needs_title") is True
        assert "needs_title" in err["error"]
        # The original entry stays put — nothing is lost.
        assert "无法命名" in result["cleaned_memory"]
        # And no stub heading was written to L1.
        for f in knowledge.glob("*.md"):
            assert "## 一条没有 L0 标签" not in f.read_text(encoding="utf-8")

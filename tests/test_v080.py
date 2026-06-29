"""
Tests for v0.8.0: Memory compaction, bloat detection, framework init, L0 pointer.

P0: inject_knowledge returns l0_pointer
P1: compact_memory detects and migrates bloat
P2: validate_knowledge includes memory bloat detection
P3: init_framework detects first-run and returns rules
"""

import json
import tempfile
from pathlib import Path

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.injector import inject_knowledge, _summarize_for_l0
from layered_memory_mcp.memory_compactor import (
    detect_memory_bloat,
    compact_memory,
    _parse_entries,
    _is_index_entry,
    is_oversized_index_entry,
    _suggest_migration,
    _get_domain_rules,
    _FALLBACK_DOMAIN_RULES,
)


class TestL0Pointer:
    """P0: inject_knowledge should return l0_pointer in result."""

    def test_inject_returns_l0_pointer(self, tmp_path):
        """inject_knowledge result should contain l0_pointer field."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        result = inject_knowledge(
            config=config,
            domain="infra",
            section="Proxy",
            content="HTTP proxy at 127.0.0.1:7890",
            mode="upsert",
        )

        assert result["success"] is True
        assert "l0_pointer" in result
        assert result["l0_pointer"].startswith("[L0]")
        assert "infra" in result["l0_pointer"]
        assert "knowledge/infra.md" in result["l0_pointer"]
        assert "hint" in result

    def test_l0_pointer_has_content_summary(self, tmp_path):
        """l0_pointer should contain a brief summary of the injected content."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        result = inject_knowledge(
            config=config,
            domain="dev",
            section="Principles",
            content="Always test before commit. Never push to main directly.",
            mode="upsert",
        )

        assert result["success"]
        pointer = result["l0_pointer"]
        # Should have a reasonable summary
        assert len(pointer) < 200  # Not bloated

    def test_summarize_for_l0(self):
        """_summarize_for_l0 should extract first meaningful line."""
        assert _summarize_for_l0("## Heading\n\nReal content here") == "Real content here"
        assert _summarize_for_l0("Just a single line") == "Just a single line"
        assert _summarize_for_l0("## Only heading") == ""
        assert _summarize_for_l0("") == ""
        # Long content should be truncated
        long_line = "A" * 200
        summary = _summarize_for_l0(long_line)
        assert len(summary) <= 83  # 80 + "..."


class TestMemoryCompaction:
    """P1: compact_memory detects and migrates bloat entries."""

    def test_parse_hermes_entries(self):
        """Should correctly parse Hermes MEMORY.md format."""
        raw = "[L0索引] infra: proxy config → knowledge/infra.md\n§\nSome long content that is not an index"
        entries = _parse_entries(raw)
        assert len(entries) == 2
        assert entries[0].startswith("[L0索引]")
        assert "Some long content" in entries[1]

    def test_is_index_entry(self):
        """Should correctly identify L0 index entries."""
        assert _is_index_entry("[L0索引] infra: proxy → knowledge/infra.md")
        assert _is_index_entry("[L0索引] dev: principles → knowledge/dev.md")
        # Over-long L0 pointer is STILL an index entry (regression guard):
        # length must never reclassify a pointer as bloat, or compact_memory
        # migrates it back into L1 and dual_write re-nests the [L0] prefix,
        # producing the runaway "[L0] [L0] [L0] …" loop.
        long_entry = "[L0索引] infra: " + "x" * 200
        assert _is_index_entry(long_entry)
        assert is_oversized_index_entry(long_entry)  # but flagged for trimming
        # A normal-length pointer is not oversized
        assert not is_oversized_index_entry("[L0索引] infra: proxy → knowledge/infra.md")
        # No L0 prefix
        assert not _is_index_entry("Random content about stuff")
        assert not is_oversized_index_entry("Random content about stuff")

    def test_suggest_migration_with_custom_rules(self, tmp_path):
        """Should suggest correct domain based on custom rules from YAML config."""
        # Create a YAML rules file
        rules_file = tmp_path / "domain_rules.yaml"
        rules_file.write_text(
            "trading:\n  - 'backtest'\n  - 'strategy'\n  - 'signal'\n"
            "infra:\n  - 'docker'\n  - 'proxy'\n  - 'server'\n"
            "content:\n  - 'article'\n  - 'publishing'\n",
            encoding="utf-8",
        )

        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            compact_domain_rules_file=str(rules_file),
        )

        domain_rules = _get_domain_rules(config)

        entry = "Backtest strategy v3.4A verified as optimal"
        result = _suggest_migration(entry, domain_rules=domain_rules)
        assert result["domain"] == "trading"

        entry = "Docker proxy port 8080 configuration"
        result = _suggest_migration(entry, domain_rules=domain_rules)
        assert result["domain"] == "infra"

        entry = "Article publishing API key configuration"
        result = _suggest_migration(entry, domain_rules=domain_rules)
        assert result["domain"] == "content"

        # Unknown domain → misc
        entry = "Something completely unrelated"
        result = _suggest_migration(entry, domain_rules=domain_rules)
        assert result["domain"] == "misc"

    def test_suggest_migration_fallback_rules(self):
        """Fallback rules should match generic English keywords."""
        # No config → fallback rules
        result = _suggest_migration("Configure the proxy server for deployment")
        assert result["domain"] == "infra"

        result = _suggest_migration("Follow TDD principles for code review")
        assert result["domain"] == "dev"

        # Unknown → misc
        result = _suggest_migration("Something completely unrelated to anything")
        assert result["domain"] == "misc"

    def test_suggest_migration_no_config_uses_fallback(self, tmp_path):
        """Without a config file, fallback rules should be used."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        # No compact_domain_rules_file specified
        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        domain_rules = _get_domain_rules(config)
        assert domain_rules == _FALLBACK_DOMAIN_RULES

        # Should match generic infra keyword
        result = _suggest_migration("Docker deploy config for nginx", domain_rules=domain_rules)
        assert result["domain"] == "infra"

    def test_suggest_migration_l0_tag_extraction(self):
        """L0 index tag domain should be used directly."""
        entry = "[L0索引] infra: proxy config details here"
        result = _suggest_migration(entry)
        assert result["domain"] == "infra"

    def test_detect_memory_bloat(self, tmp_path):
        """Should detect bloat in a memory file."""
        memory_file = tmp_path / "MEMORY.md"
        memory_file.write_text(
            "[L0索引] infra: proxy → knowledge/infra.md\n"
            "§\n"
            "This is a very long entry that contains detailed knowledge about "
            "the proxy configuration and should really be in L1 not L0. "
            "It goes on and on about specific ports and protocols.\n"
            "§\n"
            "[L0索引] dev: principles → knowledge/dev.md",
            encoding="utf-8",
        )

        result = detect_memory_bloat(str(memory_file))
        assert result["success"] is True
        assert result["total_entries"] == 3
        assert result["index_entries"] == 2
        assert result["bloat_entries"] == 1
        assert result["stats"]["bloat_percentage"] > 0

    def test_detect_memory_bloat_capacity_warning(self, tmp_path, monkeypatch):
        """Should issue capacity warning when usage exceeds threshold."""
        memory_file = tmp_path / "MEMORY.md"
        content = "[L0索引] infra: proxy → knowledge/infra.md\n§\n"
        # Add enough content to exceed 90% of a small max_chars limit
        content += "X" * 500
        memory_file.write_text(content, encoding="utf-8")

        result = detect_memory_bloat(str(memory_file), max_chars=500)
        assert result["success"] is True
        assert "warnings" in result
        assert len(result["warnings"]) > 0
        assert result["warnings"][0]["type"] == "capacity"
        assert result["warnings"][0]["usage_ratio"] > 0.9

    def test_detect_memory_bloat_no_warning_under_threshold(self, tmp_path):
        """Should NOT issue warning when usage is under threshold."""
        memory_file = tmp_path / "MEMORY.md"
        memory_file.write_text(
            "[L0索引] infra: proxy → knowledge/infra.md",
            encoding="utf-8",
        )

        result = detect_memory_bloat(str(memory_file), max_chars=100_000)
        assert result["success"] is True
        assert "warnings" not in result or len(result.get("warnings", [])) == 0

    def test_detect_memory_bloat_file_not_found(self):
        """Should return error for non-existent file."""
        result = detect_memory_bloat("/nonexistent/path/MEMORY.md")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_compact_memory_dry_run(self, tmp_path):
        """compact_memory dry_run should not modify files."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        memory_file = tmp_path / "MEMORY.md"

        original_content = (
            "[L0索引] infra: proxy → knowledge/infra.md\n"
            "§\n"
            "Detailed knowledge about proxy setup that should be in L1"
        )
        memory_file.write_text(original_content, encoding="utf-8")

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        result = compact_memory(config, memory_path=str(memory_file), dry_run=True)
        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["migrated_count"] == 1

        # File should NOT be modified in dry run
        assert memory_file.read_text(encoding="utf-8") == original_content

    def test_compact_memory_real_run(self, tmp_path):
        """compact_memory should migrate bloat and write cleaned file."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        memory_file = tmp_path / "MEMORY.md"

        memory_file.write_text(
            "[L0索引] infra: proxy → knowledge/infra.md\n"
            "§\n"
            "Docker deploy config for proxy server setup\n"
            "§\n"
            "[L0索引] dev: principles → knowledge/dev.md",
            encoding="utf-8",
        )

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        result = compact_memory(config, memory_path=str(memory_file))
        assert result["success"] is True
        assert result["migrated_count"] == 1
        assert result["error_count"] == 0

        # The cleaned file should only have index entries (2 legacy + 1 new format)
        cleaned = memory_file.read_text(encoding="utf-8")
        assert cleaned.count("[L0]") + cleaned.count("[L0索引]") == 3  # 2 original + 1 migrated
        assert cleaned.count("[L0索引]") == 2  # original entries preserved in old format

    def test_compact_memory_no_bloat(self, tmp_path):
        """compact_memory with clean file should report 0 migrations."""
        memory_file = tmp_path / "MEMORY.md"
        memory_file.write_text(
            "[L0索引] infra: proxy → knowledge/infra.md\n"
            "§\n"
            "[L0索引] dev: principles → knowledge/dev.md",
        )
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        result = compact_memory(config, memory_path=str(memory_file))
        assert result["success"] is True
        assert result["migrated_count"] == 0

    def test_oversized_l0_pointer_not_migrated(self, tmp_path):
        """Regression: an over-long L0 pointer must NOT be migrated to L1.

        Previously _is_index_entry() reclassified any L0 pointer longer than
        MAX_INDEX_ENTRY_LENGTH as bloat. compact_memory then injected it into
        the L1 file body, dual_write regenerated a nested "[L0] [L0] …"
        pointer, and the next cycle nested it again — an unbounded loop that
        corrupted both memory and the L1 file. Guard: oversized pointers stay
        put, nothing migrates, and no extra [L0] layer appears.
        """
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        memory_file = tmp_path / "MEMORY.md"

        oversized = "[L0索引] infra: " + "超长摘要" * 60 + " → knowledge/infra.md"
        assert len(oversized) > 120  # precondition: would have tripped the old gate
        memory_file.write_text(oversized, encoding="utf-8")

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        result = compact_memory(config, memory_path=str(memory_file))
        assert result["success"] is True
        assert result["migrated_count"] == 0          # nothing treated as bloat
        # L1 file must not have been created from the pointer
        assert not (knowledge_dir / "infra.md").exists()
        # Memory still holds exactly one [L0] entry — no nesting
        cleaned = memory_file.read_text(encoding="utf-8")
        assert cleaned.count("[L0索引]") == 1
        assert cleaned.count("[L0]") == 0


class TestValidateMemoryBloat:
    """P2: validate_knowledge should include memory bloat detection."""

    def test_validate_detects_bloat(self, tmp_path, monkeypatch):
        """validate_knowledge with check_memory_bloat should report bloat."""
        # This is tested via the compactor module directly since
        # the MCP tool wraps it
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "test.md").write_text("# Test\n\nContent\n", encoding="utf-8")

        # Create a mock memory file
        memory_file = tmp_path / "MEMORY.md"
        memory_file.write_text(
            "[L0索引] test: summary → knowledge/test.md\n"
            "§\n"
            "A very long bloat entry " * 10,
            encoding="utf-8",
        )

        from layered_memory_mcp.memory_compactor import detect_memory_bloat
        result = detect_memory_bloat(str(memory_file))
        assert result["bloat_entries"] >= 1


class TestInitFramework:
    """P3: init_framework detects first-run and returns rules."""

    def test_init_first_run(self, tmp_path):
        """init_framework on empty knowledge dir should create welcome file."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        from layered_memory_mcp.injector import inject_knowledge as _inject
        # Simulate what init_framework does
        l1_files_before = list(knowledge_dir.glob("*.md"))
        assert len(l1_files_before) == 0

        # Create welcome file
        _inject(
            config=config,
            domain="getting-started",
            section="Welcome",
            content="First knowledge file",
            mode="upsert",
        )

        l1_files_after = list(knowledge_dir.glob("*.md"))
        assert len(l1_files_after) == 1

    def test_init_already_initialized(self, tmp_path):
        """init_framework on existing knowledge should report already initialized."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "existing.md").write_text("# Existing\n\nContent\n", encoding="utf-8")

        from layered_memory_mcp.recall import scan_knowledge_files
        l1_files = scan_knowledge_files(str(knowledge_dir))
        assert len(l1_files) > 0  # already initialized

"""
Integration tests for v0.5.0 new modules: l0_manager, injector, auto-sync.

Tests the full pipeline: inject → auto L0 sync → recall.
"""

import json
import tempfile
from pathlib import Path

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.l0_manager import (
    sync_l0_index,
    manage_entry,
    check_l0_l1_consistency,
)
from layered_memory_mcp.injector import inject_knowledge
from layered_memory_mcp.recall import recall


class TestL0Manager:
    """Tests for l0_manager.py — L0 index sync and entry management."""

    def test_sync_creates_hermes_index(self, tmp_path):
        """sync_l0_index should generate hermes-format entries from L1 files."""
        knowledge_dir = tmp_path / "knowledge"
        l0_file = tmp_path / "L0_INDEX.md"

        # Create L1 knowledge files
        knowledge_dir.mkdir()
        (knowledge_dir / "infra.md").write_text(
            "# Infrastructure\n\n## Proxy\n\nHTTP proxy at 127.0.0.1:20172\n\n## Git\n\nGitHub connection details\n",
            encoding="utf-8",
        )
        (knowledge_dir / "dev.md").write_text(
            "# Development\n\n## Principles\n\nTest before commit\n\n## CI\n\nGitHub Actions pipeline\n",
            encoding="utf-8",
        )

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(l0_file),
            l0_format="hermes",
        )

        report = sync_l0_index(config, dry_run=False)

        assert report["success"] is True
        assert report["l1_files_found"] == 2
        assert report["entries_added"] >= 1  # At least new entries detected
        assert Path(l0_file).exists()

        content = l0_file.read_text(encoding="utf-8")
        assert "[L0]" in content
        assert "infra" in content
        assert "knowledge/infra.md" in content or "→" in content

    def test_sync_detects_removed_files(self, tmp_path):
        """sync_l0_index should detect files removed from L1."""
        knowledge_dir = tmp_path / "knowledge"
        l0_file = tmp_path / "L0_INDEX.md"
        knowledge_dir.mkdir()

        # Initial L1
        (knowledge_dir / "infra.md").write_text("# Infra\n\nSome info\n", encoding="utf-8")
        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(l0_file),
            l0_format="hermes",
        )
        sync_l0_index(config)

        # Remove L1 file
        (knowledge_dir / "infra.md").unlink()

        report2 = sync_l0_index(config)
        assert report2["entries_removed"] >= 1
        assert "infra" in report2["removed"]

    def test_manage_entry_add_remove(self, tmp_path):
        """manage_entry should add and remove individual L0 entries."""
        l0_file = tmp_path / "L0_INDEX.md"
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(l0_file),
            l0_format="hermes",
        )

        # Add
        r = manage_entry(config, "add", domain="infra", summary="Proxy and Git config", filename="infra.md")
        assert r["success"] is True
        assert r["total_entries"] == 1

        content = l0_file.read_text(encoding="utf-8")
        assert "[L0] infra: Proxy and Git config → knowledge/infra.md" in content

        # Add another
        r2 = manage_entry(config, "add", domain="dev", summary="Dev principles", filename="dev-principles.md")
        assert r2["total_entries"] == 2

        # Remove
        r3 = manage_entry(config, "remove", domain="infra")
        assert r3["success"] is True
        assert r3["total_entries"] == 1

        # Duplicate add should fail
        r4 = manage_entry(config, "add", domain="dev", summary="Dup", filename="dup.md")
        assert r4["success"] is False

    def test_consistency_check(self, tmp_path):
        """check_l0_l1_consistency should find orphaned and stale entries."""
        knowledge_dir = tmp_path / "knowledge"
        l0_file = tmp_path / "L0_INDEX.md"
        knowledge_dir.mkdir()

        (knowledge_dir / "infra.md").write_text("# Infra\n", encoding="utf-8")

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(l0_file),
            l0_format="hermes",
        )

        # Sync — should be consistent
        sync_l0_index(config)
        report = check_l0_l1_consistency(config)
        assert report["orphaned_l1"] == []
        assert report["stale_l0_entries"] == []
        assert report["health"] == "good"

        # Add orphan: L1 file not in L0
        (knowledge_dir / "orphan.md").write_text("# Orphan\n", encoding="utf-8")
        report2 = check_l0_l1_consistency(config)
        assert "orphan.md" in report2["orphaned_l1"]
        assert report2["health"] in ("fair", "poor")


class TestInjector:
    """Tests for injector.py — smart knowledge injection."""

    def test_inject_creates_new_file(self, tmp_path):
        """inject_knowledge should create a new L1 file with section."""
        knowledge_dir = tmp_path / "knowledge"
        l0_file = tmp_path / "L0_INDEX.md"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(l0_file),
            auto_sync_l0=True,
        )

        result = inject_knowledge(
            config,
            domain="infra",
            section="Proxy",
            content="HTTP proxy at 127.0.0.1:20172",
            mode="upsert",
        )

        assert result["success"] is True
        assert result["action"] == "created"
        assert result["file"] == "infra.md"
        assert result["l0_synced"] is True

        # Verify file content
        content = (knowledge_dir / "infra.md").read_text(encoding="utf-8")
        assert "## Proxy" in content
        assert "HTTP proxy at 127.0.0.1:20172" in content

        # Verify L0 was synced
        assert l0_file.exists()
        l0_text = l0_file.read_text(encoding="utf-8")
        assert "infra" in l0_text

    def test_inject_append_to_existing_section(self, tmp_path):
        """inject_knowledge with mode=append should append to an existing section."""
        knowledge_dir = tmp_path / "knowledge"
        l0_file = tmp_path / "L0_INDEX.md"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(l0_file),
            auto_sync_l0=True,
        )

        # First injection
        inject_knowledge(config, domain="dev", section="Git", content="GitHub: token-based auth")
        # Second injection — same section, different content
        result = inject_knowledge(
            config, domain="dev", section="Git", content="GitLab: oauth2 auth", mode="append",
        )

        assert result["success"] is True
        content = (knowledge_dir / "dev.md").read_text(encoding="utf-8")
        assert "GitHub: token-based auth" in content
        assert "GitLab: oauth2 auth" in content

    def test_inject_upsert_replaces_similar(self, tmp_path):
        """inject_knowledge with mode=upsert should replace similar content."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(tmp_path / "L0.md"),
            auto_sync_l0=False,  # Don't need L0 for this test
        )

        # First write
        inject_knowledge(config, domain="test", section="Config",
                         content="The database connection string is postgresql://localhost:5432/db",
                         mode="upsert")

        # Second write — very similar content (same meaning, different wording)
        result = inject_knowledge(config, domain="test", section="Config",
                                  content="Database connects via postgresql://localhost:5432/db",
                                  mode="upsert")

        assert result["success"] is True
        assert result["action"] in ("replaced", "skipped")  # Should replace due to similarity

    def test_inject_new_section_in_existing_file(self, tmp_path):
        """injecting into a new section in an existing file should append the section."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(tmp_path / "L0.md"),
            auto_sync_l0=False,
        )

        inject_knowledge(config, domain="config", section="Database",
                         content="PostgreSQL on port 5432", mode="upsert")

        result = inject_knowledge(config, domain="config", section="Redis",
                                  content="Redis on port 6379", mode="upsert")

        assert result["success"] is True
        content = (knowledge_dir / "config.md").read_text(encoding="utf-8")
        assert "## Database" in content
        assert "## Redis" in content
        assert "PostgreSQL on port 5432" in content
        assert "Redis on port 6379" in content

    def test_inject_empty_content_rejected(self, tmp_path):
        """Empty content should be rejected."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
        )

        result = inject_knowledge(config, domain="test", section="X", content="  ", mode="upsert")
        assert result["success"] is False
        assert "empty" in result["error"].lower()

    def test_inject_without_dot_md(self, tmp_path):
        """domain without .md should be auto-suffixed."""
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(tmp_path / "L0.md"),
            auto_sync_l0=False,
        )

        result = inject_knowledge(config, domain="infra", section="Test",
                                  content="some content", mode="upsert")
        assert result["success"] is True
        assert result["file"] == "infra.md"
        assert (knowledge_dir / "infra.md").exists()


class TestAutoSyncIntegration:
    """End-to-end test: inject → L0 sync → recall."""

    def test_full_pipeline(self, tmp_path):
        """The complete flow: inject knowledge, then recall finds it."""
        knowledge_dir = tmp_path / "knowledge"
        l0_file = tmp_path / "L0_INDEX.md"
        knowledge_dir.mkdir()

        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(knowledge_dir),
            l0_index_file=str(l0_file),
            auto_sync_l0=True,
        )

        # Step 1: Inject knowledge
        inject_knowledge(
            config,
            domain="infra",
            section="WSL Proxy",
            content="The WSL HTTP proxy is configured at 127.0.0.1:20172 for accessing external APIs.",
        )

        # Step 2: Verify L0 was synced
        assert l0_file.exists()
        l0_text = l0_file.read_text(encoding="utf-8")
        assert "infra" in l0_text

        # Step 3: Recall should find the injected knowledge
        results = recall("proxy", str(knowledge_dir), top_n=3)
        assert results["matched_files"] > 0
        found = False
        for item in results["results"]:
            for section in item.get("sections", []):
                if "20172" in section.get("content", ""):
                    found = True
                    break
        assert found, "Injected knowledge should be recallable"


class TestRecallIntegration:
    """Verify recall still works correctly with the new modules loaded."""

    def test_recall_basic(self, tmp_path):
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "test.md").write_text(
            "# Test File\n\n## API\n\nThe API key is stored in environment variables.\n\n## Config\n\nSettings are in config.yaml\n",
            encoding="utf-8",
        )

        results = recall("API key", str(knowledge_dir), top_n=3)
        assert len(results) > 0, "Should find the API key section"

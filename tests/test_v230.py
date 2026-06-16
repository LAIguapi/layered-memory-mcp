"""
Tests for v2.3.0: Auto-Maintain — write-triggered self-maintenance.

Covers:
  - Dual-write completion: L0 pointer auto-written to agent memory
  - Stale pointer replacement (same L1 file)
  - Idempotency (pointer already present)
  - Lazy compaction trigger via interval marker
  - auto_maintain disabled → falls back to advisory warning
  - Maintenance never raises (silent failure)
"""

import os
import time
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.injector import inject_knowledge
from layered_memory_mcp.memory_compactor import (
    auto_maintain_after_write,
    _ensure_l0_pointer_in_memory,
    _read_last_compact_time,
    _write_last_compact_time,
    _parse_entries,
)


def _make_config(tmp_path: Path, memory_file: Path, **kwargs) -> MemoryConfig:
    """Build a MemoryConfig pointed at a temp home + explicit agent memory file."""
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(exist_ok=True)
    # MemoryConfig reads agent memory path from env; set it explicitly.
    os.environ["LAYERED_MEMORY_AGENT_MEMORY_PATH"] = str(memory_file)
    # Force '§' separator (Hermes convention) regardless of filename.
    os.environ["LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR"] = "§"
    return MemoryConfig(
        home=str(tmp_path),
        knowledge_dir=str(knowledge_dir),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clean_env():
    """Isolate env mutations between tests."""
    saved = {
        k: os.environ.get(k)
        for k in (
            "LAYERED_MEMORY_AGENT_MEMORY_PATH",
            "LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR",
            "LAYERED_MEMORY_AUTO_MAINTAIN",
            "MEMORY_MAX_CHARS",
        )
    }
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestDualWriteCompletion:
    """The framework should mirror the L0 pointer into agent memory itself."""

    def test_pointer_added_when_missing(self, tmp_path):
        memfile = tmp_path / "MEMORY.md"
        memfile.write_text("[L0] existing: foo → knowledge/foo.md\n", encoding="utf-8")
        config = _make_config(tmp_path, memfile)

        pointer = "[L0] infra: bar → knowledge/infra.md"
        report = _ensure_l0_pointer_in_memory(pointer, config)

        assert report["action"] == "added"
        content = memfile.read_text(encoding="utf-8")
        assert pointer in content
        assert "existing" in content  # didn't clobber the old one

    def test_stale_pointer_replaced(self, tmp_path):
        memfile = tmp_path / "MEMORY.md"
        memfile.write_text(
            "[L0] infra: OLD summary → knowledge/infra.md\n", encoding="utf-8"
        )
        config = _make_config(tmp_path, memfile)

        new_pointer = "[L0] infra: NEW summary → knowledge/infra.md"
        report = _ensure_l0_pointer_in_memory(new_pointer, config)

        assert report["action"] == "replaced"
        content = memfile.read_text(encoding="utf-8")
        assert "NEW summary" in content
        assert "OLD summary" not in content
        # Only one pointer to infra.md should remain
        entries = _parse_entries(content, separator="§")
        infra_ptrs = [e for e in entries if "knowledge/infra.md" in e]
        assert len(infra_ptrs) == 1

    def test_idempotent_when_present(self, tmp_path):
        memfile = tmp_path / "MEMORY.md"
        pointer = "[L0] infra: bar → knowledge/infra.md"
        memfile.write_text(pointer + "\n", encoding="utf-8")
        config = _make_config(tmp_path, memfile)

        report = _ensure_l0_pointer_in_memory(pointer, config)
        assert report["action"] == "present"


class TestInjectAutoMaintain:
    """inject_knowledge should auto-complete the dual-write end to end."""

    def test_inject_writes_pointer_to_memory(self, tmp_path):
        memfile = tmp_path / "MEMORY.md"
        memfile.write_text("", encoding="utf-8")
        config = _make_config(tmp_path, memfile)

        result = inject_knowledge(
            config=config,
            domain="infra",
            section="Proxy Setup",
            content="Use HTTP proxy at 127.0.0.1:7890 for all outbound traffic.",
        )

        assert result["success"]
        # Framework should report it handled the dual-write
        assert "auto_maintain" in result
        dw = result["auto_maintain"]["dual_write"]
        assert dw["action"] in ("added", "replaced", "present")
        # The pointer should now physically be in the memory file
        content = memfile.read_text(encoding="utf-8")
        assert "knowledge/infra.md" in content

    def test_disabled_falls_back_to_warning_only(self, tmp_path):
        memfile = tmp_path / "MEMORY.md"
        # Pre-fill with bloat so the advisory warning would trigger
        bloat = "x" * 3500
        memfile.write_text(bloat + "\n", encoding="utf-8")
        config = _make_config(tmp_path, memfile, auto_maintain=False)

        result = inject_knowledge(
            config=config,
            domain="infra",
            section="Test",
            content="Some knowledge content here.",
        )

        assert result["success"]
        # Auto-maintain off → no auto_maintain key, pointer NOT auto-written
        assert "auto_maintain" not in result


class TestLazyCompaction:
    """Compaction should fire on interval elapse when bloat exists."""

    def test_interval_marker_roundtrip(self, tmp_path):
        memfile = tmp_path / "MEMORY.md"
        memfile.write_text("", encoding="utf-8")
        config = _make_config(tmp_path, memfile)

        assert _read_last_compact_time(config) == 0.0
        _write_last_compact_time(config)
        assert _read_last_compact_time(config) > 0.0

    def test_compaction_triggers_on_elapsed_interval(self, tmp_path):
        memfile = tmp_path / "MEMORY.md"
        # One bloat entry (long, not an L0 pointer) → compactable
        memfile.write_text(
            "This is a long detailed knowledge entry about proxy and docker "
            "configuration that clearly belongs in an L1 file not the index. "
            "It is way over the index length limit and should be migrated.\n",
            encoding="utf-8",
        )
        config = _make_config(tmp_path, memfile, auto_maintain_interval_days=0.0)
        # Last compact = long ago (0.0) → interval elapsed
        os.environ["MEMORY_MAX_CHARS"] = "50000"

        report = auto_maintain_after_write(config, l0_pointer=None)

        assert report["compact"]["triggered"] is True
        assert "interval elapsed" in report["compact"]["reason"]

    def test_no_compaction_when_disabled(self, tmp_path):
        memfile = tmp_path / "MEMORY.md"
        memfile.write_text("some bloat content " * 50, encoding="utf-8")
        config = _make_config(tmp_path, memfile, auto_maintain=False)

        report = auto_maintain_after_write(config, l0_pointer=None)
        assert report.get("skipped") is True


class TestSafety:
    """Maintenance must never raise, even on a missing memory file."""

    def test_missing_memory_file_no_raise(self, tmp_path):
        memfile = tmp_path / "does_not_exist.md"
        config = _make_config(tmp_path, memfile)
        # Should not raise; returns a report
        report = auto_maintain_after_write(
            config, l0_pointer="[L0] infra: x → knowledge/infra.md"
        )
        assert isinstance(report, dict)

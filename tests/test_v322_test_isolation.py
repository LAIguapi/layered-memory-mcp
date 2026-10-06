"""Regression tests for test-suite isolation (conftest.py).

Root cause being locked down: ``detect_agent_memory_path()`` probes
``Path.home()/".hermes"/"memories"/"MEMORY.md"`` and ignores
``LAYERED_MEMORY_HOME``, so tests used to resolve — and write to — the real
production memory file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig

# The real production paths this suite must never touch. Derived from the real
# home, captured at import time before any fixture redirects HOME (conftest.py
# relies on the same ordering), so this file carries no machine-specific path.
_REAL_HOME = os.path.expanduser("~")
_REAL_MEMORY = os.path.join(_REAL_HOME, ".hermes", "memories", "MEMORY.md")
_REAL_L1 = os.path.join(_REAL_HOME, ".layered-memory")


class TestPathIsolation:
    def test_agent_memory_path_is_not_production(self):
        """The exact leak that polluted MEMORY.md with 20 junk pointers."""
        resolved = MemoryConfig().detect_agent_memory_path()
        assert resolved is not None
        assert str(resolved) != _REAL_MEMORY, (
            f"agent memory resolved to production file {resolved}"
        )

    def test_knowledge_dir_is_not_production(self):
        kdir = str(MemoryConfig().knowledge_dir)
        assert not kdir.startswith(_REAL_L1), f"L1 store resolved to production {kdir}"

    def test_home_is_redirected(self):
        """Path.home() must be sandboxed for code that bypasses config."""
        sandbox_home = str(Path.home())
        assert sandbox_home == os.environ["HOME"]
        assert not sandbox_home.startswith(os.path.join(_REAL_HOME, ".hermes"))

    def test_sandbox_paths_are_writable(self):
        """Isolation must not break legitimate writes."""
        cfg = MemoryConfig()
        cfg.knowledge_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.knowledge_dir / "isolation-probe.md"
        probe.write_text("probe", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "probe"

    def test_each_test_gets_a_fresh_sandbox(self):
        """tmp_path is per-test, so no cross-test bleed."""
        cfg = MemoryConfig()
        cfg.knowledge_dir.mkdir(parents=True, exist_ok=True)
        leaked = cfg.knowledge_dir / "leaked-from-previous-test.md"
        assert not leaked.exists()
        leaked.write_text("x", encoding="utf-8")


class TestProductionWriteGuard:
    """The backstop fixture must convert a prod write into a loud failure."""

    def test_writing_to_production_memory_raises(self):
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            with open(_REAL_MEMORY, "a", encoding="utf-8") as fh:
                fh.write("this must never land\n")

    def test_writing_to_production_l1_raises(self):
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            with open(f"{_REAL_L1}/knowledge/pwned.md", "w", encoding="utf-8") as fh:
                fh.write("nope")

    def test_reading_production_is_still_allowed(self):
        """Read-only access stays legal — the guard targets writes only."""
        if Path(_REAL_MEMORY).exists():
            with open(_REAL_MEMORY, encoding="utf-8") as fh:
                fh.read(16)

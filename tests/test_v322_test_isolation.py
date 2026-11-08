"""Regression tests for test-suite isolation (conftest.py).

These tests ensure that agent-owned memory paths and framework-owned knowledge
paths remain inside the per-test sandbox instead of reaching a host-managed
path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig

# Host-managed paths this suite must never touch. Derived from the host home,
# captured before fixtures redirect HOME, so no concrete user path is embedded.
_REAL_HOME = os.path.expanduser("~")
_REAL_MEMORY = os.path.join(_REAL_HOME, ".hermes", "memories", "MEMORY.md")
_REAL_L1 = os.path.join(_REAL_HOME, ".layered-memory")


class TestPathIsolation:
    def test_agent_memory_path_is_not_host_managed(self):
        """Agent-memory resolution must not escape the test sandbox."""
        resolved = MemoryConfig().detect_agent_memory_path()
        assert resolved is not None
        assert str(resolved) != _REAL_MEMORY, (
            f"agent memory resolved to a host-managed file {resolved}"
        )

    def test_knowledge_dir_is_not_host_managed(self):
        kdir = str(MemoryConfig().knowledge_dir)
        assert not kdir.startswith(_REAL_L1), f"L1 store resolved to host-managed data {kdir}"

    def test_home_is_redirected(self):
        """Path.home() must be sandboxed for code that bypasses config."""
        sandbox_home = str(Path.home())
        assert sandbox_home == os.environ["HOME"]
        # Compare resolved paths rather than prefixes: the temporary directory
        # may be nested below another tool's cache directory.
        assert sandbox_home != _REAL_HOME, f"home not redirected: {sandbox_home}"
        sandbox_memory = os.path.join(sandbox_home, ".hermes", "memories", "MEMORY.md")
        assert sandbox_memory != _REAL_MEMORY, (
            f"sandbox home resolves to a host-managed memory path {sandbox_memory}"
        )

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


class TestHostWriteGuard:
    """The backstop fixture must convert a host write into a loud failure."""

    def test_writing_to_host_memory_raises(self, tmp_path, _guard_host_paths):
        target = tmp_path / "protected-memory.md"
        _guard_host_paths.append(str(target))
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            with open(target, "a", encoding="utf-8") as fh:
                fh.write("this must never land\n")

    def test_writing_to_host_knowledge_raises(self, tmp_path, _guard_host_paths):
        target = tmp_path / "protected-knowledge"
        _guard_host_paths.append(str(target))
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            with open(target / "neutral.md", "w", encoding="utf-8") as fh:
                fh.write("nope")

    def test_path_write_text_to_host_path_raises(self, tmp_path, _guard_host_paths):
        """``Path.write_text`` bypasses ``builtins.open`` — and is the idiom the
        framework actually uses for every memory/L1 write."""
        target = tmp_path / "protected-memory.md"
        _guard_host_paths.append(str(target))
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            target.write_text("this must never land\n", encoding="utf-8")

    def test_reading_host_path_is_still_allowed(self, tmp_path, monkeypatch):
        """Exercise the guard without depending on a readable real home.

        Classify a sandbox file as protected for the guard only. Actual I/O
        still uses the sandbox file; The host-managed path need not exist or be readable.
        The write control proves this did not accidentally bypass the guard.
        """
        probe = tmp_path / "guard-read-probe.md"
        probe.write_text("read-only evidence", encoding="utf-8")
        real_abspath = os.path.abspath
        monkeypatch.setattr(
            os.path, "abspath",
            lambda path: _REAL_MEMORY if str(path) == str(probe) else real_abspath(path),
        )
        with open(probe, encoding="utf-8") as fh:
            assert fh.read() == "read-only evidence"
        assert probe.read_text(encoding="utf-8") == "read-only evidence"
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            probe.write_text("forbidden", encoding="utf-8")

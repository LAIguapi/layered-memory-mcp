"""
Tests for v3.2.0: memory_mode — agent-memory write mode.

Covers:
  - config default is "pointers" (backward compatible)
  - invalid mode rejected with ValueError
  - "off": dual-write skipped, memory file untouched
  - "index_only": single knowledge-index entry upserted; per-domain [L0]
    pointer copies reaped (deleted, NOT migrated); non-index entries kept
  - "index_only": re-run refreshes domain count, never duplicates index line
  - "index_only": domain count matches knowledge dir .md files
  - "pointers": legacy per-domain pointer upsert unchanged
  - auto_maintain_after_write dispatches through memory_mode

All fixtures use neutral placeholder content (no business data).
"""

import os
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.memory_compactor import (
    auto_maintain_after_write,
    _ensure_index_entry_in_memory,
    _ensure_l0_pointer_in_memory,
    _knowledge_index_marker,
)


def _make_config(tmp_path: Path, memory_file: Path, **kwargs) -> MemoryConfig:
    """Build a MemoryConfig pointed at a temp home + explicit agent memory file."""
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(exist_ok=True)
    os.environ["LAYERED_MEMORY_AGENT_MEMORY_PATH"] = str(memory_file)
    os.environ["LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR"] = "§"
    return MemoryConfig(
        home=str(tmp_path),
        knowledge_dir=str(knowledge_dir),
        **kwargs,
    )


def _seed_knowledge(tmp_path: Path, names: list[str]) -> None:
    kdir = tmp_path / "knowledge"
    kdir.mkdir(exist_ok=True)
    for n in names:
        (kdir / f"{n}.md").write_text(f"# {n}\n\nneutral placeholder\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env():
    saved = {
        k: os.environ.get(k)
        for k in (
            "LAYERED_MEMORY_AGENT_MEMORY_PATH",
            "LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR",
            "LAYERED_MEMORY_MEMORY_MODE",
        )
    }
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# --- config ---------------------------------------------------------------


def test_default_mode_is_pointers(tmp_path):
    cfg = _make_config(tmp_path, tmp_path / "MEMORY.md")
    assert cfg.memory_mode == "pointers"


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYERED_MEMORY_MEMORY_MODE", "index_only")
    cfg = MemoryConfig(home=str(tmp_path), knowledge_dir=str(tmp_path / "knowledge"))
    assert cfg.memory_mode == "index_only"


def test_invalid_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="memory_mode"):
        _make_config(tmp_path, tmp_path / "MEMORY.md", memory_mode="bogus")


# --- off mode -------------------------------------------------------------


def test_off_mode_skips_dual_write(tmp_path):
    mem = tmp_path / "MEMORY.md"
    mem.write_text("existing note about database pools\n", encoding="utf-8")
    cfg = _make_config(tmp_path, mem, memory_mode="off")

    report = _ensure_l0_pointer_in_memory(
        "[L0] topic-a: something → knowledge/topic-a.md", cfg
    )

    assert report["action"] == "skipped"
    assert report["reason"] == "memory_mode=off"
    assert mem.read_text(encoding="utf-8") == "existing note about database pools\n"


# --- index_only mode ------------------------------------------------------

_POINTER_A = "[L0] topic-a: summary a → knowledge/topic-a.md"
_POINTER_B = "[L0] topic-b: summary b → knowledge/topic-b.md"
_FACT = "cache TTL should stay under five minutes for the auth service"


def test_index_only_reaps_pointers_and_keeps_facts(tmp_path):
    mem = tmp_path / "MEMORY.md"
    mem.write_text(f"{_POINTER_A}\n§\n{_FACT}\n§\n{_POINTER_B}\n", encoding="utf-8")
    _seed_knowledge(tmp_path, ["topic-a", "topic-b", "topic-c"])
    cfg = _make_config(tmp_path, mem, memory_mode="index_only")

    report = _ensure_l0_pointer_in_memory(
        "[L0] topic-c: summary c → knowledge/topic-c.md", cfg
    )

    assert report["action"] == "added"
    assert report["pointers_removed"] == 2

    body = mem.read_text(encoding="utf-8")
    assert _POINTER_A not in body
    assert _POINTER_B not in body
    assert _FACT in body  # non-index content untouched
    assert body.count(_knowledge_index_marker(cfg)) == 1
    assert "3 domain(s)" in body


def test_index_only_upserts_without_duplicates(tmp_path):
    mem = tmp_path / "MEMORY.md"
    _seed_knowledge(tmp_path, ["topic-a"])
    cfg = _make_config(tmp_path, mem, memory_mode="index_only")

    _ensure_index_entry_in_memory(cfg)
    assert "1 domain(s)" in mem.read_text(encoding="utf-8")

    # knowledge base grows → count refreshes on next write
    _seed_knowledge(tmp_path, ["topic-a", "topic-b", "topic-c", "topic-d"])
    report = _ensure_index_entry_in_memory(cfg)

    assert report["action"] == "upserted"
    body = mem.read_text(encoding="utf-8")
    assert body.count(_knowledge_index_marker(cfg)) == 1
    assert "4 domain(s)" in body
    assert "1 domain(s)" not in body


def test_index_only_drops_duplicate_index_lines(tmp_path):
    mem = tmp_path / "MEMORY.md"
    marker_seed = "[L0] knowledge-index: 1 domain(s) — stale copy"
    mem.write_text(f"{marker_seed}\n§\n{marker_seed}\n", encoding="utf-8")
    _seed_knowledge(tmp_path, ["topic-a", "topic-b"])
    cfg = _make_config(tmp_path, mem, memory_mode="index_only")

    _ensure_index_entry_in_memory(cfg)

    body = mem.read_text(encoding="utf-8")
    assert body.count(_knowledge_index_marker(cfg)) == 1
    assert "2 domain(s)" in body


def test_index_only_empty_memory_gets_index(tmp_path):
    mem = tmp_path / "MEMORY.md"
    _seed_knowledge(tmp_path, ["topic-a", "topic-b"])
    cfg = _make_config(tmp_path, mem, memory_mode="index_only")

    report = _ensure_index_entry_in_memory(cfg)

    assert report["action"] == "added"
    body = mem.read_text(encoding="utf-8")
    assert body.startswith(_knowledge_index_marker(cfg))
    assert "2 domain(s)" in body


# --- pointers mode (legacy, unchanged) -------------------------------------


def test_pointers_mode_legacy_upsert(tmp_path):
    mem = tmp_path / "MEMORY.md"
    cfg = _make_config(tmp_path, mem, memory_mode="pointers")
    pointer = "[L0] topic-a: summary → knowledge/topic-a.md"

    report = _ensure_l0_pointer_in_memory(pointer, cfg)

    assert report["action"] == "added"
    assert pointer in mem.read_text(encoding="utf-8")


# --- auto_maintain dispatch ------------------------------------------------


def test_auto_maintain_dispatches_index_only(tmp_path):
    mem = tmp_path / "MEMORY.md"
    mem.write_text(f"{_POINTER_A}\n", encoding="utf-8")
    _seed_knowledge(tmp_path, ["topic-a", "topic-b"])
    cfg = _make_config(tmp_path, mem, memory_mode="index_only")

    report = auto_maintain_after_write(
        cfg,
        l0_pointer="[L0] topic-b: summary b → knowledge/topic-b.md",
        domain="topic-b",
        filepath=tmp_path / "knowledge" / "topic-b.md",
    )

    assert report["dual_write"]["action"] == "added"
    body = mem.read_text(encoding="utf-8")
    assert _POINTER_A not in body
    assert body.count(_knowledge_index_marker(cfg)) == 1


def test_auto_maintain_dispatches_off(tmp_path):
    mem = tmp_path / "MEMORY.md"
    # Empty memory: no bloat, so lazy compaction stays out of the way and
    # the assertion isolates the dual-write branch alone.
    cfg = _make_config(tmp_path, mem, memory_mode="off")

    report = auto_maintain_after_write(
        cfg,
        l0_pointer="[L0] topic-a: summary → knowledge/topic-a.md",
        domain="topic-a",
        filepath=None,
    )

    assert report["dual_write"]["action"] == "skipped"
    assert not mem.exists() or mem.read_text(encoding="utf-8") == ""

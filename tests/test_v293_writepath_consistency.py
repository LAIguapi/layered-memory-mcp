"""v2.9.3 write-path consistency regression tests.

Root cause audited 2026-06-30: the L1↔agent-memory↔vector dual/triple-write
was only wired into inject_knowledge. update_knowledge_file /
create_knowledge_file / delete_knowledge_file each maintained a *different*
subset of the three stores, so they silently drifted:

  1. update/create wrote the WHOLE file as one vector while inject wrote
     per-section vectors → stale section vectors became orphans.
  2. update/create never ran the dual-write → the domain's L0 pointer in agent
     memory was never refreshed/deduped (21 misc pointers accumulated).
  3. delete cleaned the L0 index + vectors but left the agent-memory [L0]
     pointer dangling.
  4. inject's auto_maintain swallowed every exception with a bare `pass`,
     hiding dedup failures.

These tests pin the fixed behaviour so the holes can't silently reopen.
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from layered_memory_mcp.injector import sync_to_vector_store
from layered_memory_mcp.memory_compactor import (
    _ensure_l0_pointer_in_memory,
    _remove_l0_pointer_from_memory,
    _pointer_domain_matches,
)
from layered_memory_mcp.config import MemoryConfig


def _mk_config(tmp_path):
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"))


# --- Root cause 1+2: whole-file vector rebuild (replace_domain) -------------

def test_replace_domain_rebuilds_one_vector_per_section(tmp_path):
    """A whole-file write rebuilds vectors per-section, no orphans left."""
    cfg = _mk_config(tmp_path)
    data_dir = Path(cfg.home) / "data"

    # First write: 2 sections.
    v1 = "# infra\n\n> intro\n\n## sec_a\nbody a\n\n## sec_b\nbody b\n"
    r1 = sync_to_vector_store(data_dir, "infra", v1, replace_domain=True)
    assert r1["success"] and r1["sections"] == 2

    db = data_dir / "vectors.db"
    with sqlite3.connect(db) as c:
        n = c.execute("SELECT COUNT(*) FROM vectors WHERE domain='infra'").fetchone()[0]
    assert n == 2, f"expected 2 section vectors, got {n}"

    # Second write removes sec_b, adds sec_c — old sec_b vector must NOT linger.
    v2 = "# infra\n\n## sec_a\nbody a\n\n## sec_c\nbody c\n"
    r2 = sync_to_vector_store(data_dir, "infra", v2, replace_domain=True)
    assert r2["sections"] == 2

    with sqlite3.connect(db) as c:
        rows = [r[0] for r in c.execute(
            "SELECT text FROM vectors WHERE domain='infra'"
        ).fetchall()]
    assert len(rows) == 2, f"orphan left behind: {rows}"
    joined = "\n".join(rows)
    assert "sec_c" in joined and "sec_b" not in joined


def test_replace_domain_skips_header_and_blockquote(tmp_path):
    """The file-level '# title' and '> intro' must not become vectors."""
    cfg = _mk_config(tmp_path)
    data_dir = Path(cfg.home) / "data"
    content = "# misc\n\n> 杂项缓冲区。\n\n## real_section\nreal body\n"
    r = sync_to_vector_store(data_dir, "misc", content, replace_domain=True)
    assert r["sections"] == 1
    with sqlite3.connect(data_dir / "vectors.db") as c:
        texts = [t[0] for t in c.execute("SELECT text FROM vectors").fetchall()]
    assert all("杂项缓冲区" not in t.split("\n")[0] for t in texts)


# --- Root cause 2: dual-write dedups same-domain pointers -------------------

def test_dual_write_replaces_stale_same_domain_pointer(tmp_path):
    """Re-writing a domain replaces its old pointer, never accumulates."""
    cfg = _mk_config(tmp_path)
    mem = Path(cfg.home) / "MEMORY.md"
    mem.write_text("", encoding="utf-8")

    p1 = "[L0] infra: old summary → knowledge/infra.md"
    r1 = _ensure_l0_pointer_in_memory(p1, cfg, memory_path=mem)
    assert r1["action"] == "added"

    p2 = "[L0] infra: NEW summary → knowledge/infra.md"
    r2 = _ensure_l0_pointer_in_memory(p2, cfg, memory_path=mem)
    assert r2["action"] == "replaced"

    body = mem.read_text(encoding="utf-8")
    assert body.count("→ knowledge/infra.md") == 1, "pointer accumulated!"
    assert "NEW summary" in body and "old summary" not in body


# --- Root cause 3: delete reaps the dangling agent-memory pointer -----------

def test_delete_removes_dangling_pointer(tmp_path):
    """Deleting a domain reaps its [L0] pointer from agent memory."""
    cfg = _mk_config(tmp_path)
    mem = Path(cfg.home) / "MEMORY.md"
    mem.write_text(
        "[L0] infra: x → knowledge/infra.md\n§\n"
        "[L0] stock: y → knowledge/stock.md\n",
        encoding="utf-8",
    )
    r = _remove_l0_pointer_from_memory("infra", cfg, memory_path=mem)
    assert r["action"] == "removed" and r["removed"] == 1

    body = mem.read_text(encoding="utf-8")
    assert "knowledge/infra.md" not in body
    assert "knowledge/stock.md" in body  # sibling untouched


def test_delete_reaps_pointer_even_without_file_tail(tmp_path):
    """A pointer whose '→ knowledge/x.md' tail was lost is still reaped by domain label."""
    cfg = _mk_config(tmp_path)
    mem = Path(cfg.home) / "MEMORY.md"
    mem.write_text("[L0] infra: summary with no tail\n§\n[L0] keep: z → knowledge/keep.md\n", encoding="utf-8")
    r = _remove_l0_pointer_from_memory("infra", cfg, memory_path=mem)
    assert r["action"] == "removed" and r["removed"] == 1
    assert "keep.md" in mem.read_text(encoding="utf-8")


def test_pointer_domain_matches_exact_label_only(tmp_path):
    """_pointer_domain_matches must not match on substring collisions."""
    assert _pointer_domain_matches("[L0] infra: x → knowledge/infra.md", "infra")
    # 'infra' must not match 'infra-network'
    assert not _pointer_domain_matches(
        "[L0] infra-network: x → knowledge/infra-network.md", "infra"
    )

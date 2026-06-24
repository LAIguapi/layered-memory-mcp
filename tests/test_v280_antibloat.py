"""v2.8.0 anti-bloat regression tests.

Covers the three holes that let misc.md grow to 1.28MB / vectors.db to 4449 rows:
  1. _do_write append path: exact in-section duplicate is skipped.
  2. _resolve_action: append mode skips near-verbatim duplicate.
  3. VectorStore.add: exact (domain, text) duplicate is not re-inserted.
  4. dedup_l1_knowledge: bloated file gets line-deduped + vectors pruned.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp import injector
from layered_memory_mcp.memory_compactor import dedup_l1_file, dedup_l1_knowledge


def _mk_config(tmp_path):
    home = tmp_path / ".layered-memory"
    home.mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(home / "knowledge"),
    )


def test_append_skips_exact_intra_section_duplicate(tmp_path):
    """Writing the same content twice in append mode must not duplicate it."""
    cfg = _mk_config(tmp_path)
    line = "WSL 代理走 127.0.0.1:7890，cron 任务需显式 export。"

    r1 = injector.inject_knowledge(cfg, "infra", "网络", line, mode="append")
    assert r1["success"]

    # Second identical append must be a no-op (skipped or append_no_change).
    r2 = injector.inject_knowledge(cfg, "infra", "网络", line, mode="append")
    assert r2["success"]

    fp = Path(cfg.knowledge_dir) / "infra.md"
    text = fp.read_text(encoding="utf-8")
    assert text.count(line) == 1, f"duplicate written! count={text.count(line)}"


def test_append_many_times_stays_single(tmp_path):
    """The exact bloat scenario: append the same line 50x → still 1 copy."""
    cfg = _mk_config(tmp_path)
    line = "海外机集群共5台，巡检前先读 paste 取凭证。"
    for _ in range(50):
        injector.inject_knowledge(cfg, "infra", "机群", line, mode="append")
    fp = Path(cfg.knowledge_dir) / "infra.md"
    text = fp.read_text(encoding="utf-8")
    assert text.count(line) == 1, f"bloat! line appears {text.count(line)} times"


def test_vector_store_exact_dedup(tmp_path):
    """VectorStore.add must not insert a verbatim (domain,text) duplicate."""
    from layered_memory_mcp.storage.vector_store import VectorStore
    from layered_memory_mcp.models import (
        KnowledgeEntry, SourceInfo, SourceType, ReviewStatus, KnowledgeType,
    )
    import uuid

    db = tmp_path / "vectors.db"
    vs = VectorStore(db)

    def _entry():
        return KnowledgeEntry(
            id=str(uuid.uuid4()),
            domain="misc",
            section="misc",
            content="同一条内容反复写",
            summary="misc",
            type=KnowledgeType.FACT,
            confidence=0.9,
            review_status=ReviewStatus.APPROVED,
            source=SourceInfo(type=SourceType.MANUAL, extracted_by="test"),
        )

    for _ in range(10):
        vs.add(_entry())

    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM vectors WHERE domain='misc'").fetchone()[0]
    assert n == 1, f"vector dup! {n} rows for identical content"


def test_dedup_l1_file_reaps_duplicate_lines(tmp_path):
    """dedup_l1_file collapses repeated lines but keeps structure + uniques."""
    fp = tmp_path / "misc.md"
    body = "# misc\n\n## 段落A\n重复行X\n重复行X\n重复行X\n唯一行Y\n\n## 段落B\n重复行X\n唯一行Z\n"
    fp.write_text(body, encoding="utf-8")

    res = dedup_l1_file(fp)
    assert res["action"] == "deduped"
    out = fp.read_text(encoding="utf-8")
    # "重复行X" collapses to a single occurrence across the whole file.
    assert out.count("重复行X") == 1
    # Uniques and headings survive.
    assert "唯一行Y" in out and "唯一行Z" in out
    assert "## 段落A" in out and "## 段落B" in out


def test_dedup_l1_knowledge_scan(tmp_path):
    """End-to-end: a bloated file in the knowledge dir gets slimmed."""
    cfg = _mk_config(tmp_path)
    kdir = Path(cfg.knowledge_dir)
    kdir.mkdir(parents=True, exist_ok=True)
    fp = kdir / "bloated.md"
    dup = "\n".join(["同一条脏数据"] * 100)
    fp.write_text(f"# bloated\n\n## x\n{dup}\n", encoding="utf-8")

    report = dedup_l1_knowledge(cfg, min_dup_lines=20)
    assert report["scanned"] >= 1
    out = fp.read_text(encoding="utf-8")
    assert out.count("同一条脏数据") == 1

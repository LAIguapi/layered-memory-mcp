#!/usr/bin/env python3
"""
One-time script to rebuild the vector store index from all L1 knowledge files.

Usage:
    python scripts/rebuild_vectors.py

This scans all .md files in ~/.layered-memory/knowledge/ and indexes
each one into the vector store. Idempotent — safely re-runnable.
"""

import sys
import uuid
from pathlib import Path

# Ensure we can import layered_memory_mcp
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from layered_memory_mcp.storage.vector_store import VectorStore
from layered_memory_mcp.models import (
    KnowledgeEntry, SourceInfo, SourceType, ReviewStatus, KnowledgeType,
)
from layered_memory_mcp.config import MemoryConfig


def rebuild(config: MemoryConfig | None = None):
    if config is None:
        config = MemoryConfig()

    knowledge_dir = config.knowledge_dir
    db_path = config.home / "data" / "vectors.db"

    print(f"Knowledge dir: {knowledge_dir}")
    print(f"Vector DB:     {db_path}")

    if not knowledge_dir.exists():
        print("ERROR: Knowledge directory not found.")
        return 1

    md_files = sorted(knowledge_dir.glob("*.md"))
    print(f"Found {len(md_files)} .md files")

    vector_store = VectorStore(db_path)

    indexed = 0
    skipped = 0
    errors = 0

    for fpath in md_files:
        domain = fpath.stem
        try:
            content = fpath.read_text(encoding="utf-8")

            entry = KnowledgeEntry(
                id=str(uuid.uuid4()),
                domain=domain,
                section=domain,
                content=content,
                summary=f"L1: {domain}",
                type=KnowledgeType.FACT,
                confidence=0.9,
                review_status=ReviewStatus.APPROVED,
                source=SourceInfo(
                    type=SourceType.MANUAL, extracted_by="rebuild_vectors"
                ),
            )
            vector_store.add(entry)
            indexed += 1
            print(f"  ✓ {domain}.md ({len(content)} bytes)")
        except Exception as e:
            errors += 1
            print(f"  ✗ {domain}.md: {e}")

    stats = vector_store.stats()
    print(f"\nDone. Indexed: {indexed}, Skipped: {skipped}, Errors: {errors}")
    print(f"Vector store: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(rebuild())

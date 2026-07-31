"""L1 knowledge file storage with frontmatter support.

Reads and writes markdown files with YAML frontmatter.
Backwards compatible with v1.x files (no frontmatter).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from filelock import FileLock

from .frontmatter import dump_frontmatter, parse_frontmatter

if TYPE_CHECKING:
    from ..models import KnowledgeEntry

logger = logging.getLogger("layered_memory_mcp.storage.l1")

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB safety limit


def read_knowledge_file(filepath: Path) -> tuple[dict | None, str]:
    """Read a knowledge file, returning (frontmatter_metadata, content).

    Returns (None, content) for legacy files without frontmatter.
    """
    if not filepath.exists():
        return None, ""

    try:
        raw = filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Cannot read %s: %s", filepath, e)
        return None, ""

    meta, content = parse_frontmatter(raw)
    return meta, content


def write_knowledge_file(
    filepath: Path,
    entry: "KnowledgeEntry",
    backup: bool = True,
) -> dict:
    """Write a KnowledgeEntry to a markdown file with frontmatter.

    Args:
        filepath: Target file path.
        entry: KnowledgeEntry to serialize.
        backup: If True, create .bak before overwrite.

    Returns:
        Result dict with success, bytes_written, etc.
    """
    # Build metadata from entry
    meta = entry.model_dump(exclude={"content"})

    # Serialize
    try:
        full_text = dump_frontmatter(meta, entry.content)
    except Exception as e:
        return {"success": False, "error": f"Serialization failed: {e}"}

    # Ensure directory exists
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Lock for concurrency safety
    lock_path = filepath.with_suffix(filepath.suffix + ".lock")
    lock = FileLock(str(lock_path), timeout=10)

    try:
        with lock:
            # Backup existing file
            if backup and filepath.exists():
                try:
                    bak_path = filepath.with_suffix(filepath.suffix + ".bak")
                    bak_path.write_text(filepath.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass  # Non-critical

            filepath.write_text(full_text, encoding="utf-8")

        return {
            "success": True,
            "bytes_written": len(full_text.encode("utf-8")),
            "file_size_bytes": len(full_text.encode("utf-8")),
        }
    except Exception as e:
        logger.error("Write error for %s: %s", filepath, e)
        return {"success": False, "error": str(e)}
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


class L1Store:
    """High-level interface for L1 knowledge file operations."""

    def __init__(self, knowledge_dir: Path):
        self.knowledge_dir = Path(knowledge_dir)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, domain: str) -> Path:
        """Resolve domain to file path."""
        filename = domain if domain.endswith(".md") else f"{domain}.md"
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"Invalid domain name: {domain}")
        return self.knowledge_dir / filename

    def read(self, domain: str) -> tuple[dict | None, str]:
        """Read a knowledge file by domain."""
        filepath = self._resolve_path(domain)
        return read_knowledge_file(filepath)

    def write(self, entry: "KnowledgeEntry", backup: bool = True) -> dict:
        """Write a KnowledgeEntry to L1 storage."""
        filepath = self._resolve_path(entry.domain)
        return write_knowledge_file(filepath, entry, backup=backup)

    def list_domains(self) -> list[str]:
        """List all knowledge domains (files)."""
        domains = []
        for f in sorted(self.knowledge_dir.glob("*.md")):
            if f.name.endswith(".bak") or f.name.endswith(".lock"):
                continue
            domains.append(f.stem)
        return domains

    def delete(self, domain: str) -> bool:
        """Delete a knowledge file."""
        filepath = self._resolve_path(domain)
        if filepath.exists():
            filepath.unlink()
            # Clean up backup
            bak = filepath.with_suffix(filepath.suffix + ".bak")
            if bak.exists():
                bak.unlink()
            return True
        return False

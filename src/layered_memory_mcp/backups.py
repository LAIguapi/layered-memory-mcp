"""L1 knowledge backups, kept out of the live knowledge directory.

Every whole-file rewrite used to drop ``<file>.md.bak`` right next to the live
file. The knowledge dir is what recall, the L0 sync and the operator all read,
so those siblings were noise at best — and a second, unindexed copy of every
knowledge file at worst (old secrets survive an edit that was meant to remove
them).

Backups now live under the framework's own data root::

    <home>/data/backups/knowledge/<relative path>.bak

One backup per source file, overwritten on each write — the same bounded
one-backup-per-file model the adjacent ``.bak`` had, just somewhere else. The
relative path is kept so files in a shared dir (or a subdirectory) never
collide with a namespace file of the same name.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger(__name__)


def knowledge_backup_root(config: "MemoryConfig") -> Path:
    return Path(config.home) / "data" / "backups" / "knowledge"


def knowledge_backup_path(config: "MemoryConfig", filepath: Path) -> Path:
    """Stable backup location for ``filepath`` (one per source file).

    The primary knowledge dir maps straight onto the backup root; any other
    configured knowledge dir (the shared one) gets a ``shared/`` prefix, and a
    file outside every knowledge dir lands under ``_external/`` by name.
    """
    root = knowledge_backup_root(config)
    filepath = Path(filepath)
    kdirs = list(getattr(config, "knowledge_dirs", None) or [config.knowledge_dir])
    for i, kdir in enumerate(kdirs):
        try:
            rel = filepath.resolve().relative_to(Path(kdir).resolve())
        except (ValueError, OSError):
            continue
        base = root if i == 0 else root / "shared"
        return base / rel.parent / (rel.name + ".bak")
    return root / "_external" / (filepath.name + ".bak")


def backup_knowledge_file(
    config: "MemoryConfig", filepath: Path, content: str | None = None
) -> dict:
    """Write the backup of ``filepath`` (its current content unless given).

    Never raises: a backup failure must not block the write it protects.
    Returns ``{"path", "bytes"}`` on success, ``{"skipped": reason}`` when
    there is nothing to back up, or ``{"error": message}``.
    """
    try:
        if content is None:
            if not Path(filepath).exists():
                return {"skipped": "source does not exist"}
            content = Path(filepath).read_text(encoding="utf-8")
        dest = knowledge_backup_path(config, filepath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return {"path": str(dest), "bytes": len(content.encode("utf-8"))}
    except Exception as e:  # noqa: BLE001 — backups are non-critical
        logger.warning("Knowledge backup failed for %s: %s", filepath, e)
        return {"error": str(e)}

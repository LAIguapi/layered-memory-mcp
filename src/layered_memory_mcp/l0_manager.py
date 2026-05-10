"""
L0 Index Manager — Read, write, sync the L0 index layer.

L0 is the compact index injected into every agent turn. It maps domain names
to L1 knowledge files so the agent knows *what knowledge exists* without
loading all of it.

Two formats are supported:
  - "hermes":  [L0] domain: summary → knowledge/file.md
  - "generic": [file.md] Title → keyword1, keyword2
"""

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .recall import generate_l0_index, invalidate_scan_cache, scan_knowledge_files

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger("layered_memory_mcp.l0_manager")

# Default patterns — dynamically rebuilt when config provides a custom l0_tag
_DEFAULT_L0_TAG = "[L0]"
_HERMES_ENTRY_RE = re.compile(
    r"^\[L0\]\s*(?P<domain>[^:：]+)[：:]\s*(?P<summary>.+?)\s*→\s*(?P<path>.+)$"
)
_GENERIC_ENTRY_RE = re.compile(
    r"^\[(?P<file>[^\]]+)\]\s*(?P<title>.+?)(?:\s*→\s*(?P<keywords>.+))?$"
)

# Safe variant — more permissive for path extraction (used in _extract_referenced_files)
_HERMES_ENTRY_RE_SAFE = re.compile(
    r"^\[L0\]\s*[^:：]+[：:]\s*.+?\s*→\s*(?P<path>\S+)$"
)


def _get_hermes_re(config=None) -> re.Pattern:
    """Get the hermes-entry regex, using the configured l0_tag.

    Also matches the legacy ``[L0索引]`` tag for backward compatibility.
    """
    tag = getattr(config, "l0_tag", None) or _DEFAULT_L0_TAG
    tag_escaped = re.escape(tag)
    legacy = re.escape("[L0索引]")
    return re.compile(
        rf"(?:{tag_escaped}|{legacy})\s*(?P<domain>[^:：]+)[：:]\s*(?P<summary>.+?)\s*→\s*(?P<path>.+)$"
    )


def _get_hermes_re_safe(config=None) -> re.Pattern:
    """Get the safe hermes-entry regex (path extraction only).

    Also matches the legacy ``[L0索引]`` tag for backward compatibility.
    """
    tag = getattr(config, "l0_tag", None) or _DEFAULT_L0_TAG
    tag_escaped = re.escape(tag)
    legacy = re.escape("[L0索引]")
    return re.compile(
        rf"(?:{tag_escaped}|{legacy})\s*[^:：]+[：:]\s*.+?\s*→\s*(?P<path>\S+)$"
    )


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

def sync_l0_index(
    config: "MemoryConfig",
    dry_run: bool = False,
    l0_format: str | None = None,
) -> dict:
    """Synchronise L0 index with actual L1 knowledge files.

    Args:
        config: MemoryConfig instance.
        dry_run: If True, return preview without writing.
        l0_format: Override L0 format for this sync only (avoids mutating config).

    Returns a report regardless of dry_run.
    """
    # Use override format if provided, otherwise config default
    effective_format = l0_format or config.l0_format

    l0_file = config.l0_index_file

    # Scan existing L1 files across all knowledge dirs (namespace + shared)
    kdirs = [str(d) for d in config.knowledge_dirs]
    if len(kdirs) > 1:
        from .recall import scan_knowledge_dirs
        l1_files = scan_knowledge_dirs(kdirs)
    else:
        l1_files = scan_knowledge_files(kdirs[0])

    # --- Generate fresh index content ---
    if effective_format == "hermes":
        new_lines = _generate_hermes_index(kdirs if len(kdirs) > 1 else kdirs[0], l1_files, config)
    else:
        new_content_raw = generate_l0_index(kdirs if len(kdirs) > 1 else kdirs[0])
        new_lines = new_content_raw.split("\n") if new_content_raw else []

    new_content = "\n".join(new_lines) + "\n" if new_lines else ""

    # --- Read existing L0 entries (for diff report) ---
    existing_entries: set[str] = set()
    if l0_file and l0_file.exists():
        try:
            existing_text = l0_file.read_text(encoding="utf-8")
            existing_entries = _parse_entry_domains(existing_text, effective_format, config)
        except Exception as e:
            logger.warning("Failed to read existing L0: %s", e)

    # Compute diff
    new_domains = _parse_entry_domains(new_content, effective_format, config)
    added = new_domains - existing_entries
    removed = existing_entries - new_domains
    unchanged = existing_entries & new_domains

    report = {
        "success": True,
        "l0_file": str(l0_file) if l0_file else None,
        "l0_format": effective_format,
        "l1_files_found": len(l1_files),
        "entries_added": len(added),
        "entries_removed": len(removed),
        "entries_unchanged": len(unchanged),
        "total_entries": len(new_domains),
        "added": sorted(added),
        "removed": sorted(removed),
    }

    if dry_run:
        report["preview"] = new_content
        return report

    # --- Write ---
    if l0_file:
        try:
            if new_content:
                l0_file.write_text(new_content, encoding="utf-8")
                logger.info("Synced L0 index: %d entries → %s", len(new_domains), l0_file)
            else:
                # No L1 files at all — write empty marker
                l0_file.write_text("# L0 Index (empty — no L1 knowledge files)\n", encoding="utf-8")
            report["bytes_written"] = len(new_content.encode("utf-8")) if new_content else 0
        except Exception as e:
            logger.error("Failed to write L0 index: %s", e)
            report["success"] = False
            report["error"] = str(e)
    else:
        report["l0_file"] = None
        report["note"] = "L0 index file not configured — sync skipped (L1 files are up to date)"

    return report




# ---------------------------------------------------------------------------
# Lightweight L0 consistency check for stdio mode (v0.6.0)
# ---------------------------------------------------------------------------

def quick_l0_consistency_check(config) -> dict | None:
    """Fast mtime-based check: is L0 index stale?

    Compares the mtime of the L0 index file against the latest mtime
    of all L1 knowledge files. If any L1 file is newer than L0, returns
    a report indicating staleness.

    Designed for stdio mode where the watcher thread doesn't run.
    Call this at the start of read operations (e.g., recall_knowledge).

    Throttled: returns None if called more than once within 30 seconds.

    Returns:
        None if L0 is up-to-date or not configured.
        Dict with staleness info if L0 is stale.
    """
    if not config.l0_index_file or not config.l0_index_file.exists():
        return None

    # Throttle: skip check if last check was less than 30s ago
    now = time.time()
    if hasattr(quick_l0_consistency_check, '_last_check_ts'):
        if now - quick_l0_consistency_check._last_check_ts < 30.0:
            return None
    quick_l0_consistency_check._last_check_ts = now

    try:
        l0_mtime = config.l0_index_file.stat().st_mtime
    except OSError:
        return None

    # v0.6.0: namespace-aware scanning
    kdirs = [str(d) for d in config.knowledge_dirs]
    if len(kdirs) > 1:
        from .recall import scan_knowledge_dirs
        l1_files = scan_knowledge_dirs(kdirs)
    else:
        l1_files = scan_knowledge_files(kdirs[0])

    stale_files = []
    newest_l1_mtime = 0.0
    for name, path_str in l1_files.items():
        try:
            mtime = Path(path_str).stat().st_mtime
            if mtime > newest_l1_mtime:
                newest_l1_mtime = mtime
            if mtime > l0_mtime:
                stale_files.append(name)
        except OSError:
            continue

    if not stale_files:
        return None

    return {
        "stale": True,
        "l0_mtime": l0_mtime,
        "newest_l1_mtime": newest_l1_mtime,
        "stale_files": stale_files[:10],
        "stale_count": len(stale_files),
        "suggestion": "L0 index is stale — call sync_l0_index to update",
    }

def auto_sync_if_enabled(config) -> dict | None:
    """Convenience wrapper: sync L0 if auto_sync_l0 is enabled.

    Returns the sync report, or None if skipped.
    """
    if not config.auto_sync_l0:
        return None
    return sync_l0_index(config, dry_run=False)


# ---------------------------------------------------------------------------
# Entry management (add / remove / replace individual entries)
# ---------------------------------------------------------------------------

def manage_entry(
    config,
    action: str,
    domain: str,
    summary: str | None = None,
    filename: str | None = None,
) -> dict:
    """Add, remove, or replace a single L0 entry.

    This is for fine-grained control when you don't want to regenerate
    the entire L0 index.
    """
    l0_file = config.l0_index_file
    if not l0_file:
        return {"success": False, "error": "L0 index file not configured"}

    if not l0_file.exists():
        # Create empty file
        l0_file.parent.mkdir(parents=True, exist_ok=True)
        l0_file.write_text("", encoding="utf-8")

    try:
        content = l0_file.read_text(encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": str(e)}

    lines = [l for l in content.split("\n") if l.strip()]

    if action == "add":
        if not summary:
            return {"success": False, "error": "summary is required for 'add'"}
        fn = filename or f"{domain}.md"
        tag = getattr(config, "l0_tag", _DEFAULT_L0_TAG)
        if config.l0_format == "hermes":
            new_line = f"{tag} {domain}: {summary} → knowledge/{fn}"
        else:
            new_line = f"[{fn}] {summary}"
        # Check if entry already exists
        for line in lines:
            if _entry_matches_domain(line, domain, config.l0_format, config):
                return {"success": False, "error": f"Entry for '{domain}' already exists. Use 'replace'."}
        lines.append(new_line)

    elif action == "remove":
        original_count = len(lines)
        lines = [l for l in lines if not _entry_matches_domain(l, domain, config.l0_format, config)]
        if len(lines) == original_count:
            return {"success": False, "error": f"No entry found for '{domain}'"}

    elif action == "replace":
        if not summary:
            return {"success": False, "error": "summary is required for 'replace'"}
        fn = filename or f"{domain}.md"
        tag = getattr(config, "l0_tag", _DEFAULT_L0_TAG)
        if config.l0_format == "hermes":
            new_line = f"{tag} {domain}: {summary} → knowledge/{fn}"
        else:
            new_line = f"[{fn}] {summary}"
        found = False
        for i, line in enumerate(lines):
            if _entry_matches_domain(line, domain, config.l0_format, config):
                lines[i] = new_line
                found = True
                break
        if not found:
            lines.append(new_line)

    else:
        return {"success": False, "error": f"Unknown action: {action}"}

    new_content = "\n".join(lines) + "\n"
    try:
        l0_file.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": str(e)}

    return {
        "success": True,
        "action": action,
        "domain": domain,
        "l0_file": str(l0_file),
        "total_entries": len(lines),
    }


# ---------------------------------------------------------------------------
# L0 ↔ L1 consistency check
# ---------------------------------------------------------------------------

def check_l0_l1_consistency(config) -> dict:
    """Check that L0 index and L1 files are consistent.

    Returns orphaned L1 files (in L1 but not in L0) and stale L0 entries
    (in L0 but L1 file doesn't exist).
    """
    l0_file = config.l0_index_file

    # Scan across ALL knowledge dirs (namespace + shared) for full consistency
    kdirs = [str(d) for d in config.knowledge_dirs]
    for kdir in kdirs:
        invalidate_scan_cache(kdir)
    if len(kdirs) > 1:
        from .recall import scan_knowledge_dirs
        l1_files = scan_knowledge_dirs(kdirs)
    else:
        l1_files = scan_knowledge_files(kdirs[0])
    l1_set = set(l1_files.keys())

    # Parse L0 entries to find referenced L1 files
    l0_referenced: set[str] = set()
    if l0_file and l0_file.exists():
        try:
            l0_text = l0_file.read_text(encoding="utf-8")
            l0_referenced = _extract_referenced_files(l0_text, config.l0_format, config)
        except Exception:
            pass

    orphaned_l1 = sorted(l1_set - l0_referenced)
    stale_l0 = sorted(l0_referenced - l1_set)
    consistent = sorted(l1_set & l0_referenced)

    return {
        "orphaned_l1": orphaned_l1,       # L1 files not referenced in L0
        "stale_l0_entries": stale_l0,       # L0 references to non-existent L1
        "consistent": consistent,
        "total_l1": len(l1_set),
        "total_l0_refs": len(l0_referenced),
        "health": "good" if not orphaned_l1 and not stale_l0 else
                  "fair" if len(orphaned_l1) <= 2 else "poor",
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _generate_hermes_index(knowledge_dir: str, l1_files: dict, config=None) -> list[str]:
    """Generate L0 index in Hermes format from L1 files.

    Uses generate_l0_index() for generic format, then we build hermes-specific
    lines by reading each file's first heading and top keywords.
    """
    tag = getattr(config, "l0_tag", _DEFAULT_L0_TAG)
    # Reuse the generic generator — it already extracts title + keywords
    generic = generate_l0_index(knowledge_dir)
    if not generic:
        return []

    lines: list[str] = []
    for line in generic.split("\n"):
        if not line.strip():
            continue
        # Parse [file.md] Title → kw1, kw2
        m = _GENERIC_ENTRY_RE.match(line)
        if not m:
            continue
        gd = m.groupdict()
        filename = gd.get("file", "").strip()
        title = gd.get("title", "").strip()
        keywords = gd.get("keywords", "").strip()

        # Derive domain from filename (strip .md)
        domain = filename.removesuffix(".md")
        # Build summary from title + keywords
        summary_parts = [title] if title else []
        if keywords:
            summary_parts.append(keywords)
        summary = " — ".join(summary_parts) if summary_parts else domain

        lines.append(f"{tag} {domain}: {summary} → knowledge/{filename}")

    return lines


def _parse_entry_domains(content: str, fmt: str, config=None) -> set[str]:
    """Extract domain names from L0 index content."""
    domains: set[str] = set()
    hermes_re = _get_hermes_re(config) if fmt == "hermes" else None
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if fmt == "hermes":
            m = hermes_re.match(line)
            if m:
                domains.add(m.group("domain").strip())
        else:
            m = _GENERIC_ENTRY_RE.match(line)
            if m:
                domains.add(m.group("file").removesuffix(".md"))
    return domains


def _entry_matches_domain(line: str, domain: str, fmt: str, config=None) -> bool:
    """Check if an L0 entry line matches the given domain."""
    if fmt == "hermes":
        m = _get_hermes_re(config).match(line)
        return m is not None and m.group("domain").strip() == domain
    else:
        m = _GENERIC_ENTRY_RE.match(line)
        return m is not None and m.group("file").removesuffix(".md") == domain


def _extract_referenced_files(content: str, fmt: str, config=None) -> set[str]:
    """Extract L1 filenames referenced in L0 index."""
    files: set[str] = set()
    hermes_re_safe = _get_hermes_re_safe(config) if fmt == "hermes" else None
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if fmt == "hermes":
            m = hermes_re_safe.match(line)
            if m:
                path = m.group("path").strip()
                files.add(Path(path).name)
        else:
            m = _GENERIC_ENTRY_RE.match(line)
            if m:
                files.add(m.group("file"))
    return files

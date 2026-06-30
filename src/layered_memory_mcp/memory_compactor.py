"""
Memory Compactor — Detect and migrate non-index entries in agent memory.

Agent memory file formats vary by platform:
  - Hermes: ~/.hermes/memories/MEMORY.md, entries separated by '§'
  - Claude Code: ~/.claude/CLAUDE.md, entries separated by blank lines
  - Cursor: ./.cursorrules
  - Generic: any text file

The L0 index layer should only contain short pointer entries like:
    [L0] domain: summary → knowledge/file.md

This module detects "bloat" entries (detailed knowledge that should be in L1)
and provides tools to migrate them to proper L1 knowledge files.
"""

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .injector import inject_knowledge as _inject_knowledge

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger("layered_memory_mcp.compactor")

# Default L0 index tag (overridden by config.l0_tag at runtime)
_DEFAULT_L0_TAG = "[L0]"
_L0_INDEX_PATTERN = re.compile(r"^\[L0\]\s*")


def _get_l0_pattern(config=None) -> re.Pattern:
    """Get the L0 index tag pattern, using the configured l0_tag.

    Also matches the legacy ``[L0索引]`` tag for backward compatibility
    with previously-written memory entries.
    """
    tag = getattr(config, "l0_tag", None) or _DEFAULT_L0_TAG
    tag_escaped = re.escape(tag)
    # Also accept the legacy Chinese tag for backward compat
    legacy = re.escape("[L0索引]")
    return re.compile(rf"(?:{tag_escaped}|{legacy})\s*")
# Also consider structured tag entries as "acceptable" (e.g. [思维框架·强制])
_STRUCT_TAG_PATTERN = re.compile(r"^\[.+[·\-].+\]\s*")

# Max chars for a "valid" memory entry (index pointers are short)
# Anything longer is likely detailed knowledge that belongs in L1
MAX_INDEX_ENTRY_LENGTH = 120

# Default memory capacity in chars (for capacity warning).
# Users can override via MEMORY_MAX_CHARS env var.
#
# Priority chain (see _get_memory_max_chars):
#   1. explicit max_chars argument
#   2. MEMORY_MAX_CHARS env var
#   3. Hermes config.yaml (memory.memory_char_limit / user_char_limit) — dynamic
#   4. smart default by memory-file type below
#
# The generic default (non-Hermes agents, blank-line separated) stays high.
# Hermes-style memory (§-separated MEMORY.md / USER.md) defaults to the
# Hermes built-in default of 2000 so we never *over*-estimate capacity and
# silently skip compaction. This is only a fallback — the real limit is read
# dynamically from config.yaml when available.
_DEFAULT_MEMORY_MAX_CHARS = 50_000
_HERMES_DEFAULT_MEMORY_MAX_CHARS = 2_000

# Generic fallback domain rules — only common English keywords.
# These are used when no YAML config file is provided.
_FALLBACK_DOMAIN_RULES: list[tuple[str, list[str]]] = [
    ("infra", ["proxy", "server", "docker", "ssh", "network", "deploy",
               "config", "cloud", "kubernetes", "nginx", "dns", "firewall",
               "linux", "shell", "bash", "cron"]),
    ("dev", ["principle", "testing", "DRY", "design", "refactor",
             "code review", "TDD", "architecture", "pattern"]),
    ("docs", ["readme", "documentation", "guide", "tutorial", "how-to"]),
]


def _resolve_memory_path(
    memory_path: str | Path | None = None,
    config: "MemoryConfig | None" = None,
) -> Path | None:
    """Resolve the agent memory file path.

    Priority:
      1. Explicit memory_path argument
      2. config.detect_agent_memory_path() (auto-detect)
      3. None (caller handles missing file)
    """
    if memory_path:
        return Path(memory_path)
    if config is not None:
        return config.detect_agent_memory_path()
    return None


def _resolve_separator(
    memory_path: Path | None = None,
    config: "MemoryConfig | None" = None,
) -> str:
    """Resolve the entry separator for the agent memory file.

    Priority:
      1. config.detect_agent_memory_separator() (auto-detect)
      2. File-name heuristic: files with 'memory' in the name
         (case-insensitive) use '§' (Hermes convention)
      3. Fallback to '\\n\\n' (blank-line separator — universal)
    """
    if config is not None:
        return config.detect_agent_memory_separator(memory_path)
    # No config — try simple heuristic from filename
    if memory_path and memory_path.exists():
        name = memory_path.name.lower()
        if "memory" in name:
            return "§"
    return "\n\n"


# ---------------------------------------------------------------------------
# Domain rules loader
# ---------------------------------------------------------------------------

def _load_domain_rules_from_config(config) -> list[tuple[str, list[str]]]:
    """Load domain rules from config (YAML file or MemoryConfig object).

    Priority:
      1. config.load_domain_rules() if available (MemoryConfig)
      2. config.compact_domain_rules_file if it's a path to a YAML
      3. Return None to signal "use fallback"
    """
    if config is None:
        return None

    # MemoryConfig objects have the helper method
    if hasattr(config, "load_domain_rules"):
        rules_dict = config.load_domain_rules()
        if rules_dict:
            return [(domain, keywords) for domain, keywords in rules_dict.items()]

    # Direct path to a YAML file
    rules_path = getattr(config, "compact_domain_rules_file", None)
    if rules_path is None:
        return None
    rules_path = Path(rules_path) if not isinstance(rules_path, Path) else rules_path
    if not rules_path.exists():
        return None

    import yaml
    with open(rules_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        return None

    rules: list[tuple[str, list[str]]] = []
    for domain, keywords in data.items():
        if isinstance(keywords, list):
            rules.append((str(domain), [str(k) for k in keywords]))
        elif isinstance(keywords, str):
            rules.append((str(domain), [keywords]))
    return rules if rules else None


def _get_domain_rules(config: "MemoryConfig | None" = None) -> list[tuple[str, list[str]]]:
    """Get domain rules, falling back to generic defaults.

    Returns a list of (domain, [keywords]) tuples.
    """
    custom = _load_domain_rules_from_config(config)
    if custom:
        return custom
    return _FALLBACK_DOMAIN_RULES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_memory_bloat(
    memory_path: str | Path | None = None,
    config: "MemoryConfig | None" = None,
    max_chars: int | None = None,
) -> dict:
    """Scan agent memory file for non-index (bloat) entries.

    Supports any agent's memory file. Auto-detects path and separator
    from config if not explicitly provided.

    Returns a report with:
      - total_entries: total number of entries in the file
      - index_entries: entries that look like proper L0 pointers
      - bloat_entries: entries that are too long or don't match L0 format
      - stats: character usage stats
      - suggestions: which domain each bloat entry should migrate to
      - warnings: capacity warnings when usage exceeds thresholds
    """
    path = _resolve_memory_path(memory_path, config)

    if not path.exists():
        return {
            "success": False,
            "error": f"Memory file not found: {path}",
            "path": str(path),
        }

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": str(e), "path": str(path)}

    separator = _resolve_separator(path, config)
    entries = _parse_entries(raw, separator=separator)

    index_entries = []
    bloat_entries = []

    total_chars = 0
    index_chars = 0
    bloat_chars = 0

    for entry in entries:
        entry_len = len(entry)
        total_chars += entry_len

        if _is_index_entry(entry, config):
            index_entries.append(entry)
            index_chars += entry_len
        else:
            bloat_entries.append(entry)
            bloat_chars += entry_len

    # Generate migration suggestions for each bloat entry
    domain_rules = _get_domain_rules(config)
    suggestions = []
    for entry in bloat_entries:
        suggestion = _suggest_migration(entry, domain_rules=domain_rules, config=config)
        suggestions.append({
            "entry_preview": entry[:80] + ("..." if len(entry) > 80 else ""),
            "entry_length": len(entry),
            "suggested_domain": suggestion["domain"],
            "suggested_section": suggestion["section"],
        })

    result = {
        "success": True,
        "path": str(path),
        "total_entries": len(entries),
        "index_entries": len(index_entries),
        "bloat_entries": len(bloat_entries),
        "stats": {
            "total_chars": total_chars,
            "index_chars": index_chars,
            "bloat_chars": bloat_chars,
            "bloat_percentage": round(bloat_chars / total_chars * 100, 1) if total_chars > 0 else 0,
        },
        "suggestions": suggestions,
    }

    # Capacity warning logic
    _capacity_limit = max_chars or _get_memory_max_chars(config=config, memory_path=path)
    if _capacity_limit and _capacity_limit > 0 and total_chars > 0:
        usage_ratio = total_chars / _capacity_limit
        capacity_threshold = _get_capacity_warning_threshold(config)
        if usage_ratio > capacity_threshold:
            usage_pct = round(usage_ratio * 100, 1)
            result["warnings"] = [
                {
                    "level": "critical" if usage_ratio >= 1.0 else "warning",
                    "type": "capacity",
                    "message": (
                        f"Memory usage at {usage_pct}% of capacity "
                        f"({total_chars}/{_capacity_limit} chars)."
                    ),
                    "hint": (
                        "Consider: (1) run compact_memory() to migrate bloat to L1, "
                        "(2) increase MEMORY_MAX_CHARS env var, "
                        "(3) adjust Hermes config to allow more memory."
                    ),
                    "usage_ratio": round(usage_ratio, 3),
                    "capacity_limit": _capacity_limit,
                }
            ]

    return result


def compact_memory(
    config: "MemoryConfig",
    memory_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Migrate bloat entries from agent memory to L1 knowledge files.

    Supports any agent's memory file. Auto-detects path and separator
    from config if not explicitly provided.

    For each non-index entry:
      1. Determine the best L1 domain/section
      2. Write the content to L1 via inject_knowledge
      3. Generate an L0 pointer for it

    Returns a report of migrated entries and the cleaned memory content.
    The agent should then write the cleaned content back to its memory.
    """
    path = _resolve_memory_path(memory_path, config)

    if not path or not path.exists():
        return {"success": False, "error": f"Memory file not found: {path or '(auto-detect failed)'}"}

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": str(e)}

    separator = _resolve_separator(path, config)
    entries = _parse_entries(raw, separator=separator)

    # Load domain rules from config
    domain_rules = _get_domain_rules(config)

    migrated = []
    kept = []
    errors = []

    for entry in entries:
        if _is_index_entry(entry, config):
            kept.append(entry)
            continue

        # This is a bloat entry — migrate to L1
        suggestion = _suggest_migration(entry, domain_rules=domain_rules, config=config)

        if dry_run:
            migrated.append({
                "entry_preview": entry[:80] + ("..." if len(entry) > 80 else ""),
                "domain": suggestion["domain"],
                "section": suggestion["section"],
                "l0_pointer": suggestion["l0_pointer"],
            })
            # In dry run, still generate the pointer
            kept.append(suggestion["l0_pointer"])
        else:
            # Actually write to L1
            try:
                result = _inject_knowledge(
                    config=config,
                    domain=suggestion["domain"],
                    section=suggestion["section"],
                    content=entry,
                    mode="append",
                )
                if result.get("success"):
                    migrated.append({
                        "entry_preview": entry[:80] + ("..." if len(entry) > 80 else ""),
                        "domain": suggestion["domain"],
                        "section": suggestion["section"],
                        "l0_pointer": suggestion["l0_pointer"],
                        "l1_action": result.get("action"),
                    })
                    kept.append(suggestion["l0_pointer"])
                else:
                    errors.append({
                        "entry_preview": entry[:80],
                        "error": result.get("error", "Unknown error"),
                    })
                    kept.append(entry)  # Keep the original on error
            except Exception as e:
                errors.append({
                    "entry_preview": entry[:80],
                    "error": str(e),
                })
                kept.append(entry)

    # Build cleaned memory content
    cleaned_content = f"\n{separator}\n".join(kept)
    if kept:
        cleaned_content += "\n"

    result = {
        "success": True,
        "dry_run": dry_run,
        "migrated_count": len(migrated),
        "kept_count": len(kept) - len(migrated),  # Original index entries kept
        "error_count": len(errors),
        "migrated": migrated,
        "errors": errors,
        "cleaned_memory": cleaned_content,
        "stats": {
            "before_entries": len(entries),
            "before_chars": len(raw),
            "after_entries": len(kept),
            "after_chars": len(cleaned_content),
        },
    }

    if not dry_run:
        # Backup the original agent memory file before overwriting (S2 safety)
        try:
            backup_path = path.with_suffix(path.suffix + ".bak")
            backup_path.write_text(raw, encoding="utf-8")
            logger.info("Backed up agent memory to %s", backup_path)
        except Exception as e:
            logger.warning("Failed to backup agent memory before compact: %s", e)

        # Write the cleaned memory back
        try:
            path.write_text(cleaned_content, encoding="utf-8")
            result["file_written"] = True
        except Exception as e:
            result["file_written"] = False
            result["write_error"] = str(e)
            result["hint"] = "cleaned_memory field contains the cleaned content; write it manually"

    return result


# ---------------------------------------------------------------------------
# v2.3.0: Auto-maintain — write-triggered self-maintenance
# ---------------------------------------------------------------------------

def _last_compact_marker(config: "MemoryConfig") -> Path:
    """Path to the timestamp marker recording the last auto-compact pass."""
    return Path(config.home) / ".last_auto_compact"


def _read_last_compact_time(config: "MemoryConfig") -> float:
    """Return the epoch seconds of the last auto-compact, or 0.0 if never."""
    marker = _last_compact_marker(config)
    try:
        if marker.exists():
            return float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return 0.0


def _write_last_compact_time(config: "MemoryConfig") -> None:
    """Record the current time as the last auto-compact pass."""
    import time
    try:
        _last_compact_marker(config).write_text(str(time.time()), encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to write last-compact marker: %s", e)


def _ensure_l0_pointer_in_memory(
    l0_pointer: str,
    config: "MemoryConfig",
    memory_path: Path | None = None,
) -> dict:
    """Dual-write completion: ensure the L0 pointer exists in agent memory.

    The framework introduced the L1↔agent-memory dual-write, so the framework
    owns keeping the two consistent — the agent should never have to manually
    sync pointers. This appends the pointer if missing, or replaces a stale
    pointer for the same domain/file.

    Returns a small report: {action: added|replaced|present|skipped, ...}.
    """
    path = memory_path or _resolve_memory_path(None, config)
    if not path:
        return {"action": "skipped", "reason": "memory path not resolvable"}

    # Extract the target L1 file from the pointer ("... → knowledge/<file>")
    target_file = None
    if "→" in l0_pointer:
        target_file = l0_pointer.rsplit("→", 1)[-1].strip()

    separator = _resolve_separator(path, config)

    try:
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as e:
        return {"action": "skipped", "reason": f"read failed: {e}"}

    entries = _parse_entries(raw, separator=separator) if raw.strip() else []

    # Already present verbatim → nothing to do
    if l0_pointer.strip() in (e.strip() for e in entries):
        return {"action": "present"}

    # Look for a stale pointer to the same L1 file → replace it
    new_entries = []
    replaced = False
    for e in entries:
        if (
            target_file
            and _is_index_entry(e, config)
            and target_file in e
        ):
            new_entries.append(l0_pointer)
            replaced = True
        else:
            new_entries.append(e)

    if not replaced:
        new_entries.append(l0_pointer)

    joined = f"\n{separator}\n".join(new_entries)
    if new_entries:
        joined += "\n"

    try:
        path.write_text(joined, encoding="utf-8")
    except OSError as e:
        return {"action": "skipped", "reason": f"write failed: {e}"}

    return {"action": "replaced" if replaced else "added", "pointer": l0_pointer}


def _remove_l0_pointer_from_memory(
    domain_or_file: str,
    config: "MemoryConfig",
    memory_path: Path | None = None,
) -> dict:
    """Remove any L0 index pointer(s) for a domain/L1 file from agent memory.

    The framework owns the L1↔agent-memory dual-write, so when an L1 file is
    deleted the framework must also reap the dangling pointer it once wrote —
    otherwise the deleted file leaves a stale "[L0] … → knowledge/<file>"
    entry that recall and the L0 index will keep surfacing.

    Matches an entry if it is an L0 index pointer (carries the [L0] tag) AND
    references the target file (``knowledge/<file>`` or the bare domain after
    the tag). Returns {action: removed|absent|skipped, removed: N}.
    """
    path = memory_path or _resolve_memory_path(None, config)
    if not path or not path.exists():
        return {"action": "skipped", "reason": "memory path not resolvable"}

    # Normalize: accept "infra", "infra.md", or "knowledge/infra.md"
    base = domain_or_file.strip()
    base = base.rsplit("/", 1)[-1]  # drop any knowledge/ prefix
    domain = base[:-3] if base.endswith(".md") else base
    target_file = f"knowledge/{domain}.md"

    separator = _resolve_separator(path, config)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"action": "skipped", "reason": f"read failed: {e}"}

    entries = _parse_entries(raw, separator=separator) if raw.strip() else []
    if not entries:
        return {"action": "absent", "removed": 0}

    kept, removed = [], 0
    for e in entries:
        if _is_index_entry(e, config) and (
            target_file in e or _pointer_domain_matches(e, domain, config)
        ):
            removed += 1
            continue
        kept.append(e)

    if removed == 0:
        return {"action": "absent", "removed": 0}

    joined = f"\n{separator}\n".join(kept)
    if kept:
        joined += "\n"
    try:
        path.write_text(joined, encoding="utf-8")
    except OSError as e:
        return {"action": "skipped", "reason": f"write failed: {e}"}

    return {"action": "removed", "removed": removed}


def _pointer_domain_matches(entry: str, domain: str, config=None) -> bool:
    """True if an L0 pointer's domain label equals ``domain``.

    Pointer shape: "[L0] <domain>: <summary> → knowledge/<file>.md". This
    extracts the <domain> label right after the tag and compares it, so we can
    reap pointers even if the "→ knowledge/<file>" tail was lost.
    """
    tag = getattr(config, "l0_tag", "[L0]") if config else "[L0]"
    body = entry.strip()
    if body.startswith(tag):
        body = body[len(tag):].strip()
    label = body.split(":", 1)[0].strip()
    return label == domain


def dedup_l1_file(filepath: "Path") -> dict:
    """De-duplicate a single L1 knowledge file in place.

    Removes exact-duplicate non-heading lines within each ## section while
    preserving structure (headings, order, first occurrence of each line).
    Size-independent and idempotent. Returns a small report.

    This is the framework's own guard against L1 append-bloat: even if a
    write path slips a verbatim duplicate through, this reaps it on the next
    maintenance pass — keeping the self-maintenance responsibility *inside*
    the framework rather than relying on external cron.
    """
    try:
        raw = filepath.read_text(encoding="utf-8")
    except OSError as e:
        return {"file": filepath.name, "action": "skipped", "reason": f"read failed: {e}"}

    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    seen: set[str] = set()
    out: list[str] = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        # Always keep blank lines, headings, and HTML comments (structure).
        if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
            out.append(line)
            continue
        key = stripped
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        out.append(line)

    if removed == 0:
        return {"file": filepath.name, "action": "clean", "removed_lines": 0}

    new_text = "\n".join(out)
    try:
        bak = filepath.with_suffix(filepath.suffix + ".bak")
        bak.write_text(raw, encoding="utf-8")
        filepath.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return {"file": filepath.name, "action": "error", "reason": str(e)}

    return {
        "file": filepath.name,
        "action": "deduped",
        "removed_lines": removed,
        "before_bytes": len(raw.encode("utf-8")),
        "after_bytes": len(new_text.encode("utf-8")),
    }


def dedup_l1_knowledge(config: "MemoryConfig", min_dup_lines: int = 20) -> dict:
    """Scan all L1 knowledge files and de-duplicate bloated ones.

    Only files whose duplicate-line count exceeds ``min_dup_lines`` are
    rewritten, so clean files are left untouched (cheap no-op). Also prunes
    the matching duplicate rows from vectors.db so the vector store stays in
    sync with the slimmed files.
    """
    report: dict = {"files_deduped": [], "vector_pruned": 0, "scanned": 0}
    try:
        kdirs = getattr(config, "knowledge_dirs", None) or [config.knowledge_dir]
        for kdir in kdirs:
            kdir = Path(kdir)
            if not kdir.exists():
                continue
            for fp in kdir.glob("*.md"):
                report["scanned"] += 1
                try:
                    raw = fp.read_text(encoding="utf-8")
                except OSError:
                    continue
                # Cheap pre-check: count duplicate non-heading lines.
                seen: set[str] = set()
                dup = 0
                for line in raw.split("\n"):
                    s = line.strip()
                    if not s or s.startswith("#") or s.startswith("<!--"):
                        continue
                    if s in seen:
                        dup += 1
                    else:
                        seen.add(s)
                if dup >= min_dup_lines:
                    res = dedup_l1_file(fp)
                    if res.get("action") == "deduped":
                        report["files_deduped"].append(res)
    except Exception as e:  # noqa: BLE001 — maintenance must not raise
        logger.warning("L1 dedup scan failed: %s", e)
        report["error"] = str(e)

    # Prune duplicate (domain, text) rows from the vector store.
    try:
        import sqlite3

        db_path = Path(config.home) / "data" / "vectors.db"
        if db_path.exists():
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                before = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
                conn.execute(
                    """
                    DELETE FROM vectors
                    WHERE rowid NOT IN (
                        SELECT MIN(rowid) FROM vectors GROUP BY domain, text
                    )
                    """
                )
                conn.commit()
                after = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
                report["vector_pruned"] = before - after
    except Exception as e:  # noqa: BLE001 — maintenance must not raise
        logger.warning("Vector store dedup failed: %s", e)
        report["vector_error"] = str(e)

    return report


def auto_maintain_after_write(
    config: "MemoryConfig",
    l0_pointer: str | None = None,
    memory_path: str | Path | None = None,
) -> dict:
    """Framework self-maintenance, invoked automatically after each L1 write.

    Two responsibilities the framework now owns (so the agent doesn't have to):

      1. **Dual-write completion** — ensure the just-generated L0 pointer is
         present in agent memory (added or stale-replaced). This closes the
         L1↔agent-memory consistency gap the layered architecture introduced.

      2. **Lazy compaction** — when agent memory exceeds the bloat threshold,
         or more than ``auto_maintain_interval_days`` have passed since the last
         pass, run ``compact_memory`` to migrate bloat to L1 and slim memory
         back to pointers.

    Designed to ride along on natural ``inject_knowledge`` calls (stdio-safe,
    no background thread required). Fails silently — maintenance must never
    break the primary write.

    Returns a report dict; ``{"skipped": True, ...}`` when auto-maintain is off.
    """
    if not getattr(config, "auto_maintain", True):
        return {"skipped": True, "reason": "auto_maintain disabled"}

    report: dict = {"dual_write": None, "compact": None}

    path = _resolve_memory_path(memory_path, config)
    if not path:
        return {"skipped": True, "reason": "memory path not resolvable"}

    # --- 1. Dual-write completion ---
    if l0_pointer:
        try:
            report["dual_write"] = _ensure_l0_pointer_in_memory(
                l0_pointer, config, memory_path=path
            )
        except Exception as e:  # noqa: BLE001 — maintenance must not raise
            logger.warning("Auto-maintain dual-write failed: %s", e)
            report["dual_write"] = {"action": "error", "error": str(e)}

    # --- 2. Lazy compaction decision ---
    try:
        import time

        bloat = detect_memory_bloat(memory_path=path, config=config)
        should_compact = False
        reason = None

        if bloat.get("success"):
            total_chars = bloat["stats"]["total_chars"]
            limit = _get_memory_max_chars(config=config, memory_path=path)
            threshold = getattr(config, "compact_bloat_threshold", 0.8)
            has_bloat = bloat.get("bloat_entries", 0) > 0
            usage_ratio = (total_chars / limit) if (limit and limit > 0) else 0.0

            # Trigger A: usage over bloat threshold (and there's bloat to move)
            if limit and limit > 0 and has_bloat:
                if usage_ratio >= threshold:
                    should_compact = True
                    reason = f"usage {round(usage_ratio * 100, 1)}% >= threshold {round(threshold * 100)}%"

            # Trigger B: interval elapsed (and there's bloat to move)
            if not should_compact and has_bloat:
                interval_s = getattr(config, "auto_maintain_interval_days", 7.0) * 86400
                elapsed = time.time() - _read_last_compact_time(config)
                if elapsed >= interval_s:
                    should_compact = True
                    reason = f"interval elapsed ({round(elapsed / 86400, 1)}d >= {interval_s / 86400}d)"

            # Trigger C: hard safety net — memory critically full, compact NOW
            # regardless of interval. Prevents the "silently over the real
            # limit" failure when the agent wrote bloat directly to native
            # memory (bypassing inject_knowledge) and the interval hasn't
            # elapsed. Fires when usage >= critical threshold (default 0.95).
            if not should_compact and has_bloat and limit and limit > 0:
                critical = getattr(config, "compact_critical_threshold", 0.95)
                if usage_ratio >= critical:
                    should_compact = True
                    reason = f"CRITICAL usage {round(usage_ratio * 100, 1)}% >= {round(critical * 100)}% (hard safety net)"

        if should_compact:
            compact_result = compact_memory(config, memory_path=path, dry_run=False)
            _write_last_compact_time(config)
            report["compact"] = {
                "triggered": True,
                "reason": reason,
                "migrated_count": compact_result.get("migrated_count", 0),
                "after_chars": compact_result.get("stats", {}).get("after_chars"),
            }
        else:
            report["compact"] = {"triggered": False}
    except Exception as e:  # noqa: BLE001 — maintenance must not raise
        logger.warning("Auto-maintain compaction failed: %s", e)
        report["compact"] = {"triggered": False, "error": str(e)}

    # --- 3. L1 / vector-store dedup guard (v2.8.0) ---
    # The framework now owns L1 de-bloat too, not just MEMORY.md compaction.
    # Runs on the same cadence as lazy compaction (interval-gated) so it's a
    # cheap no-op on clean stores but reaps any append-duplicates that slipped
    # through the write-path guards. Self-contained — never breaks the write.
    try:
        import time as _time

        interval_s = getattr(config, "auto_maintain_interval_days", 7.0) * 86400
        elapsed = _time.time() - _read_last_compact_time(config)
        # Trigger if the compaction step already fired, or the interval lapsed.
        if report.get("compact", {}).get("triggered") or elapsed >= interval_s:
            report["l1_dedup"] = dedup_l1_knowledge(config)
        else:
            report["l1_dedup"] = {"triggered": False}
    except Exception as e:  # noqa: BLE001 — maintenance must not raise
        logger.warning("Auto-maintain L1 dedup failed: %s", e)
        report["l1_dedup"] = {"triggered": False, "error": str(e)}

    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_entries(raw: str, separator: str = "§") -> list[str]:
    """Parse agent memory file into individual entries.

    Args:
        raw: The full text content of the memory file.
        separator: Entry separator. Hermes uses '§', most others use '\\n\\n'.
    """
    # Split by separator and clean up
    parts = raw.split(f"\n{separator}\n")
    entries = []
    for part in parts:
        cleaned = part.strip()
        if cleaned and cleaned != separator:
            entries.append(cleaned)
    return entries


def _is_index_entry(entry: str, config=None) -> bool:
    """Determine if an entry is a proper L0 index pointer.

    Valid L0 entries start with the configured L0 tag (default: [L0]).

    Length is NOT a disqualifier: an entry carrying the L0 prefix is an
    index pointer regardless of how long its summary is. Treating an
    over-long L0 pointer as "bloat" caused a self-feeding loop —
    compact_memory() migrated it back into the L1 file body, dual_write
    then regenerated a (now nested) L0 pointer, and each cycle appended
    another "[L0] …" layer. An over-long summary is a separate concern
    (see is_oversized_index_entry) and must never route the pointer into L1.
    """
    return bool(_get_l0_pattern(config).match(entry))


def is_oversized_index_entry(entry: str, config=None) -> bool:
    """True for an L0 pointer whose summary exceeds the recommended length.

    Diagnostic only — an oversized pointer is still an index entry and must
    stay in agent memory. Callers may surface this to suggest trimming the
    summary, but must NOT migrate the entry to L1 on its basis.
    """
    return (
        bool(_get_l0_pattern(config).match(entry))
        and len(entry) > MAX_INDEX_ENTRY_LENGTH
    )


def _suggest_migration(entry: str, domain_rules: list[tuple[str, list[str]]] | None = None, config=None) -> dict:
    """Suggest which L1 domain and section a bloat entry should migrate to.

    Uses keyword matching to determine the best domain.
    Falls back to 'misc' if no match found.

    Args:
        entry: The memory entry text.
        domain_rules: List of (domain, [keywords]) tuples. If None, uses
            generic fallback rules.
        config: MemoryConfig instance (for l0_tag).
    """
    if domain_rules is None:
        domain_rules = _FALLBACK_DOMAIN_RULES

    entry_lower = entry.lower()

    # Derive tag content (without brackets) for comparison
    tag = getattr(config, "l0_tag", None) or _DEFAULT_L0_TAG
    tag_inner = tag.strip("[]")

    # Special case: L0 tag entries already have a domain tag — use it directly
    # e.g. "[L0] infra: WSL代理…" → domain=infra
    l0_match = _get_l0_pattern(config).match(entry)
    if l0_match:
        rest = entry[l0_match.end():]
        domain_match = re.match(r"([\w\-]+):", rest)
        if domain_match:
            l0_domain = domain_match.group(1)
            known_domains = {r[0] for r in domain_rules}
            matched_domain = l0_domain  # trust it even if unusual
        else:
            matched_domain = None
    else:
        matched_domain = None

    # Only fall through to keyword matching if not already identified from L0 tag
    if matched_domain is None:
        for domain, keywords in domain_rules:
            for kw in keywords:
                if re.search(kw, entry, re.IGNORECASE):
                    matched_domain = domain
                    break
            if matched_domain:
                break

    if not matched_domain:
        matched_domain = "misc"

    # Generate clean section name from entry content
    # Skip L0-format tags entirely when extracting section
    tag_match = re.match(r"^\[([^\]]+)\]\s*", entry)
    if tag_match:
        raw_tag = tag_match.group(1)
        # Only use the tag as section if it's NOT the L0 tag
        if raw_tag.strip() == tag_inner:
            # Get content after L0 tag domain: part
            after_tag = entry[tag_match.end():]
            # Try to extract meaningful content after the "domain: " prefix
            content_part = re.sub(r"^[\w\-]+:\s*", "", after_tag).strip()
            section = content_part.split("→")[0].strip() if "→" in content_part else content_part[:40]
            section = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff\s\-]", "", section).strip()
        elif "·" in raw_tag:
            # Structured tags like [tag·subcategory]
            section = raw_tag.strip()
        else:
            section = raw_tag.strip()
    else:
        # Use first meaningful words
        first_line = entry.split("\n")[0].strip()
        section = first_line[:40].strip()

    # Clean section for markdown heading
    section = re.sub(r"[^\w\s\-·‧\u4e00-\u9fff]", "", section).strip()
    if not section:
        section = "migrated"

    # Generate L0 pointer — strip the L0 tag prefix from summary if present
    summary = _summarize_brief(entry, config=config)
    # Remove double L0 tag nesting
    tag_prefix = f"{tag_inner}:"
    if summary.startswith(tag_prefix):
        summary = summary[len(tag_prefix):].strip()
    if summary.startswith(tag + " "):
        summary = summary[len(tag) + 1:].strip()

    l0_pointer = f"{tag} {matched_domain}: {summary} → knowledge/{matched_domain}.md"

    return {
        "domain": matched_domain,
        "section": section,
        "l0_pointer": l0_pointer,
    }


def _summarize_brief(entry: str, max_chars: int = 60, config=None) -> str:
    """Create a very brief summary from an entry for the L0 pointer."""
    tag = getattr(config, "l0_tag", None) or _DEFAULT_L0_TAG
    # Strip leading L0 tag prefix — this is metadata, not content
    entry_clean = entry
    if entry_clean.startswith(tag + " "):
        entry_clean = entry_clean[len(tag) + 1:].strip()
        # Also strip the "domain: " part that follows
        entry_clean = re.sub(r"^[\w\-]+:\s*", "", entry_clean).strip()
        # Return content before "→ knowledge/" or truncated
        if "→ knowledge/" in entry_clean:
            entry_clean = entry_clean.split("→ knowledge/")[0].strip()
        truncated = entry_clean[:max_chars]
        if len(entry_clean) > max_chars:
            truncated = truncated[:max_chars - 3] + "..."
        return truncated

    # Extract tag if present (non-L0 tags like [tag])
    tag_match = re.match(r"^\[([^\]]+)\]\s*", entry_clean)
    if tag_match:
        tag = tag_match.group(1)
        remaining = entry_clean[tag_match.end():].strip()[:max_chars - len(tag) - 3]
        clean = re.sub(r"[*_`#\n]", " ", remaining).strip()
        if len(tag) + len(clean) + 3 > max_chars:
            clean = clean[:max_chars - len(tag) - 6] + "..."
        return f"{tag}: {clean}" if clean else tag

    # No tag — use first line
    first_line = entry_clean.split("\n")[0].strip()
    clean = re.sub(r"[*_`#]", "", first_line).strip()
    if len(clean) > max_chars:
        clean = clean[:max_chars - 3] + "..."
    return clean


def _find_hermes_config() -> Path | None:
    """Locate the Hermes config.yaml.

    Resolution order:
      1. HERMES_CONFIG_PATH env var (set by Hermes in the MCP server's env)
      2. ~/.hermes/config.yaml (standard location)

    Returns the path if it exists, else None.
    """
    import os

    explicit = os.environ.get("HERMES_CONFIG_PATH")
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p

    standard = Path("~/.hermes/config.yaml").expanduser()
    if standard.is_file():
        return standard
    return None


def _read_hermes_memory_limit(is_user_profile: bool = False) -> int | None:
    """Dynamically read the real memory char limit from Hermes config.yaml.

    Hermes stores two independent limits under the ``memory:`` section:
      - ``memory_char_limit``  → MEMORY.md
      - ``user_char_limit``    → USER.md

    Reading this keeps the framework in sync with whatever the user has
    actually configured (default 2000, user-adjustable) instead of guessing
    a fixed value that drifts from reality.

    Args:
        is_user_profile: When True, read ``user_char_limit`` (USER.md);
            otherwise ``memory_char_limit`` (MEMORY.md).

    Returns:
        The configured limit as int, or None if unavailable/unparseable.
    """
    cfg_path = _find_hermes_config()
    if not cfg_path:
        return None
    try:
        import yaml

        with cfg_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        mem = data.get("memory") or {}
        key = "user_char_limit" if is_user_profile else "memory_char_limit"
        val = mem.get(key)
        if val is not None:
            limit = int(val)
            if limit > 0:
                return limit
    except Exception as e:  # noqa: BLE001 — never let limit-detection break maintenance
        logger.debug("Could not read Hermes memory limit from %s: %s", cfg_path, e)
    return None


def _is_hermes_memory_path(path: "str | Path | None") -> bool:
    """Heuristic: does this memory path belong to a Hermes-style agent?

    Hermes memory lives under ``.hermes/memories/`` and uses '§' separators.
    Used to pick the Hermes-appropriate fallback default (2000) instead of
    the generic 50000.
    """
    if not path:
        return False
    s = str(path).lower()
    return "hermes" in s or "memory.md" in s or "user.md" in s


def _is_user_profile_path(path: "str | Path | None") -> bool:
    """Detect whether the memory path is the USER profile (USER.md) vs MEMORY.md."""
    if not path:
        return False
    return "user.md" in str(path).lower()


def _get_memory_max_chars(
    config=None,
    memory_path: "str | Path | None" = None,
) -> int:
    """Resolve the memory capacity limit, preferring real config over guesses.

    Priority chain (most authoritative first):
      1. config.memory_char_limit (explicit, if the MemoryConfig carries one)
      2. MEMORY_MAX_CHARS env var
      3. Hermes config.yaml memory.{memory,user}_char_limit — DYNAMIC, tracks
         whatever the user actually set (e.g. they raised 2000 → 4000)
      4. smart default: Hermes-style memory → 2000, generic → 50000

    Args:
        config: optional MemoryConfig (may carry an explicit limit).
        memory_path: the memory file being evaluated; used to (a) pick the
            right Hermes key (MEMORY vs USER) and (b) choose the fallback.
    """
    import os

    # 1. explicit config value
    if config is not None:
        cfg_val = getattr(config, "memory_char_limit", None)
        if cfg_val:
            try:
                v = int(cfg_val)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass

    # 2. env override
    val = os.environ.get("MEMORY_MAX_CHARS")
    if val:
        try:
            v = int(val)
            if v > 0:
                return v
        except ValueError:
            pass

    # 3. dynamic: read the real limit from Hermes config.yaml
    is_user = _is_user_profile_path(memory_path)
    hermes_limit = _read_hermes_memory_limit(is_user_profile=is_user)
    if hermes_limit:
        return hermes_limit

    # 4. smart default by memory-file type
    if _is_hermes_memory_path(memory_path):
        return _HERMES_DEFAULT_MEMORY_MAX_CHARS
    return _DEFAULT_MEMORY_MAX_CHARS


def _get_capacity_warning_threshold(config=None) -> float:
    """Get the capacity warning threshold from config or default."""
    if config is not None:
        threshold = getattr(config, "compact_capacity_warning_threshold", None)
        if threshold is not None:
            return float(threshold)
    return 0.9

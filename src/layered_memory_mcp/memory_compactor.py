"""
Memory Compactor — Detect and migrate non-index entries in agent memory.

Agent memory file formats vary by platform:
  - Hermes: ~/.hermes/memories/MEMORY.md, entries separated by '§'
  - Claude Code: ~/.claude/CLAUDE.md, entries separated by blank lines
  - Cursor: ./.cursorrules
  - Generic: any text file

The L0 index layer should only contain short pointer entries like:
    [L0索引] domain: summary → knowledge/file.md

This module detects "bloat" entries (detailed knowledge that should be in L1)
and provides tools to migrate them to proper L1 knowledge files.
"""

import logging
import re
from pathlib import Path

from .injector import inject_knowledge as _inject_knowledge

logger = logging.getLogger("layered_memory_mcp.compactor")

# Patterns for identifying L0 index entries
_L0_INDEX_PATTERN = re.compile(r"^\[L0索引\]\s*")
# Also consider structured tag entries as "acceptable" (e.g. [思维框架·强制])
_STRUCT_TAG_PATTERN = re.compile(r"^\[.+[·\-].+\]\s*")

# Max chars for a "valid" memory entry (index pointers are short)
# Anything longer is likely detailed knowledge that belongs in L1
MAX_INDEX_ENTRY_LENGTH = 120

# Default memory capacity in chars (for capacity warning).
# Users can override via MEMORY_MAX_CHARS env var.
_DEFAULT_MEMORY_MAX_CHARS = 50_000

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
    config=None,
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
    config=None,
) -> str:
    """Resolve the entry separator for the agent memory file.

    Priority:
      1. config.detect_agent_memory_separator() (auto-detect)
      2. Fallback to '\n\n' (blank-line separator — universal)
    """
    if config is not None:
        return config.detect_agent_memory_separator(memory_path)
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


def _get_domain_rules(config=None) -> list[tuple[str, list[str]]]:
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
    config=None,
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

        if _is_index_entry(entry):
            index_entries.append(entry)
            index_chars += entry_len
        else:
            bloat_entries.append(entry)
            bloat_chars += entry_len

    # Generate migration suggestions for each bloat entry
    domain_rules = _get_domain_rules(config)
    suggestions = []
    for entry in bloat_entries:
        suggestion = _suggest_migration(entry, domain_rules=domain_rules)
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
    _capacity_limit = max_chars or _get_memory_max_chars()
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
    config,
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
        if _is_index_entry(entry):
            kept.append(entry)
            continue

        # This is a bloat entry — migrate to L1
        suggestion = _suggest_migration(entry, domain_rules=domain_rules)

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
        # Write the cleaned memory back
        try:
            path.write_text(cleaned_content, encoding="utf-8")
            result["file_written"] = True
        except Exception as e:
            result["file_written"] = False
            result["write_error"] = str(e)
            result["hint"] = "cleaned_memory 字段包含了清理后的内容，请手动写入"

    return result


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


def _is_index_entry(entry: str) -> bool:
    """Determine if an entry is a proper L0 index pointer.

    Valid L0 entries:
      - Start with [L0索引]
      - Are reasonably short (under MAX_INDEX_ENTRY_LENGTH)
    """
    if _L0_INDEX_PATTERN.match(entry):
        # Even L0 entries can be bloated if too long
        return len(entry) <= MAX_INDEX_ENTRY_LENGTH
    return False


def _suggest_migration(entry: str, domain_rules: list[tuple[str, list[str]]] | None = None) -> dict:
    """Suggest which L1 domain and section a bloat entry should migrate to.

    Uses keyword matching to determine the best domain.
    Falls back to 'misc' if no match found.

    Args:
        entry: The memory entry text.
        domain_rules: List of (domain, [keywords]) tuples. If None, uses
            generic fallback rules.
    """
    if domain_rules is None:
        domain_rules = _FALLBACK_DOMAIN_RULES

    entry_lower = entry.lower()

    # Special case: [L0索引] entries already have a domain tag — use it directly
    # e.g. "[L0索引] infra: WSL代理…" → domain=infra
    l0_match = re.match(r"^\[L0索引\]\s*([\w\-]+):", entry)
    if l0_match:
        l0_domain = l0_match.group(1)
        # Validate it's a known domain
        known_domains = {r[0] for r in domain_rules}
        if l0_domain in known_domains:
            matched_domain = l0_domain
        else:
            matched_domain = l0_domain  # trust it anyway, but it's unusual
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
        # Only use the tag as section if it's NOT [L0索引]
        if raw_tag.strip() == "L0索引":
            # Get content after [L0索引] domain: part
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

    # Generate L0 pointer — strip the [L0索引] prefix from summary if present
    summary = _summarize_brief(entry)
    # Remove double [L0索引] nesting
    if summary.startswith("L0索引:"):
        summary = summary[len("L0索引:"):].strip()
    if summary.startswith("[L0索引]"):
        summary = summary[len("[L0索引]"):].strip()

    l0_pointer = f"[L0索引] {matched_domain}: {summary} → knowledge/{matched_domain}.md"

    return {
        "domain": matched_domain,
        "section": section,
        "l0_pointer": l0_pointer,
    }


def _summarize_brief(entry: str, max_chars: int = 60) -> str:
    """Create a very brief summary from an entry for the L0 pointer."""
    # Strip leading [L0索引] prefix — this is metadata, not content
    entry_clean = entry
    if entry_clean.startswith("[L0索引]"):
        entry_clean = entry_clean[len("[L0索引]"):].strip()
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


def _get_memory_max_chars() -> int:
    """Get the configured memory max chars limit."""
    import os
    val = os.environ.get("MEMORY_MAX_CHARS")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return _DEFAULT_MEMORY_MAX_CHARS


def _get_capacity_warning_threshold(config=None) -> float:
    """Get the capacity warning threshold from config or default."""
    if config is not None:
        threshold = getattr(config, "compact_capacity_warning_threshold", None)
        if threshold is not None:
            return float(threshold)
    return 0.9

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
    from .agent_memory_guard import WriteTarget
    from .config import MemoryConfig

logger = logging.getLogger("layered_memory_mcp.compactor")

# Default L0 index tag (overridden by config.l0_tag at runtime)
_DEFAULT_L0_TAG = "[L0]"
_L0_INDEX_PATTERN = re.compile(r"^\[L0\]\s*")

# --- v3.2.1 heading hygiene --------------------------------------------------
# Max length of a derived section heading, in characters.
MAX_SECTION_TITLE_CHARS = 40

# Characters that genuinely break a markdown ATX heading or the section parser.
# Everything else — ':' '/' '.' '（）' '，' '、' '×' etc. — is CONTENT and must
# survive. The pre-3.2.1 code used an allow-list regex that deleted every one
# of those, turning "/var/cache/documents/" into
# "varcachedocuments" and welding CJK clauses into one unbroken run
# (the P2 "garbled_heading" pathology the rot auditor kept reporting).
_HEADING_STRIP_RE = re.compile(r"[#*`_\[\]\r\n]+")

# Boundary characters preferred when truncating a long heading, so we cut at a
# clause edge instead of mid-word/mid-path.
_TITLE_BOUNDARY_CHARS = "，,。.；;：:、（(【[ \t"


def _clean_heading(text: str) -> str:
    """Normalise a string for use as a markdown ## heading — losslessly.

    Only strips markdown structural characters and newlines; all punctuation,
    path separators and CJK marks are preserved. Returns "" when nothing
    usable remains (caller decides what to do — never invent a title).
    """
    if not text:
        return ""
    cleaned = _HEADING_STRIP_RE.sub(" ", text)
    # Collapse runs of whitespace introduced by the strip.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _truncate_on_boundary(text: str, max_chars: int = MAX_SECTION_TITLE_CHARS) -> str:
    """Truncate to max_chars, preferring a clause boundary over a hard cut."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    cut = max(window.rfind(ch) for ch in _TITLE_BOUNDARY_CHARS)
    # Only honour the boundary if it keeps a reasonable amount of the title.
    if cut >= max_chars // 2:
        return window[:cut].strip()
    return window.strip()


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


def _resolve_write_target(
    memory_path: str | Path | None = None,
    config: "MemoryConfig | None" = None,
    *,
    operation: str = "agent_memory_write",
) -> "WriteTarget":
    """Resolve the agent memory path for a **write**, honouring the guard.

    Same inputs as :func:`_resolve_memory_path`, but an auto-detected path
    outside this install's own home is refused instead of written (see
    :mod:`.agent_memory_guard`). Every projection write in this module goes
    through here, so the rule is defined once.
    """
    from .agent_memory_guard import resolve_write_target

    return resolve_write_target(config, memory_path, operation=operation)


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

def _dict_to_rules(dk: dict[str, list[str]] | None) -> list[tuple[str, list[str]]]:
    """Convert a ``{domain: [keyword, ...]}`` mapping to ``(domain, keywords)``.

    Pure transformation — the single point that reshapes the unified data
    source (``config.domain_keywords``) into the tuple form the compaction
    engine consumes. Non-dict input (or ``None``) yields an empty list. Values
    are coerced to lists of strings so callers see a consistent shape.
    """
    if not isinstance(dk, dict):
        return []
    rules: list[tuple[str, list[str]]] = []
    for domain, keywords in dk.items():
        if isinstance(keywords, list):
            rules.append((str(domain), [str(k) for k in keywords]))
        elif isinstance(keywords, str):
            rules.append((str(domain), [keywords]))
    return rules


def _get_domain_rules(config: "MemoryConfig | None" = None) -> list[tuple[str, list[str]]]:
    """Derive domain rules solely from ``config.domain_keywords``.

    This is the single unified data source shared with the auto-extractor —
    the framework ships **no** built-in preset table. When no user table is
    configured the result is empty, and compaction simply makes no migration
    suggestion (every unmatched entry stays "misc"), mirroring the extractor's
    "everything is general" behaviour.

    Returns a list of (domain, [keywords]) tuples.
    """
    dk = getattr(config, "domain_keywords", None) if config is not None else None
    return _dict_to_rules(dk)


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
            "needs_title": suggestion.get("needs_title", False),
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


def _entry_routing(config: "MemoryConfig", entry: str, section: str | None):
    """Classify one memory entry. Never raises; ``None`` when unavailable.

    "Not an L0 pointer" is not the same as "durable L1 knowledge", but this
    migration historically treated the two as identical: every non-index entry
    went to L1. That is a long-term pollution source — a runbook, a standing
    user preference or a one-off trace in the agent's memory file all ended up
    as L1 knowledge. The classification is what lets ``compact_memory`` report
    that (advisory mode) or refuse it (enforce mode).

    Returns the :class:`~layered_memory_mcp.routing.RoutingDecision`, or ``None``
    when routing is disabled in config or the router could not be consulted —
    the caller then falls back to the legacy "everything goes to L1" behaviour.
    """
    try:
        from .routing import classify_memory_item, routing_settings

        enabled, _mode = routing_settings(config)
        if not enabled:
            return None
        return classify_memory_item(
            entry, config=config, section=section, domain=None
        )
    except Exception as e:  # noqa: BLE001 — classification must not break compaction
        logger.warning("routing classification failed during compact (non-critical): %s", e)
        return None


# --- routing-aware compaction ------------------------------------------------
#
# Surface names are compared as plain strings so the compaction loop never has
# to import the router's enum (and never breaks when the router is
# unavailable). Authoritative definition: ``routing.MemorySurface``.

#: The only surface compaction may inject into an L1 knowledge file.
_L1_SURFACE = "l1_knowledge"

#: Non-L1 surfaces, grouped by the result bucket they are reported under.
_TODO_SURFACE = "todo"
_AGENT_OWNED_SURFACES = ("skill", "user_profile", "agent_other")


def _candidate_bucket(surface: str | None) -> str:
    """Which result bucket a non-L1 surface is reported under.

    ``l0_index`` and ``memory_changelog`` land in ``rejected`` on purpose: both
    are framework-owned *derived* surfaces, and user content must never be
    written into them by a compaction pass.
    """
    if surface == _TODO_SURFACE:
        return "todo_candidates"
    if surface in _AGENT_OWNED_SURFACES:
        return "agent_owned_candidates"
    return "rejected"


def _preview(entry: str, max_chars: int = 80) -> str:
    return entry[:max_chars] + ("..." if len(entry) > max_chars else "")


def _routing_fields(decision) -> dict | None:
    """Per-entry routing block: the advisory summary plus any agent action."""
    if decision is None:
        return None
    block = decision.advisory_summary()
    action = getattr(decision, "suggested_action", None)
    block["suggested_action"] = action.model_dump(mode="json") if action else None
    return block


def _resolve_routing_mode(
    config: "MemoryConfig", routing_mode: str | None
) -> tuple[bool, str, list[str]]:
    """``(routing_enabled, effective_mode, notes)`` for this compaction pass.

    Precedence: explicit ``routing_mode`` argument → ``config.routing_mode`` →
    ``advisory``. An unknown value is ignored rather than fatal, and ``enforce``
    degrades to ``advisory`` when routing is switched off in config — enforcing
    on classifications we are not allowed to compute would block every entry.
    """
    notes: list[str] = []
    try:
        from .routing import routing_settings

        enabled, mode = routing_settings(config)
    except Exception as e:  # noqa: BLE001 — compaction predates routing
        logger.warning("routing unavailable during compact (non-critical): %s", e)
        return False, "advisory", [
            "router unavailable — legacy advisory compaction (every non-index "
            "entry is treated as L1 knowledge)"
        ]

    if routing_mode is not None:
        candidate = str(routing_mode).strip().lower()
        if candidate in ("advisory", "enforce"):
            mode = candidate
        else:
            notes.append(
                f"ignored unknown routing_mode {routing_mode!r}; using {mode!r}"
            )
    if not enabled and mode == "enforce":
        mode = "advisory"
        notes.append(
            "routing.enabled=false, so 'enforce' was ignored: with no "
            "classification available every entry would be blocked"
        )
    return enabled, mode, notes


def compact_memory(
    config: "MemoryConfig",
    memory_path: str | Path | None = None,
    dry_run: bool = False,
    routing_mode: str | None = None,
) -> dict:
    """Migrate bloat entries from agent memory to L1 knowledge files.

    Supports any agent's memory file. Auto-detects path and separator
    from config if not explicitly provided.

    For each non-index entry:
      1. Determine the best L1 domain/section
      2. Classify it with the persistence router (see :func:`_entry_routing`)
      3. Write the content to L1 via inject_knowledge — in ``enforce`` mode
         only when step 2 called it durable L1 knowledge
      4. Generate an L0 pointer for it

    Step 2 exists because "not an L0 pointer" is a statement about the *source*,
    not a classification of the content. Two modes:

    ``advisory`` (default, backwards compatible)
        Every non-index entry still migrates to L1, exactly as before. The
        report gains ``routing`` per entry, ``routing_advisory`` for the entries
        the router would have sent elsewhere, and ``routing_summary`` counts.

    ``enforce``
        Only ``l1_knowledge`` entries are injected. TODO / skill / profile /
        agent-other / undecided / ephemeral entries are **left in agent memory
        untouched** and returned under ``todo_candidates`` /
        ``agent_owned_candidates`` / ``rejected`` so the calling agent can place
        them. Nothing outside L1 is written: no TODO is created (compaction has
        no TODO write contract) and no agent-owned store is touched (that needs
        an explicit ``agent_extensions`` adapter, invoked by the caller).

    Args:
        config: the memory configuration.
        memory_path: agent memory file; auto-detected from ``config`` if absent.
        dry_run: report only; no L1 write and no memory file rewrite.
        routing_mode: per-call override of ``config.routing_mode``
            (``"advisory"`` / ``"enforce"``). ``None`` uses the config value.

    Returns a report of migrated entries and the cleaned memory content.
    The agent should then write the cleaned content back to its memory.
    """
    path = _resolve_memory_path(memory_path, config)

    if not path or not path.exists():
        return {"success": False, "error": f"Memory file not found: {path or '(auto-detect failed)'}"}

    # A real pass rewrites the agent's memory file (and backs it up next to it),
    # so it needs the same permission as any other projection write. A dry run
    # only reports, and reporting on an auto-detected file stays allowed.
    if not dry_run:
        target = _resolve_write_target(memory_path, config, operation="compact_memory")
        if not target.allowed:
            return {
                "success": False,
                "dry_run": False,
                "error": (
                    "refusing to rewrite an auto-detected agent memory file: "
                    f"{target.reason}"
                ),
                "agent_memory_write": target.as_report("refused"),
                "hint": (
                    "Pass memory_path explicitly, or configure agent_memory.path "
                    "/ agent_memory.allow_auto_detect_write. "
                    "compact_memory(dry_run=True) still reports on this file."
                ),
            }

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": str(e)}

    separator = _resolve_separator(path, config)
    entries = _parse_entries(raw, separator=separator)

    # Load domain rules from config
    domain_rules = _get_domain_rules(config)

    routing_enabled, mode, routing_notes = _resolve_routing_mode(config, routing_mode)
    enforce = mode == "enforce"

    migrated = []
    kept = []
    errors = []
    routing_advisory = []
    buckets: dict[str, list[dict]] = {
        "l1_candidates": [],
        "todo_candidates": [],
        "agent_owned_candidates": [],
        "rejected": [],
    }
    by_surface: dict[str, int] = {}
    by_ownership: dict[str, int] = {}

    def _record(
        entry: str,
        suggestion: dict,
        routing: dict | None,
        *,
        bucket: str,
        status: str,
        reason: str = "",
        destination: str | None = None,
    ) -> dict:
        """Append one auditable candidate record to ``bucket`` and return it."""
        record = {
            "entry_preview": _preview(entry),
            "entry_length": len(entry),
            "surface": routing["surface"] if routing else None,
            "ownership": routing["ownership"] if routing else None,
            "score": routing["score"] if routing else None,
            "routing_reason": routing["reason"] if routing else "",
            "warnings": list(routing["warnings"]) if routing else [],
            "suggested_action": routing.get("suggested_action") if routing else None,
            "status": status,
            "reason": reason,
            "destination": destination,
            "suggested_domain": suggestion["domain"],
            "suggested_section": suggestion["section"] or None,
        }
        buckets[bucket].append(record)
        return record

    for entry in entries:
        if _is_index_entry(entry, config):
            kept.append(entry)
            continue

        # This is a bloat entry — candidate for migration to L1
        suggestion = _suggest_migration(entry, domain_rules=domain_rules, config=config)

        decision = _entry_routing(config, entry, suggestion.get("section"))
        routing = _routing_fields(decision)
        surface = routing["surface"] if routing else None
        if routing:
            by_surface[surface] = by_surface.get(surface, 0) + 1
            owner = routing["ownership"]
            by_ownership[owner] = by_ownership.get(owner, 0) + 1

        # --- enforce: L1 takes only what the router called L1 knowledge ----
        # The source entry is deliberately left in agent memory: a blocked
        # entry has nowhere else to go yet, and dropping it would be data loss.
        if enforce and surface != _L1_SURFACE:
            reason = (
                routing["reason"]
                if routing
                else (
                    "routing classification unavailable — enforce mode will not "
                    "inject an unclassified entry into L1"
                )
            )
            _record(
                entry,
                suggestion,
                routing,
                bucket=_candidate_bucket(surface),
                status="blocked_from_l1",
                reason=reason,
            )
            routing_advisory.append({
                "entry_preview": _preview(entry),
                "suggested_domain": suggestion["domain"],
                "routed_surface": surface,
                "ownership": routing["ownership"] if routing else None,
                "score": routing["score"] if routing else None,
                "reason": reason,
                "status": "blocked_from_l1",
                "note": (
                    "NOT migrated — routing mode is 'enforce', so only "
                    "l1_knowledge reaches L1. The entry was left in agent "
                    "memory; place it on its own surface and re-run"
                ),
            })
            kept.append(entry)
            continue

        # v3.2.1 (Bug 2): the framework could not derive a trustworthy heading
        # for this entry. Previously it fabricated one from the body's first 40
        # chars, which is exactly how stub headings accumulated next to real
        # ones. Refuse the write, keep the entry in place, and report it so the
        # calling agent (an LLM) can name it and re-run.
        if suggestion.get("needs_title"):
            errors.append({
                "entry_preview": entry[:80],
                "error": (
                    "needs_title: cannot derive a section heading for this entry "
                    "(no L0 tag). Re-inject it explicitly with an agent-chosen "
                    "section name; refusing to fabricate a heading from the body."
                ),
                "needs_title": True,
                "suggested_domain": suggestion["domain"],
                "routing": routing,
            })
            _record(
                entry,
                suggestion,
                routing,
                bucket="l1_candidates",
                status="refused_needs_title",
                reason="cannot derive a section heading; naming is the agent's job",
            )
            kept.append(entry)  # leave the original untouched
            continue

        if routing and surface != _L1_SURFACE:
            routing_advisory.append({
                "entry_preview": _preview(entry),
                "suggested_domain": suggestion["domain"],
                "routed_surface": surface,
                "ownership": routing["ownership"],
                "score": routing["score"],
                "reason": routing["reason"],
                "status": "migrated",
                "note": (
                    "migrated to L1 anyway — this pass is advisory; set routing "
                    "mode 'enforce' (or pass routing_mode='enforce') to keep "
                    "non-L1 surfaces out of L1"
                ),
            })

        destination = f"knowledge/{suggestion['domain']}.md#{suggestion['section']}"

        if dry_run:
            migrated.append({
                "entry_preview": _preview(entry),
                "domain": suggestion["domain"],
                "section": suggestion["section"],
                "l0_pointer": suggestion["l0_pointer"],
                "routing": routing,
            })
            _record(
                entry,
                suggestion,
                routing,
                bucket="l1_candidates",
                status="planned",
                destination=destination,
            )
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
                        "entry_preview": _preview(entry),
                        "domain": suggestion["domain"],
                        "section": suggestion["section"],
                        "l0_pointer": suggestion["l0_pointer"],
                        "l1_action": result.get("action"),
                        "routing": routing,
                    })
                    _record(
                        entry,
                        suggestion,
                        routing,
                        bucket="l1_candidates",
                        status="migrated",
                        destination=destination,
                    )
                    kept.append(suggestion["l0_pointer"])
                else:
                    error = result.get("error", "Unknown error")
                    errors.append({
                        "entry_preview": entry[:80],
                        "error": error,
                    })
                    _record(
                        entry,
                        suggestion,
                        routing,
                        bucket="l1_candidates",
                        status="error",
                        reason=error,
                    )
                    kept.append(entry)  # Keep the original on error
            except Exception as e:
                errors.append({
                    "entry_preview": entry[:80],
                    "error": str(e),
                })
                _record(
                    entry,
                    suggestion,
                    routing,
                    bucket="l1_candidates",
                    status="error",
                    reason=str(e),
                )
                kept.append(entry)

    # Build cleaned memory content
    cleaned_content = f"\n{separator}\n".join(kept)
    if kept:
        cleaned_content += "\n"

    blocked = sum(
        1
        for records in buckets.values()
        for r in records
        if r["status"] == "blocked_from_l1"
    )
    if enforce and blocked:
        routing_notes.append(
            f"{blocked} entr{'y' if blocked == 1 else 'ies'} were blocked from L1 "
            "and left in agent memory. Nothing was written outside L1: TODO "
            "candidates are reported, not created, and agent-owned surfaces "
            "(skill / user_profile / agent_other) stay with the agent unless an "
            "agent_extensions adapter is configured and invoked explicitly."
        )

    result = {
        "success": True,
        "dry_run": dry_run,
        "migrated_count": len(migrated),
        # Entries left in agent memory that are not freshly generated pointers:
        # original index entries, plus anything refused or (in enforce mode)
        # blocked from L1.
        "kept_count": len(kept) - len(migrated),
        "error_count": len(errors),
        "migrated": migrated,
        "errors": errors,
        # Entries the router would NOT have sent to L1. In advisory mode they
        # were migrated anyway; in enforce mode they were blocked.
        "routing_advisory": routing_advisory,
        "routing_mode": mode,
        "routing_enabled": routing_enabled,
        "routing_summary": {
            "mode": mode,
            "enabled": routing_enabled,
            "classified": sum(by_surface.values()),
            "by_surface": by_surface,
            "by_ownership": by_ownership,
            # Written to L1 in this pass (always 0 on a dry run).
            "l1_injected": sum(
                1 for r in buckets["l1_candidates"] if r["status"] == "migrated"
            ),
            # Accepted for L1, including a dry run's plan.
            "l1_planned": sum(
                1
                for r in buckets["l1_candidates"]
                if r["status"] in ("migrated", "planned")
            ),
            "blocked_from_l1": blocked,
        },
        "routing_notes": routing_notes,
        **buckets,
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

    v3.2.0: honours ``config.memory_mode`` —
      - "off"        → skip dual-write entirely;
      - "index_only" → delegate to ``_ensure_index_entry_in_memory`` (single
        index entry, per-domain pointers reaped);
      - "pointers"   → legacy per-domain pointer upsert (below).

    Returns a small report: {action: added|replaced|present|skipped, ...}.
    Refused by the safety guard → ``{action: skipped, reason:
    "unsafe_auto_detect_write", ...}`` (see :mod:`.agent_memory_guard`).
    """
    mode = getattr(config, "memory_mode", "pointers")
    if mode == "off":
        return {"action": "skipped", "reason": "memory_mode=off"}
    if mode == "index_only":
        return _ensure_index_entry_in_memory(config, memory_path=memory_path)

    target = _resolve_write_target(memory_path, config, operation="l0_pointer_dual_write")
    if not target.allowed:
        return {"action": "skipped", **target.skip_fields()}
    path = target.path

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

# v3.2.0 — index_only mode -------------------------------------------------

def _knowledge_index_marker(config=None) -> str:
    """Marker prefix identifying the single knowledge-index entry.

    Any entry starting with this prefix IS the index entry (at most one is
    kept). Tag-aware so custom ``l0_tag`` configs stay consistent.
    """
    tag = getattr(config, "l0_tag", "[L0]") if config is not None else "[L0]"
    return f"{tag} knowledge-index:"


def _knowledge_index_entry_text(config: "MemoryConfig") -> str:
    """Build the single index entry for index_only mode.

    Points at the authoritative L0 index + retrieval tools, and states the
    write rule (facts go to L1 via inject_knowledge, never into this file).
    Domain count is refreshed on every write so the entry never goes stale.
    """
    try:
        count = len(list(Path(config.knowledge_dir).glob("*.md")))
    except Exception:  # noqa: BLE001 — count is cosmetic, never break the write
        count = 0
    return (
        f"{_knowledge_index_marker(config)} {count} domain(s) — call "
        f"get_l0_index() for the map, recall_knowledge() for content; store "
        f"facts via inject_knowledge (L1), not in this file."
    )


def _ensure_index_entry_in_memory(
    config: "MemoryConfig",
    memory_path: Path | None = None,
) -> dict:
    """Index-only dual-write: keep exactly ONE knowledge-index entry.

    - Upserts the single index entry (refreshes the domain count).
    - REAPS (deletes, never migrates) per-domain [L0] pointer copies — they
      are redundant with L0.md and are the historical bloat source. They
      must NOT be routed into L1 (that was the v2.9.2 recursive-pollution
      trap: pointers are index artifacts, not knowledge).
    - Leaves non-index entries untouched; bloat detection + lazy compaction
      route those to L1 through the normal compact_memory path.

    Returns {action: added|upserted|skipped, index_entry, pointers_removed}.
    """
    target = _resolve_write_target(memory_path, config, operation="index_entry_dual_write")
    if not target.allowed:
        return {"action": "skipped", **target.skip_fields()}
    path = target.path

    index_text = _knowledge_index_entry_text(config)
    separator = _resolve_separator(path, config)
    marker = _knowledge_index_marker(config)

    try:
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as e:
        return {"action": "skipped", "reason": f"read failed: {e}"}

    entries = _parse_entries(raw, separator=separator) if raw.strip() else []

    kept: list[str] = []
    pointers_removed = 0
    index_written = False
    for e in entries:
        if e.strip().startswith(marker):
            if not index_written:
                kept.append(index_text)  # upsert with fresh count
                index_written = True
            # drop any duplicate index lines silently
        elif _is_index_entry(e, config):
            pointers_removed += 1  # reap per-domain pointer copy
        else:
            kept.append(e)

    if not index_written:
        kept.insert(0, index_text)

    joined = f"\n{separator}\n".join(kept)
    if kept:
        joined += "\n"

    try:
        path.write_text(joined, encoding="utf-8")
    except OSError as e:
        return {"action": "skipped", "reason": f"write failed: {e}"}

    return {
        "action": "upserted" if index_written else "added",
        "index_entry": index_text,
        "pointers_removed": pointers_removed,
    }


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

    Reaping is still a write, so it is guarded like the other projections: a
    deletion we were not asked to make is as unwelcome as an insertion.
    """
    target = _resolve_write_target(memory_path, config, operation="l0_pointer_reap")
    if not target.allowed:
        return {"action": "skipped", **target.skip_fields()}
    path = target.path
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


def dedup_l1_file(filepath: "Path", config: "MemoryConfig | None" = None) -> dict:
    """De-duplicate a single L1 knowledge file in place.

    Removes exact-duplicate non-heading lines within each ## section while
    preserving structure (headings, order, first occurrence of each line).
    Size-independent and idempotent. Returns a small report.

    This is the framework's own guard against L1 append-bloat: even if a
    write path slips a verbatim duplicate through, this reaps it on the next
    maintenance pass — keeping the self-maintenance responsibility *inside*
    the framework rather than relying on external cron.

    With ``config`` the pre-dedup backup goes under
    ``<home>/data/backups/knowledge`` (v3.4.7); without it, the legacy
    adjacent ``.bak`` is kept for direct callers.
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
        if config is not None:
            from .backups import backup_knowledge_file

            backup = backup_knowledge_file(config, filepath, raw)
            if "error" in backup:
                # No backup, no destructive rewrite.
                return {"file": filepath.name, "action": "error",
                        "reason": f"backup failed: {backup['error']}"}
        else:
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
                    res = dedup_l1_file(fp, config)
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
    domain: str | None = None,
    filepath: str | Path | None = None,
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

    # --- 0. Promotion detection (v2.10.0) — independent of agent-memory path ---
    # When the just-written domain is a watched catch-all (default "misc"),
    # check whether a same-topic cluster has accumulated that deserves its own
    # L1 file. Runs FIRST and unconditionally on the file (not gated behind the
    # agent-memory-path resolution below), because promotion is a pure L1-file
    # concern — it must still surface even when there's no resolvable MEMORY.md.
    # Only attaches a `promotion` key when there IS a candidate — a None result
    # leaves the report untouched (no noise on normal writes). detect() is fully
    # try/except-wrapped internally, so this never breaks the primary write.
    if domain and filepath:
        try:
            from .promotion import detect_promotion_candidate

            candidate = detect_promotion_candidate(config, domain, Path(filepath))
            if candidate is not None:
                report["promotion"] = candidate
        except Exception as e:  # noqa: BLE001 — maintenance must not raise
            logger.warning("Auto-maintain promotion detection failed: %s", e)

    # Everything below this point writes the agent's memory file (pointer
    # dual-write, then possibly a full compaction rewrite), so the guard decides
    # once, here, whether this config may touch it at all. Refusals are soft:
    # the primary L1 write that triggered this ride-along has already succeeded
    # and must not be failed by a projection we are not allowed to make.
    target = _resolve_write_target(memory_path, config, operation="auto_maintain")
    if not target.allowed:
        report["skipped"] = True
        report["reason"] = target.reason or "memory path not resolvable"
        report["agent_memory_write"] = target.as_report()
        return report
    path = target.path

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
                # Under routing mode 'enforce' non-L1 entries stay in agent
                # memory, so a non-zero count here explains why memory did not
                # shrink as much as migrated_count suggests.
                "blocked_from_l1": compact_result.get("routing_summary", {}).get(
                    "blocked_from_l1", 0
                ),
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
        domain_rules: List of (domain, [keywords]) tuples. If None, no keyword
            table is applied — entries only classify via an explicit L0 tag,
            otherwise fall back to "misc".
        config: MemoryConfig instance (for l0_tag).
    """
    if domain_rules is None:
        domain_rules = []

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
            section = (
                content_part.split("→")[0].strip()
                if "→" in content_part
                else _truncate_on_boundary(content_part, MAX_SECTION_TITLE_CHARS)
            )
            section = _clean_heading(section)
        elif "·" in raw_tag:
            # Structured tags like [tag·subcategory]
            section = raw_tag.strip()
        else:
            section = raw_tag.strip()
    else:
        # v3.2.1 (Bug 2): NO first-line-as-title fallback. Slicing the body's
        # first 40 chars fabricated a near-miss heading every time the same
        # knowledge came back reworded, producing the "stub heading sitting
        # next to the real one" pathology. The zero-LLM core must not guess a
        # title: signal that naming is required and let the calling agent name
        # it. Callers surface `needs_title` instead of writing a stub.
        section = ""

    # Clean section for markdown heading (punctuation/paths preserved — see
    # _clean_heading; the old whitelist regex here ate ':' '/' '.' and CJK
    # punctuation, which is what mangled paths into "roothermescachedocuments").
    section = _clean_heading(section)
    needs_title = not section
    if needs_title:
        section = ""

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
        "needs_title": needs_title,
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

"""MEMORY_CHANGELOG — provenance of semantic L0/L1 memory changes.

WHAT IT IS
----------
The changelog **of the memory itself**: an append-only record of semantic
mutations to the framework-owned durable surfaces (L0 index, L1 knowledge), so
that "why does my knowledge base say this now?" has an answer.

WHAT IT IS NOT
--------------
- NOT the repository release ``CHANGELOG.md`` (that documents the software).
- NOT raw session logs (those are source-owned evidence).
- NOT a generic system/event log.
- NOT TODO lifecycle history.

WHAT GETS RECORDED
------------------
Only semantic durable-memory mutations — see :data:`RECORDABLE_OPERATIONS`:
an L1 domain/section created, updated, merged, replaced or deleted; a
fusion/consolidation write-back; an L0 entry added, removed or renamed when the
semantic map actually changed.

WHAT DOES NOT GET RECORDED
--------------------------
See :data:`NOT_RECORDED`: vector reindex / search-index rebuilds (derived data,
semantic content unchanged), raw session contents, agent-owned
Skill/Profile/AgentOther recommendations, routing decisions that mutated
nothing, and TODO lifecycle events — unless such an event directly causes an
L0/L1 change, in which case the L0/L1 change is what gets recorded.

A write that *reported* success without changing semantic content records
nothing either: a no-op section merge, an identical whole-file overwrite, an L0
resync whose entry set did not move. Provenance is not an I/O log.

WHAT AN EVENT CARRIES — AND WHAT IT MUST NOT
--------------------------------------------
Structure, not content: operation, surface, domain, section heading, the tool
that caused it, a structural summary, and ``before_hash`` / ``after_hash`` of
the affected body. **Never the body itself.** The knowledge files are the
source of truth for content and are already on disk; copying bodies (or raw
session text) into a provenance log would duplicate every secret a user ever
wrote into their notes, in a file nobody thinks of as sensitive. ``metadata``
is for structured, non-sensitive details (byte counts, write action, match
kind) and its string values are clipped (:data:`MAX_METADATA_TEXT`) as a
backstop, not as a licence to put content there.

STORAGE
-------
Append-only JSONL at ``<home>/data/memory_changelog.jsonl`` (override with
``memory_changelog.path``), one event per line — see
:class:`~.storage.memory_changelog_store.MemoryChangelogStore`. Recording is
best-effort and silent on failure: provenance must never be able to fail the
write it describes. Retention is **manual**: nothing in this module deletes
history, and ``retention_days`` is reported but not enforced.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .routing import MemorySurface

logger = logging.getLogger("layered_memory_mcp.memory_changelog")

#: Bumped from 1 when the record gained its persistent backend: an event now
#: also carries ``event_id``, ``tool``, ``summary``, ``source_ref`` and
#: ``before_hash`` / ``after_hash``. No v1 record was ever persisted (there was
#: no store), so there is nothing to migrate.
SCHEMA_VERSION = 2

#: Semantic mutations of a framework-owned durable surface.
RECORDABLE_OPERATIONS: tuple[str, ...] = (
    "l1_section_created",
    "l1_section_updated",
    "l1_section_merged",
    "l1_section_replaced",
    "l1_section_deleted",
    "l1_domain_created",
    "l1_domain_updated",
    "l1_domain_deleted",
    "l1_fusion_writeback",
    "l1_consolidation_writeback",
    "l0_entry_added",
    "l0_entry_removed",
    "l0_entry_renamed",
)

#: Events that look loggable but carry no semantic durable-memory change.
NOT_RECORDED: tuple[str, ...] = (
    "vector_reindex",
    "search_index_rebuild",
    "l0_index_resync_no_semantic_change",
    "raw_session_scan",
    "routing_decision_without_write",
    "agent_extension_recommendation",
    "todo_created",
    "todo_status_changed",
    "todo_deleted",
)

ChangelogSurface = Literal["l0_index", "l1_knowledge"]

#: Clip bounds. Framework-generated text, kept short so no event can grow into
#: a content dump.
MAX_SUMMARY_TEXT = 300
MAX_REASON_TEXT = 500
MAX_METADATA_TEXT = 200
MAX_METADATA_KEYS = 24


def is_recordable(operation: str) -> bool:
    """Whether ``operation`` belongs in MEMORY_CHANGELOG.

    Unknown operations are **not** recordable: a changelog that accepts anything
    is the generic event log this surface is explicitly not.
    """
    return str(operation) in RECORDABLE_OPERATIONS


def hash_text(text: str | None) -> str:
    """Stable short content hash used for ``before_hash`` / ``after_hash``.

    Normalizes line endings and strips surrounding whitespace so cosmetic churn
    does not read as a semantic change. Deliberately the same normalisation as
    the deferred-fusion optimistic lock (``injector._hash_body`` delegates
    here), which makes a fusion write-back's ``before_hash`` *be* the
    ``expected_hash`` the handshake was issued under.
    """
    norm = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _safe_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep metadata structural: scalars and short strings only.

    Drops nested containers and callables rather than serialising them — the
    one way content leaks into a provenance record is a caller handing over a
    whole result dict, and a nested blob is exactly what that looks like.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if len(out) >= MAX_METADATA_KEYS:
            break
        if value is None or isinstance(value, (bool, int, float)):
            out[str(key)] = value
        elif isinstance(value, str):
            out[str(key)] = _clip(value, MAX_METADATA_TEXT)
    return out


class MemoryChangelogEntry(BaseModel):
    """One provenance record for a semantic L0/L1 mutation."""

    schema_version: int = SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    surface: ChangelogSurface
    operation: str
    domain: str
    section: str | None = None
    actor: str = "framework"
    agent_id: str | None = None
    #: The entry point that caused the mutation (``inject_knowledge``,
    #: ``sync_l0_index``, …), so an event can be traced back to a call.
    tool: str | None = None
    #: Structural one-liner ("replaced '## WSL 代理' in infra, 412 bytes").
    summary: str = ""
    reason: str = ""
    #: An identifier of the evidence behind the write (e.g. a session id) —
    #: a *reference*, never the raw body it points at.
    source_ref: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def build_entry(
    operation: str,
    domain: str,
    *,
    surface: MemorySurface | ChangelogSurface = MemorySurface.L1_KNOWLEDGE,
    section: str | None = None,
    actor: str = "framework",
    agent_id: str | None = None,
    tool: str | None = None,
    summary: str = "",
    reason: str = "",
    source_ref: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> MemoryChangelogEntry | None:
    """Build a changelog record, or ``None`` when ``operation`` is not recordable.

    Returning ``None`` (rather than raising) keeps call sites honest *and*
    harmless: provenance must never be able to fail a write.
    """
    if not is_recordable(operation):
        return None
    value = getattr(surface, "value", surface)
    if value not in ("l0_index", "l1_knowledge"):
        return None
    return MemoryChangelogEntry(
        surface=value,  # type: ignore[arg-type]
        operation=str(operation),
        domain=domain,
        section=section,
        actor=actor,
        agent_id=agent_id,
        tool=tool,
        summary=_clip(summary, MAX_SUMMARY_TEXT),
        reason=_clip(reason, MAX_REASON_TEXT),
        source_ref=source_ref,
        before_hash=before_hash,
        after_hash=after_hash,
        metadata=_safe_metadata(metadata),
    )


# ---------------------------------------------------------------------------
# Backend: configuration, store, record / read
# ---------------------------------------------------------------------------

#: Where the log lives under the framework home when nothing is configured.
DEFAULT_RELATIVE_PATH = ("data", "memory_changelog.jsonl")


def default_changelog_path(home: Path) -> Path:
    return Path(home).joinpath(*DEFAULT_RELATIVE_PATH)


def changelog_settings(config: Any | None = None) -> dict[str, Any]:
    """``{enabled, path, retention_days, retention_enforced}``. Never raises.

    ``retention_days`` is read and reported but **not applied**: pruning a
    provenance log is a destructive operation and this version does none of it,
    so the key exists to be configured and honoured later rather than to imply
    a sweep that silently drops history.
    """
    enabled = bool(getattr(config, "memory_changelog_enabled", True))
    path = getattr(config, "memory_changelog_path", None)
    if path is None:
        home = getattr(config, "home", None)
        path = default_changelog_path(home) if home is not None else None
    return {
        "enabled": enabled,
        "path": Path(path) if path is not None else None,
        "retention_days": getattr(config, "memory_changelog_retention_days", None),
        "retention_enforced": False,
    }


def open_store(config: Any | None = None):
    """The changelog store, or ``None`` when disabled / unresolvable."""
    try:
        settings = changelog_settings(config)
        if not settings["enabled"] or settings["path"] is None:
            return None
        from .storage.memory_changelog_store import MemoryChangelogStore

        return MemoryChangelogStore(settings["path"])
    except Exception as exc:  # noqa: BLE001 — provenance never breaks a caller
        logger.debug("memory_changelog store unavailable: %s", exc)
        return None


def record(
    config: Any | None,
    operation: str,
    domain: str,
    *,
    surface: MemorySurface | ChangelogSurface = MemorySurface.L1_KNOWLEDGE,
    section: str | None = None,
    actor: str = "framework",
    agent_id: str | None = None,
    tool: str | None = None,
    summary: str = "",
    reason: str = "",
    source_ref: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append one provenance event. Returns the event, or ``None``.

    ``None`` means "nothing was recorded" — the operation is not recordable,
    the backend is disabled, or the append failed. Never raises: every call
    site is a successful memory write that must not be undone by its own
    bookkeeping.
    """
    try:
        entry = build_entry(
            operation,
            domain,
            surface=surface,
            section=section,
            actor=actor,
            agent_id=agent_id,
            tool=tool,
            summary=summary,
            reason=reason,
            source_ref=source_ref,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata=metadata,
        )
        if entry is None:
            logger.debug("memory_changelog: %r is not a recordable operation", operation)
            return None
        store = open_store(config)
        if store is None:
            return None
        payload = entry.to_dict()
        return payload if store.append(payload) else None
    except Exception as exc:  # noqa: BLE001 — provenance never breaks a write
        logger.warning("memory_changelog record failed (non-critical): %s", exc)
        return None


def read_events(
    config: Any | None = None,
    *,
    domain: str | None = None,
    operation: str | None = None,
    surface: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    newest_first: bool = True,
) -> dict[str, Any]:
    """Bounded, filtered read of the changelog. Newest-first by default.

    Carries the store's ``read_truncated`` / ``read_error`` flags: a log above
    the store's read ceiling comes back with no entries, and the flags are what
    keep that distinguishable from a log with nothing in it.
    """
    from .storage.memory_changelog_store import DEFAULT_READ_LIMIT

    settings = changelog_settings(config)
    store = open_store(config)
    if store is None:
        return {
            "enabled": settings["enabled"],
            "path": str(settings["path"]) if settings["path"] else None,
            "exists": False,
            "read_truncated": False,
            "read_error": None,
            "total_events": 0,
            "matched": 0,
            "returned": 0,
            "entries": [],
            "note": (
                "memory_changelog is disabled (memory_changelog.enabled: false) — "
                "nothing is being recorded"
                if not settings["enabled"]
                else "memory_changelog path could not be resolved"
            ),
        }
    report = store.query(
        domain=domain,
        operation=operation,
        surface=surface,
        since=since,
        limit=DEFAULT_READ_LIMIT if limit is None else limit,
        newest_first=newest_first,
    )
    report["enabled"] = True
    return report


def changelog_stats(config: Any | None = None) -> dict[str, Any]:
    """Counts and time span of the changelog, plus the resolved settings."""
    settings = changelog_settings(config)
    store = open_store(config)
    base = {
        "enabled": settings["enabled"],
        "path": str(settings["path"]) if settings["path"] else None,
        "retention_days": settings["retention_days"],
        "retention_enforced": settings["retention_enforced"],
        "retention_note": (
            "append-only: this version never prunes the changelog. "
            "retention_days is recorded for a future sweep; removing history "
            "is a manual operation."
        ),
        "recordable_operations": list(RECORDABLE_OPERATIONS),
        "not_recorded": list(NOT_RECORDED),
    }
    if store is None:
        base.update(
            {
                "exists": False,
                "read_truncated": False,
                "read_error": None,
                "total_events": 0,
            }
        )
        return base
    base.update(store.stats())
    return base


__all__ = [
    "ChangelogSurface",
    "DEFAULT_RELATIVE_PATH",
    "MemoryChangelogEntry",
    "NOT_RECORDED",
    "RECORDABLE_OPERATIONS",
    "SCHEMA_VERSION",
    "build_entry",
    "changelog_settings",
    "changelog_stats",
    "default_changelog_path",
    "hash_text",
    "is_recordable",
    "open_store",
    "read_events",
    "record",
]

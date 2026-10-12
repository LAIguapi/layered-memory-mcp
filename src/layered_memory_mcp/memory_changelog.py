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

STATUS IN THIS VERSION
----------------------
Schema and inclusion rules only. There is **no persistent changelog store yet**:
:func:`is_recordable` lets callers and tests agree on what *would* be recorded,
and :func:`build_entry` produces the record shape, but nothing writes it
anywhere. The capability reflection (``get_memory_surface_capabilities``) states
this explicitly rather than implying a backend that does not exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .routing import MemorySurface

#: Semantic mutations of a framework-owned durable surface.
RECORDABLE_OPERATIONS: tuple[str, ...] = (
    "l1_section_created",
    "l1_section_updated",
    "l1_section_merged",
    "l1_section_replaced",
    "l1_section_deleted",
    "l1_domain_created",
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


def is_recordable(operation: str) -> bool:
    """Whether ``operation`` belongs in MEMORY_CHANGELOG.

    Unknown operations are **not** recordable: a changelog that accepts anything
    is the generic event log this surface is explicitly not.
    """
    return str(operation) in RECORDABLE_OPERATIONS


class MemoryChangelogEntry(BaseModel):
    """One provenance record for a semantic L0/L1 mutation."""

    schema_version: int = 1
    surface: ChangelogSurface
    operation: str
    domain: str
    section: str | None = None
    actor: str = "framework"
    agent_id: str | None = None
    reason: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
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
    reason: str = "",
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
        reason=reason,
        metadata=metadata or {},
    )


__all__ = [
    "ChangelogSurface",
    "MemoryChangelogEntry",
    "NOT_RECORDED",
    "RECORDABLE_OPERATIONS",
    "build_entry",
    "is_recordable",
]

"""Which mutating entrances a graph write mode supports, and what it says instead.

WHAT THIS MODULE IS
-------------------
The single table behind one rule from ARCHITECTURE_v2.md's framework contract:

    "Graph-write-enabled entry points must validate and commit through this
    authority before projection side effects. An entrance not supporting a
    semantic operation must refuse it before mutation, naming the supported
    alternative."

In ``graph_write``/``graph`` modes the journal is the authority and L1, the
vector rows and L0 are *its* projections. An entrance that writes one of those
directly would publish state no decision produced, and the next replay would
silently erase it — so it refuses instead, before any effect, and names what to
call. An entrance with no journal-backed semantics at all (a TODO, an
agent-owned Skill file) refuses for the other reason: there is no honest way to
represent it as a semantic decision yet, and inventing one would be worse.

Every other mode is untouched. ``legacy``, ``shadow`` and ``graph_recall`` keep
their existing behaviour exactly, which is what keeps the rollout reversible.

WHAT IT IS NOT
--------------
- NOT a permission system. It answers one question — "may this entrance mutate
  while the authority owns the write path?" — from the configured mode alone.
- NOT a silent downgrade. A refusal is a response, never a no-op, and never a
  partially applied call.
- NOT a reader's concern. Read-only and dry-run operations are not in the table
  and are never refused; the whole point of a dry run is that it writes nothing.

PRIVACY (§15)
-------------
A refusal carries the operation name, the configured mode, a bounded reason
token and a fixed instruction string. No path, no content, no configuration.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AUTHORITY_ENTRANCES",
    "GRAPH_MAINTENANCE_SUSPENDED",
    "GRAPH_MUTATION_REFUSED",
    "MAINTENANCE_JOBS",
    "REASON_EXTERNAL_OWNERSHIP",
    "REASON_MANAGED_PROJECTION",
    "REASON_NO_SEMANTIC_MAPPING",
    "REFUSED_OPERATIONS",
    "graph_mutation_refusal",
    "maintenance_job_suspension",
    "refusal_payload",
    "supported_alternative",
]

#: The ``error`` code every refusal carries, so a caller can branch on one
#: value rather than on prose.
GRAPH_MUTATION_REFUSED = "graph_mode_mutation_refused"

#: The surface is a managed projection of the journal. Writing it directly
#: would publish state no decision produced.
REASON_MANAGED_PROJECTION = "managed_projection"

#: The operation has no journal-backed semantic representation yet. Refused
#: rather than mapped onto an approximation.
REASON_NO_SEMANTIC_MAPPING = "no_semantic_mapping"

#: The surface belongs to somebody else (an agent-owned Skill, a user profile,
#: an externally managed file). Ownership does not move because a mode changed.
REASON_EXTERNAL_OWNERSHIP = "external_ownership"

#: The entrances that *do* route through the authority.
AUTHORITY_ENTRANCES: tuple[str, ...] = (
    "inject_knowledge_tool",
    "batch_inject_knowledge_tool",
)

_INJECT = (
    "inject_knowledge_tool(domain=…, section=…, content=…, "
    "semantic_write_request=…) — the one entrance that commits through the "
    "semantic journal and republishes L1, the vector rows and L0 from it"
)
_SUPERSEDE = (
    "inject_knowledge_tool(…, semantic_write_request=…) declaring a SUPERSEDES "
    "decision for the node that owns the section; the replaced material is "
    "excluded from L1 and the vector rows while its history is retained"
)
_RECOVER = (
    "recover_graph_projections() — the managed projections are republished "
    "from the semantic journal, never rebuilt from the files they produced"
)
_DRY_RUN = "the same call with dry_run=True, which inspects and writes nothing"
_READ_ONLY = "the read-only form of this call"

#: ``operation -> (reason, supported alternative)``. One table, consulted by
#: every transport, so MCP and HTTP cannot drift on what is allowed.
REFUSED_OPERATIONS: dict[str, tuple[str, str]] = {
    # --- MCP: whole-file L1 CRUD -------------------------------------------
    "create_knowledge_file": (REASON_MANAGED_PROJECTION, _INJECT),
    "update_knowledge_file": (REASON_MANAGED_PROJECTION, _INJECT),
    "delete_knowledge_file": (REASON_MANAGED_PROJECTION, _SUPERSEDE),
    # --- MCP: review lifecycle ---------------------------------------------
    "approve_knowledge": (REASON_NO_SEMANTIC_MAPPING, _INJECT),
    "reject_knowledge": (REASON_NO_SEMANTIC_MAPPING, _SUPERSEDE),
    # --- MCP: derived index/vector maintenance ------------------------------
    "reindex_vector_store_tool": (REASON_MANAGED_PROJECTION, _RECOVER),
    "sync_l0_index_tool": (REASON_MANAGED_PROJECTION, _RECOVER),
    "manage_l0_entry_tool": (REASON_MANAGED_PROJECTION, _RECOVER),
    "apply_safe_knowledge_cleanup_tool": (REASON_MANAGED_PROJECTION, _DRY_RUN),
    "reconcile_knowledge_tool": (REASON_MANAGED_PROJECTION, _READ_ONLY),
    # --- MCP: agent-memory and agent-owned surfaces -------------------------
    "compact_memory": (REASON_NO_SEMANTIC_MAPPING, _DRY_RUN),
    "apply_agent_action": (REASON_EXTERNAL_OWNERSHIP, _DRY_RUN),
    "init_framework": (REASON_EXTERNAL_OWNERSHIP, _READ_ONLY),
    "integrate_agent": (REASON_EXTERNAL_OWNERSHIP, "integrate_agent(action='status')"),
    # --- MCP: tasks ---------------------------------------------------------
    "add_todo": (REASON_NO_SEMANTIC_MAPPING, _INJECT),
    "update_todo": (REASON_NO_SEMANTIC_MAPPING, _INJECT),
    "delete_todo": (REASON_NO_SEMANTIC_MAPPING, _SUPERSEDE),
    # --- REST ---------------------------------------------------------------
    "rest_create_knowledge": (REASON_MANAGED_PROJECTION, _INJECT),
    "rest_update_knowledge": (REASON_MANAGED_PROJECTION, _INJECT),
    "rest_delete_knowledge": (REASON_MANAGED_PROJECTION, _SUPERSEDE),
    "rest_approve_review": (REASON_NO_SEMANTIC_MAPPING, _INJECT),
    "rest_reject_review": (REASON_NO_SEMANTIC_MAPPING, _SUPERSEDE),
    # --- Dashboard plugin ---------------------------------------------------
    "dashboard_inject_knowledge": (REASON_MANAGED_PROJECTION, _INJECT),
    "dashboard_approve_knowledge": (REASON_NO_SEMANTIC_MAPPING, _INJECT),
    "dashboard_reject_knowledge": (REASON_NO_SEMANTIC_MAPPING, _SUPERSEDE),
    "dashboard_compact_memory": (REASON_NO_SEMANTIC_MAPPING, _DRY_RUN),
    "dashboard_update_todo": (REASON_NO_SEMANTIC_MAPPING, _INJECT),
    "dashboard_rebuild_vectors": (REASON_MANAGED_PROJECTION, _RECOVER),
}


#: The ``status`` token a suspended background job reports. A suspension is a
#: *reported* non-run, never a silent one: the job is named, so a tick that did
#: nothing cannot be read as a tick that succeeded.
GRAPH_MAINTENANCE_SUSPENDED = "graph_mode_maintenance_suspended"

#: ``job -> the guarded entrance it is the unattended twin of``.
#:
#: Each of these is the same mutation as a refused tool, only with nobody
#: asking for it: the ride-along on ``get_l0_index``, the HTTP maintenance tick
#: and the watcher's L0 regeneration all rewrite a surface the authority owns
#: (or, for the guard payload, a surface somebody else owns). Refusing the tool
#: while a timer performs it unattended would be the loudest possible hole, so
#: both read the one table and cannot drift apart.
#:
#: Not listed, and deliberately: the authority's own ``publish_l0`` /
#: ``publish_vector`` projections. Those *are* the journal republishing itself,
#: which is precisely what the refusals point callers at.
MAINTENANCE_JOBS: dict[str, str] = {
    # memory_compactor.auto_maintain_after_write — agent-memory compaction and
    # L1 line-level dedup, i.e. compact_memory without a caller.
    "auto_maintain": "compact_memory",
    # maintenance.maintain_vectors — rebuilds the vector rows from the L1 files
    # they were projected onto, i.e. reindex_vector_store_tool without a caller.
    "vector_maintenance": "reindex_vector_store_tool",
    # write_guard.refresh_deployed_plugin — writes into the agent's own home.
    "write_guard_refresh": "init_framework",
    # watcher._sync_l0 — republishes L0 from whatever is on disk, i.e.
    # sync_l0_index_tool without a caller. The watcher's cache invalidation is
    # not a publication and is not covered here.
    "watcher_l0_sync": "sync_l0_index_tool",
}


def supported_alternative(operation: str) -> str:
    """What to call instead of ``operation``. Refuses an unknown name.

    An unknown operation raises rather than returning ``None``: a typo in a
    guard call site would otherwise silently become an unguarded entrance,
    which is the exact hole this table exists to close.
    """
    try:
        return REFUSED_OPERATIONS[operation][1]
    except KeyError:
        raise ValueError(
            f"{operation!r} is not a guarded mutating entrance; add it to "
            "REFUSED_OPERATIONS or route it through the semantic authority."
        ) from None


def refusal_payload(operation: str, graph_mode: str) -> dict[str, Any]:
    """The bounded refusal body, identical across every transport."""
    reason, alternative = REFUSED_OPERATIONS[operation]
    return {
        "success": False,
        "error": GRAPH_MUTATION_REFUSED,
        "operation": operation,
        "graph_mode": graph_mode,
        "reason": reason,
        "supported_alternative": alternative,
    }


def graph_mutation_refusal(config: Any, operation: str) -> dict[str, Any] | None:
    """The refusal for ``operation`` under ``config``, or ``None`` to proceed.

    ``None`` in every non-write mode, so ``legacy``, ``shadow`` and
    ``graph_recall`` behave exactly as they always did.
    """
    supported_alternative(operation)  # refuse an unguarded name, loudly
    if not bool(getattr(config, "graph_write_enabled", False)):
        return None
    return refusal_payload(operation, str(getattr(config, "graph_mode", "")))


def maintenance_job_suspension(config: Any, job: str) -> dict[str, Any] | None:
    """The suspension report for background ``job``, or ``None`` to run it.

    The background counterpart of :func:`graph_mutation_refusal`, reading the
    same table through :data:`MAINTENANCE_JOBS` — so a job is suspended in
    exactly the modes in which its caller-facing twin is refused, and ``None``
    in ``legacy``, ``shadow`` and ``graph_recall``.

    An unknown job name raises, for the same reason an unknown operation does:
    a typo at a call site must not read as "nothing to suspend here".

    The report is a *decision*, not an outcome. Callers must skip the work on
    it; returning it after having run the job anyway, or in place of a failure
    the job actually hit, would be a false claim.
    """
    try:
        operation = MAINTENANCE_JOBS[job]
    except KeyError:
        raise ValueError(
            f"{job!r} is not a known background maintenance job; add it to "
            "MAINTENANCE_JOBS naming the guarded entrance it performs."
        ) from None
    if not bool(getattr(config, "graph_write_enabled", False)):
        return None
    reason, alternative = REFUSED_OPERATIONS[operation]
    return {
        "status": GRAPH_MAINTENANCE_SUSPENDED,
        "job": job,
        "operation": operation,
        "graph_mode": str(getattr(config, "graph_mode", "")),
        "reason": reason,
        "supported_alternative": alternative,
    }

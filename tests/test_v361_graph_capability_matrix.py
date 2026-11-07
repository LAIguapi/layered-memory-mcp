"""Every refusal says which kind of refusal it is, and the table stays honest.

WHY THIS FILE EXISTS
--------------------
A safety refusal and a complete feature are not the same claim, and the easiest
way to lose that distinction is a refusal that names an "alternative" which
cannot actually do the job. ``add_todo`` once pointed at
``inject_knowledge_tool``: a task is not a claim, so following that advice does
not create the task — it writes an unrelated piece of knowledge.

The core-closure batch then took the next step for tasks: instead of a better
refusal, a journal-backed task decision. So the task entrances are no longer in
the guard table at all, and this file pins *that* too — a name may be refused
with an equivalent, refused as a gap, or routed through the authority, and it
must be exactly one of the three.

So the guard table is split into two kinds of refusal, and this file pins the
split:

* **redirected** — the operation *is* available under the graph default, by a
  different, named call. Whole-file L1 CRUD redirects to the semantic write;
  the derived index/vector rebuilds redirect to ``recover_graph_projections``;
  the agent-owned surfaces redirect to their read-only form.
* **missing** — there is no journal-backed equivalent at all. The store is
  intact and readable, the mutation is unavailable, and the refusal says so
  rather than describing a call that would not help.

The second list is the honest statement of what the rollout default costs. It
is pinned here so adding a journal-backed task or review path has to move an
entry *out* of it deliberately, and so nothing can quietly drift *into* it.
"""

from __future__ import annotations

import pytest

from layered_memory_mcp.graph_mutation_guard import (
    AUTHORITY_ENTRANCES,
    MAINTENANCE_JOBS,
    REASON_EXTERNAL_OWNERSHIP,
    REASON_MANAGED_PROJECTION,
    REASON_NO_SEMANTIC_MAPPING,
    REFUSED_OPERATIONS,
    _AUXILIARY,
    graph_mutation_refusal,
    supported_alternative,
)

#: Refused, but the capability exists under another named call.
REDIRECTED = {
    "create_knowledge_file",
    "update_knowledge_file",
    "delete_knowledge_file",
    "reindex_vector_store_tool",
    "sync_l0_index_tool",
    "manage_l0_entry_tool",
    "apply_safe_knowledge_cleanup_tool",
    "reconcile_knowledge_tool",
    "compact_memory",
    "apply_agent_action",
    "init_framework",
    "integrate_agent",
    "rest_create_knowledge",
    "rest_update_knowledge",
    "rest_delete_knowledge",
    "dashboard_inject_knowledge",
    "dashboard_compact_memory",
    "dashboard_rebuild_vectors",
}

#: Refused, and *not* available by any other call under the graph default.
#: This is a capability gap, documented as one.
#:
#: The task and review entrances used to be here. They are now in
#: :data:`~layered_memory_mcp.graph_mutation_guard.AUTHORITY_ENTRANCES`, which
#: is the only way out of this list: implement the capability. What remains is
#: the transport-specific review surface, which genuinely has no semantic form
#: yet.
MISSING = {
    "rest_approve_review",
    "rest_reject_review",
    "dashboard_approve_knowledge",
    "dashboard_reject_knowledge",
} - set(AUTHORITY_ENTRANCES)


class _Config:
    """The one attribute the guard reads, with nothing else to lean on."""

    def __init__(self, graph_mode: str, write_enabled: bool):
        self.graph_mode = graph_mode
        self.graph_write_enabled = write_enabled


def test_every_guarded_entrance_is_classified_exactly_once():
    """A new entrance has to be placed in one list or the other, on purpose."""
    assert REDIRECTED | MISSING == set(REFUSED_OPERATIONS)
    assert REDIRECTED & MISSING == set()
    assert not (set(AUTHORITY_ENTRANCES) & set(REFUSED_OPERATIONS))


@pytest.mark.parametrize("operation", sorted(MISSING))
def test_a_missing_capability_is_never_described_as_a_redirection(operation):
    reason, alternative = REFUSED_OPERATIONS[operation]
    assert reason == REASON_NO_SEMANTIC_MAPPING
    assert alternative == _AUXILIARY
    # The exact failure this file exists to prevent: an unavailable operation
    # advertising a call that does something else entirely.
    assert "inject_knowledge_tool" not in alternative
    assert "no equivalent" in alternative
    assert "not implemented" in alternative


def test_a_missing_capability_does_not_advertise_the_legacy_mode_as_a_fix():
    """A downgrade is not a remedy for a gap, and must not be offered as one.

    Telling an operator to run the entrance under ``graph_mode='legacy'`` would
    be telling them to move back to the authority this whole rollout exists to
    move off — and in a live service it would mean the migrated knowledge and
    the legacy stores answer to two different authorities. The refusal names the
    gap and stops.
    """
    assert "legacy" in _AUXILIARY, (
        "the refusal should still say that moving back is not the answer"
    )
    assert "not available by moving the service back to a legacy mode" in _AUXILIARY
    assert "graph_mode='legacy'" not in _AUXILIARY
    assert "run the entrance under" not in _AUXILIARY


def test_the_task_entrances_are_not_refused_at_all_any_more():
    """Tasks left the table by being implemented, not by being re-worded."""
    for entrance in ("add_todo", "update_todo", "delete_todo", "dashboard_update_todo"):
        assert entrance not in REFUSED_OPERATIONS, entrance
        assert entrance in AUTHORITY_ENTRANCES, entrance


@pytest.mark.parametrize("operation", sorted(REDIRECTED))
def test_a_redirection_names_a_call_that_can_actually_do_the_job(operation):
    reason, alternative = REFUSED_OPERATIONS[operation]
    assert reason in {
        REASON_MANAGED_PROJECTION,
        REASON_NO_SEMANTIC_MAPPING,
        REASON_EXTERNAL_OWNERSHIP,
    }
    assert alternative != _AUXILIARY
    assert any(
        token in alternative
        for token in (
            "inject_knowledge_tool",
            "recover_graph_projections",
            "dry_run=True",
            "read-only",
            "integrate_agent(action='status')",
        )
    ), alternative


@pytest.mark.parametrize("operation", sorted(REFUSED_OPERATIONS))
def test_every_refusal_payload_carries_its_kind_under_a_write_mode(operation):
    refusal = graph_mutation_refusal(_Config("graph", True), operation)
    assert refusal["success"] is False
    assert refusal["operation"] == operation
    assert refusal["graph_mode"] == "graph"
    assert refusal["supported_alternative"] == supported_alternative(operation)
    assert (refusal["supported_alternative"] == _AUXILIARY) is (operation in MISSING)


@pytest.mark.parametrize("operation", sorted(REFUSED_OPERATIONS))
def test_no_entrance_is_refused_when_the_write_path_is_not_the_authority(operation):
    assert graph_mutation_refusal(_Config("legacy", False), operation) is None


def test_the_authority_entrances_are_not_in_the_refusal_table():
    """The two entrances that commit are not 'refused with an alternative'."""
    for entrance in AUTHORITY_ENTRANCES:
        assert entrance not in REFUSED_OPERATIONS


def test_every_suspended_background_job_maps_to_a_redirected_entrance():
    """An unattended job must never be the unattended twin of a *gap*.

    If it were, a timer would be the only way to perform something no caller
    can ask for — which is the inverse of the hole the suspension table exists
    to close.
    """
    for job, operation in MAINTENANCE_JOBS.items():
        assert operation in REDIRECTED, job

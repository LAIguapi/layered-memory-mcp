"""v3.6.1 core closure — the four pre-cutover blockers, through real entrances.

WHY THIS FILE EXISTS
--------------------
An independent architecture review against the rollout default found four
defects in the *existing* core, each reproducible through a primary entrance.
Every one of them is the same class of mistake — a claim in a response, a
registry row or a payload that was not true of the world — so each is pinned
here by its observable consequence rather than by the shape of the fix:

1. **A task is a semantic decision.** ``add_todo`` in a graph write mode could
   not produce a listable task at all, so the whole add → list → complete →
   reopen → delete workflow was unavailable after cutover. Tasks now commit
   through the journal, the TODO view is the replayable ``todo`` projection, a
   reopen clears the finish time, a replay keeps event time, and pre-existing
   tasks are *migrated* rather than rebuilt away.
2. **A retraction needs no invented replacement.** Deleting a claim used to be
   expressible only by superseding it with a node whose content nobody decided.
   ``apply_semantic_lifecycle`` is the target-only decision.
3. **Our own failed write is not an external edit.** ``publish_l1`` recorded a
   surface's body hash before writing the file, so a failed write left the
   registry describing bytes that never landed — and recovery then reported
   ``surface_drift`` forever, with ``vector`` and ``l0`` stuck behind it. Both
   crash windows now converge without ``force=True``, and a genuine external
   edit is still refused.
4. **A retracted section does not ride along with a surviving sibling.** A
   keyword result is one file carrying several sections, and the graph kept the
   whole payload whenever any one seed survived.

Isolation: tmp_path config, tmp SQLite journal/graph/vector databases, a
deterministic fake embedding seam, no network, no user data. Fixture content is
neutral placeholder text.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp import graph_projection, graph_task, server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_application import (
    GRAPH_COMMIT_COMMITTED,
    GRAPH_COMMIT_PENDING_PROJECTIONS,
    GRAPH_COMMIT_REJECTED_DRIFT,
    GRAPH_COMMIT_REJECTED_INVALID,
    GRAPH_COMMIT_REJECTED_SURFACE,
    GRAPH_TASK_AUTHORITY_UNSEEDED,
    GRAPH_TASK_UNKNOWN,
    PROJECTION_DEPENDENCIES,
    RECOVERY_INCOMPLETE,
    TASK_SEED_APPLIED,
    TASK_SEED_PREVIEW,
    recover_projections,
    seed_task_authority,
    task_authority,
)
from layered_memory_mcp.graph_journal import (
    PROJECTION_NAMES,
    PROJECTION_TODO,
    MemoryEventJournal,
    body_sha256,
)
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    GraphScope,
    NodeKind,
    NodeStatus,
    RelationType,
)
from layered_memory_mcp.graph_write import (
    RelationAssertion,
    SemanticLifecycleRequest,
    SemanticWriteRequest,
)
from layered_memory_mcp.todo_store import TodoStore

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)
EXPLICIT = Derivation(method=DerivationMethod.USER_EXPLICIT)

DOMAIN = "retrieval"
SECTION = "Index maintenance"
BODY = "The retrieval index is rebuilt nightly at 02:00 UTC."
BODY_V2 = "The retrieval index is rebuilt nightly at 03:00 UTC."
SENTINEL = "SENTINEL-FORBIDDEN-SECTION-MUST-NOT-APPEAR"


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    """No model download, no network: these tests are about semantics."""
    from layered_memory_mcp.storage import vector_store

    monkeypatch.setattr(
        vector_store,
        "_embed_texts",
        lambda texts: np.ones((len(texts), 512), dtype=np.float32) / np.sqrt(512),
    )


def _build(tmp_path, mode: str) -> MemoryConfig:
    home = tmp_path / f"home-{mode}"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    kwargs = (
        {}
        if mode == "legacy"
        else {
            "graph_db_path": str(home / "data" / "graph.db"),
            "graph_journal_path": str(home / "data" / "journal.db"),
        }
    )
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode=mode,
        auto_maintain=False,
        **kwargs,
    )


def _install(config: MemoryConfig, monkeypatch) -> MemoryConfig:
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    monkeypatch.setattr(server, "_v2_stores", {})
    monkeypatch.setattr(server, "_todo_store", None, raising=False)
    return config


@pytest.fixture
def graph_config(tmp_path, monkeypatch) -> MemoryConfig:
    return _install(_build(tmp_path, "graph"), monkeypatch)


@pytest.fixture
def legacy_config(tmp_path, monkeypatch) -> MemoryConfig:
    return _install(_build(tmp_path, "legacy"), monkeypatch)


def _call(coro) -> dict:
    return json.loads(asyncio.run(coro))


def _decision(
    node_id: str,
    body: str,
    *,
    supersedes: str | None = None,
    scope: GraphScope | None = None,
    valid_to: datetime | None = None,
) -> dict:
    return SemanticWriteRequest(
        node=GraphNode(
            id=node_id,
            kind=NodeKind.CLAIM,
            label=node_id,
            content=body,
            recorded_at=T0,
            derivation=EXPLICIT,
            scope=scope or GraphScope(),
            valid_to=valid_to,
        ),
        supersedes=(
            [
                RelationAssertion(
                    relation_type=RelationType.SUPERSEDES,
                    target_id=supersedes,
                    derivation=EXPLICIT,
                )
            ]
            if supersedes
            else []
        ),
        occurred_at=T0,
        scope=scope or GraphScope(),
        request_key=node_id,
    ).to_dict()


def _inject(
    body: str,
    node_id: str,
    *,
    section: str = SECTION,
    supersedes: str | None = None,
    scope: GraphScope | None = None,
    valid_to: datetime | None = None,
) -> dict:
    return _call(
        server.inject_knowledge_tool(
            domain=DOMAIN,
            section=section,
            content=body,
            semantic_write_request=_decision(
                node_id, body, supersedes=supersedes, scope=scope, valid_to=valid_to
            ),
        )
    )


def _document(config: MemoryConfig) -> str:
    path = Path(config.knowledge_dir) / f"{DOMAIN}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _journal_events(config: MemoryConfig) -> int:
    path = Path(config.graph_journal_path)
    if not path.exists():
        return 0
    with sqlite3.connect(path) as connection:
        return int(
            connection.execute("SELECT count(*) FROM memory_events").fetchone()[0]
        )


def _surface_hashes(config: MemoryConfig) -> dict[str, str]:
    with MemoryEventJournal.open_readonly(config.graph_journal_path) as journal:
        return {record.entry_id: record.body_sha256 for record in journal.surfaces()}


def _intents(config: MemoryConfig) -> dict[str, str]:
    with MemoryEventJournal.open_readonly(config.graph_journal_path) as journal:
        return {
            intent.entry_id: intent.body_sha256 for intent in journal.surface_intents()
        }


def _rows(payload: dict) -> list[dict]:
    return [row for row in payload.get("todos", ()) if isinstance(row, dict)]


# ===========================================================================
# Blocker 1 — the task workflow, end to end, through the primary entrances
# ===========================================================================

def test_the_todo_view_is_a_managed_projection_with_its_own_checkpoint():
    """A projection the recovery entrance does not know about is not managed."""
    assert PROJECTION_TODO in PROJECTION_NAMES
    assert PROJECTION_DEPENDENCIES[PROJECTION_TODO] == ("graph",)


def test_a_task_added_through_the_primary_entrance_is_listable(graph_config):
    added = _call(
        server.add_todo(
            domain="layered-memory",
            content="verify the graph cutover rehearsal",
            priority="high",
        )
    )

    assert added["success"] is True, added
    assert added["action"] == "graph_task_committed"
    assert added["task_id"] == added["id"]
    assert added["graph"]["committed"] is True
    assert added["graph"]["projections_pending"] == []

    listed = _call(server.list_todos(status="pending"))
    rows = _rows(listed)
    assert [row["id"] for row in rows] == [added["task_id"]]
    assert rows[0]["priority"] == "high"
    assert rows[0]["domain"] == "layered-memory"


def test_the_listed_task_comes_from_the_journal_and_not_from_a_second_store(
    graph_config,
):
    """Delete the projection file; the authority must be able to rebuild it."""
    added = _call(server.add_todo(domain="ops", content="placeholder task"))
    task_id = added["task_id"]
    before = _rows(_call(server.list_todos()))[0]

    graph_task.task_store_path(graph_config).unlink()
    server._todo_store = None

    report = recover_projections(graph_config, force=True)
    assert report.status != RECOVERY_INCOMPLETE, report
    assert report.projections_pending == []

    after = _rows(_call(server.list_todos()))
    assert [row["id"] for row in after] == [task_id]
    # Event time, not a fresh clock: a replay reproduces the row it recorded.
    assert after[0]["created_at"] == before["created_at"]
    assert after[0]["updated_at"] == before["updated_at"]


def test_completing_a_task_and_reopening_it_clears_the_finish_time(graph_config):
    added = _call(server.add_todo(domain="ops", content="close the loop"))
    task_id = added["task_id"]

    done = _call(server.update_todo(todo_id=task_id, status="completed"))
    assert done["success"] is True, done
    assert done["task"]["completed_at"], done
    assert done["task"]["version"] == 2

    completed = _rows(_call(server.list_todos(status="completed")))
    assert [row["id"] for row in completed] == [task_id]
    assert completed[0]["completed_at"] is not None

    reopened = _call(server.update_todo(todo_id=task_id, status="pending"))
    assert reopened["success"] is True, reopened
    assert reopened["task"]["completed_at"] is None
    assert reopened["task"]["version"] == 3

    pending = _rows(_call(server.list_todos(status="pending")))
    assert [row["id"] for row in pending] == [task_id]
    assert not pending[0]["completed_at"], pending[0]
    assert not pending[0]["cancelled_at"], pending[0]


def test_a_cancelled_task_stamps_only_its_own_terminal_timestamp(graph_config):
    added = _call(server.add_todo(domain="ops", content="abandon this"))
    task_id = added["task_id"]

    _call(server.update_todo(todo_id=task_id, status="completed"))
    cancelled = _call(server.update_todo(todo_id=task_id, status="cancelled"))

    assert cancelled["task"]["cancelled_at"], cancelled
    assert cancelled["task"]["completed_at"] is None, cancelled


def test_an_update_is_a_new_version_that_supersedes_the_old_one(graph_config):
    """A task payload is never edited in place: history stays queryable."""
    from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore

    added = _call(server.add_todo(domain="ops", content="first wording"))
    task_id = added["task_id"]
    _call(server.update_todo(todo_id=task_id, content="second wording"))

    store = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        first = store.get_node(graph_task.task_node_id(task_id, 1))
        second = store.get_node(graph_task.task_node_id(task_id, 2))
    finally:
        store.close()

    assert first is not None and first.status is NodeStatus.SUPERSEDED
    assert first.content == "first wording"
    assert second is not None and second.status is NodeStatus.ACTIVE
    assert second.content == "second wording"


def test_deleting_a_task_removes_the_row_and_keeps_the_history(graph_config):
    added = _call(server.add_todo(domain="ops", content="placeholder task"))
    task_id = added["task_id"]
    events_before = _journal_events(graph_config)

    deleted = _call(server.delete_todo(todo_id=task_id))
    assert deleted["success"] is True, deleted

    assert _rows(_call(server.list_todos())) == []
    assert _journal_events(graph_config) > events_before
    assert task_id in task_authority(graph_config).known_ids
    assert task_id not in task_authority(graph_config).records


def test_a_deleted_task_cannot_be_mutated_again(graph_config):
    added = _call(server.add_todo(domain="ops", content="placeholder task"))
    task_id = added["task_id"]
    _call(server.delete_todo(todo_id=task_id))

    again = _call(server.update_todo(todo_id=task_id, status="completed"))
    assert again["success"] is False
    assert again["error"] == GRAPH_TASK_UNKNOWN


def test_the_task_statistics_entrance_reads_the_projection(graph_config):
    first = _call(server.add_todo(domain="ops", content="one", priority="blocker"))
    _call(server.add_todo(domain="ops", content="two"))
    _call(server.update_todo(todo_id=first["task_id"], status="completed"))

    stats = _call(server.get_todo_stats())
    by_status = {row["status"]: row["count"] for row in stats["stats"]["by_status"]}
    assert by_status == {"pending": 1, "completed": 1}
    assert stats["stats"]["total"] == 2


# --- the negative controls -------------------------------------------------

def test_an_unknown_task_id_is_refused_without_journalling_anything(graph_config):
    before = _journal_events(graph_config)

    missing = _call(server.update_todo(todo_id="task-does-not-exist", status="completed"))

    assert missing["success"] is False
    assert missing["error"] == GRAPH_TASK_UNKNOWN
    assert "list_todos" in missing["supported_alternative"]
    assert _journal_events(graph_config) == before


def test_an_unknown_task_id_does_not_even_create_the_journal(graph_config):
    """A refusal reaches no writable handle, so it establishes no authority."""
    assert not Path(graph_config.graph_journal_path).exists()

    missing = _call(server.delete_todo(todo_id="task-does-not-exist"))

    assert missing["success"] is False
    assert not Path(graph_config.graph_journal_path).exists()


@pytest.mark.parametrize(
    "field,value",
    [("status", "banana"), ("priority", "urgent-ish")],
)
def test_an_invalid_enum_value_is_refused_before_the_journal(
    graph_config, field, value
):
    added = _call(server.add_todo(domain="ops", content="placeholder task"))
    before = _journal_events(graph_config)

    refused = _call(server.update_todo(todo_id=added["task_id"], **{field: value}))

    assert refused["success"] is False, refused
    assert value in refused["error"]
    assert _journal_events(graph_config) == before


def test_an_update_that_changes_nothing_is_refused_rather_than_committed(
    graph_config,
):
    added = _call(server.add_todo(domain="ops", content="placeholder task"))
    before = _journal_events(graph_config)

    refused = _call(server.update_todo(todo_id=added["task_id"]))

    assert refused["success"] is False, refused
    assert _journal_events(graph_config) == before


def test_the_legacy_mode_task_store_is_still_authoritative(legacy_config):
    """Explicit rollback keeps working, and reaches no graph operation."""
    added = _call(server.add_todo(domain="ops", content="placeholder task"))
    assert added["success"] is True
    assert "graph" not in added
    assert "task_id" not in added

    updated = _call(server.update_todo(todo_id=added["id"], status="completed"))
    assert updated == {"success": True, "id": added["id"]}

    rows = _rows(_call(server.list_todos()))
    assert [row["id"] for row in rows] == [added["id"]]


def test_the_dashboard_task_endpoint_commits_through_the_same_authority(
    graph_config, monkeypatch
):
    """The other transport, through the one application layer — not a second one."""
    pytest.importorskip("fastapi")
    from layered_memory_mcp.dashboard_plugin import plugin_api

    monkeypatch.setattr(plugin_api, "_get_config", lambda: graph_config)
    monkeypatch.setattr(plugin_api, "_todo_store", None, raising=False)

    added = _call(server.add_todo(domain="ops", content="placeholder task"))
    task_id = added["task_id"]

    verdict = plugin_api._commit_task_status(graph_config, task_id, "completed")
    assert verdict["success"] is True, verdict
    assert verdict["graph_status"] == GRAPH_COMMIT_COMMITTED

    rows = _rows(_call(server.list_todos(status="completed")))
    assert [row["id"] for row in rows] == [task_id]
    assert rows[0]["completed_at"] is not None

    refused = plugin_api._commit_task_status(
        graph_config, "task-does-not-exist", "completed"
    )
    assert refused == {"success": False, "error": GRAPH_TASK_UNKNOWN}


# --- migration: pre-existing tasks are seeded, never rebuilt away ----------

def _legacy_row(config: MemoryConfig, **overrides) -> str:
    """One task written the pre-journal way, directly into the store."""
    from layered_memory_mcp.models import TodoEntry, TodoPriority

    store = TodoStore(graph_task.task_store_path(config))
    entry = TodoEntry(
        domain=overrides.get("domain", "ops"),
        content=overrides.get("content", "a task from before the journal"),
        title=overrides.get("title", "legacy task"),
        blocked_by=overrides.get("blocked_by", ["something-else"]),
        priority=overrides.get("priority", TodoPriority.BLOCKER),
        notes=overrides.get("notes", "placeholder note"),
        created_at=T0 - timedelta(days=30),
        updated_at=T0 - timedelta(days=29),
    )
    store.add(entry)
    return entry.id


def test_a_task_store_the_journal_never_saw_blocks_a_mutation_loudly(graph_config):
    legacy_id = _legacy_row(graph_config)

    refused = _call(server.add_todo(domain="ops", content="a new task"))

    assert refused["success"] is False, refused
    assert refused["error"] == GRAPH_TASK_AUTHORITY_UNSEEDED
    assert "seed_task_authority" in refused["supported_alternative"]
    # The pre-existing task is untouched, which is the whole point.
    assert [row["id"] for row in _rows(_call(server.list_todos()))] == [legacy_id]
    assert _journal_events(graph_config) == 0


def test_the_seed_preview_writes_nothing_at_all(graph_config):
    _legacy_row(graph_config)
    store_path = graph_task.task_store_path(graph_config)
    before = store_path.stat().st_mtime_ns

    report = seed_task_authority(graph_config)

    assert report.status == TASK_SEED_PREVIEW
    assert report.stored_tasks == 1
    assert report.seeded == 1
    assert not Path(graph_config.graph_journal_path).exists()
    assert not Path(graph_config.graph_db_path).exists()
    assert store_path.stat().st_mtime_ns == before


def test_seeding_carries_every_pre_existing_task_across_verbatim(graph_config):
    legacy_id = _legacy_row(graph_config)
    before = _rows(_call(server.list_todos()))[0]

    report = seed_task_authority(graph_config, apply=True)
    assert report.status == TASK_SEED_APPLIED, report
    assert report.seeded == 1
    assert report.projections_pending == []
    assert report.errors == []

    server._todo_store = None
    after = _rows(_call(server.list_todos()))
    assert [row["id"] for row in after] == [legacy_id]
    for field in ("created_at", "priority", "status", "domain", "content", "notes"):
        assert after[0][field] == before[field], field
    assert after[0]["blocked_by"] == ["something-else"]

    authority = task_authority(graph_config)
    assert authority.unseeded == []
    assert authority.known_ids == [legacy_id]


def test_a_seeded_task_is_recorded_as_migrated_rather_than_decided(graph_config):
    from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore

    legacy_id = _legacy_row(graph_config)
    seed_task_authority(graph_config, apply=True)

    store = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        node = store.get_node(graph_task.task_node_id(legacy_id, 1))
    finally:
        store.close()
    assert node is not None
    assert node.derivation is not None
    assert node.derivation.method is DerivationMethod.MIGRATED


def test_seeding_twice_is_a_no_op_and_mutations_work_afterwards(graph_config):
    legacy_id = _legacy_row(graph_config)
    seed_task_authority(graph_config, apply=True)
    events = _journal_events(graph_config)

    again = seed_task_authority(graph_config, apply=True)
    assert again.status == "graph_task_seed_nothing_to_do"
    assert again.seeded == 0
    assert _journal_events(graph_config) == events

    server._todo_store = None
    done = _call(server.update_todo(todo_id=legacy_id, status="completed"))
    assert done["success"] is True, done
    rows = _rows(_call(server.list_todos(status="completed")))
    assert [row["id"] for row in rows] == [legacy_id]


def test_the_seed_cli_previews_by_default_and_writes_nothing(
    graph_config, capsys
):
    """The operator path, and its default verb is the read-only one."""
    _legacy_row(graph_config)

    exit_code = graph_task.main(["--home", str(graph_config.home)])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["command"] == "plan"
    assert report["status"] == TASK_SEED_PREVIEW
    assert report["seeded"] == 1
    assert not Path(graph_config.graph_journal_path).exists()


def test_the_seed_cli_refuses_a_home_that_is_not_bound_to_a_generation(
    tmp_path, capsys
):
    """No journal to seed into is a bounded refusal, not a guessed location."""
    from layered_memory_mcp.models import TodoEntry

    home = tmp_path / "unbound"
    (home / "data").mkdir(parents=True)
    store = TodoStore(home / "data" / "todos.db")
    store.add(TodoEntry(domain="ops", content="a task with nowhere to go"))

    exit_code = graph_task.main(["apply", "--home", str(home)])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1, report
    assert report["status"] != TASK_SEED_APPLIED
    assert report["stored_tasks"] == 1
    assert report["seeded"] == 0
    assert report["errors"] == ["graph_journal_not_configured"]
    assert not (home / "data" / "journal.db").exists()


def test_the_projection_refuses_to_rebuild_a_store_it_has_no_authority_for(
    graph_config,
):
    """The guard that makes the migration mandatory instead of optional."""
    added = _call(server.add_todo(domain="ops", content="a decided task"))
    _legacy_row(graph_config, content="a task the journal never saw")

    with pytest.raises(graph_projection.ProjectionError) as refusal:
        graph_projection.publish_todo(
            graph_config,
            [
                graph_task.task_node(
                    task_authority(graph_config).records[added["task_id"]],
                    recorded_at=T0,
                )
            ],
        )
    assert "todo_store_not_adopted" in str(refusal.value)


def test_an_empty_task_authority_does_not_bring_the_store_into_existence(
    graph_config,
):
    """Publishing nothing writes nothing — not even an empty task database."""
    _inject(BODY, "claim_only_knowledge")

    assert not graph_task.task_store_path(graph_config).exists()


# ===========================================================================
# Blocker 2 — a pure retraction, with no invented replacement
# ===========================================================================

def test_a_lifecycle_request_carries_a_target_and_no_node():
    fields = SemanticLifecycleRequest.__dataclass_fields__
    assert "node" not in fields
    for required in ("target_id", "action", "occurred_at", "request_key"):
        assert required in fields


def test_a_supersession_is_not_available_as_a_target_only_action():
    """A replacement decision has to carry the replacement."""
    with pytest.raises(Exception) as refusal:
        SemanticLifecycleRequest(
            target_id="claim_x", action="supersede", occurred_at=T0
        )
    assert "supersede" in str(refusal.value)


def _retract(node_id: str, *, action: str = "retract", key: str = "") -> dict:
    return _call(
        server.apply_semantic_lifecycle(
            semantic_lifecycle_request=SemanticLifecycleRequest(
                target_id=node_id,
                action=action,
                occurred_at=T0 + timedelta(minutes=5),
                request_key=key or f"{action}-{node_id}",
            ).to_dict()
        )
    )


def test_retracting_a_claim_retires_its_readable_section(graph_config):
    _inject(BODY, "claim_nightly")
    assert BODY in _document(graph_config)

    retracted = _retract("claim_nightly")

    assert retracted["success"] is True, retracted
    assert retracted["graph"]["committed"] is True
    assert retracted["graph"]["projections_pending"] == []
    assert BODY not in _document(graph_config)
    assert SECTION not in _document(graph_config)


def test_a_retraction_keeps_the_history_it_retracted(graph_config):
    from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore

    _inject(BODY, "claim_nightly")
    _retract("claim_nightly")

    store = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        node = store.get_node("claim_nightly")
    finally:
        store.close()
    assert node is not None, "a retraction is not a deletion"
    assert node.status is NodeStatus.RETRACTED
    assert node.content == BODY


def test_an_unknown_lifecycle_target_is_refused_without_journalling(graph_config):
    _inject(BODY, "claim_nightly")
    before = _journal_events(graph_config)

    refused = _retract("claim_that_does_not_exist")

    assert refused["success"] is False
    assert refused["error"] == GRAPH_COMMIT_REJECTED_INVALID
    assert _journal_events(graph_config) == before


def test_a_lifecycle_decision_in_the_wrong_scope_is_refused(graph_config):
    _inject(BODY, "claim_nightly")
    before = _journal_events(graph_config)

    refused = _call(
        server.apply_semantic_lifecycle(
            semantic_lifecycle_request=SemanticLifecycleRequest(
                target_id="claim_nightly",
                action="retract",
                occurred_at=T0 + timedelta(minutes=5),
                request_key="retract-wrong-scope",
                scope=GraphScope(namespace="somebody-else"),
            ).to_dict()
        )
    )

    assert refused["success"] is False
    assert refused["error"] == GRAPH_COMMIT_REJECTED_INVALID
    assert _journal_events(graph_config) == before


def test_a_transition_the_target_cannot_take_is_refused(graph_config):
    _inject(BODY, "claim_nightly")
    assert _retract("claim_nightly")["success"] is True
    before = _journal_events(graph_config)

    again = _retract("claim_nightly", key="retract-claim_nightly-again")

    assert again["success"] is False
    assert again["error"] == GRAPH_COMMIT_REJECTED_INVALID
    assert _journal_events(graph_config) == before


def test_restoring_a_retracted_claim_republishes_its_section(graph_config):
    _inject(BODY, "claim_nightly")
    _retract("claim_nightly")
    assert BODY not in _document(graph_config)

    restored = _retract("claim_nightly", action="restore")

    assert restored["success"] is True, restored
    assert BODY in _document(graph_config)


def test_a_restore_that_would_give_one_section_two_owners_is_refused(graph_config):
    """The replacement already owns the surface; which one keeps it is a decision."""
    _inject(BODY, "claim_nightly")
    _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")
    before = _journal_events(graph_config)

    refused = _retract("claim_nightly", action="restore")

    assert refused["success"] is False, refused
    assert refused["error"] == GRAPH_COMMIT_REJECTED_SURFACE
    assert "surface_owned_by:claim_nightly_v2" in refused["graph"]["errors"]
    assert _journal_events(graph_config) == before
    assert BODY_V2 in _document(graph_config)


def test_a_lifecycle_decision_is_refused_in_legacy_mode(legacy_config):
    refused = _call(
        server.apply_semantic_lifecycle(
            semantic_lifecycle_request=SemanticLifecycleRequest(
                target_id="claim_nightly", action="retract", occurred_at=T0
            ).to_dict()
        )
    )

    assert refused["success"] is False
    assert refused["error"] == "graph_lifecycle_requires_write_mode"


def test_a_malformed_lifecycle_request_is_a_request_error(graph_config):
    refused = _call(
        server.apply_semantic_lifecycle(semantic_lifecycle_request={"action": "retract"})
    )

    assert refused["success"] is False
    assert refused["error"] == "invalid_semantic_lifecycle_request"
    assert refused["semantic_lifecycle_request_errors"]
    assert _journal_events(graph_config) == 0


def test_a_missing_lifecycle_request_names_the_argument(graph_config):
    refused = _call(server.apply_semantic_lifecycle())

    assert refused["success"] is False
    assert refused["error"] == "invalid_semantic_lifecycle_request"
    assert "semantic_lifecycle_request" in refused["supported_alternative"]


# ===========================================================================
# Blocker 3 — a failed publication is recoverable, an external edit is not
# ===========================================================================

@contextmanager
def _crashing_publication(*, after_replace: bool = False):
    """Fail one publication, on one side of the atomic replacement.

    ``after_replace=False`` is a crash *before* the bytes land — the window the
    registry used to be poisoned in. ``after_replace=True`` is the other
    window: the document is replaced and the registry never finds out.

    Patched by hand rather than with the ``monkeypatch`` fixture, deliberately:
    these tests have to keep running *after* the fault is withdrawn, and
    ``monkeypatch.undo()`` would also revert the fixture's ``server._config``
    and the deterministic embedding seam — which would make recovery report
    "not configured" for a reason that has nothing to do with the crash.
    """
    if not after_replace:
        real = graph_projection.replace_document

        def failing_replace(path, text):
            raise OSError("simulated crash before the replacement landed")

        graph_projection.replace_document = failing_replace
        try:
            yield
        finally:
            graph_projection.replace_document = real
        return

    real_record = MemoryEventJournal.record_surface

    def failing_record(self, record):
        raise OSError("simulated crash after the replacement, before finalizing")

    MemoryEventJournal.record_surface = failing_record
    try:
        yield
    finally:
        MemoryEventJournal.record_surface = real_record


@contextmanager
def _clock_pinned_to(moment: datetime):
    """Pin the graph's "is this current now?" clock for one publication.

    A validity window that closes *after* a section was published is the one
    way a healthy pipeline legitimately leaves non-current material on disk:
    nothing republishes a document when a clock passes. Pinning the publish-time
    clock is how that state is reached deterministically instead of by waiting.
    """
    from layered_memory_mcp import graph_models

    real = graph_models._utc_now
    graph_models._utc_now = lambda: moment
    try:
        yield
    finally:
        graph_models._utc_now = real


def test_a_crash_before_the_replacement_leaves_a_truthful_registry(graph_config):
    _inject(BODY, "claim_nightly")
    published = _surface_hashes(graph_config)
    assert published == {
        entry: body_sha256(BODY) for entry in published
    }, published

    with _crashing_publication():
        failed = _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")

    assert failed["graph"]["committed"] is True, failed
    assert failed["graph"]["status"] == GRAPH_COMMIT_PENDING_PROJECTIONS
    # The honest half: the decision is durable, the surface did not land, and
    # the response says which surface rather than only which projection.
    assert failed["graph"]["surface_published"] is False
    entry_id = failed["graph"]["entry_id"]
    assert f"l1_projection_pending:{entry_id}" in failed["graph"]["errors"]
    # The registry still describes the body the file still holds. This is the
    # defect: it used to describe bytes that never existed.
    assert _surface_hashes(graph_config) == published
    assert BODY in _document(graph_config)
    assert BODY_V2 not in _document(graph_config)


def test_a_crash_before_the_replacement_recovers_without_force(graph_config):
    _inject(BODY, "claim_nightly")
    with _crashing_publication():
        _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")

    recovered = _call(server.recover_graph_projections())

    assert recovered["status"] != RECOVERY_INCOMPLETE, recovered
    assert recovered["projections_pending"] == []
    assert "external" not in json.dumps(recovered).lower()
    assert "drift" not in json.dumps(recovered).lower()
    assert BODY_V2 in _document(graph_config)
    assert BODY not in _document(graph_config)
    assert _surface_hashes(graph_config) == {
        entry: body_sha256(BODY_V2) for entry in _surface_hashes(graph_config)
    }
    assert _intents(graph_config) == {}


def test_every_projection_queued_behind_the_failed_one_recovers_with_it(graph_config):
    """`vector` and `l0` were stuck behind `l1` forever; `todo` never was."""
    _inject(BODY, "claim_nightly")
    with _crashing_publication():
        failed = _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")

    assert set(failed["graph"]["projections_pending"]) == {"l0", "l1", "vector"}

    recovered = _call(server.recover_graph_projections())

    assert sorted(recovered["recovered"]) == ["l0", "l1", "vector"]
    head = recovered["head_position"]
    assert recovered["checkpoints"] == {name: head for name in PROJECTION_NAMES}


def test_a_task_decision_is_not_held_hostage_by_a_refused_section(graph_config):
    _inject(BODY, "claim_nightly")
    with _crashing_publication():
        _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")
        added = _call(server.add_todo(domain="ops", content="placeholder task"))

    assert added["success"] is True, added
    assert "todo" not in added["graph"]["projections_pending"], added
    assert [row["id"] for row in _rows(_call(server.list_todos()))] == [
        added["task_id"]
    ]


def test_a_crash_after_the_replacement_also_recovers(graph_config):
    """The other window: the file landed and the registry never found out."""
    _inject(BODY, "claim_nightly")
    published = _surface_hashes(graph_config)

    with _crashing_publication(after_replace=True):
        failed = _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")

    assert failed["graph"]["committed"] is True
    assert failed["graph"]["surface_published"] is False
    # Exactly the state that used to read as an external edit: new bytes on
    # disk, the old hash in the registry — plus the intent that explains it.
    assert BODY_V2 in _document(graph_config)
    assert _surface_hashes(graph_config) == published
    assert _intents(graph_config) == {
        entry: body_sha256(BODY_V2) for entry in _intents(graph_config)
    }

    recovered = _call(server.recover_graph_projections())

    assert recovered["status"] != RECOVERY_INCOMPLETE, recovered
    assert recovered["projections_pending"] == []
    assert "external" not in json.dumps(recovered).lower()
    assert BODY_V2 in _document(graph_config)
    assert _intents(graph_config) == {}


def test_a_crash_before_finalizing_a_brand_new_section_is_not_unadopted(graph_config):
    """A first publication has no previous record to compare against at all."""
    with _crashing_publication(after_replace=True):
        failed = _inject(BODY, "claim_nightly")

    assert failed["graph"]["committed"] is True
    assert BODY in _document(graph_config)
    assert _surface_hashes(graph_config) == {}

    recovered = _call(server.recover_graph_projections())

    assert recovered["status"] != RECOVERY_INCOMPLETE, recovered
    assert "not_adopted" not in json.dumps(recovered)
    assert recovered["projections_pending"] == []


def test_a_failed_publication_leaves_no_temporary_file_behind(graph_config):
    real_write = Path.write_text

    def failing_write(self, data, *args, **kwargs):
        if graph_projection.PUBLISH_TEMP_SUFFIX in self.name:
            raise OSError("simulated crash while writing the temporary document")
        return real_write(self, data, *args, **kwargs)

    Path.write_text = failing_write
    try:
        failed = _inject(BODY, "claim_nightly")
    finally:
        Path.write_text = real_write

    assert failed["graph"]["committed"] is True
    assert failed["graph"]["surface_published"] is False
    leftovers = sorted(p.name for p in Path(graph_config.knowledge_dir).iterdir())
    assert leftovers == [], leftovers

    recovered = _call(server.recover_graph_projections())
    assert recovered["projections_pending"] == []
    assert BODY in _document(graph_config)


def test_republishing_unchanged_content_does_not_touch_the_document(graph_config):
    """Idempotent in the literal sense: no write, so no new mtime.

    The two-phase protocol must not have turned "nothing to do" into a
    rewrite-with-identical-bytes, which would make every recovery attempt look
    like a fresh external change to anything watching the file.
    """
    _inject(BODY, "claim_nightly")
    path = Path(graph_config.knowledge_dir) / f"{DOMAIN}.md"
    before = (path.stat().st_mtime_ns, path.read_bytes())

    recovered = _call(server.recover_graph_projections(force=True))

    assert recovered["projections_pending"] == []
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before
    assert _intents(graph_config) == {}


def test_an_external_edit_is_still_refused_and_preserved(graph_config):
    """The drift comparison is not relaxed: a human's edit is still protected."""
    _inject(BODY, "claim_nightly")
    edited = _document(graph_config).replace(BODY, "A human wrote this instead.")
    (Path(graph_config.knowledge_dir) / f"{DOMAIN}.md").write_text(
        edited, encoding="utf-8"
    )

    refused = _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")

    assert refused["success"] is False, refused
    assert refused["error"] == GRAPH_COMMIT_REJECTED_DRIFT
    assert any(
        error.startswith("surface_drift:") for error in refused["graph"]["errors"]
    ), refused
    assert "A human wrote this instead." in _document(graph_config)


def test_an_external_edit_keeps_recovery_refusing_rather_than_overwriting(
    graph_config,
):
    _inject(BODY, "claim_nightly")
    with _crashing_publication():
        _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")

    path = Path(graph_config.knowledge_dir) / f"{DOMAIN}.md"
    path.write_text(
        _document(graph_config).replace(BODY, "A human wrote this instead."),
        encoding="utf-8",
    )

    recovered = _call(server.recover_graph_projections())

    assert recovered["status"] == RECOVERY_INCOMPLETE, recovered
    assert any("surface_drift" in error for error in recovered["errors"]), recovered
    assert "A human wrote this instead." in _document(graph_config)


def test_the_document_is_replaced_atomically_and_never_truncated(graph_config):
    """A reader sees the whole old document or the whole new one."""
    import os

    _inject(BODY, "claim_nightly")
    path = Path(graph_config.knowledge_dir) / f"{DOMAIN}.md"
    seen: list[str] = []
    real_replace = os.replace

    def watching_replace(source, target, *args, **kwargs):
        # Mid-publication: the target still holds the complete previous body.
        seen.append(Path(target).read_text(encoding="utf-8"))
        return real_replace(source, target, *args, **kwargs)

    os.replace = watching_replace
    try:
        _inject(BODY_V2, "claim_nightly_v2", supersedes="claim_nightly")
    finally:
        os.replace = real_replace

    assert seen, "the publication did not go through an atomic replacement"
    assert BODY in seen[0]
    assert BODY_V2 in path.read_text(encoding="utf-8")


# ===========================================================================
# Blocker 4 — a surviving section does not carry an excluded sibling
# ===========================================================================

def _two_sections(
    *,
    drop_scope: GraphScope | None = None,
    drop_valid_to: datetime | None = None,
) -> None:
    """One file, two sections: one that must survive, one that must not."""
    assert _inject(
        "nightly rebuild cadence stays at 02:00 UTC",
        "claim_keep",
        section="Kept section",
    )["success"]
    assert _inject(
        f"nightly rebuild {SENTINEL}",
        "claim_drop",
        section="Dropped section",
        scope=drop_scope,
        valid_to=drop_valid_to,
    )["success"]


def _recall(keyword: str = "nightly rebuild") -> dict:
    return _call(server.recall_knowledge(keyword=keyword, top_n=5))


def test_a_retracted_section_does_not_ride_along_with_its_sibling(graph_config):
    _two_sections()
    assert _retract("claim_drop")["success"] is True

    found = _recall()

    assert SENTINEL not in json.dumps(found, ensure_ascii=False)
    assert "nightly rebuild cadence stays at 02:00 UTC" in json.dumps(
        found, ensure_ascii=False
    )


def test_an_expired_section_still_on_disk_is_filtered_out_of_the_payload(
    graph_config,
):
    """The sharp case: the graph excludes it, and the file still carries it.

    Nothing republishes a document when a validity window closes, so the
    retracted-section path is not the only way a file can hold material the
    graph says is not current — and this is the shape that used to leak.
    """
    with _clock_pinned_to(T0):
        _two_sections(drop_valid_to=T0 + timedelta(days=1))
    assert SENTINEL in _document(graph_config), "the premise: it is still on disk"

    found = _recall()

    blob = json.dumps(found, ensure_ascii=False)
    assert SENTINEL not in blob, blob
    assert found["results"], found
    result = found["results"][0]
    assert result["file"] == f"{DOMAIN}.md"
    assert [entry["section"] for entry in result["section_entries"]] == [
        "Kept section"
    ]
    assert [fragment["title"] for fragment in result["sections"]] == ["Kept section"]
    assert result["matched_sections"] == 1
    assert found["graph"]["excluded_sections"] == 1
    assert found["graph"]["excluded_results"] == 0
    assert "graph_excluded_lifecycle" in found["graph"]["exclusion_reasons"]


def test_an_out_of_namespace_section_does_not_ride_along_either(
    tmp_path, monkeypatch
):
    home = tmp_path / "home-namespaced"
    (home / "knowledge" / "mine").mkdir(parents=True)
    (home / "knowledge" / "shared").mkdir(parents=True)
    (home / "data").mkdir(parents=True)
    config = _install(
        MemoryConfig(
            home=str(home),
            knowledge_dir=str(home / "knowledge"),
            namespace="mine",
            graph_mode="graph",
            auto_maintain=False,
            graph_db_path=str(home / "data" / "graph.db"),
            graph_journal_path=str(home / "data" / "journal.db"),
        ),
        monkeypatch,
    )

    _two_sections(drop_scope=GraphScope(namespace="somebody-else"))
    document = (Path(config.knowledge_dirs[0]) / f"{DOMAIN}.md").read_text(
        encoding="utf-8"
    )
    assert SENTINEL in document, "the premise: it is still on disk"

    found = _recall()

    blob = json.dumps(found, ensure_ascii=False)
    assert SENTINEL not in blob, blob
    assert found["results"], found
    assert found["graph"]["excluded_sections"] == 1
    assert "graph_excluded_scope" in found["graph"]["exclusion_reasons"]


def test_a_file_with_nothing_excluded_is_returned_exactly_as_before(graph_config):
    """The negative control: the screen must not touch an untouched answer."""
    _two_sections()

    found = _recall()

    assert found["graph"]["excluded_sections"] == 0
    assert found["graph"]["excluded_results"] == 0
    result = found["results"][0]
    assert sorted(entry["section"] for entry in result["section_entries"]) == [
        "Dropped section",
        "Kept section",
    ]
    assert result["matched_sections"] == 2
    assert SENTINEL in json.dumps(found, ensure_ascii=False)


def test_a_fragment_whose_owning_section_cannot_be_identified_fails_closed(
    graph_config,
):
    """With a known forbidden section in the file, "probably fine" is not an answer."""
    from layered_memory_mcp import recall as recall_module

    with _clock_pinned_to(T0):
        _two_sections(drop_valid_to=T0 + timedelta(days=1))

    real_extract = recall_module.extract_relevant_sections

    def untraceable(keyword, content, max_sections=3):
        return [
            {**fragment, "owner_section": None, "owner_occurrence": 0}
            for fragment in real_extract(keyword, content, max_sections)
        ]

    recall_module.extract_relevant_sections = untraceable
    try:
        found = _recall()
    finally:
        recall_module.extract_relevant_sections = real_extract

    blob = json.dumps(found, ensure_ascii=False)
    assert SENTINEL not in blob, blob
    assert found["results"][0]["sections"] == []
    assert found["results"][0]["matched_sections"] == 0
    assert any(
        warning.startswith("section_payload_unmappable:")
        for warning in found["graph"]["warnings"]
    ), found["graph"]


def test_the_keyword_producer_declares_which_h2_owns_each_fragment():
    """The identity the screen relies on is the canonical one, not a title match."""
    from layered_memory_mcp.injector import _bodyful_sections
    from layered_memory_mcp.recall import extract_relevant_sections

    document = (
        "preamble mentions nightly rebuild\n\n"
        "## Kept section\n\nnightly rebuild at 02:00\n\n"
        "## Dropped section\n\nnightly rebuild placeholder\n\n"
        "### deeper heading\nnightly rebuild in a sub-heading\n\n"
        "## Kept section\n\nnightly rebuild, second copy\n"
    )
    fragments = extract_relevant_sections("nightly rebuild", document, max_sections=10)
    owners = [
        (fragment["owner_section"], fragment["owner_occurrence"])
        for fragment in fragments
    ]

    assert owners == [
        (None, 0),                  # the preamble belongs to no H2
        ("Kept section", 0),
        ("Dropped section", 0),
        ("Dropped section", 0),     # the H3 belongs to the H2 containing it
        ("Kept section", 1),
    ]
    # And the occurrence numbering is the projection's own, not a recount.
    labels = [label for label, _body in _bodyful_sections(document)]
    assert labels == ["Kept section", "Dropped section", "Kept section"]


def test_legacy_recall_is_untouched_by_the_section_screen(legacy_config):
    path = Path(legacy_config.knowledge_dir) / f"{DOMAIN}.md"
    path.write_text(
        "## Kept section\n\nnightly rebuild stays\n\n"
        f"## Dropped section\n\nnightly rebuild {SENTINEL}\n",
        encoding="utf-8",
    )

    found = _recall()

    assert "graph" not in found
    assert SENTINEL in json.dumps(found, ensure_ascii=False)

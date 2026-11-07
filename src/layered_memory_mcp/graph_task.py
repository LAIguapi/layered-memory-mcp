"""Tasks as semantic decisions, and the TODO view as a projection of them.

WHAT THIS MODULE IS
-------------------
The typed task mutation contract ARCHITECTURE_v2.md's framework contract asks
for: "Framework-owned tasks must either follow the journal or reject
incompatible writes before effects." A task mutation changes semantic state, so
in a graph write mode it is an *event* in the one journal — never a second
ledger, and never direct SQL smuggled past the mutation guard.

Three shapes, and nothing else:

* :class:`TaskMutation` — one explicit add/update/delete decision, validated
  before anything is opened. The transport shape every entrance builds.
* :class:`TaskRecord` — one task as a reader sees it. This is both the payload a
  ``TASK`` node carries and the row the projection writes, so "what the journal
  says" and "what ``list_todos`` shows" cannot be two different vocabularies.
* the projection — :func:`task_records` folds a replayed node set into the
  current task state, and :func:`publish_tasks` writes that state into the
  ``todos`` table the existing read entrances already read.

HOW A TASK CHANGES
------------------
A task is a *versioned* node, exactly as a revised claim is:

* **add** — one ``INJECT`` of version 1;
* **update** — a new version node, a ``SUPERSEDES`` edge to the previous one and
  the ``SUPERSEDE`` transition that retires it. The same two-effect rule
  :mod:`~layered_memory_mcp.graph_write` applies to every other revision, for
  the same reason: a payload edited in place would erase what the task used to
  say;
* **delete** — a pure ``RETRACT`` of the current version
  (:class:`~layered_memory_mcp.graph_write.SemanticLifecycleRequest`). The node
  and every version before it stay queryable as history.

``task_id`` is stable across every version and is what an operator holds; the
*node* ID is derived from ``(task_id, version)``, so replanning the same
mutation is byte-identical and committing it twice is a no-op.

WHAT IT IS NOT
--------------
- NOT a second authority. The journal decides; this module reads the replayed
  node set and writes a projection of it. Nothing here edits a task row in
  place, and the projection is replaceable from the stream at any time.
- NOT a clock. Every timestamp a task carries is stamped from the deciding
  event's ``occurred_at``, so replaying the journal reproduces the same rows
  rather than re-dating them to the replay clock.
- NOT a caller's timestamp surface. ``completed_at``/``cancelled_at`` are owned
  by the status transition — set and cleared by it, never writable — which is
  why reopening a completed task *clears* its finish time instead of leaving a
  pending row claiming one.
- NOT an adopter. :func:`publish_tasks` refuses a store holding rows the journal
  has no authority for, rather than replacing them with a partial stream.
  :func:`seed_task_authority` is the explicit migration for those rows.
- NOT a dependency inferrer. ``blocked_by`` is preserved verbatim as data and is
  *not* minted into ``BLOCKS`` edges: an edge needs a projected node at both
  ends, and a legacy ``blocked_by`` entry is a free-form string, so converting
  one would turn unverifiable data into a dangling edge that invalidates the
  whole replay.

PRIVACY (§15)
-------------
Reports carry task IDs, node IDs, counts, statuses and bounded labels. A task
*body* is memory content and travels only in the event payload and the row it
projects to — never into a report, a warning or a log.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import Field, model_validator

from .graph_models import (
    Derivation,
    DerivationMethod,
    GraphModel,
    GraphNode,
    GraphScope,
    NodeKind,
    RelationType,
    Timestamp,
    make_node_id,
)
from .graph_write import (
    RelationAssertion,
    SemanticLifecycleRequest,
    SemanticWriteRequest,
)
from .models import TodoPriority, TodoStatus

__all__ = [
    "COMMANDS",
    "OPEN_TASK_STATUSES",
    "TASK_ACTIONS",
    "TASK_ACTION_ADD",
    "TASK_ACTION_DELETE",
    "TASK_ACTION_UPDATE",
    "TASK_ATTRIBUTE_KEY",
    "TASK_EVENT_NAMESPACE",
    "TASK_PRIORITIES",
    "TASK_SEED_NAMESPACE",
    "TASK_STATUSES",
    "TaskMutation",
    "TaskRecord",
    "main",
    "mutation_request",
    "new_task_id",
    "next_record",
    "publish_tasks",
    "seed_requests",
    "stored_task_ids",
    "stored_task_rows",
    "task_authority_ids",
    "task_node",
    "task_node_id",
    "task_records",
    "task_store_path",
]

#: The node-attribute key the task payload travels under. One key, so a task's
#: operational fields are a declared block rather than loose attributes a typo
#: could silently add to.
TASK_ATTRIBUTE_KEY = "task"

#: Identity namespace for the events a task mutation commits under. Separate
#: from the semantic-write namespace so a task decision and a knowledge decision
#: cannot collide on one request key.
TASK_EVENT_NAMESPACE = "task_mutation"

#: Identity namespace for the one bootstrap request that seeds pre-existing
#: tasks. Separate again, so a migrated task is distinguishable from a decided
#: one by its request identity alone.
TASK_SEED_NAMESPACE = "task_seed"

TASK_ACTION_ADD = "add"
TASK_ACTION_UPDATE = "update"
TASK_ACTION_DELETE = "delete"
TASK_ACTIONS: tuple[str, ...] = (
    TASK_ACTION_ADD,
    TASK_ACTION_UPDATE,
    TASK_ACTION_DELETE,
)

#: The caller-supplied enum-ish values, read from the existing vocabulary rather
#: than restated: a task's status and priority mean the same thing in the
#: journal as they do in the store the projection writes.
TASK_STATUSES: frozenset[str] = frozenset(status.value for status in TodoStatus)
TASK_PRIORITIES: frozenset[str] = frozenset(
    priority.value for priority in TodoPriority
)

#: The statuses that are not terminal. A transition into one of these clears
#: both terminal timestamps — that is what "reopened" means.
OPEN_TASK_STATUSES: tuple[str, ...] = (
    TodoStatus.PENDING.value,
    TodoStatus.IN_PROGRESS.value,
)

#: The fields an ``update`` may change. ``completed_at``/``cancelled_at`` are
#: deliberately absent: they belong to the status transition, and a caller that
#: could set them could fake a finish time.
TASK_UPDATABLE_FIELDS: tuple[str, ...] = (
    "status",
    "priority",
    "title",
    "content",
    "blocked_by",
    "notes",
)


def new_task_id() -> str:
    """A fresh task identity, in the shape the existing store already uses.

    The same ``uuid4`` the legacy ``TodoEntry`` default minted, kept because it
    is what every existing row, note and operator reference already holds. It is
    read *into* the event payload, so the identity a replay reproduces is the
    one that was decided — nothing here is re-minted on replay.
    """
    return str(uuid.uuid4())


def task_store_path(config: Any) -> Path:
    """Where the TODO projection is written for ``config``.

    One place, so the MCP entrances, the dashboard and the projection cannot
    disagree about which file the readable task view is.
    """
    return Path(config.home) / "data" / "todos.db"


#: The columns the seed reads back, named explicitly: ``SELECT *`` would make a
#: future store column silently part of the migration payload.
_STORED_TASK_COLUMNS: tuple[str, ...] = (
    "id",
    "domain",
    "title",
    "content",
    "blocked_by",
    "priority",
    "status",
    "source_session_id",
    "notes",
    "created_at",
    "updated_at",
    "completed_at",
    "cancelled_at",
)


def stored_task_rows(config: Any) -> list[dict[str, Any]]:
    """The readable store's task rows, read without creating or migrating it.

    A plain ``mode=ro`` connection rather than
    :class:`~layered_memory_mcp.todo_store.TodoStore`, on purpose: that class
    creates the database and runs its column migrations in ``__init__``, and the
    questions asked here — "whose tasks are these" and "what should the seed
    carry" — must not be answerable only by having brought the store into
    existence or changed its modification time.

    An absent store, or one without the table, is an empty list: there is
    nothing to adopt and nothing to seed.
    """
    import sqlite3

    path = task_store_path(config)
    if not path.is_file():
        return []
    columns = ", ".join(_STORED_TASK_COLUMNS)
    try:
        with sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True) as conn:
            rows = conn.execute(f"SELECT {columns} FROM todos").fetchall()
    except sqlite3.Error:
        return []
    return [dict(zip(_STORED_TASK_COLUMNS, row)) for row in rows]


def stored_task_ids(config: Any) -> set[str]:
    """The task ids the readable store holds, read without writing to it."""
    return {
        str(row["id"]) for row in stored_task_rows(config) if row.get("id")
    }


def task_node_id(task_id: str, version: int) -> str:
    """The node identity of one *version* of one task.

    Derived, never minted: the same ``(task_id, version)`` produces the same ID
    on every run and in every process, which is what makes a replanned mutation
    byte-identical and a replayed one a no-op (§10.2).
    """
    return make_node_id(NodeKind.TASK, task_id, f"v{int(version)}")


# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------

class TaskRecord(GraphModel):
    """One task, as the journal records it and as a reader receives it.

    Deliberately one model for both: the payload a ``TASK`` node carries *is*
    the row the projection writes, so there is no mapping layer between "what
    was decided" and "what ``list_todos`` shows" to drift.

    ``version`` counts the decisions that have touched this task.
    ``created_at`` is the add decision's time and never changes; ``updated_at``
    is this version's decision time. Both are event times, not projection times.
    """

    task_id: str
    domain: str
    content: str
    title: str = ""
    blocked_by: list[str] = Field(default_factory=list)
    priority: str = TodoPriority.MEDIUM.value
    status: str = TodoStatus.PENDING.value
    source_session_id: str | None = None
    notes: str = ""
    created_at: Timestamp
    updated_at: Timestamp
    completed_at: Timestamp | None = None
    cancelled_at: Timestamp | None = None
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _check_state(self) -> TaskRecord:
        if not self.task_id.strip():
            raise ValueError("a task needs a task_id")
        if self.priority not in TASK_PRIORITIES:
            raise ValueError(
                f"unknown task priority {self.priority!r}; expected one of "
                f"{sorted(TASK_PRIORITIES)}"
            )
        if self.status not in TASK_STATUSES:
            raise ValueError(
                f"unknown task status {self.status!r}; expected one of "
                f"{sorted(TASK_STATUSES)}"
            )
        return self

    @property
    def node_id(self) -> str:
        return task_node_id(self.task_id, self.version)

    @property
    def open(self) -> bool:
        return self.status in OPEN_TASK_STATUSES

    def row(self) -> dict[str, Any]:
        """The projected store row. ISO strings, exactly as the store holds."""
        return {
            "id": self.task_id,
            "domain": self.domain,
            "title": self.title,
            "content": self.content,
            "blocked_by": list(self.blocked_by),
            "priority": self.priority,
            "status": self.status,
            "source_session_id": self.source_session_id,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "cancelled_at": (
                self.cancelled_at.isoformat() if self.cancelled_at else None
            ),
        }


class TaskMutation(GraphModel):
    """One explicit task decision, validated before anything is opened.

    An ``update`` carries only the fields it changes: an unset field means
    "leave it", which is how the existing entrance already behaves. An unknown
    status or priority is refused here, before the journal — the same fail-closed
    rule the store learned in v3.4.6, moved one layer earlier so a refused
    mutation cannot leave a partial event behind either.
    """

    action: str
    occurred_at: Timestamp
    task_id: str = ""
    domain: str = ""
    content: str = ""
    title: str = ""
    priority: str = ""
    status: str = ""
    blocked_by: list[str] | None = None
    notes: str | None = None
    source_session_id: str | None = None

    @model_validator(mode="after")
    def _check_decision(self) -> TaskMutation:
        if self.action not in TASK_ACTIONS:
            raise ValueError(
                f"unknown task action {self.action!r}; expected one of "
                f"{list(TASK_ACTIONS)}"
            )
        if self.priority and self.priority not in TASK_PRIORITIES:
            raise ValueError(
                f"unknown task priority {self.priority!r}; expected one of "
                f"{sorted(TASK_PRIORITIES)}"
            )
        if self.status and self.status not in TASK_STATUSES:
            raise ValueError(
                f"unknown task status {self.status!r}; expected one of "
                f"{sorted(TASK_STATUSES)}"
            )
        if self.action == TASK_ACTION_ADD:
            if not self.domain.strip():
                raise ValueError("adding a task needs a domain")
            if not self.content.strip():
                raise ValueError("adding a task needs content")
            return self
        if not self.task_id.strip():
            raise ValueError(f"a task {self.action} needs the task_id to apply to")
        if self.action == TASK_ACTION_UPDATE and not self.changes:
            raise ValueError(
                "a task update must change at least one of: "
                + ", ".join(TASK_UPDATABLE_FIELDS)
            )
        return self

    @property
    def changes(self) -> dict[str, Any]:
        """The fields this update actually sets, as the store's own names.

        Blank strings are "unset", matching the existing entrance's contract;
        ``blocked_by`` and ``notes`` are distinguished by ``None``, so clearing
        a note to the empty string stays expressible.
        """
        changes: dict[str, Any] = {}
        for field in ("status", "priority", "title", "content"):
            value = getattr(self, field)
            if value:
                changes[field] = value
        if self.blocked_by is not None:
            changes["blocked_by"] = list(self.blocked_by)
        if self.notes is not None:
            changes["notes"] = self.notes
        return changes


# ---------------------------------------------------------------------------
# From a decision to the next record
# ---------------------------------------------------------------------------

def next_record(mutation: TaskMutation, current: TaskRecord | None) -> TaskRecord:
    """The record one mutation produces. Pure, and the only timestamp authority.

    ``mutation.occurred_at`` is the single clock: ``updated_at`` becomes the
    decision time, and a status transition stamps or clears the terminal
    timestamps from it. Nothing here reads the wall clock, so replaying the
    stream rebuilds the same rows rather than re-dating them.

    The transition table, spelled out because it is the contract:

    * ``completed`` — stamp ``completed_at``, clear ``cancelled_at``;
    * ``cancelled`` — stamp ``cancelled_at``, clear ``completed_at``;
    * ``pending``/``in_progress`` — clear **both**, because a reopened task that
      kept a finish time reads as "still open, and already done".
    """
    moment = mutation.occurred_at
    if current is None:
        if mutation.action != TASK_ACTION_ADD:  # pragma: no cover - guarded above
            raise ValueError(f"task {mutation.task_id!r} does not exist")
        task_id = mutation.task_id.strip() or new_task_id()
        return TaskRecord(
            task_id=task_id,
            domain=mutation.domain.strip(),
            content=mutation.content,
            title=mutation.title or mutation.content[:50],
            blocked_by=list(mutation.blocked_by or []),
            priority=mutation.priority or TodoPriority.MEDIUM.value,
            status=mutation.status or TodoStatus.PENDING.value,
            source_session_id=mutation.source_session_id or None,
            notes=mutation.notes or "",
            created_at=moment,
            updated_at=moment,
            version=1,
        )

    changes = mutation.changes
    updated = current.model_copy(
        update={
            **changes,
            "updated_at": moment,
            "version": current.version + 1,
        }
    )
    if "status" in changes:
        status = changes["status"]
        if status == TodoStatus.COMPLETED.value:
            updated = updated.model_copy(
                update={"completed_at": moment, "cancelled_at": None}
            )
        elif status == TodoStatus.CANCELLED.value:
            updated = updated.model_copy(
                update={"cancelled_at": moment, "completed_at": None}
            )
        else:
            updated = updated.model_copy(
                update={"completed_at": None, "cancelled_at": None}
            )
    return TaskRecord.model_validate(updated.to_dict())


def task_node(
    record: TaskRecord,
    *,
    recorded_at: datetime,
    method: DerivationMethod = DerivationMethod.USER_EXPLICIT,
    scope: GraphScope | None = None,
) -> GraphNode:
    """The ``TASK`` node one record is carried by.

    ``label`` is the task's title when that is usable as a handle and the
    ``task_id`` otherwise — a label may not wrap, and a legacy title is a
    truncated first line of free text that sometimes does. The title itself is
    preserved verbatim in the payload either way; nothing is repaired.
    """
    title = record.title.strip()
    usable = title and "\n" not in title and "\r" not in title
    return GraphNode(
        id=record.node_id,
        kind=NodeKind.TASK,
        label=title if usable else record.task_id,
        content=record.content,
        scope=scope if scope is not None else GraphScope(),
        recorded_at=recorded_at,
        derivation=Derivation(method=method),
        version=record.version,
        attributes={TASK_ATTRIBUTE_KEY: record.to_dict()},
    )


def record_from_node(node: GraphNode) -> TaskRecord | None:
    """The task a node carries, or ``None`` when it carries none.

    Forgiving in one direction only: a node that is not a task, or whose task
    block is not a task payload, projects to no task. It is never guessed at —
    a partial payload is no payload.
    """
    if node.kind is not NodeKind.TASK:
        return None
    payload = node.attributes.get(TASK_ATTRIBUTE_KEY)
    if not isinstance(payload, Mapping):
        return None
    try:
        return TaskRecord.model_validate(dict(payload))
    except Exception:  # noqa: BLE001 — an unreadable payload is not a task
        return None


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------

def task_records(nodes: Iterable[GraphNode]) -> dict[str, TaskRecord]:
    """``task_id -> current record`` for the replayed node set.

    *Current* in the graph's own sense: a node whose lifecycle status and
    validity window make it current. A retracted or superseded version is
    therefore absent — which is exactly how a deleted task leaves the readable
    view while its history stays in the journal.

    Among two current versions of one task (which an in-order commit cannot
    produce) the highest version wins, deterministically, rather than whichever
    node happened to be visited last.
    """
    current: dict[str, TaskRecord] = {}
    for node in nodes:
        record = record_from_node(node)
        if record is None or not node.is_current():
            continue
        held = current.get(record.task_id)
        if held is None or record.version > held.version:
            current[record.task_id] = record
    return current


def task_authority_ids(nodes: Iterable[GraphNode]) -> set[str]:
    """Every ``task_id`` the journal has authority for, current or not.

    Deliberately *including* retracted and superseded versions: the adoption
    guard asks "is this row one the authority knows about", and a task the
    authority deliberately deleted is still one it knows about. Comparing
    against the current set alone would read a correctly deleted task as an
    unknown foreign row.
    """
    known: set[str] = set()
    for node in nodes:
        record = record_from_node(node)
        if record is not None:
            known.add(record.task_id)
    return known


def publish_tasks(
    config: Any, nodes: Iterable[GraphNode]
) -> dict[str, Any]:
    """Project the current task state into the readable TODO store.

    Three properties, in order of how much they matter:

    1. **Nothing is adopted.** A store holding rows this journal has no
       authority for is refused (``todo_store_not_adopted:<count>``) instead of
       being replaced by a partial stream. Those rows are somebody's real tasks;
       :func:`seed_task_authority` is how they become the journal's.
    2. **Nothing is created for nothing.** With no task events in the stream,
       this writes nothing at all and does not bring the store into existence —
       an empty authority has no projection to publish, and creating an empty
       store would look like a task database somebody emptied.
    3. **The whole view is replaced, not edited.** The rows are the projection
       of the replayed state, written in one transaction, so a reader never sees
       a half-applied task view.

    Raises:
        ProjectionError: The store holds rows outside this journal's authority.
    """
    from .graph_projection import ProjectionError
    from .todo_store import TodoStore

    materialized = list(nodes)
    known = task_authority_ids(materialized)
    if not known:
        return {"tasks": 0, "projected": False, "reason": "no_task_authority"}

    path = task_store_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = TodoStore(path)
    unknown = sorted(set(store.ids()) - known)
    if unknown:
        raise ProjectionError(f"todo_store_not_adopted:{len(unknown)}")

    records = task_records(materialized)
    store.project([records[task_id].row() for task_id in sorted(records)])
    return {"tasks": len(records), "projected": True, "known": len(known)}


# ---------------------------------------------------------------------------
# From a decision to a journal request
# ---------------------------------------------------------------------------

def request_key(record: TaskRecord, *, action: str) -> str:
    """The stable retry identity of one task decision.

    ``<task id>:v<version>`` for a write and ``…:retract`` for a delete: the
    same decision retried is the same key (so a blind retry converges on its
    unfinished projections), and a *different* decision from the same version is
    the same key with a different payload — which the journal refuses. That
    refusal is the optimistic concurrency control for tasks: two callers both
    updating version 3 do not silently overwrite one another, the second is told
    its decision changed under one identity.
    """
    base = f"task:{record.task_id}:v{record.version}"
    return f"{base}:retract" if action == TASK_ACTION_DELETE else base


def mutation_request(
    mutation: TaskMutation, current: TaskRecord | None
) -> tuple[SemanticWriteRequest | SemanticLifecycleRequest, TaskRecord]:
    """The journal request one task mutation commits, and the record it means.

    An add is an injection; an update is a new version plus the ``SUPERSEDES``
    decision that retires the old one; a delete is a target-only retraction. The
    returned record is the state the mutation produces — for a delete, the
    record being retired, so the caller can report what it removed.

    Raises:
        ValueError: An update or delete whose task is not in ``current``.
    """
    if mutation.action == TASK_ACTION_DELETE:
        if current is None:
            raise ValueError(f"task {mutation.task_id!r} does not exist")
        return (
            SemanticLifecycleRequest(
                target_id=current.node_id,
                action="retract",
                occurred_at=mutation.occurred_at,
                request_key=request_key(current, action=TASK_ACTION_DELETE),
                event_namespace=TASK_EVENT_NAMESPACE,
            ),
            current,
        )

    if mutation.action == TASK_ACTION_UPDATE and current is None:
        raise ValueError(f"task {mutation.task_id!r} does not exist")
    if mutation.action == TASK_ACTION_ADD and current is not None:
        # Fail closed rather than quietly supersede: an ``add`` for an id the
        # authority already holds is a mistake, and turning it into an update
        # would edit a task the caller did not mean to touch.
        raise ValueError(f"task {mutation.task_id!r} already exists")

    record = next_record(mutation, current)
    node = task_node(record, recorded_at=mutation.occurred_at)
    supersedes: list[RelationAssertion] = []
    if current is not None:
        supersedes.append(
            RelationAssertion(
                relation_type=RelationType.SUPERSEDES,
                target_id=current.node_id,
                derivation=Derivation(method=DerivationMethod.USER_EXPLICIT),
            )
        )
    return (
        SemanticWriteRequest(
            node=node,
            occurred_at=mutation.occurred_at,
            request_key=request_key(record, action=mutation.action),
            event_namespace=TASK_EVENT_NAMESPACE,
            supersedes=supersedes,
        ),
        record,
    )


# ---------------------------------------------------------------------------
# Migration: pre-existing tasks become the journal's, verbatim
# ---------------------------------------------------------------------------

def seed_requests(
    rows: Sequence[Mapping[str, Any]], *, occurred_at: datetime
) -> list[SemanticWriteRequest]:
    """One injection per pre-existing task row, preserving it exactly.

    The honest half of the migration: every field is carried across as it was
    stored, including the timestamps, and the derivation is recorded as
    ``migrated`` so a field the original row never had cannot look observed. The
    seeded version is 1 — the migration is the task's first *decision*, not a
    claim about edits that happened before the journal existed.

    ``occurred_at`` is the migration's own instant and is used only as the event
    position; the record's ``created_at``/``updated_at`` stay the row's own.

    Raises:
        ValueError: A row that cannot be read as a task. A seed is all-or-
            nothing: a partially seeded authority would refuse the rows it
            skipped on the next publication, which is a worse outcome than
            refusing the migration.
    """
    requests: list[SemanticWriteRequest] = []
    for index, row in enumerate(rows):
        record = _record_from_row(row, index)
        node = task_node(
            record,
            recorded_at=record.created_at,
            method=DerivationMethod.MIGRATED,
        )
        requests.append(
            SemanticWriteRequest(
                node=node,
                occurred_at=occurred_at,
                request_key=f"task_seed:{record.task_id}",
                event_namespace=TASK_SEED_NAMESPACE,
            )
        )
    return requests


def _record_from_row(row: Mapping[str, Any], index: int) -> TaskRecord:
    """One stored task row as a record, refusing anything unreadable."""
    task_id = str(row.get("id") or "").strip()
    if not task_id:
        raise ValueError(f"task row {index} has no id")
    created = _timestamp(row.get("created_at"))
    if created is None:
        raise ValueError(f"task {task_id!r} has no readable created_at")
    updated = _timestamp(row.get("updated_at")) or created
    blocked = row.get("blocked_by")
    if isinstance(blocked, str):
        try:
            blocked = json.loads(blocked)
        except Exception:  # noqa: BLE001 — an unreadable list is not a list
            raise ValueError(
                f"task {task_id!r} has an unreadable blocked_by list"
            ) from None
    return TaskRecord(
        task_id=task_id,
        domain=str(row.get("domain") or ""),
        content=str(row.get("content") or ""),
        title=str(row.get("title") or ""),
        blocked_by=[str(item) for item in (blocked or [])],
        priority=str(row.get("priority") or TodoPriority.MEDIUM.value),
        status=str(row.get("status") or TodoStatus.PENDING.value),
        source_session_id=(
            str(row["source_session_id"]) if row.get("source_session_id") else None
        ),
        notes=str(row.get("notes") or ""),
        created_at=created,
        updated_at=updated,
        completed_at=_timestamp(row.get("completed_at")),
        cancelled_at=_timestamp(row.get("cancelled_at")),
        version=1,
    )


def _timestamp(value: Any) -> datetime | None:
    """A stored timestamp as a datetime, or ``None``. Never a guess."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The operator entrance for the migration
# ---------------------------------------------------------------------------

#: The CLI verbs. ``plan`` is the default because the seed is a migration of
#: somebody's real tasks, and a migration whose default verb writes is a
#: migration somebody will run by accident.
COMMANDS: tuple[str, ...] = ("plan", "apply")


def _parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m layered_memory_mcp.graph_task",
        description=(
            "Seed the semantic journal with the tasks a service home already "
            "holds, so the readable TODO view becomes a projection of the "
            "authority instead of a store with no authority behind it. The "
            "default verb is a read-only plan."
        ),
    )
    parser.add_argument("command", nargs="?", default="plan", choices=COMMANDS)
    parser.add_argument(
        "--home",
        required=True,
        metavar="PATH",
        help=(
            "The service home to read. Its configuration is resolved exactly as "
            "the service resolves it, so the seed lands in the generation the "
            "home is actually bound to — never a guessed location."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the task seed for one explicitly named home. JSON on stdout.

    Exit codes: ``0`` seeded or nothing to do, ``1`` refused, ``2`` usage.
    """
    import sys

    from .config import MemoryConfig
    from .graph_application import (
        TASK_SEED_APPLIED,
        TASK_SEED_NOTHING_TO_DO,
        TASK_SEED_PREVIEW,
        seed_task_authority,
    )

    arguments = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = MemoryConfig(home=arguments.home)
        report = seed_task_authority(
            config, apply=arguments.command == "apply"
        ).to_dict()
    except Exception as exc:  # noqa: BLE001 — bounded operator report (§15)
        report = {
            "status": "graph_task_seed_failed",
            "errors": [f"operation_failed:{type(exc).__name__}"],
        }
    report["command"] = arguments.command
    ok = report.get("status") in (
        TASK_SEED_APPLIED,
        TASK_SEED_NOTHING_TO_DO,
        TASK_SEED_PREVIEW,
    )
    report["exit_code"] = 0 if ok else 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(report["exit_code"])


if __name__ == "__main__":  # pragma: no cover - operator entrance
    raise SystemExit(main())

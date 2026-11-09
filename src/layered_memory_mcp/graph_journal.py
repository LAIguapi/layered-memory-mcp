"""The persistent, append-only semantic decision journal (§12 framework contract).

WHAT THIS MODULE IS
-------------------
The *authority* a graph write commits to. One embedded SQLite database holding
validated :class:`~layered_memory_mcp.graph_models.MemoryEvent` records in
append-only order, the request identity each one was committed under, and one
checkpoint per managed projection.

It answers four questions and no others:

* **what was decided** — ``append`` / :meth:`MemoryEventJournal.read_events`;
* **was this exact decision already recorded** — stable request identity plus a
  canonical payload hash, so an identical retry is a no-op and a *changed*
  retry under one identity is refused;
* **how far has each projection got** — :meth:`MemoryEventJournal.checkpoint` /
  :meth:`MemoryEventJournal.set_checkpoint`, which only ever move forward, and
  only when a caller says a projection actually succeeded;
* **which readable surface does a managed node own** — the surface registry,
  which is what lets a projection republish idempotently, detect external drift
  and retire a replaced surface;
* **what is a publication in the middle of doing** — the surface *intent*
  registry, which is how a crash of our own write is told apart from a human's
  edit. See "A PUBLICATION IS TWO PHASES" below.

WHAT IT IS NOT
--------------
- NOT a second editable knowledge database. Rows are appended, never updated or
  deleted; there is no ``UPDATE`` on ``memory_events`` and no delete anywhere.
- NOT the old changelog. ``MEMORY_CHANGELOG`` remains immutable historical
  *evidence* about file writes; this journal records explicit caller *semantic
  decisions*. Neither substitutes for the other, and nothing here writes to it.
- NOT a graph. It stores events; projecting them into nodes and edges is
  :mod:`~layered_memory_mcp.graph_sqlite_store`'s job, through ``rebuild``.
- NOT a discoverer. The database target is a constructor argument, always. There
  is no default path, no environment lookup and no directory creation.
- NOT a distributed transaction manager. One ``BEGIN IMMEDIATE`` serializes
  commits across connections and processes; it does **not** span the graph
  database, the vector database or the Markdown files. That is why the
  checkpoints exist, and why the application layer reports
  ``projections_pending`` instead of claiming an all-store transaction.

TWO OPEN PATHS, DELIBERATELY SEPARATE
-------------------------------------
The ordinary constructor is the *writable* path: it creates the database if the
declared file is absent, creates the schema, the metadata rows and one
checkpoint row per projection, and does all of that inside a ``BEGIN
IMMEDIATE``. That is right for a writer — a commit must find an authority ready
to take it — and wrong for a diagnostic.

:meth:`MemoryEventJournal.open_readonly` is the diagnostic path. It opens an
*already existing* database through a SQLite ``mode=ro`` URI and then only
verifies what it found: no file is created, no schema, no metadata row, no
checkpoint row and no write transaction, so a status view cannot bring the
authority into existence — or alter its modification time — merely by looking
at it. A missing database is :class:`JournalMissingError`, an existing file
that is not a journal of this layout is
:class:`JournalNotInitializedError`, and every mutating method on such a handle
refuses with :class:`JournalReadOnlyError` rather than discovering the problem
in SQLite.

A PUBLICATION IS TWO PHASES
---------------------------
``projection_surfaces`` records what a surface *last published*; it is the whole
drift test, and it may therefore only ever describe bytes that actually reached
disk. Recording it before the write was a real defect: a failed file write left
the row describing a body that never landed, and every later recovery attempt
compared the file against that phantom hash and reported ``surface_drift``
forever — our own crash, permanently misclassified as somebody else's edit.

So a publication declares its *intent* first, in ``projection_surface_intents``,
writes the file, and only then finalizes the registry and clears the intent.
That makes both crash windows recoverable without a forced republication:

* crashed **before** the file was replaced — the registry still describes the
  body the file still holds, so the retry is an ordinary republication;
* crashed **after** the replacement, before finalization — the file matches the
  recorded intent, which is recognizable as our own unfinished write.

An intent is *coordination* state, not a second semantic truth: it says what a
projection was about to do, never what was decided. The events stay the
authority, an intent is derivable from them plus a checkpoint, and nothing reads
an intent to answer a question about memory content.

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Monotonic positions.** ``position`` is a single ``AUTOINCREMENT`` column, so
   replay order is total, stable and independent of timestamps.
2. **One canonical payload per event identity.** An event ID may be replayed
   verbatim in a later request (a no-op); carrying a *different* payload under
   it is :class:`JournalConflictError`, and nothing is appended.
3. **One canonical payload per request identity.** Same rule, at request
   granularity, which is what makes a retry safe to issue blindly.
4. **Serialized commits.** ``BEGIN IMMEDIATE`` plus a busy timeout: two writers
   on independent connections queue, they do not interleave.
5. **Checkpoints never lie.** A checkpoint can only be set forward and never
   past the head; a failure is recorded as a *status*, leaving the position
   where it was.

PRIVACY (§15)
-------------
:meth:`MemoryEventJournal.health` and :class:`Checkpoint` carry counts, statuses,
positions and bounded labels — never a database path, a node body or a backend
message. The stored payloads are memory content by construction; the reports
about them are not.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from pydantic import Field

from .graph_models import GraphModel, MemoryEvent

__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "PROJECTION_GRAPH",
    "PROJECTION_L0",
    "PROJECTION_L1",
    "PROJECTION_NAMES",
    "PROJECTION_TODO",
    "PROJECTION_VECTOR",
    "REQUEST_KIND_BOOTSTRAP",
    "REQUEST_KIND_SEMANTIC_WRITE",
    "SURFACE_INTENT_PUBLISH",
    "SURFACE_INTENT_RETIRE",
    "AppendResult",
    "Checkpoint",
    "JournalClosedError",
    "JournalConflictError",
    "JournalError",
    "JournalHealth",
    "JournalMissingError",
    "JournalNotInitializedError",
    "JournalReadOnlyError",
    "JournalSchemaError",
    "JournalUnavailableError",
    "MemoryEventJournal",
    "SurfaceIntent",
    "SurfaceRecord",
    "canonical_request_hash",
]

#: Version of the physical journal layout. Recorded and checked; never migrated.
JOURNAL_SCHEMA_VERSION = 1

#: The managed projections, in publication order. The order matters: the graph
#: is the semantic projection every other one is derived from, the readable L1
#: surface is derived from the graph, the vector rows are derived from L1, and
#: L0 is derived from the L1 files as a whole. The readable TODO view is derived
#: from the graph and from nothing else, which is why a refused readable surface
#: does not hold it up — see ``graph_application`` for the dependency table.
PROJECTION_GRAPH = "graph"
PROJECTION_L1 = "l1"
PROJECTION_VECTOR = "vector"
PROJECTION_L0 = "l0"
PROJECTION_TODO = "todo"
PROJECTION_NAMES: tuple[str, ...] = (
    PROJECTION_GRAPH,
    PROJECTION_L1,
    PROJECTION_VECTOR,
    PROJECTION_L0,
    PROJECTION_TODO,
)

#: What a declared surface intent is about to do. ``publish`` renders the
#: section with the declared body; ``retire`` removes it from the rendering.
SURFACE_INTENT_PUBLISH = "publish"
SURFACE_INTENT_RETIRE = "retire"

#: The two kinds of commit this journal records. A bootstrap import is part of
#: the same replayed stream — an imported snapshot that is *not* represented
#: here would be erased by the next replacing rebuild.
REQUEST_KIND_SEMANTIC_WRITE = "semantic_write"
REQUEST_KIND_BOOTSTRAP = "bootstrap_import"

#: How long a writer waits for another writer's ``BEGIN IMMEDIATE`` before it
#: gives up. Serialization, not a performance tuning knob.
_BUSY_TIMEOUT_SECONDS = 30.0

_META_SCHEMA_VERSION = "journal_schema_version"
_META_CREATED_AT = "created_at"

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS journal_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_requests (
        request_id    TEXT PRIMARY KEY,
        request_hash  TEXT NOT NULL,
        kind          TEXT NOT NULL,
        committed_at  TEXT NOT NULL,
        event_count   INTEGER NOT NULL,
        first_position INTEGER NOT NULL,
        last_position  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_events (
        position     INTEGER PRIMARY KEY AUTOINCREMENT,
        id           TEXT NOT NULL UNIQUE,
        request_id   TEXT NOT NULL,
        kind         TEXT NOT NULL,
        occurred_at  TEXT NOT NULL,
        payload      TEXT NOT NULL,
        payload_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projection_checkpoints (
        name       TEXT PRIMARY KEY,
        position   INTEGER NOT NULL,
        status     TEXT NOT NULL,
        detail     TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projection_surfaces (
        entry_id    TEXT PRIMARY KEY,
        domain      TEXT NOT NULL,
        section     TEXT NOT NULL,
        occurrence  INTEGER NOT NULL,
        node_id     TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        status      TEXT NOT NULL,
        position    INTEGER NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'l1_markdown_section',
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projection_surface_intents (
        entry_id    TEXT PRIMARY KEY,
        domain      TEXT NOT NULL,
        section     TEXT NOT NULL,
        occurrence  INTEGER NOT NULL,
        node_id     TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        action      TEXT NOT NULL,
        position    INTEGER NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'l1_markdown_section',
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS memory_events_request ON memory_events (request_id)",
    "CREATE INDEX IF NOT EXISTS projection_surfaces_node ON projection_surfaces (node_id)",
)

#: The tables a database must already hold to be inspected read-only. Stated
#: explicitly rather than parsed back out of :data:`_SCHEMA_STATEMENTS`: the
#: read-only path must know what it requires without being able to create it.
_REQUIRED_TABLES: tuple[str, ...] = (
    "journal_meta",
    "memory_events",
    "memory_requests",
    "projection_checkpoints",
    "projection_surfaces",
)

#: Tables added to this layout *after* the first journals were written. They are
#: created by the writable path like every other table, and their absence is
#: deliberately **not** an uninitialized journal: an older journal holds no
#: publication intents, and "no publication was in flight" is the complete and
#: truthful answer for one, not a missing capability. So the readers below report
#: an absent table as an empty result instead of raising, and the read-only
#: inspection path keeps working on a journal written before they existed.
_OPTIONAL_TABLES: tuple[str, ...] = ("projection_surface_intents",)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

class JournalError(RuntimeError):
    """Base class for every refusal this module raises."""


class JournalUnavailableError(JournalError):
    """The journal database could not be opened or read at all."""


class JournalMissingError(JournalUnavailableError):
    """A read-only inspection was asked for a database that does not exist.

    A refusal rather than an empty answer, and deliberately *not* a creation:
    the writable path is the only thing allowed to bring a journal into being,
    so "there is no authority at this location" stays visible instead of being
    quietly fixed by whoever looked first.
    """


class JournalNotInitializedError(JournalUnavailableError):
    """The file exists but holds no journal of this layout.

    Distinct from :class:`JournalMissingError` because the two need different
    operator actions, and distinct from :class:`JournalSchemaError` because
    nothing claims a version here: there is simply nothing to read yet.
    """


class JournalReadOnlyError(JournalError):
    """A mutating operation was attempted on a read-only inspection handle."""


class JournalSchemaError(JournalError):
    """The database was written by a layout this process does not speak."""


class JournalClosedError(JournalError):
    """An operation was attempted on a journal whose lifecycle has ended."""


class JournalConflictError(JournalError):
    """A request or event identity was reused with a *different* payload.

    The one refusal that makes a blind retry safe: the caller learns its second
    attempt was not the same decision, and nothing was appended.
    """


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

class AppendResult(GraphModel):
    """What one :meth:`MemoryEventJournal.append` did.

    ``duplicate`` is the honest answer to "was this already here": the positions
    then describe the *first* commit, not this call, which is what lets a caller
    resume that commit's unfinished projections instead of re-deciding.
    """

    request_id: str
    kind: str = REQUEST_KIND_SEMANTIC_WRITE
    duplicate: bool = False
    events_appended: int = Field(default=0, ge=0)
    first_position: int = Field(default=0, ge=0)
    last_position: int = Field(default=0, ge=0)
    event_ids: list[str] = Field(default_factory=list)


class Checkpoint(GraphModel):
    """How far one managed projection has published, and whether it is well."""

    name: str
    position: int = Field(default=0, ge=0)
    status: str = "pending"
    detail: str = ""


class SurfaceRecord(GraphModel):
    """One readable L1 surface a managed graph node owns.

    ``entry_id`` is the versioned projection identity from
    :mod:`~layered_memory_mcp.graph_entry_identity` — the same ID the vector row
    for that section carries — so "which node is this searchable row" is an
    exact lookup rather than a text match.

    ``body_sha256`` is what the projection last *published*. It is the whole
    drift test: a surface whose on-disk body no longer hashes to this was
    changed by somebody else, and republishing over it would silently adopt an
    external write.
    """

    entry_id: str
    domain: str
    section: str
    occurrence: int = Field(default=0, ge=0)
    node_id: str
    body_sha256: str
    status: str = "published"
    position: int = Field(default=0, ge=0)
    kind: str = "l1_markdown_section"


class SurfaceIntent(GraphModel):
    """What one publication is *about* to do to one readable surface.

    The missing half of the drift test. :class:`SurfaceRecord` answers "what did
    we last publish here", which may only ever describe bytes that landed;
    this answers "what were we in the middle of writing", which is the only way
    a crash of our own write can be told apart from an external edit.

    ``body_sha256`` is the fingerprint of the body the publication *intends* to
    write (empty for a retirement, which intends the section's absence).
    ``action`` is :data:`SURFACE_INTENT_PUBLISH` or
    :data:`SURFACE_INTENT_RETIRE`.

    An intent is cleared the moment its publication finalizes, so a surface with
    an intent recorded is, by construction, one whose last publication did not
    finish. It carries no decision, and nothing projects memory content from it.
    """

    entry_id: str
    domain: str
    section: str
    occurrence: int = Field(default=0, ge=0)
    node_id: str
    body_sha256: str = ""
    action: str = SURFACE_INTENT_PUBLISH
    position: int = Field(default=0, ge=0)
    kind: str = "l1_markdown_section"


class JournalHealth(GraphModel):
    """Bounded state of the journal: counts, positions, checkpoints, errors."""

    schema_version: int = JOURNAL_SCHEMA_VERSION
    event_count: int = Field(default=0, ge=0)
    request_count: int = Field(default=0, ge=0)
    head_position: int = Field(default=0, ge=0)
    surface_count: int = Field(default=0, ge=0)
    #: Surfaces whose last publication declared an intent and never finalized
    #: it. Normally zero; a nonzero count is a publication that was interrupted,
    #: which :func:`~layered_memory_mcp.graph_application.recover_projections`
    #: finishes.
    unfinished_surface_count: int = Field(default=0, ge=0)
    checkpoints: dict[str, int] = Field(default_factory=dict)
    checkpoint_statuses: dict[str, str] = Field(default_factory=dict)
    pending_projections: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.errors


def canonical_request_hash(events: Sequence[MemoryEvent]) -> str:
    """The identity of one commit's payload, independent of declaration order.

    Events are hashed in ledger order — ``(occurred_at, id)``, the order a
    rebuild applies them in — over their canonical, key-sorted JSON. Two calls
    that mean the same thing therefore hash the same, and a single changed
    character in any payload changes the hash.
    """
    ordered = sorted(events, key=lambda event: (event.occurred_at, event.id))
    digest = hashlib.sha256()
    for event in ordered:
        digest.update(event.to_json().encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def body_sha256(body: str) -> str:
    """The published-body fingerprint the surface registry compares against."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------

class MemoryEventJournal:
    """One embedded SQLite append-only journal of semantic decisions.

    ``database`` is required and explicit — a path, the literal ``":memory:"``,
    or an already-open :class:`sqlite3.Connection` the caller owns. A path whose
    parent directory does not exist is refused rather than created, matching
    :class:`~layered_memory_mcp.graph_sqlite_store.SQLiteGraphStore`: creating
    directories for a database is how an import ends up writing into a home
    directory nobody named.

    ``read_only`` selects the inspection path described in the module docstring
    and is **not** a convenience flag on the writable one: use
    :meth:`open_readonly`, which is the named entrance for it.
    """

    __slots__ = (
        "_closed",
        "_connection",
        "_owns_connection",
        "_read_only",
        "_restore_isolation",
    )

    def __init__(
        self,
        database: str | Path | sqlite3.Connection,
        *,
        read_only: bool = False,
    ) -> None:
        self._closed = False
        self._read_only = read_only
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self._restore_isolation: str | None = None
        if read_only:
            self._connection = self._open_read_only(database)
        else:
            self._connection = self._open(database)
        try:
            if read_only:
                self._verify_schema()
            else:
                self._initialize_schema()
        except BaseException:
            self.close()
            raise

    @classmethod
    def open_readonly(cls, database: str | Path) -> MemoryEventJournal:
        """Open an existing journal for inspection only.

        The entrance every diagnostic should use: it reads, it never creates and
        it never writes. See the module docstring for why that is a separate
        path rather than a mode of the writable one.

        Args:
            database: The declared journal file. It must already exist and
                already hold this layout; a
                :class:`sqlite3.Connection` or ``":memory:"`` is refused,
                because neither can be verified to be read-only.

        Raises:
            JournalMissingError: The file (or its parent directory) is absent.
            JournalNotInitializedError: The file exists and is not a journal.
            JournalSchemaError: The journal declares a layout version this
                process does not speak.
            JournalUnavailableError: The database could not be opened or read.
        """
        return cls(database, read_only=True)

    # -- lifecycle -----------------------------------------------------------

    def _open(self, database: str | Path | sqlite3.Connection) -> sqlite3.Connection:
        if isinstance(database, sqlite3.Connection):
            self._restore_isolation = database.isolation_level
            database.isolation_level = None
            return database

        target = str(database)
        if target != ":memory:":
            resolved = Path(target).expanduser()
            if not resolved.parent.is_dir():
                raise JournalUnavailableError(
                    f"directory {str(resolved.parent)!r} does not exist; the "
                    "journal never creates directories — create the target "
                    "explicitly."
                )
            target = str(resolved)
        try:
            return sqlite3.connect(
                target, isolation_level=None, timeout=_BUSY_TIMEOUT_SECONDS
            )
        except sqlite3.Error as exc:
            raise JournalUnavailableError(
                f"could not open the memory journal: {type(exc).__name__}"
            ) from exc

    def _open_read_only(
        self, database: str | Path | sqlite3.Connection
    ) -> sqlite3.Connection:
        """Open an existing journal file through a SQLite ``mode=ro`` URI.

        Three explicit refusals before SQLite is asked for anything — a handle
        that cannot be proven read-only, an absent parent, an absent file — so
        "it does not exist" never arrives as a generic open failure, and the
        ``mode=ro`` URI is never the only thing standing between a diagnostic
        and a created database. ``query_only`` then makes the connection refuse
        a write even if some future statement tried one.
        """
        if isinstance(database, sqlite3.Connection):
            raise JournalReadOnlyError(
                "a read-only inspection needs its own connection to a journal "
                "file; a caller-owned connection cannot be shown to be "
                "read-only."
            )
        target = str(database)
        if target == ":memory:":
            raise JournalMissingError(
                "there is no existing journal to inspect in an in-memory "
                "database; read-only inspection needs a file."
            )
        resolved = Path(target).expanduser().absolute()
        if not resolved.parent.is_dir() or not resolved.is_file():
            raise JournalMissingError(
                "the declared memory journal does not exist; a read-only "
                "inspection reports that rather than creating one."
            )
        try:
            connection = sqlite3.connect(
                f"{resolved.as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
                timeout=_BUSY_TIMEOUT_SECONDS,
            )
        except sqlite3.Error as exc:
            raise JournalUnavailableError(
                f"could not open the memory journal: {type(exc).__name__}"
            ) from exc
        try:
            connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            connection.close()
            raise JournalUnavailableError(
                f"could not open the memory journal: {type(exc).__name__}"
            ) from exc
        return connection

    def close(self) -> None:
        """End this journal's lifecycle. Idempotent, and writes nothing."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._owns_connection:
                self._connection.close()
            else:
                self._connection.isolation_level = self._restore_isolation
        except sqlite3.Error:
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def read_only(self) -> bool:
        """Whether this handle was opened for inspection only."""
        return self._read_only

    def __enter__(self) -> MemoryEventJournal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise JournalClosedError(
                "this memory journal has been closed; open a new "
                "MemoryEventJournal on the same database to use it again."
            )
        return self._connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One explicit write boundary: ``BEGIN IMMEDIATE`` … ``COMMIT``.

        ``IMMEDIATE`` takes the write lock up front, so two writers serialize
        rather than discovering the conflict at ``COMMIT``. Any exception rolls
        the whole statement group back, which is what makes a refused append
        leave nothing behind.

        An inspection handle never reaches the ``BEGIN``: it is refused here,
        once, so no mutating method has to remember to check.
        """
        connection = self._require_connection()
        if self._read_only:
            raise JournalReadOnlyError(
                "this memory journal was opened for read-only inspection; open "
                "a writable MemoryEventJournal on the same database to commit."
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def _initialize_schema(self) -> None:
        with self._transaction() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            stored = self._read_meta(connection, _META_SCHEMA_VERSION)
            if stored is not None and stored != str(JOURNAL_SCHEMA_VERSION):
                raise JournalSchemaError(
                    f"journal was written with schema version {stored}, this "
                    f"process speaks {JOURNAL_SCHEMA_VERSION}; refusing to read "
                    "it rather than guessing at the layout."
                )
            for table in ("projection_surfaces", "projection_surface_intents"):
                if self._surface_kind_column(connection, table) != "kind":
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN kind TEXT NOT NULL "
                        "DEFAULT 'l1_markdown_section'"
                    )
            for key, value in (
                (_META_SCHEMA_VERSION, str(JOURNAL_SCHEMA_VERSION)),
                (_META_CREATED_AT, _now()),
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO journal_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
            for name in PROJECTION_NAMES:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO projection_checkpoints
                        (name, position, status, detail, updated_at)
                    VALUES (?, 0, 'pending', '', ?)
                    """,
                    (name, _now()),
                )

    def _verify_schema(self) -> None:
        """Check — never establish — that this database is a journal we read.

        The read-only counterpart of :meth:`_initialize_schema`: the same two
        questions (is the layout here, is it the version this process speaks),
        asked with ``SELECT`` only. A missing table is reported as an
        uninitialized journal instead of being created, because creating it is
        exactly what an inspection must not do.
        """
        connection = self._connection
        try:
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        except sqlite3.Error as exc:
            raise JournalUnavailableError(
                f"could not read the memory journal layout: {type(exc).__name__}"
            ) from exc

        absent = sorted(set(_REQUIRED_TABLES) - present)
        if absent:
            raise JournalNotInitializedError(
                f"this database holds no memory journal (missing: "
                f"{', '.join(absent)}); a read-only inspection reports that "
                "rather than creating the schema."
            )

        stored = self._read_meta(connection, _META_SCHEMA_VERSION)
        if stored is not None and stored != str(JOURNAL_SCHEMA_VERSION):
            raise JournalSchemaError(
                f"journal was written with schema version {stored}, this "
                f"process speaks {JOURNAL_SCHEMA_VERSION}; refusing to read "
                "it rather than guessing at the layout."
            )

    @staticmethod
    def _read_meta(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute(
            "SELECT value FROM journal_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    # -- append --------------------------------------------------------------

    def append(
        self,
        request_id: str,
        events: Sequence[MemoryEvent],
        *,
        kind: str = REQUEST_KIND_SEMANTIC_WRITE,
    ) -> AppendResult:
        """Commit one decision's events under one stable request identity.

        Three outcomes, and "partially appended" is not among them:

        * **new** — every event is appended in ledger order and the request is
          recorded with its canonical payload hash;
        * **duplicate** — the request identity is present with the *same*
          payload hash. Nothing is appended and the first commit's positions
          come back, so the caller can resume its unfinished projections;
        * **conflict** — the request identity (or any event identity) is present
          with a *different* payload. :class:`JournalConflictError`, and the
          transaction rolls back, so nothing partial survives.

        An event identity replayed verbatim is skipped rather than refused: a
        caller re-submitting a superset of an earlier decision is replaying, not
        contradicting.
        """
        ordered = sorted(events, key=lambda event: (event.occurred_at, event.id))
        if not ordered:
            raise ValueError("a journal commit needs at least one memory event")
        identity = request_id.strip()
        if not identity:
            raise ValueError("a journal commit needs a non-blank request identity")
        request_hash = canonical_request_hash(ordered)

        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT request_hash, kind, event_count, first_position, last_position
                FROM memory_requests WHERE request_id = ?
                """,
                (identity,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != request_hash:
                    raise JournalConflictError(
                        f"request {identity!r} was already committed with a "
                        "different payload; a retry must carry the same "
                        "decision, and a changed decision needs a new request."
                    )
                return AppendResult(
                    request_id=identity,
                    kind=str(existing[1]),
                    duplicate=True,
                    events_appended=0,
                    first_position=int(existing[3]),
                    last_position=int(existing[4]),
                    event_ids=self._event_ids_for(connection, identity),
                )

            appended: list[str] = []
            positions: list[int] = []
            for event in ordered:
                payload = event.to_json()
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                prior = connection.execute(
                    "SELECT position, payload_hash FROM memory_events WHERE id = ?",
                    (event.id,),
                ).fetchone()
                if prior is not None:
                    if str(prior[1]) != digest:
                        raise JournalConflictError(
                            f"event {event.id!r} is already recorded with a "
                            "different payload; one event identity carries one "
                            "canonical decision."
                        )
                    positions.append(int(prior[0]))
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO memory_events
                        (id, request_id, kind, occurred_at, payload, payload_hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        identity,
                        event.kind.value,
                        event.occurred_at.isoformat(),
                        payload,
                        digest,
                    ),
                )
                appended.append(event.id)
                positions.append(int(cursor.lastrowid))

            connection.execute(
                """
                INSERT INTO memory_requests
                    (request_id, request_hash, kind, committed_at, event_count,
                     first_position, last_position)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    request_hash,
                    kind,
                    _now(),
                    len(ordered),
                    min(positions),
                    max(positions),
                ),
            )
            return AppendResult(
                request_id=identity,
                kind=kind,
                duplicate=False,
                events_appended=len(appended),
                first_position=min(positions),
                last_position=max(positions),
                event_ids=[event.id for event in ordered],
            )

    @staticmethod
    def _event_ids_for(connection: sqlite3.Connection, request_id: str) -> list[str]:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM memory_events WHERE request_id = ? ORDER BY position",
                (request_id,),
            ).fetchall()
        ]

    def read_request_events(self, request_id: str) -> list[MemoryEvent]:
        """Read the events committed under one request identity."""
        connection = self._require_connection()
        return [
            self._decode(str(event_id), str(payload))
            for event_id, payload in connection.execute(
                """
                SELECT id, payload
                FROM memory_events
                WHERE request_id = ?
                ORDER BY position
                """,
                (request_id.strip(),),
            ).fetchall()
        ]

    # -- read ----------------------------------------------------------------

    def read_records(self, *, upto: int | None = None) -> list[tuple[int, MemoryEvent]]:
        """``(position, event)`` pairs in append order, up to ``upto``.

        The positional view, for the callers that need to know *which* part of
        the stream a projection has not published yet.
        """
        connection = self._require_connection()
        query = "SELECT position, id, payload FROM memory_events"
        parameters: tuple[Any, ...] = ()
        if upto is not None:
            query += " WHERE position <= ?"
            parameters = (int(upto),)
        query += " ORDER BY position"
        records: list[tuple[int, MemoryEvent]] = []
        for position, event_id, payload in connection.execute(query, parameters):
            records.append((int(position), self._decode(str(event_id), str(payload))))
        return records

    def read_events(self, *, upto: int | None = None) -> list[MemoryEvent]:
        """The complete replayable stream, in append order."""
        return [event for _position, event in self.read_records(upto=upto)]

    @staticmethod
    def _decode(event_id: str, payload: str) -> MemoryEvent:
        try:
            return MemoryEvent.model_validate(json.loads(payload))
        except Exception as exc:
            raise JournalUnavailableError(
                f"stored memory event {event_id!r} did not re-validate: "
                f"{type(exc).__name__}"
            ) from exc

    def head_position(self) -> int:
        """The highest committed position, or ``0`` for an empty journal."""
        connection = self._require_connection()
        row = connection.execute("SELECT MAX(position) FROM memory_events").fetchone()
        return int(row[0] or 0)

    def event_count(self) -> int:
        connection = self._require_connection()
        return int(
            connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        )

    def request_count(self) -> int:
        connection = self._require_connection()
        return int(
            connection.execute("SELECT COUNT(*) FROM memory_requests").fetchone()[0]
        )

    def request_hash(self, request_id: str) -> str | None:
        """The canonical payload hash recorded for ``request_id``, or ``None``.

        How a caller asks "is this the same decision I already committed?"
        *before* attempting the append — so an identical retry can go straight
        to resuming its unfinished projections instead of being re-judged.
        """
        connection = self._require_connection()
        row = connection.execute(
            "SELECT request_hash FROM memory_requests WHERE request_id = ?",
            (request_id.strip(),),
        ).fetchone()
        return None if row is None else str(row[0])

    def request_kinds(self) -> dict[str, int]:
        """How many commits of each kind the journal holds."""
        connection = self._require_connection()
        return {
            str(kind): int(count)
            for kind, count in connection.execute(
                "SELECT kind, COUNT(*) FROM memory_requests GROUP BY kind ORDER BY kind"
            ).fetchall()
        }

    # -- checkpoints ---------------------------------------------------------

    def checkpoint(self, name: str) -> Checkpoint:
        """How far ``name`` has published. An unknown name is refused."""
        self._require_projection(name)
        connection = self._require_connection()
        row = connection.execute(
            "SELECT position, status, detail FROM projection_checkpoints WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:  # pragma: no cover - the schema seeds every name
            return Checkpoint(name=name)
        return Checkpoint(
            name=name, position=int(row[0]), status=str(row[1]), detail=str(row[2])
        )

    def set_checkpoint(
        self, name: str, position: int, *, status: str = "ok", detail: str = ""
    ) -> Checkpoint:
        """Advance ``name`` to ``position``. Forward only, never past the head.

        A checkpoint is a published claim about durable state, so it may only
        move after the projection it describes actually succeeded. Moving it
        backwards, or past a position the journal has not committed, is a caller
        mistake rather than a value to clamp.
        """
        self._require_projection(name)
        with self._transaction() as connection:
            head = int(
                connection.execute(
                    "SELECT MAX(position) FROM memory_events"
                ).fetchone()[0]
                or 0
            )
            if position > head:
                raise ValueError(
                    f"checkpoint {name!r} cannot pass the journal head {head}"
                )
            row = connection.execute(
                "SELECT position FROM projection_checkpoints WHERE name = ?", (name,)
            ).fetchone()
            current = int(row[0]) if row is not None else 0
            if position < current:
                raise ValueError(
                    f"checkpoint {name!r} may not move backwards "
                    f"({current} → {position})"
                )
            connection.execute(
                """
                INSERT INTO projection_checkpoints
                    (name, position, status, detail, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    position = excluded.position,
                    status = excluded.status,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (name, int(position), status, detail, _now()),
            )
        return Checkpoint(name=name, position=int(position), status=status, detail=detail)

    def record_projection_failure(self, name: str, detail: str) -> Checkpoint:
        """Record that ``name`` failed, leaving its position exactly where it is.

        The honest half of §10.3: a projection that did not publish must not
        look published, and a failure that moved a checkpoint would be
        indistinguishable from a success.
        """
        self._require_projection(name)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT position FROM projection_checkpoints WHERE name = ?", (name,)
            ).fetchone()
            current = int(row[0]) if row is not None else 0
            connection.execute(
                """
                INSERT INTO projection_checkpoints
                    (name, position, status, detail, updated_at)
                VALUES (?, ?, 'failed', ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    status = 'failed',
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (name, current, detail, _now()),
            )
        return Checkpoint(name=name, position=current, status="failed", detail=detail)

    def pending_projections(self) -> list[str]:
        """Projection names whose checkpoint is behind the committed head."""
        head = self.head_position()
        return [
            name
            for name in PROJECTION_NAMES
            if self.checkpoint(name).position < head
        ]

    @staticmethod
    def _require_projection(name: str) -> None:
        if name not in PROJECTION_NAMES:
            raise ValueError(
                f"unknown projection {name!r}; expected one of "
                f"{', '.join(PROJECTION_NAMES)}"
            )

    # -- managed surfaces ----------------------------------------------------

    def record_surface(self, record: SurfaceRecord) -> SurfaceRecord:
        """Record (or replace) what a managed node currently publishes."""
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO projection_surfaces
                    (entry_id, domain, section, occurrence, node_id, body_sha256,
                     status, position, kind, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id) DO UPDATE SET
                    domain = excluded.domain,
                    section = excluded.section,
                    occurrence = excluded.occurrence,
                    node_id = excluded.node_id,
                    body_sha256 = excluded.body_sha256,
                    status = excluded.status,
                    position = excluded.position,
                    kind = excluded.kind,
                    updated_at = excluded.updated_at
                """,
                (
                    record.entry_id,
                    record.domain,
                    record.section,
                    int(record.occurrence),
                    record.node_id,
                    record.body_sha256,
                    record.status,
                    int(record.position),
                    record.kind,
                    _now(),
                ),
            )
        return record

    def retire_surface(self, entry_id: str, *, position: int = 0) -> None:
        """Mark a surface retired. The row stays: history is not deleted."""
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE projection_surfaces
                SET status = 'retired', position = ?, updated_at = ?
                WHERE entry_id = ?
                """,
                (int(position), _now(), entry_id),
            )

    def surface(self, entry_id: str) -> SurfaceRecord | None:
        connection = self._require_connection()
        kind = self._surface_kind_column(connection, "projection_surfaces")
        row = connection.execute(
            f"""
            SELECT entry_id, domain, section, occurrence, node_id, body_sha256,
                   status, position, {kind}
            FROM projection_surfaces WHERE entry_id = ?
            """,
            (entry_id,),
        ).fetchone()
        return None if row is None else _surface_from_row(row)

    def surfaces(self, *, include_retired: bool = True) -> list[SurfaceRecord]:
        connection = self._require_connection()
        kind = self._surface_kind_column(connection, "projection_surfaces")
        query = f"""
            SELECT entry_id, domain, section, occurrence, node_id, body_sha256,
                   status, position, {kind}
            FROM projection_surfaces
        """
        if not include_retired:
            query += " WHERE status != 'retired'"
        query += " ORDER BY entry_id"
        return [_surface_from_row(row) for row in connection.execute(query).fetchall()]

    # -- publication intents -------------------------------------------------

    def record_surface_intent(self, intent: SurfaceIntent) -> SurfaceIntent:
        """Declare what a publication is about to write to one surface.

        Recorded *before* the bytes exist, which is exactly what distinguishes
        it from :meth:`record_surface`: an intent is allowed to describe a body
        that is not on disk yet, and the published record is not.
        """
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO projection_surface_intents
                    (entry_id, domain, section, occurrence, node_id, body_sha256,
                     action, position, kind, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id) DO UPDATE SET
                    domain = excluded.domain,
                    section = excluded.section,
                    occurrence = excluded.occurrence,
                    node_id = excluded.node_id,
                    body_sha256 = excluded.body_sha256,
                    action = excluded.action,
                    position = excluded.position,
                    kind = excluded.kind,
                    updated_at = excluded.updated_at
                """,
                (
                    intent.entry_id,
                    intent.domain,
                    intent.section,
                    int(intent.occurrence),
                    intent.node_id,
                    intent.body_sha256,
                    intent.action,
                    int(intent.position),
                    intent.kind,
                    _now(),
                ),
            )
        return intent

    def clear_surface_intent(self, entry_id: str) -> None:
        """Forget one declared intent, because its publication finalized.

        A delete rather than a status, and the one deletion in this module: an
        intent is coordination state with no historical meaning, and keeping
        every finished one would turn "a publication is unfinished" — a question
        answered by a row's *existence* — into a scan.
        """
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM projection_surface_intents WHERE entry_id = ?",
                (entry_id,),
            )

    def surface_intent(self, entry_id: str) -> SurfaceIntent | None:
        """The unfinished intent for ``entry_id``, or ``None``."""
        connection = self._require_connection()
        if not self._has_table(connection, "projection_surface_intents"):
            return None
        kind = self._surface_kind_column(connection, "projection_surface_intents")
        row = connection.execute(
            f"""
            SELECT entry_id, domain, section, occurrence, node_id, body_sha256,
                   action, position, {kind}
            FROM projection_surface_intents WHERE entry_id = ?
            """,
            (entry_id,),
        ).fetchone()
        return None if row is None else _intent_from_row(row)

    def surface_intents(self) -> list[SurfaceIntent]:
        """Every unfinished publication intent, by entry ID."""
        connection = self._require_connection()
        if not self._has_table(connection, "projection_surface_intents"):
            return []
        kind = self._surface_kind_column(connection, "projection_surface_intents")
        return [
            _intent_from_row(row)
            for row in connection.execute(
                f"""
                SELECT entry_id, domain, section, occurrence, node_id, body_sha256,
                       action, position, {kind}
                FROM projection_surface_intents ORDER BY entry_id
                """
            ).fetchall()
        ]

    @staticmethod
    def _surface_kind_column(connection: sqlite3.Connection, table: str) -> str:
        columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return "kind" if any(row[1] == "kind" for row in columns) else "'l1_markdown_section'"

    @staticmethod
    def _has_table(connection: sqlite3.Connection, name: str) -> bool:
        """Whether ``name`` is present. Only asked about :data:`_OPTIONAL_TABLES`.

        A journal written before a table existed holds no rows for it, and that
        is an answer rather than a fault — see :data:`_OPTIONAL_TABLES`. Asked
        with a ``SELECT``, so a read-only handle can ask it too.
        """
        if name not in _OPTIONAL_TABLES:  # pragma: no cover - guards a typo
            return True
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def surface_node_ids(self) -> dict[str, str]:
        """``entry_id -> node_id`` for every *published* managed surface.

        The exact lookup the recall producers use to turn a searchable row's own
        identity into the graph node that owns it — no heading text, no body
        similarity, no guess.
        """
        return {
            record.entry_id: record.node_id
            for record in self.surfaces(include_retired=False)
        }

    def surfaces_by_node(self) -> dict[str, SurfaceRecord]:
        """``node_id -> surface`` for every published managed surface."""
        return {
            record.node_id: record
            for record in self.surfaces(include_retired=False)
        }

    # -- health --------------------------------------------------------------

    def health(self) -> JournalHealth:
        """Bounded state of the journal. Never raises, never leaks a path."""
        if self._closed:
            return JournalHealth(errors=["journal_closed"])
        try:
            checkpoints = {name: self.checkpoint(name) for name in PROJECTION_NAMES}
            head = self.head_position()
            return JournalHealth(
                event_count=self.event_count(),
                request_count=self.request_count(),
                head_position=head,
                surface_count=len(self.surfaces()),
                unfinished_surface_count=len(self.surface_intents()),
                checkpoints={
                    name: point.position for name, point in checkpoints.items()
                },
                checkpoint_statuses={
                    name: point.status for name, point in checkpoints.items()
                },
                pending_projections=[
                    name
                    for name, point in checkpoints.items()
                    if point.position < head
                ],
            )
        except Exception as exc:  # pragma: no cover - defensive, must not raise
            return JournalHealth(errors=[f"journal_check_failed:{type(exc).__name__}"])


def _intent_from_row(row: Sequence[Any]) -> SurfaceIntent:
    return SurfaceIntent(
        entry_id=str(row[0]),
        domain=str(row[1]),
        section=str(row[2]),
        occurrence=int(row[3]),
        node_id=str(row[4]),
        body_sha256=str(row[5]),
        action=str(row[6]),
        position=int(row[7]),
        kind=str(row[8]),
    )


def _surface_from_row(row: Sequence[Any]) -> SurfaceRecord:
    return SurfaceRecord(
        entry_id=str(row[0]),
        domain=str(row[1]),
        section=str(row[2]),
        occurrence=int(row[3]),
        node_id=str(row[4]),
        body_sha256=str(row[5]),
        status=str(row[6]),
        position=int(row[7]),
        kind=str(row[8]),
    )

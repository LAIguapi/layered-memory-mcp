"""Explicit production assembly of the persistent graph (§12 Batch I).

WHAT THIS MODULE IS
-------------------
The one place where a *declared* graph database becomes an *open* one, and the
only module in the graph layer that is allowed to construct a backend at all.
It does three things:

* :func:`resolve_graph_database_path` — read the one path the configuration
  declared, or answer ``None``.
* :func:`open_graph_store` / :func:`graph_store_session` — construct a
  :class:`~layered_memory_mcp.graph_sqlite_store.SQLiteGraphStore` when, and
  only when, the caller asked for one *and* a path was declared, and report what
  happened as data.
* :func:`open_graph_store_readonly` / :func:`graph_store_readonly_session` — the
  observer's counterpart: an *already existing* graph database, opened through a
  SQLite ``mode=ro`` URI with its layout verified rather than established.
* :func:`diagnose_graph_runtime` — the read-only operator view: is a graph
  configured, can it be opened, is it healthy, and how much of a given result
  set currently carries graph identity.

Both persistent stores are therefore assembled through *two* deliberately
separate entrances, for the same reason. :func:`open_memory_journal` and
:func:`open_graph_store` are the committer's: they establish an absent database
and its schema. :func:`open_memory_journal_readonly` and
:func:`open_graph_store_readonly` are the observer's: they create nothing and
write nothing, so a status check on a system that has never committed reports
what is absent instead of bringing it into existence.

WHAT IT IS NOT
--------------
- NOT a discoverer. There is no default path, no probe, no fallback location
  and no derivation from ``home``, ``knowledge_dir`` or the agent's private
  memory path. An unconfigured graph stays unconfigured
  (:data:`GRAPH_STORE_NOT_CONFIGURED`) — a backend that can find a location on
  its own is a backend that can find the wrong one.
- NOT a startup hook, a CLI, a migration or a backfill. Nothing imports this
  module on the default path; ``server``, ``injector`` and ``recall`` are
  untouched, and the default ``MemoryConfig()`` still reaches no graph at all.
- NOT a directory creator. ``SQLiteGraphStore`` refuses a path whose parent
  does not exist, and that refusal is passed through rather than worked around:
  creating directories for a graph backend is how an import ends up writing
  into somebody's home. The read-only entrance goes further and refuses an
  absent *file* as well (:data:`GRAPH_STORE_NOT_PRESENT`).
- NOT a mode gate. The mode decides whether recall/write may *use* a graph
  (``graph_runtime``); this module decides whether one can be *opened*. A store
  opened under ``legacy`` is reported with a ``graph_mode_legacy`` warning, not
  silently refused — assembling a store to inspect it is exactly what a
  pre-rollout check does.
- NOT a rebuilder. ``rebuild`` capability is *reported*; it is never called, and
  no node, edge or event is written by anything here.

PRIVACY (§15)
-------------
Every report and payload carries booleans, counts, mode/backend names, status
tokens and bounded labels — and nothing else. Never the database path (only
whether one is configured), never a memory body, never a configuration dump,
and never a backend exception *message*: a failure is named by its exception
class, which is enough to act on and safe to log.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from pydantic import Field

from .graph_journal import (
    JournalMissingError,
    JournalNotInitializedError,
    MemoryEventJournal,
)
from .graph_models import GraphEdge, GraphModel, GraphNode, MemoryEvent
from .graph_runtime import GraphModeDecision, build_seed_mapping
from .graph_sqlite_store import (
    META_STORE_SCHEMA_VERSION,
    SQLITE_GRAPH_BACKEND,
    STORE_SCHEMA_VERSION,
    GraphStoreError,
    SQLiteGraphStore,
    StoreSchemaError,
)
from .graph_store import RebuildReport

# ---------------------------------------------------------------------------
# Statuses — "switched off", "never named", "broken" and "open" must not read
# the same in an operator's report.
# ---------------------------------------------------------------------------

#: No caller asked for a store, so none was built. The default answer, and the
#: reason merely *declaring* a path cannot touch a database.
GRAPH_STORE_NOT_REQUESTED = "graph_store_not_requested"

#: A store was asked for and no database path is configured. Visibly absent
#: rather than guessed.
GRAPH_STORE_NOT_CONFIGURED = "graph_store_not_configured"

#: The backend refused to open the declared database (a missing parent
#: directory, an unreadable file, a schema this process does not speak).
GRAPH_STORE_OPEN_FAILED = "graph_store_open_failed"

#: A store is open. Read :attr:`GraphAssemblyReport.healthy` for whether it can
#: be trusted to answer — "open" and "healthy" are different claims (§10.3).
GRAPH_STORE_OPENED = "graph_store_opened"

#: Fallback reason: the store opened but reports integrity errors.
GRAPH_STORE_UNHEALTHY = "graph_store_unhealthy"

#: Fallback reason: a usable graph, and not one supplied result carries a graph
#: identity yet — the expected state before any backfill has run.
GRAPH_SEEDS_UNMAPPED = "graph_seeds_unmapped"

#: Fallback reason: none. The graph is open, healthy and reachable from at
#: least one result.
GRAPH_FALLBACK_NONE = ""

#: Warning label for a store opened while the integration mode is still
#: ``legacy``: legal (inspection is why the assembly exists), but nothing on the
#: recall or write path will consult it.
GRAPH_MODE_LEGACY_WARNING = "graph_mode_legacy"

#: Warning label for a configuration whose ``graph_db_path`` is present but not
#: a usable path value. Reported rather than coerced.
GRAPH_DB_PATH_INVALID_WARNING = "graph_db_path_invalid"

#: A read-only inspection found no graph database at the declared path. Only the
#: read-only path can report this: the writable one would have created the file
#: and its schema, which is exactly the difference between publishing and
#: looking.
GRAPH_STORE_NOT_PRESENT = "graph_store_not_present"

#: A read-only inspection found a file at the declared path that holds no graph
#: layout. Reported, never initialized.
GRAPH_STORE_NOT_INITIALIZED = "graph_store_not_initialized"


class GraphAssemblyReport(GraphModel):
    """What one assembly attempt did, as bounded, serializable data.

    ``path_configured`` is deliberately a boolean rather than the path: an
    operator needs to know *whether* a database was declared, and a report that
    carried the location could not be logged next to a memory answer (§15).

    ``store_opened`` and ``healthy`` are separate for the reason
    :meth:`~layered_memory_mcp.graph_sqlite_store.SQLiteGraphStore.health`
    exists: an open connection to a database holding a corrupt payload is open
    and untrustworthy at the same time.

    ``read_only`` records *which* of the two open paths ran, exactly as
    :attr:`JournalAssemblyReport.read_only` does, so a reader can tell "there is
    no graph database" (inspection) apart from "there was none, and now there
    is" (the writable path).
    """

    mode: str = "legacy"
    backend: str = ""
    status: str = GRAPH_STORE_NOT_REQUESTED
    path_configured: bool = False
    store_opened: bool = False
    read_only: bool = False
    healthy: bool = False
    node_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)
    graph_schema_version: int = Field(default=0, ge=0)
    store_schema_version: int = Field(default=0, ge=0)
    supports_rebuild: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GraphRuntimeAssembly:
    """A store that was (or was not) opened, together with its report.

    Deliberately a plain object rather than a model: it holds a live backend
    handle, and a handle is not data to be serialized beside a memory answer.
    :attr:`report` is the serializable half.
    """

    __slots__ = ("_store", "_report")

    def __init__(
        self, store: SQLiteGraphStore | None, report: GraphAssemblyReport
    ) -> None:
        self._store = store
        self._report = report

    @property
    def store(self) -> SQLiteGraphStore | None:
        """The open backend, or ``None`` — a supported answer, not an error."""
        return self._store

    @property
    def report(self) -> GraphAssemblyReport:
        return self._report

    @property
    def available(self) -> bool:
        """Whether a caller may actually hand this store to a graph seam."""
        return self._store is not None and self._report.healthy

    def close(self) -> None:
        """Close an owned store. Idempotent, and writes nothing."""
        if self._store is not None:
            self._store.close()


def resolve_graph_database_path(config: Any) -> Path | None:
    """The declared graph database path, or ``None``.

    Duck-typed on ``graph_db_path`` alone, matching
    :meth:`~layered_memory_mcp.graph_runtime.GraphModeDecision.from_config`: a
    test double or a per-request override works as well as a
    :class:`~layered_memory_mcp.config.MemoryConfig`, and this module still
    imports no config layer.

    A missing attribute, ``None`` and a blank string all mean *unconfigured*.
    A present-but-unusable value (a number, a bool) also resolves to ``None`` —
    :func:`open_graph_store` reports it as
    :data:`GRAPH_DB_PATH_INVALID_WARNING` rather than coercing it into a
    filename.
    """
    value = getattr(config, "graph_db_path", None)
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    text = value.strip()
    return Path(text).expanduser() if text else None


def _path_warnings(config: Any, path: Path | None) -> list[str]:
    """Name a declared-but-unusable path value, without quoting it."""
    declared = getattr(config, "graph_db_path", None)
    if path is None and declared is not None and not (
        isinstance(declared, str) and not declared.strip()
    ):
        return [GRAPH_DB_PATH_INVALID_WARNING]
    return []


def open_graph_store(
    config: Any,
    *,
    require: bool = False,
    mode: GraphModeDecision | None = None,
) -> GraphRuntimeAssembly:
    """Build the configured graph store, if the caller asked for one.

    Two conditions, both explicit, and neither inferable from the other:

    1. ``require=True`` — the caller wants a backend. The default is ``False``,
       so importing this module, building a config or enabling a mode opens
       nothing.
    2. a database path is configured. Otherwise the answer is
       :data:`GRAPH_STORE_NOT_CONFIGURED` and no location is invented.

    The integration mode never gates the construction; it is recorded on the
    report, and a store opened under ``legacy`` carries
    :data:`GRAPH_MODE_LEGACY_WARNING` so a reader cannot mistake "I opened it"
    for "the system is using it".

    Args:
        config: Anything exposing ``graph_db_path`` and the graph mode
            predicates. Never read for anything else, and never written.
        require: Whether a backend should actually be constructed.
        mode: An already-resolved mode decision; read from ``config`` when
            omitted.

    Returns:
        A :class:`GraphRuntimeAssembly`. The caller owns the store and must
        close it — or use :func:`graph_store_session`, which does.

    Raises:
        Nothing for a graph failure. An unopenable database is a reported
        status, because a diagnostic that raises is one an operator cannot run
        when something is already wrong.
    """
    decision = mode if mode is not None else GraphModeDecision.from_config(config)
    path = resolve_graph_database_path(config)
    warnings = _path_warnings(config, path)

    def _report(status: str, **fields: Any) -> GraphAssemblyReport:
        extra = fields.pop("extra_warnings", ())
        return GraphAssemblyReport(
            mode=decision.mode,
            status=status,
            path_configured=path is not None,
            warnings=sorted({*warnings, *extra}),
            **fields,
        )

    if not require:
        # Not even a health() call: "nobody asked" must cost nothing.
        return GraphRuntimeAssembly(None, _report(GRAPH_STORE_NOT_REQUESTED))

    if path is None:
        return GraphRuntimeAssembly(
            None,
            _report(
                GRAPH_STORE_NOT_CONFIGURED,
                extra_warnings=[f"{GRAPH_STORE_NOT_CONFIGURED}:no_path"],
            ),
        )

    try:
        # No mkdir: the backend's own refusal of a missing parent directory is
        # the behaviour we want, not an obstacle to route around.
        store = SQLiteGraphStore(path)
    except Exception as exc:  # noqa: BLE001 — backend-neutral, named not quoted
        return GraphRuntimeAssembly(
            None,
            _report(
                GRAPH_STORE_OPEN_FAILED,
                backend=SQLITE_GRAPH_BACKEND,
                errors=[f"{GRAPH_STORE_OPEN_FAILED}:{type(exc).__name__}"],
            ),
        )

    mode_warnings = (
        [] if decision.mode != "legacy" else [GRAPH_MODE_LEGACY_WARNING]
    )
    return GraphRuntimeAssembly(
        store,
        _report(
            GRAPH_STORE_OPENED,
            store_opened=True,
            extra_warnings=mode_warnings,
            **_store_facts(store),
        ),
    )


def _store_facts(store: SQLiteGraphStore) -> dict[str, Any]:
    """Counts, versions, capability and integrity errors — reads only.

    ``health`` never raises by contract, and ``schema_info`` is guarded for the
    same reason the whole module is: a diagnostic must survive the state it was
    called to diagnose.
    """
    health = store.health()
    try:
        store_schema_version = int(
            store.schema_info().get("store_schema_version") or 0
        )
    except Exception:  # noqa: BLE001 — a version we cannot read is not a crash
        store_schema_version = 0
    return {
        "backend": health.backend or SQLITE_GRAPH_BACKEND,
        "healthy": health.healthy,
        "node_count": health.node_count,
        "edge_count": health.edge_count,
        "graph_schema_version": health.schema_version,
        "store_schema_version": store_schema_version,
        # Reported, never exercised: whether a *later*, explicit batch could
        # project an event stream into this store. An inspection handle answers
        # ``False`` — its ``rebuild`` exists only to refuse (§10.3: "can be
        # rebuilt" and "this handle may rebuild it" are different claims).
        "supports_rebuild": callable(getattr(store, "rebuild", None))
        and not getattr(store, "read_only", False),
        "errors": sorted(health.errors),
    }


@contextmanager
def graph_store_session(
    config: Any,
    *,
    require: bool = False,
    mode: GraphModeDecision | None = None,
) -> Iterator[GraphRuntimeAssembly]:
    """:func:`open_graph_store` with a guaranteed close.

    The lifecycle this batch supports: open explicitly, use, close. Nothing is
    cached, pooled or kept alive between calls, so two sessions over one file
    never disagree about what it holds.
    """
    assembly = open_graph_store(config, require=require, mode=mode)
    try:
        yield assembly
    finally:
        assembly.close()


# ---------------------------------------------------------------------------
# The read-only graph store entrance (v3.6.1)
# ---------------------------------------------------------------------------

#: The tables a database must already hold before it can be inspected as a
#: graph. Checked with a ``SELECT`` against ``sqlite_master``, never created.
_GRAPH_REQUIRED_TABLES: tuple[str, ...] = ("graph_edges", "graph_meta", "graph_nodes")


class GraphStoreReadOnlyError(GraphStoreError):
    """A mutating operation was asked of an inspection-only graph handle.

    Raised by name, before SQLite is asked for anything, for the same reason
    :class:`~layered_memory_mcp.graph_journal.JournalReadOnlyError` is: an
    observer that tried to write should read a refusal naming the handle it
    holds, not a driver error about a read-only database.
    """


class ReadOnlyGraphStore(SQLiteGraphStore):
    """An existing graph database, opened for observation only.

    It *is* a :class:`~layered_memory_mcp.graph_sqlite_store.SQLiteGraphStore`,
    so every read — ``health``, ``schema_info``, ``snapshot``, ``traverse``,
    ``get_node``, ``get_edges``, ``audit_drift`` — is the backend's own
    implementation rather than a second one written here. A separate reader is
    how an observer and a committer would start disagreeing about what the same
    file holds.

    What this class replaces is only the lifecycle. The connection comes from a
    SQLite ``mode=ro`` URI with ``query_only`` on, and the layout is *verified*
    by :func:`_open_readonly_graph_store` instead of established, so inspecting
    a graph never creates a database, a table, a meta row or a write
    transaction. It lives in this module rather than beside the backend for the
    reason stated at the top: this is the only module allowed to construct one.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        # Deliberately *not* ``super().__init__``: that constructor's job is to
        # establish the schema inside a ``BEGIN IMMEDIATE``, which is the one
        # thing an inspection must not do. The four lifecycle attributes it
        # would have set are set here instead — ``_owns_connection`` is True
        # because this connection was opened for this handle — so ``close`` and
        # ``closed`` behave exactly as they do for a writable owned store.
        self._closed = False
        self._owns_connection = True
        self._restore_isolation = None
        self._connection = connection

    @property
    def read_only(self) -> bool:
        """Whether this handle was opened for inspection only. Always ``True``."""
        return True

    # -- the mutating surface, refused by name --------------------------------

    def create_node(self, node: GraphNode) -> GraphNode:
        raise GraphStoreReadOnlyError(
            "this graph store was opened for read-only inspection; open a "
            "writable SQLiteGraphStore on the same database to publish."
        )

    def append_edge(self, edge: GraphEdge) -> GraphEdge:
        raise GraphStoreReadOnlyError(
            "this graph store was opened for read-only inspection; open a "
            "writable SQLiteGraphStore on the same database to publish."
        )

    def rebuild(
        self,
        source_events: Sequence[MemoryEvent],
        *,
        expected_revision: int | None = None,
    ) -> RebuildReport:
        raise GraphStoreReadOnlyError(
            "this graph store was opened for read-only inspection; a rebuild "
            "replaces the stored projection and needs a writable store."
        )


def _open_readonly_graph_store(
    path: Path,
) -> tuple[ReadOnlyGraphStore | None, str, list[str]]:
    """Open an existing graph database for inspection: ``(store, status, errors)``.

    Two explicit refusals before SQLite is asked for anything — an absent parent
    directory, an absent file — so "it does not exist" never arrives as a
    generic open failure and the ``mode=ro`` URI is never the only thing
    standing between a diagnostic and a created database. The layout is then
    checked with ``SELECT`` only: a database missing the graph tables is
    reported as uninitialized rather than initialized, and a store schema
    version this process does not speak is refused the same way the writable
    path refuses it.
    """
    def _failed(exc: BaseException) -> tuple[None, str, list[str]]:
        label = f"{GRAPH_STORE_OPEN_FAILED}:{type(exc).__name__}"
        return None, GRAPH_STORE_OPEN_FAILED, [label]

    resolved = path.expanduser().absolute()
    if not resolved.parent.is_dir() or not resolved.is_file():
        return None, GRAPH_STORE_NOT_PRESENT, [GRAPH_STORE_NOT_PRESENT]

    try:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro", uri=True, isolation_level=None
        )
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return _failed(exc)

    try:
        # ``query_only`` makes the connection refuse a write even if some future
        # statement tried one; the URI alone states an intention, this enforces
        # it.
        connection.execute("PRAGMA query_only = ON")
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        connection.close()
        return _failed(exc)

    if set(_GRAPH_REQUIRED_TABLES) - present:
        connection.close()
        return None, GRAPH_STORE_NOT_INITIALIZED, [GRAPH_STORE_NOT_INITIALIZED]

    store = ReadOnlyGraphStore(connection)
    try:
        stored = store.schema_info().get(META_STORE_SCHEMA_VERSION)
        if stored and int(stored) != STORE_SCHEMA_VERSION:
            raise StoreSchemaError(
                f"database was written with store schema version {stored}, this "
                f"process speaks {STORE_SCHEMA_VERSION}; refusing to read it "
                "rather than guessing at the layout."
            )
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        store.close()
        return _failed(exc)
    return store, GRAPH_STORE_OPENED, []


def open_graph_store_readonly(
    config: Any,
    *,
    require: bool = False,
    mode: GraphModeDecision | None = None,
) -> GraphRuntimeAssembly:
    """Open the configured graph database for inspection only, if asked.

    The observer's counterpart of :func:`open_graph_store`, with the same two
    explicit conditions (``require=True`` and a declared path) and the same
    refusal to raise for a graph-layer failure. What it adds is the guarantee an
    operator view needs: nothing is created and nothing is written, so running a
    status check against a system that has never published leaves it exactly as
    it was — no database file, no schema, no meta rows, and an unchanged
    modification time on a graph that does exist.

    Returns:
        A :class:`GraphRuntimeAssembly` whose report distinguishes a store that
        is unrequested (:data:`GRAPH_STORE_NOT_REQUESTED`), undeclared
        (:data:`GRAPH_STORE_NOT_CONFIGURED`), absent
        (:data:`GRAPH_STORE_NOT_PRESENT`), present but holding no graph layout
        (:data:`GRAPH_STORE_NOT_INITIALIZED`), unreadable
        (:data:`GRAPH_STORE_OPEN_FAILED`) and open (:data:`GRAPH_STORE_OPENED`).
        The caller owns the store and must close it — or use
        :func:`graph_store_readonly_session`, which does.
    """
    decision = mode if mode is not None else GraphModeDecision.from_config(config)
    path = resolve_graph_database_path(config)
    warnings = _path_warnings(config, path)

    def _report(status: str, **fields: Any) -> GraphAssemblyReport:
        extra = fields.pop("extra_warnings", ())
        return GraphAssemblyReport(
            mode=decision.mode,
            status=status,
            path_configured=path is not None,
            read_only=True,
            warnings=sorted({*warnings, *extra}),
            **fields,
        )

    if not require:
        return GraphRuntimeAssembly(None, _report(GRAPH_STORE_NOT_REQUESTED))

    if path is None:
        return GraphRuntimeAssembly(
            None,
            _report(
                GRAPH_STORE_NOT_CONFIGURED,
                extra_warnings=[f"{GRAPH_STORE_NOT_CONFIGURED}:no_path"],
            ),
        )

    store, status, errors = _open_readonly_graph_store(path)
    if store is None:
        return GraphRuntimeAssembly(
            None, _report(status, backend=SQLITE_GRAPH_BACKEND, errors=errors)
        )

    mode_warnings = [] if decision.mode != "legacy" else [GRAPH_MODE_LEGACY_WARNING]
    return GraphRuntimeAssembly(
        store,
        _report(
            GRAPH_STORE_OPENED,
            store_opened=True,
            extra_warnings=mode_warnings,
            **_store_facts(store),
        ),
    )


@contextmanager
def graph_store_readonly_session(
    config: Any,
    *,
    require: bool = False,
    mode: GraphModeDecision | None = None,
) -> Iterator[GraphRuntimeAssembly]:
    """:func:`open_graph_store_readonly` with a guaranteed close."""
    assembly = open_graph_store_readonly(config, require=require, mode=mode)
    try:
        yield assembly
    finally:
        assembly.close()


# ---------------------------------------------------------------------------
# The semantic decision journal (v3.6.1) — opened the same way, from its own
# declared path
# ---------------------------------------------------------------------------

#: No caller asked for a journal, so none was opened.
JOURNAL_NOT_REQUESTED = "graph_journal_not_requested"

#: A journal was asked for and no ``graph_journal_path`` is configured. A write
#: mode in this state refuses; it never invents an authority beside the graph.
JOURNAL_NOT_CONFIGURED = "graph_journal_not_configured"

#: The declared journal could not be opened (missing parent, unreadable file,
#: a layout this process does not speak).
JOURNAL_OPEN_FAILED = "graph_journal_open_failed"

#: The journal is open. Read its own ``health()`` for whether it is well.
JOURNAL_OPENED = "graph_journal_opened"

#: A read-only inspection found no journal at the declared path. Only the
#: read-only path can report this: the writable path would have created one,
#: which is precisely the difference between committing and looking.
JOURNAL_NOT_PRESENT = "graph_journal_not_present"

#: A read-only inspection found a file at the declared path that holds no
#: journal layout. Reported, never initialized.
JOURNAL_NOT_INITIALIZED = "graph_journal_not_initialized"


class JournalAssemblyReport(GraphModel):
    """What one journal assembly attempt did, as bounded, serializable data.

    ``read_only`` records *which* of the two open paths ran, so a reader of a
    status payload can tell "there is no journal" (inspection) apart from "there
    was none, and now there is" (the writable path) — two different facts that
    the same ``opened=False`` could otherwise hide.
    """

    status: str = JOURNAL_NOT_REQUESTED
    path_configured: bool = False
    opened: bool = False
    read_only: bool = False
    errors: list[str] = Field(default_factory=list)


class JournalRuntimeAssembly:
    """A journal that was (or was not) opened, together with its report."""

    __slots__ = ("_journal", "_report")

    def __init__(
        self, journal: MemoryEventJournal | None, report: JournalAssemblyReport
    ) -> None:
        self._journal = journal
        self._report = report

    @property
    def journal(self) -> MemoryEventJournal | None:
        return self._journal

    @property
    def report(self) -> JournalAssemblyReport:
        return self._report

    @property
    def available(self) -> bool:
        return self._journal is not None

    def close(self) -> None:
        if self._journal is not None:
            self._journal.close()


def resolve_journal_database_path(config: Any) -> Path | None:
    """The declared journal path, or ``None``. Same three answers as the graph."""
    value = getattr(config, "graph_journal_path", None)
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    text = value.strip()
    return Path(text).expanduser() if text else None


def open_memory_journal(
    config: Any, *, require: bool = False
) -> JournalRuntimeAssembly:
    """Open the configured semantic decision journal, if the caller asked.

    The same two explicit conditions the graph store has — a caller that wants
    one, and a path somebody declared — and the same refusal to create a parent
    directory. A failure is a reported status rather than an exception: a write
    path has to refuse cleanly rather than raise out of an MCP entrance.

    This is the **writable** path: an absent database file is created, and its
    schema, metadata and checkpoint rows are established, inside a write
    transaction. That is what a committer needs and what an observer must not
    do — a diagnostic belongs on :func:`open_memory_journal_readonly`.
    """
    path = resolve_journal_database_path(config)
    if not require:
        return JournalRuntimeAssembly(
            None, JournalAssemblyReport(path_configured=path is not None)
        )
    if path is None:
        return JournalRuntimeAssembly(
            None, JournalAssemblyReport(status=JOURNAL_NOT_CONFIGURED)
        )
    try:
        journal = MemoryEventJournal(path)
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return JournalRuntimeAssembly(
            None,
            JournalAssemblyReport(
                status=JOURNAL_OPEN_FAILED,
                path_configured=True,
                errors=[f"{JOURNAL_OPEN_FAILED}:{type(exc).__name__}"],
            ),
        )
    return JournalRuntimeAssembly(
        journal,
        JournalAssemblyReport(
            status=JOURNAL_OPENED, path_configured=True, opened=True
        ),
    )


@contextmanager
def journal_session(
    config: Any, *, require: bool = False
) -> Iterator[JournalRuntimeAssembly]:
    """:func:`open_memory_journal` with a guaranteed close. Writable."""
    assembly = open_memory_journal(config, require=require)
    try:
        yield assembly
    finally:
        assembly.close()


def open_memory_journal_readonly(
    config: Any, *, require: bool = False
) -> JournalRuntimeAssembly:
    """Open the configured journal for inspection only, if the caller asked.

    The observer's counterpart of :func:`open_memory_journal`, with the same two
    explicit conditions (``require=True`` and a declared path) and the same
    refusal to raise for a graph-layer failure. What it adds is the guarantee an
    operator view needs: nothing is created and nothing is written, so running a
    status check on a system that has never committed leaves it exactly as it
    was — no database file, no schema, no checkpoint rows, and an unchanged
    modification time on a journal that does exist.

    Returns:
        A :class:`JournalRuntimeAssembly` whose report distinguishes a journal
        that is absent (:data:`JOURNAL_NOT_PRESENT`), present but never
        initialized (:data:`JOURNAL_NOT_INITIALIZED`), unreadable
        (:data:`JOURNAL_OPEN_FAILED`) and open (:data:`JOURNAL_OPENED`).
    """
    path = resolve_journal_database_path(config)
    if not require:
        return JournalRuntimeAssembly(
            None,
            JournalAssemblyReport(
                path_configured=path is not None, read_only=True
            ),
        )
    if path is None:
        return JournalRuntimeAssembly(
            None,
            JournalAssemblyReport(status=JOURNAL_NOT_CONFIGURED, read_only=True),
        )

    def _refused(status: str, *, errors: list[str]) -> JournalRuntimeAssembly:
        return JournalRuntimeAssembly(
            None,
            JournalAssemblyReport(
                status=status,
                path_configured=True,
                read_only=True,
                errors=errors,
            ),
        )

    try:
        journal = MemoryEventJournal.open_readonly(path)
    except JournalMissingError:
        # Named separately from a failure: "no authority here yet" is the
        # expected state before the first commit, not a fault to be alarmed by.
        return _refused(JOURNAL_NOT_PRESENT, errors=[JOURNAL_NOT_PRESENT])
    except JournalNotInitializedError:
        return _refused(JOURNAL_NOT_INITIALIZED, errors=[JOURNAL_NOT_INITIALIZED])
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return _refused(
            JOURNAL_OPEN_FAILED,
            errors=[f"{JOURNAL_OPEN_FAILED}:{type(exc).__name__}"],
        )
    return JournalRuntimeAssembly(
        journal,
        JournalAssemblyReport(
            status=JOURNAL_OPENED,
            path_configured=True,
            opened=True,
            read_only=True,
        ),
    )


@contextmanager
def journal_readonly_session(
    config: Any, *, require: bool = False
) -> Iterator[JournalRuntimeAssembly]:
    """:func:`open_memory_journal_readonly` with a guaranteed close."""
    assembly = open_memory_journal_readonly(config, require=require)
    try:
        yield assembly
    finally:
        assembly.close()


# ---------------------------------------------------------------------------
# Read-only diagnostics (§12 Batch I scope item 4)
# ---------------------------------------------------------------------------

def graph_assembly_payload(report: GraphAssemblyReport) -> dict[str, Any]:
    """The bounded, plain-JSON view of one assembly report.

    A fixed key set, so a consumer can depend on the shape, and plain types
    only, so it can be serialized, logged or returned beside an MCP response
    without carrying anything it should not.

    ``read_only`` is deliberately not one of the keys: the shape is pinned, and
    ``status`` already distinguishes the answers only an inspection can give
    (:data:`GRAPH_STORE_NOT_PRESENT`, :data:`GRAPH_STORE_NOT_INITIALIZED`).
    Read it off :class:`GraphAssemblyReport` when the entrance itself matters.
    """
    return {
        "mode": report.mode,
        "backend": report.backend,
        "status": report.status,
        "path_configured": report.path_configured,
        "store_opened": report.store_opened,
        "healthy": report.healthy,
        "node_count": report.node_count,
        "edge_count": report.edge_count,
        "graph_schema_version": report.graph_schema_version,
        "store_schema_version": report.store_schema_version,
        "supports_rebuild": report.supports_rebuild,
        "errors": list(report.errors),
        "warnings": list(report.warnings),
    }


def _fallback_reason(report: GraphAssemblyReport, mapped_results: int) -> str:
    """The single token naming why the graph cannot assist, or ``""``.

    Ordered most-fundamental first — not configured beats unopenable beats
    unhealthy beats unmapped — so one answer describes the thing an operator
    would have to fix first.
    """
    if report.status != GRAPH_STORE_OPENED:
        return report.status
    if not report.healthy:
        return GRAPH_STORE_UNHEALTHY
    if not mapped_results:
        return GRAPH_SEEDS_UNMAPPED
    return GRAPH_FALLBACK_NONE


def diagnose_graph_runtime(
    config: Any,
    results: Sequence[Any] = (),
    *,
    require: bool = False,
) -> dict[str, Any]:
    """Answer "is the graph usable, and how much of this answer can reach it?".

    Strictly read-only, in the literal sense rather than merely in intent: the
    store is opened through :func:`graph_store_readonly_session`, so a
    ``require=True`` diagnostic on a system that has never published creates no
    database file, no directory, no schema and no meta row, and reports the
    graph as :data:`GRAPH_STORE_NOT_PRESENT` instead of bringing one into
    existence. An existing graph is left byte-for-byte as it was. It opens a
    store only when ``require=True``, closes it again, and never writes,
    rebuilds, imports, repairs or re-ranks anything: the two questions it
    answers are both observations, and a diagnostic that changed state would be
    measuring itself.

    It also decides nothing semantic. Mapping coverage is a *count* of rows
    carrying graph identity — whether those rows are right, current or in scope
    is the graph's question and the reviewer's, not a metric's (§13).

    Args:
        config: The configuration to read the path and mode from.
        results: Ranked legacy result payloads, each with the ``file`` identity
            the recall path emits. Rows without one are counted as
            ``results_without_identity`` rather than dropped silently.
        require: Whether to actually open the configured database for
            inspection. ``False`` reports the configuration without touching a
            file; ``True`` reads an existing one and still creates nothing.

    Returns:
        ``{"status", "fallback_reason", "assembly", "mapping"}`` — counts,
        statuses and bounded labels only (§15): no path, no memory content, no
        metadata dump, no backend message.
    """
    with graph_store_readonly_session(config, require=require) as assembly:
        report = assembly.report

    mapping = build_seed_mapping(results)
    return {
        "status": report.status,
        "fallback_reason": _fallback_reason(report, mapping.mapped_results),
        "assembly": graph_assembly_payload(report),
        "mapping": {
            "results": len(results),
            "mapped_results": mapping.mapped_results,
            "unmapped_results": mapping.unmapped_results,
            "rejected_ids": mapping.rejected_ids,
            "warnings": list(mapping.warnings),
        },
    }


__all__ = [
    "GRAPH_DB_PATH_INVALID_WARNING",
    "JOURNAL_NOT_CONFIGURED",
    "JOURNAL_NOT_INITIALIZED",
    "JOURNAL_NOT_PRESENT",
    "JOURNAL_NOT_REQUESTED",
    "JOURNAL_OPENED",
    "JOURNAL_OPEN_FAILED",
    "JournalAssemblyReport",
    "JournalRuntimeAssembly",
    "journal_readonly_session",
    "journal_session",
    "open_memory_journal",
    "open_memory_journal_readonly",
    "resolve_journal_database_path",
    "GRAPH_FALLBACK_NONE",
    "GRAPH_MODE_LEGACY_WARNING",
    "GRAPH_SEEDS_UNMAPPED",
    "GRAPH_STORE_NOT_CONFIGURED",
    "GRAPH_STORE_NOT_INITIALIZED",
    "GRAPH_STORE_NOT_PRESENT",
    "GRAPH_STORE_NOT_REQUESTED",
    "GRAPH_STORE_OPENED",
    "GRAPH_STORE_OPEN_FAILED",
    "GRAPH_STORE_UNHEALTHY",
    "GraphAssemblyReport",
    "GraphRuntimeAssembly",
    "GraphStoreReadOnlyError",
    "ReadOnlyGraphStore",
    "diagnose_graph_runtime",
    "graph_assembly_payload",
    "graph_store_readonly_session",
    "graph_store_session",
    "open_graph_store",
    "open_graph_store_readonly",
    "resolve_graph_database_path",
]

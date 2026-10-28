"""The embedded, persistent memory-graph store (ARCHITECTURE_v2.md §9.2 step 2).

WHAT THIS MODULE IS
-------------------
A :class:`~layered_memory_mcp.graph_store.GraphStore` implementation that keeps
the graph in a single embedded SQLite database instead of in process memory. It
is the first backend in this framework that *survives the process*: write a
node, close the store, reopen the same file, and the node is still there with a
byte-identical payload.

SQLite is used strictly as physical storage. Nodes and edges are persisted as
the deterministic JSON of the §6 domain models
(:meth:`~layered_memory_mcp.graph_models.GraphModel.to_json`) and re-validated
back into :class:`~layered_memory_mcp.graph_models.GraphNode` /
:class:`~layered_memory_mcp.graph_models.GraphEdge` on the way out, so no
``sqlite3`` type, row object or SQL fragment ever reaches a caller (§9.3). The
same reasoning rules out a vendor query language: there is no Cypher, no SQL
passed in from outside, and no query builder on this surface.

WHY IT BORROWS ITS QUERY SEMANTICS
----------------------------------
``traverse``, ``find_conflicts`` and ``get_provenance`` are not re-derived here.
They are answered by projecting the persisted rows into the Batch C reference
store (:class:`~layered_memory_mcp.graph_fixture_store.InMemoryGraphStore`) and
asking *it*, and the append-only edge rule and the one documented edge sort
order are imported from the same module.

That is a deliberate correctness trade, not laziness. A second hand-rolled
implementation of "which filters apply during expansion, when does a
contradiction stop contesting, what counts as an unresolvable provenance
reference" would agree with the reference on the day it was written and drift
from it on the first change to either side — and a graph backend that quietly
disagrees with the reference about superseded state is worse than no backend.
The cost is explicit and bounded: a multi-hop read materializes the graph it
reads. This is the honest v1 shape for an embedded store holding one agent's
memory, not a claim about graph-database scale; a backend that needs
push-down traversal (a server-side graph engine, §9.3) is a different adapter
behind the same contract, and the reference store is what it would be measured
against.

WHAT IT IS NOT
--------------
- NOT wired into anything. No MCP tool, recall path, injector or storage module
  imports it; it imports no ``server``, ``storage`` or ``config`` module, and it
  has no default path, no environment lookup and no auto-discovery. The database
  target is a constructor argument, always, so this store cannot find real
  memory data by accident.
- NOT a migration tool. :data:`STORE_SCHEMA_VERSION` is recorded and checked;
  nothing is rewritten. A database written by a newer layout is refused, not
  upgraded.
- NOT Kuzu, Neo4j or any third-party graph engine. Nothing outside the Python
  standard library and the existing graph modules is imported or required here.
- NOT a cache. Every read goes to the database; nothing is memoized across
  calls, so two stores over one file never serve stale state.

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Durability with deterministic bytes.** One row per node/edge, keyed by ID,
   payload written through ``to_json`` (sorted keys), so a reopened database
   and a snapshot comparison are both byte-stable.
2. **Explicit transaction boundaries.** Every write runs inside
   ``BEGIN IMMEDIATE`` … ``COMMIT``, and any exception rolls back. There is no
   half-written edge and no half-replaced rebuild.
2b. **A serialized publish with a durable compare-and-set.** A rebuild stages
   its candidate outside the write lock — that is what keeps the window in
   which the graph is being replaced as short as possible — so the staged
   candidate describes the graph as it was at a *past* moment. Every committed
   mutation bumps a durable ``graph_revision`` in ``graph_meta``, and
   :meth:`SQLiteGraphStore.rebuild` re-reads it *inside* its own
   ``BEGIN IMMEDIATE`` and refuses (:class:`StaleRebuildError`) if it moved.
   Without that check a replacing rebuild whose candidate was staged before a
   concurrent ``create_node`` committed would delete the newer node and publish
   a graph nobody's ledger describes — a lost update, silently. The refusal
   rolls back before deleting anything, so a rejected publish mutates no row
   and does not move the revision.
3. **Validated reads.** A stored payload is re-validated before it is returned.
   A corrupted or unparseable row raises :class:`CorruptPayloadError` rather
   than being handed back as a trusted object.
4. **Honesty over convenience** (§10.3). :meth:`SQLiteGraphStore.health` never
   raises and never reports a green light next to a dangling edge, a schema
   mismatch, a corrupt payload or a closed connection. Drift between the stored
   projection and an expected source is a *separate* question with a separate
   answer — :meth:`SQLiteGraphStore.audit_drift` — because "the database is
   fine" and "the database matches the ledger" are different claims.
5. **No implicit lifecycle side effects.** Opening never creates a directory;
   closing never writes or deletes data; there is no ``__del__``, so garbage
   collection cannot touch a database.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from pydantic import Field

#: ``EdgeConflictError`` and ``UnknownNodeError`` are imported to be
#: *re-exported*: this backend really does raise them — the first through the
#: reference store's withdrawal check, the second through its provenance read —
#: so a caller holding a ``SQLiteGraphStore`` should be able to catch them from
#: this module without knowing that the rules live in the fixture one.
#: ``_edge_sort_key`` is module-private by naming but shared on purpose: it is
#: *the* documented edge order, and restating it here is how the two backends
#: would start returning edges in different orders.
from .graph_fixture_store import (
    EdgeConflictError,
    GraphStoreError,
    InMemoryGraphStore,
    UnknownNodeError,
    _edge_sort_key,
)
from .graph_models import (
    GRAPH_SCHEMA_VERSION,
    GraphEdge,
    GraphModel,
    GraphNode,
    GraphScope,
    MemoryEvent,
    RelationType,
)
from .graph_store import (
    ConflictReport,
    EdgeDirection,
    GraphHealth,
    ProvenanceRecord,
    RebuildReport,
    TraversalFilters,
    TraversalResult,
)

__all__ = [
    "META_BACKEND",
    "META_GRAPH_REVISION",
    "META_GRAPH_SCHEMA_VERSION",
    "META_STORE_SCHEMA_VERSION",
    "SQLITE_GRAPH_BACKEND",
    "STORE_SCHEMA_VERSION",
    "CorruptPayloadError",
    "EdgeConflictError",
    "GraphDriftReport",
    "GraphStoreClosedError",
    "GraphStoreError",
    "InvalidPayloadError",
    "MissingEndpointError",
    "SQLiteGraphStore",
    "StaleRebuildError",
    "StoreSchemaError",
    "StoreUnavailableError",
    "UnknownNodeError",
]

#: The name this implementation reports through
#: :attr:`~layered_memory_mcp.graph_store.GraphHealth.backend`. Callers that log
#: or branch on the backend compare against this constant, never a literal.
SQLITE_GRAPH_BACKEND = "sqlite_embedded"

#: Version of the *physical* table layout, independent of
#: :data:`~layered_memory_mcp.graph_models.GRAPH_SCHEMA_VERSION`, which versions
#: the node/edge payload shape. The two move for different reasons: adding an
#: index or a column is a storage change, renaming a domain field is a payload
#: change, and conflating them would force a payload migration for a storage
#: tweak.
STORE_SCHEMA_VERSION = 1

#: Keys in the ``graph_meta`` table. Stored as data rather than inlined so a
#: reader (a drift audit, a health check, a future migration) names the same
#: thing the writer did.
META_STORE_SCHEMA_VERSION = "store_schema_version"
META_GRAPH_SCHEMA_VERSION = "graph_schema_version"
META_BACKEND = "backend"

#: The durable mutation counter every committed write bumps, and the thing a
#: replacing rebuild compares against. Deliberately *not* part of
#: :meth:`SQLiteGraphStore.schema_info`: a schema version describes a layout and
#: moves when a release does, while this moves on every write, and reporting
#: them through one accessor would invite a caller to treat a write as a
#: migration. :meth:`SQLiteGraphStore.revision` is its own reader.
META_GRAPH_REVISION = "graph_revision"

#: What :meth:`SQLiteGraphStore.health` says about itself when nothing is wrong.
_HEALTH_DETAIL = "embedded sqlite graph store"

#: The ``WHERE`` fragments for the three edge directions. Kept as a fixed table
#: so the only thing a caller's ``direction`` can do is *select* one of these
#: three constants — an unknown direction is a loud refusal, and no part of a
#: query string is ever assembled from caller input.
_DIRECTION_PREDICATES: dict[str, str] = {
    "outgoing": "source_id = ?",
    "incoming": "target_id = ?",
    "both": "source_id = ? OR target_id = ?",
}

#: The DDL, applied with ``IF NOT EXISTS`` so initialization is idempotent and a
#: reopen of an existing database is a no-op.
#:
#: Deliberately *no* foreign key between an edge and its endpoints: §10.2
#: requires a rebuild to **keep and report** an edge whose endpoint the event
#: stream never projected, because dropping it would make a broken reference
#: indistinguishable from one that never existed. Endpoint existence is enforced
#: where it belongs instead — on :meth:`SQLiteGraphStore.append_edge`, which is
#: a single deliberate assertion — and a dangling edge that arrives through a
#: rebuild stays visible in :meth:`SQLiteGraphStore.health` forever after.
#:
#: ``kind``/``status``/``relation_type``/``schema_version`` are denormalized out
#: of the payload purely so the database can index and count them; the payload
#: remains the single source of truth, and every read re-validates it.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS graph_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_nodes (
        id             TEXT PRIMARY KEY,
        kind           TEXT NOT NULL,
        status         TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        payload        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_edges (
        id             TEXT PRIMARY KEY,
        relation_type  TEXT NOT NULL,
        source_id      TEXT NOT NULL,
        target_id      TEXT NOT NULL,
        status         TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        payload        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS graph_nodes_kind ON graph_nodes (kind)",
    "CREATE INDEX IF NOT EXISTS graph_edges_source ON graph_edges (source_id)",
    "CREATE INDEX IF NOT EXISTS graph_edges_target ON graph_edges (target_id)",
    "CREATE INDEX IF NOT EXISTS graph_edges_relation ON graph_edges (relation_type)",
)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

class StoreSchemaError(GraphStoreError):
    """The database's physical layout is not one this process can speak.

    Raised on open rather than repaired: silently adopting a layout written by
    another version is how a storage migration loses rows (§9.3). Subclasses
    :class:`~layered_memory_mcp.graph_fixture_store.GraphStoreError` so a caller
    can catch "the graph refused" without knowing which backend it holds.
    """


class StoreUnavailableError(GraphStoreError):
    """The database could not be opened or read at all.

    Separate from :class:`StoreSchemaError` because the answers differ: an
    unavailable store may come back, a wrong layout will not.
    """


class GraphStoreClosedError(GraphStoreError):
    """An operation was attempted on a store whose lifecycle has ended.

    Raised instead of transparently reopening: a reopen would make
    use-after-close invisible, and a closed store answering reads is how two
    halves of a program end up disagreeing about which database they are on.
    :meth:`SQLiteGraphStore.health` stays answerable and reports the closure.
    """


class CorruptPayloadError(GraphStoreError):
    """A stored payload did not re-validate as the domain model it claims to be.

    The store refuses to return arbitrary JSON as a trusted
    :class:`~layered_memory_mcp.graph_models.GraphNode` or
    :class:`~layered_memory_mcp.graph_models.GraphEdge`; a row that cannot be
    revalidated is a read failure, not an empty result.
    """


class InvalidPayloadError(GraphStoreError):
    """A node or edge handed to a write did not validate as its own model.

    ``model_copy(update=...)`` and ``model_construct`` both skip validation, so
    a caller can hold a :class:`~layered_memory_mcp.graph_models.GraphEdge`
    whose ``status`` is the string ``"retracted"`` rather than an
    :class:`~layered_memory_mcp.graph_models.EdgeStatus`. Persisting that as-is
    would write a row the schema cannot describe and the next read cannot
    revalidate, so the write boundary revalidates instead of trusting the type
    annotation — normalizing what is merely unnormalized, and refusing what is
    genuinely illegal.
    """


class StaleRebuildError(GraphStoreError):
    """A replacing rebuild's candidate is older than the graph it would replace.

    :meth:`SQLiteGraphStore.rebuild` stages its candidate before it takes the
    write lock, so between staging and publishing another connection may commit
    a node or an edge the candidate does not contain. Publishing anyway would
    *delete* that newer state — a lost update that no error, no health check and
    no drift audit would attribute to the rebuild.

    Raised instead, from inside the publish transaction and before a single row
    is deleted, so the newer snapshot survives untouched and the graph revision
    does not move. Retrying is safe but only after re-deriving the candidate:
    replaying the same event stream would stage the same stale graph and be
    refused again, which is the point. The caller that owns the ledger — the
    application layer's publish/recovery path — re-reads it and rebuilds from
    what is now committed.
    """


class MissingEndpointError(GraphStoreError):
    """An edge was appended whose source or target node is not stored.

    A deliberate single assertion has no excuse for a dangling endpoint — the
    caller knows both ends. :meth:`SQLiteGraphStore.rebuild` is the one path
    that keeps such an edge, because it is handed a whole ledger it did not
    author and must report rather than drop (§10.2).
    """


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

class GraphDriftReport(GraphModel):
    """How the persisted projection differs from an expected source (§10.2).

    This is *not* health. Health answers "can this backend be trusted to
    answer right now"; drift answers "does what it holds still match the
    ledger it was projected from". A store can be perfectly healthy and
    completely stale, and conflating the two would let a successful health
    check vouch for a projection that is three rebuilds behind.

    Every list is sorted by ID, and payload comparison is byte comparison of
    :meth:`~layered_memory_mcp.graph_models.GraphModel.to_json`, so a report is
    reproducible across runs and across stores.
    """

    backend: str = SQLITE_GRAPH_BACKEND
    expected_nodes: int = Field(default=0, ge=0)
    expected_edges: int = Field(default=0, ge=0)
    stored_nodes: int = Field(default=0, ge=0)
    stored_edges: int = Field(default=0, ge=0)
    #: Expected but not stored — the projection is behind, or a write was lost.
    missing_nodes: list[str] = Field(default_factory=list)
    missing_edges: list[str] = Field(default_factory=list)
    #: Stored but not expected — the projection kept something the source
    #: dropped, which is what a rebuild that was never run looks like.
    unexpected_nodes: list[str] = Field(default_factory=list)
    unexpected_edges: list[str] = Field(default_factory=list)
    #: Same ID, different payload. Named separately from missing/unexpected
    #: because "the value changed" and "the row is gone" have different causes.
    changed_nodes: list[str] = Field(default_factory=list)
    changed_edges: list[str] = Field(default_factory=list)
    #: ``schema_version_mismatch:<id|store>:<version>`` — a payload or the
    #: database itself written under a schema version other than the expected
    #: one. Reported, never migrated.
    schema_mismatches: list[str] = Field(default_factory=list)
    #: Problems that prevented the comparison itself (a corrupt row, a closed
    #: store). An audit that could not read everything must not read as clean.
    errors: list[str] = Field(default_factory=list)

    @property
    def drifted(self) -> bool:
        """Whether anything at all differs, including an unreadable row."""
        return bool(
            self.missing_nodes
            or self.missing_edges
            or self.unexpected_nodes
            or self.unexpected_edges
            or self.changed_nodes
            or self.changed_edges
            or self.schema_mismatches
            or self.errors
        )


# ---------------------------------------------------------------------------
# Row helpers — the only place domain objects and SQL rows meet
# ---------------------------------------------------------------------------

def _node_row(node: GraphNode) -> tuple[str, str, str, int, str]:
    """The persisted form of ``node``: indexable columns plus its payload."""
    return (
        node.id,
        node.kind.value,
        node.status.value,
        node.schema_version,
        node.to_json(),
    )


def _edge_row(edge: GraphEdge) -> tuple[str, str, str, str, str, int, str]:
    return (
        edge.id,
        edge.relation_type.value,
        edge.source_id,
        edge.target_id,
        edge.status.value,
        edge.schema_version,
        edge.to_json(),
    )


def _revalidated(model: type[Any], item: Any, operation: str) -> Any:
    """Return ``item`` re-run through its own validators, or refuse it.

    ``model(**dict(item))`` rather than ``model_validate(item)``: pydantic
    returns a same-class instance untouched, which is exactly the check being
    skipped here. Rebuilding from the field values applies every validator —
    the identifier rules, the validity-window rule, the provenance rules — so
    what reaches the database is a value the next read can revalidate.
    """
    if not isinstance(item, model):
        raise TypeError(
            f"{operation} takes a {model.__name__}; refusing to store a "
            f"{type(item).__name__} as one."
        )
    try:
        return model(**dict(item))
    except Exception as exc:
        raise InvalidPayloadError(
            f"{operation} refused {getattr(item, 'id', '?')!r}: {exc}"
        ) from exc


def _decode(model: type[GraphModel], item_id: str, payload: str) -> Any:
    """Re-validate a stored payload, or refuse it.

    The inverse of ``to_json``, and the reason a caller never sees raw JSON: a
    row that does not re-validate is a read failure with a name, not a dict
    passed off as a domain object.
    """
    try:
        return model.model_validate(json.loads(payload))
    except Exception as exc:
        raise CorruptPayloadError(
            f"stored {model.__name__} {item_id!r} did not re-validate: {exc}"
        ) from exc


class SQLiteGraphStore:
    """A persistent, embedded :class:`GraphStore` backed by one SQLite database.

    ``database`` is required and explicit — a path (``str`` or
    :class:`~pathlib.Path`), the literal ``":memory:"``, or an already-open
    :class:`sqlite3.Connection` the caller owns. There is no default, no
    environment variable and no config lookup, so this store can only ever
    touch a database someone named on purpose. A path whose parent directory
    does not exist is refused rather than created: creating directories for a
    graph backend is how a test or an import ends up writing into a user's home.

    ``nodes`` and ``edges`` seed the store through the normal write path, so a
    seeded store is indistinguishable from one built by calling the operations.
    """

    def __init__(
        self,
        database: str | Path | sqlite3.Connection,
        *,
        nodes: Iterable[GraphNode] = (),
        edges: Iterable[GraphEdge] = (),
    ) -> None:
        self._closed = False
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self._restore_isolation: str | None = None
        self._connection = self._open(database)
        try:
            self._initialize_schema()
            for node in nodes:
                self.create_node(node)
            for edge in edges:
                self.append_edge(edge)
        except BaseException:
            # A store that failed to initialize must not leave a usable handle
            # behind: half-open is the one state none of the methods document.
            self.close()
            raise

    # -- lifecycle -----------------------------------------------------------

    def _open(self, database: str | Path | sqlite3.Connection) -> sqlite3.Connection:
        """Adopt or open the connection, taking over transaction control.

        ``isolation_level = None`` turns off the driver's implicit transaction
        handling, which is what lets :meth:`_transaction` state its own
        ``BEGIN``/``COMMIT``/``ROLLBACK`` boundaries explicitly instead of
        inheriting whatever the driver decided. For a borrowed connection the
        previous value is remembered and restored by :meth:`close`, so taking
        over is reversible.
        """
        if isinstance(database, sqlite3.Connection):
            self._restore_isolation = database.isolation_level
            database.isolation_level = None
            return database

        target = str(database)
        if target != ":memory:":
            parent = Path(target).expanduser().parent
            if not parent.is_dir():
                raise StoreUnavailableError(
                    f"directory {str(parent)!r} does not exist; this store never "
                    "creates directories — create the target explicitly."
                )
            target = str(Path(target).expanduser())
        try:
            connection = sqlite3.connect(target, isolation_level=None)
        except sqlite3.Error as exc:
            raise StoreUnavailableError(
                f"could not open graph database {target!r}: {exc}"
            ) from exc
        return connection

    def close(self) -> None:
        """End this store's lifecycle. Idempotent, and writes nothing.

        A connection this store opened is closed; a borrowed one is left open
        with its original ``isolation_level`` restored, because closing a handle
        the caller still holds is not this store's decision. Either way the
        store itself is closed: every operation but :meth:`health` then raises
        :class:`GraphStoreClosedError`.
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self._owns_connection:
                self._connection.close()
            else:
                self._connection.isolation_level = self._restore_isolation
        except sqlite3.Error:
            # Closing is a teardown step; failing to tear down must not mask
            # whatever the caller was actually doing.
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> SQLiteGraphStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # NOTE: deliberately no ``__del__``. Garbage collection must never write to
    # or delete from a database (§10.3): an implicit flush at an unpredictable
    # moment is indistinguishable from data loss.

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise GraphStoreClosedError(
                "this graph store has been closed; open a new "
                "SQLiteGraphStore on the same database to read it again."
            )
        return self._connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One explicit write boundary: ``BEGIN IMMEDIATE`` … ``COMMIT``.

        ``IMMEDIATE`` takes the write lock up front, so two stores over one
        file fail fast instead of discovering the conflict at ``COMMIT``. Any
        exception — including a validation error raised mid-loop — rolls the
        whole statement group back, which is what makes a failed rebuild leave
        the previous graph exactly as it was.
        """
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    # -- schema --------------------------------------------------------------

    def _initialize_schema(self) -> None:
        """Create the tables and record the versions, idempotently.

        Runs in one transaction, so a database is never left with tables but no
        version record. An existing database with a *newer* layout is refused
        here (:class:`StoreSchemaError`) rather than used: reading unknown
        storage with today's code is a silent data loss.
        """
        with self._transaction() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            stored = self._read_meta(connection, META_STORE_SCHEMA_VERSION)
            if stored is not None and stored != str(STORE_SCHEMA_VERSION):
                raise StoreSchemaError(
                    f"database was written with store schema version {stored}, "
                    f"this process speaks {STORE_SCHEMA_VERSION}; refusing to "
                    "read it rather than guessing at the layout."
                )
            for key, value in (
                (META_STORE_SCHEMA_VERSION, str(STORE_SCHEMA_VERSION)),
                (META_GRAPH_SCHEMA_VERSION, str(GRAPH_SCHEMA_VERSION)),
                (META_BACKEND, SQLITE_GRAPH_BACKEND),
                # Seeded at 0 and only ever bumped by a committed write. An
                # existing database that predates the counter gets it here, at
                # 0, which is correct: no rebuild is mid-flight across an open.
                (META_GRAPH_REVISION, "0"),
            ):
                # INSERT OR IGNORE, not REPLACE: the recorded graph schema
                # version describes what is *already stored*. Overwriting it on
                # open would let a version bump erase the evidence that the
                # payloads predate it — exactly the mismatch health reports.
                connection.execute(
                    "INSERT OR IGNORE INTO graph_meta (key, value) VALUES (?, ?)",
                    (key, value),
                )

    @staticmethod
    def _read_meta(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute(
            "SELECT value FROM graph_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def schema_info(self) -> dict[str, Any]:
        """The recorded versions and backend name, as plain JSON types.

        Makes "what layout and payload version is this database on" answerable
        without reading SQL, which a drift audit and an operator both need.
        """
        connection = self._require_connection()
        return {
            META_BACKEND: self._read_meta(connection, META_BACKEND),
            META_STORE_SCHEMA_VERSION: int(
                self._read_meta(connection, META_STORE_SCHEMA_VERSION) or 0
            ),
            META_GRAPH_SCHEMA_VERSION: int(
                self._read_meta(connection, META_GRAPH_SCHEMA_VERSION) or 0
            ),
        }

    # -- the durable revision: one counter, bumped by every committed write ---

    @staticmethod
    def _read_revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM graph_meta WHERE key = ?", (META_GRAPH_REVISION,)
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            # An unreadable counter must not read as "nothing has changed".
            raise CorruptPayloadError(
                f"graph revision {row[0]!r} is not an integer; refusing to "
                "compare a rebuild against a counter this store cannot read."
            ) from None

    @staticmethod
    def _bump_revision(connection: sqlite3.Connection) -> None:
        """Advance the counter inside the caller's open write transaction.

        One statement, run under the same ``BEGIN IMMEDIATE`` as the rows it
        describes, so the counter and the graph move together or not at all. A
        read-modify-write in Python would be two statements and a window.
        """
        connection.execute(
            """
            INSERT INTO graph_meta (key, value) VALUES (?, '1')
            ON CONFLICT(key) DO UPDATE SET
                value = CAST(CAST(graph_meta.value AS INTEGER) + 1 AS TEXT)
            """,
            (META_GRAPH_REVISION,),
        )

    def revision(self) -> int:
        """How many writes this database has committed, as a comparable token.

        The value a caller captures before deriving a replacement graph and
        hands back to :meth:`rebuild` as ``expected_revision``. It is opaque and
        monotonic: it counts committed mutations, not nodes, and an idempotent
        re-append still moves it — a conservative counter refuses a few rebuilds
        that would have been harmless, while a content-sensitive one would have
        to be right about every way two graphs can differ.
        """
        return self._read_revision(self._require_connection())

    # -- writes --------------------------------------------------------------

    def create_node(self, node: GraphNode) -> GraphNode:
        """Insert or replace ``node`` by ID and return the stored value.

        An upsert, as §9.1 specifies: node IDs are stable for a source identity,
        so re-running an import updates one row rather than accumulating copies.
        Nothing is laundered on the way in: a non-``GraphNode`` is refused
        (``TypeError``), a ``GraphNode`` that does not pass its own validators
        is refused (:class:`InvalidPayloadError`), and the payload's own
        ``schema_version`` is stored as given — so this call can neither store
        an illegal object nor quietly restamp an old one with today's schema
        version.
        """
        stored: GraphNode = _revalidated(GraphNode, node, "create_node")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO graph_nodes (id, kind, status, schema_version, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    status = excluded.status,
                    schema_version = excluded.schema_version,
                    payload = excluded.payload
                """,
                _node_row(stored),
            )
            self._bump_revision(connection)
        return stored.model_copy(deep=True)

    def append_edge(self, edge: GraphEdge) -> GraphEdge:
        """Append ``edge`` and return the stored value.

        Append-only (§6.2), with exactly the Batch C rules — the withdrawal
        check is *imported* from the reference store rather than restated, so
        the two backends cannot drift on what an edge is allowed to do:

        * a new edge ID is stored as given;
        * an identical re-append is a no-op, so replaying an import stream is
          idempotent and cannot produce a second row for one ID;
        * a re-append that *changes* the assertion is refused with
          :class:`EdgeConflictError`, except for a withdrawal
          (``active`` → ``retracted``/``expired``).

        One rule this backend adds: both endpoints must already be stored.
        A single deliberate assertion has no reason to dangle, and the error
        names the missing end (:class:`MissingEndpointError`).
        """
        stored: GraphEdge = _revalidated(GraphEdge, edge, "append_edge")
        # The two refusals below are inside the transaction on purpose: an
        # append is one decision, so "is this a legal re-assertion" and "do both
        # endpoints exist" must be answered against the same state the row is
        # written into, and a refusal must leave nothing behind.
        with self._transaction() as connection:
            existing = self._fetch_edge(connection, stored.id)
            if existing is not None and existing != stored:
                InMemoryGraphStore._check_withdrawal(existing, stored)

            for endpoint in (stored.source_id, stored.target_id):
                if self._fetch_node(connection, endpoint) is None:
                    raise MissingEndpointError(
                        f"edge {stored.id!r} points at {endpoint!r}, which is not "
                        "stored; create both endpoint nodes before asserting a "
                        "relation between them."
                    )

            connection.execute(
                """
                INSERT INTO graph_edges (
                    id, relation_type, source_id, target_id,
                    status, schema_version, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    relation_type = excluded.relation_type,
                    source_id = excluded.source_id,
                    target_id = excluded.target_id,
                    status = excluded.status,
                    schema_version = excluded.schema_version,
                    payload = excluded.payload
                """,
                _edge_row(stored),
            )
            self._bump_revision(connection)
        return stored.model_copy(deep=True)

    # -- reads ---------------------------------------------------------------

    def _fetch_node(
        self, connection: sqlite3.Connection, node_id: str
    ) -> GraphNode | None:
        row = connection.execute(
            "SELECT id, payload FROM graph_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return None if row is None else _decode(GraphNode, row[0], row[1])

    def _fetch_edge(
        self, connection: sqlite3.Connection, edge_id: str
    ) -> GraphEdge | None:
        row = connection.execute(
            "SELECT id, payload FROM graph_edges WHERE id = ?", (edge_id,)
        ).fetchone()
        return None if row is None else _decode(GraphEdge, row[0], row[1])

    def get_node(self, node_id: str) -> GraphNode | None:
        """Return the node with ``node_id``, or ``None`` if it is not stored.

        "Not stored" is a normal answer (§9.1). "Cannot read" is not: a closed
        store and a corrupt payload both raise.
        """
        return self._fetch_node(self._require_connection(), node_id)

    def get_edges(
        self,
        node_id: str,
        relation_types: Sequence[RelationType] | None = None,
        direction: EdgeDirection = "both",
    ) -> list[GraphEdge]:
        """Return ``node_id``'s edges, optionally narrowed by type/direction.

        The raw relation reader, matching Batch C: withdrawn and expired edges
        come back too, because status and validity are *traversal* filters
        (§7.3 step 4) and "what was ever asserted about this node" has to be
        answerable. An unknown ``node_id`` yields ``[]``.

        Ordering is applied in Python with the reference store's own sort key,
        never left to SQLite: a database is free to return rows in whatever
        order its query plan produced, and a result a fixture can assert on
        cannot depend on that.
        """
        if direction not in _DIRECTION_PREDICATES:
            raise ValueError(
                f"direction must be one of {tuple(_DIRECTION_PREDICATES)}, "
                f"got {direction!r}"
            )
        connection = self._require_connection()
        predicate = _DIRECTION_PREDICATES[direction]
        parameters: list[Any] = [node_id] * predicate.count("?")
        query = f"SELECT id, payload FROM graph_edges WHERE ({predicate})"

        wanted = list(relation_types or ())
        if wanted:
            # Placeholders are generated from the *count* of relation types and
            # the values are bound; no caller-supplied text enters the SQL.
            query += " AND relation_type IN (%s)" % ", ".join("?" * len(wanted))
            parameters.extend(RelationType(item).value for item in wanted)

        found = [
            _decode(GraphEdge, row[0], row[1])
            for row in connection.execute(query, parameters).fetchall()
        ]
        return sorted(found, key=_edge_sort_key)

    def _load_graph(
        self, *, strict: bool = True
    ) -> tuple[dict[str, GraphNode], dict[str, GraphEdge], list[str]]:
        """Read every row back into validated domain objects.

        ``strict`` picks who hears about a corrupt row: a read operation raises
        (:class:`CorruptPayloadError`), while :meth:`health` and
        :meth:`audit_drift` ask for ``strict=False`` and get the bad IDs in a
        list — because the one caller who needs to know *which* rows are broken
        is the one diagnosing the store.
        """
        connection = self._require_connection()
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        errors: list[str] = []

        for model, table, sink in (
            (GraphNode, "graph_nodes", nodes),
            (GraphEdge, "graph_edges", edges),
        ):
            # The only interpolated SQL in this module, and ``table`` is one of
            # the two literals in the loop above — never a caller value.
            rows = connection.execute(
                f"SELECT id, payload FROM {table} ORDER BY id"  # noqa: S608
            ).fetchall()
            for item_id, payload in rows:
                try:
                    sink[item_id] = _decode(model, item_id, payload)
                except CorruptPayloadError:
                    if strict:
                        raise
                    errors.append(f"corrupt_payload:{item_id}")
        return nodes, edges, sorted(errors)

    def _projection(self) -> InMemoryGraphStore:
        """The persisted graph, loaded into the Batch C reference store.

        This is where the multi-hop semantics come from. It is built fresh on
        every call — a cached projection would answer from a graph the database
        has moved on from — through the reference store's own seeding path, so
        nothing here depends on its internals.

        Note that the reference store's ``append_edge`` imposes no endpoint
        rule, which is what lets a dangling edge that §10.2 required a rebuild
        to *keep* be loaded back and reported rather than silently lost.
        """
        nodes, edges, _ = self._load_graph()
        return InMemoryGraphStore(nodes=nodes.values(), edges=edges.values())

    def traverse(
        self,
        seed_ids: Sequence[str],
        max_hops: int = 1,
        filters: TraversalFilters | None = None,
    ) -> TraversalResult:
        """Expand from ``seed_ids`` under hard filters, bounded by ``max_hops``.

        Delegated verbatim to the Batch C reference semantics (§7.3 step 4):
        filters apply *during* expansion so a filtered-out node cannot act as a
        stepping stone, ordering is total and insertion-independent, and the
        budget, the unknown seeds and the weak-relation skips are reported
        rather than silently applied.
        """
        return self._projection().traverse(seed_ids, max_hops, filters)

    def find_conflicts(
        self,
        scope: GraphScope | None = None,
        entity_ids: Sequence[str] | None = None,
    ) -> list[ConflictReport]:
        """Return unresolved contradictions in ``scope`` / about ``entity_ids``.

        Same §8.3 reading as the reference store, for the same reason the
        traversal is: a backend that disagreed with it about when a
        contradiction stops contesting would make "is this settled?" depend on
        where the graph happened to be stored.
        """
        return self._projection().find_conflicts(scope, entity_ids)

    def get_provenance(self, node_id: str) -> ProvenanceRecord:
        """Return the evidence and derivation lineage behind ``node_id``.

        Raises :class:`~layered_memory_mcp.graph_fixture_store.UnknownNodeError`
        for a node that is not stored — a blank provenance record would read
        exactly like one for a node with no evidence — and reports every
        unresolvable reference as a warning instead of inventing a resolution.
        """
        return self._projection().get_provenance(node_id)

    def snapshot(self) -> dict[str, Any]:
        """The whole store as plain JSON types, in one stable order.

        The same shape :meth:`InMemoryGraphStore.snapshot` produces, so the two
        backends holding one graph compare equal — which is what makes "does
        the embedded store agree with the reference" a single assertion.
        """
        nodes, edges, _ = self._load_graph()
        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "nodes": [nodes[key].to_dict() for key in sorted(nodes)],
            "edges": [edges[key].to_dict() for key in sorted(edges)],
        }

    # -- rebuild -------------------------------------------------------------

    def rebuild(
        self,
        source_events: Sequence[MemoryEvent],
        *,
        expected_revision: int | None = None,
    ) -> RebuildReport:
        """Project graph state from an append-only event stream, idempotently.

        Three stages, and both splits are the point:

        1. **Staging.** The projection is built in a throwaway in-memory
           reference store, which is also where the §10.2 reporting rules live
           (duplicate replay, conflicting payloads for one ID, unresolvable
           edge endpoints, inapplicable transitions). Nothing touches the
           database while the stream is being judged.
        2. **Compare-and-set.** Inside the publish transaction, before anything
           is deleted, the stored :meth:`revision` is checked against the one
           this candidate was derived from. A candidate staged before another
           connection committed is *stale*, and publishing it would silently
           delete that newer state; :class:`StaleRebuildError` is raised
           instead and the transaction rolls back untouched.
        3. **Atomic replacement.** The same ``BEGIN IMMEDIATE`` deletes the
           current rows, writes the staged ones and bumps the revision. Any
           failure rolls back, so a failed rebuild leaves the *previous* graph
           exactly as it was — not an empty graph, and never a half-replaced
           one.

        Idempotent in both directions: the same stream produces the same rows,
        the same byte-identical payloads and the same report, and the report's
        counts describe the resulting projection rather than the attempt.

        A rebuild with ``errors`` is rejected before the transaction starts,
        matching the reference store's fail-closed boundary: a malformed ledger
        must not replace the last known-good projection. The candidate counts
        and errors remain visible in :attr:`RebuildReport`; the persisted graph
        and its health stay unchanged.

        Args:
            source_events: The complete stream to project. Anything omitted is
                erased, which is why the caller must hand over the whole ledger.
            expected_revision: The :meth:`revision` this candidate was derived
                from. Omit it and the revision is captured at the top of this
                call, which closes the staging window this method owns. Pass it
                when the candidate was derived earlier — a caller that read a
                journal, planned, and only then called ``rebuild`` owns a wider
                window than this method can see, and naming the revision it read
                at is how that window is closed too.

        Raises:
            StaleRebuildError: The graph moved after the candidate was derived.
                Nothing was deleted, written or counted; re-derive from the
                current ledger and call again.
        """
        base_revision = (
            self.revision() if expected_revision is None else int(expected_revision)
        )
        staging = InMemoryGraphStore()
        report = staging.rebuild(source_events)
        if not report.ok:
            return report
        # The staged graph is handed over as its snapshot and revalidated, so
        # the replacement reads its input through the same contract any other
        # caller would — and a staged payload that would not survive a read is
        # refused before the current graph is touched.
        staged = staging.snapshot()
        node_rows = [
            _node_row(GraphNode.model_validate(payload))
            for payload in staged["nodes"]
        ]
        edge_rows = [
            _edge_row(GraphEdge.model_validate(payload))
            for payload in staged["edges"]
        ]

        with self._transaction() as connection:
            # The compare, under the write lock and before the first DELETE.
            # ``BEGIN IMMEDIATE`` is what makes this a compare-and-*set* rather
            # than a check: no other writer can commit between this read and
            # the COMMIT below, so a candidate that passes here is published
            # onto exactly the graph it was derived from.
            current_revision = self._read_revision(connection)
            if current_revision != base_revision:
                raise StaleRebuildError(
                    f"this rebuild was staged at graph revision {base_revision} "
                    f"and the stored graph is now at {current_revision}; "
                    "publishing it would delete state committed since. Nothing "
                    "was changed — re-read the source ledger and rebuild from it."
                )
            connection.execute("DELETE FROM graph_edges")
            connection.execute("DELETE FROM graph_nodes")
            connection.executemany(
                """
                INSERT INTO graph_nodes (id, kind, status, schema_version, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                node_rows,
            )
            connection.executemany(
                """
                INSERT INTO graph_edges (
                    id, relation_type, source_id, target_id,
                    status, schema_version, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                edge_rows,
            )
            self._bump_revision(connection)
        return report

    # -- health and drift ----------------------------------------------------

    def health(self) -> GraphHealth:
        """Report whether the graph can answer queries. Never raises.

        It is the thing a caller asks when something else already failed, so
        every failure mode it could hit becomes an error string instead of an
        exception:

        * ``store_closed`` — the lifecycle has ended; counts are unknown, and
          reporting 0/0 as if the graph were empty would be a lie.
        * ``corrupt_payload:<id>`` — a row that no longer re-validates.
        * ``dangling_edge:<edge>:<node>`` and
          ``schema_version_mismatch:<id>:<version>`` — the reference store's own
          integrity checks, run over what was loaded.
        * ``schema_version_mismatch:store:<version>`` — the database records a
          payload schema version this process does not speak.
        * ``health_check_failed:<Error>`` — anything else, named rather than
          raised.

        Counts come from ``COUNT(*)``, not from the loaded projection, so they
        stay truthful even when some rows cannot be decoded.
        """
        if self._closed:
            return GraphHealth(
                backend=SQLITE_GRAPH_BACKEND,
                detail="store is closed; reopen the database to query it",
                errors=["store_closed"],
            )

        errors: list[str] = []
        node_count = edge_count = 0
        schema_version = GRAPH_SCHEMA_VERSION
        try:
            connection = self._require_connection()
            node_count = int(
                connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
            )
            edge_count = int(
                connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
            )
            recorded = self._read_meta(connection, META_GRAPH_SCHEMA_VERSION)
            if recorded is not None:
                schema_version = int(recorded)
                if schema_version != GRAPH_SCHEMA_VERSION:
                    errors.append(f"schema_version_mismatch:store:{schema_version}")

            nodes, edges, load_errors = self._load_graph(strict=False)
            errors.extend(load_errors)
            projection = InMemoryGraphStore(
                nodes=nodes.values(), edges=edges.values()
            )
            errors.extend(projection.health().errors)
        except Exception as exc:  # pragma: no cover - defensive, must not raise
            errors.append(f"health_check_failed:{type(exc).__name__}")

        return GraphHealth(
            backend=SQLITE_GRAPH_BACKEND,
            node_count=node_count,
            edge_count=edge_count,
            schema_version=schema_version,
            detail=_HEALTH_DETAIL,
            errors=sorted(set(errors)),
        )

    def audit_drift(
        self,
        expected_nodes: Iterable[GraphNode] = (),
        expected_edges: Iterable[GraphEdge] = (),
        *,
        expected_schema_version: int = GRAPH_SCHEMA_VERSION,
    ) -> GraphDriftReport:
        """Compare the persisted projection against an expected source (§10.2).

        An extra capability of this backend, not part of the §9.1 contract:
        widening the ``GraphStore`` protocol for it would make every future
        adapter owe an audit it may not be able to implement. Taking the
        expectation as explicit node/edge sequences keeps the audit usable
        against anything that can produce them — a replayed ledger, an export,
        the reference store's own contents.

        What it detects, each sorted by ID: a missing node/edge, an unexpected
        one, one whose payload differs byte-for-byte, and any payload or
        database schema version other than ``expected_schema_version``. It never
        repairs anything: a drift report is a finding, and deciding to rebuild
        is the caller's.

        Never raises. A closed store or a corrupt row lands in ``errors``, which
        on its own is enough to clear :attr:`GraphDriftReport.drifted` — an
        audit that could not read everything must not read as clean.
        """
        expected_node_payloads = {node.id: node.to_json() for node in expected_nodes}
        expected_edge_payloads = {edge.id: edge.to_json() for edge in expected_edges}
        report = GraphDriftReport(
            expected_nodes=len(expected_node_payloads),
            expected_edges=len(expected_edge_payloads),
        )
        if self._closed:
            return report.model_copy(update={"errors": ["store_closed"]})

        errors: list[str] = []
        mismatches: list[str] = []
        try:
            nodes, edges, load_errors = self._load_graph(strict=False)
            errors.extend(load_errors)
            connection = self._require_connection()
            recorded = self._read_meta(connection, META_GRAPH_SCHEMA_VERSION)
            if recorded is not None and int(recorded) != expected_schema_version:
                mismatches.append(f"schema_version_mismatch:store:{recorded}")
        except Exception as exc:
            return report.model_copy(
                update={"errors": [f"drift_audit_failed:{type(exc).__name__}"]}
            )

        stored_node_payloads = {key: node.to_json() for key, node in nodes.items()}
        stored_edge_payloads = {key: edge.to_json() for key, edge in edges.items()}
        for item in (*nodes.values(), *edges.values()):
            if item.schema_version != expected_schema_version:
                mismatches.append(
                    f"schema_version_mismatch:{item.id}:{item.schema_version}"
                )

        return report.model_copy(
            update={
                "stored_nodes": len(nodes),
                "stored_edges": len(edges),
                "missing_nodes": _absent(expected_node_payloads, stored_node_payloads),
                "missing_edges": _absent(expected_edge_payloads, stored_edge_payloads),
                "unexpected_nodes": _absent(
                    stored_node_payloads, expected_node_payloads
                ),
                "unexpected_edges": _absent(
                    stored_edge_payloads, expected_edge_payloads
                ),
                "changed_nodes": _changed(
                    expected_node_payloads, stored_node_payloads
                ),
                "changed_edges": _changed(
                    expected_edge_payloads, stored_edge_payloads
                ),
                "schema_mismatches": sorted(set(mismatches)),
                "errors": sorted(set(errors)),
            }
        )


def _absent(left: dict[str, str], right: dict[str, str]) -> list[str]:
    """IDs present in ``left`` and not in ``right``, sorted."""
    return sorted(key for key in left if key not in right)


def _changed(expected: dict[str, str], stored: dict[str, str]) -> list[str]:
    """IDs both sides hold with different payload bytes, sorted."""
    return sorted(
        key
        for key, payload in expected.items()
        if key in stored and stored[key] != payload
    )

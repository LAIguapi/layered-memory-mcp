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
* :func:`diagnose_graph_runtime` — the read-only operator view: is a graph
  configured, can it be opened, is it healthy, and how much of a given result
  set currently carries graph identity.

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
  into somebody's home.
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

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from pydantic import Field

from .graph_models import GraphModel
from .graph_runtime import GraphModeDecision, build_seed_mapping
from .graph_sqlite_store import SQLITE_GRAPH_BACKEND, SQLiteGraphStore

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


class GraphAssemblyReport(GraphModel):
    """What one assembly attempt did, as bounded, serializable data.

    ``path_configured`` is deliberately a boolean rather than the path: an
    operator needs to know *whether* a database was declared, and a report that
    carried the location could not be logged next to a memory answer (§15).

    ``store_opened`` and ``healthy`` are separate for the reason
    :meth:`~layered_memory_mcp.graph_sqlite_store.SQLiteGraphStore.health`
    exists: an open connection to a database holding a corrupt payload is open
    and untrustworthy at the same time.
    """

    mode: str = "legacy"
    backend: str = ""
    status: str = GRAPH_STORE_NOT_REQUESTED
    path_configured: bool = False
    store_opened: bool = False
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
        # project an event stream into this store.
        "supports_rebuild": callable(getattr(store, "rebuild", None)),
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
# Read-only diagnostics (§12 Batch I scope item 4)
# ---------------------------------------------------------------------------

def graph_assembly_payload(report: GraphAssemblyReport) -> dict[str, Any]:
    """The bounded, plain-JSON view of one assembly report.

    A fixed key set, so a consumer can depend on the shape, and plain types
    only, so it can be serialized, logged or returned beside an MCP response
    without carrying anything it should not.
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

    Strictly read-only. It opens a store only when ``require=True``, closes it
    again, and never writes, rebuilds, imports, repairs or re-ranks anything:
    the two questions it answers are both observations, and a diagnostic that
    changed state would be measuring itself.

    It also decides nothing semantic. Mapping coverage is a *count* of rows
    carrying graph identity — whether those rows are right, current or in scope
    is the graph's question and the reviewer's, not a metric's (§13).

    Args:
        config: The configuration to read the path and mode from.
        results: Ranked legacy result payloads, each with the ``file`` identity
            the recall path emits. Rows without one are counted as
            ``results_without_identity`` rather than dropped silently.
        require: Whether to actually open the configured database. ``False``
            reports the configuration without touching a file.

    Returns:
        ``{"status", "fallback_reason", "assembly", "mapping"}`` — counts,
        statuses and bounded labels only (§15): no path, no memory content, no
        metadata dump, no backend message.
    """
    with graph_store_session(config, require=require) as assembly:
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
    "GRAPH_FALLBACK_NONE",
    "GRAPH_MODE_LEGACY_WARNING",
    "GRAPH_SEEDS_UNMAPPED",
    "GRAPH_STORE_NOT_CONFIGURED",
    "GRAPH_STORE_NOT_REQUESTED",
    "GRAPH_STORE_OPENED",
    "GRAPH_STORE_OPEN_FAILED",
    "GRAPH_STORE_UNHEALTHY",
    "GraphAssemblyReport",
    "GraphRuntimeAssembly",
    "diagnose_graph_runtime",
    "graph_assembly_payload",
    "graph_store_session",
    "open_graph_store",
    "resolve_graph_database_path",
]

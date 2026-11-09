"""The one application layer between a semantic decision and every store.

WHAT THIS MODULE IS
-------------------
ARCHITECTURE_v2.md "Framework completion and migration delivery contract", in
code. Every graph-mode write — from MCP, from a batch, from anywhere — goes
through :func:`commit_semantic_write`, which does exactly this, in this order:

1. **Validate, closed.** The request's node must *be* the content the caller is
   publishing (the binding), the readable surface must be one this framework
   supports and owns, the lifecycle transition must apply to a target that can
   take it, and the complete proposed replay must project cleanly. Every one of
   these refuses **before** the journal, the graph, L1, the vector rows and L0
   are touched.
2. **Commit, once.** The validated events are appended to the journal under a
   stable request identity. That append is the authority: an identical retry is
   a no-op, a changed payload under the same identity is refused, and two
   writers serialize.
3. **Publish, honestly.** Graph, L1, vector and L0 are published in dependency
   order, and each checkpoint advances only after its own projection actually
   succeeded. A projection that fails leaves ``committed=true`` with
   ``projections_pending`` — never a pretended all-store transaction, and never
   a discarded accepted decision.

:func:`recover_projections` is the explicit way to finish the pending ones, and
:func:`runtime_status` is the bounded operator view of both halves.

WHAT IT IS NOT
--------------
- NOT an extractor or a decision layer. Nothing here reads content to derive a
  node, an entity, a claim, a supersession or a target. The caller Agent decides
  and this layer validates. There is no similarity, no model and no heuristic.
- NOT a merger of legacy semantics. The legacy dedup/fusion handshake is not
  consulted and not applied: an explicit graph write is a different contract,
  and quietly running one through the other is how the two would disagree.
- NOT a transaction across stores. Four stores and a filesystem cannot share
  one, and this module states its boundaries rather than implying one.
- NOT an adopter. An unknown pre-existing graph with an empty journal is
  refused; an unadopted or externally edited surface is refused. Nothing the
  framework did not publish is silently taken over or erased.

PRIVACY (§15)
-------------
:class:`SemanticCommit`, :class:`RecoveryReport` and :func:`runtime_status`
carry statuses, counts, positions, graph/entry identities and bounded labels.
Never a database path, never a section body, never a backend message — a
failure is named by its class or by a refusal token.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from filelock import FileLock
from pydantic import Field

from . import graph_projection
from .graph_entry_identity import ProjectionIdentityError
from .graph_fixture_store import InMemoryGraphStore
from .graph_journal import (
    PROJECTION_GRAPH,
    PROJECTION_L0,
    PROJECTION_L1,
    PROJECTION_NAMES,
    PROJECTION_TODO,
    PROJECTION_VECTOR,
    REQUEST_KIND_BOOTSTRAP,
    REQUEST_KIND_SEMANTIC_WRITE,
    JournalConflictError,
    MemoryEventJournal,
    canonical_request_hash,
)
from .graph_models import GraphEdge, GraphModel, GraphNode, MemoryEvent
from .graph_projection import ProjectionBinding, ProjectionError
from .graph_runtime import GRAPH_WRITE_NOT_CONFIGURED, GraphModeDecision
from .graph_runtime_assembly import (
    GRAPH_STORE_OPENED,
    graph_assembly_payload,
    graph_store_readonly_session,
    graph_store_session,
    journal_readonly_session,
    journal_session,
    resolve_graph_database_path,
    resolve_journal_database_path,
)
from .graph_store import missing_graph_store_operations
from .graph_task import TaskMutation, TaskRecord
from .graph_write import (
    SUPERSEDABLE_STATUSES,
    REVIEW_EVENT_NAMESPACE,
    SemanticLifecycleRequest,
    SemanticWriteRequest,
    plan_semantic_lifecycle,
    plan_semantic_write,
)

__all__ = [
    "GRAPH_COMMIT_COMMITTED",
    "GRAPH_COMMIT_DECISION_REQUIRED",
    "GRAPH_COMMIT_NOT_CONFIGURED",
    "GRAPH_COMMIT_PENDING_PROJECTIONS",
    "GRAPH_COMMIT_REJECTED_ADOPTION",
    "GRAPH_COMMIT_REJECTED_BINDING",
    "GRAPH_COMMIT_REJECTED_DRIFT",
    "GRAPH_COMMIT_REJECTED_INVALID",
    "GRAPH_COMMIT_REJECTED_REQUEST",
    "GRAPH_COMMIT_REJECTED_SURFACE",
    "GRAPH_COMMIT_REJECTED_UNSUPPORTED_SURFACE",
    "GRAPH_TASK_AUTHORITY_UNSEEDED",
    "GRAPH_TASK_UNKNOWN",
    "PROJECTION_DEPENDENCIES",
    "PROJECTION_STORE_ABSENT",
    "RECOVERY_INCOMPLETE",
    "RECOVERY_NOTHING_PENDING",
    "RECOVERY_NOT_CONFIGURED",
    "RECOVERY_OK",
    "TASK_SEED_APPLIED",
    "TASK_SEED_NOTHING_TO_DO",
    "TASK_SEED_PREVIEW",
    "ProjectionBinding",
    "RecoveryReport",
    "SemanticCommit",
    "TaskAuthority",
    "TaskSeedReport",
    "commit_semantic_lifecycle",
    "commit_semantic_write",
    "commit_task_mutation",
    "commit_payload",
    "pending_surface_entry_ids",
    "recover_projections",
    "runtime_status",
    "seed_task_authority",
    "stale_surface_entry_ids",
    "task_authority",
]

# ---------------------------------------------------------------------------
# Statuses. "Refused", "committed" and "committed but unfinished" must never
# read the same in a response or a log.
# ---------------------------------------------------------------------------

#: Committed, and every managed projection published.
GRAPH_COMMIT_COMMITTED = "graph_write_committed"

#: Committed — the decision is durable — and at least one projection has not
#: published yet. Recoverable, and visibly incomplete.
GRAPH_COMMIT_PENDING_PROJECTIONS = "graph_write_committed_projections_pending"

#: A graph write mode reached an entrance with no explicit semantic decision.
#: Refused before any legacy write: a write mode does not fall back.
GRAPH_COMMIT_DECISION_REQUIRED = "graph_write_decision_required"

#: A write mode without a configured journal or graph database. A bounded
#: refusal, not a guessed location and not a silent legacy mutation.
#:
#: Deliberately *the same token* the runtime seam already uses for "the mode
#: allows a graph write and no backend is configured": one meaning, one string,
#: imported rather than restated so the two cannot drift.
GRAPH_COMMIT_NOT_CONFIGURED = GRAPH_WRITE_NOT_CONFIGURED

#: The request's node is not the content being published.
GRAPH_COMMIT_REJECTED_BINDING = "graph_write_rejected_binding_mismatch"

#: The readable surface is not one this framework can own (today: a REST-owned
#: frontmatter entry file).
GRAPH_COMMIT_REJECTED_UNSUPPORTED_SURFACE = "graph_write_rejected_unsupported_surface"

#: The surface already has a current owner this decision does not supersede.
GRAPH_COMMIT_REJECTED_SURFACE = "graph_write_rejected_surface_conflict"

#: The proposed complete replay is not a valid projection — an identity,
#: scope, lifecycle or edge conflict.
GRAPH_COMMIT_REJECTED_INVALID = "graph_write_rejected_invalid_replay"

#: The same request identity was already committed with a different payload.
GRAPH_COMMIT_REJECTED_REQUEST = "graph_write_rejected_request_conflict"

#: The target surface was written or edited by somebody else.
GRAPH_COMMIT_REJECTED_DRIFT = "graph_write_rejected_surface_drift"

#: A graph exists that this journal never produced. Refused, never erased.
GRAPH_COMMIT_REJECTED_ADOPTION = "graph_write_rejected_unknown_graph"

#: The task a mutation names is not one the authority holds. A refusal before
#: the journal: nothing is appended, so a mistyped id leaves no partial event.
GRAPH_TASK_UNKNOWN = "graph_task_unknown"

#: The readable TODO store holds tasks this journal has no authority for. The
#: mutation is refused *before* the journal rather than committed into a
#: projection that would then refuse to publish — the operator's next step is
#: the explicit seed, not a retry.
GRAPH_TASK_AUTHORITY_UNSEEDED = "graph_task_authority_not_seeded"

RECOVERY_OK = "graph_recovery_complete"
RECOVERY_INCOMPLETE = "graph_recovery_incomplete"
RECOVERY_NOTHING_PENDING = "graph_recovery_nothing_pending"
RECOVERY_NOT_CONFIGURED = "graph_recovery_not_configured"

TASK_SEED_APPLIED = "graph_task_seed_applied"
TASK_SEED_PREVIEW = "graph_task_seed_preview"
TASK_SEED_NOTHING_TO_DO = "graph_task_seed_nothing_to_do"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class SemanticCommit(GraphModel):
    """What one semantic decision became. Two separate claims, never conflated.

    ``committed`` is about the *authority* only: the decision is durable in the
    journal. ``projections_pending`` is about the readable world. A caller that
    reads only ``committed`` gets the truthful answer to "was my decision
    accepted"; a caller that reads only ``projections_pending`` gets the
    truthful answer to "can a reader see it yet".
    """

    status: str
    committed: bool = False
    duplicate: bool = False
    request_id: str = ""
    node_id: str = ""
    entry_id: str = ""
    event_ids: list[str] = Field(default_factory=list)
    head_position: int = Field(default=0, ge=0)
    checkpoints: dict[str, int] = Field(default_factory=dict)
    projections_pending: list[str] = Field(default_factory=list)
    superseded_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    #: Whether the readable L1 surface this decision publishes to actually
    #: carries it now. Separate from ``committed`` on purpose: a commit whose
    #: file write failed is durable and *not* readable, and a response in which
    #: those two were one field is exactly how a surface whose bytes never
    #: landed became indistinguishable from one that published cleanly.
    surface_published: bool = False

    @property
    def ok(self) -> bool:
        return self.committed and not self.projections_pending


class RecoveryReport(GraphModel):
    """What explicit recovery finished, and what is still outstanding."""

    status: str = RECOVERY_NOT_CONFIGURED
    head_position: int = Field(default=0, ge=0)
    recovered: list[str] = Field(default_factory=list)
    checkpoints: dict[str, int] = Field(default_factory=dict)
    projections_pending: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def commit_payload(commit: SemanticCommit) -> dict[str, Any]:
    """The bounded response block one commit contributes to an entry point."""
    return {
        "status": commit.status,
        "committed": commit.committed,
        "duplicate": commit.duplicate,
        "node_id": commit.node_id,
        "entry_id": commit.entry_id,
        "surface_published": commit.surface_published,
        "event_ids": list(commit.event_ids),
        "superseded_ids": list(commit.superseded_ids),
        "head_position": commit.head_position,
        "checkpoints": dict(commit.checkpoints),
        "projections_pending": list(commit.projections_pending),
        "warnings": list(commit.warnings),
        "errors": list(commit.errors),
    }


# ---------------------------------------------------------------------------
# Handles
# ---------------------------------------------------------------------------

#: Suffix of the explicit cross-process serialization lock, taken beside the
#: journal database. The journal's own ``BEGIN IMMEDIATE`` serializes *appends*;
#: this lock serializes the whole validate → append → publish → checkpoint
#: sequence, which is the part that must not interleave: two writers publishing
#: concurrently could otherwise leave a checkpoint claiming a position whose
#: events a *staler* rebuild had just replaced. This is an explicit serialized
#: lock with recoverable checkpoints — deliberately not a pretended distributed
#: transaction across four stores.
PROJECTION_LOCK_SUFFIX = ".publish.lock"

#: How long a writer waits for the publication lock before giving up.
PROJECTION_LOCK_TIMEOUT_SECONDS = 60.0


@contextmanager
def _handles(
    config: Any, store: Any | None = None
) -> Iterator[tuple[MemoryEventJournal | None, Any | None]]:
    """Serialize, then open the journal and graph store for one operation.

    An injected ``store`` is caller-owned and is never closed here; everything
    this function opened is closed in the ``finally``, on success and on error.
    """
    with _publication_lock(config):
        with journal_session(config, require=True) as journal_assembly:
            if store is not None:
                yield journal_assembly.journal, store
                return
            with graph_store_session(config, require=True) as graph_assembly:
                opened = (
                    graph_assembly.store
                    if graph_assembly.report.status == GRAPH_STORE_OPENED
                    else None
                )
                yield journal_assembly.journal, opened


@contextmanager
def _publication_lock(config: Any) -> Iterator[None]:
    """Hold the explicit write/publish lock, or run unlocked when unconfigured.

    No journal path means the operation is about to refuse anyway, so there is
    nothing to serialize and no lock file is created.
    """
    path = resolve_journal_database_path(config)
    if path is None or not path.parent.is_dir():
        yield
        return
    lock = FileLock(
        str(path.with_name(path.name + PROJECTION_LOCK_SUFFIX)),
        timeout=PROJECTION_LOCK_TIMEOUT_SECONDS,
    )
    with lock:
        yield


def _rejected(status: str, *, errors: Sequence[str] = (), **fields: Any) -> SemanticCommit:
    return SemanticCommit(status=status, errors=sorted(set(errors)), **fields)


# ---------------------------------------------------------------------------
# The commit
# ---------------------------------------------------------------------------

def commit_semantic_write(
    config: Any,
    request: SemanticWriteRequest,
    binding: ProjectionBinding | None,
    *,
    request_id: str = "",
    store: Any | None = None,
) -> SemanticCommit:
    """Validate, commit and publish one explicit semantic write decision.

    Args:
        config: The configuration declaring the journal and graph paths. Read
            for those two targets, the knowledge directories and the data
            directory; never written.
        request: The caller's already-decided
            :class:`~layered_memory_mcp.graph_write.SemanticWriteRequest`.
        binding: Which readable L1 section this decision publishes, and the
            exact body being published. ``None`` for a node kind that has no
            readable L1 surface — today a ``TASK``, whose readable form is the
            ``todo`` projection — in which case the surface binding, ownership
            and body-agreement checks are inapplicable rather than skipped.
            Every entrance that writes knowledge passes one.
        request_id: The stable retry identity. Defaults to
            ``<event namespace>:<request key>``, which is stable for one
            decision and different for a different one.
        store: An optional caller-owned graph store. When given it is used and
            left open; otherwise the configured database is opened for this call
            and closed again.

    Returns:
        A :class:`SemanticCommit`. Every refusal is a status with bounded
        ``errors``; nothing raises for a refusable condition.
    """
    identity = request_id.strip() or f"{request.event_namespace}:{request.request_key}"

    # 1. Is this framework configured to own a write at all? Asked first, and
    #    answered from the declared paths alone — no file is opened, created or
    #    read — because "the authority does not exist here" is a more
    #    fundamental answer than anything about this particular decision, and an
    #    operator debugging a write mode with no journal must not be handed a
    #    refusal about their payload instead.
    missing = sorted(
        name
        for name, declared in (
            ("graph_journal_not_configured", resolve_journal_database_path(config)),
            ("graph_store_not_configured", resolve_graph_database_path(config)),
        )
        if declared is None
    )
    if missing and store is None:
        return _rejected(
            GRAPH_COMMIT_NOT_CONFIGURED, request_id=identity, node_id=request.node.id,
            errors=missing,
        )
    if "graph_journal_not_configured" in missing:
        # An injected store can stand in for the graph database; nothing can
        # stand in for the authority.
        return _rejected(
            GRAPH_COMMIT_NOT_CONFIGURED, request_id=identity, node_id=request.node.id,
            errors=["graph_journal_not_configured"],
        )

    # 2. Purely local validation, before anything is opened or touched. All of
    #    it is about the readable surface, so a decision that publishes to none
    #    has nothing to validate here.
    if binding is not None:
        try:
            binding.entry_id
        except ProjectionIdentityError as exc:
            # A blank, multi-line or non-string domain/section cannot name a
            # surface. Refused by name rather than trimmed into an identity the
            # source never had.
            return _rejected(
                GRAPH_COMMIT_REJECTED_BINDING,
                request_id=identity,
                node_id=request.node.id,
                errors=[f"binding_identity_invalid:{exc}"],
            )

        node_binding = binding.model_copy(update={"body": request.node.content})
        if node_binding.normalized_body != binding.normalized_body:
            return _rejected(
                GRAPH_COMMIT_REJECTED_BINDING,
                request_id=identity,
                node_id=request.node.id,
                entry_id=binding.entry_id,
                errors=[f"binding_body_mismatch:{binding.entry_id}"],
            )

        path = graph_projection.domain_path(config, binding.domain)
        if path.exists() and graph_projection.has_frontmatter_entry(
            path.read_text(encoding="utf-8")
        ):
            return _rejected(
                GRAPH_COMMIT_REJECTED_UNSUPPORTED_SURFACE,
                request_id=identity,
                node_id=request.node.id,
                entry_id=binding.entry_id,
                errors=[f"{graph_projection.SURFACE_UNSUPPORTED}:{binding.entry_id}"],
            )

    with _handles(config, store) as (journal, graph_store):
        if journal is None or graph_store is None:
            return _rejected(
                GRAPH_COMMIT_NOT_CONFIGURED,
                request_id=identity,
                node_id=request.node.id,
                entry_id="" if binding is None else binding.entry_id,
                errors=[
                    "graph_journal_not_configured"
                    if journal is None
                    else "graph_store_not_configured"
                ],
            )
        return _commit(config, journal, graph_store, request, binding, identity)


def _commit(
    config: Any,
    journal: MemoryEventJournal,
    store: Any,
    request: SemanticWriteRequest,
    binding: ProjectionBinding | None,
    identity: str,
) -> SemanticCommit:
    """Validate, append and publish one node-carrying decision.

    ``binding`` of ``None`` means this decision publishes to **no** readable L1
    surface, which is true of exactly one node kind today: a ``TASK``, whose
    readable projection is the TODO view rather than a Markdown section. The
    surface checks are then not relaxed, they are *inapplicable* — there is no
    section to own, drift from or conflict over — and every other check in this
    function still runs unchanged.
    """
    entry_id = "" if binding is None else binding.entry_id

    # 3. The store must actually implement the contract. An injected handle is
    #    the caller's, and a handle that cannot answer ``health`` or ``rebuild``
    #    is a configuration fault reported by name — never an AttributeError
    #    escaping a write path.
    incomplete = missing_graph_store_operations(store)
    if incomplete:
        return _rejected(
            GRAPH_COMMIT_NOT_CONFIGURED,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=[f"graph_store_incomplete:{','.join(incomplete)}"],
        )

    records = journal.read_records()
    events = [event for _position, event in records]

    # 4. Adoption guard. A graph nobody in this journal produced is evidence of
    #    a store this process does not own; projecting over it would erase it.
    health = store.health()
    if not events and health.node_count:
        return _rejected(
            GRAPH_COMMIT_REJECTED_ADOPTION,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=[f"unknown_graph_nodes:{health.node_count}"],
        )

    existing = InMemoryGraphStore()
    existing_report = existing.rebuild(events)
    existing_nodes, existing_edges = _materialize(existing)

    # 5. Plan. Pure, and refuses a decision no projection could carry safely.
    try:
        plan = plan_semantic_write(
            request, existing_nodes=existing_nodes, existing_edges=existing_edges
        )
    except ValueError as exc:
        return _rejected(
            GRAPH_COMMIT_REJECTED_INVALID,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=[f"invalid_decision:{type(exc).__name__}"],
        )

    bound_events = (
        plan.events
        if binding is None
        else _bind_events(plan.events, request.node.id, binding)
    )
    request_hash = canonical_request_hash(bound_events)

    # 6. Retry identity, before the semantic checks: an identical retry must
    #    converge on its pending projections rather than be re-judged.
    stored = journal.request_hash(identity)
    if stored is not None and stored != request_hash:
        return _rejected(
            GRAPH_COMMIT_REJECTED_REQUEST,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=[f"request_payload_changed:{identity}"],
        )
    if stored is not None:
        return _finish(
            config,
            journal,
            store,
            identity=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            event_ids=[event.id for event in bound_events],
            superseded_ids=list(plan.superseded_ids),
            duplicate=True,
            warnings=plan.warnings,
        )

    # 7. Surface ownership. One current owner per readable surface, and taking
    #    one over is a supersession the caller declared, never an overwrite.
    surface_error = (
        None
        if binding is None
        else _surface_conflict(
            events, existing_nodes, binding, request.node.id, plan.superseded_ids
        )
    )
    if surface_error is not None:
        return _rejected(
            GRAPH_COMMIT_REJECTED_SURFACE,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=[surface_error],
        )

    # 8. Lifecycle and scope, fail-closed (the planner only warns).
    lifecycle_errors = _lifecycle_errors(request, existing_nodes)
    if lifecycle_errors:
        return _rejected(
            GRAPH_COMMIT_REJECTED_INVALID,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=lifecycle_errors,
        )

    # 9. The complete proposed replay must project cleanly.
    candidate = InMemoryGraphStore()
    candidate_report = candidate.rebuild([*events, *bound_events])
    if not candidate_report.ok:
        new_errors = sorted(set(candidate_report.errors) - set(existing_report.errors))
        return _rejected(
            GRAPH_COMMIT_REJECTED_INVALID,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=new_errors or sorted(candidate_report.errors),
        )

    # 10. Drift: a surface somebody else wrote is never overwritten.
    drift = _drift_errors(config, journal, candidate, [*events, *bound_events])
    if drift:
        return _rejected(
            GRAPH_COMMIT_REJECTED_DRIFT,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=drift,
        )

    # 11. Commit. The journal append is the authority.
    try:
        append = journal.append(
            identity, bound_events, kind=REQUEST_KIND_SEMANTIC_WRITE
        )
    except JournalConflictError as exc:
        return _rejected(
            GRAPH_COMMIT_REJECTED_REQUEST,
            request_id=identity,
            node_id=request.node.id,
            entry_id=entry_id,
            errors=[f"journal_conflict:{type(exc).__name__}"],
        )

    return _finish(
        config,
        journal,
        store,
        identity=identity,
        node_id=request.node.id,
        entry_id=entry_id,
        event_ids=list(append.event_ids),
        superseded_ids=list(plan.superseded_ids),
        duplicate=append.duplicate,
        warnings=plan.warnings,
    )


def _commit_lifecycle(
    config: Any,
    journal: MemoryEventJournal,
    store: Any,
    request: SemanticLifecycleRequest,
    identity: str,
) -> SemanticCommit:
    """Validate, append and publish one target-only lifecycle decision.

    The same sequence :func:`_commit` runs, minus the parts that only mean
    something for a *new* node: there is no binding to validate, no body to
    compare and no surface to take ownership of. What a retraction does to a
    readable surface is decided by the replay — the target stops being current,
    so its surface is retired by the L1 projection — which is why the drift
    check still runs here: retiring a section somebody else has edited would
    destroy their edit just as surely as republishing over it would.
    """
    incomplete = missing_graph_store_operations(store)
    if incomplete:
        return _rejected(
            GRAPH_COMMIT_NOT_CONFIGURED,
            request_id=identity,
            node_id=request.target_id,
            errors=[f"graph_store_incomplete:{','.join(incomplete)}"],
        )

    records = journal.read_records()
    events = [event for _position, event in records]

    health = store.health()
    if not events and health.node_count:
        return _rejected(
            GRAPH_COMMIT_REJECTED_ADOPTION,
            request_id=identity,
            node_id=request.target_id,
            errors=[f"unknown_graph_nodes:{health.node_count}"],
        )

    existing = InMemoryGraphStore()
    existing_report = existing.rebuild(events)
    existing_nodes, _existing_edges = _materialize(existing)

    stored = journal.request_hash(identity)
    if stored is not None:
        prior_events = journal.read_request_events(identity)
        if len(prior_events) != 1:
            return _rejected(
                GRAPH_COMMIT_REJECTED_REQUEST,
                request_id=identity,
                node_id=request.target_id,
                errors=[f"request_event_count:{identity}"],
            )
        prior = prior_events[0]
        try:
            retry_event = plan_semantic_lifecycle(request, existing_nodes=None).events[0]
        except ValueError:
            return _rejected(
                GRAPH_COMMIT_REJECTED_REQUEST,
                request_id=identity,
                node_id=request.target_id,
                errors=[f"request_payload_changed:{identity}"],
            )
        if (
            retry_event.occurred_at != prior.occurred_at
            and request.event_namespace != REVIEW_EVENT_NAMESPACE
        ):
            return _rejected(
                GRAPH_COMMIT_REJECTED_REQUEST,
                request_id=identity,
                node_id=request.target_id,
                errors=[f"request_payload_changed:{identity}"],
            )
        prior_scope = None if prior.scope is None else prior.scope.to_json()
        if request.scope is not None and request.scope.to_json() != prior_scope:
            return _rejected(
                GRAPH_COMMIT_REJECTED_REQUEST,
                request_id=identity,
                node_id=request.target_id,
                errors=[f"request_payload_changed:{identity}"],
            )
        retry_event = retry_event.model_copy(
            update={"occurred_at": prior.occurred_at, "scope": prior.scope}
        )
        if canonical_request_hash([retry_event]) != stored:
            return _rejected(
                GRAPH_COMMIT_REJECTED_REQUEST,
                request_id=identity,
                node_id=request.target_id,
                errors=[f"request_payload_changed:{identity}"],
            )
        return _finish(
            config,
            journal,
            store,
            identity=identity,
            node_id=request.target_id,
            entry_id="",
            event_ids=[event.id for event in prior_events],
            superseded_ids=[],
            duplicate=True,
            warnings=(),
        )

    try:
        plan = plan_semantic_lifecycle(request, existing_nodes=existing_nodes)
    except ValueError as exc:
        return _rejected(
            GRAPH_COMMIT_REJECTED_INVALID,
            request_id=identity,
            node_id=request.target_id,
            errors=[f"invalid_lifecycle_decision:{exc}"],
        )

    candidate = InMemoryGraphStore()
    candidate_report = candidate.rebuild([*events, *plan.events])
    if not candidate_report.ok:
        new_errors = sorted(set(candidate_report.errors) - set(existing_report.errors))
        return _rejected(
            GRAPH_COMMIT_REJECTED_INVALID,
            request_id=identity,
            node_id=request.target_id,
            errors=new_errors or sorted(candidate_report.errors),
        )

    # A restore may not hand one readable surface two current owners. Refused
    # with the owner named, rather than resolved by guesswork: which of the two
    # should keep the section is a decision, and this request does not carry it.
    conflict = _restored_surface_conflict(events, candidate, request)
    if conflict is not None:
        return _rejected(
            GRAPH_COMMIT_REJECTED_SURFACE,
            request_id=identity,
            node_id=request.target_id,
            errors=[conflict],
        )

    drift = _drift_errors(config, journal, candidate, [*events, *plan.events])
    if drift:
        return _rejected(
            GRAPH_COMMIT_REJECTED_DRIFT,
            request_id=identity,
            node_id=request.target_id,
            errors=drift,
        )

    try:
        append = journal.append(identity, plan.events, kind=REQUEST_KIND_SEMANTIC_WRITE)
    except JournalConflictError as exc:
        return _rejected(
            GRAPH_COMMIT_REJECTED_REQUEST,
            request_id=identity,
            node_id=request.target_id,
            errors=[f"journal_conflict:{type(exc).__name__}"],
        )

    return _finish(
        config,
        journal,
        store,
        identity=identity,
        node_id=request.target_id,
        entry_id="",
        event_ids=list(append.event_ids),
        superseded_ids=[],
        duplicate=append.duplicate,
        warnings=plan.warnings,
    )


def _restored_surface_conflict(
    events: Sequence[MemoryEvent],
    candidate: InMemoryGraphStore,
    request: SemanticLifecycleRequest,
) -> str | None:
    """Whether this decision leaves one readable surface with two owners.

    Only a ``restore`` can: a retraction removes a current owner and an approval
    promotes a node that had none. The check is run over the *proposed* replay,
    so it answers the question that actually matters — after this decision, is
    any surface owned by two current nodes — rather than guessing from the
    request alone.
    """
    if request.action != "restore":
        return None
    nodes, _edges = _materialize(candidate)
    bindings = graph_projection.bindings_from_events(events)
    target_binding = bindings.get(request.target_id)
    if target_binding is None:
        return None
    for owner_id, owned in bindings.items():
        if owner_id == request.target_id:
            continue
        if owned.entry_id != target_binding.entry_id:
            continue
        owner = nodes.get(owner_id)
        if owner is not None and owner.is_current():
            return f"surface_owned_by:{owner_id}"
    return None


# ---------------------------------------------------------------------------
# The target-only lifecycle entrance
# ---------------------------------------------------------------------------

def commit_semantic_lifecycle(
    config: Any,
    request: SemanticLifecycleRequest,
    *,
    request_id: str = "",
    store: Any | None = None,
) -> SemanticCommit:
    """Validate, commit and publish one target-only lifecycle decision.

    The first-class way to retract, restore or approve an existing memory. It
    carries no node, so nothing is invented: the readable consequences — a
    retired L1 section, a vector row that stops being current — are decided by
    the replay of the event this commits, not by a replacement payload the
    caller had to fabricate.

    Args:
        config: The configuration declaring the journal and graph paths.
        request: The caller's already-decided
            :class:`~layered_memory_mcp.graph_write.SemanticLifecycleRequest`.
        request_id: The stable retry identity. Defaults to
            ``<event namespace>:<request key>``.
        store: An optional caller-owned graph store, used and left open.

    Returns:
        A :class:`SemanticCommit`. Every refusal is a status with bounded
        ``errors``; nothing raises for a refusable condition.
    """
    identity = request_id.strip() or f"{request.event_namespace}:{request.request_key}"

    missing = sorted(
        name
        for name, declared in (
            ("graph_journal_not_configured", resolve_journal_database_path(config)),
            ("graph_store_not_configured", resolve_graph_database_path(config)),
        )
        if declared is None
    )
    if missing and store is None:
        return _rejected(
            GRAPH_COMMIT_NOT_CONFIGURED,
            request_id=identity,
            node_id=request.target_id,
            errors=missing,
        )
    if "graph_journal_not_configured" in missing:
        return _rejected(
            GRAPH_COMMIT_NOT_CONFIGURED,
            request_id=identity,
            node_id=request.target_id,
            errors=["graph_journal_not_configured"],
        )

    with _handles(config, store) as (journal, graph_store):
        if journal is None or graph_store is None:
            return _rejected(
                GRAPH_COMMIT_NOT_CONFIGURED,
                request_id=identity,
                node_id=request.target_id,
                errors=[
                    "graph_journal_not_configured"
                    if journal is None
                    else "graph_store_not_configured"
                ],
            )
        return _commit_lifecycle(config, journal, graph_store, request, identity)


# ---------------------------------------------------------------------------
# Tasks: one typed decision, one projection
# ---------------------------------------------------------------------------

class TaskAuthority(GraphModel):
    """What the journal says about tasks right now, read without writing.

    ``unseeded`` is the honest answer to "is the readable store mine": task ids
    present in the TODO database that this journal has no authority for. They
    are somebody's real tasks — the pre-journal ones — and until
    :func:`seed_task_authority` has brought them in, a mutation is refused
    rather than committed into a projection that would then refuse to publish
    over them.
    """

    available: bool = False
    records: dict[str, TaskRecord] = Field(default_factory=dict)
    known_ids: list[str] = Field(default_factory=list)
    unseeded: list[str] = Field(default_factory=list)


class TaskSeedReport(GraphModel):
    """What the explicit task migration found, and what it did about it."""

    status: str = TASK_SEED_NOTHING_TO_DO
    stored_tasks: int = Field(default=0, ge=0)
    already_known: int = Field(default=0, ge=0)
    seeded: int = Field(default=0, ge=0)
    head_position: int = Field(default=0, ge=0)
    checkpoints: dict[str, int] = Field(default_factory=dict)
    projections_pending: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (TASK_SEED_APPLIED, TASK_SEED_NOTHING_TO_DO)


def task_authority(config: Any) -> TaskAuthority:
    """The current task state, read through the inspection entrances only.

    Opens the journal read-only and the TODO store read-only, so asking "what
    tasks are there, and is this store the journal's?" creates nothing — not the
    journal, not the store, not a schema and not a lock. A reader must not be
    the thing that establishes the authority it is asking about.
    """
    from . import graph_task

    records: dict[str, TaskRecord] = {}
    known: set[str] = set()
    available = False
    try:
        with journal_readonly_session(config, require=True) as assembly:
            if assembly.journal is not None:
                available = True
                events = assembly.journal.read_events()
                projection = InMemoryGraphStore()
                projection.rebuild(events)
                nodes, _edges = _materialize(projection)
                records = graph_task.task_records(nodes.values())
                known = graph_task.task_authority_ids(nodes.values())
    except Exception:  # noqa: BLE001 — an unreadable journal holds no tasks
        return TaskAuthority()
    stored = graph_task.stored_task_ids(config)
    return TaskAuthority(
        available=available,
        records=records,
        known_ids=sorted(known),
        unseeded=sorted(stored - known),
    )


def commit_task_mutation(
    config: Any,
    mutation: TaskMutation,
    *,
    store: Any | None = None,
) -> tuple[SemanticCommit, TaskRecord | None]:
    """Commit one task decision through the authority, and say what it became.

    Three refusals before the journal, each leaving it byte-for-byte untouched:
    a task the authority does not hold, an ``add`` for a task id it already
    holds, and a readable store carrying tasks that were never seeded. The
    returned record is the state the decision produced — for a delete, the state
    it removed — so the caller can report what changed without re-reading.
    """
    from . import graph_task

    authority = task_authority(config)
    if authority.unseeded:
        return (
            _rejected(
                GRAPH_TASK_AUTHORITY_UNSEEDED,
                node_id="",
                errors=[f"todo_store_not_adopted:{len(authority.unseeded)}"],
            ),
            None,
        )

    task_id = mutation.task_id.strip()
    current = authority.records.get(task_id) if task_id else None
    if mutation.action == graph_task.TASK_ACTION_ADD:
        if current is not None:
            return (
                _rejected(
                    GRAPH_COMMIT_REJECTED_REQUEST,
                    node_id=current.node_id,
                    errors=[f"task_already_exists:{task_id}"],
                ),
                None,
            )
    elif current is None:
        return (
            _rejected(GRAPH_TASK_UNKNOWN, errors=[f"task_unknown:{task_id}"]),
            None,
        )

    try:
        request, record = graph_task.mutation_request(mutation, current)
    except ValueError as exc:
        return (
            _rejected(
                GRAPH_COMMIT_REJECTED_INVALID,
                errors=[f"invalid_task_decision:{exc}"],
            ),
            None,
        )

    if isinstance(request, SemanticLifecycleRequest):
        return commit_semantic_lifecycle(config, request, store=store), record
    return commit_semantic_write(config, request, None, store=store), record


def seed_task_authority(
    config: Any,
    *,
    rows: Sequence[Mapping[str, Any]] | None = None,
    occurred_at: Any = None,
    apply: bool = False,
    store: Any | None = None,
) -> TaskSeedReport:
    """Make the pre-existing tasks the journal's, explicitly. Preview by default.

    The migration the adoption guard exists to require. Every row in the
    readable TODO store that the journal has no authority for is appended as one
    bootstrap request carrying it **verbatim** — same id, same timestamps, same
    priority, same status, same ``blocked_by`` — with
    ``derivation.method = migrated``, so nothing the original row did not say is
    invented and nothing is re-dated.

    ``apply=False`` (the default) inspects and writes nothing at all: no
    journal, no lock file, no store. ``apply=True`` appends once and then
    publishes, so the readable view is the journal's projection from that point
    on. Running it twice is a no-op for tasks already seeded: a task's seed
    event identity is derived from its own id, so re-appending it is a replay.

    Args:
        config: The configuration declaring the journal, graph and home.
        rows: The task rows to seed. Defaults to reading them from the
            configured TODO store, read-only.
        occurred_at: The migration's own instant, used only as the events'
            ledger position. Defaults to now.
        apply: Whether to commit. ``False`` previews.
        store: An optional caller-owned graph store.
    """
    from datetime import datetime, timezone

    from . import graph_task

    authority = task_authority(config)
    stored = (
        list(rows) if rows is not None else graph_task.stored_task_rows(config)
    )
    known = set(authority.known_ids)
    pending = [row for row in stored if str(row.get("id") or "") not in known]
    report = TaskSeedReport(
        stored_tasks=len(stored),
        already_known=len(stored) - len(pending),
    )
    if not pending:
        return report.model_copy(update={"status": TASK_SEED_NOTHING_TO_DO})
    if not apply:
        return report.model_copy(
            update={"status": TASK_SEED_PREVIEW, "seeded": len(pending)}
        )

    moment = occurred_at or datetime.now(timezone.utc)
    try:
        events: list[MemoryEvent] = []
        for request in graph_task.seed_requests(pending, occurred_at=moment):
            events.extend(plan_semantic_write(request).events)
    except ValueError as exc:
        return report.model_copy(
            update={
                "status": GRAPH_COMMIT_REJECTED_INVALID,
                "errors": [f"invalid_task_row:{exc}"],
            }
        )

    identity = (
        f"{graph_task.TASK_SEED_NAMESPACE}:"
        f"{_seed_identity(sorted(str(row.get('id')) for row in pending))}"
    )
    with _handles(config, store) as (journal, graph_store):
        if journal is None or graph_store is None:
            return report.model_copy(
                update={
                    "status": GRAPH_COMMIT_NOT_CONFIGURED,
                    "errors": [
                        "graph_journal_not_configured"
                        if journal is None
                        else "graph_store_not_configured"
                    ],
                }
            )
        candidate = InMemoryGraphStore()
        existing = journal.read_events()
        candidate_report = candidate.rebuild([*existing, *events])
        if not candidate_report.ok:
            return report.model_copy(
                update={
                    "status": GRAPH_COMMIT_REJECTED_INVALID,
                    "errors": sorted(candidate_report.errors)[:10],
                }
            )
        try:
            append = journal.append(identity, events, kind=REQUEST_KIND_BOOTSTRAP)
        except JournalConflictError as exc:
            return report.model_copy(
                update={
                    "status": GRAPH_COMMIT_REJECTED_REQUEST,
                    "errors": [f"journal_conflict:{type(exc).__name__}"],
                }
            )
        commit = _finish(
            config,
            journal,
            graph_store,
            identity=identity,
            node_id="",
            entry_id="",
            event_ids=list(append.event_ids),
            superseded_ids=[],
            duplicate=append.duplicate,
            warnings=[],
        )
    return report.model_copy(
        update={
            "status": TASK_SEED_APPLIED,
            "seeded": len(pending),
            "head_position": commit.head_position,
            "checkpoints": dict(commit.checkpoints),
            "projections_pending": list(commit.projections_pending),
            "errors": list(commit.errors),
        }
    )


def _seed_identity(task_ids: Sequence[str]) -> str:
    """A stable identity for one seeding run, from the ids it carries.

    Hashed rather than listed: a request identity is a key, not a payload, and
    106 task ids in one is neither readable nor bounded. The same set of rows
    therefore seeds under the same identity — so a repeated run is a journal
    duplicate rather than a second authority — and a *larger* set is a new
    identity whose already-recorded events are skipped as the replay they are.
    """
    import hashlib

    digest = hashlib.sha256("\x1f".join(task_ids).encode("utf-8")).hexdigest()
    return digest[:16]


def _materialize(
    projection: InMemoryGraphStore,
) -> tuple[dict[str, GraphNode], dict[str, GraphEdge]]:
    """The projection's nodes and edges as validated domain objects.

    Read through ``snapshot`` — the store's own public, deterministic export —
    so this layer depends on the contract rather than on an implementation's
    internals.
    """
    snapshot = projection.snapshot()
    nodes = {
        payload["id"]: GraphNode.model_validate(payload)
        for payload in snapshot["nodes"]
    }
    edges = {
        payload["id"]: GraphEdge.model_validate(payload)
        for payload in snapshot["edges"]
    }
    return nodes, edges


def _bind_events(
    events: Sequence[MemoryEvent], node_id: str, binding: ProjectionBinding
) -> list[MemoryEvent]:
    """Stamp the readable-surface binding onto the node event.

    The binding travels *in the journal*, not in a side table, so a full replay
    after a restart can rebuild the readable surface without consulting any
    other store. Event IDs are hashed from the decision, never from metadata,
    so stamping cannot change an identity.
    """
    metadata = graph_projection.binding_metadata(binding)
    bound: list[MemoryEvent] = []
    for event in events:
        if event.node is not None and event.node.id == node_id:
            bound.append(
                MemoryEvent(
                    **{
                        **dict(event),
                        "metadata": {
                            **dict(event.metadata),
                            graph_projection.PROJECTION_METADATA_KEY: metadata,
                        },
                    }
                )
            )
        else:
            bound.append(event)
    return bound


def _surface_conflict(
    events: Sequence[MemoryEvent],
    nodes: Mapping[str, GraphNode],
    binding: ProjectionBinding,
    node_id: str,
    superseded_ids: Sequence[str],
) -> str | None:
    """Whether some other current node already owns this readable surface."""
    bindings = graph_projection.bindings_from_events(events)
    retired = set(superseded_ids)
    for owner_id, owned in bindings.items():
        if owned.entry_id != binding.entry_id:
            continue
        if owner_id == node_id or owner_id in retired:
            continue
        owner = nodes.get(owner_id)
        if owner is not None and owner.is_current():
            return f"surface_owned_by:{owner_id}"
    return None


def _lifecycle_errors(
    request: SemanticWriteRequest, nodes: Mapping[str, GraphNode]
) -> list[str]:
    """Lifecycle and scope transitions that must fail closed, not merely warn."""
    errors: list[str] = []
    for assertion in request.supersedes:
        target = nodes.get(assertion.target_id)
        if target is None:
            errors.append(f"supersede_target_unknown:{assertion.target_id}")
            continue
        if target.status not in SUPERSEDABLE_STATUSES:
            errors.append(
                f"supersede_target_not_current:{assertion.target_id}:"
                f"{target.status.value}"
            )
        scope = request.scope if request.scope is not None else request.node.scope
        if target.scope.to_json() != scope.to_json():
            errors.append(f"supersede_target_scope_mismatch:{assertion.target_id}")
    return sorted(set(errors))


def _drift_errors(
    config: Any,
    journal: MemoryEventJournal,
    projection: InMemoryGraphStore,
    events: Sequence[MemoryEvent],
) -> list[str]:
    """Surfaces the proposed publication would touch that somebody else owns."""
    nodes, _edges = _materialize(projection)
    errors: list[str] = []
    for plan in graph_projection.surface_plans(events, nodes):
        refusal = graph_projection.surface_refusal(
            config,
            journal,
            ProjectionBinding(
                domain=plan.domain,
                section=plan.section,
                occurrence=plan.occurrence,
                kind=plan.kind,
                body=plan.body,
            ),
        )
        if refusal is not None:
            errors.append(refusal)
    return sorted(set(errors))


# ---------------------------------------------------------------------------
# Publication and recovery — one implementation, two entry points
# ---------------------------------------------------------------------------

#: What each managed projection is derived *from*. A projection may not publish
#: over an unpublished dependency: the vector rows come from the L1 files and L0
#: from the files as a whole, so publishing either over an unpublished L1 would
#: record a reader-visible state the authority never reached.
#:
#: The readable TODO view depends on the **graph alone** — a task is not a
#: Markdown section — which is the whole reason this is a table rather than a
#: single "stop at the first failure" flag. A refused readable surface used to
#: hold every later projection hostage, including ones that had nothing to do
#: with it.
PROJECTION_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    PROJECTION_GRAPH: (),
    PROJECTION_L1: (PROJECTION_GRAPH,),
    PROJECTION_VECTOR: (PROJECTION_L1,),
    PROJECTION_L0: (PROJECTION_L1,),
    PROJECTION_TODO: (PROJECTION_GRAPH,),
}


def _publish(
    config: Any,
    journal: MemoryEventJournal,
    store: Any,
    *,
    only: Sequence[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Publish the managed projections. Returns ``(published, pending, errors)``.

    Published in dependency order (:data:`PROJECTION_DEPENDENCIES`), and a
    failure blocks exactly what is *derived from* the thing that failed —
    transitively, because the steps run in topological order and a blocked step
    blocks its own dependents in turn. Everything independent of the failure
    still publishes, so one refused section no longer leaves a committed task
    decision unreadable.
    """
    wanted = list(PROJECTION_NAMES if only is None else only)
    records = journal.read_records()
    events = [event for _position, event in records]
    head = records[-1][0] if records else 0

    published: list[str] = []
    errors: list[str] = []
    pending: list[str] = []

    projection = InMemoryGraphStore()
    projection.rebuild(events)
    nodes, _edges = _materialize(projection)
    domains = sorted(
        {
            binding.domain
            for binding in graph_projection.bindings_from_events(events).values()
        }
    )

    steps = (
        (PROJECTION_GRAPH, lambda: graph_projection.publish_graph(store, events)),
        (
            PROJECTION_L1,
            lambda: graph_projection.publish_l1(
                config, journal, events, nodes, position=head
            ),
        ),
        (PROJECTION_VECTOR, lambda: graph_projection.publish_vector(config, domains)),
        (PROJECTION_L0, lambda: graph_projection.publish_l0(config)),
        (
            PROJECTION_TODO,
            lambda: graph_projection.publish_todo(config, nodes.values()),
        ),
    )

    blocked: set[str] = set()
    for name, run in steps:
        if name not in wanted:
            continue
        if blocked.intersection(PROJECTION_DEPENDENCIES[name]):
            pending.append(name)
            blocked.add(name)
            continue
        try:
            run()
        except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
            label = (
                str(exc)
                if isinstance(exc, ProjectionError)
                else f"{name}_projection_failed:{type(exc).__name__}"
            )
            journal.record_projection_failure(name, label[:200])
            errors.append(f"{name}:{label[:200]}")
            pending.append(name)
            blocked.add(name)
            continue
        journal.set_checkpoint(name, head)
        published.append(name)

    return published, sorted(set(pending + journal.pending_projections())), errors


def _finish(
    config: Any,
    journal: MemoryEventJournal,
    store: Any,
    *,
    identity: str,
    node_id: str,
    entry_id: str,
    event_ids: Sequence[str],
    superseded_ids: Sequence[str],
    duplicate: bool,
    warnings: Sequence[str],
) -> SemanticCommit:
    """Publish what this commit (or this retry) still owes, and report honestly."""
    outstanding = journal.pending_projections()
    errors: list[str] = []
    if outstanding:
        _published, outstanding, errors = _publish(
            config, journal, store, only=outstanding
        )

    head = journal.head_position()
    checkpoints = {name: journal.checkpoint(name).position for name in PROJECTION_NAMES}
    # Whether a reader can see this decision's readable surface, as its own
    # fact. A commit whose L1 write failed is durable and unreadable, and the
    # entry ID is named in the errors so "which surface did not land" does not
    # have to be inferred from a projection name.
    surface_pending = PROJECTION_L1 in outstanding
    reported = list(errors)
    if surface_pending and entry_id:
        reported.append(f"l1_projection_pending:{entry_id}")
    return SemanticCommit(
        status=(
            GRAPH_COMMIT_PENDING_PROJECTIONS
            if outstanding
            else GRAPH_COMMIT_COMMITTED
        ),
        committed=True,
        duplicate=duplicate,
        request_id=identity,
        node_id=node_id,
        entry_id=entry_id,
        surface_published=bool(entry_id) and not surface_pending,
        event_ids=list(event_ids),
        head_position=head,
        checkpoints=checkpoints,
        projections_pending=sorted(outstanding),
        superseded_ids=list(superseded_ids),
        warnings=sorted(set(warnings)),
        errors=sorted(set(reported)),
    )


def recover_projections(
    config: Any, *, store: Any | None = None, force: bool = False
) -> RecoveryReport:
    """Finish every projection the journal says is behind. Idempotent.

    The explicit recovery entry point: it reads the complete journal, republishes
    only what is pending, and advances a checkpoint only where the projection
    actually succeeded. Running it with nothing pending changes nothing, and it
    says so — a recovery that reported success for a projection it never ran
    would be exactly the dishonesty the checkpoints exist to prevent.

    ``force=True`` republishes **every** managed projection from the journal
    regardless of its checkpoint. That is the answer to a projection that was
    lost rather than left behind — a deleted graph database, a restored backup,
    a wiped vector store — which a checkpoint cannot detect by itself (see
    ``projection_drift`` in :func:`runtime_status`). It is still a
    republication, not an adoption: the readable surfaces it would overwrite are
    checked for external edits first and a drifted one is refused and reported.
    """
    with _handles(config, store) as (journal, graph_store):
        if journal is None or graph_store is None:
            return RecoveryReport(
                status=RECOVERY_NOT_CONFIGURED,
                errors=[
                    "graph_journal_not_configured"
                    if journal is None
                    else "graph_store_not_configured"
                ],
            )
        pending = list(PROJECTION_NAMES) if force else journal.pending_projections()
        head = journal.head_position()
        if not pending or not head:
            return RecoveryReport(
                status=RECOVERY_NOTHING_PENDING,
                head_position=head,
                checkpoints={
                    name: journal.checkpoint(name).position for name in PROJECTION_NAMES
                },
            )
        published, outstanding, errors = _publish(
            config, journal, graph_store, only=pending
        )
        return RecoveryReport(
            status=RECOVERY_OK if not outstanding else RECOVERY_INCOMPLETE,
            head_position=head,
            recovered=sorted(published),
            checkpoints={
                name: journal.checkpoint(name).position for name in PROJECTION_NAMES
            },
            projections_pending=sorted(outstanding),
            errors=sorted(set(errors)),
        )


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

def runtime_status(config: Any, *, store: Any | None = None) -> dict[str, Any]:
    """The bounded operator view of the authority and its projections.

    Read-only in the literal sense, not merely in intent: **both** persistent
    stores are opened through their inspection entrances —
    :func:`~layered_memory_mcp.graph_runtime_assembly.journal_readonly_session`
    and
    :func:`~layered_memory_mcp.graph_runtime_assembly.graph_store_readonly_session`
    — which create no database, no directory, no schema, no metadata, no
    checkpoint row and no write transaction. Running this against a system that
    has never committed therefore reports an absent journal *and* an absent
    graph and leaves both absent; running it against one that has leaves both
    files byte-for-byte as they were. It publishes nothing, repairs nothing and
    advances no checkpoint — a diagnostic that changed state would be measuring
    itself.

    An absent graph is a *reported* status rather than a silently recreated
    empty one, which is also the more honest answer: a deleted projection with
    a populated journal is drift, and :func:`_projection_drift` says so instead
    of comparing the ledger against a database this call had just created.

    ``store`` is accepted for the signature a caller already uses and is *not*
    consulted: this view reports the configured stores as they are on disk, and
    it has done so since before the inspection entrances existed. An injected
    handle would report a different subject under the same key.
    """
    mode = GraphModeDecision.from_config(config)
    payload: dict[str, Any] = {
        "mode": mode.mode,
        "recall_enabled": mode.recall_enabled,
        "write_enabled": mode.write_enabled,
    }

    with journal_readonly_session(config, require=True) as assembly:
        if assembly.journal is None:
            payload["journal"] = {
                "status": assembly.report.status,
                "path_configured": assembly.report.path_configured,
                "errors": list(assembly.report.errors),
            }
        else:
            health = assembly.journal.health()
            payload["journal"] = {
                "status": assembly.report.status,
                "path_configured": True,
                **health.to_dict(),
            }
            payload["surfaces"] = {
                "published": len(
                    assembly.journal.surfaces(include_retired=False)
                ),
                "total": len(assembly.journal.surfaces()),
            }

    with graph_store_readonly_session(config, require=True) as graph_assembly:
        payload["graph"] = graph_assembly_payload(graph_assembly.report)
        payload["projection_drift"] = _projection_drift(config, graph_assembly.store)
    return payload


#: The fixed key set of the drift view. A projection that was *lost* rather than
#: left behind is invisible to a checkpoint — the checkpoint says "published",
#: and it was, into a database that no longer holds it. This is how an operator
#: sees that, and the reason ``recover_projections(force=True)`` exists.
_EMPTY_DRIFT: dict[str, Any] = {
    "drifted": False,
    "expected_nodes": 0,
    "expected_edges": 0,
    "stored_nodes": 0,
    "stored_edges": 0,
    "missing_nodes": 0,
    "missing_edges": 0,
    "unexpected_nodes": 0,
    "unexpected_edges": 0,
    "changed_nodes": 0,
    "changed_edges": 0,
    "errors": [],
}

#: Bounded label for "the journal expects a projection and there is no graph
#: database to hold it". A token, never a path (§15).
PROJECTION_STORE_ABSENT = "projection_store_absent"


def _absent_store_drift(expected_nodes: int, expected_edges: int) -> dict[str, Any]:
    """The drift view when the journal has a projection and the graph has none.

    Everything the ledger expects is missing, because the database that would
    hold it is not there. With nothing expected there is nothing to be missing
    either, and the answer is the empty view — an unpublished system is not a
    drifted one.
    """
    if not expected_nodes and not expected_edges:
        return dict(_EMPTY_DRIFT)
    return {
        **_EMPTY_DRIFT,
        "drifted": True,
        "expected_nodes": expected_nodes,
        "expected_edges": expected_edges,
        "missing_nodes": expected_nodes,
        "missing_edges": expected_edges,
        "errors": [PROJECTION_STORE_ABSENT],
    }


def _projection_drift(config: Any, store: Any) -> dict[str, Any]:
    """Does the stored graph still match the journal it was projected from?

    Counts only, through the backend's own ``audit_drift``: "the database is
    fine" and "the database matches the ledger" are different claims, and this
    is the second one. Never raises and never repairs — a diagnostic that
    changed state would be measuring itself — and it reads *both* sides through
    their read-only entrances, so asking whether the projection drifted cannot
    create the ledger it compares against, nor the projection it compares.

    A ``store`` of ``None`` is the inspection path's answer for a graph that is
    not there (deleted, never published, unreadable). That is reported as the
    drift it is, from the journal alone — every expected node and edge missing —
    rather than as "clean", which is what comparing against a freshly created
    empty database used to come out as once the ledger was empty too. A store
    that is present but cannot audit keeps the previous answer: the capability
    is missing, not the data.
    """
    if store is not None and not callable(getattr(store, "audit_drift", None)):
        return dict(_EMPTY_DRIFT)
    try:
        with journal_readonly_session(config, require=True) as assembly:
            if assembly.journal is None:
                return dict(_EMPTY_DRIFT)
            events = assembly.journal.read_events()
        projection = InMemoryGraphStore()
        projection.rebuild(events)
        nodes, edges = _materialize(projection)
        if store is None:
            return _absent_store_drift(len(nodes), len(edges))
        report = store.audit_drift(nodes.values(), edges.values())
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return {
            **_EMPTY_DRIFT,
            "drifted": True,
            "errors": [f"drift_check_failed:{type(exc).__name__}"],
        }
    return {
        "drifted": report.drifted,
        "expected_nodes": report.expected_nodes,
        "expected_edges": report.expected_edges,
        "stored_nodes": report.stored_nodes,
        "stored_edges": report.stored_edges,
        "missing_nodes": len(report.missing_nodes),
        "missing_edges": len(report.missing_edges),
        "unexpected_nodes": len(report.unexpected_nodes),
        "unexpected_edges": len(report.unexpected_edges),
        "changed_nodes": len(report.changed_nodes),
        "changed_edges": len(report.changed_edges),
        "errors": sorted({*report.schema_mismatches, *report.errors}),
    }


def stale_surface_entry_ids(journal: MemoryEventJournal) -> set[str]:
    """Readable surfaces an accepted-but-unpublished write has already changed.

    The honest half of a fallback answer: when the journal holds committed
    events the L1/vector projections have not published, the rows those events
    replace are *known* to be stale, and answering with them as current facts
    would be exactly the dishonesty §10.3 forbids. Recall excludes them instead.

    Two sources, both exact: the binding an unpublished event carries (the
    surface it will publish to), and the surface currently owned by a node an
    unpublished lifecycle event retires. Nothing is inferred from content.

    Takes an open journal rather than a config so one recall request opens it
    once for this and for the surface-owner map.
    """
    watermark = min(
        journal.checkpoint(PROJECTION_L1).position,
        journal.checkpoint(PROJECTION_VECTOR).position,
    )
    records = journal.read_records()
    if not records or records[-1][0] <= watermark:
        return set()
    surfaces_by_node = journal.surfaces_by_node()
    stale: set[str] = set()
    for position, event in records:
        if position <= watermark:
            continue
        binding = graph_projection.parse_binding(event.metadata)
        if binding is not None:
            stale.add(binding.entry_id)
        if event.target_id and event.target_id in surfaces_by_node:
            stale.add(surfaces_by_node[event.target_id].entry_id)
    return stale


def pending_surface_entry_ids(config: Any) -> set[str]:
    """:func:`stale_surface_entry_ids` for a caller that has only a config.

    Never raises: an unconfigured or unreadable journal means "nothing known to
    be stale", which is the same answer the framework gave before it existed.

    Observation only, so it opens the journal through the read-only entrance: a
    *reader* asking "has anything accepted not been published yet?" must not be
    the thing that creates the authority it is asking about. An absent journal
    reaches the same ``set()`` it always did, one step earlier.
    """
    try:
        with journal_readonly_session(config, require=True) as assembly:
            if assembly.journal is None:
                return set()
            return stale_surface_entry_ids(assembly.journal)
    except Exception:  # noqa: BLE001 — a diagnostic may not break recall
        return set()

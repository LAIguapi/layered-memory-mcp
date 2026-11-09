"""Opt-in group rewrites over the existing journal and projection pipeline.

Preconditions are validated inside the same publication lock as the append.
They compare against the framework's own journal and surface registry. They
are not an OS-level guard against external processes that edit the Markdown
files without taking this lock.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from . import graph_application as application
from . import graph_projection as projection
from .graph_fixture_store import InMemoryGraphStore
from .graph_journal import (
    REQUEST_KIND_SEMANTIC_WRITE,
    JournalConflictError,
    canonical_request_hash,
)
from .graph_models import (
    GraphModel, GraphNode, GraphScope, MemoryEventKind, NodeKind, RelationType, Timestamp,
    clean_identifier,
)
from .graph_runtime_assembly import (
    journal_readonly_session,
    resolve_graph_database_path,
    resolve_journal_database_path,
)
from .graph_store import missing_graph_store_operations
from .graph_write import (
    SEMANTIC_WRITE_EVENT_NAMESPACE,
    RelationAssertion,
    SemanticWriteRequest,
    plan_semantic_write,
)

__all__ = [
    "SectionRef", "PreambleRef", "RewriteTarget", "RewritePreconditions", "SemanticRewriteRequest",
    "canonical_node_sha256", "commit_semantic_rewrite", "inspect_semantic_surfaces",
]


class SectionRef(GraphModel):
    domain: str
    section: str
    occurrence: int = Field(default=0, ge=0, strict=True)


class PreambleRef(GraphModel):
    domain: str


class RewriteTarget(GraphModel):
    old_node_ids: list[str] = Field(min_length=1)
    node: GraphNode
    surface: SectionRef | PreambleRef

    @field_validator("surface", mode="before")
    @classmethod
    def _surface_ref(cls, value: Any) -> SectionRef | PreambleRef:
        if isinstance(value, (SectionRef, PreambleRef)):
            return value
        if isinstance(value, dict) and "section" in value:
            return SectionRef.model_validate(value)
        return PreambleRef.model_validate(value)


class RewritePreconditions(GraphModel):
    expected_head_position: int | None = None
    expected_node_hashes: dict[str, str] = Field(default_factory=dict)
    expected_body_hashes: dict[str, str] = Field(default_factory=dict)
    expected_entry_ids: list[str] | None = None


class SemanticRewriteRequest(GraphModel):
    targets: list[RewriteTarget]
    occurred_at: Timestamp
    request_key: str = ""
    event_namespace: str = SEMANTIC_WRITE_EVENT_NAMESPACE
    scope: GraphScope | None = None
    preconditions: RewritePreconditions | None = None

    @field_validator("event_namespace")
    @classmethod
    def _namespace(cls, value: str) -> str:
        return clean_identifier(value, "event_namespace")

    @field_validator("request_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return clean_identifier(value, "request_key") if value else ""


def canonical_node_sha256(node: GraphNode) -> str:
    """SHA-256 of UTF-8 ``node.to_json()`` for the journal-replayed node.

    This is ``json.dumps(node.model_dump(mode="json"), sort_keys=True,
    ensure_ascii=False)`` with Python's default separators, including every
    field (and null/default values). ``version`` alone is never a CAS token.
    """
    return hashlib.sha256(node.to_json().encode("utf-8")).hexdigest()


def _domain_path(config: Any, domain: str) -> Path:
    if (
        not isinstance(domain, str) or not domain.strip() or domain != domain.strip()
        or len(domain + ".md") > 255 or ".." in domain
        or any(character in domain for character in ("/", "\\", "\x00", "\r", "\n"))
    ):
        raise ValueError("invalid_domain")
    path = projection.domain_path(config, domain)
    if path.is_symlink():
        raise ValueError("domain_alias_not_allowed")
    path.resolve().relative_to(path.parent.resolve())
    return path


def _scope_allowed(config: Any, node: GraphNode) -> bool:
    namespace = getattr(config, "namespace", "shared")
    return namespace == "shared" or node.scope.namespace in (None, "shared", namespace)


def inspect_semantic_surfaces(
    config: Any, domain: str, *, include_body: bool = False, store: Any | None = None,
) -> dict:
    """Inspect only the explicitly named primary domain, without any writes.

    ``store`` is accepted for parity with commits; authority and node hashes
    always come from read-only journal replay, never a possibly stale store.
    No publication lock, telemetry, database/schema creation or repair occurs.
    WAL-format journals are refused before SQLite opens them: even mode=ro
    can create WAL/SHM sidecars. Framework journals use rollback mode.
    """
    try:
        path = _domain_path(config, domain)
        if not isinstance(include_body, bool):
            return {"success": False, "error": "invalid_include_body"}
        journal_path = resolve_journal_database_path(config)
        if journal_path is not None and journal_path.is_file():
            with journal_path.open("rb") as stream:
                header = stream.read(20)
            if header[:16] == b"SQLite format 3\x00" and header[18:20] != b"\x01\x01":
                return {"success": False, "error": "graph_inspection_unsupported_journal_mode"}
        with journal_readonly_session(config, require=True) as assembly:
            journal = assembly.journal
            if journal is None:
                return {"success": False, "error": assembly.report.status}
            records = journal.read_records()
            replay = InMemoryGraphStore()
            report = replay.rebuild([event for _position, event in records])
            if not report.ok:
                return {"success": False, "error": application.GRAPH_COMMIT_REJECTED_INVALID}
            nodes, _edges = application._materialize(replay)
            surfaces = [record for record in journal.surfaces(include_retired=False)
                        if record.domain == domain]
            raw = path.read_text(encoding="utf-8") if path.exists() else ""
            result = []
            for record in surfaces:
                node = nodes.get(record.node_id)
                if node is None or not _scope_allowed(config, node):
                    continue
                body = projection._read_surface_body(raw, record)
                if body is None:
                    return {"success": False, "error": application.GRAPH_COMMIT_REJECTED_DRIFT}
                row = {
                    "entry_id": record.entry_id, "domain": record.domain,
                    "surface_kind": record.kind,
                    "section": record.section, "occurrence": record.occurrence,
                    "node_id": node.id, "status": node.status.value,
                    "scope": node.scope.to_dict(),
                    "body_sha256": projection.body_sha256(body),
                    "binding_body_sha256": record.body_sha256,
                    "node_sha256": canonical_node_sha256(node),
                }
                if include_body:
                    row["body"] = body
                result.append(row)
            return {"success": True, "domain": domain,
                    "head_position": records[-1][0] if records else 0, "surfaces": result}
    except Exception:
        return {"success": False, "error": "graph_inspection_failed"}


def _precondition_status(request, journal, nodes, old_ids, bindings):
    conditions = request.preconditions
    if conditions is None:
        return None
    entries = {bindings[node_id].entry_id for node_id in old_ids if node_id in bindings}
    declared = conditions.expected_entry_ids
    if declared is not None and set(declared) != entries:
        return "graph_rewrite_rejected_entry_set"
    if (conditions.expected_head_position is not None
            and conditions.expected_head_position != journal.head_position()):
        return "graph_rewrite_rejected_preconditions"
    for node_id, expected in conditions.expected_node_hashes.items():
        if node_id not in old_ids or node_id not in nodes:
            return "graph_rewrite_rejected_preconditions"
        if canonical_node_sha256(nodes[node_id]) != expected:
            return "graph_rewrite_rejected_preconditions"
    for entry_id, expected in conditions.expected_body_hashes.items():
        record = journal.surface(entry_id) if entry_id in entries else None
        if record is None or record.status == "retired" or record.body_sha256 != expected:
            return "graph_rewrite_rejected_preconditions"
    return None


def commit_semantic_rewrite(
    config: Any, request: SemanticRewriteRequest, *, request_id: str = "",
    store: Any | None = None,
) -> application.SemanticCommit:
    """Validate the complete group, append once, then use existing recovery.

    Preconditions are validated inside the same publication lock as the append.
    They compare against the framework's own journal and surface registry. They
    are not an OS-level guard against external processes that edit the Markdown
    files without taking this lock.
    """
    fingerprint = hashlib.sha256(request.to_json().encode("utf-8")).hexdigest()
    key = request.request_key.strip() or fingerprint
    identity = request_id.strip() or f"{request.event_namespace}:rewrite:{key}"
    if not request.targets:
        return application._rejected("graph_rewrite_rejected_empty", request_id=identity)
    if not getattr(config, "graph_write_enabled", False):
        return application._rejected(
            application.GRAPH_COMMIT_NOT_CONFIGURED, request_id=identity,
            errors=["graph_rewrite_requires_write_mode"],
        )
    if any(not target.old_node_ids for target in request.targets):
        return application._rejected(
            application.GRAPH_COMMIT_REJECTED_INVALID, request_id=identity,
            errors=["empty_old_node_ids"],
        )
    if any(len(target.old_node_ids) != len(set(target.old_node_ids))
           for target in request.targets):
        return application._rejected(
            application.GRAPH_COMMIT_REJECTED_INVALID, request_id=identity,
            errors=["duplicate_old_node_id_in_target"],
        )
    missing = resolve_journal_database_path(config) is None or (
        store is None and resolve_graph_database_path(config) is None
    )
    if missing:
        return application._rejected(application.GRAPH_COMMIT_NOT_CONFIGURED, request_id=identity)
    try:
        bindings = []
        writes = []
        bound_events = []
        transitioned_ids = set()
        for index, target in enumerate(request.targets):
            _domain_path(config, target.surface.domain)
            if isinstance(target.surface, PreambleRef):
                binding = projection.ProjectionBinding(
                    domain=target.surface.domain, section="", occurrence=0,
                    kind=projection.PROJECTION_PREAMBLE_SURFACE_KIND, body=target.node.content,
                )
            else:
                binding = projection.ProjectionBinding(
                    **target.surface.to_dict(), body=target.node.content,
                )
            binding.entry_id
            write = SemanticWriteRequest(
                node=target.node, occurred_at=request.occurred_at,
                request_key=f"rewrite:{key}:{index}", event_namespace=request.event_namespace,
                scope=request.scope,
                supersedes=[RelationAssertion(
                    relation_type=RelationType.SUPERSEDES, target_id=node_id,
                    derivation=target.node.derivation, evidence_refs=target.node.source_refs,
                ) for node_id in target.old_node_ids],
            )
            plan = plan_semantic_write(write)
            events = application._bind_events(plan.events, target.node.id, binding)
            for event in events:
                if event.kind == MemoryEventKind.SUPERSEDE:
                    if event.target_id in transitioned_ids:
                        continue
                    transitioned_ids.add(event.target_id)
                bound_events.append(event.model_copy(update={
                    "metadata": {**event.metadata, "semantic_rewrite_sha256": fingerprint},
                }))
            bindings.append(binding)
            writes.append(write)
    except Exception as exc:
        return application._rejected(
            application.GRAPH_COMMIT_REJECTED_BINDING, request_id=identity,
            errors=[f"invalid_rewrite_binding:{type(exc).__name__}"],
        )
    try:
        resolve_journal_database_path(config).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return application._rejected(
            application.GRAPH_COMMIT_NOT_CONFIGURED, request_id=identity,
            errors=[f"rewrite_unavailable:{type(exc).__name__}"],
        )
    with application._handles(config, store) as (journal, graph_store):
        if journal is None or graph_store is None or missing_graph_store_operations(graph_store):
            return application._rejected(application.GRAPH_COMMIT_NOT_CONFIGURED, request_id=identity)
        return _commit_group(config, request, journal, graph_store, identity,
                             writes, bindings, bound_events)


def _layout_errors(config, events, candidate):
    nodes, _edges = application._materialize(candidate)
    domains: dict[str, list[projection.SurfacePlan]] = {}
    for plan in projection.surface_plans(events, nodes):
        domains.setdefault(plan.domain, []).append(plan)
    errors = []
    for domain, plans in domains.items():
        path = _domain_path(config, domain)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        rendered, _report = projection.render_surface_document(raw, plans)
        final = raw if rendered is None else rendered
        for plan in plans:
            body = projection._read_surface_body(final, plan)
            expected = plan.normalized_body if plan.action == "publish" else (
                "" if plan.kind == projection.PROJECTION_PREAMBLE_SURFACE_KIND else None
            )
            if body != expected:
                errors.append(f"binding_layout_mismatch:{plan.entry_id}")
    return errors


def _commit_group(config, request, journal, store, identity, writes, bindings, bound_events):
    old_list = [node_id for target in request.targets for node_id in target.old_node_ids]
    old_ids = set(old_list)
    finish_fields = dict(
        identity=identity, node_id=request.targets[0].node.id, entry_id=bindings[0].entry_id,
        event_ids=[event.id for event in bound_events], superseded_ids=sorted(old_ids), warnings=(),
    )
    stored = journal.request_hash(identity)
    if stored is not None:
        if stored != canonical_request_hash(bound_events):
            return application._rejected(application.GRAPH_COMMIT_REJECTED_REQUEST, request_id=identity)
        return application._finish(config, journal, store, duplicate=True, **finish_fields)
    events = journal.read_events()
    if not events and store.health().node_count:
        return application._rejected(application.GRAPH_COMMIT_REJECTED_ADOPTION, request_id=identity)
    existing = InMemoryGraphStore()
    existing_report = existing.rebuild(events)
    nodes, edges = application._materialize(existing)
    owned = projection.bindings_from_events(events)
    status = _precondition_status(request, journal, nodes, old_ids, owned)
    if status is not None:
        return application._rejected(status, request_id=identity)
    new_ids = [target.node.id for target in request.targets]
    if (len(new_ids) != len(set(new_ids))
            or any(node_id in nodes for node_id in new_ids)):
        return application._rejected(application.GRAPH_COMMIT_REJECTED_INVALID, request_id=identity)
    if len({binding.entry_id for binding in bindings}) != len(bindings):
        return application._rejected(application.GRAPH_COMMIT_REJECTED_SURFACE, request_id=identity)
    for write, binding in zip(writes, bindings):
        if write.node.kind == NodeKind.TASK:
            return application._rejected(application.GRAPH_COMMIT_REJECTED_UNSUPPORTED_SURFACE,
                                         request_id=identity)
        if (not write.node.is_current() or not _scope_allowed(config, write.node)
                or (request.scope is not None and request.scope != write.node.scope)):
            return application._rejected(application.GRAPH_COMMIT_REJECTED_INVALID, request_id=identity)
        errors = application._lifecycle_errors(write, nodes)
        if errors or any(node_id not in owned for node_id in write.target_ids):
            return application._rejected(application.GRAPH_COMMIT_REJECTED_INVALID,
                                         request_id=identity, errors=errors)
        for node_id in write.target_ids:
            old = owned[node_id]
            try:
                path = _domain_path(config, old.domain)
            except (ValueError, OSError):
                return application._rejected(application.GRAPH_COMMIT_REJECTED_BINDING,
                                             request_id=identity)
            record = journal.surface(old.entry_id)
            raw = path.read_text(encoding="utf-8") if path.exists() else ""
            if (record is not None and record.status != "retired"
                    and (not path.exists() or projection._read_surface_body(raw, old) is None)):
                return application._rejected(application.GRAPH_COMMIT_REJECTED_DRIFT,
                                             request_id=identity)
        conflict = application._surface_conflict(events, nodes, binding, write.node.id, old_ids)
        if conflict:
            return application._rejected(application.GRAPH_COMMIT_REJECTED_SURFACE,
                                         request_id=identity, errors=[conflict])
        try:
            plan_semantic_write(write, existing_nodes=nodes, existing_edges=edges)
        except ValueError:
            return application._rejected(application.GRAPH_COMMIT_REJECTED_INVALID, request_id=identity)
    candidate = InMemoryGraphStore()
    report = candidate.rebuild([*events, *bound_events])
    if not existing_report.ok or not report.ok:
        return application._rejected(application.GRAPH_COMMIT_REJECTED_INVALID, request_id=identity)
    try:
        layout = _layout_errors(config, [*events, *bound_events], candidate)
    except (ValueError, OSError):
        return application._rejected(application.GRAPH_COMMIT_REJECTED_BINDING, request_id=identity)
    if layout:
        return application._rejected(application.GRAPH_COMMIT_REJECTED_BINDING,
                                     request_id=identity, errors=layout)
    drift = application._drift_errors(config, journal, candidate, [*events, *bound_events])
    if drift:
        return application._rejected(application.GRAPH_COMMIT_REJECTED_DRIFT,
                                     request_id=identity, errors=drift)
    try:
        appended = journal.append(identity, bound_events, kind=REQUEST_KIND_SEMANTIC_WRITE)
    except JournalConflictError:
        return application._rejected(application.GRAPH_COMMIT_REJECTED_REQUEST, request_id=identity)
    return application._finish(config, journal, store, duplicate=appended.duplicate, **finish_fields)

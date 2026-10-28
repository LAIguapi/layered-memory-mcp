"""Legacy snapshot → deterministic bootstrap ``MemoryEvent`` stream.

WHAT THIS MODULE IS
-------------------
The step that turns a validated legacy snapshot into something the framework's
*authority* can hold. ARCHITECTURE_v2.md is explicit about why this is not just
"run the importer at the destination":

    Reuse the legacy importer only on a disposable in-memory planning store,
    convert its validated snapshot into deterministic bootstrap MemoryEvents,
    and rebuild the destination from the persisted journal. Never publish the
    importer's partial per-record writes as the authority.

So that is exactly the shape here:

1. :class:`~layered_memory_mcp.graph_legacy_importer.LegacyGraphImporter` runs
   against a throwaway
   :class:`~layered_memory_mcp.graph_fixture_store.InMemoryGraphStore`.
   The importer writes per record and *reports* refusals rather than raising, so
   its report is checked and any error is a blocking finding — a half-projected
   snapshot never becomes a migration.
2. The planning store's own ``snapshot()`` is read back and re-validated into
   domain objects. Nothing the store refused can survive this step.
3. Each node and each edge becomes one ``MemoryEventKind.MIGRATE`` event with a
   derived identity, so the stream is a function of the snapshot alone: the same
   source projects the same events, in the same order, with the same IDs.
4. The claims whose readable surface this framework can own carry their
   :class:`~layered_memory_mcp.graph_projection.ProjectionBinding` *in the event
   metadata*, which is what makes the readable L1 surface replayable from the
   journal after a restart.

WHAT IT IS NOT
--------------
- NOT a writer. It opens no database, creates no file and holds no handle. The
  events are returned; persisting them is the generation builder's job.
- NOT an extractor or a merger. No body is read to decide what it means, no
  supersession or contradiction is inferred, and no timestamp is invented for a
  statement: ``valid_from``/``valid_to`` stay exactly as absent as the legacy
  data left them.
- NOT an adopter of external surfaces. A frontmatter-backed (REST-owned) entry
  gets its claim node and its vector identity, and explicitly gets **no**
  readable binding: see :attr:`BootstrapReport.external_surfaces`. Claiming one
  would mean the framework could republish a document it does not own.

ON THE CURRENCY OF A BOUND SURFACE (§8.2)
-----------------------------------------
A bound surface is always a *current* claim, and that is a property of the
input rather than a check here: the projection producer emits every ordinary
Markdown section as ``review_status = approved``, which the importer maps to
``NodeStatus.ACTIVE``. A legacy ``rejected`` entry only exists as a
frontmatter-backed one, and those are never bound. So this module cannot
produce a binding whose node a later framework republication would retire —
the one way a readable section could disappear without anybody editing it.

ON ``recorded_at`` (§6.3)
------------------------
``valid_from``/``valid_to`` are the statement's own validity and are never
fabricated. ``recorded_at`` is a different field — *when we learned it* — and
for an ordinary Markdown section the legacy source carries no timestamp at all.
The framework's own projection producer therefore defaults it to the moment it
ran, which would make two readings of one unchanged file differ. This module
does **not** copy that incidental clock into a migration event. When the caller
does not supply an explicit ``recorded_at``, it uses the fixed
``MISSING_RECORDED_AT`` sentinel and reports that the source did not provide a
recording time. An entry that *does* carry an authored source extraction time
(a frontmatter entry with ``source.extracted_at``) keeps it untouched.

That substitution is *declared* rather than silent:
:data:`INFERRED_MIGRATION_RECORDED_AT` names an explicit caller-supplied instant
in :attr:`BootstrapReport.inferred_fields` (and so in the manifest's
provenance), while the stable missing-time token is recorded as
``claim.recorded_at:missing_source``. :data:`WARNING_MISSING_RECORDED_AT`
counts the sections to which the missing-time rule applied. The same stable
instant is also used for the legacy ``SourceInfo.extracted_at`` default on
ordinary Markdown entries: that field is importer provenance, not an authored
source timestamp, and carrying its runtime default would make the event
payload non-deterministic. An explicit caller-supplied instant is still
accepted verbatim and is reported as the migration run time. In either case
this remains a recording time only: no clock read becomes ``valid_from`` or
``valid_to``, which stay exactly as absent as the legacy data left them.

PRIVACY (§15)
-------------
:class:`BootstrapReport` carries counts, identities, derived node IDs and the
importer's own bounded tokens. The events themselves carry memory content by
construction — they are the journal payload — and are deliberately not part of
the reporting half.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from .graph_fixture_store import InMemoryGraphStore
from .graph_legacy_importer import LegacyGraphImporter, legacy_claim_id
from .graph_models import (
    GraphEdge,
    GraphModel,
    GraphNode,
    MemoryEvent,
    MemoryEventKind,
)
from .graph_projection import (
    PROJECTION_METADATA_KEY,
    ProjectionBinding,
    binding_metadata,
)
from .graph_migration_inventory import SourceSnapshot
from .storage.frontmatter import parse_frontmatter

__all__ = [
    "BOOTSTRAP_EVENT_SCHEME",
    "BOOTSTRAP_REQUEST_PREFIX",
    "FINDING_BINDING_DISAGREEMENT",
    "FINDING_IMPORTER_ERROR",
    "FINDING_REPLAY_MISMATCH",
    "INFERRED_MIGRATION_RECORDED_AT",
    "MISSING_RECORDED_AT",
    "MIGRATION_PROVENANCE",
    "WARNING_MISSING_RECORDED_AT",
    "WARNING_MIGRATION_RECORDED_AT",
    "BootstrapPlan",
    "BootstrapReport",
    "plan_bootstrap",
]

#: Prefix of every bootstrap event identity, with the serialization version in
#: it. A later event-building rule mints ``gmig2_…`` identities, so which rule
#: produced an event is readable without a lookup table.
BOOTSTRAP_EVENT_SCHEME = "gmig1"

#: Prefix of the journal request identity one migration commits under. The
#: source fingerprint completes it, so the same snapshot always commits under
#: the same identity and a *different* snapshot can never land under it.
BOOTSTRAP_REQUEST_PREFIX = "graph_migration_bootstrap"

#: Carried in every bootstrap event's metadata. Deliberately free of clocks,
#: paths and tokens: an event payload that embedded the run would make two
#: migrations of one snapshot two different decisions.
MIGRATION_PROVENANCE: dict[str, Any] = {
    "tool": "graph_migration",
    "rule": BOOTSTRAP_EVENT_SCHEME,
    "kind": "legacy_bootstrap",
}

#: Named in :attr:`BootstrapReport.inferred_fields`, and from there in the
#: generation manifest's provenance, whenever an ordinary Markdown section's
#: ``recorded_at`` is the migration run's own instant rather than something the
#: source stated. Written in the importer's ``<target>:<source>`` spelling so a
#: reader of the manifest does not have to know which layer inferred it.
#:
#: It is deliberately *only* about ``recorded_at`` — when the migration learned
#: the material. ``valid_from``/``valid_to`` are the statement's own validity,
#: are not inferred anywhere in this module, and a clock read must never be
#: presented as either.
INFERRED_MIGRATION_RECORDED_AT = "claim.recorded_at:migration_run"

#: A stable, explicitly synthetic value for a missing source recording time.
#: ``recorded_at`` is required by the graph model, but inventing the current
#: clock would make an unchanged source produce different event payloads.
MISSING_RECORDED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: The operator-facing half of the missing-time fact: ``<token>:<count>``, the
#: number of ordinary Markdown sections whose recording time the source never
#: carried.
WARNING_MISSING_RECORDED_AT = "recorded_at_missing_source"

#: The operator-facing half of an explicitly supplied migration instant.
WARNING_MIGRATION_RECORDED_AT = "recorded_at_is_migration_run"

FINDING_IMPORTER_ERROR = "legacy_importer_error"
FINDING_BINDING_DISAGREEMENT = "binding_identity_disagreement"
FINDING_REPLAY_MISMATCH = "bootstrap_replay_mismatch"
FINDING_MISSING_CLAIM = "bootstrap_claim_missing"


class BootstrapReport(GraphModel):
    """The content-free account of one bootstrap plan."""

    request_id: str = ""
    events: int = Field(default=0, ge=0)
    nodes: int = Field(default=0, ge=0)
    edges: int = Field(default=0, ge=0)
    bound_surfaces: int = Field(default=0, ge=0)
    external_surfaces: list[str] = Field(default_factory=list)
    seeds: int = Field(default=0, ge=0)
    entries_read: int = Field(default=0, ge=0)
    changelog_events_read: int = Field(default=0, ge=0)
    importer_skipped: int = Field(default=0, ge=0)
    inferred_fields: list[str] = Field(default_factory=list)
    unresolved_references: list[str] = Field(default_factory=list)
    recorded_at: str = ""
    findings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass
class BootstrapPlan:
    """The working half: the events, the bindings and the seed identities."""

    report: BootstrapReport
    events: list[MemoryEvent] = field(default_factory=list)
    #: ``node_id -> binding`` for every claim this framework may publish.
    bindings: dict[str, ProjectionBinding] = field(default_factory=dict)
    #: ``entry_id -> node_id`` for every bound readable surface.
    surfaces: dict[str, str] = field(default_factory=dict)
    #: ``L1 entry_id -> node_id`` for every authored object in the snapshot.
    entry_seeds: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.report.ok


def _event_id(prefix: str, identity: str) -> str:
    return f"{BOOTSTRAP_EVENT_SCHEME}_{prefix}_{identity}"


def _source_extracted_at_is_authored(snapshot: SourceSnapshot, item) -> bool:
    """Whether a frontmatter entry explicitly carries source extraction time."""
    raw = snapshot.documents.get(item.domain, "")
    metadata, _body = parse_frontmatter(raw)
    source = metadata.get("source") if isinstance(metadata, dict) else None
    return isinstance(source, dict) and bool(source.get("extracted_at"))


def _normalized_entries(snapshot: SourceSnapshot, recorded_at: datetime):
    """The L1 entries the importer is handed, with one declared recording time.

    An entry whose document carried authored timestamps keeps them. An ordinary
    Markdown section has none — the producer defaulted them to its own clock —
    so both are replaced by the caller's explicit instant or the stable
    missing-time sentinel. See the module docstring: this removes an incidental
    clock read, it does not invent a validity the source never had.
    """
    prepared = []
    for item in snapshot.entries:
        source = item.entry.source.model_copy(
            update={"extracted_at": recorded_at}
        )
        if item.frontmatter_owned:
            if _source_extracted_at_is_authored(snapshot, item):
                prepared.append(item.entry)
            else:
                prepared.append(item.entry.model_copy(update={"source": source}))
            continue
        prepared.append(
            item.entry.model_copy(
                update={
                    "created_at": recorded_at,
                    "updated_at": recorded_at,
                    "source": source,
                }
            )
        )
    return prepared


def plan_bootstrap(
    snapshot: SourceSnapshot, *, recorded_at: datetime | None = None
) -> BootstrapPlan:
    """Convert a validated source snapshot into a bootstrap event stream.

    Args:
        snapshot: The frozen reading from
            :func:`~layered_memory_mcp.graph_migration_inventory.read_source_snapshot`.
        recorded_at: The one explicit instant recorded as "when the migration
            learned this" for material the source never timestamped. Defaults to the
            stable :data:`MISSING_RECORDED_AT` sentinel, and is echoed in
            :attr:`BootstrapReport.recorded_at`.

    Returns:
        A :class:`BootstrapPlan`. A refused import is a finding, never an
        exception, and a plan with findings is not publishable.
    """
    explicit_recorded_at = recorded_at is not None
    moment = recorded_at if explicit_recorded_at else MISSING_RECORDED_AT
    findings: set[str] = set()
    warnings: set[str] = set()

    entries = _normalized_entries(snapshot, moment)

    # The sections whose recording time the source never stated, and which
    # therefore carry the caller's instant or the stable missing-time sentinel.
    # Stated rather than left implicit: a reader of the published graph must be
    # able to tell a migrated recording time from an authored one, and a missing
    # time from both.
    restamped = sum(1 for item in snapshot.entries if not item.frontmatter_owned)
    if restamped:
        warning = (
            WARNING_MIGRATION_RECORDED_AT
            if explicit_recorded_at
            else WARNING_MISSING_RECORDED_AT
        )
        warnings.add(f"{warning}:{restamped}")

    # 1. The importer, on a disposable planning store. Its per-record writes are
    #    never the authority — they are a proposal this module validates.
    planning = InMemoryGraphStore()
    report = LegacyGraphImporter(planning).import_snapshot(
        entries, snapshot.changelog_events
    )
    for error in report.errors:
        findings.add(f"{FINDING_IMPORTER_ERROR}:{error}")
    warnings.update(report.warnings)

    # 2. Read the validated snapshot back through the store's own contract.
    projected = planning.snapshot()
    nodes = [GraphNode.model_validate(payload) for payload in projected["nodes"]]
    edges = [GraphEdge.model_validate(payload) for payload in projected["edges"]]
    by_id = {node.id: node for node in nodes}

    # 3. Readable bindings for the surfaces this framework may own.
    bindings: dict[str, ProjectionBinding] = {}
    surfaces: dict[str, str] = {}
    entry_seeds: dict[str, str] = {}
    for item in snapshot.entries:
        node_id = legacy_claim_id(item.entry_id)
        entry_seeds[item.entry_id] = node_id
        if node_id not in by_id:
            findings.add(f"{FINDING_MISSING_CLAIM}:{item.entry_id}")
            continue
        if item.frontmatter_owned or item.occurrence is None:
            continue
        binding = ProjectionBinding(
            domain=item.domain,
            section=item.section,
            occurrence=item.occurrence,
            body=by_id[node_id].content,
        )
        if binding.entry_id != item.entry_id:
            findings.add(f"{FINDING_BINDING_DISAGREEMENT}:{item.entry_id}")
            continue
        bindings[node_id] = binding
        surfaces[item.entry_id] = node_id

    # 4. One event per node and per edge, identity derived from the subject.
    events: list[MemoryEvent] = []
    for node in nodes:
        metadata: dict[str, Any] = {"migration": dict(MIGRATION_PROVENANCE)}
        binding = bindings.get(node.id)
        if binding is not None:
            metadata[PROJECTION_METADATA_KEY] = binding_metadata(binding)
        events.append(
            MemoryEvent(
                id=_event_id("node", node.id),
                kind=MemoryEventKind.MIGRATE,
                occurred_at=node.recorded_at,
                node=node,
                metadata=metadata,
            )
        )
    for edge in edges:
        events.append(
            MemoryEvent(
                id=_event_id("edge", edge.id),
                kind=MemoryEventKind.MIGRATE,
                occurred_at=edge.recorded_at,
                edge=edge,
                metadata={"migration": dict(MIGRATION_PROVENANCE)},
            )
        )

    # 5. The proposed stream must project back to exactly what was planned.
    #    A rebuild is what the destination will do, so proving it here is what
    #    keeps "the journal is the authority" a fact rather than an intention.
    replay = InMemoryGraphStore()
    replay_report = replay.rebuild(events)
    if not replay_report.ok:
        for error in replay_report.errors:
            findings.add(f"{FINDING_REPLAY_MISMATCH}:{error}")
    elif replay.snapshot() != projected:
        findings.add(f"{FINDING_REPLAY_MISMATCH}:snapshot_differs")

    request_id = (
        f"{BOOTSTRAP_REQUEST_PREFIX}:{snapshot.inventory.source_fingerprint}"
    )
    external = snapshot.external_entry_ids()
    warnings.update(f"surface_externally_owned:{entry_id}" for entry_id in external)

    inferred = set(report.inferred_fields)
    if restamped:
        inferred.add(
            INFERRED_MIGRATION_RECORDED_AT
            if explicit_recorded_at
            else "claim.recorded_at:missing_source"
        )

    return BootstrapPlan(
        report=BootstrapReport(
            request_id=request_id,
            events=len(events),
            nodes=len(nodes),
            edges=len(edges),
            bound_surfaces=len(bindings),
            external_surfaces=external,
            seeds=len(entry_seeds),
            entries_read=report.entries_read,
            changelog_events_read=report.changelog_events_read,
            importer_skipped=report.skipped,
            inferred_fields=sorted(inferred),
            unresolved_references=list(report.unresolved_references),
            recorded_at=moment.isoformat(),
            findings=sorted(findings),
            warnings=sorted(warnings),
        ),
        events=events,
        bindings=bindings,
        surfaces=surfaces,
        entry_seeds=entry_seeds,
    )

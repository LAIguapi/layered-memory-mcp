"""Legacy L1/changelog → graph snapshot import (ARCHITECTURE_v2.md §11 Phase 2).

WHAT THIS MODULE IS
-------------------
A pure, read-only projection of *explicitly supplied* legacy material into an
*explicitly supplied* :class:`~layered_memory_mcp.graph_store.GraphStore`:

    report = LegacyGraphImporter(store).import_snapshot(entries, events)

``entries`` are L1 :class:`~layered_memory_mcp.models.KnowledgeEntry` records and
``events`` are :class:`~layered_memory_mcp.memory_changelog.MemoryChangelogEntry`
provenance records. Both arrive as objects from the caller — this module never
discovers a file, resolves a config, opens a path or reads a real memory store.
That is a correctness property, not a convenience: a migration that can scan is
a migration that can scan the wrong thing.

WHAT IT IS NOT
--------------
- NOT a second ledger. L1 remains the source of truth for content (§4); the
  graph is a projection, and this importer only ever *writes* to the store it
  was handed.
- NOT a CLI, a startup hook, a migration job or a feature flag.
- NOT a write path. No MCP tool, recall path or storage module imports it.
- NOT an entity extractor. The only entities it creates come from legacy tags,
  and every one of them is marked ``INFERRED``.

THE PROPERTIES IT GUARANTEES
----------------------------
1. **Derived identity.** Node IDs come from the L1 entry identity through
   :func:`~layered_memory_mcp.graph_models.make_node_id` (§10.2), so the same
   snapshot projects the same graph on every run, in any order, and an edited
   body updates a node instead of minting one.
2. **Determinism.** Nothing here reads the clock or a random source. Every
   timestamp written comes from the supplied data, and every list in the report
   is sorted and deduplicated — so two runs produce byte-identical snapshots
   and byte-identical reports.
3. **Honest provenance** (§6.3). Every projected claim and evidence node carries
   ``DerivationMethod.MIGRATED``; anything the legacy field never encoded is
   marked ``INFERRED`` and named in :attr:`LegacyImportReport.inferred_fields`.
   A missing timestamp stays missing: ``valid_from``/``valid_to`` are never
   invented, and a changelog timestamp never becomes a fact's validity.
4. **Nothing disappears quietly** (§10.2). Unresolved ``related_ids``, unmatched
   changelog events, duplicate and conflicting entry identities and malformed
   input are all reported.

PRIVACY (§15)
-------------
This module logs nothing at all. The report carries counts, identifiers and
hashes — never a body, a summary, a quote or a changelog ``reason``. The only
place a legacy body is written is ``GraphNode.content`` on the projected claim,
which is the graph's own copy of the statement and subject to the normal source
privacy policy; the evidence node locates and fingerprints the body rather than
duplicating it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from pydantic import Field

from .graph_models import (
    Derivation,
    DerivationMethod,
    GraphEdge,
    GraphModel,
    GraphNode,
    MemoryClass,
    NodeKind,
    NodeStatus,
    RelationType,
    SourceRef,
    ensure_aware,
    make_edge_id,
    make_node_id,
)
from .graph_store import GraphStore
from .memory_changelog import MemoryChangelogEntry, hash_text
from .models import KnowledgeEntry, KnowledgeType, ReviewStatus

#: Recorded on every projected node as ``attributes["import_source"]``, so a
#: node's origin is visible without consulting the import report.
LEGACY_IMPORT_SOURCE = "l1_knowledge"

#: Identity namespace for a projected L1 entry. Part of the hashed source
#: identity (§10.2), so an L1 entry ID can never collide with an ID minted by
#: another projection that happens to reuse the same string.
LEGACY_ENTRY_NAMESPACE = "legacy_l1_entry"

#: Identity namespace for an entity projected from a legacy tag. Separate from
#: :data:`LEGACY_ENTRY_NAMESPACE` so a tag that happens to equal an entry ID
#: cannot land on the same identity.
LEGACY_TAG_NAMESPACE = "legacy_l1_tag"

#: Legacy ``type`` → ``memory_class``, for the values where the mapping is
#: unambiguous (§6.3). ``DECISION`` and ``RELATIONSHIP`` are deliberately absent:
#: a decision record is not obviously a fact, an observation, a belief or a
#: policy, and guessing would put a semantic label on the graph that the source
#: never carried. Those entries keep ``memory_class = None``, their legacy type
#: in ``attributes`` and an ``unmapped_memory_class`` warning — §12 Batch D
#: prefers fewer honest fields over speculative structure.
MEMORY_CLASS_BY_LEGACY_TYPE: dict[KnowledgeType, MemoryClass] = {
    KnowledgeType.FACT: MemoryClass.FACT,
    KnowledgeType.CONFIG: MemoryClass.FACT,
    KnowledgeType.PITFALL: MemoryClass.EXPERIENCE,
    KnowledgeType.PREFERENCE: MemoryClass.POLICY,
    KnowledgeType.PROCEDURE: MemoryClass.PROCEDURE,
}

#: Legacy ``review_status`` → node lifecycle status (§8.2). ``REJECTED`` becomes
#: ``RETRACTED`` because the graph keeps history rather than deleting it: a
#: rejected entry stays queryable and never injects as current state.
NODE_STATUS_BY_REVIEW_STATUS: dict[ReviewStatus, NodeStatus] = {
    ReviewStatus.APPROVED: NodeStatus.ACTIVE,
    ReviewStatus.PENDING: NodeStatus.PENDING,
    ReviewStatus.REJECTED: NodeStatus.RETRACTED,
}

#: Report tokens for the graph fields legacy data never encoded. Kept as
#: constants because they are the contract a caller inspects — the matching
#: per-node ``attributes["inferred_fields"]`` entries are the short field names.
INFERRED_MEMORY_CLASS = "claim.memory_class:legacy_type"
INFERRED_CLAIM_STATUS = "claim.status:legacy_review_status"
INFERRED_TAG_ENTITY = "entity:legacy_tag"
INFERRED_TAG_MENTIONS = "edge.MENTIONS:legacy_tag"


def legacy_claim_id(entry_id: str) -> str:
    """The stable Claim node ID for the L1 entry ``entry_id``.

    Keyed on the entry *identity* only — not its domain, section or body — so
    moving a section or editing a body updates one claim rather than orphaning
    it and minting a replacement.
    """
    return make_node_id(NodeKind.CLAIM, LEGACY_ENTRY_NAMESPACE, entry_id)


def legacy_evidence_id(entry_id: str) -> str:
    """The stable Evidence node ID for the L1 entry ``entry_id``.

    Also keyed on the L1 identity rather than the body (§12 Batch D): evidence
    whose ID moved with its content would make every edit look like a new
    source, and the body's own fingerprint already lives in ``quote_hash``.
    """
    return make_node_id(NodeKind.EVIDENCE, LEGACY_ENTRY_NAMESPACE, entry_id)


def legacy_tag_entity_id(tag: str) -> str:
    """The stable Entity node ID for the legacy tag ``tag``.

    Normalized the way :class:`~layered_memory_mcp.models.KnowledgeEntry` itself
    normalizes tags (lowercased, trimmed), so ``"Retrieval-Index"`` and
    ``"retrieval-index"`` are one entity rather than two.
    """
    return make_node_id(NodeKind.ENTITY, LEGACY_TAG_NAMESPACE, tag.lower().strip())


class LegacyImportReport(GraphModel):
    """What one :meth:`LegacyGraphImporter.import_snapshot` call did (§11 Phase 2).

    Counts plus the things that did not fit. ``nodes_written``/``edges_written``
    are the size of the projection rather than the number of changed rows, so a
    second identical import returns an identical report — which is what makes
    "is this import idempotent?" a question a fixture can answer.

    Every list is sorted and deduplicated, and every entry in one is a token of
    identifiers (``unresolved_related_id:<from>:<to>``) rather than prose, so
    the report is stable enough to assert on and carries no content.
    """

    entries_read: int = Field(default=0, ge=0)
    changelog_events_read: int = Field(default=0, ge=0)
    nodes_written: int = Field(default=0, ge=0)
    edges_written: int = Field(default=0, ge=0)
    #: Input records that projected nothing: a duplicate, a conflicting
    #: identity, an unmatched changelog event, an unsupported object.
    skipped: int = Field(default=0, ge=0)
    #: Graph fields the legacy data never encoded, named so a reader can tell an
    #: inferred value from a migrated one (§6.3).
    inferred_fields: list[str] = Field(default_factory=list)
    #: References the snapshot named but did not contain.
    unresolved_references: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class _Projection:
    """The graph a snapshot projects to, assembled in full before any write.

    Building the whole projection first is what makes the result independent of
    input order: a tag shared by two entries, or an entry whose ``related_ids``
    point forward, are resolved against the finished plan rather than against
    whatever had been written so far.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.errors: set[str] = set()
        self.warnings: set[str] = set()
        self.inferred: set[str] = set()
        self.unresolved: set[str] = set()
        self.skipped = 0
        #: legacy tag → the earliest ``recorded_at`` of the entries carrying it,
        #: so a shared tag's entity node does not depend on projection order.
        self.tag_first_seen: dict[str, datetime] = {}

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges[edge.id] = edge

    def note_tag(self, tag: str, recorded_at: datetime) -> None:
        seen = self.tag_first_seen.get(tag)
        self.tag_first_seen[tag] = (
            recorded_at if seen is None else min(seen, recorded_at)
        )


def _iso(value: datetime | None) -> str | None:
    """A timezone-aware ISO 8601 string, or ``None``.

    Attributes are plain JSON (``dict[str, Any]`` is not validated field by
    field), so timestamps are written as strings rather than left as
    ``datetime`` objects a serializer would have to guess at.
    """
    aware = ensure_aware(value)
    return None if aware is None else aware.isoformat()


def _event_metadata(event: MemoryChangelogEntry) -> dict[str, object]:
    """The structural part of a changelog event, as plain JSON.

    Identifiers, hashes and timestamps only. ``summary``, ``reason`` and
    ``metadata`` are deliberately dropped: they are free text, which is the one
    place a legacy body or a credential could ride into the graph (§15), and
    the changelog file itself remains the place to read them.

    ``observed_at`` is named for what it is — when the framework *recorded* a
    change to this section. It is not the fact's validity.
    """
    structural = {
        "event_id": event.event_id,
        "operation": event.operation,
        "surface": event.surface,
        "observed_at": _iso(event.at),
        "actor": event.actor,
        "agent_id": event.agent_id,
        "tool": event.tool,
        "source_ref": event.source_ref,
        "before_hash": event.before_hash,
        "after_hash": event.after_hash,
    }
    return {key: value for key, value in structural.items() if value is not None}


class LegacyGraphImporter:
    """Projects supplied legacy L1/changelog material into a ``GraphStore``.

    Backend-neutral by construction: it holds the §9.1 contract and nothing
    else, so the same importer fills the deterministic fixture store today and
    an embedded backend (Batch E) unchanged.
    """

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    def import_snapshot(
        self,
        entries: Sequence[KnowledgeEntry],
        changelog_events: Sequence[MemoryChangelogEntry] = (),
    ) -> LegacyImportReport:
        """Project ``entries`` (and ``changelog_events``) into the store.

        Re-runnable: identity is derived, so ``create_node`` upserts the same
        node and ``append_edge`` sees an identical re-append (a no-op) rather
        than a conflicting one.
        """
        plan = _Projection()
        usable = self._usable_entries(entries, plan)
        events_by_entry = self._match_changelog_events(
            self._usable_events(changelog_events, plan), usable, plan
        )

        for entry_id in sorted(usable):
            self._project_entry(
                entry_id, usable[entry_id], usable, plan,
                events_by_entry.get(entry_id, []),
            )
        self._project_tag_entities(plan)

        nodes_written, edges_written = self._write(plan)

        return LegacyImportReport(
            entries_read=len(entries),
            changelog_events_read=len(changelog_events),
            nodes_written=nodes_written,
            edges_written=edges_written,
            skipped=plan.skipped,
            inferred_fields=sorted(plan.inferred),
            unresolved_references=sorted(plan.unresolved),
            errors=sorted(plan.errors),
            # Unresolved references are *also* warnings: a caller that only
            # reads ``warnings`` must not miss a reference the snapshot named
            # and the graph could not satisfy (§10.2).
            warnings=sorted(plan.warnings | plan.unresolved),
        )

    # -- input normalization -------------------------------------------------

    @staticmethod
    def _usable_entries(
        entries: Sequence[KnowledgeEntry], plan: _Projection
    ) -> dict[str, KnowledgeEntry]:
        """The entries that can be projected, keyed by their L1 identity.

        Three input problems are *reported* here rather than projected (§10.2):

        * an object that is not a ``KnowledgeEntry`` — ``unsupported_entry``;
        * an entry whose ID is blank or multi-line, which has no stable graph
          identity at all — ``invalid_entry_id``;
        * two records for one identity.

        The duplicate policy, spelled out because "it depends on write order" is
        exactly what must not happen: **identical** repeats are a benign replay
        (``duplicate_entry`` warning, counted as skipped), while **differing**
        records make the identity ambiguous, so *neither* is projected and
        ``conflicting_entry`` is an error. Dropping both is the only choice that
        is independent of input order — picking "the last one" would make the
        graph depend on how the snapshot was listed — and it leaves the claim
        absent rather than quietly asserting one of two contradictory bodies.
        The caller fixes the snapshot and reruns.
        """
        occurrences: dict[str, int] = {}
        counted_skips: dict[str, int] = {}
        accepted: dict[str, KnowledgeEntry] = {}
        conflicting: set[str] = set()

        for index, entry in enumerate(entries):
            if not isinstance(entry, KnowledgeEntry):
                plan.errors.add(f"unsupported_entry:{index}:{type(entry).__name__}")
                plan.skipped += 1
                continue
            # The same normalization ``make_node_id`` applies, so a padded ID
            # cannot become a second identity for the same entry.
            entry_id = entry.id.strip()
            if not entry_id or "\n" in entry.id or "\r" in entry.id:
                plan.errors.add(f"invalid_entry_id:{index}")
                plan.skipped += 1
                continue

            occurrences[entry_id] = occurrences.get(entry_id, 0) + 1
            prior = accepted.get(entry_id)
            if prior is None:
                accepted[entry_id] = entry
            elif prior == entry:
                plan.warnings.add(f"duplicate_entry:{entry_id}")
                plan.skipped += 1
                counted_skips[entry_id] = counted_skips.get(entry_id, 0) + 1
            else:
                plan.errors.add(f"conflicting_entry:{entry_id}")
                conflicting.add(entry_id)

        for entry_id in conflicting:
            accepted.pop(entry_id, None)
            # Every copy of an ambiguous identity projected nothing; the ones
            # already counted as a benign replay must not be counted twice.
            plan.skipped += occurrences[entry_id] - counted_skips.get(entry_id, 0)
        return accepted

    @staticmethod
    def _usable_events(
        changelog_events: Sequence[MemoryChangelogEntry], plan: _Projection
    ) -> dict[str, MemoryChangelogEntry]:
        """The changelog events that can enrich a claim, keyed by ``event_id``.

        The same duplicate policy as :meth:`_usable_entries`, for the same
        reason: an identical replay is benign, and two different payloads under
        one event ID make the event's own identity ambiguous, so neither is used.
        """
        occurrences: dict[str, int] = {}
        counted_skips: dict[str, int] = {}
        accepted: dict[str, MemoryChangelogEntry] = {}
        conflicting: set[str] = set()

        for index, event in enumerate(changelog_events):
            if not isinstance(event, MemoryChangelogEntry):
                plan.errors.add(
                    f"unsupported_changelog_event:{index}:{type(event).__name__}"
                )
                plan.skipped += 1
                continue

            event_id = event.event_id
            occurrences[event_id] = occurrences.get(event_id, 0) + 1
            prior = accepted.get(event_id)
            if prior is None:
                accepted[event_id] = event
            elif prior == event:
                plan.warnings.add(f"duplicate_changelog_event:{event_id}")
                plan.skipped += 1
                counted_skips[event_id] = counted_skips.get(event_id, 0) + 1
            else:
                plan.errors.add(f"conflicting_changelog_event:{event_id}")
                conflicting.add(event_id)

        for event_id in conflicting:
            accepted.pop(event_id, None)
            plan.skipped += occurrences[event_id] - counted_skips.get(event_id, 0)
        return accepted

    # -- changelog events ----------------------------------------------------

    @staticmethod
    def _match_changelog_events(
        changelog_events: dict[str, MemoryChangelogEntry],
        usable: dict[str, KnowledgeEntry],
        plan: _Projection,
    ) -> dict[str, list[dict[str, object]]]:
        """Attach each event to the one L1 entry it can only be about.

        A changelog event is *input evidence*, not a second truth store (§4), so
        matching is deliberately strict: an event is attached only when its
        ``surface`` is :data:`LEGACY_IMPORT_SOURCE` **and** its ``domain`` and
        ``section`` identify exactly one supplied entry. An event from another
        surface (§12 Batch D), a domain-level event (no section), an event about
        a section this snapshot does not contain, and an event whose
        ``(domain, section)`` matches two entries are each reported and attached
        to nothing — a guessed match would put another layer's or another
        entry's history on this claim, and manufacturing a claim from an event
        would invent a fact nobody wrote.

        The surface check matters on its own: an L0 index and an L1 section can
        share a domain and a heading, so ``(domain, section)`` alone would let
        one layer's history land on the other layer's claim.
        """
        by_identity: dict[tuple[str, str], list[str]] = {}
        for entry_id, entry in usable.items():
            by_identity.setdefault((entry.domain, entry.section), []).append(entry_id)

        matched: dict[str, list[dict[str, object]]] = {}
        for event_id in sorted(changelog_events):
            event = changelog_events[event_id]
            section = event.section
            candidates = (
                sorted(by_identity.get((event.domain, section), []))
                if section and event.surface == LEGACY_IMPORT_SOURCE
                else []
            )
            if not candidates:
                plan.warnings.add(f"unmatched_changelog_event:{event_id}")
                plan.skipped += 1
                continue
            if len(candidates) > 1:
                plan.warnings.add(f"ambiguous_changelog_event:{event_id}")
                plan.skipped += 1
                continue
            matched.setdefault(candidates[0], []).append(_event_metadata(event))

        return {
            entry_id: sorted(
                events, key=lambda e: (str(e["observed_at"]), str(e["event_id"]))
            )
            for entry_id, events in matched.items()
        }

    # -- projection ----------------------------------------------------------

    def _project_entry(
        self,
        entry_id: str,
        entry: KnowledgeEntry,
        usable: dict[str, KnowledgeEntry],
        plan: _Projection,
        events: list[dict[str, object]],
    ) -> None:
        """Project one entry into a claim, its evidence and their relations.

        ``entry_id`` is the *normalized* L1 identity from
        :meth:`_usable_entries` — the one ``usable`` is keyed by — rather than
        the raw field, so an identity is resolved exactly one way everywhere.
        """
        claim_id = legacy_claim_id(entry_id)
        evidence_id = legacy_evidence_id(entry_id)
        recorded_at = ensure_aware(entry.created_at)
        quote_hash = hash_text(entry.content)
        source_ref = SourceRef(
            evidence_id=evidence_id,
            source_file=entry.domain,
            source_section=entry.section,
            quote_hash=quote_hash,
        )
        migrated = Derivation(method=DerivationMethod.MIGRATED)

        plan.add_node(
            GraphNode(
                id=evidence_id,
                kind=NodeKind.EVIDENCE,
                label=f"{entry.domain} / {entry.section}",
                recorded_at=recorded_at,
                source_refs=[source_ref],
                derivation=migrated,
                attributes={
                    "import_source": LEGACY_IMPORT_SOURCE,
                    "legacy_entry_id": entry_id,
                    "legacy_domain": entry.domain,
                    "legacy_section": entry.section,
                    "content_hash": quote_hash,
                },
            )
        )

        plan.add_node(
            GraphNode(
                id=claim_id,
                kind=NodeKind.CLAIM,
                label=entry.section,
                content=entry.content,
                status=NODE_STATUS_BY_REVIEW_STATUS[entry.review_status],
                memory_class=MEMORY_CLASS_BY_LEGACY_TYPE.get(entry.type),
                confidence=entry.confidence,
                recorded_at=recorded_at,
                source_refs=[source_ref],
                derivation=migrated,
                tags=list(entry.tags),
                attributes=self._claim_attributes(entry_id, entry, plan, events),
                version=entry.version,
            )
        )

        self._add_edge(
            plan,
            RelationType.EVIDENCED_BY,
            claim_id,
            evidence_id,
            recorded_at,
            migrated,
        )

        for tag in entry.tags:
            plan.note_tag(tag, recorded_at)
            plan.inferred.update({INFERRED_TAG_ENTITY, INFERRED_TAG_MENTIONS})
            self._add_edge(
                plan,
                RelationType.MENTIONS,
                evidence_id,
                legacy_tag_entity_id(tag),
                recorded_at,
                Derivation(method=DerivationMethod.INFERRED),
                attributes={"legacy_tag": tag},
            )

        for related_id in entry.related_ids:
            self._project_related_id(entry_id, related_id, usable, plan, recorded_at)

    @staticmethod
    def _claim_attributes(
        entry_id: str,
        entry: KnowledgeEntry,
        plan: _Projection,
        events: list[dict[str, object]],
    ) -> dict[str, object]:
        """The legacy fields the graph model has no column for.

        Nothing is dropped: ``summary``, the legacy ``type``, the review state,
        both legacy timestamps and the whole ``SourceInfo`` survive here, so the
        projection loses no information even where the graph vocabulary is
        narrower than L1's. ``inferred_fields`` makes the same honesty visible
        on the node itself that the report carries for the whole import — which
        is why this also records the inference in ``plan``.
        """
        inferred_names = ["status"]
        plan.inferred.add(INFERRED_CLAIM_STATUS)
        if entry.type in MEMORY_CLASS_BY_LEGACY_TYPE:
            inferred_names.append("memory_class")
            plan.inferred.add(INFERRED_MEMORY_CLASS)
        else:
            plan.warnings.add(f"unmapped_memory_class:{entry.type.value}")

        attributes: dict[str, object] = {
            "import_source": LEGACY_IMPORT_SOURCE,
            "legacy_entry_id": entry_id,
            "legacy_domain": entry.domain,
            "legacy_section": entry.section,
            "legacy_type": entry.type.value,
            "legacy_summary": entry.summary,
            "legacy_review_status": entry.review_status.value,
            "legacy_created_at": _iso(entry.created_at),
            "legacy_updated_at": _iso(entry.updated_at),
            "legacy_source_type": entry.source.type.value,
            "legacy_source_extracted_by": entry.source.extracted_by,
            "legacy_source_extracted_at": _iso(entry.source.extracted_at),
            "inferred_fields": sorted(inferred_names),
        }
        # Optional legacy fields are omitted rather than written as null, so
        # "the source never said" stays distinguishable from "the source said
        # nothing".
        if entry.source.session_id:
            attributes["legacy_source_session"] = entry.source.session_id
        if entry.source.message_range:
            attributes["legacy_source_message_range"] = list(
                entry.source.message_range
            )
        if entry.reviewed_by:
            attributes["legacy_reviewed_by"] = entry.reviewed_by
        if entry.reviewed_at is not None:
            attributes["legacy_reviewed_at"] = _iso(entry.reviewed_at)
        # Matched changelog events are *observed migration metadata*: they say
        # when the framework recorded a change to this section, which is not the
        # same thing as when the statement became true, so they never touch
        # ``valid_from`` / ``valid_to`` / ``recorded_at`` (§6.3).
        if events:
            attributes["legacy_changelog_events"] = events
        return attributes

    def _project_related_id(
        self,
        entry_id: str,
        related_id: str,
        usable: dict[str, KnowledgeEntry],
        plan: _Projection,
        recorded_at: datetime,
    ) -> None:
        """Project one ``related_ids`` entry as a weak ``RELATED_TO`` edge.

        Only a target the *same* snapshot projected gets an edge (§12 Batch D):
        a missing target becomes an unresolved reference, never a stub node that
        would make a dangling pointer look like a known entity.
        """
        target = related_id.strip()
        if not target:
            plan.warnings.add(f"blank_related_id:{entry_id}")
            return
        if target == entry_id:
            plan.warnings.add(f"self_related_id:{entry_id}")
            return
        if target not in usable:
            plan.unresolved.add(f"unresolved_related_id:{entry_id}:{target}")
            return
        self._add_edge(
            plan,
            RelationType.RELATED_TO,
            legacy_claim_id(entry_id),
            legacy_claim_id(target),
            recorded_at,
            # The legacy list *was* explicit, so the relation is migrated rather
            # than inferred — but RELATED_TO stays weak either way (§6.2).
            Derivation(method=DerivationMethod.MIGRATED),
            attributes={"legacy_relation": "related_ids"},
        )

    @staticmethod
    def _project_tag_entities(plan: _Projection) -> None:
        """Create one Entity node per legacy tag seen anywhere in the snapshot.

        A tag is a keyword, not a statement, so the entity is an identity node
        with no provenance requirement (§6.1) and its ``recorded_at`` is the
        earliest entry that carried it — a value that does not depend on which
        record was projected last.
        """
        for tag in sorted(plan.tag_first_seen):
            plan.add_node(
                GraphNode(
                    id=legacy_tag_entity_id(tag),
                    kind=NodeKind.ENTITY,
                    label=tag,
                    recorded_at=plan.tag_first_seen[tag],
                    attributes={
                        "import_source": LEGACY_IMPORT_SOURCE,
                        "legacy_tag": tag,
                    },
                )
            )

    @staticmethod
    def _add_edge(
        plan: _Projection,
        relation_type: RelationType,
        source_id: str,
        target_id: str,
        recorded_at: datetime,
        derivation: Derivation,
        attributes: dict[str, object] | None = None,
    ) -> None:
        """Plan one edge with an ID derived from its triple, so a rerun is a no-op."""
        edge_id = make_edge_id(relation_type, source_id, target_id)
        plan.add_edge(
            GraphEdge(
                id=edge_id,
                relation_type=relation_type,
                source_id=source_id,
                target_id=target_id,
                recorded_at=recorded_at,
                derivation=derivation,
                attributes=dict(attributes or {}),
            )
        )

    # -- writes --------------------------------------------------------------

    def _write(self, plan: _Projection) -> tuple[int, int]:
        """Write the projection through the store, reporting refusals.

        Writes are attempted one by one and a failure is *reported* rather than
        raised: an importer handed a whole snapshot must not lose the good
        records with the bad. The store contract (§9.1) names no exception
        type, so any refusal from any backend is caught and named here.
        """
        written_nodes = 0
        for node_id in sorted(plan.nodes):
            try:
                self._store.create_node(plan.nodes[node_id])
            except Exception as exc:  # noqa: BLE001 — backend-neutral refusal
                plan.errors.add(f"node_write_failed:{node_id}:{type(exc).__name__}")
                continue
            written_nodes += 1

        written_edges = 0
        for edge_id in sorted(plan.edges):
            edge = plan.edges[edge_id]
            dangling = [
                endpoint
                for endpoint in (edge.source_id, edge.target_id)
                if endpoint not in plan.nodes
            ]
            if dangling:
                # Reported, not hidden (§10.2): an edge the projection never
                # produced a node for would otherwise reach the store and only
                # surface later as a health error.
                for endpoint in dangling:
                    plan.errors.add(
                        f"unresolved_edge_endpoint:{edge_id}:{endpoint}"
                    )
                continue
            try:
                self._store.append_edge(edge)
            except Exception as exc:  # noqa: BLE001 — backend-neutral refusal
                plan.errors.add(f"edge_write_failed:{edge_id}:{type(exc).__name__}")
                continue
            written_edges += 1

        return written_nodes, written_edges


__all__ = [
    "INFERRED_CLAIM_STATUS",
    "INFERRED_MEMORY_CLASS",
    "INFERRED_TAG_ENTITY",
    "INFERRED_TAG_MENTIONS",
    "LEGACY_ENTRY_NAMESPACE",
    "LEGACY_IMPORT_SOURCE",
    "LEGACY_TAG_NAMESPACE",
    "MEMORY_CLASS_BY_LEGACY_TYPE",
    "NODE_STATUS_BY_REVIEW_STATUS",
    "LegacyGraphImporter",
    "LegacyImportReport",
    "legacy_claim_id",
    "legacy_evidence_id",
    "legacy_tag_entity_id",
]

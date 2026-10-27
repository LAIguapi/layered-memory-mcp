"""v3.6.0 Batch D — the legacy L1/changelog → graph snapshot importer.

Why this file exists: ARCHITECTURE_v2.md §11 Phase 2 / §12 Batch D require a
read-only projection of *explicitly supplied* legacy material — L1
``KnowledgeEntry`` records and MEMORY_CHANGELOG events — into the backend-neutral
``GraphStore`` contract. Batch C gave us a deterministic reference store; this is
the first thing that writes a real graph, and the first place where legacy data
can be made to look like something it never was.

These tests pin the properties that keep that from happening:

  * **Identity is derived, never minted.** A claim/evidence node ID comes from
    the L1 entry identity, so a rerun, a reordered snapshot or an edited body
    produce the same node — no random UUIDs, no content-keyed identity.
  * **Projection is lossless.** Domain, section, content, summary, type,
    confidence, version, tags, review status and timestamps all survive.
  * **Provenance is honest.** Every migrated projection carries
    ``DerivationMethod.MIGRATED``; anything the legacy field never encoded is
    marked ``INFERRED`` and named in the report; a missing timestamp stays
    missing rather than becoming a validity window.
  * **Nothing is silently dropped.** Unresolved ``related_ids``, unmatched
    changelog events, duplicate and conflicting entry identities, and malformed
    input are all reported — sorted, deduplicated and content-free.
  * **It is a pure function of its arguments.** The importer takes objects, not
    paths: it cannot read a file, a config or any real memory store.

Nothing here touches the hot path, a persistent backend or real memory data.
Fixture data is neutral technical vocabulary: a retrieval index and its rebuild
cadence.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

from layered_memory_mcp import graph_legacy_importer
from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
from layered_memory_mcp.graph_legacy_importer import (
    LEGACY_IMPORT_SOURCE,
    LegacyGraphImporter,
    LegacyImportReport,
    legacy_claim_id,
    legacy_evidence_id,
    legacy_tag_entity_id,
)
from layered_memory_mcp.graph_models import (
    DerivationMethod,
    MemoryClass,
    NodeKind,
    NodeStatus,
    RelationType,
    make_edge_id,
)
from layered_memory_mcp.memory_changelog import MemoryChangelogEntry, hash_text
from layered_memory_mcp.models import (
    KnowledgeEntry,
    KnowledgeType,
    ReviewStatus,
    SourceInfo,
    SourceType,
)

# --- neutral fixture vocabulary ---------------------------------------------

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)
T2 = T1 + timedelta(days=30)

DOMAIN = "retrieval"
SECTION = "Index maintenance"
ENTRY_ID = "entry_index_rebuild_cadence"
OTHER_ENTRY_ID = "entry_index_rebuild_window"
BODY = "The retrieval index rebuild runs nightly at 02:00."


def _entry(**kwargs) -> KnowledgeEntry:
    """One legacy L1 entry: no tags, no relations — the minimal projection."""
    data = {
        "id": ENTRY_ID,
        "domain": DOMAIN,
        "section": SECTION,
        "type": KnowledgeType.FACT,
        "content": BODY,
        "summary": "index rebuild runs nightly",
        "confidence": 0.8,
        "created_at": T0,
        "updated_at": T1,
        "version": 3,
        "review_status": ReviewStatus.APPROVED,
        "source": SourceInfo(type=SourceType.MANUAL, extracted_at=T0),
    }
    data.update(kwargs)
    return KnowledgeEntry(**data)


def _other_entry(**kwargs) -> KnowledgeEntry:
    data = {
        "id": OTHER_ENTRY_ID,
        "section": "Index rebuild window",
        "content": "The rebuild window closes at 04:00.",
        "summary": "rebuild window closes at 04:00",
        "created_at": T1,
        "updated_at": T1,
    }
    data.update(kwargs)
    return _entry(**data)


def _import(entries=(), events=(), store: InMemoryGraphStore | None = None):
    store = store if store is not None else InMemoryGraphStore()
    return store, LegacyGraphImporter(store).import_snapshot(entries, events)


# --- A. empty snapshot -------------------------------------------------------

def test_empty_snapshot_produces_a_healthy_empty_graph_and_zero_counts():
    store, report = _import()

    assert isinstance(report, LegacyImportReport)
    assert report.ok is True
    assert report.entries_read == 0
    assert report.changelog_events_read == 0
    assert report.nodes_written == 0
    assert report.edges_written == 0
    assert report.skipped == 0
    assert report.errors == []
    assert report.warnings == []
    assert report.inferred_fields == []
    assert report.unresolved_references == []
    assert store.snapshot() == InMemoryGraphStore().snapshot()
    assert store.health().healthy is True


# --- B. the minimal projection: Claim + Evidence + EVIDENCED_BY --------------

def test_one_entry_projects_a_claim_an_evidence_node_and_an_evidenced_by_edge():
    store, report = _import([_entry()])

    assert report.ok is True
    assert (report.entries_read, report.nodes_written) == (1, 2)
    assert report.edges_written == 1
    assert report.skipped == 0

    claim = store.get_node(legacy_claim_id(ENTRY_ID))
    evidence = store.get_node(legacy_evidence_id(ENTRY_ID))
    assert claim is not None and claim.kind is NodeKind.CLAIM
    assert evidence is not None and evidence.kind is NodeKind.EVIDENCE

    edges = store.get_edges(claim.id)
    assert [(e.relation_type, e.source_id, e.target_id) for e in edges] == [
        (RelationType.EVIDENCED_BY, claim.id, evidence.id)
    ]
    assert edges[0].id == make_edge_id(
        RelationType.EVIDENCED_BY, claim.id, evidence.id
    )
    assert store.health().healthy is True


def test_claim_provenance_resolves_to_the_projected_evidence_node():
    store, _ = _import([_entry()])

    provenance = store.get_provenance(legacy_claim_id(ENTRY_ID))
    assert provenance.resolved is True
    assert provenance.evidence_ids == [legacy_evidence_id(ENTRY_ID)]
    assert provenance.derivation is not None
    assert provenance.derivation.method is DerivationMethod.MIGRATED


# --- C. identity: derived from the L1 entry, not from content or order -------

def test_node_identity_is_derived_from_the_entry_id_not_from_its_content():
    first, _ = _import([_entry()])
    second, _ = _import([_entry(content="The rebuild now runs hourly.", version=4)])

    assert first.get_node(legacy_claim_id(ENTRY_ID)) is not None
    assert second.get_node(legacy_claim_id(ENTRY_ID)) is not None
    assert legacy_evidence_id(ENTRY_ID) == legacy_evidence_id(ENTRY_ID)
    assert second.get_node(legacy_evidence_id(ENTRY_ID)) is not None


def test_graph_snapshot_is_independent_of_input_order():
    forward, forward_report = _import([_entry(), _other_entry()])
    reverse, reverse_report = _import([_other_entry(), _entry()])

    assert forward.snapshot() == reverse.snapshot()
    assert forward_report.to_dict() == reverse_report.to_dict()


def test_repeated_import_of_the_same_snapshot_is_idempotent():
    store = InMemoryGraphStore()
    importer = LegacyGraphImporter(store)

    first = importer.import_snapshot([_entry(), _other_entry()])
    after_first = store.snapshot()
    second = importer.import_snapshot([_entry(), _other_entry()])

    assert store.snapshot() == after_first
    assert second.to_dict() == first.to_dict()
    assert store.health().healthy is True


# --- D. lossless projection of the legacy fields -----------------------------

def test_entry_fields_survive_the_projection():
    store, _ = _import([_entry(tags=["Retrieval-Index", "rebuild"])])
    claim = store.get_node(legacy_claim_id(ENTRY_ID))

    assert claim.label == SECTION
    assert claim.content == BODY
    assert claim.confidence == 0.8
    assert claim.version == 3
    assert claim.recorded_at == T0
    # Order preserved, so the projection does not reshuffle the legacy list.
    assert claim.tags == ["retrieval-index", "rebuild"]

    assert claim.attributes["legacy_entry_id"] == ENTRY_ID
    assert claim.attributes["legacy_domain"] == DOMAIN
    assert claim.attributes["legacy_section"] == SECTION
    assert claim.attributes["legacy_type"] == "fact"
    assert claim.attributes["legacy_summary"] == "index rebuild runs nightly"
    assert claim.attributes["legacy_review_status"] == "approved"
    assert claim.attributes["legacy_created_at"] == T0.isoformat()
    assert claim.attributes["legacy_updated_at"] == T1.isoformat()
    assert claim.attributes["legacy_source_type"] == "manual"
    assert claim.attributes["legacy_source_extracted_by"] == "human"
    assert claim.attributes["legacy_source_extracted_at"] == T0.isoformat()
    assert claim.attributes["import_source"] == LEGACY_IMPORT_SOURCE


def test_source_information_points_at_the_legacy_domain_section_and_body_hash():
    store, _ = _import([_entry()])
    claim = store.get_node(legacy_claim_id(ENTRY_ID))
    evidence = store.get_node(legacy_evidence_id(ENTRY_ID))

    for node in (claim, evidence):
        assert len(node.source_refs) == 1
        ref = node.source_refs[0]
        assert ref.source_file == DOMAIN
        assert ref.source_section == SECTION
        assert ref.evidence_id == legacy_evidence_id(ENTRY_ID)
        assert ref.quote_hash == hash_text(BODY)
        # No episode was projected, so none is claimed.
        assert ref.episode_id is None


def test_evidence_locates_and_fingerprints_the_body_without_duplicating_it():
    store, _ = _import([_entry()])
    evidence = store.get_node(legacy_evidence_id(ENTRY_ID))

    assert evidence.label == f"{DOMAIN} / {SECTION}"
    assert evidence.content == ""
    assert evidence.attributes["content_hash"] == hash_text(BODY)
    assert evidence.attributes["legacy_entry_id"] == ENTRY_ID


def test_source_session_is_recorded_as_an_attribute_not_as_a_phantom_episode():
    entry = _entry(
        source=SourceInfo(
            type=SourceType.SESSION,
            session_id="session_0007",
            message_range=(12, 18),
            extracted_by="framework",
            extracted_at=T1,
        )
    )
    store, _ = _import([entry])
    claim = store.get_node(legacy_claim_id(ENTRY_ID))

    assert claim.attributes["legacy_source_session"] == "session_0007"
    assert claim.attributes["legacy_source_message_range"] == [12, 18]
    assert all(ref.episode_id is None for ref in claim.source_refs)
    assert store.get_provenance(claim.id).episode_ids == []
    assert store.get_provenance(claim.id).resolved is True


def test_review_metadata_is_preserved_when_present_and_absent_when_not():
    reviewed, _ = _import(
        [_entry(reviewed_by="maintainer", reviewed_at=T2)]
    )
    attrs = reviewed.get_node(legacy_claim_id(ENTRY_ID)).attributes
    assert attrs["legacy_reviewed_by"] == "maintainer"
    assert attrs["legacy_reviewed_at"] == T2.isoformat()

    plain, _ = _import([_entry()])
    attrs = plain.get_node(legacy_claim_id(ENTRY_ID)).attributes
    assert "legacy_reviewed_by" not in attrs
    assert "legacy_reviewed_at" not in attrs


# --- E. honest provenance: migrated, inferred, and no invented validity ------

def test_absent_validity_timestamps_are_not_invented():
    store, _ = _import([_entry()])

    for node_id in (legacy_claim_id(ENTRY_ID), legacy_evidence_id(ENTRY_ID)):
        node = store.get_node(node_id)
        assert node.valid_from is None
        assert node.valid_to is None
    for edge in store.get_edges(legacy_claim_id(ENTRY_ID)):
        assert edge.valid_from is None
        assert edge.valid_to is None


def test_every_projection_is_marked_migrated_and_never_user_explicit():
    store, _ = _import([_entry(tags=["retrieval-index"])])
    snapshot = store.snapshot()

    methods = {
        item["derivation"]["method"]
        for item in snapshot["nodes"] + snapshot["edges"]
        if item.get("derivation")
    }
    assert methods == {
        DerivationMethod.MIGRATED.value,
        DerivationMethod.INFERRED.value,
    }
    assert DerivationMethod.USER_EXPLICIT.value not in methods


def test_review_status_maps_to_lifecycle_state_and_is_reported_as_inferred():
    for review_status, node_status in (
        (ReviewStatus.APPROVED, NodeStatus.ACTIVE),
        (ReviewStatus.PENDING, NodeStatus.PENDING),
        (ReviewStatus.REJECTED, NodeStatus.RETRACTED),
    ):
        store, report = _import([_entry(review_status=review_status)])
        claim = store.get_node(legacy_claim_id(ENTRY_ID))
        assert claim.status is node_status
        assert claim.attributes["legacy_review_status"] == review_status.value
        assert "claim.status:legacy_review_status" in report.inferred_fields
        assert "status" in claim.attributes["inferred_fields"]


def test_unambiguous_legacy_types_map_to_a_memory_class_marked_inferred():
    for legacy_type, memory_class in (
        (KnowledgeType.FACT, MemoryClass.FACT),
        (KnowledgeType.CONFIG, MemoryClass.FACT),
        (KnowledgeType.PITFALL, MemoryClass.EXPERIENCE),
        (KnowledgeType.PREFERENCE, MemoryClass.POLICY),
        (KnowledgeType.PROCEDURE, MemoryClass.PROCEDURE),
    ):
        store, report = _import([_entry(type=legacy_type)])
        claim = store.get_node(legacy_claim_id(ENTRY_ID))
        assert claim.memory_class is memory_class
        assert claim.attributes["legacy_type"] == legacy_type.value
        assert "claim.memory_class:legacy_type" in report.inferred_fields
        assert claim.attributes["inferred_fields"] == ["memory_class", "status"]


def test_ambiguous_legacy_types_leave_memory_class_unset_and_say_so():
    for legacy_type in (KnowledgeType.DECISION, KnowledgeType.RELATIONSHIP):
        store, report = _import([_entry(type=legacy_type)])
        claim = store.get_node(legacy_claim_id(ENTRY_ID))
        assert claim.memory_class is None
        assert claim.attributes["legacy_type"] == legacy_type.value
        assert f"unmapped_memory_class:{legacy_type.value}" in report.warnings
        assert "claim.memory_class:legacy_type" not in report.inferred_fields
        assert claim.attributes["inferred_fields"] == ["status"]


# --- F. tags: conservative, inferred, never a subject-matter claim -----------

def test_tags_become_entities_the_evidence_mentions_marked_inferred():
    store, report = _import([_entry(tags=["Retrieval-Index", "rebuild"])])
    entity_id = legacy_tag_entity_id("retrieval-index")
    entity = store.get_node(entity_id)

    assert entity is not None
    assert entity.kind is NodeKind.ENTITY
    assert entity.label == "retrieval-index"
    assert entity.attributes["legacy_tag"] == "retrieval-index"

    edges = store.get_edges(entity_id)
    assert [(e.relation_type, e.source_id, e.target_id) for e in edges] == [
        (RelationType.MENTIONS, legacy_evidence_id(ENTRY_ID), entity_id)
    ]
    assert edges[0].derivation.method is DerivationMethod.INFERRED
    assert "entity:legacy_tag" in report.inferred_fields
    assert "edge.MENTIONS:legacy_tag" in report.inferred_fields
    assert report.nodes_written == 4  # claim + evidence + two tag entities
    assert report.edges_written == 3  # EVIDENCED_BY + two MENTIONS


def test_a_tag_never_becomes_a_subject_matter_claim():
    store, _ = _import([_entry(tags=["retrieval-index"])])

    claim_edges = store.get_edges(legacy_claim_id(ENTRY_ID))
    assert RelationType.ABOUT not in {e.relation_type for e in claim_edges}
    assert [e.relation_type for e in claim_edges] == [RelationType.EVIDENCED_BY]


def test_a_tag_shared_by_two_entries_is_one_order_independent_entity():
    tagged = [
        _entry(tags=["retrieval-index"]),
        _other_entry(tags=["retrieval-index"]),
    ]
    forward, forward_report = _import(tagged)
    reverse, _ = _import(list(reversed(tagged)))

    assert forward.snapshot() == reverse.snapshot()
    entity = forward.get_node(legacy_tag_entity_id("retrieval-index"))
    # The earliest entry that carries the tag, so the timestamp cannot depend
    # on which record happened to be projected last.
    assert entity.recorded_at == T0
    assert forward_report.nodes_written == 5
    assert forward_report.edges_written == 4


# --- G. related_ids: only resolvable edges, everything else reported ---------

def test_related_ids_project_related_to_edges_only_for_present_targets():
    store, report = _import(
        [
            _entry(related_ids=[OTHER_ENTRY_ID, "entry_absent_forecast"]),
            _other_entry(),
        ]
    )
    claim_id = legacy_claim_id(ENTRY_ID)
    related = store.get_edges(claim_id, [RelationType.RELATED_TO])

    assert [(e.source_id, e.target_id) for e in related] == [
        (claim_id, legacy_claim_id(OTHER_ENTRY_ID))
    ]
    assert related[0].id == make_edge_id(
        RelationType.RELATED_TO, claim_id, legacy_claim_id(OTHER_ENTRY_ID)
    )
    assert related[0].is_weak() is True
    assert related[0].derivation.method is DerivationMethod.MIGRATED
    assert related[0].recorded_at == T0

    token = f"unresolved_related_id:{ENTRY_ID}:entry_absent_forecast"
    assert token in report.unresolved_references
    assert token in report.warnings
    assert report.ok is True


def test_a_missing_related_target_never_becomes_a_node():
    store, _ = _import([_entry(related_ids=["entry_absent_forecast"])])
    snapshot = store.snapshot()

    assert store.get_node(legacy_claim_id("entry_absent_forecast")) is None
    assert len(snapshot["nodes"]) == 2
    assert "entry_absent_forecast" not in store.snapshot().__str__()
    assert store.health().healthy is True


def test_a_self_related_id_is_reported_rather_than_projected():
    store, report = _import([_entry(related_ids=[ENTRY_ID])])

    assert store.get_edges(legacy_claim_id(ENTRY_ID), [RelationType.RELATED_TO]) == []
    assert f"self_related_id:{ENTRY_ID}" in report.warnings
    assert report.ok is True


# --- H. changelog events: input evidence, never a new truth store ------------

def _event(**kwargs) -> MemoryChangelogEntry:
    data = {
        "event_id": "event_0001",
        "at": T2,
        "surface": "l1_knowledge",
        "operation": "l1_section_updated",
        "domain": DOMAIN,
        "section": SECTION,
        "tool": "inject_knowledge",
        "source_ref": "session_0007",
        "before_hash": hash_text("The retrieval index rebuild runs weekly."),
        "after_hash": hash_text(BODY),
    }
    data.update(kwargs)
    return MemoryChangelogEntry(**data)


def test_a_matching_changelog_event_enriches_the_entry_it_belongs_to():
    store, report = _import([_entry(), _other_entry()], [_event()])

    assert report.ok is True
    assert report.changelog_events_read == 1
    assert report.skipped == 0

    claim = store.get_node(legacy_claim_id(ENTRY_ID))
    assert claim.attributes["legacy_changelog_events"] == [
        {
            "event_id": "event_0001",
            "operation": "l1_section_updated",
            "surface": "l1_knowledge",
            "observed_at": T2.isoformat(),
            "actor": "framework",
            "tool": "inject_knowledge",
            "source_ref": "session_0007",
            "before_hash": hash_text("The retrieval index rebuild runs weekly."),
            "after_hash": hash_text(BODY),
        }
    ]
    # The other entry in the same snapshot is untouched.
    other = store.get_node(legacy_claim_id(OTHER_ENTRY_ID))
    assert "legacy_changelog_events" not in other.attributes


def test_a_changelog_timestamp_never_becomes_the_facts_validity():
    store, _ = _import([_entry()], [_event()])
    claim = store.get_node(legacy_claim_id(ENTRY_ID))

    assert claim.valid_from is None
    assert claim.valid_to is None
    # recorded_at stays the legacy creation time; the event is observed
    # migration metadata, not a new "we learned this" moment.
    assert claim.recorded_at == T0


def test_changelog_enrichment_is_ordered_and_order_independent():
    events = [_event(), _event(event_id="event_0002", at=T1)]
    forward, forward_report = _import([_entry()], events)
    reverse, reverse_report = _import([_entry()], list(reversed(events)))

    assert forward.snapshot() == reverse.snapshot()
    assert forward_report.to_dict() == reverse_report.to_dict()
    claim = forward.get_node(legacy_claim_id(ENTRY_ID))
    assert [e["event_id"] for e in claim.attributes["legacy_changelog_events"]] == [
        "event_0002",  # T1
        "event_0001",  # T2
    ]


def test_an_unmatched_changelog_event_is_reported_and_creates_no_claim():
    store, report = _import(
        [_entry()],
        [_event(event_id="event_0009", domain="forecasting", section="Nightly job")],
    )

    assert "unmatched_changelog_event:event_0009" in report.warnings
    assert report.skipped == 1
    assert report.ok is True
    assert report.nodes_written == 2  # only the supplied entry's claim/evidence
    assert len(store.snapshot()["nodes"]) == 2


def test_a_domain_only_changelog_event_cannot_be_matched_to_one_entry():
    store, report = _import(
        [_entry()],
        [
            _event(
                event_id="event_0010",
                surface="l0_index",
                operation="l0_entry_added",
                section=None,
            )
        ],
    )

    assert "unmatched_changelog_event:event_0010" in report.warnings
    assert report.skipped == 1
    claim = store.get_node(legacy_claim_id(ENTRY_ID))
    assert "legacy_changelog_events" not in claim.attributes


def test_an_event_from_another_surface_never_enriches_an_l1_claim():
    """Surface is part of the match, not decoration.

    A same-``(domain, section)`` event recorded against a different surface is
    about that surface's own record, not this L1 section, so attaching it would
    put another layer's history on an L1 claim.
    """
    store, report = _import(
        [_entry()],
        [
            _event(
                event_id="event_0012",
                surface="l0_index",
                operation="l0_entry_updated",
            )
        ],
    )

    assert "unmatched_changelog_event:event_0012" in report.warnings
    assert report.skipped == 1
    assert report.ok is True
    claim = store.get_node(legacy_claim_id(ENTRY_ID))
    assert "legacy_changelog_events" not in claim.attributes
    assert report.nodes_written == 2  # only the supplied entry's claim/evidence


def test_an_ambiguous_changelog_event_enriches_nothing():
    twins = [_entry(), _entry(id=OTHER_ENTRY_ID)]  # same domain + section
    store, report = _import(twins, [_event(event_id="event_0011")])

    assert "ambiguous_changelog_event:event_0011" in report.warnings
    assert report.skipped == 1
    for entry_id in (ENTRY_ID, OTHER_ENTRY_ID):
        claim = store.get_node(legacy_claim_id(entry_id))
        assert "legacy_changelog_events" not in claim.attributes


def test_changelog_prose_never_reaches_the_graph_or_the_report():
    secret_ish = "rotate the shared staging credential before the rebuild"
    store, report = _import(
        [_entry()],
        [_event(summary=secret_ish, reason=secret_ish, metadata={"note": secret_ish})],
    )

    assert secret_ish not in str(store.snapshot())
    assert secret_ish not in report.to_json()


# --- I. duplicate and conflicting identities ---------------------------------

def test_identical_repeated_entries_are_idempotent_and_reported():
    once, _ = _import([_entry()])
    twice, report = _import([_entry(), _entry()])

    assert twice.snapshot() == once.snapshot()
    assert report.ok is True
    assert report.entries_read == 2
    assert report.nodes_written == 2
    assert report.edges_written == 1
    assert report.skipped == 1
    assert f"duplicate_entry:{ENTRY_ID}" in report.warnings


def test_conflicting_records_for_one_identity_are_refused_not_last_write_wins():
    conflicting = [_entry(), _entry(content="The rebuild runs hourly.")]
    forward, report = _import(conflicting)
    reverse, reverse_report = _import(list(reversed(conflicting)))

    assert report.ok is False
    assert f"conflicting_entry:{ENTRY_ID}" in report.errors
    assert report.entries_read == 2
    assert report.skipped == 2
    assert report.nodes_written == 0
    assert forward.get_node(legacy_claim_id(ENTRY_ID)) is None
    # Deterministic: neither record wins, in either input order.
    assert forward.snapshot() == reverse.snapshot()
    assert report.to_dict() == reverse_report.to_dict()


def test_a_refused_identity_is_not_available_as_a_relation_target():
    store, report = _import(
        [
            _entry(related_ids=[OTHER_ENTRY_ID]),
            _other_entry(),
            _other_entry(content="The rebuild window closes at 05:00."),
        ]
    )

    assert f"conflicting_entry:{OTHER_ENTRY_ID}" in report.errors
    token = f"unresolved_related_id:{ENTRY_ID}:{OTHER_ENTRY_ID}"
    assert token in report.unresolved_references
    assert store.get_edges(legacy_claim_id(ENTRY_ID), [RelationType.RELATED_TO]) == []


def test_duplicate_and_conflicting_changelog_events_are_reported():
    duplicated, duplicate_report = _import([_entry()], [_event(), _event()])
    claim = duplicated.get_node(legacy_claim_id(ENTRY_ID))

    assert "duplicate_changelog_event:event_0001" in duplicate_report.warnings
    assert duplicate_report.skipped == 1
    assert duplicate_report.ok is True
    assert len(claim.attributes["legacy_changelog_events"]) == 1

    _, conflict_report = _import(
        [_entry()], [_event(), _event(operation="l1_section_deleted")]
    )
    assert "conflicting_changelog_event:event_0001" in conflict_report.errors
    assert conflict_report.ok is False
    assert conflict_report.skipped == 2


# --- J. malformed and unsupported input --------------------------------------

def test_an_entry_with_a_blank_identity_is_reported_not_projected():
    store, report = _import([_entry(id="   ")])

    assert report.ok is False
    assert "invalid_entry_id:0" in report.errors
    assert report.skipped == 1
    assert report.nodes_written == 0
    assert store.snapshot() == InMemoryGraphStore().snapshot()


def test_unsupported_input_objects_are_reported_and_the_rest_still_imports():
    store, report = _import(
        [{"id": ENTRY_ID, "content": BODY}, _entry()],
        [{"event_id": "event_0001"}],
    )

    assert "unsupported_entry:0:dict" in report.errors
    assert "unsupported_changelog_event:0:dict" in report.errors
    assert report.ok is False
    assert report.skipped == 2
    # The well-formed entry is still projected: a bad record does not lose a
    # good one.
    assert store.get_node(legacy_claim_id(ENTRY_ID)) is not None
    assert report.nodes_written == 2


def test_a_store_that_refuses_a_write_is_reported_rather_than_raised():
    class _RefusingStore(InMemoryGraphStore):
        def append_edge(self, edge):
            raise RuntimeError("backend unavailable")

    store = _RefusingStore()
    report = LegacyGraphImporter(store).import_snapshot([_entry()])

    assert report.ok is False
    assert report.edges_written == 0
    assert report.nodes_written == 2
    assert any(e.startswith("edge_write_failed:") for e in report.errors)
    assert all("RuntimeError" in e for e in report.errors)


# --- K. the report itself ----------------------------------------------------

def test_the_report_is_serializable_and_dumps_no_entry_body():
    _, report = _import(
        [_entry(tags=["retrieval-index"], related_ids=["entry_absent_forecast"])],
        [_event()],
    )
    payload = json.loads(report.to_json())

    assert payload["entries_read"] == 1
    assert payload["unresolved_references"] == [
        f"unresolved_related_id:{ENTRY_ID}:entry_absent_forecast"
    ]
    assert BODY not in report.to_json()
    assert "index rebuild runs nightly" not in report.to_json()


def test_report_lists_are_sorted_and_deduplicated():
    _, report = _import(
        [
            _entry(
                type=KnowledgeType.DECISION,
                related_ids=["entry_absent_b", "entry_absent_a", "entry_absent_b"],
            ),
            _other_entry(type=KnowledgeType.DECISION),
        ]
    )

    for values in (
        report.warnings,
        report.errors,
        report.inferred_fields,
        report.unresolved_references,
    ):
        assert values == sorted(values)
        assert len(values) == len(set(values))
    assert report.unresolved_references == [
        f"unresolved_related_id:{ENTRY_ID}:entry_absent_a",
        f"unresolved_related_id:{ENTRY_ID}:entry_absent_b",
    ]
    # One warning for the two entries that share an unmappable legacy type.
    assert report.warnings.count("unmapped_memory_class:decision") == 1


# --- L. boundaries: no paths, no config, no store operations beyond writes ----

def test_the_importer_cannot_reach_a_path_or_a_configuration():
    source = inspect.getsource(graph_legacy_importer)

    for forbidden in (
        "import os",
        "import pathlib",
        "from pathlib",
        "from .config",
        "from .storage",
        "from .server",
        "open(",
        "Path(",
        "expanduser",
        "glob",
        "environ",
    ):
        assert forbidden not in source, forbidden
    for attribute in ("os", "Path", "pathlib", "open_store", "MemoryConfig"):
        assert not hasattr(graph_legacy_importer, attribute), attribute


def test_the_importer_only_needs_the_two_store_write_operations():
    class _WriteOnlyStore:
        """Implements nothing but the operations an import is allowed to use."""

        def __init__(self) -> None:
            self.nodes: list = []
            self.edges: list = []

        def create_node(self, node):
            self.nodes.append(node)
            return node

        def append_edge(self, edge):
            self.edges.append(edge)
            return edge

    store = _WriteOnlyStore()
    report = LegacyGraphImporter(store).import_snapshot([_entry()])

    assert report.ok is True
    assert len(store.nodes) == 2
    assert len(store.edges) == 1


def test_import_snapshot_does_not_mutate_its_inputs():
    entries = [
        _entry(tags=["retrieval-index"], related_ids=[OTHER_ENTRY_ID]),
        _other_entry(),
    ]
    events = [_event()]
    before = (
        [entry.model_dump(mode="json") for entry in entries],
        [event.to_dict() for event in events],
    )

    LegacyGraphImporter(InMemoryGraphStore()).import_snapshot(entries, events)

    assert (
        [entry.model_dump(mode="json") for entry in entries],
        [event.to_dict() for event in events],
    ) == before


def test_reimporting_an_edited_entry_updates_the_same_claim():
    """A rerun after a legacy edit is an update, not a second claim."""
    store = InMemoryGraphStore()
    importer = LegacyGraphImporter(store)

    importer.import_snapshot([_entry()])
    report = importer.import_snapshot(
        [_entry(content="The rebuild runs hourly.", version=4)]
    )

    claim = store.get_node(legacy_claim_id(ENTRY_ID))
    assert report.ok is True
    assert len(store.snapshot()["nodes"]) == 2
    assert len(store.snapshot()["edges"]) == 1
    assert claim.content == "The rebuild runs hourly."
    assert claim.version == 4
    assert claim.source_refs[0].quote_hash == hash_text("The rebuild runs hourly.")
    assert store.health().healthy is True


def test_a_padded_entry_id_is_one_normalized_identity():
    """``make_node_id`` trims its parts, so " x " and "x" are the same entry."""
    store, report = _import(
        [_entry(id=f"  {ENTRY_ID}  ", related_ids=[ENTRY_ID])]
    )

    assert report.ok is True
    assert store.get_node(legacy_claim_id(ENTRY_ID)) is not None
    assert (
        store.get_node(legacy_claim_id(ENTRY_ID)).attributes["legacy_entry_id"]
        == ENTRY_ID
    )
    assert f"self_related_id:{ENTRY_ID}" in report.warnings
    assert store.get_edges(legacy_claim_id(ENTRY_ID), [RelationType.RELATED_TO]) == []

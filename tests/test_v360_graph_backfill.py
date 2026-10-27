"""v3.6.0 Batch I-C — legacy identity backfill planner and its publication seam.

Why this file exists: ARCHITECTURE_v2.md §12 Batch I-C requires a *controlled
data-preparation* batch — a read-only inventory and dry-run planner over
explicitly supplied, isolated L1/vector/changelog snapshots, plus an apply phase
that may only touch graph seed metadata, only through the existing atomic vector
publication boundary, and only reversibly.

These tests pin:

  * **Total accounting.** Every supplied vector row gets exactly one stable
    reason code, and the counts sum to the number of rows read — so "all rows
    handled" is an arithmetic claim rather than a hope.
  * **Identity, never content.** The expected node ID is
    :func:`~layered_memory_mcp.graph_legacy_importer.legacy_claim_id` of the row
    identity and nothing else. No filename, heading, body, domain or fresh UUID
    ever produces one, and an unusable identity is refused by name.
  * **Nothing is coerced.** Malformed metadata, a disagreeing stored graph ID, an
    orphan row and an ambiguous L1 identity are quarantined in the report and
    never rewritten.
  * **The planner is pure.** It reads no file, opens no store and mutates none of
    its inputs; a dry run leaves the vector store byte-for-byte unchanged.
  * **Metadata-only, atomic, reversible publication.** Apply changes only the
    metadata column, refuses a stale snapshot, leaves the previous committed rows
    intact on any failure, is idempotent on rerun, is visible to search
    afterwards, and can be rolled back to the exact previous rows.
  * **Bounded reports** (§15). Counts, row identities, derived graph IDs and
    stable reason tokens only — never a body, a vector value, a host path or an
    exception message.

Isolation: every store here lives under pytest's ``tmp_path`` with a stubbed
embedder, and every L1/vector/changelog snapshot is a synthetic in-memory
object. Nothing reads a real memory path, a real config, real MCP state or any
user memory data, and no graph store is opened.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pytest

from layered_memory_mcp.graph_backfill import (
    BACKFILL_APPLIED,
    BACKFILL_DRY_RUN,
    BACKFILL_INVALID_INPUTS,
    BACKFILL_NOTHING_TO_DO,
    BACKFILL_PLAN_SNAPSHOT_MISMATCH,
    BACKFILL_STALE_SNAPSHOT,
    BACKFILL_UNRESOLVED_ROWS,
    ELIGIBLE_REASONS,
    QUARANTINE_REASONS,
    REASON_ALREADY_MAPPED,
    REASON_AMBIGUOUS_L1_IDENTITY,
    REASON_DUPLICATE_VECTOR_ROW,
    REASON_MALFORMED_METADATA,
    REASON_MISMATCHED_GRAPH_METADATA,
    REASON_MISSING_GRAPH_METADATA,
    REASON_ORPHAN_VECTOR_ROW,
    REASON_UNSUPPORTED_ROW,
    REASON_UNUSABLE_IDENTITY,
    VectorSnapshotRow,
    apply_graph_seed_backfill,
    backfill_payload,
    capture_rollback,
    plan_graph_seed_backfill,
    vector_snapshot_fingerprint,
)
from layered_memory_mcp.graph_legacy_importer import legacy_claim_id
from layered_memory_mcp.graph_runtime import (
    GRAPH_SEED_ID_KEY,
    GRAPH_SEED_IDS_KEY,
    build_seed_mapping,
)
from layered_memory_mcp.graph_seed_metadata import (
    SEED_IDENTITY_BLANK,
    SEED_IDENTITY_MULTILINE,
    SEED_IDENTITY_NOT_A_STRING,
)
from layered_memory_mcp.memory_changelog import MemoryChangelogEntry
from layered_memory_mcp.models import KnowledgeEntry, KnowledgeType
from layered_memory_mcp.storage import vector_store

#: Deliberately unmistakable fixture content: if any of it ever reaches a
#: report, a substring assertion catches it.
BODY = "NIGHTLY-REBUILD-BODY-TEXT that no report may ever carry."
SECTION = "SECTION-HEADING-FIXTURE"

T0 = datetime(2026, 2, 3, 8, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Synthetic fixtures — no file, no config, no real store
# ---------------------------------------------------------------------------

def _entry(entry_id: str = "entry-0001", **kwargs) -> KnowledgeEntry:
    data = {
        "id": entry_id,
        "domain": "retrieval",
        "section": SECTION,
        "content": BODY,
        "type": KnowledgeType.FACT,
        "confidence": 0.8,
        "tags": ["retrieval-index"],
        "created_at": T0,
        "updated_at": T0,
    }
    data.update(kwargs)
    return KnowledgeEntry(**data)


def _legacy_metadata(entry: KnowledgeEntry) -> dict:
    """What a pre-Batch-I-A row carries: the four display fields, no graph ID."""
    return {
        "type": entry.type.value,
        "confidence": entry.confidence,
        "tags": list(entry.tags),
        "section": entry.section,
    }


def _row(row_id="entry-0001", metadata=None, **kwargs) -> VectorSnapshotRow:
    """One synthetic vector row. ``metadata`` is stored exactly as supplied."""
    fields = {
        "id": row_id,
        "domain": "retrieval",
        "text": BODY,
        "vector": json.dumps([0.25] * 4),
        "metadata": metadata,
    }
    fields.update(kwargs)
    return VectorSnapshotRow(**fields)


def _legacy_row(entry: KnowledgeEntry, **extra) -> VectorSnapshotRow:
    metadata = _legacy_metadata(entry)
    metadata.update(extra)
    return _row(entry.id, json.dumps(metadata))


def _event(**kwargs) -> MemoryChangelogEntry:
    data = {
        "surface": "l1_knowledge",
        "operation": "upsert",
        "domain": "retrieval",
        "section": SECTION,
        "at": T0,
    }
    data.update(kwargs)
    return MemoryChangelogEntry(**data)


def _verdicts(plan) -> dict[str, str]:
    return {verdict.row_id: verdict.reason for verdict in plan.report.verdicts}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A vector store with a stubbed embedder — no model, no network."""
    monkeypatch.setattr(
        vector_store,
        "_embed_texts",
        lambda texts: np.ones((len(texts), vector_store.EMBED_DIM), dtype=np.float32)
        / np.sqrt(vector_store.EMBED_DIM),
    )
    opened = vector_store.VectorStore(tmp_path / "vectors" / "vectors.db")
    yield opened
    opened.close()


@pytest.fixture
def legacy_store(store, monkeypatch):
    """A store whose rows were written *before* graph seed metadata existed."""
    monkeypatch.setattr(vector_store, "entry_metadata", _legacy_metadata)
    return store


def _raw_rows(opened) -> list[tuple]:
    """Every column, including the one ``snapshot()`` does not select."""
    with sqlite3.connect(opened.db_path) as conn:
        return conn.execute(
            "SELECT id, domain, text, vector, metadata, created_at "
            "FROM vectors ORDER BY id"
        ).fetchall()


def _stored_metadata(opened, row_id: str) -> dict:
    return next(
        json.loads(row[4]) for row in _raw_rows(opened) if row[0] == row_id
    )


# ---------------------------------------------------------------------------
# A. Deterministic mapping — identity only
# ---------------------------------------------------------------------------

def test_the_expected_node_id_is_the_importers_legacy_claim_id():
    """Reused, not re-derived: the backfill and the importer cannot disagree."""
    entry = _entry()
    plan = plan_graph_seed_backfill([_legacy_row(entry)], [entry])

    update = plan.updates[0]
    assert update.row_id == entry.id
    assert update.graph_node_id == legacy_claim_id(entry.id)
    assert plan.report.verdicts[0].expected_graph_node_id == legacy_claim_id(entry.id)


def test_the_expected_node_id_depends_on_nothing_but_the_identity():
    """A different body, heading, domain, tag set or type maps to the same node."""
    original = _entry()
    edited = _entry(
        content="a completely different body",
        section="Another heading",
        domain="retrieval-v2",
        tags=["unrelated"],
        type=KnowledgeType.PREFERENCE,
        confidence=0.1,
    )
    first = plan_graph_seed_backfill([_row("entry-0001", None)], [original])
    second = plan_graph_seed_backfill([_row("entry-0001", None)], [edited])

    assert first.updates[0].graph_node_id == second.updates[0].graph_node_id


def test_a_padded_row_identity_maps_to_the_same_node_id():
    entry = _entry()
    plan = plan_graph_seed_backfill([_row("  entry-0001  ", None)], [entry])

    assert plan.updates[0].graph_node_id == legacy_claim_id("entry-0001")
    # The update is keyed by the row's own ID, not by the normalized identity:
    # that is the key the store must be asked to patch.
    assert plan.updates[0].row_id == "  entry-0001  "


def test_two_runs_over_one_snapshot_produce_the_same_report():
    entry = _entry()
    rows = [_legacy_row(entry), _row("entry-0002", None)]
    first = plan_graph_seed_backfill(rows, [entry])
    second = plan_graph_seed_backfill(rows, [entry])

    assert first.report.to_json() == second.report.to_json()


# ---------------------------------------------------------------------------
# B. Complete row accounting
# ---------------------------------------------------------------------------

def test_every_row_is_classified_exactly_once_and_the_counts_sum():
    mapped = _entry("entry-mapped")
    missing = _entry("entry-missing")
    mismatched = _entry("entry-mismatched")
    malformed = _entry("entry-malformed")
    duplicated = _entry("entry-duplicated")
    ambiguous = _entry("entry-ambiguous")
    ambiguous_other = _entry("entry-ambiguous", content="a conflicting body")

    rows = [
        _legacy_row(mapped, **{GRAPH_SEED_ID_KEY: legacy_claim_id(mapped.id)}),
        _legacy_row(missing),
        _legacy_row(mismatched, **{GRAPH_SEED_ID_KEY: legacy_claim_id("someone-else")}),
        _row(malformed.id, "not json at all"),
        _legacy_row(duplicated),
        _legacy_row(duplicated),
        _legacy_row(ambiguous),
        _row("entry-orphan", None),
        _row("   ", None),
        "not a row at all",
    ]
    plan = plan_graph_seed_backfill(
        rows, [mapped, missing, mismatched, malformed, duplicated,
               ambiguous, ambiguous_other]
    )
    report = plan.report

    assert report.rows_read == len(rows)
    assert len(report.verdicts) == len(rows)
    assert report.already_mapped == 1
    assert report.planned_updates == 1
    # mismatched + malformed + two duplicates + the object that is not a row
    assert report.quarantined == 5
    assert report.unusable_identity == 1
    assert report.orphan_rows == 1
    assert report.ambiguous_identity == 1
    # Resolved to exactly one L1 entry: mapped, missing, mismatched, malformed.
    assert report.eligible == 4
    assert report.counts_balance
    assert report.accounted_rows == report.rows_read
    assert sum(report.counts_by_reason.values()) == report.rows_read


def test_the_reason_code_of_every_classified_row_is_the_documented_one():
    mapped = _entry("entry-mapped")
    missing = _entry("entry-missing")
    mismatched = _entry("entry-mismatched")
    malformed = _entry("entry-malformed")
    ambiguous = _entry("entry-ambiguous")

    rows = [
        _legacy_row(mapped, **{GRAPH_SEED_ID_KEY: legacy_claim_id(mapped.id)}),
        _legacy_row(missing),
        _legacy_row(mismatched, **{GRAPH_SEED_ID_KEY: "claim:not-this-one"}),
        _row(malformed.id, "["),
        _legacy_row(ambiguous),
        _row("entry-orphan", None),
        _row(None, None),
    ]
    plan = plan_graph_seed_backfill(
        rows,
        [mapped, missing, mismatched, malformed, ambiguous,
         ambiguous.model_copy(deep=True)],
    )

    reasons = _verdicts(plan)
    assert reasons["entry-mapped"] == REASON_ALREADY_MAPPED
    assert reasons["entry-missing"] == REASON_MISSING_GRAPH_METADATA
    assert reasons["entry-mismatched"] == REASON_MISMATCHED_GRAPH_METADATA
    assert reasons["entry-malformed"] == REASON_MALFORMED_METADATA
    assert reasons["entry-ambiguous"] == REASON_AMBIGUOUS_L1_IDENTITY
    assert reasons["entry-orphan"] == REASON_ORPHAN_VECTOR_ROW
    assert reasons[""] == REASON_UNUSABLE_IDENTITY


def test_only_missing_metadata_rows_become_planned_updates():
    mapped = _entry("entry-mapped")
    missing = _entry("entry-missing")
    mismatched = _entry("entry-mismatched")
    rows = [
        _legacy_row(mapped, **{GRAPH_SEED_ID_KEY: legacy_claim_id(mapped.id)}),
        _legacy_row(missing),
        _legacy_row(mismatched, **{GRAPH_SEED_ID_KEY: "claim:not-this-one"}),
    ]
    plan = plan_graph_seed_backfill(rows, [mapped, missing, mismatched])

    assert [update.row_id for update in plan.updates] == ["entry-missing"]


def test_a_planned_update_preserves_every_unrelated_metadata_field():
    entry = _entry()
    plan = plan_graph_seed_backfill(
        [_legacy_row(entry, extra_legacy_field="keep me")], [entry]
    )

    metadata = plan.updates[0].metadata
    assert metadata[GRAPH_SEED_ID_KEY] == legacy_claim_id(entry.id)
    assert metadata["extra_legacy_field"] == "keep me"
    assert metadata["section"] == entry.section
    assert metadata["tags"] == entry.tags


def test_a_row_with_no_metadata_at_all_is_a_planned_update():
    entry = _entry()
    plan = plan_graph_seed_backfill([_row(entry.id, None)], [entry])

    assert plan.updates[0].metadata == {GRAPH_SEED_ID_KEY: legacy_claim_id(entry.id)}


@pytest.mark.parametrize(
    "bad_id,reason_detail",
    [
        ("", SEED_IDENTITY_BLANK),
        ("   ", SEED_IDENTITY_BLANK),
        ("entry\n0001", SEED_IDENTITY_MULTILINE),
        ("entry\r0001", SEED_IDENTITY_MULTILINE),
        (None, SEED_IDENTITY_NOT_A_STRING),
        (7, SEED_IDENTITY_NOT_A_STRING),
        (True, SEED_IDENTITY_NOT_A_STRING),
    ],
)
def test_an_unusable_row_identity_is_named_never_repaired(bad_id, reason_detail):
    entry = _entry()
    plan = plan_graph_seed_backfill([_row(bad_id, None)], [entry])

    verdict = plan.report.verdicts[0]
    assert verdict.reason == REASON_UNUSABLE_IDENTITY
    assert verdict.details == [reason_detail]
    assert verdict.expected_graph_node_id == ""
    assert plan.updates == []


def test_an_orphan_row_is_reported_and_never_mapped():
    plan = plan_graph_seed_backfill([_row("entry-orphan", None)], [_entry()])

    assert plan.report.orphan_rows == 1
    assert plan.report.verdicts[0].expected_graph_node_id == ""
    assert plan.updates == []


@pytest.mark.parametrize("identical", [True, False])
def test_a_duplicated_l1_identity_is_ambiguous_and_nothing_is_chosen(identical):
    entry = _entry()
    other = entry.model_copy(deep=True) if identical else _entry(content="different")
    plan = plan_graph_seed_backfill([_legacy_row(entry)], [entry, other])

    assert plan.report.ambiguous_identity == 1
    assert plan.report.verdicts[0].reason == REASON_AMBIGUOUS_L1_IDENTITY
    assert plan.updates == []


def test_two_vector_rows_sharing_one_identity_are_both_quarantined():
    entry = _entry()
    plan = plan_graph_seed_backfill(
        [_legacy_row(entry), _row("  entry-0001 ", None)], [entry]
    )

    assert {verdict.reason for verdict in plan.report.verdicts} == {
        REASON_DUPLICATE_VECTOR_ROW
    }
    assert plan.updates == []


def test_a_row_that_is_not_a_row_is_reported_not_guessed():
    plan = plan_graph_seed_backfill([object()], [_entry()])

    verdict = plan.report.verdicts[0]
    assert verdict.reason == REASON_UNSUPPORTED_ROW
    assert verdict.row_id == ""
    assert plan.report.errors == ["unsupported_vector_row:0:object"]
    assert not plan.report.ok


def test_the_reason_buckets_are_the_documented_partition():
    assert ELIGIBLE_REASONS == frozenset(
        {
            REASON_ALREADY_MAPPED,
            REASON_MISSING_GRAPH_METADATA,
            REASON_MISMATCHED_GRAPH_METADATA,
            REASON_MALFORMED_METADATA,
        }
    )
    assert QUARANTINE_REASONS == frozenset(
        {
            REASON_MISMATCHED_GRAPH_METADATA,
            REASON_MALFORMED_METADATA,
            REASON_DUPLICATE_VECTOR_ROW,
            REASON_UNSUPPORTED_ROW,
        }
    )


# ---------------------------------------------------------------------------
# C. Malformed metadata and graph ID shapes are never coerced
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "metadata,detail",
    [
        ("not json at all", "metadata:not_json"),
        ("[1, 2, 3]", "metadata:not_a_mapping"),
        ("null", "metadata:not_a_mapping"),
        ("17", "metadata:not_a_mapping"),
        (json.dumps({GRAPH_SEED_ID_KEY: 7}), f"{GRAPH_SEED_ID_KEY}:not_a_string"),
        (json.dumps({GRAPH_SEED_ID_KEY: True}), f"{GRAPH_SEED_ID_KEY}:not_a_string"),
        (json.dumps({GRAPH_SEED_ID_KEY: None}), f"{GRAPH_SEED_ID_KEY}:not_a_string"),
        (json.dumps({GRAPH_SEED_ID_KEY: "   "}), f"{GRAPH_SEED_ID_KEY}:blank"),
        (json.dumps({GRAPH_SEED_ID_KEY: "a\nb"}), f"{GRAPH_SEED_ID_KEY}:multiline"),
        (json.dumps({GRAPH_SEED_IDS_KEY: "claim:bare-string"}),
         f"{GRAPH_SEED_IDS_KEY}:not_a_list"),
        (json.dumps({GRAPH_SEED_IDS_KEY: [1]}),
         f"{GRAPH_SEED_IDS_KEY}:element_not_a_string"),
        (json.dumps({GRAPH_SEED_IDS_KEY: [None]}),
         f"{GRAPH_SEED_IDS_KEY}:element_not_a_string"),
        (json.dumps({GRAPH_SEED_IDS_KEY: ["  "]}), f"{GRAPH_SEED_IDS_KEY}:element_blank"),
    ],
)
def test_malformed_metadata_is_visible_and_never_coerced(metadata, detail):
    entry = _entry()
    plan = plan_graph_seed_backfill([_row(entry.id, metadata)], [entry])

    verdict = plan.report.verdicts[0]
    assert verdict.reason == REASON_MALFORMED_METADATA
    assert detail in verdict.details
    assert plan.updates == []
    # Still eligible: the identity resolved. Quarantined all the same.
    assert plan.report.eligible == 1
    assert plan.report.quarantined == 1


def test_a_padded_stored_graph_id_is_malformed_rather_than_already_mapped():
    entry = _entry()
    padded = f"  {legacy_claim_id(entry.id)}  "
    plan = plan_graph_seed_backfill(
        [_row(entry.id, json.dumps({GRAPH_SEED_ID_KEY: padded}))], [entry]
    )

    verdict = plan.report.verdicts[0]
    assert verdict.reason == REASON_MALFORMED_METADATA
    assert f"{GRAPH_SEED_ID_KEY}:padded" in verdict.details
    assert plan.updates == []


def test_the_list_key_holding_exactly_the_expected_id_is_already_mapped():
    entry = _entry()
    metadata = {GRAPH_SEED_IDS_KEY: [legacy_claim_id(entry.id)]}
    plan = plan_graph_seed_backfill([_row(entry.id, json.dumps(metadata))], [entry])

    assert plan.report.verdicts[0].reason == REASON_ALREADY_MAPPED
    assert plan.updates == []


def test_the_list_key_naming_another_node_is_a_mismatch():
    entry = _entry()
    metadata = {
        GRAPH_SEED_IDS_KEY: [legacy_claim_id(entry.id), "claim:somebody-else"]
    }
    plan = plan_graph_seed_backfill([_row(entry.id, json.dumps(metadata))], [entry])

    assert plan.report.verdicts[0].reason == REASON_MISMATCHED_GRAPH_METADATA
    assert plan.updates == []


def test_a_mismatch_report_never_echoes_the_value_it_disagreed_with():
    entry = _entry()
    stored = "claim:SOMEBODY-ELSES-NODE-ID"
    plan = plan_graph_seed_backfill(
        [_row(entry.id, json.dumps({GRAPH_SEED_ID_KEY: stored}))], [entry]
    )

    verdict = plan.report.verdicts[0]
    assert verdict.reason == REASON_MISMATCHED_GRAPH_METADATA
    assert verdict.expected_graph_node_id == legacy_claim_id(entry.id)
    assert stored not in plan.report.to_json()


# ---------------------------------------------------------------------------
# D. The planner is pure
# ---------------------------------------------------------------------------

def test_the_planner_mutates_none_of_its_inputs():
    entry = _entry()
    rows = [_legacy_row(entry), _row("entry-orphan", {"section": SECTION})]
    events = [_event()]
    entries = [entry]
    before = copy.deepcopy((rows, entries, events))

    plan_graph_seed_backfill(rows, entries, events)

    assert copy.deepcopy((rows, entries, events)) == before


def test_a_planned_metadata_dict_is_a_new_dict_not_the_rows_own():
    entry = _entry()
    source = {"section": SECTION, "tags": ["retrieval-index"]}
    plan = plan_graph_seed_backfill([_row(entry.id, source)], [entry])

    plan.updates[0].metadata["section"] = "mutated"
    assert source["section"] == SECTION


def test_the_planner_opens_no_database(monkeypatch):
    def _refuse(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the planner must not open a database")

    monkeypatch.setattr(sqlite3, "connect", _refuse)
    entry = _entry()

    plan = plan_graph_seed_backfill([_legacy_row(entry)], [entry], [_event()])
    assert plan.report.rows_read == 1


# ---------------------------------------------------------------------------
# E. The changelog is counted, never a source of identity
# ---------------------------------------------------------------------------

def test_changelog_events_are_counted_but_manufacture_no_entry():
    """A changelog event is provenance, not an L1 record (§4).

    The planner needs no changelog *content*: vector identity comes from the row
    ID alone. Events are counted and type-checked so a caller can prove which
    snapshot was inspected; graph projection stays with the explicit
    ``LegacyGraphImporter`` caller.
    """
    plan = plan_graph_seed_backfill(
        [_row("entry-only-in-changelog", None)], [], [_event(), _event()]
    )

    assert plan.report.changelog_events_read == 2
    assert plan.report.entries_read == 0
    # The event named this domain/section, and it still did not become an entry.
    assert plan.report.verdicts[0].reason == REASON_ORPHAN_VECTOR_ROW
    assert plan.updates == []


def test_an_unsupported_changelog_event_is_reported_by_type_only():
    plan = plan_graph_seed_backfill([], [], ["not an event"])

    assert plan.report.errors == ["unsupported_changelog_event:0:str"]
    assert plan.report.changelog_events_read == 1


def test_l1_entries_without_a_vector_row_are_reported_as_coverage():
    covered = _entry("entry-covered")
    uncovered = _entry("entry-uncovered")
    plan = plan_graph_seed_backfill([_legacy_row(covered)], [covered, uncovered])

    assert plan.report.entries_without_vector_rows == 1
    assert "l1_entries_without_vector_rows:1" in plan.report.warnings


def test_an_unsupported_or_unusable_l1_entry_is_reported_by_position():
    entry = _entry()
    plan = plan_graph_seed_backfill(
        [_legacy_row(entry)], [entry, "not an entry", _entry("  ")]
    )

    assert plan.report.errors == [
        "invalid_l1_entry_id:2",
        "unsupported_l1_entry:1:str",
    ]
    assert plan.report.entries_read == 3
    assert plan.report.entries_indexed == 1


# ---------------------------------------------------------------------------
# F. Fingerprints and report privacy
# ---------------------------------------------------------------------------

def test_the_fingerprint_is_stable_and_changes_with_any_row_change():
    entry = _entry()
    rows = [_legacy_row(entry)]
    first = vector_snapshot_fingerprint(rows)

    assert first == vector_snapshot_fingerprint(list(rows))
    assert first != vector_snapshot_fingerprint([_row(entry.id, None)])
    assert first != vector_snapshot_fingerprint([])


def test_the_fingerprint_carries_no_content():
    entry = _entry()
    fingerprint = vector_snapshot_fingerprint([_legacy_row(entry)])

    assert BODY not in fingerprint
    assert SECTION not in fingerprint
    assert fingerprint == plan_graph_seed_backfill(
        [_legacy_row(entry)], [entry]
    ).report.snapshot_fingerprint


def test_the_report_carries_no_body_vector_path_or_exception_text(tmp_path):
    entry = _entry()
    rows = [
        _legacy_row(entry),
        _row("entry-orphan", f"not json: {tmp_path}"),
        _row(None, None),
    ]
    serialized = plan_graph_seed_backfill(rows, [entry], [_event()]).report.to_json()

    assert BODY not in serialized
    assert SECTION not in serialized
    assert "0.25" not in serialized
    assert str(tmp_path) not in serialized
    assert "Expecting value" not in serialized  # no JSONDecodeError message


# ---------------------------------------------------------------------------
# G. Dry run touches nothing
# ---------------------------------------------------------------------------

def test_a_dry_run_leaves_the_store_byte_for_byte_unchanged(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before_rows = _raw_rows(legacy_store)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])

    application = apply_graph_seed_backfill(
        legacy_store, plan, expected=expected, dry_run=True
    )

    assert application.result.status == BACKFILL_DRY_RUN
    assert application.result.dry_run is True
    assert application.result.planned_updates == 1
    assert application.result.rows_changed == 0
    assert _raw_rows(legacy_store) == before_rows
    assert legacy_store.snapshot() == expected


def test_a_dry_run_names_unresolved_rows_without_refusing_to_report(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [])  # orphan row

    application = apply_graph_seed_backfill(
        legacy_store, plan, expected=expected, dry_run=True
    )

    assert application.result.status == BACKFILL_DRY_RUN
    assert "backfill_unresolved_rows:1" in application.result.warnings
    assert legacy_store.snapshot() == expected


# ---------------------------------------------------------------------------
# H. Metadata-only apply
# ---------------------------------------------------------------------------

def test_apply_adds_only_the_graph_node_id(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before = _raw_rows(legacy_store)[0]
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])

    application = apply_graph_seed_backfill(legacy_store, plan, expected=expected)

    assert application.result.status == BACKFILL_APPLIED
    assert application.result.rows_changed == 1
    after = _raw_rows(legacy_store)[0]
    # id, domain, text, vector and created_at are untouched.
    assert after[:4] == before[:4]
    assert after[5] == before[5]
    assert json.loads(after[4]) == {
        **json.loads(before[4]),
        GRAPH_SEED_ID_KEY: legacy_claim_id(entry.id),
    }


def test_apply_preserves_unrelated_metadata_keys(legacy_store, monkeypatch):
    entry = _entry()
    monkeypatch.setattr(
        vector_store,
        "entry_metadata",
        lambda e: {**_legacy_metadata(e), "legacy_only_field": "keep me"},
    )
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])

    apply_graph_seed_backfill(legacy_store, plan, expected=expected)

    metadata = _stored_metadata(legacy_store, entry.id)
    assert metadata["legacy_only_field"] == "keep me"
    assert metadata["section"] == entry.section
    assert metadata[GRAPH_SEED_ID_KEY] == legacy_claim_id(entry.id)


def test_search_sees_the_new_metadata_after_the_commit(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before = legacy_store.search("rebuild", top_n=5)
    assert GRAPH_SEED_ID_KEY not in before[0]["metadata"]

    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])
    apply_graph_seed_backfill(legacy_store, plan, expected=expected)

    after = legacy_store.search("rebuild", top_n=5)
    assert after[0]["metadata"][GRAPH_SEED_ID_KEY] == legacy_claim_id(entry.id)
    mapping = build_seed_mapping(
        [{"file": row["id"], "metadata": row["metadata"]} for row in after]
    )
    assert mapping.seed_node_ids_by_result == {entry.id: [legacy_claim_id(entry.id)]}


def test_a_rerun_is_idempotent_and_reports_zero_further_changes(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    apply_graph_seed_backfill(
        legacy_store, plan_graph_seed_backfill(expected, [entry]), expected=expected
    )
    after_first = legacy_store.snapshot()

    replan = plan_graph_seed_backfill(after_first, [entry])
    second = apply_graph_seed_backfill(legacy_store, replan, expected=after_first)

    assert replan.report.already_mapped == 1
    assert replan.report.planned_updates == 0
    assert second.result.status == BACKFILL_NOTHING_TO_DO
    assert second.result.rows_changed == 0
    assert legacy_store.snapshot() == after_first


def test_apply_refuses_a_plan_with_unresolved_rows_until_told_otherwise(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    legacy_store.add(_entry("entry-orphan", section="Another heading"))
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])

    refused = apply_graph_seed_backfill(legacy_store, plan, expected=expected)
    assert refused.result.status == BACKFILL_UNRESOLVED_ROWS
    assert refused.result.rows_changed == 0
    assert legacy_store.snapshot() == expected

    allowed = apply_graph_seed_backfill(
        legacy_store, plan, expected=expected, require_clean=False
    )
    assert allowed.result.status == BACKFILL_APPLIED
    assert allowed.result.rows_changed == 1


def _forbid_publication(monkeypatch, opened) -> list:
    """Replace the publication boundary with a recorder that writes nothing.

    It records *and* raises: ``apply`` reports a failed publication rather than
    propagating, so the returned list is what makes "``patch_metadata`` was never
    reached" an assertion instead of an inference from the status.
    """
    attempts: list = []

    def _refuse(updates, **kwargs):
        attempts.append(sorted(dict(updates)))
        raise AssertionError("patch_metadata must not be reached")

    monkeypatch.setattr(opened, "patch_metadata", _refuse)
    return attempts


#: A valid, publishable row paired with one input the planner refused to read.
#: Both leave ``unresolved_rows`` at zero — the row itself is perfectly clean —
#: so a coordinator that only consulted the coverage counts would publish a plan
#: built from an inventory that does not describe the snapshot trio it was given.
_REJECTED_INPUTS = [
    pytest.param(["not an entry"], (), "unsupported_l1_entry:1:str", id="l1_entry"),
    pytest.param(
        [], ["not an event"], "unsupported_changelog_event:0:str", id="changelog"
    ),
]


@pytest.mark.parametrize("require_clean", [True, False])
@pytest.mark.parametrize("extra_entries,events,error_token", _REJECTED_INPUTS)
def test_apply_refuses_a_plan_whose_inputs_the_planner_rejected(
    legacy_store, monkeypatch, require_clean, extra_entries, events, error_token
):
    """``report.errors`` is a refusal, not a decoration (§12 Batch I-C).

    An object that is not an L1 entry contributes no identity, so the rows
    around it can plan perfectly cleanly — ``unresolved_rows`` stays zero — while
    the inventory as a whole is *not* the one the caller asked for. Publishing it
    would write graph identities derived from a snapshot trio this module already
    said it could not read, and ``require_clean=False`` overrides row coverage
    only, so it must not reopen that door either.
    """
    entry = _entry()
    legacy_store.add(entry)
    before_rows = _raw_rows(legacy_store)
    expected = legacy_store.snapshot()

    plan = plan_graph_seed_backfill(expected, [entry, *extra_entries], events)
    # The precondition that makes this a real hole: a publishable update and a
    # reported input error, with nothing in the coverage counts to object to.
    assert plan.report.errors == [error_token]
    assert plan.report.planned_updates == 1
    assert plan.report.unresolved_rows == 0
    assert not plan.report.ok

    attempts = _forbid_publication(monkeypatch, legacy_store)
    application = apply_graph_seed_backfill(
        legacy_store, plan, expected=expected, require_clean=require_clean
    )

    assert attempts == []
    assert application.result.status == BACKFILL_INVALID_INPUTS
    assert application.result.errors == [error_token]
    assert application.result.rows_changed == 0
    assert not application.result.ok
    assert not application.result.applied
    assert _raw_rows(legacy_store) == before_rows
    assert legacy_store.snapshot() == expected
    # The snapshot is still the current one, so the handle stays valid — the
    # refusal is about the plan, not about another writer having moved on.
    assert application.rollback.row_ids == [entry.id]


def test_the_invalid_input_refusal_names_positions_and_types_only(
    legacy_store, monkeypatch, tmp_path
):
    entry = _entry()
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry, BODY], [SECTION])

    attempts = _forbid_publication(monkeypatch, legacy_store)
    result = apply_graph_seed_backfill(legacy_store, plan, expected=expected).result

    assert attempts == []
    assert result.status == BACKFILL_INVALID_INPUTS
    assert result.errors == [
        "unsupported_changelog_event:0:str",
        "unsupported_l1_entry:1:str",
    ]
    serialized = result.to_json()
    assert BODY not in serialized
    assert SECTION not in serialized
    assert str(tmp_path) not in serialized


def test_a_dry_run_still_reports_a_plan_with_rejected_inputs(legacy_store):
    """Refusing to publish is not refusing to inventory.

    ``dry_run`` writes nothing by construction, so it keeps reporting; the input
    errors stay visible in the plan's own report either way.
    """
    entry = _entry()
    legacy_store.add(entry)
    before_rows = _raw_rows(legacy_store)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry, "not an entry"])

    application = apply_graph_seed_backfill(
        legacy_store, plan, expected=expected, dry_run=True
    )

    assert application.result.status == BACKFILL_DRY_RUN
    assert application.result.rows_changed == 0
    assert backfill_payload(plan, application.result)["errors"] == [
        "unsupported_l1_entry:1:str"
    ]
    assert _raw_rows(legacy_store) == before_rows


def test_a_stale_snapshot_still_outranks_a_rejected_input(legacy_store):
    """The snapshot refusals stay most-fundamental: the caller must replan."""
    entry = _entry()
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry, "not an entry"])

    legacy_store.add(_entry("entry-later", section="Later heading"))
    current = _raw_rows(legacy_store)

    application = apply_graph_seed_backfill(legacy_store, plan, expected=expected)

    assert application.result.status == BACKFILL_STALE_SNAPSHOT
    assert application.rollback is None
    assert _raw_rows(legacy_store) == current


# ---------------------------------------------------------------------------
# I. Stale snapshots and failure atomicity
# ---------------------------------------------------------------------------

def test_a_stale_expected_snapshot_is_refused_and_changes_nothing(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])

    # Another writer lands between planning and publication.
    legacy_store.add(_entry("entry-later", section="Later heading"))
    current = _raw_rows(legacy_store)

    application = apply_graph_seed_backfill(legacy_store, plan, expected=expected)

    assert application.result.status == BACKFILL_STALE_SNAPSHOT
    assert application.result.rows_changed == 0
    assert _raw_rows(legacy_store) == current
    # Deliberately no rollback handle: restoring a snapshot another writer has
    # already moved past would be a clobber, not an undo.
    assert application.rollback is None


def test_the_store_itself_refuses_a_stale_snapshot_with_the_existing_error(
    legacy_store,
):
    entry = _entry()
    legacy_store.add(entry)
    stale = legacy_store.snapshot()
    legacy_store.add(_entry("entry-later", section="Later heading"))
    current = _raw_rows(legacy_store)

    with pytest.raises(RuntimeError, match="retry required"):
        legacy_store.patch_metadata({entry.id: {"a": 1}}, expected=stale)

    assert _raw_rows(legacy_store) == current


def test_a_plan_built_from_other_rows_is_refused_before_publication(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill([_row("entry-elsewhere", None)], [_entry("entry-elsewhere")])

    application = apply_graph_seed_backfill(legacy_store, plan, expected=expected)

    assert application.result.status == BACKFILL_PLAN_SNAPSHOT_MISMATCH
    assert application.result.rows_changed == 0
    assert legacy_store.snapshot() == expected


def test_an_unknown_row_id_leaves_every_committed_row_intact(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before = _raw_rows(legacy_store)
    expected = legacy_store.snapshot()

    with pytest.raises(KeyError):
        legacy_store.patch_metadata(
            {entry.id: {"a": 1}, "no-such-row": {"b": 2}}, expected=expected
        )

    assert _raw_rows(legacy_store) == before


def test_a_duplicate_update_payload_is_rejected(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before = _raw_rows(legacy_store)

    with pytest.raises(ValueError, match="duplicate"):
        legacy_store.patch_metadata(
            [(entry.id, {"a": 1}), (entry.id, {"a": 2})],
            expected=legacy_store.snapshot(),
        )

    assert _raw_rows(legacy_store) == before


def test_a_rejecting_source_validator_leaves_the_rows_intact(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before = _raw_rows(legacy_store)

    def _refuse():
        raise RuntimeError("source changed")

    with pytest.raises(RuntimeError):
        legacy_store.patch_metadata(
            {entry.id: {"a": 1}},
            expected=legacy_store.snapshot(),
            validate_source=_refuse,
        )

    assert _raw_rows(legacy_store) == before


def test_a_no_op_update_rewrites_nothing_and_reports_zero(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before = _raw_rows(legacy_store)
    stored = json.loads(before[0][4])

    changed = legacy_store.patch_metadata(
        {entry.id: stored}, expected=legacy_store.snapshot()
    )

    assert changed == 0
    assert _raw_rows(legacy_store) == before


def test_patch_metadata_touches_only_the_metadata_column(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before = _raw_rows(legacy_store)[0]

    changed = legacy_store.patch_metadata(
        {entry.id: {"section": entry.section, "graph_node_id": "claim:explicit"}},
        expected=legacy_store.snapshot(),
    )

    after = _raw_rows(legacy_store)[0]
    assert changed == 1
    assert after[:4] == before[:4]
    assert after[5] == before[5]
    assert json.loads(after[4])["graph_node_id"] == "claim:explicit"


# ---------------------------------------------------------------------------
# J. Rollback
# ---------------------------------------------------------------------------

def test_a_successful_apply_is_reversible_to_the_exact_previous_rows(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    before_rows = _raw_rows(legacy_store)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])

    application = apply_graph_seed_backfill(legacy_store, plan, expected=expected)
    assert application.result.status == BACKFILL_APPLIED
    assert legacy_store.snapshot() != expected

    restored = application.rollback.restore(legacy_store)

    assert restored == 1
    assert legacy_store.snapshot() == expected
    assert _raw_rows(legacy_store) == before_rows


def test_a_rollback_restores_a_row_that_had_no_metadata_to_having_none(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    legacy_store.patch_metadata({entry.id: None}, expected=legacy_store.snapshot())
    before_rows = _raw_rows(legacy_store)
    assert before_rows[0][4] is None

    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])
    application = apply_graph_seed_backfill(legacy_store, plan, expected=expected)
    assert application.result.rows_changed == 1

    application.rollback.restore(legacy_store)
    assert _raw_rows(legacy_store) == before_rows


def test_the_rollback_handle_names_only_the_rows_it_would_restore(legacy_store):
    entry = _entry()
    legacy_store.add(entry)
    legacy_store.add(_entry("entry-second", section="Second heading"))
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])

    rollback = capture_rollback(plan, expected)

    assert rollback.row_ids == [entry.id]
    assert rollback.fingerprint == vector_snapshot_fingerprint(expected)


def test_apply_refuses_a_store_that_cannot_publish_metadata():
    plan = plan_graph_seed_backfill([], [])

    with pytest.raises(ValueError, match="snapshot"):
        apply_graph_seed_backfill(object(), plan, expected=[])

    class _SnapshotOnly:
        def snapshot(self):  # pragma: no cover - never reached
            return []

    with pytest.raises(ValueError, match="patch_metadata"):
        apply_graph_seed_backfill(_SnapshotOnly(), plan, expected=[])


def test_the_payload_is_bounded_and_carries_the_publication_beside_the_plan(
    legacy_store, tmp_path
):
    entry = _entry()
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])
    application = apply_graph_seed_backfill(legacy_store, plan, expected=expected)

    payload = backfill_payload(plan, application.result)

    assert payload["rows_read"] == 1
    assert payload["planned_updates"] == 1
    assert payload["counts_balance"] is True
    assert payload["verdicts"][0]["reason"] == REASON_MISSING_GRAPH_METADATA
    assert payload["publication"]["status"] == BACKFILL_APPLIED
    assert payload["publication"]["rows_changed"] == 1
    serialized = json.dumps(payload)
    assert BODY not in serialized
    assert SECTION not in serialized
    assert str(tmp_path) not in serialized


def test_the_publication_result_carries_no_content(legacy_store, tmp_path):
    entry = _entry()
    legacy_store.add(entry)
    expected = legacy_store.snapshot()
    plan = plan_graph_seed_backfill(expected, [entry])

    serialized = apply_graph_seed_backfill(
        legacy_store, plan, expected=expected
    ).result.to_json()

    assert BODY not in serialized
    assert SECTION not in serialized
    assert str(tmp_path) not in serialized

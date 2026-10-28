"""v3.6.1 — the persistent, append-only semantic decision journal.

Why this file exists: ARCHITECTURE_v2.md "Framework completion and migration
delivery contract" makes the journal the *authority* a graph write commits to
before any projection runs. The properties pinned here are the ones the rest of
the framework is allowed to assume:

  * append-only, monotonic positions, stable request/event identity;
  * an identical retry is a no-op, a changed retry under one request identity is
    refused and appends nothing;
  * one canonical payload per event ID, across requests;
  * serialized commits across independent connections/processes;
  * projection checkpoints that only ever move when a caller says a projection
    actually succeeded;
  * durability across close/reopen;
  * bounded health — counts, statuses, checkpoints, never a path or a body.

Isolation: every database is under pytest's ``tmp_path``. No config file, no
memory home, no network, no embedding model and no user data is touched.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from layered_memory_mcp.graph_journal import (
    JOURNAL_SCHEMA_VERSION,
    PROJECTION_GRAPH,
    PROJECTION_L0,
    PROJECTION_L1,
    PROJECTION_NAMES,
    PROJECTION_VECTOR,
    JournalClosedError,
    JournalConflictError,
    JournalSchemaError,
    JournalUnavailableError,
    MemoryEventJournal,
    SurfaceRecord,
)
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    MemoryEvent,
    MemoryEventKind,
    NodeKind,
)

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)


def _node(node_id: str, content: str = "The index rebuild runs nightly.") -> GraphNode:
    return GraphNode(
        id=node_id,
        kind=NodeKind.CLAIM,
        label="index rebuild",
        content=content,
        recorded_at=T0,
        derivation=Derivation(method=DerivationMethod.USER_EXPLICIT),
    )


def _event(
    event_id: str,
    node_id: str,
    content: str = "The index rebuild runs nightly.",
    offset: int = 0,
) -> MemoryEvent:
    return MemoryEvent(
        id=event_id,
        kind=MemoryEventKind.INJECT,
        occurred_at=T0 + timedelta(microseconds=offset),
        node=_node(node_id, content),
    )


def _database(tmp_path, name: str = "journal.db"):
    parent = tmp_path / "graph-data"
    parent.mkdir(parents=True, exist_ok=True)
    return parent / name


@pytest.fixture
def journal(tmp_path):
    handle = MemoryEventJournal(_database(tmp_path))
    try:
        yield handle
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Append, order, identity
# ---------------------------------------------------------------------------

def test_append_assigns_monotonic_positions_and_replays_in_order(journal):
    first = journal.append("request-a", [_event("event_a1", "claim_a")])
    second = journal.append(
        "request-b",
        [_event("event_b1", "claim_b", offset=1), _event("event_b2", "claim_c", offset=2)],
    )

    assert (first.first_position, first.last_position) == (1, 1)
    assert (second.first_position, second.last_position) == (2, 3)
    assert journal.head_position() == 3
    assert [event.id for event in journal.read_events()] == [
        "event_a1",
        "event_b1",
        "event_b2",
    ]


def test_an_identical_retry_appends_nothing_and_reports_the_first_commit(journal):
    events = [_event("event_a1", "claim_a")]
    first = journal.append("request-a", events)
    retry = journal.append("request-a", [_event("event_a1", "claim_a")])

    assert first.duplicate is False and retry.duplicate is True
    assert retry.events_appended == 0
    assert (retry.first_position, retry.last_position) == (
        first.first_position,
        first.last_position,
    )
    assert retry.event_ids == first.event_ids
    assert journal.head_position() == 1
    assert len(journal.read_events()) == 1


def test_a_changed_payload_under_one_request_identity_is_refused_and_writes_nothing(
    journal,
):
    journal.append("request-a", [_event("event_a1", "claim_a")])

    with pytest.raises(JournalConflictError) as excinfo:
        journal.append("request-a", [_event("event_a1", "claim_a", content="rewritten")])

    assert "request-a" in str(excinfo.value)
    assert journal.head_position() == 1
    assert journal.read_events()[0].node.content == "The index rebuild runs nightly."


def test_one_event_identity_may_not_carry_two_payloads_across_requests(journal):
    journal.append("request-a", [_event("event_a1", "claim_a")])

    with pytest.raises(JournalConflictError):
        journal.append("request-b", [_event("event_a1", "claim_a", content="other")])

    assert journal.head_position() == 1
    assert journal.request_count() == 1


def test_replaying_an_event_identity_verbatim_in_a_new_request_appends_nothing_new(
    journal,
):
    journal.append("request-a", [_event("event_a1", "claim_a")])
    result = journal.append(
        "request-b", [_event("event_a1", "claim_a"), _event("event_b1", "claim_b", offset=1)]
    )

    assert result.events_appended == 1
    assert [event.id for event in journal.read_events()] == ["event_a1", "event_b1"]


def test_an_empty_event_list_is_refused_rather_than_recorded_as_a_commit(journal):
    with pytest.raises(ValueError):
        journal.append("request-empty", [])
    assert journal.request_count() == 0


# ---------------------------------------------------------------------------
# Bootstrap/import records are part of the same stream
# ---------------------------------------------------------------------------

def test_a_bootstrap_import_is_part_of_the_replayed_stream(journal):
    journal.append(
        "import-2026-05-01",
        [_event("event_i1", "claim_imported")],
        kind="bootstrap_import",
    )
    journal.append("request-a", [_event("event_a1", "claim_a", offset=5)])

    assert [event.id for event in journal.read_events()] == ["event_i1", "event_a1"]
    assert journal.request_kinds() == {"bootstrap_import": 1, "semantic_write": 1}


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def test_checkpoints_start_at_zero_and_only_move_when_told(journal):
    journal.append("request-a", [_event("event_a1", "claim_a")])

    assert {name: journal.checkpoint(name).position for name in PROJECTION_NAMES} == {
        name: 0 for name in PROJECTION_NAMES
    }
    assert set(journal.pending_projections()) == set(PROJECTION_NAMES)

    journal.set_checkpoint(PROJECTION_GRAPH, 1)
    assert journal.checkpoint(PROJECTION_GRAPH).position == 1
    assert PROJECTION_GRAPH not in journal.pending_projections()
    assert set(journal.pending_projections()) == {
        PROJECTION_L1,
        PROJECTION_VECTOR,
        PROJECTION_L0,
    }


def test_a_checkpoint_never_moves_backwards_and_an_unknown_name_is_refused(journal):
    journal.append("request-a", [_event("event_a1", "claim_a")])
    journal.set_checkpoint(PROJECTION_GRAPH, 1)

    with pytest.raises(ValueError):
        journal.set_checkpoint(PROJECTION_GRAPH, 0)
    with pytest.raises(ValueError):
        journal.set_checkpoint("not_a_projection", 1)
    with pytest.raises(ValueError):
        journal.set_checkpoint(PROJECTION_GRAPH, 99)

    assert journal.checkpoint(PROJECTION_GRAPH).position == 1


def test_a_failed_projection_is_recorded_without_advancing_the_checkpoint(journal):
    journal.append("request-a", [_event("event_a1", "claim_a")])
    journal.record_projection_failure(PROJECTION_L1, "surface_drift")

    checkpoint = journal.checkpoint(PROJECTION_L1)
    assert checkpoint.position == 0
    assert checkpoint.status == "failed"
    assert checkpoint.detail == "surface_drift"
    assert PROJECTION_L1 in journal.pending_projections()


# ---------------------------------------------------------------------------
# Managed surface registry
# ---------------------------------------------------------------------------

def test_a_managed_surface_is_recorded_and_replaced_in_place(journal):
    journal.record_surface(
        SurfaceRecord(
            entry_id="mdsec1_abc",
            domain="retrieval",
            section="Index maintenance",
            occurrence=0,
            node_id="claim_a",
            body_sha256="a" * 64,
            position=1,
        )
    )
    journal.record_surface(
        SurfaceRecord(
            entry_id="mdsec1_abc",
            domain="retrieval",
            section="Index maintenance",
            occurrence=0,
            node_id="claim_b",
            body_sha256="b" * 64,
            position=2,
        )
    )

    record = journal.surface("mdsec1_abc")
    assert record is not None
    assert (record.node_id, record.position) == ("claim_b", 2)
    assert [item.entry_id for item in journal.surfaces()] == ["mdsec1_abc"]
    assert journal.surface_node_ids()["mdsec1_abc"] == "claim_b"


def test_a_retired_surface_is_marked_rather_than_deleted(journal):
    journal.record_surface(
        SurfaceRecord(
            entry_id="mdsec1_abc",
            domain="retrieval",
            section="Index maintenance",
            occurrence=0,
            node_id="claim_a",
            body_sha256="a" * 64,
            position=1,
        )
    )
    journal.retire_surface("mdsec1_abc", position=2)

    record = journal.surface("mdsec1_abc")
    assert record is not None and record.status == "retired"
    assert "mdsec1_abc" not in journal.surface_node_ids()


# ---------------------------------------------------------------------------
# Durability, lifecycle, serialization
# ---------------------------------------------------------------------------

def test_events_and_checkpoints_survive_close_and_reopen(tmp_path):
    database = _database(tmp_path)
    writer = MemoryEventJournal(database)
    writer.append("request-a", [_event("event_a1", "claim_a")])
    writer.set_checkpoint(PROJECTION_GRAPH, 1)
    writer.close()

    reader = MemoryEventJournal(database)
    try:
        assert [event.id for event in reader.read_events()] == ["event_a1"]
        assert reader.head_position() == 1
        assert reader.checkpoint(PROJECTION_GRAPH).position == 1
    finally:
        reader.close()


def test_two_independent_connections_serialize_their_commits(tmp_path):
    database = _database(tmp_path)
    one = MemoryEventJournal(database)
    two = MemoryEventJournal(database)
    try:
        one.append("request-a", [_event("event_a1", "claim_a")])
        two.append("request-b", [_event("event_b1", "claim_b", offset=1)])

        positions = [one.head_position(), two.head_position()]
        assert positions == [2, 2]
        assert [event.id for event in two.read_events()] == ["event_a1", "event_b1"]
    finally:
        one.close()
        two.close()


def test_a_missing_parent_directory_is_refused_and_never_created(tmp_path):
    target = tmp_path / "absent" / "journal.db"
    with pytest.raises(JournalUnavailableError):
        MemoryEventJournal(target)
    assert not target.parent.exists()


def test_a_closed_journal_refuses_work_but_still_reports_health(tmp_path):
    handle = MemoryEventJournal(_database(tmp_path))
    handle.append("request-a", [_event("event_a1", "claim_a")])
    handle.close()
    handle.close()  # idempotent

    with pytest.raises(JournalClosedError):
        handle.read_events()
    with pytest.raises(JournalClosedError):
        handle.append("request-b", [_event("event_b1", "claim_b")])

    health = handle.health()
    assert health.errors == ["journal_closed"]
    assert health.healthy is False


def test_a_newer_store_layout_is_refused_rather_than_adopted(tmp_path):
    database = _database(tmp_path)
    MemoryEventJournal(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE journal_meta SET value = ? WHERE key = 'journal_schema_version'",
            (str(JOURNAL_SCHEMA_VERSION + 1),),
        )

    with pytest.raises(JournalSchemaError):
        MemoryEventJournal(database)


# ---------------------------------------------------------------------------
# Bounded observability (§15)
# ---------------------------------------------------------------------------

def test_health_reports_counts_and_checkpoints_and_leaks_nothing(tmp_path):
    database = _database(tmp_path)
    handle = MemoryEventJournal(database)
    try:
        handle.append("request-a", [_event("event_a1", "claim_a")])
        handle.set_checkpoint(PROJECTION_GRAPH, 1)
        health = handle.health()
        payload = json.dumps(health.to_dict(), ensure_ascii=False)
    finally:
        handle.close()

    assert health.event_count == 1
    assert health.request_count == 1
    assert health.head_position == 1
    assert health.checkpoints[PROJECTION_GRAPH] == 1
    assert set(health.pending_projections) == {
        PROJECTION_L1,
        PROJECTION_VECTOR,
        PROJECTION_L0,
    }
    assert health.healthy is True
    assert str(database) not in payload
    assert "The index rebuild runs nightly." not in payload

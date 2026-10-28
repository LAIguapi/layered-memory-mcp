"""v3.6.1 — the journal's read-only inspection path, and what it must not do.

Why this file exists: ``runtime_status`` is documented as a read-only operator
view, but it used to reach the journal through the *writable* open path. That
path creates the database when it is absent and establishes its schema,
metadata and checkpoint rows inside a ``BEGIN IMMEDIATE`` — so asking "has
anything been committed?" on a system that never committed brought the
authority into existence, and asking it on a live one took a write lock on the
file every operator tool was promised not to touch.

What is pinned here:

  * a missing journal stays missing — no file, no directory, no sibling
    ``-wal``/``-shm``/``-journal`` artefact — and is *reported*, not created;
  * an existing journal is unchanged by inspection: identical bytes, identical
    modification time, identical checkpoints;
  * an inspection handle refuses every mutating method by name rather than
    discovering it as a SQLite error;
  * a file that is not a journal, and a journal of a layout this process does
    not speak, are each refused with their own distinguishable refusal;
  * the writable path still does all of the above *on purpose* — it creates the
    database, the schema, the metadata and the seeded checkpoint rows;
  * ``runtime_status`` uses the read-only path for both the journal block and
    the projection-drift check.

Isolation: every database is under pytest's ``tmp_path``. No config file, no
memory home, no network, no embedding model and no user data is touched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_application import runtime_status
from layered_memory_mcp.graph_journal import (
    JOURNAL_SCHEMA_VERSION,
    PROJECTION_GRAPH,
    PROJECTION_NAMES,
    JournalMissingError,
    JournalNotInitializedError,
    JournalReadOnlyError,
    JournalSchemaError,
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
from layered_memory_mcp.graph_runtime_assembly import (
    JOURNAL_NOT_CONFIGURED,
    JOURNAL_NOT_INITIALIZED,
    JOURNAL_NOT_PRESENT,
    JOURNAL_NOT_REQUESTED,
    JOURNAL_OPENED,
    open_memory_journal,
    open_memory_journal_readonly,
)

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)

BODY = "The retrieval index is rebuilt nightly at 02:00 UTC."


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _event(event_id: str, node_id: str, offset: int = 0) -> MemoryEvent:
    return MemoryEvent(
        id=event_id,
        kind=MemoryEventKind.INJECT,
        occurred_at=T0 + timedelta(microseconds=offset),
        node=GraphNode(
            id=node_id,
            kind=NodeKind.CLAIM,
            label="retrieval index",
            content=BODY,
            recorded_at=T0,
            derivation=Derivation(method=DerivationMethod.USER_EXPLICIT),
        ),
    )


@pytest.fixture
def data_dir(tmp_path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def config(tmp_path, data_dir) -> MemoryConfig:
    """A graph-write config whose journal has deliberately *not* been created."""
    home = tmp_path / "home"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        l0_index_file=str(home / "L0.md"),
        graph_mode="graph_write",
        graph_db_path=str(data_dir / "graph.db"),
        graph_journal_path=str(data_dir / "journal.db"),
        auto_maintain=False,
    )


def _populate(database: Path) -> None:
    """One committed decision, one advanced checkpoint, one managed surface."""
    with MemoryEventJournal(database) as journal:
        journal.append("request-a", [_event("event_a1", "claim_a")])
        journal.set_checkpoint(PROJECTION_GRAPH, 1)
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


class _Fingerprint:
    """Everything about a journal file that an observer must not change."""

    def __init__(self, database: Path) -> None:
        stat = database.stat()
        self.digest = hashlib.sha256(database.read_bytes()).hexdigest()
        self.size = stat.st_size
        self.mtime_ns = stat.st_mtime_ns
        self.siblings = sorted(
            entry.name
            for entry in database.parent.iterdir()
            if entry.name.startswith(database.name)
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Fingerprint) and self.__dict__ == other.__dict__

    def __repr__(self) -> str:  # pragma: no cover - failure output only
        return f"_Fingerprint({self.__dict__})"


def _checkpoints(database: Path) -> dict[str, tuple[int, str]]:
    with MemoryEventJournal.open_readonly(database) as journal:
        return {
            name: (journal.checkpoint(name).position, journal.checkpoint(name).status)
            for name in PROJECTION_NAMES
        }


# ---------------------------------------------------------------------------
# A missing journal stays missing
# ---------------------------------------------------------------------------

def test_a_missing_journal_is_refused_and_never_created(data_dir):
    database = data_dir / "journal.db"

    with pytest.raises(JournalMissingError):
        MemoryEventJournal.open_readonly(database)

    assert not database.exists()
    assert sorted(entry.name for entry in data_dir.iterdir()) == []


def test_a_missing_parent_directory_is_refused_and_never_created(tmp_path):
    database = tmp_path / "absent" / "journal.db"

    with pytest.raises(JournalMissingError):
        MemoryEventJournal.open_readonly(database)

    assert not database.parent.exists()


def test_a_file_that_is_not_a_journal_is_reported_rather_than_initialized(data_dir):
    database = data_dir / "journal.db"
    database.write_bytes(b"")
    foreign = data_dir / "other.db"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")

    for target in (database, foreign):
        before = _Fingerprint(target)
        with pytest.raises(JournalNotInitializedError):
            MemoryEventJournal.open_readonly(target)
        assert _Fingerprint(target) == before


def test_an_in_memory_or_borrowed_target_is_refused_for_inspection(data_dir):
    with pytest.raises(JournalMissingError):
        MemoryEventJournal.open_readonly(":memory:")

    database = data_dir / "journal.db"
    _populate(database)
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(JournalReadOnlyError):
            MemoryEventJournal(connection, read_only=True)
    finally:
        connection.close()


def test_a_newer_layout_is_refused_by_the_read_only_path_too(data_dir):
    database = data_dir / "journal.db"
    _populate(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE journal_meta SET value = ? WHERE key = 'journal_schema_version'",
            (str(JOURNAL_SCHEMA_VERSION + 1),),
        )
    before = _Fingerprint(database)

    with pytest.raises(JournalSchemaError):
        MemoryEventJournal.open_readonly(database)

    assert _Fingerprint(database) == before


# ---------------------------------------------------------------------------
# An existing journal is readable, and unchanged by reading it
# ---------------------------------------------------------------------------

def test_inspection_reads_everything_and_changes_nothing(data_dir):
    database = data_dir / "journal.db"
    _populate(database)
    before = _Fingerprint(database)
    expected_checkpoints = _checkpoints(database)
    after_first_read = _Fingerprint(database)

    with MemoryEventJournal.open_readonly(database) as journal:
        assert journal.read_only is True
        assert [event.id for event in journal.read_events()] == ["event_a1"]
        assert journal.head_position() == 1
        assert journal.event_count() == 1
        assert journal.request_count() == 1
        assert journal.request_hash("request-a") is not None
        assert journal.checkpoint(PROJECTION_GRAPH).position == 1
        assert journal.surface_node_ids() == {"mdsec1_abc": "claim_a"}
        assert journal.surfaces_by_node()["claim_a"].entry_id == "mdsec1_abc"
        health = journal.health()

    assert health.event_count == 1
    assert health.head_position == 1
    assert health.healthy is True
    assert str(database) not in json.dumps(health.to_dict(), ensure_ascii=False)

    assert after_first_read == before
    assert _Fingerprint(database) == before
    assert _checkpoints(database) == expected_checkpoints


def test_every_mutating_method_is_refused_by_name_on_an_inspection_handle(data_dir):
    database = data_dir / "journal.db"
    _populate(database)
    before = _Fingerprint(database)

    with MemoryEventJournal.open_readonly(database) as journal:
        with pytest.raises(JournalReadOnlyError):
            journal.append("request-b", [_event("event_b1", "claim_b", offset=1)])
        with pytest.raises(JournalReadOnlyError):
            journal.set_checkpoint(PROJECTION_GRAPH, 1)
        with pytest.raises(JournalReadOnlyError):
            journal.record_projection_failure(PROJECTION_GRAPH, "nope")
        with pytest.raises(JournalReadOnlyError):
            journal.record_surface(
                SurfaceRecord(
                    entry_id="mdsec1_def",
                    domain="retrieval",
                    section="Other",
                    node_id="claim_a",
                    body_sha256="b" * 64,
                )
            )
        with pytest.raises(JournalReadOnlyError):
            journal.retire_surface("mdsec1_abc", position=1)

    assert _Fingerprint(database) == before


# ---------------------------------------------------------------------------
# The writable path still does exactly what a committer needs
# ---------------------------------------------------------------------------

def test_the_writable_path_still_creates_the_database_schema_and_checkpoints(data_dir):
    database = data_dir / "journal.db"
    assert not database.exists()

    with MemoryEventJournal(database) as journal:
        journal.append("request-a", [_event("event_a1", "claim_a")])

    assert database.is_file()
    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = connection.execute(
            "SELECT value FROM journal_meta WHERE key = 'journal_schema_version'"
        ).fetchone()
        seeded = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM projection_checkpoints"
            ).fetchall()
        }
    assert {
        "journal_meta",
        "memory_events",
        "memory_requests",
        "projection_checkpoints",
        "projection_surfaces",
    } <= tables
    assert version is not None and str(version[0]) == str(JOURNAL_SCHEMA_VERSION)
    assert seeded == set(PROJECTION_NAMES)
    # And what it wrote is exactly what the read-only path can then read.
    with MemoryEventJournal.open_readonly(database) as reader:
        assert [event.id for event in reader.read_events()] == ["event_a1"]


def test_the_writable_assembly_still_creates_a_missing_journal(config, data_dir):
    database = Path(config.graph_journal_path)
    assert not database.exists()

    assembly = open_memory_journal(config, require=True)
    try:
        assert assembly.report.status == JOURNAL_OPENED
        assert assembly.report.opened is True
        assert assembly.report.read_only is False
        assert assembly.journal is not None
        assert assembly.journal.read_only is False
    finally:
        assembly.close()

    assert database.is_file()


# ---------------------------------------------------------------------------
# The read-only assembly: four distinguishable answers, none of them a write
# ---------------------------------------------------------------------------

def test_the_read_only_assembly_opens_nothing_unless_asked(config):
    assembly = open_memory_journal_readonly(config)
    try:
        assert assembly.report.status == JOURNAL_NOT_REQUESTED
        assert assembly.report.path_configured is True
        assert assembly.report.read_only is True
        assert assembly.journal is None
    finally:
        assembly.close()

    assert not Path(config.graph_journal_path).exists()


def test_the_read_only_assembly_reports_an_absent_journal_without_creating_it(config):
    database = Path(config.graph_journal_path)

    assembly = open_memory_journal_readonly(config, require=True)
    try:
        assert assembly.report.status == JOURNAL_NOT_PRESENT
        assert assembly.report.path_configured is True
        assert assembly.report.opened is False
        assert assembly.report.errors == [JOURNAL_NOT_PRESENT]
        assert assembly.journal is None
    finally:
        assembly.close()

    assert not database.exists()
    assert sorted(entry.name for entry in database.parent.iterdir()) == []


def test_the_read_only_assembly_distinguishes_an_uninitialized_journal(config):
    Path(config.graph_journal_path).write_bytes(b"")

    assembly = open_memory_journal_readonly(config, require=True)
    try:
        assert assembly.report.status == JOURNAL_NOT_INITIALIZED
        assert assembly.report.errors == [JOURNAL_NOT_INITIALIZED]
        assert assembly.journal is None
    finally:
        assembly.close()


def test_the_read_only_assembly_reports_an_unconfigured_journal(tmp_path):
    bare = MemoryConfig(home=str(tmp_path / "home"), auto_maintain=False)

    assembly = open_memory_journal_readonly(bare, require=True)
    try:
        assert assembly.report.status == JOURNAL_NOT_CONFIGURED
        assert assembly.report.path_configured is False
        assert assembly.journal is None
    finally:
        assembly.close()


def test_the_read_only_assembly_opens_an_existing_journal(config):
    _populate(Path(config.graph_journal_path))
    before = _Fingerprint(Path(config.graph_journal_path))

    assembly = open_memory_journal_readonly(config, require=True)
    try:
        assert assembly.report.status == JOURNAL_OPENED
        assert assembly.report.opened is True
        assert assembly.report.read_only is True
        assert assembly.available is True
        assert assembly.journal is not None
        assert assembly.journal.read_only is True
        assert assembly.journal.head_position() == 1
    finally:
        assembly.close()

    assert _Fingerprint(Path(config.graph_journal_path)) == before


# ---------------------------------------------------------------------------
# runtime_status: the diagnostic that must not be able to change its subject
# ---------------------------------------------------------------------------

def test_runtime_status_reports_an_absent_journal_and_leaves_it_absent(config):
    database = Path(config.graph_journal_path)

    status = runtime_status(config)

    assert status["journal"]["status"] == JOURNAL_NOT_PRESENT
    assert status["journal"]["path_configured"] is True
    assert status["journal"]["errors"] == [JOURNAL_NOT_PRESENT]
    assert "surfaces" not in status
    assert status["projection_drift"]["errors"] == []
    assert status["projection_drift"]["drifted"] is False
    assert not database.exists()
    assert not any(
        entry.name.startswith(database.name) for entry in database.parent.iterdir()
    )
    # Bounded as before (§15): no path, no body.
    payload = json.dumps(status, ensure_ascii=False)
    assert str(database) not in payload
    assert BODY not in payload


def test_runtime_status_leaves_an_existing_journal_byte_for_byte_unchanged(config):
    database = Path(config.graph_journal_path)
    _populate(database)
    before = _Fingerprint(database)
    expected_checkpoints = _checkpoints(database)

    status = runtime_status(config)

    assert status["journal"]["status"] == JOURNAL_OPENED
    assert status["journal"]["event_count"] == 1
    assert status["journal"]["head_position"] == 1
    assert status["surfaces"] == {"published": 1, "total": 1}
    assert _Fingerprint(database) == before
    assert _checkpoints(database) == expected_checkpoints
    assert str(database) not in json.dumps(status, ensure_ascii=False)


def test_the_fingerprint_used_above_does_notice_a_write(data_dir):
    """Guard for the guards: an unchanged fingerprint has to mean something."""
    database = data_dir / "journal.db"
    _populate(database)
    before = _Fingerprint(database)

    with MemoryEventJournal(database) as journal:
        journal.append("request-b", [_event("event_b1", "claim_b", offset=1)])

    assert _Fingerprint(database) != before


def test_runtime_status_is_repeatable_without_accumulating_any_change(config):
    database = Path(config.graph_journal_path)
    _populate(database)
    runtime_status(config)
    before = _Fingerprint(database)

    for _attempt in range(3):
        runtime_status(config)

    assert _Fingerprint(database) == before

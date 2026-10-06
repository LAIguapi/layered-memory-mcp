"""v3.4.1 — terminal timestamps for TODO status changes.

Reported shape (audit of the live DB, 2026-10-06): six rows sat in ``cancelled``
with ``completed_at`` NULL and no cancel timestamp anywhere — "when was this
abandoned?" could only be answered by ``updated_at``, which any later edit
refreshes. Worse, nothing cleared ``completed_at`` when a row moved back out of
``completed``, so the next reopen would have produced "still open, but finished
at …" — a self-contradicting row that no reader can trust.

Rules locked down here:

1. ``status=completed`` → ``completed_at`` stamped, ``cancelled_at`` cleared
2. ``status=cancelled`` → ``cancelled_at`` stamped, ``completed_at`` cleared
3. reopened (``pending`` / ``in_progress``) → **both** cleared
4. neither stamp is caller-writable (no faked finish times)
5. a pre-v3.4.1 table (no ``cancelled_at`` column) migrates in place

Fixtures use neutral placeholder content.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from layered_memory_mcp.models import TodoEntry
from layered_memory_mcp.todo_store import TodoStore


def _new_store(tmp_path: Path) -> TodoStore:
    return TodoStore(tmp_path / "todos.db")


def _add(store: TodoStore, content: str = "placeholder task") -> str:
    entry = TodoEntry(domain="test", content=content)
    store.add(entry)
    return entry.id


def _row(store: TodoStore, todo_id: str) -> dict:
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone())


class TestTerminalTimestamps:
    def test_completed_stamps_completed_at(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, status="completed")
        row = _row(store, todo_id)
        assert row["completed_at"] is not None
        assert row["cancelled_at"] is None

    def test_cancelled_stamps_cancelled_at(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, status="cancelled")
        row = _row(store, todo_id)
        assert row["cancelled_at"] is not None
        assert row["completed_at"] is None

    def test_cancelling_a_completed_todo_clears_completed_at(self, tmp_path):
        """completed → cancelled must not leave both stamps set."""
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, status="completed")
        store.update(todo_id, status="cancelled")
        row = _row(store, todo_id)
        assert row["cancelled_at"] is not None
        assert row["completed_at"] is None

    def test_reopening_clears_both_stamps(self, tmp_path):
        """The stale-timestamp bug: open row carrying a finish time."""
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, status="completed")
        assert _row(store, todo_id)["completed_at"] is not None

        store.update(todo_id, status="pending")
        row = _row(store, todo_id)
        assert row["completed_at"] is None
        assert row["cancelled_at"] is None

    def test_cancelled_then_reopened_clears_cancelled_at(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, status="cancelled")
        store.update(todo_id, status="in_progress")
        row = _row(store, todo_id)
        assert row["completed_at"] is None
        assert row["cancelled_at"] is None

    def test_non_status_edit_leaves_terminal_stamp_alone(self, tmp_path):
        """A notes-only edit must not wipe the finish time, nor refresh it."""
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, status="completed")
        stamped = _row(store, todo_id)["completed_at"]

        store.update(todo_id, notes="added a note later")
        row = _row(store, todo_id)
        assert row["completed_at"] == stamped
        assert row["updated_at"] >= stamped


class TestTimestampsAreNotCallerWritable:
    def test_passing_completed_at_is_ignored(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, completed_at="1999-01-01T00:00:00+00:00")
        assert _row(store, todo_id)["completed_at"] is None

    def test_passing_cancelled_at_is_ignored(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, cancelled_at="1999-01-01T00:00:00+00:00")
        assert _row(store, todo_id)["cancelled_at"] is None


class TestLegacyDbMigration:
    def test_pre_v341_table_gains_cancelled_at(self, tmp_path):
        db = tmp_path / "legacy.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""CREATE TABLE todos (
                id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                content TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'pending',
                source_session_id TEXT,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )""")
            conn.execute(
                "INSERT INTO todos (id, domain, content, status, created_at, updated_at, completed_at) "
                "VALUES ('legacy-1','test','placeholder','cancelled','2026-01-01','2026-01-01',NULL)")
        store = TodoStore(db)  # constructor runs the migration

        cols = [r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(todos)")]
        assert "cancelled_at" in cols
        # legacy row survives untouched, and is usable afterwards
        assert _row(store, "legacy-1")["status"] == "cancelled"
        assert store.update("legacy-1", status="pending")["success"] is True
        assert _row(store, "legacy-1")["completed_at"] is None

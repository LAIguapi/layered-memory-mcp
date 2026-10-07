"""v3.4.6 — write paths must not report success for an id that matched no row.

Reported shape (two live incidents, 2026-10-07): an agent called ``update_todo``
with a mistyped id — once the real ``8ff292e9-…`` with one character corrupted,
once a fabricated UUID for a row the agent had only seen truncated. Both calls
returned ``{"success": true, "id": "<the bad id>"}`` and the row never changed.
Because the reply looked identical to a real edit, the mistake was invisible until
a later read-back showed the stale content — i.e. the *tool* was lying, not the
caller misunderstanding.

Same class of defect on three paths, all locked down here:

1. ``update()`` with an unknown id → ``success: False`` + ``"TODO not found"``
2. ``delete()`` with an unknown id → ``success: False`` (it claimed success too)
3. ``update(status=...)`` / ``update(priority=...)`` with a value outside
   :class:`TodoStatus` / :class:`TodoPriority` → rejected instead of written, so a
   garbage status can no longer skip the terminal-timestamp machine silently

The guard must not misfire: a real edit that happens to store identical values
still counts as a match (``rowcount == 1`` in SQLite), so it keeps returning True.

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
        row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    assert row is not None, f"row {todo_id} missing"
    return dict(row)


def _exists(store: TodoStore, todo_id: str) -> bool:
    with sqlite3.connect(str(store.db_path)) as conn:
        return conn.execute("SELECT 1 FROM todos WHERE id=?", (todo_id,)).fetchone() is not None


class TestUnknownIdIsNotSuccess:
    def test_update_unknown_id_reports_failure(self, tmp_path):
        store = _new_store(tmp_path)
        result = store.update("no-such-id", status="completed")
        assert result["success"] is False
        assert "not found" in result["error"].lower()
        assert "no-such-id" in result["error"]

    def test_update_one_character_off_is_reported(self, tmp_path):
        """The live incident: a single corrupted character must not read as applied."""
        store = _new_store(tmp_path)
        todo_id = _add(store)
        corrupted = ("d" + todo_id[1:]) if not todo_id.startswith("d") else ("e" + todo_id[1:])
        assert corrupted != todo_id
        result = store.update(corrupted, status="completed")
        assert result["success"] is False
        # the real row is untouched
        row = _row(store, todo_id)
        assert row["status"] == "pending"
        assert row["completed_at"] is None

    def test_update_sibling_row_is_not_touched(self, tmp_path):
        """A bad id must not fall through onto some other row's UPDATE."""
        store = _new_store(tmp_path)
        keep_id = _add(store, "keep me")
        _add(store, "other row")
        result = store.update("fabricated-0000", content="overwritten?")
        assert result["success"] is False
        assert _row(store, keep_id)["content"] == "keep me"

    def test_delete_unknown_id_reports_failure(self, tmp_path):
        store = _new_store(tmp_path)
        result = store.delete("no-such-id")
        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestGuardDoesNotMisfire:
    def test_update_existing_id_still_succeeds(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        result = store.update(todo_id, status="completed")
        assert result["success"] is True
        assert result["id"] == todo_id
        assert _row(store, todo_id)["status"] == "completed"

    def test_idempotent_update_still_succeeds(self, tmp_path):
        """Writing the same values back matches the row (rowcount=1) — still success."""
        store = _new_store(tmp_path)
        todo_id = _add(store)
        store.update(todo_id, content="same text")
        result = store.update(todo_id, content="same text")
        assert result["success"] is True

    def test_delete_existing_id_still_succeeds(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        assert store.delete(todo_id)["success"] is True
        assert _exists(store, todo_id) is False


class TestEnumValuesAreValidated:
    def test_unknown_status_rejected_and_nothing_stamped(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        result = store.update(todo_id, status="banana")
        assert result["success"] is False
        assert "invalid status" in result["error"].lower()
        row = _row(store, todo_id)
        assert row["status"] == "pending"
        assert row["completed_at"] is None
        assert row["cancelled_at"] is None

    def test_unknown_priority_rejected(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        result = store.update(todo_id, priority="urgent-ish")
        assert result["success"] is False
        assert "invalid priority" in result["error"].lower()
        assert _row(store, todo_id)["priority"] == "medium"

    def test_real_status_still_applies_with_the_machine(self, tmp_path):
        store = _new_store(tmp_path)
        todo_id = _add(store)
        assert store.update(todo_id, status="cancelled")["success"] is True
        row = _row(store, todo_id)
        assert row["status"] == "cancelled"
        assert row["cancelled_at"] is not None
        assert row["completed_at"] is None

    def test_priority_waiting_is_valid(self, tmp_path):
        """'waiting' is a real priority (see the ORDER BY in list()) — keep it open."""
        store = _new_store(tmp_path)
        todo_id = _add(store)
        assert store.update(todo_id, priority="waiting")["success"] is True
        assert _row(store, todo_id)["priority"] == "waiting"

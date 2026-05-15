import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import TodoEntry, TodoStatus, TodoPriority


class TodoStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS todos (
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

    def add(self, entry: TodoEntry) -> dict:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""INSERT INTO todos VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (entry.id, entry.domain, entry.content, entry.priority.value,
                 entry.status.value, entry.source_session_id, entry.notes,
                 entry.created_at.isoformat(), entry.updated_at.isoformat(),
                 entry.completed_at.isoformat() if entry.completed_at else None))
        return {"success": True, "id": entry.id}

    def list(self, status=None, domain=None, priority=None, limit=50) -> list:
        where = ["1=1"]
        params = []
        if status:
            where.append("status=?")
            params.append(status)
        if domain:
            where.append("domain=?")
            params.append(domain)
        if priority:
            where.append("priority=?")
            params.append(priority)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM todos WHERE {' AND '.join(where)} ORDER BY CASE priority WHEN 'blocker' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 WHEN 'waiting' THEN 4 END, created_at DESC LIMIT ?",
                params + [limit]
            ).fetchall()
        return [dict(r) for r in rows]

    def update(self, todo_id: str, **kwargs) -> dict:
        allowed = {"status", "priority", "content", "notes", "completed_at"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return {"success": False, "error": "No valid fields"}

        if "status" in updates and updates["status"] == "completed":
            updates["completed_at"] = datetime.now(timezone.utc).isoformat()

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [todo_id]
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(f"UPDATE todos SET {set_clause} WHERE id=?", values)
        return {"success": True, "id": todo_id}

    def delete(self, todo_id: str) -> dict:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        return {"success": True, "id": todo_id}

    def stats(self) -> dict:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            by_status = [dict(r) for r in conn.execute(
                "SELECT status, COUNT(*) as count FROM todos GROUP BY status").fetchall()]
            by_domain = [dict(r) for r in conn.execute(
                "SELECT domain, COUNT(*) as count FROM todos WHERE status!='completed' AND status!='cancelled' GROUP BY domain").fetchall()]
            by_priority = [dict(r) for r in conn.execute(
                "SELECT priority, COUNT(*) as count FROM todos WHERE status='pending' OR status='in_progress' GROUP BY priority").fetchall()]
        return {"by_status": by_status, "by_domain": by_domain, "by_priority": by_priority, "total": sum(s["count"] for s in by_status)}

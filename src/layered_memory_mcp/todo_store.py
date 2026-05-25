import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import TodoEntry, TodoStatus, TodoPriority

# Beijing timezone: UTC+8
_BEIJING_TZ = timezone(timedelta(hours=8))


class TodoStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS todos (
                id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                title TEXT DEFAULT '',
                content TEXT NOT NULL,
                blocked_by TEXT DEFAULT '[]',
                priority TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'pending',
                source_session_id TEXT,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )""")
        # Migration: add title/blocked_by columns if upgrading from v2.1.0
        for col in [("title", "TEXT DEFAULT ''"), ("blocked_by", "TEXT DEFAULT '[]'")]:
            try:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute(f"ALTER TABLE todos ADD COLUMN {col[0]} {col[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists

    def add(self, entry: TodoEntry) -> dict:
        # Defense: ensure created_at and updated_at are never None
        now = datetime.now(timezone.utc)
        if entry.created_at is None:
            entry.created_at = now
        if entry.updated_at is None:
            entry.updated_at = now
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""INSERT INTO todos
                (id, domain, title, content, blocked_by, priority, status,
                 source_session_id, notes, created_at, updated_at, completed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entry.id, entry.domain, entry.title, entry.content,
                 json.dumps(entry.blocked_by),
                 entry.priority.value, entry.status.value,
                 entry.source_session_id, entry.notes,
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
        result = []
        for r in rows:
            d = dict(r)
            # created_at: UTC → 北京时间 yyyy-mm-dd HH:MM:SS
            try:
                dt = datetime.fromisoformat(d["created_at"])
                dt_local = dt.replace(tzinfo=timezone.utc).astimezone(_BEIJING_TZ)
                d["created_at"] = dt_local.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            # blocked_by: JSON string → list
            try:
                d["blocked_by"] = json.loads(d["blocked_by"]) if d.get("blocked_by") else []
            except Exception:
                d["blocked_by"] = []
            result.append(d)
        return result

    def update(self, todo_id: str, **kwargs) -> dict:
        allowed = {"status", "priority", "title", "content", "blocked_by", "notes", "completed_at"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return {"success": False, "error": "No valid fields"}

        if "blocked_by" in updates and isinstance(updates["blocked_by"], list):
            updates["blocked_by"] = json.dumps(updates["blocked_by"])

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

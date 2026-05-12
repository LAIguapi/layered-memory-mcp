"""Review queue for human-in-the-loop knowledge validation.

Stores pending knowledge entries awaiting human review.
Supports approve/reject operations with notes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import KnowledgeEntry, ReviewItem

logger = logging.getLogger("layered_memory_mcp.storage.review")


class ReviewQueue:
    """SQLite-backed review queue.

    Stores knowledge entries that need human review before being
    committed to the main knowledge base.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_queue (
                    id TEXT PRIMARY KEY,
                    entry_json TEXT NOT NULL,
                    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TIMESTAMP,
                    reviewed_by TEXT,
                    review_note TEXT,
                    status TEXT DEFAULT 'pending'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status)
            """)
            conn.commit()

    def submit(self, item: "ReviewItem") -> None:
        """Submit a knowledge entry for review."""
        entry_json = json.dumps(item.entry.model_dump(), default=str)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO review_queue
                (id, entry_json, submitted_at, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (item.entry.id, entry_json, item.submitted_at.isoformat()),
            )
            conn.commit()

    def list_pending(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List pending review items."""
        from ..models import KnowledgeEntry, SourceInfo, ReviewStatus

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, entry_json, submitted_at
                FROM review_queue
                WHERE status = 'pending'
                ORDER BY submitted_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

        results = []
        for row in rows:
            try:
                data = json.loads(row[1])
                # Reconstruct entry
                entry = KnowledgeEntry(**data)
                results.append({
                    "id": row[0],
                    "entry": entry,
                    "submitted_at": row[2],
                })
            except Exception as e:
                logger.warning("Failed to parse review item %s: %s", row[0], e)

        return results

    def approve(
        self,
        item_id: str,
        reviewer: str = "human",
        note: str = "",
    ) -> dict:
        """Approve a pending review item."""
        reviewed_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE review_queue
                SET status = 'approved', reviewed_at = ?, reviewed_by = ?, review_note = ?
                WHERE id = ? AND status = 'pending'
                """,
                (reviewed_at, reviewer, note, item_id),
            )
            conn.commit()

        if cursor.rowcount == 0:
            return {"success": False, "error": "Item not found or already reviewed"}

        return {"success": True, "action": "approved", "id": item_id}

    def reject(
        self,
        item_id: str,
        reviewer: str = "human",
        note: str = "",
    ) -> dict:
        """Reject a pending review item."""
        reviewed_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE review_queue
                SET status = 'rejected', reviewed_at = ?, reviewed_by = ?, review_note = ?
                WHERE id = ? AND status = 'pending'
                """,
                (reviewed_at, reviewer, note, item_id),
            )
            conn.commit()

        if cursor.rowcount == 0:
            return {"success": False, "error": "Item not found or already reviewed"}

        return {"success": True, "action": "rejected", "id": item_id}

    def get_stats(self) -> dict:
        """Get review queue statistics."""
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status = 'pending'"
            ).fetchone()[0]
            approved = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status = 'approved'"
            ).fetchone()[0]
            rejected = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status = 'rejected'"
            ).fetchone()[0]

        return {
            "total": total,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
        }

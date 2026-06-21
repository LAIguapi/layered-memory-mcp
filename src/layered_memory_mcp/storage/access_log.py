"""Access log store — knowledge recall telemetry (v2.6.0).

Records every knowledge retrieval (recall / semantic / get_file) as a
fire-and-forget event, so the framework can answer:
  - which knowledge is heavily used (top recalled)
  - which knowledge is dead weight (zombie: indexed but never recalled)
  - how a domain's recall trend moves over time

Design principles:
  - Independent SQLite db (~/.layered-memory/data/access_log.db), never
    touches L1 / L0 / vector data.
  - Writes are best-effort: a failed write is swallowed (debug log only),
    it must NEVER break the retrieval path.
  - No aggregation on the hot path — raw events only; stats computed at
    query time.
  - Auto-prunes events older than the retention window to stay bounded.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("layered_memory.access_log")

_BEIJING_TZ = timezone(timedelta(hours=8))

# Domains considered "zombie" only after this grace period with zero recalls,
# so freshly-injected knowledge isn't unfairly flagged.
ZOMBIE_GRACE_DAYS = 30


class AccessLogStore:
    """SQLite-backed recall telemetry. All writes are best-effort."""

    def __init__(self, db_path: Path, retention_days: int = 90, log_query: bool = True):
        self.db_path = db_path
        self.retention_days = retention_days
        self.log_query = log_query
        self._init_db()

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    access_type TEXT NOT NULL,
                    query TEXT,
                    rank INTEGER,
                    score REAL,
                    accessed_at TEXT NOT NULL
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_al_domain ON access_log(domain)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_al_time ON access_log(accessed_at)")
                conn.commit()
        except Exception as exc:
            logger.debug("access_log init failed: %s", exc)

    # ── Write path (best-effort) ────────────────────────────────────────

    def record(self, domain: str, access_type: str, query: str | None = None,
               rank: int | None = None, score: float | None = None) -> None:
        """Record a single recall event. Never raises."""
        if not domain:
            return
        try:
            q = None
            if self.log_query and query:
                q = query[:200]
            now = datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(str(self.db_path), timeout=2.0) as conn:
                conn.execute(
                    "INSERT INTO access_log (domain, access_type, query, rank, score, accessed_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (domain, access_type, q, rank, score, now),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("access_log record failed (domain=%s): %s", domain, exc)

    def record_batch(self, events: list[dict]) -> None:
        """Record multiple events in one transaction. Never raises.

        Each event: {domain, access_type, query?, rank?, score?}
        """
        if not events:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            rows = []
            for e in events:
                domain = e.get("domain")
                if not domain:
                    continue
                q = None
                if self.log_query and e.get("query"):
                    q = str(e["query"])[:200]
                rows.append((
                    domain, e.get("access_type", "recall"), q,
                    e.get("rank"), e.get("score"), now,
                ))
            if not rows:
                return
            with sqlite3.connect(str(self.db_path), timeout=2.0) as conn:
                conn.executemany(
                    "INSERT INTO access_log (domain, access_type, query, rank, score, accessed_at) "
                    "VALUES (?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
        except Exception as exc:
            logger.debug("access_log record_batch failed: %s", exc)

    def prune(self) -> int:
        """Delete events older than retention_days. Returns rows deleted."""
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
            with sqlite3.connect(str(self.db_path), timeout=5.0) as conn:
                cur = conn.execute("DELETE FROM access_log WHERE accessed_at < ?", (cutoff,))
                conn.commit()
                return cur.rowcount
        except Exception as exc:
            logger.debug("access_log prune failed: %s", exc)
            return 0

    # ── Read / aggregation path (off the hot path) ──────────────────────

    def _since(self, days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def top_recalled(self, days: int = 30, limit: int = 20) -> list[dict]:
        """Domains ranked by recall count within the window."""
        try:
            since = self._since(days)
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT domain, COUNT(*) AS hits, MAX(accessed_at) AS last_access, "
                    "AVG(score) AS avg_score "
                    "FROM access_log WHERE accessed_at >= ? "
                    "GROUP BY domain ORDER BY hits DESC, last_access DESC LIMIT ?",
                    (since, limit),
                ).fetchall()
            return [self._fmt_row(r) for r in rows]
        except Exception as exc:
            logger.debug("top_recalled failed: %s", exc)
            return []

    def domain_stats(self, domain: str, days: int = 30) -> dict:
        """Recall stats for a single domain."""
        try:
            since = self._since(days)
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT COUNT(*) AS hits, MAX(accessed_at) AS last_access, "
                    "MIN(accessed_at) AS first_access, AVG(score) AS avg_score "
                    "FROM access_log WHERE domain = ? AND accessed_at >= ?",
                    (domain, since),
                ).fetchone()
                total = conn.execute(
                    "SELECT COUNT(*) AS c, MAX(accessed_at) AS last FROM access_log WHERE domain = ?",
                    (domain,),
                ).fetchone()
            return {
                "domain": domain,
                "hits_in_window": row["hits"] if row else 0,
                "total_hits": total["c"] if total else 0,
                "last_access": _to_local(total["last"]) if total and total["last"] else None,
                "first_access_in_window": _to_local(row["first_access"]) if row and row["first_access"] else None,
                "avg_score": round(row["avg_score"], 4) if row and row["avg_score"] is not None else None,
            }
        except Exception as exc:
            logger.debug("domain_stats failed: %s", exc)
            return {"domain": domain, "hits_in_window": 0, "total_hits": 0}

    def trend(self, domain: str, days: int = 30) -> list[dict]:
        """Per-day recall counts for a domain (for trend curves)."""
        try:
            since = self._since(days)
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT substr(accessed_at, 1, 10) AS day, COUNT(*) AS hits "
                    "FROM access_log WHERE domain = ? AND accessed_at >= ? "
                    "GROUP BY day ORDER BY day",
                    (domain, since),
                ).fetchall()
            return [{"day": r["day"], "hits": r["hits"]} for r in rows]
        except Exception as exc:
            logger.debug("trend failed: %s", exc)
            return []

    def recalled_domains(self) -> set[str]:
        """All domains that have ever been recalled (any time)."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                rows = conn.execute("SELECT DISTINCT domain FROM access_log").fetchall()
            return {r[0] for r in rows if r[0]}
        except Exception as exc:
            logger.debug("recalled_domains failed: %s", exc)
            return set()

    def first_seen_map(self) -> dict[str, str]:
        """Map domain -> earliest accessed_at (for grace-period checks)."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                rows = conn.execute(
                    "SELECT domain, MIN(accessed_at) AS first FROM access_log GROUP BY domain"
                ).fetchall()
            return {r[0]: r[1] for r in rows if r[0]}
        except Exception as exc:
            logger.debug("first_seen_map failed: %s", exc)
            return {}

    def overview(self, days: int = 30) -> dict:
        """High-level totals for the window."""
        try:
            since = self._since(days)
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT COUNT(*) AS events, COUNT(DISTINCT domain) AS domains "
                    "FROM access_log WHERE accessed_at >= ?",
                    (since,),
                ).fetchone()
                by_type = conn.execute(
                    "SELECT access_type, COUNT(*) AS c FROM access_log "
                    "WHERE accessed_at >= ? GROUP BY access_type",
                    (since,),
                ).fetchall()
            return {
                "window_days": days,
                "total_events": row["events"] if row else 0,
                "active_domains": row["domains"] if row else 0,
                "by_type": {r["access_type"]: r["c"] for r in by_type},
            }
        except Exception as exc:
            logger.debug("overview failed: %s", exc)
            return {"window_days": days, "total_events": 0, "active_domains": 0, "by_type": {}}

    @staticmethod
    def _fmt_row(r: sqlite3.Row) -> dict:
        return {
            "domain": r["domain"],
            "hits": r["hits"],
            "last_access": _to_local(r["last_access"]) if r["last_access"] else None,
            "avg_score": round(r["avg_score"], 4) if r["avg_score"] is not None else None,
        }


def _to_local(iso_utc: str | None) -> str | None:
    """UTC ISO -> Beijing time 'YYYY-MM-DD HH:MM:SS'."""
    if not iso_utc:
        return None
    try:
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_utc

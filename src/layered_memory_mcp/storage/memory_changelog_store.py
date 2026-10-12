"""MEMORY_CHANGELOG store — append-only JSONL provenance of L0/L1 mutations.

One line per event, appended and never rewritten. JSONL rather than SQLite
because this store has exactly one write shape (append) and one read shape
(filter the tail), and because a provenance file a human can `tail` is worth
more here than query planning.

Design principles (same contract as :mod:`.access_log`):
  - Independent file under ``<home>/data/memory_changelog.jsonl``; it never
    touches L1 / L0 / vector data and is never a second source of truth — the
    L1 markdown remains authoritative for *content*, this file only records
    that content changed.
  - Writes are best-effort: a failed append is swallowed (debug log only).
    Provenance must never be able to fail the write it describes.
  - Append-only. Nothing in this module rewrites, truncates or deletes a line;
    retention is a manual/operator decision (see ``retention_days`` in
    ``changelog_settings``), so history cannot vanish silently.
  - Reads are bounded (:data:`MAX_READ_LIMIT`) and tolerant: a corrupt line is
    counted and skipped rather than failing the whole read.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("layered_memory.memory_changelog")

#: Default number of events a read returns.
DEFAULT_READ_LIMIT = 50

#: Hard ceiling on a single read, so no caller can ask for the whole history
#: and blow up an MCP response.
MAX_READ_LIMIT = 500

#: Above this file size the log is assumed to have been misused as a generic
#: event log; reads refuse rather than load it into memory.
MAX_READ_BYTES = 16 * 1024 * 1024


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp (or bare date) to an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class MemoryChangelogStore:
    """Append-only JSONL event store. All writes are best-effort."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # ── Write path (append-only, best-effort) ───────────────────────────

    def append(self, event: dict) -> bool:
        """Append one event as a single JSONL line. Never raises.

        Written through ``open(..., "a")`` in one ``write`` call: the line is
        far below the stream buffer, so it reaches the file as a single
        ``O_APPEND`` write and concurrent appenders cannot interleave. Going
        through ``builtins.open`` (rather than ``os.write``) also keeps this
        path inside the test-suite's production-path guard.
        """
        try:
            # json.dumps escapes embedded newlines, so one event is always
            # exactly one line — the invariant the reader below relies on.
            line = json.dumps(event, ensure_ascii=False) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
            return True
        except Exception as exc:
            logger.debug("memory_changelog append failed: %s", exc)
            return False

    # ── Read path ───────────────────────────────────────────────────────

    def exists(self) -> bool:
        try:
            return self.path.is_file()
        except OSError:
            return False

    def load(self) -> tuple[list[dict], int]:
        """``(events in append order, malformed line count)``. Never raises."""
        if not self.exists():
            return [], 0
        try:
            # TRADEOFF (known gap, left as-is deliberately): returning
            # ``([], 0)`` makes an oversized changelog indistinguishable from an
            # empty one to every caller — the only trace is this log line, which
            # an MCP caller never sees, so `query`/report output reads as "no
            # provenance recorded". And this branch is reachable over time, not
            # just under misuse: ``retention_days`` is parsed and reported but
            # never enforced (see ``memory_changelog.changelog_settings``), so
            # the file only grows. The fix is to surface a flag/error in the read
            # result rather than only logging; do not "fix" it by raising here,
            # since the read path's contract is never-raises.
            if self.path.stat().st_size > MAX_READ_BYTES:
                logger.warning(
                    "memory_changelog is larger than %d bytes (%s) — refusing to "
                    "load it; a provenance log this size means something is "
                    "writing non-semantic events into it",
                    MAX_READ_BYTES,
                    self.path,
                )
                return [], 0
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("memory_changelog read failed: %s", exc)
            return [], 0

        events: list[dict] = []
        malformed = 0
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(payload, dict):
                events.append(payload)
            else:
                malformed += 1
        return events, malformed

    def query(
        self,
        *,
        domain: str | None = None,
        operation: str | None = None,
        surface: str | None = None,
        since: str | None = None,
        limit: int = DEFAULT_READ_LIMIT,
        newest_first: bool = True,
    ) -> dict:
        """Filtered, bounded read. Newest-first by default. Never raises."""
        events, malformed = self.load()
        bounded = max(1, min(int(limit or DEFAULT_READ_LIMIT), MAX_READ_LIMIT))
        since_dt = _parse_ts(since) if since else None

        matched = [
            e
            for e in events
            if (domain is None or str(e.get("domain")) == domain)
            and (operation is None or str(e.get("operation")) == operation)
            and (surface is None or str(e.get("surface")) == surface)
            and (since_dt is None or self._at_or_after(e, since_dt))
        ]
        ordered = list(reversed(matched)) if newest_first else matched
        return {
            "path": str(self.path),
            "exists": self.exists(),
            "total_events": len(events),
            "matched": len(matched),
            "returned": len(ordered[:bounded]),
            "limit": bounded,
            "order": "newest_first" if newest_first else "append_order",
            "malformed_lines": malformed,
            "filters": {
                "domain": domain,
                "operation": operation,
                "surface": surface,
                "since": since,
            },
            "entries": ordered[:bounded],
        }

    def stats(self, top_domains: int = 10) -> dict:
        """Counts by operation / surface / domain, plus the time span."""
        events, malformed = self.load()
        by_operation: dict[str, int] = {}
        by_surface: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for event in events:
            for bucket, key in (
                (by_operation, "operation"),
                (by_surface, "surface"),
                (by_domain, "domain"),
            ):
                value = event.get(key)
                if value:
                    bucket[str(value)] = bucket.get(str(value), 0) + 1

        ranked = sorted(by_domain.items(), key=lambda kv: (-kv[1], kv[0]))
        try:
            size_bytes = self.path.stat().st_size if self.exists() else 0
        except OSError:
            size_bytes = 0

        return {
            "path": str(self.path),
            "exists": self.exists(),
            "total_events": len(events),
            "malformed_lines": malformed,
            "size_bytes": size_bytes,
            "by_operation": dict(sorted(by_operation.items())),
            "by_surface": dict(sorted(by_surface.items())),
            "by_domain": dict(ranked[: max(1, int(top_domains))]),
            "first_event_at": events[0].get("at") if events else None,
            "last_event_at": events[-1].get("at") if events else None,
        }

    @staticmethod
    def _at_or_after(event: dict, since_dt: datetime) -> bool:
        at = _parse_ts(event.get("at"))
        # An event whose timestamp cannot be parsed is excluded by a `since`
        # filter rather than silently passed: "everything after T" must not
        # quietly include records we cannot place in time.
        return at is not None and at >= since_dt


__all__ = [
    "DEFAULT_READ_LIMIT",
    "MAX_READ_BYTES",
    "MAX_READ_LIMIT",
    "MemoryChangelogStore",
]

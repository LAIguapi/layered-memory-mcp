"""
Session Scanner for Knowledge Compression.

Scans agent session files and extracts summaries for AI-driven knowledge extraction.
Supports Hermes Agent JSON session format, JSONL session format, and generic session files.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .privacy import diagnostic

logger = logging.getLogger("layered_memory_mcp.scanner")

# Keywords that mark a message as carrying a conclusion/decision rather than
# chatter. Shared by the file-dir and state.db readers so both surface the same
# kind of evidence to the extractor.
DECISION_KEYWORDS = (
    "找到根因", "根因", "根本原因", "修复完成", "已修复", "已解决",
    "解决方案", "结论", "决策", "决定", "验证通过", "测试通过",
    "问题确认", "确认", "最终", "总结", "方案", "架构",
    "root cause", "fixed", "solution", "conclusion", "decided",
    "verified", "confirmed", "resolved", "architecture",
)

# Session sources skipped by default when reading the Hermes DB. Repetitive
# cron output can crowd out interactive sessions that carry durable knowledge.
DEFAULT_EXCLUDED_SESSION_SOURCES = ("cron",)

# Session files must be at least 100 bytes (skip config/metadata files)
MIN_SESSION_SIZE = 100
# Maximum individual file size to read (10 MB safety limit)
MAX_SESSION_SIZE = 10 * 1024 * 1024

# JSON files with these names/patterns are NOT session files
JSON_EXCLUDE_NAMES = {
    "package.json", "config.json", "settings.json",
    "tsconfig.json", "manifest.json", "composer.json",
    ".eslintrc.json", "pyproject.json",
    "package-lock.json", "composer.lock",
}
# Filenames containing these substrings are excluded
JSON_EXCLUDE_SUBSTRINGS = ("lock",)


def _is_excluded_json(name: str) -> bool:
    """Check if a JSON filename should be excluded from session scanning."""
    lower = name.lower()
    if lower in JSON_EXCLUDE_NAMES:
        return True
    if lower.startswith("."):
        return True
    for substr in JSON_EXCLUDE_SUBSTRINGS:
        if substr in lower:
            return True
    return False


def find_recent_sessions(sessions_dir: str, days: int = 7) -> list:
    """Find session files modified within the last N days."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    sessions = []

    sdir = Path(sessions_dir)
    if not sdir.exists():
        return sessions

    # Scan JSONL files (always session data)
    for f in sorted(sdir.rglob("*.jsonl")):
        try:
            stat = f.stat()
            if stat.st_size < MIN_SESSION_SIZE or stat.st_size > MAX_SESSION_SIZE:
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                sessions.append({
                    "path": str(f),
                    "mtime": mtime.isoformat(),
                    "size": stat.st_size,
                })
        except Exception:
            diagnostic(logger, "session_stat_failed", level=logging.DEBUG)
            continue

    # Scan JSON files (exclude obvious non-session files)
    for f in sorted(sdir.rglob("*.json")):
        try:
            if _is_excluded_json(f.name):
                continue
            stat = f.stat()
            if stat.st_size < MIN_SESSION_SIZE or stat.st_size > MAX_SESSION_SIZE:
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                sessions.append({
                    "path": str(f),
                    "mtime": mtime.isoformat(),
                    "size": stat.st_size,
                })
        except Exception:
            diagnostic(logger, "session_stat_failed", level=logging.DEBUG)
            continue

    return sorted(sessions, key=lambda x: x["mtime"], reverse=True)


def _parse_messages_from_entry(entry: dict) -> list[dict]:
    """Extract message list from a single JSON entry.

    Handles:
      - Direct message: {"role": "...", "content": "..."}
      - Hermes session dict: {"session_id": "...", "messages": [...]}
      - OpenAI export: {"mapping": {...}} or list of messages
    """
    # If entry has a "messages" key, it's a wrapper (Hermes format)
    if "messages" in entry and isinstance(entry["messages"], list):
        return entry["messages"]

    # If it looks like a message itself (has "role" key)
    if "role" in entry:
        return [entry]

    return []


def _detect_and_parse_file(filepath: str, metadata: list | None = None) -> list[dict]:
    """Parse a session file, auto-detecting format (JSON vs JSONL).

    Returns a list of message dicts: [{"role": "...", "content": "..."}, ...]
    """
    path = Path(filepath)
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        diagnostic(logger, "session_read_failed")
        return []

    # Try JSON (whole-file parse) first — handles Hermes .json sessions
    try:
        data = json.loads(raw)
        if metadata is not None:
            metadata.extend([data] if isinstance(data, dict) else data if isinstance(data, list) else [])
        if isinstance(data, dict):
            messages = _parse_messages_from_entry(data)
            if messages:
                return messages
        elif isinstance(data, list):
            # Could be a list of messages or list of session objects
            all_messages = []
            for item in data:
                if isinstance(item, dict):
                    msgs = _parse_messages_from_entry(item)
                    all_messages.extend(msgs)
            if all_messages:
                return all_messages
    except json.JSONDecodeError:
        pass

    # Fallback: JSONL (line-by-line parse)
    messages = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if isinstance(entry, dict):
                if metadata is not None:
                    metadata.append(entry)
                msgs = _parse_messages_from_entry(entry)
                messages.extend(msgs)
        except json.JSONDecodeError:
            continue

    return messages


def extract_session_summary(filepath: str, max_messages: int = 50, *, messages: list | None = None) -> dict:
    """Extract summary from a session file (auto-detects JSON/JSONL format).

    Uses a "head + tail" sampling strategy to capture both opening context
    and closing conclusions/decisions, which is where knowledge typically
    accumulates in long sessions.

    Returns:
        {
            "path": str,
            "user_messages": [str],
            "assistant_topics": [str],
            "tool_calls": [str],
            "key_decisions": [str],      # NEW: conclusions, fixes, decisions
            "truncated": bool (optional)
        }
    """
    result = {
        "path": filepath,
        "user_messages": [],
        "assistant_topics": [],
        "tool_calls": [],
        "key_decisions": [],
    }

    if messages is None:
        messages = _detect_and_parse_file(filepath)

    # Strategy: head + tail sampling for long sessions
    if len(messages) > max_messages:
        result["truncated"] = True
        head_count = max_messages // 2  # First half: context
        tail_count = max_messages - head_count  # Second half: conclusions
        sampled = messages[:head_count] + messages[-tail_count:]
    else:
        sampled = messages

    seen_topics = set()
    seen_decisions = set()

    for entry in sampled:
        role = entry.get("role", "")
        content = entry.get("content", "")

        if role == "user" and content and len(content) < 500:
            result["user_messages"].append(content[:200])
        elif role == "assistant" and content:
            text = content[:200] if isinstance(content, str) else str(content)[:200]
            if text:
                # Deduplicate topics
                topic_key = text[:50]
                if topic_key not in seen_topics:
                    seen_topics.add(topic_key)
                    result["assistant_topics"].append(text)

                # Extract key decisions/conclusions (longer content, up to 400 chars)
                content_lower = content.lower() if isinstance(content, str) else str(content).lower()
                if any(kw in content_lower for kw in DECISION_KEYWORDS):
                    decision_text = content[:400] if isinstance(content, str) else str(content)[:400]
                    decision_key = decision_text[:80]
                    if decision_key not in seen_decisions:
                        seen_decisions.add(decision_key)
                        result["key_decisions"].append(decision_text)

        # Extract tool call names
        for tc in entry.get("tool_calls", [])[:3]:
            fn = tc.get("function", {}).get("name", "")
            if fn:
                result["tool_calls"].append(fn)

    return result


def default_hermes_db() -> Path | None:
    """Path of the agent's live session database (env override honoured).

    The location comes from the environment adapter — this module owns the
    SQL it runs against that database, not the path it lives at. ``None`` means
    the resolved adapter has no such database, which callers already handle as
    "no live-session source".
    """
    env = os.environ.get("LAYERED_MEMORY_HERMES_DB")
    if env:
        return Path(env).expanduser()
    from .adapters.environment import resolve_environment

    return resolve_environment().detect_state_db()


def _summarize_db_session(conn: sqlite3.Connection, row, max_messages: int = 50) -> dict:
    """Build one summary dict from a `sessions` row + its `messages` rows.

    Mirrors :func:`extract_session_summary`'s shape (``user_messages`` /
    ``assistant_topics`` / ``tool_calls`` / ``key_decisions``) so callers do not
    care which source produced it, and adds the session metadata only the DB has.
    """
    session_id = row["id"]
    summary: dict = {
        # No file on disk → synthetic, still stable and debuggable.
        "path": f"state.db#{session_id}",
        "session_id": session_id,
        "title": row["title"] or "",
        "source": row["source"] or "",
        "user_messages": [],
        "assistant_topics": [],
        "tool_calls": [],
        "key_decisions": [],
    }
    started = row["started_at"] or 0
    last_seen = row["last_activity_at"] or started
    summary["mtime"] = float(last_seen or 0)
    summary["date"] = (
        datetime.fromtimestamp(float(started), tz=timezone.utc).isoformat() if started else ""
    )
    summary["size"] = 0  # file-size equivalent does not exist for DB rows

    try:
        rows = conn.execute(
            "SELECT * FROM messages "
            "WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        diagnostic(logger, "session_messages_read_failed")
        rows = []

    if len(rows) > max_messages:
        summary["truncated"] = True
        head = max_messages // 2
        sampled = list(rows[:head]) + list(rows[-max_messages + head:])
    else:
        sampled = list(rows)

    summary["message_count"] = len(rows)

    seen_topics: set[str] = set()
    seen_decisions: set[str] = set()
    for msg in sampled:
        role = (msg["role"] or "").strip()
        raw = msg["content"]
        content = raw if isinstance(raw, str) else ("" if raw is None else str(raw))

        if role == "user" and content and len(content) < 500:
            summary["user_messages"].append(content[:200])
        elif role == "assistant" and content:
            text = content[:200]
            topic_key = text[:50]
            if topic_key not in seen_topics:
                seen_topics.add(topic_key)
                summary["assistant_topics"].append(text)
            lowered = content.lower()
            if any(kw in lowered for kw in DECISION_KEYWORDS):
                decision = content[:400]
                if decision[:80] not in seen_decisions:
                    seen_decisions.add(decision[:80])
                    summary["key_decisions"].append(decision)

        raw_calls = msg["tool_calls"] if "tool_calls" in msg.keys() else None
        if raw_calls:
            try:
                calls = json.loads(raw_calls) if isinstance(raw_calls, str) else raw_calls
            except json.JSONDecodeError:
                calls = []
            if isinstance(calls, list):
                for call in calls[:3]:
                    if isinstance(call, dict):
                        name = (call.get("function") or {}).get("name") or call.get("name") or ""
                        if name:
                            summary["tool_calls"].append(name)

    return summary


def _exclusion_policy(exclude_markers, exclude_sources):
    markers = [str(marker).lower() for marker in (exclude_markers or []) if str(marker).strip()]
    sources = {str(source).strip().lower() for source in (
        exclude_sources if exclude_sources is not None else DEFAULT_EXCLUDED_SESSION_SOURCES
    ) if str(source).strip()}
    return markers, sources


def _decoded_session_fields(value):
    yield value
    if isinstance(value, dict):
        for field in value.values():
            yield from _decoded_session_fields(field)
    elif isinstance(value, list):
        for field in value:
            yield from _decoded_session_fields(field)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return
        yield from _decoded_session_fields(decoded)


def _session_exclusion(metadata, messages, markers, sources):
    matched = set()
    for value in _decoded_session_fields([metadata, messages]):
        if isinstance(value, dict) and str(value.get("source") or "").strip().lower() in sources:
            return "source", None
        if isinstance(value, str):
            lowered = value.lower()
            matched.update(marker for marker in markers if marker in lowered)
    for marker in markers:
        if marker in matched:
            return "marker", marker
    return None, None


def _count_exclusion(stats, reason, marker):
    if reason == "source":
        stats["skipped_sources"] += 1
    else:
        stats["excluded_sessions"] += 1
        if marker is not None:
            counts = stats.setdefault("excluded_by_marker", {})
            counts[marker] = counts.get(marker, 0) + 1


def read_state_db_sessions(
    db_path: str | Path | None = None,
    days: int = 3,
    max_sessions: int = 10,
    exclude_markers: Iterable[str] | None = None,
    exclude_sources: Iterable[str] | None = None,
    max_messages: int = 50,
    _excluded_session_ids: set | None = None,
) -> dict:
    """Read recent sessions straight out of Hermes' ``state.db``.

    Hermes stores current sessions in the database rather than the legacy
    request-dump directory. Scanning only that directory can miss recent
    sessions and leave the extraction pipeline without current input.

    Two filters, both deliberate:

    * ``exclude_sources`` (default ``("cron",)``) drops scheduled-job sessions.
      Repetitive job output can otherwise crowd out interactive sessions.
    * ``exclude_markers`` drops any session whose title/messages contain one of
      the given substrings (case-insensitive). This is a **privacy** control, not
      housekeeping: the caller is commonly a model running outside this machine,
      and an operator needs a way to keep selected work out of that export. The
      package ships an empty default — no assumptions about anyone's work.

    Excluded sessions are counted in ``stats``, never silently dropped, so the
    caller can say "N sessions held back" instead of pretending they never
    existed.

    Returns the same envelope as :func:`scan_sessions`.
    """
    path = Path(db_path).expanduser() if db_path else default_hermes_db()
    markers, sources = _exclusion_policy(exclude_markers, exclude_sources)

    output: dict = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "scan_days": days,
        "source": "hermes_state_db",
        "db_path": str(path) if path else None,
        "total_sessions": 0,
        "sessions": [],
        "stats": {
            "rows_in_window": 0,
            "skipped_sources": 0,
            "excluded_sessions": 0,
            "excluded_by_marker": {},
            "scanned": 0,
        },
    }

    if path is None or not path.is_file():
        output["error"] = "state db not found"
        return output

    cutoff = time.time() - days * 86400
    try:
        # Read-only URI: never touch a live agent's DB with a writable handle.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        output["error"] = diagnostic(logger, "session_db_open_failed")
        return output

    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE COALESCE(last_activity_at, started_at) >= ? "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC",
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            output["error"] = diagnostic(logger, "session_db_schema_invalid")
            return output

        output["stats"]["rows_in_window"] = len(rows)
        output["total_sessions"] = len(rows)

        for row in rows:
            reason, marker = _state_session_exclusion(conn, row, markers, sources)
            if reason:
                _count_exclusion(output["stats"], reason, marker)
                if _excluded_session_ids is not None:
                    _excluded_session_ids.add(str(row["id"]))
                continue
            if len(output["sessions"]) >= max_sessions:
                continue
            summary = _summarize_db_session(conn, row, max_messages=max_messages)
            output["sessions"].append(summary)

        output["stats"]["scanned"] = len(output["sessions"])
        return output
    except Exception:
        output["sessions"] = []
        output["stats"]["scanned"] = 0
        output["error"] = diagnostic(logger, "session_db_read_failed")
        return output
    finally:
        conn.close()


def _state_session_exclusion(conn, session, markers, sources):
    reason, marker = _session_exclusion([dict(session)], [], markers, sources)
    if reason is not None or not (markers or sources):
        return reason, marker
    rows = conn.execute("SELECT * FROM messages WHERE session_id=?", (session["id"],)).fetchall()
    return _session_exclusion([], [dict(row) for row in rows], markers, sources)


def search_state_db_sessions(
    keyword: str,
    db_path: str | Path | None = None,
    days: int = 7,
    max_results: int = 5,
    exclude_markers: Iterable[str] | None = None,
    exclude_sources: Iterable[str] | None = None,
    per_session_limit: int = 3,
    *,
    _excluded_session_ids: set[str] | None = None,
) -> dict:
    """Keyword search across live sessions in Hermes' ``state.db``.

    Same source policy as :func:`read_state_db_sessions` — cron sessions skipped,
    operator-configured markers excluded (and counted) rather than exported.
    """
    path = Path(db_path).expanduser() if db_path else default_hermes_db()
    markers, sources = _exclusion_policy(exclude_markers, exclude_sources)

    out: dict = {
        "success": True,
        "keyword": keyword,
        "scan_days": days,
        "source": "hermes_state_db",
        "total_sessions": 0,
        "matched_sessions": 0,
        "matches": [],
        "stats": {"rows_in_window": 0, "skipped_sources": 0, "excluded_sessions": 0},
    }
    if path is None or not path.is_file():
        out["success"] = False
        out["error"] = "state db not found"
        return out

    cutoff = time.time() - days * 86400
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:
        out.update(success=False, error=diagnostic(logger, "session_db_open_failed"))
        return out
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM sessions "
            "WHERE COALESCE(last_activity_at, started_at) >= ? "
            "ORDER BY COALESCE(last_activity_at, started_at) DESC",
            (cutoff,),
        ).fetchall()
        out["stats"]["rows_in_window"] = len(rows)
        out["total_sessions"] = len(rows)

        for row in rows:
            reason, marker = _state_session_exclusion(conn, row, markers, sources)
            if reason:
                _count_exclusion(out["stats"], reason, marker)
                if _excluded_session_ids is not None:
                    _excluded_session_ids.add(str(row["id"]))
                continue

            if len(out["matches"]) >= max_results:
                continue
            hits = conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? AND content LIKE ? "
                "ORDER BY id LIMIT ?",
                (row["id"], f"%{keyword}%", per_session_limit * 2),
            ).fetchall()
            if not hits:
                continue

            user_hits, assistant_hits = [], []
            for hit in hits:
                text = hit["content"] if isinstance(hit["content"], str) else str(hit["content"] or "")
                if (hit["role"] or "") == "user":
                    user_hits.append(text[:200])
                elif (hit["role"] or "") == "assistant":
                    assistant_hits.append(text[:200])

            out["matches"].append({
                "session_id": row["id"],
                "title": row["title"] or "",
                "mtime": float(row["last_activity_at"] or row["started_at"] or 0),
                "matched_user_messages": user_hits[:per_session_limit],
                "matched_assistant_topics": assistant_hits[:per_session_limit],
            })

        out["matched_sessions"] = len(out["matches"])
        return out
    except Exception:
        out.update(success=False, error=diagnostic(logger, "session_db_search_failed"), matches=[], matched_sessions=0)
        return out
    finally:
        conn.close()


def scan_sessions(
    sessions_dir: str | None,
    days: int = 3,
    max_sessions: int = 10,
    hermes_db_path: str | Path | None = None,
    exclude_markers: Iterable[str] | None = None,
    exclude_sources: Iterable[str] | None = None,
    prefer_state_db: bool = True,
    *,
    excluded_session_ids: Iterable[str] | None = None,
) -> dict:
    """Scan recent sessions and return summaries.

    Source order: Hermes' ``state.db`` first (that is where live sessions are),
    then the session-file directory. A DB that exists but yields nothing usable
    (empty window, or every row filtered out) falls through to the directory so
    file-based setups keep working unchanged.

    Returns:
        {
            "generated_at": str,
            "scan_days": int,
            "source": "hermes_state_db" | "session_dir" | "none",
            "total_sessions": int,
            "sessions": [dict],
            "stats": dict
        }
    """
    blocked_ids: set[str] = set(excluded_session_ids or ())
    markers, sources = _exclusion_policy(exclude_markers, exclude_sources)
    if prefer_state_db:
        db = Path(hermes_db_path).expanduser() if hermes_db_path else default_hermes_db()
        if db is not None and db.is_file():
            result = read_state_db_sessions(
                db,
                days=days,
                max_sessions=max_sessions,
                exclude_markers=exclude_markers,
                exclude_sources=exclude_sources,
                _excluded_session_ids=blocked_ids,
            )
            if result["sessions"] or result.get("error"):
                return result
            logger.info("session_db_empty_or_filtered_fallback")

    if not sessions_dir:
        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "scan_days": days,
            "source": "none",
            "total_sessions": 0,
            "sessions": [],
            "stats": {"scanned": 0},
        }

    sessions = find_recent_sessions(sessions_dir, days)

    output = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "scan_days": days,
        "source": "session_dir",
        "total_sessions": len(sessions),
        "sessions": [],
        "stats": {"scanned": 0, "excluded_sessions": 0, "skipped_sources": 0},
    }

    for s in sessions:
        metadata = []
        messages = _detect_and_parse_file(s["path"], metadata)
        reason, marker = _session_exclusion(metadata, messages, markers, sources)
        identities = {Path(s["path"]).stem}
        for record in _decoded_session_fields(metadata):
            if isinstance(record, dict):
                identities.update(str(record[key]) for key in ("id", "session_id") if key in record)
        if reason is None and identities.intersection(blocked_ids):
            reason = "blocked_session"
        if reason:
            _count_exclusion(output["stats"], reason, marker)
            continue
        if len(output["sessions"]) >= max_sessions:
            continue
        summary = extract_session_summary(s["path"], messages=messages)
        summary["mtime"] = s["mtime"]
        summary["size"] = s["size"]
        output["sessions"].append(summary)

    output["stats"]["scanned"] = len(output["sessions"])
    return output

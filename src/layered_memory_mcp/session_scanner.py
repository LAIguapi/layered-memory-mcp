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

# Session sources skipped by default when reading the Hermes DB. Cron runs
# dominate the table (field-tested: 5763 of 6607 sessions on one host, 12 of the
# last 3 days' 137), and they are mostly repetitive job output — scanning them
# would crowd out the interactive sessions that actually carry knowledge.
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
        except Exception as e:
            logger.debug("Skipping jsonl file %s: %s", f, e)
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
        except Exception as e:
            logger.debug("Skipping json file %s: %s", f, e)
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


def _detect_and_parse_file(filepath: str) -> list[dict]:
    """Parse a session file, auto-detecting format (JSON vs JSONL).

    Returns a list of message dicts: [{"role": "...", "content": "..."}, ...]
    """
    path = Path(filepath)
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Cannot read file %s: %s", filepath, e)
        return []

    # Try JSON (whole-file parse) first — handles Hermes .json sessions
    try:
        data = json.loads(raw)
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
                msgs = _parse_messages_from_entry(entry)
                messages.extend(msgs)
        except json.JSONDecodeError:
            continue

    return messages


def extract_session_summary(filepath: str, max_messages: int = 50) -> dict:
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


def default_hermes_db() -> Path:
    """Path of Hermes' live session database (env override honoured)."""
    env = os.environ.get("LAYERED_MEMORY_HERMES_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes" / "state.db"


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
            "SELECT role, content, tool_calls FROM messages "
            "WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    except sqlite3.Error as exc:  # a partially-written session must not kill the scan
        logger.warning("state.db: cannot read messages for %s: %s", session_id, exc)
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

        raw_calls = msg["tool_calls"]
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


def read_state_db_sessions(
    db_path: str | Path | None = None,
    days: int = 3,
    max_sessions: int = 10,
    exclude_markers: Iterable[str] | None = None,
    exclude_sources: Iterable[str] | None = None,
    max_messages: int = 50,
) -> dict:
    """Read recent sessions straight out of Hermes' ``state.db``.

    Why this exists: the session-file directory this module was written for now
    only holds stale request dumps, so a directory scan reports zero sessions
    while the agent has been running for weeks — the whole extraction pipeline
    silently degrades to "nothing new".

    Two filters, both deliberate:

    * ``exclude_sources`` (default ``("cron",)``) drops scheduled-job sessions.
      They dominate the table and carry almost no durable knowledge.
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
    markers = [str(m).lower() for m in (exclude_markers or []) if str(m).strip()]
    sources = {str(s).strip().lower() for s in (
        exclude_sources if exclude_sources is not None else DEFAULT_EXCLUDED_SESSION_SOURCES
    ) if str(s).strip()}

    output: dict = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "scan_days": days,
        "source": "hermes_state_db",
        "db_path": str(path),
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

    if not path.is_file():
        output["error"] = f"state db not found: {path}"
        return output

    cutoff = time.time() - days * 86400
    try:
        # Read-only URI: never touch a live agent's DB with a writable handle.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        output["error"] = f"cannot open state db: {exc}"
        return output

    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, title, source, started_at, last_activity_at, message_count "
                "FROM sessions WHERE COALESCE(last_activity_at, started_at) >= ? "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC",
                (cutoff,),
            ).fetchall()
        except sqlite3.Error as exc:
            output["error"] = f"state db schema not recognised: {exc}"
            return output

        output["stats"]["rows_in_window"] = len(rows)

        for row in rows:
            if (row["source"] or "").strip().lower() in sources:
                output["stats"]["skipped_sources"] += 1
                continue

            # Marker check runs against the DB, not against the (sampled and
            # truncated) summary: a marker buried in a long message would sail
            # through an excerpt-based check, which is exactly the kind of
            # silent under-filtering a privacy control must not have.
            if markers:
                title_lower = (row["title"] or "").lower()
                hit = next((m for m in markers if m in title_lower), None)
                if hit is None:
                    hit = _session_hits_marker(conn, row["id"], markers)
                if hit:
                    output["stats"]["excluded_sessions"] += 1
                    by_marker = output["stats"]["excluded_by_marker"]
                    by_marker[hit] = by_marker.get(hit, 0) + 1
                    continue

            summary = _summarize_db_session(conn, row, max_messages=max_messages)
            output["sessions"].append(summary)
            if len(output["sessions"]) >= max_sessions:
                break

        output["stats"]["scanned"] = len(output["sessions"])
        output["total_sessions"] = len(output["sessions"])
        return output
    finally:
        conn.close()


def _session_hits_marker(conn: sqlite3.Connection, session_id: str, markers: list[str]) -> str | None:
    """Return the first marker present anywhere in the session, else None."""
    for marker in markers:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE session_id=? AND content LIKE ? LIMIT 1",
            (session_id, f"%{marker}%"),
        ).fetchone()
        if row:
            return marker
    return None


def search_state_db_sessions(
    keyword: str,
    db_path: str | Path | None = None,
    days: int = 7,
    max_results: int = 5,
    exclude_markers: Iterable[str] | None = None,
    exclude_sources: Iterable[str] | None = None,
    per_session_limit: int = 3,
) -> dict:
    """Keyword search across live sessions in Hermes' ``state.db``.

    Same source policy as :func:`read_state_db_sessions` — cron sessions skipped,
    operator-configured markers excluded (and counted) rather than exported.
    """
    path = Path(db_path).expanduser() if db_path else default_hermes_db()
    markers = [str(m).lower() for m in (exclude_markers or []) if str(m).strip()]
    sources = {str(s).strip().lower() for s in (
        exclude_sources if exclude_sources is not None else DEFAULT_EXCLUDED_SESSION_SOURCES
    ) if str(s).strip()}

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
    if not path.is_file():
        out["success"] = False
        out["error"] = f"state db not found: {path}"
        return out

    cutoff = time.time() - days * 86400
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, source, started_at, last_activity_at FROM sessions "
            "WHERE COALESCE(last_activity_at, started_at) >= ? "
            "ORDER BY COALESCE(last_activity_at, started_at) DESC",
            (cutoff,),
        ).fetchall()
        out["stats"]["rows_in_window"] = len(rows)
        out["total_sessions"] = len(rows)

        for row in rows:
            if (row["source"] or "").strip().lower() in sources:
                out["stats"]["skipped_sources"] += 1
                continue
            if markers and (row["title"] and any(m in row["title"].lower() for m in markers)
                            or _session_hits_marker(conn, row["id"], markers)):
                out["stats"]["excluded_sessions"] += 1
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
            if len(out["matches"]) >= max_results:
                break

        out["matched_sessions"] = len(out["matches"])
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
    if prefer_state_db:
        db = Path(hermes_db_path).expanduser() if hermes_db_path else default_hermes_db()
        if db.is_file():
            result = read_state_db_sessions(
                db,
                days=days,
                max_sessions=max_sessions,
                exclude_markers=exclude_markers,
                exclude_sources=exclude_sources,
            )
            if result["sessions"]:
                return result
            logger.info(
                "state.db gave no usable sessions (stats=%s); falling back to %s",
                result.get("stats"), sessions_dir,
            )

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
        "stats": {"scanned": 0},
    }

    for s in sessions[:max_sessions]:
        summary = extract_session_summary(s["path"])
        summary["mtime"] = s["mtime"]
        summary["size"] = s["size"]
        output["sessions"].append(summary)

    output["stats"]["scanned"] = len(output["sessions"])
    return output

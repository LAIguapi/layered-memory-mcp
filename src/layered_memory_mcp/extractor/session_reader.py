"""Full session reader — reads entire session files without truncation.

Replaces the truncated session_scanner with complete session parsing.
Supports Hermes JSON, JSONL, and generic formats.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("layered_memory_mcp.extractor.reader")

# Session files must be at least 100 bytes
MIN_SESSION_SIZE = 100
MAX_SESSION_SIZE = 10 * 1024 * 1024

# Exclude non-session JSON files
JSON_EXCLUDE_NAMES = {
    "package.json", "config.json", "settings.json",
    "tsconfig.json", "manifest.json", "composer.json",
    ".eslintrc.json", "pyproject.json",
    "package-lock.json", "composer.lock",
}
JSON_EXCLUDE_SUBSTRINGS = ("lock",)


@dataclass
class Message:
    """A single message in a session."""

    role: str
    content: str
    tool_calls: list[dict] | None = None
    timestamp: str | None = None


@dataclass
class Session:
    """A parsed session with full message history."""

    path: str
    session_id: str | None
    messages: list[Message]
    mtime: datetime
    size: int

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def user_messages(self) -> list[Message]:
        return [m for m in self.messages if m.role == "user"]

    @property
    def assistant_messages(self) -> list[Message]:
        return [m for m in self.messages if m.role == "assistant"]

    def get_message_range(self, start: int, end: int) -> list[Message]:
        """Get messages in a range (inclusive start, exclusive end)."""
        return self.messages[start:end]


def _is_excluded_json(name: str) -> bool:
    lower = name.lower()
    if lower in JSON_EXCLUDE_NAMES:
        return True
    if lower.startswith("."):
        return True
    for substr in JSON_EXCLUDE_SUBSTRINGS:
        if substr in lower:
            return True
    return False


def _parse_messages_from_entry(entry: dict) -> list[Message]:
    """Extract messages from a JSON entry."""
    if "messages" in entry and isinstance(entry["messages"], list):
        return [
            Message(
                role=m.get("role", ""),
                content=m.get("content", ""),
                tool_calls=m.get("tool_calls"),
                timestamp=m.get("timestamp"),
            )
            for m in entry["messages"]
        ]

    if "role" in entry:
        return [Message(
            role=entry.get("role", ""),
            content=entry.get("content", ""),
            tool_calls=entry.get("tool_calls"),
            timestamp=entry.get("timestamp"),
        )]

    return []


def _parse_file(filepath: Path) -> list[Message]:
    """Parse a session file into messages."""
    try:
        raw = filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Cannot read %s: %s", filepath, e)
        return []

    # Try JSON first
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            msgs = _parse_messages_from_entry(data)
            if msgs:
                return msgs
        elif isinstance(data, list):
            all_msgs = []
            for item in data:
                if isinstance(item, dict):
                    all_msgs.extend(_parse_messages_from_entry(item))
            if all_msgs:
                return all_msgs
    except json.JSONDecodeError:
        pass

    # Fallback: JSONL
    messages = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if isinstance(entry, dict):
                messages.extend(_parse_messages_from_entry(entry))
        except json.JSONDecodeError:
            continue

    return messages


def find_sessions(sessions_dir: str, days: int = 7) -> list[Session]:
    """Find all session files modified within the last N days.

    Returns complete sessions with ALL messages (no truncation).
    """
    cutoff = datetime.now(tz=timezone.utc) - __import__("datetime").timedelta(days=days)
    sdir = Path(sessions_dir)
    if not sdir.exists():
        return []

    sessions = []

    # Scan JSONL files
    for f in sorted(sdir.rglob("*.jsonl")):
        try:
            stat = f.stat()
            if stat.st_size < MIN_SESSION_SIZE or stat.st_size > MAX_SESSION_SIZE:
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                msgs = _parse_file(f)
                sessions.append(Session(
                    path=str(f),
                    session_id=None,
                    messages=msgs,
                    mtime=mtime,
                    size=stat.st_size,
                ))
        except Exception as e:
            logger.debug("Skipping jsonl %s: %s", f, e)

    # Scan JSON files
    for f in sorted(sdir.rglob("*.json")):
        try:
            if _is_excluded_json(f.name):
                continue
            stat = f.stat()
            if stat.st_size < MIN_SESSION_SIZE or stat.st_size > MAX_SESSION_SIZE:
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                msgs = _parse_file(f)
                sessions.append(Session(
                    path=str(f),
                    session_id=f.stem,
                    messages=msgs,
                    mtime=mtime,
                    size=stat.st_size,
                ))
        except Exception as e:
            logger.debug("Skipping json %s: %s", f, e)

    return sorted(sessions, key=lambda s: s.mtime, reverse=True)


class SessionReader:
    """High-level interface for reading sessions."""

    def __init__(self, sessions_dir: str):
        self.sessions_dir = Path(sessions_dir)

    def read_recent(self, days: int = 3, max_sessions: int | None = None) -> list[Session]:
        """Read recent sessions, optionally limited to max_sessions."""
        sessions = find_sessions(str(self.sessions_dir), days)
        if max_sessions:
            sessions = sessions[:max_sessions]
        return sessions

    def read_session(self, path: str) -> Session | None:
        """Read a single session file by path."""
        filepath = Path(path)
        if not filepath.exists():
            return None
        try:
            stat = filepath.stat()
            msgs = _parse_file(filepath)
            return Session(
                path=str(filepath),
                session_id=filepath.stem,
                messages=msgs,
                mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                size=stat.st_size,
            )
        except Exception as e:
            logger.warning("Failed to read session %s: %s", path, e)
            return None

    def get_session_stats(self, days: int = 3) -> dict:
        """Get statistics about sessions."""
        sessions = self.read_recent(days)
        total_msgs = sum(s.message_count for s in sessions)
        user_msgs = sum(len(s.user_messages) for s in sessions)
        assistant_msgs = sum(len(s.assistant_messages) for s in sessions)

        return {
            "total_sessions": len(sessions),
            "total_messages": total_msgs,
            "user_messages": user_msgs,
            "assistant_messages": assistant_msgs,
            "avg_messages_per_session": round(total_msgs / len(sessions), 1) if sessions else 0,
            "total_size_mb": round(sum(s.size for s in sessions) / (1024 * 1024), 2),
        }

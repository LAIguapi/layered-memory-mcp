"""
Session Scanner for Knowledge Compression.

Scans agent session files and extracts summaries for AI-driven knowledge extraction.
Supports Hermes Agent JSON session format, JSONL session format, and generic session files.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("layered_memory_mcp.scanner")

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

    # Keywords that indicate knowledge-worthy content
    decision_keywords = [
        "找到根因", "根因", "根本原因", "修复完成", "已修复", "已解决",
        "解决方案", "结论", "决策", "决定", "验证通过", "测试通过",
        "问题确认", "确认", "最终", "总结", "方案", "架构",
        "root cause", "fixed", "solution", "conclusion", "decided",
        "verified", "confirmed", "resolved", "architecture",
    ]

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
                if any(kw in content_lower for kw in decision_keywords):
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


def scan_sessions(sessions_dir: str, days: int = 3, max_sessions: int = 10) -> dict:
    """Scan recent sessions and return summaries.

    Returns:
        {
            "generated_at": str,
            "scan_days": int,
            "total_sessions": int,
            "sessions": [dict]
        }
    """
    sessions = find_recent_sessions(sessions_dir, days)

    output = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "scan_days": days,
        "total_sessions": len(sessions),
        "sessions": [],
    }

    for s in sessions[:max_sessions]:
        summary = extract_session_summary(s["path"])
        summary["mtime"] = s["mtime"]
        summary["size"] = s["size"]
        output["sessions"].append(summary)

    return output

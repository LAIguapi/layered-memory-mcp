"""Content-free diagnostics for privacy-sensitive entry paths."""

import logging
import json
from functools import wraps


def diagnostic(logger: logging.Logger, code: str, *, level: int = logging.WARNING) -> str:
    """Log a caller-owned constant code, never exception or request material."""
    logger.log(level, "%s", code)
    return code


def public_pointer_report(report: object) -> dict:
    """Expose only bounded status and counters from a pointer-write helper."""
    if not isinstance(report, dict):
        return {"action": "error", "category": "pointer_result_invalid"}
    action = report.get("action")
    if not isinstance(action, str) or action not in {
        "added", "replaced", "upserted", "present", "skipped", "error"
    }:
        return {"action": "error", "category": "pointer_result_invalid"}
    category = "pointer_completed"
    if action == "present":
        category = "pointer_unchanged"
    elif action in {"skipped", "error"}:
        category = "pointer_skipped" if action == "skipped" else "pointer_failed"
        reason = report.get("reason")
        if isinstance(reason, str):
            if reason.startswith("read failed:"):
                category = "pointer_read_failed"
            elif reason.startswith("write failed:"):
                category = "pointer_write_failed"
            elif reason == "memory_mode=off":
                category = "pointer_disabled"
            elif reason == "unsafe_auto_detect_write":
                category = "pointer_write_refused"
        if report.get("error") == "knowledge_pointer_write_failed":
            category = "pointer_write_failed"
    result = {"action": action, "category": category}
    counter = report.get("pointers_removed")
    if type(counter) is int and counter >= 0:
        result["pointers_removed"] = counter
    return result


def private_tool(code: str):
    """Keep unexpected exceptions out of public JSON at selected tool seams."""
    def decorate(function):
        @wraps(function)
        async def guarded(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except Exception:
                return json.dumps({"success": False, "error": diagnostic(
                    logging.getLogger(function.__module__), code)})
        return guarded
    return decorate


def private_http(code: str):
    """Bound unexpected failures at selected HTTP read/file entrances."""
    def decorate(function):
        @wraps(function)
        async def guarded(*args, **kwargs):
            from fastapi import HTTPException

            try:
                return await function(*args, **kwargs)
            except HTTPException:
                raise
            except Exception:
                diagnostic(logging.getLogger(function.__module__), code)
                raise HTTPException(status_code=500, detail=code) from None
        return guarded
    return decorate

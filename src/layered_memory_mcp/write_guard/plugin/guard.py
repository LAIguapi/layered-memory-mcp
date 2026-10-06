"""MEMORY.md write-guard policy — the one place the rule lives.

Registered by the bundled Hermes plugin as a ``pre_tool_call`` hook.

Hermes invokes pre_tool_call callbacks with keyword arguments and passes the
model's tool arguments as ``args`` (see the call site in ``hermes_cli/plugins.py``).
Hooks in the wild sometimes read ``tool_input`` instead — a name Hermes never
passes in-process — which makes the hook silently dead. This module accepts
both spellings on purpose: a misnamed keyword must not be able to switch the
guard off unnoticed.

Everything else about the guard (deployment, host config keys, status
reporting) lives in ``write_guard/__init__.py``.
"""

from __future__ import annotations

import os
from pathlib import PurePath
from typing import Any

# The protected file's name and the directory it must sit in. Matching both
# keeps the guard away from unrelated MEMORY.md files elsewhere on disk.
MEMORY_FILE_NAME = "MEMORY.md"
MEMORY_DIR_NAME = "memories"

# Tools that can write the protected file without going through the memory tool.
FILE_EDIT_TOOLS = ("write_file", "patch")

# Runtime escape hatch for hosts that need the guard off without uninstalling
# the plugin; the same variable selects the install policy on the server side.
DISABLE_VALUES = ("off", "0", "false", "no", "disabled")

MEMORY_TOOL_MESSAGE = (
    "MEMORY.md is framework-owned: it holds only the framework-maintained "
    "[L0] knowledge-index line. Store this as L1 knowledge instead — call "
    "inject_knowledge for the matching domain. Long-lived user preferences go "
    "to USER.md (memory tool, target='user')."
)

FILE_EDIT_MESSAGE = (
    "MEMORY.md is framework-owned and must not be edited directly — the "
    "layered-memory framework maintains it. Use inject_knowledge to write the "
    "fact into its L1 domain."
)


def guard_disabled() -> bool:
    """True when the host switched the guard off at runtime."""
    return os.environ.get("LAYERED_MEMORY_WRITE_GUARD", "").strip().lower() in DISABLE_VALUES


def is_agent_memory_file(value: Any) -> bool:
    """True only for an agent ``MEMORY.md`` sitting in a ``memories/`` directory.

    USER.md (the user profile) and any other MEMORY.md on disk are deliberately
    not matched: the guard protects exactly one file.
    """
    if not isinstance(value, (str, PurePath)):
        return False
    text = str(value)
    if not text:
        return False
    try:
        parts = PurePath(text).parts
    except (TypeError, ValueError):
        return False
    if not parts or parts[-1] != MEMORY_FILE_NAME:
        return False
    return MEMORY_DIR_NAME in parts


def _normalize_target(payload: dict) -> str | None:
    """Read the memory tool's ``target``; None when absent or unreadable."""
    target = payload.get("target")
    if target is None:
        return None
    if not isinstance(target, str):
        return None
    return target.strip().lower()


def decide(tool_name: str, args: Any = None, tool_input: Any = None) -> dict | None:
    """Return a Hermes block directive, or None to let the call through.

    ``args`` is what Hermes passes; ``tool_input`` is accepted as a fallback for
    hosts/tests that use the other spelling.
    """
    if guard_disabled():
        return None

    payload = args if isinstance(args, dict) else None
    if payload is None and isinstance(tool_input, dict):
        payload = tool_input

    if tool_name == "memory":
        # Cannot read the arguments => cannot prove this is a USER.md write, so
        # fail closed on the protected store. A well-behaved model always sends
        # an explicit target.
        if not isinstance(payload, dict):
            return {"action": "block", "message": MEMORY_TOOL_MESSAGE}
        target = _normalize_target(payload)
        if target == "user":
            return None
        # Absent target means the memory tool's own default: MEMORY.md.
        return {"action": "block", "message": MEMORY_TOOL_MESSAGE}

    if tool_name in FILE_EDIT_TOOLS:
        if not isinstance(payload, dict):
            return None
        if is_agent_memory_file(payload.get("path")):
            return {"action": "block", "message": FILE_EDIT_MESSAGE}
        return None

    return None


def pre_tool_call(**kwargs: Any) -> dict | None:
    """Hermes pre_tool_call entry point. Never raises, never blocks on error."""
    try:
        return decide(
            str(kwargs.get("tool_name") or ""),
            args=kwargs.get("args"),
            tool_input=kwargs.get("tool_input"),
        )
    except Exception:  # noqa: BLE001 — a broken guard must not break the tool loop
        return None

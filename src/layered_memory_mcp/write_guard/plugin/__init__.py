"""Hermes plugin entry point for the layered-memory MEMORY.md write guard.

Deployed by the framework: ``integrate_agent(action="install_guard")`` copies
this directory into ``<hermes_home>/plugins/layered-memory-guard/`` and enables
it via ``hermes plugins enable``.

The policy itself lives in ``guard.py`` so it can be exercised without Hermes.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Called by Hermes plugin discovery (see plugin.yaml)."""
    from .guard import pre_tool_call

    ctx.register_hook("pre_tool_call", pre_tool_call)

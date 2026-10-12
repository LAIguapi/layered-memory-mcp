"""
Layered Memory MCP Server
=========================

A common-core memory framework that extends AI agent memory beyond token limits.

Framework-owned common core (the framework may write these):
  L0_INDEX         -- knowledge map, injected every turn, pure pointers
  L1_KNOWLEDGE     -- durable domain cognition: facts, decisions, current state
  TODO             -- unfinished state: pending, blocked, needs validation
  MEMORY_CHANGELOG -- provenance of semantic L0/L1 changes, appended to
                      <home>/data/memory_changelog.jsonl

Agent-owned extension surfaces (classified and suggested, not written by
default; opt in through ``agent_extensions`` to let the framework apply them):
  SKILL / USER_PROFILE / AGENT_OTHER

Source-owned evidence:
  Raw sessions    -- scanned and searched, never written

This MCP server provides tools for:
  - Searching L1 knowledge files by keyword
  - Scanning recent sessions for knowledge candidates
  - Classifying what belongs where (``classify_memory_item``) and reflecting
    on what the framework may apply (``get_memory_surface_capabilities``)
  - Reading why the knowledge base changed (``get_memory_changelog``)
  - Memory space statistics and health checks
  - Managing L1 knowledge file CRUD
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("layered-memory-mcp")
except PackageNotFoundError:
    __version__ = "3.4.7"

__all__ = ["__version__"]

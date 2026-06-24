"""
Layered Memory MCP Server
=========================

A 4-tier knowledge architecture that extends AI agent memory beyond token limits.

Tiers:
  L0 -- Index layer (injected every turn, pure pointers)
  L1 -- Knowledge files (loaded on-demand)
  L2 -- Skills/procedures (loaded via skill system)
  L3 -- Raw sessions (searched rarely)

This MCP server provides tools for:
  - Searching L1 knowledge files by keyword
  - Scanning recent sessions for knowledge candidates
  - Memory space statistics and health checks
  - Managing L1 knowledge file CRUD
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("layered-memory-mcp")
except PackageNotFoundError:
    __version__ = "2.9.1"

__all__ = ["__version__"]

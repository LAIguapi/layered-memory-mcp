#!/usr/bin/env python3
"""
Memory Compact — Cron job script for layered-memory-mcp.

Directly calls the MCP server's compact_memory function (bypassing MCP protocol).
This is the "thin wrapper" pattern: cron jobs import core modules directly rather
than going through MCP stdio/HTTP.

Usage (as Hermes cron script):
    python3 memory_compact_cron.py [--dry-run]

Environment:
    LAYERED_MEMORY_HOME         — Knowledge base root (default: ~/.layered-memory)
    LAYERED_MEMORY_SESSIONS_DIR — Session directory (default: ~/.hermes/sessions)
"""

import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Memory compaction cron script")
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't migrate")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    # ── Import MCP core modules (skip MCP protocol layer) ──
    try:
        from layered_memory_mcp.config import MemoryConfig
        from layered_memory_mcp.memory_compactor import compact_memory
    except ImportError as e:
        print(json.dumps({
            "success": False,
            "error": f"layered-memory-mcp not installed: {e}",
            "hint": "Run: pip install -e /path/to/layered-memory-mcp",
        }, ensure_ascii=False, indent=2))
        sys.exit(1)

    # ── Initialize config ──
    home = os.environ.get("LAYERED_MEMORY_HOME")
    sessions_dir = os.environ.get("LAYERED_MEMORY_SESSIONS_DIR")

    config_kwargs = {}
    if home:
        config_kwargs["home"] = Path(home)
    if sessions_dir:
        config_kwargs["sessions_dir"] = Path(sessions_dir)

    config = MemoryConfig(**config_kwargs)

    if args.verbose:
        print(f"Config: home={config.home}, knowledge={config.knowledge_dir}", file=sys.stderr)

    # ── Run compaction ──
    result = compact_memory(config=config, dry_run=args.dry_run)

    # ── Output report ──
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # Exit code: 0 if success, 1 if errors
    if not result.get("success"):
        sys.exit(1)
    if result.get("error_count", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
Layered Memory MCP Server — FastMCP 2.0 Implementation.

A 4-tier knowledge architecture MCP server that extends AI agent memory
beyond token limits. Works with any MCP-compatible agent (Hermes, Claude, etc.).

v0.5.0 — adds smart injection (inject_knowledge), L0 index sync tools,
         auto-sync on write, and knowledge health validation.

Usage:
    # stdio transport (default)
    layered-memory-mcp

    # HTTP transport
    layered-memory-mcp --transport http --port 8080

    # Custom home directory
    LAYERED_MEMORY_HOME=/path/to/data layered-memory-mcp
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

from .config import MemoryConfig
from . import __version__
from .recall import recall, scan_knowledge_files, score_relevance, knowledge_health
from .session_scanner import find_recent_sessions, extract_session_summary, scan_sessions
from .l0_manager import sync_l0_index, auto_sync_if_enabled, manage_entry, check_l0_l1_consistency
from .injector import inject_knowledge

# v2.0 imports
from .models import KnowledgeEntry, KnowledgeType, SourceInfo, SourceType, ReviewItem, ConfidenceScorer
from .models import TodoEntry, TodoStatus, TodoPriority
from .storage import L1Store, VectorStore, ReviewQueue
from .extractor import SessionReader, KnowledgeExtractor
from .todo_store import TodoStore

# ---------------------------------------------------------------------------
# v2.0 Global state
# ---------------------------------------------------------------------------
_v2_stores: dict = {}

def _get_v2_stores():
    """Get or initialize v2.0 storage layer."""
    global _v2_stores
    if not _v2_stores:
        config = _get_config()
        knowledge_dir = config.knowledge_dir
        data_dir = config.home / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _v2_stores["l1"] = L1Store(knowledge_dir)
        _v2_stores["vector"] = VectorStore(data_dir / "vectors.db")
        _v2_stores["review"] = ReviewQueue(data_dir / "review_queue.db")
    return _v2_stores


# ---------------------------------------------------------------------------
# v2.1.0: TODO store
# ---------------------------------------------------------------------------
_todo_store: TodoStore | None = None


def _get_todo_store() -> TodoStore:
    global _todo_store
    if _todo_store is None:
        config = _get_config()
        data_dir = config.home / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _todo_store = TodoStore(data_dir / "todos.db")
    return _todo_store


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONCURRENT_SESSION_SCANS = 10  # Limit parallel file reads for session search

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("layered_memory_mcp")


def _setup_logging(verbose: bool = False):
    """Configure logging for the MCP server."""
    level = logging.DEBUG if verbose else logging.WARNING
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    # Prevent duplicate handlers on repeated calls
    if not logger.handlers:
        logger.addHandler(handler)
    logger.setLevel(level)


# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP("layered-memory", version=__version__)

# Global config (initialized lazily)
_config: MemoryConfig | None = None


def _get_config() -> MemoryConfig:
    """Get or create config singleton."""
    global _config
    if _config is None:
        _config = MemoryConfig()
        logger.info("Initialized config: home=%s, knowledge=%s",
                     _config.home, _config.knowledge_dir)
    return _config


# ---------------------------------------------------------------------------
# Helper: wrap blocking I/O for async contexts
# ---------------------------------------------------------------------------

def _scan_all(config: MemoryConfig) -> dict[str, str]:
    """Scan all knowledge dirs (namespace + shared) and return merged file map."""
    from .recall import scan_knowledge_dirs, scan_knowledge_files
    kdirs = [str(d) for d in config.knowledge_dirs]
    return scan_knowledge_dirs(kdirs) if len(kdirs) > 1 else scan_knowledge_files(kdirs[0])


def _scan_knowledge_sync(knowledge_dirs: list[str]) -> dict:
    """Synchronous helper — scan knowledge files and collect stats across all dirs."""
    from .recall import scan_knowledge_dirs
    files = scan_knowledge_dirs(knowledge_dirs) if len(knowledge_dirs) > 1 else scan_knowledge_files(knowledge_dirs[0])
    file_details = []
    total_size = 0
    for name, path in files.items():
        try:
            size = Path(path).stat().st_size
            total_size += size
            file_details.append({"file": name, "size_bytes": size})
        except OSError as e:
            logger.debug("Cannot stat %s: %s", name, e)
    return files, file_details, total_size


def _read_l0_index_sync(l0_file: Path) -> dict:
    """Synchronous helper — read L0 index file info."""
    l0_content = l0_file.read_text(encoding="utf-8")
    return {
        "configured": True,
        "path": str(l0_file),
        "size_bytes": len(l0_content.encode("utf-8")),
        "lines": len(l0_content.strip().split("\n")),
    }


def _list_sessions_sync(sessions_dir: Path) -> dict:
    """Synchronous helper — count session files."""
    session_files = list(sessions_dir.rglob("*.jsonl")) + list(sessions_dir.rglob("*.json"))
    return {
        "available": True,
        "path": str(sessions_dir),
        "total_files": len(session_files),
    }


def _validate_knowledge_path(config: MemoryConfig, filename: str) -> tuple[Path | None, str | None]:
    """Validate and resolve a knowledge file path across all knowledge dirs.

    Searches namespace dir first, then shared dir. This ensures that
    get/update/delete operations work correctly for files in both directories.

    Checks:
      1. Must end with .md
      2. Must not exceed 255 characters
      3. Must not contain path separators or ..
      4. Must resolve inside one of the configured knowledge_dirs

    Returns (path, error_message):
      - (Path, None) on success
      - (None, error_message) on validation failure
    """
    if not filename.endswith(".md"):
        return None, f"Invalid extension: filename must end with .md, got {filename!r}"
    if len(filename) > 255:
        return None, f"Filename too long ({len(filename)} chars, max 255)"
    if "/" in filename or "\\" in filename or ".." in filename:
        return None, f"Path traversal not allowed: {filename!r}"

    # Check all knowledge directories (namespace first, then shared)
    for kdir in config.knowledge_dirs:
        filepath = kdir / filename
        try:
            filepath.resolve().relative_to(kdir.resolve())
        except ValueError:
            continue  # path traversal — skip this dir
        if filepath.exists():
            return filepath, None

    # File doesn't exist in any dir — return namespace path for create operations
    filepath = config.knowledge_dir / filename
    try:
        filepath.resolve().relative_to(config.knowledge_dir.resolve())
    except ValueError:
        return None, f"Path traversal blocked: {filename!r}"
    return filepath, None


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------

@mcp.resource("memory://status")
async def get_memory_status() -> str:
    """Get overall memory system status and space statistics."""
    config = _get_config()

    # Wrap all blocking I/O
    def _sync_status():
        kdirs = [str(d) for d in config.knowledge_dirs]
        files, file_details, total_l1_size = _scan_knowledge_sync(kdirs)

        l0_info = {"configured": False}
        if config.l0_index_file and config.l0_index_file.exists():
            try:
                l0_info = _read_l0_index_sync(config.l0_index_file)
            except OSError as e:
                logger.warning("Cannot read L0 index: %s", e)

        sessions_info = {"available": False}
        if config.sessions_dir and config.sessions_dir.exists():
            try:
                sessions_info = _list_sessions_sync(config.sessions_dir)
            except OSError as e:
                logger.warning("Cannot scan sessions: %s", e)

        return files, file_details, total_l1_size, l0_info, sessions_info

    files, file_details, total_l1_size, l0_info, sessions_info = \
        await asyncio.to_thread(_sync_status)

    return json.dumps({
        "status": "healthy",
        "home": str(config.home),
        "l1_knowledge": {
            "total_files": len(files),
            "total_size_bytes": total_l1_size,
            "files": file_details,
        },
        "l0_index": l0_info,
        "sessions": sessions_info,
    }, ensure_ascii=False, indent=2)


@mcp.resource("knowledge://files")
async def list_knowledge_files() -> str:
    """List all L1 knowledge files with metadata."""
    config = _get_config()

    def _sync_list():
        files = _scan_all(config)
        file_list = []
        for name, path in files.items():
            try:
                stat = Path(path).stat()
                # Read first line as title hint
                first_line = ""
                with open(path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip().removeprefix("# ").strip()
                file_list.append({
                    "file": name,
                    "size_bytes": stat.st_size,
                    "title_hint": first_line[:100],
                })
            except OSError as e:
                logger.debug("Cannot read %s: %s", name, e)
                file_list.append({"file": name, "error": "cannot read"})
        return files, file_list

    files, file_list = await asyncio.to_thread(_sync_list)

    return json.dumps({
        "knowledge_dirs": [str(d) for d in config.knowledge_dirs],
        "total_files": len(file_list),
        "files": file_list,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# MCP Tools — Read
# ---------------------------------------------------------------------------

@mcp.tool()
async def recall_knowledge(
    keyword: str,
    top_n: int = 5,
    search_mode: str = "keyword",
) -> str:
    """Search L1 knowledge files by keyword with relevance scoring.

    Finds relevant knowledge sections across all markdown files in the
    knowledge directory. Returns matched sections sorted by relevance.

    Args:
        keyword: Search keyword (supports Chinese and English).
        top_n: Maximum number of files to return (default 5).
        search_mode: Search strategy — "keyword" (exact match, default),
                     "fuzzy" (difflib similarity), "bm25" (TF-IDF ranking),
                     or "hybrid" (keyword + fuzzy combined).

    Returns:
        JSON with matched files, relevance scores, and content sections.
    """
    config = _get_config()
    # v0.6.0: namespace-aware search (namespace dir + shared dir)
    kdirs = [str(d) for d in config.knowledge_dirs]
    result = await asyncio.to_thread(
        recall, keyword, kdirs if len(kdirs) > 1 else kdirs[0], top_n, search_mode
    )

    # v0.6.0: Lightweight staleness check (stdio-safe)
    from .l0_manager import quick_l0_consistency_check
    staleness = await asyncio.to_thread(quick_l0_consistency_check, config)
    if staleness:
        result["l0_staleness_warning"] = staleness

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def scan_recent_sessions(
    days: int = 3,
    max_sessions: int = 10,
) -> str:
    """Scan recent agent sessions to identify knowledge extraction candidates.

    Reads session files and extracts user messages, assistant topics,
    and tool call patterns for AI-driven knowledge distillation.

    Args:
        days: Look back N days (default 3).
        max_sessions: Maximum sessions to scan (default 10).

    Returns:
        JSON with session summaries for AI analysis.
    """
    config = _get_config()

    if not config.sessions_dir or not config.sessions_dir.exists():
        return json.dumps({
            "success": False,
            "error": "Sessions directory not configured or not found. "
                     "Set LAYERED_MEMORY_SESSIONS_DIR or ensure ~/.hermes/sessions/ exists.",
        })

    result = await asyncio.to_thread(
        scan_sessions, str(config.sessions_dir), days, max_sessions
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_knowledge_file(filename: str) -> str:
    """Read a specific L1 knowledge file by filename.

    Args:
        filename: Name of the knowledge file (e.g. 'dev-principles.md').

    Returns:
        Full content of the knowledge file.
    """
    config = _get_config()
    filepath, err = _validate_knowledge_path(config, filename)
    if filepath is None:
        return json.dumps({"success": False, "error": err or "Invalid filename"})

    if not filepath.exists() or not filepath.is_file():
        return json.dumps({"success": False, "error": f"File not found: {filename}"})

    try:
        content = await asyncio.to_thread(filepath.read_text, "utf-8")
        return json.dumps({
            "success": True,
            "file": filename,
            "content": content,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to read %s: %s", filename, e)
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def list_memory_stats() -> str:
    """Get detailed memory space statistics and health report.

    Returns L0/L1 space usage, file counts, and optimization suggestions.
    """
    config = _get_config()

    def _sync_stats():
        # v0.6.0: namespace-aware scanning
        files = _scan_all(config)
        total_size = 0
        oversized = []
        file_stats = []

        for name, path in files.items():
            try:
                size = Path(path).stat().st_size
                total_size += size
                is_oversized = size > 2048  # 2KB threshold
                if is_oversized:
                    oversized.append({"file": name, "size_bytes": size})
                file_stats.append({
                    "file": name,
                    "size_bytes": size,
                    "size_kb": round(size / 1024, 1),
                    "oversized": is_oversized,
                })
            except OSError as e:
                logger.debug("Cannot stat %s: %s", name, e)

        return files, total_size, oversized, file_stats

    files, total_size, oversized, file_stats = await asyncio.to_thread(_sync_stats)

    suggestions = []
    if len(files) == 0:
        suggestions.append("Knowledge base is empty — consider adding knowledge files to get started")
    else:
        if len(files) > 15:
            suggestions.append("Consider consolidating L1 files — more than 15 files may reduce scan efficiency")
        if oversized:
            suggestions.append(f"{len(oversized)} file(s) exceed 2KB threshold — consider splitting for faster recall")

    return json.dumps({
        "l1_knowledge": {
            "total_files": len(files),
            "total_size_bytes": total_size,
            "total_size_kb": round(total_size / 1024, 1),
            "avg_size_kb": round(total_size / max(len(files), 1) / 1024, 1) if files else 0,
            "oversized_files": oversized,
            "files": file_stats,
        },
        "suggestions": suggestions,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_sessions_by_keyword(
    keyword: str,
    days: int = 7,
    max_results: int = 5,
) -> str:
    """Search session content for a specific keyword.

    Scans recent session files for messages containing the keyword.

    Args:
        keyword: Keyword to search for in session messages.
        days: Look back N days (default 7).
        max_results: Maximum matching sessions to return (default 5).

    Returns:
        JSON with matching session excerpts.
    """
    config = _get_config()

    if not config.sessions_dir or not config.sessions_dir.exists():
        return json.dumps({"success": False, "error": "Sessions directory not configured"})

    sessions = await asyncio.to_thread(find_recent_sessions, str(config.sessions_dir), days)

    # Limit scan scope: no point reading all sessions when we only need max_results
    scan_limit = max_results * 3
    sessions = sessions[:scan_limit]

    matches: list[dict] = []

    async def _check_session(s: dict) -> dict | None:
        summary = await asyncio.to_thread(extract_session_summary, s["path"])
        keyword_lower = keyword.lower()
        matched_msgs = [m for m in summary.get("user_messages", []) if keyword_lower in m.lower()]
        matched_topics = [t for t in summary.get("assistant_topics", []) if keyword_lower in t.lower()]
        if matched_msgs or matched_topics:
            return {
                "path": s["path"],
                "mtime": s["mtime"],
                "matched_user_messages": matched_msgs[:3],
                "matched_assistant_topics": matched_topics[:3],
            }
        return None

    # Limit concurrency to prevent thread storms
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSION_SCANS)

    async def _limited_check(s: dict) -> dict | None:
        async with semaphore:
            return await _check_session(s)

    tasks = [_limited_check(s) for s in sessions]
    results = await asyncio.gather(*tasks)

    for r in results:
        if r is not None:
            matches.append(r)
            if len(matches) >= max_results:
                break

    return json.dumps({
        "success": True,
        "keyword": keyword,
        "scan_days": days,
        "total_sessions": len(sessions),
        "matched_sessions": len(matches),
        "matches": matches,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# MCP Tools — Write (v0.5.0: auto-sync L0 after writes)
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_knowledge_file(filename: str, content: str) -> str:
    """Create a new L1 knowledge file. Auto-syncs L0 index after write.

    Args:
        filename: Name for the new file (must end with .md).
        content: Markdown content to write.

    Returns:
        JSON with success status and file path.
    """
    config = _get_config()

    filepath, err = _validate_knowledge_path(config, filename)
    if filepath is None:
        return json.dumps({"success": False, "error": err or "Invalid filename"})

    if filepath.exists():
        return json.dumps({"success": False, "error": f"File already exists: {filename}. Use update_knowledge_file instead."})

    try:
        await asyncio.to_thread(filepath.write_text, content, "utf-8")
        logger.info("Created knowledge file: %s (%d bytes)", filename, len(content.encode("utf-8")))

        # v0.5.0: auto-sync L0 index
        sync_report = await asyncio.to_thread(auto_sync_if_enabled, config)

        result = {
            "success": True,
            "action": "created",
            "file": filename,
            "size_bytes": len(content.encode("utf-8")),
            "l0_synced": sync_report is not None,
        }
        if sync_report:
            result["l0_sync"] = sync_report
        return json.dumps(result)
    except Exception as e:
        logger.error("Failed to create %s: %s", filename, e)
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def update_knowledge_file(filename: str, content: str) -> str:
    """Update (overwrite) an existing L1 knowledge file. Auto-syncs L0 index after write.

    Args:
        filename: Name of the existing knowledge file.
        content: New markdown content to write.

    Returns:
        JSON with success status.
    """
    config = _get_config()
    filepath, err = _validate_knowledge_path(config, filename)
    if filepath is None:
        return json.dumps({"success": False, "error": err or "Invalid filename"})

    if not filepath.exists():
        return json.dumps({"success": False, "error": f"File not found: {filename}. Use create_knowledge_file for new files."})

    try:
        old_size = filepath.stat().st_size
        # v0.6.0: Create .bak backup before overwriting
        try:
            old_content = await asyncio.to_thread(filepath.read_text, "utf-8")
            bak_path = filepath.with_suffix(filepath.suffix + ".bak")
            await asyncio.to_thread(bak_path.write_text, old_content, "utf-8")
        except Exception:
            pass  # Non-critical — backup failure shouldn't block updates
        await asyncio.to_thread(filepath.write_text, content, "utf-8")
        new_size = len(content.encode("utf-8"))
        logger.info("Updated knowledge file: %s (%d → %d bytes)", filename, old_size, new_size)

        # v0.5.0: auto-sync L0 index
        sync_report = await asyncio.to_thread(auto_sync_if_enabled, config)

        result = {
            "success": True,
            "action": "updated",
            "file": filename,
            "previous_size_bytes": old_size,
            "new_size_bytes": new_size,
            "l0_synced": sync_report is not None,
        }
        if sync_report:
            result["l0_sync"] = sync_report
        return json.dumps(result)
    except Exception as e:
        logger.error("Failed to update %s: %s", filename, e)
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def delete_knowledge_file(filename: str) -> str:
    """Delete an L1 knowledge file. Auto-removes from L0 index after delete.

    Args:
        filename: Name of the knowledge file to delete.

    Returns:
        JSON with success status.
    """
    config = _get_config()
    filepath, err = _validate_knowledge_path(config, filename)
    if filepath is None:
        return json.dumps({"success": False, "error": err or "Invalid filename"})

    if not filepath.exists():
        return json.dumps({"success": False, "error": f"File not found: {filename}"})

    try:
        old_size = filepath.stat().st_size
        await asyncio.to_thread(filepath.unlink)
        logger.info("Deleted knowledge file: %s (%d bytes)", filename, old_size)

        # Clean up .bak backup if it exists
        bak_path = filepath.with_suffix(filepath.suffix + ".bak")
        try:
            if bak_path.exists():
                await asyncio.to_thread(bak_path.unlink)
                logger.debug("Cleaned up .bak for deleted file: %s", filename)
        except Exception:
            pass  # Non-critical — .bak cleanup failure shouldn't block deletion

        # v0.5.0: auto-sync L0 index (removes deleted file from L0)
        sync_report = await asyncio.to_thread(auto_sync_if_enabled, config)

        result = {
            "success": True,
            "action": "deleted",
            "file": filename,
            "deleted_size_bytes": old_size,
            "l0_synced": sync_report is not None,
        }
        if sync_report:
            result["l0_sync"] = sync_report
        return json.dumps(result)
    except Exception as e:
        logger.error("Failed to delete %s: %s", filename, e)
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# MCP Tools — Write (v0.5.0: new smart injection tools)
# ---------------------------------------------------------------------------

@mcp.tool()
async def inject_knowledge_tool(
    domain: str,
    section: str,
    content: str,
    mode: str = "upsert",
    agent_id: str | None = None,
) -> str:
    """Smart knowledge injection with dedup, section targeting, and auto L0 sync.

    The recommended write path for all agents. Handles deduplication,
    section-level targeting (creates ## headings if needed), and
    automatically syncs the L0 index after successful writes.

    Args:
        domain: Target L1 file (with or without .md), e.g. "infra" or "infra.md".
        section: Target ## heading in the file, e.g. "WSL 代理".
                 Created automatically if it doesn't exist.
        content: Knowledge content to inject (markdown text).
        mode: Write mode — "upsert" (default, replace similar), "append" (always add),
              or "merge" (combine unique parts).
        agent_id: Optional agent identifier for provenance tracking.

    Returns:
        JSON with action taken, dedup info, L0 sync status, and warnings.
    """
    config = _get_config()

    if mode not in ("upsert", "append", "merge"):
        return json.dumps({"success": False, "error": f"Invalid mode: {mode!r}. Must be 'upsert', 'append', or 'merge'."})

    result = await asyncio.to_thread(
        inject_knowledge,
        config=config,
        domain=domain,
        section=section,
        content=content,
        mode=mode,
        agent_id=agent_id,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def batch_inject_knowledge_tool(
    items: list,
    mode: str = "upsert",
    agent_id: str | None = None,
) -> str:
    """Batch knowledge injection — inject multiple knowledge entries in one call.

    Use this when you have analyzed session data (or any source) and want to
    write multiple knowledge entries at once. Much more efficient than calling
    inject_knowledge_tool multiple times.

    Args:
        items: List of knowledge entries, each with keys:
               - domain: Target L1 file (e.g. "infra")
               - section: Target ## heading (e.g. "WSL 代理")
               - content: Knowledge content (markdown text)
        mode: Write mode for ALL items — "upsert" (default), "append", or "merge".
        agent_id: Optional agent identifier for provenance tracking.

    Returns:
        JSON with per-item results, total counts, and any errors.
    """
    config = _get_config()

    if mode not in ("upsert", "append", "merge"):
        return json.dumps({"success": False, "error": f"Invalid mode: {mode!r}. Must be 'upsert', 'append', or 'merge'."})

    results = []
    success_count = 0
    error_count = 0

    for idx, item in enumerate(items):
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except Exception:
                results.append({
                    "index": idx,
                    "success": False,
                    "error": "Item is a string but not valid JSON",
                    "domain": "",
                    "section": "",
                })
                error_count += 1
                continue
        domain = item.get("domain", "")
        section = item.get("section", "")
        content = item.get("content", "")

        if not domain or not content:
            results.append({
                "index": idx,
                "success": False,
                "error": "Missing required field: 'domain' and 'content' are required",
                "domain": domain,
                "section": section,
            })
            error_count += 1
            continue

        try:
            result = inject_knowledge(
                config=config,
                domain=domain,
                section=section,
                content=content,
                mode=mode,
                agent_id=agent_id,
            )
            results.append({
                "index": idx,
                "success": result.get("success", False),
                "domain": domain,
                "section": section,
                "action": result.get("action"),
                "dedup": result.get("dedup"),
                "l0_synced": result.get("l0_synced"),
                "error": result.get("error"),
            })
            if result.get("success"):
                success_count += 1
            else:
                error_count += 1
        except Exception as e:
            results.append({
                "index": idx,
                "success": False,
                "domain": domain,
                "section": section,
                "error": str(e),
            })
            error_count += 1

    return json.dumps({
        "success": error_count == 0,
        "total": len(items),
        "success_count": success_count,
        "error_count": error_count,
        "results": results,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def sync_l0_index_tool(
    format: str = "hermes",
    dry_run: bool = False,
) -> str:
    """Manually sync the L0 index with current L1 knowledge files.

    Regenerates the L0 index by scanning all L1 knowledge files. Useful when:
    - Files were added/modified outside the MCP server
    - L0 index has drifted from L1 reality
    - You want to preview what the index would look like (dry_run=True)

    Args:
        format: L0 format — "hermes" (Hermes agent memory) or "generic" (standalone).
               Default uses the configured format.
        dry_run: If True, returns preview without writing. Default False.

    Returns:
        JSON with sync report including entries added/removed/unchanged.
    """
    config = _get_config()

    # Pass format as parameter — avoids mutating shared config (no race condition)
    effective_format = format or config.l0_format
    result = await asyncio.to_thread(
        sync_l0_index, config, dry_run=dry_run, l0_format=effective_format
    )

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def validate_knowledge(
    filename: str | None = None,
) -> str:
    """Validate L1 knowledge base health and L0-L1 consistency.

    Checks for:
    - Orphaned L1 files (exist but not in L0 index)
    - Stale L0 entries (in L0 but L1 file doesn't exist)
    - File health (size, structure, age)
    - Cross-file duplicates

    Args:
        filename: Optional — check a specific file only. None = full check.

    Returns:
        JSON with health report, issues, and L0-L1 consistency info.
    """
    config = _get_config()

    def _validate():
        report = {
            "overall_health": "good",
            "issues": [],
            "l0_l1_consistency": None,
            "file_reports": [],
        }

        # L0-L1 consistency check
        consistency = check_l0_l1_consistency(config)
        report["l0_l1_consistency"] = consistency

        if consistency["orphaned_l1"]:
            report["issues"].append({
                "severity": "warning",
                "type": "orphaned_l1",
                "files": consistency["orphaned_l1"],
                "message": f"{len(consistency['orphaned_l1'])} L1 file(s) not referenced in L0 index — run sync_l0_index to fix",
            })

        if consistency["stale_l0_entries"]:
            report["issues"].append({
                "severity": "warning",
                "type": "stale_l0",
                "files": consistency["stale_l0_entries"],
                "message": f"{len(consistency['stale_l0_entries'])} L0 entry points to non-existent file — run sync_l0_index to clean",
            })

        # File health check — v0.6.0: namespace-aware
        kdirs = [str(d) for d in config.knowledge_dirs]
        health = knowledge_health(kdirs if len(kdirs) > 1 else kdirs[0])
        report["file_reports"] = health.get("files", [])
        if health.get("issues"):
            for issue in health["issues"]:
                report["issues"].append({
                    "severity": "warning",
                    "type": "health",
                    "message": issue,
                })

        # Determine overall health
        severity_score = 0
        for issue in report["issues"]:
            if issue["severity"] == "error":
                severity_score += 3
            else:
                severity_score += 1

        if severity_score == 0:
            report["overall_health"] = "good"
        elif severity_score <= 3:
            report["overall_health"] = "fair"
        else:
            report["overall_health"] = "poor"

        return report

    result = await asyncio.to_thread(_validate)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def manage_l0_entry_tool(
    action: str,
    domain: str,
    summary: str | None = None,
    filename: str | None = None,
) -> str:
    """Manually add, remove, or replace a single L0 index entry.

    For fine-grained control when you don't want to regenerate the entire
    L0 index. Only works when l0_index_file is configured.

    Args:
        action: "add", "remove", or "replace".
        domain: Domain name for the entry (e.g. "infra").
        summary: Summary text (required for "add" and "replace").
        filename: Linked L1 filename. Auto-derived from domain if omitted.

    Returns:
        JSON with success status and entry count.
    """
    config = _get_config()

    if action not in ("add", "remove", "replace"):
        return json.dumps({"success": False, "error": f"Invalid action: {action!r}"})

    result = await asyncio.to_thread(
        manage_entry,
        config,
        action=action,
        domain=domain,
        summary=summary,
        filename=filename,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# MCP Tools — v2.0: Structured Knowledge Extraction
# ---------------------------------------------------------------------------

@mcp.tool()
async def extract_session_knowledge(
    days: int = 3,
    max_sessions: int = 10,
    auto_approve_threshold: float = 0.9,
    max_items_per_session: int = 5,
) -> str:
    """Extract structured knowledge from recent sessions using v2.0 extractor.

    Replaces scan_recent_sessions + manual analysis with automated,
    structured extraction. Reads FULL sessions (no truncation),
    detects knowledge types, scores confidence, and returns
    ready-to-inject knowledge entries.

    Args:
        days: Look back N days (default 3).
        max_sessions: Maximum sessions to scan (default 10).
        auto_approve_threshold: Confidence threshold for auto-approval (default 0.9).
        max_items_per_session: Max knowledge items per session (default 5).

    Returns:
        JSON with extracted knowledge entries, confidence scores, and review status.
    """
    config = _get_config()

    if not config.sessions_dir or not config.sessions_dir.exists():
        return json.dumps({
            "success": False,
            "error": "Sessions directory not configured or not found.",
        })

    def _extract():
        reader = SessionReader(str(config.sessions_dir))
        sessions = reader.read_recent(days=days, max_sessions=max_sessions)

        extractor = KnowledgeExtractor(auto_approve_threshold=auto_approve_threshold)
        items = extractor.extract_from_sessions(sessions, max_items_per_session=max_items_per_session)

        # Convert to serializable format
        entries = []
        for item in items:
            e = item.entry
            entries.append({
                "id": e.id,
                "domain": e.domain,
                "section": e.section,
                "type": e.type.value,
                "content": e.content,
                "summary": e.summary,
                "confidence": e.confidence,
                "review_status": e.review_status.value,
                "source": {
                    "type": e.source.type.value,
                    "session_id": e.source.session_id,
                    "extracted_by": e.source.extracted_by,
                },
                "tags": e.tags,
            })

        stats = extractor.get_extraction_stats(items)
        return {
            "success": True,
            "sessions_scanned": len(sessions),
            "entries": entries,
            "stats": stats,
        }

    result = await asyncio.to_thread(_extract)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_semantic(
    query: str,
    top_n: int = 5,
    domain: str | None = None,
) -> str:
    """Semantic search for knowledge entries using vector similarity.

    Uses the v2.0 vector store to find conceptually related knowledge,
    even if keywords don't match exactly.

    Args:
        query: Search query string.
        top_n: Number of results to return (default 5).
        domain: Optional domain filter.

    Returns:
        JSON with search results and similarity scores.
    """
    stores = _get_v2_stores()
    results = stores["vector"].search(query, top_n=top_n, domain=domain)
    return json.dumps({
        "success": True,
        "query": query,
        "results": results,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_pending_reviews(
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Get pending knowledge entries awaiting human review.

    Returns low-confidence extractions that need manual approval
    before being committed to the main knowledge base.

    Args:
        limit: Max items to return (default 50).
        offset: Pagination offset (default 0).

    Returns:
        JSON with pending review items.
    """
    stores = _get_v2_stores()
    items = stores["review"].list_pending(limit=limit, offset=offset)
    return json.dumps({
        "success": True,
        "pending_count": len(items),
        "items": [
            {
                "id": item["id"],
                "domain": item["entry"].domain,
                "section": item["entry"].section,
                "type": item["entry"].type.value,
                "summary": item["entry"].summary,
                "confidence": item["entry"].confidence,
                "submitted_at": item["submitted_at"],
            }
            for item in items
        ],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def approve_knowledge(
    entry_id: str,
    reviewer: str = "human",
    note: str = "",
) -> str:
    """Approve a pending knowledge entry.

    Moves the entry from pending to approved status.

    Args:
        entry_id: ID of the knowledge entry to approve.
        reviewer: Name of the reviewer (default "human").
        note: Optional review note.

    Returns:
        JSON with approval result.
    """
    stores = _get_v2_stores()
    result = stores["review"].approve(entry_id, reviewer=reviewer, note=note)

    # Update L1 file status
    if result.get("success"):
        for domain in stores["l1"].list_domains():
            meta, content = stores["l1"].read(domain)
            if meta and meta.get("id") == entry_id:
                from .storage.frontmatter import parse_frontmatter, dump_frontmatter
                entry = KnowledgeEntry.from_markdown(
                    stores["l1"]._resolve_path(domain).read_text(),
                    domain=domain,
                )
                entry.review_status = __import__("layered_memory_mcp.models", fromlist=["ReviewStatus"]).ReviewStatus.APPROVED
                entry.reviewed_by = reviewer
                stores["l1"].write(entry)
                break

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def reject_knowledge(
    entry_id: str,
    reviewer: str = "human",
    note: str = "",
) -> str:
    """Reject a pending knowledge entry.

    Marks the entry as rejected and removes it from the review queue.

    Args:
        entry_id: ID of the knowledge entry to reject.
        reviewer: Name of the reviewer (default "human").
        note: Optional rejection reason.

    Returns:
        JSON with rejection result.
    """
    stores = _get_v2_stores()
    result = stores["review"].reject(entry_id, reviewer=reviewer, note=note)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_knowledge_by_id(
    entry_id: str,
) -> str:
    """Get a knowledge entry by its UUID.

    Args:
        entry_id: UUID of the knowledge entry.

    Returns:
        JSON with the knowledge entry, or error if not found.
    """
    stores = _get_v2_stores()
    for domain in stores["l1"].list_domains():
        meta, content = stores["l1"].read(domain)
        if meta and meta.get("id") == entry_id:
            return json.dumps({
                "success": True,
                "entry": {
                    "id": meta.get("id"),
                    "domain": domain,
                    "section": meta.get("section"),
                    "type": meta.get("type"),
                    "content": content,
                    "summary": meta.get("summary"),
                    "confidence": meta.get("confidence"),
                    "review_status": meta.get("review_status"),
                    "tags": meta.get("tags", []),
                },
            }, ensure_ascii=False, indent=2)

    return json.dumps({
        "success": False,
        "error": f"Entry not found: {entry_id}",
    })


@mcp.tool()
async def get_memory_v2_stats() -> str:
    """Get v2.0 memory system statistics.

    Returns stats about the vector store, review queue, and L1 storage.
    """
    stores = _get_v2_stores()
    return json.dumps({
        "success": True,
        "v2_stats": {
            "vector_store": stores["vector"].stats(),
            "review_queue": stores["review"].get_stats(),
            "l1_domains": stores["l1"].list_domains(),
        },
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# MCP Tools — v0.8.x: Memory Hygiene
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_l0_index() -> str:
    """Retrieve the current L0 index content for injection into agent context.

    This is the agent-agnostic way to access the L0 index. Non-Hermes agents
    (Claude Desktop, Cursor, Codex CLI, etc.) should call this at the start
    of each session to load the index into their working context.

    Hermes Agent users: L0 is already injected via memory — you typically
    don't need this tool. But it's useful for debugging or manual inspection.

    Returns:
        JSON with the full L0 index content, entry count, and format info.
    """
    config = _get_config()

    # First try the configured L0 file
    if config.l0_index_file and config.l0_index_file.exists():
        try:
            content = await asyncio.to_thread(config.l0_index_file.read_text, "utf-8")
            lines = [l for l in content.strip().split("\n") if l.strip() and not l.strip().startswith("#")]
            return json.dumps({
                "success": True,
                "source": "l0_file",
                "format": config.l0_format,
                "total_entries": len(lines),
                "content": content,
            }, ensure_ascii=False)
        except Exception as e:
            logger.warning("Failed to read L0 file: %s", e)

    # Fallback: auto-generate from L1 files
    def _gen():
        from .recall import generate_l0_index
        kdirs = [str(d) for d in config.knowledge_dirs]
        return generate_l0_index(kdirs if len(kdirs) > 1 else kdirs[0])

    generated = await asyncio.to_thread(_gen)
    if not generated:
        return json.dumps({
            "success": True,
            "source": "generated",
            "format": config.l0_format,
            "total_entries": 0,
            "content": "",
            "note": "No L1 knowledge files found — index is empty",
        })

    lines = [l for l in generated.strip().split("\n") if l.strip()]
    return json.dumps({
        "success": True,
        "source": "generated",
        "format": config.l0_format,
        "total_entries": len(lines),
        "content": generated,
        "note": "Auto-generated from L1 files (L0 file not configured or missing)",
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# MCP Prompts
# ---------------------------------------------------------------------------

@mcp.prompt()
def knowledge_compression_prompt() -> str:
    """Prompt template for AI-driven knowledge compression from sessions.

    Use this prompt with scan_recent_sessions output to extract
    durable knowledge from conversations.
    """
    return """You are a knowledge distillation agent. Your job is to scan recent AI agent sessions and extract stable, factual knowledge that should be preserved for future sessions.

## Rules
1. Only extract STABLE facts — things that will remain true across sessions (configurations, conventions, user preferences, environment details)
2. Do NOT extract: temporary debugging state, one-off commands, data that changes frequently
3. Classify each fact by domain (e.g., infrastructure, development, content)
4. Output facts as declarative statements, not instructions
5. Use inject_knowledge for smart writes (handles dedup, L0 sync, section targeting)

## Process
1. First, call `scan_recent_sessions` to get session summaries
2. For each interesting session, call `search_sessions_by_keyword` for deeper context
3. Identify new knowledge not yet in L1 files
4. Write using `inject_knowledge(domain=..., section=..., content=..., mode="upsert")`
   - For new domains: inject_knowledge creates the file + section automatically
   - For existing domains: inject_knowledge deduplicates and appends/merges/replaces

## Output Format
For each piece of knowledge:
- domain: which L1 file it belongs to (or "NEW" for new domains)
- action: "add" or "update"
- content: the factual knowledge as a declarative statement
- source: which session it came from

Be conservative — when in doubt, don't extract. It's better to miss a fact than to pollute the knowledge base with noise."""


@mcp.prompt()
def cognitive_decision_prompt() -> str:
    """Cognitive decision framework for agents using layered memory.

    Inject this prompt into the agent's system prompt to enforce disciplined
    memory usage — consult before acting, learn after solving.
    """
    return """You use a 4-tier layered memory system. Follow this decision tree for EVERY problem or request:

## Decision Tree (MANDATORY)

1. SCAN: Check your L0 index for domains relevant to the current problem.
2. IF MATCHED:
   a. Load the corresponding L1 knowledge file or L2 skill.
   b. Read and understand it BEFORE taking any action.
   c. If the knowledge fully solves the problem → use it. Do NOT bypass with guessing.
   d. If the knowledge partially helps → use what applies, then enhance/update the entry.
   e. If the knowledge is outdated or insufficient → treat as a new problem (step 3).
3. IF NO MATCH: Treat as a new problem. Solve with standard tools and reasoning.
4. AFTER SOLVING: Evaluate — is this worth preserving for future sessions?
   a. If yes → write to L1 (facts/conventions) or create a skill (procedures/workflows).
   b. If no → done.

## Key Principles

- NEVER guess when you have relevant knowledge stored. Load it first.
- The L0 index is a map, not the territory. You must load L1/L2 to actually use the knowledge.
- Every solved problem is a candidate for memory improvement. Be proactive about capturing insights.
- When you discover gaps in existing knowledge, update it immediately — don't wait to be asked."""


# ---------------------------------------------------------------------------
# MCP Tools — v0.8.x: Memory Hygiene
# ---------------------------------------------------------------------------

@mcp.tool()
async def compact_memory(dry_run: bool = True) -> str:
    """Scan agent memory for non-index bloat and migrate to L1 knowledge files.

    Detects entries in the agent's memory that are not L0 index pointers,
    classifies them by domain, and either reports (dry_run) or automatically
    migrates them to appropriate L1 files.

    Args:
        dry_run: If True (default), only report what would be migrated.
                 If False, perform the migration and clean up memory.

    Returns:
        JSON with migration plan or execution report.
    """
    config = _get_config()
    from .memory_compactor import compact_memory as _compact
    result = await asyncio.to_thread(_compact, config=config, dry_run=dry_run)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def init_framework() -> str:
    """Initialize the layered memory framework for first-time use.

    Detects whether the knowledge base is empty (first run), creates a
    welcome file with getting-started guidance, and returns memory
    management rules for the agent to follow.

    Returns:
        JSON with initialization status and memory management rules.
    """
    config = _get_config()

    def _init():
        files = _scan_all(config)
        total = len(files)

        rules = (
            "## Memory Management Rules (Layered Memory MCP)\n\n"
            "1. **L0 is for pointers only**. Each entry should be ~1 line pointing to an L1 file. "
            "No full content, no procedures, no diagnostics.\n"
            "2. **Use inject_knowledge for all writes**. It handles dedup, section targeting, and L0 sync.\n"
            "3. **Run compact_memory when warned**. If inject_knowledge returns a memory_bloat_warning, "
            "call compact_memory(dry_run=True) to inspect, then compact_memory(dry_run=False) to clean.\n"
            "4. **Expand capacity if >90% repeatedly**. If compact doesn't free enough space, "
            "increase your agent's memory/storage limit.\n"
            "5. **User preferences → user profile. Procedural knowledge → skills. "
            "Only durable factual knowledge → L1 with L0 pointer.\n"
        )

        if total == 0:
            # First run: create welcome file
            welcome_content = (
                "# Getting Started\n\n"
                "Welcome to Layered Memory MCP! Your knowledge base is empty.\n\n"
                "## Quick Start\n"
                "1. Use `inject_knowledge(domain='my-domain', section='Topic', content='...')` "
                "to create your first knowledge entry.\n"
                "2. The system will automatically create the L1 file and update the L0 index.\n"
                "3. Use `recall_knowledge(keyword='...')` to search your knowledge base.\n\n"
                "## Architecture\n"
                "- **L0**: Short index pointers (loaded every session)\n"
                "- **L1**: Detailed knowledge files (loaded on demand)\n"
                "- **L2**: Agent skills and procedures\n"
                "- **L3**: Raw session data\n"
            )
            welcome_path = config.knowledge_dir / "getting-started.md"
            welcome_path.write_text(welcome_content, encoding="utf-8")
            auto_sync_if_enabled(config)
            return {
                "success": True,
                "first_run": True,
                "action": "created getting-started.md",
                "rules": rules,
            }

        return {
            "success": True,
            "first_run": False,
            "l1_files_found": total,
            "rules": rules,
        }

    result = await asyncio.to_thread(_init)
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# MCP Tools — v2.1.0: TODO Management
# ---------------------------------------------------------------------------


@mcp.tool()
async def add_todo(domain: str, content: str, title: str = "", blocked_by: str = "[]", priority: str = "medium", notes: str = "", source_session_id: str = "") -> str:
    """Add a new TODO item."""
    import json as _json
    store = _get_todo_store()
    blocked = _json.loads(blocked_by) if isinstance(blocked_by, str) else blocked_by
    entry = TodoEntry(domain=domain, content=content, title=title or content[:50], blocked_by=blocked, priority=TodoPriority(priority), notes=notes, source_session_id=source_session_id or None)
    result = store.add(entry)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_todos(status: str = "", domain: str = "", priority: str = "", limit: int = 50) -> str:
    """List TODOs with optional filtering by status, domain, priority."""
    store = _get_todo_store()
    todos = store.list(status=status or None, domain=domain or None, priority=priority or None, limit=limit)
    return json.dumps({"success": True, "count": len(todos), "todos": todos}, ensure_ascii=False, indent=2)


@mcp.tool()
async def update_todo(todo_id: str, status: str = "", priority: str = "", title: str = "", content: str = "", blocked_by: str = "", notes: str = "") -> str:
    """Update a TODO item's status, priority, content, or notes."""
    import json as _json
    store = _get_todo_store()
    kwargs = {}
    if status:
        kwargs["status"] = status
    if priority:
        kwargs["priority"] = priority
    if title:
        kwargs["title"] = title
    if content:
        kwargs["content"] = content
    if blocked_by:
        kwargs["blocked_by"] = _json.loads(blocked_by) if isinstance(blocked_by, str) else blocked_by
    if notes:
        kwargs["notes"] = notes
    result = store.update(todo_id, **kwargs)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def delete_todo(todo_id: str) -> str:
    """Delete a TODO item by ID."""
    store = _get_todo_store()
    result = store.delete(todo_id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_todo_stats() -> str:
    """Get TODO statistics: counts by status, domain, and priority."""
    store = _get_todo_store()
    stats = store.stats()
    return json.dumps({"success": True, "stats": stats}, ensure_ascii=False, indent=2)


@mcp.prompt()
def memory_rules_prompt() -> str:
    """Memory management rules for agents using layered memory.

    Inject this prompt into the agent's system prompt to enforce
    disciplined memory usage. Works with any MCP-compatible agent.
    """
    return """## Memory Management Rules (Layered Memory MCP)

1. **L0 is for pointers only**. Each entry = 1 line pointing to an L1 file. No full content, no procedures.
2. **Use inject_knowledge for all writes**. It handles dedup, section targeting, and L0 sync automatically.
3. **Run compact_memory when warned**. If inject_knowledge returns a memory_bloat_warning, call compact_memory.
4. **Expand capacity if >90% repeatedly**. If compact doesn't free enough space, increase your memory limit.
5. **User preferences → user profile. Procedural knowledge → skills. Only durable facts → L1 with L0 pointer."""


@mcp.prompt()
def todo_management_prompt() -> str:
    """TODO management rules for agents. Inject into system prompt to enable automatic TODO tracking."""
    return """## TODO Management Rules (Layered Memory MCP)

You have access to TODO management tools through the layered-memory MCP server. Use them to track tasks across sessions.

### When to Auto-Create TODOs

1. **Topic switch without closure** — When the user moves to a new topic but the previous task had no clear resolution or next action, capture it with add_todo.
2. **Explicit deferral** — User says "put this aside", "continue tomorrow", "we'll come back to this" → add_todo immediately.
3. **Recurring open topic** — If the same issue appears in 2+ sessions without resolution, it's a pending TODO.
4. **Blocker identification** — Infrastructure issues blocking multiple other tasks → add_todo with priority=blocker.

### When NOT to Create TODOs

- Single-session tasks that are completed within the same conversation
- Trivial questions with no follow-up needed
- Tasks the user explicitly says are done

### Priority Guidelines

- **blocker**: Infrastructure broken, blocks multiple workstreams (e.g., database down, API key expired)
- **high**: Concrete action item, should be done within a week
- **medium**: Has direction but no urgency
- **low**: Nice-to-have, no deadline
- **waiting**: Waiting for external feedback or dependency

### Usage Pattern

1. At the START of each session, call list_todos(status="pending") to show the user what's outstanding.
2. When starting work on a task, call update_todo(todo_id, status="in_progress").
3. When completing a task, call update_todo(todo_id, status="completed").
4. Periodically call get_todo_stats() to review overall progress.

### Script: Periodic TODO Scan (Optional)

To set up automated weekly scanning:
1. Call extract_session_knowledge(days=7) to get recent session summaries.
2. Analyze sessions for: tasks mentioned but not completed, recurring open topics, explicit "TODO/待做/unresolved" signals.
3. For each detected candidate, call add_todo with the detected domain and priority.
4. Report the scan results to the user with candidates for confirmation."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="Layered Memory MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="HTTP port (default: 8080, only used with --transport http)",
    )
    parser.add_argument(
        "--home",
        type=str,
        default=None,
        help="Home directory for memory data (default: ~/.layered-memory/)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    # Setup logging
    _setup_logging(verbose=args.verbose)

    # Set config: only override if --home is explicitly provided
    global _config
    if args.home:
        os.environ["LAYERED_MEMORY_HOME"] = args.home
        _config = MemoryConfig(home=args.home)
    # Otherwise let _get_config() handle lazy initialization

    config = _get_config()
    logger.info("Starting server: transport=%s, home=%s", args.transport, config.home)

    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        # v0.7.0: Start knowledge watcher in HTTP mode for auto-sync
        try:
            from .watcher import KnowledgeWatcher
            from .recall import invalidate_scan_cache
            watcher = KnowledgeWatcher(
                knowledge_dir=[str(d) for d in config.knowledge_dirs],
                on_change=lambda event, fname: invalidate_scan_cache(),
                config=config,
            )
            watcher.start()
        except Exception as e:
            logger.warning("Failed to start knowledge watcher: %s", e)
        mcp.run(transport="http", port=args.port)


if __name__ == "__main__":
    main()

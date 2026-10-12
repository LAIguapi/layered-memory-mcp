"""
Layered Memory MCP Server — FastMCP 2.0 Implementation.

A common-core knowledge MCP server that extends AI agent memory beyond token
limits. Works with any MCP-compatible agent (Hermes, Claude, etc.).

The framework owns L0 index / L1 knowledge / TODO / memory changelog. Skills,
the user profile and other agent-owned content are classified and suggested,
never written by default.

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
import threading
from pathlib import Path

from fastmcp import FastMCP

from .config import MemoryConfig
from . import __version__
from .recall import recall, scan_knowledge_files, score_relevance, knowledge_health
from .session_scanner import (
    extract_session_summary,
    find_recent_sessions,
    scan_sessions,
    search_state_db_sessions,
)
from .l0_manager import sync_l0_index, auto_sync_if_enabled, manage_entry, check_l0_l1_consistency
from .injector import inject_knowledge, sync_to_vector_store, remove_from_vector_store, reindex_vector_store, vector_store_needs_reindex, calibrate_thresholds
from .promotion import scan_split_candidates, scan_cross_domain_duplicates
from .backups import backup_knowledge_file
from .governance import post_write_governance, build_validation_extras
from .routing import (
    classify_memory_item as _classify_memory_item,
    surface_capabilities as _surface_capabilities,
)
from .cleanup import plan_knowledge_cleanup, apply_safe_knowledge_cleanup
from .agent_integrator import (
    detect_agent_type,
    is_soul_injected,
    is_memory_injected,
    inject_soul,
    inject_memory,
    remove_soul_injection,
    build_init_recommendation,
)

# v2.0 imports
from .models import KnowledgeEntry, KnowledgeType, SourceInfo, SourceType, ReviewItem, ConfidenceScorer
from .models import TodoEntry, TodoStatus, TodoPriority
from .storage import L1Store, VectorStore, ReviewQueue, AccessLogStore
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
        # Warm the embedding model in the background so the first semantic
        # search doesn't block on a cold load (or a one-time ~55MB download).
        # Daemon thread: never blocks shutdown, failures are logged softly.
        threading.Thread(
            target=_v2_stores["vector"].warmup,
            name="vector-warmup",
            daemon=True,
        ).start()
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
# v2.6.0: Access-log telemetry store + best-effort recording helpers
# ---------------------------------------------------------------------------
_access_log_store: AccessLogStore | None = None


def _get_access_log() -> AccessLogStore | None:
    """Get the access-log store, or None if telemetry is disabled.

    Never raises — telemetry must not break retrieval.
    """
    global _access_log_store
    try:
        config = _get_config()
        if not getattr(config, "access_log_enabled", True):
            return None
        if _access_log_store is None:
            data_dir = config.home / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            _access_log_store = AccessLogStore(
                data_dir / "access_log.db",
                retention_days=getattr(config, "access_log_retention_days", 90),
                log_query=getattr(config, "access_log_query", True),
            )
        return _access_log_store
    except Exception as exc:
        logger.debug("access_log unavailable: %s", exc)
        return None


def _record_access(events: list[dict]) -> None:
    """Best-effort batch record of recall events. Never raises."""
    try:
        store = _get_access_log()
        if store is not None and events:
            store.record_batch(events)
    except Exception as exc:
        logger.debug("record_access failed: %s", exc)


def _domain_from_file(filename: str) -> str:
    """Map an L1 filename/relpath to its domain (basename minus .md)."""
    base = filename.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base


# ---------------------------------------------------------------------------
# MEMORY_CHANGELOG: provenance for the whole-file write tools
# ---------------------------------------------------------------------------
# inject_knowledge records its own section-level events inside the injector;
# this helper covers the three tools that replace a whole domain file.

def _record_file_mutation(
    config: MemoryConfig,
    operation: str,
    filename: str,
    *,
    tool: str,
    summary: str,
    reason: str,
    before_text: str | None = None,
    after_text: str | None = None,
) -> dict | None:
    """Record one whole-file L1 mutation. Never raises.

    Passes hashes, byte counts and the filename — never the file text. A write
    whose content hash did not move records nothing: provenance tracks meaning,
    not mtimes.
    """
    try:
        from .memory_changelog import hash_text, record as record_memory_change

        before_hash = hash_text(before_text) if before_text is not None else None
        after_hash = hash_text(after_text) if after_text is not None else None
        if before_hash is not None and before_hash == after_hash:
            return None
        return record_memory_change(
            config,
            operation,
            _domain_from_file(filename),
            tool=tool,
            summary=summary,
            reason=reason,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata={
                "file": filename,
                "before_bytes": len(before_text.encode("utf-8")) if before_text else 0,
                "after_bytes": len(after_text.encode("utf-8")) if after_text else 0,
            },
        )
    except Exception as exc:  # noqa: BLE001 — provenance never breaks a write
        logger.warning("memory changelog record failed (non-critical): %s", exc)
        return None



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

    # v2.6.0: best-effort recall telemetry (never blocks/raises)
    try:
        if result.get("success") and result.get("results"):
            events = [
                {
                    "domain": _domain_from_file(r.get("file", "")),
                    "access_type": "recall",
                    "query": keyword,
                    "rank": i + 1,
                    "score": r.get("score"),
                }
                for i, r in enumerate(result["results"])
                if r.get("file")
            ]
            await asyncio.to_thread(_record_access, events)
    except Exception as exc:
        logger.debug("recall telemetry skipped: %s", exc)

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

    db_available = config.hermes_db_path is not None and config.hermes_db_path.is_file()
    dir_available = bool(config.sessions_dir) and config.sessions_dir.exists()
    if not db_available and not dir_available:
        return json.dumps({
            "success": False,
            "error": "No session source available: neither Hermes' state.db "
                     f"({config.hermes_db_path}) nor a session directory exists. "
                     "Set LAYERED_MEMORY_HERMES_DB or LAYERED_MEMORY_SESSIONS_DIR.",
        })

    # scan_sessions prefers state.db and falls back to the directory; the privacy
    # markers come from the operator's config.yaml (empty unless they set them).
    result = await asyncio.to_thread(
        scan_sessions,
        str(config.sessions_dir) if dir_available else None,
        days,
        max_sessions,
        config.hermes_db_path,
        config.session_exclude_markers,
        config.session_exclude_sources,
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
        # v2.6.0: best-effort get_file telemetry
        try:
            await asyncio.to_thread(
                _record_access,
                [{"domain": _domain_from_file(filename), "access_type": "get_file"}],
            )
        except Exception as exc:
            logger.debug("get_file telemetry skipped: %s", exc)
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
async def audit_rot_tool() -> str:
    """Audit the knowledge base for decay/rot and return a health report.

    Read-only diagnostic. Detects four decay pathologies:
      - oversized files (append-without-merge accumulation)
      - garbled headings (lost punctuation, e.g. from old summariser bugs)
      - stale sections (transient markers / expired dates)
      - cross-file duplicate sections (same knowledge in multiple files)

    Returns a JSON report with a health score (0-100), per-pathology
    findings, and consolidation recommendations. Makes no changes — use it
    periodically (e.g. a weekly cron) to catch rot before it accumulates.
    """
    config = _get_config()
    from .rot_auditor import audit_rot
    report = await asyncio.to_thread(audit_rot, config)
    return json.dumps(report, ensure_ascii=False, indent=2)


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

    db_available = config.hermes_db_path is not None and config.hermes_db_path.is_file()
    dir_available = bool(config.sessions_dir) and config.sessions_dir.exists()
    if not db_available and not dir_available:
        return json.dumps({"success": False, "error": "No session source available (state.db or session directory)"})

    # Live sessions live in state.db; the file directory is the legacy fallback.
    if db_available:
        result = await asyncio.to_thread(
            search_state_db_sessions,
            keyword,
            config.hermes_db_path,
            days,
            max_results,
            config.session_exclude_markers,
            config.session_exclude_sources,
        )
        if result.get("matches") or not dir_available:
            return json.dumps(result, ensure_ascii=False, indent=2)

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

        # MEMORY_CHANGELOG: recorded before the derived L0/vector sync below, so
        # the L1 mutation precedes the index entries it causes.
        await asyncio.to_thread(
            _record_file_mutation,
            config,
            "l1_domain_created",
            filename,
            tool="create_knowledge_file",
            summary=f"knowledge file '{filename}' created",
            reason="whole-file create",
            after_text=content,
        )

        # v0.5.0: auto-sync L0 index
        sync_report = await asyncio.to_thread(auto_sync_if_enabled, config)

        # v2.2.0 + v2.9.3: rebuild the domain's vectors from the whole file so
        # vectors.db mirrors the file (one vector per ## section).
        domain = filename.removesuffix(".md")
        await asyncio.to_thread(
            sync_to_vector_store,
            data_dir=config.home / "data",
            domain=domain,
            content=content,
            replace_domain=True,
        )

        # v2.9.3: dual-write completion — write the new domain's L0 pointer into
        # agent memory (added or stale-replaced), same as inject_knowledge.
        dual_write = None
        if getattr(config, "auto_maintain", True):
            try:
                from .memory_compactor import _ensure_l0_pointer_in_memory
                from .injector import _summarize_for_l0

                tag = getattr(config, "l0_tag", "[L0]")
                l0_pointer = (
                    f"{tag} {domain}: {_summarize_for_l0(content)} "
                    f"→ knowledge/{filename}"
                )
                dual_write = await asyncio.to_thread(
                    _ensure_l0_pointer_in_memory, l0_pointer, config
                )
            except Exception as e:  # noqa: BLE001 — maintenance is non-critical
                logger.warning("create dual-write failed (non-critical): %s", e)
                dual_write = {"action": "error", "error": str(e)}

        result = {
            "success": True,
            "action": "created",
            "file": filename,
            "size_bytes": len(content.encode("utf-8")),
            "l0_synced": sync_report is not None,
        }
        if sync_report:
            result["l0_sync"] = sync_report
        if dual_write:
            result["dual_write"] = dual_write
        # v3.4.7: advisory lifecycle checks on what was just written.
        result["governance"] = await asyncio.to_thread(
            post_write_governance, config, domain, filepath, content, "create"
        )
        return json.dumps(result, ensure_ascii=False)
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
        # Read once and reuse: the backup below and the MEMORY_CHANGELOG
        # before_hash both need the outgoing text.
        try:
            old_text = await asyncio.to_thread(filepath.read_text, "utf-8")
        except OSError:
            old_text = None
        # v0.6.0: back up before overwriting. v3.4.7: under
        # <home>/data/backups/knowledge, not as a .bak beside the live file.
        # Never raises — a backup failure shouldn't block updates.
        backup = await asyncio.to_thread(
            backup_knowledge_file, config, filepath, old_text
        )
        await asyncio.to_thread(filepath.write_text, content, "utf-8")
        new_size = len(content.encode("utf-8"))
        logger.info("Updated knowledge file: %s (%d → %d bytes)", filename, old_size, new_size)

        # MEMORY_CHANGELOG. An overwrite with byte-identical content records
        # nothing — the file's mtime moved, its meaning did not.
        await asyncio.to_thread(
            _record_file_mutation,
            config,
            "l1_domain_updated",
            filename,
            tool="update_knowledge_file",
            summary=f"knowledge file '{filename}' overwritten ({old_size} → {new_size} bytes)",
            reason="whole-file update",
            before_text=old_text,
            after_text=content,
        )

        # v0.5.0: auto-sync L0 index
        sync_report = await asyncio.to_thread(auto_sync_if_enabled, config)

        # v2.2.0 + v2.9.3: rebuild the domain's vectors from the whole file so
        # vectors.db mirrors the file (no leftover section orphans).
        domain = filename.removesuffix(".md")
        await asyncio.to_thread(
            sync_to_vector_store,
            data_dir=config.home / "data",
            domain=domain,
            content=content,
            replace_domain=True,
        )

        # v2.9.3: dual-write completion. Whole-file writes previously bypassed
        # the L1↔agent-memory dual-write (only inject_knowledge ran it), so the
        # domain's L0 pointer in agent memory was never refreshed/deduped.
        # Generate the pointer and reconcile it (added or stale-replaced).
        dual_write = None
        if getattr(config, "auto_maintain", True):
            try:
                from .memory_compactor import (
                    _ensure_l0_pointer_in_memory,
                )
                from .injector import _summarize_for_l0

                tag = getattr(config, "l0_tag", "[L0]")
                l0_pointer = (
                    f"{tag} {domain}: {_summarize_for_l0(content)} "
                    f"→ knowledge/{filename}"
                )
                dual_write = await asyncio.to_thread(
                    _ensure_l0_pointer_in_memory, l0_pointer, config
                )
            except Exception as e:  # noqa: BLE001 — maintenance is non-critical
                logger.warning("update dual-write failed (non-critical): %s", e)
                dual_write = {"action": "error", "error": str(e)}

        result = {
            "success": True,
            "action": "updated",
            "file": filename,
            "old_size": old_size,
            "new_size": new_size,
            "l0_synced": sync_report is not None,
        }
        if sync_report:
            result["l0_sync"] = sync_report
        if dual_write:
            result["dual_write"] = dual_write
        result["backup"] = backup
        # v3.4.7: advisory lifecycle checks on what was just written.
        result["governance"] = await asyncio.to_thread(
            post_write_governance, config, domain, filepath, content, "update"
        )
        return json.dumps(result, ensure_ascii=False)
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
        try:
            old_text = await asyncio.to_thread(filepath.read_text, "utf-8")
        except OSError:
            old_text = None
        await asyncio.to_thread(filepath.unlink)
        logger.info("Deleted knowledge file: %s (%d bytes)", filename, old_size)

        # MEMORY_CHANGELOG: what the knowledge base used to say, and that it no
        # longer says it. The content itself lives on in data/backups/knowledge.
        await asyncio.to_thread(
            _record_file_mutation,
            config,
            "l1_domain_deleted",
            filename,
            tool="delete_knowledge_file",
            summary=f"knowledge file '{filename}' deleted ({old_size} bytes)",
            reason="whole-file delete",
            before_text=old_text,
        )

        # Clean up a legacy adjacent .bak if one exists (new writes no longer
        # create them; the backup under data/backups is left as a recovery copy).
        bak_path = filepath.with_suffix(filepath.suffix + ".bak")
        try:
            if bak_path.exists():
                await asyncio.to_thread(bak_path.unlink)
                logger.debug("Cleaned up .bak for deleted file: %s", filename)
        except Exception:
            pass  # Non-critical — .bak cleanup failure shouldn't block deletion

        # v0.5.0: auto-sync L0 index (removes deleted file from L0)
        sync_report = await asyncio.to_thread(auto_sync_if_enabled, config)

        # v2.2.1: remove from vector store
        domain = filename.removesuffix(".md")
        await asyncio.to_thread(
            remove_from_vector_store,
            data_dir=config.home / "data",
            domain=domain,
        )

        # v2.9.3: reap the dangling L0 pointer from agent memory. Deletes
        # previously left a stale "[L0] <domain>: … → knowledge/<file>" entry
        # behind (only the L0 index + vectors were cleaned), so recall kept
        # surfacing a pointer to a file that no longer exists.
        pointer_cleanup = None
        if getattr(config, "auto_maintain", True):
            try:
                from .memory_compactor import _remove_l0_pointer_from_memory

                pointer_cleanup = await asyncio.to_thread(
                    _remove_l0_pointer_from_memory, domain, config
                )
            except Exception as e:  # noqa: BLE001 — maintenance is non-critical
                logger.warning("delete pointer cleanup failed (non-critical): %s", e)
                pointer_cleanup = {"action": "error", "error": str(e)}

        result = {
            "success": True,
            "action": "deleted",
            "file": filename,
            "deleted_size_bytes": old_size,
            "l0_synced": sync_report is not None,
        }
        if pointer_cleanup:
            result["pointer_cleanup"] = pointer_cleanup
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
    fuse: bool = False,
    expected_hash: str | None = None,
) -> str:
    """Smart knowledge injection with dedup, section targeting, and auto L0 sync.

    The recommended write path for all agents. Handles deduplication,
    section-level targeting (creates ## headings if needed), and
    automatically syncs the L0 index after successful writes.

    v3.1.0 semantic upsert: dedup now uses bge-small-zh cosine over the
    section-level vector store (not char-level difflib). When new content is a
    semantic near-duplicate of an existing section (an UPDATE, not a new fact),
    the tool returns action="deferred_fusion" with the old section body + your
    new content instead of blindly appending. You (the LLM) fuse them into one
    lossless body and re-call with the SAME domain, section=<hit_section>,
    content=<fused text>, mode="upsert", fuse=True, AND
    expected_hash=<the expected_hash from the deferred_fusion response> to commit
    the merge under an optimistic lock.

    v3.1.2 TWO-STEP DEFERRED-FUSION HANDSHAKE (do not skip step 2):
      1. First call returns action="deferred_fusion" carrying fusion.old_body,
         fusion.new_content and expected_hash.
      2. You fuse old_body + new_content into one lossless body, then RE-CALL
         this tool with section=<hit_section>, content=<fused>, fuse=True,
         expected_hash=<that token>. The framework recomputes the section's hash
         and refuses (action="fuse_conflict") if it changed in between — re-defer
         and retry. Forgetting step 2 means the update is silently dropped.

    Args:
        domain: Target L1 file (with or without .md), e.g. "infra" or "infra.md".
        section: Target ## heading in the file, e.g. "WSL 代理".
                 Created automatically if it doesn't exist.
        content: Knowledge content to inject (markdown text).
        mode: Write mode — "upsert" (default, replace similar), "append" (always add),
              or "merge" (combine unique parts).
        agent_id: Optional agent identifier for provenance tracking.
        fuse: Commit a fusion write-back (second half of the deferred_fusion
              handshake). When True, `content` wholesale-replaces the target
              section; pair with the hit_section returned by the prior call.
        expected_hash: Optimistic-lock token echoed from the prior
              deferred_fusion response. REQUIRED for a well-behaved fuse=True
              write; the framework refuses the overwrite if the section changed
              since the token was issued (lost-update guard). Omitting it keeps
              the legacy unlocked behavior for backward compatibility.

    Returns:
        JSON with action taken, dedup info, L0 sync status, and warnings.
        May instead return action="deferred_fusion" (needs_fusion=True) carrying
        the fusion contract + expected_hash, or action="fuse_conflict" when the
        optimistic lock rejects a stale fuse. May also carry a passive
        title_diff_hint / stale_residue_hint (same-file advisories only).
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
        fuse=fuse,
        expected_hash=expected_hash,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def reindex_vector_store_tool(drop_existing: bool = True) -> str:
    """Rebuild the semantic vector store from every L1 knowledge file.

    Vectors are pure derived data (one per ## section, recomputed from the
    markdown body), so this is a lossless, single-direction rebuild. Run it:
      - On first use / after a pip upgrade over an existing knowledge base
        whose historical writes didn't all sync their vectors.
      - When vectors.db drifts from the .md files (bulk external edits).

    The semantic upsert write path reads this store to decide replace vs merge
    vs append, so a complete index is what makes dedup accurate.

    Args:
        drop_existing: wipe vectors.db before rebuilding (default True) so
            deleted sections/files leave no orphan vectors.

    Returns:
        JSON: {success, domains, sections, files, errors}.
    """
    config = _get_config()
    result = await asyncio.to_thread(
        reindex_vector_store, config=config, drop_existing=drop_existing
    )
    # Refresh the live singleton store's cache so subsequent searches see it.
    try:
        stores = _get_v2_stores()
        stores["vector"]._invalidate_cache()
    except Exception:
        pass
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def reconcile_knowledge_tool(
    checks: str = "all",
    dup_threshold: float | None = None,
    domains: str | None = None,
) -> str:
    """Active, pull-based knowledge-base maintenance — suggestions only, never mutates.

    This is the framework's first-class "housekeeping" entry point. The write path
    stays deterministic and cheap (exact dedup + in-file semantic upsert); the
    heavy, global, best-effort analysis lives HERE and runs only when you ask for
    it — NOT on a cron, NOT on every write. It reads the vector store + markdown
    and returns a structured worklist for the agent/user to act on. It NEVER moves,
    merges, or deletes any knowledge (that stays a human/agent decision).

    Runs any subset of four checks:
      - cross_dup:  cross-FILE near-duplicate section pairs (which knowledge is
                    duplicated across files / namespace / shared), each tagged
                    with scope so you know if consolidating crosses a red line.
      - split:      domains where a same-topic cluster has accumulated and
                    deserves extraction into its own L1 file.
      - calibrate:  recompute semantic thresholds from the live corpus's own
                    similarity distribution (valley detection), writing
                    sem_thresholds.json; reports sample size + reliability +
                    fallback reason if the corpus is too small to calibrate.
      - vector_health: section-level drift/gap detection between .md files and
                    vectors.db (catches silent vector-sync failures that would
                    otherwise make dedup blind).

    Args:
        checks: comma-separated subset of {cross_dup, split, calibrate,
            vector_health} or "all" (default).
        dup_threshold: override cosine threshold for cross_dup (default: the
            calibrated/config fuse threshold).
        domains: comma-separated domain filter for the split check (default: all).

    Returns:
        JSON: {ran: [...], cross_dup?, split?, calibrate?, vector_health?}.
        Every section is advisory — nothing is modified.
    """
    config = _get_config()
    requested = (
        {"cross_dup", "split", "calibrate", "vector_health"}
        if checks.strip().lower() == "all"
        else {c.strip().lower() for c in checks.split(",") if c.strip()}
    )
    domain_list = (
        [d.strip() for d in domains.split(",") if d.strip()] if domains else None
    )

    def _run() -> dict:
        out: dict = {"ran": sorted(requested)}
        if "cross_dup" in requested:
            out["cross_dup"] = scan_cross_domain_duplicates(
                config, threshold=dup_threshold
            )
        if "split" in requested:
            out["split"] = scan_split_candidates(config, domains=domain_list)
        if "calibrate" in requested:
            out["calibrate"] = calibrate_thresholds(config)
        if "vector_health" in requested:
            out["vector_health"] = vector_store_needs_reindex(config)
        return out

    result = await asyncio.to_thread(_run)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def plan_knowledge_cleanup_tool(max_items: int = 50) -> str:
    """Plan knowledge-base cleanup — read-only, never mutates.

    Sorts every lifecycle problem it can detect deterministically into three
    buckets:
      - safe_auto_fixes: mechanical actions you can hand straight to
        apply_safe_knowledge_cleanup_tool — move legacy knowledge/*.md.bak
        out of tree, re-sync a drifted L0 index, reindex an under-populated
        vector store, copy open state markers (TODO / 待拍板 / next step…)
        into the TODO store. None of them deletes or rewrites L1 text.
      - needs_user_decision: resolved history to rewrite as the current rule,
        same-skeleton heading families to merge, oversized files, marker lines
        that cannot become a TODO as-is.
      - must_not_auto_fix: markers kept on purpose (the section has a
        "状态保留原因：…" / "State retention reason: …" line).

    No embeddings, no LLM calls. For embedding-based checks (cross-file
    duplicates, split candidates) use reconcile_knowledge_tool.

    Args:
        max_items: cap per bucket (totals are always reported in summary).

    Returns:
        JSON: {read_only, summary, safe_auto_fixes, needs_user_decision,
        must_not_auto_fix, policy, see_also, how_to_apply}.
    """
    config = _get_config()
    result = await asyncio.to_thread(plan_knowledge_cleanup, config, max(1, int(max_items)))
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def apply_safe_knowledge_cleanup_tool(actions: list, dry_run: bool = True) -> str:
    """Apply safe, mechanical cleanup actions from plan_knowledge_cleanup_tool.

    DEFAULTS TO A DRY RUN. Only these actions are accepted; anything else is
    rejected, never guessed:
      - migrate_legacy_backups: move knowledge/**/*.md.bak under
        <home>/data/backups/knowledge (verified copy, then remove the sibling).
      - sync_l0_index: regenerate the L0 index from the L1 files.
      - reindex_vector_store: rebuild vectors.db from the files (skipped when
        coverage is already complete unless {"force": true}).
      - create_todo_from_marker: copy one open state-marker line into the TODO
        store. Needs file + line + line_hash from the plan; a stale line is
        skipped. The TODO id is derived from the line, so re-applying is a
        no-op. The source L1 line is ALWAYS left in place (remove_source is
        rejected).

    Args:
        actions: list of action objects, e.g. items copied from the plan's
            safe_auto_fixes.
        dry_run: True (default) reports what would happen and writes nothing.

    Returns:
        JSON: {dry_run, applied, would_apply, skipped, rejected, errors, summary}.
    """
    config = _get_config()
    result = await asyncio.to_thread(
        apply_safe_knowledge_cleanup, config, actions, dry_run, _get_todo_store
    )
    if not dry_run and any(a.get("action") == "reindex_vector_store" for a in result["applied"]):
        # Refresh the live singleton store's cache (if one was ever opened).
        try:
            if _v2_stores:
                _v2_stores["vector"]._invalidate_cache()
        except Exception:
            pass
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
        JSON with per-item results, total counts, and any errors. Each item
        carries the same `routing` advisory as a single inject (plus
        `routing_warning` when the router would have sent it to another
        surface), and a `deferred_fusion` item carries its full handshake
        (`needs_fusion`, `expected_hash`, `fusion`) — batching is a convenience,
        never a way to skip the contracts a single write has to honour.
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
            item_result = {
                "index": idx,
                "success": result.get("success", False),
                "domain": domain,
                "section": section,
                "action": result.get("action"),
                "dedup": result.get("dedup"),
                "l0_synced": result.get("l0_synced"),
                "error": result.get("error"),
                "routing": result.get("routing"),
            }
            if result.get("routing_warning"):
                item_result["routing_warning"] = result["routing_warning"]
            # A batched deferred_fusion is still a two-step handshake. Dropping
            # the contract here would make the batch path the one place where an
            # update can be silently lost, so carry it through verbatim.
            if result.get("action") == "deferred_fusion":
                item_result["needs_fusion"] = result.get("needs_fusion", True)
                item_result["expected_hash"] = result.get("expected_hash")
                item_result["fusion"] = result.get("fusion")
                item_result["reason"] = result.get("reason")
            results.append(item_result)
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


# ---------------------------------------------------------------------------
# MCP Tools — Persistence routing (read-only: classify and reflect, never write)
# ---------------------------------------------------------------------------

@mcp.tool()
async def classify_memory_item(
    content: str,
    domain: str | None = None,
    section: str | None = None,
    supported_surfaces: str | None = None,
    agent_type: str | None = None,
    mode: str | None = None,
) -> str:
    """Decide which memory surface a candidate item belongs to. READ-ONLY.

    Call this BEFORE persisting something you are unsure about. It writes
    nothing, reads no store, and makes no model call — the decision comes from
    deterministic marker + structure rules, so `score` is a rule-weight sum, not
    a probability.

    Surfaces it can return:
      - framework-owned common core — `l1_knowledge` (durable facts, decisions,
        architecture, current understanding), `todo` (pending / blocked / needs
        validation), `l0_index`, `memory_changelog` (provenance of L0/L1
        changes only);
      - agent-owned extension recommendation — `skill` (repeatable action
        protocols: steps, commands, checklists, runbooks), `user_profile`
        (standing global user preferences), `agent_other`. By default the
        framework only RECOMMENDS these: `framework_can_apply` is false and
        `suggested_action` is for YOU to apply. Enabling `agent_extensions` with
        a handler flips `framework_can_apply`; ownership stays `agent`;
      - no durable write — `undecided`, `ephemeral` (one-off output, raw traces).

    Args:
        content: the candidate item's text.
        domain: optional target domain, echoed as context.
        section: optional target heading. Scanned for markers along with the
            content (a heading is part of the item), never for structure.
        supported_surfaces: comma-separated surfaces you can actually honour
            (e.g. "l1_knowledge,todo"). When the decided surface is not among
            them, no `suggested_action` is produced — an action pointed at a
            surface you cannot write is worse than none.
        agent_type: echo/statistics only. It is returned under `echo` and has NO
            effect on the classification.
        mode: optional per-call override, "advisory" (default) or "enforce".
            Reported only; this tool never mutates anything either way.

    Returns:
        The serialized RoutingDecision, plus an `echo` object carrying the
        inputs that do not affect the decision.
    """
    config = _get_config()
    surfaces = (
        [s.strip() for s in supported_surfaces.split(",") if s.strip()]
        if supported_surfaces
        else None
    )
    decision = await asyncio.to_thread(
        _classify_memory_item,
        content,
        config=config,
        domain=domain,
        section=section,
        supported_surfaces=surfaces,
        mode=mode,
    )
    payload = decision.to_dict()
    payload["echo"] = {
        "agent_type": agent_type,
        "domain": domain,
        "section": section,
        "supported_surfaces": surfaces,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_memory_surface_capabilities() -> str:
    """Report what the framework may write, and what it can only suggest. READ-ONLY.

    Use this to find out, under the CURRENT configuration, whether to hand an
    item to the framework or to apply it yourself:

      - `core_surfaces` — framework-owned (`l0_index`, `l1_knowledge`, `todo`,
        `memory_changelog`): `can_apply` is true, write them through the
        framework's own tools.
      - `extension_surfaces` — agent-owned (`skill`, `user_profile`,
        `agent_other`): `owned_by` is always `agent`. `can_apply` is false by
        default, meaning the framework classifies and suggests only. It becomes
        true only when `agent_extensions` is enabled, that surface is enabled,
        a handler is configured and the mode allows applying — and even then
        ownership does not move; the framework is just an orchestrator.
      - `no_write_surfaces` — `undecided` / `ephemeral`: nothing is persisted.
      - `source_owned_evidence` — raw session data: scanned, never written.
      - `derived_stores` — L0 index, vector store, search index: rebuildable
        from the L1 markdown, which is the source of truth.

    Returns:
        JSON MemorySurfaceCapabilities (schema_version, routing mode,
        agent_extensions state, and the four surface groups).
    """
    config = _get_config()
    caps = await asyncio.to_thread(_surface_capabilities, config)
    return json.dumps(caps.to_dict(), ensure_ascii=False, indent=2)


@mcp.tool()
async def get_memory_changelog(
    domain: str | None = None,
    operation: str | None = None,
    surface: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> str:
    """Read the provenance log of semantic L0/L1 memory changes. READ-ONLY.

    Answers "why does my knowledge base say this now?". Every entry is one
    semantic mutation of a framework-owned durable surface: an L1 section
    created / updated / merged / replaced, a fusion or consolidation
    write-back, a whole-file create / update / delete, or an L0 entry added /
    removed / renamed when the map actually moved.

    What is deliberately NOT in here: raw session content, TODO lifecycle,
    skill / user-profile / agent-other suggestions, derived-data rebuilds
    (vector reindex, search index), deferred or refused writes, and writes that
    changed no semantic content. An entry carries structure only — operation,
    domain, section heading, tool, byte counts and `before_hash` / `after_hash`
    — never the body text; the knowledge files remain the source of truth for
    content.

    Args:
        domain: only entries for this domain (the L1 file minus `.md`).
        operation: exact operation filter, e.g. `l1_section_replaced`.
        surface: `l1_knowledge` or `l0_index`.
        since: ISO timestamp or date ("2026-10-01") — entries at or after it.
        limit: max entries (default 50, hard ceiling 500).

    Returns:
        JSON: {enabled, path, total_events, matched, returned, limit, order,
        filters, entries}. `order` is `newest_first`.
    """
    config = _get_config()
    from .memory_changelog import read_events

    report = await asyncio.to_thread(
        read_events,
        config,
        domain=domain,
        operation=operation,
        surface=surface,
        since=since,
        limit=limit,
    )
    return json.dumps(report, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_memory_changelog_stats() -> str:
    """Summarise the memory changelog: counts, span, and what it records. READ-ONLY.

    Use this before `get_memory_changelog` to see whether a domain has any
    provenance at all, and to confirm the backend's configured state.

    Returns:
        JSON: {enabled, path, total_events, by_operation, by_surface,
        by_domain (top 10), first_event_at, last_event_at, size_bytes,
        malformed_lines, retention_days, retention_enforced, retention_note,
        recordable_operations, not_recorded}. The log is append-only and this
        version never prunes it — `retention_days` is reported, not applied.
    """
    config = _get_config()
    from .memory_changelog import changelog_stats

    report = await asyncio.to_thread(changelog_stats, config)
    return json.dumps(report, ensure_ascii=False, indent=2)


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

        # v3.4.7: rot + state-marker visibility. Informational only — it does
        # not feed overall_health, and a failure degrades to an *_error key.
        report.update(build_validation_extras(config))

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

        extractor = KnowledgeExtractor(
            auto_approve_threshold=auto_approve_threshold,
            domain_keywords=config.domain_keywords,
        )
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

    # v2.6.0: best-effort semantic-recall telemetry
    try:
        events = [
            {
                "domain": r.get("domain", ""),
                "access_type": "semantic",
                "query": query,
                "rank": i + 1,
                "score": r.get("score"),
            }
            for i, r in enumerate(results)
            if r.get("domain")
        ]
        await asyncio.to_thread(_record_access, events)
    except Exception as exc:
        logger.debug("semantic telemetry skipped: %s", exc)

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

    # v2.7.0: ride-along self-maintenance. get_l0_index is the highest-frequency
    # tool in the stdio lifecycle — Hermes (and most agents) call it at the
    # start of every session. Piggybacking a memory-health check here gives the
    # framework a real chance to self-maintain even when the agent only ever
    # writes to native memory and never calls inject_knowledge. Best-effort:
    # any failure is swallowed so it can never break index retrieval.
    if getattr(config, "auto_maintain", True):
        try:
            from .memory_compactor import auto_maintain_after_write

            await asyncio.to_thread(auto_maintain_after_write, config, None)
        except Exception as e:  # noqa: BLE001 — maintenance must never break reads
            logger.debug("get_l0_index ride-along auto-maintain skipped: %s", e)

    # v3.3.5: ride-along guard self-repair. The guard on the host is a copy of
    # the payload bundled in this package, so a framework upgrade used to leave
    # it a version behind until somebody re-ran install_guard — and the auto
    # policy could not heal it either (install_guard refuses a version drift
    # unless forced). Refreshing is a files-only copy, so doing it on this read
    # path keeps the host in step without any approval step. Auto policy only:
    # a manual host keeps the human in the loop.
    if str(getattr(config, "write_guard", "manual")).strip().lower() == "auto":
        try:
            from .write_guard import refresh_deployed_plugin

            agent_info = detect_agent_type()
            if agent_info.get("home_dir"):
                await asyncio.to_thread(
                    refresh_deployed_plugin,
                    agent_info["home_dir"],
                    "auto",
                    config.home / "write_guard_state.json",
                )
        except Exception as e:  # noqa: BLE001 — a repair must never break reads
            logger.debug("get_l0_index ride-along guard refresh skipped: %s", e)

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
    return """You use a layered memory system with a framework-owned common core (L0 index, L1 knowledge, TODO, memory changelog) and your own agent-owned surfaces (skills, user profile). Follow this decision tree for EVERY problem or request:

## Decision Tree (MANDATORY)

1. SCAN: Check your L0 index for domains relevant to the current problem.
2. IF MATCHED:
   a. Load the corresponding L1 knowledge file, and your own skill if the task is procedural.
   b. Read and understand it BEFORE taking any action.
   c. If the knowledge fully solves the problem → use it. Do NOT bypass with guessing.
   d. If the knowledge partially helps → use what applies, then enhance/update the entry.
   e. If the knowledge is outdated or insufficient → treat as a new problem (step 3).
3. IF NO MATCH: Treat as a new problem. Solve with standard tools and reasoning.
4. AFTER SOLVING: Evaluate — is this worth preserving for future sessions? If so, route it:
   a. Durable facts, decisions, architecture, current understanding → L1 via inject_knowledge.
   b. Unfinished work (pending, blocked, needs validation) → the TODO store, never L1.
   c. Repeatable procedures, command sequences, checklists → YOUR skill surface.
   d. Standing global user preferences → YOUR profile surface, after confirming with the user.
   e. One-off output and raw traces → nothing. Let them go.
   f. Unsure? Call classify_memory_item — it is read-only and answers exactly this.

## Key Principles

- NEVER guess when you have relevant knowledge stored. Load it first.
- The L0 index is a map, not the territory. You must load L1 to actually use the knowledge.
- The framework owns the common core; skills and the user profile stay yours. It classifies and
  suggests for those, and only applies them if the user has explicitly opted in.
- Every solved problem is a candidate for memory improvement. Be proactive about capturing insights.
- When you discover gaps in existing knowledge, update it immediately — don't wait to be asked."""


# ---------------------------------------------------------------------------
# MCP Tools — v0.8.x: Memory Hygiene
# ---------------------------------------------------------------------------

@mcp.tool()
async def compact_memory(
    dry_run: bool = True, routing_mode: str | None = None
) -> str:
    """Scan agent memory for non-index bloat and migrate to L1 knowledge files.

    Detects entries in the agent's memory that are not L0 index pointers,
    classifies them by domain, and either reports (dry_run) or automatically
    migrates them to appropriate L1 files.

    Routing-aware: "not an L0 pointer" is a statement about the *source*, not a
    classification of the content, so every non-index entry is also classified
    into a memory surface.

      - `advisory` (default): behaviour is unchanged — every non-index entry
        still migrates to L1 — but each entry carries a `routing` block and
        `routing_advisory` lists what the router would have sent elsewhere (a
        runbook, a standing preference, a raw trace), so you can see what was
        misfiled and move it afterwards.
      - `enforce`: only `l1_knowledge` entries reach L1. Everything else is left
        in agent memory untouched and returned under `todo_candidates`,
        `agent_owned_candidates` or `rejected` for you to place. The framework
        writes nothing outside L1: no TODO is created, and skill / user_profile
        / agent_other stay agent-owned unless an `agent_extensions` adapter is
        configured and you invoke it.

    Args:
        dry_run: If True (default), only report what would be migrated.
                 If False, perform the migration and clean up memory.
        routing_mode: Override `routing.mode` from config for this call:
                 "advisory" or "enforce". None (default) uses the config value.

    Returns:
        JSON with migration plan or execution report, including
        `routing_mode`, `routing_summary`, `routing_advisory`, `l1_candidates`,
        `todo_candidates`, `agent_owned_candidates` and `rejected`.
    """
    config = _get_config()
    from .memory_compactor import compact_memory as _compact
    result = await asyncio.to_thread(
        _compact, config=config, dry_run=dry_run, routing_mode=routing_mode
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def init_framework() -> str:
    """Initialize the layered memory framework for first-time use.

    Detects whether the knowledge base is empty (first run), creates a
    welcome file with getting-started guidance, and returns memory
    management rules for the agent to follow.

    v2.2.0+: Also detects the hosting agent type (Hermes / generic MCP)
    and checks whether SOUL/MEMORY injection is already in place.
    Returns integration recommendations so the agent can guide the user
    through setup.

    Returns:
        JSON with initialization status, memory management rules,
        and agent integration recommendations.
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

        # ── v2.2.0: Agent integration detection ───────────────────
        agent_info = detect_agent_type()
        soul_injected = False
        memory_injected = False
        if agent_info["agent_type"] == "hermes":
            if agent_info["soul_path"]:
                soul_injected = is_soul_injected(agent_info["soul_path"])
            if agent_info["memory_path"]:
                memory_injected = is_memory_injected(agent_info["memory_path"])

        integration = build_init_recommendation(
            agent_info=agent_info,
            version=__version__,
            memory_already_injected=memory_injected,
            soul_already_injected=soul_injected,
        )

        # v2.5.0+: Dashboard plugin status (detect only, don't auto-deploy)
        dashboard_plugin_info = None
        if agent_info["agent_type"] == "hermes" and agent_info["home_dir"]:
            from .dashboard_plugin import check_dashboard_plugin_status
            dashboard_plugin_info = check_dashboard_plugin_status(agent_info["home_dir"])

        # v3.3.0: MEMORY.md write guard. Always detected; installed only under
        # the "auto" policy. The default is "manual" because the host agent's
        # config belongs to the user — a third-party install must not edit it
        # unasked. Without the guard, index_only mode still lets the agent write
        # straight into MEMORY.md; compaction only moves the bloat afterwards.
        write_guard_info = None
        guard_policy = getattr(config, "write_guard", "manual")
        if agent_info["agent_type"] == "hermes" and agent_info["home_dir"] and guard_policy != "off":
            from .write_guard import check_guard_status, ensure_guard_installed
            guard_kwargs = {
                "config_path": os.environ.get("HERMES_CONFIG_PATH") or None,
                "state_path": config.home / "write_guard_state.json",
            }
            if guard_policy == "auto":
                ensured = ensure_guard_installed(agent_info["home_dir"], **guard_kwargs)
                write_guard_info = dict(ensured["status"])
                write_guard_info["policy"] = guard_policy
                if ensured.get("install"):
                    write_guard_info["install"] = ensured["install"]
                if ensured.get("refresh"):
                    write_guard_info["refresh"] = ensured["refresh"]
            else:
                write_guard_info = check_guard_status(agent_info["home_dir"], **guard_kwargs)
                write_guard_info["policy"] = guard_policy

        result = {
            "success": True,
            "first_run": total == 0,
            "l1_files_found": total,
            "rules": rules,
            "agent_type": agent_info["agent_type"],
            "integration": integration,
        }

        if dashboard_plugin_info:
            result["dashboard_plugin"] = dashboard_plugin_info

        if write_guard_info:
            result["write_guard"] = write_guard_info

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
                "Framework-owned common core:\n"
                "- **L0 index**: short pointers to what exists (loaded every session)\n"
                "- **L1 knowledge**: durable facts, decisions, current state (loaded on demand)\n"
                "- **TODO**: unfinished state — pending, blocked, needs validation\n"
                "- **Memory changelog**: provenance of L0/L1 changes\n\n"
                "Agent-owned extension surfaces (the framework classifies and suggests, "
                "and only writes them if you opt in via `agent_extensions`):\n"
                "- **Skills**: repeatable procedures and runbooks\n"
                "- **User profile**: standing global preferences\n\n"
                "Source-owned evidence:\n"
                "- **Raw sessions**: scanned and searched, never written\n"
            )
            welcome_path = config.knowledge_dir / "getting-started.md"
            welcome_path.write_text(welcome_content, encoding="utf-8")
            auto_sync_if_enabled(config)
            result["action"] = "created getting-started.md"

        return result

    result = await asyncio.to_thread(_init)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def integrate_agent(action: str = "status") -> str:
    """Execute agent integration for layered-memory awareness.

    Use this after init_framework returns integration recommendations.
    The agent should present the options to the user and call this tool
    with the chosen action.

    Actions:
      - "status": check current injection status (default)
      - "inject_soul": inject rules into Hermes SOUL.md
      - "inject_memory": inject awareness snippet into MEMORY.md
      - "remove_soul": remove the injection block from SOUL.md
      - "install_dashboard": deploy the Dashboard plugin
      - "remove_dashboard": remove the Dashboard plugin
      - "guard_status": report the MEMORY.md write-guard status
      - "install_guard": deploy + enable the MEMORY.md write-guard plugin and
        silence the periodic memory-review nudge (0), so the agent stops
        writing facts into the framework-owned MEMORY.md
      - "remove_guard": uninstall the guard and restore the previous nudge value

    Returns:
        JSON with the result of the action.
    """
    config = _get_config()

    def _integrate():
        agent_info = detect_agent_type()

        if action == "status":
            soul_injected = False
            memory_injected = False
            if agent_info["agent_type"] == "hermes":
                if agent_info["soul_path"]:
                    soul_injected = is_soul_injected(agent_info["soul_path"])
                if agent_info["memory_path"]:
                    memory_injected = is_memory_injected(agent_info["memory_path"])
            return {
                "success": True,
                "agent_type": agent_info["agent_type"],
                "soul_injected": soul_injected,
                "memory_injected": memory_injected,
                "soul_path": str(agent_info["soul_path"]) if agent_info["soul_path"] else None,
                "memory_path": str(agent_info["memory_path"]) if agent_info["memory_path"] else None,
            }

        if action == "inject_soul":
            if agent_info["agent_type"] != "hermes":
                return {"success": False, "error": "SOUL injection only supported for Hermes Agent."}
            if not agent_info["soul_path"]:
                return {"success": False, "error": "SOUL.md not found."}
            return inject_soul(agent_info["soul_path"], __version__)

        if action == "inject_memory":
            if agent_info["agent_type"] != "hermes":
                return {"success": False, "error": "Memory injection only supported for Hermes Agent."}
            if not agent_info["memory_path"]:
                return {"success": False, "error": "MEMORY.md not found."}
            return inject_memory(agent_info["memory_path"])

        if action == "remove_soul":
            if not agent_info["soul_path"]:
                return {"success": False, "error": "SOUL.md not found."}
            return remove_soul_injection(agent_info["soul_path"])

        if action == "install_dashboard":
            if agent_info["agent_type"] != "hermes":
                return {"success": False, "error": "Dashboard plugin only supported for Hermes Agent."}
            if not agent_info["home_dir"]:
                return {"success": False, "error": "Hermes home directory not found."}
            from .dashboard_plugin import deploy_dashboard_plugin
            return deploy_dashboard_plugin(agent_info["home_dir"])

        if action == "remove_dashboard":
            if agent_info["agent_type"] != "hermes":
                return {"success": False, "error": "Dashboard plugin only supported for Hermes Agent."}
            if not agent_info["home_dir"]:
                return {"success": False, "error": "Hermes home directory not found."}
            from .dashboard_plugin import remove_dashboard_plugin
            return remove_dashboard_plugin(agent_info["home_dir"])

        if action in ("guard_status", "install_guard", "remove_guard"):
            if agent_info["agent_type"] != "hermes":
                return {"success": False, "error": "MEMORY.md write guard is only supported for Hermes Agent."}
            if not agent_info["home_dir"]:
                return {"success": False, "error": "Hermes home directory not found."}
            from .write_guard import check_guard_status, install_guard, remove_guard
            guard_kwargs = {
                "config_path": os.environ.get("HERMES_CONFIG_PATH") or None,
                "state_path": config.home / "write_guard_state.json",
            }
            if action == "guard_status":
                return check_guard_status(agent_info["home_dir"], **guard_kwargs)
            if action == "install_guard":
                return install_guard(agent_info["home_dir"], **guard_kwargs)
            return remove_guard(agent_info["home_dir"], **guard_kwargs)

        return {"success": False, "error": f"Unknown action: {action}"}

    result = await asyncio.to_thread(_integrate)
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# MCP Tools — v2.1.0: TODO Management
# ---------------------------------------------------------------------------


@mcp.tool()
async def add_todo(domain: str, content: str, title: str = "", blocked_by: str = "[]", priority: str = "medium", notes: str = "", source_session_id: str = "") -> str:
    """Add a new TODO item."""
    import json as _json
    from datetime import datetime, timezone
    store = _get_todo_store()
    try:
        blocked = _json.loads(blocked_by) if isinstance(blocked_by, str) else blocked_by
        entry = TodoEntry(
            domain=domain,
            content=content,
            title=title or (content[:50] if content else "Untitled"),
            blocked_by=blocked if blocked else [],
            priority=TodoPriority(priority),
            notes=notes,
            source_session_id=source_session_id or None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        result = store.add(entry)
        return _json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return _json.dumps({"success": False, "error": str(e)}, ensure_ascii=False, indent=2)


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
# v2.6.0: Access-log telemetry tools (knowledge time dimension)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_access_stats(days: int = 30, limit: int = 20) -> str:
    """Get knowledge recall telemetry — which knowledge is used vs dead weight.

    Reports the most-recalled L1 domains, zombie domains (indexed but never
    recalled past a grace period), and a window overview. This is the "time
    dimension" of the knowledge base: it answers whether each domain is
    actually producing value, not just whether it exists.

    Args:
        days: Stats window in days (default 30).
        limit: Max domains in the top-recalled list (default 20).

    Returns:
        JSON with overview, top_recalled, and zombies.
    """
    def _stats():
        store = _get_access_log()
        if store is None:
            return {"success": False, "error": "Access-log telemetry is disabled."}

        config = _get_config()
        overview = store.overview(days=days)
        top = store.top_recalled(days=days, limit=limit)

        # Zombie detection: L1 domains never recalled, created > grace days ago.
        from .recall import scan_knowledge_dirs, scan_knowledge_files
        kdirs = [str(d) for d in config.knowledge_dirs]
        files = scan_knowledge_dirs(kdirs) if len(kdirs) > 1 else scan_knowledge_files(kdirs[0])
        l1_domains = {_domain_from_file(f) for f in files.keys()}

        recalled = store.recalled_domains()
        from .storage.access_log import ZOMBIE_GRACE_DAYS
        import time as _time
        now = _time.time()
        zombies = []
        for dom in sorted(l1_domains - recalled):
            fpath = files.get(dom + ".md") or files.get(dom)
            age_days = None
            try:
                if fpath:
                    age_days = round((now - Path(fpath).stat().st_mtime) / 86400, 1)
            except Exception:
                pass
            # only flag if older than grace period (or age unknown)
            if age_days is None or age_days >= ZOMBIE_GRACE_DAYS:
                zombies.append({"domain": dom, "age_days": age_days})

        return {
            "success": True,
            "overview": overview,
            "top_recalled": top,
            "zombies": zombies,
            "zombie_count": len(zombies),
            "grace_days": ZOMBIE_GRACE_DAYS,
        }

    result = await asyncio.to_thread(_stats)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_domain_timeline(domain: str, days: int = 30) -> str:
    """Get the recall timeline for a single knowledge domain.

    Shows recall stats and a per-day recall curve, so you can see whether a
    domain is heating up, cooling down, or dormant.

    Args:
        domain: Knowledge domain name (filename without .md).
        days: Window in days (default 30).

    Returns:
        JSON with domain stats and a per-day trend series.
    """
    def _timeline():
        store = _get_access_log()
        if store is None:
            return {"success": False, "error": "Access-log telemetry is disabled."}
        dom = _domain_from_file(domain)
        return {
            "success": True,
            "stats": store.domain_stats(dom, days=days),
            "trend": store.trend(dom, days=days),
        }

    result = await asyncio.to_thread(_timeline)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def prune_access_log() -> str:
    """Delete access-log events older than the retention window.

    Telemetry auto-bounds itself, but this lets you force a cleanup. Returns
    the number of rows deleted.
    """
    def _prune():
        store = _get_access_log()
        if store is None:
            return {"success": False, "error": "Access-log telemetry is disabled."}
        deleted = store.prune()
        return {"success": True, "deleted": deleted, "retention_days": store.retention_days}

    result = await asyncio.to_thread(_prune)
    return json.dumps(result, ensure_ascii=False, indent=2)


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
        # v3.4.0: periodic self-maintenance. Only here (HTTP = long-lived daemon):
        # a stdio server is per-session, short-lived, and its stdout carries the
        # JSON-RPC stream, so a background thread must not write there.
        try:
            from .maintenance import start_maintenance

            start_maintenance(config)
        except Exception as e:
            logger.warning("Failed to start maintenance loop: %s", e)
        mcp.run(transport="http", port=args.port)


if __name__ == "__main__":
    main()

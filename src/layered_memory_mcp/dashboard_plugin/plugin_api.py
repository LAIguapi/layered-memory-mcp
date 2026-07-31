"""Layered Memory Dashboard Plugin — Backend API.

FastAPI Router exposing layered-memory MCP tools through Dashboard plugin API.
Uses absolute imports from the layered_memory_mcp package.

All write endpoints support dry_run mode for preview before execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger("layered_memory_dashboard")

router = APIRouter()

# ── Import from layered_memory_mcp (absolute, not relative) ──────────
# These work because the Dashboard process loads the plugin via importlib
# and the layered_memory_mcp package is installed in the same Python environment.

try:
    from layered_memory_mcp.config import MemoryConfig
    from layered_memory_mcp.l0_manager import sync_l0_index, check_l0_l1_consistency
    from layered_memory_mcp.injector import inject_knowledge, sync_to_vector_store
    from layered_memory_mcp.recall import recall, scan_knowledge_files, knowledge_health
    from layered_memory_mcp.storage import L1Store, VectorStore, ReviewQueue
    from layered_memory_mcp.todo_store import TodoStore
    from layered_memory_mcp import __version__ as MCP_VERSION
except ImportError as exc:
    logger.error("Failed to import layered_memory_mcp modules: %s", exc)
    raise


# ── Lazy config singleton ───────────────────────────────────────────────
_config: MemoryConfig | None = None


def _get_config() -> MemoryConfig:
    global _config
    if _config is None:
        from layered_memory_mcp.config import MemoryConfig
        _config = MemoryConfig()
    return _config


# ── Lazy store singletons ─────────────────────────────────────────────
_v2_stores: dict = {}


def _get_v2_stores():
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


_todo_store: TodoStore | None = None


def _get_todo_store() -> TodoStore:
    global _todo_store
    if _todo_store is None:
        config = _get_config()
        data_dir = config.home / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _todo_store = TodoStore(data_dir / "todos.db")
    return _todo_store


# ── Pydantic models ───────────────────────────────────────────────────

class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    top_n: int = Field(5, ge=1, le=20)


class InjectKnowledgeRequest(BaseModel):
    domain: str = Field(..., min_length=1, max_length=100)
    section: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=10000)
    dry_run: bool = Field(True, description="Preview mode — show what would be written without writing")


class ReviewActionRequest(BaseModel):
    entry_id: str = Field(..., min_length=1)
    dry_run: bool = Field(True)
    note: str = Field("", max_length=500)


class CompactRequest(BaseModel):
    dry_run: bool = Field(True, description="Preview mode — show what would be compacted")


class UpdateTodoRequest(BaseModel):
    todo_id: str = Field(..., min_length=1)
    status: str = Field(..., pattern="^(pending|in_progress|completed|cancelled)$")
    dry_run: bool = Field(True)


class RebuildVectorsRequest(BaseModel):
    dry_run: bool = Field(True)


# ── Helper: ensure imports are available ──────────────────────────────

def _check_imports():
    if MemoryConfig is None:
        raise HTTPException(
            status_code=503,
            detail="layered_memory_mcp package not available in Dashboard environment. "
                   "Ensure the package is installed in the same Python environment as Hermes.",
        )


# ── API Endpoints ─────────────────────────────────────────────────────

@router.get("/l0-index")
async def get_l0_index():
    """Return the L0 index as a structured tree."""
    _check_imports()
    config = _get_config()
    l0_path = config.home / "L0.md"

    if not l0_path.exists():
        return {"entries": [], "total": 0, "source": "L0.md not found"}

    content = l0_path.read_text(encoding="utf-8")
    entries = []
    for line in content.splitlines():
        line = line.strip()
        if not line or "→" not in line:
            continue
        left, right = line.split("→", 1)
        # right side is the L1 file path, e.g. "knowledge/ashare-data-platforms.md"
        filename = right.strip().split("/")[-1]
        if not filename.endswith(".md"):
            continue
        # left side: "[L0] domain: summary text"
        left = left.strip()
        # strip leading marker like "[L0]" or "[xxx]"
        if left.startswith("["):
            close = left.find("]")
            if close != -1:
                left = left[close + 1:].strip()
        # domain is the token before the first colon
        if ":" in left:
            domain = left.split(":", 1)[0].strip()
            summary = left.split(":", 1)[1].strip()
        else:
            domain = filename[:-3]
            summary = left
        entries.append({"domain": domain, "filename": filename, "summary": summary})

    return {"entries": entries, "total": len(entries), "source": str(l0_path)}


@router.get("/knowledge-file/{filename}")
async def get_knowledge_file(filename: str):
    """Return the content of a specific L1 knowledge file."""
    _check_imports()
    config = _get_config()

    # Security: only allow .md files, no path traversal
    if ".." in filename or "/" in filename or not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = config.knowledge_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    content = file_path.read_text(encoding="utf-8")
    stat = file_path.stat()

    return {
        "filename": filename,
        "content": content,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


@router.post("/semantic-search")
async def semantic_search(req: SemanticSearchRequest):
    """Perform semantic search over the knowledge base."""
    _check_imports()

    def _search():
        stores = _get_v2_stores()
        vector_store = stores["vector"]
        results = vector_store.search(req.query, top_n=req.top_n)
        return [
            {
                "id": r.get("id", ""),
                "domain": r.get("domain", ""),
                "section": (r.get("metadata") or {}).get("section", ""),
                "summary": (r.get("metadata") or {}).get("summary") or r.get("text", ""),
                "score": round(float(r.get("score", 0)), 4),
                "confidence": (r.get("metadata") or {}).get("confidence"),
            }
            for r in results
        ]

    results = await asyncio.to_thread(_search)
    return {"query": req.query, "results": results, "total": len(results)}


@router.get("/pending-reviews")
async def get_pending_reviews(limit: int = Query(20, ge=1, le=100)):
    """Return pending knowledge review queue."""
    _check_imports()

    def _get():
        stores = _get_v2_stores()
        review_queue = stores["review"]
        return review_queue.list_pending(limit=limit)

    items = await asyncio.to_thread(_get)
    return {
        "items": [
            {
                "id": item.entry.id,
                "domain": item.entry.domain,
                "section": item.entry.section,
                "summary": item.entry.summary,
                "type": item.entry.type.value if hasattr(item.entry.type, "value") else str(item.entry.type),
                "confidence": round(item.entry.confidence, 2),
                "extracted_at": item.extracted_at.isoformat() if hasattr(item.extracted_at, "isoformat") else str(item.extracted_at),
            }
            for item in items
        ],
        "total": len(items),
    }


@router.post("/approve-knowledge")
async def approve_knowledge(req: ReviewActionRequest):
    """Approve a pending knowledge entry."""
    _check_imports()

    if req.dry_run:
        return {
            "dry_run": True,
            "action": "approve",
            "entry_id": req.entry_id,
            "message": "Preview: This will approve the knowledge entry and move it to L1.",
        }

    def _approve():
        stores = _get_v2_stores()
        review_queue = stores["review"]
        item = review_queue.get(req.entry_id)
        if not item:
            raise HTTPException(status_code=404, detail="Entry not found in review queue")
        item.approve(reviewer="dashboard", note=req.note)
        review_queue.update(item)
        # Sync to vector store
        sync_to_vector_store(
            data_dir=_get_config().home / "data",
            domain=item.entry.domain,
            content=item.entry.content,
            summary=item.entry.summary,
        )
        return item

    item = await asyncio.to_thread(_approve)
    return {
        "dry_run": False,
        "action": "approved",
        "entry_id": req.entry_id,
        "domain": item.entry.domain,
        "message": "Knowledge entry approved and synced to vector store.",
    }


@router.post("/reject-knowledge")
async def reject_knowledge(req: ReviewActionRequest):
    """Reject a pending knowledge entry."""
    _check_imports()

    if req.dry_run:
        return {
            "dry_run": True,
            "action": "reject",
            "entry_id": req.entry_id,
            "message": "Preview: This will reject the knowledge entry and remove it from the review queue.",
        }

    def _reject():
        stores = _get_v2_stores()
        review_queue = stores["review"]
        item = review_queue.get(req.entry_id)
        if not item:
            raise HTTPException(status_code=404, detail="Entry not found in review queue")
        item.reject(reviewer="dashboard", note=req.note)
        review_queue.update(item)
        return item

    item = await asyncio.to_thread(_reject)
    return {
        "dry_run": False,
        "action": "rejected",
        "entry_id": req.entry_id,
        "message": "Knowledge entry rejected.",
    }


@router.post("/inject-knowledge")
async def inject_knowledge_endpoint(req: InjectKnowledgeRequest):
    """Inject knowledge into L1. Supports dry-run preview."""
    _check_imports()

    if req.dry_run:
        return {
            "dry_run": True,
            "domain": req.domain,
            "section": req.section,
            "content_preview": req.content[:500] + ("..." if len(req.content) > 500 else ""),
            "message": "Preview: This will create or update the L1 knowledge file. No changes made.",
        }

    def _inject():
        result = inject_knowledge(
            domain=req.domain,
            section=req.section,
            content=req.content,
            mode="upsert",
        )
        return result

    result = await asyncio.to_thread(_inject)
    return {
        "dry_run": False,
        "domain": req.domain,
        "section": req.section,
        "action": result.get("write_action", "unknown"),
        "l0_synced": result.get("l0_synced", False),
        "message": result.get("message", "Knowledge injected successfully."),
    }


@router.post("/compact-memory")
async def compact_memory_endpoint(req: CompactRequest):
    """Trigger memory compaction. Supports dry-run preview."""
    _check_imports()

    if req.dry_run:
        return {
            "dry_run": True,
            "message": "Preview: This will scan agent memory for bloat entries and migrate them to L1. No changes made.",
        }

    def _compact():
        from layered_memory_mcp.memory_compactor import compact_memory
        config = _get_config()
        return compact_memory(config=config, dry_run=False)

    result = await asyncio.to_thread(_compact)
    return {
        "dry_run": False,
        "migrated": result.get("migrated", 0),
        "removed": result.get("removed", 0),
        "message": result.get("message", "Memory compaction completed."),
    }


@router.get("/health")
async def get_health():
    """Aggregate health diagnostics: audit_rot + validate + vector stats + todos."""
    _check_imports()

    def _health():
        config = _get_config()
        data_dir = config.home / "data"

        # Audit rot
        from layered_memory_mcp.rot_auditor import audit_rot
        from layered_memory_mcp.config import MemoryConfig as _MC
        rot_result = audit_rot(_MC())

        # L0-L1 consistency
        l0_path = config.home / "L0.md"
        consistency = {"l0_exists": l0_path.exists(), "l0_size": l0_path.stat().st_size if l0_path.exists() else 0}

        # Vector store stats
        stores = _get_v2_stores()
        vector_store = stores["vector"]
        vector_stats = vector_store.stats()

        # TODO stats — count by status
        todo_store = _get_todo_store()
        todo_items = todo_store.list(limit=1000)
        from collections import Counter
        status_counts = Counter(item.get("status", "unknown") for item in todo_items)
        todo_stats = {
            "total": len(todo_items),
            "by_status": dict(status_counts),
        }

        return {
            "rot": rot_result,
            "consistency": consistency,
            "vector": vector_stats,
            "todos": todo_stats,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    result = await asyncio.to_thread(_health)
    return result


@router.get("/todos")
async def get_todos(
    status: Optional[str] = Query(None, pattern="^(pending|in_progress|completed|cancelled)$"),
    limit: int = Query(50, ge=1, le=200),
):
    """List TODO items."""
    _check_imports()

    def _list():
        todo_store = _get_todo_store()
        return todo_store.list(status=status, limit=limit)

    items = await asyncio.to_thread(_list)
    return {
        "items": [
            {
                "id": item["id"],
                "domain": item["domain"],
                "title": item["title"],
                "content": item["content"],
                "status": item["status"],
                "priority": item["priority"],
                "created_at": item["created_at"],
            }
            for item in items
        ],
        "total": len(items),
    }


@router.post("/update-todo")
async def update_todo_endpoint(req: UpdateTodoRequest):
    """Update a TODO item status. Supports dry-run preview."""
    _check_imports()

    if req.dry_run:
        return {
            "dry_run": True,
            "todo_id": req.todo_id,
            "new_status": req.status,
            "message": f"Preview: This will update TODO {req.todo_id} status to '{req.status}'. No changes made.",
        }

    def _update():
        todo_store = _get_todo_store()
        todo_store.update(req.todo_id, status=req.status)
        return True

    await asyncio.to_thread(_update)
    return {
        "dry_run": False,
        "todo_id": req.todo_id,
        "status": req.status,
        "message": "TODO updated successfully.",
    }


@router.post("/rebuild-vectors")
async def rebuild_vectors_endpoint(req: RebuildVectorsRequest):
    """Rebuild vector store index. Supports dry-run preview."""
    _check_imports()

    if req.dry_run:
        return {
            "dry_run": True,
            "message": "Preview: This will re-index all L1 knowledge files into the vector store. This may take a while. No changes made.",
        }

    def _rebuild():
        config = _get_config()
        data_dir = config.home / "data"
        knowledge_dir = config.knowledge_dir

        # Clear and re-index
        stores = _get_v2_stores()
        vector_store = stores["vector"]

        # Re-index all L1 files
        from layered_memory_mcp.models import KnowledgeEntry, KnowledgeType, SourceInfo, SourceType, ReviewStatus
        import uuid

        count = 0
        for fpath in sorted(knowledge_dir.glob("*.md")):
            content = fpath.read_text(encoding="utf-8")
            entry = KnowledgeEntry(
                id=str(uuid.uuid4()),
                domain=fpath.stem,
                section=fpath.stem,
                content=content,
                summary=f"L1: {fpath.stem}",
                type=KnowledgeType.FACT,
                confidence=0.9,
                review_status=ReviewStatus.APPROVED,
                source=SourceInfo(type=SourceType.MANUAL, extracted_by="dashboard_rebuild"),
            )
            vector_store.add(entry)
            count += 1

        return {"indexed": count}

    result = await asyncio.to_thread(_rebuild)
    return {
        "dry_run": False,
        "indexed": result["indexed"],
        "message": f"Vector store rebuilt with {result['indexed']} entries.",
    }


# ── #2: Token Economy — quantify the "load on demand" savings ──────────

@router.get("/token-economy")
async def get_token_economy():
    """Quantify the core value: L0 index (always resident) vs full L1 corpus.

    Returns char counts and the savings ratio that justifies the
    layered-on-demand design. Uses a ~4 chars/token heuristic for an
    approximate token estimate.
    """
    _check_imports()

    def _measure():
        config = _get_config()
        l0_path = config.home / "L0.md"
        knowledge_dir = config.knowledge_dir

        l0_chars = l0_path.stat().st_size if l0_path.exists() else 0

        # Sum all L1 files
        l1_total_chars = 0
        per_file = []
        for fpath in sorted(knowledge_dir.glob("*.md")):
            size = fpath.stat().st_size
            l1_total_chars += size
            per_file.append({"domain": fpath.stem, "chars": size})
        per_file.sort(key=lambda x: x["chars"], reverse=True)

        domain_count = len(per_file)
        full_load = l0_chars + l1_total_chars  # naive: everything in context
        resident = l0_chars                    # layered: only L0 resident
        saved = full_load - resident
        saved_pct = round(saved / full_load * 100, 1) if full_load else 0.0

        # ~4 chars per token heuristic (rough, for intuition only)
        CHARS_PER_TOKEN = 4
        return {
            "l0_chars": l0_chars,
            "l1_total_chars": l1_total_chars,
            "domain_count": domain_count,
            "full_load_chars": full_load,
            "resident_chars": resident,
            "saved_chars": saved,
            "saved_pct": saved_pct,
            "l0_tokens_est": round(l0_chars / CHARS_PER_TOKEN),
            "full_tokens_est": round(full_load / CHARS_PER_TOKEN),
            "saved_tokens_est": round(saved / CHARS_PER_TOKEN),
            "chars_per_token": CHARS_PER_TOKEN,
            "top_files": per_file[:8],
        }

    return await asyncio.to_thread(_measure)


# ── #3: Consistency check — verify the three-write guarantee ───────────

@router.get("/consistency")
async def get_consistency():
    """Verify L0 / L1 / vector-store three-way consistency.

    Surfaces orphaned L1 files (in L1 but missing from L0), stale L0
    entries (point to non-existent L1), and whether the vector store
    covers every L1 domain. This is the visible proof of the framework's
    "three writes stay in sync" guarantee.
    """
    _check_imports()

    def _check():
        config = _get_config()
        knowledge_dir = config.knowledge_dir
        l0_path = config.home / "L0.md"

        # L1 domains on disk
        l1_domains = {p.stem for p in knowledge_dir.glob("*.md")}

        # L0 referenced domains — parse L0.md directly (robust even when
        # config.l0_index_file is None in Hermes inline-memory mode)
        l0_domains: set[str] = set()
        if l0_path.exists():
            for line in l0_path.read_text(encoding="utf-8").splitlines():
                if "→" not in line:
                    continue
                right = line.split("→", 1)[1].strip()
                fname = right.split("/")[-1]
                if fname.endswith(".md"):
                    l0_domains.add(fname[:-3])

        orphaned_l1 = sorted(l1_domains - l0_domains)      # on disk, not in L0
        stale_l0 = sorted(l0_domains - l1_domains)          # in L0, file gone
        consistent = sorted(l1_domains & l0_domains)

        # Vector store coverage
        stores = _get_v2_stores()
        vector_store = stores["vector"]
        vstats = vector_store.stats()
        # domains present in vector store
        v_domains: set[str] = set()
        try:
            import sqlite3
            db_path = config.home / "data" / "vectors.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                try:
                    for (d,) in conn.execute("SELECT DISTINCT domain FROM vectors"):
                        if d:
                            v_domains.add(d)
                finally:
                    conn.close()
        except Exception as exc:
            logger.warning("vector domain scan failed: %s", exc)

        missing_in_vector = sorted(l1_domains - v_domains)  # L1 not indexed

        issues = len(orphaned_l1) + len(stale_l0) + len(missing_in_vector)
        if issues == 0:
            status = "healthy"
        elif issues <= 2:
            status = "minor"
        else:
            status = "needs_attention"

        return {
            "status": status,
            "issue_count": issues,
            "l1_count": len(l1_domains),
            "l0_count": len(l0_domains),
            "vector_count": len(v_domains),
            "consistent_count": len(consistent),
            "orphaned_l1": orphaned_l1,
            "stale_l0_entries": stale_l0,
            "missing_in_vector": missing_in_vector,
            "vector_fitted": bool(vstats.get("is_fitted", False)),
            "checks": [
                {
                    "key": "l0_l1",
                    "label": "L0 ↔ L1 索引一致",
                    "ok": not orphaned_l1 and not stale_l0,
                    "detail": (
                        "L0 索引与 L1 文件完全对应"
                        if not orphaned_l1 and not stale_l0
                        else f"{len(orphaned_l1)} 个 L1 未被索引 / {len(stale_l0)} 个 L0 指针失效"
                    ),
                },
                {
                    "key": "l1_vector",
                    "label": "L1 ↔ 向量库覆盖",
                    "ok": not missing_in_vector,
                    "detail": (
                        "每个 L1 文件都已建立向量索引"
                        if not missing_in_vector
                        else f"{len(missing_in_vector)} 个 L1 文件未进入向量库"
                    ),
                },
                {
                    "key": "vector_fitted",
                    "label": "向量模型就绪",
                    "ok": bool(vstats.get("is_fitted", False)),
                    "detail": (
                        "向量检索模型已训练，语义搜索可用"
                        if vstats.get("is_fitted", False)
                        else "向量模型未训练，语义搜索不可用（需重建向量）"
                    ),
                },
            ],
        }

    return await asyncio.to_thread(_check)


# ── #1: Search explain — make the hybrid retrieval visible ─────────────

@router.post("/search-explain")
async def search_explain(req: SemanticSearchRequest):
    """Semantic search with per-result explanation.

    Runs the vector search and, for each hit, surfaces which query terms
    overlap with the matched domain/section so the user can see *why* a
    result ranked where it did — turning black-box retrieval into
    explainable retrieval.
    """
    _check_imports()

    def _search():
        stores = _get_v2_stores()
        vector_store = stores["vector"]
        vstats = vector_store.stats()
        results = vector_store.search(req.query, top_n=req.top_n)

        # tokenize query for overlap highlighting
        import re
        q_terms = [t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+", req.query) if len(t) > 1]

        # vector_store.search returns list[dict]: {id, domain, text, metadata, score}
        scores = [float(r.get("score", 0)) for r in results] or [0.0]
        max_score = max(scores) if scores else 1.0

        items = []
        for r in results:
            meta = r.get("metadata") or {}
            domain = r.get("domain", "")
            section = meta.get("section", "")
            summary = meta.get("summary") or r.get("text", "")
            haystack = f"{domain} {section} {summary}".lower()
            matched = sorted({t for t in q_terms if t in haystack})
            raw = float(r.get("score", 0))
            rel = round(raw / max_score * 100) if max_score > 0 else 0
            items.append({
                "id": r.get("id", ""),
                "domain": domain,
                "section": section,
                "summary": summary[:200],
                "score": round(raw, 4),
                "relative_pct": rel,
                "confidence": meta.get("confidence"),
                "matched_terms": matched,
            })

        return {
            "query": req.query,
            "query_terms": q_terms,
            "method": "TF-IDF + SVD 向量检索（余弦相似度）",
            "vector_fitted": bool(vstats.get("is_fitted", False)),
            "total_indexed": vstats.get("total_entries", 0),
            "results": items,
        }

    return await asyncio.to_thread(_search)

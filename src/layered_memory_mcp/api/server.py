"""FastAPI server for Layered Memory v2.0 REST API.

Endpoints:
  POST /knowledge          — Create knowledge entry
  GET  /knowledge/{id}     — Get entry by ID
  PUT  /knowledge/{id}     — Update entry
  DELETE /knowledge/{id}   — Delete entry
  GET  /knowledge/search   — Semantic search
  GET  /domains            — List all domains
  GET  /review/queue       — List pending review items
  POST /review/{id}/approve — Approve review item
  POST /review/{id}/reject  — Reject review item
  GET  /health             — Health check
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import get_config
from ..models import (
    ConfidenceScorer,
    KnowledgeEntry,
    KnowledgeType,
    ReviewItem,
    ReviewStatus,
    SourceInfo,
    SourceType,
)
from ..storage import L1Store, ReviewQueue, VectorStore

logger = logging.getLogger("layered_memory_mcp.api")

# ---------------------------------------------------------------------------
# Pydantic models for API
# ---------------------------------------------------------------------------


class CreateKnowledgeRequest(BaseModel):
    domain: str = Field(..., min_length=1, max_length=64)
    section: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1)
    type: KnowledgeType = KnowledgeType.FACT
    summary: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    auto_approve: bool = Field(default=True)


class UpdateKnowledgeRequest(BaseModel):
    section: str | None = None
    content: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    confidence: float | None = None


class ReviewActionRequest(BaseModel):
    reviewer: str = "human"
    note: str = ""


class KnowledgeResponse(BaseModel):
    id: str
    domain: str
    section: str
    type: str
    content: str
    summary: str
    confidence: float
    review_status: str
    tags: list[str]
    created_at: str


class SearchResult(BaseModel):
    id: str
    domain: str
    text: str
    score: float
    metadata: dict


class ReviewQueueItem(BaseModel):
    id: str
    domain: str
    section: str
    type: str
    summary: str
    confidence: float
    submitted_at: str


# ---------------------------------------------------------------------------
# Global state (managed via lifespan)
# ---------------------------------------------------------------------------

stores: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize stores on startup."""
    cfg = get_config()
    knowledge_dir = Path(cfg.get("knowledge_dir", "~/.hermes/knowledge")).expanduser()
    data_dir = Path(cfg.get("data_dir", "~/.hermes/data")).expanduser()

    stores["l1"] = L1Store(knowledge_dir)
    stores["vector"] = VectorStore(data_dir / "vectors.db")
    stores["review"] = ReviewQueue(data_dir / "review_queue.db")

    logger.info("API initialized: knowledge_dir=%s", knowledge_dir)
    yield
    # Cleanup
    stores.clear()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Layered Memory MCP API",
        description="Structured knowledge management for AI agents",
        version="2.0.0",
        lifespan=lifespan,
    )

    # -----------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": "2.0.0",
            "stores": {
                "l1": stores.get("l1") is not None,
                "vector": stores.get("vector") is not None,
                "review": stores.get("review") is not None,
            },
        }

    # -----------------------------------------------------------------------
    # Knowledge CRUD
    # -----------------------------------------------------------------------

    @app.post("/knowledge", response_model=KnowledgeResponse)
    async def create_knowledge(req: CreateKnowledgeRequest) -> dict:
        """Create a new knowledge entry."""
        entry = KnowledgeEntry(
            domain=req.domain,
            section=req.section,
            type=req.type,
            content=req.content,
            summary=req.summary,
            tags=req.tags,
            confidence=req.confidence,
            source=SourceInfo(type=SourceType.MANUAL, extracted_by="api"),
        )

        if req.auto_approve:
            ConfidenceScorer.auto_review(entry, threshold=0.9)

        # Write to L1
        result = stores["l1"].write(entry)
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error", "Write failed"))

        # Index in vector store
        stores["vector"].add(entry)

        # If pending, add to review queue
        if entry.review_status == ReviewStatus.PENDING:
            stores["review"].submit(ReviewItem(entry=entry))

        return _entry_to_response(entry)

    @app.get("/knowledge/{entry_id}", response_model=KnowledgeResponse)
    async def get_knowledge(entry_id: str) -> dict:
        """Get a knowledge entry by ID."""
        # Search all domains
        for domain in stores["l1"].list_domains():
            meta, content = stores["l1"].read(domain)
            if meta and meta.get("id") == entry_id:
                entry = KnowledgeEntry.from_markdown(
                    stores["l1"]._resolve_path(domain).read_text(),
                    domain=domain,
                )
                return _entry_to_response(entry)

        raise HTTPException(status_code=404, detail="Entry not found")

    @app.put("/knowledge/{entry_id}", response_model=KnowledgeResponse)
    async def update_knowledge(entry_id: str, req: UpdateKnowledgeRequest) -> dict:
        """Update a knowledge entry."""
        # Find entry
        entry = None
        entry_domain = None
        for domain in stores["l1"].list_domains():
            meta, content = stores["l1"].read(domain)
            if meta and meta.get("id") == entry_id:
                entry = KnowledgeEntry.from_markdown(
                    stores["l1"]._resolve_path(domain).read_text(),
                    domain=domain,
                )
                entry_domain = domain
                break

        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Apply updates
        if req.section is not None:
            entry.section = req.section
        if req.content is not None:
            entry.content = req.content
        if req.summary is not None:
            entry.summary = req.summary
        if req.tags is not None:
            entry.tags = req.tags
        if req.confidence is not None:
            entry.confidence = req.confidence

        entry.bump_version()

        # Write back
        result = stores["l1"].write(entry)
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error"))

        # Update vector store
        stores["vector"].add(entry)

        return _entry_to_response(entry)

    @app.delete("/knowledge/{entry_id}")
    async def delete_knowledge(entry_id: str) -> dict:
        """Delete a knowledge entry."""
        # Find and delete
        for domain in stores["l1"].list_domains():
            meta, content = stores["l1"].read(domain)
            if meta and meta.get("id") == entry_id:
                stores["l1"].delete(domain)
                stores["vector"].delete(entry_id)
                return {"success": True, "id": entry_id}

        raise HTTPException(status_code=404, detail="Entry not found")

    @app.get("/knowledge/search", response_model=list[SearchResult])
    async def search_knowledge(
        q: str = Query(..., min_length=1),
        top_n: int = Query(5, ge=1, le=20),
        domain: str | None = None,
    ) -> list[dict]:
        """Semantic search for knowledge entries."""
        results = stores["vector"].search(q, top_n=top_n, domain=domain)
        return [
            SearchResult(
                id=r["id"],
                domain=r["domain"],
                text=r["text"],
                score=r["score"],
                metadata=r["metadata"],
            ).model_dump()
            for r in results
        ]

    # -----------------------------------------------------------------------
    # Domains
    # -----------------------------------------------------------------------

    @app.get("/domains")
    async def list_domains() -> list[str]:
        """List all knowledge domains."""
        return stores["l1"].list_domains()

    # -----------------------------------------------------------------------
    # Review Queue
    # -----------------------------------------------------------------------

    @app.get("/review/queue", response_model=list[ReviewQueueItem])
    async def list_review_queue(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> list[dict]:
        """List pending review items."""
        items = stores["review"].list_pending(limit=limit, offset=offset)
        return [
            ReviewQueueItem(
                id=item["id"],
                domain=item["entry"].domain,
                section=item["entry"].section,
                type=item["entry"].type.value,
                summary=item["entry"].summary,
                confidence=item["entry"].confidence,
                submitted_at=item["submitted_at"],
            ).model_dump()
            for item in items
        ]

    @app.post("/review/{item_id}/approve")
    async def approve_review(item_id: str, req: ReviewActionRequest) -> dict:
        """Approve a pending review item."""
        result = stores["review"].approve(item_id, reviewer=req.reviewer, note=req.note)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))

        # Move from review queue to main storage
        # (In v2.0, items are already in L1 storage with PENDING status)
        # Just update the status in L1
        for domain in stores["l1"].list_domains():
            meta, content = stores["l1"].read(domain)
            if meta and meta.get("id") == item_id:
                entry = KnowledgeEntry.from_markdown(
                    stores["l1"]._resolve_path(domain).read_text(),
                    domain=domain,
                )
                entry.review_status = ReviewStatus.APPROVED
                entry.reviewed_by = req.reviewer
                stores["l1"].write(entry)
                break

        return result

    @app.post("/review/{item_id}/reject")
    async def reject_review(item_id: str, req: ReviewActionRequest) -> dict:
        """Reject a pending review item."""
        result = stores["review"].reject(item_id, reviewer=req.reviewer, note=req.note)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))

        # Mark as rejected in L1
        for domain in stores["l1"].list_domains():
            meta, content = stores["l1"].read(domain)
            if meta and meta.get("id") == item_id:
                entry = KnowledgeEntry.from_markdown(
                    stores["l1"]._resolve_path(domain).read_text(),
                    domain=domain,
                )
                entry.review_status = ReviewStatus.REJECTED
                entry.reviewed_by = req.reviewer
                stores["l1"].write(entry)
                break

        return result

    @app.get("/review/stats")
    async def review_stats() -> dict:
        """Get review queue statistics."""
        return stores["review"].get_stats()

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_to_response(entry: KnowledgeEntry) -> dict:
    """Convert KnowledgeEntry to API response dict."""
    return {
        "id": entry.id,
        "domain": entry.domain,
        "section": entry.section,
        "type": entry.type.value,
        "content": entry.content,
        "summary": entry.summary,
        "confidence": entry.confidence,
        "review_status": entry.review_status.value,
        "tags": entry.tags,
        "created_at": entry.created_at.isoformat(),
    }

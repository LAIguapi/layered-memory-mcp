"""
Structured knowledge models for Layered Memory v2.0.

Defines the schema for knowledge entries, sources, and review states.
All knowledge written through the v2.0 API includes frontmatter based on these models.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class KnowledgeType(str, Enum):
    """Classification of knowledge entries."""

    CONFIG = "config"           # Environment setup, API keys, paths
    PITFALL = "pitfall"         # Things that went wrong and how fixed
    DECISION = "decision"       # Architectural or design decisions
    PREFERENCE = "preference"   # User preferences, habits
    FACT = "fact"               # Verified factual information
    PROCEDURE = "procedure"     # Step-by-step workflows
    RELATIONSHIP = "relationship"  # Connections between concepts


class ReviewStatus(str, Enum):
    """Review state for knowledge entries."""

    PENDING = "pending"         # Auto-extracted, awaiting review
    APPROVED = "approved"       # Human or high-confidence auto-approved
    REJECTED = "rejected"       # Human rejected


class SourceType(str, Enum):
    """Type of knowledge source."""

    SESSION = "session"         # Extracted from agent session
    MANUAL = "manual"           # Human-written
    IMPORTED = "imported"       # Imported from external source
    MIGRATED = "migrated"       # Migrated from legacy format


class TodoStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TodoPriority(str, Enum):
    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    WAITING = "waiting"


class TodoEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    domain: str
    title: str = ""                         # 短标题
    content: str
    blocked_by: list[str] = Field(default_factory=list)  # 被哪些 TODO 阻塞
    priority: TodoPriority = TodoPriority.MEDIUM
    status: TodoStatus = TodoStatus.PENDING
    source_session_id: str | None = None
    notes: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class SourceInfo(BaseModel):
    """Provenance information for a knowledge entry."""

    type: SourceType = SourceType.MANUAL
    session_id: str | None = None
    message_range: tuple[int, int] | None = None
    extracted_by: str = "human"
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        d = super().model_dump(**kwargs)
        # Convert enums to strings
        for key in list(d.keys()):
            if isinstance(d[key], Enum):
                d[key] = d[key].value
        # Convert datetime to ISO format for YAML compatibility
        if isinstance(d.get("extracted_at"), datetime):
            d["extracted_at"] = d["extracted_at"].isoformat()
        return d


class KnowledgeEntry(BaseModel):
    """A single structured knowledge entry.

    This is the core data model for v2.0. Every piece of knowledge
    written through the system should conform to this schema.
    """

    # Identity
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    domain: str = Field(..., min_length=1, max_length=64)
    section: str = Field(..., min_length=1, max_length=128)

    # Content
    type: KnowledgeType = KnowledgeType.FACT
    content: str = Field(..., min_length=1)
    summary: str = Field(default="", max_length=200)

    # Metadata
    source: SourceInfo = Field(default_factory=SourceInfo)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = Field(default=1, ge=1)

    # Relationships
    related_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    # Review state
    review_status: ReviewStatus = ReviewStatus.APPROVED
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    @field_validator("domain", "section")
    @classmethod
    def _no_newlines(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("Field must not contain newlines")
        return v

    @field_validator("tags")
    @classmethod
    def _tags_lowercase(cls, v: list[str]) -> list[str]:
        return [t.lower().strip() for t in v if t.strip()]

    def bump_version(self) -> None:
        """Increment version and update timestamp."""
        self.version += 1
        self.updated_at = datetime.now(timezone.utc)

    def to_frontmatter(self) -> str:
        """Serialize entry metadata to YAML frontmatter."""
        lines = ["---"]
        for key, value in self.model_dump(exclude={"content"}).items():
            if value is None:
                continue
            if isinstance(value, list):
                if not value:
                    continue
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {item}")
            elif isinstance(value, dict):
                lines.append(f"{key}:")
                for k, v in value.items():
                    if v is not None:
                        lines.append(f"  {k}: {v}")
            else:
                lines.append(f"{key}: {value}")
        lines.append("---")
        return "\n".join(lines)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        d = super().model_dump(**kwargs)
        # Recursively convert enums to strings
        def _convert(obj):
            if isinstance(obj, Enum):
                return obj.value
            elif isinstance(obj, dict):
                return {k: _convert(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [_convert(v) for v in obj]
            elif isinstance(obj, datetime):
                return obj.isoformat()
            return obj
        return _convert(d)

    def to_markdown(self) -> str:
        """Serialize full entry to markdown with frontmatter."""
        parts = [self.to_frontmatter(), ""]
        parts.append(f"## {self.section}")
        parts.append("")
        parts.append(self.content)
        return "\n".join(parts)

    @classmethod
    def from_markdown(cls, markdown: str, domain: str = "") -> KnowledgeEntry:
        """Parse a markdown string with YAML frontmatter into a KnowledgeEntry.

        Falls back to legacy mode (no frontmatter) for v1.x files.
        """
        from .storage.frontmatter import parse_frontmatter

        meta, content = parse_frontmatter(markdown)
        if not meta:
            # Legacy mode: no frontmatter
            return cls(
                domain=domain,
                section="Legacy",
                content=markdown.strip(),
                review_status=ReviewStatus.APPROVED,
            )

        # Map meta fields to model
        kwargs: dict[str, Any] = {
            "domain": meta.get("domain", domain),
            "section": meta.get("section", "General"),
            "content": content.strip(),
            "type": KnowledgeType(meta.get("type", "fact")),
            "summary": meta.get("summary", ""),
            "confidence": float(meta.get("confidence", 0.5)),
            "version": int(meta.get("version", 1)),
            "related_ids": meta.get("related_ids", []),
            "tags": meta.get("tags", []),
            "review_status": ReviewStatus(meta.get("review_status", "approved")),
            "reviewed_by": meta.get("reviewed_by"),
        }

        # Handle datetime fields
        for dt_field in ("created_at", "updated_at", "reviewed_at"):
            if dt_field in meta and meta[dt_field]:
                if isinstance(meta[dt_field], str):
                    kwargs[dt_field] = datetime.fromisoformat(meta[dt_field].replace("Z", "+00:00"))
                else:
                    kwargs[dt_field] = meta[dt_field]

        # Handle source
        if "source" in meta:
            src = meta["source"]
            if isinstance(src, dict):
                kwargs["source"] = SourceInfo(**src)

        return cls(**kwargs)


class ReviewItem(BaseModel):
    """An item in the review queue."""

    entry: KnowledgeEntry
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    review_note: str | None = None

    def approve(self, reviewer: str = "human", note: str = "") -> None:
        """Approve this review item."""
        self.entry.review_status = ReviewStatus.APPROVED
        self.entry.reviewed_by = reviewer
        self.entry.reviewed_at = datetime.now(timezone.utc)
        self.reviewed_at = self.entry.reviewed_at
        self.reviewed_by = reviewer
        self.review_note = note or None

    def reject(self, reviewer: str = "human", note: str = "") -> None:
        """Reject this review item."""
        self.entry.review_status = ReviewStatus.REJECTED
        self.entry.reviewed_by = reviewer
        self.entry.reviewed_at = datetime.now(timezone.utc)
        self.reviewed_at = self.entry.reviewed_at
        self.reviewed_by = reviewer
        self.review_note = note or None


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

class ConfidenceScorer:
    """Score the confidence of an extracted knowledge entry."""

    # Decision markers that indicate high-confidence knowledge
    DECISION_WORDS = frozenset({
        "解决", "修复", "确认", "决定", "方案", "结论", "根因",
        "fixed", "resolved", "confirmed", "decided", "solution",
        "conclusion", "root cause", "verified", "determined",
    })

    VERIFICATION_WORDS = frozenset({
        "测试通过", "验证", "成功", "生效",
        "worked", "verified", "tested", "confirmed", "passed",
        "succeeded", "validated",
    })

    PROCEDURE_WORDS = frozenset({
        "步骤", "流程", "命令", "执行", "运行",
        "step", "run", "execute", "command", "install", "configure",
        "setup", "deploy", "build", "start", "stop",
    })

    @classmethod
    def score(cls, entry: KnowledgeEntry) -> float:
        """Calculate confidence score for a knowledge entry.

        Returns a value between 0.0 and 1.0.
        """
        scores: list[float] = []
        content = entry.content.lower()

        # 1. Source quality (0-0.25)
        if entry.source.type == SourceType.SESSION:
            if entry.source.message_range:
                msg_count = entry.source.message_range[1] - entry.source.message_range[0]
                if msg_count >= 20:
                    scores.append(0.25)
                elif msg_count >= 10:
                    scores.append(0.20)
                elif msg_count >= 5:
                    scores.append(0.15)
                else:
                    scores.append(0.10)
            else:
                scores.append(0.10)
        elif entry.source.type == SourceType.MANUAL:
            scores.append(0.25)  # Human-written is high confidence
        else:
            scores.append(0.15)

        # 2. Content specificity (0-0.25)
        specificity_signals = 0
        if "`" in entry.content or "```" in entry.content:
            specificity_signals += 1  # Code/commands
        if "http" in content or "@" in entry.content:
            specificity_signals += 1  # URLs or emails
        if any(cmd in content for cmd in ["$ ", "python", "npm", "git", "docker", "pip", "apt"]):
            specificity_signals += 1  # Commands
        if any(c.isdigit() for c in entry.content):
            specificity_signals += 1  # Numbers/versions
        scores.append(min(specificity_signals / 4 * 0.25, 0.25))

        # 3. Decision markers (0-0.25)
        has_decision = any(w in content for w in cls.DECISION_WORDS)
        scores.append(0.25 if has_decision else 0.0)

        # 4. Verification markers (0-0.25)
        has_verify = any(w in content for w in cls.VERIFICATION_WORDS)
        scores.append(0.25 if has_verify else 0.0)

        return min(sum(scores), 1.0)

    @classmethod
    def auto_review(cls, entry: KnowledgeEntry, threshold: float = 0.9) -> ReviewStatus:
        """Automatically determine review status based on confidence.

        Args:
            entry: The knowledge entry to evaluate.
            threshold: Confidence threshold for auto-approval.

        Returns:
            ReviewStatus.APPROVED if confidence >= threshold,
            ReviewStatus.PENDING otherwise.
        """
        score = cls.score(entry)
        entry.confidence = round(score, 3)
        if score >= threshold:
            entry.review_status = ReviewStatus.APPROVED
            entry.reviewed_by = "auto"
            entry.reviewed_at = datetime.now(timezone.utc)
        else:
            entry.review_status = ReviewStatus.PENDING
        return entry.review_status

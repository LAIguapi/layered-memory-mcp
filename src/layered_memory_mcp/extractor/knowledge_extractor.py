"""Structured knowledge extraction from sessions.

Uses rule-based + heuristic extraction to identify knowledge-worthy content
from full session transcripts. Produces KnowledgeEntry objects.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import (
    ConfidenceScorer,
    KnowledgeEntry,
    KnowledgeType,
    ReviewItem,
    SourceInfo,
    SourceType,
)

if TYPE_CHECKING:
    from .session_reader import Message, Session

logger = logging.getLogger("layered_memory_mcp.extractor")

# Markers that indicate knowledge-worthy content
DECISION_MARKERS = [
    "找到根因", "根因", "根本原因", "修复完成", "已修复", "已解决",
    "解决方案", "结论", "决策", "决定", "验证通过", "测试通过",
    "问题确认", "确认", "最终", "总结", "方案", "架构",
    "root cause", "fixed", "solution", "conclusion", "decided",
    "verified", "confirmed", "resolved", "architecture",
    "determined", "identified", "discovered",
]

CONFIG_MARKERS = [
    "配置", "设置", "路径", "地址", "端口", "token", "key",
    "config", "configure", "path", "address", "port", "setting",
    "environment variable", "env var", "installed at",
]

PITFALL_MARKERS = [
    "错误", "失败", "异常", "坑", "注意", "警告",
    "error", "failed", "exception", "bug", "issue", "warning",
    "caution", "beware", "not working", "doesn't work",
]

PROCEDURE_MARKERS = [
    "步骤", "流程", "命令", "执行", "运行", "安装",
    "step", "run", "execute", "command", "install", "setup",
    "deploy", "build", "start", "stop", "how to",
]

PREFERENCE_MARKERS = [
    "偏好", "喜欢", "习惯", "倾向", "要求",
    "prefer", "like", "habit", "always", "never", "must",
    "should", "require", "want",
]


def _detect_knowledge_type(content: str) -> KnowledgeType:
    """Detect the type of knowledge based on content markers."""
    content_lower = content.lower()

    scores = {
        KnowledgeType.PITFALL: sum(1 for m in PITFALL_MARKERS if m in content_lower),
        KnowledgeType.CONFIG: sum(1 for m in CONFIG_MARKERS if m in content_lower),
        KnowledgeType.PROCEDURE: sum(1 for m in PROCEDURE_MARKERS if m in content_lower),
        KnowledgeType.DECISION: sum(1 for m in DECISION_MARKERS if m in content_lower),
        KnowledgeType.PREFERENCE: sum(1 for m in PREFERENCE_MARKERS if m in content_lower),
    }

    if max(scores.values()) == 0:
        return KnowledgeType.FACT

    return max(scores, key=scores.get)


def _extract_key_decisions(session: "Session") -> list[dict]:
    """Extract messages that contain decisions or conclusions."""
    decisions = []

    for i, msg in enumerate(session.messages):
        if msg.role != "assistant":
            continue

        content = msg.content or ""
        content_lower = content.lower()

        # Check for decision markers
        has_decision = any(m in content_lower for m in DECISION_MARKERS)
        has_config = any(m in content_lower for m in CONFIG_MARKERS)
        has_pitfall = any(m in content_lower for m in PITFALL_MARKERS)
        has_procedure = any(m in content_lower for m in PROCEDURE_MARKERS)

        if has_decision or has_config or has_pitfall or has_procedure:
            # Get context: previous user message + this assistant message
            context_start = max(0, i - 2)
            context_msgs = session.messages[context_start:i + 1]
            context_text = "\n".join(
                f"{m.role}: {m.content[:300]}"
                for m in context_msgs
                if m.content
            )

            decisions.append({
                "index": i,
                "type": _detect_knowledge_type(content),
                "content": content[:800],  # Truncate very long messages
                "context": context_text,
                "has_decision": has_decision,
                "has_config": has_config,
                "has_pitfall": has_pitfall,
                "has_procedure": has_procedure,
            })

    return decisions


def _generate_summary(content: str, max_chars: int = 120) -> str:
    """Generate a one-line summary from content."""
    # Take first sentence or first line
    lines = content.strip().split("\n")
    first = lines[0].strip()

    # Strip markdown
    clean = re.sub(r"[*_`#]", "", first).strip()

    if len(clean) > max_chars:
        clean = clean[:max_chars - 3] + "..."

    return clean


def _infer_domain(
    content: str,
    fallback: str = "general",
    domain_keywords: dict[str, list[str]] | None = None,
) -> str:
    """Infer the knowledge domain from content.

    The framework ships with **zero** built-in domain presets. Domain
    classification is entirely user-driven: pass a ``domain_keywords`` mapping
    (``{domain: [keyword, ...]}``, typically sourced from
    ``config.domain_keywords``) to enable keyword-based classification.

    When ``domain_keywords`` is empty or ``None``, no matching is performed and
    ``fallback`` is returned for any content — the framework makes no assumption
    about the user's subject matter.
    """
    if not domain_keywords:
        return fallback

    content_lower = content.lower()

    scores = {
        domain: sum(1 for kw in keywords if kw.lower() in content_lower)
        for domain, keywords in domain_keywords.items()
    }

    best = max(scores, key=lambda d: scores[d])
    if scores[best] > 0:
        return best
    return fallback


def _infer_section(content: str, knowledge_type: KnowledgeType) -> str:
    """Infer a section heading from content."""
    # Try to extract a heading from the first line
    lines = content.strip().split("\n")
    for line in lines[:3]:
        stripped = line.strip()
        if stripped.startswith("#"):
            # Extract heading text
            text = re.sub(r"^#+\s*", "", stripped).strip()
            if text:
                return text[:60]

    # Fallback: use knowledge type as section
    type_sections = {
        KnowledgeType.CONFIG: "Configuration",
        KnowledgeType.PITFALL: "Known Issues",
        KnowledgeType.DECISION: "Decisions",
        KnowledgeType.PREFERENCE: "Preferences",
        KnowledgeType.FACT: "Facts",
        KnowledgeType.PROCEDURE: "Procedures",
        KnowledgeType.RELATIONSHIP: "Relationships",
    }
    return type_sections.get(knowledge_type, "General")


class KnowledgeExtractor:
    """Extract structured knowledge from sessions."""

    def __init__(
        self,
        auto_approve_threshold: float = 0.9,
        domain_keywords: dict[str, list[str]] | None = None,
    ):
        self.auto_approve_threshold = auto_approve_threshold
        # User-configured domain classification table (typically
        # config.domain_keywords). Empty/None means the framework makes no
        # assumption about subject matter — every entry falls back to "general".
        self.domain_keywords = domain_keywords or {}

    def extract_from_session(self, session: "Session") -> list[ReviewItem]:
        """Extract knowledge entries from a single session.

        Returns a list of ReviewItems (may be approved or pending).
        """
        decisions = _extract_key_decisions(session)
        items = []

        for decision in decisions:
            content = decision["content"]
            if len(content) < 50:
                # Too short to be meaningful knowledge
                continue

            # Build knowledge entry
            entry = KnowledgeEntry(
                domain=_infer_domain(content, domain_keywords=self.domain_keywords),
                section=_infer_section(content, decision["type"]),
                type=decision["type"],
                content=content,
                summary=_generate_summary(content),
                source=SourceInfo(
                    type=SourceType.SESSION,
                    session_id=session.session_id or Path(session.path).stem,
                    message_range=(decision["index"], decision["index"] + 1),
                    extracted_by="layered-memory-extractor",
                ),
                tags=[],  # Could extract tags from content
            )

            # Score and auto-review
            ConfidenceScorer.auto_review(entry, threshold=self.auto_approve_threshold)

            items.append(ReviewItem(entry=entry))

        return items

    def extract_from_sessions(
        self,
        sessions: list["Session"],
        max_items_per_session: int = 5,
    ) -> list[ReviewItem]:
        """Extract knowledge from multiple sessions.

        Args:
            sessions: List of sessions to process.
            max_items_per_session: Max knowledge items per session.

        Returns:
            Flat list of ReviewItems from all sessions.
        """
        all_items = []
        for session in sessions:
            items = self.extract_from_session(session)
            # Sort by confidence, take top N
            items.sort(key=lambda x: x.entry.confidence, reverse=True)
            all_items.extend(items[:max_items_per_session])
        return all_items

    def get_extraction_stats(self, items: list[ReviewItem]) -> dict:
        """Get statistics about extracted items."""
        total = len(items)
        approved = sum(1 for i in items if i.entry.review_status.value == "approved")
        pending = sum(1 for i in items if i.entry.review_status.value == "pending")

        type_counts = {}
        for item in items:
            t = item.entry.type.value
            type_counts[t] = type_counts.get(t, 0) + 1

        return {
            "total": total,
            "approved": approved,
            "pending": pending,
            "auto_approved": approved,
            "needs_review": pending,
            "type_distribution": type_counts,
            "avg_confidence": round(
                sum(i.entry.confidence for i in items) / total, 3
            ) if total else 0,
        }

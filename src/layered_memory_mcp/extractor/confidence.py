"""Confidence scoring for knowledge extractions.

Provides fine-grained confidence scoring beyond the base ConfidenceScorer.
"""

from __future__ import annotations

from ..models import ConfidenceScorer, KnowledgeEntry, ReviewStatus


def score_extraction(entry: KnowledgeEntry, threshold: float = 0.9) -> ReviewStatus:
    """Score and auto-review a knowledge extraction.

    This is a convenience wrapper around ConfidenceScorer.auto_review
    that can be extended with additional heuristics.
    """
    return ConfidenceScorer.auto_review(entry, threshold=threshold)

"""Hybrid ranking engine with RRF (Reciprocal Rank Fusion).

Inspired by Semble (https://github.com/MinishLab/semble) — a fast and accurate
code search library for agents. We adapt their RRF + multi-signal boosting
approach for markdown knowledge retrieval.

Design choices from Semble:
  - RRF for score normalization across different search modalities
  - Multi-chunk file boosting (files with multiple relevant sections rank higher)
  - Query-aware boosting (exact identifiers get extra weight)
  - Path-based reranking penalties
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .recall import RecallResult

# ---------------------------------------------------------------------------
# RRF constants
# ---------------------------------------------------------------------------

_RRF_K = 60  # RRF constant — higher = smoother rank decay

# ---------------------------------------------------------------------------
# RRF core
# ---------------------------------------------------------------------------

def rrf_fusion(
    scored_results: list[list[tuple[str, float]]],
    weights: list[float] | None = None,
) -> dict[str, float]:
    """Fuse multiple ranked lists into a single score map using RRF.

    Args:
        scored_results: List of ranked lists, each is [(id, raw_score), ...]
                        ordered by relevance (best first).
        weights: Optional weight for each list. If None, equal weights.

    Returns:
        Dict mapping item id to fused RRF score.
    """
    if not scored_results:
        return {}

    if weights is None:
        weights = [1.0] * len(scored_results)

    fused: dict[str, float] = defaultdict(float)

    for ranked_list, weight in zip(scored_results, weights):
        for rank, (item_id, _raw_score) in enumerate(ranked_list, start=1):
            fused[item_id] += weight * (1.0 / (_RRF_K + rank))

    return dict(fused)


# ---------------------------------------------------------------------------
# Query analysis
# ---------------------------------------------------------------------------

def _looks_like_identifier(query: str) -> bool:
    """Detect if query contains code-like identifiers.

    Patterns: camelCase, snake_case, dot.path, kebab-case.
    These benefit more from exact/BM25 matching than semantic search.
    """
    patterns = [
        r"\b[a-z]+_[a-z_]+\b",           # snake_case
        r"\b[a-z]+[A-Z][a-zA-Z]*\b",     # camelCase / PascalCase
        r"\b[a-z]+-[a-z-]+\b",           # kebab-case
        r"\b[a-zA-Z_]+\.[a-zA-Z_]+\b",   # dot.path.access
    ]
    return any(re.search(p, query) for p in patterns)


def _looks_like_exact_term(query: str) -> bool:
    """Detect exact technical terms (file paths, config keys, etc.)."""
    # Contains path separators, version numbers (x.y.z), or IP addresses
    return bool(re.search(r"[/\\]|\bv?\d+\.\d+(\.\d+)*\b|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", query))


def resolve_alpha(query: str, alpha: float | None = None) -> float:
    """Determine semantic vs keyword weight for hybrid search.

    Args:
        query: User search query.
        alpha: Optional override (0.0 = pure BM25, 1.0 = pure semantic).

    Returns:
        Alpha weight for semantic search. (1 - alpha) goes to BM25/keyword.
    """
    if alpha is not None:
        return max(0.0, min(1.0, alpha))

    # Auto-detect based on query characteristics
    if _looks_like_exact_term(query):
        return 0.2  # Heavy BM25 bias for exact terms
    if _looks_like_identifier(query):
        return 0.3  # Moderate BM25 bias for identifiers

    # Default: favor semantic for natural language queries
    return 0.7


# ---------------------------------------------------------------------------
# Boost signals
# ---------------------------------------------------------------------------

def boost_multi_section_files(
    scores: dict[str, float],
    section_counts: dict[str, int],
    boost_factor: float = 1.15,
) -> dict[str, float]:
    """Boost files that have multiple relevant sections.

    If a file matches in multiple independent sections, it's likely more
    relevant than a file with a single match. This signal is especially
    useful for long knowledge files with many headings.

    Args:
        scores: Fused RRF scores keyed by file path.
        section_counts: Number of matched sections per file.
        boost_factor: Multiplicative boost per extra section.

    Returns:
        Updated scores dict.
    """
    boosted = dict(scores)
    for file_path, count in section_counts.items():
        if count > 1 and file_path in boosted:
            # Logarithmic boost to avoid runaway scores
            multiplier = boost_factor ** (count - 1)
            boosted[file_path] *= min(multiplier, 2.0)  # Cap at 2x
    return boosted


def apply_query_boost(
    scores: dict[str, float],
    query: str,
    file_keywords: dict[str, list[str]],
    boost: float = 1.3,
) -> dict[str, float]:
    """Boost files whose keywords overlap with query terms.

    Args:
        scores: Current scores.
        query: Search query.
        file_keywords: Mapping of file_path -> list of extracted keywords.
        boost: Multiplicative boost when overlap found.

    Returns:
        Updated scores.
    """
    query_words = set(query.lower().split())
    boosted = dict(scores)

    for file_path, keywords in file_keywords.items():
        if file_path not in boosted:
            continue
        kw_set = set(k.lower() for k in keywords)
        overlap = query_words & kw_set
        if overlap:
            # Boost proportional to overlap ratio
            ratio = len(overlap) / max(len(query_words), 1)
            boosted[file_path] *= 1.0 + (boost - 1.0) * ratio

    return boosted


def penalize_deep_paths(
    scores: dict[str, float],
    penalty: float = 0.05,
) -> dict[str, float]:
    """Slightly penalize deeply nested file paths.

    In knowledge bases, top-level files (e.g., infra.md) are often more
    general/important than deeply nested ones (e.g., infra/proxy/wsl/v2.md).
    This is a mild tie-breaker.

    Args:
        scores: Current scores.
        penalty: Score reduction per path depth level.

    Returns:
        Updated scores.
    """
    penalized = dict(scores)
    for file_path in penalized:
        depth = file_path.count("/") + file_path.count("\\")
        if depth > 2:
            penalized[file_path] *= max(0.8, 1.0 - penalty * (depth - 2))
    return penalized


# ---------------------------------------------------------------------------
# Top-k selection
# ---------------------------------------------------------------------------

def rerank_topk(
    scores: dict[str, float],
    top_n: int,
    penalise_paths: bool = True,
) -> list[tuple[str, float]]:
    """Sort scores descending and return top N with optional path penalty.

    Args:
        scores: Fused and boosted scores.
        top_n: Number of results to return.
        penalise_paths: Whether to apply depth penalty.

    Returns:
        List of (file_path, score) sorted by score desc.
    """
    final_scores = dict(scores)
    if penalise_paths:
        final_scores = penalize_deep_paths(final_scores)

    ranked = sorted(final_scores.items(), key=lambda x: (-x[1], x[0]))
    return ranked[:top_n]


# ---------------------------------------------------------------------------
# High-level hybrid search API
# ---------------------------------------------------------------------------

def hybrid_search(
    query: str,
    keyword_results: list[tuple[str, float]],
    fuzzy_results: list[tuple[str, float]],
    bm25_results: list[tuple[str, float]],
    semantic_results: list[tuple[str, float]] | None = None,
    section_counts: dict[str, int] | None = None,
    file_keywords: dict[str, list[str]] | None = None,
    top_n: int = 5,
    alpha: float | None = None,
) -> list[tuple[str, float]]:
    """Run hybrid search with RRF fusion and multi-signal boosting.

    This is the main entry point for hybrid retrieval. It combines multiple
    search modalities using RRF, then applies boost signals and reranking.

    Args:
        query: Original search query.
        keyword_results: [(file_path, score), ...] from exact keyword search.
        fuzzy_results: [(file_path, score), ...] from fuzzy/difflib search.
        bm25_results: [(file_path, score), ...] from BM25 search.
        semantic_results: Optional [(file_path, score), ...] from vector search.
        section_counts: Optional {file_path: matched_section_count}.
        file_keywords: Optional {file_path: [keywords]} for query boost.
        top_n: Number of results to return.
        alpha: Semantic weight override (None = auto-detect).

    Returns:
        Ranked list of (file_path, fused_score).
    """
    alpha_weight = resolve_alpha(query, alpha)

    # Build ranked lists for RRF
    ranked_lists: list[list[tuple[str, float]]] = []
    weights: list[float] = []

    # Keyword (exact match) — always included
    if keyword_results:
        ranked_lists.append(keyword_results)
        weights.append(1.0)

    # Fuzzy — always included
    if fuzzy_results:
        ranked_lists.append(fuzzy_results)
        weights.append(0.8)

    # BM25 — always included
    if bm25_results:
        ranked_lists.append(bm25_results)
        weights.append(1.0)

    # Semantic — optional, weighted by alpha
    if semantic_results and alpha_weight > 0.1:
        ranked_lists.append(semantic_results)
        weights.append(alpha_weight)

    # If BM25 exists, downweight it when alpha is high (semantic preferred)
    if bm25_results and alpha_weight > 0.5:
        # Find BM25 index
        for i, lst in enumerate(ranked_lists):
            if lst is bm25_results:
                weights[i] = 1.0 - alpha_weight + 0.3
                break

    # RRF fusion
    fused = rrf_fusion(ranked_lists, weights)

    if not fused:
        return []

    # Apply boost signals
    if section_counts:
        fused = boost_multi_section_files(fused, section_counts)

    if file_keywords:
        fused = apply_query_boost(fused, query, file_keywords)

    # Final reranking
    return rerank_topk(fused, top_n, penalise_paths=alpha_weight < 0.6)

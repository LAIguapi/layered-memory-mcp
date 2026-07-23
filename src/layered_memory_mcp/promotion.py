"""
Promotion Detector — same-topic clustering / "this file should be split".

When an agent keeps appending same-topic sections into a catch-all domain
(default ``misc``), the existing dedup layers only judge whether a *single*
piece of content is a duplicate — they never notice that a whole topic has
quietly accumulated into a cluster that deserves its own L1 file. The classic
failure: config notes piled up as 12 misc sections until they were split into
a dedicated ``database.md`` file.

This module fills that gap. After a write into a watched domain, it:

  1. parses the file's ``##`` sections (reusing injector's ``_H2_RE``),
  2. embeds each section body with the in-repo bge-small-zh model
     (reusing ``storage.vector_store._embed_texts`` — no new pipeline),
  3. single-link clusters sections by cosine similarity, and
  4. suggests extracting any cluster of ``promotion_min_cluster_size`` or more
     into its own domain.

Design philosophy (the whole point): the framework only computes the objective
fact ("these N sections are semantically one topic") and emits a *suggestion*.
It NEVER moves content. The agent reads the suggestion and decides — exactly
like dedup's ``suggestion`` field. All detection is wrapped in try/except; any
failure logs a warning and returns None, so it can never break the primary
write.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger("layered_memory_mcp.promotion")

# Reuse the injector's H2 section pattern verbatim (DRY — do not re-invent
# section parsing). Compiled here to avoid an import cycle at module load.
_H2_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# Stop-words stripped from section headings when deriving a suggested domain.
# Kept tiny and generic — the framework only offers a *rough* hint; the agent
# picks the real name.
_TITLE_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with",
    "配置", "说明", "笔记", "记录", "数据", "方案", "问题", "总结",
}


def detect_promotion_candidate(
    config: "MemoryConfig",
    domain: str,
    filepath: Path,
) -> dict | None:
    """Detect whether a watched file has a same-topic cluster worth promoting.

    Args:
        config: MemoryConfig instance.
        domain: The domain just written to (e.g. "misc").
        filepath: Path to that domain's L1 file.

    Returns:
        None when there's no candidate (or detection is disabled / out of
        scope / errored). Otherwise a dict::

            {
              "watch_domain": "misc",
              "cluster_sections": ["database 连接池...", "database 迁移...", ...],
              "cluster_size": 3,
              "suggested_domain": "database",
              "file_section_count": 8,
              "hint": "misc 已聚集 3 条语义相近 section ...",
            }

    Never raises — any failure is logged and swallowed (returns None).
    """
    try:
        # --- Gate 0: master switch ---
        if not getattr(config, "promotion_enabled", True):
            return None

        # --- Gate 1: only scan watched catch-all domains (zero-cost skip) ---
        watch = getattr(config, "promotion_watch_domains", ["misc"]) or []
        domain_clean = domain.removesuffix(".md") if domain else domain
        if domain_clean not in watch:
            return None

        path = Path(filepath)
        if not path.exists():
            return None

        raw = path.read_text(encoding="utf-8")

        # --- Gate 2: parse sections; skip if too few ---
        sections = _parse_sections(raw)
        min_sections = getattr(config, "promotion_min_sections", 4)
        if len(sections) < min_sections:
            return None

        # --- Embed each section body (reuse the in-repo bge pipeline) ---
        # Embed heading + body so a terse body still carries topical signal.
        texts = [f"{h}\n{b}".strip() for h, b in sections]
        matrix = _embed_sections(texts)
        if matrix is None or matrix.shape[0] != len(sections):
            return None

        # --- Single-link cluster by cosine similarity ---
        threshold = getattr(config, "promotion_cluster_threshold", 0.60)
        clusters = _single_link_cluster(matrix, threshold)

        # --- Largest cluster meeting the size gate wins ---
        min_size = getattr(config, "promotion_min_cluster_size", 3)
        clusters.sort(key=len, reverse=True)
        for idx_group in clusters:
            if len(idx_group) < min_size:
                continue

            cluster_headings = [sections[i][0] for i in sorted(idx_group)]
            suggested = _suggest_domain_name(cluster_headings)
            hint = (
                f"{domain_clean} 已聚集 {len(idx_group)} 条语义相近 section"
                f"（疑似同主题），建议用 create_knowledge_file 提取为独立类目 "
                f"{suggested}.md，而非继续堆 {domain_clean}。"
            )
            return {
                "watch_domain": domain_clean,
                "cluster_sections": cluster_headings,
                "cluster_size": len(idx_group),
                "suggested_domain": suggested,
                "file_section_count": len(sections),
                "hint": hint,
            }

        return None
    except Exception as e:  # noqa: BLE001 — detection must never break writes
        logger.warning("Promotion detection failed (non-critical): %s", e)
        return None


# ---------------------------------------------------------------------------
# Internal helpers — pure computation, no side effects
# ---------------------------------------------------------------------------

def _parse_sections(raw: str) -> list[tuple[str, str]]:
    """Split markdown into ``## heading`` → body pairs.

    Reuses the injector H2 heading shape. Returns a list of
    ``(heading_text, body_text)`` in document order. Sections with an empty
    body are kept (heading alone still carries topical signal).
    """
    matches = list(_H2_RE.finditer(raw))
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[body_start:body_end].strip()
        sections.append((heading, body))
    return sections


def _embed_sections(texts: list[str]):
    """Embed section texts into an (N, dim) matrix, reusing the vector store.

    Returns an np.ndarray, or None if embedding is unavailable (e.g. the model
    can't be loaded). Never raises — a None return degrades to "no candidate".
    """
    try:
        from .storage.vector_store import _embed_texts

        return _embed_texts(texts)
    except Exception as e:  # noqa: BLE001 — model load / embed may fail offline
        logger.warning("Section embedding failed (non-critical): %s", e)
        return None


def _single_link_cluster(matrix, threshold: float) -> list[list[int]]:
    """Single-link cluster row indices of ``matrix`` by cosine similarity.

    bge vectors are L2-normalized, so cosine == dot product. Two sections whose
    similarity is >= ``threshold`` are unioned into the same cluster. Returns a
    list of clusters, each a list of row indices. Pure computation.
    """
    import numpy as np

    n = matrix.shape[0]
    if n == 0:
        return []

    # Union-Find (disjoint set) over section indices.
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Cosine similarity matrix (normalized vectors → dot product).
    sims = matrix @ matrix.T
    for i in range(n):
        for j in range(i + 1, n):
            if float(sims[i, j]) >= threshold:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)
    return list(groups.values())


def _suggest_domain_name(headings: list[str]) -> str:
    """Derive a rough suggested domain name from clustered section headings.

    Deliberately simple (design decision): the framework offers only a coarse
    hint — the agent makes the final naming call. Strategy:
      1. try the longest common alphanumeric token prefix across headings,
      2. else the single most frequent non-stopword token,
      3. else fall back to "topic".
    """
    tokenized = [_tokenize_heading(h) for h in headings]
    tokenized = [t for t in tokenized if t]
    if not tokenized:
        return "topic"

    # Strategy 1: common leading token shared by ALL headings.
    first_tokens = {toks[0] for toks in tokenized}
    if len(first_tokens) == 1:
        candidate = next(iter(first_tokens))
        if candidate and candidate not in _TITLE_STOPWORDS:
            return candidate

    # Strategy 2: most frequent non-stopword token across all headings.
    freq: dict[str, int] = {}
    for toks in tokenized:
        for tok in toks:
            if tok in _TITLE_STOPWORDS:
                continue
            freq[tok] = freq.get(tok, 0) + 1
    if freq:
        # Highest count; tie-break by longer token, then alphabetical for
        # determinism.
        best = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
        top_tok, top_count = best[0]
        if top_count >= 2:
            return top_tok
        # No token repeats — nothing is common enough to name confidently.

    return "topic"


def _tokenize_heading(heading: str) -> list[str]:
    """Break a heading into lowercase alphanumeric / CJK-run tokens.

    Splits on whitespace and punctuation. ASCII words are lowercased; CJK
    characters are grouped into contiguous runs. Pure, deterministic.
    """
    # Grab ASCII word tokens and CJK runs separately, preserving nothing else.
    ascii_tokens = re.findall(r"[A-Za-z0-9]+", heading)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", heading)
    tokens = [t.lower() for t in ascii_tokens] + cjk_runs
    return [t for t in tokens if t]

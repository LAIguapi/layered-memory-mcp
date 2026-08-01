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


# ---------------------------------------------------------------------------
# v3.1.1: Active split inspection — a PULL tool (user asks "what should split?")
# rather than the PUSH hint that rode along on every write.
# ---------------------------------------------------------------------------

def scan_split_candidates(
    config: "MemoryConfig",
    domains: list[str] | None = None,
    ignore_watch_gate: bool = True,
    min_sections: int | None = None,
    min_cluster_size: int | None = None,
) -> dict:
    """Scan L1 files for same-topic clusters that deserve their own file.

    This is the ACTIVE counterpart to ``detect_promotion_candidate`` (which only
    fired passively after a write into a *watched* catch-all domain). Here the
    user/agent can proactively ask "which files should be split?" across ANY
    domain, not just ``misc``.

    Args:
        config: MemoryConfig.
        domains: optional explicit list of domains to scan (filenames without
            .md). None → scan every .md across all knowledge dirs.
        ignore_watch_gate: when True (default) do NOT restrict to
            ``promotion_watch_domains`` — the whole point of the active tool is
            to look everywhere. When False, honor the watch list (parity with
            the passive detector).
        min_sections: override ``promotion_min_sections`` for this scan.
        min_cluster_size: override ``promotion_min_cluster_size`` for this scan.

    Returns:
        ``{"success": True, "candidates": [ {domain, file, dir, cluster_size,
        cluster_sections, suggested_domain, file_section_count}, ... ],
        "scanned": N, "note": ...}``. Read-only — NEVER moves content. Any
        per-file failure is skipped, not fatal.
    """
    result: dict = {"success": True, "candidates": [], "scanned": 0}
    try:
        watch = getattr(config, "promotion_watch_domains", ["misc"]) or []
        want_min_sections = (
            min_sections if min_sections is not None
            else getattr(config, "promotion_min_sections", 4)
        )
        want_min_cluster = (
            min_cluster_size if min_cluster_size is not None
            else getattr(config, "promotion_min_cluster_size", 3)
        )
        threshold = getattr(config, "promotion_cluster_threshold", 0.60)

        # Enumerate (domain, filepath, dir_label) across all knowledge dirs.
        targets: list[tuple[str, Path, str]] = []
        seen: set[str] = set()
        for kdir in config.knowledge_dirs:
            if not kdir.exists():
                continue
            dir_label = "shared" if kdir.name == "shared" else "namespace"
            for fp in sorted(kdir.glob("*.md")):
                dom = fp.name.removesuffix(".md")
                if domains is not None and dom not in domains:
                    continue
                if not ignore_watch_gate and dom not in watch:
                    continue
                if dom in seen:
                    continue
                seen.add(dom)
                targets.append((dom, fp, dir_label))

        for dom, fp, dir_label in targets:
            result["scanned"] += 1
            try:
                raw = fp.read_text(encoding="utf-8")
            except OSError:
                continue
            sections = _parse_sections(raw)
            if len(sections) < want_min_sections:
                continue
            texts = [f"{h}\n{b}".strip() for h, b in sections]
            matrix = _embed_sections(texts)
            if matrix is None or matrix.shape[0] != len(sections):
                continue
            clusters = _single_link_cluster(matrix, threshold)
            clusters.sort(key=len, reverse=True)
            for idx_group in clusters:
                if len(idx_group) < want_min_cluster:
                    continue
                cluster_headings = [sections[i][0] for i in sorted(idx_group)]
                suggested = _suggest_domain_name(cluster_headings)
                result["candidates"].append({
                    "domain": dom,
                    "file": fp.name,
                    "dir": dir_label,
                    "cluster_size": len(idx_group),
                    "cluster_sections": cluster_headings,
                    "suggested_domain": suggested,
                    "file_section_count": len(sections),
                    "hint": (
                        f"{dom} 有 {len(idx_group)} 条语义相近 section（疑似同主题），"
                        f"可用 create_knowledge_file 提取为独立类目 {suggested}.md。"
                        "仅为建议；框架不会自动搬移内容。"
                    ),
                })
                # Report only the largest qualifying cluster per file to avoid
                # noise; a second pass after the user acts will surface the next.
                break

        result["note"] = (
            "Suggestions only — the framework never moves content. Confirm, then "
            "use create_knowledge_file to extract, and the source sections will "
            "dedup on the next semantic write."
        )
        return result
    except Exception as e:  # noqa: BLE001 — inspection must never raise
        logger.warning("scan_split_candidates failed: %s", e)
        return {"success": False, "error": str(e), "candidates": []}


def scan_cross_domain_duplicates(
    config: "MemoryConfig",
    threshold: float | None = None,
    top_pairs: int = 50,
) -> dict:
    """Scan ALL section vectors for cross-FILE near-duplicate section pairs.

    The active, pull-based counterpart to the passive ``cross_domain_hints`` that
    flickered by on individual writes. Lets the user ask "which knowledge is
    duplicated across files?" and get a ranked, structured clean-up worklist.

    Method: read every section vector from the store, compute pairwise cosine,
    and report pairs from DIFFERENT files whose similarity ≥ threshold, each
    tagged with the scope of the second file (namespace / shared / other_ns) so
    the user knows whether a consolidation would cross the shared/namespace red
    line.

    Args:
        config: MemoryConfig.
        threshold: cosine cutoff; defaults to the semantic fuse band.
        top_pairs: cap on returned pairs (highest similarity first).

    Returns:
        ``{"success": True, "duplicates": [ {domain_a, section_a, domain_b,
        section_b, cosine, scope_b}, ... ], "total": N }``. Read-only.
    """
    try:
        import numpy as np
        from .storage.vector_store import VectorStore, EMBED_DIM
        import json as _json
        import sqlite3 as _sqlite3

        # Resolve the fuse threshold lazily (avoid an injector import cycle at
        # module load; injector imports promotion only inside functions).
        if threshold is None:
            try:
                from .injector import _semantic_thresholds
                _, threshold, _ = _semantic_thresholds(config)
            except Exception:
                threshold = 0.72

        db_path = config.home / "data" / "vectors.db"
        if not db_path.exists():
            return {"success": True, "duplicates": [], "total": 0,
                    "note": "No vector store yet — run reindex_vector_store first."}

        with _sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT domain, vector, metadata FROM vectors"
            ).fetchall()

        domains: list[str] = []
        sections: list[str] = []
        vectors: list = []
        for dom, vec_json, meta_json in rows:
            try:
                vec = np.array(_json.loads(vec_json), dtype=np.float32)
            except Exception:
                continue
            if vec.shape[0] != EMBED_DIM:
                continue
            meta = _json.loads(meta_json) if meta_json else {}
            domains.append(dom)
            sections.append(meta.get("section") or "")
            vectors.append(vec)

        n = len(vectors)
        if n < 2:
            return {"success": True, "duplicates": [], "total": 0}

        matrix = np.vstack(vectors)
        sims = matrix @ matrix.T

        pairs: list[dict] = []
        for i in range(n):
            for j in range(i + 1, n):
                if domains[i] == domains[j]:
                    continue  # same file → not a cross-file duplicate
                cos = float(sims[i, j])
                if cos < threshold:
                    continue
                scope_b = _classify_scope(config, domains[j])
                pairs.append({
                    "domain_a": domains[i],
                    "section_a": sections[i],
                    "domain_b": domains[j],
                    "section_b": sections[j],
                    "cosine": round(cos, 4),
                    "scope_b": scope_b,
                })

        pairs.sort(key=lambda p: p["cosine"], reverse=True)
        capped = pairs[:top_pairs]
        return {
            "success": True,
            "duplicates": capped,
            "total": len(pairs),
            "threshold": round(float(threshold), 4),
            "note": (
                "Read-only worklist. Consolidation is manual; note scope_b — "
                "merging into shared/ or across a namespace crosses the "
                "framework's no-auto-cross-library red line and is your call."
            ),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("scan_cross_domain_duplicates failed: %s", e)
        return {"success": False, "error": str(e), "duplicates": []}


def _classify_scope(config, hit_domain: str) -> str:
    """Filesystem-scope of a domain (namespace / shared / other_namespace).

    Local copy of the injector's classifier to avoid an import cycle
    (injector imports promotion lazily; keep this direction clean).
    """
    fname = f"{hit_domain}.md"
    try:
        if (config.knowledge_dir / fname).exists():
            return "namespace"
    except Exception:
        pass
    try:
        shared = getattr(config, "_shared_knowledge_dir", None)
        if shared and (shared / fname).exists():
            return "shared"
    except Exception:
        pass
    try:
        root = getattr(config, "_knowledge_root", None)
        if root and root.exists():
            for sub in root.iterdir():
                if sub.is_dir() and sub != config.knowledge_dir and sub.name != "shared":
                    if (sub / fname).exists():
                        return "other_namespace"
    except Exception:
        pass
    return "unknown"

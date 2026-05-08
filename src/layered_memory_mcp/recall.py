"""
L1 Knowledge Retrieval Engine.

Searches knowledge files by keyword with relevance scoring.
Supports subdirectory scanning, fuzzy search, wiki-links, and L0 index generation.
Core functions are pure stdlib; embedding search is optional.
"""

import difflib
import hashlib
import logging
import re
import time
from collections import Counter
from pathlib import Path

logger = logging.getLogger("layered_memory_mcp.recall")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_KNOWLEDGE_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

# File listing cache TTL (seconds) — reduces disk scans on frequent recalls
_SCAN_CACHE_TTL = 5.0
_scan_cache: dict[str, tuple[float, dict[str, str]]] = {}

def invalidate_scan_cache(knowledge_dir: str | None = None) -> None:
    """Invalidate file listing cache (called after writes to ensure freshness).

    If knowledge_dir is None, clears the entire cache.
    Otherwise clears only entries for the given directory.
    """
    if knowledge_dir is None:
        _scan_cache.clear()
    else:
        _scan_cache.pop(knowledge_dir, None)

# Regex to match any markdown heading level (# through ######)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# Wiki-link syntax: [[file-name]] or [[subdir/file-name]]
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")

# ---------------------------------------------------------------------------
# Subdirectory-aware scanning  (Feature 4)
# ---------------------------------------------------------------------------

def scan_knowledge_files(knowledge_dir: str) -> dict[str, str]:
    """Scan knowledge directory recursively, return {relative_path: full_path}.

    Supports subdirectories: knowledge/infra/proxy.md -> key is "infra/proxy.md".
    Results are cached with a {_SCAN_CACHE_TTL}s TTL to avoid repeated disk scans.
    """
    # Check cache
    if knowledge_dir in _scan_cache:
        ts, cached = _scan_cache[knowledge_dir]
        if time.time() - ts < _SCAN_CACHE_TTL:
            return cached
    files: dict[str, str] = {}
    kdir = Path(knowledge_dir)
    if not kdir.exists():
        return files
    for f in sorted(kdir.rglob("*.md")):
        rel = f.relative_to(kdir)
        files[str(rel)] = str(f)
    _scan_cache[knowledge_dir] = (time.time(), files)
    return files


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

def score_relevance(keyword: str, content: str, filename: str) -> float:
    """Calculate relevance score between keyword and file content."""
    score = 0.0
    kw_lower = keyword.lower()

    # Exact filename match
    if kw_lower in filename.lower():
        score += 10.0

    # Content frequency (capped)
    count = content.lower().count(kw_lower)
    score += min(count * 0.5, 5.0)

    # Heading match bonus (any level: # through ######)
    for match in _HEADING_RE.finditer(content):
        if kw_lower in match.group(2).lower():
            score += 3.0

    return score


def fuzzy_score(keyword: str, content: str, filename: str) -> float:
    """Fuzzy similarity score using difflib.SequenceMatcher.

    Catches partial matches and reorderings that keyword search misses.
    """
    kw_lower = keyword.lower()
    score = 0.0

    # Cap content size to avoid O(n²) on large files
    _FUZZY_MAX_CHARS = 50 * 1024  # 50KB
    content_limited = content[:_FUZZY_MAX_CHARS]

    # Filename fuzzy match
    score += difflib.SequenceMatcher(None, kw_lower, filename.lower()).ratio() * 6.0

    # Heading fuzzy match — check each heading against keyword (limit to avoid O(n²))
    _MAX_HEADINGS = 20
    heading_matches = list(_HEADING_RE.finditer(content_limited))[:_MAX_HEADINGS]
    for match in heading_matches:
        heading_lower = match.group(2).lower()
        ratio = difflib.SequenceMatcher(None, kw_lower, heading_lower).ratio()
        score += ratio * 4.0

    # Content word overlap — split into words, compute Jaccard-like overlap
    kw_words = set(kw_lower.split())
    content_words = set(content_limited.lower().split())
    if kw_words and content_words:
        overlap = len(kw_words & content_words) / len(kw_words)
        score += overlap * 5.0

    return score




# ---------------------------------------------------------------------------
# BM25 / TF-IDF scoring (v0.6.0: better search quality)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Tokenize text for BM25 scoring. Handles CJK + English."""
    tokens = []
    for token in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", text.lower()):
        if token.lower() not in _STOPWORDS and len(token) >= 2:
            tokens.append(token)
    return tokens


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    doc_count: int,
    avg_dl: float,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """Compute BM25 score for a single document.

    Simplified BM25 without IDF (we score within a single query context,
    so IDF is constant across docs — relative ordering is preserved).
    Uses raw term frequency saturation instead.
    """
    dl = len(doc_tokens)
    if dl == 0:
        return 0.0

    score = 0.0
    doc_freq = Counter(doc_tokens)
    for qt in query_tokens:
        tf = doc_freq.get(qt, 0)
        if tf == 0:
            continue
        # BM25 TF component: (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * dl / max(avg_dl, 1))
        score += numerator / denominator

    return score


def bm25_relevance(keyword: str, content: str, filename: str, avg_dl: float = 100.0, doc_count: int = 1) -> float:
    """BM25-based relevance scoring. Better than raw frequency for longer documents.

    Args:
        keyword: Search query.
        content: File content.
        filename: File name/relative path.
        avg_dl: Average document length across the corpus.
        doc_count: Total number of documents in the corpus.

    Returns:
        BM25 score (unnormalized, comparable within a single query).
    """
    query_tokens = _tokenize(keyword)
    doc_tokens = _tokenize(content)
    fn_tokens = _tokenize(filename)

    score = 0.0

    # BM25 on content
    score += _bm25_score(query_tokens, doc_tokens, doc_count, avg_dl) * 2.0

    # Filename exact match bonus (still the strongest signal)
    kw_lower = keyword.lower()
    if kw_lower in filename.lower():
        score += 10.0

    # Heading match bonus (limit to avoid O(n²) on heading-heavy files)
    _MAX_BM25_HEADINGS = 20
    heading_matches = list(_HEADING_RE.finditer(content))[:_MAX_BM25_HEADINGS]
    for match in heading_matches:
        heading_lower = match.group(2).lower()
        # Exact keyword in heading
        if kw_lower in heading_lower:
            score += 3.0
        # BM25 on heading tokens
        heading_tokens = _tokenize(heading_lower)
        if query_tokens and heading_tokens:
            score += _bm25_score(query_tokens, heading_tokens, doc_count, 20.0)

    return score

def _extract_heading_title(line: str) -> str:
    """Extract title text from a markdown heading line (# through ######)."""
    m = _HEADING_RE.match(line)
    # Always called after _HEADING_RE.match(line) succeeds, so m is never None
    return m.group(2).strip()


def extract_relevant_sections(keyword: str, content: str, max_sections: int = 3) -> list[dict]:
    """Extract markdown sections (any heading level) containing the keyword."""
    sections: list[dict] = []
    current_title = None
    current_lines: list[str] = []
    current_matched = False

    def flush():
        if current_matched and current_lines:
            sections.append({
                "title": current_title or "Untitled",
                "content": "\n".join(current_lines).strip(),
            })

    for line in content.split("\n"):
        if line.startswith("#") and _HEADING_RE.match(line):
            flush()
            current_title = _extract_heading_title(line)
            current_lines = [line]
            current_matched = False
        else:
            if current_title is None and line.strip() and not line.startswith("#"):
                current_title = "Overview"
            current_lines.append(line)

        if keyword.lower() in line.lower():
            current_matched = True

    flush()
    return sections[:max_sections]


# ---------------------------------------------------------------------------
# Multi-directory support (v0.6.0: namespace + shared)
# ---------------------------------------------------------------------------

def scan_knowledge_dirs(knowledge_dirs: list[str]) -> dict[str, str]:
    """Scan multiple knowledge directories, merging results.
    
    Earlier dirs (namespace) have priority over later dirs (shared).
    Returns {relative_path: full_path}.
    """
    merged: dict[str, str] = {}
    for kdir in knowledge_dirs:
        for rel, full in scan_knowledge_files(kdir).items():
            if rel not in merged:  # namespace takes priority
                merged[rel] = full
    return merged


# ---------------------------------------------------------------------------
# Main recall with search modes  (Feature 2: semantic search)
# ---------------------------------------------------------------------------

def recall(
    keyword: str,
    knowledge_dir: str | list[str],
    top_n: int = 5,
    search_mode: str = "keyword",
) -> dict:
    """Main retrieval function.

    Args:
        keyword: Search query.
        knowledge_dir: Path to L1 knowledge directory, or list of paths
                      (for namespace + shared merge).
        top_n: Max results to return.
        search_mode: "keyword" (exact), "fuzzy" (difflib), "bm25" (TF-IDF-like),
                     or "hybrid" (keyword + fuzzy combined).

    Returns:
        Dict with success, keyword, results.
    """
    if isinstance(knowledge_dir, list):
        files = scan_knowledge_dirs(knowledge_dir)
    else:
        files = scan_knowledge_files(knowledge_dir)

    if not files:
        return {
            "success": False,
            "error": f"No knowledge files found in {knowledge_dir}",
            "keyword": keyword,
        }

    # Single pass: read all files, compute BM25 stats if needed
    avg_dl = 100.0  # safe default
    file_contents: dict[str, tuple[str, int]] = {}  # {rel_path: (content, word_count)}
    
    for rel_path, filepath in files.items():
        try:
            path = Path(filepath)
            file_size = path.stat().st_size
            if file_size > MAX_KNOWLEDGE_FILE_SIZE:
                logger.warning("Skipping oversized file %s (%d bytes)", rel_path, file_size)
                continue
            content = path.read_text(encoding="utf-8")
            wc = len(content.split())
            file_contents[rel_path] = (content, wc)
        except Exception as e:
            logger.debug("Cannot read knowledge file %s: %s", rel_path, e)
            continue

    # Compute avg_dl from in-memory contents if BM25 mode
    if search_mode == "bm25" and file_contents:
        total_wc = sum(wc for _, wc in file_contents.values())
        avg_dl = total_wc / len(file_contents) if file_contents else 100.0

    results: list[dict] = []
    for rel_path, (content, _wc) in file_contents.items():
        if search_mode == "keyword":
            score = score_relevance(keyword, content, rel_path)
        elif search_mode == "fuzzy":
            score = fuzzy_score(keyword, content, rel_path)
        elif search_mode == "bm25":
            score = bm25_relevance(keyword, content, rel_path, avg_dl=avg_dl, doc_count=len(file_contents))
        elif search_mode == "hybrid":
            score = score_relevance(keyword, content, rel_path) + fuzzy_score(keyword, content, rel_path) * 0.5
        else:
            score = score_relevance(keyword, content, rel_path)

        if score > 0:
            sections = extract_relevant_sections(keyword, content)
            wiki_links = extract_wiki_links(content)
            results.append({
                "file": rel_path,
                "score": round(score, 2),
                "matched_sections": len(sections),
                "sections": sections,
                "wiki_links": wiki_links[:5] if wiki_links else [],
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[:top_n]

    return {
        "success": True,
        "keyword": keyword,
        "search_mode": search_mode,
        "total_files": len(file_contents),
        "matched_files": len(results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Feature 1: L0 Index Auto-Generation
# ---------------------------------------------------------------------------

def generate_l0_index(knowledge_dir: str | list[str]) -> str:
    """Generate a compact L0 index from all L1 knowledge files.

    Format per line:
        [file.md] Title → kw1, kw2, kw3

    This index is designed to be tiny (< 2KB) and injected into every turn.
    
    Args:
        knowledge_dir: Single path or list of paths (namespace + shared).
    """
    if isinstance(knowledge_dir, list):
        files = scan_knowledge_dirs(knowledge_dir)
    else:
        files = scan_knowledge_files(knowledge_dir)
    if not files:
        return ""

    lines: list[str] = []
    for rel_path, filepath in sorted(files.items()):
        try:
            path = Path(filepath)
            # Skip oversized files to prevent OOM
            if path.stat().st_size > MAX_KNOWLEDGE_FILE_SIZE:
                logger.warning("Skipping oversized file in L0 generation: %s", rel_path)
                continue
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue

        # Extract first heading as title
        title = ""
        for m in _HEADING_RE.finditer(content):
            title = m.group(2).strip()
            break
        if not title:
            # Use first non-empty line as fallback
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    title = stripped[:60]
                    break

        # Extract top keywords (word frequency, skip stopwords)
        words = _extract_top_keywords(content, max_keywords=5)

        entry = f"[{rel_path}] {title}"
        if words:
            entry += f" → {', '.join(words)}"
        lines.append(entry)

    return "\n".join(lines)


# Simple stopwords for keyword extraction
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
    "neither", "each", "every", "all", "any", "few", "more", "most", "other",
    "some", "such", "no", "only", "own", "same", "than", "too", "very",
    "just", "because", "if", "when", "where", "how", "what", "which", "who",
    "this", "that", "these", "those", "i", "me", "my", "we", "our", "you",
    "your", "he", "him", "his", "she", "her", "it", "its", "they", "them",
    "their", "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
    "会", "着", "没有", "看", "好", "自己",
})


def _extract_top_keywords(content: str, max_keywords: int = 5) -> list[str]:
    """Extract top keywords from content by word frequency."""
    # Split into words: keep CJK chars individually, latin words as-is
    words: list[str] = []
    for token in re.findall(r"[a-zA-Z\u4e00-\u9fff][a-zA-Z0-9\u4e00-\u9fff_-]*", content):
        if len(token) >= 2 and token.lower() not in _STOPWORDS:
            words.append(token.lower())

    counter = Counter(words)
    return [w for w, _ in counter.most_common(max_keywords)]


# ---------------------------------------------------------------------------
# Feature 3: Knowledge Dedup
# ---------------------------------------------------------------------------

def find_similar_knowledge(
    content: str,
    knowledge_dir: str | list[str],
    threshold: float = 0.6,
) -> list[dict]:
    """Find existing knowledge files similar to the given content.

    Uses difflib.SequenceMatcher for similarity detection.

    Args:
        content: New content to compare against.
        knowledge_dir: Path to L1 knowledge directory, or list of paths.
        threshold: Minimum similarity ratio (0.0-1.0) to report.

    Returns:
        List of {file, similarity, suggestion} sorted by similarity desc.
    """
    if isinstance(knowledge_dir, list):
        files = scan_knowledge_dirs(knowledge_dir)
    else:
        files = scan_knowledge_files(knowledge_dir)
    if not files:
        return []

    matches: list[dict] = []
    content_lower = content.lower()
    # Truncate comparison content to avoid O(n²) on large inputs
    _COMPARE_MAX_CHARS = 100 * 1024  # 100KB
    truncated = len(content) > _COMPARE_MAX_CHARS
    compare_content = content_lower[:_COMPARE_MAX_CHARS]
    compare_len = len(compare_content)

    for rel_path, filepath in files.items():
        try:
            existing = Path(filepath).read_text(encoding="utf-8")
            # Truncate existing content to match threshold
            existing_lower = existing.lower()[:_COMPARE_MAX_CHARS]
        except Exception:
            continue

        # Quick pre-filter: if length ratio exceeds 20:1, skip (too different)
        existing_len = len(existing_lower)
        if compare_len > 0 and existing_len > 0:
            length_ratio = max(compare_len, existing_len) / max(min(compare_len, existing_len), 1)
            if length_ratio > 15:
                continue

        ratio = difflib.SequenceMatcher(None, compare_content, existing_lower).ratio()
        if ratio >= threshold:
            if ratio >= 0.9:
                suggestion = "replace"
            elif ratio >= 0.7:
                suggestion = "merge"
            else:
                suggestion = "append"
            matches.append({
                "file": rel_path,
                "similarity": round(ratio, 3),
                "suggestion": suggestion,
            })

    matches.sort(key=lambda x: x["similarity"], reverse=True)
    if truncated and matches:
        return [{**m, "truncated": True} for m in matches]
    return matches


# ---------------------------------------------------------------------------
# Feature 5: Knowledge Health
# ---------------------------------------------------------------------------

def knowledge_health(knowledge_dir: str | list[str]) -> dict:
    """Analyze health of the knowledge base.

    Returns per-file and overall health metrics.
    
    Args:
        knowledge_dir: Single path or list of paths (namespace + shared).
    """
    if isinstance(knowledge_dir, list):
        files = scan_knowledge_dirs(knowledge_dir)
    else:
        files = scan_knowledge_files(knowledge_dir)
    if not files:
        return {
            "total_files": 0,
            "overall_score": 0,
            "files": [],
            "issues": ["Knowledge base is empty"],
        }

    now = time.time()
    file_reports: list[dict] = []
    issues: list[str] = []

    for rel_path, filepath in sorted(files.items()):
        path = Path(filepath)
        try:
            stat = path.stat()
        except OSError:
            continue

        size = stat.st_size
        age_days = (now - stat.st_mtime) / 86400

        # Very large file — skip deep content analysis to avoid OOM
        if size > MAX_KNOWLEDGE_FILE_SIZE:
            file_reports.append({
                "file": rel_path,
                "health": "poor",
                "score": 0,
                "size_bytes": size,
                "word_count": 0,
                "headings": 0,
                "wiki_links": 0,
                "age_days": round(age_days, 1),
                "warning": f"Exceeds max file size ({size} > {MAX_KNOWLEDGE_FILE_SIZE} bytes)",
            })
            issues.append(f"{rel_path}: exceeds max file size ({size} bytes) — skipped deep analysis")
            continue

        # Read content for deeper analysis
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            file_reports.append({"file": rel_path, "health": "unreadable", "score": 0})
            continue

        headings = _HEADING_RE.findall(content)
        wiki_links = extract_wiki_links(content)
        word_count = len(content.split())

        # Health scoring
        score = 100.0

        # Too large → harder to recall
        if size > 4096:
            score -= min((size - 4096) / 1024 * 5, 30)
            issues.append(f"{rel_path}: oversized ({size} bytes) — consider splitting")

        # Too small → likely stub
        if word_count < 10:
            score -= 20
            issues.append(f"{rel_path}: stub ({word_count} words) — needs more content")

        # No structure (no headings)
        if not headings:
            score -= 15
            issues.append(f"{rel_path}: no headings — add structure for better recall")

        # Very old
        if age_days > 30:
            score -= min(age_days / 30 * 5, 20)
            issues.append(f"{rel_path}: {int(age_days)}d old — verify still accurate")

        # Broken wiki-links — search across all knowledge dirs
        kdir_list = [Path(filepath).parent] if not isinstance(knowledge_dir, list) else [Path(d) for d in knowledge_dir]
        for link in wiki_links:
            link_found = False
            for kdir in kdir_list:
                target = kdir / (link if link.endswith(".md") else f"{link}.md")
                if target.exists():
                    link_found = True
                    break
            if not link_found:
                score -= 5
                issues.append(f"{rel_path}: broken wiki-link [[{link}]]")

        file_reports.append({
            "file": rel_path,
            "health": "good" if score >= 70 else "fair" if score >= 40 else "poor",
            "score": round(max(score, 0), 1),
            "size_bytes": size,
            "word_count": word_count,
            "headings": len(headings),
            "wiki_links": len(wiki_links),
            "age_days": round(age_days, 1),
        })

    avg_score = sum(f["score"] for f in file_reports) / len(file_reports) if file_reports else 0

    return {
        "total_files": len(file_reports),
        "overall_score": round(avg_score, 1),
        "overall_health": "good" if avg_score >= 70 else "fair" if avg_score >= 40 else "poor",
        "files": file_reports,
        "issues": issues[:20],  # Cap issues to avoid token explosion
    }


# ---------------------------------------------------------------------------
# Feature 6: Cross-references / Wiki-links
# ---------------------------------------------------------------------------

def extract_wiki_links(content: str) -> list[str]:
    """Extract [[wiki-link]] targets from markdown content."""
    return [m.group(1).strip() for m in _WIKI_LINK_RE.finditer(content)]


def resolve_wiki_links(content: str, knowledge_dir: str | list[str]) -> list[dict]:
    """Resolve all wiki-links in content to actual file paths.

    Args:
        knowledge_dir: Single path or list of paths to search for targets.

    Returns list of {target, resolved_file, exists}.
    """
    links = extract_wiki_links(content)
    dirs = [Path(knowledge_dir)] if isinstance(knowledge_dir, str) else [Path(d) for d in knowledge_dir]
    resolved: list[dict] = []

    for link in links:
        found = False
        for kdir in dirs:
            # Try with and without .md suffix
            candidates = [
                kdir / (link if link.endswith(".md") else f"{link}.md"),
                kdir / f"{link}.md",
            ]
            for candidate in candidates:
                try:
                    candidate.resolve().relative_to(kdir.resolve())
                    resolved.append({
                        "target": link,
                        "resolved_file": str(candidate.relative_to(kdir)),
                        "exists": candidate.exists(),
                    })
                    found = True
                    break
                except ValueError:
                    continue
            if found:
                break
        if not found:
            resolved.append({
                "target": link,
                "resolved_file": link,
                "exists": False,
            })

    return resolved

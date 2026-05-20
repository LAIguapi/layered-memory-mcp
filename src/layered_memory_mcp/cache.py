"""Lightweight LRU cache for knowledge file contents and parsed structures.

Reduces disk I/O by caching file contents with mtime-based invalidation.
Inspired by Semble's _IndexCache which caches built indexes per repo.

Cache layers:
  Layer 1: Raw file content (path → (content, mtime))
  Layer 2: Parsed structures (path → (tokenized_doc, mtime))
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger("layered_memory_mcp.cache")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MAX_ENTRIES = 50  # Max cached files
DEFAULT_TTL_SECONDS = 300.0  # 5 minutes TTL fallback


# ---------------------------------------------------------------------------
# Content cache
# ---------------------------------------------------------------------------

class ContentCache:
    """LRU cache for file contents with mtime-based invalidation.

    Each entry stores:
      - content: str (file content)
      - mtime: float (modification time when cached)
      - cached_at: float (timestamp when added to cache)
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES, ttl: float = DEFAULT_TTL_SECONDS):
        self.max_entries = max_entries
        self.ttl = ttl
        self._cache: OrderedDict[str, tuple[str, float, float]] = OrderedDict()
        # ^ path → (content, mtime, cached_at)

    def _is_stale(self, path: str, cached_mtime: float) -> bool:
        """Check if cached entry is stale (file modified on disk)."""
        try:
            current_mtime = Path(path).stat().st_mtime
            return current_mtime != cached_mtime
        except OSError:
            return True  # File gone → stale

    def get(self, path: str) -> str | None:
        """Get cached content if valid, else None.

        Args:
            path: Absolute file path.

        Returns:
            Cached content string, or None if miss/stale.
        """
        if path not in self._cache:
            return None

        content, mtime, cached_at = self._cache[path]

        # TTL check
        if time.time() - cached_at > self.ttl:
            del self._cache[path]
            return None

        # mtime check
        if self._is_stale(path, mtime):
            del self._cache[path]
            return None

        # LRU: move to end (most recently used)
        self._cache.move_to_end(path)
        return content

    def put(self, path: str, content: str) -> None:
        """Cache file content.

        Args:
            path: Absolute file path.
            content: File content string.
        """
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            mtime = 0.0

        # Evict oldest if at capacity
        if len(self._cache) >= self.max_entries and path not in self._cache:
            self._cache.popitem(last=False)

        self._cache[path] = (content, mtime, time.time())
        self._cache.move_to_end(path)

    def invalidate(self, path: str | None = None) -> None:
        """Invalidate cache entry(s).

        Args:
            path: Specific path to invalidate, or None to clear all.
        """
        if path is None:
            self._cache.clear()
        else:
            self._cache.pop(path, None)

    def stats(self) -> dict:
        """Return cache statistics."""
        return {
            "entries": len(self._cache),
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl,
            "paths": list(self._cache.keys()),
        }


# ---------------------------------------------------------------------------
# Tokenized doc cache (for BM25)
# ---------------------------------------------------------------------------

class TokenizedCache:
    """Cache for tokenized documents to avoid re-tokenizing on every BM25 query.

    Stores:
      - tokens: list[str] (pre-tokenized words)
      - mtime: float (for invalidation)
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES, ttl: float = DEFAULT_TTL_SECONDS):
        self.max_entries = max_entries
        self.ttl = ttl
        self._cache: OrderedDict[str, tuple[list[str], float, float]] = OrderedDict()

    def _is_stale(self, path: str, cached_mtime: float) -> bool:
        try:
            current_mtime = Path(path).stat().st_mtime
            return current_mtime != cached_mtime
        except OSError:
            return True

    def get(self, path: str) -> list[str] | None:
        if path not in self._cache:
            return None

        tokens, mtime, cached_at = self._cache[path]

        if time.time() - cached_at > self.ttl:
            del self._cache[path]
            return None

        if self._is_stale(path, mtime):
            del self._cache[path]
            return None

        self._cache.move_to_end(path)
        return tokens

    def put(self, path: str, tokens: list[str]) -> None:
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            mtime = 0.0

        if len(self._cache) >= self.max_entries and path not in self._cache:
            self._cache.popitem(last=False)

        self._cache[path] = (tokens, mtime, time.time())
        self._cache.move_to_end(path)

    def invalidate(self, path: str | None = None) -> None:
        if path is None:
            self._cache.clear()
        else:
            self._cache.pop(path, None)

    def stats(self) -> dict:
        return {
            "entries": len(self._cache),
            "max_entries": self.max_entries,
            "paths": list(self._cache.keys()),
        }


# ---------------------------------------------------------------------------
# Global cache instances (module-level singletons)
# ---------------------------------------------------------------------------

_content_cache: ContentCache | None = None
_tokenized_cache: TokenizedCache | None = None


def get_content_cache() -> ContentCache:
    """Get the global content cache instance."""
    global _content_cache
    if _content_cache is None:
        _content_cache = ContentCache()
    return _content_cache


def get_tokenized_cache() -> TokenizedCache:
    """Get the global tokenized cache instance."""
    global _tokenized_cache
    if _tokenized_cache is None:
        _tokenized_cache = TokenizedCache()
    return _tokenized_cache


def invalidate_all_caches(path: str | None = None) -> None:
    """Invalidate all cache layers.

    Called after writes to ensure subsequent reads see fresh data.
    """
    get_content_cache().invalidate(path)
    get_tokenized_cache().invalidate(path)
    logger.debug("Invalidated caches for %s", path or "(all)")


# ---------------------------------------------------------------------------
# Cached read helper
# ---------------------------------------------------------------------------

def cached_read_text(filepath: str, encoding: str = "utf-8") -> str | None:
    """Read file text with caching.

    Args:
        filepath: Absolute path to file.
        encoding: Text encoding.

    Returns:
        File content, or None if read fails.
    """
    cache = get_content_cache()

    # Try cache first
    cached = cache.get(filepath)
    if cached is not None:
        return cached

    # Cache miss → read from disk
    try:
        content = Path(filepath).read_text(encoding=encoding, errors="replace")
        cache.put(filepath, content)
        return content
    except Exception as e:
        logger.debug("Failed to read %s: %s", filepath, e)
        return None

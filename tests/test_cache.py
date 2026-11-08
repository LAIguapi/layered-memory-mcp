"""Tests for the cache module."""

import time
import os
from pathlib import Path

import pytest

from layered_memory_mcp.cache import (
    ContentCache,
    TokenizedCache,
    cached_read_text,
    invalidate_all_caches,
)


class TestContentCache:
    def test_basic_get_put(self, tmp_path):
        cache = ContentCache(max_entries=10)
        test_file = tmp_path / "test.md"
        test_file.write_text("hello world")

        # Miss
        assert cache.get(str(test_file)) is None

        # Put and hit
        cache.put(str(test_file), "hello world")
        assert cache.get(str(test_file)) == "hello world"

    def test_mtime_invalidation(self, tmp_path):
        cache = ContentCache(max_entries=10)
        test_file = tmp_path / "test.md"
        test_file.write_text("version 1")
        cache.put(str(test_file), "version 1")

        # Hit
        assert cache.get(str(test_file)) == "version 1"

        # Modify file
        time.sleep(0.1)
        test_file.write_text("version 2")

        # Should be stale
        assert cache.get(str(test_file)) is None

    def test_ttl_expiration(self, tmp_path):
        cache = ContentCache(max_entries=10, ttl=0.1)
        test_file = tmp_path / "test.md"
        test_file.write_text("hello")
        cache.put(str(test_file), "hello")

        # Immediate hit
        assert cache.get(str(test_file)) == "hello"

        # Wait for TTL
        time.sleep(0.15)
        assert cache.get(str(test_file)) is None

    def test_lru_eviction(self, tmp_path):
        cache = ContentCache(max_entries=2)
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f3 = tmp_path / "c.md"
        f1.write_text("a")
        f2.write_text("b")
        f3.write_text("c")

        cache.put(str(f1), "a")
        cache.put(str(f2), "b")
        cache.put(str(f3), "c")  # Should evict f1

        assert cache.get(str(f1)) is None
        assert cache.get(str(f2)) == "b"
        assert cache.get(str(f3)) == "c"

    def test_lru_promotion(self, tmp_path):
        cache = ContentCache(max_entries=2)
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f3 = tmp_path / "c.md"
        f1.write_text("a")
        f2.write_text("b")
        f3.write_text("c")

        cache.put(str(f1), "a")
        cache.put(str(f2), "b")

        # Access f1 (promote to MRU)
        cache.get(str(f1))

        # Add f3 → should evict f2 (LRU)
        cache.put(str(f3), "c")

        assert cache.get(str(f1)) == "a"
        assert cache.get(str(f2)) is None
        assert cache.get(str(f3)) == "c"

    def test_invalidate(self, tmp_path):
        cache = ContentCache(max_entries=10)
        f1 = tmp_path / "a.md"
        f1.write_text("a")
        cache.put(str(f1), "a")

        assert cache.get(str(f1)) == "a"

        cache.invalidate(str(f1))
        assert cache.get(str(f1)) is None

        cache.put(str(f1), "a")
        cache.invalidate()
        assert cache.get(str(f1)) is None

    def test_stats(self, tmp_path):
        cache = ContentCache(max_entries=10)
        f1 = tmp_path / "a.md"
        f1.write_text("a")
        cache.put(str(f1), "a")

        stats = cache.stats()
        assert stats["entries"] == 1
        assert stats["max_entries"] == 10


class TestTokenizedCache:
    def test_basic(self, tmp_path):
        cache = TokenizedCache(max_entries=10)
        test_file = tmp_path / "test.md"
        test_file.write_text("hello world")

        assert cache.get(str(test_file)) is None

        cache.put(str(test_file), ["hello", "world"])
        assert cache.get(str(test_file)) == ["hello", "world"]

    def test_mtime_invalidation(self, tmp_path):
        cache = TokenizedCache(max_entries=10)
        test_file = tmp_path / "test.md"
        test_file.write_text("hello")
        cache.put(str(test_file), ["hello"])

        time.sleep(0.1)
        test_file.write_text("world")

        assert cache.get(str(test_file)) is None


class TestCachedReadText:
    def test_reads_and_caches(self, tmp_path):
        test_file = tmp_path / "test.md"
        test_file.write_text("cached content")

        # First read → disk
        content1 = cached_read_text(str(test_file))
        assert content1 == "cached content"

        # Second read validates the file again before refreshing the cache.
        content2 = cached_read_text(str(test_file))
        assert content2 == "cached content"

    def test_missing_file(self, tmp_path):
        missing = tmp_path / "missing.md"
        assert cached_read_text(str(missing)) is None


class TestInvalidateAllCaches:
    def test_clears_all(self, tmp_path):
        test_file = tmp_path / "test.md"
        test_file.write_text("hello")

        # Populate caches
        cached_read_text(str(test_file))

        # Invalidate
        invalidate_all_caches()

        # Caches should be empty
        from layered_memory_mcp.cache import get_content_cache
        assert len(get_content_cache().stats()["paths"]) == 0


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_cached_reader_validates_current_file_even_with_same_mtime(tmp_path, warm, linked):
    from layered_memory_mcp.cache import get_content_cache

    target = tmp_path / "neutral.md"
    target.write_text("ALLOWED 中性", encoding="utf-8")
    original = target.stat()
    if warm:
        assert cached_read_text(str(target)) == "ALLOWED 中性"
        assert get_content_cache().get(str(target)) == "ALLOWED 中性"
    decoy = tmp_path / "decoy.md"
    decoy.write_text("FORBIDDEN" if linked else "NEW 中性", encoding="utf-8")
    os.utime(decoy, ns=(original.st_atime_ns, original.st_mtime_ns))
    target.unlink()
    if linked:
        target.symlink_to(decoy)
    else:
        os.replace(decoy, target)
    assert target.stat().st_mtime_ns == original.st_mtime_ns
    result = cached_read_text(str(target))
    assert result is None if linked else result == "NEW 中性"


def test_cached_reader_encoding_compatibility_and_content_free_failure(tmp_path, caplog):
    target = tmp_path / "neutral.md"
    target.write_bytes(b"caf\xe9")
    assert cached_read_text(str(target), encoding="latin-1") == "café"
    assert cached_read_text(str(target)) == "caf�"
    caplog.set_level("DEBUG", logger="layered_memory_mcp.cache")
    assert cached_read_text(str(target), encoding="synthetic-invalid-codec") is None
    assert "synthetic-invalid-codec" not in caplog.text
    assert str(target) not in caplog.text

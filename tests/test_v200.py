"""Tests for Layered Memory v2.0 features."""

import tempfile
from pathlib import Path

import pytest

from layered_memory_mcp.models import (
    ConfidenceScorer,
    KnowledgeEntry,
    KnowledgeType,
    ReviewItem,
    SourceInfo,
    SourceType,
)
from layered_memory_mcp.storage import L1Store, ReviewQueue, VectorStore
from layered_memory_mcp.extractor import KnowledgeExtractor, SessionReader


class TestKnowledgeEntry:
    def test_create_entry(self):
        entry = KnowledgeEntry(
            domain="infra",
            section="Network Proxy",
            type=KnowledgeType.CONFIG,
            content="HTTP proxy at 127.0.0.1:8080",
            summary="local proxy configuration",
            tags=["proxy", "wsl"],
        )
        assert entry.domain == "infra"
        assert entry.type == KnowledgeType.CONFIG
        assert entry.confidence == 0.5

    def test_to_frontmatter(self):
        entry = KnowledgeEntry(
            domain="infra",
            section="Network Proxy",
            type=KnowledgeType.CONFIG,
            content="HTTP proxy at 127.0.0.1:8080",
            summary="local proxy configuration",
            tags=["proxy", "wsl"],
        )
        fm = entry.to_frontmatter()
        assert fm.startswith("---")
        assert "type: config" in fm
        assert "domain: infra" in fm

    def test_model_dump_converts_enums(self):
        entry = KnowledgeEntry(
            domain="infra",
            section="Test",
            type=KnowledgeType.PITFALL,
            content="Test content",
        )
        d = entry.model_dump()
        assert d["type"] == "pitfall"
        assert d["review_status"] == "approved"
        assert isinstance(d["source"], dict)
        assert d["source"]["type"] == "manual"


class TestConfidenceScorer:
    def test_low_confidence(self):
        entry = KnowledgeEntry(
            domain="test",
            section="Test",
            content="Some generic text without specifics",
            source=SourceInfo(type=SourceType.SESSION, session_id="s1"),
        )
        score = ConfidenceScorer.score(entry)
        assert 0.0 <= score < 0.5

    def test_high_confidence(self):
        entry = KnowledgeEntry(
            domain="infra",
            section="Network Proxy",
            content="""HTTP proxy configured at 127.0.0.1:8080 for local.

Run this command to verify:
\`\`\`bash
$ curl -x http://127.0.0.1:8080 https://api.github.com
\`\`\`

Test passed — connection verified successfully.
""",
            summary="local proxy config",
            source=SourceInfo(type=SourceType.SESSION, session_id="s1", message_range=(0, 15)),
        )
        score = ConfidenceScorer.score(entry)
        assert score >= 0.9

    def test_auto_review(self):
        entry = KnowledgeEntry(
            domain="test",
            section="Test",
            content="Verified solution: run `python setup.py install` and test passed. "
                    "Root cause identified: missing dependency. "
                    "Fixed by adding requirements.txt. Test passed successfully. "
                    "Confirmed working on production server.",
            source=SourceInfo(type=SourceType.SESSION, session_id="s1", message_range=(0, 25)),
        )
        status = ConfidenceScorer.auto_review(entry, threshold=0.8)
        assert status.value == "approved"
        assert entry.reviewed_by == "auto"


class TestL1Store:
    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = L1Store(Path(tmp))
            entry = KnowledgeEntry(
                domain="infra",
                section="Network Proxy",
                type=KnowledgeType.CONFIG,
                content="HTTP proxy at 127.0.0.1:8080",
            )
            result = store.write(entry)
            assert result["success"]

            meta, content = store.read("infra")
            assert meta is not None
            assert meta["type"] == "config"
            assert "HTTP proxy" in content

    def test_list_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = L1Store(Path(tmp))
            store.write(KnowledgeEntry(domain="infra", section="A", content="a"))
            store.write(KnowledgeEntry(domain="dev", section="B", content="b"))
            domains = store.list_domains()
            assert sorted(domains) == ["dev", "infra"]


class TestVectorStore:
    def test_add_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            vs = VectorStore(Path(tmp) / "vectors.db")
            entry = KnowledgeEntry(
                domain="infra",
                section="Network Proxy",
                content="HTTP proxy at 127.0.0.1:8080 for local external access",
                summary="local proxy config",
            )
            vs.add(entry)
            
            results = vs.search("How to configure network proxy?", top_n=3)
            assert len(results) >= 1
            assert results[0]["domain"] == "infra"

    def test_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            vs = VectorStore(Path(tmp) / "vectors.db")
            stats = vs.stats()
            assert stats["total_entries"] == 0
            assert stats["is_fitted"] is False


class TestReviewQueue:
    def test_submit_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            rq = ReviewQueue(Path(tmp) / "reviews.db")
            entry = KnowledgeEntry(domain="test", section="A", content="test")
            item = ReviewItem(entry=entry)
            rq.submit(item)
            
            stats = rq.get_stats()
            assert stats["total"] == 1
            assert stats["pending"] == 1
            
            pending = rq.list_pending()
            assert len(pending) == 1

    def test_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            rq = ReviewQueue(Path(tmp) / "reviews.db")
            entry = KnowledgeEntry(domain="test", section="A", content="test")
            item = ReviewItem(entry=entry)
            rq.submit(item)
            
            result = rq.approve(entry.id, reviewer="human", note="Looks good")
            assert result["success"]
            
            stats = rq.get_stats()
            assert stats["approved"] == 1
            assert stats["pending"] == 0


class TestSessionReader:
    def test_read_recent_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = SessionReader(tmp)
            sessions = reader.read_recent(days=3)
            assert sessions == []

    def test_get_stats_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = SessionReader(tmp)
            stats = reader.get_session_stats(days=3)
            assert stats["total_sessions"] == 0


class TestKnowledgeExtractor:
    def test_extract_from_empty_session(self):
        from layered_memory_mcp.extractor.session_reader import Session
        session = Session(path="/tmp/test.json", session_id="test", messages=[], mtime=None, size=0)
        extractor = KnowledgeExtractor()
        items = extractor.extract_from_session(session)
        assert items == []

    def test_extraction_stats_empty(self):
        extractor = KnowledgeExtractor()
        stats = extractor.get_extraction_stats([])
        assert stats["total"] == 0

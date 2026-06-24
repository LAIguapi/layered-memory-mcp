"""Tests for Layered Memory MCP Server."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.recall import (
    recall,
    scan_knowledge_files,
    score_relevance,
    extract_relevant_sections,
)
from layered_memory_mcp.session_scanner import (
    scan_sessions,
    find_recent_sessions,
    extract_session_summary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def knowledge_dir(tmp_path):
    """Create a temp knowledge directory with sample files."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir()

    (kdir / "infrastructure.md").write_text("""## Servers
- Production: prod.example.com
- Staging: stage.example.com

## Database
- PostgreSQL on db.example.com:5432
""", encoding="utf-8")

    (kdir / "development.md").write_text("""## Code Style
- Python 3.12+
- Formatter: ruff format

## Testing
- Framework: pytest
""", encoding="utf-8")

    return str(kdir)

@pytest.fixture
def sessions_dir(tmp_path):
    """Create a temp sessions directory with a sample session."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    session_file = sdir / "2026-01-01.jsonl"
    lines = [
        json.dumps({"role": "user", "content": "How do I deploy to production?"}),
        json.dumps({"role": "assistant", "content": "You can deploy using ./deploy.sh --env production"}),
        json.dumps({"role": "user", "content": "What's the database connection?"}),
        json.dumps({"role": "assistant", "content": "PostgreSQL is on db.example.com:5432"}),
    ]
    session_file.write_text("\n".join(lines), encoding="utf-8")

    return str(sdir)


@pytest.fixture
def mixed_sessions_dir(tmp_path):
    """Sessions dir with both valid and invalid files (to test filtering)."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    # Valid JSONL session
    (sdir / "2026-01-01.jsonl").write_text(
        "\n".join([
            json.dumps({"role": "user", "content": "Hello, how are you doing today my friend?"}),
            json.dumps({"role": "assistant", "content": "Hi there, I am doing great, thank you for asking!"}),
        ]),
        encoding="utf-8",
    )

    # Should be excluded: config.json
    (sdir / "config.json").write_text('{"key": "value"}', encoding="utf-8")

    # Should be excluded: package.json
    (sdir / "package.json").write_text('{"name": "test"}', encoding="utf-8")

    # Should be excluded: dotfile
    (sdir / ".hidden.json").write_text('{"hidden": true}', encoding="utf-8")

    # Should be excluded: too small (< 100 bytes)
    tiny = sdir / "tiny.jsonl"
    tiny.write_text('{"role":"user","content":"x"}', encoding="utf-8")

    # Valid JSON session (non-excluded name) — Hermes .json format with messages array
    (sdir / "2026-01-02.json").write_text(
        json.dumps({
            "session_id": "test-session-001",
            "messages": [
                {"role": "user", "content": "JSON session test that is long enough to pass the minimum size threshold check"},
                {"role": "assistant", "content": "Response to make it bigger than one hundred bytes total for the filter"},
            ],
        }),
        encoding="utf-8",
    )

    return str(sdir)


# ---------------------------------------------------------------------------
# Recall Tests
# ---------------------------------------------------------------------------

class TestRecall:
    def test_scan_knowledge_files(self, knowledge_dir):
        files = scan_knowledge_files(knowledge_dir)
        assert len(files) == 2
        assert "infrastructure.md" in files
        assert "development.md" in files

    def test_recall_by_keyword(self, knowledge_dir):
        result = recall("database", knowledge_dir)
        assert result["success"] is True
        assert result["matched_files"] >= 1

        files_found = [r["file"] for r in result["results"]]
        assert "infrastructure.md" in files_found

    def test_recall_no_match(self, knowledge_dir):
        result = recall("nonexistent-topic-xyz", knowledge_dir)
        assert result["success"] is True
        assert result["matched_files"] == 0

    def test_recall_empty_dir(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = recall("test", str(empty_dir))
        assert result["success"] is False
        assert "No knowledge files" in result["error"]

    def test_score_relevance(self):
        score = score_relevance(
            "database",
            "## Database\n- PostgreSQL on db.example.com",
            "infrastructure.md"
        )
        assert score > 0

    def test_score_relevance_filename_match(self):
        score = score_relevance(
            "infra",
            "some content",
            "infrastructure.md"
        )
        # +10 for filename match
        assert score >= 10.0

    def test_score_relevance_heading_bonus(self):
        score = score_relevance(
            "database",
            "## Database\nsome content here",
            "other.md"
        )
        # +3 for heading match
        assert score >= 3.0

    def test_recall_top_n(self, knowledge_dir):
        result = recall("test", knowledge_dir, top_n=1)
        assert len(result["results"]) <= 1

    def test_extract_relevant_sections(self):
        content = "## Database\n- PostgreSQL on db.example.com\n\n## Cache\n- Redis on cache.example.com"
        sections = extract_relevant_sections("database", content)
        assert len(sections) == 1
        assert sections[0]["title"] == "Database"

    def test_extract_relevant_sections_overview(self):
        content = "Some intro text with keyword\n\n## Other\nno match"
        sections = extract_relevant_sections("keyword", content)
        assert len(sections) == 1
        assert sections[0]["title"] == "Overview"

    def test_extract_relevant_sections_multiple(self):
        content = "## DB Host\n- database is here\n\n## DB Port\n- database port 5432\n\n## Unrelated\nnope"
        sections = extract_relevant_sections("database", content, max_sections=3)
        assert len(sections) == 2

    def test_extract_relevant_sections_h1_heading(self):
        """H1 headings (# ) should also be detected as section boundaries."""
        content = "# Main Title\nSome database info here\n\n## Subsection\nother stuff"
        sections = extract_relevant_sections("database", content)
        assert len(sections) == 1
        assert sections[0]["title"] == "Main Title"

    def test_extract_relevant_sections_h3_heading(self):
        """H3 headings (### ) should also be detected as section boundaries."""
        content = "### Database Config\n- host: db.example.com\n\n### Cache Config\n- redis"
        sections = extract_relevant_sections("database", content)
        assert len(sections) == 1
        assert sections[0]["title"] == "Database Config"

    def test_score_relevance_h1_heading_bonus(self):
        """H1 headings should also get the heading match bonus."""
        score = score_relevance(
            "database",
            "# Database Overview\nsome content",
            "other.md"
        )
        assert score >= 3.0

    def test_recall_chinese_keyword(self, tmp_path):
        """Verify Chinese keyword matching works."""
        kdir = tmp_path / "knowledge"
        kdir.mkdir()
        (kdir / "chinese.md").write_text("## 数据库配置\n- PostgreSQL 地址: db.example.com", encoding="utf-8")
        result = recall("数据库", str(kdir))
        assert result["success"] is True
        assert result["matched_files"] >= 1


# ---------------------------------------------------------------------------
# Session Scanner Tests
# ---------------------------------------------------------------------------

class TestSessionScanner:
    def test_scan_sessions(self, sessions_dir):
        result = scan_sessions(sessions_dir, days=30)
        assert result["total_sessions"] >= 1
        assert len(result["sessions"]) >= 1

    def test_session_summary_content(self, sessions_dir):
        result = scan_sessions(sessions_dir, days=30)
        session = result["sessions"][0]
        assert len(session["user_messages"]) > 0
        assert "deploy" in " ".join(session["user_messages"]).lower()

    def test_scan_sessions_empty_dir(self, tmp_path):
        empty_dir = tmp_path / "empty_sessions"
        empty_dir.mkdir()
        result = scan_sessions(str(empty_dir), days=30)
        assert result["total_sessions"] == 0

    def test_filter_non_session_json(self, mixed_sessions_dir):
        """JSON files like config.json, package.json should be excluded."""
        sessions = find_recent_sessions(mixed_sessions_dir, days=30)
        paths = [s["path"] for s in sessions]

        # Should include the valid JSONL and valid JSON session
        assert any("2026-01-01.jsonl" in p for p in paths)
        assert any("2026-01-02.json" in p for p in paths)

        # Should NOT include excluded files
        assert not any("config.json" in p for p in paths)
        assert not any("package.json" in p for p in paths)
        assert not any(".hidden.json" in p for p in paths)

    def test_filter_tiny_files(self, mixed_sessions_dir):
        """Files below MIN_SESSION_SIZE should be skipped."""
        sessions = find_recent_sessions(mixed_sessions_dir, days=30)
        paths = [s["path"] for s in sessions]
        assert not any("tiny.jsonl" in p for p in paths)

    def test_extract_session_summary_truncation(self, tmp_path):
        """Large session files should be truncated at max_lines."""
        sdir = tmp_path / "sessions"
        sdir.mkdir()
        lines = [json.dumps({"role": "user", "content": f"Message {i}"}) for i in range(100)]
        session_file = sdir / "large.jsonl"
        session_file.write_text("\n".join(lines), encoding="utf-8")

        summary = extract_session_summary(str(session_file), max_messages=10)
        assert summary.get("truncated") is True
        assert len(summary["user_messages"]) == 10

    def test_extract_session_tool_calls(self, tmp_path):
        """Tool calls should be extracted from session entries."""
        sdir = tmp_path / "sessions"
        sdir.mkdir()
        lines = [
            json.dumps({
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "recall_knowledge"}, "id": "1"}],
            }),
        ]
        (sdir / "tools.jsonl").write_text("\n".join(lines), encoding="utf-8")

        summary = extract_session_summary(str(sdir / "tools.jsonl"))
        assert "recall_knowledge" in summary["tool_calls"]


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_HOME", str(tmp_path / "mem"))
        config = MemoryConfig()
        assert config.home == tmp_path / "mem"
        assert config.knowledge_dir.exists()
        assert config.home.exists()  # home dir should be auto-created

    def test_custom_knowledge_dir(self, tmp_path):
        kdir = tmp_path / "custom-knowledge"
        config = MemoryConfig(knowledge_dir=str(kdir))
        assert config.knowledge_dir == kdir
        assert config.knowledge_dir.exists()

    def test_home_dir_created(self, tmp_path, monkeypatch):
        """Home directory should be auto-created on init."""
        new_home = tmp_path / "new" / "deep" / "home"
        monkeypatch.setenv("LAYERED_MEMORY_HOME", str(new_home))
        config = MemoryConfig()
        assert config.home.exists()

    def test_sessions_dir_none_when_missing(self, monkeypatch):
        """If no sessions dir env and no ~/.hermes/sessions, should be None."""
        monkeypatch.delenv("LAYERED_MEMORY_SESSIONS_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent"))
        config = MemoryConfig()
        assert config.sessions_dir is None

    def test_l0_index_file_default_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_HOME", str(tmp_path / "mem"))
        config = MemoryConfig()
        assert config.l0_index_file is None


# ---------------------------------------------------------------------------
# Server-level Tool Tests — Read
# ---------------------------------------------------------------------------

class TestServerTools:
    """Test MCP tool functions directly (bypassing transport)."""

    @pytest.mark.asyncio
    async def test_recall_knowledge_tool(self, knowledge_dir, tmp_path, monkeypatch):
        from layered_memory_mcp.server import recall_knowledge
        monkeypatch.setenv("LAYERED_MEMORY_HOME", str(tmp_path / "home"))
        # Override config to use our test knowledge dir
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=knowledge_dir)

        result_json = await recall_knowledge(keyword="database")
        result = json.loads(result_json)
        assert result["success"] is True
        assert result["matched_files"] >= 1

        # Cleanup
        srv._config = None

    @pytest.mark.asyncio
    async def test_get_knowledge_file_tool(self, knowledge_dir, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=knowledge_dir)

        result_json = await srv.get_knowledge_file(filename="infrastructure.md")
        result = json.loads(result_json)
        assert result["success"] is True
        assert "## Servers" in result["content"]

        srv._config = None

    @pytest.mark.asyncio
    async def test_get_knowledge_file_not_found(self, knowledge_dir, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=knowledge_dir)

        result_json = await srv.get_knowledge_file(filename="nonexistent.md")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "not found" in result["error"].lower()

        srv._config = None

    @pytest.mark.asyncio
    async def test_get_knowledge_file_path_traversal(self, knowledge_dir, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=knowledge_dir)

        # Non-.md extension is rejected with clear error
        result_json = await srv.get_knowledge_file(filename="../../etc/passwd")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "extension" in result["error"].lower() or "traversal" in result["error"].lower()

        # .md file with path traversal is rejected with traversal error
        result_json = await srv.get_knowledge_file(filename="../../etc/secret.md")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "traversal" in result["error"].lower()

        srv._config = None

    @pytest.mark.asyncio
    async def test_list_memory_stats_tool(self, knowledge_dir, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=knowledge_dir)

        result_json = await srv.list_memory_stats()
        result = json.loads(result_json)
        assert "l1_knowledge" in result
        assert result["l1_knowledge"]["total_files"] == 2
        assert isinstance(result["l1_knowledge"]["total_size_bytes"], int)

        srv._config = None

    @pytest.mark.asyncio
    async def test_list_memory_stats_empty(self, tmp_path):
        """Empty knowledge dir should return 0 avg and a helpful suggestion."""
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "empty_k"))

        result_json = await srv.list_memory_stats()
        result = json.loads(result_json)
        assert result["l1_knowledge"]["total_files"] == 0
        assert result["l1_knowledge"]["avg_size_kb"] == 0
        assert len(result["suggestions"]) > 0

        srv._config = None

    @pytest.mark.asyncio
    async def test_scan_recent_sessions_no_dir(self, tmp_path, monkeypatch):
        import layered_memory_mcp.server as srv
        # Force sessions_dir to a non-existent path
        srv._config = MemoryConfig(
            home=str(tmp_path / "home"),
            sessions_dir=str(tmp_path / "no_sessions"),
        )

        result_json = await srv.scan_recent_sessions()
        result = json.loads(result_json)
        assert result["success"] is False

        srv._config = None

    @pytest.mark.asyncio
    async def test_search_sessions_no_dir(self, tmp_path, monkeypatch):
        import layered_memory_mcp.server as srv
        # Force sessions_dir to a non-existent path
        srv._config = MemoryConfig(
            home=str(tmp_path / "home"),
            sessions_dir=str(tmp_path / "no_sessions"),
        )

        result_json = await srv.search_sessions_by_keyword(keyword="test")
        result = json.loads(result_json)
        assert result["success"] is False

        srv._config = None


# ---------------------------------------------------------------------------
# Server-level Tool Tests — Write
# ---------------------------------------------------------------------------

class TestServerWriteTools:
    """Test MCP write tool functions."""

    @pytest.mark.asyncio
    async def test_create_knowledge_file(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        result_json = await srv.create_knowledge_file(
            filename="new-file.md",
            content="# New File\nSome test content",
        )
        result = json.loads(result_json)
        assert result["success"] is True
        assert result["action"] == "created"
        assert (tmp_path / "k" / "new-file.md").exists()

        srv._config = None

    @pytest.mark.asyncio
    async def test_create_knowledge_file_rejects_non_md(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        result_json = await srv.create_knowledge_file(
            filename="script.py",
            content="print('hello')",
        )
        result = json.loads(result_json)
        assert result["success"] is False
        assert ".md" in result["error"].lower()

        srv._config = None

    @pytest.mark.asyncio
    async def test_create_knowledge_file_rejects_path_separator(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        result_json = await srv.create_knowledge_file(
            filename="sub/dir/file.md",
            content="content",
        )
        result = json.loads(result_json)
        assert result["success"] is False

        srv._config = None

    @pytest.mark.asyncio
    async def test_create_knowledge_file_rejects_existing(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))
        # Pre-create the file
        (tmp_path / "k" / "existing.md").write_text("old", encoding="utf-8")

        result_json = await srv.create_knowledge_file(
            filename="existing.md",
            content="new content",
        )
        result = json.loads(result_json)
        assert result["success"] is False
        assert "already exists" in result["error"]

        srv._config = None

    @pytest.mark.asyncio
    async def test_update_knowledge_file(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))
        (tmp_path / "k" / "update.md").write_text("old content", encoding="utf-8")

        result_json = await srv.update_knowledge_file(
            filename="update.md",
            content="new content here",
        )
        result = json.loads(result_json)
        assert result["success"] is True
        assert result["action"] == "updated"
        assert (tmp_path / "k" / "update.md").read_text() == "new content here"

        srv._config = None

    @pytest.mark.asyncio
    async def test_update_knowledge_file_not_found(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        result_json = await srv.update_knowledge_file(
            filename="nonexistent.md",
            content="content",
        )
        result = json.loads(result_json)
        assert result["success"] is False
        assert "not found" in result["error"].lower()

        srv._config = None

    @pytest.mark.asyncio
    async def test_update_knowledge_file_path_traversal(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        # Non-.md extension rejected first
        result_json = await srv.update_knowledge_file(
            filename="../../etc/passwd",
            content="hacked",
        )
        result = json.loads(result_json)
        assert result["success"] is False
        assert "extension" in result["error"].lower() or "traversal" in result["error"].lower()

        # .md with path traversal
        result_json = await srv.update_knowledge_file(
            filename="../../etc/secret.md",
            content="hacked",
        )
        result = json.loads(result_json)
        assert result["success"] is False
        assert "traversal" in result["error"].lower()

        srv._config = None

    @pytest.mark.asyncio
    async def test_delete_knowledge_file(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))
        (tmp_path / "k" / "delete-me.md").write_text("bye bye", encoding="utf-8")

        result_json = await srv.delete_knowledge_file(filename="delete-me.md")
        result = json.loads(result_json)
        assert result["success"] is True
        assert result["action"] == "deleted"
        assert not (tmp_path / "k" / "delete-me.md").exists()

        srv._config = None

    @pytest.mark.asyncio
    async def test_delete_knowledge_file_not_found(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        result_json = await srv.delete_knowledge_file(filename="nonexistent.md")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "not found" in result["error"].lower()

        srv._config = None

    @pytest.mark.asyncio
    async def test_delete_knowledge_file_path_traversal(self, tmp_path):
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        # Non-.md extension rejected first
        result_json = await srv.delete_knowledge_file(filename="../../etc/passwd")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "extension" in result["error"].lower() or "traversal" in result["error"].lower()

        # .md with path traversal
        result_json = await srv.delete_knowledge_file(filename="../../etc/secret.md")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "traversal" in result["error"].lower()

        srv._config = None

    @pytest.mark.asyncio
    async def test_update_knowledge_file_rejects_non_md(self, tmp_path):
        """update should reject non-.md files (unified validation)."""
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        result_json = await srv.update_knowledge_file(
            filename="dangerous.py",
            content="import os",
        )
        result = json.loads(result_json)
        assert result["success"] is False
        # Unified validation rejects non-.md via _validate_knowledge_path
        assert "traversal" in result["error"].lower() or "invalid" in result["error"].lower()

        srv._config = None

    @pytest.mark.asyncio
    async def test_delete_knowledge_file_rejects_non_md(self, tmp_path):
        """delete should reject non-.md files (unified validation)."""
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))
        (tmp_path / "k" / "config.yaml").write_text("key: value", encoding="utf-8")

        result_json = await srv.delete_knowledge_file(filename="config.yaml")
        result = json.loads(result_json)
        assert result["success"] is False
        # File should still exist
        assert (tmp_path / "k" / "config.yaml").exists()

        srv._config = None


# ---------------------------------------------------------------------------
# JSON Session Format Tests
# ---------------------------------------------------------------------------

class TestJsonSessionFormat:
    """Test that Hermes .json session files are correctly parsed."""

    def test_extract_hermes_json_session(self, tmp_path):
        """Hermes .json format: {"session_id": "...", "messages": [...]}"""
        sdir = tmp_path / "sessions"
        sdir.mkdir()

        session_data = {
            "session_id": "test-hermes-001",
            "messages": [
                {"role": "user", "content": "How do I configure the database?"},
                {"role": "assistant", "content": "You can configure it in config.yaml"},
                {"role": "user", "content": "What about caching?"},
                {"role": "assistant", "content": "Redis is configured in docker-compose.yml"},
            ],
        }
        session_file = sdir / "hermes_session.json"
        session_file.write_text(json.dumps(session_data), encoding="utf-8")

        summary = extract_session_summary(str(session_file))
        assert len(summary["user_messages"]) == 2
        assert any("database" in m for m in summary["user_messages"])
        assert len(summary["assistant_topics"]) == 2
        assert "error" not in summary

    def test_extract_jsonl_session(self, tmp_path):
        """JSONL format: one JSON object per line."""
        sdir = tmp_path / "sessions"
        sdir.mkdir()

        lines = [
            json.dumps({"role": "user", "content": "Deploy to staging"}),
            json.dumps({"role": "assistant", "content": "Running deploy.sh --staging"}),
        ]
        (sdir / "session.jsonl").write_text("\n".join(lines), encoding="utf-8")

        summary = extract_session_summary(str(sdir / "session.jsonl"))
        assert len(summary["user_messages"]) == 1
        assert "deploy" in summary["user_messages"][0].lower()

    def test_scan_sessions_with_json(self, mixed_sessions_dir):
        """scan_sessions should handle both JSONL and JSON files."""
        result = scan_sessions(mixed_sessions_dir, days=30)
        assert result["total_sessions"] >= 2  # JSONL + JSON session

    def test_search_sessions_by_keyword_json(self, tmp_path):
        """search_sessions_by_keyword should find matches in .json session files."""
        sdir = tmp_path / "sessions"
        sdir.mkdir()

        session_data = {
            "session_id": "keyword-test",
            "messages": [
                {"role": "user", "content": "How do I deploy Kubernetes?"},
                {"role": "assistant", "content": "Use kubectl apply -f deployment.yaml"},
            ],
        }
        (sdir / "k8s.json").write_text(json.dumps(session_data), encoding="utf-8")

        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(sessions_dir=str(sdir))

        # Run async test
        import asyncio
        result_json = asyncio.get_event_loop().run_until_complete(
            srv.search_sessions_by_keyword(keyword="kubernetes", days=30)
        )
        result = json.loads(result_json)
        assert result["success"] is True
        assert result["matched_sessions"] >= 1

        srv._config = None


# ---------------------------------------------------------------------------
# v0.7.3: Search mode + merge + namespace + validation tests
# ---------------------------------------------------------------------------

class TestSearchModes:
    """Test fuzzy, bm25, and hybrid search modes."""

    def test_fuzzy_search_finds_similar(self, knowledge_dir):
        """Fuzzy search should find files with similar but not exact keywords."""
        result = recall("infratructure", knowledge_dir, top_n=5, search_mode="fuzzy")
        assert result["success"] is True
        assert result["matched_files"] >= 1
        found_files = [r["file"] for r in result["results"]]
        assert any("infrastructure" in f for f in found_files)

    def test_bm25_search_ranks_relevant(self, knowledge_dir):
        """BM25 should rank files with more keyword hits higher."""
        result = recall("database", knowledge_dir, top_n=5, search_mode="bm25")
        assert result["success"] is True
        assert result["matched_files"] >= 1
        found_files = [r["file"] for r in result["results"]]
        assert any("infrastructure" in f for f in found_files)

    def test_hybrid_search_combines_scores(self, knowledge_dir):
        """Hybrid search should combine keyword + fuzzy scores."""
        result = recall("test", knowledge_dir, top_n=5, search_mode="hybrid")
        assert result["success"] is True
        assert result["matched_files"] >= 1
        found_files = [r["file"] for r in result["results"]]
        assert any("development" in f for f in found_files)

    def test_search_modes_return_scores(self, knowledge_dir):
        """All search modes should return numeric scores."""
        for mode in ["keyword", "fuzzy", "bm25", "hybrid"]:
            result = recall("database", knowledge_dir, top_n=5, search_mode=mode)
            if result["matched_files"] > 0:
                assert all(isinstance(r["score"], (int, float)) for r in result["results"]), \
                    f"Mode {mode} returned non-numeric scores"


class TestMergeMode:
    """Test inject_knowledge merge mode."""

    def test_merge_adds_unique_lines_only(self, tmp_path):
        """Merge should only add lines not already in the section."""
        from layered_memory_mcp.injector import inject_knowledge

        kdir = tmp_path / "knowledge"
        kdir.mkdir()
        config = MemoryConfig(knowledge_dir=str(kdir))

        r1 = inject_knowledge(config, domain="test", section="Notes",
                              content="- Python 3.12\n- ruff format", mode="append")
        assert r1["success"] is True

        r2 = inject_knowledge(config, domain="test", section="Notes",
                              content="- Python 3.12\n- pytest\n- ruff format",
                              mode="merge")
        assert r2["success"] is True
        assert r2.get("action") in ("merged", "merged_no_change")

        content = (kdir / "test.md").read_text(encoding="utf-8")
        assert "pytest" in content

    def test_merge_no_change_when_all_duplicate(self, tmp_path):
        """Merge should return merged_no_change when all content already exists."""
        from layered_memory_mcp.injector import inject_knowledge

        kdir = tmp_path / "knowledge"
        kdir.mkdir()
        config = MemoryConfig(knowledge_dir=str(kdir))

        inject_knowledge(config, domain="test", section="Notes",
                         content="- Line A\n- Line B", mode="append")
        r = inject_knowledge(config, domain="test", section="Notes",
                             content="- Line A\n- Line B", mode="merge")
        assert r["success"] is True
        # v2.8.0+ exact-match dedup makes an all-duplicate merge a no-op; the
        # store reports "skipped" (more precise than the older
        # "merged_no_change"). Accept either for backward compat.
        assert r.get("action") in ("merged_no_change", "skipped")


class TestNamespace:
    """Test namespace-aware knowledge isolation."""

    def test_namespace_creates_separate_dirs(self, tmp_path):
        """Namespace config should create separate knowledge directories."""
        config = MemoryConfig(
            knowledge_dir=str(tmp_path / "knowledge"),
            namespace="agent-alpha",
        )
        assert config.knowledge_dir == tmp_path / "knowledge" / "agent-alpha"
        assert len(config.knowledge_dirs) == 2
        assert config.knowledge_dirs[0] == tmp_path / "knowledge" / "agent-alpha"
        assert config.knowledge_dirs[1] == tmp_path / "knowledge" / "shared"

    def test_shared_namespace_no_shared_dir(self, tmp_path):
        """Shared namespace should have single knowledge dir."""
        config = MemoryConfig(
            knowledge_dir=str(tmp_path / "knowledge"),
            namespace="shared",
        )
        assert len(config.knowledge_dirs) == 1

    def test_namespace_searches_both_dirs(self, tmp_path):
        """Recall should search both namespace and shared directories."""
        kroot = tmp_path / "knowledge"
        ns_dir = kroot / "agent-alpha"
        shared_dir = kroot / "shared"
        ns_dir.mkdir(parents=True)
        shared_dir.mkdir(parents=True)

        (ns_dir / "private.md").write_text("# Private\nAgent alpha notes and knowledge", encoding="utf-8")
        (shared_dir / "common.md").write_text("# Common\nShared knowledge for all agents", encoding="utf-8")

        config = MemoryConfig(
            knowledge_dir=str(kroot),
            namespace="agent-alpha",
        )
        kdirs = [str(d) for d in config.knowledge_dirs]
        result = recall("agent", kdirs, top_n=10)
        assert result["success"] is True
        found_files = [r["file"] for r in result["results"]]
        assert any("private" in f for f in found_files)
        assert any("common" in f for f in found_files)


class TestFilenameValidation:
    """Test filename validation edge cases."""

    @pytest.mark.asyncio
    async def test_rejects_filename_too_long(self, tmp_path):
        """Filenames exceeding 255 chars should be rejected."""
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))

        long_name = "a" * 253 + ".md"  # 256 chars total

        result_json = await srv.create_knowledge_file(filename=long_name, content="test")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "long" in result["error"].lower() or "255" in result["error"]

        srv._config = None

    @pytest.mark.asyncio
    async def test_sync_l0_format_no_race(self, tmp_path):
        """sync_l0_index_tool should not mutate config.l0_format."""
        import layered_memory_mcp.server as srv
        config = MemoryConfig(
            knowledge_dir=str(tmp_path / "k"),
            l0_format="hermes",
        )
        srv._config = config

        result_json = await srv.sync_l0_index_tool(format="generic")
        result = json.loads(result_json)
        # Config should NOT be changed
        assert config.l0_format == "hermes"

        srv._config = None


class TestDeleteBakCleanup:
    """Test .bak cleanup on delete."""

    @pytest.mark.asyncio
    async def test_delete_cleans_bak(self, tmp_path):
        """Deleting a file should also clean up its .bak backup."""
        import layered_memory_mcp.server as srv
        srv._config = MemoryConfig(knowledge_dir=str(tmp_path / "k"))
        kdir = tmp_path / "k"

        (kdir / "test.md").write_text("test content", encoding="utf-8")
        (kdir / "test.md.bak").write_text("old content", encoding="utf-8")

        assert (kdir / "test.md.bak").exists()

        result_json = await srv.delete_knowledge_file(filename="test.md")
        result = json.loads(result_json)
        assert result["success"] is True
        assert not (kdir / "test.md").exists()
        assert not (kdir / "test.md.bak").exists()

        srv._config = None

"""Session extraction must distinguish durable surfaces without applying writes."""
import json

import pytest

from layered_memory_mcp import server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.models import KnowledgeEntry, ReviewItem


@pytest.mark.asyncio
@pytest.mark.parametrize("extensions_enabled", [False, True])
async def test_extraction_returns_routed_candidates_without_writes(tmp_path, monkeypatch, extensions_enabled):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    cfg = MemoryConfig(home=str(tmp_path / "store"), sessions_dir=str(sessions))
    if extensions_enabled:
        cfg.agent_extensions = {
            "enabled": True, "mode": "apply", "root": str(cfg.home / "extensions"),
            "surfaces": {surface: {"enabled": True, "handler": "generic_file",
                                  "path": f"{surface}.md"}
                         for surface in ("skill", "user_profile")},
        }
    server._config = cfg
    samples = {
        "fact": "Architecture decision: PostgreSQL is the source of truth; Redis is a disposable cache.",
        "skill": "Deployment runbook:\n1. Run `make build`.\n2. Execute `make test`.\n3. Verify the release.",
        "todo": "TODO: pending validation of the cache migration; blocked until the owner confirms.",
        "profile": "User preference: always give concise answers in all conversations, across all projects.",
        "trace": "Traceback (most recent call last):\n  File \"app.py\", line 12\nValueError: temporary failure\n",
        "unknown": "perhaps",
    }
    items = [ReviewItem(entry=KnowledgeEntry(domain="sample", section=key, content=body))
             for key, body in samples.items()]
    monkeypatch.setattr(server.SessionReader, "read_recent", lambda *args, **kwargs: [object()])
    monkeypatch.setattr(server.KnowledgeExtractor, "extract_from_sessions", lambda *args, **kwargs: items)
    before = sorted(str(p.relative_to(cfg.home)) for p in cfg.home.rglob("*"))
    result = json.loads(await server.extract_session_knowledge())
    assert result["success"]
    assert len(result["entries"]) == len(items)  # legacy response remains available
    assert [e["section"] for e in result["l1_candidates"]] == ["fact"]
    assert [e["section"] for e in result["todo_candidates"]] == ["todo"]
    assert {e["section"] for e in result["agent_owned_candidates"]} == {"skill", "profile"}
    assert [e["section"] for e in result["raw_session_candidates"]] == ["trace"]
    assert [e["section"] for e in result["rejected"]] == ["unknown"]
    assert result["routing_summary"]["total"] == len(items)
    for candidate in result["agent_owned_candidates"]:
        decision = candidate["routing"]
        assert decision["ownership"] == "agent"
        assert decision["framework_can_apply"] is extensions_enabled
        assert decision["suggested_action"]["source_content"] == candidate["content"]
    assert sorted(str(p.relative_to(cfg.home)) for p in cfg.home.rglob("*")) == before

"""v3.6.1 — one graph recall policy for MCP keyword, MCP semantic and REST search.

Why this file exists: ARCHITECTURE_v2.md "Framework completion and migration
delivery contract" — "Unify graph use for MCP ``search_semantic``, MCP
``recall_knowledge``, and REST ``/knowledge/search``. … A healthy graph's
temporal/lifecycle/scope filters must affect the returned primary result, not
just an optional sidecar."

What is pinned here:

  * a healthy graph's lifecycle/scope filters remove a stale row from the
    PRIMARY result list of all three transports, not just from a sidecar;
  * an unmapped row is preserved — legacy results survive;
  * a safe unavailable graph preserves the documented legacy payload exactly;
  * a known-pending projection excludes the affected row even under that
    unavailable-graph fallback, so an accepted-but-unpublished write can never
    be answered with the stale current fact;
  * the keyword producer carries exact, occurrence-aware source identity from
    the canonical H2 projection — never a guess from the extracted heading;
  * the two MCP transports expose the same graph field names.

Isolation: tmp_path config, tmp SQLite graph/journal databases, explicit
fixtures and a fake vector store. No network, no embedding model, no user data.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp import server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_entry_identity import markdown_section_entry_id
from layered_memory_mcp.graph_journal import (
    PROJECTION_L1,
    PROJECTION_VECTOR,
    MemoryEventJournal,
)
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    MemoryEvent,
    MemoryEventKind,
    NodeKind,
    NodeStatus,
)
from layered_memory_mcp.graph_runtime import GRAPH_SEED_ID_KEY
from layered_memory_mcp.graph_search import (
    GRAPH_EXCLUDED_LIFECYCLE,
    GRAPH_EXCLUDED_PENDING,
    keyword_section_seeds,
)
from layered_memory_mcp.graph_seed_metadata import graph_node_id_for_entry_id
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)
MIGRATED = Derivation(method=DerivationMethod.MIGRATED)

ACTIVE_ENTRY = markdown_section_entry_id("retrieval", "Index maintenance")
STALE_ENTRY = markdown_section_entry_id("retrieval", "Legacy rebuild")
ACTIVE_NODE = graph_node_id_for_entry_id(ACTIVE_ENTRY)
STALE_NODE = graph_node_id_for_entry_id(STALE_ENTRY)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _nodes() -> list[GraphNode]:
    return [
        GraphNode(
            id=ACTIVE_NODE,
            kind=NodeKind.CLAIM,
            label="index maintenance",
            content="The retrieval index is rebuilt nightly.",
            recorded_at=T0,
            derivation=MIGRATED,
        ),
        GraphNode(
            id=STALE_NODE,
            kind=NodeKind.CLAIM,
            label="legacy rebuild",
            content="The retrieval index is rebuilt weekly.",
            status=NodeStatus.SUPERSEDED,
            recorded_at=T0,
            derivation=MIGRATED,
        ),
    ]


def _populated_graph(tmp_path: Path) -> Path:
    parent = tmp_path / "graph-data"
    parent.mkdir(parents=True, exist_ok=True)
    database = parent / "graph.db"
    writer = SQLiteGraphStore(database, nodes=_nodes())
    writer.close()
    return database


def _config(tmp_path: Path, mode: str, *, database: Path | None = None,
            journal: Path | None = None) -> MemoryConfig:
    home = tmp_path / f"home-{mode}"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    (knowledge / "retrieval.md").write_text(
        "# Retrieval\n\n"
        "## Index maintenance\n\nThe retrieval index is rebuilt nightly.\n\n"
        "## Legacy rebuild\n\nThe retrieval index is rebuilt weekly.\n",
        encoding="utf-8",
    )
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode=mode,
        graph_db_path=str(database) if database is not None else None,
        graph_journal_path=str(journal) if journal is not None else None,
        auto_maintain=False,
    )


def _row(entry_id: str, node_id: str | None, section: str, text: str) -> dict:
    metadata = {"type": "fact", "confidence": 0.9, "tags": [], "section": section}
    if node_id is not None:
        metadata[GRAPH_SEED_ID_KEY] = node_id
    return {
        "id": entry_id,
        "domain": "retrieval",
        "text": text,
        "metadata": metadata,
        "score": 0.8,
    }


def _rows() -> list[dict]:
    return [
        _row(ACTIVE_ENTRY, ACTIVE_NODE, "Index maintenance", "rebuilt nightly"),
        _row(STALE_ENTRY, STALE_NODE, "Legacy rebuild", "rebuilt weekly"),
        _row("unmapped-row", None, "Glossary", "index glossary"),
    ]


class _FakeVectorStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def search(self, query, top_n=5, domain=None):
        return deepcopy(self.rows)


def _semantic(monkeypatch, config, rows=None) -> dict:
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", None)
    monkeypatch.setattr(
        server, "_v2_stores", {"vector": _FakeVectorStore(_rows() if rows is None else rows)}
    )
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    return json.loads(asyncio.run(server.search_semantic(query="index rebuild")))


def _recall(monkeypatch, config, keyword="rebuilt") -> dict:
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", None)
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    return json.loads(asyncio.run(server.recall_knowledge(keyword=keyword)))


# ---------------------------------------------------------------------------
# The keyword producer carries exact source identity
# ---------------------------------------------------------------------------

def test_the_keyword_producer_carries_exact_occurrence_aware_entry_ids(tmp_path):
    raw = (
        "# Notes\n\n"
        "## Alpha\n\nFirst alpha body about caching.\n\n"
        "## Alpha\n\nSecond alpha body about caching.\n\n"
        "## Beta\n\nA beta body about retrieval.\n"
    )

    beta = keyword_section_seeds("notes", raw, "retrieval")
    alpha = keyword_section_seeds("notes", raw, "caching")

    assert [item["entry_id"] for item in beta] == [
        markdown_section_entry_id("notes", "Beta", 0)
    ]
    assert [(item["section"], item["occurrence"]) for item in alpha] == [
        ("Alpha", 0),
        ("Alpha", 1),
    ]
    assert [item["entry_id"] for item in alpha] == [
        markdown_section_entry_id("notes", "Alpha", 0),
        markdown_section_entry_id("notes", "Alpha", 1),
    ]


def test_recall_results_carry_the_exact_section_entries_they_matched(
    monkeypatch, tmp_path
):
    config = _config(tmp_path, "legacy")
    response = _recall(monkeypatch, config, keyword="weekly")

    entries = response["results"][0]["section_entries"]
    assert [item["entry_id"] for item in entries] == [STALE_ENTRY]
    assert entries[0]["section"] == "Legacy rebuild"
    assert entries[0]["occurrence"] == 0
    assert "graph" not in response


# ---------------------------------------------------------------------------
# A healthy graph governs the PRIMARY results
# ---------------------------------------------------------------------------

def test_semantic_search_drops_a_superseded_row_from_the_primary_results(
    monkeypatch, tmp_path
):
    config = _config(tmp_path, "graph_recall", database=_populated_graph(tmp_path))

    response = _semantic(monkeypatch, config)

    ids = [row["id"] for row in response["results"]]
    assert STALE_ENTRY not in ids
    assert ACTIVE_ENTRY in ids
    # An unmapped row is preserved: the graph removes what it knows is stale,
    # never what it has never heard of.
    assert "unmapped-row" in ids
    assert response["graph"]["excluded_results"] == 1
    assert GRAPH_EXCLUDED_LIFECYCLE in response["graph"]["exclusion_reasons"]


def test_keyword_recall_drops_a_superseded_section_from_the_primary_results(
    monkeypatch, tmp_path
):
    config = _config(tmp_path, "graph_recall", database=_populated_graph(tmp_path))

    response = _recall(monkeypatch, config, keyword="weekly")

    assert response["results"] == []
    assert response["matched_files"] == 0
    assert response["graph"]["excluded_results"] == 1


def test_keyword_recall_keeps_a_file_whose_matched_section_is_current(
    monkeypatch, tmp_path
):
    config = _config(tmp_path, "graph_recall", database=_populated_graph(tmp_path))

    response = _recall(monkeypatch, config, keyword="nightly")

    assert [row["file"] for row in response["results"]] == ["retrieval.md"]
    assert response["graph"]["excluded_results"] == 0


def test_rest_knowledge_search_applies_the_same_filter(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import httpx

    from layered_memory_mcp.api import server as api

    config = _config(tmp_path, "graph_recall", database=_populated_graph(tmp_path))
    stores = {"vector": _FakeVectorStore(_rows()), "config": config}
    monkeypatch.setattr(api, "stores", stores)
    app = api.create_app()

    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/knowledge/search", params={"q": "index"})

    response = asyncio.run(_run())

    assert response.status_code == 200, response.text
    ids = [row["id"] for row in response.json()]
    assert STALE_ENTRY not in ids
    assert ACTIVE_ENTRY in ids
    assert "unmapped-row" in ids


def test_rest_knowledge_search_is_untouched_in_legacy_mode(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import httpx

    from layered_memory_mcp.api import server as api

    config = _config(tmp_path, "legacy", database=_populated_graph(tmp_path))
    monkeypatch.setattr(api, "stores", {"vector": _FakeVectorStore(_rows()), "config": config})
    app = api.create_app()

    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/knowledge/search", params={"q": "index"})

    response = asyncio.run(_run())

    assert [row["id"] for row in response.json()] == [
        ACTIVE_ENTRY,
        STALE_ENTRY,
        "unmapped-row",
    ]


# ---------------------------------------------------------------------------
# Fallbacks stay honest
# ---------------------------------------------------------------------------

def test_a_safe_unavailable_graph_preserves_the_legacy_payload(monkeypatch, tmp_path):
    config = _config(tmp_path, "graph_recall", database=None)

    response = _semantic(monkeypatch, config)

    assert [row["id"] for row in response["results"]] == [
        ACTIVE_ENTRY,
        STALE_ENTRY,
        "unmapped-row",
    ]
    assert response["graph"]["status"] == "graph_unavailable"
    assert response["graph"]["excluded_results"] == 0


def test_a_known_pending_projection_excludes_the_stale_current_fact(
    monkeypatch, tmp_path
):
    journal_path = tmp_path / "graph-data" / "journal.db"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = MemoryEventJournal(journal_path)
    try:
        journal.append(
            "request-pending",
            [
                MemoryEvent(
                    id="event_pending",
                    kind=MemoryEventKind.INJECT,
                    occurred_at=T0,
                    node=GraphNode(
                        id="claim_pending",
                        kind=NodeKind.CLAIM,
                        label="index maintenance",
                        content="The retrieval index is rebuilt every 15 minutes.",
                        recorded_at=T0,
                        derivation=MIGRATED,
                    ),
                    metadata={
                        "projection": {
                            "surface": "l1_markdown_section",
                            "domain": "retrieval",
                            "section": "Index maintenance",
                            "occurrence": 0,
                            "entry_id": ACTIVE_ENTRY,
                        }
                    },
                )
            ],
        )
        assert journal.checkpoint(PROJECTION_L1).position == 0
        assert journal.checkpoint(PROJECTION_VECTOR).position == 0
    finally:
        journal.close()

    # The graph is deliberately NOT configured: the unavailable-graph fallback
    # must still refuse to answer with the row the pending write replaces.
    config = _config(tmp_path, "graph_recall", database=None, journal=journal_path)

    response = _semantic(monkeypatch, config)

    ids = [row["id"] for row in response["results"]]
    assert ACTIVE_ENTRY not in ids
    assert STALE_ENTRY in ids and "unmapped-row" in ids
    assert response["graph"]["excluded_results"] == 1
    assert GRAPH_EXCLUDED_PENDING in response["graph"]["exclusion_reasons"]


def test_legacy_mode_still_adds_no_graph_key_and_changes_no_result(
    monkeypatch, tmp_path
):
    config = _config(tmp_path, "legacy", database=_populated_graph(tmp_path))

    response = _semantic(monkeypatch, config)

    assert "graph" not in response
    assert [row["id"] for row in response["results"]] == [
        ACTIVE_ENTRY,
        STALE_ENTRY,
        "unmapped-row",
    ]


def test_both_mcp_transports_use_the_same_graph_field_names(monkeypatch, tmp_path):
    database = _populated_graph(tmp_path)
    semantic = _semantic(monkeypatch, _config(tmp_path, "graph_recall", database=database))
    keyword = _recall(
        monkeypatch, _config(tmp_path, "graph_recall", database=database), keyword="nightly"
    )

    assert set(semantic["graph"]) == set(keyword["graph"])

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pytest

from layered_memory_mcp import server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_journal import MemoryEventJournal
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    NodeKind,
    NodeStatus,
)
from layered_memory_mcp.graph_write import SemanticWriteRequest, plan_semantic_write
from layered_memory_mcp.models import KnowledgeEntry, ReviewItem
from layered_memory_mcp.storage import ReviewQueue


T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)
EXPLICIT = Derivation(method=DerivationMethod.USER_EXPLICIT)


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    from layered_memory_mcp.storage import vector_store

    monkeypatch.setattr(
        vector_store,
        "_embed_texts",
        lambda texts: np.ones((len(texts), 512), dtype=np.float32) / np.sqrt(512),
    )


@pytest.fixture
def graph_config(tmp_path, monkeypatch) -> MemoryConfig:
    home = tmp_path / "home"
    knowledge = home / "knowledge"
    data = home / "data"
    knowledge.mkdir(parents=True)
    data.mkdir(parents=True)
    config = MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode="graph_write",
        graph_db_path=str(data / "graph.db"),
        graph_journal_path=str(data / "journal.db"),
        auto_maintain=False,
    )
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    monkeypatch.setattr(server, "_v2_stores", {})
    return config


@pytest.fixture
def legacy_config(tmp_path, monkeypatch) -> MemoryConfig:
    home = tmp_path / "legacy-home"
    knowledge = home / "knowledge"
    data = home / "data"
    knowledge.mkdir(parents=True)
    data.mkdir(parents=True)
    config = MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode="legacy",
        auto_maintain=False,
    )
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_v2_stores", {})
    return config


def _call(coro) -> dict:
    return json.loads(asyncio.run(coro))


def _pending_request() -> dict:
    return SemanticWriteRequest(
        node=GraphNode(
            id="review-pending-1",
            kind=NodeKind.CLAIM,
            label="review pending",
            content="This pending claim needs human review.",
            status=NodeStatus.PENDING,
            recorded_at=T0,
            derivation=EXPLICIT,
        ),
        occurred_at=T0,
        request_key="review-pending-1",
    ).to_dict()


def test_approve_knowledge_commits_reviewer_and_note_to_the_graph_journal(graph_config):
    request = SemanticWriteRequest.model_validate(_pending_request())
    events = plan_semantic_write(request).events
    with MemoryEventJournal(graph_config.graph_journal_path) as journal:
        journal.append("seed:review-pending-1", events)
    store = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        assert store.rebuild(events).ok
    finally:
        store.close()

    response = _call(
        server.approve_knowledge(
            "review-pending-1", reviewer="alice", note="Checked against source."
        )
    )

    assert response["success"] is True
    assert response["action"] == "approved"
    with MemoryEventJournal.open_readonly(graph_config.graph_journal_path) as journal:
        event = next(event for event in journal.read_events() if event.target_id == "review-pending-1")
    assert event.metadata["reviewer"] == "alice"
    assert event.metadata["note"] == "Checked against source."
    assert not (graph_config.home / "data" / "review_queue.db").exists()


def test_reject_knowledge_commits_reviewer_and_note_to_the_graph_journal(graph_config):
    request = SemanticWriteRequest.model_validate(_pending_request()).model_copy(
        update={"request_key": "review-pending-reject-1"}
    )
    request.node.id = "review-pending-reject-1"
    events = plan_semantic_write(request).events
    with MemoryEventJournal(graph_config.graph_journal_path) as journal:
        journal.append("seed:review-pending-reject-1", events)
    store = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        assert store.rebuild(events).ok
    finally:
        store.close()

    response = _call(
        server.reject_knowledge(
            "review-pending-reject-1", reviewer="bob", note="Source could not be verified."
        )
    )

    assert response["success"] is True
    assert response["action"] == "rejected"
    with MemoryEventJournal.open_readonly(graph_config.graph_journal_path) as journal:
        event = next(event for event in journal.read_events() if event.target_id == "review-pending-reject-1")
    assert event.metadata["reviewer"] == "bob"
    assert event.metadata["note"] == "Source could not be verified."
    reopened = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        assert reopened.get_node("review-pending-reject-1").status is NodeStatus.RETRACTED
    finally:
        reopened.close()
    assert not (graph_config.home / "data" / "review_queue.db").exists()


def test_repeating_the_same_approval_does_not_append_a_second_authority_event(graph_config):
    request = SemanticWriteRequest.model_validate(_pending_request())
    events = plan_semantic_write(request).events
    with MemoryEventJournal(graph_config.graph_journal_path) as journal:
        journal.append("seed:review-pending-1", events)
    store = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        assert store.rebuild(events).ok
    finally:
        store.close()

    first = _call(server.approve_knowledge("review-pending-1", reviewer="alice", note="Checked against source."))
    with MemoryEventJournal.open_readonly(graph_config.graph_journal_path) as journal:
        after_first = journal.read_events()
    second = _call(server.approve_knowledge("review-pending-1", reviewer="alice", note="Checked against source."))
    with MemoryEventJournal.open_readonly(graph_config.graph_journal_path) as journal:
        after_second = journal.read_events()

    assert first["success"] is True
    assert second["success"] is True
    assert second["graph"]["duplicate"] is True
    assert len(after_second) == len(after_first)
    assert [event.id for event in after_second] == [event.id for event in after_first]


def test_unknown_review_item_is_refused_without_side_effects(graph_config):
    request = SemanticWriteRequest.model_validate(_pending_request())
    events = plan_semantic_write(request).events
    with MemoryEventJournal(graph_config.graph_journal_path) as journal:
        journal.append("seed:review-pending-1", events)
        before = [event.to_json() for event in journal.read_events()]
    store = SQLiteGraphStore(graph_config.graph_db_path)
    try:
        assert store.rebuild(events).ok
    finally:
        store.close()

    response = _call(server.approve_knowledge("review-does-not-exist", reviewer="alice", note="Should not land."))

    assert response["success"] is False
    assert response["action"] == "approve"
    assert response["error"] == "graph_write_rejected_invalid_replay"
    assert response["graph"]["committed"] is False
    with MemoryEventJournal.open_readonly(graph_config.graph_journal_path) as journal:
        assert [event.to_json() for event in journal.read_events()] == before
    assert not (graph_config.home / "data" / "review_queue.db").exists()
    assert list(graph_config.knowledge_dir.glob("*.md")) == []
    assert not (graph_config.home / "data" / "vectors.db").exists()


def test_legacy_approve_knowledge_still_updates_the_review_queue(legacy_config):
    entry = KnowledgeEntry(id="legacy-review-1", domain="review", section="Pending claim", content="Legacy pending claim.")
    ReviewQueue(legacy_config.home / "data" / "review_queue.db").submit(ReviewItem(entry=entry))
    response = _call(server.approve_knowledge("legacy-review-1", reviewer="legacy-reviewer", note="Still valid."))
    assert response == {"success": True, "action": "approved", "id": "legacy-review-1"}
    with sqlite3.connect(legacy_config.home / "data" / "review_queue.db") as conn:
        status, reviewer, note = conn.execute("SELECT status, reviewed_by, review_note FROM review_queue WHERE id = ?", ("legacy-review-1",)).fetchone()
    assert (status, reviewer, note) == ("approved", "legacy-reviewer", "Still valid.")


def test_legacy_reject_knowledge_still_updates_the_review_queue(legacy_config):
    entry = KnowledgeEntry(id="legacy-review-reject-1", domain="review", section="Pending claim", content="Legacy pending claim.")
    ReviewQueue(legacy_config.home / "data" / "review_queue.db").submit(ReviewItem(entry=entry))
    response = _call(server.reject_knowledge("legacy-review-reject-1", reviewer="legacy-reviewer", note="Not supported."))
    assert response == {"success": True, "action": "rejected", "id": "legacy-review-reject-1"}
    with sqlite3.connect(legacy_config.home / "data" / "review_queue.db") as conn:
        status, reviewer, note = conn.execute("SELECT status, reviewed_by, review_note FROM review_queue WHERE id = ?", ("legacy-review-reject-1",)).fetchone()
    assert (status, reviewer, note) == ("rejected", "legacy-reviewer", "Not supported.")

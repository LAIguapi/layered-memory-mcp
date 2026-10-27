"""v3.6.0 Batch I-A — stable graph seed identity for the L1/vector projection.

Why this file exists: ARCHITECTURE_v2.md §12 Batch I scope item 2 requires "the
producer that maps a searchable L1/vector projection to a deterministic
``graph_node_id``", with missing/malformed/ambiguous mappings staying *visible*
degradation states rather than silently fabricated ones.

These tests pin:

  * **Identity, never content.** The seed ID is derived from the
    :class:`~layered_memory_mcp.models.KnowledgeEntry` identity through the
    importer's own :func:`~layered_memory_mcp.graph_legacy_importer.legacy_claim_id`
    — not from the body, the heading, the domain, the tags, the timestamps or a
    fresh UUID — so editing a section updates one claim instead of minting a new
    one, and two runs agree byte for byte.
  * **No fabrication.** A blank, multi-line or non-string identity produces *no*
    key and a named refusal, so the row degrades through the existing
    ``graph_metadata_without_ids`` / unmapped path instead of carrying an
    invented node ID.
  * **One rule, both write paths.** ``VectorStore.add`` and
    ``VectorStore.replace_entries`` write the same canonical key with the same
    value, and the legacy metadata fields beside it are preserved, not replaced.
  * **Directly consumable.** What ``VectorStore.search`` hands back feeds
    :func:`~layered_memory_mcp.graph_runtime.build_seed_mapping` unchanged.

Isolation: every vector store here lives under pytest's ``tmp_path`` with a
stubbed embedder. Nothing reads a real memory path, a real config or any user
memory data, and no graph store is opened.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from layered_memory_mcp.graph_legacy_importer import legacy_claim_id
from layered_memory_mcp.graph_runtime import GRAPH_SEED_ID_KEY, build_seed_mapping
from layered_memory_mcp.graph_seed_metadata import (
    SEED_IDENTITY_BLANK,
    SEED_IDENTITY_MULTILINE,
    SEED_IDENTITY_NOT_A_STRING,
    SeedIdentityError,
    entry_seed_rejection,
    graph_node_id_for_entry,
    graph_node_id_for_entry_id,
    merge_graph_seed_metadata,
)
from layered_memory_mcp.models import KnowledgeEntry, KnowledgeType
from layered_memory_mcp.storage import vector_store


def _entry(**kwargs) -> KnowledgeEntry:
    data = {
        "id": "entry-0001",
        "domain": "retrieval",
        "section": "Index maintenance",
        "content": "The retrieval index rebuild runs nightly.",
        "type": KnowledgeType.FACT,
        "confidence": 0.8,
        "tags": ["retrieval-index"],
    }
    data.update(kwargs)
    return KnowledgeEntry(**data)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A vector store with a stubbed embedder — no model, no network."""
    monkeypatch.setattr(
        vector_store,
        "_embed_texts",
        lambda texts: np.ones((len(texts), vector_store.EMBED_DIM), dtype=np.float32)
        / np.sqrt(vector_store.EMBED_DIM),
    )
    opened = vector_store.VectorStore(tmp_path / "vectors" / "vectors.db")
    yield opened
    opened.close()


def _rows(store) -> list[tuple]:
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute(
            "SELECT id, metadata FROM vectors ORDER BY id"
        ).fetchall()


def _metadata(store, entry_id: str) -> dict:
    return next(
        json.loads(row[1]) for row in _rows(store) if row[0] == entry_id
    )


# ---------------------------------------------------------------------------
# The pure producer
# ---------------------------------------------------------------------------

def test_the_seed_id_is_the_importers_legacy_claim_id():
    """Reused, not re-derived: one hash rule for one source identity."""
    assert graph_node_id_for_entry_id("entry-0001") == legacy_claim_id("entry-0001")


def test_the_same_identity_always_produces_the_same_id():
    first = graph_node_id_for_entry_id("entry-0001")
    second = graph_node_id_for_entry_id("entry-0001")
    assert first == second == graph_node_id_for_entry(_entry())


def test_editing_the_body_does_not_move_the_id():
    """An edit updates a claim; it must not orphan one and mint a replacement."""
    original = _entry(content="The rebuild runs nightly.")
    edited = _entry(
        content="The rebuild runs hourly.",
        section="Index maintenance (revised)",
        domain="retrieval-v2",
        tags=["other"],
        summary="changed",
        confidence=0.1,
        type=KnowledgeType.PREFERENCE,
    )
    assert graph_node_id_for_entry(original) == graph_node_id_for_entry(edited)


def test_two_identities_do_not_share_an_id():
    assert graph_node_id_for_entry_id("entry-0001") != graph_node_id_for_entry_id(
        "entry-0002"
    )


def test_a_padded_identity_normalises_to_the_same_id():
    assert graph_node_id_for_entry_id("  entry-0001  ") == graph_node_id_for_entry_id(
        "entry-0001"
    )


@pytest.mark.parametrize(
    "bad,reason",
    [
        ("", SEED_IDENTITY_BLANK),
        ("   ", SEED_IDENTITY_BLANK),
        ("entry\n0001", SEED_IDENTITY_MULTILINE),
        ("entry\r0001", SEED_IDENTITY_MULTILINE),
        (None, SEED_IDENTITY_NOT_A_STRING),
        (7, SEED_IDENTITY_NOT_A_STRING),
        (True, SEED_IDENTITY_NOT_A_STRING),
    ],
)
def test_an_unusable_identity_is_refused_by_name_not_fabricated(bad, reason):
    with pytest.raises(SeedIdentityError) as excinfo:
        graph_node_id_for_entry_id(bad)
    assert str(excinfo.value) == reason


def test_the_refusal_reason_is_also_readable_without_raising():
    assert entry_seed_rejection(_entry()) is None
    assert entry_seed_rejection(_entry(id="   ")) == SEED_IDENTITY_BLANK
    assert entry_seed_rejection(_entry(id="a\nb")) == SEED_IDENTITY_MULTILINE


# ---------------------------------------------------------------------------
# Compatible merge
# ---------------------------------------------------------------------------

def test_merge_adds_the_canonical_key_and_keeps_every_other_field():
    legacy = {"type": "fact", "confidence": 0.8, "tags": ["t"], "section": "S"}
    merged = merge_graph_seed_metadata(legacy, _entry())

    assert merged[GRAPH_SEED_ID_KEY] == legacy_claim_id("entry-0001")
    assert {key: merged[key] for key in legacy} == legacy
    # The input is never mutated: a caller's dict is its own.
    assert GRAPH_SEED_ID_KEY not in legacy


def test_merge_omits_the_key_entirely_for_an_unusable_identity():
    legacy = {"section": "S"}
    merged = merge_graph_seed_metadata(legacy, _entry(id="   "))

    assert GRAPH_SEED_ID_KEY not in merged
    assert merged == legacy


# ---------------------------------------------------------------------------
# Both vector write paths
# ---------------------------------------------------------------------------

def test_add_writes_the_canonical_seed_id(store):
    entry = _entry()
    store.add(entry)

    metadata = _metadata(store, entry.id)
    assert metadata[GRAPH_SEED_ID_KEY] == legacy_claim_id(entry.id)


def test_add_keeps_the_existing_metadata_fields(store):
    entry = _entry()
    store.add(entry)

    metadata = _metadata(store, entry.id)
    assert metadata["type"] == entry.type.value
    assert metadata["confidence"] == entry.confidence
    assert metadata["tags"] == entry.tags
    assert metadata["section"] == entry.section


def test_replace_entries_writes_the_same_mapping_as_add(store):
    entry = _entry()
    store.add(entry)
    from_add = _metadata(store, entry.id)

    store.replace_entries([entry], expected=store.snapshot())
    from_replace = _metadata(store, entry.id)

    assert from_replace[GRAPH_SEED_ID_KEY] == from_add[GRAPH_SEED_ID_KEY]
    assert from_replace == from_add


def test_a_body_edit_keeps_the_stored_seed_id(store):
    entry = _entry()
    store.add(entry)
    before = _metadata(store, entry.id)[GRAPH_SEED_ID_KEY]

    store.add(_entry(content="The rebuild runs hourly."))
    after = _metadata(store, entry.id)[GRAPH_SEED_ID_KEY]

    assert before == after


@pytest.mark.parametrize("bad_id", ["   ", "entry\n0001"])
def test_an_unusable_identity_writes_no_seed_id_through_either_path(store, bad_id):
    entry = _entry(id=bad_id)

    store.add(entry)
    assert GRAPH_SEED_ID_KEY not in _metadata(store, bad_id)

    store.replace_entries([entry], expected=store.snapshot())
    assert GRAPH_SEED_ID_KEY not in _metadata(store, bad_id)


def test_a_row_without_a_seed_id_stays_a_visible_unmapped_fallback(store):
    store.add(_entry())
    store.add(_entry(id="   ", section="Unmappable"))

    # The recall path keys a result by its own ``file`` identity, which is
    # independent of whether the row carries a graph node ID.
    results = []
    for row in _rows(store):
        metadata = json.loads(row[1])
        results.append(
            {"file": f"knowledge/retrieval#{metadata['section']}", "metadata": metadata}
        )
    mapping = build_seed_mapping(results)

    assert mapping.mapped_results == 1
    assert mapping.unmapped_results == 1
    assert "graph_metadata_without_ids:1" in mapping.warnings


# ---------------------------------------------------------------------------
# Search results feed the existing seam unchanged
# ---------------------------------------------------------------------------

def test_search_metadata_is_consumed_by_build_seed_mapping(store):
    entry = _entry()
    store.add(entry)

    results = store.search("index rebuild", top_n=5)
    assert results, "the stubbed embedder should match every row"

    mapping = build_seed_mapping(
        [{"file": result["id"], "metadata": result["metadata"]} for result in results]
    )
    assert mapping.seed_node_ids_by_result == {entry.id: [legacy_claim_id(entry.id)]}
    assert mapping.unmapped_results == 0
    assert mapping.rejected_ids == 0
    assert mapping.warnings == []


def test_search_still_returns_its_existing_shape(store):
    store.add(_entry())

    result = store.search("index rebuild", top_n=5)[0]
    assert set(result) == {"id", "domain", "text", "metadata", "score"}

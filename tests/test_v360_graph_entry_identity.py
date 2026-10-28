"""v3.6.0 Batch L-A — deterministic identity for the ordinary-Markdown projection.

Why this file exists: ARCHITECTURE_v2.md §12 Batch L-A. ``_vector_entries``
minted a random ``uuid4`` for every ordinary-Markdown H2 section on every
projection, so the same file projected twice produced different vector row IDs —
and, through ``graph_seed_metadata``, different graph seed IDs for the same
authored section. A graph seam that maps rows to nodes cannot be built on an
identity that changes when nothing changed.

These tests pin:

  * **Determinism without content.** The ID is a versioned SHA-256 of the exact
    domain, the exact H2 label and the zero-based occurrence among identical
    labels — and of nothing else. A body edit, an authored summary, a timestamp
    and a document-wide position cannot move it.
  * **No lossy normalization.** Case, punctuation, dates, versions and Unicode
    distinctions are retained. ``embedding_heading``/``heading_skeleton``
    deliberately erase exactly those distinctions, which is asserted here as the
    control for why identity does not use them.
  * **Refusals, not repairs.** A blank, whitespace-only, multi-line or non-string
    input is refused by name; nothing is trimmed into existence.
  * **The authoritative ID wins.** A frontmatter-backed entry keeps its own
    ``id``; only ordinary Markdown is derived.
  * **Explicit duplicate-heading limits.** Two identical labels are separated by
    position only, so edits *within* that duplicate group cannot track the
    authored object. Asserted as a limitation rather than hidden.
  * **One identity across the layers.** The ID a temporary vector replacement
    writes is the one ``graph_seed_metadata`` maps, the one the legacy importer
    mints for its Claim node, and the one a traversal expands from.

Isolation: every path is under pytest's ``tmp_path``, every config is explicit,
and the embedding function is replaced by a deterministic fake — no model is
loaded or downloaded. Nothing reads the real memory home, the real
``config.yaml``, MCP state or any user memory data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pytest

from layered_memory_mcp import injector
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_entry_identity import (
    PROJECTION_ID_PREFIX,
    PROJECTION_IDENTITY_BLANK,
    PROJECTION_IDENTITY_MULTILINE,
    PROJECTION_IDENTITY_NOT_A_STRING,
    PROJECTION_IDENTITY_OCCURRENCE_INVALID,
    PROJECTION_IDENTITY_VERSION,
    ProjectionIdentityError,
    markdown_section_entry_id,
    markdown_section_entry_ids,
    markdown_section_identity_payload,
)
from layered_memory_mcp.graph_legacy_importer import LegacyGraphImporter, legacy_claim_id
from layered_memory_mcp.graph_runtime import GRAPH_SEED_ID_KEY
from layered_memory_mcp.graph_seed_metadata import graph_node_id_for_entry_id
from layered_memory_mcp.heading import embedding_heading, heading_skeleton
from layered_memory_mcp.storage import vector_store

DOMAIN = "retrieval"
SECTION = "Index maintenance"


# ---------------------------------------------------------------------------
# The pure helper
# ---------------------------------------------------------------------------

def test_the_same_identity_always_produces_the_same_id():
    first = markdown_section_entry_id(DOMAIN, SECTION, 0)
    second = markdown_section_entry_id(DOMAIN, SECTION, 0)

    assert first == second
    assert first.startswith(f"{PROJECTION_ID_PREFIX}{PROJECTION_IDENTITY_VERSION}_")


def test_the_id_is_a_versioned_prefix_over_a_sha256_digest():
    entry_id = markdown_section_entry_id(DOMAIN, SECTION, 0)
    prefix, _, digest = entry_id.partition("_")

    assert prefix == f"{PROJECTION_ID_PREFIX}{PROJECTION_IDENTITY_VERSION}"
    assert digest
    assert all(char in "0123456789abcdef" for char in digest)
    # The digest is the hash of the canonical payload and nothing else.
    import hashlib

    payload = markdown_section_identity_payload(DOMAIN, SECTION, 0)
    assert hashlib.sha256(payload.encode("utf-8")).hexdigest().startswith(digest)


def test_each_identity_component_changes_the_id():
    base = markdown_section_entry_id(DOMAIN, SECTION, 0)

    assert markdown_section_entry_id("indexing", SECTION, 0) != base
    assert markdown_section_entry_id(DOMAIN, "Index rebuild", 0) != base
    assert markdown_section_entry_id(DOMAIN, SECTION, 1) != base


def test_the_canonical_payload_cannot_be_confused_across_fields():
    """A separator inside a label must not be readable as a field boundary."""
    left = markdown_section_entry_id("retrieval\x1fIndex", "maintenance", 0)
    right = markdown_section_entry_id("retrieval", "Index\x1fmaintenance", 0)

    assert left != right
    # Same for a label that merely *contains* the next field's text.
    assert (
        markdown_section_entry_id("a", "bc", 0)
        != markdown_section_entry_id("ab", "c", 0)
    )


def test_identity_preserves_case_punctuation_dates_versions_and_unicode():
    distinct = {
        markdown_section_entry_id(DOMAIN, "Release notes", 0),
        markdown_section_entry_id(DOMAIN, "release notes", 0),
        markdown_section_entry_id(DOMAIN, "Release notes!", 0),
        markdown_section_entry_id(DOMAIN, "Release notes v1.2", 0),
        markdown_section_entry_id(DOMAIN, "Release notes v1.3", 0),
        markdown_section_entry_id(DOMAIN, "AI项目横评 2026-09-22 期", 0),
        markdown_section_entry_id(DOMAIN, "AI项目横评 2026-10-03 期", 0),
    }

    assert len(distinct) == 7


def test_the_noise_stripping_helpers_are_deliberately_not_the_identity_rule():
    """The control: these two erase exactly what identity must retain."""
    first = "AI项目横评 2026-09-22 期"
    second = "AI项目横评 2026-10-03 期"

    assert heading_skeleton(first) == heading_skeleton(second)
    assert embedding_heading(first) == embedding_heading(second)
    # ...and the version qualifier the identity keeps is dropped by both too.
    assert heading_skeleton("Release notes v1.2") == heading_skeleton(
        "Release notes v1.3"
    )
    assert embedding_heading("Release notes v1.2") == embedding_heading(
        "Release notes v1.3"
    )
    assert markdown_section_entry_id(DOMAIN, first, 0) != markdown_section_entry_id(
        DOMAIN, second, 0
    )


@pytest.mark.parametrize(
    "domain, section, reason",
    [
        ("", SECTION, PROJECTION_IDENTITY_BLANK),
        ("   ", SECTION, PROJECTION_IDENTITY_BLANK),
        (DOMAIN, "", PROJECTION_IDENTITY_BLANK),
        (DOMAIN, " \t ", PROJECTION_IDENTITY_BLANK),
        ("retrieval\nindex", SECTION, PROJECTION_IDENTITY_MULTILINE),
        (DOMAIN, "Index\nmaintenance", PROJECTION_IDENTITY_MULTILINE),
        (DOMAIN, "Index\rmaintenance", PROJECTION_IDENTITY_MULTILINE),
        (None, SECTION, PROJECTION_IDENTITY_NOT_A_STRING),
        (DOMAIN, 7, PROJECTION_IDENTITY_NOT_A_STRING),
        (True, SECTION, PROJECTION_IDENTITY_NOT_A_STRING),
    ],
)
def test_unusable_labels_are_refused_by_name(domain, section, reason):
    with pytest.raises(ProjectionIdentityError) as excinfo:
        markdown_section_entry_id(domain, section, 0)

    assert str(excinfo.value).startswith(reason)


@pytest.mark.parametrize("occurrence", [-1, 1.0, "0", True, None])
def test_an_unusable_occurrence_is_refused(occurrence):
    with pytest.raises(ProjectionIdentityError) as excinfo:
        markdown_section_entry_id(DOMAIN, SECTION, occurrence)

    assert str(excinfo.value).startswith(PROJECTION_IDENTITY_OCCURRENCE_INVALID)


def test_a_refusal_never_quotes_the_value_it_refused():
    with pytest.raises(ProjectionIdentityError) as excinfo:
        markdown_section_entry_id(DOMAIN, "secret\nheading", 0)

    assert "secret" not in str(excinfo.value)


def test_occurrence_is_counted_per_label_not_per_document():
    ids = markdown_section_entry_ids(
        DOMAIN, ["Alpha", "Beta", "Alpha", "Gamma", "Alpha"]
    )

    assert ids[0] == markdown_section_entry_id(DOMAIN, "Alpha", 0)
    assert ids[1] == markdown_section_entry_id(DOMAIN, "Beta", 0)
    assert ids[2] == markdown_section_entry_id(DOMAIN, "Alpha", 1)
    assert ids[3] == markdown_section_entry_id(DOMAIN, "Gamma", 0)
    assert ids[4] == markdown_section_entry_id(DOMAIN, "Alpha", 2)
    assert len(set(ids)) == 5


# ---------------------------------------------------------------------------
# The projection: _vector_entries
# ---------------------------------------------------------------------------

ONE_SECTION = "# Retrieval\n\n## Index maintenance\n\nThe index is rebuilt nightly.\n"


def _ids(domain: str, raw: str) -> list[str]:
    return [entry.id for entry in injector._vector_entries(domain, raw)]


def test_repeated_projection_of_the_same_file_keeps_every_id():
    assert _ids(DOMAIN, ONE_SECTION) == _ids(DOMAIN, ONE_SECTION)


def test_a_body_edit_keeps_the_section_id():
    edited = ONE_SECTION.replace("nightly", "hourly, with a verification pass")

    assert _ids(DOMAIN, edited) == _ids(DOMAIN, ONE_SECTION)
    # ...and the body really did change, so this is not a no-op comparison.
    entry = injector._vector_entries(DOMAIN, edited)[0]
    assert "verification pass" in entry.content


def test_inserting_and_reordering_other_sections_keeps_the_id():
    before = _ids(DOMAIN, ONE_SECTION)[0]

    inserted = (
        "# Retrieval\n\n## Query planning\n\nPlans are cached.\n\n"
        "## Index maintenance\n\nThe index is rebuilt nightly.\n\n"
        "## Shard layout\n\nFour shards.\n"
    )
    reordered = (
        "# Retrieval\n\n## Shard layout\n\nFour shards.\n\n"
        "## Index maintenance\n\nThe index is rebuilt nightly.\n\n"
        "## Query planning\n\nPlans are cached.\n"
    )

    assert before in _ids(DOMAIN, inserted)
    assert before in _ids(DOMAIN, reordered)
    assert len(set(_ids(DOMAIN, inserted))) == 3


def test_a_heading_rename_or_a_domain_rename_changes_identity():
    renamed_heading = ONE_SECTION.replace("## Index maintenance", "## Index rebuild")

    assert _ids(DOMAIN, renamed_heading) != _ids(DOMAIN, ONE_SECTION)
    assert _ids("indexing", ONE_SECTION) != _ids(DOMAIN, ONE_SECTION)


def test_two_sections_with_the_same_heading_get_distinct_stable_ids():
    duplicated = (
        "# Retrieval\n\n## Index maintenance\n\nFirst note.\n\n"
        "## Index maintenance\n\nSecond note.\n"
    )
    ids = _ids(DOMAIN, duplicated)

    assert len(set(ids)) == 2
    assert ids == _ids(DOMAIN, duplicated)
    assert ids[0] == _ids(DOMAIN, ONE_SECTION)[0]


def test_the_duplicate_heading_group_is_positional_and_says_so():
    """The explicit Batch L-A limitation, asserted rather than implied.

    Deleting the first of two identical headings hands its ID to the one that
    survives: positional occurrence cannot tell which authored object stayed.
    This is a bounded limitation of identical labels only — it is not semantic
    entity resolution, and nothing infers identity from the body.
    """
    both = (
        "# Retrieval\n\n## Index maintenance\n\nFirst note.\n\n"
        "## Index maintenance\n\nSecond note.\n"
    )
    second_only = "# Retrieval\n\n## Index maintenance\n\nSecond note.\n"

    ids_both = _ids(DOMAIN, both)
    ids_after = _ids(DOMAIN, second_only)

    assert ids_after == [ids_both[0]]
    # The surviving section's own body is unchanged, and it still took the
    # first slot's identity: the limitation, named.
    assert injector._vector_entries(DOMAIN, second_only)[0].content == "Second note."
    # A differently *named* section would have been tracked correctly instead.
    renamed = "# Retrieval\n\n## Index maintenance\n\nFirst note.\n"
    assert _ids(DOMAIN, renamed) == [ids_both[0]]


def test_identity_ignores_the_authored_summary_and_the_timestamps():
    entries = injector._vector_entries(DOMAIN, ONE_SECTION)
    entry = entries[0]

    assert entry.id == markdown_section_entry_id(DOMAIN, SECTION, 0)
    assert entry.summary == embedding_heading(SECTION)[:200]
    assert entry.created_at != entry.id  # sanity: identity is not a timestamp


def test_a_frontmatter_backed_entry_keeps_its_authoritative_id():
    raw = (
        "---\n"
        "id: entry-0001\n"
        "domain: retrieval\n"
        "section: Index maintenance\n"
        "type: fact\n"
        "---\n\n"
        "The index is rebuilt nightly.\n"
    )
    entries = injector._vector_entries(DOMAIN, raw)

    assert [entry.id for entry in entries] == ["entry-0001"]
    assert entries[0].id != markdown_section_entry_id(DOMAIN, SECTION, 0)


def test_an_unusable_domain_is_refused_rather_than_given_a_random_identity():
    with pytest.raises(ProjectionIdentityError):
        injector._vector_entries("   ", ONE_SECTION)


# ---------------------------------------------------------------------------
# Agreement across the layers: vector replacement, seed metadata, importer,
# traversal
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """An explicit tmp config with a deterministic fake embedding function."""
    home = tmp_path / "identity-home"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True)
    monkeypatch.setattr(
        vector_store,
        "_embed_texts",
        lambda texts: np.ones((len(texts), vector_store.EMBED_DIM), dtype=np.float32)
        / np.sqrt(vector_store.EMBED_DIM),
    )
    return MemoryConfig(
        home=str(home), knowledge_dir=str(knowledge), auto_maintain=False
    )


def _rows(cfg) -> list[tuple]:
    db = cfg.home / "data" / "vectors.db"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT id, domain, text, metadata FROM vectors ORDER BY id"
        ).fetchall()


def _replace(cfg, raw: str, domain: str = DOMAIN) -> dict:
    path = cfg.knowledge_dir / f"{domain}.md"
    path.write_text(raw, encoding="utf-8")
    result = injector.sync_to_vector_store(
        cfg.home / "data", domain, raw, replace_domain=True, source_path=path
    )
    assert result["success"], result
    return result


def test_a_temporary_vector_replacement_keeps_the_same_row_ids(cfg):
    _replace(cfg, ONE_SECTION)
    first = _rows(cfg)
    _replace(cfg, ONE_SECTION)
    second = _rows(cfg)

    assert [row[0] for row in first] == [row[0] for row in second]
    assert first == second
    assert [row[0] for row in first] == [markdown_section_entry_id(DOMAIN, SECTION, 0)]


def test_a_body_edit_updates_the_row_instead_of_minting_a_new_one(cfg):
    _replace(cfg, ONE_SECTION)
    before = _rows(cfg)
    _replace(cfg, ONE_SECTION.replace("nightly", "hourly"))
    after = _rows(cfg)

    assert [row[0] for row in before] == [row[0] for row in after]
    assert before[0][2] != after[0][2]  # the embedded text did change


def test_the_standalone_sync_mode_is_unchanged(cfg):
    """``replace_domain=False`` still mints its own ``uuid4``; L-A touched it not."""
    first = injector.sync_to_vector_store(
        cfg.home / "data", DOMAIN, "A standalone note.", summary="note"
    )
    second = injector.sync_to_vector_store(
        cfg.home / "data", DOMAIN, "A standalone note.", summary="note"
    )

    assert first == {"success": True, "action": "vector_synced", "domain": DOMAIN}
    assert second["success"] is True
    # Still the pre-L-A shape: a random identity, and the store's own verbatim
    # (domain, text) dedup guard keeping the second call a no-op.
    ids = [row[0] for row in _rows(cfg)]
    assert len(ids) == 1
    assert not ids[0].startswith(PROJECTION_ID_PREFIX)
    assert len(ids[0]) == 36 and ids[0].count("-") == 4


def test_the_stored_row_carries_the_canonical_graph_seed_id(cfg):
    _replace(cfg, ONE_SECTION)
    row_id, _domain, _text, metadata = _rows(cfg)[0]
    seed_id = json.loads(metadata)[GRAPH_SEED_ID_KEY]

    assert row_id == markdown_section_entry_id(DOMAIN, SECTION, 0)
    assert seed_id == graph_node_id_for_entry_id(row_id)
    assert seed_id == legacy_claim_id(row_id)


def test_the_importer_the_seed_metadata_and_a_traversal_agree_on_one_id(cfg):
    """One identity end to end: projection → vector row → importer → traversal."""
    from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
    from layered_memory_mcp.graph_runtime import (
        GraphModeDecision,
        run_graph_assisted_recall,
    )

    _replace(cfg, ONE_SECTION)
    row_id, _domain, _text, metadata = _rows(cfg)[0]
    seed_id = json.loads(metadata)[GRAPH_SEED_ID_KEY]

    # The importer projects the *same* entries the vector replacement wrote.
    entries = injector._vector_entries(DOMAIN, ONE_SECTION)
    for entry in entries:
        entry.created_at = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
        entry.updated_at = entry.created_at
    store = InMemoryGraphStore()
    report = LegacyGraphImporter(store).import_snapshot(entries)

    assert report.nodes_written >= 1
    assert store.get_node(seed_id) is not None

    # ...and the recall seam expands the row from exactly that metadata.
    seeds = [{"file": row_id, "score": 1.0, "metadata": {GRAPH_SEED_ID_KEY: seed_id}}]
    runtime = run_graph_assisted_recall(
        seeds,
        store,
        mode=GraphModeDecision(mode="graph_recall", recall_enabled=True),
    )

    assert runtime.graph_assisted is True
    assert runtime.seed_mapping.mapped_results == 1
    assert seed_id in runtime.graph_results[0].graph_node_ids


def test_a_reindex_and_a_domain_replacement_produce_the_same_ids(cfg):
    _replace(cfg, ONE_SECTION)
    replaced = [row[0] for row in _rows(cfg)]

    result = injector.reindex_vector_store(cfg, calibrate=False)
    assert result["success"], result

    assert [row[0] for row in _rows(cfg)] == replaced


def test_a_stable_projection_makes_the_reindex_check_quiet(cfg):
    _replace(cfg, ONE_SECTION)

    assert injector.vector_store_needs_reindex(cfg) is False

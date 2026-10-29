"""v3.6.1 — the `graph_migration` operator tool, end to end.

Why this file exists: ARCHITECTURE_v2.md's migration delivery contract is a set
of refusals, not a feature. Each test below pins one of them:

  * a dry run reads a source and a destination and changes neither — not their
    contents, not their membership, not their modification times;
  * an apply builds a *private* staged generation, verifies it, revalidates the
    frozen source fingerprint and only then publishes it across one filesystem
    boundary; the source stays byte-for-byte intact and is backed up;
  * every vector row is accounted for in exactly one stable category, and an
    unresolved legacy identity blocks publication instead of being guessed from
    a body, a heading or a file name;
  * a journal replay reproduces the published graph exactly, and the readable
    L1/L0/vector surfaces line up with it;
  * an identical rerun writes nothing at all, a changed source or a changed
    destination is refused, and verify/rollback detect tampering;
  * a publication failure leaves no visible partial generation, and its staged
    debris is accounted for rather than silently reused.

Isolation: every path is an explicit ``tmp_path`` fixture. No real memory home,
no real config file, no default discovery, no network, no model download — the
embedding function is replaced by a deterministic hash-based one, and the
fixture vector database is built through the real ``VectorStore`` write path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp import graph_migration
from layered_memory_mcp import graph_migration_generation as generation_module
from layered_memory_mcp.graph_entry_identity import markdown_section_entry_id
from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
from layered_memory_mcp.graph_journal import (
    PROJECTION_NAMES,
    REQUEST_KIND_BOOTSTRAP,
    MemoryEventJournal,
)
from layered_memory_mcp.graph_legacy_importer import legacy_claim_id
from layered_memory_mcp.graph_runtime import GRAPH_SEED_ID_KEY
from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore
from layered_memory_mcp.memory_changelog import MemoryChangelogEntry
from layered_memory_mcp.models import KnowledgeEntry

# ---------------------------------------------------------------------------
# Neutral technical fixture content
# ---------------------------------------------------------------------------

RETRIEVAL_MD = """\
## Index maintenance

The retrieval index is rebuilt nightly by the scheduler.

## Shard layout

Each shard holds one month of documents.
"""

PIPELINE_MD = """\
---
id: entry-pipeline-0001
domain: pipeline
section: Ingest pipeline
type: fact
summary: Documents enter through the batch loader.
confidence: 0.8
version: 2
tags:
  - pipeline
  - ingest
review_status: approved
created_at: '2026-03-01T09:00:00+00:00'
updated_at: '2026-03-01T09:00:00+00:00'
---

## Ingest pipeline

Documents enter through the batch loader.
"""

FRONTMATTER_ENTRY_ID = "entry-pipeline-0001"

T0 = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


def _section_id(domain: str, section: str, occurrence: int = 0) -> str:
    return markdown_section_entry_id(domain, section, occurrence)


RETRIEVAL_IDS = {
    "Index maintenance": _section_id("retrieval", "Index maintenance"),
    "Shard layout": _section_id("retrieval", "Shard layout"),
}

#: The two documents the projection producer and the canonical readable-surface
#: reader read *differently*, in the exact shapes that were probed. Deliberately
#: not paraphrased into prettier prose: in each one the whole point is a
#: structural detail — an empty first occurrence of a duplicate H2 label, and a
#: tab-separated heading — that a rewrite would quietly remove.
DUPLICATE_LABEL_EMPTY_FIRST_MD = "## Same\n\n## Same\n\nkept\n"
TAB_SEPARATED_HEADING_MD = "## First\n\nbody\n\n##\tSecond\n\nsecond\n"

#: Compatible, and deliberately not trivially so: a bodyless trailing section
#: is invisible to the projection producer and *empty* to the canonical reader,
#: which is the two readers agreeing rather than disagreeing. Here so the
#: preflight is pinned as a comparison and not as "refuse anything unusual".
TRAILING_EMPTY_SECTION_MD = RETRIEVAL_MD + "\n## Replication planned\n"


# ---------------------------------------------------------------------------
# Deterministic embeddings — never a model download (§15, §12 Batch I)
# ---------------------------------------------------------------------------

def _fake_embed(texts):
    out = np.zeros((len(texts), 512), dtype=np.float32)
    for index, text in enumerate(texts):
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = np.frombuffer(digest * 16, dtype=np.uint8)[:512].astype(np.float32)
        out[index] = raw / (float(np.linalg.norm(raw)) or 1.0)
    return out


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    from layered_memory_mcp.storage import vector_store

    monkeypatch.setattr(vector_store, "_embed_texts", _fake_embed)


@pytest.fixture(autouse=True)
def no_publish_hook():
    """Keep the fault-injection seam off unless a test asks for it."""
    graph_migration.BEFORE_PUBLISH_HOOK = None
    yield
    graph_migration.BEFORE_PUBLISH_HOOK = None


# ---------------------------------------------------------------------------
# Fixture builders — a genuine old-format snapshot, written explicitly
# ---------------------------------------------------------------------------

def _entries_for(domain: str, raw: str) -> list[KnowledgeEntry]:
    """The real projection producer, so fixture identities are not restated."""
    from layered_memory_mcp.injector import _vector_entries

    return list(_vector_entries(domain, raw))


def _write_vector_db(
    path: Path,
    entries: list[KnowledgeEntry],
    *,
    keep_graph_ids: frozenset[str] = frozenset(),
) -> None:
    """Build a legacy vectors.db through the real store write path.

    ``entry_metadata`` has stamped ``graph_node_id`` on new rows since Batch
    I-A, so an *old* row is produced by publishing and then removing that key
    through the store's own metadata-only boundary. Rows named in
    ``keep_graph_ids`` keep theirs, which is how one fixture covers both the
    "already mapped" and the "missing metadata" classifications.
    """
    from layered_memory_mcp.storage.vector_store import VectorStore

    path.parent.mkdir(parents=True, exist_ok=True)
    with VectorStore(path) as store:
        store.replace_entries(entries, domains=None, expected=store.snapshot())
        rows = store.snapshot()
        updates = {}
        for row in rows:
            if row[0] in keep_graph_ids:
                continue
            metadata = json.loads(row[4]) if row[4] else {}
            metadata.pop(GRAPH_SEED_ID_KEY, None)
            updates[row[0]] = metadata
        if updates:
            store.patch_metadata(updates, expected=rows)


def _changelog_line(**kwargs) -> str:
    event = MemoryChangelogEntry(
        event_id=kwargs.pop("event_id"),
        at=kwargs.pop("at", T0),
        surface="l1_knowledge",
        operation="l1_section_updated",
        **kwargs,
    )
    return json.dumps(event.to_dict(), sort_keys=True)


def build_source(root: Path, *, keep_graph_ids: frozenset[str] = frozenset()) -> Path:
    """A complete old-format snapshot: knowledge/, data/vectors.db, changelog."""
    knowledge = root / "knowledge"
    data = root / "data"
    knowledge.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    (knowledge / "retrieval.md").write_text(RETRIEVAL_MD, encoding="utf-8")
    (knowledge / "pipeline.md").write_text(PIPELINE_MD, encoding="utf-8")

    entries = _entries_for("retrieval", RETRIEVAL_MD) + _entries_for(
        "pipeline", PIPELINE_MD
    )
    _write_vector_db(data / "vectors.db", entries, keep_graph_ids=keep_graph_ids)

    (data / "memory_changelog.jsonl").write_text(
        "\n".join(
            [
                _changelog_line(
                    event_id="evt0001",
                    domain="retrieval",
                    section="Index maintenance",
                    tool="inject_knowledge",
                    before_hash="a" * 8,
                    after_hash="b" * 8,
                ),
                _changelog_line(
                    event_id="evt0002",
                    domain="pipeline",
                    section="Ingest pipeline",
                    tool="update_knowledge_file",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def build_source_with_document(root: Path, raw: str) -> Path:
    """A snapshot whose one ordinary document is ``raw``, rows built from it.

    The legacy database is written from the *same* text, so nothing about the
    resulting report is explained by rows a fixture orphaned by editing a file
    after building the store: whatever blocks is the document itself.
    """
    knowledge = root / "knowledge"
    data = root / "data"
    knowledge.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    (knowledge / "retrieval.md").write_text(raw, encoding="utf-8")
    (knowledge / "pipeline.md").write_text(PIPELINE_MD, encoding="utf-8")

    entries = _entries_for("retrieval", raw) + _entries_for("pipeline", PIPELINE_MD)
    _write_vector_db(data / "vectors.db", entries)
    return root


@pytest.fixture
def source(tmp_path) -> Path:
    return build_source(tmp_path / "old-store")


@pytest.fixture
def destination(tmp_path) -> Path:
    return tmp_path / "migrated"


# ---------------------------------------------------------------------------
# Tree snapshots — content, membership and modification times
#
# Including the snapshotted directory's **own** modification time, which is the
# one thing these baselines used to miss: a file created and removed inside a
# directory leaves the directory's contents identical and its own ``mtime``
# changed, so a snapshot that only described the children could not see a
# transient lock, marker or temporary file at the top level at all.
# ---------------------------------------------------------------------------

#: The key :func:`tree_state` records the snapshotted directory itself under.
#: Named rather than spelled out, because the few places that must compare
#: everything *except* this one entry have to say so explicitly.
ROOT_KEY = "./"


def tree_state(root: Path) -> dict[str, tuple]:
    """Every path under ``root`` — and ``root`` itself — with kind, size, mtime
    and content hash."""
    if not root.exists():
        return {}
    state: dict[str, tuple] = {ROOT_KEY: ("dir", root.lstat().st_mtime_ns)}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        stat = path.lstat()
        if path.is_symlink():
            state[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            state[rel + "/"] = ("dir", stat.st_mtime_ns)
        else:
            state[rel] = (
                "file",
                stat.st_size,
                stat.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return state


def changed_paths(before: dict[str, tuple], after: dict[str, tuple]) -> set[str]:
    """Every snapshot key that differs between two :func:`tree_state` calls.

    Used instead of a bare equality where the expected difference has to be
    *named*: an applying operation serialises itself with a lock file in the
    directory it is about to write in, and creating and removing that file
    changes that directory's own ``mtime`` even when the operation goes on to
    write nothing at all. A test for such an operation asserts that the set of
    differences is at most :data:`ROOT_KEY` — every member, every
    subdirectory's own ``mtime`` and every byte identical — and then asserts
    separately that no lock or temporary file was left behind. Nothing here
    excuses a changed *file*, an appeared member or a touched subdirectory.
    """
    return {key for key in {*before, *after} if before.get(key) != after.get(key)}


def vector_rows(path: Path) -> list[tuple]:
    """Read a vectors.db read-only, exactly as the tool must."""
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return connection.execute(
            "SELECT id, domain, text, vector, metadata FROM vectors ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def row_metadata(path: Path) -> dict[str, dict]:
    return {
        row[0]: (json.loads(row[4]) if row[4] else {}) for row in vector_rows(path)
    }


def generation(destination: Path) -> Path:
    return destination / "current"


def manifest_of(destination: Path) -> dict:
    return json.loads(
        (generation(destination) / "manifest.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Dry run: reads everything, changes nothing
# ---------------------------------------------------------------------------

def test_dry_run_changes_no_source_byte_membership_or_mtime(source, destination):
    before = tree_state(source)

    report = graph_migration.plan_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_PLANNED
    assert report["dry_run"] is True
    assert report["exit_code"] == graph_migration.EXIT_OK
    assert tree_state(source) == before
    # Not even the destination directory is brought into existence.
    assert not destination.exists()
    assert report["inventory"]["destination_prestate"] == "absent"


def test_dry_run_echoes_the_resolved_explicit_inputs(source, destination):
    report = graph_migration.plan_migration(source=source, destination=destination)

    assert report["source"] == str(source.resolve())
    assert report["destination"] == str(destination.resolve())
    assert report["command"] == "plan"
    # Bodies never travel in a report.
    serialized = json.dumps(report)
    assert "rebuilt nightly" not in serialized
    assert "batch loader" not in serialized


def test_plan_bootstrap_is_stable_for_the_same_source_snapshot(source, tmp_path):
    first = graph_migration.plan_migration(
        source=source, destination=tmp_path / "first"
    )
    second = graph_migration.plan_migration(
        source=source, destination=tmp_path / "second"
    )

    assert first["bootstrap"] == second["bootstrap"]
    assert first["bootstrap"]["recorded_at"] == (
        "1970-01-01T00:00:00+00:00"
    )


def test_dry_run_leaves_an_existing_destination_untouched(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    before = tree_state(destination)

    report = graph_migration.plan_migration(source=source, destination=destination)

    assert tree_state(destination) == before
    assert report["dry_run"] is True


def test_plan_accounts_for_every_vector_row_exactly_once(source, destination):
    keep = frozenset({RETRIEVAL_IDS["Shard layout"]})
    build_source(source, keep_graph_ids=keep)

    inventory = graph_migration.plan_migration(
        source=source, destination=destination
    )["inventory"]

    counts = inventory["counts_by_category"]
    assert inventory["vector_rows"] == 3
    assert sum(counts.values()) == 3
    assert inventory["counts_balance"] is True
    assert counts["mapped"] == 2
    assert counts["existing"] == 1
    assert len(inventory["classifications"]) == 3


def test_plan_inventories_frontmatter_and_versioned_section_identities(
    source, destination
):
    inventory = graph_migration.plan_migration(
        source=source, destination=destination
    )["inventory"]

    identities = {row["row_id"] for row in inventory["classifications"]}
    assert identities == {
        FRONTMATTER_ENTRY_ID,
        RETRIEVAL_IDS["Index maintenance"],
        RETRIEVAL_IDS["Shard layout"],
    }
    assert inventory["frontmatter_entries"] == 1
    assert inventory["section_entries"] == 2
    assert inventory["changelog_events"] == 2


# ---------------------------------------------------------------------------
# Apply: a verified staged generation, published once
# ---------------------------------------------------------------------------

def test_apply_publishes_a_verified_generation(source, destination):
    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_APPLIED
    assert report["exit_code"] == graph_migration.EXIT_OK
    assert report["verification"]["ok"] is True
    root = generation(destination)
    assert (root / "journal.db").is_file()
    assert (root / "graph.db").is_file()
    assert (root / "knowledge" / "retrieval.md").is_file()
    assert (root / "data" / "vectors.db").is_file()
    assert (root / "manifest.json").is_file()
    assert (root / "backup" / "knowledge" / "retrieval.md").is_file()
    # No private staging directory survives a successful publication.
    assert [p.name for p in destination.iterdir()] == ["current"]

    # Every verification claim is made, not merely the summary one: a quietly
    # false sub-check would otherwise hide behind an aggregate.
    verification = report["verification"]
    for claim in (
        "manifest_ok",
        "membership_ok",
        "hashes_ok",
        "journal_ok",
        "graph_ok",
        "replay_equal",
        "surfaces_ok",
        "source_files_ok",
        "vector_ok",
        "counts_ok",
        "generation_intact",
    ):
        assert verification[claim] is True, claim
    assert verification["source_drift"] is False
    assert verification["artifacts_checked"] >= 8

    # And the generation is substantive: three claims, three evidence nodes and
    # the two entities the legacy tags project, plus one event each.
    assert report["inventory"]["vector_rows"] == 3
    assert report["bootstrap"]["nodes"] >= 8
    assert report["bootstrap"]["events"] == (
        report["bootstrap"]["nodes"] + report["bootstrap"]["edges"]
    )
    assert report["bootstrap"]["bound_surfaces"] == 2
    assert verification["counts"]["graph_nodes"] == report["bootstrap"]["nodes"]


def test_apply_leaves_the_source_byte_for_byte_intact(source, destination):
    before = tree_state(source)

    graph_migration.apply_migration(source=source, destination=destination)

    assert tree_state(source) == before


def test_apply_retains_the_source_bytes_as_a_backup(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)

    backup = generation(destination) / "backup"
    assert (backup / "knowledge" / "retrieval.md").read_text(
        encoding="utf-8"
    ) == RETRIEVAL_MD
    assert (backup / "knowledge" / "pipeline.md").read_text(
        encoding="utf-8"
    ) == PIPELINE_MD
    assert (backup / "data" / "vectors.db").read_bytes() == (
        source / "data" / "vectors.db"
    ).read_bytes()
    assert (backup / "data" / "memory_changelog.jsonl").read_bytes() == (
        source / "data" / "memory_changelog.jsonl"
    ).read_bytes()


def test_apply_persists_a_complete_bootstrap_journal(source, destination):
    report = graph_migration.apply_migration(source=source, destination=destination)

    with MemoryEventJournal.open_readonly(
        generation(destination) / "journal.db"
    ) as journal:
        assert journal.request_kinds() == {REQUEST_KIND_BOOTSTRAP: 1}
        events = journal.read_events()
        assert len(events) == report["bootstrap"]["events"]
        assert events
        head = journal.head_position()
        assert {
            name: journal.checkpoint(name).position for name in PROJECTION_NAMES
        } == {name: head for name in PROJECTION_NAMES}


def test_journal_replay_reproduces_the_published_graph_exactly(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    root = generation(destination)

    with MemoryEventJournal.open_readonly(root / "journal.db") as journal:
        events = journal.read_events()
    replay = InMemoryGraphStore()
    rebuild = replay.rebuild(events)
    assert rebuild.ok
    assert rebuild.nodes_written >= 8
    assert rebuild.edges_written >= 3

    store = SQLiteGraphStore(root / "graph.db")
    try:
        assert store.snapshot() == replay.snapshot()
        assert store.health().healthy
        assert store.health().node_count == rebuild.nodes_written
    finally:
        store.close()


def test_apply_preserves_the_frontmatter_identity_verbatim(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    root = generation(destination)

    expected_node = legacy_claim_id(FRONTMATTER_ENTRY_ID)
    assert row_metadata(root / "data" / "vectors.db")[FRONTMATTER_ENTRY_ID][
        GRAPH_SEED_ID_KEY
    ] == expected_node

    store = SQLiteGraphStore(root / "graph.db")
    try:
        assert store.get_node(expected_node) is not None
    finally:
        store.close()


def test_apply_aligns_l1_vector_and_l0_with_the_graph(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    root = generation(destination)

    # L1: the readable surface is the source document, byte for byte.
    assert (root / "knowledge" / "retrieval.md").read_text(
        encoding="utf-8"
    ) == RETRIEVAL_MD

    # The surface registry binds each ordinary section to its claim node.
    with MemoryEventJournal.open_readonly(root / "journal.db") as journal:
        surfaces = journal.surface_node_ids()
    for section, entry_id in RETRIEVAL_IDS.items():
        assert surfaces[entry_id] == legacy_claim_id(entry_id), section

    # Vector: every row points at the node its own identity derives.
    metadata = row_metadata(root / "data" / "vectors.db")
    for entry_id in RETRIEVAL_IDS.values():
        assert metadata[entry_id][GRAPH_SEED_ID_KEY] == legacy_claim_id(entry_id)

    # L0: regenerated from the published L1 files.
    l0 = (root / "L0.md").read_text(encoding="utf-8")
    assert "retrieval" in l0 and "pipeline" in l0


def test_apply_publishes_a_source_whose_rows_are_already_mapped(
    source, destination
):
    """An already-mapped row needs no update and must not block publication."""
    build_source(source, keep_graph_ids=frozenset(RETRIEVAL_IDS.values()))

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_APPLIED
    assert report["inventory"]["counts_by_category"]["existing"] == 2
    assert report["inventory"]["counts_by_category"]["mapped"] == 1
    metadata = row_metadata(generation(destination) / "data" / "vectors.db")
    for entry_id in RETRIEVAL_IDS.values():
        assert metadata[entry_id][GRAPH_SEED_ID_KEY] == legacy_claim_id(entry_id)


def test_apply_appends_nothing_to_the_preserved_changelog(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    root = generation(destination)

    live = (root / "data" / "memory_changelog.jsonl").read_bytes()
    assert live == (root / "backup" / "data" / "memory_changelog.jsonl").read_bytes()
    assert live == (source / "data" / "memory_changelog.jsonl").read_bytes()


def test_apply_preserves_vector_embeddings_and_changes_only_metadata(
    source, destination
):
    before = {row[0]: row[1:4] for row in vector_rows(source / "data" / "vectors.db")}

    graph_migration.apply_migration(source=source, destination=destination)

    after = {
        row[0]: row[1:4]
        for row in vector_rows(generation(destination) / "data" / "vectors.db")
    }
    assert after == before


def test_apply_infers_no_supersession_contradiction_or_validity(source, destination):
    """A migration records what the source said, and nothing it did not."""
    graph_migration.apply_migration(source=source, destination=destination)
    store = SQLiteGraphStore(generation(destination) / "graph.db")
    try:
        snapshot = store.snapshot()
    finally:
        store.close()

    relations = {edge["relation_type"] for edge in snapshot["edges"]}
    assert not relations & {"SUPERSEDES", "CONTRADICTS", "SUPPORTS"}
    for node in snapshot["nodes"]:
        # Validity the legacy data never encoded stays absent; the migration
        # records *when it learned* the material, not when it became true.
        assert node["valid_from"] is None
        assert node["valid_to"] is None
        if node["kind"] in ("claim", "evidence"):
            assert node["derivation"]["method"] == "migrated"


def test_the_apply_report_carries_no_memory_content(source, destination):
    report = graph_migration.apply_migration(source=source, destination=destination)
    serialized = json.dumps(report)

    for body in (
        "rebuilt nightly",
        "one month of documents",
        "batch loader",
        "Index maintenance",
    ):
        assert body not in serialized


def test_an_empty_write_ahead_log_is_reported_not_refused(source, destination):
    (source / "data" / "vectors.db-wal").write_bytes(b"")

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_APPLIED
    assert "vector_sidecar_present:data/vectors.db-wal" in report["warnings"]
    # The sidecar is inventoried and fingerprinted. It is retained only in the
    # source backup, never in the published projection artifact tree.
    assert not (generation(destination) / "data" / "vectors.db-wal").exists()
    assert (generation(destination) / "backup" / "data" / "vectors.db-wal").is_file()


def test_apply_reports_an_externally_owned_frontmatter_surface(source, destination):
    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["bootstrap"]["external_surfaces"] == [FRONTMATTER_ENTRY_ID]
    assert any(
        finding.startswith("surface_externally_owned") for finding in report["warnings"]
    )
    # Its readable surface is not adopted into the surface registry.
    with MemoryEventJournal.open_readonly(
        generation(destination) / "journal.db"
    ) as journal:
        assert FRONTMATTER_ENTRY_ID not in journal.surface_node_ids()


# ---------------------------------------------------------------------------
# Duplicate apply, changed source, changed destination
# ---------------------------------------------------------------------------

def test_duplicate_apply_of_an_identical_source_writes_nothing(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    before = tree_state(destination)
    source_before = tree_state(source)

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_ALREADY_PUBLISHED
    assert report["exit_code"] == graph_migration.EXIT_OK
    assert tree_state(destination) == before
    assert tree_state(source) == source_before


def test_apply_refuses_a_changed_source_against_a_published_generation(
    source, destination
):
    graph_migration.apply_migration(source=source, destination=destination)
    before = tree_state(destination)
    (source / "knowledge" / "retrieval.md").write_text(
        RETRIEVAL_MD + "\n## Replication\n\nTwo replicas per shard.\n",
        encoding="utf-8",
    )

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_DESTINATION_REFUSED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert "destination_source_mismatch" in report["findings"]
    assert tree_state(destination) == before


def test_apply_refuses_a_changed_destination(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    target = generation(destination) / "knowledge" / "retrieval.md"
    target.write_text(RETRIEVAL_MD + "\nedited by somebody else\n", encoding="utf-8")
    before = tree_state(destination)

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_DESTINATION_REFUSED
    assert "destination_changed" in report["findings"]
    assert tree_state(destination) == before


def test_apply_refuses_unknown_destination_contents(source, destination):
    destination.mkdir(parents=True)
    (destination / "somebody-elses-notes.md").write_text("hello", encoding="utf-8")
    before = tree_state(destination)

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_DESTINATION_REFUSED
    assert any(
        finding.startswith("destination_unknown_contents")
        for finding in report["findings"]
    )
    assert tree_state(destination) == before


# ---------------------------------------------------------------------------
# Unresolved identity blocks publication
# ---------------------------------------------------------------------------

def _add_vector_row(path: Path, row_id: str, *, metadata: str | None, vector=None):
    """Append one raw legacy row. Fixture-only, deliberately low level."""
    from layered_memory_mcp.storage.vector_store import EMBED_DIM

    payload = json.dumps([0.0] * EMBED_DIM) if vector is None else vector
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO vectors (id, domain, text, vector, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (row_id, "retrieval", "legacy text", payload, metadata),
        )
        connection.commit()
    finally:
        connection.close()


def test_apply_refuses_an_unmapped_random_legacy_identity(source, destination):
    _add_vector_row(
        source / "data" / "vectors.db",
        "3f9b0c1e-5a7d-4c2b-9f10-6d8e2a4b7c31",
        metadata=json.dumps({"type": "fact"}),
    )

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert report["inventory"]["counts_by_category"]["orphaned"] == 1
    assert any("orphaned" in finding for finding in report["findings"])
    assert not destination.exists()


def test_apply_refuses_conflicting_stored_graph_metadata(source, destination):
    rows = vector_rows(source / "data" / "vectors.db")
    victim = next(row for row in rows if row[0] == RETRIEVAL_IDS["Shard layout"])
    metadata = json.loads(victim[4]) if victim[4] else {}
    metadata[GRAPH_SEED_ID_KEY] = "claim_0000000000000000"
    connection = sqlite3.connect(source / "data" / "vectors.db")
    try:
        connection.execute(
            "UPDATE vectors SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, sort_keys=True), victim[0]),
        )
        connection.commit()
    finally:
        connection.close()

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert report["inventory"]["counts_by_category"]["conflicting"] == 1
    assert not destination.exists()


def test_apply_refuses_malformed_row_metadata(source, destination):
    connection = sqlite3.connect(source / "data" / "vectors.db")
    try:
        connection.execute(
            "UPDATE vectors SET metadata = ? WHERE id = ?",
            ("not json at all", RETRIEVAL_IDS["Index maintenance"]),
        )
        connection.commit()
    finally:
        connection.close()

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    counts = report["inventory"]["counts_by_category"]
    assert sum(counts.values()) == report["inventory"]["vector_rows"]
    assert counts["malformed"] == 1
    assert not destination.exists()


def test_apply_refuses_missing_vector_data_rather_than_inventing_one(
    source, destination
):
    rows = vector_rows(source / "data" / "vectors.db")
    victim = next(row for row in rows if row[0] == RETRIEVAL_IDS["Shard layout"])
    connection = sqlite3.connect(source / "data" / "vectors.db")
    try:
        connection.execute(
            "UPDATE vectors SET vector = ? WHERE id = ?", ("[]", victim[0])
        )
        connection.commit()
    finally:
        connection.close()

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert report["inventory"]["counts_by_category"]["missing"] == 1
    assert not destination.exists()


def test_an_explicit_validated_identity_mapping_resolves_an_orphan_row(
    tmp_path, destination
):
    root = build_source(tmp_path / "old-store")
    # An orphan row for a section the snapshot *does* contain, under the random
    # identity an older projection minted.
    orphan = "3f9b0c1e-5a7d-4c2b-9f10-6d8e2a4b7c31"
    _add_vector_row(
        root / "data" / "vectors.db", orphan, metadata=json.dumps({"type": "fact"})
    )
    connection = sqlite3.connect(root / "data" / "vectors.db")
    try:
        connection.execute(
            "DELETE FROM vectors WHERE id = ?", (RETRIEVAL_IDS["Shard layout"],)
        )
        connection.commit()
    finally:
        connection.close()

    mapping = tmp_path / "identity-map.json"
    mapping.write_text(
        json.dumps({orphan: RETRIEVAL_IDS["Shard layout"]}), encoding="utf-8"
    )

    report = graph_migration.apply_migration(
        source=root, destination=destination, identity_map=mapping
    )

    assert report["status"] == graph_migration.STATUS_APPLIED
    metadata = row_metadata(generation(destination) / "data" / "vectors.db")
    assert metadata[orphan][GRAPH_SEED_ID_KEY] == legacy_claim_id(
        RETRIEVAL_IDS["Shard layout"]
    )
    # The row keeps its own identity: nothing is renamed in place.
    assert RETRIEVAL_IDS["Shard layout"] not in metadata


def test_an_identity_mapping_naming_an_unknown_entry_is_refused(tmp_path, destination):
    root = build_source(tmp_path / "old-store")
    orphan = "3f9b0c1e-5a7d-4c2b-9f10-6d8e2a4b7c31"
    _add_vector_row(
        root / "data" / "vectors.db", orphan, metadata=json.dumps({"type": "fact"})
    )
    mapping = tmp_path / "identity-map.json"
    mapping.write_text(json.dumps({orphan: "no-such-entry"}), encoding="utf-8")

    report = graph_migration.apply_migration(
        source=root, destination=destination, identity_map=mapping
    )

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert any(
        finding.startswith("identity_map_") for finding in report["findings"]
    )
    assert not destination.exists()


# ---------------------------------------------------------------------------
# Source shape refusals
# ---------------------------------------------------------------------------

def test_apply_refuses_a_symlink_inside_the_source(source, destination, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("## Elsewhere\n\nNot part of the snapshot.\n", encoding="utf-8")
    (source / "knowledge" / "linked.md").symlink_to(outside)

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert any(
        finding.startswith("source_symlink") for finding in report["findings"]
    )
    assert not destination.exists()


def test_apply_refuses_an_unsupported_source_record(source, destination):
    (source / "knowledge" / "notes.txt").write_text("stray", encoding="utf-8")

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert any(
        finding.startswith("source_unsupported_record")
        for finding in report["findings"]
    )


def test_l0_is_evidence_only_and_blocks_legacy_plan(source, destination):
    """L0 provenance must not become a legacy knowledge input by accident."""
    (source / "L0.md").write_text("## evidence-only\n\nnot an L1 entry\n", encoding="utf-8")
    before = tree_state(source)

    report = graph_migration.plan_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert "source_unsupported_record:L0.md" in report["findings"]
    assert report["inventory"]["l1_documents"] == 2
    assert report["inventory"]["l1_entries"] == 3
    assert not destination.exists()
    assert tree_state(source) == before
    assert "not an L1 entry" not in json.dumps(report)


def test_apply_refuses_a_destination_inside_the_source(source):
    report = graph_migration.apply_migration(
        source=source, destination=source / "migrated"
    )

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert any(finding.startswith("path_overlap") for finding in report["findings"])
    assert not (source / "migrated").exists()


def test_apply_refuses_a_nonempty_vector_write_ahead_log(source, destination):
    (source / "data" / "vectors.db-wal").write_bytes(b"\x00" * 64)

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert any(
        finding.startswith("vector_wal_not_empty") for finding in report["findings"]
    )
    assert not destination.exists()


def test_plan_refuses_a_missing_source(tmp_path, destination):
    report = graph_migration.plan_migration(
        source=tmp_path / "nowhere", destination=destination
    )

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert "source_missing" in report["findings"]


# ---------------------------------------------------------------------------
# Section-reader compatibility
#
# A migration binds a vector row (produced by ``injector._vector_entries``) to
# a readable surface that every later read goes through
# (``graph_projection.read_section_body``). Where the two readers disagree about
# which H2 a body belongs to, the published binding would name a section whose
# body nobody else can find — so the source is refused instead.
# ---------------------------------------------------------------------------

READER_FINDINGS = [
    "l1_section_reader_incompatible:retrieval:canonical_body_differs",
    "l1_section_reader_incompatible:retrieval:canonical_section_unprojected",
]


def reader_findings(report: dict) -> list[str]:
    return [
        finding
        for finding in report["findings"]
        if finding.startswith("l1_section_reader_incompatible")
    ]


def test_the_two_readers_really_do_disagree_about_these_documents():
    """The disagreement itself, so the preflight's reason is pinned too.

    Without this, the tests below could keep passing against a preflight that
    had started refusing these documents for some *other* reason.
    """
    from layered_memory_mcp.graph_projection import read_section_body

    duplicate = _entries_for("d", DUPLICATE_LABEL_EMPTY_FIRST_MD)
    assert [(item.section, item.content) for item in duplicate] == [("Same", "kept")]
    assert read_section_body(DUPLICATE_LABEL_EMPTY_FIRST_MD, "Same", 0) == ""
    assert read_section_body(DUPLICATE_LABEL_EMPTY_FIRST_MD, "Same", 1) == "kept"

    tabbed = _entries_for("d", TAB_SEPARATED_HEADING_MD)
    assert [item.section for item in tabbed] == ["First"]
    assert "Second" in tabbed[0].content
    assert read_section_body(TAB_SEPARATED_HEADING_MD, "First", 0) == "body"
    assert read_section_body(TAB_SEPARATED_HEADING_MD, "Second", 0) == "second"


@pytest.mark.parametrize(
    "raw", [DUPLICATE_LABEL_EMPTY_FIRST_MD, TAB_SEPARATED_HEADING_MD]
)
def test_the_preflight_names_both_directions_of_a_reader_disagreement(raw):
    from layered_memory_mcp.graph_migration_inventory import section_reader_findings

    assert section_reader_findings("retrieval", raw) == READER_FINDINGS


def test_the_preflight_names_a_section_the_canonical_reader_cannot_find():
    """A projected section the canonical reader has no occurrence for at all.

    The projection accepts a heading wrapped after ``##``; the canonical reader
    requires a space or a tab and reads the whole thing as preamble. A binding
    built from this would point at nothing.
    """
    from layered_memory_mcp.graph_migration_inventory import section_reader_findings
    from layered_memory_mcp.graph_projection import read_section_body

    raw = "##\nWrapped\n\nbody\n"
    assert [item.section for item in _entries_for("d", raw)] == ["Wrapped"]
    assert read_section_body(raw, "Wrapped", 0) is None
    assert section_reader_findings("retrieval", raw) == [
        "l1_section_reader_incompatible:retrieval:canonical_section_absent"
    ]


@pytest.mark.parametrize("raw", [RETRIEVAL_MD, TRAILING_EMPTY_SECTION_MD, ""])
def test_the_preflight_accepts_documents_the_two_readers_agree_about(raw):
    from layered_memory_mcp.graph_migration_inventory import section_reader_findings

    assert section_reader_findings("retrieval", raw) == []


@pytest.mark.parametrize(
    "raw", [DUPLICATE_LABEL_EMPTY_FIRST_MD, TAB_SEPARATED_HEADING_MD]
)
def test_plan_blocks_a_document_the_two_readers_read_differently(
    tmp_path, destination, raw
):
    root = build_source_with_document(tmp_path / "old-store", raw)
    before = tree_state(root)

    report = graph_migration.plan_migration(source=root, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert reader_findings(report) == READER_FINDINGS
    # Nothing was created on either side, and no body travelled in the report.
    assert not destination.exists()
    assert tree_state(root) == before
    serialized = json.dumps(report)
    for body in ("kept", "second", "Same", "Second"):
        assert body not in serialized


@pytest.mark.parametrize(
    "raw", [DUPLICATE_LABEL_EMPTY_FIRST_MD, TAB_SEPARATED_HEADING_MD]
)
def test_apply_of_an_incompatible_document_creates_no_destination(
    tmp_path, destination, raw
):
    root = build_source_with_document(tmp_path / "old-store", raw)
    before = tree_state(root)

    report = graph_migration.apply_migration(source=root, destination=destination)

    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert reader_findings(report) == READER_FINDINGS
    assert not destination.exists()
    assert tree_state(root) == before
    # Refused before anything was planned from it, let alone staged.
    assert report["bootstrap"] == {}


def test_a_bodyless_trailing_section_is_published_rather_than_refused(
    tmp_path, destination
):
    root = build_source_with_document(
        tmp_path / "old-store", TRAILING_EMPTY_SECTION_MD
    )

    report = graph_migration.apply_migration(source=root, destination=destination)

    assert report["status"] == graph_migration.STATUS_APPLIED
    assert reader_findings(report) == []
    assert report["bootstrap"]["bound_surfaces"] == 2
    # The bodyless section is not an authored object to either reader, and the
    # published document still carries it verbatim.
    assert (generation(destination) / "knowledge" / "retrieval.md").read_text(
        encoding="utf-8"
    ) == TRAILING_EMPTY_SECTION_MD


def test_a_frontmatter_document_is_not_measured_as_an_h2_surface(
    tmp_path, destination
):
    """Frontmatter ownership is reported, not re-read as an H2 document.

    The preflight is about the one surface kind this framework owns. A
    REST-owned frontmatter file has its own model contract, so comparing it
    against the H2 reader would manufacture a disagreement — and "bind it to an
    H2 anyway" is exactly what must not happen.
    """
    from layered_memory_mcp.graph_migration_inventory import section_reader_findings

    assert section_reader_findings("pipeline", PIPELINE_MD) == []

    report = graph_migration.apply_migration(
        source=build_source(tmp_path / "old-store"), destination=destination
    )

    assert report["status"] == graph_migration.STATUS_APPLIED
    assert reader_findings(report) == []
    assert report["bootstrap"]["external_surfaces"] == [FRONTMATTER_ENTRY_ID]
    assert report["inventory"]["frontmatter_entries"] == 1


# ---------------------------------------------------------------------------
# Recording time: declared, fixed, and never a validity
# ---------------------------------------------------------------------------

MIGRATION_MOMENT = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)


def _snapshot_and_plan(source: Path, moment: datetime | None = None):
    from layered_memory_mcp.graph_migration_bootstrap import plan_bootstrap
    from layered_memory_mcp.graph_migration_inventory import read_source_snapshot

    snapshot = read_source_snapshot(source)
    return snapshot, plan_bootstrap(snapshot, recorded_at=moment)


def test_bootstrap_declares_migration_time_as_an_inferred_recording_time(source):
    from layered_memory_mcp.graph_migration_bootstrap import (
        INFERRED_MIGRATION_RECORDED_AT,
        WARNING_MIGRATION_RECORDED_AT,
    )

    _snapshot, plan = _snapshot_and_plan(source, MIGRATION_MOMENT)

    assert plan.ok
    assert plan.report.recorded_at == MIGRATION_MOMENT.isoformat()
    assert INFERRED_MIGRATION_RECORDED_AT in plan.report.inferred_fields
    # Two ordinary sections had no recording time; the frontmatter entry did.
    assert f"{WARNING_MIGRATION_RECORDED_AT}:2" in plan.report.warnings


def test_an_explicit_recorded_at_is_used_verbatim_and_repeatably(source):
    """The fixed-instant API is what makes two readings of one source equal."""
    _snapshot, first = _snapshot_and_plan(source, MIGRATION_MOMENT)
    _again, second = _snapshot_and_plan(source, MIGRATION_MOMENT)

    assert [event.id for event in first.events] == [
        event.id for event in second.events
    ]
    assert [event.occurred_at for event in first.events] == [
        event.occurred_at for event in second.events
    ]

    nodes = {
        event.node.id: event.node for event in first.events if event.node is not None
    }
    for entry_id in RETRIEVAL_IDS.values():
        assert nodes[legacy_claim_id(entry_id)].recorded_at == MIGRATION_MOMENT
    # An authored timestamp is not overwritten by the migration's clock…
    assert nodes[legacy_claim_id(FRONTMATTER_ENTRY_ID)].recorded_at == T0
    # …and no clock read is presented as the statement's own validity.
    for node in nodes.values():
        assert node.valid_from is None
        assert node.valid_to is None


def test_the_manifest_names_migration_time_as_inferred_provenance(
    source, destination
):
    from layered_memory_mcp.graph_migration_bootstrap import (
        INFERRED_MIGRATION_RECORDED_AT,
        WARNING_MISSING_RECORDED_AT,
        WARNING_MIGRATION_RECORDED_AT,
    )

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_APPLIED
    provenance = manifest_of(destination)["provenance"]
    assert "claim.recorded_at:missing_source" in provenance["inferred_fields"]
    assert INFERRED_MIGRATION_RECORDED_AT not in provenance["inferred_fields"]
    assert provenance["recorded_at"] == report["bootstrap"]["recorded_at"]
    assert any(
        warning.startswith(WARNING_MISSING_RECORDED_AT)
        for warning in report["warnings"]
    )


def test_default_missing_recorded_at_is_stable_and_explicitly_synthetic(source):
    from layered_memory_mcp.graph_migration_bootstrap import (
        MISSING_RECORDED_AT,
        WARNING_MISSING_RECORDED_AT,
    )

    _snapshot, first = _snapshot_and_plan(source)
    _again, second = _snapshot_and_plan(source)

    assert first.report.recorded_at == MISSING_RECORDED_AT.isoformat()
    assert second.report.recorded_at == MISSING_RECORDED_AT.isoformat()
    assert first.report.inferred_fields.count("claim.recorded_at:missing_source") == 1
    assert f"{WARNING_MISSING_RECORDED_AT}:2" in first.report.warnings
    assert [event.to_dict() for event in first.events] == [
        event.to_dict() for event in second.events
    ]
    nodes = {
        event.node.id: event.node for event in first.events if event.node is not None
    }
    for entry_id in RETRIEVAL_IDS.values():
        assert nodes[legacy_claim_id(entry_id)].attributes[
            "legacy_source_extracted_at"
        ] == MISSING_RECORDED_AT.isoformat()
    assert nodes[legacy_claim_id(FRONTMATTER_ENTRY_ID)].attributes[
        "legacy_source_extracted_at"
    ] == MISSING_RECORDED_AT.isoformat()


def test_authored_frontmatter_source_extracted_at_is_preserved(tmp_path):
    authored = "2025-12-31T23:59:00+00:00"
    source = build_source(tmp_path / "authored-source")
    pipeline = source / "knowledge" / "pipeline.md"
    pipeline.write_text(
        pipeline.read_text(encoding="utf-8").replace(
            "updated_at: '2026-03-01T09:00:00+00:00'\n---",
            "updated_at: '2026-03-01T09:00:00+00:00'\n"
            "source:\n"
            "  type: manual\n"
            f"  extracted_at: '{authored}'\n"
            "---",
        ),
        encoding="utf-8",
    )

    _snapshot, plan = _snapshot_and_plan(source)

    node = next(
        event.node
        for event in plan.events
        if event.node is not None
        and event.node.id == legacy_claim_id(FRONTMATTER_ENTRY_ID)
    )
    assert node.attributes["legacy_source_extracted_at"] == authored


# ---------------------------------------------------------------------------
# Source drift and publication failure
# ---------------------------------------------------------------------------

def test_apply_refuses_source_drift_detected_before_publication(source, destination):
    def _drift(staging: Path, target: Path) -> None:
        (source / "knowledge" / "retrieval.md").write_text(
            RETRIEVAL_MD + "\n## Replication\n\nTwo replicas per shard.\n",
            encoding="utf-8",
        )

    graph_migration.BEFORE_PUBLISH_HOOK = _drift

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_SOURCE_DRIFT
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert "source_drift" in report["findings"]
    assert not generation(destination).exists()
    # The staged work is accounted for, not silently left as reusable debris.
    debris = sorted(p.name for p in (destination / "debris").iterdir())
    assert debris == [Path(report["generation"]["staged_debris"]).name]


def test_an_injected_publication_failure_leaves_no_visible_generation(
    source, destination
):
    def _fail(staging: Path, target: Path) -> None:
        raise OSError("injected publication failure")

    graph_migration.BEFORE_PUBLISH_HOOK = _fail
    source_before = tree_state(source)

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_PUBLICATION_FAILED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert not generation(destination).exists()
    assert tree_state(source) == source_before
    assert report["generation"]["staged_debris"]
    assert Path(report["generation"]["staged_debris"]).is_dir()
    # A later apply does not reuse the debris; it stages afresh and publishes.
    graph_migration.BEFORE_PUBLISH_HOOK = None
    retry = graph_migration.apply_migration(source=source, destination=destination)
    assert retry["status"] == graph_migration.STATUS_APPLIED
    assert Path(report["generation"]["staged_debris"]).is_dir()


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def test_verify_accepts_a_published_generation_and_writes_nothing(
    source, destination
):
    graph_migration.apply_migration(source=source, destination=destination)
    before = tree_state(destination)
    source_before = tree_state(source)

    report = graph_migration.verify_migration(
        destination=destination, source=source
    )

    assert report["status"] == graph_migration.STATUS_VERIFIED
    assert report["exit_code"] == graph_migration.EXIT_OK
    assert report["verification"]["ok"] is True
    assert report["verification"]["replay_equal"] is True
    assert report["verification"]["source_drift"] is False
    assert tree_state(destination) == before
    assert tree_state(source) == source_before


def test_verify_detects_a_tampered_artifact(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    (generation(destination) / "knowledge" / "retrieval.md").write_text(
        "## Index maintenance\n\nQuietly rewritten.\n", encoding="utf-8"
    )

    report = graph_migration.verify_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_VERIFY_FAILED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert any(
        finding.startswith("artifact_hash_mismatch")
        for finding in report["findings"]
    )


def test_verify_detects_an_added_artifact(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    (generation(destination) / "knowledge" / "extra.md").write_text(
        "## Extra\n\nAdded later.\n", encoding="utf-8"
    )

    report = graph_migration.verify_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_VERIFY_FAILED
    assert any(
        finding.startswith("artifact_unexpected") for finding in report["findings"]
    )


def test_verify_detects_tampered_vector_metadata(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    target = generation(destination) / "data" / "vectors.db"
    connection = sqlite3.connect(target)
    try:
        connection.execute(
            "UPDATE vectors SET metadata = ? WHERE id = ?",
            (
                json.dumps({GRAPH_SEED_ID_KEY: "claim_0000000000000000"}),
                RETRIEVAL_IDS["Shard layout"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    report = graph_migration.verify_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_VERIFY_FAILED
    assert report["findings"]


def test_verify_detects_a_forged_manifest(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    path = generation(destination) / "manifest.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["counts"]["vector_rows"] = 99
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")

    report = graph_migration.verify_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_VERIFY_FAILED
    assert "manifest_digest_mismatch" in report["findings"]


def test_verify_detects_tampered_source_file_provenance(source, destination):
    from layered_memory_mcp.graph_migration_generation import write_manifest

    graph_migration.apply_migration(source=source, destination=destination)
    root = generation(destination)
    path = root / "manifest.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    source_path = next(iter(body["source_files"]))
    body["source_files"][source_path] = {"size": 999999, "sha256": "f" * 64}
    # Re-sign the manifest so this exercises source_files verification rather
    # than merely the manifest digest check.
    write_manifest(root, body)

    report = graph_migration.verify_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_VERIFY_FAILED
    assert report["verification"]["source_files_ok"] is False
    assert any(
        finding.startswith("source_file_") for finding in report["findings"]
    )


def test_verify_refuses_a_manifest_artifact_path_escape(source, destination):
    from layered_memory_mcp.graph_migration_generation import write_manifest

    graph_migration.apply_migration(source=source, destination=destination)
    root = generation(destination)
    body = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    body["artifacts"]["../../escaped.md"] = {"size": 1, "sha256": "0" * 64}
    # Re-signed on purpose: this must be refused by the path rule itself, not
    # merely caught by the digest check.
    write_manifest(root, body)

    report = graph_migration.verify_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_VERIFY_FAILED
    assert any(
        finding.startswith("manifest_path_rejected")
        for finding in report["findings"]
    )


def test_verify_reports_source_drift_instead_of_accepting_it(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    (source / "knowledge" / "retrieval.md").write_text(
        RETRIEVAL_MD + "\n## Replication\n\nTwo replicas per shard.\n",
        encoding="utf-8",
    )

    report = graph_migration.verify_migration(
        destination=destination, source=source
    )

    assert report["status"] == graph_migration.STATUS_SOURCE_DRIFT
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert report["verification"]["source_drift"] is True
    # The generation itself is still intact — the two claims stay separate.
    assert report["verification"]["generation_intact"] is True


def test_a_same_size_source_edit_is_detected_as_drift(source, destination):
    """The fingerprint covers bytes, not sizes and not modification times."""
    graph_migration.apply_migration(source=source, destination=destination)
    document = source / "knowledge" / "retrieval.md"
    before = document.read_text(encoding="utf-8")
    after = before.replace("nightly by the", "nightly BY the")
    assert len(after.encode("utf-8")) == len(before.encode("utf-8"))
    document.write_text(after, encoding="utf-8")

    report = graph_migration.verify_migration(
        destination=destination, source=source
    )

    assert report["status"] == graph_migration.STATUS_SOURCE_DRIFT
    assert report["verification"]["source_drift"] is True


def test_verify_reports_an_absent_generation(source, destination):
    report = graph_migration.verify_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_VERIFY_FAILED
    assert "generation_absent" in report["findings"]


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def test_rollback_withdraws_the_generation_and_retains_its_artifacts(
    source, destination
):
    graph_migration.apply_migration(source=source, destination=destination)
    published = tree_state(generation(destination))
    source_before = tree_state(source)

    report = graph_migration.rollback_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_ROLLED_BACK
    assert report["exit_code"] == graph_migration.EXIT_OK
    assert not generation(destination).exists()
    retained = Path(report["generation"]["withdrawn_to"])
    assert retained.is_dir()
    assert tree_state(retained) == published
    assert tree_state(source) == source_before
    assert report["generation"]["destination_prestate"] == "absent"


def test_rollback_renames_only_inside_the_destination(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)

    report = graph_migration.rollback_migration(destination=destination)

    withdrawn = Path(report["generation"]["withdrawn_to"])
    assert withdrawn.parent == destination / "rollback"
    assert destination in withdrawn.parents


def test_rollback_reports_a_recorded_empty_destination_prestate(source, destination):
    destination.mkdir(parents=True)
    applied = graph_migration.apply_migration(
        source=source, destination=destination
    )
    assert applied["generation"]["destination_prestate"] == "empty"
    assert manifest_of(destination)["destination_prestate"] == "empty"

    report = graph_migration.rollback_migration(destination=destination)

    assert report["generation"]["destination_prestate"] == "empty"
    assert not generation(destination).exists()


def test_repeated_rollback_is_safe_and_writes_nothing(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    graph_migration.rollback_migration(destination=destination)
    before = tree_state(destination)

    report = graph_migration.rollback_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_ROLLBACK_NOTHING
    assert report["exit_code"] == graph_migration.EXIT_OK
    assert tree_state(destination) == before


def test_rollback_refuses_a_changed_target(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    (generation(destination) / "knowledge" / "retrieval.md").write_text(
        "## Index maintenance\n\nChanged after the migration.\n", encoding="utf-8"
    )
    before = tree_state(destination)

    report = graph_migration.rollback_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert generation(destination).exists()
    assert tree_state(destination) == before
    assert not generation_module.served_lock_path(destination).exists()


def test_rollback_refuses_unknown_destination_contents(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    (destination / "stray.md").write_text("hello", encoding="utf-8")
    before = tree_state(destination)

    report = graph_migration.rollback_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert tree_state(destination) == before


def test_apply_after_rollback_republishes_from_the_retained_source(
    source, destination
):
    graph_migration.apply_migration(source=source, destination=destination)
    rollback = graph_migration.rollback_migration(destination=destination)
    retained = Path(rollback["generation"]["withdrawn_to"])

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_APPLIED
    assert generation(destination).is_dir()
    # The withdrawn generation is retained, not reused and not deleted.
    assert retained.is_dir()
    assert (retained / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# The admission ledger: a destination entry, owned as strictly as the rest
#
# ``served/`` is where an activation records that a runtime has been given a
# generation, and ``.served.lock`` is the guard both operations take. They are
# the only control artifacts this destination grows, and adding them must not
# turn the destination into a place arbitrary files are tolerated: every name in
# there is this tool's, every record is this tool's format, and anything else
# makes the destination refusable exactly as a stray file always has.
# ---------------------------------------------------------------------------

def served(destination: Path) -> Path:
    return destination / generation_module.SERVED_DIR


def admit(destination: Path) -> str:
    """Record a genuine admission for the published generation, as the tool does."""
    body, findings = generation_module.read_manifest(generation(destination))
    assert body is not None and findings == [], findings
    digest = generation_module.manifest_digest(body)
    generation_module.register_admission(
        destination, generation(destination), digest, tool="test"
    )
    return digest


def test_the_admission_ledger_is_an_owned_destination_entry(source, destination):
    graph_migration.apply_migration(source=source, destination=destination)
    digest = admit(destination)

    findings, warnings = generation_module.destination_findings(destination)

    assert findings == []
    assert warnings == []
    assert served(destination).is_dir()
    assert generation_module.admitted_digests(destination) == (frozenset({digest}), [])
    # A verify still reads the generation and reports it intact beside it.
    verified = graph_migration.verify_migration(destination=destination)
    assert verified["status"] == graph_migration.STATUS_VERIFIED


def test_a_repeated_admission_of_one_generation_writes_nothing_new(
    source, destination
):
    graph_migration.apply_migration(source=source, destination=destination)
    digest = admit(destination)
    before = tree_state(destination)

    assert admit(destination) == digest

    assert tree_state(destination) == before


@pytest.mark.parametrize(
    "name",
    ["stray.json", "notes.txt", "deadbeef.json", "", "0123456789abcdef.json"],
)
def test_an_unknown_file_in_the_admission_ledger_refuses_the_destination(
    source, destination, name
):
    """The ledger is a closed set of names, not a tolerated directory."""
    graph_migration.apply_migration(source=source, destination=destination)
    admit(destination)
    served(destination).mkdir(parents=True, exist_ok=True)
    stray = served(destination) / (name or "no-suffix")
    stray.write_text("{}", encoding="utf-8")
    before = tree_state(destination)

    findings, _warnings = generation_module.destination_findings(destination)
    report = graph_migration.rollback_migration(destination=destination)

    assert any(
        finding.startswith(generation_module.FINDING_SERVED_REJECTED)
        for finding in findings
    ), findings
    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation(destination).is_dir()
    assert tree_state(destination) == before


def test_a_symlinked_admission_record_refuses_the_destination(
    source, destination, tmp_path
):
    graph_migration.apply_migration(source=source, destination=destination)
    digest = admit(destination)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}", encoding="utf-8")
    link = served(destination) / f"{'b' * 64}.json"
    link.symlink_to(elsewhere)

    findings, _warnings = generation_module.destination_findings(destination)
    report = graph_migration.rollback_migration(destination=destination)

    rejected = f"{generation_module.FINDING_SERVED_REJECTED}:{link.name}:symlink"
    assert rejected in findings
    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation(destination).is_dir()
    # The planted link is refused by name; the genuine record is left alone.
    assert (served(destination) / f"{digest}.json").is_file()
    assert elsewhere.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("not json at all", "JSONDecodeError"),
        ("[]", "not_a_mapping"),
        ('{"served_version": 99, "manifest_sha256": "", "generation": "current"}',
         "version_unsupported"),
        ('{"served_version": 1, "manifest_sha256": "nope", "generation": "current"}',
         "digest_mismatch"),
        ('{"served_version": 1, "manifest_sha256": "%s", "generation": "../escape"}'
         % ("c" * 64),
         "generation_rejected"),
    ],
)
def test_a_forged_admission_record_fails_closed(source, destination, body, reason):
    """An unreadable ledger cannot establish that a generation was *not* served."""
    graph_migration.apply_migration(source=source, destination=destination)
    served(destination).mkdir(parents=True, exist_ok=True)
    marker = served(destination) / f"{'c' * 64}.json"
    marker.write_text(body, encoding="utf-8")
    before = tree_state(destination)

    findings, _warnings = generation_module.destination_findings(destination)
    report = graph_migration.rollback_migration(destination=destination)

    assert f"{generation_module.FINDING_SERVED_REJECTED}:{marker.name}:{reason}" in (
        findings
    ), findings
    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation(destination).is_dir()
    assert tree_state(destination) == before


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_an_unusable_admission_ledger_refuses_the_destination(
    source, destination, tmp_path, kind
):
    graph_migration.apply_migration(source=source, destination=destination)
    if kind == "symlink":
        target = tmp_path / "ledger"
        target.mkdir()
        served(destination).symlink_to(target, target_is_directory=True)
        expected = f"{generation_module.FINDING_SERVED_REJECTED}:served:symlink"
    else:
        served(destination).write_text("not a directory\n", encoding="utf-8")
        expected = f"{generation_module.FINDING_SERVED_REJECTED}:served:not_a_directory"

    findings, _warnings = generation_module.destination_findings(destination)
    report = graph_migration.rollback_migration(destination=destination)
    planned = graph_migration.plan_migration(source=source, destination=destination)

    assert expected in findings
    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert planned["status"] == graph_migration.STATUS_DESTINATION_REFUSED
    assert generation(destination).is_dir()


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_guard_lock_path_that_is_not_a_lock_refuses_the_destination(
    source, destination, tmp_path, kind
):
    """The unused reserved guard path must still be absent or a regular file."""
    graph_migration.apply_migration(source=source, destination=destination)
    lock = generation_module.served_lock_path(destination)
    if kind == "symlink":
        elsewhere = tmp_path / "lock-elsewhere"
        elsewhere.write_text("", encoding="utf-8")
        lock.symlink_to(elsewhere)
    else:
        lock.mkdir()
    before = tree_state(destination)

    findings, _warnings = generation_module.destination_findings(destination)
    report = graph_migration.rollback_migration(destination=destination)

    assert f"{generation_module.FINDING_DESTINATION_UNKNOWN}:{lock.name}" in findings
    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation(destination).is_dir()
    assert tree_state(destination) == before


def test_a_generation_with_no_readable_identity_is_not_withdrawn(
    source, destination
):
    """No identity, no lookup, no withdrawal: the ledger cannot clear it.

    A manifest that does not match its own digest sidecar makes "was this ever
    admitted?" unanswerable, and an unanswerable question is a refusal here —
    the same direction every other guard in this module fails in.
    """
    graph_migration.apply_migration(source=source, destination=destination)
    manifest = generation(destination) / "manifest.json"
    body = json.loads(manifest.read_text(encoding="utf-8"))
    body["counts"] = {"tampered": 1}
    manifest.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    before = tree_state(destination)

    report = graph_migration.rollback_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert f"{generation_module.FINDING_ADMISSION_UNPROVED}:manifest" in (
        report["findings"]
    )
    assert generation(destination).is_dir()
    assert tree_state(destination) == before
    assert not generation_module.served_lock_path(destination).exists()


def test_a_never_served_destination_still_rolls_back_and_repeats_as_a_no_op(
    source, destination
):
    """The ordinary path, restated beside the new refusals.

    The ledger must not become a reason an ordinary rollback stops working: a
    destination nothing was ever served from has no records, and the withdrawal
    behaves exactly as it did — once, retaining everything, then as a no-op.
    """
    graph_migration.apply_migration(source=source, destination=destination)
    assert not served(destination).exists()

    first = graph_migration.rollback_migration(destination=destination)

    assert first["status"] == graph_migration.STATUS_ROLLED_BACK
    assert first["generation"]["admitted"] is False
    retained = Path(first["generation"]["withdrawn_to"])
    assert retained.is_dir()
    after = tree_state(destination)

    second = graph_migration.rollback_migration(destination=destination)

    assert second["status"] == graph_migration.STATUS_ROLLBACK_NOTHING
    assert second["exit_code"] == graph_migration.EXIT_OK
    assert tree_state(destination) == after
    # Taking the guard left nothing behind in the destination either.
    assert not generation_module.served_lock_path(destination).exists()

"""Exact legacy identity adaptation, using only isolated neutral snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import unicodedata
from uuid import uuid4

import pytest

from layered_memory_mcp import graph_migration
from layered_memory_mcp import graph_migration_generation as generation_module
from layered_memory_mcp.graph_migration_inventory import read_source_snapshot, read_vector_rows
from layered_memory_mcp.graph_seed_metadata import graph_node_id_for_entry_id
from layered_memory_mcp.heading import embedding_heading

from test_v361_graph_migration import (
    _changelog_line,
    _entries_for,
    _write_vector_db,
    fake_embeddings,
    generation,
)


DOCUMENT = "## Neutral section\n\nA neutral café sample.\n"


class ReadOnlyConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement):
        return self.connection.execute(statement)

    def close(self):
        self.connection.close()


def tree_state(root):
    """Approved zero-write fields; ordinary reads may change access times."""
    result = {}
    pending = [(root, ".")]
    while pending:
        path, relative = pending.pop()
        metadata = path.lstat()
        payload = None
        if stat.S_ISDIR(metadata.st_mode):
            pending.extend(
                (child, f"{relative}/{child.name}") for child in path.iterdir()
            )
        elif stat.S_ISREG(metadata.st_mode):
            payload = path.read_bytes()
        result[relative] = (
            metadata.st_mode, metadata.st_dev, metadata.st_ino,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns, payload,
        )
    return result


def legacy_source(tmp_path, *, document=DOCUMENT, changes=None):
    source = tmp_path / "source"
    knowledge = source / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "neutral.md").write_text(document, encoding="utf-8")
    entries = _entries_for("neutral", document)
    legacy = entries[0].model_copy(update={"id": str(uuid4()), **(changes or {})})
    write_legacy_rows(source, [legacy])
    return source, entries, legacy


def write_legacy_rows(source, entries):
    database = source / "data" / "vectors.db"
    _write_vector_db(database, entries)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "UPDATE vectors SET text = ? WHERE id = ?",
            [(entry.content, entry.id) for entry in entries],
        )


def set_metadata(source, row_id, metadata):
    from layered_memory_mcp.storage.vector_store import VectorStore

    with VectorStore(source / "data" / "vectors.db") as store:
        store.patch_metadata({row_id: metadata}, expected=store.snapshot())


def explicit_map(tmp_path, mapping):
    path = tmp_path / "identity-map.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def test_exact_declared_section_maps_random_legacy_identity(tmp_path):
    source, entries, legacy = legacy_source(tmp_path)

    snapshot = read_source_snapshot(source)

    assert snapshot.publishable, snapshot.inventory.findings
    assert snapshot.inventory.counts_balance
    assert snapshot.inventory.counts_by_category["mapped"] == 1
    verdict = snapshot.inventory.to_payload()["classifications"][0]
    assert verdict["row_id"] == legacy.id
    assert verdict["reason"] == "mapped_by_exact_identity"
    assert verdict["mapping_origin"] == "automatic"
    assert verdict["candidate_count"] == 1
    assert verdict["target_entry_id"] == entries[0].id
    expected = graph_node_id_for_entry_id(entries[0].id)
    assert verdict["expected_graph_node_id"] == expected
    assert snapshot.identity_map == {}
    assert snapshot.automatic_identity_map == {legacy.id: entries[0].id}
    node_id, metadata = snapshot.identity_map_updates[legacy.id]
    assert node_id == metadata["graph_node_id"] == expected
    assert metadata["section"] == entries[0].section


@pytest.mark.parametrize("section", [
    "Neutral section",
    "Neutral section — 2026-10-01 v1.2 (neutral note)",
    "2026-10-01",
])
def test_legacy_embedding_projection_maps_with_distinct_automatic_reason(tmp_path, section):
    document = f"## {section}\n\nA neutral café sample.\n"
    source, entries, legacy = legacy_source(
        tmp_path, document=document, changes={"id": "legacy-projection"}
    )
    candidate = entries[0]
    projected = f"{embedding_heading(candidate.section)}\n{candidate.content}"
    write_legacy_rows(source, [legacy.model_copy(update={"content": projected})])
    metadata = {"section": candidate.section, "unrelated": "kept"}
    set_metadata(source, legacy.id, metadata)
    before = tree_state(source)

    snapshot = read_source_snapshot(source)

    assert tree_state(source) == before
    assert snapshot.inventory.counts_balance
    verdict = snapshot.inventory.to_payload()["classifications"][0]
    assert verdict["category"] == "mapped"
    assert snapshot.publishable, snapshot.inventory.findings
    assert snapshot.inventory.counts_by_category["mapped"] == 1
    assert verdict["row_id"] == legacy.id
    assert verdict["mapping_origin"] == "automatic"
    assert verdict["candidate_count"] == 1
    assert verdict["target_entry_id"] == candidate.id
    assert verdict["reason"] != "mapped_by_exact_identity"
    assert any(
        label in verdict["reason"] for label in ("embedding", "projection", "compatibility")
    )
    expected = graph_node_id_for_entry_id(candidate.id)
    assert verdict["expected_graph_node_id"] == expected
    assert snapshot.identity_map == {}
    assert snapshot.automatic_identity_map == {legacy.id: candidate.id}
    assert snapshot.identity_map_updates == {
        legacy.id: (expected, {**metadata, "graph_node_id": expected})
    }


@pytest.mark.parametrize("variant", [
    "arbitrary_prefix",
    "altered_body",
    "trailing_byte",
    "body_whitespace",
    "heading_whitespace",
    "unicode_normalized_body",
    "wrong_section_prefix",
])
def test_legacy_embedding_projection_rejects_nonexact_text(tmp_path, variant):
    source, entries, legacy = legacy_source(
        tmp_path, changes={"id": "legacy-projection"}
    )
    candidate = entries[0]
    prefix = embedding_heading(candidate.section)
    body = candidate.content
    projected = f"{prefix}\n{body}"
    texts = {
        "arbitrary_prefix": f"Arbitrary prefix\n{body}",
        "altered_body": f"{prefix}\nAn altered neutral café sample.",
        "trailing_byte": projected + "\n",
        "body_whitespace": f"{prefix}\n {body}",
        "heading_whitespace": f"{prefix} \n{body}",
        "unicode_normalized_body": f"{prefix}\n{unicodedata.normalize('NFD', body)}",
        "wrong_section_prefix": f"{embedding_heading('Wrong section')}\n{body}",
    }
    stored_text = texts[variant]
    assert stored_text.encode("utf-8") != projected.encode("utf-8")
    write_legacy_rows(source, [legacy.model_copy(update={"content": stored_text})])
    before = tree_state(source)

    snapshot = read_source_snapshot(source)

    assert tree_state(source) == before
    assert not snapshot.publishable
    assert snapshot.inventory.counts_balance
    assert snapshot.inventory.counts_by_category["mapped"] == 0
    verdict = snapshot.inventory.classifications[0]
    assert verdict.category == "orphaned"
    assert verdict.reason == "identity_text_mismatch"
    assert verdict.candidate_count == 1
    assert verdict.mapping_origin == "automatic"
    assert snapshot.identity_map == {}
    assert snapshot.automatic_identity_map == {}
    assert snapshot.identity_map_updates == {}


@pytest.mark.parametrize("domain, metadata, duplicate, reason, count", [
    ("elsewhere", {"section": "Neutral section"}, False, "identity_section_not_found", 0),
    ("Neutral", {"section": "Neutral section"}, False, "identity_section_not_found", 0),
    ("neutral", {}, False, "identity_section_missing", 0),
    ("neutral", {"section": "Wrong section"}, False, "identity_section_not_found", 0),
    ("neutral", {"section": "Neutral section"}, True, "identity_section_ambiguous", 2),
])
def test_legacy_embedding_projection_requires_unique_domain_and_declared_section(
    tmp_path, domain, metadata, duplicate, reason, count
):
    document = DOCUMENT
    if duplicate:
        document += "\n## Neutral section\n\nA different neutral body.\n"
    source, entries, legacy = legacy_source(
        tmp_path, document=document, changes={"id": "legacy-projection", "domain": domain}
    )
    candidate = entries[0]
    projected = f"{embedding_heading(candidate.section)}\n{candidate.content}"
    write_legacy_rows(source, [legacy.model_copy(update={"content": projected})])
    set_metadata(source, legacy.id, metadata)
    before = tree_state(source)

    snapshot = read_source_snapshot(source)

    assert tree_state(source) == before
    assert not snapshot.publishable
    assert snapshot.inventory.counts_balance
    assert snapshot.inventory.counts_by_category["mapped"] == 0
    verdict = snapshot.inventory.classifications[0]
    assert verdict.category == "orphaned"
    assert verdict.reason == reason
    assert verdict.candidate_count == count
    assert verdict.mapping_origin == "automatic"
    assert snapshot.identity_map == {}
    assert snapshot.automatic_identity_map == {}
    assert snapshot.identity_map_updates == {}


@pytest.mark.parametrize("stored, category", [
    ({"graph_node_id": None}, "malformed"),
    ({"graph_node_id": "claim:other"}, None),
    ({"graph_node_ids": ["claim:other"]}, None),
])
def test_legacy_embedding_projection_preserves_stored_graph_safeguards(
    tmp_path, stored, category
):
    source, entries, legacy = legacy_source(
        tmp_path, changes={"id": "legacy-projection"}
    )
    candidate = entries[0]
    projected = f"{embedding_heading(candidate.section)}\n{candidate.content}"
    write_legacy_rows(source, [legacy.model_copy(update={"content": projected})])
    set_metadata(source, legacy.id, {"section": candidate.section, **stored})
    before = tree_state(source)

    snapshot = read_source_snapshot(source)

    assert tree_state(source) == before
    assert not snapshot.publishable
    assert snapshot.inventory.counts_balance
    assert snapshot.inventory.counts_by_category["mapped"] == 0
    verdict = snapshot.inventory.classifications[0]
    assert verdict.category != "mapped"
    if category is not None:
        assert verdict.category == category
    assert snapshot.identity_map == {}
    assert snapshot.automatic_identity_map == {}
    assert snapshot.identity_map_updates == {}


def test_duplicate_automatic_proposals_both_remain_unresolved(tmp_path):
    source, entries, legacy = legacy_source(tmp_path)
    other = legacy.model_copy(update={"id": str(uuid4())})
    write_legacy_rows(source, [legacy, other])

    snapshot = read_source_snapshot(source)

    assert not snapshot.publishable
    assert snapshot.inventory.counts_balance
    assert snapshot.inventory.counts_by_category["conflicting"] == 2
    assert snapshot.identity_map_updates == {}
    assert snapshot.automatic_identity_map == {}
    for verdict in snapshot.inventory.classifications:
        assert verdict.reason == "identity_target_contested"
        assert verdict.candidate_count == 1
        assert verdict.mapping_origin == "automatic"
        assert verdict.target_entry_id == entries[0].id


@pytest.mark.parametrize("stored, category", [
    ({"graph_node_id": None}, "malformed"),
    ({"graph_node_id": 7}, "malformed"),
    ({"graph_node_id": " padded "}, "malformed"),
    ({"graph_node_ids": "claim:other"}, "malformed"),
    ({"graph_node_ids": [False]}, "malformed"),
    ({"graph_node_id": "claim:other"}, "conflicting"),
    ({"graph_node_ids": ["claim:other"]}, "conflicting"),
])
def test_explicit_map_never_overwrites_bad_stored_graph_fields(tmp_path, stored, category):
    source, entries, legacy = legacy_source(tmp_path)
    set_metadata(source, legacy.id, {"section": legacy.section, **stored})
    mapping = explicit_map(tmp_path, {legacy.id: entries[0].id})

    snapshot = read_source_snapshot(source, identity_map_path=mapping)

    assert not snapshot.publishable
    assert snapshot.inventory.classifications[0].category == category
    assert snapshot.identity_map_updates == {}
    assert snapshot.identity_map == {}
    assert snapshot.automatic_identity_map == {}


@pytest.mark.parametrize("embedding_projection", [False, True])
def test_end_to_end_origins_preserved_payloads_noop_and_retained_rollback(
    tmp_path, embedding_projection
):
    document = DOCUMENT + "\n## Second section\n\nAnother neutral sample.\n"
    source, entries, automatic = legacy_source(tmp_path, document=document)
    if embedding_projection:
        automatic = automatic.model_copy(update={
            "content": f"{embedding_heading(automatic.section)}\n{automatic.content}"
        })
    declared = entries[1].model_copy(update={
        "id": str(uuid4()), "content": "An operator-declared older sample."
    })
    write_legacy_rows(source, [automatic, declared])
    set_metadata(source, automatic.id, {
        "section": automatic.section, "unrelated": {"neutral": [1, True, "kept"]}
    })
    set_metadata(source, declared.id, {"unrelated": "also-kept"})
    mapping = explicit_map(tmp_path, {declared.id: entries[1].id})
    destination = tmp_path / "destination"
    before_rows = read_vector_rows(source / "data" / "vectors.db")
    before_tree = tree_state(source)

    planned = graph_migration.plan_migration(
        source=source, destination=destination, identity_map=mapping
    )

    assert planned["status"] == graph_migration.STATUS_PLANNED, planned
    assert tree_state(source) == before_tree
    assert not destination.exists()
    assert planned["inventory"]["automatic_identity_map_entries"] == 1
    assert planned["inventory"]["identity_map_entries"] == 1
    automatic_verdict = next(
        row for row in planned["inventory"]["classifications"]
        if row["row_id"] == automatic.id
    )
    assert automatic_verdict["reason"] == (
        "mapped_by_exact_embedding_projection" if embedding_projection
        else "mapped_by_exact_identity"
    )
    applied = graph_migration.apply_migration(
        source=source, destination=destination, identity_map=mapping
    )
    assert applied["status"] == graph_migration.STATUS_APPLIED, applied
    assert tree_state(source) == before_tree
    target = generation(destination)
    manifest, findings = generation_module.read_manifest(target)
    assert not findings
    assert manifest["identity_map"] == {declared.id: entries[1].id}
    assert manifest["automatic_identity_map"] == {automatic.id: entries[0].id}
    assert manifest["provenance"]["identity_bridge"] == {
        "version": 1, "rows": planned["inventory"]["classifications"]
    }
    published = read_vector_rows(target / "data" / "vectors.db")
    assert [row[:4] for row in published] == [row[:4] for row in before_rows]
    for original, updated in zip(before_rows, published):
        expected = json.loads(original[4])
        expected["graph_node_id"] = manifest["seeds"][original[0]]
        assert json.loads(updated[4]) == expected
    assert read_vector_rows(target / "backup" / "data" / "vectors.db") == before_rows
    verified = graph_migration.verify_migration(destination=destination, source=source)
    assert verified["status"] == graph_migration.STATUS_VERIFIED, verified
    before_repeat = tree_state(destination)
    repeated = graph_migration.apply_migration(
        source=source, destination=destination, identity_map=mapping
    )
    assert repeated["status"] == graph_migration.STATUS_ALREADY_PUBLISHED, repeated
    assert tree_state(destination) == before_repeat
    rolled_back = graph_migration.rollback_migration(destination=destination)
    assert rolled_back["status"] == graph_migration.STATUS_ROLLED_BACK, rolled_back
    assert not target.exists()
    retained = list((destination / generation_module.ROLLBACK_DIR).iterdir())
    assert len(retained) == 1
    assert read_vector_rows(retained[0] / "backup" / "data" / "vectors.db") == before_rows
    assert tree_state(source) == before_tree


@pytest.mark.parametrize("embedding_projection", [False, True])
@pytest.mark.parametrize("forgery", [
    "origin", "candidate_count", "automatic_target", "explicit_collision",
    "missing_provenance", "missing_automatic_map", "unrelated_metadata", "swapped_seeds",
    "malformed_metadata", "category_counts", "malformed_seed", "mapping_reason",
])
def test_verification_rederives_identity_evidence_after_manifest_rehash(
    tmp_path, forgery, embedding_projection
):
    document = DOCUMENT + "\n## Second section\n\nAnother neutral sample.\n"
    source, entries, first = legacy_source(tmp_path, document=document)
    if embedding_projection:
        first = first.model_copy(update={
            "content": f"{embedding_heading(first.section)}\n{first.content}"
        })
    second = entries[1].model_copy(update={"id": str(uuid4())})
    write_legacy_rows(source, [first, second])
    destination = tmp_path / "destination"
    applied = graph_migration.apply_migration(source=source, destination=destination)
    assert applied["status"] == graph_migration.STATUS_APPLIED, applied
    target = generation(destination)
    manifest, findings = generation_module.read_manifest(target)
    assert not findings
    if forgery == "origin":
        manifest["provenance"]["identity_bridge"]["rows"][0]["mapping_origin"] = "explicit"
    elif forgery == "mapping_reason":
        verdict = next(
            row for row in manifest["provenance"]["identity_bridge"]["rows"]
            if row["row_id"] == first.id
        )
        verdict["reason"] = (
            "mapped_by_exact_identity" if embedding_projection
            else "mapped_by_exact_embedding_projection"
        )
    elif forgery == "candidate_count":
        manifest["provenance"]["identity_bridge"]["rows"][0]["candidate_count"] = 2
    elif forgery == "automatic_target":
        manifest["automatic_identity_map"][first.id] = entries[1].id
    elif forgery == "explicit_collision":
        manifest["identity_map"] = {first.id: entries[0].id, second.id: entries[0].id}
    elif forgery == "missing_provenance":
        del manifest["provenance"]["identity_bridge"]
    elif forgery == "missing_automatic_map":
        del manifest["automatic_identity_map"]
    elif forgery == "unrelated_metadata":
        set_metadata(target, first.id, {"graph_node_id": manifest["seeds"][first.id]})
        manifest["artifacts"], findings = generation_module.generation_inventory(target)
        assert not findings
    elif forgery == "swapped_seeds":
        manifest["seeds"][first.id], manifest["seeds"][second.id] = (
            manifest["seeds"][second.id], manifest["seeds"][first.id]
        )
        for row_id in (first.id, second.id):
            set_metadata(target, row_id, {"graph_node_id": manifest["seeds"][row_id]})
        manifest["artifacts"], findings = generation_module.generation_inventory(target)
        assert not findings
    elif forgery == "malformed_metadata":
        with sqlite3.connect(target / "data" / "vectors.db") as connection:
            connection.execute("UPDATE vectors SET metadata = '[]' WHERE id = ?", (first.id,))
        manifest["artifacts"], findings = generation_module.generation_inventory(target)
        assert not findings
    elif forgery == "category_counts":
        manifest["counts"]["rows_by_category"]["mapped"] = 0
    elif forgery == "malformed_seed":
        manifest["seeds"][first.id] = [manifest["seeds"][first.id]]
        set_metadata(target, first.id, {"graph_node_id": manifest["seeds"][first.id]})
        manifest["artifacts"], findings = generation_module.generation_inventory(target)
        assert not findings
    generation_module.write_manifest(target, manifest)

    verified = generation_module.verify_generation(target)

    assert not verified.generation_intact
    assert not verified.vector_ok
    assert any(finding.startswith("identity_bridge_") for finding in verified.findings)


@pytest.mark.parametrize("changes, metadata, reason, count", [
    ({"domain": "elsewhere"}, {"section": "Neutral section"}, "identity_section_not_found", 0),
    ({"domain": "Neutral"}, {"section": "Neutral section"}, "identity_section_not_found", 0),
    ({}, {}, "identity_section_missing", 0),
    ({}, {"section": None}, "identity_section_missing", 0),
    ({}, {"section": ["Neutral section"]}, "identity_section_missing", 0),
    ({}, {"section": 1}, "identity_section_missing", 0),
    ({}, {"section": "Wrong section"}, "identity_section_not_found", 0),
    ({}, {"section": "Neutral section "}, "identity_section_not_found", 0),
    ({}, {"section": "neutral section"}, "identity_section_not_found", 0),
    ({"content": " A neutral café sample."}, {"section": "Neutral section"}, "identity_text_mismatch", 1),
    ({"content": "A neutral café sample.\n"}, {"section": "Neutral section"}, "identity_text_mismatch", 1),
    ({"content": "A neutral cafe\u0301 sample."}, {"section": "Neutral section"}, "identity_text_mismatch", 1),
    ({"content": "Arbitrary prefix\nA neutral café sample."}, {"section": "Neutral section"}, "identity_text_mismatch", 1),
])
def test_automatic_correspondence_is_exact_and_requires_declared_section(
    tmp_path, changes, metadata, reason, count
):
    source, _, legacy = legacy_source(tmp_path, changes=changes)
    set_metadata(source, legacy.id, metadata)
    before = tree_state(source)

    report = graph_migration.apply_migration(source=source, destination=tmp_path / "destination")

    assert report["status"] == graph_migration.STATUS_BLOCKED, report
    assert report["inventory"]["counts_balance"]
    verdict = report["inventory"]["classifications"][0]
    assert verdict["reason"] == reason
    assert verdict["candidate_count"] == count
    assert verdict["mapping_origin"] == "automatic"
    assert tree_state(source) == before
    assert not (tmp_path / "destination").exists()


def test_duplicate_headings_are_ambiguous_even_with_one_exact_body(tmp_path):
    document = DOCUMENT + "\n## Neutral section\n\nA different neutral body.\n"
    source, entries, _ = legacy_source(tmp_path, document=document)

    snapshot = read_source_snapshot(source)

    assert len(entries) == 2
    assert not snapshot.publishable
    assert snapshot.inventory.counts_balance
    verdict = snapshot.inventory.classifications[0]
    assert verdict.reason == "identity_section_ambiguous"
    assert verdict.candidate_count == 2
    assert snapshot.identity_map_updates == {}


@pytest.mark.parametrize("explicit", [False, True])
def test_validated_claims_reserve_targets_before_automatic_proposals(tmp_path, explicit):
    source, entries, automatic = legacy_source(tmp_path)
    occupied = entries[0].model_copy(update={"id": str(uuid4())}) if explicit else entries[0]
    write_legacy_rows(source, [automatic, occupied])
    mapping = explicit_map(tmp_path, {occupied.id: entries[0].id}) if explicit else None

    snapshot = read_source_snapshot(source, identity_map_path=mapping)

    assert not snapshot.publishable
    assert snapshot.inventory.counts_balance
    verdicts = {row.row_id: row for row in snapshot.inventory.classifications}
    assert verdicts[automatic.id].reason == "identity_target_reserved"
    assert verdicts[occupied.id].mapping_origin == ("explicit" if explicit else "row_id")
    assert automatic.id not in snapshot.identity_map_updates
    assert snapshot.automatic_identity_map == {}


def test_explicit_declaration_takes_precedence_without_silently_repointing(tmp_path):
    document = DOCUMENT + "\n## Second section\n\nAnother neutral sample.\n"
    source, entries, declared = legacy_source(tmp_path, document=document)
    automatic = declared.model_copy(update={"id": str(uuid4())})
    write_legacy_rows(source, [declared, automatic])
    mapping = explicit_map(tmp_path, {declared.id: entries[1].id})

    snapshot = read_source_snapshot(source, identity_map_path=mapping)

    assert snapshot.publishable, snapshot.inventory.findings
    assert snapshot.identity_map == {declared.id: entries[1].id}
    assert snapshot.automatic_identity_map == {automatic.id: entries[0].id}
    assert snapshot.inventory.counts_by_category["mapped"] == 2


def test_invalid_explicit_graph_claim_does_not_reserve_an_automatic_target(tmp_path):
    source, entries, declared = legacy_source(tmp_path)
    automatic = declared.model_copy(update={"id": str(uuid4())})
    write_legacy_rows(source, [declared, automatic])
    set_metadata(source, declared.id, {"graph_node_id": "claim:unrelated"})
    mapping = explicit_map(tmp_path, {declared.id: entries[0].id})

    snapshot = read_source_snapshot(source, identity_map_path=mapping)

    assert not snapshot.publishable
    assert snapshot.identity_map == {}
    assert snapshot.automatic_identity_map == {automatic.id: entries[0].id}
    assert set(snapshot.identity_map_updates) == {automatic.id}


@pytest.mark.parametrize("stored, category", [
    ({"graph_node_id": ""}, "malformed"),
    ({"graph_node_id": "claim:one\nclaim:two"}, "malformed"),
    ({"graph_node_ids": None}, "malformed"),
    ({"graph_node_ids": [" claim:other "]}, "malformed"),
    ({"graph_node_ids": [None]}, "malformed"),
    ({"graph_node_id": "claim:other"}, "conflicting"),
    ({"graph_node_id": "claim:one", "graph_node_ids": ["claim:two"]}, "conflicting"),
    ({"graph_node_ids": ["claim:one", "claim:two"]}, "conflicting"),
])
def test_automatic_mapping_validates_all_stored_graph_fields(tmp_path, stored, category):
    source, _, legacy = legacy_source(tmp_path)
    set_metadata(source, legacy.id, {"section": legacy.section, **stored})

    snapshot = read_source_snapshot(source)

    assert not snapshot.publishable
    assert snapshot.inventory.classifications[0].category == category
    assert snapshot.identity_map_updates == {}


@pytest.mark.parametrize("stored", ["scalar", "list", "both", "empty_list"])
def test_matching_stored_graph_fields_are_preserved(tmp_path, stored):
    source, entries, legacy = legacy_source(tmp_path)
    expected = graph_node_id_for_entry_id(entries[0].id)
    metadata = {"section": legacy.section, "unrelated": "kept"}
    if stored in ("scalar", "both"):
        metadata["graph_node_id"] = expected
    if stored in ("list", "both"):
        metadata["graph_node_ids"] = [expected, expected]
    if stored == "empty_list":
        metadata["graph_node_ids"] = []
    set_metadata(source, legacy.id, metadata)

    snapshot = read_source_snapshot(source)

    assert snapshot.publishable, snapshot.inventory.findings
    assert snapshot.identity_map_updates[legacy.id][1] == {
        **metadata, "graph_node_id": expected
    }


@pytest.mark.parametrize("column, payload, category", [
    ("metadata", "{broken", "malformed"),
    ("metadata", "[]", "malformed"),
    ("metadata", "null", "malformed"),
    ("metadata", b"\xff", "malformed"),
    ("vector", "", "missing"),
    ("vector", "not-json", "missing"),
    ("vector", "{}", "missing"),
    ("vector", "[1, 2]", "missing"),
    ("vector", "[NaN]", "missing"),
    ("vector", "[true]", "missing"),
    ("vector", b"\xff", "missing"),
    ("text", b"\xff", "malformed"),
    ("domain", b"neutral", "malformed"),
])
@pytest.mark.parametrize("explicit", [False, True])
def test_unusable_orphan_payloads_are_accounted_and_never_adapted(
    tmp_path, column, payload, category, explicit
):
    source, entries, legacy = legacy_source(tmp_path)
    assert column in {"metadata", "vector", "text", "domain"}
    with sqlite3.connect(source / "data" / "vectors.db") as connection:
        connection.execute(f"UPDATE vectors SET {column} = ? WHERE id = ?", (payload, legacy.id))
    mapping = explicit_map(tmp_path, {legacy.id: entries[0].id}) if explicit else None
    before = tree_state(source)

    snapshot = read_source_snapshot(source, identity_map_path=mapping)

    assert not snapshot.publishable
    assert snapshot.inventory.counts_balance
    assert snapshot.inventory.vector_rows == 1
    assert snapshot.inventory.classifications[0].category == category
    assert snapshot.identity_map_updates == {}
    assert tree_state(source) == before


def test_plan_is_recomputed_after_source_drift_and_reports_no_content(tmp_path):
    source, entries, legacy = legacy_source(tmp_path)
    secret = "neutral-private-metadata-marker"
    set_metadata(source, legacy.id, {"section": legacy.section, "private": secret})
    destination = tmp_path / "destination"
    before = tree_state(source)
    planned = graph_migration.plan_migration(source=source, destination=destination)
    assert planned["status"] == graph_migration.STATUS_PLANNED, planned
    assert tree_state(source) == before
    assert not destination.exists()
    with sqlite3.connect(source / "data" / "vectors.db") as connection:
        connection.execute("UPDATE vectors SET text = text || ' '")
    changed = tree_state(source)

    applied = graph_migration.apply_migration(source=source, destination=destination)

    assert applied["status"] == graph_migration.STATUS_BLOCKED, applied
    assert applied["inventory"]["source_fingerprint"] != planned["inventory"]["source_fingerprint"]
    assert applied["inventory"]["classifications"][0]["reason"] == "identity_text_mismatch"
    assert tree_state(source) == changed
    assert not destination.exists()
    for report in (planned, applied):
        serialized = json.dumps(report, ensure_ascii=False)
        assert entries[0].section not in serialized
        assert entries[0].content not in serialized
        assert secret not in serialized


def test_source_drift_during_automatic_staging_prevents_publication(tmp_path, monkeypatch):
    source, _, _ = legacy_source(tmp_path)
    destination = tmp_path / "destination"

    def drift(snapshot, staging):
        document = source / "knowledge" / "neutral.md"
        document.write_text(DOCUMENT + "\nA new neutral line.\n", encoding="utf-8")

    monkeypatch.setattr(graph_migration, "BEFORE_PUBLISH_HOOK", drift)

    report = graph_migration.apply_migration(source=source, destination=destination)

    assert report["status"] == graph_migration.STATUS_SOURCE_DRIFT, report
    assert not generation(destination).exists()


def test_checkpointed_wal_source_keeps_zero_write_identity_reading(tmp_path):
    source, _, _ = legacy_source(tmp_path)
    database = source / "data" / "vectors.db"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    assert not database.with_name("vectors.db-wal").exists()
    before = tree_state(source)

    snapshot = read_source_snapshot(source)

    assert snapshot.publishable, snapshot.inventory.findings
    assert tree_state(source) == before


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_identity_readers_use_portable_immutable_sqlite_and_ordinary_copy(
    tmp_path, monkeypatch, journal_mode
):
    source, entries, legacy = legacy_source(tmp_path)
    database = source / "data" / "vectors.db"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            f"PRAGMA journal_mode = {journal_mode}"
        ).fetchone()[0] == journal_mode
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    assert not database.with_name("vectors.db-wal").exists()
    (source / "data" / "memory_changelog.jsonl").write_text(
        _changelog_line(
            event_id="neutral-portable-event", domain="neutral", section=legacy.section
        ) + "\n",
        encoding="utf-8",
    )
    mapping = explicit_map(tmp_path, {legacy.id: entries[0].id})
    before = tree_state(source)
    before_map = tree_state(mapping)
    original_bytes = database.read_bytes()
    original_connect = sqlite3.connect
    opened = []

    def connect_readonly(database_uri, **options):
        assert database_uri == f"{database.resolve().as_uri()}?mode=ro&immutable=1"
        assert options == {"uri": True}
        opened.append(database_uri)
        return ReadOnlyConnection(original_connect(database_uri, **options))

    monkeypatch.delattr(os, "O_NOATIME", raising=False)
    monkeypatch.delattr(os, "memfd_create", raising=False)
    monkeypatch.delattr(os, "MFD_CLOEXEC", raising=False)
    monkeypatch.setattr(sqlite3, "connect", connect_readonly)

    snapshot = read_source_snapshot(source, identity_map_path=mapping)
    copied = tmp_path / "copied" / "vectors.db"
    generation_module._copy_file(database, copied)

    assert snapshot.publishable, snapshot.inventory.findings
    assert snapshot.identity_map == {legacy.id: entries[0].id}
    assert snapshot.vector_rows[0][0] == legacy.id
    assert [event.event_id for event in snapshot.changelog_events] == [
        "neutral-portable-event"
    ]
    assert len(opened) == 2
    assert copied.read_bytes() == original_bytes
    assert tree_state(source) == before
    assert tree_state(mapping) == before_map


def test_an_unusable_canonical_identity_cannot_be_adapted(tmp_path):
    document = (
        '---\nid: "invalid\\nidentity"\nsection: Neutral section\n'
        'domain: neutral\n---\n\n## Neutral section\n\nA neutral sample.\n'
    )
    source, _, _ = legacy_source(tmp_path, document=document)

    snapshot = read_source_snapshot(source)

    assert not snapshot.publishable
    assert snapshot.inventory.counts_balance
    assert snapshot.identity_map_updates == {}

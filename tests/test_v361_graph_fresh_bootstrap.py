"""Fresh graph initialisation, and the legacy data that must block it.

The default rollout mode is ``graph``. The failure this file exists to prevent
is the tempting shortcut: a service with no generation quietly getting an empty
one, while the Markdown and vector rows it should have migrated sit untouched
on disk and simply stop being visible.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from layered_memory_mcp import graph_bootstrap, graph_home_binding, graph_migration
from layered_memory_mcp.graph_journal import PROJECTION_NAMES
from layered_memory_mcp.graph_migration_generation import (
    GENERATION_TYPE_FRESH,
    GENERATION_TYPE_MIGRATION,
    read_manifest,
    verify_generation,
)
from test_v361_graph_migration import build_source, fake_embeddings, tree_state


@pytest.fixture(autouse=True)
def _no_environment_overrides(monkeypatch):
    for name in graph_home_binding.OVERRIDE_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def paths(tmp_path):
    home = tmp_path / "service"
    home.mkdir()
    control = tmp_path / "control"
    control.mkdir()
    return home, control / "graph", control / "active.json"


# ---------------------------------------------------------------------------
# plan: zero writes
# ---------------------------------------------------------------------------

def test_plan_creates_nothing_anywhere(paths, tmp_path):
    home, destination, pointer = paths
    before = tree_state(tmp_path)
    report = graph_bootstrap.plan_bootstrap_fresh(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_PLANNED
    assert report["exit_code"] == 0
    assert report["dry_run"] is True
    assert report["generation_type"] == GENERATION_TYPE_FRESH
    assert report["findings"] == []
    # The pointer cannot exist yet, so only the pointer-independent half of the
    # binding is decided — and it says so rather than claiming a prepared one.
    assert report["binding"]["status"] == "home_binding_pending_activation"
    assert report["binding"]["writes"] is False
    assert not destination.exists()
    assert not pointer.exists()
    assert tree_state(tmp_path) == before


def test_plan_is_deterministic(paths):
    home, destination, pointer = paths
    first = graph_bootstrap.plan_bootstrap_fresh(
        home=home, destination=destination, pointer=pointer
    )
    second = graph_bootstrap.plan_bootstrap_fresh(
        home=home, destination=destination, pointer=pointer
    )
    assert first == second


# ---------------------------------------------------------------------------
# legacy data blocks a fresh start
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relative", [
    "L0.md", "graph.db", "journal.db", "data/vectors.db",
    "knowledge/existing.md", "knowledge/nested/existing.md",
])
def test_existing_legacy_memory_blocks_a_fresh_bootstrap(paths, tmp_path, relative):
    home, destination, pointer = paths
    legacy = home / relative
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("## Existing\n\nneutral legacy body\n", encoding="utf-8")
    before = tree_state(tmp_path)
    report = graph_bootstrap.plan_bootstrap_fresh(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_BLOCKED
    assert report["exit_code"] == 1
    assert f"{graph_bootstrap.FINDING_LEGACY_PRESENT}:{relative}" in report["findings"]
    assert "neutral legacy body" not in json.dumps(report)
    assert not destination.exists()
    assert tree_state(tmp_path) == before
    # And apply refuses identically: there is no --force.
    applied = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert applied["status"] == graph_bootstrap.STATUS_BLOCKED
    assert tree_state(tmp_path) == before


def test_every_legacy_input_is_named_not_just_the_first(paths):
    home, destination, pointer = paths
    (home / "knowledge").mkdir()
    (home / "knowledge" / "alpha.md").write_text("## A\n\nbody\n", encoding="utf-8")
    (home / "data").mkdir()
    (home / "data" / "vectors.db").write_bytes(b"neutral")
    (home / "L0.md").write_text("index\n", encoding="utf-8")
    report = graph_bootstrap.plan_bootstrap_fresh(
        home=home, destination=destination, pointer=pointer
    )
    assert sorted(report["findings"]) == [
        f"{graph_bootstrap.FINDING_LEGACY_PRESENT}:L0.md",
        f"{graph_bootstrap.FINDING_LEGACY_PRESENT}:data/vectors.db",
        f"{graph_bootstrap.FINDING_LEGACY_PRESENT}:knowledge/alpha.md",
    ]


def test_an_empty_knowledge_directory_does_not_block(paths):
    """A home that was created but never written to is still fresh."""
    home, destination, pointer = paths
    (home / "knowledge" / "shared").mkdir(parents=True)
    (home / "data").mkdir()
    report = graph_bootstrap.plan_bootstrap_fresh(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_PLANNED


@pytest.mark.parametrize("kind", ["absent", "file", "overlap_destination", "overlap_control"])
def test_an_unusable_home_or_overlapping_path_is_refused(paths, tmp_path, kind):
    home, destination, pointer = paths
    if kind == "absent":
        home = tmp_path / "missing"
        expected = graph_bootstrap.FINDING_HOME_ABSENT
    elif kind == "file":
        home = tmp_path / "file-home"
        home.write_text("neutral", encoding="utf-8")
        expected = graph_bootstrap.FINDING_HOME_ABSENT  # also not a directory
    elif kind == "overlap_destination":
        destination = home / "inside"
        expected = f"{graph_bootstrap.FINDING_PATH_OVERLAP}:home_destination"
    else:
        pointer = home / "active.json"
        expected = f"{graph_bootstrap.FINDING_PATH_OVERLAP}:home_control"
    report = graph_bootstrap.plan_bootstrap_fresh(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_BLOCKED
    if kind == "file":
        assert graph_bootstrap.FINDING_HOME_NOT_A_DIRECTORY in json.dumps(report)
    else:
        assert expected in report["findings"]
    assert not destination.exists()


# ---------------------------------------------------------------------------
# apply: an empty generation, published, activated and bound
# ---------------------------------------------------------------------------

def test_apply_publishes_a_verified_empty_generation_and_binds_the_home(paths):
    home, destination, pointer = paths
    (home / "config.yaml").write_text(
        "# operator note\ndedup_threshold: 0.42\n", encoding="utf-8"
    )
    report = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_APPLIED, report["findings"]
    assert report["exit_code"] == 0
    assert report["findings"] == []

    generation = destination / "current"
    assert sorted(path.name for path in generation.iterdir()) == [
        "L0.md", "data", "graph.db", "journal.db", "knowledge",
        "manifest.json", "manifest.sha256",
    ]
    # No backup directory: a fresh generation has no source to freeze, and an
    # empty one would be a provenance claim with nothing behind it.
    assert not (generation / "backup").exists()
    assert list((generation / "knowledge").iterdir()) == []

    manifest, findings = read_manifest(generation)
    assert findings == []
    assert manifest["generation_type"] == GENERATION_TYPE_FRESH
    assert manifest["source"] is None
    assert manifest["source_fingerprint"] is None
    assert manifest["source_files"] == {}
    assert manifest["seeds"] == {}
    assert manifest["surfaces"] == {}
    assert manifest["journal"] == {
        "request_id": "", "request_kind": None, "events": 0, "head_position": 0,
        "request_hash": manifest["journal"]["request_hash"],
    }
    # Every managed projection, each at zero — a fresh generation's complete
    # authority. Read from the constant so a projection added later (``todo``,
    # v3.6.1) is still asserted to be declared and still asserted to be empty,
    # rather than dropping out of the check.
    assert manifest["checkpoints"] == {name: 0 for name in PROJECTION_NAMES}
    assert manifest["counts"]["graph_nodes"] == 0
    assert manifest["counts"]["vector_rows"] == 0
    assert manifest["counts"]["l1_entries"] == 0
    assert manifest["provenance"]["recorded_at"] is None

    assert report["verification"]["ok"] is True
    assert report["activation"]["status"] == "generation_cutover_applied"
    assert report["runtime"]["status"] == "generation_runtime_active"
    assert report["binding"]["status"] == graph_home_binding.STATUS_APPLIED
    assert report["effective_binding"]["status"] == graph_home_binding.STATUS_VERIFIED
    effective = report["effective_binding"]["effective"]
    assert effective["graph_mode"] == "graph"
    assert effective["graph_active_pointer"] == str(pointer)
    assert effective["knowledge_dir"] == str(generation / "knowledge")
    assert effective["vector_db_path"] == str(generation / "data" / "vectors.db")

    configuration = (home / "config.yaml").read_text(encoding="utf-8")
    assert "# operator note" in configuration
    assert "dedup_threshold: 0.42" in configuration
    assert f"active_pointer: {pointer}" in configuration


def test_an_applied_fresh_generation_verifies_independently(paths):
    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    verification = verify_generation(destination / "current")
    assert verification.ok, verification.findings
    assert verification.journal_ok
    assert verification.graph_ok
    assert verification.replay_equal
    assert verification.vector_ok
    assert verification.source_files_ok
    assert verification.counts_ok


def test_a_repeat_apply_is_a_no_op_that_republishes_nothing(paths):
    home, destination, pointer = paths
    first = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert first["status"] == graph_bootstrap.STATUS_APPLIED
    before = tree_state(destination)
    home_before = tree_state(home)
    repeat = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert repeat["status"] == graph_bootstrap.STATUS_PUBLISHED_ALREADY
    assert repeat["exit_code"] == 0
    assert repeat["binding"]["status"] == graph_home_binding.STATUS_UNCHANGED
    assert tree_state(destination) == before
    assert tree_state(home) == home_before


def test_a_migration_generation_at_the_destination_is_never_replaced(
    paths, tmp_path, fake_embeddings,
):
    home, destination, pointer = paths
    source = build_source(tmp_path / "old-store")
    applied = graph_migration.apply_migration(source=source, destination=destination)
    assert applied["status"] == graph_migration.STATUS_APPLIED
    before = tree_state(destination)
    report = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_BLOCKED
    assert graph_bootstrap.FINDING_GENERATION_NOT_FRESH in report["findings"]
    assert tree_state(destination) == before
    manifest, _ = read_manifest(destination / "current")
    assert manifest["generation_type"] == GENERATION_TYPE_MIGRATION


def test_verify_reproves_a_published_fresh_generation_read_only(paths):
    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    before = tree_state(destination)
    report = graph_bootstrap.verify_bootstrap(
        destination=destination, home=home, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_VERIFIED
    assert report["effective_binding"]["status"] == graph_home_binding.STATUS_VERIFIED
    assert tree_state(destination) == before


def test_a_served_generation_is_proved_by_attestation_not_publication_hashes(
    paths, monkeypatch,
):
    """The gate has to follow the generation's lifecycle, not its manifest.

    An activated generation is writable: an accepted semantic write
    republishes L1, the vector rows and L0 inside it, so the publication-time
    hashes stop describing it. Using them as the health gate reported a
    correctly-written store as corrupt, which made a repeat bootstrap fail
    precisely because the service had been doing its job.
    """
    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    generation = destination / "current"

    # Stand in for an accepted write: the served generation's own surface moves.
    index = generation / "L0.md"
    index.write_text(index.read_text(encoding="utf-8") + "\n[L0] later\n", encoding="utf-8")
    # The publication proof now fails, and that is correct for a *publication*.
    assert not verify_generation(generation).ok

    repeat = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert repeat["status"] == graph_bootstrap.STATUS_PUBLISHED_ALREADY, repeat["findings"]
    assert repeat["verification"]["integrity"] == "attested_runtime_health"
    assert repeat["verification"]["publication_hashes_checked"] is False

    verified = graph_bootstrap.verify_bootstrap(
        destination=destination, home=home, pointer=pointer
    )
    assert verified["status"] == graph_bootstrap.STATUS_VERIFIED
    assert verified["verification"]["integrity"] == "attested_runtime_health"

    # Without a pointer there is no attestation to lean on, so the only claim
    # available is publication integrity — and it is required.
    unattested = graph_bootstrap.verify_bootstrap(destination=destination)
    assert unattested["status"] == graph_bootstrap.STATUS_BLOCKED
    assert unattested["verification"]["integrity"] == "publication"
    assert graph_bootstrap.FINDING_GENERATION_UNVERIFIED in unattested["findings"]


def test_an_unserved_generation_is_still_proved_as_a_publication(paths):
    """The other half: no attestation means the full proof applies."""
    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    generation = destination / "current"
    pointer.unlink()  # nothing is serving it any more
    index = generation / "L0.md"
    index.write_text("tampered\n", encoding="utf-8")
    report = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_BLOCKED
    assert report["verification"]["integrity"] == "publication"
    assert graph_bootstrap.FINDING_GENERATION_UNVERIFIED in report["findings"]


def test_verify_refuses_a_tampered_fresh_generation(paths):
    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    index = destination / "current" / "L0.md"
    index.write_text(index.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    report = graph_bootstrap.verify_bootstrap(destination=destination)
    assert report["status"] == graph_bootstrap.STATUS_BLOCKED
    assert graph_bootstrap.FINDING_GENERATION_UNVERIFIED in report["findings"]


def test_a_failed_build_leaves_accounted_debris_and_no_generation(paths, monkeypatch):
    home, destination, pointer = paths

    def failing_build(*args, **kwargs):
        raise OSError("neutral-private-detail")

    monkeypatch.setattr(graph_bootstrap, "build_fresh_generation", failing_build)
    report = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_BLOCKED
    assert any(
        finding.startswith(graph_bootstrap.FINDING_BUILD_FAILED)
        for finding in report["findings"]
    )
    assert "neutral-private-detail" not in json.dumps(report)
    assert not (destination / "current").exists()
    assert not pointer.exists()
    assert not (home / "config.yaml").exists()


def test_a_home_whose_configuration_cannot_be_rewritten_blocks_before_publication(
    paths,
):
    home, destination, pointer = paths
    (home / "config.yaml").write_text(
        "graph:\n  mode: graph\ngraph:\n  mode: legacy\n", encoding="utf-8"
    )
    before = tree_state(home)
    report = graph_bootstrap.plan_bootstrap_fresh(
        home=home, destination=destination, pointer=pointer
    )
    assert report["status"] == graph_bootstrap.STATUS_BLOCKED
    assert graph_bootstrap.FINDING_BINDING_FAILED in report["findings"]
    applied = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert applied["status"] == graph_bootstrap.STATUS_BLOCKED
    assert not (destination / "current").exists()
    assert tree_state(home) == before


# ---------------------------------------------------------------------------
# the complete chain a cutover actually has to survive
# ---------------------------------------------------------------------------

def test_the_normal_startup_write_reopen_and_recall_chain_after_a_fresh_start(
    paths, monkeypatch,
):
    """Bootstrap → bind → normal config → semantic write → reopen → recall.

    The chain the rollout default depends on, started from an empty store
    rather than from a migration: a fresh generation is only usable if the
    configuration the *normal* startup path builds resolves it and a semantic
    write lands inside it.
    """
    import asyncio
    import json as _json

    import numpy as np

    from layered_memory_mcp import server
    from layered_memory_mcp.config import MemoryConfig
    from layered_memory_mcp.graph_application import GRAPH_COMMIT_DECISION_REQUIRED
    from layered_memory_mcp.storage import vector_store
    from test_v361_graph_entrances import _decision

    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    generation = destination / "current"

    monkeypatch.setattr(
        vector_store, "_embed_texts",
        lambda texts: np.ones((len(texts), vector_store.EMBED_DIM), dtype=np.float32)
        / np.sqrt(vector_store.EMBED_DIM),
    )
    # No constructor overrides and no environment pointer: the home's own
    # configuration is what has to select the generation.
    config = MemoryConfig(home=str(home), agent_memory={"adapter": "generic"})
    assert config.graph_mode == "graph"
    assert config.graph_active_pointer == pointer
    assert config.knowledge_dir == generation / "knowledge"
    assert config.vector_db_path == generation / "data" / "vectors.db"
    monkeypatch.setattr(server, "_config", config)

    def call(coroutine):
        return _json.loads(asyncio.run(coroutine))

    # An inject with no decision is refused: the authority is not bypassable
    # just because the store is empty.
    refused = call(
        server.inject_knowledge_tool(domain="infra", section="Boundary", content="x")
    )
    assert refused["success"] is False
    assert refused["error"] == GRAPH_COMMIT_DECISION_REQUIRED
    assert list((generation / "knowledge").iterdir()) == []

    committed = call(
        server.inject_knowledge_tool(
            domain="infra", section="Boundary",
            content="The retrieval index is rebuilt nightly at 02:00 UTC.",
            semantic_write_request=_decision(),
        )
    )
    assert committed["success"] is True, committed
    # Every projection landed inside the generation, and nothing beside it.
    assert (generation / "knowledge" / "infra.md").is_file()
    assert not (home / "knowledge").exists()
    assert not (home / "data" / "vectors.db").exists()
    # Auxiliary stores keep their own home, exactly as before the cutover.
    assert {path.name for path in (home / "data").iterdir()} <= {
        "access_log.db", "review_queue.db", "todos.db", "memory_changelog.jsonl",
    }

    # Close and reopen through the same normal path: the write is still there.
    monkeypatch.setattr(server, "_config", None)
    reopened = MemoryConfig(home=str(home), agent_memory={"adapter": "generic"})
    assert reopened.graph_active_pointer == pointer
    monkeypatch.setattr(server, "_config", reopened)
    recalled = call(server.search_semantic("retrieval index"))
    assert recalled["success"] is True, recalled

    # The binding proof still passes after an accepted write, and re-running the
    # bootstrap remains a no-op that republishes nothing.
    assert graph_home_binding.verify_effective_binding(
        home=home, pointer=pointer
    ).status == graph_home_binding.STATUS_VERIFIED
    repeat = graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )
    assert repeat["status"] == graph_bootstrap.STATUS_PUBLISHED_ALREADY, repeat["findings"]
    assert (generation / "knowledge" / "infra.md").is_file()


# ---------------------------------------------------------------------------
# the migration contract must not be relaxed by the fresh one
# ---------------------------------------------------------------------------

def test_a_zero_event_journal_is_only_valid_for_a_declared_fresh_generation(
    paths, tmp_path, fake_embeddings,
):
    """The exact hole the two contracts exist to keep closed."""
    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    generation = destination / "current"
    assert verify_generation(generation).ok

    # Relabel the same empty generation as a migration. Nothing about its bytes
    # changes; what changes is the claim it makes about where it came from.
    from layered_memory_mcp.graph_migration_generation import write_manifest

    manifest, _ = read_manifest(generation)
    manifest["generation_type"] = GENERATION_TYPE_MIGRATION
    write_manifest(generation, manifest)
    relabelled = verify_generation(generation)
    assert not relabelled.ok
    assert "migration_generation_has_no_authority" in relabelled.findings


def test_an_unknown_generation_type_is_refused_rather_than_assumed(paths):
    from layered_memory_mcp.graph_migration_generation import write_manifest

    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    generation = destination / "current"
    manifest, _ = read_manifest(generation)
    manifest["generation_type"] = "something_else"
    write_manifest(generation, manifest)
    report = verify_generation(generation)
    assert report.findings == ["manifest_generation_type_unsupported:something_else"]


@pytest.mark.parametrize("key,value", [
    ("source", "/somewhere"),
    ("source_fingerprint", "deadbeef"),
    ("source_files", {"knowledge/a.md": {"size": 1, "sha256": "0" * 64}}),
])
def test_a_fresh_manifest_claiming_a_source_is_refused(paths, key, value):
    from layered_memory_mcp.graph_migration_generation import write_manifest

    home, destination, pointer = paths
    assert graph_bootstrap.apply_bootstrap(
        home=home, destination=destination, pointer=pointer
    )["status"] == graph_bootstrap.STATUS_APPLIED
    generation = destination / "current"
    manifest, _ = read_manifest(generation)
    manifest[key] = value
    write_manifest(generation, manifest)
    report = verify_generation(generation)
    assert not report.ok
    assert f"fresh_generation_not_empty:{key}" in report.findings


def test_a_migration_generation_still_declares_its_type_and_verifies(
    tmp_path, fake_embeddings,
):
    source = build_source(tmp_path / "old-store")
    destination = tmp_path / "migrated"
    applied = graph_migration.apply_migration(source=source, destination=destination)
    assert applied["status"] == graph_migration.STATUS_APPLIED
    manifest, _ = read_manifest(destination / "current")
    assert manifest["generation_type"] == GENERATION_TYPE_MIGRATION
    assert manifest["journal"]["events"] > 0
    assert verify_generation(destination / "current").ok


# ---------------------------------------------------------------------------
# the real CLI
# ---------------------------------------------------------------------------

def cli(*arguments: str, cwd: Path) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1] / "src")
        + os.pathsep
        + environment.get("PYTHONPATH", "")
    )
    for name in graph_home_binding.OVERRIDE_ENVIRONMENT:
        environment.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "layered_memory_mcp.graph_bootstrap", *arguments],
        cwd=cwd, env=environment, capture_output=True, text=True, timeout=180,
    )


def test_the_real_cli_defaults_to_a_read_only_plan(paths, tmp_path):
    home, destination, pointer = paths
    before = tree_state(tmp_path)
    completed = cli(
        "--home", str(home), "--destination", str(destination),
        "--pointer", str(pointer), cwd=tmp_path,
    )
    assert completed.stdout.strip(), completed.stderr
    report = json.loads(completed.stdout)
    assert completed.returncode == report["exit_code"] == 0
    assert report["status"] == graph_bootstrap.STATUS_PLANNED
    assert report["dry_run"] is True
    assert tree_state(tmp_path) == before


def test_the_real_cli_applies_then_verifies(paths, tmp_path):
    home, destination, pointer = paths
    applied = cli(
        "apply", "--home", str(home), "--destination", str(destination),
        "--pointer", str(pointer), cwd=tmp_path,
    )
    report = json.loads(applied.stdout)
    assert applied.returncode == report["exit_code"] == 0, report["findings"]
    assert report["status"] == graph_bootstrap.STATUS_APPLIED
    verified = cli(
        "verify", "--destination", str(destination), "--home", str(home),
        "--pointer", str(pointer), cwd=tmp_path,
    )
    report = json.loads(verified.stdout)
    assert verified.returncode == report["exit_code"] == 0
    assert report["status"] == graph_bootstrap.STATUS_VERIFIED


def test_the_real_cli_requires_its_explicit_arguments(paths, tmp_path):
    _, destination, _ = paths
    completed = cli("apply", "--destination", str(destination), cwd=tmp_path)
    report = json.loads(completed.stdout)
    assert completed.returncode == graph_bootstrap.EXIT_USAGE
    assert report["findings"] == ["argument_required:--home", "argument_required:--pointer"]
    assert not destination.exists()

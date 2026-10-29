"""v3.6.1 — the production generation runtime adapter and its cutover verbs.

Why this file exists: publishing a verified generation and *serving* one are
different acts, and the second is the one that can break a running system. Each
test below pins one refusal or one guarantee of that boundary:

  * resolution takes an explicit generation root **or** an explicit pointer
    file, never both, never neither, and never a home directory, a
    ``config.yaml`` or an environment variable;
  * the *state contract*: an explicit root is a ``published`` generation and is
    always proved in full by
    :func:`~layered_memory_mcp.graph_migration_generation.verify_generation`,
    while a pointer is an ``activated`` one and is proved by the attestation
    that pointer carries. There is no argument that asks for the weaker proof on
    an arbitrary directory, a pointer with no attestation is refused, and a
    served generation never claims ``generation_intact``;
  * every structural guard — ancestor symlinks, the pointer, the generation's own
    members and the paths its manifest declares — refuses *before* the manifest
    is read and before any store is opened, which is asserted by instrumenting
    those reads to fail if they happen;
  * a pointer may only name a directory strictly inside its own parent, reached
    without a symlink: the control plane cannot be made to point at somebody
    else's tree;
  * a cutover is a dry run by default and writes *nothing at all* — no pointer,
    no temporary file, not even the control root;
  * an explicit apply writes the pointer with one atomic replace, retains the
    pointer it supersedes, is a no-op when repeated, and restores without
    deleting any generation;
  * and the whole round trip survives a *real* framework write: activate, commit
    a semantic write through the resolved config, close, reopen by pointer,
    reactivate, restore — with the graph, the journal, the readable surface,
    ``L0.md`` and the vector rows checked by independent raw readers rather than
    by the adapter agreeing with itself.

Isolation: every path is an explicit ``tmp_path``. The generations under test
are produced by the real migration operator from a neutral fixture snapshot with
deterministic fake embeddings — no real memory home, no real config file, no
discovery, no network, no model download.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from layered_memory_mcp import graph_application, graph_generation_runtime as runtime
from layered_memory_mcp import graph_migration
from layered_memory_mcp import graph_migration_generation as generation_module
from layered_memory_mcp.config import GRAPH_MODES, MemoryConfig
from layered_memory_mcp.graph_generation_runtime import (
    CUTOVER_COMMANDS,
    INTEGRITY_ATTESTED,
    INTEGRITY_UNPROVED,
    INTEGRITY_VERIFIED,
    LIFECYCLE_ACTIVATED,
    LIFECYCLE_PUBLISHED,
    POINTER_VERSION,
    STATE_ACTIVE,
    STATE_INVALID,
    STATE_ROLLBACK,
    STATUS_CUTOVER_APPLIED,
    STATUS_CUTOVER_PLANNED,
    STATUS_CUTOVER_REFUSED,
    STATUS_CUTOVER_UNCHANGED,
    STATUS_RESTORE_APPLIED,
    STATUS_RESTORE_NOTHING,
    STATUS_RESTORE_PLANNED,
    STATUS_RESTORE_REFUSED,
    STATUS_RUNTIME_ABSENT,
    STATUS_RUNTIME_ACTIVE,
    STATUS_RUNTIME_INVALID,
    STATUS_RUNTIME_ROLLBACK,
    activate_generation,
    read_active_pointer,
    resolve_generation_runtime,
    restore_generation,
    status_generation,
)
from layered_memory_mcp.graph_migration_generation import (
    MANIFEST_DIGEST_NAME,
    MANIFEST_NAME,
    manifest_digest,
    read_manifest,
    write_manifest,
)
from layered_memory_mcp.graph_models import (
    Derivation,
    DerivationMethod,
    GraphNode,
    NodeKind,
)
from layered_memory_mcp.graph_projection import ProjectionBinding
from layered_memory_mcp.graph_runtime_assembly import (
    GRAPH_STORE_OPENED,
    JOURNAL_OPENED,
    resolve_graph_database_path,
    resolve_journal_database_path,
)
from layered_memory_mcp.graph_write import SemanticWriteRequest

#: The fixture builders live in the migration suite rather than being restated:
#: two spellings of "a genuine old-format snapshot" is how two suites start
#: testing different inputs.
from test_v361_graph_migration import (  # noqa: F401 — fixtures are reused
    ROOT_KEY,
    build_source,
    changed_paths,
    fake_embeddings,
    tree_state,
)

# ---------------------------------------------------------------------------
# The deployment layout under test
#
#   <tmp>/old-store/            the legacy snapshot (never written)
#   <tmp>/runtime/              the control root — the pointer's parent
#   <tmp>/runtime/active.json   the active pointer
#   <tmp>/runtime/migrated/     the migration destination
#   <tmp>/runtime/migrated/current, .../rollback/withdrawn-…
#
# The pointer lives *beside* the destination, never inside it: the migration
# tool owns the top-level names of its destination and refuses unknown ones.
# ---------------------------------------------------------------------------

@pytest.fixture
def control_root(tmp_path) -> Path:
    root = tmp_path / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def destination(control_root) -> Path:
    return control_root / "migrated"


@pytest.fixture
def source(tmp_path) -> Path:
    return build_source(tmp_path / "old-store")


@pytest.fixture
def pointer(control_root) -> Path:
    return control_root / "active.json"


@pytest.fixture
def published(source, destination) -> Path:
    """One published, verified generation at ``<destination>/current``."""
    report = graph_migration.apply_migration(source=source, destination=destination)
    assert report["status"] == graph_migration.STATUS_APPLIED, report["findings"]
    return destination / "current"


def withdraw_and_republish(source: Path, destination: Path) -> tuple[Path, Path]:
    """``(withdrawn, republished)`` — a retained rollback plus a fresh current.

    Both are produced by the real operator, so neither is a hand-built tree
    that happens to satisfy a verification.
    """
    rolled = graph_migration.rollback_migration(destination=destination)
    assert rolled["status"] == graph_migration.STATUS_ROLLED_BACK
    withdrawn = Path(rolled["generation"]["withdrawn_to"])
    again = graph_migration.apply_migration(source=source, destination=destination)
    assert again["status"] == graph_migration.STATUS_APPLIED, again["findings"]
    return withdrawn, destination / "current"


def write_pointer(path: Path, **fields) -> None:
    """Hand-write a pointer file, bypassing the tool that normally writes it.

    How the refusals below are reached: a forged or hand-edited control-plane
    file is exactly the input the resolver has to survive, and routing it
    through :func:`activate_generation` would only test what that function
    emits.
    """
    payload = {
        "pointer_version": POINTER_VERSION,
        "tool": "layered_memory_mcp.graph_generation_runtime",
        "generation": "migrated/current",
        "activated_at": "2026-10-29T00:00:00+00:00",
        "manifest_sha256": "",
        "previous_generation": "",
        "previous_activated_at": "",
        "previous_manifest_sha256": "",
    }
    payload.update(fields)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def probe_counts(generation: Path) -> tuple[int, int]:
    """``(graph nodes, journal events)``, read with raw read-only SQLite.

    The independent reverse probe: it never calls the resolver, the verifier or
    the assembly entrances, so "the activated generation really is the populated
    one" is established by a second, unrelated reader rather than by the code
    under test agreeing with itself.
    """
    counts = []
    for name, table in (("graph.db", "graph_nodes"), ("journal.db", "memory_events")):
        uri = f"{(generation / name).resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            counts.append(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            )
        finally:
            connection.close()
    return counts[0], counts[1]


def temp_files(root: Path) -> list[str]:
    return sorted(path.name for path in root.rglob("*") if path.name.endswith(".tmp"))


def attest(generation: Path) -> str:
    """The digest a real cutover would record for ``generation``.

    Computed from the manifest body with the migration tool's own
    :func:`manifest_digest`, so a hand-written pointer below carries a *genuine*
    attestation and the tests that perturb one are perturbing something real.
    """
    body, findings = read_manifest(generation)
    assert body is not None and findings == [], findings
    return manifest_digest(body)


def probe_text(path: Path) -> str:
    """One readable file, read with nothing from the module under test."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def probe_vector_ids(generation: Path) -> set[str]:
    """The vector row ids of one generation, read with raw read-only SQLite."""
    uri = f"{(generation / 'data' / 'vectors.db').resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return {
            str(row[0]) for row in connection.execute("SELECT id FROM vectors").fetchall()
        }
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# A real framework write into an activated generation
#
# Not a hand-edited file: the point of the lifecycle tests is that an activated
# generation is written by the *framework*, through the config this adapter
# produced, and that everything the adapter then claims about it survives that.
# ---------------------------------------------------------------------------

#: A fixed instant, so a replay and a digest are reproducible.
TW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)

#: The neutral technical body one runtime write publishes.
RUNTIME_BODY = "Cutover rehearsals run on the standby replica."

#: A domain the migrated snapshot does not contain, so the write creates a new
#: readable surface rather than racing the fixture's own sections.
RUNTIME_DOMAIN = "operations"
RUNTIME_SECTION = "Cutover rehearsals"

#: The projection identity the write publishes under — also the vector row's own
#: ``id``, which is how the raw vector probe below knows what to look for.
RUNTIME_ENTRY_ID = ProjectionBinding(
    domain=RUNTIME_DOMAIN, section=RUNTIME_SECTION, body=RUNTIME_BODY
).entry_id


def commit_runtime_write(
    config,
    *,
    node_id: str = "claim_cutover_rehearsal",
    body: str = RUNTIME_BODY,
    section: str = RUNTIME_SECTION,
):
    """Commit one real semantic write through the resolved generation config."""
    request = SemanticWriteRequest(
        node=GraphNode(
            id=node_id,
            kind=NodeKind.CLAIM,
            label="cutover rehearsal",
            content=body,
            recorded_at=TW,
            derivation=Derivation(method=DerivationMethod.USER_EXPLICIT),
        ),
        occurred_at=TW,
        request_key=node_id,
    )
    return graph_application.commit_semantic_write(
        config,
        request,
        ProjectionBinding(domain=RUNTIME_DOMAIN, section=section, body=body),
    )


def write_into(generation: Path, pointer: Path, **kwargs):
    """Resolve ``pointer``, commit one real write, and let every handle close.

    Returns the commit. The resolution is discarded on purpose: a reopen later
    in the same test must go through the pointer again, not through a config
    that is still holding anything.
    """
    resolved = resolve_generation_runtime(pointer=pointer)
    assert resolved.usable is True, resolved.report.findings
    assert resolved.config.root == generation
    commit = commit_runtime_write(resolved.config, **kwargs)
    assert commit.status == graph_application.GRAPH_COMMIT_COMMITTED, commit.errors
    return commit


def refuse(name: str):
    """A stand-in for one read that fails loudly if it is ever reached."""
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} ran before the guards passed")

    return fail


def forbid(monkeypatch, *names: str) -> None:
    """Instrument the named module-level reads so that reaching one is a failure.

    How "the guard refused *before* anything was read" is established rather
    than assumed: a check that refused only *after* following a link would raise
    here instead of quietly returning the right token for the wrong reason.
    """
    for name in names:
        monkeypatch.setattr(runtime, name, refuse(name))


@pytest.fixture
def forbid_opens(monkeypatch):
    """Opening a store or running the publication proof is an error."""
    forbid(monkeypatch, "runtime_status", "verify_generation")


@pytest.fixture
def forbid_reads(monkeypatch, forbid_opens):
    """As :func:`forbid_opens`, and reading the manifest or the pointer too."""
    forbid(monkeypatch, "read_manifest", "read_active_pointer")


# ---------------------------------------------------------------------------
# Explicit inputs only
# ---------------------------------------------------------------------------

def test_resolution_refuses_both_inputs_at_once(published, pointer):
    resolved = resolve_generation_runtime(generation=published, pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert runtime.FINDING_INPUT_AMBIGUOUS in resolved.report.findings
    assert resolved.config is None
    assert resolved.usable is False


def test_resolution_refuses_neither_input():
    resolved = resolve_generation_runtime()

    assert resolved.report.state == STATE_INVALID
    assert runtime.FINDING_INPUT_MISSING in resolved.report.findings


def test_resolution_reads_no_home_config_or_environment(
    published, tmp_path, monkeypatch
):
    """An explicit root is the only input: a decoy home is never consulted."""
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    (decoy / "config.yaml").write_text("graph_mode: legacy\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(decoy))
    monkeypatch.setenv("USERPROFILE", str(decoy))
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_DB_PATH", str(decoy / "graph.db"))
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_JOURNAL_PATH", str(decoy / "journal.db"))
    before = tree_state(decoy)

    resolved = resolve_generation_runtime(generation=published)

    assert resolved.config is not None
    assert resolved.config.graph_db_path == published / "graph.db"
    assert resolved.config.graph_journal_path == published / "journal.db"
    assert tree_state(decoy) == before


# ---------------------------------------------------------------------------
# Resolution: proved, or invalid
# ---------------------------------------------------------------------------

def test_an_absent_pointer_is_absent_and_creates_nothing(control_root, pointer):
    before = tree_state(control_root)

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == runtime.STATE_ABSENT
    assert resolved.report.status == STATUS_RUNTIME_ABSENT
    assert resolved.report.pointer_present is False
    assert resolved.usable is False
    assert not pointer.exists()
    assert tree_state(control_root) == before


def test_an_explicit_root_resolves_active_with_generation_scoped_paths(published):
    resolved = resolve_generation_runtime(generation=published)
    report = resolved.report
    config = resolved.config

    assert report.state == STATE_ACTIVE
    assert report.status == STATUS_RUNTIME_ACTIVE
    assert report.generation_intact is True
    assert report.read_only_health is True
    assert report.graph_status == GRAPH_STORE_OPENED
    assert report.journal_status == JOURNAL_OPENED
    assert resolved.usable is True
    # The duck-typed config the graph write application reads, every path of it
    # derived from the generation root and nothing else.
    assert resolve_graph_database_path(config) == published / "graph.db"
    assert resolve_journal_database_path(config) == published / "journal.db"
    assert config.knowledge_dirs == [published / "knowledge"]
    assert config.l0_index_file == published / "L0.md"
    assert config.data_dir == published / "data"
    assert config.vector_db_path == published / "data" / "vectors.db"
    assert config.graph_write_enabled is True


def test_the_resolved_config_drives_the_existing_runtime_entrances(published):
    """Requirement 2, end to end: the adapter's config *is* a runtime config."""
    resolved = resolve_generation_runtime(generation=published)

    status = graph_application.runtime_status(resolved.config)

    assert status["mode"] == "graph"
    assert status["journal"]["status"] == JOURNAL_OPENED
    assert status["graph"]["status"] == GRAPH_STORE_OPENED
    assert status["graph"]["healthy"] is True
    assert status["graph"]["node_count"] > 0
    assert status["projection_drift"]["drifted"] is False
    assert status["journal"]["pending_projections"] == []


def test_the_resolved_config_points_every_projection_at_the_generation(published):
    """The duck-typed surface the write application and its projections read."""
    from layered_memory_mcp import graph_projection
    from layered_memory_mcp.graph_application import pending_surface_entry_ids
    from layered_memory_mcp.storage.vector_store import VectorStore

    config = resolve_generation_runtime(generation=published).config

    assert graph_projection.domain_path(config, "retrieval") == (
        published / "knowledge" / "retrieval.md"
    )
    # ``publish_vector`` resolves its data directory as ``<home>/data``.
    assert Path(config.home) / "data" == config.data_dir
    assert config.vector_db_path.is_file()
    with VectorStore(config.vector_db_path) as vectors:
        assert len(vectors.snapshot()) > 0
    # Read through the journal's inspection entrance, from this config alone.
    assert pending_surface_entry_ids(config) == set()
    assert config.l0_format == "hermes"
    assert config.memory_changelog_enabled is False


# ---------------------------------------------------------------------------
# The state contract: the input chooses the lifecycle, and there is no knob
# ---------------------------------------------------------------------------

def test_the_lifecycle_is_not_a_callers_argument():
    """There is deliberately no parameter that asks for the weaker proof.

    The gap this closes: a flag meaning "skip the publication proof" is, for any
    directory the caller cares to name, a flag meaning "report a never-verified
    tree as intact". The lifecycle therefore follows from the input, and the
    signature is the enforcement.
    """
    import inspect

    parameters = inspect.signature(resolve_generation_runtime).parameters

    assert set(parameters) == {"generation", "pointer", "graph_mode"}
    assert "verify" not in parameters
    assert "verify" not in inspect.signature(status_generation).parameters


def test_an_explicit_root_is_always_proved_as_a_publication(published):
    resolved = resolve_generation_runtime(generation=published)

    assert resolved.report.lifecycle == LIFECYCLE_PUBLISHED
    assert resolved.report.integrity == INTEGRITY_VERIFIED
    assert resolved.report.generation_intact is True
    assert resolved.report.attested is False
    assert resolved.verification is not None
    assert resolved.verification.hashes_ok is True


def test_a_pointer_is_proved_as_an_activation_against_its_attestation(
    published, pointer
):
    """The steady state: a pointer's digest *is* the record of the proof.

    Why this is not the publication check: by the time a pointer is being
    reopened the framework may legitimately have written into the generation, so
    the artifact hashes no longer describe it. What is still true — and what is
    therefore checked — is the manifest binding the attestation names.
    """
    write_pointer(pointer, manifest_sha256=attest(published))

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.lifecycle == LIFECYCLE_ACTIVATED
    assert resolved.report.integrity == INTEGRITY_ATTESTED
    assert resolved.report.attested is True
    assert resolved.report.projection_consistent is True
    assert resolved.usable is True
    # The weaker proof is never dressed up as the stronger one.
    assert resolved.report.generation_intact is False
    assert resolved.verification is None


def test_a_pointer_with_no_attestation_is_refused(published, pointer):
    """A fresh or forged control file records no proof, so it buys nothing.

    This is the bypass the state contract exists to remove: without this
    refusal, hand-writing a pointer would be enough to serve any directory in
    the activated lifecycle without any proof ever having been performed.
    """
    write_pointer(pointer, manifest_sha256="")

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert resolved.report.integrity == INTEGRITY_UNPROVED
    assert runtime.FINDING_POINTER_ATTESTATION in resolved.report.findings
    assert resolved.report.attested is False
    assert resolved.config is None


def test_a_never_activated_generation_cannot_reach_the_weaker_proof(
    source, tmp_path, control_root
):
    """A tree nobody ever published or activated has exactly one route in.

    An unpublished copy has no manifest, so the published lifecycle refuses it —
    and there is no second route that would have accepted it.
    """
    import shutil

    fresh = control_root / "unpublished"
    shutil.copytree(source, fresh)

    resolved = resolve_generation_runtime(generation=fresh)

    assert resolved.report.state == STATE_INVALID
    assert resolved.report.integrity == INTEGRITY_UNPROVED
    assert resolved.report.generation_intact is False
    assert any(
        finding.startswith(runtime.FINDING_GENERATION_MEMBER_REJECTED)
        for finding in resolved.report.findings
    ), resolved.report.findings
    assert resolved.config is None


def test_a_tampered_generation_is_refused_in_both_lifecycles(published, pointer):
    """The edited file is refused as a publication, and the forged attestation
    that would have smuggled it past the activated lifecycle is refused too."""
    digest = attest(published)
    (published / "knowledge" / "retrieval.md").write_text("edited\n", encoding="utf-8")
    write_pointer(pointer, manifest_sha256=digest)

    as_published = resolve_generation_runtime(generation=published)
    as_activated = resolve_generation_runtime(pointer=pointer)

    assert as_published.report.state == STATE_INVALID
    assert runtime.FINDING_GENERATION_NOT_INTACT in as_published.report.findings
    # An *edited readable file* is exactly what a legitimate runtime write looks
    # like, so the activated lifecycle resolves it — on the strength of a
    # digest that only a real cutover writes — and says so without claiming the
    # publication proof.
    assert as_activated.report.integrity == INTEGRITY_ATTESTED
    assert as_activated.report.generation_intact is False
    # Replace the manifest itself and the binding is gone in both lifecycles.
    (published / MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
    assert resolve_generation_runtime(pointer=pointer).usable is False
    assert resolve_generation_runtime(generation=published).usable is False


def test_the_reverse_probe_sees_the_same_populated_generation(published):
    resolved = resolve_generation_runtime(generation=published)

    nodes, events = probe_counts(published)

    assert nodes > 0 and events > 0
    assert resolved.report.node_count == nodes
    assert resolved.report.event_count == events


def test_a_tampered_generation_resolves_invalid_and_is_not_usable(published):
    (published / "knowledge" / "retrieval.md").write_text("edited\n", encoding="utf-8")

    resolved = resolve_generation_runtime(generation=published)

    assert resolved.report.state == STATE_INVALID
    assert resolved.report.status == STATUS_RUNTIME_INVALID
    assert resolved.report.generation_intact is False
    assert runtime.FINDING_GENERATION_NOT_INTACT in resolved.report.findings
    assert resolved.usable is False
    assert resolved.config is None


def test_an_absent_generation_root_is_rejected(tmp_path):
    resolved = resolve_generation_runtime(generation=tmp_path / "nowhere")

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_GENERATION_ROOT_REJECTED}:absent"
        in resolved.report.findings
    )


def test_a_symlinked_generation_root_is_rejected(published, tmp_path):
    link = tmp_path / "linked-current"
    link.symlink_to(published, target_is_directory=True)

    resolved = resolve_generation_runtime(generation=link)

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_GENERATION_ROOT_REJECTED}:symlink"
        in resolved.report.findings
    )


@pytest.mark.parametrize(
    "target",
    ["../escape/current", "/etc", "..", "migrated/../../escape", "", "a\\b"],
)
def test_a_pointer_target_outside_the_control_root_is_rejected(
    published, pointer, target
):
    write_pointer(pointer, generation=target)

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert any(
        finding.startswith(runtime.FINDING_POINTER_TARGET_REJECTED)
        for finding in resolved.report.findings
    ), resolved.report.findings
    assert resolved.config is None


def test_a_pointer_target_reached_through_a_symlink_is_rejected(
    published, control_root, pointer
):
    (control_root / "linked").symlink_to(published, target_is_directory=True)
    write_pointer(pointer, generation="linked")

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_POINTER_TARGET_REJECTED}:symlink"
        in resolved.report.findings
    )


def test_a_symlinked_pointer_file_is_rejected(published, control_root, pointer):
    real = control_root / "real-pointer.json"
    write_pointer(real)
    pointer.symlink_to(real)

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert runtime.FINDING_POINTER_SYMLINK in resolved.report.findings


def test_a_pointer_version_this_tool_does_not_speak_fails_closed(published, pointer):
    write_pointer(pointer, pointer_version=POINTER_VERSION + 1)

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_POINTER_VERSION}:{POINTER_VERSION + 1}"
        in resolved.report.findings
    )


def test_an_unreadable_pointer_fails_closed(published, pointer):
    pointer.write_text("{not json", encoding="utf-8")

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert any(
        finding.startswith(runtime.FINDING_POINTER_UNREADABLE)
        for finding in resolved.report.findings
    )


def test_a_pointer_whose_recorded_manifest_digest_moved_on_fails_closed(
    published, pointer
):
    write_pointer(pointer, manifest_sha256="0" * 64)

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert runtime.FINDING_POINTER_MANIFEST_CHANGED in resolved.report.findings


def test_a_pointer_naming_a_deleted_generation_reports_it_absent(
    published, destination, pointer
):
    write_pointer(pointer, generation="migrated/gone")

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_GENERATION_ROOT_REJECTED}:absent"
        in resolved.report.findings
    )


def test_read_active_pointer_reports_the_recorded_contract(published, pointer):
    write_pointer(pointer, manifest_sha256="a" * 64, extra_key="ignored")

    parsed, findings, warnings = read_active_pointer(pointer)

    assert findings == []
    assert parsed is not None
    assert parsed.generation == "migrated/current"
    assert parsed.manifest_sha256 == "a" * 64
    assert "pointer_unknown_key:extra_key" in warnings


# ---------------------------------------------------------------------------
# Security before reads
#
# Every test in this section instruments the reads themselves: the manifest
# reader, the pointer reader, the publication proof and the store-opening health
# probe are all replaced with functions that raise. A guard that refused *after*
# following the link would therefore fail here rather than quietly returning the
# right refusal token for the wrong reason.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label",
    [MANIFEST_NAME, MANIFEST_DIGEST_NAME, "graph.db", "journal.db", "L0.md"],
)
def test_a_symlinked_generation_file_is_refused_before_it_is_read(
    published, tmp_path, forbid_reads, label
):
    decoy = tmp_path / f"decoy-{label}"
    (published / label).rename(decoy)
    (published / label).symlink_to(decoy)

    resolved = resolve_generation_runtime(generation=published)

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_GENERATION_MEMBER_REJECTED}:{label}:symlink"
        in resolved.report.findings
    )
    assert resolved.config is None


@pytest.mark.parametrize("label", ["knowledge", "data"])
def test_a_symlinked_generation_directory_is_refused_before_it_is_read(
    published, tmp_path, forbid_reads, label
):
    """``knowledge/`` and ``data/`` are where an activated generation *writes*.

    A link here would let a framework write inside a proved generation publish a
    readable surface or a vector row into somebody else's tree, so it is refused
    as a path decision rather than discovered as a backend error later.
    """
    decoy = tmp_path / f"decoy-{label}"
    (published / label).rename(decoy)
    (published / label).symlink_to(decoy, target_is_directory=True)

    resolved = resolve_generation_runtime(generation=published)

    assert (
        f"{runtime.FINDING_GENERATION_MEMBER_REJECTED}:{label}:symlink"
        in resolved.report.findings
    )
    assert resolved.config is None


def test_a_symlinked_vector_database_is_refused_before_it_is_read(
    published, tmp_path, forbid_reads
):
    decoy = tmp_path / "decoy-vectors.db"
    (published / "data" / "vectors.db").rename(decoy)
    (published / "data" / "vectors.db").symlink_to(decoy)

    resolved = resolve_generation_runtime(generation=published)

    assert (
        f"{runtime.FINDING_GENERATION_MEMBER_REJECTED}:data/vectors.db:symlink"
        in resolved.report.findings
    )


def test_a_generation_member_of_the_wrong_kind_is_refused_before_it_is_read(
    published, forbid_reads
):
    """``sqlite3`` would accept a directory and fail from inside a session."""
    (published / "graph.db").unlink()
    (published / "graph.db").mkdir()

    resolved = resolve_generation_runtime(generation=published)

    assert (
        f"{runtime.FINDING_GENERATION_MEMBER_REJECTED}:graph.db:not_a_file"
        in resolved.report.findings
    )


def test_a_generation_root_behind_a_symlinked_ancestor_is_refused_first(
    published, control_root, tmp_path, forbid_reads
):
    """The guard the containment walk cannot give.

    A walk that starts at a root and checks everything below it says nothing
    about how the root itself was reached — so an ancestor that is a link would
    make every "inside this generation" conclusion a statement about the link.
    """
    via = tmp_path / "via"
    via.symlink_to(control_root, target_is_directory=True)

    resolved = resolve_generation_runtime(generation=via / "migrated" / "current")

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_GENERATION_ROOT_REJECTED}:ancestor_symlink"
        in resolved.report.findings
    )
    assert resolved.config is None


def test_a_pointer_behind_a_symlinked_ancestor_is_refused_before_it_is_read(
    published, control_root, tmp_path, pointer, forbid_reads
):
    """The pointer's parent *is* the control root, so this is checked first."""
    write_pointer(pointer, manifest_sha256=attest(published))
    via = tmp_path / "via"
    via.symlink_to(tmp_path, target_is_directory=True)

    resolved = resolve_generation_runtime(
        pointer=via / control_root.name / pointer.name
    )

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_CONTROL_ROOT_REJECTED}:ancestor_symlink"
        in resolved.report.findings
    )
    assert resolved.config is None


def test_an_activation_through_a_symlinked_ancestor_is_refused_and_writes_nothing(
    published, control_root, tmp_path
):
    via = tmp_path / "via"
    via.symlink_to(tmp_path, target_is_directory=True)
    before = tree_state(control_root)

    report = activate_generation(
        pointer=via / control_root.name / "active.json",
        generation=published,
        apply=True,
    )

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert (
        f"{runtime.FINDING_CONTROL_ROOT_REJECTED}:ancestor_symlink"
        in report["findings"]
    )
    assert not (control_root / "active.json").exists()
    assert tree_state(control_root) == before


@pytest.mark.parametrize(
    "declared", ["../escape.txt", "/etc/passwd", "a/../../escape", "x\\y"]
)
def test_a_manifest_declaring_an_unsafe_artifact_path_is_refused_before_any_open(
    published, forbid_opens, declared
):
    """A manifest is untrusted input in *both* lifecycles.

    The activated lifecycle does not run ``verify_generation``, which is where
    manifest paths used to be checked — so without this guard a served
    generation's manifest would never be checked at all. The forged manifest is
    given a matching digest sidecar, so it parses: the refusal has to come from
    the path check, not from the digest.
    """
    body, findings = read_manifest(published)
    assert body is not None and findings == []
    body["artifacts"] = {**body["artifacts"], declared: {"sha256": "0" * 64}}
    write_manifest(published, body)

    resolved = resolve_generation_runtime(generation=published)

    assert resolved.report.state == STATE_INVALID
    assert any(
        finding.startswith(f"{runtime.FINDING_MANIFEST_PATH_REJECTED}:artifacts")
        for finding in resolved.report.findings
    ), resolved.report.findings
    assert resolved.config is None


def test_a_manifest_declaring_an_unsafe_source_path_is_refused_before_any_open(
    published, forbid_opens
):
    body, _findings = read_manifest(published)
    body["source_files"] = {"../../elsewhere.db": {"sha256": "0" * 64}}
    write_manifest(published, body)

    resolved = resolve_generation_runtime(generation=published)

    assert (
        f"{runtime.FINDING_MANIFEST_PATH_REJECTED}:source_files:escape"
        in resolved.report.findings
    )


def test_a_manifest_whose_path_section_is_not_a_mapping_is_refused(
    published, forbid_opens
):
    body, _findings = read_manifest(published)
    body["artifacts"] = ["knowledge/retrieval.md"]
    write_manifest(published, body)

    resolved = resolve_generation_runtime(generation=published)

    assert (
        f"{runtime.FINDING_MANIFEST_PATH_REJECTED}:artifacts:not_a_mapping"
        in resolved.report.findings
    )


def test_a_member_guard_runs_before_any_read_in_the_activated_lifecycle(
    published, pointer, tmp_path, monkeypatch
):
    """The steady state is guarded on the same terms, not on trust.

    An attested pointer is not a licence to open whatever the generation now
    contains: the member guards run before the manifest is read on this path
    too, which is the difference between "a pointer proved this once" and "a
    pointer is proof of anything that happens to be there now".
    """
    write_pointer(pointer, manifest_sha256=attest(published))
    assert resolve_generation_runtime(pointer=pointer).usable is True

    decoy = tmp_path / "decoy-graph.db"
    (published / "graph.db").rename(decoy)
    (published / "graph.db").symlink_to(decoy)
    forbid(monkeypatch, "read_manifest", "runtime_status", "verify_generation")

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert resolved.report.integrity == INTEGRITY_UNPROVED
    assert (
        f"{runtime.FINDING_GENERATION_MEMBER_REJECTED}:graph.db:symlink"
        in resolved.report.findings
    )
    assert resolved.config is None


def test_a_forged_manifest_path_is_refused_in_the_activated_lifecycle_too(
    published, pointer, forbid_opens
):
    """Checked before the attestation, and before anything is opened.

    Which is the honest order: a manifest naming a path outside the generation
    is a refusal about the manifest, and reporting it as a digest mismatch would
    describe the symptom rather than what was wrong.
    """
    write_pointer(pointer, manifest_sha256=attest(published))
    body, _findings = read_manifest(published)
    body["artifacts"] = {**body["artifacts"], "../escape.txt": {"sha256": "0" * 64}}
    write_manifest(published, body)

    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_INVALID
    assert (
        f"{runtime.FINDING_MANIFEST_PATH_REJECTED}:artifacts:escape"
        in resolved.report.findings
    )
    assert resolved.config is None


# ---------------------------------------------------------------------------
# Activate: dry run by default, one atomic write when asked
# ---------------------------------------------------------------------------

def test_activate_dry_run_is_the_default_and_writes_nothing(
    published, control_root, pointer
):
    before = tree_state(control_root)

    report = activate_generation(pointer=pointer, generation=published)

    assert report["status"] == STATUS_CUTOVER_PLANNED
    assert report["dry_run"] is True
    assert report["exit_code"] == runtime.EXIT_OK
    assert report["resolution"]["usable"] is True
    assert not pointer.exists()
    assert temp_files(control_root) == []
    assert tree_state(control_root) == before


def test_activate_dry_run_creates_no_control_root(published, tmp_path):
    missing = tmp_path / "no-such-control-root"

    report = activate_generation(
        pointer=missing / "active.json", generation=published
    )

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert f"{runtime.FINDING_CONTROL_ROOT_REJECTED}:absent" in report["findings"]
    assert not missing.exists()


def test_activate_apply_writes_one_atomic_pointer_that_resolves_active(
    published, control_root, pointer
):
    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_APPLIED
    assert report["dry_run"] is False
    assert report["exit_code"] == runtime.EXIT_OK
    assert pointer.is_file() and not pointer.is_symlink()
    assert temp_files(control_root) == []

    resolved = resolve_generation_runtime(pointer=pointer)
    assert resolved.report.state == STATE_ACTIVE
    assert resolved.report.relative_generation == "migrated/current"
    assert resolved.usable is True
    assert resolved.config.graph_db_path == published / "graph.db"


def test_the_written_pointer_is_the_documented_json_contract(
    published, control_root, pointer
):
    """The reverse probe for the control plane: read the file, not the API."""
    activate_generation(pointer=pointer, generation=published, apply=True)

    body = json.loads(pointer.read_text(encoding="utf-8"))

    assert body["pointer_version"] == POINTER_VERSION
    assert body["tool"] == "layered_memory_mcp.graph_generation_runtime"
    # Computed here from the two explicit paths, not read back from the tool.
    assert body["generation"] == str(published.relative_to(control_root).as_posix())
    assert body["previous_generation"] == ""
    assert len(body["manifest_sha256"]) == 64
    assert body["activated_at"].endswith("+00:00")
    # A pointer is a control-plane file: it names paths, never memory content.
    assert "rebuilt nightly" not in json.dumps(body)


def test_a_repeated_activate_is_a_no_op_that_writes_nothing(
    published, destination, control_root, pointer
):
    """Nothing is written: not the pointer, not a record, not a generation byte.

    The snapshot is the whole control root including every directory's own
    modification time, so "writes nothing" means exactly that and not "writes
    nothing I chose to look at".
    """
    activate_generation(pointer=pointer, generation=published, apply=True)
    before = tree_state(control_root)
    pointer_before = pointer.read_bytes()

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_UNCHANGED
    assert report["exit_code"] == runtime.EXIT_OK
    assert tree_state(control_root) == before
    assert pointer.read_bytes() == pointer_before
    assert temp_files(control_root) == []
    assert not runtime.pointer_lock_path(pointer).exists()


def test_activate_refuses_a_tampered_generation_and_writes_no_pointer(
    published, control_root, pointer
):
    (published / "L0.md").write_text("tampered\n", encoding="utf-8")
    before = tree_state(control_root)

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert runtime.FINDING_GENERATION_NOT_INTACT in report["findings"]
    assert not pointer.exists()
    # An applying refusal decides inside the pointer's interval, so the control
    # root's own mtime carries the lock file's creation and removal; nothing
    # else differs and no lock survives.
    assert changed_paths(before, tree_state(control_root)) <= {ROOT_KEY}
    assert not runtime.pointer_lock_path(pointer).exists()
    assert temp_files(control_root) == []


def test_activate_refuses_a_generation_outside_the_control_root(
    published, tmp_path, pointer
):
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    report = activate_generation(pointer=pointer, generation=outside, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert (
        f"{runtime.FINDING_GENERATION_OUTSIDE_CONTROL_ROOT}:escape"
        in report["findings"]
    )
    assert not pointer.exists()


def test_activate_refuses_a_generation_reached_through_a_symlink(
    published, control_root, pointer
):
    """Inside the control root by name, somewhere else in fact."""
    link = control_root / "linked-current"
    link.symlink_to(published, target_is_directory=True)

    report = activate_generation(pointer=pointer, generation=link, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert (
        f"{runtime.FINDING_GENERATION_OUTSIDE_CONTROL_ROOT}:symlink"
        in report["findings"]
    )
    assert not pointer.exists()


def test_activate_refuses_to_write_through_a_symlinked_pointer(
    published, control_root, pointer
):
    """The one file this tool writes may not be a link out of the control root."""
    elsewhere = control_root.parent / "someone-elses-pointer.json"
    elsewhere.write_text("{}\n", encoding="utf-8")
    pointer.symlink_to(elsewhere)

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert runtime.FINDING_POINTER_SYMLINK in report["findings"]
    assert pointer.is_symlink()
    assert elsewhere.read_text(encoding="utf-8") == "{}\n"


def test_activate_refuses_a_pointer_path_that_is_a_directory(
    published, control_root, pointer
):
    pointer.mkdir()

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert runtime.FINDING_POINTER_NOT_A_FILE in report["findings"]
    assert pointer.is_dir()


def test_activate_over_an_unparseable_pointer_fails_closed_and_keeps_the_bytes(
    published, control_root, pointer
):
    """A corrupt control file is evidence, not something to overwrite.

    The gap this closes: silently replacing it destroyed the only record of what
    the system thought it was serving, and with it the superseded target a
    restore would have needed. The operator keeps both the refusal and the file.
    """
    corrupt = "}}} not a pointer"
    pointer.write_text(corrupt, encoding="utf-8")
    before = tree_state(control_root)

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert runtime.FINDING_SUPERSEDED_POINTER_INVALID in report["findings"]
    # Byte-identical, and no backup copy was left lying beside it either.
    assert pointer.read_text(encoding="utf-8") == corrupt
    assert temp_files(control_root) == []
    assert changed_paths(before, tree_state(control_root)) <= {ROOT_KEY}
    assert not runtime.pointer_lock_path(pointer).exists()


def test_a_repaired_pointer_activates_again(published, control_root, pointer):
    """Fail-closed is recoverable: moving the corrupt file away is the repair."""
    pointer.write_text("}}} not a pointer", encoding="utf-8")
    assert activate_generation(
        pointer=pointer, generation=published, apply=True
    )["status"] == STATUS_CUTOVER_REFUSED

    pointer.rename(control_root / "active.json.corrupt")
    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_APPLIED
    assert report["previous"]["generation"] == ""
    assert resolve_generation_runtime(pointer=pointer).usable is True


def test_activate_over_a_pointer_of_an_unknown_version_fails_closed(
    published, control_root, pointer
):
    """The same refusal for a pointer a *newer* writer wrote: not ours to edit."""
    write_pointer(pointer, pointer_version=POINTER_VERSION + 1)
    before = tree_state(control_root)

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert runtime.FINDING_SUPERSEDED_POINTER_INVALID in report["findings"]
    assert changed_paths(before, tree_state(control_root)) <= {ROOT_KEY}
    assert not runtime.pointer_lock_path(pointer).exists()


def test_activate_retains_the_pointer_it_supersedes(
    source, destination, control_root, published, pointer
):
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)

    report = activate_generation(pointer=pointer, generation=current, apply=True)

    assert report["status"] == STATUS_CUTOVER_APPLIED
    body = json.loads(pointer.read_text(encoding="utf-8"))
    assert body["generation"] == current.relative_to(control_root).as_posix()
    assert body["previous_generation"] == withdrawn.relative_to(
        control_root
    ).as_posix()
    assert report["previous"]["generation"] == body["previous_generation"]
    # Nothing was deleted to make room for the new pointer.
    assert withdrawn.is_dir() and current.is_dir()


# ---------------------------------------------------------------------------
# Rollback: a retained pointer, restored
# ---------------------------------------------------------------------------

def test_a_restored_rollback_generation_reports_the_rollback_state(
    source, destination, published, pointer
):
    withdrawn, _current = withdraw_and_republish(source, destination)

    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.report.state == STATE_ROLLBACK
    assert resolved.report.status == STATUS_RUNTIME_ROLLBACK
    assert resolved.usable is True


def test_restore_returns_to_the_retained_generation_and_deletes_nothing(
    source, destination, control_root, published, pointer
):
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    activate_generation(pointer=pointer, generation=current, apply=True)

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_APPLIED
    assert report["exit_code"] == runtime.EXIT_OK
    body = json.loads(pointer.read_text(encoding="utf-8"))
    assert body["generation"] == withdrawn.relative_to(control_root).as_posix()
    # The restore is itself reversible: the generation we left is retained.
    assert body["previous_generation"] == current.relative_to(control_root).as_posix()
    assert withdrawn.is_dir() and current.is_dir()
    assert resolve_generation_runtime(pointer=pointer).usable is True


def test_restore_dry_run_writes_nothing(
    source, destination, control_root, published, pointer
):
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    activate_generation(pointer=pointer, generation=current, apply=True)
    before = tree_state(control_root)

    report = restore_generation(pointer=pointer)

    assert report["status"] == STATUS_RESTORE_PLANNED
    assert report["dry_run"] is True
    assert report["resolution"]["usable"] is True
    assert tree_state(control_root) == before


def test_restore_with_nothing_retained_is_a_no_op(published, control_root, pointer):
    activate_generation(pointer=pointer, generation=published, apply=True)
    before = tree_state(control_root)
    pointer_before = pointer.read_bytes()

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_NOTHING
    assert report["exit_code"] == runtime.EXIT_OK
    assert tree_state(control_root) == before
    assert pointer.read_bytes() == pointer_before
    assert not runtime.pointer_lock_path(pointer).exists()


def test_restore_without_a_pointer_is_a_no_op(control_root, pointer):
    before = tree_state(control_root)

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_NOTHING
    assert runtime.FINDING_POINTER_ABSENT in report["warnings"]
    # An absent pointer stays absent, and nothing whatsoever is brought into
    # existence to discover that.
    assert tree_state(control_root) == before
    assert not pointer.exists()
    assert not runtime.pointer_lock_path(pointer).exists()
    assert temp_files(control_root) == []


def test_restore_refuses_a_tampered_retained_generation(
    source, destination, control_root, published, pointer
):
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    activate_generation(pointer=pointer, generation=current, apply=True)
    (withdrawn / "manifest.json").write_text("{}", encoding="utf-8")
    before = tree_state(control_root)

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_REFUSED
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert changed_paths(before, tree_state(control_root)) <= {ROOT_KEY}
    assert not runtime.pointer_lock_path(pointer).exists()


def test_restore_checks_the_retained_digest_against_the_retained_target(
    source, destination, control_root, published, pointer
):
    """The gap this closes: a restore that *replaced* the retained attestation
    with whatever it read at the target proved nothing about identity.

    The retained digest is the record that this generation was once activated
    after a full publication proof. A target whose manifest no longer matches it
    is a different generation under the same name, and is refused.
    """
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    activate_generation(pointer=pointer, generation=current, apply=True)
    body = json.loads(pointer.read_text(encoding="utf-8"))
    assert len(body["previous_manifest_sha256"]) == 64
    write_pointer(pointer, **{**body, "previous_manifest_sha256": "0" * 64})
    before = tree_state(control_root)

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_REFUSED
    assert runtime.FINDING_POINTER_MANIFEST_CHANGED in report["findings"]
    assert changed_paths(before, tree_state(control_root)) <= {ROOT_KEY}
    assert not runtime.pointer_lock_path(pointer).exists()


def test_restore_refuses_a_retained_generation_nothing_attested(
    source, destination, control_root, published, pointer
):
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    activate_generation(pointer=pointer, generation=current, apply=True)
    body = json.loads(pointer.read_text(encoding="utf-8"))
    write_pointer(pointer, **{**body, "previous_manifest_sha256": ""})

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_REFUSED
    assert runtime.FINDING_POINTER_ATTESTATION in report["findings"]


def test_restore_carries_the_retained_digest_forward_unchanged(
    source, destination, published, pointer
):
    """What the restored pointer attests is the proof that was actually run.

    Recording the digest read back from the target instead would launder a
    replaced manifest into a fresh attestation no proof ever stood behind.
    """
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    activate_generation(pointer=pointer, generation=current, apply=True)
    retained = json.loads(pointer.read_text(encoding="utf-8"))[
        "previous_manifest_sha256"
    ]

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_APPLIED
    body = json.loads(pointer.read_text(encoding="utf-8"))
    assert body["manifest_sha256"] == retained == attest(withdrawn)


# ---------------------------------------------------------------------------
# Confirmation: what was written is what was asked for
#
# "We wrote a pointer" and "the pointer serves the generation this report names"
# are different claims, and the second is the one an operator acts on. A
# confirmation that only asks whether *some* generation is usable turns a
# cutover that landed on another target into a success report, which is exactly
# what an independent probe observed: ``generation_cutover_applied`` at the top
# level with a different generation in ``resolution``.
# ---------------------------------------------------------------------------

def test_a_confirmation_resolving_another_usable_generation_is_not_applied(
    source, destination, control_root, published, pointer, monkeypatch
):
    """A post-write resolution that is usable but *not the target* is a failure.

    The diverted write below stands in for the pointer having moved between the
    replace and the confirmation. The generation it names is genuinely usable —
    it was activated and attested a moment earlier — so ``usable`` alone cannot
    tell the two situations apart. The requested target and the attestation the
    payload carries can, and are what must be compared.
    """
    withdrawn, current = withdraw_and_republish(source, destination)
    assert activate_generation(
        pointer=pointer, generation=withdrawn, apply=True
    )["status"] == STATUS_CUTOVER_APPLIED
    served = json.loads(pointer.read_text(encoding="utf-8"))

    real_write = runtime._write_pointer

    def divert(path: Path, _payload) -> None:
        real_write(path, runtime.ActivePointer(**served))

    monkeypatch.setattr(runtime, "_write_pointer", divert)

    report = activate_generation(pointer=pointer, generation=current, apply=True)

    assert report["status"] != STATUS_CUTOVER_APPLIED
    assert report["status"] == runtime.STATUS_CUTOVER_UNVERIFIED
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert (
        f"{runtime.FINDING_POINTER_CONFIRMATION_DRIFT}:generation"
        in report["findings"]
    )
    # The report may not carry a resolution of one generation under the name of
    # another: the two paths it prints have to be the same path.
    assert report["resolution"]["generation"] == str(withdrawn)
    assert report["generation"] == str(current)
    assert report["resolution"]["usable"] is True


def test_a_restore_confirmation_resolving_another_generation_is_not_applied(
    source, destination, control_root, published, pointer, monkeypatch
):
    """The same exactness on the restore path, which writes the same pointer."""
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    activate_generation(pointer=pointer, generation=current, apply=True)
    served = json.loads(pointer.read_text(encoding="utf-8"))

    real_write = runtime._write_pointer

    def divert(path: Path, _payload) -> None:
        real_write(path, runtime.ActivePointer(**served))

    monkeypatch.setattr(runtime, "_write_pointer", divert)

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] != STATUS_RESTORE_APPLIED
    assert report["status"] == runtime.STATUS_CUTOVER_UNVERIFIED
    assert (
        f"{runtime.FINDING_POINTER_CONFIRMATION_DRIFT}:generation"
        in report["findings"]
    )
    assert report["resolution"]["generation"] == str(current)


def test_a_confirmation_resolving_an_unattested_target_is_not_applied(
    published, control_root, pointer, monkeypatch
):
    """The attestation is compared too, not only the path.

    A pointer naming the right generation with a digest no proof stood behind is
    the bypass the state contract exists to remove; a cutover must not report
    success over one just because the path matches.
    """
    real_write = runtime._write_pointer

    def strip_attestation(path: Path, payload) -> None:
        body = payload.to_dict()
        body["manifest_sha256"] = "0" * 64
        real_write(path, runtime.ActivePointer(**body))

    monkeypatch.setattr(runtime, "_write_pointer", strip_attestation)

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == runtime.STATUS_CUTOVER_UNVERIFIED
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert (
        f"{runtime.FINDING_POINTER_CONFIRMATION_DRIFT}:attestation"
        in report["findings"]
    )


# ---------------------------------------------------------------------------
# A preview is a preview, including of the control plane
#
# The interval lock and the admission ledger are writes, and a tool that
# created either while reporting what it *would* do would have made "dry run"
# mean "almost". The baselines below cover the complete tree — every member,
# its size, its modification time in nanoseconds and its content hash — and are
# taken both before anything has been served and after a real activation, so a
# preview is pinned against a destination that already has control artifacts in
# it as well as one that has none.
# ---------------------------------------------------------------------------

def control_artifacts(control_root: Path, destination: Path, pointer: Path) -> dict:
    """Every control-plane artifact the two tools own, as presence flags."""
    return {
        "pointer": pointer.exists(),
        "pointer_lock": runtime.pointer_lock_path(pointer).exists(),
        "guard_lock": generation_module.served_lock_path(destination).exists(),
        "ledger": (destination / generation_module.SERVED_DIR).exists(),
        "temporary": temp_files(control_root),
    }


def test_every_preview_leaves_the_whole_control_plane_untouched(
    source, destination, control_root, published, pointer
):
    """Status, plan, verify and both dry-run cutovers, against one baseline.

    The baseline is taken **before this control plane has ever been applied
    to** — no pointer, no ledger, no lock has ever existed here — so "a preview
    changed nothing" cannot be an artefact of comparing two already-written
    states. The post-apply case is the next test. The snapshot includes every
    directory's own modification time, including the control root's, which is
    the only way a file created and removed at the top level is visible at all.
    """
    baseline = tree_state(control_root)
    source_baseline = tree_state(source)
    artifacts = control_artifacts(control_root, destination, pointer)
    assert artifacts == {
        "pointer": False,
        "pointer_lock": False,
        "guard_lock": False,
        "ledger": False,
        "temporary": [],
    }

    reports = [
        status_generation(generation=published),
        status_generation(pointer=pointer),
        activate_generation(pointer=pointer, generation=published),
        restore_generation(pointer=pointer),
        graph_migration.plan_migration(source=source, destination=destination),
        graph_migration.verify_migration(destination=destination, source=source),
    ]

    assert [report["dry_run"] for report in reports[:4]] == [True] * 4
    assert tree_state(control_root) == baseline
    assert control_artifacts(control_root, destination, pointer) == artifacts
    assert tree_state(source) == source_baseline


def test_a_preview_after_a_real_activation_touches_no_control_artifact(
    source, destination, control_root, published, pointer
):
    """The same claim where the ledger, the pointer and a write already exist."""
    activate_generation(pointer=pointer, generation=published, apply=True)
    write_into(published, pointer)
    baseline = tree_state(control_root)
    artifacts = control_artifacts(control_root, destination, pointer)
    assert artifacts["pointer"] is True and artifacts["ledger"] is True
    assert artifacts["pointer_lock"] is False and artifacts["guard_lock"] is False

    previews = [
        status_generation(pointer=pointer),
        activate_generation(pointer=pointer, generation=published),
        restore_generation(pointer=pointer),
    ]

    assert [report["dry_run"] for report in previews] == [True] * 3
    assert tree_state(control_root) == baseline
    assert control_artifacts(control_root, destination, pointer) == artifacts


def test_an_applied_operation_leaves_no_lock_file_behind(
    source, destination, control_root, published, pointer
):
    """Neither tool leaves a lock file behind after its operation."""
    withdrawn, current = withdraw_and_republish(source, destination)
    for step in (
        lambda: activate_generation(pointer=pointer, generation=withdrawn, apply=True),
        lambda: activate_generation(pointer=pointer, generation=current, apply=True),
        lambda: restore_generation(pointer=pointer, apply=True),
        lambda: graph_migration.rollback_migration(destination=destination),
    ):
        step()
        assert not runtime.pointer_lock_path(pointer).exists()
        assert not generation_module.served_lock_path(destination).exists()
    assert temp_files(control_root) == []


def test_an_applying_no_op_writes_nothing_in_the_whole_control_plane(
    source, destination, control_root, published, pointer
):
    """What an applying no-op does and does not do, stated exactly.

    Three operations that the contract calls no-ops, each run with ``--apply``
    against a state it has nothing to change: re-activating the generation
    already being served, restoring when nothing is retained, and withdrawing
    from a destination with nothing published. For all three, no byte of data,
    no pointer, no admission record, no temporary file and no subdirectory
    changes, and no lock survives — and the control root itself is unchanged.

    The last clause caught the original regression: creating and removing the
    pointer's interval file changed the control root's own modification time.
    Pointer decisions must stay serialized, but now lock a read-only directory
    descriptor instead. The complete equality remains the contract: restoring
    mtime afterwards or dropping the directory from the snapshot would hide a
    real mutation instead of accounting for it.

    ``graph_migration rollback`` answers "nothing is published here" from the
    target's absence, before it takes the generation guard, so it already is a
    no-op in the strict sense — not one key of the destination's snapshot
    differs.
    """
    rolled = graph_migration.rollback_migration(destination=destination)
    assert rolled["status"] == graph_migration.STATUS_ROLLED_BACK
    withdrawn = Path(rolled["generation"]["withdrawn_to"])
    assert activate_generation(
        pointer=pointer, generation=withdrawn, apply=True
    )["status"] == STATUS_CUTOVER_APPLIED
    # Serving a retained generation, with nothing retained behind it and
    # nothing published in the destination: all three operations below have
    # exactly nothing to do.
    assert not published.exists()

    pointer_before = pointer.read_bytes()
    generations_before = {
        path: tree_state(path) for path in (withdrawn, destination / "rollback")
    }
    control_before = tree_state(control_root)
    destination_before = tree_state(destination)

    repeated = activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    restored = restore_generation(pointer=pointer, apply=True)
    withdrawal = graph_migration.rollback_migration(destination=destination)

    assert repeated["status"] == STATUS_CUTOVER_UNCHANGED
    assert restored["status"] == STATUS_RESTORE_NOTHING
    assert withdrawal["status"] == graph_migration.STATUS_ROLLBACK_NOTHING
    # The business data: byte for byte, member for member, mtime for mtime.
    assert pointer.read_bytes() == pointer_before
    for path, before in generations_before.items():
        assert tree_state(path) == before, path
    assert temp_files(control_root) == []
    assert not runtime.pointer_lock_path(pointer).exists()
    assert not generation_module.served_lock_path(destination).exists()
    # The destination, whose guard was never taken, is strictly identical.
    assert tree_state(destination) == destination_before
    # Measured: whatever differs in the control root is the directory itself and
    # nothing else — no member, no subdirectory, no byte.
    assert changed_paths(control_before, tree_state(control_root)) <= {ROOT_KEY}
    # The contract, unweakened: an applying no-op writes nothing at all.
    assert tree_state(control_root) == control_before


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_an_unusable_interval_lock_path_refuses_and_writes_nothing(
    published, control_root, pointer, tmp_path, kind
):
    """The lock file is a control artifact, so its path is guarded like one.

    A symlink there would serialise somebody else's file while looking exactly
    like serialisation, which is worse than no lock at all: the operation would
    report success having held nothing.
    """
    lock = runtime.pointer_lock_path(pointer)
    if kind == "symlink":
        elsewhere = tmp_path / "lock-elsewhere"
        elsewhere.write_text("", encoding="utf-8")
        lock.symlink_to(elsewhere)
        expected = "symlink"
    else:
        lock.mkdir()
        expected = "not_a_file"
    before = tree_state(control_root)

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert (
        f"{runtime.FINDING_POINTER_LOCK}:"
        f"{generation_module.FINDING_LOCK_PATH_REJECTED}:{expected}"
        in report["findings"]
    )
    assert not pointer.exists()
    assert tree_state(control_root) == before


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_an_unusable_generation_guard_path_refuses_and_writes_no_pointer(
    published, destination, control_root, pointer, tmp_path, kind
):
    """The same for the guard the admission is recorded under."""
    lock = generation_module.served_lock_path(destination)
    if kind == "symlink":
        link = tmp_path / "guard-elsewhere"
        link.write_text("", encoding="utf-8")
        lock.symlink_to(link)
        reason = "symlink"
    else:
        lock.mkdir()
        reason = "not_a_file"
    before = tree_state(control_root)

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert (
        f"{runtime.FINDING_GENERATION_GUARD}:"
        f"{generation_module.FINDING_LOCK_PATH_REJECTED}:{reason}"
        in report["findings"]
    )
    assert not pointer.exists()
    assert tree_state(control_root) == before
    assert not runtime.pointer_lock_path(pointer).exists()
    assert temp_files(control_root) == []


def test_an_existing_reserved_guard_file_is_never_opened_or_removed(
    published, destination, control_root, pointer, monkeypatch
):
    """Legacy regular files remain validated, but do not carry the guard."""
    lock = generation_module.served_lock_path(destination)
    lock.write_bytes(b"legacy guard sentinel\n")
    before = lock.stat()
    real_open = generation_module.os.open

    def open_guard(path, flags, *args, **kwargs):
        assert Path(path) != lock
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(generation_module.os, "open", open_guard)
    report = activate_generation(pointer=pointer, generation=published, apply=True)
    assert report["status"] == STATUS_CUTOVER_APPLIED
    assert lock.read_bytes() == b"legacy guard sentinel\n"
    after = lock.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino, before.st_mtime_ns, before.st_ctime_ns
    )
    control_before = tree_state(control_root)
    withdrawal = graph_migration.rollback_migration(destination=destination)
    repeated = activate_generation(pointer=pointer, generation=published, apply=True)
    assert withdrawal["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in withdrawal["findings"]
    assert repeated["status"] == STATUS_CUTOVER_UNCHANGED
    assert tree_state(control_root) == control_before


def test_a_reentrant_interval_is_refused_rather_than_deadlocked(
    source, destination, control_root, published, pointer
):
    """A nested pointer operation is a fault, and faults must not hang.

    Injected on the thread that already holds the interval, which is the one
    case a plain mutex answers by stopping the process. It is named and refused
    instead, and the operation that legitimately holds the interval still
    completes.
    """
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    nested: list[dict] = []
    real_write = runtime._write_pointer

    def write(path: Path, payload):
        nested.append(restore_generation(pointer=pointer, apply=True))
        return real_write(path, payload)

    try:
        runtime._write_pointer = write
        report = activate_generation(pointer=pointer, generation=current, apply=True)
    finally:
        runtime._write_pointer = real_write

    assert nested[0]["status"] == STATUS_RESTORE_REFUSED
    assert f"{runtime.FINDING_POINTER_LOCK}:reentrant" in nested[0]["findings"]
    assert report["status"] == STATUS_CUTOVER_APPLIED
    assert resolve_generation_runtime(pointer=pointer).config.root == current


# ---------------------------------------------------------------------------
# Admission: a generation a runtime has been given is not the migration's to
# move
#
# The second proven defect. ``graph_migration rollback`` withdrew a generation
# an active pointer was serving — the generation verified, because nothing had
# written into it yet, and the withdrawal was therefore allowed — leaving the
# pointer naming a directory that no longer existed. The rollback verb has no
# pointer argument and must not grow one: a missing argument cannot prove that
# nothing references a generation. What *can* is a durable record, written
# where the withdrawal looks, before the pointer is published.
# ---------------------------------------------------------------------------

def destination_entries(destination: Path) -> list[str]:
    return sorted(path.name for path in destination.iterdir())


def admission_markers(destination: Path) -> list[Path]:
    served = destination / generation_module.SERVED_DIR
    return sorted(served.glob("*.json")) if served.is_dir() else []


def test_activation_records_its_admission_in_the_destination(
    published, destination, control_root, pointer
):
    """Where the record lives, and that the migration tool still owns the tree.

    Not inside the generation: a served generation is writable, its manifest is
    the attestation a pointer binds, and a tool dropping its own bookkeeping in
    there would be editing the thing it is making claims about. Not derived
    from the pointer either — the withdrawal has no pointer. It goes in the
    destination's own control plane, which is the one place both operations can
    reach from what they are each given.
    """
    report = activate_generation(pointer=pointer, generation=published, apply=True)
    assert report["status"] == STATUS_CUTOVER_APPLIED

    markers = admission_markers(destination)
    assert len(markers) == 1
    assert markers[0].is_file() and not markers[0].is_symlink()
    record = json.loads(markers[0].read_text(encoding="utf-8"))
    assert record["manifest_sha256"] == attest(published) == markers[0].stem
    assert record["generation"] == "current"
    assert record["tool"] == runtime.TOOL_NAME
    # Bounded: a control artifact names paths and digests, never memory.
    assert "rebuilt nightly" not in json.dumps(record)
    # The destination inventory still owns every name in there, including this
    # one, and still refuses anything else.
    findings, _warnings = generation_module.destination_findings(destination)
    assert findings == []
    assert generation_module.SERVED_DIR in destination_entries(destination)
    # The admission is in the destination, not in the attested tree.
    assert not (published / generation_module.SERVED_DIR).exists()
    assert attest(published) == report["resolution"]["manifest_sha256"]


def test_rollback_refuses_to_withdraw_an_activated_generation(
    source, destination, control_root, published, pointer
):
    """The availability failure, as a test: no withdrawal, no dangling pointer.

    The generation here is *unchanged* — it was activated and nothing has
    written into it — so it still verifies, which is exactly why the old
    refusals did not catch it.
    """
    assert activate_generation(
        pointer=pointer, generation=published, apply=True
    )["status"] == STATUS_CUTOVER_APPLIED
    pointer_before = pointer.read_text(encoding="utf-8")
    generation_before = tree_state(published)
    source_before = tree_state(source)

    report = graph_migration.rollback_migration(destination=destination)

    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert report["exit_code"] == graph_migration.EXIT_BLOCKED
    assert generation_module.FINDING_GENERATION_SERVED in report["findings"]
    # Nothing moved: the source, the generation and the pointer are as they were.
    assert tree_state(source) == source_before
    assert tree_state(published) == generation_before
    assert pointer.read_text(encoding="utf-8") == pointer_before
    assert published.is_dir()
    assert not (destination / "rollback").exists()
    # And the runtime is still serving it, which is the point.
    assert resolve_generation_runtime(pointer=pointer).usable is True


def test_the_rollback_cli_refuses_an_activated_generation_without_a_pointer(
    destination, published, pointer, capsys
):
    """The CLI has no ``--pointer``, and must not need one to refuse."""
    activate_generation(pointer=pointer, generation=published, apply=True)

    code = graph_migration.main(["rollback", "--destination", str(destination)])
    report = json.loads(capsys.readouterr().out)

    assert code == graph_migration.EXIT_BLOCKED
    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in report["findings"]
    assert published.is_dir()


def test_rollback_refuses_a_generation_retained_only_for_restore(
    source, tmp_path, control_root, pointer
):
    """Still referenced, just not as the current one — and still refused.

    Three real destinations in one control root. The first generation is
    activated, then superseded, then superseded again, so by the end it is
    neither served nor retained for restore and the pointer is in neither
    destination. The record of its admission is durable and conservative: it
    was given to a runtime once, and a legacy withdrawal is not the operation
    that gets to decide that nothing is using it any more.
    """
    first = graph_migration.apply_migration(
        source=source, destination=control_root / "one"
    )
    second = graph_migration.apply_migration(
        source=source, destination=control_root / "two"
    )
    third = graph_migration.apply_migration(
        source=source, destination=control_root / "three"
    )
    assert {first["status"], second["status"], third["status"]} == {
        graph_migration.STATUS_APPLIED
    }
    one = control_root / "one" / "current"
    two = control_root / "two" / "current"
    three = control_root / "three" / "current"
    activate_generation(pointer=pointer, generation=one, apply=True)
    activate_generation(pointer=pointer, generation=two, apply=True)

    # ``one`` is now retained only as the restore target.
    retained = graph_migration.rollback_migration(destination=control_root / "one")
    assert retained["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in retained["findings"]
    assert one.is_dir()

    # ``two`` is the served one.
    served = graph_migration.rollback_migration(destination=control_root / "two")
    assert served["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in served["findings"]
    assert two.is_dir()

    # And ``one`` once the pointer no longer mentions it at all.
    activate_generation(pointer=pointer, generation=three, apply=True)
    body = json.loads(pointer.read_text(encoding="utf-8"))
    assert body["generation"] == "three/current"
    assert body["previous_generation"] == "two/current"
    unreferenced = graph_migration.rollback_migration(destination=control_root / "one")
    assert unreferenced["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in unreferenced["findings"]
    assert one.is_dir()
    assert resolve_generation_runtime(pointer=pointer).config.root == three


def test_an_admission_survives_a_real_write_a_reopen_and_a_restore(
    source, destination, control_root, published, pointer
):
    """The record has to outlive everything a served generation goes through.

    A writable runtime changes ``graph.db``, ``journal.db``, a readable
    document, ``L0.md`` and the vector rows — but never ``manifest.json``,
    which is why the admission is keyed on the manifest digest the pointer
    attests. Reopening, re-activating and restoring must all leave it standing.
    """
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    write_into(withdrawn, pointer, node_id="claim_admission_rehearsal")
    activate_generation(pointer=pointer, generation=current, apply=True)
    write_into(current, pointer)
    assert activate_generation(
        pointer=pointer, generation=current, apply=True
    )["status"] == STATUS_CUTOVER_UNCHANGED
    assert restore_generation(pointer=pointer, apply=True)["status"] == (
        STATUS_RESTORE_APPLIED
    )

    # Both generations were admitted, both were written into, and both are
    # still out of the legacy withdrawal's reach.
    assert len(admission_markers(destination)) == 2
    report = graph_migration.rollback_migration(destination=destination)
    assert report["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in report["findings"]
    assert current.is_dir() and withdrawn.is_dir()
    reopened = resolve_generation_runtime(pointer=pointer)
    assert reopened.usable is True
    assert reopened.config.root == withdrawn


def test_a_restore_onto_an_unadmitted_target_records_both_admissions(
    source, destination, control_root, published, pointer
):
    """A restore publishes a pointer too, so it admits what that pointer names.

    The pointer here is hand-written, which is the only way either generation
    can be one no activation ever recorded. A published pointer names two: the
    one being served and the one retained so the move is reversible. Both have
    been given to a runtime, so both go out of the legacy withdrawal's reach —
    withdrawing the retained one would turn a reversible restore into a one-way
    trip while reporting success.
    """
    withdrawn, current = withdraw_and_republish(source, destination)
    write_pointer(
        pointer,
        generation=current.relative_to(control_root).as_posix(),
        manifest_sha256=attest(current),
        previous_generation=withdrawn.relative_to(control_root).as_posix(),
        previous_activated_at="2026-10-28T00:00:00+00:00",
        previous_manifest_sha256=attest(withdrawn),
    )
    assert admission_markers(destination) == []

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_APPLIED
    recorded = {
        json.loads(path.read_text(encoding="utf-8"))["manifest_sha256"]
        for path in admission_markers(destination)
    }
    assert recorded == {attest(withdrawn), attest(current)}
    rolled = graph_migration.rollback_migration(destination=destination)
    assert rolled["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in rolled["findings"]
    assert current.is_dir() and withdrawn.is_dir()
    # Still reversible, which is what the retained admission protects.
    assert restore_generation(pointer=pointer, apply=True)["status"] == (
        STATUS_RESTORE_APPLIED
    )
    assert resolve_generation_runtime(pointer=pointer).config.root == current


# ---------------------------------------------------------------------------
# The retained generation's admission is a refusal point, not best effort
#
# A published pointer names *two* generations: the one being served and the one
# kept so the move is reversible. Both have been given to a runtime, so both
# must be out of a legacy withdrawal's reach before the pointer naming them
# exists. The gap these pin: the retained half used to be recorded on a
# best-effort basis, so a pointer could be published whose retained generation
# was provably unprotected — an unreadable ledger, an unobtainable guard, a
# failed write, a target that had been moved, one outside the control root and
# one the pointer carried no attestation for all ended in a *success* report.
#
# Fail closed in every one of those cases, and keep everything: the prior
# pointer byte for byte, both generations, and the source. "No retained
# generation" stays the ordinary first-cutover answer, and a retained
# generation that is already admitted is still checked against the binding the
# new pointer will attest — a record protects the tree it was written for, and
# a replaced or corrupted tree is not that one.
# ---------------------------------------------------------------------------

def publish_into(source: Path, control_root: Path, name: str) -> Path:
    """One real published generation at ``<control root>/<name>/current``.

    Separate destinations inside one control root is the layout the pointer
    contract allows, and it is what makes the failure injections below
    *targeted*: a broken ledger in the retained generation's destination says
    nothing about the destination the cutover is moving to.
    """
    report = graph_migration.apply_migration(
        source=source, destination=control_root / name
    )
    assert report["status"] == graph_migration.STATUS_APPLIED, report["findings"]
    return control_root / name / "current"


def plant_unreadable_ledger(destination: Path) -> str:
    """Make one destination's admission ledger unreadable, and say how.

    A planted file in the ledger: the closed set of names the migration tool
    owns is what makes "never admitted" an answer at all, so an entry it did
    not write makes the question unanswerable rather than negative.
    """
    served = destination / generation_module.SERVED_DIR
    served.mkdir(parents=True, exist_ok=True)
    (served / "planted.json").write_text("{}", encoding="utf-8")
    return f"{generation_module.FINDING_SERVED_REJECTED}:planted.json:unknown"


def retained_refusal(report: dict) -> list[str]:
    """The findings of a refusal that names the retained generation."""
    assert report["status"] in (STATUS_CUTOVER_REFUSED, STATUS_RESTORE_REFUSED), report
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert runtime.FINDING_RETAINED_ADMISSION in report["findings"], report["findings"]
    return report["findings"]


def test_an_unreadable_retained_ledger_refuses_the_cutover_and_keeps_the_pointer(
    source, control_root, pointer, monkeypatch
):
    """The core repair: the retained half of a pointer is a refusal point.

    The failure is injected **only** in the destination of the generation being
    retained, and the generation being activated is perfectly publishable — so
    nothing but the retained admission can be what refuses this.
    """
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    assert activate_generation(pointer=pointer, generation=one, apply=True)["status"] == (
        STATUS_CUTOVER_APPLIED
    )
    rejected = plant_unreadable_ledger(control_root / "one")
    pointer_before = pointer.read_bytes()
    target_before = tree_state(two)
    source_before = tree_state(source)
    writes: list[Path] = []
    monkeypatch.setattr(
        runtime, "_write_pointer", lambda path, payload: writes.append(path)
    )

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    # The pointer was never published, and not merely replaced by an equal one.
    assert writes == []
    assert rejected in retained_refusal(report)
    assert pointer.read_bytes() == pointer_before
    # Nothing was admitted in the destination the cutover was moving to either:
    # the retained generation is proved before anything is recorded.
    assert admission_markers(control_root / "two") == []
    assert tree_state(two) == target_before
    assert tree_state(source) == source_before
    # And the runtime is still serving exactly what it was serving.
    resolved = resolve_generation_runtime(pointer=pointer)
    assert resolved.usable is True and resolved.config.root == one


def test_the_repaired_retained_destination_admits_both_generations(
    source, control_root, pointer
):
    """Recovery: with the ledger readable again, both generations are recorded.

    And a legacy withdrawal is then refused in *both* destinations — the served
    one and the one retained only so the cutover can be undone.
    """
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    activate_generation(pointer=pointer, generation=one, apply=True)
    plant_unreadable_ledger(control_root / "one")
    assert activate_generation(pointer=pointer, generation=two, apply=True)[
        "status"
    ] == STATUS_CUTOVER_REFUSED

    (control_root / "one" / generation_module.SERVED_DIR / "planted.json").unlink()
    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert report["status"] == STATUS_CUTOVER_APPLIED
    assert {path.stem for path in admission_markers(control_root / "one")} == {
        attest(one)
    }
    assert {path.stem for path in admission_markers(control_root / "two")} == {
        attest(two)
    }
    for name in ("one", "two"):
        withdrawal = graph_migration.rollback_migration(
            destination=control_root / name
        )
        assert withdrawal["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
        assert generation_module.FINDING_GENERATION_SERVED in withdrawal["findings"]
    assert one.is_dir() and two.is_dir()
    assert resolve_generation_runtime(pointer=pointer).config.root == two


def test_an_already_admitted_retained_generation_is_still_checked_for_drift(
    source, control_root, pointer, monkeypatch
):
    """A record is not a substitute for the binding it was written for.

    ``one`` was activated, so it *is* in its destination's ledger. Its manifest
    is then replaced, which means the tree the new pointer would retain is no
    longer the tree that record describes and no longer the one the pointer's
    retained digest attests. An existing record must not let that past.
    """
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    activate_generation(pointer=pointer, generation=one, apply=True)
    assert {path.stem for path in admission_markers(control_root / "one")} == {
        attest(one)
    }
    (one / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    pointer_before = pointer.read_bytes()
    writes: list[Path] = []
    monkeypatch.setattr(
        runtime, "_write_pointer", lambda path, payload: writes.append(path)
    )

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert any(
        finding.startswith(runtime.FINDING_GENERATION_MOVED)
        for finding in retained_refusal(report)
    ), report["findings"]
    assert writes == []
    assert pointer.read_bytes() == pointer_before
    assert admission_markers(control_root / "two") == []


def test_a_retained_target_outside_the_control_root_is_not_silently_dropped(
    source, control_root, pointer, tmp_path
):
    """A hand-edited pointer naming a target it may not name is a refusal.

    The old behaviour dropped such a retained target from the protection it
    needed and published the pointer anyway — the one case where "we could not
    place it" quietly became "it does not need protecting".
    """
    two = publish_into(source, control_root, "two")
    write_pointer(
        pointer,
        generation="../outside/current",
        manifest_sha256="a" * 64,
        previous_generation="",
        previous_manifest_sha256="",
    )
    pointer_before = pointer.read_bytes()

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert (
        f"{runtime.FINDING_POINTER_TARGET_REJECTED}:escape" in retained_refusal(report)
    ), report["findings"]
    assert pointer.read_bytes() == pointer_before
    assert admission_markers(control_root / "two") == []


def test_a_retained_generation_with_no_attestation_is_not_silently_dropped(
    source, control_root, pointer
):
    """No recorded digest means no identity to protect, so no cutover.

    The pointer being superseded carries a target but no attestation for it.
    Publishing a new pointer that retained it would promise a restore onto a
    generation nothing attested and nothing recorded — refused instead.
    """
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    write_pointer(
        pointer,
        generation=one.relative_to(control_root).as_posix(),
        manifest_sha256="",
    )
    pointer_before = pointer.read_bytes()

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert runtime.FINDING_POINTER_ATTESTATION in retained_refusal(report)
    assert pointer.read_bytes() == pointer_before
    assert admission_markers(control_root / "one") == []
    assert admission_markers(control_root / "two") == []


def test_an_unobtainable_retained_guard_refuses_the_cutover(
    source, control_root, pointer, tmp_path
):
    """A guard that cannot be taken is an admission that was never recorded.

    Injected in the retained generation's destination only, and the lock path
    is refused rather than followed — exactly as it is for the served half.
    """
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    write_pointer(
        pointer,
        generation=one.relative_to(control_root).as_posix(),
        manifest_sha256=attest(one),
    )
    elsewhere = tmp_path / "guard-elsewhere"
    elsewhere.write_text("", encoding="utf-8")
    generation_module.served_lock_path(control_root / "one").symlink_to(elsewhere)
    pointer_before = pointer.read_bytes()

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert (
        f"{runtime.FINDING_GENERATION_GUARD}:"
        f"{generation_module.FINDING_LOCK_PATH_REJECTED}:symlink"
        in retained_refusal(report)
    ), report["findings"]
    assert pointer.read_bytes() == pointer_before
    assert admission_markers(control_root / "two") == []
    assert elsewhere.read_text(encoding="utf-8") == ""


def test_a_failed_retained_registration_refuses_the_cutover(
    source, control_root, pointer, monkeypatch
):
    """The write itself failing is a refusal named by its exception class."""
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    write_pointer(
        pointer,
        generation=one.relative_to(control_root).as_posix(),
        manifest_sha256=attest(one),
    )
    real_register = runtime.register_admission

    def register(destination: Path, generation: Path, digest: str, **kwargs):
        if generation == one:
            raise OSError("no space left on device")
        return real_register(destination, generation, digest, **kwargs)

    monkeypatch.setattr(runtime, "register_admission", register)
    pointer_before = pointer.read_bytes()

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert f"{runtime.FINDING_ADMISSION_FAILED}:OSError" in retained_refusal(report)
    assert pointer.read_bytes() == pointer_before
    assert admission_markers(control_root / "one") == []
    assert admission_markers(control_root / "two") == []
    assert not generation_module.served_lock_path(control_root / "one").exists()


def test_a_refusal_after_the_retained_half_was_recorded_says_so_and_keeps_it(
    source, control_root, pointer, monkeypatch
):
    """The one case with a real side effect, reported rather than reversed.

    The two records are not a transaction and this module does not pretend they
    are: the retained generation is recorded first, and if the real registration
    API then fails for the generation being served, that first record is already
    in the ledger. It stays. An admission says "a runtime was given this
    generation", which the later failure did not make untrue — the operator
    holds both generations either way — and deleting it to make the refusal look
    clean would re-expose the retained generation to a legacy withdrawal, which
    is the exact defect this whole path exists to close.

    So the refusal names it. What it must still guarantee is everything else:
    not one byte of the pointer, the target, the source or either generation
    changes, nothing is admitted in the destination it was moving to, and the
    retained generation is now provably protected.
    """
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    write_pointer(
        pointer,
        generation=one.relative_to(control_root).as_posix(),
        manifest_sha256=attest(one),
    )
    real_register = runtime.register_admission

    def register(destination: Path, generation: Path, digest: str, **kwargs):
        if generation == two:
            raise OSError("no space left on device")
        return real_register(destination, generation, digest, **kwargs)

    monkeypatch.setattr(runtime, "register_admission", register)
    pointer_before = pointer.read_bytes()
    one_before = tree_state(one)
    two_before = tree_state(two)
    source_before = tree_state(source)

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert report["status"] == STATUS_CUTOVER_REFUSED
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert f"{runtime.FINDING_ADMISSION_FAILED}:OSError" in report["findings"]
    # The disclosure: refused, *and* one record remains. Not the same state as
    # "refused, nothing happened", and the report does not claim to be.
    assert f"{runtime.FINDING_ADMISSION_KEPT}:previous" in report["findings"]
    # Nothing the operator owns moved.
    assert pointer.read_bytes() == pointer_before
    assert tree_state(one) == one_before
    assert tree_state(two) == two_before
    assert tree_state(source) == source_before
    assert admission_markers(control_root / "two") == []
    assert not generation_module.served_lock_path(control_root / "one").exists()
    assert not generation_module.served_lock_path(control_root / "two").exists()
    # The record that stayed is the retained generation's, and it is doing the
    # job it was written for: that generation can no longer be withdrawn.
    assert {path.stem for path in admission_markers(control_root / "one")} == {
        attest(one)
    }
    withdrawal = graph_migration.rollback_migration(destination=control_root / "one")
    assert withdrawal["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in withdrawal["findings"]
    assert one.is_dir()
    resolved = resolve_generation_runtime(pointer=pointer)
    assert resolved.usable is True and resolved.config.root == one


def test_a_restore_refuses_when_the_generation_it_leaves_cannot_be_admitted(
    source, control_root, pointer, monkeypatch
):
    """A restore publishes a pointer too, so its retained half is the same rule.

    The generation being left is retained by the pointer the restore writes, so
    a withdrawal of it would make the restore irreversible. The failure is
    injected only in that destination; the restore target is untouched.
    """
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    activate_generation(pointer=pointer, generation=one, apply=True)
    activate_generation(pointer=pointer, generation=two, apply=True)
    rejected = plant_unreadable_ledger(control_root / "two")
    pointer_before = pointer.read_bytes()
    one_before = tree_state(one)
    writes: list[Path] = []
    monkeypatch.setattr(
        runtime, "_write_pointer", lambda path, payload: writes.append(path)
    )

    report = restore_generation(pointer=pointer, apply=True)

    assert writes == []
    assert rejected in retained_refusal(report)
    assert pointer.read_bytes() == pointer_before
    assert tree_state(one) == one_before
    resolved = resolve_generation_runtime(pointer=pointer)
    assert resolved.usable is True and resolved.config.root == two


def test_an_unchanged_activation_does_not_bypass_an_unprotected_retained_half(
    source, control_root, pointer
):
    """Being already correct about the served generation is not enough.

    The pointer here already names ``two`` and resolves, so this is the
    ``unchanged`` path — the one that reports success without a publication
    proof. It still may not answer "unchanged" while the generation the same
    pointer retains is provably unprotected.
    """
    one = publish_into(source, control_root, "one")
    two = publish_into(source, control_root, "two")
    activate_generation(pointer=pointer, generation=one, apply=True)
    activate_generation(pointer=pointer, generation=two, apply=True)
    rejected = plant_unreadable_ledger(control_root / "one")
    pointer_before = pointer.read_bytes()

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert rejected in retained_refusal(report)
    assert report["status"] != STATUS_CUTOVER_UNCHANGED
    assert pointer.read_bytes() == pointer_before
    assert resolve_generation_runtime(pointer=pointer).config.root == two


def test_a_cutover_with_nothing_retained_applies_and_admits_one_generation(
    source, control_root, pointer
):
    """No retained generation is the ordinary first cutover, not a refusal."""
    two = publish_into(source, control_root, "two")
    assert not pointer.exists()

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert report["status"] == STATUS_CUTOVER_APPLIED
    assert report["previous"]["generation"] == ""
    assert {path.stem for path in admission_markers(control_root / "two")} == {
        attest(two)
    }
    assert resolve_generation_runtime(pointer=pointer).config.root == two


def test_a_retained_generation_outside_any_destination_needs_no_record(
    source, control_root, pointer
):
    """Protection is needed where a withdrawal can reach, and nowhere else.

    A pointer retaining a directory a migration destination does not own is not
    a generation ``graph_migration rollback`` can move, so there is nothing to
    record and nothing to refuse — the refusals above are about retained
    generations that *are* reachable, not about every unusual pointer.
    """
    two = publish_into(source, control_root, "two")
    standalone = control_root / "standalone"
    standalone.mkdir()
    write_pointer(
        pointer,
        generation="standalone",
        manifest_sha256="b" * 64,
        previous_generation="",
        previous_manifest_sha256="",
    )

    report = activate_generation(pointer=pointer, generation=two, apply=True)

    assert report["status"] == STATUS_CUTOVER_APPLIED
    assert report["previous"]["generation"] == "standalone"
    assert generation_module.owning_destination(standalone) is None
    assert {path.stem for path in admission_markers(control_root / "two")} == {
        attest(two)
    }


# ---------------------------------------------------------------------------
# The whole round trip, with a real framework write in the middle
#
# The requirement these pin: an activated generation is *mutable*, so none of
# the steady-state answers may depend on the publication artifact hashes. Each
# test writes through the resolved config with the framework's own committing
# entrance and then checks the result with independent raw readers.
# ---------------------------------------------------------------------------

def test_activate_then_write_then_close_then_reopen_by_pointer(
    published, control_root, pointer
):
    nodes_before, events_before = probe_counts(published)
    assert activate_generation(
        pointer=pointer, generation=published, apply=True
    )["status"] == STATUS_CUTOVER_APPLIED

    write_into(published, pointer)
    reopened = resolve_generation_runtime(pointer=pointer)

    # Reopened purely from the pointer, with no hash from publication time.
    assert reopened.usable is True
    assert reopened.report.state == STATE_ACTIVE
    assert reopened.report.lifecycle == LIFECYCLE_ACTIVATED
    assert reopened.report.integrity == INTEGRITY_ATTESTED
    assert reopened.report.attested is True
    assert reopened.report.read_only_health is True
    assert reopened.report.projection_consistent is True
    # And it does not claim the proof that the write legitimately invalidated.
    assert reopened.report.generation_intact is False
    assert resolve_generation_runtime(generation=published).usable is False

    # The reverse probes: raw read-only SQLite and plain file reads.
    nodes_after, events_after = probe_counts(published)
    assert nodes_after == nodes_before + 1
    assert events_after > events_before
    assert reopened.report.node_count == nodes_after
    assert reopened.report.event_count == events_after
    surface = probe_text(published / "knowledge" / f"{RUNTIME_DOMAIN}.md")
    assert RUNTIME_SECTION in surface and RUNTIME_BODY in surface
    assert RUNTIME_DOMAIN in probe_text(published / "L0.md")
    assert RUNTIME_ENTRY_ID in probe_vector_ids(published)
    # Nothing leaked outside the generation: the control root still holds one
    # pointer and the destination, and no stray temporary file.
    assert temp_files(control_root) == []


def test_the_written_generation_is_still_consistent_through_the_framework(
    published, pointer
):
    """The adapter's health claim and the framework's own view are one answer."""
    activate_generation(pointer=pointer, generation=published, apply=True)
    write_into(published, pointer)

    config = resolve_generation_runtime(pointer=pointer).config
    status = graph_application.runtime_status(config)

    assert status["journal"]["status"] == JOURNAL_OPENED
    assert status["graph"]["status"] == GRAPH_STORE_OPENED
    assert status["graph"]["healthy"] is True
    assert status["projection_drift"]["drifted"] is False
    assert status["journal"]["pending_projections"] == []


def test_a_second_write_after_a_reopen_lands_in_the_same_generation(
    published, pointer
):
    """Read/write consistency across a close: the reopened config is the same
    authority, not a parallel one."""
    activate_generation(pointer=pointer, generation=published, apply=True)
    write_into(published, pointer)
    write_into(
        published,
        pointer,
        node_id="claim_second_rehearsal",
        section="Rehearsal log",
        body="Rehearsals are logged on the standby replica.",
    )

    nodes, _events = probe_counts(published)
    resolved = resolve_generation_runtime(pointer=pointer)

    assert resolved.usable is True
    assert resolved.report.node_count == nodes
    surface = probe_text(published / "knowledge" / f"{RUNTIME_DOMAIN}.md")
    assert "logged on the standby replica" in surface


def test_a_repeated_activation_after_a_real_write_is_still_a_zero_write_no_op(
    published, control_root, pointer
):
    """The idempotence that the publication proof would otherwise have broken.

    Reactivating the generation already being served is recognised *before* that
    proof is attempted, precisely because a served generation has legitimately
    moved on from its publication hashes.
    """
    activate_generation(pointer=pointer, generation=published, apply=True)
    write_into(published, pointer)
    before = tree_state(control_root)
    pointer_before = pointer.read_bytes()

    report = activate_generation(pointer=pointer, generation=published, apply=True)

    assert report["status"] == STATUS_CUTOVER_UNCHANGED
    assert report["exit_code"] == runtime.EXIT_OK
    assert report["resolution"]["lifecycle"] == LIFECYCLE_ACTIVATED
    assert report["resolution"]["integrity"] == INTEGRITY_ATTESTED
    # Including the generation that has just been written into: a no-op after a
    # real write touches neither the data nor the admission ledger — nor any
    # directory's own modification time.
    assert tree_state(control_root) == before
    assert pointer.read_bytes() == pointer_before
    assert not runtime.pointer_lock_path(pointer).exists()


def test_restoring_a_generation_that_has_been_written_into_works(
    source, destination, control_root, published, pointer
):
    """The requirement in full: a *previously activated, writable* generation is
    restorable without its original publication hashes.

    Both generations below are activated and then written into by the framework,
    so neither can pass ``verify_generation`` any more. The restore has to stand
    on the retained attestation, and the restored generation has to come back up
    carrying the write it received.
    """
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    write_into(withdrawn, pointer, node_id="claim_rollback_rehearsal")
    activate_generation(pointer=pointer, generation=current, apply=True)
    write_into(current, pointer, node_id="claim_current_rehearsal")
    assert resolve_generation_runtime(generation=withdrawn).usable is False

    report = restore_generation(pointer=pointer, apply=True)

    assert report["status"] == STATUS_RESTORE_APPLIED
    assert report["resolution"]["integrity"] == INTEGRITY_ATTESTED
    reopened = resolve_generation_runtime(pointer=pointer)
    assert reopened.usable is True
    assert reopened.report.state == STATE_ROLLBACK
    assert reopened.config.root == withdrawn
    # The restored generation is the one that received the rollback write, and
    # the generation we left still has its own — nothing was deleted or merged.
    uri_nodes, _events = probe_counts(withdrawn)
    assert uri_nodes == reopened.report.node_count
    assert "rehearsal" in probe_text(withdrawn / "knowledge" / f"{RUNTIME_DOMAIN}.md")
    assert "rehearsal" in probe_text(current / "knowledge" / f"{RUNTIME_DOMAIN}.md")
    assert withdrawn.is_dir() and current.is_dir()
    # And the restore is itself reversible, back onto a written generation.
    assert restore_generation(pointer=pointer, apply=True)["status"] == (
        STATUS_RESTORE_APPLIED
    )
    assert resolve_generation_runtime(pointer=pointer).config.root == current


def test_a_write_into_an_activated_generation_reaches_nothing_outside_it(
    published, pointer, tmp_path, source
):
    """Every path a write can land in is derived from the generation root."""
    before = tree_state(source)
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    (decoy / "knowledge").mkdir()
    activate_generation(pointer=pointer, generation=published, apply=True)

    write_into(published, pointer)

    assert tree_state(source) == before
    assert list(decoy.rglob("*.md")) == []
    assert list(decoy.rglob("*.db")) == []


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_status_reports_an_unresolved_pointer_as_a_refusal(pointer):
    report = status_generation(pointer=pointer)

    assert report["command"] == "status"
    assert report["dry_run"] is True
    assert report["status"] == STATUS_RUNTIME_ABSENT
    assert report["exit_code"] == runtime.EXIT_BLOCKED


def test_reports_carry_no_memory_bodies(published, pointer):
    serialized = json.dumps(
        [
            status_generation(generation=published),
            activate_generation(pointer=pointer, generation=published, apply=True),
            restore_generation(pointer=pointer),
        ]
    )

    assert "rebuilt nightly" not in serialized
    assert "batch loader" not in serialized
    assert "Index maintenance" not in serialized


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def cli(capsys, *args: str) -> tuple[int, dict]:
    code = runtime.main(list(args))
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def test_the_cli_default_verb_is_a_read_only_status(published, capsys):
    code, report = cli(capsys, "--generation", str(published))

    assert code == runtime.EXIT_OK
    assert report["command"] == "status"
    assert report["dry_run"] is True
    assert report["status"] == STATUS_RUNTIME_ACTIVE


def test_the_cli_activate_needs_apply_to_write(published, control_root, pointer, capsys):
    code, planned = cli(
        capsys, "activate", "--pointer", str(pointer), "--generation", str(published)
    )
    assert code == runtime.EXIT_OK
    assert planned["status"] == STATUS_CUTOVER_PLANNED
    assert not pointer.exists()

    code, applied = cli(
        capsys,
        "activate",
        "--pointer",
        str(pointer),
        "--generation",
        str(published),
        "--apply",
    )
    assert code == runtime.EXIT_OK
    assert applied["status"] == STATUS_CUTOVER_APPLIED
    assert pointer.is_file()


def test_the_cli_restore_verb_is_wired(
    source, destination, published, pointer, capsys
):
    withdrawn, current = withdraw_and_republish(source, destination)
    activate_generation(pointer=pointer, generation=withdrawn, apply=True)
    activate_generation(pointer=pointer, generation=current, apply=True)

    code, report = cli(capsys, "restore", "--pointer", str(pointer), "--apply")

    assert code == runtime.EXIT_OK
    assert report["status"] == STATUS_RESTORE_APPLIED


def test_the_cli_refuses_two_inputs_at_once(published, pointer):
    with pytest.raises(SystemExit) as raised:
        runtime.main(
            [
                "status",
                "--generation",
                str(published),
                "--pointer",
                str(pointer),
            ]
        )

    assert raised.value.code == runtime.EXIT_USAGE


def test_a_refusal_exits_nonzero_with_a_json_report(tmp_path, capsys):
    code, report = cli(capsys, "status", "--generation", str(tmp_path / "nowhere"))

    assert code == runtime.EXIT_BLOCKED
    assert report["status"] == STATUS_RUNTIME_INVALID


# ---------------------------------------------------------------------------
# The migration operator keeps its four verbs, and gains these three
# ---------------------------------------------------------------------------

def test_the_migration_cli_lists_both_verb_families():
    assert set(graph_migration.COMMANDS) == {
        "plan",
        "apply",
        "verify",
        "rollback",
        *CUTOVER_COMMANDS,
    }
    assert graph_migration.COMMANDS[0] == "plan"


def test_the_migration_cli_delegates_the_cutover_verbs(
    published, pointer, capsys
):
    code = graph_migration.main(
        ["activate", "--pointer", str(pointer), "--generation", str(published)]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == graph_migration.EXIT_OK
    assert report["command"] == "activate"
    assert report["status"] == STATUS_CUTOVER_PLANNED
    assert not pointer.exists()


def test_the_migration_verbs_still_plan_apply_verify_and_rollback(
    source, destination, capsys
):
    assert graph_migration.main(
        ["--source", str(source), "--destination", str(destination)]
    ) == graph_migration.EXIT_OK
    assert json.loads(capsys.readouterr().out)["command"] == "plan"
    assert not destination.exists()

    assert graph_migration.main(
        ["apply", "--source", str(source), "--destination", str(destination)]
    ) == graph_migration.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == (
        graph_migration.STATUS_APPLIED
    )

    assert graph_migration.main(
        ["verify", "--destination", str(destination)]
    ) == graph_migration.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == (
        graph_migration.STATUS_VERIFIED
    )

    assert graph_migration.main(
        ["rollback", "--destination", str(destination)]
    ) == graph_migration.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == (
        graph_migration.STATUS_ROLLED_BACK
    )


# ---------------------------------------------------------------------------
# Backward compatibility: the legacy default is untouched
# ---------------------------------------------------------------------------

def test_the_legacy_memory_config_default_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_DB_PATH", raising=False)
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_JOURNAL_PATH", raising=False)
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_MODE", raising=False)
    home = tmp_path / "legacy-home"
    home.mkdir()

    config = MemoryConfig(home=str(home))

    assert config.graph_mode == "legacy"
    assert config.graph_enabled is False
    assert config.graph_write_enabled is False
    assert resolve_graph_database_path(config) is None
    assert resolve_journal_database_path(config) is None


@pytest.mark.parametrize("mode", GRAPH_MODES)
def test_runtime_config_mode_predicates_agree_with_memory_config(
    mode, published, tmp_path, monkeypatch
):
    """One mode vocabulary. A predicate that drifted would be a silent cutover."""
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_MODE", raising=False)
    home = tmp_path / f"home-{mode}"
    home.mkdir()
    reference = MemoryConfig(home=str(home), graph_mode=mode)

    config = runtime.GenerationRuntimeConfig(root=published, graph_mode=mode)

    assert config.graph_mode == reference.graph_mode
    assert config.graph_enabled == reference.graph_enabled
    assert config.graph_recall_enabled == reference.graph_recall_enabled
    assert config.graph_write_enabled == reference.graph_write_enabled
    assert config.graph_shadow_enabled == reference.graph_shadow_enabled


def test_an_unknown_runtime_mode_is_refused(published):
    with pytest.raises(ValueError):
        runtime.GenerationRuntimeConfig(root=published, graph_mode="turbo")


def test_the_adapter_is_not_wired_into_any_startup_path(
    published, pointer, control_root, tmp_path, monkeypatch
):
    """Startup may resolve an explicit pointer; cutover stays an operator act."""
    operator_writes = (
        "activate_generation",
        "restore_generation",
        "register_admission",
        "_admit_generation",
        "_write_and_confirm",
        "_write_pointer",
        "cutover_command",
    )
    for module in ("server", "injector", "recall", "config"):
        text = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "layered_memory_mcp"
            / f"{module}.py"
        ).read_text(encoding="utf-8")
        if module != "config":
            assert "graph_generation_runtime" not in text
        for operation in operator_writes:
            assert operation not in text, (module, operation)

    report = activate_generation(pointer=pointer, generation=published, apply=True)
    assert report["status"] == STATUS_CUTOVER_APPLIED, report
    before = tree_state(control_root)

    def forbid_operator_write(*args, **kwargs):
        pytest.fail("Startup must not invoke generation operator writes")

    for operation in operator_writes:
        monkeypatch.setattr(runtime, operation, forbid_operator_write)
    monkeypatch.setattr(generation_module, "register_admission", forbid_operator_write)

    resolutions = []

    def readonly_resolve(**kwargs):
        resolutions.append(kwargs)
        return resolve_generation_runtime(**kwargs)

    monkeypatch.setattr(runtime, "resolve_generation_runtime", readonly_resolve)
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", raising=False)

    for mode in GRAPH_MODES:
        config = MemoryConfig(
            home=str(tmp_path / f"unbound-{mode}"),
            agent_memory={"adapter": "generic"},
            graph_mode=mode,
        )
        assert config.graph_active_pointer is None
        assert resolutions == []
        assert tree_state(control_root) == before

    config = MemoryConfig(
        home=str(tmp_path / "bound"),
        agent_memory={"adapter": "generic"},
        graph_mode="graph",
        graph_active_pointer=pointer,
    )
    assert config.graph_active_pointer == pointer
    assert config.graph_db_path == published / "graph.db"
    assert config.graph_journal_path == published / "journal.db"
    assert resolutions == [{"pointer": pointer, "graph_mode": "graph"}]
    assert tree_state(control_root) == before
    assert os.environ.get("LAYERED_MEMORY_ACTIVE_GENERATION") is None

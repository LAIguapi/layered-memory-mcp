"""The staged migration generation: build it privately, verify it, publish once.

WHAT THIS MODULE IS
-------------------
Everything about the *destination* side of
:mod:`~layered_memory_mcp.graph_migration`: the on-disk shape of one migration
generation, the manifest that describes it, the read-only verification that
proves it, the single atomic publication boundary, and the withdrawal that
undoes it without destroying anything.

THE GENERATION LAYOUT
---------------------
::

    <destination>/
        current/                       the published generation
            journal.db                 the append-only bootstrap authority
            graph.db                   the graph, rebuilt from that journal
            knowledge/<domain>.md      the readable L1 surface (source bytes)
            data/vectors.db            the vector rows (metadata-only change)
            data/memory_changelog.jsonl  the historical evidence, preserved
            L0.md                      the index, regenerated from L1
            backup/knowledge/…         the source bytes, frozen
            backup/data/…              the source databases, frozen
            manifest.json              what this generation is
            manifest.sha256            the manifest's own digest
        .staging-<token>/              a private generation under construction
        rollback/withdrawn-<stamp>/    a withdrawn generation, retained
        debris/staged-<stamp>/         a staged generation that never published

Only those four top-level names are ever created, and any *other* top-level
entry in a destination is unknown content this module refuses to work beside.

WHY A DIRECTORY RENAME IS THE WHOLE TRANSACTION
-----------------------------------------------
Two SQLite databases, a vector database and a tree of Markdown files do not
share a transaction, and pretending otherwise is the failure mode
ARCHITECTURE_v2.md names. So nothing is published incrementally: the entire
generation is assembled under a private ``.staging-<token>`` directory, verified
there, and then moved into place with one :func:`os.rename` — atomic within a
filesystem, and the reason the staging directory is a *sibling* of the
publication target rather than a temporary directory somewhere else. Before the
rename there is no visible generation; after it there is a complete one. A
failure leaves the staged tree as explicitly accounted-for debris under
``debris/`` instead of a half-published destination.

WHAT IT IS NOT
--------------
- NOT a discoverer. Both roots are arguments. The
  :class:`GenerationConfig` handed to the framework's own projection routines is
  an explicit object naming paths *inside the generation*; no
  :class:`~layered_memory_mcp.config.MemoryConfig` is constructed, so no
  ``config.yaml``, environment variable or home directory can redirect a write.
- NOT a re-embedder. The vector database is copied byte-for-byte and then
  changed only through
  :meth:`~layered_memory_mcp.storage.vector_store.VectorStore.patch_metadata`,
  the store's own atomic metadata-only boundary. ``id``, ``domain``, ``text``
  and ``vector`` are never written, and :func:`verify_generation` proves it by
  comparing the published rows against the frozen backup.
- NOT a deleter. Withdrawal *renames* a generation into ``rollback/`` and keeps
  it, including its backups and its manifest. Nothing here removes a tree, and
  the source is never written, renamed or truncated by any function in this
  module.
- NOT a trusting reader. :func:`verify_generation` recomputes membership and
  every artifact hash, replays the journal and compares the result against the
  stored graph, re-reads the vector rows and the readable sections, and only
  *then* compares the counts the manifest claims. A manifest is evidence to be
  checked, not an answer.

PRIVACY (§15)
-------------
:class:`VerificationReport` carries counts, generation-relative paths, row and
node identities, and bounded refusal tokens. The operator-supplied source and
destination paths are echoed because an operator tool must say what it operated
on; a memory body, a heading, a vector value and a backend message are not in
any report this module produces.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from pydantic import Field

from .graph_backfill import (
    BACKFILL_NOTHING_TO_DO,
    BackfillPlan,
    MetadataUpdate,
    apply_graph_seed_backfill,
    plan_graph_seed_backfill,
)
from .graph_fixture_store import InMemoryGraphStore
from .graph_journal import (
    PROJECTION_GRAPH,
    PROJECTION_L0,
    PROJECTION_L1,
    PROJECTION_NAMES,
    PROJECTION_VECTOR,
    REQUEST_KIND_BOOTSTRAP,
    MemoryEventJournal,
    SurfaceRecord,
    body_sha256,
    canonical_request_hash,
)
from .graph_models import GraphModel, GraphNode
from .graph_migration_bootstrap import BootstrapPlan
from .graph_migration_inventory import (
    SOURCE_CHANGELOG,
    SOURCE_KNOWLEDGE_DIR,
    SOURCE_VECTOR_DB,
    SourceSnapshot,
    file_sha256,
    read_vector_rows,
)
from .graph_projection import (
    ProjectionError,
    publish_graph,
    publish_l0,
    read_section_body,
)
from .graph_runtime import GRAPH_SEED_ID_KEY

__all__ = [
    "BACKUP_DIR",
    "DEBRIS_DIR",
    "DESTINATION_ENTRIES",
    "GENERATION_DIR",
    "MANIFEST_DIGEST_NAME",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "ROLLBACK_DIR",
    "STAGING_PREFIX",
    "FINDING_SOURCE_DRIFT",
    "GenerationBuild",
    "GenerationConfig",
    "VerificationReport",
    "build_generation",
    "destination_findings",
    "destination_prestate",
    "generation_inventory",
    "manifest_digest",
    "new_staging_path",
    "publish_generation",
    "quarantine_staging",
    "read_manifest",
    "verify_generation",
    "withdraw_generation",
    "write_manifest",
]

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

GENERATION_DIR = "current"
STAGING_PREFIX = ".staging-"
ROLLBACK_DIR = "rollback"
DEBRIS_DIR = "debris"
BACKUP_DIR = "backup"
MANIFEST_NAME = "manifest.json"
MANIFEST_DIGEST_NAME = "manifest.sha256"
JOURNAL_NAME = "journal.db"
GRAPH_NAME = "graph.db"
L0_NAME = "L0.md"
DATA_DIR = "data"
VECTOR_RELATIVE = "data/vectors.db"
CHANGELOG_RELATIVE = "data/memory_changelog.jsonl"

#: The only top-level names this tool ever creates in a destination. Anything
#: else there belongs to somebody else, and a migration that wrote beside it
#: would be adopting a directory it was merely pointed at.
DESTINATION_ENTRIES = frozenset({GENERATION_DIR, ROLLBACK_DIR, DEBRIS_DIR})

#: The manifest layout version. Recorded and checked; never migrated.
MANIFEST_VERSION = 1

#: Files the manifest never describes, because they describe the manifest.
_MANIFEST_FILES = frozenset({MANIFEST_NAME, MANIFEST_DIGEST_NAME})

#: SQLite companion files. Their presence in a finished generation means a
#: handle was still open when it was hashed, so they are a refusal rather than
#: something to hash along with everything else.
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

FINDING_GENERATION_ABSENT = "generation_absent"
FINDING_GENERATION_NOT_A_DIRECTORY = "generation_not_a_directory"
FINDING_GENERATION_SYMLINK = "generation_symlink"
FINDING_GENERATION_SPECIAL_FILE = "generation_special_file"
FINDING_GENERATION_SIDECAR = "generation_sqlite_sidecar"
FINDING_MANIFEST_ABSENT = "manifest_absent"
FINDING_MANIFEST_UNREADABLE = "manifest_unreadable"
FINDING_MANIFEST_DIGEST_ABSENT = "manifest_digest_absent"
FINDING_MANIFEST_DIGEST_MISMATCH = "manifest_digest_mismatch"
FINDING_MANIFEST_VERSION = "manifest_version_unsupported"
FINDING_MANIFEST_PATH_REJECTED = "manifest_path_rejected"
FINDING_ARTIFACT_MISSING = "artifact_missing"
FINDING_ARTIFACT_UNEXPECTED = "artifact_unexpected"
FINDING_ARTIFACT_HASH = "artifact_hash_mismatch"
FINDING_ARTIFACT_SIZE = "artifact_size_mismatch"
FINDING_SOURCE_FILES_UNREADABLE = "source_files_unreadable"
FINDING_SOURCE_FILE_PATH = "source_file_path_rejected"
FINDING_SOURCE_FILE_MISSING = "source_file_missing"
FINDING_SOURCE_FILE_UNEXPECTED = "source_file_unexpected"
FINDING_SOURCE_FILE_HASH = "source_file_hash_mismatch"
FINDING_SOURCE_FILE_SIZE = "source_file_size_mismatch"
FINDING_JOURNAL_UNREADABLE = "journal_unreadable"
FINDING_JOURNAL_REQUEST = "journal_request_mismatch"
FINDING_JOURNAL_COUNTS = "journal_count_mismatch"
FINDING_JOURNAL_CHECKPOINT = "journal_checkpoint_behind"
FINDING_GRAPH_UNREADABLE = "graph_unreadable"
FINDING_GRAPH_UNHEALTHY = "graph_unhealthy"
FINDING_GRAPH_REPLAY = "graph_replay_mismatch"
FINDING_SURFACE_REGISTRY = "surface_registry_missing"
FINDING_SURFACE_ABSENT = "surface_absent"
FINDING_SURFACE_BODY = "surface_body_mismatch"
FINDING_VECTOR_UNREADABLE = "vector_db_unreadable"
FINDING_VECTOR_ROW_MISSING = "vector_row_missing"
FINDING_VECTOR_ROW_UNEXPECTED = "vector_row_unexpected"
FINDING_VECTOR_SEED = "vector_seed_mismatch"
FINDING_VECTOR_EMBEDDING = "vector_embedding_changed"
FINDING_VECTOR_SEED_UNKNOWN_NODE = "vector_seed_node_unknown"
FINDING_COUNTS = "counts_mismatch"
FINDING_SOURCE_DRIFT = "source_drift"
FINDING_DESTINATION_NOT_A_DIRECTORY = "destination_not_a_directory"
FINDING_DESTINATION_SYMLINK = "destination_symlink"
FINDING_DESTINATION_UNKNOWN = "destination_unknown_contents"
FINDING_GENERATION_EXISTS = "generation_already_published"
FINDING_BUILD_FAILED = "generation_build_failed"
FINDING_SURFACE_BODY_UNALIGNED = "surface_body_unaligned"


# ---------------------------------------------------------------------------
# The explicit configuration handed to the framework's projection routines
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GenerationConfig:
    """An explicit, resolution-free configuration scoped to one generation.

    The framework's projection routines are duck-typed on a handful of
    attributes, which is what lets a migration drive them without constructing
    a :class:`~layered_memory_mcp.config.MemoryConfig`. That matters here: a
    real config reads ``<home>/config.yaml``, several environment variables and
    (for the agent memory path) absolute user-level locations. A migration must
    be incapable of being redirected by any of them, so every path below is
    derived from the generation root and nothing else.

    ``memory_changelog_enabled`` is ``False`` on purpose: the source changelog
    is preserved as *evidence*, and a migration that appended its own L0 resync
    events to it would be writing history it did not witness.
    """

    root: Path
    graph_mode: str = "graph"
    l0_format: str = "hermes"
    l0_tag: str = "[L0]"
    memory_changelog_enabled: bool = False
    auto_maintain: bool = False
    auto_sync_l0: bool = False

    @property
    def home(self) -> Path:
        return self.root

    @property
    def knowledge_dir(self) -> Path:
        return self.root / SOURCE_KNOWLEDGE_DIR

    @property
    def knowledge_dirs(self) -> list[Path]:
        return [self.knowledge_dir]

    @property
    def l0_index_file(self) -> Path:
        return self.root / L0_NAME

    @property
    def graph_db_path(self) -> Path:
        return self.root / GRAPH_NAME

    @property
    def graph_journal_path(self) -> Path:
        return self.root / JOURNAL_NAME


# ---------------------------------------------------------------------------
# Destination shape
# ---------------------------------------------------------------------------

def destination_prestate(destination: Path) -> str:
    """``absent`` / ``empty`` / ``populated`` — what a rollback restores to.

    Recorded in the manifest before anything is created, because "put it back
    the way it was" needs to know what that was: a destination this tool
    created from nothing is a different starting point from one the operator
    had already prepared.
    """
    if not destination.exists():
        return "absent"
    try:
        return "empty" if not any(destination.iterdir()) else "populated"
    except OSError:  # pragma: no cover - reported by destination_findings
        return "populated"


def destination_findings(destination: Path) -> tuple[list[str], list[str]]:
    """``(findings, warnings)`` for one destination's top-level shape.

    Only the top-level names this tool owns are tolerated. An unknown entry is
    refused rather than worked around: a destination with somebody else's data
    in it was not prepared for a migration, and finding out afterwards is too
    late. A leftover ``.staging-…`` directory is this tool's own debris from an
    interrupted run — visible as a warning, never reused, and not a reason to
    refuse the destination.
    """
    findings: list[str] = []
    warnings: list[str] = []
    if not destination.exists():
        return findings, warnings
    if destination.is_symlink():
        return [FINDING_DESTINATION_SYMLINK], warnings
    if not destination.is_dir():
        return [FINDING_DESTINATION_NOT_A_DIRECTORY], warnings
    for child in sorted(destination.iterdir(), key=lambda path: path.name):
        name = child.name
        if name.startswith(STAGING_PREFIX):
            warnings.append(f"stale_staging_directory:{name}")
            continue
        if name not in DESTINATION_ENTRIES or child.is_symlink() or not child.is_dir():
            findings.append(f"{FINDING_DESTINATION_UNKNOWN}:{name}")
    return findings, warnings


def new_staging_path(destination: Path) -> Path:
    """A fresh private staging path, a sibling of the publication target.

    A sibling because the publication is a :func:`os.rename`, and a rename is
    only atomic within one filesystem. A fresh token because staged debris is
    *retained* for inspection and must never be reused: resuming somebody
    else's half-built generation would publish material nothing verified.
    """
    return destination / f"{STAGING_PREFIX}{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def manifest_digest(body: Mapping[str, Any]) -> str:
    """The digest of one manifest body, over its canonical compact JSON.

    Tamper *detection*, not authentication: an accidental edit, a partial write
    or a hand-tweaked count changes the digest, while anyone able to rewrite
    both files can obviously rewrite both. The real integrity guarantee is that
    :func:`verify_generation` recomputes every artifact hash and replays the
    journal rather than believing any number in here.
    """
    canonical = json.dumps(dict(body), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_manifest(generation: Path, body: Mapping[str, Any]) -> None:
    """Write ``manifest.json`` and its digest sidecar."""
    payload = dict(body)
    (generation / MANIFEST_NAME).write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (generation / MANIFEST_DIGEST_NAME).write_text(
        manifest_digest(payload) + "\n", encoding="utf-8"
    )


def read_manifest(generation: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """``(body, findings)`` for one generation's manifest. Reads only."""
    findings: list[str] = []
    path = generation / MANIFEST_NAME
    digest_path = generation / MANIFEST_DIGEST_NAME
    if not path.is_file():
        return None, [FINDING_MANIFEST_ABSENT]
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"{FINDING_MANIFEST_UNREADABLE}:{type(exc).__name__}"]
    if not isinstance(body, dict):
        return None, [f"{FINDING_MANIFEST_UNREADABLE}:not_a_mapping"]
    if body.get("manifest_version") != MANIFEST_VERSION:
        findings.append(f"{FINDING_MANIFEST_VERSION}:{body.get('manifest_version')}")
    if not digest_path.is_file():
        findings.append(FINDING_MANIFEST_DIGEST_ABSENT)
    else:
        try:
            recorded = digest_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            findings.append(f"{FINDING_MANIFEST_UNREADABLE}:{type(exc).__name__}")
            recorded = ""
        if recorded != manifest_digest(body):
            findings.append(FINDING_MANIFEST_DIGEST_MISMATCH)
    return body, findings


def _safe_member(root: Path, relative: Any) -> Path | None:
    """Resolve one manifest-declared relative path, or refuse it.

    A manifest is untrusted input: it names the files a verification will stat,
    hash and read. An absolute path, a drive-relative one, a ``..`` component, a
    path that leaves the generation or one that traverses a symlink is refused
    by name, so a forged manifest cannot point this module at anything outside
    the tree it is verifying.
    """
    if not isinstance(relative, str) or not relative or "\\" in relative:
        return None
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in ("..", ".") for part in pure.parts):
        return None
    candidate = root.joinpath(*pure.parts)
    root_resolved = root.resolve()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return None
    # No component may be a symlink: a symlinked directory inside the
    # generation would make "inside the tree" a statement about the link
    # rather than about the data.
    walked = root
    for part in pure.parts:
        walked = walked / part
        if walked.is_symlink():
            return None
    return candidate


# ---------------------------------------------------------------------------
# Generation inventory
# ---------------------------------------------------------------------------

def _walk_generation(root: Path) -> Iterator[tuple[str, str, Path]]:
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        current, prefix = stack.pop()
        with os.scandir(current) as scan:
            children = sorted(scan, key=lambda entry: entry.name)
        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            path = Path(child.path)
            if child.is_symlink():
                yield "symlink", relative, path
            elif child.is_dir(follow_symlinks=False):
                stack.append((path, relative))
            elif child.is_file(follow_symlinks=False):
                yield "file", relative, path
            else:
                yield "special", relative, path


def generation_inventory(
    root: Path, *, allow_sqlite_sidecars: bool = False
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """``({relative path: {size, sha256}}, findings)`` for a generation tree.

    The manifest's own two files are excluded, because they are what this
    inventory is compared *against*. A symlink, a special file or a leftover
    SQLite companion file is a finding: all three mean the tree is not the
    closed, fully-hashed set of bytes a manifest claims. Source backups may
    opt into sidecars because an empty sidecar can be a legitimate member of
    the inventoried legacy source; published generation artifacts may not.
    """
    artifacts: dict[str, dict[str, Any]] = {}
    findings: list[str] = []
    for kind, relative, path in _walk_generation(root):
        if kind == "symlink":
            findings.append(f"{FINDING_GENERATION_SYMLINK}:{relative}")
            continue
        if kind == "special":
            findings.append(f"{FINDING_GENERATION_SPECIAL_FILE}:{relative}")
            continue
        if relative in _MANIFEST_FILES:
            continue
        if relative.endswith(_SQLITE_SIDECAR_SUFFIXES) and not (
            relative.startswith(f"{BACKUP_DIR}/") or allow_sqlite_sidecars
        ):
            findings.append(f"{FINDING_GENERATION_SIDECAR}:{relative}")
            continue
        artifacts[relative] = {
            "size": path.stat().st_size,
            "sha256": file_sha256(path),
        }
    return artifacts, findings


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

@dataclass
class GenerationBuild:
    """The outcome of assembling one staged generation."""

    staging: Path
    manifest: dict[str, Any]
    findings: list[str]

    @property
    def ok(self) -> bool:
        return not self.findings


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _vector_updates(
    snapshot: SourceSnapshot, plan: BackfillPlan
) -> list[MetadataUpdate]:
    """The complete set of metadata updates this migration publishes.

    Two sources, both explicit: the planner's own updates for rows whose *own*
    identity resolves to one L1 entry, and the operator's validated identity
    mapping for rows whose identity does not. A row ID is never rewritten by
    either — only the ``metadata`` column moves — so an old identity keeps its
    row and a new projection remains a separately coordinated operation.
    """
    updates = list(plan.updates)
    for row_id, (node_id, metadata) in sorted(snapshot.identity_map_updates.items()):
        updates.append(
            MetadataUpdate(row_id=row_id, graph_node_id=node_id, metadata=metadata)
        )
    return sorted(updates, key=lambda update: update.row_id)


def build_generation(
    snapshot: SourceSnapshot,
    bootstrap: BootstrapPlan,
    staging: Path,
    *,
    destination: Path,
    prestate: str,
) -> GenerationBuild:
    """Assemble one complete generation under ``staging``. Writes only there.

    The order is the framework's own dependency order, and every checkpoint is
    advanced only after the projection it describes actually succeeded:

    1. the source bytes are copied — twice: once as the generation's live
       readable/retrieval surface, once frozen under ``backup/``;
    2. the bootstrap events are appended to a fresh journal under
       :data:`~layered_memory_mcp.graph_journal.REQUEST_KIND_BOOTSTRAP`, which
       is the authority everything below is derived from;
    3. the graph is rebuilt **from the persisted journal** — never from the
       planning store, whose per-record writes are not the authority;
    4. each bound readable surface is registered with the body the copied
       document actually contains, after checking that body matches the claim
       the graph holds. This is the one adoption step, and it adopts only the
       documents the operator explicitly supplied: it reads them and records
       what they already say, and rewrites nothing;
    5. the vector rows gain their ``graph_node_id`` through the store's atomic
       metadata-only boundary;
    6. L0 is regenerated from the published L1 files.

    Returns:
        A :class:`GenerationBuild`. Any failure is a bounded finding; the caller
        quarantines the staged tree rather than publishing it.
    """
    from .recall import invalidate_scan_cache
    from .storage.vector_store import VectorStore

    findings: list[str] = []
    staging.mkdir(parents=True, exist_ok=False)
    config = GenerationConfig(root=staging)

    # 1. Source bytes: the live surface and the frozen backup.
    for domain in sorted(snapshot.documents):
        origin = snapshot.source / SOURCE_KNOWLEDGE_DIR / f"{domain}.md"
        _copy_file(origin, config.knowledge_dir / f"{domain}.md")
        _copy_file(origin, staging / BACKUP_DIR / SOURCE_KNOWLEDGE_DIR / f"{domain}.md")
    _copy_file(snapshot.source / SOURCE_VECTOR_DB, staging / VECTOR_RELATIVE)
    _copy_file(
        snapshot.source / SOURCE_VECTOR_DB, staging / BACKUP_DIR / SOURCE_VECTOR_DB
    )
    changelog = snapshot.source / SOURCE_CHANGELOG
    if changelog.is_file():
        _copy_file(changelog, staging / CHANGELOG_RELATIVE)
        _copy_file(changelog, staging / BACKUP_DIR / SOURCE_CHANGELOG)
    # The manifest records the complete source file inventory, including
    # harmless empty SQLite sidecars. Preserve that exact set under backup so
    # source_files verification has independent evidence for every declaration.
    for item in snapshot.inventory.files:
        _copy_file(snapshot.source / item.path, staging / BACKUP_DIR / item.path)
    invalidate_scan_cache(str(config.knowledge_dir))

    counts: dict[str, Any] = {}
    seeds: dict[str, str] = {}
    journal_facts: dict[str, Any] = {}

    with MemoryEventJournal(config.graph_journal_path) as journal:
        # 2. The authority.
        append = journal.append(
            bootstrap.report.request_id,
            bootstrap.events,
            kind=REQUEST_KIND_BOOTSTRAP,
        )
        events = journal.read_events()
        head = journal.head_position()
        journal_facts = {
            "request_id": append.request_id,
            "request_kind": REQUEST_KIND_BOOTSTRAP,
            "request_hash": canonical_request_hash(events),
            "events": len(events),
            "head_position": head,
        }

        # 3. The graph, from the persisted journal. ``publish_graph`` is the
        #    framework's own publisher, and it is handed the *complete* stream a
        #    rebuild replaces the graph with — never the planning store's rows.
        try:
            with _writable_graph_store(config.graph_db_path) as store:
                publish_graph(store, events)
                health = store.health()
                counts["graph_nodes"] = health.node_count
                counts["graph_edges"] = health.edge_count
        except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
            label = (
                str(exc)[:120]
                if isinstance(exc, ProjectionError)
                else type(exc).__name__
            )
            findings.append(f"{FINDING_BUILD_FAILED}:graph:{label}")
            return GenerationBuild(staging, {}, findings)
        journal.set_checkpoint(PROJECTION_GRAPH, head)

        # 4. The readable surfaces, registered with what the documents say.
        nodes = {node.id: node for node in _replay_nodes(events)}
        surfaces: dict[str, str] = {}
        for node_id in sorted(bootstrap.bindings):
            binding = bootstrap.bindings[node_id]
            node = nodes.get(node_id)
            if node is None:
                findings.append(f"{FINDING_SURFACE_REGISTRY}:{binding.entry_id}")
                continue
            document = config.knowledge_dir / f"{binding.domain}.md"
            raw = document.read_text(encoding="utf-8")
            body = read_section_body(raw, binding.section, binding.occurrence)
            expected = node.content.strip()
            if body is None:
                findings.append(f"{FINDING_SURFACE_ABSENT}:{binding.entry_id}")
                continue
            if body_sha256(body) != body_sha256(expected):
                findings.append(
                    f"{FINDING_SURFACE_BODY_UNALIGNED}:{binding.entry_id}"
                )
                continue
            journal.record_surface(
                SurfaceRecord(
                    entry_id=binding.entry_id,
                    domain=binding.domain,
                    section=binding.section,
                    occurrence=binding.occurrence,
                    node_id=node_id,
                    body_sha256=body_sha256(body),
                    position=head,
                )
            )
            surfaces[binding.entry_id] = node_id
        if findings:
            return GenerationBuild(staging, {}, findings)
        journal.set_checkpoint(PROJECTION_L1, head)

        # 5. The vector rows: metadata only, through the store's own boundary.
        with VectorStore(staging / VECTOR_RELATIVE) as vectors:
            expected_rows = vectors.snapshot()
            planned = plan_graph_seed_backfill(
                expected_rows,
                [item.entry for item in snapshot.entries],
                snapshot.changelog_events,
            )
            updates = _vector_updates(snapshot, planned)
            application = apply_graph_seed_backfill(
                vectors,
                BackfillPlan(report=planned.report, updates=updates),
                expected=expected_rows,
                # The identity mapping deliberately resolves rows the planner
                # itself reports as unresolved, so coverage is asserted by this
                # tool's own inventory (which blocked anything unresolved) and
                # not a second time by the planner's row-coverage gate.
                require_clean=not snapshot.identity_map_updates,
            )
            settled = (
                application.applied
                or application.status == BACKFILL_NOTHING_TO_DO
            )
            if not settled:
                findings.append(
                    f"{FINDING_BUILD_FAILED}:vector:{application.status}"
                )
                return GenerationBuild(staging, {}, findings)
            counts["vector_rows"] = len(expected_rows)
            counts["vector_rows_changed"] = application.rows_changed
            for row in vectors.snapshot():
                metadata = json.loads(row[4]) if row[4] else {}
                node_id = metadata.get(GRAPH_SEED_ID_KEY)
                if isinstance(node_id, str) and node_id:
                    seeds[str(row[0])] = node_id
        journal.set_checkpoint(PROJECTION_VECTOR, head)

        # 6. L0, regenerated from the published L1 files.
        try:
            l0 = publish_l0(config)
        except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
            findings.append(f"{FINDING_BUILD_FAILED}:l0:{type(exc).__name__}")
            return GenerationBuild(staging, {}, findings)
        counts["l0_entries"] = int(l0.get("entries", 0))
        journal.set_checkpoint(PROJECTION_L0, head)
        checkpoints = {
            name: journal.checkpoint(name).position for name in PROJECTION_NAMES
        }

    counts.update(
        {
            "l1_documents": len(snapshot.documents),
            "l1_entries": len(snapshot.entries),
            "changelog_events": len(snapshot.changelog_events),
            "journal_events": journal_facts["events"],
            "surfaces_registered": len(surfaces),
            "external_surfaces": len(bootstrap.report.external_surfaces),
            "rows_by_category": dict(snapshot.inventory.counts_by_category),
        }
    )

    artifacts, inventory_findings = generation_inventory(staging)
    findings.extend(inventory_findings)

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "tool": "layered_memory_mcp.graph_migration",
        "source": str(snapshot.source),
        "destination": str(destination),
        "destination_prestate": prestate,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": snapshot.inventory.source_fingerprint,
        "source_files": {
            item.path: {"size": item.size, "sha256": item.sha256}
            for item in snapshot.inventory.files
        },
        "artifacts": artifacts,
        "counts": counts,
        "checkpoints": checkpoints,
        "journal": journal_facts,
        "seeds": seeds,
        "surfaces": surfaces,
        "external_surfaces": list(bootstrap.report.external_surfaces),
        "identity_map": dict(snapshot.identity_map),
        "provenance": {
            "recorded_at": bootstrap.report.recorded_at,
            "inferred_fields": list(bootstrap.report.inferred_fields),
            "unresolved_references": list(bootstrap.report.unresolved_references),
            "importer_skipped": bootstrap.report.importer_skipped,
        },
    }
    if not findings:
        write_manifest(staging, manifest)
    return GenerationBuild(staging, manifest, findings)


@contextmanager
def _writable_graph_store(path: Path) -> Iterator[Any]:
    """Open a writable embedded graph store at ``path``, closed on every exit.

    The import is function-local and the handle is scoped so that the *only*
    writable graph handle this module can hold lives inside a staged build: the
    verification path opens a read-only one, and closing here before anything is
    hashed is what keeps a ``-wal``/``-shm`` companion file out of a finished
    generation.
    """
    from .graph_sqlite_store import SQLiteGraphStore

    store = SQLiteGraphStore(path)
    try:
        yield store
    finally:
        store.close()


def _replay_nodes(events: Sequence[Any]) -> list[GraphNode]:
    projection = InMemoryGraphStore()
    projection.rebuild(list(events))
    return [
        GraphNode.model_validate(payload)
        for payload in projection.snapshot()["nodes"]
    ]


# ---------------------------------------------------------------------------
# Publication, withdrawal, quarantine
# ---------------------------------------------------------------------------

def publish_generation(staging: Path, generation: Path) -> None:
    """Move a staged generation into place with one atomic rename.

    The whole transaction. ``os.rename`` of a directory is atomic within a
    filesystem, and the staging directory is a sibling of the target precisely
    so that it is one. An existing target is refused explicitly: on POSIX a
    rename over an *empty* directory succeeds, which would silently consume
    whatever an operator had put there.
    """
    if generation.exists():
        raise FileExistsError(FINDING_GENERATION_EXISTS)
    os.rename(staging, generation)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def withdraw_generation(generation: Path, destination: Path) -> Path:
    """Rename a published generation into ``rollback/`` and return the new path.

    A withdrawal, not a deletion: the whole generation — journal, graph,
    readable files, vector database, source backup and manifest — is retained
    under its new name. Nothing in this module removes a tree, so a rollback can
    itself be reviewed, and the source backup stays recoverable afterwards.
    """
    retained = destination / ROLLBACK_DIR
    retained.mkdir(parents=True, exist_ok=True)
    target = retained / f"withdrawn-{_stamp()}"
    os.rename(generation, target)
    return target


def quarantine_staging(staging: Path, destination: Path) -> Path:
    """Move a staged generation that never published into ``debris/``.

    Explicitly accounted for rather than deleted or left in place under a name
    a later run might pick up: the operator can inspect exactly what was built,
    and :func:`new_staging_path` never reuses it.
    """
    debris = destination / DEBRIS_DIR
    debris.mkdir(parents=True, exist_ok=True)
    target = debris / f"staged-{_stamp()}"
    os.rename(staging, target)
    return target


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

class VerificationReport(GraphModel):
    """Whether one generation is exactly what its manifest says it is.

    ``generation_intact`` and ``source_drift`` are deliberately two claims. A
    generation can be byte-perfect while the source it was taken from has moved
    on; reporting that as one boolean would make "your migration is corrupt" and
    "your source changed afterwards" the same answer.
    """

    generation: str = ""
    manifest_ok: bool = False
    membership_ok: bool = False
    hashes_ok: bool = False
    journal_ok: bool = False
    graph_ok: bool = False
    replay_equal: bool = False
    surfaces_ok: bool = False
    source_files_ok: bool = False
    vector_ok: bool = False
    counts_ok: bool = False
    generation_intact: bool = False
    source_drift: bool | None = None
    artifacts_checked: int = Field(default=0, ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    findings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.generation_intact and not self.source_drift

    def to_payload(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["ok"] = self.ok
        return payload


def _read_rows(path: Path) -> list[tuple]:
    return read_vector_rows(path)


def _verify_source_files(
    generation: Path, declared: Any
) -> tuple[bool, list[str]]:
    """Compare manifest source claims with the frozen source backup.

    ``source_files`` is provenance, not a generation artifact: changing it does
    not change any file under ``current/`` and therefore cannot be caught by the
    ordinary artifact hash loop. The backup is the independent evidence used to
    recompute this claim. Its paths are checked with the same containment rule
    as artifact paths so a forged manifest cannot make verification read outside
    the generation.
    """
    if not isinstance(declared, dict):
        return False, [f"{FINDING_SOURCE_FILES_UNREADABLE}:not_a_mapping"]

    backup = generation / BACKUP_DIR
    if not backup.is_dir() or backup.is_symlink():
        return False, [f"{FINDING_SOURCE_FILES_UNREADABLE}:backup_absent"]

    actual, inventory_findings = generation_inventory(
        backup, allow_sqlite_sidecars=True
    )
    findings = [
        f"{FINDING_SOURCE_FILES_UNREADABLE}:{finding}"
        for finding in inventory_findings
    ]
    declared_paths = set(declared)

    for relative in sorted(declared_paths, key=str):
        member = _safe_member(backup, relative)
        if member is None:
            findings.append(f"{FINDING_SOURCE_FILE_PATH}:{relative}")
            continue
        expected = declared[relative]
        if not isinstance(expected, dict):
            findings.append(f"{FINDING_SOURCE_FILES_UNREADABLE}:{relative}")
            continue
        observed = actual.get(relative)
        if observed is None:
            findings.append(f"{FINDING_SOURCE_FILE_MISSING}:{relative}")
            continue
        if expected.get("sha256") != observed["sha256"]:
            findings.append(f"{FINDING_SOURCE_FILE_HASH}:{relative}")
        if expected.get("size") != observed["size"]:
            findings.append(f"{FINDING_SOURCE_FILE_SIZE}:{relative}")

    for relative in sorted(set(actual) - declared_paths):
        findings.append(f"{FINDING_SOURCE_FILE_UNEXPECTED}:{relative}")

    return not findings, findings


def verify_generation(
    generation: Path, *, source_fingerprint: str | None = None
) -> VerificationReport:
    """Prove a generation, reading only. Never writes, creates or repairs.

    Every claim is recomputed rather than believed:

    * the manifest's own digest, then every artifact path it names (refused if
      it is absolute, escapes the tree or traverses a symlink);
    * the exact membership of the tree and the hash and size of every file;
    * the journal, through
      :meth:`~layered_memory_mcp.graph_journal.MemoryEventJournal.open_readonly`
      — which creates no database, no schema and no checkpoint row — and its
      recorded request identity, payload hash and checkpoints;
    * the graph, through the read-only backend entrance, compared against a
      *replay* of the journal into a throwaway projection: journal-replay
      equality is the property that makes the journal the authority;
    * every readable surface the registry claims, by re-reading its body out of
      the document and comparing the hash;
    * every vector row, against the frozen backup for its identity, domain,
      text and embedding, and against the manifest's seeds and the replayed
      graph for the node it points at;
    * and only then the manifest's counts.

    ``source_fingerprint`` additionally compares the current source against the
    frozen one; drift is reported as drift rather than quietly accepted.
    """
    findings: list[str] = []
    warnings: list[str] = []
    report_counts: dict[str, int] = {}

    if not generation.exists():
        return VerificationReport(
            generation=str(generation), findings=[FINDING_GENERATION_ABSENT]
        )
    if generation.is_symlink() or not generation.is_dir():
        return VerificationReport(
            generation=str(generation), findings=[FINDING_GENERATION_NOT_A_DIRECTORY]
        )

    manifest, manifest_findings = read_manifest(generation)
    findings.extend(manifest_findings)
    if manifest is None:
        return VerificationReport(generation=str(generation), findings=findings)

    declared = manifest.get("artifacts")
    if not isinstance(declared, dict):
        findings.append(f"{FINDING_MANIFEST_UNREADABLE}:artifacts")
        return VerificationReport(generation=str(generation), findings=findings)

    # -- paths, membership and hashes ---------------------------------------
    safe: dict[str, Path] = {}
    for relative in sorted(declared, key=str):
        member = _safe_member(generation, relative)
        if member is None:
            findings.append(f"{FINDING_MANIFEST_PATH_REJECTED}:{relative}")
            continue
        safe[str(relative)] = member

    present, inventory_findings = generation_inventory(generation)
    findings.extend(inventory_findings)

    membership_ok = set(present) == set(safe)
    for relative in sorted(set(safe) - set(present)):
        findings.append(f"{FINDING_ARTIFACT_MISSING}:{relative}")
    for relative in sorted(set(present) - set(safe)):
        findings.append(f"{FINDING_ARTIFACT_UNEXPECTED}:{relative}")

    hashes_ok = True
    for relative in sorted(set(safe) & set(present)):
        expected = declared[relative]
        actual = present[relative]
        if not isinstance(expected, dict):
            findings.append(f"{FINDING_MANIFEST_UNREADABLE}:artifacts:{relative}")
            hashes_ok = False
            continue
        if expected.get("sha256") != actual["sha256"]:
            findings.append(f"{FINDING_ARTIFACT_HASH}:{relative}")
            hashes_ok = False
        if expected.get("size") != actual["size"]:
            findings.append(f"{FINDING_ARTIFACT_SIZE}:{relative}")
            hashes_ok = False

    source_files_ok, source_file_findings = _verify_source_files(
        generation, manifest.get("source_files")
    )
    findings.extend(source_file_findings)

    # -- the journal --------------------------------------------------------
    journal_ok = False
    events: list[Any] = []
    checkpoints: dict[str, int] = {}
    surfaces: dict[str, SurfaceRecord] = {}
    declared_journal = manifest.get("journal") or {}
    try:
        with MemoryEventJournal.open_readonly(generation / JOURNAL_NAME) as journal:
            events = journal.read_events()
            head = journal.head_position()
            checkpoints = {
                name: journal.checkpoint(name).position for name in PROJECTION_NAMES
            }
            surfaces = {
                record.entry_id: record for record in journal.surfaces()
            }
            request_id = str(declared_journal.get("request_id", ""))
            recorded_hash = journal.request_hash(request_id) if request_id else None
            journal_ok = True
            if recorded_hash is None:
                findings.append(f"{FINDING_JOURNAL_REQUEST}:absent")
                journal_ok = False
            if canonical_request_hash(events) != declared_journal.get("request_hash"):
                findings.append(f"{FINDING_JOURNAL_REQUEST}:payload")
                journal_ok = False
            if len(events) != declared_journal.get("events"):
                findings.append(f"{FINDING_JOURNAL_COUNTS}:events")
                journal_ok = False
            if head != declared_journal.get("head_position"):
                findings.append(f"{FINDING_JOURNAL_COUNTS}:head")
                journal_ok = False
            for name, position in sorted(checkpoints.items()):
                if position != (manifest.get("checkpoints") or {}).get(name):
                    findings.append(f"{FINDING_JOURNAL_CHECKPOINT}:{name}")
                    journal_ok = False
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        findings.append(f"{FINDING_JOURNAL_UNREADABLE}:{type(exc).__name__}")

    report_counts["journal_events"] = len(events)

    # -- the graph, against a replay of that journal ------------------------
    replay = InMemoryGraphStore()
    rebuild = replay.rebuild(events)
    if not rebuild.ok:
        findings.append(f"{FINDING_GRAPH_REPLAY}:journal_not_projectable")
    replayed = replay.snapshot()
    nodes = {
        payload["id"]: GraphNode.model_validate(payload)
        for payload in replayed["nodes"]
    }
    report_counts["graph_nodes"] = len(nodes)
    report_counts["graph_edges"] = len(replayed["edges"])

    graph_ok = False
    replay_equal = False
    stored_snapshot: dict[str, Any] | None = None
    try:
        from .graph_runtime_assembly import graph_store_readonly_session

        with graph_store_readonly_session(
            GenerationConfig(root=generation), require=True
        ) as assembly:
            if assembly.store is None:
                findings.append(f"{FINDING_GRAPH_UNREADABLE}:{assembly.report.status}")
            else:
                health = assembly.store.health()
                if not health.healthy:
                    findings.append(f"{FINDING_GRAPH_UNHEALTHY}:{len(health.errors)}")
                else:
                    graph_ok = True
                stored_snapshot = assembly.store.snapshot()
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        findings.append(f"{FINDING_GRAPH_UNREADABLE}:{type(exc).__name__}")

    if stored_snapshot is not None:
        replay_equal = stored_snapshot == replayed
        if not replay_equal:
            findings.append(f"{FINDING_GRAPH_REPLAY}:snapshot_differs")

    # -- the readable surfaces ---------------------------------------------
    surfaces_ok = True
    declared_surfaces = manifest.get("surfaces") or {}
    for entry_id in sorted(declared_surfaces, key=str):
        node_id = declared_surfaces[entry_id]
        record = surfaces.get(str(entry_id))
        if record is None or record.node_id != node_id:
            findings.append(f"{FINDING_SURFACE_REGISTRY}:{entry_id}")
            surfaces_ok = False
            continue
        document = generation / SOURCE_KNOWLEDGE_DIR / f"{record.domain}.md"
        if not document.is_file():
            findings.append(f"{FINDING_SURFACE_ABSENT}:{entry_id}")
            surfaces_ok = False
            continue
        body = read_section_body(
            document.read_text(encoding="utf-8"), record.section, record.occurrence
        )
        if body is None or body_sha256(body) != record.body_sha256:
            findings.append(f"{FINDING_SURFACE_BODY}:{entry_id}")
            surfaces_ok = False
            continue
        node = nodes.get(str(node_id))
        if node is None or body_sha256(node.content.strip()) != record.body_sha256:
            findings.append(f"{FINDING_SURFACE_BODY}:{entry_id}:graph")
            surfaces_ok = False
    report_counts["surfaces_registered"] = len(declared_surfaces)

    # -- the vector rows ----------------------------------------------------
    vector_ok = True
    declared_seeds = manifest.get("seeds") or {}
    try:
        published = _read_rows(generation / VECTOR_RELATIVE)
        frozen = _read_rows(generation / BACKUP_DIR / SOURCE_VECTOR_DB)
    except sqlite3.Error as exc:
        findings.append(f"{FINDING_VECTOR_UNREADABLE}:{type(exc).__name__}")
        published, frozen = [], []
        vector_ok = False

    report_counts["vector_rows"] = len(published)
    frozen_by_id = {str(row[0]): row for row in frozen}
    for row in published:
        row_id = str(row[0])
        original = frozen_by_id.get(row_id)
        if original is None:
            findings.append(f"{FINDING_VECTOR_ROW_UNEXPECTED}:{row_id}")
            vector_ok = False
            continue
        if tuple(row[1:4]) != tuple(original[1:4]):
            # ``domain``, ``text`` and ``vector`` are the authored and
            # retrieval data, compared against the frozen row of the same ID. A
            # migration changes ``metadata`` and nothing else, and this is
            # where "we did not re-embed" stops being a promise.
            findings.append(f"{FINDING_VECTOR_EMBEDDING}:{row_id}")
            vector_ok = False
        metadata = json.loads(row[4]) if row[4] else {}
        expected_node = declared_seeds.get(row_id)
        if expected_node is None:
            findings.append(f"{FINDING_VECTOR_ROW_UNEXPECTED}:{row_id}")
            vector_ok = False
            continue
        if metadata.get(GRAPH_SEED_ID_KEY) != expected_node:
            findings.append(f"{FINDING_VECTOR_SEED}:{row_id}")
            vector_ok = False
            continue
        if expected_node not in nodes:
            findings.append(f"{FINDING_VECTOR_SEED_UNKNOWN_NODE}:{row_id}")
            vector_ok = False
    present_rows = {str(row[0]) for row in published}
    for row_id in sorted(set(map(str, declared_seeds)) - present_rows):
        findings.append(f"{FINDING_VECTOR_ROW_MISSING}:{row_id}")
        vector_ok = False

    # -- and only now, the counts the manifest claims -----------------------
    counts_ok = True
    declared_counts = manifest.get("counts") or {}
    for name, value in sorted(report_counts.items()):
        if name in declared_counts and declared_counts[name] != value:
            findings.append(f"{FINDING_COUNTS}:{name}")
            counts_ok = False

    # -- source drift -------------------------------------------------------
    drift: bool | None = None
    if source_fingerprint is not None:
        drift = source_fingerprint != manifest.get("source_fingerprint")
        if drift:
            findings.append(FINDING_SOURCE_DRIFT)

    structural = [
        finding for finding in findings if finding != FINDING_SOURCE_DRIFT
    ]
    return VerificationReport(
        generation=str(generation),
        manifest_ok=not manifest_findings,
        membership_ok=membership_ok,
        hashes_ok=hashes_ok,
        journal_ok=journal_ok,
        graph_ok=graph_ok,
        replay_equal=replay_equal,
        surfaces_ok=surfaces_ok,
        source_files_ok=source_files_ok,
        vector_ok=vector_ok,
        counts_ok=counts_ok,
        generation_intact=not structural,
        source_drift=drift,
        artifacts_checked=len(present),
        counts=report_counts,
        findings=sorted(set(findings)),
        warnings=sorted(set(warnings)),
    )

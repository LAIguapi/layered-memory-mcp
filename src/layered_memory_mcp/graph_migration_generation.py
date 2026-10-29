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
        served/<manifest digest>.json  a generation a runtime was given
        .served.lock                   the generation guard, while it is held

Only those names are ever created, and any *other* top-level entry in a
destination is unknown content this module refuses to work beside. The last two
are the *control plane* rather than generation data: ``served/`` is the
admission ledger a withdrawal consults (see "Admission" below) and
``.served.lock`` is the guard an activation and a withdrawal share. Both are
owned as strictly as everything else — the ledger's own contents are a closed
set of this tool's records, the lock is tolerated only as a regular file, and
neither is a licence for anything else to appear in a destination.

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
import stat
import threading
import time
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
    _decode_row_metadata,
    file_sha256,
    read_source_snapshot,
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
    "LOCK_TIMEOUT_SECONDS",
    "MANIFEST_DIGEST_NAME",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "ROLLBACK_DIR",
    "SERVED_DIR",
    "SERVED_LOCK_NAME",
    "SERVED_MARKER_SUFFIX",
    "SERVED_VERSION",
    "STAGING_PREFIX",
    "FINDING_ADMISSION_UNPROVED",
    "FINDING_GENERATION_SERVED",
    "FINDING_LOCK_PATH_REJECTED",
    "FINDING_LOCK_UNAVAILABLE",
    "FINDING_SERVED_REJECTED",
    "FINDING_SOURCE_DRIFT",
    "ControlLockError",
    "GenerationBuild",
    "GenerationConfig",
    "VerificationReport",
    "admission_findings",
    "admitted_digests",
    "build_generation",
    "control_lock",
    "lock_path_findings",
    "destination_findings",
    "destination_prestate",
    "generation_inventory",
    "manifest_digest",
    "new_staging_path",
    "owning_destination",
    "publish_generation",
    "quarantine_staging",
    "read_manifest",
    "register_admission",
    "served_findings",
    "served_lock_path",
    "served_marker_path",
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

#: The destination's admission ledger: one marker per generation a runtime has
#: been given, named by the manifest digest its pointer attested. See
#: "Admission" below for why the record lives here and never inside the
#: generation it describes.
SERVED_DIR = "served"

#: The generation guard's lock file, a top-level sibling of ``served/``. At the
#: top level rather than inside the ledger so that taking the guard does not
#: touch the directory whose contents *are* the record.
SERVED_LOCK_NAME = ".served.lock"

#: The suffix of one admission record. The stem is its manifest digest.
SERVED_MARKER_SUFFIX = ".json"

#: The admission record layout version. Recorded, checked, never migrated: a
#: record this process does not understand makes the destination refusable
#: rather than being read past.
SERVED_VERSION = 1

#: The only top-level *directory* names this tool ever creates in a
#: destination. Anything else there belongs to somebody else, and a migration
#: that wrote beside it would be adopting a directory it was merely pointed at.
#: ``served`` is the admission ledger below — a control-plane directory, whose
#: own contents are owned and validated just as strictly.
DESTINATION_ENTRIES = frozenset({GENERATION_DIR, ROLLBACK_DIR, DEBRIS_DIR, SERVED_DIR})

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
        if name == SERVED_LOCK_NAME:
            # The admission guard's lock file: owned by this tool, present only
            # while an activation or a withdrawal is holding it. Tolerated by
            # name and only as a regular file, never as a licence for other
            # files or for a link standing in for one.
            if child.is_symlink() or not child.is_file():
                findings.append(f"{FINDING_DESTINATION_UNKNOWN}:{name}")
            continue
        if name not in DESTINATION_ENTRIES or child.is_symlink() or not child.is_dir():
            findings.append(f"{FINDING_DESTINATION_UNKNOWN}:{name}")
    # The admission ledger is a destination entry like any other: its *contents*
    # are owned too, so a stray file or a forged record in there makes the
    # destination refusable rather than quietly ignored.
    findings.extend(served_findings(destination))
    return findings, warnings


# ---------------------------------------------------------------------------
# Admission — which generations a runtime has been given
#
# The record a legacy withdrawal consults before it moves anything. It lives
# here, beside the destination's own shape, for two reasons: the withdrawal is
# given nothing but a destination, so this is the only place it can look; and a
# served generation is *writable* and its manifest is the attestation a pointer
# binds, so a marker written inside it would be editing the evidence.
#
# Conservative on purpose. An admission is never retracted: "no pointer argument
# was supplied" and "the pointer no longer mentions it" are not proofs that
# nothing is using a generation, and a withdrawal that acted on either of them
# is the availability failure this ledger exists to prevent. Re-publishing is
# unaffected — a fresh generation has a fresh manifest digest and therefore no
# record.
# ---------------------------------------------------------------------------

#: A generation a runtime was given, and which therefore may not be moved by a
#: legacy withdrawal. Durable, and deliberately not conditional on the
#: generation still being referenced.
FINDING_GENERATION_SERVED = "generation_admitted_to_runtime"

#: The ledger holds something this tool did not write, or wrote in a version it
#: does not speak. Suffixed with the entry and the reason. Fail-closed: every
#: caller refuses, because an unreadable ledger cannot establish that a
#: generation was *not* admitted.
FINDING_SERVED_REJECTED = "served_record_rejected"

#: Whether this generation was ever admitted could not be established — its
#: manifest is missing, unreadable or does not match its own digest, so there is
#: no identity to look up. A refusal, for the same reason.
FINDING_ADMISSION_UNPROVED = "generation_admission_unproved"

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_digest(value: Any) -> bool:
    """Whether ``value`` is a lowercase 64-character hexadecimal digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and frozenset(value) <= _HEX_DIGITS
    )


def owning_destination(generation: Path) -> Path | None:
    """The destination whose ``rollback`` verb could move ``generation``.

    Derived from the two positions this module ever puts a generation in — the
    publication target and a retained withdrawal — rather than from a search:
    ``<destination>/current`` and ``<destination>/rollback/withdrawn-…`` are the
    only trees a withdrawal can reach, so a generation anywhere else needs no
    admission record to be safe from one. ``None`` says exactly that.
    """
    parent = generation.parent
    if generation.name == GENERATION_DIR:
        return parent
    if parent.name == ROLLBACK_DIR:
        return parent.parent
    return None


def served_lock_path(destination: Path) -> Path:
    """The validated reserved name for the destination's directory-inode guard."""
    return destination / SERVED_LOCK_NAME


def served_marker_path(destination: Path, digest: str) -> Path:
    """The admission record for the generation attesting ``digest``."""
    return destination / SERVED_DIR / f"{digest}{SERVED_MARKER_SUFFIX}"


def _read_admission(marker: Path) -> tuple[dict[str, Any] | None, str]:
    """``(record, refusal label)`` for one admission marker. Reads only."""
    try:
        body = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, type(exc).__name__
    if not isinstance(body, dict):
        return None, "not_a_mapping"
    if body.get("served_version") != SERVED_VERSION:
        return None, "version_unsupported"
    digest = body.get("manifest_sha256")
    if not _is_digest(digest) or f"{digest}{SERVED_MARKER_SUFFIX}" != marker.name:
        return None, "digest_mismatch"
    declared = body.get("generation")
    destination = marker.parent.parent
    if not isinstance(declared, str) or _safe_member(destination, declared) is None:
        return None, "generation_rejected"
    return body, ""


def served_findings(destination: Path) -> list[str]:
    """Why a destination's admission ledger may not be believed, if it may not.

    Reads only, creates nothing, and names every entry it refuses. The ledger is
    a closed set of names — one ``<64 hex digits>.json`` record per admitted
    generation, each a regular file this tool wrote — so a planted file, a
    symlink, a forged record or an unknown version is a refusal rather than an
    entry to skip. Skipping one would be answering "never admitted" from a
    ledger that could not be read.
    """
    served = destination / SERVED_DIR
    if served.is_symlink():
        return [f"{FINDING_SERVED_REJECTED}:{SERVED_DIR}:symlink"]
    if not served.exists():
        return []
    if not served.is_dir():
        return [f"{FINDING_SERVED_REJECTED}:{SERVED_DIR}:not_a_directory"]

    findings: list[str] = []
    for child in sorted(served.iterdir(), key=lambda path: path.name):
        name = child.name
        if child.is_symlink():
            findings.append(f"{FINDING_SERVED_REJECTED}:{name}:symlink")
            continue
        if not child.is_file():
            findings.append(f"{FINDING_SERVED_REJECTED}:{name}:not_a_file")
            continue
        stem = name[: -len(SERVED_MARKER_SUFFIX)]
        if not name.endswith(SERVED_MARKER_SUFFIX) or not _is_digest(stem):
            findings.append(f"{FINDING_SERVED_REJECTED}:{name}:unknown")
            continue
        _record, rejection = _read_admission(child)
        if rejection:
            findings.append(f"{FINDING_SERVED_REJECTED}:{name}:{rejection}")
    return findings


def admitted_digests(destination: Path) -> tuple[frozenset[str], list[str]]:
    """``(admitted manifest digests, findings)`` for one destination.

    Reads only: an absent ledger is an empty set and not a fault — that is the
    ordinary state of a destination nothing has served. Any finding makes the
    set meaningless and every caller refuses on it.
    """
    findings = served_findings(destination)
    if findings:
        return frozenset(), findings
    served = destination / SERVED_DIR
    if not served.is_dir():
        return frozenset(), []
    return (
        frozenset(
            path.name[: -len(SERVED_MARKER_SUFFIX)]
            for path in served.iterdir()
            if path.name.endswith(SERVED_MARKER_SUFFIX)
        ),
        [],
    )


def register_admission(
    destination: Path, generation: Path, digest: str, *, tool: str
) -> Path:
    """Record that ``generation`` was admitted to a runtime. Idempotent.

    Written before the pointer that publishes it, under the generation guard, so
    a withdrawal either sees the record and refuses or has already moved the
    generation and makes the activation refuse. The record is one small JSON
    object, written to a private temporary file and moved into place, and an
    existing record for the same digest is left exactly as it is: a repeated
    activation of a generation already admitted writes nothing at all.

    Raises:
        ValueError: ``digest`` is not a manifest digest, so there is no identity
            to record. The caller has proved the generation by this point, so
            this is a programming fault rather than an operator one.
    """
    if not _is_digest(digest):
        raise ValueError(FINDING_ADMISSION_UNPROVED)
    marker = served_marker_path(destination, digest)
    if marker.is_file():
        return marker
    served = marker.parent
    served.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "served_version": SERVED_VERSION,
            "tool": tool,
            "generation": generation.relative_to(destination).as_posix(),
            "manifest_sha256": digest,
            "admitted_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
        indent=2,
    ) + "\n"
    temporary = served / f".{digest[:12]}.{uuid.uuid4().hex[:12]}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    handle = os.open(served, os.O_RDONLY)
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - a filesystem that cannot sync a dir
        pass
    finally:
        os.close(handle)
    return marker


def admission_findings(destination: Path, generation: Path) -> list[str]:
    """Why ``generation`` may not be withdrawn from ``destination``, if it may not.

    Three answers and no inference between them: the ledger could not be read,
    the generation has no readable identity to look up, or it has one and it is
    in the ledger. An empty list means a readable ledger that does not contain
    this generation — the only basis on which a withdrawal may proceed.
    """
    digests, findings = admitted_digests(destination)
    if findings:
        return findings
    manifest, manifest_findings = read_manifest(generation)
    if manifest is None or manifest_findings:
        return [f"{FINDING_ADMISSION_UNPROVED}:manifest"]
    if manifest_digest(manifest) in digests:
        return [FINDING_GENERATION_SERVED]
    return []


# ---------------------------------------------------------------------------
# Control-plane mutual exclusion
#
# The one lock implementation the control plane has, used for the pointer
# interval (owned by :mod:`.graph_generation_runtime`) and for the generation
# admission guard below. One implementation rather than two, because two locks
# with two acquisition disciplines is how a deadlock or a missed interval is
# introduced.
# ---------------------------------------------------------------------------

#: How long an operation waits for a control-plane lock another process or
#: thread is holding. Long on purpose: the contended case is a second operator
#: command, and waiting for it is the correct answer. A refusal is reserved for
#: a lock that is genuinely unobtainable.
LOCK_TIMEOUT_SECONDS = 120.0

#: Why a lock could not be taken, as bounded labels carried on
#: :class:`ControlLockError`: another holder outlasted the timeout
#: (``busy``/``busy_thread``), the path may not hold a lock file
#: (``path_rejected:…``), or this thread already holds it (``reentrant``).
FINDING_LOCK_UNAVAILABLE = "control_lock_unavailable"
FINDING_LOCK_PATH_REJECTED = "control_lock_path_rejected"


class ControlLockError(RuntimeError):
    """A control-plane lock that could not be taken, named by a bounded label.

    Raised rather than returned because every caller is inside a report-building
    function that already knows how to turn one label into its own refusal
    status; an exception keeps the "we never entered the interval" path from
    looking like a result.
    """

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


#: One re-entrancy-detecting mutex per lock path, for threads of *this* process.
#: Thread ownership also detects reentry before waiting for the kernel lock.
#: The thread mutex is always taken *before* the file lock, in every
#: caller, which is what makes the two layers deadlock-free.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()

#: The lock paths this thread is already inside. A second acquisition of the
#: same path on one thread is a programming fault, and a plain mutex would
#: answer it with a hang; it is named and raised instead, so a mistaken nesting
#: (or an injected one) fails fast rather than stopping the process.
_HELD_PATHS = threading.local()


def lock_path_findings(path: Path) -> list[str]:
    """Why a lock file may not be created at ``path``, if it may not.

    Creates nothing. A symlink is refused rather than followed — a lock file is
    a control-plane artifact, and one that resolves elsewhere would serialise
    the wrong thing while looking exactly like serialisation. An existing
    non-file is refused, and so is a parent that is not already a directory:
    the directory a lock lives in is validated by the operation that owns it and
    is never brought into existence by taking a lock.
    """
    if path.is_symlink():
        return [f"{FINDING_LOCK_PATH_REJECTED}:symlink"]
    if path.exists() and not path.is_file():
        return [f"{FINDING_LOCK_PATH_REJECTED}:not_a_file"]
    parent = path.parent
    if parent.is_symlink():
        return [f"{FINDING_LOCK_PATH_REJECTED}:parent_symlink"]
    if not parent.is_dir():
        return [f"{FINDING_LOCK_PATH_REJECTED}:parent_absent"]
    return []


@contextmanager
def control_lock(
    path: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS, directory: bool = False
) -> Iterator[None]:
    """Hold one control-plane lock across threads **and** processes.

    Two layers, always in the same order — thread mutex, then file lock — so
    that two operations in one process exclude each other for the same reason
    two operations in two processes do, and so that no caller can produce a lock
    cycle by taking them the other way round.

    With ``directory=True``, lock the existing parent directory's inode instead
    of creating a file. All guards in that directory then share one lock,
    without changing any member or directory mtime. The reserved ``path``
    is still validated, but never opened. Unsupported directory locking refuses
    closed rather than falling back to a filesystem write or an unlocked run.

    Otherwise lock the named file's inode. Normal release unlinks the file
    while still holding its lock, then closes the descriptor. A waiter that
    opened the old inode must retry rather than enter on an unlinked lock.
    Process death releases the kernel lock; a subsequent holder removes any
    abandoned file on normal exit. Neither mode falls back to a soft lock.

    Raises:
        ControlLockError: the path may not hold a lock, this thread already
            holds it, or another holder outlasted ``timeout``. The interval was
            never entered, so the caller has written nothing.
    """
    if directory:
        with _directory_control_lock(path, timeout=timeout):
            yield
        return

    key = os.fspath(path)
    held = getattr(_HELD_PATHS, "paths", None)
    if held is None:
        held = set()
        _HELD_PATHS.paths = held
    if key in held:
        raise ControlLockError("reentrant")

    rejected = lock_path_findings(path)
    if rejected:
        raise ControlLockError(rejected[0])

    with _THREAD_LOCKS_GUARD:
        mutex = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not mutex.acquire(timeout=timeout):
        raise ControlLockError("busy_thread")
    held.add(key)

    try:
        with _file_control_lock(path, timeout=timeout):
            yield
    finally:
        held.discard(key)
        mutex.release()


@contextmanager
def _file_control_lock(path: Path, *, timeout: float) -> Iterator[None]:
    """Own the file inode and its unlink lifecycle, independently of filelock.

    Path identity is checked after acquisition: a waiter may have opened the
    inode before its last holder unlinked it. Cleanup only removes our own
    inode, and always closes the descriptor even if unlink fails.
    """
    try:
        import fcntl

        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    except (ImportError, AttributeError) as exc:
        raise ControlLockError(f"{FINDING_LOCK_PATH_REJECTED}:unsupported") from exc

    descriptor = None
    deadline = time.monotonic() + timeout
    try:
        try:
            while True:
                rejected = lock_path_findings(path)
                if rejected:
                    raise ControlLockError(rejected[0])
                descriptor = os.open(path, flags, 0o644)
                identity = os.fstat(descriptor)
                if not stat.S_ISREG(identity.st_mode):
                    raise ControlLockError(f"{FINDING_LOCK_PATH_REJECTED}:not_a_file")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    try:
                        current = path.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        current = None
                    if current is not None and (current.st_dev, current.st_ino) == (
                        identity.st_dev, identity.st_ino
                    ):
                        break
                os.close(descriptor)
                descriptor = None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlLockError("busy")
                time.sleep(min(0.05, remaining))
        except OSError as exc:
            raise ControlLockError(
                f"{FINDING_LOCK_PATH_REJECTED}:{type(exc).__name__}"
            ) from exc
        try:
            yield
        finally:
            try:
                current = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None and (current.st_dev, current.st_ino) == (
                identity.st_dev, identity.st_ino
            ):
                path.unlink()
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _directory_control_lock(path: Path, *, timeout: float) -> Iterator[None]:
    """Lock a read-only directory descriptor, with no create/unlink lifecycle.

    Kernel ownership ends on close or process death. Thread ownership is keyed
    by the same device/inode identity, not the reserved lock filename, so two
    guards in one root cannot deadlock through an undetected reentry.
    """
    try:
        import fcntl

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except (ImportError, AttributeError) as exc:
        raise ControlLockError(f"{FINDING_LOCK_PATH_REJECTED}:unsupported") from exc

    try:
        rejected = lock_path_findings(path)
        if rejected:
            raise ControlLockError(rejected[0])
        descriptor = os.open(path.parent, flags)
    except OSError as exc:
        raise ControlLockError(
            f"{FINDING_LOCK_PATH_REJECTED}:{type(exc).__name__}"
        ) from exc

    try:
        try:
            identity = os.fstat(descriptor)
        except OSError as exc:
            raise ControlLockError(
                f"{FINDING_LOCK_PATH_REJECTED}:{type(exc).__name__}"
            ) from exc
        key = f"directory:{identity.st_dev}:{identity.st_ino}"
        held = getattr(_HELD_PATHS, "paths", None)
        if held is None:
            held = set()
            _HELD_PATHS.paths = held
        if key in held:
            raise ControlLockError("reentrant")

        with _THREAD_LOCKS_GUARD:
            mutex = _THREAD_LOCKS.setdefault(key, threading.Lock())
        deadline = time.monotonic() + timeout
        if not mutex.acquire(timeout=timeout):
            raise ControlLockError("busy_thread")
        held.add(key)
        try:
            try:
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ControlLockError("busy")
                        time.sleep(min(0.05, remaining))
                current = path.parent.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise ControlLockError(f"{FINDING_LOCK_PATH_REJECTED}:parent_changed")
                rejected = lock_path_findings(path)
                if rejected:
                    raise ControlLockError(rejected[0])
            except OSError as exc:
                raise ControlLockError(
                    f"{FINDING_LOCK_PATH_REJECTED}:{type(exc).__name__}"
                ) from exc
            yield
        finally:
            held.discard(key)
            mutex.release()
    finally:
        os.close(descriptor)


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

    Two sources: the planner's updates for rows whose *own* identity resolves,
    and the inventory's validated explicit or exact automatic correspondences.
    A row ID is never rewritten by
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
        "automatic_identity_map": dict(snapshot.automatic_identity_map),
        "provenance": {
            "identity_bridge": {
                "version": 1,
                "rows": [row.to_dict() for row in snapshot.inventory.classifications],
            },
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


def _verify_identity_bridge(
    generation: Path, manifest: Mapping[str, Any], published: Sequence[tuple]
) -> list[str]:
    """Recompute correspondence and metadata from the frozen source, not seeds."""
    explicit = manifest.get("identity_map")
    if not isinstance(explicit, dict):
        return ["identity_bridge_explicit_invalid"]
    try:
        snapshot = read_source_snapshot(
            generation / BACKUP_DIR, identity_map_declarations=explicit
        )
    except (OSError, ValueError, sqlite3.Error):
        return ["identity_bridge_source_unreadable"]
    if not snapshot.publishable:
        return ["identity_bridge_source_unresolved"]
    findings: list[str] = []
    if snapshot.inventory.source_fingerprint != manifest.get("source_fingerprint"):
        findings.append("identity_bridge_source_fingerprint")
    if explicit != snapshot.identity_map:
        findings.append("identity_bridge_explicit_mismatch")
    automatic = manifest.get("automatic_identity_map")
    provenance = manifest.get("provenance")
    bridge = provenance.get("identity_bridge") if isinstance(provenance, dict) else None
    legacy = automatic is None and bridge is None and not snapshot.automatic_identity_map
    if not legacy:
        if automatic != snapshot.automatic_identity_map:
            findings.append("identity_bridge_automatic_mismatch")
        expected = {
            "version": 1,
            "rows": [row.to_dict() for row in snapshot.inventory.classifications],
        }
        if json.dumps(bridge, sort_keys=True) != json.dumps(expected, sort_keys=True):
            findings.append("identity_bridge_provenance_mismatch")
    seeds = {
        row.row_id: row.expected_graph_node_id
        for row in snapshot.inventory.classifications
    }
    if manifest.get("seeds") != seeds:
        findings.append("identity_bridge_seeds_mismatch")
    if len(set(seeds.values())) != len(seeds):
        findings.append("identity_bridge_target_collision")
    counts = manifest.get("counts")
    if (
        not isinstance(counts, dict)
        or counts.get("rows_by_category") != snapshot.inventory.counts_by_category
    ):
        findings.append("identity_bridge_counts_mismatch")
    expected_metadata = {
        row[0]: _decode_row_metadata(row[4]) for row in snapshot.vector_rows
    }
    if snapshot.backfill_plan is not None:
        for update in _vector_updates(snapshot, snapshot.backfill_plan):
            expected_metadata[update.row_id] = update.metadata
    for row in published:
        metadata = _decode_row_metadata(row[4])
        if metadata is None or json.dumps(metadata, sort_keys=True) != json.dumps(
            expected_metadata.get(row[0]), sort_keys=True
        ):
            findings.append("identity_bridge_metadata_mismatch")
    return sorted(set(findings))


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
    if not isinstance(declared_seeds, dict):
        findings.append("identity_bridge_seeds_mismatch")
        declared_seeds = {}
        vector_ok = False
    try:
        published = _read_rows(generation / VECTOR_RELATIVE)
        frozen = _read_rows(generation / BACKUP_DIR / SOURCE_VECTOR_DB)
    except (OSError, sqlite3.Error) as exc:
        findings.append(f"{FINDING_VECTOR_UNREADABLE}:{type(exc).__name__}")
        published, frozen = [], []
        vector_ok = False

    report_counts["vector_rows"] = len(published)
    if source_files_ok and membership_ok and hashes_ok:
        identity_findings = _verify_identity_bridge(generation, manifest, published)
        findings.extend(identity_findings)
        if identity_findings:
            vector_ok = False
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
        metadata = _decode_row_metadata(row[4])
        if metadata is None:
            findings.append(f"{FINDING_VECTOR_SEED}:{row_id}")
            vector_ok = False
            continue
        expected_node = declared_seeds.get(row_id)
        if not isinstance(expected_node, str) or not expected_node:
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
    counts_ok = "identity_bridge_counts_mismatch" not in findings
    declared_counts = manifest.get("counts") or {}
    if not isinstance(declared_counts, dict):
        findings.append(FINDING_COUNTS)
        declared_counts = {}
        counts_ok = False
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

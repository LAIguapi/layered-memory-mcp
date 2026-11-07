"""Serving a published generation: resolve one, prove it, cut over to it.

WHAT THIS MODULE IS
-------------------
The runtime half of the migration contract. :mod:`.graph_migration` *builds* a
verified generation and publishes it into a directory; this module answers the
two questions that come afterwards, and nothing else:

* **"Which generation am I serving, and may I?"** —
  :func:`resolve_generation_runtime` takes one explicit generation root *or* one
  explicit active-pointer file, proves the generation, and hands back a
  :class:`GenerationRuntimeConfig` scoped to it. A generation that cannot be
  proved is reported as ``invalid`` and no config is produced.
* **"Make that generation the active one."** — :func:`activate_generation` and
  :func:`restore_generation`, both read-only by default, both replacing one
  small control-plane file atomically after the target has been proved, holding
  that pointer's whole decide-and-write interval against every other process
  and thread, and recording the generation's admission where a later migration
  rollback will look before it moves anything.

::

    python -m layered_memory_mcp.graph_generation_runtime \\
        --pointer /srv/layered-memory/active.json          # read-only status

    … activate --pointer … --generation … [--apply]        # cut over
    … restore  --pointer …                [--apply]        # back to the retained one

The same three verbs are also reachable through the migration operator CLI
(``python -m layered_memory_mcp.graph_migration activate …``), which keeps one
tool for an operator and one implementation for both.

THE DEPLOYMENT LAYOUT
---------------------
::

    <control root>/                 the pointer's parent, and the only tree a
        active.json                 pointer may name a target inside
        .active.json.lock           reserved path, never created by the interval
        migrated/                   a graph_migration destination
            current/                the published generation
            rollback/withdrawn-…/   a retained withdrawn generation
            served/<digest>.json    this generation was given to a runtime
            .served.lock            the generation guard, while it is held

The pointer lives **beside** a migration destination, never inside one:
:func:`~layered_memory_mcp.graph_migration_generation.destination_findings`
owns the top-level names of a destination and refuses unknown ones, so a
pointer dropped in there would make the destination unusable to the very tool
that publishes into it.

Control artifacts beyond the pointer are deliberately *not* in the generation
they concern. The interval reserves a hidden sibling (:func:`pointer_lock_path`)
but locks the control root's existing inode without creating that file. The
admission ledger and its guard belong to the migration destination,
because the operation they have to stop — ``graph_migration rollback`` — is
given a destination and nothing else; they are owned, validated and refused by
that tool's own destination inventory. Nothing is ever written inside a
generation: its manifest is the attestation a pointer binds, and a tool keeping
notes in there would be editing its own evidence.

THE STATE CONTRACT
------------------
A generation is in exactly one of two states here, and they are proved in two
different ways because they *are* two different things::

    published   Never served; nothing has written into it. Every byte must
                still be the byte its manifest records, so the proof is the
                migration tool's own full ``verify_generation()``: membership,
                every artifact hash, the journal, a replay of it against the
                stored graph, the readable surfaces and the vector rows.
                    → integrity = "verified_publication"

    activated   A pointer attests that this generation passed that full proof
                at cutover time, and it has been *writable* ever since. The
                first framework write changes ``graph.db``, ``journal.db``, a
                readable document, ``L0.md`` and the vector rows — so the
                publication hashes stop describing it, by design. What is still
                true of a served generation, and therefore what is proved:
                the pointer's manifest binding (a runtime write never touches
                ``manifest.json``), the structural guards (nothing inside it is
                a symlink, no declared path leaves it), and read-only health
                plus journal↔graph projection consistency.
                    → integrity = "attested_activation"

Which state applies is **not** a caller's argument. It follows from the input:
an explicit generation root is ``published`` and is always fully verified, and a
pointer is ``activated`` and is proved against the attestation that pointer
carries. There is deliberately no flag that asks for the weaker proof on an
arbitrary directory, because that would make "never verified" and "verified"
the same answer for any fresh or tampered tree.

``generation_intact`` is reported only when the full publication proof actually
ran and passed. A served generation reports ``generation_intact=False`` with
``integrity="attested_activation"`` and ``attested=True``: claiming the first
would be claiming a proof nobody performed.

Reading the two states, as verbs: :func:`activate_generation` performs the
publication proof and records it; :func:`restore_generation` returns to a
generation the pointer already attested, so it proves the retained binding
rather than hashes that a write has legitimately moved on from; and a repeated
:func:`activate_generation` of the generation already being served is recognised
*before* the publication proof, so it stays a no-op after real writes: no
pointer, no admission record, no temporary file and no generation byte is
written.

The requirement is a literal one: an applying no-op writes nothing. A preview
meets it outright, taking no interval at all.

An applying no-op still decides inside the pointer's complete interval. That
interval locks a read-only descriptor of the existing control root, rather than
creating and unlinking a lock file inside it. The kernel releases the inode lock
on close or process death; no directory member or modification time changes.
Pointers sharing a control root share this interval. If the platform cannot
lock directory descriptors, an apply refuses closed without writing anything.

THE POINTER CONTRACT
--------------------
One small JSON object, written atomically, with exactly these keys
(:class:`ActivePointer` is its in-memory form, and ``to_dict()`` is literally
the file body)::

    {
      "pointer_version": 1,                 refused if not understood
      "tool": "layered_memory_mcp.graph_generation_runtime",
      "generation": "migrated/current",     control-root-relative, POSIX
      "activated_at": "2026-10-29T…+00:00",
      "manifest_sha256": "…",               the activation attestation, then
      "previous_generation": "…",           the pointer this one superseded,
      "previous_activated_at": "…",         retained so a restore is possible
      "previous_manifest_sha256": "…"
    }

``manifest_sha256`` is the attestation, not a convenience: it is written *only*
after a full publication proof passed, so a pointer carrying a digest that still
matches the generation's manifest is the record of that proof. An empty digest is
refused (:data:`FINDING_POINTER_ATTESTATION`) and a digest the generation no
longer has is refused (:data:`FINDING_POINTER_MANIFEST_CHANGED`) — a mutable
generation served on the strength of a control file that never recorded a proof
is the one bypass this module must not have. ``previous_manifest_sha256`` is the
same attestation for the retained generation, and :func:`restore_generation`
checks it against the target rather than against whatever it reads there.

``generation`` and ``previous_generation`` are *relative* on purpose: a pointer
target is resolved against the pointer's own parent and must stay strictly
inside it, reached without traversing a symlink. An absolute path, a ``..``
component, a symlinked step or a target outside the control root is refused by
name (:data:`FINDING_POINTER_TARGET_REJECTED`) rather than followed, so a forged
or hand-edited control file cannot redirect a runtime at somebody else's tree.
Unknown keys are reported as warnings and ignored: the format is the contract,
not its author.

WHAT IT IS NOT
--------------
- NOT a discoverer. There is no default pointer location, no probe, no fallback
  and no derivation from ``home``, ``config.yaml``, a ``LAYERED_MEMORY_*``
  variable or an installed service. Both inputs are arguments, and supplying
  both at once is a refusal rather than a precedence rule.
- NOT a writer of generation contents. Nothing here creates, repairs, rebuilds
  or deletes a generation, a database, a journal, a readable file or a
  directory. A state-changing ``apply`` writes only control-plane artifacts:
  the pointer and admission records under the migration destinations' guards.
  The pointer interval itself writes nothing. Without ``apply`` it writes
  nothing at all — not a lock, not a record, not a parent directory.
- NOT a deleter. A cutover retains the pointer it supersedes *inside* the new
  pointer, and a restore re-points at the retained generation while retaining
  the one it left — so a restore is itself reversible and no generation is ever
  removed.
- NOT a startup hook. Nothing imports this module on the default path;
  ``server``, ``injector``, ``recall`` and ``config`` are untouched and the
  default ``MemoryConfig()`` still reaches no graph at all. Activating a
  generation stays an explicit operator act.
- NOT a trusting reader. Every structural guard runs *before* a single byte of
  the generation is read: the path is rejected if any ancestor, the root itself
  or any of the generation's own members (``manifest.json`` and its digest
  sidecar, ``graph.db``, ``journal.db``, ``knowledge/``, ``data/``, ``L0.md``) is
  a symlink or the wrong kind of file — before :func:`read_manifest`, and before
  any database is opened. Only then is the manifest read, its own digest
  recomputed, its declared paths checked for containment, the pointer's binding
  compared, and the stores opened through their *read-only* entrances. A pointer
  that records a manifest digest the generation no longer has fails closed, and
  so does an activation over a pointer file this tool cannot parse — the corrupt
  bytes are left exactly as they are, because they are the only evidence of what
  the system thought it was serving.

PRIVACY (§15)
-------------
Reports carry bounded status tokens, counts, control-root-relative paths and the
operator-supplied pointer/generation paths — an operator tool must say what it
operated on. Never a memory body, a heading, a vector value or a backend
message: a failure is named by its exception class or by a refusal token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from pydantic import Field

from .config import GRAPH_MODES
from .graph_application import runtime_status
from .graph_migration import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, EXIT_USAGE
from .graph_migration_generation import (
    BACKUP_DIR,
    DATA_DIR,
    MANIFEST_DIGEST_NAME,
    MANIFEST_NAME,
    ROLLBACK_DIR,
    VECTOR_RELATIVE,
    ControlLockError,
    GenerationConfig,
    VerificationReport,
    admitted_digests,
    control_lock,
    manifest_digest,
    owning_destination,
    read_manifest,
    register_admission,
    served_lock_path,
    verify_generation,
)
from .graph_models import GraphModel
from .graph_runtime_assembly import GRAPH_STORE_OPENED, JOURNAL_OPENED

__all__ = [
    "CUTOVER_COMMANDS",
    "EXIT_BLOCKED",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "FINDING_ADMISSION_FAILED",
    "FINDING_ADMISSION_KEPT",
    "FINDING_CONTROL_ROOT_REJECTED",
    "FINDING_GENERATION_GUARD",
    "FINDING_GENERATION_MEMBER_REJECTED",
    "FINDING_GENERATION_MOVED",
    "FINDING_GENERATION_NOT_INTACT",
    "FINDING_GENERATION_OUTSIDE_CONTROL_ROOT",
    "FINDING_GENERATION_ROOT_REJECTED",
    "FINDING_GRAPH_NOT_READABLE",
    "FINDING_INPUT_AMBIGUOUS",
    "FINDING_INPUT_MISSING",
    "FINDING_JOURNAL_NOT_READABLE",
    "FINDING_MANIFEST_PATH_REJECTED",
    "FINDING_POINTER_ABSENT",
    "FINDING_POINTER_ATTESTATION",
    "FINDING_POINTER_CONFIRMATION_DRIFT",
    "FINDING_POINTER_LOCK",
    "FINDING_POINTER_MANIFEST_CHANGED",
    "FINDING_POINTER_NOT_A_FILE",
    "FINDING_POINTER_SYMLINK",
    "FINDING_POINTER_TARGET_REJECTED",
    "FINDING_POINTER_UNREADABLE",
    "FINDING_POINTER_VERSION",
    "FINDING_POINTER_WRITE_FAILED",
    "FINDING_PROJECTION_DRIFTED",
    "FINDING_RETAINED_ADMISSION",
    "FINDING_SUPERSEDED_POINTER_INVALID",
    "INTEGRITY_ATTESTED",
    "INTEGRITY_UNPROVED",
    "INTEGRITY_VERIFIED",
    "LIFECYCLES",
    "LIFECYCLE_ACTIVATED",
    "LIFECYCLE_PUBLISHED",
    "POINTER_LOCK_SUFFIX",
    "POINTER_TOOL",
    "POINTER_VERSION",
    "REPORT_VERSION",
    "STATE_ABSENT",
    "STATE_ACTIVE",
    "STATE_INVALID",
    "STATE_ROLLBACK",
    "STATUS_CUTOVER_APPLIED",
    "STATUS_CUTOVER_PLANNED",
    "STATUS_CUTOVER_REFUSED",
    "STATUS_CUTOVER_UNCHANGED",
    "STATUS_CUTOVER_UNVERIFIED",
    "STATUS_POINTER_WRITE_FAILED",
    "STATUS_RESTORE_APPLIED",
    "STATUS_RESTORE_NOTHING",
    "STATUS_RESTORE_PLANNED",
    "STATUS_RESTORE_REFUSED",
    "STATUS_RUNTIME_ABSENT",
    "STATUS_RUNTIME_ACTIVE",
    "STATUS_RUNTIME_INVALID",
    "STATUS_RUNTIME_ROLLBACK",
    "TOOL_NAME",
    "SERVING_STATUSES",
    "ActivePointer",
    "GenerationResolution",
    "GenerationRuntime",
    "GenerationRuntimeConfig",
    "activate_generation",
    "generation_is_served",
    "add_cutover_verbs",
    "cutover_command",
    "exit_code",
    "main",
    "pointer_lock_path",
    "read_active_pointer",
    "resolve_generation_runtime",
    "restore_generation",
    "status_generation",
]

TOOL_NAME = "layered_memory_mcp.graph_generation_runtime"

#: The report layout version. A consumer branches on this rather than on the
#: presence of a key. Deliberately this module's own number: the report shape is
#: the migration operator's, the contents are not.
REPORT_VERSION = 1

#: The pointer layout version. Recorded, checked, never migrated: a pointer this
#: process does not understand is a refusal, because guessing at a control-plane
#: file is how a runtime ends up serving the wrong generation.
POINTER_VERSION = 1

#: The tool that normally writes a pointer. A different value is a warning, not
#: a refusal — the format above is the contract, and an operator may write one
#: by hand.
POINTER_TOOL = TOOL_NAME

#: The verbs this module owns, in the order they are offered. The first is the
#: default, which is why the default is read-only.
CUTOVER_COMMANDS: tuple[str, ...] = ("status", "activate", "restore")

POINTER_LOCK_SUFFIX = ".lock"


# ---------------------------------------------------------------------------
# The state contract — see "THE STATE CONTRACT" above
# ---------------------------------------------------------------------------

#: A generation nobody has served: immutable by contract, so it is proved by
#: recomputing everything its manifest claims.
LIFECYCLE_PUBLISHED = "published"

#: A generation a pointer attests, and which has been writable ever since: its
#: publication hashes no longer describe it, so it is proved by the attestation,
#: the structural guards and read-only health.
LIFECYCLE_ACTIVATED = "activated"

#: Both, in the order a generation passes through them. Never a caller's
#: argument: the lifecycle follows from which explicit input was supplied.
LIFECYCLES: tuple[str, ...] = (LIFECYCLE_PUBLISHED, LIFECYCLE_ACTIVATED)

#: The full publication proof ran and passed. The only value that is allowed to
#: accompany ``generation_intact=True``.
INTEGRITY_VERIFIED = "verified_publication"

#: The pointer's attestation held, every structural guard passed, and both
#: stores are readable and consistent with the journal — but the publication
#: hashes were deliberately *not* recomputed, because a served generation has
#: legitimately moved on from them.
INTEGRITY_ATTESTED = "attested_activation"

#: Nothing was established. The value every refusal carries, so a report can
#: never be read as a proof by the absence of a finding.
INTEGRITY_UNPROVED = "unproved"


# ---------------------------------------------------------------------------
# States — what a resolution concluded about the generation it was pointed at
# ---------------------------------------------------------------------------

#: A proved generation, in its published position.
STATE_ACTIVE = "active"

#: A proved generation that is a *retained withdrawn* one — it lives under a
#: ``rollback/`` directory. Usable, and deliberately not reported as ``active``:
#: an operator running on a restored rollback generation must be able to see
#: that from the status alone.
STATE_ROLLBACK = "rollback"

#: Nothing is pointed at: the pointer file does not exist. The expected state
#: before the first cutover, and not a fault.
STATE_ABSENT = "absent"

#: The inputs, the pointer or the generation did not survive validation. No
#: config is produced; this is the fail-closed answer.
STATE_INVALID = "invalid"


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

STATUS_RUNTIME_ACTIVE = "generation_runtime_active"
STATUS_RUNTIME_ROLLBACK = "generation_runtime_rollback_active"
STATUS_RUNTIME_ABSENT = "generation_runtime_pointer_absent"
STATUS_RUNTIME_INVALID = "generation_runtime_invalid"

STATUS_CUTOVER_PLANNED = "generation_cutover_planned"
STATUS_CUTOVER_APPLIED = "generation_cutover_applied"
STATUS_CUTOVER_UNCHANGED = "generation_cutover_unchanged"
STATUS_CUTOVER_REFUSED = "generation_cutover_refused"

#: The pointer was written — by a cutover or by a restore — and then did not
#: resolve. Should be unreachable; it exists because "we wrote it" and "it
#: works" are different claims, and neither operation may report the second by
#: having done the first.
STATUS_CUTOVER_UNVERIFIED = "generation_cutover_unverified"

STATUS_POINTER_WRITE_FAILED = "generation_pointer_write_failed"

STATUS_RESTORE_PLANNED = "generation_restore_planned"
STATUS_RESTORE_APPLIED = "generation_restore_applied"
STATUS_RESTORE_NOTHING = "generation_restore_nothing_retained"
STATUS_RESTORE_REFUSED = "generation_restore_refused"

#: The statuses that are not a refusal. Everything else exits non-zero —
#: including "nothing is active", because an operator asking *which* generation
#: is active needs a non-zero answer when none is.
_SUCCESS_STATUSES = frozenset(
    {
        STATUS_RUNTIME_ACTIVE,
        STATUS_RUNTIME_ROLLBACK,
        STATUS_CUTOVER_PLANNED,
        STATUS_CUTOVER_APPLIED,
        STATUS_CUTOVER_UNCHANGED,
        STATUS_RESTORE_PLANNED,
        STATUS_RESTORE_APPLIED,
        STATUS_RESTORE_NOTHING,
    }
)


# ---------------------------------------------------------------------------
# Findings — bounded refusal tokens
# ---------------------------------------------------------------------------

FINDING_INPUT_AMBIGUOUS = "runtime_input_ambiguous"
FINDING_INPUT_MISSING = "runtime_input_missing"
FINDING_POINTER_ABSENT = "pointer_absent"
FINDING_POINTER_NOT_A_FILE = "pointer_not_a_file"
FINDING_POINTER_SYMLINK = "pointer_symlink"
FINDING_POINTER_UNREADABLE = "pointer_unreadable"
FINDING_POINTER_VERSION = "pointer_version_unsupported"
FINDING_POINTER_TARGET_REJECTED = "pointer_target_rejected"
FINDING_POINTER_MANIFEST_CHANGED = "pointer_manifest_digest_changed"
FINDING_POINTER_WRITE_FAILED = "pointer_write_failed"
FINDING_CONTROL_ROOT_REJECTED = "control_root_rejected"
FINDING_GENERATION_ROOT_REJECTED = "generation_root_rejected"
FINDING_GENERATION_OUTSIDE_CONTROL_ROOT = "generation_outside_control_root"
FINDING_GENERATION_NOT_INTACT = "generation_not_intact"
FINDING_GRAPH_NOT_READABLE = "generation_graph_not_readable"
FINDING_JOURNAL_NOT_READABLE = "generation_journal_not_readable"

#: A pointer that records no manifest digest, and therefore no activation proof.
#: Refused rather than treated as "not checked": the digest *is* the attestation
#: that makes the activated lifecycle safe, and a control file without one would
#: be the bypass the state contract exists to remove.
FINDING_POINTER_ATTESTATION = "pointer_attestation_missing"

#: One of the generation's own members is a symlink, or is not the kind of file
#: it has to be. Suffixed with the member and the reason
#: (``generation_member_rejected:graph.db:symlink``), and reported *before* the
#: manifest is read or any database is opened.
FINDING_GENERATION_MEMBER_REJECTED = "generation_member_rejected"

#: A manifest-declared path that is absolute, escapes the generation or
#: traverses a symlink. Checked after the manifest is parsed and before anything
#: it names is opened, so a forged manifest cannot steer a read.
FINDING_MANIFEST_PATH_REJECTED = "manifest_path_rejected"

#: The stored graph no longer matches the journal it was projected from. Part of
#: *consistency*, which is the activated lifecycle's substitute for a replay
#: proof: the publication hashes may have moved on, but the authority and its
#: projection must still agree.
FINDING_PROJECTION_DRIFTED = "generation_projection_drifted"

#: The pointer resolved after the write, but not to the generation that was
#: asked for. Suffixed with what disagreed (``:generation``, ``:target``,
#: ``:attestation``). A refusal rather than a success with a surprising
#: ``resolution``: "a usable generation is active" and "the generation this
#: report names is active" are different claims, and only the second is what an
#: operator asked for. Reached when the pointer moved between the replace and
#: the confirmation, or when the written payload was not the payload resolved.
FINDING_POINTER_CONFIRMATION_DRIFT = "pointer_confirmation_drift"

#: The pointer's interval could not be taken, so nothing was read, decided or
#: written. Suffixed with the lock's own bounded label (``busy``,
#: ``busy_thread``, ``reentrant``, ``control_lock_path_rejected:…``). A refusal
#: rather than a run without the interval: an unsynchronised cutover is the
#: defect this lock exists to remove, and taking it is not optional.
FINDING_POINTER_LOCK = "pointer_interval_unavailable"

#: The generation guard — the lock a withdrawal takes in the same destination —
#: could not be taken, so this generation's admission was never recorded and no
#: pointer was written. Same reasoning: an unrecorded admission is a generation
#: a legacy withdrawal may still move.
FINDING_GENERATION_GUARD = "generation_guard_unavailable"

#: The admission record could not be written, named by its exception class. A
#: refusal before the pointer, never after it.
FINDING_ADMISSION_FAILED = "generation_admission_failed"

#: The generation the new pointer would *retain* could not be put out of a
#: legacy withdrawal's reach, so the pointer was not written at all. Carried
#: together with the underlying reason — an unreadable ledger, an unobtainable
#: guard, a failed record, a moved tree, a target outside the control root or a
#: retained digest the pointer never recorded.
#:
#: A refusal rather than an omission, and that is the whole point: a published
#: pointer names two generations and a runtime has been given both, so a cutover
#: that published while leaving the retained half unprotected would turn a
#: reversible move into a one-way one and report success. Dropping the retained
#: target instead would be worse — it would discard the operator's only route
#: back to the generation they were serving. So nothing is written, and the
#: previous pointer stays exactly as it was.
FINDING_RETAINED_ADMISSION = "retained_generation_admission_refused"

#: An admission record this operation *did* write before refusing for another
#: reason, suffixed with the half it belongs to (``previous``). Reported, not
#: reversed: a record says "a runtime was given this generation", which stayed
#: true when the publication afterwards failed — the operator had been handed
#: both generations before this tool was asked to point at them. Deleting it to
#: make the refusal look side-effect-free would re-expose exactly the generation
#: the record protects, so the refusal names it instead and the operator learns
#: that one future withdrawal in that destination will refuse until they decide
#: otherwise. The cost of keeping it is a refused rollback; the cost of removing
#: it is a withdrawn generation someone is serving.
FINDING_ADMISSION_KEPT = "generation_admission_kept"

#: The generation was moved or replaced between its proof and its admission —
#: a concurrent withdrawal or a republication. Suffixed with ``absent`` or
#: ``digest``. The pointer is not written, so the operator keeps a working
#: control plane and a refusal rather than a pointer into thin air.
FINDING_GENERATION_MOVED = "generation_moved_before_admission"

#: An existing pointer file that could not be parsed while it was about to be
#: superseded. A finding, not a warning: an activation that overwrote it would
#: destroy the only record of what the system thought it was serving, so the
#: operator is left holding both the corrupt file and the refusal.
FINDING_SUPERSEDED_POINTER_INVALID = "superseded_pointer_invalid"

#: Warning label for a pointer key this version does not know. Ignored rather
#: than refused, so a newer writer's extra field does not strand an older
#: reader on a generation it can otherwise prove.
WARNING_POINTER_UNKNOWN_KEY = "pointer_unknown_key"

#: Warning label for a pointer whose ``tool`` is not this one.
WARNING_POINTER_FOREIGN_TOOL = "pointer_foreign_tool"


# ---------------------------------------------------------------------------
# The runtime configuration: one generation, explicitly
# ---------------------------------------------------------------------------

#: Mode → predicate tables. The *vocabulary* lives in
#: :data:`~layered_memory_mcp.config.GRAPH_MODES`, which is imported rather than
#: restated; these two tuples mirror the private tables
#: :class:`~layered_memory_mcp.config.MemoryConfig` derives its own predicates
#: from, and ``test_runtime_config_mode_predicates_agree_with_memory_config``
#: pins the two against each other so a drifted predicate cannot land quietly.
_RECALL_MODES = ("graph_recall", "graph")
_WRITE_MODES = ("graph_write", "graph")

#: The one mode that means "nothing graph-shaped is switched on", mirroring
#: ``config._GRAPH_DISABLED_MODE``. Compared against the *contract* rather than
#: against :data:`~layered_memory_mcp.config.DEFAULT_GRAPH_MODE`, so moving the
#: rollout default cannot silently invert this predicate.
_DISABLED_MODE = "legacy"


@dataclass(frozen=True)
class GenerationRuntimeConfig(GenerationConfig):
    """A runtime configuration scoped to one generation root, and nothing else.

    The duck-typed object the framework already knows how to read. It *is* a
    :class:`~layered_memory_mcp.graph_migration_generation.GenerationConfig` —
    the same resolution-free, root-scoped paths the migration drives the
    projection routines with — extended with the two things a *running* system
    additionally needs:

    * the graph integration predicates
      (:meth:`graph_recall_enabled`, :meth:`graph_write_enabled`, …) that
      :meth:`~layered_memory_mcp.graph_runtime.GraphModeDecision.from_config`
      and every mode-gated entrance read. ``graph_mode`` defaults to ``graph``
      because an explicitly resolved generation is the one case where the graph
      *is* the system; the legacy default of
      :class:`~layered_memory_mcp.config.MemoryConfig` is untouched, and this
      object is never constructed implicitly;
    * ``data_dir`` / ``vector_db_path``, which name the generation's own vector
      database. The vector projection reads ``<home>/data``, and ``home`` here
      is the generation root, so republishing a domain inside an activated
      generation cannot reach a legacy store.

    Every path is derived from :attr:`root`. No ``config.yaml``, environment
    variable or home directory participates, which is the property that makes an
    activated generation the *only* thing a write can land in.

    ``memory_changelog_enabled`` stays ``False``, as it is for a migration: the
    changelog a generation carries is preserved historical evidence, and a
    runtime appending to it would be writing history it did not witness.
    """

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.graph_mode not in GRAPH_MODES:
            raise ValueError(
                f"Invalid graph_mode: {self.graph_mode!r}. Must be one of "
                f"{', '.join(repr(mode) for mode in GRAPH_MODES)}."
            )

    @property
    def data_dir(self) -> Path:
        """The generation's data directory — the vector projection's root."""
        return self.root / DATA_DIR

    @property
    def vector_db_path(self) -> Path:
        """The generation's own vector database."""
        return self.root / VECTOR_RELATIVE

    # -- the graph integration predicates ------------------------------------

    @property
    def graph_enabled(self) -> bool:
        return self.graph_mode != _DISABLED_MODE

    @property
    def graph_recall_enabled(self) -> bool:
        return self.graph_mode in _RECALL_MODES

    @property
    def graph_write_enabled(self) -> bool:
        return self.graph_mode in _WRITE_MODES

    @property
    def graph_shadow_enabled(self) -> bool:
        return self.graph_mode == "shadow"


# ---------------------------------------------------------------------------
# The pointer
# ---------------------------------------------------------------------------

class ActivePointer(GraphModel):
    """The in-memory form of one active pointer. ``to_dict()`` is the file body.

    Flat on purpose: a control-plane file an operator may have to read, edit or
    diff under pressure is easier to trust when every field is one line.
    """

    pointer_version: int = Field(default=POINTER_VERSION, ge=0)
    tool: str = POINTER_TOOL
    generation: str = ""
    activated_at: str = ""
    manifest_sha256: str = ""
    previous_generation: str = ""
    previous_activated_at: str = ""
    previous_manifest_sha256: str = ""


_POINTER_KEYS = frozenset(ActivePointer.model_fields)


#: The reasons a target is not inside the root it must be inside, as bounded
#: labels. ``ancestor_symlink`` is a *separate* answer from ``symlink`` on
#: purpose: "the thing you named is a link" and "the path you reached it through
#: contains one" are different operator mistakes with different fixes.
_REJECTION_ESCAPE = "escape"
_REJECTION_SYMLINK = "symlink"
_REJECTION_ANCESTOR = "ancestor_symlink"
_REJECTION_MALFORMED = "malformed"


def _relative_target(control_root: Path, target: Path) -> tuple[str | None, str]:
    """``(control-root-relative POSIX path, refusal label)`` for one target.

    The containment rule the whole control plane rests on, and the same
    discipline
    :func:`~layered_memory_mcp.graph_migration_generation.verify_generation`
    applies to manifest-declared paths — restated here because the root is
    different (the pointer's own parent) and so is the trust source (a path an
    operator supplied, or a file an operator or an attacker may have edited).

    Four refusals: a target that is not lexically inside the control root, a
    ``..``/``.`` component, a component that *is* a symlink (so a link planted
    in the control root cannot stand in for a generation), and a resolved path
    that still leaves the resolved control root. The target must be strictly
    inside: the control root is where the pointer lives, and a pointer inside
    its own generation would be part of what it describes.

    The one walker both entrances use, so the pointer-declared path and the
    operator-supplied path are judged by exactly the same rule.
    """
    try:
        relative = target.relative_to(control_root)
    except ValueError:
        return None, _REJECTION_ESCAPE
    parts = relative.parts
    if not parts or any(part in ("..", ".") for part in parts):
        return None, _REJECTION_ESCAPE
    walked = control_root
    for part in parts:
        walked = walked / part
        if walked.is_symlink():
            return None, _REJECTION_SYMLINK
    try:
        resolved_root = control_root.resolve()
        resolved_target = target.resolve()
    except OSError:  # pragma: no cover - a path we cannot resolve is refused
        return None, _REJECTION_ESCAPE
    if resolved_root not in resolved_target.parents:
        return None, _REJECTION_ESCAPE
    return relative.as_posix(), ""


def _declared_target(root: Path, declared: Any) -> tuple[Path | None, str]:
    """One untrusted relative path as ``(absolute path, refusal label)``.

    The declared value is untrusted input — it comes out of a control file or a
    manifest, either of which an operator or an attacker may have edited. It is
    checked for *shape* before it is joined (no absolute path, no backslash, no
    ``..``) and for *containment* after, so neither a forged string nor a
    planted symlink can make a read happen outside ``root``.

    The one checker for every declared path this module honours: a pointer's
    target, and every path a manifest names.
    """
    if not isinstance(declared, str) or not declared.strip() or "\\" in declared:
        return None, _REJECTION_MALFORMED
    pure = PurePosixPath(declared)
    if pure.is_absolute() or any(part in ("..", ".") for part in pure.parts):
        return None, _REJECTION_ESCAPE
    candidate = root.joinpath(*pure.parts)
    relative, rejection = _relative_target(root, candidate)
    if relative is None:
        return None, rejection
    return candidate, ""


def _pointer_target(control_root: Path, declared: Any) -> tuple[Path | None, str]:
    """The pointer's declared target as ``(absolute path, finding)``."""
    target, rejection = _declared_target(control_root, declared)
    if target is None:
        return None, f"{FINDING_POINTER_TARGET_REJECTED}:{rejection}"
    return target, ""


def _ancestor_symlink(path: Path) -> bool:
    """Whether the path *above* ``path`` traverses a symlink. Follows nothing.

    The guard the containment walk cannot give: that walk starts at a root and
    checks every component below it, which says nothing about how the root
    itself was reached. An operator-supplied generation root has no such root to
    start from, so an ancestor that is a link would silently make every
    "inside this generation" conclusion a statement about somebody else's tree.

    Implemented as a single comparison rather than a component walk: the path is
    already lexically normalised by :func:`_resolved`, so ``realpath`` differing
    from it can only mean that some component above it is a link. The final
    component is excluded deliberately — a symlinked root or pointer is a more
    specific refusal (``:symlink``) and must not be reported as this one.
    """
    parent = path.parent
    try:
        return os.path.realpath(parent) != str(parent)
    except OSError:  # pragma: no cover - a path we cannot examine is refused
        return True


#: The generation members this module hands to the framework, and the kind each
#: has to be. Guarded *before* the manifest is read and before any database is
#: opened, because these are the exact paths a resolved config exposes: a
#: symlinked ``knowledge/`` or ``data/`` would let an activated generation
#: publish a readable surface or a vector row outside itself, and a symlinked
#: ``graph.db`` would open somebody else's database read-write on the next
#: commit. ``file``/``dir`` are required when the member exists; a member that is
#: absent is left to the manifest and the health checks to report, which is how
#: a never-published directory is refused by name rather than by type.
_REQUIRED_FILES = (MANIFEST_NAME, MANIFEST_DIGEST_NAME)


def _generation_members(config: GenerationRuntimeConfig) -> tuple[tuple[str, Path, str], ...]:
    """``(label, path, kind)`` for every member the resolved config exposes.

    Derived from the config's own properties rather than from the generation
    layout constants, so what is guarded is literally what a caller will be
    handed: adding a path to the config without guarding it is not possible
    without this tuple noticing.
    """
    root = config.root
    return (
        (MANIFEST_NAME, root / MANIFEST_NAME, "file"),
        (MANIFEST_DIGEST_NAME, root / MANIFEST_DIGEST_NAME, "file"),
        ("graph.db", config.graph_db_path, "file"),
        ("journal.db", config.graph_journal_path, "file"),
        ("knowledge", config.knowledge_dir, "dir"),
        ("L0.md", config.l0_index_file, "file"),
        (DATA_DIR, config.data_dir, "dir"),
        (VECTOR_RELATIVE, config.vector_db_path, "file"),
    )


def _structural_findings(config: GenerationRuntimeConfig) -> list[str]:
    """Why this generation may not be read at all, if it may not.

    Runs before :func:`read_manifest` and before any store is opened, and reads
    no content: every check is a ``lstat``. A member that is a symlink is
    refused by name rather than followed, and one that exists as the wrong kind
    of thing is refused rather than opened — ``sqlite3`` would happily be handed
    a directory and report a backend error from deep inside a session, which is
    not where a path decision belongs.
    """
    findings: list[str] = []
    for label, path, kind in _generation_members(config):
        if path.is_symlink():
            findings.append(
                f"{FINDING_GENERATION_MEMBER_REJECTED}:{label}:{_REJECTION_SYMLINK}"
            )
            continue
        if not path.exists():
            if label in _REQUIRED_FILES:
                findings.append(f"{FINDING_GENERATION_MEMBER_REJECTED}:{label}:absent")
            continue
        if kind == "file" and not path.is_file():
            findings.append(f"{FINDING_GENERATION_MEMBER_REJECTED}:{label}:not_a_file")
        elif kind == "dir" and not path.is_dir():
            findings.append(
                f"{FINDING_GENERATION_MEMBER_REJECTED}:{label}:not_a_directory"
            )
    return findings


def _manifest_path_findings(root: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Every manifest-declared path that may not be honoured, by name.

    A manifest is untrusted input in both lifecycles: it names the files a proof
    will stat, hash and read, and the *activated* lifecycle does not run that
    proof — so if these paths were only checked inside
    :func:`verify_generation`, a served generation's manifest would never be
    checked at all. Both path-keyed sections are covered: ``artifacts``,
    relative to the generation, and ``source_files``, relative to the frozen
    ``backup/`` subtree.
    """
    findings: list[str] = []
    for section, base in (
        ("artifacts", root),
        ("source_files", root / BACKUP_DIR),
    ):
        declared = manifest.get(section)
        if declared is None:
            continue
        if not isinstance(declared, Mapping):
            findings.append(f"{FINDING_MANIFEST_PATH_REJECTED}:{section}:not_a_mapping")
            continue
        for relative in sorted(declared, key=str):
            target, rejection = _declared_target(base, relative)
            if target is None:
                findings.append(
                    f"{FINDING_MANIFEST_PATH_REJECTED}:{section}:{rejection}"
                )
    return findings


def read_active_pointer(
    pointer: Path,
) -> tuple[ActivePointer | None, list[str], list[str]]:
    """``(pointer, findings, warnings)`` for one pointer file. Reads only.

    Nothing is created, repaired or rewritten — an absent pointer stays absent,
    which is what makes a status check safe to run on a system that has never
    cut over. A symlinked pointer is refused: the pointer is the one file this
    tool *writes*, and a link would move that write somewhere the control root
    does not cover.
    """
    findings: list[str] = []
    warnings: list[str] = []
    if pointer.is_symlink():
        return None, [FINDING_POINTER_SYMLINK], warnings
    if not pointer.exists():
        return None, [FINDING_POINTER_ABSENT], warnings
    if not pointer.is_file():
        return None, [FINDING_POINTER_NOT_A_FILE], warnings
    try:
        body = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"{FINDING_POINTER_UNREADABLE}:{type(exc).__name__}"], warnings
    if not isinstance(body, dict):
        return None, [f"{FINDING_POINTER_UNREADABLE}:not_a_mapping"], warnings

    version = body.get("pointer_version")
    if version != POINTER_VERSION:
        return None, [f"{FINDING_POINTER_VERSION}:{version}"], warnings

    for key in sorted(set(body) - _POINTER_KEYS):
        warnings.append(f"{WARNING_POINTER_UNKNOWN_KEY}:{str(key)[:40]}")
    tool = body.get("tool")
    if tool != POINTER_TOOL:
        warnings.append(WARNING_POINTER_FOREIGN_TOOL)

    try:
        parsed = ActivePointer(
            **{key: body[key] for key in _POINTER_KEYS if key in body}
        )
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return None, [f"{FINDING_POINTER_UNREADABLE}:{type(exc).__name__}"], warnings
    if not parsed.generation.strip():
        findings.append(f"{FINDING_POINTER_TARGET_REJECTED}:malformed")
    return (parsed if not findings else None), findings, warnings


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class GenerationResolution(GraphModel):
    """What one resolution concluded, as bounded, serializable data.

    ``state`` and ``generation_intact`` are separate claims on purpose, exactly
    as they are in
    :class:`~layered_memory_mcp.graph_migration_generation.VerificationReport`:
    "nothing is pointed at", "the pointer is forged", "the generation changed
    underneath us" and "this is a retained rollback generation" are four
    different operator situations and must not share one boolean.
    """

    state: str = STATE_INVALID
    status: str = STATUS_RUNTIME_INVALID
    lifecycle: str = LIFECYCLE_PUBLISHED
    integrity: str = INTEGRITY_UNPROVED
    pointer: str = ""
    pointer_present: bool = False
    generation: str = ""
    control_root: str = ""
    relative_generation: str = ""
    previous_generation: str = ""
    manifest_version: int = Field(default=0, ge=0)
    manifest_sha256: str = ""
    attested: bool = False
    generation_intact: bool = False
    read_only_health: bool = False
    projection_consistent: bool = False
    graph_status: str = ""
    journal_status: str = ""
    node_count: int = Field(default=0, ge=0)
    event_count: int = Field(default=0, ge=0)
    findings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether a caller may actually serve this generation.

        Three independent claims, with no inference between them: a proved
        state, an *established* integrity — the full publication proof passed,
        or the pointer's attestation held for a generation that has been
        writable since — and both persistent stores readable and consistent
        with the journal.

        ``generation_intact`` is deliberately not one of them. A served
        generation reports it ``False`` and is still usable, because the
        publication hashes stopped describing it the moment the framework wrote
        into it. :attr:`integrity` is the claim to read.
        """
        return (
            self.state in (STATE_ACTIVE, STATE_ROLLBACK)
            and self.integrity in (INTEGRITY_VERIFIED, INTEGRITY_ATTESTED)
            and self.read_only_health
            and self.projection_consistent
        )

    def to_payload(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["usable"] = self.usable
        return payload


class GenerationRuntime:
    """A resolved generation: its report, and its config when it is usable.

    Deliberately a plain object rather than a model, for the same reason
    :class:`~layered_memory_mcp.graph_runtime_assembly.GraphRuntimeAssembly` is:
    it carries a configuration object, not data to be serialized beside a memory
    answer. :attr:`report` is the serializable half.

    It holds no open handle and therefore has nothing to close: the adapter's
    whole output is a *config*, and callers open the stores they need through
    the existing read-only or writable entrances.
    """

    __slots__ = ("_config", "_report", "_verification")

    def __init__(
        self,
        config: GenerationRuntimeConfig | None,
        report: GenerationResolution,
        verification: VerificationReport | None = None,
    ) -> None:
        self._config = config
        self._report = report
        self._verification = verification

    @property
    def config(self) -> GenerationRuntimeConfig | None:
        """The generation-scoped config, or ``None`` — a supported answer."""
        return self._config

    @property
    def report(self) -> GenerationResolution:
        return self._report

    @property
    def verification(self) -> VerificationReport | None:
        """The proof that was run, when one was. Reads only, never cached."""
        return self._verification

    @property
    def usable(self) -> bool:
        return self._config is not None and self._report.usable


def _resolved(path: Any) -> Path:
    """An operator-supplied path, expanded, absolute and lexically normalised.

    Nothing is created and **no symlink is followed**: the normalisation is
    :func:`os.path.normpath`, which collapses ``..`` and ``.`` textually. That
    is the point — it leaves the path denoting exactly what the operator typed
    while making the one comparison :func:`_ancestor_symlink` rests on
    meaningful, whereas ``Path.resolve()`` would quietly rewrite a path *through*
    the links this module has to refuse.
    """
    expanded = os.path.expanduser(os.fspath(path))
    return Path(os.path.normpath(os.path.abspath(expanded)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _invalid(report: GenerationResolution) -> GenerationRuntime:
    return GenerationRuntime(None, report)


def resolve_generation_runtime(
    *, generation: Any = None, pointer: Any = None, graph_mode: str = "graph"
) -> GenerationRuntime:
    """Resolve the generation one explicit input names, and prove it.

    Exactly one of ``generation`` and ``pointer`` must be supplied. Both at once
    is :data:`FINDING_INPUT_AMBIGUOUS` and neither is
    :data:`FINDING_INPUT_MISSING`, rather than a precedence rule: a runtime that
    silently preferred one input is a runtime whose operator cannot say which
    generation it will serve.

    **The input chooses the lifecycle, and there is no third option.** An
    explicit ``generation`` root is a :data:`LIFECYCLE_PUBLISHED` resolve and is
    always proved in full; a ``pointer`` is a :data:`LIFECYCLE_ACTIVATED` resolve
    and is proved against the attestation that pointer carries. See
    "THE STATE CONTRACT" in the module docstring for why these are two proofs
    and not one proof with a switch.

    What runs, in order, and every step of it is a refusal point:

    1. the pointer, if that is the input: a real non-symlinked file, a version
       this process speaks, and a target strictly inside the pointer's own
       parent reached without a symlink;
    2. the generation root as a path: not a symlink, not reached *through* one,
       present, a directory;
    3. every member the resolved config exposes — ``manifest.json`` and its
       digest sidecar, ``graph.db``, ``journal.db``, ``knowledge/``, ``L0.md``,
       ``data/``, ``data/vectors.db`` — none a symlink, none the wrong kind of
       file. Still nothing has been read;
    4. the manifest, through
       :func:`~layered_memory_mcp.graph_migration_generation.read_manifest`
       (which checks the digest sidecar), and then every path it declares, for
       containment — so a forged manifest cannot steer a later read;
    5. the attestation: in the activated lifecycle the pointer's recorded digest
       must exist and must still equal this manifest's digest;
    6. in the published lifecycle only, the full
       :func:`~layered_memory_mcp.graph_migration_generation.verify_generation`:
       membership, every artifact hash, the journal, a replay of it against the
       stored graph, the readable surfaces and the vector rows;
    7. read-only health and journal↔graph consistency, through
       :func:`~layered_memory_mcp.graph_application.runtime_status` — which
       opens both persistent stores through their inspection entrances and
       creates no database, no schema and no checkpoint row — driven by *this
       module's own* config, so what is proved readable is what a caller will
       actually be handed.

    Args:
        generation: An explicit generation root, proved as a publication.
            Mutually exclusive with ``pointer``.
        pointer: An explicit active-pointer file, proved as an activation.
            Mutually exclusive with ``generation``.
        graph_mode: The integration mode the produced config declares. Defaults
            to ``graph``; a caller rolling out may name a narrower one.

    Returns:
        A :class:`GenerationRuntime`. Nothing raises for a refusable condition:
        an unusable generation is a reported state, because a runtime check that
        raises is one an operator cannot run when something is already wrong.
    """
    if (generation is None) == (pointer is None):
        finding = (
            FINDING_INPUT_AMBIGUOUS if generation is not None else FINDING_INPUT_MISSING
        )
        return _invalid(
            GenerationResolution(
                generation="" if generation is None else str(_resolved(generation)),
                pointer="" if pointer is None else str(_resolved(pointer)),
                findings=[finding],
            )
        )

    if pointer is not None:
        return _resolve_from_pointer(_resolved(pointer), graph_mode=graph_mode)
    return _resolve_root(
        _resolved(generation),
        lifecycle=LIFECYCLE_PUBLISHED,
        control_root=None,
        declared=None,
        graph_mode=graph_mode,
    )


def _resolve_from_pointer(pointer: Path, *, graph_mode: str) -> GenerationRuntime:
    """Resolve what one pointer attests. Always the activated lifecycle."""
    control_root = pointer.parent

    def report(**fields: Any) -> GenerationResolution:
        return GenerationResolution(
            lifecycle=LIFECYCLE_ACTIVATED,
            pointer=str(pointer),
            control_root=str(control_root),
            **fields,
        )

    if _ancestor_symlink(pointer):
        # The pointer's own parent *is* the control root, so a symlinked
        # ancestor would make every containment conclusion below meaningless.
        # Refused before the file is read.
        return _invalid(
            report(
                pointer_present=False,
                findings=[f"{FINDING_CONTROL_ROOT_REJECTED}:{_REJECTION_ANCESTOR}"],
            )
        )

    parsed, findings, warnings = read_active_pointer(pointer)
    if parsed is None:
        absent = findings == [FINDING_POINTER_ABSENT]
        return _invalid(
            report(
                state=STATE_ABSENT if absent else STATE_INVALID,
                status=STATUS_RUNTIME_ABSENT if absent else STATUS_RUNTIME_INVALID,
                pointer_present=not absent,
                findings=sorted(set(findings)),
                warnings=sorted(set(warnings)),
            )
        )

    target, rejection = _pointer_target(control_root, parsed.generation)
    if target is None:
        return _invalid(
            report(
                pointer_present=True,
                relative_generation=parsed.generation,
                findings=[rejection],
                warnings=sorted(set(warnings)),
            )
        )

    return _resolve_root(
        target,
        lifecycle=LIFECYCLE_ACTIVATED,
        control_root=control_root,
        declared=parsed,
        graph_mode=graph_mode,
        pointer=pointer,
        warnings=warnings,
    )


def _resolve_root(
    root: Path,
    *,
    lifecycle: str,
    control_root: Path | None,
    declared: ActivePointer | None,
    graph_mode: str,
    pointer: Path | None = None,
    warnings: Sequence[str] = (),
) -> GenerationRuntime:
    """Prove one generation root in one lifecycle, and build its config.

    Writes nothing, and reads nothing until every path decision has been made:
    the guards in steps 2 and 3 of :func:`resolve_generation_runtime` run before
    :func:`read_manifest`, and the manifest's own declared paths are checked
    before :func:`runtime_status` opens anything.
    """
    notes = list(warnings)

    def report(**fields: Any) -> GenerationResolution:
        return GenerationResolution(
            lifecycle=lifecycle,
            pointer="" if pointer is None else str(pointer),
            pointer_present=pointer is not None,
            generation=str(root),
            control_root="" if control_root is None else str(control_root),
            relative_generation="" if declared is None else declared.generation,
            previous_generation=(
                "" if declared is None else declared.previous_generation
            ),
            warnings=sorted(set(notes)),
            **fields,
        )

    # 2. The root as a path. Refused by name rather than followed: a symlinked
    #    root — or one reached through a symlinked ancestor — makes "inside this
    #    generation" a statement about the link.
    root_rejection = ""
    if root.is_symlink():
        root_rejection = _REJECTION_SYMLINK
    elif _ancestor_symlink(root):
        root_rejection = _REJECTION_ANCESTOR
    elif not root.exists():
        root_rejection = "absent"
    elif not root.is_dir():
        root_rejection = "not_a_directory"
    if root_rejection:
        return _invalid(
            report(findings=[f"{FINDING_GENERATION_ROOT_REJECTED}:{root_rejection}"])
        )

    config = GenerationRuntimeConfig(root=root, graph_mode=graph_mode)

    # 3. The members the config exposes. Nothing has been opened yet.
    structural = _structural_findings(config)
    if structural:
        return _invalid(report(findings=sorted(set(structural))))

    # 4. The manifest, then the paths it declares.
    manifest, manifest_findings = read_manifest(root)
    if manifest is None:
        return _invalid(report(findings=sorted(set(manifest_findings))))
    path_findings = _manifest_path_findings(root, manifest)
    if path_findings:
        return _invalid(
            report(findings=sorted({*manifest_findings, *path_findings}))
        )

    findings = list(manifest_findings)
    digest = manifest_digest(manifest)
    manifest_version = int(manifest.get("manifest_version") or 0)

    # 5. The attestation. In the activated lifecycle this is the whole basis for
    #    serving a mutable generation, so an absent digest is as fatal as a
    #    wrong one.
    recorded = "" if declared is None else declared.manifest_sha256.strip()
    attested = bool(recorded) and recorded == digest
    if recorded and not attested:
        findings.append(FINDING_POINTER_MANIFEST_CHANGED)
    elif not recorded and lifecycle == LIFECYCLE_ACTIVATED:
        findings.append(FINDING_POINTER_ATTESTATION)

    # 6. The publication proof — only where the publication hashes still
    #    describe the tree.
    verification = (
        verify_generation(root) if lifecycle == LIFECYCLE_PUBLISHED else None
    )
    if verification is not None and not verification.generation_intact:
        findings.append(FINDING_GENERATION_NOT_INTACT)
        findings.extend(verification.findings)
    intact = (
        verification is not None
        and verification.generation_intact
        and not manifest_findings
    )

    # 7. Read-only health and journal↔graph consistency.
    health = _health(config)
    findings.extend(health.findings)

    established = INTEGRITY_VERIFIED if intact else INTEGRITY_UNPROVED
    if lifecycle == LIFECYCLE_ACTIVATED:
        established = INTEGRITY_ATTESTED if attested else INTEGRITY_UNPROVED

    facts: dict[str, Any] = {
        "attested": attested,
        # Claimed only where the full publication proof actually ran and
        # passed. A served generation reports ``False`` here and says what it
        # *did* establish in ``integrity`` instead.
        "generation_intact": intact,
        "read_only_health": health.readable,
        "projection_consistent": health.consistent,
        "graph_status": health.graph_status,
        "journal_status": health.journal_status,
        "node_count": health.node_count,
        "event_count": health.event_count,
        "manifest_version": manifest_version,
        "manifest_sha256": digest,
    }

    if findings:
        return _invalid(
            report(
                integrity=INTEGRITY_UNPROVED,
                findings=sorted(set(findings)),
                **facts,
            )
        )

    rollback = root.parent.name == ROLLBACK_DIR
    resolution = report(
        state=STATE_ROLLBACK if rollback else STATE_ACTIVE,
        status=STATUS_RUNTIME_ROLLBACK if rollback else STATUS_RUNTIME_ACTIVE,
        integrity=established,
        **facts,
    )
    return GenerationRuntime(config, resolution, verification)


@dataclass(frozen=True)
class _Health:
    """What the read-only inspection of one generation's stores concluded."""

    graph_status: str = ""
    journal_status: str = ""
    node_count: int = 0
    event_count: int = 0
    readable: bool = False
    consistent: bool = False
    findings: tuple[str, ...] = ()


def _health(config: GenerationRuntimeConfig) -> _Health:
    """Open both stores read-only and ask whether they agree with the journal.

    Delegated to :func:`~layered_memory_mcp.graph_application.runtime_status`
    rather than reimplemented: that is the framework's own operator view, it
    opens the journal and the graph through the inspection entrances that create
    no database, schema or checkpoint row, and it already answers the question
    the activated lifecycle substitutes for a replay proof — whether the stored
    graph still matches the ledger it was projected from. Running it here means
    "healthy" means the same thing to this adapter and to every other diagnostic.

    Readability and consistency are separate claims: a graph that opens cleanly
    but has drifted from its journal is a different operator situation from one
    that cannot be opened at all.
    """
    try:
        status = runtime_status(config)
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return _Health(findings=(f"{FINDING_GRAPH_NOT_READABLE}:{type(exc).__name__}",))

    journal = status.get("journal") or {}
    graph = status.get("graph") or {}
    drift = status.get("projection_drift") or {}
    journal_status = str(journal.get("status") or "")
    graph_status = str(graph.get("status") or "")

    findings: list[str] = []
    if journal_status != JOURNAL_OPENED:
        findings.append(f"{FINDING_JOURNAL_NOT_READABLE}:{journal_status}")
    elif journal.get("errors"):
        findings.append(f"{FINDING_JOURNAL_NOT_READABLE}:unhealthy")
    if graph_status != GRAPH_STORE_OPENED:
        findings.append(f"{FINDING_GRAPH_NOT_READABLE}:{graph_status}")
    elif not graph.get("healthy"):
        findings.append(f"{FINDING_GRAPH_NOT_READABLE}:unhealthy")

    # Settled before the drift finding is added, so that "both stores opened and
    # are healthy" survives as its own answer: a clean graph that has drifted
    # from its journal is a different situation from one that will not open.
    readable = not findings

    # A missing key counts as drifted: an answer this module could not read is
    # not an answer that the projection is clean.
    consistent = readable and not bool(drift.get("drifted", True))
    if readable and not consistent:
        findings.append(FINDING_PROJECTION_DRIFTED)

    return _Health(
        graph_status=graph_status,
        journal_status=journal_status,
        node_count=int(graph.get("node_count") or 0),
        event_count=int(journal.get("event_count") or 0),
        readable=readable,
        consistent=consistent,
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def exit_code(report: Mapping[str, Any]) -> int:
    """The process exit code one report implies."""
    return EXIT_OK if report.get("status") in _SUCCESS_STATUSES else EXIT_BLOCKED


def _report(
    command: str,
    status: str,
    *,
    pointer: Path | None,
    generation: Path | None,
    control_root: Path | None = None,
    dry_run: bool = True,
    resolution: Mapping[str, Any] | None = None,
    previous: Mapping[str, Any] | None = None,
    findings: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": TOOL_NAME,
        "report_version": REPORT_VERSION,
        "command": command,
        "status": status,
        "dry_run": dry_run,
        "pointer": None if pointer is None else str(pointer),
        "generation": None if generation is None else str(generation),
        "control_root": None if control_root is None else str(control_root),
        "resolution": dict(resolution or {}),
        "previous": dict(previous or {}),
        "findings": sorted(set(findings)),
        "warnings": sorted(set(warnings)),
    }
    payload["exit_code"] = exit_code(payload)
    return payload


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status_generation(
    *, generation: Any = None, pointer: Any = None
) -> dict[str, Any]:
    """Report which generation one explicit input resolves to. Reads only.

    Strictly read-only in the literal sense: an absent pointer stays absent, no
    directory, database, schema or checkpoint row is created, and an existing
    generation is left byte-for-byte as it was. Exit code ``0`` means a proved
    generation is active; anything else — including "nothing is active" — is
    non-zero, because that is the answer a deployment check needs.

    ``--pointer`` reports what a *running* system would see, in the activated
    lifecycle; ``--generation`` re-proves a publication in full. The report names
    which one it ran in ``resolution.lifecycle`` and what it established in
    ``resolution.integrity``, so the two can never be read as the same check.
    """
    resolved = resolve_generation_runtime(generation=generation, pointer=pointer)
    report = resolved.report
    return _report(
        "status",
        report.status,
        pointer=None if not report.pointer else Path(report.pointer),
        generation=None if not report.generation else Path(report.generation),
        control_root=None if not report.control_root else Path(report.control_root),
        resolution=report.to_payload(),
        findings=report.findings,
        warnings=report.warnings,
    )


#: Pointer states in which a generation is actually being served.
SERVING_STATUSES = frozenset({STATUS_RUNTIME_ACTIVE, STATUS_RUNTIME_ROLLBACK})


def generation_is_served(*, pointer: Any, generation: Any) -> tuple[bool, dict[str, Any]]:
    """Is ``generation`` the one this pointer currently serves? Reads only.

    One seam, so every operator asks the question the same way. It exists
    because the answer decides *which proof applies*: a never-served generation
    is proved as a publication — manifest, membership, hashes — while a served
    one is writable, so those same hashes stop being a health test the moment a
    semantic write is accepted. Running the publication proof against a served
    generation would report a healthy, correctly-written store as corrupt.

    Returns the verdict and the read-only status report behind it, so a caller
    can report attested runtime health rather than re-deriving it.
    """
    report = status_generation(pointer=pointer)
    if report.get("status") not in SERVING_STATUSES:
        return False, report
    resolved = report.get("generation")
    if not resolved:
        return False, report
    target = os.path.abspath(os.path.expanduser(os.fspath(generation)))
    return os.path.abspath(str(resolved)) == target, report


# ---------------------------------------------------------------------------
# Activate
# ---------------------------------------------------------------------------

def activate_generation(
    *, pointer: Any, generation: Any, apply: bool = False
) -> dict[str, Any]:
    """Point one control-plane file at one proved generation.

    The order, and every step of it is a refusal point:

    1. the control root — the pointer's own parent — must already exist, not be
       a symlink and not be reached through one. It is never created: creating
       the directory a pointer lives in is how a tool ends up adopting a
       location it was merely handed;
    2. the pointer path must be writable *as a pointer*: not a symlink, not a
       directory, and — if a pointer is already there — parseable. An activation
       over a control file this tool cannot read fails closed with the corrupt
       bytes untouched, because overwriting them would destroy the only record
       of what the system thought it was serving;
    3. the generation must be strictly inside that control root, reached without
       traversing a symlink;
    4. under ``apply``, everything from here on happens inside that pointer's
       *interval* — one holder across every process and thread (see
       :func:`_serialized`). Not just the replace: the payload below names the
       pointer it supersedes, so deciding outside the interval is deciding from
       a snapshot somebody else may be replacing;
    5. a pointer that already names this generation, with a digest that still
       matches its manifest, is reported as unchanged with **zero** writes —
       and this is checked *before* the publication proof, so a repeated
       activation of a generation that has been written into since cutover stays
       the no-op it should be rather than becoming a refusal;
    6. otherwise the generation must prove as a *publication*: manifest,
       declared paths, the full verification and read-only health. This is the
       proof the pointer's ``manifest_sha256`` then attests, which is why a
       cutover and only a cutover performs it;
    7. without ``apply`` the report says what would happen and nothing is
       written: no pointer, no temporary file, no lock, no admission record, no
       directory;
    8. with ``apply`` the admissions of **both** generations the new pointer
       will name — the one being served and the one retained for a restore —
       are recorded first, under the guard a migration withdrawal takes (see
       :func:`_admitted`), so no withdrawal can move either out from under the
       pointer about to name them. Either one failing is a refusal: the pointer
       is not written and the previous one is left exactly as it was. A refusal
       that had already recorded the retained half keeps that record and says so
       (:data:`FINDING_ADMISSION_KEPT`), because an admission already given to a
       runtime is not something a later failure makes untrue;
    9. then the new pointer — carrying the superseded pointer's target and
       digest so a restore is possible — is written with one atomic replace, and
       the result is resolved again and compared against *this* request, because
       "we wrote it", "something resolves" and "the generation we were asked for
       resolves" are three different claims.

    Nothing is ever deleted: the generation this cutover leaves stays exactly
    where it is, with its own backups and manifest.
    """
    pointer_path = _resolved(pointer)
    generation_path = _resolved(generation)
    control_root = pointer_path.parent

    def report(status: str, **fields: Any) -> dict[str, Any]:
        return _report(
            "activate",
            status,
            pointer=pointer_path,
            generation=generation_path,
            control_root=control_root,
            dry_run=not apply,
            **fields,
        )

    root_findings = _control_root_findings(control_root)
    if root_findings:
        return report(STATUS_CUTOVER_REFUSED, findings=root_findings)

    target_findings = _pointer_write_findings(pointer_path)
    if target_findings:
        return report(STATUS_CUTOVER_REFUSED, findings=target_findings)

    relative, rejection = _relative_target(control_root, generation_path)
    if relative is None:
        return report(
            STATUS_CUTOVER_REFUSED,
            findings=[f"{FINDING_GENERATION_OUTSIDE_CONTROL_ROOT}:{rejection}"],
        )

    def decided() -> dict[str, Any]:
        return _decide_cutover(
            pointer_path,
            generation_path,
            control_root=control_root,
            relative=relative,
            report=report,
            apply=apply,
        )

    if not apply:
        return decided()
    return _serialized(pointer_path, report, STATUS_CUTOVER_REFUSED, decided)


def _decide_cutover(
    pointer_path: Path,
    generation_path: Path,
    *,
    control_root: Path,
    relative: str,
    report: Any,
    apply: bool,
) -> dict[str, Any]:
    """Steps 4 to 7 of :func:`activate_generation`, inside the interval.

    Split out for exactly one reason: under ``apply`` every line of it — the
    read of the pointer being superseded, the publication proof, the payload
    built from that read, the write and the confirmation — has to happen while
    one holder owns the pointer. A function boundary is how that becomes
    visible, and :func:`_serialized` is the only caller that can supply it.
    """
    current, current_findings, warnings = read_active_pointer(pointer_path)
    if current is None and current_findings != [FINDING_POINTER_ABSENT]:
        # Fail closed. The operator keeps both the refusal and the file, which
        # is what makes a repair possible; this tool will not decide on their
        # behalf that an unreadable control file was worthless.
        return report(
            STATUS_CUTOVER_REFUSED,
            findings=[FINDING_SUPERSEDED_POINTER_INVALID, *current_findings],
            warnings=warnings,
        )

    if current is not None and current.generation == relative:
        # Already serving this generation. Proved in the *activated* lifecycle,
        # so a generation the framework has legitimately written into since
        # cutover still answers "unchanged" instead of failing a publication
        # proof it can no longer pass.
        served = _resolve_root(
            generation_path,
            lifecycle=LIFECYCLE_ACTIVATED,
            control_root=control_root,
            declared=current,
            graph_mode="graph",
            pointer=pointer_path,
        )
        if served.usable:
            # An admission is recorded even here. A pointer this tool did not
            # write can be serving a generation nothing registered, and
            # answering "unchanged" to an operator while leaving it inside a
            # legacy withdrawal's reach would be the one case where being
            # already correct costs the guarantee.
            return _admitted(
                generation_path,
                served.report.manifest_sha256,
                report,
                STATUS_CUTOVER_REFUSED,
                lambda: report(
                    STATUS_CUTOVER_UNCHANGED,
                    resolution=served.report.to_payload(),
                    previous=_previous_payload(current),
                    warnings=warnings,
                ),
                apply=apply,
                retained=_retained_admission(control_root, current),
            )

    resolved = resolve_generation_runtime(generation=generation_path)
    if not resolved.usable:
        return report(
            STATUS_CUTOVER_REFUSED,
            resolution=resolved.report.to_payload(),
            findings=resolved.report.findings,
            warnings=[*warnings, *resolved.report.warnings],
        )

    payload = ActivePointer(
        generation=relative,
        activated_at=_now(),
        manifest_sha256=resolved.report.manifest_sha256,
        previous_generation="" if current is None else current.generation,
        previous_activated_at="" if current is None else current.activated_at,
        previous_manifest_sha256="" if current is None else current.manifest_sha256,
    )

    if not apply:
        return report(
            STATUS_CUTOVER_PLANNED,
            resolution=resolved.report.to_payload(),
            previous=_previous_payload(payload),
            warnings=warnings,
        )

    return _admitted(
        generation_path,
        payload.manifest_sha256,
        report,
        STATUS_CUTOVER_REFUSED,
        lambda: _write_and_confirm(
            "activate",
            pointer_path,
            payload,
            generation_path=generation_path,
            control_root=control_root,
            applied=STATUS_CUTOVER_APPLIED,
            unverified=STATUS_CUTOVER_UNVERIFIED,
            warnings=warnings,
        ),
        apply=apply,
        retained=_retained_admission(control_root, payload),
    )


def _control_root_findings(control_root: Path) -> list[str]:
    """Why a control root cannot be written in, if it cannot. Creates nothing.

    The ancestor check is here and not only in the resolver because this is the
    directory the one write lands in: a control root reached through a symlink
    would put the pointer somewhere the containment rule never examined, and
    every "strictly inside the control root" conclusion drawn afterwards would
    be about the link.
    """
    if control_root.is_symlink():
        return [f"{FINDING_CONTROL_ROOT_REJECTED}:{_REJECTION_SYMLINK}"]
    if _ancestor_symlink(control_root):
        return [f"{FINDING_CONTROL_ROOT_REJECTED}:{_REJECTION_ANCESTOR}"]
    if not control_root.exists():
        return [f"{FINDING_CONTROL_ROOT_REJECTED}:absent"]
    if not control_root.is_dir():
        return [f"{FINDING_CONTROL_ROOT_REJECTED}:not_a_directory"]
    return []


def _pointer_write_findings(pointer: Path) -> list[str]:
    """Why this path may not be written as a pointer, if it may not.

    A symlink is refused rather than replaced: an operator who linked this name
    meant something by it, and silently turning their link into a regular file
    is not a cutover decision this tool gets to take. A directory is refused
    because it is somebody else's. A file that exists but cannot be parsed is
    refused by the caller, which has the findings to report with it.
    """
    if pointer.is_symlink():
        return [FINDING_POINTER_SYMLINK]
    if pointer.exists() and not pointer.is_file():
        return [FINDING_POINTER_NOT_A_FILE]
    return []


def _previous_payload(pointer: ActivePointer) -> dict[str, Any]:
    """The bounded view of the pointer target one write retains."""
    return {
        "generation": pointer.previous_generation,
        "activated_at": pointer.previous_activated_at,
        "manifest_sha256": pointer.previous_manifest_sha256,
    }


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_generation(*, pointer: Any, apply: bool = False) -> dict[str, Any]:
    """Point back at the generation the current pointer retained.

    The rollback of a cutover, and the mirror image of
    :func:`~layered_memory_mcp.graph_migration.rollback_migration`: it moves a
    pointer, never data. No pointer and no retained target are both "nothing to
    restore" and a no-op.

    The target is proved in the **activated** lifecycle, against the digest the
    pointer *retained* for it (``previous_manifest_sha256``) — not against
    whatever digest the target happens to carry now. That is the whole point of
    retaining it: the retained generation was activated once and has been
    writable since, so its publication hashes may legitimately have moved on,
    but its identity must not have. A retained generation whose manifest no
    longer matches the retained digest is a refusal
    (:data:`FINDING_POINTER_MANIFEST_CHANGED`), and so is one the pointer
    recorded no digest for at all — restoring onto a generation nothing attested
    would be a cutover nobody reviewed.

    The restore itself retains the generation it leaves, so it is reversible,
    and no generation is removed by it. Like a cutover it holds the pointer's
    whole interval under ``apply`` and admits what it publishes — both the
    generation it returns to and the one it retains — so neither can be
    withdrawn by a migration rollback afterwards, and an admission that cannot
    be established for either of them is a refusal that moves no pointer.
    """
    pointer_path = _resolved(pointer)
    control_root = pointer_path.parent

    def report(status: str, **fields: Any) -> dict[str, Any]:
        return _report(
            "restore",
            status,
            pointer=pointer_path,
            generation=fields.pop("target", None),
            control_root=control_root,
            dry_run=not apply,
            **fields,
        )

    root_findings = _control_root_findings(control_root)
    if root_findings:
        return report(STATUS_RESTORE_REFUSED, findings=root_findings)

    write_findings = _pointer_write_findings(pointer_path)
    if write_findings:
        return report(STATUS_RESTORE_REFUSED, findings=write_findings)

    def decided() -> dict[str, Any]:
        return _decide_restore(
            pointer_path, control_root=control_root, report=report, apply=apply
        )

    if not apply:
        return decided()
    return _serialized(pointer_path, report, STATUS_RESTORE_REFUSED, decided)


def _decide_restore(
    pointer_path: Path, *, control_root: Path, report: Any, apply: bool
) -> dict[str, Any]:
    """The body of :func:`restore_generation`, inside the interval.

    The same discipline as :func:`_decide_cutover`, and for the same reason: the
    retained target a restore publishes is read out of the pointer it is about
    to replace, so reading it outside the interval is reading a decision
    somebody else may already have superseded.
    """
    current, findings, warnings = read_active_pointer(pointer_path)
    if current is None:
        if findings == [FINDING_POINTER_ABSENT]:
            return report(STATUS_RESTORE_NOTHING, warnings=[*warnings, *findings])
        return report(STATUS_RESTORE_REFUSED, findings=findings, warnings=warnings)

    if not current.previous_generation.strip():
        return report(STATUS_RESTORE_NOTHING, warnings=warnings)

    target, rejection = _pointer_target(control_root, current.previous_generation)
    if target is None:
        return report(STATUS_RESTORE_REFUSED, findings=[rejection], warnings=warnings)

    # The retained attestation, promoted into the position a resolve checks it
    # in. Built here rather than passed as a flag so that the *same* comparison
    # guards a restore and a pointer reopen: a retained digest that no longer
    # matches the target is ``pointer_manifest_digest_changed`` either way.
    retained = ActivePointer(
        generation=current.previous_generation,
        activated_at=current.previous_activated_at,
        manifest_sha256=current.previous_manifest_sha256,
    )
    resolved = _resolve_root(
        target,
        lifecycle=LIFECYCLE_ACTIVATED,
        control_root=control_root,
        declared=retained,
        graph_mode="graph",
        pointer=pointer_path,
    )
    if not resolved.usable:
        return report(
            STATUS_RESTORE_REFUSED,
            target=target,
            resolution=resolved.report.to_payload(),
            findings=resolved.report.findings,
            warnings=[*warnings, *resolved.report.warnings],
        )

    payload = ActivePointer(
        generation=retained.generation,
        activated_at=_now(),
        # The retained digest, carried forward unchanged. Writing the digest the
        # target happens to have now would launder a replaced manifest into a
        # fresh attestation that no proof ever stood behind.
        manifest_sha256=retained.manifest_sha256,
        # The generation being left is retained in turn: a restore must itself
        # be reversible, and the pointer it replaces must not be lost.
        previous_generation=current.generation,
        previous_activated_at=current.activated_at,
        previous_manifest_sha256=current.manifest_sha256,
    )

    if not apply:
        return report(
            STATUS_RESTORE_PLANNED,
            target=target,
            resolution=resolved.report.to_payload(),
            previous=_previous_payload(payload),
            warnings=warnings,
        )

    return _admitted(
        target,
        payload.manifest_sha256,
        report,
        STATUS_RESTORE_REFUSED,
        lambda: _write_and_confirm(
            "restore",
            pointer_path,
            payload,
            generation_path=target,
            control_root=control_root,
            applied=STATUS_RESTORE_APPLIED,
            unverified=STATUS_CUTOVER_UNVERIFIED,
            warnings=warnings,
        ),
        apply=apply,
        retained=_retained_admission(control_root, payload),
    )


# ---------------------------------------------------------------------------
# Admission — putting a generation out of a legacy withdrawal's reach
# ---------------------------------------------------------------------------

def _admitted(
    generation_path: Path,
    digest: str,
    report: Any,
    refused: str,
    publish: Any,
    *,
    apply: bool,
    retained: tuple[tuple[Path, str] | None, list[str]],
) -> dict[str, Any]:
    """Admit **both** generations the pointer will name, then run ``publish``.

    The second half of the withdrawal defect's repair, and the half that lives
    on this side of the boundary: before a pointer names a generation, the
    destination that could withdraw it is told that a runtime has it. A pointer
    names two — the one being served and the one retained so the move is
    reversible — and a runtime has been given both, so both are admitted here
    and **either one failing is a refusal that writes no pointer**.

    The order of the two is fixed and the retained half goes first, for one
    reason: that is the half whose protection used to be best effort, so a
    refusal there must be able to leave *nothing* behind. There is no claim of a
    transaction spanning the two records. If the retained half is recorded and
    the served half then fails, the retained record stays — an admission is
    never retracted, because "we changed our mind about publishing" is not proof
    that nothing was ever given to a runtime. A surplus record costs one future
    rollback refusal; a missing one costs an operator their generation.

    What such a refusal does owe the operator is the *truth* about itself, so
    both halves report through the same result — findings, and whether a record
    was actually written — and a refusal that left a record behind names it with
    :data:`FINDING_ADMISSION_KEPT`. "Refused, and one admission record for the
    retained generation remains" is a different state from "refused, nothing
    happened", and only the first is honest about a destination whose next
    withdrawal will now refuse.

    The two are never held at once (see :func:`_admit_generation`), so two
    destinations cannot deadlock against each other, and the lock order across
    the module stays pointer interval → generation guard with only one guard
    held at a time.

    ``apply`` is honoured, not assumed: a preview records nothing, because
    recording is a write and the ledger is part of the tree a preview promises
    to leave alone.
    """
    if not apply:
        return publish()

    retained_target, retained_findings = retained
    if retained_findings:
        return report(
            refused, findings=[FINDING_RETAINED_ADMISSION, *retained_findings]
        )

    kept: list[str] = []
    if retained_target is not None:
        failed, recorded = _admit_generation(*retained_target)
        if failed:
            return report(refused, findings=[FINDING_RETAINED_ADMISSION, *failed])
        if recorded:
            kept.append(f"{FINDING_ADMISSION_KEPT}:previous")

    failed, _ = _admit_generation(generation_path, digest)
    if failed:
        return report(refused, findings=[*failed, *kept])
    return publish()


def _admit_generation(generation_path: Path, digest: str) -> tuple[list[str], bool]:
    """Record one generation's admission, or say why it could not be recorded.

    The one registration path, used for the generation being served and for the
    generation being retained, so the two cannot drift apart: whatever is
    checked for one is checked for the other, and a caller cannot get a weaker
    answer by asking about the retained half.

    Written under the *same* guard
    :func:`~layered_memory_mcp.graph_migration.rollback_migration` takes, which
    is what makes the two operations one serial sequence in both directions:

    * a withdrawal that got there first has already moved the generation, so the
      re-check under the guard fails and this operation refuses — without a
      pointer ever naming a directory that is not there;
    * an admission that got there first is in the ledger when the withdrawal
      looks, so the withdrawal refuses instead.

    The guard is taken only when a record actually has to be written, and only
    ever one at a time. A generation in neither of the two positions a
    withdrawal can reach (:func:`owning_destination` answers ``None``) has
    nothing to be protected from and needs no record.

    Returns:
        ``(findings, a record was written just now)``. Empty findings mean this
        generation is admitted, whether this call recorded it or found it
        already recorded; the flag is what lets a caller that refuses *later*
        say which records its refusal left behind instead of implying none.
    """
    destination = owning_destination(generation_path)
    if destination is None:
        return [], False

    outstanding, findings = _admission_outstanding(destination, generation_path, digest)
    if findings:
        return findings, False
    if not outstanding:
        return [], False

    try:
        with control_lock(served_lock_path(destination), directory=True):
            moved = _admission_drift(generation_path, digest)
            if moved:
                return moved, False
            register_admission(destination, generation_path, digest, tool=TOOL_NAME)
    except ControlLockError as exc:
        return [f"{FINDING_GENERATION_GUARD}:{exc.label}"], False
    except OSError as exc:
        # The call that raised may have written part of a record or none of it;
        # either way this generation is not admitted, so the refusal stands and
        # the debris is accounted for by the ledger reader, not by a delete here.
        return [f"{FINDING_ADMISSION_FAILED}:{type(exc).__name__}"], False
    return [], True


def _admission_outstanding(
    destination: Path, generation_path: Path, digest: str
) -> tuple[bool, list[str]]:
    """``(a record still has to be written, findings)``. Reads only.

    Two independent questions, and the second is checked whether or not the
    first is answered "no": an existing record protects the tree it was written
    for, and a generation whose manifest is not the one a pointer is about to
    attest is not that tree. Accepting a record as sufficient on its own would
    let an already-admitted but replaced or corrupted generation past the very
    check the digest exists for.
    """
    digests, findings = admitted_digests(destination)
    if findings:
        return False, findings
    drift = _admission_drift(generation_path, digest)
    if drift:
        return False, drift
    return digest not in digests, []


def _retained_admission(
    control_root: Path, payload: ActivePointer
) -> tuple[tuple[Path, str] | None, list[str]]:
    """``((path, digest) or None, findings)`` for what one payload retains.

    Three answers, and the third one is the repair:

    * ``(None, [])`` — the payload retains nothing. The ordinary first cutover,
      and not a fault;
    * ``((path, digest), [])`` — a retained generation this module can place and
      identify, protected exactly like the one being served;
    * ``(None, findings)`` — a retained generation it cannot. No recorded
      attestation, a target outside the control root, a target reached through a
      symlink, or a malformed path. Previously each of these silently produced
      "nothing to admit", which is how a pointer could be published whose
      retained generation was provably unprotected.

    The retained target is resolved through the same containment rule a pointer
    read uses, so nothing outside the control root is ever recorded — and a
    target that fails that rule is reported rather than dropped.
    """
    declared = payload.previous_generation.strip()
    digest = payload.previous_manifest_sha256.strip()
    if not declared and not digest:
        return None, []
    if not declared:
        # A retained attestation with nothing to attest. Not a pointer this tool
        # writes, and not one it will publish over either.
        return None, [f"{FINDING_POINTER_TARGET_REJECTED}:{_REJECTION_MALFORMED}"]
    if not digest:
        return None, [FINDING_POINTER_ATTESTATION]
    target, rejection = _pointer_target(control_root, declared)
    if target is None:
        return None, [rejection]
    return (target, digest), []


def _admission_drift(generation_path: Path, digest: str) -> list[str]:
    """Whether the generation is still the one that was proved. Reads only.

    The re-check that makes the guard worth holding. Everything this operation
    established about the generation was established *before* the guard was
    taken, so between the proof and the record a withdrawal may have renamed the
    tree away or a republication may have put a different one in its place. The
    manifest is the identity a pointer attests and a runtime write never touches
    it, so comparing its digest answers both questions at once.
    """
    manifest, findings = read_manifest(generation_path)
    if manifest is None:
        return [f"{FINDING_GENERATION_MOVED}:absent"]
    if findings or manifest_digest(manifest) != digest:
        return [f"{FINDING_GENERATION_MOVED}:digest"]
    return []


# ---------------------------------------------------------------------------
# The interval
# ---------------------------------------------------------------------------

def pointer_lock_path(pointer: Any) -> Path:
    """The reserved lock path for a pointer's read/write interval.

    Kept as a hidden sibling for path validation and existing callers. The
    interval never creates this file: it locks the existing parent directory's
    inode instead, so even a first-use applying no-op changes no directory mtime.
    A symlink or non-file at the reserved path still refuses closed.
    """
    path = _resolved(pointer)
    return path.parent / f".{path.name}{POINTER_LOCK_SUFFIX}"


def _serialized(
    pointer_path: Path,
    report: Any,
    refused: str,
    operation: Any,
) -> dict[str, Any]:
    """Run one pointer-changing operation holding that pointer's interval.

    The whole read/validate/decide/write/confirm window, not just the final
    replace. The replace was always atomic; what was not serialisable was the
    *decision*, because the payload a cutover writes names the pointer it
    superseded and a restore's target is read out of the pointer it replaces.
    Two operations that each read before either wrote could therefore both
    report success while one of them erased the other's retained generation —
    an outcome no serial order produces.

    A lock that cannot be taken is a refusal with nothing written, never a
    silent unsynchronised run.
    """
    try:
        with control_lock(pointer_lock_path(pointer_path), directory=True):
            return operation()
    except ControlLockError as exc:
        return report(refused, findings=[f"{FINDING_POINTER_LOCK}:{exc.label}"])


# ---------------------------------------------------------------------------
# The one write
# ---------------------------------------------------------------------------

def _write_and_confirm(
    command: str,
    pointer: Path,
    payload: ActivePointer,
    *,
    generation_path: Path,
    control_root: Path,
    applied: str,
    unverified: str,
    warnings: Sequence[str],
) -> dict[str, Any]:
    """Write the pointer atomically, then resolve it again and report."""
    try:
        _write_pointer(pointer, payload)
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return _report(
            command,
            STATUS_POINTER_WRITE_FAILED,
            pointer=pointer,
            generation=generation_path,
            control_root=control_root,
            dry_run=False,
            findings=[f"{FINDING_POINTER_WRITE_FAILED}:{type(exc).__name__}"],
            warnings=warnings,
        )

    confirmed = resolve_generation_runtime(pointer=pointer)
    drift = _confirmation_findings(confirmed, payload, generation_path)
    established = confirmed.usable and not drift
    return _report(
        command,
        applied if established else unverified,
        pointer=pointer,
        generation=generation_path,
        control_root=control_root,
        dry_run=False,
        resolution=confirmed.report.to_payload(),
        previous=_previous_payload(payload),
        findings=() if established else [*drift, *confirmed.report.findings],
        warnings=[*warnings, *confirmed.report.warnings],
    )


def _confirmation_findings(
    confirmed: GenerationRuntime, payload: ActivePointer, generation_path: Path
) -> list[str]:
    """Why the pointer that resolved is not the pointer that was asked for.

    The check ``usable`` cannot make. A resolution proves that *some* generation
    the control plane names can be served; a cutover additionally claims that it
    is the generation named in the report, proved under the attestation that
    report prints. Both the path and the digest are therefore compared against
    the payload this operation wrote, not against whatever the pointer now says
    about itself — which is how a pointer replaced in between is caught rather
    than adopted.

    Three bounded answers, in the order an operator would read them: the pointer
    names another generation, it carries an attestation this operation did not
    establish, or it names this generation by a path that resolved elsewhere.
    """
    report = confirmed.report
    if report.relative_generation != payload.generation:
        return [f"{FINDING_POINTER_CONFIRMATION_DRIFT}:generation"]
    if not report.attested or report.manifest_sha256 != payload.manifest_sha256:
        return [f"{FINDING_POINTER_CONFIRMATION_DRIFT}:attestation"]
    if confirmed.config is None or confirmed.config.root != generation_path:
        return [f"{FINDING_POINTER_CONFIRMATION_DRIFT}:target"]
    return []


def _write_pointer(pointer: Path, payload: ActivePointer) -> None:
    """Replace the pointer with ``payload`` atomically, or leave it as it was.

    The discipline, and the reason it is not a plain ``write_text``: a pointer
    is the one file a *running* system reads to decide what it is serving, and a
    half-written one would be a generation nobody chose. The body is written to
    a private temporary file beside the pointer (same directory, so the rename
    cannot cross a filesystem), flushed and ``fsync``-ed, and then moved into
    place with :func:`os.replace` — atomic on POSIX and on Windows. The
    directory is ``fsync``-ed afterwards so the rename itself survives a crash.

    No directory is created: the control root is validated by the caller and an
    absent one is a refusal, not something to bring into existence. A failure
    anywhere removes the temporary file and leaves the previous pointer intact.
    """
    body = json.dumps(payload.to_dict(), sort_keys=True, indent=2) + "\n"
    directory = pointer.parent
    temporary = directory / f".{pointer.name}.{uuid.uuid4().hex[:12]}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, pointer)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - a platform that cannot open a directory
        return
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - a filesystem that cannot sync one
        pass
    finally:
        os.close(handle)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

_EPILOG = (
    "Every input is explicit: this tool never discovers a generation, reads a "
    "configuration file or changes a running service by itself. Omitting the "
    "verb runs a read-only status, and activate/restore report what they would "
    "do unless --apply is given. The report is JSON on stdout; exit 0 means a "
    "proved generation is active or the operation did what it says."
)


def add_cutover_verbs(verbs: Any) -> tuple[str, ...]:
    """Register ``status``/``activate``/``restore`` on a subparsers action.

    One definition of these three verbs and their arguments, used by this
    module's own CLI and contributed to :mod:`.graph_migration`'s, so an
    operator reaches identical flags whichever entry point they type.

    Returns:
        The verb names that were added.
    """
    status = verbs.add_parser(
        "status",
        help="report which generation an explicit input resolves to, read-only",
        description=(
            "Resolve and prove one generation. Reads only: nothing is created, "
            "written or repaired, and an absent pointer stays absent."
        ),
    )
    inputs = status.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--generation", metavar="PATH", default=None, help="an explicit generation root"
    )
    inputs.add_argument(
        "--pointer", metavar="PATH", default=None, help="an explicit active pointer file"
    )

    activate = verbs.add_parser(
        "activate",
        help="point an active pointer at a proved generation",
        description=(
            "Validate a generation and make it the active one by replacing the "
            "pointer atomically. Reports what it would do unless --apply."
        ),
    )
    activate.add_argument(
        "--pointer",
        metavar="PATH",
        required=True,
        help="the active pointer file to write; its parent is the control root",
    )
    activate.add_argument(
        "--generation",
        metavar="PATH",
        required=True,
        help="the generation to activate; must be inside the control root",
    )

    restore = verbs.add_parser(
        "restore",
        help="point back at the generation the pointer retained",
        description=(
            "Restore the retained previous generation. Validates it first, "
            "retains the one it leaves, and deletes nothing."
        ),
    )
    restore.add_argument(
        "--pointer", metavar="PATH", required=True, help="the active pointer file"
    )

    for sub in (activate, restore):
        sub.add_argument(
            "--apply",
            action="store_true",
            help="actually write the pointer (default: report and write nothing)",
        )
    return CUTOVER_COMMANDS


def cutover_command(command: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Run one parsed cutover verb and return its report.

    The single dispatch both CLIs use, so the migration operator's ``activate``
    and this module's ``activate`` cannot come to mean different things.
    """
    if command == "status":
        return status_generation(
            generation=getattr(arguments, "generation", None),
            pointer=getattr(arguments, "pointer", None),
        )
    if command == "activate":
        return activate_generation(
            pointer=arguments.pointer,
            generation=arguments.generation,
            apply=bool(getattr(arguments, "apply", False)),
        )
    if command == "restore":
        return restore_generation(
            pointer=arguments.pointer, apply=bool(getattr(arguments, "apply", False))
        )
    raise ValueError(f"unknown cutover command: {command!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {TOOL_NAME}",
        description=(
            "Resolve, prove and activate one published layered-memory graph "
            "generation."
        ),
        epilog=_EPILOG,
    )
    verbs = parser.add_subparsers(
        dest="command", metavar="{" + ",".join(CUTOVER_COMMANDS) + "}"
    )
    add_cutover_verbs(verbs)
    return parser


def _normalize(argv: Sequence[str]) -> list[str]:
    """Insert the default ``status`` verb when the first argument is an option.

    What makes the read-only verb the default: an invocation naming only
    ``--pointer`` reports rather than erroring, and one that starts with a verb
    is left exactly as given. ``--help`` is never rewritten.
    """
    arguments = list(argv)
    if arguments and not arguments[0].startswith("-"):
        return arguments
    if any(item in ("-h", "--help") for item in arguments):
        return arguments
    return [CUTOVER_COMMANDS[0], *arguments]


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and print its JSON report. Returns the exit code."""
    parser = _parser()
    arguments = parser.parse_args(_normalize(sys.argv[1:] if argv is None else argv))
    command = arguments.command or CUTOVER_COMMANDS[0]

    try:
        report = cutover_command(command, arguments)
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        print(f"graph_generation_runtime_failed:{type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILED

    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())

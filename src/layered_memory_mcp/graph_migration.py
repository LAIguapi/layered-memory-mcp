"""The graph migration operator tool (ARCHITECTURE_v2.md §12 migration contract).

WHAT THIS MODULE IS
-------------------
One operator CLI — ``python -m layered_memory_mcp.graph_migration`` — that takes
an **explicitly supplied** old-format memory snapshot and produces a verified
graph-era generation at an **explicitly supplied** destination:

.. code-block:: text

    python -m layered_memory_mcp.graph_migration \\
        --source /srv/snapshots/memory-2026-03-01 \\
        --destination /srv/layered-memory/migrated            # read-only plan

    python -m layered_memory_mcp.graph_migration apply    --source … --destination …
    python -m layered_memory_mcp.graph_migration verify   --source … --destination …
    python -m layered_memory_mcp.graph_migration rollback --destination …

Four verbs, one report shape, JSON on stdout and a bounded exit code. Omitting
the verb runs ``plan``, so the *default* behaviour of this tool is to read and
report: a forgotten argument cannot become a publication.

Three further verbs — ``status``, ``activate`` and ``restore`` — belong to
:mod:`.graph_generation_runtime`, which owns the *runtime* side of the same
contract: pointing one explicit control-plane file at one proved generation.
They are contributed by that module rather than implemented here, so an
operator reaches one tool while publishing and serving stay separate
implementations with separate success sets.

WHAT IT IS NOT
--------------
- NOT the existing ``domain_keywords`` migration. :mod:`.migrate` is a separate
  console script with a separate job and is untouched by this module.
- NOT a discoverer. ``--source`` and ``--destination`` are required and are the
  only roots that are ever read or written. No home directory, no
  ``config.yaml``, no ``LAYERED_MEMORY_*`` variable and no installed service is
  consulted, and no :class:`~layered_memory_mcp.config.MemoryConfig` is built —
  see :class:`~layered_memory_mcp.graph_migration_generation.GenerationConfig`
  for the explicit, resolution-free object the framework's own projection
  routines are driven with.
- NOT a cutover. The four verbs below publish a generation into a directory.
  They do not change a running service, a configuration file, a default mode or
  a user's store, and nothing here is wired into any startup path, MCP tool or
  REST route. A cutover is the separate, explicit act of pointing a control-plane
  file at a proved generation, which is what the contributed ``activate`` verb
  does and why it refuses to write anything without ``--apply``.
- NOT a guesser. An old random vector identity is never inferred from a body, a
  summary, a heading or a file name. Unresolved identity is a blocking finding;
  the only resolution is an explicit ``--identity-map`` whose every entry is
  validated against the inventory first.

THE THREE GUARANTEES
--------------------
1. **A dry run writes nothing.** Not the source, not the destination, not even
   the destination directory. The vector database is read through a SQLite
   ``mode=ro&immutable=1`` URI, the journal and graph through their read-only
   entrances, so an inventory cannot create, lock or checkpoint what it reads.
2. **A publication is one rename.** The whole generation is built privately,
   verified there, re-checked against the frozen source fingerprint and then
   moved into place with a single :func:`os.rename`. Four stores do not share a
   transaction and this tool does not pretend they do.
3. **Nothing is destroyed.** The source is never written. The source bytes are
   retained inside the generation. A rollback renames a generation aside and
   keeps it — and refuses outright for one a runtime has been given, because
   moving a generation an active pointer names is an outage rather than an undo.
   A failed publication leaves accounted-for debris, never a partial
   destination.

PRIVACY (§15)
-------------
The report carries the two operator-supplied paths (an operator tool must say
what it operated on), counts, relative paths, row/node identities, hashes and
stable refusal tokens. Never a memory body, a heading, a summary, a vector
value or an exception message.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .graph_migration_bootstrap import BootstrapPlan, plan_bootstrap
from .graph_migration_generation import (
    FINDING_ADMISSION_UNPROVED,
    FINDING_GENERATION_SERVED,
    FINDING_LOCK_UNAVAILABLE,
    FINDING_SOURCE_DRIFT,
    GENERATION_DIR,
    ControlLockError,
    admission_findings,
    build_generation,
    control_lock,
    destination_findings,
    destination_prestate,
    new_staging_path,
    publish_generation,
    quarantine_staging,
    read_manifest,
    served_lock_path,
    verify_generation,
    withdraw_generation,
)
from .graph_migration_inventory import SourceSnapshot, read_source_snapshot

__all__ = [
    "BEFORE_PUBLISH_HOOK",
    "COMMANDS",
    "EXIT_BLOCKED",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "FINDING_ADMISSION_UNPROVED",
    "FINDING_DESTINATION_CHANGED",
    "FINDING_DESTINATION_SOURCE_MISMATCH",
    "FINDING_GENERATION_SERVED",
    "FINDING_LOCK_UNAVAILABLE",
    "FINDING_PATH_OVERLAP",
    "MIGRATION_COMMANDS",
    "REPORT_VERSION",
    "STATUS_ALREADY_PUBLISHED",
    "STATUS_APPLIED",
    "STATUS_BLOCKED",
    "STATUS_DESTINATION_REFUSED",
    "STATUS_PLANNED",
    "STATUS_PUBLICATION_FAILED",
    "STATUS_ROLLBACK_NOTHING",
    "STATUS_ROLLBACK_REFUSED",
    "STATUS_ROLLED_BACK",
    "STATUS_SOURCE_DRIFT",
    "STATUS_VERIFIED",
    "STATUS_VERIFY_FAILED",
    "TOOL_NAME",
    "apply_migration",
    "exit_code",
    "main",
    "plan_migration",
    "rollback_migration",
    "verify_migration",
]

TOOL_NAME = "layered_memory_mcp.graph_migration"

#: The report layout version. A consumer branches on this rather than on the
#: presence of a key.
REPORT_VERSION = 1

# ---------------------------------------------------------------------------
# Exit codes — bounded, and a refusal is never zero
# ---------------------------------------------------------------------------

#: The operation did what it said: planned, applied, verified, rolled back, or
#: found the destination already holding exactly this migration.
EXIT_OK = 0

#: The operation refused. A finding explains why, nothing was published, and
#: the source is untouched. The ordinary non-zero answer.
EXIT_BLOCKED = 1

#: Argparse's own usage exit. Kept as a named constant so a caller does not have
#: to know that 2 is argparse's convention.
EXIT_USAGE = 2

#: An unexpected fault. Reported as a bounded token on stderr, because an
#: operator's stdout is a JSON stream.
EXIT_FAILED = 3

# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

STATUS_PLANNED = "migration_planned"
STATUS_BLOCKED = "migration_blocked"
STATUS_APPLIED = "migration_applied"
STATUS_ALREADY_PUBLISHED = "migration_already_published"
STATUS_VERIFIED = "migration_verified"
STATUS_VERIFY_FAILED = "migration_verify_failed"
STATUS_SOURCE_DRIFT = "migration_source_drift"
STATUS_PUBLICATION_FAILED = "migration_publication_failed"
STATUS_DESTINATION_REFUSED = "migration_destination_refused"
STATUS_ROLLED_BACK = "migration_rolled_back"
STATUS_ROLLBACK_NOTHING = "migration_rollback_nothing_published"
STATUS_ROLLBACK_REFUSED = "migration_rollback_refused"

#: The statuses that are not a refusal. Everything else exits non-zero.
_SUCCESS_STATUSES = frozenset(
    {
        STATUS_PLANNED,
        STATUS_APPLIED,
        STATUS_ALREADY_PUBLISHED,
        STATUS_VERIFIED,
        STATUS_ROLLED_BACK,
        STATUS_ROLLBACK_NOTHING,
    }
)

FINDING_PATH_OVERLAP = "path_overlap"
FINDING_DESTINATION_SOURCE_MISMATCH = "destination_source_mismatch"
FINDING_DESTINATION_CHANGED = "destination_changed"
FINDING_STAGED_BUILD_FAILED = "staged_build_failed"
FINDING_STAGED_VERIFICATION_FAILED = "staged_verification_failed"
FINDING_PUBLICATION_FAILED = "publication_failed"

#: This module's own verbs. The first is the default, which is why the default
#: behaviour of the tool is to read and report.
MIGRATION_COMMANDS: tuple[str, ...] = ("plan", "apply", "verify", "rollback")

#: Every verb this CLI accepts. The last three belong to
#: :mod:`.graph_generation_runtime` — the runtime side of the same contract —
#: and are contributed by it rather than implemented here, so an operator has
#: one tool and the cutover has one implementation. The import is function-local
#: (see :func:`_parser` and :func:`main`) to keep the dependency one-way: the
#: runtime adapter reads this module's exit codes, and this module must not
#: import it at module scope in return.
COMMANDS: tuple[str, ...] = (*MIGRATION_COMMANDS, "status", "activate", "restore")

#: Test seam, called with ``(staging path, publication target)`` immediately
#: before the frozen source fingerprint is revalidated and the generation is
#: published. ``None`` in normal operation and never set by this module.
#:
#: It exists because the two failure modes that matter most here are the two
#: hardest to reach from outside: a source that changes during the publication
#: window, and a publication that fails at the boundary itself. A raising hook
#: exercises the second; a hook that touches the source exercises the first.
BEFORE_PUBLISH_HOOK: Callable[[Path, Path], None] | None = None


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def exit_code(report: Mapping[str, Any]) -> int:
    """The process exit code one report implies."""
    return EXIT_OK if report.get("status") in _SUCCESS_STATUSES else EXIT_BLOCKED


def _report(
    command: str,
    status: str,
    *,
    source: Path | None,
    destination: Path,
    identity_map: Path | None = None,
    dry_run: bool = False,
    inventory: Mapping[str, Any] | None = None,
    bootstrap: Mapping[str, Any] | None = None,
    generation: Mapping[str, Any] | None = None,
    verification: Mapping[str, Any] | None = None,
    findings: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": TOOL_NAME,
        "report_version": REPORT_VERSION,
        "command": command,
        "status": status,
        "dry_run": dry_run,
        "source": None if source is None else str(source),
        "destination": str(destination),
        "identity_map": None if identity_map is None else str(identity_map),
        "inventory": dict(inventory or {}),
        "bootstrap": dict(bootstrap or {}),
        "generation": dict(generation or {}),
        "verification": dict(verification or {}),
        "findings": sorted(set(findings)),
        "warnings": sorted(set(warnings)),
    }
    payload["exit_code"] = exit_code(payload)
    return payload


def _resolved(path: Any) -> Path:
    """An operator-supplied path, expanded and absolute. Nothing is created."""
    return Path(path).expanduser().absolute()


def _overlap_findings(source: Path, destination: Path) -> list[str]:
    """Refuse a source and destination that are the same tree or nested.

    A destination inside a source would make the migration part of its own
    input on the next run; a source inside a destination would put the frozen
    original under the thing that is allowed to be rewritten. Both are refused
    by name rather than handled.
    """
    if source == destination:
        return [f"{FINDING_PATH_OVERLAP}:identical"]
    if source in destination.parents:
        return [f"{FINDING_PATH_OVERLAP}:destination_inside_source"]
    if destination in source.parents:
        return [f"{FINDING_PATH_OVERLAP}:source_inside_destination"]
    return []


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def plan_migration(
    *, source: Any, destination: Any, identity_map: Any = None
) -> dict[str, Any]:
    """Inventory a source and report what a migration would do. Writes nothing.

    Reads the entire source read-only and the destination's shape, and produces
    the inventory, the row classification, the bootstrap plan it would commit
    and every finding that would block it. No directory, lock, schema, index or
    write-ahead log is created on either side, and a non-existent destination
    stays non-existent.
    """
    return _plan_or_apply(
        "plan",
        source=source,
        destination=destination,
        identity_map=identity_map,
        dry_run=True,
    )


def apply_migration(
    *,
    source: Any,
    destination: Any,
    identity_map: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build, verify and publish one migration generation.

    The order, and every step of it is a refusal point:

    1. the paths must not overlap and the destination must hold nothing this
       tool does not own;
    2. an already-published generation is compared against the current source —
       an identical one is a no-op with **zero** writes, a different source or a
       changed destination is refused;
    3. the source inventory is re-read and must be free of findings: an
       unresolved, conflicting, malformed or missing row blocks publication;
    4. the bootstrap event stream is planned and must replay to exactly the
       snapshot the importer validated;
    5. the generation is built privately and verified *there*;
    6. the frozen source fingerprint is revalidated immediately before
       publication;
    7. the generation is published with one atomic rename, and verified again.

    Any failure after step 5 leaves the staged tree as accounted-for debris and
    no visible generation.
    """
    return _plan_or_apply(
        "apply",
        source=source,
        destination=destination,
        identity_map=identity_map,
        dry_run=dry_run,
    )


def _plan_or_apply(
    command: str,
    *,
    source: Any,
    destination: Any,
    identity_map: Any,
    dry_run: bool,
) -> dict[str, Any]:
    source_path = _resolved(source)
    destination_path = _resolved(destination)
    map_path = None if identity_map is None else _resolved(identity_map)
    target = destination_path / GENERATION_DIR

    def report(status: str, **fields: Any) -> dict[str, Any]:
        return _report(
            command,
            status,
            source=source_path,
            destination=destination_path,
            identity_map=map_path,
            dry_run=dry_run,
            **fields,
        )

    overlap = _overlap_findings(source_path, destination_path)
    if overlap:
        return report(STATUS_BLOCKED, findings=overlap)

    destination_refusals, destination_warnings = destination_findings(destination_path)
    prestate = destination_prestate(destination_path)

    # The source is read before the destination is judged, because a published
    # generation can only be compared against a fingerprint.
    snapshot = read_source_snapshot(
        source_path, destination_prestate=prestate, identity_map_path=map_path
    )
    inventory = snapshot.inventory.to_payload()
    warnings = [*destination_warnings, *snapshot.inventory.warnings]

    # Unknown destination content is judged before anything else about the
    # destination: a directory holding somebody else's data was not prepared
    # for a migration, and that stays true even when it also holds a generation
    # this tool published.
    if destination_refusals:
        return report(
            STATUS_DESTINATION_REFUSED,
            inventory=inventory,
            findings=destination_refusals,
            warnings=warnings,
        )

    if target.exists():
        verification = verify_generation(
            target, source_fingerprint=snapshot.inventory.source_fingerprint
        )
        published = {
            "generation": str(target),
            "destination_prestate": prestate,
            "published": True,
        }
        if verification.ok:
            return report(
                STATUS_ALREADY_PUBLISHED,
                inventory=inventory,
                generation=published,
                verification=verification.to_payload(),
                warnings=warnings,
            )
        findings = (
            [FINDING_DESTINATION_SOURCE_MISMATCH]
            if verification.generation_intact and verification.source_drift
            else [FINDING_DESTINATION_CHANGED, *verification.findings]
        )
        return report(
            STATUS_DESTINATION_REFUSED,
            inventory=inventory,
            generation=published,
            verification=verification.to_payload(),
            findings=findings,
            warnings=warnings,
        )

    if not snapshot.publishable:
        return report(
            STATUS_BLOCKED,
            inventory=inventory,
            findings=snapshot.inventory.findings,
            warnings=warnings,
        )

    bootstrap = plan_bootstrap(snapshot)
    bootstrap_payload = bootstrap.report.to_dict()
    warnings.extend(bootstrap.report.warnings)
    if not bootstrap.ok:
        return report(
            STATUS_BLOCKED,
            inventory=inventory,
            bootstrap=bootstrap_payload,
            findings=bootstrap.report.findings,
            warnings=warnings,
        )

    if dry_run:
        return report(
            STATUS_PLANNED,
            inventory=inventory,
            bootstrap=bootstrap_payload,
            generation={
                "generation": str(target),
                "destination_prestate": prestate,
                "published": False,
            },
            warnings=warnings,
        )

    return _stage_and_publish(
        snapshot,
        bootstrap,
        destination_path,
        target,
        prestate,
        report,
        inventory,
        bootstrap_payload,
        warnings,
    )


# ---------------------------------------------------------------------------
# Staging and publication
# ---------------------------------------------------------------------------

def _stage_and_publish(
    snapshot: SourceSnapshot,
    bootstrap: BootstrapPlan,
    destination: Path,
    target: Path,
    prestate: str,
    report: Callable[..., dict[str, Any]],
    inventory: Mapping[str, Any],
    bootstrap_payload: Mapping[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Build privately, verify, revalidate the source, publish, verify again."""
    destination.mkdir(parents=True, exist_ok=True)
    staging = new_staging_path(destination)

    def quarantined(status: str, findings: Sequence[str], **fields: Any):
        debris = quarantine_staging(staging, destination) if staging.exists() else None
        return report(
            status,
            inventory=inventory,
            bootstrap=bootstrap_payload,
            generation={
                "generation": str(target),
                "destination_prestate": prestate,
                "published": False,
                "staged_debris": None if debris is None else str(debris),
            },
            findings=findings,
            warnings=warnings,
            **fields,
        )

    try:
        build = build_generation(
            snapshot, bootstrap, staging, destination=destination, prestate=prestate
        )
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return quarantined(
            STATUS_BLOCKED,
            [f"{FINDING_STAGED_BUILD_FAILED}:{type(exc).__name__}"],
        )
    if not build.ok:
        return quarantined(STATUS_BLOCKED, build.findings)

    staged = verify_generation(staging)
    if not staged.generation_intact:
        return quarantined(
            STATUS_BLOCKED,
            [FINDING_STAGED_VERIFICATION_FAILED, *staged.findings],
            verification=staged.to_payload(),
        )

    # The publication window. A hook runs first so a test can make the source
    # move or the boundary fail; the frozen fingerprint is then revalidated
    # immediately before the rename, which is the only moment at which "the
    # source has not changed" is a statement about the publication itself.
    hook = BEFORE_PUBLISH_HOOK
    if hook is not None:
        try:
            hook(staging, target)
        except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
            return quarantined(
                STATUS_PUBLICATION_FAILED,
                [f"{FINDING_PUBLICATION_FAILED}:{type(exc).__name__}"],
            )

    current = read_source_snapshot(snapshot.source)
    if current.inventory.source_fingerprint != snapshot.inventory.source_fingerprint:
        return quarantined(STATUS_SOURCE_DRIFT, [FINDING_SOURCE_DRIFT])

    try:
        publish_generation(staging, target)
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return quarantined(
            STATUS_PUBLICATION_FAILED,
            [f"{FINDING_PUBLICATION_FAILED}:{type(exc).__name__}"],
        )

    final = verify_generation(
        target, source_fingerprint=snapshot.inventory.source_fingerprint
    )
    return report(
        STATUS_APPLIED if final.ok else STATUS_BLOCKED,
        inventory=inventory,
        bootstrap=bootstrap_payload,
        generation={
            "generation": str(target),
            "destination_prestate": prestate,
            "published": True,
            "staged_debris": None,
        },
        verification=final.to_payload(),
        findings=() if final.ok else final.findings,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify_migration(*, destination: Any, source: Any = None) -> dict[str, Any]:
    """Prove a published generation. Strictly read-only on both sides.

    With ``source`` supplied the frozen fingerprint is compared against the
    source as it is now, and drift is reported as :data:`STATUS_SOURCE_DRIFT`
    with ``generation_intact`` still stated separately: "your migration is
    corrupt" and "your source moved on afterwards" are different answers and
    must not share a status.
    """
    destination_path = _resolved(destination)
    source_path = None if source is None else _resolved(source)
    target = destination_path / GENERATION_DIR

    def report(status: str, **fields: Any) -> dict[str, Any]:
        return _report(
            "verify",
            status,
            source=source_path,
            destination=destination_path,
            dry_run=True,
            **fields,
        )

    refusals, warnings = destination_findings(destination_path)
    if refusals:
        return report(STATUS_VERIFY_FAILED, findings=refusals, warnings=warnings)

    fingerprint = None
    inventory: dict[str, Any] = {}
    if source_path is not None:
        overlap = _overlap_findings(source_path, destination_path)
        if overlap:
            return report(STATUS_VERIFY_FAILED, findings=overlap, warnings=warnings)
        snapshot = read_source_snapshot(source_path)
        inventory = snapshot.inventory.to_payload()
        fingerprint = snapshot.inventory.source_fingerprint
        warnings.extend(snapshot.inventory.warnings)

    verification = verify_generation(target, source_fingerprint=fingerprint)
    if verification.ok:
        status = STATUS_VERIFIED
    elif verification.generation_intact and verification.source_drift:
        status = STATUS_SOURCE_DRIFT
    else:
        status = STATUS_VERIFY_FAILED
    return report(
        status,
        inventory=inventory,
        generation={
            "generation": str(target),
            "published": target.exists(),
        },
        verification=verification.to_payload(),
        findings=verification.findings,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def rollback_migration(*, destination: Any) -> dict[str, Any]:
    """Withdraw an unchanged, never-served migration-owned generation.

    Four refusals before a single byte moves: an unknown destination entry, a
    generation that is not there (reported as nothing to do, not as a failure),
    a generation a runtime has been given, and one that no longer verifies —
    which means somebody changed it after the migration, and withdrawing it
    would be discarding their work rather than undoing ours.

    **The admission check is why this verb has no ``--pointer``.** A withdrawal
    is given one destination and nothing else; a missing pointer argument,
    or a pointer that happens to be elsewhere, proves nothing about whether a
    runtime is serving the target. What does is the destination's own admission
    ledger, written by an activation *before* it published its pointer — and
    this verb and that activation take the same generation guard, so a preflight
    here cannot race a registration there. A generation that was ever admitted
    is refused for good: not referenced by the current pointer, retained only
    for a restore, and no longer referenced at all are three situations a legacy
    withdrawal is not in a position to tell apart.

    The withdrawal itself is a rename into ``rollback/``. The journal, the
    graph, the readable files, the vector database, the frozen source backup and
    the manifest are all retained, so the rollback is itself reviewable and a
    repeated rollback is a no-op rather than a second removal. The recorded
    destination prestate is reported, so an operator can see whether this tool
    created the destination or found it.
    """
    destination_path = _resolved(destination)
    target = destination_path / GENERATION_DIR

    def report(status: str, **fields: Any) -> dict[str, Any]:
        return _report(
            "rollback",
            status,
            source=None,
            destination=destination_path,
            **fields,
        )

    refusals, warnings = destination_findings(destination_path)
    if refusals:
        return report(STATUS_ROLLBACK_REFUSED, findings=refusals, warnings=warnings)

    if not target.exists():
        return report(
            STATUS_ROLLBACK_NOTHING,
            generation={"generation": str(target), "published": False},
            warnings=warnings,
        )

    try:
        with control_lock(served_lock_path(destination_path), directory=True):
            return _withdraw(destination_path, target, report, warnings)
    except ControlLockError as exc:
        return report(
            STATUS_ROLLBACK_REFUSED,
            generation={"generation": str(target), "published": True},
            findings=[f"{FINDING_LOCK_UNAVAILABLE}:{exc.label}"],
            warnings=warnings,
        )


def _withdraw(
    destination_path: Path,
    target: Path,
    report: Callable[..., dict[str, Any]],
    warnings: Sequence[str],
) -> dict[str, Any]:
    """Check admission, verify, and rename — all under the generation guard.

    The whole check-then-rename, not just the rename: an admission registered
    between a preflight and a ``os.rename`` would be exactly the race the guard
    exists to remove, and the activation holding this same lock is what makes
    the two operations one serial sequence.
    """
    admission = admission_findings(destination_path, target)
    if admission:
        return report(
            STATUS_ROLLBACK_REFUSED,
            generation={
                "generation": str(target),
                "published": True,
                "admitted": FINDING_GENERATION_SERVED in admission,
            },
            findings=admission,
            warnings=warnings,
        )

    verification = verify_generation(target)
    if not verification.generation_intact:
        return report(
            STATUS_ROLLBACK_REFUSED,
            generation={"generation": str(target), "published": True},
            verification=verification.to_payload(),
            findings=verification.findings,
            warnings=warnings,
        )

    manifest, manifest_findings = read_manifest(target)
    prestate = str((manifest or {}).get("destination_prestate", ""))
    withdrawn = withdraw_generation(target, destination_path)
    return report(
        STATUS_ROLLED_BACK,
        generation={
            "generation": str(target),
            "published": False,
            "withdrawn_to": str(withdrawn),
            "destination_prestate": prestate,
            "retained_artifacts": verification.artifacts_checked,
            "admitted": False,
        },
        verification=verification.to_payload(),
        warnings=[*warnings, *manifest_findings],
    )


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

_EPILOG = (
    "Both paths are explicit and required: this tool never discovers a store, "
    "reads a configuration file or changes a running service. Omitting the verb "
    "runs a read-only plan. The report is JSON on stdout; exit 0 means the "
    "operation did what it says, 1 means it refused and nothing was published."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {TOOL_NAME}",
        description=(
            "Migrate an explicitly supplied old-format layered-memory snapshot "
            "into a verified graph-era generation."
        ),
        epilog=_EPILOG,
    )
    verbs = parser.add_subparsers(
        dest="command", metavar="{" + ",".join(COMMANDS) + "}"
    )

    plan = verbs.add_parser(
        "plan",
        help="read the source and report what a migration would do (default)",
        description="Inventory the source and the destination. Writes nothing.",
    )
    apply_verb = verbs.add_parser(
        "apply",
        help="build, verify and publish one migration generation",
        description=(
            "Build a private staged generation, verify it, revalidate the frozen "
            "source fingerprint and publish it with one atomic rename."
        ),
    )
    verify = verbs.add_parser(
        "verify",
        help="prove a published generation, read-only",
        description=(
            "Recompute the membership, hashes, journal replay, readable surfaces "
            "and vector seeds of a published generation."
        ),
    )
    rollback = verbs.add_parser(
        "rollback",
        help="withdraw an unchanged, never-served generation, retaining it",
        description=(
            "Rename a verified, unchanged migration-owned generation into "
            "rollback/. Refuses a changed or unknown target, and one a runtime "
            "has been given: there is no --pointer, because a missing argument "
            "cannot prove a generation is unreferenced. Deletes nothing."
        ),
    )

    for sub in (plan, apply_verb, verify, rollback):
        sub.add_argument(
            "--destination",
            metavar="PATH",
            required=True,
            help="the explicit migration destination directory",
        )
    for sub in (plan, apply_verb):
        sub.add_argument(
            "--source",
            metavar="PATH",
            required=True,
            help=(
                "the explicit old-format snapshot: knowledge/<domain>.md, "
                "data/vectors.db and an optional data/memory_changelog.jsonl"
            ),
        )
        sub.add_argument(
            "--identity-map",
            metavar="PATH",
            default=None,
            help=(
                "optional JSON object mapping a legacy vector row id to the L1 "
                "entry id it stands for; every entry is validated against the "
                "inventory, and nothing is ever inferred from content"
            ),
        )
    verify.add_argument(
        "--source",
        metavar="PATH",
        default=None,
        help="the source to check the frozen fingerprint against (optional)",
    )
    apply_verb.add_argument(
        "--dry-run",
        action="store_true",
        help="report what apply would do and write nothing",
    )

    # The runtime adapter contributes its own three verbs, with its own flags:
    # one definition of ``status``/``activate``/``restore``, reachable here and
    # from ``python -m layered_memory_mcp.graph_generation_runtime``.
    from .graph_generation_runtime import add_cutover_verbs

    add_cutover_verbs(verbs)
    return parser


def _normalize(argv: Sequence[str]) -> list[str]:
    """Insert the default ``plan`` verb when the first argument is an option.

    This is what makes a read-only dry run the default: an invocation that
    names only ``--source``/``--destination`` plans rather than erroring, and an
    invocation that starts with a verb is left exactly as given. ``--help`` is
    never rewritten, so the top-level help stays reachable.
    """
    arguments = list(argv)
    if arguments and not arguments[0].startswith("-"):
        # A verb, or something argparse should report as an unknown one.
        return arguments
    if any(item in ("-h", "--help") for item in arguments):
        return arguments
    return [COMMANDS[0], *arguments]


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and print its JSON report. Returns the exit code."""
    parser = _parser()
    arguments = parser.parse_args(_normalize(sys.argv[1:] if argv is None else argv))
    command = arguments.command or "plan"

    try:
        if command == "plan":
            report = plan_migration(
                source=arguments.source,
                destination=arguments.destination,
                identity_map=arguments.identity_map,
            )
        elif command == "apply":
            report = apply_migration(
                source=arguments.source,
                destination=arguments.destination,
                identity_map=arguments.identity_map,
                dry_run=bool(arguments.dry_run),
            )
        elif command == "verify":
            report = verify_migration(
                destination=arguments.destination, source=arguments.source
            )
        elif command == "rollback":
            report = rollback_migration(destination=arguments.destination)
        else:
            # A runtime verb. Its report carries its own ``exit_code``, decided
            # by its own success statuses: publishing a generation and serving
            # one are different operations and do not share a success set.
            from .graph_generation_runtime import cutover_command

            report = cutover_command(command, arguments)
            print(json.dumps(report, indent=2, sort_keys=True))
            return int(report["exit_code"])
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        print(f"graph_migration_failed:{type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILED

    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())

"""Fresh graph initialisation — an explicit operation, never an implicit one.

WHY THIS IS NOT MIGRATION
-------------------------
Changing the rollout default to ``graph`` creates a question migration cannot
answer: what should a service with *no* graph generation serve? The wrong
answer is "an empty one". An empty graph produced beside existing legacy
Markdown and vector rows would not be a fresh start, it would be a silent
amnesia — the memory is still on disk, and the service stops being able to see
it. ARCHITECTURE_v2.md therefore separates the two cases, and this module is
the explicit half:

.. code-block:: text

    python -m layered_memory_mcp.graph_bootstrap \\
        --home /srv/layered-memory \\
        --destination /srv/layered-memory/graph \\
        --pointer /srv/layered-memory/graph/active.json      # read-only plan

    python -m layered_memory_mcp.graph_bootstrap apply  --home … --destination … --pointer …
    python -m layered_memory_mcp.graph_bootstrap verify --destination …

Omitting the verb runs ``plan``, so a forgotten argument cannot initialise
anything.

THE FOUR REFUSALS
-----------------
1. **Legacy memory blocks a fresh start.** Any Markdown under the home's
   knowledge directory, any vector database, any L0 index or any graph/journal
   file beside them means this home already holds memory. The answer is
   migration, and this tool stops rather than publishing a generation that
   would hide it. The check reads names and types only — no memory body is
   opened and nothing is counted by parsing a document.
2. **Nothing is discovered.** ``--home``, ``--destination`` and ``--pointer``
   are required. No default home, no ``config.yaml`` lookup to find a store, no
   environment variable and no running service is consulted to choose a path.
3. **No history is invented.** The staged generation is built with the existing
   publishers and carries zero events, a zero head and zero checkpoints,
   declared as :data:`~layered_memory_mcp.graph_migration_generation.GENERATION_TYPE_FRESH`.
   No bootstrap event, source fingerprint, frozen backup or recorded timestamp
   is fabricated to make it resemble a migration.
4. **A preview writes nothing.** Not the destination, not the pointer, not the
   home's ``config.yaml``, not a lock.

WHAT APPLY ACTUALLY DOES, AND IN WHICH ORDER
--------------------------------------------
Build privately → verify there → publish with one rename → verify again →
activate the pointer → prepare the home's binding → prove the *effective*
service configuration selects this pointer. Each step is reported separately,
because they are separate files and there is no transaction spanning them. A
later step failing leaves the earlier ones reported as done, with the paths
needed to recover, rather than claimed as rolled back.

PRIVACY (§15)
-------------
The report carries the three operator-supplied paths, counts, relative paths
and stable refusal tokens. Never a memory body, a heading or an exception
message.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .graph_generation_runtime import (
    STATUS_CUTOVER_APPLIED,
    STATUS_CUTOVER_UNCHANGED,
    activate_generation,
    generation_is_served,
    status_generation,
)
from .graph_home_binding import (
    STATUS_APPLIED as BINDING_APPLIED,
    STATUS_PREPARED as BINDING_PREPARED,
    STATUS_UNCHANGED as BINDING_UNCHANGED,
    STATUS_VERIFIED as BINDING_VERIFIED,
    apply_home_binding,
    plan_home_binding,
    plan_home_config,
    verify_effective_binding,
)
from .graph_migration_generation import (
    GENERATION_DIR,
    GENERATION_TYPE_FRESH,
    build_fresh_generation,
    destination_findings,
    destination_prestate,
    new_staging_path,
    publish_generation,
    quarantine_staging,
    read_manifest,
    verify_generation,
)

__all__ = [
    "COMMANDS",
    "EXIT_BLOCKED",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "FINDING_LEGACY_PRESENT",
    "STATUS_APPLIED",
    "STATUS_BLOCKED",
    "STATUS_PLANNED",
    "STATUS_PUBLISHED_ALREADY",
    "STATUS_VERIFIED",
    "apply_bootstrap",
    "main",
    "plan_bootstrap_fresh",
    "verify_bootstrap",
]

TOOL_NAME = "layered_memory_mcp.graph_bootstrap"
REPORT_VERSION = 1

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_FAILED = 2
EXIT_USAGE = 3

STATUS_PLANNED = "fresh_bootstrap_planned"
STATUS_APPLIED = "fresh_bootstrap_applied"
STATUS_PUBLISHED_ALREADY = "fresh_bootstrap_already_published"
STATUS_VERIFIED = "fresh_bootstrap_verified"
STATUS_BLOCKED = "fresh_bootstrap_blocked"
STATUS_FAILED = "fresh_bootstrap_failed"

FINDING_LEGACY_PRESENT = "legacy_memory_present"
FINDING_HOME_ABSENT = "home_absent"
FINDING_HOME_NOT_A_DIRECTORY = "home_not_a_directory"
FINDING_HOME_SYMLINK = "home_symlink"
FINDING_PATH_OVERLAP = "path_overlap"
FINDING_PATH_SYMLINK = "path_symlink"
FINDING_GENERATION_NOT_FRESH = "published_generation_is_not_fresh"
FINDING_GENERATION_UNVERIFIED = "published_generation_unverified"
FINDING_BUILD_FAILED = "staged_build_failed"
FINDING_STAGED_UNVERIFIED = "staged_generation_unverified"
FINDING_PUBLICATION_FAILED = "publication_failed"
FINDING_ACTIVATION_FAILED = "activation_failed"
FINDING_BINDING_FAILED = "home_binding_failed"
FINDING_EFFECTIVE_FAILED = "effective_binding_failed"
FINDING_TARGET_MISMATCH = "target_mismatch"

_SUCCESS = frozenset(
    {STATUS_PLANNED, STATUS_APPLIED, STATUS_PUBLISHED_ALREADY, STATUS_VERIFIED}
)

#: Names under the home that mean "this home already holds memory". Each is
#: checked by shape alone, so nothing a user wrote is ever opened to decide it.
_LEGACY_FILES = ("L0.md", "graph.db", "journal.db")
_LEGACY_VECTOR = "data/vectors.db"
_LEGACY_KNOWLEDGE = "knowledge"


def _resolved(value: Any) -> Path:
    return Path(value).expanduser().absolute()


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _shape_findings(label: str, path: Path, *, directory: bool) -> list[str]:
    if path.is_symlink():
        return [f"{FINDING_PATH_SYMLINK}:{label}"]
    try:
        if str(path.parent.resolve()) != str(path.parent):
            return [f"{FINDING_PATH_SYMLINK}:{label}_ancestor"]
    except OSError:
        return [f"{FINDING_PATH_SYMLINK}:{label}_ancestor"]
    if directory and path.exists() and not path.is_dir():
        return [f"{FINDING_HOME_NOT_A_DIRECTORY}:{label}"]
    return []


def _legacy_findings(home: Path) -> list[str]:
    """Name every legacy input that makes this home unfit for a fresh start.

    Deliberately reports *all* of them rather than the first: an operator who
    has to choose between migration and bootstrap needs the whole picture, and
    a single finding would make a partial cleanup look like progress.
    """
    findings: list[str] = []
    for relative in (*_LEGACY_FILES, _LEGACY_VECTOR):
        candidate = home / relative
        if candidate.exists() or candidate.is_symlink():
            findings.append(f"{FINDING_LEGACY_PRESENT}:{relative}")
    knowledge = home / _LEGACY_KNOWLEDGE
    if knowledge.is_dir() and not knowledge.is_symlink():
        documents = sorted(
            str(path.relative_to(home)) for path in knowledge.rglob("*.md")
        )
        findings.extend(
            f"{FINDING_LEGACY_PRESENT}:{relative}" for relative in documents[:20]
        )
        if len(documents) > 20:
            findings.append(f"{FINDING_LEGACY_PRESENT}:{_LEGACY_KNOWLEDGE}:truncated")
    elif knowledge.is_symlink():
        findings.append(f"{FINDING_LEGACY_PRESENT}:{_LEGACY_KNOWLEDGE}")
    return findings


def _prove_existing(
    target: Path, pointer: Path
) -> tuple[dict[str, Any], list[str]]:
    """Prove an existing generation with the gate that applies to it.

    Two proofs, never one with a switch. A generation this pointer is already
    serving is *writable*: an accepted semantic write republishes L1, the vector
    rows and L0 inside it, so the publication-time hashes stop describing it the
    moment the store is used. Running those hashes as its health gate would
    report a correctly-written store as corrupt — and would make a repeat
    bootstrap fail precisely because the service had been doing its job. A
    generation nobody is serving has no attestation to lean on, so it is proved
    as a publication, in full.
    """
    served, runtime = generation_is_served(pointer=pointer, generation=target)
    if served:
        return (
            {
                "status": runtime.get("status"),
                "integrity": "attested_runtime_health",
                "publication_hashes_checked": False,
                "resolution": runtime.get("resolution") or {},
                "ok": True,
            },
            [],
        )
    verification = verify_generation(target)
    payload = verification.to_payload()
    payload["integrity"] = "publication"
    if not verification.ok:
        return payload, [FINDING_GENERATION_UNVERIFIED, *verification.findings]
    return payload, []


def _preview_binding(home: Path, pointer: Path) -> tuple[dict[str, Any], list[str]]:
    """Preview the home's binding, zero-write, with the pointer still absent.

    On a first bootstrap there is nothing to point at yet, so only the
    pointer-independent half of the binding can be decided. That half still
    *blocks*: a ``config.yaml`` this tool could never rewrite would otherwise
    only be discovered after the generation had been published.
    """
    if pointer.exists():
        report = plan_home_binding(home=home, pointer=pointer)
        payload = report.to_payload()
        if report.status not in {BINDING_PREPARED, BINDING_UNCHANGED}:
            return payload, [FINDING_BINDING_FAILED, *report.findings]
        return payload, []
    report = plan_home_config(home=home)
    payload = report.to_payload()
    payload["status"] = (
        report.status if report.findings else "home_binding_pending_activation"
    )
    if report.findings:
        return payload, [FINDING_BINDING_FAILED, *report.findings]
    return payload, []


def _report(
    command: str,
    status: str,
    *,
    home: Path | None,
    destination: Path | None,
    pointer: Path | None,
    dry_run: bool,
    generation: Mapping[str, Any] | None = None,
    verification: Mapping[str, Any] | None = None,
    activation: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    effective: Mapping[str, Any] | None = None,
    runtime: Mapping[str, Any] | None = None,
    findings: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": TOOL_NAME,
        "report_version": REPORT_VERSION,
        "command": command,
        "status": status,
        "generation_type": GENERATION_TYPE_FRESH,
        "dry_run": dry_run,
        "apply": not dry_run,
        "home": None if home is None else str(home),
        "destination": None if destination is None else str(destination),
        "pointer": None if pointer is None else str(pointer),
        "generation": dict(generation or {}),
        "verification": dict(verification or {}),
        "activation": dict(activation or {}),
        "binding": dict(binding or {}),
        "effective_binding": dict(effective or {}),
        "runtime": dict(runtime or {}),
        "findings": sorted(set(findings)),
        "warnings": sorted(set(warnings)),
    }
    payload["exit_code"] = EXIT_OK if status in _SUCCESS else EXIT_BLOCKED
    return payload


def _preflight(
    home: Path, destination: Path, pointer: Path
) -> list[str]:
    findings: list[str] = []
    findings.extend(_shape_findings("home", home, directory=True))
    findings.extend(_shape_findings("destination", destination, directory=True))
    findings.extend(_shape_findings("pointer", pointer, directory=False))
    control_root = pointer.parent
    if _overlap(home, destination):
        findings.append(f"{FINDING_PATH_OVERLAP}:home_destination")
    if _overlap(home, control_root):
        findings.append(f"{FINDING_PATH_OVERLAP}:home_control")
    if not home.exists():
        findings.append(FINDING_HOME_ABSENT)
    elif home.is_symlink():
        findings.append(FINDING_HOME_SYMLINK)
    elif not home.is_dir():
        findings.append(FINDING_HOME_NOT_A_DIRECTORY)
    return sorted(set(findings))


def _plan_or_apply(
    command: str, *, home: Any, destination: Any, pointer: Any, dry_run: bool
) -> dict[str, Any]:
    home_path = _resolved(home)
    destination_path = _resolved(destination)
    pointer_path = _resolved(pointer)
    target = destination_path / GENERATION_DIR

    def report(status: str, **fields: Any) -> dict[str, Any]:
        return _report(
            command, status, home=home_path, destination=destination_path,
            pointer=pointer_path, dry_run=dry_run, **fields,
        )

    findings = _preflight(home_path, destination_path, pointer_path)
    if findings:
        return report(STATUS_BLOCKED, findings=findings)

    legacy = _legacy_findings(home_path)
    if legacy:
        return report(STATUS_BLOCKED, findings=legacy)

    destination_refusals, destination_warnings = destination_findings(destination_path)
    if destination_refusals:
        return report(
            STATUS_BLOCKED, findings=destination_refusals, warnings=destination_warnings
        )
    prestate = destination_prestate(destination_path)
    warnings = list(destination_warnings)

    published = {
        "generation": str(target),
        "destination_prestate": prestate,
        "published": target.exists(),
        "generation_type": GENERATION_TYPE_FRESH,
    }

    if target.exists():
        # An existing generation is never rebuilt or replaced here. It is
        # re-proved, and it must be a *fresh* one: a migration generation at
        # this destination is somebody else's work and stays untouched.
        manifest, manifest_findings = read_manifest(target)
        declared = None if manifest is None else manifest.get("generation_type")
        if declared != GENERATION_TYPE_FRESH:
            return report(
                STATUS_BLOCKED,
                generation=published,
                findings=[FINDING_GENERATION_NOT_FRESH, *manifest_findings],
                warnings=warnings,
            )
        verification, verification_findings = _prove_existing(target, pointer_path)
        if verification_findings:
            return report(
                STATUS_BLOCKED,
                generation=published,
                verification=verification,
                findings=verification_findings,
                warnings=warnings,
            )
        if dry_run:
            binding, binding_findings = _preview_binding(home_path, pointer_path)
            return report(
                STATUS_BLOCKED if binding_findings else STATUS_PUBLISHED_ALREADY,
                generation=published,
                verification=verification.to_payload(),
                binding=binding,
                findings=binding_findings,
                warnings=warnings,
            )
        return _activate_and_bind(
            report, home_path, target, pointer_path, published,
            verification, warnings, STATUS_PUBLISHED_ALREADY,
        )

    binding, binding_findings = _preview_binding(home_path, pointer_path)
    if binding_findings:
        return report(
            STATUS_BLOCKED,
            generation=published,
            binding=binding,
            findings=binding_findings,
            warnings=warnings,
        )
    if dry_run:
        return report(
            STATUS_PLANNED,
            generation=published,
            binding=binding,
            warnings=warnings,
        )

    destination_path.mkdir(parents=True, exist_ok=True)
    staging = new_staging_path(destination_path)

    def quarantined(status: str, bad: Sequence[str], **fields: Any):
        debris = quarantine_staging(staging, destination_path) if staging.exists() else None
        return report(
            status,
            generation={**published, "staged_debris": None if debris is None else str(debris)},
            findings=bad,
            warnings=warnings,
            **fields,
        )

    try:
        build = build_fresh_generation(
            staging, destination=destination_path, prestate=prestate
        )
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return quarantined(STATUS_BLOCKED, [f"{FINDING_BUILD_FAILED}:{type(exc).__name__}"])
    if not build.ok:
        return quarantined(STATUS_BLOCKED, build.findings)

    staged = verify_generation(staging)
    if not staged.generation_intact:
        return quarantined(
            STATUS_BLOCKED,
            [FINDING_STAGED_UNVERIFIED, *staged.findings],
            verification=staged.to_payload(),
        )

    try:
        publish_generation(staging, target)
    except Exception as exc:  # noqa: BLE001 — named, never quoted (§15)
        return quarantined(
            STATUS_BLOCKED, [f"{FINDING_PUBLICATION_FAILED}:{type(exc).__name__}"]
        )

    published["published"] = True
    verification = verify_generation(target)
    if not verification.ok:
        return report(
            STATUS_BLOCKED,
            generation=published,
            verification=verification.to_payload(),
            findings=[FINDING_GENERATION_UNVERIFIED, *verification.findings],
            warnings=warnings,
        )
    return _activate_and_bind(
        report, home_path, target, pointer_path, published,
        verification.to_payload(), warnings, STATUS_APPLIED,
    )


def _activate_and_bind(
    report: Callable[..., dict[str, Any]],
    home: Path,
    target: Path,
    pointer: Path,
    published: Mapping[str, Any],
    verification: Mapping[str, Any],
    warnings: list[str],
    success: str,
) -> dict[str, Any]:
    """Activate the pointer, prepare the home, then prove the effective config.

    Three files, three separately reported outcomes. A failure after the
    publication is reported with the generation still named as published,
    because it is: pretending the publication was undone would send the
    operator looking for something that is still on disk.
    """
    activation = activate_generation(pointer=pointer, generation=target, apply=True)
    if activation.get("status") not in {STATUS_CUTOVER_APPLIED, STATUS_CUTOVER_UNCHANGED}:
        return report(
            STATUS_BLOCKED,
            generation=published,
            verification=verification,
            activation=activation,
            findings=[FINDING_ACTIVATION_FAILED, *activation.get("findings", [])],
            warnings=[*warnings, *activation.get("warnings", [])],
        )

    binding = apply_home_binding(home=home, pointer=pointer)
    if binding.status not in {BINDING_APPLIED, BINDING_UNCHANGED}:
        return report(
            STATUS_BLOCKED,
            generation=published,
            verification=verification,
            activation=activation,
            binding=binding.to_payload(),
            findings=[FINDING_BINDING_FAILED, *binding.findings],
            warnings=warnings,
        )

    effective = verify_effective_binding(home=home, pointer=pointer)
    runtime = status_generation(pointer=pointer)
    if effective.status != BINDING_VERIFIED:
        return report(
            STATUS_BLOCKED,
            generation=published,
            verification=verification,
            activation=activation,
            binding=binding.to_payload(),
            effective=effective.to_payload(),
            runtime=runtime,
            findings=[FINDING_EFFECTIVE_FAILED, *effective.findings],
            warnings=warnings,
        )
    return report(
        success,
        generation=published,
        verification=verification,
        activation=activation,
        binding=binding.to_payload(),
        effective=effective.to_payload(),
        runtime=runtime,
        warnings=[*warnings, *activation.get("warnings", [])],
    )


def plan_bootstrap_fresh(
    *, home: Any, destination: Any, pointer: Any
) -> dict[str, Any]:
    """Read-only preview. Creates nothing, anywhere."""
    return _plan_or_apply(
        "plan", home=home, destination=destination, pointer=pointer, dry_run=True
    )


def apply_bootstrap(*, home: Any, destination: Any, pointer: Any) -> dict[str, Any]:
    """Publish, activate and bind one fresh generation. The only writing verb."""
    return _plan_or_apply(
        "apply", home=home, destination=destination, pointer=pointer, dry_run=False
    )


def verify_bootstrap(
    *, destination: Any, home: Any = None, pointer: Any = None
) -> dict[str, Any]:
    """Re-prove a published fresh generation, and the binding if asked. Reads only."""
    destination_path = _resolved(destination)
    home_path = None if home is None else _resolved(home)
    pointer_path = None if pointer is None else _resolved(pointer)
    target = destination_path / GENERATION_DIR

    def report(status: str, **fields: Any) -> dict[str, Any]:
        return _report(
            "verify", status, home=home_path, destination=destination_path,
            pointer=pointer_path, dry_run=True, **fields,
        )

    manifest, manifest_findings = read_manifest(target)
    declared = None if manifest is None else manifest.get("generation_type")
    published = {
        "generation": str(target),
        "published": target.exists(),
        "generation_type": declared,
    }
    if declared != GENERATION_TYPE_FRESH:
        return report(
            STATUS_BLOCKED,
            generation=published,
            findings=[FINDING_GENERATION_NOT_FRESH, *manifest_findings],
        )
    # Publication integrity and attested runtime health are two claims, and a
    # served generation is writable — so which one is *required* depends on
    # whether this pointer is already serving it. Both are still reported.
    if pointer_path is None:
        # No pointer named, so there is no attestation to lean on: the only
        # claim available is publication integrity, and it is required.
        proof = verify_generation(target)
        verification = {**proof.to_payload(), "integrity": "publication"}
        findings = (
            [] if proof.ok else [FINDING_GENERATION_UNVERIFIED, *proof.findings]
        )
        runtime: dict[str, Any] = {}
    else:
        verification, findings = _prove_existing(target, pointer_path)
        runtime = status_generation(pointer=pointer_path)
    effective: dict[str, Any] = {}
    if pointer_path is not None and home_path is not None and not findings:
        proof = verify_effective_binding(home=home_path, pointer=pointer_path)
        effective = proof.to_payload()
        if proof.status != BINDING_VERIFIED:
            findings = [FINDING_EFFECTIVE_FAILED, *proof.findings]
    return report(
        STATUS_BLOCKED if findings else STATUS_VERIFIED,
        generation=published,
        verification=verification,
        effective=effective,
        runtime=runtime,
        findings=findings,
    )


COMMANDS = ("plan", "apply", "verify")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m layered_memory_mcp.graph_bootstrap",
        description=(
            "Initialise one explicitly supplied, legacy-free service home with a "
            "fresh graph generation. The default verb is a read-only plan."
        ),
    )
    parser.add_argument("command", nargs="?", default="plan", choices=COMMANDS)
    parser.add_argument("--home", metavar="PATH")
    parser.add_argument("--destination", required=True, metavar="PATH")
    parser.add_argument("--pointer", metavar="PATH")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
    required = {
        "plan": ("home", "pointer"),
        "apply": ("home", "pointer"),
        "verify": (),
    }[arguments.command]
    missing = [name for name in required if getattr(arguments, name) is None]
    if missing:
        print(
            json.dumps(
                {
                    "tool": TOOL_NAME,
                    "status": STATUS_BLOCKED,
                    "findings": sorted(f"argument_required:--{name}" for name in missing),
                    "exit_code": EXIT_USAGE,
                },
                indent=2, sort_keys=True,
            )
        )
        return EXIT_USAGE
    try:
        if arguments.command == "verify":
            report = verify_bootstrap(
                destination=arguments.destination,
                home=arguments.home,
                pointer=arguments.pointer,
            )
        elif arguments.command == "apply":
            report = apply_bootstrap(
                home=arguments.home,
                destination=arguments.destination,
                pointer=arguments.pointer,
            )
        else:
            report = plan_bootstrap_fresh(
                home=arguments.home,
                destination=arguments.destination,
                pointer=arguments.pointer,
            )
    except Exception as exc:  # noqa: BLE001 — bounded operator report (§15)
        report = _report(
            arguments.command, STATUS_FAILED,
            home=None if arguments.home is None else _resolved(arguments.home),
            destination=_resolved(arguments.destination),
            pointer=None if arguments.pointer is None else _resolved(arguments.pointer),
            dry_run=arguments.command != "apply",
            findings=[f"operation_failed:{type(exc).__name__}"],
        )
        report["exit_code"] = EXIT_FAILED
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

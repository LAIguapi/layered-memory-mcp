"""Explicit layered-memory graph update operator.

This module composes the existing migration and generation-runtime operators.
It owns no persistence format and never discovers a home, source, destination,
pointer, configuration file or service.  The default command is a read-only
preview; ``--apply`` is the only state-changing opt-in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .graph_generation_runtime import (
    FINDING_GENERATION_ROOT_REJECTED,
    STATUS_CUTOVER_REFUSED,
    STATUS_RUNTIME_ABSENT,
    activate_generation,
    status_generation,
)
from .graph_migration import (
    EXIT_BLOCKED,
    EXIT_FAILED,
    EXIT_OK,
    FINDING_DESTINATION_SOURCE_MISMATCH,
    GENERATION_DIR,
    STATUS_ALREADY_PUBLISHED,
    STATUS_APPLIED,
    STATUS_PLANNED,
    STATUS_VERIFIED,
    apply_migration,
    plan_migration,
    verify_migration,
)
from .graph_migration_generation import read_manifest
from .graph_migration_inventory import read_source_snapshot

TOOL_NAME = "layered_memory_mcp.graph_update"
REPORT_VERSION = 1

STATUS_PLANNED_UPDATE = "graph_update_planned"
STATUS_APPLIED_UPDATE = "graph_update_applied"
STATUS_UNCHANGED_UPDATE = "graph_update_unchanged"
STATUS_REFUSED_UPDATE = "graph_update_refused"
STATUS_FAILED_UPDATE = "graph_update_failed"

FINDING_PATH_OVERLAP = "path_overlap"
FINDING_HOME_OVERLAP = "home_path_overlap"
FINDING_SOURCE_PATH = "source_path_rejected"
FINDING_DESTINATION_PATH = "destination_path_rejected"
FINDING_HOME_PATH = "home_path_rejected"
FINDING_CONTROL_PATH = "control_root_rejected"
FINDING_SOURCE_PROVENANCE = "source_provenance_unproved"
FINDING_TARGET_MISMATCH = "target_mismatch"

_SUCCESS_STATUSES = frozenset(
    {STATUS_PLANNED_UPDATE, STATUS_APPLIED_UPDATE, STATUS_UNCHANGED_UPDATE}
)


def _path(value: Any) -> Path:
    absolute = os.path.abspath(os.path.expanduser(os.fspath(value)))
    return Path(os.path.normcase(os.path.normpath(absolute)))


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _ancestor_symlink(path: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(path.parent)) != os.path.normcase(
            str(path.parent)
        )
    except OSError:
        return True


def _path_shape(path: Path, finding: str, *, require_directory: bool = False) -> list[str]:
    if path.is_symlink():
        return [f"{finding}:symlink"]
    if _ancestor_symlink(path):
        return [f"{finding}:ancestor_symlink"]
    if require_directory and path.exists() and not path.is_dir():
        return [f"{finding}:not_a_directory"]
    return []


def _report(
    *,
    status: str,
    dry_run: bool,
    home: Path,
    source: Path,
    destination: Path,
    pointer: Path,
    migration: Mapping[str, Any] | None = None,
    verification: Mapping[str, Any] | None = None,
    activation: Mapping[str, Any] | None = None,
    status_preflight: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    findings: Sequence[str] = (),
    warnings: Sequence[str] = (),
    error: str | None = None,
    exit_code: int | None = None,
) -> dict[str, Any]:
    control_root = pointer.parent
    generation = destination / GENERATION_DIR
    payload: dict[str, Any] = {
        "tool": TOOL_NAME,
        "report_version": REPORT_VERSION,
        "status": status,
        "dry_run": dry_run,
        "apply": not dry_run,
        "home": str(home),
        "source": str(source),
        "destination": str(destination),
        "pointer": str(pointer),
        "control_root": str(control_root),
        "generation": str(generation),
        "migration": dict(migration or {}),
        "verification": dict(verification or {}),
        "activation": dict(activation or {}),
        "binding": dict(binding or {}),
        "status_preflight": dict(status_preflight or {}),
        "preflight": {
            "binding": dict(binding or {}),
            "status": dict(status_preflight or {}),
            "activation": dict(activation or {}),
        },
        "findings": sorted(set(findings)),
        "warnings": sorted(set(warnings)),
    }
    if error is not None:
        payload["error"] = error
    payload["exit_code"] = (
        int(exit_code)
        if exit_code is not None
        else EXIT_OK
        if status in _SUCCESS_STATUSES
        else EXIT_BLOCKED
    )
    return payload


def _binding_preflight(
    *, home: Path, source: Path, destination: Path, pointer: Path
) -> tuple[dict[str, Any], list[str]]:
    control_root = pointer.parent
    findings: list[str] = []
    findings.extend(_path_shape(source, FINDING_SOURCE_PATH))
    findings.extend(_path_shape(destination, FINDING_DESTINATION_PATH))
    findings.extend(_path_shape(home, FINDING_HOME_PATH, require_directory=True))
    findings.extend(_path_shape(control_root, FINDING_CONTROL_PATH, require_directory=True))

    if _overlap(source, destination):
        findings.append(f"{FINDING_PATH_OVERLAP}:source_destination")
    if _overlap(source, control_root):
        findings.append(f"{FINDING_PATH_OVERLAP}:source_control")
    if _overlap(source, home):
        findings.append(f"{FINDING_HOME_OVERLAP}:source")
    if _overlap(destination, home):
        findings.append(f"{FINDING_HOME_OVERLAP}:destination")
    if _overlap(control_root, home):
        findings.append(f"{FINDING_HOME_OVERLAP}:control")

    binding = {
        "status": "binding_refused" if findings else "binding_preflighted",
        "home": str(home),
        "source": str(source),
        "destination": str(destination),
        "pointer": str(pointer),
        "control_root": str(control_root),
        "generation": str(destination / GENERATION_DIR),
        "explicit": True,
        "writes": False,
    }
    return binding, sorted(set(findings))


def _activation_preflight(
    *, pointer: Path, generation: Path
) -> tuple[dict[str, Any], list[str]]:
    activation = activate_generation(pointer=pointer, generation=generation, apply=False)
    findings = list(activation.get("findings", []))
    if activation.get("status") == STATUS_CUTOVER_REFUSED:
        absent = f"{FINDING_GENERATION_ROOT_REJECTED}:absent"
        findings = [finding for finding in findings if finding != absent]
    return activation, sorted(set(findings))


def _status_preflight(pointer: Path) -> tuple[dict[str, Any], list[str]]:
    report = status_generation(pointer=pointer)
    if report.get("status") == STATUS_RUNTIME_ABSENT:
        return report, []
    return report, list(report.get("findings", []))


def _same_target_is_served(pointer: Path, generation: Path) -> bool:
    report = status_generation(pointer=pointer)
    if report.get("status") not in {
        "generation_runtime_active",
        "generation_runtime_rollback_active",
    }:
        return False
    resolution = report.get("resolution") or {}
    relative = str(resolution.get("relative_generation") or "")
    if not relative:
        return False
    return _path(pointer.parent / Path(relative)) == generation


def _source_matches_target(source: Path, generation: Path) -> tuple[bool, list[str]]:
    manifest, manifest_findings = read_manifest(generation)
    if manifest is None:
        return False, [*manifest_findings, FINDING_SOURCE_PROVENANCE]
    try:
        snapshot = read_source_snapshot(source)
    except Exception as exc:  # noqa: BLE001 - report only the exception class
        return False, [f"{FINDING_SOURCE_PROVENANCE}:{type(exc).__name__}"]
    expected = manifest.get("source_fingerprint")
    actual = snapshot.inventory.source_fingerprint
    if not snapshot.publishable:
        return False, [FINDING_SOURCE_PROVENANCE, *snapshot.inventory.findings]
    if not expected:
        return False, [FINDING_SOURCE_PROVENANCE]
    if expected != actual:
        return False, [FINDING_DESTINATION_SOURCE_MISMATCH, FINDING_SOURCE_PROVENANCE]
    return True, []


def _preflight(
    *, home: Path, source: Path, destination: Path, pointer: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    binding, findings = _binding_preflight(
        home=home, source=source, destination=destination, pointer=pointer
    )
    if findings:
        return binding, {}, {}, findings

    status, status_findings = _status_preflight(pointer)
    activation, activation_findings = _activation_preflight(
        pointer=pointer, generation=destination / GENERATION_DIR
    )
    return (
        binding,
        status,
        activation,
        sorted(set([*status_findings, *activation_findings])),
    )


def run_update(
    *, home: Any, source: Any, destination: Any, pointer: Any, apply: bool = False
) -> dict[str, Any]:
    home_path = _path(home)
    source_path = _path(source)
    destination_path = _path(destination)
    pointer_path = _path(pointer)
    generation_path = destination_path / GENERATION_DIR

    binding, status_preflight, activation_preflight, findings = _preflight(
        home=home_path,
        source=source_path,
        destination=destination_path,
        pointer=pointer_path,
    )
    if findings:
        return _report(
            status=STATUS_REFUSED_UPDATE,
            dry_run=not apply,
            home=home_path,
            source=source_path,
            destination=destination_path,
            pointer=pointer_path,
            activation=activation_preflight,
            status_preflight=status_preflight,
            binding=binding,
            findings=findings,
        )

    if generation_path.exists() and _same_target_is_served(pointer_path, generation_path):
        matches, provenance_findings = _source_matches_target(source_path, generation_path)
        if not matches:
            return _report(
                status=STATUS_REFUSED_UPDATE,
                dry_run=not apply,
                home=home_path,
                source=source_path,
                destination=destination_path,
                pointer=pointer_path,
                activation=activation_preflight,
                status_preflight=status_preflight,
                binding=binding,
                findings=provenance_findings,
            )
        unchanged = dict(activation_preflight)
        unchanged["status"] = "generation_cutover_unchanged"
        unchanged["dry_run"] = not apply
        return _report(
            status=STATUS_UNCHANGED_UPDATE,
            dry_run=not apply,
            home=home_path,
            source=source_path,
            destination=destination_path,
            pointer=pointer_path,
            activation=unchanged,
            status_preflight=status_preflight,
            binding=binding,
        )

    migration = plan_migration(source=source_path, destination=destination_path)
    if migration.get("status") not in {STATUS_PLANNED, STATUS_ALREADY_PUBLISHED}:
        return _report(
            status=STATUS_REFUSED_UPDATE,
            dry_run=not apply,
            home=home_path,
            source=source_path,
            destination=destination_path,
            pointer=pointer_path,
            migration=migration,
            activation=activation_preflight,
            status_preflight=status_preflight,
            binding=binding,
            findings=migration.get("findings", []),
            warnings=migration.get("warnings", []),
        )

    if not apply:
        return _report(
            status=STATUS_PLANNED_UPDATE,
            dry_run=True,
            home=home_path,
            source=source_path,
            destination=destination_path,
            pointer=pointer_path,
            migration=migration,
            activation=activation_preflight,
            status_preflight=status_preflight,
            binding=binding,
            warnings=migration.get("warnings", []),
        )

    if migration.get("status") == STATUS_PLANNED:
        migration = apply_migration(source=source_path, destination=destination_path)
    if migration.get("status") not in {STATUS_APPLIED, STATUS_ALREADY_PUBLISHED}:
        return _report(
            status=STATUS_REFUSED_UPDATE,
            dry_run=False,
            home=home_path,
            source=source_path,
            destination=destination_path,
            pointer=pointer_path,
            migration=migration,
            activation=activation_preflight,
            status_preflight=status_preflight,
            binding=binding,
            findings=migration.get("findings", []),
            warnings=migration.get("warnings", []),
        )

    verification = verify_migration(destination=destination_path, source=source_path)
    if verification.get("status") != STATUS_VERIFIED:
        return _report(
            status=STATUS_REFUSED_UPDATE,
            dry_run=False,
            home=home_path,
            source=source_path,
            destination=destination_path,
            pointer=pointer_path,
            migration=migration,
            verification=verification,
            activation=activation_preflight,
            status_preflight=status_preflight,
            binding=binding,
            findings=verification.get("findings", []),
            warnings=[*migration.get("warnings", []), *verification.get("warnings", [])],
        )

    activation = activate_generation(
        pointer=pointer_path, generation=generation_path, apply=True
    )
    if activation.get("status") not in {
        "generation_cutover_applied",
        "generation_cutover_unchanged",
    }:
        return _report(
            status=STATUS_REFUSED_UPDATE,
            dry_run=False,
            home=home_path,
            source=source_path,
            destination=destination_path,
            pointer=pointer_path,
            migration=migration,
            verification=verification,
            activation=activation,
            status_preflight=status_preflight,
            binding=binding,
            findings=activation.get("findings", []),
            warnings=[
                *migration.get("warnings", []),
                *verification.get("warnings", []),
                *activation.get("warnings", []),
            ],
        )

    readback = status_generation(pointer=pointer_path)
    resolution = readback.get("resolution") or {}
    actual_generation = _path(pointer_path.parent / Path(str(resolution.get("relative_generation") or "")))
    if readback.get("status") not in {
        "generation_runtime_active",
        "generation_runtime_rollback_active",
    } or actual_generation != generation_path:
        return _report(
            status=STATUS_REFUSED_UPDATE,
            dry_run=False,
            home=home_path,
            source=source_path,
            destination=destination_path,
            pointer=pointer_path,
            migration=migration,
            verification=verification,
            activation=activation,
            status_preflight=readback,
            binding=binding,
            findings=[FINDING_TARGET_MISMATCH, *readback.get("findings", [])],
            warnings=[
                *migration.get("warnings", []),
                *verification.get("warnings", []),
                *activation.get("warnings", []),
                *readback.get("warnings", []),
            ],
        )

    return _report(
        status=STATUS_APPLIED_UPDATE,
        dry_run=False,
        home=home_path,
        source=source_path,
        destination=destination_path,
        pointer=pointer_path,
        migration=migration,
        verification=verification,
        activation=activation,
        status_preflight=readback,
        binding=binding,
        warnings=[
            *migration.get("warnings", []),
            *verification.get("warnings", []),
            *activation.get("warnings", []),
            *readback.get("warnings", []),
        ],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="layered-memory-update",
        description=(
            "Preview or explicitly apply one graph migration and generation "
            "activation using only supplied paths."
        ),
    )
    parser.add_argument("--home", required=True, metavar="PATH")
    parser.add_argument("--source", required=True, metavar="PATH")
    parser.add_argument("--destination", required=True, metavar="PATH")
    parser.add_argument("--pointer", required=True, metavar="PATH")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="publish and activate; default is a read-only preview",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = run_update(
            home=arguments.home,
            source=arguments.source,
            destination=arguments.destination,
            pointer=arguments.pointer,
            apply=bool(arguments.apply),
        )
    except Exception as exc:  # noqa: BLE001 - bounded operator report
        report = _report(
            status=STATUS_FAILED_UPDATE,
            dry_run=not bool(arguments.apply),
            home=_path(arguments.home),
            source=_path(arguments.source),
            destination=_path(arguments.destination),
            pointer=_path(arguments.pointer),
            findings=[f"operation_failed:{type(exc).__name__}"],
            error=type(exc).__name__,
            exit_code=EXIT_FAILED,
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

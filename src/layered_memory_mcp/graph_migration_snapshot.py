"""Snapshot only explicit inputs, without opening SQLite or discovering a home.

Only observational stability is checked. Even matching hashes and metadata do
not prove transactional or cross-file consistency, exclude intervening writes,
or establish production readiness. No migration semantics are validated here.

The path contract rejects symlinks, noncanonical parent components and aliases.
Linux no-atime descriptor reads preserve source access times as well as bytes.
Callers must control the explicit parent namespace throughout the operation;
ancestor identities are rechecked, but there is no service/write barrier.

Coverage is decided by one policy (``_ROOT_POLICY``/``_DATA_POLICY``/
``_policy``). Unrecognised members block; they are never ignored. Two bounded
allowances exist, both explicit and both recorded: a knowledge namespace
subdirectory that is *proved* empty, and a root member the operator declares
per run. Neither opens, reads or traverses anything it covers.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


REPORT_VERSION = 1
_CHUNK_SIZE = 1 << 20

#: Every root member this adapter recognises, as ``name -> (class, kind)``.
#:
#: ``directory`` members are traversed, ``selected``/``evidence`` members are
#: copied, and ``excluded`` members are classified from their metadata alone —
#: never opened, read or traversed. A member that is not listed here is a
#: coverage blocker, because "unrecognised" and "unwanted" are not the same
#: claim: silently ignoring unknown roots is how a knowledge namespace would
#: get dropped. Operator-owned roots that this project does not produce are
#: declared per run instead (see :func:`_validate_exclusions`), so no
#: installation-specific name has to live in this source file.
_ROOT_POLICY = {
    # Migration inputs.
    "knowledge": ("directory", "directory"),
    "data": ("directory", "directory"),
    "L0.md": ("evidence", "file"),
    # Configuration, credentials, model cache, repository and backup metadata.
    "config.yaml": ("excluded", "file"),
    ".env": ("excluded", "file"),
    "models": ("excluded", "directory"),
    ".git": ("excluded", "directory"),
    ".gitignore": ("excluded", "file"),
    "backups": ("excluded", "directory"),
    # Maintenance state this project writes under the home itself: cadence
    # markers and the write guard's state are operational, not memory.
    ".last_auto_compact": ("excluded", "file"),
    ".last_auto_compact_attempt": ("excluded", "file"),
    ".last_l1_dedup": ("excluded", "file"),
    "write_guard_state.json": ("excluded", "file"),
}
#: Root members an operator may never redeclare: the three migration inputs
#: carry memory, and the rest already have a typed verdict above.
_RESERVED_EXCLUSIONS = frozenset(_ROOT_POLICY)
_DATA_POLICY = {
    "vectors.db": ("selected", "file"),
    "memory_changelog.jsonl": ("selected", "file"),
    "backups": ("excluded", "directory"),
    "sem_thresholds.json": ("excluded", "file"),
    "vector_maintenance.json": ("excluded", "file"),
    "vector_maintenance.lock": ("excluded", "file"),
    **{
        database + suffix: ("excluded", "file")
        for database in ("access_log.db", "review_queue.db", "todos.db")
        for suffix in ("", "-wal", "-shm", "-journal")
    },
    **{
        "vectors.db-" + suffix: ("sidecar", "file")
        for suffix in ("wal", "shm", "journal")
    },
}


class _Refused(Exception):
    def __init__(self, reason: str, relative: str | None = None):
        self.finding = reason if relative is None else f"{reason}:{relative}"
        super().__init__(self.finding)


@dataclass
class _Scan:
    inventory: list[dict[str, Any]]
    states: dict[str, tuple[int, ...]]
    selected: list[str]
    findings: list[str]
    warnings: list[str]
    empty_namespaces: list[str]
    observed_exclusions: list[str]


def _validate_exclusions(values: Sequence[str] | None) -> tuple[str, ...]:
    """Accept only explicit, single-component, non-reserved root member names.

    This is the escape hatch for a root member that belongs to the operator
    rather than to this project — and it is deliberately a narrow one. There
    is no pattern, no recursion and no "ignore what you do not know": each
    name is one literal root member, and :func:`_scan_source` additionally
    refuses a declared name that is not actually present, so a stale or
    speculative list cannot quietly cover a future unknown member.
    """
    exclusions: list[str] = []
    for value in values or ():
        if not isinstance(value, str):
            raise _Refused("operator_exclusion_invalid")
        if value != value.strip() or not value:
            raise _Refused("operator_exclusion_invalid")
        if "/" in value or os.sep in value or "\x00" in value or value in {".", ".."}:
            raise _Refused("operator_exclusion_invalid", value)
        if any(character in value for character in "*?[]"):
            raise _Refused("operator_exclusion_pattern", value)
        if value in _RESERVED_EXCLUSIONS:
            raise _Refused("operator_exclusion_reserved", value)
        if value in exclusions:
            raise _Refused("operator_exclusion_duplicate", value)
        exclusions.append(value)
    return tuple(sorted(exclusions))


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode, metadata.st_dev, metadata.st_ino, metadata.st_size,
        metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_nlink,
    )


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _read_flags(*, directory: bool = False) -> int:
    required = ("O_NOFOLLOW", "O_NOATIME", "O_DIRECTORY")
    if any(not hasattr(os, name) for name in required):
        raise _Refused("safe_reads_unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK
    return flags | os.O_DIRECTORY if directory else flags


def _absolute(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if ".." in path.parts or "\x00" in str(path):
        raise _Refused("path_noncanonical")
    return path if path.is_absolute() else Path.cwd() / path


def _open_directory(path: Path, stack: ExitStack, role: str):
    descriptor = os.open(path.anchor, _read_flags(directory=True))
    stack.callback(os.close, descriptor)
    chain = []
    for component in path.parts[1:]:
        try:
            metadata = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            raise _Refused(f"{role}_missing") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise _Refused(f"{role}_symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise _Refused(f"{role}_not_directory")
        child = os.open(component, _read_flags(directory=True), dir_fd=descriptor)
        stack.callback(os.close, child)
        if _identity(os.fstat(child)) != _identity(metadata):
            raise _Refused("ancestor_changed")
        chain.append((descriptor, component, _identity(metadata)))
        descriptor = child
    return descriptor, chain


def _check_ancestors(chain) -> None:
    for parent, name, identity in chain:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or _identity(metadata) != identity:
            raise _Refused("ancestor_changed")


def _check_target(parent: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    reason = (
        "destination_symlink" if stat.S_ISLNK(metadata.st_mode)
        else "destination_exists"
    )
    raise _Refused(reason)


def _policy(
    parent: str, name: str, kind: str, exclusions: tuple[str, ...] = (),
) -> tuple[str, str]:
    """One verdict per member: the only place a classification is decided."""
    if parent == ".":
        if name in _ROOT_POLICY:
            return _ROOT_POLICY[name]
        if name in exclusions:
            # The declared kind is whatever is there: an operator declaration
            # covers a member, it does not launder a symlink or special file,
            # both of which are refused before this verdict is consulted.
            return "operator_excluded", kind
        return "blocked", kind
    if parent == "data":
        return _DATA_POLICY.get(name, ("blocked", kind))
    if parent == "knowledge":
        # Suffix before shape, so a *directory* named like a memory document
        # stays a wrong-type refusal instead of passing as a namespace.
        if name.endswith(".md"):
            return "selected", "file"
        if name.endswith(".lock"):
            return "excluded", "file"
        if kind == "directory":
            # A namespace subdirectory is not copied by this adapter. It is
            # accepted only once it has been *proved* empty, so a namespace
            # holding memory stays a coverage blocker instead of a silent drop.
            return "namespace", "directory"
        return "blocked", kind
    return "blocked", kind


def _namespace_is_empty(parent: int, name: str, expected) -> bool:
    """List one knowledge namespace's member names; never open a member.

    Emptiness cannot be read out of metadata: a directory's link count only
    counts subdirectories, so a namespace holding nothing but Markdown would
    look identical to an empty one. Listing names is therefore the smallest
    check that can prove the claim, and it still reads no payload.
    """
    with ExitStack() as stack:
        descriptor = os.open(name, _read_flags(directory=True), dir_fd=parent)
        stack.callback(os.close, descriptor)
        if _signature(os.fstat(descriptor)) != expected:
            raise _Refused("source_drift", name)
        with os.scandir(descriptor) as entries:
            empty = next(iter(entries), None) is None
        if _signature(os.fstat(descriptor)) != expected:
            raise _Refused("source_drift", name)
    return empty


def _scan_source(root: int, exclusions: tuple[str, ...] = ()) -> _Scan:
    inventory = []
    states = {}
    selected = []
    findings = []
    warnings = []
    empty_namespaces = []
    observed_exclusions = []
    with ExitStack() as stack:
        pending = [(".", root)]
        directories = []
        while pending:
            relative, descriptor = pending.pop()
            before = _signature(os.fstat(descriptor))
            if relative in states and states[relative] != before:
                raise _Refused("source_drift", relative)
            states[relative] = before
            directories.append((relative, descriptor))
            with os.scandir(descriptor) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                record = name if relative == "." else f"{relative}/{name}"
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                states[record] = _signature(metadata)
                if stat.S_ISLNK(metadata.st_mode):
                    kind = "symlink"
                elif stat.S_ISDIR(metadata.st_mode):
                    kind = "directory"
                elif stat.S_ISREG(metadata.st_mode):
                    kind = "file"
                else:
                    kind = "special"
                classification, expected = _policy(relative, name, kind, exclusions)
                reason = None
                if kind == "symlink":
                    reason = "source_symlink"
                elif kind == "special":
                    reason = "source_special_file"
                elif classification == "blocked":
                    reason = "source_unknown_member"
                elif kind != expected:
                    reason = "source_wrong_type"
                elif classification == "namespace":
                    if _namespace_is_empty(descriptor, name, states[record]):
                        classification = "empty_namespace"
                        empty_namespaces.append(record)
                        warnings.append("empty_knowledge_namespace")
                    else:
                        reason = "knowledge_directory_uncovered"
                elif classification == "operator_excluded":
                    observed_exclusions.append(name)
                elif classification == "sidecar":
                    suffix = name.rsplit("-", 1)[1]
                    if suffix in {"wal", "journal"} and metadata.st_size:
                        reason = f"vector_{suffix}_not_empty"
                    else:
                        warnings.append(f"vector_{suffix}_not_copied")
                if reason is not None:
                    classification = "blocked"
                    findings.append(f"{reason}:{record}")
                inventory.append({
                    "path": record, "kind": kind,
                    "classification": classification,
                    "reason": reason or classification,
                })
                if classification == "directory":
                    child = os.open(
                        name, _read_flags(directory=True), dir_fd=descriptor
                    )
                    stack.callback(os.close, child)
                    if _signature(os.fstat(child)) != states[record]:
                        raise _Refused("source_drift", record)
                    pending.append((record, child))
                elif classification in {"selected", "evidence"}:
                    selected.append(record)
            if _signature(os.fstat(descriptor)) != before:
                raise _Refused("source_drift", relative)
        for relative, descriptor in directories:
            if _signature(os.fstat(descriptor)) != states[relative]:
                raise _Refused("source_drift", relative)
    for required in ("knowledge", "data"):
        if required not in states:
            findings.append(f"source_required_directory_missing:{required}")
    if "data/vectors.db" not in selected:
        findings.append("vector_db_missing:data/vectors.db")
    if not any(name.startswith("knowledge/") for name in selected):
        findings.append("source_no_markdown:knowledge")
    if "L0.md" not in selected:
        warnings.append("l0_absent")
    if "data/memory_changelog.jsonl" not in selected:
        warnings.append("changelog_absent")
    for declared in exclusions:
        if declared not in observed_exclusions:
            # A declaration that matches nothing is refused rather than kept
            # "just in case": otherwise one stale run's list would silently
            # pre-authorise a member nobody has classified yet.
            findings.append(f"operator_exclusion_absent:{declared}")
    return _Scan(
        sorted(inventory, key=lambda item: item["path"]), states,
        sorted(selected), sorted(findings), sorted(warnings),
        sorted(empty_namespaces), sorted(observed_exclusions),
    )


def _open_relative(root: int, relative: str, stack: ExitStack) -> int:
    parts = relative.split("/")
    parent = root
    for name in parts[:-1]:
        parent = os.open(name, _read_flags(directory=True), dir_fd=parent)
        stack.callback(os.close, parent)
    descriptor = os.open(parts[-1], _read_flags(), dir_fd=parent)
    stack.callback(os.close, descriptor)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise _Refused("file_not_regular", relative)
    return descriptor


def _digest_file(root: int, relative: str, expected=None, sink: int | None = None):
    with ExitStack() as stack:
        descriptor = _open_relative(root, relative, stack)
        before = _signature(os.fstat(descriptor))
        if expected is not None and before != expected:
            raise _Refused("source_drift", relative)
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, _CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
            if sink is not None:
                _write_all(sink, chunk)
        if _signature(os.fstat(descriptor)) != before or size != before[3]:
            raise _Refused("file_changed", relative)
    return size, digest.hexdigest()


def _observe_source(
    root: int, report: dict[str, Any], exclusions: tuple[str, ...] = (),
):
    scan = _scan_source(root, exclusions)
    report["inventory"] = scan.inventory
    report["warnings"] = sorted({*report["warnings"], *scan.warnings})
    report["empty_namespaces"] = scan.empty_namespaces
    if scan.findings:
        report["findings"].extend(scan.findings)
        raise _Refused("source_coverage_blocked")
    records = []
    for relative in scan.selected:
        size, digest = _digest_file(root, relative, scan.states[relative])
        records.append({
            "path": relative,
            "bundle_path": (
                "evidence/L0.md" if relative == "L0.md" else f"source/{relative}"
            ),
            "size": size,
            "st_mtime_ns": scan.states[relative][4],
            "sha256": digest,
        })
    after = _scan_source(root, exclusions)
    if after.states != scan.states:
        raise _Refused("source_drift")
    manifest = {
        "manifest_version": 1,
        "consistency": "observed_stable",
        "production_consistency_proven": False,
        # Coverage evidence travels with the bundle: an accepted snapshot
        # records which namespaces were proved empty and which root members the
        # operator declared, so neither reads later as an unexplained absence.
        "empty_namespaces": scan.empty_namespaces,
        "operator_exclusions": list(exclusions),
        "files": records,
    }
    return manifest, scan.states


def _report(
    source, destination, dry_run: bool, exclusions: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "report_version": REPORT_VERSION,
        "status": "refused", "dry_run": dry_run,
        "source": os.fspath(source), "destination": os.fspath(destination),
        "migration_source": None,
        "consistency": "not_checked", "production_consistency_proven": False,
        "manifest": None, "inventory": [], "findings": [],
        "warnings": ["observational_only", "production_readiness_not_assessed"],
        "empty_namespaces": [], "operator_exclusions": list(exclusions),
        "staging": None, "exit_code": 2,
    }


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if not written:
            raise _Refused("copy_write_failed")
        remaining = remaining[written:]


def _new_file(root: int, relative: str, stack: ExitStack) -> int:
    parts = relative.split("/")
    parent = root
    for name in parts[:-1]:
        parent = os.open(name, _read_flags(directory=True), dir_fd=parent)
        stack.callback(os.close, parent)
    descriptor = os.open(
        parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600, dir_fd=parent,
    )
    stack.callback(os.close, descriptor)
    return descriptor


def _copy_file(root: int, staging: int, record: dict[str, Any], expected) -> None:
    with ExitStack() as stack:
        sink = _new_file(staging, record["bundle_path"], stack)
        size, digest = _digest_file(root, record["path"], expected, sink)
        if (size, digest) != (record["size"], record["sha256"]):
            raise _Refused("source_drift", record["path"])
        os.fsync(sink)


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _bundle_state(root: int, expected_files: set[str]):
    expected_members: dict[str, set[str]] = {".": set()}
    for relative in sorted(expected_files):
        parent = "."
        for component in relative.split("/")[:-1]:
            expected_members.setdefault(parent, set()).add(component)
            parent = component if parent == "." else f"{parent}/{component}"
            expected_members.setdefault(parent, set())
        expected_members[parent].add(relative.rsplit("/", 1)[-1])
    states = {}
    with ExitStack() as stack:
        pending = [(".", root)]
        directories = []
        while pending:
            relative, descriptor = pending.pop()
            before = _signature(os.fstat(descriptor))
            if relative in states and states[relative] != before:
                raise _Refused("copy_verification_failed", relative)
            states[relative] = before
            directories.append((relative, descriptor))
            with os.scandir(descriptor) as entries:
                names = {entry.name for entry in entries}
            if names != expected_members[relative]:
                raise _Refused("copy_membership_changed", relative)
            for name in sorted(names):
                record = name if relative == "." else f"{relative}/{name}"
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                states[record] = _signature(metadata)
                if record in expected_members:
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise _Refused("copy_verification_failed", record)
                    child = os.open(
                        name, _read_flags(directory=True), dir_fd=descriptor
                    )
                    stack.callback(os.close, child)
                    pending.append((record, child))
                elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise _Refused("copy_verification_failed", record)
            if before != _signature(os.fstat(descriptor)):
                raise _Refused("copy_membership_changed", relative)
        for relative, descriptor in directories:
            if _signature(os.fstat(descriptor)) != states[relative]:
                raise _Refused("copy_membership_changed", relative)
    return states


def _verify_bundle(staging: int, manifest: dict[str, Any]) -> None:
    records = {
        record["bundle_path"]: (record["size"], record["sha256"])
        for record in manifest["files"]
    }
    manifest_payload = _manifest_bytes(manifest)
    records["snapshot-manifest.json"] = (
        len(manifest_payload), hashlib.sha256(manifest_payload).hexdigest(),
    )
    before = _bundle_state(staging, set(records))
    for relative, expected in sorted(records.items()):
        if _digest_file(staging, relative, before[relative]) != expected:
            raise _Refused("copy_verification_failed", relative)
    if _bundle_state(staging, set(records)) != before:
        raise _Refused("copy_membership_changed")


def _rename_function():
    """Require kernel no-replace rename; a check followed by rename is unsafe."""
    if not sys.platform.startswith("linux"):
        raise _Refused("atomic_noreplace_unavailable")
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    if function is None:
        raise _Refused("atomic_noreplace_unavailable")
    function.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function


def _rename_noreplace(parent: int, staging_name: str, target_name: str) -> None:
    function = _rename_function()
    if function(parent, os.fsencode(staging_name), parent, os.fsencode(target_name), 1):
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise _Refused("destination_raced")
        if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise _Refused("atomic_noreplace_unavailable")
        raise _Refused("publication_failed")


def _snapshot(
    *, source, destination, dry_run: bool, exclude: Sequence[str] | None = None,
) -> dict[str, Any]:
    report = _report(source, destination, dry_run, exclude or ())
    phase = "inspection"
    try:
        exclusions = _validate_exclusions(exclude)
        report["operator_exclusions"] = list(exclusions)
        source_path = _absolute(source)
        destination_path = _absolute(destination)
        report.update(
            source=str(source_path), destination=str(destination_path),
            migration_source=str(destination_path / "source"),
        )
        if (
            source_path.is_relative_to(destination_path)
            or destination_path.is_relative_to(source_path)
        ):
            raise _Refused("paths_overlap")
        with ExitStack() as stack:
            root, source_chain = _open_directory(source_path, stack, "source")
            parent, destination_chain = _open_directory(
                destination_path.parent, stack, "destination_parent"
            )
            _check_target(parent, destination_path.name)
            manifest, states = _observe_source(root, report, exclusions)
            source_directories = {
                (metadata[1], metadata[2]) for metadata in states.values()
                if stat.S_ISDIR(metadata[0])
            }
            if any(
                identity in source_directories
                for _, _, identity in destination_chain
            ):
                raise _Refused("paths_aliased")
            _check_ancestors(source_chain + destination_chain)
            _check_target(parent, destination_path.name)
            report.update(manifest=manifest, consistency="observed_stable")
            if dry_run:
                report.update(status="preview", exit_code=0)
                return report
            _rename_function()
            phase = "staging"
            staging_name = f".snapshot-stage-{secrets.token_hex(12)}"
            os.mkdir(staging_name, mode=0o700, dir_fd=parent)
            report["staging"] = str(destination_path.parent / staging_name)
            staging = os.open(staging_name, _read_flags(directory=True), dir_fd=parent)
            stack.callback(os.close, staging)
            staging_identity = _identity(os.fstat(staging))
            directories = ["source", "source/knowledge", "source/data"]
            if any(record["path"] == "L0.md" for record in manifest["files"]):
                directories.append("evidence")
            for relative in directories:
                parts = relative.split("/")
                directory_parent = staging
                if len(parts) > 1:
                    directory_parent = os.open(
                        parts[0], _read_flags(directory=True), dir_fd=staging
                    )
                    stack.callback(os.close, directory_parent)
                os.mkdir(parts[-1], mode=0o700, dir_fd=directory_parent)
            phase = "copy"
            for record in manifest["files"]:
                _copy_file(root, staging, record, states[record["path"]])
            with ExitStack() as manifest_stack:
                descriptor = _new_file(
                    staging, "snapshot-manifest.json", manifest_stack
                )
                _write_all(descriptor, _manifest_bytes(manifest))
                os.fsync(descriptor)
            phase = "verification"
            _verify_bundle(staging, manifest)
            final_manifest, final_states = _observe_source(root, report, exclusions)
            if (final_manifest, final_states) != (manifest, states):
                raise _Refused("source_drift")
            _verify_bundle(staging, manifest)
            if _scan_source(root, exclusions).states != states:
                raise _Refused("source_drift")
            _check_ancestors(source_chain + destination_chain)
            _check_target(parent, destination_path.name)
            metadata = os.stat(staging_name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or _identity(metadata) != staging_identity
            ):
                raise _Refused("staging_identity_changed")
            phase = "publication"
            _rename_noreplace(parent, staging_name, destination_path.name)
            report.update(status="snapshot_created", staging=None, exit_code=0)
    except _Refused as exc:
        report["findings"].append(exc.finding)
    except (OSError, ValueError):
        report["findings"].append(f"{phase}_failed")
    if report["findings"]:
        report["consistency"] = "not_verified"
        if phase != "inspection":
            report.update(status="failed", exit_code=1)
    if report["staging"] is not None:
        report["warnings"].append("staging_retained")
    report["findings"] = sorted(set(report["findings"]))
    report["warnings"] = sorted(set(report["warnings"]))
    return report


def plan_snapshot(
    *, source: str | os.PathLike[str], destination: str | os.PathLike[str],
    exclude: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Read-only preview; the destination parent must already exist.

    ``exclude`` names root members this project does not own, one literal
    member per entry. Every name must be present in the source; nothing is
    ignored implicitly and no subtree of an excluded member is ever read.
    """
    return _snapshot(
        source=source, destination=destination, dry_run=True, exclude=exclude,
    )


def apply_snapshot(
    *, source: str | os.PathLike[str], destination: str | os.PathLike[str],
    dry_run: bool = False, exclude: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Explicitly publish a new private bundle, retaining staging on failures.

    Requires Linux O_NOATIME permission and renameat2(RENAME_NOREPLACE) support.
    No fallback may modify the source or replace an existing target. The caller
    must keep the explicit source/parent namespace under its control. Failures
    never recursively delete anything; retained staging is not an accepted
    snapshot, even if it contains a manifest.
    """
    return _snapshot(
        source=source, destination=destination, dry_run=dry_run, exclude=exclude,
    )


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _Refused("cli_arguments_invalid")


def main(argv: Sequence[str] | None = None) -> int:
    """Print a bounded JSON report; only --apply permits filesystem writes."""
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="ROOT_MEMBER",
        help=(
            "one literal root member this project does not own; repeatable. "
            "A name that is reserved, patterned or absent is refused."
        ),
    )
    try:
        arguments = parser.parse_args(argv)
        report = _snapshot(
            source=arguments.source, destination=arguments.destination,
            dry_run=not arguments.apply, exclude=arguments.exclude,
        )
    except _Refused as exc:
        report = _report("", "", True)
        report["findings"] = [exc.finding]
    print(json.dumps(report, sort_keys=True, indent=2))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

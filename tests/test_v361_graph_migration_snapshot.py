"""Isolated, neutral-fixture checks for the explicit snapshot adapter."""

import builtins
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest


def test_snapshot_module_exists():
    assert importlib.util.find_spec(
        "layered_memory_mcp.graph_migration_snapshot"
    ) is not None


@pytest.fixture
def adapter():
    from layered_memory_mcp import graph_migration_snapshot

    return graph_migration_snapshot


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "input"
    (root / "knowledge").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "knowledge" / "alpha.md").write_bytes(b"## Item\r\n\r\nneutral-body\r\n")
    (root / "data" / "vectors.db").write_bytes(b"\x00neutral-vector-bytes\xff")
    (root / "data" / "memory_changelog.jsonl").write_bytes(b'{"item":"neutral"}\n')
    (root / "L0.md").write_bytes(b"neutral-evidence\n")
    return root


def tree_state(root):
    """Observe fixture metadata and bytes without changing access times."""
    result = {}
    pending = [(root, ".")]
    while pending:
        path, relative = pending.pop()
        metadata = path.lstat()
        payload = None
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK
        if stat.S_ISDIR(metadata.st_mode):
            descriptor = os.open(path, flags | os.O_DIRECTORY)
            try:
                with os.scandir(descriptor) as entries:
                    pending.extend(
                        (path / entry.name, f"{relative}/{entry.name}")
                        for entry in entries
                    )
            finally:
                os.close(descriptor)
        elif stat.S_ISREG(metadata.st_mode):
            descriptor = os.open(path, flags)
            try:
                chunks = []
                while chunk := os.read(descriptor, 65536):
                    chunks.append(chunk)
                payload = b"".join(chunks)
            finally:
                os.close(descriptor)
        result[relative] = (
            metadata.st_mode, metadata.st_dev, metadata.st_ino,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
            metadata.st_atime_ns, payload,
        )
    return result


def preview(adapter, source, tmp_path):
    planner = getattr(adapter, "plan_snapshot", None)
    assert callable(planner), "plain preview API must exist"
    return planner(source=source, destination=tmp_path / "bundle")


def test_preview_is_deterministic_and_whole_tree_read_only(adapter, source, tmp_path):
    before = tree_state(tmp_path)
    first = preview(adapter, source, tmp_path)
    second = preview(adapter, source, tmp_path)
    assert first == second
    assert first["report_version"] == 1
    assert first["status"] == "preview"
    assert first["dry_run"] is True
    assert first["exit_code"] == 0
    assert first["consistency"] == "observed_stable"
    assert first["production_consistency_proven"] is False
    assert first["migration_source"] == str(tmp_path / "bundle" / "source")
    assert "observational_only" in first["warnings"]
    assert tree_state(tmp_path) == before
    records = first["manifest"]["files"]
    assert [record["path"] for record in records] == [
        "L0.md", "data/memory_changelog.jsonl", "data/vectors.db", "knowledge/alpha.md"
    ]
    for record in records:
        metadata = (source / record["path"]).stat()
        assert record["size"] == metadata.st_size
        assert record["st_mtime_ns"] == metadata.st_mtime_ns
        assert len(record["sha256"]) == 64
    evidence = next(record for record in records if record["path"] == "L0.md")
    assert evidence["bundle_path"] == "evidence/L0.md"
    encoded = json.dumps(first)
    assert "neutral-body" not in encoded
    assert "neutral-vector-bytes" not in encoded
    assert "neutral-evidence" not in encoded
    assert "## Item" not in encoded


@pytest.mark.parametrize("relative", ["knowledge", "knowledge/alpha.md", "data", "data/vectors.db"])
def test_missing_inputs_refused_without_writes(adapter, source, tmp_path, relative):
    path = source / relative
    if path.is_dir():
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
    else:
        path.unlink()
    before = tree_state(tmp_path)
    report = preview(adapter, source, tmp_path)
    assert report["status"] == "refused"
    assert report["exit_code"] == 2
    assert report["findings"]
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("relative", [
    "unknown", "unknown.db", "data/unknown.db", "knowledge/nested",
    "knowledge/unknown.bin",
])
def test_unknown_members_are_visible_coverage_blockers(adapter, source, tmp_path, relative):
    path = source / relative
    if path.suffix:
        path.write_bytes(b"neutral-unrecognized")
    else:
        # Directories carry a member: a *nonempty* unrecognised directory is
        # the case that must never be silently dropped. Proved-empty knowledge
        # namespaces are a separate, explicitly reported outcome — see
        # ``test_an_empty_knowledge_namespace_is_reported_not_blocked``.
        path.mkdir()
        (path / "unread.md").write_bytes(b"neutral-unrecognized")
    before = tree_state(tmp_path)
    report = preview(adapter, source, tmp_path)
    assert report["status"] == "refused"
    assert any(relative in finding for finding in report["findings"])
    assert any(
        item["path"] == relative and item["classification"] == "blocked"
        for item in report["inventory"]
    )
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("suffix", ["wal", "journal"])
def test_nonempty_sqlite_sidecars_refused_without_reading(
    adapter, source, tmp_path, monkeypatch, suffix,
):
    name = f"vectors.db-{suffix}"
    (source / "data" / name).write_bytes(b"neutral-sidecar")
    original = os.open

    def guarded_open(path, *args, **kwargs):
        assert Path(path).name != name, "sidecar content must not be read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    report = preview(adapter, source, tmp_path)
    assert report["status"] == "refused"
    assert f"vector_{suffix}_not_empty:data/{name}" in report["findings"]


def test_exclusions_are_classified_but_never_opened(adapter, source, tmp_path, monkeypatch):
    files = [
        "config.yaml", ".env", "data/access_log.db", "data/review_queue.db",
        "data/todos.db", "data/access_log.db-wal", "data/review_queue.db-shm",
        "data/todos.db-journal", "data/sem_thresholds.json",
        "data/vector_maintenance.json", "data/vector_maintenance.lock",
        "knowledge/alpha.md.lock", "data/vectors.db-wal", "data/vectors.db-journal",
        "data/vectors.db-shm",
        # Operational state this project writes under the home: a real store
        # has these, and refusing them made legitimate sources unsnapshottable.
        ".gitignore", ".last_auto_compact", ".last_auto_compact_attempt",
        ".last_l1_dedup", "write_guard_state.json",
    ]
    directories = ["models", ".git", "backups", "data/backups"]
    for relative in files:
        empty = relative.endswith(("vectors.db-wal", "vectors.db-journal"))
        (source / relative).write_bytes(b"" if empty else b"neutral-excluded")
    for relative in directories:
        (source / relative).mkdir()
        (source / relative / "unread").write_bytes(b"neutral-excluded")
    before = tree_state(tmp_path)
    forbidden_names = {Path(relative).name for relative in files + directories}
    original = os.open
    hits = []

    def guarded_open(path, *args, **kwargs):
        name = Path(path).name
        assert name not in forbidden_names, "excluded entry opened"
        hits.append(name)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", guarded_open)
        report = preview(adapter, source, tmp_path)
    assert hits
    assert report["status"] == "preview"
    assert "vector_shm_not_copied" in report["warnings"]
    classified = {item["path"]: item["classification"] for item in report["inventory"]}
    for relative in files + directories:
        assert classified[relative] in {"excluded", "sidecar"}
    assert "neutral-excluded" not in json.dumps(report)
    assert tree_state(tmp_path) == before


def test_an_operator_declared_root_member_is_covered_but_never_opened(
    adapter, source, tmp_path, monkeypatch,
):
    """Positive control for the explicit, per-run root exclusion."""
    (source / "operator-owned.json").write_bytes(b"neutral-operator-owned")
    (source / "operator-archive").mkdir()
    (source / "operator-archive" / "unread.md").write_bytes(b"neutral-operator-owned")
    declared = ["operator-archive", "operator-owned.json"]
    archive_inode = (source / "operator-archive").stat().st_ino
    before = tree_state(tmp_path)
    real_open = os.open
    real_scan = os.scandir

    def guarded_open(path, *args, **kwargs):
        if not isinstance(path, int):
            assert Path(path).name not in {
                "operator-owned.json", "operator-archive", "unread.md",
            }, "declared member opened"
        return real_open(path, *args, **kwargs)

    def guarded_scan(path):
        if isinstance(path, int):
            assert os.fstat(path).st_ino != archive_inode, "declared subtree traversed"
        return real_scan(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", guarded_open)
        patch.setattr(os, "scandir", guarded_scan)
        report = adapter.plan_snapshot(
            source=source, destination=tmp_path / "bundle", exclude=list(reversed(declared)),
        )
    assert report["status"] == "preview"
    assert report["operator_exclusions"] == declared
    assert report["manifest"]["operator_exclusions"] == declared
    classified = {item["path"]: item["classification"] for item in report["inventory"]}
    assert classified["operator-owned.json"] == "operator_excluded"
    assert classified["operator-archive"] == "operator_excluded"
    assert "neutral-operator-owned" not in json.dumps(report)
    assert [record["path"] for record in report["manifest"]["files"]] == [
        "L0.md", "data/memory_changelog.jsonl", "data/vectors.db", "knowledge/alpha.md",
    ]
    assert tree_state(tmp_path) == before
    applied = adapter.apply_snapshot(
        source=source, destination=tmp_path / "bundle", exclude=declared,
    )
    assert applied["status"] == "snapshot_created"
    assert applied["manifest"] == report["manifest"]
    assert sorted(path.name for path in (tmp_path / "bundle" / "source").iterdir()) == [
        "data", "knowledge",
    ]


def test_an_undeclared_root_member_still_blocks_even_beside_a_declared_one(
    adapter, source, tmp_path,
):
    """Negative control: a declaration covers one name, never 'the unknown'."""
    (source / "operator-owned.json").write_bytes(b"neutral-operator-owned")
    (source / "second-unknown.json").write_bytes(b"neutral-second")
    before = tree_state(tmp_path)
    report = adapter.plan_snapshot(
        source=source, destination=tmp_path / "bundle",
        exclude=["operator-owned.json"],
    )
    assert report["status"] == "refused"
    assert report["findings"] == [
        "source_coverage_blocked", "source_unknown_member:second-unknown.json",
    ]
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("declared,finding", [
    (["knowledge"], "operator_exclusion_reserved:knowledge"),
    (["data"], "operator_exclusion_reserved:data"),
    (["L0.md"], "operator_exclusion_reserved:L0.md"),
    ([".env"], "operator_exclusion_reserved:.env"),
    (["config.yaml"], "operator_exclusion_reserved:config.yaml"),
    (["*"], "operator_exclusion_pattern:*"),
    (["*.json"], "operator_exclusion_pattern:*.json"),
    (["draft?"], "operator_exclusion_pattern:draft?"),
    (["knowledge/alpha.md"], "operator_exclusion_invalid:knowledge/alpha.md"),
    (["."], "operator_exclusion_invalid:."),
    ([".."], "operator_exclusion_invalid:.."),
    (["../escape"], "operator_exclusion_invalid:../escape"),
    ([""], "operator_exclusion_invalid"),
    ([" spaced"], "operator_exclusion_invalid"),
    ([None], "operator_exclusion_invalid"),
    ([7], "operator_exclusion_invalid"),
    (["dup", "dup"], "operator_exclusion_duplicate:dup"),
    (["never-present"], "operator_exclusion_absent:never-present"),
])
def test_invalid_operator_exclusions_are_refused_without_writes(
    adapter, source, tmp_path, declared, finding,
):
    before = tree_state(tmp_path)
    report = adapter.apply_snapshot(
        source=source, destination=tmp_path / "bundle", exclude=declared,
    )
    assert report["status"] == "refused"
    assert finding in report["findings"]
    assert not (tmp_path / "bundle").exists()
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("kind", ["symlink", "special"])
def test_a_declaration_cannot_launder_a_symlink_or_special_member(
    adapter, source, tmp_path, kind,
):
    path = source / "operator-owned"
    if kind == "symlink":
        path.symlink_to(tmp_path / "absent")
        expected = "source_symlink:operator-owned"
    else:
        os.mkfifo(path)
        expected = "source_special_file:operator-owned"
    before = tree_state(tmp_path)
    report = adapter.apply_snapshot(
        source=source, destination=tmp_path / "bundle", exclude=["operator-owned"],
    )
    assert report["status"] == "refused"
    assert expected in report["findings"]
    assert tree_state(tmp_path) == before


def test_a_declaration_does_not_reach_into_knowledge_or_data(adapter, source, tmp_path):
    """Root-member scope only: a nested unknown is not covered by a root name."""
    (source / "data" / "operator-owned.json").write_bytes(b"neutral")
    (source / "knowledge" / "operator-owned.bin").write_bytes(b"neutral")
    (source / "operator-owned.json").write_bytes(b"neutral")
    report = adapter.plan_snapshot(
        source=source, destination=tmp_path / "bundle",
        exclude=["operator-owned.json", "operator-owned.bin"],
    )
    assert report["status"] == "refused"
    assert "source_unknown_member:data/operator-owned.json" in report["findings"]
    assert "source_unknown_member:knowledge/operator-owned.bin" in report["findings"]
    assert "operator_exclusion_absent:operator-owned.bin" in report["findings"]


@pytest.mark.parametrize("relative", [
    "knowledge/alpha.md", "knowledge", "data", "L0.md", "models", "data/vectors.db",
])
def test_symlink_members_are_rejected(adapter, source, tmp_path, relative):
    path = source / relative
    if path.exists():
        path.rename(source / "neutral-moved")
    path.symlink_to(tmp_path / "absent")
    before = tree_state(tmp_path)
    report = preview(adapter, source, tmp_path)
    assert report["status"] == "refused"
    assert f"source_symlink:{relative}" in report["findings"]
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("relative", ["knowledge/pipe.md", "data/vectors.db", "config.yaml"])
def test_special_members_are_rejected(adapter, source, tmp_path, relative):
    path = source / relative
    if path.exists():
        path.unlink()
    os.mkfifo(path)
    before = tree_state(tmp_path)
    report = preview(adapter, source, tmp_path)
    assert report["status"] == "refused"
    assert f"source_special_file:{relative}" in report["findings"]
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("kind", [
    "source_root", "source_ancestor", "destination_ancestor", "destination_target",
    "overlap_same", "overlap_child", "overlap_parent", "existing",
    "missing_parent", "dotdot_alias",
])
def test_path_contract_refuses_without_writes(adapter, source, tmp_path, kind):
    destination = tmp_path / "bundle"
    chosen_source = source
    if kind == "source_root":
        chosen_source = tmp_path / "source-link"
        chosen_source.symlink_to(source, target_is_directory=True)
    elif kind == "source_ancestor":
        parent = tmp_path / "parent-link"
        parent.symlink_to(tmp_path, target_is_directory=True)
        chosen_source = parent / source.name
    elif kind == "destination_ancestor":
        parent = tmp_path / "parent-link"
        parent.symlink_to(tmp_path, target_is_directory=True)
        destination = parent / "bundle"
    elif kind == "destination_target":
        destination.symlink_to(tmp_path / "absent")
    elif kind == "overlap_same":
        destination = source
    elif kind == "overlap_child":
        destination = source / "bundle"
    elif kind == "overlap_parent":
        destination = tmp_path
    elif kind == "existing":
        destination.mkdir()
    elif kind == "missing_parent":
        destination = tmp_path / "absent" / "bundle"
    elif kind == "dotdot_alias":
        destination = source / ".." / "bundle"
    before = tree_state(tmp_path)
    planner = getattr(adapter, "plan_snapshot", None)
    assert callable(planner)
    report = planner(source=chosen_source, destination=destination)
    assert report["status"] == "refused"
    assert report["findings"]
    assert tree_state(tmp_path) == before


def apply(adapter, source, destination, **kwargs):
    writer = getattr(adapter, "apply_snapshot", None)
    assert callable(writer), "explicit apply API must exist"
    return writer(source=source, destination=destination, **kwargs)


def test_apply_preserves_payloads_and_separates_evidence(adapter, source, tmp_path):
    before = tree_state(source)
    planned = preview(adapter, source, tmp_path)
    destination = tmp_path / "bundle"
    report = apply(adapter, source, destination)
    assert report["status"] == "snapshot_created"
    assert report["dry_run"] is False
    assert report["exit_code"] == 0
    assert report["staging"] is None
    assert report["manifest"] == planned["manifest"]
    assert report["consistency"] == "observed_stable"
    assert report["production_consistency_proven"] is False
    assert tree_state(source) == before
    assert set(path.name for path in destination.iterdir()) == {
        "source", "evidence", "snapshot-manifest.json",
    }
    assert set(path.name for path in (destination / "source").iterdir()) == {"knowledge", "data"}
    assert not (destination / "source" / "L0.md").exists()
    for record in report["manifest"]["files"]:
        copied = (destination / record["bundle_path"]).read_bytes()
        assert copied == before[f'./{record["path"]}'][-1]
        assert hashlib.sha256(copied).hexdigest() == record["sha256"]
    manifest = json.loads((destination / "snapshot-manifest.json").read_text())
    assert manifest == report["manifest"]
    for path in [destination, *destination.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_optional_inputs_absent_and_explicit_apply_preview(adapter, source, tmp_path):
    (source / "L0.md").unlink()
    (source / "data" / "memory_changelog.jsonl").unlink()
    before = tree_state(tmp_path)
    destination = tmp_path / "bundle"
    report = apply(adapter, source, destination, dry_run=True)
    assert report["status"] == "preview"
    assert tree_state(tmp_path) == before
    report = apply(adapter, source, destination)
    assert report["status"] == "snapshot_created"
    assert not (destination / "evidence").exists()
    assert [record["path"] for record in report["manifest"]["files"]] == [
        "data/vectors.db", "knowledge/alpha.md",
    ]


def test_repeat_apply_refuses_without_any_writes(adapter, source, tmp_path):
    destination = tmp_path / "bundle"
    assert apply(adapter, source, destination)["status"] == "snapshot_created"
    before = tree_state(tmp_path)
    report = apply(adapter, source, destination)
    assert report["status"] == "refused"
    assert report["staging"] is None
    assert "destination_exists" in report["findings"]
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("kind", [
    "missing_input", "missing_parent", "unknown", "nonempty_wal", "nonempty_journal",
    "nested_knowledge", "symlink", "special", "overlap", "existing_target",
])
def test_apply_validates_before_creating_anything(adapter, source, tmp_path, kind):
    destination = tmp_path / "bundle"
    if kind == "missing_input":
        (source / "data" / "vectors.db").unlink()
    elif kind == "missing_parent":
        destination = tmp_path / "absent" / "bundle"
    elif kind == "unknown":
        (source / "data" / "unknown.db").write_bytes(b"neutral")
    elif kind.startswith("nonempty_"):
        (source / "data" / f'vectors.db-{kind.removeprefix("nonempty_")}').write_bytes(b"neutral")
    elif kind == "nested_knowledge":
        (source / "knowledge" / "nested").mkdir()
        (source / "knowledge" / "nested" / "held.md").write_bytes(b"neutral-held")
    elif kind == "symlink":
        (source / "models").symlink_to(tmp_path / "absent")
    elif kind == "special":
        os.mkfifo(source / "config.yaml")
    elif kind == "overlap":
        destination = source / "bundle"
    elif kind == "existing_target":
        destination.mkdir()
    before = tree_state(tmp_path)
    report = apply(adapter, source, destination)
    assert report["status"] == "refused"
    assert report["staging"] is None
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("kind", [
    "content", "same_size_restored_mtime", "membership_add", "membership_remove",
    "empty_directory", "file_mtime", "directory_mtime", "wal", "journal", "shm",
    "excluded_membership",
])
def test_concurrent_source_drift_retains_staging(adapter, source, tmp_path, monkeypatch, kind):
    assert callable(getattr(adapter, "apply_snapshot", None))
    original = adapter._copy_file
    hits = []
    expected_source = []
    destination = tmp_path / "bundle"

    def drifting_copy(*args, **kwargs):
        result = original(*args, **kwargs)
        hits.append(1)
        if len(hits) == 1:
            path = source / "knowledge" / "alpha.md"
            if kind in {"content", "same_size_restored_mtime"}:
                metadata = path.stat()
                path.write_bytes(b"changed" if kind == "content" else b"x" * metadata.st_size)
                if kind == "same_size_restored_mtime":
                    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            elif kind == "membership_add":
                (source / "knowledge" / "beta.md").write_bytes(b"neutral-new")
            elif kind == "membership_remove":
                path.unlink()
            elif kind == "empty_directory":
                (source / "knowledge" / "nested").mkdir()
            elif kind in {"file_mtime", "directory_mtime"}:
                target = path if kind == "file_mtime" else source / "data"
                metadata = target.stat()
                os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1))
            elif kind in {"wal", "journal", "shm"}:
                (source / "data" / f"vectors.db-{kind}").write_bytes(b"neutral-sidecar")
            elif kind == "excluded_membership":
                (source / "config.yaml").write_bytes(b"neutral-excluded")
            expected_source.append(tree_state(source))
        return result

    monkeypatch.setattr(adapter, "_copy_file", drifting_copy)
    report = apply(adapter, source, destination)
    assert hits
    assert report["status"] == "failed"
    assert report["exit_code"] == 1
    assert not destination.exists()
    assert Path(report["staging"]).is_dir()
    assert "staging_retained" in report["warnings"]
    assert tree_state(source) == expected_source[0]


def test_tampered_copied_bytes_never_publish(adapter, source, tmp_path, monkeypatch):
    assert callable(getattr(adapter, "apply_snapshot", None))
    original = adapter._copy_file
    hits = []
    before = tree_state(source)

    def tamper_copy(root, staging, record, expected):
        original(root, staging, record, expected)
        hits.append(1)
        if len(hits) == 1:
            descriptor = os.open(record["bundle_path"], os.O_WRONLY | os.O_NOFOLLOW, dir_fd=staging)
            try:
                os.write(descriptor, b"x")
            finally:
                os.close(descriptor)

    monkeypatch.setattr(adapter, "_copy_file", tamper_copy)
    report = apply(adapter, source, tmp_path / "bundle")
    assert len(hits) == 4
    assert report["status"] == "failed"
    assert any(finding.startswith("copy_verification_failed") for finding in report["findings"])
    assert Path(report["staging"]).is_dir()
    assert not (tmp_path / "bundle").exists()
    assert tree_state(source) == before


@pytest.mark.parametrize("seam", ["_copy_file", "_rename_noreplace"])
def test_operation_errors_are_bounded_and_retain_owned_staging(
    adapter, source, tmp_path, monkeypatch, seam,
):
    assert callable(getattr(adapter, "apply_snapshot", None))
    before = tree_state(source)
    hits = []

    def fail(*args, **kwargs):
        hits.append(1)
        raise OSError("neutral-private-error-detail")

    monkeypatch.setattr(adapter, seam, fail)
    report = apply(adapter, source, tmp_path / "bundle")
    assert hits == [1]
    assert report["status"] == "failed"
    assert report["exit_code"] == 1
    assert "neutral-private-error-detail" not in json.dumps(report)
    staging = Path(report["staging"])
    assert staging.is_dir()
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700
    assert not (tmp_path / "bundle").exists()
    assert tree_state(source) == before


@pytest.mark.parametrize("kind", ["empty_directory", "file", "symlink"])
def test_atomic_publication_does_not_clobber_racing_target(
    adapter, source, tmp_path, monkeypatch, kind,
):
    assert callable(getattr(adapter, "apply_snapshot", None))
    original = adapter._rename_noreplace
    before = tree_state(source)
    destination = tmp_path / "bundle"
    hits = []

    def racing_rename(parent, staging_name, target_name):
        hits.append(1)
        if kind == "empty_directory":
            os.mkdir(target_name, mode=0o700, dir_fd=parent)
        elif kind == "file":
            destination.write_bytes(b"neutral-concurrent-target")
        else:
            destination.symlink_to(tmp_path / "absent")
        return original(parent, staging_name, target_name)

    monkeypatch.setattr(adapter, "_rename_noreplace", racing_rename)
    report = apply(adapter, source, destination)
    assert hits == [1]
    assert report["status"] == "failed"
    assert Path(report["staging"]).is_dir()
    if kind == "empty_directory":
        assert list(destination.iterdir()) == []
    elif kind == "file":
        assert destination.read_bytes() == b"neutral-concurrent-target"
    else:
        assert destination.is_symlink()
    assert tree_state(source) == before


def cli_environment():
    """Run the working tree's module, never an installed snapshot of it."""
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = (
        source_root + os.pathsep + environment.get("PYTHONPATH", "")
    )
    return environment


def test_real_cli_preview_apply_repeat_and_invalid_arguments(source, tmp_path):
    destination = tmp_path / "bundle"
    command = ["/usr/bin/python3", "-m", "layered_memory_mcp.graph_migration_snapshot"]
    paths = ["--source", str(source), "--destination", str(destination)]
    before = tree_state(tmp_path)
    planned = subprocess.run(
        command + paths, capture_output=True, text=True, check=False,
        env=cli_environment(),
    )
    assert planned.stdout.strip(), "CLI must emit its JSON report"
    report = json.loads(planned.stdout)
    assert planned.returncode == report["exit_code"] == 0
    assert report["dry_run"] is True
    assert planned.stderr == ""
    assert tree_state(tmp_path) == before
    published = subprocess.run(
        command + paths + ["--apply"], capture_output=True, text=True, check=False,
        env=cli_environment(),
    )
    report = json.loads(published.stdout)
    assert published.returncode == report["exit_code"] == 0
    assert report["status"] == "snapshot_created"
    before_repeat = tree_state(tmp_path)
    repeated = subprocess.run(
        command + paths + ["--apply"], capture_output=True, text=True, check=False,
        env=cli_environment(),
    )
    assert repeated.returncode == json.loads(repeated.stdout)["exit_code"] == 2
    assert tree_state(tmp_path) == before_repeat
    invalid = subprocess.run(
        command + ["--unrecognized=neutral-private-value"],
        capture_output=True, text=True, check=False, env=cli_environment(),
    )
    report = json.loads(invalid.stdout)
    assert invalid.returncode == report["exit_code"] == 2
    assert report["findings"] == ["cli_arguments_invalid"]
    assert "neutral-private-value" not in invalid.stdout + invalid.stderr


def test_the_real_cli_accepts_declared_exclusions_and_refuses_bad_ones(source, tmp_path):
    command = ["/usr/bin/python3", "-m", "layered_memory_mcp.graph_migration_snapshot"]
    paths = ["--source", str(source), "--destination", str(tmp_path / "bundle")]
    (source / "operator-owned.json").write_bytes(b"neutral-operator-owned")
    before = tree_state(tmp_path)
    refused = subprocess.run(
        command + paths, capture_output=True, text=True, check=False,
        env=cli_environment(),
    )
    assert json.loads(refused.stdout)["findings"] == [
        "source_coverage_blocked", "source_unknown_member:operator-owned.json",
    ]
    declared = subprocess.run(
        command + paths + ["--exclude", "operator-owned.json"],
        capture_output=True, text=True, check=False, env=cli_environment(),
    )
    report = json.loads(declared.stdout)
    assert declared.returncode == report["exit_code"] == 0
    assert report["status"] == "preview"
    assert report["operator_exclusions"] == ["operator-owned.json"]
    reserved = subprocess.run(
        command + paths + ["--exclude", "knowledge"],
        capture_output=True, text=True, check=False, env=cli_environment(),
    )
    report = json.loads(reserved.stdout)
    assert reserved.returncode == report["exit_code"] == 2
    assert report["findings"] == ["operator_exclusion_reserved:knowledge"]
    assert tree_state(tmp_path) == before


def test_root_membership_drift_during_final_scan_is_not_missed(
    adapter, source, tmp_path, monkeypatch,
):
    original = os.scandir
    knowledge_identity = (source / "knowledge").stat().st_ino
    hits = []
    before = tree_state(source)

    def drifting_scan(descriptor):
        if isinstance(descriptor, int) and os.fstat(descriptor).st_ino == knowledge_identity:
            hits.append(1)
            if len(hits) == 2:
                (source / "config.yaml").write_bytes(b"neutral-late-member")
        return original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", drifting_scan)
        report = preview(adapter, source, tmp_path)
    after = tree_state(source)
    assert hits == [1, 1]
    assert report["status"] == "refused"
    assert any(finding.startswith("source_drift") for finding in report["findings"])
    assert not (tmp_path / "bundle").exists()
    assert after["./config.yaml"][-1] == b"neutral-late-member"
    assert {key: value for key, value in after.items() if key not in {".", "./config.yaml"}} == {
        key: value for key, value in before.items() if key != "."
    }


def test_staging_membership_drift_during_last_verification_is_not_missed(
    adapter, source, tmp_path, monkeypatch,
):
    original_copy = adapter._copy_file
    original_scan = os.scandir
    stage = []
    hits = []
    before = tree_state(source)

    def capture_stage(root, staging, record, expected):
        if not stage:
            stage.append((staging, os.stat("source/knowledge", dir_fd=staging).st_ino))
        return original_copy(root, staging, record, expected)

    def drifting_scan(descriptor):
        if stage and isinstance(descriptor, int) and os.fstat(descriptor).st_ino == stage[0][1]:
            hits.append(1)
            if len(hits) == 4:
                os.mkdir("unexpected", dir_fd=stage[0][0])
        return original_scan(descriptor)

    monkeypatch.setattr(adapter, "_copy_file", capture_stage)
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", drifting_scan)
        report = apply(adapter, source, tmp_path / "bundle")
    assert hits == [1, 1, 1, 1]
    assert report["status"] == "failed"
    assert not (tmp_path / "bundle").exists()
    assert (Path(report["staging"]) / "unexpected").is_dir()
    assert tree_state(source) == before


def test_apply_never_reads_exclusions_or_uses_sqlite_or_config(
    adapter, source, tmp_path, monkeypatch,
):
    from layered_memory_mcp.config import MemoryConfig

    forbidden = {"config.yaml", ".env", "models", ".git", "backups", "vectors.db-shm"}
    for name in ("config.yaml", ".env"):
        (source / name).write_bytes(b"neutral-excluded")
    for name in ("models", ".git", "backups"):
        (source / name).mkdir()
        (source / name / "unread").symlink_to(tmp_path / "absent")
    (source / "data" / "vectors.db-shm").write_bytes(b"neutral-shm")
    excluded_inodes = {(source / name).stat().st_ino for name in ("models", ".git", "backups")}
    before = tree_state(source)
    real_open = os.open
    real_builtin_open = builtins.open
    real_io_open = io.open
    real_scan = os.scandir
    hits = []

    def check_name(path):
        if not isinstance(path, int):
            assert Path(path).name not in forbidden, "excluded entry accessed"

    def guarded_open(path, *args, **kwargs):
        check_name(path)
        hits.append(1)
        return real_open(path, *args, **kwargs)

    def guarded_builtin_open(path, *args, **kwargs):
        check_name(path)
        return real_builtin_open(path, *args, **kwargs)

    def guarded_io_open(path, *args, **kwargs):
        check_name(path)
        return real_io_open(path, *args, **kwargs)

    def guarded_scan(path):
        check_name(path)
        if isinstance(path, int):
            assert os.fstat(path).st_ino not in excluded_inodes
        return real_scan(path)

    def forbidden_call(*args, **kwargs):
        pytest.fail("snapshot must not construct config or open SQLite")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", guarded_open)
        patch.setattr(builtins, "open", guarded_builtin_open)
        patch.setattr(io, "open", guarded_io_open)
        patch.setattr(os, "scandir", guarded_scan)
        patch.setattr(sqlite3, "connect", forbidden_call)
        patch.setattr(MemoryConfig, "__init__", forbidden_call)
        report = apply(adapter, source, tmp_path / "bundle")
    assert report["status"] == "snapshot_created"
    assert hits
    assert tree_state(source) == before
    assert not any(record["path"] in forbidden for record in report["manifest"]["files"])


@pytest.mark.parametrize("relative", ["unknown", "data/unknown"])
def test_unknown_subtrees_are_not_traversed(adapter, source, tmp_path, monkeypatch, relative):
    directory = source / relative
    directory.mkdir()
    (directory / "unread").write_bytes(b"neutral-unread")
    before = tree_state(tmp_path)
    original = os.open
    hits = []

    def guarded_open(path, *args, **kwargs):
        assert Path(path).name != directory.name
        hits.append(1)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", guarded_open)
        report = apply(adapter, source, tmp_path / "bundle")
    assert hits
    assert report["status"] == "refused"
    assert tree_state(tmp_path) == before


def test_a_nonempty_knowledge_namespace_blocks_without_reading_its_members(
    adapter, source, tmp_path, monkeypatch,
):
    """The namespace handle is opened to *list* names — never its members.

    A knowledge subdirectory is the one unrecognised shape whose emptiness has
    to be established before it can be dismissed, and metadata cannot establish
    it. So this directory is opened once; what must never happen is a member of
    it being opened or a byte of it being read.
    """
    directory = source / "knowledge" / "nested"
    directory.mkdir()
    (directory / "held.md").write_bytes(b"neutral-held")
    (directory / "deeper").mkdir()
    before = tree_state(tmp_path)
    original_open = os.open
    original_read = os.read
    namespace_handles = []

    def guarded_open(path, *args, **kwargs):
        assert Path(path).name not in {"held.md", "deeper"}, "namespace member opened"
        descriptor = original_open(path, *args, **kwargs)
        if not isinstance(path, int) and Path(path).name == "nested":
            namespace_handles.append(descriptor)
        return descriptor

    def guarded_read(descriptor, count):
        assert descriptor not in namespace_handles, "namespace payload read"
        return original_read(descriptor, count)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", guarded_open)
        patch.setattr(os, "read", guarded_read)
        report = apply(adapter, source, tmp_path / "bundle")
    assert namespace_handles, "emptiness must actually be checked, not assumed"
    assert report["status"] == "refused"
    assert "knowledge_directory_uncovered:knowledge/nested" in report["findings"]
    assert {
        item["path"]: item["classification"] for item in report["inventory"]
    }["knowledge/nested"] == "blocked"
    assert "knowledge/nested/held.md" not in {
        item["path"] for item in report["inventory"]
    }
    assert "neutral-held" not in json.dumps(report)
    assert tree_state(tmp_path) == before


def test_an_empty_knowledge_namespace_is_reported_not_blocked(adapter, source, tmp_path):
    """Positive control for the one bounded namespace allowance."""
    for name in ("alpha-namespace", "beta-namespace"):
        (source / "knowledge" / name).mkdir()
    report = apply(adapter, source, tmp_path / "bundle")
    assert report["status"] == "snapshot_created"
    assert report["empty_namespaces"] == [
        "knowledge/alpha-namespace", "knowledge/beta-namespace",
    ]
    assert report["manifest"]["empty_namespaces"] == report["empty_namespaces"]
    assert "empty_knowledge_namespace" in report["warnings"]
    classified = {item["path"]: item["classification"] for item in report["inventory"]}
    assert classified["knowledge/alpha-namespace"] == "empty_namespace"
    # An empty namespace is accounted for, not copied: the bundle keeps the
    # adapter's two-directory shape so migration sees no phantom namespace.
    assert sorted(path.name for path in (tmp_path / "bundle" / "source" / "knowledge").iterdir()) == [
        "alpha.md"
    ]
    assert [record["path"] for record in report["manifest"]["files"]] == [
        "L0.md", "data/memory_changelog.jsonl", "data/vectors.db", "knowledge/alpha.md",
    ]


def test_a_knowledge_namespace_that_fills_up_is_rechecked_before_publication(
    adapter, source, tmp_path, monkeypatch,
):
    """Emptiness proved at inspection must not be trusted at publication."""
    namespace = source / "knowledge" / "late-namespace"
    namespace.mkdir()
    original = adapter._copy_file
    hits = []

    def filling_copy(*args, **kwargs):
        result = original(*args, **kwargs)
        hits.append(1)
        if len(hits) == 1:
            (namespace / "late.md").write_bytes(b"neutral-late")
        return result

    monkeypatch.setattr(adapter, "_copy_file", filling_copy)
    report = apply(adapter, source, tmp_path / "bundle")
    assert hits
    assert report["status"] == "failed"
    assert not (tmp_path / "bundle").exists()
    assert Path(report["staging"]).is_dir()


@pytest.mark.parametrize("name", [
    "alpha.md", "alpha.md.lock", "nested.md",
])
def test_a_markdown_named_knowledge_directory_is_still_a_wrong_type(
    adapter, source, tmp_path, name,
):
    """A directory shaped like a document is a defect, not a namespace."""
    path = source / "knowledge" / name
    if path.exists():
        path.unlink()
    path.mkdir()
    before = tree_state(tmp_path)
    report = apply(adapter, source, tmp_path / "bundle")
    assert report["status"] == "refused"
    assert f"source_wrong_type:knowledge/{name}" in report["findings"]
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("relative", [
    "knowledge/alpha.md", "data/vectors.db", "L0.md", "config.yaml",
])
def test_required_and_excluded_files_cannot_be_directories(adapter, source, tmp_path, relative):
    path = source / relative
    if path.exists():
        path.unlink()
    path.mkdir()
    before = tree_state(tmp_path)
    report = apply(adapter, source, tmp_path / "bundle")
    assert report["status"] == "refused"
    assert any(relative in finding for finding in report["findings"])
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("kind", [
    "source_missing", "source_file", "source_fifo", "parent_file", "parent_fifo",
    "destination_fifo", "double_slash_alias",
])
def test_more_path_contract_refusals(adapter, source, tmp_path, kind):
    chosen_source = source
    destination = tmp_path / "bundle"
    if kind.startswith("source_"):
        chosen_source = tmp_path / "other"
        if kind == "source_file":
            chosen_source.write_bytes(b"neutral")
        elif kind == "source_fifo":
            os.mkfifo(chosen_source)
    elif kind.startswith("parent_"):
        parent = tmp_path / "other"
        if kind == "parent_file":
            parent.write_bytes(b"neutral")
        else:
            os.mkfifo(parent)
        destination = parent / "bundle"
    elif kind == "destination_fifo":
        os.mkfifo(destination)
    else:
        chosen_source = Path("/" + str(source))
        destination = source / "bundle"
    before = tree_state(tmp_path)
    report = apply(adapter, chosen_source, destination)
    assert report["status"] == "refused"
    assert report["staging"] is None
    if kind == "double_slash_alias":
        assert "paths_aliased" in report["findings"]
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("kind", [
    "extra_file", "empty_directory", "missing_file", "symlink", "manifest_bytes",
])
def test_copied_membership_and_manifest_tampering_refused(
    adapter, source, tmp_path, monkeypatch, kind,
):
    original = adapter._verify_bundle
    hits = []
    before = tree_state(source)

    def tampered_verification(staging, manifest):
        hits.append(1)
        if len(hits) == 1:
            if kind == "empty_directory":
                os.mkdir("extra", dir_fd=staging)
            elif kind == "missing_file":
                os.unlink("source/data/vectors.db", dir_fd=staging)
            elif kind == "symlink":
                os.unlink("source/data/vectors.db", dir_fd=staging)
                os.symlink(str(tmp_path / "absent"), "source/data/vectors.db", dir_fd=staging)
            else:
                path = "extra" if kind == "extra_file" else "snapshot-manifest.json"
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600, dir_fd=staging,
                )
                try:
                    os.write(descriptor, b"neutral-tampered")
                finally:
                    os.close(descriptor)
        return original(staging, manifest)

    monkeypatch.setattr(adapter, "_verify_bundle", tampered_verification)
    report = apply(adapter, source, tmp_path / "bundle")
    assert hits == [1]
    assert report["status"] == "failed"
    assert Path(report["staging"]).is_dir()
    assert not (tmp_path / "bundle").exists()
    assert tree_state(source) == before


def test_in_place_change_during_hash_is_detected(adapter, source, tmp_path, monkeypatch):
    original = os.read
    hits = []
    before = tree_state(source)

    def changing_read(descriptor, count):
        payload = original(descriptor, count)
        hits.append(1)
        if len(hits) == 1:
            (source / "L0.md").write_bytes(b"neutral-changed-evidence")
        return payload

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", changing_read)
        report = preview(adapter, source, tmp_path)
    after = tree_state(source)
    assert hits
    assert report["status"] == "refused"
    assert "file_changed:L0.md" in report["findings"]
    assert not (tmp_path / "bundle").exists()
    assert after["./L0.md"][-1] == b"neutral-changed-evidence"
    assert {key: value for key, value in after.items() if key != "./L0.md"} == {
        key: value for key, value in before.items() if key != "./L0.md"
    }


def test_source_ancestor_replacement_is_detected_before_staging(
    adapter, source, tmp_path, monkeypatch,
):
    original = adapter._observe_source
    original_open = os.open
    hits = []
    before = tree_state(source)
    moved = tmp_path / "moved"

    def replace_source(*args, **kwargs):
        result = original(*args, **kwargs)
        hits.append(1)
        source.rename(moved)
        source.symlink_to(tmp_path / "absent")
        return result

    def guarded_open(path, *args, **kwargs):
        assert Path(path).name != "absent"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(adapter, "_observe_source", replace_source)
    monkeypatch.setattr(os, "open", guarded_open)
    report = apply(adapter, source, tmp_path / "bundle")
    assert hits == [1]
    assert report["status"] == "refused"
    assert "ancestor_changed" in report["findings"]
    assert report["staging"] is None
    after = tree_state(moved)
    assert {key: value for key, value in after.items() if key != "."} == {
        key: value for key, value in before.items() if key != "."
    }


@pytest.mark.parametrize("kind", ["mkdir_error", "unsupported_publication", "early_target_race"])
def test_prepublication_failures_do_not_create_unowned_debris(
    adapter, source, tmp_path, monkeypatch, kind,
):
    before = tree_state(tmp_path)
    hits = []
    destination = tmp_path / "bundle"
    if kind == "mkdir_error":
        original = os.mkdir

        def failed_mkdir(path, *args, **kwargs):
            if str(path).startswith(".snapshot-stage-"):
                hits.append(1)
                raise OSError("neutral-private-detail")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "mkdir", failed_mkdir)
    elif kind == "unsupported_publication":
        def unsupported():
            hits.append(1)
            raise adapter._Refused("atomic_noreplace_unavailable")

        monkeypatch.setattr(adapter, "_rename_function", unsupported)
    else:
        original = adapter._observe_source

        def create_target(*args, **kwargs):
            result = original(*args, **kwargs)
            hits.append(1)
            destination.mkdir()
            return result

        monkeypatch.setattr(adapter, "_observe_source", create_target)
    report = apply(adapter, source, destination)
    assert hits == [1]
    assert report["status"] == ("failed" if kind == "mkdir_error" else "refused")
    assert report["staging"] is None
    assert "neutral-private-detail" not in json.dumps(report)
    if kind == "early_target_race":
        assert list(destination.iterdir()) == []
        assert not list(tmp_path.glob(".snapshot-stage-*"))
    else:
        assert tree_state(tmp_path) == before


def test_bundle_layout_is_accepted_without_loosening_strict_inventory(adapter, source, tmp_path):
    from layered_memory_mcp import graph_migration_inventory as inventory

    database = source / "data" / "vectors.db"
    database.unlink()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE vectors "
            "(id TEXT, domain TEXT, text TEXT, vector TEXT, metadata TEXT)"
        )
    (source / "data" / "memory_changelog.jsonl").unlink()
    report = apply(adapter, source, tmp_path / "bundle")
    assert report["status"] == "snapshot_created"
    snapshot = inventory.read_source_snapshot(Path(report["migration_source"]))
    assert not any(
        "source_unsupported_record" in finding
        for finding in snapshot.inventory.findings
    )
    original = inventory.read_source_snapshot(source)
    assert any(
        "source_unsupported_record:L0.md" in finding
        for finding in original.inventory.findings
    )

"""Explicit synthetic legacy-layout repairs; no default config or external data."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from test_v361_graph_application import _request, config, fake_embeddings  # noqa: F401
from layered_memory_mcp.graph_application import _bind_events, commit_semantic_write
from layered_memory_mcp.graph_journal import MemoryEventJournal, SurfaceRecord, body_sha256
from layered_memory_mcp.graph_projection import (
    ProjectionBinding, binding_metadata, read_section_body, surface_refusal,
)
from layered_memory_mcp.graph_write import plan_semantic_write

BODY = "First authored paragraph.\n\n## Inner heading\n\nLast authored paragraph."
PREFIX = b"# Fixture\n\nUnmanaged preface.\n\n"
SUFFIX = b"## Neighbor\n\nUnmanaged neighbor.\n"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def legacy_fixture(config, *, body: str = BODY):
    binding = ProjectionBinding(domain="fixture", section="Outer", body=body)
    request = _request("repair-owner", body)
    events = _bind_events(plan_semantic_write(request).events, request.node.id, binding)
    with MemoryEventJournal(config.graph_journal_path) as journal:
        result = journal.append("historical-publication", events)
        journal.record_surface(SurfaceRecord(
            **{key: value for key, value in binding_metadata(binding).items()
               if key not in ("surface",)},
            node_id=request.node.id, position=result.last_position,
        ))
        for name in ("graph", "l1", "vector", "l0", "todo"):
            journal.set_checkpoint(name, result.last_position)
    path = Path(config.knowledge_dir) / "fixture.md"
    raw = PREFIX + f"## Outer\n\n{body.strip()}\n\n".encode() + SUFFIX
    path.write_bytes(raw)
    return binding, path, raw


def make_plan(config, binding, path):
    from layered_memory_mcp.graph_surface_repair import (
        plan_legacy_surface_repair, repair_registry_sha256,
    )
    with MemoryEventJournal.open_readonly(config.graph_journal_path) as journal:
        record = journal.surface(binding.entry_id)
        return plan_legacy_surface_repair(
            config, entry_id=binding.entry_id, node_id=record.node_id,
            domain=binding.domain, section=binding.section, occurrence=binding.occurrence,
            published_position=record.position, expected_head=journal.head_position(),
            expected_registry_sha256=repair_registry_sha256(journal),
            expected_document_sha256=digest(path.read_bytes()),
        )


def test_explicit_repair_preserves_semantics_and_enables_next_regular_commit(config, tmp_path):
    from layered_memory_mcp.graph_surface_repair import apply_legacy_surface_repair

    binding, path, before = legacy_fixture(config)
    with MemoryEventJournal.open_readonly(config.graph_journal_path) as journal:
        semantic_before = [event.to_json() for event in journal.read_events()]
        record_before = journal.surface(binding.entry_id)
        checkpoints_before = {name: journal.checkpoint(name) for name in ("graph", "l1", "vector", "l0", "todo")}
        assert surface_refusal(config, journal, binding)
    database_before = Path(config.graph_journal_path).read_bytes()
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    backup.mkdir()
    receipt = apply_legacy_surface_repair(
        config, plan=plan, expected_plan_sha256=plan.sha256,
        backup_root=backup, maintenance_confirmed=True,
    )
    assert receipt.status == "repaired"
    assert receipt.refresh_required == ("vector", "l0", "keyword_cache")
    after = path.read_bytes()
    assert after.startswith(PREFIX) and after.endswith(SUFFIX)
    assert read_section_body(after.decode(), "Outer") == BODY
    assert (Path(receipt.operation_dir) / "document.before").read_bytes() == before
    assert json.loads((Path(receipt.operation_dir) / "receipt.json").read_text())["after_sha256"] == digest(after)
    with MemoryEventJournal.open_readonly(config.graph_journal_path) as journal:
        assert [event.to_json() for event in journal.read_events()] == semantic_before
        assert journal.surface(binding.entry_id) == record_before
        assert {name: journal.checkpoint(name) for name in checkpoints_before} == checkpoints_before
        assert surface_refusal(config, journal, binding) is None
    assert Path(config.graph_journal_path).read_bytes() == database_before
    with sqlite3.connect(Path(receipt.operation_dir) / "journal.before.db") as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] == len(semantic_before)
    retry = apply_legacy_surface_repair(
        config, plan=plan, expected_plan_sha256=plan.sha256,
        backup_root=backup, maintenance_confirmed=True,
    )
    assert retry.status == "already_repaired"
    commit = commit_semantic_write(
        config, _request("next-owner", "A regular next decision.", offset=1),
        ProjectionBinding(domain="fixture", section="Next", body="A regular next decision."),
    )
    assert commit.committed and commit.surface_published
    assert read_section_body(path.read_text(), "Outer") == BODY


def apply(config, plan, backup, **kwargs):
    from layered_memory_mcp.graph_surface_repair import apply_legacy_surface_repair
    backup.mkdir(exist_ok=True)
    return apply_legacy_surface_repair(
        config, plan=plan, expected_plan_sha256=plan.sha256,
        backup_root=backup, maintenance_confirmed=True, **kwargs,
    )


@pytest.mark.parametrize("phase", ["intent_durable", "document_replaced"])
def test_recovery_after_each_atomic_state(config, tmp_path, phase):
    from layered_memory_mcp.graph_surface_repair import recover_legacy_surface_repair
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"

    def crash(actual):
        if actual == phase:
            raise RuntimeError("synthetic_crash")

    with pytest.raises(RuntimeError, match="synthetic_crash"):
        apply(config, plan, backup, checkpoint=crash)
    operation = backup / plan.sha256
    assert (operation / "intent.json").exists()
    assert not (operation / "receipt.json").exists()
    assert digest(path.read_bytes()) == (
        plan.expected_document_sha256 if phase == "intent_durable" else plan.after_sha256
    )
    result = recover_legacy_surface_repair(
        config, plan=plan, expected_plan_sha256=plan.sha256,
        backup_root=backup, maintenance_confirmed=True,
    )
    assert result.status == "repaired"
    assert digest(path.read_bytes()) == plan.after_sha256
    assert (operation / "document.before").read_bytes() == before


@pytest.mark.parametrize("change", ["file", "head", "registry", "intent"])
def test_stale_plan_rejects_without_creating_backup(config, tmp_path, change):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    from layered_memory_mcp.graph_journal import SurfaceIntent
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    if change == "file":
        path.write_bytes(before + b"External edit.\n")
    else:
        with MemoryEventJournal(config.graph_journal_path) as journal:
            if change == "head":
                journal.append("new-decision", plan_semantic_write(_request("new-owner", "New.", offset=2)).events)
            elif change == "registry":
                record = journal.surface(binding.entry_id)
                journal.record_surface(record.model_copy(update={"body_sha256": "0" * 64}))
            else:
                journal.record_surface_intent(SurfaceIntent(
                    entry_id=binding.entry_id, domain=binding.domain, section=binding.section,
                    node_id="repair-owner", position=plan.expected_head,
                ))
    current = path.read_bytes()
    backup = tmp_path / "backups"
    with pytest.raises(SurfaceRepairError, match="stale_"):
        apply(config, plan, backup)
    assert path.read_bytes() == current
    assert list(backup.iterdir()) == []


@pytest.mark.parametrize("shape", ["edit", "tail", "duplicate", "wrong_occurrence", "interleave", "planned_interleave"])
def test_unproven_legacy_spans_refuse(config, shape):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    binding, path, before = legacy_fixture(config)
    if shape == "edit":
        path.write_bytes(before.replace(b"Last authored", b"Edited authored"))
    elif shape == "tail":
        path.write_bytes(before.replace(b"Last authored paragraph.", b""))
    elif shape == "duplicate":
        path.write_bytes(before + f"## Outer\n\n{BODY}\n\n".encode())
    elif shape == "wrong_occurrence":
        binding = binding.model_copy(update={"occurrence": 1})
        with MemoryEventJournal(config.graph_journal_path) as journal:
            record = journal.surfaces()[0]
            journal.record_surface(record.model_copy(update={"entry_id": binding.entry_id, "occurrence": 1}))
    else:
        other = ProjectionBinding(domain="fixture", section="Inner heading", body="Last authored paragraph.")
        events = _bind_events(plan_semantic_write(_request("interleaved-owner", other.body, offset=1)).events,
                              "interleaved-owner", other)
        with MemoryEventJournal(config.graph_journal_path) as journal:
            position = journal.append("interleaved-publication", events).last_position
            if shape == "interleave":
                journal.record_surface(SurfaceRecord(
                    entry_id=other.entry_id, domain=other.domain, section=other.section,
                    node_id="interleaved-owner", body_sha256=body_sha256(other.body), position=position,
                ))
    with pytest.raises(SurfaceRepairError):
        make_plan(config, binding, path)
    assert "managed-body" not in path.read_text()


@pytest.mark.parametrize("phase", ["intent_durable", "document_replaced"])
def test_recovery_never_overwrites_external_edits(config, tmp_path, phase):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    binding, path, _ = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"

    def crash(actual):
        if actual == phase:
            raise RuntimeError("synthetic_crash")

    with pytest.raises(RuntimeError):
        apply(config, plan, backup, checkpoint=crash)
    edited = path.read_bytes() + b"External edit after crash.\n"
    path.write_bytes(edited)
    with pytest.raises(SurfaceRepairError):
        apply(config, plan, backup)
    assert path.read_bytes() == edited


def test_conditional_rollback_only_restores_document(config, tmp_path):
    from layered_memory_mcp.graph_surface_repair import rollback_legacy_surface_repair
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    apply(config, plan, backup)
    database_before = Path(config.graph_journal_path).read_bytes()
    result = rollback_legacy_surface_repair(
        config, plan=plan, expected_plan_sha256=plan.sha256, backup_root=backup,
        maintenance_confirmed=True, expected_current_sha256=plan.after_sha256,
    )
    assert result.status == "rolled_back"
    assert path.read_bytes() == before
    assert Path(config.graph_journal_path).read_bytes() == database_before


def test_rollback_lifecycle_does_not_silently_reapply(config, tmp_path):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError, rollback_legacy_surface_repair
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    apply(config, plan, backup)
    rollback_legacy_surface_repair(
        config, plan=plan, expected_plan_sha256=plan.sha256, backup_root=backup,
        maintenance_confirmed=True, expected_current_sha256=plan.after_sha256,
    )
    with pytest.raises(SurfaceRepairError, match="repair_rolled_back"):
        apply(config, plan, backup)
    assert path.read_bytes() == before


@pytest.mark.parametrize("unsafe", ["document_symlink", "journal_symlink", "backup_in_source", "backup_symlink", "domain_traversal"])
def test_unsafe_paths_refuse(config, tmp_path, unsafe):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    binding, path, _ = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    if unsafe.endswith("symlink"):
        target = path if unsafe == "document_symlink" else (
            Path(config.graph_journal_path) if unsafe == "journal_symlink" else backup
        )
        if unsafe == "backup_symlink":
            real = tmp_path / "real-backups"
            real.mkdir()
        else:
            real = target.with_suffix(".real")
            target.rename(real)
        target.symlink_to(real)
    elif unsafe == "backup_in_source":
        backup = Path(config.knowledge_dir) / "backups"
    else:
        from dataclasses import replace
        from layered_memory_mcp.graph_surface_repair import plan_legacy_surface_repair
        with pytest.raises(SurfaceRepairError, match="unsafe_domain"):
            plan_legacy_surface_repair(config, **{
                key: getattr(replace(plan, domain="../fixture"), key) for key in (
                    "entry_id", "node_id", "domain", "section", "occurrence", "published_position",
                    "expected_head", "expected_registry_sha256", "expected_document_sha256",
                )
            })
        return
    with pytest.raises(SurfaceRepairError):
        apply(config, plan, backup)


def test_plan_is_zero_write_to_source_tree(config):
    binding, path, _ = legacy_fixture(config)
    root = Path(config.home)

    def manifest():
        return {str(item.relative_to(root)): (
            item.lstat().st_mtime_ns, item.read_bytes() if item.is_file() else None
        ) for item in (root, *root.rglob("*"))}

    before = manifest()
    make_plan(config, binding, path)
    assert manifest() == before


def test_rollback_failure_cannot_turn_into_false_idempotent_repair(config, tmp_path, monkeypatch):
    from layered_memory_mcp import graph_surface_repair as repair
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    apply(config, plan, backup)
    original = repair._atomic_document

    def crash_after_replace(*args):
        original(*args)
        raise RuntimeError("rollback_crash")

    monkeypatch.setattr(repair, "_atomic_document", crash_after_replace)
    with pytest.raises(RuntimeError, match="rollback_crash"):
        repair.rollback_legacy_surface_repair(
            config, plan=plan, expected_plan_sha256=plan.sha256, backup_root=backup,
            maintenance_confirmed=True, expected_current_sha256=plan.after_sha256,
        )
    monkeypatch.setattr(repair, "_atomic_document", original)
    assert path.read_bytes() == before
    with pytest.raises(repair.SurfaceRepairError, match="repair_rolled_back"):
        apply(config, plan, backup)
    result = repair.rollback_legacy_surface_repair(
        config, plan=plan, expected_plan_sha256=plan.sha256, backup_root=backup,
        maintenance_confirmed=True, expected_current_sha256=plan.after_sha256,
    )
    assert result.status == "rolled_back"
    assert (backup / plan.sha256 / "rollback.json").exists()


def test_unaffected_managed_surface_is_verified_and_byte_preserved(config, tmp_path):
    from layered_memory_mcp.markdown_sections import render_section
    binding, path, before = legacy_fixture(config)
    other = ProjectionBinding(domain="fixture", section="Neighbor", body="Unmanaged neighbor.")
    events = _bind_events(plan_semantic_write(_request("neighbor-owner", other.body, offset=1)).events,
                          "neighbor-owner", other)
    with MemoryEventJournal(config.graph_journal_path) as journal:
        position = journal.append("neighbor-publication", events).last_position
        journal.record_surface(SurfaceRecord(
            entry_id=other.entry_id, domain=other.domain, section=other.section,
            node_id="neighbor-owner", body_sha256=body_sha256(other.body), position=position,
        ))
    plan = make_plan(config, binding, path)
    apply(config, plan, tmp_path / "backups")
    assert path.read_bytes() == PREFIX + render_section("Outer", BODY).encode() + SUFFIX
    assert read_section_body(path.read_text(), "Neighbor") == other.body


def test_unaffected_managed_drift_blocks_repair(config):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    binding, path, before = legacy_fixture(config)
    other = ProjectionBinding(domain="fixture", section="Neighbor", body="Known neighbor.")
    with MemoryEventJournal(config.graph_journal_path) as journal:
        journal.record_surface(SurfaceRecord(
            entry_id=other.entry_id, domain=other.domain, section=other.section,
            node_id="neighbor-owner", body_sha256=body_sha256(other.body), position=1,
        ))
    with pytest.raises(SurfaceRepairError, match="managed_surface_mismatch"):
        make_plan(config, binding, path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("after", [False, True])
def test_crash_states_do_not_make_regular_writes_unsafe(config, tmp_path, after):
    binding, path, _ = legacy_fixture(config)
    plan = make_plan(config, binding, path)

    def crash(phase):
        if phase == ("document_replaced" if after else "intent_durable"):
            raise RuntimeError("synthetic_crash")

    with pytest.raises(RuntimeError):
        apply(config, plan, tmp_path / "backups", checkpoint=crash)
    current = path.read_bytes()
    with MemoryEventJournal.open_readonly(config.graph_journal_path) as journal:
        original_head = journal.head_position()
    commit = commit_semantic_write(
        config, _request("post-crash", "Next decision.", offset=2),
        ProjectionBinding(domain="fixture", section="Next", body="Next decision."),
    )
    assert commit.committed is after
    if after:
        assert commit.surface_published
        assert read_section_body(path.read_text(), "Outer") == BODY
    else:
        assert path.read_bytes() == current
        with MemoryEventJournal.open_readonly(config.graph_journal_path) as journal:
            assert journal.head_position() == original_head


@pytest.mark.parametrize("change", ["document", "head"])
def test_rollback_refuses_intervening_changes(config, tmp_path, change):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError, rollback_legacy_surface_repair
    binding, path, _ = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    apply(config, plan, backup)
    if change == "document":
        path.write_bytes(path.read_bytes() + b"External edit.\n")
    else:
        with MemoryEventJournal(config.graph_journal_path) as journal:
            journal.append("new-decision", plan_semantic_write(_request("new-owner", "New.", offset=2)).events)
    current = path.read_bytes()
    with pytest.raises(SurfaceRepairError, match="stale_"):
        rollback_legacy_surface_repair(
            config, plan=plan, expected_plan_sha256=plan.sha256, backup_root=backup,
            maintenance_confirmed=True, expected_current_sha256=plan.after_sha256,
        )
    assert path.read_bytes() == current


def test_recheck_after_durable_backup_rejects_ignoring_editor(config, tmp_path):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    edited = before + b"Noncooperating edit.\n"

    def editor(phase):
        if phase == "intent_durable":
            path.write_bytes(edited)

    with pytest.raises(SurfaceRepairError, match="stale_document"):
        apply(config, plan, tmp_path / "backups", checkpoint=editor)
    assert path.read_bytes() == edited


@pytest.mark.parametrize("variant", ["eof", "unicode", "closed_fence", "unterminated"])
def test_exact_legacy_renderer_variants(config, tmp_path, variant):
    body = {
        "eof": BODY, "unicode": "Authored café.\n\n## 子标题\n\n完整正文。",
        "closed_fence": "Example:\n\n```md\n## example heading\n```",
        "unterminated": "Unclosed example:\n\n~~~text\n## authored inside",
    }[variant]
    binding, path, _ = legacy_fixture(config, body=body)
    if variant == "eof":
        path.write_bytes(PREFIX + f"## Outer\n\n{body}\n".encode())
    if variant == "closed_fence":
        from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
        with pytest.raises(SurfaceRepairError, match="repair_not_required"):
            make_plan(config, binding, path)
        assert read_section_body(path.read_text(), "Outer") == body
        return
    plan = make_plan(config, binding, path)
    apply(config, plan, tmp_path / "backups")
    assert read_section_body(path.read_text(), "Outer") == body


def test_partial_backup_never_authorizes_mutation(config, tmp_path):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    operation = backup / plan.sha256
    operation.mkdir(parents=True)
    (operation / "document.before").write_bytes(before)
    with pytest.raises(SurfaceRepairError):
        apply(config, plan, backup)
    assert path.read_bytes() == before


def test_completed_receipt_cannot_authorize_repair_of_reverted_file(config, tmp_path):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    apply(config, plan, backup)
    path.write_bytes(before)
    with pytest.raises(SurfaceRepairError, match="completed_repair_drift"):
        apply(config, plan, backup)
    assert path.read_bytes() == before


@pytest.mark.parametrize("member", ["document.before", "journal.before.db", "plan.json", "intent.json"])
def test_tampered_backup_refuses_recovery(config, tmp_path, member):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError
    binding, path, _ = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"

    def crash(phase):
        if phase == "document_replaced":
            raise RuntimeError("synthetic_crash")

    with pytest.raises(RuntimeError):
        apply(config, plan, backup, checkpoint=crash)
    target = backup / plan.sha256 / member
    if member == "intent.json":
        value = json.loads(target.read_text())
        value["after_sha256"] = "0" * 64
        target.write_text(json.dumps(value))
    else:
        target.write_bytes(target.read_bytes() + b"tampered")
    current = path.read_bytes()
    with pytest.raises(SurfaceRepairError, match="backup_evidence_mismatch"):
        apply(config, plan, backup)
    assert path.read_bytes() == current


def test_explicit_digest_and_maintenance_confirmation_required(config, tmp_path):
    from layered_memory_mcp.graph_surface_repair import SurfaceRepairError, apply_legacy_surface_repair
    binding, path, before = legacy_fixture(config)
    plan = make_plan(config, binding, path)
    backup = tmp_path / "backups"
    backup.mkdir()
    for expected, maintenance, error in (("0" * 64, True, "plan_digest_mismatch"),
                                         (plan.sha256, False, "maintenance_window_required")):
        with pytest.raises(SurfaceRepairError, match=error):
            apply_legacy_surface_repair(
                config, plan=plan, expected_plan_sha256=expected,
                backup_root=backup, maintenance_confirmed=maintenance,
            )
    assert list(backup.iterdir()) == []
    assert path.read_bytes() == before


def test_online_backup_includes_committed_wal_not_only_main_database(config, tmp_path):
    binding, path, _ = legacy_fixture(config)
    with sqlite3.connect(config.graph_journal_path, isolation_level=None) as live:
        assert live.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        with MemoryEventJournal(live) as journal:
            journal.append("wal-only-event", plan_semantic_write(_request("wal-owner", "WAL decision.", offset=3)).events)
            head = journal.head_position()
        wal = Path(str(config.graph_journal_path) + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        plan = make_plan(config, binding, path)
        receipt = apply(config, plan, tmp_path / "backups")
        with MemoryEventJournal.open_readonly(Path(receipt.operation_dir) / "journal.before.db") as backup:
            assert backup.head_position() == head
            assert any(event.node is not None and event.node.id == "wal-owner"
                       for event in backup.read_events())

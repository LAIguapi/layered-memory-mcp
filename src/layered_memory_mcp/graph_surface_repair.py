"""Explicit, fail-closed repair of a legacy rendered H2 surface.

This is an operator callable, not a CLI, startup migration or drift bypass.
Pass an explicit config naming existing absolute knowledge/journal paths, exact
identities and CAS hashes. Planning is read-only. Applying requires a maintenance
window excluding editors that do not honor the ordinary publication lock.

Only an exact old-renderer span at one unambiguous heading is eligible. The
journal is never modified: body hashes, semantic events and checkpoints remain
unchanged. Backups and durable intent/receipt live outside all knowledge roots.
Atomic replacement leaves either the old (normally refused as drift) document
or the fully validated new document; an unfinished sidecar cannot authorize a
normal write. Recovery refuses newer semantic decisions or unrelated file edits.
Completed semantic checkpoints are NOT evidence of refreshed derived projections;
every successful receipt explicitly requires vector/L0/keyword-cache refresh.

Backup directories contain authored content and must be kept private. No default
paths, discovery, external services, model construction or automatic refresh.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .graph_application import PROJECTION_LOCK_SUFFIX, _publication_lock
from .graph_fixture_store import InMemoryGraphStore
from .graph_journal import MemoryEventJournal, body_sha256
from .graph_models import GraphNode, MemoryEvent
from .graph_projection import (
    PROJECTION_METADATA_KEY,
    PROJECTION_SURFACE_KIND,
    bindings_from_events,
    has_frontmatter_entry,
    parse_binding,
    read_preamble,
    read_section_body,
    surface_plans,
)
from .markdown_sections import (
    MANAGED_BODY_VERSION,
    body_requires_envelope,
    canonical_body,
    heading_label,
    render_section,
    scan_document,
)


class SurfaceRepairError(RuntimeError):
    """Bounded refusal token, never an authored body or local path."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


@dataclass(frozen=True)
class SurfaceRepairPlan:
    """Immutable CAS/evidence contract; contains no authored body."""

    entry_id: str
    node_id: str
    domain: str
    section: str
    occurrence: int
    published_position: int
    expected_head: int
    expected_registry_sha256: str
    expected_document_sha256: str
    journal_path: str
    document_path: str
    events_sha256: str
    after_sha256: str
    span_start: int
    span_end: int
    before_span_sha256: str
    after_span_sha256: str
    representation_version: int = MANAGED_BODY_VERSION

    @property
    def sha256(self) -> str:
        return _sha(_json(asdict(self)))


@dataclass(frozen=True)
class SurfaceRepairReceipt:
    """Physical repair only: projection refresh is intentionally not claimed."""

    status: str
    plan_sha256: str
    before_sha256: str
    after_sha256: str
    operation_dir: str
    refresh_required: tuple[str, ...] = ("vector", "l0", "keyword_cache")


def repair_registry_sha256(journal: MemoryEventJournal) -> str:
    """Digest of all registry and unfinished publication coordination records."""
    return _sha(_json({
        "surfaces": [item.model_dump(mode="json") for item in journal.surfaces()],
        "intents": [item.model_dump(mode="json") for item in journal.surface_intents()],
    }))


def _safe_path(value: Any, *, exists: bool = True) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise SurfaceRepairError("explicit_path_required")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SurfaceRepairError("unsafe_path")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise SurfaceRepairError("symlink_refused")
    if exists and not path.exists():
        raise SurfaceRepairError("path_missing")
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise SurfaceRepairError("aliased_file_refused")
    return path


def _paths(config: Any, domain: str) -> tuple[Path, Path, tuple[Path, ...]]:
    if not domain or domain in (".", "..") or any(c in domain for c in "/\\\x00"):
        raise SurfaceRepairError("unsafe_domain")
    configured = getattr(config, "knowledge_dirs", None)
    roots = tuple(_safe_path(value) for value in (configured or (
        getattr(config, "knowledge_dir", None),
    )))
    if any(not root.is_dir() for root in roots):
        raise SurfaceRepairError("knowledge_root_required")
    journal_path = _safe_path(getattr(config, "graph_journal_path", None))
    document = _safe_path(roots[0] / f"{domain}.md")
    _safe_path(journal_path.with_name(journal_path.name + PROJECTION_LOCK_SUFFIX), exists=False)
    if not journal_path.is_file() or not document.is_file():
        raise SurfaceRepairError("regular_file_required")
    return journal_path, document, roots


def _nodes(events: list[MemoryEvent]) -> dict[str, GraphNode]:
    store = InMemoryGraphStore()
    report = store.rebuild(events)
    if not report.ok:
        raise SurfaceRepairError("journal_replay_refused")
    nodes = [GraphNode.model_validate(value) for value in store.snapshot()["nodes"]]
    return {node.id: node for node in nodes}


def _owner(events: list[MemoryEvent], entry_id: str, node_id: str) -> GraphNode:
    nodes = _nodes(events)
    owners = [key for key, binding in bindings_from_events(events).items()
              if binding.entry_id == entry_id and key in nodes and nodes[key].is_current()]
    if owners != [node_id]:
        raise SurfaceRepairError("historical_owner_mismatch")
    return nodes[node_id]


def _verify_document(raw: bytes, journal: MemoryEventJournal, domain: str) -> None:
    text = raw.decode("utf-8")
    scan_document(text)
    for record in journal.surfaces(include_retired=False):
        if record.domain != domain:
            continue
        body = (read_preamble(text) if record.kind != PROJECTION_SURFACE_KIND else
                read_section_body(text, record.section, record.occurrence))
        if body is None or body_sha256(body) != record.body_sha256:
            raise SurfaceRepairError("managed_surface_mismatch")
    events = journal.read_events()
    for plan in surface_plans(events, _nodes(events)):
        if plan.domain != domain:
            continue
        body = (read_preamble(text) if plan.kind != PROJECTION_SURFACE_KIND else
                read_section_body(text, plan.section, plan.occurrence))
        expected = plan.normalized_body if plan.action == "publish" else (
            "" if plan.kind != PROJECTION_SURFACE_KIND else None
        )
        if body != expected:
            raise SurfaceRepairError("planned_surface_mismatch")


def _proof(
    journal: MemoryEventJournal, raw: bytes, *, entry_id: str, node_id: str,
    domain: str, section: str, occurrence: int, published_position: int,
    expected_head: int, expected_registry_sha256: str,
) -> tuple[bytes, int, int, str]:
    if journal.head_position() != expected_head:
        raise SurfaceRepairError("stale_head")
    if repair_registry_sha256(journal) != expected_registry_sha256:
        raise SurfaceRepairError("stale_registry")
    if journal.surface_intents():
        raise SurfaceRepairError("publication_pending")
    record = journal.surface(entry_id)
    if (record is None or record.status != "published" or record.node_id != node_id
            or record.domain != domain or record.section != section
            or record.occurrence != occurrence or record.position != published_position
            or record.kind != PROJECTION_SURFACE_KIND or not (0 < published_position <= expected_head)):
        raise SurfaceRepairError("surface_selector_mismatch")
    historical = journal.read_events(upto=published_position)
    current = journal.read_events()
    node = _owner(historical, entry_id, node_id)
    latest = _owner(current, entry_id, node_id)
    binding = bindings_from_events(historical)[node_id]
    if (binding.entry_id != entry_id or binding.domain != domain or binding.section != section
            or binding.occurrence != occurrence or canonical_body(node.content) != canonical_body(latest.content)
            or body_sha256(canonical_body(node.content)) != record.body_sha256):
        raise SurfaceRepairError("historical_binding_mismatch")
    declarations = [event for event in historical if event.node is not None
                    and event.node.id == node_id and parse_binding(event.metadata) is not None]
    if not declarations:
        raise SurfaceRepairError("historical_binding_missing")
    declaration = declarations[-1].metadata[PROJECTION_METADATA_KEY]
    if declaration.get("entry_id") != entry_id or declaration.get("body_sha256") != record.body_sha256:
        raise SurfaceRepairError("historical_binding_mismatch")
    text = raw.decode("utf-8")
    if has_frontmatter_entry(text):
        raise SurfaceRepairError("unsupported_document")
    scan_document(text)
    if not body_requires_envelope(node.content):
        raise SurfaceRepairError("repair_not_required")
    lines = raw.splitlines(keepends=True)
    headings: list[tuple[int, str]] = []
    offset = 0
    for line in lines:
        label = heading_label(line.decode("utf-8").rstrip("\r\n"))
        if label is not None:
            headings.append((offset, label))
        offset += len(line)
    targets = [position for position, label in headings if label == section]
    # Identical/moved duplicates cannot be authenticated from the journal alone.
    if len(targets) != 1 or occurrence != 0:
        raise SurfaceRepairError("ambiguous_heading_occurrence")
    start = targets[0]
    legacy = f"## {section}\n\n{node.content.strip()}\n\n".encode("utf-8")
    if raw[start:start + len(legacy)] != legacy:
        # The old whole-document writer retained one final LF at EOF.
        legacy = legacy[:-1]
        if raw[start:] != legacy:
            raise SurfaceRepairError("legacy_span_mismatch")
    end = start + len(legacy)
    if end != len(raw) and not any(position == end for position, _ in headings):
        raise SurfaceRepairError("legacy_span_boundary_mismatch")
    plans = surface_plans(current, _nodes(current))
    occupied_labels = {item.section for item in journal.surfaces()
                       if item.domain == domain and item.entry_id != entry_id}
    occupied_labels.update(item.section for item in plans
                           if item.domain == domain and item.entry_id != entry_id)
    if any(start < position < end and label in occupied_labels for position, label in headings):
        raise SurfaceRepairError("interleaved_surface")
    replacement = render_section(section, node.content).encode("utf-8")
    if end == len(raw) and not legacy.endswith(b"\n\n"):
        replacement = replacement[:-1]
    final = raw[:start] + replacement + raw[end:]
    _verify_document(final, journal, domain)
    events_hash = _sha(_json([event.model_dump(mode="json") for event in current]))
    return final, start, end, events_hash


def plan_legacy_surface_repair(
    config: Any, *, entry_id: str, node_id: str, domain: str, section: str,
    occurrence: int, published_position: int, expected_head: int,
    expected_registry_sha256: str, expected_document_sha256: str,
) -> SurfaceRepairPlan:
    """Read-only plan; exact legacy bytes are proven, not substring-searched."""
    journal_path, path, _roots = _paths(config, domain)
    raw = path.read_bytes()
    if _sha(raw) != expected_document_sha256:
        raise SurfaceRepairError("stale_document")
    with MemoryEventJournal.open_readonly(journal_path) as journal:
        final, start, end, events_hash = _proof(
            journal, raw, entry_id=entry_id, node_id=node_id, domain=domain, section=section,
            occurrence=occurrence, published_position=published_position, expected_head=expected_head,
            expected_registry_sha256=expected_registry_sha256,
        )
    replacement_end = len(final) - (len(raw) - end)
    return SurfaceRepairPlan(
        entry_id=entry_id, node_id=node_id, domain=domain, section=section, occurrence=occurrence,
        published_position=published_position, expected_head=expected_head,
        expected_registry_sha256=expected_registry_sha256,
        expected_document_sha256=expected_document_sha256, journal_path=str(journal_path),
        document_path=str(path), events_sha256=events_hash, after_sha256=_sha(final),
        span_start=start, span_end=end, before_span_sha256=_sha(raw[start:end]),
        after_span_sha256=_sha(final[start:replacement_end]),
    )


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_new(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_dir(path.parent)
    if path.read_bytes() != payload:
        raise SurfaceRepairError("backup_verification_failed")


def _atomic_document(path: Path, payload: bytes, expected: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".surface-repair-", suffix=".publish.tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        if _sha(path.read_bytes()) != expected:
            raise SurfaceRepairError("stale_document")
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_journal(journal_path: Path, destination: Path) -> str:
    # SQLite online backup captures committed WAL state, unlike copying the DB.
    with sqlite3.connect(journal_path.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SurfaceRepairError("journal_backup_invalid")
    os.chmod(destination, 0o600)
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())
    _sync_dir(destination.parent)
    return _sha(destination.read_bytes())


def _plan_arguments(plan: SurfaceRepairPlan) -> dict[str, Any]:
    return {name: getattr(plan, name) for name in (
        "entry_id", "node_id", "domain", "section", "occurrence", "published_position",
        "expected_head", "expected_registry_sha256", "expected_document_sha256",
    )}


def _operation(
    config: Any, plan: SurfaceRepairPlan, expected_plan_sha256: str,
    backup_root: Path, maintenance_confirmed: bool,
) -> tuple[Path, Path, Path]:
    if maintenance_confirmed is not True:
        raise SurfaceRepairError("maintenance_window_required")
    if plan.sha256 != expected_plan_sha256:
        raise SurfaceRepairError("plan_digest_mismatch")
    journal_path, path, roots = _paths(config, plan.domain)
    if str(path) != plan.document_path or str(journal_path) != plan.journal_path:
        raise SurfaceRepairError("plan_target_mismatch")
    root = _safe_path(backup_root)
    if not root.is_dir() or any(root == item or item in root.parents for item in roots):
        raise SurfaceRepairError("unsafe_backup_root")
    operation = _safe_path(root / plan.sha256, exists=False)
    return journal_path, path, operation


def _receipt(plan: SurfaceRepairPlan, operation: Path, status: str) -> SurfaceRepairReceipt:
    return SurfaceRepairReceipt(
        status=status, plan_sha256=plan.sha256, before_sha256=plan.expected_document_sha256,
        after_sha256=plan.after_sha256, operation_dir=str(operation),
    )


def _finalize(
    plan: SurfaceRepairPlan, operation: Path, journal: MemoryEventJournal, path: Path,
) -> SurfaceRepairReceipt:
    if journal.head_position() != plan.expected_head:
        raise SurfaceRepairError("stale_head")
    if repair_registry_sha256(journal) != plan.expected_registry_sha256:
        raise SurfaceRepairError("stale_registry")
    raw = path.read_bytes()
    if _sha(raw) != plan.after_sha256:
        raise SurfaceRepairError("repair_document_drift")
    _verify_document(raw, journal, plan.domain)
    receipt = _receipt(plan, operation, "repaired")
    target = _safe_path(operation / "receipt.json", exists=False)
    if target.exists():
        if target.read_bytes() != _json(asdict(receipt)):
            raise SurfaceRepairError("receipt_mismatch")
        return _receipt(plan, operation, "already_repaired")
    _durable_new(target, _json(asdict(receipt)))
    return receipt


def apply_legacy_surface_repair(
    config: Any, *, plan: SurfaceRepairPlan, expected_plan_sha256: str,
    backup_root: Path, maintenance_confirmed: bool,
    checkpoint: Callable[[str], None] | None = None,
) -> SurfaceRepairReceipt:
    """CAS apply/retry under the normal publication lock, with durable backups.

    ``checkpoint`` is an optional fault-injection/observer seam. Exceptions leave
    evidence intact; retry the same callable/plan to recover either atomic state.
    An operation directory without a durable intent is a refused partial backup,
    not permission to mutate. Keep the maintenance barrier until recovery ends.
    """
    journal_path, path, operation = _operation(
        config, plan, expected_plan_sha256, backup_root, maintenance_confirmed,
    )
    notify = checkpoint or (lambda _phase: None)
    with _publication_lock(config):
        with MemoryEventJournal.open_readonly(journal_path) as journal:
            if operation.exists():
                if any(_safe_path(operation / name, exists=False).exists()
                       for name in ("rollback.json", "rollback.intent.json")):
                    raise SurfaceRepairError("repair_rolled_back")
                _verify_backup(operation, plan)
                if _sha(path.read_bytes()) == plan.after_sha256:
                    return _finalize(plan, operation, journal, path)
                if _safe_path(operation / "receipt.json", exists=False).exists():
                    raise SurfaceRepairError("completed_repair_drift")
            fresh = plan_legacy_surface_repair(config, **_plan_arguments(plan))
            if fresh != plan:
                raise SurfaceRepairError("stale_plan")
            before = path.read_bytes()
            final, _start, _end, _events = _proof(
                journal, before, **{key: value for key, value in _plan_arguments(plan).items()
                                  if key != "expected_document_sha256"},
            )
            if not operation.exists():
                operation.mkdir(mode=0o700)
                _sync_dir(operation.parent)
                _durable_new(operation / "document.before", before)
                _durable_new(operation / "plan.json", _json(asdict(plan)))
                backup_hash = _backup_journal(journal_path, operation / "journal.before.db")
                with MemoryEventJournal.open_readonly(operation / "journal.before.db") as copied:
                    if (copied.head_position() != plan.expected_head
                            or repair_registry_sha256(copied) != plan.expected_registry_sha256
                            or [event.to_json() for event in copied.read_events()] !=
                            [event.to_json() for event in journal.read_events()]):
                        raise SurfaceRepairError("journal_backup_mismatch")
                _durable_new(operation / "intent.json", _json({
                    "plan_sha256": plan.sha256, "before_sha256": plan.expected_document_sha256,
                    "after_sha256": plan.after_sha256, "journal_backup_sha256": backup_hash,
                }))
            _verify_backup(operation, plan)
            notify("intent_durable")
            # Repeat whole proof after backups/observer, immediately before replace.
            if plan_legacy_surface_repair(config, **_plan_arguments(plan)) != plan:
                raise SurfaceRepairError("stale_plan")
            _atomic_document(path, final, plan.expected_document_sha256)
            notify("document_replaced")
            return _finalize(plan, operation, journal, path)


def _verify_backup(operation: Path, plan: SurfaceRepairPlan) -> None:
    for name in ("document.before", "plan.json", "journal.before.db", "intent.json"):
        _safe_path(operation / name)
    intent = json.loads((operation / "intent.json").read_bytes())
    if (intent.get("plan_sha256") != plan.sha256
            or intent.get("before_sha256") != plan.expected_document_sha256
            or intent.get("after_sha256") != plan.after_sha256
            or _sha((operation / "document.before").read_bytes()) != plan.expected_document_sha256
            or (operation / "plan.json").read_bytes() != _json(asdict(plan))
            or _sha((operation / "journal.before.db").read_bytes()) != intent.get("journal_backup_sha256")):
        raise SurfaceRepairError("backup_evidence_mismatch")


def recover_legacy_surface_repair(
    config: Any, *, plan: SurfaceRepairPlan, expected_plan_sha256: str,
    backup_root: Path, maintenance_confirmed: bool,
) -> SurfaceRepairReceipt:
    """Explicit recovery of either before/after hash; unrelated bytes refuse."""
    return apply_legacy_surface_repair(
        config, plan=plan, expected_plan_sha256=expected_plan_sha256,
        backup_root=backup_root, maintenance_confirmed=maintenance_confirmed,
    )


def rollback_legacy_surface_repair(
    config: Any, *, plan: SurfaceRepairPlan, expected_plan_sha256: str,
    backup_root: Path, maintenance_confirmed: bool, expected_current_sha256: str,
) -> SurfaceRepairReceipt:
    """Conditional document-only rollback; NEVER restore an old journal DB.

    Refuses intervening semantic changes, registry changes or file edits. The
    old legacy shape may again be refused by normal writes; refresh is required.
    """
    journal_path, path, operation = _operation(
        config, plan, expected_plan_sha256, backup_root, maintenance_confirmed,
    )
    if expected_current_sha256 != plan.after_sha256:
        raise SurfaceRepairError("rollback_expected_after_required")
    with _publication_lock(config):
        with MemoryEventJournal.open_readonly(journal_path) as journal:
            _verify_backup(operation, plan)
            if journal.head_position() != plan.expected_head:
                raise SurfaceRepairError("stale_head")
            if repair_registry_sha256(journal) != plan.expected_registry_sha256:
                raise SurfaceRepairError("stale_registry")
            before = (operation / "document.before").read_bytes()
            rollback_intent = _safe_path(operation / "rollback.intent.json", exists=False)
            intent_payload = _json({"plan_sha256": plan.sha256, "action": "rollback"})
            if rollback_intent.exists():
                if rollback_intent.read_bytes() != intent_payload:
                    raise SurfaceRepairError("rollback_intent_mismatch")
            else:
                if _sha(path.read_bytes()) != expected_current_sha256:
                    raise SurfaceRepairError("stale_document")
                _durable_new(rollback_intent, intent_payload)
            if _sha(path.read_bytes()) != plan.expected_document_sha256:
                _atomic_document(path, before, expected_current_sha256)
            if _sha(path.read_bytes()) != plan.expected_document_sha256:
                raise SurfaceRepairError("rollback_verification_failed")
            receipt = _receipt(plan, operation, "rolled_back")
            target = _safe_path(operation / "rollback.json", exists=False)
            if not target.exists():
                _durable_new(target, _json(asdict(receipt)))
            elif target.read_bytes() != _json(asdict(receipt)):
                raise SurfaceRepairError("rollback_receipt_mismatch")
            return receipt

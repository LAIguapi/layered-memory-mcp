"""Cleanup planning and the safe-apply boundary for L1 knowledge.

``governance`` finds lifecycle problems; this module turns them into a plan and
executes the part of that plan that is *mechanical*. The split is the point:

  • ``safe_auto_fixes``     — derived-data repairs and additive moves. Nothing
                              in L1 is deleted or rewritten: legacy ``.bak``
                              siblings move out of the knowledge dir, the L0
                              index / vector store are rebuilt from the files,
                              and open state markers are *copied* into the
                              existing TODO store (the L1 line stays).
  • ``needs_user_decision`` — anything that needs judgement: rewriting resolved
                              history as the current rule, merging a heading
                              family, splitting an oversized file.
  • ``must_not_auto_fix``   — markers that are kept deliberately (explicit
                              keep reason). Listed so nobody "fixes" them.

``apply_safe_knowledge_cleanup`` accepts only the action names in
``SAFE_ACTIONS``; everything else is rejected, never guessed. It defaults to a
dry run. There is no LLM call, no semantic rewrite and no cross-domain move
anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .backups import knowledge_backup_path, knowledge_backup_root
from .governance import (
    ACTION_KEEP,
    ACTION_MOVE_TO_TODO,
    ACTION_REWRITE,
    heading_families,
    knowledge_file_map,
    line_hash,
    rules_from_config,
    scan_state_markers_text,
)

if TYPE_CHECKING:
    from .config import MemoryConfig
    from .todo_store import TodoStore

logger = logging.getLogger(__name__)

ACTION_MIGRATE_BACKUPS = "migrate_legacy_backups"
ACTION_SYNC_L0 = "sync_l0_index"
ACTION_REINDEX = "reindex_vector_store"
ACTION_CREATE_TODO = "create_todo_from_marker"

SAFE_ACTIONS = frozenset({
    ACTION_MIGRATE_BACKUPS, ACTION_SYNC_L0, ACTION_REINDEX, ACTION_CREATE_TODO,
})

# Named so the rejection can say *why*, rather than "unknown action".
UNSAFE_ACTIONS = frozenset({
    ACTION_REWRITE, "rewrite_section", "delete_section", "delete_fact",
    "delete_file", "merge_sections", "merge_family", "consolidate_family",
    "move_section", "move_cross_domain", "split_domain", "remove_source_text",
})

POLICY = {
    "safe_auto_fixes": (
        "Mechanical only: move legacy .bak files out of knowledge/, rebuild "
        "derived L0/vector data from the files, copy open markers into the "
        "TODO store. No L1 text is deleted or rewritten."
    ),
    "never_automatic": [
        "deleting facts or sections",
        "merging semantically conflicting or same-topic sections",
        "moving content across domains / files",
        "rewriting resolved history as a current rule",
        "removing the source L1 line after a TODO is created",
    ],
}

SEE_ALSO = {
    "reconcile_knowledge_tool": "embedding-based cross-file duplicates and split candidates",
    "audit_rot_tool": "rot health score, stale sections, promotion candidates",
}

_TODO_ID_PREFIX = "gov-"
_TODO_CONTENT_CHARS = 1000

# List bullets, checkboxes and ordinals in front of a marker line.
_BULLET_RE = re.compile(r"^[\s>*\-+]*(?:\[[ xX]\]\s*)?(?:\d+[.)]\s*)?")

# Cheap screen so a TODO migration never copies something credential-shaped
# into another store. A hit only demotes the item to needs_user_decision.
_SECRET_LIKE_RE = re.compile(
    r"sk-[A-Za-z0-9_\-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?i:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}"
)


def _todo_id(file_label: str, stripped: str) -> str:
    """Deterministic id, so applying the same plan twice creates one TODO."""
    digest = hashlib.sha1(f"{file_label}\n{stripped.strip()}".encode("utf-8")).hexdigest()
    return _TODO_ID_PREFIX + digest[:16]


def _todo_text(stripped: str) -> str:
    return _BULLET_RE.sub("", stripped).strip()[:_TODO_CONTENT_CHARS]


def _todo_readiness(stripped: str, marker: str) -> str | None:
    """None when the line can become a TODO as-is, else the reason it can't."""
    if _SECRET_LIKE_RE.search(stripped):
        return "line looks like it contains a credential; migrate it by hand"
    rest = _todo_text(stripped)
    rest = re.sub(re.escape(marker), "", rest, flags=re.IGNORECASE)
    if len(re.sub(r"[\W_]+", "", rest)) < 3:
        return "marker line has too little text to stand alone as a TODO"
    return None


def find_legacy_backups(config: "MemoryConfig") -> list[Path]:
    """``*.md.bak`` files still sitting inside a knowledge dir (pre-v3.4.7)."""
    found: dict[str, Path] = {}
    for kdir in config.knowledge_dirs:
        kdir = Path(kdir)
        if not kdir.exists():
            continue
        for p in sorted(kdir.rglob("*.md.bak")):
            if p.is_file() and not p.is_symlink():
                found.setdefault(str(p.resolve()), p)
    return list(found.values())


# ---------------------------------------------------------------------------
# Planning (read-only)
# ---------------------------------------------------------------------------

def plan_knowledge_cleanup(config: "MemoryConfig", max_items: int = 50) -> dict:
    """Build the three-bucket cleanup plan. Never writes anything.

    Every ``safe_auto_fixes`` item is a ready-to-pass action for
    ``apply_safe_knowledge_cleanup``.
    """
    from .l0_manager import check_l0_l1_consistency
    from .recall import invalidate_scan_cache
    from .rot_auditor import OVERSIZED_BYTES

    for kdir in config.knowledge_dirs:
        invalidate_scan_cache(str(kdir))

    safe: list[dict] = []
    decide: list[dict] = []
    keep: list[dict] = []
    errors: list[str] = []

    # 1. Legacy .bak siblings in the knowledge dir.
    legacy = find_legacy_backups(config)
    if legacy:
        safe.append({
            "action": ACTION_MIGRATE_BACKUPS,
            "count": len(legacy),
            "files": [_rel_label(config, p) for p in legacy[:max_items]],
            "destination": str(knowledge_backup_root(config)),
            "reason": "live knowledge dir holds legacy .md.bak copies; move them "
                      "under the out-of-tree backup root",
        })

    # 2. L0 index drift (only when an L0 index file is configured at all).
    try:
        if getattr(config, "l0_index_file", None):
            cons = check_l0_l1_consistency(config)
            if cons.get("health") != "good":
                safe.append({
                    "action": ACTION_SYNC_L0,
                    "orphaned_l1": len(cons.get("orphaned_l1", [])),
                    "stale_l0_entries": len(cons.get("stale_l0_entries", [])),
                    "reason": "L0 index disagrees with the L1 files; regenerate it "
                              "from the files",
                })
    except Exception as e:  # noqa: BLE001 — one failed probe must not sink the plan
        errors.append(f"l0_consistency: {e}")

    # 3. Vector store coverage.
    try:
        from .injector import vector_store_needs_reindex

        if vector_store_needs_reindex(config):
            safe.append({
                "action": ACTION_REINDEX,
                "reason": "vectors.db is missing domains or sections present in "
                          "the files; rebuild the derived index",
            })
    except Exception as e:  # noqa: BLE001
        errors.append(f"vector_health: {e}")

    # 4. Per-file lifecycle findings, heading families and size.
    rules = rules_from_config(config)
    for label, path in sorted(knowledge_file_map(config).items()):
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            errors.append(f"{label}: {e}")
            continue
        scan = scan_state_markers_text(raw, label, rules)
        lines = raw.splitlines()
        for f in scan["lifecycle_findings"]:
            if f["action"] == ACTION_MOVE_TO_TODO:
                stripped = lines[f["line"] - 1].strip()
                blocker = _todo_readiness(stripped, f["marker"])
                if blocker is None:
                    safe.append({
                        "action": ACTION_CREATE_TODO,
                        "file": label,
                        "line": f["line"],
                        "line_hash": f["line_hash"],
                        "domain": f["domain"],
                        "marker": f["marker"],
                        "heading": f["heading"],
                        "text_preview": f["text_preview"],
                        "remove_source": False,
                        "reason": "open process state in durable L1; copy it into "
                                  "the TODO store (the L1 line is left in place)",
                    })
                else:
                    decide.append({"kind": ACTION_MOVE_TO_TODO, **f, "reason": blocker})
            elif f["action"] == ACTION_REWRITE:
                decide.append({
                    "kind": ACTION_REWRITE, **f,
                    "reason": "resolved/process-history line; rewrite it as the "
                              "rule that holds now (judgement call)",
                })
            elif f["action"] == ACTION_KEEP:
                keep.append({
                    "kind": ACTION_KEEP, **f,
                    "reason": "section carries an explicit keep reason",
                })
        for fam in heading_families(raw):
            decide.append({
                "kind": "merge_heading_family",
                "file": label,
                **fam,
                "reason": "same-skeleton sections; merge into one current-state "
                          "section (semantic merge — not automatic)",
            })
        size = len(raw.encode("utf-8"))
        if size > OVERSIZED_BYTES:
            decide.append({
                "kind": "oversized_file",
                "file": label,
                "bytes": size,
                "threshold": OVERSIZED_BYTES,
                "reason": "consolidate or split the file (content decision)",
            })

    totals = {
        "safe_auto_fixes": len(safe),
        "needs_user_decision": len(decide),
        "must_not_auto_fix": len(keep),
    }
    plan = {
        "read_only": True,
        "summary": {
            **totals,
            "truncated": any(n > max_items for n in totals.values()),
        },
        "safe_auto_fixes": safe[:max_items],
        "needs_user_decision": decide[:max_items],
        "must_not_auto_fix": keep[:max_items],
        "policy": POLICY,
        "see_also": SEE_ALSO,
        "how_to_apply": (
            "Pass chosen safe_auto_fixes items to apply_safe_knowledge_cleanup_tool"
            "(actions=[...], dry_run=True) to preview, then dry_run=False to apply."
        ),
    }
    if errors:
        plan["errors"] = errors
    return plan


def _rel_label(config: "MemoryConfig", path: Path) -> str:
    for kdir in config.knowledge_dirs:
        try:
            return str(Path(path).resolve().relative_to(Path(kdir).resolve()))
        except (ValueError, OSError):
            continue
    return Path(path).name


# ---------------------------------------------------------------------------
# Apply (dry-run by default)
# ---------------------------------------------------------------------------

def _coerce_actions(actions) -> tuple[list[dict], list[dict]]:
    """Normalise the caller's actions; returns (valid dicts, rejected entries)."""
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except ValueError:
            return [], [{"index": None, "error": "actions is a string but not valid JSON"}]
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        return [], [{"index": None, "error": "actions must be a list of action objects"}]
    out: list[dict] = []
    rejected: list[dict] = []
    for i, item in enumerate(actions):
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                item = {"action": item}
        if not isinstance(item, dict) or not isinstance(item.get("action"), str):
            rejected.append({"index": i, "error": "action object needs an 'action' string"})
            continue
        out.append({**item, "_index": i})
    return out, rejected


def apply_safe_knowledge_cleanup(
    config: "MemoryConfig",
    actions,
    dry_run: bool = True,
    todo_store_factory: Callable[[], "TodoStore"] | None = None,
) -> dict:
    """Execute recognised safe actions; reject everything else.

    Returns ``{dry_run, applied, would_apply, skipped, rejected, errors,
    summary}``. In a dry run nothing is written — not even the TODO database.
    """
    result: dict = {
        "dry_run": bool(dry_run),
        "applied": [],
        "would_apply": [],
        "skipped": [],
        "rejected": [],
        "errors": [],
    }
    items, bad = _coerce_actions(actions)
    result["rejected"].extend(bad)

    done_singletons: set[str] = set()
    for item in items:
        name = item["action"]
        idx = item.pop("_index")
        if name not in SAFE_ACTIONS:
            why = (
                "not a mechanical action — it needs a user decision"
                if name in UNSAFE_ACTIONS else "unknown action"
            )
            result["rejected"].append({"index": idx, "action": name, "error": why})
            continue
        if name != ACTION_CREATE_TODO:
            if name in done_singletons:
                result["skipped"].append({"index": idx, "action": name,
                                          "reason": "duplicate action in this request"})
                continue
            done_singletons.add(name)
        try:
            if name == ACTION_MIGRATE_BACKUPS:
                outcome = _apply_migrate_backups(config, dry_run)
            elif name == ACTION_SYNC_L0:
                outcome = _apply_sync_l0(config, dry_run)
            elif name == ACTION_REINDEX:
                outcome = _apply_reindex(config, dry_run, bool(item.get("force")))
            else:
                outcome = _apply_create_todo(config, item, dry_run, todo_store_factory)
        except Exception as e:  # noqa: BLE001 — one failure must not stop the batch
            logger.warning("cleanup action %s failed: %s", name, e)
            result["errors"].append({"index": idx, "action": name, "error": str(e)})
            continue
        status = outcome.pop("status")
        entry = {"index": idx, "action": name, **outcome}
        if status == "applied":
            result["would_apply" if dry_run else "applied"].append(entry)
        elif status == "rejected":
            result["rejected"].append(entry)
        elif status == "error":
            result["errors"].append(entry)
        else:
            result["skipped"].append(entry)

    result["summary"] = {
        k: len(result[k])
        for k in ("applied", "would_apply", "skipped", "rejected", "errors")
    }
    return result


def _apply_migrate_backups(config: "MemoryConfig", dry_run: bool) -> dict:
    legacy = find_legacy_backups(config)
    if not legacy:
        return {"status": "skipped", "reason": "no legacy .md.bak files in knowledge dirs"}
    moved: list[dict] = []
    left: list[dict] = []
    for bak in legacy:
        label = _rel_label(config, bak)
        source_md = bak.with_name(bak.name[: -len(".bak")])
        dest = knowledge_backup_path(config, source_md)
        if dest.exists():
            # A newer out-of-tree backup already owns the name; keep both.
            dest = dest.with_name(dest.name[: -len(".bak")] + ".legacy.bak")
        data = bak.read_bytes()
        if dest.exists():
            if dest.read_bytes() != data:
                left.append({"file": label, "reason": f"{dest.name} already exists with different content"})
                continue
            if not dry_run:
                bak.unlink()  # identical copy already preserved out of tree
            moved.append({"file": label, "to": str(dest), "deduplicated": True})
            continue
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if dest.read_bytes() != data:
                left.append({"file": label, "reason": "copy verification failed"})
                continue
            bak.unlink()
        moved.append({"file": label, "to": str(dest)})
    if not moved:
        return {"status": "skipped", "reason": "nothing could be moved", "left_in_place": left}
    return {"status": "applied", "moved": moved, "left_in_place": left}


def _apply_sync_l0(config: "MemoryConfig", dry_run: bool) -> dict:
    from .l0_manager import check_l0_l1_consistency, sync_l0_index

    if not getattr(config, "l0_index_file", None):
        return {"status": "skipped", "reason": "no L0 index file configured"}
    cons = check_l0_l1_consistency(config)
    if cons.get("health") == "good":
        return {"status": "skipped", "reason": "L0 index already consistent"}
    if dry_run:
        return {"status": "applied", "orphaned_l1": len(cons.get("orphaned_l1", [])),
                "stale_l0_entries": len(cons.get("stale_l0_entries", []))}
    report = sync_l0_index(config, dry_run=False)
    keep = {
        k: report[k]
        for k in ("success", "entries_added", "entries_removed", "entries_unchanged",
                  "total_entries", "bytes_written", "error")
        if k in report
    }
    return {"status": "applied" if report.get("success") else "error", "report": keep}


def _apply_reindex(config: "MemoryConfig", dry_run: bool, force: bool) -> dict:
    from .injector import reindex_vector_store, vector_store_needs_reindex

    if not force and not vector_store_needs_reindex(config):
        return {"status": "skipped", "reason": "vector store already covers every section"}
    if dry_run:
        return {"status": "applied", "forced": force}
    report = reindex_vector_store(config=config, drop_existing=True)
    keep = {k: report.get(k) for k in ("success", "domains", "sections", "files", "errors")}
    return {"status": "applied" if report.get("success") else "error", "report": keep}


def _todo_exists_readonly(db_path: Path, todo_id: str) -> bool:
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            return conn.execute(
                "SELECT 1 FROM todos WHERE id=? LIMIT 1", (todo_id,)
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def _apply_create_todo(
    config: "MemoryConfig",
    item: dict,
    dry_run: bool,
    todo_store_factory: Callable[[], "TodoStore"] | None,
) -> dict:
    from .models import TodoEntry, TodoPriority

    if item.get("remove_source"):
        return {"status": "rejected",
                "error": "remove_source is not supported: the L1 line always stays; "
                         "edit the file yourself once the TODO exists"}
    label = item.get("file")
    files = knowledge_file_map(config)
    if not isinstance(label, str) or label not in files:
        return {"status": "rejected", "error": f"unknown knowledge file: {label!r}"}
    priority = item.get("priority") or "medium"
    if priority not in {p.value for p in TodoPriority}:
        return {"status": "rejected", "error": f"invalid priority: {priority!r}"}
    want_hash = item.get("line_hash")
    if not isinstance(want_hash, str) or not want_hash:
        return {"status": "rejected", "error": "line_hash is required (take it from the plan)"}

    lines = Path(files[label]).read_text(encoding="utf-8").splitlines()
    lineno = item.get("line")
    target = None
    if isinstance(lineno, int) and 1 <= lineno <= len(lines) and line_hash(lines[lineno - 1]) == want_hash:
        target = lineno
    else:
        # The file moved under the plan; accept the line only if it is unique.
        hits = [i for i, ln in enumerate(lines, start=1) if line_hash(ln) == want_hash]
        if len(hits) == 1:
            target = hits[0]
    if target is None:
        return {"status": "skipped", "file": label,
                "reason": "stale plan: the source line changed or is ambiguous; re-plan"}

    stripped = lines[target - 1].strip()
    # Re-classify against the current file and rules, never trusting the plan.
    scan = scan_state_markers_text("\n".join(lines), label, rules_from_config(config))
    finding = next((f for f in scan["lifecycle_findings"] if f["line"] == target), None)
    if finding is None or finding["action"] != ACTION_MOVE_TO_TODO:
        return {"status": "skipped", "file": label, "line": target,
                "reason": "line is no longer an open state marker"}
    blocker = _todo_readiness(stripped, finding["marker"])
    if blocker:
        return {"status": "skipped", "file": label, "line": target, "reason": blocker}

    todo_id = _todo_id(label, stripped)
    db_path = Path(config.home) / "data" / "todos.db"
    if _todo_exists_readonly(db_path, todo_id):
        return {"status": "skipped", "file": label, "line": target, "todo_id": todo_id,
                "reason": "TODO already created from this line"}

    text = _todo_text(stripped)
    heading = finding["heading"]
    notes = (
        f"Migrated from knowledge/{label} line {target}"
        + (f" (## {heading})" if heading else "")
        + " by apply_safe_knowledge_cleanup; the L1 line was left in place."
    )
    out = {"file": label, "line": target, "todo_id": todo_id, "domain": finding["domain"],
           "title": text[:50], "source_retained": True}
    if dry_run:
        return {"status": "applied", **out}

    if todo_store_factory is None:
        from .todo_store import TodoStore

        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = TodoStore(db_path)
    else:
        store = todo_store_factory()
    if store.exists(todo_id):
        return {"status": "skipped", **out, "reason": "TODO already created from this line"}
    store.add(TodoEntry(
        id=todo_id,
        domain=finding["domain"],
        title=text[:50],
        content=text,
        priority=TodoPriority(priority),
        notes=notes,
    ))
    return {"status": "applied", **out}

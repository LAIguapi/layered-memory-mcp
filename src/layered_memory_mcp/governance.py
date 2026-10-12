"""Deterministic lifecycle governance for L1 knowledge files.

``inject_knowledge`` has semantic and family gates, but the whole-file tools
(``create_knowledge_file`` / ``update_knowledge_file``) wrote raw content and
said nothing about what they had just written. This module is the cheap,
advisory counterpart for those paths:

  • state markers — "待拍板", "尚未接入", "TODO"… are *process* state. They
    belong in the TODO store, or should be rewritten as the current rule once
    resolved; left in durable L1 they read as live facts long after the fact.
  • heading families — the same heading skeleton logged twice in one file
    (``## 发布流程 2026-10-01`` / ``## 发布流程 2026-10-12``), the classic
    append-without-merge shape.
  • size — the file has grown past the rot auditor's oversized threshold.

Everything here is read-only, regex/string based, and never calls a model or
an embedder. It only produces suggestions; nothing is moved or rewritten.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .heading import heading_skeleton

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger(__name__)

# Process-state markers that usually do not belong in durable L1. Longest
# first, so a line is attributed to "尚未接入" rather than its prefix "尚未".
STATE_MARKERS = sorted(
    [
        "待办", "下一步", "待拍板", "待验证", "待复测", "尚未接入", "尚未",
        "未落地", "待接", "TODO", "FIXME", "PENDING",
    ],
    key=len,
    reverse=True,
)

# A line that records the discharge of its own marker is history, not state.
COMPLETED_MARKERS = [
    "已完成", "已解决", "已闭环", "已生效", "已修复", "已归档", "已落地", "已确认",
]

# An explicit justification anywhere in the section keeps its markers.
_KEEP_REASON_RE = re.compile(r"^[\s>*\-+]*(?:\*\*)?(?:状态)?保留原因(?:\*\*)?\s*[:：]\s*\S")

# ASCII markers need word boundaries: "TODO" yes, "todo_store" / "TODOS" no.
_ASCII_MARKER_RES = {
    m: re.compile(rf"(?<![A-Za-z0-9_]){m}(?![A-Za-z0-9_])")
    for m in STATE_MARKERS
    if m.isascii()
}

# Same section boundaries as the rot auditor (## and ###).
_SECTION_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")

SUGGESTED_ACTION = "move_to_todo_or_rewrite_as_current_rule"
_PREVIEW_CHARS = 80


def _match_marker(line: str) -> str | None:
    for m in STATE_MARKERS:
        if m in _ASCII_MARKER_RES:
            if _ASCII_MARKER_RES[m].search(line):
                return m
        elif m in line:
            return m
    return None


def scan_state_markers_text(text: str, file_label: str) -> dict:
    """Scan one markdown document for state markers.

    Skips fenced code blocks and HTML comments (provenance lines). Returns
    ``{"summary": {...}, "findings": [...]}``; findings carry 1-based lines.
    """
    findings: list[dict] = []
    suppressed_completed = 0
    suppressed_keep = 0

    # Group lines into sections first, so a keep-reason line can cover a
    # marker that appears above it in the same section.
    sections: list[dict] = [{"heading": "", "keep": False, "hits": []}]
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith("<!--"):
            continue
        hm = _SECTION_HEADING_RE.match(stripped)
        if hm:
            sections.append({"heading": hm.group(2), "keep": False, "hits": []})
        cur = sections[-1]
        if _KEEP_REASON_RE.match(stripped):
            cur["keep"] = True
            continue
        marker = _match_marker(stripped)
        if marker is None:
            continue
        if any(cm in stripped for cm in COMPLETED_MARKERS):
            suppressed_completed += 1
            continue
        cur["hits"].append((lineno, marker, stripped))

    for sec in sections:
        if sec["keep"]:
            suppressed_keep += len(sec["hits"])
            continue
        for lineno, marker, stripped in sec["hits"]:
            findings.append({
                "file": file_label,
                "line": lineno,
                "marker": marker,
                "heading": sec["heading"][:60],
                "text_preview": stripped[:_PREVIEW_CHARS],
                "suggested_action": SUGGESTED_ACTION,
            })

    findings.sort(key=lambda f: f["line"])
    return {
        "summary": {
            "files_scanned": 1,
            "state_markers": len(findings),
            "suppressed_completed": suppressed_completed,
            "suppressed_keep_reason": suppressed_keep,
        },
        "findings": findings,
    }


def scan_state_markers(config: "MemoryConfig", max_findings: int = 50) -> dict:
    """Scan every L1 knowledge file for state markers (capped findings list)."""
    from .recall import scan_knowledge_files

    files: dict[str, str] = {}
    for kdir in config.knowledge_dirs:
        try:
            files.update(scan_knowledge_files(str(kdir)))
        except Exception:
            continue

    summary = {
        "files_scanned": 0,
        "files_with_markers": 0,
        "state_markers": 0,
        "suppressed_completed": 0,
        "suppressed_keep_reason": 0,
    }
    findings: list[dict] = []
    for name, path in sorted(files.items()):
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        res = scan_state_markers_text(raw, name)
        s = res["summary"]
        summary["files_scanned"] += 1
        summary["state_markers"] += s["state_markers"]
        summary["suppressed_completed"] += s["suppressed_completed"]
        summary["suppressed_keep_reason"] += s["suppressed_keep_reason"]
        if s["state_markers"]:
            summary["files_with_markers"] += 1
        findings.extend(res["findings"])

    return {
        "summary": summary,
        "findings": findings[:max_findings],
        "truncated": len(findings) > max_findings,
    }


def heading_families(text: str) -> list[dict]:
    """Same-skeleton heading groups (2+ members) within one document."""
    groups: dict[str, list[str]] = {}
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        hm = _SECTION_HEADING_RE.match(stripped)
        if not hm:
            continue
        skel = heading_skeleton(hm.group(2))
        if skel:
            groups.setdefault(skel, []).append(hm.group(2)[:60])
    return [
        {"skeleton": skel, "count": len(heads), "headings": heads}
        for skel, heads in groups.items()
        if len(heads) >= 2
    ]


def post_write_governance(
    config: "MemoryConfig",
    domain: str,
    filepath: Path,
    content: str | None = None,
    write_kind: str = "update",
    max_findings: int = 10,
) -> dict:
    """Advisory checks on a file that was just written. Never mutates.

    Never raises either: an internal failure comes back as ``{"error": ...}``
    so the caller can attach it without failing the primary write.
    """
    try:
        from .rot_auditor import OVERSIZED_BYTES

        if content is None:
            content = Path(filepath).read_text(encoding="utf-8")
        label = Path(filepath).name or f"{domain}.md"

        markers = scan_state_markers_text(content, label)
        families = heading_families(content)
        size = len(content.encode("utf-8"))
        oversized = size > OVERSIZED_BYTES

        suggestions: list[str] = []
        n_markers = markers["summary"]["state_markers"]
        if n_markers:
            suggestions.append(
                f"{n_markers} state marker(s) (待办/待拍板/TODO…) in durable L1 — "
                "move open items to the TODO store, or rewrite them as the current "
                "rule once resolved; add a '状态保留原因：…' line to keep one deliberately."
            )
        if families:
            suggestions.append(
                f"{len(families)} same-skeleton heading famil(ies) — merge the "
                "repeated sections into one current-state section."
            )
        if oversized:
            suggestions.append(
                f"file is {size} bytes (> {OVERSIZED_BYTES}) — consolidate or split."
            )

        return {
            "write_kind": write_kind,
            "domain": domain,
            "state_markers": {
                "summary": markers["summary"],
                "findings": markers["findings"][:max_findings],
            },
            "heading_families": families,
            "size": {
                "bytes": size,
                "threshold": OVERSIZED_BYTES,
                "oversized": oversized,
            },
            "suggestions": suggestions,
        }
    except Exception as e:  # noqa: BLE001 — governance is advisory only
        logger.warning("post-write governance failed for %s: %s", filepath, e)
        return {"error": str(e)}


def build_validation_extras(config: "MemoryConfig", max_findings: int = 10) -> dict:
    """Rot + state-marker summaries for ``validate_knowledge``.

    Each half degrades to an ``*_error`` key on failure; validation itself
    must never crash because a diagnostic did.
    """
    extras: dict = {}
    try:
        from .rot_auditor import audit_rot

        rot = audit_rot(config)
        extras["rot_summary"] = rot.get("summary")
        extras["rot_health_score"] = rot.get("health_score")
    except Exception as e:  # noqa: BLE001
        logger.warning("validate: audit_rot failed: %s", e)
        extras["rot_summary"] = None
        extras["rot_error"] = str(e)
    try:
        scan = scan_state_markers(config, max_findings=max_findings)
        extras["governance_summary"] = {
            "state_markers": scan["summary"],
            "top_findings": scan["findings"],
            "truncated": scan["truncated"],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("validate: state-marker scan failed: %s", e)
        extras["governance_summary"] = None
        extras["governance_error"] = str(e)
    return extras

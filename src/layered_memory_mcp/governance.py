"""Deterministic lifecycle governance for L1 knowledge files.

``inject_knowledge`` has semantic and family gates, but the whole-file tools
(``create_knowledge_file`` / ``update_knowledge_file``) wrote raw content and
said nothing about what they had just written. This module is the cheap,
advisory counterpart for those paths:

  • state markers — "待拍板", "尚未接入", "TODO"… are *process* state. Each
    marker line is classified into one lifecycle action:
      - ``move_to_todo``            open work → belongs in the TODO store;
      - ``rewrite_as_current_rule`` resolved history ("已修复", "has been
                                    fixed") → rewrite as the rule that holds;
      - ``keep_with_reason``        the section carries an explicit
                                    "状态保留原因：…" / "State retention reason: …".
    The marker lists are data (``DEFAULT_*`` below) and can be extended per
    deployment through the ``governance:`` section of ``config.yaml``.
  • procedure contamination — a section the persistence router reads as an
    action protocol (steps/commands/checklist) instead of durable knowledge.
    Those belong on the agent-owned SKILL surface; see ``routing``.
  • heading families — the same heading skeleton logged twice in one file
    (``## 发布流程 2026-10-01`` / ``## 发布流程 2026-10-12``), the classic
    append-without-merge shape.
  • size — the file has grown past the rot auditor's oversized threshold.

Everything here is read-only, regex/string based, and never calls a model or
an embedder. It only produces suggestions; nothing is moved or rewritten.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .heading import heading_skeleton

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Marker rules
#
# Every lifecycle decision below is driven by these lists — nothing else in the
# module knows a marker by name. To support another language or house style,
# extend them through ``config.yaml`` (see ``rules_from_config``) rather than
# editing code.
# ---------------------------------------------------------------------------

ACTION_MOVE_TO_TODO = "move_to_todo"
ACTION_REWRITE = "rewrite_as_current_rule"
ACTION_KEEP = "keep_with_reason"
LIFECYCLE_ACTIONS = (ACTION_MOVE_TO_TODO, ACTION_REWRITE, ACTION_KEEP)

# Open process state: work that is not done yet. It belongs in the TODO store,
# not in durable L1.
DEFAULT_OPEN_MARKERS = (
    # Chinese
    "待办", "下一步", "待拍板", "待决策", "待确认", "待验证", "待复测",
    "尚未接入", "尚未", "未落地", "未实现", "待接",
    # English. All-caps tags match case-sensitively; phrases ignore case.
    "TODO", "FIXME", "PENDING", "TBD",
    "next step", "next steps", "needs decision", "needs validation",
    "not implemented", "not yet implemented", "not integrated", "not yet integrated",
)

# Discharged process state: a line that records how something *got* done is
# history; it should usually be rewritten as the rule that now holds.
DEFAULT_RESOLVED_MARKERS = (
    "已完成", "已解决", "已闭环", "已生效", "已修复", "已归档", "已落地", "已确认",
    "DONE", "RESOLVED", "FIXED",
    "has been resolved", "was resolved", "has been fixed", "was fixed",
    "has been implemented", "was implemented", "has been completed",
    "was completed", "has been integrated", "was integrated",
)

# A "<label>: <reason>" line anywhere in a section keeps that section's markers.
DEFAULT_KEEP_REASON_LABELS = (
    "状态保留原因", "保留原因", "State retention reason", "Keep reason",
)

# Back-compat names (v3.4.7).
STATE_MARKERS = sorted(DEFAULT_OPEN_MARKERS, key=len, reverse=True)
COMPLETED_MARKERS = list(DEFAULT_RESOLVED_MARKERS)

# Same section boundaries as the rot auditor (## and ###).
_SECTION_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")

# Legacy single suggestion, still emitted on every open-marker finding.
SUGGESTED_ACTION = "move_to_todo_or_rewrite_as_current_rule"
_PREVIEW_CHARS = 80


def compile_marker(marker: str) -> re.Pattern:
    """Compile one marker.

    Non-ASCII markers (CJK) are plain substrings. ASCII markers need word
    boundaries — "TODO" yes, "todo_store" / "TODOS" no — and only all-caps
    tags are case-sensitive, so "Next step:" and "next step" both match.

    Public because ``routing`` compiles its own marker vocabulary with exactly
    these semantics; one implementation keeps the two from drifting.
    """
    if not marker.isascii():
        return re.compile(re.escape(marker))
    body = r"\s+".join(re.escape(w) for w in marker.split())
    flags = 0 if marker.isupper() else re.IGNORECASE
    return re.compile(rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])", flags)


# Former internal name, kept for back-compat.
_marker_regex = compile_marker


@dataclass(frozen=True)
class GovernanceRules:
    """Compiled marker rules. Build with ``build_rules`` / ``rules_from_config``."""

    open_markers: tuple[tuple[str, re.Pattern], ...]
    resolved_markers: tuple[tuple[str, re.Pattern], ...]
    keep_reason_re: re.Pattern

    def match_open(self, line: str) -> str | None:
        for marker, rx in self.open_markers:
            if rx.search(line):
                return marker
        return None

    def match_resolved(self, line: str) -> str | None:
        for marker, rx in self.resolved_markers:
            if rx.search(line):
                return marker
        return None

    def keep_reason(self, line: str) -> str | None:
        m = self.keep_reason_re.match(line)
        return m.group("reason").strip() if m else None


def _merge(defaults: Iterable[str], extra: Iterable[str], disabled: set[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in (*defaults, *extra):
        m = str(m).strip()
        if m and m not in seen and m not in disabled:
            seen.add(m)
            out.append(m)
    # Longest first, so a line is attributed to "尚未接入" rather than "尚未".
    return sorted(out, key=len, reverse=True)


@lru_cache(maxsize=32)
def build_rules(
    extra_open: tuple[str, ...] = (),
    extra_resolved: tuple[str, ...] = (),
    extra_keep_labels: tuple[str, ...] = (),
    disabled: tuple[str, ...] = (),
) -> GovernanceRules:
    """Defaults plus extras, minus anything listed in ``disabled``."""
    off = {d.strip() for d in disabled if d.strip()}
    opens = _merge(DEFAULT_OPEN_MARKERS, extra_open, off)
    resolved = _merge(DEFAULT_RESOLVED_MARKERS, extra_resolved, off)
    labels = _merge(DEFAULT_KEEP_REASON_LABELS, extra_keep_labels, off)
    label_alt = "|".join(r"\s*".join(map(re.escape, lbl.split())) for lbl in labels) or r"(?!)"
    keep_re = re.compile(
        rf"^[\s>*\-+]*(?:\*\*)?(?:{label_alt})(?:\*\*)?\s*[:：]\s*(?P<reason>\S.*)$",
        re.IGNORECASE,
    )
    return GovernanceRules(
        open_markers=tuple((m, _marker_regex(m)) for m in opens),
        resolved_markers=tuple((m, _marker_regex(m)) for m in resolved),
        keep_reason_re=keep_re,
    )


def rules_from_config(config: "MemoryConfig | None") -> GovernanceRules:
    """Rules for ``config`` (its ``governance:`` extensions over the defaults)."""
    if config is None:
        return build_rules()

    def _t(name: str) -> tuple[str, ...]:
        return tuple(getattr(config, name, None) or ())

    return build_rules(
        _t("governance_open_markers"),
        _t("governance_resolved_markers"),
        _t("governance_keep_reason_labels"),
        _t("governance_disabled_markers"),
    )


def line_hash(text: str) -> str:
    """Short, stable fingerprint of a stripped line (staleness check for plans)."""
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:12]


def domain_of(file_label: str) -> str:
    base = file_label.replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base


def _match_marker(line: str) -> str | None:
    return build_rules().match_open(line)


def scan_state_markers_text(
    text: str, file_label: str, rules: GovernanceRules | None = None
) -> dict:
    """Scan one markdown document for state markers.

    Skips fenced code blocks and HTML comments (provenance lines). Returns
    ``{"summary": {...}, "findings": [...], "lifecycle_findings": [...]}``;
    findings carry 1-based lines.

    ``findings`` keeps its v3.4.7 meaning — open markers that should leave
    durable L1 — now with an ``action`` of ``move_to_todo``.
    ``lifecycle_findings`` is the full classification: every marker line with
    exactly one of ``move_to_todo`` / ``rewrite_as_current_rule`` /
    ``keep_with_reason``.
    """
    rules = rules or build_rules()
    domain = domain_of(file_label)
    suppressed_completed = 0
    suppressed_keep = 0

    # Group lines into sections first, so a keep-reason line can cover a
    # marker that appears above it in the same section.
    sections: list[dict] = [{"heading": "", "keep": None, "hits": []}]
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
            sections.append({"heading": hm.group(2), "keep": None, "hits": []})
        cur = sections[-1]
        reason = rules.keep_reason(stripped)
        if reason is not None:
            cur["keep"] = cur["keep"] or reason
            continue
        marker = rules.match_open(stripped)
        resolved = rules.match_resolved(stripped)
        if marker is None and resolved is None:
            continue
        if marker is not None and resolved is not None:
            suppressed_completed += 1
        cur["hits"].append((lineno, marker, resolved, stripped))

    findings: list[dict] = []
    lifecycle: list[dict] = []
    for sec in sections:
        for lineno, marker, resolved, stripped in sec["hits"]:
            item = {
                "file": file_label,
                "domain": domain,
                "line": lineno,
                "marker": marker or resolved,
                "heading": sec["heading"][:60],
                "text_preview": stripped[:_PREVIEW_CHARS],
                "line_hash": line_hash(stripped),
            }
            if resolved is not None:
                item["resolved_marker"] = resolved
            if sec["keep"]:
                if marker is not None and resolved is None:
                    suppressed_keep += 1
                item["action"] = ACTION_KEEP
                item["keep_reason"] = sec["keep"][:_PREVIEW_CHARS]
            elif resolved is not None:
                item["action"] = ACTION_REWRITE
            else:
                item["action"] = ACTION_MOVE_TO_TODO
                findings.append({**item, "suggested_action": SUGGESTED_ACTION})
            lifecycle.append(item)

    findings.sort(key=lambda f: f["line"])
    lifecycle.sort(key=lambda f: f["line"])
    by_action = {a: 0 for a in LIFECYCLE_ACTIONS}
    for item in lifecycle:
        by_action[item["action"]] += 1
    return {
        "summary": {
            "files_scanned": 1,
            "state_markers": len(findings),
            "suppressed_completed": suppressed_completed,
            "suppressed_keep_reason": suppressed_keep,
            "by_action": by_action,
        },
        "findings": findings,
        "lifecycle_findings": lifecycle,
    }


def knowledge_file_map(config: "MemoryConfig") -> dict[str, str]:
    """``{relative label: path}`` across the knowledge dirs, namespace first.

    On a name clash the namespace file wins — the same precedence as recall
    and the vector rebuild.
    """
    from .recall import scan_knowledge_files

    files: dict[str, str] = {}
    for kdir in config.knowledge_dirs:
        try:
            for label, path in scan_knowledge_files(str(kdir)).items():
                files.setdefault(label, path)
        except Exception:
            continue
    return files


def scan_state_markers(config: "MemoryConfig", max_findings: int = 50) -> dict:
    """Scan every L1 knowledge file for state markers (capped findings lists)."""
    files = knowledge_file_map(config)
    rules = rules_from_config(config)
    summary = {
        "files_scanned": 0,
        "files_with_markers": 0,
        "state_markers": 0,
        "suppressed_completed": 0,
        "suppressed_keep_reason": 0,
        "by_action": {a: 0 for a in LIFECYCLE_ACTIONS},
    }
    findings: list[dict] = []
    lifecycle: list[dict] = []
    for name, path in sorted(files.items()):
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        res = scan_state_markers_text(raw, name, rules)
        s = res["summary"]
        summary["files_scanned"] += 1
        summary["state_markers"] += s["state_markers"]
        summary["suppressed_completed"] += s["suppressed_completed"]
        summary["suppressed_keep_reason"] += s["suppressed_keep_reason"]
        for a, n in s["by_action"].items():
            summary["by_action"][a] += n
        if s["state_markers"]:
            summary["files_with_markers"] += 1
        findings.extend(res["findings"])
        lifecycle.extend(res["lifecycle_findings"])

    return {
        "summary": summary,
        "findings": findings[:max_findings],
        "truncated": len(findings) > max_findings,
        "lifecycle_findings": lifecycle[:max_findings],
        "lifecycle_truncated": len(lifecycle) > max_findings,
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


def _iter_sections(text: str) -> list[tuple[str, int, str]]:
    """``[(heading, 1-based heading line, body), ...]`` for one document.

    Headings inside fenced code blocks are not headings. The preamble before the
    first heading comes back with an empty heading.
    """
    sections: list[tuple[str, int, list[str]]] = [("", 0, [])]
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            sections[-1][2].append(line)
            continue
        if not in_fence:
            hm = _SECTION_HEADING_RE.match(stripped)
            if hm:
                sections.append((hm.group(2), lineno, []))
                continue
        sections[-1][2].append(line)
    return [(h, n, "\n".join(body)) for h, n, body in sections]


# A section must reach this routing score before it is reported as procedure
# contamination. Advisory either way; the floor keeps the noise down.
PROCEDURE_CONTAMINATION_SCORE = 0.6


def scan_procedure_contamination(
    config: "MemoryConfig | None",
    text: str,
    file_label: str,
    max_findings: int = 10,
) -> dict:
    """Report L1 sections that the router reads as agent-owned action protocols.

    Durable L1 is for the facts and rules that hold; a runbook ("step 1 … step
    2 …", a command sequence, a checklist) is an *action protocol* and belongs on
    the agent-owned SKILL surface. This finds sections whose markers **and**
    structure both say "procedure" — marker-only matches ("验证", "流程" inside an
    architecture note) are deliberately not reported.

    Advisory only: nothing is moved, and the framework cannot write the SKILL
    surface unless the operator has opted in through ``agent_extensions``.
    """
    from .routing import MemorySurface, classify_memory_item

    domain = domain_of(file_label)
    findings: list[dict] = []
    for heading, lineno, body in _iter_sections(text):
        if not heading or not body.strip():
            continue
        decision = classify_memory_item(
            body, config=config, domain=domain, section=heading
        )
        if (
            decision.surface is not MemorySurface.SKILL
            or decision.score < PROCEDURE_CONTAMINATION_SCORE
        ):
            continue
        findings.append({
            "file": file_label,
            "domain": domain,
            "heading": heading[:60],
            "line": lineno,
            "surface": decision.surface.value,
            "ownership": decision.ownership.value,
            "score": decision.score,
            "confidence_basis": decision.confidence_basis,
            "signals": [s.name for s in decision.signals if s.kind != "negative"],
            "suggested_action": "extract_to_agent_skill_surface",
            "framework_can_apply": decision.framework_can_apply,
        })
    findings.sort(key=lambda f: (-f["score"], f["line"]))
    return {
        "summary": {
            "sections_flagged": len(findings),
            "score_floor": PROCEDURE_CONTAMINATION_SCORE,
        },
        "findings": findings[:max_findings],
        "truncated": len(findings) > max_findings,
    }


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

        markers = scan_state_markers_text(content, label, rules_from_config(config))
        families = heading_families(content)
        size = len(content.encode("utf-8"))
        oversized = size > OVERSIZED_BYTES
        procedures = scan_procedure_contamination(
            config, content, label, max_findings=max_findings
        )

        suggestions: list[str] = []
        n_markers = markers["summary"]["state_markers"]
        n_rewrite = markers["summary"]["by_action"][ACTION_REWRITE]
        if n_markers:
            suggestions.append(
                f"{n_markers} state marker(s) (待办/待拍板/TODO…) in durable L1 — "
                "move open items to the TODO store, or rewrite them as the current "
                "rule once resolved; add a '状态保留原因：…' / 'State retention "
                "reason: …' line to keep one deliberately."
            )
        if n_rewrite:
            suggestions.append(
                f"{n_rewrite} resolved/process-history line(s) (已完成/已修复/"
                "has been fixed…) — rewrite them as the rule that holds now."
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
        n_proc = procedures["summary"]["sections_flagged"]
        if n_proc:
            suggestions.append(
                f"{n_proc} section(s) read as action protocols (steps/commands/"
                "checklists) rather than durable knowledge — they belong on the "
                "agent-owned SKILL surface; L1 keeps the facts and rules."
            )

        return {
            "write_kind": write_kind,
            "domain": domain,
            "state_markers": {
                "summary": markers["summary"],
                "findings": markers["findings"][:max_findings],
                "lifecycle_findings": markers["lifecycle_findings"][:max_findings],
            },
            "procedure_contamination": procedures,
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
            "lifecycle_findings": scan["lifecycle_findings"],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("validate: state-marker scan failed: %s", e)
        extras["governance_summary"] = None
        extras["governance_error"] = str(e)
    return extras

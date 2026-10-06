"""
Rot Auditor — Knowledge base health / decay detection.

A read-only diagnostic that surfaces the four common decay pathologies
observed in long-lived layered-memory knowledge bases:

  P1  oversized        — files that have grown past the recommended size,
                         often from "append but never merge" accumulation.
  P2  garbled_heading  — section headings that lost their punctuation/spaces
                         (long run of characters with no separators), usually
                         from an early summariser bug or hand-edited memory.
  P3  stale            — sections carrying a *forward-looking* promise
                         ("下次执行", "待测试", "尚未…") whose date has already
                         passed, on the same line. Retrospective notes that
                         merely carry an observation date are history, not rot.
  P4  cross_dup        — near-duplicate sections living in different files,
                         i.e. the same knowledge defined in more than one place.

The auditor never modifies anything. It returns a structured report so a
human (or a higher-level agent) can decide what to consolidate, fix, or drop.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import MemoryConfig

# Thresholds
OVERSIZED_BYTES = 4096          # files above this are flagged (P1)
GARBLED_MIN_LEN = 30           # heading length above which we check for garbling
CROSS_DUP_SIMILARITY = 0.82    # section-pair similarity to flag as duplicate (P4)

# Forward-looking status markers (P3). Each one promises an action that carries
# a deadline, so a past date next to it means the promise is overdue.
#
# Retrospective qualifiers such as 临时 / 暂时 are deliberately absent: they
# describe the *scope* of a note, not a pending action, and pairing them with an
# observation date ("（2026-09-11 实测）") flagged every dated lesson in the
# library — a measured 100% false-positive rate, 18 of 56 points in the score.
_PENDING_MARKERS = [
    "下次执行", "下次", "待测试", "待后续", "待实施", "待验证", "待复测", "待定",
    "尚未", "未完成", "未应用", "未修", "未落地", "稍后", "先放", "如仍触发",
    "TODO", "FIXME", "PENDING",
]

# A promise that was already discharged cannot be overdue. Lines that record a
# completion ("…TODO dd1eb34c 已完成") carry both a pending marker and a past
# date, but they are history — flagging them was the last false-positive class
# left after v3.3.3.
_COMPLETED_MARKERS = [
    "已完成", "已解决", "已闭环", "已生效", "已修复", "已归档", "已发布",
    "已交付", "已核实", "已确认",
]

# Date patterns to detect expired content (P3)
_DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)

# Heading noise stripped before comparing two headings as "the same topic" (P4b):
# parenthetical qualifiers, dates, 期号/版次, versions and bare years/ids.
# Heading normalisation lives in one place: the read side (this module) and the
# write side (injector) must agree on what "the same topic, logged again" means.
from .heading import (  # noqa: F401 — re-exported for backwards compatibility
    HEADING_NOISE_RE,
    heading_skeleton as _heading_skeleton,
)

_HEADING_NOISE_RE = HEADING_NOISE_RE


def audit_rot(config: "MemoryConfig") -> dict:
    """Scan all L1 knowledge files and report decay signals.

    Returns a dict with per-pathology findings plus an overall health score.
    Read-only — makes no changes.
    """
    from .recall import scan_knowledge_files

    # Collect files across every knowledge dir (namespace + shared)
    files: dict[str, str] = {}
    for kdir in config.knowledge_dirs:
        try:
            files.update(scan_knowledge_files(str(kdir)))
        except Exception:
            continue

    oversized: list[dict] = []
    garbled: list[dict] = []
    stale: list[dict] = []

    # Per-section index for cross-file duplicate detection
    sections: list[dict] = []  # {file, heading, body, norm}

    today = date.today()

    for name, path in sorted(files.items()):
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        size = len(raw.encode("utf-8"))

        # P1 — oversized
        if size > OVERSIZED_BYTES:
            oversized.append({"file": name, "size_bytes": size,
                              "size_kb": round(size / 1024, 1)})

        # Walk sections
        matches = list(_HEADING_RE.finditer(raw))
        for i, m in enumerate(matches):
            heading = m.group(2).strip()
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            body = raw[body_start:body_end].strip()

            # P2 — garbled heading: long, and almost no separators.
            # A genuine heading has spaces/punctuation; a garbled one (from a
            # summariser that ate punctuation) runs characters together.
            if len(heading) >= GARBLED_MIN_LEN:
                seps = sum(heading.count(c) for c in
                           " ，,。.、:：/-_（）()—「」【】《》·|")
                # also count ASCII-letter word boundaries (CamelCase / spaces)
                ascii_words = len(re.findall(r"[A-Za-z][a-z]+", heading))
                sep_score = (seps + ascii_words) / max(len(heading), 1)
                if sep_score < 0.08:
                    garbled.append({"file": name, "heading": heading[:60],
                                    "length": len(heading)})

            # P3 — stale: a *forward-looking* marker and an expired date ON THE
            # SAME LINE. All three conditions matter:
            #   • forward-looking marker — a pending action can go overdue; a
            #     retrospective note cannot. (Requiring this is what removed the
            #     100%-false-positive behaviour where every dated lesson was
            #     flagged for the mere presence of 临时/暂时.)
            #   • a past date — the deadline has passed.
            #   • same line — otherwise an unrelated observation date elsewhere
            #     in the lead ("…（2026-09-11 实测）") would convict a note whose
            #     marker points somewhere else entirely.
            # The shape that survives all three is "下次执行 2026-08-01".
            stale_hit: tuple[str, str] | None = None
            for line in (heading, *body.split("\n")[:2]):
                # A line that records the discharge of its own promise is
                # history, not rot ("…TODO dd1eb34c 已完成（2026-07-26）").
                if any(cm in line for cm in _COMPLETED_MARKERS):
                    continue
                marker_hit = next((mk for mk in _PENDING_MARKERS if mk in line), None)
                if not marker_hit:
                    continue
                for dm in _DATE_RE.finditer(line):
                    try:
                        dt = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
                    except ValueError:
                        continue
                    if dt < today:
                        stale_hit = (marker_hit, dt.isoformat())
                        break
                if stale_hit:
                    break
            if stale_hit:
                stale.append({
                    "file": name,
                    "heading": heading[:60],
                    "reason": f"pending '{stale_hit[0]}' + expired date {stale_hit[1]}",
                })

            # collect for cross-dup (skip tiny / pure-pointer sections)
            if len(body) >= 40 and not body.startswith("[L0]"):
                sections.append({
                    "file": name,
                    "heading": heading[:50],
                    "norm": _normalize(body),
                    "skeleton": _heading_skeleton(heading),
                })

    # P4 — near-duplicate sections, split into cross-file and same-file.
    # Same-file duplicates are the classic "append but never merge" rot
    # (e.g. dual-write leaving two copies of the same section); cross-file
    # duplicates mean the same knowledge lives in more than one file.
    #
    # Two detection routes, because they catch different shapes:
    #   a) body similarity — ordered cheapest-first. A naive O(n²) loop calling
    #      ratio() on every pair is what pushed this audit past the 60s MCP
    #      client timeout on a ~800-section store. The length-ratio bound is
    #      exact (ratio() <= 2*min/(la+lb)), then come the O(1)/O(n)
    #      SequenceMatcher upper bounds, and only then the real ratio().
    #   b) same heading skeleton — identical title once dates/versions/
    #      parentheticals are stripped. A re-appended copy is often much shorter
    #      than the original, so body similarity alone misses it; same-file
    #      same-skeleton pairs are rot regardless of body length. Restricted to
    #      same-file pairs: across files an identical generic heading is often
    #      legitimate.
    cross_dup: list[dict] = []
    same_file_dup: list[dict] = []

    for a in range(len(sections)):
        sa = sections[a]
        na = sa["norm"]
        la = len(na)
        for b in range(a + 1, len(sections)):
            sb = sections[b]
            nb = sb["norm"]

            sim: float | None = None
            if _length_gate(la, len(nb)):
                matcher = SequenceMatcher(None, na, nb)
                if (matcher.real_quick_ratio() >= CROSS_DUP_SIMILARITY
                        and matcher.quick_ratio() >= CROSS_DUP_SIMILARITY):
                    ratio = matcher.ratio()
                    if ratio >= CROSS_DUP_SIMILARITY:
                        sim = ratio

            same_file = sa["file"] == sb["file"]
            skeleton_hit = bool(sa["skeleton"]) and same_file and sa["skeleton"] == sb["skeleton"]
            if sim is None and not skeleton_hit:
                continue

            if sim is None:
                reason = "same heading skeleton"
            elif skeleton_hit:
                reason = "near-identical body + same heading skeleton"
            else:
                reason = "near-identical body"
            entry = {
                "similarity": round(sim, 2) if sim is not None else None,
                "reason": reason,
                "a": {"file": sa["file"], "heading": sa["heading"]},
                "b": {"file": sb["file"], "heading": sb["heading"]},
            }
            if same_file:
                same_file_dup.append(entry)
            else:
                cross_dup.append(entry)

    cross_dup.sort(key=lambda x: x["similarity"] or 0.0, reverse=True)
    same_file_dup.sort(key=lambda x: x["similarity"] or 0.0, reverse=True)

    # Promotion candidates (v2.10.0) — same-topic clusters in watched catch-all
    # domains that deserve extraction into their own L1 file. Advisory only.
    promotion_candidates = _detect_promotion_candidates(config, files)

    # Health score: start at 100, dock points per finding (capped)
    score = 100
    score -= min(len(oversized) * 4, 24)
    score -= min(len(garbled) * 6, 24)
    score -= min(len(stale) * 3, 18)
    score -= min(len(cross_dup) * 5, 30)
    score -= min(len(same_file_dup) * 5, 24)
    # Promotion is an optimisation suggestion, not decay — dock lightly, and
    # with a lower weight/cap than oversized (its closest cousin).
    score -= min(len(promotion_candidates) * 2, 10)
    score = max(score, 0)

    return {
        "success": True,
        "health_score": score,
        "total_files": len(files),
        "total_sections": len(sections),
        "findings": {
            "oversized": oversized,
            "garbled_heading": garbled,
            "stale": stale,
            "cross_file_duplicate": cross_dup,
            "same_file_duplicate": same_file_dup,
            "promotion_candidates": promotion_candidates,
        },
        "summary": {
            "oversized": len(oversized),
            "garbled_heading": len(garbled),
            "stale": len(stale),
            "cross_file_duplicate": len(cross_dup),
            "same_file_duplicate": len(same_file_dup),
            "promotion_candidates": len(promotion_candidates),
        },
        "recommendations": _build_recommendations(
            oversized, garbled, stale, cross_dup, same_file_dup, promotion_candidates
        ),
    }


def _detect_promotion_candidates(config: "MemoryConfig", files: dict[str, str]) -> list[dict]:
    """Run the promotion detector over each watched catch-all domain file.

    Read-only. Any failure degrades to an empty list — never breaks the audit.
    Returns a list of candidate dicts (see promotion.detect_promotion_candidate).
    """
    if not getattr(config, "promotion_enabled", True):
        return []

    watch = getattr(config, "promotion_watch_domains", ["misc"]) or []
    candidates: list[dict] = []
    try:
        from .promotion import detect_promotion_candidate
    except Exception:
        return []

    for name, path in sorted(files.items()):
        domain = name.removesuffix(".md")
        if domain not in watch:
            continue
        try:
            hit = detect_promotion_candidate(config, domain, Path(path))
        except Exception:
            continue
        if hit is not None:
            candidates.append(hit)
    return candidates


def _length_gate(la: int, lb: int) -> bool:
    """Exact upper bound on SequenceMatcher.ratio() for two lengths.

    ``ratio() = 2*M/T`` with ``M <= min(la, lb)`` and ``T = la + lb``, so
    ``2*min/(la+lb)`` can never be exceeded. When that bound is already below
    the threshold, no comparison of these two lengths can match and the
    expensive work is skipped — this is what keeps the audit inside the MCP
    client timeout on a large store.
    """
    if la <= 0 or lb <= 0:
        return False
    return (2 * min(la, lb)) / (la + lb) >= CROSS_DUP_SIMILARITY


def _normalize(text: str) -> str:
    """Normalize section body for similarity comparison."""
    t = re.sub(r"\s+", " ", text.lower())
    t = re.sub(r"[*_`#>\-]", "", t)
    return t.strip()[:500]  # cap for speed


def _build_recommendations(oversized, garbled, stale, cross_dup, same_file_dup, promotion_candidates=None) -> list[str]:
    recs: list[str] = []
    promotion_candidates = promotion_candidates or []
    if oversized:
        recs.append(
            f"{len(oversized)} oversized file(s) — review for 'append-without-merge' "
            "accumulation; consolidate repeated sections."
        )
    if garbled:
        recs.append(
            f"{len(garbled)} garbled heading(s) — likely lost punctuation; "
            "rewrite the heading to a concise human-readable form."
        )
    if stale:
        recs.append(
            f"{len(stale)} possibly-stale section(s) — contains transient markers "
            "or past dates; verify and recycle if obsolete."
        )
    if same_file_dup:
        recs.append(
            f"{len(same_file_dup)} same-file duplicate pair(s) — classic "
            "'append but never merge' rot (often a dual-write leaving two copies); "
            "merge the duplicate sections into one."
        )
    if cross_dup:
        recs.append(
            f"{len(cross_dup)} cross-file duplicate pair(s) — same knowledge in "
            "multiple files; pick a single authoritative source and replace the rest "
            "with a pointer."
        )
    if promotion_candidates:
        domains = ", ".join(
            sorted({c.get("watch_domain", "?") for c in promotion_candidates})
        )
        recs.append(
            f"{len(promotion_candidates)} promotion candidate(s) in catch-all "
            f"domain(s) [{domains}] — a same-topic cluster has accumulated; consider "
            "extracting it into its own L1 file with create_knowledge_file instead of "
            "letting it keep piling up."
        )
    if not recs:
        recs.append("No significant decay detected. Knowledge base is healthy.")
    return recs

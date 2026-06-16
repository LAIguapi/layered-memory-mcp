"""
Rot Auditor — Knowledge base health / decay detection.

A read-only diagnostic that surfaces the four common decay pathologies
observed in long-lived layered-memory knowledge bases:

  P1  oversized        — files that have grown past the recommended size,
                         often from "append but never merge" accumulation.
  P2  garbled_heading  — section headings that lost their punctuation/spaces
                         (long run of characters with no separators), usually
                         from an early summariser bug or hand-edited memory.
  P3  stale            — sections carrying an expired date or a transient
                         marker ("下次执行", "待测试", "TODO", "临时") that
                         should have been recycled.
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

# Transient markers suggesting a section was meant to be temporary (P3)
_TRANSIENT_MARKERS = [
    "下次执行", "待测试", "临时", "TODO", "待后续", "待实施",
    "暂时", "先放", "稍后", "如仍触发", "兜底拆分",
]

# Date patterns to detect expired content (P3)
_DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)


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

            # P3 — stale: a transient state marker AND an expired date.
            # Requiring both keeps false positives low — a section that merely
            # mentions "TODO" or "临时" in passing (e.g. the P2 difficulty tier,
            # a standing TODO list) is NOT stale. Real rot is a time-bound
            # status note ("下次执行 5/22") whose date has passed.
            head_and_lead = heading + "\n" + "\n".join(body.split("\n")[:2])
            marker_hit = next((mk for mk in _TRANSIENT_MARKERS if mk in head_and_lead), None)
            expired = None
            for dm in _DATE_RE.finditer(head_and_lead):
                try:
                    y, mo, d = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
                    dt = date(y, mo, d)
                    if dt < today:
                        expired = dt.isoformat()
                except ValueError:
                    continue
            # Flag only when a transient marker co-occurs with a past date in
            # the heading/lead — the high-confidence "stale status note" shape.
            if marker_hit and expired:
                stale.append({
                    "file": name,
                    "heading": heading[:60],
                    "reason": f"transient '{marker_hit}' + expired date {expired}",
                })

            # collect for cross-dup (skip tiny / pure-pointer sections)
            if len(body) >= 40 and not body.startswith("[L0]"):
                sections.append({
                    "file": name,
                    "heading": heading[:50],
                    "norm": _normalize(body),
                })

    # P4 — near-duplicate sections, split into cross-file and same-file.
    # Same-file duplicates are the classic "append but never merge" rot
    # (e.g. dual-write leaving two copies of the same section); cross-file
    # duplicates mean the same knowledge lives in more than one file.
    cross_dup: list[dict] = []
    same_file_dup: list[dict] = []
    for a in range(len(sections)):
        for b in range(a + 1, len(sections)):
            sa, sb = sections[a], sections[b]
            sim = SequenceMatcher(None, sa["norm"], sb["norm"]).ratio()
            if sim < CROSS_DUP_SIMILARITY:
                continue
            entry = {
                "similarity": round(sim, 2),
                "a": {"file": sa["file"], "heading": sa["heading"]},
                "b": {"file": sb["file"], "heading": sb["heading"]},
            }
            if sa["file"] == sb["file"]:
                same_file_dup.append(entry)
            else:
                cross_dup.append(entry)

    cross_dup.sort(key=lambda x: x["similarity"], reverse=True)
    same_file_dup.sort(key=lambda x: x["similarity"], reverse=True)

    # Health score: start at 100, dock points per finding (capped)
    score = 100
    score -= min(len(oversized) * 4, 24)
    score -= min(len(garbled) * 6, 24)
    score -= min(len(stale) * 3, 18)
    score -= min(len(cross_dup) * 5, 30)
    score -= min(len(same_file_dup) * 5, 24)
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
        },
        "summary": {
            "oversized": len(oversized),
            "garbled_heading": len(garbled),
            "stale": len(stale),
            "cross_file_duplicate": len(cross_dup),
            "same_file_duplicate": len(same_file_dup),
        },
        "recommendations": _build_recommendations(oversized, garbled, stale, cross_dup, same_file_dup),
    }


def _normalize(text: str) -> str:
    """Normalize section body for similarity comparison."""
    t = re.sub(r"\s+", " ", text.lower())
    t = re.sub(r"[*_`#>\-]", "", t)
    return t.strip()[:500]  # cap for speed


def _build_recommendations(oversized, garbled, stale, cross_dup, same_file_dup) -> list[str]:
    recs: list[str] = []
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
    if not recs:
        recs.append("No significant decay detected. Knowledge base is healthy.")
    return recs

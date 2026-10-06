"""Heading normalisation shared by the read side and the write side.

``audit_rot`` (read) and ``injector`` (write) must agree on what "the same topic,
logged again" means. They used to be separate pieces of logic — and a read-side
detector that flags a family the write side happily appends to is only half a
mechanism. One function, one regular expression, both callers.

The rule: strip the qualifiers that make a periodic entry look unique — the
date, the issue number, the version, the parenthesised note, bare 3-4 digit
numbers (years, ids) — then drop whitespace and punctuation. Two headings that
survive this with the same text are the same topic:

    "AI项目横评 2026-09-22 期（文档解析 / PDF→Markdown 四强）"  → "ai项目横评期"
    "AI项目横评 2026-10-03 期（本地判定模型 / decision model）" → "ai项目横评期"

Deliberately conservative: it removes *temporal* noise only. Two headings that
differ in their actual words stay different, so unrelated sections never get
consolidated just because they share a prefix.
"""

from __future__ import annotations

import re

# NOTE: keep this in step with the samples above — they are asserted in tests.
HEADING_NOISE_RE = re.compile(
    r"[（(][^）)]*[）)]"                     # （2026-07-16，含实证）
    r"|20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}"     # 2026-10-01
    r"|\d{1,2}[-/.]\d{1,2}"                 # 10-01
    r"|第\s*\d+\s*[期版次]"                  # 第 3 期 / 第 2 版
    r"|v?\d+\.\d+(?:\.\d+)?"                # v3.3.1 / 1.2.0
    r"|\b\d{3,4}\b"                         # bare years, issue ids
)

# Kept as the old private name too: rot_auditor and its tests have referred to
# it that way since v3.3.2.
_HEADING_NOISE_RE = HEADING_NOISE_RE


def heading_skeleton(heading: str) -> str:
    """Collapse a heading to its bare topic (drops dates, versions, qualifiers)."""
    if not heading:
        return ""
    text = HEADING_NOISE_RE.sub(" ", heading)
    # \w keeps CJK (Python 3), so this drops whitespace and punctuation only.
    text = re.sub(r"[\W_]+", "", text)
    return text.lower()


# Backwards-compatible alias for the previous private name.
_heading_skeleton = heading_skeleton

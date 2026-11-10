"""The one H2 scanner every reader of a managed L1 document shares.

Four readers answer "where does this section end?": the drift fingerprint
(``graph_projection.read_section_body``), the vector projection
(``injector._bodyful_sections``), keyword ownership
(``recall._h2_owner_by_line``) and the migration preflight. They used to split
on *every* line starting with ``##``, which is wrong in two directions — a
``##`` inside a fenced code block is code, and an authored body may legitimately
contain an H2 — so a publication embedded such a body verbatim, the registry
hashed all of it, the next reader stopped at the internal heading, and the drift
check refused the authority's own publication for every surface in that file.

INVARIANTS
----------
* **Fences.** A heading inside a *closed* backtick or tilde fence (any length,
  any info string) is content. An opener with no closer is not a fence at all,
  so a legacy document with one unbalanced fence keeps the sections it has
  always had; :func:`body_requires_envelope` is what stops such a body from
  reaching forward once it is published.
* **The managed body envelope.** A body the plain layout cannot carry is
  written between a pair of versioned, reserved HTML comment delimiters tied by
  digest to its own heading. The authored ``##`` stays an H2 — not demoted, not
  escaped, not fenced, not truncated — and the delimiters are layout only:
  :attr:`MarkdownBlock.body` excludes them, so the drift hash, the vector row
  and the embedding text are the authored body exactly.
* **Managed boundaries isolate fences.** Active envelope delimiters bound
  fence scans, so an interior opener cannot pair outside its envelope. Marker
  examples inside ordinary closed legacy fences remain code, not delimiters.
  New authored bodies claiming the reserved scheme are still refused.
* **Ambiguity is refused, never resolved.** Unterminated, unpaired, nested,
  mis-tied, content-preceded and close-followed delimiters each raise
  :class:`MarkdownLayoutError` with a bounded reason. Derived readers expose
  no sections from a malformed document, preventing forged inner identities
  and shifted duplicate ownership.
* **Occurrence is not decided here.** The projection counts every same-label
  H2 and the derived readers count only bodyful ones; both conventions are left
  as they were, and ``graph_projection.layout_errors`` refuses a managed
  surface where they would disagree.

PRIVACY (§15)
-------------
Every refusal is a bounded ``<reason>`` or ``<reason>:<where>`` token. A
heading, a body and a delimiter value never appear in one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "MANAGED_BODY_SCHEME",
    "MANAGED_BODY_VERSION",
    "MANAGED_MARKER_COLLISION",
    "MANAGED_MARKER_TRAILING",
    "MANAGED_MARKER_UNEXPECTED",
    "MANAGED_MARKER_UNPAIRED",
    "MANAGED_MARKER_UNTERMINATED",
    "MarkdownBlock",
    "MarkdownDocument",
    "MarkdownLayoutError",
    "block_body",
    "body_requires_envelope",
    "canonical_body",
    "heading_label",
    "managed_body_markers",
    "normalize_text",
    "projected_document",
    "render_section",
    "reserved_marker_lines",
    "scan_document",
]

#: The reserved delimiter scheme, namespaced to this framework and this one
#: purpose so no other managed block or authored comment reads as one of these.
MANAGED_BODY_SCHEME = "layered-memory:managed-body"

#: The envelope version, carried *in* both delimiters: a later shape is a new
#: version rather than a silent reinterpretation of this one.
MANAGED_BODY_VERSION = 1

#: Bounded refusal reasons.
MANAGED_MARKER_UNEXPECTED = "managed_marker_unexpected"
MANAGED_MARKER_UNPAIRED = "managed_marker_unpaired"
MANAGED_MARKER_UNTERMINATED = "managed_marker_unterminated"
MANAGED_MARKER_COLLISION = "managed_marker_collision"
MANAGED_MARKER_TRAILING = "managed_marker_trailing"
_SECTION_MISMATCH = "managed_marker_section_mismatch"

_OPEN_TEMPLATE = (
    f'<!-- {MANAGED_BODY_SCHEME} v{MANAGED_BODY_VERSION} section="{{digest}}" -->'
)
_CLOSE_TEMPLATE = (
    f'<!-- /{MANAGED_BODY_SCHEME} v{MANAGED_BODY_VERSION} section="{{digest}}" -->'
)

#: An H2, by the rule the projection identity has always used: two hashes at
#: column zero then a space or a tab. ``###`` is deeper, ``##x`` is not a
#: heading, and a bare ``##`` opens no section.
_HEADING = re.compile(r"^##[ \t]+(.*?)[ \t]*$")

#: A fence opener candidate: CommonMark's up-to-three leading spaces, three or
#: more backticks or tildes, then the info string.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

#: Any line *claiming* the reserved scheme, well-formed or not — matched
#: leniently because an almost-correct delimiter is ambiguous, and ambiguity is
#: refused rather than guessed at.
_RESERVED = re.compile(rf"^[ \t]*<!--[ \t]*/?{re.escape(MANAGED_BODY_SCHEME)}\b")
_OPEN = re.compile(
    rf'^<!-- {re.escape(MANAGED_BODY_SCHEME)} v(\d+) section="([0-9a-f]+)" -->$'
)
_CLOSE = re.compile(
    rf'^<!-- /{re.escape(MANAGED_BODY_SCHEME)} v(\d+) section="([0-9a-f]+)" -->$'
)

_FIELD_SEPARATOR = "\x1f"
_DIGEST_LENGTH = 16


class MarkdownLayoutError(ValueError):
    """This document (or body) has no unambiguous section layout.

    A ``ValueError`` because every caller that validates a layout already
    refuses one by name; its ``str()`` is a bounded reason token, never the
    text that caused it.
    """


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """One H2 block: label, verbatim text, canonical body, line span.

    ``text`` is byte-for-byte what the document holds, which is what lets an
    untouched section survive a publication unchanged. ``body`` is what readers
    compare, hash and embed: LF-normalized, boundary-stripped, envelope
    delimiters removed.
    """

    label: str
    text: str
    body: str
    managed: bool
    start_line: int
    end_line: int

    @property
    def bodyful(self) -> bool:
        return bool(self.body)


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    """One document's preamble, H2 blocks and normalized lines."""

    preamble: str
    blocks: tuple[MarkdownBlock, ...] = ()
    lines: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_text(raw: str) -> str:
    """CRLF and CR to LF — the one line-ending rule all readers share.

    The file readers always normalized; the renderer did not, so a CRLF body
    was written as CRLF, read back as LF, and drifted from its own hash.
    """
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def canonical_body(text: str) -> str:
    """A section or preamble body: LF, boundary-stripped, interior untouched."""
    return normalize_text(text).strip()


def heading_label(line: str) -> str | None:
    """The label of one H2 heading line, or ``None`` when it is not one."""
    match = _HEADING.match(line)
    return None if match is None else match.group(1).strip()


# ---------------------------------------------------------------------------
# The managed body envelope
# ---------------------------------------------------------------------------

def _label_digest(label: str) -> str:
    """The delimiter's tie to its own heading: deterministic and opaque.

    A digest rather than the label, because a label may contain quotes, ``--``
    or ``-->`` — none of which can be written into an HTML comment
    unambiguously — and because a heading is content §15 keeps out of tokens.
    """
    payload = _FIELD_SEPARATOR.join([
        f"{MANAGED_BODY_SCHEME}.v{MANAGED_BODY_VERSION}",
        f"{len(label.encode('utf-8'))}:{label}",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def managed_body_markers(label: str) -> tuple[str, str]:
    """The ``(open, close)`` delimiter pair for one heading."""
    digest = _label_digest(label)
    return _OPEN_TEMPLATE.format(digest=digest), _CLOSE_TEMPLATE.format(digest=digest)


def reserved_marker_lines(text: str) -> bool:
    """Whether any line of ``text`` claims the reserved delimiter scheme."""
    return any(_RESERVED.match(line) for line in normalize_text(text).split("\n"))


def body_requires_envelope(body: str) -> bool:
    """Whether the plain ``## label`` layout cannot carry this body.

    Two cases, both about the *next* section: a top-level ``##`` line, which a
    plain layout would hand to the document as a new section, and an
    unterminated fence, which would pair with some later fence line and swallow
    every heading in between. A body whose ``##`` lines are all inside closed
    fences needs nothing — the scanner reads them as the code they are.
    """
    lines = canonical_body(body).split("\n")
    fenced, unterminated = _fence_regions(lines)
    if unterminated:
        return True
    return any(
        index not in fenced and _HEADING.match(line)
        for index, line in enumerate(lines)
    )


def render_section(label: str, body: str) -> str:
    """Render one managed H2 block, enveloping it only when it must be.

    Raises:
        MarkdownLayoutError: The body claims the reserved delimiter scheme and
            therefore has no unambiguous rendering.
    """
    canonical = canonical_body(body)
    if reserved_marker_lines(canonical):
        raise MarkdownLayoutError(f"{MANAGED_MARKER_COLLISION}:body")
    if not body_requires_envelope(canonical):
        return f"## {label}\n\n{canonical}\n\n"
    opening, closing = managed_body_markers(label)
    return f"## {label}\n\n{opening}\n{canonical}\n{closing}\n\n"


# ---------------------------------------------------------------------------
# Fences, bounded to the region being scanned
# ---------------------------------------------------------------------------

def _fence_opener(line: str) -> tuple[str, int] | None:
    match = _FENCE.match(line)
    if match is None:
        return None
    fence = match.group(1)
    if fence[0] == "`" and "`" in match.group(2):
        # CommonMark: a backtick fence's info string may not contain a backtick.
        return None
    return fence[0], len(fence)


def _fence_closes(line: str, character: str, length: int) -> bool:
    match = _FENCE.match(line)
    if match is None:
        return False
    fence = match.group(1)
    return (
        fence[0] == character
        and len(fence) >= length
        and not match.group(2).strip()
    )


def _fence_regions(lines: Sequence[str]) -> tuple[frozenset[int], bool]:
    """``(indices inside a closed fence, any opener left unterminated)``.

    An unterminated opener is deliberately not a region, so one unbalanced
    fence line does not delete every heading after it.

    Index the next adequate closer in reverse with a monotone stack per
    character. Every candidate is pushed/popped once; decreasing unclosed
    opener lengths cannot trigger repeated suffix scans.
    """
    total = len(lines)
    openers = [_fence_opener(line) for line in lines]
    next_close: list[int | None] = [None] * total
    candidates: dict[str, list[tuple[int, int]]] = {"`": [], "~": []}
    for index in range(total - 1, -1, -1):
        opener = openers[index]
        if opener is None:
            continue
        character, length = opener
        stack = candidates[character]
        low, high = 0, len(stack)
        # Stack lengths decrease: the last adequate candidate is the nearest.
        while low < high:
            middle = (low + high) // 2
            if stack[middle][0] >= length:
                low = middle + 1
            else:
                high = middle
        if low:
            next_close[index] = stack[low - 1][1]
        if _fence_closes(lines[index], character, length):
            while stack and stack[-1][0] <= length:
                stack.pop()
            stack.append((length, index))
    fenced: set[int] = set()
    unterminated = False
    index = 0
    while index < total:
        if openers[index] is None:
            index += 1
            continue
        close = next_close[index]
        if close is None:
            unterminated = True
            index += 1
            continue
        fenced.update(range(index, close + 1))
        index = close + 1
    return frozenset(fenced), unterminated


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------

def scan_document(raw: str, *, strict: bool = True) -> MarkdownDocument:
    """Scan one document into its preamble and H2 blocks.

    Args:
        raw: The document text. Line endings are normalized; nothing else is.
        strict: Refuse a malformed or ambiguous delimiter by raising.
            ``False`` answers with the *structural* reading the framework used
            before the envelope existed — labels and verbatim block text, with
            the delimiters as ordinary content — and is for layout-only callers
            that must not raise. Never use it for a body the authority
            compares: :func:`projected_document` is the fail-closed reader for
            the derived projections, and the drift and round-trip paths are
            strict.

    Raises:
        MarkdownLayoutError: ``strict`` and a delimiter is unterminated,
            unpaired, nested, mis-tied to its heading, preceded by content in
            its own block or followed by content after its close.
    """
    lines = tuple(normalize_text(raw).split("\n"))
    try:
        return _scan(lines, envelopes=True)
    except MarkdownLayoutError:
        if strict:
            raise
        return _scan(lines, envelopes=False)


def projected_document(raw: str) -> MarkdownDocument:
    """The bodyful H2 blocks the derived projections may safely identify.

    Never raises. A malformed managed envelope invalidates ownership for the
    entire document: a structural fallback could mint authored inner headings
    or shift a later duplicate's occurrence. Preserve lines for owner-map
    sizing, but expose no sections until the layout is repaired.
    """
    lines = tuple(normalize_text(raw).split("\n"))
    try:
        document = _scan(lines, envelopes=True)
        blocks = [block for block in document.blocks if block.label and block.body]
    except MarkdownLayoutError:
        return MarkdownDocument(preamble="", lines=lines)
    return MarkdownDocument(
        preamble=document.preamble, blocks=tuple(blocks), lines=document.lines,
    )


def block_body(text: str) -> str:
    """The canonical body of one block's verbatim text, envelope removed.

    The compatibility reader for a caller holding a ``(label, block text)``
    pair from ``graph_projection._parse_document``.
    """
    document = scan_document(text, strict=False)
    if document.blocks:
        return document.blocks[0].body
    _heading, _, rest = normalize_text(text).partition("\n")
    return rest.strip()


def _marker_limits(lines: tuple[str, ...], *, envelopes: bool) -> list[int]:
    """Next active delimiter, excluding examples inside closed legacy fences.

    An envelope opened outside code keeps every interior marker authoritative.
    A legacy unclosed fence must not pair with code *inside* a later managed
    body: if a heading-bound envelope crosses that candidate fence's end, its
    opening remains authoritative and bounds the fence scan instead.
    """
    total = len(lines)
    limits = [total] * (total + 1)
    if not envelopes:
        return limits
    fenced, _unterminated = _fence_regions(lines)
    fence_end: dict[int, int] = {}
    end = total
    for index in range(total - 1, -1, -1):
        if index in fenced:
            if index + 1 not in fenced:
                end = index
            fence_end[index] = end
    markers = [index for index, line in enumerate(lines) if _RESERVED.match(line)]
    closes = {lines[index]: index for index in reversed(markers) if _CLOSE.match(lines[index])}
    active: set[int] = set()
    inside = False
    previous_nonblank = None
    marker_set = set(markers)
    for index, line in enumerate(lines):
        if index in marker_set:
            promoted = False
            if index in fenced and previous_nonblank is not None:
                label = heading_label(lines[previous_nonblank])
                if label is not None:
                    opening, closing = managed_body_markers(label)
                    promoted = (
                        line == opening
                        and closes.get(closing, total) > fence_end[index]
                    )
            if inside or index not in fenced or promoted:
                active.add(index)
                inside = not inside and bool(_OPEN.match(line))
        if line.strip():
            previous_nonblank = index
    for index in range(total - 1, -1, -1):
        limits[index] = index if index in active else limits[index + 1]
    return limits


def _document_fences(lines: tuple[str, ...], limits: list[int]) -> frozenset[int]:
    """Index each delimiter-bounded region once, not once per H2 suffix."""
    fenced: set[int] = set()
    start = 0
    while start < len(lines):
        limit = limits[start]
        region, _unterminated = _fence_regions(lines[start:limit])
        fenced.update(start + offset for offset in region)
        start = limit + 1
    return frozenset(fenced)


def _first_heading(
    lines: tuple[str, ...], start: int, limit: int, fenced: frozenset[int],
) -> int | None:
    """The first heading in ``[start, limit)`` using the document index."""
    for index in range(start, limit):
        if index not in fenced and _HEADING.match(lines[index]):
            return index
    return None


def _scan(lines: tuple[str, ...], *, envelopes: bool) -> MarkdownDocument:
    total = len(lines)
    limits = _marker_limits(lines, envelopes=envelopes)
    fenced = _document_fences(lines, limits)
    preamble: list[str] = []
    index = 0
    while index < total:
        limit = limits[index]
        heading = _first_heading(lines, index, limit, fenced)
        if heading is not None:
            preamble.extend(lines[index:heading])
            index = heading
            break
        preamble.extend(lines[index:limit])
        if limit == total:
            index = total
            break
        raise MarkdownLayoutError(f"{MANAGED_MARKER_UNEXPECTED}:preamble")
    blocks: list[MarkdownBlock] = []
    while index < total:
        block, index = _scan_block(lines, index, limits, fenced)
        blocks.append(block)
    return MarkdownDocument(
        preamble=(
            "".join(f"{line}\n" for line in preamble)
            if blocks
            else "\n".join(preamble)
        ),
        blocks=tuple(blocks),
        lines=lines,
    )


def _scan_block(
    lines: tuple[str, ...],
    start: int,
    limits: list[int],
    fenced: frozenset[int],
) -> tuple[MarkdownBlock, int]:
    """One block, from its heading to the line before the next one.

    Walks the block one *segment* at a time, each segment ending at the next
    reserved delimiter, so fence state never crosses an envelope boundary in
    either direction. The envelope interior is skipped whole: the delimiter
    that closes it is the next reserved line, and anything else there is a
    collision rather than a body this scanner gets to reinterpret.
    """
    total = len(lines)
    label = heading_label(lines[start]) or ""
    opening, closing = managed_body_markers(label)
    managed = False
    content = False
    body_start = start + 1
    body_end: int | None = None
    index = start + 1
    while index < total:
        limit = limits[index]
        heading = None
        for offset in range(limit - index):
            line = lines[index + offset]
            if body_end is not None and line.strip():
                if _HEADING.match(line):
                    heading = index + offset
                    break
                raise MarkdownLayoutError(MANAGED_MARKER_TRAILING)
            if index + offset in fenced:
                content = content or bool(line.strip())
                continue
            if _HEADING.match(line):
                heading = index + offset
                break
            content = content or bool(line.strip())
        if heading is not None:
            index = heading
            break
        if limit == total:
            index = total
            break
        if managed:
            raise MarkdownLayoutError(f"{MANAGED_MARKER_UNEXPECTED}:duplicate")
        if _CLOSE.match(lines[limit]):
            raise MarkdownLayoutError(MANAGED_MARKER_UNPAIRED)
        if lines[limit] != opening:
            raise MarkdownLayoutError(
                f"{MANAGED_MARKER_UNEXPECTED}:"
                + (_SECTION_MISMATCH if _OPEN.match(lines[limit]) else "shape")
            )
        if content:
            raise MarkdownLayoutError(f"{MANAGED_MARKER_UNEXPECTED}:content")
        close = limits[limit + 1]
        if close == total:
            raise MarkdownLayoutError(MANAGED_MARKER_UNTERMINATED)
        if lines[close] != closing:
            raise MarkdownLayoutError(f"{MANAGED_MARKER_COLLISION}:{_SECTION_MISMATCH}")
        managed = True
        body_start = limit + 1
        body_end = close
        index = close + 1
    end = index
    body = "\n".join(lines[body_start:(end if body_end is None else body_end)])
    return (
        MarkdownBlock(
            label=label,
            text="\n".join(lines[start:end]) + ("\n" if end < total else ""),
            body=body.strip(),
            managed=managed,
            start_line=start,
            end_line=end,
        ),
        end,
    )

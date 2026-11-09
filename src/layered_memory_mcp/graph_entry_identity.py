"""Deterministic identity for the ordinary-Markdown projection (§12 Batch L-A).

WHAT THIS MODULE IS
-------------------
The one rule that answers "which authored object is this projected H2 section?".
:func:`~layered_memory_mcp.injector._vector_entries` used to mint a fresh
``uuid4`` for every ordinary-Markdown section on every projection, so projecting
the same unchanged file twice produced two different vector row IDs — and,
through :func:`~layered_memory_mcp.graph_seed_metadata.merge_graph_seed_metadata`,
two different graph seed IDs for one authored section. An identity that changes
when nothing changed cannot carry a mapping.

The rule is a versioned, length-prefixed serialization of exactly three things —
the exact domain, the exact H2 label, and the zero-based occurrence among
*identical* labels — hashed with SHA-256 under a dedicated projection prefix.
Pure: no I/O, no store, no config, no clock, no randomness.

WHAT IT IS NOT
--------------
- NOT a content fingerprint. The body, the derived summary, the timestamps, the
  confidence and the section's document-wide position are all deliberately
  absent, so editing a body or inserting another section updates a row instead
  of orphaning one (§10.2).
- NOT a normalizer. Case, punctuation, dates, versions and Unicode distinctions
  are retained verbatim. ``embedding_heading`` and ``heading_skeleton`` exist to
  erase exactly those distinctions for *retrieval*; using either here would make
  "AI项目横评 2026-09-22 期" and "AI项目横评 2026-10-03 期" the same object.
- NOT a repairer. A blank, whitespace-only, multi-line or non-string input is
  refused by name. The labels :func:`~layered_memory_mcp.injector._bodyful_sections`
  emits are the canonical input: already single-line and non-blank. Trimming
  something else into an identity would invent one the source never had.
- NOT entity resolution. Two *identical* labels in one domain are separated by
  position alone, and position cannot say which of them an author kept when one
  is deleted. That limitation is explicit (see
  :func:`markdown_section_entry_ids`) rather than papered over by guessing from
  body similarity.
- NOT a migration. This changes the IDs *future* projections mint, including in
  legacy mode. No existing database is rewritten here, no old random row ID is
  aliased or backfilled, and a production reindex remains a separately approved,
  coordinated vector/graph rebuild with backups.
- NOT the frontmatter rule. An entry carrying an authoritative ``id`` keeps it;
  this module is only consulted for ordinary Markdown.

PRIVACY (§15)
-------------
A refusal reads ``<reason>:<field name>`` — a bounded token naming which input
was unusable and why, never the value itself, so a rejected projection can be
logged without logging the heading or the domain it refused.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Iterable, Sequence

#: The ID prefix. Dedicated to this projection so an ordinary-Markdown row ID is
#: distinguishable at a glance from an L1 frontmatter ``id``, a graph node ID or
#: a legacy ``uuid4`` — including in an old database that still holds all three.
PROJECTION_ID_PREFIX = "mdsec"

#: The identity-rule version, carried *in the ID*. A later rule change mints
#: ``mdsec2_...`` rows, so which rule produced a row is readable without a
#: lookup table, and the two generations can never be mistaken for each other.
PROJECTION_IDENTITY_VERSION = 1

#: The scheme token opening every canonical payload: the version is part of what
#: is hashed as well as part of the prefix, so two versions of the rule cannot
#: collide even if a future one serializes the same three fields.
PROJECTION_IDENTITY_SCHEME = f"{PROJECTION_ID_PREFIX}.v{PROJECTION_IDENTITY_VERSION}"

PREAMBLE_ID_PREFIX = "mdpre"
PREAMBLE_IDENTITY_VERSION = 1
PREAMBLE_IDENTITY_SCHEME = f"{PREAMBLE_ID_PREFIX}.v{PREAMBLE_IDENTITY_VERSION}"

#: Field separator for the canonical payload. ASCII unit separator, matching
#: :func:`~layered_memory_mcp.graph_models.make_node_id`. Every field is
#: additionally length-prefixed, so a separator *inside* a heading is still
#: unambiguous rather than readable as a field boundary.
_FIELD_SEPARATOR = "\x1f"

#: How much of the digest the ID carries. 32 hex characters = 128 bits, the same
#: order of magnitude of collision resistance as the ``uuid4`` this replaces,
#: and short enough to read in a diff.
_ID_DIGEST_LENGTH = 32

#: The label was not a ``str`` (``None``, a number, a bool, an object). Coercing
#: it is how a projection ends up keyed by ``"True"``.
PROJECTION_IDENTITY_NOT_A_STRING = "projection_identity_not_a_string"

#: The label was a string, but empty or whitespace-only. Nothing identifies it.
PROJECTION_IDENTITY_BLANK = "projection_identity_blank"

#: The label wrapped across lines. Refused for the reason the graph layer
#: refuses it too: a multi-line label has no single stable form.
PROJECTION_IDENTITY_MULTILINE = "projection_identity_multiline"

#: The occurrence was not a non-negative ``int`` (a float, a string, a bool, a
#: negative). An occurrence is a position in a duplicate group, so anything else
#: is a caller mistake rather than a value to round.
PROJECTION_IDENTITY_OCCURRENCE_INVALID = "projection_identity_occurrence_invalid"


class ProjectionIdentityError(ValueError):
    """A projection input cannot produce a stable identity.

    Its ``str()`` is ``"<reason>:<field>"`` built from the constants above and
    nothing else: the caller gets a token it can count and log, never the value
    that was refused.
    """


def _require_label(value: Any, field: str) -> str:
    """Return ``value`` unchanged, or refuse it by name.

    Deliberately *not* a normalizer: the value is returned exactly as given.
    Stripping it here would mean two spellings of one heading could both produce
    an identity, and the caller's own stored ``section``/``domain`` would then
    disagree with the ID derived from it.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ProjectionIdentityError(f"{PROJECTION_IDENTITY_NOT_A_STRING}:{field}")
    if "\n" in value or "\r" in value:
        raise ProjectionIdentityError(f"{PROJECTION_IDENTITY_MULTILINE}:{field}")
    if not value.strip():
        raise ProjectionIdentityError(f"{PROJECTION_IDENTITY_BLANK}:{field}")
    return value


def _require_occurrence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectionIdentityError(
            f"{PROJECTION_IDENTITY_OCCURRENCE_INVALID}:occurrence"
        )
    return value


def _length_prefixed(text: str) -> str:
    """``<byte length>:<text>`` — what makes the payload unambiguous.

    With the length stated up front, no label content (a separator, a colon, a
    digit run) can be read as the start of the next field.
    """
    return f"{len(text.encode('utf-8'))}:{text}"


def markdown_section_identity_payload(
    domain: Any, section: Any, occurrence: Any = 0
) -> str:
    """The canonical serialization that gets hashed. Exposed to be asserted on.

    ``<scheme>␟<len>:<domain>␟<len>:<section>␟<occurrence>`` — the version, the
    exact domain, the exact H2 label and the zero-based occurrence among
    identical labels. Nothing else is in it, which is the whole contract: a body
    edit, a new summary, a timestamp or a move within the document cannot change
    the bytes below.

    Raises:
        ProjectionIdentityError: An input is unusable. Nothing is repaired.
    """
    return _FIELD_SEPARATOR.join(
        [
            PROJECTION_IDENTITY_SCHEME,
            _length_prefixed(_require_label(domain, "domain")),
            _length_prefixed(_require_label(section, "section")),
            str(_require_occurrence(occurrence)),
        ]
    )


def markdown_section_entry_id(domain: Any, section: Any, occurrence: Any = 0) -> str:
    """The stable entry ID for one ordinary-Markdown H2 section.

    Deterministic across processes and runs: the same domain, label and
    occurrence always produce the same ID, and no other input can change it.

    Args:
        domain: The exact domain (the L1 file stem) the section belongs to.
        section: The exact H2 label, as ``_bodyful_sections`` emits it.
        occurrence: Zero-based position among *identical* labels in that domain.

    Raises:
        ProjectionIdentityError: An input is unusable. Nothing is fabricated.
    """
    payload = markdown_section_identity_payload(domain, section, occurrence)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return (
        f"{PROJECTION_ID_PREFIX}{PROJECTION_IDENTITY_VERSION}"
        f"_{digest[:_ID_DIGEST_LENGTH]}"
    )


def markdown_preamble_identity_payload(domain: Any) -> str:
    """Canonical scheme token and byte-length-prefixed exact domain.

    Uses the section rule's separator and refusal discipline, but a distinct
    scheme so a preamble can never be interpreted as a section identity.
    """
    return _FIELD_SEPARATOR.join([
        PREAMBLE_IDENTITY_SCHEME,
        _length_prefixed(_require_label(domain, "domain")),
    ])


def markdown_preamble_entry_id(domain: Any) -> str:
    """The stable, versioned entry ID for one domain's Markdown preamble."""
    payload = markdown_preamble_identity_payload(domain)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return (
        f"{PREAMBLE_ID_PREFIX}{PREAMBLE_IDENTITY_VERSION}"
        f"_{digest[:_ID_DIGEST_LENGTH]}"
    )


def markdown_section_entry_ids(
    domain: Any, sections: Sequence[Any] | Iterable[Any]
) -> list[str]:
    """The IDs for one document's H2 labels, in document order.

    The one place occurrence is *assigned*: the ``n``-th appearance of an
    identical label gets occurrence ``n``. Counting per label rather than per
    document is what keeps an unrelated insertion, deletion or reordering from
    renumbering a section that did not move.

    The explicit limitation (§12 Batch L-A): within a group of *identical*
    labels, insertion, deletion and reordering shift the occurrences, so the IDs
    follow positions rather than authored objects. Two sections titled the same
    thing in one domain are indistinguishable to this rule, and inferring which
    is which from body similarity would be a guess dressed as an identity.

    Raises:
        ProjectionIdentityError: Any label (or the domain) is unusable. The
            whole document is refused rather than partly identified.
    """
    seen: Counter[str] = Counter()
    ids: list[str] = []
    for section in sections:
        label = _require_label(section, "section")
        ids.append(markdown_section_entry_id(domain, label, seen[label]))
        seen[label] += 1
    return ids


__all__ = [
    "PREAMBLE_ID_PREFIX",
    "PREAMBLE_IDENTITY_VERSION",
    "PREAMBLE_IDENTITY_SCHEME",
    "markdown_preamble_identity_payload",
    "markdown_preamble_entry_id",
    "PROJECTION_ID_PREFIX",
    "PROJECTION_IDENTITY_BLANK",
    "PROJECTION_IDENTITY_MULTILINE",
    "PROJECTION_IDENTITY_NOT_A_STRING",
    "PROJECTION_IDENTITY_OCCURRENCE_INVALID",
    "PROJECTION_IDENTITY_SCHEME",
    "PROJECTION_IDENTITY_VERSION",
    "ProjectionIdentityError",
    "markdown_section_entry_id",
    "markdown_section_entry_ids",
    "markdown_section_identity_payload",
]

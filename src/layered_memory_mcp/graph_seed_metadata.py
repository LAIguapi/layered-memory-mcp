"""Stable graph seed identity for the existing L1/vector projection (§12 Batch I).

WHAT THIS MODULE IS
-------------------
The one place that answers "which graph node does this searchable row stand
for?". It maps an L1 :class:`~layered_memory_mcp.models.KnowledgeEntry`'s *stable
identity* to the legacy Claim node ID the importer already mints, and packages
that as the canonical :data:`~layered_memory_mcp.graph_runtime.GRAPH_SEED_ID_KEY`
metadata entry :func:`~layered_memory_mcp.graph_runtime.build_seed_mapping`
consumes.

It is deliberately tiny and deliberately pure: four functions, no I/O, no store,
no config, no clock and no randomness.

WHAT IT IS NOT
--------------
- NOT a second ID algorithm. The hash rule is
  :func:`~layered_memory_mcp.graph_legacy_importer.legacy_claim_id`, imported
  rather than restated, so the vector projection and a legacy backfill cannot
  disagree about which node a section is. Two rules for one identity is how a
  backfill ends up duplicating every claim it was supposed to update.
- NOT a content fingerprint. Nothing here reads a body, a summary, a heading, a
  domain, a tag, a timestamp or a file name, so an edit updates a claim instead
  of orphaning one (§10.2). The body's own fingerprint already lives in the
  evidence node's ``quote_hash``.
- NOT a guesser. An identity that is blank, multi-line or not a string yields
  *no* key and a named refusal. A fabricated ID would attach a row to a node
  that does not exist and make the gap invisible; the missing key keeps it a
  counted ``graph_metadata_without_ids`` fallback instead.
- NOT a writer. It produces a dict. Whether that reaches a vector row, an MCP
  response or nothing at all is the caller's decision.

PRIVACY (§15)
-------------
A refusal reason is one of the four constants below — a label, never the
offending value, so a rejected identity can be logged without logging whatever
it contained.
"""

from __future__ import annotations

from typing import Any, Mapping

from .graph_legacy_importer import legacy_claim_id
from .graph_runtime import GRAPH_SEED_ID_KEY

#: The entry exposed no ``id`` at all.
SEED_IDENTITY_MISSING = "graph_seed_identity_missing"

#: The identity was a string, but an empty or whitespace-only one.
SEED_IDENTITY_BLANK = "graph_seed_identity_blank"

#: The identity wrapped across lines. Refused for the same reason the legacy
#: importer refuses it: a multi-line identity has no single stable form, and
#: trimming one into existence would invent an identity the source never had.
SEED_IDENTITY_MULTILINE = "graph_seed_identity_multiline"

#: The identity was not a ``str`` (a number, a bool, ``None``, an object).
#: Coercing it is exactly how a graph ends up keyed by ``"True"``.
SEED_IDENTITY_NOT_A_STRING = "graph_seed_identity_not_a_string"


class SeedIdentityError(ValueError):
    """An entry identity cannot produce a graph node ID.

    Its ``str()`` is one of the four reason constants above and nothing else:
    the caller gets a token it can count and log, not the value it refused.
    """


def normalize_seed_identity(value: Any) -> str:
    """Return the trimmed L1 identity, or refuse it by name.

    The same normalization
    :meth:`~layered_memory_mcp.graph_legacy_importer.LegacyGraphImporter._usable_entries`
    applies, so a padded identity cannot become a second identity for one entry.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise SeedIdentityError(SEED_IDENTITY_NOT_A_STRING)
    if "\n" in value or "\r" in value:
        raise SeedIdentityError(SEED_IDENTITY_MULTILINE)
    text = value.strip()
    if not text:
        raise SeedIdentityError(SEED_IDENTITY_BLANK)
    return text


def graph_node_id_for_entry_id(entry_id: Any) -> str:
    """The canonical graph node ID for the L1 identity ``entry_id``.

    Deterministic across processes and runs: the same identity always produces
    the same ID, and no other input can change it.

    Raises:
        SeedIdentityError: The identity is unusable. Nothing is fabricated.
    """
    return legacy_claim_id(normalize_seed_identity(entry_id))


def graph_node_id_for_entry(entry: Any) -> str:
    """The canonical graph node ID for one L1 entry.

    Duck-typed on ``id`` alone — the only field a graph identity may depend on —
    so a test double or a projection row works as well as a
    :class:`~layered_memory_mcp.models.KnowledgeEntry`.
    """
    if not hasattr(entry, "id"):
        raise SeedIdentityError(SEED_IDENTITY_MISSING)
    return graph_node_id_for_entry_id(entry.id)


def entry_seed_rejection(entry: Any) -> str | None:
    """Why ``entry`` has no graph identity, or ``None`` when it has one.

    The non-raising view of the same rule, for the callers that count gaps
    rather than handle them — a diagnostic, a dry-run report, a write path that
    must not fail because one row is unmappable.
    """
    try:
        graph_node_id_for_entry(entry)
    except SeedIdentityError as exc:
        return str(exc)
    return None


def merge_graph_seed_metadata(
    metadata: Mapping[str, Any], entry: Any
) -> dict[str, Any]:
    """Return ``metadata`` plus the canonical seed ID, as a new dict.

    A *merge*, not a replacement: every existing key is carried through
    untouched, so adding graph identity to an existing projection cannot break
    a consumer (or a test) that reads the legacy fields beside it. The input is
    never mutated.

    Exactly one key is this module's to decide —
    :data:`~layered_memory_mcp.graph_runtime.GRAPH_SEED_ID_KEY` — and it is set
    to the derived value, because that key means "the node this row's identity
    maps to" and two answers to that cannot both be right.

    An entry with no usable identity leaves the key *absent* rather than
    present-and-empty: ``build_seed_mapping`` then counts the row as
    ``graph_metadata_without_ids`` and unmapped, which is the visible
    degradation §10.3 asks for.
    """
    merged = dict(metadata)
    try:
        merged[GRAPH_SEED_ID_KEY] = graph_node_id_for_entry(entry)
    except SeedIdentityError:
        merged.pop(GRAPH_SEED_ID_KEY, None)
    return merged


__all__ = [
    "SEED_IDENTITY_BLANK",
    "SEED_IDENTITY_MISSING",
    "SEED_IDENTITY_MULTILINE",
    "SEED_IDENTITY_NOT_A_STRING",
    "SeedIdentityError",
    "entry_seed_rejection",
    "graph_node_id_for_entry",
    "graph_node_id_for_entry_id",
    "merge_graph_seed_metadata",
    "normalize_seed_identity",
]

"""Legacy graph-identity backfill: inventory, plan, publish, reverse (§12 Batch I-C).

WHAT THIS MODULE IS
-------------------
The controlled data-preparation seam that gives *already stored* vector rows the
canonical :data:`~layered_memory_mcp.graph_runtime.GRAPH_SEED_ID_KEY` metadata
the Batch I-A write paths now produce for new rows. Three strictly separated
stages:

* :func:`plan_graph_seed_backfill` — a **pure planner** over explicitly supplied
  snapshots. It classifies every vector row exactly once with a stable reason
  code and returns the metadata updates it would publish, plus a content-free
  :class:`BackfillReport`.
* :meth:`~layered_memory_mcp.storage.vector_store.VectorStore.patch_metadata` —
  the metadata-only, atomic publication boundary (added in this batch, lives in
  the store because a second SQL mutation path is how two writers start
  disagreeing).
* :func:`apply_graph_seed_backfill` — the coordinator that refuses a stale or
  foreign snapshot and a plan built from an input the planner could not read,
  honours ``dry_run``, publishes through that one boundary and hands back a
  :class:`BackfillRollback` able to restore the exact previous rows.

WHAT IT IS NOT
--------------
- NOT a migration job, a CLI, a startup hook, a feature flag or a default-mode
  change. Nothing imports this module on any existing path.
- NOT a discoverer. It never resolves a source root, a database path, a config,
  a home directory or a real memory store: the vector rows, the L1 entries, the
  changelog events, the store and the expected snapshot all arrive as arguments.
  A backfill that can find its own input is a backfill that can find the wrong
  input.
- NOT a re-embedder or a re-ranker. Only the ``metadata`` column is ever
  written; ``id``, ``domain``, ``text``, ``vector`` and ``created_at`` are
  untouched, so no authored text, embedding, ranking order or source timestamp
  moves.
- NOT a second ID algorithm. The expected node ID is
  :func:`~layered_memory_mcp.graph_seed_metadata.graph_node_id_for_entry_id`,
  which is the importer's own
  :func:`~layered_memory_mcp.graph_legacy_importer.legacy_claim_id`. A filename,
  heading, body, domain, tag or fresh UUID never produces one.
- NOT a repairer. A disagreeing stored identity, malformed metadata, an orphan
  row, an ambiguous L1 identity and an unusable row ID are *quarantined in the
  report*. Nothing is coerced, guessed or silently overwritten.
- NOT a graph importer. It projects no node and no edge, opens no graph backend
  and adds none: the graph side of I-C stays with an explicit
  :class:`~layered_memory_mcp.graph_legacy_importer.LegacyGraphImporter` call on
  a store the caller supplies. Vector metadata must never become an independent
  semantic truth source, so this module only ever *points at* node IDs the
  importer's own rule derives.
- NOT a cross-store transaction. Publication is atomic for the vector store
  alone; this module makes no claim about graph/vector atomicity, because it has
  none to make (§10.3).

WHY THE CHANGELOG IS ONLY COUNTED
---------------------------------
:func:`plan_graph_seed_backfill` accepts ``changelog_events`` so a caller can
prove which provenance snapshot an inventory was taken against, and it type-
checks them through the importer's own contract
(:class:`~layered_memory_mcp.memory_changelog.MemoryChangelogEntry`). It reads
no event *content*: a changelog record is input evidence, never a second ledger
(§4), it is not a source of vector identity, and it may never manufacture a
:class:`~layered_memory_mcp.models.KnowledgeEntry`. Vector identity comes from
the row ID and the supplied L1 snapshot, or it does not come at all.

PRIVACY (§15)
-------------
:class:`BackfillReport` and :class:`PublicationResult` carry counts, row
identities, *derived* graph node IDs, snapshot fingerprints and stable reason
tokens — and nothing else. Never a body, a summary, a heading, a vector value, a
database path, a stored metadata value or an exception message. A mismatch names
the ID this module derived and the fact of disagreement, never the value it
disagreed with: that value is unvalidated stored data, and echoing it into a
loggable report is how a report becomes a copy of the thing it describes.

:class:`BackfillPlan.updates` and :class:`BackfillRollback` are the *working*
half and do carry real metadata, because publishing and reversing require it.
They are deliberately not the reporting half.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from pydantic import Field

from .graph_models import GraphModel
from .graph_runtime import GRAPH_SEED_ID_KEY, GRAPH_SEED_IDS_KEY
from .graph_seed_metadata import (
    SeedIdentityError,
    graph_node_id_for_entry_id,
    normalize_seed_identity,
)
from .memory_changelog import MemoryChangelogEntry
from .models import KnowledgeEntry

# ---------------------------------------------------------------------------
# Reason codes — one per row, stable, and safe to log
# ---------------------------------------------------------------------------

#: The row already carries exactly the expected graph identity. A no-op, and
#: reported as one rather than rewritten.
REASON_ALREADY_MAPPED = "already_mapped"

#: The row is eligible and carries no graph identity at all. The only reason
#: code that produces a planned update.
REASON_MISSING_GRAPH_METADATA = "missing_graph_metadata"

#: The row carries a graph identity that disagrees with the deterministic one.
#: Quarantined: overwriting it would silently re-point a row at another node.
REASON_MISMATCHED_GRAPH_METADATA = "mismatched_graph_metadata"

#: The row's metadata — or its graph identity field — has a shape this module
#: refuses to interpret. Quarantined rather than coerced, because coercion is
#: how a graph ends up keyed by ``"True"`` or a row loses its other fields.
#:
#: Beyond the four classification states §12 Batch I-C enumerates, because
#: "visible in the report and not coerced" needs a code of its own: a metadata
#: column holding a JSON list has no identity to *mis*match.
REASON_MALFORMED_METADATA = "malformed_metadata"

#: The row ID cannot produce a stable identity at all (blank, multi-line, not a
#: string). No ID is invented; see the ``details`` for the named refusal.
REASON_UNUSABLE_IDENTITY = "unusable_identity"

#: The row ID is usable and the supplied L1 snapshot contains no such entry.
#: The row is not L1-backed in this snapshot, so it gets no identity here.
REASON_ORPHAN_VECTOR_ROW = "orphan_vector_row"

#: The supplied L1 snapshot holds more than one record for the row's normalized
#: identity. Neither is chosen — picking one would make the backfill depend on
#: how the snapshot happened to be listed.
REASON_AMBIGUOUS_L1_IDENTITY = "ambiguous_l1_identity"

#: Two or more supplied vector rows normalize to one identity, so one row ID
#: cannot be published without deciding which row that identity belongs to.
REASON_DUPLICATE_VECTOR_ROW = "duplicate_vector_row"

#: The supplied object is not a vector row at all. Reported by position and
#: type name, never by value.
REASON_UNSUPPORTED_ROW = "unsupported_row"

#: The rows whose identity resolved to exactly one L1 entry. An eligible row may
#: still be quarantined — eligibility is about identity, not about metadata.
ELIGIBLE_REASONS = frozenset(
    {
        REASON_ALREADY_MAPPED,
        REASON_MISSING_GRAPH_METADATA,
        REASON_MISMATCHED_GRAPH_METADATA,
        REASON_MALFORMED_METADATA,
    }
)

#: The rows withheld because their stored metadata or their shape could not be
#: trusted. Distinct from the identity-side buckets (unusable / orphan /
#: ambiguous) so the counts form one exact partition of ``rows_read``.
QUARANTINE_REASONS = frozenset(
    {
        REASON_MISMATCHED_GRAPH_METADATA,
        REASON_MALFORMED_METADATA,
        REASON_DUPLICATE_VECTOR_ROW,
        REASON_UNSUPPORTED_ROW,
    }
)

#: Every code, in report order.
ALL_REASONS = (
    REASON_ALREADY_MAPPED,
    REASON_MISSING_GRAPH_METADATA,
    REASON_MISMATCHED_GRAPH_METADATA,
    REASON_MALFORMED_METADATA,
    REASON_DUPLICATE_VECTOR_ROW,
    REASON_UNSUPPORTED_ROW,
    REASON_UNUSABLE_IDENTITY,
    REASON_ORPHAN_VECTOR_ROW,
    REASON_AMBIGUOUS_L1_IDENTITY,
)

# ---------------------------------------------------------------------------
# Detail tokens — ``<field>:<shape problem>``, a label and never a value
# ---------------------------------------------------------------------------

_METADATA_FIELD = "metadata"
_DETAIL_NOT_JSON = "not_json"
_DETAIL_NOT_A_MAPPING = "not_a_mapping"
_DETAIL_KEYS_NOT_STRINGS = "keys_not_strings"
_DETAIL_NOT_A_STRING = "not_a_string"
_DETAIL_NOT_A_LIST = "not_a_list"
_DETAIL_BLANK = "blank"
_DETAIL_MULTILINE = "multiline"
_DETAIL_PADDED = "padded"
_DETAIL_MISMATCH = "mismatch"
_DETAIL_ELEMENT = "element_"

#: The L1 snapshot held two byte-identical records for one identity. Stricter
#: than :meth:`~layered_memory_mcp.graph_legacy_importer.LegacyGraphImporter._usable_entries`,
#: which treats an identical repeat as a benign replay: a *backfill* that
#: accepted a duplicated identity would be asserting the snapshot it was handed
#: is clean when it demonstrably is not, and the cost of refusing is one rerun.
_DETAIL_DUPLICATE_L1 = "duplicate_l1_identity"

#: The L1 snapshot held two *different* records for one identity.
_DETAIL_CONFLICTING_L1 = "conflicting_l1_identity"


# ---------------------------------------------------------------------------
# Publication statuses
# ---------------------------------------------------------------------------

#: Nothing was written because the caller asked for an inventory only.
BACKFILL_DRY_RUN = "backfill_dry_run"

#: There was no planned update, so no publication was attempted. The status of
#: a clean rerun.
BACKFILL_NOTHING_TO_DO = "backfill_nothing_to_do"

#: The planned metadata reached the store's atomic publication boundary. Read
#: ``rows_changed`` for how many rows actually differed.
BACKFILL_APPLIED = "backfill_applied"

#: The store no longer holds the snapshot the plan was built against. Refused
#: before publication; the caller replans.
BACKFILL_STALE_SNAPSHOT = "backfill_stale_snapshot"

#: The plan names a row the supplied snapshot does not contain — a plan and a
#: snapshot from two different inventories. Refused before publication.
BACKFILL_PLAN_SNAPSHOT_MISMATCH = "backfill_plan_snapshot_mismatch"

#: The plan was built from an input this module refused to read — an object
#: that is not a vector row, not a :class:`~layered_memory_mcp.models.KnowledgeEntry`,
#: not a :class:`~layered_memory_mcp.memory_changelog.MemoryChangelogEntry`, or
#: an L1 identity that is unusable or self-conflicting. Any
#: :attr:`BackfillReport.errors` means the inventory does not describe the
#: snapshot trio it was handed, so the plan derived from it is not publishable.
#: Refused before publication and *not* overridable: ``require_clean`` is a
#: judgement about row coverage, not a licence to publish a plan whose inputs
#: broke the planner's contract.
BACKFILL_INVALID_INPUTS = "backfill_invalid_inputs"

#: The plan quarantined rows, so identity coverage is incomplete and §12 Batch
#: I-C requires the apply to abort. Overridable with ``require_clean=False``,
#: which is a decision a caller makes explicitly and visibly — and which
#: overrides *coverage only*, never :data:`BACKFILL_INVALID_INPUTS`.
BACKFILL_UNRESOLVED_ROWS = "backfill_unresolved_rows"

#: The store refused or failed the publication. Named by exception class, never
#: by message, and the previous committed rows are intact.
BACKFILL_PUBLICATION_FAILED = "backfill_publication_failed"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VectorSnapshotRow:
    """One row of an explicitly supplied vector snapshot.

    Field order and meaning match
    :meth:`~layered_memory_mcp.storage.vector_store.VectorStore.snapshot` tuples
    exactly, so a snapshot can be planned over without conversion. ``id``,
    ``vector`` and ``metadata`` are typed ``Any`` on purpose: a backfill planner
    whose input model rejected a malformed row could never *report* one.

    ``text`` and ``vector`` are carried only so a fingerprint can cover the
    whole row. Neither is read for a classification decision and neither ever
    reaches a report.
    """

    id: Any
    domain: Any = ""
    text: Any = ""
    vector: Any = None
    metadata: Any = None

    @classmethod
    def from_snapshot(cls, row: Any) -> "VectorSnapshotRow":
        """Coerce one supplied row, or raise :class:`TypeError`.

        Accepts a :class:`VectorSnapshotRow`, a mapping with the snapshot's own
        column names, or a 5-element sequence (the ``snapshot()`` tuple).
        ``str``/``bytes`` are refused explicitly: a string is a sequence, and
        silently reading one character-by-character is exactly the kind of
        coercion this module exists to avoid.
        """
        if isinstance(row, VectorSnapshotRow):
            return row
        if isinstance(row, Mapping):
            unknown = set(row) - {"id", "domain", "text", "vector", "metadata"}
            if unknown or "id" not in row:
                raise TypeError("mapping is not a vector snapshot row")
            return cls(**dict(row))
        if isinstance(row, (str, bytes, bytearray)):
            raise TypeError("a string is not a vector snapshot row")
        if isinstance(row, Sequence) and len(row) == 5:
            return cls(*row)
        raise TypeError("unsupported vector snapshot row")


def coerce_snapshot_rows(rows: Sequence[Any]) -> list[VectorSnapshotRow]:
    """Every supplied row as a :class:`VectorSnapshotRow`, refusing the rest.

    Raises:
        TypeError: As soon as one row is unusable. Callers that must *report*
            rather than fail use :func:`plan_graph_seed_backfill`, which keeps
            the position of each refusal.
    """
    return [VectorSnapshotRow.from_snapshot(row) for row in rows]


def vector_snapshot_fingerprint(rows: Sequence[Any]) -> str:
    """A content-free fingerprint of one vector snapshot (§12 Batch I-C).

    Covers every column of every row in order, so any change to any row — text,
    vector, metadata, ordering — changes the answer. The output is a digest:
    safe to carry in a report, store beside a plan and compare before
    publication, and impossible to read a memory body out of.

    An unusable row contributes its *position and type name* rather than being
    skipped, so a snapshot cannot be fingerprinted into looking clean.
    """
    digest = hashlib.sha256()
    for index, row in enumerate(rows):
        try:
            coerced = VectorSnapshotRow.from_snapshot(row)
        except TypeError:
            digest.update(
                f"{index}\x1funsupported\x1f{type(row).__name__}\x1e".encode("utf-8")
            )
            continue
        parts = (
            coerced.id,
            coerced.domain,
            coerced.text,
            coerced.vector,
            _metadata_text(coerced.metadata),
        )
        digest.update(
            ("\x1f".join(repr(part) for part in parts) + "\x1e").encode("utf-8")
        )
    return digest.hexdigest()


def _metadata_text(raw: Any) -> str | None:
    """The stored metadata *text* for a row, or ``None`` for "no metadata".

    A real snapshot already carries JSON text (or SQL ``NULL``); a synthetic
    fixture row may carry a mapping. Normalizing the mapping to canonical JSON
    here is what lets a rollback written from either shape be byte-stable.
    """
    if raw is None:
        return None
    if isinstance(raw, (str, bytes, bytearray)):
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        return text or None
    if isinstance(raw, Mapping):
        try:
            return json.dumps(dict(raw), sort_keys=True)
        except TypeError:
            return repr(sorted(raw))
    return repr(raw)


# ---------------------------------------------------------------------------
# Reports and plans
# ---------------------------------------------------------------------------

class RowVerdict(GraphModel):
    """What one vector row was classified as, and why.

    ``row_index`` is the row's position in the supplied snapshot and is the
    identifier of record: ``row_id`` is empty whenever the row's own ID is not a
    clean single-line string, because a report token holding a blank or
    multi-line value is neither loggable nor diffable.

    ``expected_graph_node_id`` is the ID *this module derived*; it is empty when
    no identity could be derived. ``details`` are stable ``<field>:<problem>``
    tokens — never the offending value (§15).
    """

    row_index: int = Field(default=0, ge=0)
    row_id: str = ""
    reason: str
    expected_graph_node_id: str = ""
    details: list[str] = Field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return self.reason in ELIGIBLE_REASONS

    @property
    def quarantined(self) -> bool:
        return self.reason in QUARANTINE_REASONS


class BackfillReport(GraphModel):
    """The content-free inventory of one planning pass.

    The counts are a *partition* of ``rows_read``, not a selection of
    interesting numbers::

        rows_read == already_mapped + planned_updates + quarantined
                     + unusable_identity + orphan_rows + ambiguous_identity

    :attr:`counts_balance` asserts exactly that, which is what makes "every row
    was handled" an arithmetic claim instead of a hope (§12 Batch I-C).
    ``eligible`` cuts the same rows a different way — those whose identity
    resolved to exactly one L1 entry — so it deliberately does not appear in the
    sum.
    """

    rows_read: int = Field(default=0, ge=0)
    entries_read: int = Field(default=0, ge=0)
    entries_indexed: int = Field(default=0, ge=0)
    changelog_events_read: int = Field(default=0, ge=0)
    eligible: int = Field(default=0, ge=0)
    already_mapped: int = Field(default=0, ge=0)
    planned_updates: int = Field(default=0, ge=0)
    quarantined: int = Field(default=0, ge=0)
    unusable_identity: int = Field(default=0, ge=0)
    orphan_rows: int = Field(default=0, ge=0)
    ambiguous_identity: int = Field(default=0, ge=0)
    #: L1 entries the supplied vector snapshot has no row for. Reported, not
    #: fixed: giving them a row would mean embedding text, which this batch
    #: forbids.
    entries_without_vector_rows: int = Field(default=0, ge=0)
    counts_by_reason: dict[str, int] = Field(default_factory=dict)
    snapshot_fingerprint: str = ""
    verdicts: list[RowVerdict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def accounted_rows(self) -> int:
        return (
            self.already_mapped
            + self.planned_updates
            + self.quarantined
            + self.unusable_identity
            + self.orphan_rows
            + self.ambiguous_identity
        )

    @property
    def counts_balance(self) -> bool:
        """Whether every supplied row is accounted for exactly once."""
        return self.accounted_rows == self.rows_read == len(self.verdicts)

    @property
    def unresolved_rows(self) -> int:
        """Rows this plan refuses to touch for a reason other than "correct"."""
        return (
            self.quarantined
            + self.unusable_identity
            + self.orphan_rows
            + self.ambiguous_identity
        )

    @property
    def ok(self) -> bool:
        return not self.errors


class MetadataUpdate(GraphModel):
    """One row's complete replacement metadata, ready to publish.

    Complete rather than a key patch: the publication boundary compares the
    whole supplied snapshot before it writes, so a complete dict built from the
    row's own metadata is deterministic *and* cannot lose a field the planner
    never looked at. Part of the working plan, not of the report — it carries
    the row's real metadata by necessity.
    """

    row_id: str
    graph_node_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class BackfillPlan(GraphModel):
    """A content-free :attr:`report` plus the :attr:`updates` it would publish."""

    report: BackfillReport
    updates: list[MetadataUpdate] = Field(default_factory=list)

    @property
    def has_updates(self) -> bool:
        return bool(self.updates)

    def metadata_updates(self) -> dict[str, dict[str, Any]]:
        """The ``row ID → complete metadata`` mapping ``patch_metadata`` takes."""
        return {update.row_id: dict(update.metadata) for update in self.updates}


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------

@dataclass
class _Inventory:
    """Mutable planning state. Assembled in full before anything is returned."""

    verdicts: list[RowVerdict] = field(default_factory=list)
    updates: list[MetadataUpdate] = field(default_factory=list)
    errors: set[str] = field(default_factory=set)
    warnings: set[str] = field(default_factory=set)

    def classify(
        self,
        index: int,
        row_id: Any,
        reason: str,
        *,
        expected: str = "",
        details: Sequence[str] = (),
    ) -> None:
        self.verdicts.append(
            RowVerdict(
                row_index=index,
                row_id=_reportable_row_id(row_id),
                reason=reason,
                expected_graph_node_id=expected,
                details=sorted(set(details)),
            )
        )


def _reportable_row_id(row_id: Any) -> str:
    """The row ID when it is safe to put in a token, otherwise ``""``."""
    if isinstance(row_id, bool) or not isinstance(row_id, str):
        return ""
    if "\n" in row_id or "\r" in row_id:
        return ""
    return row_id if row_id.strip() else ""


def plan_graph_seed_backfill(
    vector_rows: Sequence[Any],
    entries: Sequence[Any],
    changelog_events: Sequence[Any] = (),
) -> BackfillPlan:
    """Inventory one explicit snapshot trio and plan the metadata it is missing.

    Pure: no file is opened, no store is constructed, no path is resolved, no
    clock or random source is read, and none of the three inputs is mutated. The
    same inputs always produce the same plan and a byte-identical report.

    Args:
        vector_rows: The vector snapshot — ``VectorStore.snapshot()`` tuples,
            mappings, or :class:`VectorSnapshotRow` objects. Anything else is
            reported as :data:`REASON_UNSUPPORTED_ROW` by position and type.
        entries: The L1 :class:`~layered_memory_mcp.models.KnowledgeEntry`
            snapshot the rows must be matched against. The *only* source of "is
            this row L1-backed?".
        changelog_events: The provenance snapshot. Counted and type-checked;
            never read for content and never a source of identity (see the
            module docstring).

    Returns:
        A :class:`BackfillPlan` whose ``report`` accounts for every supplied row
        exactly once and whose ``updates`` contain only the rows that are
        eligible and carry no graph identity at all.
    """
    inventory = _Inventory()
    index, ambiguous = _index_entries(entries, inventory)
    _count_changelog(changelog_events, inventory)

    rows = _coerce_rows_for_report(vector_rows, inventory)
    duplicates = _duplicate_identities(rows)
    matched: set[str] = set()

    for position, row in rows:
        if row is None:
            inventory.classify(position, None, REASON_UNSUPPORTED_ROW)
            continue
        try:
            identity = normalize_seed_identity(row.id)
        except SeedIdentityError as exc:
            inventory.classify(
                position, row.id, REASON_UNUSABLE_IDENTITY, details=[str(exc)]
            )
            continue
        if identity in duplicates:
            inventory.warnings.add(f"{REASON_DUPLICATE_VECTOR_ROW}:{identity}")
            inventory.classify(position, row.id, REASON_DUPLICATE_VECTOR_ROW)
            continue
        if identity in ambiguous:
            inventory.classify(
                position,
                row.id,
                REASON_AMBIGUOUS_L1_IDENTITY,
                details=[ambiguous[identity]],
            )
            continue
        if identity not in index:
            inventory.classify(position, row.id, REASON_ORPHAN_VECTOR_ROW)
            continue

        matched.add(identity)
        _classify_eligible_row(position, row, identity, inventory)

    uncovered = len(index) - len(matched)
    if uncovered:
        inventory.warnings.add(f"l1_entries_without_vector_rows:{uncovered}")

    report = _build_report(
        rows_read=len(vector_rows),
        entries_read=len(entries),
        entries_indexed=len(index),
        changelog_events_read=len(changelog_events),
        entries_without_vector_rows=uncovered,
        fingerprint=vector_snapshot_fingerprint(vector_rows),
        inventory=inventory,
    )
    return BackfillPlan(
        report=report,
        updates=sorted(inventory.updates, key=lambda update: update.row_id),
    )


def _coerce_rows_for_report(
    vector_rows: Sequence[Any], inventory: _Inventory
) -> list[tuple[int, VectorSnapshotRow | None]]:
    """Coerce each row, keeping its position and reporting each refusal.

    ``None`` stands for "this position held something that is not a row", so the
    classification loop still visits it and the counts still balance.
    """
    coerced: list[tuple[int, VectorSnapshotRow | None]] = []
    for position, raw in enumerate(vector_rows):
        try:
            coerced.append((position, VectorSnapshotRow.from_snapshot(raw)))
        except TypeError:
            inventory.errors.add(
                f"unsupported_vector_row:{position}:{type(raw).__name__}"
            )
            coerced.append((position, None))
    return coerced


def _duplicate_identities(
    rows: Sequence[tuple[int, VectorSnapshotRow | None]],
) -> set[str]:
    """Normalized identities more than one supplied row claims."""
    seen: dict[str, int] = {}
    for _, row in rows:
        if row is None:
            continue
        try:
            identity = normalize_seed_identity(row.id)
        except SeedIdentityError:
            continue
        seen[identity] = seen.get(identity, 0) + 1
    return {identity for identity, count in seen.items() if count > 1}


def _index_entries(
    entries: Sequence[Any], inventory: _Inventory
) -> tuple[dict[str, Any], dict[str, str]]:
    """The L1 snapshot keyed by normalized identity, plus the ambiguous ones.

    An unsupported object and an entry whose ID has no stable form are reported
    by position — the same token shapes
    :meth:`~layered_memory_mcp.graph_legacy_importer.LegacyGraphImporter._usable_entries`
    uses — and contribute no identity.
    """
    index: dict[str, Any] = {}
    ambiguous: dict[str, str] = {}

    for position, entry in enumerate(entries):
        if not isinstance(entry, KnowledgeEntry):
            inventory.errors.add(
                f"unsupported_l1_entry:{position}:{type(entry).__name__}"
            )
            continue
        try:
            identity = normalize_seed_identity(entry.id)
        except SeedIdentityError:
            inventory.errors.add(f"invalid_l1_entry_id:{position}")
            continue

        prior = index.get(identity)
        if prior is None:
            index[identity] = entry
            continue
        if prior == entry:
            ambiguous[identity] = _DETAIL_DUPLICATE_L1
            inventory.warnings.add(f"{_DETAIL_DUPLICATE_L1}:{identity}")
        else:
            ambiguous[identity] = _DETAIL_CONFLICTING_L1
            inventory.errors.add(f"{_DETAIL_CONFLICTING_L1}:{identity}")

    for identity in ambiguous:
        index.pop(identity, None)
    return index, ambiguous


def _count_changelog(changelog_events: Sequence[Any], inventory: _Inventory) -> None:
    """Type-check the provenance snapshot. No event content is ever read."""
    for position, event in enumerate(changelog_events):
        if not isinstance(event, MemoryChangelogEntry):
            inventory.errors.add(
                f"unsupported_changelog_event:{position}:{type(event).__name__}"
            )


def _classify_eligible_row(
    position: int,
    row: VectorSnapshotRow,
    identity: str,
    inventory: _Inventory,
) -> None:
    """Classify a row whose identity resolved to exactly one L1 entry."""
    expected = graph_node_id_for_entry_id(identity)
    metadata, issues = _decode_metadata(row.metadata)
    if metadata is None:
        inventory.classify(
            position,
            row.id,
            REASON_MALFORMED_METADATA,
            expected=expected,
            details=issues,
        )
        return

    shape_issues, scalar, listed = _stored_graph_identity(metadata)
    if shape_issues:
        inventory.classify(
            position,
            row.id,
            REASON_MALFORMED_METADATA,
            expected=expected,
            details=shape_issues,
        )
        return

    if scalar is None and not listed:
        inventory.updates.append(
            MetadataUpdate(
                row_id=row.id,
                graph_node_id=expected,
                # A new dict: the row's own metadata is never mutated, and every
                # unrelated field is carried through untouched.
                metadata={**metadata, GRAPH_SEED_ID_KEY: expected},
            )
        )
        inventory.classify(
            position, row.id, REASON_MISSING_GRAPH_METADATA, expected=expected
        )
        return

    # A legacy row stands for exactly one claim, so *any* identity other than
    # the derived one is a disagreement — named by the field it sits in, never
    # by the value it holds.
    disagreeing: list[str] = []
    if scalar is not None and scalar != expected:
        disagreeing.append(f"{GRAPH_SEED_ID_KEY}:{_DETAIL_MISMATCH}")
    if any(value != expected for value in listed):
        disagreeing.append(f"{GRAPH_SEED_IDS_KEY}:{_DETAIL_MISMATCH}")
    if disagreeing:
        inventory.classify(
            position,
            row.id,
            REASON_MISMATCHED_GRAPH_METADATA,
            expected=expected,
            details=disagreeing,
        )
        return

    inventory.classify(
        position, row.id, REASON_ALREADY_MAPPED, expected=expected
    )


def _decode_metadata(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """``(metadata, issues)`` for one row's stored metadata column.

    ``({}, [])`` for a row that has no metadata at all — an absent column is a
    missing mapping, not a malformed one. ``(None, [...])`` when the column
    cannot be read as a JSON object; the issues are shape labels, never the
    value and never the decoder's own message.
    """
    if raw is None:
        return {}, []
    if isinstance(raw, Mapping):
        if any(not isinstance(key, str) for key in raw):
            return None, [f"{_METADATA_FIELD}:{_DETAIL_KEYS_NOT_STRINGS}"]
        return dict(raw), []
    if isinstance(raw, (str, bytes, bytearray)):
        if isinstance(raw, str) and not raw.strip():
            return {}, []
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None, [f"{_METADATA_FIELD}:{_DETAIL_NOT_JSON}"]
        if not isinstance(decoded, dict):
            return None, [f"{_METADATA_FIELD}:{_DETAIL_NOT_A_MAPPING}"]
        return decoded, []
    return None, [f"{_METADATA_FIELD}:{_DETAIL_NOT_A_MAPPING}"]


def _stored_graph_identity(
    metadata: Mapping[str, Any],
) -> tuple[list[str], str | None, list[str]]:
    """``(shape issues, scalar ID, listed IDs)`` for the graph keys, uncoerced.

    A stored graph ID must be an *exact* identifier: a non-blank, single-line
    ``str`` with no surrounding whitespace. A padded value is reported rather
    than trimmed into agreement — "already mapped" means the row already says
    the right thing, and ``" claim:x "`` does not say it.
    """
    issues: list[str] = []
    scalar: str | None = None
    listed: list[str] = []

    if GRAPH_SEED_ID_KEY in metadata:
        problem = _graph_id_problem(metadata[GRAPH_SEED_ID_KEY])
        if problem:
            issues.append(f"{GRAPH_SEED_ID_KEY}:{problem}")
        else:
            scalar = metadata[GRAPH_SEED_ID_KEY]

    if GRAPH_SEED_IDS_KEY in metadata:
        raw = metadata[GRAPH_SEED_IDS_KEY]
        if isinstance(raw, (list, tuple)):
            for value in raw:
                problem = _graph_id_problem(value)
                if problem:
                    issues.append(
                        f"{GRAPH_SEED_IDS_KEY}:{_DETAIL_ELEMENT}{problem}"
                    )
                elif value not in listed:
                    listed.append(value)
        else:
            # A bare string here is a caller mistake, not a character sequence —
            # the same rule ``build_seed_mapping`` already applies.
            issues.append(f"{GRAPH_SEED_IDS_KEY}:{_DETAIL_NOT_A_LIST}")

    return sorted(set(issues)), scalar, listed


def _graph_id_problem(value: Any) -> str:
    """Why ``value`` is not an exact stored graph node ID, or ``""``."""
    if isinstance(value, bool) or not isinstance(value, str):
        return _DETAIL_NOT_A_STRING
    if "\n" in value or "\r" in value:
        return _DETAIL_MULTILINE
    if not value.strip():
        return _DETAIL_BLANK
    if value != value.strip():
        return _DETAIL_PADDED
    return ""


def _build_report(
    *,
    rows_read: int,
    entries_read: int,
    entries_indexed: int,
    changelog_events_read: int,
    entries_without_vector_rows: int,
    fingerprint: str,
    inventory: _Inventory,
) -> BackfillReport:
    """Count the verdicts into the partition :attr:`BackfillReport` promises."""
    counts = {reason: 0 for reason in ALL_REASONS}
    for verdict in inventory.verdicts:
        counts[verdict.reason] = counts.get(verdict.reason, 0) + 1

    return BackfillReport(
        rows_read=rows_read,
        entries_read=entries_read,
        entries_indexed=entries_indexed,
        changelog_events_read=changelog_events_read,
        eligible=sum(counts[reason] for reason in ELIGIBLE_REASONS),
        already_mapped=counts[REASON_ALREADY_MAPPED],
        planned_updates=counts[REASON_MISSING_GRAPH_METADATA],
        quarantined=sum(counts[reason] for reason in QUARANTINE_REASONS),
        unusable_identity=counts[REASON_UNUSABLE_IDENTITY],
        orphan_rows=counts[REASON_ORPHAN_VECTOR_ROW],
        ambiguous_identity=counts[REASON_AMBIGUOUS_L1_IDENTITY],
        entries_without_vector_rows=entries_without_vector_rows,
        counts_by_reason={
            reason: counts[reason] for reason in ALL_REASONS if counts[reason]
        },
        snapshot_fingerprint=fingerprint,
        verdicts=inventory.verdicts,
        warnings=sorted(inventory.warnings),
        errors=sorted(inventory.errors),
    )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

class BackfillRollback:
    """The prior metadata of exactly the rows a plan intends to change.

    Deliberately a plain object rather than a model: it holds real metadata so
    it can put a row back byte-for-byte, which makes it working material rather
    than something to serialize beside a memory answer. The reportable half is
    :attr:`fingerprint` and :attr:`row_ids`.

    Rollback here is *projection* rollback (§12 Batch I-C): it restores vector
    metadata only. The L1 snapshot and the append-only changelog are never
    rewritten, never deleted and never read by it.
    """

    __slots__ = ("_prior", "_fingerprint")

    def __init__(self, prior: Mapping[str, str | None], fingerprint: str) -> None:
        self._prior = {row_id: prior[row_id] for row_id in sorted(prior)}
        self._fingerprint = fingerprint

    @property
    def fingerprint(self) -> str:
        """The fingerprint of the snapshot this handle restores *to*."""
        return self._fingerprint

    @property
    def row_ids(self) -> list[str]:
        """The rows it would write, sorted. Identities only."""
        return list(self._prior)

    def restore(self, store: Any, *, expected: Sequence[tuple] | None = None) -> int:
        """Write the prior metadata back through the store's atomic boundary.

        Args:
            store: The same explicitly supplied store the apply used. Nothing is
                discovered: no path, no default database, no config.
            expected: The snapshot to publish against. ``None`` reads the
                store's current committed snapshot, which is the normal
                "restore what I just published" case; the store re-validates it
                inside the write transaction either way.

        Returns:
            How many rows actually changed. Zero means the rows already held
            their prior metadata.
        """
        if not self._prior:
            return 0
        snapshot = store.snapshot() if expected is None else expected
        return store.patch_metadata(dict(self._prior), expected=snapshot)


def capture_rollback(plan: BackfillPlan, rows: Sequence[Any]) -> BackfillRollback:
    """Capture the prior metadata of the rows ``plan`` would change.

    Pure, and derived from the explicitly supplied snapshot rather than from a
    store read, so the captured state is exactly the state the plan was built
    against.

    Raises:
        KeyError: A planned row is absent from ``rows``. Capturing a rollback
            for a row nobody can see would make the restoration a guess.
    """
    by_id = {
        row.id: _metadata_text(row.metadata)
        for row in coerce_snapshot_rows(
            [row for row in rows if _is_row_like(row)]
        )
        # A row ID that is not a string is not a key this store can be asked to
        # patch, so it is also not a row a rollback can restore.
        if isinstance(row.id, str)
    }
    prior: dict[str, str | None] = {}
    for update in plan.updates:
        if update.row_id not in by_id:
            raise KeyError(update.row_id)
        prior[update.row_id] = by_id[update.row_id]
    return BackfillRollback(prior, vector_snapshot_fingerprint(rows))


def _is_row_like(row: Any) -> bool:
    try:
        VectorSnapshotRow.from_snapshot(row)
    except TypeError:
        return False
    return True


# ---------------------------------------------------------------------------
# The apply coordinator
# ---------------------------------------------------------------------------

class PublicationResult(GraphModel):
    """What one apply attempt did, as bounded, serializable data.

    The fingerprints are the honest before/after record: equal fingerprints
    after a reported ``rows_changed`` of zero is a consistent story, and a
    changed fingerprint without a changed row count would not be.
    """

    status: str = BACKFILL_NOTHING_TO_DO
    dry_run: bool = False
    planned_updates: int = Field(default=0, ge=0)
    rows_changed: int = Field(default=0, ge=0)
    unresolved_rows: int = Field(default=0, ge=0)
    fingerprint_before: str = ""
    fingerprint_after: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def applied(self) -> bool:
        """Whether a publication actually reached the store cleanly."""
        return self.status == BACKFILL_APPLIED and not self.errors

    @property
    def ok(self) -> bool:
        return not self.errors


class BackfillApplication:
    """One apply attempt: its :attr:`result` and its :attr:`rollback` handle.

    Two objects rather than one for the reason §12 Batch I-C separates them: the
    result is the loggable claim about what happened, and the rollback is the
    material needed to undo it. Mixing them would put real metadata inside the
    thing a caller reports.
    """

    __slots__ = ("_result", "_rollback")

    def __init__(
        self, result: PublicationResult, rollback: BackfillRollback | None
    ) -> None:
        self._result = result
        self._rollback = rollback

    @property
    def result(self) -> PublicationResult:
        return self._result

    @property
    def rollback(self) -> BackfillRollback | None:
        """The restoration handle, or ``None`` when nothing could be captured."""
        return self._rollback

    @property
    def status(self) -> str:
        return self._result.status

    @property
    def rows_changed(self) -> int:
        return self._result.rows_changed

    @property
    def applied(self) -> bool:
        return self._result.applied


def apply_graph_seed_backfill(
    store: Any,
    plan: BackfillPlan,
    *,
    expected: Sequence[tuple],
    dry_run: bool = False,
    require_clean: bool = True,
    validate_source: Any = None,
) -> BackfillApplication:
    """Publish ``plan``'s metadata updates through the store's atomic boundary.

    Everything is explicit. The store is handed in, the snapshot the plan was
    built against is handed in, and nothing here resolves a source root, a
    default database path, a config or a memory home.

    The refusal order, most-fundamental first:

    1. the plan names a row the snapshot does not contain
       (:data:`BACKFILL_PLAN_SNAPSHOT_MISMATCH`);
    2. the store no longer holds that snapshot
       (:data:`BACKFILL_STALE_SNAPSHOT`);
    3. ``dry_run`` — report and write nothing (:data:`BACKFILL_DRY_RUN`);
    4. the plan reports an input-contract error
       (:data:`BACKFILL_INVALID_INPUTS`) — unconditional, see ``require_clean``;
    5. the plan quarantined rows and ``require_clean`` is set
       (:data:`BACKFILL_UNRESOLVED_ROWS`);
    6. there is nothing to publish (:data:`BACKFILL_NOTHING_TO_DO`);
    7. publish.

    Args:
        store: An explicitly supplied object exposing ``snapshot()`` and
            ``patch_metadata()`` — normally a
            :class:`~layered_memory_mcp.storage.vector_store.VectorStore`.
        plan: The plan from :func:`plan_graph_seed_backfill`.
        expected: The snapshot the plan was built against. Compared here *and*
            again inside the store's write transaction.
        dry_run: Report what would happen and write nothing.
        require_clean: Abort when the plan has unresolved rows, as §12 Batch
            I-C requires. Set ``False`` to publish a partially resolved
            inventory deliberately; the count stays visible in the result. It
            overrides *row coverage* only: a plan carrying
            :attr:`BackfillReport.errors` is refused either way, because an
            input the planner could not read is a broken call rather than an
            incomplete inventory.
        validate_source: Passed to the store, which calls it inside the write
            transaction. A raising validator aborts the publication.

    Returns:
        A :class:`BackfillApplication`. A refused or failed publication is a
        status, never an exception, and the previous committed rows are intact.
        Its ``rollback`` is present whenever the snapshot it restores to is the
        current one; it is ``None`` for a stale or foreign snapshot, because
        restoring a snapshot another writer has moved past is a clobber rather
        than an undo.

    Raises:
        ValueError: Only for a caller-contract mistake — a store that cannot
            snapshot or patch metadata.
    """
    for operation in ("snapshot", "patch_metadata"):
        if not callable(getattr(store, operation, None)):
            raise ValueError(f"store is missing the {operation} operation")

    report = plan.report
    fingerprint_before = vector_snapshot_fingerprint(expected)
    warnings: list[str] = []
    if report.unresolved_rows:
        warnings.append(f"{BACKFILL_UNRESOLVED_ROWS}:{report.unresolved_rows}")
    if not report.counts_balance:
        warnings.append("backfill_counts_unbalanced")

    def _result(status: str, **fields: Any) -> PublicationResult:
        extra = fields.pop("extra_warnings", ())
        return PublicationResult(
            status=status,
            dry_run=dry_run,
            planned_updates=len(plan.updates),
            unresolved_rows=report.unresolved_rows,
            fingerprint_before=fingerprint_before,
            fingerprint_after=fields.pop("fingerprint_after", fingerprint_before),
            warnings=sorted({*warnings, *extra}),
            **fields,
        )

    try:
        rollback: BackfillRollback | None = capture_rollback(plan, expected)
    except KeyError as exc:
        return BackfillApplication(
            _result(
                BACKFILL_PLAN_SNAPSHOT_MISMATCH,
                errors=[f"plan_row_not_in_snapshot:{exc.args[0]}"],
            ),
            None,
        )

    live = list(store.snapshot())
    if live != list(expected):
        # No rollback handle: it would restore a snapshot somebody else has
        # already moved past, which is a clobber dressed up as an undo. The
        # caller replans against the current snapshot instead.
        return BackfillApplication(
            _result(
                BACKFILL_STALE_SNAPSHOT,
                errors=[f"{BACKFILL_STALE_SNAPSHOT}:retry_required"],
                fingerprint_after=vector_snapshot_fingerprint(live),
            ),
            None,
        )

    if dry_run:
        # Nothing is written, so "after" is "before" by construction.
        return BackfillApplication(_result(BACKFILL_DRY_RUN), rollback)

    if report.errors:
        # Deliberately ahead of — and independent of — ``require_clean``. An
        # input-contract error says the inventory does not describe the trio it
        # was handed (an object that is not an L1 entry contributes no identity,
        # so a row can be planned as "missing metadata" while really being an
        # orphan), and that is not a coverage decision a caller gets to
        # override. The plan's own stable tokens are carried through as-is: they
        # are positions and type names, never values (§15).
        return BackfillApplication(
            _result(BACKFILL_INVALID_INPUTS, errors=list(report.errors)), rollback
        )

    if require_clean and report.unresolved_rows:
        return BackfillApplication(_result(BACKFILL_UNRESOLVED_ROWS), rollback)

    if not plan.updates:
        return BackfillApplication(_result(BACKFILL_NOTHING_TO_DO), rollback)

    try:
        changed = store.patch_metadata(
            plan.metadata_updates(),
            expected=list(expected),
            validate_source=validate_source,
        )
    except Exception as exc:  # noqa: BLE001 — named by class, never quoted (§15)
        return BackfillApplication(
            _result(
                BACKFILL_PUBLICATION_FAILED,
                errors=[f"{BACKFILL_PUBLICATION_FAILED}:{type(exc).__name__}"],
                fingerprint_after=vector_snapshot_fingerprint(store.snapshot()),
            ),
            rollback,
        )

    return BackfillApplication(
        _result(
            BACKFILL_APPLIED,
            rows_changed=int(changed),
            fingerprint_after=vector_snapshot_fingerprint(store.snapshot()),
            extra_warnings=(
                ["backfill_no_rows_changed"] if not changed else ()
            ),
        ),
        rollback,
    )


def backfill_payload(plan: BackfillPlan, result: PublicationResult | None = None):
    """The bounded, plain-JSON view of one inventory and (optionally) its apply.

    A fixed key set of counts, statuses, fingerprints and labels, so the block
    can be logged or returned beside an MCP response without carrying a body, a
    vector, a path or a stored metadata value (§15). Per-row verdicts are
    included as identity/reason/expected-ID triples — the actionable part — and
    nothing else.
    """
    report = plan.report
    payload: dict[str, Any] = {
        "rows_read": report.rows_read,
        "entries_read": report.entries_read,
        "changelog_events_read": report.changelog_events_read,
        "eligible": report.eligible,
        "already_mapped": report.already_mapped,
        "planned_updates": report.planned_updates,
        "quarantined": report.quarantined,
        "unusable_identity": report.unusable_identity,
        "orphan_rows": report.orphan_rows,
        "ambiguous_identity": report.ambiguous_identity,
        "entries_without_vector_rows": report.entries_without_vector_rows,
        "unresolved_rows": report.unresolved_rows,
        "counts_balance": report.counts_balance,
        "counts_by_reason": dict(report.counts_by_reason),
        "snapshot_fingerprint": report.snapshot_fingerprint,
        "verdicts": [
            {
                "row_index": verdict.row_index,
                "row_id": verdict.row_id,
                "reason": verdict.reason,
                "expected_graph_node_id": verdict.expected_graph_node_id,
                "details": list(verdict.details),
            }
            for verdict in report.verdicts
        ],
        "warnings": list(report.warnings),
        "errors": list(report.errors),
    }
    if result is not None:
        payload["publication"] = {
            "status": result.status,
            "dry_run": result.dry_run,
            "planned_updates": result.planned_updates,
            "rows_changed": result.rows_changed,
            "fingerprint_before": result.fingerprint_before,
            "fingerprint_after": result.fingerprint_after,
            "applied": result.applied,
            "warnings": list(result.warnings),
            "errors": list(result.errors),
        }
    return payload


__all__ = [
    "ALL_REASONS",
    "BACKFILL_APPLIED",
    "BACKFILL_DRY_RUN",
    "BACKFILL_INVALID_INPUTS",
    "BACKFILL_NOTHING_TO_DO",
    "BACKFILL_PLAN_SNAPSHOT_MISMATCH",
    "BACKFILL_PUBLICATION_FAILED",
    "BACKFILL_STALE_SNAPSHOT",
    "BACKFILL_UNRESOLVED_ROWS",
    "ELIGIBLE_REASONS",
    "QUARANTINE_REASONS",
    "REASON_ALREADY_MAPPED",
    "REASON_AMBIGUOUS_L1_IDENTITY",
    "REASON_DUPLICATE_VECTOR_ROW",
    "REASON_MALFORMED_METADATA",
    "REASON_MISMATCHED_GRAPH_METADATA",
    "REASON_MISSING_GRAPH_METADATA",
    "REASON_ORPHAN_VECTOR_ROW",
    "REASON_UNSUPPORTED_ROW",
    "REASON_UNUSABLE_IDENTITY",
    "BackfillApplication",
    "BackfillPlan",
    "BackfillReport",
    "BackfillRollback",
    "MetadataUpdate",
    "PublicationResult",
    "RowVerdict",
    "VectorSnapshotRow",
    "apply_graph_seed_backfill",
    "backfill_payload",
    "capture_rollback",
    "coerce_snapshot_rows",
    "plan_graph_seed_backfill",
    "vector_snapshot_fingerprint",
]

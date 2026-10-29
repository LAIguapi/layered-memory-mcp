"""Read-only inventory of one explicitly supplied old-format memory snapshot.

WHAT THIS MODULE IS
-------------------
The planning half of :mod:`~layered_memory_mcp.graph_migration`. It reads an
operator-named source directory and answers four questions, without writing a
single byte anywhere:

* **what is in it** — a full recursive inventory of the explicit layout below,
  fingerprinted over every byte *and* over membership, so a later publication
  can prove the snapshot did not move underneath it;
* **what is unusable** — a symlink, a special file, an unknown record, a
  non-empty write-ahead log, an unreadable document, a document the projection
  producer and the canonical readable-surface reader disagree about
  (:func:`section_reader_findings`): each named as a bounded finding rather
  than skipped;
* **which authored objects it holds** — the L1 entries, through the *actual*
  projection producer (:func:`~layered_memory_mcp.injector._vector_entries`),
  so a frontmatter ``id`` is preserved verbatim and an ordinary H2 section gets
  the versioned identity
  :func:`~layered_memory_mcp.graph_entry_identity.markdown_section_entry_id`
  mints — never a filename, a heading guess or a fresh UUID;
* **what every vector row is** — exactly one of six mutually exclusive
  categories, with the counts forming an exact partition of the rows read.

THE EXPLICIT SOURCE LAYOUT
--------------------------
::

    <source>/
        knowledge/<domain>.md               the L1 documents (required)
        data/vectors.db                     the legacy vector store (required)
        data/memory_changelog.jsonl         historical evidence (optional)

Anything else under ``<source>`` is an unsupported record and blocks a
publication. That is deliberate: the operator supplies a *snapshot*, and a
migration that quietly ignored the files it did not recognise would be claiming
to have accounted for a store it only partly read.

WHAT IT IS NOT
--------------
- NOT a discoverer. The source is an argument. No home directory, no
  ``config.yaml``, no environment variable and no default path is consulted,
  and nothing here constructs a
  :class:`~layered_memory_mcp.config.MemoryConfig`.
- NOT a writer. The vector database is opened through a SQLite
  ``mode=ro&immutable=1`` URI — never
  :class:`~layered_memory_mcp.storage.vector_store.VectorStore`, whose
  constructor creates directories, a schema and an index — so an inventory
  cannot create, lock, migrate or WAL-checkpoint the thing it is inspecting.
- NOT a second identity algorithm. Every expected graph node ID comes from
  :func:`~layered_memory_mcp.graph_seed_metadata.graph_node_id_for_entry_id`,
  and every row classification comes from
  :func:`~layered_memory_mcp.graph_backfill.plan_graph_seed_backfill`.
- NOT a guesser. Legacy correspondence requires an exact domain, declared
  section and projection text, or a validated operator declaration. Ambiguous
  and conflicting identities remain blockers.

PRIVACY (§15)
-------------
:class:`SourceInventory` carries counts, relative paths, row identities,
*derived* node IDs, hashes and stable reason tokens. Never a body, a summary, a
heading, a vector value or an exception message — a refusal names the record
and the problem, not the content.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from pydantic import Field

from .graph_backfill import (
    REASON_ALREADY_MAPPED,
    REASON_AMBIGUOUS_L1_IDENTITY,
    REASON_DUPLICATE_VECTOR_ROW,
    REASON_MALFORMED_METADATA,
    REASON_MISMATCHED_GRAPH_METADATA,
    REASON_MISSING_GRAPH_METADATA,
    REASON_ORPHAN_VECTOR_ROW,
    REASON_UNSUPPORTED_ROW,
    REASON_UNUSABLE_IDENTITY,
    BackfillPlan,
    _decode_metadata,
    _stored_graph_identity,
    plan_graph_seed_backfill,
)
from .graph_entry_identity import ProjectionIdentityError, markdown_section_entry_id
from .graph_models import GraphModel
from .graph_runtime import GRAPH_SEED_ID_KEY
from .graph_seed_metadata import graph_node_id_for_entry_id
from .heading import embedding_heading
from .memory_changelog import MemoryChangelogEntry
from .models import KnowledgeEntry

__all__ = [
    "CATEGORY_CONFLICTING",
    "CATEGORY_EXISTING",
    "CATEGORY_MALFORMED",
    "CATEGORY_MAPPED",
    "CATEGORY_MISSING",
    "CATEGORY_ORPHANED",
    "CATEGORY_PRECEDENCE",
    "FILE_KIND_CHANGELOG",
    "FILE_KIND_L1_MARKDOWN",
    "FILE_KIND_UNSUPPORTED",
    "FILE_KIND_VECTOR_DB",
    "FILE_KIND_VECTOR_SIDECAR",
    "PUBLISHABLE_CATEGORIES",
    "ROW_CATEGORIES",
    "SOURCE_CHANGELOG",
    "SOURCE_DATA_DIR",
    "SOURCE_KNOWLEDGE_DIR",
    "SOURCE_VECTOR_DB",
    "L1Entry",
    "RowClassification",
    "SourceFile",
    "SourceInventory",
    "SourceSnapshot",
    "file_sha256",
    "read_source_snapshot",
    "read_vector_rows",
    "resolve_identity_map",
    "section_reader_findings",
]

# ---------------------------------------------------------------------------
# The explicit layout
# ---------------------------------------------------------------------------

SOURCE_KNOWLEDGE_DIR = "knowledge"
SOURCE_DATA_DIR = "data"
SOURCE_VECTOR_DB = "data/vectors.db"
SOURCE_CHANGELOG = "data/memory_changelog.jsonl"

#: SQLite's own companion files for the vector database. Inventoried (so a
#: non-empty one is *visible*) rather than ignored, and never copied.
_VECTOR_SIDECARS: tuple[str, ...] = (
    "data/vectors.db-wal",
    "data/vectors.db-shm",
    "data/vectors.db-journal",
)

#: The directories the layout allows. A directory outside this set is an
#: unsupported record, not a place to go looking.
_ALLOWED_DIRS = frozenset({SOURCE_KNOWLEDGE_DIR, SOURCE_DATA_DIR})

FILE_KIND_L1_MARKDOWN = "l1_markdown"
FILE_KIND_VECTOR_DB = "vector_db"
FILE_KIND_VECTOR_SIDECAR = "vector_sidecar"
FILE_KIND_CHANGELOG = "changelog"
FILE_KIND_UNSUPPORTED = "unsupported"

#: The columns a legacy ``vectors`` table must expose. Checked rather than
#: assumed: a table of another shape is reported, not read as if it matched.
_VECTOR_COLUMNS: tuple[str, ...] = ("id", "domain", "text", "vector", "metadata")


# ---------------------------------------------------------------------------
# Findings — bounded tokens, never a value (§15)
# ---------------------------------------------------------------------------

FINDING_SOURCE_MISSING = "source_missing"
FINDING_SOURCE_NOT_A_DIRECTORY = "source_not_a_directory"
FINDING_SOURCE_SYMLINK = "source_symlink"
FINDING_SOURCE_SPECIAL_FILE = "source_special_file"
FINDING_SOURCE_UNSUPPORTED_RECORD = "source_unsupported_record"
FINDING_SOURCE_PATH_ESCAPE = "source_path_escape"
FINDING_SOURCE_UNREADABLE = "source_unreadable"
FINDING_NO_L1_DOCUMENTS = "source_no_l1_documents"
FINDING_L1_UNREADABLE = "l1_unreadable"
FINDING_L1_IDENTITY_REFUSED = "l1_identity_refused"
FINDING_SECTION_READER_INCOMPATIBLE = "l1_section_reader_incompatible"
FINDING_VECTOR_DB_MISSING = "vector_db_missing"
FINDING_VECTOR_DB_UNREADABLE = "vector_db_unreadable"
FINDING_VECTOR_SCHEMA_UNKNOWN = "vector_schema_unknown"
FINDING_VECTOR_WAL_NOT_EMPTY = "vector_wal_not_empty"
FINDING_CHANGELOG_UNREADABLE = "changelog_unreadable"
FINDING_CHANGELOG_TRUNCATED = "changelog_read_truncated"
FINDING_CHANGELOG_MALFORMED = "changelog_malformed_lines"
FINDING_IDENTITY_MAP_UNREADABLE = "identity_map_unreadable"
FINDING_IDENTITY_MAP_INVALID = "identity_map_invalid"
FINDING_IDENTITY_MAP_ROW_UNKNOWN = "identity_map_row_unknown"
FINDING_IDENTITY_MAP_ROW_NOT_ORPHANED = "identity_map_row_not_orphaned"
FINDING_IDENTITY_MAP_ENTRY_UNKNOWN = "identity_map_entry_unknown"
FINDING_IDENTITY_MAP_ENTRY_TAKEN = "identity_map_entry_taken"
FINDING_INVENTORY_UNBALANCED = "inventory_counts_unbalanced"
FINDING_PLANNER_INPUT = "vector_planner_input_refused"


# ---------------------------------------------------------------------------
# Row categories
# ---------------------------------------------------------------------------

#: The row already carries exactly the graph identity its own L1 identity
#: derives. Nothing to publish for it, and nothing wrong with it.
CATEGORY_EXISTING = "existing"

#: The row resolves to exactly one L1 entry and carries no graph identity yet.
#: The only category a migration publishes metadata for.
CATEGORY_MAPPED = "mapped"

#: The row's retrieval payload is unusable — absent, undecodable, non-finite or
#: of an unexpected dimension. Blocking, because the alternative is inventing an
#: embedding, and a fabricated vector is worse than a refused migration.
CATEGORY_MISSING = "missing"

#: The row's identity is usable and the supplied L1 snapshot has no such entry.
#: Only a validated declaration or exact, unique correspondence can resolve it.
CATEGORY_ORPHANED = "orphaned"

#: Two rows claim one identity, the snapshot holds two records for it, or the
#: row's stored graph identity disagrees with the derived one.
CATEGORY_CONFLICTING = "conflicting"

#: The row, its ID or its metadata has a shape this tool refuses to interpret.
CATEGORY_MALFORMED = "malformed"

ROW_CATEGORIES: tuple[str, ...] = (
    CATEGORY_MAPPED,
    CATEGORY_EXISTING,
    CATEGORY_MISSING,
    CATEGORY_ORPHANED,
    CATEGORY_CONFLICTING,
    CATEGORY_MALFORMED,
)

#: The categories a publication may contain. Everything else blocks.
PUBLISHABLE_CATEGORIES = frozenset({CATEGORY_MAPPED, CATEGORY_EXISTING})

#: A row can be wrong in more than one way — an unreadable metadata column on a
#: row whose vector is also empty — so the category is decided by a *fixed*
#: precedence rather than by whichever check ran last. Shape problems first
#: (nothing else about the row can be trusted), then identity disagreements,
#: then missing retrieval data, then an unresolved identity, then the two clean
#: answers. Stated as data so the rule is readable and assertable.
CATEGORY_PRECEDENCE: tuple[str, ...] = (
    CATEGORY_MALFORMED,
    CATEGORY_CONFLICTING,
    CATEGORY_MISSING,
    CATEGORY_ORPHANED,
    CATEGORY_EXISTING,
    CATEGORY_MAPPED,
)

#: The planner's reason code → this tool's category. One mapping, so "which
#: bucket is a mismatched identity in" has exactly one answer.
_CATEGORY_BY_REASON: dict[str, str] = {
    REASON_ALREADY_MAPPED: CATEGORY_EXISTING,
    REASON_MISSING_GRAPH_METADATA: CATEGORY_MAPPED,
    REASON_MISMATCHED_GRAPH_METADATA: CATEGORY_CONFLICTING,
    REASON_DUPLICATE_VECTOR_ROW: CATEGORY_CONFLICTING,
    REASON_AMBIGUOUS_L1_IDENTITY: CATEGORY_CONFLICTING,
    REASON_MALFORMED_METADATA: CATEGORY_MALFORMED,
    REASON_UNSUPPORTED_ROW: CATEGORY_MALFORMED,
    REASON_UNUSABLE_IDENTITY: CATEGORY_MALFORMED,
    REASON_ORPHAN_VECTOR_ROW: CATEGORY_ORPHANED,
}

#: Why a row's retrieval payload is unusable. Shape labels, never the payload.
DETAIL_VECTOR_ABSENT = "vector_absent"
DETAIL_VECTOR_NOT_JSON = "vector_not_json"
DETAIL_VECTOR_NOT_A_LIST = "vector_not_a_list"
DETAIL_VECTOR_NOT_NUMERIC = "vector_not_numeric"
DETAIL_VECTOR_NOT_FINITE = "vector_not_finite"
DETAIL_VECTOR_DIMENSION = "vector_dimension_unexpected"

#: The reason token for a row an explicit mapping resolved.
REASON_MAPPED_BY_IDENTITY_MAP = "mapped_by_identity_map"
REASON_MAPPED_BY_EXACT_IDENTITY = "mapped_by_exact_identity"
REASON_MAPPED_BY_EXACT_EMBEDDING_PROJECTION = "mapped_by_exact_embedding_projection"


# ---------------------------------------------------------------------------
# Reportable shapes
# ---------------------------------------------------------------------------

class SourceFile(GraphModel):
    """One inventoried source record: where it is, how big, what it hashes to."""

    path: str
    kind: str
    size: int = Field(default=0, ge=0)
    sha256: str = ""


class RowClassification(GraphModel):
    """What one vector row was classified as, and why.

    ``row_index`` is the position in the read-only snapshot and is the
    identifier of record; ``row_id`` is empty whenever the row's own ID is not a
    clean single-line string, because a token holding a blank or multi-line
    value is neither loggable nor diffable.
    """

    row_index: int = Field(default=0, ge=0)
    row_id: str = ""
    category: str
    reason: str
    expected_graph_node_id: str = ""
    mapping_origin: str = "none"
    target_entry_id: str = ""
    candidate_count: int = Field(default=0, ge=0)
    details: list[str] = Field(default_factory=list)


class SourceInventory(GraphModel):
    """The content-free inventory of one source snapshot.

    The category counts are a *partition* of ``vector_rows``::

        vector_rows == mapped + existing + missing + orphaned
                       + conflicting + malformed

    which :attr:`counts_balance` asserts, so "every row was accounted for" is
    arithmetic rather than a hope.
    """

    source: str = ""
    destination: str = ""
    destination_prestate: str = ""
    source_fingerprint: str = ""
    file_count: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    files: list[SourceFile] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    l1_documents: int = Field(default=0, ge=0)
    l1_entries: int = Field(default=0, ge=0)
    frontmatter_entries: int = Field(default=0, ge=0)
    section_entries: int = Field(default=0, ge=0)
    vector_rows: int = Field(default=0, ge=0)
    changelog_events: int = Field(default=0, ge=0)
    counts_by_category: dict[str, int] = Field(default_factory=dict)
    classifications: list[RowClassification] = Field(default_factory=list)
    identity_map_entries: int = Field(default=0, ge=0)
    automatic_identity_map_entries: int = Field(default=0, ge=0)
    findings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def accounted_rows(self) -> int:
        return sum(self.counts_by_category.get(name, 0) for name in ROW_CATEGORIES)

    @property
    def counts_balance(self) -> bool:
        return (
            self.accounted_rows == self.vector_rows == len(self.classifications)
        )

    @property
    def publishable(self) -> bool:
        """Whether this inventory may be published at all."""
        return not self.findings and self.counts_balance

    def to_payload(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["counts_balance"] = self.counts_balance
        payload["accounted_rows"] = self.accounted_rows
        return payload


# ---------------------------------------------------------------------------
# Working material — the half that carries real content
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class L1Entry:
    """One authored L1 object, with the surface identity it was read from.

    ``occurrence`` is the zero-based position among *identical* H2 labels in the
    domain, as :func:`markdown_section_entry_ids` assigns it, and is ``None``
    for a frontmatter-backed entry — those have an authoritative ``id`` and are
    not an H2-section surface this framework owns.
    """

    entry: KnowledgeEntry
    domain: str
    section: str
    occurrence: int | None
    frontmatter_owned: bool

    @property
    def entry_id(self) -> str:
        return self.entry.id


@dataclass
class SourceSnapshot:
    """The frozen reading of one source: its inventory plus its real content.

    Deliberately split the way :mod:`~layered_memory_mcp.graph_backfill` splits
    :class:`~layered_memory_mcp.graph_backfill.BackfillPlan` from its report:
    :attr:`inventory` is the loggable claim about the source, everything else is
    the working material a staged generation is built from.
    """

    source: Path
    inventory: SourceInventory
    documents: dict[str, str] = field(default_factory=dict)
    entries: list[L1Entry] = field(default_factory=list)
    vector_rows: list[tuple] = field(default_factory=list)
    changelog_events: list[MemoryChangelogEntry] = field(default_factory=list)
    identity_map: dict[str, str] = field(default_factory=dict)
    automatic_identity_map: dict[str, str] = field(default_factory=dict)
    #: ``row_id -> (graph node ID, complete replacement metadata)`` for the rows
    #: an explicit or automatic mapping resolved. The planner only
    #: ever derives an identity from the row's *own* ID.
    identity_map_updates: dict[str, tuple[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    backfill_plan: BackfillPlan | None = None

    @property
    def publishable(self) -> bool:
        return self.inventory.publishable

    def bound_entries(self) -> list[L1Entry]:
        """The entries whose readable surface this framework can own."""
        return [item for item in self.entries if not item.frontmatter_owned]

    def external_entry_ids(self) -> list[str]:
        """Entry IDs whose readable surface stays externally owned."""
        return sorted(item.entry_id for item in self.entries if item.frontmatter_owned)


# ---------------------------------------------------------------------------
# Hashing and the recursive walk
# ---------------------------------------------------------------------------

_HASH_CHUNK = 1 << 20


def file_sha256(path: Path) -> str:
    """The content hash of one file, read in bounded chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path) -> Iterator[tuple[str, str, Path]]:
    """``(kind, relative path, absolute path)`` for everything under ``root``.

    Hand-rolled on :func:`os.scandir` with ``follow_symlinks=False`` rather than
    :meth:`~pathlib.Path.rglob`: a symlink must be *reported* as a symlink and
    never descended into, and a glob that quietly followed one would inventory a
    directory the operator never supplied.
    """
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        current, prefix = stack.pop()
        try:
            with os.scandir(current) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            yield "unreadable", prefix, current
            continue
        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            path = Path(child.path)
            if child.is_symlink():
                yield "symlink", relative, path
            elif child.is_dir(follow_symlinks=False):
                yield "dir", relative, path
                stack.append((path, relative))
            elif child.is_file(follow_symlinks=False):
                yield "file", relative, path
            else:
                yield "special", relative, path


def _file_kind(relative: str) -> str:
    if relative == SOURCE_VECTOR_DB:
        return FILE_KIND_VECTOR_DB
    if relative == SOURCE_CHANGELOG:
        return FILE_KIND_CHANGELOG
    if relative in _VECTOR_SIDECARS:
        return FILE_KIND_VECTOR_SIDECAR
    head, _, tail = relative.partition("/")
    if (
        head == SOURCE_KNOWLEDGE_DIR
        and tail
        and "/" not in tail
        and tail.endswith(".md")
    ):
        return FILE_KIND_L1_MARKDOWN
    return FILE_KIND_UNSUPPORTED


# ---------------------------------------------------------------------------
# The read-only vector database reader
# ---------------------------------------------------------------------------

def read_vector_rows(path: Path) -> list[tuple]:
    """Every row of a legacy ``vectors`` table, in ``id`` order, read-only.

    Opened through ``mode=ro&immutable=1``. ``mode=ro`` refuses a write;
    ``immutable=1`` additionally tells SQLite the file cannot change, so it
    takes no lock, creates no ``-shm``, replays no write-ahead log and leaves
    the modification time alone. That is what makes this a *coherent* read of an
    untouched snapshot rather than a recovery of one — and the reason a
    non-empty WAL is refused before this is ever called: with ``immutable=1``
    its contents would be silently invisible.

    Returns:
        ``(id, domain, text, vector, metadata)`` tuples, the same shape and the
        same order
        :meth:`~layered_memory_mcp.storage.vector_store.VectorStore.snapshot`
        produces, so they can be planned over without conversion.

    Raises:
        sqlite3.Error: The database could not be opened or read.
    """
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return connection.execute(
            "SELECT id, domain, text, vector, metadata FROM vectors ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def _vector_table_columns(path: Path) -> list[str]:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(vectors)").fetchall()
        ]
    finally:
        connection.close()


def _vector_payload_problem(raw: Any, dimension: int) -> str:
    """Why a row's stored vector is unusable, or ``""``. Reads no value out."""
    if raw is None:
        return DETAIL_VECTOR_ABSENT
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return DETAIL_VECTOR_NOT_JSON
    if isinstance(raw, str):
        if not raw.strip():
            return DETAIL_VECTOR_ABSENT
        try:
            decoded = json.loads(raw)
        except ValueError:
            return DETAIL_VECTOR_NOT_JSON
    else:
        decoded = raw
    if not isinstance(decoded, list):
        return DETAIL_VECTOR_NOT_A_LIST
    if len(decoded) != dimension:
        return DETAIL_VECTOR_DIMENSION
    for value in decoded:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return DETAIL_VECTOR_NOT_NUMERIC
        if not math.isfinite(float(value)):
            return DETAIL_VECTOR_NOT_FINITE
    return ""


# ---------------------------------------------------------------------------
# L1 documents
# ---------------------------------------------------------------------------

#: Why the two readers disagree about one document. Shape labels: a reason says
#: *how* they disagree, never which heading or which body disagreed (§15).
READER_BODY_DIFFERS = "canonical_body_differs"
READER_SECTION_ABSENT = "canonical_section_absent"
READER_SECTION_UNPROJECTED = "canonical_section_unprojected"


def _canonical_surfaces(raw: str) -> dict[tuple[str, int], str]:
    """``(label, occurrence) -> body`` as the readable-surface reader sees them.

    Read through :mod:`~layered_memory_mcp.graph_projection`'s own document
    parser — the one :func:`~layered_memory_mcp.graph_projection.read_section_body`
    and therefore every surface drift check is taken from. Restating its heading
    rules here would make this module a *third* reader of an H2 document, which
    is precisely the class of bug the preflight below exists to catch.
    """
    from .graph_projection import _block_body, _parse_document

    _preamble, blocks = _parse_document(raw)
    seen: dict[str, int] = {}
    surfaces: dict[tuple[str, int], str] = {}
    for label, block in blocks:
        occurrence = seen.get(label, 0)
        seen[label] = occurrence + 1
        surfaces[(label, occurrence)] = _block_body(block)
    return surfaces


def section_reader_findings(
    domain: str, raw: str, projected: Sequence[Any] | None = None
) -> list[str]:
    """Refuse one ordinary Markdown document the two readers read differently.

    A migration binds a vector row to a readable surface, and the two ends of
    that binding are produced by *different* readers: the row and its identity
    come from :func:`~layered_memory_mcp.injector._vector_entries`, while every
    later read of the published surface — the drift fingerprint, the republish
    refusal, the verification — goes through
    :func:`~layered_memory_mcp.graph_projection.read_section_body`. Where the
    two disagree about which H2 a body belongs to, a published binding would
    name a section whose body nobody else can find, so the document is refused
    here rather than migrated into a store that cannot read it back.

    Two real disagreements this catches, both exact:

    * a duplicate H2 label whose first occurrence has no body — the projection
      drops the empty one and renumbers the occurrences, so its body lands on
      the *canonical* reader's occurrence 1;
    * a ``##\\t``-separated heading — the projection keeps it inside the
      previous section's body, the canonical reader splits on it.

    A REST-owned frontmatter document is **not** compared: it is not an
    H2-section surface, it keeps its own authoritative ``id``, and neither
    reader claims to own it. Measuring it against the H2 reader would
    manufacture a disagreement — and resolving that disagreement by binding it
    to an H2 is the fabrication this framework refuses (see
    :meth:`SourceSnapshot.external_entry_ids`).

    Args:
        domain: The L1 file stem, reported verbatim in the finding token.
        raw: The document bytes, decoded. Never reported.
        projected: The vector reader's result when a caller already has it;
            otherwise the real producer is run here.

    Returns:
        Sorted ``l1_section_reader_incompatible:<domain>:<reason>`` tokens — one
        per *kind* of disagreement, never a heading, a body or an occurrence —
        or ``[]`` when the two readers agree about every surface.
    """
    from .graph_projection import has_frontmatter_entry

    if has_frontmatter_entry(raw):
        return []
    if projected is None:
        from .injector import _vector_entries

        projected = list(_vector_entries(domain, raw))

    canonical = _canonical_surfaces(raw)
    produced: set[tuple[str, int]] = set()
    reasons: set[str] = set()

    seen: dict[str, int] = {}
    for entry in projected:
        section = entry.section
        occurrence = seen.get(section, 0)
        seen[section] = occurrence + 1
        key = (section, occurrence)
        produced.add(key)
        if key not in canonical:
            reasons.add(READER_SECTION_ABSENT)
        elif canonical[key] != entry.content:
            reasons.add(READER_BODY_DIFFERS)

    # The other direction: a section the canonical reader can read a body out
    # of and the projection never produced is authored material the migration
    # would silently leave behind. An *empty* canonical section is not a
    # disagreement — neither reader has an entry for it.
    for key, body in canonical.items():
        if body and key not in produced:
            reasons.add(READER_SECTION_UNPROJECTED)

    return [
        f"{FINDING_SECTION_READER_INCOMPATIBLE}:{domain}:{reason}"
        for reason in sorted(reasons)
    ]


def _read_l1_entries(
    domain: str, raw: str, findings: set[str]
) -> list[L1Entry]:
    """Project one document through the framework's own producer.

    :func:`~layered_memory_mcp.injector._vector_entries` is *the* projection:
    it preserves a frontmatter ``id`` verbatim and derives an ordinary section's
    ID from the versioned identity rule. Restating either here would be a second
    producer, and two producers for one identity is how a migration ends up
    pointing a row at a node nobody else believes in.

    The derived ID is re-checked against
    :func:`~layered_memory_mcp.graph_entry_identity.markdown_section_entry_id`
    for the occurrence this module assigns, so the ``(domain, section,
    occurrence)`` triple a readable binding is built from is *verified* to be
    the one that produced the row ID rather than assumed to be.

    An ordinary document additionally goes through
    :func:`section_reader_findings`: the triple being internally consistent is
    not enough if the *canonical* readable-surface reader resolves it to another
    body. The entries are still reported — the inventory says what the vector
    reader produced — and the finding is what makes the inventory unpublishable.
    """
    from .graph_projection import has_frontmatter_entry
    from .injector import _vector_entries

    try:
        projected = list(_vector_entries(domain, raw))
    except ProjectionIdentityError as exc:
        findings.add(f"{FINDING_L1_IDENTITY_REFUSED}:{domain}:{exc}")
        return []
    except Exception as exc:  # noqa: BLE001 — named by class, never quoted (§15)
        findings.add(f"{FINDING_L1_UNREADABLE}:{domain}:{type(exc).__name__}")
        return []

    if has_frontmatter_entry(raw):
        return [
            L1Entry(
                entry=entry,
                domain=domain,
                section=entry.section,
                occurrence=None,
                frontmatter_owned=True,
            )
            for entry in projected
        ]

    findings.update(section_reader_findings(domain, raw, projected))

    seen: dict[str, int] = {}
    records: list[L1Entry] = []
    for entry in projected:
        occurrence = seen.get(entry.section, 0)
        seen[entry.section] = occurrence + 1
        derived = markdown_section_entry_id(domain, entry.section, occurrence)
        if derived != entry.id:
            findings.add(
                f"{FINDING_L1_IDENTITY_REFUSED}:{domain}:surface_identity_disagreement"
            )
            continue
        records.append(
            L1Entry(
                entry=entry,
                domain=domain,
                section=entry.section,
                occurrence=occurrence,
                frontmatter_owned=False,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Historical evidence
# ---------------------------------------------------------------------------

def _read_changelog(
    path: Path, findings: set[str]
) -> list[MemoryChangelogEntry]:
    """The optional historical evidence, through the store's own reader.

    :meth:`~layered_memory_mcp.storage.memory_changelog_store.MemoryChangelogStore.load`
    is read-only (only ``append`` ever creates anything) but answers ``([], 0)``
    for a file above its read ceiling, which is indistinguishable from an empty
    log. :meth:`read_status` is therefore consulted *first* and an unparsed log
    is a finding — an oversized changelog must not migrate as "no history".
    """
    from .storage.memory_changelog_store import MemoryChangelogStore

    store = MemoryChangelogStore(path)
    status = store.read_status()
    if status.get("read_truncated"):
        size = status.get("file_size_bytes", 0)
        findings.add(f"{FINDING_CHANGELOG_TRUNCATED}:{size}")
        return []
    payloads, malformed = store.load()
    if malformed:
        findings.add(f"{FINDING_CHANGELOG_MALFORMED}:{malformed}")
    events: list[MemoryChangelogEntry] = []
    for index, payload in enumerate(payloads):
        try:
            events.append(MemoryChangelogEntry.model_validate(payload))
        except Exception as exc:  # noqa: BLE001 — named by class (§15)
            findings.add(f"{FINDING_CHANGELOG_UNREADABLE}:{index}:{type(exc).__name__}")
    return events


# ---------------------------------------------------------------------------
# Explicit identity mapping
# ---------------------------------------------------------------------------

def _clean_identity(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    if "\n" in value or "\r" in value:
        return None
    text = value.strip()
    return text or None


def resolve_identity_map(
    raw: Mapping[str, Any],
    classifications: Sequence[RowClassification],
    known_entry_ids: Sequence[str],
    findings: set[str],
    *,
    rows: Sequence[tuple] | None = None,
) -> dict[str, str]:
    """Validate an operator-supplied ``row ID -> L1 entry ID`` mapping.

    This is a declaration rather than an inference: the operator states which
    authored object an old random row stands for, and every statement is checked
    against the inventory before it counts.

    Refusals are bounded tokens:

    * a key or value that is not a clean single-line identifier;
    * a row the snapshot does not contain;
    * a row that is not actually orphaned — a mapping may not re-point a row
      that already resolves, conflicts or is malformed;
    * an entry ID the L1 snapshot does not hold;
    * an entry ID already claimed, by its own row or by another mapping — one
      claim stands for one authored object, and two rows claiming one node is a
      conflict, not a convenience;
    * malformed or conflicting stored graph identities, when rows are supplied.
    """
    by_row = {row.row_id: row for row in classifications if row.row_id}
    entries = set(known_entry_ids)
    claimed = {row.row_id for row in classifications if row.row_id in entries}
    resolved: dict[str, str] = {}

    for key in sorted(raw, key=str):
        row_id = _clean_identity(key)
        entry_id = _clean_identity(raw[key])
        if row_id is None or entry_id is None:
            findings.add(f"{FINDING_IDENTITY_MAP_INVALID}:entry_{len(resolved)}")
            continue
        if row_id not in by_row:
            findings.add(f"{FINDING_IDENTITY_MAP_ROW_UNKNOWN}:{row_id}")
            continue
        if by_row[row_id].category != CATEGORY_ORPHANED:
            findings.add(f"{FINDING_IDENTITY_MAP_ROW_NOT_ORPHANED}:{row_id}")
            continue
        if entry_id not in entries:
            findings.add(f"{FINDING_IDENTITY_MAP_ENTRY_UNKNOWN}:{row_id}")
            continue
        verdict = by_row[row_id]
        verdict.mapping_origin = "explicit"
        verdict.target_entry_id = entry_id
        verdict.expected_graph_node_id = graph_node_id_for_entry_id(entry_id)
        verdict.candidate_count = known_entry_ids.count(entry_id)
        if verdict.candidate_count != 1:
            verdict.category = CATEGORY_CONFLICTING
            verdict.reason = "identity_entry_ambiguous"
            findings.add(f"{FINDING_IDENTITY_MAP_INVALID}:ambiguous:{row_id}")
            continue
        if rows is not None:
            _, reason, details = _identity_metadata(
                rows[verdict.row_index][4], verdict.expected_graph_node_id
            )
            if reason:
                verdict.category = _CATEGORY_BY_REASON[reason]
                verdict.reason = reason
                verdict.details = details
                findings.add(f"{FINDING_IDENTITY_MAP_INVALID}:metadata:{row_id}")
                continue
        if entry_id in claimed:
            verdict.category = CATEGORY_CONFLICTING
            verdict.reason = "identity_target_reserved"
            findings.add(f"{FINDING_IDENTITY_MAP_ENTRY_TAKEN}:{row_id}")
            continue
        claimed.add(entry_id)
        resolved[row_id] = entry_id
    return resolved


def _load_identity_map(path: Path, findings: set[str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        findings.add(f"{FINDING_IDENTITY_MAP_UNREADABLE}:{type(exc).__name__}")
        return {}
    if not isinstance(payload, dict):
        findings.add(f"{FINDING_IDENTITY_MAP_INVALID}:not_a_mapping")
        return {}
    return payload


# ---------------------------------------------------------------------------
# Row classification
# ---------------------------------------------------------------------------

def _decode_row_metadata(raw: Any) -> dict[str, Any] | None:
    """One row's stored metadata as a mapping, or ``None`` when unreadable.

    Only ever used to build the *complete replacement* metadata for a row an
    explicit mapping resolved; every other row's metadata is decoded by the
    backfill planner itself. The rules are the planner's rules: an absent or
    blank column is an empty mapping, anything that is not a JSON object is
    refused rather than coerced.
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw) if all(isinstance(key, str) for key in raw) else None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            decoded = json.loads(raw)
        except ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _identity_metadata(
    raw: Any, expected: str = ""
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """Apply the backfill graph-field rules before any legacy adaptation."""
    metadata, issues = _decode_metadata(raw)
    if metadata is None:
        return None, REASON_MALFORMED_METADATA, issues
    issues, scalar, listed = _stored_graph_identity(metadata)
    if issues:
        return metadata, REASON_MALFORMED_METADATA, issues
    identities = set(listed)
    if scalar is not None:
        identities.add(scalar)
    if (expected and identities - {expected}) or len(identities) > 1:
        return metadata, REASON_MISMATCHED_GRAPH_METADATA, ["graph_identity_conflict"]
    return metadata, "", []


def _classify_rows(
    rows: Sequence[tuple],
    entries: Sequence[KnowledgeEntry],
    changelog_events: Sequence[MemoryChangelogEntry],
    dimension: int,
    findings: set[str],
) -> tuple[list[RowClassification], BackfillPlan]:
    """Classify every row exactly once, reusing the framework's own planner."""
    plan = plan_graph_seed_backfill(rows, entries, changelog_events)
    for error in plan.report.errors:
        findings.add(f"{FINDING_PLANNER_INPUT}:{error}")

    classifications: list[RowClassification] = []
    for verdict in plan.report.verdicts:
        category = _CATEGORY_BY_REASON.get(verdict.reason, CATEGORY_MALFORMED)
        reason = verdict.reason
        details = list(verdict.details)
        if reason == REASON_ORPHAN_VECTOR_ROW:
            raw_row = rows[verdict.row_index]
            _, metadata_reason, metadata_details = _identity_metadata(raw_row[4])
            if metadata_reason:
                reason = metadata_reason
                category = _CATEGORY_BY_REASON[reason]
                details.extend(metadata_details)
            if not isinstance(raw_row[1], str) or not isinstance(raw_row[2], str):
                category = CATEGORY_MALFORMED
                reason = REASON_UNSUPPORTED_ROW
                details.append("identity_payload_not_text")
        raw_vector = (
            rows[verdict.row_index][3]
            if verdict.row_index < len(rows)
            and isinstance(rows[verdict.row_index], (tuple, list))
            and len(rows[verdict.row_index]) >= 4
            else None
        )
        problem = _vector_payload_problem(raw_vector, dimension)
        if problem and _precedes(CATEGORY_MISSING, category):
            category = CATEGORY_MISSING
            reason = CATEGORY_MISSING
        if problem:
            details.append(problem)
        classifications.append(
            RowClassification(
                row_index=verdict.row_index,
                row_id=verdict.row_id,
                category=category,
                reason=reason,
                expected_graph_node_id=verdict.expected_graph_node_id,
                mapping_origin="row_id" if verdict.expected_graph_node_id else "none",
                target_entry_id=verdict.row_id if verdict.expected_graph_node_id else "",
                candidate_count=1 if verdict.expected_graph_node_id else 0,
                details=sorted(set(details)),
            )
        )
    return classifications, plan


def _precedes(candidate: str, current: str) -> bool:
    """Whether ``candidate`` outranks ``current`` in :data:`CATEGORY_PRECEDENCE`."""
    return CATEGORY_PRECEDENCE.index(candidate) < CATEGORY_PRECEDENCE.index(current)


def _automatic_identities(
    rows: Sequence[tuple],
    entries: Sequence[L1Entry],
    classifications: list[RowClassification],
    updates: dict[str, tuple[str, dict[str, Any]]],
) -> dict[str, str]:
    """Propose exact correspondence without changing the row-ID producer."""
    sections: dict[tuple[str, str], list[L1Entry]] = {}
    for item in entries:
        sections.setdefault((item.domain, item.section), []).append(item)
    reserved = {
        verdict.expected_graph_node_id
        for verdict in classifications
        if verdict.expected_graph_node_id and (
            verdict.mapping_origin == "row_id"
            or verdict.category in PUBLISHABLE_CATEGORIES
        )
    }
    proposals: dict[str, list[tuple[int, dict[str, Any], str]]] = {}
    resolved: dict[str, str] = {}
    for position, verdict in enumerate(classifications):
        if verdict.category != CATEGORY_ORPHANED:
            continue
        row = rows[verdict.row_index]
        metadata = _decode_row_metadata(row[4])
        section = None if metadata is None else metadata.get("section")
        candidates = (
            sections.get((row[1], section), [])
            if isinstance(row[1], str) and isinstance(section, str)
            else []
        )
        verdict.mapping_origin = "automatic"
        verdict.candidate_count = len(candidates)
        if not isinstance(section, str):
            verdict.reason = "identity_section_missing"
            continue
        if not candidates:
            verdict.reason = "identity_section_not_found"
            continue
        if len(candidates) != 1:
            verdict.reason = "identity_section_ambiguous"
            continue
        candidate = candidates[0]
        if not isinstance(row[2], str):
            verdict.reason = "identity_text_mismatch"
            continue
        text = row[2].encode("utf-8")
        if text == candidate.entry.content.encode("utf-8"):
            mapping_reason = REASON_MAPPED_BY_EXACT_IDENTITY
        elif text == (
            f"{embedding_heading(candidate.section)}\n{candidate.entry.content}"
        ).encode("utf-8"):
            mapping_reason = REASON_MAPPED_BY_EXACT_EMBEDDING_PROJECTION
        else:
            verdict.reason = "identity_text_mismatch"
            continue
        target = candidate.entry_id
        checked = plan_graph_seed_backfill(
            [(target, *row[1:])], [candidate.entry], []
        ).report.verdicts[0]
        verdict.expected_graph_node_id = checked.expected_graph_node_id
        verdict.target_entry_id = target if checked.expected_graph_node_id else ""
        if checked.reason not in (REASON_ALREADY_MAPPED, REASON_MISSING_GRAPH_METADATA):
            verdict.category = _CATEGORY_BY_REASON[checked.reason]
            verdict.reason = checked.reason
            verdict.details = list(checked.details)
            continue
        proposals.setdefault(target, []).append((position, metadata, mapping_reason))
    for target, claims in sorted(proposals.items()):
        node_id = graph_node_id_for_entry_id(target)
        for position, metadata, mapping_reason in claims:
            verdict = classifications[position]
            if node_id in reserved or len(claims) != 1:
                verdict.category = CATEGORY_CONFLICTING
                verdict.reason = (
                    "identity_target_reserved" if node_id in reserved
                    else "identity_target_contested"
                )
                continue
            resolved[verdict.row_id] = target
            updates[verdict.row_id] = (node_id, {**metadata, GRAPH_SEED_ID_KEY: node_id})
            classifications[position] = verdict.model_copy(update={
                "category": CATEGORY_MAPPED,
                "reason": mapping_reason,
            })
    return resolved


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

def read_source_snapshot(
    source: Path,
    *,
    destination_prestate: str = "",
    identity_map_path: Path | None = None,
    identity_map_declarations: Mapping[str, Any] | None = None,
) -> SourceSnapshot:
    """Read one explicitly supplied source snapshot. Writes nothing, anywhere.

    Args:
        source: The operator-named snapshot root. Never derived, never guessed.
        destination_prestate: Echoed into the inventory so a report says what
            the destination looked like when the source was read.
        identity_map_path: An optional operator-supplied ``row ID -> L1 entry
            ID`` mapping (see :func:`resolve_identity_map`).
        identity_map_declarations: Recorded explicit declarations for replay
            against a frozen backup, mutually exclusive with a map path.

    Returns:
        A :class:`SourceSnapshot`. Every problem is a bounded finding on
        :attr:`SourceSnapshot.inventory`; nothing raises for a condition an
        operator can act on.
    """
    from .storage.vector_store import EMBED_DIM

    findings: set[str] = set()
    warnings: set[str] = set()
    root = Path(source).expanduser()

    if not root.exists():
        return SourceSnapshot(
            source=root,
            inventory=SourceInventory(
                source=str(root),
                destination_prestate=destination_prestate,
                findings=[FINDING_SOURCE_MISSING],
            ),
        )
    if not root.is_dir() or root.is_symlink():
        return SourceSnapshot(
            source=root,
            inventory=SourceInventory(
                source=str(root),
                destination_prestate=destination_prestate,
                findings=[FINDING_SOURCE_NOT_A_DIRECTORY],
            ),
        )

    resolved_root = root.resolve()
    files: list[SourceFile] = []
    members: list[str] = []
    documents: dict[str, str] = {}

    for kind, relative, path in _walk(resolved_root):
        if kind == "symlink":
            findings.add(f"{FINDING_SOURCE_SYMLINK}:{relative}")
            members.append(f"symlink\x1f{relative}\x1f{os.readlink(path)}")
            continue
        if kind == "unreadable":
            findings.add(f"{FINDING_SOURCE_UNREADABLE}:{relative}")
            continue
        if kind == "special":
            findings.add(f"{FINDING_SOURCE_SPECIAL_FILE}:{relative}")
            members.append(f"special\x1f{relative}")
            continue
        try:
            if resolved_root not in path.resolve().parents:
                findings.add(f"{FINDING_SOURCE_PATH_ESCAPE}:{relative}")
                continue
        except OSError:
            findings.add(f"{FINDING_SOURCE_UNREADABLE}:{relative}")
            continue
        if kind == "dir":
            members.append(f"dir\x1f{relative}")
            if relative not in _ALLOWED_DIRS:
                findings.add(f"{FINDING_SOURCE_UNSUPPORTED_RECORD}:{relative}")
            continue

        file_kind = _file_kind(relative)
        try:
            size = path.stat().st_size
            digest = file_sha256(path)
        except OSError:
            findings.add(f"{FINDING_SOURCE_UNREADABLE}:{relative}")
            continue
        files.append(
            SourceFile(path=relative, kind=file_kind, size=size, sha256=digest)
        )
        members.append(f"file\x1f{relative}\x1f{size}\x1f{digest}")
        if file_kind == FILE_KIND_UNSUPPORTED:
            findings.add(f"{FINDING_SOURCE_UNSUPPORTED_RECORD}:{relative}")
        elif file_kind == FILE_KIND_VECTOR_SIDECAR:
            if size:
                findings.add(f"{FINDING_VECTOR_WAL_NOT_EMPTY}:{relative}:{size}")
            else:
                warnings.add(f"vector_sidecar_present:{relative}")
        elif file_kind == FILE_KIND_L1_MARKDOWN:
            try:
                documents[Path(relative).stem] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                findings.add(
                    f"{FINDING_L1_UNREADABLE}:{Path(relative).stem}:"
                    f"{type(exc).__name__}"
                )

    fingerprint = hashlib.sha256(
        "\x1e".join(sorted(members)).encode("utf-8")
    ).hexdigest()

    # -- L1 entries, through the real producer ------------------------------
    entries: list[L1Entry] = []
    for domain in sorted(documents):
        entries.extend(_read_l1_entries(domain, documents[domain], findings))
    if not documents:
        findings.add(FINDING_NO_L1_DOCUMENTS)

    # -- historical evidence ------------------------------------------------
    changelog_path = resolved_root / SOURCE_CHANGELOG
    changelog_events = (
        _read_changelog(changelog_path, findings)
        if changelog_path.is_file()
        else []
    )
    if not changelog_path.is_file():
        warnings.add("changelog_absent")

    # -- vector rows --------------------------------------------------------
    vector_path = resolved_root / SOURCE_VECTOR_DB
    rows: list[tuple] = []
    classifications: list[RowClassification] = []
    plan: BackfillPlan | None = None
    if not vector_path.is_file():
        findings.add(FINDING_VECTOR_DB_MISSING)
    elif not any(
        finding.startswith(FINDING_VECTOR_WAL_NOT_EMPTY) for finding in findings
    ):
        try:
            columns = _vector_table_columns(vector_path)
            missing = [name for name in _VECTOR_COLUMNS if name not in columns]
            if missing:
                findings.add(
                    f"{FINDING_VECTOR_SCHEMA_UNKNOWN}:{','.join(sorted(missing))}"
                )
            else:
                rows = read_vector_rows(vector_path)
        except (OSError, sqlite3.Error) as exc:
            findings.add(f"{FINDING_VECTOR_DB_UNREADABLE}:{type(exc).__name__}")

    if rows:
        classifications, plan = _classify_rows(
            rows,
            [item.entry for item in entries],
            changelog_events,
            EMBED_DIM,
            findings,
        )

    # -- the explicit identity mapping -------------------------------------
    identity_map: dict[str, str] = {}
    identity_updates: dict[str, tuple[str, dict[str, Any]]] = {}
    if identity_map_path is not None or identity_map_declarations is not None:
        if identity_map_path is not None and identity_map_declarations is not None:
            findings.add(f"{FINDING_IDENTITY_MAP_INVALID}:multiple_declarations")
        raw_map = (
            _load_identity_map(Path(identity_map_path), findings)
            if identity_map_path is not None else identity_map_declarations
        )
        if not isinstance(raw_map, Mapping):
            findings.add(f"{FINDING_IDENTITY_MAP_INVALID}:not_a_mapping")
            raw_map = {}
        identity_map = resolve_identity_map(
            raw_map,
            classifications,
            [item.entry_id for item in entries],
            findings,
            rows=rows,
        )
        # Positions, not values: the verdict being replaced is located by where
        # it sits in the list, so reclassification cannot land on a different
        # row that happens to compare equal.
        positions = {
            row.row_id: index
            for index, row in enumerate(classifications)
            if row.row_id
        }
        for row_id, entry_id in sorted(identity_map.items()):
            node_id = graph_node_id_for_entry_id(entry_id)
            raw_metadata = next(
                (row[4] for row in rows if row[0] == row_id), None
            )
            metadata = _decode_row_metadata(raw_metadata)
            if metadata is None:
                findings.add(f"{FINDING_IDENTITY_MAP_INVALID}:metadata:{row_id}")
                continue
            identity_updates[row_id] = (
                node_id,
                {**metadata, GRAPH_SEED_ID_KEY: node_id},
            )
            index = positions[row_id]
            verdict = classifications[index]
            classifications[index] = RowClassification(
                row_index=verdict.row_index,
                row_id=verdict.row_id,
                category=CATEGORY_MAPPED,
                reason=REASON_MAPPED_BY_IDENTITY_MAP,
                expected_graph_node_id=node_id,
                mapping_origin="explicit",
                target_entry_id=entry_id,
                candidate_count=1,
            )

    automatic_map = _automatic_identities(
        rows, entries, classifications, identity_updates
    )
    counts = {name: 0 for name in ROW_CATEGORIES}
    for verdict in classifications:
        counts[verdict.category] += 1

    for name in ROW_CATEGORIES:
        if name not in PUBLISHABLE_CATEGORIES and counts.get(name):
            findings.add(f"vector_rows_{name}:{counts[name]}")

    inventory = SourceInventory(
        source=str(resolved_root),
        destination_prestate=destination_prestate,
        source_fingerprint=fingerprint,
        file_count=len(files),
        total_bytes=sum(item.size for item in files),
        files=sorted(files, key=lambda item: item.path),
        domains=sorted(documents),
        l1_documents=len(documents),
        l1_entries=len(entries),
        frontmatter_entries=sum(1 for item in entries if item.frontmatter_owned),
        section_entries=sum(1 for item in entries if not item.frontmatter_owned),
        vector_rows=len(rows),
        changelog_events=len(changelog_events),
        counts_by_category=counts,
        classifications=classifications,
        identity_map_entries=len(identity_map),
        automatic_identity_map_entries=len(automatic_map),
        findings=sorted(findings),
        warnings=sorted(warnings),
    )
    if not inventory.counts_balance:
        unbalanced = sorted({*inventory.findings, FINDING_INVENTORY_UNBALANCED})
        inventory = inventory.model_copy(update={"findings": unbalanced})

    return SourceSnapshot(
        source=resolved_root,
        inventory=inventory,
        documents=documents,
        entries=entries,
        vector_rows=rows,
        changelog_events=changelog_events,
        identity_map=identity_map,
        automatic_identity_map=automatic_map,
        identity_map_updates=identity_updates,
        backfill_plan=plan,
    )

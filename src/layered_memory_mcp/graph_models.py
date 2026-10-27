"""Memory graph domain model — typed nodes, typed edges, provenance, validity.

WHAT THIS MODULE IS
-------------------
The backend-neutral vocabulary of the layered memory graph described in
``ARCHITECTURE_v2.md`` §6. It answers "what kinds of things do we remember, how
do they relate, since when is that true, and what backs it up?" — and nothing
else. There is no storage, no query planner, no vendor type and no import of a
graph engine anywhere in this file, because the semantic contract has to survive
an embedded-to-server migration, a schema evolution and a full rebuild (§9.3).

WHAT IT IS NOT
--------------
- NOT a replacement for :mod:`~layered_memory_mcp.models`. ``KnowledgeEntry``
  remains the L1 document model and the MCP-facing shape; this module is the
  semantic layer those documents will eventually be *projected from*.
- NOT a store. The store contract lives in
  :mod:`~layered_memory_mcp.graph_store` and the deterministic in-memory
  implementation of it in :mod:`~layered_memory_mcp.graph_fixture_store`.
- NOT a second editable ledger. A node is projected from append-only memory
  events over source evidence (§4); it is not an independent fact source.

THE INVARIANTS THIS FILE ENFORCES
---------------------------------
1. **Identity is explicit.** Node and edge IDs are required, non-empty and
   single-line. A rebuild must produce the *same* ID for the same source
   identity (§10.2), so there is no random default: use :func:`make_node_id` /
   :func:`make_edge_id`, which hash the source identity.
2. **Remembered claims carry provenance.** A node whose kind is in
   :data:`PROVENANCE_REQUIRED_NODE_KINDS` must have at least one
   :class:`SourceRef` or an explicit :class:`Derivation` record, and so must a
   strong assertion edge (:data:`PROVENANCE_REQUIRED_RELATIONS`). An ``Entity``
   is an identity node, not a remembered statement, so it stays cheap to create
   — otherwise legitimate identity nodes could not exist at all.
3. **Time is explicit and timezone-aware.** ``valid_to`` may not precede
   ``valid_from``; every timestamp that arrives naive is pinned to UTC rather
   than left ambiguous. Absence stays absence — nothing invents a timestamp, so
   a migrated entry with no original timestamp keeps ``valid_from = None``
   (§6.3) and records ``derivation.method = migrated``.
4. **Serialization is deterministic and leaks nothing.** :meth:`GraphModel.to_dict`
   returns plain JSON types — never an ``Enum`` or a ``datetime`` object — and
   :meth:`GraphModel.to_json` sorts keys, so two equal models serialize
   byte-identically. Rebuild/drift fixtures depend on that.
5. **Unknown fields are refused.** ``extra="forbid"`` keeps a typo from becoming
   a silent, unqueryable attribute. Kind-specific extras belong in
   ``attributes``.

PRIVACY
-------
``GraphNode.content`` on an ``Evidence`` node may hold a source excerpt, and
``SourceRef`` may name a file and a section. Both are subject to the existing
source privacy policy (§15): prefer ``quote_hash`` over a quote where a hash is
enough, and never log either.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationInfo,
    field_validator,
    model_validator,
)

#: Bumped whenever a persisted node/edge payload changes shape. Version 1 is the
#: first semantic foundation (Batch B); nothing has been persisted under it yet.
GRAPH_SCHEMA_VERSION = 1

#: Separator for hashed identity parts. Not a legal character in an identifier,
#: so ``make_node_id("a\x1fb")`` cannot collide with ``make_node_id("a", "b")``.
_IDENTITY_SEPARATOR = "\x1f"

#: Length of the hex digest kept in a generated ID. 16 hex chars = 64 bits, which
#: is ample for one memory store and keeps IDs readable in a fixture diff.
_ID_DIGEST_LENGTH = 16


# ---------------------------------------------------------------------------
# Enums — the architecture's vocabulary, spelled as §6 spells it
# ---------------------------------------------------------------------------

class NodeKind(str, Enum):
    """Typed memory objects (§6.1). A new kind needs a schema decision."""

    EPISODE = "episode"         # One ingestion event, session, import, capture
    EVIDENCE = "evidence"       # A located source excerpt or source pointer
    ENTITY = "entity"           # Person, project, component, concept, object
    CLAIM = "claim"             # An atomic statement about one or more entities
    EVENT = "event"             # Something that happened at a point/interval
    EXPERIENCE = "experience"   # A reusable lesson derived from events
    PROCEDURE = "procedure"     # A repeatable method, projected to a Skill
    POLICY = "policy"           # A preference, invariant, or operating rule
    TASK = "task"               # A TODO item with lifecycle and dependencies
    COMMUNITY = "community"     # A derived cluster/summary, never an authority


class RelationType(str, Enum):
    """Typed edges (§6.2). Uppercase, as the architecture and queries use them."""

    EVIDENCED_BY = "EVIDENCED_BY"   # Claim/Experience/Policy -> Evidence
    DERIVED_FROM = "DERIVED_FROM"   # Claim/Experience -> Episode/Claim/Event
    ABOUT = "ABOUT"                 # Claim/Event/Experience -> Entity
    MENTIONS = "MENTIONS"           # Episode/Evidence -> Entity
    SUPPORTS = "SUPPORTS"           # Claim -> Claim
    CONTRADICTS = "CONTRADICTS"     # Claim -> Claim, cannot both be settled
    SUPERSEDES = "SUPERSEDES"       # New Claim/Policy/Procedure -> Old node
    APPLIES_TO = "APPLIES_TO"       # Policy/Procedure -> Entity/Scope
    IMPLEMENTS = "IMPLEMENTS"       # Procedure -> Policy
    OBSERVED_IN = "OBSERVED_IN"     # Event/Claim -> Episode
    BLOCKS = "BLOCKS"               # Task -> Task/Entity
    RELATED_TO = "RELATED_TO"       # Weak explicit relation only
    PART_OF = "PART_OF"             # Hierarchical containment


class NodeStatus(str, Enum):
    """Lifecycle state (§8.2). Historical state is kept, not deleted."""

    ACTIVE = "active"
    PENDING = "pending"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class EdgeStatus(str, Enum):
    """An edge is asserted, withdrawn, or timed out. It is never superseded —
    a replacement assertion is a new edge, and the old one is retracted."""

    ACTIVE = "active"
    RETRACTED = "retracted"
    EXPIRED = "expired"


class MemoryClass(str, Enum):
    """What sort of memory a semantic node holds (§6.3)."""

    FACT = "fact"
    OBSERVATION = "observation"
    EXPERIENCE = "experience"
    PROCEDURE = "procedure"
    BELIEF = "belief"
    POLICY = "policy"


class DerivationMethod(str, Enum):
    """How a node/edge came to exist (§6.3).

    ``MIGRATED`` and ``INFERRED`` are the honest labels for legacy import: a
    field the original source never had must not look observed.
    """

    USER_EXPLICIT = "user_explicit"
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    SUMMARIZED = "summarized"
    MIGRATED = "migrated"


class MemoryEventKind(str, Enum):
    """Append-only memory events the graph is projected from (§4)."""

    INJECT = "inject"
    REVISE = "revise"
    SUPERSEDE = "supersede"
    RETRACT = "retract"
    APPROVE = "approve"
    RESTORE = "restore"
    MIGRATE = "migrate"


# ---------------------------------------------------------------------------
# Derived vocabulary — the policy constants the validators read
# ---------------------------------------------------------------------------

#: Kinds that may not exist without evidence or an explicit derivation record
#: (§3.1 goal 4). ``EVENT`` is excluded on purpose: an event may be recorded
#: from a tool/session before its evidence excerpt has been located, and
#: ``OBSERVED_IN`` carries its source. ``ENTITY`` is excluded because an
#: identity node is not a remembered statement.
PROVENANCE_REQUIRED_NODE_KINDS: tuple[NodeKind, ...] = (
    NodeKind.CLAIM,
    NodeKind.EXPERIENCE,
    NodeKind.POLICY,
    NodeKind.PROCEDURE,
)

#: Relations that assert something beyond their own endpoints and therefore need
#: their own justification. ``EVIDENCED_BY``/``DERIVED_FROM``/``OBSERVED_IN``
#: are excluded because their *target* is the provenance; requiring an extra
#: evidence ref on them would be circular.
PROVENANCE_REQUIRED_RELATIONS: tuple[RelationType, ...] = (
    RelationType.SUPPORTS,
    RelationType.CONTRADICTS,
    RelationType.SUPERSEDES,
)

#: Relations too weak to justify a high-trust answer on their own (§6.2).
WEAK_RELATIONS: tuple[RelationType, ...] = (RelationType.RELATED_TO,)

#: Statuses eligible for default current-context injection (§8.2).
CURRENT_NODE_STATUSES: tuple[NodeStatus, ...] = (NodeStatus.ACTIVE,)

#: Statuses that stay queryable as history but never inject as current state.
HISTORICAL_NODE_STATUSES: tuple[NodeStatus, ...] = (
    NodeStatus.SUPERSEDED,
    NodeStatus.RETRACTED,
    NodeStatus.EXPIRED,
    NodeStatus.ARCHIVED,
)

#: The :class:`SourceRef` fields that actually say *where* the evidence is. A
#: quote hash proves integrity but locates nothing, so it is not on this list.
SOURCE_LOCATOR_FIELDS: tuple[str, ...] = (
    "episode_id",
    "evidence_id",
    "source_file",
)


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def ensure_aware(value: datetime | None) -> datetime | None:
    """Return ``value`` with a timezone, assuming UTC when none was given.

    A naive timestamp is ambiguous, and the rest of the framework already
    records in UTC (``datetime.now(timezone.utc)``), so UTC is the only
    assumption that does not change the stored instant for framework-produced
    data. An explicit offset is preserved exactly — converting it would make a
    serialized fixture depend on the reader's notion of "the same moment".
    """
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


#: A timestamp that is always timezone-aware in Python and always a full ISO
#: 8601 string with an explicit offset in JSON. ``isoformat()`` (``+00:00``)
#: rather than pydantic's default ``Z`` keeps graph payloads byte-comparable
#: with the rest of the framework, which serializes with ``isoformat()`` too.
Timestamp = Annotated[
    datetime,
    AfterValidator(ensure_aware),
    PlainSerializer(lambda v: v.isoformat(), return_type=str, when_used="json"),
]


def _reject_newlines(value: str, field_name: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must not contain newlines")


def clean_identifier(value: str, field_name: str = "identifier") -> str:
    """Normalize a required identifier: single-line, trimmed, non-empty.

    Newlines are checked *before* trimming, so an embedded newline is rejected
    rather than quietly surviving at the end of a value.
    """
    _reject_newlines(value, field_name)
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    return text


def clean_optional_identifier(
    value: str | None, field_name: str = "identifier"
) -> str | None:
    """Normalize an optional identifier; a blank value means "unset"."""
    if value is None:
        return None
    _reject_newlines(value, field_name)
    return value.strip() or None


def clean_text_line(value: str, field_name: str = "text") -> str:
    """Normalize a single-line human label. May be empty, may not wrap."""
    _reject_newlines(value, field_name)
    return value.strip()


def _check_interval(valid_from: datetime | None, valid_to: datetime | None) -> None:
    if valid_from is not None and valid_to is not None and valid_to < valid_from:
        raise ValueError("valid_to must not precede valid_from")


def _within_interval(
    valid_from: datetime | None, valid_to: datetime | None, at: datetime
) -> bool:
    """Inclusive on both ends: a zero-length interval is valid at its instant."""
    if valid_from is not None and at < valid_from:
        return False
    if valid_to is not None and at > valid_to:
        return False
    return True


def make_node_id(kind: NodeKind | str, *parts: str) -> str:
    """Build a stable node ID from the node kind and its source identity.

    §10.2 requires a rebuild to produce the same ID for the same source
    identity, which rules out a random default. Pass the parts that *identify*
    the source (file and section, episode and offset, entity canonical name) —
    not its content, or every edit would mint a new node.
    """
    kind_value = NodeKind(kind).value
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if not cleaned:
        raise ValueError("make_node_id requires at least one source identity part")
    digest = hashlib.sha256(
        _IDENTITY_SEPARATOR.join(cleaned).encode("utf-8")
    ).hexdigest()
    return f"{kind_value}_{digest[:_ID_DIGEST_LENGTH]}"


def make_edge_id(
    relation_type: RelationType | str,
    source_id: str,
    target_id: str,
    *,
    discriminator: str = "",
) -> str:
    """Build a stable edge ID from the relation triple.

    ``discriminator`` separates two assertions of the same triple that are
    genuinely distinct — typically different validity windows. Omitting it
    makes re-appending the same triple idempotent, which is what an idempotent
    rebuild wants.
    """
    relation = RelationType(relation_type).value
    source = clean_identifier(source_id, "source_id")
    target = clean_identifier(target_id, "target_id")
    digest = hashlib.sha256(
        _IDENTITY_SEPARATOR.join(
            [relation, source, target, discriminator.strip()]
        ).encode("utf-8")
    ).hexdigest()
    return f"edge_{digest[:_ID_DIGEST_LENGTH]}"


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------

class GraphModel(BaseModel):
    """Shared serialization contract for every graph domain object.

    ``to_dict`` is the fixture- and projection-facing shape: plain JSON types
    only, so no consumer ever has to know about ``NodeKind`` or ``datetime``.
    ``to_json`` additionally sorts keys, which makes byte comparison of two
    semantically equal payloads a valid assertion.
    """

    model_config = ConfigDict(extra="forbid")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class GraphScope(GraphModel):
    """Who a memory belongs to (§6.3).

    Every field is optional: an unset field means "not scoped by this
    dimension", not "scoped to empty". Used both as a node's own scope and as a
    traversal filter — see :meth:`matches`.
    """

    user_id: str | None = None
    agent_id: str | None = None
    namespace: str | None = None

    @field_validator("user_id", "agent_id", "namespace")
    @classmethod
    def _clean(cls, v: str | None, info: ValidationInfo) -> str | None:
        return clean_optional_identifier(v, info.field_name or "scope field")

    def matches(self, query: GraphScope) -> bool:
        """Whether this scope satisfies ``query`` read as a filter.

        Each field the filter sets must match exactly; fields it leaves unset
        are not constrained. An unscoped node therefore does *not* satisfy a
        narrowed filter — a graph-wide memory must be asked for explicitly
        rather than leaking into every scope.
        """
        for field in ("user_id", "agent_id", "namespace"):
            wanted = getattr(query, field)
            if wanted is not None and getattr(self, field) != wanted:
                return False
        return True


class SourceRef(GraphModel):
    """A pointer back into immutable source evidence (§6.3).

    At least one *locator* is required — an episode, an evidence node, or a
    source file. A quote hash alone proves integrity but locates nothing, so it
    cannot stand in for provenance.
    """

    episode_id: str | None = None
    evidence_id: str | None = None
    source_file: str | None = None
    source_section: str | None = None
    quote_hash: str | None = None

    @field_validator(
        "episode_id", "evidence_id", "source_file", "source_section", "quote_hash"
    )
    @classmethod
    def _clean(cls, v: str | None, info: ValidationInfo) -> str | None:
        return clean_optional_identifier(v, info.field_name or "source ref field")

    @model_validator(mode="after")
    def _require_locator(self) -> SourceRef:
        if not any(getattr(self, f) for f in SOURCE_LOCATOR_FIELDS):
            raise ValueError(
                "source ref needs at least one of: "
                + ", ".join(SOURCE_LOCATOR_FIELDS)
            )
        return self


class Derivation(GraphModel):
    """How a node or edge came to exist (§6.3).

    This is the alternative to a source ref, not an excuse for missing one: an
    inferred or summarized memory is still traceable, but traceable to a
    *process* rather than to a quote. ``method`` is mandatory precisely so that
    "we do not know where this came from" is not expressible.
    """

    method: DerivationMethod
    model: str | None = None
    prompt_version: str | None = None
    generated_at: Timestamp | None = None

    @field_validator("model", "prompt_version")
    @classmethod
    def _clean(cls, v: str | None, info: ValidationInfo) -> str | None:
        return clean_optional_identifier(v, info.field_name or "derivation field")


# ---------------------------------------------------------------------------
# Nodes and edges
# ---------------------------------------------------------------------------

class GraphNode(GraphModel):
    """One typed memory object in the graph (§6.1, §6.3).

    ``id`` is required and must be stable for a given source identity; use
    :func:`make_node_id` unless the ID already exists upstream (a TODO id, a
    ``KnowledgeEntry`` id). ``status`` defaults to ``active`` because the
    importer and the explicit-write path both produce settled state; an
    extraction pipeline that needs review should pass ``pending`` and let the
    approve transition promote it.
    """

    id: str
    kind: NodeKind

    # Content. ``label`` is the short, single-line handle used in paths, routing
    # projections and fixture diffs; ``content`` is the body and may wrap.
    label: str = ""
    content: str = ""

    # Semantic state
    scope: GraphScope = Field(default_factory=GraphScope)
    status: NodeStatus = NodeStatus.ACTIVE
    memory_class: MemoryClass | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # Temporal validity. ``recorded_at`` is when *we* learned it; ``valid_from``
    # / ``valid_to`` are when the statement itself holds. They are deliberately
    # separate, so a late-recorded fact does not become a recently-true fact.
    valid_from: Timestamp | None = None
    valid_to: Timestamp | None = None
    recorded_at: Timestamp = Field(default_factory=_utc_now)

    # Provenance
    source_refs: list[SourceRef] = Field(default_factory=list)
    derivation: Derivation | None = None

    # Conflict and bookkeeping
    conflict_group: str | None = None
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    schema_version: int = GRAPH_SCHEMA_VERSION

    @field_validator("id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        return clean_identifier(v, "id")

    @field_validator("conflict_group")
    @classmethod
    def _clean_conflict_group(cls, v: str | None) -> str | None:
        return clean_optional_identifier(v, "conflict_group")

    @field_validator("label")
    @classmethod
    def _clean_label(cls, v: str) -> str:
        return clean_text_line(v, "label")

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        return [t.lower().strip() for t in v if t.strip()]

    @model_validator(mode="after")
    def _check_state(self) -> GraphNode:
        _check_interval(self.valid_from, self.valid_to)
        if self.kind in PROVENANCE_REQUIRED_NODE_KINDS and not (
            self.source_refs or self.derivation
        ):
            raise ValueError(
                f"a {self.kind.value} node requires at least one source ref or "
                "an explicit derivation record"
            )
        return self

    def is_valid_at(self, at: datetime | None = None) -> bool:
        """Whether the statement holds at ``at`` (default: now). Ignores status."""
        moment = ensure_aware(at) or _utc_now()
        return _within_interval(self.valid_from, self.valid_to, moment)

    def is_current(self, at: datetime | None = None) -> bool:
        """Whether this may be injected as current state (§8.2).

        Both conditions must hold: the lifecycle status is current *and* the
        validity window covers ``at``. A superseded claim that is still inside
        its stated window is history, not context.
        """
        return self.status in CURRENT_NODE_STATUSES and self.is_valid_at(at)

    def requires_provenance(self) -> bool:
        return self.kind in PROVENANCE_REQUIRED_NODE_KINDS


class GraphEdge(GraphModel):
    """One typed, append-only relation between two nodes (§6.2, §6.3).

    Edges are asserted and withdrawn, never edited in place: a changed relation
    is a new edge plus a retraction of the old one, which is what keeps the
    history queryable.
    """

    id: str
    relation_type: RelationType
    source_id: str
    target_id: str

    status: EdgeStatus = EdgeStatus.ACTIVE
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    valid_from: Timestamp | None = None
    valid_to: Timestamp | None = None
    recorded_at: Timestamp = Field(default_factory=_utc_now)

    evidence_refs: list[SourceRef] = Field(default_factory=list)
    derivation: Derivation | None = None

    attributes: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = GRAPH_SCHEMA_VERSION

    @field_validator("id", "source_id", "target_id")
    @classmethod
    def _clean_ids(cls, v: str, info: ValidationInfo) -> str:
        return clean_identifier(v, info.field_name or "id")

    @model_validator(mode="after")
    def _check_state(self) -> GraphEdge:
        _check_interval(self.valid_from, self.valid_to)
        if self.source_id == self.target_id:
            raise ValueError("an edge must connect two distinct nodes")
        if self.relation_type in PROVENANCE_REQUIRED_RELATIONS and not (
            self.evidence_refs or self.derivation
        ):
            raise ValueError(
                f"a {self.relation_type.value} edge requires at least one "
                "evidence ref or an explicit derivation record"
            )
        return self

    def is_active_at(self, at: datetime | None = None) -> bool:
        """Whether this relation is asserted and in force at ``at``."""
        if self.status is not EdgeStatus.ACTIVE:
            return False
        moment = ensure_aware(at) or _utc_now()
        return _within_interval(self.valid_from, self.valid_to, moment)

    def is_weak(self) -> bool:
        """Whether this relation is too weak to justify a high-trust answer."""
        return self.relation_type in WEAK_RELATIONS

    def requires_provenance(self) -> bool:
        return self.relation_type in PROVENANCE_REQUIRED_RELATIONS


class MemoryEvent(GraphModel):
    """One append-only memory event the graph state is projected from (§4).

    An event must reference something to project — a node, an edge, or the ID of
    an existing node it transitions (``approve``, ``retract``, ``restore``).
    An event that references nothing is not graph source material.
    """

    id: str
    kind: MemoryEventKind
    occurred_at: Timestamp
    scope: GraphScope | None = None
    target_id: str | None = None
    node: GraphNode | None = None
    edge: GraphEdge | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        return clean_identifier(v, "id")

    @field_validator("target_id")
    @classmethod
    def _clean_target(cls, v: str | None) -> str | None:
        return clean_optional_identifier(v, "target_id")

    @model_validator(mode="after")
    def _require_reference(self) -> MemoryEvent:
        if self.node is None and self.edge is None and self.target_id is None:
            raise ValueError(
                "a memory event must carry a node, an edge, or a target_id"
            )
        return self

"""The managed, writable projections of the semantic authority (§12 contract).

WHAT THIS MODULE IS
-------------------
The publishers that turn a replayed :class:`~layered_memory_mcp.graph_models.
MemoryEvent` stream into the four surfaces a reader actually uses:

* the **graph** — ``GraphStore.rebuild`` over the complete journal stream;
* the readable **L1** Markdown section, which is also what BM25/keyword recall
  reads, so publishing L1 *is* publishing BM25;
* the **vector** rows for the domains L1 touched, through the existing
  ``sync_to_vector_store`` optimistic-snapshot path — never ad-hoc SQL;
* the **L0** index, regenerated from the L1 files by the existing
  ``sync_l0_index``.

They are *projections*, not sources: everything they write is derivable from the
journal, which is why a failed publication is recoverable rather than lost.

WHAT IT IS NOT
--------------
- NOT an owner of unrelated content. The L1 publisher touches exactly the
  surfaces a managed node is bound to. Every unmanaged section, preamble and
  file is preserved.
- NOT an adopter. A section that exists on disk and is not in the surface
  registry is refused (``surface_not_adopted``), and a registered section whose
  on-disk body no longer matches what was published is refused
  (``surface_drift``). An external Skill's file is never silently taken over.
- NOT a deleter of history. Retiring a surface removes the *rendered* section
  and marks the registry row retired; the node stays in the graph and every
  event stays in the journal.
- NOT a transaction manager. Four stores and a filesystem do not share a
  transaction, and this module does not pretend they do: each projection is
  published separately and the caller advances its checkpoint only after it
  actually succeeded.
- NOT an extractor. Nothing here reads a body to decide what it means. The only
  thing it reads a body for is the drift fingerprint.

PRIVACY (§15)
-------------
Reports carry entry IDs, node IDs, domains, counts and bounded labels. A domain
is a file stem the operator chose and is already present in every existing
report; a section *body* never leaves this module except by being written to the
file it belongs in.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pydantic import Field, model_validator

from .graph_entry_identity import markdown_preamble_entry_id, markdown_section_entry_id
from .graph_journal import (
    SURFACE_INTENT_PUBLISH,
    SURFACE_INTENT_RETIRE,
    MemoryEventJournal,
    SurfaceIntent,
    SurfaceRecord,
    body_sha256,
)
from .graph_models import GraphModel, GraphNode, MemoryEvent
from .markdown_sections import (
    MarkdownLayoutError,
    block_body,
    canonical_body,
    render_section,
    scan_document,
)

__all__ = [
    "LAYOUT_ERROR_LIMIT",
    "LAYOUT_MISMATCH",
    "LAYOUT_OCCURRENCE_MISMATCH",
    "PROJECTION_METADATA_KEY",
    "PROJECTION_SURFACE_KIND",
    "PROJECTION_PREAMBLE_SURFACE_KIND",
    "PUBLISH_TEMP_SUFFIX",
    "SURFACE_DRIFT",
    "SURFACE_NOT_ADOPTED",
    "SURFACE_UNSUPPORTED",
    "L1ProjectionReport",
    "MarkdownLayoutError",
    "ProjectionBinding",
    "ProjectionError",
    "SurfacePlan",
    "binding_metadata",
    "bindings_from_events",
    "domain_path",
    "layout_errors",
    "parse_binding",
    "plan_layout_errors",
    "publish_graph",
    "publish_l0",
    "publish_l1",
    "publish_todo",
    "publish_vector",
    "read_section_body",
    "read_preamble",
    "render_surface_document",
    "replace_document",
    "surface_plans",
    "surface_refusal",
]

#: The event-metadata key carrying the readable-surface binding. It travels in
#: the journal, so a full replay after a restart can rebuild L1 without
#: consulting any other store.
PROJECTION_METADATA_KEY = "projection"

#: The one readable surface kind this framework supports today: an H2 section in
#: an ordinary-Markdown L1 domain file. Named in the payload so a later surface
#: kind is a new value rather than a silent reinterpretation of this one.
PROJECTION_SURFACE_KIND = "l1_markdown_section"
PROJECTION_PREAMBLE_SURFACE_KIND = "l1_markdown_preamble"

#: Refusal labels. Bounded tokens: ``<reason>:<entry id>``, never a body.
SURFACE_DRIFT = "surface_drift"
SURFACE_NOT_ADOPTED = "surface_not_adopted"
SURFACE_UNSUPPORTED = "surface_unsupported"

#: Suffix of the temporary file an L1 document is written through before being
#: renamed over its target. Deliberately **not** ``.md``: every knowledge
#: scanner globs ``*.md`` (including dotfiles), so a temporary file carrying that
#: suffix would be a readable knowledge document for as long as it existed, and
#: a killed process would leave a scannable fragment behind.
PUBLISH_TEMP_SUFFIX = ".publish.tmp"

#: Bounded label for "this publication could not be read back the way it was
#: written". The round-trip guard's own refusal, carried as
#: ``<label>:<entry id>`` exactly like every other surface refusal.
LAYOUT_MISMATCH = "binding_layout_mismatch"

#: Bounded label for the one reader disagreement this framework refuses rather
#: than resolves: the projection addresses a section by its position among
#: *every* same-label H2 and the vector/keyword readers count only bodyful
#: ones, so a bodyless duplicate heading in front of a managed surface would
#: publish a body under one identity and index it under another.
LAYOUT_OCCURRENCE_MISMATCH = "binding_occurrence_mismatch"

#: How many layout labels one refusal reports. A refusal is a bounded report,
#: not a document listing (§15).
LAYOUT_ERROR_LIMIT = 20


class ProjectionError(RuntimeError):
    """A projection could not publish. Recoverable; never a lost commit."""


# ---------------------------------------------------------------------------
# The binding between a semantic node and a readable surface
# ---------------------------------------------------------------------------

class ProjectionBinding(GraphModel):
    """Which readable L1 section a semantic node publishes to.

    The binding is what stops a graph write and the document it claims to
    describe from becoming two independent facts: the committing layer refuses a
    request whose node body disagrees with ``body``, and the surface identity is
    *derived* here rather than supplied, so a caller cannot point one decision at
    a section it did not name.

    ``occurrence`` is the zero-based position among *identical* H2 labels in the
    domain, exactly as :func:`~layered_memory_mcp.graph_entry_identity.
    markdown_section_entry_ids` assigns it. Today's entrances address a section
    by heading and therefore always bind occurrence 0; the field exists so a
    duplicate-label document is representable rather than silently merged.
    """

    domain: str
    section: str
    occurrence: int = Field(default=0, ge=0)
    body: str
    kind: str = PROJECTION_SURFACE_KIND

    @model_validator(mode="after")
    def _surface_shape(self) -> ProjectionBinding:
        _validate_surface_shape(self.kind, self.section, self.occurrence)
        if (self.kind == PROJECTION_PREAMBLE_SURFACE_KIND
                and _preamble_carries_section(self.normalized_body)):
            raise ValueError(LAYOUT_MISMATCH)
        return self

    @property
    def entry_id(self) -> str:
        """The versioned projection identity — also the vector row's own ``id``."""
        if self.kind == PROJECTION_PREAMBLE_SURFACE_KIND:
            return markdown_preamble_entry_id(self.domain)
        return markdown_section_entry_id(self.domain, self.section, self.occurrence)

    @property
    def normalized_body(self) -> str:
        """The canonical published body: LF line endings, boundary-stripped.

        One rule for both surface kinds. A section body used to skip the line
        ending normalization the file reader always applied, so a CRLF body was
        hashed in one form and read back in another — a surface that drifted
        from itself the moment it was published.
        """
        return canonical_body(self.body)


class SurfacePlan(GraphModel):
    """What one managed surface should look like after this publication."""

    entry_id: str
    domain: str
    section: str
    occurrence: int = Field(default=0, ge=0)
    node_id: str
    body: str = ""
    #: ``publish`` renders the section; ``retire`` removes it. A retired surface
    #: is removed from the *rendering* only — the node and its events remain.
    action: str = "publish"
    kind: str = PROJECTION_SURFACE_KIND

    @model_validator(mode="after")
    def _surface_shape(self) -> SurfacePlan:
        _validate_surface_shape(self.kind, self.section, self.occurrence)
        if (self.kind == PROJECTION_PREAMBLE_SURFACE_KIND
                and _preamble_carries_section(self.normalized_body)):
            raise ValueError(LAYOUT_MISMATCH)
        return self

    @property
    def normalized_body(self) -> str:
        """The canonical published body: LF line endings, boundary-stripped.

        One rule for both surface kinds. A section body used to skip the line
        ending normalization the file reader always applied, so a CRLF body was
        hashed in one form and read back in another — a surface that drifted
        from itself the moment it was published.
        """
        return canonical_body(self.body)


def _validate_surface_shape(kind: str, section: str, occurrence: int) -> None:
    if kind not in (PROJECTION_SURFACE_KIND, PROJECTION_PREAMBLE_SURFACE_KIND):
        raise ValueError(SURFACE_UNSUPPORTED)
    if kind == PROJECTION_PREAMBLE_SURFACE_KIND and (section != "" or occurrence != 0):
        raise ValueError("binding_layout_mismatch")


class L1ProjectionReport(GraphModel):
    """What the readable projection wrote, as bounded data."""

    domains: list[str] = Field(default_factory=list)
    published: list[str] = Field(default_factory=list)
    retired: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Binding metadata, carried in the journal
# ---------------------------------------------------------------------------

def binding_metadata(binding: ProjectionBinding) -> dict[str, Any]:
    """The serializable binding an event carries, so replay can rebuild L1."""
    return {
        "surface": PROJECTION_SURFACE_KIND,
        "kind": binding.kind,
        "domain": binding.domain,
        "section": binding.section,
        "occurrence": binding.occurrence,
        "entry_id": binding.entry_id,
        "body_sha256": body_sha256(binding.normalized_body),
    }


def parse_binding(metadata: Any) -> ProjectionBinding | None:
    """Read a binding back out of event metadata, or answer ``None``.

    Deliberately forgiving in one direction only: an event with no binding, or
    with a surface kind this process does not speak, projects to no readable
    surface. It is never *guessed* — a partial binding is no binding.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(PROJECTION_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    if raw.get("surface") != PROJECTION_SURFACE_KIND:
        return None
    kind = raw.get("kind", PROJECTION_SURFACE_KIND)
    if kind not in (PROJECTION_SURFACE_KIND, PROJECTION_PREAMBLE_SURFACE_KIND):
        return None
    domain = raw.get("domain")
    section = raw.get("section")
    if not isinstance(domain, str) or not isinstance(section, str):
        return None
    occurrence = raw.get("occurrence", 0)
    if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0:
        return None
    if kind == PROJECTION_PREAMBLE_SURFACE_KIND and (section != "" or occurrence != 0):
        return None
    return ProjectionBinding(
        domain=domain, section=section, occurrence=occurrence, body="", kind=kind,
    )


def bindings_from_events(
    events: Sequence[MemoryEvent],
) -> dict[str, ProjectionBinding]:
    """``node_id -> binding`` for every event that declared one.

    Read in stream order, last declaration winning, which is the same rule the
    graph projection applies to the node payload itself.
    """
    bindings: dict[str, ProjectionBinding] = {}
    for event in events:
        if event.node is None:
            continue
        binding = parse_binding(event.metadata)
        if binding is None:
            continue
        bindings[event.node.id] = binding
    return bindings


# ---------------------------------------------------------------------------
# Markdown surface rendering
# ---------------------------------------------------------------------------

def domain_path(config: Any, domain: str) -> Path:
    """The authoritative L1 file for ``domain`` — the first knowledge dir.

    Only the primary (namespace) directory is ever written: a shared directory
    is somebody else's store, and a projection that wrote into it would be
    adopting a surface it does not own.
    """
    directories = list(getattr(config, "knowledge_dirs", ()) or [])
    root = Path(directories[0]) if directories else Path(config.knowledge_dir)
    return root / f"{domain}.md"


def _parse_document(raw: str) -> tuple[str, list[tuple[str, str]]]:
    """``(preamble, [(label, block text)])`` for one Markdown document.

    The block text is kept verbatim, which is what lets an untouched section
    survive a publication byte-for-byte. Boundaries come from the one shared
    scanner (:mod:`~layered_memory_mcp.markdown_sections`), so a fenced ``##``
    line is code and a managed body keeps its authored headings.

    The *structural* reader, so it never raises: a document whose managed
    delimiters are malformed is split the way it was split before the envelope
    existed, which keeps a layout-only caller (the migration's two-reader
    preflight, the renderer's preservation of unmanaged blocks) working on a
    file somebody has edited into an ambiguous state. The readers that *compare
    a body* — :func:`read_section_body`, :func:`read_preamble` and therefore
    every drift check and the round-trip guard — are strict and refuse it.
    """
    document = scan_document(raw, strict=False)
    return document.preamble, [(block.label, block.text) for block in document.blocks]


def _block_body(block: str) -> str:
    return block_body(block)


def _render_block(section: str, body: str) -> str:
    return render_section(section, body)


def _locate(blocks: Sequence[tuple[str, str]], section: str, occurrence: int) -> int | None:
    seen = 0
    for index, (label, _block) in enumerate(blocks):
        if label != section:
            continue
        if seen == occurrence:
            return index
        seen += 1
    return None


def _bodyful_occurrence(
    blocks: Sequence[tuple[str, str]], index: int
) -> int | None:
    """``index``'s position among *bodyful* same-label blocks, or ``None``.

    The vector and keyword readers' occurrence rule, asked of a block the
    projection's own rule already located. ``None`` means this block has no
    body at all, so those readers mint no identity for it.
    """
    label = blocks[index][0]
    seen = 0
    for position, (block_label, text) in enumerate(blocks):
        if block_label != label or not _block_body(text):
            continue
        if position == index:
            return seen
        seen += 1
    return None


def read_section_body(raw: str, section: str, occurrence: int = 0) -> str | None:
    """The current body of one H2 section, or ``None`` when it is absent.

    The canonical reader the drift fingerprint is taken from, and the same
    occurrence rule the identity projector uses. Strict: a document whose
    managed delimiters are malformed has no single body to compare against, and
    answering with one of the readings would be the fingerprint agreeing with
    itself by accident.

    Raises:
        MarkdownLayoutError: The document has no unambiguous layout.
    """
    document = scan_document(raw)
    blocks = [(block.label, block.text) for block in document.blocks]
    index = _locate(blocks, section, occurrence)
    return None if index is None else document.blocks[index].body


def read_preamble(raw: str) -> str:
    """Return the LF-normalized text preceding the first H2 heading.

    Managed preamble bodies canonically normalize CRLF/CR to LF and strip
    boundary whitespace, preserving all interior text. Rendering adds two LF
    separators before sections; the document ends with one LF. Hashes and
    comparisons use that same canonical body, not the layout separators, so
    section insertion/removal cannot create preamble drift on a round trip.

    Strict for the same reason :func:`read_section_body` is: this is the body a
    managed preamble's fingerprint is taken from.

    Raises:
        MarkdownLayoutError: The document has no unambiguous layout.
    """
    return scan_document(raw).preamble


def _normalize_preamble(body: str) -> str:
    return canonical_body(body)


def _preamble_carries_section(body: str) -> bool:
    """Whether a preamble body would stop being the preamble once written.

    A preamble ends at the first H2, so a body containing one cannot be carried
    there — and unlike a section there is no outer block to tie an envelope to.
    Refused, as it always has been, but now by the shared scanner: a ``##``
    inside a closed fence is code and no longer refuses a legitimate body.
    """
    try:
        return bool(scan_document(body).blocks)
    except MarkdownLayoutError:
        return True


def _read_surface_body(raw: str, surface: Any) -> str | None:
    """The on-disk body of one surface. Raises on an unreadable document.

    Deliberately not softened to ``None``: "this section is absent" and "this
    document cannot be read unambiguously" are different answers, and a caller
    that treated the second as the first would publish over a tampered file.
    """
    if surface.kind == PROJECTION_PREAMBLE_SURFACE_KIND:
        return _normalize_preamble(read_preamble(raw))
    return read_section_body(raw, surface.section, surface.occurrence)


def _write_document(preamble: str, blocks: Sequence[tuple[str, str]]) -> str:
    text = preamble + "".join(block for _label, block in blocks)
    return text.rstrip("\n") + "\n" if text.strip() else ""


def has_frontmatter_entry(raw: str) -> bool:
    """Whether this document is a REST-owned single-entry frontmatter file.

    Those files have their own model contract (``KnowledgeEntry.from_markdown``)
    and are not an H2-section surface, so the graph projection refuses them
    instead of rewriting somebody else's shape.
    """
    from .storage.frontmatter import parse_frontmatter

    meta, _body = parse_frontmatter(raw)
    return bool(meta and meta.get("id") and meta.get("section"))


# ---------------------------------------------------------------------------
# Drift and adoption
# ---------------------------------------------------------------------------

def surface_refusal(
    config: Any, journal: MemoryEventJournal, binding: ProjectionBinding
) -> str | None:
    """Why this surface may not be published, or ``None``.

    Three refusals, each a bounded ``<reason>:<entry id>`` token:

    * ``surface_unsupported`` — the domain file is a frontmatter-backed entry;
    * ``surface_not_adopted`` — the section exists and the registry never
      published it, so it belongs to whoever wrote it;
    * ``surface_drift`` — the registry published it and the on-disk body has
      changed since, so republishing would overwrite an external edit.

    And one thing that looks like both of those and is neither: **our own
    unfinished write**. A publication declares its intent before replacing the
    file (see :func:`publish_l1`), so a body that matches the recorded intent is
    a write of ours whose finalization never ran — the second crash window. It
    must not be reported as an external edit, because the publication that
    produced those bytes was the authority's own and recovery has to be able to
    finish it. A body matching *neither* the last published record nor the
    declared intent is still drift, and still refused: that comparison is what
    protects a human's edit, and it is not relaxed here.
    """
    path = domain_path(config, binding.domain)
    if not path.exists() and binding.kind != PROJECTION_PREAMBLE_SURFACE_KIND:
        return None
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    if has_frontmatter_entry(raw):
        return f"{SURFACE_UNSUPPORTED}:{binding.entry_id}"

    try:
        current = _read_surface_body(raw, binding)
    except MarkdownLayoutError:
        # A document whose managed delimiters are malformed is one somebody
        # edited into a shape this framework cannot read back. Refused as the
        # drift it is: republishing over it would destroy whatever they meant.
        return f"{SURFACE_DRIFT}:{binding.entry_id}"
    if current is None:
        return None
    on_disk = body_sha256(current)
    record = journal.surface(binding.entry_id)
    if (binding.kind == PROJECTION_PREAMBLE_SURFACE_KIND
            and not current and record is not None and record.status == "retired"):
        return None
    if (record is not None and on_disk == record.body_sha256
            and (binding.kind != PROJECTION_PREAMBLE_SURFACE_KIND or record.status != "retired")):
        return None
    intent = journal.surface_intent(binding.entry_id)
    if (binding.kind == PROJECTION_PREAMBLE_SURFACE_KIND and not current
            and intent is not None and intent.action == SURFACE_INTENT_RETIRE):
        return None
    if (
        intent is not None
        and intent.action == SURFACE_INTENT_PUBLISH
        and on_disk == intent.body_sha256
    ):
        return None
    if record is None:
        if binding.kind == PROJECTION_PREAMBLE_SURFACE_KIND and not current:
            return None
        return f"{SURFACE_NOT_ADOPTED}:{binding.entry_id}"
    return f"{SURFACE_DRIFT}:{binding.entry_id}"


# ---------------------------------------------------------------------------
# The publishers
# ---------------------------------------------------------------------------

def publish_graph(store: Any, events: Sequence[MemoryEvent]) -> Any:
    """Project the complete stream into the graph store. Never a partial stream.

    ``rebuild`` replaces graph state with the projection of what it is handed,
    which is exactly why the caller must hand it the *whole* journal: anything
    omitted would be erased.
    """
    report = store.rebuild(list(events))
    if not report.ok:
        raise ProjectionError(
            f"graph rebuild reported {len(report.errors)} error(s)"
        )
    return report


def surface_plans(
    events: Sequence[MemoryEvent], nodes: Mapping[str, GraphNode]
) -> list[SurfacePlan]:
    """What every bound node should publish, after the replay decided its state.

    One plan per surface identity. Among the nodes bound to one surface the
    *current* one wins; when none is current the surface is retired, which is
    how a supersession or a retraction excludes stale readable material without
    deleting any history.
    """
    bindings = bindings_from_events(events)
    by_surface: dict[str, list[tuple[str, ProjectionBinding]]] = {}
    for node_id, binding in bindings.items():
        by_surface.setdefault(binding.entry_id, []).append((node_id, binding))

    plans: list[SurfacePlan] = []
    for entry_id in sorted(by_surface):
        owners = by_surface[entry_id]
        current = [
            (node_id, binding)
            for node_id, binding in owners
            if node_id in nodes and nodes[node_id].is_current()
        ]
        node_id, binding = (current or owners)[-1]
        plans.append(
            SurfacePlan(
                entry_id=entry_id,
                domain=binding.domain,
                section=binding.section,
                occurrence=binding.occurrence,
                kind=binding.kind,
                node_id=node_id,
                body=nodes[node_id].content if current and node_id in nodes else "",
                action="publish" if current else "retire",
            )
        )
    return plans


def render_surface_document(
    raw: str, plans: Sequence[SurfacePlan],
) -> tuple[str | None, L1ProjectionReport]:
    """Render one domain's final state without I/O or registry mutations.

    Resolve occurrences against the original blocks before removing anything;
    retiring one duplicate must not change which block a sibling plan names.
    ``None`` means no managed body changed, preserving the original bytes.
    """
    preamble, blocks = _parse_document(raw)
    report = L1ProjectionReport()
    updates: dict[int, tuple[str, str] | None] = {}
    additions: list[SurfacePlan] = []
    preamble_changed = False
    preamble_domains: set[str] = set()
    for plan in plans:
        if plan.kind == PROJECTION_PREAMBLE_SURFACE_KIND:
            if plan.domain in preamble_domains:
                raise ProjectionError(f"surface_owned_by:{plan.node_id}")
            preamble_domains.add(plan.domain)
            desired = plan.normalized_body if plan.action != "retire" else ""
            if _normalize_preamble(preamble) == desired:
                report.unchanged.append(plan.entry_id)
            else:
                preamble_changed = True
                if plan.action == "retire":
                    report.retired.append(plan.entry_id)
                else:
                    report.published.append(plan.entry_id)
            preamble = desired + "\n\n" if desired else ""
            continue
        index = _locate(blocks, plan.section, plan.occurrence)
        if plan.action == "retire":
            if index is not None:
                updates[index] = None
                report.retired.append(plan.entry_id)
        elif index is None:
            additions.append(plan)
            report.published.append(plan.entry_id)
        elif _block_body(blocks[index][1]) != plan.normalized_body:
            updates[index] = (plan.section, _render_block(plan.section, plan.body))
            report.published.append(plan.entry_id)
        else:
            report.unchanged.append(plan.entry_id)
    if not updates and not additions and not preamble_changed:
        return None, report
    final_blocks = [updates.get(index, block) for index, block in enumerate(blocks)
                    if updates.get(index, block) is not None]
    final_blocks.extend((plan.section, _render_block(plan.section, plan.body))
                        for plan in sorted(additions, key=lambda item: item.occurrence))
    return _write_document(preamble, final_blocks), report


def _plan_layout_error(
    final: str, blocks: Sequence[tuple[str, str]], plan: SurfacePlan
) -> list[str]:
    """Why this plan's rendered document would not read back, or nothing."""
    try:
        body = _read_surface_body(final, plan)
    except MarkdownLayoutError:
        return [f"{LAYOUT_MISMATCH}:{plan.entry_id}"]
    expected = plan.normalized_body if plan.action == "publish" else (
        "" if plan.kind == PROJECTION_PREAMBLE_SURFACE_KIND else None
    )
    if body != expected:
        return [f"{LAYOUT_MISMATCH}:{plan.entry_id}"]
    if (
        plan.action != "publish"
        or plan.kind == PROJECTION_PREAMBLE_SURFACE_KIND
        or not plan.normalized_body
    ):
        return []
    index = _locate(blocks, plan.section, plan.occurrence)
    if index is None or _bodyful_occurrence(blocks, index) != plan.occurrence:
        return [f"{LAYOUT_OCCURRENCE_MISMATCH}:{plan.entry_id}"]
    return []


def layout_errors(
    config: Any,
    plans: Sequence[SurfacePlan],
    *,
    path_for: Callable[[str], Path] | None = None,
) -> list[str]:
    """Render every affected domain and read every plan back. Bounded labels.

    The one round-trip guard, shared by the ordinary semantic write, the group
    rewrite and the publication/recovery path, because the question is the same
    one in all three: *if this were published, could this framework still read
    what it wrote?* A layout it cannot support is refused here — before any
    durable event is appended and before any domain's bytes are replaced — so a
    publication can never succeed into a document that then locks every surface
    in it behind a drift refusal.

    Nothing is written and no registry row is touched: the rendering is the pure
    :func:`render_surface_document`, and the files are only read.

    Args:
        config: The configuration naming the knowledge directories.
        plans: The surfaces this publication would write, any mix of domains.
        path_for: An alternative domain-to-path resolver, for a caller that
            validates the path itself (the group rewrite refuses an aliased or
            escaping domain before it reads one).

    Returns:
        Sorted, deduplicated ``<reason>:<entry id>`` labels, capped at
        :data:`LAYOUT_ERROR_LIMIT`. Empty means every plan round-trips.
    """
    resolve = path_for if path_for is not None else (
        lambda domain: domain_path(config, domain)
    )
    by_domain: dict[str, list[SurfacePlan]] = {}
    for plan in plans:
        by_domain.setdefault(plan.domain, []).append(plan)
    errors: list[str] = []
    for domain in sorted(by_domain):
        domain_plans = by_domain[domain]
        path = resolve(domain)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            rendered, _report = render_surface_document(raw, domain_plans)
            final = raw if rendered is None else rendered
            _preamble, blocks = _parse_document(final)
        except MarkdownLayoutError:
            errors.extend(
                f"{LAYOUT_MISMATCH}:{plan.entry_id}" for plan in domain_plans
            )
            continue
        for plan in domain_plans:
            errors.extend(_plan_layout_error(final, blocks, plan))
    return sorted(set(errors))[:LAYOUT_ERROR_LIMIT]


def plan_layout_errors(
    config: Any,
    events: Sequence[MemoryEvent],
    nodes: Mapping[str, GraphNode],
    *,
    path_for: Callable[[str], Path] | None = None,
) -> list[str]:
    """:func:`layout_errors` for a caller holding a proposed replay.

    The commit paths validate the *complete* proposed stream, so the guard sees
    exactly the plans the publication that follows would carry — including the
    retirements a supersession implies.
    """
    return layout_errors(config, surface_plans(events, nodes), path_for=path_for)


def publish_l1(
    config: Any,
    journal: MemoryEventJournal,
    events: Sequence[MemoryEvent],
    nodes: Mapping[str, GraphNode],
    *,
    position: int = 0,
) -> L1ProjectionReport:
    """Render every managed surface into its L1 file, preserving the rest.

    Idempotent: a surface whose on-disk body already equals the desired one is
    left alone and reported as ``unchanged``, so republishing writes nothing.

    Refuses rather than overwrites. A drifted or unadopted surface raises
    :class:`ProjectionError` before any file is written, so one external edit
    cannot be half-overwritten across a multi-section publication.

    **Three phases per domain**, because a registry row may never describe bytes
    that are not on disk:

    1. **Declare.** Every surface this document is about to change is recorded
       as a :class:`~layered_memory_mcp.graph_journal.SurfaceIntent`. The
       published record is left exactly as it was — it still describes the body
       the file still holds.
    2. **Replace.** The whole document is written to a temporary sibling and
       renamed over the target in one ``os.replace``, so the target is never
       truncated and never half-written.
    3. **Finalize.** Only now does the registry adopt the new body (or mark the
       surface retired), and only now is the intent cleared.

    A crash in phase 2 leaves the registry truthful, so the retry is an ordinary
    republication. A crash between phase 2 and phase 3 leaves a file matching
    the declared intent, which :func:`surface_refusal` recognises as ours. This
    ordering is the whole fix for a failed publication that used to be
    permanently misread as an external edit; moving ``record_surface`` after the
    write alone would only swap which window was unrecoverable.
    """
    plans = surface_plans(events, nodes)
    if not plans:
        return L1ProjectionReport()

    refusals: list[str] = []
    for plan in plans:
        binding = ProjectionBinding(
            domain=plan.domain,
            section=plan.section,
            occurrence=plan.occurrence,
            kind=plan.kind,
            body=plan.body,
        )
        refusal = surface_refusal(config, journal, binding)
        if refusal is not None:
            refusals.append(refusal)
    if refusals:
        raise ProjectionError("; ".join(sorted(set(refusals))))

    # Every domain's round trip, before the first one is written. A layout
    # error found while publishing the third domain must not leave the first
    # two published: the commit paths already refused this layout, so reaching
    # here means the files changed underneath, and the honest answer is a
    # recoverable pending projection rather than a partial publication.
    layout = layout_errors(config, plans)
    if layout:
        raise ProjectionError("; ".join(layout))

    by_domain: dict[str, list[SurfacePlan]] = {}
    for plan in plans:
        by_domain.setdefault(plan.domain, []).append(plan)

    report = L1ProjectionReport(domains=sorted(by_domain))
    for domain in sorted(by_domain):
        path = domain_path(config, domain)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        rendered, changes = render_surface_document(raw, by_domain[domain])
        report.published.extend(changes.published)
        report.retired.extend(changes.retired)
        report.unchanged.extend(changes.unchanged)

        if rendered is not None:
            for plan in by_domain[domain]:
                journal.record_surface_intent(_plan_intent(plan, position))
            replace_document(path, rendered)

        for plan in by_domain[domain]:
            if plan.action == "retire":
                journal.retire_surface(plan.entry_id, position=position)
            else:
                journal.record_surface(
                    SurfaceRecord(
                        entry_id=plan.entry_id,
                        domain=plan.domain,
                        section=plan.section,
                        occurrence=plan.occurrence,
                        kind=plan.kind,
                        node_id=plan.node_id,
                        body_sha256=body_sha256(plan.normalized_body),
                        position=position,
                    )
                )
            # Unconditionally, not only when this run wrote: the surface we just
            # finalized may be one whose *previous* publication crashed after
            # its replacement landed, in which case this run found the file
            # already carrying the intended body, changed nothing, and is still
            # the publication that finished it.
            journal.clear_surface_intent(plan.entry_id)

    _invalidate_scan_cache(config)
    return report


def _plan_intent(plan: SurfacePlan, position: int) -> SurfaceIntent:
    """The intent one plan declares before its document is replaced."""
    retire = plan.action == "retire"
    return SurfaceIntent(
        entry_id=plan.entry_id,
        domain=plan.domain,
        section=plan.section,
        occurrence=plan.occurrence,
        kind=plan.kind,
        node_id=plan.node_id,
        body_sha256="" if retire else body_sha256(plan.normalized_body),
        action=SURFACE_INTENT_RETIRE if retire else SURFACE_INTENT_PUBLISH,
        position=position,
    )


def replace_document(path: Path, text: str) -> None:
    """Replace one L1 document atomically, or leave it exactly as it was.

    The bytes go to a temporary sibling first and then over the target in a
    single ``os.replace``, which is atomic within a directory: a reader sees
    either the whole previous document or the whole new one, never a truncated
    or half-written file. ``Path.write_text`` on the target cannot give that —
    it truncates first — and a truncated knowledge file is the one outcome a
    publication must never produce.

    The temporary file is in the *target's* directory, because a rename is only
    atomic within a filesystem and the knowledge directory may be configured
    anywhere. It is removed on every path out, including the failing one, so an
    interrupted publication leaves no debris for the next scan to read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}{PUBLISH_TEMP_SUFFIX}")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Debris cleanup may not fail a publication that landed. The
            # ordinary case is that there is nothing to remove, because the
            # rename consumed it; ``missing_ok`` covers that without hiding it.
            pass


def _invalidate_scan_cache(config: Any) -> None:
    """Drop the keyword-recall file cache so the next read sees the new file."""
    from .recall import invalidate_scan_cache

    for directory in getattr(config, "knowledge_dirs", ()) or ():
        invalidate_scan_cache(str(directory))


def publish_vector(config: Any, domains: Iterable[str]) -> dict[str, Any]:
    """Republish the vector rows for ``domains`` from their published L1 files.

    Uses the existing ``sync_to_vector_store(replace_domain=True)`` path — the
    one that takes a database snapshot, re-reads the source file and validates
    it has not changed before publishing — rather than any ad-hoc SQL. A failed
    sync raises, so its checkpoint is not advanced.
    """
    data_dir = Path(config.home) / "data"
    from .injector import sync_to_vector_store

    published: list[str] = []
    for domain in sorted(set(domains)):
        path = domain_path(config, domain)
        result = sync_to_vector_store(
            data_dir,
            domain,
            path.read_text(encoding="utf-8") if path.exists() else "",
            replace_domain=True,
            source_path=path if path.exists() else None,
            vector_db_path=getattr(config, "vector_db_path", None),
        )
        if not result.get("success"):
            raise ProjectionError(f"vector publication failed for domain {domain!r}")
        published.append(domain)
    return {"domains": published}


def publish_todo(config: Any, nodes: Iterable[GraphNode]) -> dict[str, Any]:
    """Republish the readable TODO view from the replayed task state.

    A projection like every other one here: derived from the journal, written
    through one owner (:func:`~layered_memory_mcp.graph_task.publish_tasks`),
    and refused rather than forced when the store holds rows the authority does
    not know about. It is the one projection that depends on the *graph* alone —
    a task is not a readable L1 surface — which is why a refused L1 section does
    not hold it up (see ``graph_application``'s dependency table).
    """
    from .graph_task import publish_tasks

    return publish_tasks(config, nodes)


def publish_l0(config: Any) -> dict[str, Any]:
    """Regenerate the L0 index from the published L1 files."""
    from .l0_manager import sync_l0_index

    result = sync_l0_index(config)
    if not result.get("success"):
        raise ProjectionError("l0 index publication failed")
    return {"entries": result.get("total_entries", 0)}

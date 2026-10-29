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
  sections a managed node is bound to. Every other section, the preamble and
  every other file are preserved.
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

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import Field

from .graph_entry_identity import markdown_section_entry_id
from .graph_journal import MemoryEventJournal, SurfaceRecord, body_sha256
from .graph_models import GraphModel, GraphNode, MemoryEvent

__all__ = [
    "PROJECTION_METADATA_KEY",
    "PROJECTION_SURFACE_KIND",
    "SURFACE_DRIFT",
    "SURFACE_NOT_ADOPTED",
    "SURFACE_UNSUPPORTED",
    "L1ProjectionReport",
    "ProjectionBinding",
    "ProjectionError",
    "SurfacePlan",
    "binding_metadata",
    "bindings_from_events",
    "domain_path",
    "parse_binding",
    "publish_graph",
    "publish_l0",
    "publish_l1",
    "publish_vector",
    "read_section_body",
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

#: Refusal labels. Bounded tokens: ``<reason>:<entry id>``, never a body.
SURFACE_DRIFT = "surface_drift"
SURFACE_NOT_ADOPTED = "surface_not_adopted"
SURFACE_UNSUPPORTED = "surface_unsupported"

_HEADING_SPLIT = re.compile(r"(?m)^(?=##[ \t])")
_HEADING_LABEL = re.compile(r"^##[ \t]+(.*?)[ \t]*$")


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

    @property
    def entry_id(self) -> str:
        """The versioned projection identity — also the vector row's own ``id``."""
        return markdown_section_entry_id(self.domain, self.section, self.occurrence)

    @property
    def normalized_body(self) -> str:
        return self.body.strip()


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
    domain = raw.get("domain")
    section = raw.get("section")
    if not isinstance(domain, str) or not isinstance(section, str):
        return None
    occurrence = raw.get("occurrence", 0)
    if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0:
        return None
    return ProjectionBinding(
        domain=domain, section=section, occurrence=occurrence, body=""
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
    survive a publication byte-for-byte.
    """
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = _HEADING_SPLIT.split(normalized)
    if not parts:
        return "", []
    preamble = parts[0]
    blocks: list[tuple[str, str]] = []
    for part in parts[1:]:
        heading, _, _rest = part.partition("\n")
        match = _HEADING_LABEL.match(heading)
        blocks.append((match.group(1).strip() if match else "", part))
    return preamble, blocks


def _block_body(block: str) -> str:
    _heading, _, rest = block.partition("\n")
    return rest.strip()


def _render_block(section: str, body: str) -> str:
    return f"## {section}\n\n{body.strip()}\n\n"


def _locate(blocks: Sequence[tuple[str, str]], section: str, occurrence: int) -> int | None:
    seen = 0
    for index, (label, _block) in enumerate(blocks):
        if label != section:
            continue
        if seen == occurrence:
            return index
        seen += 1
    return None


def read_section_body(raw: str, section: str, occurrence: int = 0) -> str | None:
    """The current body of one H2 section, or ``None`` when it is absent.

    The canonical reader the drift fingerprint is taken from, and the same
    occurrence rule the identity projector uses.
    """
    _preamble, blocks = _parse_document(raw)
    index = _locate(blocks, section, occurrence)
    return None if index is None else _block_body(blocks[index][1])


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
    """
    path = domain_path(config, binding.domain)
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if has_frontmatter_entry(raw):
        return f"{SURFACE_UNSUPPORTED}:{binding.entry_id}"

    current = read_section_body(raw, binding.section, binding.occurrence)
    if current is None:
        return None
    record = journal.surface(binding.entry_id)
    if record is None:
        return f"{SURFACE_NOT_ADOPTED}:{binding.entry_id}"
    if body_sha256(current) != record.body_sha256:
        return f"{SURFACE_DRIFT}:{binding.entry_id}"
    return None


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
                node_id=node_id,
                body=nodes[node_id].content if current and node_id in nodes else "",
                action="publish" if current else "retire",
            )
        )
    return plans


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
            body=plan.body,
        )
        refusal = surface_refusal(config, journal, binding)
        if refusal is not None:
            refusals.append(refusal)
    if refusals:
        raise ProjectionError("; ".join(sorted(set(refusals))))

    by_domain: dict[str, list[SurfacePlan]] = {}
    for plan in plans:
        by_domain.setdefault(plan.domain, []).append(plan)

    report = L1ProjectionReport(domains=sorted(by_domain))
    for domain in sorted(by_domain):
        path = domain_path(config, domain)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        preamble, blocks = _parse_document(raw)
        changed = False

        for plan in by_domain[domain]:
            index = _locate(blocks, plan.section, plan.occurrence)
            if plan.action == "retire":
                if index is not None:
                    blocks.pop(index)
                    changed = True
                    report.retired.append(plan.entry_id)
                journal.retire_surface(plan.entry_id, position=position)
                continue

            rendered = _render_block(plan.section, plan.body)
            if index is None:
                blocks.append((plan.section, rendered))
                changed = True
                report.published.append(plan.entry_id)
            elif _block_body(blocks[index][1]) != plan.body.strip():
                blocks[index] = (plan.section, rendered)
                changed = True
                report.published.append(plan.entry_id)
            else:
                report.unchanged.append(plan.entry_id)
            journal.record_surface(
                SurfaceRecord(
                    entry_id=plan.entry_id,
                    domain=plan.domain,
                    section=plan.section,
                    occurrence=plan.occurrence,
                    node_id=plan.node_id,
                    body_sha256=body_sha256(plan.body.strip()),
                    position=position,
                )
            )

        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_write_document(preamble, blocks), encoding="utf-8")

    _invalidate_scan_cache(config)
    return report


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


def publish_l0(config: Any) -> dict[str, Any]:
    """Regenerate the L0 index from the published L1 files."""
    from .l0_manager import sync_l0_index

    result = sync_l0_index(config)
    if not result.get("success"):
        raise ProjectionError("l0 index publication failed")
    return {"entries": result.get("total_entries", 0)}

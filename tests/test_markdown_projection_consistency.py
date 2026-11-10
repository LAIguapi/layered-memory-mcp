"""One Markdown section reader for every surface of one authored body.

Why this file exists: a managed H2 section is written by the projection and read
back by four independent readers — the drift fingerprint
(``graph_projection.read_section_body``), the vector projection
(``injector._vector_entries``), keyword ownership (``recall._h2_owner_by_line``)
and the migration preflight. Where those readers disagree about where one
section ends, the framework refuses *its own* publication: the body is published
verbatim, the next reader splits it at an internal ``##`` line, and the drift
check then sees a body nobody wrote — for every surface in that document, not
only the one involved.

What is pinned here:

  * an authored H2 (and a fenced ``##`` line, and a tilde fence, and a short or
    wrongly-sized closing fence, and an unterminated one) round-trips
    byte-identically through the published file, with the authored heading
    neither demoted, escaped, fenced nor truncated;
  * one managed body stays one identity: the vector row and the keyword owner
    map the *whole* body to the same entry ID the registry hashed, and the
    envelope delimiters never appear in indexed content;
  * ordinary documents — plain prose, H3, duplicate labels, legacy files nobody
    managed — keep their bytes and their reading, and get no envelope they do
    not need;
  * a tampered, unpaired, duplicated, mis-tied or colliding delimiter is
    refused by name, never absorbed, and a genuine external body edit is still
    blocked;
  * the round-trip guard runs on the ordinary write, the group rewrite and the
    publication/recovery path, before the durable append and before any
    domain's bytes are replaced, so an unsupportable layout leaves zero events,
    zero file mutations and zero registry rows.

Isolation: the ``config`` fixture from ``test_v361_graph_application`` (tmp_path
home, tmp SQLite journal/graph/vector databases) and its deterministic fake
embedding function. No network, no model, no user memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_v361_graph_application import (  # noqa: F401 — fake_embeddings is autouse
    _journal,
    _l1_text,
    _request,
    _vector_rows,
    config,
    fake_embeddings,
)

from layered_memory_mcp import graph_projection, injector
from layered_memory_mcp.graph_application import (
    GRAPH_COMMIT_COMMITTED,
    GRAPH_COMMIT_REJECTED_BINDING,
    GRAPH_COMMIT_REJECTED_DRIFT,
    RECOVERY_INCOMPLETE,
    RECOVERY_OK,
    ProjectionBinding,
    _bind_events,
    commit_semantic_write,
    recover_projections,
)
from layered_memory_mcp.graph_entry_identity import markdown_section_entry_id
from layered_memory_mcp.graph_journal import (
    PROJECTION_L1,
    REQUEST_KIND_SEMANTIC_WRITE,
    body_sha256,
)
from layered_memory_mcp.graph_search import keyword_section_seeds
from layered_memory_mcp.graph_write import plan_semantic_write
from layered_memory_mcp.markdown_sections import (
    MANAGED_BODY_SCHEME,
    MANAGED_MARKER_COLLISION,
    MANAGED_MARKER_TRAILING,
    MANAGED_MARKER_UNEXPECTED,
    MANAGED_MARKER_UNPAIRED,
    MANAGED_MARKER_UNTERMINATED,
    MarkdownLayoutError,
    canonical_body,
    managed_body_markers,
    render_section,
    scan_document,
)
from layered_memory_mcp.recall import _h2_owner_by_line

# An authored body that *contains* an H2. The heading is the author's; the
# projection may not demote it, escape it, fence it or truncate the body at it.
H2_BODY = (
    "The retrieval index is rebuilt nightly at 02:00 UTC.\n"
    "\n"
    "## Rollback\n"
    "\n"
    "Keep the previous index for one cycle."
)

BODY_OPS = "Restart the indexer, then re-run the nightly job."

#: Bodies whose ``##`` line is code, or whose fence would otherwise reach into
#: the next section. Every one must round-trip exactly.
FENCE_BODIES = {
    "backtick_closed": "Run the generator:\n\n```sh\n## not a heading\n```\n\nDone.",
    "tilde_closed": "Run the generator:\n\n~~~\n## not a heading\n~~~\n\nDone.",
    "long_fence_info": (
        "Run the generator:\n\n````markdown\n## not a heading\n```\nstill code\n````"
    ),
    "short_closer": "Run the generator:\n\n````\n## not a heading\n```",
    "unterminated": "Run the generator:\n\n```sh\n## not a heading",
}

#: An ordinary document nobody managed: a preamble, a duplicate H2 label, a
#: deeper heading and a fenced ``##`` line.
LEGACY_DOCUMENT = (
    "Operational notes.\n"
    "\n"
    "## Runbook\n"
    "\n"
    "Restart the indexer.\n"
    "\n"
    "### Escalation\n"
    "\n"
    "Page the on-call.\n"
    "\n"
    "## Runbook\n"
    "\n"
    "Second block with the same label.\n"
    "\n"
    "## Snippets\n"
    "\n"
    "```md\n"
    "## Not a section\n"
    "```\n"
)


def _write(
    config,
    node_id: str,
    domain: str,
    section: str,
    body: str,
    *,
    occurrence: int = 0,
    offset: int = 0,
):
    """One ordinary semantic write, bound to one H2 surface."""
    return commit_semantic_write(
        config,
        _request(node_id, body, offset=offset, request_key=node_id),
        ProjectionBinding(
            domain=domain, section=section, occurrence=occurrence, body=body
        ),
    )


def _domain_file(config, domain: str) -> Path:
    return Path(config.knowledge_dir) / f"{domain}.md"


def _registry(config) -> dict[str, str]:
    """``entry_id -> published body hash`` straight from the surface registry."""
    with _journal(config) as journal:
        return {
            record.entry_id: record.body_sha256
            for record in journal.surfaces(include_retired=False)
        }


def _head(config) -> int:
    with _journal(config) as journal:
        return journal.head_position()


# ---------------------------------------------------------------------------
# The body the framework published is the body it reads back
# ---------------------------------------------------------------------------

def test_authored_h2_body_round_trips_through_the_published_surface(config):
    """The canonical reader reads back exactly the body that was published."""
    commit = _write(config, "claim_h2", "retrieval", "Index maintenance", H2_BODY)

    assert commit.status == GRAPH_COMMIT_COMMITTED
    assert commit.surface_published is True

    raw = _l1_text(config, "retrieval")
    assert "## Rollback" in raw
    assert (
        graph_projection.read_section_body(raw, "Index maintenance") == H2_BODY
    )


def test_an_unrelated_domain_still_commits_after_an_h2_surface_is_published(config):
    """The self-lock: one H2 body used to refuse every later write, everywhere.

    The published body read back truncated, so the drift fingerprint of a
    surface nobody had touched no longer matched its registry row — and
    ``_drift_errors`` is computed over the whole proposed replay, so the
    refusal was not even confined to the domain that caused it.
    """
    first = _write(config, "claim_h2", "retrieval", "Index maintenance", H2_BODY)
    assert first.status == GRAPH_COMMIT_COMMITTED

    second = _write(config, "claim_ops", "ops", "Runbook", BODY_OPS, offset=1)

    assert second.status == GRAPH_COMMIT_COMMITTED
    assert second.committed is True
    assert second.surface_published is True
    assert second.errors == []
    assert "## Runbook" in _l1_text(config, "ops")
    # ...and the H2 surface is still exactly what it was.
    assert (
        graph_projection.read_section_body(
            _l1_text(config, "retrieval"), "Index maintenance"
        )
        == H2_BODY
    )


@pytest.mark.parametrize("name", sorted(FENCE_BODIES))
def test_fenced_bodies_round_trip_whatever_the_fence_looks_like(config, name):
    """Backtick and tilde, varied lengths, info strings, a too-short closer.

    A ``##`` line inside a closed fence is code and must not become a surface;
    a fence left open must not reach forward into the document.
    """
    body = FENCE_BODIES[name]
    commit = _write(config, f"claim_{name}", "retrieval", "Generator", body)

    assert commit.status == GRAPH_COMMIT_COMMITTED
    raw = _l1_text(config, "retrieval")
    assert graph_projection.read_section_body(raw, "Generator") == body
    assert [row[0] for row in _vector_rows(config)] == [commit.entry_id]


def test_an_unterminated_fence_body_does_not_consume_the_next_section(config):
    """The fence the body left open may not swallow a sibling surface."""
    first = _write(
        config, "claim_fence", "retrieval", "Generator", FENCE_BODIES["unterminated"]
    )
    second = _write(
        config, "claim_cache", "retrieval", "Cache", "The cache TTL is 30 seconds.", offset=1
    )

    assert first.status == GRAPH_COMMIT_COMMITTED
    assert second.status == GRAPH_COMMIT_COMMITTED
    raw = _l1_text(config, "retrieval")
    assert (
        graph_projection.read_section_body(raw, "Generator")
        == FENCE_BODIES["unterminated"]
    )
    assert (
        graph_projection.read_section_body(raw, "Cache")
        == "The cache TTL is 30 seconds."
    )
    assert sorted(row[0] for row in _vector_rows(config)) == sorted(
        [first.entry_id, second.entry_id]
    )


def test_a_crlf_body_is_published_and_hashed_in_one_canonical_form(config):
    """A body carrying CRLF used to be written as CRLF and read back as LF.

    The registry then held the hash of a body the file did not contain, so a
    surface drifted from itself the moment it was published.
    """
    body = "Rebuilt nightly.\r\n\r\n## Rollback\r\n\r\nKeep one cycle."
    commit = _write(config, "claim_crlf", "retrieval", "Index maintenance", body)

    assert commit.status == GRAPH_COMMIT_COMMITTED
    raw = _l1_text(config, "retrieval")
    assert "\r" not in raw
    assert (
        graph_projection.read_section_body(raw, "Index maintenance")
        == canonical_body(body)
    )
    assert _registry(config)[commit.entry_id] == body_sha256(canonical_body(body))


def test_republishing_an_h2_surface_is_byte_for_byte_idempotent(config):
    """Forced recovery republishes everything and must change nothing."""
    commit = _write(config, "claim_h2", "retrieval", "Index maintenance", H2_BODY)
    before = _domain_file(config, "retrieval").read_bytes()
    registry = _registry(config)

    recovery = recover_projections(config, force=True)

    assert recovery.status == RECOVERY_OK
    assert recovery.projections_pending == []
    assert _domain_file(config, "retrieval").read_bytes() == before
    assert _registry(config) == registry
    assert registry[commit.entry_id] == body_sha256(H2_BODY)


# ---------------------------------------------------------------------------
# One body, one identity, for every reader
# ---------------------------------------------------------------------------

def test_one_managed_body_is_one_identity_for_the_vector_and_keyword_readers(config):
    """The whole body maps to the outer surface, with no delimiter in it."""
    commit = _write(config, "claim_h2", "retrieval", "Index maintenance", H2_BODY)
    raw = _l1_text(config, "retrieval")

    rows = _vector_rows(config)
    assert [row[0] for row in rows] == [commit.entry_id]
    assert "Keep the previous index for one cycle." in rows[0][2]
    assert MANAGED_BODY_SCHEME not in rows[0][2]
    assert injector._bodyful_sections(raw) == [("Index maintenance", H2_BODY)]
    # The framework's own cross-reader consistency check.
    assert injector.vector_store_needs_reindex(config) is False

    # Keyword recall: every line of the body — including the authored heading
    # inside it — is owned by the one surface that publishes it.
    owners = _h2_owner_by_line(raw)
    lines = raw.split("\n")
    for needle in ("rebuilt nightly", "## Rollback", "Keep the previous index"):
        index = next(position for position, line in enumerate(lines) if needle in line)
        assert owners[index] == ("Index maintenance", 0)
    assert keyword_section_seeds("retrieval", raw, "Rollback") == [
        {"section": "Index maintenance", "occurrence": 0, "entry_id": commit.entry_id}
    ]


def test_duplicate_labels_keep_separate_identities_and_separate_envelopes(config):
    """Two same-label H2 bodies, both carrying headings, stay two surfaces."""
    second_body = "Rebuilt hourly.\n\n## Rollback\n\nKeep two cycles."
    first = _write(config, "claim_one", "retrieval", "Notes", H2_BODY)
    second = _write(
        config, "claim_two", "retrieval", "Notes", second_body, occurrence=1, offset=1
    )

    assert first.status == GRAPH_COMMIT_COMMITTED
    assert second.status == GRAPH_COMMIT_COMMITTED
    assert first.entry_id != second.entry_id
    raw = _l1_text(config, "retrieval")
    assert graph_projection.read_section_body(raw, "Notes", 0) == H2_BODY
    assert graph_projection.read_section_body(raw, "Notes", 1) == second_body
    assert injector._bodyful_sections(raw) == [
        ("Notes", H2_BODY),
        ("Notes", second_body),
    ]
    assert sorted(row[0] for row in _vector_rows(config)) == sorted(
        [first.entry_id, second.entry_id]
    )


# ---------------------------------------------------------------------------
# Ordinary documents: unchanged bytes, unchanged reading, no envelope
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        "Plain prose with no heading at all.",
        "Lead paragraph.\n\n### Escalation\n\nPage the on-call.",
        FENCE_BODIES["backtick_closed"],
    ],
)
def test_a_body_the_plain_layout_can_carry_gets_no_envelope(config, body):
    """The envelope appears only where it is genuinely required."""
    commit = _write(config, "claim_plain", "retrieval", "Index maintenance", body)

    assert commit.status == GRAPH_COMMIT_COMMITTED
    raw = _l1_text(config, "retrieval")
    assert MANAGED_BODY_SCHEME not in raw
    assert graph_projection.read_section_body(raw, "Index maintenance") == body


def test_an_ordinary_legacy_document_reads_and_survives_unchanged(config):
    """Nobody managed this file; publishing beside it changes nothing in it."""
    path = _domain_file(config, "ops")
    path.write_text(LEGACY_DOCUMENT, encoding="utf-8")

    assert injector._bodyful_sections(LEGACY_DOCUMENT) == [
        ("Runbook", "Restart the indexer.\n\n### Escalation\n\nPage the on-call."),
        ("Runbook", "Second block with the same label."),
        ("Snippets", "```md\n## Not a section\n```"),
    ]

    commit = _write(config, "claim_ops", "ops", "Index maintenance", H2_BODY)

    assert commit.status == GRAPH_COMMIT_COMMITTED
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith(LEGACY_DOCUMENT)
    assert graph_projection.read_section_body(raw, "Index maintenance") == H2_BODY
    assert graph_projection.read_section_body(raw, "Runbook", 1) == (
        "Second block with the same label."
    )


@pytest.mark.parametrize("fence", ["```markdown", "~~~markdown"])
@pytest.mark.parametrize(
    "example",
    [
        "<!-- layered-memory:managed-body sample -->",
        managed_body_markers("Example")[0],
        "\n".join(managed_body_markers("Example")),
    ],
)
def test_closed_legacy_fence_keeps_marker_examples_as_code(fence, example):
    closer = fence[:3]
    body = f"{fence}\n{example}\n## codeheading\n{closer}"
    raw = f"## A\n\n{body}\n\n## B\n\nbody\n"

    assert [(block.label, block.body) for block in scan_document(raw).blocks] == [
        ("A", body), ("B", "body")
    ]
    assert graph_projection.read_section_body(raw, "B") == "body"
    assert injector._bodyful_sections(raw) == [("A", body), ("B", "body")]
    code_line = raw.split("\n").index("## codeheading")
    assert _h2_owner_by_line(raw)[code_line] == ("A", 0)
    # Existing fenced examples are readable, but new collision admission is strict.
    with pytest.raises(MarkdownLayoutError, match=MANAGED_MARKER_COLLISION):
        render_section("A", body)


# ---------------------------------------------------------------------------
# Delimiters: refused by name, never absorbed
# ---------------------------------------------------------------------------

def _enveloped(label: str, body: str, *, tail: str = "") -> str:
    opening, closing = managed_body_markers(label)
    return f"## {label}\n\n{opening}\n{body}\n{closing}\n{tail}"


@pytest.mark.parametrize(
    "document,reason",
    [
        (
            _enveloped("Managed", "Body.\n\n## Inner\n\nMore.").replace(
                managed_body_markers("Managed")[1] + "\n", ""
            ),
            MANAGED_MARKER_UNTERMINATED,
        ),
        (
            f"## Managed\n\n{managed_body_markers('Managed')[1]}\n\nBody.\n",
            MANAGED_MARKER_UNPAIRED,
        ),
        (
            f"## Managed\n\n{managed_body_markers('Other')[0]}\nBody.\n",
            MANAGED_MARKER_UNEXPECTED,
        ),
        (
            f"## Managed\n\nAuthored first.\n{managed_body_markers('Managed')[0]}\nBody.\n",
            MANAGED_MARKER_UNEXPECTED,
        ),
        (
            _enveloped("Managed", "Body.", tail="\nAppended by somebody else.\n"),
            MANAGED_MARKER_TRAILING,
        ),
        (
            f"{managed_body_markers('Managed')[0]}\n\n## Managed\n\nBody.\n",
            MANAGED_MARKER_UNEXPECTED,
        ),
        (
            _enveloped("Managed", f"Body.\n{managed_body_markers('Other')[1]}\nMore."),
            MANAGED_MARKER_COLLISION,
        ),
    ],
)
def test_a_malformed_managed_delimiter_is_refused_by_name(document, reason):
    """Ambiguity is refused, not resolved by absorbing the next section."""
    with pytest.raises(MarkdownLayoutError) as caught:
        scan_document(document)

    assert str(caught.value).startswith(reason)
    # The readers that may not raise degrade to the pre-envelope reading.
    assert scan_document(document, strict=False) is not None


def test_content_appended_after_a_close_delimiter_is_refused_even_inside_a_fence():
    """A fence *inside* an envelope may not pair with a line outside it.

    The envelope interior is opaque, so an appended ``` cannot close a fence
    the managed body opened: if it could, the tampered lines between them would
    count as fenced content, the trailing-content check would never see them,
    and the drift fingerprint would read the body as unchanged while somebody
    else's text sat in the same block.
    """
    tampered = render_section("A", "```") + "tamper\n```\n"

    with pytest.raises(MarkdownLayoutError) as caught:
        scan_document(tampered)

    assert str(caught.value).startswith(MANAGED_MARKER_TRAILING)


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_closed_fenced_tail_is_rejected_before_append(config, fence):
    first = _write(config, "claim_h2", "retrieval", "Index maintenance", H2_BODY)
    assert first.status == GRAPH_COMMIT_COMMITTED
    path = _domain_file(config, "retrieval")
    tampered = path.read_text(encoding="utf-8") + f"{fence}\nEXTERNAL TAMPER\n{fence}\n"
    path.write_text(tampered, encoding="utf-8")
    head, registry, rows = _head(config), _registry(config), _vector_rows(config)

    commit = _write(config, "claim_cache", "retrieval", "Cache", "TTL 30s.", offset=1)

    assert commit.committed is False
    # Layout admission runs before drift when the target document is malformed.
    assert commit.status == GRAPH_COMMIT_REJECTED_BINDING
    assert commit.errors == sorted(
        f"{graph_projection.LAYOUT_MISMATCH}:{entry_id}"
        for entry_id in (first.entry_id, commit.entry_id)
    )
    assert _head(config) == head
    assert _registry(config) == registry
    assert _vector_rows(config) == rows
    assert path.read_text(encoding="utf-8") == tampered
    with pytest.raises(MarkdownLayoutError, match=MANAGED_MARKER_TRAILING):
        scan_document(tampered)


def test_two_envelopes_in_one_document_are_both_read():
    """Neither envelope's interior may influence the scan of the other."""
    document = render_section("A", "```") + render_section("B", "```")

    blocks = scan_document(document).blocks

    assert [(block.label, block.body, block.managed) for block in blocks] == [
        ("A", "```", True),
        ("B", "```", True),
    ]


@pytest.mark.parametrize(
    "body",
    [
        "Body.\n\n## Inner\n\n```py\ncode\n```",
        "Body.\n\n## Inner\n\n```py\ncode\n```\n~~~\nmore\n~~~",
        "Body.\n\n## Inner\n\n```\n~~~\nmore\n~~~",
    ],
)
def test_a_fence_opened_before_an_envelope_cannot_hide_the_managed_section(body):
    """An unmanaged section's unbalanced fence may not swallow what follows."""
    document = "## Legacy\n\n```sh\nnot closed\n\n" + render_section("Managed", body)

    blocks = scan_document(document).blocks

    assert [block.label for block in blocks] == ["Legacy", "Managed"]
    assert blocks[1].body == body
    assert blocks[0].body == "```sh\nnot closed"


@pytest.mark.parametrize("closing", ["", "<!-- /layered-memory:managed-body bad -->"])
def test_broken_envelope_after_legacy_unclosed_fence_fails_closed(closing):
    raw = "## Legacy\n\n```sh\nnot closed\n\n" + render_section(
        "Managed", "intro\n\n## Inner\n\n```py\ncode\n```"
    ).replace(managed_body_markers("Managed")[1], closing)

    with pytest.raises(MarkdownLayoutError):
        scan_document(raw)
    assert injector._bodyful_sections(raw) == []
    assert _registry_projected_ids(raw) == []


def test_fence_scanning_does_not_rescan_every_following_line(monkeypatch):
    """Repeated unclosable openers may not cost one document scan each.

    Counted structurally rather than timed: once a fence of some length has
    been proven unclosable from one position, it is unclosable from every later
    position too, and re-deriving that for each opener is what made a document
    of unbalanced fences quadratic.
    """
    from layered_memory_mcp import markdown_sections

    calls = []
    original = markdown_sections._fence_closes

    def counted(line, character, length):
        calls.append(line)
        return original(line, character, length)

    monkeypatch.setattr(markdown_sections, "_fence_closes", counted)
    lines = 400
    scan_document("## A\n\n" + "```x\n" * lines)

    assert len(calls) <= 4 * lines


@pytest.mark.parametrize("shape", ["plain_sections", "decreasing_fences", "mixed"])
@pytest.mark.parametrize("size", [80, 240])
def test_scanner_work_is_bounded_by_document_size(monkeypatch, shape, size):
    """Count visited lines and fence predicates, not elapsed time."""
    from layered_memory_mcp import markdown_sections

    if shape == "plain_sections":
        raw = "".join(f"## S{index}\n\nbody\n\n" for index in range(size))
    elif shape == "decreasing_fences":
        raw = "## A\n\n" + "\n".join("`" * length + "x" for length in range(size, 2, -1))
    else:
        raw = "".join(
            f"## S{index}\n\n```md\n## code\n```\n\n" for index in range(size)
        )
    work = {"lines": 0, "predicates": 0}
    original_regions = markdown_sections._fence_regions
    original_opener = markdown_sections._fence_opener
    original_closes = markdown_sections._fence_closes

    def regions(lines):
        work["lines"] += len(lines)
        return original_regions(lines)

    def opener(line):
        work["predicates"] += 1
        return original_opener(line)

    def closes(line, character, length):
        work["predicates"] += 1
        return original_closes(line, character, length)

    monkeypatch.setattr(markdown_sections, "_fence_regions", regions)
    monkeypatch.setattr(markdown_sections, "_fence_opener", opener)
    monkeypatch.setattr(markdown_sections, "_fence_closes", closes)
    document = scan_document(raw)
    assert len(document.blocks) == (1 if shape == "decreasing_fences" else size)
    total = len(raw.split("\n"))
    assert work["lines"] <= 4 * total, work
    assert work["predicates"] <= 8 * total, work


def test_a_renamed_heading_does_not_re_own_a_managed_body():
    """The delimiter is tied to its own heading, so a rename is malformed."""
    document = _enveloped("Managed", "Body.\n\n## Inner\n\nMore.")
    renamed = document.replace("## Managed", "## Renamed", 1)

    with pytest.raises(MarkdownLayoutError):
        scan_document(renamed)


def test_a_body_claiming_the_reserved_delimiter_is_refused_before_the_append(config):
    """An authored body cannot be published into an ambiguous document."""
    body = f"Neutral text.\n\n{managed_body_markers('Index maintenance')[0]}\n\nMore."
    entry_id = markdown_section_entry_id("retrieval", "Index maintenance")

    commit = _write(config, "claim_marker", "retrieval", "Index maintenance", body)

    assert commit.committed is False
    assert commit.status == GRAPH_COMMIT_REJECTED_BINDING
    assert commit.errors == [f"{graph_projection.LAYOUT_MISMATCH}:{entry_id}"]
    assert not _domain_file(config, "retrieval").exists()
    assert _head(config) == 0
    assert _registry(config) == {}


def test_a_tampered_delimiter_blocks_the_next_publication_and_changes_nothing(config):
    """External tamper stays blocked: refused, with the file left as it is.

    Reported as drift rather than as a layout refusal, and that distinction is
    the point: the *body* comparison is strict, so a delimiter somebody removed
    makes the on-disk body unreadable and therefore not the one the registry
    published — which is what an external edit is.
    """
    _write(config, "claim_h2", "retrieval", "Index maintenance", H2_BODY)
    path = _domain_file(config, "retrieval")
    closing = managed_body_markers("Index maintenance")[1]
    assert closing in path.read_text(encoding="utf-8")
    tampered = path.read_text(encoding="utf-8").replace(closing + "\n", "")
    path.write_text(tampered, encoding="utf-8")
    head = _head(config)
    registry = _registry(config)

    commit = _write(config, "claim_cache", "retrieval", "Cache", "TTL 30s.", offset=1)

    assert commit.committed is False
    assert commit.status == GRAPH_COMMIT_REJECTED_DRIFT
    assert path.read_text(encoding="utf-8") == tampered
    assert _head(config) == head
    assert _registry(config) == registry


def test_a_tampered_envelope_mints_no_delimiter_bearing_identity(config):
    """Malformed layout invalidates document-wide ownership, including siblings."""
    _write(config, "claim_h2", "retrieval", "Index maintenance", H2_BODY)
    _write(config, "claim_cache", "retrieval", "Cache", "TTL 30s.", offset=1)
    path = _domain_file(config, "retrieval")
    closing = managed_body_markers("Index maintenance")[1]
    path.write_text(
        path.read_text(encoding="utf-8").replace(closing + "\n", ""), encoding="utf-8"
    )
    raw = path.read_text(encoding="utf-8")

    assert injector._bodyful_sections(raw) == []
    assert _registry_projected_ids(raw) == []
    assert keyword_section_seeds("retrieval", raw, "TTL") == []
    assert keyword_section_seeds("retrieval", raw, "Rollback") == []
    assert _h2_owner_by_line(raw) == [(None, 0)] * len(raw.split("\n"))


@pytest.mark.parametrize("damage", ["missing_close", "wrong_close", "trailing"])
def test_malformed_envelope_cannot_shift_duplicate_ownership(damage):
    first = render_section("Notes", "intro\n\n## Inner\n\nleaf")
    closing = managed_body_markers("Notes")[1]
    if damage == "missing_close":
        first = first.replace(closing, "")
    elif damage == "wrong_close":
        first = first.replace(closing, managed_body_markers("Other")[1])
    else:
        first += "tamper\n"
    raw = first + render_section("Notes", "untouched sibling")

    assert injector._bodyful_sections(raw) == []
    assert _registry_projected_ids(raw) == []
    assert keyword_section_seeds("retrieval", raw, "sibling") == []
    assert _h2_owner_by_line(raw) == [(None, 0)] * len(raw.split("\n"))


def _registry_projected_ids(raw: str) -> list[str]:
    """The entry IDs the vector projection would mint for ``raw``."""
    return [entry.id for entry in injector._vector_entries("retrieval", raw)]


def test_a_genuine_external_body_edit_is_still_refused_as_drift(config):
    """The protection this repair may not relax."""
    _write(config, "claim_h2", "retrieval", "Index maintenance", H2_BODY)
    path = _domain_file(config, "retrieval")
    edited = path.read_text(encoding="utf-8").replace(
        "Keep the previous index for one cycle.", "Somebody else wrote this."
    )
    path.write_text(edited, encoding="utf-8")
    head = _head(config)

    commit = _write(config, "claim_cache", "retrieval", "Cache", "TTL 30s.", offset=1)

    assert commit.committed is False
    assert commit.status == GRAPH_COMMIT_REJECTED_DRIFT
    assert path.read_text(encoding="utf-8") == edited
    assert _head(config) == head


# ---------------------------------------------------------------------------
# The guard: before the append, and before any domain is published
# ---------------------------------------------------------------------------

def test_a_missing_occurrence_is_refused_before_the_append(config):
    """An occurrence the rendered document would not contain is unpublishable."""
    entry_id = markdown_section_entry_id("retrieval", "Index maintenance", 2)

    commit = _write(
        config, "claim_sparse", "retrieval", "Index maintenance", H2_BODY, occurrence=2
    )

    assert commit.committed is False
    assert commit.status == GRAPH_COMMIT_REJECTED_BINDING
    assert commit.errors == [f"{graph_projection.LAYOUT_MISMATCH}:{entry_id}"]
    assert not _domain_file(config, "retrieval").exists()
    assert _head(config) == 0
    assert _registry(config) == {}


def test_the_two_occurrence_rules_disagreeing_is_refused_explicitly(config):
    """A bodyless duplicate heading is named, not silently mis-indexed.

    The projection addresses a section by its position among *every* same-label
    H2; the vector and keyword readers count only bodyful ones. Publishing into
    the second ``## Notes`` of a document whose first one is empty would store
    the body under occurrence 1 and index it under occurrence 0, so the write
    is refused with that disagreement named.
    """
    path = _domain_file(config, "retrieval")
    path.write_text("## Notes\n\n## Notes\n\nLegacy body.\n", encoding="utf-8")
    entry_id = markdown_section_entry_id("retrieval", "Notes", 1)

    commit = _write(config, "claim_notes", "retrieval", "Notes", H2_BODY, occurrence=1)

    assert commit.committed is False
    assert commit.status == GRAPH_COMMIT_REJECTED_BINDING
    assert commit.errors == [
        f"{graph_projection.LAYOUT_OCCURRENCE_MISMATCH}:{entry_id}"
    ]
    assert path.read_text(encoding="utf-8") == "## Notes\n\n## Notes\n\nLegacy body.\n"
    assert _head(config) == 0


def test_the_group_rewrite_publishes_an_h2_body_through_the_same_guard(config):
    """The grouped path renders through the shared helper, so it agrees."""
    import importlib

    api = importlib.import_module("layered_memory_mcp.graph_rewrite")
    _write(config, "claim_old", "retrieval", "Index maintenance", BODY_OPS)

    from datetime import timedelta

    from test_v361_graph_application import T0, _node

    result = api.commit_semantic_rewrite(
        config,
        api.SemanticRewriteRequest(
            targets=[
                api.RewriteTarget(
                    old_node_ids=["claim_old"],
                    node=_node("claim_new", H2_BODY),
                    surface=api.SectionRef(domain="retrieval", section="New schedule"),
                )
            ],
            occurred_at=T0 + timedelta(seconds=10),
            request_key="rewrite_h2",
        ),
    )

    assert result.committed is True
    assert result.projections_pending == []
    raw = _l1_text(config, "retrieval")
    assert graph_projection.read_section_body(raw, "New schedule") == H2_BODY
    assert [row[0] for row in _vector_rows(config)] == [
        markdown_section_entry_id("retrieval", "New schedule")
    ]


def test_the_guards_labels_are_bounded(config):
    """A refusal is a bounded report, not a listing of the whole document."""
    plans = [
        graph_projection.SurfacePlan(
            entry_id=markdown_section_entry_id("retrieval", f"Section {index}", 2),
            domain="retrieval",
            section=f"Section {index}",
            occurrence=2,
            node_id=f"claim_{index}",
            body=H2_BODY,
        )
        for index in range(graph_projection.LAYOUT_ERROR_LIMIT + 10)
    ]

    errors = graph_projection.layout_errors(config, plans)

    assert len(errors) == graph_projection.LAYOUT_ERROR_LIMIT
    assert errors == sorted(errors)
    assert all(error.startswith(graph_projection.LAYOUT_MISMATCH) for error in errors)


def test_recovery_preflights_every_domain_before_publishing_any(config):
    """One unpublishable domain may not leave another half-published.

    The journal is the authority, so a decision an older version accepted can
    outlive the validation that would refuse it today. Recovery must then
    refuse the publication *before* replacing any document — including the
    documents of the domains that were fine — and report the L1 projection as
    pending rather than partly done.
    """
    published = _write(config, "claim_ops", "ops", "Runbook", BODY_OPS)
    assert published.status == GRAPH_COMMIT_COMMITTED
    before = _domain_file(config, "ops").read_bytes()

    body = f"Neutral text.\n\n{managed_body_markers('Index maintenance')[0]}\n\nMore."
    request = _request("claim_legacy", body, offset=5, request_key="claim_legacy")
    binding = ProjectionBinding(
        domain="retrieval", section="Index maintenance", body=body
    )
    with _journal(config) as journal:
        journal.append(
            "older-version:claim_legacy",
            _bind_events(plan_semantic_write(request).events, "claim_legacy", binding),
            kind=REQUEST_KIND_SEMANTIC_WRITE,
        )

    recovery = recover_projections(config)

    assert recovery.status == RECOVERY_INCOMPLETE
    assert PROJECTION_L1 in recovery.projections_pending
    assert any(
        graph_projection.LAYOUT_MISMATCH in error for error in recovery.errors
    )
    assert not _domain_file(config, "retrieval").exists()
    assert _domain_file(config, "ops").read_bytes() == before
    assert binding.entry_id not in _registry(config)

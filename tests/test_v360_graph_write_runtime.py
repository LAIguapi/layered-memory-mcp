"""v3.6.0 Batch I-D — the one explicit semantic-write seam on the MCP write path.

Why this file exists: ARCHITECTURE_v2.md §12 Batch I-D wires
``run_semantic_graph_write`` into ``inject_knowledge_tool`` — the section-level
MCP write entry point — without turning ordinary Markdown into inferred graph
semantics. That distinction is the whole batch, so these tests pin it directly:

  * **No request, no graph.** A call shaped exactly like today's reaches no
    graph operation and gets back exactly today's response, with no ``graph``
    key. Explicit ``legacy`` remains the no-graph compatibility mode.
  * **The decision arrives from the caller, validated.** A graph write happens
    only because the caller handed over a serialized
    :class:`~layered_memory_mcp.graph_write.SemanticWriteRequest`. Nothing —
    not the domain, the section, the content, the filename or a hash — is
    turned into a node, an entity, a claim, a supersession or a target ID.
  * **An invalid request is a request error.** It is refused before any graph
    operation and before the L1 write, never silently repaired, and the
    refusal echoes field locations rather than the caller's content.
  * **The mode is the only switch.** ``legacy`` reaches no store operation even
    with a request and a store; ``shadow`` plans and never rebuilds.
  * **A destructive projection is refused, not attempted.**
    :meth:`~layered_memory_mcp.graph_store.GraphStore.rebuild` is a *full
    replacement* of graph state from an event stream — "nothing outside
    ``source_events`` survives" — so a write mode may only commit when it holds
    the *complete* stream.

    v3.6.1 INTENTIONAL CONTRACT CHANGE. That complete stream now exists: the
    persistent semantic journal (``graph_journal``), declared by
    ``graph_journal_path``. So a ``graph_write``/``graph`` mode with a journal
    configured COMMITS through ``graph_application.commit_semantic_write``
    (covered by ``tests/test_v361_graph_application.py`` and
    ``tests/test_v361_graph_entrances.py``), and a write mode with **no**
    journal declared returns a bounded ``graph_write_not_configured`` refusal
    and mutates nothing at all — not the store, and no longer the L1 file
    either, because a write mode does not fall back to the legacy write path.
    The configurations in *this* file deliberately declare no journal, so every
    test below still pins the refusal half of that contract: no store
    operation, no L1 write, no leak, and a prepopulated graph left untouched.
  * **The two write paths stay separate.** The L1/changelog outcome is
    authoritative and reported as it happened; a graph failure never rolls it
    back, never claims to have rolled it back, and never becomes a claimed
    commit. A legacy failure or a deferral is never projected beside.
  * **The block is additive and bounded.** Existing response fields keep their
    values; the ``graph`` block carries IDs, counts, statuses and warning
    labels only — no node body, no Markdown, no host path, no backend
    diagnostic, no exception message.

Every test builds its own ``MemoryConfig`` against ``tmp_path`` with an
explicit home and knowledge dir, injects an in-memory graph store, and uses
neutral technical fixture vocabulary (a retrieval index and its rebuild
cadence). Nothing reads or writes real memory data, the real config.yaml or any
host path; conftest's sandbox fixtures are the backstop.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from layered_memory_mcp import server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_application import (
    GRAPH_COMMIT_DECISION_REQUIRED,
    GRAPH_COMMIT_NOT_CONFIGURED,
    commit_payload,
)
from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
from layered_memory_mcp.graph_models import (
    GraphNode,
    GraphScope,
    MemoryClass,
    MemoryEvent,
    MemoryEventKind,
    NodeKind,
    RelationType,
    SourceRef,
)
from layered_memory_mcp.graph_runtime import (
    GRAPH_WRITE_FAILED,
    GRAPH_WRITE_NO_REQUEST,
    GRAPH_WRITE_NOT_CONFIGURED,
    GRAPH_WRITE_NOT_ENABLED,
    GRAPH_WRITE_PREPARED,
    GRAPH_WRITE_PROJECTED,
)
from layered_memory_mcp.graph_store import RebuildReport
from layered_memory_mcp.graph_write import RelationAssertion, SemanticWriteRequest
from layered_memory_mcp.memory_changelog import changelog_settings

# ---------------------------------------------------------------------------
# Neutral fixtures
# ---------------------------------------------------------------------------

NAMESPACE = "retrieval_fixture"
EPISODE_ID = "episode_build_0101"
NEW_CLAIM_ID = "claim_index_rebuild_nightly"
OLD_CLAIM_ID = "claim_index_rebuild_weekly"

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)

#: The L1 write the tool has always done: one domain, one section, one body.
DOMAIN = "retrieval"
SECTION = "Index maintenance"
CONTENT = (
    "The retrieval index rebuild runs nightly at 02:00 UTC. A rebuild reads "
    "every section once and replaces the domain's vectors in one pass."
)

#: The graph node body. Deliberately different text from ``CONTENT`` so a
#: payload leak of either one is unambiguous.
CLAIM_BODY = "The retrieval index rebuild cadence is nightly."


def _scope() -> GraphScope:
    return GraphScope(namespace=NAMESPACE)


def _source_ref() -> SourceRef:
    return SourceRef(episode_id=EPISODE_ID)


def _new_claim() -> GraphNode:
    return GraphNode(
        id=NEW_CLAIM_ID,
        kind=NodeKind.CLAIM,
        label="Retrieval index rebuild cadence",
        content=CLAIM_BODY,
        memory_class=MemoryClass.FACT,
        scope=_scope(),
        recorded_at=T1,
        source_refs=[_source_ref()],
    )


def _old_claim() -> GraphNode:
    """A claim a *previous* write already projected into the graph."""
    return GraphNode(
        id=OLD_CLAIM_ID,
        kind=NodeKind.CLAIM,
        label="Retrieval index rebuild cadence",
        content="The retrieval index rebuild cadence is weekly.",
        memory_class=MemoryClass.FACT,
        scope=_scope(),
        recorded_at=T0,
        source_refs=[_source_ref()],
    )


def _prior_event() -> MemoryEvent:
    """The one event a prior, properly-sourced projection left in the store."""
    return MemoryEvent(
        id="event_prior_weekly_cadence",
        kind=MemoryEventKind.INJECT,
        occurred_at=T0,
        scope=_scope(),
        node=_old_claim(),
    )


def _request(**kwargs) -> SemanticWriteRequest:
    data = {"node": _new_claim(), "occurred_at": T1}
    data.update(kwargs)
    return SemanticWriteRequest(**data)


def _request_payload(**kwargs) -> dict:
    """The transport shape: the model's own JSON serialization, nothing else."""
    return _request(**kwargs).to_dict()


def _supersedes() -> RelationAssertion:
    return RelationAssertion(
        relation_type=RelationType.SUPERSEDES,
        target_id=OLD_CLAIM_ID,
        evidence_refs=[_source_ref()],
    )


def _config(tmp_path: Path, mode: str, name: str = "home", **kwargs) -> MemoryConfig:
    """An explicit test config: tmp home, tmp knowledge dir, chosen mode."""
    home = tmp_path / f"{name}-{mode}"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(home / "knowledge"),
        graph_mode=mode,
        **kwargs,
    )


class _RecordingStore:
    """Records every store operation, delegating to the in-memory fixture store."""

    def __init__(self):
        self.inner = InMemoryGraphStore()
        self.calls: list[str] = []

    def __getattr__(self, name):
        inner_attr = getattr(self.inner, name)
        if not callable(inner_attr):
            return inner_attr

        def _recorded(*args, **kwargs):
            self.calls.append(name)
            return inner_attr(*args, **kwargs)

        return _recorded


class _RaisingRebuildStore:
    """A backend whose rebuild fails with a diagnostic naming a host path."""

    def __init__(self):
        self.calls: list[str] = []

    def rebuild(self, *args, **kwargs):
        self.calls.append("rebuild")
        raise RuntimeError("backend diagnostic naming /tmp/not-a-real-path/graph.db")


class _ReportingErrorsStore:
    """A rebuild that completes and reports problems — not a clean commit."""

    def __init__(self):
        self.calls: list[str] = []

    def rebuild(self, source_events, *args, **kwargs):
        self.calls.append("rebuild")
        return RebuildReport(
            events_read=len(source_events),
            errors=["dangling_reference:claim_index_rebuild_unknown"],
        )


class _NotAStore:
    """An injected object with no write surface at all."""


def _knowledge_files(config: MemoryConfig) -> list[str]:
    return sorted(p.name for p in Path(config.knowledge_dir).glob("*.md"))


def _changelog_events(config: MemoryConfig) -> list[dict]:
    path = Path(changelog_settings(config)["path"])
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _inject(monkeypatch, config, store=None, **kwargs) -> dict:
    """Call the real MCP tool with an explicit config and injected store."""
    from layered_memory_mcp import server

    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "_graph_store", store)

    call = {"domain": DOMAIN, "section": SECTION, "content": CONTENT}
    call.update(kwargs)
    return json.loads(asyncio.run(server.inject_knowledge_tool(**call)))


# ---------------------------------------------------------------------------
# The API shape: one optional argument, every old argument untouched
# ---------------------------------------------------------------------------

def test_the_tool_grows_exactly_one_optional_argument():
    from layered_memory_mcp import server

    parameters = list(
        inspect.signature(server.inject_knowledge_tool).parameters.values()
    )
    names = [p.name for p in parameters]

    assert names[:8] == [
        "domain",
        "section",
        "content",
        "mode",
        "agent_id",
        "fuse",
        "expected_hash",
        "routing_override",
    ]
    assert names[8:] == [server.SEMANTIC_WRITE_REQUEST_ARG]
    assert parameters[8].default is None


def test_the_new_argument_is_exposed_as_an_optional_object_in_the_mcp_schema():
    """The transport shape is the model's own JSON serialization, not a new schema."""
    from layered_memory_mcp import server

    tools = asyncio.run(server.mcp._list_tools())
    schema = next(t for t in tools if t.name == "inject_knowledge_tool").parameters

    field = schema["properties"][server.SEMANTIC_WRITE_REQUEST_ARG]
    assert field["default"] is None
    assert {"type": "object"} in [
        {k: v for k, v in option.items() if k == "type"}
        for option in field["anyOf"]
    ]
    assert server.SEMANTIC_WRITE_REQUEST_ARG not in schema["required"]


# ---------------------------------------------------------------------------
# No request, no graph (the normal legacy call)
# ---------------------------------------------------------------------------

def test_a_legacy_call_is_unchanged_and_reaches_no_graph_operation(
    monkeypatch, tmp_path
):
    store = _RecordingStore()
    config = _config(tmp_path, "legacy")

    response = _inject(monkeypatch, config, store=store)

    assert response["success"] is True
    assert response["action"] in ("created", "section_created", "appended")
    assert "graph" not in response
    assert store.calls == []
    assert _knowledge_files(config) == ["retrieval.md"]


def test_passing_the_argument_as_none_is_the_same_call_as_omitting_it(
    monkeypatch, tmp_path
):
    from layered_memory_mcp import server

    omitted = _inject(monkeypatch, _config(tmp_path, "legacy", name="omitted"))
    explicit = _inject(
        monkeypatch,
        _config(tmp_path, "legacy", name="explicit"),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: None},
    )

    assert list(omitted) == list(explicit)
    assert "graph" not in omitted and "graph" not in explicit
    assert omitted["action"] == explicit["action"]


def test_no_request_reaches_no_graph_runtime_in_legacy_mode(monkeypatch, tmp_path):
    """Legacy with no request must be indistinguishable from pre-I-D."""
    from layered_memory_mcp import graph_runtime, graph_write

    def _refuse(*args, **kwargs):
        raise AssertionError("a legacy call without a request must not reach the graph")

    monkeypatch.setattr(graph_runtime, "run_semantic_graph_write", _refuse)
    monkeypatch.setattr(graph_runtime, "graph_write_payload", _refuse)
    monkeypatch.setattr(graph_write, "plan_semantic_write", _refuse)
    monkeypatch.setattr(graph_write, "project_semantic_write", _refuse)

    response = _inject(monkeypatch, _config(tmp_path, "legacy"), store=_RecordingStore())

    assert response["success"] is True
    assert "graph" not in response


def test_a_graph_mode_without_a_request_refuses_before_any_write(
    monkeypatch, tmp_path
):
    """A successful L1 write is not a semantic decision (§3.4).

    v3.6.1: in a write mode that is now a *refusal* rather than an L1 write
    with a ``no_request`` note beside it. The authority owns the write path, so
    an undecided call cannot be allowed to mutate L1 and then report that the
    graph side was skipped — it fails closed, before any effect, naming the
    argument that was missing.
    """
    store = _RecordingStore()
    config = _config(tmp_path, "graph_write")
    response = _inject(monkeypatch, config, store=store)

    assert response["success"] is False
    assert response["error"] == GRAPH_COMMIT_DECISION_REQUIRED
    assert server.SEMANTIC_WRITE_REQUEST_ARG in response["supported_alternative"]
    assert store.calls == []
    assert _knowledge_files(config) == []


# ---------------------------------------------------------------------------
# The mode matrix
# ---------------------------------------------------------------------------

def test_legacy_mode_with_a_request_and_a_store_reaches_no_graph_operation(
    monkeypatch, tmp_path
):
    from layered_memory_mcp import server

    store = _RecordingStore()
    config = _config(tmp_path, "legacy")

    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["success"] is True
    assert store.calls == []
    assert response["graph"]["mode"] == "legacy"
    assert response["graph"]["status"] == GRAPH_WRITE_NOT_ENABLED
    assert response["graph"]["committed"] is False
    # The L1 write is still the authoritative one, and it happened.
    assert _knowledge_files(config) == ["retrieval.md"]


def test_shadow_mode_plans_the_decision_without_rebuilding_anything(
    monkeypatch, tmp_path
):
    from layered_memory_mcp import server

    store = _RecordingStore()
    response = _inject(
        monkeypatch,
        _config(tmp_path, "shadow"),
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    block = response["graph"]
    assert response["success"] is True
    assert block["status"] == GRAPH_WRITE_PREPARED
    assert block["committed"] is False
    assert block["node_id"] == NEW_CLAIM_ID
    assert block["event_ids"]
    assert "rebuild" not in store.calls
    assert store.calls == []


def test_graph_write_mode_without_a_configured_journal_refuses_before_any_write(
    monkeypatch, tmp_path
):
    """No declared authority, no write — and no legacy fallback either.

    v3.6.1: the refusal moved from "this seam holds no event stream" to "this
    deployment declared no journal", and it got stricter: the L1 file is not
    written either. A write mode that mutated L1 and then refused the graph
    half would leave readable state no decision produced.
    """
    store = _RecordingStore()
    config = _config(tmp_path, "graph_write")

    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    block = response["graph"]
    assert response["success"] is False
    assert block["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert block["committed"] is False
    assert block["errors"] == ["graph_journal_not_configured"]
    assert store.calls == []
    assert store.inner.get_node(NEW_CLAIM_ID) is None
    assert _knowledge_files(config) == []


def test_the_full_graph_mode_also_refuses_rather_than_rebuilding(monkeypatch, tmp_path):
    store = _RecordingStore()
    config = _config(tmp_path, "graph")
    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["graph"]["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert response["graph"]["committed"] is False
    assert store.calls == []
    assert _knowledge_files(config) == []


def test_a_prepopulated_graph_store_is_neither_erased_nor_rewritten(
    monkeypatch, tmp_path
):
    """The regression this refusal exists for.

    A store already holding a previous write's node must come out of this call
    byte-identical. A projection carrying only *this* request's events would
    have replaced that node with the new one — silent destruction of graph
    state, reported as a successful commit. (With a journal configured the
    same property is kept by projecting the *complete* journal stream; see
    ``tests/test_v361_graph_application.py``.)
    """
    store = _RecordingStore()
    store.inner.rebuild([_prior_event()])
    before = store.inner.snapshot()
    store.calls.clear()

    config = _config(tmp_path, "graph_write")
    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    # Checked first and on their own: these hold whatever the status says.
    assert store.calls == []
    assert store.inner.snapshot() == before
    assert store.inner.get_node(OLD_CLAIM_ID) is not None
    assert store.inner.get_node(NEW_CLAIM_ID) is None

    assert response["success"] is False
    assert response["graph"]["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert response["graph"]["committed"] is False
    assert _knowledge_files(config) == []


def test_a_write_enabled_mode_never_reaches_the_projection_seam(monkeypatch, tmp_path):
    """The refusal is upstream of the runtime: the seam is not called at all."""
    from layered_memory_mcp import graph_runtime, graph_write

    def _refuse(*args, **kwargs):
        raise AssertionError(
            "a write-enabled mode with no configured authority must not reach "
            "the projection seam"
        )

    monkeypatch.setattr(graph_runtime, "run_semantic_graph_write", _refuse)
    monkeypatch.setattr(graph_write, "project_semantic_write", _refuse)
    monkeypatch.setattr(graph_write, "plan_semantic_write", _refuse)

    for mode in ("graph_write", "graph"):
        response = _inject(
            monkeypatch,
            _config(tmp_path, mode, name="unreached"),
            store=_RecordingStore(),
            **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
        )
        assert response["graph"]["status"] == GRAPH_COMMIT_NOT_CONFIGURED


def test_the_refusal_status_is_distinct_from_every_runtime_write_status():
    """A refusal must not read like a commit, a plan, or a missing backend."""
    from layered_memory_mcp import server

    assert server.GRAPH_WRITE_SOURCE_EVENTS_REQUIRED not in {
        GRAPH_WRITE_FAILED,
        GRAPH_WRITE_NO_REQUEST,
        GRAPH_WRITE_NOT_CONFIGURED,
        GRAPH_WRITE_NOT_ENABLED,
        GRAPH_WRITE_PREPARED,
        GRAPH_WRITE_PROJECTED,
    }


def test_a_read_only_graph_mode_never_projects_a_write(monkeypatch, tmp_path):
    from layered_memory_mcp import server

    store = _RecordingStore()
    response = _inject(
        monkeypatch,
        _config(tmp_path, "graph_recall"),
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["graph"]["status"] == GRAPH_WRITE_NOT_ENABLED
    assert response["graph"]["committed"] is False
    assert store.calls == []


def test_an_enabled_mode_without_an_injected_store_also_refuses_safely(
    monkeypatch, tmp_path
):
    """With or without a store, an undeclared authority is the first refusal.

    Both halves are named: with neither declared the refusal lists both, which
    is what an operator needs to fix it in one pass rather than two.
    """
    config = _config(tmp_path, "graph_write")
    response = _inject(
        monkeypatch,
        config,
        store=None,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["success"] is False
    assert response["graph"]["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert response["graph"]["committed"] is False
    assert response["graph"]["errors"] == [
        "graph_journal_not_configured",
        "graph_store_not_configured",
    ]
    assert _knowledge_files(config) == []


# ---------------------------------------------------------------------------
# Nothing is inferred from the Markdown write
# ---------------------------------------------------------------------------

def test_the_markdown_write_projects_no_node_of_its_own(monkeypatch, tmp_path):
    """The domain, the section and the body produce no graph state whatsoever.

    Not even when a request is present and the mode is enabled: the refusal
    leaves the store empty, so there is nothing that *could* have been derived.
    """
    from layered_memory_mcp import server

    store = _RecordingStore()
    _inject(
        monkeypatch,
        _config(tmp_path, "graph_write"),
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert store.inner.get_node(NEW_CLAIM_ID) is None
    snapshot = store.inner.snapshot()
    assert snapshot["nodes"] == []
    assert snapshot["edges"] == []


def test_a_lifecycle_target_is_not_invented_from_the_existing_section(
    monkeypatch, tmp_path
):
    """A supersession target is never derived from the file the write touched.

    With no declared authority the write mode refuses outright. In ``shadow`` —
    which mutates nothing — the plan still runs and reports its own honest
    failure, rather than a dangling edge or a target guessed from the section.
    """
    store = _RecordingStore()
    config = _config(tmp_path, "graph_write")
    payload = _request_payload(supersedes=[_supersedes()])

    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: payload},
    )

    block = response["graph"]
    assert response["success"] is False
    assert block["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert block["committed"] is False
    assert block["superseded_ids"] == []
    assert store.calls == []
    assert _knowledge_files(config) == []

    shadow_store = _RecordingStore()
    shadow = _inject(
        monkeypatch,
        _config(tmp_path, "shadow", name="lifecycle"),
        store=shadow_store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: payload},
    )

    assert shadow["success"] is True
    assert shadow["graph"]["status"] == GRAPH_WRITE_FAILED
    assert shadow["graph"]["committed"] is False
    assert "graph_write_runtime_failed:ValueError" in shadow["graph"]["warnings"]
    assert shadow_store.calls == []


# ---------------------------------------------------------------------------
# Validation: an invalid request never reaches a graph operation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"node": {"id": "claim_x"}, "occurred_at": "not-a-timestamp"},
        {"node": {"id": "claim_x", "kind": "claim", "label": "L"}},
        {"occurred_at": "2026-02-01T09:00:00+00:00"},
    ],
)
def test_an_invalid_request_is_a_request_error_before_anything_is_written(
    monkeypatch, tmp_path, bad
):
    from layered_memory_mcp import server

    store = _RecordingStore()
    config = _config(tmp_path, "graph_write")

    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: bad},
    )

    assert response["success"] is False
    assert response["error"] == server.SEMANTIC_WRITE_REQUEST_ERROR
    assert response[f"{server.SEMANTIC_WRITE_REQUEST_ARG}_errors"]
    assert "graph" not in response
    assert store.calls == []
    # Refused as a request error: no L1 file, no section, no changelog event.
    assert _knowledge_files(config) == []
    assert _changelog_events(config) == []


@pytest.mark.parametrize(
    "bad", ["{}", '{"node": {}}', [], 7, True, ["node"]]
)
def test_a_request_that_is_not_an_object_is_refused_rather_than_parsed(
    monkeypatch, tmp_path, bad
):
    """A JSON string is not quietly re-parsed, and a list is not a request."""
    from layered_memory_mcp import server

    store = _RecordingStore()
    config = _config(tmp_path, "graph_write")

    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: bad},
    )

    assert response["success"] is False
    assert response["error"] == server.SEMANTIC_WRITE_REQUEST_ERROR
    assert store.calls == []
    assert _knowledge_files(config) == []


def test_a_semantically_invalid_decision_is_refused_not_repaired(
    monkeypatch, tmp_path
):
    """A self-referential supersession is a caller mistake, never rewritten."""
    from layered_memory_mcp import server

    payload = _request_payload()
    payload["supersedes"] = [
        {
            "relation_type": RelationType.SUPERSEDES.value,
            "target_id": NEW_CLAIM_ID,
            "evidence_refs": [_source_ref().to_dict()],
        }
    ]

    store = _RecordingStore()
    config = _config(tmp_path, "graph_write")
    response = _inject(
        monkeypatch, config, store=store, **{server.SEMANTIC_WRITE_REQUEST_ARG: payload}
    )

    assert response["success"] is False
    assert response["error"] == server.SEMANTIC_WRITE_REQUEST_ERROR
    assert store.calls == []
    assert _knowledge_files(config) == []


def test_the_refusal_echoes_field_locations_not_the_callers_content(
    monkeypatch, tmp_path
):
    from layered_memory_mcp import server

    payload = _request_payload()
    payload["occurred_at"] = "not-a-timestamp"

    response = _inject(
        monkeypatch,
        _config(tmp_path, "graph_write"),
        store=_RecordingStore(),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: payload},
    )

    blob = json.dumps(response, ensure_ascii=False)
    assert "occurred_at" in blob
    assert CLAIM_BODY not in blob
    assert CONTENT not in blob
    assert str(tmp_path) not in blob


def test_an_invalid_request_is_refused_in_legacy_mode_too(monkeypatch, tmp_path):
    """Validation is a request contract, not something a mode turns off."""
    from layered_memory_mcp import server

    config = _config(tmp_path, "legacy")
    response = _inject(
        monkeypatch,
        config,
        store=_RecordingStore(),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: {"node": {"id": "claim_x"}}},
    )

    assert response["success"] is False
    assert response["error"] == server.SEMANTIC_WRITE_REQUEST_ERROR
    assert _knowledge_files(config) == []


# ---------------------------------------------------------------------------
# A legacy failure or deferral is never projected beside
# ---------------------------------------------------------------------------

def test_a_legacy_request_error_is_never_projected(monkeypatch, tmp_path):
    """Exercised in ``shadow``, the mode where a legacy write still happens.

    v3.6.1: ``graph_write``/``graph`` no longer run the legacy write path at
    all, so "a failed legacy write is never projected beside" is now a property
    of ``shadow`` — the one remaining mode where both paths run. The write-mode
    half of the same property is asserted below: nothing is written anywhere.
    """
    store = _RecordingStore()
    config = _config(tmp_path, "shadow", name="legacy-error")

    response = _inject(
        monkeypatch,
        config,
        store=store,
        content="   ",
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["success"] is False
    assert response["graph"]["status"] == server.GRAPH_WRITE_LEGACY_FAILED
    assert response["graph"]["legacy_write_ok"] is False
    assert response["graph"]["committed"] is False
    assert store.calls == []
    assert _knowledge_files(config) == []

    write_config = _config(tmp_path, "graph_write", name="legacy-error")
    refused = _inject(
        monkeypatch,
        write_config,
        store=store,
        content="   ",
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )
    assert refused["success"] is False
    assert store.calls == []
    assert _knowledge_files(write_config) == []


def test_a_legacy_refusal_is_never_projected(monkeypatch, tmp_path):
    """The routing gate refuses before the write; the graph must not run after.

    In ``shadow``, where the legacy write path still runs (v3.6.1).
    """
    from layered_memory_mcp.injector import ROUTING_REFUSED_ACTION

    def _refused(**kwargs):
        return {
            "success": False,
            "action": ROUTING_REFUSED_ACTION,
            "error": "routing_enforce_direct_write_refused",
        }

    monkeypatch.setattr(server, "inject_knowledge", _refused)

    store = _RecordingStore()
    response = _inject(
        monkeypatch,
        _config(tmp_path, "shadow", name="routing-refusal"),
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["success"] is False
    assert response["action"] == ROUTING_REFUSED_ACTION
    assert response["graph"]["status"] == server.GRAPH_WRITE_LEGACY_FAILED
    assert response["graph"]["committed"] is False
    assert store.calls == []


def test_a_deferred_fusion_handshake_is_never_projected(monkeypatch, tmp_path):
    """Nothing was written yet, so there is no L1 state for a graph node to mirror.

    In ``shadow``, where the legacy handshake still runs (v3.6.1).
    """
    def _deferred(**kwargs):
        return {
            "success": True,
            "action": "deferred_fusion",
            "needs_fusion": True,
            "file": "retrieval.md",
            "expected_hash": "0" * 16,
        }

    monkeypatch.setattr(server, "inject_knowledge", _deferred)

    store = _RecordingStore()
    response = _inject(
        monkeypatch,
        _config(tmp_path, "shadow", name="deferred"),
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["success"] is True
    assert response["action"] == "deferred_fusion"
    assert response["graph"]["status"] == server.GRAPH_WRITE_LEGACY_NOT_COMMITTED
    assert response["graph"]["committed"] is False
    assert store.calls == []


@pytest.mark.parametrize(
    "action",
    [
        "deferred_consolidate",
        "skipped",
        "merged_no_change",
        "append_no_change",
    ],
)
def test_a_successful_write_that_changed_nothing_is_never_projected(
    monkeypatch, tmp_path, action
):
    """Every legacy action that succeeds without writing L1 content.

    In ``shadow``, where the legacy write path still runs (v3.6.1).
    """
    def _skipped(**kwargs):
        return {"success": True, "action": action, "file": "retrieval.md"}

    monkeypatch.setattr(server, "inject_knowledge", _skipped)

    store = _RecordingStore()
    response = _inject(
        monkeypatch,
        _config(tmp_path, "shadow", name="no-change"),
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["graph"]["status"] == server.GRAPH_WRITE_LEGACY_NOT_COMMITTED
    assert store.calls == []


# ---------------------------------------------------------------------------
# A graph failure never touches the legacy write
# ---------------------------------------------------------------------------

def test_a_stores_rebuild_is_never_called_so_it_cannot_fail_or_leak(
    monkeypatch, tmp_path
):
    """The refused projection means even a hostile backend is never consulted."""
    store = _RaisingRebuildStore()
    config = _config(tmp_path, "graph_write")

    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    block = response["graph"]
    assert response["success"] is False
    assert _knowledge_files(config) == []
    assert block["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert block["committed"] is False
    assert store.calls == []

    blob = json.dumps(block, ensure_ascii=False)
    assert "/tmp/not-a-real-path" not in blob
    assert "backend diagnostic" not in blob
    assert "rollback" not in blob
    assert "RuntimeError" not in blob


def test_a_rebuild_that_would_report_errors_is_never_reached(monkeypatch, tmp_path):
    from layered_memory_mcp import server

    store = _ReportingErrorsStore()
    config = _config(tmp_path, "graph_write")
    response = _inject(
        monkeypatch,
        config,
        store=store,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    block = response["graph"]
    assert block["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert block["committed"] is False
    assert store.calls == []
    assert _knowledge_files(config) == []


def test_a_store_without_a_write_surface_is_refused_not_raised(
    monkeypatch, tmp_path
):
    """An injected handle that cannot answer the contract is named, not raised.

    v3.6.1: with a journal declared the commit path really does reach the
    store, so this is the case that proves an incomplete handle produces a
    bounded refusal instead of an ``AttributeError`` escaping a write path.
    """
    config = _config(
        tmp_path,
        "graph_write",
        name="no-surface",
        graph_journal_path=str(tmp_path / "no-surface-journal.db"),
    )
    response = _inject(
        monkeypatch,
        config,
        store=_NotAStore(),
        # The node must *be* the published body for the binding to validate, so
        # the refusal under test is the store's and not the binding's.
        content=CLAIM_BODY,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    block = response["graph"]
    assert response["success"] is False
    assert _knowledge_files(config) == []
    assert block["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert block["committed"] is False
    assert any(
        error.startswith("graph_store_incomplete:") for error in block["errors"]
    )


# ---------------------------------------------------------------------------
# The block is additive, bounded and not a second ledger
# ---------------------------------------------------------------------------

def test_the_graph_block_is_additive_only_in_the_observing_modes(
    monkeypatch, tmp_path
):
    """``shadow`` still answers the legacy response plus a block, unchanged.

    v3.6.1: ``graph_write``/``graph`` deliberately no longer do. That path is
    the authority's own response — ``success``/``action``/``domain``/``section``
    plus the bounded commit block — because the legacy write did not happen and
    reporting its fields would describe a write nobody performed. The
    write-mode shape is pinned by the next test.
    """
    legacy = _inject(monkeypatch, _config(tmp_path, "legacy", name="additive"))
    enabled = _inject(
        monkeypatch,
        _config(tmp_path, "shadow", name="additive"),
        store=_RecordingStore(),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert [k for k in enabled if k != "graph"] == list(legacy)
    assert enabled["action"] == legacy["action"]
    assert enabled["success"] == legacy["success"]
    assert enabled["section"] == legacy["section"]


def test_the_graph_block_carries_no_memory_body_markdown_or_host_path(
    monkeypatch, tmp_path
):
    from layered_memory_mcp import server

    config = _config(tmp_path, "graph")
    response = _inject(
        monkeypatch,
        config,
        store=_RecordingStore(),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    blob = json.dumps(response["graph"], ensure_ascii=False)
    assert CLAIM_BODY not in blob
    assert CONTENT not in blob
    assert SECTION not in blob
    assert DOMAIN not in blob
    assert str(tmp_path) not in blob
    assert str(config.knowledge_dir) not in blob


def test_the_graph_block_keys_are_the_bounded_payload_keys(monkeypatch, tmp_path):
    """Both blocks are the fixed, bounded key sets their own payload builders define."""
    from layered_memory_mcp.graph_application import SemanticCommit
    from layered_memory_mcp.graph_runtime import GraphWriteOutcome, graph_write_payload

    shadow = _inject(
        monkeypatch,
        _config(tmp_path, "shadow", name="bounded"),
        store=_RecordingStore(),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )
    assert set(shadow["graph"]) == set(
        graph_write_payload(GraphWriteOutcome(mode="shadow"))
    )

    write = _inject(
        monkeypatch,
        _config(tmp_path, "graph_write", name="bounded"),
        store=_RecordingStore(),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )
    assert set(write["graph"]) == set(
        commit_payload(SemanticCommit(status=GRAPH_COMMIT_NOT_CONFIGURED))
    )


def test_a_refused_write_mode_call_creates_no_artifact_at_all(monkeypatch, tmp_path):
    """A refusal leaves the data directory exactly as it was.

    v3.6.1 replaces the old "MEMORY_CHANGELOG stays the one event stream"
    assertion with the honest statement of the new design, which this file's
    unconfigured write mode can actually check: a refused write mode creates
    nothing — no changelog entry, no vector database, no journal, no lock file.
    The complementary half (a *committed* write writes its decision to the
    journal and leaves MEMORY_CHANGELOG alone, because the two are different
    streams — immutable file-write evidence vs. explicit semantic decisions) is
    pinned in ``tests/test_v361_graph_application.py`` and
    ``tests/test_v361_graph_entrances.py``.
    """
    config = _config(tmp_path, "graph_write")
    before = sorted(p.name for p in (Path(config.home) / "data").glob("*"))

    response = _inject(
        monkeypatch,
        config,
        store=_RecordingStore(),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["graph"]["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert _changelog_events(config) == []
    after = sorted(p.name for p in (Path(config.home) / "data").glob("*"))
    assert after == before


def test_the_legacy_write_path_still_records_its_own_changelog_in_shadow(
    monkeypatch, tmp_path
):
    """The retained half of the old assertion, in the mode that still writes.

    MEMORY_CHANGELOG remains the immutable evidence stream for file writes, and
    the graph adds nothing to it: in ``shadow`` the legacy write happens, it
    records exactly one event, and the only artifacts are the legacy ones.
    """
    config = _config(tmp_path, "shadow", name="one-ledger")
    before = sorted(p.name for p in (Path(config.home) / "data").glob("*"))

    _inject(
        monkeypatch,
        config,
        store=_RecordingStore(),
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    events = _changelog_events(config)
    assert len(events) == 1
    assert events[0]["tool"] == "inject_knowledge"
    after = sorted(p.name for p in (Path(config.home) / "data").glob("*"))
    assert [name for name in after if name not in before] == [
        "memory_changelog.jsonl",
        "vectors.db",
    ]


def test_the_tool_constructs_no_graph_backend_of_its_own(monkeypatch, tmp_path):
    """A graph-enabled write with no injected store opens no database (§3.1).

    v3.6.1: with the journal undeclared the refusal happens before anything is
    opened, so the declared graph database is still never touched — the
    authority is checked first and a graph file is never materialised by a
    refused call.
    """
    import sqlite3

    real_connect = sqlite3.connect
    opened: list[str] = []

    def _watch(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _watch)

    config = _config(tmp_path, "graph", graph_db_path=str(tmp_path / "graph.db"))
    response = _inject(
        monkeypatch,
        config,
        store=None,
        **{server.SEMANTIC_WRITE_REQUEST_ARG: _request_payload()},
    )

    assert response["graph"]["status"] == GRAPH_COMMIT_NOT_CONFIGURED
    assert not [path for path in opened if "graph.db" in path]
    assert not (tmp_path / "graph.db").exists()

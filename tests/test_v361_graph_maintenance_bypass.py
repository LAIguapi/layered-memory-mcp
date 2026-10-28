"""v3.6.1 — unattended maintenance must not perform a refused mutation.

Why this file exists: ARCHITECTURE_v2.md "Framework completion and migration
delivery contract" — "Audit injection, batch injection, whole-file CRUD,
review, cleanup/consolidation, REST CRUD, maintenance and task mutations for
bypasses."

``tests/test_v361_graph_entrances.py`` pins the *caller-facing* half: in a
graph write mode ``compact_memory``, ``reindex_vector_store_tool``,
``sync_l0_index_tool`` and ``init_framework`` refuse before any effect. This
file pins the other half — the same mutations running with nobody asking:

  * the ``get_l0_index`` ride-alongs (agent-memory compaction, deployed-guard
    refresh), which are the only writes a read tool performs;
  * the HTTP maintenance tick (legacy compaction, vector rebuild, guard
    refresh), which a long-lived daemon runs on a timer;
  * the watcher's L0 regeneration, which republishes the index from whatever
    is on disk rather than from the journal.

Each is suspended through the one shared table,
``graph_mutation_guard.MAINTENANCE_JOBS``, and each suspension is *reported* —
a tick that did nothing must not read as a tick that succeeded. The watcher's
cache invalidation is not a publication and keeps working.

Negative controls run everywhere: the same stubs in ``legacy``, ``shadow`` and
``graph_recall`` must still be called, so a test passing in a graph mode is
evidence of suppression rather than of a broken fixture.

Isolation: tmp_path homes, neutral content, no network, no user data, no real
embedding model — the jobs under test are stubbed precisely so that a call
would be recorded instead of performed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from layered_memory_mcp import (
    agent_integrator,
    injector,
    l0_manager,
    maintenance,
    memory_compactor,
    server,
    write_guard,
)
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.graph_mutation_guard import (
    GRAPH_MAINTENANCE_SUSPENDED,
    MAINTENANCE_JOBS,
    REFUSED_OPERATIONS,
    maintenance_job_suspension,
)
from layered_memory_mcp.watcher import KnowledgeWatcher

WRITE_MODES = ("graph_write", "graph")
UNCHANGED_MODES = ("legacy", "shadow", "graph_recall")

L0_CONTENT = "- retrieval: index maintenance -> retrieval.md#index-maintenance\n"


def _config(tmp_path: Path, mode: str, **kwargs) -> MemoryConfig:
    home = tmp_path / f"home-{mode}"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    graph_paths = {}
    if mode != "legacy":
        graph_paths = {
            "graph_db_path": str(home / "data" / "graph.db"),
            "graph_journal_path": str(home / "data" / "journal.db"),
        }
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(knowledge),
        graph_mode=mode,
        **graph_paths,
        **kwargs,
    )


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` with its bytes — the zero-side-effect probe."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_suspended(report: dict, job: str) -> None:
    """A suspension names itself, its job and what to call instead."""
    operation = MAINTENANCE_JOBS[job]
    reason, alternative = REFUSED_OPERATIONS[operation]
    assert report["status"] == GRAPH_MAINTENANCE_SUSPENDED
    assert report["job"] == job
    assert report["operation"] == operation
    assert report["reason"] == reason
    assert report["supported_alternative"] == alternative


# ---------------------------------------------------------------------------
# The shared policy itself
# ---------------------------------------------------------------------------

def test_every_background_job_names_a_guarded_entrance():
    """The two halves read one table, so they cannot drift apart."""
    assert set(MAINTENANCE_JOBS) == {
        "auto_maintain",
        "vector_maintenance",
        "write_guard_refresh",
        "watcher_l0_sync",
    }
    for job, operation in MAINTENANCE_JOBS.items():
        assert operation in REFUSED_OPERATIONS, job


def test_an_unknown_job_name_raises_instead_of_reading_as_allowed(tmp_path):
    config = _config(tmp_path, "graph_write")
    with pytest.raises(ValueError, match="not a known background maintenance job"):
        maintenance_job_suspension(config, "compact_everything")


@pytest.mark.parametrize("mode", WRITE_MODES)
@pytest.mark.parametrize("job", sorted(MAINTENANCE_JOBS))
def test_a_write_mode_suspends_every_job(tmp_path, mode, job):
    suspension = maintenance_job_suspension(_config(tmp_path, mode), job)
    assert suspension is not None
    _assert_suspended(suspension, job)
    assert suspension["graph_mode"] == mode


@pytest.mark.parametrize("mode", UNCHANGED_MODES)
@pytest.mark.parametrize("job", sorted(MAINTENANCE_JOBS))
def test_no_other_mode_suspends_anything(tmp_path, mode, job):
    assert maintenance_job_suspension(_config(tmp_path, mode), job) is None


# ---------------------------------------------------------------------------
# get_l0_index — the read tool that used to write
# ---------------------------------------------------------------------------

@pytest.fixture
def ride_along_calls(monkeypatch):
    """Record the two ride-alongs instead of letting them touch anything."""
    calls: list[str] = []

    def _auto_maintain(config, *args, **kwargs):
        calls.append("auto_maintain")
        return {"compact": {"status": "not_triggered"}}

    def _refresh(home, mode, state_path):
        calls.append("write_guard_refresh")
        return {"status": "refreshed"}

    monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", _auto_maintain)
    monkeypatch.setattr(write_guard, "refresh_deployed_plugin", _refresh)
    return calls


def _l0_config(tmp_path: Path, mode: str) -> MemoryConfig:
    config = _config(tmp_path, mode, write_guard="auto", auto_maintain=True)
    l0 = Path(config.home) / "l0_index.md"
    l0.write_text(L0_CONTENT, encoding="utf-8")
    config.l0_index_file = l0
    return config


def _get_l0_index(config, monkeypatch) -> dict:
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(
        server, "detect_agent_type", lambda: {"home_dir": str(Path(config.home) / "agent")}
    )
    return json.loads(asyncio.run(server.get_l0_index()))


@pytest.mark.parametrize("mode", WRITE_MODES)
def test_get_l0_index_runs_no_ride_along_in_a_write_mode(
    tmp_path, monkeypatch, ride_along_calls, mode
):
    config = _l0_config(tmp_path, mode)
    before = _tree(Path(config.home))

    result = _get_l0_index(config, monkeypatch)

    assert ride_along_calls == []
    assert _tree(Path(config.home)) == before
    # The read itself is untouched.
    assert result["success"] is True
    assert result["source"] == "l0_file"
    assert result["content"] == L0_CONTENT


@pytest.mark.parametrize("mode", UNCHANGED_MODES)
def test_get_l0_index_keeps_both_ride_alongs_everywhere_else(
    tmp_path, monkeypatch, ride_along_calls, mode
):
    """Negative control: the suppression above is the mode, not the stubs."""
    config = _l0_config(tmp_path, mode)

    result = _get_l0_index(config, monkeypatch)

    assert sorted(ride_along_calls) == ["auto_maintain", "write_guard_refresh"]
    assert result["content"] == L0_CONTENT


# ---------------------------------------------------------------------------
# The HTTP maintenance tick
# ---------------------------------------------------------------------------

@pytest.fixture
def maintenance_calls(monkeypatch):
    """Record each maintenance job; a vector rebuild fails the test outright."""
    calls: list[str] = []

    def _auto_maintain(config, *args, **kwargs):
        calls.append("auto_maintain")
        return {"compact": {"status": "not_triggered"}}

    def _refresh(home, mode, state_path):
        calls.append("write_guard_refresh")
        return {"status": "refreshed"}

    def _needs_reindex(config):
        calls.append("vector_inspect")
        # Stale once, then healthy — the shape the repair path expects.
        return "vector_rebuild" not in calls

    def _reindex(config, calibrate=False):
        calls.append("vector_rebuild")
        return {"success": True}

    monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", _auto_maintain)
    monkeypatch.setattr(write_guard, "refresh_deployed_plugin", _refresh)
    monkeypatch.setattr(injector, "vector_store_needs_reindex", _needs_reindex)
    monkeypatch.setattr(injector, "reindex_vector_store", _reindex)
    monkeypatch.setattr(
        agent_integrator, "detect_agent_type", lambda: {"home_dir": "agent-home"}
    )
    return calls


@pytest.mark.parametrize("mode", WRITE_MODES)
def test_the_maintenance_tick_suspends_every_job_in_a_write_mode(
    tmp_path, maintenance_calls, mode
):
    config = _config(tmp_path, mode, write_guard="auto")
    before = _tree(Path(config.home))

    report = maintenance.maintenance_task(config)

    assert maintenance_calls == []
    _assert_suspended(report["auto_maintain"], "auto_maintain")
    _assert_suspended(report["vector_maintenance"], "vector_maintenance")
    _assert_suspended(report["write_guard"], "write_guard_refresh")
    # Not even the retry bookkeeping is written: a suspended job left no trace.
    assert _tree(Path(config.home)) == before
    assert not (Path(config.home) / "data" / "vector_maintenance.json").exists()
    assert not (Path(config.home) / "data" / "vector_maintenance.lock").exists()


@pytest.mark.parametrize("mode", UNCHANGED_MODES)
def test_the_maintenance_tick_is_unchanged_everywhere_else(
    tmp_path, maintenance_calls, mode
):
    """Negative control: compaction, rebuild and guard refresh all still run."""
    config = _config(tmp_path, mode, write_guard="auto")

    report = maintenance.maintenance_task(config)

    assert "auto_maintain" in maintenance_calls
    assert "vector_rebuild" in maintenance_calls
    assert "write_guard_refresh" in maintenance_calls
    assert report["auto_maintain"] == {"compact": {"status": "not_triggered"}}
    assert report["vector_maintenance"]["status"] == "repaired"
    assert report["write_guard"] == {"status": "refreshed"}


@pytest.mark.parametrize("mode", WRITE_MODES)
def test_maintain_vectors_called_directly_is_suspended_too(
    tmp_path, maintenance_calls, mode
):
    """The guard sits in the job, so a direct caller cannot route around it."""
    config = _config(tmp_path, mode)
    before = _tree(Path(config.home))

    report = maintenance.maintain_vectors(config)

    _assert_suspended(report, "vector_maintenance")
    assert maintenance_calls == []
    assert _tree(Path(config.home)) == before


# ---------------------------------------------------------------------------
# auto_maintain_after_write — the choke point every ride-along shares
# ---------------------------------------------------------------------------

def _memory_file(config: MemoryConfig) -> Path:
    path = Path(config.home) / "AGENT_MEMORY.md"
    path.write_text("# Memory\n\n" + "- a recorded line\n" * 200, encoding="utf-8")
    return path


@pytest.mark.parametrize("mode", WRITE_MODES)
def test_auto_maintain_writes_nothing_in_a_write_mode(tmp_path, mode):
    config = _config(tmp_path, mode, auto_maintain=True, compact_bloat_threshold=0.0)
    memory = _memory_file(config)
    before = _tree(Path(config.home))

    report = memory_compactor.auto_maintain_after_write(config, memory_path=memory)

    assert report["skipped"] is True
    _assert_suspended(report, "auto_maintain")
    assert _tree(Path(config.home)) == before


@pytest.mark.parametrize("mode", UNCHANGED_MODES)
def test_auto_maintain_still_reports_its_own_disabled_switch(tmp_path, mode):
    """Negative control: the ordinary skip path is unchanged and distinct."""
    config = _config(tmp_path, mode, auto_maintain=False)

    report = memory_compactor.auto_maintain_after_write(config)

    assert report == {"skipped": True, "reason": "auto_maintain disabled"}


# ---------------------------------------------------------------------------
# The knowledge watcher
# ---------------------------------------------------------------------------

@pytest.fixture
def l0_syncs(monkeypatch):
    """Record every L0 publication the watcher attempts."""
    syncs: list[str] = []

    def _sync(config, dry_run=False):
        syncs.append("publish")
        return {"success": True, "entries_added": 1, "entries_removed": 0, "total_entries": 1}

    monkeypatch.setattr(l0_manager, "sync_l0_index", _sync)
    return syncs


def _watcher(config: MemoryConfig, events: list[tuple[str, str]]) -> KnowledgeWatcher:
    return KnowledgeWatcher(
        knowledge_dir=[str(d) for d in config.knowledge_dirs],
        on_change=lambda event, filename: events.append((event, filename)),
        poll_interval=600.0,  # never fires on its own inside a test
        config=config,
    )


def _watcher_config(tmp_path: Path, mode: str) -> MemoryConfig:
    config = _config(tmp_path, mode, auto_sync_l0=True)
    config.l0_index_file = Path(config.home) / "l0_index.md"
    return config


@pytest.mark.parametrize("mode", WRITE_MODES)
def test_the_watcher_invalidates_caches_but_publishes_no_l0(
    tmp_path, l0_syncs, mode
):
    config = _watcher_config(tmp_path, mode)
    events: list[tuple[str, str]] = []
    watcher = _watcher(config, events)

    try:
        watcher.start()  # startup sync would publish here
        assert watcher._should_auto_sync() is False
        assert l0_syncs == []

        # A real change: the cache callback must still fire...
        (Path(config.knowledge_dir) / "retrieval.md").write_text("# Retrieval\n", encoding="utf-8")
        watcher._poll()
        assert events == [("created", "0/retrieval.md")]

        # ...while nothing is scheduled to publish, and the debounced sync the
        # timer would have run is itself a no-op.
        assert watcher._pending_sync is False
        assert watcher._sync_timer is None
        watcher._perform_sync()
    finally:
        watcher.stop()

    assert l0_syncs == []
    assert not config.l0_index_file.exists()


@pytest.mark.parametrize("mode", UNCHANGED_MODES)
def test_the_watcher_still_auto_syncs_everywhere_else(tmp_path, l0_syncs, mode):
    """Negative control: v0.5.0 auto-sync behaviour is untouched."""
    config = _watcher_config(tmp_path, mode)
    events: list[tuple[str, str]] = []
    watcher = _watcher(config, events)

    try:
        watcher.start()
        assert watcher._should_auto_sync() is True
        assert l0_syncs == ["publish"]  # startup sync

        (Path(config.knowledge_dir) / "retrieval.md").write_text("# Retrieval\n", encoding="utf-8")
        watcher._poll()
        assert events == [("created", "0/retrieval.md")]
        assert watcher._pending_sync is True
        watcher._perform_sync()
    finally:
        watcher.stop()

    assert l0_syncs == ["publish", "publish"]

"""v3.4.0 — periodic framework self-maintenance.

The framework's self-maintenance used to ride along on ``inject_knowledge`` (and
``get_l0_index`` as a backstop), so a long-idle HTTP server never compacted and
never healed a deployed guard that drifted behind the package. These tests cover
the loop itself (runs, stops, survives a failing task), the factory that maps
config to a loop, and the task's composition — including the negative control
that a manual write-guard policy must not be touched.

Timing: the loop tests use a 20 ms tick and poll with a deadline, so they assert
"it ticks repeatedly" without being timing-sensitive on a loaded machine.
"""

from __future__ import annotations

import time

import pytest

from layered_memory_mcp import agent_integrator, memory_compactor, write_guard
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.maintenance import (
    DEFAULT_INITIAL_DELAY,
    DEFAULT_TICK_SECONDS,
    MIN_TICK_SECONDS,
    MaintenanceLoop,
    maintenance_task,
    start_maintenance,
)


def _mk_config(tmp_path, **kw) -> MemoryConfig:
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"), **kw)


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestLoop:
    def test_runs_the_task_repeatedly_then_stops(self):
        calls = []
        loop = MaintenanceLoop(lambda: calls.append(1), interval=0.02, initial_delay=0)
        assert loop.start() is True
        assert loop.running is True
        assert _wait_for(lambda: len(calls) >= 3), f"only {len(calls)} ticks"
        assert loop.stop() is True
        assert loop.running is False

        # A stopped loop stays stopped: no further ticks.
        settled = len(calls)
        time.sleep(0.06)
        assert len(calls) == settled

    def test_start_is_idempotent(self):
        loop = MaintenanceLoop(lambda: None, interval=0.02, initial_delay=0)
        assert loop.start() is True
        assert loop.start() is False, "a second start must not spawn a second thread"
        assert loop.stop() is True

    def test_stop_before_start_is_safe(self):
        loop = MaintenanceLoop(lambda: None, interval=5.0, initial_delay=5.0)
        assert loop.running is False
        assert loop.stop() is True

    def test_a_failing_task_does_not_kill_the_loop(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("first tick explodes")

        loop = MaintenanceLoop(flaky, interval=0.02, initial_delay=0)
        loop.start()
        assert _wait_for(lambda: len(calls) >= 3), "loop died with the task"
        loop.stop()

    def test_initial_delay_is_interruptible(self):
        """A long first wait must not hold up shutdown."""
        loop = MaintenanceLoop(lambda: pytest.fail("must not tick"), interval=99.0, initial_delay=99.0)
        loop.start()
        started = time.time()
        assert loop.stop(timeout=2.0) is True
        assert time.time() - started < 2.0

    def test_run_once_returns_result_and_swallows_failure(self):
        assert MaintenanceLoop(lambda: {"ok": True}).run_once() == {"ok": True}

        def boom():
            raise ValueError("nope")

        assert MaintenanceLoop(boom).run_once() is None


class TestFactory:
    def test_disabled_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_MAINTENANCE_ENABLED", "0")
        cfg = _mk_config(tmp_path)
        assert start_maintenance(cfg) is None

    def test_enabled_starts_and_is_stoppable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_MAINTENANCE_TICK", "45")
        monkeypatch.setenv("LAYERED_MEMORY_MAINTENANCE_DELAY", "0")
        cfg = _mk_config(tmp_path)
        seen = []
        loop = start_maintenance(cfg, task=lambda: seen.append(1))
        try:
            assert loop is not None
            assert _wait_for(lambda: seen)
        finally:
            assert loop.stop() is True

    def test_tick_below_the_floor_is_raised(self, tmp_path):
        """Seconds/milliseconds mix-ups must not turn into a busy loop."""
        cfg = _mk_config(tmp_path, maintenance_tick_seconds=0.001, maintenance_initial_delay=0)
        loop = start_maintenance(cfg, task=lambda: None)
        try:
            assert loop is not None
            assert loop._interval == MIN_TICK_SECONDS
        finally:
            loop.stop()

    def test_config_supplies_the_interval(self, tmp_path):
        cfg = _mk_config(tmp_path, maintenance_tick_seconds=120.0, maintenance_initial_delay=0.0)
        loop = start_maintenance(cfg, task=lambda: None)
        try:
            assert loop is not None
            assert loop._interval == 120.0
        finally:
            loop.stop()


class TestTaskComposition:
    def test_runs_auto_maintain_and_refreshes_the_guard(self, tmp_path, monkeypatch):
        cfg = _mk_config(tmp_path, write_guard="auto")
        calls = {}

        def _auto_maintain(config):
            calls["auto_maintain"] = config
            return {"compact": "ok"}

        def _refresh(home, mode, state):
            calls["guard"] = (home, mode, state)
            return {"status": "refreshed"}

        monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", _auto_maintain)
        monkeypatch.setattr(agent_integrator, "detect_agent_type", lambda: {"home_dir": tmp_path / "hermes"})
        monkeypatch.setattr(write_guard, "refresh_deployed_plugin", _refresh)

        report = maintenance_task(cfg)

        assert calls["auto_maintain"] is cfg
        assert report["auto_maintain"] == {"compact": "ok"}
        assert calls["guard"][0] == tmp_path / "hermes"
        assert calls["guard"][1] == "auto"
        assert report["write_guard"] == {"status": "refreshed"}

    def test_manual_policy_leaves_the_guard_alone(self, tmp_path, monkeypatch):
        """Negative control: a manual host keeps the human in the loop."""
        cfg = _mk_config(tmp_path, write_guard="manual")
        monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", lambda config: {})
        monkeypatch.setattr(
            write_guard, "refresh_deployed_plugin",
            lambda *a, **kw: pytest.fail("manual policy must not refresh the deployed guard"),
        )
        report = maintenance_task(cfg)
        assert "write_guard" not in report

    def test_a_failing_auto_maintain_does_not_block_the_guard(self, tmp_path, monkeypatch):
        cfg = _mk_config(tmp_path, write_guard="auto")

        def boom(config):
            raise RuntimeError("compaction down")

        monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", boom)
        monkeypatch.setattr(agent_integrator, "detect_agent_type", lambda: {"home_dir": tmp_path / "hermes"})
        monkeypatch.setattr(write_guard, "refresh_deployed_plugin", lambda *a, **kw: {"status": "refreshed"})

        report = maintenance_task(cfg)

        assert "compaction down" in report["auto_maintain"]["error"]
        assert report["write_guard"] == {"status": "refreshed"}

    def test_task_never_raises_even_with_no_agent_home(self, tmp_path, monkeypatch):
        cfg = _mk_config(tmp_path, write_guard="auto")
        monkeypatch.setattr(memory_compactor, "auto_maintain_after_write", lambda config: {"ok": True})
        monkeypatch.setattr(agent_integrator, "detect_agent_type", lambda: {"home_dir": None})
        report = maintenance_task(cfg)
        assert report["auto_maintain"] == {"ok": True}
        assert "write_guard" not in report


class TestConfig:
    def test_defaults_are_on(self, tmp_path):
        cfg = _mk_config(tmp_path)
        assert cfg.maintenance_enabled is True
        assert cfg.maintenance_tick_seconds == DEFAULT_TICK_SECONDS
        assert cfg.maintenance_initial_delay == DEFAULT_INITIAL_DELAY

    def test_env_can_disable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_MAINTENANCE_ENABLED", "false")
        monkeypatch.setenv("LAYERED_MEMORY_MAINTENANCE_TICK", "600")
        monkeypatch.setenv("LAYERED_MEMORY_MAINTENANCE_DELAY", "5")
        cfg = _mk_config(tmp_path)
        assert cfg.maintenance_enabled is False
        assert cfg.maintenance_tick_seconds == 600.0
        assert cfg.maintenance_initial_delay == 5.0

    def test_garbage_values_fall_back(self, tmp_path):
        cfg = _mk_config(tmp_path, maintenance_tick_seconds="not-a-number", maintenance_initial_delay="")
        assert cfg.maintenance_tick_seconds == DEFAULT_TICK_SECONDS
        assert cfg.maintenance_initial_delay == DEFAULT_INITIAL_DELAY

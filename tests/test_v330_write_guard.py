"""Tests for v3.3.0: the MEMORY.md write guard.

Covers:
  - policy truth table, with negative controls (USER.md and unrelated paths
    must stay writable — a guard that blocks everything would look healthy on
    the positive cases alone)
  - the ``tool_input`` vs ``args`` keyword trap: Hermes passes ``args``, so a
    hook reading only ``tool_input`` is silently dead; both spellings must work
  - runtime kill switch via LAYERED_MEMORY_WRITE_GUARD
  - deployment: atomic file copy, enable via the host CLI, nudge silenced,
    state recorded, self-test run
  - idempotence, version conflict, and the "no hermes CLI" path (which must
    surface manual commands instead of hand-editing the host's config)
  - removal restores the previous nudge value
  - the ``write_guard`` config policy (auto / manual / off, invalid rejected)
  - MCP wiring: integrate_agent routes install_guard/guard_status/remove_guard

All fixtures use neutral placeholder content (no business data).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.write_guard import (
    GUARD_PLUGIN_NAME,
    GUARD_VERSION,
    check_guard_status,
    ensure_guard_installed,
    install_guard,
    plugin_dir,
    remove_guard,
    self_test,
)
from layered_memory_mcp.write_guard.plugin import guard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _host_config(tmp_path: Path, *, nudge: int = 10, enabled: list[str] | None = None) -> Path:
    """Write a minimal host config.yaml (Hermes-shaped, placeholder content)."""
    enabled = enabled or ["some-other-plugin"]
    lines = [
        "memory:",
        "  memory_enabled: true",
        f"  nudge_interval: {nudge}",
        "plugins:",
        "  enabled:",
    ]
    lines += [f"    - {name}" for name in enabled]
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _Recorder:
    """Stand-in for the host CLI: records argv, returns success."""

    def __init__(self, code: int = 0):
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return self.code, "ok", ""


def _assert_blocked(result) -> dict:
    """The call must be blocked; a None here means the guard failed open."""
    assert isinstance(result, dict), "expected a block directive, got None (guard failed open)"
    assert result.get("action") == "block"
    return result


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

class TestGuardPolicy:
    def test_blocks_memory_tool_targeting_memory(self):
        result = _assert_blocked(
            guard.decide("memory", args={"action": "add", "target": "memory", "content": "x"})
        )
        assert result["message"] == guard.MEMORY_TOOL_MESSAGE

    def test_blocks_memory_tool_with_omitted_target(self):
        """No target means the memory tool's own default: MEMORY.md."""
        _assert_blocked(guard.decide("memory", args={"action": "add", "content": "x"}))

    def test_allows_memory_tool_targeting_user(self):
        """Negative control: the user profile is a legitimate store."""
        assert guard.decide("memory", args={"action": "add", "target": "user", "content": "x"}) is None

    def test_blocks_direct_edits_of_the_agent_memory_file(self, tmp_path):
        memory_md = str(tmp_path / "memories" / "MEMORY.md")
        for tool in ("write_file", "patch"):
            payload = {"path": memory_md, "content": "x", "old_string": "a", "new_string": "b"}
            _assert_blocked(guard.decide(tool, args=payload))

    def test_allows_edits_of_other_files(self, tmp_path):
        """Negative controls: USER.md and unrelated MEMORY.md files stay writable."""
        cases = [
            {"path": str(tmp_path / "memories" / "USER.md"), "content": "x"},
            {"path": str(tmp_path / "notes.md"), "content": "x"},
            # A directory merely *containing* the word must not match.
            {"path": str(tmp_path / "memories_backup" / "MEMORY.md"), "content": "x"},
        ]
        for payload in cases:
            assert guard.decide("write_file", args=payload) is None, payload

    def test_ignores_unrelated_tools(self):
        assert guard.decide("terminal", args={"command": "ls"}) is None
        assert guard.decide("read_file", args={"path": "/tmp/MEMORY.md"}) is None

    def test_accepts_the_tool_input_spelling(self):
        """Hermes passes ``args``; a hook that reads only ``tool_input`` is dead."""
        _assert_blocked(guard.pre_tool_call(tool_name="memory", tool_input={"target": "memory"}))
        _assert_blocked(guard.pre_tool_call(tool_name="memory", args={"target": "memory"}))

    def test_memory_tool_fails_closed_without_readable_args(self):
        """Unreadable args cannot prove a USER.md write, so the protected store wins."""
        _assert_blocked(guard.pre_tool_call(tool_name="memory"))

    def test_never_raises_on_garbage(self):
        assert guard.pre_tool_call(tool_name=None, args="not-a-dict") is None
        assert guard.decide("write_file", args={"path": object()}) is None

    def test_kill_switch_disables_blocking(self, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_WRITE_GUARD", "off")
        assert guard.guard_disabled() is True
        assert guard.decide("memory", args={"target": "memory"}) is None


# ---------------------------------------------------------------------------
# Config policy
# ---------------------------------------------------------------------------

class TestWriteGuardConfig:
    def test_defaults_to_manual(self, tmp_path):
        assert MemoryConfig(home=str(tmp_path)).write_guard == "manual"

    def test_reads_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_WRITE_GUARD", "auto")
        assert MemoryConfig(home=str(tmp_path)).write_guard == "auto"

    def test_reads_from_framework_config_yaml(self, tmp_path):
        (tmp_path / "config.yaml").write_text('write_guard: "auto"\n', encoding="utf-8")
        assert MemoryConfig(home=str(tmp_path)).write_guard == "auto"

    def test_yaml_boolean_off_is_coerced(self, tmp_path):
        """Regression: YAML 1.1 parses bare ``off`` as False, not as 'off'.

        Before the coercion the most natural spelling of "turn it off" raised
        ValueError and would have taken the server down at start-up.
        """
        (tmp_path / "config.yaml").write_text("write_guard: off\n", encoding="utf-8")
        assert MemoryConfig(home=str(tmp_path)).write_guard == "off"

    def test_yaml_boolean_on_is_coerced(self, tmp_path):
        (tmp_path / "config.yaml").write_text("write_guard: on\n", encoding="utf-8")
        assert MemoryConfig(home=str(tmp_path)).write_guard == "auto"

    def test_constructor_argument_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_WRITE_GUARD", "off")
        assert MemoryConfig(home=str(tmp_path), write_guard="auto").write_guard == "auto"

    def test_invalid_value_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid write_guard"):
            MemoryConfig(home=str(tmp_path), write_guard="sometimes")

    def test_broken_config_yaml_falls_back(self, tmp_path):
        (tmp_path / "config.yaml").write_text("write_guard: [unclosed\n", encoding="utf-8")
        assert MemoryConfig(home=str(tmp_path)).write_guard == "manual"


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

class TestGuardDeployment:
    def test_status_is_not_installed_on_a_clean_host(self, tmp_path):
        status = check_guard_status(tmp_path, config_path=_host_config(tmp_path), state_path=tmp_path / "state.json")
        assert status["status"] == "not_installed"
        assert status["installed"] is False

    def test_install_deploys_enables_and_self_tests(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        rec = _Recorder()
        cfg = _host_config(tmp_path, nudge=10)

        result = install_guard(tmp_path, config_path=cfg, state_path=tmp_path / "state.json", runner=rec)

        assert result["action"] == "installed"
        assert result["success"] is True
        assert result["restart_required"] is True
        deployed = plugin_dir(tmp_path)
        assert (deployed / "guard.py").exists()
        assert (deployed / "plugin.yaml").exists()
        assert (deployed / "__init__.py").exists()
        assert not (deployed / "__pycache__").exists()
        assert [GUARD_PLUGIN_NAME] == [c[3] for c in rec.calls if c[1] == "plugins" and c[2] == "enable"]
        assert ["memory.nudge_interval", "0"] == [c[3:] for c in rec.calls if c[1] == "config"][0]
        assert result["self_test"]["ok"] is True
        assert result["self_test"]["passed"] == result["self_test"]["total"] >= 5

    def test_install_records_previous_nudge_for_restore(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        cfg = _host_config(tmp_path, nudge=7)
        state_path = tmp_path / "state.json"

        install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())

        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["nudge_interval_previous"] == 7
        assert state["version"] == GUARD_VERSION

    def test_install_is_idempotent_when_already_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        cfg = _host_config(tmp_path, nudge=0, enabled=[GUARD_PLUGIN_NAME])
        state_path = tmp_path / "state.json"
        install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())

        again = install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())

        assert again["action"] == "already_installed"
        assert check_guard_status(tmp_path, config_path=cfg, state_path=state_path)["status"] == "up_to_date"

    def test_installed_but_not_enabled_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        cfg = _host_config(tmp_path, nudge=0)  # plugin absent from plugins.enabled
        state_path = tmp_path / "state.json"
        install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())

        status = check_guard_status(tmp_path, config_path=cfg, state_path=state_path)
        assert status["status"] == "installed_disabled"
        assert status["enabled"] is False

    def test_version_conflict_requires_force(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        cfg = _host_config(tmp_path, nudge=0, enabled=[GUARD_PLUGIN_NAME])
        state_path = tmp_path / "state.json"
        install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())

        manifest = plugin_dir(tmp_path) / "plugin.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(GUARD_VERSION, "0.0.1"), encoding="utf-8")

        conflict = install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())
        assert conflict["action"] == "version_conflict"
        assert conflict["success"] is False

        forced = install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder(), force=True)
        assert forced["action"] in ("updated", "already_installed")
        assert (plugin_dir(tmp_path) / "plugin.yaml").read_text(encoding="utf-8").count(GUARD_VERSION) == 1

    def test_missing_hermes_cli_surfaces_manual_commands(self, tmp_path, monkeypatch):
        """Without the host CLI the framework must not hand-edit config.yaml."""
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: None)
        cfg = _host_config(tmp_path, nudge=10)
        before = cfg.read_text(encoding="utf-8")

        result = install_guard(tmp_path, config_path=cfg, state_path=tmp_path / "state.json", runner=_Recorder())

        assert result["success"] is False
        assert f"hermes plugins enable {GUARD_PLUGIN_NAME}" in result["manual_actions"]
        assert "hermes config set memory.nudge_interval 0" in result["manual_actions"]
        assert cfg.read_text(encoding="utf-8") == before

    def test_ensure_is_a_noop_on_a_healthy_host(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        cfg = _host_config(tmp_path, nudge=0, enabled=[GUARD_PLUGIN_NAME])
        state_path = tmp_path / "state.json"
        install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())

        ensured = ensure_guard_installed(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())
        assert ensured["action"] == "noop"

    def test_ensure_installs_on_a_clean_host(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        cfg = _host_config(tmp_path, nudge=10)
        ensured = ensure_guard_installed(
            tmp_path, config_path=cfg, state_path=tmp_path / "state.json", runner=_Recorder()
        )
        assert ensured["action"] == "installed"
        assert ensured["status"]["installed"] is True

    def test_remove_restores_nudge_and_deletes_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        cfg = _host_config(tmp_path, nudge=9)
        state_path = tmp_path / "state.json"
        install_guard(tmp_path, config_path=cfg, state_path=state_path, runner=_Recorder())
        rec = _Recorder()

        result = remove_guard(tmp_path, config_path=cfg, state_path=state_path, runner=rec)

        assert result["action"] == "removed"
        assert result["restored_nudge_interval"] == 9
        assert not plugin_dir(tmp_path).exists()
        assert ["memory.nudge_interval", "9"] == [c[3:] for c in rec.calls if c[1] == "config"][0]
        assert any(c[1] == "plugins" and c[2] == "disable" for c in rec.calls)
        assert not state_path.exists()

    def test_self_test_reports_missing_payload(self, tmp_path):
        result = self_test(tmp_path)
        assert result["ok"] is False
        assert result["passed"] == 0


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------

class TestPluginWiring:
    def test_register_hooks_pre_tool_call(self):
        from layered_memory_mcp.write_guard.plugin import register as plugin_register

        registered = {}

        class _Ctx:
            def register_hook(self, name, callback):
                registered[name] = callback

        plugin_register(_Ctx())

        assert "pre_tool_call" in registered
        decision = _assert_blocked(registered["pre_tool_call"](tool_name="memory", args={"target": "memory"}))
        assert decision["message"] == guard.MEMORY_TOOL_MESSAGE
        # Negative control through the registered callback.
        assert registered["pre_tool_call"](tool_name="memory", args={"target": "user"}) is None

    def test_deployed_payload_matches_the_package(self, tmp_path, monkeypatch):
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        cfg = _host_config(tmp_path)
        install_guard(tmp_path, config_path=cfg, state_path=tmp_path / "state.json", runner=_Recorder())

        source = Path(guard.__file__)
        assert (plugin_dir(tmp_path) / "guard.py").read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# MCP tool wiring
# ---------------------------------------------------------------------------

class TestIntegrateAgentWiring:
    @pytest.fixture()
    def _fake_host(self, tmp_path, monkeypatch):
        """A fake Hermes host: no real CLI, no real home."""
        from layered_memory_mcp import server

        host_home = tmp_path / "hermes-home"
        host_home.mkdir(parents=True, exist_ok=True)
        cfg = _host_config(tmp_path, nudge=10)

        monkeypatch.setattr(
            server,
            "detect_agent_type",
            lambda: {"agent_type": "hermes", "home_dir": host_home, "soul_path": None, "memory_path": None},
        )
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")
        monkeypatch.setattr(
            "layered_memory_mcp.write_guard._run_command",
            lambda cmd, runner=None: (0, "ok", ""),
        )
        return server, host_home

    @pytest.mark.asyncio
    async def test_install_guard_action(self, _fake_host):
        server, host_home = _fake_host
        payload = json.loads(await server.integrate_agent("install_guard"))

        assert payload["action"] == "installed"
        assert payload["self_test"]["ok"] is True
        assert (plugin_dir(host_home) / "guard.py").exists()

    @pytest.mark.asyncio
    async def test_guard_status_and_remove_actions(self, _fake_host):
        server, host_home = _fake_host
        await server.integrate_agent("install_guard")

        status = json.loads(await server.integrate_agent("guard_status"))
        assert status["installed"] is True

        removed = json.loads(await server.integrate_agent("remove_guard"))
        assert removed["action"] == "removed"
        assert not plugin_dir(host_home).exists()

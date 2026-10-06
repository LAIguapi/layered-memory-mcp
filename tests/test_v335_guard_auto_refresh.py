"""v3.3.5 — the deployed write guard must not drift behind the framework.

The guard on the host is a *copy* of the payload bundled in this package, so
every release left it one version behind. Worse, the ``auto`` policy could not
heal that on its own: ``ensure_guard_installed`` routed a version drift into
``install_guard``, which refuses to overwrite without ``force=True`` — so the
drift was permanent and ``check_guard_status`` reported ``update_available``
forever (measured against a real host: deployed 3.3.0 while the package was
3.3.4, with byte-identical ``guard.py`` and only ``plugin.yaml`` differing).

``refresh_deployed_plugin`` closes that: files-only re-copy when the versions
differ, invoked from the framework's read path. These tests cover the repair
itself, the policy gate (negative controls), the healing of the ``auto`` path,
and the ``get_l0_index`` ride-along end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from layered_memory_mcp.write_guard import (
    GUARD_VERSION,
    ensure_guard_installed,
    get_guard_version,
    install_guard,
    plugin_dir,
    refresh_deployed_plugin,
)

STALE_VERSION = "3.2.0"


def _deploy_stale_guard(host_home: Path, version: str = STALE_VERSION) -> Path:
    """Write a fake deployed guard at an older version."""
    pdir = plugin_dir(host_home)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.yaml").write_text(
        f'name: layered-memory-guard\nversion: "{version}"\n', encoding="utf-8"
    )
    for name in ("guard.py", "__init__.py"):
        (pdir / name).write_text("# deployed by an older release\n", encoding="utf-8")
    return pdir


def _host_config(tmp_path: Path, write_guard: str = "auto") -> Path:
    cfg = tmp_path / "host-config.yaml"
    cfg.write_text(
        f"memory:\n  nudge_interval: 0\nwrite_guard: {write_guard}\n", encoding="utf-8"
    )
    return cfg


class TestRefreshRepairsDrift:
    def test_stale_deployed_copy_is_refreshed(self, tmp_path):
        host = tmp_path / "hermes-home"
        pdir = _deploy_stale_guard(host)
        state = tmp_path / "write_guard_state.json"
        state.write_text(json.dumps({"version": STALE_VERSION, "installed_at": "x"}), encoding="utf-8")

        result = refresh_deployed_plugin(host, "auto", state_path=state)

        assert result["refreshed"] is True, result
        assert result["from"] == STALE_VERSION
        assert result["to"] == GUARD_VERSION
        assert get_guard_version(host) == GUARD_VERSION
        # The payload really is the bundled one, not just a rewritten version line.
        assert (pdir / "guard.py").read_text(encoding="utf-8").startswith('"""')

    def test_install_record_is_updated(self, tmp_path):
        host = tmp_path / "hermes-home"
        _deploy_stale_guard(host)
        state = tmp_path / "write_guard_state.json"
        state.write_text(json.dumps({"version": STALE_VERSION, "installed_at": "x"}), encoding="utf-8")

        refresh_deployed_plugin(host, "auto", state_path=state)

        record = json.loads(state.read_text(encoding="utf-8"))
        assert record["version"] == GUARD_VERSION
        assert "refreshed_at" in record
        assert record["installed_at"] == "x", "an install record must not be rewritten wholesale"

    def test_up_to_date_is_a_no_op(self, tmp_path):
        host = tmp_path / "hermes-home"
        pdir = _deploy_stale_guard(host, version=GUARD_VERSION)
        before = (pdir / "guard.py").stat().st_mtime_ns

        result = refresh_deployed_plugin(host, "auto", state_path=tmp_path / "s.json")

        assert result == {"refreshed": False, "reason": "up_to_date", "version": GUARD_VERSION}
        assert (pdir / "guard.py").stat().st_mtime_ns == before

    def test_idempotent_second_call(self, tmp_path):
        host = tmp_path / "hermes-home"
        _deploy_stale_guard(host)
        state = tmp_path / "s.json"

        first = refresh_deployed_plugin(host, "auto", state_path=state)
        second = refresh_deployed_plugin(host, "auto", state_path=state)

        assert first["refreshed"] is True
        assert second["refreshed"] is False and second["reason"] == "up_to_date"

    def test_broken_source_reports_failure_without_raising(self, tmp_path, monkeypatch):
        host = tmp_path / "hermes-home"
        _deploy_stale_guard(host)
        monkeypatch.setattr(
            "layered_memory_mcp.write_guard._get_plugin_source_dir",
            lambda: tmp_path / "does-not-exist",
        )

        result = refresh_deployed_plugin(host, "auto", state_path=tmp_path / "s.json")

        assert result["refreshed"] is False
        assert result["reason"] == "deploy_failed"
        assert get_guard_version(host) == STALE_VERSION, "a failed refresh must leave the host alone"


class TestPolicyGate:
    """Negative controls: nothing happens unless the host opted into auto."""

    @pytest.mark.parametrize("policy", ["manual", "off", "", None, "MANUAL"])
    def test_non_auto_policy_never_touches_the_host(self, tmp_path, policy):
        host = tmp_path / "hermes-home"
        pdir = _deploy_stale_guard(host)

        result = refresh_deployed_plugin(host, policy, state_path=tmp_path / "s.json")

        assert result["refreshed"] is False
        assert result["reason"] == "policy_not_auto"
        assert get_guard_version(host) == STALE_VERSION
        assert (pdir / "guard.py").read_text(encoding="utf-8") == "# deployed by an older release\n"

    def test_not_installed_is_not_installed_by_refresh(self, tmp_path):
        # A refresh must never bootstrap: that is install_guard's job, and it
        # carries config changes this path has no business making.
        host = tmp_path / "hermes-home"
        host.mkdir(parents=True, exist_ok=True)

        result = refresh_deployed_plugin(host, "auto", state_path=tmp_path / "s.json")

        assert result == {"refreshed": False, "reason": "not_installed"}
        assert not plugin_dir(host).exists()


class TestAutoPolicyHealsDrift:
    def test_ensure_guard_installed_refreshes_instead_of_conflicting(self, tmp_path, monkeypatch):
        host = tmp_path / "hermes-home"
        _deploy_stale_guard(host)
        cfg = _host_config(tmp_path)
        state = tmp_path / "s.json"
        monkeypatch.setattr("layered_memory_mcp.write_guard.find_hermes_cli", lambda: "/usr/bin/hermes")

        result = ensure_guard_installed(host, config_path=cfg, state_path=state)

        assert result["action"] == "refreshed", result
        assert result["install"] is None, "a refresh must not run install steps"
        assert result["status"]["status"] == "up_to_date"
        assert result["status"]["version"] == GUARD_VERSION

    def test_explicit_install_still_refuses_unforced_drift(self, tmp_path):
        """The protection this change builds on: install is not a silent overwrite."""
        host = tmp_path / "hermes-home"
        _deploy_stale_guard(host)

        result = install_guard(
            host, config_path=_host_config(tmp_path), state_path=tmp_path / "s.json"
        )

        assert result["success"] is False
        assert result["action"] == "version_conflict"
        assert get_guard_version(host) == STALE_VERSION


class TestGetL0IndexRideAlong:
    @pytest.fixture()
    def _fake_host(self, tmp_path, monkeypatch):
        from layered_memory_mcp import server
        from layered_memory_mcp.config import MemoryConfig

        host_home = tmp_path / "hermes-home"
        (host_home / "memories").mkdir(parents=True, exist_ok=True)
        (host_home / "config.yaml").write_text("memory:\n  nudge_interval: 0\n", encoding="utf-8")
        pdir = _deploy_stale_guard(host_home)

        def fake_config(policy: str) -> MemoryConfig:
            return MemoryConfig(home=tmp_path / "lm", write_guard=policy)

        monkeypatch.setattr(
            server,
            "detect_agent_type",
            lambda: {
                "agent_type": "hermes",
                "home_dir": host_home,
                "soul_path": None,
                "memory_path": None,
            },
        )
        return server, host_home, pdir, fake_config

    @pytest.mark.asyncio
    async def test_read_path_refreshes_a_stale_guard(self, _fake_host, monkeypatch):
        server, host_home, pdir, fake_config = _fake_host
        monkeypatch.setattr(server, "_config", fake_config("auto"))

        await server.get_l0_index()

        assert get_guard_version(host_home) == GUARD_VERSION
        assert (pdir / "guard.py").read_text(encoding="utf-8").startswith('"""')

    @pytest.mark.asyncio
    async def test_read_path_leaves_a_manual_host_alone(self, _fake_host, monkeypatch):
        server, host_home, pdir, fake_config = _fake_host
        monkeypatch.setattr(server, "_config", fake_config("manual"))

        await server.get_l0_index()

        assert get_guard_version(host_home) == STALE_VERSION
        assert (pdir / "guard.py").read_text(encoding="utf-8") == "# deployed by an older release\n"

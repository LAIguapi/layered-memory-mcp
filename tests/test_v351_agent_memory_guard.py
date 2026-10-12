"""Agent-memory write guard — the incident this file exists to prevent.

An ad-hoc sanity script built a bare ``MemoryConfig(home=<tmpdir>)``, called
``inject_knowledge`` with ``dry_run=False``, and the ride-along
``auto_maintain_after_write`` auto-detected the *real* ``~/.hermes/memories/
MEMORY.md`` and appended an ``[L0] misc: ...`` pointer to a live agent's memory.
Every individual step behaved as designed; the hazard was that an ambiguous
config silently promoted an auto-detected path to a write target.

These tests pin both halves of the fix (see
:mod:`layered_memory_mcp.agent_memory_guard`):

  * an auto-detected memory file outside this install's own home is **refused**
    as a write target, the refusal is **reported**, and the primary L1 write
    still succeeds;
  * an explicit target — or an explicit opt-in — still works.

Every path below uses a *fake* production file inside ``tmp_path``. Nothing
here, in any branch, is allowed to resolve the real ``~/.hermes`` or
``~/.layered-memory`` (conftest's sandbox fixtures are the backstop).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from layered_memory_mcp import agent_memory_guard as guard
from layered_memory_mcp import config as config_mod
from layered_memory_mcp.agent_memory_guard import UNSAFE_AUTO_DETECT, resolve_write_target
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.injector import inject_knowledge
from layered_memory_mcp.memory_compactor import (
    _ensure_index_entry_in_memory,
    _ensure_l0_pointer_in_memory,
    _remove_l0_pointer_from_memory,
    auto_maintain_after_write,
    compact_memory,
)

#: Content of the fake "production" memory file. Long enough to read as bloat,
#: and carrying a pointer so the reap path has something to match.
PROD_CONTENT = (
    "[L0] infra: proxy notes → knowledge/infra.md\n"
    "§\n"
    "A long detailed entry about proxy and docker configuration that clearly "
    "belongs in an L1 knowledge file rather than in the agent's index layer, "
    "well past the pointer length limit.\n"
)

POINTER = "[L0] misc: sanity check → knowledge/misc.md"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def prod(tmp_path: Path) -> Path:
    """A fake production memory file, *outside* any config home we build."""
    target = tmp_path / "fake-prod-home" / ".hermes" / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(PROD_CONTENT, encoding="utf-8")
    return target


@pytest.fixture
def detect_prod(monkeypatch, prod: Path):
    """Make auto-detection resolve the fake production file.

    Mirrors the real method's contract: an explicitly configured path still
    wins, so the opt-in tests below exercise the same precedence production
    would.
    """

    def _detect(self):
        return self.agent_memory_path or prod

    monkeypatch.setattr(MemoryConfig, "detect_agent_memory_path", _detect)
    return prod


def _bare_config(tmp_path: Path, monkeypatch, **kwargs) -> MemoryConfig:
    """A temp-home config with no explicit agent memory target — the hazard."""
    monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
    monkeypatch.delenv(
        "LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE", raising=False
    )
    home = tmp_path / "bare-home"
    return MemoryConfig(home=str(home), **kwargs)


class TestUnsafeAutoDetectIsRefused:
    """A bare/temp config must never write an auto-detected production file."""

    def test_pointer_dual_write_is_refused_and_reported(
        self, tmp_path, monkeypatch, detect_prod
    ):
        cfg = _bare_config(tmp_path, monkeypatch)
        before = _digest(detect_prod)

        report = _ensure_l0_pointer_in_memory(POINTER, cfg)

        assert report["action"] == "skipped"
        assert report["reason"] == UNSAFE_AUTO_DETECT
        assert report["detected_path"] == str(detect_prod)
        assert "agent_memory.path" in report["hint"]
        assert _digest(detect_prod) == before
        assert detect_prod.read_text(encoding="utf-8") == PROD_CONTENT

    def test_index_entry_upsert_is_refused(self, tmp_path, monkeypatch, detect_prod):
        """index_only mode rewrites the whole file — same permission needed."""
        cfg = _bare_config(tmp_path, monkeypatch, memory_mode="index_only")
        before = _digest(detect_prod)

        report = _ensure_index_entry_in_memory(cfg)

        assert report["action"] == "skipped"
        assert report["reason"] == UNSAFE_AUTO_DETECT
        assert _digest(detect_prod) == before

    def test_dual_write_dispatch_through_memory_mode_is_refused(
        self, tmp_path, monkeypatch, detect_prod
    ):
        """The index_only dispatch inside the pointer path is guarded too."""
        cfg = _bare_config(tmp_path, monkeypatch, memory_mode="index_only")
        before = _digest(detect_prod)

        report = _ensure_l0_pointer_in_memory(POINTER, cfg)

        assert report["action"] == "skipped"
        assert report["reason"] == UNSAFE_AUTO_DETECT
        assert _digest(detect_prod) == before

    def test_pointer_reap_is_refused(self, tmp_path, monkeypatch, detect_prod):
        """Deleting an entry we were not asked to touch is a write as well."""
        cfg = _bare_config(tmp_path, monkeypatch)
        before = _digest(detect_prod)

        report = _remove_l0_pointer_from_memory("infra", cfg)

        assert report["action"] == "skipped"
        assert report["reason"] == UNSAFE_AUTO_DETECT
        assert _digest(detect_prod) == before
        assert "[L0] infra:" in detect_prod.read_text(encoding="utf-8")

    def test_auto_maintain_skips_and_reports(self, tmp_path, monkeypatch, detect_prod):
        """The exact chain from the incident, at the maintenance entry point."""
        cfg = _bare_config(tmp_path, monkeypatch, auto_maintain_interval_days=0.0)
        before = _digest(detect_prod)

        report = auto_maintain_after_write(cfg, l0_pointer=POINTER)

        assert report["skipped"] is True
        assert report["reason"] == UNSAFE_AUTO_DETECT
        assert report["agent_memory_write"]["status"] == "skipped"
        assert report["agent_memory_write"]["reason"] == UNSAFE_AUTO_DETECT
        assert report["agent_memory_write"]["detected_path"] == str(detect_prod)
        # Neither the dual-write nor the lazy compaction ran.
        assert report["dual_write"] is None
        assert report["compact"] is None
        assert _digest(detect_prod) == before

    def test_compact_memory_refuses_to_rewrite(self, tmp_path, monkeypatch, detect_prod):
        cfg = _bare_config(tmp_path, monkeypatch)
        before = _digest(detect_prod)

        report = compact_memory(cfg, dry_run=False)

        assert report["success"] is False
        assert UNSAFE_AUTO_DETECT in report["error"]
        assert report["agent_memory_write"]["status"] == "refused"
        assert report["agent_memory_write"]["reason"] == UNSAFE_AUTO_DETECT
        assert _digest(detect_prod) == before
        # Refused *before* any L1 injection — no content is lifted out of a
        # file we are not allowed to clean up afterwards.
        assert not list(Path(cfg.knowledge_dir).glob("*.md"))

    def test_compact_memory_dry_run_still_reports(
        self, tmp_path, monkeypatch, detect_prod
    ):
        """Auto-detect stays usable for reading/reporting — just not for writes."""
        cfg = _bare_config(tmp_path, monkeypatch)
        before = _digest(detect_prod)

        report = compact_memory(cfg, dry_run=True)

        assert report["success"] is True
        assert report["stats"]["before_entries"] == 2
        assert _digest(detect_prod) == before

    def test_inject_knowledge_writes_l1_and_surfaces_the_refusal(
        self, tmp_path, monkeypatch, detect_prod
    ):
        """Replay of the incident: the L1 write succeeds, the projection does not."""
        cfg = _bare_config(tmp_path, monkeypatch)
        before = _digest(detect_prod)

        result = inject_knowledge(
            cfg,
            domain="misc",
            section="Guard sanity check",
            content="A fact that belongs in L1 and nowhere near agent memory.",
        )

        assert result["success"] is True
        assert (Path(cfg.knowledge_dir) / "misc.md").exists()
        assert result["agent_memory_write"]["reason"] == UNSAFE_AUTO_DETECT
        assert result["agent_memory_write"]["detected_path"] == str(detect_prod)
        # No "auto-written to agent memory" claim when nothing was written.
        assert "auto-written" not in result.get("hint", "")
        assert _digest(detect_prod) == before
        assert detect_prod.read_text(encoding="utf-8") == PROD_CONTENT


class TestExplicitTargetAndOptIn:
    """Explicit is safe: a named target or a named opt-in still projects."""

    def test_explicit_config_path_inside_temp_home(
        self, tmp_path, monkeypatch, detect_prod
    ):
        home = tmp_path / "explicit-home"
        mem = home / "MEMORY.md"
        home.mkdir(parents=True, exist_ok=True)
        mem.write_text("", encoding="utf-8")
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
        before = _digest(detect_prod)

        cfg = MemoryConfig(home=str(home), agent_memory={"path": str(mem)})
        report = _ensure_l0_pointer_in_memory(POINTER, cfg)

        assert report["action"] == "added"
        assert POINTER in mem.read_text(encoding="utf-8")
        assert _digest(detect_prod) == before  # the detected file stays untouched

    def test_explicit_memory_path_argument(self, tmp_path, monkeypatch, detect_prod):
        cfg = _bare_config(tmp_path, monkeypatch)
        mem = tmp_path / "caller-named.md"
        mem.write_text("", encoding="utf-8")
        before = _digest(detect_prod)

        report = _ensure_l0_pointer_in_memory(POINTER, cfg, memory_path=mem)

        assert report["action"] == "added"
        assert POINTER in mem.read_text(encoding="utf-8")
        assert _digest(detect_prod) == before

    def test_allow_auto_detect_write_opt_in(self, tmp_path, monkeypatch):
        """Opt-in restores the legacy behaviour — against an isolated fake file."""
        fake = tmp_path / "opt-in-home" / ".hermes" / "memories" / "MEMORY.md"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            MemoryConfig,
            "detect_agent_memory_path",
            lambda self: self.agent_memory_path or fake,
        )
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)

        cfg = MemoryConfig(
            home=str(tmp_path / "optin-l1"),
            agent_memory={"allow_auto_detect_write": True},
        )
        report = _ensure_l0_pointer_in_memory(POINTER, cfg)

        assert report["action"] == "added"
        assert POINTER in fake.read_text(encoding="utf-8")

    def test_detected_file_inside_framework_home_is_allowed(
        self, tmp_path, monkeypatch
    ):
        """``<home>/agent-memory.md`` — the generic fallback the framework owns."""
        home = tmp_path / "generic-home"
        home.mkdir(parents=True, exist_ok=True)
        fallback = home / "agent-memory.md"
        fallback.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            MemoryConfig,
            "detect_agent_memory_path",
            lambda self: self.agent_memory_path or fallback,
        )
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)

        cfg = MemoryConfig(home=str(home))
        report = _ensure_l0_pointer_in_memory(POINTER, cfg)

        assert report["action"] == "added"
        assert POINTER in fallback.read_text(encoding="utf-8")

    def test_env_var_opt_in(self, tmp_path, monkeypatch, detect_prod):
        """The env knob is equivalent to the config.yaml one."""
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
        monkeypatch.setenv(
            "LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE", "true"
        )
        cfg = MemoryConfig(home=str(tmp_path / "env-home"))

        target = resolve_write_target(cfg)

        assert target.allowed is True
        assert target.trust == "allow_auto_detect_write"
        assert target.path == detect_prod


class TestConfigKnobs:
    def test_yaml_section_is_read(self, tmp_path, monkeypatch):
        home = tmp_path / "yaml-home"
        home.mkdir(parents=True, exist_ok=True)
        mem = home / "MEMORY.md"
        (home / "config.yaml").write_text(
            "agent_memory:\n"
            f"  path: {mem}\n"
            "  allow_auto_detect_write: true\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)

        cfg = MemoryConfig(home=str(home))

        assert cfg.agent_memory_path == mem
        assert cfg.agent_memory_allow_auto_detect_write is True

    def test_defaults_are_conservative(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
        cfg = MemoryConfig(home=str(tmp_path / "default-home"))

        assert cfg.agent_memory_path is None
        assert cfg.agent_memory_allow_auto_detect_write is False

    def test_env_path_still_wins(self, tmp_path, monkeypatch):
        """Pre-existing env var keeps its priority over the new YAML section."""
        home = tmp_path / "prio-home"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            "agent_memory:\n  path: /from/yaml/MEMORY.md\n", encoding="utf-8"
        )
        monkeypatch.setenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", "/from/env/MEMORY.md")

        cfg = MemoryConfig(home=str(home))

        assert cfg.agent_memory_path == Path("/from/env/MEMORY.md")


class TestGuardRules:
    """The predicates the rules are built on."""

    def test_pytest_sandbox_is_ephemeral(self, tmp_path):
        assert guard.is_ephemeral_home(tmp_path) is True

    def test_real_install_dir_is_not_ephemeral(self):
        assert guard.is_ephemeral_home("/opt/layered-memory") is False

    def test_a_test_run_is_never_a_configured_home(self, tmp_path, monkeypatch):
        """The legacy allowance can never fire from inside the suite.

        Even when the sandbox home *is* ``default_home()`` (conftest sets
        ``LAYERED_MEMORY_HOME`` for every test), a test run is not an install.
        """
        home = tmp_path / "pretend-prod"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config_mod, "default_home", lambda: home)
        cfg = MemoryConfig(home=str(home))

        assert guard._is_configured_home(cfg) is False

    def test_configured_home_allows_legacy_projection(self, tmp_path, monkeypatch):
        """Rule 5, simulated: an existing install keeps its projection.

        By design this cannot be satisfied for real from the test suite — the
        two sentinels patched here are exactly what keeps tests out of
        production memory — so the branch is pinned with them stubbed.
        """
        home = tmp_path / "install-home"
        home.mkdir(parents=True, exist_ok=True)
        detected = tmp_path / "isolated-agent-memory.md"
        detected.write_text("", encoding="utf-8")
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
        monkeypatch.setattr(guard, "_running_under_test", lambda: False)
        monkeypatch.setattr(guard, "is_ephemeral_home", lambda home: False)
        monkeypatch.setattr(config_mod, "default_home", lambda: home)
        monkeypatch.setattr(
            MemoryConfig,
            "detect_agent_memory_path",
            lambda self: self.agent_memory_path or detected,
        )

        cfg = MemoryConfig(home=str(home))
        target = resolve_write_target(cfg)

        assert target.allowed is True
        assert target.trust == "configured_home"

        report = _ensure_l0_pointer_in_memory(POINTER, cfg)
        assert report["action"] == "added"
        assert POINTER in detected.read_text(encoding="utf-8")

    def test_an_ad_hoc_home_is_refused_even_outside_a_test_run(
        self, tmp_path, monkeypatch, detect_prod
    ):
        """A scratch home on a real machine is still not *the* configured home."""
        monkeypatch.setattr(guard, "_running_under_test", lambda: False)
        monkeypatch.setattr(guard, "is_ephemeral_home", lambda home: False)
        monkeypatch.setattr(config_mod, "default_home", lambda: tmp_path / "elsewhere")
        cfg = _bare_config(tmp_path, monkeypatch)

        target = resolve_write_target(cfg)

        assert target.allowed is False
        assert target.reason == UNSAFE_AUTO_DETECT

    def test_unresolvable_path_is_not_a_safety_refusal(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
        monkeypatch.setattr(MemoryConfig, "detect_agent_memory_path", lambda self: None)
        cfg = MemoryConfig(home=str(tmp_path / "no-memory-home"))

        target = resolve_write_target(cfg)

        assert target.allowed is False
        assert target.reason == guard.UNRESOLVED
        assert "hint" not in target.skip_fields()

    def test_no_config_is_handled(self):
        target = resolve_write_target(None)
        assert target.allowed is False
        assert target.path is None

    def test_a_configured_path_does_not_license_a_different_file(
        self, tmp_path, monkeypatch
    ):
        """Rule 2 is about *this* file, not about having configured some file.

        Matters for callers that detect a candidate by another route and hand it
        to the guard (``detected=``): a configured target elsewhere must not be
        read as permission to write the probe's find.
        """
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
        home = tmp_path / "configured-home"
        home.mkdir(parents=True, exist_ok=True)
        mine = home / "MEMORY.md"
        mine.write_text("", encoding="utf-8")
        someone_elses = tmp_path / "other-home" / ".hermes" / "memories" / "MEMORY.md"
        someone_elses.parent.mkdir(parents=True, exist_ok=True)
        someone_elses.write_text(PROD_CONTENT, encoding="utf-8")

        cfg = MemoryConfig(home=str(home), agent_memory={"path": str(mine)})

        mine_target = resolve_write_target(cfg, detected=mine)
        assert mine_target.allowed is True
        assert mine_target.trust == "explicit_config"

        other = resolve_write_target(cfg, detected=someone_elses)
        assert other.allowed is False
        assert other.reason == UNSAFE_AUTO_DETECT
        assert other.path == someone_elses


class TestIntegrateAgentInjectMemory:
    """`integrate_agent(action="inject_memory")` is a write into agent memory.

    The user asks for it, but *which file* is still decided by a probe — so it
    is subject to the same rule as the L0 projection. Before this pass it wrote
    the auto-detected file directly, which is the one path in the codebase that
    still treated detection as permission.
    """

    def _run(self, cfg, action: str = "inject_memory") -> dict:
        import asyncio
        import json

        from layered_memory_mcp import server

        server._config = cfg
        try:
            return json.loads(asyncio.run(server.integrate_agent(action)))
        finally:
            server._config = None

    @pytest.fixture
    def hermes_host(self, monkeypatch):
        """Make the integration probe report a Hermes host at ``path``."""
        from layered_memory_mcp import server

        def _set(path):
            monkeypatch.setattr(
                server,
                "detect_agent_type",
                lambda: {
                    "agent_type": "hermes",
                    "soul_path": None,
                    "memory_path": path,
                    "home_dir": path.parent.parent,
                },
            )

        return _set

    def test_an_auto_detected_memory_file_is_refused(
        self, tmp_path, monkeypatch, prod, hermes_host
    ):
        hermes_host(prod)
        cfg = _bare_config(tmp_path, monkeypatch)
        before = _digest(prod)

        payload = self._run(cfg)

        assert payload["success"] is False
        assert payload["error"] == UNSAFE_AUTO_DETECT
        assert payload["agent_memory_write"]["status"] == "refused"
        assert payload["agent_memory_write"]["detected_path"] == str(prod)
        assert "agent_memory.path" in payload["agent_memory_write"]["hint"]
        assert _digest(prod) == before
        assert prod.read_text(encoding="utf-8") == PROD_CONTENT

    def test_an_explicitly_configured_file_is_still_injected(
        self, tmp_path, monkeypatch, hermes_host
    ):
        """The legitimate case keeps working, unchanged."""
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
        home = tmp_path / "explicit-home"
        home.mkdir(parents=True, exist_ok=True)
        mem = home / "MEMORY.md"
        mem.write_text("[L0] infra: notes → knowledge/infra.md\n", encoding="utf-8")
        hermes_host(mem)

        cfg = MemoryConfig(home=str(home), agent_memory={"path": str(mem)})
        payload = self._run(cfg)

        assert payload["success"] is True
        assert payload["action"] == "injected"
        assert "get_l0_index()" in mem.read_text(encoding="utf-8")

        # Idempotent, as before — and still guarded.
        again = self._run(cfg)
        assert again["action"] == "already_injected"

    def test_the_opt_in_restores_the_legacy_behaviour(
        self, tmp_path, monkeypatch, hermes_host
    ):
        monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
        fake = tmp_path / "opt-in-home" / ".hermes" / "memories" / "MEMORY.md"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text("", encoding="utf-8")
        hermes_host(fake)

        cfg = MemoryConfig(
            home=str(tmp_path / "optin-l1"),
            agent_memory={"allow_auto_detect_write": True},
        )
        payload = self._run(cfg)

        assert payload["success"] is True
        assert payload["action"] == "injected"
        assert "get_l0_index()" in fake.read_text(encoding="utf-8")

    def test_status_still_reads_the_detected_file_without_writing(
        self, tmp_path, monkeypatch, prod, hermes_host
    ):
        """Detection and reporting are untouched: the guard gates writes only."""
        hermes_host(prod)
        cfg = _bare_config(tmp_path, monkeypatch)
        before = _digest(prod)

        payload = self._run(cfg, action="status")

        assert payload["success"] is True
        assert payload["agent_type"] == "hermes"
        assert payload["memory_path"] == str(prod)
        assert payload["memory_injected"] is False
        assert _digest(prod) == before

"""Agent-environment adapter extraction — the core must hold no product paths.

Before this batch the common core *was* the Hermes integration:
``config.py`` spelled out ``~/.hermes/memories/MEMORY.md``,
``~/.hermes/state.db`` and ``~/.hermes/sessions``; ``memory_compactor.py``
carried the ``'§'`` heuristic, the 2000-char fallback and a reader for
``~/.hermes/config.yaml``; ``agent_integrator.py`` walked ``~/.hermes`` looking
for ``SOUL.md``. Supporting a second agent meant editing all three.

These tests pin the seam that replaced that:

  * the Hermes adapter resolves exactly what the core used to resolve, under an
    isolated fake HOME (nothing here may touch a real ``~/.hermes``);
  * the core asks the adapter layer — patching the registry changes what
    ``MemoryConfig`` resolves, which it could not do if a literal were left
    behind;
  * ``generic`` / ``none`` auto-detects no product path at all, and an unknown
    id falls back there rather than silently to Hermes;
  * the write guard still refuses an adapter-resolved production path from a
    temp home — detection is not permission;
  * capability reflection reports the configured and resolved adapter ids;
  * classification/routing is untouched by any of it.

Every HOME below is a ``tmp_path`` subdirectory. The adapters probe
``Path.home()``, so a test that forgot to redirect it would read the developer's
own install (conftest's sandbox fixtures are the backstop).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from layered_memory_mcp import agent_integrator
from layered_memory_mcp.adapters import environment as env_mod
from layered_memory_mcp.adapters.editors import ClaudeCodeEnvironment
from layered_memory_mcp.adapters.environment import (
    AUTO,
    GENERIC_ID,
    AgentEnvironmentAdapter,
    EnvironmentAdapterBase,
    EnvironmentRegistry,
    configured_adapter_id,
    describe_environment,
    environments,
    integration_environment,
    resolve_environment,
)
from layered_memory_mcp.adapters.hermes import (
    HERMES_DEFAULT_MEMORY_MAX_CHARS,
    HERMES_SEPARATOR,
    HermesEnvironment,
)
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.memory_compactor import _get_memory_max_chars, _resolve_separator
from layered_memory_mcp.routing import classify_memory_item, surface_capabilities

#: Core modules that must not name a product path any more. The Hermes adapter
#: itself is deliberately excluded — naming Hermes is its whole job.
#:
#: ``api/server.py`` is on the list because it was the last module outside
#: ``adapters/`` that still defaulted its stores to ``~/.hermes/knowledge`` and
#: ``~/.hermes/data`` — a layout no other entry point shared — and the boundary
#: test never looked at it. It now resolves ``MemoryConfig``'s own
#: ``knowledge_dir`` / ``<home>/data``.
_CORE_SOURCES = (
    "config.py",
    "memory_compactor.py",
    "routing.py",
    "agent_memory_guard.py",
    "memory_changelog.py",
    "api/server.py",
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "layered_memory_mcp"


def _operative_string_literals(path: Path) -> list[str]:
    """Every string constant in ``path`` except docstrings.

    Comments never reach the AST, and docstrings are skipped explicitly, so what
    is left is the strings the module actually *uses* — which is where a path
    literal would have to live. Keeps the source assertions below from failing
    on prose that documents the extraction.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch) -> Path:
    """A fake HOME carrying a complete Hermes install."""
    home = tmp_path / "fake-home"
    (home / ".hermes" / "memories").mkdir(parents=True)
    (home / ".hermes" / "memories" / "MEMORY.md").write_text(
        "[L0] infra: notes → knowledge/infra.md\n", encoding="utf-8"
    )
    (home / ".hermes" / "memories" / "USER.md").write_text("pref\n", encoding="utf-8")
    (home / ".hermes" / "config.yaml").write_text(
        "memory:\n  memory_char_limit: 3000\n  user_char_limit: 7000\n",
        encoding="utf-8",
    )
    (home / ".hermes" / "SOUL.md").write_text("# soul\n", encoding="utf-8")
    (home / ".hermes" / "sessions").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # conftest points this at its own sandbox; these tests exercise detection.
    monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
    monkeypatch.delenv("LAYERED_MEMORY_HERMES_DB", raising=False)
    monkeypatch.delenv("LAYERED_MEMORY_SESSIONS_DIR", raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MEMORY_MAX_CHARS", raising=False)
    return home


@pytest.fixture
def bare_home(tmp_path: Path, monkeypatch) -> Path:
    """A fake HOME with no agent product installed at all."""
    home = tmp_path / "empty-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", raising=False)
    monkeypatch.delenv("LAYERED_MEMORY_HERMES_DB", raising=False)
    monkeypatch.delenv("LAYERED_MEMORY_SESSIONS_DIR", raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MEMORY_MAX_CHARS", raising=False)
    return home


def _config(tmp_path: Path, **kwargs) -> MemoryConfig:
    return MemoryConfig(home=str(tmp_path / "lm-home"), **kwargs)


# ---------------------------------------------------------------------------
# 1. The Hermes adapter resolves what the core used to resolve
# ---------------------------------------------------------------------------


class TestHermesAdapterParity:
    def test_paths_match_the_legacy_literals(self, hermes_home):
        adapter = HermesEnvironment()
        assert adapter.detect_memory_path() == (
            hermes_home / ".hermes" / "memories" / "MEMORY.md"
        )
        assert adapter.detect_state_db() == hermes_home / ".hermes" / "state.db"
        assert adapter.detect_sessions_dir() == hermes_home / ".hermes" / "sessions"
        assert adapter.soul_path() == hermes_home / ".hermes" / "SOUL.md"
        assert adapter.config_path() == hermes_home / ".hermes" / "config.yaml"

    def test_separator_is_the_hermes_convention(self, hermes_home):
        adapter = HermesEnvironment()
        assert adapter.default_separator() == HERMES_SEPARATOR == "§"
        memory = hermes_home / ".hermes" / "memories" / "MEMORY.md"
        assert adapter.memory_separator(memory) == "§"

    def test_separator_declines_for_an_unrecognised_file(self, hermes_home):
        """The pre-adapter heuristic keyed off 'memory' in the file name.

        Declining (None) rather than asserting '§' is what lets an operator's
        configured separator keep winning for some other file.
        """
        assert HermesEnvironment().memory_separator(Path("/tmp/notes.txt")) is None

    def test_state_db_env_override_still_wins(self, hermes_home, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_HERMES_DB", str(hermes_home / "custom.db"))
        assert HermesEnvironment().detect_state_db() == hermes_home / "custom.db"

    def test_product_char_limit_is_read_back_from_hermes_config(self, hermes_home):
        adapter = HermesEnvironment()
        memory = hermes_home / ".hermes" / "memories" / "MEMORY.md"
        user = hermes_home / ".hermes" / "memories" / "USER.md"
        assert adapter.memory_char_limit(memory) == 3000
        assert adapter.memory_char_limit(user) == 7000

    def test_default_char_limit_is_the_hermes_builtin(self, hermes_home):
        adapter = HermesEnvironment()
        assert (
            adapter.default_memory_char_limit(Path("/x/.hermes/memories/MEMORY.md"))
            == HERMES_DEFAULT_MEMORY_MAX_CHARS
            == 2000
        )
        # Not a Hermes-shaped file → no opinion, so the generic default applies.
        assert adapter.default_memory_char_limit(Path("/x/notes.txt")) is None

    def test_satisfies_the_protocol(self):
        assert isinstance(HermesEnvironment(), AgentEnvironmentAdapter)
        assert isinstance(ClaudeCodeEnvironment(), AgentEnvironmentAdapter)
        assert isinstance(EnvironmentAdapterBase(), AgentEnvironmentAdapter)


# ---------------------------------------------------------------------------
# 2. The core goes *through* the adapter layer
# ---------------------------------------------------------------------------


class TestCoreUsesTheAdapterLayer:
    def test_config_detects_the_hermes_memory_file(self, hermes_home, tmp_path):
        cfg = _config(tmp_path)
        assert cfg.detect_agent_memory_path() == (
            hermes_home / ".hermes" / "memories" / "MEMORY.md"
        )
        assert cfg.describe_agent_environment()["resolved"] == "hermes"

    def test_config_separator_and_state_db_come_from_the_adapter(
        self, hermes_home, tmp_path
    ):
        cfg = _config(tmp_path)
        assert cfg.agent_memory_separator == "§"
        assert cfg.detect_agent_memory_separator() == "§"
        assert cfg.hermes_db_path == hermes_home / ".hermes" / "state.db"
        assert cfg.sessions_dir == hermes_home / ".hermes" / "sessions"

    def test_a_registered_adapter_changes_what_the_core_resolves(
        self, bare_home, tmp_path, monkeypatch
    ):
        """The proof there is no literal left: swap the registry, the core moves.

        A fictional product with its own layout and separator. No core file
        mentions it, and nothing in config.py or memory_compactor.py is edited —
        registering is the whole integration.
        """
        memory = bare_home / ".acme" / "brain.txt"
        memory.parent.mkdir(parents=True)
        memory.write_text("entry one\n~\nentry two\n", encoding="utf-8")

        class AcmeEnvironment(EnvironmentAdapterBase):
            id = "acme"
            display_name = "ACME Agent"
            agent_type = "acme"
            memory_relpath = (".acme", "brain.txt")
            install_marker_relpath = (".acme",)
            state_db_relpath = (".acme", "sessions.sqlite")
            separator = "~"
            path_tokens = (".acme", "brain.txt")

            def memory_separator(self, memory_path=None):
                return "~"

            def default_memory_char_limit(self, memory_path=None):
                return 1234

        monkeypatch.setattr(env_mod, "environments", EnvironmentRegistry())
        env_mod.environments.register(AcmeEnvironment(), priority=10)
        env_mod.environments.register(env_mod.GenericEnvironment(), priority=90)

        cfg = _config(tmp_path)
        assert cfg.detect_agent_memory_path() == memory
        assert cfg.detect_agent_memory_separator() == "~"
        assert cfg.hermes_db_path == bare_home / ".acme" / "sessions.sqlite"
        assert _resolve_separator(memory, cfg) == "~"
        assert _get_memory_max_chars(config=cfg, memory_path=memory) == 1234
        assert cfg.describe_agent_environment()["resolved"] == "acme"

    def test_compactor_char_limit_chain_prefers_the_product_config(
        self, hermes_home, tmp_path
    ):
        cfg = _config(tmp_path)
        memory = hermes_home / ".hermes" / "memories" / "MEMORY.md"
        # 3 — the adapter's dynamic read of the agent's own config.yaml.
        assert _get_memory_max_chars(config=cfg, memory_path=memory) == 3000

    def test_compactor_char_limit_falls_back_to_the_adapter_default(
        self, hermes_home, tmp_path
    ):
        (hermes_home / ".hermes" / "config.yaml").unlink()
        cfg = _config(tmp_path)
        memory = hermes_home / ".hermes" / "memories" / "MEMORY.md"
        # 4 — no product config to read, so the adapter's own default.
        assert _get_memory_max_chars(config=cfg, memory_path=memory) == 2000

    def test_compactor_char_limit_falls_back_to_the_generic_default(
        self, bare_home, tmp_path
    ):
        cfg = _config(tmp_path)
        # 5 — no adapter opinion at all. The path is a literal rather than a
        # tmp_path child on purpose: TMPDIR itself can sit under ~/.hermes on a
        # Hermes host, and the product heuristic (kept verbatim from the
        # pre-adapter compactor) matches "hermes" anywhere in the path.
        assert _get_memory_max_chars(config=cfg, memory_path="/srv/app/notes.txt") == 50_000

    def test_explicit_limit_still_outranks_the_adapter(self, hermes_home, tmp_path):
        cfg = _config(tmp_path, memory_char_limit=9999)
        memory = hermes_home / ".hermes" / "memories" / "MEMORY.md"
        assert _get_memory_max_chars(config=cfg, memory_path=memory) == 9999

    def test_integration_detection_is_adapter_driven(self, hermes_home):
        info = agent_integrator.detect_agent_type()
        assert info == integration_environment()
        assert info["agent_type"] == "hermes"
        assert info["home_dir"] == hermes_home / ".hermes"
        assert info["soul_path"] == hermes_home / ".hermes" / "SOUL.md"

    def test_integration_detection_reports_generic_for_an_unknown_host(self, bare_home):
        info = agent_integrator.detect_agent_type()
        assert info == {
            "agent_type": "generic_mcp",
            "soul_path": None,
            "memory_path": None,
            "home_dir": None,
        }

    def test_core_sources_name_no_product_paths(self):
        """Targeted source assertion: the literals that moved must be gone.

        Scoped to the core modules, and to *operative* string literals only —
        docstrings and comments are excluded, because a docstring explaining
        what moved where is the documentation this batch is meant to leave
        behind, not a leak. The Hermes adapter is excluded entirely: naming
        Hermes is its whole job.
        """
        forbidden = (".hermes", "memory.md", "claude.md", "soul.md", ".cursorrules", ".cline")
        for name in _CORE_SOURCES:
            for literal in _operative_string_literals(_SRC / name):
                lowered = literal.lower()
                for bad in forbidden:
                    assert bad not in lowered, (
                        f"{name} still carries the product path literal {literal!r}"
                    )

    def test_core_sources_hold_no_separator_literal(self):
        """'§' is a Hermes convention and now lives with the Hermes adapter."""
        for name in _CORE_SOURCES:
            for literal in _operative_string_literals(_SRC / name):
                assert "§" not in literal, (
                    f"{name} still hardcodes the '§' separator in {literal!r}"
                )

    def test_api_server_resolves_the_framework_layout(self, tmp_path, monkeypatch):
        """The REST server's stores follow `MemoryConfig`, not a product path.

        Same layout as every other entry point: `knowledge_dir` from
        `LAYERED_MEMORY_HOME` (or an explicit override), the SQLite stores in
        `<home>/data`. Skipped where FastAPI is not installed — the REST layer
        is optional, the boundary assertion above is not.
        """
        pytest.importorskip("fastapi")
        from layered_memory_mcp.api.server import store_dirs

        home = tmp_path / "api-home"
        monkeypatch.setenv("LAYERED_MEMORY_HOME", str(home))
        knowledge_dir, data_dir = store_dirs()
        assert knowledge_dir == home / "knowledge"
        assert data_dir == home / "data"

        explicit = MemoryConfig(
            home=str(tmp_path / "other"), knowledge_dir=str(tmp_path / "kb")
        )
        assert store_dirs(explicit) == (tmp_path / "kb", tmp_path / "other" / "data")


# ---------------------------------------------------------------------------
# 3. Generic / none / unknown must not reach a product path
# ---------------------------------------------------------------------------


class TestGenericAdapterDoesNotProbeProducts:
    @pytest.mark.parametrize("token", ["generic", "none", "off", "disabled"])
    def test_generic_ignores_an_installed_hermes(self, hermes_home, tmp_path, token):
        cfg = _config(tmp_path, agent_memory={"adapter": token})
        assert configured_adapter_id(cfg) == GENERIC_ID
        assert resolve_environment(cfg).id == GENERIC_ID
        assert cfg.detect_agent_memory_path() is None
        assert cfg.hermes_db_path is None
        assert cfg.sessions_dir is None
        assert cfg.agent_memory_separator == "\n\n"

    def test_generic_still_resolves_the_framework_owned_fallback(
        self, hermes_home, tmp_path
    ):
        cfg = _config(tmp_path, agent_memory={"adapter": "generic"})
        fallback = cfg.home / "agent-memory.md"
        fallback.write_text("x\n", encoding="utf-8")
        assert cfg.detect_agent_memory_path() == fallback

    def test_unknown_adapter_falls_back_to_generic_not_hermes(
        self, hermes_home, tmp_path, caplog
    ):
        cfg = _config(tmp_path, agent_memory={"adapter": "does-not-exist"})
        with caplog.at_level("WARNING"):
            resolved = resolve_environment(cfg)
        assert resolved.id == GENERIC_ID
        assert cfg.detect_agent_memory_path() is None
        assert "does-not-exist" in caplog.text

    def test_explicit_adapter_id_pins_without_probing(self, bare_home, tmp_path):
        """`adapter: hermes` is honoured even with nothing installed.

        Pinning must not silently degrade: the operator said Hermes, so Hermes'
        state DB path is reported even though the file is absent — which is what
        makes the "no session source at <path>" diagnostic useful.
        """
        cfg = _config(tmp_path, agent_memory={"adapter": "hermes"})
        assert resolve_environment(cfg).id == "hermes"
        assert cfg.hermes_db_path == bare_home / ".hermes" / "state.db"
        assert cfg.detect_agent_memory_path() is None  # the file does not exist

    def test_env_var_selects_the_adapter(self, hermes_home, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_AGENT_ADAPTER", "generic")
        cfg = _config(tmp_path)
        assert cfg.agent_adapter == "generic"
        assert cfg.detect_agent_memory_path() is None

    def test_default_is_auto(self, hermes_home, tmp_path):
        cfg = _config(tmp_path)
        assert cfg.agent_adapter == "auto"
        assert configured_adapter_id(cfg) == AUTO


# ---------------------------------------------------------------------------
# 4. Adapter-resolved paths are still not write permission
# ---------------------------------------------------------------------------


class TestGuardStillBlocksAdapterResolvedWrites:
    def test_temp_home_cannot_write_the_adapter_detected_memory_file(
        self, hermes_home, tmp_path
    ):
        from layered_memory_mcp.agent_memory_guard import (
            UNSAFE_AUTO_DETECT,
            resolve_write_target,
        )

        cfg = _config(tmp_path)
        detected = cfg.detect_agent_memory_path()
        assert detected == hermes_home / ".hermes" / "memories" / "MEMORY.md"

        target = resolve_write_target(cfg)
        assert target.allowed is False
        assert target.reason == UNSAFE_AUTO_DETECT
        assert target.path == detected  # reported, not written

    def test_explicit_target_is_still_allowed(self, hermes_home, tmp_path):
        from layered_memory_mcp.agent_memory_guard import resolve_write_target

        explicit = hermes_home / ".hermes" / "memories" / "MEMORY.md"
        cfg = _config(tmp_path, agent_memory={"path": str(explicit)})
        target = resolve_write_target(cfg)
        assert target.allowed is True
        assert target.trust == "explicit_config"
        assert target.path == explicit

    def test_generic_adapter_leaves_nothing_to_refuse(self, hermes_home, tmp_path):
        from layered_memory_mcp.agent_memory_guard import UNRESOLVED, resolve_write_target

        cfg = _config(tmp_path, agent_memory={"adapter": "generic"})
        target = resolve_write_target(cfg)
        assert target.allowed is False
        assert target.reason == UNRESOLVED
        assert target.path is None


# ---------------------------------------------------------------------------
# 5. Capability reflection
# ---------------------------------------------------------------------------


class TestCapabilityReflection:
    def test_reports_configured_and_resolved_ids(self, hermes_home, tmp_path):
        caps = surface_capabilities(_config(tmp_path)).to_dict()
        env = caps["agent_environment"]
        assert env["configured"] == "auto"
        assert env["resolved"] == "hermes"
        assert env["entry_separator"] == "§"
        assert env["supports_write_guard"] is True
        assert "generic" in env["known"]

    def test_reports_a_pinned_generic_adapter(self, hermes_home, tmp_path):
        cfg = _config(tmp_path, agent_memory={"adapter": "none"})
        env = surface_capabilities(cfg).to_dict()["agent_environment"]
        assert env["configured"] == "generic"
        assert env["resolved"] == "generic"
        assert env["supports_write_guard"] is False

    def test_extension_adapter_field_is_a_different_thing(self, hermes_home, tmp_path):
        """``adapter`` names an extension-action handler bundle, not this layer."""
        caps = surface_capabilities(_config(tmp_path)).to_dict()
        assert caps["adapter"] is None
        assert caps["agent_environment"]["resolved"] == "hermes"

    def test_describe_environment_never_raises(self, tmp_path, monkeypatch):
        class Exploding(EnvironmentRegistry):
            def ordered(self):
                raise RuntimeError("boom")

            def get(self, adapter_id):
                raise RuntimeError("boom")

        monkeypatch.setattr(env_mod, "environments", Exploding())
        assert describe_environment(None)["resolved"] is None


# ---------------------------------------------------------------------------
# 6. Nothing about routing moved
# ---------------------------------------------------------------------------


#: Items spanning the surfaces the classifier can reach. The *expected* surface
#: is deliberately not asserted here (test_v350_routing owns that contract);
#: what matters is that the answer does not move when the adapter does.
_ROUTING_SAMPLES = (
    "用户偏好：回答请用中文，先给结论。",
    "重启流程：1. 停止服务 2. 清理缓存 3. 运行 `make restart` 4. 验证健康检查",
    "待办：等待用户确认是否迁移到新网关，被阻塞。",
    "代理架构：网关在 8080 端口终止 TLS，后端服务通过内网直连，证书由 acme 续期。",
    "ok",
)


class TestRoutingUnchanged:
    @pytest.mark.parametrize("text", _ROUTING_SAMPLES)
    def test_classification_is_adapter_independent(self, hermes_home, tmp_path, text):
        """Which agent's files the framework can see must not steer routing."""
        baseline = classify_memory_item(text, config=None)
        for token in ("auto", "hermes", "claude_code", "generic", "none"):
            cfg = _config(tmp_path, agent_memory={"adapter": token})
            decision = classify_memory_item(text, config=cfg)
            assert decision.surface is baseline.surface, token
            assert decision.ownership is baseline.ownership, token
            assert decision.score == baseline.score, token
            assert decision.confidence_basis == baseline.confidence_basis, token

    def test_surface_sets_are_untouched(self, hermes_home, tmp_path):
        caps = surface_capabilities(_config(tmp_path)).to_dict()
        assert set(caps["core_surfaces"]) == {
            "l0_index",
            "l1_knowledge",
            "todo",
            "memory_changelog",
        }
        assert set(caps["extension_surfaces"]) == {"skill", "user_profile", "agent_other"}


# ---------------------------------------------------------------------------
# 7. Registry mechanics
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_builtins_are_registered_in_probe_order(self):
        assert environments.ids() == [
            "hermes",
            "claude_code",
            "cursor",
            "cline",
            "generic",
        ]

    def test_registration_requires_an_id(self):
        class Nameless(EnvironmentAdapterBase):
            id = ""

        with pytest.raises(ValueError, match="non-empty 'id'"):
            EnvironmentRegistry().register(Nameless())

    def test_priority_decides_probe_order(self):
        class A(EnvironmentAdapterBase):
            id = "a"

        class B(EnvironmentAdapterBase):
            id = "b"

        registry = EnvironmentRegistry()
        registry.register(A(), priority=30)
        registry.register(B(), priority=10)
        assert registry.ids() == ["b", "a"]

    def test_unregister_removes_an_adapter(self):
        registry = EnvironmentRegistry()
        registry.register(HermesEnvironment(), priority=10)
        registry.unregister("hermes")
        assert registry.get("hermes") is None
        assert registry.ids() == []

    def test_claims_path_identifies_a_product_by_layout(self):
        hermes = HermesEnvironment()
        assert hermes.claims_path("/root/.hermes/memories/MEMORY.md")
        assert hermes.claims_path("/anywhere/MEMORY.md")
        assert hermes.claims_path("/anywhere/USER.md")
        assert not hermes.claims_path("/root/.claude/CLAUDE.md")
        assert not hermes.claims_path(None)
        assert ClaudeCodeEnvironment().claims_path("/root/.claude/CLAUDE.md")
        # The generic adapter claims nothing — that is what makes it the safe
        # fallback for an unknown id.
        assert not env_mod.GenericEnvironment().claims_path("/root/.hermes/x")

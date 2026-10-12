"""Hermes Agent environment adapter — the backward-compatible default.

Everything the core used to hardcode about Hermes lives here: the
``~/.hermes`` layout, the ``'§'`` entry separator, the 2000-char memory
fallback, the dynamic read of ``memory.{memory,user}_char_limit`` out of
Hermes' own ``config.yaml``, the ``SOUL.md`` persona target and the
``plugins/`` layout the write guard deploys into.

This adapter is registered first (priority 10) and therefore keeps an existing
install resolving exactly the paths it resolved before the extraction. Nothing
here is reachable from the core except through
:class:`..environment.AgentEnvironmentAdapter`, so removing Hermes support
would be an unregistration rather than a refactor.

What stays outside this module, on purpose:

* :mod:`...write_guard` — the plugin payload, its installer and the
  ``hermes`` CLI probing. It is already a self-contained Hermes support
  package; this adapter only points at it (:meth:`HermesEnvironment.write_guard`)
  so the core never has to name it.
* :mod:`...session_scanner` — reads the ``state.db`` schema. It takes the path
  from this adapter but owns the SQL.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .environment import EnvironmentAdapterBase

logger = logging.getLogger("layered_memory_mcp.adapters.hermes")

#: Directory Hermes keeps everything in, relative to the user's home.
HERMES_DIRNAME = ".hermes"

#: Entry separator in a Hermes ``MEMORY.md`` / ``USER.md``.
HERMES_SEPARATOR = "§"

#: Hermes' own built-in memory limit. Used only as a fallback: the real value is
#: read from its ``config.yaml`` whenever that is available, because a user who
#: raised 2000 → 4000 must not have the framework plan against the old number.
#: Erring *low* is deliberate — over-estimating capacity silently skips
#: compaction, which is the failure mode that lets a memory file overflow.
HERMES_DEFAULT_MEMORY_MAX_CHARS = 2_000

#: Env var Hermes sets in the MCP server's environment.
HERMES_CONFIG_ENV = "HERMES_CONFIG_PATH"

#: Env var overriding the state DB location (predates this adapter).
HERMES_STATE_DB_ENV = "LAYERED_MEMORY_HERMES_DB"

#: Injected into Hermes' persona file by ``init_framework``. Hermes-native: it
#: names ``SOUL.md`` and ``MEMORY.md``, which is why it is not in the core.
SOUL_BLOCK = """{begin_marker} by init_framework v{version} on {timestamp} -->

## 记忆框架规则 (layered-memory-mcp)

每个 session 开始时，调用 `get_l0_index()` 获取完整 L0 索引。
L0.md 由 MCP Server 自动维护，是权威的知识地图。
不要依赖 MEMORY.md 中的 [L0] 条目——它们可能过时。

遇问题 → 扫 L0 → recall_knowledge 检索 L1 → 读懂了再行动。
写入知识用 inject_knowledge，它自动处理去重、分段、L0 同步。

{end_marker}
"""


class HermesEnvironment(EnvironmentAdapterBase):
    """Hermes Agent: ``~/.hermes`` layout, ``§``-separated memory."""

    id = "hermes"
    display_name = "Hermes Agent"
    agent_type = "hermes"

    memory_relpath = (HERMES_DIRNAME, "memories", "MEMORY.md")
    # config.yaml, not the directory: a bare ~/.hermes can be left behind by an
    # uninstall, and the pre-adapter detect_agent_type() required the config
    # file before claiming the host was Hermes.
    install_marker_relpath = (HERMES_DIRNAME, "config.yaml")
    state_db_relpath = (HERMES_DIRNAME, "state.db")
    sessions_relpath = (HERMES_DIRNAME, "sessions")
    separator = HERMES_SEPARATOR

    # Matches both the directory layout and the two file names Hermes uses, so
    # an explicitly configured path is recognised even outside ~/.hermes (the
    # heuristic the compactor applied before the extraction).
    path_tokens = ("hermes", "memory.md", "user.md")

    # -- paths ------------------------------------------------------------

    def home_dir(self, home: Path | None = None) -> Path:
        """Hermes' own home directory (``~/.hermes``)."""
        return (Path(home) if home else Path.home()) / HERMES_DIRNAME

    def soul_path(self, home: Path | None = None) -> Path:
        """Hermes' persona file."""
        return self.home_dir(home) / "SOUL.md"

    def config_path(self, home: Path | None = None) -> Path | None:
        """Hermes' ``config.yaml``, if it can be found.

        ``HERMES_CONFIG_PATH`` first — Hermes sets it in the MCP server's env,
        and it is authoritative for a non-standard install.
        """
        explicit = os.environ.get(HERMES_CONFIG_ENV)
        if explicit:
            candidate = Path(explicit).expanduser()
            if candidate.is_file():
                return candidate
        standard = self.home_dir(home) / "config.yaml"
        return standard if standard.is_file() else None

    def detect_state_db(self, home: Path | None = None) -> Path | None:
        """``~/.hermes/state.db``, or the env override.

        Not existence-checked (see the base class): callers report the path in
        their "no session source" diagnostic.
        """
        env = os.environ.get(HERMES_STATE_DB_ENV)
        if env:
            return Path(env).expanduser()
        return super().detect_state_db(home)

    # -- memory file shape ------------------------------------------------

    def memory_separator(self, memory_path: Path | None = None) -> str | None:
        """``§`` for a Hermes-style memory file; no opinion otherwise.

        The file-name test (``'memory' in name``) is the pre-adapter heuristic
        kept verbatim: ``MEMORY.md`` and ``memories/MEMORY.md`` are §-separated,
        while some other file an operator pointed ``agent_memory.path`` at may
        not be, and must keep falling through to the configured separator.
        """
        if memory_path is None:
            return HERMES_SEPARATOR
        if "memory" in Path(memory_path).name.lower():
            return HERMES_SEPARATOR
        return None

    def memory_char_limit(self, memory_path: Path | None = None) -> int | None:
        """The limit Hermes itself enforces, read from its ``config.yaml``.

        Two independent keys under ``memory:``:
          * ``memory_char_limit`` → ``MEMORY.md``
          * ``user_char_limit``   → ``USER.md``

        Reading them keeps the framework in step with whatever the user
        actually configured instead of planning against a guess.
        """
        cfg_path = self.config_path()
        if cfg_path is None:
            return None
        try:
            import yaml

            with cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            section = data.get("memory") or {}
            key = (
                "user_char_limit"
                if self.is_user_profile_path(memory_path)
                else "memory_char_limit"
            )
            value = section.get(key)
            if value is not None:
                limit = int(value)
                if limit > 0:
                    return limit
        except Exception as exc:  # noqa: BLE001 — never break maintenance
            logger.debug("could not read Hermes memory limit from %s: %s", cfg_path, exc)
        return None

    def default_memory_char_limit(self, memory_path: Path | None = None) -> int | None:
        """2000 for a recognised Hermes memory file, else no opinion."""
        return HERMES_DEFAULT_MEMORY_MAX_CHARS if self.claims_path(memory_path) else None

    @staticmethod
    def is_user_profile_path(path: "str | Path | None") -> bool:
        """Whether ``path`` is the USER profile rather than MEMORY.md."""
        if not path:
            return False
        return "user.md" in str(path).lower()

    # -- integration ------------------------------------------------------

    def describe_integration(self, home: Path | None = None) -> dict[str, Any]:
        """Persona/memory injection targets, or the generic shape.

        ``config.yaml`` must exist before this claims the host is Hermes, and
        ``soul_path`` / ``memory_path`` are reported only when the files
        themselves exist — both conditions kept from the pre-adapter probe.
        """
        hermes_home = self.home_dir(home)
        if not (hermes_home / "config.yaml").exists():
            return super().describe_integration(home)
        soul = self.soul_path(home)
        memory = hermes_home / "memories" / "MEMORY.md"
        return {
            "agent_type": self.agent_type,
            "soul_path": soul if soul.exists() else None,
            "memory_path": memory if memory.exists() else None,
            "home_dir": hermes_home,
        }

    @staticmethod
    def write_guard():
        """The Hermes write-guard support package (imported lazily).

        Lets a caller reach the installer without the core naming
        :mod:`...write_guard` — the plugin layout, its payload and the ``hermes``
        CLI probing all stay in that package.
        """
        from .. import write_guard

        return write_guard

    def describe_capabilities(self) -> dict[str, Any]:
        caps = super().describe_capabilities()
        caps.update(
            {
                "reads_product_memory_limit": True,
                "default_memory_char_limit": HERMES_DEFAULT_MEMORY_MAX_CHARS,
                "supports_persona_injection": True,
                "supports_write_guard": True,
            }
        )
        return caps


__all__ = [
    "HERMES_CONFIG_ENV",
    "HERMES_DEFAULT_MEMORY_MAX_CHARS",
    "HERMES_DIRNAME",
    "HERMES_SEPARATOR",
    "HERMES_STATE_DB_ENV",
    "HermesEnvironment",
    "SOUL_BLOCK",
]

"""Adapter/support layer for the *native agent environment*.

WHY THIS MODULE EXISTS
----------------------
The common core owns L0 / L1 / TODO / MEMORY_CHANGELOG semantics. It does not
own — and after this version no longer knows — *where* a particular agent
product keeps its own memory file, what separates the entries inside it, where
its live-session database lives, or how many characters it allows. Those are
product facts, and they used to be literals sitting in the core:
``~/.hermes/memories/MEMORY.md`` and ``~/.hermes/state.db`` in ``config.py``,
the ``'§'`` separator heuristic in ``config.py`` *and* ``memory_compactor.py``,
the 2000-char fallback and the ``~/.hermes/config.yaml`` reader in
``memory_compactor.py``, the ``~/.hermes/SOUL.md`` probe in
``agent_integrator.py``.

An :class:`AgentEnvironmentAdapter` answers those questions instead. Hermes is
one adapter (:mod:`.hermes`) and stays the first one probed, so an existing
install resolves exactly the paths it always did. Supporting a second agent is
now a registration — not a patch to the router, the compactor or the config.

SCOPE — READ/DESCRIBE, NOT WRITE
--------------------------------
This is the *environment* adapter: it tells the framework where the agent's
native files are and what shape they have. It never writes them. Whether a
derived write into an agent-owned file is permitted at all is a separate
question answered by :mod:`..agent_memory_guard` (for the L0 projection) and by
:class:`..adapters.base.AgentExtensionAdapter` (for Skill / Profile actions).
Resolving a path through an adapter grants no permission to write it.

HOW THE ADAPTER IS CHOSEN
-------------------------
``agent_memory.adapter`` (or ``LAYERED_MEMORY_AGENT_ADAPTER``) names it:

    agent_memory:
      adapter: auto      # default — probe the registry, Hermes first

* ``auto`` (the default) reproduces the legacy probe chain exactly: the
  configured memory path is matched against each adapter's own layout, then
  each adapter is asked whether its memory file exists, then whether the
  product is installed at all. Hermes has the lowest priority number, so an
  existing Hermes install resolves to the Hermes adapter as it always did.
* an explicit id (``hermes``, ``claude_code``, ``cursor``, ``cline``,
  ``generic``) pins one adapter and skips probing.
* ``none`` / ``off`` is an alias for ``generic``: the framework then knows
  about no product paths at all and only ever looks inside its own home.
* an **unknown** id is a loud fallback to ``generic`` — a warning plus
  product-path probing switched off. It must never resolve silently to Hermes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger("layered_memory_mcp.adapters.environment")

#: Config token meaning "probe the registry" — the backward-compatible default.
AUTO = "auto"

#: Id of the adapter that knows no product paths. Also the fallback for an
#: unknown id, so a typo can never be answered with somebody else's memory file.
GENERIC_ID = "generic"

#: Environment variable form of ``agent_memory.adapter``.
ADAPTER_ENV_VAR = "LAYERED_MEMORY_AGENT_ADAPTER"

#: Tokens accepted as "no product adapter".
_NONE_TOKENS = ("none", "off", "disabled", "null")

#: Tokens accepted as "probe the registry".
_AUTO_TOKENS = ("", AUTO, "detect", "autodetect")

#: Separator used when an adapter has no opinion — a blank line, which is what
#: every markdown-ish memory file degrades to safely.
DEFAULT_SEPARATOR = "\n\n"

#: Name of the memory file the *framework* owns inside its own home. This one
#: is not a product path: it is the fallback the generic adapter writes about,
#: and the only agent-memory file the write guard trusts without an opt-in.
FRAMEWORK_MEMORY_FILENAME = "agent-memory.md"

#: Agent type reported for a host with no recognised native memory layout.
GENERIC_AGENT_TYPE = "generic_mcp"


@runtime_checkable
class AgentEnvironmentAdapter(Protocol):
    """What the core may ask about the agent hosting it.

    Every method is read-only and must tolerate a missing product: return
    ``None`` rather than raising when there is nothing to report. ``None`` from
    :meth:`memory_separator` / :meth:`memory_char_limit` means "no opinion", and
    the caller keeps its own configured fallback — it does not mean "empty".
    """

    #: Stable id used in config and in capability reflection.
    id: str
    #: Human-readable product name.
    display_name: str

    def claims_path(self, path: str | Path | None) -> bool:
        """Whether ``path`` has this product's file layout."""
        ...

    def is_present(self, home: Path | None = None) -> bool:
        """Whether the product looks installed under ``home``."""
        ...

    def detect_memory_path(
        self, home: Path | None = None, framework_home: Path | None = None
    ) -> Path | None:
        """The agent's native memory file, if it exists."""
        ...

    def detect_state_db(self, home: Path | None = None) -> Path | None:
        """The product's live-session database, if it has one."""
        ...

    def detect_sessions_dir(self, home: Path | None = None) -> Path | None:
        """The product's session-dump directory, if it exists."""
        ...

    def default_separator(self) -> str:
        """Entry separator to assume when nothing is configured."""
        ...

    def memory_separator(self, memory_path: Path | None = None) -> str | None:
        """Separator for ``memory_path``, or ``None`` for "no opinion"."""
        ...

    def memory_char_limit(self, memory_path: Path | None = None) -> int | None:
        """Limit the product itself enforces, read from *its* config."""
        ...

    def default_memory_char_limit(self, memory_path: Path | None = None) -> int | None:
        """Fallback limit when the product's config cannot be read."""
        ...

    def describe_integration(self, home: Path | None = None) -> dict[str, Any]:
        """Persona/memory injection targets — see :mod:`..agent_integrator`."""
        ...

    def describe_capabilities(self) -> dict[str, Any]:
        """Static facts about this adapter, for capability reflection."""
        ...


class EnvironmentAdapterBase:
    """Declarative adapter: state the file layout, inherit the behaviour.

    A product whose integration is "a memory file at a known place, separated a
    known way" needs no code — only the class attributes below. Hermes needs
    more than that (it has a config file with a real limit in it, a persona file
    and a plugin directory), which is why it subclasses and overrides.
    """

    id: str = GENERIC_ID
    display_name: str = "Generic MCP agent"

    #: Value :func:`..agent_integrator.detect_agent_type` reports for this host.
    agent_type: str = GENERIC_AGENT_TYPE

    #: Memory file, relative to the *user's* home. ``None`` → this adapter has
    #: no user-home memory file (the generic adapter, which looks in the
    #: framework's own home instead).
    memory_relpath: tuple[str, ...] | None = None

    #: Memory file relative to the *working directory* (project-scoped products
    #: such as Cursor keep their rules file in the repo, not in the home).
    cwd_relpath: tuple[str, ...] | None = None

    #: File whose presence proves the product is installed. ``None`` → fall back
    #: to the first component of :attr:`memory_relpath`.
    install_marker_relpath: tuple[str, ...] | None = None

    #: Live-session SQLite database, relative to the user's home.
    state_db_relpath: tuple[str, ...] | None = None

    #: Session-dump directory, relative to the user's home.
    sessions_relpath: tuple[str, ...] | None = None

    #: Entry separator this product uses.
    separator: str = DEFAULT_SEPARATOR

    #: Lowercase substrings that identify a path as belonging to this product.
    #: Used when the operator configured an explicit memory path: the layout of
    #: the path they named tells us which product it is, without a probe.
    path_tokens: tuple[str, ...] = ()

    # -- identity ---------------------------------------------------------

    def claims_path(self, path: str | Path | None) -> bool:
        if not path or not self.path_tokens:
            return False
        text = str(path).lower()
        return any(token in text for token in self.path_tokens)

    def is_present(self, home: Path | None = None) -> bool:
        marker = self.install_marker_relpath
        if marker is None:
            if not self.memory_relpath:
                return False
            marker = (self.memory_relpath[0],)
        return self._user_path(marker, home).exists()

    # -- paths ------------------------------------------------------------

    def _user_path(self, relpath: tuple[str, ...], home: Path | None = None) -> Path:
        base = Path(home) if home else Path.home()
        return base.joinpath(*relpath)

    def detect_memory_path(
        self, home: Path | None = None, framework_home: Path | None = None
    ) -> Path | None:
        if self.memory_relpath:
            candidate = self._user_path(self.memory_relpath, home)
            return candidate if candidate.exists() else None
        if self.cwd_relpath:
            candidate = Path.cwd().joinpath(*self.cwd_relpath)
            return candidate if candidate.exists() else None
        if framework_home:
            candidate = Path(framework_home) / FRAMEWORK_MEMORY_FILENAME
            return candidate if candidate.exists() else None
        return None

    def detect_state_db(self, home: Path | None = None) -> Path | None:
        """Path of the live-session DB — **not** existence-checked.

        Callers report this path in diagnostics ("no session source at ...") and
        test ``is_file()`` themselves, so returning the expected location of a
        missing file is more useful than returning ``None``.
        """
        if self.state_db_relpath is None:
            return None
        return self._user_path(self.state_db_relpath, home)

    def detect_sessions_dir(self, home: Path | None = None) -> Path | None:
        if self.sessions_relpath is None:
            return None
        candidate = self._user_path(self.sessions_relpath, home)
        return candidate if candidate.exists() else None

    # -- memory file shape ------------------------------------------------

    def default_separator(self) -> str:
        return self.separator

    def memory_separator(self, memory_path: Path | None = None) -> str | None:
        """No opinion by default — the configured separator wins.

        Deliberately *not* ``self.separator``: the pre-adapter core fell back to
        the configured ``agent_memory.separator`` for any file it did not
        recognise, and silently switching an existing install's separator would
        rewrite that install's memory file into one unparsed blob.
        """
        return None

    def memory_char_limit(self, memory_path: Path | None = None) -> int | None:
        return None

    def default_memory_char_limit(self, memory_path: Path | None = None) -> int | None:
        return None

    # -- reflection -------------------------------------------------------

    def describe_integration(self, home: Path | None = None) -> dict[str, Any]:
        """No native persona surface — the user wires the snippet in by hand."""
        return {
            "agent_type": GENERIC_AGENT_TYPE,
            "soul_path": None,
            "memory_path": None,
            "home_dir": None,
        }

    def describe_capabilities(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "agent_type": self.agent_type,
            "entry_separator": self.default_separator(),
            "detects_memory_path": bool(self.memory_relpath or self.cwd_relpath),
            "detects_state_db": self.state_db_relpath is not None,
            "detects_sessions_dir": self.sessions_relpath is not None,
            "reads_product_memory_limit": False,
            "supports_persona_injection": False,
            "supports_write_guard": False,
        }


class GenericEnvironment(EnvironmentAdapterBase):
    """The product-agnostic adapter: knows only the framework's own home.

    It claims no path, detects no product, and resolves the one agent-memory
    file the framework itself owns (``<home>/agent-memory.md``). This is what a
    non-Hermes deployment gets without registering anything, and what an
    unknown adapter id falls back to.
    """

    id = GENERIC_ID
    display_name = "Generic MCP agent"


class EnvironmentRegistry:
    """Priority-ordered id → adapter lookup.

    Priority is a sort key, not a rank: the lowest number is probed first.
    Built-ins leave gaps (10, 20, 30, 40, 90) so a registration can slot in
    between them without re-numbering anything.
    """

    def __init__(self, bootstrap: Callable[[], None] | None = None) -> None:
        self._adapters: dict[str, AgentEnvironmentAdapter] = {}
        self._priorities: dict[str, int] = {}
        #: Called once before the first read, to register the bundled adapters
        #: without this module having to import them at module level.
        self._bootstrap = bootstrap
        self._bootstrapped = bootstrap is None

    def _ensure_bootstrapped(self) -> None:
        if self._bootstrapped:
            return
        self._bootstrapped = True  # set first: a re-entrant call must not loop
        if self._bootstrap is not None:
            self._bootstrap()

    def register(self, adapter: AgentEnvironmentAdapter, *, priority: int = 50) -> None:
        adapter_id = str(getattr(adapter, "id", "") or "").strip()
        if not adapter_id:
            raise ValueError("environment adapter must expose a non-empty 'id'")
        self._adapters[adapter_id] = adapter
        self._priorities[adapter_id] = int(priority)

    def unregister(self, adapter_id: str) -> None:
        self._adapters.pop(str(adapter_id), None)
        self._priorities.pop(str(adapter_id), None)

    def get(self, adapter_id: str | None) -> AgentEnvironmentAdapter | None:
        if not adapter_id:
            return None
        self._ensure_bootstrapped()
        return self._adapters.get(str(adapter_id))

    def ids(self) -> list[str]:
        return [a.id for a in self.ordered()]

    def ordered(self) -> list[AgentEnvironmentAdapter]:
        """Adapters in probe order (lowest priority number first)."""
        self._ensure_bootstrapped()
        return [
            self._adapters[key]
            for key in sorted(
                self._adapters, key=lambda k: (self._priorities.get(k, 50), k)
            )
        ]


def _register_builtins() -> None:
    """Register the bundled adapters once, lazily.

    Lazy because the adapter modules import this one for the base class, and a
    module-level registration here would close that loop.
    """
    from .editors import ClaudeCodeEnvironment, ClineEnvironment, CursorEnvironment
    from .hermes import HermesEnvironment

    # Probe order matches the pre-adapter candidate list in
    # MemoryConfig.detect_agent_memory_path, so resolution is unchanged.
    environments.register(HermesEnvironment(), priority=10)
    environments.register(ClaudeCodeEnvironment(), priority=20)
    environments.register(CursorEnvironment(), priority=30)
    environments.register(ClineEnvironment(), priority=40)
    environments.register(GenericEnvironment(), priority=90)


#: Process-wide registry of environment adapters. Unlike the extension-action
#: registry in :mod:`.base`, this one ships populated: describing where an
#: agent's files are is read-only and carries none of the write risk that keeps
#: the extension registry empty by default.
environments = EnvironmentRegistry(bootstrap=_register_builtins)


def generic_environment() -> AgentEnvironmentAdapter:
    """The adapter that knows no product paths. Always available."""
    found = environments.get(GENERIC_ID)
    if found is None:  # pragma: no cover — only if somebody unregisters it
        found = GenericEnvironment()
        environments.register(found, priority=90)
    return found


def configured_adapter_id(config: Any | None = None) -> str:
    """The adapter id the operator asked for — ``"auto"`` when they did not.

    Resolution: ``config.agent_adapter`` (which ``MemoryConfig`` has already
    resolved from constructor → env var → ``config.yaml``) → the env var
    directly, for a duck-typed config that carries no such attribute → auto.
    """
    import os

    raw = getattr(config, "agent_adapter", None) if config is not None else None
    if not raw:
        raw = os.environ.get(ADAPTER_ENV_VAR)
    token = str(raw).strip().lower() if raw else ""
    if token in _AUTO_TOKENS:
        return AUTO
    if token in _NONE_TOKENS:
        return GENERIC_ID
    return token


def resolve_environment(
    config: Any | None = None,
    *,
    memory_path: str | Path | None = None,
    home: Path | None = None,
) -> AgentEnvironmentAdapter:
    """The adapter describing the agent this install is running under.

    Never raises and never returns ``None``: the worst case is the generic
    adapter, which knows no product paths. ``memory_path`` lets a caller that
    is already working on a specific file (the compactor) have the adapter
    chosen by that file's layout instead of by a filesystem probe.
    """
    requested = configured_adapter_id(config)
    if requested != AUTO:
        found = environments.get(requested)
        if found is not None:
            return found
        logger.warning(
            "unknown agent adapter %r — falling back to %r, which probes no "
            "product paths. Known adapters: %s",
            requested,
            GENERIC_ID,
            ", ".join(environments.ids()),
        )
        return generic_environment()

    candidates = environments.ordered()

    # 1. An explicitly configured memory path names the product by its layout.
    named = memory_path or getattr(config, "agent_memory_path", None)
    for adapter in candidates:
        if adapter.claims_path(named):
            return adapter

    framework_home = getattr(config, "home", None)

    # 2. Whose memory file actually exists — the legacy candidate chain.
    for adapter in candidates:
        try:
            if adapter.detect_memory_path(home=home, framework_home=framework_home):
                return adapter
        except OSError:  # pragma: no cover — probing must not break resolution
            continue

    # 3. Nothing to read yet, but the product is installed: still the right
    #    adapter for its state DB / sessions dir / persona file.
    for adapter in candidates:
        try:
            if adapter.is_present(home):
                return adapter
        except OSError:  # pragma: no cover
            continue

    return generic_environment()


def integration_environment(home: Path | None = None) -> dict[str, Any]:
    """Persona/memory injection targets for the detected host.

    Shape is the contract :func:`..agent_integrator.detect_agent_type` has
    published since v2.2: ``{agent_type, soul_path, memory_path, home_dir}``.
    The first adapter that recognises its own installation wins; a host nothing
    recognises reports the generic shape with every path ``None``.
    """
    for adapter in environments.ordered():
        try:
            info = adapter.describe_integration(home)
        except OSError:  # pragma: no cover — a probe must not break init
            continue
        if info and info.get("agent_type") != GENERIC_AGENT_TYPE:
            return info
    return generic_environment().describe_integration(home)


def describe_environment(config: Any | None = None) -> dict[str, Any]:
    """Capability reflection for the environment layer. Never raises."""
    try:
        adapter = resolve_environment(config)
        return {
            "configured": configured_adapter_id(config),
            "resolved": adapter.id,
            "known": environments.ids(),
            **adapter.describe_capabilities(),
        }
    except Exception as exc:  # noqa: BLE001 — reflection must never raise
        logger.debug("environment reflection failed: %s", exc)
        return {"configured": AUTO, "resolved": None, "known": []}


__all__ = [
    "ADAPTER_ENV_VAR",
    "AUTO",
    "AgentEnvironmentAdapter",
    "DEFAULT_SEPARATOR",
    "EnvironmentAdapterBase",
    "EnvironmentRegistry",
    "FRAMEWORK_MEMORY_FILENAME",
    "GENERIC_AGENT_TYPE",
    "GENERIC_ID",
    "GenericEnvironment",
    "configured_adapter_id",
    "describe_environment",
    "environments",
    "generic_environment",
    "integration_environment",
    "resolve_environment",
]

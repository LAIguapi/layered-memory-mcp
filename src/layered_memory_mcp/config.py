"""
Configuration for Layered Memory MCP Server.

All paths are configurable via environment variables or constructor arguments.
No hardcoded personal paths.
"""

import os
from pathlib import Path

import yaml


def default_home() -> Path:
    """Default home directory for the memory system.
    
    Priority:
      1. LAYERED_MEMORY_HOME env var
      2. ~/.layered-memory/
    """
    env = os.environ.get("LAYERED_MEMORY_HOME")
    if env:
        p = Path(env)
        if not p.is_absolute():
            raise ValueError(f"LAYERED_MEMORY_HOME must be an absolute path, got: {env}")
        return p
    return Path.home() / ".layered-memory"


def default_knowledge_dir(home: Path | None = None) -> Path:
    """Default directory for L1 knowledge files."""
    base = home or default_home()
    return base / "knowledge"


def default_sessions_dir() -> Path | None:
    """Try to auto-detect agent sessions directory.
    
    Supports:
      - Hermes Agent: ~/.hermes/sessions/
      - Custom: LAYERED_MEMORY_SESSIONS_DIR env var
    """
    env = os.environ.get("LAYERED_MEMORY_SESSIONS_DIR")
    if env:
        return Path(env)
    
    hermes_sessions = Path.home() / ".hermes" / "sessions"
    if hermes_sessions.exists():
        return hermes_sessions
    
    return None


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from an environment variable."""
    val = os.environ.get(name, "").lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(name: str, default: float) -> float:
    """Read a float from an environment variable."""
    val = os.environ.get(name)
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return default


class MemoryConfig:
    """Runtime configuration for the memory server."""
    
    def __init__(
        self,
        home: str | None = None,
        knowledge_dir: str | None = None,
        sessions_dir: str | None = None,
        l0_index_file: str | None = None,
        # v0.5.0 new fields
        auto_sync_l0: bool | None = None,
        dedup_threshold: float | None = None,
        l0_format: str | None = None,
        # v0.6.0 new fields
        namespace: str | None = None,
        # v0.7.0 new fields — compact / domain migration
        compact_domain_rules_file: str | None = None,
        compact_bloat_threshold: float | None = None,
        compact_capacity_warning_threshold: float | None = None,
    ):
        self.home = Path(home) if home else default_home()
        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else default_knowledge_dir(self.home)
        self.sessions_dir = Path(sessions_dir) if sessions_dir else default_sessions_dir()
        self.l0_index_file = Path(l0_index_file) if l0_index_file else None

        # v0.5.0: Auto-sync L0 index after writes (default: True)
        self.auto_sync_l0: bool = (
            auto_sync_l0 if auto_sync_l0 is not None
            else _env_bool("LAYERED_MEMORY_AUTO_SYNC_L0", True)
        )
        # v0.5.0: Dedup similarity threshold (default: 0.7)
        self.dedup_threshold: float = (
            dedup_threshold if dedup_threshold is not None
            else _env_float("LAYERED_MEMORY_DEDUP_THRESHOLD", 0.7)
        )
        # v0.5.0: L0 index format — "hermes" or "generic" (default: "hermes")
        _fmt = l0_format or os.environ.get("LAYERED_MEMORY_L0_FORMAT", "hermes")
        if _fmt not in ("hermes", "generic"):
            raise ValueError(f"Invalid l0_format: {_fmt!r}. Must be 'hermes' or 'generic'.")
        self.l0_format: str = _fmt
        
        # v0.6.0: Agent namespace for multi-agent isolation
        # "shared" means no isolation (backward compatible)
        _ns = namespace or os.environ.get("LAYERED_MEMORY_NAMESPACE", "shared")
        _ns = _ns.strip().lower()
        if _ns and not all(c.isalnum() or c in "-_" for c in _ns):
            raise ValueError(f"Invalid namespace: {_ns!r}. Use alphanumeric, hyphens, underscores only.")
        self.namespace: str = _ns

        # v0.7.0: Compact — path to YAML file with domain → keywords mapping
        _rules_file = (
            compact_domain_rules_file
            or os.environ.get("LAYERED_MEMORY_COMPACT_DOMAIN_RULES_FILE")
        )
        self.compact_domain_rules_file: Path | None = (
            Path(_rules_file) if _rules_file else None
        )

        # v0.7.0: Compact — bloat threshold (0–1, default 0.8)
        self.compact_bloat_threshold: float = (
            compact_bloat_threshold if compact_bloat_threshold is not None
            else _env_float("LAYERED_MEMORY_COMPACT_BLOAT_THRESHOLD", 0.8)
        )

        # v0.7.0: Compact — capacity warning threshold (0–1, default 0.9)
        self.compact_capacity_warning_threshold: float = (
            compact_capacity_warning_threshold
            if compact_capacity_warning_threshold is not None
            else _env_float("LAYERED_MEMORY_COMPACT_CAPACITY_WARNING_THRESHOLD", 0.9)
        )

        # v1.0: Agent memory adapter — auto-detect or explicit
        _amp = os.environ.get("LAYERED_MEMORY_AGENT_MEMORY_PATH")
        self.agent_memory_path: Path | None = Path(_amp) if _amp else None
        _ams = os.environ.get("LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR")
        self.agent_memory_separator: str = _ams if _ams else "§"

        # Resolve namespace-aware knowledge directories
        if self.namespace != "shared":
            self._knowledge_root = self.knowledge_dir  # base knowledge/
            self.knowledge_dir = self._knowledge_root / self.namespace
            self._shared_knowledge_dir = self._knowledge_root / "shared"
        else:
            self._knowledge_root = self.knowledge_dir
            self._shared_knowledge_dir = None  # already in knowledge_dir
        
        # Ensure directories exist
        self.home.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        if self._shared_knowledge_dir:
            self._shared_knowledge_dir.mkdir(parents=True, exist_ok=True)
    
    @property
    def knowledge_dirs(self) -> list[Path]:
        """All knowledge directories to search (namespace + shared).
        
        Order matters: namespace-specific first (higher priority),
        then shared.
        """
        dirs = [self.knowledge_dir]
        if self._shared_knowledge_dir and self._shared_knowledge_dir != self.knowledge_dir:
            dirs.append(self._shared_knowledge_dir)
        return dirs

    def detect_agent_memory_path(self) -> Path | None:
        """Auto-detect the agent's memory file path.

        Priority:
          1. Explicitly configured agent_memory_path
          2. Hermes Agent: ~/.hermes/memories/MEMORY.md
          3. Claude Code: ~/.claude/CLAUDE.md
          4. Cursor: ./.cursorrules
          5. Cline: ~/.cline/rules
          6. Generic fallback: <home>/agent-memory.md

        Returns Path if found or configured, None if nothing exists.
        """
        # 1. Explicit config
        if self.agent_memory_path:
            return self.agent_memory_path

        # 2-6. Auto-detect by existence
        candidates = [
            Path.home() / ".hermes" / "memories" / "MEMORY.md",     # Hermes
            Path.home() / ".claude" / "CLAUDE.md",                   # Claude Code
            Path.cwd() / ".cursorrules",                              # Cursor
            Path.home() / ".cline" / "rules",                         # Cline
            self.home / "agent-memory.md",                            # Generic fallback
        ]
        for p in candidates:
            if p.exists():
                return p

        return None

    def detect_agent_memory_separator(self, memory_path: Path | None = None) -> str:
        """Auto-detect the entry separator used in the agent's memory file.

        Hermes uses '§', most others use blank-line separators.
        Falls back to the configured separator.
        """
        path = memory_path or self.detect_agent_memory_path()
        if path and path.exists():
            name = path.name.lower()
            if "memory" in name and path.parent.name == "memories":
                return "§"  # Hermes convention
        return self.agent_memory_separator

    # ------------------------------------------------------------------
    # Domain-rule helpers
    # ------------------------------------------------------------------

    def load_domain_rules(self) -> dict[str, list[str]]:
        """Load domain migration rules from the configured YAML file.

        Returns a dict mapping ``domain_name -> [keywords]``.
        Returns an empty dict when no rules file is configured or the
        file does not exist.
        """
        if self.compact_domain_rules_file is None:
            return {}

        path = self.compact_domain_rules_file
        if not path.exists():
            return {}

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if not isinstance(data, dict):
            return {}

        rules: dict[str, list[str]] = {}
        for domain, keywords in data.items():
            if isinstance(keywords, list):
                rules[str(domain)] = [str(k) for k in keywords]
            elif isinstance(keywords, str):
                rules[str(domain)] = [keywords]
        return rules

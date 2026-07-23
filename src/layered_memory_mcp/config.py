"""
Configuration for Layered Memory MCP Server.

All paths are configurable via environment variables or constructor arguments.
No hardcoded personal paths.
"""

import json
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


def _env_int(name: str, default: int) -> int:
    """Read an int from an environment variable."""
    val = os.environ.get(name)
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return default


def _env_list(name: str, default: list[str]) -> list[str]:
    """Read a comma-separated list from an environment variable."""
    val = os.environ.get(name)
    if val:
        items = [x.strip() for x in val.split(",") if x.strip()]
        if items:
            return items
    return default


def _env_json_dict(name: str, default: dict[str, list[str]]) -> dict[str, list[str]]:
    """Read a JSON object (``{domain: [keyword, ...]}``) from an env variable.

    Malformed or non-object values fall back to ``default``. Values are
    coerced into lists of strings so downstream code sees a consistent shape.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return default
    if not isinstance(data, dict):
        return default
    result: dict[str, list[str]] = {}
    for domain, keywords in data.items():
        if isinstance(keywords, list):
            result[str(domain)] = [str(k) for k in keywords]
        elif isinstance(keywords, str):
            result[str(domain)] = [keywords]
    return result


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
        # v2.3.0 new fields — auto-maintain (write-triggered self-maintenance)
        auto_maintain: bool | None = None,
        auto_maintain_interval_days: float | None = None,
        # v2.7.0 new fields — critical safety-net + explicit memory limit
        compact_critical_threshold: float | None = None,
        memory_char_limit: int | None = None,
        # v2.10.0 new fields — promotion detector (same-topic clustering)
        promotion_enabled: bool | None = None,
        promotion_watch_domains: list[str] | None = None,
        promotion_min_sections: int | None = None,
        promotion_cluster_threshold: float | None = None,
        promotion_min_cluster_size: int | None = None,
        # v2.10.1 new field — user-configured domain classification for the
        # auto-extractor (framework ships zero business presets)
        domain_keywords: dict[str, list[str]] | None = None,
    ):
        self.home = Path(home) if home else default_home()
        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else default_knowledge_dir(self.home)
        self.sessions_dir = Path(sessions_dir) if sessions_dir else default_sessions_dir()
        self.l0_index_file = Path(l0_index_file) if l0_index_file else None
        # Fallback to environment variable if not passed explicitly
        if self.l0_index_file is None:
            env_l0 = os.environ.get("LAYERED_MEMORY_L0_INDEX_FILE")
            if env_l0:
                self.l0_index_file = Path(env_l0)

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

        # v2.6.0: Access-log telemetry — record knowledge recall events to
        # answer "which knowledge is used / which is dead weight".
        # Enabled by default; the log is a best-effort side-channel that never
        # blocks retrieval.
        self.access_log_enabled: bool = _env_bool("LAYERED_MEMORY_ACCESS_LOG", True)
        # Whether to store the query text alongside events (privacy toggle).
        self.access_log_query: bool = _env_bool("LAYERED_MEMORY_LOG_QUERY", True)
        # Days of raw events to keep before auto-prune.
        _ret = os.environ.get("LAYERED_MEMORY_ACCESS_RETENTION_DAYS")
        try:
            self.access_log_retention_days: int = int(_ret) if _ret else 90
        except ValueError:
            self.access_log_retention_days = 90


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

        # v2.3.0: Auto-maintain — framework self-manages the L1↔agent-memory
        # dual-write and triggers compaction after writes, so the agent never
        # has to manually sync L0 pointers or remember to compact.
        # Default: enabled (the framework owns the complexity it introduced).
        self.auto_maintain: bool = (
            auto_maintain if auto_maintain is not None
            else _env_bool("LAYERED_MEMORY_AUTO_MAINTAIN", True)
        )
        # v2.3.0: Minimum days between automatic compaction passes (default 7).
        # Compaction also fires immediately when memory exceeds the bloat
        # threshold, regardless of this interval.
        self.auto_maintain_interval_days: float = (
            auto_maintain_interval_days if auto_maintain_interval_days is not None
            else _env_float("LAYERED_MEMORY_AUTO_MAINTAIN_INTERVAL_DAYS", 7.0)
        )

        # v2.7.0: Critical safety-net threshold (0–1, default 0.95). When agent
        # memory usage reaches this fraction of the real limit AND there is
        # bloat to migrate, compaction fires immediately, ignoring the
        # auto_maintain interval. Guards against the "silently over the real
        # limit" failure when bloat was written directly to native memory.
        self.compact_critical_threshold: float = (
            compact_critical_threshold if compact_critical_threshold is not None
            else _env_float("LAYERED_MEMORY_COMPACT_CRITICAL_THRESHOLD", 0.95)
        )

        # v2.7.0: Explicit agent-memory char limit. Highest-priority override
        # in _get_memory_max_chars. Normally left None — the framework reads
        # the real limit dynamically from Hermes config.yaml instead of
        # hard-coding a value that would drift from the user's actual setting.
        _mcl = memory_char_limit
        if _mcl is None:
            _env_mcl = os.environ.get("MEMORY_MAX_CHARS")
            if _env_mcl:
                try:
                    _mcl = int(_env_mcl)
                except ValueError:
                    _mcl = None
        self.memory_char_limit: int | None = _mcl if (_mcl and _mcl > 0) else None

        # v2.10.0: Promotion detector — when an agent keeps appending
        # same-topic sections into a catch-all domain (default "misc"), the
        # framework detects the semantic cluster after a write and *suggests*
        # extracting it into its own L1 file. It never moves content itself —
        # like dedup's suggestion, it's a soft advisory signal the agent acts on.
        self.promotion_enabled: bool = (
            promotion_enabled if promotion_enabled is not None
            else _env_bool("LAYERED_MEMORY_PROMOTION_ENABLED", True)
        )
        # Only these catch-all domains are scanned (avoids false positives on
        # legitimately-dense specialised files). Default: only "misc".
        self.promotion_watch_domains: list[str] = (
            promotion_watch_domains if promotion_watch_domains is not None
            else _env_list("LAYERED_MEMORY_PROMOTION_WATCH_DOMAINS", ["misc"])
        )
        # Files with fewer sections than this are never scanned.
        self.promotion_min_sections: int = (
            promotion_min_sections if promotion_min_sections is not None
            else _env_int("LAYERED_MEMORY_PROMOTION_MIN_SECTIONS", 4)
        )
        # Semantic similarity at/above which two sections join the same cluster.
        self.promotion_cluster_threshold: float = (
            promotion_cluster_threshold if promotion_cluster_threshold is not None
            else _env_float("LAYERED_MEMORY_PROMOTION_CLUSTER_THRESHOLD", 0.60)
        )
        # A cluster must reach this size before a promotion is suggested.
        self.promotion_min_cluster_size: int = (
            promotion_min_cluster_size if promotion_min_cluster_size is not None
            else _env_int("LAYERED_MEMORY_PROMOTION_MIN_CLUSTER_SIZE", 3)
        )

        # v2.10.1: Domain classification table for the auto-extractor. The
        # framework ships **no** business presets — domain inference is opt-in.
        # Maps ``domain_name -> [keyword, ...]``; when empty (the default), the
        # extractor makes no assumption and tags everything as its fallback
        # ("general"). Configured via config.yaml, constructor arg, or the
        # LAYERED_MEMORY_DOMAIN_KEYWORDS env var (JSON object).
        self.domain_keywords: dict[str, list[str]] = (
            domain_keywords if domain_keywords is not None
            else _env_json_dict("LAYERED_MEMORY_DOMAIN_KEYWORDS", {})
        )

        # v1.2: L0 index entry tag — configurable for i18n (default: "[L0]")
        _tag = os.environ.get("LAYERED_MEMORY_L0_TAG", "[L0]")
        if not (_tag.startswith("[") and _tag.endswith("]")):
            raise ValueError(
                f"Invalid l0_tag: {_tag!r}. Must start with '[' and end with ']'."
            )
        self.l0_tag: str = _tag

        # v1.2: Range validation for threshold parameters (must be 0.0–1.0)
        for _name, _val in [
            ("compact_bloat_threshold", self.compact_bloat_threshold),
            ("compact_capacity_warning_threshold", self.compact_capacity_warning_threshold),
            ("compact_critical_threshold", self.compact_critical_threshold),
            ("dedup_threshold", self.dedup_threshold),
            ("promotion_cluster_threshold", self.promotion_cluster_threshold),
        ]:
            if not (0.0 <= _val <= 1.0):
                raise ValueError(
                    f"{_name} must be between 0.0 and 1.0, got {_val}"
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
            if "memory" in name:
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

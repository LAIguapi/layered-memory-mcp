"""
Configuration for Layered Memory MCP Server.

All paths are configurable via environment variables or constructor arguments.
No hardcoded personal paths.

AGENT-NATIVE PATHS COME FROM AN ADAPTER
---------------------------------------
This module resolves the *framework's* own layout (``~/.layered-memory`` and
everything under it). It deliberately knows nothing about where a particular
agent product keeps its memory file, its session database or its session dumps:
those used to be literals here (``~/.hermes/memories/MEMORY.md``,
``~/.hermes/state.db``, ``~/.hermes/sessions``, the ``'§'`` separator) and now
come from :mod:`.adapters.environment`. ``agent_memory.adapter`` selects which
adapter answers; the default (``auto``) probes the registry Hermes-first, so an
existing install resolves exactly the paths it always did.
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


def default_state_db_path(adapter: object | None = None) -> Path | None:
    """Path of the agent's live session database, per the environment adapter.

    An agent that keeps sessions in a SQLite DB (Hermes: ``~/.hermes/state.db``)
    does *not* keep them in the JSON directory :func:`default_sessions_dir`
    points at — that directory only holds stale request dumps these days, which
    is why a directory scan can come back with zero sessions while the agent has
    been busy for weeks.

    ``None`` means the resolved adapter has no such database; callers treat that
    as "no live-session source" rather than as an error.
    """
    env = os.environ.get("LAYERED_MEMORY_HERMES_DB")
    if env:
        return Path(env).expanduser()
    from .adapters.environment import resolve_environment

    return (adapter or resolve_environment()).detect_state_db()


#: Legacy alias — the Hermes-named entry point kept for external callers.
default_hermes_db_path = default_state_db_path


def default_sessions_dir(adapter: object | None = None) -> Path | None:
    """Auto-detect the agent's session-dump directory.

    Priority: ``LAYERED_MEMORY_SESSIONS_DIR`` → whatever the resolved
    environment adapter reports (Hermes: ``~/.hermes/sessions``) → ``None``.
    """
    env = os.environ.get("LAYERED_MEMORY_SESSIONS_DIR")
    if env:
        return Path(env)

    from .adapters.environment import resolve_environment

    return (adapter or resolve_environment()).detect_sessions_dir()


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from an environment variable."""
    val = os.environ.get(name, "").lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce a YAML scalar to bool.

    YAML 1.1 resolves bare ``on``/``off``/``yes``/``no`` to booleans before this
    code ever sees them, so strings and bools both have to be accepted.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in ("1", "true", "yes", "on", "enabled"):
        return True
    if token in ("0", "false", "no", "off", "disabled", "none"):
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


def _env_float_or_none(name: str) -> float | None:
    """Read an optional float from an env var; None when unset/invalid.

    Distinct from ``_env_float``: there is no default float. Used for the
    semantic-threshold overrides, where "unset" must stay None so the
    calibrated>config>default resolution chain in injector still applies.
    """
    val = os.environ.get(name)
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return None


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


def _coerce_domain_keywords(data: object) -> dict[str, list[str]]:
    """Normalise a raw ``{domain: [keyword, ...]}`` mapping.

    Accepts a dict whose values are lists (or a single string) and coerces
    everything to ``dict[str, list[str]]``. Anything else yields ``{}``.
    """
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[str]] = {}
    for domain, keywords in data.items():
        if isinstance(keywords, list):
            result[str(domain)] = [str(k) for k in keywords]
        elif isinstance(keywords, str):
            result[str(domain)] = [keywords]
    return result


def _load_domain_keywords_from_yaml(home: Path) -> dict[str, list[str]]:
    """Read the ``domain_keywords`` section from ``<home>/config.yaml``.

    This is the file written by the ``layered-memory-migrate`` CLI. Returns an
    empty dict when the file is missing, unreadable, not a mapping, or has no
    ``domain_keywords`` section. Never raises — a broken config.yaml must not
    crash server start-up; it simply means "no user table configured".
    """
    path = home / "config.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return _coerce_domain_keywords(data.get("domain_keywords"))


def _load_write_guard_from_yaml(home: Path) -> str | None:
    """Read the ``write_guard`` knob from ``<home>/config.yaml``.

    Returns the canonical policy string, or None when the file is missing,
    unreadable, or has no such key. Never raises — a broken config.yaml must
    not crash server start-up; it just means "use the default policy".

    YAML 1.1 resolves bare ``off`` / ``on`` / ``yes`` / ``no`` to booleans, so
    ``write_guard: off`` arrives here as ``False``. Without that coercion the
    most natural spelling of "turn it off" would raise at start-up.
    """
    path = home / "config.yaml"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("write_guard")
    if value is None:
        return None
    if isinstance(value, bool):  # YAML 1.1 on/off, true/false
        return "auto" if value else "off"
    token = str(value).strip().lower()
    if token in ("off", "false", "no", "0", "disabled", "none"):
        return "off"
    if token in ("on", "true", "yes", "1", "enabled"):
        return "auto"
    return token


def _load_session_scan_from_yaml(home: Path) -> dict:
    """Read the ``session_scan`` section from ``<home>/config.yaml``.

    Shape (all keys optional)::

        session_scan:
          hermes_db_path: ~/.hermes/state.db   # live session DB; defaults to
                                               # whatever the environment
                                               # adapter reports
          exclude_sources: [cron]              # session sources to skip
          exclude_markers: [...]               # substrings that drop a session

    ``exclude_markers`` exists for a privacy reason, not a tidy-up one: the scan
    output is handed to a model that may run outside this machine, so the
    operator must be able to keep selected work (e.g. a client's confidential
    material) out of the export. It is deliberately **empty by default** — this
    package ships zero assumptions about what any operator works on. Never
    raises: a broken config.yaml must not crash server start-up.
    """
    path = home / "config.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get("session_scan")
    return section if isinstance(section, dict) else {}


def _load_consolidate_from_yaml(home: Path) -> dict:
    """Read the ``consolidate`` section from ``<home>/config.yaml``.

    Shape (all keys optional)::

        consolidate:
          enabled: true        # kill switch for the same-skeleton family gate
          min_family: 2        # members at which a write must go through the gate
          size_ceiling: 0.7    # write-back must shrink the family below this

    These are the tuning knobs of the write-path gate. They belong next to the
    operator's other policy (``write_guard``, ``session_scan``) rather than in
    this package because "how much compaction is enough" is a judgement about
    *their* knowledge base, not a framework constant — and a knob that can only
    be moved by editing the library, or by adding an env var to a systemd unit,
    is the kind of policy that silently stays at its default forever (which is
    exactly what happened to the 0.9 ceiling). Never raises: a broken config.yaml
    must not crash server start-up, it just means "use the defaults".
    """
    path = home / "config.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get("consolidate")
    return section if isinstance(section, dict) else {}


def _load_yaml_section(home: Path, key: str) -> dict:
    """Read one top-level mapping from ``<home>/config.yaml`` ({} if absent).

    Never raises: a broken config.yaml must not crash server start-up.
    """
    path = home / "config.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get(key)
    return section if isinstance(section, dict) else {}


# Catch-all file names a new user is likely to pile unsorted notes into. Glob
# patterns, matched case-insensitively against the domain (filename minus .md).
# Used by the maintenance-time split scans; see ``promotion.is_watched_domain``.
DEFAULT_PROMOTION_WATCH_PATTERNS = [
    "misc", "misc-*", "misc_*", "general", "other", "others", "tmp", "temp",
    "notes", "inbox", "scratch", "unsorted", "uncategorized",
    "杂项", "杂记", "其他", "其它", "临时", "笔记", "收件箱", "未分类",
]


#: Agent-owned extension surfaces the optional integration layer can target.
#: Mirrors ``routing.EXTENSION_SURFACES``; kept as plain strings here so config
#: loading never imports the router (and never fails because of it).
AGENT_EXTENSION_SURFACES = ("skill", "user_profile", "agent_other")

AGENT_EXTENSION_MODES = ("suggest", "apply_with_confirmation", "apply")

ROUTING_MODES = ("advisory", "enforce")


def _normalize_agent_extensions(raw: object) -> dict:
    """Normalise the ``agent_extensions`` section to a complete, safe shape.

    Returns
    ``{enabled, mode, adapter, root, surfaces: {<surface>: {enabled, handler, path}}}``.

    Every key falls back independently, and every fallback is the conservative
    one (disabled / suggest-only / no handler / no path). A half-written or
    malformed section therefore degrades to "classify and suggest" instead of
    raising at start-up — and in particular can never *accidentally* grant the
    framework permission to write a store that belongs to the agent.

    ``root`` and the per-surface ``path`` are what an apply-capable handler
    writes to. They are deliberately *not* defaulted: the reference
    ``generic_file`` handler refuses when neither is set, because an implicit
    target is exactly the hazard the whole opt-in chain exists to prevent.
    """
    out: dict = {
        "enabled": False,
        "mode": "suggest",
        "adapter": None,
        "root": None,
        "surfaces": {
            s: {"enabled": False, "handler": None, "path": None}
            for s in AGENT_EXTENSION_SURFACES
        },
    }
    if not isinstance(raw, dict):
        return out

    out["enabled"] = _coerce_bool(raw.get("enabled"), False)
    mode = str(raw.get("mode") or "suggest").strip().lower()
    out["mode"] = mode if mode in AGENT_EXTENSION_MODES else "suggest"
    adapter = raw.get("adapter")
    out["adapter"] = str(adapter).strip() or None if adapter else None
    root = raw.get("root")
    out["root"] = (str(root).strip() or None) if root else None

    surfaces = raw.get("surfaces")
    if isinstance(surfaces, dict):
        for key, value in surfaces.items():
            name = str(key).strip().lower()
            if name not in out["surfaces"]:
                continue
            if isinstance(value, dict):
                handler = value.get("handler")
                path = value.get("path")
                out["surfaces"][name] = {
                    "enabled": _coerce_bool(value.get("enabled"), False),
                    "handler": (str(handler).strip() or None) if handler else None,
                    "path": (str(path).strip() or None) if path else None,
                }
            else:
                # ``skill: true`` shorthand — enabled, but still no handler and
                # no path, so the framework still cannot write it.
                out["surfaces"][name] = {
                    "enabled": _coerce_bool(value, False),
                    "handler": None,
                    "path": None,
                }
    return out


def _coerce_str_list(value: object) -> list[str]:
    """Coerce a YAML scalar or list into a clean list of strings."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    out = []
    for item in items:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


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
        # v3.1.2 new fields — semantic write-path thresholds (explicit overrides
        # of the shipped bge/Chinese defaults; None → use default/calibrated).
        sem_skip_threshold: float | None = None,
        sem_fuse_threshold: float | None = None,
        sem_merge_threshold: float | None = None,
        # v3.2.0 new field — agent-memory write mode
        memory_mode: str | None = None,
        # v3.3.0 new field — MEMORY.md write-guard install policy
        write_guard: str | None = None,
        consolidate_enabled: bool | None = None,
        consolidate_min_family: int | None = None,
        consolidate_size_ceiling: float | None = None,
        maintenance_enabled: bool | None = None,
        maintenance_tick_seconds: float | None = None,
        maintenance_initial_delay: float | None = None,
        # v3.4.2 new fields — session scan source + privacy filter
        hermes_db_path: str | None = None,
        session_exclude_markers: list[str] | None = None,
        session_exclude_sources: list[str] | None = None,
        # Unreleased — maintenance-time split-scan patterns + governance rules
        promotion_watch_patterns: list[str] | None = None,
        governance_open_markers: list[str] | None = None,
        governance_resolved_markers: list[str] | None = None,
        governance_keep_reason_labels: list[str] | None = None,
        governance_disabled_markers: list[str] | None = None,
        # Unreleased — persistence routing + opt-in agent extension execution.
        # Both take the raw YAML section shape so a caller/test can pass exactly
        # what config.yaml would hold (see _normalize_agent_extensions).
        routing: dict | None = None,
        agent_extensions: dict | None = None,
        # Unreleased — agent-memory write safety (see agent_memory_guard.py).
        # Raw YAML section shape: {path, allow_auto_detect_write}.
        agent_memory: dict | None = None,
        # Unreleased — MEMORY_CHANGELOG backend (see memory_changelog.py).
        # Raw YAML section shape: {enabled, path, retention_days}.
        memory_changelog: dict | None = None,
    ):
        self.home = Path(home) if home else default_home()

        # Unreleased: the agent-native environment adapter. Resolved *first*,
        # because everything product-shaped below (session DB, session dir,
        # memory path, entry separator) now asks it instead of hardcoding a
        # path. See adapters/environment.py for the selection rules.
        self._load_agent_memory_settings(agent_memory)
        _env_adapter = self._environment_adapter()

        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else default_knowledge_dir(self.home)
        self.sessions_dir = (
            Path(sessions_dir) if sessions_dir else default_sessions_dir(_env_adapter)
        )
        self.l0_index_file = Path(l0_index_file) if l0_index_file else None
        # Fallback to environment variable if not passed explicitly
        if self.l0_index_file is None:
            env_l0 = os.environ.get("LAYERED_MEMORY_L0_INDEX_FILE")
            if env_l0:
                self.l0_index_file = Path(env_l0)

        # v3.4.2: where session summaries come from, and what must never leave
        # this machine. Precedence: explicit arg → env var → config.yaml → default.
        _scan_cfg = _load_session_scan_from_yaml(self.home)
        _db_setting = (
            hermes_db_path
            or os.environ.get("LAYERED_MEMORY_HERMES_DB")
            or _scan_cfg.get("hermes_db_path")
        )
        self.hermes_db_path: Path | None = (
            Path(str(_db_setting)).expanduser()
            if _db_setting
            else default_state_db_path(_env_adapter)
        )
        self.session_exclude_markers: list[str] = _coerce_str_list(
            session_exclude_markers
            if session_exclude_markers is not None
            else os.environ.get("LAYERED_MEMORY_SESSION_EXCLUDE_MARKERS")
            or _scan_cfg.get("exclude_markers")
        )
        self.session_exclude_sources: list[str] = _coerce_str_list(
            session_exclude_sources
            if session_exclude_sources is not None
            else _scan_cfg.get("exclude_sources", ["cron"])
        )

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
        # legitimately-dense specialised files). Exact names; these are also
        # the only domains the write-path ride-along embeds. Default: "misc".
        # Precedence: constructor → env → config.yaml `promotion:` → default.
        _promo_yaml = _load_yaml_section(self.home, "promotion")
        self.promotion_watch_domains: list[str] = (
            promotion_watch_domains if promotion_watch_domains is not None
            else _env_list(
                "LAYERED_MEMORY_PROMOTION_WATCH_DOMAINS",
                _coerce_str_list(_promo_yaml.get("watch_domains")) or ["misc"],
            )
        )
        # Glob patterns for the maintenance-time scans (audit_rot, cleanup
        # planning). A new user rarely has a file literally named misc.md, so
        # an exact-name-only default left split detection dead; the default
        # covers common catch-all names in English and Chinese. Set to [] to
        # restrict the scans to promotion_watch_domains.
        if promotion_watch_patterns is not None:
            self.promotion_watch_patterns: list[str] = _coerce_str_list(promotion_watch_patterns)
        elif os.environ.get("LAYERED_MEMORY_PROMOTION_WATCH_PATTERNS") is not None:
            self.promotion_watch_patterns = _coerce_str_list(
                os.environ["LAYERED_MEMORY_PROMOTION_WATCH_PATTERNS"]
            )
        elif "watch_patterns" in _promo_yaml:
            self.promotion_watch_patterns = _coerce_str_list(_promo_yaml.get("watch_patterns"))
        else:
            self.promotion_watch_patterns = list(DEFAULT_PROMOTION_WATCH_PATTERNS)
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

        # v3.1.2: Semantic write-path thresholds. These are EXPLICIT overrides
        # of the shipped defaults (constants live in injector.py). Left as None
        # they fall through the "calibrated > config > default" chain in
        # injector._semantic_thresholds; set to a float to pin them. Kept on the
        # config (not just read via getattr) so `hermes config set` / env vars /
        # config.yaml can tune the write boundary without code edits.
        self.sem_skip_threshold: float | None = (
            sem_skip_threshold if sem_skip_threshold is not None
            else _env_float_or_none("LAYERED_MEMORY_SEM_SKIP_THRESHOLD")
        )
        self.sem_fuse_threshold: float | None = (
            sem_fuse_threshold if sem_fuse_threshold is not None
            else _env_float_or_none("LAYERED_MEMORY_SEM_FUSE_THRESHOLD")
        )
        self.sem_merge_threshold: float | None = (
            sem_merge_threshold if sem_merge_threshold is not None
            else _env_float_or_none("LAYERED_MEMORY_SEM_MERGE_THRESHOLD")
        )

        # v3.2.0: Agent-memory write mode — how dual-write treats MEMORY.md:
        #   "pointers"   — legacy: one [L0] pointer per domain per write
        #                  (accumulates; can overflow small memory budgets).
        #   "index_only" — keep exactly ONE knowledge-index entry; per-domain
        #                  pointers are reaped on every write, and any
        #                  non-index entry remains bloat for compaction.
        #   "off"        — no dual-write at all.
        self.memory_mode: str = (
            memory_mode if memory_mode is not None
            else os.environ.get("LAYERED_MEMORY_MEMORY_MODE", "pointers")
        ).strip().lower()
        if self.memory_mode not in ("pointers", "index_only", "off"):
            raise ValueError(
                f"Invalid memory_mode: {self.memory_mode!r}. "
                "Must be 'pointers', 'index_only', or 'off'."
            )

        # v3.3.0: MEMORY.md write guard — install policy for the Hermes
        # pre_tool_call plugin that keeps agent-side writes out of the
        # framework-owned MEMORY.md:
        #   "auto"   — init_framework installs/repairs it when missing
        #   "manual" — init_framework only reports that it is missing
        #              (default: the host agent's config is the user's, so a
        #              third-party install must not edit it unasked)
        #   "off"    — never mentioned
        # Resolution priority (highest first):
        #   1. constructor argument
        #   2. LAYERED_MEMORY_WRITE_GUARD env var
        #   3. `write_guard:` key of <home>/config.yaml
        #   4. "manual"
        # The deployed plugin reads the same variable as a runtime kill switch
        # ("off" disables blocking without uninstalling).
        _wg = (
            write_guard
            if write_guard is not None
            else os.environ.get("LAYERED_MEMORY_WRITE_GUARD")
            or _load_write_guard_from_yaml(self.home)
            or "manual"
        )
        self.write_guard: str = str(_wg).strip().lower()
        if self.write_guard not in ("auto", "manual", "off"):
            raise ValueError(
                f"Invalid write_guard: {self.write_guard!r}. "
                "Must be 'auto', 'manual', or 'off'."
            )

        # v3.3.7: same-skeleton family consolidation (the write side of the
        # v3.3.2 read detector). Precedence: constructor → env → config.yaml →
        # default; `consolidate:` in the operator's config.yaml is the intended
        # home for these three knobs (see _load_consolidate_from_yaml).
        # `consolidate_enabled` is the kill switch: with it off, an upsert that
        # would add another member to a same-topic family writes as it always
        # did (the auditor will still report the family afterwards).
        _consolidate_yaml = _load_consolidate_from_yaml(self.home)
        _env_consolidate_enabled = os.environ.get("LAYERED_MEMORY_CONSOLIDATE_ENABLED")
        self.consolidate_enabled: bool = (
            bool(consolidate_enabled)
            if consolidate_enabled is not None
            else (
                _env_bool("LAYERED_MEMORY_CONSOLIDATE_ENABLED", True)
                if _env_consolidate_enabled is not None
                else _coerce_bool(_consolidate_yaml.get("enabled"), True)
            )
        )
        _cmf = consolidate_min_family
        if _cmf is None:
            _cmf = os.environ.get("LAYERED_MEMORY_CONSOLIDATE_MIN_FAMILY")
        if _cmf is None:
            _cmf = _consolidate_yaml.get("min_family")
        try:
            self.consolidate_min_family: int = int(_cmf) if _cmf is not None else 2
        except (TypeError, ValueError):
            self.consolidate_min_family = 2
        _csc = consolidate_size_ceiling
        if _csc is None:
            _csc = os.environ.get("LAYERED_MEMORY_CONSOLIDATE_SIZE_CEILING")
        if _csc is None:
            _csc = _consolidate_yaml.get("size_ceiling")
        try:
            self.consolidate_size_ceiling: float = (
                float(_csc) if _csc is not None else 0.9
            )
        except (TypeError, ValueError):
            self.consolidate_size_ceiling = 0.9

        # v3.4.0: periodic self-maintenance (see maintenance.py). Enabled by
        # default — the framework's self-maintenance is the framework's job
        # (the ride-along only ever ran when somebody happened to call the
        # server). The loop is a daemon thread on long-lived servers only, and
        # the work it triggers is interval/threshold-gated internally.
        self.maintenance_enabled: bool = (
            bool(maintenance_enabled)
            if maintenance_enabled is not None
            else _env_bool("LAYERED_MEMORY_MAINTENANCE_ENABLED", True)
        )
        _mtick = (
            maintenance_tick_seconds
            if maintenance_tick_seconds is not None
            else os.environ.get("LAYERED_MEMORY_MAINTENANCE_TICK")
        )
        try:
            self.maintenance_tick_seconds: float = (
                float(_mtick) if _mtick is not None else 1800.0
            )
        except (TypeError, ValueError):
            self.maintenance_tick_seconds = 1800.0
        _mdelay = (
            maintenance_initial_delay
            if maintenance_initial_delay is not None
            else os.environ.get("LAYERED_MEMORY_MAINTENANCE_DELAY")
        )
        try:
            self.maintenance_initial_delay: float = (
                float(_mdelay) if _mdelay is not None else 60.0
            )
        except (TypeError, ValueError):
            self.maintenance_initial_delay = 60.0

        # Lifecycle governance marker rules (governance.py). Each list EXTENDS
        # the shipped defaults (Chinese + English); `disabled_markers` removes
        # defaults that are noise for this knowledge base. Precedence:
        # constructor → `governance:` section of config.yaml → none.
        _gov_yaml = _load_yaml_section(self.home, "governance")

        def _gov(arg: list[str] | None, key: str) -> list[str]:
            return _coerce_str_list(arg if arg is not None else _gov_yaml.get(key))

        self.governance_open_markers: list[str] = _gov(governance_open_markers, "open_markers")
        self.governance_resolved_markers: list[str] = _gov(
            governance_resolved_markers, "resolved_markers"
        )
        self.governance_keep_reason_labels: list[str] = _gov(
            governance_keep_reason_labels, "keep_reason_labels"
        )
        self.governance_disabled_markers: list[str] = _gov(
            governance_disabled_markers, "disabled_markers"
        )

        # Unreleased: Persistence routing (routing.py). The router classifies a
        # candidate memory item into a surface — framework-owned common core
        # (L0/L1/TODO/MEMORY_CHANGELOG), an agent-owned extension
        # recommendation (skill/user_profile/agent_other), or no durable write.
        #
        # `mode` defaults to "advisory": the decision is reported alongside
        # existing results and changes NOTHING about what a write does. That is
        # the only non-breaking default for an existing knowledge base — a
        # classifier that started rejecting writes on upgrade would be a data
        # loss event, not a feature. "enforce" is opt-in, and exactly one path
        # gates on it: compact_memory injects only L1_KNOWLEDGE into L1 and
        # returns the other surfaces as candidates. inject_knowledge still
        # writes L1 in either mode (and says so in its routing_warning).
        #
        # Precedence: constructor → env → `routing:` in config.yaml → default.
        # Malformed values fall back per key rather than raising: routing is an
        # advisory layer, and nothing about it justifies a failed start-up.
        _routing_yaml = routing if isinstance(routing, dict) else _load_yaml_section(
            self.home, "routing"
        )
        _env_routing_enabled = os.environ.get("LAYERED_MEMORY_ROUTING_ENABLED")
        self.routing_enabled: bool = (
            _env_bool("LAYERED_MEMORY_ROUTING_ENABLED", True)
            if _env_routing_enabled is not None
            else _coerce_bool(_routing_yaml.get("enabled"), True)
        )
        _routing_mode = (
            os.environ.get("LAYERED_MEMORY_ROUTING_MODE")
            or _routing_yaml.get("mode")
            or "advisory"
        )
        _routing_mode = str(_routing_mode).strip().lower()
        self.routing_mode: str = (
            _routing_mode if _routing_mode in ROUTING_MODES else "advisory"
        )
        # Marker extensions, in the same style as `governance:`: each list
        # EXTENDS the shipped vocabulary, `disabled_markers` removes defaults
        # that are noise for this knowledge base.
        self.routing_markers: dict[str, list[str]] = {
            key: _coerce_str_list(_routing_yaml.get(key))
            for key in (
                "extra_skill_markers",
                "extra_todo_markers",
                "extra_profile_markers",
                "extra_knowledge_markers",
                "disabled_markers",
            )
        }

        # Unreleased: Opt-in execution for agent-owned extension surfaces.
        # DEFAULT: disabled / suggest-only / no handler. Skill, user profile and
        # "agent other" belong to the agent, so the framework classifies them and
        # returns an AgentAction recommendation; it writes nothing. An operator
        # who wants full service enables this section and names a handler per
        # surface, which makes the framework an orchestrator for that surface —
        # ownership does not move (see routing.surface_capabilities).
        _ax_yaml = (
            agent_extensions
            if isinstance(agent_extensions, dict)
            else _load_yaml_section(self.home, "agent_extensions")
        )
        self.agent_extensions: dict = _normalize_agent_extensions(_ax_yaml)
        _env_ax_enabled = os.environ.get("LAYERED_MEMORY_AGENT_EXTENSIONS_ENABLED")
        if _env_ax_enabled is not None:
            self.agent_extensions["enabled"] = _env_bool(
                "LAYERED_MEMORY_AGENT_EXTENSIONS_ENABLED", False
            )
        _env_ax_mode = os.environ.get("LAYERED_MEMORY_AGENT_EXTENSIONS_MODE")
        if _env_ax_mode:
            _m = _env_ax_mode.strip().lower()
            if _m in AGENT_EXTENSION_MODES:
                self.agent_extensions["mode"] = _m
        _env_ax_adapter = os.environ.get("LAYERED_MEMORY_AGENT_EXTENSIONS_ADAPTER")
        if _env_ax_adapter:
            self.agent_extensions["adapter"] = _env_ax_adapter.strip() or None
        _env_ax_root = os.environ.get("LAYERED_MEMORY_AGENT_EXTENSIONS_ROOT")
        if _env_ax_root:
            self.agent_extensions["root"] = _env_ax_root.strip() or None

        # v2.10.1: Domain classification table for the auto-extractor. The
        # framework ships **no** business presets — domain inference is opt-in.
        # Maps ``domain_name -> [keyword, ...]``; when empty (the default), the
        # extractor makes no assumption and tags everything as its fallback
        # ("general"). Resolution priority (highest first):
        #   1. constructor argument
        #   2. LAYERED_MEMORY_DOMAIN_KEYWORDS env var (JSON object)
        #   3. `domain_keywords:` section of <home>/config.yaml
        #      (written by the `layered-memory-migrate` CLI)
        #   4. empty table (framework ships no presets)
        self.domain_keywords: dict[str, list[str]] = (
            domain_keywords if domain_keywords is not None
            else _env_json_dict(
                "LAYERED_MEMORY_DOMAIN_KEYWORDS",
                _load_domain_keywords_from_yaml(self.home),
            )
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

        # v3.1.2: validate the optional semantic overrides only when explicitly
        # set (None means "use the resolution chain", not a range violation).
        for _name, _val in [
            ("sem_skip_threshold", self.sem_skip_threshold),
            ("sem_fuse_threshold", self.sem_fuse_threshold),
            ("sem_merge_threshold", self.sem_merge_threshold),
        ]:
            if _val is not None and not (0.0 <= _val <= 1.0):
                raise ValueError(
                    f"{_name} must be between 0.0 and 1.0, got {_val}"
                )

        # v1.0 / Unreleased: the `agent_memory:` section is resolved at the top
        # of __init__ (see _load_agent_memory_settings) because the adapter it
        # selects decides every other product-shaped path.

        # Unreleased: MEMORY_CHANGELOG backend (see memory_changelog.py). The
        # append-only provenance record of semantic L0/L1 mutations:
        #
        #   memory_changelog:
        #     enabled: true          # kill switch; off = nothing is recorded
        #     path: null             # defaults to <home>/data/memory_changelog.jsonl
        #     retention_days: null   # read and reported, NOT enforced
        #
        # Enabled by default: the surface was already declared framework-owned,
        # and a provenance record that only exists when somebody remembered to
        # turn it on cannot answer "why does my knowledge base say this now?".
        # `retention_days` is deliberately inert — pruning provenance is
        # destructive, so this version never deletes a line and says so rather
        # than shipping a silent sweep.
        # Precedence: constructor → env → config.yaml → default.
        _cl_yaml = (
            memory_changelog
            if isinstance(memory_changelog, dict)
            else _load_yaml_section(self.home, "memory_changelog")
        )
        _env_cl = os.environ.get("LAYERED_MEMORY_CHANGELOG_ENABLED")
        self.memory_changelog_enabled: bool = (
            _env_bool("LAYERED_MEMORY_CHANGELOG_ENABLED", True)
            if _env_cl is not None
            else _coerce_bool(_cl_yaml.get("enabled"), True)
        )
        _clp = (
            os.environ.get("LAYERED_MEMORY_CHANGELOG_PATH")
            or _cl_yaml.get("path")
        )
        self.memory_changelog_path: Path = (
            Path(str(_clp)).expanduser()
            if _clp
            else self.home / "data" / "memory_changelog.jsonl"
        )
        _clr = os.environ.get("LAYERED_MEMORY_CHANGELOG_RETENTION_DAYS")
        if _clr is None:
            _clr = _cl_yaml.get("retention_days")
        try:
            self.memory_changelog_retention_days: int | None = (
                int(_clr) if _clr is not None else None
            )
        except (TypeError, ValueError):
            self.memory_changelog_retention_days = None

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

    # ------------------------------------------------------------------
    # Agent-native environment (adapters/environment.py)
    # ------------------------------------------------------------------

    def _load_agent_memory_settings(self, agent_memory: dict | None) -> None:
        """Resolve the ``agent_memory:`` section.

        Unreleased: the section of config.yaml joined the env vars as a
        resolution source, because these knobs decide whether the framework may
        project L0 pointers into a file it does not own:

            agent_memory:
              adapter: auto                   # environment adapter id
              path: null                      # explicit target (recommended)
              allow_auto_detect_write: false  # opt back into auto-detect writes
              separator: null                 # override the adapter's separator

        With neither ``path`` nor ``allow_auto_detect_write`` set, an
        auto-detected production memory file is refused as a *write* target
        unless the home is clearly this install's own — see
        ``agent_memory_guard.resolve_write_target`` for the exact rule. That is
        what stops a bare temp config (a test, a sanity script) from silently
        appending a pointer to a live agent's memory.

        Precedence throughout: constructor → env → config.yaml → default.
        """
        _am_yaml = (
            agent_memory
            if isinstance(agent_memory, dict)
            else _load_yaml_section(self.home, "agent_memory")
        )
        _amp = (
            os.environ.get("LAYERED_MEMORY_AGENT_MEMORY_PATH")
            or _am_yaml.get("path")
        )
        self.agent_memory_path: Path | None = (
            Path(str(_amp)).expanduser() if _amp else None
        )
        _env_aadw = os.environ.get("LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE")
        self.agent_memory_allow_auto_detect_write: bool = (
            _env_bool("LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE", False)
            if _env_aadw is not None
            else _coerce_bool(_am_yaml.get("allow_auto_detect_write"), False)
        )
        # Which agent product's file layout to assume. "auto" (the default)
        # probes the adapter registry Hermes-first, which is exactly the
        # candidate chain this class used to hardcode.
        _ada = (
            os.environ.get("LAYERED_MEMORY_AGENT_ADAPTER")
            or _am_yaml.get("adapter")
        )
        self.agent_adapter: str = str(_ada).strip() if _ada else "auto"
        # An explicit separator override. None means "ask the adapter" — which
        # is why this is stored separately from the resolved property below.
        _ams = (
            os.environ.get("LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR")
            or _am_yaml.get("separator")
        )
        self._agent_memory_separator: str | None = str(_ams) if _ams else None

    def _environment_adapter(self, memory_path: Path | None = None):
        """The resolved environment adapter for this config.

        ``memory_path`` lets a caller already working on a specific file have
        the adapter chosen by *that* file's layout rather than by a probe.

        Resolved on every call rather than cached: adapter selection probes the
        filesystem, and a long-lived server whose agent appears (or whose
        operator fixes ``agent_memory.adapter``) mid-run should pick it up the
        way the pre-adapter probe chain did.
        """
        from .adapters.environment import resolve_environment

        return resolve_environment(self, memory_path=memory_path)

    def describe_agent_environment(self) -> dict:
        """Read-only reflection of the environment adapter. Never raises."""
        from .adapters.environment import describe_environment

        return describe_environment(self)

    @property
    def agent_state_db_path(self) -> Path | None:
        """Product-neutral name for :attr:`hermes_db_path`.

        The attribute, its ``session_scan.hermes_db_path`` config key and the
        ``LAYERED_MEMORY_HERMES_DB`` env var keep their Hermes-era names so no
        existing install breaks; the value itself comes from the environment
        adapter. New code should read this.
        """
        return self.hermes_db_path

    @property
    def agent_memory_separator(self) -> str:
        """Entry separator for the agent's memory file.

        The configured override wins; otherwise the resolved adapter's default
        (Hermes: ``§``; generic: a blank line).
        """
        if self._agent_memory_separator:
            return self._agent_memory_separator
        try:
            return self._environment_adapter().default_separator()
        except Exception:  # noqa: BLE001 — a separator must always resolve
            from .adapters.environment import DEFAULT_SEPARATOR

            return DEFAULT_SEPARATOR

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
          1. Explicitly configured ``agent_memory.path``
          2. whatever the resolved environment adapter reports

        Under the default ``adapter: auto`` the registry is probed in its
        registered order — Hermes, Claude Code, Cursor, Cline, then the
        framework-owned ``<home>/agent-memory.md`` fallback — which is the same
        candidate chain this method used to spell out inline. Under an explicit
        adapter id only that adapter is asked, so ``adapter: generic`` resolves
        nothing outside the framework's own home.

        Returns a ``Path`` if found or configured, ``None`` if nothing exists.
        **Detection is not permission:** an auto-detected path is still refused
        as a write target by ``agent_memory_guard.resolve_write_target`` unless
        it was named explicitly or opted into.
        """
        # 1. Explicit config
        if self.agent_memory_path:
            return self.agent_memory_path

        # 2. Ask the adapter layer.
        return self._environment_adapter().detect_memory_path(framework_home=self.home)

    def detect_agent_memory_separator(self, memory_path: Path | None = None) -> str:
        """Auto-detect the entry separator used in the agent's memory file.

        The adapter decides per file (Hermes answers ``§`` for a ``MEMORY.md``
        and declines to answer for anything else); an adapter with no opinion
        falls through to :attr:`agent_memory_separator`.
        """
        path = memory_path or self.detect_agent_memory_path()
        if path and path.exists():
            resolved = self._environment_adapter(path).memory_separator(path)
            if resolved:
                return resolved
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

"""Persistence router / classifier for the layered-memory common core.

WHAT THIS MODULE IS
-------------------
A **deterministic persistence classifier**. Given one candidate memory item
(a chunk of text the agent is about to persist), it decides which *memory
surface* that item belongs to, using marker and structure rules only — no model
call, no embedding, no probabilistic inference. ``score`` below is a rule-weight
sum, deliberately not called "confidence": it is not a probability and must not
be read as semantic certainty.

THE SURFACE MODEL
-----------------
The framework owns a **common core** and merely *recognises* everything else::

    Raw session / user input / tool result
            │
            ▼
    persistence classifier  (this module)
            │
            ├─ framework-owned common core
            │    ├─ L0_INDEX          knowledge map / entry index
            │    ├─ L1_KNOWLEDGE      durable domain cognition
            │    ├─ TODO              unfinished state
            │    └─ MEMORY_CHANGELOG  provenance of L0/L1 changes
            │
            ├─ agent-owned extension recommendation  (suggest-only by default)
            │    ├─ SKILL             action protocols / repeatable procedures
            │    ├─ USER_PROFILE      small global user preferences
            │    └─ AGENT_OTHER       agent-owned memory-like content
            │
            └─ no durable write
                 └─ UNDECIDED / EPHEMERAL

Raw session data is **source-owned, read-only evidence**: the framework may scan
it, but it is not a writable surface and therefore not a member of
:class:`MemorySurface`.

OWNERSHIP IS NOT APPLY-CAPABILITY
---------------------------------
For an extension surface, ownership stays ``agent`` forever. Whether the
framework may *apply* an :class:`AgentAction` on the user's behalf is a separate,
opt-in question answered by the ``agent_extensions`` config section: default off,
so the framework classifies and suggests but writes nothing outside the core.
With ``agent_extensions.enabled`` plus a configured per-surface handler, the
framework becomes an orchestrator for that surface — the surface is still the
agent's.

``can_apply`` / ``framework_can_apply`` report *real* readiness, not just the
config switches: :func:`extension_readiness` also asks whether the named handler
is registered and whether it can resolve a safe target. A capability output that
promised a write ``apply_agent_action`` then refuses would be worse than no
capability output at all.

This module never touches a filesystem path belonging to a specific agent
product. Any such mapping belongs in an adapter (see ``adapters/base.py``).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, Field

from .governance import DEFAULT_OPEN_MARKERS, compile_marker

# ---------------------------------------------------------------------------
# Surfaces and ownership
# ---------------------------------------------------------------------------


class MemorySurface(str, Enum):
    """Every surface the classifier can name.

    Deliberately absent: a writable ``raw_session`` (source-owned evidence) and
    a ``reject`` pseudo-surface (rejection is "no durable write", expressed by
    ``UNDECIDED`` / ``EPHEMERAL``).
    """

    # framework-owned common core
    L0_INDEX = "l0_index"
    L1_KNOWLEDGE = "l1_knowledge"
    TODO = "todo"
    MEMORY_CHANGELOG = "memory_changelog"

    # agent-owned extension recommendations
    SKILL = "skill"
    USER_PROFILE = "user_profile"
    AGENT_OTHER = "agent_other"

    # no durable write
    UNDECIDED = "undecided"
    EPHEMERAL = "ephemeral"


class SurfaceOwnership(str, Enum):
    """Who owns a surface — i.e. whose data model the write belongs to."""

    FRAMEWORK = "framework"
    AGENT = "agent"
    SOURCE = "source"   # read-only evidence (raw session); no writable surface
    NONE = "none"       # nothing is persisted


CORE_SURFACES: tuple[MemorySurface, ...] = (
    MemorySurface.L0_INDEX,
    MemorySurface.L1_KNOWLEDGE,
    MemorySurface.TODO,
    MemorySurface.MEMORY_CHANGELOG,
)

EXTENSION_SURFACES: tuple[MemorySurface, ...] = (
    MemorySurface.SKILL,
    MemorySurface.USER_PROFILE,
    MemorySurface.AGENT_OTHER,
)

NO_WRITE_SURFACES: tuple[MemorySurface, ...] = (
    MemorySurface.UNDECIDED,
    MemorySurface.EPHEMERAL,
)

_OWNERSHIP: dict[MemorySurface, SurfaceOwnership] = {
    **{s: SurfaceOwnership.FRAMEWORK for s in CORE_SURFACES},
    **{s: SurfaceOwnership.AGENT for s in EXTENSION_SURFACES},
    **{s: SurfaceOwnership.NONE for s in NO_WRITE_SURFACES},
}


def ownership_of(surface: MemorySurface) -> SurfaceOwnership:
    """Ownership of ``surface``. Single source of truth; never hand-set."""
    return _OWNERSHIP[MemorySurface(surface)]


ApplicationMode = Literal["suggest", "apply_with_confirmation", "apply"]
RoutingMode = Literal["advisory", "enforce"]
ConfidenceBasis = Literal["markers", "structure", "mixed", "insufficient"]

# --- apply-blocker vocabulary ------------------------------------------------
# Stable reason codes for "why can this extension surface not be applied".
# Defined here, the lower layer, so the capability reflection and
# ``adapters.base.apply_agent_action`` cannot drift apart; ``adapters.base``
# re-exports them.
BLOCK_EXTENSIONS_DISABLED = "extensions_disabled"
BLOCK_SURFACE_DISABLED = "surface_disabled"
BLOCK_MODE_SUGGEST = "mode_suggest"
BLOCK_NO_HANDLER = "no_handler"
BLOCK_HANDLER_NOT_REGISTERED = "handler_not_registered"
BLOCK_HANDLER_CANNOT_WRITE_SURFACE = "handler_cannot_write_surface"
BLOCK_HANDLER_READINESS_ERROR = "handler_readiness_error"
BLOCK_HANDLER_NOT_READY = "handler_not_ready"
BLOCK_ADAPTER_LAYER_UNAVAILABLE = "adapter_layer_unavailable"
BLOCK_NOT_AN_EXTENSION_SURFACE = "not_an_extension_surface"

#: Order used to break score ties. Earlier wins: an item that is both open work
#: and a procedure leaves durable L1 first, and prose only falls through to L1
#: when nothing more specific fits.
_TIE_BREAK: tuple[MemorySurface, ...] = (
    MemorySurface.TODO,
    MemorySurface.SKILL,
    MemorySurface.USER_PROFILE,
    MemorySurface.AGENT_OTHER,
    MemorySurface.EPHEMERAL,
    MemorySurface.L1_KNOWLEDGE,
)


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """One piece of rule evidence behind a decision.

    ``kind`` drives ``RoutingDecision.confidence_basis``: a surface supported by
    markers alone is a weaker call than one that also matched a structure rule.
    ``negative`` signals suppress a surface instead of supporting one.
    """

    kind: Literal["marker", "structure", "negative"]
    name: str
    surface: MemorySurface | None = None
    evidence: str = ""
    weight: float = 0.0


class AgentAction(BaseModel):
    """A recommendation addressed to the agent that owns an extension surface.

    The framework never writes prose for an agent-owned surface: it hands back
    the caller's own ``source_content`` plus a structural ``shape_hint``, so the
    agent (which has a model) does the authoring. Intentionally generic — no
    field here names a product, a file or a path.
    """

    kind: Literal["agent_native_write"] = "agent_native_write"
    surface: MemorySurface
    operation: Literal["create", "update", "append"]
    title: str | None = None
    source_content: str
    shape_hint: list[str] = Field(default_factory=list)
    rationale: str
    requires_user_confirmation: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoutingAlternative(BaseModel):
    """A runner-up surface, so a caller can see what the decision was close to."""

    surface: MemorySurface
    ownership: SurfaceOwnership
    score: float
    reason: str = ""


class RoutingDecision(BaseModel):
    """The classifier's full answer for one candidate item.

    ``ownership`` and the three application fields are **derived** (see
    :func:`_finalize`), never supplied by a caller: that is what keeps
    "agent-owned but framework-appliable" from drifting into a contradictory
    hand-assembled combination.
    """

    schema_version: int = 1
    surface: MemorySurface
    ownership: SurfaceOwnership
    score: float = 0.0
    confidence_basis: ConfidenceBasis = "insufficient"
    reason: str = ""
    signals: list[Signal] = Field(default_factory=list)
    alternatives: list[RoutingAlternative] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    mode: RoutingMode = "advisory"
    suggested_action: AgentAction | None = None

    # capability / application — computed from surface + config
    configured_handler: str | None = None
    application_mode: ApplicationMode = "suggest"
    framework_can_apply: bool = False
    agent_should_apply: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (enums as their string values)."""
        return self.model_dump(mode="json")

    def advisory_summary(self) -> dict[str, Any]:
        """Compact form for attaching to a write result (drops bulky fields)."""
        return {
            "schema_version": self.schema_version,
            "surface": self.surface.value,
            "ownership": self.ownership.value,
            "score": self.score,
            "confidence_basis": self.confidence_basis,
            "mode": self.mode,
            "reason": self.reason,
            "signals": [
                {"kind": s.kind, "name": s.name, "evidence": s.evidence}
                for s in self.signals
            ],
            "alternatives": [
                {"surface": a.surface.value, "score": a.score} for a in self.alternatives
            ],
            "warnings": list(self.warnings),
            "framework_can_apply": self.framework_can_apply,
            "agent_should_apply": self.agent_should_apply,
            "application_mode": self.application_mode,
            "configured_handler": self.configured_handler,
        }


class SurfaceCapability(BaseModel):
    """What a caller may do with one surface under the *current* config."""

    surface: MemorySurface
    owned_by: SurfaceOwnership
    default_behavior: Literal["apply", "suggest", "none"]
    configured_handler: str | None = None
    #: Whether ``configured_handler`` actually resolves to a registered adapter.
    #: False with a non-null handler means the config names something unknown —
    #: visible here rather than only in a refusal at write time.
    handler_registered: bool = False
    can_apply: bool = False
    application_mode: ApplicationMode = "suggest"
    #: Stable reason codes for why ``can_apply`` is false. Empty when it is true.
    apply_blockers: list[str] = Field(default_factory=list)
    #: Resolved location of the surface's own durable store, when it has one
    #: the caller can read directly: the memory changelog's JSONL file for the
    #: core, or the configured handler's resolved target for an extension.
    store_path: str | None = None
    notes: str = ""


class MemorySurfaceCapabilities(BaseModel):
    """Read-only reflection of the whole surface model under this config."""

    schema_version: int = 1
    routing_enabled: bool = True
    routing_mode: RoutingMode = "advisory"
    agent_extensions_enabled: bool = False
    agent_extensions_mode: ApplicationMode = "suggest"
    adapter: str | None = None
    #: Optional base directory configured for extension handlers that write
    #: files (``agent_extensions.root``). ``None`` means "no default location".
    agent_extensions_root: str | None = None
    #: Handler names an operator may put in ``surfaces.*.handler`` — i.e. the
    #: adapters actually registered in this process. A configured handler that
    #: is not in this list is the reason a surface reports
    #: ``handler_registered: false``.
    registered_handlers: list[str] = Field(default_factory=list)
    #: Which *environment* adapter is answering the framework's questions about
    #: the host agent's own files — ``{configured, resolved, known, ...}`` from
    #: ``adapters.environment.describe_environment``. Distinct from ``adapter``
    #: above, which names the optional extension-*action* handler bundle.
    agent_environment: dict[str, Any] = Field(default_factory=dict)
    core_surfaces: dict[str, SurfaceCapability] = Field(default_factory=dict)
    extension_surfaces: dict[str, SurfaceCapability] = Field(default_factory=dict)
    no_write_surfaces: dict[str, SurfaceCapability] = Field(default_factory=dict)
    source_owned_evidence: list[str] = Field(default_factory=list)
    derived_stores: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Marker vocabulary (data — extend through config, not by editing code)
# ---------------------------------------------------------------------------

#: Action / procedure markers. Necessary but **not sufficient** for SKILL: an
#: architecture note that merely says "验证" or "流程" is still L1 knowledge, so a
#: structure rule (numbered steps, checklist, commands, code) must also match.
DEFAULT_SKILL_ACTION_MARKERS: tuple[str, ...] = (
    "步骤", "流程", "操作方法", "操作步骤", "执行", "运行", "命令", "部署",
    "安装", "配置方法", "做法", "怎么做", "如何操作", "检查清单", "清单",
    "排查", "排错", "重跑", "重启", "回滚", "手册", "脚本", "跑一遍",
    "step", "steps", "run", "execute", "command", "install", "deploy",
    "build", "restart", "rollback", "checklist", "runbook", "playbook",
    "procedure", "workflow", "how to", "troubleshoot", "re-run", "script",
)

#: Unfinished-state markers. Shares the governance "open marker" vocabulary so
#: that "what must leave durable L1" has exactly one definition, plus the
#: blocked/waiting phrasings that are specific to routing.
DEFAULT_TODO_EXTRA_MARKERS: tuple[str, ...] = (
    "阻塞", "被阻塞", "卡住", "等待用户", "等待确认", "等待决策", "待排期",
    "blocked", "blocked by", "waiting on", "waiting for", "awaiting",
    "needs user decision", "unresolved", "open question",
)

#: Preference / persona markers.
DEFAULT_PROFILE_MARKERS: tuple[str, ...] = (
    "用户偏好", "我偏好", "我喜欢", "我习惯", "偏好", "称呼我", "叫我",
    "我的风格", "我的习惯", "语言偏好",
    "user prefers", "user preference", "i prefer", "i like", "my preference",
    "call me", "preferred language", "preferred name", "persona", "pronouns",
)

#: Global-scope markers. A preference only reaches USER_PROFILE when it is
#: stated as a standing, cross-project rule rather than a one-project choice.
DEFAULT_PROFILE_SCOPE_MARKERS: tuple[str, ...] = (
    "以后", "每次", "始终", "一律", "全局", "所有项目", "任何时候", "默认都",
    "always", "every time", "from now on", "globally", "in all projects",
    "by default", "never",
)

#: Durable-cognition markers: facts, decisions, boundaries, current state.
DEFAULT_KNOWLEDGE_MARKERS: tuple[str, ...] = (
    "架构", "决策", "决定", "事实", "边界", "现状", "当前", "约定", "规范",
    "规则", "原则", "根因", "结论", "理解", "职责", "负责", "采用", "权衡",
    "设计", "依赖", "版本", "端口", "路径", "口径", "语义", "不变量",
    "architecture", "architectural", "decision", "decided", "fact",
    "boundary", "ownership", "owns", "responsible for", "convention",
    "invariant", "current state", "rationale", "root cause", "conclusion",
    "design", "trade-off", "constraint", "semantics",
)

#: One-off / non-durable markers.
DEFAULT_EPHEMERAL_MARKERS: tuple[str, ...] = (
    "一次性", "临时输出", "本次运行", "调试输出", "日志片段", "控制台输出",
    "原始日志", "刚才的输出",
    "stdout", "stderr", "console output", "raw log", "log excerpt",
    "exit code", "one-off", "debug output", "traceback",
)

#: Agent-owned-but-unclassifiable markers. Product-neutral on purpose: no file
#: name of any agent product appears here.
DEFAULT_AGENT_OTHER_MARKERS: tuple[str, ...] = (
    "代理记忆", "智能体记忆", "原生记忆", "自有记忆", "代理自有",
    "agent memory", "native memory", "agent-owned", "agent native",
)


# ---------------------------------------------------------------------------
# Structure rules
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•])\s+\S")
_NUMBERED_RE = re.compile(
    r"^\s*(?:\d+\s*[.)、]|第\s*\d+\s*步|step\s*\d+\b)\s*\S", re.IGNORECASE
)
_CHECKBOX_RE = re.compile(r"^\s*[-*+]\s*\[[ xX]\]")
_SHELL_RE = re.compile(
    r"^\s*(?:[$>#]\s*)?(?:sudo\s+)?"
    r"(?:git|pip|pip3|python|python3|npm|pnpm|yarn|docker|docker-compose|"
    r"kubectl|systemctl|journalctl|apt|apt-get|brew|make|cargo|go|curl|wget|"
    r"ssh|scp|rsync|psql|mysql|redis-cli|bash|sh|pytest|uv|poetry)\b"
)
_LOG_LINE_RE = re.compile(
    r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"          # 2026-10-12 08:00
    r"|^\s*\d{2}:\d{2}:\d{2}\b"                           # 08:00:01
    r"|\b(?:DEBUG|INFO|WARN|WARNING|ERROR|TRACE|CRITICAL)\b\s*[:\]|-]"
    r"|^\s*Traceback \(most recent call last\)"
    r"|^\s*File \"[^\"]+\", line \d+"
)

#: Below this many characters, an item with no marker at all is UNDECIDED
#: rather than assumed to be durable knowledge.
MIN_CHARS_FOR_FALLBACK = 80

#: A surface must reach this rule-weight to win outright.
SCORE_FLOOR = 0.40

#: Share of non-empty lines that must look like log output before an item is
#: treated as raw trace (which also suppresses the SKILL candidate).
LOG_SHAPE_RATIO = 0.4


class _Structure:
    """Counted structure facts about one item of content."""

    __slots__ = (
        "lines", "nonempty", "fences", "bullets", "numbered",
        "checkboxes", "shell", "log_lines", "chars",
    )

    def __init__(self, content: str) -> None:
        self.lines = content.splitlines()
        self.chars = len(content.strip())
        self.nonempty = 0
        self.fences = 0
        self.bullets = 0
        self.numbered = 0
        self.checkboxes = 0
        self.shell = 0
        self.log_lines = 0
        in_fence = False
        for raw in self.lines:
            line = raw.rstrip()
            if not line.strip():
                continue
            self.nonempty += 1
            if _FENCE_RE.match(line):
                self.fences += 1
                in_fence = not in_fence
                continue
            if _LOG_LINE_RE.search(line):
                self.log_lines += 1
            if _SHELL_RE.match(line):
                self.shell += 1
            if in_fence:
                continue
            if _CHECKBOX_RE.match(line):
                self.checkboxes += 1
            elif _NUMBERED_RE.match(line):
                self.numbered += 1
            elif _BULLET_RE.match(line):
                self.bullets += 1

    @property
    def code_blocks(self) -> int:
        return self.fences // 2 + (self.fences % 2)  # unterminated fence counts

    @property
    def log_shaped(self) -> bool:
        """Raw trace: several log lines, and they dominate the item."""
        return (
            self.log_lines >= 2
            and self.nonempty > 0
            and (self.log_lines / self.nonempty) >= LOG_SHAPE_RATIO
        )

    def skill_signals(self) -> list[Signal]:
        """Structure rules that make an item an executable protocol.

        Bullets alone never qualify — prose knowledge is routinely bulleted, and
        accepting it here is exactly how architectural notes get misread as
        procedures.
        """
        out: list[Signal] = []
        if self.numbered >= 2:
            out.append(Signal(
                kind="structure", name="numbered_steps",
                surface=MemorySurface.SKILL,
                evidence=f"{self.numbered} numbered step line(s)", weight=0.12,
            ))
        if self.checkboxes >= 2:
            out.append(Signal(
                kind="structure", name="checklist",
                surface=MemorySurface.SKILL,
                evidence=f"{self.checkboxes} checkbox item(s)", weight=0.12,
            ))
        if self.shell >= 2:
            out.append(Signal(
                kind="structure", name="command_sequence",
                surface=MemorySurface.SKILL,
                evidence=f"{self.shell} command line(s)", weight=0.12,
            ))
        elif self.shell == 1 and self.code_blocks >= 1:
            out.append(Signal(
                kind="structure", name="command_in_code_block",
                surface=MemorySurface.SKILL,
                evidence="1 command line inside a code block", weight=0.10,
            ))
        elif self.shell == 1 and self.bullets >= 3:
            out.append(Signal(
                kind="structure", name="command_in_bullet_list",
                surface=MemorySurface.SKILL,
                evidence="1 command line in a bulleted list", weight=0.08,
            ))
        return out


# ---------------------------------------------------------------------------
# Compiled rule sets
# ---------------------------------------------------------------------------


def _merge_markers(
    defaults: Iterable[str], extra: Iterable[str], disabled: set[str]
) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for m in (*defaults, *extra):
        token = str(m).strip()
        if token and token not in seen and token not in disabled:
            seen.add(token)
            out.append(token)
    # Longest first, so a line is attributed to "操作步骤" rather than "步骤".
    return tuple(sorted(out, key=len, reverse=True))


class RoutingRules:
    """Compiled marker rules for one configuration.

    Built by :func:`rules_from_config`; cheap enough to rebuild per call, so no
    cache is kept that could go stale when a test swaps the config.
    """

    def __init__(
        self,
        extra_skill: Iterable[str] = (),
        extra_todo: Iterable[str] = (),
        extra_profile: Iterable[str] = (),
        extra_knowledge: Iterable[str] = (),
        disabled: Iterable[str] = (),
    ) -> None:
        off = {str(d).strip() for d in disabled if str(d).strip()}
        self.skill = self._compile(DEFAULT_SKILL_ACTION_MARKERS, extra_skill, off)
        self.todo = self._compile(
            (*DEFAULT_OPEN_MARKERS, *DEFAULT_TODO_EXTRA_MARKERS), extra_todo, off
        )
        self.profile = self._compile(DEFAULT_PROFILE_MARKERS, extra_profile, off)
        self.profile_scope = self._compile(DEFAULT_PROFILE_SCOPE_MARKERS, (), off)
        self.knowledge = self._compile(DEFAULT_KNOWLEDGE_MARKERS, extra_knowledge, off)
        self.ephemeral = self._compile(DEFAULT_EPHEMERAL_MARKERS, (), off)
        self.agent_other = self._compile(DEFAULT_AGENT_OTHER_MARKERS, (), off)

    @staticmethod
    def _compile(
        defaults: Iterable[str], extra: Iterable[str], disabled: set[str]
    ) -> tuple[tuple[str, re.Pattern], ...]:
        return tuple(
            (m, compile_marker(m)) for m in _merge_markers(defaults, extra, disabled)
        )

    @staticmethod
    def matches(
        rules: tuple[tuple[str, re.Pattern], ...], text: str, limit: int = 3
    ) -> list[str]:
        """Distinct markers from ``rules`` present in ``text`` (capped)."""
        found: list[str] = []
        for marker, rx in rules:
            if rx.search(text):
                found.append(marker)
                if len(found) >= limit:
                    break
        return found


def rules_from_config(config: Any | None) -> RoutingRules:
    """Rules for ``config`` (its ``routing:`` extensions over the defaults)."""
    if config is None:
        return RoutingRules()
    markers = getattr(config, "routing_markers", None) or {}
    return RoutingRules(
        extra_skill=markers.get("extra_skill_markers", ()),
        extra_todo=markers.get("extra_todo_markers", ()),
        extra_profile=markers.get("extra_profile_markers", ()),
        extra_knowledge=markers.get("extra_knowledge_markers", ()),
        disabled=markers.get("disabled_markers", ()),
    )


# ---------------------------------------------------------------------------
# Config accessors (tolerant — a partial or absent config must still work)
# ---------------------------------------------------------------------------


def routing_settings(config: Any | None) -> tuple[bool, RoutingMode]:
    """``(enabled, mode)`` for ``config``, falling back to enabled+advisory."""
    enabled = True
    mode: str = "advisory"
    if config is not None:
        enabled = bool(getattr(config, "routing_enabled", True))
        mode = str(getattr(config, "routing_mode", "advisory") or "advisory").lower()
    if mode not in ("advisory", "enforce"):
        mode = "advisory"
    return enabled, mode  # type: ignore[return-value]


def agent_extension_settings(config: Any | None) -> dict[str, Any]:
    """Normalised ``agent_extensions`` view, with conservative defaults.

    Never raises and never trusts the shape: a half-written config section
    degrades key by key to "disabled / suggest-only", because the failure mode
    of guessing here would be an unrequested write to somebody else's store.
    """
    default: dict[str, Any] = {
        "enabled": False,
        "mode": "suggest",
        "adapter": None,
        "root": None,
        "surfaces": {
            s.value: {"enabled": False, "handler": None, "path": None}
            for s in EXTENSION_SURFACES
        },
    }
    raw = getattr(config, "agent_extensions", None) if config is not None else None
    if not isinstance(raw, dict):
        return default

    out = dict(default)
    out["enabled"] = bool(raw.get("enabled", False))
    mode = str(raw.get("mode") or "suggest").strip().lower()
    out["mode"] = mode if mode in ("suggest", "apply_with_confirmation", "apply") else "suggest"
    adapter = raw.get("adapter")
    out["adapter"] = str(adapter) if adapter else None
    root = raw.get("root")
    out["root"] = str(root) if root else None

    surfaces = {
        s.value: {"enabled": False, "handler": None, "path": None}
        for s in EXTENSION_SURFACES
    }
    raw_surfaces = raw.get("surfaces")
    if isinstance(raw_surfaces, dict):
        for key, value in raw_surfaces.items():
            name = str(key).strip().lower()
            if name not in surfaces or not isinstance(value, dict):
                continue
            handler = value.get("handler")
            path = value.get("path")
            surfaces[name] = {
                "enabled": bool(value.get("enabled", False)),
                "handler": str(handler) if handler else None,
                "path": str(path) if path else None,
            }
    out["surfaces"] = surfaces
    return out


def _handler_readiness(
    handler: str | None, surface: MemorySurface, config: Any | None
) -> tuple[bool, list[str], str | None, str]:
    """``(registered, blockers, target, detail)`` for a configured handler name.

    Imported lazily: ``adapters.base`` imports this module for the surface enum
    and the action model, so a module-level import here would be a cycle. A
    broken or missing adapter layer degrades to "not ready", never to "ready".
    """
    try:
        from .adapters.base import adapter_readiness, registry
    except Exception:  # noqa: BLE001 — reflection must never raise
        return False, [BLOCK_ADAPTER_LAYER_UNAVAILABLE], None, ""

    adapter = registry.get(handler)
    if adapter is None:
        return False, [BLOCK_HANDLER_NOT_REGISTERED], None, ""
    try:
        supports = bool(adapter.supports(MemorySurface(surface)))
    except Exception:  # noqa: BLE001 — a broken probe is not a permission
        supports = False
    if not supports:
        return True, [BLOCK_HANDLER_CANNOT_WRITE_SURFACE], None, ""

    probe = adapter_readiness(adapter, surface, config)
    return (
        True,
        [] if probe.ready else list(probe.blockers) or [BLOCK_HANDLER_NOT_READY],
        probe.target,
        probe.detail,
    )


def extension_readiness(
    surface: MemorySurface, config: Any | None
) -> dict[str, Any]:
    """Everything that decides whether an extension surface can be applied.

    One computation shared by the capability reflection and by
    ``adapters.base.apply_agent_action``, so the two can never disagree about
    whether a write is possible. Read-only and never raises.

    Returns ``{surface, enabled, handler, handler_registered, mode,
    configured_path, target, needs_confirmation, can_apply, blockers, detail}``.
    ``blockers`` is empty exactly when ``can_apply`` is true.
    """
    surface = MemorySurface(surface)
    settings = agent_extension_settings(config)
    entry = settings["surfaces"].get(surface.value, {})
    handler = entry.get("handler")
    mode: ApplicationMode = settings["mode"]

    blockers: list[str] = []
    if not settings["enabled"]:
        blockers.append(BLOCK_EXTENSIONS_DISABLED)
    if not entry.get("enabled"):
        blockers.append(BLOCK_SURFACE_DISABLED)
    if mode == "suggest":
        blockers.append(BLOCK_MODE_SUGGEST)
    if not handler:
        blockers.append(BLOCK_NO_HANDLER)

    registered = False
    target: str | None = None
    detail = ""
    if handler:
        registered, handler_blockers, target, detail = _handler_readiness(
            handler, surface, config
        )
        blockers.extend(handler_blockers)

    return {
        "surface": surface.value,
        "enabled": bool(settings["enabled"]) and bool(entry.get("enabled")),
        "handler": handler,
        "handler_registered": registered,
        "mode": mode,
        "configured_path": entry.get("path"),
        "target": target,
        "needs_confirmation": mode == "apply_with_confirmation",
        "can_apply": not blockers,
        "blockers": blockers,
        "detail": detail,
    }


def _apply_capability(
    surface: MemorySurface, config: Any | None
) -> tuple[bool, bool, ApplicationMode, str | None]:
    """``(framework_can_apply, agent_should_apply, application_mode, handler)``.

    The one place these are decided, so no caller can assemble the impossible
    combination "agent-owned surface the framework silently writes anyway".

    For an extension surface this is the *real* readiness, not just the config
    switches: a handler that is named but not registered, or registered but
    unable to resolve a safe target, reports ``framework_can_apply: False``.
    Saying otherwise would promise a write that ``apply_agent_action`` then
    refuses.
    """
    owner = ownership_of(surface)
    if owner is SurfaceOwnership.FRAMEWORK:
        return True, False, "apply", None
    if owner is SurfaceOwnership.NONE:
        return False, False, "suggest", None

    readiness = extension_readiness(surface, config)
    can_apply = bool(readiness["can_apply"])
    return (
        can_apply,
        not can_apply,
        readiness["mode"] if can_apply else "suggest",
        readiness["handler"],
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_SHAPE_HINTS: dict[MemorySurface, list[str]] = {
    MemorySurface.SKILL: [
        "title", "when_to_use", "preconditions", "steps", "verification", "pitfalls",
    ],
    MemorySurface.USER_PROFILE: [
        "preference_statement", "scope", "stability", "confirmation",
    ],
    MemorySurface.AGENT_OTHER: ["title", "body", "provenance"],
}

_DEFAULT_OPERATION: dict[MemorySurface, Literal["create", "update", "append"]] = {
    MemorySurface.SKILL: "create",
    MemorySurface.USER_PROFILE: "update",
    MemorySurface.AGENT_OTHER: "create",
}


def _marker_signal(
    surface: MemorySurface, markers: list[str], weight: float
) -> Signal:
    return Signal(
        kind="marker",
        name=f"{surface.value}_markers",
        surface=surface,
        evidence=", ".join(markers),
        weight=round(weight * len(markers), 3),
    )


def _normalize_supported(
    supported: Sequence[str] | Sequence[MemorySurface] | None,
) -> tuple[set[MemorySurface] | None, list[str]]:
    """``(declared surfaces, unknown names)``.

    ``None`` means "the caller declared nothing", i.e. no constraint. An empty
    set is different: the caller declared a list and nothing in it is a surface
    we know, so we withhold the action rather than assume they can take it.
    """
    if supported is None:
        return None, []
    out: set[MemorySurface] = set()
    unknown: list[str] = []
    for item in supported:
        raw = getattr(item, "value", item)
        try:
            out.add(MemorySurface(raw))
        except ValueError:
            unknown.append(str(raw))
    return out, unknown


def classify_memory_item(
    content: str,
    *,
    config: Any | None = None,
    domain: str | None = None,
    section: str | None = None,
    supported_surfaces: Sequence[str] | Sequence[MemorySurface] | None = None,
    mode: str | None = None,
) -> RoutingDecision:
    """Route one candidate memory item to a surface. Read-only, no model call.

    Args:
        content: the candidate item's text.
        config: a ``MemoryConfig``-like object. Supplies marker extensions, the
            routing mode and the ``agent_extensions`` capability. ``None`` means
            "shipped defaults".
        domain: optional target domain, used as context in the reason/metadata.
        section: optional target heading. Scanned for markers alongside the
            content (a heading like "发布流程" is part of the item), but never
            for structure.
        supported_surfaces: surfaces the caller can actually honour. When the
            decided surface is not among them, no ``suggested_action`` is
            produced — an action aimed at a surface the caller cannot write is
            worse than no action at all.
        mode: per-call override of the routing mode (``advisory`` / ``enforce``).
            Reported, never used to mutate anything from inside this module.

    Returns:
        A :class:`RoutingDecision`. ``surface`` is a recommendation; in advisory
        mode nothing in the framework changes behaviour because of it.
    """
    enabled, cfg_mode = routing_settings(config)
    eff_mode: RoutingMode = cfg_mode
    if mode:
        candidate = str(mode).strip().lower()
        if candidate in ("advisory", "enforce"):
            eff_mode = candidate  # type: ignore[assignment]

    body = content or ""
    marker_text = "\n".join(p for p in (section or "", body) if p)
    rules = rules_from_config(config)
    struct = _Structure(body)
    supported_set, unknown_surfaces = _normalize_supported(supported_surfaces)

    signals: list[Signal] = []
    scores: dict[MemorySurface, float] = {}
    reasons: dict[MemorySurface, str] = {}
    warnings: list[str] = []

    if not body.strip():
        return _finalize(
            RoutingDecision(
                surface=MemorySurface.UNDECIDED,
                ownership=ownership_of(MemorySurface.UNDECIDED),
                score=0.0,
                confidence_basis="insufficient",
                reason="empty content — nothing to route",
                mode=eff_mode,
            ),
            config=config,
            content=body,
            section=section,
            domain=domain,
            supported=supported_set,
            unknown_surfaces=unknown_surfaces,
        )

    # --- TODO: unfinished state -------------------------------------------
    todo_hits = RoutingRules.matches(rules.todo, marker_text)
    if todo_hits:
        signals.append(_marker_signal(MemorySurface.TODO, todo_hits, 0.10))
        scores[MemorySurface.TODO] = 0.55 + 0.10 * len(todo_hits)
        reasons[MemorySurface.TODO] = (
            "carries open/blocked process state ("
            + ", ".join(todo_hits)
            + ") — unfinished work belongs in the TODO store, not in durable L1"
        )

    # --- EPHEMERAL: raw trace / one-off output -----------------------------
    eph_hits = RoutingRules.matches(rules.ephemeral, marker_text)
    if struct.log_shaped:
        signals.append(Signal(
            kind="structure", name="log_shaped",
            surface=MemorySurface.EPHEMERAL,
            evidence=f"{struct.log_lines}/{struct.nonempty} lines look like log output",
            weight=0.30,
        ))
        scores[MemorySurface.EPHEMERAL] = 0.80 + 0.03 * len(eph_hits)
        reasons[MemorySurface.EPHEMERAL] = (
            "raw process trace: most lines are log output — evidence, not durable "
            "cognition"
        )
    if eph_hits:
        signals.append(_marker_signal(MemorySurface.EPHEMERAL, eph_hits, 0.08))
        scores.setdefault(MemorySurface.EPHEMERAL, 0.48 + 0.08 * len(eph_hits))
        reasons.setdefault(
            MemorySurface.EPHEMERAL,
            "one-off/non-durable content (" + ", ".join(eph_hits) + ")",
        )

    # --- SKILL: action protocol (markers AND structure) -------------------
    skill_hits = RoutingRules.matches(rules.skill, marker_text)
    skill_struct = struct.skill_signals()
    if skill_hits and skill_struct and not struct.log_shaped:
        signals.append(_marker_signal(MemorySurface.SKILL, skill_hits, 0.08))
        signals.extend(skill_struct)
        scores[MemorySurface.SKILL] = (
            0.52 + 0.08 * len(skill_hits) + sum(s.weight for s in skill_struct)
        )
        reasons[MemorySurface.SKILL] = (
            "repeatable action protocol: action markers ("
            + ", ".join(skill_hits)
            + ") plus procedural structure ("
            + ", ".join(s.name for s in skill_struct)
            + ")"
        )
    elif skill_hits and not skill_struct:
        signals.append(Signal(
            kind="negative", name="skill_markers_without_structure",
            surface=MemorySurface.SKILL,
            evidence=", ".join(skill_hits),
            weight=0.0,
        ))
    elif skill_hits and struct.log_shaped:
        signals.append(Signal(
            kind="negative", name="skill_suppressed_by_log_shape",
            surface=MemorySurface.SKILL,
            evidence="action markers appear inside raw log output",
            weight=0.0,
        ))

    # --- USER_PROFILE: standing global preference --------------------------
    profile_hits = RoutingRules.matches(rules.profile, marker_text)
    scope_hits = RoutingRules.matches(rules.profile_scope, marker_text)
    if profile_hits and scope_hits:
        signals.append(_marker_signal(MemorySurface.USER_PROFILE, profile_hits, 0.08))
        signals.append(Signal(
            kind="marker", name="global_scope_markers",
            surface=MemorySurface.USER_PROFILE,
            evidence=", ".join(scope_hits), weight=0.08 * len(scope_hits),
        ))
        scores[MemorySurface.USER_PROFILE] = (
            0.56 + 0.07 * len(profile_hits) + 0.05 * len(scope_hits)
        )
        reasons[MemorySurface.USER_PROFILE] = (
            "standing global user preference ("
            + ", ".join(profile_hits)
            + ") stated with cross-context scope ("
            + ", ".join(scope_hits)
            + ")"
        )
    elif profile_hits:
        signals.append(_marker_signal(MemorySurface.USER_PROFILE, profile_hits, 0.06))
        scores[MemorySurface.USER_PROFILE] = 0.38 + 0.04 * len(profile_hits)
        reasons[MemorySurface.USER_PROFILE] = (
            "preference wording (" + ", ".join(profile_hits) + ") but no global-scope "
            "marker — may be a project-local convention rather than a profile fact"
        )

    # --- AGENT_OTHER: agent-owned, not skill/profile -----------------------
    other_hits = RoutingRules.matches(rules.agent_other, marker_text)
    if other_hits:
        signals.append(_marker_signal(MemorySurface.AGENT_OTHER, other_hits, 0.08))
        scores[MemorySurface.AGENT_OTHER] = 0.46 + 0.07 * len(other_hits)
        reasons[MemorySurface.AGENT_OTHER] = (
            "explicitly agent-owned memory content (" + ", ".join(other_hits) + ")"
        )

    # --- L1_KNOWLEDGE: durable domain cognition ---------------------------
    know_hits = RoutingRules.matches(rules.knowledge, marker_text, limit=4)
    if know_hits:
        signals.append(_marker_signal(MemorySurface.L1_KNOWLEDGE, know_hits, 0.07))
        score = 0.45 + 0.07 * len(know_hits)
        if struct.chars >= MIN_CHARS_FOR_FALLBACK and not skill_struct:
            signals.append(Signal(
                kind="structure", name="declarative_prose",
                surface=MemorySurface.L1_KNOWLEDGE,
                evidence=f"{struct.chars} chars of prose, no procedural structure",
                weight=0.06,
            ))
            score += 0.06
        scores[MemorySurface.L1_KNOWLEDGE] = score
        reasons[MemorySurface.L1_KNOWLEDGE] = (
            "durable domain cognition (" + ", ".join(know_hits) + ")"
        )

    # --- pick a winner -----------------------------------------------------
    if scores:
        best = max(
            scores.items(),
            key=lambda kv: (round(kv[1], 6), -_TIE_BREAK.index(kv[0])),
        )
        surface, score = best[0], min(round(best[1], 3), 1.0)
        reason = reasons.get(surface, "")
    else:
        surface, score, reason = MemorySurface.UNDECIDED, 0.0, ""

    if surface is not MemorySurface.UNDECIDED and score < SCORE_FLOOR:
        warnings.append(
            f"weak signal for {surface.value} (score {score:.2f} < {SCORE_FLOOR}); "
            "treating the item as unrouted"
        )
        surface, score, reason = MemorySurface.UNDECIDED, score, ""

    if surface is MemorySurface.UNDECIDED and not reason:
        if struct.chars >= MIN_CHARS_FOR_FALLBACK:
            # Substantive prose with no decisive marker: keep the long-standing
            # behaviour (durable knowledge) rather than invent a new default,
            # but say plainly that the call rests on nothing.
            surface = MemorySurface.L1_KNOWLEDGE
            score = min(score, 0.35)
            reason = (
                "no decisive marker matched; substantive prose defaults to durable "
                "knowledge — review the target domain/section yourself"
            )
            warnings.append(
                "classification rests on length alone, not on markers or structure"
            )
        else:
            reason = (
                "too little signal to route: short item with no marker and no "
                "procedural structure"
            )

    decision = RoutingDecision(
        surface=surface,
        ownership=ownership_of(surface),
        score=score,
        reason=reason,
        signals=signals,
        warnings=warnings,
        mode=eff_mode,
    )
    decision.alternatives = [
        RoutingAlternative(
            surface=s,
            ownership=ownership_of(s),
            score=min(round(v, 3), 1.0),
            reason=reasons.get(s, ""),
        )
        for s, v in sorted(scores.items(), key=lambda kv: -kv[1])
        if s is not surface
    ]
    if not enabled:
        decision.warnings.append(
            "routing is disabled in config (routing.enabled=false); this decision "
            "is informational only"
        )
    return _finalize(
        decision,
        config=config,
        content=body,
        section=section,
        domain=domain,
        supported=supported_set,
        unknown_surfaces=unknown_surfaces,
    )


def _basis(signals: list[Signal], surface: MemorySurface) -> ConfidenceBasis:
    kinds = {
        s.kind for s in signals
        if s.surface is surface and s.kind in ("marker", "structure")
    }
    if {"marker", "structure"} <= kinds:
        return "mixed"
    if "marker" in kinds:
        return "markers"
    if "structure" in kinds:
        return "structure"
    return "insufficient"


def _finalize(
    decision: RoutingDecision,
    *,
    config: Any | None,
    content: str,
    section: str | None,
    domain: str | None,
    supported: set[MemorySurface] | None,
    unknown_surfaces: list[str] | None = None,
) -> RoutingDecision:
    """Derive ownership-independent capability fields and the suggested action."""
    decision.ownership = ownership_of(decision.surface)
    decision.confidence_basis = _basis(decision.signals, decision.surface)
    if unknown_surfaces:
        decision.warnings.append(
            "ignored unknown supported_surfaces: " + ", ".join(unknown_surfaces)
        )

    can_apply, agent_should, app_mode, handler = _apply_capability(
        decision.surface, config
    )
    decision.framework_can_apply = can_apply
    decision.agent_should_apply = agent_should
    decision.application_mode = app_mode
    decision.configured_handler = handler

    if decision.surface not in EXTENSION_SURFACES:
        decision.suggested_action = None
        return decision

    if supported is not None and decision.surface not in supported:
        decision.suggested_action = None
        decision.agent_should_apply = False
        decision.warnings.append(
            f"surface '{decision.surface.value}' is not in the caller's supported "
            "surfaces — no agent action suggested; keep the item with the agent or "
            "the user instead of forcing it into a core surface"
        )
        return decision

    requires_confirmation = (
        decision.surface is MemorySurface.USER_PROFILE
        or app_mode == "apply_with_confirmation"
    )
    decision.suggested_action = AgentAction(
        surface=decision.surface,
        operation=_DEFAULT_OPERATION[decision.surface],
        title=(section or None),
        source_content=content.strip(),
        shape_hint=list(_SHAPE_HINTS.get(decision.surface, [])),
        rationale=decision.reason,
        requires_user_confirmation=requires_confirmation,
        metadata={
            "domain": domain,
            "score": decision.score,
            "confidence_basis": decision.confidence_basis,
            "owned_by": decision.ownership.value,
            "framework_can_apply": decision.framework_can_apply,
            "application_mode": decision.application_mode,
            "configured_handler": decision.configured_handler,
        },
    )
    return decision


# ---------------------------------------------------------------------------
# Capability reflection
# ---------------------------------------------------------------------------

_CORE_NOTES: dict[MemorySurface, str] = {
    MemorySurface.L0_INDEX: "derived knowledge map; rebuilt by sync_l0_index",
    MemorySurface.L1_KNOWLEDGE: "source of truth for durable domain cognition",
    MemorySurface.TODO: "unfinished state: pending / blocked / needs validation",
    MemorySurface.MEMORY_CHANGELOG: (
        "provenance of semantic L0/L1 mutations only — NOT the repository "
        "CHANGELOG.md, not session logs, not TODO history"
    ),
}

#: Appended to the changelog note depending on the backend's configured state,
#: so the capability output never implies a store that is not recording.
_CHANGELOG_ENABLED_NOTE = (
    ". Append-only JSONL store; read it with get_memory_changelog / "
    "get_memory_changelog_stats. Retention is manual — nothing prunes it"
)
_CHANGELOG_DISABLED_NOTE = (
    ". DISABLED by config (memory_changelog.enabled: false): no event is "
    "recorded and the read tools report an empty log"
)


def _changelog_capability(config: Any | None) -> tuple[bool, str, str | None]:
    """``(can_write, note suffix, resolved store path)`` for the changelog.

    Imported lazily: ``memory_changelog`` imports this module for the surface
    enum, so a module-level import here would be a cycle.
    """
    try:
        from .memory_changelog import changelog_settings

        settings = changelog_settings(config)
        enabled = bool(settings["enabled"]) and settings["path"] is not None
        path = str(settings["path"]) if settings["path"] is not None else None
    except Exception:  # noqa: BLE001 — reflection must never raise
        return False, _CHANGELOG_DISABLED_NOTE, None
    return (
        enabled,
        _CHANGELOG_ENABLED_NOTE if enabled else _CHANGELOG_DISABLED_NOTE,
        path,
    )


def _environment_capability(config: Any | None) -> dict[str, Any]:
    """Reflect the resolved agent-environment adapter. Imported lazily.

    The router must not depend on the adapter package at module level — that is
    the boundary this whole layer exists to keep — and reflection must never
    raise, so a broken adapter degrades to an empty dict.
    """
    try:
        from .adapters.environment import describe_environment

        return describe_environment(config)
    except Exception:  # noqa: BLE001 — reflection must never raise
        return {}


def _registered_handlers() -> list[str]:
    """Extension-action handler names registered in this process. Never raises."""
    try:
        from .adapters.base import registry

        return registry.names()
    except Exception:  # noqa: BLE001 — reflection must never raise
        return []


_EXTENSION_NOTES: dict[MemorySurface, str] = {
    MemorySurface.SKILL: "action protocols, methods, runbooks, commands, pitfalls",
    MemorySurface.USER_PROFILE: "small global user preference / persona facts",
    MemorySurface.AGENT_OTHER: (
        "agent-owned memory-like content the framework will not classify further"
    ),
}


def surface_capabilities(config: Any | None = None) -> MemorySurfaceCapabilities:
    """Describe every surface under ``config``. Read-only; touches no store."""
    enabled, mode = routing_settings(config)
    settings = agent_extension_settings(config)
    changelog_can_write, changelog_note, changelog_path = _changelog_capability(config)

    caps = MemorySurfaceCapabilities(
        routing_enabled=enabled,
        routing_mode=mode,
        agent_extensions_enabled=bool(settings["enabled"]),
        agent_extensions_mode=settings["mode"],
        adapter=settings["adapter"],
        agent_extensions_root=settings.get("root"),
        registered_handlers=_registered_handlers(),
        agent_environment=_environment_capability(config),
        source_owned_evidence=["raw_session"],
        derived_stores=["l0_index", "vector_store", "search_index"],
        notes=[
            "Core surfaces are framework-owned: the framework may write them.",
            "Extension surfaces stay agent-owned no matter how they are "
            "configured; 'can_apply' only says whether the framework may act as "
            "an orchestrator for them.",
            "Raw session data is source-owned, read-only evidence — the framework "
            "scans it but never writes it.",
            "MEMORY_CHANGELOG records semantic L0/L1 mutations only — not raw "
            "sessions, not TODO history, not derived-data rebuilds, and not a "
            "generic event log.",
        ],
    )

    for surface in CORE_SURFACES:
        can_apply, _agent, app_mode, handler = _apply_capability(surface, config)
        note = _CORE_NOTES.get(surface, "")
        store_path = None
        if surface is MemorySurface.MEMORY_CHANGELOG:
            # The one core surface whose writability is a config question
            # rather than a given: with the backend off nothing is recorded,
            # and saying `can_apply: true` anyway would be the overclaim the
            # previous version's "no store yet" note existed to avoid.
            can_apply = changelog_can_write
            app_mode = "apply" if changelog_can_write else "suggest"
            note += changelog_note
            store_path = changelog_path
        caps.core_surfaces[surface.value] = SurfaceCapability(
            surface=surface,
            owned_by=ownership_of(surface),
            default_behavior="apply",
            configured_handler=handler,
            can_apply=can_apply,
            application_mode=app_mode,
            store_path=store_path,
            notes=note,
        )

    for surface in EXTENSION_SURFACES:
        readiness = extension_readiness(surface, config)
        can_apply = bool(readiness["can_apply"])
        note = _EXTENSION_NOTES.get(surface, "")
        if not can_apply:
            note += (
                " — suggestion-only: apply_agent_action would refuse ("
                + ", ".join(readiness["blockers"])
                + "). Enable agent_extensions, enable this surface, name a "
                "registered handler, set a mode other than 'suggest', and give "
                "the handler an explicit target"
            )
        caps.extension_surfaces[surface.value] = SurfaceCapability(
            surface=surface,
            owned_by=ownership_of(surface),
            default_behavior="suggest",
            configured_handler=readiness["handler"],
            handler_registered=bool(readiness["handler_registered"]),
            can_apply=can_apply,
            application_mode=readiness["mode"] if can_apply else "suggest",
            apply_blockers=list(readiness["blockers"]),
            store_path=readiness.get("target"),
            notes=note,
        )

    for surface in NO_WRITE_SURFACES:
        caps.no_write_surfaces[surface.value] = SurfaceCapability(
            surface=surface,
            owned_by=ownership_of(surface),
            default_behavior="none",
            can_apply=False,
            application_mode="suggest",
            notes="no durable write",
        )

    return caps


__all__ = [
    "AgentAction",
    "ApplicationMode",
    "ConfidenceBasis",
    "CORE_SURFACES",
    "EXTENSION_SURFACES",
    "MemorySurface",
    "MemorySurfaceCapabilities",
    "NO_WRITE_SURFACES",
    "RoutingAlternative",
    "RoutingDecision",
    "RoutingMode",
    "RoutingRules",
    "Signal",
    "SurfaceCapability",
    "SurfaceOwnership",
    "agent_extension_settings",
    "classify_memory_item",
    "extension_readiness",
    "ownership_of",
    "routing_settings",
    "rules_from_config",
    "surface_capabilities",
]

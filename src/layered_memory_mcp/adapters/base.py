"""Adapter protocol and gated execution path for agent-owned extension actions.

An adapter is the *only* place where "the SKILL surface" becomes a concrete
location in some agent product. The routing core deliberately knows nothing
about such locations; it decides a surface and hands back an
:class:`~layered_memory_mcp.routing.AgentAction`.

WHAT THIS MODULE DOES
---------------------
:func:`apply_agent_action` is the single entry point that may turn a
recommendation into a write. It is **closed by default**: every gate below must
be opened explicitly by the operator before one byte is written, and every
refusal is *reported* (a structured :class:`AgentActionResult`) rather than
silent.

The gates, in the order they are evaluated:

1. the action itself must validate — ``kind``, ``surface``, ``operation`` and a
   non-empty ``source_content``. An invalid action is refused, never coerced;
2. the surface must be an agent-owned extension surface. Core surfaces are
   written through the framework's own tools, not through here;
3. ``agent_extensions.enabled`` **and** that surface's ``enabled`` must be true;
4. ``agent_extensions.mode`` must not be ``suggest`` (the default);
5. a ``handler`` must be configured for the surface, an adapter must be
   registered under that name, and it must support the surface;
6. the adapter itself must report that it is ready — for the reference
   :mod:`.generic_file` handler that means an explicitly configured, safe target
   path;
7. under ``apply_with_confirmation`` (or for an action that asks for it), the
   caller must pass ``confirmed=True``;
8. ``dry_run=True`` reports what would happen and returns before any write.

Ownership never moves. A surface the framework may apply is still the agent's;
the framework is only acting as an explicitly configured orchestrator.

Applying an agent-owned action does **not** write ``MEMORY_CHANGELOG``: that log
is provenance for framework-owned L0/L1 mutations only.
"""

from __future__ import annotations

import inspect
from typing import Any, NamedTuple, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from ..routing import (
    BLOCK_ADAPTER_LAYER_UNAVAILABLE,
    BLOCK_EXTENSIONS_DISABLED,
    BLOCK_HANDLER_CANNOT_WRITE_SURFACE,
    BLOCK_HANDLER_NOT_READY,
    BLOCK_HANDLER_NOT_REGISTERED,
    BLOCK_HANDLER_READINESS_ERROR,
    BLOCK_MODE_SUGGEST,
    BLOCK_NOT_AN_EXTENSION_SURFACE,
    BLOCK_NO_HANDLER,
    BLOCK_SURFACE_DISABLED,
    EXTENSION_SURFACES,
    AgentAction,
    ApplicationMode,
    MemorySurface,
    agent_extension_settings,
)

# --- stable vocabulary -------------------------------------------------------
# The ``BLOCK_*`` reason codes live in ``routing`` (the lower layer) and are
# re-exported here; statuses are this layer's own.

#: Result statuses. ``applied`` is the only one that wrote anything.
STATUS_APPLIED = "applied"
STATUS_WOULD_APPLY = "would_apply"
STATUS_CONFIRMATION_REQUIRED = "confirmation_required"
STATUS_DISABLED = "disabled"
STATUS_UNSUPPORTED = "unsupported"
STATUS_REFUSED = "refused"
STATUS_INVALID_ACTION = "invalid_action"
STATUS_ERROR = "error"

#: Blocker for an action that never reached a gate — it did not validate.
BLOCK_INVALID_ACTION = "invalid_action"
#: Blocker for "wired up, mode allows it, but nobody has confirmed".
BLOCK_NOT_CONFIRMED = "not_confirmed"
#: Blocker for an adapter that cannot honour ``dry_run``.
BLOCK_DRY_RUN_UNSUPPORTED = "dry_run_unsupported"

#: Blockers that mean "the opt-in switches are off", i.e. status ``disabled``.
_DISABLED_BLOCKERS = (BLOCK_EXTENSIONS_DISABLED, BLOCK_SURFACE_DISABLED)
#: Blockers that mean "nothing is wired up", i.e. status ``unsupported``.
_UNSUPPORTED_BLOCKERS = (
    BLOCK_NO_HANDLER,
    BLOCK_HANDLER_NOT_REGISTERED,
    BLOCK_HANDLER_CANNOT_WRITE_SURFACE,
)


class AgentActionResult(BaseModel):
    """Outcome of an attempt to apply an :class:`AgentAction`.

    ``applied`` is true for exactly one status, ``applied``. Everything else —
    including ``would_apply`` — wrote nothing.
    """

    applied: bool = False
    status: str = STATUS_UNSUPPORTED
    surface: MemorySurface | None = None
    operation: str | None = None
    handler: str | None = None
    adapter: str | None = None
    mode: ApplicationMode = "suggest"
    dry_run: bool = False
    confirmed: bool = False
    #: Where the adapter resolved (or would resolve) the write. Reported for
    #: refusals too, so a caller can see what it was about to touch.
    target: str | None = None
    #: Stable reason codes for why this did not apply. Empty on success.
    blockers: list[str] = Field(default_factory=list)
    detail: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AdapterReadiness(NamedTuple):
    """An adapter's own answer to "could you write this surface right now?".

    Returned from the optional :meth:`AgentExtensionAdapter.readiness` hook.
    ``blockers`` carries adapter-specific reason codes (the reference handler
    uses ``no_configured_path`` / ``unsafe_target`` / ...), which surface in both
    the capability output and the refusal result.
    """

    ready: bool
    blockers: tuple[str, ...] = ()
    target: str | None = None
    detail: str = ""


@runtime_checkable
class AgentExtensionAdapter(Protocol):
    """What an integration must implement to apply extension actions.

    Implementations own their own paths, formats and locking. They must treat
    ``action.source_content`` as the user's content and must not invent prose.

    ``apply`` may declare ``config`` and/or ``dry_run`` parameters; they are
    passed when present. An adapter that declares no ``dry_run`` parameter is
    never called under ``dry_run=True`` — the framework refuses instead of
    risking a write it promised not to make.
    """

    #: Handler name an operator puts in ``agent_extensions.surfaces.*.handler``.
    name: str

    def supports(self, surface: MemorySurface) -> bool:
        """Whether this adapter can write ``surface``."""
        ...

    def apply(self, action: AgentAction) -> AgentActionResult:
        """Apply ``action`` to the agent-owned surface it names."""
        ...


class AdapterRegistry:
    """Name → adapter lookup.

    Ships with exactly one entry, the inert reference handler
    :mod:`.generic_file`, which still writes nothing until it is both named as a
    surface's ``handler`` and given an explicit target path.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, AgentExtensionAdapter] = {}

    def register(self, adapter: AgentExtensionAdapter) -> None:
        name = str(getattr(adapter, "name", "") or "").strip()
        if not name:
            raise ValueError("adapter must expose a non-empty 'name'")
        self._adapters[name] = adapter

    def unregister(self, name: str) -> None:
        self._adapters.pop(str(name), None)

    def get(self, name: str | None) -> AgentExtensionAdapter | None:
        if not name:
            return None
        return self._adapters.get(str(name))

    def names(self) -> list[str]:
        return sorted(self._adapters)


#: Process-wide registry. See :class:`AdapterRegistry` for what ships in it.
registry = AdapterRegistry()


def adapter_readiness(
    adapter: AgentExtensionAdapter, surface: MemorySurface, config: Any | None
) -> AdapterReadiness:
    """Ask ``adapter`` whether it could write ``surface`` under ``config``.

    The hook is optional: an adapter that does not implement it is taken at its
    word (``ready``), because only the adapter knows what it needs. A hook that
    raises is treated as *not* ready — reflection must never raise, and an
    adapter that cannot answer must not be trusted with a write.
    """
    probe = getattr(adapter, "readiness", None)
    if probe is None:
        return AdapterReadiness(True)
    try:
        result = probe(MemorySurface(surface), config)
    except Exception as exc:  # noqa: BLE001 — a probe must not break the caller
        return AdapterReadiness(
            False, (BLOCK_HANDLER_READINESS_ERROR,), None, str(exc)
        )
    if isinstance(result, AdapterReadiness):
        return result
    if isinstance(result, tuple) and result:
        ready = bool(result[0])
        blockers = tuple(str(b) for b in (result[1] if len(result) > 1 else ()))
        target = str(result[2]) if len(result) > 2 and result[2] else None
        detail = str(result[3]) if len(result) > 3 and result[3] else ""
        return AdapterReadiness(ready, blockers, target, detail)
    return AdapterReadiness(bool(result))


def resolve_adapter(
    config: Any | None, surface: MemorySurface
) -> AgentExtensionAdapter | None:
    """The adapter configured for ``surface``, or ``None``.

    ``None`` is returned whenever anything in the opt-in chain is missing:
    extensions disabled, this surface disabled, no handler named, or no adapter
    registered under that handler. Deliberately *not* a readiness check — see
    :func:`~layered_memory_mcp.routing.extension_readiness` for that.
    """
    settings = agent_extension_settings(config)
    if not settings["enabled"]:
        return None
    entry = settings["surfaces"].get(MemorySurface(surface).value, {})
    if not entry.get("enabled"):
        return None
    adapter = registry.get(entry.get("handler"))
    if adapter is None or not adapter.supports(MemorySurface(surface)):
        return None
    return adapter


# ---------------------------------------------------------------------------
# Action validation — explicit refusal, never coercion
# ---------------------------------------------------------------------------

#: An action whose text is longer than this is refused outright rather than
#: silently truncated by the core; a handler may still bound what it writes.
MAX_SOURCE_CONTENT_CHARS = 200_000

_VALID_OPERATIONS = ("create", "update", "append")


def validate_action(action: Any) -> tuple[AgentAction | None, str]:
    """``(action, "")`` when ``action`` is a usable :class:`AgentAction`.

    Accepts an :class:`AgentAction` or its dict serialisation. On failure returns
    ``(None, reason)`` — nothing is guessed, defaulted or repaired, because a
    half-understood action is the one case where writing would be worse than
    doing nothing.
    """
    if isinstance(action, AgentAction):
        parsed = action
    elif isinstance(action, dict):
        try:
            parsed = AgentAction.model_validate(action)
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            return None, f"action does not validate as an AgentAction — {errors}"
    else:
        return None, (
            "action must be an AgentAction or a dict of its fields, got "
            f"{type(action).__name__}"
        )

    if parsed.kind != "agent_native_write":
        return None, f"unsupported action kind {parsed.kind!r}"
    if parsed.operation not in _VALID_OPERATIONS:
        return None, (
            f"unsupported operation {parsed.operation!r}; expected one of "
            + ", ".join(_VALID_OPERATIONS)
        )
    if not (parsed.source_content or "").strip():
        return None, (
            "source_content is empty — the framework never invents prose for an "
            "agent-owned surface, so there is nothing to write"
        )
    if len(parsed.source_content) > MAX_SOURCE_CONTENT_CHARS:
        return None, (
            f"source_content is {len(parsed.source_content)} chars, over the "
            f"{MAX_SOURCE_CONTENT_CHARS} limit; split it before applying"
        )
    return parsed, ""


# ---------------------------------------------------------------------------
# The gated execution path
# ---------------------------------------------------------------------------


def _invoke(
    adapter: AgentExtensionAdapter,
    action: AgentAction,
    config: Any | None,
    dry_run: bool,
) -> AgentActionResult | None:
    """Call ``adapter.apply`` with whatever of ``config``/``dry_run`` it accepts.

    Returns ``None`` when ``dry_run`` was asked for and the adapter has no way
    to honour it — the caller then refuses rather than writing.
    """
    kwargs: dict[str, Any] = {}
    try:
        params = inspect.signature(adapter.apply).parameters
    except (TypeError, ValueError):  # pragma: no cover — exotic callables
        params = {}
    takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())
    if "config" in params or takes_kwargs:
        kwargs["config"] = config
    if "dry_run" in params or takes_kwargs:
        kwargs["dry_run"] = dry_run
    elif dry_run:
        return None
    return adapter.apply(action, **kwargs)


def apply_agent_action(
    action: AgentAction | dict[str, Any],
    config: Any | None = None,
    *,
    confirmed: bool = False,
    dry_run: bool = False,
) -> AgentActionResult:
    """Apply ``action`` through a configured adapter — refusing by default.

    See the module docstring for the full gate list. This function never raises
    and never writes unless every gate is open; a refusal is returned as a
    structured :class:`AgentActionResult` with ``applied=False``, a ``status``
    and the ``blockers`` that stopped it.

    Args:
        action: an :class:`AgentAction` or the dict form of one.
        config: a ``MemoryConfig``-like object carrying ``agent_extensions``.
        confirmed: the user has agreed to this specific write. Required under
            ``mode: apply_with_confirmation`` and for any action whose
            ``requires_user_confirmation`` is set.
        dry_run: report what would happen and return without writing.
    """
    parsed, invalid = validate_action(action)
    if parsed is None:
        raw_surface = (
            action.get("surface") if isinstance(action, dict) else None
        )
        return AgentActionResult(
            status=STATUS_INVALID_ACTION,
            surface=(
                raw_surface
                if isinstance(raw_surface, str)
                and raw_surface in MemorySurface._value2member_map_
                else None
            ),
            dry_run=dry_run,
            confirmed=confirmed,
            detail=invalid,
            error=invalid,
            blockers=[BLOCK_INVALID_ACTION],
        )

    surface = MemorySurface(parsed.surface)
    settings = agent_extension_settings(config)
    entry = settings["surfaces"].get(surface.value, {})
    base: dict[str, Any] = {
        "surface": surface,
        "operation": parsed.operation,
        "handler": entry.get("handler"),
        "adapter": settings["adapter"],
        "mode": settings["mode"],
        "dry_run": dry_run,
        "confirmed": confirmed,
    }

    if surface not in EXTENSION_SURFACES:
        return AgentActionResult(
            status=STATUS_REFUSED,
            blockers=[BLOCK_NOT_AN_EXTENSION_SURFACE],
            detail=(
                f"{surface.value} is not an agent-owned extension surface; core "
                "surfaces are written through the framework's own tools"
            ),
            **base,
        )

    # One shared readiness computation, so the capability reflection and this
    # execution path can never disagree about whether a write is possible.
    from ..routing import extension_readiness

    readiness = extension_readiness(surface, config)
    blockers = list(readiness["blockers"])
    base["target"] = readiness.get("target")

    if any(b in blockers for b in _DISABLED_BLOCKERS):
        return AgentActionResult(
            status=STATUS_DISABLED,
            blockers=blockers,
            detail=(
                "agent_extensions is not enabled for this surface — the framework "
                "only classifies and suggests. Apply the action yourself, or opt in "
                "via agent_extensions in config.yaml"
            ),
            **base,
        )
    if BLOCK_MODE_SUGGEST in blockers:
        return AgentActionResult(
            status=STATUS_DISABLED,
            blockers=blockers,
            detail=(
                "agent_extensions.mode is 'suggest': recommendation only, no "
                "framework write. The suggested_action is yours to apply"
            ),
            metadata={"recommendation_only": True},
            **base,
        )
    if any(b in blockers for b in _UNSUPPORTED_BLOCKERS):
        handler = base["handler"]
        if BLOCK_NO_HANDLER in blockers:
            detail = (
                f"no handler configured for surface {surface.value}; set "
                f"agent_extensions.surfaces.{surface.value}.handler to a "
                f"registered adapter (available: {', '.join(registry.names()) or 'none'})"
            )
        elif BLOCK_HANDLER_NOT_REGISTERED in blockers:
            detail = (
                f"no adapter registered under handler {handler!r}; "
                f"registered handlers: {', '.join(registry.names()) or 'none'}"
            )
        else:
            detail = f"adapter {handler!r} does not support surface {surface.value}"
        return AgentActionResult(
            status=STATUS_UNSUPPORTED, blockers=blockers, detail=detail, **base
        )
    if blockers:
        # Adapter-level refusal: wired up, but the handler says it cannot write
        # safely (no explicit path, unsafe target, ...).
        return AgentActionResult(
            status=STATUS_REFUSED,
            blockers=blockers,
            detail=readiness.get("detail")
            or f"handler {base['handler']!r} is not ready: " + ", ".join(blockers),
            **base,
        )

    adapter = registry.get(base["handler"])
    if adapter is None:  # pragma: no cover — readiness already proved otherwise
        return AgentActionResult(
            status=STATUS_UNSUPPORTED,
            blockers=[BLOCK_HANDLER_NOT_REGISTERED],
            detail=f"handler {base['handler']!r} disappeared from the registry",
            **base,
        )

    needs_confirmation = bool(
        readiness["needs_confirmation"] or parsed.requires_user_confirmation
    )
    if needs_confirmation and not confirmed:
        return AgentActionResult(
            status=STATUS_CONFIRMATION_REQUIRED,
            blockers=[BLOCK_NOT_CONFIRMED],
            detail=(
                "this action requires explicit user confirmation; re-call with "
                "confirmed=True once the user has agreed"
            ),
            metadata={
                "mode": settings["mode"],
                "action_requires_confirmation": parsed.requires_user_confirmation,
                "would_write_to": readiness.get("target"),
            },
            **base,
        )

    try:
        result = _invoke(adapter, parsed, config, dry_run)
    except Exception as exc:  # noqa: BLE001 — an adapter must not break the caller
        return AgentActionResult(
            status=STATUS_ERROR,
            error=str(exc),
            detail=f"adapter {base['handler']!r} raised",
            **base,
        )

    if result is None:
        return AgentActionResult(
            status=STATUS_REFUSED,
            blockers=[BLOCK_DRY_RUN_UNSUPPORTED],
            detail=(
                f"adapter {base['handler']!r} cannot honour dry_run, so the "
                "framework refused rather than risk a write"
            ),
            **base,
        )
    if not isinstance(result, AgentActionResult):
        return AgentActionResult(
            status=STATUS_ERROR,
            error=f"adapter returned {type(result).__name__}, expected AgentActionResult",
            detail=f"adapter {base['handler']!r} broke the result contract",
            **base,
        )

    # Fill in the context the adapter has no reason to repeat, without
    # overwriting anything it set deliberately.
    for field, value in base.items():
        if field in ("dry_run", "confirmed"):
            setattr(result, field, value)
        elif getattr(result, field, None) in (None, "", "suggest") and value is not None:
            setattr(result, field, value)
    result.applied = result.status == STATUS_APPLIED
    return result


__all__ = [
    "AdapterReadiness",
    "AdapterRegistry",
    "AgentActionResult",
    "AgentExtensionAdapter",
    "BLOCK_ADAPTER_LAYER_UNAVAILABLE",
    "BLOCK_DRY_RUN_UNSUPPORTED",
    "BLOCK_EXTENSIONS_DISABLED",
    "BLOCK_HANDLER_CANNOT_WRITE_SURFACE",
    "BLOCK_HANDLER_NOT_READY",
    "BLOCK_HANDLER_NOT_REGISTERED",
    "BLOCK_HANDLER_READINESS_ERROR",
    "BLOCK_INVALID_ACTION",
    "BLOCK_MODE_SUGGEST",
    "BLOCK_NOT_AN_EXTENSION_SURFACE",
    "BLOCK_NOT_CONFIRMED",
    "BLOCK_NO_HANDLER",
    "BLOCK_SURFACE_DISABLED",
    "MAX_SOURCE_CONTENT_CHARS",
    "STATUS_APPLIED",
    "STATUS_CONFIRMATION_REQUIRED",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "STATUS_INVALID_ACTION",
    "STATUS_REFUSED",
    "STATUS_UNSUPPORTED",
    "STATUS_WOULD_APPLY",
    "adapter_readiness",
    "apply_agent_action",
    "registry",
    "resolve_adapter",
    "validate_action",
]

"""Adapter protocol for applying agent-owned extension actions.

An adapter is the *only* place where "the SKILL surface" becomes a concrete
location in some agent product. The routing core deliberately knows nothing
about such locations; it decides a surface and hands back an
:class:`~layered_memory_mcp.routing.AgentAction`.

Status in this version: **interface only.** The registry ships empty, so
:func:`apply_agent_action` always refuses with ``status="unsupported"``. That is
the intended default — the framework must not write another system's store on a
guess. Wiring a real adapter takes three explicit steps by the operator:

1. enable ``agent_extensions`` and the specific surface in ``config.yaml``;
2. name a ``handler`` for that surface;
3. register an adapter under that handler name via :data:`registry`.

Until all three hold, classification and suggestion are all that happen.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..routing import (
    EXTENSION_SURFACES,
    AgentAction,
    MemorySurface,
    agent_extension_settings,
)


class AgentActionResult(BaseModel):
    """Outcome of an attempt to apply an :class:`AgentAction`."""

    applied: bool = False
    status: str = "unsupported"   # unsupported | disabled | applied | refused | error
    surface: MemorySurface
    handler: str | None = None
    adapter: str | None = None
    detail: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@runtime_checkable
class AgentExtensionAdapter(Protocol):
    """What an integration must implement to apply extension actions.

    Implementations own their own paths, formats and locking. They must treat
    ``action.source_content`` as the user's content and must not invent prose.
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
    """Name → adapter lookup. Empty until an integration registers itself."""

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


#: Process-wide registry. Ships empty on purpose.
registry = AdapterRegistry()


def resolve_adapter(
    config: Any | None, surface: MemorySurface
) -> AgentExtensionAdapter | None:
    """The adapter configured for ``surface``, or ``None``.

    ``None`` is returned whenever anything in the opt-in chain is missing:
    extensions disabled, this surface disabled, no handler named, or no adapter
    registered under that handler.
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


def apply_agent_action(
    action: AgentAction,
    config: Any | None = None,
    *,
    confirmed: bool = False,
) -> AgentActionResult:
    """Apply ``action`` through a configured adapter — refusing by default.

    Refuses (never raises, never writes) when:
      - the action targets a surface the framework does not orchestrate;
      - ``agent_extensions`` is disabled, or this surface has no handler;
      - no adapter is registered under the configured handler name;
      - the configured mode is ``suggest``;
      - the mode is ``apply_with_confirmation`` (or the action asks for
        confirmation) and ``confirmed`` is not True.
    """
    surface = MemorySurface(action.surface)
    settings = agent_extension_settings(config)
    entry = settings["surfaces"].get(surface.value, {})
    handler = entry.get("handler")
    base = {
        "surface": surface,
        "handler": handler,
        "adapter": settings["adapter"],
    }

    if surface not in EXTENSION_SURFACES:
        return AgentActionResult(
            status="refused",
            detail=(
                f"{surface.value} is not an agent-owned extension surface; core "
                "surfaces are written through the framework's own tools"
            ),
            **base,
        )
    if not settings["enabled"] or not entry.get("enabled"):
        return AgentActionResult(
            status="disabled",
            detail=(
                "agent_extensions is not enabled for this surface — the framework "
                "only classifies and suggests. Apply the action yourself, or opt in "
                "via agent_extensions in config.yaml"
            ),
            **base,
        )
    if settings["mode"] == "suggest":
        return AgentActionResult(
            status="disabled",
            detail="agent_extensions.mode is 'suggest': no framework write",
            **base,
        )
    adapter = registry.get(handler)
    if adapter is None:
        return AgentActionResult(
            status="unsupported",
            detail=(
                f"no adapter registered under handler {handler!r}; "
                f"registered handlers: {registry.names() or 'none'}"
            ),
            **base,
        )
    if not adapter.supports(surface):
        return AgentActionResult(
            status="unsupported",
            detail=f"adapter {handler!r} does not support surface {surface.value}",
            **base,
        )
    needs_confirmation = (
        settings["mode"] == "apply_with_confirmation" or action.requires_user_confirmation
    )
    if needs_confirmation and not confirmed:
        return AgentActionResult(
            status="refused",
            detail=(
                "this action requires explicit user confirmation; re-call with "
                "confirmed=True once the user has agreed"
            ),
            **base,
        )
    try:
        return adapter.apply(action)
    except Exception as exc:  # noqa: BLE001 — an adapter must not break the caller
        return AgentActionResult(
            status="error", error=str(exc), detail="adapter raised", **base
        )


__all__ = [
    "AdapterRegistry",
    "AgentActionResult",
    "AgentExtensionAdapter",
    "apply_agent_action",
    "registry",
    "resolve_adapter",
]

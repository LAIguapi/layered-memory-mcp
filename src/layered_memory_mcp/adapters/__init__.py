"""Optional integration adapters for agent-owned extension surfaces.

The framework classifies Skill / Profile / AgentOther content and returns an
:class:`~layered_memory_mcp.routing.AgentAction` recommendation. Applying that
action means writing into a store the *agent* owns, which is product-specific
work — so it lives here, behind an explicit opt-in, and never in the routing
core. No adapter ships enabled, and the core imports nothing from this package
at module level.
"""

from .base import (
    AdapterRegistry,
    AgentActionResult,
    AgentExtensionAdapter,
    apply_agent_action,
    registry,
    resolve_adapter,
)

__all__ = [
    "AdapterRegistry",
    "AgentActionResult",
    "AgentExtensionAdapter",
    "apply_agent_action",
    "registry",
    "resolve_adapter",
]

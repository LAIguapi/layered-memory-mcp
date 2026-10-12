"""Adapter/support layer — everything the common core must not know.

Two independent concerns live here, and they must not be confused:

1. **Environment adapters** (:mod:`.environment`) describe *where* an agent
   product keeps its own files and *what shape* they have: memory path, entry
   separator, state DB, session dir, char limit, persona target. Read-only, and
   registered by default — Hermes (:mod:`.hermes`) first, so an existing install
   resolves exactly the paths it always did, then the editor-hosted layouts
   (:mod:`.editors`), then a generic adapter that knows no product paths at all.
   The core asks this layer instead of hardcoding ``~/.hermes/...``.

2. **Extension-action adapters** (:mod:`.base`) *apply* a classified
   Skill / Profile / AgentOther action into a store the agent owns. That
   registry ships **empty** and nothing enables it implicitly, because writing
   another system's store on a guess is the one thing the framework must never
   do.

Resolving a path through (1) therefore grants no permission to write it. Derived
L0 projection writes are gated separately by
:mod:`..agent_memory_guard`; extension writes by the four-step opt-in in (2).
"""

from .base import (
    AdapterRegistry,
    AgentActionResult,
    AgentExtensionAdapter,
    apply_agent_action,
    registry,
    resolve_adapter,
)
from .environment import (
    AUTO,
    GENERIC_ID,
    AgentEnvironmentAdapter,
    EnvironmentAdapterBase,
    EnvironmentRegistry,
    GenericEnvironment,
    configured_adapter_id,
    describe_environment,
    environments,
    generic_environment,
    integration_environment,
    resolve_environment,
)

__all__ = [
    "AUTO",
    "AdapterRegistry",
    "AgentActionResult",
    "AgentEnvironmentAdapter",
    "AgentExtensionAdapter",
    "EnvironmentAdapterBase",
    "EnvironmentRegistry",
    "GENERIC_ID",
    "GenericEnvironment",
    "apply_agent_action",
    "configured_adapter_id",
    "describe_environment",
    "environments",
    "generic_environment",
    "integration_environment",
    "registry",
    "resolve_adapter",
    "resolve_environment",
]

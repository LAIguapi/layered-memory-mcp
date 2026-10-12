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
   Skill / Profile / AgentOther action into a store the agent owns. One
   reference handler ships registered — :mod:`.generic_file`, which appends
   Markdown to a file the operator named — and it writes nothing until the full
   opt-in chain is configured. Nothing is enabled implicitly, because writing
   another system's store on a guess is the one thing the framework must never
   do.

Resolving a path through (1) therefore grants no permission to write it, and
registration in (2) is not permission either. Derived L0 projection writes are
gated separately by :mod:`..agent_memory_guard`; extension writes by the opt-in
chain documented in :mod:`.base`.
"""

from .base import (
    AdapterReadiness,
    AdapterRegistry,
    AgentActionResult,
    AgentExtensionAdapter,
    adapter_readiness,
    apply_agent_action,
    registry,
    resolve_adapter,
    validate_action,
)
from .generic_file import (
    HANDLER_NAME as GENERIC_FILE_HANDLER,
    GenericFileAdapter,
    resolve_target,
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
    "AdapterReadiness",
    "AdapterRegistry",
    "AgentActionResult",
    "AgentEnvironmentAdapter",
    "AgentExtensionAdapter",
    "EnvironmentAdapterBase",
    "EnvironmentRegistry",
    "GENERIC_FILE_HANDLER",
    "GENERIC_ID",
    "GenericEnvironment",
    "GenericFileAdapter",
    "adapter_readiness",
    "apply_agent_action",
    "configured_adapter_id",
    "describe_environment",
    "environments",
    "generic_environment",
    "integration_environment",
    "registry",
    "resolve_adapter",
    "resolve_environment",
    "resolve_target",
    "validate_action",
]

"""Environment adapters for the editor-hosted agents the core used to probe.

``MemoryConfig.detect_agent_memory_path()`` had a hardcoded candidate list:
Hermes, then Claude Code, then Cursor, then Cline, then the framework's own
fallback. The three in the middle are pure file-layout facts, so they need no
code of their own — just the layout, declared.

None of them gets a separator opinion. The pre-adapter core fell back to the
configured ``agent_memory.separator`` for every file whose name did not contain
"memory", and these three are exactly that case. Declaring a separator here
would change how an existing non-Hermes install parses its own memory file, so
:meth:`..environment.EnvironmentAdapterBase.memory_separator` keeps returning
"no opinion" and the configured value keeps winning.
"""

from __future__ import annotations

from .environment import EnvironmentAdapterBase


class ClaudeCodeEnvironment(EnvironmentAdapterBase):
    """Claude Code: ``~/.claude/CLAUDE.md``."""

    id = "claude_code"
    display_name = "Claude Code"
    memory_relpath = (".claude", "CLAUDE.md")
    install_marker_relpath = (".claude",)
    path_tokens = (".claude", "claude.md")


class CursorEnvironment(EnvironmentAdapterBase):
    """Cursor: ``./.cursorrules`` — project-scoped, not home-scoped."""

    id = "cursor"
    display_name = "Cursor"
    cwd_relpath = (".cursorrules",)
    path_tokens = (".cursorrules",)

    def is_present(self, home=None) -> bool:
        """Project-scoped: presence is a question about the working directory."""
        return self.detect_memory_path() is not None


class ClineEnvironment(EnvironmentAdapterBase):
    """Cline: ``~/.cline/rules``."""

    id = "cline"
    display_name = "Cline"
    memory_relpath = (".cline", "rules")
    install_marker_relpath = (".cline",)
    path_tokens = (".cline",)


__all__ = ["ClaudeCodeEnvironment", "ClineEnvironment", "CursorEnvironment"]

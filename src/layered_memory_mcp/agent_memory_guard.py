"""Safety guard for derived writes into the agent's *own* memory file.

WHY THIS MODULE EXISTS
----------------------
``MemoryConfig.detect_agent_memory_path()`` probes absolute, user-level
locations (``~/.hermes/memories/MEMORY.md``, ``~/.claude/CLAUDE.md``, ...) and
deliberately ignores ``LAYERED_MEMORY_HOME`` — in production the L1 store and
the agent's native memory file genuinely are separate things.

The side effect is that *any* ambiguous config picks up a real production
memory file. A throwaway sanity script built on a bare ``MemoryConfig(home=
tmpdir)`` called ``inject_knowledge(dry_run=False)``, the ride-along
``auto_maintain_after_write`` auto-detected ``~/.hermes/memories/MEMORY.md``,
and one ``[L0] misc: ...`` pointer landed in a live agent's memory. Nothing in
that chain was a bug on its own; the hazard is the *silent* promotion of an
auto-detected path to a write target.

THE RULE
--------
Framework writes into agent-native memory require an **explicit target** or an
**explicit opt-in**. That covers the derived / compatibility L0 projection and
the ``integrate_agent(action="inject_memory")`` awareness snippet alike: both
land in a file the framework does not own, chosen by a probe. Auto-detection
may still be used for reads, capability probes and reporting — never for a
silent write from an ambiguous config.

A write target is accepted when any of these holds:

1. the caller passed an explicit ``memory_path`` (it named the file itself);
2. ``agent_memory.path`` / ``LAYERED_MEMORY_AGENT_MEMORY_PATH`` is configured
   *and* resolves to the file about to be written;
3. ``agent_memory.allow_auto_detect_write`` is true (explicit opt-in);
4. the detected path lies inside the framework's own ``home`` (the
   ``<home>/agent-memory.md`` fallback — the framework owns that directory);
5. *legacy compatibility:* ``home`` is this process's real configured home
   (``default_home()``) and is not a temporary / test sandbox.

Rule 5 is what keeps an existing install working: the deployed service resolves
its memory file by auto-detection and has done so since v1.0, so a flat
default-deny would silently switch off its L0 projection on upgrade. It is
narrow on purpose — a temp home, a pytest sandbox, or any home that is not the
configured one fails it, which is exactly the shape of every accidental caller.

Refusals are reported, never silent: callers surface the diagnostic from
:meth:`WriteTarget.as_report` in their existing result dict.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger("layered_memory_mcp.agent_memory_guard")

#: Reason code for a refused auto-detected target (stable, assert-able).
UNSAFE_AUTO_DETECT = "unsafe_auto_detect_write"

#: Reason code for "nothing to write to" — not a safety refusal.
UNRESOLVED = "agent_memory_path_unresolved"

HINT = (
    "Set agent_memory.path (or LAYERED_MEMORY_AGENT_MEMORY_PATH) to the memory "
    "file this install owns, or agent_memory.allow_auto_detect_write: true to "
    "keep the legacy auto-detect projection."
)


class WriteTarget(NamedTuple):
    """Outcome of resolving an agent-memory *write* target.

    ``path`` is only safe to write when ``allowed`` is true. ``trust`` records
    which rule decided it, so a refusal (and an allowance) can be explained in
    a result dict instead of being guessed at from behaviour.
    """

    path: Path | None
    allowed: bool
    trust: str
    reason: str = ""

    def skip_fields(self) -> dict:
        """Diagnostic fields to merge into an existing ``{action: skipped}`` report."""
        fields = {
            "reason": self.reason or self.trust,
            "detected_path": str(self.path) if self.path else None,
            "trust": self.trust,
        }
        if self.reason == UNSAFE_AUTO_DETECT:
            fields["hint"] = HINT
        return fields

    def as_report(self, status: str = "skipped") -> dict:
        """Standalone structured diagnostic (``result["agent_memory_write"]``)."""
        return {"status": status, **self.skip_fields()}


def guard_refusal(maintenance_report: dict | None) -> dict | None:
    """Pull a guard refusal out of an ``auto_maintain_after_write`` report.

    Returns the structured diagnostic when this maintenance pass declined to
    project into agent memory, else ``None``. Lets a caller hoist the refusal to
    the top level of its own result without hard-coding where it is nested.
    """
    if not isinstance(maintenance_report, dict):
        return None
    for key in ("agent_memory_write", "dual_write"):
        block = maintenance_report.get(key)
        if isinstance(block, dict) and block.get("reason") == UNSAFE_AUTO_DETECT:
            return {"status": "skipped", **{k: v for k, v in block.items() if k != "action"}}
    return None


def _resolved(value) -> Path:
    """``Path`` with ``~`` expanded and symlinks resolved; never raises."""
    path = Path(value).expanduser()
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):  # pragma: no cover - exotic FS
        return path


def _is_within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or lives underneath it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _running_under_test() -> bool:
    """True when this process is a test run.

    Belt and braces: ``PYTEST_CURRENT_TEST`` covers test execution, the
    ``sys.modules`` probe also covers collection and fixture set-up. Nothing in
    the shipped server imports pytest, so this cannot fire in production.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def is_ephemeral_home(home) -> bool:
    """True when ``home`` is a throwaway location rather than a real install.

    Matches the system temp directory (honouring ``TMPDIR``) and pytest's
    ``tmp_path`` roots, which are named ``pytest-of-<user>/pytest-<n>/...``.
    """
    resolved = _resolved(home)
    if any(part.startswith("pytest-") for part in resolved.parts):
        return True
    try:
        tmp_root = _resolved(tempfile.gettempdir())
    except Exception:  # noqa: BLE001 - gettempdir probes the filesystem
        return False
    return _is_within(resolved, tmp_root)


def _is_configured_home(config: "MemoryConfig") -> bool:
    """True when ``config.home`` is this process's real configured home.

    "Real" means it equals :func:`config.default_home` (so it came from
    ``LAYERED_MEMORY_HOME`` or the ``~/.layered-memory`` default rather than
    from an ad-hoc constructor argument) *and* is not a temp/test sandbox.
    """
    home = getattr(config, "home", None)
    if not home:
        return False
    if _running_under_test() or is_ephemeral_home(home):
        return False
    try:
        from .config import default_home

        return _resolved(home) == _resolved(default_home())
    except Exception as e:  # noqa: BLE001 - a bad env var must not grant trust
        logger.debug("could not resolve default home (%s); treating as untrusted", e)
        return False


def _detect(config) -> Path | None:
    """``config.detect_agent_memory_path()``, tolerant of duck-typed configs."""
    detect = getattr(config, "detect_agent_memory_path", None)
    if detect is None:
        return None
    try:
        detected = detect()
    except Exception as e:  # noqa: BLE001 - detection must never break a write
        logger.warning("agent memory detection failed: %s", e)
        return None
    return Path(detected) if detected else None


def resolve_write_target(
    config: "MemoryConfig | None",
    memory_path: str | Path | None = None,
    *,
    operation: str = "agent_memory_write",
    detected: str | Path | None = None,
) -> WriteTarget:
    """Resolve where a derived L0 projection may be written — or refuse.

    Mirrors ``memory_compactor._resolve_memory_path`` for the *write* case; see
    the module docstring for the five acceptance rules. Read-only callers
    (bloat detection, capability reporting) keep using the plain resolver.

    ``detected`` is for a caller that has *already* auto-detected a candidate by
    another route (``agent_integrator.detect_agent_type()["memory_path"]``) and
    needs that exact file judged — detection by a different code path is still
    detection, so it buys no permission. Unlike ``memory_path`` it is not the
    caller naming a target; rule 2 then only applies when the configured path
    *is* that file, so a configured target cannot license a write to a
    different one.

    ``operation`` only labels the refusal log line.
    """
    # 1. The caller named the file — that is as explicit as it gets.
    if memory_path:
        return WriteTarget(Path(memory_path), True, "explicit_argument")

    if config is None:
        return WriteTarget(None, False, "no_config", UNRESOLVED)

    candidate = Path(detected) if detected else _detect(config)
    if candidate is None:
        return WriteTarget(None, False, "unresolved", UNRESOLVED)

    # 2. Explicitly configured path (agent_memory.path / env var) — and it is
    #    this candidate, not merely some configured file.
    configured = getattr(config, "agent_memory_path", None)
    if configured and _resolved(configured) == _resolved(candidate):
        return WriteTarget(candidate, True, "explicit_config")

    # 3. Explicit opt-in to the legacy auto-detect projection.
    if getattr(config, "agent_memory_allow_auto_detect_write", False):
        return WriteTarget(candidate, True, "allow_auto_detect_write")

    # 4. Inside the framework's own home — the generic fallback file.
    home = getattr(config, "home", None)
    if home and _is_within(_resolved(candidate), _resolved(home)):
        return WriteTarget(candidate, True, "inside_framework_home")

    # 5. Legacy compatibility for a real install (see module docstring).
    if _is_configured_home(config):
        return WriteTarget(candidate, True, "configured_home")

    logger.warning(
        "%s refused: auto-detected agent memory %s is outside the framework "
        "home %s and no explicit target/opt-in is configured. %s",
        operation,
        candidate,
        home,
        HINT,
    )
    return WriteTarget(candidate, False, "auto_detect_outside_home", UNSAFE_AUTO_DETECT)

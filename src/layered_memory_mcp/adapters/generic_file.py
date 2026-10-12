"""Reference extension-action handler: append Markdown to an explicit file.

WHY THIS EXISTS
---------------
``apply_agent_action`` needs at least one real implementation, or the contract
is only a docstring. This is that implementation — and deliberately the most
boring one possible: it appends a bounded Markdown section to a file the
operator named, and does nothing else. No shell command, no fuzzy edit of
existing text, no knowledge of any agent product's layout.

It is also the handler most people actually want: "put the skills you recognise
into *this* file and let me read them myself" is a complete workflow.

CONFIG
------
::

    agent_extensions:
      enabled: true
      mode: apply_with_confirmation      # suggest | apply_with_confirmation | apply
      adapter: generic_file              # label only; the handler below is what runs
      root: /path/to/extensions          # optional base for relative paths
      surfaces:
        skill:
          enabled: true
          handler: generic_file
          path: /path/to/extensions/skills.md

``path`` may be a file or a directory; a directory (or a path ending in a
separator) gets ``<surface>.md`` inside it. A *relative* ``path`` is resolved
against ``root`` when set, else against the framework's own ``home`` — and must
stay inside that base. With no ``path`` and no ``root`` the handler refuses:
there is no default target, because an implicit target is exactly the hazard the
opt-in chain exists to prevent.

WHAT IT WRITES
--------------
One ``## `` section per applied action, appended to the end of the file, headed
by an HTML marker comment so the sections are machine-findable. The body is the
caller's own ``source_content`` (truncated at :data:`MAX_SECTION_CHARS` with an
explicit marker) under a short bounded metadata block. The append is taken under
the same ``<path>.lock`` ``FileLock`` the L1 write path uses, so two concurrent
applies to one file cannot interleave their sections.

OPERATION SEMANTICS (simple on purpose)
---------------------------------------
``create``, ``append`` and ``update`` all **append a new section**. ``update``
additionally records whether a section with the same heading already existed
(``supersedes``), so a reader can see the lineage. The handler never rewrites or
deletes existing text: a fuzzy in-place edit of somebody else's file is the one
behaviour that could destroy content it does not own.

It does not write ``MEMORY_CHANGELOG``. That log is provenance for
framework-owned L0/L1 mutations; these writes are the agent's.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from filelock import FileLock

from ..routing import EXTENSION_SURFACES, AgentAction, MemorySurface
from .base import (
    STATUS_APPLIED,
    STATUS_WOULD_APPLY,
    AdapterReadiness,
    AgentActionResult,
    registry,
)

logger = logging.getLogger("layered_memory_mcp.adapters.generic_file")

#: Handler name an operator puts in ``surfaces.<surface>.handler``.
HANDLER_NAME = "generic_file"

#: Marker comment opening every section this handler writes.
SECTION_MARKER = "<!-- layered-memory:agent-action -->"

#: Hard ceilings, so one action cannot turn into an unbounded file append.
MAX_SECTION_CHARS = 8_000
MAX_RATIONALE_CHARS = 400
MAX_TITLE_CHARS = 120
MAX_SHAPE_HINTS = 12
MAX_METADATA_ITEMS = 8

# --- blocker codes this handler can report -----------------------------------

BLOCK_NO_CONFIGURED_PATH = "no_configured_path"
BLOCK_PATH_ESCAPES_BASE = "path_escapes_base"
BLOCK_UNSAFE_SYSTEM_TARGET = "unsafe_system_target"
BLOCK_UNSAFE_FILE_TYPE = "unsafe_file_type"
BLOCK_UNRESOLVABLE_PATH = "unresolvable_path"
BLOCK_UNSUPPORTED_SURFACE = "unsupported_surface"

#: Directory prefixes this handler will never write into, however explicitly it
#: is pointed at them. Operating-system territory, not user content — appending
#: Markdown there is never what anybody meant.
UNSAFE_ROOTS: tuple[str, ...] = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc",
    "/run", "/sbin", "/sys", "/usr", "/var/lib", "/var/log", "/var/run",
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
)

#: Suffixes this handler refuses: appending a Markdown section to code, config,
#: a database or a credential file corrupts it. Absence of a suffix is fine.
UNSAFE_SUFFIXES: frozenset[str] = frozenset({
    ".py", ".pyc", ".pyo", ".js", ".ts", ".rb", ".go", ".rs", ".java", ".c",
    ".h", ".cpp", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".db", ".sqlite", ".sqlite3", ".jsonl", ".csv", ".parquet",
    ".env", ".pem", ".key", ".crt", ".p12", ".pfx", ".so", ".dll", ".exe",
})

#: Suffixes known to be plain prose. Anything else with a suffix is refused.
SAFE_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".mdown", ".txt"})


class Target(NamedTuple):
    """Where an action would be written, or why it would not be."""

    path: Path | None
    blockers: tuple[str, ...] = ()
    detail: str = ""
    #: Which rule produced ``path`` — reported so an allowance is explainable.
    trust: str = ""


def _expand(value: str | os.PathLike[str]) -> Path:
    """``Path`` with ``~`` expanded; never resolved against the filesystem."""
    return Path(os.path.expanduser(str(value)))


def _normalise(path: Path) -> Path:
    """Absolute, ``..``-collapsed path that does not require the file to exist."""
    try:
        return Path(os.path.normpath(os.path.abspath(str(path))))
    except (OSError, ValueError):  # pragma: no cover — exotic filesystem
        return path


def _is_within(path: Path, base: Path) -> bool:
    try:
        _normalise(path).relative_to(_normalise(base))
    except ValueError:
        return False
    return True


def _looks_like_directory(raw: str, path: Path) -> bool:
    return raw.endswith(("/", os.sep)) or path.is_dir()


def _settings(config: Any | None) -> dict[str, Any]:
    from ..routing import agent_extension_settings

    return agent_extension_settings(config)


def resolve_target(
    surface: MemorySurface, config: Any | None
) -> Target:
    """Resolve the file this handler would write for ``surface`` — or refuse.

    Pure: touches the filesystem only to ask whether a configured path is an
    existing directory. Every refusal carries a stable blocker code.
    """
    surface = MemorySurface(surface)
    if surface not in EXTENSION_SURFACES:
        return Target(
            None,
            (BLOCK_UNSUPPORTED_SURFACE,),
            f"{surface.value} is not an agent-owned extension surface",
        )

    settings = _settings(config)
    entry = settings["surfaces"].get(surface.value, {})
    raw_path = entry.get("path")
    raw_root = settings.get("root")
    home = getattr(config, "home", None)

    root = _expand(raw_root) if raw_root else None
    if root is not None and not root.is_absolute():
        if home is None:
            return Target(
                None,
                (BLOCK_UNRESOLVABLE_PATH,),
                "agent_extensions.root is relative and no framework home is "
                "configured to resolve it against",
            )
        root = _expand(home) / root

    if not raw_path:
        if root is None:
            return Target(
                None,
                (BLOCK_NO_CONFIGURED_PATH,),
                (
                    f"no explicit target for surface {surface.value}: set "
                    f"agent_extensions.surfaces.{surface.value}.path (or "
                    "agent_extensions.root). This handler has no default "
                    "location on purpose"
                ),
            )
        candidate = root / f"{surface.value}.md"
        trust = "surface_file_under_configured_root"
        base: Path | None = root
    else:
        candidate = _expand(raw_path)
        if candidate.is_absolute():
            # Naming an absolute file *is* the explicitness this gate wants, so
            # no containment base applies — only the system-directory and
            # file-type refusals below.
            trust = "explicit_absolute_path"
            base = None
        else:
            base = root if root is not None else (_expand(home) if home else None)
            if base is None:
                return Target(
                    None,
                    (BLOCK_UNRESOLVABLE_PATH,),
                    (
                        f"path {raw_path!r} is relative and there is no "
                        "agent_extensions.root or framework home to resolve it "
                        "against"
                    ),
                )
            candidate = base / candidate
            trust = (
                "relative_under_configured_root"
                if root is not None
                else "relative_under_framework_home"
            )
        if _looks_like_directory(str(raw_path), candidate):
            candidate = candidate / f"{surface.value}.md"

    candidate = _normalise(candidate)

    # Only relative and root-derived paths get a containment base, so only they
    # are confined here. An absolute ``path`` deliberately leaves ``base`` as
    # None above: naming an absolute file *is* the explicitness this gate
    # requires, and it is still checked by the system-directory and file-type
    # rules below — a configured ``root`` does not confine it.
    if base is not None and not _is_within(candidate, base):
        return Target(
            None,
            (BLOCK_PATH_ESCAPES_BASE,),
            (
                f"{candidate} resolves outside its configured base {base} — "
                "'..' traversal out of the extension root is refused"
            ),
        )

    if candidate.parent == candidate or not candidate.name:
        return Target(
            None, (BLOCK_UNSAFE_SYSTEM_TARGET,), f"{candidate} is not a file path"
        )
    text = str(candidate)
    for bad in UNSAFE_ROOTS:
        if text == bad or text.startswith(bad.rstrip("/\\") + os.sep) or text.startswith(bad + "/"):
            return Target(
                None,
                (BLOCK_UNSAFE_SYSTEM_TARGET,),
                f"{candidate} is inside the system directory {bad} — refused",
            )

    suffix = candidate.suffix.lower()
    # A dotfile (``.env``, ``.cursorrules``) reports no suffix at all, so it has
    # to be judged by its name: hidden files are configuration, not prose docs.
    hidden_non_prose = candidate.name.startswith(".") and suffix not in SAFE_SUFFIXES
    if hidden_non_prose or (
        suffix and (suffix in UNSAFE_SUFFIXES or suffix not in SAFE_SUFFIXES)
    ):
        return Target(
            None,
            (BLOCK_UNSAFE_FILE_TYPE,),
            (
                f"{candidate} is not a prose file (suffix {suffix or 'none'}, "
                f"name {candidate.name!r}); this handler appends Markdown and "
                "would corrupt it. Use "
                + "/".join(sorted(SAFE_SUFFIXES))
                + " or a plain name with no suffix"
            ),
        )

    return Target(candidate, (), "", trust)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + f"\n\n[truncated at {limit} chars]", True


def _heading(action: AgentAction) -> str:
    """A bounded, single-line heading for ``action``."""
    title = (action.title or "").strip().replace("\n", " ")
    if not title:
        first = next(
            (
                line.strip().lstrip("#").strip()
                for line in (action.source_content or "").splitlines()
                if line.strip()
            ),
            "",
        )
        title = first or f"{MemorySurface(action.surface).value} entry"
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return title


def _metadata_lines(action: AgentAction, extra: dict[str, Any]) -> list[str]:
    """Readable, bounded metadata for the section header."""
    surface = MemorySurface(action.surface).value
    lines = [
        f"- surface: {surface}",
        f"- operation: {action.operation}",
        f"- handler: {HANDLER_NAME}",
        f"- applied_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
    ]
    for key, value in extra.items():
        lines.append(f"- {key}: {value}")
    rationale, _ = _truncate(action.rationale, MAX_RATIONALE_CHARS)
    if rationale:
        lines.append(f"- rationale: {rationale.replace(chr(10), ' ')}")
    hints = [str(h).strip() for h in (action.shape_hint or []) if str(h).strip()]
    if hints:
        lines.append("- shape_hint: " + ", ".join(hints[:MAX_SHAPE_HINTS]))
    meta = {
        k: v
        for k, v in (action.metadata or {}).items()
        if v is not None and not isinstance(v, (dict, list, tuple))
    }
    if meta:
        items = list(meta.items())[:MAX_METADATA_ITEMS]
        lines.append("- metadata: " + "; ".join(f"{k}={v}" for k, v in items))
    return lines


def render_section(action: AgentAction, extra: dict[str, Any] | None = None) -> str:
    """The exact Markdown this handler would append for ``action``."""
    heading = _heading(action)
    body, truncated = _truncate(action.source_content, MAX_SECTION_CHARS)
    extras = dict(extra or {})
    if truncated:
        extras["truncated"] = True
    lines = [
        SECTION_MARKER,
        f"## {heading}",
        "",
        *_metadata_lines(action, extras),
        "",
        body,
        "",
    ]
    return "\n".join(lines)


def _file_header(surface: MemorySurface) -> str:
    return (
        f"# Agent extension surface: {MemorySurface(surface).value}\n\n"
        "Appended by layered-memory `apply_agent_action` through the "
        f"`{HANDLER_NAME}` handler. This surface is **agent-owned**: the "
        "framework writes here only because it was explicitly configured to, "
        "and never edits or removes an existing section.\n"
    )


class GenericFileAdapter:
    """Append an agent action as a Markdown section to an explicit file.

    Registered under :data:`HANDLER_NAME` at import time. Registration is not
    permission: it writes nothing until ``agent_extensions`` is enabled, the
    surface is enabled, this handler is named for it, the mode allows applying
    and an explicit target path resolves safely.
    """

    name = HANDLER_NAME

    def supports(self, surface: MemorySurface) -> bool:
        try:
            return MemorySurface(surface) in EXTENSION_SURFACES
        except ValueError:
            return False

    def readiness(
        self, surface: MemorySurface, config: Any | None
    ) -> AdapterReadiness:
        """Whether a safe target resolves for ``surface`` right now."""
        target = resolve_target(surface, config)
        if target.path is None:
            return AdapterReadiness(False, target.blockers, None, target.detail)
        return AdapterReadiness(True, (), str(target.path), target.trust)

    def apply(
        self,
        action: AgentAction,
        config: Any | None = None,
        *,
        dry_run: bool = False,
    ) -> AgentActionResult:
        """Append ``action`` to its configured file, or report what it would do.

        The caller (:func:`..base.apply_agent_action`) has already checked the
        opt-in chain and confirmation; this method re-resolves the target
        because resolution is where the path safety lives, and a handler must
        not depend on having been called correctly.
        """
        surface = MemorySurface(action.surface)
        target = resolve_target(surface, config)
        if target.path is None:
            return AgentActionResult(
                status="refused",
                surface=surface,
                operation=action.operation,
                handler=self.name,
                blockers=list(target.blockers),
                detail=target.detail,
            )

        existed = target.path.exists()
        extra: dict[str, Any] = {"trust": target.trust}
        if action.operation == "update":
            heading = f"## {_heading(action)}"
            previous = False
            if existed:
                try:
                    previous = heading in target.path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.debug("could not read %s: %s", target.path, exc)
            # No fuzzy edit: an update appends a superseding section and says so.
            extra["supersedes"] = heading[3:] if previous else "nothing (new section)"

        section = render_section(action, extra)
        preview = {
            "target": str(target.path),
            "file_exists": existed,
            "bytes_to_append": len(section.encode("utf-8")),
            "creates_parent_dirs": not target.path.parent.exists(),
            "section_heading": _heading(action),
        }

        if dry_run:
            return AgentActionResult(
                status=STATUS_WOULD_APPLY,
                surface=surface,
                operation=action.operation,
                handler=self.name,
                target=str(target.path),
                dry_run=True,
                detail=(
                    f"would append a {preview['bytes_to_append']}-byte section to "
                    f"{target.path}"
                ),
                metadata=preview,
            )

        # Parent directories are created only for a path the operator named.
        target.path.parent.mkdir(parents=True, exist_ok=True)
        payload = ("" if existed else _file_header(surface)) + "\n" + section
        # Same lock convention as the L1 write path, so two concurrent applies
        # to one file cannot interleave their sections.
        lock = FileLock(str(target.path) + ".lock", timeout=10)
        with lock, target.path.open("a", encoding="utf-8") as handle:
            handle.write(payload)

        return AgentActionResult(
            applied=True,
            status=STATUS_APPLIED,
            surface=surface,
            operation=action.operation,
            handler=self.name,
            target=str(target.path),
            detail=f"appended a Markdown section to {target.path}",
            metadata={**preview, "created_file": not existed},
        )


#: The shipped instance. Named in config as ``handler: generic_file``.
adapter = GenericFileAdapter()
registry.register(adapter)


__all__ = [
    "BLOCK_NO_CONFIGURED_PATH",
    "BLOCK_PATH_ESCAPES_BASE",
    "BLOCK_UNRESOLVABLE_PATH",
    "BLOCK_UNSAFE_FILE_TYPE",
    "BLOCK_UNSAFE_SYSTEM_TARGET",
    "BLOCK_UNSUPPORTED_SURFACE",
    "GenericFileAdapter",
    "HANDLER_NAME",
    "MAX_SECTION_CHARS",
    "SAFE_SUFFIXES",
    "SECTION_MARKER",
    "Target",
    "UNSAFE_ROOTS",
    "UNSAFE_SUFFIXES",
    "adapter",
    "render_section",
    "resolve_target",
]

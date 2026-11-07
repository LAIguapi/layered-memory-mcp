"""Prepare and prove the service home's binding to one activated generation.

WHY THIS MODULE EXISTS
----------------------
Publishing a generation and activating a pointer changes two files that the
*service* never reads. A running MCP or HTTP server builds
:class:`~layered_memory_mcp.config.MemoryConfig` from ``<home>/config.yaml``
and the environment, so until that file names the pointer, an "activated"
generation is something only the operator's CLI can see. Reporting activation
as operator success would make the success conditional on a manual edit nobody
recorded — which is the one failure mode where the tool says yes and the
service still writes to the legacy store.

WHAT IT PROMISES, AND WHAT IT DOES NOT
--------------------------------------
Three separately reported steps, deliberately not one transaction:

``plan_home_binding``
    Reads. Decides whether the home already selects this pointer, and refuses
    anything it cannot rewrite safely. Creates and changes nothing at all —
    not the config file, not the home's mtime, not a lock.
``apply_home_binding``
    Rewrites only the ``graph:`` block of the selected YAML, byte-for-byte
    preserving every other line, after keeping a recoverable original. The
    replacement is validated *as parsed YAML* before it is written, so a
    textual edit that would have lost or invented a key is refused instead.
``verify_effective_binding``
    Builds the real configuration the service would build for that home and
    checks it resolves this exact pointer, the authoritative mode and the
    generation's own projections. An environment override is a refusal, not a
    footnote: if ``LAYERED_MEMORY_GRAPH_*`` is set, the YAML is not what
    decides, and this step cannot prove what the service will do.

There is no hidden fallback anywhere: an unparsable file, a duplicated key, an
unknown ``graph:`` key, a layout this module cannot edit textually, or a
conflicting explicit target all stop the run with the home untouched.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

CONFIG_NAME = "config.yaml"
BACKUP_SUFFIX = ".pre-graph-binding"
GRAPH_SECTION = "graph"
AUTHORITATIVE_MODE = "graph"

#: Keys this module understands inside the ``graph:`` section. Anything else is
#: refused rather than preserved-and-hoped-for: this step rewrites the section,
#: and rewriting a key whose meaning is unknown is how a setting gets lost.
KNOWN_GRAPH_KEYS = ("mode", "db_path", "journal_path", "active_pointer")

#: Environment variables that outrank ``<home>/config.yaml`` for the fields this
#: binding owns. Their presence makes the effective mode unprovable from YAML.
OVERRIDE_ENVIRONMENT = (
    "LAYERED_MEMORY_GRAPH_MODE",
    "LAYERED_MEMORY_GRAPH_ACTIVE_POINTER",
    "LAYERED_MEMORY_GRAPH_DB_PATH",
    "LAYERED_MEMORY_GRAPH_JOURNAL_PATH",
    "LAYERED_MEMORY_L0_INDEX_FILE",
)

STATUS_PREPARED = "home_binding_prepared"
STATUS_UNCHANGED = "home_binding_unchanged"
STATUS_APPLIED = "home_binding_applied"
STATUS_VERIFIED = "home_binding_verified"
STATUS_REFUSED = "home_binding_refused"

FINDING_HOME_ABSENT = "home_absent"
FINDING_HOME_NOT_A_DIRECTORY = "home_not_a_directory"
FINDING_HOME_SYMLINK = "home_symlink"
FINDING_CONFIG_SYMLINK = "config_symlink"
FINDING_CONFIG_NOT_A_FILE = "config_not_a_file"
FINDING_CONFIG_UNREADABLE = "config_unreadable"
FINDING_CONFIG_NOT_A_MAPPING = "config_not_a_mapping"
FINDING_CONFIG_UNSUPPORTED_LAYOUT = "config_unsupported_layout"
FINDING_CONFIG_DUPLICATE_KEY = "config_duplicate_key"
FINDING_GRAPH_NOT_A_MAPPING = "graph_section_not_a_mapping"
FINDING_GRAPH_UNKNOWN_KEY = "graph_section_unknown_key"
FINDING_GRAPH_CONFLICTING_TARGET = "graph_section_conflicting_target"
FINDING_POINTER_UNRESOLVED = "pointer_unresolved"
FINDING_POINTER_PATH = "pointer_path_rejected"
FINDING_REWRITE_WOULD_CHANGE = "rewrite_would_change_unrelated_settings"
FINDING_BACKUP_EXISTS = "recoverable_original_exists"
FINDING_WRITE_FAILED = "config_write_failed"
FINDING_ENVIRONMENT_OVERRIDE = "environment_override"
FINDING_EFFECTIVE_MODE = "effective_mode_mismatch"
FINDING_EFFECTIVE_POINTER = "effective_pointer_mismatch"
FINDING_EFFECTIVE_PROJECTION = "effective_projection_mismatch"
FINDING_EFFECTIVE_UNUSABLE = "effective_config_rejected"

_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(\s|$)")
_INDENTED = re.compile(r"^[ ]+\S")
_COMMENT_OR_BLANK = re.compile(r"^\s*(#.*)?$")


@dataclass
class BindingReport:
    """One step's outcome. ``writes`` is what actually happened, not intent."""

    status: str
    home: str
    pointer: str
    config_path: str
    generation: str | None = None
    changes: list[str] = field(default_factory=list)
    retained_keys: list[str] = field(default_factory=list)
    backup: str | None = None
    writes: bool = False
    findings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    effective: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "home": self.home,
            "pointer": self.pointer,
            "config_path": self.config_path,
            "generation": self.generation,
            "changes": sorted(set(self.changes)),
            "retained_keys": sorted(set(self.retained_keys)),
            "backup": self.backup,
            "writes": self.writes,
            "findings": sorted(set(self.findings)),
            "warnings": sorted(set(self.warnings)),
            "effective": dict(self.effective),
            "ok": self.ok,
        }


class _DuplicateKeyLoader(yaml.SafeLoader):
    """``yaml.safe_load`` keeps the *last* duplicate; this refuses instead.

    A config holding ``graph:`` twice parses cleanly and silently discards one
    of them. Rewriting such a file would publish whichever copy the parser
    happened to keep, so it is a refusal.
    """


def _no_duplicates(loader, node, deep=False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates
)


@dataclass
class _Config:
    path: Path
    text: str
    data: dict[str, Any]
    exists: bool


def _absolute(value: Any) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _read_config(home: Path) -> tuple[_Config | None, list[str]]:
    """Read ``<home>/config.yaml`` without creating it. Opens nothing else."""
    path = home / CONFIG_NAME
    if path.is_symlink():
        return None, [FINDING_CONFIG_SYMLINK]
    if not path.exists():
        return _Config(path=path, text="", data={}, exists=False), []
    if not path.is_file():
        return None, [FINDING_CONFIG_NOT_A_FILE]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, [FINDING_CONFIG_UNREADABLE]
    try:
        data = yaml.load(text, Loader=_DuplicateKeyLoader)
    except yaml.YAMLError as exc:
        finding = (
            FINDING_CONFIG_DUPLICATE_KEY
            if "duplicate key" in str(exc)
            else FINDING_CONFIG_UNREADABLE
        )
        return None, [finding]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, [FINDING_CONFIG_NOT_A_MAPPING]
    return _Config(path=path, text=text, data=data, exists=True), []


def _top_level_spans(text: str) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Map each top-level key to its ``[start, end)`` line span.

    Only the simple block-mapping layout this module can edit textually is
    accepted. A document marker, a top-level sequence, a flow mapping or a tab
    makes the span of a key ambiguous, so the layout is refused rather than
    guessed at — the alternative is an edit that silently relocates settings.
    """
    lines = text.splitlines()
    spans: dict[str, tuple[int, int]] = {}
    order: list[str] = []
    for index, line in enumerate(lines):
        if "\t" in line:
            return {}, [f"{FINDING_CONFIG_UNSUPPORTED_LAYOUT}:tab"]
        if _COMMENT_OR_BLANK.match(line) or _INDENTED.match(line):
            continue
        match = _TOP_LEVEL_KEY.match(line)
        if match is None:
            return {}, [f"{FINDING_CONFIG_UNSUPPORTED_LAYOUT}:line_{index + 1}"]
        key = match.group(1)
        if key in spans:
            return {}, [f"{FINDING_CONFIG_DUPLICATE_KEY}:{key}"]
        spans[key] = (index, len(lines))
        if order:
            spans[order[-1]] = (spans[order[-1]][0], index)
        order.append(key)
    # Trailing blank lines and comments belong to whatever comes next — usually
    # the operator's note about the following key. Leaving them inside a span
    # would delete them the first time that section is rewritten.
    for key, (start, end) in spans.items():
        while end - 1 > start and _COMMENT_OR_BLANK.match(lines[end - 1]):
            end -= 1
        spans[key] = (start, end)
    return spans, []


def _render_section(section: Mapping[str, Any]) -> list[str]:
    body = yaml.safe_dump(
        {GRAPH_SECTION: dict(section)}, default_flow_style=False, sort_keys=False,
        allow_unicode=True, width=10**6,
    )
    return body.rstrip("\n").splitlines()


def _resolve_pointer(pointer: Path) -> tuple[Any, list[str]]:
    """Resolve the activated generation the pointer names. Reads only."""
    from .graph_generation_runtime import resolve_generation_runtime

    if pointer.is_symlink():
        return None, [f"{FINDING_POINTER_PATH}:symlink"]
    runtime = resolve_generation_runtime(
        pointer=pointer, graph_mode=AUTHORITATIVE_MODE
    )
    if not runtime.usable or runtime.config is None:
        return None, [
            f"{FINDING_POINTER_UNRESOLVED}:{runtime.report.status}",
            *runtime.report.findings,
        ]
    return runtime.config, []


@dataclass
class _Preparation:
    config: _Config
    section: dict[str, Any]
    text: str
    changes: list[str]
    retained: list[str]
    generation: Path


def _inspect_home(
    home: Path,
) -> tuple[_Config | None, dict[str, Any], list[str]]:
    """Everything about the home that does not depend on the pointer.

    Separated so a preflight can report an unrewritable home *before* the
    pointer exists: on a first cutover the generation is not activated yet, and
    "the pointer does not resolve" must not hide "this config.yaml holds two
    graph sections".
    """
    if not home.exists():
        return None, {}, [FINDING_HOME_ABSENT]
    if home.is_symlink():
        return None, {}, [FINDING_HOME_SYMLINK]
    if not home.is_dir():
        return None, {}, [FINDING_HOME_NOT_A_DIRECTORY]

    config, config_findings = _read_config(home)
    if config is None:
        return None, {}, config_findings

    declared = config.data.get(GRAPH_SECTION, {})
    if declared is None:
        declared = {}
    if not isinstance(declared, dict):
        return None, {}, [FINDING_GRAPH_NOT_A_MAPPING]

    findings = [
        f"{FINDING_GRAPH_UNKNOWN_KEY}:{key}"
        for key in sorted(map(str, declared))
        if str(key) not in KNOWN_GRAPH_KEYS
    ]
    _, layout_findings = _top_level_spans(config.text)
    findings.extend(layout_findings)
    if findings:
        return None, {}, findings
    return config, declared, []


def plan_home_config(*, home: Any) -> BindingReport:
    """Report whether this home's ``config.yaml`` can be bound at all.

    Pointer-independent, read-only. Used as a preflight before a first
    migration, where refusing on a not-yet-activated pointer would be wrong but
    refusing on a config this step could never rewrite is exactly right.
    """
    home_path = _absolute(home)
    config, declared, findings = _inspect_home(home_path)
    report = _report(
        status=STATUS_REFUSED if findings else STATUS_PREPARED,
        home=home_path, pointer="", findings=findings,
    )
    if config is not None:
        report.retained_keys = [
            key for key in KNOWN_GRAPH_KEYS if key in declared
        ]
        report.warnings = [] if config.exists else ["config_absent"]
    return report


def _prepare(home: Path, pointer: Path) -> tuple[_Preparation | None, list[str]]:
    config, declared, findings = _inspect_home(home)
    if config is None:
        return None, findings

    bound, pointer_findings = _resolve_pointer(pointer)
    if bound is None:
        return None, pointer_findings

    findings = []
    # A declared database or journal target that is not this generation's would
    # make the complete config refuse at construction. Report it here, where the
    # operator can still fix it, instead of after the pointer has moved.
    expected_targets = {
        "db_path": bound.graph_db_path,
        "journal_path": bound.graph_journal_path,
    }
    retained: list[str] = []
    for key, expected in expected_targets.items():
        if key not in declared:
            continue
        value = declared[key]
        if not isinstance(value, str) or _absolute(value) != _absolute(expected):
            findings.append(f"{FINDING_GRAPH_CONFLICTING_TARGET}:{key}")
        else:
            retained.append(key)
    if findings:
        return None, findings

    section = {key: declared[key] for key in KNOWN_GRAPH_KEYS if key in declared}
    changes = []
    if section.get("mode") != AUTHORITATIVE_MODE:
        changes.append("mode")
    declared_pointer = section.get("active_pointer")
    if (
        not isinstance(declared_pointer, str)
        or _absolute(declared_pointer) != pointer
    ):
        changes.append("active_pointer")
    section["mode"] = AUTHORITATIVE_MODE
    section["active_pointer"] = str(pointer)
    # Keep the operator's own ordering: the two keys this step owns are written
    # in the canonical order only when they were not already present.
    ordered = {key: section[key] for key in KNOWN_GRAPH_KEYS if key in section}

    spans, _ = _top_level_spans(config.text)
    lines = config.text.splitlines()
    rendered = _render_section(ordered)
    if GRAPH_SECTION in spans:
        start, end = spans[GRAPH_SECTION]
        updated = lines[:start] + rendered + lines[end:]
    else:
        updated = lines + rendered
    text = "\n".join(updated) + "\n"

    # The textual edit is only accepted once the *parsed* result proves that
    # nothing but the graph section moved. This is what makes "retains all
    # unrelated configuration" a checked claim rather than a careful intention.
    try:
        reparsed = yaml.load(text, Loader=_DuplicateKeyLoader) or {}
    except yaml.YAMLError:
        return None, [FINDING_REWRITE_WOULD_CHANGE]
    expected_data = {**config.data, GRAPH_SECTION: ordered}
    if not isinstance(reparsed, dict) or reparsed != expected_data:
        return None, [FINDING_REWRITE_WOULD_CHANGE]

    return (
        _Preparation(
            config=config, section=ordered, text=text, changes=changes,
            retained=retained, generation=Path(bound.root),
        ),
        [],
    )


def _report(
    *, status: str, home: Path, pointer: Path, findings=(), **extra: Any
) -> BindingReport:
    return BindingReport(
        status=status, home=str(home), pointer=str(pointer),
        config_path=str(home / CONFIG_NAME), findings=list(findings), **extra,
    )


def plan_home_binding(*, home: Any, pointer: Any) -> BindingReport:
    """Decide the binding without touching the home. Zero writes, always."""
    home_path = _absolute(home)
    pointer_path = _absolute(pointer)
    preparation, findings = _prepare(home_path, pointer_path)
    if preparation is None:
        return _report(
            status=STATUS_REFUSED, home=home_path, pointer=pointer_path,
            findings=findings,
        )
    return _report(
        status=STATUS_UNCHANGED if not preparation.changes else STATUS_PREPARED,
        home=home_path, pointer=pointer_path,
        generation=str(preparation.generation),
        changes=preparation.changes, retained_keys=preparation.retained,
        writes=False,
    )


def apply_home_binding(*, home: Any, pointer: Any) -> BindingReport:
    """Write the prepared ``graph:`` block, keeping a recoverable original.

    An unchanged home is a no-op that writes nothing, including on a repeat run
    after accepted semantic writes. A changed home gets its previous bytes
    copied to a timestamped sibling *before* the replacement, and the
    replacement itself lands through one ``os.replace``, so no reader ever sees
    a half-written configuration.
    """
    home_path = _absolute(home)
    pointer_path = _absolute(pointer)
    preparation, findings = _prepare(home_path, pointer_path)
    if preparation is None:
        return _report(
            status=STATUS_REFUSED, home=home_path, pointer=pointer_path,
            findings=findings,
        )
    if not preparation.changes:
        return _report(
            status=STATUS_UNCHANGED, home=home_path, pointer=pointer_path,
            generation=str(preparation.generation),
            retained_keys=preparation.retained, writes=False,
        )

    target = preparation.config.path
    backup: Path | None = None
    if preparation.config.exists:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = target.with_name(f"{CONFIG_NAME}{BACKUP_SUFFIX}-{stamp}")
        if backup.exists() or backup.is_symlink():
            return _report(
                status=STATUS_REFUSED, home=home_path, pointer=pointer_path,
                findings=[FINDING_BACKUP_EXISTS],
            )
    temporary = target.with_name(f".{CONFIG_NAME}.binding-{os.getpid()}")
    try:
        if backup is not None:
            backup.write_text(preparation.config.text, encoding="utf-8")
        temporary.write_text(preparation.text, encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        # Partial outcomes are reported as partial: the retained original, if it
        # was written, is named so recovery does not have to be guessed at.
        temporary.unlink(missing_ok=True)
        return _report(
            status=STATUS_REFUSED, home=home_path, pointer=pointer_path,
            findings=[f"{FINDING_WRITE_FAILED}:{type(exc).__name__}"],
            backup=None if backup is None else str(backup),
            writes=backup is not None and backup.exists(),
        )
    return _report(
        status=STATUS_APPLIED, home=home_path, pointer=pointer_path,
        generation=str(preparation.generation), changes=preparation.changes,
        retained_keys=preparation.retained,
        backup=None if backup is None else str(backup), writes=True,
    )


def verify_effective_binding(*, home: Any, pointer: Any) -> BindingReport:
    """Prove the *service's* configuration selects this pointer and mode.

    This constructs the complete :class:`~layered_memory_mcp.config.MemoryConfig`
    the normal startup path would build for that home — not the adapter's
    narrower generation config — and compares every projection it resolved
    against the generation the pointer attests.
    """
    from .config import MemoryConfig

    home_path = _absolute(home)
    pointer_path = _absolute(pointer)
    overrides = [
        f"{FINDING_ENVIRONMENT_OVERRIDE}:{name}"
        for name in OVERRIDE_ENVIRONMENT
        if os.environ.get(name) is not None
    ]
    if overrides:
        return _report(
            status=STATUS_REFUSED, home=home_path, pointer=pointer_path,
            findings=overrides,
        )
    bound, pointer_findings = _resolve_pointer(pointer_path)
    if bound is None:
        return _report(
            status=STATUS_REFUSED, home=home_path, pointer=pointer_path,
            findings=pointer_findings,
        )
    try:
        config = MemoryConfig(home=str(home_path))
    except Exception as exc:  # noqa: BLE001 — bounded operator report
        return _report(
            status=STATUS_REFUSED, home=home_path, pointer=pointer_path,
            generation=str(bound.root),
            findings=[f"{FINDING_EFFECTIVE_UNUSABLE}:{type(exc).__name__}"],
        )

    findings: list[str] = []
    if config.graph_mode != AUTHORITATIVE_MODE:
        findings.append(f"{FINDING_EFFECTIVE_MODE}:{config.graph_mode}")
    if config.graph_active_pointer != pointer_path:
        findings.append(FINDING_EFFECTIVE_POINTER)
    projections = {
        "graph_db_path": bound.graph_db_path,
        "graph_journal_path": bound.graph_journal_path,
        "knowledge_dir": bound.knowledge_dir,
        "l0_index_file": bound.l0_index_file,
        "vector_db_path": bound.vector_db_path,
    }
    effective: dict[str, Any] = {
        "graph_mode": config.graph_mode,
        "graph_active_pointer": (
            None if config.graph_active_pointer is None
            else str(config.graph_active_pointer)
        ),
        "home": str(config.home),
    }
    for name, expected in projections.items():
        actual = getattr(config, name, None)
        effective[name] = None if actual is None else str(actual)
        if actual is None or _absolute(actual) != _absolute(expected):
            findings.append(f"{FINDING_EFFECTIVE_PROJECTION}:{name}")
    return _report(
        status=STATUS_REFUSED if findings else STATUS_VERIFIED,
        home=home_path, pointer=pointer_path, generation=str(bound.root),
        findings=findings, effective=effective,
    )

"""Layered Memory write guard — deployment utilities for the Hermes plugin.

WHAT IT PROTECTS
----------------
MEMORY.md is framework-owned: the framework keeps exactly one index entry in it
by rewriting the file directly (``memory_compactor._ensure_index_entry_in_memory``).
Agent-side writes are therefore not just noise, they are the wrong store:

  * the native ``memory`` tool with ``target="memory"`` → blocked
  * ``write_file`` / ``patch`` aimed at ``memories/MEMORY.md`` → blocked
  * ``target="user"`` (USER.md) and every other path → untouched

WHY A HERMES PLUGIN, NOT A SHELL HOOK
-------------------------------------
An equivalent shell hook needs a ``hooks.pre_tool_call`` entry *plus*
``hooks_auto_accept: true`` — a global trust relaxation that auto-approves every
future hook on the host. A plugin needs one directory and one
``plugins.enabled`` entry, has no consent gate to weaken, and speaks the same
in-process ``pre_tool_call`` blocking contract.

SHAPE
-----
Mirrors ``dashboard_plugin``: ``check_guard_status`` / ``install_guard`` /
``remove_guard``, plus ``ensure_guard_installed`` for the ``auto`` policy and
``refresh_deployed_plugin`` to keep a deployed copy in step with the package.

KEEPING THE DEPLOYED COPY IN STEP
---------------------------------
The guard on the host is a *copy* of the payload bundled in this package. Every
framework upgrade therefore leaves the host one version behind unless somebody
re-runs an install — and the ``auto`` policy could not heal that by itself,
because ``install_guard`` refuses a version drift unless forced. So a drift was
sticky: ``check_guard_status`` reported ``update_available`` forever while the
deployed files quietly aged. ``refresh_deployed_plugin`` (v3.3.5) closes that:
it re-copies the payload when the versions differ, taking no other action, and
the framework calls it on its read path so upgrades self-heal.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("layered_memory_mcp.write_guard")

# Plugin identity — the directory name doubles as the `plugins.enabled` key.
GUARD_PLUGIN_NAME = "layered-memory-guard"
GUARD_VERSION = "3.4.1"

# Files that make up the deployed plugin (source dir → plugin dir).
PLUGIN_FILES = ("plugin.yaml", "__init__.py", "guard.py")

# Framework-side state: remembers what the installer changed on the host so
# `remove_guard` can put it back.
STATE_FILE_NAME = "write_guard_state.json"

# Locations probed when `hermes` is not on PATH — the MCP server usually runs as
# a service with a narrow PATH, so `shutil.which` alone is not enough.
HERMES_CLI_CANDIDATES = (
    "~/.hermes/bin/hermes",
    "/usr/local/bin/hermes",
    "/usr/local/lib/hermes-agent/.hermes/bin/hermes",
)

# A runner takes the argv list and returns (returncode, stdout, stderr).
Runner = Callable[[list[str]], "tuple[int, str, str]"]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _get_plugin_source_dir() -> Path:
    """Bundled plugin payload shipped inside this package."""
    return Path(__file__).parent / "plugin"


def plugin_dir(hermes_home: Path) -> Path:
    """Where the plugin lives once deployed."""
    return Path(hermes_home) / "plugins" / GUARD_PLUGIN_NAME


def _default_config_path(hermes_home: Path) -> Path:
    explicit = os.environ.get("HERMES_CONFIG_PATH")
    if explicit:
        return Path(explicit).expanduser()
    return Path(hermes_home) / "config.yaml"


def _default_state_path() -> Path:
    home = os.environ.get("LAYERED_MEMORY_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".layered-memory"
    return base / STATE_FILE_NAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def is_guard_installed(hermes_home: Path) -> bool:
    """True when the plugin payload is present."""
    target = plugin_dir(hermes_home)
    return all((target / name).exists() for name in PLUGIN_FILES)


def get_guard_version(hermes_home: Path) -> str | None:
    """Read the deployed plugin's version from its plugin.yaml.

    Line-scanned rather than YAML-parsed on purpose: the guard must be able to
    report status even where no YAML parser is importable.
    """
    manifest = plugin_dir(hermes_home) / "plugin.yaml"
    if not manifest.exists():
        return None
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("version:"):
                return stripped.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        return None
    return None


def _read_hermes_config(config_path: Path) -> dict:
    """Best-effort read of the host config; {} whenever it cannot be parsed."""
    try:
        import yaml
    except ImportError:
        return {}
    if not Path(config_path).exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception:  # noqa: BLE001 — status reporting must never raise
        return {}
    return data if isinstance(data, dict) else {}


def _plugin_enabled(config: dict) -> bool | None:
    """True/False when the host config is readable, None when it is not."""
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return None
    enabled = plugins.get("enabled")
    if enabled is None:
        return None
    if not isinstance(enabled, list):
        return None
    return GUARD_PLUGIN_NAME in [str(name) for name in enabled]


def _nudge_interval(config: dict) -> int | None:
    memory = config.get("memory")
    if not isinstance(memory, dict):
        return None
    value = memory.get("nudge_interval")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def check_guard_status(
    hermes_home: Path,
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
) -> dict:
    """Inspect the host and report what is (not) in place."""
    home = Path(hermes_home)
    cfg_path = Path(config_path) if config_path else _default_config_path(home)
    state = _read_state(Path(state_path) if state_path else _default_state_path())
    config = _read_hermes_config(cfg_path)

    installed = is_guard_installed(home)
    version = get_guard_version(home)
    enabled = _plugin_enabled(config)
    nudge = _nudge_interval(config)

    if not installed:
        status = "not_installed"
        message = (
            "MEMORY.md write guard is not installed. Agent-side writes to "
            "MEMORY.md (memory tool with target='memory', or write_file/patch on "
            "memories/MEMORY.md) are currently unguarded."
        )
    elif version != GUARD_VERSION:
        status = "update_available"
        message = (
            f"Write guard installed at v{version}, framework expects "
            f"v{GUARD_VERSION}. Re-run install_guard (force=True) to refresh."
        )
    elif enabled is False:
        status = "installed_disabled"
        message = (
            "Write guard files are deployed but the plugin is not in "
            f"plugins.enabled — the hook will not load. Run: "
            f"hermes plugins enable {GUARD_PLUGIN_NAME}"
        )
    else:
        status = "up_to_date"
        message = f"Write guard v{version} is installed and enabled."

    return {
        "installed": installed,
        "version": version,
        "expected_version": GUARD_VERSION,
        "enabled": enabled,
        "status": status,
        "nudge_interval": nudge,
        "nudge_silenced": nudge == 0,
        "hermes_cli": find_hermes_cli(),
        "config_path": str(cfg_path),
        "state_path": str(Path(state_path) if state_path else _default_state_path()),
        "last_install": state or None,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Host command execution
# ---------------------------------------------------------------------------

def find_hermes_cli() -> str | None:
    """Locate the `hermes` executable, or None when it cannot be resolved."""
    found = shutil.which("hermes")
    if found:
        return found
    for candidate in HERMES_CLI_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _run_command(cmd: list[str], runner: Runner | None) -> "tuple[int, str, str]":
    if runner is not None:
        try:
            return runner(cmd)
        except Exception as exc:  # noqa: BLE001 — a broken runner is reported, not raised
            return 127, "", str(exc)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


def _tail(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# ---------------------------------------------------------------------------
# State (install bookkeeping)
# ---------------------------------------------------------------------------

def _read_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state_path: Path, payload: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Deploy / enable / nudge
# ---------------------------------------------------------------------------

def _deploy_files(hermes_home: Path) -> dict:
    """Copy the bundled plugin payload into the host plugins directory."""
    source = _get_plugin_source_dir()
    target = plugin_dir(hermes_home)
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in PLUGIN_FILES:
            src = source / name
            if not src.exists():
                return {"ok": False, "detail": f"bundled file missing: {src}"}
            (target / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        # A stale bytecode cache from an older version must not shadow the new one.
        shutil.rmtree(target / "__pycache__", ignore_errors=True)
    except OSError as exc:
        return {"ok": False, "detail": f"deploy failed: {exc}"}
    return {"ok": True, "detail": f"deployed {len(PLUGIN_FILES)} files to {target}"}


def _enable_plugin(hermes_home: Path, runner: Runner | None) -> dict:
    """Put the plugin into `plugins.enabled` through the host's own CLI.

    The config file is never edited by hand: it is security-sensitive on the
    host, its exact schema is Hermes's business, and `hermes plugins enable` is
    the supported, validating path.
    """
    cli = find_hermes_cli()
    if not cli:
        return {
            "ok": False,
            "detail": "`hermes` CLI not resolvable — enable the plugin manually",
            "manual_command": f"hermes plugins enable {GUARD_PLUGIN_NAME}",
        }
    code, out, err = _run_command([cli, "plugins", "enable", GUARD_PLUGIN_NAME], runner)
    return {
        "ok": code == 0,
        "detail": f"`hermes plugins enable {GUARD_PLUGIN_NAME}` → exit {code}"
                  + (f"; stderr: {_tail(err)}" if code != 0 and err else ""),
        "stdout": _tail(out),
    }


def _disable_plugin(runner: Runner | None) -> dict:
    cli = find_hermes_cli()
    if not cli:
        return {
            "ok": False,
            "detail": "`hermes` CLI not resolvable — disable the plugin manually",
            "manual_command": f"hermes plugins disable {GUARD_PLUGIN_NAME}",
        }
    code, out, err = _run_command([cli, "plugins", "disable", GUARD_PLUGIN_NAME], runner)
    return {
        "ok": code == 0,
        "detail": f"`hermes plugins disable {GUARD_PLUGIN_NAME}` → exit {code}"
                  + (f"; stderr: {_tail(err)}" if code != 0 and err else ""),
        "stdout": _tail(out),
    }


def _set_nudge_interval(value: int, runner: Runner | None) -> dict:
    cli = find_hermes_cli()
    if not cli:
        return {
            "ok": False,
            "detail": "`hermes` CLI not resolvable — set the key manually",
            "manual_command": f"hermes config set memory.nudge_interval {value}",
        }
    code, out, err = _run_command(
        [cli, "config", "set", "memory.nudge_interval", str(value)], runner
    )
    return {
        "ok": code == 0,
        "detail": f"`hermes config set memory.nudge_interval {value}` → exit {code}"
                  + (f"; stderr: {_tail(err)}" if code != 0 and err else ""),
        "stdout": _tail(out),
    }


# ---------------------------------------------------------------------------
# Self-test — prove the deployed policy blocks the right calls
# ---------------------------------------------------------------------------

def self_test(hermes_home: Path) -> dict:
    """Exercise the *deployed* policy with positive cases and negative controls.

    Negative controls matter as much as the positive ones: a guard that blocks
    every file write would look healthy on the positive cases alone.
    """
    home = Path(hermes_home)
    guard_file = plugin_dir(home) / "guard.py"
    memory_path = str(home / "memories" / "MEMORY.md")
    user_path = str(home / "memories" / "USER.md")

    cases = (
        ("memory tool, target=memory", "memory", {"action": "add", "target": "memory", "content": "x"}, True),
        ("memory tool, target omitted", "memory", {"action": "add", "content": "x"}, True),
        ("memory tool, target=user (control)", "memory", {"action": "add", "target": "user", "content": "x"}, False),
        ("write_file on MEMORY.md", "write_file", {"path": memory_path, "content": "x"}, True),
        ("patch on USER.md (control)", "patch", {"path": user_path, "old_string": "a", "new_string": "b"}, False),
        ("write_file on an unrelated file (control)", "write_file", {"path": str(home / "notes.md"), "content": "x"}, False),
        ("unrelated tool (control)", "terminal", {"command": "ls"}, False),
    )

    if not guard_file.exists():
        return {"ok": False, "passed": 0, "total": len(cases), "error": "guard.py not deployed"}

    try:
        spec = importlib.util.spec_from_file_location("layered_memory_write_guard", guard_file)
        if spec is None or spec.loader is None:
            return {"ok": False, "passed": 0, "total": len(cases), "error": "cannot load deployed guard.py"}
        module = importlib.util.module_from_spec(spec)
        # Importing must not litter the host's plugin directory with bytecode.
        import sys as _sys
        _previous_dont_write = _sys.dont_write_bytecode
        _sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            _sys.dont_write_bytecode = _previous_dont_write
        decide = module.decide
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "passed": 0, "total": len(cases), "error": f"import failed: {exc}"}

    # The runtime escape hatch must not silently turn "all clear" into "no-op".
    disabled = bool(module.guard_disabled())

    results = []
    passed = 0
    for label, tool, payload, expect_block in cases:
        got = decide(tool, args=payload)
        blocked = isinstance(got, dict) and got.get("action") == "block"
        ok = blocked is expect_block
        passed += 1 if ok else 0
        results.append({"case": label, "expect": "block" if expect_block else "allow",
                        "got": "block" if blocked else "allow", "ok": ok})

    return {
        "ok": passed == len(cases) and not disabled,
        "passed": passed,
        "total": len(cases),
        "guard_disabled_by_env": disabled,
        "cases": results,
    }


# ---------------------------------------------------------------------------
# Install / remove
# ---------------------------------------------------------------------------

def install_guard(
    hermes_home: Path,
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    set_nudge_zero: bool = True,
    force: bool = False,
    runner: Runner | None = None,
) -> dict:
    """Deploy + enable the guard, optionally silence the memory-review nudge.

    Idempotent: re-running on a healthy install reports ``already_installed``
    and still repairs a missing enable / nudge setting.
    """
    home = Path(hermes_home)
    cfg_path = Path(config_path) if config_path else _default_config_path(home)
    st_path = Path(state_path) if state_path else _default_state_path()

    before = check_guard_status(home, config_path=cfg_path, state_path=st_path)

    if before["status"] == "update_available" and not force and before["installed"]:
        return {
            "success": False,
            "action": "version_conflict",
            "installed_version": before["version"],
            "expected_version": GUARD_VERSION,
            "status": before,
            "message": (
                f"Version conflict (installed v{before['version']}, expected "
                f"v{GUARD_VERSION}). Re-run with force=True to overwrite."
            ),
        }

    steps: list[dict] = []

    deploy = _deploy_files(home)
    steps.append({"step": "deploy_plugin_files", **deploy})

    enable = _enable_plugin(home, runner)
    steps.append({"step": "enable_plugin", **enable})

    nudge_result: dict | None = None
    if set_nudge_zero:
        previous = _read_hermes_config(cfg_path)
        previous_nudge = _nudge_interval(previous)
        nudge_result = _set_nudge_interval(0, runner)
        steps.append({"step": "silence_memory_nudge", **nudge_result})
        if nudge_result.get("ok"):
            state = _read_state(st_path)
            state.update(
                {
                    "nudge_interval_previous": previous_nudge,
                    "nudge_changed_at": _now_iso(),
                    "version": GUARD_VERSION,
                    "installed_at": state.get("installed_at") or _now_iso(),
                    "hermes_home": str(home),
                }
            )
            try:
                _write_state(st_path, state)
            except OSError as exc:
                steps.append({"step": "write_state", "ok": False, "detail": str(exc)})

    test = self_test(home)
    steps.append({
        "step": "self_test",
        "ok": bool(test.get("ok")),
        "detail": f"{test.get('passed')}/{test.get('total')} policy cases passed",
    })

    after = check_guard_status(home, config_path=cfg_path, state_path=st_path)
    manual = [s["manual_command"] for s in steps if s.get("manual_command")]

    if not deploy.get("ok"):
        action = "failed"
    elif before["installed"]:
        action = "updated" if before["status"] in ("update_available", "installed_disabled") else "already_installed"
    else:
        action = "installed"

    activated = bool(enable.get("ok"))
    message = {
        "installed": "Write guard installed.",
        "updated": "Write guard refreshed.",
        "already_installed": "Write guard was already installed.",
        "failed": "Write guard deployment FAILED — see steps.",
    }[action]
    if manual:
        message += " Manual steps still required: " + "; ".join(manual)
    if activated:
        message += " Restart Hermes to activate (CLI: next start; gateway: `hermes gateway restart`)."

    return {
        "success": deploy.get("ok", False) and not manual,
        "action": action,
        "version": GUARD_VERSION,
        "plugin_dir": str(plugin_dir(home)),
        "steps": steps,
        "self_test": test,
        "enabled": enable.get("ok"),
        "manual_actions": manual,
        "restart_required": activated,
        "status": after,
        "message": message,
    }


def refresh_deployed_plugin(
    hermes_home: Path,
    mode: str = "auto",
    state_path: str | Path | None = None,
) -> dict:
    """Re-copy the bundled plugin when the deployed copy is a different version.

    Deliberately **files-only**: no Hermes config is written, no CLI is invoked,
    nothing is enabled or disabled. This is the narrow repair for version drift,
    not an install — installing (config + enable) stays on the explicit path.

    Guarded so it can never surprise anyone:

    * ``mode`` must be ``auto`` — a ``manual`` host keeps the human in the loop,
      and the framework calls this on a read path nobody asked about;
    * the guard must already be installed;
    * a version match is a no-op.

    Never raises: it is called from ``get_l0_index`` and a repair attempt must
    not be able to break index retrieval.
    """
    home = Path(hermes_home)
    try:
        mode_clean = str(mode or "").strip().lower()
        if mode_clean != "auto":
            return {"refreshed": False, "reason": "policy_not_auto", "policy": mode_clean}
        if not is_guard_installed(home):
            return {"refreshed": False, "reason": "not_installed"}

        deployed = get_guard_version(home)
        if deployed == GUARD_VERSION:
            return {"refreshed": False, "reason": "up_to_date", "version": deployed}

        deploy = _deploy_files(home)
        if not deploy.get("ok"):
            logger.warning("write guard auto-refresh failed: %s", deploy.get("detail"))
            return {
                "refreshed": False,
                "reason": "deploy_failed",
                "from": deployed,
                "to": GUARD_VERSION,
                "detail": deploy.get("detail"),
            }

        # Keep the install record honest about what is now on disk.
        st_path = Path(state_path) if state_path else _default_state_path()
        state = _read_state(st_path)
        if state:
            state.update({"version": GUARD_VERSION, "refreshed_at": _now_iso()})
            try:
                _write_state(st_path, state)
            except OSError as exc:
                logger.debug("write guard refresh could not update state: %s", exc)

        logger.info("write guard auto-refreshed %s → %s", deployed, GUARD_VERSION)
        return {
            "refreshed": True,
            "from": deployed,
            "to": GUARD_VERSION,
            "detail": deploy.get("detail"),
            "note": (
                "Files refreshed. A running host keeps the previously loaded "
                "plugin until it restarts; a logic change needs that restart, a "
                "version bump alone does not."
            ),
        }
    except Exception as exc:  # noqa: BLE001 — a repair must never break a read
        logger.debug("write guard auto-refresh skipped: %s", exc)
        return {"refreshed": False, "reason": "error", "detail": str(exc)}


def ensure_guard_installed(
    hermes_home: Path,
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    set_nudge_zero: bool = True,
    runner: Runner | None = None,
) -> dict:
    """Install only when the host is not already healthy (the ``auto`` policy)."""
    home = Path(hermes_home)
    cfg_path = Path(config_path) if config_path else _default_config_path(home)
    st_path = Path(state_path) if state_path else _default_state_path()

    before = check_guard_status(home, config_path=cfg_path, state_path=st_path)
    if before["status"] == "up_to_date":
        return {"action": "noop", "before": before, "install": None, "status": before}

    if before["status"] == "update_available":
        # A version drift is not an install: install_guard refuses it without
        # force=True (to protect a host somebody else configured), so routing it
        # there made the drift permanent under the auto policy — which is
        # exactly how a deployed guard stayed behind a released framework.
        refreshed = refresh_deployed_plugin(home, mode="auto", state_path=st_path)
        after = check_guard_status(home, config_path=cfg_path, state_path=st_path)
        return {
            "action": "refreshed" if refreshed.get("refreshed") else "refresh_noop",
            "before": before,
            "install": None,
            "refresh": refreshed,
            "status": after,
        }

    install = install_guard(
        home,
        config_path=cfg_path,
        state_path=st_path,
        set_nudge_zero=set_nudge_zero,
        runner=runner,
    )
    return {"action": install.get("action"), "before": before, "install": install,
            "status": install.get("status", before)}


def remove_guard(
    hermes_home: Path,
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    restore_nudge: bool = True,
    runner: Runner | None = None,
) -> dict:
    """Uninstall the plugin, disable it on the host, restore the nudge setting."""
    home = Path(hermes_home)
    cfg_path = Path(config_path) if config_path else _default_config_path(home)
    st_path = Path(state_path) if state_path else _default_state_path()

    steps: list[dict] = []
    state = _read_state(st_path)
    target = plugin_dir(home)

    if target.exists():
        try:
            shutil.rmtree(target)
            steps.append({"step": "remove_plugin_files", "ok": True, "detail": f"removed {target}"})
        except OSError as exc:
            steps.append({"step": "remove_plugin_files", "ok": False, "detail": str(exc)})
    else:
        steps.append({"step": "remove_plugin_files", "ok": True, "detail": "not present"})

    disable = _disable_plugin(runner)
    steps.append({"step": "disable_plugin", **disable})

    previous = state.get("nudge_interval_previous")
    if restore_nudge and isinstance(previous, int):
        restored = _set_nudge_interval(previous, runner)
        steps.append({"step": "restore_memory_nudge", **restored})

    try:
        if st_path.exists():
            st_path.unlink()
    except OSError as exc:
        steps.append({"step": "remove_state", "ok": False, "detail": str(exc)})

    manual = [s["manual_command"] for s in steps if s.get("manual_command")]
    return {
        "success": not manual,
        "action": "removed",
        "steps": steps,
        "manual_actions": manual,
        "restart_required": bool(disable.get("ok")),
        "restored_nudge_interval": previous if restore_nudge else None,
        "message": "Write guard removed. Restart Hermes to unload the hook."
                   + (" Manual steps still required: " + "; ".join(manual) if manual else ""),
    }

"""Layered Memory Dashboard Plugin — Auto-deployment utilities.

This module provides functions to deploy the Dashboard plugin to the
Hermes plugins directory. Used by init_framework() and integrate_agent().
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger("layered_memory_mcp.dashboard_plugin")

# Plugin version — must match manifest.json
PLUGIN_VERSION = "2.5.0"


def _get_plugin_source_dir() -> Path:
    """Return the path to the bundled plugin files."""
    return Path(__file__).parent


def is_dashboard_plugin_installed(hermes_home: Path) -> bool:
    """Check if the Dashboard plugin is already installed."""
    plugin_dir = hermes_home / "plugins" / "layered-memory-dashboard" / "dashboard"
    manifest_path = plugin_dir / "manifest.json"
    return manifest_path.exists()


def get_dashboard_plugin_version(hermes_home: Path) -> str | None:
    """Read the installed plugin version from manifest.json."""
    manifest_path = hermes_home / "plugins" / "layered-memory-dashboard" / "dashboard" / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return manifest.get("version")
    except (json.JSONDecodeError, OSError):
        return None


def check_dashboard_plugin_status(hermes_home: Path) -> dict:
    """Check the plugin installation status.

    Returns a dict with keys:
      - installed: bool
      - version: str | None (installed version)
      - expected_version: str (current framework version)
      - status: "not_installed" | "up_to_date" | "update_available" | "version_conflict"
      - message: str (human-readable status)
    """
    expected = PLUGIN_VERSION
    installed_version = get_dashboard_plugin_version(hermes_home)
    installed = is_dashboard_plugin_installed(hermes_home)

    if not installed:
        return {
            "installed": False,
            "version": None,
            "expected_version": expected,
            "status": "not_installed",
            "message": "Dashboard plugin not installed.",
        }

    if installed_version == expected:
        return {
            "installed": True,
            "version": installed_version,
            "expected_version": expected,
            "status": "up_to_date",
            "message": f"Dashboard plugin is up to date (v{installed_version}).",
        }

    # Version mismatch — don't auto-update, prompt user to resolve with Hermes
    return {
        "installed": True,
        "version": installed_version,
        "expected_version": expected,
        "status": "update_available",
        "message": (
            f"Dashboard plugin version mismatch: installed v{installed_version}, "
            f"expected v{expected}. Please ask Hermes to assist with updating the plugin."
        ),
    }


def deploy_dashboard_plugin(hermes_home: Path, force: bool = False) -> dict:
    """Deploy the Dashboard plugin to the Hermes plugins directory.

    Args:
        hermes_home: Path to ~/.hermes
        force: If True, overwrite existing files even if up-to-date.

    Returns:
        Status dict with keys: success, action, version, message
    """
    plugin_dir = hermes_home / "plugins" / "layered-memory-dashboard" / "dashboard"
    dist_dir = plugin_dir / "dist"

    # Check current status
    status = check_dashboard_plugin_status(hermes_home)

    if status["status"] == "up_to_date" and not force:
        return {
            "success": True,
            "action": "already_installed",
            "version": PLUGIN_VERSION,
            "message": "Dashboard plugin is already installed and up to date.",
        }

    if status["status"] == "update_available" and not force:
        return {
            "success": False,
            "action": "version_conflict",
            "installed_version": status["version"],
            "expected_version": PLUGIN_VERSION,
            "message": (
                f"Version conflict detected (installed: {status['version']}, "
                f"expected: {PLUGIN_VERSION}). Use force=True to overwrite, or "
                f"ask Hermes to assist with resolving the conflict."
            ),
        }

    # Deploy files
    source_dir = _get_plugin_source_dir()
    try:
        plugin_dir.mkdir(parents=True, exist_ok=True)
        dist_dir.mkdir(parents=True, exist_ok=True)

        # Copy manifest.json
        manifest_src = source_dir / "manifest.json"
        manifest_dst = plugin_dir / "manifest.json"
        manifest_dst.write_text(manifest_src.read_text(encoding="utf-8"), encoding="utf-8")

        # Copy plugin_api.py
        api_src = source_dir / "plugin_api.py"
        api_dst = plugin_dir / "plugin_api.py"
        api_dst.write_text(api_src.read_text(encoding="utf-8"), encoding="utf-8")

        # Copy dist/index.js
        js_src = source_dir / "dist" / "index.js"
        js_dst = dist_dir / "index.js"
        js_dst.write_text(js_src.read_text(encoding="utf-8"), encoding="utf-8")

        return {
            "success": True,
            "action": "installed" if status["status"] == "not_installed" else "updated",
            "version": PLUGIN_VERSION,
            "message": (
                f"Dashboard plugin {'installed' if status['status'] == 'not_installed' else 'updated'} "
                f"successfully (v{PLUGIN_VERSION}). Restart Hermes Dashboard to activate."
            ),
        }
    except OSError as exc:
        return {
            "success": False,
            "action": "failed",
            "version": PLUGIN_VERSION,
            "message": f"Failed to deploy plugin: {exc}",
        }


def remove_dashboard_plugin(hermes_home: Path) -> dict:
    """Remove the Dashboard plugin from the Hermes plugins directory."""
    plugin_dir = hermes_home / "plugins" / "layered-memory-dashboard"
    if not plugin_dir.exists():
        return {
            "success": True,
            "action": "not_found",
            "message": "Dashboard plugin not found.",
        }
    try:
        import shutil
        shutil.rmtree(plugin_dir)
        return {
            "success": True,
            "action": "removed",
            "message": "Dashboard plugin removed. Restart Hermes Dashboard to complete.",
        }
    except OSError as exc:
        return {
            "success": False,
            "action": "failed",
            "message": f"Failed to remove plugin: {exc}",
        }

"""v3.8.1 — the declared version, pinned where a release actually reads it.

Why this file exists: a release ships a version the repo carries in more than
one place (``pyproject.toml``, the package's own import-time fallback, and the
write-guard plugin manifest it deploys). A bump that moves only one of them
produces a package that reports a version it is not — and, for the plugin, a
deployed guard that the installer then refuses as stale. So the version is
asserted as *one* fact with several spellings.

Nothing here reads the installed distribution metadata: a test that asserted
``importlib.metadata.version`` would pass or fail on how the dev environment
happens to be installed rather than on what this repository declares.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The target of this batch.
EXPECTED_VERSION = "3.8.1"


def _match(path: Path, pattern: str) -> str:
    found = re.search(pattern, path.read_text(encoding="utf-8"), re.M)
    assert found, f"no version declaration matching {pattern!r} in {path.name}"
    return found.group(1)


def test_pyproject_declares_the_target_version():
    assert _match(REPO / "pyproject.toml", r'^version = "([^"]+)"') == EXPECTED_VERSION


def test_the_package_fallback_matches_pyproject():
    """The fallback is what an uninstalled / source checkout reports."""
    declared = _match(REPO / "pyproject.toml", r'^version = "([^"]+)"')
    fallback = _match(
        REPO / "src" / "layered_memory_mcp" / "__init__.py",
        r'__version__ = "([^"]+)"',
    )
    assert fallback == declared == EXPECTED_VERSION


def test_the_write_guard_artifacts_agree_with_the_package_version():
    """Directly coupled metadata: the installer compares these for staleness."""
    from layered_memory_mcp.write_guard import GUARD_VERSION

    manifest = _match(
        REPO / "src" / "layered_memory_mcp" / "write_guard" / "plugin" / "plugin.yaml",
        r'^version: "([^"]+)"',
    )
    assert GUARD_VERSION == manifest == EXPECTED_VERSION


def test_no_source_file_still_declares_the_previous_version():
    """A bump that leaves one spelling behind is the failure mode here."""
    stale = [
        path.relative_to(REPO).as_posix()
        for path in [
            REPO / "pyproject.toml",
            REPO / "src" / "layered_memory_mcp" / "__init__.py",
            REPO / "src" / "layered_memory_mcp" / "write_guard" / "__init__.py",
            REPO / "src" / "layered_memory_mcp" / "write_guard" / "plugin" / "plugin.yaml",
        ]
        if "3.8.0" in path.read_text(encoding="utf-8")
    ]
    assert stale == []

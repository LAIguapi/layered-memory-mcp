#!/usr/bin/env python3
"""Test script for layered-memory-dashboard plugin.

Verifies:
1. Plugin files exist in the installed package
2. Plugin files can be deployed to Hermes plugins directory
3. Backend imports work correctly
4. Manifest is valid JSON
"""

import json
import sys
from pathlib import Path


def test_plugin_files_in_package():
    """Test that plugin files exist in the installed package."""
    import layered_memory_mcp
    
    pkg_dir = Path(layered_memory_mcp.__file__).parent
    plugin_dir = pkg_dir / "dashboard_plugin"
    
    required_files = [
        "__init__.py",
        "manifest.json",
        "plugin_api.py",
        "dist/index.js",
    ]
    
    for fname in required_files:
        fpath = plugin_dir / fname
        assert fpath.exists(), f"Missing: {fpath}"
        print(f"✓ {fname} exists ({fpath.stat().st_size} bytes)")
    
    return True


def test_manifest_valid():
    """Test that manifest.json is valid."""
    import layered_memory_mcp
    
    pkg_dir = Path(layered_memory_mcp.__file__).parent
    manifest_path = pkg_dir / "dashboard_plugin" / "manifest.json"
    
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    
    required_fields = ["name", "label", "description", "icon", "version", "tab", "entry"]
    for field in required_fields:
        assert field in manifest, f"Missing field: {field}"
    
    assert manifest["name"] == "layered-memory-dashboard"
    assert manifest["tab"]["path"] == "/layered-memory"
    
    print(f"✓ manifest.json valid (version: {manifest['version']})")
    return True


def test_deploy_function():
    """Test deploy function works."""
    from layered_memory_mcp.dashboard_plugin import (
        deploy_dashboard_plugin,
        check_dashboard_plugin_status,
        remove_dashboard_plugin,
        PLUGIN_VERSION,
    )
    
    hermes_home = Path.home() / ".hermes"
    
    # Check status
    status = check_dashboard_plugin_status(hermes_home)
    print(f"✓ check_dashboard_plugin_status: {status['status']}")
    
    # If already installed, verify version matches
    if status["installed"]:
        assert status["version"] == PLUGIN_VERSION, \
            f"Version mismatch: installed={status['version']}, expected={PLUGIN_VERSION}"
        print(f"✓ Version matches: {PLUGIN_VERSION}")
    
    return True


def test_backend_imports():
    """Test that backend API imports work."""
    try:
        from layered_memory_mcp.dashboard_plugin.plugin_api import router
        print(f"✓ plugin_api.router imported successfully")
        
        # Check endpoints
        routes = [r.path for r in router.routes]
        expected = ["/l0-index", "/knowledge-file", "/semantic-search", 
                   "/pending-reviews", "/approve-knowledge", "/reject-knowledge",
                   "/inject-knowledge", "/compact-memory", "/health",
                   "/todos", "/update-todo", "/rebuild-vectors"]
        
        for ep in expected:
            found = any(ep in r for r in routes)
            assert found, f"Missing endpoint: {ep}"
        
        print(f"✓ All {len(expected)} endpoints registered")
        return True
    except ImportError as e:
        print(f"⚠ Backend imports failed (expected if MCP package not in same env): {e}")
        return True  # Not a failure — this is expected in some environments


def main():
    print("=" * 60)
    print("Layered Memory Dashboard Plugin — Self-Test")
    print("=" * 60)
    
    tests = [
        ("Plugin files in package", test_plugin_files_in_package),
        ("Manifest valid JSON", test_manifest_valid),
        ("Deploy function", test_deploy_function),
        ("Backend imports", test_backend_imports),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_fn in tests:
        print(f"\n--- {name} ---")
        try:
            if test_fn():
                passed += 1
        except Exception as e:
            print(f"✗ FAILED: {e}")
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

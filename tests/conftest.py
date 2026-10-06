"""Global test isolation — keeps the suite from ever touching production data.

WHY THIS FILE EXISTS
--------------------
`MemoryConfig.detect_agent_memory_path()` resolves the agent memory file by
probing absolute, user-level locations (``Path.home()/".hermes"/...``). Those
probes deliberately ignore ``LAYERED_MEMORY_HOME``, because in production the
L1 store and the agent's own memory file are genuinely separate things.

The side effect: a test that set only ``LAYERED_MEMORY_HOME`` still resolved
the *real* ``~/.hermes/memories/MEMORY.md``, so any test exercising the
dual-write path wrote test fixtures straight into production memory. That is
exactly how 20 junk ``[L0]`` pointers (``svc_a``, ``new-file``, ``test``,
eight duplicate ``misc`` entries, ...) ended up in a live MEMORY.md.

The autouse fixture below closes every escape hatch at once, so isolation is
the default and no future test has to remember to opt in.
"""

from __future__ import annotations

import os

import pytest

# The developer's real home, captured at import time — before any fixture
# redirects HOME — so the production-path guard cannot be fooled by ordering.
_REAL_HOME = os.path.expanduser("~")

# Every env var that can steer a write at production data.
_REDIRECTED_ENV_VARS = (
    "LAYERED_MEMORY_HOME",
    "LAYERED_MEMORY_AGENT_MEMORY_PATH",
    "LAYERED_MEMORY_L0_INDEX_FILE",
    "LAYERED_MEMORY_SESSIONS_DIR",
    "LAYERED_MEMORY_COMPACT_DOMAIN_RULES_FILE",
)


@pytest.fixture(autouse=True)
def _isolate_memory_home(tmp_path, monkeypatch):
    """Redirect all memory paths into a per-test tmp_path.

    autouse: applies to every test in the suite, including ones added later
    that never think about isolation.

    Covers three independent resolution routes:
      1. ``LAYERED_MEMORY_HOME``           → the L1 knowledge store
      2. ``LAYERED_MEMORY_AGENT_MEMORY_PATH`` → the agent memory file, which
         would otherwise be probed from the real ``Path.home()``
      3. ``HOME`` / ``USERPROFILE``        → backstop for any code path that
         calls ``Path.home()`` directly rather than reading the config
    """
    sandbox = tmp_path / "memory-sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)

    # 1. L1 store
    monkeypatch.setenv("LAYERED_MEMORY_HOME", str(sandbox / "layered-memory"))

    # 2. Agent memory file — the route that caused the production leak.
    fake_home = sandbox / "home"
    (fake_home / ".hermes" / "memories").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(
        "LAYERED_MEMORY_AGENT_MEMORY_PATH",
        str(fake_home / ".hermes" / "memories" / "MEMORY.md"),
    )

    # 3. Anything reaching for Path.home() on its own.
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows equivalent

    # Drop inherited overrides so a developer's shell env can't leak in and
    # re-point a write at real data.
    for var in _REDIRECTED_ENV_VARS:
        if var in ("LAYERED_MEMORY_HOME", "LAYERED_MEMORY_AGENT_MEMORY_PATH"):
            continue  # set explicitly above
        monkeypatch.delenv(var, raising=False)

    yield sandbox


@pytest.fixture(autouse=True)
def _guard_production_paths(monkeypatch):
    """Hard stop: make writes to the real memory dirs raise instead of land.

    Defence in depth. If a code path ever hardcodes an absolute production
    path (bypassing both config and env), this converts a silent production
    write into a loud test failure.

    The plugin directory and the host config are on the list too: the write
    guard can deploy into ``~/.hermes/plugins`` and ``hermes plugins enable``
    rewrites ``config.yaml``, so a test that forgot to redirect ``HOME`` would
    otherwise be able to reconfigure the developer's own host.
    """
    import builtins

    real_open = builtins.open
    # Resolved once, from the real environment, before HOME is redirected.
    # Snapshotting at import time keeps this independent of fixture ordering.
    forbidden = tuple(
        os.path.join(_REAL_HOME, part)
        for part in (
            ".hermes/memories",
            ".layered-memory",
            ".hermes/plugins",
            ".hermes/config.yaml",
        )
    )

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(w in str(mode) for w in ("w", "a", "x", "+")):
            target = os.path.abspath(str(file))
            for bad in forbidden:
                if target.startswith(bad):
                    raise AssertionError(
                        "TEST ISOLATION VIOLATION: attempted write to "
                        f"production path {target!r}. Use the sandbox from "
                        "the _isolate_memory_home fixture instead."
                    )
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    yield


@pytest.fixture(autouse=True)
def _isolate_server_config():
    """Snapshot and restore the server's module-level config singleton.

    ``layered_memory_mcp.server._config`` is a lazily-built singleton, and this
    suite has a long-standing habit of assigning to it directly and setting it
    back to ``None`` by hand. A test that raises before its own cleanup then
    leaks its tmp config into every later test — the same class of leak that
    once wrote test fixtures into production memory. Restoring it centrally
    makes isolation the default instead of something every test must remember.
    """
    from layered_memory_mcp import server

    sentinel = getattr(server, "_config", None)
    yield
    server._config = sentinel

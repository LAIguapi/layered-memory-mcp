"""Global test isolation — keeps the suite from ever touching host data.

WHY THIS FILE EXISTS
--------------------
`MemoryConfig.detect_agent_memory_path()` resolves the agent memory file by
probing absolute, user-level locations (``Path.home()/".hermes"/...``). Those
probes deliberately ignore ``LAYERED_MEMORY_HOME``, because the L1 store and
the agent's own memory file are separate things.

The side effect: a test that set only ``LAYERED_MEMORY_HOME`` could still
resolve a host-managed memory path, so a test exercising the dual-write path
could escape the sandbox. The fixtures below close that escape hatch.

The autouse fixture below closes every escape hatch at once, so isolation is
the default and no future test has to remember to opt in.
"""

from __future__ import annotations

import os

import pytest

# The host home, captured at import time — before any fixture redirects HOME —
# so the host-path guard cannot be fooled by ordering.
_REAL_HOME = os.path.expanduser("~")

# Every env var that can steer a write at host-managed data.
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

    # 2. Agent memory file — a route that must remain inside the sandbox.
    fake_home = sandbox / "home"
    (fake_home / ".hermes" / "memories").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(
        "LAYERED_MEMORY_AGENT_MEMORY_PATH",
        str(fake_home / ".hermes" / "memories" / "MEMORY.md"),
    )

    # 3. Anything reaching for Path.home() on its own.
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows equivalent

    # Vector models are read-only large artifacts, not writable test data.
    # Keep an existing model cache visible after HOME is redirected so vector tests
    # do not try to bootstrap-download the embedding model in offline CI/dev.
    real_model_dir = os.path.join(_REAL_HOME, ".layered-memory", "models")
    if os.path.exists(real_model_dir):
        monkeypatch.setenv("LAYERED_MEMORY_MODEL_DIR", real_model_dir)

    # Drop inherited overrides so the caller's shell env can't leak in and
    # re-point a write at real data.
    for var in _REDIRECTED_ENV_VARS:
        if var in ("LAYERED_MEMORY_HOME", "LAYERED_MEMORY_AGENT_MEMORY_PATH"):
            continue  # set explicitly above
        monkeypatch.delenv(var, raising=False)

    yield sandbox


@pytest.fixture
def legacy_graph_mode(monkeypatch):
    """Opt in, explicitly, to the ``legacy`` rollback mode for one test.

    WHY THIS IS NOT AUTOUSE
    -----------------------
    The rollout default is ``graph`` (``config.DEFAULT_GRAPH_MODE``). Under that
    default the semantic journal owns the write path, so the whole-file L1 CRUD
    tools, the review lifecycle, the derived index/vector maintenance jobs and
    agent-memory compaction all *refuse before mutating* and name the semantic
    call to use instead — see ``graph_mutation_guard.REFUSED_OPERATIONS`` and
    ``MAINTENANCE_JOBS``.

    A suite-wide legacy fixture would make the default untested: every test
    would silently exercise the rollback mode, and the refusals that make the
    graph default safe would have no coverage at all. So this fixture is
    requested, one test or one feature at a time, and only where the test is
    *about* the legacy contract that ``legacy`` exists to keep working. Tests of
    the default must never request it.

    Requesting it is also the honest label: it says "this behaviour is what
    explicit rollback preserves", which is exactly what those tests pin.
    """
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_MODE", "legacy")
    return "legacy"


@pytest.fixture(autouse=True)
def _guard_host_paths(monkeypatch):
    """Hard stop: make writes to host-managed memory dirs raise instead of land.

    Defence in depth. If a code path ever hardcodes an absolute host-managed
    path (bypassing both config and env), this converts a silent host-data
    write into a loud test failure.

    The plugin directory and the host config are on the list too: the write
    guard can deploy into ``~/.hermes/plugins`` and ``hermes plugins enable``
    rewrites ``config.yaml``, so a test that forgot to redirect ``HOME`` would
    otherwise be able to reconfigure the caller's own host.

    ``pathlib.Path.open`` is patched alongside ``builtins.open`` because it does
    **not** go through it: ``Path.open`` calls ``io.open`` directly, so every
    ``path.write_text(...)`` in the framework — which is how all agent-memory
    and L1 writes are actually performed — used to sail straight past this
    backstop.
    """
    import builtins
    import pathlib

    real_open = builtins.open
    real_path_open = pathlib.Path.open
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

    def _check(file, mode):
        if not any(w in str(mode) for w in ("w", "a", "x", "+")):
            return
        target = os.path.abspath(str(file))
        for bad in forbidden:
            if target.startswith(bad):
                raise AssertionError(
                    "TEST ISOLATION VIOLATION: attempted write to "
                    f"host-managed path {target!r}. Use the sandbox from "
                    "the _isolate_memory_home fixture instead."
                )

    def guarded_open(file, mode="r", *args, **kwargs):
        _check(file, mode)
        return real_open(file, mode, *args, **kwargs)

    def guarded_path_open(self, mode="r", *args, **kwargs):
        _check(self, mode)
        return real_path_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(pathlib.Path, "open", guarded_path_open)
    yield


@pytest.fixture(autouse=True)
def _isolate_server_config():
    """Snapshot and restore the server's module-level config singleton.

    ``layered_memory_mcp.server._config`` is a lazily-built singleton, and this
    suite has a long-standing habit of assigning to it directly and setting it
    back to ``None`` by hand. A test that raises before its own cleanup then
    leaks its tmp config into every later test — the same class of cross-test
    leak that can write fixtures outside the sandbox. Restoring it centrally
    makes isolation the default instead of something every test must remember.
    """
    from layered_memory_mcp import server

    sentinel = getattr(server, "_config", None)
    yield
    server._config = sentinel

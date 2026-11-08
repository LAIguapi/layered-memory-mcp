"""Global test path isolation with a defense-in-depth Python mutation guard.

WHY THIS FILE EXISTS
--------------------
`MemoryConfig.detect_agent_memory_path()` resolves the agent memory file by
probing absolute, user-level locations (``Path.home()/".hermes"/...``). Those
probes deliberately ignore ``LAYERED_MEMORY_HOME``, because the L1 store and
the agent's own memory file are separate things.

The side effect: a test that set only ``LAYERED_MEMORY_HOME`` could still
resolve a host-managed memory path, so a test exercising the dual-write path
could escape the sandbox. The fixtures below close that escape hatch.

The runner must isolate HOME, config/memory/graph paths and cache roots before
collection imports. Fixtures then give each test disposable paths. This guard
is not an OS sandbox: native code, subprocesses, preopened descriptors,
unpatched APIs, symlink aliases and hostile races remain outside its guarantee.
"""

from __future__ import annotations

import os

import pytest

# The host home, captured at import time — before any fixture redirects HOME —
# so the host-path guard cannot be fooled by ordering.
_REAL_HOME = os.path.expanduser("~")
_HOST_HOMES = {_REAL_HOME, "/root"}
if os.environ.get("USERPROFILE"):
    _HOST_HOMES.add(os.environ["USERPROFILE"])

# Every env var that can steer a write at host-managed data.
_REDIRECTED_ENV_VARS = (
    "LAYERED_MEMORY_HOME",
    "LAYERED_MEMORY_AGENT_MEMORY_PATH",
    "LAYERED_MEMORY_L0_INDEX_FILE",
    "LAYERED_MEMORY_SESSIONS_DIR",
    "LAYERED_MEMORY_COMPACT_DOMAIN_RULES_FILE",
    "LAYERED_MEMORY_HERMES_DB",
    "LAYERED_MEMORY_MODEL_DIR",
    "LAYERED_MEMORY_CHANGELOG_PATH",
    "LAYERED_MEMORY_GRAPH_DB_PATH",
    "LAYERED_MEMORY_GRAPH_JOURNAL_PATH",
    "LAYERED_MEMORY_GRAPH_ACTIVE_POINTER",
    "LAYERED_MEMORY_AGENT_EXTENSIONS_ROOT",
)
_INHERITED_PROTECTED_PATHS = [
    os.environ[name] if name.endswith(("HOME", "DIR", "ROOT")) else os.path.dirname(os.environ[name])
    for name in _REDIRECTED_ENV_VARS if os.environ.get(name) and os.path.isabs(os.environ[name])
]
_INHERITED_PROTECTED_PATHS.extend(
    os.environ[name] for name in (
        "HF_HOME", "TRANSFORMERS_CACHE", "TORCH_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    ) if os.environ.get(name) and os.path.isabs(os.environ[name])
)


def pytest_configure(config):
    """Block model construction even in legacy collection-time model probes."""
    import fastembed

    def forbidden_model(*args, **kwargs):
        raise RuntimeError("test_model_loading_forbidden")

    config._privacy_model_guard = pytest.MonkeyPatch()
    config._privacy_model_guard.setattr(fastembed, "TextEmbedding", forbidden_model)


def pytest_unconfigure(config):
    guard = getattr(config, "_privacy_model_guard", None)
    if guard is not None:
        guard.undo()


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

    # Drop inherited overrides so the caller's shell env can't leak in and
    # re-point a write at real data.
    for var in _REDIRECTED_ENV_VARS:
        if var in ("LAYERED_MEMORY_HOME", "LAYERED_MEMORY_AGENT_MEMORY_PATH"):
            continue  # set explicitly above
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("LAYERED_MEMORY_HERMES_DB", str(sandbox / "sessions.db"))
    monkeypatch.setenv("LAYERED_MEMORY_SESSIONS_DIR", str(sandbox / "sessions"))
    monkeypatch.setenv("LAYERED_MEMORY_MODEL_DIR", str(sandbox / "models"))

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


@pytest.fixture
def synthetic_embeddings(monkeypatch):
    """Explicit model-free vectors for storage/persistence tests, not semantics."""
    import numpy as np
    from layered_memory_mcp.storage import vector_store

    def embed(texts):
        return np.ones((len(texts), vector_store.EMBED_DIM), dtype=np.float32) / np.sqrt(vector_store.EMBED_DIM)

    monkeypatch.setattr(vector_store, "_embed_texts", embed)
    return embed


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
    import io
    import sqlite3
    from urllib.parse import unquote, urlsplit

    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open
    real_connect = sqlite3.connect
    # Resolved once, from the real environment, before HOME is redirected.
    # Snapshotting at import time keeps this independent of fixture ordering.
    forbidden = list(
        os.path.join(home, part)
        for home in _HOST_HOMES
        for part in (
            ".hermes/memories",
            ".layered-memory",
            ".layered-memory-graph",
            ".hermes/plugins",
            ".hermes/config.yaml",
            ".hermes/sessions",
            ".hermes/state.db",
            ".claude",
            ".cursor",
            ".cline",
        )
    )
    forbidden.extend(_INHERITED_PROTECTED_PATHS)

    def _check(file, writing=True, dir_fd=None):
        if not writing:
            return
        if isinstance(file, int):
            file = os.readlink(f"/proc/self/fd/{file}")
        target = os.fsdecode(file)
        if dir_fd is not None and not os.path.isabs(target):
            try:
                target = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), target)
            except OSError:
                raise AssertionError("TEST ISOLATION VIOLATION: unresolved directory descriptor") from None
        target = os.path.abspath(target)
        for bad in forbidden:
            if os.path.commonpath((target, os.path.abspath(bad))) == os.path.abspath(bad):
                raise AssertionError("TEST ISOLATION VIOLATION: protected mutation")

    def _writing(mode):
        return any(flag in str(mode) for flag in ("w", "a", "x", "+"))

    def guarded_open(file, mode="r", *args, **kwargs):
        _check(file, _writing(mode))
        return real_open(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        _check(file, _writing(mode))
        return real_io_open(file, mode, *args, **kwargs)

    def guarded_os_open(file, flags, *args, **kwargs):
        _check(file, bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)),
               kwargs.get("dir_fd"))
        return real_os_open(file, flags, *args, **kwargs)

    def guarded_connect(database, *args, **kwargs):
        target = os.fsdecode(database)
        if target != ":memory:":
            if kwargs.get("uri") and target.startswith("file:"):
                target = unquote(urlsplit(target).path)
            _check(target)
        return real_connect(database, *args, **kwargs)

    def mutation_guard(original, two_paths=False):
        def guarded(source, *args, **kwargs):
            _check(source, dir_fd=kwargs.get("src_dir_fd") if two_paths else kwargs.get("dir_fd"))
            if two_paths:
                destination = args[0] if args else kwargs["dst"]
                _check(destination, dir_fd=kwargs.get("dst_dir_fd"))
            return original(source, *args, **kwargs)
        return guarded

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    for name in ("replace", "rename", "link", "symlink"):
        monkeypatch.setattr(os, name, mutation_guard(getattr(os, name), two_paths=True))
    for name in ("unlink", "remove", "mkdir", "rmdir", "truncate", "chmod", "utime"):
        monkeypatch.setattr(os, name, mutation_guard(getattr(os, name)))
    yield forbidden


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


@pytest.fixture(autouse=True)
def _forbid_real_models_and_network(monkeypatch):
    """Tests must inject models; this is not a replacement embedding model."""
    import fastembed
    import socket

    def forbidden_model(*args, **kwargs):
        raise RuntimeError("test_model_loading_forbidden")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TEST ISOLATION VIOLATION: network connection")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TEST ISOLATION VIOLATION: network connection")
        return real_connect_ex(self, address)

    monkeypatch.setattr(fastembed, "TextEmbedding", forbidden_model)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield

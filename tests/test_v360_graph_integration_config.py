"""v3.6.0 Batch H — the one graph integration decision lives in the config.

Why this file exists: ARCHITECTURE_v2.md §12 Batch H turns the B–G graph seams
into a *reversible* runtime integration. Reversible means exactly one validated
switch, defaulting to graph with an explicit legacy rollback, that a caller can
read without string comparisons — not a handful of booleans that drift apart
until nobody can say whether the graph is authoritative.

These tests pin:

  * **Graph is the rollout default.** A ``MemoryConfig()`` with no mode override
    enables authoritative graph recall and write, not shadow. Explicit legacy
    keeps both graph paths disabled for rollback.
  * **The mode is validated, never guessed.** An unknown value raises at
    construction instead of silently degrading to legacy — a half-enabled graph
    is the one state this batch must not be able to reach by typo.
  * **One decision, four callers.** ``graph_recall_enabled`` /
    ``graph_write_enabled`` / ``graph_shadow_enabled`` are the only things a
    caller branches on, and they derive from the single mode.
  * **Resolution follows the project's own chain** — constructor → env →
    ``graph:`` in ``<home>/config.yaml`` → default — and a malformed YAML
    section cannot crash start-up.
  * **No backend is constructed.** Enabling a mode must not make the config
    build, open or name a graph store; the store is injected by the runtime
    assembly (Batch H §3.1).

Every config here is built against ``tmp_path`` with an explicit home. Nothing
reads the real memory home, the real config.yaml or any user memory data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from layered_memory_mcp.config import GRAPH_MODES, MemoryConfig

_GRAPH_ENV_VARS = ("LAYERED_MEMORY_GRAPH_MODE", "LAYERED_MEMORY_GRAPH_DB_PATH")


@pytest.fixture(autouse=True)
def _clean_graph_env(monkeypatch):
    """No inherited graph env var may decide what these tests observe."""
    for var in _GRAPH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _config(tmp_path: Path, **kwargs) -> MemoryConfig:
    home = tmp_path / "graph-home"
    home.mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), **kwargs)


def _write_graph_yaml(tmp_path: Path, body: str) -> None:
    home = tmp_path / "graph-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------

def test_the_declared_modes_are_the_five_batch_h_modes():
    assert GRAPH_MODES == (
        "legacy",
        "shadow",
        "graph_recall",
        "graph_write",
        "graph",
    )


@pytest.mark.parametrize("body", [None, "graph: {}\n"])
def test_default_is_graph_and_authoritative_on_both_paths(tmp_path, body):
    if body is not None:
        _write_graph_yaml(tmp_path, body)
    config = _config(tmp_path)

    assert config.graph_mode == "graph"
    assert config.graph_enabled is True
    assert config.graph_recall_enabled is True
    assert config.graph_write_enabled is True
    assert config.graph_shadow_enabled is False


def test_existing_programmatic_construction_still_works(tmp_path):
    """No existing caller has to learn about the new option."""
    config = MemoryConfig(
        home=str(tmp_path / "plain"),
        knowledge_dir=str(tmp_path / "plain" / "knowledge"),
    )
    assert config.graph_mode == "graph"


# ---------------------------------------------------------------------------
# The mode matrix — one decision, three predicates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mode,recall,write,shadow",
    [
        ("legacy", False, False, False),
        ("shadow", False, False, True),
        ("graph_recall", True, False, False),
        ("graph_write", False, True, False),
        ("graph", True, True, False),
    ],
)
def test_each_mode_resolves_to_exactly_one_set_of_predicates(
    tmp_path, mode, recall, write, shadow
):
    config = _config(tmp_path, graph_mode=mode)

    assert config.graph_mode == mode
    assert config.graph_recall_enabled is recall
    assert config.graph_write_enabled is write
    assert config.graph_shadow_enabled is shadow
    assert config.graph_enabled is (mode != "legacy")


def test_shadow_enables_neither_authoritative_path(tmp_path):
    """Shadow observes; it never makes the graph the answer or the writer."""
    config = _config(tmp_path, graph_mode="shadow")
    assert (config.graph_recall_enabled, config.graph_write_enabled) == (False, False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["graphrecall", "on", "true", "GRAPH-RECALL", "both"])
def test_an_unknown_mode_fails_validation_instead_of_falling_back(tmp_path, bad):
    with pytest.raises(ValueError) as excinfo:
        _config(tmp_path, graph_mode=bad)
    assert "graph_mode" in str(excinfo.value)


def test_an_unknown_mode_in_the_environment_also_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_MODE", "graph_everything")
    with pytest.raises(ValueError):
        _config(tmp_path)


def test_case_and_whitespace_are_normalised(tmp_path):
    assert _config(tmp_path, graph_mode="  Graph_Recall  ").graph_mode == "graph_recall"


# ---------------------------------------------------------------------------
# Resolution chain: constructor → env → config.yaml → default
# ---------------------------------------------------------------------------

def test_environment_sets_the_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_MODE", "graph")
    config = _config(tmp_path)
    assert config.graph_mode == "graph"
    assert (config.graph_recall_enabled, config.graph_write_enabled) == (True, True)


def test_constructor_beats_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_MODE", "graph")
    assert _config(tmp_path, graph_mode="legacy").graph_mode == "legacy"


def test_config_yaml_sets_the_mode(tmp_path):
    _write_graph_yaml(tmp_path, "graph:\n  mode: shadow\n")
    assert _config(tmp_path).graph_mode == "shadow"


def test_environment_beats_config_yaml(tmp_path, monkeypatch):
    _write_graph_yaml(tmp_path, "graph:\n  mode: shadow\n")
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_MODE", "graph_recall")
    assert _config(tmp_path).graph_mode == "graph_recall"


def test_a_malformed_graph_section_falls_back_to_graph(tmp_path):
    """A broken config.yaml must not crash start-up (project convention)."""
    _write_graph_yaml(tmp_path, "graph: not-a-mapping\n")
    assert _config(tmp_path).graph_mode == "graph"


def test_an_invalid_mode_in_config_yaml_still_fails_loudly(tmp_path):
    _write_graph_yaml(tmp_path, "graph:\n  mode: graph_everything\n")
    with pytest.raises(ValueError):
        _config(tmp_path)


# ---------------------------------------------------------------------------
# No backend assembly in the config (§3.1, §5 non-goal 2)
# ---------------------------------------------------------------------------

def test_enabling_a_mode_constructs_no_graph_backend(tmp_path, monkeypatch):
    """Without a pointer, modes declare targets without resolving or opening them."""
    import sqlite3

    from layered_memory_mcp import graph_generation_runtime, graph_runtime_assembly

    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", raising=False)
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_JOURNAL_PATH", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("a config without an active pointer must not resolve or build a backend")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(graph_generation_runtime, "resolve_generation_runtime", forbidden)
    for backend in (
        graph_runtime_assembly.SQLiteGraphStore,
        graph_runtime_assembly.MemoryEventJournal,
        graph_runtime_assembly.GraphRuntimeAssembly,
        graph_runtime_assembly.JournalRuntimeAssembly,
    ):
        monkeypatch.setattr(backend, "__init__", forbidden)

    config = _config(tmp_path, graph_mode="graph")

    assert not [
        name
        for name in dir(config)
        if "graph" in name and name not in {
            "graph_mode",
            "graph_enabled",
            "graph_recall_enabled",
            "graph_write_enabled",
            "graph_shadow_enabled",
            "graph_db_path",
            "graph_journal_path",
            "graph_active_pointer",
            "_bind_graph_generation",
        }
    ]
    assert config.graph_db_path is None
    assert config.graph_journal_path is None
    assert config.graph_active_pointer is None
    assert not [path for path in config.home.rglob("*") if path.is_file()]


# ---------------------------------------------------------------------------
# The graph database path (v3.6.0 Batch I-A)
# ---------------------------------------------------------------------------

def test_no_database_path_is_configured_by_default(tmp_path):
    config = _config(tmp_path, graph_mode="graph")

    # Not guessed from the home, the knowledge dir or the agent memory path:
    # an unconfigured graph has no file, and the assembly says so.
    assert config.graph_db_path is None


def test_the_constructor_sets_the_database_path(tmp_path):
    target = tmp_path / "graph-data" / "graph.db"
    assert _config(tmp_path, graph_db_path=str(target)).graph_db_path == target


def test_setting_a_path_creates_no_file_and_no_directory(tmp_path):
    target = tmp_path / "graph-data" / "graph.db"
    _config(tmp_path, graph_db_path=str(target))

    assert not target.exists()
    assert not target.parent.exists()


def test_the_environment_sets_the_database_path(tmp_path, monkeypatch):
    target = tmp_path / "from-env.db"
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_DB_PATH", str(target))
    assert _config(tmp_path).graph_db_path == target


def test_the_constructor_beats_the_environment_for_the_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_DB_PATH", str(tmp_path / "from-env.db"))
    target = tmp_path / "explicit.db"
    assert _config(tmp_path, graph_db_path=str(target)).graph_db_path == target


def test_config_yaml_sets_the_database_path(tmp_path):
    target = tmp_path / "from-yaml.db"
    _write_graph_yaml(tmp_path, f"graph:\n  mode: shadow\n  db_path: {target}\n")
    assert _config(tmp_path).graph_db_path == target


def test_the_environment_beats_config_yaml_for_the_path(tmp_path, monkeypatch):
    _write_graph_yaml(tmp_path, f"graph:\n  db_path: {tmp_path / 'from-yaml.db'}\n")
    target = tmp_path / "from-env.db"
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_DB_PATH", str(target))
    assert _config(tmp_path).graph_db_path == target


def test_a_malformed_graph_section_leaves_the_path_unset(tmp_path):
    _write_graph_yaml(tmp_path, "graph: not-a-mapping\n")
    assert _config(tmp_path).graph_db_path is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_path_means_unconfigured_rather_than_a_guess(tmp_path, blank):
    assert _config(tmp_path, graph_db_path=blank).graph_db_path is None


def test_a_user_relative_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    assert _config(tmp_path, graph_db_path="~/graph.db").graph_db_path == (
        tmp_path / "fake-home" / "graph.db"
    )


@pytest.mark.parametrize("bad", ["graph.db", "data/graph.db", "./graph.db"])
def test_a_relative_path_is_refused_instead_of_resolved_against_the_cwd(
    tmp_path, bad
):
    with pytest.raises(ValueError) as excinfo:
        _config(tmp_path, graph_db_path=bad)
    assert "graph_db_path" in str(excinfo.value)


def test_a_multi_line_path_is_refused(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        _config(tmp_path, graph_db_path=f"{tmp_path}/a\n{tmp_path}/b")
    assert "graph_db_path" in str(excinfo.value)


def test_a_non_path_value_in_config_yaml_is_refused(tmp_path):
    _write_graph_yaml(tmp_path, "graph:\n  db_path: true\n")
    with pytest.raises(ValueError) as excinfo:
        _config(tmp_path)
    assert "graph_db_path" in str(excinfo.value)


def test_the_config_module_opens_no_graph_database(tmp_path, monkeypatch):
    """Building a graph-enabled config touches no sqlite connection."""
    import sqlite3

    def _refuse(*args, **kwargs):
        raise AssertionError("a graph-enabled config must not open a database")

    monkeypatch.setattr(sqlite3, "connect", _refuse)
    assert _config(tmp_path, graph_mode="graph").graph_mode == "graph"


def test_no_graph_module_is_imported_by_the_config(tmp_path, monkeypatch):
    """Only explicit, read-only pointer resolution may cross the graph boundary."""
    import ast
    import sqlite3
    from unittest.mock import Mock

    from layered_memory_mcp import (
        graph_generation_runtime,
        graph_migration,
        graph_migration_generation,
        graph_runtime_assembly,
    )
    from layered_memory_mcp.storage import vector_store
    from test_v361_graph_migration import _fake_embed, build_source, tree_state

    config_source = Path(__file__).resolve().parents[1].joinpath(
        "src", "layered_memory_mcp", "config.py"
    )
    tree = ast.parse(config_source.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
            if node.module == "graph_generation_runtime":
                assert {alias.name for alias in node.names} == {"resolve_generation_runtime"}
    assert {name for name in imported if "graph" in name} == {".graph_generation_runtime"}

    for variable in (
        "LAYERED_MEMORY_GRAPH_ACTIVE_POINTER",
        "LAYERED_MEMORY_GRAPH_JOURNAL_PATH",
        "LAYERED_MEMORY_NAMESPACE",
        "LAYERED_MEMORY_L0_FORMAT",
        "LAYERED_MEMORY_L0_TAG",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(vector_store, "_embed_texts", _fake_embed)
    source = build_source(tmp_path / "old-store")
    control = tmp_path / "control"
    control.mkdir()
    destination = control / "migration"
    migration = graph_migration.apply_migration(source=source, destination=destination)
    assert migration["status"] == graph_migration.STATUS_APPLIED, migration
    generation = destination / "current"
    pointer = control / "active.json"
    activation = graph_generation_runtime.activate_generation(
        pointer=pointer, generation=generation, apply=True
    )
    assert activation["status"] == graph_generation_runtime.STATUS_CUTOVER_APPLIED, activation
    before = tree_state(control)

    def forbidden(*args, **kwargs):
        pytest.fail("config binding must not open a database or write generation state")

    for operation in (
        "activate_generation",
        "restore_generation",
        "register_admission",
        "_write_pointer",
    ):
        monkeypatch.setattr(graph_generation_runtime, operation, forbidden)
    monkeypatch.setattr(graph_migration_generation, "register_admission", forbidden)
    monkeypatch.setattr(graph_migration, "apply_migration", forbidden)
    monkeypatch.setattr(graph_migration, "rollback_migration", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)

    resolved = graph_generation_runtime.resolve_generation_runtime(
        pointer=pointer, graph_mode="graph"
    )
    assert resolved.usable, resolved.report
    assert tree_state(control) == before

    resolver = Mock(return_value=resolved)
    monkeypatch.setattr(graph_generation_runtime, "resolve_generation_runtime", resolver)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    for backend in (
        graph_runtime_assembly.SQLiteGraphStore,
        graph_runtime_assembly.MemoryEventJournal,
        graph_runtime_assembly.GraphRuntimeAssembly,
        graph_runtime_assembly.JournalRuntimeAssembly,
    ):
        monkeypatch.setattr(backend, "__init__", forbidden)

    ordinary = _config(tmp_path, graph_mode="graph")
    assert ordinary.graph_active_pointer is None
    assert ordinary.graph_db_path is None
    assert ordinary.graph_journal_path is None
    resolver.assert_not_called()
    assert tree_state(control) == before

    config = _config(tmp_path, graph_mode="graph", graph_active_pointer=pointer)
    resolver.assert_called_once_with(pointer=pointer, graph_mode="graph")
    assert config.graph_active_pointer == pointer
    assert config.graph_db_path == generation / "graph.db"
    assert config.graph_journal_path == generation / "journal.db"
    assert config.knowledge_dir == generation / "knowledge"
    assert config.l0_index_file == generation / "L0.md"
    assert config.vector_db_path == generation / "data" / "vectors.db"
    assert tree_state(control) == before

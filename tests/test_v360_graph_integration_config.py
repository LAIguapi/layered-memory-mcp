"""v3.6.0 Batch H — the one graph integration decision lives in the config.

Why this file exists: ARCHITECTURE_v2.md §12 Batch H turns the B–G graph seams
into a *reversible* runtime integration. Reversible means exactly one validated
switch, defaulting to the old behaviour, that a caller can read without string
comparisons — not a handful of booleans that drift apart until nobody can say
whether the graph is authoritative.

These tests pin:

  * **Legacy is the default.** A ``MemoryConfig()`` built the way every existing
    test builds one keeps the old recall and the old write path, and the three
    predicates all say so.
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

_GRAPH_ENV_VARS = ("LAYERED_MEMORY_GRAPH_MODE",)


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


def test_default_is_legacy_and_nothing_graph_shaped_is_enabled(tmp_path):
    config = _config(tmp_path)

    assert config.graph_mode == "legacy"
    assert config.graph_enabled is False
    assert config.graph_recall_enabled is False
    assert config.graph_write_enabled is False
    assert config.graph_shadow_enabled is False


def test_existing_programmatic_construction_still_works(tmp_path):
    """No existing caller has to learn about the new option."""
    config = MemoryConfig(
        home=str(tmp_path / "plain"),
        knowledge_dir=str(tmp_path / "plain" / "knowledge"),
    )
    assert config.graph_mode == "legacy"


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


def test_a_malformed_graph_section_falls_back_to_legacy(tmp_path):
    """A broken config.yaml must not crash start-up (project convention)."""
    _write_graph_yaml(tmp_path, "graph: not-a-mapping\n")
    assert _config(tmp_path).graph_mode == "legacy"


def test_an_invalid_mode_in_config_yaml_still_fails_loudly(tmp_path):
    _write_graph_yaml(tmp_path, "graph:\n  mode: graph_everything\n")
    with pytest.raises(ValueError):
        _config(tmp_path)


# ---------------------------------------------------------------------------
# No backend assembly in the config (§3.1, §5 non-goal 2)
# ---------------------------------------------------------------------------

def test_enabling_a_mode_constructs_no_graph_backend(tmp_path):
    config = _config(tmp_path, graph_mode="graph")

    # No store, no handle, no path: the backend is injected by the runtime
    # assembly, not derived from a default config.
    assert not [
        name
        for name in dir(config)
        if "graph" in name and name not in {
            "graph_mode",
            "graph_enabled",
            "graph_recall_enabled",
            "graph_write_enabled",
            "graph_shadow_enabled",
        }
    ]


def test_the_config_module_opens_no_graph_database(tmp_path, monkeypatch):
    """Building a graph-enabled config touches no sqlite connection."""
    import sqlite3

    def _refuse(*args, **kwargs):
        raise AssertionError("a graph-enabled config must not open a database")

    monkeypatch.setattr(sqlite3, "connect", _refuse)
    assert _config(tmp_path, graph_mode="graph").graph_mode == "graph"


def test_no_graph_module_is_imported_by_the_config(tmp_path):
    """The config resolves a string; it does not reach into the graph layer."""
    import ast

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
    assert not {name for name in imported if "graph" in name}

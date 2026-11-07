"""Complete service configuration must bind one explicitly activated generation."""

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from layered_memory_mcp.config import DEFAULT_GRAPH_MODE, MemoryConfig
from layered_memory_mcp.graph_generation_runtime import STATUS_CUTOVER_APPLIED, activate_generation
from layered_memory_mcp import graph_migration
from test_v361_graph_migration import build_source, fake_embeddings, tree_state


@pytest.fixture
def activated(tmp_path, monkeypatch, fake_embeddings):
    home = tmp_path / "service"
    source = build_source(tmp_path / "old-store")
    control = tmp_path / "control"
    control.mkdir()
    destination = control / "migration"
    report = graph_migration.apply_migration(source=source, destination=destination)
    assert report["status"] == graph_migration.STATUS_APPLIED, report
    generation = destination / "current"
    pointer = control / "active.json"
    report = activate_generation(pointer=pointer, generation=generation, apply=True)
    assert report["status"] == STATUS_CUTOVER_APPLIED, report
    monkeypatch.setenv("LAYERED_MEMORY_HOME", str(home))
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_MODE", "graph")
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", str(pointer))
    return home, source, generation, pointer


def config_for(home, **kwargs):
    return MemoryConfig(home=str(home), agent_memory={"adapter": "generic"}, **kwargs)


def assert_bound(config, home, generation, pointer):
    assert isinstance(config, MemoryConfig)
    assert getattr(config, "graph_active_pointer", None) == pointer
    assert config.graph_db_path == generation / "graph.db"
    assert config.graph_journal_path == generation / "journal.db"
    assert config.knowledge_dir == generation / "knowledge"
    assert config.knowledge_dirs == [generation / "knowledge"]
    assert config.l0_index_file == generation / "L0.md"
    assert config.vector_db_path == generation / "data" / "vectors.db"
    assert config.home == home


def test_environment_binding_keeps_complete_settings_and_auxiliary_home(activated):
    home, source, generation, pointer = activated
    before = tree_state(source)
    config = config_for(home, dedup_threshold=0.43, auto_maintain=False)
    assert_bound(config, home, generation, pointer)
    assert config.dedup_threshold == 0.43
    assert config.auto_maintain is False
    assert not (home / "knowledge").exists()
    assert not (home / "data" / "vectors.db").exists()
    assert tree_state(source) == before


def test_constructor_environment_yaml_unset_precedence(activated, monkeypatch):
    home, _, generation, pointer = activated
    home.mkdir()
    (home / "config.yaml").write_text(
        f"graph:\n  mode: graph\n  active_pointer: {pointer}\n", encoding="utf-8"
    )
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER")
    assert_bound(config_for(home), home, generation, pointer)
    missing = pointer.parent / "missing.json"
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", str(missing))
    with pytest.raises(ValueError, match="graph_active_pointer"):
        config_for(home)
    assert "graph_active_pointer" in inspect.signature(MemoryConfig).parameters
    assert_bound(config_for(home, graph_active_pointer=str(pointer)), home, generation, pointer)
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", str(pointer))
    (home / "config.yaml").write_text(
        f"graph:\n  active_pointer: {missing}\n", encoding="utf-8"
    )
    assert_bound(config_for(home), home, generation, pointer)
    (home / "config.yaml").unlink()
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER")
    config = config_for(home, graph_mode="legacy")
    assert config.graph_active_pointer is None
    assert config.vector_db_path == home / "data" / "vectors.db"


@pytest.mark.parametrize("mode", ["legacy", "shadow", "graph_recall", "graph_write"])
def test_partial_or_non_authoritative_pointer_mode_is_refused(activated, mode):
    home, _, _, _ = activated
    before = tree_state(home.parent)
    with pytest.raises(ValueError, match="graph_active_pointer"):
        config_for(home, graph_mode=mode)
    assert tree_state(home.parent) == before


@pytest.mark.parametrize("mode", ["legacy", "shadow", "graph_recall", "graph_write", "graph"])
def test_unbound_direct_paths_and_legacy_vector_layout(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", raising=False)
    home = tmp_path / mode
    graph = tmp_path / "direct" / "graph.db"
    journal = tmp_path / "direct" / "journal.db"
    config = config_for(home, graph_mode=mode, graph_db_path=graph, graph_journal_path=journal)
    assert getattr(config, "graph_active_pointer", None) is None
    assert getattr(config, "vector_db_path", None) == home / "data" / "vectors.db"
    assert config.graph_db_path == graph
    assert config.graph_journal_path == journal
    assert not graph.parent.exists()
    assert DEFAULT_GRAPH_MODE == "graph"


@pytest.mark.parametrize("case", [
    "missing", "malformed", "unhealthy", "attestation", "missing_vector", "relative",
    "missing_graph", "missing_journal", "missing_knowledge", "missing_l0", "blank",
    "symlink", "incompatible_version",
    "graph_conflict", "journal_conflict", "knowledge_conflict", "l0_conflict",
    "namespace", "l0_format", "l0_tag",
])
def test_bad_binding_refuses_before_mkdir_store_or_model(activated, monkeypatch, case):
    from layered_memory_mcp.storage import vector_store

    home, _, generation, pointer = activated
    kwargs = {}
    if case == "missing":
        pointer.unlink()
    elif case == "malformed":
        pointer.write_text("not-json", encoding="utf-8")
    elif case == "unhealthy":
        with sqlite3.connect(generation / "graph.db") as connection:
            connection.execute("DELETE FROM graph_nodes")
    elif case == "attestation":
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        payload["manifest_sha256"] = "0" * 64
        pointer.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "missing_vector":
        (generation / "data" / "vectors.db").unlink()
    elif case in ("missing_graph", "missing_journal", "missing_l0"):
        relative = {"missing_graph": "graph.db", "missing_journal": "journal.db", "missing_l0": "L0.md"}[case]
        (generation / relative).unlink()
    elif case == "missing_knowledge":
        (generation / "knowledge").rename(generation / "retained-knowledge")
    elif case == "blank":
        monkeypatch.setenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", " ")
    elif case == "symlink":
        retained = pointer.with_name("retained.json")
        pointer.rename(retained)
        pointer.symlink_to(retained)
    elif case == "incompatible_version":
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        payload["pointer_version"] = 999
        pointer.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "relative":
        monkeypatch.setenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER", "active.json")
    elif case in ("graph_conflict", "journal_conflict", "knowledge_conflict", "l0_conflict"):
        field = {
            "graph_conflict": "graph_db_path", "journal_conflict": "graph_journal_path",
            "knowledge_conflict": "knowledge_dir", "l0_conflict": "l0_index_file",
        }[case]
        kwargs[field] = str(home / "conflicting")
    elif case == "namespace":
        kwargs["namespace"] = "isolated"
    elif case == "l0_format":
        kwargs["l0_format"] = "generic"
    elif case == "l0_tag":
        monkeypatch.setenv("LAYERED_MEMORY_L0_TAG", "[Custom]")
    before = tree_state(home.parent)

    def forbidden(*args, **kwargs):
        pytest.fail("binding validation reached mkdir/store/model construction")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(vector_store.VectorStore, "__init__", forbidden)
    monkeypatch.setattr(vector_store, "_get_embedder", forbidden)
    with pytest.raises(ValueError, match="graph_active_pointer"):
        config_for(home, **kwargs)
    assert tree_state(home.parent) == before


def test_matching_explicit_surfaces_are_accepted(activated):
    home, _, generation, pointer = activated
    config = config_for(
        home, graph_db_path=generation / "graph.db",
        graph_journal_path=generation / "journal.db",
        knowledge_dir=str(generation / "knowledge"), l0_index_file=str(generation / "L0.md"),
    )
    assert_bound(config, home, generation, pointer)


@pytest.mark.parametrize("origin", ["constructor", "environment", "yaml"])
@pytest.mark.parametrize("field,key", [("graph_db_path", "db_path"), ("graph_journal_path", "journal_path")])
def test_conflicting_targets_from_each_configuration_source_fail(activated, monkeypatch, origin, field, key):
    home, _, _, _ = activated
    conflict = home / "other.db"
    kwargs = {}
    if origin == "constructor":
        kwargs[field] = conflict
    elif origin == "environment":
        monkeypatch.setenv(f"LAYERED_MEMORY_{field.upper()}", str(conflict))
    else:
        home.mkdir()
        (home / "config.yaml").write_text(f"graph:\n  {key}: {conflict}\n", encoding="utf-8")
    before = tree_state(home.parent)
    with pytest.raises(ValueError, match="graph_active_pointer.*conflicts"):
        config_for(home, **kwargs)
    assert tree_state(home.parent) == before


@pytest.mark.parametrize("value", ["true", "7", "[]", "{}", "\"\""])
def test_malformed_yaml_pointer_is_not_ignored(activated, monkeypatch, value):
    home, _, _, _ = activated
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER")
    home.mkdir()
    (home / "config.yaml").write_text(f"graph:\n  active_pointer: {value}\n", encoding="utf-8")
    before = tree_state(home.parent)
    with pytest.raises(ValueError, match="graph_active_pointer"):
        config_for(home)
    assert tree_state(home.parent) == before


def test_the_rollout_default_is_authoritative_and_binds_an_explicit_pointer(
    activated, monkeypatch,
):
    """The forward counterpart of the old 'a pointer cannot flip the default'.

    That test existed because the default was ``legacy`` and a pointer alone
    must not have promoted the mode. The default is now ``graph``, so the
    contract to protect is the other direction: the default *is* authoritative,
    so an explicit pointer binds without any mode being named anywhere — and a
    pointer still does not conjure a generation when none is pointed at.
    """
    home, _, generation, pointer = activated
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_MODE")
    config = config_for(home)
    assert config.graph_mode == DEFAULT_GRAPH_MODE == "graph"
    assert_bound(config, home, generation, pointer)
    monkeypatch.delenv("LAYERED_MEMORY_GRAPH_ACTIVE_POINTER")
    unbound = config_for(home)
    assert unbound.graph_mode == "graph"
    assert unbound.graph_active_pointer is None
    assert unbound.vector_db_path == home / "data" / "vectors.db"
    assert unbound.graph_db_path is None


def test_an_explicit_legacy_rollback_still_refuses_a_pointer(activated, monkeypatch):
    """The rejection the default change must not quietly retire."""
    home, _, _, _ = activated
    monkeypatch.setenv("LAYERED_MEMORY_GRAPH_MODE", "legacy")
    before = tree_state(home.parent)
    with pytest.raises(ValueError, match="graph_active_pointer"):
        config_for(home)
    assert tree_state(home.parent) == before

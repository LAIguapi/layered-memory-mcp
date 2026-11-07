"""The service home's binding to an activated generation: prepare, then prove.

Activating a pointer changes a control file the service never reads. These
tests exist because "the generation is healthy and the pointer names it" was
being reported as operator success while a server launched against that home
would still have resolved the legacy store.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from layered_memory_mcp import graph_home_binding as binding
from layered_memory_mcp import graph_migration
from layered_memory_mcp.graph_generation_runtime import (
    STATUS_CUTOVER_APPLIED,
    activate_generation,
)
from test_v361_graph_migration import build_source, fake_embeddings, tree_state


@pytest.fixture
def activated(tmp_path, monkeypatch, fake_embeddings):
    """A published, activated generation plus an empty but existing home."""
    for name in binding.OVERRIDE_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "service"
    home.mkdir()
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
    return home, generation, pointer


def configuration(home: Path) -> Path:
    return home / binding.CONFIG_NAME


# ---------------------------------------------------------------------------
# plan: decides, writes nothing
# ---------------------------------------------------------------------------

def test_plan_reports_the_required_changes_without_touching_the_home(activated):
    home, generation, pointer = activated
    before = tree_state(home.parent)
    report = binding.plan_home_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_PREPARED
    assert report.changes == ["mode", "active_pointer"]
    assert report.generation == str(generation)
    assert report.writes is False
    assert report.ok
    assert not configuration(home).exists()
    assert tree_state(home.parent) == before


def test_plan_is_unchanged_once_the_home_already_selects_the_pointer(activated):
    home, _, pointer = activated
    assert binding.apply_home_binding(home=home, pointer=pointer).status == (
        binding.STATUS_APPLIED
    )
    before = tree_state(home)
    report = binding.plan_home_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_UNCHANGED
    assert report.changes == []
    assert report.writes is False
    assert tree_state(home) == before


@pytest.mark.parametrize("payload,finding", [
    ("graph:\n  mode: graph\ngraph:\n  mode: legacy\n", binding.FINDING_CONFIG_DUPLICATE_KEY),
    ("graph:\n  mode: graph\n  mode: legacy\n", binding.FINDING_CONFIG_DUPLICATE_KEY),
    ("graph: not-a-mapping\n", binding.FINDING_GRAPH_NOT_A_MAPPING),
    ("graph:\n  mode: graph\n  unknown_key: 1\n", binding.FINDING_GRAPH_UNKNOWN_KEY),
    ("- a\n- b\n", binding.FINDING_CONFIG_NOT_A_MAPPING),
    ("---\ngraph:\n  mode: graph\n", binding.FINDING_CONFIG_UNSUPPORTED_LAYOUT),
    ("{graph: {mode: graph}}\n", binding.FINDING_CONFIG_UNSUPPORTED_LAYOUT),
    ("graph:\n\tmode: graph\n", binding.FINDING_CONFIG_UNREADABLE),
    ("key: [unclosed\n", binding.FINDING_CONFIG_UNREADABLE),
])
def test_an_unrewritable_configuration_is_refused_without_writes(
    activated, payload, finding,
):
    home, _, pointer = activated
    configuration(home).write_text(payload, encoding="utf-8")
    before = tree_state(home)
    report = binding.plan_home_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_REFUSED
    assert any(item.startswith(finding) for item in report.findings), report.findings
    assert tree_state(home) == before
    # And apply refuses identically: there is no "try anyway" path.
    assert binding.apply_home_binding(home=home, pointer=pointer).status == (
        binding.STATUS_REFUSED
    )
    assert tree_state(home) == before


def test_a_conflicting_explicit_target_is_refused_rather_than_overwritten(activated):
    home, _, pointer = activated
    configuration(home).write_text(
        f"graph:\n  db_path: {home / 'other.db'}\n", encoding="utf-8"
    )
    before = tree_state(home)
    report = binding.plan_home_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_REFUSED
    assert f"{binding.FINDING_GRAPH_CONFLICTING_TARGET}:db_path" in report.findings
    assert tree_state(home) == before


def test_a_matching_explicit_target_is_retained(activated):
    home, generation, pointer = activated
    configuration(home).write_text(
        f"graph:\n  db_path: {generation / 'graph.db'}\n", encoding="utf-8"
    )
    report = binding.plan_home_binding(home=home, pointer=pointer)
    assert report.ok
    assert report.retained_keys == ["db_path"]
    applied = binding.apply_home_binding(home=home, pointer=pointer)
    assert applied.status == binding.STATUS_APPLIED
    parsed = yaml.safe_load(configuration(home).read_text(encoding="utf-8"))
    assert parsed["graph"] == {
        "mode": "graph",
        "db_path": str(generation / "graph.db"),
        "active_pointer": str(pointer),
    }


@pytest.mark.parametrize("kind", ["absent", "file", "symlink"])
def test_a_home_that_is_not_a_usable_directory_is_refused(activated, tmp_path, kind):
    _, _, pointer = activated
    home = tmp_path / f"home-{kind}"
    expected = binding.FINDING_HOME_ABSENT
    if kind == "file":
        home.write_text("neutral", encoding="utf-8")
        expected = binding.FINDING_HOME_NOT_A_DIRECTORY
    elif kind == "symlink":
        target = tmp_path / "elsewhere"
        target.mkdir()
        home.symlink_to(target, target_is_directory=True)
        expected = binding.FINDING_HOME_SYMLINK
    report = binding.plan_home_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_REFUSED
    assert expected in report.findings


@pytest.mark.parametrize("kind", ["absent", "corrupt", "symlink"])
def test_an_unresolvable_pointer_is_refused_before_the_home_is_rewritten(
    activated, tmp_path, kind,
):
    home, _, pointer = activated
    if kind == "absent":
        pointer.unlink()
    elif kind == "corrupt":
        pointer.write_text("not-json", encoding="utf-8")
    else:
        retained = pointer.with_name("retained.json")
        pointer.rename(retained)
        pointer.symlink_to(retained)
    before = tree_state(home)
    report = binding.apply_home_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_REFUSED
    assert report.findings
    assert tree_state(home) == before


def test_a_configuration_file_that_is_not_a_regular_file_is_refused(activated):
    home, _, pointer = activated
    configuration(home).mkdir()
    before = tree_state(home)
    report = binding.plan_home_binding(home=home, pointer=pointer)
    assert report.findings == [binding.FINDING_CONFIG_NOT_A_FILE]
    assert tree_state(home) == before


def test_a_symlinked_configuration_file_is_refused(activated, tmp_path):
    home, _, pointer = activated
    target = tmp_path / "elsewhere.yaml"
    target.write_text("dedup_threshold: 0.5\n", encoding="utf-8")
    configuration(home).symlink_to(target)
    report = binding.plan_home_binding(home=home, pointer=pointer)
    assert report.findings == [binding.FINDING_CONFIG_SYMLINK]
    assert target.read_text(encoding="utf-8") == "dedup_threshold: 0.5\n"


# ---------------------------------------------------------------------------
# apply: writes exactly the graph block
# ---------------------------------------------------------------------------

def test_apply_creates_a_configuration_when_the_home_has_none(activated):
    home, generation, pointer = activated
    report = binding.apply_home_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_APPLIED
    assert report.writes is True
    assert report.backup is None  # nothing existed, so nothing to retain
    assert configuration(home).read_text(encoding="utf-8") == (
        "graph:\n  mode: graph\n  active_pointer: " + str(pointer) + "\n"
    )
    assert report.generation == str(generation)


def test_apply_preserves_every_unrelated_line_byte_for_byte(activated):
    home, _, pointer = activated
    original = (
        "# operator header\n"
        "dedup_threshold: 0.42\n"
        "\n"
        "promotion:\n"
        "  watch_domains:\n"
        "    - misc\n"
        "    - infra\n"
        "\n"
        "# a note about the graph\n"
        "graph:\n"
        "  mode: legacy\n"
        "\n"
        "# a note about write_guard\n"
        "write_guard: warn\n"
    )
    configuration(home).write_text(original, encoding="utf-8")
    report = binding.apply_home_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_APPLIED
    updated = configuration(home).read_text(encoding="utf-8")
    assert "# operator header" in updated
    assert "dedup_threshold: 0.42" in updated
    assert "    - infra" in updated
    assert "# a note about the graph" in updated
    assert "# a note about write_guard" in updated
    assert "write_guard: warn" in updated
    assert "mode: legacy" not in updated
    parsed = yaml.safe_load(updated)
    assert parsed["dedup_threshold"] == 0.42
    assert parsed["promotion"] == {"watch_domains": ["misc", "infra"]}
    assert parsed["write_guard"] == "warn"
    assert parsed["graph"] == {"mode": "graph", "active_pointer": str(pointer)}
    assert Path(report.backup).read_text(encoding="utf-8") == original


def test_apply_appends_a_graph_section_when_none_exists(activated):
    home, _, pointer = activated
    configuration(home).write_text("session_scan:\n  enabled: true\n", encoding="utf-8")
    assert binding.apply_home_binding(home=home, pointer=pointer).ok
    parsed = yaml.safe_load(configuration(home).read_text(encoding="utf-8"))
    assert parsed["session_scan"] == {"enabled": True}
    assert parsed["graph"]["active_pointer"] == str(pointer)


def test_a_repeat_apply_writes_nothing_and_leaves_no_second_backup(activated):
    home, _, pointer = activated
    configuration(home).write_text("dedup_threshold: 0.42\n", encoding="utf-8")
    first = binding.apply_home_binding(home=home, pointer=pointer)
    assert first.status == binding.STATUS_APPLIED
    before = tree_state(home)
    second = binding.apply_home_binding(home=home, pointer=pointer)
    assert second.status == binding.STATUS_UNCHANGED
    assert second.writes is False
    assert second.backup is None
    assert tree_state(home) == before


def test_a_failed_write_reports_the_partial_outcome_and_keeps_the_original(
    activated, monkeypatch,
):
    home, _, pointer = activated
    original = "dedup_threshold: 0.42\n"
    configuration(home).write_text(original, encoding="utf-8")
    real_replace = os.replace

    def failing_replace(source, target, *args, **kwargs):
        raise OSError("neutral-private-detail")

    monkeypatch.setattr(os, "replace", failing_replace)
    report = binding.apply_home_binding(home=home, pointer=pointer)
    monkeypatch.setattr(os, "replace", real_replace)
    assert report.status == binding.STATUS_REFUSED
    assert any(
        item.startswith(binding.FINDING_WRITE_FAILED) for item in report.findings
    )
    assert "neutral-private-detail" not in str(report.to_payload())
    # The original is untouched, the retained copy is named, and the temporary
    # file is gone: a partial outcome is reported as one, not cleaned up silently.
    assert configuration(home).read_text(encoding="utf-8") == original
    assert report.writes is True
    assert Path(report.backup).read_text(encoding="utf-8") == original
    assert not list(home.glob(".config.yaml.binding-*"))


def test_a_textual_edit_that_would_change_anything_else_is_refused(
    activated, monkeypatch,
):
    """The parsed-result guard, not the careful intention, is the contract."""
    home, _, pointer = activated
    original = "dedup_threshold: 0.42\ngraph:\n  mode: legacy\n"
    configuration(home).write_text(original, encoding="utf-8")

    def sabotaged(section):
        return ["graph:", "  mode: graph", "dedup_threshold: 0.1"]

    monkeypatch.setattr(binding, "_render_section", sabotaged)
    report = binding.apply_home_binding(home=home, pointer=pointer)
    assert report.findings == [binding.FINDING_REWRITE_WOULD_CHANGE]
    assert configuration(home).read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# verify: the effective service configuration, not the adapter's projection
# ---------------------------------------------------------------------------

def test_verification_proves_the_effective_configuration_and_every_projection(
    activated,
):
    home, generation, pointer = activated
    assert binding.apply_home_binding(home=home, pointer=pointer).ok
    report = binding.verify_effective_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_VERIFIED
    assert report.effective == {
        "graph_mode": "graph",
        "graph_active_pointer": str(pointer),
        "home": str(home),
        "graph_db_path": str(generation / "graph.db"),
        "graph_journal_path": str(generation / "journal.db"),
        "knowledge_dir": str(generation / "knowledge"),
        "l0_index_file": str(generation / "L0.md"),
        "vector_db_path": str(generation / "data" / "vectors.db"),
    }


def test_verification_refuses_a_home_that_was_never_bound(activated):
    home, _, pointer = activated
    report = binding.verify_effective_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_REFUSED
    assert binding.FINDING_EFFECTIVE_POINTER in report.findings
    assert report.effective["graph_active_pointer"] is None


@pytest.mark.parametrize("name", binding.OVERRIDE_ENVIRONMENT)
def test_an_environment_override_blocks_the_proof_instead_of_being_ignored(
    activated, monkeypatch, name,
):
    home, _, pointer = activated
    assert binding.apply_home_binding(home=home, pointer=pointer).ok
    monkeypatch.setenv(name, str(pointer))
    report = binding.verify_effective_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_REFUSED
    assert f"{binding.FINDING_ENVIRONMENT_OVERRIDE}:{name}" in report.findings
    assert report.effective == {}


def test_a_legacy_mode_in_the_home_is_reported_as_an_effective_mismatch(
    activated, monkeypatch,
):
    """The config file wins over this tool's intention, and must be checked."""
    home, _, pointer = activated
    assert binding.apply_home_binding(home=home, pointer=pointer).ok
    # Hand-edited after the binding: the pointer line stays, the mode does not.
    text = configuration(home).read_text(encoding="utf-8")
    configuration(home).write_text(
        text.replace("mode: graph", "mode: legacy"), encoding="utf-8"
    )
    report = binding.verify_effective_binding(home=home, pointer=pointer)
    assert report.status == binding.STATUS_REFUSED
    # A legacy mode with a pointer is refused by the complete config itself, so
    # the proof reports a rejected configuration rather than inventing a mode.
    assert any(
        item.startswith(binding.FINDING_EFFECTIVE_UNUSABLE)
        or item.startswith(binding.FINDING_EFFECTIVE_MODE)
        for item in report.findings
    ), report.findings


# ---------------------------------------------------------------------------
# the pointer-independent preflight
# ---------------------------------------------------------------------------

def test_the_pointer_independent_preflight_still_catches_a_bad_configuration(
    activated,
):
    home, _, _ = activated
    assert binding.plan_home_config(home=home).ok
    assert binding.plan_home_config(home=home).warnings == ["config_absent"]
    configuration(home).write_text(
        "graph:\n  mode: graph\ngraph:\n  mode: legacy\n", encoding="utf-8"
    )
    report = binding.plan_home_config(home=home)
    assert report.status == binding.STATUS_REFUSED
    assert binding.FINDING_CONFIG_DUPLICATE_KEY in report.findings

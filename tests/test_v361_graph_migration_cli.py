"""v3.6.1 — the `graph_migration` operator CLI, exercised as a real process.

Why a subprocess suite: an operator tool's contract includes the things an
in-process call cannot show — that ``python -m
layered_memory_mcp.graph_migration`` is runnable, that the normal report is
*only* JSON on stdout, that ``--source``/``--destination`` are required rather
than discovered, that a forgotten verb is a read-only dry run rather than a
publication, and that the exit code distinguishes success from a refusal.

The old ``domain_keywords`` migration CLI is a separate entry point and is
checked here to still be itself.

Isolation: the fixture snapshot is built in-process (with the deterministic
embedding function), and every subprocess is given explicit ``--source`` /
``--destination`` paths under ``tmp_path``, a redirected ``HOME`` and the
working tree on ``PYTHONPATH`` — never an installed snapshot of the package.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from layered_memory_mcp import graph_migration

#: The fixture builders live beside the behavioural suite rather than being
#: restated here: two spellings of "a genuine old-format snapshot" is how the
#: in-process and the subprocess suites would start testing different inputs.
#: Imported by module name — ``tests/`` is not a package, and pytest's default
#: import mode puts the test directory on ``sys.path``.
from test_v361_graph_migration import (  # noqa: F401 — fixtures are reused
    DUPLICATE_LABEL_EMPTY_FIRST_MD,
    build_source,
    build_source_with_document,
    fake_embeddings,
    generation,
    tree_state,
)

MODULE = "layered_memory_mcp.graph_migration"
SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def source(tmp_path) -> Path:
    return build_source(tmp_path / "old-store")


@pytest.fixture
def destination(tmp_path) -> Path:
    return tmp_path / "migrated"


def run_cli(*args: str, home: Path, expect: int | None = None):
    """Run the module as a process and return ``(returncode, stdout, stderr)``."""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("LAYERED_MEMORY_GRAPH_DB_PATH", None)
    env.pop("LAYERED_MEMORY_GRAPH_JOURNAL_PATH", None)
    completed = subprocess.run(
        [sys.executable, "-m", MODULE, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    if expect is not None:
        assert completed.returncode == expect, completed.stderr
    return completed


def json_report(completed) -> dict:
    """The parsed stdout report. Fails loudly if stdout is not pure JSON."""
    return json.loads(completed.stdout)


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "fake-home"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Usage and help
# ---------------------------------------------------------------------------

def test_top_level_help_names_every_verb(home):
    completed = run_cli("--help", home=home, expect=graph_migration.EXIT_OK)

    for verb in ("plan", "apply", "verify", "rollback"):
        assert verb in completed.stdout


@pytest.mark.parametrize("verb", ["plan", "apply", "verify", "rollback"])
def test_each_verb_has_help_and_documents_its_explicit_inputs(verb, home):
    completed = run_cli(verb, "--help", home=home, expect=graph_migration.EXIT_OK)

    assert "--destination" in completed.stdout
    if verb != "rollback":
        assert "--source" in completed.stdout


def test_a_missing_destination_is_a_usage_error_and_writes_nothing(source, home):
    completed = run_cli(
        "apply", "--source", str(source), home=home, expect=graph_migration.EXIT_USAGE
    )

    assert completed.stdout == ""
    assert "--destination" in completed.stderr


def test_apply_without_a_source_is_a_usage_error(destination, home):
    completed = run_cli(
        "apply",
        "--destination",
        str(destination),
        home=home,
        expect=graph_migration.EXIT_USAGE,
    )

    assert not destination.exists()
    assert "--source" in completed.stderr


# ---------------------------------------------------------------------------
# Dry run is the default
# ---------------------------------------------------------------------------

def test_an_omitted_verb_is_a_read_only_dry_run(source, destination, home):
    before = tree_state(source)

    completed = run_cli(
        "--source",
        str(source),
        "--destination",
        str(destination),
        home=home,
        expect=graph_migration.EXIT_OK,
    )

    report = json_report(completed)
    assert report["command"] == "plan"
    assert report["dry_run"] is True
    assert report["status"] == graph_migration.STATUS_PLANNED
    assert not destination.exists()
    assert tree_state(source) == before


def test_plan_prints_only_json_on_stdout(source, destination, home):
    completed = run_cli(
        "plan",
        "--source",
        str(source),
        "--destination",
        str(destination),
        home=home,
        expect=graph_migration.EXIT_OK,
    )

    report = json_report(completed)
    assert report["tool"] == MODULE
    assert report["inventory"]["counts_balance"] is True


def test_apply_dry_run_flag_writes_nothing(source, destination, home):
    completed = run_cli(
        "apply",
        "--source",
        str(source),
        "--destination",
        str(destination),
        "--dry-run",
        home=home,
        expect=graph_migration.EXIT_OK,
    )

    assert json_report(completed)["dry_run"] is True
    assert not destination.exists()


# ---------------------------------------------------------------------------
# apply → verify → rollback, as a process
# ---------------------------------------------------------------------------

def test_apply_then_verify_then_rollback_through_the_cli(source, destination, home):
    applied = json_report(
        run_cli(
            "apply",
            "--source",
            str(source),
            "--destination",
            str(destination),
            home=home,
            expect=graph_migration.EXIT_OK,
        )
    )
    assert applied["status"] == graph_migration.STATUS_APPLIED
    assert generation(destination).is_dir()

    verified = json_report(
        run_cli(
            "verify",
            "--source",
            str(source),
            "--destination",
            str(destination),
            home=home,
            expect=graph_migration.EXIT_OK,
        )
    )
    assert verified["status"] == graph_migration.STATUS_VERIFIED

    rolled = json_report(
        run_cli(
            "rollback",
            "--destination",
            str(destination),
            home=home,
            expect=graph_migration.EXIT_OK,
        )
    )
    assert rolled["status"] == graph_migration.STATUS_ROLLED_BACK
    assert not generation(destination).exists()
    assert Path(rolled["generation"]["withdrawn_to"]).is_dir()


def test_a_second_apply_reports_already_published_and_exits_zero(
    source, destination, home
):
    run_cli(
        "apply",
        "--source",
        str(source),
        "--destination",
        str(destination),
        home=home,
        expect=graph_migration.EXIT_OK,
    )
    before = tree_state(destination)

    completed = run_cli(
        "apply",
        "--source",
        str(source),
        "--destination",
        str(destination),
        home=home,
        expect=graph_migration.EXIT_OK,
    )

    assert (
        json_report(completed)["status"] == graph_migration.STATUS_ALREADY_PUBLISHED
    )
    assert tree_state(destination) == before


def test_a_blocked_plan_exits_nonzero_with_a_json_report(tmp_path, destination, home):
    completed = run_cli(
        "plan",
        "--source",
        str(tmp_path / "nowhere"),
        "--destination",
        str(destination),
        home=home,
        expect=graph_migration.EXIT_BLOCKED,
    )

    report = json_report(completed)
    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert "source_missing" in report["findings"]


def test_an_incompatible_document_blocks_the_cli_and_publishes_nothing(
    tmp_path, destination, home
):
    """The section-reader preflight refuses through the real process too."""
    root = build_source_with_document(
        tmp_path / "old-store", DUPLICATE_LABEL_EMPTY_FIRST_MD
    )

    completed = run_cli(
        "apply",
        "--source",
        str(root),
        "--destination",
        str(destination),
        home=home,
        expect=graph_migration.EXIT_BLOCKED,
    )

    report = json_report(completed)
    assert report["status"] == graph_migration.STATUS_BLOCKED
    assert (
        "l1_section_reader_incompatible:retrieval:canonical_body_differs"
        in report["findings"]
    )
    assert not destination.exists()
    assert "kept" not in completed.stdout


def test_verify_of_an_absent_generation_exits_nonzero(destination, home):
    completed = run_cli(
        "verify",
        "--destination",
        str(destination),
        home=home,
        expect=graph_migration.EXIT_BLOCKED,
    )

    assert json_report(completed)["status"] == graph_migration.STATUS_VERIFY_FAILED


# ---------------------------------------------------------------------------
# The old migration CLI is untouched
# ---------------------------------------------------------------------------

def test_the_domain_keywords_migration_cli_is_still_its_own_tool(home):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(home)
    completed = subprocess.run(
        [sys.executable, "-m", "layered_memory_mcp.migrate", "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert completed.returncode == 0
    assert "domain_keywords" in completed.stdout
    assert "--destination" not in completed.stdout

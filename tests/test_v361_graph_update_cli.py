"""Default graph update preview is JSON and leaves explicit paths untouched."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path


def tree_state(root: Path) -> dict:
    return {
        str(path.relative_to(root)): (
            path.stat().st_mtime_ns,
            None if path.is_dir() else path.read_bytes(),
        )
        for path in [root, *sorted(root.rglob("*"))]
    }


def neutral_source(root: Path) -> Path:
    from layered_memory_mcp.graph_entry_identity import markdown_section_entry_id
    from layered_memory_mcp.models import KnowledgeEntry
    from layered_memory_mcp.storage.vector_store import EMBED_DIM, entry_embedding_text

    entry = KnowledgeEntry(
        id=markdown_section_entry_id("neutral", "Sample", 0),
        domain="neutral",
        section="Sample",
        content="A neutral fixture for graph update.",
    )
    (root / "knowledge").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "knowledge" / "neutral.md").write_text(
        f"# Neutral fixture\n\n## {entry.section}\n\n{entry.content}\n",
        encoding="utf-8",
    )
    with closing(sqlite3.connect(root / "data" / "vectors.db")) as database:
        database.execute(
            "CREATE TABLE vectors (id TEXT PRIMARY KEY, domain TEXT NOT NULL, "
            "text TEXT NOT NULL, vector TEXT NOT NULL, metadata TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        database.execute(
            "INSERT INTO vectors (id, domain, text, vector, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.domain,
                entry_embedding_text(entry),
                json.dumps([1.0] + [0.0] * (EMBED_DIM - 1)),
                json.dumps({"section": entry.section}),
            ),
        )
        database.commit()
    return root


def update_environment(home: Path, temporary: Path, application: Path) -> dict[str, str]:
    temporary.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CONFIG_HOME": str(application / "config"),
        "XDG_CACHE_HOME": str(application / "cache"),
        "XDG_DATA_HOME": str(application / "data"),
        "XDG_STATE_HOME": str(application / "state"),
        "APPDATA": str(application / "appdata"),
        "LOCALAPPDATA": str(application / "localappdata"),
        "HERMES_HOME": str(application / "hermes"),
        "LAYERED_MEMORY_HOME": str(home),
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }


def run_update(
    tmp_path: Path,
    environment: dict[str, str],
    home: Path,
    source: Path,
    destination: Path,
    pointer: Path,
    *extra: str,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "layered_memory_mcp.graph_update",
            "--home",
            str(home),
            "--source",
            str(source),
            "--destination",
            str(destination),
            "--pointer",
            str(pointer),
            *extra,
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_default_preview_is_json_and_writes_nothing(tmp_path, monkeypatch):
    home = tmp_path / "update-home"
    source = tmp_path / "source"
    control = tmp_path / "control"
    control.mkdir()
    destination = control / "destination"
    pointer = control / "active.json"
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    application = tmp_path / "application"
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CONFIG_HOME": str(application / "config"),
        "XDG_CACHE_HOME": str(application / "cache"),
        "XDG_DATA_HOME": str(application / "data"),
        "XDG_STATE_HOME": str(application / "state"),
        "APPDATA": str(application / "appdata"),
        "LOCALAPPDATA": str(application / "localappdata"),
        "HERMES_HOME": str(application / "hermes"),
        "LAYERED_MEMORY_HOME": str(home),
        "LAYERED_MEMORY_AGENT_MEMORY_PATH": str(application / "agent.md"),
        "LAYERED_MEMORY_L0_INDEX_FILE": str(application / "index.md"),
        "LAYERED_MEMORY_SESSIONS_DIR": str(application / "sessions"),
        "LAYERED_MEMORY_COMPACT_DOMAIN_RULES_FILE": str(application / "rules.yaml"),
        "LAYERED_MEMORY_HERMES_DB": str(application / "sessions.db"),
        "LAYERED_MEMORY_GRAPH_DB_PATH": str(application / "graph.db"),
        "LAYERED_MEMORY_GRAPH_JOURNAL_PATH": str(application / "journal.jsonl"),
        "LAYERED_MEMORY_MODEL_DIR": str(application / "models"),
        "HF_HOME": str(application / "huggingface"),
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    from layered_memory_mcp.graph_entry_identity import markdown_section_entry_id
    from layered_memory_mcp.graph_migration import plan_migration
    from layered_memory_mcp.models import KnowledgeEntry
    from layered_memory_mcp.storage.vector_store import EMBED_DIM, entry_embedding_text

    entry = KnowledgeEntry(
        id=markdown_section_entry_id("neutral", "Sample", 0),
        domain="neutral",
        section="Sample",
        content="A neutral fixture for preview.",
    )
    (source / "knowledge").mkdir(parents=True)
    (source / "data").mkdir()
    (source / "knowledge" / "neutral.md").write_text(
        f"# Neutral fixture\n\n## {entry.section}\n\n{entry.content}\n",
        encoding="utf-8",
    )
    with closing(sqlite3.connect(source / "data" / "vectors.db")) as database:
        database.execute(
            "CREATE TABLE vectors (id TEXT PRIMARY KEY, domain TEXT NOT NULL, "
            "text TEXT NOT NULL, vector TEXT NOT NULL, metadata TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        database.execute(
            "INSERT INTO vectors (id, domain, text, vector, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.domain,
                entry_embedding_text(entry),
                json.dumps([1.0] + [0.0] * (EMBED_DIM - 1)),
                json.dumps({"section": entry.section}),
            ),
        )
        database.commit()

    # The home must exist: this entry point prepares that home's *binding*, and
    # a binding cannot be prepared — or previewed — in a home nobody installed.
    home.mkdir()
    (home / "config.yaml").write_text("dedup_threshold: 0.9\n", encoding="utf-8")
    before = tree_state(source)
    control_before = tree_state(control)
    home_before = tree_state(home)
    plan = plan_migration(source=source, destination=destination)
    assert plan["status"] == "migration_planned", plan["findings"]
    assert tree_state(source) == before
    assert not destination.exists()
    assert not pointer.exists()

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "layered_memory_mcp.graph_update",
            "--home",
            str(home),
            "--source",
            str(source),
            "--destination",
            str(destination),
            "--pointer",
            str(pointer),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert tree_state(source) == before
    assert tree_state(control) == control_before
    assert tree_state(home) == home_before
    assert not destination.exists()
    assert not pointer.exists()
    assert list(control.iterdir()) == []
    assert not application.exists()
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert isinstance(report, dict)
    assert report["dry_run"] is True
    assert report["migration"]["status"] == "migration_planned"
    assert report["preflight"]["binding"]["status"] == "binding_preflighted"
    assert report["preflight"]["status"]["status"] == (
        "generation_runtime_pointer_absent"
    )
    # The pointer is not activated yet, so only the pointer-independent half of
    # the binding can be planned — and it says so rather than claiming success.
    assert report["binding"]["preparation"]["status"] == (
        "home_binding_pending_activation"
    )
    assert report["binding"]["preparation"]["writes"] is False
    assert report["effective_binding"] == {}


def test_apply_refuses_destination_outside_control_before_publication(tmp_path):
    home = tmp_path / "update-home"
    source = neutral_source(tmp_path / "source")
    control = tmp_path / "control"
    control.mkdir()
    destination = tmp_path / "outside-destination"
    pointer = control / "active.json"
    environment = update_environment(home, tmp_path / "temporary", tmp_path / "application")
    before = tree_state(control)

    completed = run_update(
        tmp_path,
        environment,
        home,
        source,
        destination,
        pointer,
        "--apply",
    )

    assert completed.returncode == 1, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "graph_update_refused"
    assert any(finding.startswith("generation_outside_control_root:") for finding in report["findings"])
    assert not destination.exists()
    assert not pointer.exists()
    assert tree_state(control) == before


def test_apply_refuses_corrupt_pointer_without_publication(tmp_path):
    home = tmp_path / "update-home"
    source = neutral_source(tmp_path / "source")
    control = tmp_path / "control"
    control.mkdir()
    destination = control / "destination"
    pointer = control / "active.json"
    pointer.write_text("not-json\n", encoding="utf-8")
    environment = update_environment(home, tmp_path / "temporary", tmp_path / "application")
    before = tree_state(control)

    completed = run_update(
        tmp_path,
        environment,
        home,
        source,
        destination,
        pointer,
        "--apply",
    )

    assert completed.returncode == 1, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "graph_update_refused"
    assert any("pointer" in finding for finding in report["findings"])
    assert pointer.read_text(encoding="utf-8") == "not-json\n"
    assert not destination.exists()
    assert tree_state(control) == before


def test_apply_refuses_symlink_destination_without_publication(tmp_path):
    if not hasattr(os, "symlink"):
        return
    home = tmp_path / "update-home"
    source = neutral_source(tmp_path / "source")
    control = tmp_path / "control"
    control.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    destination = control / "destination"
    destination.symlink_to(external, target_is_directory=True)
    pointer = control / "active.json"
    environment = update_environment(home, tmp_path / "temporary", tmp_path / "application")
    before = tree_state(control)
    external_before = tree_state(external)

    completed = run_update(
        tmp_path,
        environment,
        home,
        source,
        destination,
        pointer,
        "--apply",
    )

    assert completed.returncode == 1, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "graph_update_refused"
    assert any("symlink" in finding for finding in report["findings"])
    assert tree_state(control) == before
    assert tree_state(external) == external_before
    assert not pointer.exists()


def test_apply_publishes_verifies_and_activates_exact_generation(tmp_path):
    home = tmp_path / "update-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "# operator note\ndedup_threshold: 0.9\nauto_maintain: false\n",
        encoding="utf-8",
    )
    source = neutral_source(tmp_path / "source")
    control = tmp_path / "control"
    control.mkdir()
    destination = control / "destination"
    pointer = control / "active.json"
    environment = update_environment(home, tmp_path / "temporary", tmp_path / "application")

    completed = run_update(
        tmp_path,
        environment,
        home,
        source,
        destination,
        pointer,
        "--apply",
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "graph_update_applied"
    assert report["migration"]["status"] == "migration_applied"
    assert report["verification"]["status"] == "migration_verified"
    assert report["activation"]["status"] == "generation_cutover_applied"
    assert report["status_preflight"]["status"] == "generation_runtime_active"
    assert report["status_preflight"]["resolution"]["generation"] == str(
        destination / "current"
    )
    assert destination.joinpath("current").is_dir()
    assert pointer.is_file()

    # An activated pointer the service cannot see is not an applied update: the
    # home's configuration must now select this exact pointer, the unrelated
    # settings must survive, and the previous bytes must be recoverable.
    preparation = report["binding"]["preparation"]
    assert preparation["status"] == "home_binding_applied"
    assert preparation["changes"] == ["active_pointer", "mode"]
    assert preparation["writes"] is True
    configuration = (home / "config.yaml").read_text(encoding="utf-8")
    assert "# operator note" in configuration
    assert "dedup_threshold: 0.9" in configuration
    assert "auto_maintain: false" in configuration
    assert f"active_pointer: {pointer}" in configuration
    assert "mode: graph" in configuration
    recoverable = Path(preparation["backup"])
    assert recoverable.read_text(encoding="utf-8") == (
        "# operator note\ndedup_threshold: 0.9\nauto_maintain: false\n"
    )
    effective = report["effective_binding"]
    assert effective["status"] == "home_binding_verified"
    assert effective["effective"]["graph_mode"] == "graph"
    assert effective["effective"]["graph_active_pointer"] == str(pointer)
    generation = destination / "current"
    assert effective["effective"]["graph_db_path"] == str(generation / "graph.db")
    assert effective["effective"]["graph_journal_path"] == str(generation / "journal.db")
    assert effective["effective"]["knowledge_dir"] == str(generation / "knowledge")
    assert effective["effective"]["l0_index_file"] == str(generation / "L0.md")
    assert effective["effective"]["vector_db_path"] == str(
        generation / "data" / "vectors.db"
    )


def test_apply_refuses_a_home_whose_graph_configuration_cannot_be_rewritten(tmp_path):
    """A config this tool cannot safely edit stops the run before publication."""
    home = tmp_path / "update-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "graph:\n  mode: legacy\ngraph:\n  mode: shadow\n", encoding="utf-8"
    )
    source = neutral_source(tmp_path / "source")
    control = tmp_path / "control"
    control.mkdir()
    destination = control / "destination"
    pointer = control / "active.json"
    environment = update_environment(home, tmp_path / "temporary", tmp_path / "application")
    home_before = tree_state(home)
    control_before = tree_state(control)

    completed = run_update(
        tmp_path, environment, home, source, destination, pointer, "--apply",
    )

    assert completed.returncode == 1, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "graph_update_refused"
    assert "home_binding_failed" in report["findings"]
    assert any("duplicate_key" in finding for finding in report["findings"])
    assert not destination.exists()
    assert not pointer.exists()
    assert tree_state(home) == home_before
    assert tree_state(control) == control_before


def test_repeat_apply_is_zero_write_after_served_generation_changes(tmp_path):
    home = tmp_path / "update-home"
    home.mkdir()
    source = neutral_source(tmp_path / "source")
    control = tmp_path / "control"
    control.mkdir()
    destination = control / "destination"
    pointer = control / "active.json"
    environment = update_environment(home, tmp_path / "temporary", tmp_path / "application")

    first = run_update(
        tmp_path,
        environment,
        home,
        source,
        destination,
        pointer,
        "--apply",
    )
    assert first.returncode == 0, first.stderr

    served_surface = destination / "current" / "L0.md"
    served_surface.write_text(
        served_surface.read_text(encoding="utf-8") + "\n# Served write\n",
        encoding="utf-8",
    )
    destination_before = tree_state(destination)
    control_before = tree_state(control)
    home_before = tree_state(home)

    repeated = run_update(
        tmp_path,
        environment,
        home,
        source,
        destination,
        pointer,
        "--apply",
    )

    assert repeated.returncode == 0, repeated.stderr
    report = json.loads(repeated.stdout)
    assert report["status"] == "graph_update_unchanged"
    assert report["activation"]["status"] == "generation_cutover_unchanged"
    assert tree_state(destination) == destination_before
    assert tree_state(control) == control_before
    # The binding is still *proved* on a repeat — and because it is already
    # correct, proving it rewrites nothing, including the config's own bytes.
    assert report["binding"]["preparation"]["status"] == "home_binding_unchanged"
    assert report["binding"]["preparation"]["writes"] is False
    assert report["effective_binding"]["status"] == "home_binding_verified"
    assert tree_state(home) == home_before


def test_repeat_apply_refuses_source_mismatch_without_touching_active_target(tmp_path):
    home = tmp_path / "update-home"
    home.mkdir()
    source = neutral_source(tmp_path / "source")
    control = tmp_path / "control"
    control.mkdir()
    destination = control / "destination"
    pointer = control / "active.json"
    environment = update_environment(home, tmp_path / "temporary", tmp_path / "application")

    first = run_update(
        tmp_path,
        environment,
        home,
        source,
        destination,
        pointer,
        "--apply",
    )
    assert first.returncode == 0, first.stderr

    (source / "knowledge" / "neutral.md").write_text(
        "# Neutral fixture\n\n## Sample\n\nChanged source.\n",
        encoding="utf-8",
    )
    destination_before = tree_state(destination)
    control_before = tree_state(control)
    pointer_before = pointer.read_bytes()

    repeated = run_update(
        tmp_path,
        environment,
        home,
        source,
        destination,
        pointer,
        "--apply",
    )

    assert repeated.returncode == 1, repeated.stderr
    report = json.loads(repeated.stdout)
    assert report["status"] == "graph_update_refused"
    assert "source_provenance_unproved" in report["findings"]
    assert tree_state(destination) == destination_before
    assert tree_state(control) == control_before
    assert pointer.read_bytes() == pointer_before

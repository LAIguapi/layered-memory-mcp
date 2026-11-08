"""Synthetic privacy regressions; no models, host data or MCP connections."""

import io
import json
import os
import socket
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from layered_memory_mcp import server
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.recall import recall
from layered_memory_mcp.session_scanner import read_state_db_sessions, scan_sessions
from layered_memory_mcp.storage.access_log import AccessLogStore
from layered_memory_mcp.storage.l1_store import L1Store, read_knowledge_file
from layered_memory_mcp.storage import vector_store


def session_db(tmp_path, sessions):
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE sessions (id TEXT, title TEXT, source TEXT, started_at REAL, "
            "last_activity_at REAL, message_count INTEGER);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, tool_calls TEXT);"
        )
        for index, (source, content) in enumerate(sessions):
            connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                               (str(index), "neutral", source, time.time(), time.time(), 1))
            connection.execute("INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                               (str(index), "system", content))
    return database


def session_file(directory, name, source="interactive", marker=""):
    directory.mkdir(exist_ok=True)
    messages = [{"role": "user", "content": "neutral " * 20} for _ in range(60)]
    messages[30] = {"role": "system", "content": "neutral " * 100 + marker}
    path = directory / name
    path.write_text(json.dumps({"source": source, "title": "neutral", "messages": messages}))
    return path


@pytest.mark.parametrize("fallback", [False, True])
def test_session_directory_filters_raw_messages_before_limit(tmp_path, fallback):
    directory = tmp_path / "sessions"
    session_file(directory, "allowed.json")
    session_file(directory, "marker.json", marker="PRIVATE-SYNTHETIC")
    session_file(directory, "scheduled.json", source="cron")
    database = session_db(tmp_path, [("cron", "PRIVATE-SYNTHETIC")])
    result = scan_sessions(str(directory), max_sessions=1, hermes_db_path=database,
                           prefer_state_db=fallback, exclude_markers=["private-synthetic"])
    assert [Path(item["path"]).name for item in result["sessions"]] == ["allowed.json"]
    assert result["total_sessions"] == 3
    assert result["stats"]["scanned"] == 1
    assert result["stats"]["excluded_sessions"] == 1
    assert result["stats"]["skipped_sources"] == 1


def test_db_counts_and_source_metadata_marker(tmp_path):
    database = session_db(tmp_path, [("interactive", "neutral"),
                                    ("private-synthetic", "neutral"),
                                    ("cron", "neutral"), ("interactive", "neutral")])
    result = read_state_db_sessions(database, max_sessions=1, exclude_markers=["private-synthetic"])
    assert result["total_sessions"] == 4
    assert result["stats"]["scanned"] == 1
    assert result["stats"]["excluded_sessions"] == 1
    assert result["stats"]["skipped_sources"] == 1


@pytest.mark.parametrize("optional_calls", [False, True])
def test_db_full_raw_fields_and_optional_schema(tmp_path, optional_calls):
    from layered_memory_mcp.session_scanner import search_state_db_sessions

    database = session_db(tmp_path, [("interactive", "NEUTRAL-QUERY ALLOWED"),
                                     ("interactive", "NEUTRAL-QUERY AXB"),
                                     ("interactive", "NEUTRAL-QUERY A_B")])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE messages SET role='user'")
        if not optional_calls:
            connection.execute("ALTER TABLE messages DROP COLUMN tool_calls")
        else:
            connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                               ("late", "neutral", "interactive", time.time(), time.time(), 61))
            for position in range(61):
                connection.execute("INSERT INTO messages(session_id, role, content, tool_calls) VALUES (?, ?, ?, ?)",
                                   ("late", "user", "NEUTRAL-QUERY HIDDEN", json.dumps({
                                       "argument": '中性\n"限制' if position == 30 else "neutral"})))
    options = {"exclude_markers": ["A_B", '中性\n"限制'], "exclude_sources": []}
    result = read_state_db_sessions(database, **options)
    assert {row["session_id"] for row in result["sessions"]} == {"0", "1"}
    assert all(row["message_count"] == 1 for row in result["sessions"])
    assert "ALLOWED" in json.dumps(result)
    search = search_state_db_sessions("NEUTRAL-QUERY", database, **options)
    assert {row["session_id"] for row in search["matches"]} == {"0", "1"}


@pytest.mark.parametrize("database_kind", ["missing", "empty"])
def test_missing_or_empty_db_keeps_neutral_fallback(tmp_path, database_kind):
    directory = tmp_path / "sessions"
    session_file(directory, "neutral.json")
    database = session_db(tmp_path, []) if database_kind == "empty" else tmp_path / "absent.db"
    assert len(scan_sessions(str(directory), hermes_db_path=database)["sessions"]) == 1


@pytest.mark.parametrize("nested_identity", [False, True])
def test_filtered_db_identity_cannot_return_through_directory_alias(tmp_path, nested_identity):
    database = session_db(tmp_path, [("interactive", "PRIVATE-SYNTHETIC")])
    directory = tmp_path / "sessions"
    session_file(directory, "allowed.json")
    blocked = session_file(directory, "different-name.json")
    payload = json.loads(blocked.read_text())
    payload.update({"session": {"id": "0"}} if nested_identity else {"session_id": "0"})
    blocked.write_text(json.dumps(payload))
    result = scan_sessions(str(directory), max_sessions=1, hermes_db_path=database,
                           exclude_markers=["PRIVATE-SYNTHETIC"])
    assert [Path(row["path"]).name for row in result["sessions"]] == ["allowed.json"]
    assert result["total_sessions"] == 2
    assert result["stats"]["scanned"] == result["stats"]["excluded_sessions"] == 1


@pytest.mark.parametrize("opt_in", [False, True])
def test_query_persistence_requires_explicit_opt_in(tmp_path, opt_in):
    database = tmp_path / "access.db"
    store = AccessLogStore(database, **({"log_query": True} if opt_in else {}))
    store.record("neutral", "recall", query="synthetic-query", rank=1, score=0.5)
    store.record_batch([{"domain": "neutral", "access_type": "recall", "query": "synthetic-query",
                         "rank": 2, "score": 0.8}])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT query FROM access_log").fetchall() == [
            ("synthetic-query" if opt_in else None,)] * 2
    assert store.overview()["total_events"] == 2
    assert store.domain_stats("neutral")["total_hits"] == 2


def test_query_config_and_server_fallback_default_off(tmp_path, monkeypatch):
    monkeypatch.delenv("LAYERED_MEMORY_LOG_QUERY", raising=False)
    config = MemoryConfig(home=tmp_path)
    assert config.access_log_query is False
    monkeypatch.setattr(server, "_access_log_store", None)
    monkeypatch.setattr(server, "_get_config", lambda: SimpleNamespace(
        home=tmp_path, access_log_enabled=True, access_log_retention_days=90))
    store = server._get_access_log()
    assert store.log_query is False


@pytest.mark.parametrize("configured, expected", [("true", True), ("false", False), ("invalid", False)])
def test_query_config_parsing_and_environment_precedence(tmp_path, monkeypatch, configured, expected):
    (tmp_path / "config.yaml").write_text(f"access_log:\n  log_query: {configured}\n")
    monkeypatch.delenv("LAYERED_MEMORY_LOG_QUERY", raising=False)
    assert MemoryConfig(home=tmp_path).access_log_query is expected
    monkeypatch.setenv("LAYERED_MEMORY_LOG_QUERY", "false")
    assert MemoryConfig(home=tmp_path).access_log_query is False
    monkeypatch.setenv("LAYERED_MEMORY_LOG_QUERY", "true")
    assert MemoryConfig(home=tmp_path).access_log_query is True


@pytest.mark.parametrize("kind", ["outside", "inside", "ancestor"])
def test_knowledge_readers_refuse_links(tmp_path, kind):
    root = tmp_path / "knowledge"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("## Neutral\nsynthetic-link-content")
    normal = root / "normal.md"
    normal.write_text("## Neutral\nsynthetic-link-content")
    if kind == "ancestor":
        linked = root / "nested"
        linked.symlink_to(tmp_path, target_is_directory=True)
        target = linked / "outside.md"
    else:
        target = root / "linked.md"
        target.symlink_to(outside if kind == "outside" else normal)
    assert read_knowledge_file(target) == (None, "")
    if kind != "ancestor":
        assert L1Store(root).read("linked") == (None, "")
    results = recall("synthetic-link-content", str(root))["results"]
    assert all(item["file"] == "normal.md" for item in results)


def test_regular_unicode_nested_knowledge_is_readable(tmp_path):
    root = tmp_path / "knowledge"
    (root / "nested").mkdir(parents=True)
    target = root / "nested" / "中性.md"
    target.write_text("## 中性\nneutral fixture", encoding="utf-8")
    assert read_knowledge_file(target)[1] == "## 中性\nneutral fixture"
    assert len(recall("neutral", str(root))["results"]) == 1
    assert read_knowledge_file(root / "absent.md") == (None, "")


@pytest.mark.parametrize("switch", ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "LAYERED_MEMORY_OFFLINE"])
def test_offline_cache_miss_never_retries_or_opens_socket(tmp_path, monkeypatch, switch, caplog):
    import fastembed

    calls = []
    def constructor(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("synthetic-exception-sentinel")

    def no_socket(*args, **kwargs):
        pytest.fail("offline model load attempted network")

    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "LAYERED_MEMORY_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(switch, "1")
    monkeypatch.setattr(socket, "create_connection", no_socket)
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(fastembed, "TextEmbedding", constructor)
    monkeypatch.setattr(vector_store, "_embedder", None)
    monkeypatch.setattr(vector_store, "MODEL_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(vector_store, "PROXY_URL", "http://synthetic.invalid")
    with pytest.raises(RuntimeError) as caught:
        vector_store._get_embedder()
    assert len(calls) == 1
    assert calls[0][1]["local_files_only"] is True
    assert "model_cache_miss_offline" in str(caught.value)
    assert "synthetic-exception-sentinel" not in caplog.text + str(caught.value)


@pytest.mark.parametrize("operation", ["io", "os", "sqlite", "replace", "rename", "unlink", "mkdir", "dir_fd"])
def test_guard_blocks_synthetic_protected_mutations(tmp_path, _guard_host_paths, operation):
    protected = tmp_path / "protected"
    protected.mkdir()
    target = protected / "neutral"
    target.write_text("neutral")
    source = tmp_path / "source"
    source.write_text("neutral")
    _guard_host_paths.append(str(protected))
    operations = {
        "io": lambda: io.open(target, "w"),
        "os": lambda: os.open(target, os.O_WRONLY),
        "sqlite": lambda: sqlite3.connect(protected / "new.db"),
        "replace": lambda: os.replace(source, target),
        "rename": lambda: os.rename(source, target),
        "unlink": lambda: os.unlink(target),
        "mkdir": lambda: os.mkdir(protected / "new"),
    }
    if operation == "dir_fd":
        descriptor = os.open(protected, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
                os.open("neutral", os.O_WRONLY, dir_fd=descriptor)
        finally:
            os.close(descriptor)
    else:
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            operations[operation]()


def test_guard_allows_component_sibling_and_disposable_io(tmp_path, _guard_host_paths):
    protected = tmp_path / "protected"
    _guard_host_paths.append(str(protected))
    sibling = tmp_path / "protected-sibling"
    sibling.mkdir()
    target = sibling / "normal"
    with io.open(target, "w") as handle:
        handle.write("neutral")
    replacement = sibling / "replacement"
    replacement.write_text("neutral")
    os.replace(replacement, target)
    os.rename(target, replacement)
    os.unlink(replacement)
    with sqlite3.connect(sibling / "normal.db") as connection:
        connection.execute("CREATE TABLE neutral (value TEXT)")
    assert (sibling / "normal.db").is_file()


@pytest.mark.parametrize("operation", ["replace_destination", "rename_source", "unlink", "mkdir", "sqlite_uri"])
def test_guard_directory_fd_and_sqlite_uri_mutations(tmp_path, _guard_host_paths, operation):
    protected = tmp_path / "protected"
    disposable = tmp_path / "disposable"
    protected.mkdir()
    disposable.mkdir()
    (protected / "neutral").write_text("neutral")
    (disposable / "neutral").write_text("neutral")
    protected_fd = os.open(protected, os.O_RDONLY | os.O_DIRECTORY)
    disposable_fd = os.open(disposable, os.O_RDONLY | os.O_DIRECTORY)
    _guard_host_paths.append(str(protected))
    operations = {
        "replace_destination": lambda: os.replace("neutral", "neutral", src_dir_fd=disposable_fd, dst_dir_fd=protected_fd),
        "rename_source": lambda: os.rename("neutral", "renamed", src_dir_fd=protected_fd, dst_dir_fd=disposable_fd),
        "unlink": lambda: os.unlink("neutral", dir_fd=protected_fd),
        "mkdir": lambda: os.mkdir("new", dir_fd=protected_fd),
        "sqlite_uri": lambda: sqlite3.connect((protected / "new.db").as_uri() + "?mode=rwc", uri=True),
    }
    try:
        with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
            operations[operation]()
        assert (protected / "neutral").read_text() == "neutral"
        descriptor = os.open("allowed", os.O_WRONLY | os.O_CREAT, dir_fd=disposable_fd)
        os.close(descriptor)
        os.unlink("allowed", dir_fd=disposable_fd)
    finally:
        os.close(protected_fd)
        os.close(disposable_fd)


def test_guard_rejects_link_directory_writes(tmp_path, _guard_host_paths):
    protected = tmp_path / "protected"
    protected.mkdir()
    source = tmp_path / "source"
    source.write_text("neutral")
    _guard_host_paths.append(str(protected))
    with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
        os.link(source, protected / "hard")
    with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
        os.symlink(source, protected / "symbolic")
    os.link(source, tmp_path / "allowed-hard")
    os.symlink(source, tmp_path / "allowed-symbolic")


@pytest.mark.parametrize("excluded", [False, True])
def test_graph_section_mirrors_and_allowed_noop(tmp_path, excluded):
    from datetime import datetime, timezone
    from layered_memory_mcp.graph_search import apply_graph_recall
    from layered_memory_mcp.graph_models import Derivation, DerivationMethod, GraphNode, NodeKind, NodeStatus
    from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore
    from layered_memory_mcp.graph_seed_metadata import graph_node_id_for_entry_id

    root = tmp_path / "knowledge"
    root.mkdir()
    raw = "## Allowed\nneedle [[allowed-link]]\n\n## Excluded\nneedle [[excluded-link]]\n"
    (root / "neutral.md").write_text(raw)
    rows = recall("needle", str(root))["results"]
    nodes = []
    for entry in rows[0]["section_entries"]:
        nodes.append(GraphNode(id=graph_node_id_for_entry_id(entry["entry_id"]), kind=NodeKind.CLAIM,
            label="neutral", content="neutral", recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            derivation=Derivation(method=DerivationMethod.MIGRATED),
            status=NodeStatus.SUPERSEDED if excluded and entry["section"] == "Excluded" else NodeStatus.ACTIVE))
    config = SimpleNamespace(graph_mode="graph_recall", graph_recall_enabled=True,
                             graph_write_enabled=False, graph_shadow_enabled=False, namespace="shared")
    for key in ("content", "text", "summary", "snippet", "excerpt", "body"):
        rows[0][key] = raw
    with SQLiteGraphStore(tmp_path / "graph.db", nodes=nodes) as store:
        outcome = apply_graph_recall(config, rows, identity_key="file", store=store)
    if excluded:
        assert "excluded-link" not in json.dumps(outcome.results)
        assert len(outcome.results[0]["section_entries"]) == 1
        excluded_ids = {node.id for node in nodes if node.status == NodeStatus.SUPERSEDED}
        assert not excluded_ids.intersection(outcome.block["results"][0]["graph_node_ids"])
    else:
        assert outcome.results == rows
        assert "excluded-link" in outcome.results[0]["wiki_links"]


@pytest.mark.parametrize("mode", ["graph", "shadow"])
def test_namespace_ids_and_diagnostics_are_screened(tmp_path, monkeypatch, mode):
    from datetime import datetime, timezone
    from layered_memory_mcp.graph_search import apply_graph_recall
    from layered_memory_mcp.graph_models import Derivation, DerivationMethod, GraphNode, GraphScope, NodeKind
    from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore
    from layered_memory_mcp import graph_search

    nodes = [GraphNode(id=identity, kind=NodeKind.CLAIM, label="neutral", content="neutral",
             scope=GraphScope(namespace=namespace), recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
             derivation=Derivation(method=DerivationMethod.MIGRATED))
             for identity, namespace in (("own-node", "own"), ("excluded-node-sentinel", "foreign"))]
    rows = [{"file": "neutral", "score": 1,
             "metadata": {"graph_node_ids": [node.id for node in nodes]}}]
    config = SimpleNamespace(graph_mode=mode, graph_recall_enabled=mode == "graph",
                             graph_write_enabled=False, graph_shadow_enabled=mode == "shadow", namespace="own")
    original_report = graph_search._graph_report
    def report_with_unsafe_warnings(*args, **kwargs):
        report = original_report(*args, **kwargs)
        warning = "provenance_unavailable:excluded-node-sentinel"
        inner = report.report.model_copy(update={
            "warnings": [warning],
            "results": [row.model_copy(update={"warnings": [warning]}) for row in report.report.results],
        })
        return report.model_copy(update={"report": inner, "warnings": [warning]})
    monkeypatch.setattr(graph_search, "_graph_report", report_with_unsafe_warnings)
    with SQLiteGraphStore(tmp_path / "graph.db", nodes=nodes) as store:
        outcome = apply_graph_recall(config, rows, identity_key="file", store=store)
    assert "excluded-node-sentinel" not in json.dumps(outcome.block)
    if mode == "shadow":
        assert outcome.results == rows
    else:
        assert "excluded-node-sentinel" not in json.dumps(outcome.results)


def test_absent_foreign_node_conflict_is_removed():
    from layered_memory_mcp import graph_runtime, graph_search
    from layered_memory_mcp.graph_models import GraphNode, GraphScope, Derivation
    from layered_memory_mcp.graph_recall import GraphRecallReport, GraphRecallResult
    from layered_memory_mcp.graph_store import ConflictReport

    nodes = [GraphNode(id=identity, kind="claim", label="neutral", content="neutral",
                      scope=GraphScope(namespace=namespace), derivation=Derivation(method="user_explicit"))
             for identity, namespace in (("own", "own"), ("shared", "shared"))]
    result = GraphRecallResult(file="neutral", graph_node_ids=["own", "foreign"], graph_nodes=nodes,
        conflicts=[ConflictReport(node_ids=["own", "shared"], conflict_group="allowed"),
                   ConflictReport(node_ids=["own", "foreign"], conflict_group="excluded")])
    report = graph_runtime.GraphRecallRuntimeReport(mode="graph", status="graph_partial",
        graph_authoritative=True, report=GraphRecallReport(status="graph_partial", results=[result]))
    mode = graph_runtime.GraphModeDecision(mode="graph", recall_enabled=True)
    screened, _ = graph_search._screen_namespaces(report, mode, frozenset({"own", "shared"}))
    encoded = json.dumps(screened.to_dict())
    assert "allowed" in encoded
    assert "foreign" not in encoded and '"excluded"' not in encoded


def test_narrow_namespace_sanitizes_diagnostics_even_without_returned_foreign_node(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from layered_memory_mcp import graph_search
    from layered_memory_mcp.graph_models import Derivation, DerivationMethod, GraphNode, GraphScope, NodeKind
    from layered_memory_mcp.graph_sqlite_store import SQLiteGraphStore

    node = GraphNode(id="own-node", kind=NodeKind.CLAIM, label="neutral", content="neutral",
        scope=GraphScope(namespace="own"), recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        derivation=Derivation(method=DerivationMethod.MIGRATED))
    original_report = graph_search._graph_report
    def unsafe_report(*args, **kwargs):
        report = original_report(*args, **kwargs)
        warning = "excluded_seed:foreign-warning-sentinel"
        inner = report.report.model_copy(update={"warnings": [warning], "results": [
            row.model_copy(update={"warnings": [warning]}) for row in report.report.results]})
        return report.model_copy(update={"report": inner, "warnings": [warning]})
    monkeypatch.setattr(graph_search, "_graph_report", unsafe_report)
    config = SimpleNamespace(graph_mode="graph_recall", graph_recall_enabled=True,
        graph_write_enabled=False, graph_shadow_enabled=False, namespace="own")
    with SQLiteGraphStore(tmp_path / "graph.db", nodes=[node]) as store:
        result = graph_search.apply_graph_recall(config, [{"file": "neutral", "score": 1,
            "metadata": {"graph_node_ids": [node.id, "foreign-warning-sentinel"]}}], identity_key="file", store=store)
    assert "foreign-warning-sentinel" not in json.dumps(result.block)
    assert "foreign-warning-sentinel" not in json.dumps(result.results)
    assert result.results


@pytest.mark.parametrize("allow_cron", [False, True])
def test_message_source_metadata_uses_same_db_and_file_policy(tmp_path, allow_cron):
    database = session_db(tmp_path, [("interactive", "neutral ALLOWED")])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE messages SET role='user', tool_calls=?", (json.dumps([{"source": "cron"}]),))
    directory = tmp_path / "sessions"
    session_file(directory, "neutral.json")
    (directory / "neutral.json").write_text(json.dumps({"source": "interactive", "messages": [
        {"role": "user", "content": "neutral ALLOWED " * 25, "tool_calls": [{"source": "cron"}]}]}))
    options = {"exclude_sources": []} if allow_cron else {}
    scanned_db = read_state_db_sessions(database, **options)
    scanned_files = scan_sessions(str(directory), prefer_state_db=False, **options)
    assert len(scanned_db["sessions"]) == len(scanned_files["sessions"]) == int(allow_cron)
    assert scanned_db["stats"]["skipped_sources"] == scanned_files["stats"]["skipped_sources"] == int(not allow_cron)


@pytest.mark.parametrize("phase", ["ancestor_open", "after_read"])
def test_knowledge_reader_detects_component_and_postread_replacement(tmp_path, monkeypatch, phase):
    from layered_memory_mcp.storage import knowledge_reader

    root = tmp_path / "knowledge"
    nested = root / "nested"
    nested.mkdir(parents=True)
    target = nested / "neutral.md"
    target.write_text("neutral")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "neutral.md").write_text("outside-sentinel")
    real_open, real_read = os.open, os.read
    replaced = False
    forbidden_opens = []
    decoy = (outside / "neutral.md").stat()
    def replace_component(path, flags, *args, **kwargs):
        nonlocal replaced
        if phase == "ancestor_open" and str(path) == "nested" and not replaced:
            replaced = True
            nested.rename(root / "previous")
            nested.symlink_to(outside, target_is_directory=True)
        descriptor = real_open(path, flags, *args, **kwargs)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == (decoy.st_dev, decoy.st_ino):
            forbidden_opens.append(descriptor)
        return descriptor
    def replace_after_read(descriptor, count):
        nonlocal replaced
        content = real_read(descriptor, count)
        if not replaced:
            replaced = True
            target.unlink()
            target.symlink_to(outside / "neutral.md")
        return content
    monkeypatch.setattr(knowledge_reader.os, "open", replace_component)
    if phase == "after_read":
        monkeypatch.setattr(knowledge_reader.os, "read", replace_after_read)
    assert read_knowledge_file(target) == (None, "")
    assert replaced
    assert forbidden_opens == []


def test_unified_hydration_refuses_symlink(tmp_path):
    from layered_memory_mcp.unified_recall import L1StoreHydrator

    root = tmp_path / "knowledge"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("## Neutral\noutside-sentinel")
    (root / "linked.md").symlink_to(outside)
    candidate = SimpleNamespace(entry_id="neutral-id", payload={"domain": "linked", "section": "Neutral"})
    assert L1StoreHydrator(L1Store(root)).hydrate(["neutral-id"], [candidate]) == {}


@pytest.mark.parametrize("linked", [False, True])
def test_l0_router_uses_secure_reader(tmp_path, linked):
    from layered_memory_mcp.unified_recall import L0RouteAdapter

    target = tmp_path / "index.md"
    outside = tmp_path / "outside.md"
    outside.write_text("neutral FORBIDDEN")
    if linked:
        target.symlink_to(outside)
    else:
        target.write_text("neutral ALLOWED")
    result = L0RouteAdapter(target).route("neutral")
    assert result == [] if linked else "ALLOWED" in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["inside", "outside", "ancestor", "regular"])
async def test_mcp_file_reader_uses_secure_boundary(tmp_path, monkeypatch, target_kind):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "neutral.md").write_text("neutral")
    outside = tmp_path / "outside.md"
    outside.write_text("outside-sentinel")
    if target_kind in ("inside", "outside"):
        (root / "linked.md").symlink_to(root / "neutral.md" if target_kind == "inside" else outside)
        filename = "linked.md"
    else:
        filename = "neutral.md"
    if target_kind == "ancestor":
        linked_root = tmp_path / "linked-root"
        linked_root.symlink_to(root, target_is_directory=True)
        root = linked_root
    monkeypatch.setattr(server, "_get_config", lambda: SimpleNamespace(knowledge_dirs=[root], knowledge_dir=root))
    monkeypatch.setattr(server, "_record_access", lambda events: None)
    result = json.loads(await server.get_knowledge_file(filename))
    assert result["success"] is (target_kind == "regular")
    assert "outside-sentinel" not in json.dumps(result)


@pytest.mark.asyncio
async def test_knowledge_listing_title_hint_cannot_follow_link(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# outside-title-sentinel\nneutral")
    (root / "linked.md").symlink_to(outside)
    (root / "regular.md").write_text("# Neutral\nneutral")
    monkeypatch.setattr(server, "_get_config", lambda: SimpleNamespace(knowledge_dirs=[root], knowledge_dir=root))
    result = json.loads(await server.list_knowledge_files())
    assert "outside-title-sentinel" not in json.dumps(result)
    assert next(row for row in result["files"] if row["file"] == "regular.md")["title_hint"] == "Neutral"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_rest_get_and_unified_search_validate_knowledge_files(tmp_path, monkeypatch, legacy_graph_mode, linked):
    import httpx
    from layered_memory_mcp.api import server as api
    from layered_memory_mcp.models import KnowledgeEntry

    config = MemoryConfig(home=tmp_path, auto_maintain=False)
    config.knowledge_dir.mkdir(parents=True, exist_ok=True)
    entry = KnowledgeEntry(domain="neutral", section="Neutral", content="neutral ALLOWED", summary="neutral")
    target = config.knowledge_dir / "neutral.md"
    outside = tmp_path / "outside.md"
    outside.write_text(entry.to_markdown().replace("ALLOWED", "FORBIDDEN"))
    if linked:
        target.symlink_to(outside)
    else:
        target.write_text(entry.to_markdown())
    row = {"id": entry.id, "domain": "neutral", "text": "neutral CACHED", "score": 1,
           "metadata": {"section": "Neutral"}}
    vectors = SimpleNamespace(search=lambda *args, **kwargs: [row], snapshot=lambda: [])
    monkeypatch.setattr(api, "stores", {"config": config, "l1": L1Store(config.knowledge_dir), "vector": vectors})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.create_app()), base_url="http://neutral") as client:
        fetched = await client.get(f"/knowledge/{entry.id}")
        assert fetched.status_code == (404 if linked else 200)
        search = await client.get("/knowledge/search", params={"q": "neutral", "unified": "true"})
        assert search.status_code == 200
        assert "FORBIDDEN" not in fetched.text + search.text
        assert search.json()["results"] == [] if linked else "ALLOWED" in search.text
        legacy = await client.get("/knowledge/search", params={"q": "neutral"})
        assert legacy.status_code == 200 and "CACHED" in legacy.text


@pytest.mark.asyncio
@pytest.mark.parametrize("entrance", ["get", "search"])
async def test_touched_rest_errors_are_content_free(tmp_path, monkeypatch, legacy_graph_mode, entrance, caplog):
    import httpx
    from layered_memory_mcp.api import server as api

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-exception-sentinel")
    config = MemoryConfig(home=tmp_path, auto_maintain=False)
    monkeypatch.setattr(api, "stores", {"config": config, "l1": SimpleNamespace(list_domains=fail),
                                      "vector": SimpleNamespace(search=fail)})
    path = "/knowledge/neutral-id" if entrance == "get" else "/knowledge/search?q=neutral"
    escaped = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.create_app()), base_url="http://neutral") as client:
        try:
            result = await client.get(path)
        except RuntimeError:
            escaped = True
    assert escaped is False
    assert result.status_code == 500
    assert "synthetic-exception-sentinel" not in result.text + caplog.text


@pytest.mark.parametrize("online_success", ["direct", "proxy", "cache"])
def test_model_loader_exact_online_and_cache_calls(tmp_path, monkeypatch, online_success):
    import fastembed

    for switch in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "LAYERED_MEMORY_OFFLINE"):
        monkeypatch.delenv(switch, raising=False)
    calls = []
    model = object()
    def construct(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) < {"cache": 1, "direct": 2, "proxy": 3}[online_success]:
            raise RuntimeError("synthetic-exception-sentinel")
        return model
    monkeypatch.setattr(fastembed, "TextEmbedding", construct)
    monkeypatch.setattr(vector_store, "MODEL_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(vector_store, "_embedder", None)
    monkeypatch.setattr(vector_store, "_embedder_local_only", False)
    monkeypatch.setattr(vector_store, "PROXY_URL", "http://synthetic.invalid")
    assert vector_store._get_embedder() is model
    assert calls[0] == ((vector_store.MODEL_NAME,), {"cache_dir": str(tmp_path / "models"), "local_files_only": True})
    assert all(call[1] == {"cache_dir": str(tmp_path / "models")} for call in calls[1:])
    assert vector_store._get_embedder() is model
    assert len(calls) == {"cache": 1, "direct": 2, "proxy": 3}[online_success]


def test_later_offline_request_cannot_reuse_online_singleton(tmp_path, monkeypatch):
    import fastembed

    monkeypatch.setattr(vector_store, "_embedder", object())
    monkeypatch.setattr(vector_store, "_embedder_local_only", False)
    monkeypatch.setattr(vector_store, "MODEL_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("LAYERED_MEMORY_OFFLINE", "1")
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("synthetic-exception-sentinel")
    monkeypatch.setattr(fastembed, "TextEmbedding", fail)
    with pytest.raises(RuntimeError, match="model_cache_miss_offline"):
        vector_store._get_embedder()
    assert len(calls) == 1 and calls[0]["local_files_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("entrance", ["get", "create", "update", "delete", "recall", "semantic", "scan", "search"])
async def test_public_entry_errors_are_content_free(tmp_path, monkeypatch, caplog, entrance):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-exception-sentinel")
    monkeypatch.setattr(server, "_get_config", fail)
    monkeypatch.setattr(server, "_get_v2_stores", fail)
    calls = {
        "get": (server.get_knowledge_file, ("neutral.md",)),
        "create": (server.create_knowledge_file, ("neutral.md", "neutral")),
        "update": (server.update_knowledge_file, ("neutral.md", "neutral")),
        "delete": (server.delete_knowledge_file, ("neutral.md",)),
        "recall": (server.recall_knowledge, ("neutral",)),
        "semantic": (server.search_semantic, ("neutral",)),
        "scan": (server.scan_recent_sessions, ()),
        "search": (server.search_sessions_by_keyword, ("neutral",)),
    }
    function, arguments = calls[entrance]
    result = json.loads(await function(*arguments))
    assert result["success"] is False
    assert "synthetic-exception-sentinel" not in json.dumps(result) + caplog.text


def test_access_log_failures_are_content_free(tmp_path, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-exception-sentinel")
    monkeypatch.setattr(sqlite3, "connect", fail)
    store = AccessLogStore(tmp_path / "access.db")
    store.record("neutral", "recall", "neutral")
    store.record_batch([{"domain": "neutral", "query": "neutral"}])
    store.overview()
    store.prune()
    assert "synthetic-exception-sentinel" not in caplog.text


def test_knowledge_read_replacement_race_is_refused(tmp_path, monkeypatch):
    from layered_memory_mcp.storage import knowledge_reader

    root = tmp_path / "knowledge"
    root.mkdir()
    target = root / "normal.md"
    target.write_text("neutral")
    outside = tmp_path / "outside.md"
    outside.write_text("synthetic-outside-sentinel")
    real_open = os.open
    hook_hits = []
    forbidden_opens = []
    decoy = outside.stat()
    def replace_on_open(path, flags, *args, **kwargs):
        if str(path) == "normal.md":
            hook_hits.append(path)
            target.unlink()
            target.symlink_to(outside)
        descriptor = real_open(path, flags, *args, **kwargs)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == (decoy.st_dev, decoy.st_ino):
            forbidden_opens.append(descriptor)
        return descriptor

    monkeypatch.setattr(knowledge_reader.os, "open", replace_on_open)
    assert read_knowledge_file(target) == (None, "")
    assert hook_hits == ["normal.md"]
    assert forbidden_opens == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["vector", "index"])
async def test_delete_reports_partial_cleanup(tmp_path, monkeypatch, legacy_graph_mode, cleanup, caplog):
    config = MemoryConfig(home=tmp_path, auto_maintain=False)
    config.knowledge_dir.mkdir(parents=True, exist_ok=True)
    target = config.knowledge_dir / "neutral.md"
    target.write_text("## Neutral\nneutral fixture")
    monkeypatch.setattr(server, "_get_config", lambda: config)
    monkeypatch.setattr(server, "_record_file_mutation", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "auto_sync_if_enabled", lambda *args: {"success": cleanup != "index"})
    monkeypatch.setattr(server, "remove_from_vector_store", lambda **kwargs: {
        "success": cleanup != "vector", "error": "synthetic-exception-sentinel"})
    result = json.loads(await server.delete_knowledge_file("neutral.md"))
    assert not target.exists()
    assert result["success"] is False
    assert result["file_deleted"] is True
    assert result["cleanup_pending"] is True
    assert result["steps"][cleanup] == "failed"
    assert "synthetic-exception-sentinel" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("index_result", ["false", "exception", "disabled"])
async def test_delete_index_failure_still_removes_real_vector_rows(
    tmp_path, monkeypatch, legacy_graph_mode, index_result, caplog
):
    from layered_memory_mcp.injector import remove_from_vector_store

    config = MemoryConfig(home=tmp_path, auto_maintain=False)
    config.knowledge_dir.mkdir(parents=True, exist_ok=True)
    (config.knowledge_dir / "neutral.md").write_text("## Neutral\nALLOWED")
    config.vector_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.vector_db_path) as connection:
        connection.execute("CREATE TABLE vectors (id TEXT, domain TEXT)")
        connection.executemany("INSERT INTO vectors VALUES (?, ?)", [("one", "neutral"), ("two", "other")])
    calls = []
    def index_cleanup(*args):
        if index_result == "exception":
            raise RuntimeError("synthetic-exception-sentinel")
        return None if index_result == "disabled" else {"success": False, "error": "synthetic-exception-sentinel"}
    def vector_cleanup(**kwargs):
        calls.append(kwargs["domain"])
        return remove_from_vector_store(**kwargs)
    monkeypatch.setattr(server, "_get_config", lambda: config)
    monkeypatch.setattr(server, "_record_file_mutation", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "auto_sync_if_enabled", index_cleanup)
    monkeypatch.setattr(server, "remove_from_vector_store", vector_cleanup)
    result = json.loads(await server.delete_knowledge_file("neutral.md"))
    assert calls == ["neutral"]
    assert result["file_deleted"] is True
    assert result["steps"]["vector"] == "completed"
    assert result["steps"]["index"] == ("skipped" if index_result == "disabled" else "failed")
    assert result["success"] is (index_result == "disabled")
    assert result["cleanup_pending"] is (index_result != "disabled")
    with sqlite3.connect(config.vector_db_path) as connection:
        assert connection.execute("SELECT domain FROM vectors").fetchall() == [("other",)]
    assert "synthetic-exception-sentinel" not in json.dumps(result) + caplog.text


def test_vector_removal_real_rows_and_failure_codes(tmp_path, monkeypatch, caplog):
    from layered_memory_mcp.injector import remove_from_vector_store

    database = tmp_path / "vectors.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE vectors (id TEXT, domain TEXT)")
        connection.executemany("INSERT INTO vectors VALUES (?, ?)", [("one", "neutral"), ("two", "other")])
    result = remove_from_vector_store(tmp_path, "neutral", vector_db_path=database)
    assert result["success"] and result["removed"] == 1
    assert remove_from_vector_store(tmp_path, "neutral", vector_db_path=database)["success"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT domain FROM vectors").fetchall() == [("other",)]
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-exception-sentinel")
    monkeypatch.setattr(sqlite3, "connect", fail)
    result = remove_from_vector_store(tmp_path, "neutral", vector_db_path=database)
    assert result["success"] is False
    assert "synthetic-exception-sentinel" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("memory_mode", ["pointers", "index_only"])
@pytest.mark.parametrize("failure", ["read", "write"])
async def test_dual_write_downstream_failures_are_screened(
    tmp_path, monkeypatch, legacy_graph_mode, operation, memory_mode, failure, caplog, capsys
):
    memory = tmp_path / "agent" / "neutral.md"
    memory.parent.mkdir()
    memory.write_text("neutral agent fixture")
    monkeypatch.setenv("LAYERED_MEMORY_AGENT_MEMORY_PATH", str(memory))
    monkeypatch.setenv("LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR", "---")
    config = MemoryConfig(home=tmp_path / "application", auto_maintain=True, memory_mode=memory_mode)
    config.knowledge_dir.mkdir(parents=True, exist_ok=True)
    target = config.knowledge_dir / "neutral.md"
    if operation == "update":
        target.write_text("## Neutral\nprevious fixture")
    monkeypatch.setattr(server, "_get_config", lambda: config)
    monkeypatch.setattr(server, "_record_file_mutation", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "auto_sync_if_enabled", lambda *args: None)
    monkeypatch.setattr(server, "sync_to_vector_store", lambda **kwargs: {"success": True})
    monkeypatch.setattr(server, "post_write_governance", lambda *args: {})
    original_read, original_write = Path.read_text, Path.write_text
    hits = []
    def read(path, *args, **kwargs):
        if path == memory and failure == "read":
            hits.append("read")
            raise OSError("synthetic-downstream-exception-sentinel")
        return original_read(path, *args, **kwargs)
    def write(path, *args, **kwargs):
        if path == memory and failure == "write":
            hits.append("write")
            raise OSError("synthetic-downstream-exception-sentinel")
        return original_write(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", write)
    tool = server.create_knowledge_file if operation == "create" else server.update_knowledge_file
    result = json.loads(await tool("neutral.md", "## Neutral\nALLOWED updated fixture"))
    captured = capsys.readouterr()
    assert result["success"] is True
    assert hits == [failure]
    assert "ALLOWED updated fixture" in original_read(target)
    assert original_read(memory) == "neutral agent fixture"
    assert "synthetic-downstream-exception-sentinel" not in json.dumps(result) + caplog.text + captured.out + captured.err
    assert result["dual_write"] == {"action": "skipped", "category": f"pointer_{failure}_failed"}


@pytest.mark.parametrize("counterpart_namespace", ["own", "foreign"])
def test_grouped_conflict_retains_content_free_signal(tmp_path, counterpart_namespace):
    from layered_memory_mcp.graph_search import apply_graph_recall
    from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
    from layered_memory_mcp.graph_models import GraphNode, GraphScope, Derivation

    nodes = [GraphNode(id=identity, kind="claim", label="neutral", content="neutral",
                      scope=GraphScope(namespace=namespace), conflict_group="sensitive-group-sentinel",
                      derivation=Derivation(method="user_explicit"))
             for identity, namespace in (("own-node", "own"), ("unreturned-counterpart-sentinel", counterpart_namespace))]
    rows = [{"file": "neutral", "score": 1, "text": "ALLOWED body",
             "metadata": {"graph_node_id": "own-node"}}]
    config = SimpleNamespace(graph_mode="graph", graph_recall_enabled=True, graph_shadow_enabled=False,
                             graph_write_enabled=False, namespace="own")
    outcome = apply_graph_recall(config, rows, identity_key="file", store=InMemoryGraphStore(nodes=nodes))
    assert "ALLOWED body" in json.dumps(outcome.results)
    assert "unreturned-counterpart-sentinel" not in json.dumps(outcome.block)
    assert "sensitive-group-sentinel" not in json.dumps(outcome.block)
    assert "unresolved_conflict" in outcome.block["warnings"]


@pytest.mark.parametrize("mode", ["graph", "shadow"])
@pytest.mark.parametrize("listed", [None, "foreign-id-sentinel", {"foreign-id-sentinel": "ignored"},
                                   [None, {}, [], 7, "foreign-id-sentinel"],
                                   ("own-node", "foreign-id-sentinel", {})])
def test_tolerant_metadata_ids_keep_allowed_body(tmp_path, mode, listed):
    from copy import deepcopy
    from layered_memory_mcp.graph_search import apply_graph_recall
    from layered_memory_mcp.graph_fixture_store import InMemoryGraphStore
    from layered_memory_mcp.graph_models import GraphNode, GraphScope, Derivation

    node = GraphNode(id="own-node", kind="claim", label="neutral", content="neutral",
                     scope=GraphScope(namespace="own"), derivation=Derivation(method="user_explicit"))
    rows = [{"file": "neutral", "score": 1, "text": "ALLOWED body",
             "metadata": {"graph_node_id": "own-node", "graph_node_ids": listed}}]
    original = deepcopy(rows)
    config = SimpleNamespace(graph_mode=mode, graph_recall_enabled=mode == "graph", graph_shadow_enabled=mode == "shadow",
                             graph_write_enabled=False, namespace="own")
    escaped = False
    try:
        outcome = apply_graph_recall(config, rows, identity_key="file", store=InMemoryGraphStore(nodes=[node]))
    except TypeError:
        escaped = True
    assert escaped is False
    assert rows == original
    assert "ALLOWED body" in json.dumps(outcome.results)
    assert "foreign-id-sentinel" not in json.dumps(outcome.block)
    if mode == "shadow":
        assert outcome.results == original
    else:
        assert "foreign-id-sentinel" not in json.dumps(outcome.results)
        assert outcome.results[0]["metadata"]["graph_node_ids"] == (["own-node"] if isinstance(listed, tuple) else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("database_kind", ["unrelated", "missing", "empty", "file_only", "invalid"])
async def test_keyword_directory_fallback_preserves_db_exclusions(tmp_path, monkeypatch, database_kind):
    directory = tmp_path / "sessions"
    allowed = session_file(directory, "allowed.json")
    allowed.write_text(json.dumps({"messages": [{"role": "user", "content": "NEUTRAL-QUERY ALLOWED " * 15}]}))
    database = None
    if database_kind == "unrelated":
        database = session_db(tmp_path, [("interactive", "unrelated allowed body"),
                                        ("interactive", "PRIVATE-SYNTHETIC")])
        for position in range(5):
            alias = session_file(directory, f"alias-{position}.json")
            alias.write_text(json.dumps({"session": {"id": "1"}, "messages": [
                {"role": "user", "content": "NEUTRAL-QUERY HIDDEN " * 15}]}))
    elif database_kind == "empty":
        database = session_db(tmp_path, [])
    elif database_kind == "missing":
        database = tmp_path / "missing.db"
    elif database_kind == "invalid":
        database = tmp_path / "invalid.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE neutral(value TEXT)")
    config = SimpleNamespace(hermes_db_path=database, sessions_dir=directory,
                             session_exclude_markers=["PRIVATE-SYNTHETIC"], session_exclude_sources=[])
    monkeypatch.setattr(server, "_get_config", lambda: config)
    result = json.loads(await server.search_sessions_by_keyword("NEUTRAL-QUERY", max_results=1))
    if database_kind == "invalid":
        assert result["success"] is False
        assert result["matches"] == []
    else:
        assert result["success"] is True
        assert result["matched_sessions"] == 1
        assert "ALLOWED" in json.dumps(result["matches"])
        assert "HIDDEN" not in json.dumps(result)
        assert Path(result["matches"][0]["path"]).name == "allowed.json"

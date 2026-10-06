"""v3.4.2 — session scanning reads Hermes' live session database.

Why: the session-file directory this module was written for now only holds stale
request dumps, so ``scan_sessions`` reported **zero** sessions while the agent had
been running for weeks. The whole session-driven pipeline (knowledge extraction,
keyword search, the memory-compression job) silently degraded to "nothing new" —
the worst failure mode, because it looks like success.

Locked down here:

1. sessions come out of ``state.db`` (title/user messages/decisions intact)
2. ``cron`` sessions are skipped by default and **counted** — on a real host they
   are ~87% of the table (5763/6607) and would crowd out interactive sessions
3. operator-configured ``exclude_markers`` withhold matching sessions entirely
   and are reported in ``stats`` instead of vanishing (privacy control: the scan
   output is handed to a model that may run off-machine)
4. the package ships **no** markers by default — an empty list, not a guess about
   anyone's work
5. keyword search follows the same source + filter policy
6. a missing DB still falls back to the file directory (file-based setups)

Fixtures use neutral placeholder content.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.session_scanner import (
    read_state_db_sessions,
    scan_sessions,
    search_state_db_sessions,
)


def _build_db(tmp_path: Path, sessions: list[dict], messages: dict[str, list[tuple]]) -> Path:
    """Minimal stand-in for Hermes' state.db (only the columns we read)."""
    db = tmp_path / "state.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, source TEXT, "
            "started_at REAL, last_activity_at REAL, message_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "role TEXT, content TEXT, tool_calls TEXT)"
        )
        for s in sessions:
            conn.execute(
                "INSERT INTO sessions (id, title, source, started_at, last_activity_at, message_count) "
                "VALUES (?,?,?,?,?,?)",
                (s["id"], s.get("title", ""), s.get("source", "cli"),
                 s.get("started_at", time.time()), s.get("last_activity_at"), s.get("message_count", 0)),
            )
        for session_id, rows in messages.items():
            for role, content in rows:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
                    (session_id, role, content),
                )
    return db


def _now() -> float:
    return time.time()


class TestReadStateDbSessions:
    def test_returns_interactive_sessions_with_content(self, tmp_path):
        db = _build_db(
            tmp_path,
            [{"id": "sess-1", "title": "placeholder task", "message_count": 2}],
            {"sess-1": [("user", "placeholder question"),
                        ("assistant", "找到根因：placeholder explanation")]},
        )
        result = read_state_db_sessions(db, days=3, max_sessions=5)
        assert result["source"] == "hermes_state_db"
        assert result["stats"]["scanned"] == 1
        session = result["sessions"][0]
        assert session["session_id"] == "sess-1"
        assert session["user_messages"] == ["placeholder question"]
        assert session["key_decisions"], "decision keyword should surface evidence"
        for key in ("path", "title", "mtime", "assistant_topics", "tool_calls"):
            assert key in session

    def test_cron_sessions_are_skipped_and_counted(self, tmp_path):
        db = _build_db(
            tmp_path,
            [
                {"id": "cron_x", "title": "placeholder job", "source": "cron"},
                {"id": "sess-2", "title": "placeholder interactive", "source": "cli"},
            ],
            {"cron_x": [("user", "placeholder")], "sess-2": [("user", "placeholder")]},
        )
        result = read_state_db_sessions(db, days=3, max_sessions=5)
        assert [s["session_id"] for s in result["sessions"]] == ["sess-2"]
        assert result["stats"]["skipped_sources"] == 1

    def test_exclude_markers_withhold_and_are_reported(self, tmp_path):
        db = _build_db(
            tmp_path,
            [{"id": "sess-sensitive", "title": "PLACEHOLDER-SECRET workstream"},
             {"id": "sess-open", "title": "placeholder open work"}],
            {"sess-sensitive": [("user", "placeholder about PLACEHOLDER-SECRET")],
             "sess-open": [("user", "placeholder")]},
        )
        result = read_state_db_sessions(
            db, days=3, max_sessions=5, exclude_markers=["placeholder-secret"]
        )
        assert [s["session_id"] for s in result["sessions"]] == ["sess-open"]
        assert result["stats"]["excluded_sessions"] == 1
        assert result["stats"]["excluded_by_marker"] == {"placeholder-secret": 1}

    def test_marker_match_inside_body_not_only_title(self, tmp_path):
        db = _build_db(
            tmp_path,
            [{"id": "sess-body", "title": "innocent title"}],
            {"sess-body": [("user", "placeholder"), ("assistant", "mentions placeholder-secret here")]},
        )
        result = read_state_db_sessions(db, days=3, max_sessions=5,
                                        exclude_markers=["placeholder-secret"])
        assert result["sessions"] == []
        assert result["stats"]["excluded_sessions"] == 1

    def test_window_excludes_old_sessions(self, tmp_path):
        old = _now() - 30 * 86400
        db = _build_db(
            tmp_path,
            [{"id": "sess-old", "title": "placeholder old", "started_at": old}],
            {"sess-old": [("user", "placeholder")]},
        )
        result = read_state_db_sessions(db, days=3, max_sessions=5)
        assert result["sessions"] == []
        assert result["stats"]["rows_in_window"] == 0

    def test_max_sessions_caps_output(self, tmp_path):
        sessions = [{"id": f"sess-{i}", "title": f"placeholder {i}"} for i in range(5)]
        messages = {s["id"]: [("user", "placeholder")] for s in sessions}
        db = _build_db(tmp_path, sessions, messages)
        result = read_state_db_sessions(db, days=3, max_sessions=2)
        assert len(result["sessions"]) == 2

    def test_missing_db_reports_error_not_exception(self, tmp_path):
        result = read_state_db_sessions(tmp_path / "nope.db", days=3)
        assert result["sessions"] == []
        assert "not found" in result["error"]


class TestScanSessionsSourceOrder:
    def test_prefers_state_db(self, tmp_path):
        db = _build_db(tmp_path, [{"id": "sess-1", "title": "placeholder"}],
                       {"sess-1": [("user", "placeholder")]})
        result = scan_sessions(str(tmp_path / "empty-dir"), days=3, hermes_db_path=db)
        assert result["source"] == "hermes_state_db"

    def test_falls_back_to_session_dir_when_db_missing(self, tmp_path):
        sdir = tmp_path / "sessions"
        sdir.mkdir()
        (sdir / "20260101_000000_aaaa.json").write_text(
            json.dumps({"session_id": "file-1",
                        "messages": [{"role": "user", "content": "placeholder " * 20}]}),
            encoding="utf-8",
        )
        result = scan_sessions(str(sdir), days=30, hermes_db_path=tmp_path / "nope.db")
        assert result["source"] == "session_dir"
        assert result["total_sessions"] == 1

    def test_falls_through_when_db_filters_everything(self, tmp_path):
        sdir = tmp_path / "sessions"
        sdir.mkdir()
        (sdir / "20260101_000000_bbbb.json").write_text(
            json.dumps({"session_id": "file-2",
                        "messages": [{"role": "user", "content": "placeholder " * 20}]}),
            encoding="utf-8",
        )
        db = _build_db(tmp_path, [{"id": "cron_only", "source": "cron"}],
                       {"cron_only": [("user", "placeholder")]})
        result = scan_sessions(str(sdir), days=30, hermes_db_path=db)
        assert result["source"] == "session_dir"

    def test_no_source_returns_empty_envelope(self, tmp_path):
        result = scan_sessions(None, days=3, hermes_db_path=tmp_path / "nope.db")
        assert result["source"] == "none"
        assert result["sessions"] == []


class TestSearchStateDbSessions:
    def test_finds_keyword_and_skips_cron(self, tmp_path):
        db = _build_db(
            tmp_path,
            [{"id": "cron_x", "source": "cron"}, {"id": "sess-1", "title": "placeholder"}],
            {"cron_x": [("user", "placeholder-needle")],
             "sess-1": [("user", "placeholder-needle here")]},
        )
        result = search_state_db_sessions("placeholder-needle", db_path=db, days=3)
        assert [m["session_id"] for m in result["matches"]] == ["sess-1"]
        assert result["matched_sessions"] == 1

    def test_markers_exclude_sessions(self, tmp_path):
        db = _build_db(
            tmp_path,
            [{"id": "sess-1", "title": "placeholder"}],
            {"sess-1": [("user", "placeholder-needle plus placeholder-secret")]},
        )
        result = search_state_db_sessions("placeholder-needle", db_path=db, days=3,
                                          exclude_markers=["placeholder-secret"])
        assert result["matches"] == []
        assert result["stats"]["excluded_sessions"] == 1


class TestConfigDefaults:
    def test_package_ships_no_marker_assumptions(self, tmp_path):
        cfg = MemoryConfig(home=str(tmp_path / "home"))
        assert cfg.session_exclude_markers == []
        assert cfg.session_exclude_sources == ["cron"]

    def test_markers_read_from_config_yaml(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "session_scan:\n"
            "  exclude_markers:\n"
            "    - placeholder-secret\n"
            "  exclude_sources: [cron, webhook]\n"
            "  hermes_db_path: /placeholder/state.db\n",
            encoding="utf-8",
        )
        cfg = MemoryConfig(home=str(home))
        assert cfg.session_exclude_markers == ["placeholder-secret"]
        assert cfg.session_exclude_sources == ["cron", "webhook"]
        assert str(cfg.hermes_db_path) == "/placeholder/state.db"

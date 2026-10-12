"""Lifecycle governance: structured marker actions, cleanup plan / safe apply,
embedding-heading normalisation, and catch-all watch patterns.

Why this test file exists: v3.4.7 flagged state markers with one generic
"move to TODO or rewrite" hint and nothing could act on it; vectors embedded
the section heading twice, dates included; and the promotion detector only
watched a file literally named ``misc.md``, which most new users never have.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from layered_memory_mcp.cleanup import (
    apply_safe_knowledge_cleanup,
    plan_knowledge_cleanup,
)
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.governance import (
    build_rules,
    rules_from_config,
    scan_state_markers,
    scan_state_markers_text,
)
from layered_memory_mcp.heading import embedding_heading, heading_skeleton
from layered_memory_mcp.promotion import detect_promotion_candidate, is_watched_domain


def _cfg(tmp_path, **kw) -> MemoryConfig:
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"), **kw)


def _write_yaml(tmp_path, text: str) -> None:
    home = tmp_path / ".layered-memory"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*") if p.is_file()
    }


def _actions(res: dict) -> list[tuple[int, str]]:
    return [(f["line"], f["action"]) for f in res["lifecycle_findings"]]


# --- P1. lifecycle actions -----------------------------------------------------

class TestLifecycleActions:
    def test_english_open_markers_move_to_todo(self):
        text = (
            "## Rollout\n"
            "- Next step: enable the canary\n"
            "- Retry budget needs decision\n"
            "- Webhooks not implemented yet\n"
            "- pending requests are queued in FIFO order\n"
            "- TODO: document the flag\n"
        )
        res = scan_state_markers_text(text, "ops.md")
        assert [(f["line"], f["marker"]) for f in res["findings"]] == [
            (2, "next step"), (3, "needs decision"), (4, "not implemented"), (6, "TODO"),
        ]
        assert all(f["action"] == "move_to_todo" for f in res["findings"])
        # Legacy keys survive next to the new ones.
        f = res["findings"][0]
        assert f["suggested_action"] == "move_to_todo_or_rewrite_as_current_rule"
        assert f["domain"] == "ops" and len(f["line_hash"]) == 12
        assert res["summary"]["by_action"] == {
            "move_to_todo": 4, "rewrite_as_current_rule": 0, "keep_with_reason": 0,
        }

    def test_resolved_lines_become_rewrite(self):
        text = (
            "## 接入\n"
            "- 行情源尚未接入 → 2026-10-01 已落地\n"
            "- 2026-09-30 已修复登录超时\n"
            "- The cache bug has been fixed in 3.4\n"
            "- 普通规则\n"
        )
        res = scan_state_markers_text(text, "x.md")
        assert res["findings"] == []  # legacy list: open markers only
        assert res["summary"]["state_markers"] == 0
        assert res["summary"]["suppressed_completed"] == 1  # legacy meaning kept
        assert _actions(res) == [
            (2, "rewrite_as_current_rule"),
            (3, "rewrite_as_current_rule"),
            (4, "rewrite_as_current_rule"),
        ]
        assert res["lifecycle_findings"][0]["marker"] == "尚未接入"
        assert res["lifecycle_findings"][0]["resolved_marker"] == "已落地"

    def test_keep_reason_english_and_chinese(self):
        text = (
            "## Canary\n- Phase two needs validation\n"
            "**State retention reason**: reminds on-call during the canary\n\n"
            "## 灰度\n- 第二阶段待验证\n状态保留原因：灰度期间提醒复测\n\n"
            "## Other\n- TODO here\n"
        )
        res = scan_state_markers_text(text, "x.md")
        assert [f["heading"] for f in res["findings"]] == ["Other"]
        assert res["summary"]["suppressed_keep_reason"] == 2
        keeps = [f for f in res["lifecycle_findings"] if f["action"] == "keep_with_reason"]
        assert [k["heading"] for k in keeps] == ["Canary", "灰度"]
        assert keeps[0]["keep_reason"].startswith("reminds on-call")

    def test_rules_are_extensible_from_config_yaml(self, tmp_path):
        _write_yaml(tmp_path, (
            "governance:\n"
            "  open_markers: ['À FAIRE', 'follow up']\n"
            "  resolved_markers: ['erledigt']\n"
            "  keep_reason_labels: ['Raison de conservation']\n"
            "  disabled_markers: ['下一步']\n"
        ))
        cfg = _cfg(tmp_path)
        assert cfg.governance_open_markers == ["À FAIRE", "follow up"]
        text = (
            "## a\n- À FAIRE: migrer\n- Follow up with legal\n- 下一步：上线\n"
            "- Migration erledigt\n\n"
            "## b\n- TODO keep me\nRaison de conservation : audit\n"
        )
        res = scan_state_markers_text(text, "x.md", rules_from_config(cfg))
        assert _actions(res) == [
            (2, "move_to_todo"), (3, "move_to_todo"),
            (5, "rewrite_as_current_rule"), (8, "keep_with_reason"),
        ]
        # The disabled default is gone; the defaults are otherwise intact.
        assert build_rules().match_open("下一步：上线") == "下一步"

    def test_scan_over_dir_aggregates_by_action(self, tmp_path):
        cfg = _cfg(tmp_path)
        (cfg.knowledge_dir / "a.md").write_text("## x\nTODO 补文档\n已修复：缓存\n", encoding="utf-8")
        res = scan_state_markers(cfg)
        assert res["summary"]["by_action"]["move_to_todo"] == 1
        assert res["summary"]["by_action"]["rewrite_as_current_rule"] == 1
        assert len(res["lifecycle_findings"]) == 2 and res["lifecycle_truncated"] is False


# --- P2. plan + safe apply -----------------------------------------------------

def _seed(cfg: MemoryConfig) -> Path:
    fp = cfg.knowledge_dir / "ops.md"
    fp.write_text(
        "# ops\n\n## 发布\n"
        "- 灰度开关待拍板\n"
        "- TODO\n"
        "- TODO rotate api_key=PLACEHOLDER_VALUE\n"
        "- 2026-10-01 已修复回滚脚本\n\n"
        "## 保留\n- 第二阶段待验证\n状态保留原因：提醒复测\n\n"
        "## Deploy 2026-10-01\nfirst\n\n## Deploy 2026-10-12\nsecond\n",
        encoding="utf-8",
    )
    return fp


class TestCleanupPlan:
    def test_plan_buckets_and_read_only(self, tmp_path):
        cfg = _cfg(tmp_path)
        fp = _seed(cfg)
        (cfg.knowledge_dir / "ops.md.bak").write_text("old", encoding="utf-8")
        before = _snapshot(cfg.home)

        plan = plan_knowledge_cleanup(cfg)

        assert _snapshot(cfg.home) == before  # nothing written, no todos.db
        assert plan["read_only"] is True
        safe = {a["action"] for a in plan["safe_auto_fixes"]}
        assert {"migrate_legacy_backups", "create_todo_from_marker"} <= safe
        todos = [a for a in plan["safe_auto_fixes"] if a["action"] == "create_todo_from_marker"]
        assert [t["line"] for t in todos] == [4]
        assert todos[0]["remove_source"] is False
        kinds = [(d["kind"], d.get("line")) for d in plan["needs_user_decision"]]
        # bare "TODO" and the credential-shaped line need a human; so do
        # resolved history and the heading family.
        assert ("move_to_todo", 5) in kinds and ("move_to_todo", 6) in kinds
        assert ("rewrite_as_current_rule", 7) in kinds
        assert any(k == "merge_heading_family" for k, _ in kinds)
        assert [k["kind"] for k in plan["must_not_auto_fix"]] == ["keep_with_reason"]
        # The plan never echoes a credential beyond the existing short preview.
        assert "TEST_TOKEN_SHOULD_NOT_ECHO" not in json.dumps(plan["safe_auto_fixes"])
        assert plan["summary"]["safe_auto_fixes"] == len(plan["safe_auto_fixes"])
        assert fp.exists()

    def test_vector_and_l0_candidates(self, tmp_path, monkeypatch):
        import layered_memory_mcp.injector as inj

        l0 = tmp_path / "l0.md"
        l0.write_text("", encoding="utf-8")
        cfg = _cfg(tmp_path, l0_index_file=str(l0))
        (cfg.knowledge_dir / "a.md").write_text("# a\n\n## x\nbody\n", encoding="utf-8")
        monkeypatch.setattr(inj, "vector_store_needs_reindex", lambda _c: True)
        safe = {a["action"] for a in plan_knowledge_cleanup(cfg)["safe_auto_fixes"]}
        assert {"sync_l0_index", "reindex_vector_store"} <= safe


class TestSafeApply:
    def _todo_action(self, cfg):
        plan = plan_knowledge_cleanup(cfg)
        return next(a for a in plan["safe_auto_fixes"] if a["action"] == "create_todo_from_marker")

    def test_default_is_dry_run_and_writes_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed(cfg)
        action = self._todo_action(cfg)
        before = _snapshot(cfg.home)
        res = apply_safe_knowledge_cleanup(cfg, [action])
        assert res["dry_run"] is True
        assert res["applied"] == [] and len(res["would_apply"]) == 1
        assert _snapshot(cfg.home) == before
        assert not (cfg.home / "data" / "todos.db").exists()

    def test_creates_todo_keeps_source_and_is_idempotent(self, tmp_path):
        from layered_memory_mcp.todo_store import TodoStore

        cfg = _cfg(tmp_path)
        fp = _seed(cfg)
        original = fp.read_text(encoding="utf-8")
        action = self._todo_action(cfg)

        res = apply_safe_knowledge_cleanup(cfg, [action], dry_run=False)
        assert res["summary"]["applied"] == 1, res
        applied = res["applied"][0]
        assert applied["source_retained"] is True
        assert fp.read_text(encoding="utf-8") == original

        todos = TodoStore(cfg.home / "data" / "todos.db").list(domain="ops")
        assert len(todos) == 1
        assert todos[0]["content"] == "灰度开关待拍板"
        assert "knowledge/ops.md line 4" in todos[0]["notes"]

        again = apply_safe_knowledge_cleanup(cfg, [action], dry_run=False)
        assert again["summary"]["applied"] == 0
        assert "already" in again["skipped"][0]["reason"]

    def test_stale_line_is_skipped_but_moved_line_is_found(self, tmp_path):
        cfg = _cfg(tmp_path)
        fp = _seed(cfg)
        action = self._todo_action(cfg)
        # Insert a line above: the line number moves, the hash still matches.
        fp.write_text(fp.read_text(encoding="utf-8").replace("## 发布\n", "## 发布\n- 新规则\n"),
                      encoding="utf-8")
        assert apply_safe_knowledge_cleanup(cfg, [action])["would_apply"][0]["line"] == 5
        # Change the line itself: the plan is stale.
        fp.write_text(fp.read_text(encoding="utf-8").replace("灰度开关待拍板", "灰度开关已定"),
                      encoding="utf-8")
        res = apply_safe_knowledge_cleanup(cfg, [action])
        assert res["would_apply"] == [] and "stale" in res["skipped"][0]["reason"]

    def test_rejects_unknown_unsafe_and_source_removal(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed(cfg)
        action = self._todo_action(cfg)
        res = apply_safe_knowledge_cleanup(cfg, [
            {"action": "delete_section", "file": "ops.md"},
            {"action": "make_it_better"},
            {**action, "remove_source": True},
            {**action, "file": "../../etc/passwd.md"},
            "not json at all",
        ], dry_run=False)
        errors = [r["error"] for r in res["rejected"]]
        assert "needs a user decision" in errors[0]
        assert errors[1] == "unknown action"
        assert "remove_source is not supported" in errors[2]
        assert "unknown knowledge file" in errors[3]
        assert len(res["rejected"]) == 5 and res["applied"] == []
        assert not (cfg.home / "data" / "todos.db").exists()

    def test_migrate_legacy_backups(self, tmp_path):
        cfg = _cfg(tmp_path)
        kdir = cfg.knowledge_dir
        (kdir / "a.md").write_text("new", encoding="utf-8")
        (kdir / "a.md.bak").write_text("legacy-a", encoding="utf-8")
        (kdir / "b.md.bak").write_text("legacy-b", encoding="utf-8")
        root = cfg.home / "data" / "backups" / "knowledge"
        root.mkdir(parents=True)
        (root / "b.md.bak").write_text("recent-b", encoding="utf-8")

        dry = apply_safe_knowledge_cleanup(cfg, [{"action": "migrate_legacy_backups"}])
        assert len(dry["would_apply"][0]["moved"]) == 2
        assert (kdir / "a.md.bak").exists()

        res = apply_safe_knowledge_cleanup(cfg, [{"action": "migrate_legacy_backups"}], dry_run=False)
        assert res["summary"]["applied"] == 1, res
        assert list(kdir.rglob("*.bak")) == []
        assert (root / "a.md.bak").read_text(encoding="utf-8") == "legacy-a"
        # The existing out-of-tree backup is not overwritten.
        assert (root / "b.md.bak").read_text(encoding="utf-8") == "recent-b"
        assert (root / "b.md.legacy.bak").read_text(encoding="utf-8") == "legacy-b"

    def test_reindex_runs_only_when_needed_or_forced(self, tmp_path, monkeypatch):
        import layered_memory_mcp.injector as inj

        cfg = _cfg(tmp_path)
        calls = []
        monkeypatch.setattr(inj, "vector_store_needs_reindex", lambda _c: False)
        monkeypatch.setattr(inj, "reindex_vector_store",
                            lambda config, drop_existing: calls.append(1) or
                            {"success": True, "domains": 1, "sections": 2, "files": 1, "errors": []})
        res = apply_safe_knowledge_cleanup(cfg, [{"action": "reindex_vector_store"}], dry_run=False)
        assert res["skipped"] and calls == []
        res = apply_safe_knowledge_cleanup(
            cfg, [{"action": "reindex_vector_store", "force": True}], dry_run=False
        )
        assert res["applied"][0]["report"]["sections"] == 2 and calls == [1]

    @pytest.mark.asyncio
    async def test_mcp_tools_round_trip(self, tmp_path, monkeypatch):
        import layered_memory_mcp.server as srv

        cfg = _cfg(tmp_path)
        _seed(cfg)
        srv._config = cfg
        monkeypatch.setattr(srv, "_todo_store", None)
        try:
            plan = json.loads(await srv.plan_knowledge_cleanup_tool())
            todo = [a for a in plan["safe_auto_fixes"] if a["action"] == "create_todo_from_marker"]
            dry = json.loads(await srv.apply_safe_knowledge_cleanup_tool(actions=todo))
            real = json.loads(await srv.apply_safe_knowledge_cleanup_tool(actions=todo, dry_run=False))
            listed = json.loads(await srv.list_todos(domain="ops"))
        finally:
            srv._config = None
            srv._todo_store = None
        assert dry["dry_run"] is True and dry["summary"]["would_apply"] == 1
        assert real["summary"]["applied"] == 1
        assert listed["count"] == 1


# --- P3. embedding heading -----------------------------------------------------

class TestEmbeddingHeading:
    def test_strips_noise_keeps_words(self):
        assert embedding_heading("Release process — 2026-10-01") == "Release process"
        assert embedding_heading("发布流程 v3.4.7") == "发布流程"
        assert embedding_heading("WSL 代理") == "WSL 代理"
        # Pure-date heading has an empty skeleton: keep the original.
        assert heading_skeleton("2026-10-01") == ""
        assert embedding_heading("2026-10-01") == "2026-10-01"

    @pytest.fixture
    def captured(self, monkeypatch):
        texts: list[str] = []

        def fake_embed(batch):
            texts.extend(batch)
            out = np.zeros((len(batch), 512), dtype=np.float32)
            for i in range(len(batch)):
                out[i, (len(texts) - len(batch) + i) % 512] = 1.0
            return out

        monkeypatch.setattr("layered_memory_mcp.storage.vector_store._embed_texts", fake_embed)
        return texts

    def _rows(self, data_dir: Path) -> list[tuple[str, dict]]:
        with sqlite3.connect(data_dir / "vectors.db") as conn:
            rows = conn.execute("SELECT text, metadata FROM vectors ORDER BY rowid").fetchall()
        return [(t, json.loads(m)) for t, m in rows]

    def test_rebuild_uses_normalised_heading_once(self, tmp_path, captured):
        from layered_memory_mcp.injector import sync_to_vector_store

        data = tmp_path / "data"
        content = (
            "# ops\n\n> intro\n\n## 发布流程 2026-10-01\n先打 tag\n\n"
            "## Release process (v3.4.7)\npush image\n\n## 空节\n\n## 回滚\n一键回滚\n"
        )
        res = sync_to_vector_store(data_dir=data, domain="ops", content=content, replace_domain=True)
        assert res["sections"] == 3  # one per non-empty ## section

        rows = self._rows(data)
        assert [m["section"] for _t, m in rows] == [
            "发布流程 2026-10-01", "Release process (v3.4.7)", "回滚",
        ]
        assert [t for t, _m in rows] == [
            "发布流程\n先打 tag", "Release process\npush image", "回滚\n一键回滚",
        ]
        assert captured == [t for t, _m in rows]  # what was embedded
        assert all("2026-10-01" not in t and "v3.4.7" not in t for t in captured)

    def test_same_skeleton_identical_body_keeps_both_vectors(self, tmp_path, captured):
        from layered_memory_mcp.injector import _count_bodyful_sections, sync_to_vector_store

        content = "## Deploy 2026-10-01\nsame\n\n## Deploy 2026-10-12\nsame\n"
        res = sync_to_vector_store(data_dir=tmp_path / "data", domain="d",
                                   content=content, replace_domain=True)
        assert res["sections"] == _count_bodyful_sections(content) == 2
        assert len(self._rows(tmp_path / "data")) == 2

    def test_search_hit_body_no_longer_repeats_heading(self, tmp_path, captured):
        from layered_memory_mcp.injector import _section_body_from_hit, sync_to_vector_store

        sync_to_vector_store(data_dir=tmp_path / "data", domain="d",
                             content="## WSL 代理\n走 7890\n", replace_domain=True)
        (text, _meta), = self._rows(tmp_path / "data")
        assert _section_body_from_hit(text) == "走 7890"


# --- P4. watch patterns ----------------------------------------------------------

def _fake_embed_same(texts):
    out = np.zeros((len(texts), 512), dtype=np.float32)
    out[:, 0] = 1.0  # every section on one axis → one cluster
    return out


class TestWatchPatterns:
    def test_default_patterns_cover_common_catch_alls(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert cfg.promotion_watch_domains == ["misc"]  # explicit list unchanged
        for dom in ("misc", "notes", "Inbox", "general", "misc-2026", "杂项", "其他"):
            assert is_watched_domain(cfg, dom), dom
        assert not is_watched_domain(cfg, "infra")
        assert not is_watched_domain(cfg, "notes", include_patterns=False)
        assert is_watched_domain(cfg, "misc.md", include_patterns=False)

    def test_patterns_from_yaml_env_and_arg(self, tmp_path, monkeypatch):
        _write_yaml(tmp_path, "promotion:\n  watch_patterns: ['dump-*']\n  watch_domains: [junk]\n")
        cfg = _cfg(tmp_path)
        assert cfg.promotion_watch_patterns == ["dump-*"]
        assert cfg.promotion_watch_domains == ["junk"]
        assert is_watched_domain(cfg, "dump-q3") and not is_watched_domain(cfg, "notes")

        monkeypatch.setenv("LAYERED_MEMORY_PROMOTION_WATCH_PATTERNS", "")
        assert _cfg(tmp_path).promotion_watch_patterns == []  # env empties it
        assert _cfg(tmp_path, promotion_watch_patterns=["x*"]).promotion_watch_patterns == ["x*"]

    def test_audit_scans_notes_but_write_path_does_not(self, tmp_path, monkeypatch):
        from layered_memory_mcp.rot_auditor import audit_rot

        monkeypatch.setattr("layered_memory_mcp.storage.vector_store._embed_texts", _fake_embed_same)
        cfg = _cfg(tmp_path)
        fp = cfg.knowledge_dir / "notes.md"
        fp.write_text("# notes\n\n" + "".join(
            f"## database tip {i}\npool size {i}\n\n" for i in range(5)
        ), encoding="utf-8")

        report = audit_rot(cfg)
        assert [c["watch_domain"] for c in report["findings"]["promotion_candidates"]] == ["notes"]
        # The write-path ride-along stays on the explicit list (no extra embedding).
        assert detect_promotion_candidate(cfg, "notes", fp) is None
        assert detect_promotion_candidate(cfg, "notes", fp, include_patterns=True) is not None

    def test_split_scan_honours_patterns_when_gated(self, tmp_path, monkeypatch):
        from layered_memory_mcp.promotion import scan_split_candidates

        monkeypatch.setattr("layered_memory_mcp.storage.vector_store._embed_texts", _fake_embed_same)
        cfg = _cfg(tmp_path)
        body = "".join(f"## t{i}\nb{i}\n\n" for i in range(5))
        (cfg.knowledge_dir / "inbox.md").write_text(body, encoding="utf-8")
        (cfg.knowledge_dir / "infra.md").write_text(body, encoding="utf-8")
        out = scan_split_candidates(cfg, ignore_watch_gate=False)
        assert out["scanned"] == 1
        assert [c["domain"] for c in out["candidates"]] == ["inbox"]

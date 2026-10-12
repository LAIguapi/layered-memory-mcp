"""v3.4.7 — governance for whole-file writes, and backups out of knowledge/.

Why this test file exists: ``inject_knowledge`` had semantic and family gates,
but ``create_knowledge_file`` / ``update_knowledge_file`` wrote raw content and
reported nothing about lifecycle state ("待拍板", "尚未接入", a second
``## 发布流程 <date>`` section). Every rewrite also left ``<file>.md.bak`` beside
the live file, so the knowledge dir collected unindexed shadow copies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from layered_memory_mcp.backups import backup_knowledge_file, knowledge_backup_path
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.governance import (
    build_validation_extras,
    post_write_governance,
    scan_state_markers,
    scan_state_markers_text,
)
from layered_memory_mcp.memory_compactor import dedup_l1_file, dedup_l1_knowledge


def _cfg(tmp_path) -> MemoryConfig:
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"))


def _bak_siblings(kdir: Path) -> list[Path]:
    return list(kdir.rglob("*.bak"))


# --- A. backups --------------------------------------------------------------

class TestBackups:
    def test_backup_goes_under_data_backups_not_beside_file(self, tmp_path):
        cfg = _cfg(tmp_path)
        fp = cfg.knowledge_dir / "infra.md"
        fp.write_text("# infra\n\n## a\nold\n", encoding="utf-8")

        res = backup_knowledge_file(cfg, fp)

        expected = cfg.home / "data" / "backups" / "knowledge" / "infra.md.bak"
        assert res["path"] == str(expected)
        assert res["bytes"] == len("# infra\n\n## a\nold\n".encode("utf-8"))
        assert expected.read_text(encoding="utf-8") == "# infra\n\n## a\nold\n"
        assert _bak_siblings(cfg.knowledge_dir) == []

    def test_backup_is_stable_per_source(self, tmp_path):
        cfg = _cfg(tmp_path)
        fp = cfg.knowledge_dir / "infra.md"
        fp.write_text("v1", encoding="utf-8")
        backup_knowledge_file(cfg, fp)
        fp.write_text("v2", encoding="utf-8")
        backup_knowledge_file(cfg, fp)
        root = cfg.home / "data" / "backups" / "knowledge"
        assert [p.name for p in root.rglob("*.bak")] == ["infra.md.bak"]
        assert (root / "infra.md.bak").read_text(encoding="utf-8") == "v2"

    def test_relative_path_preserved_for_subdirectories(self, tmp_path):
        cfg = _cfg(tmp_path)
        fp = cfg.knowledge_dir / "team" / "notes.md"
        assert knowledge_backup_path(cfg, fp) == (
            cfg.home / "data" / "backups" / "knowledge" / "team" / "notes.md.bak"
        )

    def test_missing_source_is_skipped_not_raised(self, tmp_path):
        cfg = _cfg(tmp_path)
        res = backup_knowledge_file(cfg, cfg.knowledge_dir / "nope.md")
        assert "skipped" in res

    def test_dedup_l1_knowledge_leaves_no_adjacent_bak(self, tmp_path):
        cfg = _cfg(tmp_path)
        fp = cfg.knowledge_dir / "bloated.md"
        dup = "\n".join(["同一条脏数据"] * 50)
        fp.write_text(f"# bloated\n\n## x\n{dup}\n", encoding="utf-8")

        report = dedup_l1_knowledge(cfg, min_dup_lines=20)

        assert report["files_deduped"], report
        assert fp.read_text(encoding="utf-8").count("同一条脏数据") == 1
        assert _bak_siblings(cfg.knowledge_dir) == []
        bak = cfg.home / "data" / "backups" / "knowledge" / "bloated.md.bak"
        assert bak.read_text(encoding="utf-8").count("同一条脏数据") == 50

    def test_dedup_l1_file_with_config_backs_up_out_of_tree(self, tmp_path):
        cfg = _cfg(tmp_path)
        fp = cfg.knowledge_dir / "misc.md"
        fp.write_text("# misc\n\n## a\nX\nX\n", encoding="utf-8")
        assert dedup_l1_file(fp, cfg)["action"] == "deduped"
        assert _bak_siblings(cfg.knowledge_dir) == []

    @pytest.mark.asyncio
    async def test_update_knowledge_file_backs_up_out_of_tree(self, tmp_path, monkeypatch):
        import layered_memory_mcp.server as srv

        cfg = _cfg(tmp_path)
        srv._config = cfg
        monkeypatch.setattr(
            srv,
            "sync_to_vector_store",
            lambda **_kwargs: {"success": True, "action": "mock_vector_sync", "sections": 1},
        )
        try:
            fp = cfg.knowledge_dir / "infra.md"
            fp.write_text("# infra\n\n## a\nold\n", encoding="utf-8")
            res = json.loads(await srv.update_knowledge_file(
                filename="infra.md", content="# infra\n\n## a\nnew\n"
            ))
        finally:
            srv._config = None

        assert res["success"] is True
        # Existing fields survive.
        for key in ("action", "file", "old_size", "new_size", "l0_synced"):
            assert key in res
        assert _bak_siblings(cfg.knowledge_dir) == []
        assert Path(res["backup"]["path"]).read_text(encoding="utf-8") == "# infra\n\n## a\nold\n"
        assert "governance" in res and "error" not in res["governance"]


# --- B. state markers ----------------------------------------------------------

class TestStateMarkers:
    def test_flags_pending_markers(self):
        text = (
            "# stock\n\n## 选股规则\n"
            "- 新因子权重待拍板\n"
            "- 实时行情尚未接入\n"
            "- 普通规则一条\n"
        )
        res = scan_state_markers_text(text, "stock-analysis.md")
        markers = [(f["line"], f["marker"]) for f in res["findings"]]
        assert markers == [(4, "待拍板"), (5, "尚未接入")]
        f = res["findings"][0]
        assert f["file"] == "stock-analysis.md"
        assert f["heading"] == "选股规则"
        assert f["suggested_action"] == "move_to_todo_or_rewrite_as_current_rule"
        assert res["summary"]["state_markers"] == 2
        json.dumps(res)  # JSON-serialisable

    def test_same_line_completed_marker_suppresses(self):
        text = "## 接入\n- 行情源尚未接入 → 2026-10-01 已落地\n"
        res = scan_state_markers_text(text, "x.md")
        assert res["findings"] == []
        assert res["summary"]["suppressed_completed"] == 1

    def test_keep_reason_in_section_suppresses(self):
        text = (
            "## 灰度方案\n- 第二阶段待验证\n状态保留原因：灰度期间需要提醒复测\n\n"
            "## 另一节\n- 这条待验证\n"
        )
        res = scan_state_markers_text(text, "x.md")
        # Only the section without a reason is reported.
        assert [f["heading"] for f in res["findings"]] == ["另一节"]
        assert res["summary"]["suppressed_keep_reason"] == 1

    def test_ascii_markers_need_word_boundaries(self):
        text = "## a\n- 用 todo_store 记录\n- TODOS 列表\n- TODO: 补测试\n"
        res = scan_state_markers_text(text, "x.md")
        assert [f["line"] for f in res["findings"]] == [4]

    def test_code_fences_and_comments_ignored(self):
        text = "## a\n```\n# TODO in code\n```\n<!-- 待办 -->\n"
        assert scan_state_markers_text(text, "x.md")["findings"] == []

    def test_scan_over_knowledge_dir(self, tmp_path):
        cfg = _cfg(tmp_path)
        (cfg.knowledge_dir / "a.md").write_text("## x\n下一步：上线\n", encoding="utf-8")
        (cfg.knowledge_dir / "b.md").write_text("## y\n干净\n", encoding="utf-8")
        res = scan_state_markers(cfg)
        assert res["summary"]["files_scanned"] == 2
        assert res["summary"]["files_with_markers"] == 1
        assert res["findings"][0]["marker"] == "下一步"


# --- C. post-write hook --------------------------------------------------------

class TestPostWriteGovernance:
    def test_reports_same_skeleton_family(self, tmp_path):
        cfg = _cfg(tmp_path)
        content = (
            "# ops\n\n## 发布流程 2026-10-01\n先打 tag\n\n"
            "## 发布流程 2026-10-12\n再推镜像\n\n## 回滚\n一键回滚\n"
        )
        fp = cfg.knowledge_dir / "ops.md"
        fp.write_text(content, encoding="utf-8")

        gov = post_write_governance(cfg, "ops", fp, content, "update")

        assert gov["write_kind"] == "update"
        fams = gov["heading_families"]
        assert len(fams) == 1 and fams[0]["count"] == 2
        assert fams[0]["headings"] == ["发布流程 2026-10-01", "发布流程 2026-10-12"]
        assert gov["size"]["oversized"] is False
        assert gov["suggestions"]
        json.dumps(gov)

    def test_size_warning(self, tmp_path):
        cfg = _cfg(tmp_path)
        content = "## big\n" + ("内容" * 3000)
        gov = post_write_governance(cfg, "big", cfg.knowledge_dir / "big.md", content)
        assert gov["size"]["oversized"] is True

    def test_never_raises(self, tmp_path):
        cfg = _cfg(tmp_path)
        gov = post_write_governance(cfg, "ghost", cfg.knowledge_dir / "ghost.md")
        assert "error" in gov

    @pytest.mark.asyncio
    async def test_create_attaches_governance(self, tmp_path, monkeypatch):
        import layered_memory_mcp.server as srv

        cfg = _cfg(tmp_path)
        srv._config = cfg
        monkeypatch.setattr(
            srv,
            "sync_to_vector_store",
            lambda **_kwargs: {"success": True, "action": "mock_vector_sync", "sections": 1},
        )
        try:
            res = json.loads(await srv.create_knowledge_file(
                filename="plan.md", content="# plan\n\n## 迁移\n方案待拍板\n"
            ))
        finally:
            srv._config = None
        assert res["success"] is True and res["action"] == "created"
        assert res["governance"]["write_kind"] == "create"
        assert res["governance"]["state_markers"]["summary"]["state_markers"] == 1


# --- D. validate_knowledge -----------------------------------------------------

class TestValidationExtras:
    def test_extras_shape(self, tmp_path):
        cfg = _cfg(tmp_path)
        (cfg.knowledge_dir / "a.md").write_text("## x\n待复测\n", encoding="utf-8")
        extras = build_validation_extras(cfg)
        assert "oversized" in extras["rot_summary"]
        assert isinstance(extras["rot_health_score"], int)
        assert extras["governance_summary"]["state_markers"]["state_markers"] == 1

    def test_extras_degrade_on_failure(self, tmp_path, monkeypatch):
        import layered_memory_mcp.rot_auditor as ra

        def boom(_cfg):
            raise RuntimeError("audit exploded")

        monkeypatch.setattr(ra, "audit_rot", boom)
        extras = build_validation_extras(_cfg(tmp_path))
        assert extras["rot_summary"] is None
        assert extras["rot_error"] == "audit exploded"
        assert "governance_summary" in extras

    @pytest.mark.asyncio
    async def test_validate_knowledge_keeps_shape_and_adds_keys(self, tmp_path):
        import layered_memory_mcp.server as srv

        cfg = _cfg(tmp_path)
        (cfg.knowledge_dir / "a.md").write_text("# a\n\n## x\nTODO 补文档\n", encoding="utf-8")
        srv._config = cfg
        try:
            report = json.loads(await srv.validate_knowledge())
        finally:
            srv._config = None
        for key in ("overall_health", "issues", "l0_l1_consistency", "file_reports"):
            assert key in report
        assert "rot_summary" in report
        assert report["governance_summary"]["state_markers"]["state_markers"] == 1

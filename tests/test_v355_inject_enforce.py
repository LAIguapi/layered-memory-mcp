"""The direct-write routing gate — `routing.enforce_direct_writes`.

Why this file exists: `compact_memory` has gated on `routing.mode: enforce`
since the router shipped, and `inject_knowledge` has not. That asymmetry was
deliberate but undecided. It is decided here, and the decision is encoded as
two independent switches rather than one:

  * `routing.mode` gates the path where the **framework** picked the target.
    It still never refuses an explicit write — a caller that names a domain and
    a section gets that write, annotated.
  * `routing.enforce_direct_writes` (default **false**) is the only switch that
    can refuse an explicit `inject_knowledge` call. Turning it on turns today's
    `routing_warning` into a refusal that happens *before* the write; turning it
    off is the shipped behaviour, byte for byte.

The properties pinned below are the ones that make that safe to ship on top of
an existing knowledge base:

  * **The default is unchanged.** Same result keys, same actions, same writes.
  * **Only a positive non-L1 classification is refused.** `skill`,
    `user_profile`, `agent_other`, `todo`, `ephemeral` — things the classifier
    says belong somewhere else. `undecided` is the classifier saying it has no
    evidence at all; that is not a verdict, so the write proceeds and is
    annotated (`routing_enforcement.insufficient_evidence`). Refusing it would
    have made the gate's answer a function of *length*: the same short fact
    written out at length is admitted as `l1_knowledge` by the length-only
    fallback.
  * **A refusal writes nothing.** No file, no section, no MEMORY_CHANGELOG
    event, no agent-owned write — and it hands back the surface the item
    belongs to, with the `AgentAction` to take instead.
  * **The caller can still insist.** `routing_override="force_l1"` writes, and
    says it overrode.
  * **The flag is inert without a classification** (`routing.enabled: false`)
    and does not reach into compaction, which has its own gate.

Every test runs against a temp home; conftest's sandbox fixtures are the
backstop. Nothing here may resolve the real `~/.hermes` or `~/.layered-memory`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.injector import (
    ROUTING_REFUSED_ACTION,
    ROUTING_REFUSED_ERROR,
    _hash_body,
    inject_knowledge,
    normalize_routing_override,
)
from layered_memory_mcp.memory_changelog import changelog_settings
from layered_memory_mcp.routing import direct_write_settings, surface_capabilities


# --- fixtures ----------------------------------------------------------------


def _cfg(tmp_path: Path, name: str = ".layered-memory", **kwargs) -> MemoryConfig:
    home = tmp_path / name
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home), knowledge_dir=str(home / "knowledge"), **kwargs
    )


def _strict(tmp_path: Path, name: str = ".layered-memory", **routing) -> MemoryConfig:
    """A config with the direct-write gate switched on."""
    return _cfg(
        tmp_path, name, routing={"enforce_direct_writes": True, **routing}
    )


def _events(cfg: MemoryConfig) -> list[dict]:
    path = Path(changelog_settings(cfg)["path"])
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    ]


def _knowledge_files(cfg: MemoryConfig) -> list[str]:
    return sorted(p.name for p in Path(cfg.knowledge_dir).glob("*.md"))


SKILL_TEXT = """发布流程（每次发版都按这个来）：

1. 运行 pytest -q，确认全绿
2. 执行 python -m build 打包
3. git tag v3.5.5 && git push --tags
4. 上传后在 PyPI 页面核对版本号
"""

TODO_TEXT = (
    "待验证：enforce 模式下的直接写入拒绝行为还要等用户拍板；"
    "在那之前这条一直是阻塞状态，需要用户决策。"
)

PROFILE_TEXT = (
    "用户偏好：以后所有回复都先给结论再给细节，始终用中文，"
    "任何时候都不要在回答里加免责声明。"
)

KNOWLEDGE_TEXT = (
    "路由器的职责边界：routing.py 只做确定性的标记与结构匹配，不做语义推断，"
    "也不认识任何 agent 产品的具体路径。这个架构决策的原因是核心框架必须保持产品中立，"
    "产品相关的落地逻辑全部放在 adapters 层。当前 L1 存的是成立的事实与边界。"
)

KNOWLEDGE_TEXT_2 = (
    "写入路径的职责边界：直写闸门在分类之后、去重之前运行，这个顺序是刻意的决策 —— "
    "被拒绝的内容不应该付出向量与合并的代价。当前的事实是：闸门只影响调用方自己指定目标的写入。"
)

#: Short, unmarked, structurally plain: no rule matches and it is below
#: ``routing.MIN_CHARS_FOR_FALLBACK``, so it classifies `undecided` with score
#: 0.0 and `confidence_basis: insufficient`.
SHORT_UNMARKED_FACT = "镜像仓库在 registry.example.com。"

#: The same shape of content, past ``MIN_CHARS_FOR_FALLBACK``: still no marker,
#: but long enough for the length-only fallback to call it `l1_knowledge`.
LONG_UNMARKED_PROSE = (
    "镜像仓库在 registry.example.com，内网直连，不走代理；构建机和跳板机都已经配好解析，"
    "别的机器要先改 hosts 才能拉取。这段话没有任何标记词，也没有编号步骤或命令块，"
    "纯粹靠长度被归到持久知识。"
)

LOG_TEXT = """08:12:01 INFO: starting scan of 14 domains
08:12:02 DEBUG: loaded vectors.db (512 dim)
08:12:03 ERROR: vector sync failed for infra
Traceback (most recent call last):
  File "injector.py", line 884
"""


# --- A. config ---------------------------------------------------------------


class TestConfigFlag:
    def test_default_is_off(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert cfg.routing_enforce_direct_writes is False
        settings = direct_write_settings(cfg)
        assert settings == {
            "routing_enabled": True,
            "mode": "advisory",
            "enforce_direct_writes": False,
            "gate_active": False,
        }

    def test_yaml_section_turns_it_on(self, tmp_path):
        cfg = _strict(tmp_path)
        assert cfg.routing_enforce_direct_writes is True
        assert direct_write_settings(cfg)["gate_active"] is True

    def test_env_var_turns_it_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_ROUTING_ENFORCE_DIRECT_WRITES", "true")
        assert _cfg(tmp_path).routing_enforce_direct_writes is True

    def test_malformed_value_falls_back_to_off(self, tmp_path):
        cfg = _cfg(tmp_path, routing={"enforce_direct_writes": "maybe"})
        assert cfg.routing_enforce_direct_writes is False

    def test_mode_and_the_gate_are_independent(self, tmp_path):
        """`enforce` does not imply strict direct writes, and vice versa."""
        enforce_only = _cfg(tmp_path, "a", routing={"mode": "enforce"})
        assert direct_write_settings(enforce_only) == {
            "routing_enabled": True,
            "mode": "enforce",
            "enforce_direct_writes": False,
            "gate_active": False,
        }
        gate_only = _strict(tmp_path, "b")
        assert direct_write_settings(gate_only)["mode"] == "advisory"
        assert direct_write_settings(gate_only)["gate_active"] is True

    def test_routing_disabled_makes_the_flag_inert(self, tmp_path):
        cfg = _strict(tmp_path, enabled=False)
        assert cfg.routing_enforce_direct_writes is True
        assert direct_write_settings(cfg)["gate_active"] is False

    def test_capabilities_report_both_switches(self, tmp_path):
        caps = surface_capabilities(_strict(tmp_path)).to_dict()
        assert caps["routing_mode"] == "advisory"
        assert caps["routing_enforce_direct_writes"] is True
        assert "REFUSED" in caps["core_surfaces"]["l1_knowledge"]["notes"]

        default = surface_capabilities(_cfg(tmp_path, "plain")).to_dict()
        assert default["routing_enforce_direct_writes"] is False
        assert "routing_warning" in default["core_surfaces"]["l1_knowledge"]["notes"]


class TestOverrideNormalization:
    def test_known_values(self):
        assert normalize_routing_override(None) == ("none", None)
        assert normalize_routing_override("") == ("none", None)
        assert normalize_routing_override("force_l1") == ("force_l1", None)
        assert normalize_routing_override("FORCE_L1 ") == ("force_l1", None)

    def test_unknown_value_is_not_permission(self):
        override, warning = normalize_routing_override("force")
        assert override == "none"
        assert "ignored unknown routing_override" in warning


# --- B. the default stays the default ---------------------------------------


class TestDefaultsAreUnchanged:
    def test_skill_shaped_direct_write_still_lands_in_l1(self, tmp_path):
        cfg = _cfg(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["success"] is True
        assert res["action"] in ("created", "section_created", "appended", "replaced")
        assert "发布流程" in (Path(cfg.knowledge_dir) / "ops.md").read_text(
            encoding="utf-8"
        )
        assert res["routing"]["surface"] == "skill"
        assert "NOT changed" in res["routing_warning"]
        # The strict gate is not configured, so it says nothing at all.
        assert "routing_enforcement" not in res
        assert "routing_override_applied" not in res

    def test_enforce_mode_alone_still_does_not_refuse_a_direct_write(self, tmp_path):
        cfg = _cfg(tmp_path, routing={"mode": "enforce"})
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["success"] is True
        assert res["action"] != ROUTING_REFUSED_ACTION
        assert res["routing"]["mode"] == "enforce"
        assert "gates compact_memory" in res["routing_warning"]
        assert "routing_enforcement" not in res
        assert (Path(cfg.knowledge_dir) / "ops.md").exists()

    def test_override_argument_changes_nothing_without_the_flag(self, tmp_path):
        """An override of a gate that is not armed is a no-op, not a bypass."""
        plain = inject_knowledge(
            config=_cfg(tmp_path, "a"),
            domain="ops",
            section="发布流程",
            content=SKILL_TEXT,
        )
        forced = inject_knowledge(
            config=_cfg(tmp_path, "b"),
            domain="ops",
            section="发布流程",
            content=SKILL_TEXT,
            routing_override="force_l1",
        )
        assert forced["action"] == plain["action"]
        assert "routing_override_applied" not in forced
        assert "routing_enforcement" not in forced


# --- C. the gate refuses ------------------------------------------------------


class TestStrictGateRefuses:
    def test_skill_shaped_content_is_refused(self, tmp_path):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["success"] is False
        assert res["action"] == ROUTING_REFUSED_ACTION
        assert res["error"] == ROUTING_REFUSED_ERROR
        assert res["written"] is False
        assert res["domain"] == "ops" and res["section"] == "发布流程"
        assert "refused by routing.enforce_direct_writes" in res["message"]
        assert "skill" in res["message"]

    def test_a_refusal_writes_absolutely_nothing(self, tmp_path):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["action"] == ROUTING_REFUSED_ACTION
        assert _knowledge_files(cfg) == []
        assert _events(cfg) == [], "a refused write is not a mutation"

    def test_a_refusal_does_not_touch_an_existing_file_or_section(self, tmp_path):
        cfg = _strict(tmp_path)
        inject_knowledge(
            config=cfg, domain="ops", section="路由边界", content=KNOWLEDGE_TEXT
        )
        path = Path(cfg.knowledge_dir) / "ops.md"
        before = path.read_text(encoding="utf-8")
        events_before = _events(cfg)

        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["action"] == ROUTING_REFUSED_ACTION
        assert path.read_text(encoding="utf-8") == before
        assert "## 发布流程" not in before
        assert _events(cfg) == events_before

    def test_refusal_carries_the_decision_and_the_agent_action(self, tmp_path):
        res = inject_knowledge(
            config=_strict(tmp_path),
            domain="ops",
            section="发布流程",
            content=SKILL_TEXT,
        )
        routing = res["routing"]
        assert routing["surface"] == "skill"
        assert routing["ownership"] == "agent"
        assert routing["score"] > 0
        assert routing["signals"], "the refusal must show its evidence"

        action = res["suggested_action"]
        assert action["kind"] == "agent_native_write"
        assert action["surface"] == "skill"
        assert action["title"] == "发布流程"
        # The framework hands back the caller's own text, never prose it wrote.
        assert action["source_content"] == SKILL_TEXT.strip()
        assert "steps" in action["shape_hint"]

    def test_user_profile_refusal_asks_for_confirmation(self, tmp_path):
        res = inject_knowledge(
            config=_strict(tmp_path),
            domain="prefs",
            section="回答风格",
            content=PROFILE_TEXT,
        )
        assert res["action"] == ROUTING_REFUSED_ACTION
        assert res["routing"]["surface"] == "user_profile"
        assert res["suggested_action"]["requires_user_confirmation"] is True

    def test_todo_shaped_content_comes_back_as_a_todo_candidate(self, tmp_path):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="ops", section="待办", content=TODO_TEXT
        )
        assert res["action"] == ROUTING_REFUSED_ACTION
        assert res["routing"]["surface"] == "todo"
        candidate = res["todo_candidate"]
        assert candidate["surface"] == "todo"
        assert candidate["title"] == "待办"
        assert candidate["content"] == TODO_TEXT
        assert "add_todo" in candidate["hint"]
        # Reported, never created — the write path has no TODO write contract.
        assert res["suggested_action"] is None
        assert _knowledge_files(cfg) == []

    def test_raw_log_output_is_refused_as_ephemeral(self, tmp_path):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="ops", section="同步失败", content=LOG_TEXT
        )
        assert res["action"] == ROUTING_REFUSED_ACTION
        assert res["routing"]["surface"] == "ephemeral"
        assert _knowledge_files(cfg) == []

    def test_the_enforcement_block_explains_the_gate(self, tmp_path):
        res = inject_knowledge(
            config=_strict(tmp_path),
            domain="ops",
            section="发布流程",
            content=SKILL_TEXT,
        )
        block = res["routing_enforcement"]
        assert block["enforce_direct_writes"] is True
        assert block["routing_mode"] == "advisory"
        assert block["direct_write"] is True
        assert block["gate_active"] is True
        assert block["override_requested"] == "none"
        assert block["override_applied"] is False
        assert "refused before any L1 write" in block["note"]

    def test_an_unknown_override_is_not_a_bypass(self, tmp_path):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg,
            domain="ops",
            section="发布流程",
            content=SKILL_TEXT,
            routing_override="yes please",
        )
        assert res["action"] == ROUTING_REFUSED_ACTION
        assert "ignored unknown routing_override" in (
            res["routing_enforcement"]["override_warning"]
        )
        assert _knowledge_files(cfg) == []


# --- C2. absence of evidence is not a refusal --------------------------------


class TestUndecidedIsAllowedWithAWarning:
    """`undecided` means "no evidence", and no evidence is not a misfile.

    The gate refuses what the classifier *positively* places elsewhere. An item
    it cannot place at all goes where the caller said, annotated — otherwise a
    strict install would refuse a one-line fact and accept the same fact padded
    out past `MIN_CHARS_FOR_FALLBACK`, which is a rule about length, not about
    content.
    """

    def test_the_fixture_really_is_undecided(self, tmp_path):
        """Guards the tests below against a rule change in the classifier."""
        from layered_memory_mcp.routing import classify_memory_item

        decision = classify_memory_item(
            SHORT_UNMARKED_FACT,
            config=_strict(tmp_path),
            domain="infra",
            section="镜像仓库",
        )
        assert decision.surface.value == "undecided"
        assert decision.confidence_basis == "insufficient"
        assert decision.score == 0.0

    def test_a_short_unmarked_fact_is_written_not_refused(self, tmp_path):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg,
            domain="infra",
            section="镜像仓库",
            content=SHORT_UNMARKED_FACT,
        )
        assert res["success"] is True
        assert res["action"] != ROUTING_REFUSED_ACTION
        assert res["action"] in ("created", "section_created", "appended", "replaced")
        assert "## 镜像仓库" in (Path(cfg.knowledge_dir) / "infra.md").read_text(
            encoding="utf-8"
        )
        assert res["routing"]["surface"] == "undecided"

    def test_the_allowed_write_carries_the_insufficient_evidence_warning(
        self, tmp_path
    ):
        res = inject_knowledge(
            config=_strict(tmp_path),
            domain="infra",
            section="镜像仓库",
            content=SHORT_UNMARKED_FACT,
        )
        block = res["routing_enforcement"]
        assert block["gate_active"] is True
        assert block["insufficient_evidence"] is True
        assert "insufficient evidence" in block["note"]
        assert "the write was allowed and annotated" in block["note"]

        warning = res["routing_warning"]
        assert "INSUFFICIENT EVIDENCE" in warning
        assert "allowed and annotated rather than refused" in warning
        assert "routing_override='force_l1'" in warning

    def test_the_allowed_write_records_a_normal_changelog_event(self, tmp_path):
        """A real write is a real mutation — the warning does not change that."""
        cfg = _strict(tmp_path)
        inject_knowledge(
            config=cfg,
            domain="infra",
            section="镜像仓库",
            content=SHORT_UNMARKED_FACT,
            agent_id="tester",
        )
        events = _events(cfg)
        assert [e["operation"] for e in events] == ["l1_domain_created"]
        assert events[0]["tool"] == "inject_knowledge"

    def test_long_unmarked_prose_still_lands_as_l1_by_length_fallback(self, tmp_path):
        """The other half of the asymmetry that used to decide refusals."""
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg,
            domain="infra",
            section="镜像仓库",
            content=LONG_UNMARKED_PROSE,
        )
        assert res["success"] is True
        assert res["routing"]["surface"] == "l1_knowledge"
        assert res["routing"]["confidence_basis"] == "insufficient"
        assert "insufficient_evidence" not in res["routing_enforcement"]
        assert "allowed the write" in res["routing_enforcement"]["note"]
        assert "routing_warning" not in res
        assert [e["operation"] for e in _events(cfg)] == ["l1_domain_created"]

    def test_an_unmarked_fact_is_written_while_a_runbook_is_refused(self, tmp_path):
        """One config, both halves of the policy."""
        cfg = _strict(tmp_path)
        allowed = inject_knowledge(
            config=cfg,
            domain="infra",
            section="镜像仓库",
            content=SHORT_UNMARKED_FACT,
        )
        refused = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert allowed["success"] is True
        assert refused["action"] == ROUTING_REFUSED_ACTION
        assert refused["routing"]["surface"] == "skill"
        assert _knowledge_files(cfg) == ["infra.md"]
        assert [e["operation"] for e in _events(cfg)] == ["l1_domain_created"]

    @pytest.mark.parametrize(
        ("surface", "section", "content"),
        [
            ("todo", "待办", TODO_TEXT),
            ("ephemeral", "同步失败", LOG_TEXT),
            ("user_profile", "回答风格", PROFILE_TEXT),
        ],
    )
    def test_positive_non_l1_classifications_are_still_refused(
        self, tmp_path, surface, section, content
    ):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="ops", section=section, content=content
        )
        assert res["action"] == ROUTING_REFUSED_ACTION
        assert res["error"] == ROUTING_REFUSED_ERROR
        assert res["routing"]["surface"] == surface
        assert "insufficient_evidence" not in res["routing_enforcement"]
        assert _knowledge_files(cfg) == []
        assert _events(cfg) == [], "a refusal is not a mutation"

    def test_force_l1_on_an_undecided_item_is_a_no_op_that_says_so(self, tmp_path):
        """The override is not needed here, and the result does not pretend it was."""
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg,
            domain="infra",
            section="镜像仓库",
            content=SHORT_UNMARKED_FACT,
            routing_override="force_l1",
        )
        assert res["success"] is True
        assert "routing_override_applied" not in res
        assert res["routing_enforcement"]["override_requested"] == "force_l1"
        assert res["routing_enforcement"]["override_applied"] is False
        assert "it was not needed here" in res["routing_enforcement"]["note"]
        assert "would have REFUSED" not in res["routing_warning"]

    def test_capabilities_say_undecided_is_written_not_refused(self, tmp_path):
        note = (
            surface_capabilities(_strict(tmp_path))
            .to_dict()["core_surfaces"]["l1_knowledge"]["notes"]
        )
        assert "REFUSED" in note
        assert "'undecided'" in note
        assert "rather than refused" in note


# --- D. the gate lets L1 through, and the caller can insist ------------------


class TestStrictGateAllows:
    def test_l1_looking_content_writes_normally_and_records_provenance(
        self, tmp_path
    ):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg,
            domain="infra",
            section="路由边界",
            content=KNOWLEDGE_TEXT,
            agent_id="tester",
        )
        assert res["success"] is True
        assert res["action"] in ("created", "section_created", "appended", "replaced")
        assert "## 路由边界" in (Path(cfg.knowledge_dir) / "infra.md").read_text(
            encoding="utf-8"
        )
        assert res["routing"]["surface"] == "l1_knowledge"
        assert "routing_warning" not in res
        assert res["routing_enforcement"]["gate_active"] is True
        assert "allowed the write" in res["routing_enforcement"]["note"]

        events = _events(cfg)
        assert len(events) == 1
        assert events[0]["operation"] == "l1_domain_created"
        assert events[0]["tool"] == "inject_knowledge"
        assert events[0]["agent_id"] == "tester"

    def test_force_l1_writes_and_says_it_overrode(self, tmp_path):
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg,
            domain="ops",
            section="发布流程",
            content=SKILL_TEXT,
            routing_override="force_l1",
        )
        assert res["success"] is True
        assert res["action"] in ("created", "section_created", "appended", "replaced")
        assert res["routing_override_applied"] is True
        assert res["routing_enforcement"]["override_requested"] == "force_l1"
        assert res["routing_enforcement"]["override_applied"] is True
        assert "would have REFUSED" in res["routing_warning"]
        assert "## 发布流程" in (Path(cfg.knowledge_dir) / "ops.md").read_text(
            encoding="utf-8"
        )

    def test_an_overridden_write_is_a_real_write_in_the_changelog(self, tmp_path):
        """The override changes the gate's answer, not what a write *is*."""
        cfg = _strict(tmp_path)
        inject_knowledge(
            config=cfg,
            domain="ops",
            section="发布流程",
            content=SKILL_TEXT,
            routing_override="force_l1",
        )
        events = _events(cfg)
        assert [e["operation"] for e in events] == ["l1_domain_created"]
        assert SKILL_TEXT.strip() not in json.dumps(events, ensure_ascii=False)

    def test_routing_disabled_writes_like_legacy(self, tmp_path):
        cfg = _strict(tmp_path, enabled=False)
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["success"] is True
        assert res["action"] != ROUTING_REFUSED_ACTION
        assert "routing" not in res
        assert "routing_enforcement" not in res
        assert (Path(cfg.knowledge_dir) / "ops.md").exists()

    def test_a_broken_classifier_allows_the_write(self, tmp_path, monkeypatch):
        """Fail open: a refusal caused by the judge breaking is data loss."""
        from layered_memory_mcp import routing as routing_module

        def _boom(*args, **kwargs):
            raise RuntimeError("classifier exploded")

        monkeypatch.setattr(routing_module, "classify_memory_item", _boom)
        cfg = _strict(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["success"] is True
        assert res["action"] != ROUTING_REFUSED_ACTION
        assert res["routing_enforcement"]["gate_active"] is False
        assert "classifier exploded" in res["routing_enforcement"]["error"]


# --- E. the gate does not eat the existing contracts -------------------------


class TestExistingContractsSurvive:
    def test_validation_errors_keep_their_own_shape(self, tmp_path):
        cfg = _strict(tmp_path)
        assert inject_knowledge(
            config=cfg, domain="infra", section="x", content="   "
        ) == {"success": False, "error": "Content is empty"}
        assert inject_knowledge(
            config=cfg, domain="infra", section="  ", content=KNOWLEDGE_TEXT
        ) == {"success": False, "error": "Section heading cannot be empty"}
        bad = inject_knowledge(
            config=cfg, domain="../etc/passwd", section="x", content=KNOWLEDGE_TEXT
        )
        assert bad["success"] is False
        assert "path traversal blocked" in bad["error"]

    def test_deferred_consolidate_handshake_survives_the_gate(self, tmp_path):
        cfg = _strict(tmp_path)
        inject_knowledge(
            config=cfg,
            domain="reviews",
            section="路由边界 2026-10-01",
            content=KNOWLEDGE_TEXT,
        )
        before = _events(cfg)
        gated = inject_knowledge(
            config=cfg,
            domain="reviews",
            section="路由边界 2026-10-12",
            content=KNOWLEDGE_TEXT_2,
        )
        assert gated["action"] == "deferred_consolidate"
        assert gated["expected_hash"] and gated["family"] and gated["new_content"]
        assert gated["routing"]["surface"] == "l1_knowledge"
        assert gated["routing_enforcement"]["gate_active"] is True
        # A handshake is not a mutation, gate or no gate.
        assert _events(cfg) == before

    def test_fuse_conflict_still_refuses_with_its_own_shape(self, tmp_path):
        cfg = _strict(tmp_path)
        inject_knowledge(
            config=cfg, domain="infra", section="路由边界", content=KNOWLEDGE_TEXT
        )
        before = _events(cfg)
        res = inject_knowledge(
            config=cfg,
            domain="infra",
            section="路由边界",
            content=KNOWLEDGE_TEXT_2,
            fuse=True,
            expected_hash="0000000000000000",
        )
        assert res["action"] == "fuse_conflict"
        assert res["success"] is False
        assert _events(cfg) == before

    def test_fusion_writeback_commits_under_the_gate(self, tmp_path):
        cfg = _strict(tmp_path)
        inject_knowledge(
            config=cfg, domain="infra", section="路由边界", content=KNOWLEDGE_TEXT
        )
        body = (
            (Path(cfg.knowledge_dir) / "infra.md")
            .read_text(encoding="utf-8")
            .split("## 路由边界\n\n", 1)[1]
            .strip()
        )
        res = inject_knowledge(
            config=cfg,
            domain="infra",
            section="路由边界",
            content=KNOWLEDGE_TEXT + " " + KNOWLEDGE_TEXT_2,
            fuse=True,
            expected_hash=_hash_body(body),
        )
        assert res["action"] == "replaced"
        assert [e["operation"] for e in _events(cfg)][-1] == "l1_fusion_writeback"


# --- F. the gate does not reach into compaction ------------------------------


class TestCompactionIsGatedByModeInstead:
    def _bloated(self, tmp_path: Path, **routing) -> tuple[MemoryConfig, Path]:
        """A memory file holding one skill-shaped and one L1-shaped entry."""
        memory = tmp_path / "agent-memory.md"
        memory.write_text(
            "[L0] infra: 路由边界 → knowledge/infra.md\n"
            "§\n"
            f"[发布流程] {SKILL_TEXT}\n"
            "§\n"
            f"[路由边界] {KNOWLEDGE_TEXT}\n",
            encoding="utf-8",
        )
        cfg = _cfg(
            tmp_path,
            routing={"enforce_direct_writes": True, **routing},
            agent_memory={"path": str(memory)},
        )
        return cfg, memory

    def test_advisory_compaction_still_migrates_under_the_direct_gate(
        self, tmp_path
    ):
        """`enforce_direct_writes` gates the caller's writes, not compaction's.

        Compaction already has a gate (`routing.mode`). Letting the direct-write
        flag silently re-decide what a compaction pass migrates would mean one
        config key changing two unrelated behaviours.
        """
        from layered_memory_mcp.memory_compactor import compact_memory

        cfg, memory = self._bloated(tmp_path)
        report = compact_memory(config=cfg, dry_run=False, memory_path=str(memory))
        assert report["success"] is True
        assert report["routing_mode"] == "advisory"
        assert report["migrated_count"] == 2
        assert report["error_count"] == 0
        surfaces = {a["routed_surface"] for a in report["routing_advisory"]}
        assert "skill" in surfaces, "the advisory pass still reports the misfile"
        assert _knowledge_files(cfg), "advisory compaction wrote L1 as before"

    def test_enforce_mode_is_what_blocks_compaction(self, tmp_path):
        from layered_memory_mcp.memory_compactor import compact_memory

        cfg, memory = self._bloated(tmp_path, mode="enforce")
        report = compact_memory(config=cfg, dry_run=False, memory_path=str(memory))
        assert report["routing_mode"] == "enforce"
        assert report["routing_summary"]["blocked_from_l1"] >= 1
        assert any(
            c["surface"] == "skill" and c["status"] == "blocked_from_l1"
            for c in report["agent_owned_candidates"]
        )


# --- G. the MCP surface ------------------------------------------------------


class TestMcpTool:
    def _run(self, cfg, coro_factory):
        from layered_memory_mcp import server

        server._config = cfg
        try:
            return json.loads(asyncio.run(coro_factory(server)))
        finally:
            server._config = None

    def test_old_call_without_the_argument_still_works(self, tmp_path):
        cfg = _strict(tmp_path)
        payload = self._run(
            cfg,
            lambda s: s.inject_knowledge_tool(
                domain="infra", section="路由边界", content=KNOWLEDGE_TEXT
            ),
        )
        assert payload["success"] is True
        assert payload["routing_enforcement"]["override_requested"] == "none"

    def test_tool_refuses_a_non_l1_write_and_keeps_the_shape(self, tmp_path):
        cfg = _strict(tmp_path)
        payload = self._run(
            cfg,
            lambda s: s.inject_knowledge_tool(
                domain="ops", section="发布流程", content=SKILL_TEXT
            ),
        )
        assert payload["success"] is False
        assert payload["action"] == ROUTING_REFUSED_ACTION
        assert payload["error"] == ROUTING_REFUSED_ERROR
        assert payload["suggested_action"]["surface"] == "skill"
        assert _knowledge_files(cfg) == []

    def test_tool_accepts_the_override(self, tmp_path):
        cfg = _strict(tmp_path)
        payload = self._run(
            cfg,
            lambda s: s.inject_knowledge_tool(
                domain="ops",
                section="发布流程",
                content=SKILL_TEXT,
                routing_override="force_l1",
            ),
        )
        assert payload["success"] is True
        assert payload["routing_override_applied"] is True

    def test_tool_rejects_an_unknown_override(self, tmp_path):
        cfg = _strict(tmp_path)
        payload = self._run(
            cfg,
            lambda s: s.inject_knowledge_tool(
                domain="ops",
                section="发布流程",
                content=SKILL_TEXT,
                routing_override="sure",
            ),
        )
        assert payload["success"] is False
        assert "Invalid routing_override" in payload["error"]
        assert _knowledge_files(cfg) == []

    def test_batch_carries_the_refusal_per_item(self, tmp_path):
        cfg = _strict(tmp_path)
        payload = self._run(
            cfg,
            lambda s: s.batch_inject_knowledge_tool(
                items=[
                    {
                        "domain": "infra",
                        "section": "路由边界",
                        "content": KNOWLEDGE_TEXT,
                    },
                    {"domain": "ops", "section": "发布流程", "content": SKILL_TEXT},
                ]
            ),
        )
        by_domain = {r["domain"]: r for r in payload["results"]}
        assert by_domain["infra"]["success"] is True
        refused = by_domain["ops"]
        assert refused["success"] is False
        assert refused["action"] == ROUTING_REFUSED_ACTION
        assert refused["error"] == ROUTING_REFUSED_ERROR
        assert refused["suggested_action"]["surface"] == "skill"
        assert refused["routing_enforcement"]["gate_active"] is True
        assert _knowledge_files(cfg) == ["infra.md"]

    def test_batch_override_applies_to_every_item(self, tmp_path):
        cfg = _strict(tmp_path)
        payload = self._run(
            cfg,
            lambda s: s.batch_inject_knowledge_tool(
                items=[
                    {"domain": "ops", "section": "发布流程", "content": SKILL_TEXT}
                ],
                routing_override="force_l1",
            ),
        )
        item = payload["results"][0]
        assert item["success"] is True
        assert item["routing_override_applied"] is True

    def test_capabilities_tool_reports_the_gate(self, tmp_path):
        cfg = _strict(tmp_path)
        payload = self._run(cfg, lambda s: s.get_memory_surface_capabilities())
        assert payload["routing_enforce_direct_writes"] is True
        assert payload["routing_mode"] == "advisory"

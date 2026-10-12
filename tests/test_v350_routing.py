"""Common-core persistence routing + opt-in agent extension surfaces.

Why this file exists: everything an agent wanted to remember used to end up in
L1, because L1 was the only thing the framework could write. A runbook, a
standing user preference and a raw trace are not durable domain knowledge, and
burying them in L1 is how a knowledge base turns into a junk drawer.

The surface model splits them:
  * framework-owned common core — L0_INDEX / L1_KNOWLEDGE / TODO /
    MEMORY_CHANGELOG — the framework may write these;
  * agent-owned extension surfaces — SKILL / USER_PROFILE / AGENT_OTHER — the
    framework classifies and *suggests*, and only applies them when the operator
    has explicitly opted in (``agent_extensions`` + a handler). Ownership never
    moves;
  * no durable write — UNDECIDED / EPHEMERAL.

The tests below pin the three properties that make this safe to ship on top of
an existing knowledge base: classification is deterministic, defaults write
nothing new, and advisory routing does not touch the existing write contracts.
"""

from __future__ import annotations

import json

from layered_memory_mcp.adapters import apply_agent_action, registry, resolve_adapter
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.injector import inject_knowledge
from layered_memory_mcp.memory_changelog import (
    NOT_RECORDED,
    build_entry,
    is_recordable,
)
from layered_memory_mcp.models import KnowledgeEntry, KnowledgeType
from layered_memory_mcp.routing import (
    AgentAction,
    MemorySurface,
    SurfaceOwnership,
    classify_memory_item,
    ownership_of,
    surface_capabilities,
)


def _cfg(tmp_path, **kwargs) -> MemoryConfig:
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home), knowledge_dir=str(home / "knowledge"), **kwargs
    )


# --- fixtures: one representative item per surface ---------------------------

SKILL_TEXT = """发布流程（每次发版都按这个来）：

1. 运行 pytest -q，确认全绿
2. 执行 python -m build 打包
3. git tag v3.5.0 && git push --tags
4. 上传后在 PyPI 页面核对版本号
"""

SKILL_TEXT_EN = """How to rotate the API credentials:

- [ ] run `python scripts/rotate.py --dry-run`
- [ ] execute the rollback plan if the dry run reports drift
- [ ] restart the worker pool
"""

TODO_TEXT = (
    "待验证：enforce 模式下的写入拒绝行为尚未接入，需要用户拍板之后再继续；"
    "在那之前这条一直是阻塞状态。"
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

# The trap case: architectural prose that *mentions* 验证 / 流程 / 步骤 but has no
# procedural structure at all. Marker-only matching would call this a skill.
KNOWLEDGE_WITH_ACTION_WORDS = (
    "知识治理的边界约定：验证由 governance 负责，流程的步骤顺序由调用方决定，"
    "框架本身不执行。这里记录的是当前的架构事实和职责划分，不是操作手册；"
    "核心决策是 routing 永远不写 agent 自己的存储。"
)

LOG_TEXT = """08:12:01 INFO: starting scan of 14 domains
08:12:02 DEBUG: loaded vectors.db (512 dim)
08:12:03 ERROR: vector sync failed for infra
Traceback (most recent call last):
  File "injector.py", line 884
"""


# --- A. classification -------------------------------------------------------

class TestClassification:
    def test_procedure_with_steps_routes_to_agent_owned_skill(self):
        d = classify_memory_item(SKILL_TEXT)
        assert d.surface is MemorySurface.SKILL
        assert d.ownership is SurfaceOwnership.AGENT
        # markers alone are never enough; structure must have matched too
        assert d.confidence_basis == "mixed"
        assert d.suggested_action is not None
        assert d.suggested_action.kind == "agent_native_write"
        # the framework hands back the caller's own text, never invented prose
        assert d.suggested_action.source_content == SKILL_TEXT.strip()
        assert "steps" in d.suggested_action.shape_hint

    def test_checklist_with_commands_routes_to_skill(self):
        d = classify_memory_item(SKILL_TEXT_EN)
        assert d.surface is MemorySurface.SKILL
        assert {"marker", "structure"} <= {s.kind for s in d.signals}

    def test_pending_blocked_work_routes_to_framework_owned_todo(self):
        d = classify_memory_item(TODO_TEXT)
        assert d.surface is MemorySurface.TODO
        assert d.ownership is SurfaceOwnership.FRAMEWORK
        # a core surface needs no agent action — the framework owns the write
        assert d.suggested_action is None
        assert d.framework_can_apply is True
        assert d.agent_should_apply is False

    def test_global_user_preference_routes_to_profile_and_needs_confirmation(self):
        d = classify_memory_item(PROFILE_TEXT)
        assert d.surface is MemorySurface.USER_PROFILE
        assert d.ownership is SurfaceOwnership.AGENT
        assert d.suggested_action is not None
        assert d.suggested_action.requires_user_confirmation is True

    def test_architecture_and_decisions_route_to_l1_knowledge(self):
        d = classify_memory_item(KNOWLEDGE_TEXT)
        assert d.surface is MemorySurface.L1_KNOWLEDGE
        assert d.ownership is SurfaceOwnership.FRAMEWORK
        assert d.suggested_action is None

    def test_action_words_without_structure_stay_l1_knowledge(self):
        """验证/流程/步骤 in prose is knowledge, not a procedure."""
        d = classify_memory_item(KNOWLEDGE_WITH_ACTION_WORDS)
        assert d.surface is MemorySurface.L1_KNOWLEDGE
        # and the near-miss is recorded rather than silently dropped
        assert any(
            s.kind == "negative" and s.name == "skill_markers_without_structure"
            for s in d.signals
        )

    def test_raw_trace_is_not_durable(self):
        d = classify_memory_item(LOG_TEXT)
        assert d.surface in (MemorySurface.EPHEMERAL, MemorySurface.UNDECIDED)
        assert d.ownership is SurfaceOwnership.NONE
        assert d.framework_can_apply is False

    def test_short_signalless_item_is_undecided(self):
        d = classify_memory_item("ok 了")
        assert d.surface is MemorySurface.UNDECIDED
        assert d.confidence_basis == "insufficient"

    def test_empty_content_is_undecided_not_an_error(self):
        d = classify_memory_item("   ")
        assert d.surface is MemorySurface.UNDECIDED
        assert d.score == 0.0

    def test_score_is_a_rule_weight_in_range(self):
        for text in (SKILL_TEXT, TODO_TEXT, PROFILE_TEXT, KNOWLEDGE_TEXT, LOG_TEXT):
            assert 0.0 <= classify_memory_item(text).score <= 1.0

    def test_ownership_is_derived_never_hand_set(self):
        for surface in MemorySurface:
            owner = ownership_of(surface)
            assert isinstance(owner, SurfaceOwnership)
        assert ownership_of(MemorySurface.SKILL) is SurfaceOwnership.AGENT
        assert ownership_of(MemorySurface.L1_KNOWLEDGE) is SurfaceOwnership.FRAMEWORK


class TestCallerContextDoesNotLeakIntoClassification:
    def test_agent_type_is_echo_only(self):
        """agent_type is statistics. It must not steer the decision."""
        import asyncio

        from layered_memory_mcp import server

        async def _run(agent_type):
            return json.loads(
                await server.classify_memory_item(SKILL_TEXT, agent_type=agent_type)
            )

        a = asyncio.run(_run("hermes"))
        b = asyncio.run(_run("some-other-agent"))
        c = asyncio.run(_run(None))
        assert a["echo"]["agent_type"] == "hermes"
        decisions = [{k: v for k, v in r.items() if k != "echo"} for r in (a, b, c)]
        assert decisions[0] == decisions[1] == decisions[2]

    def test_unsupported_surface_yields_no_impossible_action(self):
        d = classify_memory_item(
            SKILL_TEXT, supported_surfaces=["l1_knowledge", "todo"]
        )
        # the classification itself is unchanged — only the action is withheld
        assert d.surface is MemorySurface.SKILL
        assert d.suggested_action is None
        assert d.agent_should_apply is False
        assert any("not in the caller's supported surfaces" in w for w in d.warnings)

    def test_supported_surface_still_yields_an_action(self):
        d = classify_memory_item(SKILL_TEXT, supported_surfaces=["skill"])
        assert d.suggested_action is not None

    def test_unknown_surface_names_are_reported_not_fatal(self):
        """A typo'd surface list is a constraint we cannot satisfy, not a free pass."""
        d = classify_memory_item(SKILL_TEXT, supported_surfaces=["not_a_surface"])
        assert d.surface is MemorySurface.SKILL
        assert d.suggested_action is None
        assert any("unknown supported_surfaces" in w for w in d.warnings)


# --- B. config defaults ------------------------------------------------------

class TestConfigDefaults:
    def test_routing_defaults_to_enabled_and_advisory(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert cfg.routing_enabled is True
        assert cfg.routing_mode == "advisory"

    def test_agent_extensions_default_to_disabled_suggest_only(self, tmp_path):
        cfg = _cfg(tmp_path)
        ax = cfg.agent_extensions
        assert ax["enabled"] is False
        assert ax["mode"] == "suggest"
        assert ax["adapter"] is None
        for surface in ("skill", "user_profile", "agent_other"):
            assert ax["surfaces"][surface] == {"enabled": False, "handler": None}

    def test_malformed_sections_fall_back_per_key_without_raising(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            routing={"enabled": "yes", "mode": "nonsense"},
            agent_extensions={
                "enabled": True,
                "mode": "launch_the_missiles",
                "surfaces": {"skill": "not-a-mapping", "bogus": {"enabled": True}},
            },
        )
        assert cfg.routing_enabled is True
        assert cfg.routing_mode == "advisory"          # invalid → safe default
        assert cfg.agent_extensions["mode"] == "suggest"
        # unknown surface names are dropped, known ones stay conservative
        assert "bogus" not in cfg.agent_extensions["surfaces"]
        assert cfg.agent_extensions["surfaces"]["skill"]["handler"] is None

    def test_yaml_sections_are_read_from_config_yaml(self, tmp_path):
        home = tmp_path / ".layered-memory"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            "routing:\n"
            "  mode: enforce\n"
            "  extra_skill_markers: [祭祀流程]\n"
            "agent_extensions:\n"
            "  enabled: true\n"
            "  mode: apply\n"
            "  adapter: generic\n"
            "  surfaces:\n"
            "    skill:\n"
            "      enabled: true\n"
            "      handler: my-skill-writer\n",
            encoding="utf-8",
        )
        cfg = MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"))
        assert cfg.routing_mode == "enforce"
        assert cfg.routing_markers["extra_skill_markers"] == ["祭祀流程"]
        assert cfg.agent_extensions["surfaces"]["skill"]["handler"] == "my-skill-writer"

    def test_existing_configs_keep_working(self, tmp_path):
        """No new required key: a config written before routing existed still builds."""
        cfg = _cfg(tmp_path, memory_mode="index_only", write_guard="off")
        assert cfg.memory_mode == "index_only"
        assert cfg.routing_enabled is True


# --- C. capability reflection ------------------------------------------------

class TestCapabilityReflection:
    def test_default_capabilities_are_core_only(self, tmp_path):
        caps = surface_capabilities(_cfg(tmp_path)).to_dict()
        assert caps["agent_extensions_enabled"] is False
        assert caps["routing_mode"] == "advisory"
        for name, cap in caps["core_surfaces"].items():
            assert cap["owned_by"] == "framework", name
            assert cap["can_apply"] is True, name
        for name, cap in caps["extension_surfaces"].items():
            assert cap["owned_by"] == "agent", name
            assert cap["can_apply"] is False, name
            assert cap["default_behavior"] == "suggest", name
        assert caps["source_owned_evidence"] == ["raw_session"]
        assert "l1_knowledge" in caps["core_surfaces"]
        assert "memory_changelog" in caps["core_surfaces"]
        assert "raw_session" not in caps["core_surfaces"]

    def test_opt_in_makes_an_extension_appliable_without_moving_ownership(
        self, tmp_path
    ):
        cfg = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "mode": "apply",
                "adapter": "generic",
                "surfaces": {
                    "skill": {"enabled": True, "handler": "my-skill-writer"},
                    "user_profile": {"enabled": False, "handler": None},
                },
            },
        )
        caps = surface_capabilities(cfg).to_dict()
        skill = caps["extension_surfaces"]["skill"]
        assert skill["can_apply"] is True
        assert skill["owned_by"] == "agent"          # ownership never moves
        assert skill["configured_handler"] == "my-skill-writer"
        # a surface left disabled stays suggestion-only
        assert caps["extension_surfaces"]["user_profile"]["can_apply"] is False

    def test_enabled_surface_without_handler_cannot_be_applied(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "mode": "apply",
                "surfaces": {"skill": {"enabled": True, "handler": None}},
            },
        )
        caps = surface_capabilities(cfg).to_dict()
        assert caps["extension_surfaces"]["skill"]["can_apply"] is False

    def test_suggest_mode_blocks_apply_even_with_a_handler(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "mode": "suggest",
                "surfaces": {"skill": {"enabled": True, "handler": "writer"}},
            },
        )
        assert surface_capabilities(cfg).to_dict()["extension_surfaces"]["skill"][
            "can_apply"
        ] is False

    def test_decision_capability_fields_follow_config(self, tmp_path):
        plain = classify_memory_item(SKILL_TEXT, config=_cfg(tmp_path))
        assert plain.framework_can_apply is False
        assert plain.agent_should_apply is True
        assert plain.application_mode == "suggest"

        opted_in = classify_memory_item(
            SKILL_TEXT,
            config=_cfg(
                tmp_path / "b",
                agent_extensions={
                    "enabled": True,
                    "mode": "apply",
                    "surfaces": {"skill": {"enabled": True, "handler": "writer"}},
                },
            ),
        )
        assert opted_in.framework_can_apply is True
        assert opted_in.agent_should_apply is False
        assert opted_in.ownership is SurfaceOwnership.AGENT
        assert opted_in.configured_handler == "writer"


# --- D. the opt-in execution layer refuses by default ------------------------

class TestAgentActionApplication:
    def _action(self) -> AgentAction:
        return AgentAction(
            surface=MemorySurface.SKILL,
            operation="create",
            source_content=SKILL_TEXT,
            rationale="test",
        )

    def test_no_adapter_is_registered_by_default(self):
        assert registry.names() == []

    def test_apply_refuses_when_extensions_are_disabled(self, tmp_path):
        res = apply_agent_action(self._action(), _cfg(tmp_path))
        assert res.applied is False
        assert res.status == "disabled"

    def test_apply_refuses_when_no_adapter_matches_the_handler(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "mode": "apply",
                "surfaces": {"skill": {"enabled": True, "handler": "ghost"}},
            },
        )
        res = apply_agent_action(self._action(), cfg)
        assert res.applied is False
        assert res.status == "unsupported"
        assert resolve_adapter(cfg, MemorySurface.SKILL) is None

    def test_apply_refuses_core_surfaces_outright(self, tmp_path):
        action = AgentAction(
            surface=MemorySurface.L1_KNOWLEDGE,
            operation="create",
            source_content="x",
            rationale="test",
        )
        res = apply_agent_action(action, _cfg(tmp_path))
        assert res.status == "refused"


# --- E. advisory inject keeps every existing contract ------------------------

class TestAdvisoryInject:
    def test_normal_l1_write_is_unchanged_and_annotated(self, tmp_path):
        cfg = _cfg(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="infra", section="路由边界", content=KNOWLEDGE_TEXT
        )
        assert res["success"] is True
        assert res["action"] in ("created", "section_created", "appended", "replaced")
        assert (cfg.knowledge_dir / "infra.md").exists()
        assert res["routing"]["surface"] == "l1_knowledge"
        assert res["routing"]["mode"] == "advisory"
        # L1-looking content gets no scolding
        assert "routing_warning" not in res

    def test_non_l1_write_is_warned_but_still_written(self, tmp_path):
        cfg = _cfg(tmp_path)
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        # advisory means advisory: the write happened, with its usual action
        assert res["success"] is True
        assert res["action"] in ("created", "section_created", "appended", "replaced")
        assert (cfg.knowledge_dir / "ops.md").read_text(encoding="utf-8")
        assert res["routing"]["surface"] == "skill"
        assert res["routing"]["ownership"] == "agent"
        assert "routing_warning" in res
        assert "NOT changed" in res["routing_warning"]

    def test_enforce_mode_still_does_not_block_a_write(self, tmp_path):
        """The schema knows 'enforce'; the write path does not act on it yet."""
        cfg = _cfg(tmp_path, routing={"mode": "enforce"})
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["success"] is True
        assert res["routing"]["mode"] == "enforce"
        assert "not yet acted on" in res["routing_warning"]

    def test_routing_can_be_switched_off_entirely(self, tmp_path):
        cfg = _cfg(tmp_path, routing={"enabled": False})
        res = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        assert res["success"] is True
        assert "routing" not in res

    def test_deferred_consolidate_handshake_survives_the_advisory_layer(
        self, tmp_path
    ):
        """Routing annotates a handshake response; it must not eat or rename it."""
        cfg = _cfg(tmp_path)
        inject_knowledge(
            config=cfg,
            domain="ops",
            section="发布流程 2026-10-01",
            content=KNOWLEDGE_TEXT,
        )
        gated = inject_knowledge(
            config=cfg,
            domain="ops",
            section="发布流程 2026-10-12",
            content=KNOWLEDGE_TEXT + " 本期补充：改用 uv 构建。",
        )
        assert gated["action"] == "deferred_consolidate"
        # every field of the contract is still there, under its own name
        assert gated["expected_hash"]
        assert gated["family_size"] >= 1
        assert gated["family"]
        assert gated["new_content"]
        assert gated["hint"]
        assert "routing" in gated

    def test_advisory_layer_adds_only_routing_keys(self, tmp_path, monkeypatch):
        """Pin the wrapper's contract: it annotates, it never edits."""
        from layered_memory_mcp import injector

        canned = {
            "success": True,
            "action": "deferred_fusion",
            "needs_fusion": True,
            "expected_hash": "abc123",
            "fusion": {"hit_section": "WSL 代理", "old_body": "old", "cosine": 0.81},
            "l0_synced": False,
        }
        monkeypatch.setattr(
            injector, "_inject_knowledge_core", lambda **kwargs: dict(canned)
        )
        out = inject_knowledge(
            config=_cfg(tmp_path), domain="infra", section="WSL 代理", content=SKILL_TEXT
        )
        for key, value in canned.items():
            assert out[key] == value
        assert set(out) - set(canned) <= {"routing", "routing_warning"}

    def test_empty_content_error_shape_is_untouched(self, tmp_path):
        res = inject_knowledge(
            config=_cfg(tmp_path), domain="infra", section="x", content="   "
        )
        assert res == {"success": False, "error": "Content is empty"}


class TestBatchInjectPropagatesRouting:
    def test_each_item_carries_its_own_routing(self, tmp_path):
        import asyncio

        from layered_memory_mcp import server

        cfg = _cfg(tmp_path)
        server._config = cfg
        try:
            payload = json.loads(
                asyncio.run(
                    server.batch_inject_knowledge_tool(
                        items=[
                            {
                                "domain": "infra",
                                "section": "路由边界",
                                "content": KNOWLEDGE_TEXT,
                            },
                            {
                                "domain": "ops",
                                "section": "发布流程",
                                "content": SKILL_TEXT,
                            },
                        ]
                    )
                )
            )
        finally:
            server._config = None

        assert payload["success"] is True
        by_domain = {r["domain"]: r for r in payload["results"]}
        assert by_domain["infra"]["routing"]["surface"] == "l1_knowledge"
        assert by_domain["ops"]["routing"]["surface"] == "skill"
        assert "routing_warning" in by_domain["ops"]
        assert "routing_warning" not in by_domain["infra"]

    def test_batch_is_not_a_quieter_write_path(self, tmp_path):
        """Whatever a single inject reports for an item, the batch reports too."""
        import asyncio

        from layered_memory_mcp import server

        cfg = _cfg(tmp_path)
        single = inject_knowledge(
            config=cfg, domain="ops", section="发布流程", content=SKILL_TEXT
        )
        cfg2 = _cfg(tmp_path / "second")
        server._config = cfg2
        try:
            payload = json.loads(
                asyncio.run(
                    server.batch_inject_knowledge_tool(
                        items=[
                            {
                                "domain": "ops",
                                "section": "发布流程",
                                "content": SKILL_TEXT,
                            }
                        ]
                    )
                )
            )
        finally:
            server._config = None
        batched = payload["results"][0]
        assert batched["action"] == single["action"]
        assert batched["routing"]["surface"] == single["routing"]["surface"]


# --- F. governance: procedure contamination (advisory) -----------------------

class TestProcedureContamination:
    def test_runbook_section_in_l1_is_flagged(self, tmp_path):
        from layered_memory_mcp.governance import post_write_governance

        cfg = _cfg(tmp_path)
        fp = cfg.knowledge_dir / "ops.md"
        text = f"# ops\n\n## 架构边界\n{KNOWLEDGE_TEXT}\n\n## 发布流程\n{SKILL_TEXT}\n"
        fp.write_text(text, encoding="utf-8")

        report = post_write_governance(cfg, "ops", fp, text, "update")
        flagged = report["procedure_contamination"]
        assert flagged["summary"]["sections_flagged"] == 1
        finding = flagged["findings"][0]
        assert finding["heading"] == "发布流程"
        assert finding["ownership"] == "agent"
        assert finding["suggested_action"] == "extract_to_agent_skill_surface"
        assert any("action protocols" in s for s in report["suggestions"])

    def test_pure_knowledge_file_is_not_flagged(self, tmp_path):
        from layered_memory_mcp.governance import post_write_governance

        cfg = _cfg(tmp_path)
        fp = cfg.knowledge_dir / "infra.md"
        text = f"# infra\n\n## 架构边界\n{KNOWLEDGE_WITH_ACTION_WORDS}\n"
        fp.write_text(text, encoding="utf-8")

        report = post_write_governance(cfg, "infra", fp, text, "update")
        assert report["procedure_contamination"]["summary"]["sections_flagged"] == 0


# --- G. MEMORY_CHANGELOG is provenance, not an event log ---------------------

class TestMemoryChangelogSchema:
    def test_semantic_l1_mutations_are_recordable(self):
        assert is_recordable("l1_section_updated")
        assert is_recordable("l0_entry_renamed")

    def test_derived_rebuilds_and_todo_lifecycle_are_not(self):
        for op in ("vector_reindex", "todo_status_changed", "raw_session_scan"):
            assert op in NOT_RECORDED
            assert not is_recordable(op)

    def test_unknown_operations_are_refused_rather_than_logged(self):
        assert not is_recordable("something_happened")
        assert build_entry("something_happened", "infra") is None

    def test_entry_shape(self):
        entry = build_entry(
            "l1_section_merged", "infra", section="WSL 代理", reason="fusion write-back"
        )
        assert entry is not None
        payload = entry.to_dict()
        assert payload["surface"] == "l1_knowledge"
        assert payload["schema_version"] == 1
        assert payload["domain"] == "infra"


# --- H. legacy readability ---------------------------------------------------

class TestLegacyProcedureType:
    def test_procedure_value_still_exists(self):
        assert KnowledgeType.PROCEDURE.value == "procedure"
        assert KnowledgeType("procedure") is KnowledgeType.PROCEDURE

    def test_legacy_frontmatter_still_loads(self):
        markdown = (
            "---\n"
            "domain: ops\n"
            "section: 发布流程\n"
            "type: procedure\n"
            "confidence: 0.8\n"
            "---\n\n"
            "## 发布流程\n\n"
            "1. 运行 pytest\n2. 打包上传\n"
        )
        entry = KnowledgeEntry.from_markdown(markdown, domain="ops")
        assert entry.type is KnowledgeType.PROCEDURE
        assert "pytest" in entry.content


# --- I. compact_memory advisory ---------------------------------------------

class TestCompactMemoryRoutingAdvisory:
    def test_non_l1_entries_are_reported_but_still_migrated(self, tmp_path):
        from layered_memory_mcp.memory_compactor import compact_memory

        cfg = _cfg(tmp_path)
        mem = tmp_path / "MEMORY.md"
        mem.write_text(
            "[L0] infra: servers → knowledge/infra.md\n§\n[发布流程] " + SKILL_TEXT,
            encoding="utf-8",
        )

        report = compact_memory(cfg, memory_path=mem, dry_run=True)
        assert report["success"] is True
        assert report["migrated_count"] == 1
        advisory = report["routing_advisory"]
        assert len(advisory) == 1
        assert advisory[0]["routed_surface"] == "skill"
        assert advisory[0]["ownership"] == "agent"
        assert "advisory" in advisory[0]["note"]

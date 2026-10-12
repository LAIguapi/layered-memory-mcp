"""The optional `apply_agent_action` execution path.

WHAT THESE TESTS PIN
--------------------
`skill`, `user_profile` and `agent_other` are agent-owned surfaces. The
framework may *apply* an action on one of them only as an explicitly configured
orchestrator, and ownership never moves. So the properties worth proving are
mostly negative:

  * the default configuration writes **nothing** and says why;
  * every link of the opt-in chain, removed on its own, stops the write
    (master switch, per-surface switch, mode, handler name, handler
    registration, explicit target path);
  * confirmation semantics are real: `apply_with_confirmation` without
    `confirmed=True` returns `confirmation_required` and leaves the file absent;
  * `dry_run` reports what it would do and touches no filesystem;
  * an invalid action is refused, never repaired into something writable;
  * the reference `generic_file` handler refuses implicit, escaping and
    non-prose targets, so "explicitly configured" is the only way in;
  * applying an agent-owned action records **no** MEMORY_CHANGELOG event — that
    log is provenance for framework-owned L0/L1 mutations only;
  * the capability output agrees with the execution path, blocker for blocker.

Every path written here is under `tmp_path`. Nothing in this file resolves a
production location, and the one test that mentions absolute system directories
only asks the resolver to *refuse* them.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from layered_memory_mcp.adapters import apply_agent_action, registry
from layered_memory_mcp.adapters.base import (
    STATUS_APPLIED,
    STATUS_CONFIRMATION_REQUIRED,
    STATUS_DISABLED,
    STATUS_INVALID_ACTION,
    STATUS_REFUSED,
    STATUS_UNSUPPORTED,
    STATUS_WOULD_APPLY,
    validate_action,
)
from layered_memory_mcp.adapters.generic_file import (
    HANDLER_NAME,
    SECTION_MARKER,
    GenericFileAdapter,
    resolve_target,
)
from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.routing import (
    AgentAction,
    MemorySurface,
    SurfaceOwnership,
    classify_memory_item,
    extension_readiness,
    surface_capabilities,
)

SKILL_TEXT = """发布流程（每次发版都按这个来）：

1. 运行 pytest -q，确认全绿
2. 执行 python -m build 打包
3. git tag v3.5.4 && git push --tags
4. 上传后在 PyPI 页面核对版本号
"""


# --- helpers -----------------------------------------------------------------


def _cfg(tmp_path: Path, **kwargs) -> MemoryConfig:
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), knowledge_dir=str(home / "knowledge"), **kwargs)


def _ext_cfg(
    tmp_path: Path,
    *,
    mode: str = "apply",
    enabled: bool = True,
    surface_enabled: bool = True,
    handler: str | None = HANDLER_NAME,
    path: str | Path | None = None,
    root: str | Path | None = None,
    surface: str = "skill",
) -> MemoryConfig:
    """A config whose only write target is inside ``tmp_path``."""
    if path is None and root is None:
        path = tmp_path / "ext" / "skills.md"
    entry: dict = {"enabled": surface_enabled, "handler": handler}
    if path is not None:
        entry["path"] = str(path)
    return _cfg(
        tmp_path,
        agent_extensions={
            "enabled": enabled,
            "mode": mode,
            "adapter": HANDLER_NAME,
            "root": str(root) if root is not None else None,
            "surfaces": {surface: entry},
        },
    )


def _action(
    surface: str = "skill",
    operation: str = "create",
    content: str = SKILL_TEXT,
    title: str | None = "发布流程",
    **kwargs,
) -> AgentAction:
    return AgentAction(
        surface=MemorySurface(surface),
        operation=operation,
        title=title,
        source_content=content,
        shape_hint=["title", "steps", "verification"],
        rationale="test fixture: repeatable action protocol",
        **kwargs,
    )


def _target(tmp_path: Path) -> Path:
    return tmp_path / "ext" / "skills.md"


def _written_files(tmp_path: Path) -> list[Path]:
    """Every file under ``tmp_path`` that is not part of the framework home."""
    home = tmp_path / ".layered-memory"
    return sorted(
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and home not in p.parents and p != home
    )


@contextmanager
def _registered(adapter):
    registry.register(adapter)
    try:
        yield adapter
    finally:
        registry.unregister(adapter.name)


# --- 1-3. the opt-in chain, link by link -------------------------------------


class TestDefaultsWriteNothing:
    def test_default_config_refuses_and_writes_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_DISABLED
        assert "extensions_disabled" in res.blockers
        assert res.detail                      # a refusal is always explained
        assert _written_files(tmp_path) == []

    def test_extensions_enabled_but_surface_disabled_refuses(self, tmp_path):
        cfg = _ext_cfg(tmp_path, surface_enabled=False)
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_DISABLED
        assert "surface_disabled" in res.blockers
        assert not _target(tmp_path).exists()

    def test_enabled_surface_without_a_handler_refuses(self, tmp_path):
        cfg = _ext_cfg(tmp_path, handler=None)
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_UNSUPPORTED
        assert res.blockers == ["no_handler"]
        assert HANDLER_NAME in res.detail       # tells you what you may configure
        assert not _target(tmp_path).exists()

    def test_handler_nobody_registered_refuses(self, tmp_path):
        cfg = _ext_cfg(tmp_path, handler="ghost-writer")
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_UNSUPPORTED
        assert res.blockers == ["handler_not_registered"]
        assert "ghost-writer" in res.detail
        assert not _target(tmp_path).exists()

    def test_handler_that_does_not_support_the_surface_refuses(self, tmp_path):
        class _SkillOnly:
            name = "skill-only"

            def supports(self, surface) -> bool:
                return MemorySurface(surface) is MemorySurface.SKILL

            def apply(self, action, config=None, *, dry_run=False):
                raise AssertionError("must not be invoked")

        cfg = _ext_cfg(
            tmp_path,
            handler="skill-only",
            surface="user_profile",
            path=tmp_path / "ext" / "profile.md",
        )
        with _registered(_SkillOnly()):
            res = apply_agent_action(_action(surface="user_profile"), cfg)
        assert res.applied is False
        assert res.status == STATUS_UNSUPPORTED
        assert res.blockers == ["handler_cannot_write_surface"]

    def test_core_surfaces_are_refused_outright(self, tmp_path):
        res = apply_agent_action(
            _action(surface="l1_knowledge", content="架构决策：X 由 Y 负责"),
            _ext_cfg(tmp_path),
        )
        assert res.applied is False
        assert res.status == STATUS_REFUSED
        assert res.blockers == ["not_an_extension_surface"]
        assert "framework's own tools" in res.detail


# --- 4. suggest mode is recommendation-only ----------------------------------


class TestSuggestMode:
    def test_suggest_mode_writes_nothing_even_when_fully_wired(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="suggest")
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_DISABLED
        assert "mode_suggest" in res.blockers
        assert res.metadata["recommendation_only"] is True
        assert not _target(tmp_path).exists()

    def test_suggest_mode_is_the_default_for_a_half_written_section(self, tmp_path):
        """An operator who enables the switches but forgets `mode` gets suggest."""
        cfg = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "surfaces": {
                    "skill": {
                        "enabled": True,
                        "handler": HANDLER_NAME,
                        "path": str(_target(tmp_path)),
                    }
                },
            },
        )
        assert cfg.agent_extensions["mode"] == "suggest"
        res = apply_agent_action(_action(), cfg)
        assert res.status == STATUS_DISABLED
        assert not _target(tmp_path).exists()


# --- 5-7. confirmation semantics and the real write --------------------------


class TestConfirmationAndApply:
    def test_confirmation_required_writes_nothing(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply_with_confirmation")
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_CONFIRMATION_REQUIRED
        assert res.blockers == ["not_confirmed"]
        # it still tells the user what it is asking permission for
        assert res.metadata["would_write_to"] == str(_target(tmp_path))
        assert not _target(tmp_path).exists()
        assert _written_files(tmp_path) == []

    def test_confirmed_apply_with_confirmation_writes_to_the_temp_file(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply_with_confirmation")
        res = apply_agent_action(_action(), cfg, confirmed=True)
        assert res.applied is True
        assert res.status == STATUS_APPLIED
        assert res.confirmed is True
        assert res.target == str(_target(tmp_path))
        text = _target(tmp_path).read_text(encoding="utf-8")
        assert SECTION_MARKER in text
        assert "## 发布流程" in text
        assert "git tag v3.5.4" in text         # the caller's own content
        assert "- handler: generic_file" in text
        assert "- operation: create" in text

    def test_mode_apply_writes_without_confirmation(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        res = apply_agent_action(_action(), cfg)
        assert res.applied is True
        assert res.status == STATUS_APPLIED
        assert _target(tmp_path).exists()
        assert res.metadata["created_file"] is True

    def test_an_action_asking_for_confirmation_overrides_mode_apply(self, tmp_path):
        """`requires_user_confirmation` is the action's own veto."""
        cfg = _ext_cfg(tmp_path, mode="apply")
        res = apply_agent_action(
            _action(requires_user_confirmation=True), cfg
        )
        assert res.status == STATUS_CONFIRMATION_REQUIRED
        assert res.metadata["action_requires_confirmation"] is True
        assert not _target(tmp_path).exists()

        ok = apply_agent_action(
            _action(requires_user_confirmation=True), cfg, confirmed=True
        )
        assert ok.applied is True

    def test_second_apply_appends_and_never_rewrites(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        apply_agent_action(_action(title="A"), cfg)
        first = _target(tmp_path).read_text(encoding="utf-8")
        apply_agent_action(_action(title="B"), cfg)
        second = _target(tmp_path).read_text(encoding="utf-8")
        assert second.startswith(first)          # existing text is untouched
        assert second.count(SECTION_MARKER) == 2
        assert "## A" in second and "## B" in second

    def test_update_appends_a_superseding_section_instead_of_editing(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        apply_agent_action(_action(title="发布流程"), cfg)
        before = _target(tmp_path).read_text(encoding="utf-8")
        res = apply_agent_action(_action(title="发布流程", operation="update"), cfg)
        after = _target(tmp_path).read_text(encoding="utf-8")
        assert res.applied is True
        assert after.startswith(before)          # no fuzzy edit of the original
        assert "- supersedes: 发布流程" in after
        assert after.count("## 发布流程") == 2

    def test_update_with_nothing_to_supersede_says_so(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        apply_agent_action(_action(title="发布流程", operation="update"), cfg)
        text = _target(tmp_path).read_text(encoding="utf-8")
        assert "- supersedes: nothing (new section)" in text

    def test_user_profile_surface_applies_through_its_own_file(self, tmp_path):
        profile = tmp_path / "ext" / "profile.md"
        cfg = _ext_cfg(
            tmp_path, mode="apply", surface="user_profile", path=profile
        )
        res = apply_agent_action(
            _action(
                surface="user_profile",
                operation="update",
                title="语言偏好",
                content="以后所有回复都先给结论再给细节，始终用中文。",
            ),
            cfg,
        )
        assert res.applied is True
        assert profile.read_text(encoding="utf-8").count("语言偏好") >= 1
        assert not _target(tmp_path).exists()    # surfaces do not bleed together


# --- 8. dry run --------------------------------------------------------------


class TestDryRun:
    def test_dry_run_reports_would_apply_and_writes_nothing(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        res = apply_agent_action(_action(), cfg, dry_run=True)
        assert res.applied is False
        assert res.status == STATUS_WOULD_APPLY
        assert res.dry_run is True
        assert res.target == str(_target(tmp_path))
        assert res.metadata["bytes_to_append"] > 0
        assert res.metadata["creates_parent_dirs"] is True
        assert res.metadata["section_heading"] == "发布流程"
        assert not _target(tmp_path).exists()
        assert not _target(tmp_path).parent.exists()
        assert _written_files(tmp_path) == []

    def test_dry_run_on_an_existing_file_leaves_it_byte_identical(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        apply_agent_action(_action(title="A"), cfg)
        before = _target(tmp_path).read_bytes()
        res = apply_agent_action(_action(title="B"), cfg, dry_run=True)
        assert res.status == STATUS_WOULD_APPLY
        assert res.metadata["file_exists"] is True
        assert _target(tmp_path).read_bytes() == before

    def test_an_adapter_that_cannot_dry_run_is_refused_not_called(self, tmp_path):
        """A handler with no `dry_run` parameter must never be asked to write."""
        calls: list = []

        class _LegacyAdapter:
            name = "legacy"

            def supports(self, surface) -> bool:
                return True

            def apply(self, action):
                calls.append(action)
                raise AssertionError("must not be invoked under dry_run")

        cfg = _ext_cfg(tmp_path, handler="legacy")
        with _registered(_LegacyAdapter()):
            res = apply_agent_action(_action(), cfg, dry_run=True)
        assert res.applied is False
        assert res.status == STATUS_REFUSED
        assert res.blockers == ["dry_run_unsupported"]
        assert calls == []


# --- 9. action validation ----------------------------------------------------


class TestActionValidation:
    @pytest.mark.parametrize(
        "payload, needle",
        [
            ({"kind": "run_shell_command"}, "kind"),
            ({"surface": "not_a_surface"}, "surface"),
            ({"surface": "l0_index"}, None),          # valid surface, wrong family
            ({"operation": "delete"}, "operation"),
            ({"source_content": "   "}, "source_content"),
            ({"rationale": None}, "rationale"),
        ],
    )
    def test_invalid_actions_are_refused_not_coerced(self, tmp_path, payload, needle):
        base = {
            "kind": "agent_native_write",
            "surface": "skill",
            "operation": "create",
            "title": "x",
            "source_content": SKILL_TEXT,
            "rationale": "test",
        }
        base.update(payload)
        res = apply_agent_action(base, _ext_cfg(tmp_path, mode="apply"))
        assert res.applied is False
        assert res.status in (STATUS_INVALID_ACTION, STATUS_REFUSED)
        if needle:
            assert needle in (res.detail or "")
        assert not _target(tmp_path).exists()

    def test_a_non_mapping_action_is_refused(self, tmp_path):
        res = apply_agent_action("just a string", _ext_cfg(tmp_path))  # type: ignore[arg-type]
        assert res.status == STATUS_INVALID_ACTION
        assert "AgentAction" in res.detail

    def test_empty_source_content_is_never_invented(self, tmp_path):
        res = apply_agent_action(
            {
                "kind": "agent_native_write",
                "surface": "skill",
                "operation": "create",
                "source_content": "",
                "rationale": "test",
            },
            _ext_cfg(tmp_path, mode="apply"),
        )
        assert res.status == STATUS_INVALID_ACTION
        assert "never invents prose" in res.detail
        assert not _target(tmp_path).exists()

    def test_oversized_source_content_is_refused_rather_than_truncated(self):
        action, error = validate_action(
            {
                "kind": "agent_native_write",
                "surface": "skill",
                "operation": "create",
                "source_content": "x" * 200_001,
                "rationale": "test",
            }
        )
        assert action is None
        assert "over the" in error

    def test_a_valid_dict_action_is_accepted(self, tmp_path):
        res = apply_agent_action(
            {
                "kind": "agent_native_write",
                "surface": "skill",
                "operation": "append",
                "title": "dict form",
                "source_content": SKILL_TEXT,
                "rationale": "serialized form works too",
            },
            _ext_cfg(tmp_path, mode="apply"),
        )
        assert res.applied is True
        assert res.operation == "append"
        assert "## dict form" in _target(tmp_path).read_text(encoding="utf-8")


# --- 10. the generic file handler's path safety ------------------------------


class TestGenericFilePathSafety:
    def test_no_configured_path_refuses_rather_than_guessing(self, tmp_path):
        """The hazard this gate exists for: an implicit target."""
        cfg = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "mode": "apply",
                "surfaces": {"skill": {"enabled": True, "handler": HANDLER_NAME}},
            },
        )
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_REFUSED
        assert res.blockers == ["no_configured_path"]
        assert "no default location" in res.detail
        assert _written_files(tmp_path) == []

    @pytest.mark.parametrize(
        "path",
        ["/etc/layered-memory-skills.md", "/usr/local/share/skills.md", "/sys/skills"],
    )
    def test_system_directories_are_refused_however_explicit(self, tmp_path, path):
        cfg = _ext_cfg(tmp_path, mode="apply", path=path)
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_REFUSED
        assert res.blockers == ["unsafe_system_target"]
        assert not Path(path).exists()

    @pytest.mark.parametrize(
        "name", ["config.yaml", "state.db", "handler.py", "memory.jsonl", ".env"]
    )
    def test_non_prose_file_types_are_refused(self, tmp_path, name):
        cfg = _ext_cfg(tmp_path, mode="apply", path=tmp_path / "ext" / name)
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_REFUSED
        assert res.blockers == ["unsafe_file_type"]
        assert not (tmp_path / "ext" / name).exists()

    def test_a_relative_path_escaping_the_root_is_refused(self, tmp_path):
        root = tmp_path / "ext"
        root.mkdir()
        cfg = _ext_cfg(
            tmp_path, mode="apply", root=root, path="../../escaped.md"
        )
        res = apply_agent_action(_action(), cfg)
        assert res.applied is False
        assert res.status == STATUS_REFUSED
        assert res.blockers == ["path_escapes_base"]
        assert not (tmp_path.parent / "escaped.md").exists()

    def test_a_relative_path_resolves_under_the_configured_root(self, tmp_path):
        root = tmp_path / "ext"
        cfg = _ext_cfg(tmp_path, mode="apply", root=root, path="skills/release.md")
        res = apply_agent_action(_action(), cfg)
        assert res.applied is True
        assert res.target == str(root / "skills" / "release.md")
        assert (root / "skills" / "release.md").exists()

    def test_a_relative_path_without_a_root_resolves_under_framework_home(
        self, tmp_path
    ):
        cfg = _ext_cfg(tmp_path, mode="apply", path="extensions/skills.md")
        res = apply_agent_action(_action(), cfg)
        assert res.applied is True
        expected = tmp_path / ".layered-memory" / "extensions" / "skills.md"
        assert res.target == str(expected)
        assert expected.exists()

    def test_a_configured_root_alone_derives_one_file_per_surface(self, tmp_path):
        root = tmp_path / "ext"
        cfg = _ext_cfg(tmp_path, mode="apply", root=root, path=None)
        res = apply_agent_action(_action(), cfg)
        assert res.applied is True
        assert res.target == str(root / "skill.md")

    def test_a_configured_directory_gets_the_surface_file_inside_it(self, tmp_path):
        box = tmp_path / "ext"
        box.mkdir()
        cfg = _ext_cfg(tmp_path, mode="apply", path=box)
        res = apply_agent_action(_action(), cfg)
        assert res.applied is True
        assert res.target == str(box / "skill.md")

    def test_resolution_is_pure_and_reports_which_rule_allowed_it(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        target = resolve_target(MemorySurface.SKILL, cfg)
        assert target.path == _target(tmp_path)
        assert target.trust == "explicit_absolute_path"
        assert _written_files(tmp_path) == []        # resolving writes nothing

    def test_the_handler_supports_only_extension_surfaces(self):
        handler = GenericFileAdapter()
        assert handler.supports(MemorySurface.SKILL) is True
        assert handler.supports(MemorySurface.USER_PROFILE) is True
        assert handler.supports(MemorySurface.AGENT_OTHER) is True
        assert handler.supports(MemorySurface.L1_KNOWLEDGE) is False
        assert handler.supports(MemorySurface.TODO) is False

    def test_a_long_action_is_bounded_not_unbounded(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        res = apply_agent_action(_action(content="步骤:\n" + "行\n" * 20_000), cfg)
        assert res.applied is True
        text = _target(tmp_path).read_text(encoding="utf-8")
        assert "[truncated at 8000 chars]" in text
        assert len(text) < 12_000


# --- 11. capability reflection agrees with the execution path ----------------


class TestCapabilityReflection:
    def test_default_capabilities_say_suggest_only(self, tmp_path):
        caps = surface_capabilities(_cfg(tmp_path)).to_dict()
        assert caps["agent_extensions_enabled"] is False
        assert caps["registered_handlers"] == [HANDLER_NAME]
        for name, cap in caps["extension_surfaces"].items():
            assert cap["owned_by"] == "agent", name
            assert cap["default_behavior"] == "suggest", name
            assert cap["can_apply"] is False, name
            assert cap["handler_registered"] is False, name
            assert cap["store_path"] is None, name
            assert "extensions_disabled" in cap["apply_blockers"], name

    def test_configured_temp_handler_can_apply_without_moving_ownership(
        self, tmp_path
    ):
        cfg = _ext_cfg(tmp_path, mode="apply_with_confirmation")
        caps = surface_capabilities(cfg).to_dict()
        skill = caps["extension_surfaces"]["skill"]
        assert skill["can_apply"] is True
        assert skill["owned_by"] == "agent"             # never moves
        assert skill["default_behavior"] == "suggest"   # never moves either
        assert skill["configured_handler"] == HANDLER_NAME
        assert skill["handler_registered"] is True
        assert skill["application_mode"] == "apply_with_confirmation"
        assert skill["apply_blockers"] == []
        assert skill["store_path"] == str(_target(tmp_path))
        # the surfaces nobody configured are untouched by one surface's opt-in
        for other in ("user_profile", "agent_other"):
            assert caps["extension_surfaces"][other]["can_apply"] is False

    def test_an_unknown_handler_is_visible_in_the_capability_output(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply", handler="not-a-real-adapter")
        skill = surface_capabilities(cfg).to_dict()["extension_surfaces"]["skill"]
        assert skill["configured_handler"] == "not-a-real-adapter"
        assert skill["handler_registered"] is False
        assert skill["can_apply"] is False
        assert skill["apply_blockers"] == ["handler_not_registered"]

    def test_a_missing_path_blocks_can_apply_before_any_call(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "mode": "apply",
                "surfaces": {"skill": {"enabled": True, "handler": HANDLER_NAME}},
            },
        )
        skill = surface_capabilities(cfg).to_dict()["extension_surfaces"]["skill"]
        assert skill["can_apply"] is False
        assert skill["apply_blockers"] == ["no_configured_path"]

    @pytest.mark.parametrize(
        "kwargs, blocker",
        [
            ({"enabled": False}, "extensions_disabled"),
            ({"surface_enabled": False}, "surface_disabled"),
            ({"mode": "suggest"}, "mode_suggest"),
            ({"handler": None}, "no_handler"),
            ({"handler": "nope"}, "handler_not_registered"),
        ],
    )
    def test_every_blocker_the_reflection_reports_is_one_apply_refuses_with(
        self, tmp_path, kwargs, blocker
    ):
        cfg = _ext_cfg(tmp_path, **kwargs)
        readiness = extension_readiness(MemorySurface.SKILL, cfg)
        assert blocker in readiness["blockers"]
        assert readiness["can_apply"] is False
        res = apply_agent_action(_action(), cfg)
        assert blocker in res.blockers
        assert res.applied is False

    def test_core_surfaces_are_unaffected_by_extension_config(self, tmp_path):
        caps = surface_capabilities(_ext_cfg(tmp_path, mode="apply")).to_dict()
        for name, cap in caps["core_surfaces"].items():
            assert cap["owned_by"] == "framework", name
            assert cap["configured_handler"] is None, name


# --- 12. no MEMORY_CHANGELOG for agent-owned writes --------------------------


class TestChangelogIsNotTouched:
    def _changelog(self, tmp_path: Path) -> Path:
        return tmp_path / ".layered-memory" / "data" / "memory_changelog.jsonl"

    def test_applying_an_agent_owned_action_records_no_event(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        assert not self._changelog(tmp_path).exists()

        res = apply_agent_action(_action(), cfg)
        assert res.applied is True
        assert _target(tmp_path).exists()

        log = self._changelog(tmp_path)
        assert not log.exists() or log.read_text(encoding="utf-8").strip() == ""

    def test_an_l1_write_still_records_one(self, tmp_path):
        """Control: the changelog is live, it just does not cover this surface."""
        from layered_memory_mcp.injector import inject_knowledge

        cfg = _ext_cfg(tmp_path, mode="apply")
        inject_knowledge(
            config=cfg,
            domain="infra",
            section="路由边界",
            content=(
                "架构决策：routing 只做确定性分类，不做语义推断，也不认识任何 agent "
                "产品的具体路径；产品相关的落地逻辑全部放在 adapters 层。"
            ),
        )
        apply_agent_action(_action(), cfg)

        lines = [
            json.loads(line)
            for line in self._changelog(tmp_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert lines, "the L1 write should have recorded provenance"
        assert {e["surface"] for e in lines} <= {"l1_knowledge", "l0_index"}
        assert all(e["surface"] != "skill" for e in lines)


# --- 13. the end-to-end path -------------------------------------------------


class TestClassifyThenApply:
    def test_classify_to_suggested_action_to_apply(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply_with_confirmation")

        decision = classify_memory_item(SKILL_TEXT, config=cfg, section="发布流程")
        assert decision.surface is MemorySurface.SKILL
        assert decision.ownership is SurfaceOwnership.AGENT
        assert decision.framework_can_apply is True
        assert decision.application_mode == "apply_with_confirmation"
        assert decision.configured_handler == HANDLER_NAME

        action = decision.suggested_action
        assert action is not None
        # the classifier already marks the action as needing confirmation,
        # because that is the configured mode — the flag is not an afterthought
        assert action.requires_user_confirmation is True
        assert action.source_content == SKILL_TEXT.strip()

        pending = apply_agent_action(action, cfg)
        assert pending.status == STATUS_CONFIRMATION_REQUIRED
        assert not _target(tmp_path).exists()

        done = apply_agent_action(action, cfg, confirmed=True)
        assert done.applied is True
        text = _target(tmp_path).read_text(encoding="utf-8")
        assert "## 发布流程" in text
        assert "pytest -q" in text
        assert "- shape_hint: title, when_to_use" in text

    def test_the_serialized_decision_round_trips_into_an_apply(self, tmp_path):
        """What an MCP client actually has: JSON from classify_memory_item."""
        cfg = _ext_cfg(tmp_path, mode="apply")
        payload = json.loads(
            json.dumps(classify_memory_item(SKILL_TEXT, config=cfg).to_dict())
        )
        res = apply_agent_action(payload["suggested_action"], cfg)
        assert res.applied is True
        assert _target(tmp_path).exists()

    def test_an_ephemeral_item_offers_nothing_to_apply(self, tmp_path):
        cfg = _ext_cfg(tmp_path, mode="apply")
        decision = classify_memory_item(
            "08:12:01 INFO: starting scan\n08:12:02 ERROR: sync failed\n", config=cfg
        )
        assert decision.suggested_action is None
        assert _written_files(tmp_path) == []

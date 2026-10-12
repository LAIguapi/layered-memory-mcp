"""Lazy auto-compaction backs off on an unchanged blocked set (ROADMAP item 6).

The loop this file exists to close: under ``routing.mode: enforce`` a compaction
pass deliberately leaves every non-L1 entry in the agent's memory file, because
a blocked entry has nowhere else to go yet and deleting it would be data loss.
``auto_maintain_after_write``'s trigger is a usage ratio, so a memory file full
of blocked entries stays over threshold *permanently* — and every write re-ran a
full pass (backup, classification, rewrite) that migrated nothing.

What is pinned below:

  * the first enforced lazy pass runs and records a fingerprint of what it
    blocked;
  * a second pass over the same blocked set, with nothing migratable, is
    skipped with a structured reason instead of repeating itself;
  * a new blocked entry changes the fingerprint and triggers a pass at once;
  * a new *migratable* entry triggers a pass even when the blocked set is
    unchanged — the backoff may delay noise, never work;
  * advisory mode is untouched, and an explicit ``compact_memory`` call always
    runs;
  * the state lives under the framework's own temp home, is versioned, and
    carries hashes rather than memory text or memory paths.

Every path uses ``tmp_path`` for both the framework home and the agent memory
file (conftest's sandbox fixtures are the backstop).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.memory_compactor import (
    BLOCKED_SET_UNCHANGED,
    _auto_compaction_decision,
    auto_maintain_after_write,
    compact_memory,
)
from layered_memory_mcp.storage.auto_compaction_state import (
    SCHEMA_VERSION,
    STATE_FILENAME,
    STATE_KIND,
    AutoCompactionState,
    blocked_set_fingerprint,
    entry_content_hash,
    state_path,
    target_key,
)

SEPARATOR = "§"

# --- one entry per surface, long enough to read as bloat ---------------------

POINTER = "[L0] infra: proxy notes → knowledge/infra.md"

SKILL_ENTRY = """[发布流程] 发布流程（每次发版都按这个来）：

1. 运行 pytest -q，确认全绿
2. 执行 python -m build 打包
3. git tag v3.5.6 && git push --tags
4. 上传后在 PyPI 页面核对版本号
"""

PROFILE_ENTRY = (
    "[用户偏好] 用户偏好：以后所有回复都先给结论再给细节，始终用中文，"
    "任何时候都不要在回答里加免责声明。"
)

LOG_ENTRY = """[扫描日志] 08:12:01 INFO: starting scan of 14 domains
08:12:02 DEBUG: loaded vectors.db (512 dim)
08:12:03 ERROR: vector sync failed for infra
Traceback (most recent call last):
  File "injector.py", line 884
"""

KNOWLEDGE_ENTRY = (
    "[架构边界] 路由器的职责边界：routing.py 只做确定性的标记与结构匹配，不做语义推断，"
    "也不认识任何 agent 产品的具体路径。这个架构决策的原因是核心框架必须保持产品中立，"
    "产品相关的落地逻辑全部放在 adapters 层。当前 L1 存的是成立的事实与边界。"
)

SECOND_KNOWLEDGE_ENTRY = (
    "[治理边界] 知识治理的边界约定：governance 负责的是失效判定与复核排期，"
    "框架本身不执行任何清理动作。这里记录的是当前的架构事实与职责划分，"
    "核心决策是 routing 永远不写 agent 自己的存储。"
)


def _cfg(tmp_path: Path, *, mode: str = "enforce", **kwargs) -> MemoryConfig:
    """Temp-home config whose usage-ratio trigger fires on the fixtures above.

    The usage ratio is the trigger this file is about — the one a blocked set
    keeps permanently satisfied — so the interval trigger is pushed a century
    out of the way and the bloat threshold down to 10% of a small limit. That
    makes "would have compacted" deterministic for every fixture below.
    """
    home = tmp_path / "home"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("memory_char_limit", 600)
    kwargs.setdefault("compact_bloat_threshold", 0.1)
    kwargs.setdefault("auto_maintain_interval_days", 36500.0)
    kwargs.setdefault("routing", {"mode": mode})
    return MemoryConfig(
        home=str(home),
        knowledge_dir=str(home / "knowledge"),
        **kwargs,
    )


def _write_memory(tmp_path: Path, entries, name: str = "MEMORY.md") -> Path:
    mem = tmp_path / name
    mem.write_text(f"\n{SEPARATOR}\n".join(entries) + "\n", encoding="utf-8")
    return mem


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state_doc(config) -> dict:
    return json.loads(state_path(config).read_text(encoding="utf-8"))


def _record(config, mem: Path) -> dict | None:
    return AutoCompactionState(state_path(config)).get(target_key(mem))


@pytest.fixture
def blocked_memory(tmp_path):
    """Agent memory holding a pointer plus two entries enforce will block."""
    return _write_memory(tmp_path, [POINTER, SKILL_ENTRY, PROFILE_ENTRY])


class TestBlockedSetFingerprint:
    """The fingerprint must identify the *set*, not the pass that saw it."""

    def test_order_and_bucket_order_do_not_change_it(self, tmp_path):
        cfg = _cfg(tmp_path)
        one = _write_memory(tmp_path, [POINTER, SKILL_ENTRY, PROFILE_ENTRY], "a.md")
        other = _write_memory(tmp_path, [PROFILE_ENTRY, POINTER, SKILL_ENTRY], "b.md")

        first = compact_memory(cfg, memory_path=one, dry_run=True)["blocked_set"]
        second = compact_memory(cfg, memory_path=other, dry_run=True)["blocked_set"]

        assert first["count"] == 2
        assert first["fingerprint"].startswith("sha256:")
        assert first == second

    def test_reformatting_an_entry_is_not_a_new_blocked_set(self, tmp_path):
        """Whitespace is layout, not content."""
        cfg = _cfg(tmp_path)
        one = _write_memory(tmp_path, [SKILL_ENTRY], "a.md")
        other = _write_memory(tmp_path, [SKILL_ENTRY.replace("\n", "\n  ")], "b.md")

        assert (
            compact_memory(cfg, memory_path=one, dry_run=True)["blocked_set"][
                "fingerprint"
            ]
            == compact_memory(cfg, memory_path=other, dry_run=True)["blocked_set"][
                "fingerprint"
            ]
        )

    def test_changed_content_changes_it(self, tmp_path):
        cfg = _cfg(tmp_path)
        one = _write_memory(tmp_path, [SKILL_ENTRY], "a.md")
        other = _write_memory(tmp_path, [SKILL_ENTRY + "5. 发公告\n"], "b.md")

        assert (
            compact_memory(cfg, memory_path=one, dry_run=True)["blocked_set"][
                "fingerprint"
            ]
            != compact_memory(cfg, memory_path=other, dry_run=True)["blocked_set"][
                "fingerprint"
            ]
        )

    def test_nothing_blocked_has_no_fingerprint_to_match(self, tmp_path):
        """"Nothing is blocked" is a state to clear, not one to compare."""
        cfg = _cfg(tmp_path)
        mem = _write_memory(tmp_path, [POINTER])

        blocked_set = compact_memory(cfg, memory_path=mem, dry_run=True)["blocked_set"]
        assert blocked_set == {"count": 0, "fingerprint": None, "by_surface": {}}
        assert blocked_set_fingerprint([]) == blocked_set

    def test_advisory_reports_an_empty_set_rather_than_omitting_it(self, tmp_path):
        cfg = _cfg(tmp_path, mode="advisory")
        mem = _write_memory(tmp_path, [POINTER, SKILL_ENTRY])

        report = compact_memory(cfg, memory_path=mem, dry_run=True)
        assert report["blocked_set"]["count"] == 0
        assert report["blocked_set"]["fingerprint"] is None

    def test_candidates_carry_the_entry_hash_not_the_entry(self, tmp_path):
        cfg = _cfg(tmp_path)
        mem = _write_memory(tmp_path, [SKILL_ENTRY])

        candidate = compact_memory(cfg, memory_path=mem, dry_run=True)[
            "agent_owned_candidates"
        ][0]
        assert candidate["entry_hash"] == entry_content_hash(SKILL_ENTRY)
        assert len(candidate["entry_hash"]) == 16


class TestFirstEnforcedPass:
    def test_it_runs_and_records_what_it_blocked(self, tmp_path, blocked_memory):
        cfg = _cfg(tmp_path)

        report = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        compact = report["compact"]

        assert compact["triggered"] is True
        # Ran, and only blocked: nothing reached L1 this pass.
        assert compact["status"] == "blocked_only"
        assert compact["migrated_count"] == 0
        assert compact["blocked_from_l1"] == 2
        assert "usage" in compact["reason"]
        assert compact["backoff_decision"] == "no_recorded_blocked_set"
        assert compact["blocked_set"]["count"] == 2
        assert compact["blocked_set"]["by_surface"] == {"skill": 1, "user_profile": 1}
        assert "auto_compaction_backoff" not in report

        state = compact["blocked_set_state"]
        assert state["action"] == "recorded"
        assert state["fingerprint"] == compact["blocked_set"]["fingerprint"]
        assert state["source_pinned"] is True
        assert _record(cfg, blocked_memory)["blocked_set"]["count"] == 2

        # The blocked entries are still in agent memory — backing off later must
        # not be mistaken for having placed them.
        assert "pytest -q" in blocked_memory.read_text(encoding="utf-8")

    def test_a_migrating_pass_reports_status_migrated(self, tmp_path):
        cfg = _cfg(tmp_path)
        mem = _write_memory(tmp_path, [POINTER, KNOWLEDGE_ENTRY])

        compact = auto_maintain_after_write(cfg, memory_path=mem)["compact"]
        assert compact["status"] == "migrated"
        assert compact["migrated_count"] == 1
        assert compact["blocked_from_l1"] == 0
        # Nothing blocked → nothing to remember.
        assert compact["blocked_set_state"]["action"] in (
            "cleared",
            "nothing_to_clear",
        )
        assert _record(cfg, mem) is None


class TestRepeatedPassBacksOff:
    def test_second_pass_is_skipped_with_a_structured_reason(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        before = _digest(blocked_memory)

        report = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        compact = report["compact"]

        assert compact["triggered"] is False
        assert compact["status"] == "backed_off"
        # The trigger has not gone away — it just cannot be acted on.
        assert "usage" in compact["reason"]
        assert compact["blocked_from_l1"] == 2

        backoff = report["auto_compaction_backoff"]
        assert backoff == compact["backoff"]
        assert backoff["status"] == BLOCKED_SET_UNCHANGED
        assert backoff["routing_mode"] == "enforce"
        assert backoff["probe"] == "source_unchanged"
        assert backoff["blocked_set"]["count"] == 2
        assert backoff["migratable_l1_candidates"] == 0
        assert backoff["skipped_passes"] == 1
        assert backoff["first_seen_at"]
        assert "place them on their own surfaces" in backoff["hint"]

        # Not one byte rewritten, and no L1 file conjured.
        assert _digest(blocked_memory) == before
        assert list(Path(cfg.knowledge_dir).glob("*.md")) == []

    def test_repeated_skips_accumulate_instead_of_repeating_work(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        for expected in (1, 2, 3):
            report = auto_maintain_after_write(cfg, memory_path=blocked_memory)
            assert report["compact"]["status"] == "backed_off"
            assert report["auto_compaction_backoff"]["skipped_passes"] == expected

    def test_a_new_pointer_changes_the_file_but_not_the_blocked_set(
        self, tmp_path, blocked_memory
    ):
        """The dual-write appends pointers; that is not new bloat.

        The byte-identity shortcut cannot answer this one, so the decision
        falls through to the classification probe — which must reach the same
        conclusion and re-pin the new bytes.
        """
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        report = auto_maintain_after_write(
            cfg,
            l0_pointer="[L0] misc: sanity check → knowledge/misc.md",
            memory_path=blocked_memory,
        )
        assert report["dual_write"]["action"] in ("added", "replaced", "updated")
        backoff = report["auto_compaction_backoff"]
        assert backoff["status"] == BLOCKED_SET_UNCHANGED
        assert backoff["probe"] == "dry_run_classification"

        # Re-pinned: the next pass takes the cheap path again.
        again = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert again["auto_compaction_backoff"]["probe"] == "source_unchanged"


class TestNewWorkStillTriggersAPass:
    def test_a_new_blocked_entry_changes_the_fingerprint(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        first_fingerprint = first["compact"]["blocked_set"]["fingerprint"]
        assert auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"][
            "status"
        ] == "backed_off"

        with blocked_memory.open("a", encoding="utf-8") as fh:
            fh.write(f"{SEPARATOR}\n{LOG_ENTRY}")

        report = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        compact = report["compact"]
        assert compact["triggered"] is True
        assert compact["status"] == "blocked_only"
        assert compact["backoff_decision"] == "blocked_set_changed"
        assert compact["blocked_from_l1"] == 3
        assert compact["blocked_set"]["fingerprint"] != first_fingerprint
        assert "auto_compaction_backoff" not in report
        # …and the new fingerprint is what the next pass compares against.
        assert (
            _record(cfg, blocked_memory)["blocked_set"]["fingerprint"]
            == compact["blocked_set"]["fingerprint"]
        )

    def test_a_new_l1_entry_migrates_even_with_the_blocked_set_unchanged(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        blocked_fingerprint = first["compact"]["blocked_set"]["fingerprint"]

        with blocked_memory.open("a", encoding="utf-8") as fh:
            fh.write(f"{SEPARATOR}\n{KNOWLEDGE_ENTRY}\n")

        report = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        compact = report["compact"]
        assert compact["triggered"] is True
        assert compact["status"] == "migrated"
        assert compact["migrated_count"] == 1
        assert compact["backoff_decision"] == "l1_work_pending"
        # The blocked set is genuinely the same one — the pass ran for the work.
        assert compact["blocked_set"]["fingerprint"] == blocked_fingerprint
        assert compact["blocked_from_l1"] == 2

        written = "\n".join(
            p.read_text(encoding="utf-8")
            for p in Path(cfg.knowledge_dir).glob("*.md")
        )
        assert "routing.py 只做确定性的标记与结构匹配" in written

    def test_an_entry_refused_for_want_of_a_heading_is_not_swallowed(
        self, tmp_path, blocked_memory
    ):
        """A refusal has to keep being reported until an agent acts on it."""
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        # Untagged L1 prose: classified as knowledge, but no derivable heading.
        with blocked_memory.open("a", encoding="utf-8") as fh:
            fh.write(f"{SEPARATOR}\n{KNOWLEDGE_ENTRY.split('] ', 1)[1]}\n")

        decision = _auto_compaction_decision(
            cfg, blocked_memory, trigger_reason="test"
        )
        assert decision["skip"] is False
        assert decision["status"] == "l1_work_pending"
        assert decision["needs_title_count"] == 1


class TestAdvisoryModeIsUnchanged:
    def test_no_state_is_read_or_written_and_no_backoff_is_reported(self, tmp_path):
        cfg = _cfg(tmp_path, mode="advisory")
        mem = _write_memory(tmp_path, [POINTER, SKILL_ENTRY, PROFILE_ENTRY])

        decision = _auto_compaction_decision(cfg, mem, trigger_reason="test")
        assert decision == {
            "skip": False,
            "status": "not_applicable",
            "routing_mode": "advisory",
        }

        compact = auto_maintain_after_write(cfg, memory_path=mem)["compact"]
        assert compact["triggered"] is True
        assert compact["status"] == "migrated"
        # Advisory migrates everything, so nothing is ever blocked and there is
        # no state file to grow.
        assert compact["blocked_from_l1"] == 0
        assert "blocked_set_state" not in compact
        assert not state_path(cfg).exists()

    def test_enforce_disabled_by_routing_enabled_false_does_not_back_off(
        self, tmp_path
    ):
        cfg = _cfg(tmp_path, routing={"enabled": False, "mode": "enforce"})
        mem = _write_memory(tmp_path, [POINTER, SKILL_ENTRY])

        decision = _auto_compaction_decision(cfg, mem, trigger_reason="test")
        assert decision["skip"] is False
        assert decision["status"] == "not_applicable"
        assert not state_path(cfg).exists()


class TestExplicitCompactionIsNeverBackedOff:
    def test_manual_compact_runs_with_the_blocked_set_unchanged(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        fingerprint = first["compact"]["blocked_set"]["fingerprint"]
        assert auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"][
            "status"
        ] == "backed_off"
        before = _state_doc(cfg)

        report = compact_memory(cfg, memory_path=blocked_memory, dry_run=False)

        assert report["success"] is True
        assert report["file_written"] is True
        assert report["routing_summary"]["blocked_from_l1"] == 2
        assert report["blocked_set"]["fingerprint"] == fingerprint
        assert "backed_off" not in json.dumps(report, ensure_ascii=False)
        # An explicit call neither consults nor advances the backoff state.
        assert _state_doc(cfg) == before

    def test_a_dry_run_is_not_backed_off_either(self, tmp_path, blocked_memory):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        report = compact_memory(cfg, memory_path=blocked_memory, dry_run=True)
        assert report["success"] is True
        assert report["blocked_set"]["count"] == 2


class TestStateFile:
    def test_it_lives_under_the_temp_home_and_is_versioned(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        path = state_path(cfg)
        assert path == Path(cfg.home) / "data" / STATE_FILENAME
        assert str(tmp_path) in str(path)
        assert path.is_file()

        doc = _state_doc(cfg)
        assert doc["schema_version"] == SCHEMA_VERSION
        assert doc["kind"] == STATE_KIND
        assert doc["updated_at"]
        assert list(doc["targets"]) == [target_key(blocked_memory)]

        record = doc["targets"][target_key(blocked_memory)]
        assert set(record) == {
            "blocked_set",
            "routing_mode",
            "routing_enabled",
            "source_digest",
            # v3.5.7: byte identity alone never proved the outcome would
            # repeat — the classifier it was taken under is the other half.
            "classifier_inputs_hash",
            "first_seen_at",
            "recorded_at",
            "last_attempt_at",
            "skipped_passes",
        }
        assert record["routing_mode"] == "enforce"
        assert record["routing_enabled"] is True
        assert record["blocked_set"]["by_surface"] == {"skill": 1, "user_profile": 1}

    def test_it_stores_hashes_not_memory_text_or_paths(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        raw = state_path(cfg).read_text(encoding="utf-8")
        for leak in ("pytest -q", "用户偏好", "PyPI", str(blocked_memory), "MEMORY.md"):
            assert leak not in raw
        assert target_key(blocked_memory) in raw

    def test_an_unknown_schema_version_is_ignored_rather_than_guessed_at(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        path = state_path(cfg)
        doc = _state_doc(cfg)
        doc["schema_version"] = SCHEMA_VERSION + 99
        path.write_text(json.dumps(doc), encoding="utf-8")

        store = AutoCompactionState(path)
        assert store.get(target_key(blocked_memory)) is None
        # …so the next pass runs rather than trusting a document it cannot read.
        compact = auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"]
        assert compact["triggered"] is True
        assert compact["backoff_decision"] == "no_recorded_blocked_set"

    def test_a_corrupt_state_file_costs_one_pass_not_a_failure(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        state_path(cfg).write_text("{not json", encoding="utf-8")

        compact = auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"]
        assert compact["triggered"] is True
        assert compact["blocked_set_state"]["action"] == "recorded"

    def test_two_memory_files_under_one_home_keep_separate_records(self, tmp_path):
        cfg = _cfg(tmp_path)
        one = _write_memory(tmp_path, [POINTER, SKILL_ENTRY], "one.md")
        other = _write_memory(tmp_path, [POINTER, PROFILE_ENTRY], "other.md")

        auto_maintain_after_write(cfg, memory_path=one)
        auto_maintain_after_write(cfg, memory_path=other)

        doc = _state_doc(cfg)
        assert set(doc["targets"]) == {target_key(one), target_key(other)}
        assert doc["targets"][target_key(one)]["blocked_set"]["by_surface"] == {
            "skill": 1
        }
        # The other file's unchanged set must not unblock this one's backoff.
        assert (
            auto_maintain_after_write(cfg, memory_path=one)["compact"]["status"]
            == "backed_off"
        )


class TestStateIsClearedWhenNothingIsBlocked:
    def test_placing_the_blocked_entries_clears_the_record(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert _record(cfg, blocked_memory) is not None

        # The agent placed the skill and the preference on their own surfaces
        # and left durable knowledge behind.
        _write_memory(tmp_path, [POINTER, KNOWLEDGE_ENTRY])

        compact = auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"]
        assert compact["status"] == "migrated"
        assert compact["blocked_from_l1"] == 0
        assert compact["blocked_set_state"]["action"] == "cleared"
        assert _record(cfg, blocked_memory) is None
        assert _state_doc(cfg)["targets"] == {}

    def test_new_bloat_after_a_clear_triggers_immediately(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        _write_memory(tmp_path, [POINTER, KNOWLEDGE_ENTRY])
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        _write_memory(tmp_path, [POINTER, SECOND_KNOWLEDGE_ENTRY, LOG_ENTRY])
        compact = auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"]
        assert compact["triggered"] is True
        assert compact["backoff_decision"] == "no_recorded_blocked_set"
        assert compact["migrated_count"] == 1
        assert compact["blocked_from_l1"] == 1

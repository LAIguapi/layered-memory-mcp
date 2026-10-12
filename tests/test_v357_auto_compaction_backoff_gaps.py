"""The two gaps left by the auto-compaction backoff (ROADMAP item 6).

GAP 1 — ONE MARKER MEANT THREE THINGS
-------------------------------------
``<home>/.last_auto_compact`` recorded "a compaction pass ran", and was read as
both "the interval has elapsed" and "the L1 dedup scan is due". Under routing
mode ``enforce`` those come apart: a lazy pass that can only block the same
entries again is *considered* and not run. Stamping the compact marker for it
would postpone the next real pass by a whole interval; stamping nothing left the
interval trigger asking again on every single write — and dragged a full L1
dedup scan (every knowledge file, plus the vector-store prune) along each time,
because that step read the same untouched marker.

Three markers now: ``.last_auto_compact`` (a pass ran),
``.last_auto_compact_attempt`` (a decision was reached, backoff included) and
``.last_l1_dedup`` (the dedup scan).

GAP 2 — "CLASSIFICATION IS DETERMINISTIC" WAS HALF A SENTENCE
-------------------------------------------------------------
The cheap path compared the memory file's bytes against the file the recorded
pass left behind. That is only an answer for a *fixed* classifier: edit
``routing.extra_*_markers``, ``routing.disabled_markers``, ``domain_keywords``
or ``l0_tag`` and the identical file classifies differently, so the shortcut
went on matching a stale blocked set until somebody happened to touch the memory
file. ``routing.classifier_inputs_hash`` identifies those inputs, the state
record pins it, and a mismatch closes the shortcut.

What is pinned below: the hash is stable under reordering and moves on every
input that matters; a classifier change re-classifies instead of shortcutting
and then re-pins; a throttled interval trigger never delays new blocked
entries, new L1 candidates, or any edit to the memory file; a backoff never
writes the compaction marker; dedup keeps its own cadence; an explicit
``compact_memory`` ignores all of it; and advisory mode is untouched.

Every path uses ``tmp_path`` for both the framework home and the agent memory
file (conftest's sandbox fixtures are the backstop).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.memory_compactor import (
    BLOCKED_SET_UNCHANGED,
    CLASSIFIER_INPUTS_CHANGED,
    MARKER_LAST_ATTEMPT,
    MARKER_LAST_COMPACT,
    MARKER_LAST_L1_DEDUP,
    _attempt_interval_seconds,
    _auto_compaction_decision,
    _read_last_l1_dedup_time,
    auto_maintain_after_write,
    compact_memory,
)
from layered_memory_mcp.routing import (
    CLASSIFIER_INPUTS_HASH_VERSION,
    classifier_inputs,
    classifier_inputs_hash,
)
from layered_memory_mcp.storage.auto_compaction_state import (
    SCHEMA_VERSION,
    AutoCompactionState,
    state_path,
    target_key,
)

SEPARATOR = "§"

POINTER = "[L0] infra: proxy notes → knowledge/infra.md"

SKILL_ENTRY = """[发布流程] 发布流程（每次发版都按这个来）：

1. 运行 pytest -q，确认全绿
2. 执行 python -m build 打包
3. git tag v3.5.7 && git push --tags
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


def _cfg(tmp_path: Path, *, mode: str = "enforce", **kwargs) -> MemoryConfig:
    """Temp-home config whose **usage-ratio** trigger fires on these fixtures.

    Same shape as the v3.5.6 fixtures: a small limit and a low bloat threshold,
    with the interval trigger pushed a century away so Trigger A is the one
    under test.
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


def _interval_cfg(tmp_path: Path, *, mode: str = "enforce", **kwargs) -> MemoryConfig:
    """Temp-home config where **only** the interval trigger can fire.

    A limit far above the fixtures keeps the usage ratio (and the critical
    safety net) quiet, and a zero interval means Trigger B is satisfied on
    every call — which is exactly the "a decision per write, forever" shape
    gap 1 is about.
    """
    kwargs.setdefault("memory_char_limit", 1_000_000)
    kwargs.setdefault("compact_bloat_threshold", 0.8)
    kwargs.setdefault("auto_maintain_interval_days", 0.0)
    kwargs.setdefault("auto_maintain_attempt_interval_hours", 1.0)
    return _cfg(tmp_path, mode=mode, **kwargs)


def _write_memory(tmp_path: Path, entries, name: str = "MEMORY.md") -> Path:
    mem = tmp_path / name
    mem.write_text(f"\n{SEPARATOR}\n".join(entries) + "\n", encoding="utf-8")
    return mem


def _marker(config, name: str) -> Path:
    return Path(config.home) / name


def _marker_value(config, name: str) -> str | None:
    path = _marker(config, name)
    return path.read_text(encoding="utf-8") if path.exists() else None


def _record(config, mem: Path) -> dict | None:
    return AutoCompactionState(state_path(config)).get(target_key(mem))


@pytest.fixture
def blocked_memory(tmp_path):
    """Agent memory holding a pointer plus two entries enforce will block."""
    return _write_memory(tmp_path, [POINTER, SKILL_ENTRY, PROFILE_ENTRY])


# ---------------------------------------------------------------------------
# Gap 2 — the classifier inputs hash
# ---------------------------------------------------------------------------


class TestClassifierInputsHash:
    def test_shape_and_version(self, tmp_path):
        cfg = _cfg(tmp_path)
        digest = classifier_inputs_hash(cfg)

        assert digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 32
        inputs = classifier_inputs(cfg)
        assert inputs["hash_version"] == CLASSIFIER_INPUTS_HASH_VERSION
        # Everything the decision claims to cover is actually in there.
        assert set(inputs) == {
            "hash_version",
            "decision_schema_version",
            "routing",
            "markers",
            "thresholds",
            "tie_break",
            "domain_keywords",
            "l0_tag",
            "agent_extensions",
        }
        assert set(inputs["markers"]) == {
            "agent_other",
            "ephemeral",
            "knowledge",
            "profile",
            "profile_scope",
            "skill",
            "todo",
        }

    def test_it_is_stable_for_the_same_config(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert classifier_inputs_hash(cfg) == classifier_inputs_hash(cfg)

    def test_reordering_a_semantically_identical_config_does_not_change_it(
        self, tmp_path
    ):
        """A YAML list is a set here; a digest that notices order is a bug.

        It would make the backoff re-probe after a cosmetic config edit — and,
        worse, make two installs with the same rules disagree about whether a
        recorded blocked set still holds.
        """
        one = _cfg(
            tmp_path,
            domain_keywords={"infra": ["proxy", "wsl"], "api": ["fastapi", "mcp"]},
            routing={
                "mode": "enforce",
                "extra_skill_markers": ["冒烟", "灰度"],
                "extra_knowledge_markers": ["口径", "量纲"],
                "disabled_markers": ["清单", "手册"],
            },
        )
        other = _cfg(
            tmp_path,
            domain_keywords={"api": ["mcp", "fastapi"], "infra": ["wsl", "proxy"]},
            routing={
                "mode": "enforce",
                "extra_skill_markers": ["灰度", "冒烟"],
                "extra_knowledge_markers": ["量纲", "口径"],
                "disabled_markers": ["手册", "清单"],
            },
        )

        assert classifier_inputs_hash(one) == classifier_inputs_hash(other)

    def test_changing_domain_keywords_changes_it(self, tmp_path):
        base = _cfg(tmp_path, domain_keywords={"infra": ["proxy"]})
        added_keyword = _cfg(tmp_path, domain_keywords={"infra": ["proxy", "wsl"]})
        added_domain = _cfg(
            tmp_path, domain_keywords={"infra": ["proxy"], "api": ["mcp"]}
        )

        assert classifier_inputs_hash(base) != classifier_inputs_hash(added_keyword)
        assert classifier_inputs_hash(base) != classifier_inputs_hash(added_domain)

    def test_changing_the_marker_vocabulary_changes_it(self, tmp_path):
        base = _cfg(tmp_path)
        extended = _cfg(
            tmp_path, routing={"mode": "enforce", "extra_skill_markers": ["冒烟测试"]}
        )
        # Removing a shipped default counts as much as adding a marker.
        reduced = _cfg(
            tmp_path, routing={"mode": "enforce", "disabled_markers": ["步骤"]}
        )

        assert classifier_inputs_hash(base) != classifier_inputs_hash(extended)
        assert classifier_inputs_hash(base) != classifier_inputs_hash(reduced)
        assert classifier_inputs_hash(extended) != classifier_inputs_hash(reduced)

    def test_changing_the_routing_switches_changes_it(self, tmp_path):
        enforce = _cfg(tmp_path, routing={"mode": "enforce"})
        advisory = _cfg(tmp_path, routing={"mode": "advisory"})
        disabled = _cfg(tmp_path, routing={"mode": "enforce", "enabled": False})
        strict_direct = _cfg(
            tmp_path, routing={"mode": "enforce", "enforce_direct_writes": True}
        )

        digests = {
            classifier_inputs_hash(c)
            for c in (enforce, advisory, disabled, strict_direct)
        }
        assert len(digests) == 4

    def test_changing_the_l0_tag_changes_it(self, tmp_path, monkeypatch):
        """The tag decides which entries reach the classifier at all."""
        base = _cfg(tmp_path)
        monkeypatch.setenv("LAYERED_MEMORY_L0_TAG", "[IDX]")
        retagged = _cfg(tmp_path)

        assert retagged.l0_tag == "[IDX]"
        assert classifier_inputs_hash(base) != classifier_inputs_hash(retagged)

    def test_enabling_an_agent_extension_surface_changes_it(self, tmp_path):
        base = _cfg(tmp_path)
        wired = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "mode": "apply",
                "surfaces": {"skill": {"enabled": True, "handler": "generic_file"}},
            },
        )

        assert classifier_inputs_hash(base) != classifier_inputs_hash(wired)

    def test_it_carries_no_path_and_no_timestamp(self, tmp_path):
        """A digest over volatile or location-bearing inputs is worthless.

        Timestamps would change it on every call (nothing could ever match),
        and a path would put the user's directory layout into a value that gets
        quoted in reports.
        """
        cfg = _cfg(
            tmp_path,
            agent_extensions={
                "enabled": True,
                "root": str(tmp_path / "skills"),
                "surfaces": {
                    "skill": {
                        "enabled": True,
                        "handler": "generic_file",
                        "path": str(tmp_path / "skills" / "s.md"),
                    }
                },
            },
        )

        raw = json.dumps(classifier_inputs(cfg), ensure_ascii=False)
        assert str(tmp_path) not in raw
        assert str(cfg.home) not in raw
        assert "skills" not in raw
        # Stable across the clock.
        first = classifier_inputs_hash(cfg)
        time.sleep(0.01)
        assert classifier_inputs_hash(cfg) == first

    def test_a_malformed_routing_section_still_hashes(self, tmp_path):
        """A half-written config must degrade, not take the digest with it.

        ``None`` means *invalidate* to every caller, so returning it for a
        config the framework can still classify with would disable the backoff
        rather than protect it.
        """
        cfg = _cfg(tmp_path)
        cfg.routing_markers = ["not", "a", "mapping"]  # type: ignore[assignment]
        cfg.domain_keywords = "nonsense"  # type: ignore[assignment]

        digest = classifier_inputs_hash(cfg)
        assert digest and digest.startswith("sha256:")
        assert classifier_inputs(cfg)["domain_keywords"] == {}


class TestByteShortcutIsClosedByAClassifierChange:
    """Gap 2: the memory file is not the only input to a classification."""

    def test_an_unchanged_file_under_changed_inputs_is_re_classified(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        fingerprint = first["compact"]["blocked_set"]["fingerprint"]
        recorded_hash = _record(cfg, blocked_memory)["classifier_inputs_hash"]
        assert recorded_hash == first["compact"]["blocked_set_state"][
            "classifier_inputs_hash"
        ]
        before = blocked_memory.read_bytes()

        # Same file, same routing mode — a different domain table. The blocked
        # set cannot change (a blocked entry's fingerprint is its content and
        # its surface, not the domain it would have gone to), so this is
        # precisely the case the byte shortcut used to get wrong: it would have
        # matched on bytes alone and never asked the new classifier.
        changed = _cfg(tmp_path, domain_keywords={"infra": ["proxy", "wsl"]})
        assert classifier_inputs_hash(changed) != recorded_hash

        report = auto_maintain_after_write(changed, memory_path=blocked_memory)
        backoff = report["auto_compaction_backoff"]

        assert blocked_memory.read_bytes() == before
        assert backoff["status"] == BLOCKED_SET_UNCHANGED
        # Not the cheap path: it re-derived the blocked set under the new inputs.
        assert backoff["probe"] == "dry_run_classification"
        assert backoff[CLASSIFIER_INPUTS_CHANGED] is True
        assert backoff["blocked_set"]["fingerprint"] == fingerprint
        assert "classifier inputs" in backoff["detail"]

        # …and re-pinned them, so the next decision may be cheap again.
        assert (
            _record(changed, blocked_memory)["classifier_inputs_hash"]
            == classifier_inputs_hash(changed)
        )
        again = auto_maintain_after_write(changed, memory_path=blocked_memory)
        assert again["auto_compaction_backoff"]["probe"] == "source_unchanged"
        assert again["auto_compaction_backoff"][CLASSIFIER_INPUTS_CHANGED] is False

    def test_a_classifier_change_that_moves_the_blocked_set_runs_a_pass(
        self, tmp_path, blocked_memory, monkeypatch
    ):
        """The outcome the shortcut would have hidden until the next edit."""
        cfg = _cfg(tmp_path)
        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert first["compact"]["blocked_from_l1"] == 2
        before = blocked_memory.read_bytes()

        # Re-tag the index: the "[L0] infra: …" pointer stops being an index
        # entry and becomes a third candidate. Identical bytes, different
        # blocked set.
        monkeypatch.setenv("LAYERED_MEMORY_L0_TAG", "[IDX]")
        retagged = _cfg(tmp_path)
        assert blocked_memory.read_bytes() == before

        decision = _auto_compaction_decision(
            retagged, blocked_memory, trigger_reason="test"
        )
        assert decision["skip"] is False
        assert decision["status"] == "blocked_set_changed"
        assert decision[CLASSIFIER_INPUTS_CHANGED] is True

        report = auto_maintain_after_write(retagged, memory_path=blocked_memory)
        compact = report["compact"]
        assert compact["triggered"] is True
        assert compact["backoff_decision"] == "blocked_set_changed"
        assert compact["classifier_inputs_changed"] is True
        assert compact["blocked_from_l1"] == 3

    def test_an_undescribable_classifier_fails_open(
        self, tmp_path, blocked_memory, monkeypatch
    ):
        """Not knowing the inputs is not evidence that they are unchanged."""
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"][
            "status"
        ] == "backed_off"

        monkeypatch.setattr(
            "layered_memory_mcp.routing.classifier_inputs_hash",
            lambda config=None: None,
        )
        decision = _auto_compaction_decision(cfg, blocked_memory, trigger_reason="t")
        assert decision["skip"] is False
        assert decision["status"] == "classifier_inputs_unavailable"

        compact = auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"]
        assert compact["triggered"] is True
        # An unknown hash is recorded as unknown, and never matches later.
        assert _record(cfg, blocked_memory)["classifier_inputs_hash"] is None
        assert (
            _auto_compaction_decision(cfg, blocked_memory, trigger_reason="t")["status"]
            == "classifier_inputs_unavailable"
        )

    def test_a_record_without_a_classifier_hash_is_not_shortcutted(
        self, tmp_path, blocked_memory
    ):
        """A schema-1 record, or one written with an unknown classifier."""
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        path = state_path(cfg)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["targets"][target_key(blocked_memory)]["classifier_inputs_hash"] = None
        path.write_text(json.dumps(doc), encoding="utf-8")

        backoff = auto_maintain_after_write(cfg, memory_path=blocked_memory)[
            "auto_compaction_backoff"
        ]
        assert backoff["probe"] == "dry_run_classification"
        assert backoff[CLASSIFIER_INPUTS_CHANGED] is True

    def test_the_schema_bump_ignores_a_version_1_document(
        self, tmp_path, blocked_memory
    ):
        """Derived state: a v1 record costs one pass, never a bad shortcut."""
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        path = state_path(cfg)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert SCHEMA_VERSION == 2
        doc["schema_version"] = 1
        doc["targets"][target_key(blocked_memory)].pop("classifier_inputs_hash")
        path.write_text(json.dumps(doc), encoding="utf-8")

        compact = auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"]
        assert compact["triggered"] is True
        assert compact["backoff_decision"] == "no_recorded_blocked_set"
        # Re-recorded under the current schema, with the hash populated.
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2
        assert _record(cfg, blocked_memory)["classifier_inputs_hash"]

    def test_a_skip_never_erases_the_pins_it_is_checked_against(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        store = AutoCompactionState(state_path(cfg))
        key = target_key(blocked_memory)
        before = store.get(key)

        # Neither pin recomputed by the caller → neither overwritten.
        after = store.note_skipped_pass(key)
        assert after["source_digest"] == before["source_digest"]
        assert after["classifier_inputs_hash"] == before["classifier_inputs_hash"]
        assert after["skipped_passes"] == before["skipped_passes"] + 1


# ---------------------------------------------------------------------------
# Gap 1 — the cadence marker split
# ---------------------------------------------------------------------------


class TestCompactionMarkerStillMeansCompaction:
    def test_a_real_pass_stamps_it_and_a_backoff_does_not(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)

        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert first["compact"]["triggered"] is True
        compacted_at = _marker_value(cfg, MARKER_LAST_COMPACT)
        assert compacted_at
        assert first["compact"]["attempt_marker"]["marker"] == MARKER_LAST_ATTEMPT
        assert _marker_value(cfg, MARKER_LAST_ATTEMPT)

        time.sleep(0.01)
        second = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert second["compact"]["status"] == "backed_off"

        # The lie this split exists to prevent: a backoff claiming a pass.
        assert _marker_value(cfg, MARKER_LAST_COMPACT) == compacted_at
        # …while still recording that a decision was reached.
        attempt = second["auto_compaction_backoff"]["attempt_marker"]
        assert attempt["marker"] == MARKER_LAST_ATTEMPT
        assert attempt["recorded_at"]
        assert attempt["throttles"] == "interval_trigger"
        assert float(_marker_value(cfg, MARKER_LAST_ATTEMPT)) > float(compacted_at)

    def test_the_three_markers_are_separate_files_under_the_home(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        for name in (MARKER_LAST_COMPACT, MARKER_LAST_ATTEMPT, MARKER_LAST_L1_DEDUP):
            marker = _marker(cfg, name)
            assert marker.is_file(), name
            assert str(tmp_path) in str(marker)
        assert len({MARKER_LAST_COMPACT, MARKER_LAST_ATTEMPT, MARKER_LAST_L1_DEDUP}) == 3


class TestAttemptCadenceThrottlesTheIntervalTrigger:
    def test_an_interval_only_trigger_stops_deciding_on_every_write(
        self, tmp_path, blocked_memory
    ):
        cfg = _interval_cfg(tmp_path)

        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert first["compact"]["triggered"] is True
        assert "interval elapsed" in first["compact"]["reason"]

        second = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        compact = second["compact"]
        assert compact["triggered"] is False
        assert compact["status"] == "not_triggered"
        # Held, not absent — a throttle must not read as "memory is fine".
        throttled = compact["throttled"]
        assert throttled["trigger"] == "interval"
        assert throttled["marker"] == MARKER_LAST_ATTEMPT
        assert throttled["attempt_interval_hours"] == 1.0
        assert throttled["last_attempt_at"] and throttled["retry_after"]
        assert "interval elapsed" in throttled["reason"]
        # No decision was reached, so nothing was skipped or re-recorded.
        assert "auto_compaction_backoff" not in second
        assert _record(cfg, blocked_memory)["skipped_passes"] == 0

    def test_the_throttle_is_what_holds_it(self, tmp_path, blocked_memory):
        """Same state, throttle disabled → the decision is reached again.

        Pins the cause: without ``auto_maintain_attempt_interval_hours`` the
        pre-3.5.7 behaviour is back (a decision per write), which is what the
        knob is for.
        """
        cfg = _interval_cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert (
            auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"][
                "status"
            ]
            == "not_triggered"
        )

        unthrottled = _interval_cfg(tmp_path, auto_maintain_attempt_interval_hours=0.0)
        report = auto_maintain_after_write(unthrottled, memory_path=blocked_memory)
        assert report["compact"]["status"] == "backed_off"
        assert report["auto_compaction_backoff"]["skipped_passes"] == 1

    def test_a_new_blocked_entry_is_not_delayed_by_a_recent_attempt(
        self, tmp_path, blocked_memory
    ):
        cfg = _interval_cfg(tmp_path)
        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        fingerprint = first["compact"]["blocked_set"]["fingerprint"]
        assert (
            auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"][
                "status"
            ]
            == "not_triggered"
        )

        with blocked_memory.open("a", encoding="utf-8") as fh:
            fh.write(f"{SEPARATOR}\n{LOG_ENTRY}")

        compact = auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"]
        assert compact["triggered"] is True
        assert "throttled" not in compact
        assert compact["backoff_decision"] == "blocked_set_changed"
        assert compact["blocked_from_l1"] == 3
        assert compact["blocked_set"]["fingerprint"] != fingerprint

    def test_a_new_l1_candidate_is_not_delayed_by_a_recent_attempt(
        self, tmp_path, blocked_memory
    ):
        cfg = _interval_cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert (
            auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"][
                "status"
            ]
            == "not_triggered"
        )

        with blocked_memory.open("a", encoding="utf-8") as fh:
            fh.write(f"{SEPARATOR}\n{KNOWLEDGE_ENTRY}\n")

        compact = auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"]
        assert compact["triggered"] is True
        assert compact["status"] == "migrated"
        assert compact["migrated_count"] == 1
        assert compact["backoff_decision"] == "l1_work_pending"
        written = "\n".join(
            p.read_text(encoding="utf-8") for p in Path(cfg.knowledge_dir).glob("*.md")
        )
        assert "routing.py 只做确定性的标记与结构匹配" in written

    def test_the_usage_trigger_is_never_throttled(self, tmp_path, blocked_memory):
        """Trigger A describes a file that is actually filling up.

        It still reaches the decision on every write — and the decision it
        reaches is the cheap one (bytes plus classifier inputs), which is the
        whole point of the backoff.
        """
        cfg = _cfg(tmp_path)  # usage-ratio trigger, interval a century out
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        for expected in (1, 2, 3):
            report = auto_maintain_after_write(cfg, memory_path=blocked_memory)
            assert report["compact"]["status"] == "backed_off"
            backoff = report["auto_compaction_backoff"]
            assert backoff["skipped_passes"] == expected
            assert backoff["probe"] == "source_unchanged"
            assert "usage" in backoff["trigger_reason"]

    def test_the_attempt_interval_never_outlives_the_compaction_interval(
        self, tmp_path
    ):
        """A throttle longer than the interval could delay a real pass."""
        cfg = _cfg(
            tmp_path,
            auto_maintain_interval_days=0.5,  # 12h
            auto_maintain_attempt_interval_hours=48.0,
        )
        assert _attempt_interval_seconds(cfg) == 0.5 * 86400

        disabled = _cfg(tmp_path, auto_maintain_attempt_interval_hours=0.0)
        assert _attempt_interval_seconds(disabled) == 0.0

        negative = _cfg(tmp_path, auto_maintain_attempt_interval_hours=-5.0)
        assert _attempt_interval_seconds(negative) == 0.0

        assert _cfg(tmp_path).auto_maintain_attempt_interval_hours == 1.0


class TestL1DedupHasItsOwnCadence:
    def test_a_backed_off_pass_does_not_drag_a_dedup_scan_along(
        self, tmp_path, blocked_memory
    ):
        """The regression this marker exists for.

        A backed-off pass leaves ``.last_auto_compact`` alone — correctly,
        nothing was compacted — and the dedup step used to read that marker. On
        an install that has been backing off for a while, every single write
        therefore re-ran a full scan of every knowledge file plus the
        vector-store prune, to find nothing.
        """
        cfg = _cfg(tmp_path, auto_maintain_interval_days=0.1)
        first = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert first["l1_dedup"]["triggered"] is True
        assert _marker(cfg, MARKER_LAST_L1_DEDUP).is_file()

        # An install whose last *real* pass is long gone, as a long-running
        # backoff produces.
        _marker(cfg, MARKER_LAST_COMPACT).write_text(
            str(time.time() - 10_000_000), encoding="utf-8"
        )

        report = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert report["compact"]["status"] == "backed_off"
        assert report["l1_dedup"] == {"triggered": False}

    def test_a_due_dedup_still_runs_while_compaction_backs_off(
        self, tmp_path, blocked_memory
    ):
        """Independence cuts both ways: a backoff must not suppress it."""
        cfg = _cfg(tmp_path, auto_maintain_interval_days=0.1)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        _marker(cfg, MARKER_LAST_L1_DEDUP).write_text(
            str(time.time() - 10_000_000), encoding="utf-8"
        )

        report = auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert report["compact"]["status"] == "backed_off"
        assert report["l1_dedup"]["triggered"] is True
        assert "scanned" in report["l1_dedup"]

    def test_an_upgrading_install_continues_from_the_old_shared_marker(
        self, tmp_path
    ):
        """No dedup marker yet is an upgrade, not "never deduped"."""
        cfg = _cfg(tmp_path)
        stamp = time.time() - 42.0
        _marker(cfg, MARKER_LAST_COMPACT).write_text(str(stamp), encoding="utf-8")
        assert not _marker(cfg, MARKER_LAST_L1_DEDUP).exists()

        assert _read_last_l1_dedup_time(cfg) == pytest.approx(stamp)

        _marker(cfg, MARKER_LAST_L1_DEDUP).write_text("1000.0", encoding="utf-8")
        assert _read_last_l1_dedup_time(cfg) == 1000.0


# ---------------------------------------------------------------------------
# Unchanged contracts
# ---------------------------------------------------------------------------


class TestExplicitCompactionIgnoresEveryMarker:
    def test_it_runs_with_fresh_markers_and_a_recorded_blocked_set(
        self, tmp_path, blocked_memory
    ):
        cfg = _interval_cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)
        assert (
            auto_maintain_after_write(cfg, memory_path=blocked_memory)["compact"][
                "status"
            ]
            == "not_triggered"
        )
        markers = {
            name: _marker_value(cfg, name)
            for name in (MARKER_LAST_COMPACT, MARKER_LAST_ATTEMPT, MARKER_LAST_L1_DEDUP)
        }
        state_before = state_path(cfg).read_text(encoding="utf-8")

        report = compact_memory(cfg, memory_path=blocked_memory, dry_run=False)

        assert report["success"] is True
        assert report["file_written"] is True
        assert report["routing_summary"]["blocked_from_l1"] == 2
        assert report["classifier_inputs_hash"] == classifier_inputs_hash(cfg)
        assert "backed_off" not in json.dumps(report, ensure_ascii=False)
        assert "throttled" not in json.dumps(report, ensure_ascii=False)
        # An explicit call is intent: it neither reads nor advances any cadence.
        for name, value in markers.items():
            assert _marker_value(cfg, name) == value, name
        assert state_path(cfg).read_text(encoding="utf-8") == state_before


class TestAdvisoryModeIsUnaffected:
    def test_no_state_no_backoff_no_throttle(self, tmp_path):
        cfg = _cfg(tmp_path, mode="advisory")
        mem = _write_memory(tmp_path, [POINTER, SKILL_ENTRY, PROFILE_ENTRY])

        report = auto_maintain_after_write(cfg, memory_path=mem)
        compact = report["compact"]
        assert compact["triggered"] is True
        assert compact["status"] == "migrated"
        assert compact["blocked_from_l1"] == 0
        assert "blocked_set_state" not in compact
        assert "throttled" not in compact
        assert "auto_compaction_backoff" not in report
        assert not state_path(cfg).exists()

    def test_advisory_leaves_nothing_to_throttle(self, tmp_path):
        """A pass migrates everything, so the trigger stops firing by itself."""
        cfg = _interval_cfg(tmp_path, mode="advisory")
        mem = _write_memory(tmp_path, [POINTER, KNOWLEDGE_ENTRY])

        assert auto_maintain_after_write(cfg, memory_path=mem)["compact"][
            "status"
        ] == "migrated"
        second = auto_maintain_after_write(cfg, memory_path=mem)["compact"]
        # Nothing left to move: not throttled, simply nothing to do.
        assert second["status"] == "not_triggered"
        assert "throttled" not in second
        assert not state_path(cfg).exists()


class TestNothingTouchesProductionPaths:
    def test_every_file_this_feature_writes_is_under_the_temp_home(
        self, tmp_path, blocked_memory
    ):
        cfg = _cfg(tmp_path)
        auto_maintain_after_write(cfg, memory_path=blocked_memory)

        written = [
            state_path(cfg),
            _marker(cfg, MARKER_LAST_COMPACT),
            _marker(cfg, MARKER_LAST_ATTEMPT),
            _marker(cfg, MARKER_LAST_L1_DEDUP),
        ]
        for path in written:
            assert path.is_file()
            # Under the per-test temp home, not a user-level location.
            # (conftest's `_guard_production_paths` turns any escape into a
            # failure; this says the same thing about the paths themselves.)
            assert path.is_relative_to(tmp_path), path
        assert blocked_memory.is_relative_to(tmp_path)

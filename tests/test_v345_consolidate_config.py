"""v3.4.5 — the consolidation knobs are readable from the operator's config.yaml.

Why this test file exists: `consolidate_size_ceiling` was settable only through a
constructor argument or an env var, so on a deployed host it was effectively
frozen at its 0.9 default — a recommended tightening to 0.7 stayed "recommended"
for a week while every write kept being judged against 0.9. A tuning knob whose
only local spelling is "edit the library" or "add an env var to a systemd unit"
is a knob that never moves.
"""

from __future__ import annotations

import pytest

from layered_memory_mcp.config import MemoryConfig


def _cfg(tmp_path, monkeypatch, **kwargs) -> MemoryConfig:
    """Config rooted at ``tmp_path``.

    Note: this deliberately does NOT clear the consolidate env vars — the tests
    that set them call monkeypatch.setenv *before* building the config, which is
    the only ordering in which they can mean anything. The defaults test clears
    them itself.
    """
    return MemoryConfig(home=str(tmp_path), **kwargs)


class TestConsolidateConfigFromYaml:
    def test_defaults_when_nothing_is_configured(self, tmp_path, monkeypatch):
        for name in (
            "LAYERED_MEMORY_CONSOLIDATE_ENABLED",
            "LAYERED_MEMORY_CONSOLIDATE_MIN_FAMILY",
            "LAYERED_MEMORY_CONSOLIDATE_SIZE_CEILING",
        ):
            monkeypatch.delenv(name, raising=False)
        cfg = _cfg(tmp_path, monkeypatch)
        assert cfg.consolidate_enabled is True
        assert cfg.consolidate_min_family == 2
        assert cfg.consolidate_size_ceiling == 0.9

    def test_reads_section_from_config_yaml(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "consolidate:\n"
            "  enabled: true\n"
            "  min_family: 3\n"
            "  size_ceiling: 0.7\n",
            encoding="utf-8",
        )
        cfg = _cfg(tmp_path, monkeypatch)
        assert cfg.consolidate_enabled is True
        assert cfg.consolidate_min_family == 3
        assert cfg.consolidate_size_ceiling == 0.7

    def test_yaml_boolean_off_is_coerced(self, tmp_path, monkeypatch):
        """YAML 1.1 parses bare ``off`` as False before the loader sees it."""
        (tmp_path / "config.yaml").write_text(
            "consolidate:\n  enabled: off\n", encoding="utf-8"
        )
        assert _cfg(tmp_path, monkeypatch).consolidate_enabled is False

    def test_yaml_kill_switch_leaves_other_knobs_alone(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "consolidate:\n  enabled: false\n  size_ceiling: 0.6\n", encoding="utf-8"
        )
        cfg = _cfg(tmp_path, monkeypatch)
        assert cfg.consolidate_enabled is False
        assert cfg.consolidate_size_ceiling == 0.6

    def test_env_beats_yaml(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "consolidate:\n  size_ceiling: 0.7\n  min_family: 3\n", encoding="utf-8"
        )
        monkeypatch.setenv("LAYERED_MEMORY_CONSOLIDATE_SIZE_CEILING", "0.5")
        monkeypatch.setenv("LAYERED_MEMORY_CONSOLIDATE_MIN_FAMILY", "4")
        cfg = _cfg(tmp_path, monkeypatch)
        assert cfg.consolidate_size_ceiling == 0.5
        assert cfg.consolidate_min_family == 4

    def test_constructor_argument_wins(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "consolidate:\n  size_ceiling: 0.7\n", encoding="utf-8"
        )
        monkeypatch.setenv("LAYERED_MEMORY_CONSOLIDATE_SIZE_CEILING", "0.5")
        cfg = MemoryConfig(home=str(tmp_path), consolidate_size_ceiling=0.65)
        assert cfg.consolidate_size_ceiling == 0.65

    def test_broken_yaml_falls_back_to_defaults(self, tmp_path, monkeypatch):
        """A broken config.yaml must never take the server down at start-up."""
        (tmp_path / "config.yaml").write_text(
            "consolidate: [unclosed\n", encoding="utf-8"
        )
        cfg = _cfg(tmp_path, monkeypatch)
        assert cfg.consolidate_size_ceiling == 0.9
        assert cfg.consolidate_min_family == 2

    def test_non_mapping_section_falls_back(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "consolidate: 0.7\n", encoding="utf-8"
        )
        assert _cfg(tmp_path, monkeypatch).consolidate_size_ceiling == 0.9

    def test_garbage_values_fall_back_per_key(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "consolidate:\n  size_ceiling: nonsense\n  min_family: also-nonsense\n",
            encoding="utf-8",
        )
        cfg = _cfg(tmp_path, monkeypatch)
        assert cfg.consolidate_size_ceiling == 0.9
        assert cfg.consolidate_min_family == 2

    def test_other_sections_unaffected(self, tmp_path, monkeypatch):
        """The new section must not disturb write_guard / session_scan reading."""
        (tmp_path / "config.yaml").write_text(
            "write_guard: auto\n"
            "consolidate:\n  size_ceiling: 0.7\n"
            "session_scan:\n  exclude_markers: [secret]\n",
            encoding="utf-8",
        )
        cfg = _cfg(tmp_path, monkeypatch)
        assert cfg.write_guard == "auto"
        assert cfg.consolidate_size_ceiling == 0.7
        assert "secret" in cfg.session_exclude_markers


class TestCeilingReachesTheWritePath:
    """The configured ceiling must be the one the write-back check compares to."""

    def test_writeback_ratio_is_judged_against_configured_ceiling(
        self, tmp_path, monkeypatch
    ):
        from layered_memory_mcp.injector import _consolidation_writeback

        kb = tmp_path / "knowledge"
        kb.mkdir()
        (tmp_path / "config.yaml").write_text(
            "consolidate:\n  size_ceiling: 0.7\n", encoding="utf-8"
        )
        cfg = _cfg(tmp_path, monkeypatch)
        cfg.knowledge_dir = kb

        filepath = kb / "demo.md"
        filepath.write_text(
            "# demo\n\n## 横评 2026-09-22 期\n\n" + "甲" * 200 + "\n"
            "\n## 横评 2026-09-26 期\n\n" + "乙" * 200 + "\n",
            encoding="utf-8",
        )
        from layered_memory_mcp.injector import _family_hash, _heading_family

        members = _heading_family(filepath.read_text(encoding="utf-8"), "横评 2026-10-03 期")
        assert len(members) == 2, "fixture must look like a two-member family"
        token = _family_hash(members)

        # 0.8 of the family (≈320 of 400 bytes) — under the old 0.9 default this
        # was accepted, under the configured 0.7 it must be refused.
        borderline = "丙" * 320
        result = _consolidation_writeback(
            cfg, filepath, "横评 2026-10-03 期", token, borderline
        )
        assert result is not None and "refused" in result, result
        assert result["refused"]["ceiling"] == 0.7
        assert result["refused"]["action"] == "consolidate_refused"

        # A real consolidation (≈0.25 of the family) goes through.
        real = "丙" * 100
        ok = _consolidation_writeback(cfg, filepath, "横评 2026-10-03 期", token, real)
        assert ok == {"ok": True, "family_size": 2}, ok

    def test_disabled_kill_switch_skips_the_gate(self, tmp_path, monkeypatch):
        from layered_memory_mcp.injector import _family_gate

        kb = tmp_path / "knowledge"
        kb.mkdir()
        (tmp_path / "config.yaml").write_text(
            "consolidate:\n  enabled: false\n", encoding="utf-8"
        )
        cfg = _cfg(tmp_path, monkeypatch)
        cfg.knowledge_dir = kb
        path = kb / "demo.md"
        path.write_text(
            "# demo\n\n## 横评 2026-09-22 期\n\n甲甲甲\n", encoding="utf-8"
        )
        assert _family_gate(cfg, path, "横评 2026-10-03 期", "新内容") is None


@pytest.mark.parametrize("value,expected", [("0.7", 0.7), (0.75, 0.75)])
def test_yaml_accepts_string_and_number(tmp_path, monkeypatch, value, expected):
    (tmp_path / "config.yaml").write_text(
        f"consolidate:\n  size_ceiling: {value}\n", encoding="utf-8"
    )
    assert _cfg(tmp_path, monkeypatch).consolidate_size_ceiling == expected

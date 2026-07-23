"""Tests for v2.11.0: unified domain-keyword source + migration CLI.

Covers the design's test matrix:
  - _dict_to_rules pure conversion
  - compact: empty domain_keywords -> no migration suggestion
  - compact: populated domain_keywords -> suggestion by table
  - migrate: seeds classic default, idempotent (no re-write)
  - migrate --dry-run: preview only, no write
  - migrate: already-configured is not overwritten unless --force
  - config.yaml -> MemoryConfig.domain_keywords loading chain

All fixtures use neutral technical placeholders (database / networking /
topic-a) only.
"""

import yaml

from layered_memory_mcp.config import MemoryConfig, _load_domain_keywords_from_yaml
from layered_memory_mcp.memory_compactor import (
    _dict_to_rules,
    _get_domain_rules,
    _suggest_migration,
)
from layered_memory_mcp import migrate


# ---------------------------------------------------------------------------
# _dict_to_rules — pure conversion
# ---------------------------------------------------------------------------

class TestDictToRules:
    def test_basic_conversion(self):
        dk = {"database": ["sql", "index"], "networking": ["tcp", "dns"]}
        rules = _dict_to_rules(dk)
        assert rules == [
            ("database", ["sql", "index"]),
            ("networking", ["tcp", "dns"]),
        ]

    def test_empty_dict_is_empty_list(self):
        assert _dict_to_rules({}) == []

    def test_none_is_empty_list(self):
        assert _dict_to_rules(None) == []

    def test_string_value_coerced_to_list(self):
        assert _dict_to_rules({"database": "sql"}) == [("database", ["sql"])]

    def test_values_coerced_to_strings(self):
        rules = _dict_to_rules({"topic-a": [1, 2]})
        assert rules == [("topic-a", ["1", "2"])]


# ---------------------------------------------------------------------------
# compact: domain rules derived only from config.domain_keywords
# ---------------------------------------------------------------------------

class TestCompactDomainRules:
    def test_empty_keywords_no_suggestion(self, tmp_path):
        """Empty domain_keywords -> empty rules -> unmatched entry stays misc."""
        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(tmp_path / "knowledge"),
        )
        assert config.domain_keywords == {}

        rules = _get_domain_rules(config)
        assert rules == []

        result = _suggest_migration(
            "run the sql query and rebuild the index", domain_rules=rules
        )
        assert result["domain"] == "misc"

    def test_populated_keywords_suggests_domain(self, tmp_path):
        """Populated domain_keywords -> compaction suggests by the table."""
        config = MemoryConfig(
            home=str(tmp_path),
            knowledge_dir=str(tmp_path / "knowledge"),
            domain_keywords={
                "database": ["sql", "query", "index"],
                "networking": ["tcp", "dns", "proxy"],
            },
        )
        rules = _get_domain_rules(config)
        assert ("database", ["sql", "query", "index"]) in rules

        assert (
            _suggest_migration("optimize the sql query", domain_rules=rules)["domain"]
            == "database"
        )
        assert (
            _suggest_migration("inspect the dns proxy", domain_rules=rules)["domain"]
            == "networking"
        )
        # Content matching nothing still routes to misc.
        assert (
            _suggest_migration("topic-a placeholder note", domain_rules=rules)["domain"]
            == "misc"
        )


# ---------------------------------------------------------------------------
# config.yaml -> MemoryConfig.domain_keywords loading chain
# ---------------------------------------------------------------------------

class TestConfigYamlLoading:
    def test_load_helper_reads_section(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"domain_keywords": {"database": ["sql", "index"]}}),
            encoding="utf-8",
        )
        assert _load_domain_keywords_from_yaml(tmp_path) == {
            "database": ["sql", "index"]
        }

    def test_load_helper_missing_file(self, tmp_path):
        assert _load_domain_keywords_from_yaml(tmp_path) == {}

    def test_load_helper_no_section(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"other_key": 1}), encoding="utf-8"
        )
        assert _load_domain_keywords_from_yaml(tmp_path) == {}

    def test_load_helper_malformed_is_empty(self, tmp_path):
        (tmp_path / "config.yaml").write_text("::: not valid yaml :::", encoding="utf-8")
        assert _load_domain_keywords_from_yaml(tmp_path) == {}

    def test_memoryconfig_reads_config_yaml(self, tmp_path):
        """End-to-end: a config.yaml table is visible on config.domain_keywords."""
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"domain_keywords": {"networking": ["tcp", "dns"]}}),
            encoding="utf-8",
        )
        config = MemoryConfig(home=str(tmp_path))
        assert config.domain_keywords == {"networking": ["tcp", "dns"]}

    def test_constructor_beats_config_yaml(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"domain_keywords": {"networking": ["tcp"]}}),
            encoding="utf-8",
        )
        config = MemoryConfig(
            home=str(tmp_path), domain_keywords={"database": ["sql"]}
        )
        assert config.domain_keywords == {"database": ["sql"]}

    def test_env_beats_config_yaml(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"domain_keywords": {"networking": ["tcp"]}}),
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "LAYERED_MEMORY_DOMAIN_KEYWORDS", '{"database": ["sql"]}'
        )
        config = MemoryConfig(home=str(tmp_path))
        assert config.domain_keywords == {"database": ["sql"]}


# ---------------------------------------------------------------------------
# migrate CLI
# ---------------------------------------------------------------------------

class TestMigrateCLI:
    def test_writes_classic_default_when_unconfigured(self, tmp_path, capsys):
        cfg = tmp_path / "config.yaml"
        rc = migrate._run(["--config", str(cfg)])
        assert rc == 0
        assert cfg.exists()

        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert set(data["domain_keywords"].keys()) == {"infra", "dev", "docs"}
        # Classic table is purely technical — no business presets.
        assert data["domain_keywords"] == migrate._CLASSIC_DEFAULT_DOMAIN_KEYWORDS

        out = capsys.readouterr().out
        assert "domain_keywords" in out

    def test_written_table_is_readable_by_config(self, tmp_path):
        """A migrated config.yaml is actually consumed by MemoryConfig."""
        cfg = tmp_path / "config.yaml"
        migrate._run(["--config", str(cfg)])

        config = MemoryConfig(home=str(tmp_path))
        assert config.domain_keywords == migrate._CLASSIC_DEFAULT_DOMAIN_KEYWORDS

    def test_idempotent_second_run_does_not_rewrite(self, tmp_path, capsys):
        cfg = tmp_path / "config.yaml"
        migrate._run(["--config", str(cfg)])
        first = cfg.read_text(encoding="utf-8")
        capsys.readouterr()  # drain

        rc = migrate._run(["--config", str(cfg)])
        assert rc == 0
        assert cfg.read_text(encoding="utf-8") == first  # unchanged

        out = capsys.readouterr().out
        assert "already defines domain_keywords" in out

    def test_dry_run_previews_without_writing(self, tmp_path, capsys):
        cfg = tmp_path / "config.yaml"
        rc = migrate._run(["--config", str(cfg), "--dry-run"])
        assert rc == 0
        assert not cfg.exists()  # nothing written

        out = capsys.readouterr().out
        assert "dry-run" in out
        assert "infra" in out

    def test_existing_table_not_overwritten(self, tmp_path, capsys):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            yaml.safe_dump({"domain_keywords": {"topic-a": ["placeholder"]}}),
            encoding="utf-8",
        )
        rc = migrate._run(["--config", str(cfg)])
        assert rc == 0

        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["domain_keywords"] == {"topic-a": ["placeholder"]}  # preserved

        out = capsys.readouterr().out
        assert "already defines domain_keywords" in out

    def test_force_overwrites_existing_table(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            yaml.safe_dump({"domain_keywords": {"topic-a": ["placeholder"]}}),
            encoding="utf-8",
        )
        rc = migrate._run(["--config", str(cfg), "--force"])
        assert rc == 0

        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["domain_keywords"] == migrate._CLASSIC_DEFAULT_DOMAIN_KEYWORDS

    def test_appends_to_existing_file_preserving_other_keys(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "# my existing config\nnamespace: work\n", encoding="utf-8"
        )
        rc = migrate._run(["--config", str(cfg)])
        assert rc == 0

        text = cfg.read_text(encoding="utf-8")
        assert "# my existing config" in text  # original comment preserved
        data = yaml.safe_load(text)
        assert data["namespace"] == "work"  # original key preserved
        assert set(data["domain_keywords"].keys()) == {"infra", "dev", "docs"}

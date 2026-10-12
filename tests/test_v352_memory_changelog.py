"""MEMORY_CHANGELOG — the persistent provenance backend.

Why this file exists: the surface model declared `MEMORY_CHANGELOG` a
framework-owned common-core surface, but there was no store behind it. "Why
does my knowledge base say this now?" had no answer, and the capability output
had to admit it.

These tests pin the two properties that make a provenance log worth having, and
the one that makes it safe:

  * **It records real mutations.** A successful L1 write, a fusion write-back,
    a whole-file create/update/delete, an L0 entry that appeared or vanished.
  * **It records nothing else.** A deferral handshake, a refused fuse, a
    validation failure, a failed write, a dry-run compaction, a skipped
    duplicate, an L0 resync that moved no entry — all of them leave the log
    exactly as it was. A changelog that logs attempts is the generic event log
    this surface is explicitly not.
  * **It never copies content.** Events carry hashes, byte counts and the
    heading — never the body. A knowledge file full of credentials must not get
    a second, unindexed copy in a file nobody thinks of as sensitive.

Plus the mechanical guarantee the whole thing rests on: the store is
append-only, so an earlier event stays byte-identical forever.

Every test runs against a temp home (conftest's sandbox fixtures are the
backstop); nothing here may resolve the real `~/.hermes` or `~/.layered-memory`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from layered_memory_mcp.config import MemoryConfig
from layered_memory_mcp.injector import _hash_body, inject_knowledge
from layered_memory_mcp.l0_manager import manage_entry, sync_l0_index
from layered_memory_mcp.memory_changelog import (
    NOT_RECORDED,
    SCHEMA_VERSION,
    changelog_settings,
    changelog_stats,
    hash_text,
    read_events,
    record,
)
from layered_memory_mcp.routing import surface_capabilities
from layered_memory_mcp.storage import memory_changelog_store as store_mod
from layered_memory_mcp.storage.memory_changelog_store import (
    MAX_READ_LIMIT,
    MemoryChangelogStore,
)

#: A fake credential, shaped like the real thing. The point of the fixture is
#: that it appears in an L1 body and must NOT appear anywhere in the changelog.
FAKE_SECRET = "sk-live-DEADBEEFcafef00d0123456789abcdef"


def _cfg(tmp_path: Path, **kwargs) -> MemoryConfig:
    home = tmp_path / ".layered-memory"
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    return MemoryConfig(
        home=str(home), knowledge_dir=str(home / "knowledge"), **kwargs
    )


def _cfg_with_l0(tmp_path: Path, **kwargs) -> MemoryConfig:
    """A config whose L0 index is an actual file, so L0 writes can be observed."""
    cfg = _cfg(tmp_path, **kwargs)
    cfg.l0_index_file = Path(cfg.home) / "L0.md"
    return cfg


def _log_path(cfg: MemoryConfig) -> Path:
    return Path(changelog_settings(cfg)["path"])


def _events(cfg: MemoryConfig) -> list[dict]:
    """Every recorded event, in append order."""
    path = _log_path(cfg)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    ]


def _ops(cfg: MemoryConfig) -> list[str]:
    return [e["operation"] for e in _events(cfg)]


def _bloated_memory(tmp_path: Path) -> tuple[MemoryConfig, Path]:
    """An agent memory file with one migratable entry, plus a matching config.

    The memory path is configured explicitly, which is what makes the
    compaction pass guard-safe (see test_v351_agent_memory_guard.py).
    """
    memory = tmp_path / "agent-memory.md"
    memory.write_text(
        "[L0] infra: proxy notes → knowledge/infra.md\n"
        "§\n"
        "[架构边界] A long entry about the service boundary between the ingest "
        "workers and the API that plainly belongs in a durable L1 knowledge "
        "file rather than in the agent's index layer.\n",
        encoding="utf-8",
    )
    return _cfg(tmp_path, agent_memory={"path": str(memory)}), memory


# --- A. storage mechanics ----------------------------------------------------


class TestAppendOnlyStore:
    def test_default_path_is_under_the_home_data_dir(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert _log_path(cfg) == Path(cfg.home) / "data" / "memory_changelog.jsonl"

    def test_configured_path_wins(self, tmp_path):
        target = tmp_path / "elsewhere" / "provenance.jsonl"
        cfg = _cfg(tmp_path, memory_changelog={"path": str(target)})
        assert _log_path(cfg) == target
        assert record(cfg, "l1_section_created", "infra") is not None
        assert target.exists()

    def test_second_write_appends_and_leaves_the_first_event_byte_identical(
        self, tmp_path
    ):
        cfg = _cfg(tmp_path)
        record(cfg, "l1_section_created", "infra", section="A")
        first_bytes = _log_path(cfg).read_bytes()

        record(cfg, "l1_section_updated", "infra", section="B")
        after_bytes = _log_path(cfg).read_bytes()

        assert after_bytes.startswith(first_bytes)
        assert len(after_bytes) > len(first_bytes)
        assert _ops(cfg) == ["l1_section_created", "l1_section_updated"]

    def test_one_event_is_always_one_line(self, tmp_path):
        """A multi-line reason must not become two records."""
        cfg = _cfg(tmp_path)
        record(cfg, "l1_section_updated", "infra", reason="line one\nline two")
        assert len(_log_path(cfg).read_text(encoding="utf-8").strip().split("\n")) == 1
        assert _events(cfg)[0]["reason"] == "line one\nline two"

    def test_a_corrupt_line_is_counted_not_fatal(self, tmp_path):
        cfg = _cfg(tmp_path)
        record(cfg, "l1_section_created", "infra")
        with open(_log_path(cfg), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        record(cfg, "l1_section_updated", "infra")

        report = read_events(cfg)
        assert report["total_events"] == 2
        assert report["malformed_lines"] == 1

    def test_append_never_raises_when_the_path_is_unwritable(self, tmp_path):
        """A provenance failure must not be able to fail the write it describes."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        store = MemoryChangelogStore(blocker / "nested" / "log.jsonl")
        assert store.append({"operation": "l1_section_created"}) is False


# --- A2. the read ceiling reports itself -------------------------------------


class TestOversizedReadIsReported:
    """An unreadable log must not read like an empty one.

    The store refuses to parse a file above `MAX_READ_BYTES` (a provenance log
    that big means something is writing non-semantic events into it), and that
    refusal used to be visible only in a server log line — so
    `get_memory_changelog` answered `total_events: 0` with no entries, which is
    exactly what a clean install answers. The ceiling is reached by growth, not
    only by misuse: `retention_days` is reported but never enforced, so the file
    only ever gets bigger.

    `MAX_READ_BYTES` is monkeypatched down rather than writing 16 MiB.
    """

    @pytest.fixture
    def tiny_ceiling(self, monkeypatch):
        monkeypatch.setattr(store_mod, "MAX_READ_BYTES", 64)
        return 64

    def _populated(self, tmp_path: Path) -> MemoryConfig:
        cfg = _cfg(tmp_path)
        for section in ("A", "B", "C"):
            record(cfg, "l1_section_created", "infra", section=section)
        return cfg

    def test_an_oversized_log_is_distinguishable_from_an_empty_one(
        self, tmp_path, tiny_ceiling
    ):
        big = self._populated(tmp_path / "big")
        empty = _cfg(tmp_path / "empty")

        over = read_events(big)
        none = read_events(empty)

        # Both report nothing — that is the whole hazard.
        assert over["total_events"] == 0 and over["entries"] == []
        assert none["total_events"] == 0 and none["entries"] == []
        # ...and the reports are no longer the same answer.
        assert over["read_truncated"] is True
        assert over["read_error"] == store_mod.READ_TOO_LARGE
        assert over["max_read_bytes"] == tiny_ceiling
        assert over["file_size_bytes"] > tiny_ceiling
        assert "NOT parsed" in over["read_note"]
        assert none["read_truncated"] is False
        assert none["read_error"] is None
        assert "read_note" not in none

    def test_stats_flag_the_same_condition(self, tmp_path, tiny_ceiling):
        cfg = self._populated(tmp_path)
        stats = changelog_stats(cfg)
        assert stats["read_truncated"] is True
        assert stats["read_error"] == store_mod.READ_TOO_LARGE
        assert stats["total_events"] == 0
        assert stats["by_operation"] == {}
        # The long-standing size key still agrees with the new one.
        assert stats["size_bytes"] == stats["file_size_bytes"] > tiny_ceiling

    def test_the_oversized_file_is_neither_parsed_nor_touched(
        self, tmp_path, tiny_ceiling
    ):
        cfg = self._populated(tmp_path)
        path = _log_path(cfg)
        before = path.read_bytes()

        assert read_events(cfg)["entries"] == []
        assert changelog_stats(cfg)["total_events"] == 0

        assert path.read_bytes() == before, "the read path never rewrites the log"

    def test_an_absent_log_reports_a_clean_read(self, tmp_path, tiny_ceiling):
        status = MemoryChangelogStore(tmp_path / "nope.jsonl").read_status()
        assert status["read_truncated"] is False
        assert status["read_error"] is None
        assert status["file_size_bytes"] == 0

    def test_a_log_under_the_ceiling_still_parses(self, tmp_path, monkeypatch):
        """Negative control: the flag is about the ceiling, not about reading."""
        cfg = self._populated(tmp_path)
        monkeypatch.setattr(store_mod, "MAX_READ_BYTES", 16 * 1024 * 1024)
        report = read_events(cfg)
        assert report["total_events"] == 3
        assert report["read_truncated"] is False
        assert report["read_error"] is None


# --- B. inclusion rules ------------------------------------------------------


class TestWhatIsRefused:
    def test_unknown_operation_records_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert record(cfg, "something_happened", "infra") is None
        assert _events(cfg) == []

    def test_declared_non_events_record_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        for operation in NOT_RECORDED:
            assert record(cfg, operation, "infra") is None
        assert _events(cfg) == []

    def test_metadata_keeps_structure_and_drops_containers(self, tmp_path):
        """The one way content leaks in is a caller handing over a result dict."""
        cfg = _cfg(tmp_path)
        record(
            cfg,
            "l1_section_updated",
            "infra",
            metadata={
                "bytes_written": 42,
                "write_action": "appended",
                "semantic": {"old_body": FAKE_SECRET},
                "candidates": [FAKE_SECRET],
            },
        )
        metadata = _events(cfg)[0]["metadata"]
        assert metadata == {"bytes_written": 42, "write_action": "appended"}
        assert FAKE_SECRET not in _log_path(cfg).read_text(encoding="utf-8")


# --- C. the L1 write path ----------------------------------------------------


class TestInjectKnowledgeRecording:
    def test_successful_inject_records_exactly_one_event(self, tmp_path):
        cfg = _cfg(tmp_path)
        result = inject_knowledge(
            cfg,
            "infra",
            "WSL proxy",
            "The outbound proxy listens on 127.0.0.1:7890 and the WSL bridge "
            "routes through it.",
        )
        assert result["success"] is True

        events = _events(cfg)
        assert len(events) == 1
        event = events[0]
        assert event["operation"] == "l1_domain_created"
        assert event["surface"] == "l1_knowledge"
        assert event["domain"] == "infra"
        assert event["section"] == "WSL proxy"
        assert event["tool"] == "inject_knowledge"
        assert event["actor"] == "framework"
        assert event["schema_version"] == SCHEMA_VERSION
        assert event["event_id"] and len(event["event_id"]) == 32
        assert event["at"].startswith("20") and "T" in event["at"]
        # A fresh file had no previous body to hash; the new one does.
        assert event["before_hash"] is None
        assert event["after_hash"]
        assert event["metadata"]["write_action"] == "created"
        assert event["metadata"]["bytes_written"] > 0

    def test_event_ids_and_order(self, tmp_path):
        cfg = _cfg(tmp_path)
        inject_knowledge(cfg, "infra", "WSL proxy", "Proxy on 127.0.0.1:7890.")
        inject_knowledge(
            cfg, "infra", "Disk layout", "Data volumes mount under /mnt/data as xfs."
        )

        events = _events(cfg)
        assert _ops(cfg) == ["l1_domain_created", "l1_section_created"]
        assert len({e["event_id"] for e in events}) == 2
        # The read tool hands back the newest first.
        assert read_events(cfg)["entries"][0]["section"] == "Disk layout"

    def test_agent_id_is_carried_as_provenance(self, tmp_path):
        cfg = _cfg(tmp_path)
        inject_knowledge(
            cfg, "infra", "WSL proxy", "Proxy on 127.0.0.1:7890.", agent_id="hermes"
        )
        assert _events(cfg)[0]["agent_id"] == "hermes"

    def test_fusion_writeback_records_the_hash_the_lock_was_issued_under(
        self, tmp_path
    ):
        cfg = _cfg(tmp_path)
        inject_knowledge(
            cfg, "infra", "WSL proxy", "The proxy listens on 127.0.0.1:7890."
        )
        old_body = (
            Path(cfg.knowledge_dir) / "infra.md"
        ).read_text(encoding="utf-8").split("## WSL proxy\n\n", 1)[1].strip()

        result = inject_knowledge(
            cfg,
            "infra",
            "WSL proxy",
            "The proxy listens on 127.0.0.1:7890; the WSL bridge routes through it.",
            fuse=True,
            expected_hash=_hash_body(old_body),
        )
        assert result["action"] == "replaced"

        events = _events(cfg)
        assert len(events) == 2
        fusion = events[-1]
        assert fusion["operation"] == "l1_fusion_writeback"
        # The optimistic lock and the provenance record share one hash
        # definition, so before_hash IS the expected_hash of the handshake.
        assert fusion["before_hash"] == _hash_body(old_body)
        assert fusion["after_hash"] != fusion["before_hash"]
        assert fusion["metadata"]["match_kind"] == "fusion_writeback"

    def test_fuse_conflict_records_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        inject_knowledge(cfg, "infra", "WSL proxy", "Proxy on 127.0.0.1:7890.")
        before = _events(cfg)

        result = inject_knowledge(
            cfg,
            "infra",
            "WSL proxy",
            "A blind overwrite attempt.",
            fuse=True,
            expected_hash="0000000000000000",
        )
        assert result["action"] == "fuse_conflict"
        assert _events(cfg) == before

    def test_deferred_consolidate_records_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        inject_knowledge(
            cfg,
            "reviews",
            "Model review 2026-09-22",
            "The 09-22 round put the parser ahead on tables.",
        )
        before = _events(cfg)

        result = inject_knowledge(
            cfg,
            "reviews",
            "Model review 2026-09-26",
            "The 09-26 round put the parser ahead on formulas too.",
        )
        assert result["action"] == "deferred_consolidate"
        assert _events(cfg) == before, "a handshake is not a mutation"

    def test_consolidation_writeback_is_recorded_as_one_event(self, tmp_path):
        cfg = _cfg(tmp_path)
        # A file that already accumulated a two-member family, written straight
        # to disk: the gate exists precisely to stop a third member, so there is
        # no supported write path that would build this state through the tool.
        (Path(cfg.knowledge_dir) / "reviews.md").write_text(
            "# reviews\n\n"
            "## Model review 2026-09-22\n\n"
            "The 09-22 round put the parser ahead on tables, with a long "
            "supporting note restating the whole comparison.\n\n"
            "## Model review 2026-09-26\n\n"
            "The 09-26 round put the parser ahead on formulas as well, with "
            "another long supporting note restating the whole comparison.\n",
            encoding="utf-8",
        )

        gate = inject_knowledge(
            cfg,
            "reviews",
            "Model review 2026-10-03",
            "The 10-03 round kept the parser ahead.",
        )
        assert gate["action"] == "deferred_consolidate"
        assert gate["family_size"] == 2
        assert _events(cfg) == []

        result = inject_knowledge(
            cfg,
            "reviews",
            "Model review",
            "Parser leads on tables and formulas (09-22, 09-26, 10-03).",
            fuse=True,
            expected_hash=gate["expected_hash"],
        )
        assert result["action"] == "consolidated"
        assert _ops(cfg) == ["l1_consolidation_writeback"]
        event = _events(cfg)[-1]
        assert event["metadata"]["sections_removed"] == 1
        assert event["metadata"]["family_size_before"] == 2
        # A consolidation has no single prior section body; the "before" is the
        # family hash the handshake was issued under.
        assert event["before_hash"] == gate["expected_hash"]
        assert event["after_hash"] != event["before_hash"]

    def test_skipped_duplicate_records_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        content = "The outbound proxy listens on 127.0.0.1:7890 for the WSL bridge."
        inject_knowledge(cfg, "infra", "WSL proxy", content)
        before = _events(cfg)

        result = inject_knowledge(cfg, "infra", "WSL proxy", content)
        assert result["action"] == "skipped"
        assert _events(cfg) == before

    def test_validation_failure_records_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert inject_knowledge(cfg, "infra", "WSL proxy", "   ")["success"] is False
        assert inject_knowledge(cfg, "infra", "", "something")["success"] is False
        assert _events(cfg) == []

    def test_failed_write_records_no_success_event(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        from layered_memory_mcp import injector

        monkeypatch.setattr(
            injector,
            "_execute_write",
            lambda **kwargs: {"success": False, "error": "disk on fire"},
        )
        result = inject_knowledge(cfg, "infra", "WSL proxy", "Proxy on 7890.")
        assert result["success"] is False
        assert _events(cfg) == []

    def test_raw_content_is_never_copied_into_the_log(self, tmp_path):
        cfg = _cfg(tmp_path)
        body = (
            f"The deploy key for the staging cluster is {FAKE_SECRET} and it is "
            "rotated every quarter."
        )
        inject_knowledge(cfg, "secrets-notes", "Staging deploy key", body)

        raw_log = _log_path(cfg).read_text(encoding="utf-8")
        assert FAKE_SECRET not in raw_log
        assert "deploy key for the staging cluster" not in raw_log

        event = _events(cfg)[0]
        # Only the structural facts, plus the hash that proves which body it was.
        assert event["section"] == "Staging deploy key"
        assert event["after_hash"]
        assert FAKE_SECRET not in json.dumps(event, ensure_ascii=False)

    def test_dry_run_compaction_records_nothing(self, tmp_path):
        cfg, memory = _bloated_memory(tmp_path)
        from layered_memory_mcp.memory_compactor import compact_memory

        result = compact_memory(cfg, memory_path=str(memory), dry_run=True)
        assert result["success"] is True and result["dry_run"] is True
        # The pass genuinely had something to migrate — it just did not.
        assert result["migrated_count"] == 1
        assert _events(cfg) == [], "a dry run writes nothing, so it records nothing"

    def test_real_compaction_records_its_l1_writes(self, tmp_path):
        """Compaction writes L1 through inject_knowledge, so it is covered."""
        cfg, memory = _bloated_memory(tmp_path)
        from layered_memory_mcp.memory_compactor import compact_memory

        result = compact_memory(cfg, memory_path=str(memory), dry_run=False)
        assert result["success"] is True and result["migrated_count"] == 1
        assert _ops(cfg) == ["l1_domain_created"]
        assert _events(cfg)[0]["tool"] == "inject_knowledge"


# --- D. whole-file tools -----------------------------------------------------


class TestWholeFileToolRecording:
    @pytest.fixture(autouse=True)
    def _server_config(self, tmp_path, monkeypatch):
        from layered_memory_mcp import server

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(server, "_config", cfg)
        self.cfg = cfg
        return cfg

    @pytest.mark.asyncio
    async def test_create_update_delete_each_record_once(self, tmp_path):
        from layered_memory_mcp import server

        await server.create_knowledge_file(
            "ops.md", "# ops\n\n## Boundary\n\nIngest and API are separate.\n"
        )
        assert _ops(self.cfg) == ["l1_domain_created"]

        await server.update_knowledge_file(
            "ops.md", "# ops\n\n## Boundary\n\nIngest, API and billing are separate.\n"
        )
        assert _ops(self.cfg) == ["l1_domain_created", "l1_domain_updated"]
        updated = _events(self.cfg)[-1]
        assert updated["before_hash"] and updated["after_hash"]
        assert updated["before_hash"] != updated["after_hash"]
        assert updated["tool"] == "update_knowledge_file"

        await server.delete_knowledge_file("ops.md")
        assert _ops(self.cfg)[-1] == "l1_domain_deleted"
        deleted = _events(self.cfg)[-1]
        assert deleted["before_hash"] and deleted["after_hash"] is None

    @pytest.mark.asyncio
    async def test_identical_overwrite_records_nothing(self, tmp_path):
        from layered_memory_mcp import server

        text = "# ops\n\n## Boundary\n\nIngest and API are separate.\n"
        await server.create_knowledge_file("ops.md", text)
        before = _events(self.cfg)

        await server.update_knowledge_file("ops.md", text)
        assert _events(self.cfg) == before, "the mtime moved, the meaning did not"

    @pytest.mark.asyncio
    async def test_a_refused_create_records_nothing(self, tmp_path):
        from layered_memory_mcp import server

        await server.create_knowledge_file("ops.md", "# ops\n")
        before = _events(self.cfg)

        payload = json.loads(
            await server.create_knowledge_file("ops.md", "# ops again\n")
        )
        assert payload["success"] is False
        assert _events(self.cfg) == before


# --- E. the L0 index ---------------------------------------------------------


class TestL0Recording:
    def test_sync_records_entry_changes_and_skips_a_noop_resync(self, tmp_path):
        cfg = _cfg_with_l0(tmp_path)
        (Path(cfg.knowledge_dir) / "infra.md").write_text(
            "# infra\n\n## Boundary\n\nIngest and API are separate.\n",
            encoding="utf-8",
        )

        first = sync_l0_index(cfg)
        assert first["success"] is True and first["entries_added"] == 1
        assert _ops(cfg) == ["l0_entry_added"]
        added = _events(cfg)[0]
        assert added["surface"] == "l0_index"
        assert added["domain"] == "infra"
        assert added["tool"] == "sync_l0_index"
        assert added["after_hash"]

        before = _events(cfg)
        resync = sync_l0_index(cfg)
        assert resync["entries_added"] == 0 and resync["entries_removed"] == 0
        assert _events(cfg) == before, "a rebuild is not a semantic change"

        (Path(cfg.knowledge_dir) / "infra.md").unlink()
        sync_l0_index(cfg)
        assert _ops(cfg) == ["l0_entry_added", "l0_entry_removed"]

    def test_dry_run_sync_records_nothing(self, tmp_path):
        cfg = _cfg_with_l0(tmp_path)
        (Path(cfg.knowledge_dir) / "infra.md").write_text(
            "# infra\n\n## Boundary\n\nIngest and API are separate.\n",
            encoding="utf-8",
        )
        report = sync_l0_index(cfg, dry_run=True)
        assert report["entries_added"] == 1
        assert _events(cfg) == []

    def test_unconfigured_l0_file_records_nothing(self, tmp_path):
        """The index was computed, but it was never persisted anywhere."""
        cfg = _cfg(tmp_path)
        assert cfg.l0_index_file is None
        (Path(cfg.knowledge_dir) / "infra.md").write_text(
            "# infra\n\n## Boundary\n\nIngest and API are separate.\n",
            encoding="utf-8",
        )
        sync_l0_index(cfg)
        assert _events(cfg) == []

    def test_manage_entry_records_add_rename_and_remove(self, tmp_path):
        cfg = _cfg_with_l0(tmp_path)

        assert manage_entry(cfg, "add", "infra", summary="proxy notes")["success"]
        assert _ops(cfg) == ["l0_entry_added"]

        assert manage_entry(cfg, "replace", "infra", summary="proxy + disk notes")[
            "success"
        ]
        assert _ops(cfg) == ["l0_entry_added", "l0_entry_renamed"]
        renamed = _events(cfg)[-1]
        assert renamed["before_hash"] and renamed["after_hash"]
        assert renamed["before_hash"] != renamed["after_hash"]

        assert manage_entry(cfg, "remove", "infra")["success"]
        assert _ops(cfg)[-1] == "l0_entry_removed"
        assert _events(cfg)[-1]["after_hash"] is None

    def test_identical_replace_and_failed_edits_record_nothing(self, tmp_path):
        cfg = _cfg_with_l0(tmp_path)
        manage_entry(cfg, "add", "infra", summary="proxy notes")
        before = _events(cfg)

        assert manage_entry(cfg, "replace", "infra", summary="proxy notes")["success"]
        assert _events(cfg) == before

        assert not manage_entry(cfg, "add", "infra", summary="dup")["success"]
        assert not manage_entry(cfg, "remove", "nosuchdomain")["success"]
        assert _events(cfg) == before


# --- F. the read tools -------------------------------------------------------


class TestReadTools:
    def _seed(self, cfg: MemoryConfig) -> None:
        for i in range(12):
            record(
                cfg,
                "l1_section_updated",
                "infra" if i % 2 else "ops",
                section=f"S{i}",
                tool="inject_knowledge",
            )
        record(cfg, "l0_entry_added", "infra", surface="l0_index")

    def test_read_is_bounded_and_newest_first(self, tmp_path):
        cfg = _cfg(tmp_path)
        self._seed(cfg)

        report = read_events(cfg, limit=3)
        assert report["returned"] == 3
        assert report["total_events"] == 13
        assert report["order"] == "newest_first"
        assert report["entries"][0]["operation"] == "l0_entry_added"

        assert read_events(cfg)["returned"] == 13  # default limit is 50
        # No caller can ask for the whole history.
        assert read_events(cfg, limit=10_000)["limit"] == MAX_READ_LIMIT

    def test_filters(self, tmp_path):
        cfg = _cfg(tmp_path)
        self._seed(cfg)

        by_domain = read_events(cfg, domain="ops")
        assert by_domain["matched"] == 6
        assert {e["domain"] for e in by_domain["entries"]} == {"ops"}

        assert read_events(cfg, surface="l0_index")["matched"] == 1
        assert read_events(cfg, operation="l1_section_updated")["matched"] == 12
        assert read_events(cfg, domain="nosuchdomain")["entries"] == []

    def test_since_filter(self, tmp_path):
        cfg = _cfg(tmp_path)
        record(cfg, "l1_section_created", "infra")
        assert read_events(cfg, since="2000-01-01")["matched"] == 1
        assert read_events(cfg, since="2999-01-01")["matched"] == 0

    def test_stats_summarise_without_dumping(self, tmp_path):
        cfg = _cfg(tmp_path)
        self._seed(cfg)

        stats = changelog_stats(cfg)
        assert stats["enabled"] is True
        assert stats["total_events"] == 13
        assert stats["by_operation"] == {"l0_entry_added": 1, "l1_section_updated": 12}
        assert stats["by_surface"] == {"l0_index": 1, "l1_knowledge": 12}
        assert stats["by_domain"]["infra"] == 7
        assert stats["first_event_at"] and stats["last_event_at"]
        # Retention is reported, never applied — nothing prunes this log.
        assert stats["retention_enforced"] is False
        assert "entries" not in stats

    def test_stats_on_an_empty_log(self, tmp_path):
        stats = changelog_stats(_cfg(tmp_path))
        assert stats["exists"] is False
        assert stats["total_events"] == 0

    @pytest.mark.asyncio
    async def test_the_mcp_tools_serialise_what_the_facades_return(
        self, tmp_path, monkeypatch
    ):
        from layered_memory_mcp import server

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(server, "_config", cfg)
        self._seed(cfg)

        payload = json.loads(await server.get_memory_changelog(domain="ops", limit=2))
        assert payload["returned"] == 2
        assert payload["matched"] == 6
        assert {e["domain"] for e in payload["entries"]} == {"ops"}

        stats = json.loads(await server.get_memory_changelog_stats())
        assert stats["total_events"] == 13
        assert "l1_section_updated" in stats["recordable_operations"]
        assert "vector_reindex" in stats["not_recorded"]


# --- G. the kill switch ------------------------------------------------------


class TestDisabledBackend:
    def test_disabled_records_nothing(self, tmp_path):
        cfg = _cfg(tmp_path, memory_changelog={"enabled": False})
        inject_knowledge(cfg, "infra", "WSL proxy", "Proxy on 127.0.0.1:7890.")
        assert record(cfg, "l1_section_created", "infra") is None
        assert not _log_path(cfg).exists()

    def test_disabled_read_tools_say_so_instead_of_looking_empty(self, tmp_path):
        cfg = _cfg(tmp_path, memory_changelog={"enabled": False})
        report = read_events(cfg)
        assert report["enabled"] is False
        assert report["entries"] == []
        assert "disabled" in report["note"]
        assert changelog_stats(cfg)["enabled"] is False

    def test_capabilities_report_the_backend_accurately(self, tmp_path):
        enabled = surface_capabilities(_cfg(tmp_path)).to_dict()["core_surfaces"][
            "memory_changelog"
        ]
        assert enabled["owned_by"] == "framework"
        assert enabled["can_apply"] is True
        assert enabled["application_mode"] == "apply"
        assert enabled["store_path"].endswith("data/memory_changelog.jsonl")
        assert "Append-only JSONL" in enabled["notes"]
        assert "no persistent store" not in enabled["notes"]

        off = surface_capabilities(
            _cfg(tmp_path, memory_changelog={"enabled": False})
        ).to_dict()["core_surfaces"]["memory_changelog"]
        assert off["owned_by"] == "framework"  # ownership never moves
        assert off["can_apply"] is False
        assert "DISABLED by config" in off["notes"]

    def test_env_var_kill_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_MEMORY_CHANGELOG_ENABLED", "false")
        cfg = _cfg(tmp_path)
        assert cfg.memory_changelog_enabled is False
        assert record(cfg, "l1_section_created", "infra") is None


# --- H. hashes ---------------------------------------------------------------


class TestHashing:
    def test_cosmetic_churn_does_not_read_as_a_change(self):
        assert hash_text("body\n") == hash_text("  body  ")
        assert hash_text("a\r\nb") == hash_text("a\nb")
        assert hash_text(None) == hash_text("")

    def test_the_fuse_lock_and_the_changelog_share_one_definition(self):
        assert _hash_body("some section body") == hash_text("some section body")

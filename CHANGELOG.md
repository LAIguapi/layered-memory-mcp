# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.4.0] - 2026-06-16

### Added — Rot Auditor (`audit_rot` tool)

A new **read-only** diagnostic tool, `audit_rot`, surfaces knowledge-base decay
before it accumulates. It detects the four common rot pathologies seen in
long-lived layered-memory stores:

- **oversized** — files grown past the recommended size (often from
  "append-but-never-merge" accumulation).
- **garbled_heading** — section headings that lost their punctuation/spaces
  (a run of characters with no separators), e.g. from an older summariser bug
  or hand-edited memory. CJK-, CamelCase-, and punctuation-aware so genuine
  headings aren't flagged.
- **stale** — sections carrying a transient marker (`下次执行`, `待测试`,
  `TODO`, `临时`, …) **together with** an expired date in the heading/lead.
  Requiring both keeps false positives low — a standing TODO list or a passing
  mention of "临时" is not flagged.
- **cross_file_duplicate** — near-duplicate sections living in different files,
  i.e. the same knowledge defined in more than one place.
- **same_file_duplicate** — near-duplicate sections within the *same* file: the
  classic "append but never merge" rot (often left behind by a dual-write that
  created two copies of one section).

Returns a health score (0–100), per-pathology findings, and consolidation
recommendations. Makes no changes — designed to be run periodically (e.g. a
weekly cron) so a human can decide what to consolidate.

### Fixed — Summariser corrupted snake_case identifiers

`_summarize_for_l0` stripped **all** underscores via a naive `[*_`#]` regex,
turning `enabled_toolsets` into `enabledtoolsets` and `fallback_providers` into
`fallbackproviders` in generated L0 pointers. The summariser now strips only
paired emphasis/code markers and leading heading hashes, preserving underscores
inside identifiers and file paths while still removing `_italic_` spans.

## [2.3.0] - 2026-06-16

### Added — Auto-Maintain (write-triggered self-maintenance)

The layered architecture introduced an **L1↔agent-memory dual-write**: every
`inject_knowledge` writes the knowledge to an L1 file *and* needs the resulting
L0 pointer mirrored into the agent's memory store. Previously the agent had to
do that second write manually (and remember to compact when memory filled up),
which was error-prone — agents forgot to sync pointers, or let memory overflow.

The framework now **owns the complexity it introduced**. After each write it
self-maintains, riding along on the natural `inject_knowledge` call (stdio-safe,
no background thread):

- **Dual-write completion** — automatically writes/updates the L0 pointer in
  agent memory (adds if missing, replaces a stale pointer to the same L1 file).
  The agent no longer needs to manually mirror pointers.
- **Lazy compaction** — when agent memory exceeds `compact_bloat_threshold`,
  **or** more than `auto_maintain_interval_days` (default 7) have elapsed since
  the last pass, runs `compact_memory()` to migrate bloat to L1 and slim memory
  back to pointers. Tracked via a `.last_auto_compact` marker in the home dir.

Maintenance fails silently — it never breaks the primary write.

### Configuration

- `LAYERED_MEMORY_AUTO_MAINTAIN`: enable/disable auto-maintain (default: `true`)
- `LAYERED_MEMORY_AUTO_MAINTAIN_INTERVAL_DAYS`: min days between auto-compaction
  passes (default: `7`)

When disabled, falls back to the legacy advisory `memory_bloat_warning`.

## [1.1.0] - 2026-05-08

### Changed — Agent-Agnostic Architecture

- **Removed** all Hermes-specific hardcodes from compact/detect pipeline
- **Added** auto-detection of agent memory file path (Hermes/Claude/Cursor/Cline/Generic)
- **Added** configurable entry separator (`LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR` env var)
- `detect_memory_bloat()` and `compact_memory()` now auto-detect agent memory via config
- `inject_knowledge` hint is now agent-agnostic English (was Chinese + Hermes-specific)
- `init_framework` returns unified rules (removed Hermes vs generic split)
- `_parse_entries()` accepts `separator` parameter (default `§` for backward compat)

### Configuration

- `LAYERED_MEMORY_AGENT_MEMORY_PATH`: explicit agent memory file path
- `LAYERED_MEMORY_AGENT_MEMORY_SEPARATOR`: entry separator (default: `§`)
- Auto-detect order: explicit → Hermes → Claude Code → Cursor → Cline → Generic

## [1.0.0] - 2026-05-08

### Added

- **4-tier knowledge architecture**: L0 (index pointers), L1 (knowledge files), L2 (skills), L3 (raw sessions)
- **Smart injection** (`inject_knowledge`): dedup, section targeting, auto L0 sync, L0 pointer generation
- **Auto-compact**: automatically triggers memory cleanup when usage >80%
- **Capacity warning**: alerts when memory >90% repeatedly, suggests expanding limits
- **Configurable domain rules**: load domain-to-keyword mappings from YAML config file
- **`compact_memory` MCP tool**: scan, classify, and migrate bloat entries to L1 files
- **`init_framework` MCP tool**: first-run detection, welcome file creation, management rules
- **`validate_knowledge` MCP tool**: L0-L1 consistency check, file health, cross-file duplicates
- **`manage_l0_entry` MCP tool**: fine-grained L0 index add/remove/replace
- **`get_l0_index` MCP tool**: agent-agnostic L0 index retrieval
- **MCP prompts**: `memory_rules`, `cognitive_decision`, `knowledge_compression`
- **Namespace support**: multi-agent isolation with per-namespace knowledge directories
- **Session scanning**: scan agent sessions for knowledge extraction candidates
- **Session keyword search**: find sessions containing specific keywords
- **Auto L0 sync**: index automatically synced after all write operations
- **Backup on update**: `.bak` files created before overwriting L1 knowledge files
- **Generic English fallback rules**: works out-of-the-box without configuration

### Configuration

- `LAYERED_MEMORY_HOME`: custom data directory (default `~/.layered-memory/`)
- `LAYERED_MEMORY_SESSIONS_DIR`: custom sessions directory
- `LAYERED_MEMORY_AUTO_SYNC_L0`: auto-sync after writes (default true)
- `LAYERED_MEMORY_NAMESPACE`: multi-agent isolation namespace
- `LAYERED_MEMORY_COMPACT_DOMAIN_RULES_FILE`: YAML file with domain rules
- `LAYERED_MEMORY_COMPACT_BLOAT_THRESHOLD`: auto-compact trigger (default 0.8)
- `LAYERED_MEMORY_COMPACT_CAPACITY_WARNING_THRESHOLD`: capacity warning (default 0.9)

### License

- MIT License

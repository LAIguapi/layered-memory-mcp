# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.10.0] - 2026-07-23

### Added — Promotion Detector (same-topic clustering → "this file should be split")

The existing dedup layers only judge whether a *single* piece of content is a
duplicate; they never notice that a whole topic has quietly accumulated into a
catch-all domain (default `misc`) until it deserves its own L1 file. This
release adds a Promotion Detector that closes that gap.

- **Semantic clustering on watched domains:** after a write into a watched
  catch-all domain, the detector parses the file's `##` sections, embeds each
  section body with the in-repo embedding pipeline (no new model), single-link
  clusters them by cosine similarity, and suggests extracting any cluster of
  `promotion_min_cluster_size` or more into its own domain.
- **Suggestion-only, never mutating:** the framework computes the objective fact
  ("these N sections are semantically one topic") and emits a *suggestion* with
  a rough suggested domain name. It never moves content — the agent decides,
  exactly like the dedup `suggestion` field.
- **Dual exit points:** the candidate surfaces both on the `inject` return value
  (`auto_maintain.promotion`) and via `audit_rot` findings
  (`promotion_candidates`), so it is visible both inline at write time and in
  periodic health audits.
- **`promotion_enabled` switch + tunables:** master on/off plus
  `promotion_watch_domains`, `promotion_min_sections`,
  `promotion_cluster_threshold`, and `promotion_min_cluster_size`.
- **Fault-isolated:** all detection is wrapped in try/except — any failure logs
  a warning and returns `None`, so it can never break the primary write.

## [2.9.2] - 2026-06-29

### Fixed — Runaway `[L0]` nesting loop (compaction ate its own index pointers)

An over-long L0 index pointer was misclassified as migratable "bloat",
creating a self-feeding loop that corrupted both agent memory and L1 files.

**Root cause:** `_is_index_entry()` required an L0 pointer to be **both**
`[L0]`-prefixed **and** shorter than `MAX_INDEX_ENTRY_LENGTH` (120 chars).
A pointer with a long summary failed the length test, so `compact_memory()`
treated it as bloat and `inject_knowledge`-ed it back into the L1 file body.
That write triggered `dual_write`, which regenerated an L0 pointer now
prefixed with the *previous* pointer text — `[L0] x: [L0] x: …`. Each
compaction cycle nested one more `[L0]` layer, unbounded. Observed in the
wild as dozens of `[L0] [L0] [L0] …` garbage sections inside L1 files after
high-frequency `inject_knowledge` calls.

**Fix:** length no longer disqualifies an index entry. `_is_index_entry()`
returns true for **any** `[L0]`-prefixed entry, so pointers are never routed
into L1. Over-length is now a separate, non-routing diagnostic exposed via
the new `is_oversized_index_entry()` (callers may suggest trimming the
summary, but must keep the pointer in memory). Regression tests assert an
oversized pointer is still an index entry and survives a real `compact_memory`
run without migration or `[L0]` nesting.

## [2.7.0] - 2026-06-21

### Fixed — Dynamic agent-memory limit detection (root-cause of silent bloat)

The framework's lazy compaction silently failed to fire when agent memory
actually overflowed, because it could not see the *real* capacity limit.

**Root cause:** `_get_memory_max_chars` defaulted to a hard-coded `50_000`
chars and only honored the `MEMORY_MAX_CHARS` env var. Hermes' real memory
limit lives in `config.yaml` (`memory.memory_char_limit`, default 2000,
user-adjustable). With no env var set, the framework thought capacity was
50000 while the true limit was 4000 — so a 3974-char (≈99% full) MEMORY.md
registered as ~8% usage and never tripped the compaction threshold.

**Fix:** the limit is now resolved through a priority chain that reads the
user's actual configuration instead of guessing:

1. explicit `config.memory_char_limit`
2. `MEMORY_MAX_CHARS` env var
3. **dynamic read of Hermes `config.yaml` `memory.{memory,user}_char_limit`**
   (tracks whatever the user set — 2000, 4000, 8000…)
4. smart default by memory-file type (Hermes-style `§` memory → 2000,
   generic → 50000)

New helpers in `memory_compactor.py`: `_find_hermes_config()`,
`_read_hermes_memory_limit(is_user_profile)`, `_is_hermes_memory_path()`,
`_is_user_profile_path()`. `_get_memory_max_chars()` now takes `config` and
`memory_path` so it can pick the right limit (MEMORY.md vs USER.md) and the
right fallback. `detect_memory_bloat` and `auto_maintain_after_write` pass
both through.

`HERMES_CONFIG_PATH` env var (set by Hermes in the MCP server's env) is the
preferred config locator; falls back to `~/.hermes/config.yaml`.

### Added — Trigger C: critical-usage safety net

`auto_maintain_after_write` gains a third compaction trigger: when usage
reaches `compact_critical_threshold` (default 0.95) **and** there is bloat to
migrate, compaction fires immediately, ignoring the `auto_maintain_interval`.
This catches the case where bloat was written straight to native memory
(bypassing `inject_knowledge`) and the 7-day interval hasn't elapsed.

### Added — Ride-along self-maintenance on `get_l0_index`

Under stdio (Hermes' mode), the MCP process is short-lived, so a background
daemon thread can't run periodic maintenance. Instead, `get_l0_index` — the
highest-frequency tool, called at the start of nearly every session — now
piggybacks a best-effort `auto_maintain_after_write` check. This gives the
framework a real chance to self-maintain even when the agent only ever writes
to native memory and never calls `inject_knowledge`. Failures are swallowed so
maintenance can never break index retrieval.

### Added — Config

- `compact_critical_threshold` (0–1, default 0.95, env
  `LAYERED_MEMORY_COMPACT_CRITICAL_THRESHOLD`), range-validated.
- `memory_char_limit` (explicit override, env `MEMORY_MAX_CHARS`).

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

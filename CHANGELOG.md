# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.4.3] - 2026-10-06

### Fixed

- **The session export filter under-reported: it checked excerpts, not content.**
  `exclude_markers` was matched against the *summarised* session (head+tail sample
  of at most 50 messages, user messages only when under 500 chars, assistant
  topics truncated to 200), so a marker buried in a long message was never seen —
  the very first live run against real sessions reported `excluded_sessions: 0`
  while the configured markers were plainly present in 13 and 14 of that
  session's messages. A privacy filter that under-reports is worse than none,
  because the operator trusts the count: the check now runs against the database
  itself (title plus a full-content `LIKE` per marker) before any summary is
  built, so what is filtered is decided by what the session actually contains.

## [3.4.2] - 2026-10-06

### Fixed

- **Session scanning read a directory that no longer holds sessions.** Hermes keeps
  live sessions in a SQLite database (`~/.hermes/state.db`); the JSON directory
  `sessions_dir` points at now only accumulates stale request dumps. A scan
  therefore reported **zero** sessions while the agent had been busy for weeks, and
  everything downstream (knowledge extraction, keyword search, an external
  compression job) degraded to "nothing new" — a failure that looks like success.
  `scan_sessions()` and `search_sessions_by_keyword()` now read the database first
  and keep the file directory as a fallback, so file-based setups are unaffected.

### Added

- `read_state_db_sessions()` / `search_state_db_sessions()` — read summaries and
  keyword hits straight from `state.db`, timestamp-windowed, opened **read-only**
  (never take a writable handle on a live agent's database).
- **Source filter**: `cron` sessions are skipped by default and counted in
  `stats.skipped_sources`. On a real host they are ~87% of the table (5763/6607
  rows; 12 of the last 3 days' 137 sessions were interactive), so leaving them in
  would crowd out exactly the sessions that carry durable knowledge.
- **Export filter**: `session_scan.exclude_markers` in `config.yaml` withholds any
  session whose title or body matches one of the given substrings, and reports the
  hit in `stats.excluded_by_marker` instead of silently dropping it. This is a
  privacy control: scan output is handed to a model that may run off-machine, so an
  operator needs a way to keep selected work out of that export. The shipped
  default is an **empty list** — the package makes no assumptions about anyone's
  work, and the markers live in the operator's own config.
- `session_scan.hermes_db_path` / `LAYERED_MEMORY_HERMES_DB` to point at a
  non-default database location.

## [3.4.1] - 2026-10-06

### Fixed

- **TODO terminal timestamps: `cancelled` had none, and a reopened row kept its
  finish time.** `todos` carried a single `completed_at` column, stamped only when
  `status` became `completed`. Two consequences, both found by auditing the live
  DB: (a) six `cancelled` rows had `completed_at` NULL and no cancel time at all,
  so “when was this abandoned?” could only be answered by `updated_at` — which any
  later edit (e.g. appending a note) silently refreshes; and (b) nothing cleared
  `completed_at` when a row moved back out of `completed`, so the first reopen
  would have produced a still-open task that claimed a finish time.

  `update()` now owns both stamps as a single status machine: `completed` stamps
  `completed_at` and clears `cancelled_at`; `cancelled` stamps the new
  `cancelled_at` column and clears `completed_at`; a move back to
  `pending`/`in_progress` clears both (the stale-timestamp half). Both fields were
  also removed from the caller-writable set, so they can only be derived from a
  status transition — a caller can no longer pass a fabricated finish time.

  Existing databases gain `cancelled_at` through the same idempotent ALTER
  migration already used for `title`/`blocked_by`; pre-existing rows keep `NULL`
  and are otherwise untouched.

## [3.4.0] - 2026-10-06

### Added

- **Periodic self-maintenance on long-lived servers** (`maintenance.py`). On HTTP
  transport the server now starts a daemon thread: every
  `maintenance_tick_seconds` (default 1800 / 30 min, first tick after a 60 s
  `maintenance_initial_delay`) it runs the same self-maintenance the write path
  used to ride along on — agent-memory compaction plus L1 line-level dedup — and
  refreshes a deployed write-guard that drifted behind the package. Until now
  that work only happened when somebody called the server, so an idle daemon
  never compacted and never healed the guard; the framework's own design notes
  called for closing this **inside** the framework rather than with an external
  cron job.

  The work it triggers is itself interval/threshold-gated (memory usage ≥
  `compact_bloat_threshold`, or `auto_maintain_interval_days` elapsed, or the
  critical threshold), so a normal tick is a cheap read that does nothing.

  Bounded on purpose:

  * `audit_rot` is **not** in the loop — a full rot audit is an O(n²) scan
    (seconds of CPU on a real store) and the weekly health-watchdog cron already
    runs it.
  * stdio servers do not start it — they are per-session and short-lived, and
    stdout carries the JSON-RPC stream, so a background thread must not write
    there. The ride-along still covers them.
  * A failing job is logged and retried next tick; it never kills the loop, and a
    failing compaction never blocks the guard refresh (independent jobs).

### Config

- `maintenance_enabled` (default **true** — the kill switch),
  `maintenance_tick_seconds` (1800; anything under a 30 s floor is raised to it,
  so a seconds/milliseconds mix-up cannot become a busy loop),
  `maintenance_initial_delay` (60); env overrides
  `LAYERED_MEMORY_MAINTENANCE_{ENABLED,TICK,DELAY}`.

## [3.3.8] - 2026-10-06

### Fixed

- **The v3.3.7 gate could deadlock a single-member family.** The anti-laziness
  ceiling was compared against the *existing* member's bytes, but the merged body
  necessarily carries that member's facts **plus** the incoming note — so for a
  family of one, the write-back was refused while the write itself stayed
  blocked: the caller had no legal move. The ceiling now applies from two
  existing members upward, which is where "a re-statement dressed as
  consolidation" is the real risk; a single member only has to come back as one
  section.

  Found by running the handshake against the live service minutes after 3.3.7 was
  installed — a 22-character member cannot host a merged body in under 0.9 × 22
  characters. No live data was affected: the library has no same-skeleton families
  (`same_file_duplicate: 0`), so the gate had nothing to block, and the flaw was
  reachable only through the probe. Regression test added.

## [3.3.7] - 2026-10-06

### Added

- **Same-skeleton family gate on the write path** — the write side of the
  v3.3.2 read-side detector, and the answer to "why does this store keep growing
  periodic families?". Two headings that agree after dropping dates, issue
  numbers, versions and parentheticals are one topic; when a write would add
  another member to such a family, **nothing is written**: the response is
  `action="deferred_consolidate"` carrying the whole family, an optimistic-lock
  `expected_hash`, and the instruction to rewrite it as ONE section (current
  understanding + one provenance line). Re-submitting that text with `fuse=True`
  and the hash collapses the family in place (`action="consolidated"`,
  `family_size_before`, `sections_removed`).
- **Anti-laziness check**: a write-back that is not smaller than the family it
  replaces (default ceiling: 0.9 of the family's bytes) is refused with
  `action="consolidate_refused"`, leaving the family untouched — re-stating the
  old sections in a new shape is not consolidation.
- A refused deferral is logged (`deferred_consolidate: …`), so an unattended
  caller that ignores the action is visible instead of looking like a no-op.

### Changed

- **`mode="append"` is retired.** It now behaves as `upsert`, and the response
  carries `mode_deprecated` + `mode_note` so a stale caller can be found and
  fixed. Append-without-merge is what produced the families in the first place;
  callers keep working (no errors), they just stop being able to grow one.
- Heading normalisation is shared with the read side (`heading.py`), so the
  auditor's notion of a family and the writer's are the same function.

### Config

- `consolidate_enabled` (default **true** — this is the kill switch),
  `consolidate_min_family` (default 2), `consolidate_size_ceiling` (default 0.9),
  each with a `LAYERED_MEMORY_CONSOLIDATE_*` env override.

## [3.3.6] - 2026-10-06

### Fixed

- **The suite is fully green for the first time.** `test_search_sessions_by_keyword_json`
  called `asyncio.get_event_loop().run_until_complete(...)`; once an earlier test
  had closed the ambient loop, `get_event_loop()` raised ``RuntimeError: There is
  no current event loop in thread 'MainThread'`` — which is why the test passed
  when run alone and failed in the full suite. It now uses `asyncio.run()`.
  Result: 383 passed, 0 failed (previously 1 order-dependent failure).
- **Test isolation: the server config singleton can no longer leak between
  tests.** `layered_memory_mcp.server._config` is a lazily-built module-level
  singleton that this suite has long assigned to directly and reset by hand; a
  test raising before its own cleanup left its tmp config behind for every later
  test. An autouse fixture now snapshots and restores it, the same way the
  production-path guard already covers files.

### Changed

- **Heading normalisation has one home** (`heading.py`): the read side
  (`audit_rot`) and the write side both need to agree on what "the same topic,
  logged again" means — a read-side detector that flags a family the writer keeps
  appending to is only half a mechanism. `rot_auditor._heading_skeleton` and
  `rot_auditor._HEADING_NOISE_RE` remain as aliases of the shared objects, so
  existing imports keep working and no behaviour changes.

## [3.3.5] - 2026-10-06

### Fixed

- **A deployed write guard no longer drifts behind the framework.** The guard
  on the host is a copy of the payload bundled in this package, so every release
  left it one version behind — and the ``auto`` policy could not heal it:
  ``ensure_guard_installed`` routed a version drift into ``install_guard``,
  which refuses to overwrite without ``force=True``. The result was sticky:
  ``check_guard_status`` reported ``update_available`` forever. Measured on a
  real host: deployed 3.3.0 against a 3.3.4 package, ``guard.py`` byte-identical
  and only ``plugin.yaml`` differing, so nothing but bookkeeping was wrong —
  which is exactly the kind of silent drift that hides a real one later.

### Added

- ``refresh_deployed_plugin(...)`` — files-only re-copy of the payload when the
  deployed version differs. It writes no Hermes config, invokes no CLI, and
  enables/disables nothing (that is ``install_guard``'s job, on the explicit
  path). It acts only when the guard is already installed **and** the policy is
  ``auto``, and it never raises.
- ``get_l0_index`` now carries a guard-refresh ride-along (auto policy only)
  next to the existing auto-maintain ride-along, so one framework upgrade plus
  one session start is enough to bring the host in step — no manual re-install.
- ``ensure_guard_installed`` handles ``update_available`` by refreshing instead
  of conflicting, so the ``auto`` policy can finally heal what it detects.

### Notes

- Refreshing the files does not reload a running host: a *logic* change still
  needs the host to restart, while a version bump alone does not.
- Test isolation hardened with this change: the production-path guard now also
  blocks writes to ``~/.hermes/plugins`` and ``~/.hermes/config.yaml`` (the two
  places a guard deploy/enable can reach), and resolves the real home at import
  time so fixture ordering cannot defeat it.

## [3.3.4] - 2026-10-06

### Fixed

- **Stale detection: a discharged promise is history, not rot.** A line such as
  `… TODO dd1eb34c 已完成（2026-07-26）` carries both a pending marker and a past
  date, so v3.3.3 still flagged it — the last false-positive class in the live
  library. Lines recording a completion (`已完成`, `已解决`, `已闭环`, `已生效`,
  …) are now skipped. `尚未完成` is deliberately unaffected: it contains 完成 but
  not `已完成`, so a genuinely open item is still reported.
- **Promotion hint no longer suggests a domain named after a year.** The
  suggested name came back as `2026.md`: the heading tokenizer splits
  `2026-07-30` into `2026` / `07` / `30`, and a year repeated across dated
  headings won the frequency vote outright (it also won strategy 1 whenever
  every heading started with it). Pure-digit tokens are now excluded from both
  naming strategies, so the hint falls through to a real topic word — or to
  `topic` when nothing repeats, which is the honest answer.

## [3.3.3] - 2026-10-06

### Fixed

- **Stale detection no longer convicts retrospective notes.** The auditor
  flagged a section when a marker appeared anywhere in the heading or the
  leading two lines together with any past date in that same text — and the
  marker list contained the scope qualifiers `临时` / `暂时`. Every dated lesson
  (`（2026-09-11 实测）`, `（2026-07-16，含实证）`) therefore tripped it: six of
  six reported "stale" sections in the live library were false positives, and
  they cost 18 points of a 56-point score.
  A section can only be *overdue* if it carries a promise, so the marker list is
  now forward-looking only (`下次执行`, `待…`, `尚未…`, `未落地`, `TODO`, …) and
  the date must sit **on the same line** as the marker — otherwise an unrelated
  observation date nearby would still convict it. The surviving shape is
  `下次执行 2026-08-01`.

## [3.3.2] - 2026-10-06

### Fixed

- **`audit_rot` no longer times out on a large store.** The same-file duplicate
  scan was an O(n²) loop calling `SequenceMatcher.ratio()` on every pair of
  section bodies; on a 791-section library (~312k pairs) the MCP call hit the
  60s client timeout every time, leaving the health score unobtainable. The
  comparison is now ordered cheapest-first: an exact length-ratio bound
  (`ratio() <= 2*min/(la+lb)`, so it can never discard a real match), then the
  O(1)/O(n) `real_quick_ratio` / `quick_ratio` upper bounds, and only then
  `ratio()`.
- **`audit_rot` now finds re-appended shorter copies of a section.** Body
  similarity alone misses a second copy that is much shorter than the original
  (it falls below the threshold). Same-file section pairs whose headings agree
  after stripping dates, versions and parenthetical qualifiers are now reported
  too, with `reason: "same heading skeleton"`. Restricted to same-file pairs —
  across files an identical generic heading is usually legitimate.
- **One malformed L1 file no longer breaks L0 index generation.**
  `l0_manager._generate_hermes_index` called `gd.get("keywords", "").strip()` on
  an optional regex group; a line with no `→ keywords` part yields `None` (the
  default applies only to a *missing* key), so a single stray knowledge file
  crashed index generation for `inject_knowledge` / `create_knowledge_file` /
  `update_knowledge_file` / `sync_l0_index` alike — while the L1 write had
  already landed, making it look like nothing was written.

## [3.3.1] - 2026-10-06

### Fixed

- **Declare `PyYAML` as a real dependency.** `config.py` imports `yaml` at
  module level, so the package cannot be imported without it — but it was only
  arriving transitively (fastembed → huggingface_hub). A clean environment
  installing with `--no-deps` (or any future dependency reshuffle) would hit
  `ModuleNotFoundError: No module named 'yaml'`. Comment in `pyproject.toml`
  records why the pin exists.

## [3.3.0] - 2026-10-06

### Added

- **MEMORY.md write guard** (`write_guard/`), shipped as a deployable Hermes
  `pre_tool_call` plugin. Blocks the native `memory` tool with `target="memory"`
  (and an omitted target, which defaults to MEMORY.md) plus direct
  `write_file`/`patch` edits of `memories/MEMORY.md`, and points the model at
  `inject_knowledge` (L1) or `target="user"` instead. `USER.md` and unrelated
  paths stay writable. The policy accepts both `args` (what Hermes passes) and
  `tool_input` (a spelling that silently kills hooks reading it).
- `write_guard.check_guard_status` / `install_guard` / `remove_guard` /
  `ensure_guard_installed`, mirroring `dashboard_plugin`. Installation enables
  the plugin through `hermes plugins enable`, silences `memory.nudge_interval`
  (the periodic review fork that drove the writes) and records the previous
  value for `remove_guard` to restore. It never hand-edits the host config:
  without a resolvable `hermes` CLI it returns the exact commands instead.
- `init_framework()` now reports guard status; `integrate_agent()` gains
  `install_guard` / `guard_status` / `remove_guard`.
- New `write_guard` config policy: `auto` / `manual` (default) / `off`.
- Installation runs a seven-case self-test against the *deployed* payload,
  negative controls included, so a guard that blocks everything cannot look
  healthy.

### Changed

- **Test suite now runs against the working tree** (`pythonpath = ["src"]`).
  Before this, pytest imported whatever copy happened to be installed in the
  active interpreter, so it silently tested a stale snapshot and could not see
  newly added modules at all.
- Version consistency is locked by a test: `pyproject.toml` == `__init__.py`
  fallback == `plugin.yaml` == `write_guard.GUARD_VERSION`.

### Notes

- YAML 1.1 parses a bare `off` as the boolean `False`, so the `write_guard`
  config loader coerces booleans; without that, the most natural spelling of
  "turn it off" raised at start-up.

## [3.2.2] - 2026-10-01

### Fixed

- **Test suite no longer writes to production memory.** `detect_agent_memory_path()`
  resolved `~/.hermes/memories/MEMORY.md` through `Path.home()`, ignoring
  `LAYERED_MEMORY_HOME`, so tests exercising the dual-write path appended junk
  `[L0]` pointers to a live MEMORY.md. `tests/conftest.py` now redirects every
  resolution route (L1 home, agent memory path, HOME/USERPROFILE) and wraps
  `open` to turn any write into the real memory dirs into a loud failure.

## [3.2.1] - 2026-09-30

### Fixed

- **Compaction no longer mangles section headings.** Heading text was being
  trimmed mid-token (lost punctuation/characters) when written back.

## [3.2.0] - 2026-08-12

### Added

- **`memory_mode`** — how the dual-write treats the agent's MEMORY.md:
  `pointers` (legacy: one `[L0]` pointer per domain per write), `index_only`
  (exactly one `knowledge-index` entry; per-domain pointer copies are reaped,
  not migrated), `off` (no dual-write). Selectable via `LAYERED_MEMORY_MEMORY_MODE`.
  `auto_maintain_after_write` dispatches through the mode.

## [3.1.0] - 2026-07-31

### Added

- **Content-only semantic upsert**: write-path dedup compares section bodies
  semantically (bge-small-zh cosine) instead of char-level similarity, and a
  semantic near-duplicate returns a `deferred_fusion` handshake instead of
  blindly appending — the caller fuses the two bodies and commits with an
  optimistic-lock token.
- **Active reconcile tools** (`reconcile_knowledge_tool`, `audit_rot_tool`):
  pull-based housekeeping that reports decay worklists without mutating
  knowledge.

## [2.11.0] - 2026-07-23

### BREAKING — Domain classification unified to a single config source

- **Compaction no longer ships a built-in `_FALLBACK_DOMAIN_RULES` preset
  table, and no longer reads `compact_domain_rules_file`.** Domain
  classification is now sourced exclusively from `config.domain_keywords`, the
  single data source shared by both the auto-extractor and compaction. With no
  configured table the framework makes no assumption — extracted entries stay
  `general` and compaction offers no migration suggestion.

### Added

- **`MemoryConfig` now reads `domain_keywords` from `<home>/config.yaml`.**
  Previously the config object never read its own `config.yaml` for this field,
  so a migrated file was silently ignored. Resolution priority is now
  constructor argument > `LAYERED_MEMORY_DOMAIN_KEYWORDS` env var >
  `config.yaml` > empty table. A malformed `config.yaml` degrades to an empty
  table instead of crashing start-up.
- **New `layered-memory-migrate` CLI** (`--config`, `--dry-run`, `--force`).
  Writes a classic technical-domain default table (`infra`/`dev`/`docs`) into
  the user's `config.yaml` in one shot. Idempotent — it never overwrites an
  existing `domain_keywords` section unless `--force` is passed, and preserves
  the rest of the file. Users upgrading from a version with built-in presets
  run this once to restore automatic classification.

### Changed

- `memory_compactor` gains a pure `_dict_to_rules` helper as the single reshape
  point from the `domain_keywords` dict to the internal rule tuples.

## [2.10.1] - 2026-07-23

### Changed — Auto-extractor domain classification is now fully user-configurable

- **Removed hardcoded domain keyword presets from the auto-extractor.** The
  knowledge extractor previously shipped a built-in keyword table that guessed
  a domain for every extracted entry. A general-purpose framework should make
  no assumption about the user's subject matter, so those presets are gone.
- **Zero presets by default.** `_infer_domain` now performs no keyword matching
  unless a `domain_keywords` mapping is supplied; with an empty table (the
  default) every entry falls back to `general`.
- **New `domain_keywords` config field.** Users who want automatic
  classification can supply their own `{domain: [keyword, ...]}` table via the
  `MemoryConfig(domain_keywords=...)` constructor argument or the
  `LAYERED_MEMORY_DOMAIN_KEYWORDS` env var (JSON object). Defaults to an empty
  dict — fully backward compatible.
- **Docs.** `config/config.example.yaml` documents the opt-in `domain_keywords`
  mapping with neutral technical examples.

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

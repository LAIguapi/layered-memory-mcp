# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A surface model, and a deterministic router that uses it.** The framework
  now states what it owns instead of absorbing everything into L1. The
  framework-owned **common core** is `L0_INDEX`, `L1_KNOWLEDGE`, `TODO` and
  `MEMORY_CHANGELOG`. Skills, the user profile and other agent-owned content
  are **agent-owned extension surfaces**: the framework classifies them and
  returns a structured recommendation, and writes nothing. Raw session data is
  source-owned, read-only evidence; the L0 index, vector store and search index
  are derived data.
- **`classify_memory_item` (read-only MCP tool).** Routes one candidate item to
  a surface using marker **and** structure rules — no model call, no embedding.
  The result is a `RoutingDecision` (`schema_version`, `surface`, `ownership`,
  `score`, `confidence_basis`, `reason`, `signals`, `alternatives`, `warnings`,
  `mode`, `suggested_action`, plus the capability fields). `score` is a
  rule-weight sum and is deliberately not called a confidence: nothing here
  claims semantic certainty. An optional `agent_type` is echoed for statistics
  and has no effect on the decision; `supported_surfaces` suppresses an action
  the caller could not honour.
- **`get_memory_surface_capabilities` (read-only MCP tool).** Reports, under
  the current config, which surfaces the framework may write and which it can
  only suggest.
- **Opt-in agent extensions.** A new `agent_extensions:` config section
  (default: disabled, `suggest`, no adapter, no per-surface handler, no path)
  lets an operator have the framework apply extension actions on their behalf.
  All five conditions — master switch, per-surface switch, a `handler` naming a
  **registered** adapter, a mode other than `suggest`, and a handler that can
  resolve a safe target — must hold, and each falls back independently to
  suggestion-only when missing or malformed. Opting in does **not** move
  ownership: those surfaces stay `owned_by: agent` with
  `default_behavior: suggest`, and only `can_apply` changes.
- **`apply_agent_action` (write-capable MCP tool, off by default).** Turns an
  `AgentAction` recommendation into a real write — but only as an explicitly
  configured orchestrator. Out of the box it returns `status: "disabled"` and
  writes nothing. The gates, each reported rather than silent: the action must
  validate (`kind`, `surface`, `operation`, non-empty `source_content` — an
  invalid action is refused, never repaired); the surface must be an extension
  surface; both `enabled` switches must be true; `mode` must not be `suggest`;
  the configured `handler` must be registered and support the surface; the
  handler must resolve a safe target; `apply_with_confirmation` (and any action
  whose own `requires_user_confirmation` is set) needs `confirmed=True`; and
  `dry_run=True` reports `would_apply` and returns before writing. The result
  is `{applied, status, surface, operation, handler, adapter, mode, dry_run,
  confirmed, target, blockers, detail, error, metadata}` with statuses
  `applied` / `would_apply` / `confirmation_required` / `disabled` /
  `unsupported` / `refused` / `invalid_action` / `error`, and `blockers`
  carrying stable reason codes. Applying an agent-owned action records **no**
  `MEMORY_CHANGELOG` event: that log is L0/L1 provenance only.
- **`generic_file` reference handler (`adapters/generic_file.py`).** The one
  adapter that ships registered — and registration is not permission. It
  appends a single bounded Markdown `##` section per action (the caller's own
  `source_content` under a short metadata block, opened by a
  `<!-- layered-memory:agent-action -->` marker) to a file the operator named
  in `agent_extensions.surfaces.<surface>.path`, or `<root>/<surface>.md` under
  `agent_extensions.root`. `create` / `append` / `update` all append; `update`
  records what it `supersedes` instead of attempting a fuzzy in-place edit,
  because rewriting a file the framework does not own is the one behaviour that
  could destroy content. Path safety is refusal, not repair:
  `no_configured_path` when nothing explicit is set (there is no default
  location, on purpose), `path_escapes_base` for a relative path leaving its
  root/home, `unsafe_system_target` for `/etc`, `/usr`, `/sys`, `C:\Windows`
  and friends, and `unsafe_file_type` for code / config / database / dotfile
  targets that appended Markdown would corrupt. Parent directories are created
  only for a configured path. No command adapter and no shell execution exist
  on this path, and writing a Hermes-native skill or profile file remains out
  of scope.
- **`routing:` config section.** `enabled` (default true), `mode` (default
  `advisory`) and `enforce_direct_writes` (default false), plus marker
  extensions in the same style as `governance:` (`extra_skill_markers`,
  `extra_todo_markers`, `extra_profile_markers`, `extra_knowledge_markers`,
  `disabled_markers`). Also readable from `LAYERED_MEMORY_ROUTING_ENABLED` /
  `LAYERED_MEMORY_ROUTING_MODE` /
  `LAYERED_MEMORY_ROUTING_ENFORCE_DIRECT_WRITES` /
  `LAYERED_MEMORY_AGENT_EXTENSIONS_ENABLED`.
- **Strict direct writes — `routing.enforce_direct_writes` (opt-in, default
  false).** The `inject_knowledge` enforce question is decided, as two
  independent switches rather than one. `routing.mode` keeps gating
  `compact_memory` only, where the *framework* picks the target, and still never
  refuses an explicit write. `enforce_direct_writes` is the only switch that
  can: with it on, a direct write whose content classifies as anything but
  `l1_knowledge` / `l0_index` is refused **before** the write — before dedup,
  before the vector store, before anything touches disk — and returns
  `{success: false, action: "routing_refused", error:
  "routing_enforce_direct_write_refused", message, written: false, routing,
  routing_warning, suggested_action, routing_enforcement, hint}`, plus a
  `todo_candidate` for TODO-shaped content. A refusal writes nothing at all: no
  file, no section, no `MEMORY_CHANGELOG` event (a refusal is not a mutation)
  and no agent-owned write — `suggested_action` is handed back for the caller to
  apply, as on the compaction path. A new `routing_override` argument
  (`"none"` / `"force_l1"`) on `inject_knowledge`, `inject_knowledge_tool` and
  `batch_inject_knowledge_tool` writes anyway and reports
  `routing_override_applied: true`; an unknown value is ignored rather than read
  as permission. `routing.enabled: false` makes the flag inert, a classifier
  failure fails open (a write must not be lost because the thing that judges
  writes broke), and compaction is unaffected — it is gated by `routing.mode`,
  so one config key never silently re-decides two behaviours.
  `get_memory_surface_capabilities` reports both switches
  (`routing_mode`, `routing_enforce_direct_writes`) and the `l1_knowledge` note
  says which one is live. **Default behaviour is unchanged**: an explicit write
  still lands in L1 with a `routing_warning`, and a default install's result
  shape gains no new keys.
- **Adapter interface (`layered_memory_mcp.adapters`).** The protocol, registry
  and `apply_agent_action` entry point for applying extension actions, plus an
  optional `readiness(surface, config)` hook an adapter uses to say whether it
  could write right now. An adapter that declares no `dry_run` parameter is
  never called under `dry_run=True` — the framework refuses rather than risk a
  write it promised not to make. This is the only place product-specific paths
  may live; the routing core knows none.
- **`MEMORY_CHANGELOG`, with a persistent backend
  (`layered_memory_mcp.memory_changelog`).** The provenance record for semantic
  L0/L1 mutations, with explicit inclusion and exclusion rules
  (`RECORDABLE_OPERATIONS` / `NOT_RECORDED`). It is not the repository
  `CHANGELOG.md`, not session logs, not a generic event log, and not TODO
  lifecycle history.
  - **Store.** Append-only JSONL at `<home>/data/memory_changelog.jsonl`
    (`storage.MemoryChangelogStore`), one event per line. Recording is
    best-effort and swallows its own failures: provenance must never be able to
    fail the write it describes. Nothing rewrites or deletes a line —
    `retention_days` is read and reported but **not enforced**, because pruning
    provenance is destructive and silently dropping history is worse than not
    pruning.
  - **Event (`schema_version: 2`).** `event_id`, `at` (UTC ISO), `operation`,
    `surface`, `domain`, `section`, `actor` / `agent_id`, `tool`, `summary`,
    `reason`, `source_ref`, `before_hash` / `after_hash`, `metadata`. Structure
    only: the body text is never copied, and `metadata` keeps scalars and short
    strings while dropping nested containers — the one way content leaks into a
    provenance log is a caller handing over a whole result dict. The hash
    normalisation is shared with the deferred-fusion optimistic lock, so a
    fusion write-back's `before_hash` *is* the `expected_hash` the handshake was
    issued under.
  - **Recorded paths.** `inject_knowledge` (hence `compact_memory`'s L1 writes
    and `batch_inject_knowledge_tool`) for create / section-create / update /
    merge / replace / fusion write-back / consolidation write-back;
    `create_knowledge_file` / `update_knowledge_file` / `delete_knowledge_file`
    for whole-file mutations; `sync_l0_index` and `manage_l0_entry_tool` for L0
    entries that actually moved.
  - **Not recorded.** A deferral handshake, a `fuse_conflict` or
    `consolidate_refused` refusal, a skipped duplicate, a validation failure, a
    failed write, `dry_run=True`, an L0 resync whose entry set did not change,
    and any write whose content hash came out identical. Derived-data rebuilds
    (vector reindex, search index) and TODO lifecycle stay out by design.
- **`get_memory_changelog` / `get_memory_changelog_stats` (read-only MCP
  tools).** Filtered newest-first reads (domain / operation / surface / since,
  default 50 entries, hard ceiling 500) and a summary of counts by operation,
  surface and domain plus the time span. A corrupt line is counted and skipped,
  not fatal.
- **`memory_changelog:` config section.** `enabled` (default true), `path`
  (default `<home>/data/memory_changelog.jsonl`) and `retention_days` (default
  none, reported not enforced). Also readable from
  `LAYERED_MEMORY_CHANGELOG_ENABLED` / `LAYERED_MEMORY_CHANGELOG_PATH`.
- **Procedure-contamination governance check.** `post_write_governance` now
  reports L1 sections that read as action protocols rather than durable
  knowledge. A section is flagged only when action markers *and* procedural
  structure both match, so an architecture note that merely mentions "验证" or
  "流程" is left alone. Advisory; nothing is moved.

- **A write guard on the agent's own memory file
  (`layered_memory_mcp.agent_memory_guard`).** The framework projects an L0
  pointer / knowledge-index entry into the agent's native memory file
  (`MEMORY.md`, `CLAUDE.md`, …), and when no path is configured it *auto-detects*
  one from absolute user-level locations. Auto-detection is fine for reading and
  reporting; as a silent write target it meant any ambiguous config resolved a
  live file. A throwaway script on a bare `MemoryConfig(home=<tmpdir>)` called
  `inject_knowledge`, the ride-along maintenance auto-detected
  `~/.hermes/memories/MEMORY.md`, and a stray `[L0] misc: …` pointer landed in a
  real agent's memory. A projection write now requires an **explicit target** or
  an **explicit opt-in**: a caller-supplied `memory_path`, `agent_memory.path`,
  `agent_memory.allow_auto_detect_write: true`, a detected file inside `<home>`,
  or a `<home>` that is this install's real configured home and not a temp/test
  sandbox (which is what keeps an existing deployment's projection working).
  Anything else is refused and reported — never silently dropped.
- **`agent_memory:` config section.** `adapter` (default `auto`), `path`
  (explicit target, recommended) and `allow_auto_detect_write` (default
  `false`), also readable from `LAYERED_MEMORY_AGENT_ADAPTER`,
  `LAYERED_MEMORY_AGENT_MEMORY_PATH` and
  `LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE`. The pre-existing env
  vars keep their priority; `separator` may now also be set in the section.
- **Agent-environment adapters (`layered_memory_mcp.adapters.environment`).**
  The second half of the adapter layer, and the one that makes the common core
  agent-neutral. An `AgentEnvironmentAdapter` answers *where* the host agent
  keeps its own memory file, which character separates its entries, where its
  live-session database and session dumps live, how many characters it allows,
  and which persona file to inject into. Read-only throughout: resolving a path
  through an adapter grants no permission to write it.
  - **Registry, probed in priority order.** Unlike the extension-action
    registry, this one ships populated: `hermes` (10), `claude_code` (20),
    `cursor` (30), `cline` (40), `generic` (90) — the same candidate order the
    core used to hardcode. `EnvironmentAdapterBase` makes a declarative
    integration (layout + separator + tokens) code-free.
  - **`adapters.hermes`** owns every Hermes fact: the `~/.hermes` layout, `§`,
    the 2000-char fallback, the dynamic read of
    `memory.{memory,user}_char_limit` from `~/.hermes/config.yaml`, `SOUL.md`,
    `SOUL_BLOCK`, and a pointer to the `write_guard` support package.
    **Hermes remains the default adapter for backwards compatibility.**
  - **`agent_memory.adapter`** selects one. `auto` (the default) reproduces the
    pre-adapter probe chain exactly; an explicit id pins one adapter;
    `generic` / `none` / `off` knows no product paths at all and resolves only
    the framework-owned `<home>/agent-memory.md`; an unknown id falls back to
    `generic` **with a warning**, never silently to Hermes.
  - **`get_memory_surface_capabilities`** gained `agent_environment`
    (`configured`, `resolved`, `known`, `entry_separator`, and the adapter's
    capability flags). Distinct from `adapter`, which names the optional
    extension-*action* handler bundle.
  - **`MemoryConfig.agent_state_db_path`** — product-neutral alias for
    `hermes_db_path`, whose Hermes-era name is kept so no install breaks.
- **Blocked-set backoff for lazy compaction under `enforce`.** A blocked entry
  is deliberately left in agent memory, so the usage-ratio trigger in
  `auto_maintain_after_write` stayed satisfied forever and every write re-ran a
  full pass (backup, classification, rewrite) that migrated nothing.
  `compact_memory` now reports `blocked_set: {count, fingerprint, by_surface}` —
  a sorted hash over each blocked entry's whitespace-normalised content plus its
  routed surface and status, with no timestamps, so two passes over unchanged
  content agree and ordering cannot change it. The lazy path remembers the last
  one in `<home>/data/auto_compaction_state.json`
  (`storage/auto_compaction_state.py`: `schema_version`, keyed by a hash of the
  memory file path, hashes and counts only — no memory text and no absolute
  path, written through an atomic replace) and skips a pass that could only
  repeat itself, reporting `compact.status: "backed_off"` plus a top-level
  `auto_compaction_backoff` (`status: "blocked_set_unchanged"`, `probe`,
  `blocked_set`, `migratable_l1_candidates`, `first_seen_at`,
  `last_attempt_at`, `skipped_passes`, `hint`). The decision is cheap: first a
  byte-identity check against the file the recorded pass left behind, otherwise
  a dry-run classification — never a full pass to learn nothing changed. New
  bloat changes the fingerprint and triggers a pass on the next write; a new
  L1-migratable entry (or one refused for want of a heading) counts as pending
  work and runs regardless; placing the blocked entries clears the record; and
  `advisory` mode neither reads nor writes any of it. This is derived
  operational state — not L1 knowledge, and **not** a `MEMORY_CHANGELOG` event.
- **`routing.classifier_inputs_hash(config)`** — a digest over every input that
  can move a classification, so a cached classification outcome can be
  invalidated when the classifier changes rather than only when the content
  does. Covers the marker vocabulary *as compiled for this config* (so
  `routing.extra_*_markers` and `routing.disabled_markers` are folded in, and a
  rule set added later is covered without anyone remembering to), the routing
  switches, the scoring thresholds and tie-break order, `domain_keywords`, and
  `l0_tag` — which decides which entries reach the classifier at all.
  Normalised before hashing (lists are sets, dict order is irrelevant), with no
  timestamp, no absolute path and no memory content in it, so two installs with
  the same rules agree and a cosmetic config edit does not invalidate anything.
  `routing.classifier_inputs(config)` returns the same payload unhashed, for
  reports and tests. A malformed section degrades to the shipped defaults; only
  an unexpected failure returns `None`, which every caller must read as
  *invalidate*, never as *unchanged*.
- **Three auto-maintenance cadence markers under `<home>`**, where there was
  one. `.last_auto_compact` keeps its name and its meaning — a compaction pass
  ran — so an existing install's cadence survives the upgrade.
  `.last_auto_compact_attempt` records that the lazy path *reached a decision*,
  a backoff included; it throttles the interval trigger only, and only while
  agent memory has not changed since
  (`auto_maintain_attempt_interval_hours`, default 1,
  `LAYERED_MEMORY_AUTO_MAINTAIN_ATTEMPT_INTERVAL_HOURS`; `0` restores the
  previous "decide on every write" behaviour, and the value is clamped to at
  most one `auto_maintain_interval_days`). `.last_l1_dedup` gives the L1 /
  vector-store dedup scan its own schedule. A held interval trigger is visible:
  `compact.status: "not_triggered"` plus `throttled: {trigger: "interval",
  reason, marker, last_attempt_at, attempt_interval_hours, retry_after, note}`.

### Changed

- **`can_apply` for an extension surface now reports real readiness, not just
  the config switches.** Previously an enabled surface with a `handler` string
  and a non-`suggest` mode reported `can_apply: true` even if no adapter had
  ever been registered under that name — a promise `apply_agent_action` would
  then refuse. `routing.extension_readiness` is now the single computation
  behind both the capability output and the execution path, so they cannot
  disagree. `SurfaceCapability` gains `handler_registered` and `apply_blockers`
  (and reuses `store_path` for the handler's resolved target);
  `MemorySurfaceCapabilities` gains `agent_extensions_root` and
  `registered_handlers`. `RoutingDecision.framework_can_apply` follows the same
  rule. `configured_handler` is now reported verbatim even while the surface is
  disabled, so a handler name nobody registered is visible rather than hidden.
- **`agent_extensions` config shape gains two keys.** `root` at the top level
  and `path` per surface, both defaulting to `null`. They are the explicit
  target a file-writing handler needs; nothing is defaulted, because an
  implicit target is the hazard the whole opt-in chain exists to prevent. Also
  readable from `LAYERED_MEMORY_AGENT_EXTENSIONS_ROOT`. Existing configs are
  unaffected — a section without them behaves exactly as before.
- **The common core holds no product paths.** `config.py`,
  `memory_compactor.py`, `routing.py`, `agent_memory_guard.py` and
  `memory_changelog.py` no longer contain a product path, product file name or
  `§` literal in operative code; `tests/test_v353_agent_adapters.py` asserts
  that over the AST (docstrings excluded, since explaining what moved is the
  point). What moved: `default_hermes_db_path` / `default_sessions_dir` /
  `detect_agent_memory_path` / `detect_agent_memory_separator` now ask the
  adapter; `agent_memory_separator` is a property that falls back to the
  adapter's default instead of a hardcoded `§`;
  `memory_compactor._get_memory_max_chars` asks the adapter for the product's
  configured limit and then its fallback, dropping `_find_hermes_config`,
  `_read_hermes_memory_limit`, `_is_hermes_memory_path` and
  `_HERMES_DEFAULT_MEMORY_MAX_CHARS`; `agent_integrator.detect_agent_type`
  delegates to the registry and re-exports `SOUL_BLOCK` from the Hermes
  adapter; `session_scanner.default_hermes_db` takes the path from the adapter
  and keeps only the SQL. Behaviour under Hermes is unchanged — a second agent
  is now a registration, not a patch to the router, the compactor or the config.
  `_parse_entries` lost its `separator="§"` default and now requires the
  argument; every call site already resolved it through `_resolve_separator`.
- **No live-session database is a reportable state, not a crash.** With an
  adapter that has none, `config.hermes_db_path` is `None` and
  `scan_sessions` / `search_sessions` / `read_state_db_sessions` report
  "no live-session database for this agent" instead of assuming a path.
- **Non-Hermes hosts get their own separator and their own limit.** Two
  consequences of routing these through the adapter, both affecting non-Hermes
  installs only. (1) With no `agent_memory.separator` configured, a Claude Code
  / Cursor / Cline / generic memory file now falls back to a blank line instead
  of `§` — the old default meant such a file parsed as one single entry, so the
  compactor saw one giant bloat entry instead of its real contents. (2) The
  dynamic limit read is no longer attempted against `~/.hermes/config.yaml`
  when the resolved adapter is not Hermes; previously a stray Hermes config on
  the host could cap an unrelated agent's memory file at 2000 characters.
  Behaviour on a Hermes host is byte-for-byte unchanged.

- **Agent-memory projections report refusals.** `_ensure_l0_pointer_in_memory`,
  the `index_only` upsert and the pointer reap return
  `{action: "skipped", reason: "unsafe_auto_detect_write", detected_path, trust,
  hint}`; `auto_maintain_after_write` adds `agent_memory_write` and skips both
  the dual-write and lazy compaction; `inject_knowledge` hoists the same block
  to `agent_memory_write` so a skipped projection is visible at the top level.
  The primary L1 write is never failed by a refused projection. `compact_memory`
  refuses a real pass (`dry_run=False`) on an unguarded auto-detected file
  before injecting anything into L1 — it would otherwise lift content out of a
  file it is not allowed to clean afterwards; `dry_run=True` still reports on it.
- **The dashboard's `/compact-memory` no longer reports a refusal as success.**
  A failed pass (missing memory file, or a guard-declined target) returns
  `success: false` with the error, hint and `agent_memory_write` block instead
  of "Memory compaction completed."
- **`get_memory_surface_capabilities` reports `memory_changelog` honestly.** Its
  `can_apply` now follows the backend's configured state rather than being
  `true` because the surface is framework-owned: with
  `memory_changelog.enabled: false` it reports `can_apply: false` and says so in
  `notes`. `SurfaceCapability` gained an optional `store_path`, which the
  changelog fills with its resolved JSONL path.
- **Test isolation covers `Path.open`.** `pathlib.Path.open` calls `io.open`
  directly, so it never went through the `builtins.open` backstop in
  `conftest.py` — every `path.write_text(...)` in the framework, which is how
  all memory and L1 writes are actually performed, bypassed the production-path
  guard. Both are patched now.
- **`inject_knowledge` annotates, it does not redirect.** Every result now
  carries a `routing` block, and a write the router would have sent elsewhere
  also carries `routing_warning`. Existing fields, actions and handshakes are
  untouched: advisory mode changes nothing about what a write does. `enforce`
  is recognised by the schema and reported, but no write path gates on it.
- **`batch_inject_knowledge_tool` exposes per-item routing** and now propagates
  the full `deferred_fusion` handshake (`needs_fusion`, `expected_hash`,
  `fusion`, `reason`) for batched items, so batching cannot be the one path
  where an update is silently lost.
- **`compact_memory` is routing-aware, and `enforce` stops the L1 dumping
  ground.** Its rule has always been structural ("not an L0 pointer" → L1),
  which is a statement about the source, not a classification of the content —
  so runbooks, standing preferences and raw traces in the agent's memory file
  all landed in L1. Every non-index entry is now classified, and the report
  gains `routing_mode`, `routing_summary` (counts by surface and by ownership,
  `l1_injected` / `l1_planned` / `blocked_from_l1`), `l1_candidates`,
  `todo_candidates`, `agent_owned_candidates`, `rejected` and `routing_notes`.
  In **advisory** mode (the default) migration behaviour is unchanged: every
  non-index entry still goes to L1, and `routing_advisory` lists what the
  router would have sent elsewhere. In **enforce** mode only `l1_knowledge`
  entries are injected; TODO / skill / user-profile / agent-other / undecided /
  ephemeral entries are left in agent memory untouched and returned as
  candidates. Enforce writes nothing outside L1 — no TODO is created
  (compaction has no TODO write contract) and no `agent_extensions` adapter is
  invoked, even when one is configured — and never deletes a blocked entry.
  Legacy fields (`migrated_count`, `migrated`, `errors`, `cleaned_memory`,
  `stats`) keep their meaning in both modes.
- **`compact_memory` takes an optional `routing_mode`.** Per-call override of
  `routing.mode` (`"advisory"` / `"enforce"`), on the MCP tool and the module
  function; an unknown value is ignored with a note rather than failing. Omit it
  to use the configured mode, so existing calls behave exactly as before. With
  `routing.enabled: false`, `enforce` degrades to advisory — enforcing on
  classifications the config forbids computing would block every entry.
- **`auto_maintain_after_write`'s `compact` block gained a `status`.** Three
  outcomes used to be indistinguishable from `triggered` plus a count:
  `migrated` (entries reached L1), `blocked_only` (the pass ran and could only
  block non-L1 entries) and `backed_off` (it did not run, and says why), next to
  `no_changes`, `not_triggered` and `error`. A pass that ran also reports
  `blocked_set`, `backoff_decision` and, under `enforce`, `blocked_set_state`.
  `triggered` and every pre-existing key keep their meaning. Compaction
  candidates additionally carry `entry_hash`, so a caller can follow one entry
  across passes without storing its text.
- **The backoff's byte-identity shortcut now needs two pins, not one.** It
  compared the memory file against the one the recorded pass left behind and
  called classification deterministic — true only for a *fixed* classifier, so
  a changed marker vocabulary or domain table went unnoticed until somebody
  happened to edit the memory file. The decision (and the state record, hence
  `auto_compaction_state.json` `schema_version: 2`) now also carries
  `classifier_inputs_hash`; a mismatch closes the shortcut and re-derives the
  blocked set with a dry run, reporting `classifier_inputs_changed: true` and
  re-pinning the inputs so the next decision can be cheap again. An
  unavailable hash fails open with `status: "classifier_inputs_unavailable"`.
  `compact_memory` reports `classifier_inputs_hash` next to `blocked_set`, so
  an external caller can compare two blocked sets on the same terms. Being
  derived state, a `schema_version: 1` document is ignored rather than
  migrated: one extra pass per memory file, then it re-records itself.
- **The L1 dedup step no longer rides the compaction marker.** It read
  `.last_auto_compact`, which a backed-off pass correctly leaves alone — so on
  an install that had been backing off for a while, *every write* re-ran a full
  scan of every knowledge file plus the vector-store prune to find nothing. It
  now has `.last_l1_dedup`, falling back to the old marker while that file is
  absent so an upgrading install continues its existing cadence instead of
  firing one extra scan. `report["l1_dedup"]` gained `triggered: true` when it
  runs (it already reported `{"triggered": false}` when it did not).
- **Docs corrected.** README (en / zh-CN / ja / ko), the package docstring, the
  `cognitive_decision_prompt`, and `init_framework`'s welcome text no longer
  present skills as an "L2" tier beneath L1. They describe the common core, the
  agent-owned extension surfaces, source-owned raw sessions and derived stores.
- **`README.ja.md` / `README.ko.md` carry the full surface-and-routing model.**
  Both were a release behind the en/zh-CN pair: they stated the surface table
  but not the behaviour around it. They now document the same public model —
  the complete environment-variable table (routing, `agent_extensions`,
  `agent_memory`, `memory_changelog`), the `config.yaml` section with the
  advisory-vs-`enforce` split and "detection is not permission", the
  agent-neutral common core and its `agent_memory.adapter` table, the opt-in
  `agent_extensions` layer with the `apply_agent_action` status table and the
  `generic_file` path rules, the governance/cleanup tool tables, and the
  architecture sections for the memory changelog, durable knowledge vs. process
  state, advisory routing on the write path, `routing.enforce_direct_writes`,
  routing on the compaction path, derived data, the safe-cleanup boundary and
  namespace isolation. Their stale inline v0.4.0/v0.5.0 changelogs now point at
  `CHANGELOG.md` like the en/zh-CN pair. Docs only; no behaviour change.
- **`KnowledgeType.PROCEDURE` is documented as legacy.** The value is kept and
  still parses from existing frontmatter — removing it would make older
  markdown unreadable — but procedural content belongs on the SKILL surface.

## [3.4.7] - 2026-10-12

### Added

- **Lifecycle governance for L1 knowledge.** Durable knowledge and process
  state are now told apart. After a write and in `validate_knowledge`, state
  markers (TODO / FIXME / next step / pending / 待办 / 下一步 / 待拍板 / 待验证 /
  尚未接入 / 未落地 …) are detected and each marker line gets one structured
  suggested action: `move_to_todo` (open work belongs in the TODO store),
  `rewrite_as_current_rule` (resolved history such as "has been fixed" /
  "已修复"), or `keep_with_reason` (the section has an explicit
  `State retention reason:` / `状态保留原因：` line). Detection is
  deterministic, advisory, and makes no model calls. The post-write report
  also flags same-topic heading families and oversized files.
- **Configurable marker vocabulary.** A new `governance:` section in
  `<LAYERED_MEMORY_HOME>/config.yaml` accepts `open_markers`,
  `resolved_markers`, `keep_reason_labels`, and `disabled_markers`. The shipped
  Chinese and English defaults are extended by user lists, and individual
  defaults can be turned off. No code changes are needed to support another
  language or house style.
- **`plan_knowledge_cleanup_tool(max_items=50)`.** A read-only planner that
  sorts every deterministic finding into `safe_auto_fixes`,
  `needs_user_decision`, and `must_not_auto_fix`, with a policy block that
  states which actions are never automatic.
- **`apply_safe_knowledge_cleanup_tool(actions, dry_run=True)`.** Applies only
  mechanical actions from the plan: `migrate_legacy_backups`, `sync_l0_index`,
  `reindex_vector_store`, and `create_todo_from_marker`. It defaults to a dry
  run. Semantic changes (deleting facts, merging sections, cross-domain moves,
  rewriting history as a rule, removing the source line of a migrated TODO)
  are rejected by name and left to the user or agent. TODO ids are derived from
  the source line, so applying the same plan twice is a no-op.
- **`promotion.watch_patterns` / `LAYERED_MEMORY_PROMOTION_WATCH_PATTERNS`.**
  Case-insensitive glob patterns for catch-all files, used by the
  maintenance, audit, and cleanup scans. The shipped defaults cover common
  English and Chinese names (`misc`, `misc-*`, `general`, `other`, `notes`,
  `inbox`, `scratch`, `unsorted`, `杂项`, `其它`, `未分类`, …), so a new
  knowledge base no longer needs a file literally named `misc.md` to get
  split/promotion suggestions. Set it to `[]` to turn pattern matching off.
- `validate_knowledge` now includes `rot_summary`, `rot_health_score`, and
  `governance_summary`. Each degrades to an `*_error` key instead of failing
  validation.

### Changed

- **Backups moved out of the live knowledge directory.** Whole-file rewrites
  no longer drop `<file>.md.bak` next to the live file. A single rolling
  backup per file is kept under `<LAYERED_MEMORY_HOME>/data/backups/knowledge/`
  (relative paths preserved). Existing in-tree `*.md.bak` files are reported
  by the cleanup planner and can be moved with the `migrate_legacy_backups`
  safe action.
- **Vector text uses a noise-stripped heading.** The embedded text now uses
  `embedding_heading()`, which removes dates, versions, issue numbers, and
  parenthesised qualifiers, so periodic entries cluster by topic instead of by
  date. The heading also appears once in the embedded text instead of twice.
  Displayed headings and `metadata["section"]` keep the original text.
  Vectors are derived data: run `reindex_vector_store_tool` to rebuild
  existing vectors with the new heading text.
- Exact `promotion_watch_domains` matching is unchanged and is still the only
  gate on the write path. The broader patterns apply only to pull-style
  maintenance and audit scans, so ordinary writes do no extra embedding work.

### Compatibility

- All new tools, config keys, and response fields are additive. Existing tool
  signatures are unchanged, and the legacy `suggested_action` field is still
  emitted on open-marker findings alongside the new `action`.

## [3.4.6] - 2026-10-07

### Fixed

- **`update_todo` / `delete_todo` answered "success" for an id that matched no
  row.** Both paths ran the statement and returned `{"success": true, "id": …}`
  without ever looking at the result, so a mistyped or fabricated id was
  indistinguishable from an applied edit. This is not a theoretical slip: an
  agent hand-typed a UUID (once with one character corrupted, once invented
  outright, because it had only seen a truncated id) and both calls reported
  success while the row stayed untouched — the mistake only surfaced on a later
  read-back, as stale content. Silent success on a write is worse than a loud
  failure: it makes the caller's own audit unverifiable. `update()` and
  `delete()` now check `rowcount` and return `{"success": false, "error": "TODO
  not found: <id>"}` (SQLite reports `rowcount == 1` for a no-op edit that
  matched the row, so idempotent updates still succeed). The dashboard
  `/update-todo` endpoint, which discarded the store's verdict and always
  answered "TODO updated successfully.", now propagates it.
- **An unknown `status` / `priority` was written as-is.** `status="banana"`
  stored garbage *and* skipped the terminal-timestamp machine, leaving a row
  that was neither open nor finished and had no stamp of either kind. Both
  values are now validated against `TodoStatus` / `TodoPriority`.

## [3.4.5] - 2026-10-07

### Added

- **The consolidation knobs are now readable from the operator's config.yaml**
  (`consolidate: {enabled, min_family, size_ceiling}`). Precedence is
  constructor → env → config.yaml → default, matching `write_guard` and
  `session_scan`. Why it matters: `consolidate_size_ceiling` could previously
  only be moved by editing this package or by adding an env var to a systemd
  unit, so on a deployed host it sat at its 0.9 default — a recommended
  tightening to 0.7 stayed "recommended" for a week while every write-back kept
  being judged against 0.9. A tuning knob whose only local spelling is "patch the
  library" is a knob that never moves; "how much compaction is enough" is a
  judgement about the operator's knowledge base, not a framework constant.

## [3.4.4] - 2026-10-07

### Fixed

- **The size penalty stopped responding, so the backlog it was meant to flag was
  never touched.** `oversized` docked `min(count * 4, 24)` points: with 45 files
  over the 4KB threshold on the live store the cap was already fully charged, so
  fixing 39 of them moved the score by exactly zero — 89% of the work for no
  visible return, which is why that backlog sat for 8 weeks. The penalty is now
  charged on **excess bytes** (`OVERSIZED_BYTES_PER_POINT` = 24KB per point,
  still capped at 24), so every fixed file moves the score and a 57KB file costs
  more than a 4.5KB one. Live effect: 45 files / 505KB of excess now docks 21
  points instead of a saturated 24, and drops as files are consolidated.
- **Test-suite isolation test failed whenever TMPDIR lived inside `~/.hermes`.**
  `test_home_is_redirected` asserted the sandbox home is not *path-prefixed* by
  `~/.hermes`, but Hermes points TMPDIR at `~/.hermes/cache/scratch`, so pytest's
  `tmp_path` (and therefore the sandbox home) sits under that prefix by design —
  a red that had nothing to do with the leak it guards. It now compares resolved
  paths (sandbox home ≠ real home; sandbox `MEMORY.md` ≠ production `MEMORY.md`),
  which holds in both environments.

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

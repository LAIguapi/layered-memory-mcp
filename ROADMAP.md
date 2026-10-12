# Roadmap

Remaining work on the surface model introduced in `[Unreleased]`, in the order
it should be taken. Each item states what exists today and what "done" means,
so none of them has to be re-derived later.

## 1. Persistent `MEMORY_CHANGELOG` backend — done, with two gaps

**Done.** `memory_changelog` now has an append-only JSONL store at
`<home>/data/memory_changelog.jsonl` (`storage.MemoryChangelogStore`), written
from `inject_knowledge` (and therefore from `compact_memory` and the batch
tool), from the whole-file create/update/delete tools, and from the two L0 entry
paths (`sync_l0_index`, `manage_entry`). It is readable through
`get_memory_changelog` / `get_memory_changelog_stats`, configurable through
`memory_changelog:`, and `get_memory_surface_capabilities` reports `can_apply`
from the backend's actual state instead of from ownership.

**Gap 1 — retention is inert.** `retention_days` is parsed and reported, and
nothing applies it. That is deliberate for one release: a sweep that deletes
provenance is destructive and a silent one is worse than none. Done when a
prune exists that is explicit (its own tool or an opt-in flag), reports what it
removed, and is documented as deleting history.

Since the file therefore only grows, the store's read ceiling is now *reported*
rather than only logged: above `MAX_READ_BYTES` (16 MiB) nothing is parsed and
both reads carry `read_truncated: true`, `read_error:
"memory_changelog_too_large"`, `max_read_bytes` and `file_size_bytes`, so an
unreadable log is no longer indistinguishable from an empty one. That is a
report flag, not retention — the file is still never rewritten.

**Gap 2 — `l1_section_deleted` has no producer.** The operation is in
`RECORDABLE_OPERATIONS` because section deletion is a semantic mutation, but no
tool deletes a single section today (`apply_safe_knowledge_cleanup` deliberately
never removes L1 text). Done when a section-level delete exists and records it —
or, if it never does, when the operation is dropped from the vocabulary rather
than left as a slot nothing fills.

Also left for whoever needs it: `sync_l0_index` diffs domain *sets*, so a
rename shows up as an add plus a remove. Only `manage_entry` can emit
`l0_entry_renamed`. Fixing this means the sync comparing entry lines per domain,
which is more than the provenance value justifies right now.

## 2. Adapter extraction of the Hermes-specific paths — done, with named leftovers

**Done.** `adapters/environment.py` defines `AgentEnvironmentAdapter` plus a
priority-ordered registry; `adapters/hermes.py` holds every Hermes fact
(`~/.hermes` layout, `§`, the 2000-char fallback, the `config.yaml` limit
reader, `SOUL.md` and `SOUL_BLOCK`); `adapters/editors.py` holds the Claude
Code / Cursor / Cline layouts; a generic adapter knows only
`<home>/agent-memory.md`. `agent_memory.adapter` selects one, default `auto`
(probe Hermes-first, i.e. the pre-adapter chain verbatim); an unknown id falls
back to `generic` with a warning, never silently to Hermes.

`config.py`, `memory_compactor.py`, `routing.py`, `agent_memory_guard.py` and
`memory_changelog.py` now carry **no** product path, product file name or `§`
literal in operative code — `tests/test_v353_agent_adapters.py` asserts that
over the AST, excluding docstrings. `detect_agent_type` delegates to the
registry. `get_memory_surface_capabilities` reports
`agent_environment: {configured, resolved, known, …}`. Registering an adapter
is a complete integration: the test suite proves a fictional `acme` adapter
redirects path, separator, state DB and char limit without a core edit.

**Leftover 1 — Hermes-era public names.** `MemoryConfig.hermes_db_path`, the
`session_scan.hermes_db_path` config key, the `LAYERED_MEMORY_HERMES_DB` env
var, `session_scanner.default_hermes_db()` / `config.default_hermes_db_path()`
and `scan_sessions(hermes_db_path=...)` keep their names. Their *values* come
from the adapter, and `config.agent_state_db_path` is the neutral alias to read
instead. Renaming them is a breaking config/API change, so it belongs to a
major version, not here.

**Leftover 2 — `l0_format: "hermes"`.** The L0 entry *format* token (and
`_generate_hermes_index`) is a format name, not a path. Renaming it would
invalidate every existing `config.yaml` and `LAYERED_MEMORY_L0_FORMAT`. Done
when a major version renames it to something like `pointer`/`index` with an
alias for the old value.

**Leftover 3 — two Hermes support packages stay out of the adapter.**
`write_guard/` (plugin payload, installer, `hermes` CLI candidates) and
`dashboard_plugin/` are self-contained Hermes integrations; the Hermes adapter
only points at the first (`HermesEnvironment.write_guard()`) so the core never
names it. `agent_integrator.build_init_recommendation` and `integrate_agent`'s
`inject_soul` / `install_dashboard` / `install_guard` branches still key off
`agent_type == "hermes"` — the type comes from the adapter, but the option text
and the installers are Hermes-only, and refuse clearly on any other host. Done
when a second agent actually needs one of those surfaces and the branch becomes
a capability lookup.

## 3. Optional `apply_agent_action` execution path — done, with three gaps

**Done.** `apply_agent_action` is a write-capable MCP tool and public function
that refuses by default and reports every refusal as a structured
`AgentActionResult` (`{applied, status, surface, operation, handler, adapter,
mode, dry_run, confirmed, target, blockers, detail, error, metadata}`). The
gates are: action validation (`kind` / `surface` / `operation` /
`source_content`, refused not repaired), extension-surface membership, the two
`enabled` switches, `mode != suggest`, a `handler` that is registered *and*
supports the surface, the handler's own `readiness`, `confirmed=True` under
`apply_with_confirmation` or for an action asking for it, then `dry_run`.

`adapters/generic_file.py` is the reference adapter: it appends one bounded
Markdown section to an explicitly configured file and never edits existing text
(`update` records what it supersedes). It refuses `no_configured_path`,
`path_escapes_base`, `unsafe_system_target` and `unsafe_file_type`. It is the
only registered handler, and registration grants nothing.
`routing.extension_readiness` is the single computation behind both
`get_memory_surface_capabilities` and the execution path, so `can_apply` can no
longer promise a write that would then be refused.
`tests/test_v354_apply_agent_action.py` covers the chain link by link; no
agent-owned apply records a `MEMORY_CHANGELOG` event.

**Gap 1 — no agent-native handler.** Writing a *Hermes* skill or profile file
(or Claude Code's, or Cursor's) is still unimplemented, deliberately: the
generic handler proves the contract without the framework claiming to know
another product's skill format. Done when an environment adapter also exposes
an extension-write target and a handler uses it — at which point the question
"what shape does a skill file have in this product?" has to be answered, and
that answer belongs to the product, not here.

**Gap 2 — append-only, so no lifecycle.** `update` appends a superseding
section and nothing ever removes or supersedes-in-place. A file that
accumulates forty revisions of one runbook is the user's to clean up; the
handler will not prune it. Done when either a compaction pass for these files
exists (and says what it removed) or the limitation is documented as permanent.

**Gap 3 — the lock file is left behind.** The handler takes the same
`<path>.lock` `FileLock` the L1 write path uses, so two concurrent applies
cannot interleave their sections, but nothing removes the lock file afterwards
(neither does `l1_store`). Harmless, and still litter next to a file the user
owns. Done when the write paths clean up their own locks, or when the `.lock`
convention moves somewhere out of the user's directory.

## 4. `inject_knowledge` enforce behaviour — decided, with two gaps

**Decided, and encoded as two independent switches.** `routing.mode` keeps
gating `compact_memory` only — the path where the *framework* picks the target —
and still never refuses an explicit write. The new
`routing.enforce_direct_writes` (default **false**, env
`LAYERED_MEMORY_ROUTING_ENFORCE_DIRECT_WRITES`) is the only switch that can
refuse one. With it on, an `inject_knowledge` whose content classifies as a
*positive* non-L1 surface (`skill`, `user_profile`, `agent_other`, `todo`,
`ephemeral` — the gate is an allow-list, so any future surface starts refused)
returns
`{success: false, action: "routing_refused", error:
"routing_enforce_direct_write_refused", routing, routing_warning,
suggested_action, routing_enforcement, hint}` — classified *before* the write,
so no file, no section, no `MEMORY_CHANGELOG` event and no agent-owned write.
`routing_override="force_l1"` (on `inject_knowledge`, `inject_knowledge_tool`
and `batch_inject_knowledge_tool`) writes anyway and reports
`routing_override_applied: true`; an unknown value is ignored rather than read
as permission. `routing.enabled: false` makes the flag inert, a classifier
failure fails **open**, and compaction passes `direct_write=False` so one config
key never silently re-decides two behaviours. All four READMEs carry the
behaviour matrix, and `get_memory_surface_capabilities` reports both switches
plus an `l1_knowledge` note saying which one is live.
`tests/test_v355_inject_enforce.py` locks all of it.

**`undecided` is not refused** — decided here, with the gate shipping as an
allow-list of `l1_knowledge` / `l0_index` / `undecided`. `undecided` is what the
classifier returns when *no rule matched*, so refusing it would make the gate's
answer a function of length rather than of content: a short unmarked fact
(below `MIN_CHARS_FOR_FALLBACK`) classifies `undecided`, while the same fact
written out at length is admitted as `l1_knowledge` with score 0.0 by the
length-only fallback. Such a write proceeds and is annotated —
`routing_enforcement.insufficient_evidence: true` plus a `routing_warning`
naming the absence of evidence and the override — rather than being refused for
absence of evidence.

**Gap 1 — the fuse/consolidate handshake is gated on both halves.** Under the
strict gate the second call (`fuse=True`) is classified like any other direct
write, so a fused body that reads as a runbook is refused mid-handshake and the
caller has to re-submit with the override. That is consistent (it *is* a direct
L1 write) but it means a handshake can dead-end on content the first half
accepted. Done when either the gate recognises a handshake continuation by its
`expected_hash`, or the limitation is documented as intended.

**Gap 2 — the whole-file tools are not gated; now documented as such.**
`create_knowledge_file` / `update_knowledge_file` write L1 without passing
through the router at all, so `enforce_direct_writes` does not reach them. They
are blunt instruments by design, but "strict common-core purity" is not strict
while that door is open. The documentation half of "done" is in: all four
READMEs, `config/config.example.yaml` and the `l1_knowledge` capability note now
state that the flag governs `inject_knowledge` / `batch_inject_knowledge_tool`
only. Fully done when the whole-file tools classify too — which needs a decision
about what "the content" of a whole-file write even is (one body, many
sections), so it is a feature, not a follow-up edit.

## 5. ja/ko README routing-section translation pass — done, with one gap

**Done.** `README.ja.md` and `README.ko.md` now carry the same public model as
the en/zh-CN pair: the full environment-variable table (routing,
`agent_extensions`, `agent_memory`, changelog), the `config.yaml` section with
the advisory-vs-`enforce` and `enforce_direct_writes` explanation and the
"detection is not permission" paragraph, "the common core is agent-neutral"
(the `agent_memory.adapter` table plus the registration example), the opt-in
`agent_extensions` layer with the `apply_agent_action` status table, the
`generic_file` handler and its path rules, the governance/cleanup tool tables,
and the architecture sections for the memory changelog, durable knowledge vs.
process state, advisory routing on the write path, strict direct writes,
routing on the compaction path, derived data, the safe-cleanup boundary and
namespace isolation. Raw sessions are stated as source-owned evidence rather
than a writable surface in all four READMEs.

**Gap — two en-only sections are still en-only.** The Hermes write-guard
quick-start (`init_framework` → `integrate_agent(action="install_guard")` →
restart, plus `write_guard: auto`) and the Semble attribution design note exist
in `README.md` only; `README.zh-CN.md` does not carry them either, so ja/ko
match the zh-CN baseline rather than the en superset. Done when all three
translations gain them, or when the en README's extra sections are explicitly
declared en-only.

Also worth knowing, not a gap: the `governance:` / `routing:` examples in the
ja/ko files use Japanese and Korean marker strings (`保持理由`, `手順書`,
`보존 이유`, `운영 절차서`, …). Those are *user-added* values — each list is
annotated in-line as extending the built-in vocabulary, which remains English +
Chinese only.

## 6. Lazy auto-compaction re-trigger under `enforce` — done

**Done.** `compact_memory` reports `blocked_set: {count, fingerprint,
by_surface}` — a sorted hash over each blocked entry's whitespace-normalised
content plus its routed surface and status, with no timestamps, so it is a
property of the set and not of the pass that saw it. Candidate records carry
`entry_hash` for the same reason. `auto_maintain_after_write` remembers the last
one per memory file in `<home>/data/auto_compaction_state.json`
(`storage/auto_compaction_state.py`: versioned, keyed by a hash of the memory
file path, hashes and counts only, atomic replace, every read/write
best-effort) and skips a lazy pass that could only repeat itself, reporting
`compact.status: "backed_off"` plus a top-level `auto_compaction_backoff`
(`status: "blocked_set_unchanged"`, `probe`, `blocked_set`,
`migratable_l1_candidates`, `first_seen_at`, `last_attempt_at`,
`skipped_passes`, `hint`).

The decision is deliberately cheap and deliberately conservative: first a
byte-identity check against the file the recorded pass left behind *plus* a
classifier-inputs check (see below), otherwise a dry-run classification — never
a full write pass to learn nothing changed — and *anything* unexpected (no
record, changed fingerprint, changed routing config, classifier inputs that
cannot be described, failed probe, a migratable entry, an entry refused for
want of a heading) resolves to "run". The backoff may delay noise; it may never
delay work.
`compact.status` now separates the three outcomes that used to look alike
(`migrated` / `blocked_only` / `backed_off`), advisory mode neither reads nor
writes the state, and an explicit `compact_memory` call is never backed off.
`tests/test_v356_auto_compaction_backoff.py` locks all of it; README (en /
zh-CN / ja / ko) carries the behaviour matrix.

**Both gaps are closed** (`tests/test_v357_auto_compaction_backoff_gaps.py`).

*The marker that meant three things.* `<home>/.last_auto_compact` recorded "a
pass ran" and was read as both "the interval has elapsed" and "the dedup scan
is due". There are now three markers: `.last_auto_compact` (unchanged name and
meaning, so an install's cadence survives the upgrade),
`.last_auto_compact_attempt` (the lazy path reached a *decision*, backoff
included) and `.last_l1_dedup`. The attempt marker throttles the interval
trigger only, and only while agent memory has not changed since it
(`auto_maintain_attempt_interval_hours`, default 1, clamped to at most one
`auto_maintain_interval_days`, `0` to disable) — so a backed-off decision is no
longer re-derived on every write, while new bloat, a new L1 candidate, any edit
to the file, the usage-ratio trigger and the critical safety net all still
reach a decision at once. A held trigger is reported, not silent:
`compact.status: "not_triggered"` plus `throttled: {...}`. The coupling in Gap 1
was real and worse than recorded: because a backoff leaves the compact marker
alone, the dedup step re-ran a full scan of every knowledge file plus the
vector-store prune on *every write* once an install had been backing off for a
while. Its own marker ends that, falling back to the old one while absent.

*The other half of "classification is deterministic".* `routing.classifier_inputs_hash`
digests the marker vocabulary as compiled for the config (so
`routing.extra_*_markers` / `disabled_markers` are folded in, and a rule set
added to `RoutingRules` later is covered automatically), the routing switches,
the scoring thresholds and tie-break order, `domain_keywords` and `l0_tag` —
normalised so order never matters, with no timestamp, path or memory content in
it. The state record pins it (`schema_version: 2`; a v1 document is ignored
rather than migrated, costing one pass), a mismatch closes the byte shortcut
and re-derives the blocked set with a dry run reporting
`classifier_inputs_changed: true`, a skip under changed inputs re-pins them,
and an unavailable hash fails open (`classifier_inputs_unavailable`).

Left as deliberate behaviour, not a gap: a classifier-config edit on an install
whose *only* live trigger is the interval one can wait out the attempt throttle
(≤ 1h by default) before it is noticed. Every other path — any memory write,
the usage-ratio trigger, the critical trigger, an explicit `compact_memory` —
sees it immediately, and the alternative (reading the state file and hashing
the config on the trigger hot path) buys nothing a 7-day interval cares about.

## 7. Final review annotations — comments, plus the fixups that followed

A read-only review of the common-core routing work (verdict: pass, no P0) named
a set of non-obvious invariants and latent gaps. They are recorded **as
comments at the code they constrain**, so the tradeoff is read by whoever edits
the line rather than by whoever happens to open this file.

Fixed before release (see `3.5.0` in `CHANGELOG.md`):

- `injector._L1_WRITE_SURFACES` — the strict gate no longer refuses on absence
  of evidence. It is an allow-list (`_GATE_ALLOWED_SURFACES`) of
  `l1_knowledge` / `l0_index` / `undecided`; a positive non-L1 classification
  is refused, `undecided` is written with
  `routing_enforcement.insufficient_evidence: true`. See §4.
- `storage.MemoryChangelogStore.load` — an oversized log no longer reads as an
  empty one: `read_status()` reports `read_truncated` / `read_error` /
  `max_read_bytes` / `file_size_bytes` through `query` and `stats`. See §1.
- `api/server.py` `lifespan` — resolves `MemoryConfig`'s own `knowledge_dir`
  and `<home>/data` instead of defaulting to a product path, and the module is
  now in the AST boundary test's `_CORE_SOURCES`.
- `integrate_agent(action="inject_memory")` — the one remaining write that
  treated detection as permission now goes through
  `agent_memory_guard.resolve_write_target` (with the detected candidate
  judged, so a configured target elsewhere is not read as permission for it).

Still open; each is a reminder, not a change:

- `routing.SCORE_FLOOR` — unreachable with shipped weights; the "treat as
  unrouted" branch has never run and needs tests before it is relied on.
- `routing.MIN_CHARS_FOR_FALLBACK` — the only path returning `l1_knowledge`
  with no supporting signal. It no longer moves what the direct-write gate
  refuses, but it still moves `compact_memory`'s enforce behaviour and which
  `routing_warning` an explicit write carries.
- `injector._direct_write_gate` — the validation-failure `note` is not
  observable through the current result shape; do not report from it.
- `memory_compactor.detect_memory_bloat` — dereferences a possibly-`None` path
  where `compact_memory` guards; the only in-tree caller swallows the
  exception. Should return `compact_memory`'s `success: false` shape.
- `memory_compactor._attempt_interval_seconds` — `auto_maintain_interval_days:
  0` skips the clamp, so the interval trigger means "at most hourly while agent
  memory is untouched"; the usage-ratio and critical triggers stay unthrottled.
- `governance.scan_procedure_contamination` — runs on every L1 write at
  sections × marker vocabulary; bound the scan before dropping the advisory.
- `integrate_agent(action="inject_soul")` — writes the persona file
  (`SOUL.md`), also adapter-detected, and is **not** guarded: it is a persona
  surface rather than a memory file, the block is marker-delimited and
  reversible through `remove_soul`, and no derived memory content lands in it.
  Done when the guard's rule is generalised to "any framework write into an
  agent-owned file", or when this exemption is restated as permanent.

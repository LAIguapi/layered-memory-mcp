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

## 3. Optional `apply_agent_action` execution path

The `agent_extensions` config exists and `apply_agent_action` refuses with
`status: "unsupported"` because the registry is empty. No real skill/profile
write is implemented, by design.

Done when: one reference adapter performs a real write for one agent-owned
surface, gated on all four opt-in conditions plus confirmation semantics for
`apply_with_confirmation`, and a refusal path that is reported rather than
silent — the same contract [item 5 of the write guard] establishes.

## 4. `inject_knowledge` enforce behaviour — decide it

`compact_memory` gates on `routing.mode: enforce`; `inject_knowledge` does not.
A direct L1 write of non-L1-looking content is annotated (`routing_warning`)
and written anyway. That asymmetry is deliberate for now — a classifier that
starts rejecting explicit writes on upgrade is a data-loss event — but it is an
open decision, not a settled design.

Done when: either enforce gates direct writes too (with an override argument),
or the asymmetry is documented as permanent and `routing_warning` is the whole
contract. Either way the capability output and README must state it.

## 5. ja/ko README routing-section translation pass

`README.md` and `README.zh-CN.md` describe the common core, the agent-owned
extension surfaces and the routing config. `README.ja.md` and `README.ko.md`
had their "L2 skills" claim corrected but not the full section.

Done when: both carry the same routing/surface section as the en/zh-CN pair.

## 6. Lazy auto-compaction re-trigger under `enforce`

Under `routing.mode: enforce`, entries blocked from L1 are deliberately left in
agent memory. `auto_maintain_after_write`'s trigger conditions are usage ratio
and elapsed interval, so a memory file full of blocked entries stays over
threshold and re-runs compaction on every write, each pass migrating nothing
(`blocked_from_l1` is reported, but nothing backs off).

Done when: a blocked set is remembered (hash + count) and does not re-trigger a
pass until it changes, while a genuinely new bloat entry still triggers one
immediately.

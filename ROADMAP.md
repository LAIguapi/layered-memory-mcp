# Roadmap

Remaining work on the surface model introduced in `[Unreleased]`, in the order
it should be taken. Each item states what exists today and what "done" means,
so none of them has to be re-derived later.

## 1. Persistent `MEMORY_CHANGELOG` backend

Today `layered_memory_mcp.memory_changelog` ships the schema and the inclusion
rules (`RECORDABLE_OPERATIONS` / `NOT_RECORDED`) only; there is no store, and
`get_memory_surface_capabilities` says so. Provenance for semantic L0/L1
mutations is the one common-core surface the framework claims to own but cannot
yet write.

Done when: a durable append-only store under `<home>/data/`, written from the
L1 write paths, readable by a tool, with a retention policy — and the
capability output flipped to `can_write`.

## 2. Adapter extraction of the Hermes-specific paths

`adapters/` defines the protocol and an empty registry, but Hermes assumptions
still sit in the core: `~/.hermes/memories/MEMORY.md` and `~/.hermes/state.db`
defaults, the `§` separator heuristic, the `write_guard` plugin layout,
`agent_integrator`'s SOUL/MEMORY snippets.

Done when: those live behind an adapter/support layer, the core knows no
product paths, and a second agent can be supported by registering an adapter
rather than by editing the router or the compactor.

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

# Layered Memory MCP Server

> Extend AI agent memory beyond token limits with a common-core knowledge framework.

[**中文**](README.zh-CN.md) | [**日本語**](README.ja.md) | [**한국어**](README.ko.md)

[![PyPI version](https://img.shields.io/pypi/v/layered-memory-mcp.svg)](https://pypi.org/project/layered-memory-mcp/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## The Problem

AI agents have **limited memory** — typically 2-4KB of persistent context injected every turn. Once it's full, the agent forgets everything else. You can't store project configurations, user preferences, API conventions, or domain knowledge without constantly fighting the space limit.

## The Solution

**Layered Memory** owns a small, well-defined **common core** and keeps everything else where it already belongs:

```
Raw session / user input / tool result
        │
        ▼
  persistence classifier
        │
        ├─ framework-owned common core  ← the framework writes these
        │    ├─ L0 index          2-4KB, injected every turn: what exists and where
        │    ├─ L1 knowledge      durable domain cognition: facts, decisions, current state
        │    ├─ TODO              unfinished state: pending, blocked, needs validation
        │    └─ memory changelog  provenance of L0/L1 changes
        │
        ├─ agent-owned extension surfaces  ← classified and suggested, not written
        │    ├─ skill             action protocols, runbooks, command sequences
        │    ├─ user profile      small standing user preferences
        │    └─ agent other       agent-owned content the framework won't classify further
        │
        └─ no durable write
             └─ undecided / ephemeral (one-off output, raw traces)
```

**L0 is your table of contents. L1 is your bookshelf. TODO is your open-items tray. Raw sessions are your diary — read-only evidence the framework searches but never writes.**

Your skills and your user profile stay yours. The framework recognises them and hands back a structured recommendation; it only writes them if you explicitly [opt in](#opt-in-agent-extensions).

The L0 index, the vector store and the search index are **derived** from the L1 markdown and can be rebuilt at any time.

## Features

- **Smart Knowledge Injection** — Inject facts with auto-dedup and section merging (upsert/append/merge), auto L0 sync
- **Persistence Routing** — A deterministic classifier (`classify_memory_item`) says which surface an item belongs to: durable L1, the TODO store, your own skill/profile surface, or nothing at all. Marker + structure rules only; no model call, no embedding. Advisory by default, so it annotates writes without changing them
- **Capability Reflection** — `get_memory_surface_capabilities` reports, under your current config, what the framework may write and what it can only suggest
- **Opt-in Agent Action Execution** — `apply_agent_action` can write a classified skill / profile / agent-other item for you, but only as an explicitly configured orchestrator: off by default, with per-surface switches, a confirmation mode, `dry_run`, and an explicit target path. Every refusal is reported, never silent
- **Hybrid Search (RRF)** — Combines keyword, fuzzy, BM25, and semantic search with Reciprocal Rank Fusion for robust ranking. Query-type auto-detection optimizes the semantic/keyword balance
- **Agent-Agnostic L0 Access** — `get_l0_index` tool lets any MCP agent retrieve the memory index
- **Multi-Agent Namespace** — Isolate knowledge per agent via `LAYERED_MEMORY_NAMESPACE` while sharing common knowledge
- **Session Scanning** — Extract knowledge candidates from recent agent sessions
- **Health Validation** — Check L0-L1 consistency, file structure, and knowledge quality, plus a rot and governance summary
- **Lifecycle Governance** — Flags process state left in durable knowledge (TODO / FIXME / next step / pending / 待办 / 待拍板 …) and suggests a structured action: `move_to_todo`, `rewrite_as_current_rule`, or `keep_with_reason`. Marker lists are configurable
- **Cleanup Planning** — A read-only planner sorts findings into safe mechanical fixes, items that need a decision, and items that must not be auto-fixed; a separate tool applies only the safe ones, dry-run by default
- **Write Safety** — Whole-file rewrites keep a backup out of tree under `<LAYERED_MEMORY_HOME>/data/backups/knowledge/`, so the live knowledge directory holds only live files
- **Cleaner Vector Recall** — Embeddings use a noise-stripped heading (dates, versions, and issue numbers removed); displayed headings and metadata stay unchanged
- **Configurable Catch-all Scans** — Maintenance and audit scans find "misc"-style files by glob pattern (`misc`, `notes`, `inbox`, `scratch`, `杂项`, …), so you don't need a file named `misc.md`
- **Space Analysis** — Monitor memory usage and get optimization suggestions
- **Agent-Neutral** — Works with any MCP-compatible agent (Hermes, Claude, Cursor, etc.)
- **Zero Dependencies** — Core engine uses Python stdlib only; only `fastmcp` for MCP transport
- **Privacy First** — All data stored locally, no external API calls

> **Design Note:** The hybrid search ranking is inspired by [Semble](https://github.com/MinishLab/semble), a fast code search library for agents. We adapted their RRF (Reciprocal Rank Fusion) approach — with multi-signal boosting and query-aware alpha detection — for markdown knowledge retrieval.

## Quick Start

### Install

```bash
pip install layered-memory-mcp
```

### Hermes Agent

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  layered-memory:
    command: layered-memory-mcp
    timeout: 30
```

Then, from an agent session:

1. `init_framework()` — creates the store and reports whether the MEMORY.md
   write guard is installed.
2. `integrate_agent(action="install_guard")` — deploys and enables the guard
   plugin (Hermes only) and silences the periodic memory-review nudge.
3. Restart Hermes so the plugin loads (gateway: `hermes gateway restart`).

The guard keeps agent-side writes out of the framework-owned MEMORY.md: the
native `memory` tool with `target="memory"` and direct `write_file` / `patch`
edits of `memories/MEMORY.md` are blocked, and the model is pointed at
`inject_knowledge` (L1 knowledge) or `target="user"` (the user profile) instead.
Set `write_guard: auto` in `~/.layered-memory/config.yaml` to have
`init_framework()` install or repair it on its own.

### OpenClaw

Install the MCP server, then register it:

```bash
pip install layered-memory-mcp

# Register as an MCP server
openclaw mcp set layered-memory --command layered-memory-mcp
```

Layered Memory complements OpenClaw's built-in vector-based memory:
- **OpenClaw memory**: semantic search over session transcripts (heavy, needs embeddings)
- **Layered Memory**: structured keyword search over curated knowledge files (light, instant)
- Use both: OpenClaw for "what did I say about X?" and Layered Memory for "what's the database connection string?"

### Claude Desktop

Add to your Claude Desktop MCP config:

```json
{
  "mcpServers": {
    "layered-memory": {
      "command": "layered-memory-mcp"
    }
  }
}
```

### Cursor / Other MCP Clients

```bash
# stdio mode (default)
layered-memory-mcp

# HTTP mode
layered-memory-mcp --transport http --port 8080

# Verbose logging
layered-memory-mcp --verbose
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LAYERED_MEMORY_HOME` | Root directory for memory data | `~/.layered-memory/` |
| `LAYERED_MEMORY_SESSIONS_DIR` | Agent sessions directory (auto-detected) | `~/.hermes/sessions/` |
| `LAYERED_MEMORY_AUTO_SYNC_L0` | Auto-sync L0 index after writes | `true` |
| `LAYERED_MEMORY_DEDUP_THRESHOLD` | Similarity threshold for dedup (0.0-1.0) | `0.7` |
| `LAYERED_MEMORY_L0_FORMAT` | L0 index format: `hermes` or `generic` | `hermes` |
| `LAYERED_MEMORY_NAMESPACE` | Agent namespace for multi-agent isolation | `shared` |
| `LAYERED_MEMORY_PROMOTION_WATCH_PATTERNS` | Comma-separated catch-all glob patterns for maintenance scans | built-in English + Chinese list |
| `LAYERED_MEMORY_ROUTING_ENABLED` | Classify items and annotate write results | `true` |
| `LAYERED_MEMORY_ROUTING_MODE` | `advisory` or `enforce` — gates `compact_memory` only | `advisory` |
| `LAYERED_MEMORY_ROUTING_ENFORCE_DIRECT_WRITES` | Refuse an explicit `inject_knowledge` write of non-L1 content | `false` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_ENABLED` | Let the framework apply agent-surface actions | `false` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_MODE` | `suggest` / `apply_with_confirmation` / `apply` | `suggest` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_ROOT` | Base dir for a file-writing extension handler | unset (refuses) |
| `LAYERED_MEMORY_AGENT_MEMORY_PATH` | The agent's own memory file (explicit write target) | auto-detected |
| `LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE` | Allow L0 projection into an auto-detected memory file | `false` |
| `LAYERED_MEMORY_CHANGELOG_ENABLED` | Record semantic L0/L1 mutations to the memory changelog | `true` |
| `LAYERED_MEMORY_CHANGELOG_PATH` | Changelog file location | `<home>/data/memory_changelog.jsonl` |

### config.yaml

You can also set most behavior in `<LAYERED_MEMORY_HOME>/config.yaml`. The precedence is: constructor argument, then environment variable, then config.yaml, then the built-in default. Sections relevant to knowledge governance and routing:

```yaml
governance:
  # Each list EXTENDS the built-in Chinese + English defaults.
  open_markers: ["WIP", "follow-up"]          # open process state → move_to_todo
  resolved_markers: ["shipped"]                # resolved history → rewrite_as_current_rule
  keep_reason_labels: ["Retention note"]       # "<label>: <reason>" keeps a section's markers
  disabled_markers: ["TBD"]                    # turn off defaults that are noise for you

promotion:
  watch_domains: ["misc"]                      # exact catch-all file names (also checked on writes)
  watch_patterns: ["misc", "misc-*", "notes", "inbox", "scratch", "杂项"]  # case-insensitive globs, maintenance/audit scans only

routing:
  enabled: true
  mode: advisory                               # advisory | enforce — gates compact_memory (see below)
  enforce_direct_writes: false                 # true = inject_knowledge REFUSES non-L1 content
  # Each list EXTENDS the built-in vocabulary, same style as governance:
  extra_skill_markers: ["cookbook"]
  extra_todo_markers: ["needs sign-off"]
  extra_profile_markers: ["addresses me as"]
  extra_knowledge_markers: ["service contract"]
  disabled_markers: ["script"]                 # drop a default that is noise for you

agent_memory:
  adapter: auto                                # which agent's file layout to assume (see below)
  path: null                                   # the agent's own memory file (explicit target)
  allow_auto_detect_write: false               # or opt back into auto-detected projection writes
  separator: null                              # override the adapter's entry separator

memory_changelog:
  enabled: true                                # kill switch; off = nothing is recorded
  path: null                                   # defaults to <home>/data/memory_changelog.jsonl
  retention_days: null                         # reported, NOT enforced — nothing prunes the log
```

`mode: advisory` is the default: routing reports its opinion alongside the normal result and changes nothing about what a write does. A classifier that started rejecting writes on upgrade would be a data-loss event, not a feature, so `enforce` is opt-in — and exactly one path gates on it, `compact_memory` (see [Routing on the Compaction Path](#routing-on-the-compaction-path)), because that is the path where the *framework* picks the target. `inject_knowledge` still writes L1 in either mode and says so in its `routing_warning`.

`enforce_direct_writes` is the second, independent switch, and the only one that can refuse an explicit write (see [Strict Direct Writes](#strict-direct-writes-opt-in)). Setting `mode: enforce` does not turn it on, and turning it on does not change what compaction migrates. With `routing.enabled: false` it is inert — there is no classification to enforce.

Omit a key to keep the defaults. If you set `watch_patterns`, your list replaces the built-in pattern list. Set it to `[]` to turn pattern matching off.

`agent_memory:` controls where the framework may project the L0 pointer / knowledge-index entry into the agent's *own* memory file (`MEMORY.md`, `CLAUDE.md`, …). With no path configured the framework auto-detects one, which is fine for reading but not for writing: any ambiguous config — a test, a throwaway script on a temp home — would otherwise resolve the real file and silently append to it. A projection write therefore needs an explicit target (`agent_memory.path`, or a `memory_path` argument), an explicit opt-in (`allow_auto_detect_write: true`), a detected file inside `<LAYERED_MEMORY_HOME>`, or a home that is this install's real configured home and not a temp/test sandbox. Anything else is refused and reported as `agent_memory_write: {status: "skipped", reason: "unsafe_auto_detect_write", detected_path, hint}` — the L1 write itself always succeeds, only the projection stops. **Detection is not permission:** resolving a path through an adapter grants no right to write it.

### The common core is agent-neutral

The core owns L0, L1, TODO and `MEMORY_CHANGELOG` semantics, and nothing else. *Where* the host agent keeps its own memory file, which character separates the entries in it, where its live-session database lives and how many characters it allows are product facts, answered by an **environment adapter** in `src/layered_memory_mcp/adapters/`. Those facts used to be literals in `config.py`, `memory_compactor.py` and `agent_integrator.py`; they are not any more.

```yaml
agent_memory:
  adapter: auto     # default
```

| value | resolves |
| --- | --- |
| `auto` *(default)* | probe the registry in order — `hermes`, `claude_code`, `cursor`, `cline`, `generic` — which is exactly the candidate chain the pre-adapter core hardcoded |
| `hermes` | `~/.hermes/memories/MEMORY.md`, `§` separator, `~/.hermes/state.db`, `~/.hermes/sessions`, and the real limit read back from `~/.hermes/config.yaml` |
| `claude_code` | `~/.claude/CLAUDE.md` |
| `cursor` | `./.cursorrules` |
| `cline` | `~/.cline/rules` |
| `generic` | no product paths at all — only the framework-owned `<home>/agent-memory.md` |
| `none` / `off` | alias for `generic` |

**Hermes is the default adapter for backwards compatibility**, and is the first one probed, so an existing install resolves exactly the paths it always did. An unknown id falls back to `generic` with a warning — never silently to Hermes. Env equivalent: `LAYERED_MEMORY_AGENT_ADAPTER=hermes`.

A second agent is supported by registering an adapter, not by editing the router, the compactor or the config:

```python
from layered_memory_mcp.adapters.environment import EnvironmentAdapterBase, environments

class AcmeEnvironment(EnvironmentAdapterBase):
    id = "acme"
    display_name = "ACME Agent"
    memory_relpath = (".acme", "brain.md")      # relative to the user's home
    install_marker_relpath = (".acme",)
    state_db_relpath = (".acme", "sessions.sqlite")
    separator = "---"
    path_tokens = (".acme", "brain.md")         # identifies an explicitly configured path

environments.register(AcmeEnvironment(), priority=15)
```

`get_memory_surface_capabilities()` reports the outcome under `agent_environment: {configured, resolved, known, entry_separator, …}`. That is distinct from `adapter`, which names the optional extension-*action* handler bundle below.

Two Hermes-specific pieces deliberately stay outside the adapter, in their own support packages: the `write_guard` plugin (payload, installer, `hermes` CLI probing) and `dashboard_plugin`. `integrate_agent`'s Hermes-only actions (`inject_soul`, `install_dashboard`, `install_guard`) refuse with a clear message on any other host.

### Opt-in agent extensions

Skills, the user profile and other agent-owned content are **suggestion-only by default**: the framework classifies them, hands back an `AgentAction` with your own text plus a structural `shape_hint`, and writes nothing. If you would rather have the framework apply those actions for you, opt in explicitly:

```yaml
agent_extensions:
  enabled: false                 # master switch (default false)
  mode: suggest                  # suggest | apply_with_confirmation | apply
  adapter: null                  # label for your integration, e.g. generic_file / custom
  root: null                     # optional base dir for relative handler paths
  surfaces:
    skill:
      enabled: false
      handler: null              # name of a registered adapter
      path: null                 # explicit target for file-writing handlers
    user_profile:
      enabled: false
      handler: null
      path: null
    agent_other:
      enabled: false
      handler: null
      path: null
```

Five conditions must hold before the framework will apply anything to a surface: `enabled: true` at the top level, `enabled: true` for that surface, a `handler` naming an actually **registered** adapter, a `mode` other than `suggest`, and a handler that can resolve a safe target. Anything missing or malformed falls back, key by key, to "classify and suggest". **Opting in does not transfer ownership**: `get_memory_surface_capabilities` keeps reporting `owned_by: agent` and `default_behavior: suggest` for those surfaces and only flips `can_apply`. The framework is an orchestrator there, never the owner.

`can_apply` reports *real* readiness, not just the switches: a handler you named but never registered reports `handler_registered: false`, and `apply_blockers` lists the same reason codes `apply_agent_action` would refuse with (`extensions_disabled`, `surface_disabled`, `mode_suggest`, `no_handler`, `handler_not_registered`, `no_configured_path`, …). `registered_handlers` names what you may configure.

#### `apply_agent_action` — the write-capable tool, off by default

```
apply_agent_action(
  surface="skill", operation="create", title="发布流程",
  source_content="<your text>", rationale="repeatable action protocol",
  confirmed=false, dry_run=false,
  action=null,            # a JSON AgentAction; replaces the flat fields
)
```

It returns `{applied, status, surface, operation, handler, adapter, mode, dry_run, confirmed, target, blockers, detail, error, metadata}`. `status` is one of:

| status | wrote anything? | meaning |
| --- | --- | --- |
| `disabled` | no | `agent_extensions` (or this surface) is off, or `mode: suggest` — recommendation only |
| `unsupported` | no | no handler configured, not registered, or it does not support the surface |
| `refused` | no | not an extension surface, or the handler declined (no explicit path, unsafe target, …) |
| `invalid_action` | no | `kind` / `surface` / `operation` / `source_content` did not validate — never repaired |
| `confirmation_required` | no | `mode: apply_with_confirmation` (or the action asked for it) and `confirmed` was false |
| `would_apply` | no | `dry_run: true` — reports the target, byte count and heading |
| `applied` | **yes** | the handler wrote it |
| `error` | no | the handler raised; the exception is in `error` |

The three modes: `suggest` (default) classifies and hands you the `AgentAction`; `apply_with_confirmation` resolves and reports the target but needs a second call with `confirmed=true`; `apply` writes on the first call, except for an action whose own `requires_user_confirmation` is set, which always needs confirming.

Applying an agent-owned action does **not** record a `MEMORY_CHANGELOG` event. That log is provenance for framework-owned L0/L1 mutations; these writes are the agent's.

#### The `generic_file` reference handler

One handler ships registered, and registration is not permission — it writes nothing until the chain above is configured *and* a target path resolves:

```yaml
agent_extensions:
  enabled: true
  mode: apply_with_confirmation
  adapter: generic_file
  surfaces:
    skill:
      enabled: true
      handler: generic_file
      path: /srv/agent/extensions/skills.md
```

It appends one bounded Markdown `##` section per action — your `source_content` under a short metadata block (surface, operation, handler, timestamp, rationale, `shape_hint`, action metadata), opened by a `<!-- layered-memory:agent-action -->` marker so the sections stay machine-findable. `create`, `append` and `update` **all append**; `update` records what it `supersedes` rather than attempting a fuzzy in-place edit, because rewriting a file the framework does not own is the one behaviour that could destroy content. Content over 8000 chars is truncated with an explicit marker.

Path rules, all of them refusals rather than guesses:

| situation | outcome |
| --- | --- |
| no `path` and no `root` | refused, `no_configured_path` — there is no default location, on purpose |
| `root` only | `<root>/<surface>.md` |
| relative `path` | resolved under `root`, else under `LAYERED_MEMORY_HOME`; must stay inside it (`path_escapes_base`) |
| absolute `path` | accepted — naming the file *is* the explicitness this gate wants |
| a directory (existing, or trailing `/`) | `<dir>/<surface>.md` |
| inside `/etc`, `/usr`, `/sys`, `/var/lib`, `C:\Windows`, … | refused, `unsafe_system_target` |
| a `.py` / `.yaml` / `.json` / `.db` / `.env` / dotfile target | refused, `unsafe_file_type` — appending Markdown would corrupt it |

Parent directories are created only for a path you configured. There is no command adapter and no shell execution anywhere in this path. Writing a Hermes-native skill or profile file is **not** implemented and remains out of scope.

A custom handler is a class with `name`, `supports(surface)`, an optional `readiness(surface, config)` and `apply(action, config=None, *, dry_run=False)`, registered via `layered_memory_mcp.adapters.registry.register(...)`. An adapter that declares no `dry_run` parameter is never called under `dry_run=True` — the framework refuses instead of risking a write it promised not to make.

## Usage

### 1. Write Knowledge (Recommended)

The `inject_knowledge` tool is the **primary write path** for all agents. It handles deduplication, section targeting, and automatic L0 index sync in a single call.

```
Agent learns: "Production DB is PostgreSQL 15 on prod-db:5432"
→ inject_knowledge(
    domain="infrastructure",
    section="Database",
    content="PostgreSQL 15 on prod-db:5432, connection pool: 20 max",
    mode="upsert"
  )
← Creates/updates infrastructure.md, auto-syncs L0 index
```

**Write modes:**
| Mode | Behavior |
|------|----------|
| `upsert` (default) | Replace if similar content exists, append if new |
| `append` | Always append, skip dedup check |
| `merge` | Combine unique parts from new + existing |

### 2. Read Knowledge

```
Agent: "What's the database connection string?"
→ recall_knowledge(keyword="database")
← Returns relevant sections from infrastructure.md
```

### 3. Validate Health

```
→ validate_knowledge()
← Checks L0↔L1 consistency, orphaned files, stale entries, file health,
  and adds rot_summary / rot_health_score and governance_summary
  (state markers found in L1, grouped by lifecycle action)
```

### 4. Plan and Apply Cleanup

The cleanup workflow keeps mechanical fixes separate from semantic ones:

```
1. plan_knowledge_cleanup_tool(max_items=50)
   ← read-only JSON with three buckets:
     safe_auto_fixes      move legacy *.md.bak out of knowledge/, re-sync L0,
                          reindex the vector store, copy open markers into TODOs
     needs_user_decision  rewrite resolved history, merge heading families,
                          shrink oversized files, …
     must_not_auto_fix    markers kept on purpose with a keep-reason line

2. apply_safe_knowledge_cleanup_tool(actions=<items from safe_auto_fixes>)
   ← dry run (default): reports what would change, writes nothing

3. apply_safe_knowledge_cleanup_tool(actions=..., dry_run=False)
   ← applies only the four safe action types, and rejects anything else
```

The apply tool never deletes or rewrites L1 text. When it turns a marker into a TODO, the source line stays in place. Merges, rewrites, deletions, and cross-domain moves are left to the user or agent, and are made through the normal write tools. For embedding-based checks (cross-file duplicates, split candidates), use `reconcile_knowledge_tool`.

### 5. Session Compression (Cron Job)

Set up a daily cron to extract new knowledge from conversations:

```
1. scan_recent_sessions → get session summaries
2. AI analyzes summaries → identifies stable facts
3. New facts → written via inject_knowledge (auto L0 sync)
4. L0 index → always up to date
```

### 6. Legacy CRUD (Also Available)

For direct file manipulation:

| Tool | Description |
|------|-------------|
| `create_knowledge_file` | Create a new .md file (auto L0 sync) |
| `update_knowledge_file` | Overwrite an existing file (auto L0 sync) |
| `delete_knowledge_file` | Delete a file (auto L0 sync) |

## MCP Tools

### Read Tools

| Tool | Description |
|------|-------------|
| `recall_knowledge` | Search L1 knowledge files by keyword with relevance scoring |
| `get_knowledge_file` | Read a specific knowledge file by name |
| `list_memory_stats` | Get space statistics, file sizes, and optimization suggestions |
| `scan_recent_sessions` | Scan recent sessions for knowledge extraction candidates |
| `search_sessions_by_keyword` | Search session content by keyword |
| `get_l0_index` | Retrieve the full L0 index (agent-agnostic) |
| `classify_memory_item` | Route a candidate item to a surface (`l1_knowledge` / `todo` / `skill` / `user_profile` / `agent_other` / `undecided` / `ephemeral`). Read-only, deterministic, no model call |
| `get_memory_surface_capabilities` | Report which surfaces the framework may write and which it can only suggest, under the current config |
| `get_memory_changelog` | Read the provenance log of semantic L0/L1 mutations (filter by domain / operation / surface / since; newest-first, bounded) |
| `get_memory_changelog_stats` | Summarise the changelog: counts by operation / surface / domain, time span, configured state |

### Write Tools

| Tool | Description |
|------|-------------|
| **`inject_knowledge`** | **Primary write path** — smart injection with dedup, section targeting, auto L0 sync |
| `create_knowledge_file` | Create a new .md file (auto L0 sync) |
| `update_knowledge_file` | Overwrite an existing file (auto L0 sync) |
| `delete_knowledge_file` | Delete a file (auto L0 sync) |
| `apply_agent_action` | Apply an agent-owned extension action (`skill` / `user_profile` / `agent_other`) through a configured handler. **Disabled by default** — returns `status: "disabled"` until `agent_extensions` is opted into; supports `dry_run` and `confirmed`, and records no `MEMORY_CHANGELOG` event |

### Management Tools

| Tool | Description |
|------|-------------|
| `sync_l0_index` | Manually rebuild L0 index from L1 files (supports `dry_run`) |
| `validate_knowledge` | Health check: L0↔L1 consistency, file quality, duplicates, plus rot score and governance (state-marker) summary |
| `manage_l0_entry` | Add / remove / replace individual L0 entries |

### Governance & Cleanup Tools

| Tool | Description |
|------|-------------|
| `audit_rot_tool` | Read-only rot audit: health score (0-100), oversized files, garbled headings, stale sections, cross-file duplicates, catch-all promotion candidates |
| `reconcile_knowledge_tool` | Embedding-based cross-file duplicate and split-candidate scan |
| `plan_knowledge_cleanup_tool` | Read-only cleanup plan: `safe_auto_fixes` / `needs_user_decision` / `must_not_auto_fix` |
| `apply_safe_knowledge_cleanup_tool` | Applies only safe mechanical actions from the plan (`migrate_legacy_backups`, `sync_l0_index`, `reindex_vector_store`, `create_todo_from_marker`); `dry_run=True` by default |

These tools are additive. Existing tools keep their parameters and response fields. `validate_knowledge` and the post-write governance report only gain extra keys, for example a per-finding `action`.

## MCP Resources

| Resource | Description |
|----------|-------------|
| `memory://status` | Overall system status and configuration |
| `knowledge://files` | List all knowledge files with metadata |

## MCP Prompts

| Prompt | Description |
|--------|-------------|
| `knowledge_compression_prompt` | Template for AI-driven knowledge extraction from sessions |
| `cognitive_decision_prompt` | Decision framework for disciplined memory usage |

## Architecture Deep Dive

### The Surface Model

| Surface | Owner | Cost | Holds |
|---------|-------|------|-------|
| L0 index | framework | Tokens per turn | ~2KB of pointers: what exists and where |
| L1 knowledge | framework | 1 file read | Durable facts, decisions, boundaries, current understanding |
| TODO | framework | 1 query | Unfinished state: pending, blocked, needs validation, awaiting a decision |
| Memory changelog | framework | — | Provenance of semantic L0/L1 changes |
| Skill | **agent** | 1 skill load | Repeatable procedures, command sequences, checklists, runbooks |
| User profile | **agent** | injected | Small standing user preferences and persona facts |
| Agent other | **agent** | — | Agent-owned memory-like content that fits neither |
| Raw sessions | **source** | Full search | Read-only evidence; scanned and searched, never written |

Two distinctions do the real work here:

- **Owner vs. appliable.** Ownership is fixed: L0/L1/TODO/changelog are the framework's, skill/profile/other are the agent's, raw sessions are the source's. Whether the framework may *apply* a change to an agent-owned surface is a separate, opt-in question — see [Opt-in agent extensions](#opt-in-agent-extensions).
- **Source of truth vs. derived.** The L1 markdown files are the source of truth. The L0 index, `data/vectors.db` and the search index are derived from them and are rebuildable (`sync_l0_index`, `reindex_vector_store_tool`).

### What the Memory Changelog Is

`MEMORY_CHANGELOG` is the changelog **of your memory**, not of this software. It records semantic mutations of the durable surfaces — an L1 section created, updated, merged, replaced or deleted; a fusion or consolidation write-back; an L0 entry added, removed or renamed when the map actually changed.

It is explicitly *not* the repository `CHANGELOG.md`, not raw session logs, not a generic event log, and not TODO lifecycle history. Derived-data rebuilds (vector reindex, search index) are not recorded either: the semantic content did not change.

**Storage.** Append-only JSONL at `<home>/data/memory_changelog.jsonl`, one event per line. Recording is best-effort and silent on failure — provenance must never be able to fail the write it describes — and nothing in the framework rewrites or deletes a line. `retention_days` is read from config and reported, but **not enforced**: pruning provenance is destructive, so removing history stays a manual decision.

**What an event carries.** Structure, not content: `schema_version`, `event_id`, `at` (UTC ISO), `operation`, `surface`, `domain`, `section`, `actor` / `agent_id`, `tool`, a structural `summary` and `reason`, an optional `source_ref`, `before_hash` / `after_hash` of the affected body, and a `metadata` dict of byte counts and write-path details. **Never the body text.** The knowledge files are the source of truth for content, and copying bodies into a provenance log would hand every secret a user ever wrote into their notes a second, unindexed home. `metadata` accepts scalars and short strings only; a nested container is dropped rather than serialised.

**What is recorded today.** `inject_knowledge` (and so `compact_memory`'s L1 writes and `batch_inject_knowledge_tool`) for create / section-create / update / merge / replace / fusion write-back / consolidation write-back; `create_knowledge_file`, `update_knowledge_file` and `delete_knowledge_file` for whole-file mutations; `sync_l0_index` and `manage_l0_entry_tool` for L0 entries that actually appeared, vanished or changed.

**What is not recorded.** A deferred-fusion or deferred-consolidate handshake, a `fuse_conflict` or `consolidate_refused` refusal, a skipped duplicate, a validation failure, a failed write, a `dry_run=True` pass, an L0 resync whose entry set did not move, and a write whose content hash came out unchanged. Single-section deletion has no event yet because no tool deletes one section — see `ROADMAP.md`.

**Reading it.** `get_memory_changelog(domain, operation, surface, since, limit)` returns newest-first, bounded entries (default 50, hard ceiling 500); `get_memory_changelog_stats()` summarises counts by operation / surface / domain and the time span. Both are read-only. `get_memory_surface_capabilities` reports `memory_changelog` with `can_apply` reflecting whether the backend is actually enabled, plus the resolved `store_path`.

### Write-Once, Fully-Visible Pipeline (v0.5.0)

The key innovation in v0.5.0 is that **every write path automatically syncs the L0 index**:

```
Agent calls inject_knowledge(domain="infra", section="Proxy", content="...")
  │
  ├─ 1. Dedup check (SequenceMatcher, threshold=0.7)
  ├─ 2. Resolve action: upsert / append / merge / skip
  ├─ 3. Section targeting (finds or creates ## heading)
  ├─ 4. File write (with fcntl.flock for concurrency safety)
  └─ 5. Auto L0 index sync
        │
        ↓
  L0 index updated → Agent sees it next turn
```

This eliminates the "write-but-invisible" problem where agents write L1 files but the L0 index (injected into every turn) doesn't update, causing future sessions to ignore the new knowledge.

### Durable Knowledge vs. Process State

L1 is for **durable knowledge**, meaning the rules and facts that hold now. Unfinished work ("TODO: wire up X", "待拍板") and narratives of how something got fixed are **process state**, and they go stale fast. After a write, and in `validate_knowledge`, governance scans L1 for state markers and classifies each marker line:

| Action | Meaning |
|--------|---------|
| `move_to_todo` | Open work. It belongs in the TODO store (`add_todo` / `list_todos`), not in L1 |
| `rewrite_as_current_rule` | Resolved history ("has been fixed", "已修复"). Rewrite it as the rule that now holds |
| `keep_with_reason` | The section has an explicit `<keep-reason label>: <reason>` line, so its markers are intentional |

Detection is deterministic (regex and string matching, with no model calls) and advisory. The marker vocabulary is data and is configured through `governance:` in config.yaml, so other languages and house styles can be added without code changes.

The same pass also reports **procedure contamination**: sections that read as action protocols rather than durable knowledge. A section is only flagged when action markers **and** procedural structure both match (numbered steps, a checklist, a command sequence, commands in a code block) — an architecture note that merely mentions "验证" or "流程" is knowledge and is left alone. Flagged sections belong on your own skill surface; the framework reports them and moves nothing.

### Advisory Routing on the Write Path

`inject_knowledge` writes L1 — that is its contract, and by default routing does not change it. What routing adds is visibility: every result carries a `routing` block, and a write the classifier would have sent elsewhere also carries a `routing_warning` saying so. The write still happens, with the same `action` it always had. Batched writes expose the same per-item `routing`, and a batched `deferred_fusion` carries its full handshake (`needs_fusion`, `expected_hash`, `fusion`) — batching is a convenience, never a quieter write path.

This is the deliberate asymmetry with compaction: an explicit `inject_knowledge` call is a caller naming its own target, and `mode: enforce` does not override that. An installation that wants the stricter rule opts into it separately.

### Strict Direct Writes (opt-in)

`routing.enforce_direct_writes: true` turns the warning above into a refusal. The classification then runs **before** the write — before dedup, before the vector store, before anything touches disk:

| `routing.enabled` | `enforce_direct_writes` | `routing_override` | Content classified | Result |
|---|---|---|---|---|
| `true` | `false` (default) | — | anything | Written, `routing_warning` when non-L1 (unchanged behaviour) |
| `true` | `true` | `"none"` (default) | `l1_knowledge` / `l0_index` | Written normally, `MEMORY_CHANGELOG` event recorded |
| `true` | `true` | `"none"` | anything else | **Refused**, nothing written |
| `true` | `true` | `"force_l1"` | anything else | Written, `routing_override_applied: true` |
| `false` | `true` | — | not classified | Written, legacy result shape (no routing keys) |

`routing.mode` is orthogonal throughout: it decides what `compact_memory` migrates and never refuses a direct write on its own.

A refusal is structured, and it is not a validation error — the request was fine, the destination was not:

```json
{
  "success": false,
  "action": "routing_refused",
  "error": "routing_enforce_direct_write_refused",
  "message": "Content classified as 'skill' (owned by agent, score 0.88); direct L1 write to 'ops' / '## 发布流程' refused by routing.enforce_direct_writes. …",
  "written": false,
  "routing": {"surface": "skill", "ownership": "agent", "score": 0.88, "signals": ["…"]},
  "routing_warning": "…re-submit with routing_override='force_l1' if it really is L1 knowledge.",
  "suggested_action": {"kind": "agent_native_write", "surface": "skill", "shape_hint": ["…"]},
  "routing_enforcement": {"gate_active": true, "override_requested": "none", "override_applied": false},
  "hint": "Nothing was written: no L1 file, no section, no MEMORY_CHANGELOG event, and no agent-owned write."
}
```

Nothing is written on a refusal: no file, no section, no `MEMORY_CHANGELOG` event (that log records mutations, and a refusal is not one), and no agent-owned write either — `suggested_action` is handed back for *you* to apply, exactly as on the compaction path. TODO-shaped content comes back as a `todo_candidate`; `add_todo` stays your call.

`inject_knowledge_tool(..., routing_override="force_l1")` (and the same argument on `batch_inject_knowledge_tool`) writes anyway and marks the result `routing_override_applied: true`. Use it when the classifier is wrong about a piece of durable knowledge — not to push a runbook, a preference or a log into L1, because those have surfaces of their own and the refusal names them. An unknown value is ignored rather than read as permission. Both switches are reported by `get_memory_surface_capabilities` (`routing_mode`, `routing_enforce_direct_writes`, and the `l1_knowledge` note).

### Routing on the Compaction Path

`compact_memory` is routing-aware. Its migration rule has always been structural ("not an L0 pointer" → L1), which is a statement about the *source*, not a classification of the content — so a runbook, a standing user preference or a raw trace sitting in the agent's memory file all became L1 knowledge. Every non-index entry is now classified, and the mode decides what that classification is worth:

| Mode | What happens |
|------|--------------|
| `advisory` (default) | Unchanged migration: every non-index entry still goes to L1. The report adds `routing` per entry, `routing_advisory` for the entries the classifier would have sent elsewhere, and `routing_summary` counts by surface and ownership, so you can see what was misfiled and move it afterwards |
| `enforce` | Only `l1_knowledge` entries are injected. TODO, skill, user-profile, agent-other, undecided and ephemeral entries are **left in agent memory untouched** and returned as candidates for you to place |

Set it in `routing:` in config.yaml, or per call with `compact_memory(routing_mode="enforce")` — useful for previewing the split with `dry_run=True` before changing the config. Omitting the argument keeps the configured mode.

Enforce mode writes nothing outside L1. Blocked entries are reported, never deleted:

```json
{
  "routing_mode": "enforce",
  "routing_summary": {
    "by_surface": {"l1_knowledge": 1, "skill": 1, "todo": 1},
    "by_ownership": {"framework": 2, "agent": 1},
    "l1_injected": 1, "l1_planned": 1, "blocked_from_l1": 2
  },
  "l1_candidates": [{"surface": "l1_knowledge", "status": "migrated",
                     "destination": "knowledge/infra.md#架构边界"}],
  "todo_candidates": [{"surface": "todo", "status": "blocked_from_l1", "reason": "…"}],
  "agent_owned_candidates": [{"surface": "skill", "status": "blocked_from_l1",
                              "suggested_action": {"kind": "agent_native_write", "…": "…"}}],
  "rejected": []
}
```

Each candidate carries the entry preview, the routed surface and ownership, the rule-weight score, the reason, any warnings, its status and its destination when written — enough to audit the pass. Two deliberate limits: no TODO is created (compaction has no TODO write contract; `todo_candidates` is a report, and `add_todo` stays your call), and skill / user-profile / agent-other remain agent-owned — the `suggested_action` is handed back for you to apply, and compaction never invokes an `agent_extensions` adapter even when one is configured.

The legacy fields (`migrated_count`, `migrated`, `errors`, `cleaned_memory`, `stats`) keep their meaning in both modes, so existing callers do not need to change.

#### Backing Off a Blocked Set

A blocked entry stays in agent memory, which means the *lazy* maintenance trigger stays satisfied. `auto_maintain_after_write` fires on a usage ratio, so a memory file full of entries `enforce` will not move used to re-run a full pass — backup, classification, rewrite — on **every** write, migrating nothing each time.

The pass now fingerprints what it blocked (`blocked_set: {count, fingerprint, by_surface}` — per-entry content hashes plus the routed surface, sorted, no timestamps) and remembers it in `data/auto_compaction_state.json`. The next lazy trigger compares before it works:

| Situation | What the lazy pass does |
|-----------|-------------------------|
| First enforced pass that blocks entries | runs, records the fingerprint, reports `compact.status: "blocked_only"` |
| Same blocked set, nothing migratable | **skipped** — `compact.status: "backed_off"` plus `auto_compaction_backoff: {status: "blocked_set_unchanged", probe, blocked_set, skipped_passes, first_seen_at, hint}` |
| A new blocked entry (fingerprint changed) | runs on the very next write; `backoff_decision: "blocked_set_changed"` |
| A new L1-migratable entry | runs and migrates it, whether or not the blocked set changed; `backoff_decision: "l1_work_pending"` |
| The blocked entries get placed elsewhere | runs; the record is cleared, so the next bloat entry triggers immediately |
| `advisory` mode | unchanged — nothing is ever blocked, so there is nothing to back off, and no state file is written |
| Explicit `compact_memory(...)` | always runs. The backoff is for the lazy ride-along only; an explicit call is you stating intent |

`compact.status` separates the three outcomes that used to look alike: `migrated` (entries reached L1), `blocked_only` (the pass ran and could only block non-L1 entries), `backed_off` (it did not run, and says why) — plus `no_changes`, `not_triggered` and `error`.

The state file is derived operational state: versioned (`schema_version`), keyed by a hash of the memory file path, and carrying hashes and counts rather than memory text or absolute paths. It is neither knowledge nor `MEMORY_CHANGELOG` provenance, and deleting it costs exactly one extra no-op pass. Two details worth knowing: the decision first checks whether the memory file is byte-identical to the one the recorded pass left behind and only otherwise re-classifies with a dry run (so backing off is cheap, never a full pass), and an entry refused for want of a heading (`refused_needs_title`) counts as pending work — a refusal keeps being reported instead of being backed off into silence.

### Derived Data: L0 Index and Vectors

The L1 markdown files are the source of truth. The L0 index and the vector store (`data/vectors.db`) are **derived** from them and can be rebuilt at any time (`sync_l0_index`, `reindex_vector_store_tool`). The text that gets embedded uses a noise-stripped heading: `## Release process 2026-10-01 (v3.2)` is embedded as `Release process`, so periodic entries cluster by topic rather than by date. Displayed headings and stored metadata keep the original text. After upgrading, you can reindex so that existing vectors pick up the cleaner headings.

### Safe Cleanup Boundary

Automatic cleanup is limited to mechanical, reversible, or idempotent actions: moving legacy `*.md.bak` files out of `knowledge/` into `data/backups/knowledge/`, rebuilding derived L0 and vector data, and *copying* an open marker into the TODO store (with a deterministic id, so re-applying does nothing). Anything semantic is outside the safe boundary and is always left to the user or agent: deleting facts, merging conflicting sections, moving content across domains, rewriting history as a rule, or removing the source line after a TODO is created. `apply_safe_knowledge_cleanup_tool` rejects these actions by name.

### Relevance Scoring

When you call `recall_knowledge`, files are scored by:

1. **Filename match** (+10 points) — keyword appears in filename
2. **Heading match** (+3 points) — keyword appears in a `## heading`
3. **Content frequency** (+0.5 per occurrence, capped at 5) — how often keyword appears

Results are sorted by score, and only matching `## sections` are returned (not entire files).

### Namespace Isolation (v0.6.0)

Set `LAYERED_MEMORY_NAMESPACE` to isolate knowledge per agent:

```
knowledge/
├── shared/           ← Common knowledge, visible to all agents
│   ├── infrastructure.md
│   └── coding-standards.md
├── claude/           ← Claude Desktop's private knowledge
│   └── claude-specific.md
├── cursor/           ← Cursor IDE's private knowledge
│   └── cursor-config.md
└── hermes/           ← Hermes Agent's private knowledge
    └── hermes-setup.md
```

Each agent sees its own namespace first, then falls back to `shared/`. File name collisions resolve in favor of the namespace. All read/search/inject tools automatically merge both directories.

```bash
# Claude Desktop config
LAYERED_MEMORY_NAMESPACE=claude layered-memory-mcp

# Backward compatible: default "shared" = no isolation
layered-memory-mcp
```

### L0 Index Formats

Two index formats are supported:

| Format | Example | Best For |
|--------|---------|----------|
| `hermes` | `[L0索引] infra: servers, DB → knowledge/infra.md` | Hermes Agent memory injection |
| `generic` | `[infra.md] Server Configuration → proxy, db, deploy` | Standalone / other agents |

Configure via `LAYERED_MEMORY_L0_FORMAT` env var or the `l0_format` constructor argument.

### Session Compression

The `scan_recent_sessions` tool is designed for cron-job automation:

1. It scans session files from the past N days
2. Extracts user messages, assistant topics, and tool calls
3. Returns a structured JSON for an AI to analyze
4. The AI identifies stable knowledge and writes it to L1 files via `inject_knowledge`

This creates a **self-improving memory system** — the agent gets smarter over time as more knowledge is distilled from conversations.

## Agent Compatibility

Layered Memory is an MCP server — it works with any MCP-compatible agent.

| Agent | Config Method | Notes |
|-------|--------------|-------|
| **Hermes Agent** | `config.yaml` → `mcp_servers` | Native MCP client, L0 auto-injection via memory |
| **OpenClaw** | `openclaw mcp set` | Complements built-in vector memory |
| **Claude Desktop** | `claude_desktop_config.json` | Full MCP support, L0 via tool calls |
| **Cursor** | Settings → MCP | Full MCP support |
| **Codex CLI** | Codex MCP config | Full MCP support |
| **Any MCP client** | stdio or HTTP transport | Standard MCP protocol |

### When to use Layered Memory vs. built-in memory

Most agents have **limited persistent memory** (2-4KB per turn). Layered Memory solves this by:

1. **Separating index from content** — L0 stays small (fits in agent memory), L1 holds unlimited knowledge
2. **On-demand loading** — the agent only reads what it needs, when it needs it
3. **Self-improving** — session compression automatically extracts new knowledge over time

### Integration patterns

```
Agent (2KB memory limit)
  └── L0 index (injected every turn, ~500 bytes)
        ├── [L0] infrastructure: servers, DB → knowledge/infrastructure.md
        ├── [L0] api: REST conventions → knowledge/api-conventions.md
        └── [L0] dev: code style, testing → knowledge/development.md
              │
              ↓ (on demand via recall_knowledge)
        L1 knowledge files (unlimited, loaded by keyword)
```

## Cognitive Decision Framework

The surface model only works if the agent follows a disciplined decision process. This framework should be injected into the agent's system prompt (or loaded via the `cognitive_decision_prompt` MCP prompt) to ensure consistent behavior.

### Decision Tree

```
Agent encounters a problem or receives a request
  │
  ├─ Step 1: Scan L0 index for relevant domains
  │
  ├─ Step 2: Match found?
  │   ├─ YES → Load the corresponding L1 knowledge file (and your own skill, if procedural)
  │   │   │
  │   │   ├─ Knowledge solves it → Use it. Do NOT bypass with guessing.
  │   │   ├─ Knowledge partially covers it → Use it, then enhance the entry.
  │   │   └─ Knowledge insufficient → Treat as new problem (Step 3).
  │   │
  │   └─ NO → Treat as new problem (Step 3).
  │
  ├─ Step 3: Handle as new problem/requirement
  │   Use standard tools and reasoning to solve.
  │
  └─ Step 4: Post-solution evaluation — is this worth preserving, and where?
      ├─ Durable fact / decision / architecture  → L1, via inject_knowledge
      ├─ Unfinished work (pending, blocked)      → the TODO store, never L1
      ├─ Repeatable procedure / command sequence → your own skill surface
      ├─ Standing global user preference         → your own profile, after confirming
      ├─ One-off output or raw trace             → nothing; let it go
      └─ Unsure?                                 → classify_memory_item (read-only)
```

### Why This Matters

Without this decision framework, agents tend to:
- **Ignore existing knowledge** — they see the L0 index but forget to load L1 files, then waste time guessing
- **Repeat mistakes** — solved problems aren't captured, so the agent re-learns from scratch next session
- **Bypass established conventions** — each session starts from zero instead of building on accumulated knowledge

The framework turns the memory system from a passive storage into an **active cognitive loop**: consult → act → learn → improve.

### Integration

Add this to your agent's system prompt:

```
You use a layered memory system. Before tackling any problem:
1. Check the L0 index for matching domains
2. If matched, load and follow the L1 knowledge before acting
3. If unmatched, solve normally
4. After solving, route what is worth keeping: durable facts to L1 via
   inject_knowledge, unfinished work to the TODO store, procedures and
   standing user preferences to your own surfaces. Call classify_memory_item
   if you are unsure.
```

Or use the built-in MCP prompt `cognitive_decision_prompt` to get the full decision framework at runtime.

## Development

```bash
# Clone
git clone https://github.com/LAIguapi/layered-memory-mcp.git
cd layered-memory-mcp

# Install in dev mode
pip install -e ".[dev]"

# Run tests
pytest

# Run locally
python -m layered_memory_mcp.server
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full release history. Recent highlights in `3.4.7`:

- Lifecycle governance with structured actions (`move_to_todo` / `rewrite_as_current_rule` / `keep_with_reason`) and configurable marker lists
- `plan_knowledge_cleanup_tool` and `apply_safe_knowledge_cleanup_tool` (read-only planning, plus dry-run-first application of safe actions)
- Knowledge backups moved out of the live knowledge directory into `data/backups/knowledge/`
- Noise-stripped headings in embedded vector text
- Glob-based catch-all scans (`promotion.watch_patterns`) with English and Chinese defaults

## License

MIT License — see [LICENSE](LICENSE) for details.

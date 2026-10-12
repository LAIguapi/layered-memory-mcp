# Layered Memory MCP Server

> Extend AI agent memory beyond token limits with a 4-tier knowledge architecture.

[**中文**](README.zh-CN.md) | [**日本語**](README.ja.md) | [**한국어**](README.ko.md)

[![PyPI version](https://img.shields.io/pypi/v/layered-memory-mcp.svg)](https://pypi.org/project/layered-memory-mcp/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## The Problem

AI agents have **limited memory** — typically 2-4KB of persistent context injected every turn. Once it's full, the agent forgets everything else. You can't store project configurations, user preferences, API conventions, or domain knowledge without constantly fighting the space limit.

## The Solution

**Layered Memory** organizes knowledge into 4 tiers, trading immediacy for capacity:

```
┌─────────────────────────────────────────────────────┐
│  L0 — Index Layer (2-4KB, injected every turn)      │
│  Pure pointers: "what knowledge exists and where"    │
├─────────────────────────────────────────────────────┤
│  L1 — Knowledge Files (unlimited, loaded on-demand)  │
│  Structured markdown: configs, conventions, facts    │
├─────────────────────────────────────────────────────┤
│  L2 — Skills Layer (loaded when needed)              │
│  Procedures, workflows, tool-specific knowledge      │
├─────────────────────────────────────────────────────┤
│  L3 — Raw Sessions (searched rarely)                 │
│  Full conversation history, searchable by keyword    │
└─────────────────────────────────────────────────────┘
```

**L0 is your table of contents. L1 is your bookshelf. L2 is your cookbook. L3 is your diary.**

## Features

- **Smart Knowledge Injection** — Inject facts with auto-dedup and section merging (upsert/append/merge), auto L0 sync
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

### config.yaml

You can also set most behavior in `<LAYERED_MEMORY_HOME>/config.yaml`. The precedence is: constructor argument, then environment variable, then config.yaml, then the built-in default. Two of the sections that are relevant to knowledge governance:

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
```

Omit a key to keep the defaults. If you set `watch_patterns`, your list replaces the built-in pattern list. Set it to `[]` to turn pattern matching off.

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

### Write Tools

| Tool | Description |
|------|-------------|
| **`inject_knowledge`** | **Primary write path** — smart injection with dedup, section targeting, auto L0 sync |
| `create_knowledge_file` | Create a new .md file (auto L0 sync) |
| `update_knowledge_file` | Overwrite an existing file (auto L0 sync) |
| `delete_knowledge_file` | Delete a file (auto L0 sync) |

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

### Why 4 Tiers?

| Tier | Cost | Capacity | Use Case |
|------|------|----------|----------|
| L0 (Index) | Tokens per turn | ~2KB | Quick lookup table |
| L1 (Knowledge) | 1 file read | Unlimited | Structured facts |
| L2 (Skills) | 1 skill load | Unlimited | Procedures |
| L3 (Sessions) | Full search | Unlimited | Historical recall |

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

The 4-tier architecture only works if the agent follows a disciplined decision process. This framework should be injected into the agent's system prompt (or loaded via the `cognitive_decision_prompt` MCP prompt) to ensure consistent behavior.

### Decision Tree

```
Agent encounters a problem or receives a request
  │
  ├─ Step 1: Scan L0 index for relevant domains
  │
  ├─ Step 2: Match found?
  │   ├─ YES → Load the corresponding L1 knowledge file / L2 skill
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
  └─ Step 4: Post-solution evaluation
      Is this worth preserving?
      ├─ YES → Write to L1 (via inject_knowledge) or L2 (skill) for future reuse.
      └─ NO  → Done.
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
You use a 4-tier layered memory system. Before tackling any problem:
1. Check L0 index for matching domains
2. If matched, load and follow L1/L2 before acting
3. If unmatched, solve normally
4. After solving, use inject_knowledge to preserve new knowledge
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

See [CHANGELOG.md](CHANGELOG.md) for the full release history. Recent highlights (currently in `Unreleased`):

- Lifecycle governance with structured actions (`move_to_todo` / `rewrite_as_current_rule` / `keep_with_reason`) and configurable marker lists
- `plan_knowledge_cleanup_tool` and `apply_safe_knowledge_cleanup_tool` (read-only planning, plus dry-run-first application of safe actions)
- Knowledge backups moved out of the live knowledge directory into `data/backups/knowledge/`
- Noise-stripped headings in embedded vector text
- Glob-based catch-all scans (`promotion.watch_patterns`) with English and Chinese defaults

## License

MIT License — see [LICENSE](LICENSE) for details.

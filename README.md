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
- **Keyword Search** — Search with keyword, fuzzy, or BM25/TF-IDF scoring across all L1 files
- **Agent-Agnostic L0 Access** — `get_l0_index` tool lets any MCP agent retrieve the memory index
- **Multi-Agent Namespace** — Isolate knowledge per agent via `LAYERED_MEMORY_NAMESPACE` while sharing common knowledge
- **Session Scanning** — Extract knowledge candidates from recent agent sessions
- **Health Validation** — Check L0-L1 consistency, file structure, and knowledge quality
- **Write Safety** — Automatic `.bak` backup before every file modification
- **Space Analysis** — Monitor memory usage and get optimization suggestions
- **Agent-Neutral** — Works with any MCP-compatible agent (Hermes, Claude, Cursor, etc.)
- **Zero Dependencies** — Core engine uses Python stdlib only; only `fastmcp` for MCP transport
- **Privacy First** — All data stored locally, no external API calls

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
← Checks L0↔L1 consistency, orphaned files, stale entries, file health
```

### 4. Session Compression (Cron Job)

Set up a daily cron to extract new knowledge from conversations:

```
1. scan_recent_sessions → get session summaries
2. AI analyzes summaries → identifies stable facts
3. New facts → written via inject_knowledge (auto L0 sync)
4. L0 index → always up to date
```

### 5. Legacy CRUD (Also Available)

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

### Write Tools (5)

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
| `validate_knowledge` | Health check: L0↔L1 consistency, file quality, duplicates |
| `manage_l0_entry` | Add / remove / replace individual L0 entries |

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

### v0.6.0 — Agent-Agnostic L0, BM25, Namespaces

- **`get_l0_index` tool** — Any MCP agent can now retrieve the L0 index, not just Hermes
- **BM25/TF-IDF search mode** — Better relevance scoring for long-form knowledge files (use `search_mode: "bm25"`)
- **Multi-agent namespace isolation** — Set `LAYERED_MEMORY_NAMESPACE` to isolate per-agent knowledge with shared fallback
- **`.bak` backup** — Automatic backup before every file modification via `inject_knowledge` or `update_knowledge_file`
- **L0 staleness check** — `recall_knowledge` detects stale L0 index and warns with `l0_staleness_warning`
- **Multi-language docs** — Japanese and Korean READMEs synced to v0.6.0

### v0.5.0 — Smart Injection & Auto-Sync

- **`inject_knowledge` tool** — Primary write path with dedup, section targeting, auto L0 sync
- **`sync_l0_index` tool** — Manual L0 index rebuild with dry_run preview
- **`validate_knowledge` tool** — L0↔L1 consistency check, health diagnostics
- **`manage_l0_entry` tool** — Fine-grained L0 entry add/remove/replace
- **Auto L0 sync** — All write tools (create/update/delete/inject) automatically sync L0 index
- **Dedup engine** — SequenceMatcher-based similarity detection with configurable threshold
- **File locking** — fcntl.flock for concurrent write safety
- **Knowledge watcher** — File changes trigger debounced L0 sync (HTTP mode)
- **`cognitive_decision_prompt`** — Built-in decision framework prompt

### v0.4.0 — Initial Release

- 4-tier knowledge architecture (L0/L1/L2/L3)
- Keyword search with relevance scoring
- Session scanning and compression
- MCP protocol support (stdio + HTTP)
- Zero external dependencies (core engine)

## License

MIT License — see [LICENSE](LICENSE) for details.

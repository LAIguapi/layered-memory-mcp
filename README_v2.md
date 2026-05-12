# Layered Memory MCP v2.0

> **Status**: Released  
> **Version**: 2.0.0  
> **Date**: 2026-05-12

---

## What's New in v2.0

### 🎯 Core Improvements

| Feature | v1.x | v2.0 |
|---------|------|------|
| Session reading | Truncated at 50 messages | **Full read** — 0% content loss |
| Knowledge format | Plain markdown | **YAML frontmatter** + markdown |
| Knowledge types | None | **6 structured types** (config/pitfall/decision/preference/fact/procedure) |
| Search | Keyword/BM25 | **Keyword + BM25 + Semantic** (hybrid) |
| Confidence | None | **0-1 scoring** with auto-approval |
| Review queue | None | **Pending → Approved/Rejected** workflow |
| Multi-agent | MCP only | **MCP + REST API** |

### 📦 New Modules

```
src/layered_memory_mcp/
├── models.py                    # KnowledgeEntry, SourceInfo, ReviewItem
├── storage/
│   ├── frontmatter.py           # YAML frontmatter parser
│   ├── l1_store.py              # L1 file storage with frontmatter
│   ├── vector_store.py          # SQLite + sklearn semantic search
│   └── review_queue.py          # Human-in-the-loop review
├── extractor/
│   ├── session_reader.py        # Full session reading (no truncation)
│   ├── knowledge_extractor.py   # Structured knowledge extraction
│   └── confidence.py            # Confidence scoring
└── api/
    └── server.py                # FastAPI REST endpoints
```

### 🛠️ New MCP Tools (7 added, 23 total)

| Tool | Purpose |
|------|---------|
| `extract_session_knowledge` | Auto-extract structured knowledge from sessions |
| `search_semantic` | Vector similarity search |
| `get_pending_reviews` | List items awaiting approval |
| `approve_knowledge` | Approve a pending entry |
| `reject_knowledge` | Reject a pending entry |
| `get_knowledge_by_id` | Lookup by UUID |
| `get_memory_v2_stats` | v2 system statistics |

### 🔧 Usage

```python
# Extract knowledge from recent sessions
extract_session_knowledge(
    days=3,
    auto_approve_threshold=0.9,  # High confidence auto-approves
)

# Semantic search
search_semantic(
    query="How to configure WSL proxy?",
    top_n=5,
)

# Review queue
get_pending_reviews(limit=10)
approve_knowledge(entry_id="...", reviewer="human")
```

### 📊 Test Results

```
110 tests passing (94 v1.x + 16 v2.0)
```

### 🔄 Migration from v1.x

v2.0 is **backwards compatible**:
- v1.x L1 files (no frontmatter) → read as legacy mode
- New writes include YAML frontmatter
- All v1.x MCP tools continue to work

---

## Architecture

```
┌─────────────────────────────────────────┐
│  AGENT LAYER (Hermes / Claude / Cursor) │
├─────────────────────────────────────────┤
│  API Gateway (MCP stdio / REST / HTTP)  │
├─────────────────────────────────────────┤
│  KNOWLEDGE EXTRACTOR (full sessions)    │
├─────────────────────────────────────────┤
│  STORAGE LAYER                          │
│  ├── L0 Index (pointers)                │
│  ├── L1 Files (markdown + frontmatter)  │
│  ├── L1.5 Vector Store (embeddings)     │
│  └── Review Queue (pending/approved)    │
├─────────────────────────────────────────┤
│  RETRIEVAL ENGINE                       │
│  ├── Keyword (exact)                    │
│  ├── BM25 (TF-IDF)                      │
│  └── Semantic (cosine similarity)       │
└─────────────────────────────────────────┘
```

---

## License

MIT

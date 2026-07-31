# Layered Memory MCP — v2.0 Architecture Design

> Status: DRAFT — under review before implementation  
> Version: 2.0.0 (target)  
> Date: 2026-05-12

---

## 1. Problem Statement

Current state (v1.2) is **prototype-grade**. It works for basic read/write but fails at the core promise: **reliable, automated knowledge extraction from sessions**.

### Pain Points

| # | Pain | Impact | Current Workaround |
|---|------|--------|-------------------|
| 1 | `scan_recent_sessions` only reads first 50 messages → loses 76% of long session content | AI makes decisions on incomplete data | None (patched in WIP, not merged) |
| 2 | No structured knowledge schema — everything is free-text markdown | Inconsistent quality, hard to validate | Human review in cron job |
| 3 | No semantic search — only keyword/BM25 | Misses conceptually related knowledge | User must know exact keywords |
| 4 | No versioning or confidence tracking | Can't distinguish "tentative observation" from "verified fact" | None |
| 5 | MCP-only interface — other agents need custom adapters | "Agent-agnostic" is aspirational, not real | None |
| 6 | Cron job fails due to tool call limits | Knowledge compression is unreliable | Manual intervention |

---

## 2. Design Principles

1. **Structured over free-text**: Knowledge must have schema, not just markdown
2. **Extract, don't summarize**: Pull facts/decisions/configs, don't paraphrase
3. **Confidence, not trust**: Every knowledge entry has a confidence score
4. **Agent-agnostic by protocol**: REST API + MCP, not MCP-only
5. **Incremental, not batch**: Real-time extraction beats nightly compression
6. **Human-in-the-loop**: Review queue for uncertain extractions

---

## 3. Target Architecture (v2.0)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           AGENT LAYER                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────────┐ │
│  │   Hermes    │  │ Claude Code │  │   Cursor    │  │    Generic     │ │
│  │   (MCP)     │  │   (MCP)     │  │  (REST/API) │  │    (REST)      │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └───────┬────────┘ │
│         │                │                │                 │          │
│         └────────────────┴────────────────┴─────────────────┘          │
│                                   │                                     │
│                              ┌────┴────┐                                │
│                              │  API    │  ←── FastAPI (new)            │
│                              │ Gateway │      /mcp/stdio (existing)    │
│                              └────┬────┘                                │
└───────────────────────────────────┼─────────────────────────────────────┘
                                    │
┌───────────────────────────────────┼─────────────────────────────────────┐
│                         CORE ENGINE                                  │
│                              │                                       │
│  ┌───────────────────────────┼─────────────────────────────────────┐ │
│  │      KNOWLEDGE EXTRACTOR  │  (replaces session_scanner)        │ │
│  │  ┌─────────────────────┐  │  ┌─────────────────────────────┐   │ │
│  │  │  Session Analyzer   │  │  │  Structured Output Parser   │   │ │
│  │  │  - Full read (not   │  │  │  - Schema validation        │   │ │
│  │  │    truncated)       │  │  │  - Confidence scoring       │   │ │
│  │  │  - Multi-strategy   │  │  │  - Source attribution       │   │ │
│  │  │  - Key decision     │  │  └─────────────────────────────┘   │ │
│  │  │    extraction       │  │                                    │ │
│  │  └─────────────────────┘  │                                    │ │
│  └───────────────────────────┼────────────────────────────────────┘ │
│                              │                                       │
│  ┌───────────────────────────┼─────────────────────────────────────┐ │
│  │      STORAGE LAYER        │                                     │ │
│  │  ┌─────────────┐  ┌──────┴──────┐  ┌─────────────────────┐     │ │
│  │  │  L0 Index   │  │  L1 Files   │  │  L1.5 Vector Store  │     │ │
│  │  │  (pointers) │  │  (markdown) │  │  (embeddings)       │     │ │
│  │  │  ~2KB       │  │  ~4KB each  │  │  SQLite + numpy     │     │ │
│  │  └─────────────┘  └─────────────┘  └─────────────────────┘     │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                              │                                       │
│  ┌───────────────────────────┼─────────────────────────────────────┐ │
│  │      RETRIEVAL ENGINE     │                                     │ │
│  │  ┌─────────────┐  ┌──────┴──────┐  ┌─────────────────────┐     │ │
│  │  │  Keyword    │  │  BM25       │  │  Semantic (cosine)  │     │ │
│  │  │  (exact)    │  │  (TF-IDF)   │  │  (embeddings)       │     │ │
│  │  └─────────────┘  └─────────────┘  └─────────────────────┘     │ │
│  │                                                                   │ │
│  │  Hybrid ranking: combine all three, return best matches          │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                              │                                       │
│  ┌───────────────────────────┼─────────────────────────────────────┐ │
│  │      REVIEW QUEUE         │  (new — human-in-the-loop)          │ │
│  │  ┌─────────────────────────────────────────────────────────┐    │ │
│  │  │  Low-confidence extractions → pending review            │    │ │
│  │  │  Cron job: generate daily digest of pending items       │    │ │
│  │  │  API: approve/reject/modify pending knowledge           │    │ │
│  │  └─────────────────────────────────────────────────────────┘    │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 4. Knowledge Schema (v2.0)

### 4.1 Knowledge Types

```python
class KnowledgeType(str, Enum):
    CONFIG = "config"           # Environment setup, API keys, paths
    PITFALL = "pitfall"         # Things that went wrong and how fixed
    DECISION = "decision"       # Architectural or design decisions
    PREFERENCE = "preference"   # User preferences, habits
    FACT = "fact"               # Verified factual information
    PROCEDURE = "procedure"     # Step-by-step workflows
    RELATIONSHIP = "relationship"  # Connections between concepts
```

### 4.2 Knowledge Entry Schema

```python
class KnowledgeEntry(BaseModel):
    # Identity
    id: str                    # UUID v4
    domain: str                # L1 file name (e.g., "infra")
    section: str               # ## heading (e.g., "本地代理")
    
    # Content
    type: KnowledgeType
    content: str               # Markdown text
    summary: str               # One-line summary for L0
    
    # Metadata
    source: SourceInfo         # Where did this come from?
    confidence: float          # 0.0 - 1.0
    created_at: datetime
    updated_at: datetime
    version: int               # Increment on update
    
    # Relationships
    related_ids: list[str]     # Links to related entries
    tags: list[str]            # Free-form tags
    
    # Review state
    review_status: ReviewStatus  # "approved" | "pending" | "rejected"
    reviewed_by: str | None     # Human reviewer or "auto"
    reviewed_at: datetime | None

class SourceInfo(BaseModel):
    type: str                  # "session" | "manual" | "import" | "migration"
    session_id: str | None     # Original session
    message_range: tuple[int, int] | None  # (start, end) message indices
    extracted_by: str          # Model/agent that extracted
    extracted_at: datetime

class ReviewStatus(str, Enum):
    PENDING = "pending"        # Auto-extracted, awaiting review
    APPROVED = "approved"      # Human or high-confidence auto-approved
    REJECTED = "rejected"      # Human rejected
```

### 4.3 Storage Format

L1 files remain markdown (human-readable, git-friendly), but with **structured frontmatter**:

```markdown
---
id: "550e8400-e29b-41d4-a716-446655440000"
type: "config"
confidence: 0.95
source_type: "session"
source_session: "2026-05-11_abc123"
extracted_by: "gpt-4"
extracted_at: "2026-05-11T03:00:00Z"
version: 1
review_status: "approved"
tags: ["proxy", "wsl", "network"]
---

## 本地代理

HTTP proxy configured at `127.0.0.1:8080` for local external access.
```

---

## 5. API Design

### 5.1 MCP Tools (existing + new)

| Tool | Status | Description |
|------|--------|-------------|
| `recall_knowledge` | ✅ existing | Search L1 by keyword |
| `inject_knowledge_tool` | ✅ existing | Smart write with dedup |
| `batch_inject_knowledge_tool` | ✅ existing | Bulk write |
| `scan_recent_sessions` | 🔄 refactor | Full read + structured extraction |
| `get_knowledge_file` | ✅ existing | Read L1 file |
| `create/update/delete_knowledge_file` | ✅ existing | CRUD |
| `sync_l0_index_tool` | ✅ existing | L0 sync |
| `search_semantic` | 🆕 new | Vector similarity search |
| `get_pending_reviews` | 🆕 new | Get review queue |
| `approve_knowledge` | 🆕 new | Approve pending entry |
| `reject_knowledge` | 🆕 new | Reject pending entry |
| `get_knowledge_by_id` | 🆕 new | Lookup by UUID |
| `get_related_knowledge` | 🆕 new | Follow relationship links |

### 5.2 REST API (new)

```
GET  /health                    → system status
GET  /knowledge                 → list all knowledge files
GET  /knowledge/{domain}        → read specific file
POST /knowledge                 → create new entry
PUT  /knowledge/{id}            → update entry
DELETE /knowledge/{id}          → delete entry
GET  /search?query=...          → hybrid search (keyword + semantic)
GET  /search/semantic?query=... → semantic-only search
GET  /reviews/pending           → get review queue
POST /reviews/{id}/approve      → approve entry
POST /reviews/{id}/reject       → reject entry
GET  /sessions                  → list recent sessions
POST /sessions/{id}/extract     → trigger extraction on session
GET  /stats                     → memory statistics
```

---

## 6. Migration Plan

### Phase 1: Foundation (Week 1)
- [ ] Add knowledge schema models (Pydantic)
- [ ] Add frontmatter parser/generator for L1 files
- [ ] Add `search_semantic` MCP tool (SQLite + numpy, no external deps)
- [ ] Add review queue data model

### Phase 2: Extractor (Week 2)
- [ ] Rewrite `session_scanner` → `knowledge_extractor`
  - Full session read (no truncation)
  - Multi-strategy extraction (decision keywords, tool call patterns, conclusion markers)
  - Structured output (KnowledgeEntry)
  - Confidence scoring
- [ ] Add `extract_session_knowledge` MCP tool
- [ ] Add review queue API endpoints

### Phase 3: REST API (Week 3)
- [ ] FastAPI server with dual transport (MCP stdio + HTTP)
- [ ] All CRUD endpoints
- [ ] Hybrid search endpoint
- [ ] Review workflow endpoints

### Phase 4: Integration (Week 4)
- [ ] Update cron job to use new extractor
- [ ] Add daily review digest
- [ ] Multi-agent test (Hermes + Claude Code + Cursor)
- [ ] Documentation + examples
- [ ] Release v2.0.0

---

## 7. Open Questions

1. **Embedding model**: Use sentence-transformers (local, no API key) or OpenAI embeddings? → **Decision: Start with local (all-MiniLM-L6-v2), make configurable**
2. **Review workflow**: Auto-approve above threshold (e.g., 0.9), queue below? → **Decision: Yes, threshold configurable**
3. **Backwards compatibility**: v1.x L1 files have no frontmatter → **Decision: Graceful read (no frontmatter = legacy mode), new writes include frontmatter**
4. **Namespace isolation**: How do review queues work with namespaces? → **Decision: Per-namespace review queues**

---

## 8. File Structure (v2.0)

```
layered-memory-mcp/
├── src/layered_memory_mcp/
│   ├── __init__.py
│   ├── __version__.py
│   ├── server.py              # MCP tool registration (refactored)
│   ├── api_server.py          # FastAPI REST server (new)
│   ├── config.py              # MemoryConfig (extended)
│   ├── models.py              # Pydantic schemas (new)
│   ├── extractor/             # (new package)
│   │   ├── __init__.py
│   │   ├── session_reader.py  # Full session parsing
│   │   ├── knowledge_extractor.py  # AI-driven extraction
│   │   └── confidence.py      # Confidence scoring
│   ├── storage/               # (new package)
│   │   ├── __init__.py
│   │   ├── l1_store.py        # Markdown + frontmatter I/O
│   │   ├── vector_store.py    # SQLite embeddings
│   │   └── review_queue.py    # Pending review management
│   ├── retrieval/             # (new package)
│   │   ├── __init__.py
│   │   ├── keyword.py         # Existing recall logic
│   │   ├── bm25.py            # Existing BM25
│   │   └── semantic.py        # Vector similarity (new)
│   ├── l0_manager.py          # (refactored)
│   ├── injector.py            # (refactored for schema)
│   ├── memory_compactor.py    # (refactored)
│   ├── recall.py              # (moved to retrieval/)
│   ├── session_scanner.py     # (deprecated → extractor/)
│   └── watcher.py             # (unchanged)
├── tests/
├── examples/
├── docs/
└── README.md
```

---

## Appendix: Confidence Scoring Algorithm

```python
def score_confidence(extraction: KnowledgeEntry) -> float:
    """Calculate confidence score for an extracted knowledge entry."""
    scores = []
    
    # 1. Source quality (0-0.3)
    if extraction.source.type == "session":
        msg_count = extraction.source.message_range[1] - extraction.source.message_range[0]
        if msg_count >= 10:
            scores.append(0.3)  # Long discussion = more context
        elif msg_count >= 5:
            scores.append(0.2)
        else:
            scores.append(0.1)
    
    # 2. Content specificity (0-0.3)
    content = extraction.content
    has_code = "`" in content or "```" in content
    has_url = "http" in content
    has_command = any(cmd in content for cmd in ["$", "python", "npm", "git", "docker"])
    specificity = sum([has_code, has_url, has_command]) / 3 * 0.3
    scores.append(specificity)
    
    # 3. Decision markers (0-0.2)
    decision_words = ["解决", "修复", "确认", "决定", "方案", "结论",
                      "fixed", "resolved", "confirmed", "decided", "solution"]
    has_decision = any(w in content.lower() for w in decision_words)
    scores.append(0.2 if has_decision else 0.0)
    
    # 4. Verification markers (0-0.2)
    verify_words = ["测试通过", "验证", "成功", "worked", "verified", "tested", "confirmed"]
    has_verify = any(w in content.lower() for w in verify_words)
    scores.append(0.2 if has_verify else 0.0)
    
    return min(sum(scores), 1.0)
```

---

*End of Architecture Document*

# Release v2.0.0 — Structured Knowledge Extraction

**Date**: 2026-05-12  
**Commits**: 5 new commits (b9d18d5 → de27869)  
**Tests**: 110 passing (94 v1.x + 16 v2.0)

---

## 🎯 Problem Solved

**Before**: Memory compression cron job failed repeatedly (5 retries → BLOCKED)
- Session scanner truncated at 50 messages (76% content loss)
- No structured knowledge types
- No confidence scoring
- No review mechanism
- Single point of failure (MCP tool timeout)

**After**: Reliable, structured, human-in-the-loop knowledge extraction
- Full session reading (0% loss)
- 6 knowledge types with auto-classification
- Confidence scoring (0-1) with auto-approval
- Review queue for low-confidence items
- Hybrid search (keyword + BM25 + semantic)

---

## 📦 New Features

### 1. Structured Knowledge Schema (`models.py`)
- **6 types**: config, pitfall, decision, preference, fact, procedure
- **Source tracking**: session_id, message_range, extracted_by
- **Confidence**: 0-1 score with multi-factor calculation
- **Review status**: pending → approved/rejected

### 2. Vector Storage (`storage/vector_store.py`)
- SQLite persistence
- sklearn TF-IDF + SVD embeddings (offline, no API)
- Cosine similarity search
- Domain filtering

### 3. Review Queue (`storage/review_queue.py`)
- SQLite-backed pending items
- Approve/reject with notes
- Stats tracking

### 4. Full Session Reader (`extractor/session_reader.py`)
- Reads ALL messages (no truncation)
- JSON + JSONL support
- Role-based filtering (user/assistant/tool/system)
- Tool call tracking

### 5. Knowledge Extractor (`extractor/knowledge_extractor.py`)
- Rule-based type detection
- Domain inference (infra/dev/content/trading)
- Context extraction (±3 messages)
- Confidence scoring

### 6. REST API (`api/server.py`)
- FastAPI endpoints for any agent system
- Knowledge CRUD
- Semantic search
- Review workflow

---

## 🛠️ New MCP Tools

| Tool | Description |
|------|-------------|
| `extract_session_knowledge` | Auto-extract from sessions |
| `search_semantic` | Vector similarity search |
| `get_pending_reviews` | List pending items |
| `approve_knowledge` | Approve entry |
| `reject_knowledge` | Reject entry |
| `get_knowledge_by_id` | UUID lookup |
| `get_memory_v2_stats` | System stats |

**Total tools**: 23 (16 v1.x + 7 v2.0)

---

## 🔧 New Cron Workflow

```
┌─────────────────────────────────────────┐
│  extract_session_knowledge(days=3)      │
│  → Full session read                    │
│  → Structured extraction                │
│  → Confidence scoring                   │
├─────────────────────────────────────────┤
│  High confidence (≥0.9)                 │
│  → auto-approved → batch_inject         │
├─────────────────────────────────────────┤
│  Low confidence (<0.9)                   │
│  → review queue → human approval        │
├─────────────────────────────────────────┤
│  sync_l0_index                          │
│  → update L0 pointers                   │
└─────────────────────────────────────────┘
```

---

## 🧪 Test Results

```bash
$ pytest tests/ -v
============================= 110 passed in 1.38s ==============================
```

---

## 🔄 Backwards Compatibility

- All v1.x MCP tools continue to work
- v1.x L1 files readable (legacy mode)
- New writes include YAML frontmatter
- No breaking changes

---

## 📁 Files Changed

```
ARCHITECTURE_v2.md              (new)
README_v2.md                    (new)
src/layered_memory_mcp/
├── models.py                   (new)
├── storage/
│   ├── __init__.py             (new)
│   ├── frontmatter.py          (new)
│   ├── l1_store.py             (new)
│   ├── vector_store.py         (new)
│   └── review_queue.py         (new)
├── extractor/
│   ├── __init__.py             (new)
│   ├── session_reader.py       (new)
│   ├── knowledge_extractor.py  (new)
│   └── confidence.py           (new)
├── api/
│   ├── __init__.py             (new)
│   └── server.py               (new)
└── server.py                   (+7 tools)
tests/
└── test_v200.py                (new, 16 tests)
```

---

## 🚀 Next Steps

1. **Push to GitHub** (auth pending)
2. **Update cron job** to use `extract_session_knowledge`
3. **Monitor review queue** for first week
4. **Tune confidence threshold** based on results

---

## 📝 Notes

- Vector store uses sklearn (no external API dependencies)
- Semantic search requires ~10 entries before effective
- Review queue starts empty; fills as sessions are processed
- REST API runs on port 8080 by default

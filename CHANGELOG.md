# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

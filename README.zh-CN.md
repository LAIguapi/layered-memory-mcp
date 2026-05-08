# Layered Memory MCP Server

> 用 4 层知识架构突破 AI Agent 的记忆上限。

[**English**](README.md) | [**日本語**](README.ja.md) | [**한국어**](README.ko.md)

[![PyPI version](https://img.shields.io/pypi/v/layered-memory-mcp.svg)](https://pypi.org/project/layered-memory-mcp/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 问题

AI Agent 具有**有限的记忆**——通常每轮对话只有 2-4KB 的持久化上下文被注入。一旦满了，Agent 就会忘记其他所有内容。你无法存储项目配置、用户偏好、API 约定或领域知识，除非不断与空间限制作斗争。

## 解决方案

**Layered Memory** 将知识组织为 4 个层级，用即时性换取容量：

```
┌─────────────────────────────────────────────────────┐
│  L0 — 索引层 (2-4KB, 每轮注入)                       │
│  纯指针："有哪些知识，在哪里"                          │
├─────────────────────────────────────────────────────┤
│  L1 — 知识文件 (无限容量, 按需加载)                   │
│  结构化 Markdown: 配置、约定、事实                     │
├─────────────────────────────────────────────────────┤
│  L2 — 技能层 (需要时加载)                             │
│  操作流程、工作流、工具专有知识                        │
├─────────────────────────────────────────────────────┤
│  L3 — 原始会话 (偶尔搜索)                             │
│  完整对话历史，可按关键词搜索                          │
└─────────────────────────────────────────────────────┘
```

**L0 是你的目录，L1 是你的书架，L2 是你的菜谱，L3 是你的日记。**

## 核心功能

- **智能知识注入** — 一次写入即可见：自动去重、定位章节、同步 L0 索引（支持 upsert/append/merge 模式）
- **关键词搜索** — 支持关键词、模糊、BM25/TF-IDF 三种搜索模式，长文档评分更准确
- **Agent 无关 L0 访问** — `get_l0_index` 工具让任何 MCP agent 都能获取记忆索引
- **多 Agent 命名空间** — 通过 `LAYERED_MEMORY_NAMESPACE` 隔离不同 agent 的知识，同时共享公共知识
- **会话扫描** — 从最近的 agent 会话中提取知识候选项
- **健康验证** — 检查 L0↔L1 一致性、文件结构、知识质量
- **写入安全** — 每次修改文件前自动创建 `.bak` 备份
- **空间分析** — 监控内存使用情况，获取优化建议
- **Agent 无关** — 适用于任何 MCP 兼容的 agent（Hermes、Claude、Cursor 等）
- **零依赖** — 核心引擎仅使用 Python 标准库；仅需 `fastmcp` 用于 MCP 传输
- **隐私优先** — 所有数据存储在本地，无外部 API 调用

## 快速开始

### 安装

```bash
pip install layered-memory-mcp
```

### Hermes Agent

添加到 `~/.hermes/config.yaml`：

```yaml
mcp_servers:
  layered-memory:
    command: layered-memory-mcp
    timeout: 30
```

### OpenClaw

安装 MCP Server，然后注册：

```bash
pip install layered-memory-mcp

# 注册为 MCP Server
openclaw mcp set layered-memory --command layered-memory-mcp
```

Layered Memory 与 OpenClaw 内置的向量记忆互补：
- **OpenClaw 记忆**：基于会话记录的语义搜索（较重，需要嵌入模型）
- **Layered Memory**：基于精选知识文件的结构化关键词搜索（轻量，即时）
- 两者配合使用：OpenClaw 回答"我之前说过什么关于 X 的？"，Layered Memory 回答"数据库连接字符串是什么？"

### Claude Desktop

添加到你的 Claude Desktop MCP 配置中：

```json
{
  "mcpServers": {
    "layered-memory": {
      "command": "layered-memory-mcp"
    }
  }
}
```

### Cursor / 其他 MCP 客户端

```bash
# stdio 模式（默认）
layered-memory-mcp

# HTTP 模式
layered-memory-mcp --transport http --port 8080

# 详细日志
layered-memory-mcp --verbose
```

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LAYERED_MEMORY_HOME` | 记忆数据根目录 | `~/.layered-memory/` |
| `LAYERED_MEMORY_SESSIONS_DIR` | Agent 会话目录（自动检测） | `~/.hermes/sessions/` |
| `LAYERED_MEMORY_AUTO_SYNC_L0` | 写入后自动同步 L0 索引 | `true` |
| `LAYERED_MEMORY_DEDUP_THRESHOLD` | 去重相似度阈值（0.0-1.0） | `0.7` |
| `LAYERED_MEMORY_L0_FORMAT` | L0 索引格式：`hermes` 或 `generic` | `hermes` |
| `LAYERED_MEMORY_NAMESPACE` | Agent 命名空间，用于多 Agent 隔离 | `shared` |

## 使用方法

### 1. 写入知识（推荐）

`inject_knowledge` 是**所有 Agent 的首选写入路径**。一次调用完成去重、分段定位和 L0 索引自动同步。

```
Agent 学到: "生产数据库是 prod-db:5432 上的 PostgreSQL 15"
→ inject_knowledge(
    domain="infrastructure",
    section="Database",
    content="PostgreSQL 15 on prod-db:5432, connection pool: 20 max",
    mode="upsert"
  )
← 创建/更新 infrastructure.md，自动同步 L0 索引
```

**写入模式：**

| 模式 | 行为 |
|------|------|
| `upsert`（默认） | 存在相似内容则替换，否则追加 |
| `append` | 始终追加，跳过去重检查 |
| `merge` | 合并新旧内容中的独特部分 |

### 2. 读取知识

```
Agent: "数据库连接字符串是什么？"
→ recall_knowledge(keyword="database")
← 返回 infrastructure.md 中的相关章节
```

### 3. 健康诊断

```
→ validate_knowledge()
← 检查 L0↔L1 一致性、孤立文件、过期条目、文件健康度
```

### 4. 会话压缩（定时任务）

设置每日定时任务，从对话中提取新知识：

```
1. scan_recent_sessions → 获取会话摘要
2. AI 分析摘要 → 识别稳定的知识点
3. 新知识 → 通过 inject_knowledge 写入（自动同步 L0）
4. L0 索引 → 始终保持最新
```

### 5. 基础 CRUD（同样可用）

直接文件操作：

| 工具 | 说明 |
|------|------|
| `create_knowledge_file` | 创建新的 .md 文件（自动 L0 同步） |
| `update_knowledge_file` | 覆盖已有文件（自动 L0 同步） |
| `delete_knowledge_file` | 删除文件（自动 L0 同步） |

## MCP 工具

### 读取工具

| 工具 | 说明 |
|------|------|
| `recall_knowledge` | 按关键词搜索 L1 知识文件，带相关性评分 |
| `get_knowledge_file` | 按文件名读取指定知识文件 |
| `list_memory_stats` | 获取空间统计、文件大小、优化建议 |
| `scan_recent_sessions` | 扫描最近会话，发现知识提取候选 |
| `search_sessions_by_keyword` | 按关键词搜索会话记录 |
| `get_l0_index` | 获取完整 L0 索引（Agent 无关） |

### 写入工具

| 工具 | 说明 |
|------|------|
| **`inject_knowledge`** | **首选写入路径** — 智能注入：去重、分段定位、L0 自动同步 |
| `create_knowledge_file` | 创建新的 .md 文件（自动 L0 同步） |
| `update_knowledge_file` | 覆盖已有文件（自动 L0 同步） |
| `delete_knowledge_file` | 删除文件（自动 L0 同步） |

### 管理工具

| 工具 | 说明 |
|------|------|
| `sync_l0_index` | 手动从 L1 文件重建 L0 索引（支持 `dry_run` 预览） |
| `validate_knowledge` | 健康检查：L0↔L1 一致性、文件质量、重复检测 |
| `manage_l0_entry` | 单条 L0 条目的增删改 |

## MCP 资源

| 资源 | 说明 |
|------|------|
| `memory://status` | 整体系统状态和配置 |
| `knowledge://files` | 列出所有知识文件及其元数据 |

## MCP 提示

| 提示 | 说明 |
|------|------|
| `knowledge_compression_prompt` | 用于从会话中 AI 驱动提取知识的模板 |
| `cognitive_decision_prompt` | 规范记忆使用的决策框架 |

## 架构深入

### 为什么是 4 层？

| 层级 | 开销 | 容量 | 用途 |
|------|------|------|------|
| L0（索引） | 每轮消耗 Token | ~2KB | 快速查找表 |
| L1（知识） | 1 次文件读取 | 无限 | 结构化事实 |
| L2（技能） | 1 次技能加载 | 无限 | 操作流程 |
| L3（会话） | 全文搜索 | 无限 | 历史回溯 |

### 写即可见流水线（v0.5.0）

v0.5.0 的核心创新是**每条写入路径都会自动同步 L0 索引**：

```
Agent 调用 inject_knowledge(domain="infra", section="Proxy", content="...")
  │
  ├─ 1. 去重检查 (SequenceMatcher, threshold=0.7)
  ├─ 2. 决定动作: upsert / append / merge / skip
  ├─ 3. 分段定位 (找到或创建 ## 标题)
  ├─ 4. 文件写入 (fcntl.flock 并发安全锁)
  └─ 5. 自动 L0 索引同步
        │
        ↓
  L0 索引已更新 → Agent 下一轮即可看到
```

这彻底消除了"写但不可见"的问题——Agent 写入 L1 文件后 L0 索引不更新，导致未来会话忽略新知识。

### 相关性评分

调用 `recall_knowledge` 时，文件按以下规则评分：

1. **文件名匹配**（+10 分）— 关键词出现在文件名中
2. **标题匹配**（+3 分）— 关键词出现在 `## 标题` 中
3. **内容频率**（每次出现 +0.5 分，上限 5 分）— 关键词出现的频率

结果按评分排序，仅返回匹配的 `## 章节`（而非整个文件）。

### 命名空间隔离（v0.6.0）

设置 `LAYERED_MEMORY_NAMESPACE` 可隔离不同 agent 的知识：

```
knowledge/
├── shared/           ← 公共知识，所有 agent 可见
│   ├── infrastructure.md
│   └── coding-standards.md
├── claude/           ← Claude Desktop 私有知识
│   └── claude-specific.md
├── cursor/           ← Cursor IDE 私有知识
│   └── cursor-config.md
└── hermes/           ← Hermes Agent 私有知识
    └── hermes-setup.md
```

每个 agent 先看自己的命名空间，再看 `shared/` 公共区。文件名冲突时命名空间优先。所有读/搜索/注入工具自动合并两个目录。

```bash
# Claude Desktop 配置
LAYERED_MEMORY_NAMESPACE=claude layered-memory-mcp

# 向后兼容：默认 "shared" = 无隔离
layered-memory-mcp
```

### L0 索引格式

支持两种格式：

| 格式 | 示例 | 适用场景 |
|------|------|---------|
| `hermes` | `[L0索引] infra: 服务器, DB → knowledge/infra.md` | Hermes Agent 记忆注入 |
| `generic` | `[infra.md] Server Configuration → proxy, db, deploy` | 独立使用 / 其他 Agent |

通过 `LAYERED_MEMORY_L0_FORMAT` 环境变量或 `l0_format` 构造参数配置。

### 会话压缩

`scan_recent_sessions` 工具专为定时任务自动化设计：

1. 扫描过去 N 天的会话文件
2. 提取用户消息、助手主题和工具调用
3. 返回结构化 JSON 供 AI 分析
4. AI 识别稳定的知识并通过 `inject_knowledge` 写入 L1 文件

这创造了一个**自我改进的记忆系统**——随着更多知识从对话中被提炼出来，Agent 会变得越来越智能。

## Agent 兼容性

Layered Memory 是一个 MCP Server——它适用于任何兼容 MCP 的 Agent。

| Agent | 配置方式 | 备注 |
|-------|---------|------|
| **Hermes Agent** | `config.yaml` → `mcp_servers` | 原生 MCP 客户端，L0 通过 memory 自动注入 |
| **OpenClaw** | `openclaw mcp set` | 与内置向量记忆互补 |
| **Claude Desktop** | `claude_desktop_config.json` | 完整 MCP 支持，L0 通过工具调用 |
| **Cursor** | Settings → MCP | 完整 MCP 支持 |
| **Codex CLI** | Codex MCP 配置 | 完整 MCP 支持 |
| **任何 MCP 客户端** | stdio 或 HTTP 传输 | 标准 MCP 协议 |

### 何时使用 Layered Memory vs. 内置记忆

大多数 Agent 具有**有限的持久化记忆**（每轮 2-4KB）。Layered Memory 通过以下方式解决这个问题：

1. **索引与内容分离** — L0 保持小巧（适合 Agent 记忆），L1 容纳无限知识
2. **按需加载** — Agent 仅在需要时读取所需内容
3. **自我改进** — 会话压缩随时间自动提取新知识

### 集成模式

```
Agent (2KB 记忆上限)
  └── L0 索引 (每轮注入, ~500 bytes)
        ├── [L0] infrastructure: 服务器, DB → knowledge/infrastructure.md
        ├── [L0] api: REST 约定 → knowledge/api-conventions.md
        └── [L0] dev: 代码风格, 测试 → knowledge/development.md
              │
              ↓ (通过 recall_knowledge 按需加载)
        L1 知识文件 (无限, 按关键词加载)
```

## 认知决策框架

四层架构只有在 Agent 遵循严格的决策流程时才能发挥最大价值。此框架应注入 Agent 的系统提示中（或通过 `cognitive_decision_prompt` MCP prompt 加载），以确保行为一致性。

### 决策树

```
Agent 遇到问题或收到请求
  │
  ├─ 步骤 1: 扫描 L0 索引，寻找相关领域
  │
  ├─ 步骤 2: 找到匹配？
  │   ├─ 是 → 加载对应的 L1 知识文件 / L2 技能
  │   │   │
  │   │   ├─ 知识能解决 → 直接使用，禁止凭猜测绕过
  │   │   ├─ 知识部分覆盖 → 先用它解决，再增强该条目
  │   │   └─ 知识不足 → 视作新问题（步骤 3）
  │   │
  │   └─ 否 → 视作新问题（步骤 3）
  │
  ├─ 步骤 3: 作为新问题/新需求处理
  │   使用常规工具和推理解决。
  │
  └─ 步骤 4: 解决后评估
      是否值得保留？
      ├─ 是 → 通过 inject_knowledge 写入 L1 或创建 L2 技能，供未来复用
      └─ 否 → 结束
```

### 为什么这很重要

没有这套决策框架，Agent 容易出现以下问题：
- **忽略已有知识** — 看到 L0 索引却忘记加载 L1 文件，浪费时间猜测
- **重复犯错** — 已解决的问题未被记录，下次从头摸索
- **绕过既有约定** — 每次会话从零开始，而非在积累的知识上构建

此框架将记忆系统从被动存储变为**主动认知循环**：查阅 → 行动 → 学习 → 改进。

### 集成方式

在 Agent 的系统提示中添加：

```
你使用四层分层记忆系统。处理任何问题前：
1. 检查 L0 索引寻找匹配领域
2. 如匹配，先加载并遵循 L1/L2 再行动
3. 如不匹配，正常解决
4. 解决后，通过 inject_knowledge 保存新知识
```

或使用内置 MCP prompt `cognitive_decision_prompt` 在运行时获取完整决策框架。

## 开发

```bash
# 克隆
git clone https://github.com/LAIguapi/layered-memory-mcp.git
cd layered-memory-mcp

# 开发模式安装
pip install -e ".[dev]"

# 运行测试
pytest

# 本地运行
python -m layered_memory_mcp.server
```

## 更新日志

### v0.6.0 — Agent 无关 L0、BM25 搜索、命名空间

- **`get_l0_index` 工具** — 任何 MCP agent 均可获取 L0 索引，不再限于 Hermes
- **BM25/TF-IDF 搜索模式** — 长文档相关性评分更准确（使用 `search_mode: "bm25"`）
- **多 Agent 命名空间隔离** — 设置 `LAYERED_MEMORY_NAMESPACE` 隔离知识，支持共享区回退
- **`.bak` 备份** — `inject_knowledge` 或 `update_knowledge_file` 修改前自动备份
- **L0 过期检测** — `recall_knowledge` 检测 L0 是否过期并返回 `l0_staleness_warning`
- **多语言文档** — 日文、韩文 README 同步至 v0.6.0

### v0.5.0 — 智能注入与自动同步

- **`inject_knowledge` 工具** — 首选写入路径：去重、分段定位、L0 自动同步
- **`sync_l0_index` 工具** — 手动 L0 索引重建，支持 dry_run 预览
- **`validate_knowledge` 工具** — L0↔L1 一致性检查、健康诊断
- **`manage_l0_entry` 工具** — L0 条目精细化管理（增删改）
- **L0 自动同步** — 所有写入工具（create/update/delete/inject）自动同步 L0 索引
- **去重引擎** — 基于 SequenceMatcher 的相似度检测，阈值可配置
- **文件锁** — fcntl.flock 并发写入安全
- **知识监控器** — 文件变化触发 L0 同步（HTTP 模式）
- **`cognitive_decision_prompt`** — 内置决策框架提示

### v0.4.0 — 初始版本

- 四层知识架构（L0/L1/L2/L3）
- 关键词搜索与相关性评分
- 会话扫描与压缩
- MCP 协议支持（stdio + HTTP）
- 核心引擎零外部依赖

## 许可证

MIT License — 详情见 [LICENSE](LICENSE)。

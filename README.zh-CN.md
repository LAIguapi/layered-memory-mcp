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
- **健康验证** — 检查 L0↔L1 一致性、文件结构、知识质量，并附带腐化（rot）与治理摘要
- **生命周期治理** — 识别混在长期知识里的过程状态（TODO / FIXME / next step / pending / 待办 / 下一步 / 待拍板 / 待验证 / 尚未接入 / 未落地 …），给出结构化建议动作：`move_to_todo`、`rewrite_as_current_rule`、`keep_with_reason`；标记词表可配置
- **清理规划** — 只读规划器把问题分为"可安全自动修复"、"需要用户决定"、"禁止自动修复"三类；另一个工具只执行安全动作，且默认 dry-run
- **写入安全** — 整文件改写时的备份存放在知识目录之外（`<LAYERED_MEMORY_HOME>/data/backups/knowledge/`），活动知识目录里只有活动文件
- **更干净的向量召回** — 写入向量的标题会去掉日期、版本号、期号等噪声；展示用标题和元数据保持原样
- **可配置的杂项文件扫描** — 维护/审计扫描按 glob 模式识别"杂项"类文件（`misc`、`notes`、`inbox`、`scratch`、`杂项`、`其它` …），不再要求必须有 `misc.md`
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
| `LAYERED_MEMORY_PROMOTION_WATCH_PATTERNS` | 维护扫描使用的杂项文件 glob 模式（逗号分隔） | 内置中英文列表 |

### config.yaml

大部分行为也可以在 `<LAYERED_MEMORY_HOME>/config.yaml` 里配置。优先级为：构造参数 → 环境变量 → config.yaml → 内置默认值。与知识治理相关的两个段落：

```yaml
governance:
  # 以下列表都是在内置中英文默认值基础上"追加"
  open_markers: ["WIP", "follow-up"]          # 未完成的过程状态 → move_to_todo
  resolved_markers: ["shipped"]                # 已解决的历史记录 → rewrite_as_current_rule
  keep_reason_labels: ["Retention note"]       # 章节中有 "<标签>: <原因>" 行时保留其标记
  disabled_markers: ["TBD"]                    # 关闭对你来说是噪声的默认标记

promotion:
  watch_domains: ["misc"]                      # 精确匹配的杂项文件名（写入时也会检查）
  watch_patterns: ["misc", "misc-*", "notes", "inbox", "scratch", "杂项"]  # 不区分大小写的 glob，仅用于维护/审计扫描
```

不写某个键就保持默认。如果设置了 `watch_patterns`，你的列表会替换内置模式列表；设为 `[]` 可关闭模式匹配。

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
← 检查 L0↔L1 一致性、孤立文件、过期条目、文件健康度，
  并附加 rot_summary / rot_health_score 与 governance_summary
  （L1 中发现的状态标记，按生命周期动作分组）
```

### 4. 规划并执行清理

清理流程把机械修复和语义修改分开处理：

```
1. plan_knowledge_cleanup_tool(max_items=50)
   ← 只读 JSON，分三类：
     safe_auto_fixes      把遗留的 *.md.bak 移出 knowledge/、重新同步 L0、
                          重建向量库、把未完成标记复制到 TODO
     needs_user_decision  改写已解决的历史、合并同主题标题族、拆分过大文件……
     must_not_auto_fix    带有保留原因行、有意保留的标记

2. apply_safe_knowledge_cleanup_tool(actions=<safe_auto_fixes 中的条目>)
   ← 默认 dry-run：只报告将会发生什么，不写入任何内容

3. apply_safe_knowledge_cleanup_tool(actions=..., dry_run=False)
   ← 只执行四种安全动作，其他动作一律拒绝
```

执行工具从不删除或改写 L1 文本。把标记转成 TODO 时，原始行保持不动。合并、改写、删除、跨领域移动交给用户或 agent，通过常规写入工具完成。基于向量的检查（跨文件重复、拆分候选）请使用 `reconcile_knowledge_tool`。

### 5. 会话压缩（定时任务）

设置每日定时任务，从对话中提取新知识：

```
1. scan_recent_sessions → 获取会话摘要
2. AI 分析摘要 → 识别稳定的知识点
3. 新知识 → 通过 inject_knowledge 写入（自动同步 L0）
4. L0 索引 → 始终保持最新
```

### 6. 基础 CRUD（同样可用）

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
| `validate_knowledge` | 健康检查：L0↔L1 一致性、文件质量、重复检测，并附带 rot 评分与治理（状态标记）摘要 |
| `manage_l0_entry` | 单条 L0 条目的增删改 |

### 治理与清理工具

| 工具 | 说明 |
|------|------|
| `audit_rot_tool` | 只读腐化审计：健康分（0-100）、过大文件、损坏标题、过期章节、跨文件重复、杂项文件拆分候选 |
| `reconcile_knowledge_tool` | 基于向量的跨文件重复与拆分候选扫描 |
| `plan_knowledge_cleanup_tool` | 只读清理计划：`safe_auto_fixes` / `needs_user_decision` / `must_not_auto_fix` |
| `apply_safe_knowledge_cleanup_tool` | 只执行计划中的安全机械动作（`migrate_legacy_backups`、`sync_l0_index`、`reindex_vector_store`、`create_todo_from_marker`），默认 `dry_run=True` |

这些工具都是新增的。已有工具的参数和返回字段保持不变；`validate_knowledge` 和写入后的治理报告只是多了字段（例如每条发现的 `action`）。

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

### 长期知识 vs. 过程状态

L1 用来存放**长期知识**，也就是当前成立的规则和事实。未完成的工作（"TODO: 接入 X"、"待拍板"）和"某问题是怎么修好的"这类叙述属于**过程状态**，很快就会过时。写入后以及 `validate_knowledge` 时，治理模块会扫描 L1 中的状态标记，并对每个标记行做分类：

| 动作 | 含义 |
|------|------|
| `move_to_todo` | 未完成的工作，应放进 TODO 存储（`add_todo` / `list_todos`），而不是 L1 |
| `rewrite_as_current_rule` | 已解决的历史（"已修复"、"has been fixed"），应改写为当前成立的规则 |
| `keep_with_reason` | 章节里有明确的 `<保留原因标签>：<原因>` 行，说明这些标记是有意保留的 |

检测是确定性的（正则和字符串匹配，不调用模型），结果只作为建议。标记词表是数据，通过 config.yaml 的 `governance:` 段配置，扩展其他语言或团队风格不需要改代码。

### 派生数据：L0 索引与向量

L1 markdown 文件是唯一的事实来源。L0 索引和向量库（`data/vectors.db`）都是从这些文件**派生**出来的，随时可以重建（`sync_l0_index`、`reindex_vector_store_tool`）。写入向量的文本使用去噪后的标题：`## Release process 2026-10-01 (v3.2)` 会以 `Release process` 嵌入，这样周期性条目按主题而不是日期聚类。展示用标题和存储的元数据仍保留原文。升级后可以重建索引，让已有向量也用上更干净的标题。

### 安全清理边界

自动清理只做机械的、可逆或幂等的动作：把遗留的 `*.md.bak` 从 `knowledge/` 移到 `data/backups/knowledge/`，重建派生的 L0 和向量数据，以及把未完成标记*复制*到 TODO 存储（使用确定性 id，重复执行不会产生新条目）。任何语义层面的修改都在安全边界之外，始终交给用户或 agent 决定：删除事实、合并冲突章节、跨领域移动内容、把历史改写成规则、创建 TODO 后删除原始行。`apply_safe_knowledge_cleanup_tool` 会按名称拒绝这些动作。

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

完整版本历史见 [CHANGELOG.md](CHANGELOG.md)。`3.4.7` 近期要点：

- 生命周期治理：结构化建议动作（`move_to_todo` / `rewrite_as_current_rule` / `keep_with_reason`），标记词表可配置
- `plan_knowledge_cleanup_tool` 与 `apply_safe_knowledge_cleanup_tool`：只读规划，安全动作先 dry-run 再执行
- 知识备份移出活动知识目录，统一放在 `data/backups/knowledge/`
- 向量文本中的标题去除日期/版本等噪声
- 基于 glob 的杂项文件扫描（`promotion.watch_patterns`），内置中英文默认值

## 许可证

MIT License — 详情见 [LICENSE](LICENSE)。

# Layered Memory MCP Server

> 用「框架公共核心 + 代理自有扩展面」的记忆架构突破 AI Agent 的记忆上限。

[**English**](README.md) | [**日本語**](README.ja.md) | [**한국어**](README.ko.md)

[![PyPI version](https://img.shields.io/pypi/v/layered-memory-mcp.svg)](https://pypi.org/project/layered-memory-mcp/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 问题

AI Agent 具有**有限的记忆**——通常每轮对话只有 2-4KB 的持久化上下文被注入。一旦满了，Agent 就会忘记其他所有内容。你无法存储项目配置、用户偏好、API 约定或领域知识，除非不断与空间限制作斗争。

## 解决方案

**Layered Memory** 只拥有一个边界清晰的**公共核心**，其余内容仍归原主：

```
原始会话 / 用户输入 / 工具结果
        │
        ▼
    持久化分类器
        │
        ├─ 框架公共核心（框架可写）
        │    ├─ L0 索引       2-4KB，每轮注入："有哪些知识，在哪里"
        │    ├─ L1 知识       稳定的领域认知：事实、决策、当前状态
        │    ├─ TODO          未完成状态：待办、阻塞、待验证
        │    └─ 记忆变更日志   L0/L1 语义变更的来源记录
        │
        ├─ 代理自有扩展面（只识别与建议，默认不写）
        │    ├─ 技能 (skill)        操作协议、命令序列、排查手册
        │    ├─ 用户画像 (profile)  少量长期有效的用户偏好
        │    └─ 其他代理内容        框架不再进一步分类的代理自有内容
        │
        └─ 不做持久化
             └─ 未决 / 一次性内容（临时输出、原始日志）
```

**L0 是你的目录，L1 是你的书架，TODO 是你的待办盘；原始会话是你的日记 —— 只读证据，框架只检索、从不写入。**

技能和用户画像始终归你所有。框架识别出它们后只返回结构化建议；只有你[显式开启](#可选的代理扩展执行层)后，它才会代为写入。

L0 索引、向量库与搜索索引都是从 L1 Markdown **派生**出来的，随时可重建。

## 核心功能

- **智能知识注入** — 一次写入即可见：自动去重、定位章节、同步 L0 索引（支持 upsert/append/merge 模式）
- **持久化路由** — 确定性分类器（`classify_memory_item`）判断一条内容该去哪个记忆面：L1 知识、TODO、你自己的技能/画像面，还是干脆不保留。只用标记词 + 结构规则，不调用模型、不做向量推断。默认只给建议，为写入结果加注而不改变写入行为
- **能力反射** — `get_memory_surface_capabilities` 按当前配置如实报告：哪些面框架可以代写，哪些面只能给建议
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
| `LAYERED_MEMORY_ROUTING_ENABLED` | 分类内容并为写入结果加注路由信息 | `true` |
| `LAYERED_MEMORY_ROUTING_MODE` | `advisory`（建议）或 `enforce` | `advisory` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_ENABLED` | 允许框架代写代理自有面 | `false` |
| `LAYERED_MEMORY_AGENT_MEMORY_PATH` | 代理自己的记忆文件（显式写入目标） | 自动探测 |
| `LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE` | 允许向自动探测到的记忆文件投射 L0 指针 | `false` |
| `LAYERED_MEMORY_CHANGELOG_ENABLED` | 把 L0/L1 的语义变更记入记忆变更日志 | `true` |
| `LAYERED_MEMORY_CHANGELOG_PATH` | 变更日志文件位置 | `<home>/data/memory_changelog.jsonl` |

### config.yaml

大部分行为也可以在 `<LAYERED_MEMORY_HOME>/config.yaml` 里配置。优先级为：构造参数 → 环境变量 → config.yaml → 内置默认值。与知识治理和路由相关的段落：

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

routing:
  enabled: true
  mode: advisory                               # advisory | enforce（见下）
  # 以下列表同样是在内置词表基础上"追加"，风格与 governance 一致：
  extra_skill_markers: ["操作手册"]
  extra_todo_markers: ["待签字"]
  extra_profile_markers: ["请称呼我"]
  extra_knowledge_markers: ["服务契约"]
  disabled_markers: ["脚本"]                   # 关闭对你来说是噪声的默认词

agent_memory:
  path: null                                   # 代理自己的记忆文件（显式写入目标）
  allow_auto_detect_write: false               # 或显式允许向自动探测到的文件投射

memory_changelog:
  enabled: true                                # 总开关；关掉就什么都不记
  path: null                                   # 默认 <home>/data/memory_changelog.jsonl
  retention_days: null                         # 只报告，不执行 —— 没有任何逻辑会清理它
```

`mode: advisory` 是默认值：路由只在正常结果旁边附上它的判断，不改变写入行为。升级后突然开始拒写，那是数据丢失事故，不是功能，所以 `enforce` 必须显式开启 —— 目前只有一条路径会据此拦截：`compact_memory`（见[压实路径上的路由](#压实路径上的路由)）。`inject_knowledge` 在两种模式下都照常写 L1，并在 `routing_warning` 里说明这一点。

不写某个键就保持默认。如果设置了 `watch_patterns`，你的列表会替换内置模式列表；设为 `[]` 可关闭模式匹配。

`agent_memory:` 决定框架可以把 L0 指针 / 知识索引条目投射到哪个「代理自己的」记忆文件（`MEMORY.md`、`CLAUDE.md` 等）。没配路径时框架会自动探测 —— 用于读取没问题，用于写入就危险了：任何含糊的配置（一个测试、一个建在临时 home 上的试跑脚本）都会解析到真实文件并静默追加。所以投射写入必须满足其中之一：显式目标（`agent_memory.path`，或调用时传 `memory_path`）、显式开关（`allow_auto_detect_write: true`）、探测到的文件就在 `<LAYERED_MEMORY_HOME>` 之内、或者这个 home 确实是本次安装真正配置的 home 而非临时/测试沙箱。其余情况一律拒绝并上报 `agent_memory_write: {status: "skipped", reason: "unsafe_auto_detect_write", detected_path, hint}` —— L1 写入本身永远照常成功，只是投射停下。

### 可选的代理扩展执行层

技能、用户画像以及其他代理自有内容**默认只给建议**：框架完成分类后，返回一个带有你原文和结构提示（`shape_hint`）的 `AgentAction`，自己不写任何东西。如果你希望框架代劳，需要显式开启：

```yaml
agent_extensions:
  enabled: false                 # 总开关（默认关闭）
  mode: suggest                  # suggest | apply_with_confirmation | apply
  adapter: null                  # 你的集成名，例如 generic / command / custom
  surfaces:
    skill:
      enabled: false
      handler: null              # 已注册适配器的名字
    user_profile:
      enabled: false
      handler: null
    agent_other:
      enabled: false
      handler: null
```

框架要代写某个面，四个条件必须同时成立：顶层 `enabled: true`、该面 `enabled: true`、`handler` 指向一个已注册的适配器、并且 `mode` 不是 `suggest`。任何一项缺失或配置写错，都会逐键回退到"只分类、只建议"。**开启它不会转移归属**：`get_memory_surface_capabilities` 仍然把这些面报告为 `owned_by: agent`，只是把 `can_apply` 置为 true —— 框架在那里是编排者，永远不是所有者。

本包不附带任何适配器：在集成方注册适配器（`layered_memory_mcp.adapters`）之前，`apply_agent_action` 一律以 `status: "unsupported"` 拒绝执行。

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
| `classify_memory_item` | 把一条候选内容路由到某个记忆面（`l1_knowledge` / `todo` / `skill` / `user_profile` / `agent_other` / `undecided` / `ephemeral`）。只读、确定性、不调用模型 |
| `get_memory_surface_capabilities` | 按当前配置报告哪些面框架可以代写、哪些只能给建议 |
| `get_memory_changelog` | 读取 L0/L1 语义变更的来源记录（可按 domain / operation / surface / since 过滤；默认倒序、有上限） |
| `get_memory_changelog_stats` | 汇总变更日志：按操作 / 面 / 领域计数、时间跨度、后端配置状态 |

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

### 记忆面模型

| 记忆面 | 归属 | 开销 | 存什么 |
|--------|------|------|--------|
| L0 索引 | 框架 | 每轮消耗 Token | ~2KB 指针："有哪些知识，在哪里" |
| L1 知识 | 框架 | 1 次文件读取 | 稳定的事实、决策、边界、当前理解 |
| TODO | 框架 | 1 次查询 | 未完成状态：待办、阻塞、待验证、待用户拍板 |
| 记忆变更日志 | 框架 | — | L0/L1 语义变更的来源记录 |
| 技能 | **代理** | 1 次技能加载 | 可复用的操作流程、命令序列、检查清单 |
| 用户画像 | **代理** | 随轮注入 | 少量长期有效的用户偏好与人设事实 |
| 其他代理内容 | **代理** | — | 既非技能也非画像的代理自有内容 |
| 原始会话 | **来源方** | 全文搜索 | 只读证据：可检索，永不写入 |

这里有两组关键区分：

- **归属 ≠ 可代写。** 归属是固定的：L0/L1/TODO/变更日志属于框架，技能/画像/其他属于代理，原始会话属于来源方。框架能否**代为写入**代理自有面，是另一个需要显式开启的问题，见[可选的代理扩展执行层](#可选的代理扩展执行层)。
- **真相源 ≠ 派生数据。** L1 Markdown 是唯一真相源；L0 索引、`data/vectors.db` 和搜索索引都由它派生，可随时重建（`sync_l0_index`、`reindex_vector_store_tool`）。

### 记忆变更日志是什么

`MEMORY_CHANGELOG` 是**你的记忆**的变更日志，不是这个软件的发版日志。它只记录持久面的语义变更：L1 章节的新建/更新/合并/替换/删除、融合与合并写回、以及语义地图真的发生变化时的 L0 条目增删改名。

它明确**不是**仓库的 `CHANGELOG.md`，不是原始会话日志，不是通用事件日志，也不是 TODO 的生命周期历史。派生数据重建（向量重建索引、搜索索引）同样不记录 —— 语义内容并没有变。

**存储。** 仅追加的 JSONL，位于 `<home>/data/memory_changelog.jsonl`，一行一条事件。记录是尽力而为的，失败时静默 —— 来源记录绝不能让它所描述的那次写入失败；框架也不会重写或删除任何一行。`retention_days` 会从配置读取并如实报告，但**不会执行**：清理来源记录是破坏性操作，删除历史始终是人工决定。

**一条事件带什么。** 只带结构，不带内容：`schema_version`、`event_id`、`at`（UTC ISO）、`operation`、`surface`、`domain`、`section`、`actor` / `agent_id`、`tool`、结构化的 `summary` 与 `reason`、可选的 `source_ref`、受影响正文的 `before_hash` / `after_hash`，以及一个装字节数和写入细节的 `metadata`。**绝不带正文本身。** 知识文件才是内容的真相源；把正文拷进来源记录，等于给用户写进笔记里的每一个密钥再建一份没人当成敏感文件的副本。`metadata` 只接受标量和短字符串，嵌套容器会被丢弃而不是序列化。

**当前已接入的写入路径。** `inject_knowledge`（因此也覆盖 `compact_memory` 的 L1 写入与 `batch_inject_knowledge_tool`）的新建 / 新建章节 / 更新 / 合并 / 替换 / 融合写回 / 家族合并写回；`create_knowledge_file`、`update_knowledge_file`、`delete_knowledge_file` 的整文件变更；`sync_l0_index` 与 `manage_l0_entry_tool` 中条目真的增删改名的那些变化。

**不记录的。** 延迟融合 / 延迟合并的握手、`fuse_conflict` 或 `consolidate_refused` 的拒绝、跳过的重复内容、参数校验失败、写入失败、`dry_run=True` 的试跑、条目集合没有变化的 L0 重新同步，以及内容哈希没变的写入。单个章节删除暂时没有事件，因为目前没有任何工具只删一个章节 —— 见 `ROADMAP.md`。

**怎么读。** `get_memory_changelog(domain, operation, surface, since, limit)` 返回倒序、有上限的条目（默认 50，硬上限 500）；`get_memory_changelog_stats()` 汇总按操作 / 面 / 领域的计数与时间跨度。两者都是只读。`get_memory_surface_capabilities` 报告的 `memory_changelog` 中，`can_apply` 如实反映后端是否真的启用，并给出解析后的 `store_path`。

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

同一次检查还会报告**流程污染**：那些读起来是操作协议、而不是长期知识的章节。只有标记词**和**结构规则同时命中（编号步骤、检查清单、命令序列、代码块里的命令）才会被标记 —— 只是提到"验证""流程"的架构说明属于知识，不会被误判。被标记的章节应该放到你自己的技能面；框架只报告，不搬运。

### 写入路径上的建议式路由

`inject_knowledge` 写的是 L1，这一点路由不会改变。它增加的是可见性：每个结果都带一个 `routing` 块；如果分类器认为这条内容该去别处，还会带上 `routing_warning`。写入照常发生，`action` 与过去完全一致。批量写入同样逐条暴露 `routing`，批量里的 `deferred_fusion` 也会完整带上握手字段（`needs_fusion`、`expected_hash`、`fusion`）—— 批量只是省事，不是更安静的写入通道。

### 压实路径上的路由

`compact_memory` 现在是路由感知的。它的迁移规则一直是结构性的（"不是 L0 指针" → L1），而那是关于**来源**的判断，不是对内容的分类 —— 于是放在 agent 记忆文件里的操作手册、长期用户偏好、原始日志统统变成了 L1 知识。现在每一条非索引条目都会被分类，而模式决定这个分类算不算数：

| 模式 | 行为 |
|------|------|
| `advisory`（默认） | 迁移行为不变：所有非索引条目仍然进 L1。报告里新增逐条的 `routing`、列出分类器认为该去别处的 `routing_advisory`，以及按面与归属统计的 `routing_summary`，让你看到哪些放错了地方、事后再搬 |
| `enforce` | 只有 `l1_knowledge` 会被写入 L1。TODO、技能、用户偏好、agent 自有、无法判定、一次性内容都**原样留在 agent 记忆里**，并作为候选项返回，由你来安置 |

可以在 config.yaml 的 `routing:` 段设置，也可以单次调用时传 `compact_memory(routing_mode="enforce")` —— 配合 `dry_run=True` 可以先预览拆分结果再决定要不要改配置。不传这个参数就沿用配置里的模式。

enforce 模式不会往 L1 之外写任何东西，被拦下的条目只被报告，绝不删除：

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

每个候选项都带上条目预览、路由到的面与归属、规则权重分、原因、警告、状态，以及真正写入时的目标位置 —— 足够复盘整次压实。两条刻意的边界：不会自动创建 TODO（压实本来就没有写 TODO 的契约，`todo_candidates` 只是报告，`add_todo` 由你决定），技能／用户偏好／agent 自有内容仍然归 agent 所有 —— `suggested_action` 交回给你执行，即使配置了 `agent_extensions` 适配器，压实也绝不会去调用它。

旧字段（`migrated_count`、`migrated`、`errors`、`cleaned_memory`、`stats`）在两种模式下含义不变，已有调用方不需要改动。

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

记忆面模型只有在 Agent 遵循严格的决策流程时才能发挥最大价值。此框架应注入 Agent 的系统提示中（或通过 `cognitive_decision_prompt` MCP prompt 加载），以确保行为一致性。

### 决策树

```
Agent 遇到问题或收到请求
  │
  ├─ 步骤 1: 扫描 L0 索引，寻找相关领域
  │
  ├─ 步骤 2: 找到匹配？
  │   ├─ 是 → 加载对应的 L1 知识文件（涉及操作时，再加载你自己的技能）
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
  └─ 步骤 4: 解决后评估 —— 值得保留吗？该放哪里？
      ├─ 稳定事实 / 决策 / 架构      → 通过 inject_knowledge 写入 L1
      ├─ 未完成的工作（待办、阻塞）   → 写入 TODO store，绝不写进 L1
      ├─ 可复用的流程 / 命令序列      → 写入你自己的技能面
      ├─ 长期有效的用户偏好          → 与用户确认后写入你自己的画像
      ├─ 一次性输出或原始日志        → 不保留
      └─ 拿不准？                   → 调用 classify_memory_item（只读）
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
你使用分层记忆系统。处理任何问题前：
1. 检查 L0 索引寻找匹配领域
2. 如匹配，先加载并遵循 L1 知识再行动
3. 如不匹配，正常解决
4. 解决后分流保存：稳定事实用 inject_knowledge 写入 L1，未完成的工作写入
   TODO store，操作流程与长期用户偏好写入你自己的面；拿不准就调用
   classify_memory_item
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

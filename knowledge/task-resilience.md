# Task-Resilience：AI Agent 任务断点续传框架

## 问题背景
AI Agent 任务流水线（如自动化内容生产、数据采集），遇到 LLM API 429 限流或网络错误时：
- cron 框架标记执行 ok，但实际 AI 调用失败
- 整个流水线从头重跑，已完成步骤被浪费
- 无断点续传，无自动间隔重试

## 设计方案（推荐方案B：Retry Daemon）

### 核心理念
轻量 durable execution for AI Agent — 纯 JSON 文件存储，零外部依赖，对现有 cron 任务零侵入。

### 架构
```
Cron Job (现有，不改)
   ↓ 执行任务
Task-Resilience MCP Server
   ↓ 注册任务 → 记录状态到 JSON
   ↓ 执行步骤 → 每步标记 done/failed
   ↓ 失败 → 保存断点，daemon 接管重试
Retry Daemon (独立进程)
   ↓ 扫描 pending/failed 任务
   ↓ 按策略重试（5min→15min→30min）
   ↓ 超限 → 标记 BLOCKED，发通知
```

### 任务状态机
```
REGISTERED → RUNNING → COMPLETED
                ↓
              FAILED → RETRYING → RUNNING (循环)
                         ↓ (超过5次)
                       BLOCKED → 通知
```

### 存储
- 路径：`~/.task-resilience/tasks/{task_id}_{date}.json`
- 纯 JSON，无数据库
- 每个任务记录：id、pipeline定义、当前步骤、每步状态、错误信息、重试次数、时间戳

### MCP 工具接口
1. `register_task(task_id, pipeline)` — 注册任务，定义步骤列表
2. `complete_step(task_id, step_name, result)` — 标记步骤完成
3. `fail_step(task_id, step_name, error)` — 标记步骤失败，触发重试
4. `get_task_status(task_id)` — 查询任务状态
5. `get_pending_retries()` — daemon 调用，获取待重试任务

### 重试策略
- 退避：5min → 15min → 30min
- 最多 5 次
- 超限标记 BLOCKED，发送通知

### 最佳组合（开源生态调研结论）
```
LiteLLM（429 自动 fallback 到备用模型）
  +
Tenacity（单调用指数退避重试）
  +
自建轻量状态机（pipeline 断点续传，200-300 行代码）
```

## 开源生态定位
填补了一个真实空白 — "轻量 durable execution for AI Agent"：

```
重试库(Tenacity) ──太底层──> 我们的需求 <──太重──> 编排引擎(Temporal/Prefect)
                              ↑
                          这个位置没人做
```

最接近的现有项目：
- Inngest (~7k stars) — 理念最像，但 Go 实现+独立部署
- Temporal (~13k stars) — 功能 100% 匹配，但太重
- Prefect (~18k stars) — 可用但过重
- LangGraph (~10k stars) — 绑死 LangChain

## 设计哲学
和 layered-memory-mcp 一致：纯文件、零外部依赖、单文件可运行。

## 待定决策
1. 单独立项为新仓库，还是集成到 Hermes cron 框架内？
2. 是否先集成 LiteLLM 处理 429，再做断点续传？

## 关联
- layered-memory-mcp 的 L0 索引：knowledge/task-resilience.md
- 关联项目：layered-memory-mcp（设计哲学参考）
- 关联框架：Hermes Agent cron 系统

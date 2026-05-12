# Layered Memory Knowledge Audit — 2026-05-12

## 知识库总览

| 指标 | 数值 |
|------|------|
| 总域名数 | 11 |
| 总文件大小 | 19.5 KB |
| 平均文件大小 | 1.8 KB |
| 最大文件 | chanlun-trading (5.6 KB) |
| 最小文件 | dev-principles (0.6 KB) |

## 域名清单

| 域名 | 类型 | 大小 | 核心内容 |
|------|------|------|----------|
| chanlun-trading | decision | 5.6 KB | 缠论量化交易系统，v10多级别区间套架构 |
| content-pipeline | decision | 1.5 KB | 公众号内容流水线，TrendRadar舆情 |
| cron-jobs | config | 2.3 KB | 15个定时任务，Retry Daemon |
| dashboard | config | 1.1 KB | Hermes Dashboard，FastAPI+React |
| dev-principles | decision | 0.6 KB | 开发原则铁律，6条核心规则 |
| email-delivery | decision | 0.8 KB | 邮件投递架构，deliver.py |
| infra | config | 2.8 KB | WSL代理，GitHub/GitLab，API配置 |
| java-basic | config | 1.9 KB | Spring Boot多租户系统 |
| mingwei-logistics | fact | 0.9 KB | 物流/货代公司业务系统 |
| stock-analysis | fact | 1.1 KB | A股分析系统，数据采集 |
| wechat | config | 0.9 KB | 公众号配置，API注意事项 |

## 知识类型分布

- **config**: 8 个域名（基础设施配置）
- **decision**: 9 个域名（原则、决策）
- **fact**: 9 个域名（项目信息）
- **procedure**: 6 个域名（操作流程）
- **pitfall**: 5 个域名（踩坑经验）
- **preference**: 4 个域名（偏好设置）

## v2.0 迁移状态

| 检查项 | 状态 |
|--------|------|
| 文件格式 | v1.x 纯 markdown（无 frontmatter） |
| 向量存储 | 空（待首次 semantic search 时构建） |
| 审核队列 | 空（待首次 extraction 时填充） |
| 兼容性 | ✅ 向后兼容，新写入自动加 frontmatter |

## 关键记忆摘要

### 基础设施 (infra)
- WSL HTTP代理: 127.0.0.1:20172
- GitHub双Token: GitLab(laiguapi) / GitHub(LAIguapi)
- 开发邮箱: 1361984065@qq.com
- 邮件配置: ~/.config/himalaya/config.toml

### 开发项目
- **stock-analyse**: /root/stock-analyse/ (Java Spring Boot, 10255端口)
- **chanlun_quant**: ~/chanlun_quant/ (实盘bot + v10-v12引擎)
- **java-basic**: /root/java-basic/ (Spring Boot 3多租户)
- **dashboard**: /root/hermes-dashboard/ (FastAPI+React, 9002端口)

### 内容管线
- 公众号「今日发现」: AppID wx8537c71cb8641690
- 日更: 21:00, 周更: 深度体验(周日/一/三/五 20:30)
- 美股早报: 工作日 06:00
- A股分析: 周六 09:00

### 开发原则
1. 先查 skills_list，不跳过直接写代码
2. 修改前描述方案并征得同意
3. 所有邮件走 deliver.py
4. 代码开发委派给 Claude Code
5. 第三方对接优先查官方文档

## 待审核队列

当前为空。下次 `extract_session_knowledge` 运行后将填充。

## 建议

1. 运行一次 `extract_session_knowledge(days=7)` 填充向量存储
2. 检查审核队列中的低信心条目
3. 考虑将大文件（chanlun-trading 5.6KB）拆分为多个 domain

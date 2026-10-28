# Layered Memory Graph v1 迁移工具验收记录

状态：隔离样本验收通过；不代表生产迁移或默认模式切换
版本：3.6.0（迁移工具验收记录）
范围：`python -m layered_memory_mcp.graph_migration`

## 1. 结论

独立迁移 CLI 已在中性隔离样本上完成预演、应用、核验、幂等恢复和安全边界验收。

已验证：

- 默认不带 verb 的调用是只读 `plan`，不会创建目标目录。
- `plan`、`apply`、`verify`、`rollback` 四个操作均有明确退出码和 JSON 报告。
- 应用前会构建并核验私有 staged generation，随后以一次目录 rename 发布。
- 源 Markdown、源 SQLite、源 changelog 不被修改；源文件字节被保留在 generation backup 中。
- 相同源的重复 apply 是幂等 no-op；源或目标发生变化时拒绝继续。
- verify 是只读核验，不修复、不创建缺失结构。
- rollback 通过 rename 保留 withdrawn generation，不物理删除任何迁移产物。
- 不兼容的 L1 分段语料在 plan/apply 前 fail closed，目标保持不存在，源保持不变。
- frontmatter 显式 ID 保留为向量/图身份，但外部拥有的可读 surface 不被迁移框架接管。
- 缺失的源记录时间使用固定哨兵，不把迁移时刻写入 bootstrap event payload；显式 authored `source.extracted_at` 保留。
- manifest 的 `source_files` 会与 generation 内冻结的 `backup/` 逐项重算核对；篡改声明即 fail closed，即使同步重签 manifest digest 也不能绕过。

未验证、也未声称完成：

- 真实 `/root/.layered-memory` 或其他用户记忆目录迁移。
- 生产 graph population、默认 graph mode 切换、服务重启或 cutover。
- 大规模真实语料的性能、p95、容量和检索质量门槛。
- 跨文件系统 rename 的原子性；工具只依赖同一文件系统内的目录 rename 语义。

## 2. 实现边界

迁移工具是独立模块，不替代旧的 `domain_keywords` 配置迁移命令：

- CLI：`src/layered_memory_mcp/graph_migration.py`
- 只读源盘点与兼容预检：`src/layered_memory_mcp/graph_migration_inventory.py`
- MemoryEvent bootstrap：`src/layered_memory_mcp/graph_migration_bootstrap.py`
- staged generation、发布、核验、恢复：`src/layered_memory_mcp/graph_migration_generation.py`
- 行为测试：`tests/test_v361_graph_migration.py`
- 真实进程 CLI 测试：`tests/test_v361_graph_migration_cli.py`

入口要求显式提供 `--source` 和 `--destination`。工具不读取默认 home、配置文件、环境变量中的 store 路径，也不连接运行中的服务。

## 3. 隔离样本生命周期证据

父流程独立探针：本地 scratch 证据文件 `migration-five-gates-final.json`

关键结果：

| 门槛 | 结果 | 证据 |
|---|---|---|
| plan 前目标不存在 | 通过 | `pre_destination_absent: true` |
| plan 零写 | 通过 | `destination_absent_after: true`、`source_unchanged: true` |
| frontmatter 外部 ownership | 通过 | `external_surfaces: [entry-pipeline-0001]`、`surface_registered: false` |
| 显式 ID 的 graph/vector 对齐 | 通过 | `vector_seed_matches: true` |
| 源只读 | 通过 | apply 后 `source_unchanged_after_apply: true` |
| bootstrap 确定性 | 通过 | 两次 report 和 event payload 均相等 |
| verify | 通过 | `verify_status: migration_verified`、`verify_ok: true`、`source_files_ok: true` |
| rollback | 通过 | current 消失、withdrawn path 存在、源仍未变化 |

补充完整生命周期证据：本地 scratch 证据文件 `migration-parent-acceptance-probe.json`。
该探针还覆盖了重复 apply、重复 rollback、源 tree state 前后相等和 frontmatter ownership 保留。
当前 HEAD 的独立回归还确认：同一源快照的重复 `plan_migration()` bootstrap 报告稳定；重签后篡改 `source_files` 会被核验拒绝。

## 4. 不兼容语料的 fail-closed 证据

独立预检触发结果：本地 scratch 证据文件 `migration-preflight-independent-final.json`

测试了两种 reader 分段不一致的文档：

1. 重复 H2，第一段为空：`## Same` / `## Same`。
2. tab 分隔标题：`##\tSecond`。

两种样本均得到：

- `status: migration_blocked`
- `exit_code: 1`
- `l1_section_reader_incompatible:retrieval:canonical_body_differs`
- `l1_section_reader_incompatible:retrieval:canonical_section_unprojected`
- plan 前目标不存在，plan 后目标仍不存在
- 源快照 byte-for-byte 未变化

处置策略是拒绝整体迁移，而不是在已验收的两个 reader 之间擅自选择或改写其中一个。人工修复源文件后再重新 plan。

## 5. 五项门槛审计

### 5.1 显式 frontmatter ID 与外部所有权

frontmatter 条目的显式 ID `entry-pipeline-0001` 被保留为 entry/vector identity，并映射到稳定的 legacy claim node。它被列入 `external_surfaces`，但没有写入 journal surface registry。

这意味着迁移可以保留并核对该对象，却不会声称拥有外部维护的可读文档 surface，也不会在后续 projection replay 中静默覆盖它。

依据：

- `graph_migration_bootstrap.py` 的 `external_surfaces`、`bindings` 构建逻辑。
- `tests/test_v361_graph_migration.py::test_apply_preserves_the_frontmatter_identity_verbatim`。
- `tests/test_v361_graph_migration.py::test_apply_reports_an_externally_owned_frontmatter_surface`。
- 独立探针中的 `surface_registered: false` 与 `vector_seed_matches: true`。

### 5.2 两 reader 分段一致性

迁移盘点同时比较实际 vector projection producer 与 canonical readable-surface reader。发现空 H2 和 tab 标题会产生不同 occurrence/body 结果，因此在生成前阻断，不让不一致进入 graph/vector/L1 协调发布。

依据：

- 独立预检 JSON。
- CLI 真实进程用例 `test_an_incompatible_document_blocks_the_cli_and_publishes_nothing`。

### 5.3 bootstrap surface binding 与恢复能力

普通 Markdown H2 section 才建立 `ProjectionBinding`，binding 的 body hash 在 staged build 与 verify 阶段重新读取核对。bootstrap journal 写入完成后，graph 从持久化 journal replay，而不是从 planning store 直接复制。

发布 generation 后，journal、graph、L1、vector、L0 的 checkpoint 和 hash 都会进入 manifest；verify 会逐项回读。rollback 只 rename generation 到 `rollback/withdrawn-*`，不删除 source backup、journal、graph、vector 或 manifest。

依据：

- `graph_migration_generation.py::build_generation`、`verify_generation`、`withdraw_generation`。
- 独立生命周期探针 `verify_ok: true`、`withdrawn_path_exists: true`。
- 全量迁移测试中的 journal replay、surface、vector、L0、rollback 和 tamper refusal 用例。

### 5.4 缺失时间的确定性

普通 Markdown 条目没有 authored recording time 时，bootstrap 使用固定 `MISSING_RECORDED_AT`：

`1970-01-01T00:00:00+00:00`

同时在 report provenance 中记录 `claim.recorded_at:missing_source` 和 warning。该哨兵不是源事实，只是对“源未提供记录时间”的稳定、可审计表达。

frontmatter 条目的 `created_at`/`updated_at` 继续保留其 authored 语义；只有未显式 authored 的 `source.extracted_at` 才补哨兵；显式 `source.extracted_at` 原样保留。

独立探针确认：

- 两次 bootstrap report 相等。
- 两次 event payload 相等。
- `recorded_at` 固定为上述哨兵。
- authored source extraction time 的专门测试通过。

审查处置：早期只读审查曾指出 `plan_migration()` 可能把 wall-clock 传入 bootstrap；当前 HEAD 已由固定 `MISSING_RECORDED_AT` 哨兵覆盖，独立重复 plan 探针确认 bootstrap 报告稳定。迁移时间只在 generation manifest 的 `published_at` 中记录，不进入 bootstrap event payload。

### 5.5 source_files provenance 核验

`manifest.source_files` 不是只读声明：verify 会把它与 `backup/` 中的冻结源文件集合、大小和 SHA-256 逐项比较，并拒绝越界路径、缺失文件和多余文件。该检查独立于 manifest 自身 digest，因此“篡改 source_files 后同步重签 manifest”仍然得到 `source_file_hash_mismatch` / `source_file_size_mismatch`，不会报告 verified。

依据：

- `graph_migration_generation.py::_verify_source_files` 与 `VerificationReport.source_files_ok`。
- `tests/test_v361_graph_migration.py::test_verify_detects_tampered_source_file_provenance`。

### 5.6 源 SQLite 只读

源 vector database 的盘点走 SQLite URI：`mode=ro&immutable=1`，不使用会在构造时 mkdir、建表或 checkpoint WAL 的 `VectorStore` 写入口。迁移只在 staged generation 的副本上通过受控 metadata-only backfill 更新 graph seed metadata；不会删除旧 vector row、重嵌入文本或原地改写源数据库。

独立探针和完整测试均确认 apply 后源 tree 未变化。源存在非空 WAL、symlink、未知记录或无法解析的对象时，盘点会报告阻断而不是猜测或跳过。

## 6. 静态安全审计

落盘证据：本地 scratch 证据文件 `migration-static-audit-final.json`

对四个迁移生产模块进行 AST 解析和逐行扫描，结果：

- 无 `subprocess`、`requests`、`urllib` 网络/外部进程调用。
- 无 `write_bytes`、脚本式源码替换或删除操作。
- 无读取真实记忆目录、凭证或其他 profile 的默认路径。
- 四个模块语法解析均通过。
- `expanduser()` 只用于 operator 明确传入的 source/destination 参数规范化，不参与默认路径发现。
- `datetime.now()` 只出现在 generation manifest 的发布时间和 rollback/debris 目录 stamp；不进入 bootstrap event payload。bootstrap 的记录时间由固定哨兵或显式调用方参数决定。

迁移模块只在显式 source/destination 下工作；测试使用 `tmp_path`、重定向 HOME 和 deterministic fake embeddings。

## 7. 真实执行结果

### 聚焦迁移测试

证据：本地 scratch 证据文件 `migration-focused-final-rerun.txt`

```text
86 passed in 11.13s
exit=0
```

### 系统解释器全量回归

证据：本地 scratch 证据文件 `migration-full-usr-final-rerun.txt`

```text
2180 passed in 33.17s
exit=0
```

该解释器包含 REST 测试依赖，因此没有用 skip 代替通过。

### 默认解释器全量回归

证据：本地 scratch 证据文件 `migration-full-default-final-rerun.txt`

```text
2154 passed, 17 skipped in 35.89s
exit=0
```

17 个 skip 来自默认环境缺少可选依赖；系统解释器的 2180 passed / 0 skipped 才是本批完整回归结论。

### 编译与空白检查

- 六个迁移源/测试文件 `compileall`：exit 0。
- 六个文件 `git diff --no-index --check`：均无空白错误；命令对未跟踪文件返回预期的 no-index code 1，汇总见 `migration-diffcheck-summary.txt`。
- CLI help 在显式源码路径下成功，列出 `plan`、`apply`、`verify`、`rollback`，并说明默认 plan、JSON stdout 和退出码。

## 8. 运行和恢复口径

只允许对复制出的隔离快照执行：

```text
PYTHONPATH=src python -m layered_memory_mcp.graph_migration \
  --source <explicit-old-snapshot> \
  --destination <explicit-destination>
```

默认调用只读 plan。确认报告无 blocking findings 后，才由操作方显式执行 `apply`。完成后执行 `verify`；需要撤回时执行 `rollback`，再读取 returned `withdrawn_to` 路径核对保留的 generation。

任何 source fingerprint drift、destination tamper、manifest mismatch、reader incompatibility 或 unresolved identity 都必须停止，不得用 `--force` 绕过；当前 CLI 没有提供这种绕过开关。

## 9. 当前未处理项

- 生产数据迁移仍冻结，真实目录未读取、未写入、未枚举。
- 默认 `graph_mode` 仍为 `legacy`。
- `pyproject.toml` 尚未新增 graph migration console script；当前稳定入口是显式的 `python -m ...`。
- 仓外 Agent 工作目录中的越界记忆文件不属于本批迁移产物，本批未删除或修改；需要单独授权和单独审计。
- 本文档记录的是本地隔离能力验收，不是生产 cutover runbook，也不构成“已迁移真实记忆”的声明。

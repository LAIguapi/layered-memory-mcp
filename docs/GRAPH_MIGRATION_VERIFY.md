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

- 任何真实生产用户记忆目录（operator 自有的真实 memory store 路径）的迁移。
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

## 3. 隔离样本生命周期验收

迁移 CLI 在中性隔离样本上完成了完整生命周期验收。验收不导入测试
fixture 或测试断言，而是构造独立的 Markdown、SQLite 向量库和 changelog，
通过真实迁移模块与 `VectorStore` 写入路径检查行为。

关键结果：

| 门槛 | 结果 |
|---|---|
| plan 前目标不存在 | 通过：`migration_planned` |
| plan 零写 | 通过：`dry_run_zero_write: true` |
| apply 验证与源只读 | 通过：`apply_verified_and_source_unchanged: true` |
| 重复 apply 幂等零写 | 通过：`migration_already_published`、`idempotent_zero_write: true` |
| verify 只读 | 通过：`migration_verified`、`verify_read_only: true` |
| rollback 保留产物 | 通过：`migration_rolled_back`、`rollback_retains_artifacts: true` |
| 重复 rollback 幂等零写 | 通过：`migration_rollback_nothing_published` |
| 篡改 generation 拒绝 | 通过：`migration_verify_failed` |
| 源漂移拒绝 | 通过：`migration_source_drift` |
| 未知目标内容零写拒绝 | 通过：`migration_destination_refused`、拒绝时零写 |

上述结果来自隔离验收记录；记录文件属于发布工作区，不随公开仓库分发。

## 4. 不兼容语料的 fail-closed 证据

独立预检结果：两种 reader 分段不一致的文档均在 plan/apply 前被阻断，目标
保持不存在，源保持不变。

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
- 隔离集成检查确认 `surface_registered: false` 与 `vector_seed_matches: true`。

### 5.2 两 reader 分段一致性

迁移盘点同时比较实际 vector projection producer 与 canonical readable-surface reader。发现空 H2 和 tab 标题会产生不同 occurrence/body 结果，因此在生成前阻断，不让不一致进入 graph/vector/L1 协调发布。

依据：

- 隔离预检确认不一致输入被阻断。
- CLI 真实进程用例 `test_an_incompatible_document_blocks_the_cli_and_publishes_nothing`。

### 5.3 bootstrap surface binding 与恢复能力

普通 Markdown H2 section 才建立 `ProjectionBinding`，binding 的 body hash 在 staged build 与 verify 阶段重新读取核对。bootstrap journal 写入完成后，graph 从持久化 journal replay，而不是从 planning store 直接复制。

发布 generation 后，journal、graph、L1、vector、L0 的 checkpoint 和 hash 都会进入 manifest；verify 会逐项回读。rollback 只 rename generation 到 `rollback/withdrawn-*`，不删除 source backup、journal、graph、vector 或 manifest。

依据：

- `graph_migration_generation.py::build_generation`、`verify_generation`、`withdraw_generation`。
- 隔离生命周期检查确认 `verify_ok: true`、`withdrawn_path_exists: true`。
- 全量迁移测试中的 journal replay、surface、vector、L0、rollback 和 tamper refusal 用例。

### 5.4 缺失时间的确定性

普通 Markdown 条目没有 authored recording time 时，bootstrap 使用固定 `MISSING_RECORDED_AT`：

`1970-01-01T00:00:00+00:00`

同时在 report provenance 中记录 `claim.recorded_at:missing_source` 和 warning。该哨兵不是源事实，只是对“源未提供记录时间”的稳定、可审计表达。

frontmatter 条目的 `created_at`/`updated_at` 继续保留其 authored 语义；只有未显式 authored 的 `source.extracted_at` 才补哨兵；显式 `source.extracted_at` 原样保留。

隔离重复运行确认：

- 两次 bootstrap report 相等。
- 两次 event payload 相等。
- `recorded_at` 固定为上述哨兵。
- authored source extraction time 的专门测试通过。

审查处置：早期只读审查曾指出 `plan_migration()` 可能把 wall-clock 传入 bootstrap；当前实现已由固定 `MISSING_RECORDED_AT` 哨兵覆盖，隔离重复 plan 检查确认 bootstrap 报告稳定。迁移时间只在 generation manifest 的 `published_at` 中记录，不进入 bootstrap event payload。

### 5.5 source_files provenance 核验

`manifest.source_files` 不是只读声明：verify 会把它与 `backup/` 中的冻结源文件集合、大小和 SHA-256 逐项比较，并拒绝越界路径、缺失文件和多余文件。该检查独立于 manifest 自身 digest，因此“篡改 source_files 后同步重签 manifest”仍然得到 `source_file_hash_mismatch` / `source_file_size_mismatch`，不会报告 verified。

依据：

- `graph_migration_generation.py::_verify_source_files` 与 `VerificationReport.source_files_ok`。
- `tests/test_v361_graph_migration.py::test_verify_detects_tampered_source_file_provenance`。

### 5.6 源 SQLite 只读

源 vector database 的盘点走 SQLite URI：`mode=ro&immutable=1`，不使用会在构造时 mkdir、建表或 checkpoint WAL 的 `VectorStore` 写入口。迁移只在 staged generation 的副本上通过受控 metadata-only backfill 更新 graph seed metadata；不会删除旧 vector row、重嵌入文本或原地改写源数据库。

完整测试与隔离检查均确认 apply 后源 tree 未变化。源存在非空 WAL、symlink、未知记录或无法解析的对象时，盘点会报告阻断而不是猜测或跳过。

## 6. 静态安全审计

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

聚焦迁移回归：`107 passed`，退出码为 0。

### 独立隔离验收

独立隔离验收通过；其记录属于发布工作区，不随公开仓库分发。

该验收在隔离 HOME、XDG 缓存目录和离线模型标志下运行；向量使用
确定性本地 embedding seam，不下载模型，也不连接真实服务。

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

- 生产数据迁移仍冻结。本记录早期隔离验收未访问真实目录；后续获批的真实副本严格预演安全拒绝，未 apply。身份桥接实现与验收只使用中性样本，未访问或修改生产目录。
- 默认 `graph_mode` 已切换为 `graph`；显式 `legacy` 仍是回滚/兼容模式，不代表已完成生产迁移或 cutover。
- `pyproject.toml` 已提供 `layered-memory-update` 更新入口；迁移模块入口 `python -m layered_memory_mcp.graph_migration` 仍可用。
- 仓外 Agent 工作目录中的越界记忆文件不属于本批迁移产物，本批未删除或修改；需要单独授权和单独审计。
- 本文档记录的是本地隔离能力验收，不是生产 cutover runbook，也不构成“已迁移真实记忆”的声明。

## 10. Batch 2：旧向量身份桥接验收

范围：仅迁移 inventory、generation 和新增身份桥接测试。已有 row-ID
backfill、entry-ID producer、metadata-only 原子更新机制保持不变。

- 无法由行自身 ID 解析的合法旧行，只有 domain、声明的 metadata section、
  UTF-8 projection body 完全一致且 section 内候选唯一时才自动映射。
- 不做空白、大小写或 Unicode 归一化，不使用相似度；重复 section、重复目标
  claim、已占用目标、畸形或冲突 graph metadata 均阻断，不丢弃旧行。
- 显式 operator map 与自动 map 分别记录来源；manifest 带逐行来源证据。
  verify 从冻结 backup 重算盘点、映射、seed 和 metadata，拒绝伪造来源、
  候选数、目标或碰撞声明，即使重新计算 manifest digest。
- 只更新 staged generation 的 metadata，保留原 row ID/domain/text/vector，
  保留无关 metadata，冻结 backup 与源内容一致。
- I/O 继续使用原有普通文件读取、SQLite `mode=ro&immutable=1` 与 copyfile；
  不引入 Linux noatime/memfd、SQLite deserialize 或整库内存镜像要求。
  零写断言覆盖成员、内容、inode/mode/size/mtime/ctime，不把读产生的 atime
  更新当作写入，也不主动恢复时间戳。

验收执行结果：

- 六文件聚焦回归：383 passed，0 skipped；其中新增 identity 测试 83 项。
- 独立中性验收：36 项检查通过。验收手写 SQLite 旧格式输入，不导入测试
  或 fixture；证明随机旧 ID 确实进入 automatic/explicit 两条分支。
- 隔离检查在 apply 前断言 plan 零写；执行 apply → verify → 重复 apply 无写 →
  retained rollback，逐行比较 text/vector 载荷和完整 metadata，确认源不变。
- 阴性对照：错 domain、缺 section、空白差异、重复 claim、畸形 graph 字段、
  冲突 graph 字段、已占用目标均阻断，行计数平衡且源/目标零写。
- 全量隔离离线回归：2569 passed、16 skipped、4 failed。
  四项均依赖缺失的 embedding 模型；未安装模型、未用 fake 结果替代全量结果。
  首轮另外一项 ephemeral-home 失败在未改代码的基线复现；将 basetemp 放进
  TMPDIR 后通过，无产品代码或原测试变更。
- compile 与 diff-check：exit 0。原有测试文件未修改。
- 测试仅实测 Linux/Python 3.12；未宣称原生 Python 3.10、Windows 或 macOS 已运行通过。

结论仅为身份桥接隔离验收通过。真实语料 reader 兼容性诊断、完整生产副本
演练、默认切换、update 接入、服务激活与远端发版仍未完成。

## 11. Batch 3：L0 evidence-only 边界与窄兼容回归

本批把 `L0.md` 的迁移语义固定为 **evidence-only**：快照适配器可以把它
复制到 `evidence/L0.md`，用于源文件 provenance、hash 和零写核验；它不是
旧格式 L1 知识输入，不参与 section projection、graph node bootstrap、vector
identity bridge 或可读 surface binding。因而在直接对旧格式源执行 migration
inventory/plan 时，`source_unsupported_record:L0.md` 是预期的阻断 finding，
不得通过删除 L0、把它伪装成 L1、关闭 `section_reader_findings()` 或放宽
source allowlist 来消除。

该边界的验收要求是：一个除此之外完整、结构合法的中性旧格式源，只加入
`L0.md` 后，plan 仍然 fail closed；目标目录不创建，源成员、字节和 mtime
不变，报告不包含 L0 正文。snapshot adapter 的正向 evidence 复制测试与
migration planner 的负向阻断测试分别覆盖这两个不同语义，不能用其中一个
替代另一个。

本批窄兼容契约已先文档化并提交：

- `139107d docs: specify legacy embedding projection compatibility`
- 仅接受 `embedding_heading(section) + "\\n" + canonical body` 的精确 UTF-8
  前缀；不接受任意前缀、空白/大小写/Unicode 归一化、尾随字节、子串或相似度。
- `4a72bb4 feat: bridge exact legacy embedding projections` 只在 domain、声明
  section 和 section 内唯一候选门禁通过后建立 derived map。

隔离验收结果：

- 身份桥接 focused：18 passed；身份测试文件：115 passed。
- 迁移集合（migration、CLI、snapshot、identity）：314 passed。
- 独立反向验收通过：自建中性 Markdown 和手工旧 schema SQLite，不复用测试
  fixture，覆盖 plan 零写、精确前缀映射、变体拒绝、apply、verify、重复 apply
  no-op 和 rollback。
- 契约化快照重跑：590/590 vector rows 自动精确映射，`orphaned=0`；唯一
  finding 为预期的 `source_unsupported_record:L0.md`。这证明 L0 evidence-only
  阻断仍有效，同时证明窄兼容没有把 590 条旧向量行误留为 orphan。

本批仍不代表生产迁移、默认 graph mode、服务激活、update 接入或远端发版已完成。

## 12. Batch 4：图架构收尾（本地实现，未经生产验证）

状态：在实现中完成并本地验证；**不**代表生产迁移、安装态服务激活或发版完成。
需进行独立复验。

### 12.1 五项收尾内容

1. **快照源分类**（`graph_migration_snapshot.py`）
   - 本项目自己写在 home 下的运维状态进入**有类型的已知排除表**：
     `.gitignore`、`.last_auto_compact`、`.last_auto_compact_attempt`、
     `.last_l1_dedup`、`write_guard_state.json`。只按 metadata 分类，不打开、
     不遍历。
   - 非本项目拥有的 root 成员，只能通过**逐次显式声明**覆盖：
     `--exclude NAME`（可重复）。每个名字必须是单个字面 root 成员，不得是
     保留名、不得含通配符、不得重复、且必须真实存在；声明了但不存在会报
     `operator_exclusion_absent`，所以过期清单无法预先放行未来的未知成员。
     声明不能把 symlink/特殊文件洗白。
   - `knowledge/` 下的空命名空间子目录：**先证明为空**（只列名字，不读载荷）
     才接受，并记入报告与 manifest 的 `empty_namespaces`。非空命名空间仍然
     阻断（`knowledge_directory_uncovered`），且不进入、不读取。
   - 名字以 `.md` 结尾的目录仍然是 `source_wrong_type`，不会被当成命名空间。
   - 公开源码中不含任何安装相关的私有名字。

2. **默认模式与测试**：`DEFAULT_GRAPH_MODE` 保持 `graph`。旧契约测试逐个
   （按测试/类/特性）显式请求 `legacy_graph_mode` opt-in fixture，每处都写明
   理由并指向 graph 模式下的对应覆盖。没有 autouse fixture、没有全局环境变量
   覆盖、没有整体 monkeypatch 默认值。

3. **服务 home 绑定**（`graph_home_binding.py`，接入 `graph_update.py`）
   - `plan_home_binding`：零写（不动 config.yaml、不动 home mtime、不建锁）。
   - `apply_home_binding`：只重写 `graph:` 段，其余行逐字节保留；先留可恢复的
     原件副本，再用一次 `os.replace` 落盘；写入前用**解析后的 YAML** 校验除
     graph 段外没有任何变化，否则拒绝。
   - `verify_effective_binding`：构造正常启动路径会构造的完整 `MemoryConfig`，
     核对它解析到的正是该 pointer、authoritative 模式与各条 projection 路径。
   - 重复 graph 段、畸形 YAML、未知 graph key、冲突的显式 target，或任何
     `LAYERED_MEMORY_GRAPH_*` 环境覆盖，都是拒绝，不是回退。
   - `graph_update` 只有在这项证明通过后才报告 `graph_update_applied`。

4. **全新初始化**（`graph_bootstrap.py`）
   - 独立 CLI，默认 preview；必须显式给出已检查的 home、destination、pointer。
   - home 下存在任何旧数据（knowledge Markdown、vectors.db、L0.md、graph.db、
     journal.db）即阻断，并一次列出全部，而不是只报第一个。
   - staged generation 通过现有 publisher 构建，manifest 声明
     `generation_type: fresh`；零 event、零 head、零 checkpoint 是它完整且合法的
     authority，不伪造任何 bootstrap event、source fingerprint 或记录时间。
   - 核验按声明类型分流：fresh 必须为空（无 backup、无 source 声明、无 seed、
     无 surface）；migration 必须有 authority——零 event 的 migration 现在明确
     报 `migration_generation_has_no_authority`。没有声明类型的旧 manifest 按
     migration（较严）读取。
   - 已存在的 generation 按**生命周期**选择门槛，走统一的
     `generation_is_served` seam：正在被该 pointer 服务的 generation 是可写的，
     因此用 attested runtime health 核验；从未被服务过的则完整走 publication
     证明。两者都在报告里用 `integrity` 字段标明。本批第一版实现曾对正在服务的
     generation 跑 publication hash，导致"写入过的正常 store 被报成损坏"、
     重复 bootstrap 反而因为服务被正常使用而失败；已修复并加了正反用例。

5. **向量 embedding 批次**：`storage/vector_store.py` 新增
   `EMBED_BATCH_SIZE = 16`，在唯一的 `model.embed` 调用点传入。向量值、顺序与
   被嵌入文本集合完全不变；只影响 ONNX runtime 保留的 arena。

### 12.2 运行口径

全新初始化（默认只读 plan）：

```text
PYTHONPATH=src python -m layered_memory_mcp.graph_bootstrap \
  --home <explicit-service-home> \
  --destination <explicit-destination> \
  --pointer <explicit-pointer>

PYTHONPATH=src python -m layered_memory_mcp.graph_bootstrap apply  --home … --destination … --pointer …
PYTHONPATH=src python -m layered_memory_mcp.graph_bootstrap verify --destination … [--home … --pointer …]
```

旧数据迁移后的更新（默认只读 preview，`--apply` 才写）：

```text
PYTHONPATH=src python -m layered_memory_mcp.graph_update \
  --home <explicit-service-home> \
  --source <explicit-old-snapshot> \
  --destination <explicit-destination> \
  --pointer <explicit-pointer>
```

`--home` 必须已存在：这个入口要准备并核验该 home 的绑定，preview 也不会创建它。

快照适配器带显式排除：

```text
PYTHONPATH=src python -m layered_memory_mcp.graph_migration_snapshot \
  --source <explicit-old-store> --destination <explicit-bundle> \
  --exclude <operator-owned-root-member> [--exclude …]
```

回滚口径不变：显式 `graph_mode: legacy` 仍是回滚模式；generation 的撤回仍走
`graph_migration rollback`，pointer 的回退仍走 `graph_generation_runtime restore`。
两者都不删除产物。

### 12.3 切换后的主入口验收矩阵

隔离集成检查（自建中性 fixture，确定性 embedding seam，离线）从 bootstrap →
activate → 绑定 home → 正常 `MemoryConfig()` → 逐个入口调用，读回磁盘结果：

| 入口 | 结果 |
|---|---|
| `inject_knowledge_tool(…, semantic_write_request=…)` | **支持**，`graph_committed`，L1/向量/L0 落在所绑定 generation 内 |
| `inject_knowledge_tool` 不带决策 | **拒绝** `graph_write_decision_required`（设计如此，不是缺口） |
| `recall_knowledge`、`search_semantic`、`get_l0_index`、REST `GET /knowledge/search`、`/domains` | **支持**，从所绑定 generation 读 |
| `recover_graph_projections`、`graph_memory_status` | **支持** |
| 整文件 L1 CRUD（MCP/REST）、派生索引与向量重建、cleanup/reconcile、`compact_memory(dry_run=False)`、agent 自有 surface | **拒绝，但有等价替代**；dry-run / 只读形式从不被拒 |
| 后台 job `auto_maintain`、`vector_maintenance`、`write_guard_refresh`、`watcher_l0_sync` | **挂起并上报** |
| TODO 写入（`add_todo`/`update_todo`/`delete_todo`、dashboard `POST /update-todo`） | **支持**（Batch 5 落地）：提交为 journal 中的任务决策，读取走 `todo` 投影 |
| `apply_semantic_lifecycle`（retract/restore/approve） | **支持**（Batch 5 落地）：只带目标的语义决策，不编造替代节点 |
| review 裁决（`approve_knowledge`/`reject_knowledge` 及 REST/dashboard 形式） | **支持**（统一 lifecycle）：reviewer/note 作为权威事件载荷写入 `review` namespace，graph journal 发布并可重放；legacy 行为保持不变 |

review 裁决不再是能力缺口：graph 入口不写入无关 claim，也不另造 review ledger，
而是复用统一 semantic lifecycle。`tests/test_v361_graph_review_decisions.py`
覆盖 approve/reject、载荷、幂等与 legacy 兼容；`tests/test_v361_graph_capability_matrix.py`
固定入口分类，避免能力状态漂移。

辅助 store 归属：access log 与 review queue 仍是 service home 自己的；todos
**不再属于辅助 store**，它是 journal 的 `todo` 投影（仍放在 service home 下，
和 knowledge 一样——投影放在读者会去看的位置）。graph、journal、knowledge、
vector、L0 解析到激活的 generation 内。

### 12.4 本批未验证项

- 任何真实生产记忆目录的迁移、全新初始化或 cutover。
- 安装态服务（实际运行的 MCP/HTTP 进程）消费该 pointer。
- 真实语料的性能、p95、容量与检索质量门槛；真实模型下的 batch 内存实测。
- 远端发版、包发布、CI。

## 13. Batch 5：切换前的四项核心收尾（本地实现，未经生产验证）

本批补齐四项既有核心能力缺口；每项均通过真实主入口和隔离回归契约验证。
与前几批一致：仅本地验证，不含任何生产迁移、安装态激活、发版或 cutover。

### 13.1 四项收尾内容

1. **任务是语义决策，TODO 视图是它的投影**（`graph_task.py` 新增，
   `graph_journal.py` / `graph_projection.py` / `graph_application.py` /
   `server.py` / `dashboard_plugin/plugin_api.py` 接线）。

   任务改动即事件，写在同一个 journal 里：不是第二套账本，也不是绕过 guard 的
   直接 SQL。一个任务是带操作字段的 `TASK` 节点；**更新 = 新版本 + SUPERSEDES**
   （与修订一条 claim 完全同构），**删除 = 对当前版本的纯 RETRACT**。`task_id`
   跨版本稳定，是运维手里的那个 id。

   `todo` 与 `graph`/`l1`/`vector`/`l0` 并列进入 `PROJECTION_NAMES`：有自己的
   checkpoint，被 `recover_graph_projections()` 覆盖，从重放的事件流重建而非
   就地编辑。投影写的是读入口本来就在读的那张 `todos` 表，所以 `list_todos`、
   `get_todo_stats`、dashboard `GET /todos` 无需第二套返回结构。

   四条必须成立的性质：终态时间戳归状态迁移所有（重开已完成任务会**清除**
   `completed_at`）；重放用事件时间而非新时钟；既有任务**显式迁移、绝不被重建
   掉**（`publish_todo` 对无授权行以 `todo_store_not_adopted:<count>` 拒绝）；
   只有一个权威（graph 写模式下没有隐藏的 legacy 回退）。

   刻意未做：`blocked_by` 仅作为载荷字段原样保留，不铸造 `BLOCKS` 边——边要求
   两端都是被投影的节点，而 legacy 的 `blocked_by` 是自由字符串，转换会把不可
   验证的数据变成让整次重放失效的悬挂边。

2. **纯生命周期变更**（`graph_write.SemanticLifecycleRequest`、
   `graph_application.commit_semantic_lifecycle`、`server.apply_semantic_lifecycle`）。

   删除或恢复一条记忆不再需要编造一个带虚构内容的替代节点。请求只带目标、
   动作、时间与 request key，**没有 node 字段**。动作取既有的只带目标的事件
   种类（`retract`/`restore`/`approve`）；`supersede` 刻意不可用——替代决策必须
   带上它所声明的替代物。进 journal 之前即 fail-closed：目标不存在、scope 与
   目标不一致、当前状态不接受该迁移，都是拒绝而非告警。§8.2 迁移表现在在
   `graph_models` 中只有一份，投影与校验读同一张表。`restore` 若会让同一个 L1
   surface 出现两个当前所有者，按 `surface_owned_by:<node>` 拒绝；历史始终保留。

3. **可恢复的 L1 发布**（`graph_journal.py`、`graph_projection.py`、
   `graph_application.py`）。

   复现到的缺陷：`publish_l1` 在写文件**之前**就登记了 surface 的 body hash，
   于是一次失败的写入留下一条描述"从未落盘的字节"的注册记录，之后每次恢复都
   拿磁盘和这个幻影 hash 比较并报 `surface_drift`——我们自己的崩溃被永久误判为
   外部编辑，`vector` 与 `l0` 永远排在它后面。

   修复是两阶段发布协议，而不是放宽比较（那个比较正是保护人工编辑的东西）：
   先登记本次发布对每个 surface 的 **intent**（协调状态，明确不是第二份语义
   真相），再通过同目录临时文件 + 一次 `os.replace` 原子替换（目标文件永不被
   截断、retire 不删文件），最后才让注册表采用新 body 并清除 intent。

   两个崩溃窗口都能自行收敛，不需要 `force=True`：替换**之前**崩溃，注册表仍
   描述磁盘上现有的 body，重试就是一次普通重发；替换**之后**、定稿之前崩溃，
   磁盘内容与已登记 intent 相符，`surface_refusal` 认得这是我们自己未完成的
   写入。两者都不匹配的 body 仍然是 `surface_drift`，仍然被拒。

   发布链同时改为按依赖关系而非单一顺序链：`l1` 依赖 `graph`，`vector`/`l0`
   依赖 `l1`，`todo` 只依赖 `graph`——一个被拒的可读 surface 不再扣住任务投影。
   L1 未落盘的提交会在 `committed: true` 旁给出 `surface_published: false` 与
   `l1_projection_pending:<entry id>`。

4. **按 section 精确的检索**（`recall.py`、`graph_search.py`）。

   一个关键词结果是**一个文件**带多个命中 section。过去只要任一 seed 存活就保留
   整个文件载荷，于是被撤回/过期/跨 namespace 的 section 会搭在同文件存活兄弟
   section 的载荷里出去。现在生产者为每个展示片段声明拥有它的 H2 surface 身份，
   图层逐 section 判定并**重建**主载荷（`sections`、`section_entries`、
   `matched_sections`），文件保留原排名。对一个确有被排除 section 的文件，无法
   确认归属的片段一并丢弃并以 `section_payload_unmappable:<count>` 上报——此时
   "大概没问题"不是答案。没有任何排除的文件按原样返回。

### 13.2 运行口径

```
cd <PROJECT_ROOT>
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python3 -m pytest -q -p no:randomly
```

- 全量默认套件：**2891 passed, 0 failed**（隔离环境）。
- 本批新增用例：`tests/test_v361_core_closure.py`（52 条），覆盖四项缺陷的
  正向契约、负向控制（未知 id / 非法枚举 / 空更新不写 journal）、两个崩溃窗口、
  外部编辑仍被拒、迁移 preview 零写入与 apply 原样搬迁、legacy 模式不变。
- 任务迁移的操作路径：

```
# 预览（默认，不写任何东西）
python3 -m layered_memory_mcp.graph_task plan  --home <SERVICE_HOME>
# 落地（追加一条 bootstrap 请求并发布投影）
python3 -m layered_memory_mcp.graph_task apply --home <SERVICE_HOME>
```

  home 的配置按服务自己的解析方式解析，所以种子落在该 home 实际绑定的
  generation 里，不会猜位置。未绑定 generation 的 home 会得到
  `graph_write_not_configured` 的有界拒绝并且不写任何东西。

- 迁移生成的 generation 其 `todo` checkpoint 与其它投影一同推进到 head，并在
  manifest 里记 `tasks_seeded: 0` / `task_authority: "seeded_separately"`：
  迁移源是 L1/向量/L0 物料，任务 store 在 **service home** 下而不在 generation
  内，所以"该事件流对任务投影无要求"与"既有任务已迁移"是两件不同的事，不能
  从一个看起来完整的 checkpoint 行推出后者。

### 13.3 本批未验证项

- 任何真实生产记忆目录的任务种子、迁移或 cutover；安装态服务未重启、未切换。
- 两个崩溃窗口由本仓库用例在真实接缝上注入验证
  （`replace_document` 失败、`os.replace` 之后 `record_surface` 失败、临时文件
  写入失败），结果确认缺陷已消失。
- review 裁决已通过统一 semantic lifecycle 写入 `review` namespace；reviewer/note
  作为权威事件载荷进入 journal，approve/reject 的 graph 入口、幂等、载荷与
  legacy 兼容已由 `tests/test_v361_graph_review_decisions.py` 覆盖。

### 13.4 任务视图位置这件事（刻意的边界）

任务视图解析在 `<home>/data/todos.db`：在实际运行的 `MemoryConfig` 下这是
service home（而 graph/journal/knowledge/vector/L0 解析进激活的 generation），
在 generation 作用域的运行配置下则是该 generation 自己的根——与它的向量库同理。
本批没有搬迁运维的任务数据。

因此：**激活另一个 generation 或回滚到更早的 generation**，会留下一个其中若干行
对新绑定 journal 而言"无授权"的任务视图。此时的行为就是 adoption guard 已经给出
的那个：任务入口拒绝并指向 `seed_task_authority`，而不是把这些行重建掉。运维动作
是对该 home 重新跑一次 `layered-memory-seed-tasks apply`。

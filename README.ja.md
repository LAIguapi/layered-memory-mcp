# Layered Memory MCP Server

> 共通コア型の知識フレームワークでAIエージェントの記憶をトークン制限の先へ

[**English**](README.md) | [**中文**](README.zh-CN.md) | [**한국어**](README.ko.md)

[![PyPI version](https://img.shields.io/pypi/v/layered-memory-mcp.svg)](https://pypi.org/project/layered-memory-mcp/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 課題

AIエージェントには**限られた記憶容量**しかありません。通常、毎ターン注入される永続コンテキストは2〜4KB程度です。容量がいっぱいになると、エージェントは他のすべてを忘れてしまいます。プロジェクトの設定、ユーザーの好み、API規約、ドメイン知識などを保存しようとすると、常に容量制限との戦いになります。

## 解決策

**Layered Memory**は、境界の明確な**共通コア**だけを所有し、それ以外は元の持ち主のもとに残します。

```
生セッション / ユーザー入力 / ツール結果
        │
        ▼
    永続化分類器
        │
        ├─ フレームワーク所有の共通コア（書き込み可）
        │    ├─ L0インデックス   2-4KB、毎ターン注入：どの知識がどこにあるか
        │    ├─ L1ナレッジ       持続する領域認知：事実、決定、現在の状態
        │    ├─ TODO            未完了の状態：保留、ブロック、要検証
        │    └─ 記憶チェンジログ  L0/L1の意味的変更の来歴
        │
        ├─ エージェント所有の拡張面（分類と提案のみ、既定では書き込まない）
        │    ├─ スキル           操作手順、コマンド列、ランブック
        │    ├─ ユーザープロフィール  少数の恒常的なユーザー設定
        │    └─ その他           それ以上分類しないエージェント所有の内容
        │
        └─ 永続化しない
             └─ 未決 / 一時的な内容（使い捨ての出力、生ログ）
```

**L0は目次。L1は本棚。TODOは未処理トレイ。生セッションは日記 — 読み取り専用の証拠で、検索はしても書き込みはしません。**

スキルとユーザープロフィールはエージェントのものです。フレームワークはそれらを認識して構造化された提案を返すだけで、[明示的に有効化](#任意のエージェント拡張実行層)しない限り書き込みません。

L0インデックス、ベクトルストア、検索インデックスはL1 Markdownからの**派生データ**で、いつでも再構築できます。

## 特徴

- **スマートナレッジ注入** — 書き込み一回で即座に可視化：重複排除、セクションターゲティング、L0インデックス自動同期付き（upsert/append/mergeモード）
- **永続化ルーティング** — 決定論的な分類器（`classify_memory_item`）が、その内容をどの記憶面に置くべきかを判定します：L1ナレッジ、TODOストア、あなた自身のスキル/プロフィール面、あるいは「保存しない」。マーカー＋構造規則のみで、モデル呼び出しもベクトル推論もありません。既定では助言のみで、書き込み結果に注記を加えるだけで挙動は変えません
- **能力リフレクション** — `get_memory_surface_capabilities` が現在の設定のもとで、どの面をフレームワークが代わりに書けるのか、どの面は提案しかできないのかをそのまま報告します
- **任意のエージェント動作実行** — `apply_agent_action` は分類済みのスキル／プロフィール／エージェント自有内容を代わりに書き込めますが、あくまで「明示的に設定された調整役」としてです：既定は無効、面ごとのスイッチ、確認モード、`dry_run`、そして明示的な書き込み先パスが必要です。拒否は常に報告され、沈黙することはありません
- **ハイブリッド検索（RRF）** — キーワード、あいまい一致、BM25、セマンティック検索をReciprocal Rank Fusionで統合し、安定した順位付けを行います。クエリ種別の自動判定でセマンティック／キーワードの比重を調整します
- **エージェント非依存のL0アクセス** — `get_l0_index` ツールで、どのMCPエージェントからでも記憶インデックスを取得できます
- **マルチエージェント名前空間** — `LAYERED_MEMORY_NAMESPACE` でエージェントごとに知識を分離しつつ、共通知識は共有します
- **セッションスキャン** — 直近のエージェントセッションから知識候補を抽出
- **ヘルス診断** — L0↔L1の整合性、ファイル構造、知識品質をチェックし、腐化（rot）と治理のサマリーを付加
- **ライフサイクル治理** — 持続的知識に混ざったプロセス状態（TODO / FIXME / next step / pending / 待办 / 要対応 …）を検出し、構造化された推奨アクションを返します：`move_to_todo`、`rewrite_as_current_rule`、`keep_with_reason`。マーカー語彙は設定可能です
- **クリーンアップ計画** — 読み取り専用のプランナーが、検出事項を「安全に自動修復できるもの」「ユーザーの判断が必要なもの」「自動修復してはいけないもの」に分類。別のツールが安全なものだけを実行し、既定はdry-runです
- **書き込み安全性** — ファイル全体を書き換える際のバックアップは知識ディレクトリの外（`<LAYERED_MEMORY_HOME>/data/backups/knowledge/`）に置かれ、稼働中の知識ディレクトリには稼働中のファイルだけが残ります
- **よりクリーンなベクトル想起** — 埋め込み対象の見出しからは日付・バージョン・課題番号などのノイズを除去します。表示用の見出しとメタデータは元のまま
- **設定可能な雑多ファイルスキャン** — 保守・監査スキャンはglobパターンで「雑多」系ファイル（`misc`、`notes`、`inbox`、`scratch`、`杂项` …）を検出するので、`misc.md` という名前のファイルは必須ではありません
- **容量分析** — メモリ使用量を監視し、最適化の提案を表示
- **エージェント非依存** — MCP互換のエージェントなら何でも動作（Hermes、Claude、Cursorなど）
- **依存関係ゼロ** — コアエンジンはPython標準ライブラリのみ。MCPトランスポートには`fastmcp`のみ使用
- **プライバシーファースト** — すべてのデータはローカルに保存、外部API呼び出しなし

## クイックスタート

### インストール

```bash
pip install layered-memory-mcp
```

### Hermes Agent

`~/.hermes/config.yaml`に追加：

```yaml
mcp_servers:
  layered-memory:
    command: layered-memory-mcp
    timeout: 30
```

### OpenClaw

MCPサーバーをインストール後、登録します：

```bash
pip install layered-memory-mcp

# MCPサーバーとして登録
openclaw mcp set layered-memory --command layered-memory-mcp
```

Layered MemoryはOpenClawの内蔵ベクトルベースメモリを補完します：
- **OpenClawメモリ**：セッション履歴のセマンティック検索（重い、埋め込みが必要）
- **Layered Memory**：キュレートされたナレッジファイルの構造化キーワード検索（軽い、即座）
- 両方使い分けましょう：OpenClawで「Xについて何と言ったっけ？」、Layered Memoryで「データベースの接続文字列は何？」

### Claude Desktop

Claude DesktopのMCP設定に追加：

```json
{
  "mcpServers": {
    "layered-memory": {
      "command": "layered-memory-mcp"
    }
  }
}
```

### Cursor / その他のMCPクライアント

```bash
# stdioモード（デフォルト）
layered-memory-mcp

# HTTPモード
layered-memory-mcp --transport http --port 8080

# 詳細ログ出力
layered-memory-mcp --verbose
```

### 環境変数

| 変数 | 説明 | デフォルト |
|------|------|-----------|
| `LAYERED_MEMORY_HOME` | メモリデータのルートディレクトリ | `~/.layered-memory/` |
| `LAYERED_MEMORY_SESSIONS_DIR` | エージェントセッションディレクトリ（自動検出） | `~/.hermes/sessions/` |
| `LAYERED_MEMORY_AUTO_SYNC_L0` | 書き込み後にL0インデックスを自動同期 | `true` |
| `LAYERED_MEMORY_DEDUP_THRESHOLD` | 重複排除の類似度しきい値（0.0〜1.0） | `0.7` |
| `LAYERED_MEMORY_L0_FORMAT` | L0インデックス形式：`hermes`または`generic` | `hermes` |
| `LAYERED_MEMORY_NAMESPACE` | マルチエージェント分離用の名前空間 | `shared` |
| `LAYERED_MEMORY_PROMOTION_WATCH_PATTERNS` | 保守スキャン用の雑多ファイルglobパターン（カンマ区切り） | 内蔵の英語＋中国語リスト |
| `LAYERED_MEMORY_AUTO_MAINTAIN_INTERVAL_DAYS` | 自動圧縮の最小間隔（日） | `7` |
| `LAYERED_MEMORY_AUTO_MAINTAIN_ATTEMPT_INTERVAL_HOURS` | *周期トリガー*による遅延判定の最小間隔（時間、`0` で無効） | `1` |
| `LAYERED_MEMORY_ROUTING_ENABLED` | 内容を分類し、書き込み結果にルーティング情報を注記 | `true` |
| `LAYERED_MEMORY_ROUTING_MODE` | `advisory`（助言）または `enforce`（強制）— `compact_memory` にのみ作用 | `advisory` |
| `LAYERED_MEMORY_ROUTING_ENFORCE_DIRECT_WRITES` | 明示的な `inject_knowledge` が非L1内容を書こうとしたら拒否 | `false` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_ENABLED` | エージェント所有面への代理書き込みを許可 | `false` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_MODE` | `suggest` / `apply_with_confirmation` / `apply` | `suggest` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_ROOT` | ファイル書き込み型ハンドラの基準ディレクトリ | 未設定（拒否される） |
| `LAYERED_MEMORY_AGENT_MEMORY_PATH` | エージェント自身の記憶ファイル（明示的な書き込み先） | 自動検出 |
| `LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE` | 自動検出された記憶ファイルへのL0投射を許可 | `false` |
| `LAYERED_MEMORY_CHANGELOG_ENABLED` | L0/L1の意味的変更を記憶チェンジログに記録 | `true` |
| `LAYERED_MEMORY_CHANGELOG_PATH` | チェンジログファイルの位置 | `<home>/data/memory_changelog.jsonl` |

### config.yaml

ほとんどの挙動は `<LAYERED_MEMORY_HOME>/config.yaml` でも設定できます。優先順位は、コンストラクタ引数 → 環境変数 → config.yaml → 内蔵デフォルトです。知識治理とルーティングに関わる段落：

```yaml
governance:
  # 以下のリストはいずれも内蔵の英語・中国語デフォルトに「追加」されます
  open_markers: ["WIP", "follow-up"]          # 未完了のプロセス状態 → move_to_todo
  resolved_markers: ["shipped"]                # 解決済みの履歴 → rewrite_as_current_rule
  keep_reason_labels: ["保持理由"]             # 「<ラベル>: <理由>」行があればそのセクションのマーカーを保持
  disabled_markers: ["TBD"]                    # 自分にとってノイズなデフォルトを無効化

promotion:
  watch_domains: ["misc"]                      # 完全一致の雑多ファイル名（書き込み時にも検査）
  watch_patterns: ["misc", "misc-*", "notes", "inbox", "scratch", "杂项"]  # 大文字小文字を区別しないglob、保守／監査スキャン専用

routing:
  enabled: true
  mode: advisory                               # advisory | enforce — compact_memory のみを制御（下記参照）
  enforce_direct_writes: false                 # true = inject_knowledge が非L1内容を拒否
  # 以下のリストも governance と同じく内蔵語彙に「追加」されます：
  extra_skill_markers: ["手順書"]
  extra_todo_markers: ["承認待ち"]
  extra_profile_markers: ["呼び方"]
  extra_knowledge_markers: ["サービス契約"]
  disabled_markers: ["スクリプト"]             # 自分にとってノイズなデフォルト語を無効化

agent_memory:
  adapter: auto                                # どのエージェント製品のファイル配置を前提にするか（下記参照）
  path: null                                   # エージェント自身の記憶ファイル（明示的な書き込み先）
  allow_auto_detect_write: false               # 自動検出先への投射を明示的に許可する場合
  separator: null                              # アダプタ既定のエントリ区切り文字を上書き

memory_changelog:
  enabled: true                                # 総合スイッチ。offなら何も記録しない
  path: null                                   # 既定は <home>/data/memory_changelog.jsonl
  retention_days: null                         # 報告するだけで**実行しない** — ログを削除する処理は存在しない
```

`mode: advisory` が既定です：ルーティングは通常の結果の隣に自分の判断を添えるだけで、書き込みの挙動は変えません。アップグレード後に突然書き込みを拒否し始めるのは機能ではなくデータ損失事故なので、`enforce` は明示的な有効化が必要です。そして、それを根拠に書き込みを止めるパスはただ一つ、`compact_memory` だけです（[圧縮パスのルーティング](#圧縮パスのルーティング)を参照）。そこが**フレームワーク自身**が書き込み先を選ぶパスだからです。`inject_knowledge` はどちらのモードでも従来どおりL1に書き込み、そのことを `routing_warning` で明示します。

`enforce_direct_writes` は二つ目の、独立したスイッチであり、明示的な書き込みを拒否できる唯一のスイッチです（[厳格な直接書き込み](#厳格な直接書き込み任意で有効化)を参照）。`mode: enforce` にしてもこちらは有効になりませんし、こちらを有効にしても圧縮が何を移行するかは変わりません。`routing.enabled: false` なら空転します — 分類がなければ強制するものもありません。

キーを書かなければデフォルトが維持されます。`watch_patterns` を設定すると、あなたのリストが内蔵パターンリストを置き換えます。`[]` にするとパターン一致を無効化できます。

`agent_memory:` は、L0ポインタ／知識インデックス項目をどの「エージェント自身の」記憶ファイル（`MEMORY.md`、`CLAUDE.md` など）に投射できるかを決めます。パス未設定の場合フレームワークは自動検出します — 読むだけなら問題ありませんが、書き込みには危険です：曖昧な設定（テスト、一時的なhome上で走る使い捨てスクリプト）が実ファイルを解決して静かに追記してしまいます。そのため投射書き込みには次のいずれかが必要です：明示的な書き込み先（`agent_memory.path`、または呼び出し時の `memory_path` 引数）、明示的な許可（`allow_auto_detect_write: true`）、検出先が `<LAYERED_MEMORY_HOME>` の内側にあること、あるいはそのhomeが一時／テスト用サンドボックスではなく本インストールの実際の設定homeであること。それ以外はすべて拒否され、`agent_memory_write: {status: "skipped", reason: "unsafe_auto_detect_write", detected_path, hint}` として報告されます — L1書き込み自体は常に成功し、止まるのは投射だけです。**検出できることは権限ではありません**：アダプタ経由でパスを解決できても、それを書く権利は生じません。

### 共通コアはエージェント非依存

コアが所有するのはL0、L1、TODO、`MEMORY_CHANGELOG` の意味だけで、それ以外は何も所有しません。ホストのエージェントが自分の記憶ファイルをどこに置くか、エントリの区切り文字は何か、ライブセッションのDBがどこにあるか、何文字まで許されるか — これらはすべて製品固有の事実で、`src/layered_memory_mcp/adapters/` にある**環境アダプタ**が答えます。かつては `config.py`、`memory_compactor.py`、`agent_integrator.py` に直書きされていたリテラルですが、今はそうではありません。

```yaml
agent_memory:
  adapter: auto     # デフォルト
```

| 値 | 解決結果 |
| --- | --- |
| `auto`（デフォルト） | レジストリを順に探索 — `hermes`、`claude_code`、`cursor`、`cline`、`generic` — アダプタ抽出前のコアが直書きしていた候補列と完全に同一 |
| `hermes` | `~/.hermes/memories/MEMORY.md`、`§` 区切り、`~/.hermes/state.db`、`~/.hermes/sessions`、および `~/.hermes/config.yaml` から読み戻した実際の上限 |
| `claude_code` | `~/.claude/CLAUDE.md` |
| `cursor` | `./.cursorrules` |
| `cline` | `~/.cline/rules` |
| `generic` | 製品固有パスを一切持たない — フレームワーク自身の `<home>/agent-memory.md` だけ |
| `none` / `off` | `generic` のエイリアス |

**後方互換のため、Hermesがデフォルトのアダプタ**であり最初に探索されるので、既存のインストールは従来とまったく同じパスを解決します。未知のidは警告つきで `generic` にフォールバックします — 黙ってHermesになることはありません。環境変数での等価な指定：`LAYERED_MEMORY_AGENT_ADAPTER=hermes`。

二つ目のエージェントに対応するにはアダプタを登録すればよく、ルーター・圧縮器・設定層を書き換える必要はありません：

```python
from layered_memory_mcp.adapters.environment import EnvironmentAdapterBase, environments

class AcmeEnvironment(EnvironmentAdapterBase):
    id = "acme"
    display_name = "ACME Agent"
    memory_relpath = (".acme", "brain.md")      # ユーザーのhomeからの相対パス
    install_marker_relpath = (".acme",)
    state_db_relpath = (".acme", "sessions.sqlite")
    separator = "---"
    path_tokens = (".acme", "brain.md")         # 明示的に設定されたパスの識別に使用

environments.register(AcmeEnvironment(), priority=15)
```

`get_memory_surface_capabilities()` は結果を `agent_environment: {configured, resolved, known, entry_separator, …}` の下に報告します。これは下記の `adapter` とは別物です — 後者が指すのは任意の拡張**動作**ハンドラです。

Hermes固有の実装のうち二つは、意図的にアダプタの外、それぞれ独立した支援パッケージに残しています：`write_guard` プラグイン（ペイロード、インストーラ、`hermes` CLIの探索）と `dashboard_plugin` です。`integrate_agent` のHermes限定アクション（`inject_soul`、`install_dashboard`、`install_guard`）は、他のホストでは理由を明示して拒否します。

### 任意のエージェント拡張実行層

スキル、ユーザープロフィール、その他のエージェント所有内容は**既定では提案のみ**です：フレームワークは分類を終えると、あなたの原文と構造ヒント（`shape_hint`）を持つ `AgentAction` を返し、自分では何も書きません。代わりに書き込んでほしい場合は、明示的に有効化します：

```yaml
agent_extensions:
  enabled: false                 # 総合スイッチ（既定は無効）
  mode: suggest                  # suggest | apply_with_confirmation | apply
  adapter: null                  # 統合のラベル。例：generic_file / custom
  root: null                     # 任意：相対パスの基準ディレクトリ
  surfaces:
    skill:
      enabled: false
      handler: null              # 登録済みアダプタの名前
      path: null                 # ファイル書き込み型ハンドラの明示的な書き込み先
    user_profile:
      enabled: false
      handler: null
      path: null
    agent_other:
      enabled: false
      handler: null
      path: null
```

フレームワークがある面に代理で書き込むには、五つの条件がすべて成立していなければなりません：トップレベルの `enabled: true`、その面の `enabled: true`、**実際に登録済み**のアダプタを指す `handler`、`suggest` 以外の `mode`、そしてそのハンドラが安全な書き込み先を解決できること。どれか欠けていたり設定が壊れていれば、キー単位で「分類して提案するだけ」に戻ります。**有効化しても所有権は移りません**：`get_memory_surface_capabilities` はこれらの面を引き続き `owned_by: agent`、`default_behavior: suggest` と報告し、変わるのは `can_apply` だけです。フレームワークはそこでは調整役であって、決して所有者ではありません。

`can_apply` が報告するのはスイッチの状態ではなく*実際の*準備状況です：名前は設定したが登録されていないハンドラは `handler_registered: false` を返し、`apply_blockers` には `apply_agent_action` が拒否するときと同じ理由コード（`extensions_disabled`、`surface_disabled`、`mode_suggest`、`no_handler`、`handler_not_registered`、`no_configured_path` など）が並びます。`registered_handlers` は設定可能なハンドラ名を列挙します。

#### `apply_agent_action` — 唯一の書き込み可能ツール、既定は無効

```
apply_agent_action(
  surface="skill", operation="create", title="リリース手順",
  source_content="<あなたの原文>", rationale="再利用可能な操作手順",
  confirmed=false, dry_run=false,
  action=null,            # AgentAction のJSON形式。指定すると上のフラットな引数を上書き
)
```

戻り値は `{applied, status, surface, operation, handler, adapter, mode, dry_run, confirmed, target, blockers, detail, error, metadata}` です。`status` の取り得る値：

| status | 書いたか | 意味 |
| --- | --- | --- |
| `disabled` | いいえ | `agent_extensions`（またはその面）が無効、あるいは `mode: suggest` — 提案のみ |
| `unsupported` | いいえ | handler未設定、未登録、またはその面に非対応 |
| `refused` | いいえ | 拡張面ではない、またはハンドラが拒否（明示的パスがない、書き込み先が安全でない等） |
| `invalid_action` | いいえ | `kind` / `surface` / `operation` / `source_content` の検証に失敗 — 自動補修は一切しない |
| `confirmation_required` | いいえ | `mode: apply_with_confirmation`（または動作自身が確認を要求）で `confirmed` がfalse |
| `would_apply` | いいえ | `dry_run: true` — 書き込み先、バイト数、見出しを報告 |
| `applied` | **はい** | ハンドラが書き込んだ |
| `error` | いいえ | ハンドラが例外を投げた。例外情報は `error` に入る |

三つのモード：`suggest`（既定）は分類して `AgentAction` を渡すだけ。`apply_with_confirmation` は書き込み先を解決して報告しますが、`confirmed=true` 付きの二回目の呼び出しが必要です。`apply` は一回目の呼び出しで書き込みますが、動作自身の `requires_user_confirmation` が真の場合は常に確認が必要です。

エージェント自有の動作を実行しても `MEMORY_CHANGELOG` には**記録されません**。あのログはフレームワーク所有のL0/L1変更の来歴であり、ここでの書き込みはエージェントのものです。

#### `generic_file` リファレンスハンドラ

登録済みのハンドラが一つ同梱されていますが、「登録済み」は「権限あり」ではありません — 上のチェーンが揃い、かつ書き込み先パスが安全に解決できるまで、何も書きません：

```yaml
agent_extensions:
  enabled: true
  mode: apply_with_confirmation
  adapter: generic_file
  surfaces:
    skill:
      enabled: true
      handler: generic_file
      path: /srv/agent/extensions/skills.md
```

このハンドラは動作ごとに、長さ上限のあるMarkdownの `##` セクションを一つ追記します：あなたの `source_content` の上に短いメタ情報ブロック（面、操作、ハンドラ、タイムスタンプ、rationale、`shape_hint`、動作metadata）を置き、機械検索しやすいように `<!-- layered-memory:agent-action -->` マーカーで始めます。`create`、`append`、`update` は**すべて追記**です。`update` は何を `supersedes`（置き換え）したかを記録するだけで、曖昧な原位置編集はしません — フレームワークが所有していないファイルを書き換えることは、内容を壊しうる唯一の挙動だからです。8000文字を超える内容は明示的なマーカー付きで切り詰められます。

パス規則はすべて、推測ではなく明確な拒否です：

| 状況 | 結果 |
| --- | --- |
| `path` も `root` もない | 拒否、`no_configured_path` — 既定の場所は意図的に用意していません |
| `root` だけ | `<root>/<surface>.md` |
| 相対 `path` | `root` を基準に解決、なければ `LAYERED_MEMORY_HOME` を基準に解決。その内側に留まる必要あり（`path_escapes_base`） |
| 絶対 `path` | 受理 — ファイルを名指しすること自体が、このゲートの求める「明示性」です |
| ディレクトリ（既存、または末尾が `/`） | `<dir>/<surface>.md` |
| `/etc`、`/usr`、`/sys`、`/var/lib`、`C:\Windows` などの配下 | 拒否、`unsafe_system_target` |
| `.py` / `.yaml` / `.json` / `.db` / `.env` や隠しファイル | 拒否、`unsafe_file_type` — Markdownを追記すればファイルが壊れます |

親ディレクトリが作成されるのは、あなたが明示的に設定したパスに対してだけです。このパス上にコマンドアダプタはなく、shell実行も一切ありません。Hermesネイティブのスキル／プロフィールファイルへの書き込みは**未実装**で、引き続き範囲外です。

カスタムハンドラは `name`、`supports(surface)`、任意の `readiness(surface, config)`、`apply(action, config=None, *, dry_run=False)` を持つクラスで、`layered_memory_mcp.adapters.registry.register(...)` で登録します。`dry_run` 引数を宣言していないアダプタは `dry_run=True` のもとで呼ばれることはありません — フレームワークは、しないと約束した書き込みのリスクを取る代わりに拒否します。

## 使い方

### 1. ナレッジの書き込み（推奨）

`inject_knowledge`ツールは、すべてのエージェント向けの**プライマリ書き込みパス**です。1回の呼び出しで重複排除、セクションターゲティング、L0インデックスの自動同期を処理します。

```
エージェントが学習：「本番DBはPostgreSQL 15 on prod-db:5432」
→ inject_knowledge(
    domain="infrastructure",
    section="Database",
    content="PostgreSQL 15 on prod-db:5432, connection pool: 20 max",
    mode="upsert"
  )
← infrastructure.mdを作成/更新し、L0インデックスを自動同期
```

**書き込みモード：**
| モード | 動作 |
|------|------|
| `upsert`（デフォルト） | 類似コンテンツがあれば置換、新規なら追記 |
| `append` | 常に追記、重複チェックをスキップ |
| `merge` | 新規と既存のユニークな部分を結合 |

### 2. ナレッジの読み取り

```
エージェント: 「データベースの接続文字列は？」
→ recall_knowledge(keyword="database")
← infrastructure.mdから関連セクションを返す
```

### 3. ヘルス診断

```
→ validate_knowledge()
← L0↔L1の整合性、孤立ファイル、古いエントリ、ファイル健全性をチェックし、
  rot_summary / rot_health_score と governance_summary
  （L1で見つかった状態マーカーをライフサイクルアクション別に集計）を付加
```

### 4. クリーンアップの計画と実行

クリーンアップのフローは、機械的な修復と意味的な修正を分けて扱います：

```
1. plan_knowledge_cleanup_tool(max_items=50)
   ← 読み取り専用のJSON、三分類：
     safe_auto_fixes      残存する *.md.bak を knowledge/ の外へ移動、L0を再同期、
                          ベクトルストアを再構築、未完了マーカーをTODOへコピー
     needs_user_decision  解決済み履歴の書き換え、同主題の見出し統合、過大ファイルの分割…
     must_not_auto_fix    保持理由行付きで意図的に残されたマーカー

2. apply_safe_knowledge_cleanup_tool(actions=<safe_auto_fixes の項目>)
   ← 既定はdry-run：何が起きるかを報告するだけで、何も書かない

3. apply_safe_knowledge_cleanup_tool(actions=..., dry_run=False)
   ← 四種類の安全なアクションのみを実行し、それ以外はすべて拒否
```

実行ツールはL1のテキストを削除したり書き換えたりしません。マーカーをTODOに変えるときも、元の行はそのまま残ります。統合、書き換え、削除、ドメイン間の移動はユーザーまたはエージェントに委ね、通常の書き込みツールで行います。ベクトルベースの検査（ファイル横断の重複、分割候補）には `reconcile_knowledge_tool` を使ってください。

### 5. セッション圧縮（cronジョブ）

会話から新しい知識を抽出するために、毎日のcronジョブを設定：

```
1. scan_recent_sessions → セッションサマリーを取得
2. AIがサマリーを分析 → 安定した事実を特定
3. 新しい事実 → inject_knowledgeで書き込み（L0自動同期）
4. L0インデックス → 常に最新状態を維持
```

### 6. レガシーCRUD（引き続き利用可能）

ファイルの直接操作向け：

| ツール | 説明 |
|------|------|
| `create_knowledge_file` | 新しい.mdファイルを作成（L0自動同期） |
| `update_knowledge_file` | 既存ファイルを上書き（L0自動同期） |
| `delete_knowledge_file` | ファイルを削除（L0自動同期） |

## MCPツール

### 読み取りツール

| ツール | 説明 |
|-------|------|
| `recall_knowledge` | キーワードでL1ナレッジファイルを検索（関連度スコアリング付き） |
| `get_knowledge_file` | 特定のナレッジファイルを名前で読み取り |
| `list_memory_stats` | 容量統計、ファイルサイズ、最適化の提案を取得 |
| `scan_recent_sessions` | 直近のセッションから知識抽出候補をスキャン |
| `search_sessions_by_keyword` | キーワードでセッション履歴を検索 |
| `get_l0_index` | 完全なL0インデックスを取得（エージェント非依存） |
| `classify_memory_item` | 候補の内容をどの記憶面に置くか判定（`l1_knowledge` / `todo` / `skill` / `user_profile` / `agent_other` / `undecided` / `ephemeral`）。読み取り専用・決定論的・モデル呼び出しなし |
| `get_memory_surface_capabilities` | 現在の設定で、どの面をフレームワークが書けてどの面が提案のみかを報告 |
| `get_memory_changelog` | L0/L1の意味的変更の来歴を読む（domain / operation / surface / since で絞り込み。新しい順、件数上限あり） |
| `get_memory_changelog_stats` | チェンジログを集計：操作／面／領域別の件数、期間、設定状態 |

### 書き込みツール

| ツール | 説明 |
|-------|------|
| **`inject_knowledge`** | **プライマリ書き込みパス** — 重複排除、セクションターゲティング、L0自動同期付きのスマート注入 |
| `create_knowledge_file` | 新しい.mdファイルを作成（L0自動同期） |
| `update_knowledge_file` | 既存ファイルを上書き（L0自動同期） |
| `delete_knowledge_file` | ファイルを削除（L0自動同期） |
| `apply_agent_action` | 設定済みハンドラ経由でエージェント所有の拡張面アクション（`skill` / `user_profile` / `agent_other`）を実行。**既定では無効** — `agent_extensions` を明示的に有効化するまで `status: "disabled"` を返します。`dry_run` と `confirmed` に対応し、`MEMORY_CHANGELOG` には記録しません |

### 管理ツール

| ツール | 説明 |
|-------|------|
| `sync_l0_index` | L1ファイルからL0インデックスを手動再構築（`dry_run`対応） |
| `validate_knowledge` | ヘルスチェック：L0↔L1整合性、ファイル品質、重複検出、さらにrotスコアと治理（状態マーカー）サマリー |
| `manage_l0_entry` | 個別のL0エントリの追加/削除/置換 |

### 治理・クリーンアップツール

| ツール | 説明 |
|-------|------|
| `audit_rot_tool` | 読み取り専用の腐化監査：健全性スコア（0-100）、過大ファイル、壊れた見出し、古いセクション、ファイル横断の重複、雑多ファイルの昇格候補 |
| `reconcile_knowledge_tool` | ベクトルベースのファイル横断重複・分割候補スキャン |
| `plan_knowledge_cleanup_tool` | 読み取り専用のクリーンアップ計画：`safe_auto_fixes` / `needs_user_decision` / `must_not_auto_fix` |
| `apply_safe_knowledge_cleanup_tool` | 計画のうち安全な機械的アクションのみを実行（`migrate_legacy_backups`、`sync_l0_index`、`reindex_vector_store`、`create_todo_from_marker`）。既定は `dry_run=True` |

これらのツールはすべて追加です。既存ツールの引数と返却フィールドは変わりません。`validate_knowledge` と書き込み後の治理レポートはフィールドが増えるだけです（たとえば検出事項ごとの `action`）。

## MCPリソース

| リソース | 説明 |
|---------|------|
| `memory://status` | システム全体のステータスと設定 |
| `knowledge://files` | 全ナレッジファイルの一覧とメタデータ |

## MCPプロンプト

| プロンプト | 説明 |
|-----------|------|
| `knowledge_compression_prompt` | セッションからのAI駆動ナレッジ抽出用テンプレート |
| `cognitive_decision_prompt` | 規律あるメモリ使用のための意思決定フレームワーク |

## アーキテクチャ詳細

### 記憶面モデル

| 記憶面 | 所有者 | コスト | 内容 |
|--------|--------|-------|------|
| L0インデックス | フレームワーク | 毎ターンのトークン | ~2KBのポインタ：どの知識がどこにあるか |
| L1ナレッジ | フレームワーク | 1ファイル読み込み | 持続する事実、決定、境界、現在の理解 |
| TODO | フレームワーク | 1クエリ | 未完了の状態：保留、ブロック、要検証、要判断 |
| 記憶チェンジログ | フレームワーク | — | L0/L1の意味的変更の来歴 |
| スキル | **エージェント** | 1スキル読み込み | 再利用可能な手順、コマンド列、チェックリスト |
| ユーザープロフィール | **エージェント** | 注入 | 少数の恒常的なユーザー設定・人物像 |
| その他 | **エージェント** | — | スキルでもプロフィールでもないエージェント所有の内容 |
| 生セッション | **提供元** | 全文検索 | 読み取り専用の証拠：検索のみ、書き込みなし |

ここで重要な区別が二組あります：

- **所有 ≠ 代理で書けるか。** 所有は固定で、L0/L1/TODO/チェンジログはフレームワーク、スキル/プロフィール/その他はエージェント、生セッションは提供元のものです。フレームワークがエージェント所有の面に**代わりに書き込めるか**は別の問題で、明示的な有効化が必要です — [任意のエージェント拡張実行層](#任意のエージェント拡張実行層)を参照してください。
- **真実の源 ≠ 派生データ。** L1 Markdownが唯一の真実の源です。L0インデックス、`data/vectors.db`、検索インデックスはそこから派生し、いつでも再構築できます（`sync_l0_index`、`reindex_vector_store_tool`）。

### 記憶チェンジログとは何か

`MEMORY_CHANGELOG` は**あなたの記憶**の変更履歴で、このソフトウェアのリリースノートではありません。記録するのは永続面の意味的な変更だけです：L1セクションの新規作成・更新・統合・置換・削除、融合や統合の書き戻し、そして意味的な地図が実際に変わったときのL0エントリの追加・削除・改名です。

これは明確に、リポジトリの `CHANGELOG.md` ではなく、生のセッションログでもなく、汎用イベントログでもなく、TODOのライフサイクル履歴でもありません。派生データの再構築（ベクトル再インデックス、検索インデックス）も記録しません — 意味的な内容は変わっていないからです。

**保存方式。** `<home>/data/memory_changelog.jsonl` の追記専用JSONLで、1行1イベント。記録はベストエフォートで、失敗時は沈黙します — 来歴の記録が、それが説明する書き込みを失敗させてはならないからです。フレームワークが行を書き換えたり削除したりすることもありません。`retention_days` は設定から読まれ、そのまま報告されますが、**実行はされません**：来歴の削除は破壊的な操作であり、履歴を消すのは常に人の判断です。

**1イベントが持つもの。** 構造だけで、内容は持ちません：`schema_version`、`event_id`、`at`（UTC ISO）、`operation`、`surface`、`domain`、`section`、`actor` / `agent_id`、`tool`、構造的な `summary` と `reason`、任意の `source_ref`、対象本文の `before_hash` / `after_hash`、そしてバイト数や書き込み経路の詳細を入れる `metadata`。**本文そのものは決して含みません。** 内容の真実の源はナレッジファイルであり、本文を来歴ログにコピーすることは、ユーザーがメモに書いたあらゆる秘密に、索引もされない二つ目の住処を与えることになります。`metadata` はスカラーと短い文字列だけを受け取り、入れ子のコンテナはシリアライズせずに破棄します。

**現在記録されるもの。** `inject_knowledge`（したがって `compact_memory` のL1書き込みと `batch_inject_knowledge_tool` も）の新規作成／セクション新規作成／更新／統合／置換／融合書き戻し／統合書き戻し。`create_knowledge_file`、`update_knowledge_file`、`delete_knowledge_file` のファイル全体の変更。`sync_l0_index` と `manage_l0_entry_tool` のうち、エントリが実際に追加・消滅・変化したもの。

**記録されないもの。** 融合／統合の遅延ハンドシェイク、`fuse_conflict` や `consolidate_refused` の拒否、スキップされた重複、検証の失敗、書き込みの失敗、`dry_run=True` の試行、エントリ集合が変わらなかったL0再同期、そして内容ハッシュが変わらなかった書き込み。単一セクションの削除にはまだイベントがありません — 一つのセクションだけを削除するツールが存在しないためです（`ROADMAP.md` 参照）。

**読み方。** `get_memory_changelog(domain, operation, surface, since, limit)` は新しい順・件数上限付き（既定50、ハード上限500）で返します。`get_memory_changelog_stats()` は操作／面／領域別の件数と期間を集計します。どちらも読み取り専用です。`get_memory_surface_capabilities` は `memory_changelog` について、バックエンドが実際に有効かどうかを `can_apply` に反映し、解決後の `store_path` も報告します。

### 書き込み一回で即座に可視化パイプライン (v0.5.0)

v0.5.0の最大の革新は、**すべての書き込みパスが自動的にL0インデックスを同期**することです：

```
エージェントが inject_knowledge(domain="infra", section="Proxy", content="...") を呼び出す
  │
  ├─ 1. 重複チェック (SequenceMatcher、しきい値=0.7)
  ├─ 2. アクションの決定: upsert / append / merge / skip
  ├─ 3. セクションターゲティング (## 見出しを見つけるか作成)
  ├─ 4. ファイル書き込み (並行安全性のためfcntl.flockを使用)
  └─ 5. L0インデックス自動同期
        │
        ↓
  L0インデックス更新 → 次ターンでエージェントに可視
```

これにより、「書いたのに見えない」問題を解消します。エージェントがL1ファイルを書き込んでもL0インデックス（毎ターン注入される）が更新されず、将来のセッションで新しいナレッジが無視されるという問題が発生しなくなりました。

### 持続する知識 vs. プロセス状態

L1に置くのは**持続する知識**、つまり今成り立っているルールと事実です。未完了の作業（「TODO: Xを接続する」「要判断」）や「どう直したか」という語りは**プロセス状態**で、すぐに古くなります。書き込み後と `validate_knowledge` のとき、治理モジュールがL1の状態マーカーを走査し、各マーカー行を分類します：

| アクション | 意味 |
|--------|------|
| `move_to_todo` | 未完了の作業。L1ではなくTODOストア（`add_todo` / `list_todos`）に属します |
| `rewrite_as_current_rule` | 解決済みの履歴（「修正済み」「has been fixed」）。今成り立つルールとして書き直します |
| `keep_with_reason` | セクションに明示的な `<保持理由ラベル>: <理由>` 行があり、マーカーが意図的だと分かる場合 |

検出は決定論的（正規表現と文字列一致のみ、モデル呼び出しなし）で、結果は助言です。マーカー語彙はデータであり、config.yamlの `governance:` 段で設定するので、他の言語やチームの書き方に対応するのにコード変更は不要です。

同じ検査は**手順の混入**も報告します：持続する知識ではなく操作手順として読めるセクションです。マーカー**と**手順的な構造の両方が一致したときにだけ印が付きます（番号付きステップ、チェックリスト、コマンド列、コードブロック内のコマンド）— 「検証」や「フロー」に触れているだけのアーキテクチャ解説は知識であり、そのまま残されます。印が付いたセクションはあなた自身のスキル面に属します。フレームワークは報告するだけで、移動はしません。

### 書き込みパス上の助言的ルーティング

`inject_knowledge` が書くのはL1です — それが契約であり、ルーティングは既定でそれを変えません。ルーティングが加えるのは可視性です：すべての結果に `routing` ブロックが付き、分類器が別の面に送るべきと判断した書き込みには `routing_warning` も付きます。書き込みは従来どおり実行され、`action` もこれまでと同じです。バッチ書き込みも項目ごとに同じ `routing` を公開し、バッチ内の `deferred_fusion` も完全なハンドシェイク（`needs_fusion`、`expected_hash`、`fusion`）を伴います — バッチは手間を省く手段であって、より静かな書き込み経路ではありません。

これが圧縮との間にある意図的な非対称です：`inject_knowledge` の明示的な呼び出しは、呼び出し側が自分で書き込み先を指定したということであり、`mode: enforce` がそれを覆すことはありません。より厳格な規則を望むインストールは、次のスイッチを個別に有効化します。

### 厳格な直接書き込み（任意で有効化）

`routing.enforce_direct_writes: true` は、上の警告を拒否に変えます。分類は書き込みの**前**に走ります — 重複排除の前、ベクトルストアの前、何かがディスクに触れる前です：

| `routing.enabled` | `enforce_direct_writes` | `routing_override` | 分類結果 | 挙動 |
|---|---|---|---|---|
| `true` | `false`（既定） | — | 任意 | 従来どおり書き込み。非L1なら `routing_warning` 付き（挙動は不変） |
| `true` | `true` | `"none"`（既定） | `l1_knowledge` / `l0_index` | 通常どおり書き込み、`MEMORY_CHANGELOG` イベントも記録 |
| `true` | `true` | `"none"` | それ以外 | **拒否**、何も書かない |
| `true` | `true` | `"force_l1"` | それ以外 | 書き込み、`routing_override_applied: true` を付与 |
| `false` | `true` | — | 分類しない | 従来どおり書き込み、結果は旧来の形（routingフィールドなし） |

`routing.mode` は終始これと直交します：`compact_memory` が何を移行するかを決めるだけで、それ自体が直接書き込みを拒否することはありません。

拒否結果は構造化されており、検証エラーではありません — リクエスト自体は正しく、問題は**書き込み先**です：

```json
{
  "success": false,
  "action": "routing_refused",
  "error": "routing_enforce_direct_write_refused",
  "message": "Content classified as 'skill' (owned by agent, score 0.88); direct L1 write to 'ops' / '## 发布流程' refused by routing.enforce_direct_writes. …",
  "written": false,
  "routing": {"surface": "skill", "ownership": "agent", "score": 0.88, "signals": ["…"]},
  "routing_warning": "…re-submit with routing_override='force_l1' if it really is L1 knowledge.",
  "suggested_action": {"kind": "agent_native_write", "surface": "skill", "shape_hint": ["…"]},
  "routing_enforcement": {"gate_active": true, "override_requested": "none", "override_applied": false},
  "hint": "Nothing was written: no L1 file, no section, no MEMORY_CHANGELOG event, and no agent-owned write."
}
```

拒否されたとき何も書かれていません：ファイルもセクションも、`MEMORY_CHANGELOG` イベントも（あのログが記録するのは変更であり、拒否は変更ではありません）、エージェント所有の書き込みもありません — `suggested_action` は**あなた**が実行するために返されます。圧縮パスと同じ規律です。TODOの形をした内容は `todo_candidate` として返り、`add_todo` の呼び出しは引き続きあなたの判断です。

`inject_knowledge_tool(..., routing_override="force_l1")`（`batch_inject_knowledge_tool` でも同名の引数）はそれでも書き込み、結果に `routing_override_applied: true` を付けます。分類器が持続する知識を取り違えたときにだけ使ってください — ランブックやユーザー設定やログをL1に押し込むためではありません。それらには専用の面があり、拒否メッセージがどの面かを明示します。認識できない値は許可ではなく無視として扱われます。両方のスイッチは `get_memory_surface_capabilities` が報告します（`routing_mode`、`routing_enforce_direct_writes`、および `l1_knowledge` の注記）。

### 圧縮パスのルーティング

`compact_memory` はルーティングを認識します。その移行規則はずっと構造的なもの（「L0ポインタでない」→ L1）でしたが、それは**出所**についての判断であって内容の分類ではありません — その結果、エージェントの記憶ファイルに置かれたランブック、恒常的なユーザー設定、生ログがすべてL1ナレッジになっていました。いまは非インデックス項目のすべてが分類され、その分類をどう扱うかをモードが決めます：

| モード | 挙動 |
|------|------|
| `advisory`（既定） | 移行の挙動は不変：非インデックス項目はすべてL1へ。レポートに項目ごとの `routing`、分類器が別の面に送るべきと判断した項目の `routing_advisory`、面と所有者別に集計した `routing_summary` が加わるので、どれが置き場所を間違えたかを見てから後で移せます |
| `enforce` | `l1_knowledge` だけがL1に書き込まれます。TODO、スキル、ユーザープロフィール、エージェント自有、未決、一時的な項目は**エージェントの記憶にそのまま残され**、あなたが置き場所を決めるための候補として返されます |

config.yamlの `routing:` 段で設定するか、呼び出しごとに `compact_memory(routing_mode="enforce")` を渡します — `dry_run=True` と組み合わせれば、設定を変える前に分割結果をプレビューできます。引数を省略すると設定済みのモードが使われます。

enforceモードはL1の外には何も書きません。止められた項目は報告されるだけで、削除されることはありません：

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

各候補には項目のプレビュー、ルーティング先の面と所有者、規則重みスコア、理由、警告、ステータス、そして実際に書き込まれた場合の書き込み先が付きます — 一回の圧縮を後から検証できる情報量です。意図的な境界が二つあります：TODOは自動作成しません（圧縮にはTODO書き込みの契約がなく、`todo_candidates` は報告であって `add_todo` はあなたの判断です）。そしてスキル／ユーザープロフィール／エージェント自有の内容は引き続きエージェントのものです — `suggested_action` はあなたが実行するために返され、`agent_extensions` アダプタが設定されていても圧縮がそれを呼び出すことは決してありません。

旧来のフィールド（`migrated_count`、`migrated`、`errors`、`cleaned_memory`、`stats`）の意味はどちらのモードでも変わらないので、既存の呼び出し側に変更は不要です。

#### 止められた項目集合へのバックオフ

止められた項目はエージェントの記憶に残るので、**遅延**メンテナンスの発火条件は満たされたままになります。`auto_maintain_after_write` が見るのは使用率なので、`enforce` が動かさない項目で埋まった記憶ファイルは、以前は**書き込みごとに**完全な圧縮（バックアップ、分類、書き戻し）を繰り返し、そのたびに一件も移行しませんでした。

いまは圧縮が止めた集合の指紋を計算し（`blocked_set: {count, fingerprint, by_surface}` — 項目ごとの内容ハッシュとルーティング先の面を並べ替えてから計算し、タイムスタンプは含みません）、`data/auto_compaction_state.json` に覚えます。次の遅延発火は、作業する前に比較します：

| 状況 | 遅延パスの挙動 |
|------|------|
| 項目を止めた最初のenforce圧縮 | 実行し、指紋を記録し、`compact.status: "blocked_only"` を報告 |
| 同じ集合で、移行できる項目もない | **スキップ** — `compact.status: "backed_off"` に加えて `auto_compaction_backoff: {status: "blocked_set_unchanged", probe, blocked_set, classifier_inputs_changed, skipped_passes, first_seen_at, attempt_marker, hint}` |
| 止められる項目が増えた（指紋が変わった） | 次の書き込みで即座に実行；`backoff_decision: "blocked_set_changed"` |
| L1へ移行できる項目が増えた | 止められた集合が変わったかどうかに関係なく実行して移行；`backoff_decision: "l1_work_pending"` |
| 分類器の入力が変わり、ファイルは同じ | バイトを信じるのをやめて再分類します：`classifier_inputs_changed: true`、`probe: "dry_run_classification"`。止められた集合が動いていれば実行し、動いていなければバックオフして新しい入力を記録し直します |
| 止められた項目が別の場所に置かれた | 実行し、記録は消去されるので、次の冗長な項目は即座に発火する |
| *周期*トリガーだけが期限に達し、ファイルは無変更 | 前回の判定から `auto_maintain_attempt_interval_hours`（既定1時間）が経つまで保留します：`compact.status: "not_triggered"` に加えて `throttled: {trigger: "interval", last_attempt_at, retry_after, …}` |
| `advisory` モード | 変更なし — このモードでは何も止められないのでバックオフする対象がなく、状態ファイルも書かれません |
| 明示的な `compact_memory(...)` | 必ず実行されます。バックオフは便乗で走る遅延パスだけのもので、明示的な呼び出しはあなたの意思表示です |

`compact.status` は、以前は見分けがつかなかった三つの結果を分けます：`migrated`（項目がL1に入った）、`blocked_only`（実行したが非L1項目を止めることしかできなかった）、`backed_off`（実行せず、その理由を述べる）— ほかに `no_changes`、`not_triggered`、`error` があります。

状態ファイルは派生した運用状態です：バージョン付き（`schema_version`）で、記憶ファイルのパスのハッシュをキーにし、記憶本文や絶対パスではなくハッシュと件数だけを持ちます。ナレッジでもなく `MEMORY_CHANGELOG` の来歴でもないので、削除してもコストは空振りの圧縮が一回増えるだけです。見出しが決められずに拒否された項目（`refused_needs_title`）は未処理の作業として数えられます — 拒否はバックオフで黙らされることなく、報告され続けます。

**留めるべき釘は一本ではなく二本です。** 安い経路が使えるのは、記憶ファイルが記録時のものとバイト単位で同一であり、**かつ**分類器のハッシュも変わっていないときだけです。そうでなければdry runで再分類します（完全な圧縮には決してなりません）。`classifier_inputs_hash` は分類結果を動かしうるすべての入力のダイジェストです — あなたの設定でコンパイルされたマーカー語彙（`routing.extra_*_markers`、`routing.disabled_markers`）、routingの各スイッチ、スコアのしきい値、`domain_keywords`、そして `l0_tag` — タイムスタンプもパスも含みません。どれか一つでも編集すれば、次の遅延判定は古い分類器が作った集合に照合するのではなく、止められた集合を作り直します。ハッシュがまったく計算できない場合は*無効*として扱い、圧縮を実行します。

**`<home>` 配下の三つのカデンスマーカー。** `.last_auto_compact` は従来どおり「圧縮が実行された」を意味し、周期トリガーが測るのもこれです。`.last_auto_compact_attempt` は遅延パスが*判定に到達した*こと（バックオフを含む）を記録します — 抑えるのは周期トリガーだけで、しかもエージェントの記憶がそれ以降変わっていない間だけです。だからバックオフの判定が書き込みごとに再計算されることはなく、新しい冗長項目・新しいL1候補・ファイルへのどんな編集も即座に判定を受けます。`.last_l1_dedup` はL1／ベクトルストアの重複排除に独自のスケジュールを与えます。以前は圧縮マーカーを読んでいたため、長くバックオフしているインストールでは書き込みごとに全ナレッジファイルの再スキャンが走っていました。`auto_maintain_attempt_interval_hours: 0` にすれば、書き込みごとに判定する以前の挙動に戻ります。

### 派生データ：L0インデックスとベクトル

L1 Markdownファイルが唯一の真実の源です。L0インデックスとベクトルストア（`data/vectors.db`）はそこから**派生**し、いつでも再構築できます（`sync_l0_index`、`reindex_vector_store_tool`）。埋め込まれるテキストはノイズを除いた見出しを使います：`## Release process 2026-10-01 (v3.2)` は `Release process` として埋め込まれるので、定期的なエントリが日付ではなく主題でまとまります。表示用の見出しと保存されたメタデータは元のテキストのままです。アップグレード後に再インデックスすれば、既存のベクトルもきれいな見出しを使えます。

### 安全なクリーンアップの境界

自動クリーンアップは機械的で、可逆または冪等な操作に限られます：残存する `*.md.bak` を `knowledge/` から `data/backups/knowledge/` へ移す、派生データのL0とベクトルを再構築する、未完了マーカーをTODOストアに*コピー*する（決定論的なidを使うので再実行しても増えません）。意味に関わる変更はすべて安全な境界の外で、常にユーザーまたはエージェントに委ねられます：事実の削除、矛盾するセクションの統合、ドメイン間の内容移動、履歴をルールに書き換えること、TODO作成後に元の行を消すこと。`apply_safe_knowledge_cleanup_tool` はこれらのアクションを名前で拒否します。

### 関連度スコアリング

`recall_knowledge`を呼び出すと、ファイルは以下の基準でスコアリングされます：

1. **ファイル名の一致** (+10ポイント) — キーワードがファイル名に含まれる
2. **見出しの一致** (+3ポイント) — キーワードが`## 見出し`に含まれる
3. **コンテンツの出現頻度** (出現ごとに+0.5、上限5) — キーワードの出現回数

結果はスコア順にソートされ、ファイル全体ではなく一致した`##セクション`のみが返されます。

### 名前空間による分離（v0.6.0）

`LAYERED_MEMORY_NAMESPACE` を設定すると、エージェントごとに知識を分離できます：

```
knowledge/
├── shared/           ← 共通知識、すべてのエージェントから見える
│   ├── infrastructure.md
│   └── coding-standards.md
├── claude/           ← Claude Desktop専用の知識
│   └── claude-specific.md
├── cursor/           ← Cursor IDE専用の知識
│   └── cursor-config.md
└── hermes/           ← Hermes Agent専用の知識
    └── hermes-setup.md
```

各エージェントはまず自分の名前空間を見て、次に `shared/` にフォールバックします。ファイル名が衝突した場合は名前空間が優先されます。読み取り／検索／注入のすべてのツールが両方のディレクトリを自動的にマージします。

```bash
# Claude Desktopの設定
LAYERED_MEMORY_NAMESPACE=claude layered-memory-mcp

# 後方互換：既定の "shared" は分離なし
layered-memory-mcp
```

### L0インデックス形式

2つの形式をサポートしています：

| 形式 | 例 | 最適な用途 |
|------|---|----------|
| `hermes` | `[L0索引] infra: servers, DB → knowledge/infra.md` | Hermes Agentのメモリ注入 |
| `generic` | `[infra.md] Server Configuration → proxy, db, deploy` | スタンドアロン/他エージェント向け |

`LAYERED_MEMORY_L0_FORMAT`環境変数、または`l0_format`コンストラクタ引数で設定できます。

### セッション圧縮

`scan_recent_sessions`ツールはcronジョブでの自動化を想定して設計されています：

1. 過去N日間のセッションファイルをスキャン
2. ユーザーメッセージ、アシスタントのトピック、ツール呼び出しを抽出
3. AIが分析するための構造化JSONを返す
4. AIが安定した知識を特定し、`inject_knowledge`でL1ファイルに書き込む

これにより**自己改善型のメモリシステム**が実現します — 会話からより多くの知識が抽出されるにつれて、エージェントは時間とともに賢くなります。

## エージェント互換性

Layered MemoryはMCPサーバーです — MCP互換のエージェントならどれでも動作します。

| エージェント | 設定方法 | 備考 |
|------------|---------|------|
| **Hermes Agent** | `config.yaml` → `mcp_servers` | ネイティブMCPクライアント、メモリ経由でL0自動注入 |
| **OpenClaw** | `openclaw mcp set` | 内蔵ベクトルメモリを補完 |
| **Claude Desktop** | `claude_desktop_config.json` | 完全なMCPサポート、ツール呼び出しでL0利用 |
| **Cursor** | Settings → MCP | 完全なMCPサポート |
| **Codex CLI** | Codex MCP設定 | 完全なMCPサポート |
| **任意のMCPクライアント** | stdioまたはHTTPトランスポート | 標準MCPプロトコル |

### Layered Memoryと内蔵メモリの使い分け

ほとんどのエージェントには**限られた永続メモリ**（毎ターン2〜4KB）しかありません。Layered Memoryは次の方法でこれを解決します：

1. **インデックスとコンテンツの分離** — L0は小さく保ち（エージェントメモリに収まる）、L1が無制限の知識を保持
2. **オンデマンド読み込み** — エージェントは必要な時に必要なものだけ読み取る
3. **自己改善** — セッション圧縮が自動的に新しい知識を随時抽出

### 統合パターン

```
エージェント (2KBメモリ制限)
  └── L0インデックス (毎ターン注入、~500バイト)
        ├── [L0] infrastructure: servers, DB → knowledge/infrastructure.md
        ├── [L0] api: REST規約 → knowledge/api-conventions.md
        └── [L0] dev: コードスタイル、テスト → knowledge/development.md
              │
              ↓ (recall_knowledgeでオンデマンド)
        L1ナレッジファイル (無制限、キーワードで読み込み)
```

## 認知意思決定フレームワーク

記憶面モデルは、エージェントが規律ある意思決定プロセスに従う場合にのみ最大の価値を発揮します。このフレームワークはエージェントのシステムプロンプトに注入するか、`cognitive_decision_prompt` MCP プロンプトで読み込むことで、一貫した動作を保証します。

### 意思決定ツリー

```
エージェントが問題に直面、またはリクエストを受信
  │
  ├─ ステップ 1: L0インデックスをスキャンして関連ドメインを探す
  │
  ├─ ステップ 2: 一致するものが見つかったか？
  │   ├─ はい → 対応するL1ナレッジファイル（手順ならエージェント自身のスキルも）を読み込む
  │   │   │
  │   │   ├─ ナレッジで解決 → そのまま使用。推測でバイパスしない。
  │   │   ├─ ナレッジが部分的に有効 → 該当部分を使用し、エントリを強化
  │   │   └─ ナレッジが不十分 → 新問題として扱う（ステップ 3）
  │   │
  │   └─ いいえ → 新問題として扱う（ステップ 3）
  │
  ├─ ステップ 3: 新しい問題/要件として処理
  │   標準ツールと推論で解決
  │
  └─ ステップ 4: 解決後の評価 — 保存する価値があるか、どこに置くか？
      ├─ 持続する事実 / 決定 / アーキテクチャ → inject_knowledgeでL1へ
      ├─ 未完了の作業（保留、ブロック）       → TODOストアへ（L1には置かない）
      ├─ 再利用可能な手順 / コマンド列        → エージェント自身のスキル面へ
      ├─ 恒常的なユーザー設定                → 確認のうえ自身のプロフィール面へ
      ├─ 使い捨ての出力や生ログ              → 保存しない
      └─ 迷ったら                           → classify_memory_item（読み取り専用）
```

### なぜこれが重要か

この意思決定フレームワークがないと、エージェントは次のような問題を起こしやすくなります：
- **既存のナレッジを無視** — L0インデックスを見てもL1ファイルの読み込みを忘れ、推測で時間を浪費
- **同じ過ちを繰り返す** — 解決済みの問題が記録されず、次回もゼロから学習
- **既存の規約をバイパス** — セッションごとにゼロから始め、蓄積されたナレッジの上に構築しない

このフレームワークは、記憶システムを受動的なストレージから**能動的な認知ループ**へと変えます：参照 → 行動 → 学習 → 改善。

### 統合方法

エージェントのシステムプロンプトに以下を追加：

```
あなたはレイヤードメモリシステムを使用しています。問題に取り組む前に：
1. L0インデックスで一致するドメインを確認
2. 一致する場合、行動前にL1ナレッジを読み込んで従う
3. 一致しない場合、通常通り解決
4. 解決後は振り分ける：持続する事実はinject_knowledgeでL1へ、未完了の作業は
   TODOストアへ、手順と恒常的なユーザー設定は自身の面へ。迷ったら
   classify_memory_itemを呼ぶ
```

または、内蔵のMCPプロンプト `cognitive_decision_prompt` を使用して、実行時に完全な意思決定フレームワークを取得できます。

## 開発

```bash
# クローン
git clone https://github.com/LAIguapi/layered-memory-mcp.git
cd layered-memory-mcp

# 開発モードでインストール
pip install -e ".[dev]"

# テスト実行
pytest

# ローカルで実行
python -m layered_memory_mcp.server
```

## 変更履歴

完全なリリース履歴は [CHANGELOG.md](CHANGELOG.md) を参照してください。`3.4.7` の主な内容：

- ライフサイクル治理：構造化された推奨アクション（`move_to_todo` / `rewrite_as_current_rule` / `keep_with_reason`）、マーカー語彙は設定可能
- `plan_knowledge_cleanup_tool` と `apply_safe_knowledge_cleanup_tool`：読み取り専用の計画と、安全なアクションのdry-run優先の実行
- 知識のバックアップを稼働中の知識ディレクトリの外、`data/backups/knowledge/` に集約
- ベクトルに埋め込むテキストの見出しから日付・バージョンなどのノイズを除去
- globベースの雑多ファイルスキャン（`promotion.watch_patterns`）、英語・中国語のデフォルト付き

## ライセンス

MIT License — 詳細は[LICENSE](LICENSE)を参照してください。

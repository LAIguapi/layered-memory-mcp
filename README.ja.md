# Layered Memory MCP Server

> 4層知識アーキテクチャでAIエージェントの記憶をトークン制限の先へ

[**English**](README.md) | [**中文**](README.zh-CN.md) | [**한국어**](README.ko.md)

[![PyPI version](https://img.shields.io/pypi/v/layered-memory-mcp.svg)](https://pypi.org/project/layered-memory-mcp/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 課題

AIエージェントには**限られた記憶容量**しかありません。通常、毎ターン注入される永続コンテキストは2〜4KB程度です。容量がいっぱいになると、エージェントは他のすべてを忘れてしまいます。プロジェクトの設定、ユーザーの好み、API規約、ドメイン知識などを保存しようとすると、常に容量制限との戦いになります。

## 解決策

**Layered Memory**は、知識を4つの階層に整理し、即時性と引き換えに容量を確保します。

```
┌─────────────────────────────────────────────────────┐
│  L0 — インデックス層 (2-4KB、毎ターン注入)            │
│  純粋なポインタ：「どの知識がどこにあるか」             │
├─────────────────────────────────────────────────────┤
│  L1 — ナレッジファイル (無制限、オンデマンドで読み込み)  │
│  構造化Markdown：設定、規約、事実                      │
├─────────────────────────────────────────────────────┤
│  L2 — スキル層 (必要時に読み込み)                      │
│  手順、ワークフロー、ツール固有の知識                   │
├─────────────────────────────────────────────────────┤
│  L3 — 生セッション (まれに検索)                        │
│  完全な会話履歴、キーワードで検索可能                   │
└─────────────────────────────────────────────────────┘
```

**L0は目次。L1は本棚。L2はレシピ集。L3は日記です。**

## 特徴

- **スマートナレッジ注入（Smart Knowledge Injection）** — 書き込み一回で即座に可視化：重複排除、セクションターゲティング、L0インデックス自動同期付き
- **キーワード検索** — 関連度スコアリング付きで、全L1ファイルから関連知識を検索
- **セッションスキャン** — 直近のエージェントセッションから知識候補を抽出
- **ヘルス診断** — L0↔L1の整合性チェック、孤立ファイルや古いエントリを検出
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
| `LAYERED_MEMORY_DEDUP_THRESHOLD` | 重複排除の類似度しきい値（0.3〜1.0） | `0.7` |
| `LAYERED_MEMORY_L0_FORMAT` | L0インデックス形式：`hermes`または`generic` | `hermes` |

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
← L0↔L1の整合性、孤立ファイル、古いエントリ、ファイル健全性をチェック
```

### 4. セッション圧縮（cronジョブ）

会話から新しい知識を抽出するために、毎日のcronジョブを設定：

```
1. scan_recent_sessions → セッションサマリーを取得
2. AIがサマリーを分析 → 安定した事実を特定
3. 新しい事実 → inject_knowledgeで書き込み（L0自動同期）
4. L0インデックス → 常に最新状態を維持
```

### 5. レガシーCRUD（引き続き利用可能）

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

### 書き込みツール

| ツール | 説明 |
|-------|------|
| **`inject_knowledge`** | **プライマリ書き込みパス** — 重複排除、セクションターゲティング、L0自動同期付きのスマート注入 |
| `create_knowledge_file` | 新しい.mdファイルを作成（L0自動同期） |
| `update_knowledge_file` | 既存ファイルを上書き（L0自動同期） |
| `delete_knowledge_file` | ファイルを削除（L0自動同期） |

### 管理ツール

| ツール | 説明 |
|-------|------|
| `sync_l0_index` | L1ファイルからL0インデックスを手動再構築（`dry_run`対応） |
| `validate_knowledge` | ヘルスチェック：L0↔L1整合性、ファイル品質、重複検出 |
| `manage_l0_entry` | 個別のL0エントリの追加/削除/置換 |

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

### なぜ4層なのか？

| 階層 | コスト | 容量 | ユースケース |
|------|-------|------|-------------|
| L0 (インデックス) | 毎ターンのトークン | ~2KB | 素早い検索テーブル |
| L1 (ナレッジ) | 1ファイル読み込み | 無制限 | 構造化された事実 |
| L2 (スキル) | 1スキル読み込み | 無制限 | 手順 |
| L3 (セッション) | 全文検索 | 無制限 | 過去の記録 |

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

### 関連度スコアリング

`recall_knowledge`を呼び出すと、ファイルは以下の基準でスコアリングされます：

1. **ファイル名の一致** (+10ポイント) — キーワードがファイル名に含まれる
2. **見出しの一致** (+3ポイント) — キーワードが`## 見出し`に含まれる
3. **コンテンツの出現頻度** (出現ごとに+0.5、上限5) — キーワードの出現回数

結果はスコア順にソートされ、ファイル全体ではなく一致した`##セクション`のみが返されます。

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

4層アーキテクチャは、エージェントが規律ある意思決定プロセスに従う場合にのみ最大の価値を発揮します。このフレームワークはエージェントのシステムプロンプトに注入するか、`cognitive_decision_prompt` MCP プロンプトで読み込むことで、一貫した動作を保証します。

### 意思決定ツリー

```
エージェントが問題に直面、またはリクエストを受信
  │
  ├─ ステップ 1: L0インデックスをスキャンして関連ドメインを探す
  │
  ├─ ステップ 2: 一致するものが見つかったか？
  │   ├─ はい → 対応するL1ナレッジファイル/L2スキルを読み込む
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
  └─ ステップ 4: 解決後の評価
      保存する価値があるか？
      ├─ はい → inject_knowledgeでL1に書き込むか、L2（スキル）として保存
      └─ いいえ → 終了
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
あなたは4層レイヤードメモリシステムを使用しています。問題に取り組む前に：
1. L0インデックスで一致するドメインを確認
2. 一致する場合、行動前にL1/L2を読み込んで従う
3. 一致しない場合、通常通り解決
4. 解決後、inject_knowledgeで新しいナレッジを保存
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

### v0.5.0 — 書き込み一回で即座に可視化

- **`inject_knowledge` ツール** — 重複排除、セクションターゲティング、L0自動同期付きのプライマリ書き込みパス
- **`sync_l0_index` ツール** — dry_runプレビュー付きのL0インデックス手動再構築
- **`validate_knowledge` ツール** — L0↔L1整合性チェック、ヘルス診断
- **`manage_l0_entry` ツール** — 個別L0エントリの追加/削除/置換
- **L0自動同期** — すべての書き込みツール（create/update/delete/inject）が自動的にL0インデックスを同期
- **重複排除エンジン** — SequenceMatcherベースの類似度検出、設定可能なしきい値
- **ファイルロック** — 並行書き込みの安全性のためfcntl.flockを使用
- **ナレッジウォッチャー** — ファイル変更がデバウンス付きL0同期をトリガー（HTTPモード）
- **`cognitive_decision_prompt`** — 内蔵の意思決定フレームワークプロンプト

### v0.4.0 — 初回リリース

- 4層ナレッジアーキテクチャ（L0/L1/L2/L3）
- 関連度スコアリング付きキーワード検索
- セッションスキャンと圧縮
- MCPプロトコルサポート（stdio + HTTP）
- 外部依存関係ゼロ（コアエンジン）

## ライセンス

MIT License — 詳細は[LICENSE](LICENSE)を参照してください。

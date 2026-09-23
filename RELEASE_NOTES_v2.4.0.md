# MLXBar v2.4.0

v2.4.0は、v2.3.0までに搭載したOMLX相当機能（Paged KV再利用、モデルプール並行生成、
メモリガード、永続プロンプトキャッシュ、RAG、Adaptive Memory）の設定キーを、
GUIの「Settings…」と`mlxbarctl`のnamedコマンドの両方から管理できるようにします。
新規エンジン機能の追加はありません。既定値はすべてv2.3.0と同一で、既存の
`config.json`はそのまま動作します。

## 操作

- 「Settings…」>「モデル」：常駐上限・同時生成ヘッドルーム・モデル別メモリ上限、
  プロンプト入力上限、生成タイムアウト、メモリガード比率、アダプティブメモリ。
- 「Settings…」>「APIサーバー」：要求上限（サイズ・同時接続数）。
- 「Settings…」>「キャッシュ」：プロンプトキャッシュ詳細とPaged KVの状態表示。
- 「Settings…」>「知識ベース」：埋め込みタイムアウト・バッチサイズ・コレクション上限。
- 「Settings…」>「一般」：ログレベル・前回モデル復元。「LM Studio」：連携の有効化とフォルダ。
- CLI：`config set-model-pool --per-generation-headroom-gb`、`prompt-cache set`の詳細4種、
  新規`config set-generation-limits`／`config set-api-limits`／`config set-log-level`、
  `config set-flag preload-last-model|lmstudio-enabled`、`lmstudio set-enabled|set-folder`、
  `config set-adaptive-memory --fallback-to-exact`。

将来phase用のゲート（RAMホット層、`hybridKVReuse`）は状態表示のみで、有効化手段を
提供しません。`hybridKVReuse: true`はサーバ検証で拒否されます。

詳細は `DESIGN_v2.4.0.md`、`TEST_PLAN_v2.4.0.md` を参照してください。

## 検証

- Python回帰テスト: 516 passed（ベースライン492＋新規24、固定順・ランダム順）
- Swift Debug / Release build: 成功
- 日英ローカライズの重複キー検査: 新規重複なし

DMG SHA-256: `3e92cea8221f381f21f1900f73ad53190e55d3f1c3253606fea9e936a99c693d`

> 本ビルドはad-hoc署名でApple公証は行っていません。初回起動時は「システム設定 →
> プライバシーとセキュリティ」から起動許可が必要になる場合があります。

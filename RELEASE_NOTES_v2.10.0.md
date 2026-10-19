# MLXBar v2.10.0 リリースノート

**リリース日**: 2026-10-19
**版数**: app 2.10.0 / build 55
**Coordinator**: 2.10.0

---

## 🎉 主な新機能

### 1. VLM誤起動ガード＋明示エンジン選択

ビジョンモデルをmlx-lmに誤って渡して起こる失敗を止め、かつユーザーが常に意図したエンジンを最優先で実行できるようにしました。

#### カタログ分類 v3（`CATALOG_CLASSIFIER_VERSION=3`）
- `config.json` の vision 系キー拡張（`vision_tower` / `image_token_id` / `mm_projector_type` / `video_token_id` 等）
- アーキ名トークン判定（qwen2_vl、llava、internvl、mllama、paligemma、molmo、pixtral、minicpm(v)、smolvlm、deepseek_vl、aria、ovis など）
- mlx-vlm がネイティブ実装を持つテキスト専用アーキテクチャ（laguna）を直接 mlx-vlm へ分類
- 起動時に旧判別の行を自動で再スキャン

#### 明示エンジン選択
- モデル一覧の行メニューから「mlx-lm で読み込む」「mlx-vlm で読み込む」を指定可能
- **明示選択を常に優先**: エンジン再導出なし・フォールバックなし・カタログ書換なし（読み込み時に `allow_vlm=True` を渡し、ユーザーの選択をそのまま実行）
- **自動ロード時のみ**アシスト: ロード時に再検出し、失敗時は mlx-lm⇄mlx-vlm へ**1回だけ**フォールバック。成功後はカタログ行を自己修復
- mlx-lm アダプタは自動ロード時の vision モデルを `MODEL_INCOMPATIBLE` でガード

### 2. oMLX を MLXBar 内でセットアップ

「設定…」→「ランタイム」ページの新しい **oMLXカード** から、インストールからサーバー起動・停止までを完結できます。

- 状態表示: oMLX CLI 有無・バージョン、Homebrew 有無、リリースページ案内
- `brew install jundot/omlx/omlx` をMLXBarから実行（進捗メッセージ・最大1時間・途中キャンセル可）
- `omlx start` / `omlx stop` を実行し、起動後は設定URLへの到達を最大2分待機
- すべてジョブ化（`omlx_install` / `omlx_start` / `omlx_stop`）。進捗・経過秒・キャンセルボタンつきで、同一アクションの重複実行は自動でまとめる
- 未開始ジョブは CLI からも操作可能: `mlxbarctl omlx install|start|stop [--wait]`
- oMLXサーバーが未インストールでも他機能への影響なし

### 3. oMLX autoConnect

`models.omlx.autoConnect` を有効にすると、Coordinator 起動時に設定URLへ自動接続します（20秒間隔・最大6回のbest-effort。失敗はログのみで起動を妨げない）。

---

## 🐛 修復した実バグ（v2.9.0由来）

- **mlx-vlm ワーカーへのロードがすべて失敗していた**: ワーカーへの引数変更に mlx-vlm アダプタが未対応で、ロードが必ず `TypeError` になっていた
- **`engine="auto"` が拒否されていた**: モデルプール経由の auto 指定が正規化されず「利用可能なエンジンがありません」で止まっていた
- 生成ジョブのキャンセルが queued や「シグナル送信前の running」で呼ばれると状態が書き残る問題を修正

---

## 🚚 配布訂正

- v2.9.0 のDMG実体には `2.8.1 / build 54` が入ったまま出荷されていました（packaging 更新漏れ）。本版から `2.10.0 / build 55` に是正しています。仕様・機能に復帰操作は不要です。

---

## 📋 その他

- `/omlx/status` とモデル一覧APIを正形に統一（`memoryBytes` / `pleOffload` / `available` / `providerKey` / `contextLength`、top-level `connected` / `version` / `error` / `serverUrl` / `totalMemoryBytes`）
- oMLX設定のモデル行は `available` で選択可否を判定、ルーティング変更は単一キーPATCH（並行編集の消失を防ぐ）
- 日本語・英語UI完全対応（新規文字列20キー）

---

## ⚠️ 既知の制限

- 明示エンジン選択はロード時の一回限りで、カタログの engine 列は自動ロード後にしか修復されません（仕様）
- `omlx start` は CLI を1回実行するのみ。マシン再起動後の自動再起動は oMLX 側（`brew services` 等）設定に依存します
- oMLXカードのインストールには Homebrew が必要（無い環境はGitHub Releasesからの手動インストールを案内）

---

## 📦 インストール・アップデート

### 対象環境
- macOS 14.0以降（Apple Silicon）

### インストール手順
1. GitHub Releasesから`MLXBar-2.10.0.dmg`をダウンロードして開く
2. `MLXBar.app`を「アプリケーション」へコピー
3. メニューバーから「モデルを選択…」でロード
4. oMLXを使う場合は「設定…」→「ランタイム」のoMLXカードからインストール・起動

### アップデート時の注意
- 既存設定・モデル・ランタイムはそのまま引き継がれます
- v2.10.0 の新機能は既定の動作を変更しません（フォールバック・ガードはオプション／明示操作由来のみ）

---

## 🧪 テスト結果

- Python回帰テスト: **721 passed**（新設41件＋26件を含む、既存全件通過）
- Swiftビルド: **成功**
- DMG検証: `hdiutil verify` VALID、`codesign --verify --deep --strict` 通過
- 実機確認: `/Applications` 入替・Coordinator version 2.10.0・oMLX requirements 応答確認
- 多言語UI: 日本語/英語完全対応確認済み

---

## 📝 関連ドキュメント

- 設計書: [`DESIGN_v2.10.0.md`](DESIGN_v2.10.0.md)
- テスト計画: [`TEST_PLAN_v2.10.0.md`](TEST_PLAN_v2.10.0.md)
- oMLX公式: https://github.com/jundot/omlx

---

## 🔐 署名・配布

- **ad-hoc署名**（Developer ID署名・公証なし）
- 初回起動時に「システム設定 → プライバシーとセキュリティ」からの許可が必要
- GitHub ReleasesでDMG + SHA-256を配布

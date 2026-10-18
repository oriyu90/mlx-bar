# MLXBar v2.9.0 リリースノート

**リリース日**: 2026-10-18
**版数**: app 2.9.0 / build 55
**Coordinator**: 2.9.0

---

## 🎉 主な新機能: oMLX統合

MLXBarに新しい推論ランタイムとして **oMLX** を追加しました。oMLXはApple Siliconに特化した高速化機能（PLE SSD Offload、Lightning MTP、TurboQuant KV、ANE Prefill、MoE Expert Offloadなど）を持つ推論サーバーです。

### oMLX連携の特徴

- **モデル単位のルーティング**: 全モデルをoMLXへ移行するのではなく、ユーザーが明示的に指定したモデルのみoMLXへルーティング
- **既存ランタイムとの共存**: mlx-lm、mlx-vlm、LM Studioをこれまでどおり使用可能
- **統合メモリ管理**: oMLXで使用しているメモリもMLXBar全体のメモリ予算に含めて管理
- **外部プロセスとして動作**: oMLXサーバーは別プロセスで稼働し、MLXBarはHTTP APIで制御

---

## 📋 新機能詳細

### 1. oMLX設定タブ（設定画面）

設定画面に専用の「oMLX」タブを追加しました。

#### 接続設定
- oMLX連携の有効/無効
- oMLXサーバーURL（初期値: `http://127.0.0.1:8000`）
- APIキー設定（表示/非表示切替対応）
- 接続テストボタン（リアルタイム状態表示）
- 接続状態表示（未接続/接続中/接続済み/エラー）
- oMLXバージョン表示

#### モデルルーティング
- oMLXから取得したモデル一覧を表示
- 各モデルについて「oMLXで実行する」をON/OFFで指定
- 初期状態はすべてOFF
- 設定はアプリ再起動後も保持
- モデルIDで対応付け（名前変更に強い）
- oMLX側で利用できないモデルは選択不可（明確な警告表示）
- ロード済みモデルはメモリ使用量(GB)、生成速度(tok/s)、MTP、PLE Offload状態を表示

#### 状態表示
- 接続状態、バージョン、エラー詳細
- ロード済みモデル一覧と詳細メトリクス

### 2. モデル管理への統合

- **ロード/アンロード**: MLXBarのGUI/CLIからoMLXモデルをロード・アンロード可能
- **推論ルーティング**: oMLX指定モデルへのチャット/API要求は自動的にoMLXへ送信
- **ストリーミング対応**: SSEによるトークン単位のストリーミング生成
- **Tool Calling**: oMLXから返されるtool callを既存API形式へ中継
- **生成キャンセル**: 進行中の生成をキャンセル可能

### 3. 統合メモリ管理【重要】

oMLXで使用しているメモリも、MLXBar全体のメモリ使用量・メモリ予算に**必ず含めて管理**します。

- モデルロード前のメモリ予算判定でoMLX使用分を考慮
- 常駐メモリ総計（`_resident_charge`）にoMLX分を加算
- 新規ロード許可判定（`_admit`）時にoMLX使用メモリを予算から差引
- 96GB環境でoMLXが約80GB使用中の場合、MLXBar側で大型モデルを同時ロードしてメモリ不足に陥らないようガード
- 他モデルを勝手にアンロードせず、既存の常駐固定設定も尊重

### 4. ステータス表示統合

- メニューバーおよびモデル管理画面でoMLX状態を確認可能
- モデルごとにランタイム（oMLX）、ロード状態、生成速度、メモリ使用量、MTP/PLE Offload状態を表示
- MLXBar全体のメモリ表示にoMLX分も含む

### 5. CLI対応（`mlxbarctl`）

```
mlxbarctl omlx connect           # 接続テスト
mlxbarctl omlx status            # ステータス表示
mlxbarctl omlx models            # モデル一覧
mlxbarctl omlx load <model_id>   # モデルロード
mlxbarctl omlx unload <id> [--force]  # アンロード
mlxbarctl omlx set-enabled <true|false>  # 有効/無効
mlxbarctl omlx set-url <url>     # サーバーURL設定
mlxbarctl omlx set-routing <model_id> <true|false>  # ルーティング設定

mlxbarctl secrets get-omlx-token
mlxbarctl secrets set-omlx-token [token]
```

### 6. API対応

- **OpenAI互換API** (`/v1/chat/completions`) からoMLXモデルを利用可能
- **Anthropic互換API** (`/anthropic/v1/messages`) からoMLXモデルを利用可能
- Management APIでoMLX操作可能（`/api/v1/omlx/*`）

---

## 🔧 改善・修正

### 設定管理
- oMLX設専用セクション `models.omlx` を追加
- APIキーは設定JSONに含めず、別ファイル（`control/omlx-token`）で安全に管理
- 設定検証でURL形式、boolean型、routingオブジェクト構造をチェック

### メモリ安全性
- oMLXメモリ取得失敗時は「取得不可」として扱い、推測で確定値としない
- メモリ逼迫時は既存のガード（pressure >= 2等）がoMLXモデルを含めて動作

### 互換性
- oMLX無効時（既定）は従来どおり動作
- 既存API仕様・ステータスコード・SSEフォーマット・設定スキーマ・既定値は不変
- 既存テスト全654件継続通過

---

## ⚠️ 既知の制限

1. **oMLXインストール管理**: 現版ではoMLXのインストール・更新はMLXBar外部で行う必要があります（Homebrew等で別途インストール）
2. **oMLX固有設定**: PLE/MTP/TurboQuant等の詳細設定はoMLX側で管理。MLXBar側は読取専用表示のみ
3. **生成キャンセル**: oMLXにサーバー側キャンセルAPIがないため、クライアント側ストリームクローズのみ対応
4. **クラスタ/分散推論**: oMLXのクラスタ機能は未対応

---

## 📦 インストール・アップデート

### 対象環境
- macOS 13.0以降（Apple Silicon推奨）
- oMLXサーバー別途インストール必要（`brew install jundot/omlx/omlx` 等）

### インストール手順
1. oMLXサーバーをインストール・起動
2. MLXBar v2.9.0をインストール
3. 設定画面の「oMLX」タブで接続設定
4. 接続テストで疎通確認
5. 使用したいモデルで「oMLXで実行する」をON
6. モデルをロードして使用開始

### アップデート時の注意
- 既存設定・モデル・ランタイムはそのまま引き継がれます
- oMLX連携は既定で無効のため、明示的に有効化するまで既存動作に影響しません

---

## 🧪 テスト結果

- Python回帰テスト: **654 passed**（新規失敗なし）
- Swiftビルド: **成功**（警告なし）
- CLI全コマンド: 動作確認済み
- 多言語UI: 日本語/英語完全対応確認済み

---

## 📝 関連ドキュメント

- 設計書: [`DESIGN_v2.9.0.md`](DESIGN_v2.9.0.md)
- テスト計画: [`TEST_PLAN_v2.9.0.md`](TEST_PLAN_v2.9.0.md)
- oMLX公式: https://github.com/jundot/omlx
- oMLX Admin API: https://github.com/jundot/omlx/blob/main/docs/admin-api.md

---

## 🔐 署名・配布

- **ad-hoc署名**（Developer ID署名・公証なし）
- 初回起動時に「システム設定 → プライバシーとセキュリティ」からの許可が必要
- GitHub ReleasesでDMG + SHA-256を配布

---

## 🙏 謝辞

oMLXプロジェクト（jundot/omlx）の優れた実装とドキュメントに感謝します。
# MLXBar v1.7.1 設計書（複数モデル表示 / エラー日本語化 / OpenAI互換クライアント対応）

更新日: 2026-08-29
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

v1.7.0（`ModelPoolSupervisor` によるモデル間同時生成）を前提に、3件のバグ修正と1件の
互換性精査を行う。**ランタイム挙動は変えない。** `generationConcurrency == 1` は v1.6.2 と、
`models.pool.enabled == false` は v1.6.1 と、それぞれ等価のままである。OpenAI 互換の入口
（`/v1/models` のルーティング、Chat Completions のモデル解決、128 tools 上限、bearer 認証、
SSE keep-alive コメント）と設定 schema version 1 も不変である。

非目的: 新しい設定キーの追加、プールのスケジューリング／メモリガードの変更、CLI の変更、
サーバ側 i18n インフラの導入、`DESIGN_v1.7.0.md` の同時生成設計の見直し。

## 2. 複数モデル表示（Issue 1）

`GET /api/v1/status` は v1.6.2 から全常駐モデルを `loadedModels`（配列）で返している
（`ModelPoolSupervisor.status()`）。各要素は子 Worker の `loadedModel` に
`poolState` / `memoryReservationBytes` / `activeLeases` / `laneQueueDepth` /
`keepLoaded` / `idleExpiresAt` / （LM Studio のとき）`memoryManagedBy` を付与したものである。

v1.7.1 では GUI 表示層のみを変更する。バックエンドは変更しない。

- `MenuBarViewModel` に `struct ResidentModel` と `@Published var residentModels` を追加し、
  `refreshStatus()` で `loadedModels` を復元する。旧コーディネータ（`loadedModels` 無し）では
  単数 `loadedModel` から1件だけ合成する。
- 単数 `loadedModel` は今後も「プライマリ（直近使用モデル）」でヘッダー専用。既存の
  `loadedName` / `loadedEngine` / `residentModelCount` などのフィールドは意味も設定箇所も
  変更しない。
- `MenuBarView` は `residentModels.count > 1` のときだけ一覧セクションを描画する。各行に
  エンジン・メモリ予約量・常駐維持（ピン）トグル（`setModelPin` を再利用）・その行だけの
  アンロード（`unloadModel(id)` → 既存 `POST /api/v1/models/{id}/unload`）。`poolState` が
  `external`（LM Studio 管理）の行はアンロード／ピンを出さない。常駐が既定上限の2を大きく
  超えて設定されている場合に備え、5件以上のときだけ `ScrollView`（最大高さ180pt）で囲む。
- 既存「アンロード」（全解放）は複数常駐時のみ「すべてアンロード」表記に変える。動作は不変。

不変条件: 単一常駐時の見た目は v1.7.0 と同一。`setIfChanged` 経由の差分更新を維持し、
`MenuBarExtra` の window スタイルを不安定化させる内容チャーンを増やさない（`mlx-bar.md`）。

## 3. エラーメッセージの日本語化（Issue 2）

### 方針

サーバ側に i18n インフラを持ち込まない。コーディネーターと各 Worker は今後も「安定した
機械可読 `code` ＋ 日本語 `message`」を返すだけとし、**表示言語の出し分け（ja/en）は GUI が
`code` から辞書引きで行う**。上流ライブラリ（mlx-lm / mlx-vlm / transformers）の英語例外文言に
表示を依存させない。外部 API 利用者に届くエラー本文の形（`{"error": {...}}` / `{"detail": {...}}`）
と `code` 値は不変で、`message` の言語だけが変わる。

### GUI 側（一次対策）

- 新規 `Sources/MLXBar/Services/ErrorText.swift`。`CoordinatorErrorText.table` が `code` →
  (日本語, 英語) を保持し、`resolve(code:serverMessage:language:)` が
  「既知 code の訳文 → サーバ message → code → 汎用文」の順で解決する。
- `ClientError` に `case api(code:message:)` を追加（`command(String)` は互換のため残す）。
  `CoordinatorClient.apiError(in:)` が `detail.code` と `detail.message` の両方を返す。
- `MenuBarViewModel.presentError(_:)` を全 `catch` の共通経路にする。`ClientError.api` は
  `guiLanguage` で解決、その他の `ClientError` は `errorDescription`、想定外の
  `URLError`/デコード失敗などはシステムロケール文字列を出さず汎用文にする。
- 生成ストリームの `error` イベントは `StreamEvent.code` を新設して同じ resolver に通す。

### サーバ側（副対策：`message` の存在保証）

- `make_management_app` の catch-all は `message` 固定文＋クラス名を `detail` へ。
- `RequestValidationError` ハンドラは `message` 固定文＋pydantic の英語文を `detail` へ。
- `detail={"code": "X"}` だけだった管理 API（`MODEL_NOT_FOUND` / `INVALID_ENGINE` /
  `JOB_NOT_FOUND` など）と OpenAI 入口の `AUTHENTICATION_FAILED` に日本語 `message` を付与。
- `Workers/common/server.py` の RPC 例外・生成例外は、英語原文を `detail` へ退避し、
  `message` を分類済み日本語文（メモリ不足 / チャットテンプレート / ファイル欠落 など）にする。
  例外の `code` は従来どおり（`MODEL_INCOMPATIBLE` の retry 分岐を壊さない）。

新しい `MLXBarError` code を追加したら `ErrorText.swift` の `table` にも追加すること
（`mlx-bar.md` に明記）。

## 4. OpenAI 互換クライアント対応（Issue 3）

対象クライアント: Zed（OpenAI互換プロバイダ）、Cline（OpenAI Compatible）、
OpenCode（Vercel AI SDK `@ai-sdk/openai-compatible`）。

1. **`max_tokens` 省略時のフォールバック。** `max_tokens` も `max_completion_tokens` も
   無い要求は、512 ではなく `workers.effective_max_tokens()`
   （＝`min(generation.maxTokens, modelMaxTokens)`、既定 8192）を既定にする。
   明示値は従来どおり Worker 側 `_validate_generation` が `min(値, effective_limit)` で clamp。
   `effective_max_tokens` を持たない Worker 実装では 512 にフォールバック。
   オーナー確認済みの意図的な既定変更（エージェント用途の応答途中終了への対応）。
2. **単一常駐フォールバックルーティング。** `_ensure_requested_model` で、`find_loaded_model` /
   `loaded` のいずれにも一致せず `loaded_models()` が**ちょうど1件**、かつ
   `autoLoadOnAPIRequest` 有効なら、その1件を返す（自動ロードも `ENGINE_BUSY` もしない）。
   常駐2件以上のときの解決ロジック（自動ロード / `ENGINE_BUSY` / `MODEL_NOT_FOUND`）は不変。
3. **ストリーミング `delta.role`。** tool call デルタで `role: "assistant"` を最初の1チャンク
   のみに付ける（OpenAI 準拠。厳格な SDK パーサ対策）。`_tool_call_stream_chunks` 経路は
   元から最初のみ role なので、そこを通ったら同じフラグを立てて後続の再送を防ぐ。
4. **未知エンドポイント。** `POST /v1/completions` と未知パスを OpenAI エラー形式
   （`code: UNSUPPORTED_ENDPOINT` / `HTTP_404`）で返す。`main.py` に
   `StarletteHTTPException` ハンドラを追加（`fastapi.HTTPException` ハンドラは routing 由来の
   bare 404 を捕まえない）。

精査のみで変更なし: `GET /v1/models` の形（`{object:"list", data:[...]}`）、SSE
`chat.completion.chunk` の形状、`stream_options.include_usage` の最終 usage チャンク、
`reasoning_content` デルタ、128 tools 上限、`response_format` 非対応（設計どおり「黙って
無視しない」）。

## 5. 検証

`TEST_PLAN_v1.7.1.md` を参照。Python 回帰 316 件（v1.7.0 の 307 ＋ 新規 9）、Swift Debug /
Release ビルド成功。実機 GUI／推論検証は現場で実施（この環境に MLX ランタイム・モデル本体なし）。

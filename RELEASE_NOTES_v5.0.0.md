# MLXBar v5.0.0 リリースノート

## APIフォールバック — キューが一杯のとき、外部のAIへ

v5.0.0 は、MLXBar のキューが一杯のとき、レート上限に達しているとき、またはローカルモデルが応答を開始できないときに、**登録した外部AIプロバイダーへリクエストを転送できる**ようにします。設定画面に新しい「APIフォールバック」タブが加わり、GUI と `mlxbarctl fallback` の両方から設定できます。

**既定は無効です。** 有効にしないかぎり、v4.0.2 と同じ経路・同じエラーを返します。

> **プライバシーについて**: 有効にすると、対象のリクエスト内容（プロンプトと画像）が、選んだ外部プロバイダーへ送信されます。画像は「画像に対応」を有効にしたフォールバックモデルにだけ送られます。有効化の際は確認ダイアログを表示します。

---

## 追加された機能

### 複数のプロバイダーに対応

| プロトコル | エンドポイント | 認証 |
|---|---|---|
| OpenAI Chat Completions | `{baseURL}/chat/completions` | `Authorization: Bearer` |
| OpenAI Responses | `{baseURL}/responses` | `Authorization: Bearer` |
| Anthropic Messages | `{baseURL}/messages` | `x-api-key` + `anthropic-version` |

プロバイダーは何件でも登録でき、それぞれに複数のフォールバックモデルを持たせられます。接続テスト（1トークン送信）で、URL・APIキー・モデル名をまとめて確認できます。

### フォールバックモデルごとの詳細設定

- **優先順位** — 小さいほど先に試します。同じならプロバイダーの登録順、次にモデルの登録順です
- **フォールバック元モデル** — MLXBar のどのモデルの要求をこの外部モデルへ流すか。`*`（すべてのモデル）または個別のモデルを複数選択できます
- **画像対応** — 画像付きの要求を受けられるか
- **レート上限** — 一定時間内に送るリクエスト数の上限（`0` は無制限）
- **最大出力トークン** — 要求の `max_tokens` の上限
- **最大コンテキスト** — 超える会話は古いメッセージから省いて送ります（システムメッセージは残します）
- **画像の詳細度** — `auto` / `low` / `high`
- **追加HTTPヘッダー** — 特殊なプロバイダー向け

### 決まりごと

1. **契機は3つ**: キュー満杯、レート上限、ローカルエラー（`ENGINE_BUSY`・`MODEL_NOT_LOADED`・メモリ不足など）。`MODEL_NOT_FOUND` と `INVALID_REQUEST` は転送しません（入力の誤りを隠さないため）
2. **明示指定は `*` より優先**: あるモデルが別のフォールバックモデルに明示指定されている場合、「すべてのモデル」しか持たないフォールバックモデルはその要求に使われません
3. **有効化時は未選択モデルを拾わない**: フォールバック元に選ばれていないモデルの要求は、フォールバックしません
4. **画像**: 画像付きの要求は「画像に対応」のモデルにだけ送られます。そのようなモデルがあればキューを待たずに転送し、無ければ従来どおり MLXBar のキューで待ちます
5. **失敗時**: 次の候補を試し、全滅した場合は**元のローカルエラー**をそのまま返します（外部の試行結果は `error.fallback` に付きます）

---

## 使いはじめ

```sh
# 1. プロバイダーを追加（返ってくる p-xxxxxxxx を控えます）
mlxbarctl fallback add-provider --name OpenAI \
  --protocol openai-chat --base-url https://api.openai.com/v1

# 2. APIキーを保存（config.json には保存されません）
mlxbarctl secrets set-fallback-key p-xxxxxxxx sk-...

# 3. フォールバックモデルを追加
#    --source-model を省略すると「すべてのモデル」。複数指定も可
mlxbarctl fallback add-model p-xxxxxxxx --model gpt-4.1-mini --priority 10 \
  --source-model qwen-9b --supports-images true

# 4. 有効化
mlxbarctl fallback set --enabled true

# 5. 状態を見る（直近のフォールバック、レート使用状況、クールダウン）
mlxbarctl fallback status
```

GUI では「設定…」→「APIフォールバック」から同じ設定ができます。フォールバック元モデルの選択は、MLXBar のモデル一覧からチェックで選べます。

---

## 互換性

- **既定で無効**: `apiFallback.enabled` の既定は `false`。無効時は v4.0.2 と同じ経路・同じエラーコード・同じステータスです
- **既存APIの変更なし**: `/v1/chat/completions`、`/v1/models`、`/anthropic/v1/messages` の形は変わりません。外部応答は既存のワーカーイベント形へ正規化して流すため、SSE の組み立てコードは変更していません
- **設定スキーマ**: `schemaVersion` は `1` のまま。`apiFallback` は追加キーで、既存の設定ファイルはそのまま読めます
- **APIキーの保存先**: `~/Library/Application Support/MLXBar/control/fallback-<providerId>.key`（0600）。`config.json` には `apiKeyConfigured` だけを記録します
- **公開APIからの不可視**: `/v1/*` と `/anthropic/*` からは、フォールバック設定もAPIキーも取得できません（管理APIのみ）

---

## 修正

- **フォールバック契機の取りこぼし**: モデル解決中に発生したローカルエラー（`MODEL_NOT_LOADED` など）でフォールバックした場合、後続のキュー容量チェックが契機を `None` に戻してしまい、外部へ送信したにもかかわらずローカルエラーを返していました。実機e2eで発見し、修正と回帰テスト3件を追加しました
- **Anthropic面のHTTP 500**: 全フォールバック先が失敗したとき、`FallbackError` が汎用ハンドラに落ちて500を返していました。元のローカルエラー（例: 429 `overloaded_error`）を返すよう修正しました
- **無言の空応答**: 外部プロバイダーがエラーをSSEイベントとして返した場合（Anthropic の `event: error`、Responses の `response.failed`、OpenAI互換の `{"error": ...}`）、空の成功応答として扱われていました。内容より前なら次の候補を試し、内容より後なら `error` イベントで通知します
- **表示名でのフォールバック元指定**: `sourceModels` に表示名を書いても一致しませんでした。カタログ行の `id` / `name` / `provider_key` すべてを一致対象にしました
- **空のストリーム**: 内容・終了理由・usage が1つも無いストリームは、空の成功応答ではなく `FALLBACK_PROTOCOL_ERROR` として次の候補へ回します
- **メモリの上限**: ツール呼び出し引数の累積バッファを 1 MiB で打ち切り、無制限レートのモデルは送信を数えず、1ウィンドウの記録も 4,096 件で打ち切ります
- **設定の部分破損**: 壊れたモデル/プロバイダー1件だけを落とし、残りの設定を保持します（以前は全プロバイダーが消えることがありました）
- **管理APIの全置換PUT**: 削除済みプロバイダーを含む古い設定を送ると 409 `SETTINGS_CONFLICT` で拒否します

### GUI で編集できる項目（v5.0.0 最終）

プロバイダー: 表示名・プロトコル・ベースURL・タイムアウト・APIキー・接続テスト。フォールバックモデル: モデル名・表示名・有効化・優先順位・画像対応・フォールバック元モデル（複数選択）・レート上限/集計秒数・最大出力トークン・最大コンテキスト・画像の詳細度・温度・top_p・追加HTTPヘッダー。

---

## 検証

| 種別 | 結果 |
|---|---|
| `pytest` | **1051 passed / 0 failed / 1 skipped**（v4.0.2 の 947 から +104） |
| `swift test` | **45 / 0**（+12） |
| `check_i18n` | 517キー欠落 0 |
| 実機e2e（`scripts/verify-fallback-runtime.py`） | 15項目すべて PASS（実コーディネーター＋偽上流サーバー、OpenAI/Anthropic両入口、画像振り分け、管理API往復、無効時の互換） |
| パッケージ版e2e | 7項目すべて PASS（DMG内の `MLXBarCoordinator` 実体を起動し、`/health` が 5.0.0、外部への転送・APIキー送信・状態記録を確認） |

---

## インストール

DMG を開き、`MLXBar.app` を「アプリケーション」へコピーしてください。

このビルドは **ad-hoc 署名**で、Apple の公証は受けていません。初回起動時に「システム設定」→「プライバシーとセキュリティ」からの許可が必要になることがあります。外付けボリューム上のモデルを使う場合は、更新のたびに「リムーバブルボリューム」の許可を求められることがあります（ad-hoc 署名のため）。

SHA-256: `c4123c7b876b90de310ff3254b912ed2ca7870f79829d976bf290dc8c8b30e72`

---

# MLXBar v5.0.0 Release Notes (English)

## API Fallback — reach an external AI when the queue is full

v5.0.0 lets MLXBar **forward a request to an external AI provider** when its own queue is full, a rate limit is reached, or the local model cannot start answering. A new "API Fallback" tab in Settings configures it, and `mlxbarctl fallback` mirrors the same settings.

**It is off by default.** Unless you enable it, requests take the exact v4.0.2 path and return the exact v4.0.2 errors.

> **Privacy**: when enabled, the content of matching requests (prompts and images) is sent to the provider you choose. Images go only to fallback models you marked as accepting images. Enabling the feature asks for confirmation first.

### What you can configure

- **Providers** (any number): OpenAI Chat Completions, OpenAI Responses, or Anthropic Messages, each with a base URL, timeout, API key and a one-token connection test
- **Per fallback model**: priority, which MLXBar models it answers for (`*` = all), image support, a request rate limit, a maximum output-token cap, a maximum context (older messages are dropped, system messages kept), image detail, and extra HTTP headers

### How it decides

1. **Triggers**: queue full, rate limit, or a local error that means "cannot answer right now" (`ENGINE_BUSY`, `MODEL_NOT_LOADED`, memory refusals…). `MODEL_NOT_FOUND` and `INVALID_REQUEST` never fall back, so a typo stays visible.
2. **Explicit beats "all models"**: a model that another fallback entry names explicitly is never answered by an entry that only lists `*`.
3. **Nothing is captured by accident**: a model that no fallback entry names does not fall back at all.
4. **Images**: an image request goes only to entries marked as accepting images; when one exists the request is forwarded without waiting in the queue, and when none exists it waits locally as before.
5. **Failures**: the next candidate is tried; if every candidate fails, the **original local error** is returned, with the external attempts attached as `error.fallback`.

### Try it

```sh
mlxbarctl fallback add-provider --name OpenAI \
  --protocol openai-chat --base-url https://api.openai.com/v1
mlxbarctl secrets set-fallback-key p-xxxxxxxx sk-...
mlxbarctl fallback add-model p-xxxxxxxx --model gpt-4.1-mini --priority 10 \
  --source-model qwen-9b --supports-images true
mlxbarctl fallback set --enabled true
mlxbarctl fallback status
```

### Compatibility

- Off by default; when off, the request path, error codes and statuses are unchanged from v4.0.2.
- No change to the shape of `/v1/chat/completions`, `/v1/models` or `/anthropic/v1/messages`: external answers are normalised into the existing worker event vocabulary, so the SSE encoders are untouched.
- Settings schema stays at `schemaVersion` 1; `apiFallback` is an additive key.
- API keys live in `control/fallback-<providerId>.key` (mode 0600), never in `config.json`, and are unreachable from the public API.

### Verification

`pytest` 1051 passed / 0 failed / 1 skipped · `swift test` 45 / 0 · `check_i18n` 525 keys, 0 missing · live end-to-end harness 15/15 PASS.

### Install

Open the DMG and copy `MLXBar.app` to Applications. This build is **ad-hoc signed and not notarised**; on first launch macOS may ask you to allow it in System Settings → Privacy & Security.

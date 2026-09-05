# MLXBar v2.0.0rc1 (pre-release / draft)

A pre-release draft: OpenAI/Anthropic-compatible API expansion. `/v1/completions`,
`response_format` (`json_object` / `json_schema`), `n > 1`, and Anthropic Extended
Thinking. No breaking changes to any existing endpoint's default behaviour, no
settings-schema changes.

プレリリース（下書き）: OpenAI/Anthropic互換APIの拡充です。`/v1/completions`、
`response_format`（`json_object` / `json_schema`）、`n > 1`、Anthropic Extended Thinkingに
対応しました。既存エンドポイントの既定挙動に破壊的変更はなく、設定スキーマの変更もありません。

## `/v1/completions` (legacy text completion)

The Coordinator/Worker pipeline already had a raw-string prompt path that skips the chat
template entirely (`WorkerSupervisor._validate_generation` accepts either a message list
or a plain string). This release is the first to expose it publicly: `prompt` (a single
string; batched array prompts are rejected), `max_tokens`, `temperature`, `top_p`, `n`,
`stream`, `stop`, `frequency_penalty`, `presence_penalty`, `seed`. `echo`, `suffix`,
`logprobs`, and `best_of > 1` are explicitly rejected rather than silently ignored.
Context compression does not apply here -- it operates on message lists, and a raw prompt
has no message boundaries to compress around.

Coordinator/Workerパイプラインには元々chat templateを完全にスキップする生プロンプト経路が
ありました（`WorkerSupervisor._validate_generation`はメッセージ配列と文字列の両方を受理）。
今回初めてこれを公開APIから使えるようにしました。`prompt`（単一文字列。配列バッチは拒否）、
`max_tokens`、`temperature`、`top_p`、`n`、`stream`、`stop`、`frequency_penalty`、
`presence_penalty`、`seed`に対応。`echo`・`suffix`・`logprobs`・`best_of>1`は黙って
無視せず明示的に拒否します。コンテキスト自動圧縮はメッセージ配列前提の機能のため、この経路
には適用されません。

## `response_format`: JSON mode and Structured Outputs

MLXBar has no grammar-constrained decoder, so both modes are implemented as a prompt
instruction plus post-generation validation, not guaranteed-valid decoding. The
instruction is appended to the leading system message (the same message context
compression always preserves verbatim, so the two features compose safely). If the
generated text fails validation -- not valid JSON for `json_object`, or does not match
the schema for `json_schema` -- MLXBar does **not** silently return the invalid output:
it fails with HTTP 502 `RESPONSE_FORMAT_INVALID` (`retryable: true`). `json_schema`
schemas using keywords outside a supported subset (`type`, `enum`, `properties`,
`required`, `additionalProperties`, `items`, basic length/range bounds -- no `oneOf` /
`anyOf` / `$ref`) are rejected up front with HTTP 400, before any generation happens.

MLXBarには文法制約デコーダがないため、両モードとも「プロンプトへの指示＋生成後の検証」で
実装しており、生成そのものが保証されるわけではありません。指示はリーディングsystemメッセージ
へ追記します（コンテキスト自動圧縮が常に逐語保持するメッセージと同じ場所なので、両機能は
安全に共存します）。生成結果が検証に失敗した場合（`json_object`で無効なJSON、または
`json_schema`でスキーマ不一致）、無効な出力を黙って返さず、HTTP 502
`RESPONSE_FORMAT_INVALID`（`retryable: true`）で失敗します。対応サブセット外のキーワード
（`oneOf` / `anyOf` / `$ref`等）を含む`json_schema`は、生成前にHTTP 400で拒否します。

## `n > 1`: multiple candidates

Non-streaming only, capped at 8. MLXBar runs `n` full sequential generations and returns
them as `choices[0..n-1]`; there is no true parallel decoding on a single local
GPU/model/lane, so latency scales with `n` (documented, not hidden). `stream: true`
combined with `n > 1` is rejected with HTTP 400 rather than faking OpenAI's interleaved
per-choice SSE deltas over a pipeline that can only finish one generation at a time.

非stream時のみ、最大8件。`n`回の完全な生成を逐次実行し`choices[0..n-1]`として返します。
ローカル1GPU・1モデル・1レーンでは真の並列生成ができないため、レイテンシは`n`に比例します
（隠さず明記）。`stream: true`と`n > 1`の組み合わせは、1件ずつしか完成できないパイプライン上で
OpenAIのインターリーブされたper-choice SSEデルタを模造することを避け、HTTP 400で拒否します。

## Anthropic Extended Thinking

`thinking: {type: "enabled", budget_tokens}` now produces real `thinking` content blocks
(streaming: `thinking_delta` then a closing `signature_delta`; non-streaming: a
`thinking` block with a `signature` field), built from the same `reasoning_delta` worker
events already exposed as `reasoning_content` on the OpenAI side. **The `signature` is a
local, unverified marker** (`mlxbar-local-unsigned:` + a SHA-256 hash), not Anthropic's
cryptographic signature -- MLXBar never verifies an incoming signature either, so
round-tripping through MLXBar works, but sending one of these signatures to the real
Anthropic API would be rejected there. This is an inherent limitation of reproducing
extended thinking outside Anthropic's own infrastructure, not a bug.

`thinking: {type: "enabled", budget_tokens}`が、実際の`thinking` content blockを生成する
ようになりました（stream: `thinking_delta`ののち`signature_delta`で閉じる。非stream:
`signature`フィールド付きの`thinking` block）。OpenAI側で`reasoning_content`として既に
公開している`reasoning_delta`イベントをそのまま再利用しています。**`signature`はローカルで
計算した未検証の印**（`mlxbar-local-unsigned:` + SHA-256ハッシュ）であり、Anthropic本家の
暗号署名ではありません。MLXBar自身は受け取った`signature`を検証しないためMLXBar内での往復は
問題ありませんが、本家Anthropic APIへ送ると拒否されます。ローカルモデルで拡張思考を再現する
以上避けられない制約であり、バグではありません。

## Deliberately out of scope (with rationale)

- **OpenAI Responses API** (`/v1/responses`): stateful `previous_response_id` chaining,
  background mode, and built-in server-side tools do not fit a single local model
  process with no server-side storage. A partial implementation would silently diverge
  from the spec at exactly the corners a client is least likely to test. `/v1/responses`
  now returns an explicit `UNSUPPORTED_ENDPOINT` error instead of a bare 404.
- **`logprobs`**: exposing full-vocabulary per-token log-probabilities over the
  Coordinator↔Worker IPC boundary would add meaningful memory/bandwidth cost to the
  existing hot path; deferred pending a design that bounds that cost (e.g. top-k only).
  Behaviour unchanged (still rejected with HTTP 400).
- **Chat Completions non-text output**: already correctly rejected by the existing
  `modalities` check; confirmed compliant, no change needed.

見送った項目（理由付き）:

- **OpenAI Responses API**（`/v1/responses`）: `previous_response_id`によるstateful
  chaining、バックグラウンドモード、組み込みサーバーサイドツールは、サーバー側永続状態を
  持たない単一ローカルモデルプロセスに合いません。部分実装はクライアントが検証しなさそうな
  角で静かに仕様と乖離するため見送りました。`/v1/responses`は素の404ではなく明示的な
  `UNSUPPORTED_ENDPOINT`を返します。
- **`logprobs`**: 語彙全体のトークンごとlogprobsをCoordinator↔Worker間のIPCへ載せると、
  既存のホットパスに無視できないメモリ・帯域コストが乗ります。コストを抑える設計
  （top-kのみ等）ができるまで見送りました。挙動は変更ありません（引き続きHTTP 400）。
- **Chat Completionsのテキスト以外の出力**: 既存の`modalities`チェックが元々正しく拒否して
  おり、追加実装は不要と確認しました。

## Compatibility / 互換性

- No settings-schema changes; every new feature is request-scoped and opt-in. An
  existing `config.json` needs no migration.
- With none of the new parameters set, every response is byte-identical to v1.9.2.
- Coordinator/Worker RPC surface: unchanged.
- 設定スキーマの変更なし。全機能はリクエスト単位のオプトインで、既存の`config.json`は
  マイグレーション不要です。
- 新パラメータを一切指定しない場合、応答はv1.9.2とバイト同一です。
- Coordinator/Worker間のRPCは無変更です。

## Verification / 検証

- Python regression suite: **403 passed** (388 from v1.9.2 + 15 new; 3 pre-existing tests
  updated to match the intentional behaviour changes -- see `TEST_PLAN_v2.0.0rc1.md`).
  Run 3× consecutively, stable.
- Design and invariants: `DESIGN_v2.0.0rc1.md`.

## Checksum

`4ac1329b11eaf9a68a118998e9064f69e115f2c9ec7479170fbb095d21ed08e9`  `MLXBar-2.0.0rc1.dmg`

# MLXBar v2.0.0 設計

対象: OpenAI/Anthropic互換APIの拡充。ユーザーから提示された検討項目は次の8点。

1. OpenAI Responses API
2. `/v1/completions`（レガシー補完）
3. Structured Outputs（`response_format: json_schema`）
4. JSON mode（`response_format: json_object`）
5. logprobs
6. `n > 1`（複数候補生成）
7. Chat Completionsでのテキスト以外の出力
8. Anthropic Extended Thinking

v2.0.0では上記に加え、リリース前の静的デバッグ（外部クライアント互換性）とGUI操作の
CLI完全対応の精査を行い、見つかった1件のパリティ欠落を修正した（§6・§7）。
本設計書はプレリリース `v2.0.0rc1`（GitHub上はdraft、一般公開せず）の設計を引き継ぐ。

## 1. 設計方針

MLXBarには文法制約デコーダ（grammar-constrained decoding）がなく、Coordinator/Worker間のプロトコルは
「メッセージ配列 or 生プロンプト文字列 → テキスト生成イベント列」という単純な形を前提にしている。
8項目それぞれについて、**この既存契約の中で安全に実装できるか**を先に評価し、無理に押し込むと
crash safety・memory safetyやAPI互換性の既存不変条件を壊すものは、実装するふりをせず明示的に
`UNSUPPORTED_PARAMETER` / `UNSUPPORTED_ENDPOINT`で拒否する（`_reject_unimplemented`など既存の
「黙って無視しない」方針を継続）。

## 2. 項目ごとの評価と結論

### 2.1 `/v1/completions`（実装する）

調査の結果、Coordinator↔Worker間には**既に**生プロンプト文字列の経路がある。
`WorkerSupervisor._validate_generation`は`prompt`が`str`か`list`かを受理し、`str`の場合は
`worker_params["messages"]`を設定しない（`Coordinator/mlxbar/workers/supervisor.py:565`）。
Worker側の`_render_prompt`/`stream()`も`isinstance(prompt, list)`でだけ`apply_chat_template`を
呼び、そうでなければ文字列をそのままトークナイズする（`Workers/mlx_lm_worker/adapter.py:107-120`）。
つまりレガシー補完に必要な「chat templateを通さない生成」は元から存在する契約で、これまで
公開APIから使われていなかっただけ。新しいバックエンド経路を作る必要がなく、リスクは小さい。

実装: `prompt`は単一文字列のみ対応（配列バッチは422で拒否）。`echo` / `suffix` / `logprobs` /
`best_of>1`は明示的に拒否。`n`（後述）とstreamに対応。コンテキスト自動圧縮
（`context_compression.py`）はメッセージ配列前提の機能のため、この経路には適用しない
（生プロンプトにメッセージ境界がなく、圧縮対象を安全に選べないため）。

### 2.2 JSON mode / Structured Outputs（実装する。ただしベストエフォート）

OpenAI本家のjson_object modeは「プロンプトへの指示」に近い弱い保証で、json_schema modeは
文法制約デコードによる強い保証。MLXBarには後者の実装手段（outlines等）がなく、新規サードパーティ
依存を追加するとPyInstallerでのバンドル・ライセンス・supply chainのリスクが増える。そこで両方とも
「プロンプト指示＋生成後バリデーション」で実装し、**保証の強さの違いを実装ではなく失敗時の挙動で
表現する**：

- 指示をリーディングsystemメッセージへ追記する（`response_format.py: inject_into_messages`）。
  `context_compression`が常に保持するのと同じ「先頭system」なので、圧縮が先に効いても後に効いても
  指示は必ず生き残る。
- 生成完了後、`json.loads`と（`json_schema`の場合は）スキーマ検証を行う。**失敗したら無効なJSONを
  黙って返さず、HTTP 502 `RESPONSE_FORMAT_INVALID`（`retryable: true`）で拒否する**。
  「保証を謳って実は守れていない」状態は、拒否そのものより有害と判断した。

スキーマ検証は依存追加を避けるため`response_format.py`に自前の軽量サブセット実装
（`type` / `enum` / `properties` / `required` / `additionalProperties` / `items` / 文字列長・数値範囲）
を置いた。`oneOf` / `$ref`など未対応キーワードを含むスキーマは、生成前にHTTP 400で拒否する
（`check_schema_supported`）。黙って無視するとクライアントは「制約が効いている」と誤認する。

ストリーミングは全文が揃うまで検証できない。既に送出済みのチャンクを撤回できないため、末尾に
エラーイベントを追加する形にした（後方互換: `stream_options.include_usage`の前、`[DONE]`の前）。
`n > 1`とstreamの組み合わせを禁止したのと同じ理由で、`response_format`検証の不完全さも
ドキュメントで明示する。

### 2.3 `n > 1`（実装する。非stream限定）

`n`件の完全生成をCoordinator層で逐次ループする（`_run_one_completion` / `_run_one_text_completion`
をn回呼ぶ）。新しいバックエンド機能は不要。ローカル1GPU・1モデル・1レーンの制約上、真の並列生成は
できないため、レイテンシはn倍になる（ドキュメントに明記）。

`stream=true`との組み合わせは実装しない：OpenAIのstreaming n>1は`choices[].index`ごとに
インターリーブされたSSEデルタを返す必要があり、逐次生成しかできないMLXBarでそれを模倣すると
「ストリームの体裁だけ整えて実際は1件ずつ完成してから送る」擬似実装になり、クライアントの
体感レイテンシに関する前提を裏切る。素直に組み合わせをHTTP 400で拒否する。上限は`n<=8`
（`MAX_N`）とし、暴走防止とした。

### 2.4 Anthropic Extended Thinking（実装する）

MLXBarは既にreasoningモデルの`<think>`ブロックを`reasoning_delta`イベントとして分離しており、
OpenAI経路では`reasoning_content`として返している（v1.8.4で確立した不変条件）。Anthropic経路の
`AnthropicMessageBuilder`は今までこのイベントを黙って捨てていた（v1では「対応しない」と明示していた
箇所）。同じイベントを`thinking` content block（`thinking_delta` / `signature_delta`）として
組み立て直すだけで実装でき、生成パイプライン自体の変更は不要。

`thinking`パラメータの正規化はOpenAI経路の`_normalize_thinking`（`type` / `budget_tokens` /
`effort`というAnthropic形の入力を元々受け付けていた）をそのまま再利用し、`chat_template_kwargs`
（`enable_thinking` / `thinking_budget`）へ変換する。

**制約**: 本家のthinking blockには、Anthropicのサーバーが発行し後で検証する暗号署名
`signature`が付く。MLXBarにはその発行権限がなく、`signature`はローカルで計算した
`sha256(thinking_text)`を`mlxbar-local-unsigned:`接頭辞で明示したものに過ぎない（
`anthropic_stream.py: _local_thinking_signature`）。MLXBar自身は受け取った`signature`を
検証しないため、MLXBar内で往復させる分には問題ないが、これを本家Anthropic APIへそのまま
送っても本家側は拒否する。ローカルモデルで拡張思考を再現する以上避けられない制約であり、
バグではない（README・RELEASE_NOTESに明記）。

### 2.5 OpenAI Responses API（見送り。理由を明示して`/v1/responses`をスタブ化）

Responses APIはChat Completionsと別の契約：サーバー側に会話状態を持つ`previous_response_id`、
バックグラウンドモード、暗号化されたreasoning item、組み込みサーバーサイドツール（web検索・
コードインタプリタ・computer use）などを含む。MLXBarは単一ローカルモデルプロセスでサーバー側
永続状態を持たず、これらの多くはそもそも意味を持たない。

一部（テキスト入出力・関数呼び出しのみ）を実装する案も検討したが、「仕様の一部だけを実装した
Responses API」は、クライアントが最も検証しなさそうな角（`previous_response_id`によるstateful
chainingなど）で静かに本家と乖離する。これは「対応していないと明言する」よりも有害と判断し、
**v2.0.0でも実装を見送る**。ただし`404`への素通しに任せず、`/v1/completions`が過去そうで
あったように、明示的な`UNSUPPORTED_ENDPOINT`メッセージを返すスタブを追加した
（`openai_compat.py: responses_api`）。将来実装する場合は別途設計・別バージョンとする。

### 2.6 logprobs（見送り。既存の拒否を維持）

`mlx_lm.stream_generate`が返す`GenerationResponse`には各生成ステップの語彙全体（数万〜十数万語彙）
に対するlogprobsが含まれ得るが、これをCoordinator↔Worker間のUnix domain socket JSON-linesの
IPCへ毎トークン載せることは、既存のホットパス（プロンプトキャッシュ、ハートビート、
キャンセル処理）に新たな大きなメモリ・帯域コストを持ち込む。安全性（crash safety / memory
safety）を優先する既存方針に照らし、**v2.0.0でも実装を見送る**。挙動は変更なし
（`logprobs`は引き続きHTTP 400 `UNSUPPORTED_PARAMETER`）。トップkのみに絞る、専用の低頻度
イベントにする等の設計は将来の検討課題として`common-rules-document`に記録する。

### 2.7 Chat Completionsでのテキスト以外の出力（既存のまま。変更なし）

`modalities`が`["text"]`以外（音声出力等）を要求された場合、`chat()`は既に
`modalities not in (None, ["text"])`でHTTP 400を返している（`openai_compat.py`、v1から存在）。
MLXBarはテキスト（＋画像入力）のローカルLLMサーバーであり、音声合成等の出力モダリティを
持たない。今回のレビューで再確認した結果、**追加の実装は不要、既存動作が正しい**と結論した。

## 3. 影響範囲

| ファイル | 変更 |
|---|---|
| `Coordinator/mlxbar/api/response_format.py`（新規） | JSON mode / json_schemaの指示生成・検証 |
| `Coordinator/mlxbar/api/openai_compat.py` | `/v1/completions`実装、`/v1/responses`スタブ、`response_format`受理、`n>1`ループ |
| `Coordinator/mlxbar/api/anthropic_compat.py` | `thinking`パラメータ受理、budget検証 |
| `Coordinator/mlxbar/api/anthropic_stream.py` | `thinking` content block（stream/非stream） |
| `Coordinator/mlxbar/cli.py`（v2.0.0） | `config set-context-compression` サブコマンド追加（§7） |
| `README.md`（v2.0.0） | Codex接続手順の追記（§6）、新CLIコマンドの記載 |

設定スキーマ（`SettingsStore.DEFAULTS`）への変更は**なし**。API拡充の全項目がリクエスト単位の
オプトインで、永続設定を必要としないため、マイグレーションやGUI変更が一切不要だった。§7のCLI追加も
`cli.py`のみの変更で、管理API・設定スキーマ・GUIには触れていない。

## 4. 互換性

- 既存の`response_format: text`、`logprobs`拒否、`n=1`、`/v1/messages`のthinking無指定時の挙動は
  全て不変（回帰テストで確認）。
- `thinking`を送らないAnthropicクライアントには、v1.9.2までと完全に同一の応答が返る
  （`emit_thinking=False`が既定）。
- `/v1/completions`は404から実装済みへ変わるが、これは「未対応→対応」であって既存の成功応答を
  変えるものではない。
- Coordinator/Worker間のRPC・wire formatは無変更（既存の生プロンプト経路を公開しただけ）。

## 5. 検証

`TEST_PLAN_v2.0.0.md`を参照。

## 6. リリース前の静的デバッグ（外部クライアント互換性）

Claude Code / Codex / ZCode / OpenClaw など、Anthropic互換・OpenAI互換の各CLIクライアントで
MLXBarが正常動作するかを、コードを読んで静的に検証した（`openai_compat.py` /
`anthropic_compat.py` / `anthropic_stream.py` / `main.py`）。

**結論: 正確性・クラッシュ安全性・メモリ安全性のバグは無し。** 確認した不変条件:

- 認証とリクエストサイズ上限は本文パース前のASGI層（`PublicRequestGuard`）で強制される。
  未認証リクエストはソケットがバッファした分しかメモリを使わない。
- Anthropic Extended ThinkingのSSE列順は正しい:
  `message_start` → `content_block_start{thinking}` → `thinking_delta*` → `signature_delta`
  → `content_block_stop` → text block → `message_delta` → `message_stop`。
  thinkingブロックは常にindex 0（Workerは`reasoning_delta`を`delta`より先に出す）。
  非streamでも`content`の先頭に`thinking`が来る。
- `n>1` + `stream` は400で拒否。`n`ループのトークン集計は正しい。
- `response_format`はベストエフォートで、検証失敗時は無効なJSONを黙って返さず
  HTTP 502 `RESPONSE_FORMAT_INVALID`。
- コンテキスト自動圧縮と`response_format`指示注入はどちらも先頭systemメッセージを対象にし、
  安全に共存する。
- `null` tool-call index耐性、`role`は先頭1回のみ、推論内容と本文の分離 — すべて維持。

**Codex向けの注意（バグではなくドキュメントの欠落）:** OpenAI Codex CLIは既定で
Responses API（`wire_api = "responses"`）を使う。MLXBarにResponses APIは無く、
明示的な `404 UNSUPPORTED_ENDPOINT` を返す（フリーズしない＝安全側に倒れる）。Codexから
使うには `~/.codex/config.toml` の `[model_providers.<id>]` で `wire_api = "chat"` を
指定する必要がある。READMEはClaude Code / ZCode / Zed・Cline・OpenCode / OpenClawを
説明していたがCodexに触れていなかったため、v2.0.0でCodexの接続手順を追記した。

## 7. GUI操作のCLI完全対応（v1.8.1契約の精査と1件の修正）

v1.8.1で「GUIの全ミューテーションに*名前付き*CLIコマンドを用意する（汎用 `config set` は
エスケープハッチ）」という契約を定めた。`MenuBarViewModel` と `MLXBarSettingsView` の全
ミューテーションを `mlxbarctl`（`cli.py`）と突き合わせた結果、**1件だけ欠落**していた:

- **`contextCompression`（v1.9.2追加）に名前付きCLIコマンドが無かった。**
  GUIの「設定 > モデル」に4つのコントロール（有効化トグル・発火の目安%・要約後も残す
  直近ターン数・要約の最大トークン数、`setContextCompressionSettings`）があるのに、
  CLIからは汎用 `config set contextCompression.enabled true` でしか到達できなかった。

**修正:** `mlxbarctl config set-context-compression`（`--enabled` / `--trigger-percent 50-95` /
`--keep-tail 2-50` / `--summary-max-tokens 100-4000`）を追加。`set-model-pool` と同じ
「指定したオプションだけ変更」方式で、`--trigger-percent` は GUI と同じく `triggerRatio`
（0〜1）へ変換する。既存の `PUT /api/v1/settings` を1回呼ぶだけで、管理API・設定スキーマ・
GUI・ローカライズ文字列には一切触れない（`cli.py` への追加のみ）。汎用 `config set` は
引き続きエスケープハッチとして残す。

名前付きコマンドが不要と確認した項目（既存の記録どおり、Swift専用のOS API）:
`SMAppService` のログイン項目登録（CLIは設定値のみ、GUIが次回起動時にreconcile）、
`SMAppService.unregister()`（remove-all-data時）、クリップボードの「コピー」ボタン
（クライアント側処理。`secrets get-api-token` / `logs show` で同じ値は取得できる）。

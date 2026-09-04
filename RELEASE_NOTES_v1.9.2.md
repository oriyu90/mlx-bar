# MLXBar v1.9.2

A feature release: an opt-in context compression for long agent conversations, and a redesigned
menu bar model list. No breaking changes to the OpenAI/Anthropic-compatible API, no route
changes, no settings-schema removals.

機能追加リリース: 長いエージェント会話向けのオプトイン式コンテキスト自動圧縮と、メニューバーの
モデル一覧UIの刷新です。OpenAI/Anthropic互換APIに破壊的変更はありません。

## Context compression (opt-in, off by default) / コンテキスト自動圧縮（既定オフのオプトイン機能）

ZCode / Claude Code / OpenCode-style clients resend the entire conversation on every request.
MLXBar's prompt cache already avoids re-prefilling that history, but attention cost, the model's
own context window, and KV-cache memory all still scale with total token count — a long-running
coding-agent session eventually gets slow, and past `effectiveMaxPromptCharacters` outright fails
with `INPUT_TOO_LARGE`.

v1.9.2 adds an optional, transparent compression step: when a request's prompt crosses a
configurable fraction of the model's context window, MLXBar summarizes the *middle* of the
conversation (never the leading system/tool-schema message, never the most recent messages, never
a `tool_calls`/`tool` pairing split across the boundary, never an image-bearing turn) into one
synthetic system message, using the already-loaded model. Any failure falls back to the original,
uncompressed prompt.

**Off by default.** The summary is not a byte-for-byte stand-in for what the client sent, so this
is an explicit opt-in (Settings → Models → "コンテキスト自動圧縮" / "Automatic Context
Compression") rather than a default behaviour change for existing ZCode/OpenCode/Claude Code
setups. Enabling it never changes tool definitions (`tools`/`tool_choice` are untouched) and never
produces an invalid OpenAI/Anthropic message sequence.

ZCode・Claude Code・OpenCodeのようなクライアントは、毎リクエスト会話全体を送り続けます。
MLXBarのプロンプトキャッシュはプリフィルの重複計算を防ぎますが、アテンション計算量・モデルの
コンテキスト窓・KVキャッシュのメモリはいずれも総トークン数に比例するため、会話が長く続くと
最終的には遅くなり、`effectiveMaxPromptCharacters`を超えると`INPUT_TOO_LARGE`で失敗します。

v1.9.2では、モデルのコンテキスト窓に対する割合（既定70%）を超えたリクエストで、会話の
「中間部分」（先頭のsystem/tool定義、直近の会話、tool_calls/toolのペア、画像を含むターンは
常に除外）を、既にロード済みの同じモデルで1件の要約メッセージへ置き換える機能を追加しました。
要約生成が失敗した場合は元のプロンプトのまま送られます。**既定は無効**です。要約は元の発言
そのものではないため、有効化は明示的な設定操作としました。ツール定義は圧縮の対象外で、有効化
してもOpenAI/Anthropic互換のメッセージ形式が壊れることはありません。

## Unified model list in the menu bar / メニューバーのモデル一覧を刷新

The menu bar used to show one "primary" model prominently, with the same model repeated in a
"常駐モデル" list below it. v1.9.2 replaces this with a single list where every resident model
(including whichever one Quick Chat targets, now marked with a small "既定"/"Default" badge) is an
equal row: a copy button on the left of every row (not just the primary model's), the existing
engine/size/replica detail line, and a new status line that always shows
loading/generating/queued/idle and adds live tokens/sec only while that model is actually
generating.

これまでメニューバーは「主モデル」1件を大きく表示し、同じモデルを含む常駐モデル一覧をその下に
重複表示していました。v1.9.2では全常駐モデル（Quick Chatの対象モデルには「既定」バッジを表示）
を対等な1つのリストとして表示するよう刷新しました。各行の左側にコピーボタンを追加（主モデルに
限らずどのモデル名もコピー可能）、既存のエンジン・サイズ・並列数などの内訳行は維持、新たに
ロード中／応答生成中／順番待ち／待機中を常に示すステータス行を追加し、生成中はtok/sを併記します。

バックエンド（Coordinator/Workers）はこの変更について無変更です。

## Compatibility / 互換性

- OpenAI/Anthropic-compatible routes, request/response shapes, and tool-calling semantics: no
  changes.
- Settings schema: additive only (`contextCompression` section, merges to a disabled default when
  absent from an existing `config.json`).
- Coordinator/Worker RPC surface: no changes.
- Model load/unload, prompt cache, model pool behaviour: no changes.
- With `contextCompression.enabled` left at its default (`false`), v1.9.2 behaves identically to
  v1.9.1 for every existing ZCode/OpenCode/Claude Code setup.

## Verification / 検証

- Python regression suite: **388 passed** (380 from v1.9.1 + 8 new): compression disabled by
  default, tool_calls/tool pairing preserved under compression, fallback on summarization failure,
  image turns excluded from the compressible middle, short-conversation no-op, and settings
  validation (defaults, out-of-range rejection, valid patch). Run 3× consecutively, stable.
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed).
- On-hardware confirmation (see `TEST_PLAN_v1.9.2.md` for the full table): with compression off
  (default), behaviour is byte-identical to v1.9.1. With it enabled, a synthetic 703,300-character,
  26-message conversation compressed to 88,963 characters (21 messages replaced by one summary),
  confirmed via `/api/v1/status`'s `contextCompression` field. The documented fallback (summary
  itself too large to fit the model's context) was also reproduced and correctly fell back to the
  uncompressed prompt.
- Design and invariants: `DESIGN_v1.9.2.md`.

## Checksum

`b2fcd1482e0b73e227aac577b95d9b32fb2b10ec7ff5cb24df70ff7f6076bcf5`  `MLXBar-1.9.2.dmg`

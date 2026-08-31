# MLXBar v1.8.4 設計書（tool無しリクエストで推論ブロックが本文へ漏れる不具合の修正）

更新日: 2026-08-31
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

**目的:** 推論（thinking）対応モデルに **`tools` を含まない** リクエストを送ると、`<think>…</think>`
の中身（およびテンプレートがプロンプト側で先頭の `<think>` を開くモデルでは閉じ側の `</think>` 単独タグ）が
そのまま `content` へ混ざる不具合を修正する。Ornith 1.5系（`enable_thinking=false` / `/no_think` /
`reasoning_effort=none` をいずれも無視して常に推論ブロックを出す）で顕在化し、OpenClaw の
補助的な LLM 呼び出し（memory dreaming、タイトル生成、compaction 要約、パイプラインの整形ステップなど、
`tools` を付けない呼び出し）で生の思考文が配信チャネルへ流出する実害があった。

**非目的:**
- Coordinator（OpenAI互換 / Anthropic互換、stream / non-stream）のハンドラは無変更。
- 設定 schema（version 1）は不変。管理 API 無変更。
- non-stream 応答へ `reasoning_content` を新設しない（現行仕様どおり non-stream では推論を破棄。
  stream では既存どおり `reasoning_content` デルタで分離）。「漏らさない」ことが目的で、
  推論の可視化は別機能。
- mlx-vlm 内部の `mlx_vlm.prompt_utils` 側 thinking 整形は触らない。

## 2. 不具合の実体

同一の重み `Ornith-1.5-35B-A3B-MLX-4bit`（mlx-lm エンジン）に対し:

| 経路 | `content` | 判定 |
|---|---|---|
| mlx-bar / `tools` **なし** | `'The user is asking me to…\n</think>\n\nPONG'` | ❌ 漏れる |
| mlx-bar / `tools` **あり**（stream） | `'\n\nPONG'` ＋ `reasoning_content` にCoT | ✅ 分離 |
| mlx-bar / `tools` **あり**（non-stream） | `'\n\nPONG'`（推論は破棄） | ✅ 混入なし |
| LM Studio 直（:1234） | `'\n\nPONG'` ＋ `reasoning_content` にCoT | ✅ 分離 |

### 根本原因

`Workers/common/server.py` の `/generate` イベントループ:

```python
tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
tool_stream = IncrementalToolStream() if tool_mode else None   # ← tools無しだと None
...
if tool_mode and event.get("type") == "reasoning_start":
    tool_stream.start_reasoning()
elif tool_mode and event.get("type") == "delta":
    visible_events = tool_stream.feed(...)      # ← <think> 分離はここだけ
    ...
elif event.get("type") == "delta":
    # tools無し経路：生パススルー。<think> 分離が一切走らない
    yield json.dumps(event, ...)
```

`<think>…</think>` を `reasoning_delta` / `delta` に切り分ける `IncrementalToolStream` が
`tool_mode` のときしか構築・使用されない。`tools` を含まないリクエストでは、推論文も閉じタグも
そのまま `delta`（= `content`）として流れる。

v1.2.0 の「tool呼び出しが無い応答で think が混ざる」修正（`parse_tool_markup` 内の
`re.sub(r"<think>.*?</think>|…")`）は `adapter.finalize` 経由、すなわち `tool_mode` 内でしか
呼ばれず、かつ `<think>` と `</think>` が **両方揃っている** ときにしか中身を消せない。
テンプレートが先頭 `<think>` をプロンプト側で開くモデル（Ornith 1.5）では出力に閉じ `</think>`
しか現れないため、この正規表現ではタグだけ消えて推論文が残る。

## 3. 修正内容

### 3.1 `Workers/common/tool_calls.py` — `IncrementalToolStream` に `reasoning_only` を追加

```python
class IncrementalToolStream:
    def __init__(self, reasoning_only: bool = False):
        self.pending = ""
        self.in_reasoning = False
        self.tool_detected = False
        # reasoning_only: <think>/</think>/<assistant> の切り分けだけ行い、
        # <tool_call> 系マーカーは一切特別扱いしない（tools無しリクエスト用）。
        self._tool_markers = () if reasoning_only else self.TOOL_START
```

`feed()` 内の `markers` 構築を `self.TOOL_START` から `self._tool_markers` に変える
（1 箇所、`in_reasoning` 分岐の両側）。`reasoning_only=True` では:

- `<tool_call>` 等が来ても `tool_detected` にならず、ふつうの本文としてそのまま流れる
  （＝ tools 無しリクエストの現行挙動を維持。モデルが自発的に tool マークアップを吐いても隠さない）。
- 保留（holdback）対象マーカーの最長は `</assistant>`（12文字）。`<` で終わるデルタの末尾
  最大 11 文字だけを次デルタまで保留する。本文が失われることはなく、デルタ境界がまたぐだけ。
- `THINK_START` / `THINK_END` / `ASSISTANT_TAGS` の切り分けは通常どおり。

デフォルト引数のため既存の `IncrementalToolStream()` 呼び出し（テスト含む）は完全に不変。

### 3.2 `Workers/common/server.py` — think 分離を `tool_mode` 非依存に

- `tool_stream = IncrementalToolStream() if tool_mode else None`
  → `tool_stream = IncrementalToolStream(reasoning_only=not tool_mode)`（常に構築）
- `if tool_mode and event.get("type") == "reasoning_start":` から `tool_mode and` を外す。
- 2 本あった `delta` 分岐を 1 本に統合。`tool_stream.feed()` を常に通す。
  - `buffered += text`（tool マークアップ解析用のバッファ）と `adapter.finalize` / `tool_calls`
    / `TOOL_PARSE_FAILED` は **`tool_mode` のときだけ**（従来どおり）。
  - `stop_filter` は `delta` 型 visible にのみ適用（`reasoning_delta` には掛けない）。従来の
    tool_mode 経路と完全に同じ。→ stop シーケンスは推論文中では発火しない（従来仕様）。
  - `tool_parse` ハートビート（`not visible_events` のとき）は `tool_mode` のときだけ維持。
    tools 無し経路の保留は最大 11 文字・1 イテレーションなので追加ハートビート不要。
- 末尾 `tool_stream.finish()` のフラッシュを `if tool_mode:` の外へ出し、常に実行。
  `finalize` / `tool_calls` 確定 / `TOOL_PARSE_FAILED` は `if tool_mode:` 内に残す。
  - `reasoning_only` かつ最後まで `</think>` が来ない（`length` 打ち切り）場合、`finish()` は
    残りを `reasoning_delta` として返す → 打ち切られた推論のみの応答は `content` 空 ＋
    `reasoning_content`（stream）になり、生思考が本文へ漏れない。

### 3.3 `Workers/mlx_lm_worker/adapter.py` / `Workers/mlx_vlm_worker/adapter.py`

```python
tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
if tool_mode and isinstance(prompt, str) and prompt.rstrip().endswith("<think>"):
    yield {"type": "reasoning_start"}
```
→ ガードから `tool_mode and` を外す（両ワーカーで対称に）:
```python
if isinstance(prompt, str) and prompt.rstrip().endswith("<think>"):
    yield {"type": "reasoning_start"}
```
`prompt.rstrip()` は末尾の改行・空白をすべて除去するので、テンプレートが `<think>\n` で
終わる一般的なケースもそのまま拾える（追加の緩和は不要）。`tool_mode` 変数自体は
`tool_support` メトリクスで使うため残す。

### 3.4 Coordinator — 変更なし

- OpenAI stream: `reasoning_delta` → `delta.reasoning_content` は実装済み
  （`openai_compat.py`、`test_openai_tools::test_reasoning_delta_uses_openai_compatible_reasoning_content_field`）。
- OpenAI non-stream: `reasoning_delta` は集計対象外 → `content` がクリーンになる（これが修正の効果）。
- Anthropic stream / non-stream: `reasoning_delta` → `[]`（`anthropic_stream.py`、v1 の既知の制限）。
  イベントが増えても無害。

## 4. 不変条件（守ること）

1. **`tool_mode` 経路は論理的に不変。** `IncrementalToolStream(reasoning_only=False)` は
   従来の `IncrementalToolStream()` と同一。tool 関連テストは全て無改変で緑。
2. **非推論モデル・tools 無し**：`<think>` 等を一切出さないモデルは、末尾 `<` 由来の最大 11 文字・
   1 デルタの保留を除き、従来とバイト等価でストリームされる（本文の欠落なし）。
3. **tools 無し応答中に自発的な `<tool_call>`**：`reasoning_only` は tool マーカーを検出しないので
   従来どおり本文として可視。非 tool リクエストに `TOOL_PARSE_FAILED` 経路は増えない。
4. **Coordinator / API ハンドラ / 設定 schema：1 行も変更しない。**
5. `count` / `generation_tps`、`buffered` / `finalize`、stop シーケンス意味論、ハートビートは不変。
6. **mlx-vlm**：変更は既存イベント `reasoning_start` の発火条件を広げるだけ。対称に mlx-lm と同じ。

## 5. 意図した挙動変更（ドキュメント化する）

推論モデルへ **`tools` 無し** で問い合わせると、`<think>…</think>` は
`reasoning_content`（stream）へ分離／破棄（non-stream）され、`content` へは出なくなる。
`tools` 無しリクエストで `content` から思考文を読んでいたクライアントはそこで読めなくなる。
これは修正の本旨。

## 6. 検証

`TEST_PLAN_v1.8.4.md` を参照。既存 367 件は無改変で緑を維持し、新規回帰を追加する。
実機は稼働中の mlx-bar（`Ornith-1.5-35B-A3B-MLX-4bit` 常駐）へ tools 無し合成リクエストを送り、
`content` に思考文・`</think>` が出ないこと、tools あり経路が従来どおりであることを確認する。

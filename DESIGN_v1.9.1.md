# MLXBar v1.9.1 設計書（コード監査4件の修正：OpenAI互換ストリームの`role`重複／非ストリームの推論内容欠落／`index: null`耐性／可読性）

更新日: 2026-09-02
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

**目的:** 2026-09-02 のコード監査（`Workers/common/server.py`、`Workers/common/tool_calls.py`、
`Coordinator/mlxbar/api/openai_compat.py` の部分読み）で報告された4件を、互換性・既存機能・
クラッシュ安全性・メモリ安全性を保ったまま修正する。いずれもバグ修正のみのパッチリリース。

| # | 種別 | 対象 |
|---|---|---|
| 1 | 正しさ（互換性） | ストリーミングでtool callが2つ以上あると`delta.role`がN回送られる |
| 2 | 正しさ（データ欠落） | `tools`無し非ストリームで推論モデルの`<think>`内容がどこにも返らず捨てられる |
| 3 | クラッシュ安全性 | `int(delta.get("index", …))`が`"index": null`で`TypeError` |
| 4 | 可読性・一貫性 | `server.py`のロード失敗分類の非括弧`or`/`and`式 |

**非目的:**
- Coordinator のルーティング・設定 schema（version 1）・管理 API・Swift GUI は無変更。
- 新しいエンドポイント・新しいリクエストパラメータは追加しない。
- Anthropic 互換経路の推論ブロック方針（v1では署名なし thinking を出さない）は変更しない。
- ワーカー（`server.py` / `tool_calls.py`）の生成ロジックは #4 の括弧追加を除き無変更。
- 監査所見5（`test_buffered_tool_generation_still_emits_heartbeats` のフレーク）は
  据え置き（観測性テスト、互換性・機能とは無関係。`mlx-bar.md`「未解決の課題」に記録済み）。

## 2. 各不具合の実体と修正

### 2.1 #1 — マルチtool callで`delta.role`が繰り返される

**場所:** `Coordinator/mlxbar/api/openai_compat.py` `_tool_call_stream_chunks`

```python
for index, call in enumerate(normalized):
    first = {"index": index, "id": call["id"], "type": "function",
             "function": {"name": call["function"]["name"], "arguments": ""}}
    yield {... "choices": [{"index": 0,
           "delta": {"role": "assistant", "tool_calls": [first]},   # ← 各callで role
           "finish_reason": None}]}
```

`tool_calls` イベントに複数の call が入っていると（`<tool_call>` ブロックを2つ以上出すモデル）、
各 call の先頭チャンクで `delta.role="assistant"` が送られる。OpenAI は `delta.role` を
choice ごとに1回だけ送る。同ファイル L266-270 のコメントと `tool_call_role_sent` ガードが
まさにこの繰り返しを「厳格なパーサ（Vercel AI SDK / OpenCode）を壊す」として防ごうとしているが、
そのガードは `tool_call_delta` イベント経路にしか効いておらず、`_tool_call_stream_chunks`
自体のループには効かない。

**修正:** `role` を先頭の call（`index == 0`）だけに付ける。

```python
opening_delta = {"role": "assistant", "tool_calls": [first]} if index == 0 else {"tool_calls": [first]}
```

- 単一 call の出力は完全に不変（その1件が `index == 0`）。
- 呼び出し側（`chat()` の stream 内）は従来どおり `tool_call_role_sent = True` を
  `_tool_call_stream_chunks` 呼び出し前にセットするので、後続の `tool_call_delta` が
  `role` を二重に出すことはない。
- OpenAI 実体のワイヤ形式（2件目以降の先頭チャンクは `{"tool_calls":[…]}` のみ）と一致し、
  `tool_call_delta` 経路の挙動とも一致する。

### 2.2 #2 — `tools`無し非ストリームで推論内容が捨てられる

**場所:** `Coordinator/mlxbar/api/openai_compat.py` `chat()` の非ストリーム `async for event`

v1.8.4 以降、ワーカーの `IncrementalToolStream` は `tools` 無しリクエストでも
`reasoning_only=True` で常時走る。推論モデルの `<think>…</think>` は Coordinator へ
`reasoning_delta` イベントとして届く。ストリーム経路はこれを `delta.reasoning_content`
として配信する（v1.8.4 で実装済み）。しかし**非ストリーム経路には `reasoning_delta` の分岐が無い**。
結果、`tools` 無し・非ストリームで推論モデルへ問い合わせると、思考内容は

- `content` にも出ず（v1.8.4 の意図どおり）、
- `reasoning_content` としても返らず、
- 完全に破棄される（無音のデータ欠落）。

v1.8.4 の設計書は「non-stream 応答へ `reasoning_content` を新設しない（現行仕様どおり
non-stream では推論を破棄）」を**非目的**として明記していた。当時の判断は「漏らさないことが目的で、
推論の可視化は別機能」。本リリースはこの判断を意図的に見直す:

- **「漏らさない」と「捨てる」は別物。** ストリームでは分離して返せているものが、
  非ストリームというだけで消えるのは API の非対称性であり、クライアントから見て予測不能。
- 修正は**純粋に加算的**：`message.reasoning_content` は推論があったときだけ現れ、
  OpenAI クライアントは未知フィールドを無視する（DeepSeek / vLLM 等が既に使う慣行）。
- **`content` へは一切戻さない。** 生の思考文が配信チャネルへ漏れた 2026-08-31 の事故
  （Ornith 1.5 35B をメインにした際、内部状態メモが Telegram へ配信）は再発しない。

**修正:**

```python
text = ""
reasoning_text = ""          # 追加
...
elif event.get("type") == "reasoning_delta":
    # first_token_ms 記録は delta 分岐と対称に（推論が最初の出力でも遅延計測が働くよう）
    if request.state.api_log.get("first_token_ms") is None:
        origin = getattr(request.state, "api_started_monotonic", None)
        if origin is not None:
            request.state.api_log["first_token_ms"] = round((time.monotonic() - origin) * 1000)
    reasoning_text += str(event.get("text", ""))
...
message = {"role": "assistant", "content": text or None if tool_calls else text}
if reasoning_text:
    message["reasoning_content"] = reasoning_text     # 追加（非空のときだけ）
```

Anthropic 互換の非ストリーム／ストリームは無変更（`anthropic_stream.py` は `reasoning_delta`
を `[]` に落とす。v1 の既知制約＝署名なし thinking ブロックは出さない）。

### 2.3 #3 — `"index": null` で `int(None)` が応答途中にクラッシュ

**場所:**
- `Coordinator/mlxbar/api/openai_compat.py` `_merge_tool_call_deltas`（非ストリーム経路が
  `tool_call_delta` イベントを畳むところ）
- `Coordinator/mlxbar/api/anthropic_stream.py` `AnthropicMessageBuilder._merge_tool_call_delta`

```python
index = int(delta.get("index", position))          # position は enumerate 由来
index = int(delta.get("index", len(self.content))) # Anthropic 側
```

`delta` に `index` キーがあり値が `None`（JSON の `null`）だと `dict.get` はデフォルトを
返さず `None` を返し、`int(None)` が `TypeError` になる。これは `async for` の中なので
応答ストリームが途中で 500／切断に落ちる。`float` や数字文字列など他の非整数でも同様。

**修正:** 両箇所で、整数（かつ `bool` でない）でなければ「`index` 省略時と同じ意味」＝
そのデルタの位置インデックスへフォールバックする。

```python
raw_index = delta.get("index", position)
index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else position
```

整数 `index` に対する挙動は完全に不変（従来の `int(x)` は `x` が int なら恒等）。
mlx-bar 自身のワーカーは常に int を出すため実運用の挙動変化は無く、外部由来・将来の
イベント整形の破損に対する防御。

### 2.4 #4 — 非括弧の `or`/`and` 混在式

**場所:** `Workers/common/server.py` の `rpc()` ロード例外分類（L397 付近）

```python
elif "out of memory" in lowered or "insufficient memory" in lowered or "metal" in lowered and "memory" in lowered:
```

演算子優先順位で `and` が `or` より強く束縛するため `a or b or (c and d)` と等価で、
**従来から論理的には正しい**。だが同ファイルの双子（`/generate` の例外分類、L610-611）は
既に明示括弧で書かれている。片方だけ暗黙なのは読み手を誤らせる。

**修正:** 括弧を付けて双子と揃える。**挙動は完全に不変。**

```python
elif "out of memory" in lowered or "insufficient memory" in lowered or (
        "metal" in lowered and "memory" in lowered):
```

## 3. 不変条件（守ること）

1. **単一 tool call のストリーム出力はバイト等価。** #1 の変更は2件目以降の call の
   先頭チャンクから `role` を落とすだけ。
2. **`content` に推論文を戻さない。** #2 は `reasoning_content` への加算のみ。`content` は
   `delta` イベント由来の `text` だけ（v1.8.4 の不変条件）。
3. **整数 `index` の畳み込みは不変。** #3 は非整数のみフォールバック。
4. **ワーカーの生成ロジックは #4 の括弧を除き無変更。** stop シーケンス、ハートビート、
   `tool_mode`、`finalize`、メモリ監視、`count`/`generation_tps` すべて不変。
5. **Coordinator のルート・設定 schema・管理 API・Swift GUI：1行も変更しない。**
6. **Anthropic 互換経路の可視挙動は不変**（#3 の内部ガードのみ、出力に影響なし）。
7. 日本語／英語の UI 文字列は追加なし（`Localizable.strings` 変更なし）。GUI 無変更のため。

## 4. 意図した挙動変更（ドキュメント化する）

- **`tools` 無し・非ストリームで推論モデルへ問い合わせると、応答 `message` に
  `reasoning_content` フィールドが付く**（推論ブロックがあった場合のみ）。従来はこの内容が
  破棄されていた。`content` は従来どおりクリーン。v1.8.4 設計書の「non-stream に
  `reasoning_content` を新設しない」という非目的を、データ欠落の解消として明示的に撤回する。
- **1レスポンスに tool call が2つ以上あるストリームで、2件目以降の tool call の
  先頭チャンクに `delta.role` が付かなくなる**（OpenAI 実体と一致）。単一 call は不変。

## 5. 検証

`TEST_PLAN_v1.9.1.md` を参照。v1.9.0 の 376 件は無改変で緑を維持し、4件の回帰テストを追加する
（計 380）。実機は稼働中の `/Applications/MLXBar.app` を v1.9.1 へ入れ替え後、LAN の
OpenAI 互換エンドポイントで #1・#2 を確認する。

# MLXBar v1.6.1

v1.6.0 shipped snapshot-based prompt reuse for architectures that cannot roll a cache back. On this machine it never once turned on. The capability probe asked an empty cache a question it cannot answer, read the resulting `AttributeError` as "not supported", and every hybrid model — Qwen3.5, Qwen3.8, anything mixing recurrent layers with full attention — stayed on `rollbackCapability: none`. This release is that fix and the four others it uncovered.

v1.6.0は「巻き戻せないアーキテクチャのためのスナップショット再利用」を入れましたが、**手元の実機では一度も有効になっていませんでした。** 能力判定が空のキャッシュに答えられない質問をし、返ってきた`AttributeError`を「非対応」と読んでいたためです。Qwen3.5・Qwen3.8のように再帰層と全注意層が混在するモデルはすべて`rollbackCapability: none`のままでした。この版はその修正と、そこから芋づるで出た4件です。

## What v1.6.0 actually did on this machine / v1.6.0が実機でしていたこと

One LAN agent session, one model, one conversation growing turn by turn:

| turn | messages | prompt | cached | tier | first token |
|---|---|---|---|---|---|
| 04:55 | 38 | 23,753 | 12,517 | disk | 55 s |
| 05:00 | 48 | 29,713 | 12,517 | disk | 65 s |
| 05:07 | 60 | 30,917 | 12,517 | disk | 66 s |
| 05:09 | 63 | 33,269 | **0** | cold | 128 s |
| 05:35 | 85 | 46,323 | **0** | cold | 175 s |
| 07:25 | 138 | 77,127 | **0** | cold | 343 s |

The reuse that was happening came from mlx-vlm's own exact-prefix cache, and it never grew past the 12,517 tokens it stored first. MLXBar's own tiers contributed nothing. Then it stopped matching at all, and every turn re-read the whole conversation.

同じモデル・同じ会話が伸びていく1セッションです。効いていた再利用はmlx-vlm自身のexactキャッシュで、最初に保存した12,517トークンから伸びません。MLXBar側の層は何も寄与していませんでした。やがて一致もしなくなり、以降は毎ターン会話全体を読み直しています。

**And nothing said so.** Across all 191 generations logged under v1.6.0, `cold_reason` was NULL every time and `shared_prefix_tokens` was 0 every time — the three columns v1.6.0 added to explain a slow request were never populated, because the one hop that carries them from the worker to the log did not list them.

**そして、そのことはどこにも出ませんでした。** v1.6.0で記録した191件の生成すべてで`cold_reason`はNULL、`shared_prefix_tokens`は0です。遅い要求を説明するためにv1.6.0が足した3列は、Workerからログへ運ぶ1か所に載っていなかったので、一度も値が入っていません。

## The probe asked the wrong question / 判定が尋ねる相手を間違えていた

`can_capture()` decides whether a snapshot is possible by checking that every cache component exposes a settable `state`. It ran `hasattr(entry, "state")`. The probe deliberately runs on an **empty** cache at load time, because that costs nothing — but an empty `KVCache` cannot answer: its getter reads `self.keys.shape[2]` while `keys` is still `None`, so the read raises `AttributeError`, and `hasattr` reports that as "no such attribute".

The question belongs to the type, not to the value:

```python
descriptor = getattr(type(entry), "state", None)
if not isinstance(descriptor, property) or descriptor.fset is None:
    return False
```

Nothing else changes: a type with no `state`, a plain attribute, or a getter-only property all answer `False` exactly as before.

`can_capture()`は「各要素がsetter付きの`state`を持つか」で捕獲可否を決めますが、`hasattr(entry, "state")`で**インスタンスの値**を読みに行っていました。判定は「コストがゼロだから」という理由でロード直後の**空のキャッシュ**に対して走ります。ところが空の`KVCache`はこれに答えられません。ゲッターが`self.keys.shape[2]`を読み、`keys`はまだ`None`だからです。例外は`hasattr`が「属性なし」に変換します。**型のプロパティを見れば、同じ質問に空でも答えられます。**

## A continuation could still reach `trim()` / 継続でも`trim()`に届きえた

v1.6.0 decided rollback capability before the runtime could reach `c.trim(n)`. It was not enough. mlx-vlm computes how many tokens to drop from **the cache's own offset**, not from the length MLXBar returns, so a cache sitting ahead of its own labels turns an ordinary continuation into a `trim()` on a recurrent component. The live worker recorded 14 of these while this release was being written.

v1.6.1 checks that the answer leaves nothing to drop before giving it:

```python
if natural >= len(held) and (cached is None or cached <= natural):
    return natural          # nothing is dropped; every architecture can reuse it
```

v1.6.0は`c.trim(n)`に届く前に可否を判定していましたが、それでは足りませんでした。ランタイムは破棄量を**キャッシュ自身のoffset**から計算するため、キャッシュが自分のラベルより先行していると、単なる継続でも再帰層に対して`trim()`が呼ばれます（この版を書いている間に稼働中のWorkerで14件記録されていました）。v1.6.1は、返す長さが破棄を生まないことまで確認してから答えます。

## The snapshot swap was unreachable, and unsafe if reached / 差し替えは到達せず、到達しても危なかった

The restore path was guarded by "swap only while nobody has read the cache". mlx-vlm 0.6.15 reads `.cache` twice — once to test it against `None`, then again after asking for the prefix length — so the guard counted the null check and **the restore never ran even once**. Fixing the count alone would have exposed the other half: the path set `.cache` to `None`, and the runtime, having already passed its own null check, goes on to compute a drop amount by iterating whatever it is given. `TypeError: 'NoneType' object is not iterable`.

v1.6.1 restores **into** the retained list instead of replacing it, and empties that list rather than discarding it. Both call orders are then correct, and there is nothing for the runtime to trip over.

復元経路は「まだ誰も`.cache`を読んでいない間だけ差し替える」で守られていました。mlx-vlm 0.6.15は`.cache`を2回読みます（null検査で1回、prefix長を尋ねた後にもう1回）。ガードは前者を数えてしまい、**復元は一度も走りませんでした。** 数え方だけ直すと今度は反対側が出ます。この経路は`.cache`を`None`にしていましたが、ランタイムは自分のnull検査を通過済みで、その後に渡されたものを反復して破棄量を計算するからです。v1.6.1は保持しているリストを**中身ごと入れ替える**方式にし、破棄するときも空にするだけにしました。呼び出し順のどちらでも正しく、ランタイムがつまずくものも残りません。

## Every snapshot write failed, and left 200 MB behind / 書き込みは毎回失敗し、200 MBだけ残していた

Once snapshots ran, the first disk write failed: `mx.save_safetensors` appends `.safetensors` to a path that does not already end in it, so writing to `<digest>.safetensors.tmp` produced a third name that the following `chmod`, the rename and the eviction all miss. The snapshot was never readable and the leftover was never reclaimed — 214 MB per attempt, measured on disk.

The temporary name now ends in the extension, and unfinished writes are swept at load. **The unit tests could not have caught this**: the fake `mlx.core` wrote exactly where it was told. It now appends the extension the way the real one does.

スナップショットが動き出した最初のディスク書き込みが失敗しました。`mx.save_safetensors`は拡張子で終わらないパスに`.safetensors`を足すため、`<digest>.safetensors.tmp`へ書くと、後続のchmod・rename・容量管理がどれも見つけられない第三の名前ができます。読めないスナップショットと、回収されない残骸（実測214 MB／回）だけが残っていました。一時ファイル名を拡張子で終わるようにし、未完了の書き込みは次のロード時に回収します。**この不具合は単体テストでは出ません**——偽の`mlx.core`が言われたとおりの場所へ書いていたためです。実物と同じく拡張子を足すようにしました。

## Verification / 検証

`scripts/verify-prompt-cache-runtime.py` runs against the installed mlx-vlm with real MLX arrays and **no model weights**, so it can run on every release. Both defects above are exactly the kind a substituted `mlx.core` cannot show.

| check (mlx-vlm 0.6.15) | v1.6.0 | v1.6.1 |
|---|---|---|
| `can_capture(empty hybrid)` | **False** | True |
| `rollback_capability(empty hybrid)` | **`none`** | `checkpoint` |
| continuation, cache ahead of its labels | **reaches `trim()`** | nothing dropped |
| restore visible to a caller that read `.cache` first | **no** | yes |
| drop amount after a refused branch | **`TypeError`** | 0 |
| snapshot written under the name the index records | **no** | yes |

On the machine itself, after the update: `rollbackCapability: checkpoint`, `affordableTokens: 194,259` (was 0), a 214 MB snapshot on disk under its own digest with no leftovers, and a continuation reporting `cached_tokens: 925` of a 942-token prompt.

`scripts/verify-prompt-cache-runtime.py`は、インストール済みのmlx-vlmと実際のMLX配列に対して、**モデルの重みなしで**走ります。上の2件はどちらも、差し替えた`mlx.core`では出ない種類でした。実機では更新後に`rollbackCapability: checkpoint`、`affordableTokens: 194,259`（従来0）、ダイジェスト名で保存された214 MBのスナップショット（残骸なし）、942トークンのプロンプトで`cached_tokens: 925`を確認しています。

## Also in this release / この版のその他

- **`usage.prompt_tokens_details.cached_tokens`** — the standard place an OpenAI-compatible client reads reuse from. Omitted entirely, rather than reported as zero, when the runtime did not measure it.
- **`GET /v1/models` states `modalities`** and no longer lists catalog rows that cannot generate. A diffusion model's `vae`, `text_encoder` and `transformer` folders are indistinguishable from MLX weights from the outside; they stay in the management API, which exists to explain what was skipped.
- **An unknown model name returns 404 while another request runs**, instead of a retryable `ENGINE_BUSY`.
- **The API port binds again right after an update.** Quit, replace, launch leaves the previous listener in `TIME_WAIT`; without `SO_REUSEADDR` the public API simply did not come back. A port another process is actually listening on still fails, and is still reported as a conflict.
- **Unexpected management-API failures are logged** with route and traceback, and the cache report answers while the worker is generating instead of timing out into a bare 500.
- **README has an OpenClaw section.** OpenClaw's idle watchdog does not count SSE comments, so MLXBar's keep-alive does not reach it; `models.providers.<id>.timeoutSeconds` is required and is the only setting that lifts the implicit 120-second cap.
- Streaming no longer re-serialises the whole message list once per generated token (0.088 ms/token at 33 KB of messages, 1.1 ms/token at 400 KB).

- **`usage.prompt_tokens_details.cached_tokens`を返します。** OpenAI互換クライアントが再利用量を読む標準の場所です。ランタイムが計測しなかった場合は0ではなくフィールドごと省きます。
- **`GET /v1/models`が`modalities`を返し、生成に使えないカタログ行を外します。** 拡散モデルの`vae`・`text_encoder`・`transformer`は外形がMLXの重みと区別できません。「何を読み飛ばしたか」を説明する管理API側には残します。
- **他の要求の実行中でも、存在しないモデル名はHTTP 404を返します**（再試行可能な`ENGINE_BUSY`ではなく）。
- **更新直後にAPIポートを再び確保できます。** 終了→差し替え→起動では前の待ち受けが`TIME_WAIT`に残り、`SO_REUSEADDR`なしでは公開APIが戻ってきませんでした。別プロセスが実際に待ち受けているポートは従来どおり失敗し、競合として表示されます。
- **管理APIの想定外エラーを経路とtracebackごと記録します。** キャッシュ状況は生成中でも答えます（従来は10秒で切れて素の500）。
- **READMEにOpenClaw向けの節を追加しました。** OpenClawの無応答監視はSSEコメントを数えないため、MLXBarのheartbeatが届きません。`models.providers.<id>.timeoutSeconds`の指定が必要で、暗黙の120秒上限を上げられるのはこの設定だけです。
- ストリーム中にmessages全体をtokenごとにJSON化しなくなりました（33 KBで0.088 ms/token、400 KB相当で1.1 ms/token）。

## Upgrading / 更新方法

Quit MLXBar, replace `MLXBar.app` in Applications, and launch it again. Settings, models and the API key are kept. Existing disk caches stay valid; snapshots start being written on the first completed turn after the update.

MLXBarを終了し、「アプリケーション」の`MLXBar.app`を差し替えて起動し直してください。設定・モデル・APIキーはそのまま残ります。既存のディスクキャッシュも有効なままで、スナップショットは更新後に最初のターンが完了した時点から書かれ始めます。

## Checksum

```
SHA-256 (MLXBar-1.6.1.dmg) = 9830933162c66d90f2d99f9d055f9c986e2f9de2e501e3989d6ecc563533b1d6
```

macOS 14 or later, Apple Silicon. Requirements are unchanged from v1.6.0.

macOS 14以降、Apple Silicon。動作要件はv1.6.0から変わりません。

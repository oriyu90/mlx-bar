# MLXBar v1.6.0 テスト計画

対象: 中断後の再開、巻き戻し不能アーキテクチャでの再利用、容量算出、可観測性。

## 1. 自動テスト（CIで毎回）

```sh
Coordinator/.venv/bin/python -m pytest Tests -q     # 253件
swift build --disable-sandbox
```

新規 `Tests/test_prompt_cache_reuse.py`（37件）が押さえている契約:

| 契約 | テスト |
|---|---|
| 中断したターンはキャッシュを保ち、ラベルを組み直す | `test_an_interrupted_turn_keeps_its_cache_and_relabels_it` |
| offsetが一致しなければ破棄する | `..._is_discarded_when_the_cache_is_not_where_it_should_be` |
| offsetを報告しないランタイムでは破棄する | `test_a_cache_without_an_offset_cannot_be_relabelled` |
| メモリ逼迫による中断は解放する | `test_an_interruption_for_memory_pressure_releases_the_cache` |
| 巻き戻し不能な再利用をランタイム到達前に拒否する | `..._is_refused_before_the_runtime_tries` |
| スナップショットが一致すれば復元する | `test_a_branch_restores_a_snapshot_when_one_matches` |
| 呼び出し側が先にcacheを読んだら差し替えない | `..._not_swapped_in_once_the_caller_holds_the_old_cache` |
| 容量算出が全注意層だけを数える | `test_budget_counts_only_full_attention_layers_per_token` |
| 上限に収まらない書き込みを拒否する | `test_a_store_refuses_to_write_what_its_limit_cannot_hold` |
| 最長の利用可能スナップショットを選ぶ | `test_the_longest_usable_snapshot_wins` |
| cold理由が閉じた語彙である | `test_cold_reason_is_a_closed_vocabulary` |

**偽ランタイムの限界（重要）**: `mlx.core`を差し替えているため、状態ツリーの構造（list / tuple / `None`の穴 / manifest往復）と再利用方針の分岐は検証できますが、**MLXの配列意味論は検証できません**。v1.5.1で「偽モジュールでは検証できない判定」を一度取りこぼしているため、下の実機項目を省略しないこと。

## 1.5 実ランタイムに対する検証（v1.6.0リリース時に実施済み）

偽`mlx.core`では証明できない部分を、**重みをロードせずに**実ランタイムのキャッシュクラスで確認した。
モデルは不要なので、リリースごとに再実行すること。

```sh
VENV="$HOME/Library/Application Support/MLXBar/runtimes/mlx-vlm/slots/<slot>/.venv/bin/python"
"$VENV" - <<'PY'
import sys; sys.path.insert(0, "Workers")
import mlx.core as mx
from mlx_vlm.models import cache as rc
from mlx_vlm.generate import PromptCacheState
from common import cache_state
from mlx_vlm_worker.prompt_cache import build_guarded_state
# ... 本文はリリース時のログを参照
PY
```

実施結果（mlx-vlm 0.6.15）:

| 確認 | 結果 |
|---|---|
| 実`ArraysCache`＋実`KVCache`の能力判定 | `can_trim=False` / `can_capture=True` / `checkpoint` |
| 実`KVCache`単体 | `can_trim=True` |
| `cached_length`が実offsetを読む | 8 |
| capture後にライブを進めても複製が追随しない | ライブ12 → 復元8、`arrays[0]`は`[1,2,3]`のまま |
| `None`の穴が往復で保たれる | 保たれた |
| flatten→unflatten→restore | 復元長8で一致 |
| 実`PromptCacheState`を継承できる | `isinstance`成立 |
| 継続 / 巻き戻し不能な分岐 / 巻き戻し可能な分岐 | `4 reuse` / `0 cold reuse_unsupported`（キャッシュ解放） / `2 trim` |

**まだ検証していないもの**: 実モデルを載せた状態での中断→再開（下の2-1・2-2）。**リリース時点で未実施。**
中断復帰の失敗はすべてcold prefillへ退避する設計なので、最悪でもv1.5.3と同じ速度に戻るだけだが、
スナップショット復元だけは誤ると出力が変わりうる経路なので、常用前に2-1を通すこと。

## 2. 実機テスト（リリース前に手動）

### 2-1. 小さいモデルで再現できるもの（`Qwen3.5-9B-MLX-8bit`、約9 GB）

`Qwen3.8-27B`と同じ`qwen3_5`ハイブリッド構成（`text_config.layer_types`に`linear_attention`）なので、27 GBを待つ必要はない。

| # | 手順 | 期待 |
|---|---|---|
| A | モデルをロードし、Coordinatorログを見る | `Prompt cache: rollback=checkpoint, 32.0 KB/token, snapshots affordable up to N tokens` が1行出る |
| B | メニューバーを開く | 「プロンプト再利用: スナップショット方式 · 約N トークンまで保存可」 |
| C | 長い会話を2ターン続ける | 2ターン目の`cache_tier`が`memory`、first token < 3秒 |
| D | 3ターン目を生成中にキャンセル → 続けて同じ会話を1ターン | `cache_tier`が`memory`、`cold_reason`なし。**v1.5.3では必ず`cold`になっていた箇所** |
| E | キャンセル後、部分出力を含めずに送り直す | `cache_tier`が`memory`または`disk`、probeの`action`が`restore` |
| F | `worker-mlx-vlm.log`を確認 | `'ArraysCache' object has no attribute 'trim'`が**1件も出ない** |
| G | `promptCache.diskMaxGB`を1に下げて再ロード | 「キャッシュ上限が小さく、スナップショットを1件も保存できません」を表示し、`prompt-cache/checkpoints`にファイルが増えない |
| H | 生成中にランタイムスロットを切り替える | `ENGINE_BUSY`で拒否される |
| I | MLXBarを再起動 | 前回のモデルが自動でロードされる。手動アンロード後の再起動では**ロードされない** |

### 2-2. 27B級でしか測れないもの（`Qwen3.8-27B-MLX-8bit`、約29.5 GB）

| # | 手順 | 期待 |
|---|---|---|
| J | 10万トークン級のZCodeセッションで、生成中にESCし、続けて1ターン | first tokenが数秒。v1.5.3の実測は477秒 |
| K | 同セッションでメモリ使用量を観測 | スナップショット保持でKVがおよそ2倍（+6.4 GB）を超えない。3倍に達したらremember側の解放漏れ |
| L | `promptCache.diskWriteBudgetGB`を既定32のまま20ターン継続 | 書き込みが逓減し、途中で`write_budget_reached`にならない |
| M | 生成中にメモリ上限へ到達させる | `MEMORY_PRESSURE`後の次要求が`cold_reason: memory_pressure`。キャッシュが解放されている |

### 2-3. 容量算出の突き合わせ

```sh
python3 scripts/cache-capability-matrix.py ~/.lmstudio/models
```

- `Qwen3.8-27B-MLX-8bit` が `checkpoint` / `64.0 KB` であること。
- ロード後の`GET /api/v1/status`の`promptCacheHealth.perTokenBytes`が`65536`で一致すること。
  **スクリプトはconfigからの予測、statusは実キャッシュへのプローブ結果なので、食い違いは所見**（ランタイムがそのアーキで使うキャッシュ種別を変えたということ）。

## 3. リリース

```sh
sh scripts/build-release.sh
sh scripts/verify-release.sh
shasum -a 256 dist/MLXBar-1.6.0.dmg > dist/MLXBar-1.6.0.dmg.sha256
```

- SHA-256を`RELEASE_NOTES_v1.6.0.md`へ反映する。
- 紹介サイトは`oriyu90/studio-rizi`の`website/projects/mlx-bar/`にあり、**このリポジトリには無い**。共通ルール「htmlの更新は実装後の最終作業」に従い、そちらで版数・DMG名・`lastmod`を更新すること。

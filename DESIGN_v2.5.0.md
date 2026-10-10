# MLXBar v2.5.0 設計：実験的KVキャッシュ量子化（bf16/8bit/4bit）

## 1. 目的と範囲

v2.5.0は、mlx-lmのネイティブ`QuantizedKVCache`経路をmlx-barのmlx-lm Workerへ
配線し、ライブKVキャッシュの量子化（bf16＝無効・既定、8bit、4bit）を実験的
オプションとして追加する。対象はmlx-lmのライブ生成KVのみ。mlx-vlm、
Paged KVのSSDブロック形式、Adaptive MemoryのHybrid KVとは統合しない。

上流の確定事実（mlx-lm main、`mlx_lm/models/cache.py`・`generate.py`）：

- `QuantizedKVCache(group_size=64, bits=8)`はper-token group-scalar affine量子化。
  K/V同一bits、RoPE後、キャリブレーション不要。`KVCache.to_quantized()`で変換。
- `RotatingKVCache.to_quantized()`は`NotImplementedError`。Batch/Chunked/複合は
  `to_quantized`を持たず、`maybe_quantize_kv_cache`は静かにskipする。
- `generate_step`/`stream_generate`は`kv_bits`/`kv_group_size`/
  `quantized_kv_start`を受け、`maybe_quantize_kv_cache`で`offset >= start`の層を
  その場で置換する。CLI既定は`--kv-bits None`、`--kv-group-size 64`、
  `--quantized-kv-start 5000`。`mx.quantize`は2/3/4/5/6/8bitを受け付けるが、
  5/6bitは直接ctorにpacking不具合があり、文書は8/4bitへ誘導する。
- 品質報告は8bitほぼ無劣化、4bitは一部checkpointで劣化（Qwen2.5-7Bでの崩壊報告あり）。
  KがVより敏感。性能は量子化pathが3op構成（2x quantized_matmul＋softmax）のため
  固定費があり、長いcontextで帯域削減が勝つ構造。短文ではdefer（start）が有利。

## 2. リリース不変条件

- `experimental.kvCacheQuantization.mode`既定`bf16`。bf16時はCoordinatorが
  `MLXBAR_KV_QUANT_*`を渡さず、Workerは量子化分岐へ到達せずv2.4.1とバイト等価。
  専用import・保存先・namespace分岐は量子化時のみ。
- 許可するのは上流`to_quantized`を持つ層のネイティブ変換のみ。自前の量子化
  カーネル・ attention実装・K/V分離bits・groupSize可変・start可変は本版で作らない。
  groupSize 64・start 0に固定し、statusに実効値を載せて将来phaseへ残す。
- Paged KV（plain `KVCache`の256-token SSDブロック）とは排他。量子化中有効時は
  Paged環境変数を渡さず、Paged tierを未構築とし、status理由`kv_quantized`を表示する。
  両方onの設定自体は422にせず、量子化を優先してPagedを休止するfail-closed。
- プロンプト再利用（RAM `LRUPromptCache`＋SSD snapshot）はKVモード別namespaceと
  メモリキーで分離する。bf16/8bit/4bitのsnapshotは混ざらない。型不一致のfetchは
  cold miss扱い、storeは型不一致を書き込まない。budget計算はbf16のまま
  （量子化実使用量＜bf16見積りのため、admissionは保守側に倒れる）。
- 量子化失敗は生成を失敗させない。最初のモデル出力前の失敗だけfresh bf16 cacheで
  1回再試行し、理由を`kvQuant.fallbackReason`に記録する。出力開始後の失敗は
  再実行せず例外をそのまま上げる（二重応答を作らない）。3連続のようなcircuitは
  本版で持たず、Pagedのcircuitと干渉させない。
- mlx-vlm Workerは変更しない。量子化設定はmlx-lm起動時にのみ配線する。
- `schemaVersion`は1のまま。追加キーのみ。既存`config.json`はそのまま読める。
- 日英完全対応：新規`LS()`キーは全て`en.lproj`へ英訳を追加し、重複キーを作らない。
  `MenuBarViewModel.ui()`の直書き文言は日英両 literal を持つ。

## 3. 設定と運用

- 設定：`experimental.kvCacheQuantization.mode ∈ {bf16,8bit,4bit}`。
  検証は`settings.py:_validate`が正本。GUI（Picker＋Swift guard）とCLI
  （`config set-kv-cache-quantization --mode`）はそのmirror。
- GUI：「Settings…」→「キャッシュ」に「KVキャッシュ量子化（実験）」Section。
  Picker＋適用ボタン、現在の動作（active/mode）、fallback理由、Paged休止注意、
  mlx-lmのみ・8bit/4bit品質注意・再起動反映注意を表示する。
- CLI：`mlxbarctl config set-kv-cache-quantization --mode bf16|8bit|4bit`。
  mode未指定はValueError（他namedコマンドと同一契約）。
- Worker起動：`supervisor._configure_kv_quant_environment`がmlx-lmのみに
  `MLXBAR_KV_QUANT_MODE/BITS/GROUP_SIZE/START`を立てる。bf16では何も立てない。
  Paged設定は量子化中は立てない。
- Worker生成：`adapter._kv_generate_kwargs()`を`stream_generate`へ渡す。
  fetch後の型検査（`_cache_matches_kv_mode`）で混入をcoldへ落とし、
  `QuantizedKVCache`由来の失敗を初回出力前1回だけbf16再試行する。
- 状態：`prompt_cache_stats()["kvQuant"]`と`capabilities()["kvQuant"]`に
  mode/bits/groupSize/quantizedStart/active/fallbackReasonを載せる。
  Paged statsは量子化中`disabledReason: kv_quantized`。

## 4. 検証

`TEST_PLAN_v2.5.0.md`に記録する。自動テスト（parity・validation・env gating・
namespace分離）、`swift build`、`.strings`重複検査、実MLX weight-free probe
（plain eligible・quantized/rotating非eligibleの継承確認＋量子化 round-trip）、
DMG build/verify、ad-hoc署名確認を行う。実モデルでの品質・TTFT・ピークメモリの
定量比較は本版の必須にせず、次版への持ち越しとして明記する。

## 5. 将来phase（本版でやらないこと）

- groupSize/startの可変化、K/V分離bits、Rotating量子化、Batch量子化、
  Pagedブロックの量子化対応、RAMホット層、Hybrid KV再利用との統合。
- 実機での8bit/4bit品質・速度・メモリの定量ゲート通過後に、拒否・固定値・
  文言（「実験」表示）を見直す。

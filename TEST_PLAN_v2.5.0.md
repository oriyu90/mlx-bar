# MLXBar v2.5.0 テスト計画：KVキャッシュ量子化

## 1. 自動テスト

- `Tests/test_gui_settings_parity.py::KVQuantizationTests`（4件）：
  CLI送信形状、mode未指定拒否、GUI payload受理（bf16/8bit/4bit）、不正値拒否、
  既定bf16。
- `Tests/test_cli.py`：`set-kv-cache-quantization`送信形状＋mode未指定拒否。
- `Tests/test_core.py`：
  既定bf16・受理・拒否、bf16時env空・mlx-vlmへenv無し・8bit時env設定、
  Paged＋量子化の同時設定でPaged envが立たず量子化envが立つこと。
- `Tests/test_paged_kv_cache.py`：
  namespace/model_keyのモード分離、量子化中のPaged未構築と
  `disabledReason: kv_quantized`、`kvQuant` status、型一致検査
  （bf16×quantized拒否、8bit×4bit拒否、8bit×plain許容）。
- 既存全テストの回帰（bf16既定でv2.4.1と同一経路）。

実行：`PYTHONPATH=Coordinator python -m pytest Tests/ -q`（system python 3.14＋
httpx/fastapi/packaging/pytest）。`Coordinator/.venv`があればそちらでも実行し、
既知のasyncioタイミング失敗（3.12のみ、`test_model_pool.py`）と区別する。

## 2. 静的・ビルド検証

- `swift build`（Debug、Swift 6 strict concurrency）成功。
- `.strings`重複キー検査：新規重複なし（既存`モデル未ロード`重複1件は残置）。
- `plutil -lint`（Info.plist、LaunchAgents）。
- `sh scripts/build-release.sh`＋`VERSION=2.5.0 sh scripts/verify-release.sh`通過。
  ad-hoc署名、`codesign -v --strict` OK、DMG `hdiutil verify` VALID、
  app 2.5.0/build 44。

## 3. 実MLX runtime検証（weight-free）

- 組み込みruntime（mlx-lm 0.31.x系）の実tensorで：
  plain `KVCache`はeligible、`QuantizedKVCache`と`RotatingKVCache`は非eligible
  （既存`scripts/verify-paged-cache-runtime.py`の継承確認）。
- `KVCache.to_quantized(group_size=64, bits=8/4)`のround-trip（offset保持、
  state arity 5、復元後のoffset一致）を実MLXで確認。失敗時は量子化設定を
  bf16へ戻して出荷可否を判断する（fail-closedのため生成自体は継続する）。

## 4. 実モデル検証（任意・持ち越し可）

- 実モデルでの8bit/4bitの応答品質・TTFT・ピークメモリの定量比較は本版の合否に
  しない。実施した場合はモデル名・context長・`kvQuant` status・fallback有無を
  記録し、次版の既定値・文言見直しの材料とする。
- Paged＋量子化の同時onでPagedが休止し理由が表示されること、
  モード切替後に旧モードのsnapshotを再利用しないこと（namespace分離）を
  隔離Coordinatorで確認する。

## 5. GUI目視（持ち越し記録）

- 実画面目視はv2.4.0から継続未実施の可能性がある。次版で実機GUI確認を行うこと。
  本版ではビルド・配線・文言・従属状態の確認で補完する。

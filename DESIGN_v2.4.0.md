# MLXBar v2.4.0 設計：OMLX相当機能のGUI設定管理化

更新日: 2026-09-23
対象: `oriyu90/mlx-bar` v2.3.0 → v2.4.0

## 1. 目的と範囲

v2.3.0までに搭載されたOMLX相当機能（Paged KV再利用、capability probe＋fail-closed、
SSD/memory上限の個別管理、pool並行生成、memory guard比率、RAG、Adaptive Memory）は、
設定キーとしては存在するがGUIから操作できないものが19件あった（調査記録は開発時計画書を参照）。
本版は**新規エンジン機能を一切追加せず**、既存キーをGUI設定画面と `mlxbarctl` の
named コマンドで管理可能にする。対象キーの一覧とGUI/CLIの対応は §3 の表を正本とする。

やらないこと（将来phaseのゲートは開けない）:

- `experimental.pagedKVCache.memoryTier`（`off`固定の検証を維持）、
  `experimental.adaptiveMemory.hybridKVReuse`（本版から `true` を検証で拒否。§2 参照）、
  `promptCache.memoryBlocks` の `auto` 化、KV量子化、chunked prefill、speculative系。
  これらは状態表示に留め、有効化手段を提供しない。

## 2. リリース不変条件

1. 既定値は全て v2.3.0 と同一。未設定の既存 `config.json` はバイト等価に動作する
   （`SettingsStore.deep_merge(DEFAULTS, ...)` のため。新規キーは追加していない）。
2. 検証範囲は `settings.py:_validate` が唯一の正本。Swift側ガードとCLI側ガードは
   これと完全一致させ、サーバ側422を第二の防壁とする（範囲の二重化、単一正本）。
3. 唯一のサーバ側動作変更は `hybridKVReuse: true` の拒否である。これは
   `DESIGN_OMLX_STYLE_PAGED_KV_CACHE_PROPOSAL.md` §3.3 と `mlx-bar.md` の
   「4重gateが将来も必須」契約を検証で強制するもので、既定 `false` の既存設定には
   影響しない（`Tests/test_core.py` の既存アサートが `false` を確認済み）。
4. 日本語文字列をキーとし、英語は `en.lproj/Localizable.strings` のみに追加する
   （`AppLanguage` の規約。`.strings` 内の重複キーを禁止。既存の `モデル未ロード`
   重複は本版の範囲外として残す）。
5. 公開物に秘密・絶対パス・環境固有値を含めない。署名は ad-hoc（ルール8）。

## 3. 設定と運用

### 3.1 追加・拡張した操作面

| 対象キー | GUI | CLI |
|---|---|---|
| `models.pool.maxReplicasPerModel` (1–8) | モデル > 複数モデル常駐 Stepper（適用は再起動反映の注意文つき） | 既存 `config set-model-pool --max-replicas-per-model` |
| `models.pool.perGenerationHeadroomGB` (0自動/0.25–32) | モデル > ヘッドルーム Stepper＋単独適用（新規レーンから反映） | 新規 `--per-generation-headroom-gb` |
| `models.pool.profiles[].maxMemoryGB` (1–512) | モデル > 常駐モデル行の上限 Stepper（他profile fieldを保持して書換え） | 既存 `model pin --max-memory-gb` |
| `promptCache.keepGenerations` (1–10) | キャッシュ > 詳細キャッシュ設定 | `prompt-cache set --keep-generations` 追加 |
| `promptCache.memoryRatio` (0–0.5) | 同上（Slider相当のStepper） | `--memory-ratio` 追加 |
| `promptCache.branchCheckpoint` (auto/off) | 同上 Picker | `--branch-checkpoint` 追加 |
| `promptCache.diskWriteBudgetGB` (0–4096) | 同上（1 snapshotがGB級の注意文つき） | `--write-budget-gb` 追加 |
| `promptCache.memoryBlocks` | 状態表示のみ（auto化は見送り） | なし |
| `generation.maxPromptCharacters` (1–10M) | モデル > プロンプト入力上限 | 新規 `config set-generation-limits` |
| `generation.maxImages` (0–128) | 同上（0＝全面禁止の説明つき） | 同上 |
| `generation.maxImageBytes` (1–2GB) | 同上（MB単位で表示） | 同上 |
| timeouts 5種 | モデル > 生成タイムアウト | 同上 |
| `memoryLimitRatio`/`wiredLimitRatio`/`cacheLimitRatio` | モデル > メモリガード（wired≤memoryの相互検査つき） | 同上 |
| `api.maxRequestBytes` (0–4GB, 0＝自動導出) | APIサーバー > 要求上限（MB単位） | 新規 `config set-api-limits` |
| `api.maxConcurrentConnections` (1–1024) | 同上 | 同上 |
| `general.logLevel` | 一般 > Picker (debug/info/warning/error) | 新規 `config set-log-level` |
| `general.preloadLastModel` | 一般 > Toggle | `config set-flag preload-last-model` 追加 |
| `models.lmStudio.enabled` | LM Studio > Toggle | `config set-flag lmstudio-enabled`＋`lmstudio set-enabled` 追加 |
| `models.lmStudio.folder` | LM Studio > 選択/クリア（nullで既定復帰） | `lmstudio set-folder` 追加（空でクリア） |
| `rag.embedding.timeoutSeconds/batchSize` | 知識ベース > Steppers | 既存 `config set-rag` |
| `rag.maxChunksPerCollection` | 同上（純Python全件走査の注意文つき） | 既存 `config set-rag` |
| `adaptiveMemory.fallbackToExact` | モデル > 実験的機能 Toggle | `--fallback-to-exact` 追加 |
| `adaptiveMemory.{persistentLatentCache,persistentHybridKV,hybridKVReuse}` | 状態表示のみ | なし |
| `pagedKVCache.memoryTier/memoryRatio` | 状態表示のみ（SSD層のみの注意文） | なし |

### 3.2 プロセス・スレッド契約の変更

なし。追加した Swift 関数は既存の `perform`＋`PUT /api/v1/settings`＋`refreshSettings`
経路のみを使い、新規エンドポイント・新規バックグラウンド処理はない。

## 4. 検証

- `Tests/test_gui_settings_parity.py`（新規24件想定→実績はTEST_PLANに記録）:
  GUIが送るpayload形状を `SettingsStore.update` で受理確認する契約テスト、
  CLI named コマンドの範囲テスト、将来ゲート（`hybridKVReuse`/`memoryTier`）の
  拒否テスト。
- 既存回帰 492 件の維持（固定順・ランダム順の両方をCI手順に準拠して実施）。
- `swift build`（Debug）と `swift build -c release`（build-release.sh 経由）の成功。
- 日英 `.strings` の重複キー検査（スクリプト相当を手動実施。既存重複1件は対象外）。
- 実機smokeは TEST_PLAN_v2.4.0.md §2–§5 に従う（設定の適用→再起動→反映確認、
  GUI目視は日英）。

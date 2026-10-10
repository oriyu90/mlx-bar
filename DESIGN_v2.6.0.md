# DESIGN v2.6.0 — 一括設定モード / 設定JSON入出力 / 初回セットアップ / 最新ランタイム対応 / CLI・GUI両対応

- 版数: app 2.6.0 / build 46。Coordinator `__version__` 2.6.0。`schemaVersion` は 1 のまま（追加のみ）。
- 方針: 既定値不変（preset適用・wizard完了・import時のみ変更）、追加のみ、検証失敗は fail-closed、
  適用不可は skip＋理由報告。Coordinator が正本、GUI/CLI は薄いクライアント（三者契約テストで固定）。

## 1. 一括設定モード（presets）

### 1.1 管理対象キー（preset footprint、6キー）

| キー | 軽量 (light) | フル (full) |
|---|---|---|
| `experimental.kvCacheQuantization.mode` | `8bit` | `bf16` |
| `models.pool.totalMemoryRatio` | `0.80`（物理メモリの80%） | `0.85` |
| `models.pool.minimumSystemReserveGB` | `clamp(round(physGB*0.15), 4, 32)` | `clamp(round(physGB*0.10), 4, 32)` |
| `models.pool.defaultPerModelMaxGB` | `clamp(round(physGB*0.25), 4, 64)` | `clamp(round(physGB*0.35), 8, 128)` |
| `promptCache.diskMaxGB` | `clamp(round(freeGB*0.02), 2, 20)` | `clamp(round(freeGB*0.04), 4, 40)` |
| `promptCache.diskWriteBudgetGB` | `clamp(round(freeGB*0.05), 8, 64)` | `clamp(round(freeGB*0.10), 16, 128)` |

- `physGB`＝物理メモリ GiB、`freeGB`＝アプリデータ領域の空き GiB（`shutil.disk_usage`）。
- 全値は `_validate` 範囲内に clamp 済みであることを単体テストで固定（-property test ではなく代表値＋境界値）。
- `maxResidentModels`・`generationConcurrency` は同時使用数の意思決定＝wizard/ユーザーが持つ。preset は触らない。
- Paged・adaptive・RAG・生成上限・API・セキュリティは実験／用途依存のため preset 外。
- 空き容量が 50GB 未満の場合は `warnings` に注意を添える（値は安全側最小値のため適用自体は継続）。

### 1.2 状態と判定

- 新規 `presets` section: `{"mode": "custom", "lastApplied": None}`。
  既定 `custom`＝既存ユーザーの設定はそのまま「カスタム」と表示され、動作は v2.5.1 と同一。
- 新規 `setup` section は §3（wizard）で定義。
- `_validate` 追加: `presets.mode ∈ {light,full,custom}`、`lastApplied` は None または dict。
- effective 判定（`GET /presets/status` と GUI Picker 表示の正本）:
  現在の6キー値がその場で再計算した light プロファイルと完全一致→`light`、
  full と一致→`full`、それ以外→`custom`。stored `mode` は「最後に選択/適用したモード」の記録。
- `custom` の選択は no-op（設定変更なし、`presets.mode=custom` の記録のみ）。
  管理対象キーを1つでも手で変えると effective は自動で `custom` になる（再計算比較のため）。

### 1.3 API・CLI・GUI

- `POST /presets/apply {"mode": "light"|"full"}`: 計算→`settings.update`（6キー＋`presets.mode`＋`lastApplied{mode,at,values}`）→
  `scan_job()` を発行（202 `{applied, values, warnings, scanJob}`）。
  `mode=custom` は 400（何もしない選択は PUT `/settings` 直接で行う）。
  適用はサービス再起動不要のキーのみ（KV量子化は Worker 起動時 latch のため再起動注意を `warnings` に添える。
  既存の再起動注意文と同一扱い）。
- CLI: `config apply-preset light|full [--wait]`（scan job の待機は `--wait`）、
  `config preset-status`（effective＋両プロファイル値＋warnings を表示）。
- GUI 一般ページ: 一括設定モード Picker（軽量/フル/カスタム）＋適用値の説明文。
  light/full 選択＝即時適用（確認ダイアログ付き、既存 LAN 公開と同一流儀）、custom 選択＝記録のみ。

## 2. 設定JSONの書出・読込（export / import）

### 2.1 ファイル形式（v1）

```json
{
  "format": "mlxbar-settings-export",
  "formatVersion": 1,
  "exportedAt": "2026-10-11T...",
  "appVersion": "2.6.0",
  "schemaVersion": 1,
  "hardware": {"physicalMemoryGB": 96, "cpuBrand": "...", "cpuCores": 28},
  "settings": { ... GET /settings 全量 ... }
}
```

- トークン類（api-token/lm-studio-token/rag-embedding-token）は含めない（移行先で再設定。画面に明記）。
- `machine` 固有の識別子（ホスト名等）は含めない。

### 2.2 読込（`POST /settings/import {"export": {...}}`）

1. `format`/`formatVersion`/`schemaVersion` を検査。`schemaVersion > 1`（未来の版の書出）は 422 全体拒否。
2. `settings` を leaf に展開しソート、1 leaf ずつ `deep_merge(pending, leaf)`→`_validate` で可否判定。
   可→pending に採用、不可是→`skipped[{key, reason}]`。
3. 機械依存の明示 skip（検証素通しでもこのPCで無意味なもの）:
   - `models.roots[]` の存在しないディレクトリ要素（配列全体ではなく要素単位で除去）。
   - `models.lmStudio.folder` が非 null かつ存在しないディレクトリ→skip（null 維持）。
   - `api.port` がこのPCで使用中→skip（`test_port` で判定）。
4. 採用分を1回 `settings.update`（原子書込）。応答 `{applied:[keys], skipped:[...], importedFrom:{...}}`。
   `api.host/security.allowLan` の同時変更ルール等、cross-field 制約は whole-config 検証で自然に扱われる
   （片方だけ来た leaf は invalid→skip。これが「適用できない項目はそのまま」の実装）。

### 2.3 CLI・GUI

- CLI: `config export [--file PATH]`（省略時は stdout）、`config import --file PATH`（結果の applied/skipped を表示）。
- GUI 一般ページ: 「設定を書き出す」「設定を読み込む」ボタン（fileExporter/fileImporter、`.json`）。
  読込後は適用件数・skip 件数と理由をアラート表示。トークン除外の注記つき。

## 3. 初回セットアップ（wizard）

### 3.1 fresh 検出（更新時は出さない）

- `SettingsStore._load` で `config.json` が存在しなかった場合のみ `self.fresh_install=True`
  （プロセス内 transient、永続化しない）。破損復旧・既存 config は `False`。
- `GET /setup/status` → `{required: fresh_install and not setup.completed, completed, skipped, version, freshInstall}`。
- 新規 `setup` section: `{"completed": False, "completedAt": None, "skipped": False, "version": None}`。
  `_validate`: 全 field 型検査（completed/skipped bool、completedAt None/str、version None/str）。

### 3.2 完了（`POST /setup/complete`）

- `{"skipped": true}` → 何も変えず `completed/skipped` を記録。
- `{"skipped": false, "choices": {"modelsFolder": str|null, "maxResidentModels": int, "preset": "light"|"full"}}`:
  - `modelsFolder`: LM Studio 既定（`~/.lmstudio/models`）と異なる実在ディレクトリなら `models.roots` へ追加
    （重複排除）＋scan。既定と同値/null→roots 不変。
  - `maxResidentModels` 1–8→`models.pool.maxResidentModels`。
  - `preset`→§1.3 の apply と同一経路（計算＋適用＋lastApplied）。
  - いずれも `_validate` を通る値のみ。完了後に `setup.completed=true, version=app version` を記録。
- CLI: `setup status`、`setup run [--skip] [--models-folder PATH] [--max-resident N] [--preset light|full] [--non-interactive]`。
  対話時（tty）は不足分を `input()` で質問。`--non-interactive` は省略＝既定値
  （フォルダ＝LM Studio 既定、同時使用数＝メモリ既定 §3.3、preset＝light）。
- GUI: 起動直後（`start()`→`refreshSettings()` 後）に `required` なら専用 Window を表示。
  3ステップ（フォルダ／同時使用数／プリセット）＋概要＋「スキップ」常時可。
  完了/スキップで `required` が消えるまで再表示しない（`setup.completed` 記録で保証）。

### 3.3 同時使用数の既定値（メモリ連動）

- `physGB <= 16`→1、`<= 64`→2、else→3。wizard・CLI 非対話・GUI 初期値の共通既定（単体テストで固定）。
- preset 適用では変更しない（§1.1）。

## 4. ハードウェアスキャン（`system_info.py` 新規）

- `collect()` → `{memory: {physicalBytes, availableBytes, pressureLevel}, cpu: {brand, logicalCores},
  disk: {path, totalBytes, freeBytes}, lmStudio: {defaultFolder, exists}, warnings[]}`。
- 実装: `os.sysconf(SC_PAGE_SIZE/SC_PHYS_PAGES)`、Darwin は `vm_stat`＋`sysctl kern.memorystatus_vm_pressure_level`
 （Workers/common/server.py・model_pool.py と同一コマンド、worker 非依存）、
  `shutil.disk_usage(app_support_dir())`、CPU は `sysctl machdep.cpu.brand_string`＋`os.cpu_count()`。
- 各 probe は try/except で個別失敗→`warnings`（全体は決して raise しない。設定画面を開くだけで落ちないことが契約）。
- `GET /system/hardware`、CLI `setup scan`、GUI wizard・preset 説明文の表示源。

## 5. 最新ランタイム対応・最適化（mlx-lm 0.32.0 / mlx-vlm 0.7.6 / mlx 0.32.3）

- 現状: ランタイム自動更新が PyPI stable を追従し、実機 slot は既に最新（probe 互換）。
  本版で行うのは「追従の検証＋未使用の新機能の安全な取込」。
- mlx-vlm に KV 量子化を拡張（現状 mlx-lm のみ）:
  supervisor が mlx-vlm Worker にも `MLXBAR_KV_QUANT_*` を注入（bf16時は無注入＝v2.5.1 バイト等価）。
  vlm adapter は `kv_bits/kv_group_size/quantized_kv_start=0` を渡す（0.7.6 の generate_step が対応済み）。
  画像付き生成は APC 同様に共有キャッシュを使わない既存設計のため、量子化も画像なし text のみ有効化し、
  画像付きは bf16 経路（fail-closed）。失敗時は mlx-lm と同一の「初回出力前のみ bf16 再試行」。
  prompt-cache namespace は `-kv-<mode>` 分離（mlx-lm と同一則）。
  Paged との排他（量子化優先）は mlx-lm 側の既存則を維持（Paged は mlx-lm plain KV のみのため vlm に影響なし）。
- `prefill_step_size`: 両上流の既定 2048 が現 adapter（未指定）で既に適用中。明示指定はしない
  （既定値の上書きは将来の切替余地として残す）。smoke で有効性をログ確認。
- `prompt_progress_callback`・`cached_tokens` 等の server 向け機能は当方の自前 Coordinator 構成では不要のため不採用。
- 検証: 最新 slot 上で mlx-lm（bf16/8bit 実生成＋不一致時の bf16 再試行）・mlx-vlm（bf16/8bit 実生成）の
  smoke テストを実機で行い TEST_PLAN に記録。transformers 5.x は slot 要件として解決済み（5.19.0）。

## 6. CLI・GUI 両対応の穴埋め（新機能以外）

- `model set-memory-limit <model_id> <gb>`（1–512）: pin 後変更の命名 CLI。profiles 配列の保持書換
  （GUI `setModelMemoryLimit` と同一形状）。未 pin は `set-replicas` と対称に自動 pin（keepLoaded）。
- `config set-runtime [--channel-mlx-lm stable] [--channel-mlx-vlm stable]
  [--auto-check-mlx-lm true|false] [--auto-check-mlx-vlm true|false]
  [--auto-install-missing true|false] [--check-interval-hours H] [--keep-slots N]`:
  GUI ランタイムページに設定 Section を追加（channel は stable 固定表示＋autoCheck 等の Toggle/Stepper）。
- GUI 詳細ページに診断サマリ（version/platform/python/root/常駐数。既存 `GET /diagnostics` の表示のみ）。
- 例外として残す（文書化）: クリップボード系（CLI に概念なし）、`generate --stream-events` の GUI 対応物なし、
  `launchAtLogin` の OS 実登録（CLI は desired のみ、GUI 起動時 reconcile）、`remove-all-data` の
  `SMAppService.unregister`（CLI 不可、既知制限として維持）。

## 7. UI 文言（日英）

- 新規リテラルは日本語原文キー＋`en.lproj` 英訳。候補（確定は実装時）:
  一括設定モード／軽量／フル／カスタム／適用内容の説明／再起動注意／設定を書き出す・読み込む／
  トークン除外注記／初回セットアップ／モデルフォルダ／同時使用数／スキップ／完了／
  ランタイム自動確認／診断サマリ 等、約20キー。
- SwiftUI 制約（mlx-bar.md 既知の罠）: wizard は単独 Window＋VStack/ScrollView（NavigationSplitView なし）、
  説明文は `frame(maxWidth:.infinity)`、`.fixedSize` 禁止。preset Picker は一般ページ既存 Picker と同一形式。

## 8. テスト（TEST_PLAN_v2.6.0.md に詳述）

- `Tests/test_v2_6_profiles.py`（新規）: hardware collect のモック単体、6キープロファイル計算の境界値、
  clamp が _validate 範囲内であること、effective 判定の一致/乖離、apply の update 形状、
  import の適用/skip 行列（未来 schema 拒否・存在しない roots 要素除去・使用中 port skip・
  host/allowLan 片方 skip）、setup fresh/complete/skip、既定同時使用数の RAM 境界。
- `Tests/test_gui_settings_parity.py` 拡張: preset apply payload、set-runtime payload、
  set-memory-limit payload の三者契約。
- `Tests/test_cli.py` 拡張: 新コマンドの parser＋PUT 形状（Mock client）。
- Python 回帰全緑（既知 flake 1 件の除外理由を記録）、`swift build`、実機 GUI 目視（日英 preset/wizard/export/import）、
  最新 slot 実生成 smoke（lm 2mode＋vlm 2mode）。

# DESIGN v3.3.3 — 常駐モデルの起動時 preload の診断性と回復性

## 1. 問題

起動時 preload（`Coordinator/mlxbar/main.py:_preload_last_model`）は、次の 3 経路で
「何もせず戻る」ことができ、いずれも **logger を呼ばず、ワーカーも起動しなかった**。

| 経路 | 条件 |
|---|---|
| `api_autoload_suspended` | `DELETE /api/v1/models/loaded` が立てたグローバル 1 bit |
| `no_resident_target` | 常駐プロファイルも `last_loaded_model_id` も無い |
| `model_not_in_catalog` | 常駐プロファイルの `modelId` がカタログに無い |

GUI 側も `skipped` を一律に無視していた（`preloadStatusLine` の `default: return nil`）。
結果として「常駐設定は生きているのに読み込まれない。ログもエラーも無い」状態が
どこにも現れず、launchd 固有の不具合と誤診されていた。

## 2. 設計判断

### D-1 常駐設定はゲートを解除する

`api_autoload_suspended` は「意図的に解放した」を再起動後も保つための 1 bit ゲートで、
AC-7-3（unload → 再起動で戻らない）を満たすために必要。ただしこれは**全モデル共通**
なので、「別のモデルを常駐に設定する」という逆方向の意思表示を打ち消してしまう。

判断: `keepLoaded: true` を書いた時点でゲートを解除する。設定の書き込みが成功した
**後**にのみ解除する（422 で拒否されたプロファイルがゲートを動かさないように）。

互換性: アンロードのみ、unpin のみの操作はゲートに触れないため、既存の契約は不変。
`Tests/test_v3_2_0_pin_semantics.py` の `test_all_unload_keeps_the_global_gate` /
`test_single_unload_clears_last_loaded_without_arming_the_gate` は無変更で通る。

### D-2 常駐モデルの不在は「失敗」であり「スキップ」ではない

モデルIDは `sha256(realpath)[:16]`。モデルの移動・改名で常駐設定が指す先が消える。
これは良性のスキップではなく、ユーザーの設定が機能していない状態なので
`failed` + `RESIDENT_MODEL_MISSING` として公開する。

自動復元は行わない。プロファイルがパスを保持していないため、同一性を推測すると
別モデルを読み込む危険がある。代わりに警告を出し、ユーザーが選び直せるようにする。

`last_loaded_model_id` 経由の一時モデルが消えた場合は、従来どおり `skipped`
（ユーザーが明示的に常駐させたわけではないため）。

### D-3 到達不能なルートでカタログを破壊しない

`scan_job` はカタログ全置換 + バージョンマーカー更新を 1 組で行う。未マウントの
外付けボリュームがあると、そのルートの行が 0 件になり、置換で消え、マーカーが
進むため再スキャンされない（恒久化）。

判断: **現在カタログ行を保持しているルート**が到達不能なときは、置換もマーカー更新も
行わず、警告を出して戻る。行を持たない未使用ルート（設定に残っただけのパス）は
スキャンを妨げない。これにより「未マウント → 消える」ではなく
「未マウント → 保留 → マウント後に再スキャン」になる。

### D-4 診断性

- 起動バナー（version / pid / root / cwd）。従来は起動時に何も記録せず、
  「どのビルドが、どのデータフォルダで、いつ起動したか」がログから辿れなかった。
- preload の全判断を INFO/WARNING で記録。成功時は所要時間つき。
- スキャン待ちの間は `preload.state="pending", reason="catalog_scan"` を公開し、
  「まだ何も始まっていない」のか「スキップされた」のかを区別できるようにする。
- シグナルハンドラをスキャン待ち・preload の**前**に登録。従来は後だったため、
  その窓で SIGTERM を受けると `finally` が走らずワーカーが残った。

### D-5 GUI

`preloadStatusLine` は `skipped` を無視し続ける（`no_resident_target` は語ることが
無い）。ただし `api_autoload_suspended` のときだけは説明行を出す。常駐設定が
生きているのに読み込まれない、という最も混乱する状態だから。
`RESIDENT_MODEL_MISSING` は失敗としてオレンジの警告行になる。

### D-6 読み取りは必ず時間で打ち切る

原因1（TCC が `open()` を保持する）は、コードだけでは解決できない。許可は
ユーザーが与えるものだからだ。したがって設計上の責任は「**止まったまま何も
言わない状態を作らない**」ことに置く。

`Coordinator/mlxbar/io_guard.py` の `VolumeGuard` が、モデルのメタデータ読み取りを
20秒で打ち切り `MLXBarError(MODEL_VOLUME_UNREADABLE, status=503)` にする。
`asyncio.to_thread` のスレッドはキャンセルできないので、一度タイムアウトしたら
60秒のクールダウン中は即座に失敗し、スレッドを増やさない。

エラーメッセージは「リムーバブルボリューム」または「フルディスクアクセス」で
MLXBar を許可するよう、日英で具体的に案内する。GUI は `preload.state="failed"`
としてオレンジの警告行に出す。

カタログスキャンの到達不能判定も同じ理由で 15 秒に制限する（`state.py`）。

## 3. 変更点

| ファイル | 変更 |
|---|---|
| `Coordinator/mlxbar/api/management.py` | `_rearm_resident_autoload` / `_profiles_have_resident` を追加。プロファイル PUT と設定 PUT から呼ぶ |
| `Coordinator/mlxbar/main.py` | `_preload_last_model` の全面ログ化・失敗分類・所要時間。起動バナー。シグナル登録の前倒し。スキャン待ちの状態公開。`__version__` の import |
| `Coordinator/mlxbar/io_guard.py` | 新規。`VolumeGuard`（上限時間つき読み取り・停止記憶） |
| `Coordinator/mlxbar/workers/supervisor.py` | 3 つのメタデータ読み取りを `volume_guard.read` へ |
| `Coordinator/mlxbar/state.py` | `_roots_that_would_be_lost` と `scan_job` のガード（15秒上限） |
| `Sources/MLXBar/Services/ErrorText.swift` | `RESIDENT_MODEL_MISSING`（日英） |
| `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` | `preloadStatusLine` に停止ゲートの説明行 |
| `Sources/MLXBar/Resources/en.lproj/Localizable.strings` | 対応する英語文 |
| `Tests/test_v3_3_3_resident_preload.py` | 22 テスト |

## 4. リスクと対策

| リスク | 対策 |
|---|---|
| ゲート解除で「アンロードしたのに勝手に戻る」 | 解除するのは**常駐設定を書いたときだけ**。アンロード単独では解除しない（テストで固定） |
| スキャン保留でカタログが永久に古くなる | 保留は「行を持つルートが到達不能」な場合のみ。ルートを設定から外せば通常スキャンに戻る。マーカーを進めないので次回起動で再試行される |
| 常駐IDが無効なだけで起動が重くなる | 追加の I/O は無し。カタログ参照は既存の `list_models()` のみ |
| 起動バナーがログを汚す | 1 行のみ |
| 新コードの日英欠落 | `check_i18n.py` と `Tests/test_v3_3_3_...::test_the_new_code_has_bilingual_text` |

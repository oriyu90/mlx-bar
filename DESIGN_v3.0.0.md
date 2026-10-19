# DESIGN v3.0.0 — v2.10.0設計レビュー（7観点精査）に基づく安定版修正

- 版数: app 3.0.0 / build 56。Coordinator `__version__` 3.0.0。`schemaVersion` は 1 のまま（変更なし）。`CATALOG_CLASSIFIER_VERSION` は "3" のまま（判定規則不変）。
- 方針: v2.10.0の設計を **メモリ安全性／クラッシュ安全性／動作安定性／GUI完全性／設定整合性／互換性／表示正確性** の7観点で精査し、発見された問題のみ修正する。既存API形状・既定値・設定スキーマ・判定規則は不変。明示選択最優先・安全設計・日英完全対応UIの原則はv2.10.0のまま。

---

## 1. 精査で見つかった問題と修正設計

### 1.1 明示エンジン選択が ready スロットで握りつぶされる（GUI完全性・最重要）

**問題**: `_load_replica` の2箇所のfast-pathは「当該モデルのreadyスロットがあればそのまま返す」のみで、要求された明示エンジンを見ていなかった。既にロード済みのモデルに対して「mlx-vlm で読み込む」を選ぶと旧エンジン（例: mlx-lm）のload結果がそのまま返り、ユーザーの選択が無反応になる。

**設計**: `ModelPoolSupervisor.load` の冒頭（レプリカループ前）に切り替え段を追加:

```
if explicit and engine in {"mlx-lm","mlx-vlm"}:
    _switch_ready_slots_to_explicit_engine(model_id, engine)
```

- `_switch_ready_slots_to_explicit_engine`: 当該モデルのreadyスロットを走査し、
  - `slot.worker.loaded.engine != 要求エンジン` のスロットは **退避**（`_evict_slot` ワーカーunload+shutdownで実メモリ解放）、その後は通常のレプリカループで再ロード
  - `slot.leases > 0`（生成中）は `ENGINE_BUSY` 409（retryable）で拒否 — 生成を壊さない
  - 一致するスロットは触らない（同一エンジン明示はno-op）
- **自動ロードのsemanticsは不変**: auto（engine None/auto）は従来どおりreadyスロット優先。再導出済みスロット（例: mlx-vlm自己修復済み）がカタログ行と違っても再ロードしない（無churn）。
- 既知の制限: in-flight（load_future進行中）スロットは実際エンジン未確定のため対象外（完了後の再選択で切替が効く）。コードコメントに記載。
- テスト: ready スロット用の明示切替4件（`Tests/test_v2_10_1_fixes.py::ExplicitEngineSwitchTests`）。

### 1.2 フォールバック中の表示文言（表示正確性）

**問題**: supervisorのフォールバック表示が「mlx-vlmへ切り替え中」で固定。v2.10.0で対称化された mlx-vlm→mlx-lm への切替時もmlx-vlmと表示してしまう。

**設計**: `supervisor.load` のcandidateループで `f"{chosen}へ切り替え中"` に動的化（`chosen`は今まさに試す候補）。テストで両方向の文言を固定。

### 1.3 未判別（engine=None）モデルの自動ロード拒否（動作安定性）

**問題**: カタログ行の engine が None（判別不能・不完全ディレクトリ等）だと auto ロードは一律409「利用可能なエンジンがありません」。`detect_engine` が読めればロード可能な機会が捨てられていた。

**設計**: `supervisor.load` で **導出をゲート（`chosen not in {mlx-lm, mlx-vlm}`拒否）の前に移動**:

```
if not explicit and model.get("path") and chosen in {"mlx-lm","mlx-vlm",None}:
    detected = detect_engine(Path(model["path"]))
    if detected and detected != chosen: chosen = detected
```

- config が読めれば誘導（通常テキスト→mlx-lm）、読めなければ選択せずゲートが従来どおり409を返す（誠実なエラー表示）
- 明示選択（explicit=True）は dir を consult しない（v2.10.0の決定を維持）
- `chosen` が mlx-lm/mlx-vlm の場合の再導出は従来どおり（行が正しければ変わらない）

### 1.4 カタログ自己修復のイベントループ停止リスク（動作安定性）

**問題**: `_load_replica` 成功後の `_refresh_catalog_classification` がイベントループ上で同期実行で、(a) config.json 等のFS読み取り、(b) `Database.update_model_classification`（threading.Lock下のsqlite書込み）。スキャン（replace_models）がエグゼキュータスレッドで同一ロックを長時間保持している場合、ロード処理がポーリング/ハートビートを含むイベントループ全体を停止させてしまう。

**設計**: 呼び出しサイトを `await asyncio.to_thread(self._refresh_catalog_classification, model, result)` に。**メソッド自体は同期のまま**（`PoolSelfHealTests` のspy型・`test_self_heal_never_surfaces_failure` 等の既存単体を無傷で通す）。例外は発生源のメソッド内で吸収（ロードは既に成功しているため失敗を漏らさない設計を維持）。

テスト: 実行スレッドが MainThread でないことを実測（`SelfHealThreadTests::test_self_heal_runs_off_the_event_loop`）＋失敗非漏出。

### 1.5 ジョブ配管の例外で子プロセスが残る（クラッシュ安全性）

**問題**: `omlx_setup._run_command` と `runtimes/updater._command` は `except asyncio.CancelledError` のみterminateを伴う。`update()`/`heartbeat()` の書込みが_Cancelled以外の例外（DB異常等）を投げると、spawn済みの子プロセス（brew install等・最大1時間）がterminateされず放置されうる。

**設計**: 両コマンドランナーに同一モデルの追加ガード:

```python
except BaseException:
    await self._terminate(proc, communication)
    raise
```

timeout（CancelledError→MLXBarError変換）とユーザーキャンセルの既存経路は不変。BaseException分岐はタイムアウト変換をしない（内部原因をそのまま表面化、ジョブ層でfailedへ）。

テスト: 実子プロセス `/bin/sleep 300` で heartbeat がraise → returncode設定済みを実測（`CommandRunnerTearDownTests` 3件、timeout経路は `OMLX_COMMAND_TIMEOUT` も保持）。

### 1.6 oMLX autoConnectの再試行契約（動作安定性）

**問題**: main.py `_omlx_auto_connect_task` の `except Exception → return` が、connect_omlxが「エラーをstatusに折り込まない形」でraiseしたとき（現実にはfolding前提だが）、docstring/設計の「20秒×6回」契約を1回で破った。

**設計**:

- `except Exception → continue`（1試行として消化し、次インターバルで再試行）。`CancelledError` の再raiseは不変（shutdown cancelの伝搬用）。
- 成功時は即終了のまま（成功後にsleepしない）。
- `max_attempts=6, interval=20.0` をkwargs化（`serve()` の既知呼出しは引数なしのまま）。単体テストは小intervalで契約を直測。

テスト: raise 3回=3試行 / 成功1回で1試行 / 未到達4回=4試行 / 無効時はタスクなし（`AutoConnectRetryTests` 4件）。

### 1.7 実行中 oMLX 有効化時の常駐メモリ計上が欠落（メモリ安全性）

**問題**: `ModelPoolSupervisor.__init__` で `models.omlx.enabled` が true のときだけ `start_status_updates()` を着手。実行中にGUI/CLIから enabled をONにした場合、背景のキャッシュ更新（5秒周期）が開始されず、`_get_omlx_memory_usage_cached()`（`_resident_charge()` のoMLX分）が恒0となり、メモリ予算が実態より緩く解釈される（ネイティブモデルの追加ロード猶予が過大）。

**設計**: `ModelPoolSupervisor._ensure_omlx_background()` を新設:

- `_omlx_enabled()`（実行時のsettings参照）が false なら何もしない
- true なら `provider.start_status_updates()`（provider内は冪等：既存タスクのdone/None以外は即return）
- 呼び出しは `_get_omlx_memory_usage_cached()`（ resident charge が参照）とoMLXロード経路。失敗はログのみ（ブックキーピングで load を巻き込まない）

テスト: `enabled 実行中ON → タスク開始、再呼出しで同一タスク / disabledでは開始しない / cached経由でも開始` （`EnsureOmlxBackgroundTests` 3件）。

### 1.8 表示・shape の細部（表示正確性／互換性）

- `omlx_adapter._update_cached_status` 失敗時のキャッシュ上書きが `serverUrl` を欠くcanonical shape破壊 → 直前値の `serverUrl` を保持（GUIの接続先表示が1周期空白になるのを防止）
- 英語UIのoMLXジョブカードの未翻訳メッセージ（待機中/開始しています/完了/失敗/利用者が処理を中止しました）をexact一致翻訳mapへ追加（カードは既存の `job.isFailed → errorMessage` 直参照戦略を維持 = ランタイムジョブカードと同一挙動）
- Swift: 未使用だった `monitoredOMLXSetupJobIDs` を除去（誤読防止）

### 1.9 確認して問題なしとした領域（修正外）

- **設定内容の整合性**: GUIの単一キーPATCH（`{models:{omlx:{modelRouting:{id:…}}}}`）はsettings.py `update()` が `deep_merge` で他キー・他routingを破壊しない。セットアップジョブは設定を書かない（トークン・URLは設定側管理のまま）
- **jobs.cancel 頑健化**: 二重cancel/実行後cancel/未起動キャンセルのterminal書込一貫性を確認（v2.10.0の実装のまま）
- **API互換**: `/omlx/status` canonical payload＋旧キーデコード、jobs SSE、CLI `omlx install|start|stop [--wait]`、`allow_vlm` ワーカーRPC（mlx_vlmは無視・mlx-lmはガード閾値）を確認
- **FakeWorker署名のテスト側更新**: v2.10.0で更新済みの6テストファイルのまま再確認

---

## 2. 変更ファイル（v3.0.0差分）

| ファイル | 変更 |
|---|---|
| `Coordinator/mlxbar/workers/model_pool.py` | `_ensure_omlx_background()`、`_switch_ready_slots_to_explicit_engine()`、loadでの呼出、`_refresh_catalog_classification` の `to_thread` 呼出、omlxロード経路の `_ensure_omlx_background` |
| `Coordinator/mlxbar/workers/supervisor.py` | 導出のゲート前移動＋None導出、fallback表示の動的文言 |
| `Coordinator/mlxbar/omlx_setup.py` | `_run_command` に `except BaseException` terminate |
| `Coordinator/mlxbar/runtimes/updater.py` | 同上 |
| `Coordinator/mlxbar/main.py` | `_omlx_auto_connect_task(state, *, max_attempts=6, interval=20.0)`、exceptionでcontinue |
| `Coordinator/mlxbar/workers/omlx_adapter.py` | キャッシュ失敗時のshape維持 |
| `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` | デッドコード除去 |
| `Sources/MLXBar/Runtimes/RuntimeManagerView.swift` | oMLXカードメッセージ翻訳map追加（待機中/開始しています/完了/失敗/キャンセル） |
| `Tests/test_v2_10_1_fixes.py` | 新設21件（上記各設計の回帰固定） |
| 版数系 | `Packaging/Info.plist`（3.0.0/56）、`__init__.py`、`pyproject.toml`、`build-release.sh`、`verify-release.sh` フォールバック、README、CHANGELOG |

---

## 3. 既知の残課題（この版では対応しない）

- in-flight ロード中の明示エンジン指定は fast-path（結果保証なし）のまま。完了後の再選択で切替が効く。次の機能版で「future完了後に切替」の単一flight化を検討
- （維持）`explicit` 系のLM Studio拒否、`omlx` 明示engineのMODEL_INCOMPATIBLE文言はv2.10.0設計のまま
- （維持）v2.9.0由来の `en.lproj` 重複キーは今回変更範囲外

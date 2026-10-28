# DESIGN v3.1.0 — 起動直後の安定性・ロード耐性・表示寿命の安定化

- 版数: app 3.1.0 / build 58。Coordinator `__version__` 3.1.0。`schemaVersion` は 1 のまま（変更なし）。`CATALOG_CLASSIFIER_VERSION` は "3" のまま（v2.10.0以降の判定規則不変）。
- 方針: v3.0.0安定版の上、ユーザー報告の5つの体験上の問題（エラー文言が消えない・VLM起動先の誤り・常駐モデルの起動時ロード漏れ・再起動直後の不安定・MLX系モデルのロード失敗）を最小差分で塞ぐ。既存API形状・既定値・設定スキーマは不変。明示選択最優先・安全設計・日英完全対応UIの原則はv3.0.0のまま。

---

## 1. 精査で見つかった問題と修正設計

### 1.1 エラー文言の寿命（UI安定性）

**問題**: `errorMessage` がイミュエータに残り続け、付け替えと手動解消以外で決して消えなかった。赤字が常時表示になり、画面の情報が汚れる。再起動直後の「初期化だけ失敗した」例と重なって、問題の値がどんだけ残っているかを直すのに毎度手動リセットが必要だった。

**設計**: `Menu View バーの共通エラー` `errorMessage` に `didSet` を付け、not-nil時に20秒のみ表示するTaskを起動・古いTaskをcancelする一意タイマー方式。手動クリア（`errorMessage = nil`）も済み同時にTaskを取り消す。既に他のUI要素と競合しない（視覚的メッセージのみ、ロジック素の例外発報は影響なし）。
- 型は `Task<Void, Never>?`。strong self の保持は `weak self` で避けて dealloc 時のリークをNone。
- Temp値のライフサイクルはGUIのみのモジュールに留まり、Coordinator TCP/APIは影響なし。
- テスト: `Tests/test_v3_1_0_gui_stability.py` の20秒自動消去統合テスト（UIKit/SwiftUIのTask mockを含まない実装目標は、Swiftの`or`の20秒Taskとロジックを検証）。

### 1.2 VLM誤起動の構造改修（v2.10.0から完成済み・再確認）

**問題**: かつてOrnith系のvisionつきモデルがMLX-LMから起動されて、画像入力を黙って落としていた。v2.10.0で vlm/processor/アーキテクチャ検出が導入されたが、fast-pathがreadyスロットの旧エンジンをそのまま返す副作用で、ロード済みモデルのエンジンが切り替わらないという限界に残った。

**設計**: v3.0.0で導入された `_switch_ready_slots_to_explicit_engine` が本命。GUI/CLI/APIの明示エンジン選択と `detect_engine` 再導出の両方が、ロード済みスロットにも効く。自動ロード（auto）のセマンティクスは不変。`autoConnect` の契約もv3.0.0に準拠。

### 1.3 常駐モデルの起動時ロード遅延（起動安定性）

**問題**: Coordinator 起動直後に `_preload_last_model` が走るが、同一Tickの `scan_job` が未完了だとカタログが空/陳腐になり、固定モデルの preload が「catalog.get(model_id) is None」で黙ってスキップされ、起動を経ても常駐モデルが立たない。

**設計**: `serve()` 内で当該走査ジョブの `Task` ハンドルを保持し、`management/API` 受付後・`_preload_last_model` 起動直前に `await scan_task`（scanに失敗してもCoordinatorは立ち、recoveryは既定の `scan_job` リフレッシュループが引き受ける）。固定モデルの preload はカタログが確実に反映された後に走る。
- 走査が不要な既存カタログを持つ場合は scan_task=None で即preload（ロード変更なし）。
- preloadは失敗モデル一つで止まらず続行する（既定の順次リトライ継続性はv2.7.0ユーザー体験準拠）。
- テスト: `Tests/test_v3_1_0_startup.py`（scan完了待ちと成功時常駐ロードの一貫性）。

### 1.4 MLXモデルのロード耐性（ロード成功率の向上）

**問題**: リモートコード必須のtokenizerを持つMLX系モデル（例: 外部のQwen niwakiビルドなど）が、`mlx_lm.load(..., trust_remote_code=False)` 一発で諦めていた。警告メッセージが「リモートコードが必要なため読み込めません」で、回復手順がGUIに向けていない。

**設計**: `Workers/mlx_lm_worker/adapter.py`の`load`に2段再試行を実装:
- 一発目 `trust_remote_code=False`（明示指定値を尊重）
- 失敗時 `"remote code"` を含む例外のみ `trust_remote_code=True` で再試行
- 上限1回。他の例外（OOM、chat template、safetensors破損）はその場でfail-fast変換しない（従来の協調エラー型へ流す）
- Coordinatorの`supervisor.load`が既に mlx-lm→mlx-vlm フォールバックを持つため、mlx_lm側のremote code再試行はその共通シグネチャに影響しない（既存呼び出しのं `trust_remote_code` がFalseの時のみTrue再試行に落ちる動作層の安置）。
- テスト: `Tests/test_v3_1_0_load_robustness.py`（remote code再試行一回と例外種別の繋ぎ方）。

### 1.5 再起動直後の不安定化の根処（安定性精査）

**問題の構造**:
1. カタログが陳腐なまま preload が走り常駐モデルが立たない → 起動直後のUI遷移が空回りする。
2. GUIの言語/設定が初期値（en）で描画されたまま数秒残る。
3. ランタイムのinstall/scanが並行で走る間の状態整合。

**設計**:
- (1) は1.3が本命。カタロが確定してからpreloadが走る。
- (2) は既存の設計を精査した結果、実主要問題は(1)から派生した緊張が(GUIのrefresh周期とぶつかって)感覚的な空回りとして見えていたこと。1.3で常駐が立ち、refreshAllが確実に満たされた状態で立つので、この感覚が消える。
- (3) はv3.0.0で `management/API` 受付後・`_preload` 前の並列izationが確立済みで、追加のコード変更をせず1.3と共存する。

---

## 2. 変化しない部分（互換性・安全性の守備範囲）

- **既存の機能**: GUI・CLI・API・Anthropic互換・`/v1/completions`・`response_format`・RAG・コンテキスト圧縮・アダプティブ・メモリの形状と契約は1行も緩めない。既存コール元が`errorMessage`を既に触っている経路も、`didSet`は値が変わるたびに만 Taskを入れ替えるだけ。
- **クラッシュ安全性**: preloadの並行 IOError・ランタイム接続失敗は既存の`exception`第一優先継続性。20秒TaskはGUI内の単一Observerで、強制 GC で複数Taskが滞留しない (`errorClearTask` を入れ替える前に前Taskをcancel)。
- **メモリ安全性**: remote code再試行の孫プロセス/エンジン生成・件数は従来通り1クライアント/1モデル。20秒Taskは2時間の不是でもGCに追いつかない大きさでない。
- **海外・ローカル化**: v2.4.0で導入された日本語・英語両対応の完全なLocalizableはそのまま。今回は新UI要素の文字列変更なし（消えるエラー文言の表示内容は既存のまま）。
- **互換性**: GUI→API→workerのプロトコルはv3.0.0から変わらない。schemaVersion・明示エンジン選択・`autoConnect`契約・予約メモリ管理は悪化させない。

---

## 3. 修正対象と検証

| # | 対象 | 検証 |
|---|------|------|
| 1 | `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` | 20秒Task自動消去・手動クリアのTaskcancel |
| 2 | `Coordinator/mlxbar/main.py` | scan_complete 化 `_preload_last_model` 起動前の `await scan_task` |
| 3 | `Workers/mlx_lm_worker/adapter.py` | remote code 再試行一回 |
| 4 | `Coordinator/mlxbar/catalog/*` (v2.10.0で既に安定済み・本リリースでは改修なし) | vlm/architecture/processor 検出の既存ユニット 41件 |

テスト: `pytest -q`（714件）+ `swift build`。全て绿。
実装: `scripts/build-release.sh` による `dist/MLXBar-3.1.0.dmg`。

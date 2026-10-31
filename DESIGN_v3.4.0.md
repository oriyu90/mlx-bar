# DESIGN v3.4.0 — API の診断性（401 とボリューム停止）

## 1. 入力

開発者向けレポート `mlxbar-auth-401-report-2026-10-31.md`（OpenClaw 運用者）。
Issue A はクライアント側の鍵キャッシュ不具合（修正済み）だが、**サーバーの応答では
原因を切り分けられなかった**。Issue B は 20 秒の読み取り停止が 60 秒の全面 503 になり、
クライアントからは認証エラーと区別できなかった。

## 2. 採用した方針

**診断性を上げる。認可は緩めない。**

レポートの提案のうち、`/v1/models` の無認証化とローテーション猶予は
「切り分けを楽にする」ための手段だが、どちらも既存の契約を弱める。

| 提案 | 判断 | 理由 |
|---|---|---|
| 401 に鍵の指紋を付ける | **採用** | 秘密を出さずに「鍵が古い」を判定できる。最も価値が高い |
| 無認証 `GET /v1/auth/status` | **採用** | サーバーが期待する鍵を、秘密なしで照会できる |
| 無認証 `/v1/models` | 見送り | モデル一覧は認証で守る対象。e2e が契約として固定している。代替（`/health` + `/v1/auth/status`）で同じ切り分けが可能 |
| 旧鍵を5分猶予 | 見送り | ローテーションは失効操作。漏洩時の意味が薄れる。旧鍵は即時無効を維持 |
| エラー語彙を1つに | **採用** | `code` は常に `AUTHENTICATION_FAILED`。`message` は日本語のまま（GUI が表示する） |
| 停止時に一覧を stale で返す | 部分採用 | `/v1/models` の形（OpenAI 互換の配列）は変えない。状態は `/health` の `volume_state` で公開 |
| コードを3分割 | 部分採用 | 権限待ちと低速はサーバー側から区別不能なので分けない。**切断**（パス不在）だけは確実に判定できるため `MODEL_VOLUME_DETACHED` として分離 |
| `retry_after_ms` | **採用** | 「恒久的な設定ミス」ではなく「待てば直る」と伝える |
| 起動時に1回読んでキャッシュ | **採用** | リクエスト経路から読み取りを消す |
| 無認証 `GET /v1/health` | **採用（拡張）** | 既に無認証。状態のみでモデル名・パスは出さない |

## 3. 設計

### D-1 指紋（`authtoken.fingerprint`）

`sha256(token)` の先頭 8 hex。クライアントは自分の鍵から同じ値を計算して比較する。
トークン空間は 32 バイト乱数なので、8 hex から鍵を復元することはできない。
`token_issued_at` はトークンファイルの mtime。ローテーションの検知に使える。

**401 に提示された鍵は返さない**（照合結果を漏らさないため。等しいかどうかは
クライアント側で比較できる）。

### D-2 認証の単一情報源

`authtoken.accept()` を、ASGI ラッパー（`main.py:_authorized`）と
FastAPI ルート（`authorize()`）の両方が使う。以前は同じ比較が2箇所に重複していた。

### D-3 ボリュームエラーの分類

```
読み取りタイムアウト
  └─ 2秒の存在確認（これも上限つき）
       ├─ パスが無い        → MODEL_VOLUME_DETACHED   （接続されていない）
       ├─ パスはある        → MODEL_VOLUME_UNREADABLE （応答しない／権限待ち）
       └─ 確認も応答しない  → MODEL_VOLUME_UNREADABLE
```

どちらも `retryable=True`、`retry_after_ms` はクールダウン（60秒）。
権限待ちと低速は区別できないため、メッセージで両方に触れる。

### D-4 メタデータキャッシュ

`WorkerSupervisor._cached_metadata(kind, path, func, ...)` が
`{kind}\0{path}` をキーにプロセス内で1回だけ読む。トークン上限と能力フラグは
モデルの内容から決まり、プロセスの生存中に変わらない。
読み取りは引き続き `VolumeGuard` を通る（停止検出と上限は維持）。

### D-5 `/health` は I/O しない

`volume_state` は `VolumeGuard` が記憶している停止フラグを返すだけで、
存在確認やディレクトリ走査は行わない。停止したボリュームで `/health` が
固まると、監視の意味がなくなるため。uptime は `AppState.started_at`（wall clock）。

## 4. 変更点

| ファイル | 変更 |
|---|---|
| `Coordinator/mlxbar/authtoken.py` | 新規。指紋・発行時刻・照合・401 本文・auth status 本文 |
| `Coordinator/mlxbar/errors.py` | `retry_after_ms`（任意）を payload に追加 |
| `Coordinator/mlxbar/io_guard.py` | 停止時に再試行可能・`retry_after_ms`・`MODEL_VOLUME_DETACHED` 判定 |
| `Coordinator/mlxbar/workers/supervisor.py` | `_cached_metadata`（1モデル1回の読み取り） |
| `Coordinator/mlxbar/api/openai_compat.py` | `authorize` を指紋つきに、`/v1/auth/status` 追加、`/health` 拡張 |
| `Coordinator/mlxbar/main.py` | 許可リストに `/v1/auth/status`、403 本文に診断フィールド、エラー envelope に追加フィールドを通す |
| `Sources/MLXBar/Services/ErrorText.swift` | `MODEL_VOLUME_DETACHED`（日英） |
| `Tests/test_v3_4_0_api_diagnostics.py` | 16テスト |

## 5. リスク

| リスク | 対策 |
|---|---|
| 指紋から鍵が漏れる | SHA-256 の先頭 8 hex のみ。原像計算は不可能。テストで「トークン文字列が応答に含まれない」ことを固定 |
| 無認証エンドポイントの情報漏洩 | 状態のみ（モデル名・パスなし）。テストで固定 |
| ローテーション猶予の実装漏れで旧鍵が通る | 即時失効をテストで固定（e2e も同契約） |
| キャッシュが古いモデル情報を返す | キーはパス。モデル内容はプロセス生存中に変わらない前提。`unload`→再 `load` でも同じ値 |
| `retry_after_ms` 追加でクライアントが誤解 | 未設定時は payload に含めない（既存の形を維持） |

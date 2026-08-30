# MLXBar v1.8.2 テスト計画と結果

対象: API経由で2体目以降のモデルへ切り替えられない不具合の修正
（`Coordinator/mlxbar/api/openai_compat.py::_ensure_requested_model`）。

v1.8.1以前の全契約は回帰として維持する。管理API・Anthropic互換APIの各ハンドラは無変更。
設定schema version 1は不変。

## 1. 自動検証

```sh
cd Coordinator && uv run pytest ../Tests -q
```

2026-08-30 結果:

- Python: **361 passed**（v1.8.1の360 + v1.8.2の1）
- `sh scripts/build-release.sh` → `sh scripts/verify-release.sh`: 実行して確認（本リリース手順の一部）

## 2. 本不具合の新規契約

| 契約 | 検証 |
|---|---|
| 常駐0体から`model=A`を要求すると`A`がロードされ応答する | `test_openai_tools::test_a_second_distinct_model_actually_loads_when_the_pool_allows_it` |
| 常駐1体（A）の状態で、カタログに実在する別モデル`B`を要求すると、`B`が実際にロードされ応答する（`A`のまま応答し続けない） | 同上 |
| 上記2回のAPI呼び出し後、プールのスロットが`{A, B}`の2体になっている | 同上（`pool._slots`のkey集合を検証） |
| 修正前のコードでは上記テストが失敗することを確認済み（`residents[0]`のエイリアス早期returnにより`B`要求が`A`の応答になる） | 手動確認: 該当ファイルを修正前バージョンへ一時的に戻してテスト実行 → `AssertionError: assert 'model-a' == 'model-b'` |

### 回帰（既存契約が壊れていないことの確認）

| 契約 | 検証 |
|---|---|
| `maxResidentModels<=1`のプールでは、一致しないモデル名は従来通り常駐モデルのエイリアスとして扱われる | 既存スイート全体のPASSで間接確認（本修正のガードが`max_resident <= 1`のときは早期returnを素通しするため、既存テストのいずれかがこの経路に依存していれば失敗するはずだが361件全PASS） |
| 同一モデルの並列ストリーミング（v1.8.0機能）は不変 | `test_openai_tools::test_real_pool_behind_openai_api_streams_two_models_concurrently` PASS |
| CLI・Anthropic互換（v1.8.1機能）は不変 | `test_cli.py` / `test_anthropic_compat.py` 全PASS |

## 3. 配布検証（リリース前）

- `sh scripts/build-release.sh` → `sh scripts/verify-release.sh`。同梱Coordinatorが`version 1.8.2`、
  `GET /v1/models`形不変。
- `CFBundleShortVersionString = 1.8.2` / `CFBundleVersion = 30`。
- Apple公証: 未実施（ad-hoc署名。`mlx-bar.md`参照）。

## 4. 実機確認（要Apple Silicon・要モデル本体）

- 実際に2つの異なるモデル（例: `Qwen3.8-27B-MLX-6bit`と別のモデル）を、常駐0体の状態から
  順番にAPIリクエストし、2体目が正しくロードされ、それぞれ正しいモデル名で応答することを確認する。
  OpenClaw実接続での再現手順に基づく（2026-08-30、本Design文書§2参照）。
- v1.8.0の`TEST_PLAN_v1.8.0.md`§2/§3（同一モデル並列のメモリトレース）は本修正では変更していない
  経路のため、影響なし。

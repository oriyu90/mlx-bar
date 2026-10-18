# TEST PLAN v2.8.1 — 生成レーン整合性 / イベントループ / GUI・CLI パリティ

対象: app 2.8.1 / build 54。既定値不変・スキーマ変更なし・API 形状不変。

## 1. 自動回帰（実施済み）

- `PYTHONPATH=Coordinator Coordinator/.venv/bin/python -m pytest Tests/ -q`
  - 結果: **654 passed**（fluke なし）。
  - 新規 `Tests/test_v2_8_1_lane_integrity.py`（3件）:
    - `test_inflight_acquisition_aborts_after_lane_recovery` — v2.8.0 ストレスで再現した
      取り残しレーンの replay。headroom probe 内で parked 中の取得タスクが、orphan 回復に
      よってレーンを解放された後に commit しないこと（permit 二重返却なし・counter 不変・
      marker 消費）と、回復後にプールがそのまま使えることを検証。
    - `test_abandoned_parent_neutralises_a_late_acquire_task` — finalizer 順序
      （marker → `_release_lane` → cancel）でも parked タスクが commit しない。
    - `test_lane_recovery_requires_a_persisted_orphan_state` — 単発の orphan 観測では回復せず、
      生存確認で pending 判定がクリアされ、2 回連続観測（0.5 秒幅）で回復する。
  - 既存テスト拡張:
    - `test_model_pool.py` — slot 生成が `_admit` 後になった v2.8.0 挙動への追従と、
      orphan 回復の二段観測（1 回目 False → 0.5 秒経過後 True）。
    - `test_v2_6_profiles.py::test_lan_pair_round_trips_together` — LAN エクスポートが
      LAN のまま再 import される（ペアクラスタ修正）。
    - `test_v2_8_generation_abort.py::test_rayon_thread_pool_is_bounded_for_mlx_vlm` —
      mlx-vlm adapter と common server の両方が `RAYON_NUM_THREADS` /
      `TOKENIZERS_PARALLELISM` を setdefault していることを AST で検証。
- `plutil -lint Packaging/Info.plist`: OK（2.8.1 / build 54）。
- Coordinator `__version__` = pyproject = app = 2.8.1 の一致を確認（v2.8.0 の health 表示
  疏漏の再発防止）。

## 2. 実機スモーク（実施済み、2026-10-18、M1 Max 64GB）

- Coordinator 2.8.1 を実ホームで起動（起動前 state.sqlite3 を backup）。
- `mlxbarctl diagnostics` → `coordinatorVersion: 2.8.1`。
- 同一モデルへ 8 並列 generate × 反復 → `status` の `activeLanes` が負荷後に常時 0、
  `_gen_slots` 残数が初期値に復帰（v2.8.0 では反復中に 1 本ずつ枯渇しうる case）。
- 生成中に GUI（メニューバー）から unload → `ENGINE_BUSY` (503) が表示され、
  生成完了後の unload は成功。レーン回復後に追加 generate が正常。
- `mlxbarctl config set-lan false` → `api.requireToken` が変更されないことを確認
  （true からの無効化では host のみ 127.0.0.1 へ）。`set-lan true` では requireToken=true が強制。
- `mlxbarctl config apply-preset custom` → `presets.mode: custom` が記録される。
- 設定 export（LAN 構成）→ 別ホームへ import → `api.host: 0.0.0.0` + `allowLan: true` が
  ともに適用（applied に両 key）。
- `/api/v1/system/hardware` 2 連打 → 応答が数百 ms 以内で安定（メインループ詰まりなし）。
- mlx-vlm モデルで長 prompt tokenise → プロセスの thread 数がコア数に比例して増えない。
## 3. 残作業（次版への引き継ぎ）
1. 実画面目視の日英確認（文言追加は error 3 code のみ。`JOB_WAIT_TIMEOUT` は CLI のみで
   発生するため GUI では通常非表示）。
2. 長期ストレス（24h）での `_gen_active_lanes` / `gen_recoveries` の追跡（v2.8.0 から継続）。
3. `runtime autoCheck` ループの実効確認（interval 詰めでの metadata 更新、autoCheck 既定 off のため通常環境では発火しない）。

## 4. 公開検証（実施済み、2026-10-18）

- `sh scripts/build-release.sh` → `dist/MLXBar-2.8.1.dmg`、`hdiutil verify` VALID、
  app 2.8.1 / build 54。
- `VERSION=2.8.1 sh scripts/verify-release.sh` → 署名・plist・DMG すべて通過（ad-hoc）。
- `gh release create v2.8.1` → DMG + `.sha256` 添付。
- studio-rizi プロジェクトページ更新 → `npm run build`・`npm test` 通過後に push、
  公開 HTTP 200 + `softwareVersion: 2.8.1` を確認。

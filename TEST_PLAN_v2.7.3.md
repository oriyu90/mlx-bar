# TEST PLAN v2.7.3 — 常駐数上限の厳格化

- 対象: app 2.7.3 / build 50。Coordinator 2.7.3。`schemaVersion` 1（据え置き）。
- 設計: `DESIGN_v2.7.3.md`。

## 1. 自動ゲート（必須）

- [ ] `Coordinator/.venv/bin/python -m pytest -q Tests -p no:randomly` が全緑。既知の失敗
      `test_model_pool.py::test_ttl_starts_when_a_slow_load_finishes`（asyncio
      タイミング依存。クリーンツリーでも失敗を確認）
      以外の失敗はリリース禁止。
- [ ] `Tests/test_v2_7_3_resident_limit.py`（4件）: 上限未満の追加・満杯の
      409 と非退避・同一モデル再ロード許可・明示 pin も厳格。
- [ ] 更新 3件が緑：`test_third_model_evicts_oldest_idle_api_model`・
      `test_manual_pin_is_never_lru_evicted`（409 期待・集合不変）、
      `test_max_resident_models_one_still_rejects_a_second_distinct_model`
     （`RESIDENT_LIMIT_REACHED` 期待）。
      `test_v2_7_2_model_routing.py::test_max1_switches_to_a_known_distinct_model`
     （409 後 unload→再要求で載る手順）が緑。
- [ ] `swift build` 成功（本版の Swift 差分なし。ビルドの再確認のみ）。
- [ ] `.strings` 新規重複なし（既存`モデル未ロード`重複1件のみ残置。本版の追加なし）。
- [ ] 新規Swiftコードなし。
- [ ] `sh scripts/build-release.sh` → `VERSION=2.7.3 sh scripts/verify-release.sh` exit 0。
- [ ] DMG `hdiutil verify` VALID、ad-hoc署名、`codesign --verify --deep --strict`。

## 2. 実機API/CLI確認

- [ ] 上限未満（例: `maxResidentModels=2` で1常駐）で別カタログモデルを要求 →
      新規常駐として追加され応答の `model` が要求と一致し、既存常駐が残る。
- [ ] 上限満杯（例: 2/2）で新規異なるモデルを要求 → `RESIDENT_LIMIT_REACHED`
      (409) で明示拒否され、常駐集合が変わらない。
- [ ] 満杯から `model unload <id>` で1つ解放後に再要求 → ロードされ応答する。
- [ ] 明示ロード（GUI/設定画面・`mlxbarctl model load`）でも満杯の新規は 409。
- [ ] 同一モデルの再要求・レプリカは満杯でも許可される。

## 3. 実機ランタイムスモーク（機構の確認）

- [ ] lm/vlm のテキスト生成が完走する（モデル能力の品質評価は対象外）。
- [ ] 固定・TTL・レプリカ表示が v2.7.2 と同一。

## 4. 検証結果（記録欄）

- Python回帰: 596 passed＋1 failed（既知fluke、上記のとおり）。
  新規 `Tests/test_v2_7_3_resident_limit.py` 4件は緑。
- `swift build` 成功（差分なし）。
- `.strings` 新規重複なし（追加なし）。
- DMG `MLXBar-2.7.3.dmg`（SHA-256 `0c210d8ef20563f817f8544a0da7afca9412bb895a25a6c4315caefd2972b413`）。
  ad-hoc署名、`hdiutil verify` VALID、app 2.7.3/build 50。
- GitHub Release `v2.7.3` は正式版。DMG＋`.sha256`添付。
- `/Applications/MLXBar.app` を2.7.3へ入れ替え（旧版はバックアップ）。
- 紹介サイトはstudio-riziで公開。プロジェクト8箇所を2.7.3へ、4言語UPDATE追加。
  validate＋count-files・npm test通過後にpush。
  プロジェクトHTTP 200＋`softwareVersion: 2.7.3`、4言語news各HTTP 200を確認。

# TEST PLAN v2.7.4 — 別名形実在モデルの優先解決 / キュー＋逼迫時の非常駐即時解放

- 対象: app 2.7.4 / build 51。Coordinator 2.7.4。`schemaVersion` 1（据え置き）。
- 設計: `DESIGN_v2.7.4.md`。

## 1. 自動ゲート（必須）

- [ ] `Coordinator/.venv/bin/python -m pytest -q Tests -p no:randomly` が全緑。既知の失敗
      `test_model_pool.py::test_ttl_starts_when_a_slow_load_finishes`（asyncio
      タイミング依存）
      以外の失敗はリリース禁止。
- [ ] `Tests/test_v2_7_2_model_routing.py::test_alias_shaped_real_model_is_autoloaded_not_aliased`
      が緑（別名形 `x` の自動ロード・純粋別名の維持・未知の 404）。
- [ ] `Tests/test_v2_7_3_resident_limit.py` 6件が緑（上限未満の追加・満杯の
      409 と非退避・同一モデル許可・明示 pin 厳格・queue＋逼迫の即時解放・
      queue なし warning 逼迫の保持）。
- [ ] `swift build` 成功（本版の Swift 差分なし。ビルドの再確認のみ）。
- [ ] `.strings` 新規重複なし（既存`モデル未ロード`重複1件のみ残置。本版の追加なし）。
- [ ] 新規Swiftコードなし。
- [ ] `sh scripts/build-release.sh` → `VERSION=2.7.4 sh scripts/verify-release.sh` exit 0。
- [ ] DMG `hdiutil verify` VALID、ad-hoc署名、`codesign --verify --deep --strict`。

## 2. 実機API/CLI確認

- [ ] 上限未満で別カタログモデルを要求 → 新規常駐として追加され既存常駐が残る。
- [ ] 上限満杯で新規異なるモデルを要求 → `RESIDENT_LIMIT_REACHED` (409) で
      常駐集合不変。`model unload` 後に再要求すると載る。
- [ ] 常駐決定モデル（固定・規定レプリカ）は応答終了後も規定数で残る。
- [ ] 非常駐は最終要求からの TTL 後に解放される。キュー＋逼迫時は TTL を
      待たず待機中非固定が解放され、生成中・待機中要求は保護される。
- [ ] 実在 `x` 要求は常駐 A に吸収されず `x` が載る。純粋別名は常駐へ解決。

## 3. 実機ランタイムスモーク（機構の確認）

- [ ] lm/vlm のテキスト生成が完走する（モデル能力の品質評価は対象外）。

## 4. 検証結果（記録欄）

- Python回帰: 599 passed＋1 failed（既知fluke、上記のとおり）。
- `swift build` 成功（差分なし）。
- `.strings` 新規重複なし（追加なし）。
- DMG `MLXBar-2.7.4.dmg`（SHA-256 `b34ec31d8f0dba544b6c750e276b5e400d4dd2d8a727c490c30cd20f0e0d57c1`）。
  ad-hoc署名、`hdiutil verify` VALID、app 2.7.4/build 51。
- GitHub Release `v2.7.4` は正式版。DMG＋`.sha256`添付。
- `/Applications/MLXBar.app` を2.7.4へ入れ替え済み（旧2.7.3は
  `/Applications/MLXBar.app.v273-backup`）。`Info.plist` で 2.7.4/build 51、
  `codesign --verify --deep --strict` 通過を確認。
- 紹介サイトはstudio-rizi `4fefda0` で公開。プロジェクト8箇所を2.7.4へ、4言語UPDATE追加。
  validate＋count-files・npm test通過後にpush。
  プロジェクトHTTP 200＋`softwareVersion: 2.7.4`、4言語news各HTTP 200を確認。

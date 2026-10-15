# TEST PLAN v2.7.2 — モデル要求の厳格解決 / 常駐認識の安定化

- 対象: app 2.7.2 / build 49。Coordinator 2.7.2。`schemaVersion` 1（据え置き）。
- 設計: `DESIGN_v2.7.2.md`。

## 1. 自動ゲート（必須）

- [ ] `Coordinator/.venv/bin/python -m pytest -q Tests -p no:randomly` が全緑。既知の失敗
      `test_model_pool.py::test_ttl_starts_when_a_slow_load_finishes`（asyncio
      タイミング依存、§0-J-6 から継続。クリーンツリーでも失敗を確認）
      以外の失敗はリリース禁止。
- [ ] `Tests/test_v2_7_2_model_routing.py`（7件）: max1 での既知別モデル切替・
      未知名 404・汎用エイリアス維持・exact-over-alias・primary 安定・
      `none` 非一致・モデル別既定上限。
- [ ] `Tests/test_openai_tools.py::test_a_made_up_model_name_routes_to_the_sole_resident_instead_of_erroring`
      が 404 期待で緑（厳格解決への意図的更新）。
- [ ] `swift build` 成功（本版の Swift 差分なし。ビルドの再確認のみ）。
- [ ] `.strings` 新規重複なし（既存`モデル未ロード`重複1件のみ残置。本版の追加なし）。
- [ ] 新規Swiftコードなし（`.fixedSize` 検査は既存のみ）。
- [ ] `sh scripts/build-release.sh` → `VERSION=2.7.2 sh scripts/verify-release.sh` exit 0。
- [ ] DMG `hdiutil verify` VALID、ad-hoc署名、`codesign --verify --deep --strict`。

## 2. 実機API/CLI確認

- [ ] 単一常駐（例: 27B を `maxResidentModels=1` で常駐）中に別カタログモデルを
      `/v1/chat/completions` の `model` に指定 → 待機中なら切り替わり応答の
      `model` が要求モデルと一致する。生成中なら `ENGINE_BUSY` (429) で明示拒否
      され、常駐モデルでの黙示応答にならない。
- [ ] 固定（`keepLoaded`）モデル常駐中に別モデルを要求 → `MEMORY_BUDGET_EXCEEDED`
      (503) で明示拒否され、固定が追い出されない。
- [ ] 未知モデル名（typo）を要求 → `MODEL_NOT_FOUND` (404) で明示拒否される。
- [ ] 汎用別名（`loaded`/`local`/`x`/`openai/x`）→ 常駐モデルで応答し、
      `status` の primary が跳ばない。
- [ ] `/v1/models` の `loaded: true` 行の `max_tokens` がそのモデルの上限を指す
      （複数常駐で primary と要求が違うときも混ざらない）。
- [ ] `max_tokens` 省略の生成が要求モデルの上限で切り詰められる（primary の
      上限で切り詰めない）。

## 3. 実機ランタイムスモーク（機構の確認）

- [ ] lm/vlm のテキスト生成が完走する（モデル能力の品質評価は対象外）。
- [ ] 固定・TTL・レプリカ表示が v2.7.1 と同一（行の「固定」・残り時間・並列数）。

## 4. 検証結果（記録欄）

- Python回帰: 592 passed＋1 failed（既知fluke、上記のとおり）。
  新規 `Tests/test_v2_7_2_model_routing.py` 7件は緑。
- `swift build` 成功（差分なし）。
- `.strings` 新規重複なし（追加なし）。
- DMG `MLXBar-2.7.2.dmg`（SHA-256 `f74338d2f908a0bf92a130b5577b1180415f7d15541a5d2b0378495b11c312b2`）。
  ad-hoc署名、`hdiutil verify` VALID、app 2.7.2/build 49。
- GitHub Release `v2.7.2` は正式版。DMG＋`.sha256`添付。
- `/Applications/MLXBar.app` を2.7.2へ入れ替え（旧版はバックアップ）。
- 紹介サイトはstudio-riziで公開。プロジェクト8箇所を2.7.2へ、4言語UPDATE追加。
  validate＋count-files・npm test通過後にpush。
  プロジェクトHTTP 200＋`softwareVersion: 2.7.2`、4言語news各HTTP 200を確認。

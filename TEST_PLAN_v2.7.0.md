# TEST PLAN v2.7.0 — 待機表示の自己回復 / オンデマンド並列 / 待機時間の可視化

- 対象: app 2.7.0 / build 47。Coordinator 2.7.0。`schemaVersion` 1（据え置き）。
- 設計: `DESIGN_v2.7.0.md`。

## 1. 自動ゲート（必須）

- [ ] `Coordinator/.venv/bin/python -m pytest -q Tests -p no:randomly` が全緑。既知の失敗
      `test_model_pool.py::test_ttl_starts_when_a_slow_load_finishes`（asyncio
      タイミング依存、§0-J-6 から継続。クリーンツリー `bf40479` でも失敗を確認）
      以外の失敗はリリース禁止。
- [ ] `Tests/test_v2_7_parallel_idle.py`（6件）: 残留回復・live非破壊・拡張・
      拒否フォールバック・上限1維持・TTL可視化。
- [ ] `Tests/test_model_pool.py` の2件（`test_same_model_requests_stay_serialised`、
      `test_queued_request_can_be_cancelled_per_model`）は v2.7.0 の意図的変更に
      合わせ `maxReplicasPerModel=1` を明示化した設定で直列・cancel を維持。
      無変更の `test_model_replicas.py` 全件と `concurrency=1` 系が緑。
- [ ] `swift build` 成功。
- [ ] `.strings` 新規重複なし（既存`モデル未ロード`重複1件のみ残置）。
- [ ] 新規Swiftコードに `.fixedSize` 使用なし（既存 `QuickChatView` の1件と
      コメント言及は残置）。
- [ ] `sh scripts/build-release.sh` → `VERSION=2.7.0 sh scripts/verify-release.sh` exit 0。
- [ ] DMG `hdiutil verify` VALID、ad-hoc署名、`codesign --verify --deep --strict`。

## 2. 実機GUI目視（日英）

- [ ] 生成完了後に行表示が「待機中」に戻る（1秒 poll 以内）。生成中にアプリを
      終了させず、通常完了・キャンセル・クライアント切断の各後で確認。
- [ ] 同一モデルへの同時生成（例: QuickChat＋API を同時実行）で、空きレプリカが
      なければ自動で `×2` になり両方が進行する。メモリ不足時は片方が順番待ちになる。
- [ ] 非固定モデルの待機中の行に残り時間（例: 「あと○分で解放」）が表示され、
      固定モデルには表示されない。待機時間を変更すると再起動なしで残り時間が変わる。
- [ ] 英語UI: 上記を `Language=English` で一通り確認（新規2キーの訳出と
      行の "Unloads in ..." 表示）。

## 3. 実機API/CLI確認

- [ ] `mlxbarctl model resident` に `idleTTLSeconds` と行ごとの `idleExpiresAt`
      が含まれる。
- [ ] `config set-model-pool --idle-ttl-seconds 60` → 即時反映（再起動不要）。
      元値へ復元する。
- [ ] `generationConcurrency=2` のまま `maxResidentModels=1` で同一モデル2並列が
      完走する。異なるモデル2種の同時常駐は `maxResidentModels` の上限どおり
      拒否される（種類数制限は維持）。
- [ ] `/api/v1/health` → `{"status":"ok","version":"2.7.0"}`。

## 4. 検証結果（記録欄）

- Python回帰: 572 passed＋1 failed（既知fluke、上記のとおりクリーンツリーでも失敗）。
  新規 `Tests/test_v2_7_parallel_idle.py` 6件は緑。`test_model_pool.py` の意図的変更2件も緑。
- `swift build` 成功。
- `.strings` 新規重複なし（既存`モデル未ロード`重複1件のみ残置）。
- 新規Swiftコードに `.fixedSize` 使用なし。
- DMG `MLXBar-2.7.0.dmg`（SHA-256 `33f0669829612e5e3e842b1bf44e22b10e8d522c590de3202554b729e4dce7c7`）。
  ad-hoc署名、`hdiutil verify` VALID、app 2.7.0/build 47。
- GitHub Release `v2.7.0` は正式版。DMG＋`.sha256`添付。
- （実機GUI目視・実機API/CLI確認は §2–3 の手順で実施し、結果を追記する）

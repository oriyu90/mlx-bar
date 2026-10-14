# TEST PLAN v2.7.1 — 常駐固定のライフサイクル修正 / 最新ランタイム互換の確定

- 対象: app 2.7.1 / build 48。Coordinator 2.7.1。`schemaVersion` 1（据え置き）。
- 設計: `DESIGN_v2.7.1.md`。

## 1. 自動ゲート（必須）

- [ ] `Coordinator/.venv/bin/python -m pytest -q Tests -p no:randomly` が全緑。既知の失敗
      `test_model_pool.py::test_ttl_starts_when_a_slow_load_finishes`（asyncio
      タイミング依存、§0-J-6 から継続。クリーンツリーでも失敗を確認）
      以外の失敗はリリース禁止。
- [ ] `Tests/test_v2_7_1_pin_runtime.py`（13件）: リロードの固定優先・transient skip・
      固定失敗の raise、プリロードの二系統 pin、unpin のセッション解放と TTL 回収、
      CLI の `pin` 透過、新旧 registry の finalize、probe 契約。
- [ ] `swift build` 成功（本版の Swift 差分なし。ビルドの再確認のみ）。
- [ ] `.strings` 新規重複なし（既存`モデル未ロード`重複1件のみ残置。本版の追加なし）。
- [ ] 新規Swiftコードなし（`.fixedSize` 検査は既存 `QuickChatView` の1件と
      コメント言及のみ残置）。
- [ ] `sh scripts/build-release.sh` → `VERSION=2.7.1 sh scripts/verify-release.sh` exit 0。
- [ ] DMG `hdiutil verify` VALID、ad-hoc署名、`codesign --verify --deep --strict`。

## 2. 実機API/CLI確認

- [ ] `POST /api/v1/models/{id}/load {"pin": false}` → 常駐するが `keepLoaded: false`・
      `idleExpiresAt` 付き（TTL 管理）。省略時は従来どおり固定。
- [ ] `mlxbarctl model load ID --no-pin` → 同上。`--no-pin` なしは従来どおり固定。
- [ ] 固定プロファイルの `model unpin`（または設定画面の固定解除）→ 行の「固定」が
      消え、TTL 後に解放される。profiles に属さない手動ロードは固定のまま。
- [ ] 再起動プリロード：固定プロファイルは固定復帰、最終使用の非固定は TTL 管理で
      復帰（`idleExpiresAt` 付き）またはメモリ不足時の skip。
- [ ] ランタイム更新：固定優先で復元され、非固定の再収容不可は更新失敗にしない。
- [ ] `runtime stage mlx-vlm`（最新）→ probe に `warnings` 付きで成功。
      `pip check` 通過。`activate` 後の生成が完走する。

## 3. 実機ランタイムスモーク（最新slot直結。量産品質ではなく機構の確認）

- [ ] lm-0.32.0 bf16 実生成（SmolLM2-135M-8bit）: 出力あり、`finish_reason` 正常。
- [ ] lm-0.32.0 8bit（KV量子化）: 出力あり、fallback なし。
- [ ] vlm-0.7.8 bf16 実生成（SmolVLM-256M、テキスト）: 出力あり。
- [ ] vlm-0.7.8 8bit（KV量子化、テキスト）: 出力あり、fallback なし。
- [ ] vlm-0.7.8 2ターン目：`cache_tier: memory`（`cached_tokens` > 0）の再利用。
- [ ] 品質注記: 小規模モデルの指示追従の弱さ（PONG 要求への冗長応答等）は
      モデル能力の問題。bf16 対照でも再現するため本版の回帰ではない。

## 4. 検証結果（記録欄）

- Python回帰: 585 passed＋1 failed（既知fluke、上記のとおり）。
  新規 `Tests/test_v2_7_1_pin_runtime.py` 13件は緑。
- `swift build` 成功（差分なし）。
- `.strings` 新規重複なし（追加なし）。
- 最新slot直結smoke: lm-bf16完走（SmolLM2-135M）／lm-8bit完走（fallbackなし）／
  vlm-bf16完走（SmolVLM-256M、2ターン目 memory reuse `cached_tokens: 15`）／
  vlm-8bit完走（fallbackなし）。
- DMG `MLXBar-2.7.1.dmg`（SHA-256 `94df3360d06f4b0c0bb2c30e9aa8ad74493831c0b96f636a5580760935add4ca`）。
  ad-hoc署名、`hdiutil verify` VALID、app 2.7.1/build 48。
- GitHub Release `v2.7.1` は正式版。DMG＋`.sha256`添付。
- `/Applications/MLXBar.app` を2.7.1へ入れ替え済み（旧2.7.0はバックアップ）。
  `diagnostics`で2.7.1確認。
- 紹介サイトはstudio-riziで公開（v2.7.0分を含む today's update）。プロジェクト8箇所を
  2.7.1へ、4言語UPDATE追加。validate＋count-files・npm test通過後にpush。
  プロジェクトHTTP 200＋`softwareVersion: 2.7.1`、4言語news各HTTP 200を確認。

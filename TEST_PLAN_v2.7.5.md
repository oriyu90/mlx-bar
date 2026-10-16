# TEST PLAN v2.7.5 — 省メモリ / リーク / クラッシュ安全性

対象: app 2.7.5 / build 52。既定値不変・スキーマ追加なし・UI 文言追加なし。

## 1. 自動回帰（実施済み）

- `PYTHONPATH=Coordinator python3 -m pytest Tests/ -p no:cacheprovider -q`
  - 結果: **607 passed**（新規 `test_v2_7_5_memory_stability.py` 7件含む、fluke なし）。
  - 新規 7件:
    - `test_jobs_table_is_bounded`（260 terminal→≤200、active 不触）
    - `test_runtime_history_is_bounded_per_engine`（150→≤100）
    - `test_prune_helpers_keep_newest`（keep 最新 N）
    - `test_usage_from_event_never_raises_on_garbage`（`"abc"`/`None`→0、`cached_tokens` 不正→省略）
    - `test_effective_limits_survive_corrupt_settings`（fallback 8192/100000）
    - `test_status_cache_same_shape_and_cached`（同形状＋2回目は DB 1回）
    - `test_clear_memory_endpoint_has_mlxbar_error_guard`
- `swift build`: 成功（7.6s）。
- `.strings` 新規重複なし。新規 `.fixedSize` なし（`QuickChatView:24` は v1.2.0 からの既存）。
- `plutil -lint Packaging/Info.plist`: OK。

## 2. 実機スモーク（実施済み、2026-10-17、M1 Max 64GB）

- Coordinator 2.7.5 を実ホームで起動（起動前 state.sqlite3 を `/tmp` へ backup）。
- `mlxbarctl model scan --wait` → completed（10件）。
- `mlxbarctl model load lm_studio_folder:670b847bdd4e320c`（gpt-oss-20b-MXFP4-Q8、11GB、mlx-lm）→ `modelMaxTokens: 131072` 正常。
- `mlxbarctl generate --prompt "Reply with PONG only." --max-tokens 16` → 完走。直後 `status` は active 0・lock idle・leases [0]・resident 1＝待機復帰。
- OpenAI `POST /v1/chat/completions`（explicit model、max_tokens 16）→ 200、`usage` に `prompt_tokens_details.cached_tokens: 0`（計測ありのため含める、仕様どおり）。
- `status` 2連打 → 139ms→123ms（cache 有効、形状同一）。
- `logs show --limit 3` → retention 2000、`prompt-cache status` → memory 18MB・namespace `-kv-bf16` 正常。
- `model unload` → resident 0。coordinator 正常停止。実ホームの jobs 5・history 7・logs 46（有界）。

## 3. 残作業（次版への引き継ぎ）
1. 実画面目視の日英確認（本版 Swift は文言不変のため再確認のみ。`chatOutput` cap・`recentLogs` 200 件の表示崩れなしを確認）。
2. 実機 2 並列のメモリトレース（v2.7.0 から継続。27B 級 2 重常駐を伴うため単体テストで担保し本番では未実施）。
3. 長期稼働（30日）での `state.sqlite3` サイズ・WAL 回収の追跡（本版の prune＋checkpoint の効果測定）。
4. 実機 active slot の更新はオーナーの明示操作に任せること（autoCheck 既定 off 維持）。

## 4. 公開検証（実施済み、2026-10-17）

- `sh scripts/build-release.sh` → `dist/MLXBar-2.7.5.dmg`（47MB、SHA-256 `881a4088…5151a59`）、`hdiutil verify` VALID、app 2.7.5/build 52。
- `VERSION=2.7.5 sh scripts/verify-release.sh` → 署名・plist・DMG すべて通過（ad-hoc）。
- `gh release create v2.7.5` → 正式版（draft ではない）。DMG＋`.sha256` 添付。
- `/Applications/MLXBar.app` を 2.7.5 へ入れ替え（旧 2.7.4 は `/Applications/MLXBar.app.v274-backup`）。`codesign --verify` 通過。
- 紹介サイト studio-rizi `b1d5494`（EveryTMP 同時更新の競合は merge＋再生成で解消）。`npm run build`（validate＋count-files 260/16000・重複 0）・`npm test`（34 passed）通過後に push。
- 公開確認：プロジェクト HTTP 200＋`softwareVersion: 2.7.5`、4言語トップ（`/`,`/en/`,`/zh/`,`/pt/`）各 HTTP 200＋v2.7.5 news。

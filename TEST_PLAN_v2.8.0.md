# TEST_PLAN v2.8.0 — 検証計画と実施結果

2026-10-18。v2.8.0（非常駐モデル＋待機時間延長、生成中断の協調化、
調査報告書の反映）の検証記録。

---

## 1. 自動テスト

### 1.1 Python 回帰

```
PYTHONPATH=Coordinator Coordinator/.venv/bin/python -m pytest Tests/ -q
```

| 版 | 結果 |
|---|---|
| ベースライン（v2.7.5、`git stash`） | 607 passed ＋ 1 failed |
| v2.8.0 | **648 passed ＋ 1 failed** |

failed は既知の fluke
`test_model_pool.py::ModelPoolTests::test_ttl_starts_when_a_slow_load_finishes`。
本版の差分ではないことをクリーンツリーで再確認した（stash して単独実行、
3/3 回同じ失敗）。

### 1.2 新規テスト

| ファイル | 件数 | 内容 |
|---|---|---|
| `Tests/test_v2_8_overflow_idle.py` | 29 | resident／非常駐の admission、非常駐でも要求に応答できる、メモリ安全が非常駐にも適用される、reaper の victim 順序と上限引き下げ、延長の加算・消費・範囲・非数値、replica 全体への適用、HTTP 経路（200/422）、CLI wire、既存 CLI Namespace への追加フィールド露出 |
| `Tests/test_v2_8_generation_abort.py` | 13 | 切断で即停止、`/rpc cancel` で即停止、executor を経由しないこと、切断時の出力が `stop` にならないこと、orphan limit、`totalTimeoutSeconds` の受け渡しと範囲外の無視、rayon/tokenizers スレッド数の上限 |

### 1.3 旧実装で落ちることの確認

新テストを `Workers/common/server.py` を stash した旧実装に対して実行
（`ORPHAN_GENERATION_SECONDS` は shim で定義して collection を通した）。
落ちたのは 5 件で、いずれも本版の意図した変更に一致する：

- `test_cancel_rpc_stops_the_generation_immediately`（abort を signal しない）
- `test_cancelled_generation_reports_cancelled_not_stop`
- `test_orphan_generation_is_stopped_without_a_cancel`
- `test_abort_handles_are_released_when_streams_end`
- `test_abandoned_stream_releases_its_abort_handle`

`test_disconnect_stops_the_generation_immediately` は**旧実装でも通る**。
これは測定結果であり、DESIGN_v2.8.0.md §2.2 に記録したとおり、旧実装も
executor が空いているなら切断で 0 token だった。このテストは回帰ガード
（協力中断が「遅れても止まる」状態を壊していないことの保証）として残す。

### 1.4 v2.7.3 の suite の意図的更新

v2.7.3 の「上限到達は 409 で拒否」契約はオーナーの依頼により置き換わった。
`Tests/test_v2_7_3_resident_limit.py` の docstring にその経緯を書き、
拒否テスト 2 件を非常駐契約のテストに書き換えた。件数は維持。
`test_v2_7_2_model_routing.py::test_max1_switches_to_a_known_distinct_model`
も 409 期待だったため、`..._loads_a_second_distinct_model_as_non_resident`
へ変更。

### 1.5 Swift

```
swift build            # 成功
plutil -lint en.lproj  # OK
```

`.strings` の重複キー検査：v2.8.0 で**新規の重複はなし**。**既存の重複 1 件
（`モデル未ロード`、177 行目と 354 行目）を解消**した（英訳の
"No model loaded" と "No Model Loaded"）。長い間残っていた既知の
不一致で、本版の `en.lproj` 編集で解消できたため片付けた。
`.fixedSize` の新規使用なし。

### 1.6 ローカライズ

新規キー 6 件（`非常駐` / `非常駐モデル` / `加算秒数` / `延長` /
`加算済み` / 入力欄の label・hint）。`ja.lproj` はソース言語なので
追加不要。秒表示とバッジは既存慣習どおり `japanese:` 引数の
インライン日英（`LS()` キーではない）。

---

## 2. 実機項目（未実施 — 次版 实機作业）

以下は**この環境で実機確認できていない**。オーナー側の実機作業として
残す。特に GUI は目視が一度もしていない。

### 2.1 非常駐モデルの一連

- [ ] `maxResidentModels: 2` で A・B をロード → 一覧に「常駐 2」
- [ ] C をロード → 成功し「非常駐」バッジ、`ロード済みモデル · 3（常駐 2・非常駐 1）`
- [ ] C で API リクエスト（`/v1/chat/completions`）→ `model: C` で応答
- [ ] C の行に `あと900秒で解放` が**秒**で出る
- [ ] 入力欄 に `600` → 延長 → `あと1500秒で解放`、`加算済み +600` 表示
- [ ] C を1回使う → カウントダウンが `900` に戻る（延長は消費される）
- [ ] 延長を3回繰り返しても恒久 pin にならない
- [ ] ピン留めした C → 入力欄が**出ない**（期限が無いモデルなので）
- [ ] `mlxbarctl model resident` に `resident` / `idleSecondsRemaining` が出る
- [ ] `mlxbarctl model extend-idle C 600` が同じ結果になる
- [ ] TTL 経過で C が自動解放される

### 2.2 英語UI

- [ ] 上記と同等の一連の英語 UI 目視（`非常駐` → "Non-resident"、
      `あと900秒で解放` → "Unloads in 900 sec"、入力欄の placeholder / label）

### 2.3 協力中断（§2.2 の未実測分）

報告書の `ReadError` 連鎖を再現し、CPU が下がることを**測る**：

- [ ] `timeoutSeconds` 120 秒のクライアントから長い生成を要求し、
      切断を繰り返す
- [ ] Worker の `%CPU` が切断後 1〜2 token で 2% 台へ落ちることを `ps` で確認
- [ ] Coordinator ログの行数が増え続ける異常がないか

GUI 目視と 2 並列メモリトレースは v2.7.0 からの継続事項（§2.1）。

---

## 3. 報告書の主張と検証結果

| 報告書 § | 主張 | 判定 |
|---|---|---|
| §2.1 | `max_workers=1` は不可避で正しい | **正しい**（MLX の GPU stream はスレッドごと） |
| §2.2 / §5.1 | `close()` はキューに積むだけ、中断が遅れる | **正しい**。ただし「`max_tokens` まで続く」は実測で否定（executor が空なら 0 token） |
| §2.3 A | `cancel` 経路も 1 token 遅延 | **正しい**（`await on_mlx_thread(close)`）。修正 |
| §2.3 B | `MEMORY_PRESSURE` 監視も token 境界でしか効かない | **正しい**。今回は変更せず（検出は token 境界である必要があり、prefill 中の判定は別問題） |
| §2.3 C | `totalTimeoutSeconds` が機能していない可能性 | **機能している**（`supervisor.py:614`）。**追加のソフトタイムアウトは不要**と判断 |
| §4.1 | 28 コア中 1 コアが飽和 | 報告者の実測どおり。当方の環境では再現できず |
| §4.3 | 2 モデルが同時常駐 | `maxResidentModels` と同期している。v2.8.0 で上限超過が弾かなくなったので、この状況が起きやすい → §2.1 の緩和が効く |
| §5.1 | 協調的取消を導入 | **採用** |
| §5.2 | 15 分ソフトタイムアウト | **不採用**（§2.2。既に 3600 秒で機能している） |
| §5.3 | Metal Residency の最適化 | **不採用**（§2.2。MLX 内部で干渉点なし、計測もしていない） |
| §5.4 | `RAYON_NUM_THREADS` の制御 | **採用** |
| §6.1 | `maxResidentModels` 3→1 | **コードにはしない**（依頼内容と逆。README に運用として記載） |
| §6.2 | 設定だけでは対処不可 | **正しい**。コード修正が要ると判断した点は §5.1 のとおり |
| §6.3 | プロンプトキャッシュを縮小 | **不採用**（既定値変更はしない。§1 の非常駐機能が同じ効果を運用として得られる） |
| §7.3 | 既存テストへの影響 | `Tests/test_v2_7_3_resident_limit.py` 等 5 ファイルを意図的に更新（§1.4）。それ以外の影響なし |

---

## 4. 既知の残件

1. `test_ttl_starts_when_a_slow_load_finishes` の asyncio タイミング依存
   （v2.4.0 からの既知失敗）。
2. `CrossModelGenerationTests::test_stress_concurrent_requests_leave_no_leaked_lane_state`
   は**クリーンツリーでも flaky**（12 回中 6 回失敗）。本版の原因ではないが、
   CI の Python 版固定かテスト側の待機条件の堅牢化を検討すべき。
3. GUI の目視確認（§2.1 / §2.2）。
4. 協力中断の定量的効果の実測（§2.3）。
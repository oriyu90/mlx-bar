# TEST_PLAN v3.2.0 — 実機スモーク手順（TASK-001 / WP-913）

- 対象: mlx-bar v3.1.1〜v3.2.0（リポジトリ HEAD 9530341 + TASK-001 の変更）
- 前提: **実機の `/Applications/MLXBar.app` は v2.8.1 のまま**。以下の手順は v3.1.x 以降を
  インストールしたうえで実施する（AC-0-1）。
- 自動テストで担保済みの範囲は `TEST_RESULTS.md` を参照。本手順書は**実機・実画面・
  実モデル**でしか確認できない項目だけを扱う。
- 記録: 各項目の「実測」欄に日時・結果・ログ抜粋を残す。失敗は `tasks/TASK-001/EXEC/` に
  `smoke-YYYYMMDD.md` として保存する。

---

## 0. 事前準備

```bash
# 1) 現在の版と DB の分類版を控える（読み取りのみ）
defaults read /Applications/MLXBar.app/Contents/Info.plist CFBundleShortVersionString
sqlite3 "file:$HOME/Library/Application Support/MLXBar/state.sqlite3?mode=ro" \
  "select key,value from metadata where key in ('catalog_classifier_version','last_loaded_model_id','api_autoload_suspended');"

# 2) 設定のバックアップ（上書き前に必須）
cp "$HOME/Library/Application Support/MLXBar/config.json" /tmp/mlxbar-config-backup.json

# 3) ログの現在位置を控える（以後の差分だけを見る）
wc -l "$HOME/Library/Application Support/MLXBar/logs/coordinator.log"
```

- 旧 `.app` を `/Applications/MLXBar-v2.8.1.app` に退避してから差し替える（ロールバック用）。
- 失敗時のロールバック: 退避した `.app` を戻し、`/tmp/mlxbar-config-backup.json` を復元する。

---

## 1. AC-0-2 / AC-2-1: アップグレード後の再スキャンと Ornith の再分類

1. 新 `.app` を起動し、メニューバー →「今すぐ再スキャン」または起動を待つ。
2. DB を読む:

```bash
sqlite3 "file:$HOME/Library/Application Support/MLXBar/state.sqlite3?mode=ro" \
  "select id,engine,json_extract(capabilities,'$.vision'),loaderId from models where id like '%Ornith%' or id like '%Niwaki%';"
```

- 期待: Ornith が `engine=mlx-vlm` / `capabilities.vision=1`、Niwaki が `engine=mlx-vlm` /
  `capabilities.vision=0` / `loaderId=niwaki-qwen4-exp`。
- 実測: ______

## 2. AC-1-1: 赤文字が 20 秒で消え、再点灯しない

1. Coordinator を停止する（`launchctl bootout` もしくはアプリのサービス停止）。
2. メニューを開いたまま **60 秒**観察する。
3. 期待: 赤文字は最初の表示から 20 秒 ±1 秒で消え、その後**戻らない**。
   代わりにステータスが「サービス停止 / Service stopped」とアイコンで示される。
4. 実測: ______（スクリーンショットは `shots/` に置く。Git に入れない）

## 3. AC-1-3: 設定画面のバリデータ違反バナー

1. 設定 →「詳細」で範囲外の値（例: 温度 5）を入力して保存。
2. 期待: 設定画面内の赤バナーに理由が出て、20 秒で消える。
3. 実測: ______

## 4. AC-4-1 / 4-2 / 4-3: 言語・設定の復元

1. 言語を「日本語」にしてアプリを終了 → 再起動。
2. 期待: **最初の描画から**メニューバーの表示が日本語。
3. Coordinator を 5 秒遅らせて起動（`launchctl kickstart` を遅らせる等）。
4. 期待: 起動後に言語・設定が自動で正しくなる（設定画面を開かなくてよい）。
5. 3 の状態で Coordinator を一度も起動せずに終了・再起動。
6. 期待: 最後に使った言語のまま表示される。
7. 実測: ______

## 5. AC-4-4: 設定復旧の一度だけ通知

1. `config.json` を壊す（例: `{` だけ書く）→ アプリ起動。
2. 期待: バックアップ名入りの通知が**一度だけ**出る。再起動では出ない。
3. 実測: ______（終了後 `config.json` は `config.invalid-*.json` が作られているか確認）

## 6. AC-3-1 / 3-3: 常駐モデルの起動時ロードと 15 分維持

1. 設定で Niwaki を「常駐」にする（他モデルは常駐解除）。
2. アプリとサービスを再起動。
3. 期待: メニューに「常駐モデルを読み込み中…」→ ロード後 `loadedModels` に Niwaki。
4. **15 分後**も常駐が維持されている（idle TTL 900 秒で消えない）。
5. 常駐をメモリ不足になる設定（`defaultPerModelMaxGB` を小さく）にして再起動。
6. 期待: 「常駐モデルの読み込みに失敗しました（理由: …）」が日英で出る。
7. 実測: ______

## 7. AC-5-1 / 5-2: niwaki のロード

- **v3.2.0 では AC-5-1 は未達**（重み再構成フック未実装）。期待されるのは 5-2 のみ:
1. Niwaki をロード。
2. 期待: 数十 GB を積む前に `MODEL_NEEDS_BUNDLED_LOADER` で即失敗し、日英の対処が出る。
3. `models.bundledLoaders` を true にしても、未実装のため同じコードで止まる（安全側）。
4. 実測: ______

## 8. AC-6-1 / 6-2 / 6-5: メモリ判定

1. 他モデル常駐なしで Niwaki（35.0 GiB）をロード。
2. 期待: 成功する（`MEMORY_HEADROOM_INSUFFICIENT` が出ない）。
3. Ornith 常駐中に Niwaki を要求。
4. 期待: Ornith を回収してロードできる。回収不能なら理由付きで拒否。
5. `logs/coordinator.log` を 1 週間監視し、`安全余白を残してロードできません` が
   回収可能な状況で出ないこと。
6. 実測: ______

## 9. AC-7-2 / 7-3: 常駐の切り替えと解除

1. 常駐を A（Niwaki）→ B（Ornith）に変更。
2. 期待: A の「固定」が外れ、B が自動でロードされる。
3. 再起動 → B のみが載る（A は載らない）。
4. B の固定を解除 → 再起動 → B が復活しない。
5. 実測: ______

## 10. AC-7-1 / 7-5: ピン表示と失敗の可視化

1. 手動でモデルをロード（ピンしない）。
2. 期待: 「固定」ではなく「一時 / Temporary」。
3. メモリ不足になる状態でピン → ロードを実行。
4. 期待: 無音ではなく赤バナーに理由が出る。
5. 実測: ______

## 11. AC-5-5: 失敗後に worker が残らない

```bash
ps aux | grep -E "mlx_(lm|vlm)_worker" | grep -v grep
```

- ロード失敗の直後に上記が空であること。
- 実測: ______

---

## 付録: 自動テストで担保済み（実機不要）

- ISS-1 のタイマー/キャンセル競合、ISS-4 の言語ミラー・再試行（Swift ユニット）
- ISS-2 の capabilities / attempts / 警告（Python ユニット + 実モデル config probe）
- ISS-6 の判定順・コード分離（Python ユニット）
- ISS-7 の profilePinned・マージ API・解除セマンティクス（Python ユニット + HTTP）
- ISS-5 のレジストリ・preflight・opt-in 既定無効（Python ユニット）
- 壊れた中間コミットの再発防止（`import mlxbar.main` スモーク）

詳細と実測値は `TEST_RESULTS.md`。

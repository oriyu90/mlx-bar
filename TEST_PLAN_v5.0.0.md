# TEST PLAN v5.0.0 — APIフォールバック

対象: MLXBar v5.0.0（build 70）。v4.0.2 からの追加機能。
設計: [`DESIGN_v5.0.0.md`](DESIGN_v5.0.0.md)

---

## 1. 自動テスト（必須・全パス）

```sh
cd Coordinator && .venv/bin/python -m pytest ../Tests -q
swift build --disable-sandbox
swift test --disable-sandbox
python3 scripts/check_i18n.py
```

| 項目 | 期待値（v5.0.0 実測） |
|---|---|
| pytest | **1051 passed / 0 failed / 1 skipped** |
| swift test | **45 / 0** |
| check_i18n | 525キー参照、欠落 **0** |

### 1-1. 新規テストの内訳（`Tests/test_v5_0_0_fallback.py`）

| クラス | 確認内容 |
|---|---|
| `SettingsValidationTests` | 既定値が無効・空、プロバイダーID形状、ID重複、プロトコル、URL、優先順位・レート窓の範囲、空の `sourceModels`、ヘッダーの改行拒否、`SettingsStore` の未知キー拒否、永続化、**APIキーが config.json に入らない**（0600）、`providerId` のパストラバーサル拒否 |
| `SelectorTests` | 無効時は0件、**明示 > ワイルドカード**、ワイルドカードのみの一致、未割当は0件、優先順位→宣言順、無効なプロバイダー/モデルの除外、**画像は vision のみ**、vision が無ければ0件、別名一致、クールダウン/レートの後回し、試行数上限 |
| `LimiterTests` | 窓内カウントと失効、0=無制限、クールダウン失効、成功で解除、**有界（256）** |
| `TranslatorTests` | Chat/Responses/Anthropic の本文、system の移動、ツール呼び出し/結果、画像の貼り付け先、存在しない画像のスキップ、認証ヘッダー、出力トークン上限、温度上書き、コンテキスト省略（system 保持・最終ターン保持） |
| `ParserTests` | 3プロトコルのストリーム/非ストリーム解析 |
| `ServiceTests` | 最初の候補で成功、次候補へフォールスルー、全滅で例外、401は再試行不可、レート上限で後回し、APIキー送信、非SSE応答の受理、接続テスト、トリガー集合、状態 |
| `OpenAISurfaceFallbackTests` | **無効時は v4.0.2 のエラー**、非ストリーム/ストリームのフォールバック、全滅時は元のローカルエラー、`MODEL_NOT_FOUND` は転送しない、未割当は転送しない、**画像は vision モデルのみ**、vision 無しはローカル、3プロトコル切替 |
| `AnthropicSurfaceFallbackTests` | messages の非ストリーム/ストリーム、無効時の `overloaded_error` |
| `ManagementApiTests` | 追加・更新・削除・APIキー（応答に含まれない）・404・422・状態 |
| `CliParityTests` | CLI が管理APIと同じ形を送る（空パッチ拒否、範囲外拒否、`*` 既定） |
| `EarlyLocalErrorTests` | **モデル解決中のローカルエラーでもフォールバックする**（実機e2eで発見した不具合の回帰） |
| `ImageForwardFailureTests` | 画像の即時転送が全滅したとき、外部のエラーを報告する（ローカルエラーが無い経路） |
| `UpstreamErrorAsSSETests` | **外部がエラーをSSEで返したとき空応答にしない**（Anthropic `event: error` / Responses `response.failed` / `{"error":...}`。内容前は次候補、内容後は `error` イベント） |
| `AnthropicAllFailedTests` | **Anthropic面で全滅時に元のローカルエラー（429 overloaded_error）を返す**（500にしない） |
| `ReviewFindingsTests` | 独立レビューの指摘9件の回帰（表示名一致・空ストリーム・ツール引数上限・レート有界・部分破損の保持・画像順序・削除済みプロバイダーの復活拒否・エラー種別） |

### 1-2. Swift（`Tests/MLXBarTests/FallbackSettingsTests.swift`）

文書のデコード、欠落キーの既定値、`*` = 全モデル、明示+`*` の併存、明示のみ（大小無視）、空の割当、表示名、プロバイダーの利用可否、使用状況/直近結果の解析、プロトコル表示キー。

### 1-3. 回帰（既存テスト）

`CoordinatorErrorText` の登録漏れ検査（`test_v3_2_0_error_contract.py`）が `FALLBACK_*` 11コードを検出し、日英登録を強制します。既存 947 件は変更なく通ります。

### 1-4. 独立レビューの指摘に対する回帰

レビューで見つかった指摘は、修正を戻すと対応するテストが fail することを確認済みです（Anthropic面の500: `AnthropicAllFailedTests` 2件、SSEエラーの無言化: `UpstreamErrorAsSSETests` 3件、ほか `ReviewFindingsTests` 9件）。

---

## 2. 実機e2e（必須）

```sh
python3 scripts/verify-fallback-runtime.py
```

実コーディネーター（一時ホーム・ポート18935・管理APIはUDS）と偽の上流プロバイダーを起動し、公開APIを実際に叩きます。モデルの重みは不要です（フォールバック経路はワーカーに触れません）。

| # | 確認 | 期待 |
|---|---|---|
| 2-1 | 無効時の `MODEL_NOT_FOUND` | 404 `MODEL_NOT_FOUND` のまま（転送しない） |
| 2-5 | プロバイダー/モデル追加、APIキー保存 | 201 / 201 / 200、キーは応答に含まれない |
| 2-2 | 自動ロード停止＋有効化 → ローカル拒否 | 200、外部の応答が返る、`Authorization: Bearer` が送られる |
| 2-3 | Anthropic 入口 | 200、`content[0].text` が外部の応答 |
| 2-4 | 画像付き要求 | 送信先は vision モデルのみ、`image_url` が含まれる |
| 2-5 | 状態 | `lastOutcome.model` が直近の送信先 |
| 2-6 | 無効に戻す | 409 `MODEL_NOT_LOADED`（元のローカルエラー） |

v5.0.0 実測: **15/15 PASS**。

---

## 3. 手動確認（GUI・実機）

アプリを入れ替えたあと、次を目視で確認します。

| # | 手順 | 期待 |
|---|---|---|
| 3-1 | 設定 → 「APIフォールバック」を開く | 日本語UI（英語設定では英語UI）で表示され、レイアウト崩れ・行間の詰まりがない |
| 3-2 | 「プロバイダーを追加」→ 名前・プロトコル・URL・タイムアウトを入力 | カードが増え、再起動後も残る |
| 3-3 | APIキーを保存 → アプリ再起動 | 「APIキー（保存済み。変更する場合だけ入力）」になる |
| 3-4 | 「接続テスト」 | 成功時は「接続に成功しました（NNN ms）」、失敗時は日本語のエラー文言 |
| 3-5 | 「モデルを追加」→ 優先順位・画像対応・全モデル | 行が増え、`*` のバッジが出る |
| 3-6 | 「フォールバック元を選択…」で個別モデルをチェック | 個別モデルが `*` より優先される旨の説明どおりに動く（「すべてのモデル」チェックと併存可） |
| 3-7 | 「詳細」でレート上限・窓・最大出力・最大コンテキスト・画像詳細度を変更 | 再表示しても値が保持される |
| 3-8 | 有効化のトグル | 確認ダイアログが出てから有効になる |
| 3-9 | キューが一杯の状態でAPIへ要求 | 外部の応答が返り、「状態」に直近のフォールバックが出る |
| 3-10 | 言語を英語に切り替え | すべての文言が英語（日本語のままのキーが無い） |
| 3-11 | 無効に戻して同じ要求 | 従来の `QUEUE_FULL` などが返る |

---

## 4. 確認しなかった範囲（既知の制限）

- 実際の OpenAI / Anthropic 本番APIへの課金を伴う送信は行っていません（偽サーバーで検証）。実キーでの疎通は 3-4 の「接続テスト」で確認してください。
- 外部プロバイダーのレート制御はMLXBar内のカウントのみで、プロバイダー側の `Retry-After` は読みません。
- レート表とクールダウンはメモリ内で、再起動するとリセットされます。
- `openai-responses` の `previous_response_id`（サーバー側の会話状態）は使いません。毎回すべてのメッセージを送ります。

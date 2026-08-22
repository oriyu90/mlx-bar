# MLXBar v1.5.1 Test Plan

v1.5.1は、ランタイムがプロンプトキャッシュを共通prefixまで巻き戻せない構成でも生成を失敗させないための修正リリースです。

## 受入基準

1. mlx-vlmの再利用機構が例外を送出しても、応答をまだ送信していなければ新しいキャッシュで一度だけ再試行し、生成が成功する。
2. 再試行はディスクキャッシュを維持し、該当ターンがcold prefillへ落ちない。
3. ランタイム由来でない通常のモデルエラーは再試行せず、そのまま報告する。
4. 回復回数が`GET /api/v1/prompt-cache`の`reuseFailures`に出る。
5. v1.5.0で追加した動作（mlx-lmキャッシュ、メモリ監視、`finish_reason`、`stop`、tool記法検出）を退行させない。

## 障害注入・実機シナリオ

- 実モデルへ、システムprefixだけを共有する枝分かれ要求を送り、巻き戻しが必要な状態を作る。
- ランタイムのdispatchモジュールを装った例外と、通常のモデルエラーを注入し、再試行の有無を区別する。

## 完了条件

- Pythonテストが非推奨警告なしで完走する。
- Swift Debug／Release buildが成功する。
- DMG内のアプリ版が1.5.1、build 20で、arm64、署名、CLI、同梱Coordinator、ソース収録、ディスクイメージ整合性が検証される。
- 公開assetのSHA-256がローカル成果物と一致する。

## 2026-08-22 実施結果

- 実機`Qwen3.5-9B-MLX-8bit`（`Qwen3.8-27B-MLX-8bit`と同じqwen3_5 hybrid構成）で、システムprefixだけを共有する枝分かれ要求が
  `AttributeError: 'ArraysCache' object has no attribute 'trim'`で失敗することを再現した。
- **同じ手順がv1.4.1のコードでも失敗することを確認した。** 本件はv1.5.0の変更が原因ではない。
- 修正後は同じ3要求がすべて成功し、枝分かれターンは`cache_tier=disk`（1,174 tokens再利用）になった。`reuseFailures`は2。
- `_cache_fully_retained()`が`ArraysCache`に対してTrueを返す一方`is_trimmable()`はFalseを返すことを、ランタイムの実クラスで確認した。
- ランタイム由来の失敗だけが再試行され、通常のモデルエラーは1回で報告されることを回帰テストで固定した。
- Pythonテスト195件が成功した。

## 上流への報告

- 根本原因はmlx-vlm 0.6.15の`generate/dispatch.py`にある。巻き戻しの事前確認が`_cache_fully_retained()`で行われ、
  `trim`メソッドを持たないキャッシュ型を除外できていない。`is_trimmable()`を併用すれば解決する。
- MLXBar側は回避策のみを実装しており、上流の修正が入れば再試行は自然に発生しなくなる。

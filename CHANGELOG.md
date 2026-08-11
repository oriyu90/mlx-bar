# 変更履歴

このプロジェクトの主な変更を記録します。

## [1.0.0] - 2026-08-11

最初の正式リリースです。

- 設定ウィンドウを可変サイズ化し、長いフォームが切れない最小サイズへ拡大
- GUIをEnglish標準に変更し、「General」から日本語へ切替可能に
- ランタイム操作をメニューバーから設定の「Runtime」へ移動
- 未導入の`mlx-lm`と`mlx-vlm`を初回起動時にバックグラウンドで自動インストール
- OpenAI互換APIの入力エラー形式、SSE終了処理、時刻整合性、接続維持を安定化

- Apple Silicon Mac向けメニューバー型モデル管理UI
- MLX LM、MLX VLM、LM Studioモデルの統合カタログ
- モデルのロード、生成、停止、アンロードと状態表示
- OpenAI Chat Completions互換APIとAPIキー・LAN公開設定
- ZCode、Open Interpreter、LibreChat向け互換処理
- tool calling、ストリーミング、並列要求キュー
- ランタイムのGUI更新、復元、旧版削除
- Max token、温度、Top P、繰り返しペナルティ設定
- APIログ、診断、データとランタイムの一括削除

# MLXBar v1.8.3 設計書（Ornith 1.5系のtool呼び出しクラッシュ修正）

更新日: 2026-08-31
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

**目的:** Ornith 1.5系モデル（`Ornith-1.5-35B-A3B-MLX-4bit`・`Ornith-1.5-9B-MLX-8bit`、いずれもmlx-lmエンジン割り当て時）が、tool呼び出しを含む会話で`GENERATION_FAILED`により必ずクラッシュする不具合を修正する。

**非目的:** 他のエンジン（mlx-vlm）・他のモデルファミリーの挙動は変更しない。管理API・Anthropic互換APIのハンドラは無変更。設定schemaの変更もなし。

## 2. 不具合の実体

OpenClaw（外部クライアント、tool呼び出しを行うsub-agentのモデルとして検証中）との実接続テストで発見。

再現手順（最小構成）:
1. `Ornith-1.5-9B-MLX-8bit`へ1ターン目（system+userのみ、tool呼び出し無し）をリクエスト → 成功。
2. 直後に2ターン目（assistantのtool_calls + tool結果を含む会話）を同モデルへリクエスト
   → `{"error":{"code":"GENERATION_FAILED","message":"生成に失敗しました（ランタイムエラー）"}}`
   で確実に失敗。

再現率100%。並行処理の有無、`repetition_penalty`の値、プロンプトキャッシュの有無、事前に何件リクエストを送ったか、いずれも結果に影響しない。

### 調査で分かったこと（詳細は`mlx-bar.md`§0-U）

- `mlx_vlm_worker`経由（アダプター単体を直接呼び出すテスト、実キャッシュ・実モデルファイル使用）では**再現しない**。
- 実際に本番でクラッシュしたリクエストをプロキシで捕獲し、開発用チェックアウトからCoordinator＋Worker一式
  （本番と同一の`common/server.py`のHTTP/UDS経路）を起動して再生したところ、**mlx-lmエンジン経由でのみ再現**した。
- 一時的に例外の完全なトレースバックをログへ出して確認したところ、原因は
  `mlx_lm_worker/adapter.py`の`stream()`が呼ぶ`self.processor.apply_chat_template(prompt, ...)`の内部、
  Ornith 1.5のchat_template.jinjaがtool callの引数をJinjaの`|items`フィルタで反復している箇所で、
  `TypeError: Can only get item pairs from a mapping.`が発生していた。

### 根本原因

OpenAI互換の`assistant.tool_calls[].function.arguments`はJSON**文字列**（OpenAI仕様どおり）。
`mlx_lm_worker`はこれを一切加工せず、そのまま`self.processor.apply_chat_template()`
（HuggingFaceトークナイザの生の実装）へ渡していた。Ornith 1.5のchat templateは
この値を辞書として扱い`|items`で反復するため、文字列を渡すと即座に失敗する。

`mlx_vlm_worker`は同じ問題を持たない。`mlx_vlm.prompt_utils.apply_chat_template`が、
自身の非公開関数`_normalize_tool_message`で`arguments`をJSON文字列→dictへ変換してから
テンプレートへ渡しているため。`mlx_lm_worker`側には同等の変換が存在しなかった。

## 3. 修正内容

`Workers/common/tool_calls.py`に`normalize_tool_call_messages(messages)`を追加した
（`mlx_vlm`の`_normalize_tool_message`と同等の変換を、mlx-bar自身の共有コードとして実装。
サードパーティの非公開APIを直接importする設計は避けた）。

```python
def normalize_tool_call_messages(messages) -> list:
    ...
```

- `assistant`メッセージの`tool_calls[].function.arguments`が文字列なら`json.loads`でdictへ変換する。
- パースに失敗した場合は空dict`{}`にフォールバックする（テンプレートをクラッシュさせず、
  もともと壊れていた引数の呼び出しを空引数として扱う方が、無関係な500エラーより呼び出し元に有用）。
- 変換が必要なメッセージが1件も無ければ、入力の`messages`を**そのままの同一オブジェクトで**返す
  （tool呼び出しの無い会話が大多数を占めるので、不要なコピーを避ける）。
- 変換が必要な場合だけ、該当メッセージとその`tool_calls`/`function`をコピーして書き換える。
  呼び出し元が保持する元のメッセージ辞書は変更しない。

`mlx_lm_worker/adapter.py`の`stream()`と`_render_prompt()`（`count_tokens`が内部で呼ぶ）の、
`self.processor.apply_chat_template()`を呼ぶ直前でこの関数を適用するようにした。

## 4. 不変条件（守ったこと）

- tool_callsを含まない会話（大多数のケース）は、変換関数が入力と同一オブジェクトを返すため、
  実質的に無変更。回帰テストで同一オブジェクトであることを直接確認している。
- 既にdictとして`arguments`を送ってくるクライアント（OpenAI仕様外だが呼び出し元が自前で
  パース済みの場合）も壊さない。文字列でなければ変換をスキップする。
- `mlx_vlm_worker`・管理API・Anthropic互換APIは1行も変更していない。
- `tool_template_kwargs_attempts`によるtool_choice/tools対応のフォールバック機構
  （v1.7.1由来）はそのまま。この修正は「テンプレートに渡す前のメッセージ整形」だけを追加する。

## 5. 検証

`TEST_PLAN_v1.8.3.md`を参照。Python回帰367件（v1.8.2の361 + 新規6）。
実機で`Ornith-1.5-9B-MLX-8bit`・`Ornith-1.5-35B-A3B-MLX-4bit`双方のtool呼び出しクラッシュ解消を確認。

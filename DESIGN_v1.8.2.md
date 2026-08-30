# MLXBar v1.8.2 設計書（API経由の複数モデル常駐が機能しない不具合の修正）

更新日: 2026-08-30
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

**目的:** `models.pool.maxResidentModels`を2以上に設定していても、API（`/v1/chat/completions`）
経由の逐次リクエストだけでは常駐モデル数が実質的に1体を超えられない不具合を修正する。

**非目的:** プールの他の挙動（メモリ予算判定、ENGINE_BUSY判定、replica管理）は変更しない。
設定schemaの変更もなし。管理API・Anthropic互換APIのハンドラも無変更。

## 2. 不具合の実体

OpenClaw（外部クライアント）との実接続テストで発見。手順:

1. 何も常駐していない状態で`model=A`をリクエスト → 正しくロードされ、応答も`A`。
2. その直後に`model=B`（Aとは別の、カタログに実在するモデル）をリクエスト → **`B`はロードされず、
   `A`が黙って応答し続ける。エラーは出ない。**

`models.pool.maxResidentModels: 2`が設定済みでも、この経路では2体目に到達できない。

### 原因

`Coordinator/mlxbar/api/openai_compat.py`の`_ensure_requested_model`に、次の早期returnがあった:

```python
list_loaded = getattr(state.workers, "loaded_models", None)
if callable(list_loaded):
    residents = [item for item in list_loaded() if item]
    if len(residents) == 1:
        return residents[0]
```

これは「ちょうど1体だけ常駐していて、要求されたモデル名がどの常駐モデルのid/name/aliasにも
一致しない場合、それを『別モデルの要求』ではなく『その1体を指す別名』とみなす」という意図的な
設計だった（コメント参照）。別モデルとして自動ロードを試みると、生成中の別モデルに対して
`ENGINE_BUSY`を返してしまう既存の単一モデル運用者を壊さないための配慮。

しかし、この判定は**「ちょうど1体常駐」という状態にのみ依存し、プールが2体以上を許容する設定か
どうかを見ていなかった**。そのため:

- 常駐0体→1体: 曖昧さがなく、常に正しくロードされる。
- 常駐1体→2体を試みる: 上記の早期returnに必ず捕まり、「実在する別モデルへの切り替え」の
  意思表示であっても、本来の自動ロード経路（コメントに言う「normal resolve/autoload path」）
  へ**一度も到達しない**。
- 結果、`maxResidentModels`をいくつに設定していても、API駆動の逐次リクエストだけでは
  常駐数が1を超えることが構造的にありえなかった。

## 3. 修正内容

早期returnの条件に2つのガードを追加した:

```python
if len(residents) == 1:
    pool_settings_fn = getattr(state.workers, "_pool_settings", None)
    max_resident = 1
    if callable(pool_settings_fn):
        max_resident = int(pool_settings_fn().get("maxResidentModels", 2))
    if max_resident <= 1 or not _find_model(state, requested):
        return residents[0]
```

- `max_resident <= 1`: プールが単一モデル運用の設定なら、従来通りエイリアス扱いを維持
  （このケースでは自動ロードしても意味がなく、既存の安全策をそのまま活かす）。
- `not _find_model(state, requested)`: 要求名がカタログのどのモデルとも一致しない、
  本当に「未知の別名」である場合も、従来通りエイリアス扱いを維持する
  （`_find_model`は同ファイル内で定義済み、カタログ照合＋`loaded`等の擬似エイリアスに対応）。
- どちらにも該当しない場合（プールが2体以上を許容し、かつ要求名が実在の別モデルを指す場合）は、
  早期returnをスキップし、本来のENGINE_BUSY判定＋自動ロード経路へ進む。

## 4. 不変条件（守ったこと）

- 単一モデル運用（`maxResidentModels <= 1`、または`pool.enabled: false`相当）の挙動は完全に不変。
  既存のエイリアス吸収はそのまま機能する。
- 未知のモデル名に対する挙動は不変（`_find_model`が見つけられなければ従来通りエイリアス扱い→
  その後`MODEL_NOT_FOUND`等の既存パスに委ねられる）。
- 生成中の別モデルへ切り替えようとした場合の`ENGINE_BUSY`判定はそのまま（`_ensure_requested_model`
  の後段、本関数は変更していない）。
- 管理API・Anthropic互換APIは1行も変更していない。設定schemaも無変更。

## 5. 検証

`TEST_PLAN_v1.8.2.md`を参照。Python回帰361件（v1.8.1の360 + 新規1）。
新規テストは実際に「修正前は失敗し、修正後は成功する」ことを確認済み（`git stash`相当の
一時ロールバックで再確認）。

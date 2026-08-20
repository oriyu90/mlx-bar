# MLXBar v1.3.2

MLXBar v1.3.2 is a patch release fixing a crash reported through an external OpenAI-compatible client (ZCode): generating against an mlx-vlm model failed with `apply_chat_template requires jinja2 to be installed`. Upgrading is recommended for anyone using mlx-vlm (vision/image-capable) models — mlx-lm (text-only) models were not affected.

MLXBar v1.3.2は、外部のOpenAI互換クライアント(ZCode)経由で報告された「mlx-vlmモデルへの生成リクエストが`apply_chat_template requires jinja2 to be installed`で失敗する」問題を修正するパッチリリースです。mlx-vlm(画像対応)モデルを利用している方は更新を推奨します。mlx-lm(テキストのみ)モデルは影響を受けていません。

## Fixes

- **Generation against an mlx-vlm model failed with `apply_chat_template requires jinja2 to be installed. Please install it using pip install jinja2.`** `apply_chat_template` is ultimately handled by the Hugging Face `transformers` tokenizer/processor that `mlx_vlm.prompt_utils.apply_chat_template` ([Workers/mlx_vlm_worker/adapter.py](Workers/mlx_vlm_worker/adapter.py)) calls into, and `transformers` doesn't treat `jinja2` as a required dependency. The runtime installer ([Coordinator/mlxbar/runtimes/updater.py](Coordinator/mlxbar/runtimes/updater.py)) only pinned the engine package itself plus `fastapi`/`uvicorn` when running `uv pip install`, without `jinja2`. Comparing the actual `requirements.lock` on a real install: `mlx-lm` happened to pull `jinja2` in transitively through an unrelated dependency, but `mlx-vlm` did not — so every chat-template render on an mlx-vlm model hit the `ImportError` at generation time. The install command now pins `jinja2>=3.1,<4` explicitly for both engines. See [mlx-bar.md](mlx-bar.md) for the full investigation notes.

## 修正

- **mlx-vlmモデルへの生成リクエストが`apply_chat_template requires jinja2 to be installed. Please install it using pip install jinja2.`で失敗していました。** `apply_chat_template`は最終的に`mlx_vlm.prompt_utils.apply_chat_template`([Workers/mlx_vlm_worker/adapter.py](Workers/mlx_vlm_worker/adapter.py))が呼び出すHugging Face `transformers`のtokenizer/processorが担当しますが、`transformers`自体は`jinja2`を必須依存として扱っていません。ランタイムのインストーラー([Coordinator/mlxbar/runtimes/updater.py](Coordinator/mlxbar/runtimes/updater.py))は`uv pip install`実行時にエンジン本体と`fastapi`/`uvicorn`のみを指定しており、`jinja2`を指定していませんでした。実機の`requirements.lock`を比較したところ、`mlx-lm`はたまたま別の依存経由で`jinja2`が入っていた一方、`mlx-vlm`には全く含まれておらず、mlx-vlmモデルでチャットテンプレートを適用するたびに`ImportError`で生成が失敗していました。インストールコマンドに`jinja2>=3.1,<4`を明示追加しました。詳しい調査記録は[mlx-bar.md](mlx-bar.md)を参照してください。

## Verification / 検証結果

- 134 unit, contract, and integration tests passed (Coordinator, unchanged in this release aside from the installer fix).
- Manually verified on-device: reproduced the missing `jinja2` in an already-installed mlx-vlm runtime slot's `requirements.lock`/site-packages, confirmed `mlx-lm`'s slot had it only by transitive accident, patched the installer, and confirmed a fresh `uv pip install` with the new command includes `jinja2`.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約・結合テスト134件に成功しました(Coordinatorはインストーラー修正以外に変更なし)。
- 実機検証として、既にインストール済みのmlx-vlmランタイムスロットの`requirements.lock`/site-packagesに`jinja2`が欠落していることを再現し、`mlx-lm`側は別の依存経由で偶然入っていただけであることを確認した上でインストーラーを修正、新しいインストールコマンドで`jinja2`が確実に含まれることを確認しました。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator/CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.3.2.dmg`):

`2d285ea7723bc19976c2d7327be54170af7149b63b027f0fbf2b48284d27f9f1`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security. Existing mlx-vlm runtime installs are not automatically repaired by this update — reinstall/update the mlx-vlm runtime from the Runtimes screen (or `mlxbarctl runtime`) after upgrading to pick up `jinja2`.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。既にインストール済みのmlx-vlmランタイムは今回の更新だけでは自動修復されません。アップデート後、ランタイム画面(または`mlxbarctl runtime`)からmlx-vlmランタイムを再インストール・更新して`jinja2`を反映させてください。

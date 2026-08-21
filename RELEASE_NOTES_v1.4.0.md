# MLXBar v1.4.0

MLXBar v1.4.0 makes the first ZCode conversation after an mlx-vlm worker restart fast by persisting the stable system/tools prompt prefix to disk. It keeps full OpenAI Chat Completions compatibility and the existing in-memory cache for subsequent turns.

MLXBar v1.4.0は、mlx-vlm Worker再起動後の最初のZCode会話でも、安定したsystem/toolsプロンプトprefixをディスクから再利用して高速化します。OpenAI Chat Completions互換性と、後続ターン向けの既存RAMキャッシュはそのまま維持します。

## Highlights / 主な変更

- Adds mlx-vlm's official `APCManager` / `DiskBlockStore` as a disk-only cache tier below `PromptCacheState`.
- Reuses a large stable ZCode system/tools prefix even when the first user message changes.
- Separates cache namespaces by model path, tokenizer/template/config contents, weight metadata, mlx-vlm version, and cache format.
- Uses stable SHA-256 hashes, private 0700 cache directories, and a 5 GB default LRU limit configurable from 1 to 100 GB.
- Adds a Cache settings page for enable/disable, usage and hit statistics, and independent RAM/disk clearing.
- Records only privacy-safe `cold` / `memory` / `disk` cache tiers in API diagnostics; prompts, responses, tool definitions, and API keys remain unrecorded.
- Falls back once to the proven memory/cold path only when a disk-cache failure occurs before any response is emitted.
- Moves FastAPI/Starlette tests to `httpx2`; the deprecated compatibility warning is now treated as a test failure.

- mlx-vlm公式の`APCManager` / `DiskBlockStore`を、`PromptCacheState`の下位にあるディスク専用キャッシュ層として追加しました。
- 最初のユーザー文が変わっても、大きく安定したZCodeのsystem/tools prefixを再利用します。
- モデルパス、tokenizer/template/config内容、重みのメタデータ、mlx-vlm版、キャッシュ形式ごとにnamespaceを分離します。
- 安定SHA-256、権限0700の専用フォルダ、既定5 GB（設定範囲1〜100 GB）のLRU上限を使用します。
- 設定画面へ「キャッシュ」を追加し、有効化、使用量・hit確認、RAM／ディスクの個別消去を行えます。
- API診断には本文を保存せず、`cold` / `memory` / `disk`だけを記録します。プロンプト、応答、tool定義、APIキーは保存しません。
- ディスクキャッシュ障害が応答開始前に起きた場合だけ、実績のあるRAM／cold経路へ一度フォールバックします。
- FastAPI／Starletteテストを`httpx2`へ移行し、旧互換経路の非推奨警告をテスト失敗として検出します。

## Performance verification / 性能検証

On `Qwen3.8-27B-MLX-8bit` with a synthetic 23-tool ZCode-style prompt of 15,852 tokens:

| Path | Elapsed | Cached tokens | Prompt TPS |
| --- | ---: | ---: | ---: |
| Cold | 56.106 s | 0 | 290.96 |
| Disk hit, same question | 1.531 s | 15,596 | 12,673.68 |
| Disk hit, changed first question | 1.414 s | 15,596 | 13,761.29 |
| In-memory follow-up | 0.262 s | 15,862 | 68,704.27 |

実機`Qwen3.8-27B-MLX-8bit`へ23 toolsを含む15,852-tokenの合成ZCode入力を送り、cold 56.106秒に対してWorker再作成相当のdisk hitは1.531秒、最初の質問を変更したdisk hitは1.414秒、RAM上の後続会話は0.262秒でした。OpenAI messages／tools／tool callingの内容は省略・改変していません。

## Verification / 検証

- 164 Python unit, contract, integration, fault-injection, and network tests passed with zero deprecation warnings.
- Swift Debug and Release builds passed using Xcode 26.6.
- App signature, embedded versions/resources, LaunchAgent, source exclusions, DMG structure, and disk image integrity were verified.

- Pythonの単体・契約・結合・障害注入・ネットワークテスト164件が成功し、非推奨警告は0件でした。
- Xcode 26.6でSwift Debug／Releaseビルドが成功しました。
- アプリ署名、内蔵バージョン／資材、LaunchAgent、ソース除外、DMG構造、ディスクイメージ整合性を検証しました。

SHA-256 (`MLXBar-1.4.0.dmg`):

`ac9b5ed7c518b9fb37bb49bef127818ad15766818f3af8234c761244bafe0f75`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。

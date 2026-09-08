import SwiftUI

/// Settings > Knowledge base (RAG).
///
/// Fully bilingual: this view resolves every string through `t(_:_:)` against
/// `model.guiLanguage` rather than the `LS()` table, so runtime-interpolated
/// labels (counts, dimensions) are localized correctly too.
struct KnowledgeBaseSettingsView: View {
    @ObservedObject var model: MenuBarViewModel

    @State private var enabled = false
    @State private var baseURL = "http://127.0.0.1:1234/v1"
    @State private var embeddingModel = "text-embedding-nomic-embed-text-v1.5"
    @State private var showsToken = false
    @State private var chunkSize = 1000
    @State private var chunkOverlap = 200
    @State private var defaultTopK = 4
    @State private var maxContextChars = 6000

    @State private var newCollectionName = ""
    @State private var selectedCollection: String?
    @State private var newDocTitle = ""
    @State private var newDocText = ""
    @State private var testQuery = ""
    @State private var confirmingDeleteCollection: String?

    private var rag: [String: Any] { model.settings["rag"] as? [String: Any] ?? [:] }

    private func t(_ ja: String, _ en: String) -> String { model.guiLanguage == "ja" ? ja : en }

    var body: some View {
        Form {
            Section(t("ナレッジベース（RAG）", "Knowledge Base (RAG)")) {
                Toggle(t("ナレッジベースを有効にする", "Enable the knowledge base"), isOn: $enabled)
                Text(t("有効にすると、リクエストに rag フィールドを付けたときだけ、コレクションから関連文を検索してプロンプト先頭に注入します。rag を付けない通常のリクエストの動作は一切変わりません。",
                       "When enabled, a request that carries a `rag` field has relevant passages retrieved from a collection and prepended to the prompt. Ordinary requests without `rag` behave exactly as before."))
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section(t("埋め込みエンドポイント", "Embedding endpoint")) {
                Text(t("MLXBar自身は埋め込みを計算しません。OpenAI互換の /v1/embeddings を提供するサーバー（LM Studio、Ollama など）を指定してください。",
                       "MLXBar does not compute embeddings itself. Point this at any server that exposes an OpenAI-compatible /v1/embeddings endpoint (LM Studio, Ollama, ...)."))
                    .font(.caption).foregroundStyle(.secondary)
                TextField("Base URL", text: $baseURL)
                TextField(t("埋め込みモデル名", "Embedding model name"), text: $embeddingModel)
                HStack {
                    if showsToken {
                        TextField(t("APIキー（任意）", "API key (optional)"), text: $model.ragEmbeddingToken)
                    } else {
                        SecureField(t("APIキー（任意）", "API key (optional)"), text: $model.ragEmbeddingToken)
                    }
                    Button { showsToken.toggle() } label: {
                        Image(systemName: showsToken ? "eye.slash" : "eye")
                            .accessibilityLabel(showsToken ? t("APIキーを隠す", "Hide the API key")
                                                           : t("APIキーを表示", "Show the API key"))
                    }
                }
                HStack {
                    Button(t("接続テスト", "Test connection")) { Task { await model.testRagBackend() } }
                    Button(t("APIキーを保存", "Save API key")) {
                        Task { await model.saveRagEmbeddingToken(model.ragEmbeddingToken) }
                    }
                    Button(t("APIキーを削除", "Delete API key")) {
                        model.ragEmbeddingToken = ""
                        Task { await model.saveRagEmbeddingToken("") }
                    }.disabled(model.ragEmbeddingToken.isEmpty)
                }
                if let reachable = model.ragBackendReachable {
                    Label(reachable
                          ? t("接続OK（埋め込み次元: \(model.ragEmbeddingDim ?? 0)）",
                              "Connected (embedding dimension: \(model.ragEmbeddingDim ?? 0))")
                          : (model.ragBackendMessage
                             ?? t("埋め込みエンドポイントに接続できません", "Could not reach the embedding endpoint")),
                          systemImage: reachable ? "checkmark.circle" : "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(reachable ? .green : .orange)
                }
            }

            Section(t("チャンク設定", "Chunking")) {
                Stepper(value: $chunkSize, in: 100...8000, step: 100) {
                    Text(t("チャンクサイズ: \(chunkSize) 文字", "Chunk size: \(chunkSize) chars"))
                }
                Stepper(value: $chunkOverlap, in: 0...max(0, chunkSize / 2), step: 50) {
                    Text(t("チャンクの重なり: \(chunkOverlap) 文字", "Chunk overlap: \(chunkOverlap) chars"))
                }
                Stepper(value: $defaultTopK, in: 1...20) {
                    Text(t("既定の取得件数: \(defaultTopK)", "Default passages retrieved: \(defaultTopK)"))
                }
                Stepper(value: $maxContextChars, in: 500...32000, step: 500) {
                    Text(t("注入する文脈の最大文字数: \(maxContextChars)",
                           "Max injected context: \(maxContextChars) chars"))
                }
            }

            Section {
                Button(t("設定を保存", "Save settings")) {
                    Task {
                        await model.setRagSettings(
                            enabled: enabled, baseURL: baseURL, model: embeddingModel,
                            chunkSize: chunkSize, chunkOverlap: chunkOverlap,
                            defaultTopK: defaultTopK, maxContextChars: maxContextChars)
                    }
                }.buttonStyle(.borderedProminent)
                if let message = model.ragStatusMessage {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }
            }

            Section(t("コレクション", "Collections")) {
                if model.ragCollections.isEmpty {
                    Text(t("コレクションがありません。", "No collections yet."))
                        .font(.caption).foregroundStyle(.secondary)
                }
                ForEach(model.ragCollections) { collection in
                    HStack {
                        Button {
                            selectedCollection = collection.name
                            Task { await model.refreshRagDocuments(collection: collection.name) }
                        } label: {
                            HStack {
                                Image(systemName: selectedCollection == collection.name
                                      ? "folder.fill" : "folder")
                                Text(collection.name)
                                Spacer()
                                Text(t("\(collection.documentCount)件 / \(collection.chunkCount)チャンク",
                                       "\(collection.documentCount) docs · \(collection.chunkCount) chunks"))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }.buttonStyle(.plain)
                        Button(role: .destructive) {
                            confirmingDeleteCollection = collection.name
                        } label: { Image(systemName: "trash") }
                    }
                }
                HStack {
                    TextField(t("新しいコレクション名", "New collection name"), text: $newCollectionName)
                    Button(t("作成", "Create")) {
                        Task {
                            await model.createRagCollection(newCollectionName)
                            newCollectionName = ""
                        }
                    }.disabled(newCollectionName.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                Text(t("コレクション名は英数字で始まり、英数字・ドット・ハイフン・アンダースコアのみ（64文字以内）。",
                       "A collection name starts with an alphanumeric and uses only letters, digits, dot, hyphen and underscore (max 64 chars)."))
                    .font(.caption).foregroundStyle(.secondary)
            }

            if let collection = selectedCollection,
               model.ragCollections.contains(where: { $0.name == collection }) {
                documentSection(collection)
                querySection(collection)
            }
        }
        .padding()
        .task {
            enabled = rag["enabled"] as? Bool ?? false
            let embedding = rag["embedding"] as? [String: Any] ?? [:]
            baseURL = embedding["baseUrl"] as? String ?? baseURL
            embeddingModel = embedding["model"] as? String ?? embeddingModel
            chunkSize = (rag["chunkSize"] as? NSNumber)?.intValue ?? chunkSize
            chunkOverlap = (rag["chunkOverlap"] as? NSNumber)?.intValue ?? chunkOverlap
            defaultTopK = (rag["defaultTopK"] as? NSNumber)?.intValue ?? defaultTopK
            maxContextChars = (rag["maxContextChars"] as? NSNumber)?.intValue ?? maxContextChars
            await model.refreshRag()
        }
        .confirmationDialog(t("このコレクションを削除しますか？", "Delete this collection?"),
                            isPresented: Binding(get: { confirmingDeleteCollection != nil },
                                                 set: { if !$0 { confirmingDeleteCollection = nil } })) {
            Button(t("削除", "Delete"), role: .destructive) {
                if let name = confirmingDeleteCollection {
                    if selectedCollection == name { selectedCollection = nil }
                    Task { await model.deleteRagCollection(name) }
                }
                confirmingDeleteCollection = nil
            }
            Button(t("キャンセル", "Cancel"), role: .cancel) { confirmingDeleteCollection = nil }
        } message: {
            Text(t("コレクション内のすべてのドキュメントとチャンクが削除されます。この操作は取り消せません。",
                   "Every document and chunk in the collection is removed. This cannot be undone."))
        }
    }

    @ViewBuilder
    private func documentSection(_ collection: String) -> some View {
        Section(t("「\(collection)」のドキュメント", "Documents in “\(collection)”")) {
            ForEach(model.ragDocuments) { document in
                HStack {
                    VStack(alignment: .leading) {
                        Text(document.title.isEmpty ? t("（無題）", "(untitled)") : document.title)
                        Text(t("\(document.charCount)文字 / \(document.chunkCount)チャンク",
                               "\(document.charCount) chars · \(document.chunkCount) chunks"))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(role: .destructive) {
                        Task { await model.deleteRagDocument(collection: collection, documentId: document.id) }
                    } label: { Image(systemName: "trash") }
                }
            }
            if model.ragDocuments.isEmpty {
                Text(t("ドキュメントがありません。", "No documents yet."))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button(t("ファイルから追加…", "Add from file…")) {
                Task {
                    let selection = await FileSelectionService.shared.chooseTextFile()
                    if case let .chosen(urls) = selection, let url = urls.first {
                        await model.addRagDocumentFile(collection: collection, path: url.path)
                    }
                }
            }
            Divider()
            TextField(t("タイトル（任意）", "Title (optional)"), text: $newDocTitle)
            TextEditor(text: $newDocText)
                .frame(minHeight: 80)
                .font(.body.monospaced())
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(.quaternary))
            Button(t("テキストを追加", "Add text")) {
                Task {
                    await model.addRagDocument(collection: collection, title: newDocTitle, text: newDocText)
                    newDocTitle = ""
                    newDocText = ""
                }
            }.disabled(newDocText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
    }

    @ViewBuilder
    private func querySection(_ collection: String) -> some View {
        Section(t("検索テスト", "Query test")) {
            HStack {
                TextField(t("クエリを入力", "Enter a query"), text: $testQuery)
                Button(t("検索", "Search")) {
                    Task { await model.runRagQuery(collection: collection, query: testQuery) }
                }.disabled(testQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            ForEach(model.ragQueryResults) { hit in
                VStack(alignment: .leading, spacing: 2) {
                    Text(t("スコア \(String(format: "%.3f", hit.score))",
                           "Score \(String(format: "%.3f", hit.score))") +
                         (hit.documentTitle.isEmpty ? "" : " · \(hit.documentTitle)"))
                        .font(.caption).foregroundStyle(.secondary)
                    Text(hit.text).font(.caption).lineLimit(4)
                }
            }
        }
    }
}

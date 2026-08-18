import SwiftUI

struct ModelCatalogView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var search = ""
    @State private var source = allSourcesTag
    @State private var selected: CatalogModel.ID?
    @State private var hasLoadedCatalog = false

    /// Sentinel for the "no source filter" row; never shown to the user
    /// directly, so it stays stable across an interface language change.
    private static let allSourcesTag = "__all__"

    private var filtered: [CatalogModel] {
        model.models.filter { item in
            (search.isEmpty || item.name.localizedCaseInsensitiveContains(search)) &&
            (source == Self.allSourcesTag || item.source == source)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                TextField(LS("モデルを検索"), text: $search).textFieldStyle(.roundedBorder)
                Picker(LS("ソース"), selection: $source) {
                    Text(LS("すべて")).tag(Self.allSourcesTag)
                    ForEach(Array(Set(model.models.map(\.source))).sorted(), id: \.self) { Text($0).tag($0) }
                }.frame(width: 210)
                Button { Task { await model.scan() } } label: {
                    Label(LS("再スキャン"), systemImage: "arrow.clockwise")
                }.disabled(model.busy)
            }.padding()
            if !hasLoadedCatalog && model.models.isEmpty {
                VStack(spacing: 10) {
                    ProgressView()
                    Text(LS("モデルを読み込み中…")).font(.caption).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.models.isEmpty {
                ContentUnavailableView(LS("モデルが見つかりません"), systemImage: "shippingbox",
                                       description: Text(LS("モデルフォルダを追加してから再スキャンしてください。")))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Table(filtered, selection: $selected) {
                    TableColumn(LS("モデル")) { item in
                        VStack(alignment: .leading) { Text(item.name); Text(item.reason).font(.caption).foregroundStyle(.secondary) }
                    }
                    TableColumn(LS("形式")) { item in Text(item.format).font(.caption).padding(4).background(.quaternary, in: Capsule()) }.width(100)
                    TableColumn(LS("エンジン")) { item in Text(item.engine ?? LS("未対応")) }.width(100)
                    TableColumn(LS("ソース")) { item in Text(item.source).lineLimit(1) }.width(140)
                    TableColumn(LS("サイズ")) { item in Text(ByteCountFormatter.string(fromByteCount: item.size, countStyle: .file)) }.width(80)
                }
            }
            if let loadingName = model.loadingModelName {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(loadingName) \(LS("をロード中"))").font(.headline)
                        HStack(spacing: 6) {
                            Text(model.loadingPhase ?? LS("モデルを読み込み中"))
                            if let started = model.loadingStartedAt {
                                TimelineView(.periodic(from: .now, by: 1)) { context in
                                    Text("· \(max(0, Int(context.date.timeIntervalSince(started))))\(LS("秒経過"))")
                                }
                            }
                        }.font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                }.padding(.horizontal).padding(.top, 10)
            } else if let loadedName = model.loadedName {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Label("\(LS("ロード済み")): \(loadedName)", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                        Button(LS("モデル名をコピー")) { model.copyLoadedModelName() }
                        if let status = model.modelCopyStatus {
                            Text(status).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    Text("\(LS("モデル上限")): \(model.loadedModelMaxTokens.map(MenuBarViewModel.tokenCount) ?? LS("不明")) tokens · \(LS("API有効上限")): \(MenuBarViewModel.tokenCount(model.effectiveMaxTokens)) tokens")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(.horizontal).padding(.top, 10)
            }
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.red).lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal).padding(.top, 6)
            }
            HStack {
                Text("\(filtered.count) \(LS("モデル"))").foregroundStyle(.secondary)
                Spacer()
                Button(LS(model.loadingModelName == nil ? "ロード" : "ロード中…")) {
                    if let selected, let item = model.models.first(where: { $0.id == selected }) { Task { await model.load(item) } }
                }.disabled(selected == nil || model.busy || model.loadingModelName != nil)
            }.padding()
        }
        .navigationTitle(LS("モデル"))
        .task {
            await model.refreshModels()
            hasLoadedCatalog = true
        }
    }
}

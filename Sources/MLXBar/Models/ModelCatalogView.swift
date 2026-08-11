import SwiftUI

struct ModelCatalogView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var search = ""
    @State private var source = "すべて"
    @State private var selected: CatalogModel.ID?

    private var filtered: [CatalogModel] {
        model.models.filter { item in
            (search.isEmpty || item.name.localizedCaseInsensitiveContains(search)) &&
            (source == "すべて" || item.source == source)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("モデルを検索", text: $search).textFieldStyle(.roundedBorder)
                Picker("ソース", selection: $source) {
                    Text("すべて").tag("すべて")
                    ForEach(Array(Set(model.models.map(\.source))).sorted(), id: \.self) { Text($0).tag($0) }
                }.frame(width: 210)
                Button { Task { await model.scan() } } label: { Label("再スキャン", systemImage: "arrow.clockwise") }
            }.padding()
            Table(filtered, selection: $selected) {
                TableColumn("モデル") { item in
                    VStack(alignment: .leading) { Text(item.name); Text(item.reason).font(.caption).foregroundStyle(.secondary) }
                }
                TableColumn("形式") { item in Text(item.format).font(.caption).padding(4).background(.quaternary, in: Capsule()) }.width(100)
                TableColumn("エンジン") { item in Text(item.engine ?? "未対応") }.width(100)
                TableColumn("ソース") { item in Text(item.source).lineLimit(1) }.width(140)
                TableColumn("サイズ") { item in Text(ByteCountFormatter.string(fromByteCount: item.size, countStyle: .file)) }.width(80)
            }
            if let loadingName = model.loadingModelName {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(loadingName) をロード中").font(.headline)
                        HStack(spacing: 6) {
                            Text(model.loadingPhase ?? "モデルを読み込み中")
                            if let started = model.loadingStartedAt {
                                TimelineView(.periodic(from: .now, by: 1)) { context in
                                    Text("· \(max(0, Int(context.date.timeIntervalSince(started))))秒経過")
                                }
                            }
                        }.font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                }.padding(.horizontal).padding(.top, 10)
            } else if let loadedName = model.loadedName {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Label("ロード済み: \(loadedName)", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                        Button("モデル名をコピー") { model.copyLoadedModelName() }
                        if let status = model.modelCopyStatus {
                            Text(status).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    Text("モデル上限: \(model.loadedModelMaxTokens.map(MenuBarViewModel.tokenCount) ?? "不明") tokens · API有効上限: \(MenuBarViewModel.tokenCount(model.effectiveMaxTokens)) tokens")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(.horizontal).padding(.top, 10)
            }
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.red).lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal).padding(.top, 6)
            }
            HStack {
                Text("\(filtered.count)モデル").foregroundStyle(.secondary)
                Spacer()
                Button(model.loadingModelName == nil ? "ロード" : "ロード中…") {
                    if let selected, let item = model.models.first(where: { $0.id == selected }) { Task { await model.load(item) } }
                }.disabled(selected == nil || model.busy)
            }.padding()
        }
        .navigationTitle("モデル")
        .task { await model.refreshModels() }
    }
}

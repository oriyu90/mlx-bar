import SwiftUI
import UniformTypeIdentifiers

struct QuickChatView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var prompt = ""
    @State private var temperature = 0.7
    @State private var maxTokens = 512
    @State private var images: [URL] = []
    @State private var dropTarget = false
    @State private var isSelectingImages = false

    var body: some View {
        VStack(spacing: 12) {
            Label(model.loadedName ?? LS("モデル未選択"), systemImage: "cpu")
                .lineLimit(1).truncationMode(.middle)
            HStack {
                Text("Temperature \(temperature, specifier: "%.1f")")
                Slider(value: $temperature, in: 0...2, step: 0.1).frame(minWidth: 100, idealWidth: 120)
                    .accessibilityLabel("Temperature")
                Spacer(minLength: 12)
                Stepper(LS("最大 ") + "\(maxTokens)", value: $maxTokens,
                        in: 1...max(1, model.effectiveMaxTokens), step: 128)
                    .fixedSize()
            }
            ScrollView {
                Text(model.chatOutput.isEmpty ? LS("ここに生成結果が表示されます") : model.chatOutput)
                    .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled).padding()
                    .foregroundStyle(model.chatOutput.isEmpty ? .secondary : .primary)
            }.background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            if !images.isEmpty {
                ScrollView(.horizontal) { HStack { ForEach(images, id: \.self) { url in Label(url.lastPathComponent, systemImage: "photo") } } }
            }
            if let cancellationStatus = model.cancellationStatus {
                Label(cancellationStatus, systemImage: model.cancellationInProgress ? "hourglass" : "stop.circle")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.red).lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            TextEditor(text: $prompt).frame(height: 90).overlay(RoundedRectangle(cornerRadius: 6).stroke(dropTarget ? Color.accentColor : Color(nsColor: .separatorColor)))
                .accessibilityLabel(LS("プロンプト"))
                .onDrop(of: [UTType.image.identifier, UTType.fileURL.identifier], isTargeted: $dropTarget) { providers in
                    for provider in providers {
                        _ = provider.loadObject(ofClass: URL.self) { url, _ in if let url { Task { @MainActor in images.append(url) } } }
                    }; return true
                }
            HStack {
                Button(LS("画像を追加")) { chooseImages() }
                    .disabled(!model.loadedModalities.contains("image") || isSelectingImages)
                Button(LS("会話をクリア")) { model.chatOutput = ""; prompt = ""; images = [] }
                Spacer()
                if model.currentRequestID != nil {
                    Button(LS(model.cancellationInProgress ? "停止処理中…" : "停止")) {
                        Task { await model.cancelGeneration() }
                    }.tint(.red).disabled(model.cancellationInProgress)
                }
                Button(LS(model.busy ? "生成中…" : "送信")) {
                    Task { await model.generate(prompt: prompt, images: images, temperature: temperature, maxTokens: maxTokens) }
                }.buttonStyle(.borderedProminent).disabled(model.busy || prompt.isEmpty || model.loadedName == nil)
            }
        }
        .padding()
        .onAppear { NSApplication.shared.activate(ignoringOtherApps: true) }
        .task {
            await model.refreshSettings()
            temperature = model.configuredTemperature
        }
        .onChange(of: model.effectiveMaxTokens) { _, value in maxTokens = min(maxTokens, max(1, value)) }
    }

    private func chooseImages() {
        isSelectingImages = true
        Task { @MainActor in
            defer { isSelectingImages = false }
            switch await FileSelectionService.shared.chooseImages() {
            case .chosen(let urls):
                images.append(contentsOf: urls)
            case .busy:
                model.errorMessage = LS("別のファイル選択画面が開いています。先にそちらを閉じてください。")
            case .cancelled:
                break
            }
        }
    }
}

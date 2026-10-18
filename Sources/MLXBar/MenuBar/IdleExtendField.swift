import SwiftUI

/// The "+N seconds" field under one loaded-model row (v2.8.0).
///
/// Only shown for models the coordinator will actually unload for idleness --
/// a pinned, keep-loaded or externally managed model has no deadline to push
/// back, so the control is hidden instead of shown-and-rejected.
///
/// Two deliberate choices:
///
/// * `@State` lives here rather than in a `Binding(get:set:)` on the view
///   model. The status poll rebuilds every row once a second; a binding whose
///   setter fired on that rebuild would re-submit the request forever. Text is
///   submitted by the Return key or the button, never on every keystroke.
/// * `.autocorrectionDisabled()` keeps the value parseable. A corrected
///   "600" must not become "6OO".
struct IdleExtendField: View {
    let resident: ResidentModel
    let isBusy: Bool
    let onExtend: (Int) async -> Void

    @State private var seconds: String = ""
    @FocusState private var isFocused: Bool

    /// `nil` when this model is not idle-managed, or `isBusy`.
    private var isAvailable: Bool { resident.isIdleManaged && !isBusy }

    private var parsedSeconds: Int? {
        let trimmed = seconds.trimmingCharacters(in: .whitespaces)
        guard let value = Int(trimmed), value > 0 else { return nil }
        return value
    }

    var body: some View {
        // A pinned / externally managed model has no idle deadline; say why the
        // control is missing rather than leaving a silent gap in the row.
        if resident.isIdleManaged {
            HStack(spacing: 4) {
                TextField(LS("加算秒数"), text: $seconds)
                    .textFieldStyle(.roundedBorder)
                    .font(.caption2)
                    .monospacedDigit()
                    .multilineTextAlignment(.trailing)
                    .frame(width: 62)
                    .focused($isFocused)
                    .disabled(!isAvailable)
                    // A corrected "600" must not become "6OO". There is no
                    // `textInputAutocapitalization` on macOS, but digits cannot
                    // be autocapitalised either way.
                    .autocorrectionDisabled()
                    .onSubmit(submit)
                    .accessibilityLabel(LS("解放までの時間を加算する秒数"))
                    .accessibilityHint(LS("入力した秒数だけ、このモデルの解放までの待機時間を延長します"))

                Button(LS("延長"), action: submit)
                    .font(.caption2)
                    .buttonStyle(.borderless)
                    .disabled(!isAvailable || parsedSeconds == nil)
                    .accessibilityHint(LS("入力した秒数だけ、このモデルの解放までの待機時間を延長します"))

                if let extended = resident.idleExtendSeconds, extended > 0 {
                    Text(LS("加算済み") + " +\(extended)" + LS("秒"))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                        .accessibilityLabel(LS("加算済み") + " \(extended)" + LS("秒"))
                }
                Spacer(minLength: 0)
            }
            // Returning focus here keeps the keyboard from staying on a field
            // the user is done with, which in a MenuBarExtra popover would
            // swallow the next keystroke meant for the menu.
            .onChange(of: resident.idleSecondsRemaining) { _, _ in
                if !isFocused { seconds = "" }
            }
        }
    }

    private func submit() {
        guard let value = parsedSeconds, isAvailable else { return }
        seconds = ""
        isFocused = false
        Task { await onExtend(value) }
    }
}
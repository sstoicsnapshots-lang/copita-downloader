import SwiftUI

struct TaskRowView: View {
    let task: CopitaTask
    @ObservedObject var store: CopitaStore
    var onPlay: (CopitaTask) -> Void = { _ in }
    @State private var showDetails = false
    @State private var isHovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 12) {
                FileIconView(task: task)
                VStack(alignment: .leading, spacing: 4) {
                    Text(task.title)
                        .font(.system(size: 13, weight: .semibold))
                        .lineLimit(1)
                    statusLine
                    if task.status == "downloading", task.total != nil {
                        ProgressView(value: task.percent, total: 100)
                            .progressViewStyle(.linear)
                            .tint(CopitaTheme.accent)
                    } else if task.status == "downloading" || ["analyzing", "merging", "compressing", "extracting"].contains(task.status) {
                        // Unknown total (e.g. a live stream recorded via
                        // ffmpeg) means percent is meaningless — a determinate
                        // bar frozen at 0% reads as stuck, not as "no total
                        // yet," so this falls back to the same indeterminate
                        // treatment as the other unknown-length phases.
                        // A bare ProgressView() defaults to a large circular
                        // spinner on macOS — needs .linear explicitly for
                        // the slim indeterminate bar matching the rest of
                        // the row (caught live: it rendered as an oversized
                        // spinner blowing out the row's height).
                        ProgressView()
                            .progressViewStyle(.linear)
                            .tint(CopitaTheme.accent)
                    }
                    if task.speed > 0 {
                        Text("\(formatBytes(Int64(task.speed)))/s\(task.eta.map { " · \(Int($0))s remaining" } ?? "")")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer(minLength: 8)
                actions
            }

            if task.status == "failed", let details = task.errorDetails, !details.isEmpty {
                DisclosureGroup("Technical details", isExpanded: $showDetails) {
                    Text(details)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .padding(.top, 4)
                }
                .font(.system(size: 11.5))
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: CopitaTheme.cardRadius)
                .fill(Color.primary.opacity(isHovering ? 0.055 : 0.03))
        )
        .animation(.easeOut(duration: 0.12), value: isHovering)
        .onHover { isHovering = $0 }
        .contextMenu { contextMenuItems }
    }

    @ViewBuilder
    private var contextMenuItems: some View {
        switch task.status {
        case "completed":
            if isPreviewable, task.outputPath != nil {
                Button(previewActionLabel) { onPlay(task) }
            }
            Button("Show in Finder") { store.revealInFinder(taskId: task.id) }
            Divider()
            Button("Remove") { store.remove(task.id) }
        case "failed":
            Button("Retry") { store.retry(task.id) }
            Divider()
            Button("Remove") { store.remove(task.id) }
        case "downloading":
            if task.canPause {
                Button("Pause") { store.pause(task.id) }
            }
            Button("Cancel") { store.cancel(task.id) }
        case "paused":
            Button("Resume") { store.resume(task.id) }
            Button("Cancel") { store.cancel(task.id) }
        case "queued":
            Button("Cancel") { store.cancel(task.id) }
        case "cancelled":
            Button("Retry") { store.retry(task.id) }
            Divider()
            Button("Remove") { store.remove(task.id) }
        default:
            EmptyView()
        }
    }

    @ViewBuilder
    private var statusLine: some View {
        switch task.status {
        case "failed":
            Text(task.error ?? "Download interrupted")
                .font(.system(size: 11.5))
                .foregroundStyle(.red)
                .lineLimit(2)
        case "completed":
            Text(formatBytes(task.downloaded))
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        case "queued":
            Text("Waiting for another download to finish…")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        case "analyzing":
            Text(task.engine == "torrent" ? "Connecting to peers…" : "Scanning…")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        // Without these, a task sitting at "merging" (ffmpeg remux, etc.)
        // fell through to the default percent/bytes text below, which
        // reads as stuck at ~100% with no explanation — this is exactly
        // the multi-second gap between "looks finished" and the
        // Play/Show-in-Finder buttons actually appearing.
        case "merging":
            Text("Finishing…")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        case "compressing":
            Text("Packaging…")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        case "extracting":
            Text("Extracting…")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        // Missing case: a cancelled task fell through to the same
        // percent/bytes text as an active download (e.g. "0% · 11.8 MB"),
        // reading as broken rather than as a deliberately stopped task —
        // and with no dedicated actions case either (below), it became a
        // permanent zombie row with no way to remove or retry it.
        case "cancelled":
            Text("Cancelled")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        default:
            if task.status == "downloading", task.total == nil {
                // No known total means no honest percent to show (a live
                // stream has no end) — the growing byte count is the only
                // real signal, so lead with that instead of a "0%" that
                // never moves.
                Text("\(formatBytes(task.downloaded)) downloaded")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
            } else {
                let totalPart = task.total.map { " of \(formatBytes($0))" } ?? ""
                Text("\(Int(task.percent))% · \(formatBytes(task.downloaded))\(totalPart)")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var previewKind: PreviewKind { PreviewKind.from(filename: task.filename) }
    private var isPreviewable: Bool { previewKind != .none }

    // "Play" only makes sense for actual media — a PDF or image button
    // labeled "Play" reads wrong even though the same onPlay(task) action
    // (open the native preview window) is correct for all of them.
    private var previewActionLabel: String {
        switch previewKind {
        case .video, .audio: return "Play"
        case .pdf, .image: return "Preview"
        case .none: return "Open"
        }
    }

    @ViewBuilder
    private var actions: some View {
        HStack(spacing: 6) {
            switch task.status {
            case "completed":
                if isPreviewable, task.outputPath != nil {
                    Button(previewActionLabel) { onPlay(task) }
                        .tint(.green)
                }
                Button("Show in Finder") { store.revealInFinder(taskId: task.id) }
                Button("Remove") { store.remove(task.id) }
            case "failed":
                Button("Retry") { store.retry(task.id) }
                Button("Remove") { store.remove(task.id) }
            case "downloading":
                if task.canPause {
                    Button("Pause") { store.pause(task.id) }
                }
                Button("Cancel") { store.cancel(task.id) }
            case "paused":
                Button("Resume") { store.resume(task.id) }
                Button("Cancel") { store.cancel(task.id) }
            case "queued":
                Button("Cancel") { store.cancel(task.id) }
            case "cancelled":
                Button("Retry") { store.retry(task.id) }
                Button("Remove") { store.remove(task.id) }
            default:
                EmptyView()
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
    }
}

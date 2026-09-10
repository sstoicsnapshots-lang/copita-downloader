import SwiftUI

/// One collapsible row standing in for a whole expanded playlist:
/// "<playlist> — 7 of 20", a combined progress bar and speed, and group
/// actions. Tapping it reveals the individual episode rows.
struct GroupRowView: View {
    let bundle: PlaylistBundle
    @ObservedObject var store: CopitaStore
    var onPlay: (CopitaTask) -> Void = { _ in }

    @Binding var expanded: Bool
    @State private var isHovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .contentShape(Rectangle())
                .onTapGesture { withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() } }

            if expanded {
                VStack(spacing: CopitaTheme.rowSpacing) {
                    ForEach(bundle.children) { child in
                        TaskRowView(task: child, store: store, onPlay: onPlay)
                    }
                }
                .padding(.top, CopitaTheme.rowSpacing)
                .padding(.leading, 14)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: CopitaTheme.cardRadius)
                .fill(Color.primary.opacity(isHovering ? 0.06 : 0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: CopitaTheme.cardRadius)
                .strokeBorder(CopitaTheme.accent.opacity(bundle.activeCount > 0 ? 0.18 : 0), lineWidth: 1)
        )
        .animation(.easeOut(duration: 0.12), value: isHovering)
        .onHover { isHovering = $0 }
        .contextMenu { menuItems }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 8)
                    .fill(CopitaTheme.accent.opacity(0.14))
                    .frame(width: 34, height: 34)
                Image(systemName: "square.stack.3d.up.fill")
                    .font(.system(size: 15))
                    .foregroundStyle(CopitaTheme.accent)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(bundle.title)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)

                Text(subtitle)
                    .font(.system(size: 11.5))
                    .foregroundStyle(bundle.failed > 0 ? .red : .secondary)

                if !bundle.allComplete {
                    ProgressView(value: bundle.percent, total: 100)
                        .progressViewStyle(.linear)
                        .tint(CopitaTheme.accent)
                        .padding(.top, 1)
                }

                if bundle.combinedSpeed > 0 {
                    Text("\(formatBytes(Int64(bundle.combinedSpeed)))/s · \(bundle.activeCount) downloading")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 8)

            HStack(spacing: 6) {
                groupActions
                Image(systemName: expanded ? "chevron.up" : "chevron.down")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 16)
            }
        }
    }

    private var subtitle: String {
        var parts = ["\(bundle.done) of \(bundle.total) done"]
        if bundle.failed > 0 { parts.append("\(bundle.failed) failed") }
        else if bundle.activeCount > 0 { parts.append("\(bundle.activeCount) downloading") }
        else if bundle.queuedCount > 0 { parts.append("\(bundle.queuedCount) queued") }
        else if bundle.pausedCount > 0 { parts.append("\(bundle.pausedCount) paused") }
        if let t = bundle.combinedTotal, !bundle.allComplete {
            parts.append("\(formatBytes(bundle.combinedDownloaded)) / \(formatBytes(t))")
        }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var groupActions: some View {
        HStack(spacing: 6) {
            if bundle.failed > 0 {
                Button("Retry Failed") {
                    for c in bundle.children where c.status == "failed" { store.retry(c.id) }
                }
            }
            if bundle.activeCount > 0 || bundle.queuedCount > 0 || bundle.pausedCount > 0 {
                Button("Cancel All") {
                    for c in bundle.children where DownloadStatus.nonTerminal.contains(c.status) {
                        store.cancel(c.id)
                    }
                }
            } else if bundle.allTerminal {
                Button("Remove All") {
                    for id in bundle.childIDs { store.remove(id) }
                }
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
    }

    @ViewBuilder
    private var menuItems: some View {
        Button(expanded ? "Collapse" : "Expand") { expanded.toggle() }
        Divider()
        if bundle.failed > 0 {
            Button("Retry \(bundle.failed) Failed") {
                for c in bundle.children where c.status == "failed" { store.retry(c.id) }
            }
        }
        if bundle.activeCount > 0 || bundle.queuedCount > 0 || bundle.pausedCount > 0 {
            Button("Cancel All") {
                for c in bundle.children where DownloadStatus.nonTerminal.contains(c.status) {
                    store.cancel(c.id)
                }
            }
        }
        if bundle.allComplete {
            Button("Show in Finder") {
                if let first = bundle.children.first { store.revealInFinder(taskId: first.id) }
            }
        }
        if bundle.allTerminal {
            Button("Remove All") { for id in bundle.childIDs { store.remove(id) } }
        }
    }
}

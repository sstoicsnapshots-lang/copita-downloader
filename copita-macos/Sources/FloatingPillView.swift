import SwiftUI

struct FloatingPillView: View {
    @ObservedObject var store: CopitaStore

    private var activeTasks: [CopitaTask] {
        store.tasks.filter { $0.status == "downloading" || $0.status == "analyzing" || $0.status == "merging" }
    }
    private var pausedTasks: [CopitaTask] { store.tasks.filter { $0.status == "paused" } }
    private var completedTasks: [CopitaTask] { store.tasks.filter { $0.status == "completed" } }
    private var pausableActive: [CopitaTask] { activeTasks.filter { $0.canPause } }
    // Only worth showing the pause control when there's actually something it
    // can act on — yt-dlp / HLS-remux downloads aren't pausable and the
    // button just did nothing before.
    private var showsPauseControl: Bool { !pausableActive.isEmpty || !pausedTasks.isEmpty }

    private var combinedSpeed: Double { activeTasks.reduce(0) { $0 + $1.speed } }
    private var averagePercent: Double {
        activeTasks.isEmpty ? 0 : activeTasks.reduce(0) { $0 + $1.percent } / Double(activeTasks.count)
    }

    var body: some View {
        if !activeTasks.isEmpty || !pausedTasks.isEmpty {
            HStack(spacing: 14) {
                Image(systemName: "arrow.down.circle.fill")
                    .font(.system(size: 22))
                    .foregroundStyle(CopitaTheme.accent)

                VStack(alignment: .leading, spacing: 2) {
                    Text(titleText)
                        .font(.system(size: 12.5, weight: .semibold))
                        .lineLimit(1)
                    Text(subtitleText)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                }

                Spacer(minLength: 12)

                if !activeTasks.isEmpty {
                    ProgressView(value: averagePercent, total: 100)
                        .frame(width: 100)
                        .tint(CopitaTheme.accent)
                }

                if showsPauseControl {
                    Button {
                        if pausableActive.isEmpty {
                            pausedTasks.forEach { store.resume($0.id) }
                        } else {
                            pausableActive.forEach { store.pause($0.id) }
                        }
                    } label: {
                        Image(systemName: pausableActive.isEmpty ? "play.fill" : "pause.fill")
                            .font(.system(size: 11))
                            .frame(width: 26, height: 26)
                            .background(Circle().fill(Color.primary.opacity(0.85)))
                            .foregroundStyle(Color(nsColor: .windowBackgroundColor))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 18)
            .frame(width: CopitaTheme.pillWidth, height: CopitaTheme.pillHeight)
            .background(VisualEffectView(material: .hudWindow, blendingMode: .withinWindow))
            .clipShape(RoundedRectangle(cornerRadius: CopitaTheme.pillRadius))
            .shadow(color: .black.opacity(0.25), radius: 16, y: 6)
            .padding(.bottom, 18)
        }
    }

    private var titleText: String {
        if let first = activeTasks.first { return first.title }
        if let first = pausedTasks.first { return first.title }
        return "Copita Downloader"
    }

    private var subtitleText: String {
        if !activeTasks.isEmpty {
            let speedText = combinedSpeed > 0 ? "\(formatBytes(Int64(combinedSpeed)))/s" : "0 B/s"
            return "\(activeTasks.count) active · \(speedText)"
        }
        if !pausedTasks.isEmpty {
            return "\(pausedTasks.count) paused"
        }
        return "Ready"
    }
}

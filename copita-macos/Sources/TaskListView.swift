import SwiftUI
import UniformTypeIdentifiers

struct TaskListView: View {
    @ObservedObject var store: CopitaStore
    var onPlay: (CopitaTask) -> Void = { _ in }
    @State private var urlText: String = ""
    @State private var isDropTargeted = false
    @State private var expandedGroups: Set<String> = []
    @FocusState private var urlFieldFocused: Bool

    private var filter: SidebarFilter { store.selectedFilter }
    private var tasks: [CopitaTask] { store.tasks(for: filter, search: store.searchQuery) }
    private var listItems: [TaskListItem] { groupTaskList(tasks) }

    var body: some View {
        VStack(spacing: 0) {
            header
                .padding(CopitaTheme.contentPadding)

            if tasks.isEmpty {
                emptyState
            } else {
                ScrollView {
                    LazyVStack(spacing: CopitaTheme.rowSpacing) {
                        ForEach(listItems) { item in
                            switch item {
                            case .single(let task):
                                TaskRowView(task: task, store: store, onPlay: onPlay)
                            case .group(let bundle):
                                GroupRowView(
                                    bundle: bundle,
                                    store: store,
                                    onPlay: onPlay,
                                    expanded: bindingForGroup(bundle.id)
                                )
                            }
                        }
                    }
                    .padding(.horizontal, CopitaTheme.contentPadding)
                    .padding(.bottom, 90) // room for the floating pill
                }
            }
        }
        .navigationTitle(filter.label)
        .onDrop(of: [.url, .plainText], isTargeted: $isDropTargeted, perform: handleDrop)
        .background(
            // Cmd+L to jump straight to the paste field, browser-address-
            // bar style — hidden since it's a keyboard-only affordance.
            Button("") { urlFieldFocused = true }
                .keyboardShortcut("l", modifiers: .command)
                .hidden()
        )
    }

    @ViewBuilder
    private var header: some View {
        switch filter {
        case .active:
            activeHeader
        case .completed:
            completedHeader
        default:
            urlInputHeader
        }
    }

    private var urlInputHeader: some View {
        HStack(spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "link")
                    .foregroundStyle(.secondary)
                TextField(filter.urlPlaceholder ?? "Paste a download link", text: $urlText)
                    .textFieldStyle(.plain)
                    .focused($urlFieldFocused)
                    .onSubmit(submit)
            }
            .padding(.horizontal, 12)
            .frame(height: CopitaTheme.inputBarHeight)
            .background(RoundedRectangle(cornerRadius: CopitaTheme.controlRadius + 1).fill(Color.primary.opacity(0.05)))

            Button("Paste") {
                if let clip = NSPasteboard.general.string(forType: .string) {
                    urlText = clip.trimmingCharacters(in: .whitespacesAndNewlines)
                }
            }
            .buttonStyle(.bordered)
            .frame(height: CopitaTheme.inputBarHeight)

            Button(filter.primaryButtonLabel, action: submit)
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .tint(CopitaTheme.accent)
                .frame(height: CopitaTheme.inputBarHeight)
        }
        .background(isDropTargeted ? CopitaTheme.accent.opacity(0.08) : Color.clear)
    }

    private var activeHeader: some View {
        VStack(spacing: 14) {
            HStack(spacing: 12) {
                statCard(title: "Active Transfers", value: "\(activeCount)")
                statCard(title: "Reported Speed", value: speedAvailable ? "\(formatBytes(Int64(combinedSpeed)))/s" : "Not available")
                statCard(title: "Paused", value: "\(pausedCount)")
            }
            HStack(spacing: 10) {
                Button("Pause Queue") { store.pauseAllQueue() }
                Button("Resume Queue") { store.resumeAllQueue() }
                Button("Clear Completed") { store.clearCompleted() }
                Spacer()
            }
            .buttonStyle(.bordered)
        }
    }

    private var completedHeader: some View {
        HStack {
            Button("Open in Finder") { store.openDownloadsFolder() }
            Button("Clear History") { store.clearCompleted() }
            Spacer()
        }
        .buttonStyle(.bordered)
    }

    private func statCard(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(value)
                .font(.system(size: 20, weight: .bold, design: .monospaced))
            Text(title.uppercased())
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(RoundedRectangle(cornerRadius: CopitaTheme.cardRadius).fill(Color.primary.opacity(0.04)))
    }

    private var activeCount: Int {
        store.tasks.filter { DownloadStatus.nonTerminal.contains($0.status) && $0.status != "paused" }.count
    }
    private var pausedCount: Int { store.tasks.filter { $0.status == "paused" }.count }
    private var speedAvailable: Bool { store.tasks.contains { $0.speedAvailable && $0.speed > 0 } }
    private var combinedSpeed: Double {
        store.tasks.reduce(0) { total, task in task.status == "downloading" ? total + task.speed : total }
    }

    // A search that matches nothing needs to say so specifically — showing
    // the tab's generic "No Downloads Yet" copy here would be actively
    // misleading when there ARE downloads, just none matching the query.
    private var isSearching: Bool {
        !store.searchQuery.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: isSearching ? "magnifyingglass" : filter.symbol)
                .font(.system(size: 36))
                .foregroundStyle(.secondary)
            Text(isSearching ? "No Results" : filter.emptyTitle)
                .font(.system(size: 16, weight: .semibold))
            Text(isSearching ? "No downloads match \u{201C}\(store.searchQuery)\u{201D}." : filter.emptySubtitle)
                .font(.system(size: 13))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 320)
            if isSearching {
                Button("Clear Search") { store.searchQuery = "" }
                    .buttonStyle(.bordered)
                    .padding(.top, 4)
            } else if filter.urlPlaceholder != nil {
                Button("Paste from Clipboard") {
                    if let clip = NSPasteboard.general.string(forType: .string) {
                        urlText = clip.trimmingCharacters(in: .whitespacesAndNewlines)
                        submit()
                    }
                }
                .buttonStyle(.bordered)
                .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func bindingForGroup(_ id: String) -> Binding<Bool> {
        Binding(
            get: { expandedGroups.contains(id) },
            set: { isOn in
                if isOn { expandedGroups.insert(id) } else { expandedGroups.remove(id) }
            }
        )
    }

    private func submit() {
        let trimmed = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        store.download(url: trimmed, audioOnly: filter == .audio)
        urlText = ""
    }

    private func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first else { return false }
        if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
            _ = provider.loadObject(ofClass: URL.self) { url, _ in
                guard let url else { return }
                DispatchQueue.main.async { store.download(url: url.absoluteString, audioOnly: filter == .audio) }
            }
            return true
        }
        if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) {
            provider.loadItem(forTypeIdentifier: UTType.plainText.identifier, options: nil) { item, _ in
                var text: String?
                if let s = item as? String { text = s }
                else if let d = item as? Data { text = String(data: d, encoding: .utf8) }
                guard let text, text.hasPrefix("http") else { return }
                DispatchQueue.main.async { store.download(url: text, audioOnly: filter == .audio) }
            }
            return true
        }
        return false
    }
}

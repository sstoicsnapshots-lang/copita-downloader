import SwiftUI

struct ContentView: View {
    @ObservedObject var store: CopitaStore
    var onOpenSettings: () -> Void = {}
    var onPlay: (CopitaTask) -> Void = { _ in }

    var body: some View {
        NavigationSplitView {
            SidebarView(store: store, onOpenSettings: onOpenSettings)
                .navigationSplitViewColumnWidth(CopitaTheme.sidebarWidth)
        } detail: {
            ZStack(alignment: .bottom) {
                VStack(spacing: 0) {
                    if let banner = store.statusBanner {
                        HStack(spacing: 8) {
                            if banner.hasPrefix("Updating") {
                                ProgressView().controlSize(.small)
                            } else {
                                Image(systemName: (banner.contains("Couldn't") || banner.contains("didn't"))
                                      ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                                    .foregroundStyle(banner.contains("Couldn't") ? .orange : CopitaTheme.accent)
                            }
                            Text(banner).font(.system(size: 12, weight: .medium))
                            Spacer()
                            Button { store.statusBanner = nil } label: {
                                Image(systemName: "xmark").font(.system(size: 10))
                            }.buttonStyle(.plain).foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(.thinMaterial)
                        .transition(.move(edge: .top).combined(with: .opacity))
                    }
                    TaskListView(store: store, onPlay: onPlay)
                }
                .animation(.easeInOut(duration: 0.2), value: store.statusBanner)
                FloatingPillView(store: store)
            }
        }
        .onAppear {
            store.connect()
            store.refreshOnce()
            // Previously only fetched when the Settings window happened to
            // be opened -- the main window's disk-space footer sat on
            // "-- GB available" for the entire session otherwise (caught
            // live: a fresh launch never populated it since the user
            // hadn't opened Settings yet).
            store.fetchSystemInfo()
        }
    }
}

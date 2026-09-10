import SwiftUI

struct SidebarView: View {
    @ObservedObject var store: CopitaStore
    var onOpenSettings: () -> Void

    private let primaryFilters: [SidebarFilter] = [.all, .active, .completed]
    private let libraryFilters: [SidebarFilter] = [.videos, .audio, .manga, .files]

    var body: some View {
        VStack(spacing: 0) {
            searchField
                .padding(.horizontal, 12)
                .padding(.top, 12)
                .padding(.bottom, 8)

            List(selection: $store.selectedFilter) {
                Section {
                    ForEach(primaryFilters) { filter in
                        row(for: filter)
                    }
                }
                Section("Library") {
                    ForEach(libraryFilters) { filter in
                        row(for: filter)
                    }
                }
            }
            .listStyle(.sidebar)
            .scrollContentBackground(.hidden)
            .tint(CopitaTheme.accent)

            Divider()
            footer
        }
        .background(VisualEffectView(material: .sidebar))
        .onAppear { store.fetchSystemInfo() }
    }

    private var searchField: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            TextField("Search downloads", text: $store.searchQuery)
                .textFieldStyle(.plain)
                .font(.system(size: 12))
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.06)))
    }

    private func row(for filter: SidebarFilter) -> some View {
        Label {
            HStack {
                Text(filter.label)
                Spacer()
                let count = store.count(for: filter)
                if count > 0 {
                    Text("\(count)")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(filter == .active ? .white : .secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(
                            Capsule().fill(filter == .active ? CopitaTheme.accent : Color.primary.opacity(0.08))
                        )
                }
            }
        } icon: {
            Image(systemName: filter.symbol)
        }
        .tag(filter)
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Button {
                store.openDownloadsFolder()
            } label: {
                Image(systemName: "folder.fill")
                    .foregroundStyle(CopitaTheme.accent)
                    .frame(width: 30, height: 30)
                    .background(RoundedRectangle(cornerRadius: 7).fill(CopitaTheme.accent.opacity(0.12)))
            }
            .buttonStyle(.plain)

            VStack(alignment: .leading, spacing: 1) {
                Text("Copita Downloads")
                    .font(.system(size: 11.5, weight: .medium))
                if let info = store.systemInfo {
                    Text("\(String(format: "%.1f", info.diskFreeGb)) GB available")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                } else {
                    Text("-- GB available")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()

            Button(action: onOpenSettings) {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }
}

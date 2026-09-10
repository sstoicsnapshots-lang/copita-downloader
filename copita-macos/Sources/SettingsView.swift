import SwiftUI

struct SettingsView: View {
    @ObservedObject var store: CopitaStore

    var body: some View {
        TabView {
            GeneralSettingsTab(store: store)
                .tabItem { Label("General", systemImage: "gearshape") }
            SupportedSitesTab()
                .tabItem { Label("Supported Sites", systemImage: "globe") }
            ConnectionTab(store: store)
                .tabItem { Label("Connection", systemImage: "checkmark.circle") }
            IntegrationsTab()
                .tabItem { Label("Integrations", systemImage: "puzzlepiece.extension") }
        }
        .frame(width: 600, height: 620)
        .onAppear {
            store.fetchSystemInfo()
            store.fetchRequirements()
        }
    }
}

// MARK: - General

private struct GeneralSettingsTab: View {
    @ObservedObject var store: CopitaStore

    @State private var threads = 16
    @State private var maxConcurrent = 3
    @State private var cookiesBrowser = "auto"
    @State private var speedLimit = 0          // KB/s, 0 = unlimited
    @State private var proxyUrl = ""
    @State private var verifySsl = true
    @AppStorage("copita.videoQuality") private var videoQuality = "Best available"
    @AppStorage("copita.audioFormat") private var audioFormat = "MP3"

    // tag = the name the backend / yt-dlp expects (NOT the display string —
    // the old picker sent "Google Chrome" which never matched anything).
    private let browserOptions: [(String, String)] = [
        ("auto", "Automatic (your default browser)"),
        ("", "Off"),
        ("chrome", "Google Chrome"), ("brave", "Brave"), ("safari", "Safari"),
        ("firefox", "Firefox"), ("edge", "Microsoft Edge"), ("vivaldi", "Vivaldi"),
    ]

    var body: some View {
        Form {
            Section("Downloads Location & Storage") {
                HStack {
                    Text(store.systemInfo?.downloadDir ?? "—")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    Button("Open in Finder") { store.openDownloadsFolder() }
                }
                if let info = store.systemInfo {
                    storageBar(info: info)
                }
            }

            Section("Downloads") {
                Picker("Download connections", selection: $threads) {
                    Text("8 connections").tag(8)
                    Text("16 connections (Default)").tag(16)
                    Text("32 connections").tag(32)
                }
                Text("Used for direct files and streams. More connections may help on faster networks.")
                    .font(.caption).foregroundStyle(.secondary)

                Picker("Simultaneous downloads", selection: $maxConcurrent) {
                    Text("1 at a time").tag(1)
                    Text("2 at a time").tag(2)
                    Text("3 at a time (Default)").tag(3)
                    Text("5 at a time").tag(5)
                    Text("10 at a time").tag(10)
                }
                Text("Extra downloads wait in queue rather than all starting at once.")
                    .font(.caption).foregroundStyle(.secondary)

                Picker("Browser session", selection: $cookiesBrowser) {
                    ForEach(browserOptions, id: \.0) { Text($0.1).tag($0.0) }
                }
                Text("Signs downloads in with your browser session — needed for account-only content, and it also raises YouTube's rate limits a lot. \"Automatic\" uses whichever browser you last used.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section("Network") {
                Picker("Speed limit", selection: $speedLimit) {
                    Text("Unlimited").tag(0)
                    Text("500 KB/s").tag(500)
                    Text("1 MB/s").tag(1024)
                    Text("2 MB/s").tag(2048)
                    Text("5 MB/s").tag(5120)
                    Text("10 MB/s").tag(10240)
                }
                Text("Caps the combined speed of all active downloads.")
                    .font(.caption).foregroundStyle(.secondary)

                TextField("Proxy", text: $proxyUrl, prompt: Text("http://  or  socks5://host:port"))
                    .textFieldStyle(.roundedBorder)
                Text("Routes every download — including video and torrent traffic — through a proxy. Leave blank for a direct connection.")
                    .font(.caption).foregroundStyle(.secondary)

                Toggle("Verify TLS certificates", isOn: $verifySsl)
                Text("Turn off only for a proxy that re-signs HTTPS, or a host with a broken certificate.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section("Media Defaults") {
                Picker("Video quality", selection: $videoQuality) {
                    Text("Best available").tag("Best available")
                    Text("Up to 1080p").tag("1080p")
                    Text("Up to 720p").tag("720p")
                    Text("Up to 480p").tag("480p")
                }
                Picker("Audio format", selection: $audioFormat) {
                    Text("MP3").tag("MP3")
                    Text("M4A").tag("M4A")
                    Text("FLAC").tag("FLAC")
                    Text("WAV").tag("WAV")
                }
            }

            HStack {
                Spacer()
                Button("Save Changes") {
                    store.saveSettings(threads: threads, maxConcurrent: maxConcurrent,
                                       cookiesBrowser: cookiesBrowser,
                                       maxSpeedKbps: speedLimit,
                                       proxyUrl: proxyUrl.trimmingCharacters(in: .whitespaces),
                                       verifySsl: verifySsl)
                }
                .buttonStyle(.borderedProminent)
                .tint(CopitaTheme.accent)
            }
        }
        .formStyle(.grouped)
        .onAppear(perform: syncFromSystemInfo)
        .onChange(of: store.systemInfo) { _ in syncFromSystemInfo() }
    }

    private func syncFromSystemInfo() {
        guard let info = store.systemInfo else { return }
        threads = info.defaultThreads
        maxConcurrent = info.maxConcurrentDownloads
        cookiesBrowser = info.cookiesBrowser ?? "auto"
        speedLimit = info.maxSpeedKbps
        proxyUrl = info.proxyUrl
        verifySsl = info.verifySsl
    }

    private func storageBar(info: SystemInfo) -> some View {
        let used = max(0, info.diskTotalGb - info.diskFreeGb)
        let fraction = info.diskTotalGb > 0 ? used / info.diskTotalGb : 0
        return VStack(alignment: .leading, spacing: 4) {
            ProgressView(value: fraction)
                .tint(.blue)
            HStack {
                Text("\(String(format: "%.1f", info.diskFreeGb)) GB free")
                Spacer()
                Text("of \(String(format: "%.1f", info.diskTotalGb)) GB total")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }
}

// MARK: - Supported Sites (static reference content)

private struct ServiceCategory {
    let symbol: String
    let title: String
    let items: [(String, String)]
}

private struct SupportedSitesTab: View {
    private let categories: [ServiceCategory] = [
        ServiceCategory(symbol: "play.rectangle.fill", title: "Video & Streaming", items: [
            ("YouTube", "4K · HDR · 60fps · Shorts · playlists & chapters"),
            ("TikTok, Instagram & Facebook", "Reels, stories and posts"),
            ("Twitch, Vimeo & Dailymotion", "Clips, VODs and full videos"),
            ("Reddit, X, Bilibili, Loom & 1,000+ more", "Any embedded or hosted video"),
        ]),
        ServiceCategory(symbol: "headphones", title: "Audio & Podcasts", items: [
            ("SoundCloud & Bandcamp", "Full-quality streams with cover art & tags"),
            ("Spotify", "Album and playlist tracks matched and fetched"),
            ("Podcasts", "Episodes pulled straight from the show's feed"),
        ]),
        ServiceCategory(symbol: "externaldrive.fill.badge.icloud", title: "Cloud & File Hosts", items: [
            ("Google Drive & MEGA", "Single files or a whole shared folder at once"),
            ("MediaFire, Dropbox, Gofile, pixeldrain", "Direct links, tokens handled for you"),
            ("1fichier, Catbox, KrakenFiles, workupload", "Ad-gates and wait timers cleared automatically"),
        ]),
        ServiceCategory(symbol: "dot.radiowaves.left.and.right", title: "Streams & Transfers", items: [
            ("HLS (.m3u8)", "Quality picking + AES-128 decryption"),
            ("DASH (.mpd)", "Separate video and audio muxed together"),
            ("BitTorrent & magnet links", "DHT, resume and sequential download"),
            ("Direct HTTP/HTTPS", "Up to 32 parallel connections per file"),
        ]),
        ServiceCategory(symbol: "book.fill", title: "Books & Documents", items: [
            ("Scribd", "Documents and presentations"),
            ("Issuu, FlipHTML5, AnyFlip, Calaméo, Yumpu", "Flipbooks rebuilt into a clean PDF"),
            ("Any web page", "Finds the real file behind the page for you"),
        ]),
        ServiceCategory(symbol: "books.vertical.fill", title: "Manga, Comics & 3D", items: [
            ("WEBTOON, MangaDex, MangaPlus, Tapas", "Chapters packaged as CBZ"),
            ("Sketchfab", "3D models with their textures"),
            ("Google Arts & Culture", "Gigapixel deep-zoom artwork"),
        ]),
    ]

    private let columns = [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("Copita has built-in support for the sites and formats below — nothing to install, no plugins, no setup. Paste a link and it works out which one it is.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                // A plain Grid so both cards in a row share the same height —
                // the card outlines line up no matter how many items each holds.
                Grid(horizontalSpacing: 12, verticalSpacing: 12) {
                    ForEach(Array(stride(from: 0, to: categories.count, by: 2)), id: \.self) { row in
                        GridRow {
                            card(categories[row])
                            if row + 1 < categories.count {
                                card(categories[row + 1])
                            } else {
                                Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                            }
                        }
                    }
                }
            }
            .padding(16)
        }
    }

    private func card(_ category: ServiceCategory) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Image(systemName: category.symbol)
                    .foregroundStyle(CopitaTheme.accent)
                    .font(.system(size: 12))
                Text(category.title)
                    .font(.system(size: 12, weight: .semibold))
                Spacer(minLength: 6)
                Text("Built in")
                    .font(.system(size: 9, weight: .semibold))
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(Color.green.opacity(0.15)))
                    .foregroundStyle(.green)
            }
            ForEach(category.items, id: \.0) { item in
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.0).font(.system(size: 11.5, weight: .medium))
                    Text(item.1).font(.system(size: 10.5)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: CopitaTheme.cardRadius).fill(Color.primary.opacity(0.04)))
    }
}

// MARK: - Connection

private struct ConnectionTab: View {
    @ObservedObject var store: CopitaStore
    @State private var healthStatus: String = ""
    @State private var isChecking = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 12) {
                    Image(systemName: "checkmark.shield.fill")
                        .foregroundStyle(.green)
                        .font(.system(size: 22))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Download service").font(.system(size: 13, weight: .semibold))
                        Text("Check that Copita's download service is responding.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(isChecking ? "Checking…" : "Run Health Check") {
                        isChecking = true
                        store.runHealthCheck { ok, elapsed in
                            isChecking = false
                            healthStatus = ok ? "Healthy — \(Int(elapsed * 1000))ms" : "Unreachable"
                        }
                    }
                    .disabled(isChecking)
                }
                if !healthStatus.isEmpty {
                    Text(healthStatus)
                        .font(.caption)
                        .foregroundStyle(healthStatus.hasPrefix("Healthy") ? .green : .red)
                }
                Text("A connection check does not test individual websites.")
                    .font(.caption2).foregroundStyle(.secondary)

                Divider()

                VStack(alignment: .leading, spacing: 4) {
                    Text("Requirements").font(.system(size: 13, weight: .semibold))
                    Text("What Copita needs on this Mac, and whether it's already there.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                ForEach(store.requirements) { req in
                    RequirementRow(req: req, store: store)
                }
            }
            .padding(16)
        }
        .onAppear { store.fetchRequirements() }
    }
}

// MARK: - Integrations (browser extension + system-wide Services)

// Everything about how a link gets *into* Copita from outside the app
// lives here, separate from Connection's backend-health/requirements
// concerns -- the extension and the Finder/system Service are the same
// kind of thing (an entry point into Copita) and belong together.
private struct IntegrationsTab: View {
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                BrowserExtensionSection()
                Divider()
                FinderServiceSection()
            }
            .padding(16)
        }
    }
}

// Chrome/Brave don't let any outside installer silently add an extension --
// that's a deliberate anti-malware boundary on their end, not something to
// route around. This gets the user to the real install point (Developer
// Mode + Load Unpacked) with zero guesswork: the folder opens in Finder
// and the right extensions page opens right next to it.
private struct BrowserExtensionSection: View {
    private let workspacePath = AppDelegate.shared?.workspacePath ?? ""
    private var browsers: [BrowserExtensionInstaller.DetectedBrowser] {
        BrowserExtensionInstaller.detectedBrowsers()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Browser Extension").font(.system(size: 13, weight: .semibold))
                Text("Catches downloads and adds a Copita button on video/audio pages.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if browsers.isEmpty {
                Text("Install Google Chrome or Brave to use the browser extension.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(browsers) { browser in
                    HStack {
                        Text(browser.name).font(.system(size: 12))
                        Spacer()
                        Button("Open \(browser.name) Extensions") {
                            BrowserExtensionInstaller.openExtensionsPage(browser)
                        }
                    }
                }
                Button("Reveal Extension Folder in Finder") {
                    BrowserExtensionInstaller.revealExtensionFolder(workspacePath: workspacePath)
                }

                VStack(alignment: .leading, spacing: 3) {
                    Text("1. Turn on Developer Mode (top right of the Extensions page)")
                    Text("2. Click \"Load unpacked\"")
                    Text("3. Select the copita-extension folder that opened in Finder")
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            }
        }
    }
}

// A system Service (registered via NSServices in Info.plist, handled by
// AppDelegate.downloadWithCopita) rather than anything specific to
// Finder -- it shows up in the right-click "Services" submenu wherever
// text is selected anywhere on the Mac, not just in Finder. macOS
// sometimes needs a Service explicitly turned on the first time under
// Keyboard Shortcuts before it appears in that menu, so this links
// straight there instead of just describing it.
private struct FinderServiceSection: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Finder & System-Wide").font(.system(size: 13, weight: .semibold))
                Text("Select a link anywhere on your Mac -- Finder, Notes, Mail, TextEdit -- right-click it, and choose Services > \"Download with Copita.\"")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button("Open Keyboard Shortcuts Settings") {
                if let url = URL(string: "x-apple.systempreferences:com.apple.preference.keyboard") {
                    NSWorkspace.shared.open(url)
                }
            }
            Text("If it doesn't appear in your right-click menu yet, enable it under Keyboard Shortcuts > Services > Text.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }
}

private struct RequirementRow: View {
    let req: Requirement
    @ObservedObject var store: CopitaStore

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(req.label).font(.system(size: 12.5, weight: .medium))
                Text(req.detail ?? req.description).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            statusBadge
            if req.status == "missing" && req.installable {
                Button("Install") { store.installRequirement(req.id) }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            } else if req.status == "error" && req.installable {
                Button("Retry") { store.installRequirement(req.id) }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            } else if req.status == "missing", let urlString = req.manualUrl, let url = URL(string: urlString) {
                Button("Open Site") { NSWorkspace.shared.open(url) }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: CopitaTheme.controlRadius).fill(Color.primary.opacity(0.03)))
    }

    private var statusBadge: some View {
        Group {
            switch req.status {
            case "installed":
                Label("Installed", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            case "installing":
                HStack(spacing: 4) {
                    ProgressView().controlSize(.small)
                    Text("Installing…")
                }
                .foregroundStyle(.secondary)
            case "error":
                Label("Error", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red)
            default:
                Label("Missing", systemImage: "circle").foregroundStyle(.secondary)
            }
        }
        .font(.system(size: 11, weight: .medium))
    }
}

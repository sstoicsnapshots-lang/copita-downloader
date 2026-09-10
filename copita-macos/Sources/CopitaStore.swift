import Foundation
import AppKit
import UserNotifications

@MainActor
class CopitaStore: NSObject, ObservableObject, URLSessionWebSocketDelegate {
    @Published var tasks: [CopitaTask] = [] {
        didSet { updateDockBadge() }
    }
    @Published var connected: Bool = false
    @Published var selectedFilter: SidebarFilter = .all
    @Published var searchQuery: String = ""
    @Published var systemInfo: SystemInfo?
    @Published var requirements: [Requirement] = []
    // A transient status line (e.g. "Updating yt-dlp…") shown at the top of
    // the window; nil when there's nothing to say.
    @Published var statusBanner: String?

    let baseURL = URL(string: "http://127.0.0.1:8888")!
    private lazy var session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
    private var webSocketTask: URLSessionWebSocketTask?
    private var previousStatusById: [String: String] = [:]

    func connect() {
        webSocketTask?.cancel()
        let wsURL = URL(string: "ws://127.0.0.1:8888/ws")!
        let task = session.webSocketTask(with: wsURL)
        webSocketTask = task
        task.resume()
        listen()
    }

    private func listen() {
        webSocketTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                Task { @MainActor in
                    self.connected = false
                    self.scheduleReconnect()
                }
            case .success(let message):
                Task { @MainActor in
                    if case .string(let text) = message, let data = text.data(using: .utf8) {
                        self.handle(data: data)
                    }
                    self.listen()
                }
            }
        }
    }

    private func handle(data: Data) {
        guard let envelope = try? JSONDecoder().decode(WSEnvelope.self, from: data) else { return }
        Task { @MainActor in
            self.connected = true
            if let allTasks = envelope.tasks {
                self.tasks = allTasks.sorted { $0.createdAt > $1.createdAt }
                for t in allTasks { self.previousStatusById[t.id] = t.status }
                return
            }
            if envelope.event == "requirement_updated", let req = envelope.requirement {
                if let idx = self.requirements.firstIndex(where: { $0.id == req.id }) {
                    self.requirements[idx] = req
                } else {
                    self.requirements.append(req)
                }
                return
            }

            if envelope.event == "component_update" {
                self.handleComponentUpdate(state: envelope.state ?? "",
                                           message: envelope.message ?? "")
                return
            }

            guard let t = envelope.task else { return }
            if envelope.event == "task_deleted" {
                self.tasks.removeAll { $0.id == t.id }
                self.previousStatusById.removeValue(forKey: t.id)
                return
            }
            self.notifyIfNewlyTerminal(t)
            if let idx = self.tasks.firstIndex(where: { $0.id == t.id }) {
                self.tasks[idx] = t
            } else {
                self.tasks.insert(t, at: 0)
            }
        }
    }

    @MainActor
    private func handleComponentUpdate(state: String, message: String) {
        switch state {
        case "updating":
            statusBanner = message                          // "Updating yt-dlp 2026.08.19 → …"
        case "updated":
            statusBanner = message
            let content = UNMutableNotificationContent()
            content.title = "Copita"
            content.body = message                           // "yt-dlp updated to 2026.09.01"
            content.sound = .default
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: "copita-component-\(Date().timeIntervalSince1970)",
                                      content: content, trigger: nil), withCompletionHandler: nil)
            DispatchQueue.main.asyncAfter(deadline: .now() + 6) {
                if self.statusBanner == message { self.statusBanner = nil }
            }
        case "failed":
            statusBanner = message
            DispatchQueue.main.asyncAfter(deadline: .now() + 6) {
                if self.statusBanner == message { self.statusBanner = nil }
            }
        default:                                             // "current"
            // Brief confirmation on launch that the auto-update ran.
            statusBanner = message                           // "yt-dlp is up to date (…)"
            DispatchQueue.main.asyncAfter(deadline: .now() + 3.5) {
                if self.statusBanner == message { self.statusBanner = nil }
            }
        }
    }

    // A completed/failed download deserves a real native notification —
    // no JS bridge needed for this any more, this is just Swift calling
    // UserNotifications directly.
    private func notifyIfNewlyTerminal(_ t: CopitaTask) {
        let previous = previousStatusById[t.id]
        previousStatusById[t.id] = t.status
        guard previous != t.status else { return }
        guard t.status == "completed" || t.status == "failed" else { return }

        let content = UNMutableNotificationContent()
        content.title = t.status == "completed" ? "Download complete" : "Download failed"
        content.body = t.title
        content.sound = .default
        let request = UNNotificationRequest(identifier: t.id + "-" + t.status, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }

    private func scheduleReconnect() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.connect()
        }
    }

    func refreshOnce() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/tasks"))
        request.httpMethod = "GET"
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let self, let data else { return }
            guard let decoded = try? JSONDecoder().decode([String: [CopitaTask]].self, from: data),
                  let list = decoded["tasks"] else { return }
            Task { @MainActor in
                self.tasks = list.sorted { $0.createdAt > $1.createdAt }
            }
        }.resume()
    }

    func download(url: String, threads: Int = 16, audioOnly: Bool = false) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/download"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["url": url, "threads": threads]
        if audioOnly { body["audio_only"] = true }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: request).resume()
    }

    private func post(_ path: String, taskId: String) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/tasks/\(taskId)/\(path)"))
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request).resume()
    }

    func retry(_ id: String) { post("retry", taskId: id) }
    func pause(_ id: String) { post("pause", taskId: id) }
    func resume(_ id: String) { post("resume", taskId: id) }
    func cancel(_ id: String) { post("cancel", taskId: id) }

    func remove(_ id: String) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/tasks/\(id)"))
        request.httpMethod = "DELETE"
        URLSession.shared.dataTask(with: request).resume()
    }

    func pauseAllQueue() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/queue/pause-all"))
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request).resume()
    }

    func resumeAllQueue() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/queue/resume-all"))
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request).resume()
    }

    func clearCompleted() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/tasks-completed"))
        request.httpMethod = "DELETE"
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            Task { @MainActor in self?.refreshOnce() }
        }.resume()
    }

    func revealInFinder(taskId: String) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/tasks/\(taskId)/open"))
        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "reveal", value: "true")]
        request.url = components.url
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request).resume()
    }

    func openDownloadsFolder() {
        guard let dir = systemInfo?.downloadDir else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: dir))
    }

    func fetchSystemInfo(retriesLeft: Int = 8) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/system"))
        request.httpMethod = "GET"
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let self else { return }
            if let data, let info = try? JSONDecoder().decode(SystemInfo.self, from: data) {
                Task { @MainActor in self.systemInfo = info }
                return
            }
            // The backend usually isn't listening yet on a cold launch — keep
            // retrying so the sidebar footer / storage bar actually populate
            // without the user having to open Settings.
            if retriesLeft > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                    self.fetchSystemInfo(retriesLeft: retriesLeft - 1)
                }
            }
        }.resume()
    }

    // MARK: - Derived state for the UI

    func tasks(for filter: SidebarFilter, search: String) -> [CopitaTask] {
        let base = tasks.filter { filter.matches($0) }
        guard !search.trimmingCharacters(in: .whitespaces).isEmpty else { return base }
        let q = search.lowercased()
        return base.filter { $0.title.lowercased().contains(q) || $0.url.lowercased().contains(q) }
    }

    func count(for filter: SidebarFilter) -> Int {
        tasks.filter { filter.matches($0) }.count
    }

    private func updateDockBadge() {
        let activeCount = tasks.filter { DownloadStatus.nonTerminal.contains($0.status) }.count
        NSApp.dockTile.badgeLabel = activeCount > 0 ? "\(activeCount)" : nil
    }

    // MARK: - Settings / Requirements (Phase 2)

    func fetchRequirements() {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/requirements"))
        request.httpMethod = "GET"
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let self, let data else { return }
            guard let decoded = try? JSONDecoder().decode([String: [Requirement]].self, from: data),
                  let list = decoded["requirements"] else { return }
            Task { @MainActor in self.requirements = list }
        }.resume()
    }

    func installRequirement(_ id: String) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/requirements/\(id)/install"))
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request).resume()
    }

    func saveSettings(threads: Int, maxConcurrent: Int, cookiesBrowser: String?,
                      maxSpeedKbps: Int? = nil, proxyUrl: String? = nil, verifySsl: Bool? = nil) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/settings"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["threads": threads, "max_concurrent": maxConcurrent]
        body["cookies_browser"] = cookiesBrowser as Any? ?? NSNull()
        if let s = maxSpeedKbps { body["max_speed_kbps"] = s }
        if let p = proxyUrl { body["proxy_url"] = p }
        if let v = verifySsl { body["verify_ssl"] = v }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            Task { @MainActor in self?.fetchSystemInfo() }
        }.resume()
    }

    func runHealthCheck(completion: @escaping (Bool, TimeInterval) -> Void) {
        let start = Date()
        var request = URLRequest(url: baseURL.appendingPathComponent("api/health"))
        request.httpMethod = "GET"
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let elapsed = Date().timeIntervalSince(start)
            let ok = (response as? HTTPURLResponse)?.statusCode == 200 && data != nil
            Task { @MainActor in completion(ok, elapsed) }
        }.resume()
    }
}

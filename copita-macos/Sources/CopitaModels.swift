import Foundation

struct CopitaTask: Codable, Identifiable, Equatable {
    let id: String
    let url: String
    var title: String
    var category: String
    var engine: String
    var status: String
    var percent: Double
    var downloaded: Int64
    var total: Int64?
    var speed: Double
    var speedAvailable: Bool
    var eta: Double?
    var threads: Int
    var outputPath: String?
    var filename: String?
    var error: String?
    var errorDetails: String?
    var canPause: Bool
    var createdAt: Double
    var group: TaskGroup?

    enum CodingKeys: String, CodingKey {
        case id, url, title, category, engine, status, percent, downloaded, total, speed
        case speedAvailable = "speed_available"
        case eta, threads
        case outputPath = "output_path"
        case filename, error
        case errorDetails = "error_details"
        case canPause = "can_pause"
        case createdAt = "created_at"
        case group
    }
}

/// Set on every child task of an expanded playlist — same `id` across the
/// whole playlist so the UI can fold them into one collapsible row.
struct TaskGroup: Codable, Equatable {
    let id: String
    let title: String
    let index: Int?
    let total: Int?
}

struct WSEnvelope: Codable {
    let event: String?
    let task: CopitaTask?
    let tasks: [CopitaTask]?
    let requirement: Requirement?
    // component_update events (yt-dlp auto-update, …)
    let component: String?
    let state: String?
    let message: String?
}

struct Requirement: Codable, Identifiable, Equatable {
    let id: String
    let label: String
    let description: String
    let installed: Bool
    let version: String?
    let detail: String?
    let installable: Bool
    let required: Bool
    let manualUrl: String?
    let status: String // missing|installing|installed|error

    enum CodingKeys: String, CodingKey {
        case id, label, description, installed, version, detail, installable, required
        case manualUrl = "manual_url"
        case status
    }
}

struct SystemInfo: Codable, Equatable {
    let appName: String
    let version: String
    let downloadDir: String
    let diskFreeGb: Double
    let diskTotalGb: Double
    let ffmpeg: String
    let ytdlp: String
    let defaultThreads: Int
    let cookiesBrowser: String?
    let maxConcurrentDownloads: Int
    let maxSpeedKbps: Int
    let proxyUrl: String
    let verifySsl: Bool

    enum CodingKeys: String, CodingKey {
        case appName = "app_name"
        case version
        case downloadDir = "download_dir"
        case diskFreeGb = "disk_free_gb"
        case diskTotalGb = "disk_total_gb"
        case ffmpeg, ytdlp
        case defaultThreads = "default_threads"
        case cookiesBrowser = "cookies_browser"
        case maxConcurrentDownloads = "max_concurrent_downloads"
        case maxSpeedKbps = "max_speed_kbps"
        case proxyUrl = "proxy_url"
        case verifySsl = "verify_ssl"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        appName = try c.decode(String.self, forKey: .appName)
        version = try c.decode(String.self, forKey: .version)
        downloadDir = try c.decode(String.self, forKey: .downloadDir)
        diskFreeGb = try c.decode(Double.self, forKey: .diskFreeGb)
        diskTotalGb = try c.decode(Double.self, forKey: .diskTotalGb)
        ffmpeg = try c.decode(String.self, forKey: .ffmpeg)
        ytdlp = try c.decode(String.self, forKey: .ytdlp)
        defaultThreads = try c.decode(Int.self, forKey: .defaultThreads)
        cookiesBrowser = try c.decodeIfPresent(String.self, forKey: .cookiesBrowser)
        maxConcurrentDownloads = try c.decode(Int.self, forKey: .maxConcurrentDownloads)
        maxSpeedKbps = try c.decodeIfPresent(Int.self, forKey: .maxSpeedKbps) ?? 0
        proxyUrl = try c.decodeIfPresent(String.self, forKey: .proxyUrl) ?? ""
        verifySsl = try c.decodeIfPresent(Bool.self, forKey: .verifySsl) ?? true
    }
}

enum SidebarFilter: String, CaseIterable, Identifiable {
    case all, active, completed, videos, audio, manga, files

    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "All Downloads"
        case .active: return "Active Queue"
        case .completed: return "Downloaded"
        case .videos: return "Movies & Videos"
        case .audio: return "Audio & Music"
        case .manga: return "Manga & Comics"
        case .files: return "Files & Archives"
        }
    }

    var symbol: String {
        switch self {
        case .all: return "tray.and.arrow.down"
        case .active: return "clock"
        case .completed: return "checkmark.circle"
        case .videos: return "film"
        case .audio: return "music.note"
        case .manga: return "book.closed"
        case .files: return "folder"
        }
    }

    var isLibrarySection: Bool {
        switch self {
        case .videos, .audio, .manga, .files: return true
        default: return false
        }
    }

    var urlPlaceholder: String? {
        switch self {
        case .all: return "Paste a download link"
        case .videos: return "Paste a video link"
        case .audio: return "Paste a music or video link"
        case .manga: return "Paste a chapter link"
        case .files: return "Paste a file link"
        case .active, .completed: return nil
        }
    }

    var primaryButtonLabel: String {
        self == .audio ? "Extract Audio" : "Download"
    }

    func matches(_ task: CopitaTask) -> Bool {
        switch self {
        case .all: return true
        case .active: return DownloadStatus.nonTerminal.contains(task.status)
        case .completed: return task.status == "completed"
        case .videos: return task.category == "video"
        case .audio: return task.category == "audio"
        case .manga: return task.category == "manga"
        case .files: return ["document", "file", "torrent", "webpage"].contains(task.category)
        }
    }

    var emptyTitle: String {
        switch self {
        case .all: return "No Downloads Yet"
        case .active: return "Queue is Empty"
        case .completed: return "No Downloaded Files"
        case .videos: return "No Videos Yet"
        case .audio: return "No Audio Yet"
        case .manga: return "No Manga Yet"
        case .files: return "No Files Yet"
        }
    }

    var emptySubtitle: String {
        switch self {
        case .active: return "Downloads in progress will show up here."
        case .completed: return "Files you've finished downloading will show up here."
        default: return "Paste or drag any video, audio, or document link to begin."
        }
    }
}

enum DownloadStatus {
    static let nonTerminal: Set<String> = [
        "queued", "analyzing", "downloading", "merging", "compressing", "extracting", "paused"
    ]
    static let active: Set<String> = [
        "downloading", "analyzing", "merging", "compressing", "extracting"
    ]
}

// MARK: - Playlist grouping (one collapsible row per expanded playlist)

/// A row in the task list is either a standalone download or a folded
/// playlist group.
enum TaskListItem: Identifiable {
    case single(CopitaTask)
    case group(PlaylistBundle)

    var id: String {
        switch self {
        case .single(let t): return "t-\(t.id)"
        case .group(let b): return "g-\(b.id)"
        }
    }
}

/// All the children of one expanded playlist, plus the aggregate figures the
/// group header shows.
struct PlaylistBundle: Identifiable {
    let id: String
    let title: String
    let declaredTotal: Int?
    /// Children visible under the current sidebar filter, sorted by playlist position.
    let children: [CopitaTask]

    var total: Int { max(declaredTotal ?? children.count, children.count) }
    var done: Int { children.filter { $0.status == "completed" }.count }
    var failed: Int { children.filter { $0.status == "failed" }.count }
    var activeCount: Int { children.filter { DownloadStatus.active.contains($0.status) }.count }
    var pausedCount: Int { children.filter { $0.status == "paused" }.count }
    var queuedCount: Int { children.filter { $0.status == "queued" }.count }
    var combinedSpeed: Double {
        children.reduce(0) { $0 + ($1.status == "downloading" ? $1.speed : 0) }
    }
    var combinedDownloaded: Int64 { children.reduce(0) { $0 + $1.downloaded } }
    var combinedTotal: Int64? {
        let known = children.compactMap(\.total)
        return known.count == children.count && !children.isEmpty ? known.reduce(0, +) : nil
    }
    /// 0–100, completed items count full, in-flight items count their own %.
    var percent: Double {
        guard total > 0 else { return 0 }
        let sum = children.reduce(0.0) { acc, t in
            if t.status == "completed" { return acc + 100 }
            if t.status == "downloading" { return acc + max(0, min(100, t.percent)) }
            return acc
        }
        return sum / Double(total)
    }
    var allComplete: Bool { !children.isEmpty && done == children.count }
    var allTerminal: Bool {
        !children.isEmpty && children.allSatisfy { ["completed", "failed", "cancelled"].contains($0.status) }
    }
    var overallStatus: String {
        if allComplete { return "completed" }
        if activeCount > 0 { return "downloading" }
        if !children.isEmpty && failed == children.count { return "failed" }
        if pausedCount > 0 { return "paused" }
        return "queued"
    }
    var childIDs: [String] { children.map(\.id) }
}

/// Fold a filtered, sorted task list into standalone rows + playlist groups.
/// A group takes the list position of its first-seen child.
func groupTaskList(_ tasks: [CopitaTask]) -> [TaskListItem] {
    var buckets: [String: [CopitaTask]] = [:]
    for t in tasks where t.group != nil { buckets[t.group!.id, default: []].append(t) }

    var out: [TaskListItem] = []
    var emitted = Set<String>()
    for t in tasks {
        guard let g = t.group else { out.append(.single(t)); continue }
        if emitted.insert(g.id).inserted {
            let kids = (buckets[g.id] ?? []).sorted {
                ($0.group?.index ?? .max) < ($1.group?.index ?? .max)
            }
            out.append(.group(PlaylistBundle(
                id: g.id, title: g.title, declaredTotal: g.total, children: kids
            )))
        }
    }
    return out
}

func formatBytes(_ bytes: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
}

enum PreviewKind: Equatable {
    case video, audio, pdf, image, none

    static func from(filename: String?) -> PreviewKind {
        guard let ext = filename.flatMap({ ($0 as NSString).pathExtension.lowercased() }) else { return .none }
        switch ext {
        case "mp4", "webm", "mov", "m4v": return .video
        case "mp3", "m4a", "wav", "flac", "ogg": return .audio
        case "pdf": return .pdf
        case "png", "jpg", "jpeg", "webp", "gif": return .image
        default: return .none
        }
    }
}

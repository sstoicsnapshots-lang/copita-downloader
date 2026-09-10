import SwiftUI
import QuickLookThumbnailing

/// File-type icons/symbols, not status colors — a generic colored circle
/// (checkmark/arrow/triangle) told you a task's *state* but nothing about
/// what the file actually *is*. Real download managers (and Finder) show
/// the file's real appearance; this does the same via two tiers:
/// a genuine QuickLook thumbnail when a completed file exists on disk
/// (real cover art, a video poster frame, an actual PDF page), falling
/// back to a file-type-specific SF Symbol (never a generic blob) when
/// there's no file yet or QuickLook can't render one.
enum FileTypeIcon {
    static func symbol(filename: String?, category: String) -> String {
        if let ext = filename.flatMap({ ($0 as NSString).pathExtension.lowercased() }), !ext.isEmpty {
            switch ext {
            case "mp4", "mov", "webm", "m4v", "mkv", "avi", "ts": return "film"
            case "mp3", "m4a", "wav", "flac", "ogg", "aac": return "music.note"
            case "pdf": return "doc.richtext"
            case "png", "jpg", "jpeg", "webp", "gif", "heic", "bmp": return "photo"
            case "zip", "rar", "7z", "tar", "gz", "bz2", "xz": return "archivebox"
            case "cbz", "cbr": return "book.closed"
            case "epub", "mobi", "azw3": return "book"
            case "torrent": return "arrow.triangle.2.circlepath"
            case "doc", "docx", "rtf", "txt": return "doc.text"
            default: break
            }
        }
        switch category {
        case "video": return "film"
        case "audio": return "music.note"
        case "document": return "doc.richtext"
        case "manga": return "book.closed"
        case "torrent": return "arrow.triangle.2.circlepath"
        case "webpage": return "globe"
        default: return "doc"
        }
    }
}

struct FileIconView: View {
    let task: CopitaTask
    @State private var thumbnail: NSImage?

    private var symbolName: String { FileTypeIcon.symbol(filename: task.filename, category: task.category) }
    private var statusColor: Color { StatusVisual(status: task.status).color }

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: CopitaTheme.iconBoxRadius)
                .fill(thumbnail != nil ? Color.primary.opacity(0.06) : statusColor.opacity(0.12))
            if let thumbnail {
                Image(nsImage: thumbnail)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(width: CopitaTheme.iconBoxSize.width, height: CopitaTheme.iconBoxSize.height)
                    .clipShape(RoundedRectangle(cornerRadius: CopitaTheme.iconBoxRadius))
            } else {
                Image(systemName: symbolName)
                    .foregroundStyle(statusColor)
                    .font(.system(size: 17))
            }
        }
        .frame(width: CopitaTheme.iconBoxSize.width, height: CopitaTheme.iconBoxSize.height)
        .overlay(alignment: .bottomTrailing) { statusBadge }
        .task(id: task.outputPath) { await loadThumbnail() }
    }

    @ViewBuilder
    private var statusBadge: some View {
        switch task.status {
        case "completed":
            badge(symbol: "checkmark.circle.fill", color: .green)
        case "failed":
            badge(symbol: "exclamationmark.circle.fill", color: .red)
        default:
            EmptyView()
        }
    }

    private func badge(symbol: String, color: Color) -> some View {
        Image(systemName: symbol)
            .font(.system(size: 13))
            .symbolRenderingMode(.palette)
            .foregroundStyle(.white, color)
            .background(Circle().fill(Color(nsColor: .windowBackgroundColor)).frame(width: 15, height: 15))
            .offset(x: 4, y: 4)
    }

    private func loadThumbnail() async {
        guard task.status == "completed",
              let path = task.outputPath,
              FileManager.default.fileExists(atPath: path) else { return }

        let url = URL(fileURLWithPath: path)
        let scale = NSScreen.main?.backingScaleFactor ?? 2
        let request = QLThumbnailGenerator.Request(
            fileAt: url,
            size: CopitaTheme.iconBoxSize,
            scale: scale,
            representationTypes: .thumbnail
        )

        // Deliberately no NSWorkspace.icon(forFile:) fallback here: that
        // reflects whatever app happens to be registered for the
        // extension on *this specific Mac* — caught live, a .rar showed
        // VLC's icon because VLC happened to claim that file association
        // on this machine, not because it means anything. Falling through
        // to the file-type SF Symbol below keeps the app's icon language
        // fully deterministic instead of at the mercy of arbitrary local
        // app registrations.
        thumbnail = await withCheckedContinuation { continuation in
            QLThumbnailGenerator.shared.generateBestRepresentation(for: request) { rep, _ in
                continuation.resume(returning: rep?.nsImage)
            }
        }
    }
}

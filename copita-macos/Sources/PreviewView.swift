import SwiftUI
import AVKit
import AVFoundation
import PDFKit

/// The one thing this native app can do that the web app's `<video>`/
/// `<iframe>`-based player modal can't: real playback frameworks. The app
/// and the completed file are on the same machine, so this also skips the
/// web app's necessary detour through `/api/stream/{id}` — straight to the
/// local file.
struct PreviewView: View {
    let task: CopitaTask

    private var kind: PreviewKind { PreviewKind.from(filename: task.filename) }
    private var fileURL: URL? { task.outputPath.map { URL(fileURLWithPath: $0) } }

    var body: some View {
        Group {
            if let url = fileURL, FileManager.default.fileExists(atPath: url.path) {
                switch kind {
                case .video:
                    VideoPreview(url: url)
                case .audio:
                    AudioPreview(url: url, title: task.title)
                case .pdf:
                    PDFPreview(url: url)
                case .image:
                    ImagePreview(url: url)
                case .none:
                    unsupported
                }
            } else {
                missingFile
            }
        }
        .frame(minWidth: 360, minHeight: 240)
    }

    private var unsupported: some View {
        placeholder(symbol: "questionmark.circle", text: "This file type can't be previewed in-app.")
    }

    private var missingFile: some View {
        placeholder(symbol: "exclamationmark.triangle", text: "This file is no longer on disk.")
    }

    private func placeholder(symbol: String, text: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: symbol).font(.system(size: 36)).foregroundStyle(.secondary)
            Text(text).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// SwiftUI's `VideoPlayer` (the `_AVKit_SwiftUI` bridge) crashes on first use
// in this environment — reproduced live, a genuine Swift metadata-
// instantiation failure deep in Apple's framework, not app logic (see
// PreviewWindowController.swift's note). AVPlayerView is the older,
// AppKit-native AVKit class (predates the SwiftUI bridge entirely) and
// sidesteps that broken code path completely while still giving native
// playback controls.
private struct VideoPreview: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.controlsStyle = .floating
        view.showsFullScreenToggleButton = true
        let player = AVPlayer(url: url)
        view.player = player
        player.play()
        return view
    }

    func updateNSView(_ nsView: AVPlayerView, context: Context) {}

    static func dismantleNSView(_ nsView: AVPlayerView, coordinator: ()) {
        nsView.player?.pause()
        nsView.player = nil
    }
}

private struct AudioPreview: View {
    let url: URL
    let title: String
    @StateObject private var controller = AudioPlayerController()

    var body: some View {
        VStack(spacing: 22) {
            ZStack {
                Circle().fill(CopitaTheme.accent.opacity(0.12)).frame(width: 96, height: 96)
                Image(systemName: "waveform").font(.system(size: 36)).foregroundStyle(CopitaTheme.accent)
            }
            Text(title)
                .font(.system(size: 13, weight: .semibold))
                .lineLimit(1)
                .padding(.horizontal, 20)

            VStack(spacing: 6) {
                Slider(value: $controller.progress, in: 0...1, onEditingChanged: controller.seek)
                    .tint(CopitaTheme.accent)
                HStack {
                    Text(controller.currentTimeText)
                    Spacer()
                    Text(controller.durationText)
                }
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 28)

            Button(action: controller.togglePlayPause) {
                Image(systemName: controller.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: 18))
                    .frame(width: 44, height: 44)
                    .background(Circle().fill(CopitaTheme.accent))
                    .foregroundStyle(.white)
            }
            .buttonStyle(.plain)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear { controller.load(url: url) }
        .onDisappear { controller.pause() }
    }
}

@MainActor
private final class AudioPlayerController: ObservableObject {
    @Published var isPlaying = false
    @Published var progress: Double = 0
    @Published var currentTimeText = "0:00"
    @Published var durationText = "0:00"

    private var player: AVPlayer?
    private var timeObserver: Any?
    private var duration: Double = 0

    func load(url: URL) {
        let item = AVPlayerItem(url: url)
        let p = AVPlayer(playerItem: item)
        player = p

        Task {
            if let loaded = try? await item.asset.load(.duration) {
                let seconds = CMTimeGetSeconds(loaded)
                if seconds.isFinite {
                    duration = seconds
                    durationText = Self.format(seconds)
                }
            }
        }

        timeObserver = p.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main) { [weak self] time in
            Task { @MainActor in
                guard let self else { return }
                let seconds = CMTimeGetSeconds(time)
                self.currentTimeText = Self.format(seconds)
                if self.duration > 0 { self.progress = seconds / self.duration }
            }
        }

        p.play()
        isPlaying = true
    }

    func togglePlayPause() {
        guard let player else { return }
        if isPlaying { player.pause() } else { player.play() }
        isPlaying.toggle()
    }

    func seek(editing: Bool) {
        guard !editing, let player, duration > 0 else { return }
        player.seek(to: CMTime(seconds: progress * duration, preferredTimescale: 600))
    }

    func pause() {
        player?.pause()
        if let observer = timeObserver { player?.removeTimeObserver(observer) }
    }

    private static func format(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "0:00" }
        return String(format: "%d:%02d", Int(seconds) / 60, Int(seconds) % 60)
    }
}

private struct PDFPreview: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> PDFView {
        let view = PDFView()
        view.displayMode = .singlePageContinuous
        view.document = PDFDocument(url: url)
        // `autoScales` needs the view's real frame to compute a sane zoom.
        // At `makeNSView` time SwiftUI hasn't finished layout yet, so
        // setting it here alone produced a blank page. Setting it again in
        // `updateNSView` isn't reliable either — SwiftUI only re-invokes
        // that when something causes a diff, which a standalone preview
        // window with no other state changes may never do (caught live:
        // a synthetic test file happened to get a lucky second layout
        // pass and rendered fine, but a real file opened via the actual
        // Play button stayed permanently blank — a "cold render"). Only a
        // deferred dispatch, guaranteed to run on a later run-loop tick
        // after AppKit has actually finished layout, fixes it reliably.
        DispatchQueue.main.async {
            view.autoScales = true
        }
        return view
    }

    func updateNSView(_ nsView: PDFView, context: Context) {
        nsView.autoScales = true
    }
}

private struct ImagePreview: View {
    let url: URL

    var body: some View {
        if let image = NSImage(contentsOf: url) {
            ScrollView([.horizontal, .vertical]) {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
            }
            .background(Color.black)
        } else {
            Text("Unable to load image").foregroundStyle(.secondary)
        }
    }
}

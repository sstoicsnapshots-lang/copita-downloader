import Cocoa
import SwiftUI

/// Holds the task currently being previewed. Kept separate from
/// PreviewWindowController so the window's NSHostingView can be created
/// ONCE, empty, at controller-init time, and have its content swap in
/// later via a normal @Published update — not by AppKit tearing down and
/// replacing the hosting view's contentView each time a preview is shown.
///
/// This isn't stylistic: creating a fresh NSHostingView whose very first
/// rendered content is an AVKit VideoPlayer, then immediately handing it
/// to `window.setContentView:`, crashed in `_AVKit_SwiftUI`'s
/// `ViewResponderFilter.init` (a Swift metadata-instantiation failure deep
/// in Apple's own frameworks, not app logic) — reproduced live during this
/// session's own verification pass. Starting the hosting view on an empty
/// placeholder and only introducing VideoPlayer via a later, ordinary
/// SwiftUI diff avoids that exact code path entirely.
@MainActor
private final class PreviewTaskHolder: ObservableObject {
    @Published var task: CopitaTask?
}

private struct PreviewHostView: View {
    @ObservedObject var holder: PreviewTaskHolder

    var body: some View {
        if let task = holder.task {
            // Without an explicit identity here, SwiftUI sees the same
            // view type in the same tree position across different tasks
            // and treats it as the *same* view getting a prop update, not
            // a new one -- so AudioPreview's @StateObject player (and its
            // .onAppear load(url:) call) never re-runs for the new file.
            // Confirmed live: playing one audio file, then clicking Play
            // on a different one, kept the first file's audio going.
            // Keying on the task id forces SwiftUI to tear down and
            // rebuild the subtree per task instead.
            PreviewView(task: task).id(task.id)
        } else {
            Color.clear
        }
    }
}

@MainActor
class PreviewWindowController: NSWindowController, NSWindowDelegate {
    private let holder = PreviewTaskHolder()

    convenience init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 600),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        self.init(window: window)
        window.contentView = NSHostingView(rootView: PreviewHostView(holder: holder))
        // The red traffic-light button (and Cmd+W) call AppKit's own
        // close(), not closeAndClear() -- and since isReleasedWhenClosed
        // is false, that just hides the window without ever clearing
        // holder.task, so the SwiftUI subtree (and its AVPlayer) never
        // tears down. Confirmed live: closing the preview left audio/video
        // playing indefinitely with no visible window. Only a window
        // delegate catches every way a window can close, not just the
        // one path our own showWindow-adjacent code happens to call.
        window.delegate = self
    }

    func windowWillClose(_ notification: Notification) {
        holder.task = nil
    }

    func show(task: CopitaTask) {
        guard let window else { return }
        window.title = task.title
        window.setContentSize(Self.windowSize(for: PreviewKind.from(filename: task.filename)))
        window.center()
        holder.task = task
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func closeAndClear() {
        holder.task = nil
        window?.orderOut(nil)
    }

    private static func windowSize(for kind: PreviewKind) -> NSSize {
        switch kind {
        case .video: return NSSize(width: 960, height: 620)
        case .audio: return NSSize(width: 480, height: 320)
        case .pdf: return NSSize(width: 900, height: 900)
        case .image: return NSSize(width: 800, height: 700)
        case .none: return NSSize(width: 480, height: 240)
        }
    }
}

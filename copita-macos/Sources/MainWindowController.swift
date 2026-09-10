import Cocoa
import SwiftUI

@MainActor
class MainWindowController: NSObject, NSWindowDelegate {
    var window: NSWindow!
    let store = CopitaStore()
    private var settingsController: SettingsWindowController?
    private var previewController: PreviewWindowController?

    func setupWindow() {
        let rect = NSRect(x: 100, y: 100, width: 1060, height: 720)
        window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )

        window.title = "Copita Downloader"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.minSize = NSSize(width: 800, height: 540)
        window.center()
        window.isOpaque = true
        window.backgroundColor = .windowBackgroundColor
        // Follow system appearance naturally (Light/Dark mode)
        window.appearance = nil
        window.delegate = self

        let hosting = NSHostingView(rootView: ContentView(
            store: store,
            onOpenSettings: { [weak self] in self?.openPreferences() },
            onPlay: { [weak self] task in self?.showPreview(for: task) }
        ))
        hosting.frame = NSRect(origin: .zero, size: rect.size)
        hosting.autoresizingMask = [.width, .height]
        window.contentView = hosting
    }

    func loadApp() {
        store.connect()
        store.refreshOnce()
    }

    func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func openPreferences() {
        if settingsController == nil {
            settingsController = SettingsWindowController(store: store)
        }
        settingsController?.show()
    }

    func showPreview(for task: CopitaTask) {
        if previewController == nil {
            previewController = PreviewWindowController()
        }
        previewController?.show(task: task)
    }

    // NSWindowDelegate
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
}

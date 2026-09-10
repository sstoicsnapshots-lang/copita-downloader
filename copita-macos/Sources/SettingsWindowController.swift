import Cocoa
import SwiftUI

/// The app uses the legacy NSApplicationDelegate lifecycle (see main.swift),
/// not the modern SwiftUI `App`/`Settings { }` scene API, so a real
/// Preferences window is the native equivalent here — same end result
/// (a proper native preferences window), just via NSWindowController
/// instead of a Scene.
@MainActor
class SettingsWindowController: NSWindowController {
    convenience init(store: CopitaStore) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 600, height: 620),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Copita Preferences"
        window.center()
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: SettingsView(store: store))
        self.init(window: window)
    }

    func show() {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

import Cocoa
import Combine
import UserNotifications

@MainActor
class AppDelegate: NSObject, NSApplicationDelegate {
    static var shared: AppDelegate?
    var statusItem: NSStatusItem?
    var mainWindowController: MainWindowController!
    var workspacePath: String = ""
    private var storeSubscription: AnyCancellable?
    // Set when a copita://download link is what woke the app up from cold
    // -- an extension click launching Copita to catch a download should
    // behave like a classic download-manager tray icon: start quietly in the menu bar and do
    // the work, not yank the main window in front of whatever the user
    // was doing in the browser.
    private var launchedForBackgroundDownload = false

    // Must be registered before applicationDidFinishLaunching returns: a
    // cold launch triggered by opening a copita:// link delivers its
    // GetURL Apple Event around this point, and a handler installed any
    // later (e.g. inside applicationDidFinishLaunching itself) can miss it.
    func applicationWillFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleGetURLEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        AppDelegate.shared = self
        setupMainMenu()
        // Resolve workspace path
        workspacePath = resolveWorkspacePath()
        print("[Copita] Resolved workspace: \(workspacePath)")

        // Start backend if needed
        BackendManager.shared.startIfNeeded(workspacePath: workspacePath)
        
        // LSUIElement=true in Info.plist does NOT actually make
        // NSApp.activationPolicy default to .accessory on its own --
        // confirmed live via a debug log: at the very start of this method
        // it still reports .regular (rawValue 0) even with that key set.
        // AppKit's own bootstrap starts .regular regardless; something has
        // to explicitly flip it. So this still needs an explicit call for
        // the background-launch case, same as before -- LSUIElement is
        // only useful here for whatever tiny head start it gives before
        // Swift code even starts running.
        NSApp.setActivationPolicy(launchedForBackgroundDownload ? .accessory : .regular)

        // Setup Window
        mainWindowController = MainWindowController()
        mainWindowController.setupWindow()
        mainWindowController.loadApp()
        if !launchedForBackgroundDownload {
            mainWindowController.showWindow()
        }

        // Ensure AppIcon is active in Dock
        if let iconPath = Bundle.main.path(forResource: "AppIcon", ofType: "icns"),
           let iconImg = NSImage(contentsOfFile: iconPath) {
            NSApp.applicationIconImage = iconImg
        }
        
        // Setup Menu Bar Item
        setupStatusItem()

        // "Download with Copita" in the system Services menu (see NSServices
        // in Info.plist) -- registering the provider is what actually wires
        // the downloadWithCopita(_:userData:error:) selector up.
        NSApp.servicesProvider = self

        // Live progress in the menu bar, download-manager-style — a
        // download shouldn't be invisible just because the main window
        // isn't open. Rebuilds the dropdown's task rows on every task
        // update, whether or not the window is currently visible.
        storeSubscription = mainWindowController.store.$tasks
            .receive(on: DispatchQueue.main)
            .sink { [weak self] tasks in
                self?.rebuildStatusMenu(tasks: tasks)
            }

        // Request Notification Permission
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }
    
    private func resolveWorkspacePath() -> String {
        // 1. Two levels up from bundle (when in copita-macos/Copita.app)
        let twoLevelsUp = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().path
        if FileManager.default.fileExists(atPath: (twoLevelsUp as NSString).appendingPathComponent("copita")) {
            return twoLevelsUp
        }
        
        // 2. Resource path inside bundle
        if let resPath = Bundle.main.resourcePath,
           FileManager.default.fileExists(atPath: (resPath as NSString).appendingPathComponent("copita")) {
            return resPath
        }
        
        // 3. Known workspace path
        let hardcoded = "/Users/nurik/Downloads/copita downloader"
        if FileManager.default.fileExists(atPath: (hardcoded as NSString).appendingPathComponent("copita")) {
            return hardcoded
        }
        
        // 4. Fallback to currentDirectoryPath
        return FileManager.default.currentDirectoryPath
    }
    
    // The app never had a real application menu — only the status-bar
    // dropdown existed. Without this, Cmd+Q/Cmd+,/Cmd+W/Cmd+H and even
    // ordinary Cut/Copy/Paste/Select All inside text fields aren't
    // guaranteed to work, since AppKit doesn't synthesize a menu bar for a
    // plain NSApplication built outside Xcode's default template.
    func setupMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenuItem.submenu = appMenu
        appMenu.addItem(NSMenuItem(title: "About Copita Downloader", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: ""))
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(NSMenuItem(title: "Preferences…", action: #selector(openPreferences), keyEquivalent: ","))
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(NSMenuItem(title: "Hide Copita Downloader", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"))
        let hideOthers = NSMenuItem(title: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(hideOthers)
        appMenu.addItem(NSMenuItem(title: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: ""))
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(NSMenuItem(title: "Quit Copita Downloader", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "Edit")
        editMenuItem.submenu = editMenu
        editMenu.addItem(NSMenuItem(title: "Undo", action: Selector(("undo:")), keyEquivalent: "z"))
        let redo = NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(redo)
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))

        let windowMenuItem = NSMenuItem()
        mainMenu.addItem(windowMenuItem)
        let windowMenu = NSMenu(title: "Window")
        windowMenuItem.submenu = windowMenu
        windowMenu.addItem(NSMenuItem(title: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
        windowMenu.addItem(NSMenuItem(title: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        windowMenu.addItem(NSMenuItem.separator())
        windowMenu.addItem(NSMenuItem(title: "Bring All to Front", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: ""))

        NSApp.mainMenu = mainMenu
        NSApp.windowsMenu = windowMenu
    }

    func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        
        if let button = statusItem?.button {
            var iconImg: NSImage? = nil
            
            // Prefer high-DPI @2x and 1x composite for crisp Retina rendering
            let resPath = Bundle.main.resourcePath ?? ""
            let path2x = (resPath as NSString).appendingPathComponent("MenuBarIcon@2x.png")
            let path1x = (resPath as NSString).appendingPathComponent("MenuBarIcon.png")
            
            if FileManager.default.fileExists(atPath: path2x) && FileManager.default.fileExists(atPath: path1x),
               let rep2x = NSImageRep(contentsOfFile: path2x),
               let rep1x = NSImageRep(contentsOfFile: path1x) {
                let composite = NSImage(size: NSSize(width: 18, height: 18))
                composite.addRepresentation(rep1x)
                composite.addRepresentation(rep2x)
                composite.isTemplate = false // Preserve vibrant orange color
                iconImg = composite
            } else if let iconPath = Bundle.main.path(forResource: "MenuBarIcon", ofType: "png"),
                      let loaded = NSImage(contentsOfFile: iconPath) {
                loaded.size = NSSize(width: 18, height: 18)
                loaded.isTemplate = false
                iconImg = loaded
            }
            
            if let img = iconImg {
                button.image = img
                button.imagePosition = .imageOnly
            } else {
                button.title = "⬇"
            }
        }
        
        rebuildStatusMenu(tasks: [])
    }

    // Rebuilt on every task update (see the $tasks subscription in
    // applicationDidFinishLaunching) so the dropdown always reflects
    // what's actually happening — a real download-manager-style tray
    // progress view, not a static action list that gives no sign
    // anything is downloading unless the main window happens to be open.
    private func rebuildStatusMenu(tasks: [CopitaTask]) {
        let active = tasks
            .filter { DownloadStatus.nonTerminal.contains($0.status) }
            .sorted { $0.createdAt > $1.createdAt }

        let menu = NSMenu()

        if active.isEmpty {
            let idle = NSMenuItem(title: "No Active Downloads", action: nil, keyEquivalent: "")
            idle.isEnabled = false
            menu.addItem(idle)
        } else {
            let header = NSMenuItem(
                title: active.count == 1 ? "1 Active Download" : "\(active.count) Active Downloads",
                action: nil, keyEquivalent: ""
            )
            header.isEnabled = false
            menu.addItem(header)
            menu.addItem(NSMenuItem.separator())

            for task in active.prefix(6) {
                let progressText: String
                switch task.status {
                case "downloading":
                    progressText = "\(Int(task.percent))%"
                case "analyzing":
                    progressText = task.engine == "torrent" ? "Connecting…" : "Scanning…"
                case "merging":
                    progressText = "Finishing…"
                case "queued":
                    progressText = "Queued"
                case "paused":
                    progressText = "Paused"
                default:
                    progressText = task.status.capitalized
                }
                var title = task.title
                if title.count > 42 { title = String(title.prefix(41)) + "…" }
                let item = NSMenuItem(title: "\(title) — \(progressText)", action: #selector(openMainWindow), keyEquivalent: "")
                item.isEnabled = true
                item.toolTip = task.title
                menu.addItem(item)
            }
            if active.count > 6 {
                let more = NSMenuItem(title: "+ \(active.count - 6) more…", action: #selector(openMainWindow), keyEquivalent: "")
                menu.addItem(more)
            }
        }

        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Open Copita Downloader", action: #selector(openMainWindow), keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "Paste & Download", action: #selector(pasteAndDownload), keyEquivalent: "v"))
        menu.addItem(NSMenuItem(title: "Open Downloads Folder", action: #selector(openDownloadsFolder), keyEquivalent: "d"))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Preferences...", action: #selector(openPreferences), keyEquivalent: ","))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit Copita Downloader", action: #selector(quitApp), keyEquivalent: "q"))

        statusItem?.menu = menu

        // A glance at the Dock badge already shows the count; the menu bar
        // icon itself picking up a subtle motion/tooltip cue means you
        // don't need to open the menu at all to know something's moving.
        statusItem?.button?.toolTip = active.isEmpty
            ? "Copita Downloader"
            : "Copita — \(active.count) active"
    }
    
    // The Dock-icon-free `.accessory` mode a background launch starts in
    // only applies until the user actually asks to see the app -- clicking
    // anything in the status-bar menu that shows a window is exactly that
    // ask, so it promotes back to a normal app with a Dock icon first,
    // same as a tray icon handing off to its full window on demand.
    private func presentAsRegularApp() {
        if NSApp.activationPolicy() != .regular {
            NSApp.setActivationPolicy(.regular)
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func openPreferences() {
        presentAsRegularApp()
        mainWindowController.openPreferences()
    }

    @objc func openMainWindow() {
        presentAsRegularApp()
        mainWindowController.showWindow()
    }
    
    // Handles copita://download?url=<encoded> -- fired either as part of a
    // cold launch (the browser extension found nothing on 127.0.0.1:8888
    // and asked the OS to open Copita) or, if Copita is already running,
    // delivered directly to this same handler with no relaunch at all.
    @objc private func handleGetURLEvent(_ event: NSAppleEventDescriptor, withReplyEvent: NSAppleEventDescriptor) {
        guard let raw = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue,
              let components = URLComponents(string: raw),
              components.scheme == "copita" else { return }

        if components.host == "download",
           let target = components.queryItems?.first(where: { $0.name == "url" })?.value,
           !target.isEmpty {
            launchedForBackgroundDownload = true
            triggerDownloadWhenReady(urlString: target)
        } else if components.host == "open" {
            // The browser extension's "Open app" button — just surface the window.
            NSApp.activate(ignoringOtherApps: true)
            openMainWindow()
        }
    }

    // The backend takes a moment to bind its port after BackendManager
    // spawns it -- a download handed off right at cold launch needs to
    // wait that out rather than fail on the first connection refusal.
    private func triggerDownloadWhenReady(urlString: String, attemptsLeft: Int = 12, onDone: ((Bool) -> Void)? = nil) {
        guard let apiURL = URL(string: "http://127.0.0.1:8888/api/download") else { return }
        var request = URLRequest(url: apiURL)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["url": urlString])
        request.timeoutInterval = 3

        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            if ok || attemptsLeft <= 0 {
                if !ok {
                    print("[Copita] Extension-triggered download failed after retries: \(String(describing: error))")
                }
                DispatchQueue.main.async { onDone?(ok) }
                return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self?.triggerDownloadWhenReady(urlString: urlString, attemptsLeft: attemptsLeft - 1, onDone: onDone)
            }
        }.resume()
    }

    // Registered via NSServices in Info.plist -- appears as "Download with
    // Copita" in the Services submenu wherever text is selected (Finder's
    // path bar, TextEdit, Notes, Mail, anywhere), not just inside the app
    // itself. macOS launches Copita automatically to deliver this if it
    // isn't already running. Unlike the extension's background launch,
    // this is always a deliberate, named action the user picked from a
    // menu, so it behaves like Paste & Download: show the window so the
    // download is visibly happening, not silent.
    @objc func downloadWithCopita(_ pboard: NSPasteboard, userData: String, error: AutoreleasingUnsafeMutablePointer<NSString>) {
        guard let text = pboard.string(forType: .string) else {
            error.pointee = "Copita couldn't read the selected text." as NSString
            return
        }
        let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
        let range = NSRange(text.startIndex..., in: text)
        guard let match = detector?.firstMatch(in: text, range: range),
              let urlRange = Range(match.range, in: text) else {
            error.pointee = "No link found in the selected text." as NSString
            return
        }
        let urlString = String(text[urlRange])
        triggerDownloadWhenReady(urlString: urlString) { [weak self] ok in
            guard ok else { return }
            self?.presentAsRegularApp()
            self?.mainWindowController.showWindow()
        }
    }

    @objc func pasteAndDownload() {
        guard let clip = NSPasteboard.general.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines),
              clip.hasPrefix("http") else {
            return
        }
        
        guard let apiURL = URL(string: "http://127.0.0.1:8888/api/download") else { return }
        var request = URLRequest(url: apiURL)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let payload: [String: Any] = ["url": clip, "threads": 16]
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            DispatchQueue.main.async {
                self?.presentAsRegularApp()
                self?.mainWindowController.showWindow()
            }
        }.resume()
    }

    // Was locally computing workspacePath + "/downloads" -- silently wrong
    // as soon as the backend's real download directory (see the matching
    // fix in copita/api/server.py) stopped being derived from wherever the
    // app binary happens to run from. Routing through the backend's own
    // reported systemInfo.downloadDir (the same source CopitaStore's own
    // openDownloadsFolder() already correctly uses) means there's exactly
    // one place that decides where downloads live, not two that can drift.
    @objc func openDownloadsFolder() {
        mainWindowController.store.openDownloadsFolder()
    }
    
    @objc func quitApp() {
        NSApp.terminate(nil)
    }
    
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        mainWindowController.showWindow()
        return true
    }
    
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }
    
    func applicationWillTerminate(_ notification: Notification) {
        BackendManager.shared.stop()
    }
}

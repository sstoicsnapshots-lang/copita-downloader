import Cocoa

/// Chrome and Brave deliberately give no API for an outside app to drop an
/// extension into the browser without the user clicking through Developer
/// Mode + Load Unpacked themselves -- that's an intentional anti-malware
/// boundary, not something to work around. This gets the user to that
/// exact point with zero guesswork instead: detect the browser, open its
/// extensions page, and reveal the folder to drag in.
enum BrowserExtensionInstaller {
    struct DetectedBrowser: Identifiable {
        let id: String
        let name: String
        let appURL: URL
        let extensionsURL: URL
    }

    private static let candidates: [(appPath: String, name: String, scheme: String)] = [
        ("/Applications/Google Chrome.app", "Google Chrome", "chrome://extensions"),
        ("/Applications/Brave Browser.app", "Brave Browser", "brave://extensions"),
    ]

    static func detectedBrowsers() -> [DetectedBrowser] {
        candidates.compactMap { candidate in
            guard FileManager.default.fileExists(atPath: candidate.appPath),
                  let extURL = URL(string: candidate.scheme) else { return nil }
            return DetectedBrowser(
                id: candidate.appPath,
                name: candidate.name,
                appURL: URL(fileURLWithPath: candidate.appPath),
                extensionsURL: extURL
            )
        }
    }

    /// Prefers the copy build.sh bundles into Resources -- it's rebuilt
    /// fresh every build, so it's always exactly what this running copy of
    /// the app actually ships, whether that's a dev build, a DMG install,
    /// or /Applications. The live project folder is only a fallback for
    /// the (normally unreachable) case where the bundle is somehow
    /// missing it -- checking it *first* pointed a real DMG install at
    /// this one machine's dev source tree instead of the app's own copy,
    /// caught live.
    static func extensionFolderURL(workspacePath: String) -> URL? {
        if let resPath = Bundle.main.resourcePath {
            let bundled = (resPath as NSString).appendingPathComponent("copita-extension")
            if FileManager.default.fileExists(atPath: bundled) {
                return URL(fileURLWithPath: bundled)
            }
        }
        let devPath = (workspacePath as NSString).appendingPathComponent("copita-extension")
        if FileManager.default.fileExists(atPath: devPath) {
            return URL(fileURLWithPath: devPath)
        }
        return nil
    }

    static func revealExtensionFolder(workspacePath: String) {
        guard let url = extensionFolderURL(workspacePath: workspacePath) else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    /// chrome:// isn't a URL scheme macOS itself knows how to route -- it
    /// only means anything inside a Chromium browser -- so this launches
    /// the browser directly with the URL as an argument (exactly what
    /// happens when Chrome itself opens a chrome:// link) rather than
    /// asking the OS to resolve the scheme.
    static func openExtensionsPage(_ browser: DetectedBrowser) {
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        NSWorkspace.shared.open([browser.extensionsURL], withApplicationAt: browser.appURL, configuration: config)
    }
}

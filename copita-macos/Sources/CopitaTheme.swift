import SwiftUI

/// Design tokens for the native app. Deliberately not a 1:1 port of the web
/// app's CSS values — see notes/CONTINUATION.md and the plan this was built
/// from for why: native semantic colors (`.primary`, `.green`, `.red`)
/// replace the web app's hardcoded (and inconsistent — three different
/// greens across its stylesheet) hex values, and real NSVisualEffectView
/// materials replace its faked `backdrop-filter` blur. The one color kept
/// literally is the brand accent orange.
enum CopitaTheme {
    static let accent = Color(hex: 0xEA580C)
    static let accentHover = Color(hex: 0xF97316)

    // Corner radii
    static let cardRadius: CGFloat = 11
    static let controlRadius: CGFloat = 8
    static let pillRadius: CGFloat = 26
    static let iconBoxRadius: CGFloat = 9

    // Sizing
    static let iconBoxSize = CGSize(width: 36, height: 40)
    static let sidebarWidth: CGFloat = 230
    static let inputBarHeight: CGFloat = 44
    static let pillHeight: CGFloat = 52
    static let pillWidth: CGFloat = 580

    // Spacing
    static let contentPadding: CGFloat = 24
    static let rowSpacing: CGFloat = 8
}

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: opacity
        )
    }
}

/// Bridges an AppKit vibrancy material into SwiftUI. Real translucency,
/// not an approximation — this is the thing a WKWebView-based app can only
/// fake with CSS.
struct VisualEffectView: NSViewRepresentable {
    var material: NSVisualEffectView.Material = .sidebar
    var blendingMode: NSVisualEffectView.BlendingMode = .behindWindow

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}

/// A row status color/icon pairing, computed once per status string so
/// TaskRowView and any future summary UI (the floating pill) stay in sync.
struct StatusVisual {
    let symbol: String
    let color: Color

    init(status: String) {
        switch status {
        case "completed":
            symbol = "checkmark.circle.fill"
            color = .green
        case "failed":
            symbol = "exclamationmark.triangle.fill"
            color = .orange
        case "paused":
            symbol = "pause.circle.fill"
            color = .secondary
        case "queued":
            symbol = "clock.fill"
            color = .secondary
        default:
            symbol = "arrow.down.circle.fill"
            color = CopitaTheme.accent
        }
    }
}

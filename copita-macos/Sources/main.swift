import Cocoa

@MainActor
func launchCopita() {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}

// main.swift's top-level statements execute on the main thread at process
// startup, but aren't implicitly @MainActor-isolated under strict
// concurrency checking — assumeIsolated documents (and asserts) that this
// really is the main thread rather than fighting the type system over it.
MainActor.assumeIsolated {
    launchCopita()
}

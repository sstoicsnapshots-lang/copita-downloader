import Foundation

class BackendManager {
    static let shared = BackendManager()
    private var process: Process?
    let serverURL = URL(string: "http://127.0.0.1:8888")!
    
    var isRunning: Bool {
        guard let url = URL(string: "http://127.0.0.1:8888/api/system") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.8
        let semaphore = DispatchSemaphore(value: 0)
        var alive = false
        
        let task = URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                alive = true
            }
            semaphore.signal()
        }
        task.resume()
        _ = semaphore.wait(timeout: .now() + 1.0)
        return alive
    }
    
    func startIfNeeded(workspacePath: String) {
        if isRunning {
            print("[Copita] Backend is already active on port 8888.")
            return
        }
        
        print("[Copita] Launching background Python engine from \(workspacePath)...")
        let p = Process()
        
        let pythonPath = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
        let execPath = FileManager.default.fileExists(atPath: pythonPath) ? pythonPath : "/usr/bin/python3"
        
        p.executableURL = URL(fileURLWithPath: execPath)
        p.arguments = ["-m", "uvicorn", "copita.api.server:app", "--host", "127.0.0.1", "--port", "8888"]
        p.currentDirectoryURL = URL(fileURLWithPath: workspacePath)
        
        var env = ProcessInfo.processInfo.environment
        let extraPaths = [
            "/opt/homebrew/bin",
            "/Library/Frameworks/Python.framework/Versions/3.14/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin"
        ]
        let currentPath = env["PATH"] ?? ""
        env["PATH"] = (extraPaths + [currentPath]).joined(separator: ":")
        
        // Deliberately NOT appending any inherited PYTHONPATH here. A
        // Terminal-launched process and a LaunchServices (`open`/Dock/
        // extension) launch of the exact same app do not inherit the same
        // environment -- the latter picks up launchd's session-wide
        // environment instead of shell rc files, and on this machine that
        // included a stale PYTHONPATH entry pointing at an unavailable
        // directory. Python's import machinery calls os.listdir() on every
        // sys.path entry while resolving `-m uvicorn`, and one blocking
        // opendir() on a dead/network path hung the entire backend forever
        // with zero output -- reproduced live via `sample`, whose stack
        // trace showed the process stuck inside
        // PyImport_ImportModuleLevelObject -> os_listdir -> opendir the
        // whole time it looked "just slow to start." Copita's backend is
        // fully self-contained in workspacePath + the bundle's own
        // resources, so nothing external ever needs to be on this path.
        var pythonPaths = [workspacePath, Bundle.main.resourcePath].compactMap { $0 }
        // libtorrent has no pip wheel for this app's Python version yet, so
        // it installs through Homebrew's libtorrent-rasterbar formula
        // instead (see requirements_checker.py) -- that puts the compiled
        // module in Homebrew's own Python 3.14 site-packages, which this
        // app's Python (a separate install at
        // /Library/Frameworks/Python.framework) never sees on its own.
        // This is a fixed, known local path (unlike the removed inherited
        // PYTHONPATH above) so it doesn't carry that same hang risk.
        let brewSitePackages = "/opt/homebrew/lib/python3.14/site-packages"
        if FileManager.default.fileExists(atPath: brewSitePackages) {
            pythonPaths.append(brewSitePackages)
        }
        env["PYTHONPATH"] = pythonPaths.joined(separator: ":")
        p.environment = env
        
        // Log output to /tmp/copita_backend.log
        let logPath = "/tmp/copita_backend.log"
        FileManager.default.createFile(atPath: logPath, contents: nil)
        if let handle = FileHandle(forWritingAtPath: logPath) {
            p.standardOutput = handle
            p.standardError = handle
        }
        
        do {
            try p.run()
            self.process = p
            print("[Copita] Backend started with PID \(p.processIdentifier)")
        } catch {
            print("[Copita] Failed to launch backend: \(error)")
        }
    }

    func stop() {
        if let p = process, p.isRunning {
            print("[Copita] Stopping backend process PID \(p.processIdentifier)...")
            p.terminate()
            process = nil
        }
    }
}

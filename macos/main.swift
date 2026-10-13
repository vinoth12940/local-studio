// Local Studio — native macOS shell for the local web app.
// The app bundle carries the code (Resources/app). On launch it checks the Mac and, on first
// run, shows a setup screen that installs the engines. Then it starts the servers, shows the
// UI in a WKWebView, and stops everything on quit unless "Keep Running After Quit" is on.

import Cocoa
import WebKit

let uiURL = URL(string: "http://127.0.0.1:7860/")!
let probeURL = URL(string: "http://127.0.0.1:7860/api/system")!
let keepRunningKey = "KeepRunningAfterQuit"
let supportDir = NSHomeDirectory() + "/Library/Application Support/Local Studio"

/// Bundled code (Resources/app) when present; otherwise the dev checkout recorded at build time.
var bundled: Bool { FileManager.default.fileExists(atPath: (Bundle.main.resourcePath ?? "") + "/app/start.sh") }
func codeDir() -> String {
    if bundled { return Bundle.main.resourcePath! + "/app" }
    if let p = Bundle.main.object(forInfoDictionaryKey: "LSAppRoot") as? String,
       FileManager.default.fileExists(atPath: p + "/start.sh") { return p }
    return NSHomeDirectory() + "/local-studio"
}
func stateDir() -> String { bundled ? supportDir : codeDir() }

func scriptEnv() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = NSHomeDirectory() + "/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["LOCAL_STUDIO_HOME"] = stateDir()
    env["UV_NO_MODIFY_PATH"] = "1"
    return env
}

/// Runs a bundled script. `onLine` receives stdout/stderr lines as they arrive.
@discardableResult
func runScript(_ name: String, args: [String] = [], wait: Bool = false,
               onLine: ((String) -> Void)? = nil, onExit: ((Int32) -> Void)? = nil) -> Int32 {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/bin/zsh")
    task.arguments = [codeDir() + "/" + name] + args
    task.currentDirectoryURL = URL(fileURLWithPath: codeDir())
    task.environment = scriptEnv()
    if let onLine = onLine {
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        var buffer = Data()
        pipe.fileHandleForReading.readabilityHandler = { h in
            let chunk = h.availableData
            if chunk.isEmpty { return }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = String(decoding: buffer[buffer.startIndex..<nl], as: UTF8.self)
                buffer.removeSubrange(buffer.startIndex...nl)
                onLine(line)
            }
        }
    } else {
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
    }
    task.terminationHandler = { t in onExit?(t.terminationStatus) }
    do { try task.run() } catch { onExit?(-1); return -1 }
    if wait { task.waitUntilExit(); return task.terminationStatus }
    return 0
}

func label(_ s: String, size: CGFloat = 13, weight: NSFont.Weight = .regular, color: NSColor = .labelColor) -> NSTextField {
    let l = NSTextField(wrappingLabelWithString: s)
    l.font = .systemFont(ofSize: size, weight: weight)
    l.textColor = color
    l.isSelectable = false
    return l
}

// MARK: - Loading screen

final class LoadingView: NSView {
    let title = label("Starting Local Studio…", size: 17, weight: .semibold)
    let detail = label("Loading the engine and interface", color: .secondaryLabelColor)
    let spinner = NSProgressIndicator()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        spinner.style = .spinning
        spinner.startAnimation(nil)
        title.alignment = .center; detail.alignment = .center
        let stack = NSStackView(views: [spinner, title, detail])
        stack.orientation = .vertical; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    func fail(_ message: String) {
        spinner.stopAnimation(nil); spinner.isHidden = true
        title.stringValue = "Local Studio couldn't start"
        detail.stringValue = message
    }
}

// MARK: - First-run setup screen

final class SetupView: NSView {
    let checks = NSStackView()
    let progress = NSProgressIndicator()
    let status = label("", weight: .medium)
    let logLine = label("", size: 11.5, color: .secondaryLabelColor)
    let primary = NSButton(title: "Install", target: nil, action: nil)
    let secondary = NSButton(title: "Quit", target: nil, action: nil)
    let headline = label("Welcome to Local Studio", size: 24, weight: .semibold)
    let subtitle = label("Private AI chat and image generation that runs entirely on your Mac. "
                         + "Setup installs the engines (about 3 GB, a few minutes). You'll pick models to download next.",
                         size: 13.5, color: .secondaryLabelColor)

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let icon = NSImageView(image: NSApp.applicationIconImage ?? NSImage())
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 96).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 96).isActive = true

        checks.orientation = .vertical; checks.alignment = .leading; checks.spacing = 8
        let box = NSBox(); box.titlePosition = .noTitle; box.contentViewMargins = NSSize(width: 16, height: 14)
        box.contentView = checks; box.boxType = .custom; box.cornerRadius = 12
        box.borderColor = .separatorColor; box.fillColor = .controlBackgroundColor
        box.translatesAutoresizingMaskIntoConstraints = false

        progress.isIndeterminate = false; progress.minValue = 0; progress.maxValue = 1
        progress.isHidden = true
        logLine.lineBreakMode = .byTruncatingTail; logLine.maximumNumberOfLines = 1
        primary.bezelStyle = .rounded; primary.keyEquivalent = "\r"; primary.controlSize = .large
        secondary.bezelStyle = .rounded; secondary.controlSize = .large
        let buttons = NSStackView(views: [secondary, primary]); buttons.spacing = 10

        let stack = NSStackView(views: [icon, headline, subtitle, box, progress, status, logLine, buttons])
        stack.orientation = .vertical; stack.alignment = .centerX; stack.spacing = 14
        stack.setCustomSpacing(18, after: icon); stack.setCustomSpacing(22, after: subtitle)
        stack.setCustomSpacing(22, after: logLine)
        headline.alignment = .center; subtitle.alignment = .center; status.alignment = .center; logLine.alignment = .center
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.widthAnchor.constraint(equalToConstant: 460),
            box.widthAnchor.constraint(equalTo: stack.widthAnchor),
            progress.widthAnchor.constraint(equalTo: stack.widthAnchor),
            subtitle.widthAnchor.constraint(equalTo: stack.widthAnchor),
            logLine.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    /// One row per `@check <state> <name> <detail>` line.
    func setChecks(_ lines: [String]) {
        checks.arrangedSubviews.forEach { $0.removeFromSuperview() }
        for line in lines {
            let parts = line.split(separator: " ", maxSplits: 3).map(String.init)
            guard parts.count >= 3 else { continue }
            let state = parts[1], name = parts[2], detail = parts.count > 3 ? parts[3] : ""
            let symbol: String, tint: NSColor
            switch state {
            case "ok": symbol = "checkmark.circle.fill"; tint = .systemGreen
            case "warn": symbol = "exclamationmark.triangle.fill"; tint = .systemOrange
            case "fail": symbol = "xmark.octagon.fill"; tint = .systemRed
            default: symbol = "arrow.down.circle"; tint = .secondaryLabelColor
            }
            let img = NSImageView(image: NSImage(systemSymbolName: symbol, accessibilityDescription: state) ?? NSImage())
            img.contentTintColor = tint
            let n = label(name, weight: .medium)
            let d = label(state == "missing" ? "\(detail) · will be installed" : detail, color: .secondaryLabelColor)
            let row = NSStackView(views: [img, n, d]); row.spacing = 8
            checks.addArrangedSubview(row)
        }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var loading: LoadingView!
    var setup: SetupView!
    var keepItem: NSMenuItem!
    var setupLog: FileHandle?

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Local Studio"
        window.minSize = NSSize(width: 760, height: 560)
        window.center()
        window.setFrameAutosaveName("LocalStudioMain")
        window.isReleasedWhenClosed = false
        loading = LoadingView(frame: window.contentView!.bounds)
        setup = SetupView(frame: window.contentView!.bounds)
        setup.primary.target = self; setup.primary.action = #selector(runInstall)
        setup.secondary.target = self; setup.secondary.action = #selector(quitOrCancel)
        loading.detail.stringValue = "Checking this Mac"
        window.contentView = loading
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        checkThenStart()
    }

    // MARK: setup flow

    func checkThenStart(forceSetup: Bool = false) {
        guard FileManager.default.fileExists(atPath: codeDir() + "/setup.sh") else {
            loading.fail("The app is incomplete (setup.sh missing). Please reinstall Local Studio.")
            return
        }
        var lines: [String] = []
        runScript("setup.sh", args: ["--check"], onLine: { l in if l.hasPrefix("@check") { lines.append(l) } },
                  onExit: { code in
            DispatchQueue.main.async {
                if code == 0 && !forceSetup {
                    runScript("setup.sh", args: ["--migrate"], wait: true)
                    self.startServersAndLoad(); return
                }
                self.setup.setChecks(lines)
                if code == 2 {
                    self.setup.headline.stringValue = "This Mac isn't supported"
                    self.setup.subtitle.stringValue = "Local Studio needs a Mac with Apple Silicon (M1 or newer) and macOS 14 or later."
                    self.setup.primary.isHidden = true
                } else if code == 0 {
                    self.setup.headline.stringValue = "Everything is installed"
                    self.setup.subtitle.stringValue = "Reinstall the engines to repair or update them."
                    self.setup.primary.title = "Reinstall"
                }
                self.window.contentView = self.setup
            }
        })
    }

    @objc func runInstall() {
        setup.primary.isEnabled = false
        setup.secondary.title = "Cancel"
        setup.progress.isHidden = false
        setup.progress.doubleValue = 0.02
        setup.status.stringValue = "Starting…"
        try? FileManager.default.createDirectory(atPath: stateDir() + "/logs", withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: stateDir() + "/logs/setup.log", contents: nil)
        setupLog = FileHandle(forWritingAtPath: stateDir() + "/logs/setup.log")
        var failed: String?
        runScript("setup.sh", args: ["--install"], onLine: { line in
            self.setupLog?.write((line + "\n").data(using: .utf8)!)
            DispatchQueue.main.async {
                if line.hasPrefix("@step ") {
                    let p = line.split(separator: " ", maxSplits: 3).map(String.init)
                    if p.count == 4, let n = Double(p[1]), let t = Double(p[2]) {
                        self.setup.progress.doubleValue = (n - 1) / t + 0.02
                        self.setup.status.stringValue = "Step \(Int(n)) of \(Int(t)) · \(p[3])"
                    }
                } else if line.hasPrefix("@log ") {
                    self.setup.logLine.stringValue = String(line.dropFirst(5))
                } else if line.hasPrefix("@error ") {
                    failed = String(line.dropFirst(7))
                }
            }
        }, onExit: { code in
            DispatchQueue.main.async {
                try? self.setupLog?.close()
                if code == 0 && failed == nil {
                    self.setup.progress.doubleValue = 1
                    self.setup.status.stringValue = "All set"
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { self.startServersAndLoad() }
                } else {
                    self.setup.status.stringValue = "Setup didn't finish: \(failed ?? "exit code \(code)")"
                    self.setup.logLine.stringValue = "Details: \(stateDir())/logs/setup.log"
                    self.setup.primary.title = "Try Again"
                    self.setup.primary.isEnabled = true
                    self.setup.secondary.title = "Quit"
                }
            }
        })
    }

    @objc func quitOrCancel() { NSApp.terminate(nil) }

    func startServersAndLoad() {
        loading.title.stringValue = "Starting Local Studio…"
        loading.detail.stringValue = "Loading the engine and interface"
        loading.spinner.isHidden = false; loading.spinner.startAnimation(nil)
        window.contentView = loading
        DispatchQueue.global().async {
            if !self.uiIsUp() { runScript("start.sh") }
            let deadline = Date().addingTimeInterval(180)
            while Date() < deadline {
                if self.uiIsUp() { DispatchQueue.main.async { self.showUI() }; return }
                Thread.sleep(forTimeInterval: 0.5)
            }
            DispatchQueue.main.async {
                self.loading.fail("The interface didn't respond. See \(stateDir())/logs/ui.log")
            }
        }
    }

    func uiIsUp() -> Bool {
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        var req = URLRequest(url: probeURL)
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        sem.wait()
        return ok
    }

    func showUI() {
        // Skip the web cache: after an update a cached copy of the old page would hide new features.
        webView.load(URLRequest(url: uiURL, cachePolicy: .reloadIgnoringLocalCacheData))
        window.contentView = webView
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { window.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if UserDefaults.standard.bool(forKey: keepRunningKey) || window.contentView === setup { return .terminateNow }
        loading.title.stringValue = "Stopping Local Studio…"
        loading.detail.stringValue = "Freeing memory used by the models"
        window.contentView = loading
        DispatchQueue.global().async {
            runScript("stop.sh", wait: true)
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    // MARK: menus

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let app = NSMenu()
        app.addItem(withTitle: "About Local Studio", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        keepItem = NSMenuItem(title: "Keep Running After Quit", action: #selector(toggleKeep), keyEquivalent: "")
        keepItem.target = self
        keepItem.state = UserDefaults.standard.bool(forKey: keepRunningKey) ? .on : .off
        app.addItem(keepItem)
        let repair = NSMenuItem(title: "Repair or Update Engines…", action: #selector(showSetup), keyEquivalent: "")
        repair.target = self
        app.addItem(repair)
        let folder = NSMenuItem(title: "Show Data Folder", action: #selector(showFolder), keyEquivalent: "")
        folder.target = self
        app.addItem(folder)
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Local Studio", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = app.addItem(withTitle: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        app.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit Local Studio", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = app

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        let reload = NSMenuItem(title: "Reload", action: #selector(reloadPage), keyEquivalent: "r"); reload.target = self
        view.addItem(reload)
        let zin = NSMenuItem(title: "Zoom In", action: #selector(zoomIn), keyEquivalent: "+"); zin.target = self
        let zout = NSMenuItem(title: "Zoom Out", action: #selector(zoomOut), keyEquivalent: "-"); zout.target = self
        let zreset = NSMenuItem(title: "Actual Size", action: #selector(zoomReset), keyEquivalent: "0"); zreset.target = self
        view.addItem(.separator()); view.addItem(zin); view.addItem(zout); view.addItem(zreset)
        view.addItem(.separator())
        view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f").keyEquivalentModifierMask = [.command, .control]
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        let show = NSMenuItem(title: "Local Studio", action: #selector(showWindow), keyEquivalent: "1"); show.target = self
        win.addItem(.separator()); win.addItem(show)
        winItem.submenu = win
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    @objc func toggleKeep() {
        let v = !UserDefaults.standard.bool(forKey: keepRunningKey)
        UserDefaults.standard.set(v, forKey: keepRunningKey)
        keepItem.state = v ? .on : .off
    }
    @objc func showSetup() {
        setup.primary.isEnabled = true; setup.primary.isHidden = false; setup.primary.title = "Install"
        setup.secondary.title = "Quit"; setup.progress.isHidden = true; setup.status.stringValue = ""; setup.logLine.stringValue = ""
        checkThenStart(forceSetup: true)
    }
    @objc func showFolder() {
        try? FileManager.default.createDirectory(atPath: stateDir(), withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: stateDir()))
    }
    @objc func reloadPage() { if window.contentView === webView { webView.reloadFromOrigin() } else { checkThenStart() } }
    @objc func zoomIn() { webView.pageZoom = min(webView.pageZoom + 0.1, 2.0) }
    @objc func zoomOut() { webView.pageZoom = max(webView.pageZoom - 0.1, 0.6) }
    @objc func zoomReset() { webView.pageZoom = 1.0 }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil) }

    // MARK: web view

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.shouldPerformDownload { decisionHandler(.download); return }
        if let url = action.request.url, let host = url.host, host != "127.0.0.1", host != "localhost",
           action.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var dest = dir.appendingPathComponent(suggestedFilename)
        var n = 1
        while FileManager.default.fileExists(atPath: dest.path) {
            let base = (suggestedFilename as NSString).deletingPathExtension
            let ext = (suggestedFilename as NSString).pathExtension
            dest = dir.appendingPathComponent("\(base) \(n)" + (ext.isEmpty ? "" : ".\(ext)"))
            n += 1
        }
        completionHandler(dest)
    }
    func downloadDidFinish(_ download: WKDownload) {
        if let url = download.progress.fileURL { NSWorkspace.shared.activateFileViewerSelecting([url]) }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        window.contentView = loading
        loading.fail("Lost connection to the local server. Use View → Reload to restart it.")
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

// Local Studio — native macOS shell for the local web app.
// Starts the servers (start.sh) on launch, shows the UI in a WKWebView, and stops
// everything (stop.sh) on quit unless "Keep Running After Quit" is checked.

import Cocoa
import WebKit

let uiURL = URL(string: "http://127.0.0.1:7860/")!
let probeURL = URL(string: "http://127.0.0.1:7860/api/system")!
let keepRunningKey = "KeepRunningAfterQuit"

/// Folder holding start.sh / stop.sh. Set at build time in Info.plist (LSAppRoot);
/// falls back to ~/local-studio.
func appRoot() -> String {
    if let p = Bundle.main.object(forInfoDictionaryKey: "LSAppRoot") as? String,
       FileManager.default.fileExists(atPath: p + "/start.sh") { return p }
    return NSHomeDirectory() + "/local-studio"
}

@discardableResult
func runScript(_ name: String, args: [String] = [], wait: Bool = false) -> Int32 {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/bin/zsh")
    task.arguments = [appRoot() + "/" + name] + args
    task.currentDirectoryURL = URL(fileURLWithPath: appRoot())
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = NSHomeDirectory() + "/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    task.environment = env
    task.standardOutput = FileHandle.nullDevice
    task.standardError = FileHandle.nullDevice
    do { try task.run() } catch { return -1 }
    if wait { task.waitUntilExit(); return task.terminationStatus }
    return 0
}

final class LoadingView: NSView {
    let label = NSTextField(labelWithString: "Starting Local Studio…")
    let detail = NSTextField(labelWithString: "Loading the engine and interface")
    let spinner = NSProgressIndicator()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.startAnimation(nil)
        label.font = .systemFont(ofSize: 17, weight: .semibold)
        detail.font = .systemFont(ofSize: 13)
        detail.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [spinner, label, detail])
        stack.orientation = .vertical
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    func fail(_ message: String) {
        spinner.stopAnimation(nil)
        spinner.isHidden = true
        label.stringValue = "Local Studio couldn't start"
        detail.stringValue = message
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var loading: LoadingView!
    var keepItem: NSMenuItem!

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Local Studio"
        window.minSize = NSSize(width: 760, height: 520)
        window.center()
        window.setFrameAutosaveName("LocalStudioMain")
        window.isReleasedWhenClosed = false
        loading = LoadingView(frame: window.contentView!.bounds)
        window.contentView = loading
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        startServersAndLoad()
    }

    func startServersAndLoad() {
        guard FileManager.default.fileExists(atPath: appRoot() + "/start.sh") else {
            loading.fail("start.sh not found in \(appRoot()). Reinstall from the repository.")
            return
        }
        DispatchQueue.global().async {
            if !self.uiIsUp() { runScript("start.sh") }
            let deadline = Date().addingTimeInterval(120)
            while Date() < deadline {
                if self.uiIsUp() {
                    DispatchQueue.main.async { self.showUI() }
                    return
                }
                Thread.sleep(forTimeInterval: 0.5)
            }
            DispatchQueue.main.async {
                self.loading.fail("The interface didn't respond. See \(appRoot())/logs/ui.log")
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
        webView.load(URLRequest(url: uiURL))
        window.contentView = webView
    }

    // Close button hides the window; the app keeps running until ⌘Q.
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { window.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if UserDefaults.standard.bool(forKey: keepRunningKey) { return .terminateNow }
        loading.label.stringValue = "Stopping Local Studio…"
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
        let folder = NSMenuItem(title: "Show App Folder", action: #selector(showFolder), keyEquivalent: "")
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
        let reload = NSMenuItem(title: "Reload", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self
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
    @objc func showFolder() { NSWorkspace.shared.open(URL(fileURLWithPath: appRoot())) }
    @objc func reloadPage() { if window.contentView === webView { webView.reload() } else { startServersAndLoad() } }
    @objc func zoomIn() { webView.pageZoom = min(webView.pageZoom + 0.1, 2.0) }
    @objc func zoomOut() { webView.pageZoom = max(webView.pageZoom - 0.1, 0.6) }
    @objc func zoomReset() { webView.pageZoom = 1.0 }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil) }

    // MARK: web view

    // <input type=file> → native open panel (attachments)
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    // window.open / target=_blank → open full-size images in a viewer window of the default browser
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // Keep the app on localhost; anything external opens in the default browser.
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

    // <a download> → save to ~/Downloads
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

    // If the UI server goes away (e.g. after Uninstall), show the loading screen with a hint.
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

import Cocoa
import WebKit

final class LoginApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let state: URL
    var window: NSWindow!
    var web: WKWebView!
    var requestID = ""
    var finished = false
    var displayingError = false
    var timer: Timer?

    init(state: URL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/state/seasnet-vpn")) {
        self.state = state
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit SEASnet VPN Login", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = NSMenuItem()
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let edit = NSMenu(title: "Edit")
        for (title, action, key) in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        editItem.submenu = edit
        menu.addItem(editItem)
        NSApp.mainMenu = menu

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "SEASnet VPN Login"
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        let config = WKWebViewConfiguration()
        web = WKWebView(frame: window.contentView!.bounds, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        window.contentView!.addSubview(web)
        // Never activate or make this window key. cua-bg-launch parks it away
        // from the user's current Space; the user chooses when to interact.
        window.orderBack(nil)
        loadRequest()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.loadRequest() }
    }

    func read(_ name: String) -> [String: Any]? {
        guard let data = try? Data(contentsOf: state.appendingPathComponent(name)) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    func write(_ name: String, _ value: [String: String]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value) else { return }
        let url = state.appendingPathComponent(name)
        do {
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            // Diagnostics are optional. A cancellation write failure cannot
            // expose data or cause an authentication success to be reported.
        }
    }

    func currentRequest() -> (id: String, url: URL)? {
        guard let request = read("login-request.json"), let id = request["id"] as? String,
              !id.isEmpty, let raw = request["url"] as? String,
              let url = URL(string: raw), url.scheme == "https",
              url.user == nil, url.password == nil,
              url.host?.hasSuffix(".ucla.edu") == true else { return nil }
        return (id, url)
    }

    func terminalResult(for id: String) -> Bool {
        guard let result = read("login-result.json"), result["id"] as? String == id,
              let outcome = result["result"] as? String else { return false }
        return ["connected", "failed"].contains(outcome)
    }

    func loadRequest() {
        guard let request = currentRequest() else { return }
        // A running app may be reused by a later SSH attempt. Read that
        // attempt first so an older completed result cannot close its window.
        let changed = request.id != requestID
        requestID = request.id
        if terminalResult(for: requestID) {
            finished = true
            NSApp.terminate(nil)
            return
        }
        guard changed else { return }
        finished = false
        displayingError = false
        window.subtitle = ""
        web.load(URLRequest(url: request.url))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard !displayingError else { return }
        let page: [String: String] = ["id": requestID, "state": "loaded",
                                     "title": webView.title ?? "", "host": webView.url?.host ?? ""]
        write("login-page.json", page)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showNavigationError(error, in: webView)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showNavigationError(error, in: webView)
    }

    func isCancelledNavigation(_ error: NSError) -> Bool {
        (error.domain == NSURLErrorDomain && error.code == NSURLErrorCancelled)
            || (error.domain == "WebKitErrorDomain" && error.code == 102)
    }

    func showNavigationError(_ error: Error, in webView: WKWebView) {
        let failure = error as NSError
        // Redirects, popup replacement, and declining an app handoff cancel
        // the old load. An error page here would interrupt the current login.
        guard !isCancelledNavigation(failure),
              !displayingError, !terminalResult(for: requestID) else { return }
        displayingError = true
        write("login-page.json", ["id": requestID, "state": "failed", "code": String(failure.code)])
        webView.loadHTMLString("<html><body style='font:17px -apple-system;padding:36px'><h2>UCLA login couldn't load</h2><p>Close this window and retry <code>ssh seasnet</code>.</p></body></html>", baseURL: nil)
    }

    func cancel() {
        guard !finished, let request = currentRequest() else { return }
        // Closing the window cancels the latest request, including one that
        // arrived between timer ticks. Never overwrite the helper's result.
        if !terminalResult(for: request.id) {
            write("login-cancel.json", ["id": request.id, "result": "cancelled"])
        }
        finished = true
    }

    func windowWillClose(_ notification: Notification) {
        cancel()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) { cancel() }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        let local = ["127.0.0.1", "localhost", "::1"].contains(url.host ?? "")
        let allowed = url.scheme == "https" || (url.scheme == "http" && local) || url.scheme == "about"
        if !allowed, let scheme = url.scheme, !["http", "file", "data", "javascript"].contains(scheme) {
            // Embedded-browser authentication cannot safely foreground another
            // app. Keep the current login usable for supported Duo methods.
            window.subtitle = "Use Duo Push or a passcode to continue in this window."
        }
        decisionHandler(allowed ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if navigationAction.targetFrame == nil { webView.load(navigationAction.request) }
        return nil
    }
}

let app = NSApplication.shared
let delegate = LoginApp()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

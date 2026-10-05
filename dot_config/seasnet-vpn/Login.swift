import Cocoa
import WebKit

final class LoginApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let state = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/state/seasnet-vpn")
    var window: NSWindow!
    var web: WKWebView!
    var requestID = ""
    var finished = false
    var timer: Timer?

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

    func loadRequest() {
        if let result = read("login-result.json"), result["id"] as? String == requestID {
            finished = true
            NSApp.terminate(nil)
            return
        }
        guard let request = read("login-request.json"), let id = request["id"] as? String,
              id != requestID, let raw = request["url"] as? String,
              let url = URL(string: raw), url.scheme == "https",
              url.host?.hasSuffix(".ucla.edu") == true else { return }
        requestID = id
        finished = false
        web.load(URLRequest(url: url))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let page: [String: String] = ["id": requestID, "state": "loaded",
                                     "title": webView.title ?? "", "host": webView.url?.host ?? ""]
        if let data = try? JSONSerialization.data(withJSONObject: page) {
            try? data.write(to: state.appendingPathComponent("login-page.json"), options: .atomic)
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let page: [String: String] = ["id": requestID, "state": "failed", "code": String((error as NSError).code)]
        if let data = try? JSONSerialization.data(withJSONObject: page) {
            try? data.write(to: state.appendingPathComponent("login-page.json"), options: .atomic)
        }
        webView.loadHTMLString("<html><body style='font:17px -apple-system;padding:36px'><h2>UCLA login couldn't load</h2><p>Close this window and retry <code>ssh seasnet</code>.</p></body></html>", baseURL: nil)
    }

    func cancel() {
        guard !finished, !requestID.isEmpty else { return }
        let result: [String: String] = ["id": requestID, "result": "cancelled"]
        if let data = try? JSONSerialization.data(withJSONObject: result) {
            let url = state.appendingPathComponent("login-result.json")
            try? data.write(to: url, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
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
        decisionHandler(url.scheme == "https" || (url.scheme == "http" && local) || url.scheme == "about" ? .allow : .cancel)
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

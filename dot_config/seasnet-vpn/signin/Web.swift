// The web sign-in, for when Touch ID and vlt can't do it: UCLA's own pages, in a
// process of its own (--web) that is never given the approval key and never talks
// to vlt.
//
// openconnect (9.21, hpke.c) waits on [::1]:29786 for exactly one
// GET /api/sso/<token>?return=<page>. UCLA's VPN server sends the signed-in page
// there, perhaps by way of its own closing page, whose script then does it; so pages
// of the VPN server run behind the Connecting panel and are never shown. openconnect
// answers 302 to Cisco's "you can close this" page. This window hides inside the
// navigation callback for that GET, sends the GET itself without following the
// redirect, and quits, so nothing loads after it.
//
// The callback is caught as any navigation of any frame: location, replace, meta
// refresh, a GET form, a server redirect, an iframe. A fetch, XHR, image or beacon
// to localhost is not a navigation and WebKit offers no way to see one; if the
// gateway used that, seasnet-vpn would notice openconnect moving on instead.
//
// Nothing here logs URLs, injects script, or bridges to the page.
import Cocoa
import SwiftUI
import WebKit

// MARK: - Navigation policy

struct Policy {
    var callbackPort = 29786
    /// This attempt's gateway, the host of its login URL. Its ACS, closing page and the
    /// callback's return page must be on it, or on ssl.vpn.ucla.edu; other VPN hosts are
    /// refused, so one attempt never wanders between gateways.
    var gateway = ""

    enum Verdict: Equatable {
        case allow
        case callback            // openconnect's listener: hide and deliver
        case finished            // Cisco's closing page: run it, never show it
        case handoff             // the VPN server is taking the signed-in response
        case block(String)
    }

    static func isUCLA(_ host: String) -> Bool { host == "ucla.edu" || host.hasSuffix(".ucla.edu") }

    /// Duo's prompt lives on the account's own API host, api-xxxxxxxx.duosecurity.com.
    static func isDuo(_ host: String) -> Bool {
        host.range(of: #"^api-[0-9a-z]+\.duosecurity\.com$"#, options: .regularExpression) != nil
    }

    static func isVPN(_ host: String) -> Bool { host == "vpn.ucla.edu" || host.hasSuffix(".vpn.ucla.edu") }

    func ours(_ host: String) -> Bool { (!gateway.isEmpty && host == gateway) || host == "ssl.vpn.ucla.edu" }

    /// The callback's return page, as openconnect will redirect to it.
    func returnOK(_ url: URL) -> Bool {
        guard let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              items.count == 1, items[0].name == "return", let raw = items[0].value,
              raw.unicodeScalars.allSatisfy({ $0.value > 32 && $0.value != 127 && $0 != "\\" && $0 != "#" }),
              raw.hasPrefix("https://"), let page = URL(string: raw), let host = page.host?.lowercased() else { return false }
        return ours(host) && page.user == nil && page.password == nil && (page.port == nil || page.port == 443)
    }

    #if TESTING
    /// A plain-http gateway on 127.0.0.1 that stands for the VPN server.
    var mockGateway: Int?
    #endif

    /// The URL as the policy sees it; only test builds ever change it.
    func seen(_ url: URL?) -> URL? {
        #if TESTING
        if let url, let mockGateway, url.host == "127.0.0.1", url.port == mockGateway,
           var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            (parts.scheme, parts.host, parts.port) = ("https", gateway, nil)
            return parts.url
        }
        #endif
        return url
    }

    func verdict(_ url: URL?, method: String = "GET", mainFrame: Bool = true) -> Verdict {
        guard let url = seen(url), let scheme = url.scheme?.lowercased() else { return .block("That link") }
        if scheme == "about" {
            return ["about:blank", "about:srcdoc"].contains(url.absoluteString) ? .allow : .block("That link")
        }
        let host = url.host?.lowercased() ?? ""
        guard url.user == nil, url.password == nil else { return .block(host) }
        if scheme == "http", host == "localhost", url.port == callbackPort {
            let token = url.path(percentEncoded: true).dropFirst("/api/sso/".count)
            return method == "GET" && url.path.hasPrefix("/api/sso/") && !token.isEmpty && url.fragment == nil && returnOK(url)
                ? .callback : .block(host)
        }
        guard scheme == "https", url.port == nil || url.port == 443,
              Policy.isUCLA(host) || Policy.isDuo(host) else {
            return .block(host.isEmpty ? "A \(scheme): link" : host)
        }
        if Policy.isVPN(host), !ours(host) { return .block(host) }
        if mainFrame, ours(host) {
            let path = url.path
            if path == "/+CSCOE+/saml_ac_login.html" { return .finished }
            if path.hasPrefix("/+CSCOE+/saml/sp/acs") { return .handoff }
        }
        return .allow
    }
}

/// What a trace line may say about a URL: never a query, a fragment or an unknown path.
enum Redacted {
    static let paths: Set<String> = [
        "/", "/+CSCOE+/saml/sp/login", "/+CSCOE+/saml/sp/acs", "/+CSCOE+/saml_ac_login.html", "/+CSCOE+/message.html",
        "/+CSCOE+/logon.html", "/+CSCOE+/portal.html", "/+CSCOE+/blank.html", "/+webvpn+/index.html",
        "/shibboleth-idp/profile/SAML2/Redirect/SSO", "/shibboleth-idp/profile/SAML2/POST/SSO",
        "/frame/frameless/v4/auth", "/frame/v4/auth/prompt", "/frame/v4/auth/prompt/data", "/frame/v4/preauth/healthcheck",
        "/frame/v4/exit",
    ]

    static func origin(_ url: URL) -> String {
        let scheme = url.scheme?.lowercased() ?? "", host = url.host?.lowercased() ?? ""
        let named = scheme == "https" || scheme == "http" ? scheme : scheme == "about" ? "about" : "other"
        let place = Policy.isDuo(host) ? "duo" : Policy.isUCLA(host) || ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host)
            ? host + (url.port == 29786 ? ":29786" : url.port == nil || url.port == 443 ? "" : ":other")
            : host.isEmpty ? "none" : "other"
        return "\(named)://\(place)"
    }

    static func path(_ url: URL) -> String {
        if url.path.hasPrefix("/api/sso/") { return "/api/sso" }
        return paths.contains(url.path) ? url.path : "other"
    }
}

// MARK: - State

enum WebPhase: Equatable {
    case opening, login, duo, connecting
    case failed(title: String, detail: String)
    case ended

    /// Which stop on the route is current: UCLA Logon, Duo, SEASnet.
    var step: Int? {
        switch self {
        case .opening, .login: return 0
        case .duo: return 1
        case .connecting: return 2
        case .failed, .ended: return nil
        }
    }
    var showsPage: Bool { self == .login || self == .duo }
}

final class WebModel: ObservableObject {
    @Published var phase = WebPhase.opening
    @Published var host = ""
    @Published var notice: String?
    /// The last live step, so an error keeps the route where it stopped.
    @Published var reached = 0
    /// Why this window and not Touch ID.
    var reason = ""
    var retry: () -> Void = {}
    var cancel: () -> Void = {}
}

// MARK: - Look

enum WebMetrics {
    static let width: CGFloat = 540
    static let height: CGFloat = 852
    static let header: CGFloat = 52   // A unified titlebar, so the close button centres on it.
    static let route: CGFloat = 64
    static let inset: CGFloat = 12
    static let bar: CGFloat = 40
    static let mat: CGFloat = 6       // The frame around UCLA's always-light page.
    static let footer: CGFloat = 52
    static let radius: CGFloat = 18

    /// The web page's rectangle in a flipped container of this size.
    static func page(in size: CGSize) -> CGRect {
        let top = header + route + bar
        return CGRect(x: inset + mat, y: top, width: size.width - 2 * (inset + mat),
                      height: size.height - top - mat - footer)
    }
}

/// The one visual idea: the route from this Mac's sign-in to SEASnet, filling in
/// as the login moves along it.
struct Route: View {
    @ObservedObject var model: WebModel
    private let stops = [("person.fill", "UCLA Logon"), ("iphone", "Duo"), ("server.rack", "SEASnet")]

    private var stopped: Bool { model.phase.step == nil }
    private var current: Int { model.phase.step ?? model.reached }

    var body: some View {
        HStack(spacing: 10) {
            ForEach(stops.indices, id: \.self) { i in
                if i > 0 {
                    Capsule()
                        .fill(i <= current ? AnyShapeStyle(Color.ucla) : AnyShapeStyle(.quaternary))
                        .frame(height: 2)
                        .frame(maxWidth: .infinity)
                }
                stop(i)
            }
        }
        .padding(.horizontal, 24)
        .frame(height: 36)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Step \(current + 1) of 3, \(stops[current].1)")
    }

    private func stop(_ i: Int) -> some View {
        let done = i < current, live = i == current && !stopped
        let broken = i == current && stopped
        return HStack(spacing: 8) {
            ZStack {
                Circle().fill(done || live ? AnyShapeStyle(Color.uclaFill)
                              : broken ? AnyShapeStyle(Color.orange.opacity(0.18)) : AnyShapeStyle(.quaternary))
                if live {
                    Circle().strokeBorder(Color.ucla.opacity(0.35), lineWidth: 3).padding(-4)
                }
                Image(systemName: done ? "checkmark" : broken ? "exclamationmark" : stops[i].0)
                    .font(.system(size: 12, weight: done || broken ? .bold : .semibold))
                    .foregroundStyle(done || live ? AnyShapeStyle(.white) : broken ? AnyShapeStyle(.orange) : AnyShapeStyle(.secondary))
            }
            .frame(width: 28, height: 28)
            Text(stops[i].1)
                .font(.system(size: 13, weight: live ? .semibold : .regular))
                .foregroundStyle(live || done || broken ? .primary : .secondary)
                .fixedSize()
        }
    }
}

struct AddressBar: View {
    @ObservedObject var model: WebModel
    var body: some View {
        HStack(spacing: 0) {
            Color.clear.frame(width: 28, height: 28)
            Spacer(minLength: 8)
            HStack(spacing: 6) {
                Image(systemName: "lock.fill").font(.system(size: 11)).foregroundStyle(Color.ucla)
                address
            }
            .help("This page came over HTTPS from \(model.host). Only UCLA and its Duo prompt open here.")
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Secure page on \(model.host)")
            Spacer(minLength: 8)
            Button(action: model.retry) {
                Image(systemName: "arrow.counterclockwise").font(.system(size: 12, weight: .medium))
                    .frame(width: 28, height: 28).contentShape(Rectangle())
            }
            .buttonStyle(.plain).foregroundStyle(.secondary)
            .help("Start over").accessibilityLabel("Start over")
        }
        .font(.system(size: 13))
        .padding(.horizontal, 8)
        .frame(height: WebMetrics.bar)
    }

    /// Host with its registrable domain emphasised, as browsers do.
    private var address: Text {
        let parts = model.host.split(separator: ".")
        let root = parts.suffix(2).joined(separator: ".")
        let lead = String(model.host.dropLast(root.count))
        return Text("\(Text(lead).foregroundStyle(.secondary))\(Text(root).fontWeight(.medium))")
    }
}

/// This Mac, the tunnel's lock, and SEASnet; the dots light up on connecting.
struct WideTunnel: View {
    let through: Bool
    var body: some View {
        HStack(spacing: 16) {
            Image(systemName: "laptopcomputer").font(.system(size: 34, weight: .light)).foregroundStyle(Color.ucla)
                .frame(width: 52)
            dots(lit: true)
            Tile(size: 60)
            dots(lit: through)
            Image(systemName: "server.rack").font(.system(size: 30, weight: .light))
                .foregroundStyle(through ? AnyShapeStyle(Color.ucla) : AnyShapeStyle(.tertiary))
                .frame(width: 52)
        }
        .accessibilityHidden(true)
    }

    private func dots(lit: Bool) -> some View {
        HStack(spacing: 6) {
            ForEach(0..<4, id: \.self) { _ in
                Circle().fill(lit ? AnyShapeStyle(Color.ucla) : AnyShapeStyle(.quaternary)).frame(width: 4, height: 4)
            }
        }
    }
}

struct Veil: View {
    @ObservedObject var model: WebModel
    var body: some View {
        VStack(spacing: 0) {
            switch model.phase {
            case .opening:
                waiting("Opening UCLA sign-in", "This takes a second or two.")
            case .connecting:
                waiting("Signed in", "Connecting to SEASnet…")
            case .failed(let title, let detail):
                message("exclamationmark.icloud", title, detail) {
                    Button("Cancel", action: model.cancel)
                    Button("Retry", action: model.retry).buttonStyle(.borderedProminent).tint(Color.uclaFill)
                }
            case .ended:
                message("xmark.circle", "Sign-in cancelled",
                        "The VPN is no longer waiting for this sign-in.\nNothing was connected. Start it again from the terminal.") {
                    Button("Close", action: model.cancel).buttonStyle(.borderedProminent).tint(Color.uclaFill)
                }
            case .login, .duo:
                EmptyView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.bottom, 56)
    }

    private func waiting(_ title: String, _ detail: String) -> some View {
        VStack(spacing: 0) {
            WideTunnel(through: model.phase == .connecting).padding(.bottom, 24)
            Text(title).font(.system(size: 17, weight: .semibold)).padding(.bottom, 4)
            Text(detail).font(.system(size: 13)).foregroundStyle(.secondary).padding(.bottom, 22)
            ProgressView().controlSize(.small)
        }
    }

    private func message(_ symbol: String, _ title: String, _ detail: String,
                         @ViewBuilder buttons: () -> some View) -> some View {
        VStack(spacing: 0) {
            Image(systemName: symbol).font(.system(size: 40, weight: .regular)).foregroundStyle(.secondary)
                .frame(height: 48).padding(.bottom, 16)
            Text(title).font(.system(size: 17, weight: .semibold)).padding(.bottom, 6)
            Text(detail).font(.system(size: 13)).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).lineSpacing(2).padding(.bottom, 20)
            HStack(spacing: 10) { buttons() }.controlSize(.large).buttonBorderShape(.capsule)
        }
        .frame(maxWidth: 380)
    }
}

struct Footer: View {
    @ObservedObject var model: WebModel

    private var status: (String, String, Bool) {
        if let notice = model.notice { return ("hand.raised.fill", notice, true) }
        switch model.phase {
        case .opening, .connecting: return ("lock.fill", "Only UCLA and Duo pages open in this window", false)
        case .login: return ("key.fill", "Use your password and Duo push. Passkeys don’t work here.", false)
        case .duo: return ("iphone", "Approve the sign-in with Duo", false)
        case .failed: return ("exclamationmark.triangle.fill", "Not signed in", true)
        case .ended: return ("xmark.circle.fill", "Not connected", false)
        }
    }

    var body: some View {
        let (symbol, text, warn) = status
        HStack(spacing: 8) {
            Image(systemName: symbol).font(.system(size: 12, weight: .medium))
                .foregroundStyle(warn ? AnyShapeStyle(.orange) : AnyShapeStyle(Color.ucla))
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 1) {
                Text(text).font(.system(size: 13)).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
                    .contentTransition(.opacity)
                if model.phase == .login, model.notice == nil {
                    Text("⌘\\ may fill from 1Password.").font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 12)
            if model.phase.showsPage || model.phase == .opening {
                Button("Cancel", action: model.cancel).buttonBorderShape(.capsule).help("Cancel sign-in (Esc)")
            }
        }
        .padding(.horizontal, 22)
        .frame(height: WebMetrics.footer)
    }
}

struct Chrome: View {
    @ObservedObject var model: WebModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Tile(size: 34)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Sign in to the UCLA VPN").font(.system(size: 15, weight: .semibold))
                    Text(model.reason.isEmpty ? "Needed to reach SEASnet (cs35l.seas.ucla.edu) over SSH" : model.reason)
                        .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
            }
            .padding(.leading, 54).padding(.trailing, 22)
            .frame(height: WebMetrics.header)
            .accessibilityElement(children: .combine)

            Route(model: model).frame(height: WebMetrics.route, alignment: .center)

            card
                .padding(.horizontal, WebMetrics.inset)

            Footer(model: model)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(LinearGradient(colors: [Color.ucla.opacity(0.14), Color.ucla.opacity(0)],
                                   startPoint: .top, endPoint: .init(x: 0.5, y: 0.3)))
        .ignoresSafeArea()
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: model.phase)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: model.notice)
    }

    private var card: some View {
        let shape = RoundedRectangle(cornerRadius: WebMetrics.radius, style: .continuous)
        let inner = RoundedRectangle(cornerRadius: WebMetrics.radius - WebMetrics.mat, style: .continuous)
        return VStack(spacing: 0) {
            // With a page: address bar over a matted page. Without: one clean panel.
            if model.phase.showsPage { AddressBar(model: model) }
            Veil(model: model)
                .background(inner.fill(Color.cardFill).opacity(model.phase.showsPage ? 1 : 0))
                .padding([.horizontal, .bottom], WebMetrics.mat)
        }
        .background(shape.fill(model.phase.showsPage ? Color.matFill : Color.cardFill).shadow(color: .black.opacity(0.14), radius: 12, y: 4))
        .overlay(shape.strokeBorder(.primary.opacity(0.08), lineWidth: 1))
    }
}

final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

final class WebController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let start: URL
    let policy: Policy
    let model = WebModel()
    let window: EscapeWindow
    let web: WKWebView
    let interactive: Bool
    /// How long the closing page may take, once loaded, to send its result on.
    var handoffPatience: TimeInterval = 8
    /// How long the VPN server may take over the signed-in response.
    var handoffLimit: TimeInterval = 20
    /// Called once with the exit code; the window is already hidden.
    var finish: (Int32) -> Void = { exit($0) }
    /// Redacted steps of the sign-in, for seasnet-vpn's trace file.
    var emitTrace: ([String: String]) -> Void = { _ in }
    /// The GET may or may not have arrived; the window stays hidden meanwhile.
    var unknown: () -> Void = {}
    /// Self-test trace.
    var events: [String] = []

    private(set) var hidden = false
    private(set) var over = false
    private var delivering = false
    /// The result was sent on: from here nothing loads until a Retry.
    private var called = false
    /// The VPN server has the signed-in response; its pages run unseen.
    private var handoff = false
    private let began = Date()
    private var pageShown = false
    private var patience: Timer?
    private var noticeTimer: Timer?
    private var showTimer: Timer?

    init(start: URL, policy: Policy = Policy(), store: WKWebsiteDataStore, interactive: Bool) {
        self.start = start
        var policy = policy
        if policy.gateway.isEmpty, let gateway = Contract.gateway(of: start.absoluteString) { policy.gateway = gateway }
        self.policy = policy
        self.interactive = interactive
        let config = WKWebViewConfiguration()
        config.websiteDataStore = store
        config.preferences.tabFocusesLinks = true
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        config.allowsAirPlayForMediaPlayback = false
        web = WKWebView(frame: .zero, configuration: config)
        window = WebController.makeWindow()
        super.init()

        let root = WebController.root(model: model, page: web)
        window.contentView = root
        window.delegate = self
        window.onEscape = { [weak self] in self?.cancel() }
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setAccessibilityLabel("UCLA sign-in page")
        model.retry = { [weak self] in self?.load() }
        model.cancel = { [weak self] in self?.cancel() }
    }

    static func makeWindow() -> EscapeWindow {
        let window = EscapeWindow(contentRect: NSRect(x: 0, y: 0, width: WebMetrics.width, height: WebMetrics.height),
                                  styleMask: [.titled, .closable, .fullSizeContentView],
                                  backing: .buffered, defer: false)
        window.title = windowTitle
        window.identifier = NSUserInterfaceItemIdentifier("signin")
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        // An empty unified toolbar gives the tall titlebar, so the close button
        // lines up with the header.
        window.toolbar = NSToolbar(identifier: "header")
        window.toolbarStyle = .unified
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true
        window.isMovableByWindowBackground = true
        window.isReleasedWhenClosed = false
        window.isRestorable = false
        window.hidesOnDeactivate = false
        window.level = .floating
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.tabbingMode = .disallowed
        window.backgroundColor = .windowBackgroundColor
        return window
    }

    /// Material, SwiftUI chrome, and the page laid over the card's page area.
    static func root(model: WebModel, page: NSView, asKey: Bool = false) -> NSView {
        let frame = NSRect(x: 0, y: 0, width: WebMetrics.width, height: WebMetrics.height)
        let root = FlippedView(frame: frame)
        let material = NSVisualEffectView(frame: frame)
        material.material = .underWindowBackground
        material.blendingMode = .behindWindow
        material.state = .active
        material.autoresizingMask = [.width, .height]
        root.addSubview(material)
        let chrome: NSView & HostingSizing = asKey  // Previews have no key window to take it from.
            ? NSHostingView(rootView: Chrome(model: model).environment(\.controlActiveState, .key))
            : NSHostingView(rootView: Chrome(model: model))
        chrome.sizingOptions = []
        chrome.frame = frame
        chrome.autoresizingMask = [.width, .height]
        root.addSubview(chrome)
        page.frame = WebMetrics.page(in: frame.size)
        page.autoresizingMask = [.width, .height]
        page.wantsLayer = true
        page.layer?.cornerRadius = WebMetrics.radius - WebMetrics.mat  // Concentric with the card.
        page.layer?.cornerCurve = .continuous
        page.layer?.masksToBounds = true
        page.alphaValue = 0
        root.addSubview(page)
        return root
    }

    // MARK: lifecycle

    func present() {
        guard interactive else { return }
        if let area = workingArea() {
            let size = NSSize(width: WebMetrics.width, height: min(WebMetrics.height, area.height - 24))
            window.setFrame(NSRect(x: (area.midX - size.width / 2).rounded(),
                                   y: (area.midY - size.height / 2).rounded(),
                                   width: size.width, height: size.height), display: true)
        }
        bringUp(window)
    }

    func load() {
        guard !over, !delivering else { return }
        patience?.invalidate()
        (called, handoff, pageShown) = (false, false, false)
        trace("load", start)
        setPhase(.opening)
        model.notice = nil
        web.load(URLRequest(url: start, timeoutInterval: 30))
    }

    /// Off screen in this very call: no fade, no closing animation.
    private func hide() {
        guard !hidden else { return }
        hidden = true
        events.append("hide")
        window.animationBehavior = .none
        window.alphaValue = 0
        window.orderOut(nil)
    }

    private func reappear(_ phase: WebPhase) {
        guard !over else { return }
        delivering = false
        hidden = false
        window.alphaValue = 1
        setPhase(phase)
        events.append("reappear")
        if interactive { window.makeKeyAndOrderFront(nil) }
    }

    func cancel() { end(1) }

    func end(_ code: Int32) {
        guard !over else { return }
        over = true
        for timer in [patience, noticeTimer, showTimer] { timer?.invalidate() }
        hide()
        web.stopLoading()
        finish(code)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        cancel()
        return false
    }

    // MARK: appearance

    private func setPhase(_ phase: WebPhase) {
        if let step = phase.step { model.reached = step }
        if model.phase != phase { model.phase = phase }
        if !phase.showsPage { model.host = "" }
        let show = phase.showsPage
        let animate = show && interactive && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        NSAnimationContext.runAnimationGroup { context in
            // In: cross-fade over the card. Out: at once, so no stale page lingers.
            context.duration = animate ? 0.18 : 0
            (animate ? web.animator() : web).alphaValue = show ? 1 : 0
        }
        web.setAccessibilityHidden(!show)
    }

    private func phase(for url: URL?) -> WebPhase? {
        guard let host = policy.seen(url)?.host?.lowercased() else { return nil }
        if Policy.isDuo(host) { return .duo }
        guard Policy.isUCLA(host) else { return nil }
        switch policy.verdict(url) {
        case .handoff, .finished: return .connecting
        default: return .login
        }
    }

    private func showPage() {
        guard !over, !hidden, !handoff, !called, let phase = phase(for: web.url), phase.showsPage else { return }
        showTimer?.invalidate()
        setPhase(phase)
        model.host = policy.seen(web.url)?.host?.lowercased() ?? ""
        if !pageShown {
            pageShown = true
            if interactive { window.makeFirstResponder(web) }
        }
    }

    private func notice(_ what: String) {
        events.append("blocked")
        model.notice = "Blocked \(what). Only UCLA and Duo pages open here."
        noticeTimer?.invalidate()
        noticeTimer = Timer.scheduledTimer(withTimeInterval: 6, repeats: false) { [weak self] _ in
            self?.model.notice = nil
        }
    }

    /// Over a page, a note. With no page to go back to, nothing else would come.
    private func blocked(_ what: String) {
        guard !model.phase.showsPage else { notice(what); return }
        events.append("blocked")
        showTimer?.invalidate()
        patience?.invalidate()
        setPhase(.failed(title: "Couldn’t open UCLA sign-in",
                         detail: "It led to \(what), which this window doesn’t open.\nRetry to start the sign-in over."))
    }

    /// The VPN server has the signed-in response; the callback should come at once.
    func beginHandoff() {
        showTimer?.invalidate()
        setPhase(.connecting)
        guard !handoff else { return }
        handoff = true
        wait(handoffLimit, then: .silent)
    }

    enum Lost: Equatable {
        case refused(Int), silent, closed

        var name: String {
            switch self {
            case .refused: return "refused"
            case .silent: return "silent"
            case .closed: return "closed"
            }
        }
        var phase: WebPhase {
            switch self {
            case .refused(let status):
                return .failed(title: "The VPN refused the sign-in",
                               detail: "It answered UCLA’s result with \(status).\nTry signing in again.")
            case .silent:
                return .failed(title: "UCLA’s VPN server went quiet",
                               detail: "It took the sign-in but never sent a result.\nTry signing in again.")
            case .closed:
                return .failed(title: "UCLA finished without a result",
                               detail: "Its closing page came up, but sent nothing to the VPN.\nTry signing in again.")
            }
        }
    }

    private func lose(_ lost: Lost) {
        guard !over else { return }
        var more = ["lost": lost.name]
        if case .refused(let status) = lost { more["status"] = String(status) }
        trace("lost", nil, more)
        web.stopLoading()
        reappear(lost.phase)
    }

    private func wait(_ seconds: TimeInterval, then lost: Lost) {
        patience?.invalidate()
        patience = Timer.scheduledTimer(withTimeInterval: seconds, repeats: false) { [weak self] _ in self?.lose(lost) }
    }

    private func trace(_ kind: String, _ url: URL?, _ more: [String: String] = [:]) {
        var line = more
        line["kind"] = kind
        line["ms"] = String(Int(Date().timeIntervalSince(began) * 1000))
        if let url = policy.seen(url) {
            line["origin"] = Redacted.origin(url)
            line["path"] = Redacted.path(url)
        }
        emitTrace(line)
    }

    // MARK: hand-off

    /// openconnect needs one GET. Sending it here, outside WebKit, means nothing
    /// follows its redirect and the window can already be gone.
    private func deliver(_ url: URL) {
        guard !delivering, !over else { return }
        (delivering, called) = (true, true)
        patience?.invalidate()
        showTimer?.invalidate()
        setPhase(.connecting)
        events.append("deliver")
        web.stopLoading()
        let port = policy.callbackPort
        Thread.detachNewThread { [weak self] in
            let outcome = Callback.send(url, port: port)
            DispatchQueue.main.async {
                guard let self, !self.over else { return }
                switch outcome {
                case .delivered(let status):
                    self.events.append("delivered")
                    self.trace("answer", nil, ["status": String(status)])
                    self.end(0)
                case .unknown:
                    self.events.append("unknown")
                    self.trace("answer", nil, ["lost": "unknown"])
                    self.unknown()
                case .refused(let status): self.lose(.refused(status))
                case .gone:
                    self.trace("lost", nil, ["lost": "gone"])
                    self.reappear(.ended)
                }
            }
        }
    }

    // MARK: WebKit

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        let url = action.request.url, method = action.request.httpMethod ?? "GET"
        let mainFrame = action.targetFrame?.isMainFrame ?? false
        let verdict = policy.verdict(url, method: method, mainFrame: mainFrame)
        let closed = over || called || action.shouldPerformDownload
        trace("nav", url, ["verdict": closed ? "closed" : verdict.name, "method": ["GET", "POST"].contains(method) ? method : "other",
                           "frame": action.targetFrame == nil ? "new" : mainFrame ? "main" : "sub",
                           "by": action.navigationType.name])
        guard !closed else { decisionHandler(.cancel); return }
        if verdict == .callback {  // Whatever frame, or none: this is the result.
            hide()
            decisionHandler(.cancel)
            deliver(url!)
            return
        }
        guard action.targetFrame != nil else {  // A new window or tab.
            notice(url?.host ?? "A pop-up")
            decisionHandler(.cancel)
            return
        }
        switch verdict {
        case .allow:
            if mainFrame, handoff, let host = policy.seen(url)?.host?.lowercased(), !Policy.isVPN(host) {
                // Back to UCLA or Duo: there is a page to see again.
                handoff = false
                patience?.invalidate()
            }
            decisionHandler(.allow)
        case .handoff, .finished:
            beginHandoff()
            decisionHandler(.allow)
        case .callback:
            decisionHandler(.cancel)
        case .block(let what):
            decisionHandler(.cancel)
            if mainFrame { blocked(what) }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping @MainActor (WKNavigationResponsePolicy) -> Void) {
        let status = (response.response as? HTTPURLResponse)?.statusCode
        trace("response", response.response.url, ["frame": response.isForMainFrame ? "main" : "sub", "status": status.map(String.init) ?? "none"])
        guard !called else { decisionHandler(.cancel); return }
        decisionHandler(response.canShowMIMEType ? .allow : .cancel)
        if !response.canShowMIMEType, response.isForMainFrame { blocked("a download") }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        if !pageShown { showTimer?.invalidate() }  // Only passing through; keep the opening panel.
    }

    func webView(_ webView: WKWebView, didReceiveServerRedirectForProvisionalNavigation navigation: WKNavigation!) {
        trace("redirect", webView.url, ["by": "server"])
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        guard !over, !hidden, !handoff else { return }
        pageShown ? showPage() : show(after: 1.5)
    }

    /// The first page waits until it has painted, so nothing flashes white and
    /// SAML's self-submitting pages stay out of sight.
    private func show(after delay: TimeInterval) {
        showTimer?.invalidate()
        showTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in self?.showPage() }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        trace("loaded", web.url)
        guard !over, !hidden, !called else { return }
        guard handoff else {
            let relay = Policy.isVPN(policy.seen(web.url)?.host?.lowercased() ?? "")
            pageShown ? showPage() : show(after: relay ? 1.5 : 0.15)
            return
        }
        // Still unseen. The closing page is the last stop, so it gets less time.
        if policy.verdict(web.url) == .finished { wait(handoffPatience, then: .closed) }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    private func failed(_ error: Error) {
        let error = error as NSError
        trace("failed", nil, ["status": String(error.code)])
        // A redirect, a retry or our own policy cancelled the old load.
        if error.domain == NSURLErrorDomain && error.code == NSURLErrorCancelled { return }
        if error.domain == "WebKitErrorDomain" && error.code == 102 { return }
        guard !over, !hidden, !called else { return }
        patience?.invalidate()
        let detail: String
        switch error.code {
        case NSURLErrorNotConnectedToInternet, NSURLErrorNetworkConnectionLost:
            detail = "This Mac looks offline.\nCheck the connection, then retry."
        case NSURLErrorTimedOut:
            detail = "UCLA took too long to answer.\nCheck the connection, then retry."
        case NSURLErrorSecureConnectionFailed, NSURLErrorServerCertificateUntrusted,
             NSURLErrorServerCertificateHasBadDate, NSURLErrorServerCertificateHasUnknownRoot,
             NSURLErrorServerCertificateNotYetValid:
            detail = "The page’s certificate couldn’t be verified,\nso it was not opened."
        default:
            detail = "The page didn’t load (error \(error.code)).\nCheck the connection, then retry."
        }
        showTimer?.invalidate()
        setPhase(.failed(title: "Couldn’t open UCLA sign-in", detail: detail))
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        guard !over, !hidden, !called else { return }
        patience?.invalidate()
        setPhase(.failed(title: "The sign-in page stopped", detail: "Its web process quit.\nRetry to start the sign-in over."))
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        notice(action.request.url?.host ?? "a pop-up")
        return nil
    }

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping @MainActor (WKPermissionDecision) -> Void) {
        decisionHandler(.deny)
    }
}

/// The GET openconnect waits for, on a plain socket to [::1]: URLSession would send
/// it again by itself when the answer goes missing.
enum Callback {
    case delivered(Int), refused(Int)
    case gone       // Nobody was listening; nothing was sent.
    case unknown    // Sent, perhaps, and not answered. seasnet-vpn finds out.

    static func send(_ url: URL, port: Int) -> Callback {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return .refused(0) }
        let target = parts.percentEncodedPath + (parts.percentEncodedQuery.map { "?" + $0 } ?? "")
        guard target.utf8.count < 4000, target.utf8.allSatisfy({ $0 > 32 && $0 < 127 }) else { return .refused(0) }
        let fd = socket(AF_INET6, SOCK_STREAM, 0)
        guard fd >= 0 else { return .gone }
        defer { close(fd) }
        var limit = timeval(tv_sec: 10, tv_usec: 0), on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &limit, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &limit, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in6()
        address.sin6_family = sa_family_t(AF_INET6)
        address.sin6_port = UInt16(port).bigEndian
        address.sin6_addr = in6addr_loopback
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in6>.size))
            }
        }
        guard connected == 0 else { return .gone }
        let request = Data("GET \(target) HTTP/1.1\r\nHost: localhost:\(port)\r\nConnection: close\r\n\r\n".utf8)
        var sent = 0
        while sent < request.count {
            let count = request.withUnsafeBytes { write(fd, $0.baseAddress! + sent, request.count - sent) }
            guard count > 0 else { return .unknown }
            sent += count
        }
        var answer = Data(), buffer = [UInt8](repeating: 0, count: 1024)
        while answer.range(of: Data("\r\n".utf8)) == nil, answer.count < 4096 {
            let count = read(fd, &buffer, buffer.count)
            guard count > 0 else { return .unknown }
            answer.append(contentsOf: buffer[0..<count])
        }
        let words = String(decoding: answer, as: UTF8.self).split(separator: "\r\n")[0].split(separator: " ")
        guard let version = words.first, version.hasPrefix("HTTP/1."), words.count > 1, let status = Int(words[1]),
              (100..<600).contains(status) else { return .unknown }
        return status == 302 || status == 200 ? .delivered(status) : .refused(status)
    }
}

extension Policy.Verdict {
    var name: String {
        switch self {
        case .allow: return "allow"
        case .callback: return "callback"
        case .finished: return "finished"
        case .handoff: return "handoff"
        case .block: return "block"
        }
    }
}

extension WKNavigationType {
    /// Script, meta refresh and server redirects are all "other" to WebKit; a server
    /// redirect also leaves a "redirect" line.
    var name: String {
        switch self {
        case .linkActivated: return "link"
        case .formSubmitted: return "form"
        case .formResubmitted: return "form"
        case .backForward: return "history"
        case .reload: return "reload"
        default: return "other"
        }
    }
}

final class WebAppDelegate: NSObject, NSApplicationDelegate {
    let controller: WebController
    init(_ controller: WebController) { self.controller = controller }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = WebAppDelegate.menu(for: controller)
        writeLine(["event": "prepared"])
        Thread.detachNewThread { [controller] in
            while let line = readLine() {
                guard let command = parse(line), command["do"] as? String == "show" else { continue }
                DispatchQueue.main.async { WebAppDelegate.show(controller) }
            }
            // The supervisor only ever closes this pipe, by hand or by dying.
            DispatchQueue.main.async { controller.end(4) }
        }
    }

    /// Nothing is on screen or loaded before seasnet-vpn says so.
    static func show(_ controller: WebController) {
        guard !controller.over, !controller.window.isVisible else { return }
        controller.present()
        guard controller.window.isVisible else { exit(70) }
        writeLine(["event": "ready"])
        controller.load()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        controller.cancel()
        return .terminateCancel
    }

    @objc func startOver(_ sender: Any?) { controller.load() }

    /// An accessory app shows no menu bar, but these key equivalents still work.
    static func menu(for controller: WebController) -> NSMenu {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [(String, String, String)]) {
            let menu = NSMenu(title: title)
            for (title, action, key) in items {
                menu.addItem(withTitle: title, action: Selector((action)), keyEquivalent: key)
            }
            let item = NSMenuItem()
            item.submenu = menu
            main.addItem(item)
        }
        submenu(windowTitle, [("Quit", "terminate:", "q")])
        submenu("Edit", [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                         ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")])
        submenu("Window", [("Close", "performClose:", "w"), ("Start Over", "startOver:", "r")])
        return main
    }
}

func runWeb() -> Never {
    guard let line = readLine(), let request = parse(line), let raw = request["url"] as? String,
          Contract.accepts(raw), let url = URL(string: raw) else { exit(64) }
    // In memory only. A remembered UCLA or Duo session would let a later login finish
    // with nobody there, and a person completes every login.
    let controller = WebController(start: url, store: .nonPersistent(), interactive: true)
    controller.model.reason = String((request["reason"] as? String ?? "").prefix(90))
    controller.emitTrace = { writeLine(["event": "trace"].merging($0) { mine, _ in mine }) }
    controller.unknown = { writeLine(["event": "unknown"]) }
    controller.finish = { code in
        if code == 0 { writeLine(["event": "delivered"]) }
        exit(code)
    }
    runAccessoryApp(WebAppDelegate(controller))
}

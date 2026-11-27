// The web route through the person's own browser (Zen), so its 1Password extension
// can fill the UCLA login and offer passkeys, which no WKWebView of ours can.
//
// An ASWebAuthenticationSession hands the gateway URL to the default browser. Zen
// (Firefox 159+) opens it in a chromeless window of its own and reaches openconnect's
// localhost listener itself; this process never sees that hop. seasnet-vpn watches
// openconnect instead and says {"do":"done"} the moment the result has landed, and
// cancel() then closes Zen's window.
//
// The session's callback scheme never occurs, so Zen never ends the session on its
// own. When it ends without us (the person closed Zen's window, said no to macOS's
// alert, or Zen gave up), the card stays and asks: Zen again, our window, or cancel.
// No timing guess tells those apart.
//
// Not ephemeral: Firefox opens an ephemeral session in a private window
// (browser/modules/ASWebAuthSessionService.sys.mjs, openAuthWindow: `private:
// !!ephemeral`), where extensions such as 1Password don't run unless allowed there.
// A normal one uses his Zen profile, extensions and cookies, behind a one-time macOS
// consent alert per sign-in.
import AuthenticationServices
import Cocoa
import SwiftUI

/// A browser sign-in that can be started and cancelled; the real one only in the app.
protocol BrowserAuth: AnyObject {
    func start() -> Bool
    func cancel()
}

final class SystemBrowserAuth: NSObject, BrowserAuth, ASWebAuthenticationPresentationContextProviding {
    /// Matches nothing on the web: Zen keeps the session open until we cancel it.
    static let callbackScheme = "x-ucla-vpn-signin-unused"
    private let session: ASWebAuthenticationSession
    private weak var anchor: NSWindow?

    init(url: URL, anchor: NSWindow, ended: @escaping (Error?) -> Void) {
        session = ASWebAuthenticationSession(url: url, callback: .customScheme(SystemBrowserAuth.callbackScheme)) { _, error in
            DispatchQueue.main.async { ended(error) }
        }
        self.anchor = anchor
        super.init()
        session.prefersEphemeralWebBrowserSession = false
        session.presentationContextProvider = self
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        anchor ?? NSWindow()
    }

    var ephemeral: Bool { session.prefersEphemeralWebBrowserSession }
    func start() -> Bool { session.start() }
    func cancel() { session.cancel() }
}

/// The default browser for https, and whether it takes ASWebAuthenticationSession
/// requests itself. Without that, macOS would send the sign-in to Safari.
struct DefaultBrowser: Equatable {
    var bundleID: String
    var name: String
    var supported: Bool
    var path: String?

    static func current() -> DefaultBrowser? {
        guard let app = NSWorkspace.shared.urlForApplication(toOpen: URL(string: "https://ucla.edu/")!),
              let bundle = Bundle(url: app), let id = bundle.bundleIdentifier else { return nil }
        let name = (bundle.object(forInfoDictionaryKey: "CFBundleDisplayName") ?? bundle.object(forInfoDictionaryKey: "CFBundleName")) as? String
        return DefaultBrowser(bundleID: id, name: name ?? app.deletingPathExtension().lastPathComponent,
                              supported: supports(id, bundle.infoDictionary ?? [:]), path: app.path)
    }

    static func supports(_ id: String, _ info: [String: Any]) -> Bool {
        if ["com.apple.Safari", "com.apple.SafariTechnologyPreview"].contains(id) { return true }
        let capabilities = info["ASWebAuthenticationSessionWebBrowserSupportCapabilities"] as? [String: Any]
        return (capabilities?["IsSupported"] as? Bool) == true || (capabilities?["IsSupported"] as? NSNumber)?.boolValue == true
    }

    /// For the trace: a closed set, never a bundle id as such.
    var traceName: String {
        switch bundleID {
        case "app.zen-browser.zen": return "zen"
        case "com.apple.Safari", "com.apple.SafariTechnologyPreview": return "safari"
        case "org.mozilla.firefox", "org.mozilla.firefoxdeveloperedition", "org.mozilla.nightly": return "firefox"
        case "com.google.Chrome": return "chrome"
        default: return "other"
        }
    }
}

final class BrowserModel: ObservableObject {
    @Published var browser = "Zen"
    @Published var reason = ""
    var icon: NSImage?
    /// Zen's window went away without a result; the card asks what next.
    @Published var closed = false
    var again: () -> Void = {}
    var here: () -> Void = {}
    var cancel: () -> Void = {}
}

struct BrowserCard: View {
    @ObservedObject var model: BrowserModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 14) {
                Tile(size: 56)
                HStack(spacing: 5) {
                    ForEach(0..<3, id: \.self) { _ in Circle().fill(Color.ucla).frame(width: 4, height: 4) }
                }
                if let icon = model.icon {
                    Image(nsImage: icon).resizable().interpolation(.high).frame(width: 64, height: 64)
                } else {
                    Image(systemName: "arrow.up.forward.app").font(.system(size: 30, weight: .light)).foregroundStyle(Color.ucla)
                        .frame(width: 56, height: 56)
                }
            }
            .accessibilityHidden(true)
            VStack(spacing: 4) {
                Text(model.closed ? "The \(model.browser) window closed" : "Continue in \(model.browser)")
                    .font(.system(size: 17, weight: .semibold)).accessibilityAddTraits(.isHeader)
                Text(model.closed ? "Nothing reached the VPN. Try \(model.browser) again, or sign in here instead."
                     : "Sign in to the UCLA VPN in the \(model.browser) window. This closes by itself once you’re in.")
                    .font(.system(size: 13)).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center).lineSpacing(1.5).fixedSize(horizontal: false, vertical: true)
            }
            .padding(.top, 16)
            if !model.reason.isEmpty, !model.closed {
                Text(model.reason + ".").font(.system(size: 11)).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center).padding(.horizontal, 12).padding(.vertical, 7)
                    .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(.primary.opacity(0.05)))
                    .padding(.top, 14)
            }
            Spacer(minLength: 0)
            if !model.closed {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for UCLA…").font(.system(size: 13)).foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
                Spacer(minLength: 0)
            }
            VStack(spacing: 8) {
                if model.closed {
                    Button(action: model.again) { Text("Try \(model.browser) again").frame(maxWidth: .infinity) }
                        .buttonStyle(.borderedProminent).tint(Color.uclaFill).controlSize(.large).buttonBorderShape(.capsule)
                        .keyboardShortcut(.defaultAction)
                }
                Button(action: model.here) { Text("Sign in here instead").frame(maxWidth: .infinity) }
                    .buttonStyle(.bordered).controlSize(.large).buttonBorderShape(.capsule)
                Button(action: model.cancel) { Text("Cancel").frame(maxWidth: .infinity) }
                    .buttonStyle(.bordered).controlSize(.large).buttonBorderShape(.capsule)
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 34).padding(.bottom, 22)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(LinearGradient(colors: [Color.ucla.opacity(0.16), Color.ucla.opacity(0)],
                                   startPoint: .top, endPoint: .init(x: 0.5, y: 0.45)))
        .ignoresSafeArea()
    }
}

/// The card that stands for the sign-in while it happens in the browser, and the
/// session's anchor.
final class BrowserController: NSObject, NSWindowDelegate {
    static let size = NSSize(width: 340, height: 400)
    /// How long cancel() may take to reach the browser before we leave anyway.
    var cancelGrace: TimeInterval = 1.5

    let model = BrowserModel()
    let window: EscapeWindow
    let interactive: Bool
    private let makeSession: (NSWindow, @escaping (Error?) -> Void) -> BrowserAuth
    private var session: BrowserAuth?
    private var startedAt: Date?
    private var ending: Int32?
    private(set) var over = false
    var emitTrace: ([String: String]) -> Void = { _ in }
    /// Hand the sign-in to our own web window, with the trace's reason.
    var fallBack: (String) -> Void = { _ in }
    var finish: (Int32) -> Void = { exit($0) }

    init(browser: DefaultBrowser, reason: String, interactive: Bool,
         makeSession: @escaping (NSWindow, @escaping (Error?) -> Void) -> BrowserAuth) {
        self.interactive = interactive
        window = BrowserController.makeWindow()
        self.makeSession = makeSession
        super.init()
        browserTraceName = browser.traceName
        model.browser = browser.name
        model.reason = reason
        model.icon = browser.path.map { NSWorkspace.shared.icon(forFile: $0) }
        model.here = { [weak self] in self?.leave(for: "chosen-here") }
        model.again = { [weak self] in self?.again() }
        model.cancel = { [weak self] in self?.end(1) }
        window.delegate = self
        window.onEscape = { [weak self] in self?.end(1) }
        window.contentView = BrowserController.root(model: model)
    }

    static func makeWindow() -> EscapeWindow {
        let window = EscapeWindow(contentRect: NSRect(origin: .zero, size: size),
                                  styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
        window.title = windowTitle
        window.identifier = NSUserInterfaceItemIdentifier("signin")
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        for button in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            window.standardWindowButton(button)?.isHidden = true
        }
        window.isMovableByWindowBackground = true
        window.isReleasedWhenClosed = false
        window.isRestorable = false
        window.hidesOnDeactivate = false
        // Not floating: the browser's sign-in window must be able to come in front of it.
        window.level = .normal
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.tabbingMode = .disallowed
        return window
    }

    static func root(model: BrowserModel, asKey: Bool = false) -> NSView {
        let frame = NSRect(origin: .zero, size: size)
        let material = NSVisualEffectView(frame: frame)
        material.material = .popover
        material.blendingMode = .behindWindow
        material.state = .active
        let card = BrowserCard(model: model)
        let chrome: NSView & HostingSizing = asKey
            ? NSHostingView(rootView: card.environment(\.controlActiveState, .key)) : NSHostingView(rootView: card)
        chrome.sizingOptions = []
        chrome.frame = frame
        chrome.autoresizingMask = [.width, .height]
        material.addSubview(chrome)
        return material
    }

    /// Beside where Firefox centres its 600 by 700 sign-in window, so neither hides the other.
    func present() {
        guard interactive else { return }
        if let area = workingArea() {
            let size = window.frame.size
            let beside = area.midX - 300 - 24 - size.width
            window.setFrameOrigin(NSPoint(x: (beside >= area.minX + 16 ? beside : area.midX - size.width / 2).rounded(),
                                          y: (area.midY - size.height / 2).rounded()))
        }
        bringUp(window)
    }

    func begin() {
        guard !over, ending == nil, session == nil else { return }
        let session = makeSession(window) { [weak self] error in self?.ended(error) }
        self.session = session
        guard session.start() else {
            trace("unstartable")
            leave(for: "unstartable")
            return
        }
        startedAt = Date()
        trace("started")
    }

    /// seasnet-vpn saw the result land at openconnect: the browser window goes now.
    func done() { end(0) }

    func end(_ code: Int32) {
        guard !over, ending == nil else { return }
        ending = code
        trace(code == 0 ? "done" : code == 1 ? "cancelled" : "closed")
        hide()
        guard let session, startedAt != nil else { return close() }
        session.cancel()
        // The answer to cancel() is how we know the browser heard it; don't wait forever.
        DispatchQueue.main.asyncAfter(deadline: .now() + cancelGrace) { [weak self] in self?.close() }
    }

    private func ended(_ error: Error?) {
        guard !over else { return }
        if ending != nil { return close() }
        let error = error as NSError?
        if error?.domain == ASWebAuthenticationSessionErrorDomain,
           error?.code == ASWebAuthenticationSessionError.canceledLogin.rawValue {
            // Not ours. The person closed Zen's window or said no to macOS's alert, or
            // Zen gave up; nothing here can tell which, so the card asks.
            (session, startedAt) = (nil, nil)
            model.closed = true
            trace("closed-by-person")
            if interactive { bringUp(window) }
            return
        }
        trace(error == nil ? "matched" : "error", ["status": String(error?.code ?? 0)])
        leave(for: error == nil ? "matched" : "error")
    }

    /// Try Zen again: a new session, the same login.
    private func again() {
        guard !over, ending == nil, model.closed else { return }
        model.closed = false
        trace("retry")
        begin()
    }

    /// To our own web window: the session is dropped and the card goes.
    private func leave(for reason: String) {
        guard !over, ending == nil else { return }
        over = true
        if reason == "chosen-here" { trace(reason) }
        if startedAt != nil { session?.cancel() }
        hide()
        fallBack(reason)
    }

    private func hide() {
        window.animationBehavior = .none
        window.orderOut(nil)
    }

    private func close() {
        guard !over else { return }
        over = true
        finish(ending ?? 1)
    }

    private func trace(_ state: String, _ more: [String: String] = [:]) {
        var line = more
        (line["kind"], line["zen"]) = ("zen", state)
        line["browser"] = browserTraceName
        emitTrace(line)
    }

    var browserTraceName = "other"

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        end(1)
        return false
    }
}

// The approval card: who is asking, Touch ID in the window itself, and the Secure
// Enclave signature vlt checks. It runs in a process of its own, which never
// creates a web view; seasnet-vpn starts the web window separately, without the key.
//
// The enclave enforces the approval, not this window: the key is made with
// [.privateKeyUsage, .biometryCurrentSet], so without a matching fingerprint it
// signs for nobody, and it is unusable once the enrolled fingerprints change.
import Cocoa
import CryptoKit
import LocalAuthentication
import LocalAuthenticationEmbeddedUI
import SwiftUI

// MARK: - What gets signed

extension Contract {
    /// The exact bytes vlt verifies: a purpose, its one-time challenge, this URL.
    static func message(challenge: String, url: String) -> Data? {
        guard challenge.range(of: "^[A-Za-z0-9_-]{43}$", options: .regularExpression) != nil,
              !challenge.contains("\n") else { return nil }
        let digest = SHA256.hash(data: Data(url.utf8)).map { String(format: "%02x", $0) }.joined()
        return Data("vlt-vpn-login-v1\n\(challenge)\n\(digest)".utf8)
    }
}

extension Data {
    var base64URL: String {
        base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
}

// MARK: - The key

enum KeyFailure: Error, Equatable {
    case missing, unsafe, unusable, exists, noEnclave, noFingerprints
    case other(String)
}

struct KeyFile: Codable {
    var version = 1
    var key: Data

    static func load(_ path: String) throws -> KeyFile {
        let descriptor = open(path, O_RDONLY | O_NOFOLLOW)
        guard descriptor >= 0 else { throw errno == ENOENT ? KeyFailure.missing : KeyFailure.unsafe }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        var info = stat()
        guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid(),
              info.st_mode & 0o077 == 0, info.st_size < 65536 else { throw KeyFailure.unsafe }
        guard let data = try? handle.readToEnd(), let file = try? JSONDecoder().decode(KeyFile.self, from: data),
              file.version == 1 else { throw KeyFailure.unusable }
        return file
    }

    func create(_ path: String) throws {
        let descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard descriptor >= 0 else { throw errno == EEXIST ? KeyFailure.exists : KeyFailure.other(String(cString: strerror(errno))) }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        do {
            try handle.write(contentsOf: JSONEncoder().encode(self))
            try handle.synchronize()
        } catch {
            unlink(path)
            throw error
        }
    }
}

enum EnclaveKey {
    static func access() -> SecAccessControl? {
        SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                                        [.privateKeyUsage, .biometryCurrentSet], nil)
    }

    static func create(_ path: String) throws -> Data {
        var info = stat()
        guard lstat(path, &info) != 0 else { throw KeyFailure.exists }
        guard SecureEnclave.isAvailable else { throw KeyFailure.noEnclave }
        guard LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil),
              let control = access() else { throw KeyFailure.noFingerprints }
        let key = try SecureEnclave.P256.Signing.PrivateKey(compactRepresentable: false, accessControl: control)
        try KeyFile(key: key.dataRepresentation).create(path)
        return key.publicKey.x963Representation
    }

    static func open(_ file: KeyFile, _ context: LAContext) throws -> SecureEnclave.P256.Signing.PrivateKey {
        do {
            return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: file.key, authenticationContext: context)
        } catch {
            throw KeyFailure.unusable
        }
    }

    static func publicKey(_ path: String) throws -> Data {
        let context = LAContext()
        context.interactionNotAllowed = true
        return try open(KeyFile.load(path), context).publicKey.x963Representation
    }

    /// A signature with interaction forbidden. The enclave must refuse it for
    /// want of a fingerprint; any other answer means the key is no good here.
    static func asksForTouchID(_ file: KeyFile) -> Bool {
        let context = LAContext()
        context.interactionNotAllowed = true
        do {
            _ = try open(file, context).signature(for: Data("seasnet-vpn probe".utf8))
            return false
        } catch {
            let error = error as NSError
            return error.domain == LAError.errorDomain && error.code == LAError.notInteractive.rawValue
        }
    }
}

// MARK: - Touch ID

/// One approval: a view to show, one evaluation, then at most two signatures.
protocol Approver: AnyObject {
    var view: NSView { get }
    func authorize(_ reply: @escaping @MainActor (Error?) -> Void)
    func sign(_ message: Data) throws -> Data
    func invalidate()
}

final class TouchIDApprover: Approver {
    private let context = LAContext()
    private let file: KeyFile
    private(set) lazy var view: NSView = LAAuthenticationView(context: context, controlSize: .regular)

    init(path: String) throws {
        file = try KeyFile.load(path)
        context.localizedFallbackTitle = ""  // Fingerprint only; no password path exists for this key.
        context.touchIDAuthenticationAllowableReuseDuration = 0
        var error: NSError?
        if !context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) {
            throw error ?? LAError(.biometryNotAvailable)
        }
        guard EnclaveKey.asksForTouchID(file) else { throw KeyFailure.unusable }
    }

    func authorize(_ reply: @escaping @MainActor (Error?) -> Void) {
        guard let control = EnclaveKey.access() else {
            DispatchQueue.main.async { reply(KeyFailure.unusable) }
            return
        }
        // The paired LAAuthenticationView takes the place of the system's sheet.
        context.evaluateAccessControl(control, operation: .useKeySign,
                                      localizedReason: "sign in to the UCLA VPN") { ok, error in
            DispatchQueue.main.async { reply(ok ? nil : error ?? LAError(.authenticationFailed)) }
        }
    }

    func sign(_ message: Data) throws -> Data {
        // Already approved, so this must need nothing more. If it does, it fails
        // here rather than raising a system sheet.
        context.interactionNotAllowed = true
        return try EnclaveKey.open(file, context).signature(for: message).rawRepresentation
    }

    func invalidate() { context.invalidate() }
}

// MARK: - State

struct Problem: Equatable {
    var symbol: String?
    var title: String
    var detail: String
    var retry = true
    var web = true
    var wait = 0
    /// Ask again by itself when the window is back in front.
    var resumes = false

    /// What Touch ID or the key reported; nil when the web window should take over.
    static func from(_ error: Error) -> Problem? {
        let error = error as NSError
        guard error.domain == LAError.errorDomain, let code = LAError.Code(rawValue: error.code) else { return nil }
        switch code {
        case .authenticationFailed:
            return Problem(symbol: "touchid", title: "Fingerprint not recognized", detail: "Try again with an enrolled finger.")
        case .biometryLockout:
            return Problem(symbol: "lock", title: "Touch ID is locked",
                           detail: "Lock this Mac, unlock it with your password, then retry.")
        case .biometryNotEnrolled, .biometryNotAvailable, .biometryNotPaired, .biometryDisconnected, .passcodeNotSet:
            return nil
        default:
            return Problem(symbol: "touchid", title: "Touch ID was interrupted", detail: "Nothing was signed. Retry to touch again.", resumes: true)
        }
    }
}

/// Why the card gives way to the web window; seasnet-vpn words it.
enum HandOver: String {
    case chosen, touchID = "touchid", fingerprints, key, keyFile = "keyfile", refused

    static func from(_ error: Error) -> HandOver {
        switch error as? KeyFailure {
        case .unsafe: return .keyFile
        case .some: return .key
        case nil: break
        }
        let error = error as NSError
        guard error.domain == LAError.errorDomain else { return .key }
        switch LAError.Code(rawValue: error.code) {
        case .biometryNotEnrolled, .passcodeNotSet: return .fingerprints
        case .notInteractive: return .refused
        default: return .touchID
        }
    }
}

enum Phase: Equatable {
    case prompt
    case signing        // Approved; vlt is logging in.
    case connected
    case problem(Problem)
    case handOver       // The web window is opening over this one.
}

struct Asker: Equatable {
    var command = "seasnet-vpn start"
    var by = ""
    var agent = false
}

final class ApprovalModel: ObservableObject {
    @Published var phase = Phase.prompt
    @Published var wait = 0
    /// A new identity for each attempt, so its Touch ID view is rebuilt.
    @Published var attempt = 0
    var asker = Asker()
    var touch: NSView?
    var retry: () -> Void = {}
    var cancel: () -> Void = {}
    var web: () -> Void = {}
}

// MARK: - Look

enum Metrics {
    static let width: CGFloat = 340
    static let height: CGFloat = 424
    static let touch: CGFloat = 64  // LAAuthenticationView's fixed regular size.
}

struct BadgedTile: View {
    var body: some View {
        Tile(size: 60).overlay(alignment: .bottomTrailing) {
            Circle().fill(.orange).frame(width: 24, height: 24)
                .overlay(Image(systemName: "exclamationmark").font(.system(size: 12, weight: .heavy)).foregroundStyle(.white))
                .overlay(Circle().strokeBorder(.background, lineWidth: 2).padding(-2))
                .offset(x: 8, y: 6)
        }
        .accessibilityHidden(true)
    }
}

/// This Mac, the tunnel's lock, and SEASnet; the far half lights up once approved.
struct Tunnel: View {
    let through: Bool
    var broken = false
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "laptopcomputer").font(.system(size: 26, weight: .light))
                .foregroundStyle(Color.ucla).frame(width: 40)
            dots(lit: true)
            if broken { BadgedTile() } else { Tile(size: 60) }
            dots(lit: through)
            Image(systemName: "server.rack").font(.system(size: 22, weight: .light))
                .foregroundStyle(through ? AnyShapeStyle(Color.ucla) : AnyShapeStyle(.tertiary)).frame(width: 40)
        }
        .accessibilityHidden(true)
    }

    private func dots(lit: Bool) -> some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { _ in
                Circle().fill(lit ? AnyShapeStyle(Color.ucla) : AnyShapeStyle(.quaternary)).frame(width: 4, height: 4)
            }
        }
    }
}

struct TouchID: NSViewRepresentable {
    let view: NSView
    func makeNSView(context: Context) -> NSView { view }
    func updateNSView(_ view: NSView, context: Context) {}
}

/// Who is asking: the command, and where it was run when that is known.
struct AskerCard: View {
    let asker: Asker
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: asker.agent ? "sparkles" : "terminal")
                .font(.system(size: 14, weight: .medium)).foregroundStyle(Color.ucla).frame(width: 20)
            VStack(alignment: .leading, spacing: 1) {
                Text(asker.command).font(.system(size: 12, weight: .medium, design: .monospaced))
                    .lineLimit(1).truncationMode(.middle)
                if !asker.by.isEmpty {
                    Text(asker.by).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .frame(height: 46)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(.primary.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(.primary.opacity(0.07), lineWidth: 1))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Requested by \(asker.command)\(asker.by.isEmpty ? "" : ", \(asker.by)")")
    }
}

struct Progress: View {
    let connected: Bool
    private let stops = ["Approved", "UCLA and Duo", "Connected"]

    var body: some View {
        let current = connected ? 2 : 1
        HStack(alignment: .top, spacing: 0) {
            ForEach(stops.indices, id: \.self) { i in
                VStack(spacing: 8) {
                    ZStack {
                        HStack(spacing: 0) {
                            line(i > 0, lit: i <= current).padding(.trailing, 18)
                            line(i < 2, lit: i < current).padding(.leading, 18)
                        }
                        Circle().fill(i < current || connected ? AnyShapeStyle(Color.uclaFill)
                                      : AnyShapeStyle(.quaternary)).frame(width: 26, height: 26)
                        if i < current || connected {
                            Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(.white)
                        } else if i == current {
                            ProgressView().controlSize(.small)
                        }
                    }
                    .frame(height: 26)
                    Text(stops[i]).font(.system(size: 11, weight: i == current ? .semibold : .regular))
                        .foregroundStyle(i <= current ? .primary : .secondary)
                }
                .frame(maxWidth: .infinity)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(connected ? "Connected" : "Approved. Signing in to UCLA and Duo")
    }

    private func line(_ shown: Bool, lit: Bool) -> some View {
        Rectangle().fill(lit ? AnyShapeStyle(Color.ucla) : AnyShapeStyle(.quaternary))
            .frame(height: 2).opacity(shown ? 1 : 0)
    }
}

struct Card: View {
    @ObservedObject var model: ApprovalModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 0) {
            switch model.phase {
            case .prompt: prompt
            case .signing: progress(connected: false)
            case .connected: progress(connected: true)
            case .problem(let problem): self.problem(problem)
            case .handOver: handOver
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 34).padding(.bottom, 22)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(LinearGradient(colors: [Color.ucla.opacity(0.16), Color.ucla.opacity(0)],
                                   startPoint: .top, endPoint: .init(x: 0.5, y: 0.45)))
        .ignoresSafeArea()
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: model.phase)
    }

    private func heading(_ title: String, _ detail: String) -> some View {
        VStack(spacing: 4) {
            Text(title).font(.system(size: 17, weight: .semibold)).accessibilityAddTraits(.isHeader)
            Text(detail).font(.system(size: 13)).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).lineSpacing(1.5).fixedSize(horizontal: false, vertical: true)
        }
    }

    private var prompt: some View {
        Group {
            Tile(size: 60)
            heading("Allow UCLA VPN sign-in?", "Connects this Mac to SEASnet.").padding(.top, 16)
            AskerCard(asker: model.asker).padding(.top, 16)
            Spacer(minLength: 0)
            ZStack {
                if let touch = model.touch {
                    TouchID(view: touch).id(model.attempt).frame(width: Metrics.touch, height: Metrics.touch)
                }
            }
            .frame(width: Metrics.touch, height: Metrics.touch)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Touch ID")
            Text("Touch ID to sign in").font(.system(size: 13, weight: .medium)).padding(.top, 8)
            Spacer(minLength: 0)
            wide("Cancel", action: model.cancel)
        }
    }

    private func progress(connected: Bool) -> some View {
        Group {
            Tunnel(through: true)
            heading(connected ? "Connected" : "Signing in…",
                    connected ? "Handing the sign-in to the VPN." : "vlt is signing in to UCLA for you.").padding(.top, 16)
            AskerCard(asker: model.asker).padding(.top, 16)
            Spacer(minLength: 0)
            Progress(connected: connected)
            Spacer(minLength: 0)
            wide("Cancel", action: model.cancel).opacity(connected ? 0 : 1).disabled(connected)
        }
    }

    private var handOver: some View {
        Group {
            Tile(size: 60)
            heading("Opening UCLA sign-in", "Sign in with your password instead.").padding(.top, 16)
            AskerCard(asker: model.asker).padding(.top, 16)
            Spacer(minLength: 0)
            ProgressView().controlSize(.regular)
            Spacer(minLength: 0)
            wide("Cancel", action: model.cancel)
        }
    }

    private func problem(_ problem: Problem) -> some View {
        Group {
            // Touch ID and key trouble shows its own symbol; a login that stopped
            // along the way shows the tunnel cut short.
            if problem.symbol == nil { Tunnel(through: false, broken: true) } else { BadgedTile() }
            heading(problem.title, problem.detail).padding(.top, 18)
            if problem.symbol == nil { AskerCard(asker: model.asker).padding(.top, 16) }
            Spacer(minLength: 0)
            if let symbol = problem.symbol {
                Image(systemName: symbol).font(.system(size: 44, weight: .light)).foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
                Spacer(minLength: 0)
            }
            VStack(spacing: 8) {
                if problem.retry, model.wait > 0 {
                    Text("You can retry in \(model.wait) s").font(.system(size: 13)).foregroundStyle(.secondary)
                        .monospacedDigit().frame(height: 28)
                } else if problem.retry {
                    wide("Retry", primary: true, action: model.retry)
                }
                if problem.web {
                    wide("Sign in here instead", primary: !problem.retry || model.wait > 0, action: model.web)
                }
                wide(problem.retry || problem.web ? "Cancel" : "Close", action: model.cancel)
            }
        }
    }

    @ViewBuilder
    private func wide(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> some View {
        let label = Text(title).frame(maxWidth: .infinity)
        if primary {
            Button(action: action) { label }.buttonStyle(.borderedProminent).tint(Color.uclaFill)
                .controlSize(.large).buttonBorderShape(.capsule).keyboardShortcut(.defaultAction)
        } else {
            Button(action: action) { label }.buttonStyle(.bordered)
                .controlSize(.large).buttonBorderShape(.capsule)
        }
    }
}

// MARK: - Window

final class ApprovalController: NSObject, NSWindowDelegate {
    let url: String
    let model = ApprovalModel()
    let window: EscapeWindow
    let interactive: Bool
    let makeApprover: () throws -> Approver
    var emit: ([String: String]) -> Void = { _ in }
    /// Called once with the exit code; the window is already hidden.
    var finish: (Int32) -> Void = { exit($0) }
    private(set) var over = false
    private var approver: Approver?
    private var approved = false
    private var signatures = 0
    private var countdown: Timer?
    /// Bumped whenever an approval ends, so its late answers are ignored.
    private var generation = 0
    private var lostKey = false

    init(url: String, asker: Asker, interactive: Bool, makeApprover: @escaping () throws -> Approver) {
        self.url = url
        self.interactive = interactive
        self.makeApprover = makeApprover
        window = ApprovalController.makeWindow()
        super.init()
        model.asker = asker
        model.cancel = { [weak self] in self?.end(1) }
        model.retry = { [weak self] in self?.ask() }
        model.web = { [weak self] in self?.handOver(.chosen) }
        window.contentView = ApprovalController.root(model: model)
        window.delegate = self
        window.onEscape = { [weak self] in self?.end(1) }
    }

    static func makeWindow() -> EscapeWindow {
        let window = EscapeWindow(contentRect: NSRect(x: 0, y: 0, width: Metrics.width, height: Metrics.height),
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
        window.level = .floating
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.tabbingMode = .disallowed
        return window
    }

    static func root(model: ApprovalModel, asKey: Bool = false) -> NSView {
        let frame = NSRect(x: 0, y: 0, width: Metrics.width, height: Metrics.height)
        let material = NSVisualEffectView(frame: frame)
        material.material = .popover
        material.blendingMode = .behindWindow
        material.state = .active
        let card = Card(model: model)
        let chrome: NSView & HostingSizing = asKey  // Previews have no key window to take it from.
            ? NSHostingView(rootView: card.environment(\.controlActiveState, .key)) : NSHostingView(rootView: card)
        chrome.sizingOptions = []
        chrome.frame = frame
        chrome.autoresizingMask = [.width, .height]
        material.addSubview(chrome)
        return material
    }

    func present() {
        if let area = workingArea() {
            let size = window.frame.size
            // A little above centre, where alerts sit.
            window.setFrameOrigin(NSPoint(x: (area.midX - size.width / 2).rounded(),
                                          y: (area.midY - size.height / 2 + area.height * 0.06).rounded()))
        }
        bringUp(window)
    }

    /// A fresh Touch ID request; every retry comes through here.
    func ask() {
        guard !over, model.wait == 0, model.phase != .handOver else { return }
        reset()
        do {
            let approver = try makeApprover()
            self.approver = approver
            model.touch = approver.view
            model.attempt += 1
            model.phase = .prompt
            let generation = self.generation
            // Next turn, so the Touch ID view is in the window before it is asked.
            DispatchQueue.main.async { [weak self] in
                guard let self, !self.over, self.generation == generation else { return }
                if self.interactive { self.window.displayIfNeeded() }
                approver.authorize { [weak self] error in self?.authorized(generation, error) }
            }
        } catch {
            fail(error)
        }
    }

    private func fail(_ error: Error) {
        if let problem = Problem.from(error) { fail(problem) } else { handOver(.from(error)) }
    }

    /// Stay up, approval dropped, until seasnet-vpn has the web window open.
    private func handOver(_ why: HandOver) {
        guard !over, model.phase != .handOver else { return }
        reset()
        model.phase = .handOver
        say("Opening UCLA sign-in.")
        emit(["event": "web", "why": why.rawValue])
    }

    private func authorized(_ generation: Int, _ error: Error?) {
        guard !over, generation == self.generation, !approved else { return }
        if let error {
            fail(error)
            return
        }
        approved = true
        model.phase = .signing
        say("Approved. Signing in.")
        emit(["event": "approved", "n": String(generation)])
    }

    func handle(_ command: [String: Any]) {
        guard !over, model.phase != .handOver else { return }
        switch command["do"] as? String {
        case "sign":
            // One touch covers one login: its challenge, and one replacement if that expired.
            guard approved, signatures < 2, let approver, command["n"] as? String == String(generation),
                  let challenge = command["challenge"] as? String,
                  let message = Contract.message(challenge: challenge, url: url) else {
                emit(["event": "unsigned"])
                if approved, command["n"] as? String == String(generation) {
                    fail(Problem(title: "The sign-in couldn’t be signed", detail: "Nothing was sent. Touch again to retry."))
                }
                return
            }
            signatures += 1
            do {
                emit(["event": "signed", "challenge": challenge, "signature": try approver.sign(message).base64URL])
            } catch {
                // Approved, and still it would not sign: not something a touch fixes.
                emit(["event": "unsigned"])
                handOver(HandOver.from(error))
            }
        case "problem":
            guard let title = command["title"] as? String, let detail = command["detail"] as? String else { return }
            fail(Problem(title: title, detail: detail, retry: command["retry"] as? Bool ?? false,
                         web: command["web"] as? Bool ?? true, wait: command["wait"] as? Int ?? 0))
        case "web":  // seasnet-vpn has decided, and knows why.
            reset()
            model.phase = .handOver
        case "done":
            model.phase = .connected
            end(0)
        default:
            break
        }
    }

    private func reset() {
        generation += 1
        approver?.invalidate()
        approver = nil
        approved = false
        signatures = 0
        model.touch = nil
        countdown?.invalidate()
        model.wait = 0
    }

    private func fail(_ problem: Problem) {
        reset()
        model.wait = max(0, min(problem.wait, 3600))
        model.phase = .problem(problem)
        say("\(problem.title). \(problem.detail)")
        guard model.wait > 0 else { return }
        countdown = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] timer in
            guard let self, self.model.wait > 0 else { timer.invalidate(); return }
            self.model.wait -= 1
        }
    }

    private func say(_ text: String) {
        guard interactive else { return }
        NSAccessibility.post(element: window, notification: .announcementRequested,
                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }

    /// Off screen in this very call: no fade, no closing animation.
    func end(_ code: Int32) {
        guard !over else { return }
        over = true
        window.animationBehavior = .none
        window.alphaValue = 0
        window.orderOut(nil)
        reset()
        finish(code)
    }

    // Touch ID stops when the window loses the keys; it picks up on return.
    func windowDidResignKey(_ notification: Notification) { lostKey = true }

    func windowDidBecomeKey(_ notification: Notification) {
        guard lostKey, case .problem(let problem) = model.phase, problem.resumes else { return }
        lostKey = false
        ask()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        end(1)
        return false
    }
}

// MARK: - App

final class ApprovalDelegate: NSObject, NSApplicationDelegate {
    let controller: ApprovalController
    private var begun = false
    init(_ controller: ApprovalController) { self.controller = controller }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = ApprovalDelegate.menu()
        controller.present()
        guard controller.window.isVisible else { exit(70) }
        controller.emit(["event": "ready"])
        // Touch ID answers only to the active app, and activation lands a beat later.
        if NSApp.isActive { begin() } else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in self?.begin() } }
        Thread.detachNewThread { [controller] in
            while let line = readLine() {
                guard let command = parse(line) else { continue }
                DispatchQueue.main.async { controller.handle(command) }
            }
            // The supervisor only ever closes this pipe, by hand or by dying.
            DispatchQueue.main.async { controller.end(4) }
        }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        if NSApp.mainMenu != nil, controller.window.isVisible { begin() }
    }

    private func begin() {
        guard !begun else { return }
        begun = true
        controller.ask()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        controller.end(1)
        return .terminateCancel
    }

    @objc func cancelSignIn(_ sender: Any?) { controller.end(1) }

    /// An accessory app shows no menu bar, but these key equivalents still work.
    static func menu() -> NSMenu {
        let main = NSMenu(), item = NSMenuItem(), menu = NSMenu(title: windowTitle)
        menu.addItem(withTitle: "Close", action: #selector(ApprovalDelegate.cancelSignIn(_:)), keyEquivalent: "w")
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = menu
        main.addItem(item)
        return main
    }
}

func keyCommand(_ action: () throws -> Data) -> Never {
    do {
        print(try action().base64URL)
        exit(0)
    } catch {
        let text: String
        switch error as? KeyFailure {
        case .missing: text = "There is no approval key yet. Create one with seasnet-vpn approve-key --create."
        case .exists: text = "An approval key already exists. --create --replace makes a new one (the old one is kept aside)."
        case .unsafe: text = "The approval key file is not a regular file of yours with mode 600."
        case .unusable: text = "The approval key file is damaged or from another Mac."
        case .noEnclave: text = "This Mac has no Secure Enclave."
        case .noFingerprints: text = "Touch ID is not available, or no fingerprints are enrolled."
        case .other(let reason): text = reason
        case nil: text = (error as NSError).localizedDescription
        }
        FileHandle.standardError.write(Data((text + "\n").utf8))
        exit(error as? KeyFailure == .exists ? 3 : 1)
    }
}

func runApproval() -> Never {
    guard let line = readLine(), let request = parse(line), let url = request["url"] as? String, Contract.accepts(url),
          let key = request["key"] as? String, key.hasPrefix("/") else { exit(64) }
    let asker = Asker(command: String((request["command"] as? String ?? "seasnet-vpn").prefix(60)),
                      by: String((request["by"] as? String ?? "").prefix(80)), agent: request["agent"] as? Bool ?? false)
    let controller = ApprovalController(url: url, asker: asker, interactive: true) { try TouchIDApprover(path: key) }
    controller.emit = writeLine
    runAccessoryApp(ApprovalDelegate(controller))
}

#if TESTING
// Tests and previews. Never in the app seasnet-vpn builds, and the only place a
// software key exists.
import AuthenticationServices
import Cocoa
import CryptoKit
import LocalAuthentication
import SwiftUI
import WebKit

final class SoftwareApprover: Approver {
    let key = P256.Signing.PrivateKey()
    let view = NSView()
    var signError: Error?
    private(set) var dead = false
    private var authorized = false
    private var reply: ((Error?) -> Void)?
    func authorize(_ reply: @escaping @MainActor (Error?) -> Void) {
        self.reply = { error in MainActor.assumeIsolated { reply(error) } }
    }
    func touch(_ error: Error?) {
        authorized = error == nil
        reply?(error)
    }
    func sign(_ message: Data) throws -> Data {
        guard authorized, !dead else { throw LAError(.notInteractive) }
        if let signError { throw signError }
        return try key.signature(for: message).rawRepresentation
    }
    func invalidate() { dead = true }
}

func fromBase64URL(_ text: String) -> Data {
    var text = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    while text.count % 4 != 0 { text += "=" }
    return Data(base64Encoded: text) ?? Data()
}

/// A stand-in for LAAuthenticationView, which draws nothing until the system runs it.
struct TouchStandIn: View {
    var body: some View {
        Image(systemName: "touchid").font(.system(size: 54, weight: .light))
            .foregroundStyle(Color.pair(0xFF375F, 0xFF4F70)).frame(width: Metrics.touch, height: Metrics.touch)
    }
}

func renderPreviews(to directory: String) -> Never {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let out = URL(fileURLWithPath: directory, isDirectory: true)
    try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
    let person = Asker(command: "ssh seasnet", by: "In the main space"), agent = Asker(command: "relayctl push", by: "An agent: Claude, agent D", agent: true)
    let plain = Asker(command: "seasnet-vpn start")
    func la(_ code: LAError.Code) -> Phase { .problem(Problem.from(LAError(code))!) }
    let states: [(String, Phase, Asker, Int)] = [
        ("01-prompt", .prompt, person, 0), ("02-prompt-agent", .prompt, agent, 0), ("03-prompt-plain", .prompt, plain, 0),
        ("04-signing-in", .signing, person, 0), ("05-connected", .connected, person, 0),
        ("06-fingerprint-not-recognized", la(.authenticationFailed), person, 0), ("07-touchid-locked", la(.biometryLockout), person, 0),
        ("11-touchid-interrupted", la(.systemCancel), person, 0),
        ("12-login-failed", .problem(Problem(title: "UCLA sign-in failed", detail: "vlt couldn’t finish the UCLA login. Run the command again to start fresh.", retry: false)), person, 0),
        ("13-rate-limited", .problem(Problem(title: "Too many sign-ins", detail: "vlt is limiting VPN logins.", wait: 42)), person, 42),
        ("15-vpn-gone", .problem(Problem(title: "The VPN stopped waiting", detail: "Start it again from the terminal.", retry: false, web: false)), person, 0),
        ("16-hand-over", .handOver, person, 0),
        ("17-answer-lost", .problem(Problem(title: "vlt’s answer was lost", detail: "The sign-in may have run, so it won’t be sent again. Run the command again to start fresh.", retry: false)), person, 0),
    ]
    for (scheme, name) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
        for (title, phase, asker, wait) in states {
            let model = ApprovalModel()
            (model.phase, model.asker, model.wait) = (phase, asker, wait)
            model.touch = NSHostingView(rootView: TouchStandIn())
            let window = ApprovalController.makeWindow()
            window.appearance = NSAppearance(named: name)
            window.contentView = ApprovalController.root(model: model, asKey: true)
            writePreview(window, out.appendingPathComponent("\(title)-\(scheme).png"))
        }
    }
    renderWebPreviews(to: out)
    exit(0)
}

func writePreview(_ window: NSWindow, _ file: URL) {
    window.layoutIfNeeded()
    RunLoop.main.run(until: Date().addingTimeInterval(0.35))
    let view = window.contentView!.superview!
    let size = view.bounds.size
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2),
                                     pixelsHigh: Int(size.height * 2), bitsPerSample: 8, samplesPerPixel: 4,
                                     hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
    rep.size = size
    view.cacheDisplay(in: view.bounds, to: rep)
    try? rep.representation(using: .png, properties: [:])?.write(to: file)
}

func spin(_ seconds: TimeInterval, until done: () -> Bool = { false }) {
    let deadline = Date().addingTimeInterval(seconds)
    while !done(), Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
}

func selfTest(_ directory: String) -> Never {
    NSApplication.shared.setActivationPolicy(.prohibited)
    var failures = 0, skipped = false
    func check(_ ok: Bool, _ name: String) {
        print("\(ok ? "ok  " : "FAIL") \(name)")
        if !ok { failures += 1 }
    }

    let url = "https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login?tgname=x&acsamlcap=v2"
    check(Contract.accepts(url), "accepts UCLA's VPN login")
    for raw in ["", "http://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login", "https://ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login",
                "https://v2.ssl.vpn.ucla.edu.evil.com/+CSCOE+/saml/sp/login", "https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login/x",
                "https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/../sp/login", "https://v2.ssl.vpn.ucla.edu/", "https://a@v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login",
                "https://v2.ssl.vpn.ucla.edu:444/+CSCOE+/saml/sp/login", url + " x", url + "\n"] {
        check(!Contract.accepts(raw), "refuses \(raw.debugDescription)")
    }
    let challenge = String(repeating: "A", count: 42) + "_"
    for bad in ["", "short", String(repeating: "A", count: 44), String(repeating: "A", count: 42) + "=", challenge + "\n", challenge.dropLast() + "\n"] {
        check(Contract.message(challenge: bad, url: url) == nil, "refuses challenge \(bad.debugDescription)")
    }

    func make(failing: Error? = nil)
        -> (ApprovalController, () -> [[String: String]], () -> Int32?, () -> [SoftwareApprover]) {
        var events: [[String: String]] = [], code: Int32?, made: [SoftwareApprover] = []
        let controller = ApprovalController(url: url, asker: Asker(), interactive: false) {
            if let failing { throw failing }
            let approver = SoftwareApprover()
            made.append(approver)
            return approver
        }
        controller.emit = { events.append($0) }
        controller.finish = { code = $0 }
        return (controller, { events }, { code }, { made })
    }
    func prompt(_ controller: ApprovalController, _ made: () -> [SoftwareApprover]) -> SoftwareApprover {
        controller.ask()
        spin(0.1)
        return made().last!
    }

    let (login, events, code, made) = make()
    check(login.window.title == windowTitle && login.window.level == .floating
          && login.window.collectionBehavior.contains(.moveToActiveSpace)
          && !login.window.styleMask.contains(.resizable) && !login.window.isVisible, "window attributes, not on screen")
    let key = prompt(login, made)
    login.handle(["do": "sign", "challenge": challenge, "n": "1"])
    check(events() == [["event": "unsigned"]] && login.model.phase == .prompt, "nothing is signed before Touch ID")
    key.touch(nil)
    let n = events().last!["n"]!
    check(events().last!["event"] == "approved" && login.model.phase == .signing, "touch approves")
    login.handle(["do": "sign", "challenge": challenge, "n": "0"])
    check(events().last! == ["event": "unsigned"], "a request for another approval is not signed")
    for text in [challenge, String(repeating: "B", count: 43), String(repeating: "C", count: 43)] {
        login.handle(["do": "sign", "challenge": text, "n": n])
    }
    let signed = events().filter { $0["event"] == "signed" }
    check(signed.count == 2, "one touch covers a challenge and one replacement")
    let raw = fromBase64URL(signed[0]["signature"]!)
    let message = Contract.message(challenge: challenge, url: url)!
    check(raw.count == 64 && key.key.publicKey.isValidSignature(try! P256.Signing.ECDSASignature(rawRepresentation: raw), for: message),
          "signature is raw r||s over the contract's bytes")
    login.handle(["do": "done"])
    check(code() == 0 && login.model.phase == .connected && key.dead, "done closes at once and drops the approval")

    var (again, againEvents, againCode, againMade) = make()
    prompt(again, againMade).touch(nil)
    let first = againEvents().last!["n"]!
    again.handle(["do": "problem", "title": "UCLA sign-in failed", "detail": "x", "retry": true])
    check(againMade()[0].dead, "a problem ends the approval")
    again.handle(["do": "sign", "challenge": challenge, "n": first])
    check(againEvents().last! == ["event": "unsigned"], "no signature after a problem")
    again.model.retry()
    spin(0.1)
    check(againMade().count == 2 && again.model.phase == .prompt, "retry asks for a new touch")
    againMade()[0].touch(nil)
    check(again.model.phase == .prompt, "a late answer to the old touch is ignored")
    againMade()[1].signError = LAError(.notInteractive)
    againMade()[1].touch(nil)
    again.handle(["do": "sign", "challenge": challenge, "n": againEvents().last!["n"]!])
    check(!againEvents().contains { $0["event"] == "signed" }, "a refused signature is reported, not invented")
    check(againEvents().last! == ["event": "web", "why": "refused"], "a key that won't sign after a touch hands over")
    (again, againEvents, againCode, againMade) = make()
    again.ask()
    spin(0.1)
    again.handle(["do": "problem", "title": "Too many", "detail": "x", "retry": true, "wait": 2])
    again.model.retry()
    check(again.model.phase != .prompt && again.model.wait == 2, "retry waits out a rate limit")
    spin(3) { again.model.wait == 0 }
    again.model.retry()
    check(again.model.phase == .prompt, "then allows it")
    spin(0.1)
    againMade().last!.touch(nil)
    let live = againEvents().last!["n"]!
    again.model.web()
    check(againEvents().last! == ["event": "web", "why": "chosen"] && again.model.phase == .handOver && againCode() == nil
          && againMade().last!.dead, "signing in here drops the approval and stays up")
    let quiet = againEvents().count
    again.handle(["do": "sign", "challenge": challenge, "n": live])
    again.handle(["do": "problem", "title": "x", "detail": "x", "retry": true])
    again.model.retry()
    again.model.web()
    check(again.model.phase == .handOver && againEvents().count == quiet, "and nothing brings it back")
    again.model.cancel()
    check(againCode() == 1, "but Cancel still cancels")

    let (early, _, earlyCode, earlyMade) = make()
    let pending = prompt(early, earlyMade)
    early.handle(["do": "problem", "title": "Too many sign-ins", "detail": "x", "retry": true])
    pending.touch(LAError(.appCancel))
    if case .problem(let problem) = early.model.phase { check(problem.title == "Too many sign-ins" && earlyCode() == nil, "a cancelled touch keeps the real problem") }
    else { check(false, "a cancelled touch keeps the real problem") }

    for (error, word) in [(LAError(.authenticationFailed), "recognized"), (LAError(.biometryLockout), "locked"),
                          (LAError(.systemCancel), "interrupted"), (LAError(.notInteractive), "interrupted")] as [(Error, String)] {
        let (one, oneEvents, _, oneMade) = make()
        prompt(one, oneMade).touch(error)
        if case .problem(let problem) = one.model.phase {
            check(problem.retry && problem.web && problem.title.contains(word) && oneEvents().isEmpty, "\(word): retry, or sign in here")
        } else { check(false, "\(word): retry, or sign in here") }
    }
    for (error, why) in [(LAError(.biometryNotAvailable), "touchid"), (LAError(.biometryDisconnected), "touchid"), (LAError(.biometryNotPaired), "touchid"),
                         (LAError(.biometryNotEnrolled), "fingerprints"), (KeyFailure.missing, "key"), (KeyFailure.unusable, "key"),
                         (KeyFailure.unsafe, "keyfile"), (NSError(domain: "CryptoTokenKit", code: -3), "key")] as [(Error, String)] {
        let (one, oneEvents, oneCode, _) = make(failing: error)
        one.ask()
        check(one.model.phase == .handOver && oneEvents() == [["event": "web", "why": why]] && oneCode() == nil, "\(why): hands over by itself")
        let (late, lateEvents, _, lateMade) = make()
        prompt(late, lateMade).touch(error)
        check(late.model.phase == .handOver && lateEvents() == [["event": "web", "why": why]], "\(why) during the prompt: hands over")
    }
    let (told, toldEvents, _, toldMade) = make()
    let toldTouch = prompt(told, toldMade)
    told.handle(["do": "web"])
    toldTouch.touch(nil)
    check(told.model.phase == .handOver && toldEvents().isEmpty && toldTouch.dead, "told to hand over, a late touch approves nothing")

    let escape = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil,
                                  characters: "\u{1b}", charactersIgnoringModifiers: "\u{1b}", isARepeat: false, keyCode: 53)!
    for (name, act) in [("Esc", { $0.window.keyDown(with: escape) }), ("cancelOperation", { $0.window.cancelOperation(nil) }),
                        ("close", { _ = $0.windowShouldClose($0.window) }), ("Cmd-W", { ApprovalDelegate($0).cancelSignIn(nil) }),
                        ("Cancel", { $0.model.cancel() })] as [(String, (ApprovalController) -> Void)] {
        for approve in [false, true] {
            let (one, oneEvents, oneCode, oneMade) = make()
            let touch = prompt(one, oneMade)
            if approve { touch.touch(nil) }
            act(one)
            touch.touch(nil)
            one.handle(["do": "sign", "challenge": challenge, "n": "1"])
            check(oneCode() == 1 && oneEvents().count == (approve ? 1 : 0) && touch.dead, "\(name) cancels \(approve ? "after" : "before") the touch")
        }
    }

    // The real enclave, with interaction forbidden throughout.
    let path = directory + "/approve-key.json"
    // With the screen locked the enclave makes no keys (errSecInteractionNotAllowed):
    // skip then. Any other failure to make one is a failure.
    var fresh: Data?
    if SecureEnclave.isAvailable, LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil) {
        do { fresh = try EnclaveKey.create(path) } catch {
            check((error as NSError).code == Int(errSecInteractionNotAllowed), "the enclave makes a key")
        }
    }
    if let made = fresh {
        var info = stat()
        check(stat(path, &info) == 0 && info.st_mode & 0o777 == 0o600, "key file is mode 600")
        check(made.count == 65 && made[0] == 4 && (try? EnclaveKey.publicKey(path)) == made, "public key is x963 and stable")
        check((try? EnclaveKey.create(path)) == nil && (try? EnclaveKey.publicKey(path)) == made, "create never overwrites")
        let file = try! KeyFile.load(path)
        check(EnclaveKey.asksForTouchID(file), "the enclave refuses to sign without Touch ID")
        if let approver = try? TouchIDApprover(path: path) {
            check((try? approver.sign(message)) == nil, "the real approver cannot sign unapproved")
        } else { check(false, "the real approver loads") }
        var damaged = file
        damaged.key[damaged.key.count / 2] ^= 0xff
        check(!EnclaveKey.asksForTouchID(damaged), "a damaged key is noticed before asking")
        chmod(path, 0o644)
        do { _ = try KeyFile.load(path); check(false, "loose permissions refused") } catch { check(error as? KeyFailure == .unsafe, "loose permissions refused") }
        symlink(path, path + ".link")
        do { _ = try KeyFile.load(path + ".link"); check(false, "symlink refused") } catch { check(error as? KeyFailure == .unsafe, "symlink refused") }
        do { _ = try KeyFile.load(path + ".none"); check(false, "missing key") } catch { check(error as? KeyFailure == .missing, "missing key") }
    } else {
        skipped = true
    }
    webSelfTest(check)
    browserSelfTest(check)
    if failures > 0 {
        print("self-test: \(failures) failed")
        exit(1)
    }
    if skipped {
        print("SKIPPED: the Secure Enclave checks did not run (no usable Touch ID right now). Everything else passed.")
        exit(77)
    }
    print("self-test passed")
    exit(0)
}

struct MockPage: View {
    let duo: Bool
    var body: some View {
        VStack(alignment: duo ? .center : .leading, spacing: 16) {
            if duo {
                Text("Check for a push").font(.system(size: 22, weight: .semibold))
                RoundedRectangle(cornerRadius: 22).fill(Color(white: 0.93)).frame(width: 96, height: 160)
                    .overlay(Image(systemName: "checkmark.circle.fill").font(.system(size: 34)).foregroundStyle(.green))
                Text("Placeholder for the Duo prompt").foregroundStyle(Color(white: 0.4))
            } else {
                Text("Sign in").font(.system(size: 24, weight: .semibold))
                ForEach(["User name", "Password"], id: \.self) { label in
                    Text(label).foregroundStyle(Color(white: 0.5)).padding(.horizontal, 12)
                        .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
                        .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Color(white: 0.75)))
                }
                Text("Continue").fontWeight(.medium).foregroundStyle(.white).padding(.horizontal, 22).frame(height: 40)
                    .background(RoundedRectangle(cornerRadius: 4).fill(Color(white: 0.3)))
                Text("Placeholder page").foregroundStyle(Color(white: 0.5)).padding(.top, 8)
            }
        }
        .font(.system(size: 14))
        .foregroundStyle(.black)
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: duo ? .center : .topLeading)
        .background(.white)
        .environment(\.colorScheme, .light)
    }
}

func renderWebPreviews(to out: URL) {
    let ucla = "shb.ais.ucla.edu", duo = "api-0123abcd.duosecurity.com"
    let error = WebPhase.failed(title: "Couldn’t open UCLA sign-in",
                                detail: "This Mac looks offline.\nCheck the connection, then retry.")
    let states: [(String, WebPhase, String, Int, String?, String)] = [
        ("web-1-opening", .opening, "", 0, nil, "vlt couldn’t be reached, so sign in here."),
        ("web-2-login", .login, ucla, 0, nil, "vlt couldn’t be reached, so sign in here."),
        ("web-3-login-blocked-link", .login, ucla, 0, "Blocked example.com. Only UCLA and Duo pages open here.", ""),
        ("web-4-duo", .duo, duo, 1, nil, "Touch ID isn’t available, so sign in here."),
        ("web-5-connecting", .connecting, "", 2, nil, ""),
        ("web-6-error", error, "", 0, nil, ""), ("web-7-handoff-error", WebController.Lost.closed.phase, "", 2, nil, ""),
        ("web-9-went-quiet", WebController.Lost.silent.phase, "", 2, nil, ""),
        ("web-10-refused", WebController.Lost.refused(404).phase, "", 2, nil, ""),
        ("web-8-cancelled", .ended, "", 2, nil, ""),
    ]
    for (scheme, name) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
        for (title, reason, closed) in [("zen-1-continue", "", false), ("zen-2-continue-with-reason", "vlt couldn’t be reached", false),
                                        ("zen-3-window-closed", "vlt couldn’t be reached", true)] {
            let model = BrowserModel()
            (model.browser, model.reason, model.closed) = ("Zen", reason, closed)
            let window = BrowserController.makeWindow()
            window.appearance = NSAppearance(named: name)
            window.contentView = BrowserController.root(model: model, asKey: true)
            writePreview(window, out.appendingPathComponent("\(title)-\(scheme).png"))
        }
    }
    for (scheme, name) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
        for (title, phase, host, reached, notice, reason) in states {
            let model = WebModel()
            (model.phase, model.host, model.reached, model.notice, model.reason) = (phase, host, reached, notice, reason)
            let page = NSHostingView(rootView: MockPage(duo: phase == .duo))
            let window = WebController.makeWindow()
            window.appearance = NSAppearance(named: name)
            window.contentView = WebController.root(model: model, page: page, asKey: true)
            page.alphaValue = phase.showsPage ? 1 : 0
            page.isHidden = !phase.showsPage
            writePreview(window, out.appendingPathComponent("\(title)-\(scheme).png"))
        }
    }
}

/// openconnect's side of the hand-off (hpke.c): one GET on [::1], a 302, done.
final class FakeListener {
    private let fd = socket(AF_INET6, SOCK_STREAM, 0)
    private(set) var port = 0
    private(set) var requests: [String] = []
    private let lock = NSLock()

    init(answer: Bool = true, reply text: String? = nil) {
        var address = sockaddr_in6()
        address.sin6_family = sa_family_t(AF_INET6)
        address.sin6_addr = in6addr_loopback
        var length = socklen_t(MemoryLayout<sockaddr_in6>.size)
        withUnsafeMutablePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { raw in
                _ = bind(fd, raw, length)
                _ = getsockname(fd, raw, &length)
            }
        }
        port = Int(UInt16(bigEndian: address.sin6_port))
        guard answer else { close(fd); return }  // A port nothing listens on.
        listen(fd, 1)
        Thread.detachNewThread { [self] in
            while true {
                let client = accept(fd, nil, nil)
                guard client >= 0 else { return }
                var data = Data(), buffer = [UInt8](repeating: 0, count: 4096)
                while !data.suffix(4).elementsEqual(Array("\r\n\r\n".utf8)) {
                    let count = read(client, &buffer, buffer.count)
                    guard count > 0 else { break }
                    data.append(contentsOf: buffer[0..<count])
                }
                let line = String(decoding: data, as: UTF8.self).components(separatedBy: "\r\n")[0]
                lock.withLock { requests.append(line) }
                let reply = text ?? "HTTP/1.1 302 Found\r\nConnection: close\r\nContent-Length: 0\r\n"
                    + "Location: https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml_ac_login.html\r\n\r\n"
                _ = reply.withCString { write(client, $0, strlen($0)) }
                close(client)
            }
        }
    }
    var seen: [String] { lock.withLock { requests } }
}

/// UCLA's VPN server after the assertion: plain HTTP on 127.0.0.1, one answer per path.
final class MockGateway {
    private let fd = socket(AF_INET, SOCK_STREAM, 0)
    private(set) var port = 0
    private var log: [String] = []
    private let lock = NSLock()
    private let answer: (String, String) -> String
    var seen: [String] { lock.withLock { log } }

    static func page(_ body: String, headers: String = "") -> String {
        "HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/html\r\n\(headers)Content-Length: \(body.utf8.count)\r\n\r\n\(body)"
    }
    static func redirect(_ to: String) -> String {
        "HTTP/1.1 302 Found\r\nConnection: close\r\nLocation: \(to)\r\nContent-Length: 0\r\n\r\n"
    }

    init(_ answer: @escaping (String, String) -> String) {
        self.answer = answer
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        withUnsafeMutablePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { raw in
                _ = bind(fd, raw, length)
                _ = getsockname(fd, raw, &length)
            }
        }
        port = Int(UInt16(bigEndian: address.sin_port))
        listen(fd, 16)
        Thread.detachNewThread { [self] in
            while true {
                let client = accept(fd, nil, nil)
                guard client >= 0 else { return }
                var on: Int32 = 1
                setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
                Thread.detachNewThread { [self] in
                    var data = Data(), buffer = [UInt8](repeating: 0, count: 8192)
                    while data.range(of: Data("\r\n\r\n".utf8)) == nil {
                        let count = read(client, &buffer, buffer.count)
                        guard count > 0 else { close(client); return }
                        data.append(contentsOf: buffer[0..<count])
                    }
                    let words = String(decoding: data, as: UTF8.self).split(separator: " ")
                    let path = String(words[1].split(separator: "?")[0])
                    lock.withLock { log.append("\(words[0]) \(path)") }
                    _ = self.answer(String(words[0]), path).withCString { write(client, $0, strlen($0)) }
                    close(client)
                }
            }
        }
    }
}

/// Every order the gateway could hand the result on in, against a fake openconnect.
func gatewaySelfTest(_ check: @escaping (Bool, String) -> Void, callbackQuery: String) {
    let idp = URL(string: "https://shb.ais.ucla.edu/shibboleth-idp/profile/SAML2/Redirect/SSO?secret=query")!
    let acs = "/+CSCOE+/saml/sp/acs", closing = "/+CSCOE+/saml_ac_login.html"
    struct Run {
        let controller: WebController, gateway: MockGateway, code: () -> Int32?, trace: () -> [[String: String]], shown: () -> Bool
    }
    /// The IdP's page posts the assertion, as UCLA's does; from there the gateway decides.
    func run(_ listener: FakeListener, patience: TimeInterval = 8, limit: TimeInterval = 20,
             _ answer: @escaping (String, String, String) -> String) -> Run {
        let callback = "http://localhost:\(listener.port)\(callbackQuery)"
        let gateway = MockGateway { answer($0, $1, callback) }
        var policy = Policy(callbackPort: listener.port)
        policy.mockGateway = gateway.port
        let controller = WebController(start: idp, policy: policy, store: .nonPersistent(), interactive: false)
        (controller.handoffPatience, controller.handoffLimit) = (patience, limit)
        var code: Int32?, trace: [[String: String]] = [], shown = false, posted = false
        controller.finish = { code = $0 }
        controller.emitTrace = { line in
            trace.append(line)
            // From the line after the POST, nothing of the VPN server's is ever visible.
            defer { if line["verdict"] == "handoff" { posted = true } }
            if posted, controller.web.alphaValue > 0 || controller.model.phase.showsPage { shown = true }
        }
        controller.web.loadHTMLString("<form id=f method=post action='http://127.0.0.1:\(gateway.port)\(acs)?tgname=secret'>"
            + "<input name=SAMLResponse value=secret-assertion></form><script>setTimeout(()=>f.submit(),400)</script>", baseURL: idp)
        return Run(controller: controller, gateway: gateway, code: { code }, trace: { trace }, shown: { shown })
    }
    func settle(_ one: Run, _ seconds: TimeInterval = 10) {
        spin(seconds) {
            if one.code() != nil { return true }
            if case .failed = one.controller.model.phase { return true }
            return false
        }
    }
    let hop = "<html><head></head><body>secret page text"
    let forms: [(String, (String) -> String)] = [
        ("location.href", { "\(hop)<script>location.href='\($0)'</script>" }),
        ("location.replace", { "\(hop)<script>location.replace('\($0)')</script>" }),
        ("a script after a second", { "\(hop)<script>setTimeout(()=>location.assign('\($0)'),900)</script>" }),
        ("meta refresh", { "<html><head><meta http-equiv=refresh content=\"0;url=\($0)\"></head><body>secret page text" }),
        ("GET form", { callback in
            let parts = callback.components(separatedBy: "?return=")
            return "\(hop)<form id=g method=get action='\(parts[0])'><input name=return value='\(parts[1].removingPercentEncoding!)'></form><script>g.submit()</script>"
        }),
        ("iframe", { "\(hop)<iframe src='\($0)'></iframe>" }),
    ]
    // The order the first live run points to: ACS, then the closing page, whose script hands over.
    for (name, page) in forms {
        let listener = FakeListener()
        let one = run(listener) { method, path, callback in
            path == acs ? MockGateway.redirect(closing) : path == closing ? MockGateway.page(page(callback)) : MockGateway.page("")
        }
        settle(one)
        check(one.code() == 0 && listener.seen.count == 1 && listener.seen[0].hasPrefix("GET /api/sso/") && listener.seen[0].contains("?return=https"),
              "closing page, \(name): delivered once")
        check(one.controller.events.prefix(2) == ["hide", "deliver"] && !one.shown(), "closing page, \(name): hidden at the callback, page never shown")
        let seen = one.gateway.seen
        spin(0.4)
        check(one.gateway.seen == seen && listener.seen.count == 1, "closing page, \(name): nothing loads afterwards")
    }
    // The old order, and the shortest.
    for (name, reply) in [("ACS page's script", { MockGateway.page("\(hop)<script>location.href='\($0)'</script>") }),
                          ("ACS redirect", { MockGateway.redirect($0) })] as [(String, (String) -> String)] {
        let listener = FakeListener()
        let one = run(listener) { _, path, callback in path == acs ? reply(callback) : MockGateway.page("late") }
        settle(one)
        check(one.code() == 0 && listener.seen.count == 1 && !one.shown() && one.gateway.seen == ["POST \(acs)"], "\(name): delivered, no closing page")
        check(one.controller.events == ["hide", "deliver", "delivered"], "\(name): hidden first")
    }
    // Three pages of the gateway's own before the result.
    let long = FakeListener()
    let chain = run(long) { _, path, callback in
        switch path {
        case acs: return MockGateway.redirect("/+webvpn+/index.html")
        case "/+webvpn+/index.html": return MockGateway.page("\(hop)<script>setTimeout(()=>location.href='\(closing)',700)</script>")
        default: return MockGateway.page("\(hop)<script>location.href='\(callback)'</script>")
        }
    }
    settle(chain)
    check(chain.code() == 0 && !chain.shown() && long.seen.count == 1, "a longer chain is run unseen")

    // Each way to lose it says its own thing, and never shows the gateway's page.
    let quiet = FakeListener()
    let closedRun = run(quiet, patience: 0.5) { _, path, _ in path == acs ? MockGateway.redirect(closing) : MockGateway.page("Please close this browser window.") }
    settle(closedRun)
    let silentRun = run(quiet, limit: 1) { _, _, _ in MockGateway.page("secret page text") }
    settle(silentRun)
    let no = FakeListener(reply: "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    let refusedRun = run(no) { _, path, callback in path == acs ? MockGateway.redirect(callback) : MockGateway.page("late") }
    settle(refusedRun)
    for (one, lost) in [(closedRun, WebController.Lost.closed), (silentRun, .silent), (refusedRun, .refused(404))] {
        check(one.controller.model.phase == lost.phase && one.code() == nil && !one.controller.hidden && !one.shown()
              && one.controller.web.alphaValue == 0, "lost (\(lost.name)) shows its own error, not the page")
        let lines = one.trace().filter { $0["kind"] == "lost" }
        check(lines.count == 1 && lines[0]["lost"] == lost.name && lines[0]["status"] == (lost.name == "refused" ? "404" : nil),
              "lost (\(lost.name)) is traced once")
    }
    check(Set([WebController.Lost.closed, .silent, .refused(404)].map { "\($0.phase)" }).count == 3, "three different wordings")
    refusedRun.controller.web.evaluateJavaScript("location.href='http://127.0.0.1:\(refusedRun.gateway.port)\(closing)'")
    spin(0.6)
    check(!refusedRun.gateway.seen.contains("GET \(closing)"), "nothing loads after a refused result either")
    // A result that comes after the error still counts.
    closedRun.controller.web.evaluateJavaScript("location.href='http://localhost:\(quiet.port)\(callbackQuery)'")
    settle(closedRun)
    spin(4) { closedRun.code() != nil }
    check(closedRun.code() == 0 && quiet.seen.count == 1, "a late result is still delivered")

    // Sent back to the IdP: there is a page to see again.
    let again = run(FakeListener()) { _, _, _ in MockGateway.page("\(hop)<script>location.href='about:blank'</script>") }
    spin(6) { again.controller.model.phase == .connecting }
    again.controller.web.loadHTMLString("log in again", baseURL: idp)
    spin(6) { again.controller.model.phase == .login }
    check(again.controller.model.phase == .login && again.controller.web.alphaValue == 1, "back at the IdP, its page shows again")

    // The trace: the chain in order, and nothing private.
    let traced = chain.trace() + closedRun.trace() + refusedRun.trace()
    let text = "\(traced)"
    for secret in ["secret", "AAEAAQ", "return", "tgname", "?", "#", "saml2response", "\(hop)"] {
        check(!text.lowercased().contains(secret.lowercased()), "trace carries no \(secret.prefix(12))")
    }
    let order = chain.trace().filter { $0["kind"] == "nav" }.map { "\($0["verdict"]!) \($0["method"]!) \($0["frame"]!) \($0["path"]!)" }
    check(order == ["allow GET main /shibboleth-idp/profile/SAML2/Redirect/SSO", "handoff POST main \(acs)", "allow GET main /+webvpn+/index.html",
                    "finished GET main \(closing)", "callback GET main /api/sso"], "trace is the chain in order: \(order)")
    check(chain.trace().contains { $0["kind"] == "response" && $0["status"] == "200" && $0["path"] == closing }
          && chain.trace().contains { $0["kind"] == "redirect" && $0["by"] == "server" }
          && chain.trace().allSatisfy { Int($0["ms"] ?? "") != nil }
          && chain.trace().contains { $0["kind"] == "answer" && $0["status"] == "302" }, "trace has statuses, redirects and times")
    for (raw, origin, path) in [("https://evil.example.com/a?b=c#d", "https://other", "other"), ("https://api-0a1b2c3d.duosecurity.com/frame/v4/secret", "https://duo", "other"),
                                ("http://localhost:29786/api/sso/TOKEN?return=x", "http://localhost:29786", "/api/sso"), ("javascript:alert(1)", "other://none", "other"),
                                ("https://v2.ssl.vpn.ucla.edu/+CSCOE+/unknown-token", "https://v2.ssl.vpn.ucla.edu", "other"),
                                ("https://user:pw@shb.ais.ucla.edu/", "https://shb.ais.ucla.edu", "/")] {
        let url = URL(string: raw)!
        check(Redacted.origin(url) == origin && Redacted.path(url) == path, "redacts \(raw): \(Redacted.origin(url)) \(Redacted.path(url))")
    }

    // What no policy decision ever sees. Printed, not judged: it is WebKit's to change.
    for (name, script) in [("fetch", "fetch('CB').catch(()=>{})"), ("XHR", "x=new XMLHttpRequest();x.open('GET','CB');x.send()"),
                           ("img", "new Image().src='CB'"), ("beacon", "navigator.sendBeacon('CB')")] {
        let listener = FakeListener()
        let controller = WebController(start: idp, policy: Policy(callbackPort: listener.port), store: .nonPersistent(), interactive: false)
        var sawCallback = false
        controller.emitTrace = { if $0["verdict"] == "callback" { sawCallback = true } }
        controller.web.loadHTMLString("x<script>\(script.replacingOccurrences(of: "CB", with: "http://localhost:\(listener.port)/api/sso/AAAA"))</script>", baseURL: idp)
        spin(2) { !listener.seen.isEmpty }
        print("note \(name) to localhost from an https page: \(sawCallback ? "seen by the policy" : "not seen by the policy"), \(listener.seen.isEmpty ? "blocked by WebKit" : "reached the listener")")
    }
}

/// Stands in for ASWebAuthenticationSession: nothing reaches a browser.
final class FakeBrowserAuth: BrowserAuth {
    var starts = true, started = false, cancelled = 0
    var ended: (Error?) -> Void = { _ in }
    func start() -> Bool { started = starts; return starts }
    func cancel() { cancelled += 1 }
}

func browserSelfTest(_ check: @escaping (Bool, String) -> Void) {
    let zen: [String: Any] = ["ASWebAuthenticationSessionWebBrowserSupportCapabilities":
        ["IsSupported": true, "CallbackURLMatchingIsSupported": true, "EphemeralBrowserSessionIsSupported": true]]
    check(DefaultBrowser.supports("app.zen-browser.zen", zen), "Zen's Info.plist says it takes the session")
    check(DefaultBrowser.supports("x", ["ASWebAuthenticationSessionWebBrowserSupportCapabilities": ["IsSupported": NSNumber(value: 1)]]), "as a number too")
    check(DefaultBrowser.supports("com.apple.Safari", [:]), "Safari always does")
    for info in [[:], ["ASWebAuthenticationSessionWebBrowserSupportCapabilities": ["IsSupported": false]],
                 ["ASWebAuthenticationSessionWebBrowserSupportCapabilities": "yes"]] as [[String: Any]] {
        check(!DefaultBrowser.supports("com.google.Chrome", info), "a browser that doesn't say so is not used: \(info)")
    }
    check(DefaultBrowser(bundleID: "app.zen-browser.zen", name: "Zen", supported: true).traceName == "zen"
          && DefaultBrowser(bundleID: "com.example.secret", name: "x", supported: true).traceName == "other", "trace names are a closed set")
    let scheme = SystemBrowserAuth.callbackScheme
    check(scheme.range(of: "^[a-z][a-z0-9+.-]*$", options: .regularExpression) != nil && !["http", "https"].contains(scheme)
          && URL(string: "\(scheme)://x") != nil, "the callback scheme is valid and never occurs on the web")
    let url = URL(string: "https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login?tgname=x")!
    let anchor = NSWindow()
    let real = SystemBrowserAuth(url: url, anchor: anchor, ended: { _ in })  // Made, never started.
    check(!real.ephemeral, "not ephemeral: Firefox would use a private window, without 1Password")
    withExtendedLifetime(anchor) {}

    func make(_ configure: (FakeBrowserAuth) -> Void = { _ in }) -> (BrowserController, FakeBrowserAuth, () -> Int32?, () -> [String], () -> [String]) {
        let auth = FakeBrowserAuth()
        configure(auth)
        let controller = BrowserController(browser: DefaultBrowser(bundleID: "app.zen-browser.zen", name: "Zen", supported: true),
                                           reason: "", interactive: false) { anchor, ended in
            auth.ended = ended
            return auth
        }
        controller.cancelGrace = 0.3
        var code: Int32?, fell: [String] = [], states: [String] = []
        controller.finish = { code = $0 }
        controller.fallBack = { fell.append($0) }
        controller.emitTrace = { states.append($0["zen"] ?? "") }
        return (controller, auth, { code }, { fell }, { states })
    }
    let canceled = NSError(domain: ASWebAuthenticationSessionErrorDomain, code: ASWebAuthenticationSessionError.canceledLogin.rawValue)

    let (done, doneAuth, doneCode, _, doneStates) = make()
    check(done.window.title == windowTitle && done.window.level == .normal && !done.window.isVisible
          && done.window.collectionBehavior.contains(.moveToActiveSpace), "card attributes, not on screen, not over the browser")
    done.begin()
    check(doneAuth.started && doneStates() == ["started"], "starts the session")
    done.done()
    check(doneAuth.cancelled == 1 && doneCode() == nil, "done cancels the session, then waits for the answer")
    doneAuth.ended(canceled)
    check(doneCode() == 0 && doneStates() == ["started", "done"], "done closes with success once the browser has heard")
    let (slow, slowAuth, slowCode, _, _) = make()
    slow.begin(); slow.done()
    spin(1) { slowCode() != nil }
    check(slowCode() == 0 && slowAuth.cancelled == 1, "and does not wait forever for it")

    for (name, act) in [("Cancel", { $0.model.cancel() }), ("Esc", { $0.window.cancelOperation(nil) }),
                        ("close", { _ = $0.windowShouldClose($0.window) })] as [(String, (BrowserController) -> Void)] {
        let (one, auth, code, fell, _) = make()
        one.begin(); act(one); auth.ended(canceled)
        check(code() == 1 && auth.cancelled == 1 && fell().isEmpty, "\(name) cancels the browser too")
    }
    // Ended without us, at any moment: the card stays and asks.
    for wait in [0.0, 0.5] {
        let (closed, closedAuth, closedCode, closedFell, closedStates) = make()
        closed.begin(); spin(wait)
        closedAuth.ended(canceled)
        check(closedCode() == nil && closedFell().isEmpty && closed.model.closed && !closed.over && closedStates().last == "closed-by-person",
              "a browser window closed after \(wait) s leaves the card asking, no exit, no fallback")
        closed.model.again()
        check(closedAuth.started && closedStates().suffix(2) == ["retry", "started"] && !closed.model.closed, "Try Zen again starts a new session")
        closedAuth.ended(canceled)
        closed.model.here()
        check(closedFell() == ["chosen-here"] && closedCode() == nil, "or our own window")
    }
    let (closedCancel, closedCancelAuth, closedCancelCode, _, _) = make()
    closedCancel.begin(); closedCancelAuth.ended(canceled)
    closedCancel.window.cancelOperation(nil)
    check(closedCancelCode() == 1 && closedCancelAuth.cancelled == 0, "or Esc/Cancel ends it at once, nothing left to cancel")
    let (twice, twiceAuth, _, _, twiceStates) = make()
    twice.begin(); twiceAuth.ended(canceled)
    twice.model.again(); twice.model.again()
    check(twiceStates().filter { $0 == "started" }.count == 2, "a double click starts one new session")
    let (err, errAuth, errCode, errFell, errStates) = make()
    err.begin(); spin(0.5)
    errAuth.ended(NSError(domain: ASWebAuthenticationSessionErrorDomain, code: ASWebAuthenticationSessionError.presentationContextInvalid.rawValue))
    check(errCode() == nil && errFell() == ["error"] && errStates().contains("error"), "any other error goes to our window")
    let (refuse, _, refuseCode, refuseFell, _) = make { $0.starts = false }
    refuse.begin()
    check(refuseCode() == nil && refuseFell() == ["unstartable"], "a session that won't start goes to our window")
    let (here, hereAuth, hereCode, hereFell, _) = make()
    here.begin(); here.model.here()
    hereAuth.ended(canceled)
    check(hereAuth.cancelled == 1 && hereFell() == ["chosen-here"] && hereCode() == nil, "Sign in here instead drops the browser's session")
    let (eof, eofAuth, eofCode, _, _) = make()
    eof.begin(); eof.end(4); eofAuth.ended(canceled)
    check(eofCode() == 4 && eofAuth.cancelled == 1, "EOF closes the browser's window too")

    // The app: browser first, our window only on a fallback, and "done" goes to the browser.
    let (shown, shownAuth, _, _, _) = make()
    var webs = 0
    let delegate = WebAppDelegate(browser: shown) {
        webs += 1
        return WebController(start: url, store: .nonPersistent(), interactive: false)
    }
    delegate.show()
    check(shownAuth.started && webs == 0, "shows the card and starts the browser, no web window")
    delegate.handle("done")
    check(shownAuth.cancelled == 1, "done reaches the browser")
    let (fallback, fallbackAuth, _, _, _) = make { $0.starts = false }
    let fallbackDelegate = WebAppDelegate(browser: fallback) {
        webs += 1
        return WebController(start: url, store: .nonPersistent(), interactive: false)
    }
    fallbackDelegate.show()
    check(!fallbackAuth.started && webs == 1 && fallbackDelegate.controller != nil, "a fallback opens our own web window")
}

func webSelfTest(_ check: @escaping (Bool, String) -> Void) {
    let policy = Policy()
    func verdict(_ raw: String, method: String = "GET", mainFrame: Bool = true) -> Policy.Verdict {
        policy.verdict(URL(string: raw), method: method, mainFrame: mainFrame)
    }
    for raw in ["https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/login?x=1", "https://shb.ais.ucla.edu/idp", "https://ucla.edu/",
                "https://api-0a1b2c3d.duosecurity.com/frame/v4", "about:blank"] {
        check(verdict(raw) == .allow, "allow \(raw)")
    }
    for raw in ["http://shb.ais.ucla.edu/", "https://ucla.edu.evil.com/", "https://evilucla.edu/", "https://shb.ais.ucla.edu:8443/",
                "https://user@shb.ais.ucla.edu/", "https://duosecurity.com/", "https://api-x.duosecurity.com.evil.com/",
                "https://admin-x.duosecurity.com/", "https://api-x.evil.duosecurity.com/", "file:///etc/hosts", "javascript:alert(1)",
                "data:text/html,x", "duomobile://x", "about:config", "http://127.0.0.1:29786/api/sso/AAAA",
                "http://localhost:29787/api/sso/AAAA", "http://localhost/api/sso/AAAA", "http://localhost:29786/",
                "http://localhost:29786/api/sso/", "http://localhost:29786/other", "https://localhost:29786/api/sso/AAAA",
                "http://u@localhost:29786/api/sso/AAAA", "http://localhost.evil.com:29786/api/sso/AAAA"] {
        if case .block = verdict(raw) { check(true, "block \(raw)") } else { check(false, "block \(raw)") }
    }
    check(verdict("http://localhost:29786/api/sso/AAAA?return=https%3A%2F%2Fv2.ssl.vpn.ucla.edu%2Fx") == .callback, "callback")
    if case .block = verdict("http://localhost:29786/api/sso/AAAA", method: "POST") { check(true, "callback is GET only") } else { check(false, "callback is GET only") }
    check(verdict("https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml_ac_login.html") == .finished, "Cisco page")
    check(verdict("https://shb.ais.ucla.edu/+CSCOE+/saml_ac_login.html") == .allow, "Cisco page only on the VPN host")
    check(verdict("https://v2.ssl.vpn.ucla.edu/+CSCOE+/saml/sp/acs?tgname=x", method: "POST") == .handoff, "hand-off")
    let base = URL(string: "https://shb.ais.ucla.edu/idp")!
    func make(_ listener: FakeListener) -> (WebController, () -> Int32?) {
        let controller = WebController(start: base, policy: Policy(callbackPort: listener.port),
                                          store: .nonPersistent(), interactive: false)
        var code: Int32?
        controller.finish = { code = $0 }
        return (controller, { code })
    }
    func page(_ script: String) -> String { "<html><body>mock<a id=a target=_blank href='https://shb.ais.ucla.edu/x'>x</a><script>\(script)</script></body></html>" }

    // The real hand-off, end to end against a fake openconnect.
    let listener = FakeListener()
    // Not a secret: a truncated public-key header standing in for openconnect's SSO token.
    let token = "AAEAAQBbMFkwEwYHKoZIzj0CAQYI%2BA%3D%3D" // gitleaks:allow
    let query = "?return=https%3A%2F%2Fv2.ssl.vpn.ucla.edu%2F%2BCSCOE%2B%2Fsaml_ac_login.html"
    let callback = "http://localhost:\(listener.port)/api/sso/\(token)\(query)"
    let (login, code) = make(listener)
    check(login.window.title == windowTitle && login.window.level == .floating
          && login.window.collectionBehavior.contains(.moveToActiveSpace)
          && !login.window.styleMask.contains(.resizable) && !login.window.isVisible, "window attributes, not on screen")
    login.web.loadHTMLString(page(""), baseURL: base)
    spin(8) { login.model.phase == .login }
    check(login.model.phase == .login && login.model.host == "shb.ais.ucla.edu" && login.web.alphaValue == 1, "login page is shown with its host")
    login.web.loadHTMLString(page("location.href='\(callback)'"), baseURL: base)
    spin(8) { code() != nil }
    check(code() == 0, "delivered, exit 0")
    check(login.events == ["hide", "deliver", "delivered"], "hidden before the GET was sent: \(login.events)")
    check(listener.seen == ["GET /api/sso/\(token)\(query) HTTP/1.1"], "openconnect got one exact GET")
    check(login.web.url?.host != "v2.ssl.vpn.ucla.edu" && !login.window.isVisible, "Cisco page never loaded")
    spin(0.5)
    check(listener.seen.count == 1, "no second request")

    // Nobody listening: the window comes back instead of vanishing.
    let gone = FakeListener(answer: false)
    let (late, lateCode) = make(gone)
    late.web.loadHTMLString(page("location.href='http://localhost:\(gone.port)/api/sso/AAAA'"), baseURL: base)
    spin(12) { late.events.contains("reappear") }
    check(late.model.phase == .ended && lateCode() == nil && !late.hidden, "refused hand-off reappears as cancelled")

    gatewaySelfTest(check, callbackQuery: "/api/sso/\(token)\(query)")

    // Anything else opens nowhere and says so.
    let (blocked, blockedCode) = make(listener)
    blocked.web.loadHTMLString(page(""), baseURL: base)
    spin(8) { blocked.model.phase == .login }
    blocked.web.evaluateJavaScript("location.href='https://example.com/'")
    spin(8) { blocked.model.notice != nil }
    check(blocked.model.notice?.contains("example.com") == true && blocked.model.phase == .login
          && blocked.web.url?.host == base.host && blockedCode() == nil && !blocked.hidden, "foreign page blocked with a notice")
    let (first, firstCode) = make(listener)
    first.web.loadHTMLString(page("location.href='https://example.com/'"), baseURL: base)
    spin(8) { if case .failed = first.model.phase { return true } else { return false } }
    if case .failed(_, let detail) = first.model.phase {
        check(detail.contains("example.com") && first.web.alphaValue == 0 && firstCode() == nil && !first.hidden, "a block before any page shows the error with Retry")
    } else { check(false, "a block before any page shows the error with Retry") }
    let (stuck, stuckCode) = make(listener)
    stuck.handoffLimit = 0.4
    stuck.web.loadHTMLString(page(""), baseURL: base)
    spin(8) { stuck.model.phase == .login }
    stuck.beginHandoff()
    spin(4) { stuck.model.phase != .connecting }
    check(stuck.model.phase == WebController.Lost.silent.phase && stuckCode() == nil, "a hand-off that goes nowhere ends in the error")
    // Bytes sent, no answer: neither "gone" nor a fresh start, and nothing on screen.
    let mute = FakeListener(reply: "")
    let (unsure, unsureCode) = make(mute)
    var asked = 0
    unsure.unknown = { asked += 1 }
    unsure.web.loadHTMLString(page("location.href='http://localhost:\(mute.port)/api/sso/AAAA'"), baseURL: base)
    spin(12) { asked > 0 }
    spin(0.5)
    check(asked == 1 && unsure.hidden && unsureCode() == nil && !unsure.events.contains("reappear") && mute.seen.count == 1, "an unanswered GET is reported as unknown")
    let pop = make(listener).0
    pop.web.loadHTMLString(page(""), baseURL: base)
    spin(8) { pop.model.phase == .login }
    pop.web.evaluateJavaScript("document.getElementById('a').click()")  // Counts as a user's click.
    spin(8) { pop.model.notice != nil }
    check(pop.model.notice != nil && pop.web.url?.host == base.host, "pop-up blocked with a notice")

    // Esc, Cmd-W and Cancel all cancel at once.
    let escape = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil,
                                  characters: "\u{1b}", charactersIgnoringModifiers: "\u{1b}", isARepeat: false, keyCode: 53)!
    for (name, act) in [("Esc", { $0.window.keyDown(with: escape) }), ("cancelOperation", { $0.window.cancelOperation(nil) }),
                        ("close", { _ = $0.windowShouldClose($0.window) }),
                        ("Cancel", { $0.model.cancel() })] as [(String, (WebController) -> Void)] {
        let (one, oneCode) = make(listener)
        act(one)
        check(oneCode() == 1 && one.hidden, "\(name) cancels")
    }
    let (typed, typedCode) = make(listener)
    typed.web.loadHTMLString(page(""), baseURL: base)
    spin(8) { typed.model.phase == .login }
    typed.window.makeFirstResponder(typed.web)
    typed.web.keyDown(with: escape)
    spin(4) { typedCode() != nil }
    check(typedCode() == 1, "Esc typed in the page cancels")
    let menu = WebAppDelegate.menu(for: login)
    let keys = menu.items.flatMap { $0.submenu!.items }.map { $0.keyEquivalent + " " + NSStringFromSelector($0.action!) }
    check(["x cut:", "c copy:", "v paste:", "a selectAll:", "w performClose:"].allSatisfy(keys.contains), "editing keys")
}
#endif

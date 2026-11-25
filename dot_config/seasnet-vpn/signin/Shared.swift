// What both windows share: the login URL gate, the look, and the pipe to seasnet-vpn.
import Cocoa
import SwiftUI

let windowTitle = "UCLA VPN Sign-In"

enum Contract {
    static let loginHost = "v2.ssl.vpn.ucla.edu"
    static let loginPath = "/+CSCOE+/saml/sp/login"

    /// Same gate as seasnet-vpn's login_url_ok.
    static func accepts(_ raw: String) -> Bool {
        guard !raw.isEmpty, raw.count < 8192,
              raw.unicodeScalars.allSatisfy({ $0.value > 32 && $0.value < 127 }),
              let url = URLComponents(string: raw), url.scheme?.lowercased() == "https",
              url.host?.lowercased() == loginHost, url.percentEncodedPath == loginPath,
              url.user == nil, url.password == nil, url.port == nil || url.port == 443 else { return false }
        return true
    }
}

extension Color {
    static func pair(_ light: UInt32, _ dark: UInt32) -> Color {
        func rgb(_ v: UInt32) -> NSColor {
            NSColor(srgbRed: CGFloat(v >> 16 & 255) / 255, green: CGFloat(v >> 8 & 255) / 255,
                    blue: CGFloat(v & 255) / 255, alpha: 1)
        }
        return Color(nsColor: NSColor(name: nil) {
            rgb($0.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light)
        })
    }
    static let ucla = pair(0x2774AE, 0x6DB3E8)
    static let uclaFill = pair(0x2774AE, 0x2F7FBD)
    static let cardFill = pair(0xFFFFFF, 0x24272B)
    static let matFill = pair(0xFBFCFD, 0x2C3035)
}

struct Tile: View {
    var size: CGFloat
    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
            .fill(LinearGradient(colors: [Color(red: 0.24, green: 0.56, blue: 0.80),
                                          Color(red: 0.10, green: 0.33, blue: 0.54)],
                                 startPoint: .top, endPoint: .bottom))
            .overlay(RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
                .strokeBorder(.white.opacity(0.22), lineWidth: 0.75))
            .overlay(Image(systemName: "lock.shield.fill")
                .font(.system(size: size * 0.52, weight: .medium)).foregroundStyle(.white))
            .frame(width: size, height: size)
            .shadow(color: .black.opacity(0.18), radius: size * 0.1, y: size * 0.04)
            .accessibilityHidden(true)
    }
}

final class EscapeWindow: NSWindow {
    var onEscape: () -> Void = {}
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
    override func cancelOperation(_ sender: Any?) { onEscape() }
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 { onEscape() } else { super.keyDown(with: event) }
    }
}

/// The display being worked on, found by the pointer.
func workingArea() -> NSRect? {
    let pointer = NSEvent.mouseLocation
    return (NSScreen.screens.first { NSMouseInRect(pointer, $0.frame, false) } ?? NSScreen.main)?.visibleFrame
}

/// In front, on the Space in use, with the keys.
func bringUp(_ window: NSWindow) {
    window.animationBehavior = .alertPanel  // AppKit's own scale-in; follows Reduce Motion.
    window.orderFrontRegardless()
    NSApp.activate(ignoringOtherApps: true)
    window.makeKeyAndOrderFront(nil)
}

protocol HostingSizing: AnyObject { var sizingOptions: NSHostingSizingOptions { get set } }
extension NSHostingView: HostingSizing {}

func parse(_ line: String) -> [String: Any]? {
    (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
}

func writeLine(_ message: [String: String]) {
    guard let data = try? JSONSerialization.data(withJSONObject: message, options: [.sortedKeys]) else { return }
    FileHandle.standardOutput.write(data + Data("\n".utf8))
}

/// NSApplication holds its delegate weakly.
private var appDelegate: NSApplicationDelegate?

func runAccessoryApp(_ delegate: NSApplicationDelegate) -> Never {
    signal(SIGPIPE, SIG_IGN)
    appDelegate = delegate
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    app.delegate = delegate
    app.run()
    exit(70)
}

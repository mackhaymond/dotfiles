// touchid.swift: ~/.local/bin/tmux-touchid, the kill guard's authenticator.
// Docs: ~/.config/tmux/docs/kill-guard.md
//
//   tmux-touchid "REASON"
//
// LocalAuthentication .deviceOwnerAuthentication: Touch ID, with the login
// password as fallback (no Touch ID sensor, clamshell, too many misses).
// macOS words the dialog as "tmux-touchid is trying to REASON.".
//
// Exit: 0 approved, 1 denied / cancelled / timed out, 2 unavailable (no GUI
// session, e.g. over ssh; no passcode set; policy can't be evaluated).
// A hard 60 s timeout invalidates the context and denies, so a prompt nobody
// answers can't hold a kill (or the tmux client waiting on it) forever.
//
// Build: swiftc -O -o tmux-touchid touchid.swift

import Foundation
import LocalAuthentication

let timeoutSeconds: Double = 60

func fail(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write(("tmux-touchid: " + message + "\n").data(using: .utf8)!)
    exit(code)
}

let args = CommandLine.arguments
var reason = args.count > 1 ? args[1] : ""
if reason.trimmingCharacters(in: .whitespaces).isEmpty {
    reason = "approve a destructive tmux action"
}

let context = LAContext()
var policyError: NSError?
guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &policyError) else {
    fail(2, "authentication unavailable: \(policyError?.localizedDescription ?? "unknown")")
}

final class Outcome: @unchecked Sendable {
    var approved = false
    var error: Error?
}
let outcome = Outcome()
let done = DispatchSemaphore(value: 0)

context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, error in
    outcome.approved = success
    outcome.error = error
    done.signal()
}

if done.wait(timeout: .now() + timeoutSeconds) == .timedOut {
    context.invalidate()
    fail(1, "timed out after \(Int(timeoutSeconds))s; denied")
}

if outcome.approved {
    exit(0)
}

if let laError = outcome.error as? LAError {
    switch laError.code {
    case .notInteractive, .passcodeNotSet, .biometryNotAvailable:
        fail(2, "authentication unavailable: \(laError.localizedDescription)")
    default:
        fail(1, "denied: \(laError.localizedDescription)")
    }
}
fail(1, "denied: \(outcome.error?.localizedDescription ?? "unknown")")

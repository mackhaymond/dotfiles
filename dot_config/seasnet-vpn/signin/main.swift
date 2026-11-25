// The UCLA VPN sign-in windows. `seasnet-vpn` compiles this folder into one app on
// first use and runs one window per login attempt.
//
//   (no arguments)      the Touch ID approval card (Approval.swift)
//   --web               UCLA's login pages, when Touch ID can't do it (Web.swift)
//   --create-key FILE   make the approval key (never overwrites), print its public half
//   --show-key FILE     print the public half
//
//   stdin   JSON lines. First the request: {"url","key","command","by","agent"} for
//           the card, {"url","reason"} for --web. The card then takes
//           {"do":"sign","challenge","n"}, {"do":"problem",...}, {"do":"web"} and
//           {"do":"done"}; --web shows itself only on {"do":"show"}. EOF closes either.
//   stdout  JSON lines {"event": ready | approved | signed | unsigned | web} from
//           the card ("n" ties a signing request to its approval; "why" goes with
//           web), and prepared | ready | delivered | unknown | trace from --web
//   exit    0 done, 1 closed by the person, 4 stdin closed, 64 bad input;
//           anything else means it could not run
//
// Test and preview code is compiled only with -D TESTING, which seasnet-vpn
// never passes.
import Foundation

let arguments = CommandLine.arguments
#if TESTING
if arguments.count == 3, arguments[1] == "--self-test" { selfTest(arguments[2]) }
if arguments.count == 3, arguments[1] == "--render-previews" { renderPreviews(to: arguments[2]) }
if arguments.count == 4, arguments[1] == "--message" {
    FileHandle.standardOutput.write(Contract.message(challenge: arguments[2], url: arguments[3]) ?? Data())
    exit(0)
}
#endif
if arguments.count == 3, arguments[1] == "--create-key" { keyCommand { try EnclaveKey.create(arguments[2]) } }
if arguments.count == 3, arguments[1] == "--show-key" { keyCommand { try EnclaveKey.publicKey(arguments[2]) } }
if arguments.count == 2, arguments[1] == "--web" { runWeb() }
if arguments.count > 1 { exit(64) }
runApproval()

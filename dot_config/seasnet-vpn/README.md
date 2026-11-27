# SEASnet VPN lifetime

`ssh seasnet` reaches cs35l.seas.ucla.edu through a userspace UCLA VPN
(openconnect + ocproxy) that forwards only that host's port 22. Other SSH hosts
and system traffic use their normal connections.

## Who can start it

A foreground command on a terminal, typed by you or run by an agent in its pty:
`seasnet-vpn start`, or `ssh seasnet` / `relayctl …` when the VPN is down. That
opens the **UCLA VPN Sign-In** window on the current Space: a Touch ID card, or UCLA's
pages where that can't work (see below). Cancel, Esc, ⌘W or Ctrl-C in the terminal cancels
at once; an unfinished login times out after 5 minutes and the window closes. Login progress and refusals
are written to your terminal even when the caller hides ssh's stderr, which
relayctl does.

Everything else rides an existing tunnel or fails at once with a one-line
reason, and never opens the login window. That covers:

- Mutagen
- background jobs (`ssh seasnet &`)
- BatchMode
- anything with no controlling terminal

An ssh whose `ConnectTimeout` is shorter than a login is refused too, rather
than killed halfway through Duo; run `seasnet-vpn start` first. An agent whose
shell has no terminal (a plain subprocess rather than a pty) falls under "no
controlling terminal" and is refused.

After a failed or cancelled login, SSH stops reopening the login for 1, 2, 4,
… up to 30 minutes. `seasnet-vpn start` always tries right away.

## The sign-in window

**Touch ID card.** It says what asked (`ssh seasnet`, `relayctl …` or
`seasnet-vpn start`, and the agent or space when that is certain) and takes your
fingerprint in the window itself. vlt then does UCLA login and Duo, `seasnet-vpn`
sends openconnect the one request it waits for on `[::1]:29786`, and the card
closes at once.

The fingerprint is enforced by the Secure Enclave: the approval key signs only
after a match, has no password path, and stops working if the enrolled
fingerprints change. vlt accepts a login only with its signature over a one-time
challenge and that exact login URL. Set up once, or again after changing
fingerprints:

- `seasnet-vpn approve-key --create` makes the key and prints its public half;
  enroll that in vlt. It never overwrites a key; `--create --replace` makes a new
  one and keeps the old file aside.
- `seasnet-vpn approve-key --show` prints the public half again.

The key file is `~/.local/share/seasnet-vpn/approve-key.json` (mode 600). It works
on this Mac only, and only with your finger.

A vlt login is sent once. After it fails, or its answer is lost, there is no Retry:
run the command again to start fresh. Cancelling one already sent doesn't stop it
at UCLA, but this Mac won't connect; the terminal says so. If it is unclear whether
the result reached openconnect, `seasnet-vpn` checks whether openconnect moved on,
and otherwise ends the attempt.

**Web sign-in.** When a touch can't do it, the same login continues on UCLA's own
pages, and the terminal says why in one line. That happens by itself when there is
no key or it no longer works, Touch ID is unavailable (lid closed), or vlt can't be
reached, has no valid VPN credentials, doesn't know the key, refuses, needs you to
act (then `vlt vpn reset`), or doesn't recognize UCLA's answer. Otherwise it is the
**Sign in here instead** button: wrong finger, locked or interrupted Touch ID, a
failed login, a rate limit, or a lost answer from vlt.

**In Zen first.** The pages open in your default browser through macOS's sign-in
session (ASWebAuthenticationSession), so 1Password fills there and its passkeys
work. Zen (Firefox 159+) opens them in a small window of its own, with your normal
profile, extensions and cookies, after macOS asks once whether the app may use the
site to sign in. A small card of ours ("Continue in Zen") stands beside it.

- Zen hands the result to openconnect itself. `seasnet-vpn` sees it land when
  openconnect closes its listener and logs its next `POST https://…`, both required.
  It watches openconnect's log with kqueue, so Zen's window closes within moments
  of UCLA's "you can close this" page, before the tunnel is even up.
- Cancel or Esc on the card cancels the login. If Zen's window closes without a
  result (you closed it, said no to macOS's alert, or Zen gave up), the card stays:
  **Try Zen again**, **Sign in here instead**, or Cancel.
- **Sign in here instead** opens our own window below. So does, by itself, a
  session that can't start or ends in an error, or a default browser that doesn't
  take these sessions; `events.log` says which.
- The session is not private ("ephemeral"): Firefox would open a private window,
  where 1Password doesn't run unless allowed there.

**Our own window**, when the browser can't do it:

- Password and Duo push work there. Passkeys, security keys and Touch ID don't:
  Apple allows WebAuthn in an app's web view only for that app's own domains. ⌘\ may fill from 1Password.
- Once UCLA's VPN server has the sign-in, its own pages run behind the Connecting
  panel and are never shown. The window hides in the same callback that gives it
  the login result, and nothing loads after that.
- `~/.local/state/seasnet-vpn/signin-trace.log` keeps the last three attempts' steps:
  verdict, method, host, a known path or "other", status and time, and for the
  browser route its states and when the result landed. No queries, tokens or page
  text.
- Only HTTPS on `ucla.edu` and Duo's `api-*.duosecurity.com` opens, plus the one
  `localhost:29786` request. Anything else, pop-ups included, is blocked with a
  message.
- It remembers nothing: no cookies, no Duo "remember this device". A remembered
  session could let a later login finish with nobody there. An earlier build's
  store is never read.
- It is a separate process with neither the approval key nor vlt, and the card
  never creates a web view.

The login opens in a plain Zen tab only if our app can't be built or run.

`signin/*.swift` is compiled on first use (a few seconds, again after any change)
into `~/.local/share/seasnet-vpn/UCLA VPN Sign-In.app`, and only the very file that
was built is run. `approve-key --create` always builds afresh, in a private folder.
Window title and app name: `UCLA VPN Sign-In`; bundle id: `co.mackhaymond.seasnet-vpn-signin`. No URLs,
tokens or signatures are logged or put on a command line of ours; only
openconnect's own call to `seasnet-vpn` and the Zen fallback carry the login URL.

To check it without opening a window, build it with
`xcrun swiftc -D TESTING signin/*.swift -o /tmp/signin-testing` and run that with
`--self-test DIR` or `--render-previews DIR`. The app `seasnet-vpn` builds has
neither, nor the software key they use.

## When it stops

The VPN disconnects after `idle_minutes` (in `config.toml`, default 30) with
both of these true:

- no SSH session open over it except Mutagen's, and
- no change to any file a running seasnet Mutagen session syncs. Changes count
  in both directions, because Mutagen writes remote edits into the local tree.
  Ignored paths such as `.git/` do not count.

Mutagen's own connection and keepalives never count as use. Before
disconnecting, the helper pauses the Mutagen sessions whose remote is a
`seasnet-vpn proxy` host. A disconnect from sleep, a network change or UCLA
also leads to a pause: Mutagen's next reconnect is refused and its sessions are
paused, so it does not retry every 20 s.

The next successful start resumes exactly the sessions the helper paused
(listed in `~/.local/state/seasnet-vpn/paused-sync.json`). If the tunnel is
already up but sync was left paused, your next `ssh seasnet` or `relayctl`
resumes it. A session that fails to resume stays on the list for the next try.
Sessions you paused yourself stay paused.

An open interactive `ssh seasnet` holds the tunnel for as long as it stays open,
even if you forget it. UCLA may still end the session on its side.

## Commands

- `seasnet-vpn status`: connection, idle minutes, paused sessions, login back-off, recent events.
- `seasnet-vpn stop`: pause seasnet sync, then disconnect.
- `seasnet-vpn start`: log in, then resume the paused sync.

The event log is `~/.local/state/seasnet-vpn/events.log`. The openconnect log
of the last login is `openconnect.log`.

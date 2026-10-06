# SEASnet VPN lifetime

`ssh seasnet` reaches cs35l.seas.ucla.edu through a userspace UCLA VPN
(openconnect + ocproxy) that forwards only that host's port 22. Other SSH hosts
and system traffic use their normal connections.

## Who can start it

Only you, at a terminal: `seasnet-vpn start`, or an interactive `ssh seasnet` /
`relayctl …` when the VPN is down. That opens **SEASnet VPN Login** in the
background for UCLA sign-in and Duo.

Everything else rides an existing tunnel or fails at once with a one-line
reason, and never opens the login window. That covers Mutagen, agents (Claude
ptys in the `agents` tmux session), `ssh -o BatchMode=yes`, and anything with no
controlling terminal. An agent may run `seasnet-vpn start` only with
`SEASNET_VPN_ALLOW_AGENT_LOGIN=1`, meaning you said yes.

After a failed or cancelled login, SSH stops reopening the login for 1, 2, 4,
… up to 30 minutes. `seasnet-vpn start` always tries right away.

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
(listed in `~/.local/state/seasnet-vpn/paused-sync.json`). Sessions you paused
yourself stay paused.

## Commands

- `seasnet-vpn status`: connection, idle minutes, paused sessions, login back-off, recent events.
- `seasnet-vpn stop`: pause seasnet sync, then disconnect.
- `seasnet-vpn start`: log in, then resume the paused sync.

The event log is `~/.local/state/seasnet-vpn/events.log`. The openconnect log
of the last login is `openconnect.log`.

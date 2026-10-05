# SEASnet VPN lifetime

`ssh seasnet` starts the restricted VPN when needed and opens **SEASnet VPN
Login** in the background for UCLA sign-in and Duo. Other SSH hosts and system
traffic use their normal connections.

The VPN stays connected while any SSH transport is open. Mutagen's background
watcher counts as an active transport even when no files are changing, so
continuous sync can keep the VPN connected indefinitely. There is no forced
session expiration in this helper. UCLA may expire the authenticated session.

Automatic disconnect happens after 15 minutes with every SSH transport closed.
To let that timer start, pause the Mutagen sessions using `seasnet` and close
other SSH connections. An SSH multiplexing master may remain open briefly after
the last command exits.

Inspect sync sessions with `mutagen sync list`, pause one with
`mutagen sync pause <session>`, and resume it with `mutagen sync resume <session>`.
Resuming reconnects through the same VPN helper when needed. Stopping only the
VPN while Mutagen is still running can make Mutagen immediately reconnect and
request login again.

Run `seasnet-vpn status` to see whether open transports are holding the timer.

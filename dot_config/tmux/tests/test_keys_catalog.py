"""keys.tsv, the key help's catalog (prefix ?, `agent-ui keys`), against the
config it describes.

Shape: every row has a key and a short description, a tmux table exactly when
it names tmux keys, no key listed twice, prefix ? itself listed.

Live: a PRIVATE tmux server (`-L keys-<uuid>`, `-f /dev/null`, its own
TMUX_TMPDIR, never the user's) loads the REAL tmux.conf (the chezmoi template
rendered with a throwaway HOME), which sources spaces.conf (@spaces on, as in
the file), agent-birth.conf and agent-pause.conf from the same directory.
Under that HOME there is nothing: no tpm, so no plugin (no resurrect, no
continuum), and every script the config would run (`#{HOME}/.config/tmux/
scripts/...`, run-shell or hook) is missing and fails harmlessly; notifyutil is
a no-op stub at the front of the server's PATH. `~/.local/bin/agent-ui` is a
stub too (the config only `test -x`es it), so the agent-ui bindings are the
ones loaded, as on Mack's machine; one test loads it without the stub. Then
every catalog row that names a table and keys must find each key bound in that
table BY THE CONFIG: a binding identical to a bare server's (tmux's defaults,
which `-f /dev/null` keeps) fails, unless the row writes the key `=key` (a
stock binding listed on purpose), which must then still be the stock one.
And the other way round: every binding the config makes, in ANY key table
(one that differs from the bare server's), must be a catalog row's key or in
NOT_IN_HELP below, so a key added or moved in the config without a keys.tsv
row fails instead of silently missing from the help.
Teardown kill-servers exactly that socket (and the server's pid, should
kill-guard's alias be in the way) and removes the temp dir.
Run with unittest or pytest.
"""
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import unittest
import unicodedata
import uuid

HERE = Path(__file__).resolve().parents[1]
CATALOG = HERE / "keys.tsv"
CONF = HERE / "tmux.conf.tmpl"
SOURCED = ("spaces.conf", "agent-birth.conf", "agent-pause.conf")
TMUX = shutil.which("tmux")
TABLES = {"root", "prefix", "jump", "stash"}

# Bindings the config makes that the key help deliberately does NOT list, as
# (table, tmux keys, reason); keys take the catalog's ranges. Keep it small:
# a key Mack presses gets a keys.tsv row, not an entry here.
NOT_IN_HELP = (
    # The leaders themselves: every ⌃S row and every ⌥W row starts with them.
    ("root", "C-s", "the prefix leader (prefix is None): the ⌃S of every ⌃S row"),
    ("root", "M-w", "the jump leader: the ⌥W of every ⌥W row"),
    # spaces.conf: WezTerm sends each digit chord as one User key; these two
    # rows only hand the pane the key itself, as if tmux were not there.
    ("root", "User0..User8", "WezTerm ⌃1-9, passed on to the pane as C-1..C-9"),
    ("root", "User9..User17", "WezTerm ⌥1-9, passed on to the pane as M-1..M-9"),
)


def cells(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def rows():
    """(line number, section, key, description, table, [tmux keys], note)."""
    out = []
    for n, line in enumerate(CATALOG.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")] + [""] * 6
        out.append((n, cols[0], cols[1], cols[2], cols[3], cols[4].split(), cols[5]))
    return out


def expand(token):
    """`a..z`, `1..9`, `User18..User26` → the keys; anything else → itself."""
    m = re.fullmatch(r"(\D*)(\d+)\.\.\1(\d+)", token)
    if m:
        return [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
    m = re.fullmatch(r"(.)\.\.(.)", token)
    if m:
        return [chr(c) for c in range(ord(m.group(1)), ord(m.group(2)) + 1)]
    return [token]


def targets(tokens):
    """A row's tmux keys → [(key, stock_ok)]: `=key` is a key whose stock
    tmux binding we list on purpose (prefix z, prefix [)."""
    out = []
    for tok in tokens:
        stock = tok.startswith("=") and len(tok) > 1
        out += [(k, stock) for k in expand(tok[1:] if stock else tok)]
    return out


def catalogued():
    """Every (table, tmux key) some catalog row names, `=key`s included."""
    return {(table, k) for _, _, _, _, table, keys, _ in rows() for k, _ in targets(keys)}


def not_in_help():
    """NOT_IN_HELP expanded → {(table, tmux key)}."""
    return {(table, k) for table, tok, _ in NOT_IN_HELP for k in expand(tok)}


class CatalogShape(unittest.TestCase):
    def test_rows_are_complete_and_short(self):
        rs = rows()
        self.assertGreater(len(rs), 50)
        for n, section, key, desc, table, keys, note in rs:
            where = f"keys.tsv:{n}"
            self.assertTrue(section and key and desc, where)
            self.assertLessEqual(cells(desc), 50, where)
            self.assertLessEqual(cells(key), 16, where)
            # A table exactly when there are keys to look up in it.
            self.assertEqual(bool(table), bool(keys), where)
            if table:
                self.assertIn(table, TABLES, where)

    def test_ranges_expand(self):
        self.assertEqual(expand("a..e"), list("abcde"))
        self.assertEqual(expand("1..3"), ["1", "2", "3"])
        self.assertEqual(expand("User18..User20"), ["User18", "User19", "User20"])
        self.assertEqual(expand("M-`"), ["M-`"])
        self.assertEqual(expand("C-Space"), ["C-Space"])
        self.assertEqual(targets(["=z", "a..b", "="]), [("z", True), ("a", False), ("b", False), ("=", False)])

    def test_no_key_listed_twice(self):
        seen = {}
        for n, _, _, _, table, keys, _ in rows():
            for tok in keys:
                for k, _ in targets([tok]):
                    self.assertNotIn((table, k), seen, f"keys.tsv:{n} and :{seen.get((table, k))}")
                    seen[(table, k)] = n

    def test_sections_are_contiguous(self):
        # A section's rows sit together, so the file reads like the popup.
        order = []
        for _, section, *_ in rows():
            if not order or order[-1] != section:
                self.assertNotIn(section, order, f"{section} appears twice")
                order.append(section)

    def test_allowlist_has_reasons_and_no_catalog_keys(self):
        for table, tok, reason in NOT_IN_HELP:
            self.assertTrue(table and tok and reason.strip(), (table, tok))
        # A key is either in the help or allowlisted out of it, never both.
        self.assertEqual(not_in_help() & catalogued(), set())

    def test_the_help_lists_itself(self):
        self.assertTrue(any(t == "prefix" and k == ["?"] for _, _, _, _, t, k, _ in rows()))

    def test_the_binary_embeds_this_file(self):
        src = (HERE / "agent-ui" / "src" / "keys" / "catalog.rs").read_text()
        self.assertIn('include_str!("../../../keys.tsv")', src)


@unittest.skipIf(TMUX is None, "no tmux")
class Live(unittest.TestCase):
    """The real config on a private server: is every catalog key bound?"""

    agent_ui = True

    def setUp(self):
        self.sock = "keys-" + uuid.uuid4().hex[:12]
        self.tmp = tempfile.mkdtemp(prefix="keys-catalog-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = Path(self.tmp) / "home"
        bin_ = home / ".local" / "bin"
        bin_.mkdir(parents=True)
        # tmux.conf's PATH puts $HOME/.local/bin ahead of /usr/bin: the hooks'
        # notifyutil posts land here, never on the real sidebar.
        stub = bin_ / "notifyutil"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        if self.agent_ui:
            ui = bin_ / "agent-ui"
            ui.write_text("#!/bin/sh\nexit 0\n")
            ui.chmod(0o755)
        conf_dir = Path(self.tmp) / "conf"
        conf_dir.mkdir()
        text = (CONF.read_text().replace("{{ .chezmoi.homeDir }}", str(home))
                .replace("{{ .homebrew_prefix }}", "/opt/homebrew"))
        self.assertNotIn("{{", text)
        (conf_dir / "tmux.conf").write_text(text)
        for name in SOURCED:
            shutil.copy(HERE / name, conf_dir / name)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        # The socket's own short dir: macOS's $TMPDIR is too long for a
        # socket path once tmux adds tmux-UID/NAME.
        sockdir = tempfile.mkdtemp(prefix="kc-", dir="/tmp")
        self.addCleanup(shutil.rmtree, sockdir, True)
        self.env.update(TMUX_TMPDIR=sockdir, TERM="xterm-256color", HOME=str(home),
                        PATH=f"{bin_}:{self.env.get('PATH', '/usr/bin:/bin')}", AGENT_STATE_NOTICE="")
        self.pid = None
        self.addCleanup(self.kill)
        self.t("new-session", "-d", "-s", "main", "-x", "120", "-y", "40")
        self.pid = int(self.t("display", "-p", "#{pid}"))
        # Parse/command errors in the config would show here; run-shell's
        # failures (missing scripts, by design) are asynchronous and do not.
        r = self.t("source-file", str(conf_dir / "tmux.conf"), check=False)
        self.load_errors = r

    def kill(self):
        subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "kill-server"], env=self.env,
                       capture_output=True, timeout=10)
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def t(self, *args, check=True):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=20)
        if check:
            if r.returncode:
                raise AssertionError(f"tmux {args}: {r.stderr}")
            return r.stdout.strip()
        return r

    @staticmethod
    def parse_keys(listing):
        """`list-keys` output → {(table, key): line, whitespace squeezed}.
        (tmux 3.7's `list-keys -T table KEY` prints nothing, so whole tables
        are listed and parsed; list-keys escapes some key names: `\\"`,
        `\\%`, ...)"""
        out = {}
        for line in listing.splitlines():
            m = re.match(r"bind-key\s+(?:-r\s+)?-T\s+(\S+)\s+(\S+)", line)
            if not m:
                continue
            name = m.group(2)
            if len(name) == 2 and name[0] == "\\":
                name = name[1]
            out[(m.group(1), name)] = " ".join(line.split())
        return out

    def bound(self, table, key):
        """The binding's line, or ""."""
        return self.parse_keys(self.t("list-keys", "-T", table)).get((table, key), "")

    def stock(self):
        """Every binding of a BARE server (`-f /dev/null`, nothing loaded):
        tmux's defaults, which the config's server also starts with."""
        sock = "keys-stock-" + uuid.uuid4().hex[:12]
        run = lambda *a: subprocess.run([TMUX, "-L", sock, "-f", "/dev/null", *a], env=self.env,
                                        capture_output=True, text=True, timeout=20)
        try:
            self.assertEqual(run("new-session", "-d", "-x", "80", "-y", "20").returncode, 0)
            return self.parse_keys(run("list-keys").stdout)
        finally:
            pid = run("display", "-p", "#{pid}").stdout.strip()
            run("kill-server")
            if pid.isdigit():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def test_config_loaded_with_spaces_on(self):
        self.assertEqual(self.t("show", "-gv", "@spaces"), "on")
        # spaces.conf ran: the jump leader is ⌥W.
        self.assertIn("switch-client -T jump", self.bound("root", "M-w"))

    # Keys this config leaves at tmux's stock binding although the row has
    # no `=` (none with agent-ui; LiveWithoutAgentUi's prefix ?).
    stock_ok = frozenset()

    def test_every_catalog_key_is_bound_by_the_config(self):
        # Bound, and bound by US: `-f /dev/null` leaves tmux's defaults in
        # place, so a key the config stopped binding (prefix t's pin gone →
        # clock-mode) must not pass as "bound". A row's `=key` is the
        # other way round: a stock binding listed on purpose, which must
        # still be the stock one.
        stock = self.stock()
        self.assertEqual(stock.get(("prefix", "t")), "bind-key -T prefix t clock-mode")
        ours = {}
        for table in TABLES:
            ours.update(self.parse_keys(self.t("list-keys", "-T", table)))
        problems = []
        checked = 0
        for n, _, key, _, table, keys, _ in rows():
            for k, stock_ok in targets(keys):
                checked += 1
                where = f"keys.tsv:{n} {key!r}: {table} {k}"
                line = ours.get((table, k))
                if not line:
                    problems.append(f"{where}: not bound")
                elif stock_ok and line != stock.get((table, k)):
                    problems.append(f"{where}: marked =stock but the config rebinds it")
                elif not stock_ok and (table, k) not in self.stock_ok and line == stock.get((table, k)):
                    problems.append(f"{where}: only tmux's stock binding ({line})")
        self.assertGreater(checked, 100)
        self.assertEqual(problems, [], "\n".join(problems))

    def config_bindings(self):
        """{(table, key): line} for every binding the config made, in every
        key table on the server (custom ones like jump and stash, copy-mode
        too): those that differ from a bare server's."""
        stock = self.stock()
        everything = self.parse_keys(self.t("list-keys"))
        # The custom tables are in the full listing (sanity: not a parse miss).
        self.assertIn(("jump", "a"), everything)
        self.assertIn(("stash", "H"), everything)
        return {k: line for k, line in everything.items() if line != stock.get(k)}

    def test_every_config_binding_is_in_the_catalog(self):
        # The reverse direction: a key added or moved in the config without
        # a keys.tsv row would otherwise pass silently and the help drift.
        covered = catalogued() | not_in_help()
        problems = [
            f"{table} {key} is bound by the config but not in keys.tsv: add a row "
            f"(or allowlist it in test_keys_catalog.py with a reason)"
            for (table, key) in sorted(self.config_bindings())
            if (table, key) not in covered
        ]
        self.assertEqual(problems, [], "\n".join(problems))

    def test_allowlisted_keys_are_still_bound_by_the_config(self):
        # An entry whose binding is gone (or back to stock) is dead weight:
        # drop it from NOT_IN_HELP so the list stays honest.
        ours = self.config_bindings()
        stale = sorted(k for k in not_in_help() if k not in ours)
        self.assertEqual(stale, [], f"NOT_IN_HELP entries the config no longer binds: {stale}")

    def test_prefix_question_mark_opens_agent_ui_keys(self):
        b = self.bound("prefix", "?")
        self.assertIn("display-popup -c '#{client_tty}' -E -B", b)
        self.assertIn("-x '##{E:@popup_x}'", b)
        self.assertIn("/.local/bin/agent-ui' keys --client '#{client_tty}'", b)
        notes = self.t("list-keys", "-N", "-T", "prefix")
        # (No prefix shown: the prefix option is None, C-s is a root binding.)
        self.assertRegex(notes, r"(?m)^\s*\?\s+Key help: every key, type to filter$")


@unittest.skipIf(TMUX is None, "no tmux")
class LiveWithoutAgentUi(Live):
    """No binary: prefix ? is tmux's own list of the keys and their notes."""

    agent_ui = False
    # The fallback IS tmux's own prefix ? (list-keys -N), only re-noted.
    stock_ok = frozenset({("prefix", "?")})

    def test_prefix_question_mark_opens_agent_ui_keys(self):
        b = self.bound("prefix", "?")
        self.assertTrue(b.endswith(" list-keys -N") and "agent-ui" not in b, b)
        self.assertIn("List the keys (agent-ui draws the full help)", self.t("list-keys", "-N", "-T", "prefix"))


if __name__ == "__main__":
    unittest.main()

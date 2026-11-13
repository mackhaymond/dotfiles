"""closed-tabs.sh: the stash as a pinned shelf inside the closed-tabs history.

An entry with "pinned": true (+ "stashed_ts") is stashed: prune never ages it
out or counts it against MAX_ENTRIES, a bare `reopen` skips it, `forget`
unpins it rather than deleting it, `pin`/`unpin` move it on and off the shelf,
`pinned-json` lists the shelf, and the picker shows a Stashed section (pinned
entries + the stash session's live windows, from `stash.sh rows`) above the
Backlog. `close --pin` resolves the agent's session id BEFORE the kill and
refuses (exit 3, nothing killed) with no agent or no id.

Two harnesses:
  * No server: a `tmux` on PATH that only records its arguments, for
    everything that is history bookkeeping and picker rows.
  * A PRIVATE tmux server (`-L closed-pin-<uuid>`, `-f /dev/null`, its own
    TMUX_TMPDIR, killed in teardown) for close --pin, reopen and the live
    window preview.
stash.sh is always a fake (STASH_SH) that logs its arguments and answers
`rows` from a file. The agent detection and session lookup are fakes too
(lib-detect.sh / save-assistant-sessions.sh under the test's HOME): a `sleep
6001` is "claude", and its session id is only found while its pane is still
on the server, so an entry with an id proves the lookup ran before the kill.
`sleep 6002` is opencode and `sleep 6003` / `fakecodex` codex, whose fakes
also answer from the cwd-and-recency fallbacks the real plugin has: a pinned
close must refuse those and take only sources tied to the process.
HOME, XDG_STATE_HOME and TMPDIR are the test's temp dir. Run with unittest or
pytest.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("CLOSED_TABS_UNDER_TEST", HERE / "scripts" / "executable_closed-tabs.sh"))
TMUX = shutil.which("tmux")
ANSI = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]")
DAY = 86400

LIB_DETECT = r"""
detect_tool() {
    case "$1" in
    *"sleep 6001"*) echo claude ;;
    *"sleep 6002"*) echo opencode ;;
    *"sleep 6003"*|*fakecodex*) echo codex ;;
    esac
}
posix_quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
"""
# The plugin's emit_session, faked. The session id is in the state-file copy,
# but only handed out while the agent's pane still exists: a lookup after the
# kill finds nothing. Where the real one falls back to sources NOT tied to the
# process, so does this: opencode's DB-by-cwd (only with its 6th argument 1)
# answers "ses_db_other", codex's thread-DB heuristic "thread-from-db" (unless
# FAKE_CODEX_DB=0); like the real one, it never reads codex-<pid>.json.
SAVE = r"""
SESSION_FLAGS_FALLBACK_claude=""
extract_cli_args() { echo "--fake-args"; }
emit_session() {
    local tool="$2" apid="$3" db="${6:-1}" sid=""
    if tmux list-panes -a -F '#{pane_pid}' 2>/dev/null | grep -qx "$apid"; then
        case "$tool" in
        claude|opencode) sid=$(jq -r '.session_id // empty' "$STATE_DIR/$tool-$apid.json" 2>/dev/null) ;;
        esac
    fi
    [ -z "$sid" ] && [ "$tool" = opencode ] && [ "$db" = 1 ] && sid=ses_db_other
    [ "$tool" = codex ] && [ "${FAKE_CODEX_DB:-1}" = 1 ] && sid=thread-from-db
    [ -n "$sid" ] || return 0
    jq -cn --arg s "$sid" '{session_id: $s, cli_args: "", model: "", env: {}}' >"$PARTS_FILE"
}
"""
CODEX_SID = "01a19d8a-4290-7533-9bb9-afeaca0210bf"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="closed-pin-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = Path(self.tmp, "bin")
        self.bin.mkdir()
        (self.bin / "ai-exec").write_text("#!/bin/sh\nexit 1\n")      # describe never leaves
        (self.bin / "ai-exec").chmod(0o755)
        self.state = Path(self.tmp, "state", "tmux-closed-tabs")
        self.previews = self.state / "previews"
        self.previews.mkdir(parents=True)
        self.hist = self.state / "history.jsonl"
        self.stash_log = Path(self.tmp, "stash.log")
        self.stash_rows = Path(self.tmp, "stash.rows")
        self.stash = Path(self.tmp, "stash.sh")
        self.stash.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$*" >> "{self.stash_log}"\n'
            f'case "$1" in\n'
            f'rows) cat "{self.stash_rows}" 2>/dev/null ;;\n'
            # stash.sh's own preview: the live pane, or nothing for a gone window.
            f'preview) [ -n "$2" ] && tmux capture-pane -ep -t "$2" 2>/dev/null ;;\n'
            f'esac\n'
            "exit 0\n")
        # snooze.sh, faked the same way: every call logged with the
        # CLOSED_TABS_SH it was handed.
        self.snooze_log = Path(self.tmp, "snooze.log")
        self.snooze = Path(self.tmp, "snooze.sh")
        self.snooze.write_text(
            "#!/bin/bash\n"
            f'printf "%s | %s\\n" "$*" "$CLOSED_TABS_SH" >> "{self.snooze_log}"\n'
            "exit 0\n")
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(HOME=self.tmp, TMPDIR=self.tmp + "/", XDG_STATE_HOME=os.path.join(self.tmp, "state"),
                        PATH=f"{self.bin}:{self.env.get('PATH', '/usr/bin:/bin')}",
                        NOTIFY_SH=os.path.join(self.tmp, "no-notify"), NOTIFY_TMUX="true",
                        STASH_SH=str(self.stash), SNOOZE_SH=str(self.snooze),
                        TMUX_ASSISTANT_RESURRECT_DIR=os.path.join(self.tmp, "ard"))
        Path(self.tmp, "ard").mkdir()

    # -- helpers ---------------------------------------------------------
    def run_script(self, *args, rc=0):
        r = subprocess.run(["bash", str(SCRIPT), *args], env=self.script_env(), capture_output=True,
                           text=True, timeout=30)
        if rc is not None:
            self.assertEqual(r.returncode, rc, (args, r.stdout, r.stderr))
        return r

    def script_env(self):
        return self.env

    def write(self, *entries):
        self.hist.write_text("".join(json.dumps(e) + "\n" for e in entries))

    def entries(self):
        if not self.hist.exists():
            return []
        return [json.loads(l) for l in self.hist.read_text().splitlines() if l.strip()]

    def ids(self):
        return [e["id"] for e in self.entries()]

    def preview(self, id_, text="screen\n", age=0):
        p = self.previews / f"{id_}.ansi"
        p.write_text(text)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def stash_calls(self, want=None, timeout=5.0):
        """The fake stash.sh's calls; with `want`, wait until that line is there."""
        end = time.time() + timeout
        while True:
            calls = self.stash_log.read_text().splitlines() if self.stash_log.exists() else []
            if want is None or want in calls:
                return calls
            if time.time() > end:
                self.fail(f"stash.sh never got {want!r}: {calls}")
            time.sleep(0.05)

    def snooze_calls(self, want=None, timeout=5.0):
        """The fake snooze.sh's calls ("<args> | <CLOSED_TABS_SH>"); with
        `want` (the args), wait until such a call is there."""
        end = time.time() + timeout
        while True:
            calls = self.snooze_log.read_text().splitlines() if self.snooze_log.exists() else []
            if want is None or any(c.split(" | ")[0] == want for c in calls):
                return calls
            if time.time() > end:
                self.fail(f"snooze.sh never got {want!r}: {calls}")
            time.sleep(0.05)


def entry(id_, ts, **kw):
    e = {"id": id_, "ts": ts, "session": "main", "tmux_session": "$0", "session_created": "",
         "index": 1, "cwd": "/", "label": f"tab {id_}", "tool": "", "session_id": "", "cmd": ""}
    e.update(kw)
    return e


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class Bookkeeping(Base):
    """History and picker logic; tmux is a recorder, no server."""

    def setUp(self):
        super().setUp()
        self.tmux_log = Path(self.tmp, "tmux.log")
        (self.bin / "tmux").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.tmux_log}"\nexit 0\n')
        (self.bin / "tmux").chmod(0o755)
        self.now = int(time.time())

    def prune(self):
        self.run_script("pick-popup", "main", "")

    def test_prune_ages_out_only_unpinned_entries(self):
        old = self.now - 40 * DAY
        self.write(entry("a", old), entry("b", old, pinned=True, stashed_ts=old), entry("c", self.now))
        pa, pb = self.preview("a", age=3600), self.preview("b", age=3600)
        self.prune()
        self.assertEqual(self.ids(), ["b", "c"])
        self.assertFalse(pa.exists())
        self.assertTrue(pb.exists())

    def test_the_cap_counts_unpinned_entries_only(self):
        pinned = [entry(f"p{i}", self.now - 10, pinned=True, stashed_ts=self.now) for i in range(3)]
        unpinned = [entry(f"u{i}", self.now - 5) for i in range(405)]
        # Pinned ones at the very start (the oldest lines) and in the middle.
        self.write(pinned[0], pinned[1], *unpinned[:200], pinned[2], *unpinned[200:])
        self.prune()
        ids = self.ids()
        self.assertEqual(len(ids), 403)
        self.assertEqual([i for i in ids if i.startswith("p")], ["p0", "p1", "p2"])
        self.assertEqual([i for i in ids if i.startswith("u")], [f"u{i}" for i in range(5, 405)])
        self.assertEqual(ids[:2], ["p0", "p1"])                # pinned ones stay where they were

    def test_pin_and_unpin(self):
        self.write(entry("a", self.now - 100), entry("b", self.now - 50), entry("c", self.now - 10))
        self.run_script("pin", "a")
        a = self.entries()[0]
        self.assertTrue(a["pinned"])
        self.assertAlmostEqual(a["stashed_ts"], self.now, delta=5)
        self.stash_calls("publish-count")
        self.stash_log.unlink()
        # Unpinned: back in the backlog as its newest entry, its clock restarted.
        self.run_script("unpin", "a")
        es = self.entries()
        self.assertEqual([e["id"] for e in es], ["b", "c", "a"])
        self.assertNotIn("pinned", es[2])
        self.assertNotIn("stashed_ts", es[2])
        self.assertGreaterEqual(es[2]["ts"], self.now)
        self.stash_calls("publish-count")
        # Unpinning an unpinned entry changes nothing (and publishes nothing).
        self.stash_log.unlink()
        self.run_script("unpin", "b")
        self.assertEqual(self.ids(), ["b", "c", "a"])
        time.sleep(0.3)
        self.assertEqual(self.stash_calls(), [])
        # No such entry: an error.
        self.run_script("pin", "nope", rc=1)

    def test_forget_unpins_a_pinned_entry_and_deletes_an_unpinned_one(self):
        self.write(entry("a", self.now - 100, pinned=True, stashed_ts=self.now - 100),
                   entry("b", self.now - 50), entry("c", self.now - 10))
        pa, pb, pc = self.preview("a"), self.preview("b"), self.preview("c")
        self.run_script("forget", "a", "b")
        es = self.entries()
        self.assertEqual([e["id"] for e in es], ["c", "a"])
        self.assertNotIn("pinned", es[1])
        self.assertTrue(pa.exists())             # still an entry: keeps its snapshot
        self.assertFalse(pb.exists())
        self.assertTrue(pc.exists())
        self.stash_calls("publish-count")
        # Forgetting it again, now in the backlog, deletes it.
        self.run_script("forget", "a")
        self.assertEqual(self.ids(), ["c"])
        self.assertFalse(pa.exists())

    def test_pinned_json(self):
        self.assertEqual(json.loads(self.run_script("pinned-json").stdout), [])
        self.write(entry("a", 1, pinned=True, stashed_ts=self.now - 100), entry("b", 2),
                   entry("c", 3, pinned=True, stashed_ts=self.now - 10))
        out = json.loads(self.run_script("pinned-json").stdout)
        self.assertEqual([e["id"] for e in out], ["c", "a"])
        self.assertTrue(all(e["pinned"] for e in out))

    def rows(self, *args):
        out = self.run_script("pick-rows", *args).stdout
        return [ANSI.sub("", l).split("\t") for l in out.splitlines()]

    def test_picker_rows_stashed_above_backlog(self):
        self.write(entry("old", self.now - 7200), entry("pin1", self.now - 3 * DAY, pinned=True,
                                                         stashed_ts=self.now - 2 * 3600, tool="claude"),
                   entry("new", self.now - 60),
                   entry("pin2", self.now - 100, pinned=True, stashed_ts=self.now - 30, tool="codex"))
        self.stash_rows.write_text(
            "usage: stash.sh …\n"                                              # not a row
            f"@7\x1fproj\x1fdev server\x1f{self.now - 3600}\x1ffrozen\x1f/tmp/web\n"
            f"@9\x1fmain\x1f\x1f{self.now - 5 * 60}\x1fidle\x1f/tmp/x\n")
        rows = self.rows()
        keys = [r[0] for r in rows]
        self.assertEqual(keys, ["-", "p:pin2", "w:@9", "w:@7", "p:pin1", "-", "b:new", "b:old"])
        self.assertIn("Stashed", rows[0][1])
        self.assertIn("Backlog", rows[5][1])
        # Ages: a stashed row from when it was stashed, a backlog one from its close.
        self.assertTrue(rows[1][1].startswith("just now"), rows[1])
        self.assertTrue(rows[3][1].startswith("1h ago"), rows[3])
        self.assertTrue(rows[4][1].startswith("2h ago"), rows[4])
        self.assertTrue(rows[6][1].startswith("1m ago"), rows[6])
        self.assertIn("frozen", rows[3][1])
        self.assertIn("dev server", rows[3][1])
        self.assertIn("from proj", rows[3][1])
        self.assertIn("shell in x", rows[2][1])
        self.assertEqual(rows[3][2], "/tmp/web")
        # --stashed: the Stashed section alone.
        self.assertEqual([r[0] for r in self.rows("--stashed")], ["-", "p:pin2", "w:@9", "w:@7", "p:pin1"])
        # No stash windows and nothing pinned: no Stashed heading at all.
        self.stash_rows.unlink()
        self.write(entry("x", self.now))
        self.assertEqual([r[0] for r in self.rows()], ["-", "b:x"])
        self.assertEqual(self.rows("--stashed"), [])

    def test_suspended_and_pending_windows_read_as_such(self):
        self.stash_rows.write_text(
            f"@11\x1fproj\x1fOld agent\x1f{self.now - 3600}\x1fsuspended\x1f/tmp/a\n"
            f"@12\x1fwork\x1fresumer\x1f{self.now - 60}\x1fpending\x1f/tmp/b\n"
            f"@13\x1fproj\x1fodd\x1f{self.now - 7200}\x1fsomething-new\x1f/tmp/c\n")
        rows = self.rows("--stashed")
        self.assertEqual([r[0] for r in rows], ["-", "r:@12", "w:@11", "w:@13"])
        pending, suspended, odd = rows[1][1], rows[2][1], rows[3][1]
        self.assertIn("pending", pending)
        self.assertIn("resumer", pending)
        self.assertIn("resuming · on the bar in work", pending)
        self.assertIn("suspended", suspended)
        self.assertIn("from proj", suspended)
        self.assertNotIn("idle", odd)                  # an unknown state is not dressed up as idle
        self.assertIn("something", odd)

    def test_pick_act(self):
        self.write(entry("a", self.now - 100), entry("b", self.now - 50, pinned=True, stashed_ts=self.now))
        # ⌃s toggles: a backlog entry is pinned, a stashed one unpinned, a window left alone.
        self.run_script("pick-act", "ctrl-s", "", "", "b:a", "p:b", "w:@3", "-")
        es = {e["id"]: e for e in self.entries()}
        self.assertTrue(es["a"].get("pinned"))
        self.assertNotIn("pinned", es["b"])
        self.stash_calls("publish-count")
        self.assertNotIn("kill-many @3", self.stash_calls())
        # ⌃x: a stashed entry drops to the backlog, a backlog one is forgotten,
        # a window is discarded through stash.sh.
        self.stash_log.unlink()
        self.run_script("pick-act", "ctrl-x", "$1", "/dev/ttys999", "p:a", "b:b", "w:@3", "w:@4")
        es = self.entries()
        self.assertEqual([e["id"] for e in es], ["a"])
        self.assertNotIn("pinned", es[0])
        self.stash_calls("kill-many @3 @4")
        # ⏎ on windows: each handed to stash.sh unstash with the client.
        self.stash_log.unlink()
        self.run_script("pick-act", "", "$1", "/dev/ttys999", "w:@3", "w:@4")
        calls = self.stash_calls("unstash @4 /dev/ttys999")
        self.assertIn("unstash @3 /dev/ttys999", calls)

    def test_pick_act_on_a_pending_window(self):
        notes = Path(self.tmp, "notes.log")
        Path(self.env["NOTIFY_SH"]).write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{notes}"\n')
        Path(self.env["NOTIFY_SH"]).chmod(0o755)
        # ⌃x: on the bar already — left for closing there, and said so; the
        # parked window beside it is still discarded.
        self.run_script("pick-act", "ctrl-x", "", "/dev/ttys999", "r:@5", "w:@6")
        calls = self.stash_calls("kill-many @6")
        self.assertFalse([c for c in calls if "@5" in c], calls)
        self.assertIn("already back on the bar", notes.read_text())
        # ⌃s: nothing at all.
        self.stash_log.unlink()
        self.run_script("pick-act", "ctrl-s", "", "", "r:@5")
        time.sleep(0.2)
        self.assertEqual(self.stash_calls(), [])
        # ⏎: its resume, through stash.sh unstash.
        self.run_script("pick-act", "", "", "/dev/ttys999", "r:@5")
        self.stash_calls("unstash @5 /dev/ttys999")

    def test_preview_keys(self):
        self.preview("a", "the screen\n")
        self.assertEqual(self.run_script("preview", "p:a").stdout, "the screen\n")
        self.assertEqual(self.run_script("preview", "b:a").stdout, "the screen\n")
        self.assertEqual(self.run_script("preview", "a").stdout, "the screen\n")
        self.assertEqual(self.run_script("preview", "-").stdout, "")
        self.assertEqual(self.run_script("preview", "b:zz").stdout, "(no snapshot)\n")

    def test_the_stashed_only_picker_has_its_own_header(self):
        fzf_args = Path(self.tmp, "fzf.args")
        (self.bin / "fzf").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{fzf_args}"\ncat >/dev/null\n')
        (self.bin / "fzf").chmod(0o755)
        self.write(entry("a", self.now, pinned=True, stashed_ts=self.now), entry("b", self.now))

        def header(*args):
            self.run_script("pick", *args)
            lines = fzf_args.read_text().splitlines()
            return lines[lines.index("--header") + 1]
        full, stashed = header("$1", ""), header("--stashed", "$1", "")
        self.assertIn("backlog→forget", full)
        self.assertNotIn("forget", stashed)
        self.assertNotIn("backlog→", stashed)
        self.assertIn("⏎ restore", stashed)
        self.assertIn("⌃x discard", stashed)

    def popups(self):
        return [l for l in (self.tmux_log.read_text().splitlines() if self.tmux_log.exists() else [])
                if l.startswith("display-popup")]

    def test_pick_popup_stashed(self):
        self.write(entry("a", self.now))
        # Nothing pinned, no stash windows: --stashed has nothing to show.
        self.run_script("pick-popup", "--stashed", "$1", "/dev/ttys999")
        self.assertEqual(self.popups(), [])
        self.run_script("pick-popup", "$1", "/dev/ttys999")
        [p] = self.popups()
        self.assertIn(" pick '$1' '/dev/ttys999'", p)
        self.assertIn(f"STASH_SH='{self.stash}'", p)
        self.tmux_log.unlink()
        # A stash window alone is enough, for both.
        self.write()
        self.stash_rows.write_text(f"@7\x1fproj\x1fweb\x1f{self.now}\x1ffrozen\x1f/tmp\n")
        self.run_script("pick-popup", "$1", "/dev/ttys999", "--stashed")
        [p] = self.popups()
        self.assertIn(" pick --stashed '$1' '/dev/ttys999'", p)
        # A pinned entry too.
        self.stash_rows.unlink()
        self.tmux_log.unlink()
        self.write(entry("a", self.now, pinned=True, stashed_ts=self.now))
        self.run_script("pick-popup", "--stashed", "$1", "")
        self.assertEqual(len(self.popups()), 1)


class ServerBase(Base):
    """A private tmux server and the fake agent plugin (no tests of its own;
    test_closed_tabs_snooze.py builds on it too)."""

    def setUp(self):
        super().setUp()
        self.sock = "closed-pin-" + uuid.uuid4().hex[:12]
        sockdir = tempfile.mkdtemp(prefix="cpn-", dir="/private/tmp")   # short: sun_path
        self.addCleanup(shutil.rmtree, sockdir, True)
        self.env.update(TERM="xterm-256color", TMUX_TMPDIR=sockdir)
        self.addCleanup(self.kill_server)
        self.t("new-session", "-d", "-s", "main", "-x", "120", "-y", "38", "sleep 600")
        for opt, val in (("exit-empty", "off"), ("default-shell", "/bin/sh")):
            self.t("set", "-g", opt, val)
        self.path = self.t("display", "-p", "#{socket_path}")
        plug = Path(self.tmp, ".config", "tmux", "plugins", "tmux-assistant-resurrect", "scripts")
        plug.mkdir(parents=True)
        (plug / "lib-detect.sh").write_text(LIB_DETECT)
        (plug / "save-assistant-sessions.sh").write_text(SAVE)

    def kill_server(self):
        subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "kill-server"], env=self.env,
                       capture_output=True, timeout=10)

    def t(self, *args):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def script_env(self):
        return dict(self.env, TMUX=f"{self.path},0,0")

    def panes(self):
        return self.t("list-panes", "-a", "-F", "#{pane_id}").split()

    def agent_tab(self, sid="11111111-2222-3333-4444-555555555555"):
        pane = self.t("new-window", "-d", "-t", "=main:", "-P", "-F", "#{pane_id}", "exec sleep 6001")
        pid = self.t("display", "-p", "-t", pane, "#{pane_pid}")
        if sid:
            Path(self.tmp, "ard", f"claude-{pid}.json").write_text(json.dumps({"session_id": sid}))
        return pane


@unittest.skipUnless(TMUX and shutil.which("jq"), "tmux / jq not installed")
class OnAServer(ServerBase):
    """close --pin, reopen and the live preview against a private tmux server."""

    def test_close_pin_resolves_the_session_before_the_kill(self):
        pane = self.agent_tab()
        self.run_script("close", "--pin", pane)
        self.assertNotIn(pane, self.panes())
        [e] = self.entries()
        self.assertTrue(e["pinned"])
        self.assertAlmostEqual(e["stashed_ts"], e["ts"], delta=1)
        self.assertEqual(e["tool"], "claude")
        self.assertEqual(e["session_id"], "11111111-2222-3333-4444-555555555555")
        self.assertIn("--resume '11111111-2222-3333-4444-555555555555'", e["cmd"])
        self.assertTrue((self.previews / f"{e['id']}.ansi").exists())
        self.stash_calls("publish-count")

    def test_a_plain_close_still_resolves_after_the_kill(self):
        # The contrast that makes the test above mean something: the fake only
        # answers while the pane lives, and a plain close asks after the kill.
        pane = self.agent_tab()
        self.run_script("close", pane)
        [e] = self.entries()
        self.assertEqual((e["tool"], e["session_id"]), ("claude", ""))
        self.assertNotIn("pinned", e)
        time.sleep(0.3)
        self.assertNotIn("publish-count", self.stash_calls())

    def test_close_pin_refuses_without_an_agent_or_a_session_id(self):
        shell = self.t("new-window", "-d", "-t", "=main:", "-P", "-F", "#{pane_id}", "sleep 600")
        nosid = self.agent_tab(sid=None)
        for pane, why in ((shell, "no agent"), (nosid, "session id")):
            with self.subTest(why):
                r = self.run_script("close", "--pin", pane, rc=3)
                self.assertIn(why, r.stderr)
                self.assertIn(pane, self.panes())
        self.assertEqual(self.entries(), [])
        time.sleep(0.2)
        self.assertEqual(list(self.previews.iterdir()), [])
        self.assertEqual(self.stash_calls(), [])

    # -- pinning only takes a session tied to this very process ----------
    def tab(self, command):
        pane = self.t("new-window", "-d", "-t", "=main:", "-P", "-F", "#{pane_id}", command)
        return pane, self.t("display", "-p", "-t", pane, "#{pane_pid}")

    def refused(self, pane):
        n = len(self.entries())
        r = self.run_script("close", "--pin", pane, rc=3)
        self.assertIn("couldn't tie the session to this tab", r.stderr)
        self.assertIn(pane, self.panes())
        self.assertEqual(len(self.entries()), n)

    def pinned(self, pane):
        self.run_script("close", "--pin", pane)
        self.assertNotIn(pane, self.panes())
        e = self.entries()[-1]
        self.assertTrue(e["pinned"])
        return e

    def test_opencode_pins_only_from_argv_or_its_state_file(self):
        # A plain close takes the DB's newest session in the cwd (the fake's
        # stand-in for another opencode in the same directory) ...
        pane, _ = self.tab("exec sleep 6002")
        self.run_script("close", pane)
        self.assertEqual(self.entries()[0]["session_id"], "ses_db_other")
        self.hist.unlink()
        # ... a pinned one never does.
        pane, pid = self.tab("exec sleep 6002")
        self.refused(pane)
        Path(self.tmp, "ard", f"opencode-{pid}.json").write_text(json.dumps({"session_id": "ses_mine"}))
        e = self.pinned(pane)
        self.assertEqual((e["tool"], e["session_id"]), ("opencode", "ses_mine"))
        self.assertIn("-s 'ses_mine'", e["cmd"])

    def tracker(self, pid, pane, **over):
        start = " ".join(subprocess.run(["ps", "-p", pid, "-o", "lstart="], capture_output=True,
                                        text=True).stdout.split())
        rec = dict(session_id=CODEX_SID, ppid=int(pid), frontend_start=start,
                   env=dict(tmux_pane=pane, tmux_socket=self.path))
        rec.update(over)
        Path(self.tmp, "ard", f"codex-{pid}.json").write_text(json.dumps(rec))

    def test_codex_pins_only_a_thread_tied_to_the_process(self):
        # The plugin's answer here is its cwd/recency heuristic: a plain close
        # takes it, a pinned close refuses it.
        pane, _ = self.tab("exec sleep 6003")
        self.run_script("close", pane)
        self.assertEqual(self.entries()[0]["session_id"], "thread-from-db")
        self.hist.unlink()
        pane, pid = self.tab("exec sleep 6003")
        self.refused(pane)
        # A tracker record that is not this process's on this pane: refused.
        other = self.t("display", "-p", "-t", "=main:", "#{pane_id}")
        for bad in (dict(env=dict(tmux_pane=other, tmux_socket=self.path)),
                    dict(env=dict(tmux_pane=pane, tmux_socket="/elsewhere/default")),
                    dict(frontend_start="Thu Jan 1 00:00:00 2026"),          # a reused pid
                    dict(ppid=1)):
            with self.subTest(bad=bad):
                self.tracker(pid, pane, **bad)
                self.refused(pane)
        # The real one: its thread, not the heuristic's.
        self.tracker(pid, pane)
        e = self.pinned(pane)
        self.assertEqual((e["tool"], e["session_id"]), ("codex", CODEX_SID))
        self.assertIn(f"resume '{CODEX_SID}'", e["cmd"])

    def test_codex_tied_by_session_tags_or_argv(self):
        tags = Path(self.tmp, ".codex", "session-tags.jsonl")
        tags.parent.mkdir()
        pane, pid = self.tab("exec sleep 6003")
        tags.write_text(json.dumps({"pid": int(pid) + 100000, "session": "not-mine"}) + "\n"
                        + json.dumps({"pid": int(pid), "session": "tagged-thread"}) + "\n")
        self.assertEqual(self.pinned(pane)["session_id"], "tagged-thread")
        # Launched as `codex resume <uuid>`, and the plugin found nothing at
        # all: the argv's thread, with the plugin's cli_args.
        self.env["FAKE_CODEX_DB"] = "0"
        pane, _ = self.tab(f"exec perl -e 'sleep 600' fakecodex resume {CODEX_SID}")
        e = self.pinned(pane)
        self.assertEqual(e["session_id"], CODEX_SID)
        self.assertIn("--fake-args", e["cmd"])
        # A uuid in argv that is not a `resume` operand ties nothing.
        pane, _ = self.tab(f"exec perl -e 'sleep 600' fakecodex --note {CODEX_SID}")
        self.refused(pane)

    def windows(self):
        return self.t("list-windows", "-t", "=main", "-F", "#{window_id}").split()

    def test_bare_reopen_skips_pinned_entries(self):
        now = int(time.time())
        self.write(entry("a", now - 100, cwd=self.tmp), entry("p", now - 10, cwd=self.tmp,
                                                               pinned=True, stashed_ts=now - 10))
        before = self.windows()
        self.run_script("reopen", "main", "")
        self.assertEqual(len(self.windows()), len(before) + 1)
        self.assertEqual(self.ids(), ["p"])
        # Nothing unpinned left: a bare reopen leaves the shelf alone.
        self.run_script("reopen", "main", "")
        self.assertEqual(self.ids(), ["p"])
        self.assertEqual(len(self.windows()), len(before) + 1)
        self.assertEqual(self.stash_calls(), [])
        # By id, a stashed one comes back and leaves the store.
        self.run_script("reopen", "main", "", "p")
        self.assertEqual(self.ids(), [])
        self.assertEqual(len(self.windows()), len(before) + 2)
        self.stash_calls("publish-count")

    def test_a_window_rows_preview_comes_from_stash_sh(self):
        win = self.t("new-window", "-d", "-t", "=main:", "-P", "-F", "#{window_id}",
                     "printf 'live-screen-marker\\n'; exec sleep 600")

        def preview(key):
            end = time.time() + 5
            while True:
                out = self.run_script("preview", key).stdout
                if "live-screen-marker" in out or time.time() > end:
                    return out
                time.sleep(0.05)
        for key in (f"w:{win}", f"r:{win}"):
            out = preview(key)
            self.assertIn("live-screen-marker", out)
            self.assertTrue(out.endswith("live-screen-marker\n"), repr(out[-80:]))   # blank rows trimmed
        self.assertIn(f"preview {win}", self.stash_calls())     # stash.sh's (a snapshot for a suspended one)
        self.assertEqual(self.run_script("preview", "w:@9999").stdout, "(gone)\n")
        # No stash.sh: the live pane itself.
        self.env["STASH_SH"] = os.path.join(self.tmp, "no-stash.sh")
        self.assertTrue(preview(f"w:{win}").endswith("live-screen-marker\n"))

    # -- a reopen never lands in a hidden session ------------------------
    def hidden(self, name, how="option"):
        self.t("new-session", "-d", "-s", name, "sleep 600")
        if how == "option":
            self.t("set", "-t", name, "@space_hidden", "1")
        return self.t("display", "-p", "-t", f"={name}:", "#{session_id}")

    def reopened_in(self, rec, sess, tty):
        self.write(rec)
        self.run_script("reopen", sess, tty)
        self.assertEqual(self.entries(), [])
        # The newest window on the server is the reopened one.
        rows = self.t("list-windows", "-a", "-F", "#{window_id} #{session_name}").splitlines()
        return max(rows, key=lambda r: int(r.split()[0][1:])).split()[1]

    def test_reopen_never_lands_in_a_hidden_session(self):
        now = int(time.time())
        agents = self.hidden("agents")
        self.hidden("stash", how="name")                   # hidden by name alone
        self.t("set", "-g", "@space_hidden_sessions", "agents tasks stash scratch")
        gone = entry("g", now, session="vanished", cwd=self.tmp)
        # No session and no client given (the sidebar, run-shell -b): main,
        # never tmux's "best" session.
        self.assertEqual(self.reopened_in(gone, "", ""), "main")
        # The client's session names a hidden one: main.
        self.assertEqual(self.reopened_in(gone, agents, ""), "main")
        # The entry's own session exists but is hidden (closed out of the stash
        # before origins were recorded): not back into it.
        self.assertEqual(self.reopened_in(entry("s", now, session="stash", cwd=self.tmp), "", ""), "main")
        # A visible client session is used as before.
        self.t("new-session", "-d", "-s", "proj", "sleep 600")
        proj = self.t("display", "-p", "-t", "=proj:", "#{session_id}")
        self.assertEqual(self.reopened_in(gone, proj, ""), "proj")

    def test_reopen_falls_back_to_the_clients_session_by_its_tty(self):
        now = int(time.time())
        agents = self.hidden("agents")
        self.t("new-session", "-d", "-s", "proj", "sleep 600")
        # A real client, attached to proj from a pane of a hidden host session.
        attach = f"env -u TMUX -u TMUX_PANE tmux -L {self.sock} -f /dev/null attach -t proj"
        self.t("new-session", "-d", "-s", "host", "-x", "120", "-y", "40", attach)
        self.t("set", "-t", "host", "@space_hidden", "1")
        end = time.time() + 5
        while not self.t("list-clients", "-F", "#{client_tty}") and time.time() < end:
            time.sleep(0.05)
        tty = self.t("list-clients", "-F", "#{client_tty}")
        self.assertTrue(tty)
        gone = entry("g", now, session="vanished", cwd=self.tmp)
        self.assertEqual(self.reopened_in(gone, "", tty), "proj")
        self.assertEqual(self.reopened_in(gone, agents, tty), "proj")

    def test_a_window_closed_out_of_the_stash_is_recorded_under_its_origin(self):
        self.t("new-session", "-d", "-s", "proj", "sleep 600")
        self.hidden("stash", how="name")
        pane = self.t("new-window", "-d", "-t", "=stash:", "-P", "-F", "#{pane_id}", "sleep 600")
        self.t("set", "-w", "-t", pane, "@stash_origin", "proj")
        self.run_script("close", pane)
        [e] = self.entries()
        self.assertEqual((e["session"], e["tmux_session"]), ("proj", ""))
        self.assertEqual(self.reopened_in(e, "", ""), "proj")


if __name__ == "__main__":
    unittest.main()

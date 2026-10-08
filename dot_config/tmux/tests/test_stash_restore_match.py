"""stash.sh: a parked window's sidecar row must find its window after a restore.

The bug (live, 2026-10-01 .. 10-07): stash:4 held a Claude agent that was
parked while mid-turn, so it was never suspended and had no @stash_cwd. Its
sidecar row's only discriminator was therefore the window NAME, which with
automatic-rename on is the agent's process title — its version string. After a
tmux restart the restored pane was a shell (or a just-restarted claude), the
name differed, restore-state logged "skipped stash:4 — no matching window" and
dropped @stash_origin/@stash_label; the next save mirrored the now-bare window
as a row of nothing but an index and a name, which then failed the same way on
every later restore, forever.

Isolated HOME and TMPDIR, a fake `tmux` on PATH whose state is a JSON file, and
@resurrect-dir pointed into the temp dir (the pattern from test_stash_ts.py).
No real tmux server, sidecar, lock or log is touched. STASH_SELF is
/usr/bin/true so the detached `describe` a park spawns is a no-op.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPTS = Path(os.environ.get(
    "AGENT_TMUX_SCRIPTS",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts")))
STASH = SCRIPTS / "executable_stash.sh"
BASH_DIR = str(Path(shutil.which("bash") or "/opt/homebrew/bin/bash").parent)
SEP = "\x1f"

# test_stash_ts.py's fake, plus hyphenated format names (#{automatic-rename}),
# which a window answers from its opts like any other option.
FAKE_TMUX = r'''#!/usr/bin/env python3
import fcntl, json, os, re, sys
from pathlib import Path
p = Path(os.environ["FAKE_TMUX_STATE"])

def arg(c, f):
    return c[c.index(f) + 1] if f in c else None

def has_flag(c, letter):
    return any(x.startswith("-") and not x.startswith("--") and letter in x[1:] for x in c[1:])

def render(fmt, v):
    return re.sub(r"#\{([@\w-]+)\}", lambda m: str(v.get(m.group(1), "")), fmt)

def wvars(s, wid, pane=None):
    w = s["windows"][wid]
    pane = pane or (w["panes"][0] if w["panes"] else {})
    v = {"window_id": wid, "session_name": w["session"], "window_index": w["index"],
         "window_name": w.get("name", "zsh"), "pane_id": pane.get("id", ""),
         "pane_index": pane.get("index", ""), "pane_current_path": pane.get("cwd", ""),
         "pane_pid": pane.get("pid", ""), "pane_current_command": "zsh"}
    v.update(w.get("opts", {}))
    return v

def ordered(s, sess=None):
    ws = [(w["session"], w["index"], wid) for wid, w in s["windows"].items()
          if sess is None or w["session"] == sess]
    return [wid for _, _, wid in sorted(ws)]

def sessions(s):
    return {w["session"] for w in s["windows"].values()}

def resolve(s, t):
    if t in s["windows"]:
        return t, None
    for wid, w in s["windows"].items():
        for pn in w["panes"]:
            if pn["id"] == t:
                return wid, pn
    return None, None

def new_window(s, sess, name):
    s["next"] += 1
    wid = "@%d" % s["next"]
    idx = max([w["index"] for w in s["windows"].values() if w["session"] == sess] or [0]) + 1
    s["windows"][wid] = {"session": sess, "index": idx, "name": name, "opts": {},
                         "panes": [{"index": 0, "id": "%%%d" % s["next"], "cwd": "/", "pid": ""}]}
    return wid

with p.with_suffix(".lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    s = json.loads(p.read_text())
    rc, out = 0, []
    c = sys.argv[1:]
    s["calls"].append(c)
    cmd = c[0]
    if cmd == "has-session":
        rc = 0 if arg(c, "-t").lstrip("=") in sessions(s) else 1
    elif cmd == "list-windows":
        if has_flag(c, "a"):
            wins = ordered(s)
        else:
            sess = arg(c, "-t").lstrip("=")
            if sess not in sessions(s):
                rc = 1; wins = []
            else:
                wins = ordered(s, sess)
        out = [render(arg(c, "-F"), wvars(s, w)) for w in wins]
    elif cmd == "list-panes":
        wid, _ = resolve(s, arg(c, "-t"))
        if wid is None: rc = 1
        else: out = [render(arg(c, "-F"), wvars(s, wid, pn)) for pn in s["windows"][wid]["panes"]]
    elif cmd in ("list-clients", "run-shell"):
        pass
    elif cmd in ("show", "show-options"):
        name = c[-1]
        if has_flag(c, "g"):
            out = [s["globals"].get(name, "")]
        else:
            wid, _ = resolve(s, arg(c, "-t"))
            if wid is None: rc = 1
            else: out = [s["windows"][wid].get("opts", {}).get(name, "")]
    elif cmd in ("set-option", "set"):
        t = arg(c, "-t")
        rest = [x for x in c[1:] if not x.startswith("-") and x != t]
        name = rest[0]; val = rest[1] if len(rest) > 1 else ""
        unset = has_flag(c, "u")
        if has_flag(c, "g") and not t:
            if unset: s["globals"].pop(name, None)
            else: s["globals"][name] = val
        else:
            wid, _ = resolve(s, t)
            if wid is None: rc = 1
            else:
                o = s["windows"][wid].setdefault("opts", {})
                if unset: o.pop(name, None)
                else: o[name] = val
    elif cmd == "display-message":
        if has_flag(c, "p") and arg(c, "-t") is None:
            # Server-wide formats only (#{start_time}); no current window here.
            out = [render(c[-1], {"start_time": s.get("start_time", "")})]
        elif has_flag(c, "p"):
            wid, pn = resolve(s, arg(c, "-t"))
            if wid is None: rc = 1
            else: out = [render(c[-1], wvars(s, wid, pn))]
        else:
            s["messages"].append(c[-1])
    elif cmd == "new-session":
        sess = arg(c, "-s")
        if sess in sessions(s): rc = 1
        else:
            wid = new_window(s, sess, arg(c, "-n") or "zsh")
            if "-P" in c: out = [render(arg(c, "-F"), wvars(s, wid))]
    elif cmd == "move-window":
        if has_flag(c, "r"):
            sess = arg(c, "-t").lstrip("=")
            for i, wid in enumerate(ordered(s, sess), 1):
                s["windows"][wid]["index"] = i
        else:
            wid, _ = resolve(s, arg(c, "-s"))
            dst = arg(c, "-t").rstrip(":").lstrip("=")
            if wid is None or dst not in sessions(s): rc = 1
            else:
                idx = max([w["index"] for w in s["windows"].values() if w["session"] == dst] or [0]) + 1
                s["windows"][wid]["session"] = dst; s["windows"][wid]["index"] = idx
    elif cmd == "kill-window":
        wid, _ = resolve(s, arg(c, "-t"))
        if wid is None: rc = 1
        else: del s["windows"][wid]
    else:
        sys.stderr.write("fake tmux: unhandled %r\n" % (c,)); rc = 1
    p.write_text(json.dumps(s))
    if out: print("\n".join(out))
    sys.exit(rc)
'''

AUTO = {"automatic-rename": "1"}     # tmux's default here; the name tracks the process
MANUAL = {"automatic-rename": "0"}   # a name somebody chose


def win(session, index, name, cwd, opts=None):
    return {"session": session, "index": index, "name": name, "opts": dict(opts or {}),
            "panes": [{"index": 0, "id": "%" + str(index + 100 * len(session)), "cwd": cwd, "pid": ""}]}


def row(*fields):
    return SEP.join(fields) + "\n"


class FakeEnv:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="stash-match-"))
        self.home = self.dir / "home"; self.tmp = self.dir / "tmp"; self.bin = self.dir / "bin"
        self.rdir = self.dir / "resurrect"
        for d in (self.home, self.tmp, self.bin, self.rdir):
            d.mkdir()
        (self.home / "Library/Logs").mkdir(parents=True)
        f = self.bin / "tmux"; f.write_text(FAKE_TMUX); f.chmod(0o755)
        self.state = self.dir / "tmux.json"
        self.sidecar = self.rdir / "stash-state.tsv"
        self.orphans = self.rdir / "stash-orphans.tsv"
        self.logfile = self.home / "Library/Logs/tmux-stash.log"

    def set_server(self, windows, start_time=""):
        n = max([int(w[1:]) for w in windows] or [0]) + 100
        self.state.write_text(json.dumps({
            "windows": windows, "globals": {"@resurrect-dir": str(self.rdir)},
            "calls": [], "messages": [], "next": n, "start_time": str(start_time)}))

    def snapshot(self, stamp, mtime=None, last=True):
        """A resurrect save captured at <stamp>; `last` -> it, as resurrect-save.sh links it.

        Named the way resurrect-save.sh names it (local-time `date
        +%Y%m%dT%H%M%S`). <mtime> defaults to the stamp; the post-save hook
        makes it later in real life. The link itself is created NOW, so its
        own (lstat) mtime is not the snapshot's — as after an unchanged save
        or a resurrect-guard revert."""
        mtime = stamp if mtime is None else mtime
        f = self.rdir / ("tmux_resurrect_%s.txt" % time.strftime("%Y%m%dT%H%M%S", time.localtime(stamp)))
        f.write_text("pane\tstash\t1\n")
        os.utime(f, (mtime, mtime))
        if last:
            link = self.rdir / "last"
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(f.name, link)
        return f

    def calls(self):
        return self.server()["calls"]

    def server(self):
        return json.loads(self.state.read_text())

    def opts(self, wid):
        return self.server()["windows"][wid].get("opts", {})

    def run(self, *args, tmpdir=None):
        env = {"HOME": str(self.home), "TMPDIR": (tmpdir or str(self.tmp)) + "/",
               "PATH": f"{self.bin}:{BASH_DIR}:/usr/bin:/bin",
               "FAKE_TMUX_STATE": str(self.state), "STASH_SELF": "/usr/bin/true"}
        if "TZ" in os.environ:      # snapshot stamps are local time on both sides
            env["TZ"] = os.environ["TZ"]
        r = subprocess.run(["bash", str(STASH), *args], env=env,
                           capture_output=True, text=True, timeout=60)
        unhandled = [l for l in r.stderr.splitlines() if "fake tmux: unhandled" in l]
        assert not unhandled, unhandled
        return r

    def rows(self):
        if not self.sidecar.exists():
            return []
        return [l.split(SEP) for l in self.sidecar.read_text().splitlines() if l]

    def log(self):
        return self.logfile.read_text() if self.logfile.exists() else ""

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class RestoreMatch(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def test_unsuspended_agent_park_survives_restore(self):
        """The stash:4 bug end to end: park a live agent, restart, restore."""
        e = self.env
        e.set_server({
            "@1": win("main", 1, "2.1.285", "/Users/u",
                      {**AUTO, "@agent_summary": "Bruincast media download research"}),
            "@2": win("main", 2, "zsh", "/Users/u/code", AUTO),
        })
        r = e.run("stash-many", "@1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(e.server()["windows"]["@1"]["session"], "stash")
        self.assertNotIn("@stash_session", e.opts("@1"))   # no agent registered: not suspended
        ts = e.opts("@1")["@stash_ts"]

        rows = [x for x in e.rows() if x[0] == "stash"]
        self.assertEqual(len(rows), 1, e.sidecar.read_text())
        self.assertEqual(rows[0][4:6], ["main", "Bruincast media download research"])
        self.assertEqual(rows[0][7], "")             # no @stash_cwd: nothing to resume
        self.assertEqual(rows[0][9:], ["/Users/u"])  # ...so the pane dir is its identity
        idx = int(rows[0][1])

        # Restart. The restored pane is a shell again (or a claude that has not
        # retitled itself yet), so the auto-renamed name no longer reads 2.1.285.
        e.set_server({
            "@7": win("main", 1, "zsh", "/Users/u/code", AUTO),
            "@86": win("stash", idx, "zsh", "/Users/u", AUTO),
        })
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        o = e.opts("@86")
        self.assertEqual(o.get("@stash_origin"), "main")
        self.assertEqual(o.get("@stash_label"), "Bruincast media download research")
        self.assertEqual(o.get("@stash_ts"), ts)
        self.assertNotIn("@stash_cwd", o)            # identity only, never an option
        self.assertNotIn("no matching window", e.log())
        self.assertNotIn("could not re-apply", e.log())
        self.assertNotIn("no recorded origin", e.log())

        # And the next save mirrors it whole again, rather than as a bare row.
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [x for x in e.rows() if x[0] == "stash"]
        self.assertEqual(rows[0][4], "main")

    def test_live_bare_row_is_ignored_and_the_real_gap_is_named(self):
        """The row actually on disk: nothing but an index and a version string."""
        e = self.env
        sid = "aa3db67d-9fc9-49f8-a307-47f8732edc0e"
        e.sidecar.write_text(
            row("stash", "1", "zsh", "1", "main", "~/Backyard Stage Spec", sid, "/Users/u", "1790796914")
            + row("stash", "4", "2.1.291", "", "", "", "", "", ""))
        e.set_server({
            "@7": win("main", 1, "zsh", "/Users/u", AUTO),
            "@83": win("stash", 1, "zsh", "/Users/u", AUTO),
            "@86": win("stash", 4, "claude", "/Users/u", AUTO),
        })
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = e.log()
        self.assertNotIn("skipped stash:4", log)
        self.assertNotIn("no matching window", log)
        self.assertEqual(e.opts("@83").get("@stash_session"), sid)
        self.assertNotIn("@stash_origin", e.opts("@86"))
        # The actual problem — a parked tab with no origin — is reported instead.
        self.assertIn("parked window stash:4 (@86", log)
        self.assertIn("no recorded origin", log)
        self.assertNotIn("parked window stash:1", log)

        # A save no longer writes a row for a window that carries no state.
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x[1] for x in e.rows() if x[0] == "stash"], ["1"], e.sidecar.read_text())

    def test_auto_renamed_window_is_still_checked_by_directory(self):
        """Dropping the name must not degrade identity to the bare index."""
        e = self.env
        e.sidecar.write_text(row("stash", "1", "2.1.285", "", "main", "Lost tab", "", "", "1790000000", "/Users/u"))
        e.set_server({"@5": win("stash", 1, "zsh", "/somewhere/else", AUTO)})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("@stash_origin", e.opts("@5"))
        self.assertNotIn("@stash_label", e.opts("@5"))
        self.assertIn('could not re-apply parked state to stash:1 (from main, "Lost tab")', e.log())

    def test_a_chosen_name_still_has_to_match(self):
        e = self.env
        e.sidecar.write_text(row("stash", "1", "notes", "", "main", "Notes", "", "", "1790000000"))
        e.set_server({"@5": win("stash", 1, "scratch", "/w", MANUAL)})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("@stash_origin", e.opts("@5"))
        # A hand-named window records its pane dir too: resurrect may bring it
        # back auto-renamed, and then the dir is all that corroborates the slot.
        e.set_server({"@5": win("stash", 1, "notes", "/w", {**MANUAL, "@stash_origin": "main"}),
                      "@6": win("main", 1, "zsh", "/w", AUTO)})
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x for x in e.rows() if x[0] == "stash"][0][9:], ["/w"])

    def test_index_alone_never_matches(self):
        """F2: an auto-renamed window, a row with neither cwd nor pane dir."""
        e = self.env
        e.sidecar.write_text(row("stash", "1", "notes", "", "main", "Notes", "", "", "1790000000"))
        e.set_server({"@5": win("stash", 1, "zsh", "/anywhere", AUTO)})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({k: v for k, v in e.opts("@5").items() if k.startswith("@stash")}, {})
        self.assertIn("could not re-apply parked state to stash:1", e.log())

    def test_unpark_then_renumber_never_moves_a_label(self):
        """F1, save-time: A leaves, B slides into A's slot, then a publish."""
        e = self.env
        e.sidecar.write_text(
            row("stash", "1", "2.1.285", "", "main", "Tab A", "", "", "1790000001", "/Users/u")
            + row("stash", "2", "2.1.285", "", "work", "Tab B", "", "", "1790000002", "/Users/u"))
        # Restarted; restore-state has not run; A was unparked and renumber
        # slid B (@22) into slot 1.
        e.set_server({"@22": win("stash", 1, "zsh", "/Users/u", AUTO),
                      "@9": win("main", 1, "zsh", "/Users/u/x", AUTO)})
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Tab A", e.sidecar.read_text() if e.sidecar.exists() else "")
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({k: v for k, v in e.opts("@22").items() if k.startswith("@stash")}, {})

    def test_stale_snapshot_never_moves_a_label(self):
        """F1, restore-time: the sidecar describes a layout resurrect did not rebuild."""
        e = self.env
        e.sidecar.write_text(
            row("stash", "1", "zsh", "", "main", "Tab A", "", "", "1790000001", "/Users/u")
            + row("stash", "2", "zsh", "", "work", "Tab B", "", "", "1790000002", "/Users/u"))
        e.set_server({"@22": win("stash", 1, "zsh", "/Users/u", AUTO),
                      "@23": win("stash", 2, "zsh", "/Users/u", AUTO)})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        for wid in ("@22", "@23"):
            self.assertNotIn("@stash_label", e.opts(wid))
        self.assertEqual(e.log().count("could not re-apply parked state"), 2)

    def test_a_lookalike_window_without_a_row_blocks_the_match(self):
        e = self.env
        e.sidecar.write_text(row("stash", "1", "2.1.285", "", "main", "Tab A", "", "", "1790000001", "/Users/u"))
        e.set_server({"@22": win("stash", 1, "zsh", "/Users/u", AUTO),
                      "@23": win("stash", 2, "zsh", "/Users/u", AUTO)})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("@stash_label", e.opts("@22"))

    def test_live_layout_places_the_unsuspended_tab_among_suspended_ones(self):
        """Today's stash after the repair: four windows in $HOME, one without a sid."""
        e = self.env
        sids = ["aa3db67d-0000-0000-0000-000000000001", "4664ef27-0000-0000-0000-000000000003",
                "98c7f75b-0000-0000-0000-000000000005"]
        e.sidecar.write_text(
            row("stash", "1", "zsh", "1", "main", "Backyard", sids[0], "/Users/u", "1790796914", "/Users/u")
            + row("stash", "2", "zsh", "1", "main", "Notch", "bcf8d86e-0000-0000-0000-000000000002",
                  "/Users/u/proj", "1790811939", "/Users/u/proj")
            + row("stash", "3", "zsh", "1", "main", "Canvas", sids[1], "/Users/u", "1790811956", "/Users/u")
            + row("stash", "4", "2.1.291", "", "main", "~/Bruincast Media Download", "", "", "1790812058", "/Users/u")
            + row("stash", "5", "zsh", "1", "main", "House", sids[2], "/Users/u", "1791296890", "/Users/u"))
        windows = {"@9": win("main", 1, "zsh", "/Users/u", AUTO)}
        for i, wid in enumerate(["@83", "@84", "@85", "@86", "@77"], 1):
            w = win("stash", i, "claude" if wid == "@86" else "zsh",
                    "/Users/u/proj" if wid == "@84" else "/Users/u", AUTO)
            w["panes"][0]["index"] = 1
            windows[wid] = w
        e.set_server(windows)
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        o = e.opts("@86")
        self.assertEqual(o.get("@stash_origin"), "main")
        self.assertEqual(o.get("@stash_label"), "~/Bruincast Media Download")
        self.assertEqual(o.get("@stash_ts"), "1790812058")
        self.assertNotIn("@stash_session", o)
        self.assertEqual(e.opts("@83").get("@stash_session"), sids[0])
        self.assertEqual(e.opts("@77").get("@stash_session"), sids[2])
        self.assertNotIn("could not", e.log())
        self.assertNotIn("no recorded origin", e.log())

    def test_origin_less_window_is_named_even_without_a_sidecar(self):
        e = self.env
        e.set_server({"@86": win("stash", 1, "claude", "/Users/u", AUTO)})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("parked window stash:1 (@86", e.log())

    def test_suspended_row_with_a_retitled_window_is_carried_not_orphaned(self):
        """Same name problem on the save-time merge: a sid row was orphaned."""
        e = self.env
        sid = "bcf8d86e-34b7-4f79-b230-703d9d3b2398"
        e.sidecar.write_text(row("stash", "1", "2.1.285", "0", "main", "Notch", sid, "/Users/u/proj", "1790811939"))
        e.set_server({"@84": win("stash", 1, "zsh", "/Users/u/proj", AUTO),
                      "@9": win("main", 1, "zsh", "/Users/u", AUTO)})
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x[6] for x in e.rows() if len(x) > 6 and x[6]], [sid], e.sidecar.read_text())
        self.assertFalse(e.orphans.exists(), e.orphans.read_text() if e.orphans.exists() else "")

    def test_park_before_restore_keeps_other_tabs_parked_state(self):
        """A publish after a restart, before restore-state, wiped sid-less rows."""
        e = self.env
        e.sidecar.write_text(row("stash", "1", "2.1.285", "", "main", "Bruincast", "", "", "1790796914", "/Users/u"))
        # Restarted server: the parked window is back, with no options yet.
        e.set_server({
            "@86": win("stash", 1, "zsh", "/Users/u", AUTO),
            "@1": win("main", 1, "zsh", "/Users/u/a", AUTO),
            "@2": win("main", 2, "zsh", "/Users/u/b", AUTO),
        })
        r = e.run("stash-many", "@2")                # an unrelated park first
        self.assertEqual(r.returncode, 0, r.stderr)
        stash_rows = {x[1]: x for x in e.rows() if x[0] == "stash"}
        self.assertEqual(stash_rows["1"][4:6], ["main", "Bruincast"], e.sidecar.read_text())

        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        o = e.opts("@86")
        self.assertEqual(o.get("@stash_origin"), "main")
        self.assertEqual(o.get("@stash_label"), "Bruincast")
        self.assertEqual(o.get("@stash_ts"), "1790796914")


T0 = 1790900000          # the resurrect snapshot that gets restored
T1 = T0 + 300            # A parked, inside the 15-minute continuum interval
T2 = T0 + 600            # tmux restarts


class StaleSnapshot(unittest.TestCase):
    """F3: a stale snapshot can leave exactly ONE candidate window for a sid-less row.

    Snapshot at T0 has X parked at stash:1 in $HOME. By T1, X was unparked and
    busy agent A (also in $HOME, so not suspended) parked; renumber put A at
    stash:1 and the sidecar's slot-1 row is A's. tmux restarts and restores T0:
    stash:1 is X, with no state; A's key is unique and X is the only pool
    window, so every uniqueness test passes — and A's origin/label/ts land on X.
    """

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def restored_server(self, e, start_time=T2):
        e.set_server({
            "@30": win("stash", 1, "zsh", "/Users/u", AUTO),        # X, from the T0 snapshot
            "@31": win("main", 1, "zsh", "/Users/u/code", AUTO),
        }, start_time=start_time)

    def a_row(self, ts=str(T1)):
        return row("stash", "1", "2.1.291", "", "work", "Tab A", "", "", ts, "/Users/u")

    def assert_x_untouched(self, e):
        self.assertEqual({k: v for k, v in e.opts("@30").items() if k.startswith("@stash")}, {})

    def test_restore_does_not_put_a_later_park_on_the_snapshot_window(self):
        e = self.env
        e.sidecar.write_text(self.a_row())
        # Started well after the link was made, so a script that read the
        # LINK's own mtime (now) instead of its target's (T0) would place A.
        e.snapshot(T0)
        self.restored_server(e, start_time=int(time.time()) + 3600)
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_x_untouched(e)
        self.assertIn('could not re-apply parked state to stash:1 (from work, "Tab A")', e.log())

    def test_save_time_merge_does_not_carry_it_either(self):
        e = self.env
        e.sidecar.write_text(self.a_row())
        e.snapshot(T0)
        self.restored_server(e)
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Tab A", e.sidecar.read_text() if e.sidecar.exists() else "")
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_x_untouched(e)

    def test_a_save_after_the_restart_does_not_unlock_it(self):
        """`last` moves on with every save of THIS server; the restored one predates it."""
        e = self.env
        e.sidecar.write_text(self.a_row())
        e.snapshot(T0)
        e.snapshot(T2 + 900)          # continuum, 15 minutes into the new server
        self.restored_server(e)
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_x_untouched(e)

    def test_a_park_that_predates_the_snapshot_is_still_placed(self):
        """The test is the park time, not a blanket refusal."""
        e = self.env
        e.sidecar.write_text(self.a_row(ts=str(T0 - 60)))
        e.snapshot(T0)
        e.snapshot(T2 + 900)
        self.restored_server(e)
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        o = e.opts("@30")
        self.assertEqual(o.get("@stash_origin"), "work")
        self.assertEqual(o.get("@stash_label"), "Tab A")
        self.assertEqual(o.get("@stash_ts"), str(T0 - 60))

    def test_a_row_without_a_park_time_is_not_placed_against_a_snapshot(self):
        """Nothing shows it predates the snapshot, so it is refused."""
        e = self.env
        e.sidecar.write_text(self.a_row(ts=""))
        e.snapshot(T0)
        self.restored_server(e)
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_x_untouched(e)

    def test_the_capture_time_is_the_name_not_the_mtime(self):
        """resurrect-save-repair.py os.replace()s the file seconds after the capture."""
        e = self.env
        e.sidecar.write_text(self.a_row(ts=str(T0 + 5)))      # parked just after the capture
        e.snapshot(T0, mtime=T0 + 20)                          # file rewritten by the hook
        self.restored_server(e)
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_x_untouched(e)
        self.assertEqual(e.server()["globals"].get("@stash_snapshot_ts"), str(T0))

    def test_the_restored_snapshot_is_recorded_and_beats_a_guard_rejected_save(self):
        """L2: a rejected save R is newer than the good G that `last` was reverted to."""
        e = self.env
        G, R = T0 - 900, T0
        e.sidecar.write_text(self.a_row(ts=str(G + 300)))      # parked between G and R
        e.snapshot(R)                                          # the rejected shrink save...
        e.snapshot(G)                                          # ...and `last` reverted to G
        self.restored_server(e)
        r = e.run("restore-state")                             # the post-restore hook
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(e.server()["globals"].get("@stash_snapshot_ts"), str(G))
        self.assert_x_untouched(e)
        # Continuum moves `last` past the server start; inference alone would
        # now pick R and let the save-time merge carry A's row onto X.
        e.snapshot(T2 + 900)
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Tab A", e.sidecar.read_text() if e.sidecar.exists() else "")

    def test_a_rerun_of_restore_state_never_moves_the_recorded_time_later(self):
        e = self.env
        e.snapshot(T0)
        self.restored_server(e)
        e.run("restore-state")
        e.snapshot(T2 + 900)
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(e.server()["globals"].get("@stash_snapshot_ts"), str(T0))


class PublishCost(unittest.TestCase):
    """Every publish ran resolve_parked_rows' per-row work under the lock."""

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def test_steady_state_publish_does_no_per_row_work(self):
        e = self.env
        sids = ["aa3db67d-0000-0000-0000-000000000001", "bcf8d86e-0000-0000-0000-000000000002",
                "4664ef27-0000-0000-0000-000000000003"]
        side = ""
        windows = {"@9": win("main", 1, "zsh", "/Users/u", AUTO)}
        for i, (wid, sid) in enumerate(zip(["@83", "@84", "@85"], sids), 1):
            side += row("stash", str(i), "zsh", "0", "main", "Tab %d" % i, sid, "/Users/u", "179080000%d" % i, "/Users/u")
            windows[wid] = win("stash", i, "zsh", "/Users/u",
                               {**AUTO, "@stash_origin": "main", "@stash_label": "Tab %d" % i,
                                "@stash_session": sid, "@stash_cwd": "/Users/u",
                                "@stash_pane_idx": "0", "@stash_ts": "179080000%d" % i})
        side += row("stash", "4", "claude", "", "main", "Tab 4", "", "", "1790800004", "/Users/u")
        windows["@86"] = win("stash", 4, "claude", "/Users/u",
                             {**AUTO, "@stash_origin": "main", "@stash_label": "Tab 4",
                              "@stash_ts": "1790800004"})
        e.sidecar.write_text(side)
        e.snapshot(T0)
        e.set_server(windows, start_time=T2)
        before = e.sidecar.read_text()

        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = e.calls()
        verbs = [c[0] for c in calls]
        # window_for_row is has-session + list-windows + list-panes +
        # display-message per row; the pool is a list-panes per window; the
        # snapshot time is a display-message. None of it may run.
        self.assertNotIn("list-panes", verbs, calls)
        self.assertNotIn("display-message", verbs, calls)
        self.assertEqual(verbs.count("has-session"), 1, calls)    # count()'s, nothing else
        self.assertLessEqual(len(calls), 7, calls)
        # ...and the mirror is unchanged.
        self.assertEqual(sorted(e.sidecar.read_text().splitlines()), sorted(before.splitlines()))


if __name__ == "__main__":
    unittest.main()

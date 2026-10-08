"""stash.sh: @stash_ts (the park time) survives a tmux-resurrect restore.

The sidecar (<@resurrect-dir>/stash-state.tsv) mirrors parked windows' options
because resurrect does not save window options. @stash_ts was missing from it,
so a restored parked tab had no park time and the roster showed a blank age.

Isolated HOME and TMPDIR, a fake `tmux` on PATH whose state is a JSON file, and
@resurrect-dir pointed into the temp dir. No real tmux server, sidecar, lock or
log is touched. STASH_SELF is /usr/bin/true so the detached `describe` the park
spawns is a no-op. Run with unittest.
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

# Just enough of tmux for stash.sh's park, publish and restore-state paths.
FAKE_TMUX = r'''#!/usr/bin/env python3
import fcntl, json, os, re, sys
from pathlib import Path
p = Path(os.environ["FAKE_TMUX_STATE"])

def arg(c, f):
    return c[c.index(f) + 1] if f in c else None

def has_flag(c, letter):
    return any(x.startswith("-") and not x.startswith("--") and letter in x[1:] for x in c[1:])

def render(fmt, v):
    return re.sub(r"#\{([@\w]+)\}", lambda m: str(v.get(m.group(1), "")), fmt)

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
    """-> (window id, pane dict or None) for a window id or a pane id."""
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
        if has_flag(c, "p"):
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


def win(session, index, name, cwd, opts=None):
    return {"session": session, "index": index, "name": name, "opts": dict(opts or {}),
            "panes": [{"index": 0, "id": "%" + str(index + 100 * len(session)), "cwd": cwd, "pid": ""}]}


class FakeEnv:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="stash-ts-"))
        self.home = self.dir / "home"; self.tmp = self.dir / "tmp"; self.bin = self.dir / "bin"
        self.rdir = self.dir / "resurrect"
        for d in (self.home, self.tmp, self.bin, self.rdir):
            d.mkdir()
        f = self.bin / "tmux"; f.write_text(FAKE_TMUX); f.chmod(0o755)
        self.state = self.dir / "tmux.json"
        self.sidecar = self.rdir / "stash-state.tsv"

    def set_server(self, windows):
        """A fresh server: these windows, no globals but @resurrect-dir."""
        n = max([int(w[1:]) for w in windows] or [0]) + 100
        self.state.write_text(json.dumps({
            "windows": windows, "globals": {"@resurrect-dir": str(self.rdir)},
            "calls": [], "messages": [], "next": n}))

    def server(self):
        return json.loads(self.state.read_text())

    def opts(self, wid):
        return self.server()["windows"][wid].get("opts", {})

    def run(self, *args):
        env = {"HOME": str(self.home), "TMPDIR": str(self.tmp) + "/",
               "PATH": f"{self.bin}:{BASH_DIR}:/usr/bin:/bin",
               "FAKE_TMUX_STATE": str(self.state), "STASH_SELF": "/usr/bin/true"}
        r = subprocess.run(["bash", str(STASH), *args], env=env,
                           capture_output=True, text=True, timeout=60)
        unhandled = [l for l in r.stderr.splitlines() if "fake tmux: unhandled" in l]
        assert not unhandled, unhandled
        return r

    def rows(self):
        return [l.split(SEP) for l in self.sidecar.read_text().splitlines() if l]

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class StashTsSidecar(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def resurrect(self, windows):
        """What a resurrect restore leaves: same layout, no window options."""
        self.env.set_server(windows)

    def test_park_save_restore_round_trip(self):
        e = self.env
        e.set_server({
            "@1": win("main", 1, "editor", "/work/a", {"@agent_summary": "Fix the parser"}),
            "@2": win("main", 2, "shell", "/work/b"),
        })
        before = int(time.time())
        r = e.run("stash-many", "@1")
        after = int(time.time())
        self.assertEqual(r.returncode, 0, r.stderr)

        o = e.opts("@1")
        self.assertEqual(e.server()["windows"]["@1"]["session"], "stash")
        self.assertTrue(o.get("@stash_ts", "").isdigit(), o)
        ts = o["@stash_ts"]
        self.assertTrue(before <= int(ts) <= after)

        rows = [r for r in e.rows() if r[0] == "stash"]
        self.assertEqual(len(rows), 1, e.sidecar.read_text())
        row = rows[0]
        self.assertEqual(len(row), 9, row)
        self.assertEqual(row[4], "main")             # origin
        self.assertEqual(row[5], "Fix the parser")   # label
        self.assertEqual(row[8], ts)                 # the new field, appended last
        idx = int(row[1])

        # Restart: resurrect rebuilds the parked window (name and cwd restored)
        # but none of its @stash_* options.
        self.resurrect({
            "@7": win("main", 1, "shell", "/work/b"),
            "@8": win("stash", idx, "editor", "/work/a"),
        })
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        o = e.opts("@8")
        self.assertEqual(o.get("@stash_ts"), ts)
        self.assertEqual(o.get("@stash_origin"), "main")
        self.assertEqual(o.get("@stash_label"), "Fix the parser")
        self.assertEqual(e.server()["globals"].get("@stash_count"), "1")

    def test_old_format_row_restores_everything_else(self):
        e = self.env
        sid = "11111111-2222-3333-4444-555555555555"
        e.sidecar.write_text(SEP.join(
            ["stash", "1", "2.1.241", "0", "main", "Old label", sid, "/work/old"]) + "\n")
        e.set_server({"@3": win("stash", 1, "2.1.241", "/work/old")})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        o = e.opts("@3")
        self.assertEqual(o.get("@stash_label"), "Old label")
        self.assertEqual(o.get("@stash_session"), sid)
        self.assertEqual(o.get("@stash_cwd"), "/work/old")   # no stray separator glued on
        self.assertEqual(o.get("@stash_origin"), "main")
        self.assertEqual(o.get("@stash_pane_idx"), "0")
        self.assertNotIn("@stash_ts", o)

    def test_old_format_row_carried_forward_keeps_cwd_clean(self):
        # save_state's merge reads the OLD file: an 8-field row for a window that
        # exists but has no options yet is carried forward, now as 9 fields.
        e = self.env
        sid = "aaaaaaaa-2222-3333-4444-555555555555"
        e.sidecar.write_text(SEP.join(
            ["stash", "1", "2.1.241", "0", "main", "Old label", sid, "/work/old"]) + "\n")
        e.set_server({"@3": win("stash", 1, "2.1.241", "/work/old"),
                      "@4": win("main", 1, "zsh", "/work/b")})
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [r for r in e.rows() if len(r) > 6 and r[6] == sid]
        self.assertEqual(len(rows), 1, e.sidecar.read_text())
        self.assertEqual(rows[0][7], "/work/old")
        self.assertEqual(rows[0][8:], [""])

    def test_non_digit_ts_is_not_applied(self):
        e = self.env
        e.sidecar.write_text(
            SEP.join(["stash", "1", "editor", "", "main", "Bad ts", "", "", "12x4"]) + "\n"
            + SEP.join(["stash", "2", "other", "", "main", "Neg ts", "", "", "-5"]) + "\n")
        e.set_server({"@3": win("stash", 1, "editor", "/w"),
                      "@4": win("stash", 2, "other", "/w")})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        for wid, label in (("@3", "Bad ts"), ("@4", "Neg ts")):
            o = e.opts(wid)
            self.assertEqual(o.get("@stash_label"), label)
            self.assertNotIn("@stash_ts", o)


if __name__ == "__main__":
    unittest.main()

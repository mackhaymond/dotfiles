"""External subagents' family lifecycle (plan §2.5, U10; docs/spaces.md
"Closing a parent: its subagents go together"):

- closed-tabs.sh `close-family` closes a parent and every subagent under it
  as ONE ⌘Z group (group / group_ord on the entries), ⌘Z brings the group
  back parent first, and a snooze / unpin of one entry takes the group;
- a closing child tells its parent through `agent-spawn _notify-closed`
  (--busy/--idle, --result, never with --parent-closing in a family close),
  and lone closes record parent_sid / child_sids from the registry;
- prefix x and C-l on a parent (tmux.conf.tmpl's bindings, sourced as they
  are) raise the three-way box, driven with real keys on a nested client;
- stash.sh parks a parent with its family, children first, as one group,
  and a working child keeps the whole family; a child alone parks plain;
- space.sh move-to moves the family, space.sh close closes it as groups.

Every server is PRIVATE: `-L fam-<uuid>`, `-f /dev/null`, its own short
TMUX_TMPDIR, TMUX unset; HOME, XDG_STATE_HOME and TMPDIR are a temp dir.
agent-spawn is a recorder (AGENT_SPAWN_BIN), its registry a temp dir
(AGENT_SPAWN_DIR). No harness is launched: the "agents" are sleep copies.
Teardown kills the server. Run with unittest or pytest.
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

from test_stash_freeze import Base as StashBase, STASH

HERE = Path(__file__).resolve().parents[1]
CLOSED = HERE / "scripts" / "executable_closed-tabs.sh"
SPACE = HERE / "scripts" / "executable_space.sh"
CONF = HERE / "tmux.conf.tmpl"
SPACES = HERE / "spaces.conf"
TMUX = shutil.which("tmux")
LIFE = "sleep 600"


def conf_line(path, prefix):
    lines = [l for l in Path(path).read_text().splitlines() if l.startswith(prefix)]
    return lines


@unittest.skipUnless(TMUX and shutil.which("jq"), "tmux / jq not installed")
class Family(unittest.TestCase):
    """closed-tabs.sh on a private server with a nested client (the box is
    read off its screen, keys reach it through the host pane)."""

    def setUp(self):
        self.sock = "fam-" + uuid.uuid4().hex[:12]
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fam-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sockdir = tempfile.mkdtemp(prefix="fam-", dir="/private/tmp")
        self.addCleanup(shutil.rmtree, sockdir, True)
        bin_ = Path(self.tmp, "bin")
        bin_.mkdir()
        (bin_ / "ai-exec").write_text("#!/bin/sh\nexit 1\n")       # describe never leaves
        (bin_ / "ai-exec").chmod(0o755)
        # The scripts where the bindings look for them (#{HOME}/.config/…).
        scripts = Path(self.tmp, ".config/tmux/scripts")
        scripts.mkdir(parents=True)
        for name, real in (("closed-tabs.sh", CLOSED), ("space.sh", SPACE)):
            (scripts / name).write_text(f'#!/bin/sh\nexec bash "{real}" "$@"\n')
            (scripts / name).chmod(0o755)
        # agent-spawn: a recorder.
        self.spawn_log = Path(self.tmp, "spawn.log")
        spawn = bin_ / "agent-spawn"
        spawn.write_text(f'#!/bin/sh\necho "$*" >> "{self.spawn_log}"\n')
        spawn.chmod(0o755)
        self.reg = Path(self.tmp, "registry")
        self.reg.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(TERM="xterm-256color", HOME=self.tmp, TMPDIR=self.tmp + "/", TMUX_TMPDIR=sockdir,
                        XDG_STATE_HOME=os.path.join(self.tmp, "state"), AGENT_FOCUS_NOTICE="",
                        NOTIFY_SH=os.path.join(self.tmp, "no-notify"), NOTIFY_TMUX="true",
                        AGENT_SPAWN_BIN=str(spawn), AGENT_SPAWN_DIR=str(self.reg),
                        SPACE_TMUX_BIN="tmux", SHELL="/bin/sh",
                        PATH=f"{bin_}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        self.addCleanup(self.reap)
        self.procs = []
        self.t("new-session", "-d", "-s", "main", "-x", "120", "-y", "38", LIFE)
        self.path = self.t("display", "-p", "#{socket_path}")
        for opt, val in (("exit-empty", "off"), ("default-shell", "/bin/sh"), ("@agent_kids_any", "1"),
                         ("renumber-windows", "on")):           # as tmux.conf.tmpl
            self.t("set", "-g", opt, val)
        self.t("set-environment", "-g", "AGENT_SPAWN_BIN", str(spawn))
        self.t("set-environment", "-g", "AGENT_SPAWN_DIR", str(self.reg))
        self.home = self.t("display", "-p", "-t", "=main:", "#{window_id}")
        self.t("set", "-w", "-t", self.home, "@space_home", "1")

    # -- harness -----------------------------------------------------------
    def t(self, *args, check=True):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if check:
            self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "kill-server"], env=self.env,
                       capture_output=True, timeout=10)

    def wait(self, f, want=None, timeout=6.0):
        end = time.time() + timeout
        while True:
            v = f()
            if (v == want) if want is not None else v:
                return v
            if time.time() > end:
                self.fail(f"timed out: last {v!r}, want {want!r}")
            time.sleep(0.05)

    def script_env(self, **kw):
        return dict(self.env, TMUX=f"{self.path},0,0", **kw)

    def run_script(self, *args, script=CLOSED, **kw):
        r = subprocess.run(["bash", str(script), *args], env=self.script_env(**kw), capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def tab(self, name, session="main"):
        d = Path(self.tmp, "cwd", name)
        d.mkdir(parents=True, exist_ok=True)
        return self.t("new-window", "-d", "-t", f"={session}:", "-c", str(d), "-P", "-F", "#{window_id}", LIFE)

    def link(self, kid, parent, spawn, state="idle"):
        for opt, val in (("@agent_state", state), ("@agent_parent", parent), ("@agent_child", parent),
                         ("@agent_spawn_id", spawn)):
            self.t("set", "-w", "-t", kid, opt, val)
        meta = {"id": spawn, "parent": {"session_id": "sid-parent"},
                "child": {"session_id": "sid-" + spawn}, "turns": [{"n": 1, "parent_session_id": "sid-parent"}]}
        Path(self.reg, spawn).mkdir(exist_ok=True)
        Path(self.reg, spawn, "meta.json").write_text(json.dumps(meta))

    def family(self, session="main"):
        """A parent with two children (k1 has a grandchild g) and a tab after them."""
        p = self.tab("p", session)
        k1, g, k2 = self.tab("k1", session), self.tab("g", session), self.tab("k2", session)
        after = self.tab("after", session)
        for w in (p,):
            self.t("set", "-w", "-t", w, "@agent_state", "idle")
            self.t("set", "-w", "-t", w, "@agent_kids", "3 0")
            self.t("set", "-w", "-t", w, "@agent_kids_sub", "3 0")
        self.link(k1, p, "s-k1")
        self.link(k2, p, "s-k2")
        self.link(g, k1, "s-g")
        self.t("set", "-w", "-t", k1, "@agent_kids_sub", "1 0")
        return p, k1, g, k2, after

    def pane(self, w):
        return self.t("display", "-p", "-t", w, "#{pane_id}")

    def history(self):
        f = Path(self.tmp, "state", "tmux-closed-tabs", "history.jsonl")
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def windows(self, session="main"):
        return self.t("list-windows", "-t", f"={session}", "-F", "#{window_id}").split()

    def cwds(self, session="main"):
        return [os.path.basename(p) for p in
                self.t("list-windows", "-t", f"={session}", "-F", "#{pane_current_path}").split("\n")]

    def spawn_calls(self):
        return self.spawn_log.read_text().splitlines() if self.spawn_log.exists() else []

    def attach(self):
        attach = f"env -u TMUX -u TMUX_PANE tmux -L {self.sock} -f /dev/null attach -t main"
        self.t("new-session", "-d", "-s", "host", "-x", "120", "-y", "40", attach)
        return self.wait(lambda: self.t("list-clients", "-F", "#{client_name}"))

    def screen(self):
        return self.t("capture-pane", "-p", "-t", "host:")

    def key(self, *k):
        self.t("send-keys", "-t", "host:", *k)

    def source_bindings(self):
        """tmux.conf.tmpl's x and C-l exactly as written, checked with
        source-file -n first, then loaded (prefix stays C-b)."""
        lines = conf_line(CONF, "bind x ") + conf_line(CONF, "bind C-l ") + conf_line(CONF, "set -g @close_title ")
        self.assertEqual(len(lines), 3, lines)
        f = Path(self.tmp, "binds.conf")
        f.write_text("\n".join(lines) + "\n")
        self.t("source-file", "-n", str(f))
        self.t("source-file", str(f))
        self.t("set", "-g", "@popup_x", "C")

    # -- close-family --------------------------------------------------------
    def test_close_family_is_one_group_and_cmd_z_brings_it_back_parent_first(self):
        p, k1, g, k2, after = self.family()
        self.run_script("close-family", self.pane(p))
        left = self.windows()
        for w in (p, k1, g, k2):
            self.assertNotIn(w, left)
        self.assertIn(after, left)
        h = self.wait(lambda: len(self.history()) == 4 and self.history())
        groups = {e["group"] for e in h}
        self.assertEqual(len(groups), 1, h)
        by = {os.path.basename(e["cwd"]): e for e in h}
        self.assertEqual([by[n]["group_ord"] for n in ("p", "k1", "g", "k2")], [0, 1, 2, 3])
        self.assertEqual(by["k1"]["parent_window"], p)
        self.assertEqual(by["g"]["parent_window"], k1)            # named, not read after k1 went
        self.assertEqual(by["k2"]["spawn_id"], "s-k2")
        self.assertNotIn("parent_window", by["p"])
        # The children are closed with their parent: nobody is told.
        calls = self.wait(lambda: len(self.spawn_calls()) == 3 and self.spawn_calls())
        self.assertTrue(all(c.startswith("_notify-closed s-") and c.endswith("--parent-closing") for c in calls), calls)
        # A snooze of one child takes the group, an unpin of the parent drops it.
        ids = {n: by[n]["id"] for n in by}
        self.run_script("snooze", str(int(time.time()) + 3600), ids["k2"], SNOOZE_SH="/nonexistent")
        self.assertTrue(all(e.get("pinned") and e.get("wake_at") for e in self.history()), self.history())
        self.run_script("unpin", ids["p"], STASH_SH="/nonexistent", SNOOZE_SH="/nonexistent")
        self.assertFalse(any(e.get("pinned") or e.get("wake_at") for e in self.history()), self.history())
        # ⌘Z (the newest backlog entry) brings the whole group, parent first,
        # each child right after the one before.
        self.run_script("reopen", "main", "")
        self.wait(lambda: len(self.windows()), 6)
        self.assertEqual(self.history(), [])
        self.wait(lambda: self.cwds()[1:], ["p", "k1", "g", "k2", "after"])

    def test_family_link_fills_parent_and_child_sids_from_the_group(self):
        hist = Path(self.tmp, "state", "tmux-closed-tabs", "history.jsonl")
        hist.parent.mkdir(parents=True)
        rows = [{"id": "1", "ts": 1, "group": "g1", "group_ord": 1, "window": "@2", "parent_window": "@1", "session_id": "B"},
                {"id": "2", "ts": 1, "group": "g1", "group_ord": 2, "window": "@3", "parent_window": "@2", "session_id": "C"},
                {"id": "3", "ts": 2, "group": "g1", "group_ord": 0, "window": "@1", "session_id": "A"},
                {"id": "4", "ts": 2, "window": "@9", "parent_window": "@1", "session_id": "Z"}]
        hist.write_text("".join(json.dumps(r) + "\n" for r in rows))
        r = subprocess.run(["bash", "-c", 'source "$1" pinned-json >/dev/null; family_link g1', "_", str(CLOSED)],
                           env=self.script_env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        h = {e["id"]: e for e in self.history()}
        self.assertEqual(h["3"]["child_sids"], ["B"])
        self.assertEqual((h["1"]["parent_sid"], h["1"]["child_sids"]), ("A", ["C"]))
        self.assertEqual(h["2"]["parent_sid"], "B")
        self.assertNotIn("parent_sid", h["4"])                    # not in the group

    def test_lone_closes_tell_the_parent_and_record_the_registry_link(self):
        p, k1, g, k2, after = self.family()
        self.t("set", "-w", "-t", k2, "@agent_state", "running")
        self.t("set", "-w", "-t", k2, "@agent_spawn_result", "ready")
        self.run_script("close", self.pane(k2))
        self.run_script("close", self.pane(g))
        calls = self.wait(lambda: len(self.spawn_calls()) == 2 and sorted(self.spawn_calls()))
        self.assertEqual(calls, ["_notify-closed s-g --idle", "_notify-closed s-k2 --busy --result ready"])
        by = {os.path.basename(e["cwd"]): e for e in self.history()}
        self.assertEqual((by["k2"]["parent_sid"], by["k2"]["parent_window"]), ("sid-parent", p))
        self.assertNotIn("group", by["k2"])
        # The parent alone ("keep them"): its children's sids from the registry.
        self.run_script("close", self.pane(p))
        by = {os.path.basename(e["cwd"]): e for e in self.history()}
        self.assertEqual(by["p"]["child_sids"], ["sid-s-k1"])     # k2 is gone already
        self.assertIn(k1, self.windows())                         # kept: an orphan now
        time.sleep(0.3)
        self.assertEqual(len(self.spawn_calls()), 2)              # the parent is nobody's child

    # -- the close box, real keys -------------------------------------------------
    def test_prefix_x_on_a_parent_asks_close_keep_cancel_and_y_closes_the_family(self):
        self.source_bindings()
        p, k1, g, k2, after = self.family()
        self.attach()
        self.t("select-window", "-t", p)
        self.key("C-b", "x")
        box = self.wait(lambda: "Close + 3 children" in self.screen() and self.screen())
        self.box_text = box
        print("\n--- prefix x on a parent ---\n" + "\n".join(l for l in box.splitlines() if l.strip()))
        self.assertIn("Keep them (they become orphans)", box)
        self.assertIn("Cancel", box)
        self.assertRegex(box, r"close .*\+ 3 children\?")
        self.key("Escape")                                        # cancel: nothing closes
        self.wait(lambda: "Close + 3" not in self.screen(), True)
        time.sleep(0.3)
        self.assertIn(p, self.windows())
        self.key("C-b", "x")
        self.wait(lambda: "Close + 3 children" in self.screen(), True)
        self.key("y")
        self.wait(lambda: any(w in self.windows() for w in (p, k1, g, k2)), False)
        self.wait(lambda: len(self.history()), 4)
        self.assertEqual(len({e.get("group") for e in self.history()}), 1)

    def test_c_l_on_a_parent_asks_too_and_k_closes_the_parent_alone(self):
        self.source_bindings()
        p, k1, g, k2, after = self.family()
        self.attach()
        self.t("select-window", "-t", p)
        self.key("C-b", "C-l")
        self.wait(lambda: "Keep them (they become orphans)" in self.screen(), True)
        self.key("k")
        self.wait(lambda: p in self.windows(), False)
        time.sleep(0.3)
        for w in (k1, g, k2):
            self.assertIn(w, self.windows())
        h = self.history()
        self.assertEqual(len(h), 1)
        self.assertNotIn("group", h[0])
        # A plain tab: C-l closes at once, x shows the plain y/n.
        self.t("select-window", "-t", after)
        self.key("C-b", "x")
        self.wait(lambda: "close tab" in self.screen(), True)
        self.assertNotIn("children", self.screen())
        self.key("Escape")

    def test_spaces_conf_bindings_parse(self):
        on = [l for l in SPACES.read_text().splitlines() if l.startswith(("bind x ", "bind C-l ", "set -g @space_n_kids_busy", "set -g @space_close_prompt"))]
        self.assertEqual(len(on), 6, on)                          # on-branch x, C-l; the off copies; the prompt
        f = Path(self.tmp, "spaces-binds.conf")
        f.write_text("\n".join(on) + "\n")
        self.t("source-file", "-n", str(f))
        self.t("source-file", str(f))
        for opt, val in (("@space_n_agents", "#{n:#{W:#{?#{@agent_state},x,}}}"),
                         ("@space_n_shells", "#{n:#{W:#{?#{@agent_state},,x}}}")):
            self.t("set", "-g", opt, val)
        prompt = lambda: self.t("display", "-p", "-t", "=main:", "#{E:@space_close_prompt}")
        self.assertNotIn("subagent", prompt())
        p, k1, g, k2, after = self.family()
        self.t("set", "-w", "-t", k1, "@agent_state", "running")
        self.t("set", "-w", "-t", g, "@agent_state", "needs-input")
        self.assertTrue(prompt().endswith(" · 2 subagents still working?"), prompt())

    # -- space.sh -------------------------------------------------------------------
    def test_move_to_takes_the_family_along_in_order(self):
        p, k1, g, k2, after = self.family()
        self.t("new-session", "-d", "-s", "dst", LIFE)
        dst = self.t("display", "-p", "-t", "=dst:", "#{session_id}")
        self.run_script("move-to", p, dst, "", script=SPACE)
        self.assertEqual(self.windows("dst")[1:], [p, k1, g, k2])
        self.assertIn(after, self.windows())

    def test_space_close_closes_each_family_as_a_group_with_its_parent(self):
        self.t("set", "-g", "@spaces", "on")
        self.t("new-session", "-d", "-s", "proj", LIFE)
        self.t("set", "-w", "-t", "=proj:", "@space_home", "1")
        p, k1, g, k2, after = self.family("proj")
        self.t("set", "-w", "-t", k2, "@agent_state", "running")
        sid = self.t("display", "-p", "-t", "=proj:", "#{session_id}")
        self.run_script("close", sid, "", script=SPACE)
        self.assertNotIn("proj", self.t("list-sessions", "-F", "#{session_name}").split())
        h = self.wait(lambda: len(self.history()) == 6 and self.history())
        by = {os.path.basename(e["cwd"]): e for e in h}
        fam = {by[n].get("group") for n in ("p", "k1", "g", "k2")}
        self.assertEqual(len(fam), 1)
        group = fam.pop()
        self.assertIsNotNone(group)
        self.assertNotIn("group", by["after"])
        # Tree order (family_rows'), not depth: ⌘Z replays p, k1, g, k2.
        self.assertEqual([by[n]["group_ord"] for n in ("p", "k1", "g", "k2")], [0, 1, 2, 3])
        log = Path(self.tmp, "state", "tmux-closed-tabs", "log").read_text()
        self.assertIn(f"family-link {group}", log)                 # its sids, after every closer
        calls = self.wait(lambda: len(self.spawn_calls()) == 3 and sorted(self.spawn_calls()))
        self.assertTrue(all(c.endswith("--parent-closing") for c in calls), calls)
        self.assertIn("_notify-closed s-k2 --busy --parent-closing", calls)


# stash.sh against a closed-tabs.sh stand-in: `family` and `family-link` are
# the real ones, `close --pin …` is recorded whole, kills the pane and prints
# an id — or, with a refuse-<pane> file, refuses as close --pin does (exit 3).
FAMILY_STUB = r'''#!/bin/bash
case "$1" in
    family|family-link) exec bash "@CLOSED@" "$@" ;;
    pinned-json) return 0 2>/dev/null; exit 0 ;;   # sourced by pin_dry_why: no functions, no verdict
esac
here="$(dirname "$0")"
echo "$*" >> "$here/closed-calls"
if [ "$1" = close ]; then
    pane="${@: -1}"
    if [ -e "$here/refuse-$pane" ]; then
        echo "closed-tabs: not stashed — no agent in this pane" >&2; exit 3
    fi
    tmux kill-pane -t "$pane"
    echo "1700000000-$RANDOM"
fi
exit 0
'''


class StashFamily(StashBase):
    def setUp(self):
        super().setUp()
        self.stub.write_text(FAMILY_STUB.replace("@CLOSED@", str(CLOSED)))
        self.t("set", "-g", "@agent_kids_any", "1")

    def fake_agent(self, w):
        f = Path(self.tmp, "bin", "codex")
        if not f.exists():
            f.parent.mkdir(exist_ok=True)
            shutil.copy("/bin/sleep", f)
        self.run_job(self.pane(w), f"'{f}' 1000")

    def family(self):
        self.keep_one()
        p, k1, k2 = self.new_window(), self.new_window(), self.new_window()
        for w in (p, k1, k2):
            self.fake_agent(w)
        for k, s in ((k1, "s-k1"), (k2, "s-k2")):
            for opt, val in (("@agent_parent", p), ("@agent_child", p), ("@agent_spawn_id", s)):
                self.t("set", "-w", "-t", k, opt, val)
        self.t("set", "-w", "-t", p, "@agent_kids", "2 0")
        return p, k1, k2

    def closed_calls(self):
        f = Path(self.tmp, "closed-calls")
        return f.read_text().splitlines() if f.exists() else []

    def test_a_parent_parks_with_its_family_children_first_as_one_group(self):
        p, k1, k2 = self.family()
        pp, pk1, pk2 = self.pane(p), self.pane(k1), self.pane(k2)
        self.script("stash", p)
        calls = self.closed_calls()
        self.assertEqual([c.split()[-1] for c in calls], [pk2, pk1, pp])
        groups = {re.search(r"--group (\S+)", c).group(1) for c in calls}
        self.assertEqual(len(groups), 1, calls)
        self.assertIn(f"--group-ord 2 --parent-window {p} --parent-closing {pk2}", calls[0])
        self.assertIn(f"--group-ord 1 --parent-window {p} --parent-closing {pk1}", calls[1])
        self.assertTrue(calls[2].endswith(f"--group-ord 0 {pp}"), calls[2])
        closed_log = Path(self.tmp, "state", "tmux-closed-tabs", "log")
        self.assertIn(f"family-link {groups.pop()}", closed_log.read_text())

    def test_a_child_whose_pin_is_refused_keeps_the_whole_family(self):
        p, k1, k2 = self.family()
        pk2 = self.pane(k2)
        Path(self.tmp, f"refuse-{pk2}").touch()                   # close --pin refuses k2 (asked first)
        self.script("stash", p)
        calls = self.closed_calls()
        self.assertEqual(len(calls), 1, calls)                    # k2 tried; k1 and p never
        self.assertTrue(calls[0].endswith(pk2), calls)
        for w in (p, k1, k2):
            self.assertEqual(self.session_of(w), "main")
        self.assertIn(f"refused to hide {k2} — no agent in this pane; its family stays", self.log())
        self.assertIn(f"kept {k1} — its family stays", self.log())

    def test_a_child_with_no_agent_left_keeps_the_family_and_is_named(self):
        self.keep_one()
        p, k1 = self.new_window(), self.new_window()
        self.fake_agent(p)
        self.run_job(self.pane(k1), "sleep 1000")                 # a shell job: would park, not pin
        for opt, val in (("@agent_parent", p), ("@agent_child", p), ("@agent_spawn_id", "s-k1"),
                         ("@agent_spawn_task", "Docs pass")):
            self.t("set", "-w", "-t", k1, opt, val)
        self.script("stash", p)
        self.assertEqual(self.closed_calls(), [])
        self.assertEqual((self.session_of(p), self.session_of(k1)), ("main", "main"))
        self.assertIn(f"refused to hide {p} and its subagents — its subagent: “Docs pass” has no agent left",
                      self.log())

    def test_a_working_child_keeps_the_whole_family(self):
        p, k1, k2 = self.family()
        self.t("set", "-w", "-t", k2, "@agent_state", "running")
        self.script("stash", p)
        self.assertEqual(self.closed_calls(), [])
        for w in (p, k1, k2):
            self.assertEqual(self.session_of(w), "main")
        self.assertIn(f"refused to hide {p} and its subagents — its subagent is still working", self.log())

    def test_a_child_parks_alone_without_the_group(self):
        p, k1, k2 = self.family()
        self.script("stash", k1)
        calls = self.closed_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertNotIn("--group", calls[0])
        self.assertNotIn("--parent-closing", calls[0])
        self.assertEqual(self.session_of(p), "main")


if __name__ == "__main__":
    unittest.main()

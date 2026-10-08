import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).parents[1] / "scripts/executable_resurrect-save-repair.py"
SPEC = importlib.util.spec_from_file_location("save_repair", SCRIPT)
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


class SaveRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.tracker = self.root / "tracker"
        self.tracker.mkdir()
        self.socket = "/tmp/test-tmux-socket"
        self.record = dict(session_id="correct", frontend_pid=123, frontend_start="start",
                           pane="%7", pane_pid=77, tmux_socket=self.socket, tty="ttys7", token="token")
        self.panes = {"%7": dict(target="main:7.1", cwd="/shared", active="1", command="zsh", pid="77")}
        self.claude = dict(pane="stash:1.1", tool="claude", session_id="claude-id", cwd="/old",
                           pid="222", cli_args="--model opus", model="opus", env={"CUSTOM": "with spaces"})
        self.sidecar = json.dumps(dict(timestamp="original-time", sessions=[self.claude,
            dict(pane="main:4.1", tool="codex", session_id="wrong-cwd-guess", cwd="/shared")]))
        self.layout = self.row("main", 7) + self.row("stash", 1)
        self.owner = SimpleNamespace(valid=Mock(return_value=True), process_identity=Mock(return_value=None))
        with sqlite3.connect(self.codex / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads(id TEXT,thread_source TEXT,source TEXT,agent_path TEXT,rollout_path TEXT)")
        self.persist("correct")

    def persist(self, sid, thread_source="user", source="cli", agent_path="", header_sid=None, name=None):
        path = self.codex / (name or f"{sid}.jsonl")
        path.write_text(json.dumps(dict(type="session_meta", payload=dict(id=header_sid or sid))) + "\n")
        with sqlite3.connect(self.codex / "state_5.sqlite") as db:
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?)", (sid, thread_source, source, agent_path, str(path)))

    @staticmethod
    def row(session, window):
        return f"pane\t{session}\t{window}\t0\t:\t1\tTitle\t:/shared\t1\tzsh\t:\n"

    def repaired(self, records=None, command="codex --remote unix:///tmp/cx-tmux-test/s"):
        with patch.object(repair, "run", return_value=command):
            return repair.repair(self.sidecar, self.layout, records or {self.record["session_id"]: self.record},
                                 self.panes, self.socket, self.owner, self.codex, self.tracker)

    def test_wrong_ids_are_replaced_missing_binding_added_and_current_target_used(self):
        self.persist("second")
        second = dict(self.record, session_id="second", frontend_pid=124, pane="%8")
        self.panes["%8"] = dict(self.panes["%7"], target="main:8.1", pid="78")
        self.layout += self.row("main", 8)
        content, layout, targets, report = self.repaired({"correct": self.record, "second": second})
        data = json.loads(content)
        self.assertEqual(data["timestamp"], "original-time")
        self.assertEqual(data["sessions"][0], self.claude)
        self.assertEqual([(s["pane"], s["session_id"]) for s in data["sessions"][1:]],
                         [("main:7.1", "correct"), ("main:8.1", "second")])
        self.assertEqual(targets, {"main:7.1", "main:8.1"})
        self.assertEqual(layout.decode(), self.layout)
        self.assertEqual(report["codex"], 2)
        self.assertTrue(all(s["cli_args"] == "" for s in data["sessions"][1:]))

    def test_one_unrestorable_bound_command_line_never_aborts_the_repair(self):
        # A bound frontend launched with a prompt used to raise "Positional
        # Codex arguments…" and abort the whole repair, so the plugin's rows
        # (stale `-c model_context_window` copies included) went to restore.
        self.persist("second")
        second = dict(self.record, session_id="second", frontend_pid=124, pane="%8")
        self.panes["%8"] = dict(self.panes["%7"], target="main:8.1", pid="78")
        self.layout += self.row("main", 8)
        for second_argv, rows, invalid in (
                # The binding verifies the thread: the prompt is dropped, flags kept.
                ("codex --no-daemon -c model_context_window=272000 -m o3 fix the bug",
                 [("main:7.1", "correct", "--no-daemon"), ("main:8.1", "second", "--no-daemon -m o3")], 0),
                # Not restorable even without the prompt: that row alone is invalid.
                ("codex --no-daemon exec fix it", [("main:7.1", "correct", "--no-daemon")], 1),
                ("codex --no-daemon --model", [("main:7.1", "correct", "--no-daemon")], 1)):
            commands = {"123": "codex --no-daemon -c model_context_window=272000", "124": second_argv}
            with self.subTest(argv=second_argv), patch.object(repair, "run", side_effect=lambda a: commands[a[2]]):
                content, _, _, report = repair.repair(self.sidecar, self.layout, {"correct": self.record,
                    "second": second}, self.panes, self.socket, self.owner, self.codex, self.tracker)
                self.assertEqual([(s["pane"], s["session_id"], s["cli_args"])
                                  for s in json.loads(content)["sessions"][1:]], rows)
                self.assertEqual(report["invalid_bindings"], invalid)

    def test_ephemeral_binding_falls_back_only_to_matching_persisted_pid_tracker(self):
        self.record["session_id"] = "ephemeral"
        state = dict(session_id="correct", ppid=123, frontend_start="start", terminal_binding_id="old",
                     env=dict(tmux_pane="%7", tmux_socket=self.socket))
        (self.tracker / "codex-123.json").write_text(json.dumps(state))
        content, *_ = self.repaired()
        self.assertEqual(json.loads(content)["sessions"][1]["session_id"], "correct")
        for field, value in (("ppid", 999), ("frontend_start", "stale")):
            with self.subTest(field=field):
                (self.tracker / "codex-123.json").write_text(json.dumps(dict(state, **{field: value})))
                content, *_ = self.repaired()
                self.assertEqual(json.loads(content)["sessions"], [self.claude])
        for env in (dict(tmux_pane="%99", tmux_socket=self.socket), dict(tmux_pane="%7", tmux_socket="/wrong")):
            (self.tracker / "codex-123.json").write_text(json.dumps(dict(state, env=env)))
            content, *_ = self.repaired()
            self.assertEqual(json.loads(content)["sessions"], [self.claude])

    def test_explicit_resume_fallback_requires_persisted_root_rollout(self):
        self.record["session_id"] = "ephemeral"
        content, *_ = self.repaired(command="codex resume correct --last --remote unix:///tmp/cx-tmux-a/s")
        self.assertEqual(json.loads(content)["sessions"][1]["session_id"], "correct")
        for sid, options in (("agent", dict(thread_source="agent")), ("nested", dict(agent_path="/root/task")),
                             ("web", dict(source="web")), ("mismatch", dict(header_sid="other"))):
            self.persist(sid, **options)
            content, *_ = self.repaired(command=f"codex resume {sid}")
            self.assertEqual(json.loads(content)["sessions"], [self.claude])
        content, *_ = self.repaired(command="codex fork correct")
        self.assertEqual(json.loads(content)["sessions"], [self.claude])

    def test_stale_invalid_socket_and_unknown_pane_are_never_used(self):
        self.owner.valid.return_value = False
        records = dict(stale=dict(self.record, session_id="stale"), wrong=dict(self.record, session_id="wrong", tmux_socket="/wrong"),
                       absent=dict(self.record, session_id="absent", pane="%99"), malformed=None)
        with patch.object(repair, "run") as command:
            content, _, _, report = repair.repair(self.sidecar, self.layout, records, self.panes,
                                                self.socket, self.owner, self.codex, self.tracker)
        command.assert_not_called()
        self.assertEqual(json.loads(content)["sessions"], [self.claude])
        self.assertEqual(report["invalid_bindings"], 4)

    def test_argument_extraction_preserves_permissions_and_custom_remote(self):
        flags, sid, model = repair.codex_args("node /opt/codex.js --model o3 --sandbox workspace-write -a on-request "
            "--remote=unix:///tmp/cx-tmux-a/s resume correct --last --all --include-non-interactive")
        self.assertEqual(flags, "--model o3 --sandbox workspace-write -a on-request")
        self.assertEqual((sid, model), ("correct", "o3"))
        self.assertEqual(repair.codex_args("/opt/codex --remote unix:///custom/socket --full-auto fork parent")[0],
                         "--remote unix:///custom/socket --full-auto")
        self.assertEqual(repair.codex_args("codex --model resume resume correct")[0], "--model resume")
        for command in ("codex --add-dir '/tmp/with spaces'", "codex --model o3 hello world", "codex -c 'key=value'", "codex --model"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                repair.codex_args(command)

    def test_blank_parked_titles_repaired_without_losing_any_panes(self):
        self.layout = "".join(self.row("main", index) for index in range(170))
        for index in range(1, 4):
            self.layout += f"pane\tstash\t{index}\t0\t:\t1\t:/old\t1\tzsh\t{200 + index}\t:\n"
            self.panes[f"%{200 + index}"] = dict(target=f"stash:{index}.1", cwd=f"/actual/{index}", active="1", command="zsh", pid=str(200 + index))
        content, repaired, _, report = self.repaired()
        rows = repaired.decode().splitlines()
        self.assertEqual(len(rows), 173)
        self.assertEqual(report["repaired_panes"], 3)
        self.assertEqual(rows[:170], self.layout.splitlines()[:170])
        for index, row in enumerate(rows[170:], 1):
            fields = row.split("\t")
            self.assertEqual(len(fields), 11)
            self.assertEqual(fields[6:], [":", f":/actual/{index}", "1", "zsh", ":"])
        self.assertEqual(json.loads(content)["sessions"][0], self.claude)
        self.panes["%201"]["pid"] = "999"
        with self.assertRaises(ValueError):
            self.repaired()

    def test_archive_only_removes_verified_codex_panes(self):
        archive = self.root / "pane_contents.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for name in ("./pane_contents/pane-main:7.1", "pane_contents/pane-stash:1.1", "pane_contents/pane-other:1.1"):
                info = tarfile.TarInfo(name)
                info.size = 6
                output.addfile(info, io.BytesIO(b"screen"))
        content, removed = repair.stripped_archive(archive, {"main:7.1"})
        self.assertEqual(removed, 1)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as output:
            self.assertEqual(output.getnames(), ["pane_contents/pane-stash:1.1", "pane_contents/pane-other:1.1"])
            self.assertEqual(output.extractfile(output.getmembers()[0]).read(), b"screen")

    def test_parked_shell_retains_recorded_cwd_after_restore_to_home(self):
        intended = str(self.root / "parked project")
        Path(intended).mkdir()
        pane = dict(target="stash:1.1", cwd="/home", active="1", command="zsh", pid="201",
                    stash_session="parked-id", stash_pane_idx="1", stash_cwd=intended)
        self.panes["%201"] = pane
        content, layout, _, report = self.repaired()
        self.assertEqual(layout.decode().splitlines()[1].split("\t")[7], ":" + intended)
        self.assertEqual(report["parked_cwds"], 1)
        self.assertEqual(json.loads(content)["sessions"][0], self.claude)
        # The blank-title format still requires the saved shell PID to match.
        self.layout = self.row("main", 7) + "pane\tstash\t1\t0\t:\t1\t:/home\t1\tzsh\t201\t:\r\n"
        _, layout, _, report = self.repaired()
        self.assertTrue(layout.endswith(b"\r\n"))
        self.assertEqual(layout.decode().splitlines()[1].split("\t")[7], ":" + intended)
        self.assertEqual(report["repaired_panes"], 1)
        pane["pid"] = "999"
        with self.assertRaisesRegex(ValueError, "verified against its live shell"):
            self.repaired()

    def test_parked_cwd_never_applies_to_unverified_or_other_panes(self):
        pane = dict(target="stash:1.1", cwd="/home", active="1", command="zsh", pid="201",
                    stash_session="parked-id", stash_pane_idx="1", stash_cwd=str(self.root))
        for changes in ({"stash_pane_idx": "2"}, {"stash_session": ""}, {"command": "claude"},
                        {"stash_cwd": "relative"}, {"stash_cwd": str(self.root / "missing")}):
            with self.subTest(changes=changes):
                self.panes["%201"] = dict(pane, **changes)
                _, layout, _, report = self.repaired()
                self.assertEqual(layout.decode(), self.layout)
                self.assertEqual(report["parked_cwds"], 0)
        # Window options are shared by every split, but only the named pane
        # receives the override even when both panes are idle shells.
        self.panes["%201"] = pane
        self.panes["%202"] = dict(pane, target="stash:1.2", pid="202")
        sibling = self.row("stash", 1).replace("\t1\tTitle", "\t2\tTitle")
        self.layout += sibling
        _, layout, _, report = self.repaired()
        self.assertTrue(layout.decode().endswith(sibling))
        self.assertEqual(report["parked_cwds"], 1)

    def test_snapshot_captures_optional_parked_metadata_from_same_pane(self):
        line = f"%201\tstash:1.1\t/home\t1\tzsh\t201\t:parked-id\t:1\t:{self.root}"
        with patch.object(repair, "run", return_value=line) as command:
            panes = repair.pane_snapshot(self.socket)
        self.assertEqual(panes["%201"]["stash_session"], "parked-id")
        self.assertEqual(panes["%201"]["stash_pane_idx"], "1")
        self.assertEqual(panes["%201"]["stash_cwd"], str(self.root))
        self.assertIn("#{@stash_cwd}", command.call_args.args[0][-1])
        with patch.object(repair, "run", return_value="%7\tmain:7.1\t/shared\t1\tzsh\t77"):
            self.assertEqual(repair.pane_snapshot(self.socket)["%7"]["stash_session"], "")

    def test_dry_run_writes_nothing_and_apply_preserves_layout_symlink(self):
        sidecar, layout = self.root / "assistant-sessions.json", self.root / "save.txt"
        sidecar.write_text(self.sidecar)
        layout.write_text(self.layout)
        layout.chmod(0o640)
        (self.root / "last").symlink_to(layout.name)
        bindings = self.root / "bindings.json"
        bindings.write_text(json.dumps({"correct": self.record}))
        helper = self.root / "owner.py"
        helper.write_text("def valid(record): return True\ndef process_identity(pid): return None\n")
        argv = ["--resurrect-dir", str(self.root), "--bindings", str(bindings), "--socket", self.socket,
                "--owner-helper", str(helper), "--codex-home", str(self.codex), "--tracker-dir", str(self.tracker)]
        snapshot = "%7\tmain:7.1\t/shared\t1\tzsh\t77"
        def command(arguments):
            return snapshot if arguments[0] == "tmux" else "codex --remote unix:///tmp/cx-tmux-xx/s"
        with patch.object(repair, "run", side_effect=command), patch("sys.stdout", new=io.StringIO()):
            repair.main([*argv, "--dry-run"])
            self.assertEqual(sidecar.read_text(), self.sidecar)
            self.assertEqual(layout.read_text(), self.layout)
            repair.main(argv)
        self.assertTrue((self.root / "last").is_symlink())
        self.assertEqual(layout.stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(sidecar.read_text())["sessions"][1]["session_id"], "correct")
        self.assertEqual(list(self.root.glob(".repair-*")), [])

    def assertDropped(self, call, *dropped):
        """`call` completes the repair but drops each unresolved (pid, pane) Codex row,
        naming them all on one stderr line."""
        with patch("sys.stderr", new=io.StringIO()) as err:
            result = call()
        report, kept = result[3], [int(e["pid"]) for e in json.loads(result[0])["sessions"] if e["tool"] == "codex"]
        self.assertEqual(report["unresolved_dropped"], len(dropped))
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        for pid, pane in dropped:
            self.assertNotIn(pid, kept)
            self.assertIn(f"pid {pid} ({pane})", err.getvalue())
        return result

    def test_unresolved_living_saved_owner_is_dropped_without_aborting(self):
        data = json.loads(self.sidecar)
        data["sessions"][1]["pid"] = "123"
        self.sidecar = json.dumps(data)
        self.owner.valid.return_value = False
        self.owner.process_identity.return_value = dict(frontend_pid=123, tty="ttys7")
        content, *_ = self.assertDropped(self.repaired, (123, "main:4.1"))
        self.assertEqual(json.loads(content)["sessions"], [self.claude])
        self.assertEqual(json.loads(self.sidecar), data)
        # A dead saved PID does not justify restoring an unverified session,
        # and is not reported as dropped: it was never living.
        self.owner.process_identity.return_value = None
        with patch("sys.stderr", new=io.StringIO()) as err:
            content, *_, report = self.repaired()
        self.assertEqual(json.loads(content)["sessions"], [self.claude])
        self.assertEqual((report["unresolved_dropped"], err.getvalue()), (0, ""))

    def test_unresolved_codex_is_dropped_while_the_rest_of_the_save_is_repaired(self):
        # One verified binding (main:7.1), one living frontend nothing
        # identifies (main:8.1), and a blank-title shell row in the same save.
        self.persist("second")
        data = json.loads(self.sidecar)
        data["sessions"][1].update(pid="555", pane="main:8.1", session_id="second")
        self.sidecar = json.dumps(data)
        self.panes["%8"] = dict(self.panes["%7"], target="main:8.1", pid="78")
        self.panes["%201"] = dict(target="stash:2.1", cwd="/actual", active="1", command="zsh", pid="201")
        self.layout += self.row("main", 8) + "pane\tstash\t2\t0\t:\t1\t:/old\t1\tzsh\t201\t:\n"
        self.owner.valid = Mock(side_effect=lambda record: record.get("token") == "token")
        self.owner.process_identity.side_effect = lambda pid: dict(frontend_pid=pid, tty="ttys8") if pid == 555 else None
        archive = self.root / "pane_contents.tar.gz"
        content, layout, targets, report = self.assertDropped(
            lambda: self.repaired(command="codex --no-daemon"), (555, "main:8.1"))
        self.assertEqual([(e["pane"], e["session_id"]) for e in json.loads(content)["sessions"]],
                         [("stash:1.1", "claude-id"), ("main:7.1", "correct")])
        self.assertEqual(targets, {"main:7.1"})
        self.assertEqual(report["repaired_panes"], 1)
        self.assertEqual(layout.decode().splitlines()[-1].split("\t")[6:], [":", ":/actual", "1", "zsh", ":"])
        # Only the verified pane's contents are stripped; the dropped pane keeps
        # its screen and restores as a plain shell.
        with tarfile.open(archive, "w:gz") as output:
            for name in ("pane_contents/pane-main:7.1", "pane_contents/pane-main:8.1"):
                info = tarfile.TarInfo(name)
                info.size = 6
                output.addfile(info, io.BytesIO(b"screen"))
        stripped, removed = repair.stripped_archive(archive, targets)
        self.assertEqual(removed, 1)
        with tarfile.open(fileobj=io.BytesIO(stripped), mode="r:gz") as output:
            self.assertEqual(output.getnames(), ["pane_contents/pane-main:8.1"])
        # Corrupt input still aborts the whole repair.
        self.panes["%201"]["pid"] = "999"
        with patch("sys.stderr", new=io.StringIO()), \
                self.assertRaisesRegex(ValueError, "verified against its live shell"):
            self.repaired(command="codex --no-daemon")

    def test_main_reports_dropped_frontends_and_still_writes_the_repair(self):
        sidecar, layout = self.root / "assistant-sessions.json", self.root / "save.txt"
        data = json.loads(self.sidecar)
        data["sessions"][1].update(pid="555", pane="main:7.1")
        sidecar.write_text(json.dumps(data))
        layout.write_text(self.layout)
        (self.root / "last").symlink_to(layout.name)
        helper = self.root / "owner.py"
        helper.write_text("def valid(record): return False\n"
                          "def process_identity(pid): return dict(frontend_pid=pid, tty='ttys7')\n")
        snapshot = "%7\tmain:7.1\t/shared\t1\tzsh\t77"
        def command(arguments):
            return snapshot if arguments[0] == "tmux" and "list-panes" in arguments else "codex"
        with patch.object(repair, "run", side_effect=command), patch("sys.stdout", new=io.StringIO()) as out, \
                patch("sys.stderr", new=io.StringIO()) as err:
            repair.main(["--resurrect-dir", str(self.root), "--bindings", str(self.root / "none.json"),
                         "--socket", self.socket, "--owner-helper", str(helper), "--codex-home", str(self.codex),
                         "--tracker-dir", str(self.tracker)])
        self.assertEqual(json.loads(out.getvalue())["unresolved_dropped"], 1)
        self.assertEqual(err.getvalue(), "resurrect save repair: dropped unresolved living Codex frontends: "
                                         "pid 555 (main:7.1)\n")
        self.assertEqual(json.loads(sidecar.read_text())["sessions"], [self.claude])

    def test_living_standalone_frontend_is_kept_only_on_its_own_resume_argument(self):
        data = json.loads(self.sidecar)
        data["sessions"][1].update(pid="123", pane="main:7.1", session_id="correct")
        self.sidecar = json.dumps(data)
        self.owner.valid.return_value = False  # no relay binding, as under --no-daemon
        self.owner.process_identity.return_value = dict(frontend_pid=123, tty="ttys7")
        content, *_, report = self.repaired(command="node /x/codex --no-daemon -c k=v resume correct")
        codex = [entry for entry in json.loads(content)["sessions"] if entry["tool"] == "codex"]
        self.assertEqual(codex, [dict(pane="main:7.1", tool="codex", session_id="correct", cwd="/shared",
                                      pid="123", model="", cli_args="--no-daemon -c k=v", env=None)])
        self.assertEqual(report["standalone_resumes"], 1)
        # Its argv names a different thread, the thread is not a persisted root,
        # or it is not standalone: still unresolved, so dropped.
        for command in ("codex --no-daemon resume other", "codex resume correct"):
            self.assertDropped(lambda: self.repaired(command=command), (123, "main:7.1"))
        data["sessions"][1]["session_id"] = "never-persisted"
        self.sidecar = json.dumps(data)
        self.assertDropped(lambda: self.repaired(command="codex --no-daemon resume never-persisted"),
                           (123, "main:7.1"))

    def fresh_frontend(self, tty="ttys7"):
        """A living `codex --no-daemon` launched fresh (no `resume <id>` argv)
        with no bindings.json entry. The plugin saved the npm launcher (123);
        hooks resolve to its native `codex` child (124), the real frontend."""
        data = json.loads(self.sidecar)
        data["sessions"][1].update(pid="123", pane="main:7.1", session_id="wrong-cwd-guess")
        self.sidecar = json.dumps(data)
        self.identities = {123: dict(frontend_pid=123, frontend_start="start-123", tty=tty),
                           124: dict(frontend_pid=124, frontend_start="start-124", tty=tty)}
        self.owner.process_identity = Mock(side_effect=lambda pid: self.identities.get(pid))
        self.owner.valid = Mock(side_effect=lambda record: record.get("tty") == "ttys7"
                                and record.get("pane_pid") == 77 and record.get("pane") == "%7")
        self.window = ""
        self.process_table = ""

    def live(self, children=None, argv="node /x/.bun/bin/codex --no-daemon"):
        def command(arguments):
            if arguments[:2] == ["ps", "-axo"]:
                if self.process_table is None:
                    raise repair.subprocess.CalledProcessError(1, arguments)
                return self.process_table
            if arguments[0] == "ps":
                return argv
            if arguments[0] == "pgrep":
                if children == "":
                    raise repair.subprocess.CalledProcessError(1, arguments)
                return children or str(int(arguments[-1]) + 1)
            if arguments[0] == "tmux":
                return self.window
            raise AssertionError(arguments)
        with patch.object(repair, "run", side_effect=command):
            return repair.repair(self.sidecar, self.layout, {"gone": None}, self.panes, self.socket,
                                 self.owner, self.codex, self.tracker)

    def codex_rows(self, content):
        return [(e["pane"], e["session_id"], e["pid"], e["cli_args"])
                for e in json.loads(content)["sessions"] if e["tool"] == "codex"]

    def test_living_fresh_frontend_is_kept_on_its_matching_tracker_record(self):
        self.fresh_frontend()
        state = dict(session_id="correct", ppid=124, frontend_start="start-124",
                     env=dict(tmux_pane="%7", tmux_socket=self.socket))
        record = self.tracker / "codex-124.json"
        record.write_text(json.dumps(state))
        content, *_, report = self.live()
        self.assertEqual(self.codex_rows(content), [("main:7.1", "correct", "123", "--no-daemon")])
        self.assertEqual(report["standalone_resumes"], 1)
        # A frontend that is itself the saved process (no npm launcher).
        record.rename(self.tracker / "codex-123.json")
        state.update(ppid=123, frontend_start="start-123")
        (self.tracker / "codex-123.json").write_text(json.dumps(state))
        content, *_ = self.live(children="")
        self.assertEqual(self.codex_rows(content), [("main:7.1", "correct", "123", "--no-daemon")])
        (self.tracker / "codex-123.json").unlink()
        # Mismatched or stale records are never trusted: a reused pid (other
        # start time), another frontend, pane, or server, an unpersisted or
        # nested thread, or a process no longer on the pane's tty.
        self.persist("nested", agent_path="/root/task")
        good = dict(session_id="correct", ppid=124, frontend_start="start-124",
                    env=dict(tmux_pane="%7", tmux_socket=self.socket))
        for changes in (dict(frontend_start="start-old"), dict(ppid=999), dict(session_id="never-persisted"),
                        dict(session_id="nested"), dict(env=dict(tmux_pane="%8", tmux_socket=self.socket)),
                        dict(env=dict(tmux_pane="%7", tmux_socket="/wrong")), dict(env=None), dict(ppid="x")):
            with self.subTest(changes=changes):
                record.write_text(json.dumps(dict(good, **changes)))
                self.assertDropped(self.live, (123, "main:7.1"))
        record.write_text("[]")
        self.assertDropped(self.live, (123, "main:7.1"))
        record.write_text(json.dumps(good))
        self.identities[124]["tty"] = "ttys9"  # moved off the pane's terminal
        self.assertDropped(self.live, (123, "main:7.1"))
        self.identities[124]["tty"] = "ttys7"
        self.panes["%7"]["target"] = "main:9.1"  # the saved pane is not the record's pane
        self.assertDropped(self.live, (123, "main:7.1"))

    def token(self, pid=124, start="start-124", pane="%7"):
        # codex-terminal-owner.direct_owner()'s deterministic token, which the
        # tab indicator stores in @agent_owner_token for the window. Written
        # out independently here; test_direct_token_matches_codex_terminal_owner
        # pins the repair's copy to the real one.
        import hashlib
        socket = repair.os.path.realpath(self.socket)
        return hashlib.sha256(f"{pid}:{start}:{socket}:{pane}".encode()).hexdigest()

    def rollout(self, sid, mtime=None):
        name = f"rollout-2026-10-08T10-00-00-{sid}.jsonl"
        self.persist(sid, name=name)
        if mtime is not None:
            repair.os.utime(self.codex / name, (mtime, mtime))
        return str(self.codex / name)

    def test_living_fresh_frontend_is_kept_on_its_own_window_rollout(self):
        self.fresh_frontend()
        sid = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
        rollout, token = self.rollout(sid), self.token
        self.window = f"1\t{token()}\t{sid}\t{rollout}"
        content, *_ = self.live()
        self.assertEqual(self.codex_rows(content), [("main:7.1", sid, "123", "--no-daemon")])
        self.window = f"1\t{token()}\t\t{rollout}"  # @agent_session_id not yet set
        content, *_ = self.live()
        self.assertEqual(self.codex_rows(content), [("main:7.1", sid, "123", "--no-daemon")])
        self.persist("other-thread")
        other = str(self.codex / "other-thread.jsonl")
        for window in (f"1\t{token(start='start-old')}\t{sid}\t{rollout}",   # stale: previous process
                       f"1\t{token(pane='%8')}\t{sid}\t{rollout}",           # another split's frontend
                       f"1\t{token(pid=999)}\t{sid}\t{rollout}",
                       f"1\t\t{sid}\t{rollout}", "",
                       f"1\t{token()}\tother-thread\t{rollout}",             # session id disagrees
                       f"1\t{token()}\t{sid}\t{other}",                       # not that thread's rollout
                       f"1\t{token()}\t{sid}\t{self.codex / ('rollout-x-' + sid + '.jsonl')}",
                       f"2\t{token()}\t{sid}\t{rollout}"):                   # a split the snapshot lacks
            with self.subTest(window=window):
                self.window = window
                self.assertDropped(self.live, (123, "main:7.1"))
        unpersisted = "019a2b3c-4d5e-7f60-8a9b-ffffffffffff"
        self.window = f"1\t{token()}\t{unpersisted}\t{self.codex}/rollout-2026-10-08T10-00-00-{unpersisted}.jsonl"
        self.assertDropped(self.live, (123, "main:7.1"))

    def test_window_options_are_ignored_while_another_split_runs_codex(self):
        # Hooks from Codex in two splits interleave the indicator's three
        # separate writes: the token is %7's frontend, the thread %8's (Y).
        self.fresh_frontend()
        y = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
        self.window = f"2\t{self.token()}\t{y}\t{self.rollout(y)}"
        self.panes["%8"] = dict(self.panes["%7"], target="main:7.2", pid="78")
        self.process_table = "\n".join(["  77     1 -zsh", "  78     1 -zsh", " 123    77 node /x/.bun/bin/codex --no-daemon",
                                        " 124   123 /x/vendor/codex/codex --no-daemon",
                                        " 223    78 node /x/.bun/bin/codex --no-daemon",
                                        " 224   223 /x/vendor/codex/codex --no-daemon"])
        self.assertDropped(self.live, (123, "main:7.1"))
        # A native install in the split is recognized too.
        self.process_table = "  77 1 -zsh\n  78 1 -zsh\n 300 78 /opt/homebrew/bin/codex"
        self.assertDropped(self.live, (123, "main:7.1"))
        # An unreadable process table is not evidence that the split is clear.
        self.process_table = None
        self.assertDropped(self.live, (123, "main:7.1"))
        # With only a shell (and an unrelated editor) in the split, the window
        # options are this pane's own.
        self.process_table = "  77 1 -zsh\n  78 1 -zsh\n 301 78 nvim notes.md\n 123 77 node /x/.bun/bin/codex --no-daemon"
        content, *_ = self.live()
        self.assertEqual(self.codex_rows(content), [("main:7.1", y, "123", "--no-daemon")])
        # The tracker record stays usable regardless: it names its own pane.
        self.process_table = " 223 78 node /x/.bun/bin/codex --no-daemon"
        self.persist("correct-x")
        (self.tracker / "codex-124.json").write_text(json.dumps(dict(
            session_id="correct-x", ppid=124, frontend_start="start-124",
            env=dict(tmux_pane="%7", tmux_socket=self.socket))))
        content, *_ = self.live()
        self.assertEqual(self.codex_rows(content), [("main:7.1", "correct-x", "123", "--no-daemon")])

    def test_newer_window_thread_beats_a_tracker_left_behind_by_new(self):
        # /new A -> B: SessionStart for B fires before B is persisted, so the
        # tracker keeps naming A while the window options already name B.
        self.fresh_frontend()
        a = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5a"
        b = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
        self.rollout(a, mtime=1_000_000)
        self.window = f"1\t{self.token()}\t{b}\t{self.rollout(b, mtime=2_000_000)}"
        (self.tracker / "codex-124.json").write_text(json.dumps(dict(
            session_id=a, ppid=124, frontend_start="start-124", env=dict(tmux_pane="%7", tmux_socket=self.socket))))
        content, *_ = self.live()
        self.assertEqual(self.codex_rows(content), [("main:7.1", b, "123", "--no-daemon")])
        # Whichever thread was written last wins, the tracker's included.
        repair.os.utime(self.codex / f"rollout-2026-10-08T10-00-00-{a}.jsonl", (3_000_000, 3_000_000))
        content, *_ = self.live()
        self.assertEqual(self.codex_rows(content), [("main:7.1", a, "123", "--no-daemon")])

    def test_prompt_launch_keeps_its_flags_and_drops_the_prompt(self):
        self.fresh_frontend()
        (self.tracker / "codex-124.json").write_text(json.dumps(dict(
            session_id="correct", ppid=124, frontend_start="start-124",
            env=dict(tmux_pane="%7", tmux_socket=self.socket))))
        for argv, kept in (("node /x/codex --no-daemon -c model_context_window=272000 fix the bug",
                            "--no-daemon"),  # the wrapper re-adds a fresh context override
                           ("codex --no-daemon -m o3 don't break \"it\" -- --model x", "--no-daemon -m o3"),
                           ("codex --no-daemon fix it --sandbox danger-full-access", "--no-daemon")):
            with self.subTest(argv=argv):
                content, *_ = self.live(argv=argv)
                self.assertEqual(self.codex_rows(content), [("main:7.1", "correct", "123", kept)])
        # Genuinely unsafe or non-interactive command lines are still dropped.
        for argv in ("codex --no-daemon --add-dir '/tmp/x y' fix", "codex --no-daemon review this",
                     "codex --no-daemon exec fix it", "codex --no-daemon --model", "/bin/zsh -c codex"):
            with self.subTest(argv=argv):
                self.assertDropped(lambda: self.live(argv=argv), (123, "main:7.1"))
        # Without evidence a prompt is never guessed past: argv alone needs
        # a restorable `resume <id>`.
        (self.tracker / "codex-124.json").unlink()
        self.assertDropped(lambda: self.live(argv="codex --no-daemon resume correct fix it"), (123, "main:7.1"))

    def test_kept_binding_pane_is_not_reported_dropped_for_its_launcher_pid(self):
        # The binding names the native child (123); the plugin saved the npm
        # launcher (122) for the same pane.
        data = json.loads(self.sidecar)
        data["sessions"][1].update(pid="122", pane="main:7.1", session_id="correct")
        self.sidecar = json.dumps(data)
        self.owner.process_identity.return_value = dict(frontend_pid=122, frontend_start="s", tty="ttys7")
        with patch("sys.stderr", new=io.StringIO()) as err:
            content, *_, report = self.repaired(command="codex --no-daemon")
        self.assertEqual([(e["pane"], e["session_id"], e["pid"]) for e in json.loads(content)["sessions"]
                          if e["tool"] == "codex"], [("main:7.1", "correct", "123")])
        self.assertEqual((report["unresolved_dropped"], err.getvalue()), (0, ""))

    def test_failures_inspecting_a_frontend_drop_only_that_row(self):
        self.fresh_frontend()
        (self.tracker / "codex-123.json").write_text(json.dumps(dict(
            session_id="correct", ppid=123, frontend_start="start-123",
            env=dict(tmux_pane="%7", tmux_socket=self.socket))))
        def command(arguments):
            if arguments[0] == "ps":
                return "codex --no-daemon"
            if arguments[0] == "pgrep":
                raise FileNotFoundError("pgrep")
            return ""
        with patch.object(repair, "run", side_effect=command):
            content, *_ = repair.repair(self.sidecar, self.layout, {}, self.panes, self.socket, self.owner,
                                        self.codex, self.tracker)
        self.assertEqual(self.codex_rows(content), [("main:7.1", "correct", "123", "--no-daemon")])
        for error in (repair.subprocess.TimeoutExpired("ps", 3), OSError("ps"), FileNotFoundError("tmux")):
            with self.subTest(error=error):
                def identity(pid, error=error):
                    if pid == 123 and identity.calls:
                        raise error
                    identity.calls += 1
                    return self.identities.get(pid)
                identity.calls = 0
                self.owner.process_identity = Mock(side_effect=identity)
                self.assertDropped(lambda: self.live(children=""), (123, "main:7.1"))

    def test_verified_evidence_claims_threads_before_any_argv_fallback(self):
        # main:7.1 (first) only has `resume shared` in argv; main:8.1 (later)
        # has a tracker record proving its frontend holds `shared`.
        self.fresh_frontend()
        self.persist("shared")
        data = json.loads(self.sidecar)
        data["sessions"][1]["session_id"] = "shared"
        data["sessions"].append(dict(data["sessions"][1], pid="133", pane="main:8.1"))
        self.sidecar = json.dumps(data)
        self.layout += self.row("main", 8)
        self.panes["%8"] = dict(self.panes["%7"], target="main:8.1", pid="78")
        self.identities.update({133: dict(frontend_pid=133, frontend_start="start-133", tty="ttys8"),
                                134: dict(frontend_pid=134, frontend_start="start-134", tty="ttys8")})
        self.owner.valid = Mock(side_effect=lambda record: (record.get("tty"), record.get("pane_pid"),
                                record.get("pane")) in (("ttys7", 77, "%7"), ("ttys8", 78, "%8")))
        (self.tracker / "codex-134.json").write_text(json.dumps(dict(
            session_id="shared", ppid=134, frontend_start="start-134",
            env=dict(tmux_pane="%8", tmux_socket=self.socket))))
        content, *_ = self.assertDropped(lambda: self.live(argv="codex --no-daemon resume shared"),
                                         (123, "main:7.1"))
        self.assertEqual(self.codex_rows(content), [("main:8.1", "shared", "133", "--no-daemon")])

    def test_direct_token_matches_codex_terminal_owner(self):
        owner = repair.load_owner(SCRIPT.with_name("codex-terminal-owner.py"))
        socket = repair.os.path.realpath(self.socket)
        captured = dict(frontend_pid=124, frontend_start="Thu Oct 8 10:00:00 2026", tty="ttys7", pane="%7",
                        pane_pid=77, tmux_socket=socket, term="tmux", status="bound", token="random")
        ps = SimpleNamespace(returncode=0, stdout="1 /x/vendor/codex/codex --no-daemon\n")
        with patch.object(owner, "thread_row", return_value=("user", "cli", "")), \
                patch.object(owner, "run", return_value=ps), patch.object(owner, "capture", return_value=captured), \
                patch.object(owner.os, "getppid", return_value=124):
            record = owner.direct_owner("thread")
        self.assertTrue(record and record.get("direct"))
        self.assertEqual(record["token"], repair.direct_token(124, "Thu Oct 8 10:00:00 2026", socket, "%7"))
        self.assertEqual(record["token"], self.token(124, "Thu Oct 8 10:00:00 2026", "%7"))

    def test_verified_frontend_prefers_its_current_thread_and_never_claims_one_twice(self):
        self.fresh_frontend()
        self.persist("switched")
        (self.tracker / "codex-124.json").write_text(json.dumps(dict(
            session_id="switched", ppid=124, frontend_start="start-124",
            env=dict(tmux_pane="%7", tmux_socket=self.socket))))
        # Launched as `resume wrong-cwd-guess`, then /resume'd to a persisted
        # thread in the TUI: that SessionStart rewrote the tracker record, so
        # it outranks the launch argv. (After /new the record may lag; see
        # test_newer_window_thread_beats_a_tracker_left_behind_by_new.)
        content, *_ = self.live(argv="codex --no-daemon resume wrong-cwd-guess")
        self.assertEqual(self.codex_rows(content), [("main:7.1", "switched", "123", "--no-daemon")])
        # Without a usable record the old argv evidence still applies.
        self.persist("wrong-cwd-guess")
        (self.tracker / "codex-124.json").unlink()
        content, *_ = self.live(argv="codex --no-daemon resume wrong-cwd-guess")
        self.assertEqual(self.codex_rows(content), [("main:7.1", "wrong-cwd-guess", "123", "--no-daemon")])
        # Arguments that cannot be restored safely are not kept either.
        (self.tracker / "codex-124.json").write_text(json.dumps(dict(
            session_id="switched", ppid=124, frontend_start="start-124",
            env=dict(tmux_pane="%7", tmux_socket=self.socket))))
        self.assertDropped(lambda: self.live(argv="codex --no-daemon --add-dir '/tmp/x y'"), (123, "main:7.1"))
        # Two panes can never both resume one thread.
        data = json.loads(self.sidecar)
        data["sessions"].append(dict(data["sessions"][1], pid="133", pane="main:8.1"))
        self.sidecar = json.dumps(data)
        self.layout += self.row("main", 8)
        self.panes["%8"] = dict(self.panes["%7"], target="main:8.1", pid="78")
        self.identities.update({133: dict(frontend_pid=133, frontend_start="start-133", tty="ttys8"),
                                134: dict(frontend_pid=134, frontend_start="start-134", tty="ttys8")})
        self.owner.valid = Mock(side_effect=lambda record: (record.get("tty"), record.get("pane_pid"),
                                record.get("pane")) in (("ttys7", 77, "%7"), ("ttys8", 78, "%8")))
        (self.tracker / "codex-134.json").write_text(json.dumps(dict(
            session_id="switched", ppid=134, frontend_start="start-134",
            env=dict(tmux_pane="%8", tmux_socket=self.socket))))
        content, *_ = self.assertDropped(self.live, (133, "main:8.1"))
        self.assertEqual(self.codex_rows(content), [("main:7.1", "switched", "123", "--no-daemon")])
        self.persist("second")
        (self.tracker / "codex-134.json").write_text(json.dumps(dict(
            session_id="second", ppid=134, frontend_start="start-134",
            env=dict(tmux_pane="%8", tmux_socket=self.socket))))
        content, *_ = self.live()
        self.assertEqual(self.codex_rows(content), [("main:7.1", "switched", "123", "--no-daemon"),
                                                    ("main:8.1", "second", "133", "--no-daemon")])

    def test_unsafe_cli_arguments_drop_only_their_own_row(self):
        # Once this aborted the whole repair (every save file left as the
        # plugin wrote it); now that binding alone is invalid, and its pane
        # restores as a plain shell rather than with mangled arguments.
        sidecar, layout = self.root / "assistant-sessions.json", self.root / "save.txt"
        sidecar.write_text(self.sidecar)
        layout.write_text(self.layout)
        (self.root / "last").symlink_to(layout.name)
        bindings = self.root / "bindings.json"
        bindings.write_text(json.dumps({"correct": self.record}))
        helper = self.root / "owner.py"
        helper.write_text("def valid(record): return True\ndef process_identity(pid): return None\n")
        snapshot = "%7\tmain:7.1\t/shared\t1\tzsh\t77"
        def command(arguments):
            return snapshot if arguments[0] == "tmux" else "codex --add-dir '/tmp/with spaces'"
        with patch.object(repair, "run", side_effect=command), patch.object(repair, "atomic_write") as write, \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            repair.main(["--resurrect-dir", str(self.root), "--bindings", str(bindings),
                "--socket", self.socket, "--owner-helper", str(helper), "--codex-home", str(self.codex)])
        self.assertEqual(json.loads(out.getvalue())["invalid_bindings"], 1)
        written = {path.name: content for (path, content), _ in write.call_args_list}
        self.assertEqual(set(written), {"assistant-sessions.json"})  # the layout needed no repair
        self.assertEqual(json.loads(written["assistant-sessions.json"])["sessions"], [self.claude])
        self.assertNotIn(b"--add-dir", written["assistant-sessions.json"])
        self.assertEqual(layout.read_text(), self.layout)


WRAPPER = Path(__file__).parents[1] / "scripts/codex-max-context.py"


class Exec(Exception):
    pass


class ContextOverrideRoundTripTests(unittest.TestCase):
    """Saved cli_args, resumed through the real wrapper, saved again: no pile-up.

    The wrapper (codex-max-context.py launch, as ~/.local/bin/codex runs it)
    prepends `-c model_context_window=<catalog max>`; a saved copy would sit
    later in argv and win, one more per cycle, pinning the old maximum.
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cx-ctx-")
        self.addCleanup(temp.cleanup)
        self.real = Path(temp.name) / "codex"
        self.real.write_text("#!/bin/sh\nexit 0\n")
        self.real.chmod(0o755)
        spec = importlib.util.spec_from_file_location("codex_max_context_rt", WRAPPER)
        self.wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wrapper)

    def launch(self, args, maximum):
        """The argv the wrapper execs (what ps then shows) for `codex <args>`."""
        catalog = json.dumps({"models": [{"max_context_window": maximum}]})

        def execv(path, argv):
            raise Exec(argv)

        with patch.dict("os.environ", {"PATH": str(self.real.parent)}), \
                patch("subprocess.check_output", return_value=catalog), patch("os.execv", execv), \
                patch("sys.argv", ["codex-max-context.py", "launch", *args]), \
                patch("sys.dont_write_bytecode", True), self.assertRaises(Exec) as launched:
            self.wrapper.main()
        return launched.exception.args[0]

    def test_three_save_restore_cycles_keep_one_fresh_context_override(self):
        for first, drop_prompt in ((["-m", "o3", "-c", "model_reasoning_effort=high"], False),
                                   (["-m", "o3", "fix", "the", "bug"], True)):
            argv = self.launch(first, 272000)
            for cycle, maximum in enumerate((400000, 872000, 1000000), 1):
                with self.subTest(drop_prompt=drop_prompt, cycle=cycle):
                    cli_args, _, model = repair.codex_args(" ".join(argv), drop_prompt=drop_prompt)
                    self.assertNotIn("model_context_window", cli_args)
                    self.assertEqual(cli_args.split().count("--no-daemon"), 1)
                    self.assertEqual(model, "o3")
                    argv = self.launch(cli_args.split() + ["resume", "sid-1"], maximum)
                    self.assertEqual(sum("model_context_window" in arg for arg in argv), 1, argv)
                    self.assertEqual(argv[:4], [str(self.real), "-c", f"model_context_window={maximum}",
                                                "--no-daemon"])
                    self.assertEqual(argv.count("--no-daemon"), 1)
                    tail = ["-m", "o3"] + (["-c", "model_reasoning_effort=high"] if not drop_prompt else [])
                    self.assertEqual(argv[4:], tail + ["resume", "sid-1"])

    def test_every_spelling_of_the_override_is_dropped(self):
        for spelling in ("-c model_context_window=1", "--config model_context_window=1",
                         "-c=model_context_window=1", "--config=model_context_window=1",
                         "-cmodel_context_window=1", "-c model_context_window=1 -c model_context_window=2"):
            for drop_prompt in (False, True):
                with self.subTest(spelling=spelling, drop_prompt=drop_prompt):
                    flags, sid, _ = repair.codex_args(
                        f"codex --no-daemon {spelling} -c model_reasoning_effort=high -cfoo=1 resume s-1",
                        drop_prompt=drop_prompt)
                    self.assertEqual((flags, sid), ("--no-daemon -c model_reasoning_effort=high -cfoo=1", "s-1"))
        self.assertEqual(repair.codex_args("codex -c model_context_window_x=1")[0], "-c model_context_window_x=1")


if __name__ == "__main__":
    unittest.main()

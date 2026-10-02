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

    def persist(self, sid, thread_source="user", source="cli", agent_path="", header_sid=None):
        path = self.codex / f"{sid}.jsonl"
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

    def test_unresolved_living_saved_owner_aborts_before_any_write(self):
        data = json.loads(self.sidecar)
        data["sessions"][1]["pid"] = "123"
        self.sidecar = json.dumps(data)
        self.owner.valid.return_value = False
        self.owner.process_identity.return_value = dict(frontend_pid=123, tty="ttys7")
        with self.assertRaisesRegex(ValueError, "Unresolved living Codex frontend PIDs: 123"):
            self.repaired()
        self.assertEqual(json.loads(self.sidecar), data)
        # A dead saved PID does not justify restoring an unverified session.
        self.owner.process_identity.return_value = None
        content, *_ = self.repaired()
        self.assertEqual(json.loads(content)["sessions"], [self.claude])

    def test_unsafe_cli_arguments_leave_every_real_save_file_unchanged(self):
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
        with patch.object(repair, "run", side_effect=command), patch.object(repair, "atomic_write") as write:
            with self.assertRaisesRegex(ValueError, "Quoted/spaced"):
                repair.main(["--resurrect-dir", str(self.root), "--bindings", str(bindings),
                    "--socket", self.socket, "--owner-helper", str(helper), "--codex-home", str(self.codex)])
            write.assert_not_called()
        self.assertEqual(sidecar.read_text(), self.sidecar)
        self.assertEqual(layout.read_text(), self.layout)


if __name__ == "__main__":
    unittest.main()

"""agent-session-lib.sh against a fake ~/.claude tree in a temp HOME.

Covers resolve_session_bases (own sessionId only, no compaction walk), the
subagent finished/running rules, the workflow rule, and the cost contract:
ONE `stat` fork per session base no matter how many subagent transcripts.
"""
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

LIB = Path(os.environ.get(
    "AGENT_SESSION_LIB_SOURCE",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts/agent-session-lib.sh")))

# The lib needs bash 4+ (associative arrays, %(%s)T); macOS /bin/bash is 3.2.
BASH = next((b for b in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", shutil.which("bash"))
             if b and os.path.exists(b)), "bash")

PID = "424242"
SID = "11111111-aaaa-bbbb-cccc-000000000001"
CWD = "/Users/someone/code/my.proj_x"
PROJ = "-Users-someone-code-my-proj-x"


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


class LibCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude/sessions").mkdir(parents=True)
        (self.home / ".claude/sessions" / f"{PID}.json").write_text(json.dumps(
            {"pid": int(PID), "sessionId": SID, "cwd": CWD, "status": "idle"},
            separators=(",", ":")))
        self.proj = self.home / ".claude/projects" / PROJ
        self.proj.mkdir(parents=True)
        self.parent = self.proj / f"{SID}.jsonl"
        self.parent.write_text('{"type":"user","message":{"content":"hi"}}\n')
        self.now = int(time.time())
        self.env = dict(os.environ, HOME=str(self.home))

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def bash(self, script, env=None):
        r = subprocess.run([BASH, "-c", f'set -u; . "{LIB}"; {script}'],
                           capture_output=True, text=True, env=env or self.env, timeout=30)
        self.assertEqual(r.stderr, "", r.stderr)
        return r.stdout

    def rc(self, fn, env=None):
        return self.bash(f'{fn} {PID}; echo "rc=$?"', env).strip().splitlines()[-1]

    def subagent(self, aid, age, last='{"type":"assistant","message":{"content":[]}}'):
        d = self.proj / SID / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"agent-{aid}.jsonl"
        f.write_text('{"type":"user","message":{"content":"task"}}\n' + last + "\n")
        os.utime(f, (self.now - age, self.now - age))
        return f

    def notify(self, aid, at):
        with self.parent.open("a") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": iso(at), "message": {
                "content": f"<task-notification><task-id>{aid}</task-id>"
                           "<status>completed</status></task-notification>"}},
                separators=(",", ":")) + "\n")

    # -- resolve_session_bases ------------------------------------------
    def test_resolve_sets_only_own_session(self):
        # A transcript whose compaction summary names ours must NOT be
        # followed any more: the walk is gone (2026-10-07).
        (self.proj / "22222222-dddd.jsonl").write_text(json.dumps(
            {"type": "user", "isCompactSummary": True,
             "message": {"content": f"continued from {self.proj}/{SID}.jsonl"}},
            separators=(",", ":")) + "\n")
        out = self.bash(f'resolve_session_bases {PID}; echo "rc=$?"; '
                        'echo "proj=$SESSION_PROJ"; echo "n=${#SESSION_BASES[@]}"; '
                        'echo "bases=${SESSION_BASES[*]}"')
        self.assertIn("rc=0", out)
        self.assertIn(f"proj={self.proj}", out)
        self.assertIn("n=1", out)
        self.assertIn(f"bases={SID}\n", out)

    def test_resolve_missing_or_invalid_session_file(self):
        out = self.bash('resolve_session_bases 999999; echo "rc=$? n=${#SESSION_BASES[@]}"')
        self.assertIn("rc=1 n=0", out)
        (self.home / ".claude/sessions/777.json").write_text('{"pid":777}')
        out = self.bash('resolve_session_bases 777; echo "rc=$? n=${#SESSION_BASES[@]}"')
        self.assertIn("rc=1 n=0", out)

    # -- session_has_running_subagent -----------------------------------
    def test_no_subagents_dir(self):
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=1")

    def test_fresh_unnotified_is_running(self):
        self.subagent("a1", 30)
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=0")

    def test_notified_since_last_move_is_finished(self):
        self.subagent("a1", 100)
        self.notify("a1", self.now - 105)   # ~ just before its last write, inside the grace
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=1")

    def test_notified_then_moved_again_is_running(self):
        self.subagent("a1", 100)
        self.notify("a1", self.now - 500)   # resumed after the notice
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=0")

    def test_latest_notification_wins(self):
        self.subagent("a1", 100)
        self.subagent("a10", 100)            # prefix of no one's id but its own
        self.notify("a1", self.now - 900)    # an earlier run
        self.notify("a10", self.now - 100)
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=0")  # a1 resumed
        self.notify("a1", self.now - 100)
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=1")

    def test_incremental_parent_read_in_one_process(self):
        self.subagent("a1", 100)
        script = (f'session_has_running_subagent {PID}; echo "rc=$?"; '
                  f'printf "%s\\n" \'{{"type":"user","timestamp":"{iso(self.now - 100)}",'
                  f'"message":{{"content":"<task-id>a1</task-id>"}}}}\' >> "{self.parent}"; '
                  f'session_has_running_subagent {PID}; echo "rc=$?"')
        self.assertEqual(self.bash(script).split(), ["rc=0", "rc=1"])

    def test_rotated_parent_forgets_old_notices(self):
        self.subagent("a1", 100)
        self.notify("a1", self.now - 100)
        script = (f'session_has_running_subagent {PID}; echo "rc=$?"; '
                  f': > "{self.parent}"; '                      # truncated: size < consumed
                  f'session_has_running_subagent {PID}; echo "rc=$?"')
        self.assertEqual(self.bash(script).split(), ["rc=1", "rc=0"])

    def test_partial_last_line_is_read_next_time(self):
        self.subagent("a1", 100)
        rec = json.dumps({"type": "user", "timestamp": iso(self.now - 100), "message": {
            "content": "<task-notification><task-id>a1</task-id></task-notification>"}},
            separators=(",", ":"))
        with self.parent.open("a") as fh:
            fh.write(rec)                                   # no newline yet: mid-write
        script = (f'session_has_running_subagent {PID}; echo "rc=$?"; '
                  f'printf "\\n" >> "{self.parent}"; '
                  f'session_has_running_subagent {PID}; echo "rc=$?"')
        self.assertEqual(self.bash(script).split(), ["rc=0", "rc=1"])

    # -- hostile transcript content ---------------------------------------
    def test_injected_task_id_runs_nothing(self):
        # The reviewer's repro: with the old [^<]+ id regex this token reached
        # $(( )) as `a[$(touch …)]` and bash ran the command.
        pwned = self.home / "PWNED"
        self.subagent("abc", 100)
        with self.parent.open("a") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": iso(self.now - 100), "message": {
                "content": f"<task-id>abc=a[$(touch${{IFS}}{pwned})]</task-id>"}},
                separators=(",", ":")) + "\n")
        rc = self.rc("session_has_running_subagent")
        self.assertFalse(pwned.exists(), "transcript text was executed")
        self.assertEqual(rc, "rc=0")                                        # notice refused

    def test_hostile_tokens_rejected_in_bash_too(self):
        # Even if the python layer let one through, bash keeps it out of $(( )).
        pwned = self.home / "PWNED"
        fake = self.home / "fakepy"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' 100 "
                        f"'abc=a[$(touch${{IFS}}{pwned})] abc=1=a[$(touch${{IFS}}{pwned})] "
                        f"$(touch${{IFS}}{pwned})=5 abc=0x10'\n")
        fake.chmod(0o755)
        self.subagent("abc", 100)
        out = self.bash(f'_SL_PY={fake}; session_has_running_subagent {PID}; echo "rc=$?"; '
                        'for k in "${!NOTIF_WHEN[@]}"; do echo "key=${k##*|}"; done')
        self.assertIn("rc=0", out)
        self.assertNotIn("key=", out)
        self.assertFalse(pwned.exists())

    def test_vanished_interpreter_is_re_resolved(self):
        self.subagent("a1", 100)
        self.notify("a1", self.now - 100)
        out = self.bash(f'_SL_PY={self.home}/gone/python3; session_has_running_subagent {PID}; '
                        'echo "rc=$? py=$_SL_PY"')
        self.assertIn("rc=1", out)
        self.assertNotIn("/gone/", out)

    # -- bounded first read ----------------------------------------------
    def assistant(self, at, uid):
        return json.dumps({"parentUuid": None, "message": {"role": "assistant", "content": []},
                           "type": "assistant", "uuid": uid, "timestamp": iso(at)},
                          separators=(",", ":"))

    def note(self, aid, at):
        return json.dumps({"type": "attachment", "attachment": {"type": "queued_command",
                           "prompt": f"<task-notification><task-id>{aid}</task-id>"},
                           "timestamp": iso(at)}, separators=(",", ":"))

    def test_bounded_first_read_matches_full_read(self):
        old, pad = self.now - 3 * 3600, "x" * 300
        lines = []
        for i in range(200):                                   # ~3 h ago, > 60 KB
            lines.append(self.assistant(old + i, f"o{i}"))
            lines.append(json.dumps({"type": "user", "timestamp": iso(old + i), "pad": pad}))
            if i % 40 == 0:
                lines.append(self.note(f"old{i}", old + i))
        lines.append(self.note("r2", self.now - 2000))         # r2 resumed since
        for i in range(50):                                    # the last ~half hour
            lines.append(self.assistant(self.now - 1800 + i * 30, f"n{i}"))
            lines.append(json.dumps({"type": "user", "timestamp": iso(self.now - 1800 + i * 30),
                                     "pad": pad}))
        lines.append(self.note("r1", self.now - 100))
        lines.append(self.note("old0", old))                   # a laggard, written late
        self.parent.write_text("\n".join(lines) + "\n")
        self.subagent("r1", 100)      # notified after it last moved → finished
        self.subagent("r2", 100)      # notified, then moved again → running
        self.subagent("r3", 100)      # never notified → running
        probe = ('echo "rc=$rc size=${NOTIF_SIZE[$p]}"; '
                 'for k in r1 r2 r3 old0 old40; do echo "$k=${NOTIF_WHEN[$p|$k]:-}"; done')
        pre = f'resolve_session_bases {PID}; p="$SESSION_PROJ/${{SESSION_BASES[0]}}.jsonl"; '
        bounded = self.bash(pre + f'NOTIF_FIRST_CHUNK=4096; session_has_running_subagent {PID}; '
                            f'rc=$?; {probe}').split()
        full = self.bash(pre + f'notified_ids "$p"; session_has_running_subagent {PID}; '
                         f'rc=$?; {probe}').split()
        size = f"size={self.parent.stat().st_size}"
        self.assertIn(size, bounded)
        self.assertIn(size, full)
        self.assertEqual(bounded[:5], full[:5])                # rc, size, r1, r2, r3
        self.assertEqual(bounded[0], "rc=0")
        self.assertIn(f"r1={self.now - 100}", bounded)
        self.assertIn(f"old0={old}", bounded)                  # the late laggard: read
        self.assertIn("old40=", bounded)                       # before the anchor: skipped
        self.assertIn(f"old40={old + 40}", full)

    def test_bounded_read_without_anchor_reads_everything(self):
        self.parent.write_text("\n".join(
            [json.dumps({"type": "user", "timestamp": iso(self.now - 9000), "pad": "x" * 9000})] * 3
            + [self.note("a1", self.now - 100)]) + "\n")
        self.subagent("a1", 100)
        out = self.bash(f'NOTIF_FIRST_CHUNK=4096; session_has_running_subagent {PID}; echo "rc=$?"')
        self.assertIn("rc=1", out)

    def test_window_variable_matches_pinned_literal(self):
        import re
        src = LIB.read_text()
        pinned = re.findall(r'\[ "\$age" -lt (\d+) \] \|\| continue', src)
        window = re.findall(r"^SUBAGENT_WINDOW=(\d+)", src, re.M)
        self.assertEqual(len(pinned), 1)
        self.assertEqual(pinned, window)

    def test_interrupted_is_finished(self):
        self.subagent("a1", 30, last=json.dumps(
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "[Request interrupted by user]"}]}},
            separators=(",", ":")))
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=1")

    def test_older_than_an_hour_is_ignored(self):
        self.subagent("a1", 3700)
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=1")

    def test_one_running_among_finished(self):
        for i in range(5):
            self.subagent(f"f{i}", 100)
            self.notify(f"f{i}", self.now - 100)
        self.subagent("old", 7200)
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=1")
        self.subagent("live", 10)
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=0")

    def test_missing_parent_transcript_reads_running(self):
        self.parent.unlink()
        self.subagent("a1", 30)
        self.assertEqual(self.rc("session_has_running_subagent"), "rc=0")

    # -- session_has_running_workflow -----------------------------------
    def workflow(self, wid, age, completed):
        d = self.proj / SID / "subagents/workflows" / f"wf_{wid}"
        d.mkdir(parents=True)
        for name in ("agent-x.jsonl", "journal.jsonl"):
            (d / name).write_text("{}\n")
            os.utime(d / name, (self.now - age, self.now - age))
        if completed:
            (self.proj / SID / "workflows").mkdir(parents=True, exist_ok=True)
            (self.proj / SID / "workflows" / f"wf_{wid}.json").write_text("{}")

    def test_workflow_running(self):
        self.workflow("run", 60, completed=False)
        self.assertEqual(self.rc("session_has_running_workflow"), "rc=0")

    def test_workflow_completed(self):
        self.workflow("done", 60, completed=True)
        self.assertEqual(self.rc("session_has_running_workflow"), "rc=1")

    def test_workflow_stale_without_completion(self):
        self.workflow("crashed", 4000, completed=False)
        self.assertEqual(self.rc("session_has_running_workflow"), "rc=1")

    def test_no_workflows(self):
        self.assertEqual(self.rc("session_has_running_workflow"), "rc=1")

    # -- cost contract ---------------------------------------------------
    def test_python_skips_version_manager_shims(self):
        shims = self.home / ".pyenv/shims"
        shims.mkdir(parents=True)
        (shims / "python3").write_text("#!/bin/sh\nexit 97\n")   # would break the parse
        (shims / "python3").chmod(0o755)
        env = dict(self.env, PATH=f"{shims}:{self.env['PATH']}")
        out = self.bash('_sl_python; echo "py=$_SL_PY"', env)
        self.assertNotIn("/shims/", out)
        self.subagent("a1", 100)
        self.notify("a1", self.now - 100)
        self.assertEqual(self.rc("session_has_running_subagent", env), "rc=1")


    def test_one_stat_per_base(self):
        n = 40
        for i in range(n):
            self.subagent(f"f{i:02d}", 100)
            self.notify(f"f{i:02d}", self.now - 100)
        for i in range(5):
            self.subagent(f"old{i}", 7200)
        fake = self.home / "fakebin"
        fake.mkdir()
        log = self.home / "stat.log"
        real = shutil.which("stat", path="/usr/bin:/bin")
        (fake / "stat").write_text(f'#!/bin/sh\necho x >> "{log}"\nexec {real} "$@"\n')
        (fake / "stat").chmod(0o755)
        env = dict(self.env, PATH=f"{fake}:{self.env['PATH']}")
        # Cold and warm call in one process (the watcher's long-lived loop).
        out = self.bash(f'session_has_running_subagent {PID}; echo "rc=$?"; '
                        f'session_has_running_subagent {PID}; echo "rc=$?"', env)
        self.assertEqual(out.split(), ["rc=1", "rc=1"])
        self.assertEqual(len(log.read_text().splitlines()), 2,
                         f"expected one stat per base per call for {n} subagent files")
        # ...and the verdict really came from the files the one stat covered.
        self.subagent("live", 5)
        log.write_text("")
        self.assertEqual(self.rc("session_has_running_subagent", env), "rc=0")
        self.assertEqual(len(log.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()

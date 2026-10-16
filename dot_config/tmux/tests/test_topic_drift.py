"""Topic drift retitle + custom-title (/rename) precedence for Claude tabs.

Two layers:
  * DriftAwkTests drive scripts/agent-topic-drift.awk directly with macOS
    /usr/bin/awk (BWK awk) under LC_ALL=C, exactly as the hook and an offline
    replay do.
  * DriftHookTests reuse test_codex_indicator's fake-tmux harness (isolated
    HOME/TMPDIR, no real tmux server, no model call: a fake copilot condenses
    and judges deterministically) to check what extract_summary /
    compose_summary / the condenser paint.
Run with pytest or unittest.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

import test_codex_indicator as ci

AWK_SOURCE = ci.SOURCE.parent / "agent-topic-drift.awk"
AWK = "/usr/bin/awk" if os.path.exists("/usr/bin/awk") else "awk"
TITLE = "Fix websocket reconnect bug"

ON_TOPIC = [
    "the websocket reconnect drops messages whenever the server restarts under load",
    "websocket reconnect should replay the dropped messages after the server restarts",
    "reconnect backoff for the websocket client loses messages during server restarts",
    "add a test that restarts the server and checks websocket messages survive reconnect",
    "the reconnect test still drops two websocket messages on the second server restart",
]
DRIFT = [
    "lowk can you look at the payment invoice generation pdf rendering margins",
    "the invoice pdf export has wrong margins and footers on every printed page",
    "invoice footers overlap the totals table when the pdf has many line items",
]


class DriftAwkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="topic-drift-awk-")
        self.dir = Path(self.tmp.name)
        self.state = self.dir / "state"
        self.now = 10_000

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, prompt, title=TITLE, custom="", ai="", launch="", now=None, locale="C", **params):
        out = self.dir / "state.new"
        args = [AWK, "-v", "now=%d" % (self.now if now is None else now), "-v", "state_out=%s" % out]
        for k, v in params.items():
            args += ["-v", "%s=%s" % (k, v)]
        args += ["-f", str(AWK_SOURCE), str(self.state) if self.state.exists() else "/dev/null"]
        env = dict(os.environ, DRIFT_PROMPT=prompt, DRIFT_TITLE=title, DRIFT_CUSTOM=custom, DRIFT_AI=ai,
                   DRIFT_LAUNCH=launch, LC_ALL=locale)
        r = subprocess.run(args, env=env, capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, b"")
        out.replace(self.state)
        return r.stdout.decode().strip()

    def fields(self):
        f = {}
        # Every byte the awk writes must be valid UTF-8 (strict decode).
        for line in self.state.read_bytes().decode("utf-8").splitlines():
            k, _, v = line.partition("\t")
            f.setdefault(k, []).append(v)
        return f

    def get(self, key):
        return self.fields().get(key, [""])[-1]

    def drift_to_retitle(self, **kw):
        """On-topic, then three drifted prompts: the third fires."""
        self.feed(ON_TOPIC[0], **kw)
        self.assertNotIn("RETITLE", self.feed(DRIFT[0], **kw))
        self.assertNotIn("RETITLE", self.feed(DRIFT[1], **kw))
        v = self.feed(DRIFT[2], **kw)
        self.assertIn("RETITLE", v)
        return v

    def test_defaults(self):
        src = AWK_SOURCE.read_text()
        for line in ('if (strikes_needed == "") strikes_needed = 3', 'if (cooldown == "") cooldown = 1200',
                     'if (drift_t == "") drift_t = 0.15', 'if (keep == "") keep = 6',
                     'if (min_turn == "") min_turn = 2', 'if (min_words == "") min_words = 4',
                     'if (max_chars == "") max_chars = 600'):
            self.assertIn(line, src)

    def test_short_prompts_are_ignored(self):
        self.feed(ON_TOPIC[0])
        before = self.fields()
        for p in ["yes", "do it", "lgtm", "ok go ahead", "sounds good, thanks!", "yeah lowk just do that"]:
            self.assertTrue(self.feed(p).startswith("skip short"), p)
        after = self.fields()
        self.assertEqual(after["bag"], before["bag"])
        self.assertEqual(after["turns"], before["turns"])
        self.assertEqual(after["strikes"], ["0"])

    def test_on_topic_sequence_never_triggers(self):
        for p in ON_TOPIC * 2:
            self.assertNotIn("RETITLE", self.feed(p), p)
        self.assertEqual(self.get("retitle_raw"), "")
        self.assertEqual(self.get("strikes"), "0")
        self.assertLessEqual(len(self.fields()["bag"]), 6)

    def test_three_drifted_prompts_trigger(self):
        self.drift_to_retitle()
        raw = self.get("retitle_raw")
        self.assertEqual(raw, " / ".join(DRIFT))
        self.assertEqual(self.get("retitle_at"), str(self.now))
        self.assertEqual(self.get("strikes"), "0")
        self.assertNotIn("cand", self.fields())
        # The ring restarted from the drifted prompts: the new topic is now on topic.
        self.assertIn("on", self.feed("invoice pdf margins are still wrong on the footer pages"))
        self.assertEqual(self.get("retitle_raw"), raw)

    def test_pivot_phrase_alone_does_not_trigger(self):
        self.feed(ON_TOPIC[0])
        # A pivot phrase on an ON-topic prompt is no strike at all.
        self.assertTrue(self.feed("different topic but the websocket reconnect drops messages "
                                  "after server restarts").startswith("on"))
        self.assertEqual(self.get("strikes"), "0")
        v = self.feed("Switching gears: the invoice pdf export has wrong margins on printed footers")
        self.assertIn("strikes=2", v)
        self.assertNotIn("RETITLE", v)
        # ...but it is +1, so one more drifted prompt fires.
        self.assertIn("RETITLE", self.feed(DRIFT[2]))

    def test_on_topic_prompt_resets_strikes(self):
        self.feed(ON_TOPIC[0])
        self.feed(DRIFT[0])
        self.feed(DRIFT[1])
        self.assertEqual(self.get("strikes"), "2")
        self.feed(ON_TOPIC[1])
        self.assertEqual(self.get("strikes"), "0")
        self.assertNotIn("cand", self.fields())
        self.assertNotIn("RETITLE", self.feed(DRIFT[2]))

    def test_cooldown_blocks_then_allows(self):
        self.drift_to_retitle()
        first = self.get("retitle_raw")
        third = ["the kubernetes ingress controller rejects grpc traffic over http2 upstreams",
                 "grpc upstream health checks in the kubernetes ingress keep flapping hourly",
                 "kubernetes ingress annotations for grpc timeouts are ignored by nginx",
                 "nginx ingress grpc keepalive settings need tuning for long streams"]
        for p in third[:3]:   # 300 s apart: inside the 1200 s cooldown
            self.now += 300
            self.assertNotIn("RETITLE", self.feed(p))
        self.assertEqual(self.get("retitle_raw"), first)
        self.assertGreaterEqual(int(self.get("strikes")), 3)
        self.now += 400       # 1300 s since the last retitle
        self.assertIn("RETITLE", self.feed(third[3]))
        self.assertIn("kubernetes", self.get("retitle_raw"))
        self.assertEqual(self.get("retitle_at"), str(self.now))

    def test_first_substantive_prompt_never_triggers(self):
        v = self.feed("switching gears, totally unrelated: the invoice pdf export margins are wrong",
                      strikes_needed=1)
        self.assertNotIn("RETITLE", v)
        self.assertEqual(self.get("retitle_raw"), "")
        self.assertEqual(self.get("turns"), "1")

    def test_empty_bag_takes_first_prompt_on_topic(self):
        v = self.feed(DRIFT[0], title="")
        self.assertTrue(v.startswith("on"), v)
        self.assertEqual(self.get("strikes"), "0")

    def test_tag_slash_peer_and_continuation_prompts_skipped(self):
        self.feed(ON_TOPIC[0])
        before = self.state.read_text()
        for p in ["<task-notification><task-id>abc</task-id> the invoice pdf export finished rendering margins</task-notification>",
                  "<command-name>/rename</command-name> invoice pdf export margins footer pages",
                  "/rename invoice pdf export margins footer pages printed",
                  "  /compact invoice pdf export margins footer pages printed",
                  "\n\n\t  <task-notification>invoice pdf export margins footer pages printed</task-notification>",
                  "  /compact invoice pdf export margins footer pages printed",
                  "\r\n[message from @reviewer] invoice pdf export margins footer pages are wrong",
                  "[message from @reviewer] invoice pdf export margins footer pages are wrong",
                  "This session is being continued from a previous conversation that ran out of context. "
                  "The invoice pdf export margins footer pages summary follows"]:
            self.assertEqual(self.feed(p), "skip tag", repr(p))
        self.assertEqual(self.state.read_text(), before)

    def test_pasted_content_is_cut_before_tokenizing(self):
        self.feed(ON_TOPIC[0])
        paste = "kubernetes ingress grpc nginx upstream keepalive " * 20
        # A paste alone carries no goal.
        self.assertEqual(self.feed('<pasted_content id="1">' + paste + "</pasted_content>"), "skip tag")
        # Unterminated: runs to the end.
        v = self.feed("look at this <pasted_content>" + paste)
        self.assertTrue(v.startswith("skip short"), v)
        # The user's own words around a paste are what counts: on topic here.
        v = self.feed("<pasted_content>" + paste + "</pasted_content> the websocket reconnect still "
                      "drops messages after server restarts <pasted_content>" + paste)
        self.assertTrue(v.startswith("on"), v)
        self.assertNotIn("kubernet", " ".join(self.fields()["bag"]))

    def test_corrupt_state_tolerated(self):
        for junk in [b"garbage\x01\x02\n\n", b"v\t1\nturns\tabc\nstrikes\t-3\nbag\tUPPER Case!\ncand\tno-tab\n",
                     b"", b"\x00\xff\xfe binary"]:
            self.state.write_bytes(junk)
            v = self.feed(ON_TOPIC[0])
            self.assertTrue(v.startswith("on"), (junk, v))
            f = self.fields()
            self.assertEqual(f["v"], ["1"])
            self.assertTrue(f["turns"][0].isdigit())
            self.assertTrue(all(b and b == b.lower() for b in f.get("bag", [])), f)

    def test_title_seeds_bag_on_first_use(self):
        self.feed("yes")
        self.assertEqual(self.fields()["bag"], ["websocket reconnect"])

    def test_rename_reseeds_bag(self):
        self.feed(ON_TOPIC[0])
        self.feed(DRIFT[0])
        v = self.feed("yes", custom="invoice-pdf-margins")
        self.assertTrue(v.startswith("renamed"), v)
        self.assertEqual(self.fields()["bag"], ["invoice pdf margin"])
        self.assertEqual(self.get("seen_custom"), "invoice-pdf-margins")
        self.assertEqual(self.get("strikes"), "0")
        self.assertTrue(self.feed(DRIFT[1], custom="invoice-pdf-margins").startswith("on"))

    def test_launch_custom_recorded_once(self):
        slug = "check-five-variable-solu"
        self.feed("yes", custom=slug)
        self.assertEqual(self.get("launch_custom"), slug)
        self.assertEqual(self.get("seen_custom"), slug)
        self.feed(ON_TOPIC[0], custom=slug)
        self.feed("yes", custom="real-user-rename")
        self.assertEqual(self.get("launch_custom"), slug)
        self.assertEqual(self.get("seen_custom"), "real-user-rename")
        # No custom at creation: no launch_custom, ever.
        self.state.unlink()
        self.feed("yes")
        self.feed("yes", custom="later-rename")
        self.assertNotIn("launch_custom", self.fields())

    def test_launch_from_transcript_head(self):
        slug = "check-five-variable-solu"
        # The slug is out of the tail (DRIFT_CUSTOM empty): the head's wins.
        self.feed("yes", launch=slug)
        self.assertEqual(self.get("launch_custom"), slug)
        self.assertEqual(self.get("seen_custom"), slug)
        # Re-appended later: not a rename (no reseed).
        self.assertFalse(self.feed("yes", custom=slug).startswith("renamed"))
        # Only read on a fresh state: ignored afterwards.
        self.feed("yes", launch="something-else")
        self.assertEqual(self.get("launch_custom"), slug)
        # Head slug + a different tail custom: the tail's is the user's.
        self.state.unlink()
        self.feed("yes", launch=slug, custom="user-rename")
        self.assertEqual(self.get("launch_custom"), slug)
        self.assertEqual(self.get("seen_custom"), "user-rename")

    def test_retitle_records_custom_in_force(self):
        c = "websocket-reconnect"
        self.drift_to_retitle(custom=c)
        self.assertEqual(self.get("retitle_custom"), c)
        # The custom-title line fell out of the hook's tail (DRIFT_CUSTOM empty):
        # the one last seen is still the one in force.
        self.state.unlink()
        self.feed(ON_TOPIC[0], custom=c)
        self.feed(DRIFT[0])
        self.feed(DRIFT[1])
        self.assertIn("RETITLE", self.feed(DRIFT[2]))
        self.assertEqual(self.get("retitle_custom"), c)

    def test_seen_ai_kept(self):
        self.feed("yes", ai="Websocket reconnect fix")
        self.assertEqual(self.get("seen_ai"), "Websocket reconnect fix")
        self.feed("yes", ai="")
        self.assertEqual(self.get("seen_ai"), "Websocket reconnect fix")
        self.feed("yes", ai="Newer\ttitle")
        self.assertEqual(self.get("seen_ai"), "Newer title")

    def test_retitle_prev_carried_and_cleared(self):
        self.drift_to_retitle()
        with self.state.open("a") as f:
            f.write("retitle_prev\tWebsocket reconnect\n")   # what the hook appends
        self.feed(ON_TOPIC[0])
        self.assertEqual(self.get("retitle_prev"), "Websocket reconnect")
        self.now += 5000
        for p in ["kubernetes ingress controller rejects grpc traffic over http2 upstreams",
                  "grpc upstream health checks in the kubernetes ingress keep flapping hourly",
                  "kubernetes ingress annotations for grpc timeouts are ignored by nginx"]:
            if "RETITLE" in self.feed(p):
                break
        else:
            self.fail("no retitle")
        self.assertNotIn("retitle_prev", self.fields())

    # Multibyte characters straddling the byte cuts: 200 (candidate text), 300
    # (retitle_raw) and 600 (the tokenized head). Before the fix, macOS awk
    # exited 2 on such a cut and the prompt was silently dropped; a cut left
    # in retitle_raw broke sanitize_summary's tr ("Illegal byte sequence").
    # No spaces near the cut, so there is no word boundary to back off to.
    CHARS = ("\U0001F680", "—", "é")

    def test_utf8_straddling_200(self):
        for ch in self.CHARS:
            for offset in (197, 198, 199):
                with self.subTest(ch=ch, offset=offset):
                    self.state.unlink(missing_ok=True)
                    self.feed(ON_TOPIC[0])
                    self.assertIn("drift", self.feed(
                        "x" * offset + ch * 3 + " invoice pdf export margins footer pages printed"))
                    text = self.fields()["cand"][0].split("\t", 1)[1]    # strict UTF-8 decode
                    self.assertLessEqual(len(text.encode()), 200)
                    self.assertGreaterEqual(len(text.encode()), 196)     # a byte cut, not a back-off

    def test_utf8_straddling_300(self):
        for ch in self.CHARS:
            for l1 in range(110, 114):   # every alignment of a 2/3/4-byte char against byte 300
                with self.subTest(ch=ch, l1=l1):
                    self.state.unlink(missing_ok=True)
                    self.feed(ON_TOPIC[0])
                    p1 = (DRIFT[0] + " " + "q" * 300)[:l1]
                    p2 = ch * 120 + " invoice pdf export margins footer pages printed"
                    self.feed(p1)
                    self.feed(p2)
                    self.assertIn("RETITLE", self.feed(DRIFT[2]))
                    raw = self.get("retitle_raw")                        # strict UTF-8 decode
                    self.assertTrue(raw.startswith(p1 + " / " + ch))
                    self.assertLessEqual(len(raw.encode()), 300)
                    self.assertGreaterEqual(len(raw.encode()), 297)

    def test_utf8_straddling_600(self):
        for ch in self.CHARS:
            for offset in (597, 598, 599):
                with self.subTest(ch=ch, offset=offset):
                    self.state.unlink(missing_ok=True)
                    self.feed(ON_TOPIC[0])
                    v = self.feed("websocket reconnect messages server " + "x" * (offset - 36) + ch * 200)
                    self.assertTrue(v.startswith("on"), v)
                    self.assertEqual(self.get("turns"), "2")

    def test_paste_beyond_max_chars_does_not_count(self):
        self.feed(ON_TOPIC[0])
        log = "websocket reconnect server restart messages " * 14   # > max_chars (600)
        paste = log + " ".join("kubernetes%d ingress%d grpc%d" % (i, i, i) for i in range(400))
        self.assertTrue(self.feed(paste).startswith("on"))
        self.assertTrue(all(len(b) < 600 for b in self.fields()["bag"]))

    def test_tunables_override(self):
        self.feed(ON_TOPIC[0])
        self.assertIn("RETITLE", self.feed(DRIFT[0], strikes_needed=1))

    def test_hostile_text_stays_one_line(self):
        self.feed(ON_TOPIC[0])
        self.feed("invoice\tpdf\rexport \x1b[31m margins \\n footer\x07 pages printed wrong")
        self.feed("invoice pdf export margins footer pages printed wrong again today")
        for line in self.state.read_text().split("\n")[:-1]:
            self.assertEqual(line.count("\t"), 2 if line.startswith("cand\t") else 1, line)
            self.assertFalse(any(ord(c) < 32 and c != "\t" for c in line), line)


FAKE_COPILOT = r'''#!/usr/bin/env python3
import os,re,sys,time
from pathlib import Path
r=Path(os.environ['FAKE_CONDENSER'])
p=sys.argv[sys.argv.index('-p')+1]
def first(t,n): return ' '.join(re.findall(r'[A-Za-z0-9]+',t)[:n])
if 'Current tab label: ' in p:
    prev=p.split('Current tab label: ',1)[1].split('\n',1)[0]
    msgs=p.split("The user's latest messages: ",1)[1].split('\n',1)[0]
    entry='JUDGE '+prev+' | '+msgs
    mode=(r/'judge').read_text().strip() if (r/'judge').exists() else 'same'
    ans={'same':prev,'same-lower':prev.lower()+'.','bad':'I think this is still the same work.'}.get(mode,'N '+first(msgs,2))
else:
    entry=p.rsplit('Title: ',1)[1]
    ans='I cannot label that.' if (r/'plainfail').exists() else 'C '+first(entry,2)
with (r/'calls').open('a') as f: f.write(entry+'\n')
while (r/'block').exists(): time.sleep(.02)
print(ans)
'''


class DriftHookTests(ci.IndicatorIntegrationTests):
    SID = "c39610c8-da42-4152-8abd-8e50f3a23b27"

    def setUp(self):
        super().setUp()
        shutil.copyfile(AWK_SOURCE, self.root / "agent-topic-drift.awk")
        (self.bin / "copilot").write_text(FAKE_COPILOT)
        self.tp = self.root / "t.jsonl"
        self.tp.write_text("")
        self.drift_dir = self.root / ".cache/agent-tab/drift"

    def lines(self, *objs):
        with self.tp.open("a") as f:
            for o in objs:
                f.write(json.dumps(o, separators=(",", ":")) + "\n")

    def ai(self, t):
        self.lines({"type": "ai-title", "aiTitle": t, "sessionId": self.SID})

    def custom(self, t):
        self.lines({"type": "custom-title", "customTitle": t, "sessionId": self.SID})

    def pad(self, kb=80):
        """Push earlier title lines out of the hook's 64KB tail."""
        for _ in range(kb):
            self.lines({"type": "user", "message": {"content": "x" * 1000}})

    def claude(self, mode, **payload):
        payload.setdefault("transcript_path", str(self.tp))
        return self.hook(mode, sid=self.SID, agent="claude", **payload)

    def drift_state(self, **kv):
        self.drift_dir.mkdir(parents=True, exist_ok=True)
        body = "v\t1\nturns\t3\nstrikes\t0\nretitle_at\t1\n" + "".join("%s\t%s\n" % i for i in kv.items())
        (self.drift_dir / self.SID).write_text(body)

    def state_fields(self):
        f = {}
        for line in (self.drift_dir / self.SID).read_text().splitlines():
            k, _, v = line.partition("\t")
            f[k] = v
        return f

    def calls(self):
        p = self.cond / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def judge(self, mode):
        (self.cond / "judge").write_text(mode)

    # --- custom-title (/rename) ---------------------------------------------

    def test_custom_title_beats_ai_title_and_is_not_condensed(self):
        self.drift_state()
        self.ai("Claude app window space detection")
        self.custom("drift-detector-hook")
        self.ai("Claude app window space detection")  # re-appended later: still loses
        w = self.claude("done", last_assistant_message="ok")
        self.assertEqual(w["@agent_summary"], "project/drift detector hook")
        self.assertEqual(w["@agent_summary_cond"], "1")
        time.sleep(.3)
        self.assertEqual(self.calls(), [])

    def test_custom_title_display_rules(self):
        self.drift_state()
        self.custom("a-very-long-generated-session-name-here")
        self.assertEqual(self.claude("done")["@agent_summary"], "project/a very long generated")
        self.custom("Hand typed name with-dash")   # has spaces: shown as typed
        self.assertEqual(self.claude("done")["@agent_summary"], "project/Hand typed name")
        self.custom("snake_case_name")
        self.assertEqual(self.claude("done")["@agent_summary"], "project/snake case name")

    def test_latest_custom_title_wins(self):
        self.drift_state()
        self.custom("first-name")
        self.custom("second-name")
        self.assertEqual(self.claude("done")["@agent_summary"], "project/second name")

    def test_custom_title_untrusted_without_state(self):
        # SessionStart before any prompt: a custom-title can't be classified yet.
        self.ai("Claude app window space detection")
        self.custom("drift-detector-hook")
        self.claude("done")
        self.assertEqual(self.summary_after("project/C Claude app"), "project/C Claude app")

    def test_ai_title_still_used_without_custom(self):
        self.ai("Claude app window space detection")
        self.claude("done")
        self.assertEqual(self.summary_after("project/C Claude app"), "project/C Claude app")

    def test_tasks_launcher_slug_is_not_a_rename(self):
        # CuaNotch Tasks: launcher slug as custom-title at line 2, no ai-title.
        self.lines({"type": "permission-mode", "permissionMode": "default"})
        self.custom("check-five-variable-solu")
        w = self.claude("idle", source="startup")
        self.assertEqual(w["@agent_summary"], "project/New Session")
        self.claude("running", prompt="check the five variable solutions in the algebra worksheet")
        self.assertEqual(self.summary_after("project/C check the"), "project/C check the")
        self.assertEqual(self.state_fields()["launch_custom"], "check-five-variable-solu")
        self.assertEqual(self.claude("done")["@agent_summary"], "project/C check the")
        # A real /rename later is the user's.
        self.custom("algebra-worksheet")
        self.assertEqual(self.claude("done")["@agent_summary"], "project/algebra worksheet")

    def set_label(self, label):
        state = json.loads(self.state.read_text())
        state["windows"]["@7"].update({"@agent_summary": label, "@agent_summary_cond": "1"})
        self.state.write_text(json.dumps(state))

    def test_title_lines_outside_tail_fall_back_to_state(self):
        self.drift_state(seen_custom="drift-detector-hook")
        self.custom("drift-detector-hook")
        self.pad()
        self.assertEqual(self.claude("done")["@agent_summary"], "project/drift detector hook")
        # seen_ai: written by the running hook, read when the tail has no ai-title.
        (self.drift_dir / self.SID).unlink()
        self.tp.write_text("")
        self.ai("Claude app window space detection")
        self.claude("running", prompt="yes")
        self.assertEqual(self.state_fields()["seen_ai"], "Claude app window space detection")
        self.assertEqual(self.summary_after("project/C Claude app"), "project/C Claude app")
        self.pad()
        self.set_label("project/something else")
        self.assertEqual(self.claude("done")["@agent_summary"], "project/C Claude app")   # cache hit

    # --- retitle ------------------------------------------------------------

    def test_retitle_without_prev_uses_plain_condense(self):
        self.ai("Claude app window space detection")
        self.custom("drift-detector-hook")
        self.drift_state(retitle_raw="port the watcher to rust", retitle_custom="drift-detector-hook",
                         seen_custom="drift-detector-hook")
        self.claude("done")
        self.assertEqual(self.summary_after("project/C port the"), "project/C port the")
        self.assertIn("port the watcher to rust", self.calls())

    def test_retitle_judge_keeps_label(self):
        self.ai("Claude app window space detection")
        self.drift_state(retitle_raw="port the watcher to rust", retitle_prev="Space detection")
        self.judge("same")
        w = self.claude("done")
        self.assertNotIn("port the watcher", w.get("@agent_summary", ""))   # never painted raw
        self.wait_for("calls")
        time.sleep(.3)
        self.assertEqual(self.calls(), ["JUDGE Space detection | port the watcher to rust"])
        self.assertEqual(self.summary_after("project/Space detection"), "project/Space detection")
        self.assertEqual(self.window()["@agent_summary_cond"], "1")
        # Cached under the composed input: later events make no new call.
        self.assertEqual(self.claude("idle", source="resume")["@agent_summary"], "project/Space detection")
        self.assertEqual(len(self.calls()), 1)

    def test_retitle_judge_names_new_task(self):
        self.drift_state(retitle_raw="port the watcher to rust", retitle_prev="Space detection")
        self.judge("new")
        self.claude("done")
        self.assertEqual(self.summary_after("project/N port the"), "project/N port the")

    def test_retitle_judge_keeps_label_valid_short_would_refuse(self):
        # A stored prev is always a CONDENSED label (raw stand-ins are never
        # recorded, see test_raw_label_gets_plain_condense), but a condensed
        # label can still fail valid_short: a 5-word /rename display.
        prev = "port the watcher to rust"   # 5 words, 24 chars
        self.drift_state(retitle_raw="also bump the tokio version in the watcher crate", retitle_prev=prev)
        self.judge("same-lower")   # case and a trailing period differ
        self.claude("done")
        self.assertEqual(self.summary_after("project/" + prev), "project/" + prev)

    def test_retitle_judge_garbage_keeps_old_label(self):
        self.set_label("project/Space detection")
        self.drift_state(retitle_raw="port the watcher to rust", retitle_prev="Space detection")
        self.judge("bad")
        self.claude("done")
        self.wait_for("calls")
        time.sleep(.5)
        self.assertEqual(self.window()["@agent_summary"], "project/Space detection")

    def test_rename_after_retitle_beats_retitle(self):
        self.drift_state(retitle_raw="port the watcher to rust", retitle_custom="drift-detector-hook",
                         retitle_prev="Space detection")
        self.custom("drift-detector-hook")
        self.custom("rust-watcher-port")
        w = self.claude("done")
        self.assertEqual(w["@agent_summary"], "project/rust watcher port")
        self.assertEqual(w["@agent_summary_cond"], "1")
        # A rename with no custom at retitle time wins too.
        self.drift_state(retitle_raw="port the watcher to rust")
        self.assertEqual(self.claude("idle", source="resume")["@agent_summary"], "project/rust watcher port")

    def test_running_hook_drift_end_to_end(self):
        self.ai("Fix websocket reconnect bug")
        self.claude("running", prompt=ON_TOPIC[0])
        self.assertEqual(self.summary_after("project/C Fix websocket"), "project/C Fix websocket")
        self.assertIn("bag\twebsocket reconnect", (self.drift_dir / self.SID).read_text())
        (self.cond / "block").touch()
        self.judge("new")
        for p in DRIFT:
            self.claude("running", prompt=p)
        f = self.state_fields()
        self.assertTrue(f["retitle_raw"].startswith(DRIFT[0]))
        self.assertEqual(f["retitle_prev"], "C Fix websocket")
        deadline = time.monotonic() + 5
        while not any(c.startswith("JUDGE") for c in self.calls()) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(any(c.startswith("JUDGE C Fix websocket | lowk can") for c in self.calls()), self.calls())
        # The old label stays up while the judge is out: no raw flash.
        self.assertEqual(self.window()["@agent_summary"], "project/C Fix websocket")
        self.assertEqual(self.window()["@agent_summary_cond"], "1")
        (self.cond / "block").unlink()
        self.assertEqual(self.summary_after("project/N lowk can"), "project/N lowk can")
        n = len(self.calls())
        self.assertEqual(self.claude("done")["@agent_summary"], "project/N lowk can")
        self.assertEqual(self.claude("idle", source="resume")["@agent_summary"], "project/N lowk can")
        self.assertEqual(len(self.calls()), n)

    def test_raw_label_gets_plain_condense(self):
        # The condenser is failing, so the tab shows a RAW stand-in
        # (@agent_summary_cond unset). A retitle must not hand that to the
        # judge's "keep it if it's the same work": no retitle_prev, plain prompt.
        (self.cond / "plainfail").touch()
        self.ai("Fix websocket reconnect bug")
        self.claude("running", prompt=ON_TOPIC[0])
        self.wait_for("calls")
        time.sleep(.3)
        self.assertNotEqual(self.window().get("@agent_summary_cond"), "1")
        for p in DRIFT:
            self.claude("running", prompt=p)
        f = self.state_fields()
        self.assertIn("retitle_raw", f)
        self.assertNotIn("retitle_prev", f)
        deadline = time.monotonic() + 20   # detached condenser; generous under a loaded machine
        while not any(c.startswith("lowk can") for c in self.calls()) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(any(c.startswith("lowk can") for c in self.calls()), self.calls())
        self.assertFalse([c for c in self.calls() if c.startswith("JUDGE")])

    def test_retitle_prev_stored_sanitized(self):
        self.ai("Fix websocket reconnect bug")
        self.claude("running", prompt=ON_TOPIC[0])
        self.summary_after("project/C Fix websocket")
        self.claude("running", prompt=DRIFT[0])
        self.claude("running", prompt=DRIFT[1])
        self.set_label('project/Space "det"ection #1')   # each hook repaints; set just before the trigger
        self.claude("running", prompt=DRIFT[2])
        self.assertEqual(self.state_fields()["retitle_prev"], "Space detection 1")

    def test_tasks_slug_out_of_tail_at_first_running(self):
        # Long-lived Tasks session (live at ship time): the slug is outside the
        # 64KB tail when the drift state is first created, and re-appended later.
        self.custom("check-five-variable-solu")
        self.ai("Claude app window space detection")
        self.pad()
        self.claude("running", prompt="check the five variable solutions in the algebra worksheet")
        self.assertEqual(self.state_fields()["launch_custom"], "check-five-variable-solu")
        self.custom("check-five-variable-solu")   # Claude re-appends it
        self.claude("running", prompt="yes")
        w = self.claude("done")
        self.assertNotEqual(w.get("@agent_summary"), "project/check five variable",
                            "launcher slug shown as a rename")

    def test_hook_survives_utf8_straddling_prompt(self):
        self.ai("Fix websocket reconnect bug")
        self.claude("running", prompt=ON_TOPIC[0])
        for ch in ("\U0001F680", "—"):
            for offset in (598, 599):
                before = int(self.state_fields()["turns"])
                self.claude("running", prompt="websocket reconnect messages " + "x" * (offset - 29) + ch * 300)
                self.assertEqual(int(self.state_fields()["turns"]), before + 1, (ch, offset))

    def test_rename_reseeds_detector(self):
        self.ai("Fix websocket reconnect bug")
        self.claude("running", prompt=ON_TOPIC[0])
        self.custom("invoice-pdf-margins")
        self.claude("running", prompt="yes")
        st = (self.drift_dir / self.SID).read_text()
        self.assertIn("bag\tinvoice pdf margin\n", st)
        self.assertIn("seen_custom\tinvoice-pdf-margins\n", st)

    def test_bad_session_id_writes_no_state(self):
        self.hook("running", sid="../../etc/x", agent="claude", prompt=ON_TOPIC[0],
                  transcript_path=str(self.tp))
        self.assertFalse(self.drift_dir.exists() and any(self.drift_dir.iterdir()))

    def test_missing_detector_is_harmless(self):
        (self.root / "agent-topic-drift.awk").unlink()
        w = self.claude("running", prompt=ON_TOPIC[0])
        self.assertEqual(w["@agent_state"], "running")
        self.assertFalse(self.drift_dir.exists() and any(self.drift_dir.iterdir()))

    def test_session_start_gcs_old_state(self):
        self.drift_dir.mkdir(parents=True)
        old, new = self.drift_dir / "aaaa-old", self.drift_dir / "bbbb-new"
        old.write_text("v\t1\n")
        new.write_text("v\t1\n")
        t = time.time() - 40 * 86400
        os.utime(old, (t, t))
        self.claude("idle", source="startup")
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


# Keep the harness's own tests from running twice.
for _name in [n for n in dir(ci.IndicatorIntegrationTests) if n.startswith("test_")]:
    setattr(DriftHookTests, _name, None)


if __name__ == "__main__":
    unittest.main()

"""closed-tabs.sh's picker, DRIVEN: the real fzf in a pane of a private tmux
server, keys typed into it with send-keys, and the history checked for which
entry the key acted on.

The bug it pins (2026-11-13): with `load:pos(2)`, a typed filter left the
cursor on its SECOND match, so ⏎ / ⌃x / ⌃z acted on a row nobody picked (⌃x
forgot the wrong entry). Now the list loads with the cursor on line 2 (the
first row under the first heading) and any query change, or a reload landing
with a query, puts it on the top match.

The server is test_closed_tabs_pin's ServerBase (`-L closed-pin-<uuid>`,
`-f /dev/null`, its own TMUX_TMPDIR, killed in teardown); the picker's own
hand-off (`tmux run-shell -b … pick-act`) runs on that server with the test's
history, stash.sh and snooze.sh fakes. The rows are labelled so the two
"alpha" matches tie on fzf's score and length and rank by list order.
"""
import shutil
import time
import unittest

from test_closed_tabs_pin import SCRIPT, TMUX, ServerBase, entry


@unittest.skipUnless(TMUX and shutil.which("jq") and shutil.which("fzf"), "tmux / jq / fzf not installed")
class DrivenPicker(ServerBase):
    def setUp(self):
        super().setUp()
        now = int(time.time())
        # Oldest first in the file; the Backlog lists them newest first:
        # beta zzz, alpha aaa, alpha bbb.
        self.write(entry("bbb", now - 120, label="alpha bbb", cwd=self.tmp),
                   entry("aaa", now - 60, label="alpha aaa", cwd=self.tmp),
                   entry("zzz", now - 10, label="beta zzz", cwd=self.tmp))

    def wait(self, f, what, timeout=8.0):
        end = time.time() + timeout
        while True:
            v = f()
            if v:
                return v
            if time.time() > end:
                self.fail(f"timed out waiting for {what}; screen:\n{self.screen()}")
            time.sleep(0.05)

    def screen(self):
        return self.t("capture-pane", "-p", "-t", self.pane)

    def open_picker(self, *args):
        self.pane = self.t("new-window", "-d", "-t", "=main:", "-P", "-F", "#{pane_id}",
                           f"bash '{SCRIPT}' pick {' '.join(args)} main ''; exec sleep 600")
        self.wait(lambda: "Backlog" in self.screen(), "the picker")

    def type_(self, text, matches):
        self.t("send-keys", "-t", self.pane, "-l", text)
        self.wait(lambda: f" {matches}/" in self.screen(), f"{matches} matches for {text!r}")

    def key(self, k):
        self.t("send-keys", "-t", self.pane, k)

    def ids(self):
        return sorted(e["id"] for e in self.entries())

    def test_ctrl_x_after_a_filter_forgets_the_top_match(self):
        self.open_picker()
        self.type_("alpha", 2)
        self.key("C-x")
        self.wait(lambda: self.ids() != ["aaa", "bbb", "zzz"], "the forget")
        self.assertEqual(self.ids(), ["bbb", "zzz"])

    def test_enter_with_no_query_takes_the_first_row_under_the_heading(self):
        self.open_picker()
        count = lambda: len(self.t("list-windows", "-t", "=main", "-F", "#{window_id}").split())
        before = count()                                             # the picker's own window included
        self.key("Enter")
        # The entry leaves the history before its window is made: wait for both.
        self.wait(lambda: self.ids() != ["aaa", "bbb", "zzz"], "the reopen")
        self.assertEqual(self.ids(), ["aaa", "bbb"])                 # zzz, the newest close
        self.wait(lambda: count() == before + 1, "the reopened window")

    def test_a_reload_with_a_query_keeps_the_cursor_on_the_top_match(self):
        self.open_picker()
        self.type_("alpha", 2)
        self.key("C-s")                                  # pins "alpha aaa", reloads in place
        self.wait(lambda: any(e.get("pinned") for e in self.entries()), "the pin")
        self.assertEqual([e["id"] for e in self.entries() if e.get("pinned")], ["aaa"])
        # The reloaded list (the query hides the headings): aaa, now stashed
        # and listed first, is the top match and the cursor's row.
        self.wait(lambda: " 2/5 " in self.screen(), "the reloaded list (two headings, three rows)")
        self.key("Enter")
        self.wait(lambda: len(self.entries()) == 2, "the reopen")
        self.assertEqual(self.ids(), ["bbb", "zzz"])


if __name__ == "__main__":
    unittest.main()

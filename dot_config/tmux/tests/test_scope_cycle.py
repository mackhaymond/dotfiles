"""Option-V (spaces.conf's M-v): the agent sidebar's scope is a CYCLE,
this space -> active -> all spaces -> this space, persisted in the global
@strip_scope (agent-ui's Scope::flipped cycles the same). Unset counts as
"space" while spaces are on; an unknown value goes back to "space".

Drives the real binding on test_spaces' private `-L` server (spaces.conf
sourced, one attached client, killed in cleanup). Run with unittest or
pytest.
"""
import unittest

import test_spaces as ts


@unittest.skipUnless(ts.TMUX, "tmux not installed")
class ScopeCycle(ts.Server):
    def scope(self):
        return self.t("show", "-gqv", "@strip_scope")

    def test_option_v_cycles_three_scopes(self):
        self.attach()
        self.assertEqual(self.scope(), "")
        for want in ["active", "all", "space", "active"]:
            self.key("M-v")
            self.wait(self.scope, want)

    def test_unknown_value_goes_back_to_this_space(self):
        self.attach()
        self.t("set", "-g", "@strip_scope", "junk")
        self.key("M-v")
        self.wait(self.scope, "space")


if __name__ == "__main__":
    unittest.main()

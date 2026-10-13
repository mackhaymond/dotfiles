import os

# The tab scripts post a Darwin notification (com.mackhaymond.tmux.agent-state,
# the STATE NOTICE) on every state change, and the live CuaNotch re-reads the
# real tmux server on each one. Tests never post it: an empty name turns the
# notice off. Harnesses that build their env from scratch set it themselves.
os.environ["AGENT_STATE_NOTICE"] = ""

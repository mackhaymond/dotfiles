#!/usr/bin/env python3
"""Compile skhdrc into Karabiner-Elements rules, so Karabiner (not skhd) fires every bind.

WHY: skhd listens through a CGEventTap, and macOS blinds every event tap while Secure
Event Input is on. Browsers turn Secure Input on for password fields (and leave it
stuck on sometimes -- verified 2026-09-23: an agent-launched Chrome held it for hours),
so hyper+z/x etc. silently died "in Chrome and Arc". Karabiner grabs the keyboard at the
HID level, below Secure Input, below every app: a bind matched there cannot be swallowed.

skhdrc stays the declarative SOURCE OF TRUTH for the bind list; this compiles it.
Invoked by chezmoi's modify_ script for ~/.config/karabiner/karabiner.json:

    skhd-to-karabiner.py BASE_JSON < skhdrc  > karabiner.json

BASE_JSON is the hand-written Karabiner config (.chezmoitemplates/karabiner-base.json).
Output = BASE_JSON with
  1. a generated rule prepended, one manipulator per `<mods> - <key> : <cmd>` bind, and
  2. every base manipulator whose sole output is a bare key that skhdrc binds (the
     F13/F14/F18/F19 chords) rewritten to run that bind's command directly -- Karabiner
     never re-processes its own output, so an emitted F18 could only ever reach skhd.
Unknown modifiers / keys are a hard error: a bind must never be dropped silently.
"""
import json
import os
import pwd
import re
import sys

GENERATED_DESCRIPTION = "skhd binds, run by Karabiner (GENERATED from ~/.config/skhd/skhdrc -- edit that)"

# skhd modifier -> Karabiner mandatory modifiers (side-agnostic, as skhd's are).
MODIFIERS = {
    "hyper": ["command", "control", "option", "shift"],
    "meh": ["control", "option", "shift"],
    "cmd": ["command"],
    "alt": ["option"],
    "ctrl": ["control"],
    "shift": ["shift"],
    "fn": ["fn"],
}

# macOS virtual keycodes skhdrc uses in hex -> Karabiner key_code (ANSI).
HEX_KEYS = {
    0x32: "grave_accent_and_tilde",
    0x2A: "backslash",
    0x21: "open_bracket",
    0x1E: "close_bracket",
    0x29: "semicolon",
    0x27: "quote",
    0x2C: "slash",
    0x2B: "comma",
    0x2F: "period",
    0x1B: "hyphen",
    0x18: "equal_sign",
}

NAMED_KEYS = {
    "tab": "tab", "space": "spacebar", "return": "return_or_enter", "escape": "escape",
    "backspace": "delete_or_backspace", "delete": "delete_forward",
    "left": "left_arrow", "right": "right_arrow", "up": "up_arrow", "down": "down_arrow",
}

BIND_RE = re.compile(r"^\s*(?:(?P<mods>[a-z]+(?:\s*\+\s*[a-z]+)*)\s*-\s*)?(?P<key>0x[0-9A-Fa-f]+|[a-z0-9]+)\s*:\s*(?P<cmd>.+)$")

# The console-user-server that runs shell_command has launchd's bare environment.
ENV_PREFIX = (
    'export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"'
    ' HOME="{home}" USER="{user}"; '
)


def strip_comment(cmd):
    """Drop a trailing shell comment (` # ...` outside quotes) so the command can be wrapped."""
    quote = None
    for i, ch in enumerate(cmd):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or cmd[i - 1].isspace()):
            return cmd[:i].rstrip()
    return cmd.strip()


def karabiner_key(key, lineno):
    if key.startswith("0x"):
        code = int(key, 16)
        if code not in HEX_KEYS:
            sys.exit(f"skhd-to-karabiner: line {lineno}: unmapped keycode {key} (add it to HEX_KEYS)")
        return HEX_KEYS[code]
    if re.fullmatch(r"[a-z0-9]", key) or re.fullmatch(r"f([1-9]|1[0-9]|20)", key):
        return key
    if key in NAMED_KEYS:
        return NAMED_KEYS[key]
    sys.exit(f"skhd-to-karabiner: line {lineno}: unknown key {key!r}")


def parse_skhdrc(text):
    binds = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = BIND_RE.match(line)
        if not m:
            sys.exit(f"skhd-to-karabiner: line {lineno}: can't parse bind: {line!r}")
        mods = []
        for mod in re.split(r"\s*\+\s*", m["mods"]) if m["mods"] else []:
            if mod not in MODIFIERS:
                sys.exit(f"skhd-to-karabiner: line {lineno}: unknown modifier {mod!r}")
            mods += [x for x in MODIFIERS[mod] if x not in mods]
        binds.append((mods, karabiner_key(m["key"], lineno), strip_comment(m["cmd"]), line.strip()))
    return binds


def shell_command(cmd):
    # Backgrounded like skhd's fork/exec: a slow script never queues the next bind.
    # From the passwd entry, not $USER/os.getlogin(): chezmoi may run with a bare env
    # (no USER), where getlogin() returns "root" and yabai's socket lookup would break.
    pw = pwd.getpwuid(os.getuid())
    prefix = ENV_PREFIX.format(home=pw.pw_dir, user=pw.pw_name)
    return f"{prefix}({cmd}) >/dev/null 2>&1 &"


def main():
    with open(sys.argv[1]) as f:
        config = json.load(f)
    binds = parse_skhdrc(sys.stdin.read())

    bare = {key: cmd for mods, key, cmd, _ in binds if not mods}
    manipulators = [
        {
            "description": src,
            "type": "basic",
            # No `optional`: an exact modifier match, like skhd (hyper-z must not also fire on hyper+fn+z).
            "from": {"key_code": key, "modifiers": {"mandatory": mods}},
            "to": [{"shell_command": shell_command(cmd)}],
        }
        for mods, key, cmd, src in binds if mods
    ]
    # fn-layer binds first, so a less specific bind can never shadow a more specific one.
    manipulators.sort(key=lambda m: -len(m["from"]["modifiers"]["mandatory"]))

    used_bare = set()
    for profile in config["profiles"]:
        rules = profile.setdefault("complex_modifications", {}).setdefault("rules", [])
        for rule in rules:
            for manip in rule["manipulators"]:
                to = manip.get("to", [])
                if len(to) == 1 and set(to[0]) == {"key_code"} and to[0]["key_code"] in bare:
                    key = to[0]["key_code"]
                    manip["to"] = [{"shell_command": shell_command(bare[key])}]
                    used_bare.add(key)
        rules.insert(0, {"description": GENERATED_DESCRIPTION, "manipulators": manipulators})

    orphans = sorted(set(bare) - used_bare)
    if orphans:
        sys.exit(f"skhd-to-karabiner: bare-key binds {orphans} have no Karabiner chord producing them")

    json.dump(config, sys.stdout, indent=4)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

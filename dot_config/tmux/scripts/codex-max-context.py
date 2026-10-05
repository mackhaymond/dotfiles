#!/usr/bin/env python3
"""Request model-specific maximum context using Codex's native model catalog."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def maximum_request(catalog):
    # Codex clamps this request to the selected model's max_context_window,
    # including later /model switches. Requesting only the launch model's limit
    # would prevent a switch from a smaller model to a larger one from growing.
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise ValueError("invalid model catalog")
    maxima = [model.get("max_context_window") for model in catalog["models"] if isinstance(model, dict)]
    valid = [value for value in maxima if isinstance(value, int) and not isinstance(value, bool) and value > 0]
    if not valid:
        raise ValueError("catalog has no advertised context maxima")
    return max(valid)


def launch_args(codex, args):
    # The native CLI handles authentication, account identity, refresh, and cache.
    output = subprocess.check_output([codex, "debug", "models"], text=True,
                                     stderr=subprocess.DEVNULL, timeout=8)
    maximum = maximum_request(json.loads(output))
    # Explicit user options appear last and retain their normal precedence.
    return ["-c", f"model_context_window={maximum}"] + args


def main():
    if sys.argv[1:2] == ["launch"]:
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location(
            "codex_terminal_owner", Path(__file__).with_name("codex-terminal-owner.py"))
        owner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(owner)
        args = sys.argv[2:]
        needs_context = owner.interactive_args(args) or any(arg in {"exec", "e", "review"} for arg in args)
        if needs_context and not any(arg in {"--help", "-h", "--version", "-V"} for arg in args):
            try:
                args = launch_args(owner.real_codex(), args)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                print("codex: maximum context unavailable; using the model's native default", file=sys.stderr)
        owner.launch(args)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    args = parser.parse_args()
    print(json.dumps(launch_args(args.codex, [])))


if __name__ == "__main__":
    main()

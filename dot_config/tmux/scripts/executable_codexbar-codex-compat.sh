#!/bin/bash
# CodexBar 0.53 starts its usage RPC with an approval policy removed in
# Codex 0.160. Adapt only that exact read-only probe; normal Codex launches
# and every other argument list pass through unchanged.
set -euo pipefail

if [[ $# == 5 && "$1" == '-s' && "$2" == 'read-only' &&
      "$3" == '-a' && "$4" == 'untrusted' && "$5" == 'app-server' ]]; then
  set -- -s read-only -a on-request app-server
fi

exec "${CODEXBAR_CODEX_REAL_CLI:?usage fetcher must supply the Codex executable}" "$@"

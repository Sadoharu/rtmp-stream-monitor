#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-server}"
case "$MODE" in
  server) exec "$ROOT/install-server.sh" ;;
  agent)
    if [[ $# -lt 2 ]]; then
      echo "Usage: sudo ./install.sh agent /path/to/agent.yaml" >&2
      exit 2
    fi
    exec "$ROOT/install-agent.sh" "$2"
    ;;
  *) echo "Usage: sudo ./install.sh [server | agent /path/to/agent.yaml]" >&2; exit 2 ;;
esac

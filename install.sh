#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-server}"
case "$MODE" in
  server) exec bash "$ROOT/install-server.sh" ;;
  agent)
    shift
    exec bash "$ROOT/install-agent.sh" "$@"
    ;;
  *) echo "Usage: sudo ./install.sh [server | agent --server https://monitor.example.net | agent --config /path/to/agent.yaml]" >&2; exit 2 ;;
esac

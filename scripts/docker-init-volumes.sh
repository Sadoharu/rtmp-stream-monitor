#!/usr/bin/env bash
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
target_uid="$(sed -n 's/^[[:space:]]*RTMP_MONITOR_UID=//p' .env | tail -n 1)"
target_gid="$(sed -n 's/^[[:space:]]*RTMP_MONITOR_GID=//p' .env | tail -n 1)"
# Match the Compose service defaults when setup has not created a .env file.
target_uid="${target_uid:-10001}"
target_gid="${target_gid:-10001}"

docker compose run --rm --no-deps -T --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE \
  -e RTMP_MONITOR_INIT_UID="$target_uid" \
  -e RTMP_MONITOR_INIT_GID="$target_gid" \
  --entrypoint python central -m rtmp_monitor.docker_migration \
  --data-dir /data \
  --legacy-data-dir /migration-data \
  --logs-dir /logs \
  --legacy-logs-dir /migration-logs \
  --uid "$target_uid" \
  --gid "$target_gid"

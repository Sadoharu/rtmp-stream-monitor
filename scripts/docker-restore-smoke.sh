#!/usr/bin/env bash
set -euo pipefail
export MSYS_NO_PATHCONV=1

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temp_root="$(mktemp -d)"
test_root="$temp_root/repo"
project_name="rtmp-restore-smoke-$$"
mkdir -p "$test_root"
(cd "$repo_root" && git archive HEAD) | tar -x -C "$test_root"
cd "$test_root"
export COMPOSE_PROJECT_NAME="$project_name"

cleanup() {
  result=$?
  trap - EXIT
  if [[ $result -ne 0 ]]; then
    docker compose logs --tail=100 central >&2 || true
  fi
  docker compose down --volumes --remove-orphans --timeout 10 >/dev/null 2>&1 || true
  rm -rf -- "$temp_root"
  exit "$result"
}
trap cleanup EXIT

python_command=python3
if ! python3 -c 'import sys' >/dev/null 2>&1; then
  python_command=python
fi
port="$("$python_command" - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
mkdir -p secrets backups smoke-input/data smoke-input/logs
: > secrets/openai_api_key
chmod 600 secrets/openai_api_key
cat > .env <<EOF
RTMP_MONITOR_PORT=$port
RTMP_MONITOR_BIND_HOST=127.0.0.1
RTMP_MONITOR_UID=10001
RTMP_MONITOR_GID=10001
RTMP_MONITOR_LEGACY_DATA_DIR=./smoke-input/data
RTMP_MONITOR_LEGACY_LOG_DIR=./smoke-input/logs
EOF
chmod 600 .env

docker compose config -q
echo "Building central image for isolated Compose project $project_name."
docker compose build central
echo "Initializing the isolated named volumes."
bash ./scripts/docker-init-volumes.sh
docker compose up -d central

base_url="http://127.0.0.1:$port"
healthy=false
for attempt in $(seq 1 45); do
  if curl --fail --silent --show-error "$base_url/healthz" >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
done
if [[ "$healthy" != true ]]; then
  echo "Central service did not become healthy at $base_url." >&2
  exit 1
fi
echo "Central health endpoint became ready. Checking its SQLite volume as the service user."
docker compose exec -T central python -c \
  'import sqlite3; db=sqlite3.connect("/data/central.db"); db.execute("BEGIN IMMEDIATE"); db.rollback(); db.close(); print("SQLite volume write check passed.")'

admin_token="$(docker compose exec -T central cat /data/admin.token | tr -d '\r\n')"
if [[ -z "$admin_token" ]]; then
  echo "Central did not create an admin token." >&2
  exit 1
fi
echo "Read the generated admin token; creating the pre-backup stream through the authenticated API."

create_stream() {
  local stream_id="$1"
  curl --fail --silent --show-error \
    -H "Authorization: Bearer $admin_token" \
    -H 'Content-Type: application/json' \
    -X POST "$base_url/api/v1/streams" \
    --data "{\"id\":\"$stream_id\",\"name\":\"$stream_id\",\"local_url\":\"rtmp://127.0.0.1:1935/live/$stream_id\",\"public_url\":\"rtmp://127.0.0.1:1935/live/$stream_id\"}" >/dev/null
}

assert_stream_ids() {
  curl --fail --silent --show-error \
    -H "Authorization: Bearer $admin_token" \
    "$base_url/api/v1/streams" |
    "$python_command" -c 'import json,sys; actual=sorted(row["id"] for row in json.load(sys.stdin)); expected=sorted(sys.argv[1:]); assert actual == expected, f"expected streams {expected}, got {actual}"' "$@"
}

create_stream restore_keep
assert_stream_ids restore_keep
echo "Pre-backup API state is ready; creating a verified backup."
bash ./scripts/docker-backup.sh
backup="$(find backups -maxdepth 1 -type f -name 'central-*.tar.gz' -print -quit)"
if [[ -z "$backup" ]]; then
  echo "Backup script did not create a central-*.tar.gz bundle." >&2
  exit 1
fi

create_stream restore_remove
assert_stream_ids restore_keep restore_remove
echo "Post-backup API mutation is ready; restoring the earlier backup."
bash ./scripts/docker-restore.sh "$backup"
assert_stream_ids restore_keep
restored_token="$(docker compose exec -T central cat /data/admin.token | tr -d '\r\n')"
if [[ "$restored_token" != "$admin_token" ]]; then
  echo "Restore did not return the admin token paired with the backup database." >&2
  exit 1
fi

docker compose restart central
healthy=false
for attempt in $(seq 1 45); do
  if curl --fail --silent --show-error "$base_url/healthz" >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
done
if [[ "$healthy" != true ]]; then
  echo "Central service did not recover after restart." >&2
  exit 1
fi
assert_stream_ids restore_keep
echo "Docker Compose build, health, backup/restore, token pairing, and restart persistence passed."

#!/usr/bin/env bash
set -euo pipefail
export MSYS_NO_PATHCONV=1

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temp_root="$(mktemp -d)"
test_root="$temp_root/repo"
project_name="rtmp-migration-restore-smoke-$$"
rollback_container="rtmp-migration-rollback-$$"
mkdir -p "$test_root"
(cd "$repo_root" && git archive HEAD) | tar -x -C "$test_root"
cp "$repo_root/scripts/docker-restore-smoke.sh" "$test_root/scripts/docker-restore-smoke.sh"
cd "$test_root"
export COMPOSE_PROJECT_NAME="$project_name"

cleanup() {
  result=$?
  trap - EXIT
  if [[ $result -ne 0 ]]; then
    docker compose logs --tail=100 central >&2 || true
  fi
  docker rm -f "$rollback_container" >/dev/null 2>&1 || true
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
service_uid="$(id -u)"
service_gid="$(id -g)"
case "$(uname -s)" in
  MINGW*|MSYS*)
    # Git Bash's synthetic uid/gid do not map to Linux named-volume ownership.
    service_uid=10001
    service_gid=10001
    ;;
esac
service_uid="${RTMP_MONITOR_SMOKE_UID:-$service_uid}"
service_gid="${RTMP_MONITOR_SMOKE_GID:-$service_gid}"
if [[ ! "$service_uid" =~ ^[0-9]+$ || ! "$service_gid" =~ ^[0-9]+$ ]]; then
  echo "Compose smoke UID/GID must be non-negative integers." >&2
  exit 1
fi
echo "Compose smoke service identity: $service_uid:$service_gid"
mkdir -p secrets backups smoke-input/rollback-data
: > secrets/openai_api_key
chmod 600 secrets/openai_api_key
legacy_admin_token="legacy-admin-token-for-compose-smoke"
legacy_agent_token="legacy-agent-token-for-compose-smoke"
cat > .env <<EOF
RTMP_MONITOR_PORT=$port
RTMP_MONITOR_BIND_HOST=127.0.0.1
RTMP_MONITOR_UID=$service_uid
RTMP_MONITOR_GID=$service_gid
EOF
chmod 600 .env
cat > docker-compose.smoke.yml <<'YAML'
volumes:
  smoke-systemd-data:
  smoke-systemd-logs:

services:
  central:
    volumes:
      # Named volumes keep fixture I/O portable across Docker Desktop and
      # hosted Linux runners, including runners with restricted bind mounts.
      - smoke-systemd-data:/migration-data
      - smoke-systemd-logs:/migration-logs
YAML

compose_file_separator=":"
case "$(uname -s)" in
  MINGW*|MSYS*) compose_file_separator=";" ;;
esac
export COMPOSE_FILE="docker-compose.yml${compose_file_separator}docker-compose.smoke.yml"

docker compose config -q
echo "Building central image for isolated Compose project $project_name."
docker compose build central
echo "Creating an old-version systemd-style database and a verified migration snapshot."
docker compose run --rm --no-deps -T --user 0 --entrypoint python central - <<'PY'
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from rtmp_monitor.db import Agent, Base, Incident, Stream, Telemetry

source_db = Path("/migration-data/systemd/central.db")
source_db.parent.mkdir(parents=True, exist_ok=True)
Path("/migration-data/admin.token").write_text("legacy-admin-token-for-compose-smoke\n", encoding="utf-8")
Path("/migration-logs/central.jsonl").write_text('{"event":"legacy-systemd-log"}\n', encoding="utf-8")
engine = create_engine(f"sqlite:///{source_db}")
old_tables = [table for table in Base.metadata.sorted_tables if table.name != "probe_enrollments"]
Base.metadata.create_all(engine, tables=old_tables)
observed_at = datetime.now(timezone.utc) - timedelta(seconds=15)
agent_token = "legacy-agent-token-for-compose-smoke"
with Session(engine) as session:
    session.add(Stream(
        id="poland", name="Legacy Poland", local_url="rtmp://127.0.0.1/live/poland",
        public_url="rtmp://stream.example.net/live/poland", source_url="",
    ))
    session.add(Agent(
        id="legacy-agent", name="legacy-egress", location="server", platform="Ubuntu",
        role="SERVER_EGRESS", stream_id="poland",
        token_hash=hashlib.sha256(agent_token.encode()).hexdigest(), last_seen_at=observed_at,
    ))
    session.add(Telemetry(
        id="legacy-sample", agent_id="legacy-agent", stream_id="poland",
        observed_at=observed_at, received_at=observed_at, status="OK",
        metrics={"profile": "LIGHT", "received_media_bitrate_bps": 4_250_000,
                 "received_media_bitrate_quality": "MEASURED", "measurement_window_seconds": 1.0},
        events=[{"code": "FREEZE_START", "severity": "WARNING",
                 "timestamp": observed_at.isoformat(), "details": {"last_frame_age_seconds": 2.5}}],
        context={},
    ))
    session.add(Incident(
        id="legacy-incident", stream_id="poland", opened_at=observed_at,
        updated_at=observed_at, resolved_at=observed_at + timedelta(seconds=5),
        severity="WARNING", diagnosis="CLIENT_PATH_UNCONFIRMED",
        probable_location="CLIENT path is unconfirmed", affected_agents=["legacy-egress"],
        symptoms=[], context={}, active=False, fingerprint="legacy-incident-smoke",
    ))
    session.commit()
engine.dispose()
print("Created a stopped systemd-style SQLite fixture with legacy stream, probe, telemetry, and incident rows.")
PY

docker compose run --rm --no-deps -T --user 0 \
  --entrypoint python central -m rtmp_monitor.docker_migration \
  --snapshot-source /migration-data/systemd/central.db \
  --snapshot-target /migration-data/central.db

echo "Initializing the isolated Compose named volumes from the systemd snapshot."
bash ./scripts/docker-init-volumes.sh
echo "Checking initialized volumes as the configured service user."
docker compose run --rm --no-deps -T --user 0 --entrypoint python central -c \
  "import os; expected=($service_uid,$service_gid); paths=('/data','/logs'); owners=tuple((os.stat(path).st_uid,os.stat(path).st_gid) for path in paths); print('Initialized volume owners:', owners); assert all(owner==expected for owner in owners), f'expected volume owner {expected}, got {owners}'"
docker compose run --rm --no-deps -T --entrypoint python central -c \
  'import os; print(f"Runtime identity: {os.geteuid()}:{os.getegid()}"); paths=("/data", "/logs"); [(lambda p: (open(p, "w").close(), os.unlink(p)))(os.path.join(path, ".write-check")) for path in paths]; print("SQLite and log volume write checks passed.")'
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
docker compose exec -T central python -c \
  'import os; assert os.access("/backups", os.W_OK), "service user cannot write to the backup bind mount"; print("Backup bind mount write check passed.")'

admin_token="$(docker compose exec -T central cat /data/admin.token | tr -d '\r\n')"
if [[ -z "$admin_token" ]]; then
  echo "Central did not create an admin token." >&2
  exit 1
fi
if [[ "$admin_token" != "$legacy_admin_token" ]]; then
  echo "The migrated Compose service did not retain the systemd admin token." >&2
  exit 1
fi
echo "Compose retained the systemd admin token; checking migrated history and the existing agent token."

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

assert_stream_ids poland
docker compose exec -T central python - "$legacy_agent_token" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8090"
with open("/data/admin.token", encoding="utf-8") as stream:
    admin_token = stream.read().strip()

def get(path):
    request = Request(base + path, headers={"Authorization": f"Bearer {admin_token}"})
    with urlopen(request, timeout=5) as response:
        return json.load(response)

streams = get("/api/v1/streams")
agents = get("/api/v1/agents")
now = datetime.now(timezone.utc)
params = urlencode({"from": (now - timedelta(hours=1)).isoformat(), "to": now.isoformat()})
series = get(f"/api/v2/streams/poland/series?{params}")["series"]
events = get(f"/api/v2/streams/poland/events?{params}")["events"]
assert any(row["id"] == "poland" and row["name"] == "Legacy Poland" for row in streams)
assert any(row["name"] == "legacy-egress" for row in agents)
assert any(
    item["probe"]["name"] == "legacy-egress"
    and any(point["avg_bps"] == 4_250_000 for point in item["points"])
    for item in series
), f"migrated measured series is missing: {series}"
assert any(item.get("code") == "FREEZE_START" for item in events), f"migrated telemetry event is missing: {events}"
assert any(item.get("kind") == "incident" and item.get("id") == "legacy-incident" for item in events), f"migrated incident is missing: {events}"

payload = json.dumps({"items": [{
    "sample_id": "post-migration-legacy-agent-sample",
    "stream_id": "poland",
    "observed_at": now.isoformat(),
    "status": "OK",
    "metrics": {"received_media_bitrate_bps": 4_500_000},
    "events": [],
    "context": {},
}]}).encode()
request = Request(
    base + "/api/v1/ingest", data=payload, method="POST",
    headers={"Authorization": f"Bearer {sys.argv[1]}", "Content-Type": "application/json"},
)
with urlopen(request, timeout=5) as response:
    result = json.load(response)
assert result["accepted"] == 1, f"old systemd agent token stopped working: {result}"
print("Migrated API history and the existing agent token are available in Compose.")
PY

create_stream restore_keep
assert_stream_ids poland restore_keep
echo "Pre-backup API state is ready; creating a verified backup."
bash ./scripts/docker-backup.sh
backup="$(find backups -maxdepth 1 -type f -name 'central-*.tar.gz' -print -quit)"
if [[ -z "$backup" ]]; then
  echo "Backup script did not create a central-*.tar.gz bundle." >&2
  exit 1
fi

create_stream restore_remove
assert_stream_ids poland restore_keep restore_remove
echo "Post-backup API mutation is ready; restoring the earlier backup."
bash ./scripts/docker-restore.sh "$backup"
assert_stream_ids poland restore_keep
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
assert_stream_ids poland restore_keep

echo "Exporting the live Compose database/token pair into a systemd-style rollback location."
mkdir -p smoke-input/rollback-data
docker compose stop central
docker compose run --rm --no-deps -T --entrypoint cat central /data/central.db > smoke-input/rollback-data/central.db
docker compose run --rm --no-deps -T --entrypoint cat central /data/admin.token > smoke-input/rollback-data/admin.token
chmod 600 smoke-input/rollback-data/central.db smoke-input/rollback-data/admin.token
rollback_token="$(cat smoke-input/rollback-data/admin.token | tr -d '\r\n')"
if [[ "$rollback_token" != "$admin_token" ]]; then
  echo "Systemd rollback export did not preserve the database's matching admin token." >&2
  exit 1
fi
"$python_command" - smoke-input/rollback-data/central.db <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as database:
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert database.execute("SELECT id FROM streams WHERE id='poland'").fetchone() == ("poland",)
    assert database.execute("SELECT id FROM incidents WHERE id='legacy-incident'").fetchone() == ("legacy-incident",)
    assert database.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] >= 2
print("Exported rollback database is healthy and retains legacy and post-migration history.")
PY

rollback_port="$("$python_command" - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
cat > smoke-input/systemd-smoke.yaml <<'YAML'
database_url: sqlite:////var/lib/rtmp-monitor/central.db
bind_host: 0.0.0.0
bind_port: 8091
admin_token_file: /var/lib/rtmp-monitor/admin.token
logs_dir: /logs
YAML
cat > docker-compose.rollback-smoke.yml <<'YAML'
services:
  central:
    volumes:
      - ./smoke-input/rollback-data:/var/lib/rtmp-monitor
      - ./smoke-input/systemd-smoke.yaml:/etc/rtmp-monitor/systemd-smoke.yaml:ro
    environment:
      RTMP_MONITOR_DATABASE_URL: sqlite:////var/lib/rtmp-monitor/central.db
      RTMP_MONITOR_ADMIN_TOKEN_FILE: /var/lib/rtmp-monitor/admin.token
YAML
docker compose -f docker-compose.yml -f docker-compose.smoke.yml -f docker-compose.rollback-smoke.yml config -q
docker compose -f docker-compose.yml -f docker-compose.smoke.yml -f docker-compose.rollback-smoke.yml run \
  --rm --no-deps -T --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE \
  --entrypoint python central -c \
  "import os; root='/var/lib/rtmp-monitor'; uid=$service_uid; gid=$service_gid; os.chown(root, uid, gid); [os.chown(os.path.join(root, name), uid, gid) for name in os.listdir(root)]; print(f'Rollback files owned by service identity {uid}:{gid}.')"
echo "Starting the systemd-style central service from the rollback files."
docker compose -f docker-compose.yml -f docker-compose.smoke.yml -f docker-compose.rollback-smoke.yml run \
  --detach --no-deps -T --name "$rollback_container" --user "$service_uid:$service_gid" \
  -p "127.0.0.1:$rollback_port:8091" \
  --entrypoint rtmp-monitor central server --config /etc/rtmp-monitor/systemd-smoke.yaml \
  --host 0.0.0.0 --port 8091
base_url="http://127.0.0.1:$rollback_port"
healthy=false
for attempt in $(seq 1 45); do
  if curl --fail --silent --show-error "$base_url/healthz" >/dev/null 2>&1; then
    healthy=true
    break
  fi
  container_state="$(docker inspect --format '{{.State.Status}}' "$rollback_container" 2>/dev/null || echo missing)"
  if [[ "$container_state" != running ]]; then
    break
  fi
  sleep 2
done
if [[ "$healthy" != true ]]; then
  echo "Systemd-style service did not start from the rollback database." >&2
  docker logs "$rollback_container" >&2 || true
  exit 1
fi
admin_token="$rollback_token"
assert_stream_ids poland restore_keep
docker exec "$rollback_container" python -c \
  'import sqlite3; db=sqlite3.connect("/var/lib/rtmp-monitor/central.db"); assert db.execute("SELECT COUNT(*) FROM incidents WHERE id=?", ("legacy-incident",)).fetchone()[0] == 1; db.close(); print("Rollback service reopened the migrated incident history.")'
docker rm -f "$rollback_container" >/dev/null

echo "Docker Compose systemd migration, old-agent compatibility, backup/restore, rollback startup, history, and token pairing passed."

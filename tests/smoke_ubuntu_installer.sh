#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${1:?Pass the downloaded Ubuntu package artifact directory}"
WORK="$(mktemp -d)"
SERVER_URL="http://127.0.0.1:18090"
SERVER_PID=""
PACKAGE_INSTALLED=0

cleanup() {
  result=$?
  set +e
  sudo systemctl disable --now rtmp-monitor-agent.service >/dev/null 2>&1
  if [[ "$PACKAGE_INSTALLED" == 1 ]]; then
    sudo apt-get purge -y rtmp-monitor-agent >/dev/null 2>&1
  fi
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1
    wait "$SERVER_PID" >/dev/null 2>&1
  fi
  rm -rf "$WORK"
  exit "$result"
}
trap cleanup EXIT

mapfile -t packages < <(find "$ARTIFACT_DIR" -maxdepth 1 -type f -name 'rtmp-monitor-agent_*.deb' -print)
[[ ${#packages[@]} -eq 1 ]] || { echo "Expected one Ubuntu agent package, found ${#packages[@]}" >&2; exit 1; }
(
  cd "$ARTIFACT_DIR"
  sha256sum --check ./*.deb.sha256
)
PACKAGE_INSTALLED=1
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[0]}"
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
command -v rtmp-monitor-agent-install >/dev/null

python3 -m venv "$WORK/server-venv"
"$WORK/server-venv/bin/pip" install --disable-pip-version-check "$ROOT"
mkdir -p "$WORK/data" "$WORK/logs"
cat > "$WORK/central.yaml" <<EOF
database_url: "sqlite:////$WORK/data/central.db"
bind_host: 127.0.0.1
bind_port: 18090
admin_token_file: "$WORK/data/admin.token"
logs_dir: "$WORK/logs"
EOF
"$WORK/server-venv/bin/rtmp-monitor" server --config "$WORK/central.yaml" --host 127.0.0.1 --port 18090 >"$WORK/central.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 30); do
  if curl -fsS "$SERVER_URL/healthz" >/dev/null; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    cat "$WORK/central.log" >&2
    exit 1
  fi
  sleep 1
done
curl -fsS "$SERVER_URL/healthz" >/dev/null || { cat "$WORK/central.log" >&2; exit 1; }
ADMIN_TOKEN="$(cat "$WORK/data/admin.token")"
curl -fsS -X POST "$SERVER_URL/api/v1/streams" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  --data '{"id":"ubuntu-ci","name":"Ubuntu installer CI","public_url":"rtmp://127.0.0.1:9/live/ubuntu-ci"}' >/dev/null
curl -fsS -X POST "$SERVER_URL/api/v2/probe-enrollments" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  --data "{\"name\":\"ubuntu-installer-ci\",\"location\":\"github-actions\",\"platform\":\"Ubuntu\",\"role\":\"CLIENT\",\"stream_id\":\"ubuntu-ci\",\"central_url\":\"$SERVER_URL\",\"profile\":\"LIGHT\"}" \
  >"$WORK/enrollment.json"
ENROLLMENT_CODE="$(jq -er '.code' "$WORK/enrollment.json")"
printf '%s\n' "$ENROLLMENT_CODE" | sudo rtmp-monitor-agent-install --server "$SERVER_URL"

service_active=0
for _ in $(seq 1 10); do
  if sudo systemctl is-active --quiet rtmp-monitor-agent.service; then
    service_active=1
    break
  fi
  sleep 1
done
if [[ "$service_active" != 1 ]]; then
  sudo systemctl status --no-pager -l rtmp-monitor-agent.service >&2 || true
  sudo journalctl -u rtmp-monitor-agent.service --no-pager -n 100 >&2 || true
  cat "$WORK/central.log" >&2
  echo "Enrolled Ubuntu agent service did not stay active" >&2
  exit 1
fi
[[ -x /opt/rtmp-monitor-agent/.venv/bin/rtmp-monitor ]] || { echo "Agent executable was not installed" >&2; exit 1; }
[[ -f /etc/rtmp-monitor-agent/agent.yaml ]] || { echo "Enrollment config was not saved" >&2; exit 1; }
config_permissions="$(stat -c '%a:%U:%G' /etc/rtmp-monitor-agent/agent.yaml)"
[[ "$config_permissions" == "640:root:rtmp-monitor" ]] || {
  echo "Unexpected enrollment config permissions: $config_permissions" >&2
  exit 1
}

telemetry_seen=0
for _ in $(seq 1 30); do
  curl -fsS "$SERVER_URL/api/v1/agents" -H "Authorization: Bearer $ADMIN_TOKEN" >"$WORK/agents.json"
  if jq -e 'any(.[]; .name == "ubuntu-installer-ci" and .last_seen_at != null)' "$WORK/agents.json" >/dev/null; then
    telemetry_seen=1
    break
  fi
  sleep 1
done
if [[ "$telemetry_seen" != 1 ]]; then
  sudo journalctl -u rtmp-monitor-agent.service --no-pager -n 80 >&2 || true
  cat "$WORK/central.log" >&2
  echo "Enrolled Ubuntu agent did not deliver telemetry to the central API" >&2
  exit 1
fi

sudo apt-get purge -y rtmp-monitor-agent
PACKAGE_INSTALLED=0
! systemctl list-unit-files rtmp-monitor-agent.service --no-legend | grep -q .
[[ ! -e /opt/rtmp-monitor-agent ]]
[[ ! -e /etc/rtmp-monitor-agent ]]
curl -fsS "$SERVER_URL/api/v1/agents" -H "Authorization: Bearer $ADMIN_TOKEN" >"$WORK/agents-after-purge.json"
jq -e 'any(.[]; .name == "ubuntu-installer-ci" and .last_seen_at != null)' "$WORK/agents-after-purge.json" >/dev/null
echo "Ubuntu .deb install, one-time enrollment, systemd start, telemetry delivery, and local purge passed. Central probe metadata remained after purge."

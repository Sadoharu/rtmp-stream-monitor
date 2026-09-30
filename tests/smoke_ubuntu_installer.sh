#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${1:?Pass the downloaded Ubuntu package artifact directory}"
WORK="$(mktemp -d)"
SERVER_URL="http://127.0.0.1:18090"
SERVER_PID=""
PACKAGE_INSTALLED=0
PRESERVATION_MARKER=""

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
  if [[ -n "$PRESERVATION_MARKER" ]]; then
    sudo rm -f "$PRESERVATION_MARKER"
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
sudo test -f /etc/rtmp-monitor-agent/agent.yaml || { echo "Enrollment config was not saved" >&2; exit 1; }
config_permissions="$(sudo stat -c '%a:%U:%G' /etc/rtmp-monitor-agent/agent.yaml)"
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

current_version="$(dpkg-query -W rtmp-monitor-agent | awk '{print $2}')"
upgrade_source="$WORK/upgrade-source"
upgrade_artifact_dir="$WORK/agent-deb-upgrade"
mkdir -p "$upgrade_source/scripts" "$upgrade_artifact_dir"
cp "$ROOT/pyproject.toml" "$ROOT/LICENSE" "$ROOT/install-agent.sh" "$upgrade_source/"
cp -a "$ROOT/src" "$upgrade_source/src"
cp "$ROOT/scripts/build-agent-deb.sh" "$upgrade_source/scripts/build-agent-deb.sh"
next_version="$(python3 - "$upgrade_source/pyproject.toml" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
match = re.search(r'(?m)^version\s*=\s*"([0-9]+)\.([0-9]+)\.([0-9]+)"\s*$', text)
if not match:
    raise SystemExit("Ubuntu upgrade smoke requires a three-part numeric project version.")
major, minor, patch = map(int, match.groups())
next_version = f"{major}.{minor}.{patch + 1}"
text = text[:match.start()] + f'version = "{next_version}"' + text[match.end():]
path.write_text(text, encoding="utf-8")
print(next_version)
PY
)"
[[ "$next_version" != "$current_version" ]] || { echo "Upgrade package version did not advance" >&2; exit 1; }
PYTHON_BIN=python3 bash "$upgrade_source/scripts/build-agent-deb.sh" "$upgrade_artifact_dir"
upgrade_package_count="$(find "$upgrade_artifact_dir" -maxdepth 1 -type f -name 'rtmp-monitor-agent_*.deb' | wc -l)"
[[ "$upgrade_package_count" -eq 1 ]] || { echo "Expected one Ubuntu upgrade package, found $upgrade_package_count" >&2; exit 1; }
upgrade_package="$(find "$upgrade_artifact_dir" -maxdepth 1 -type f -name 'rtmp-monitor-agent_*.deb' -print -quit)"
[[ "$(dpkg-deb --field "$upgrade_package" Version)" == "$next_version" ]] || {
  echo "Upgrade package does not contain expected version $next_version" >&2
  exit 1
}

config_before_update="$(sudo sha256sum /etc/rtmp-monitor-agent/agent.yaml | awk '{print $1}')"
marker="/var/lib/rtmp-monitor-agent/ci-upgrade-preservation-marker"
if sudo test -e "$marker"; then
  echo "Refusing to overwrite existing CI preservation marker: $marker" >&2
  exit 1
fi
PRESERVATION_MARKER="$marker"
printf '%s\n' "preserve-through-upgrade" | sudo tee "$marker" >/dev/null
service_start_before="$(sudo systemctl show -p ExecMainStartTimestampMonotonic --value rtmp-monitor-agent.service)"
[[ -n "$service_start_before" ]] || { echo "Could not read Ubuntu probe service start timestamp" >&2; exit 1; }
curl -fsS "$SERVER_URL/api/v1/agents" -H "Authorization: Bearer $ADMIN_TOKEN" >"$WORK/agents-before-upgrade.json"
last_seen_before_update="$(jq -er '.[] | select(.name == "ubuntu-installer-ci") | .last_seen_at' "$WORK/agents-before-upgrade.json")"

sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$upgrade_package"
installed_version="$(dpkg-query -W rtmp-monitor-agent | awk '{print $2}')"
[[ "$installed_version" == "$next_version" ]] || {
  echo "Ubuntu agent package upgrade installed $installed_version, expected $next_version" >&2
  exit 1
}
config_after_update="$(sudo sha256sum /etc/rtmp-monitor-agent/agent.yaml | awk '{print $1}')"
[[ "$config_after_update" == "$config_before_update" ]] || {
  echo "Ubuntu agent package upgrade changed the enrolled config" >&2
  exit 1
}
sudo test -f "$marker" || { echo "Ubuntu agent package upgrade removed local data" >&2; exit 1; }
sudo systemctl is-active --quiet rtmp-monitor-agent.service || {
  sudo systemctl status --no-pager -l rtmp-monitor-agent.service >&2 || true
  sudo journalctl -u rtmp-monitor-agent.service --no-pager -n 100 >&2 || true
  echo "Ubuntu probe service is not active after package upgrade" >&2
  exit 1
}
service_start_after="$(sudo systemctl show -p ExecMainStartTimestampMonotonic --value rtmp-monitor-agent.service)"
[[ "$service_start_after" != "$service_start_before" ]] || {
  echo "Ubuntu probe service did not restart during package upgrade" >&2
  exit 1
}

telemetry_updated=0
for _ in $(seq 1 30); do
  curl -fsS "$SERVER_URL/api/v1/agents" -H "Authorization: Bearer $ADMIN_TOKEN" >"$WORK/agents-after-upgrade.json"
  last_seen_after_update="$(jq -er '.[] | select(.name == "ubuntu-installer-ci") | .last_seen_at' "$WORK/agents-after-upgrade.json")"
  if [[ "$last_seen_after_update" != "$last_seen_before_update" ]]; then
    telemetry_updated=1
    break
  fi
  sleep 1
done
if [[ "$telemetry_updated" != 1 ]]; then
  sudo journalctl -u rtmp-monitor-agent.service --no-pager -n 80 >&2 || true
  cat "$WORK/central.log" >&2
  echo "Upgraded Ubuntu agent did not deliver fresh telemetry to the central API" >&2
  exit 1
fi

sudo apt-get purge -y rtmp-monitor-agent
PACKAGE_INSTALLED=0
! systemctl list-unit-files rtmp-monitor-agent.service --no-legend | grep -q .
[[ ! -e /opt/rtmp-monitor-agent ]]
[[ ! -e /etc/rtmp-monitor-agent ]]
sudo test -f "$marker" || { echo "Ubuntu agent purge removed retained local data" >&2; exit 1; }
curl -fsS "$SERVER_URL/api/v1/agents" -H "Authorization: Bearer $ADMIN_TOKEN" >"$WORK/agents-after-purge.json"
jq -e 'any(.[]; .name == "ubuntu-installer-ci" and .last_seen_at != null)' "$WORK/agents-after-purge.json" >/dev/null
echo "Ubuntu .deb install, one-time enrollment, service upgrade with config/data preservation, telemetry delivery, and local purge passed. Central probe metadata remained after purge."

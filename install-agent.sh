#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo: ./install-agent.sh /path/to/agent.yaml" >&2; exit 1; fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/rtmp-monitor-agent
CONF_DIR=/etc/rtmp-monitor-agent
DATA_DIR=/var/lib/rtmp-monitor-agent
LOG_DIR=/var/log/rtmp-monitor-agent
CONFIG_SOURCE=""
SERVER_URL=""
while (($#)); do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { echo "--config requires a file path" >&2; exit 2; }
      CONFIG_SOURCE="$2"
      shift 2
      ;;
    --server)
      [[ $# -ge 2 ]] || { echo "--server requires the central dashboard URL" >&2; exit 2; }
      SERVER_URL="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: sudo ./install-agent.sh --server https://monitor.example.net"
      echo "   or: sudo ./install-agent.sh --config /path/to/agent.yaml"
      exit 0
      ;;
    *)
      if [[ -z "$CONFIG_SOURCE" ]]; then CONFIG_SOURCE="$1"; shift; else echo "Unexpected argument: $1" >&2; exit 2; fi
      ;;
  esac
done
if [[ -n "$CONFIG_SOURCE" ]]; then
  [[ -f "$CONFIG_SOURCE" ]] || { echo "Agent config not found: $CONFIG_SOURCE" >&2; exit 2; }
  CONFIG_SOURCE="$(realpath "$CONFIG_SOURCE")"
elif [[ -f "$CONF_DIR/agent.yaml" ]]; then
  CONFIG_SOURCE="$CONF_DIR/agent.yaml"
elif [[ -z "$SERVER_URL" ]]; then
  echo "Use --server https://monitor.example.net for a new probe, or --config /path/to/agent.yaml." >&2
  exit 2
fi
if [[ -d "$ROOT/wheelhouse" ]]; then
  # Debian packages carry a Python 3.10-compatible wheelhouse and declare the
  # operating-system dependencies in DEBIAN/control. Do not need PyPI at install time.
  command -v python3 >/dev/null 2>&1 || { echo "python3 is missing; install the package dependencies with apt." >&2; exit 1; }
  command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is missing; install the package dependencies with apt." >&2; exit 1; }
  command -v ffprobe >/dev/null 2>&1 || { echo "ffprobe is missing; install the package dependencies with apt." >&2; exit 1; }
else
  apt-get update
  apt-get install -y python3 python3-venv python3-pip ffmpeg ca-certificates
fi
if [[ -n "$SERVER_URL" ]]; then
  python3 - "$SERVER_URL" <<'PY'
import sys
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
        or url.path not in {"", "/"} or url.query or url.fragment
        or (url.scheme != "https" and url.hostname.lower() not in {"localhost", "127.0.0.1", "::1"})):
    raise SystemExit("--server must be an HTTPS origin without credentials or a path (HTTP is allowed only for localhost testing).")
PY
fi
PYTHON_BIN=""
for candidate in python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.10+ is required. Install it alongside the system Python; the system python3 does not need to be replaced." >&2
  exit 1
fi
PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
SYSTEM_PYTHON_BIN="$(command -v python3)"
if [[ "$(readlink -f "$PYTHON_BIN")" != "$(readlink -f "$SYSTEM_PYTHON_BIN")" ]] && apt-cache show "python${PYTHON_VERSION}-venv" >/dev/null 2>&1; then
  apt-get install -y "python${PYTHON_VERSION}-venv"
fi
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg was not found after installing the package" >&2; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "ffprobe was not found after installing the package" >&2; exit 1; }
id -u rtmp-monitor >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin rtmp-monitor
install -d -o rtmp-monitor -g rtmp-monitor "$APP" "$DATA_DIR" "$LOG_DIR"
install -d -m 0750 -o root -g rtmp-monitor "$CONF_DIR"
ENROLLED_CONFIG=0
if [[ -z "$CONFIG_SOURCE" ]]; then
  read -r -s -p "One-time probe enrollment code (valid for 15 minutes): " ENROLLMENT_CODE
  echo
  [[ -n "$ENROLLMENT_CODE" ]] || { echo "Enrollment code cannot be empty" >&2; exit 1; }
  CONFIG_SOURCE="$CONF_DIR/agent.yaml"
  printf '%s' "$ENROLLMENT_CODE" | python3 -c '
import json, os, sys, urllib.request
server_url, output_path = sys.argv[1:]
code = sys.stdin.read().strip()
request = urllib.request.Request(
    server_url.rstrip("/") + "/api/v2/probe-enrollments/redeem",
    data=json.dumps({"code": code}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=20) as response:
    config = json.load(response)["config"]
fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
with os.fdopen(fd, "w", encoding="utf-8") as target:
    json.dump(config, target, ensure_ascii=False, indent=2)
    target.write("\n")
' "$SERVER_URL" "$CONFIG_SOURCE"
  ENROLLMENT_CODE=""
  chown root:rtmp-monitor "$CONFIG_SOURCE"
  chmod 0640 "$CONFIG_SOURCE"
  ENROLLED_CONFIG=1
fi
cp -a "$ROOT/src" "$ROOT/pyproject.toml" "$APP/"
"$PYTHON_BIN" -m venv "$APP/.venv" || { echo "Could not create the Python virtual environment. Install python${PYTHON_VERSION}-venv and rerun." >&2; exit 1; }
if [[ -d "$ROOT/wheelhouse" ]]; then
  shopt -s nullglob
  APP_WHEELS=("$ROOT"/wheelhouse/rtmp_stream_monitor-*.whl)
  shopt -u nullglob
  [[ ${#APP_WHEELS[@]} -eq 1 ]] || { echo "Expected one rtmp-stream-monitor wheel in $ROOT/wheelhouse." >&2; exit 1; }
  "$APP/.venv/bin/pip" install --no-index --find-links "$ROOT/wheelhouse" --upgrade pip setuptools wheel
  "$APP/.venv/bin/pip" install --no-index --find-links "$ROOT/wheelhouse" "${APP_WHEELS[0]}"
else
  "$APP/.venv/bin/pip" install --upgrade pip
  "$APP/.venv/bin/pip" install "$APP"
fi
if [[ "$ENROLLED_CONFIG" == 0 && "$CONFIG_SOURCE" != "$CONF_DIR/agent.yaml" ]]; then
  install -m 0640 -o root -g rtmp-monitor "$CONFIG_SOURCE" "$CONF_DIR/agent.yaml"
fi
chown -R rtmp-monitor:rtmp-monitor "$APP" "$DATA_DIR" "$LOG_DIR"
cat > /etc/systemd/system/rtmp-monitor-agent.service <<'UNIT'
[Unit]
Description=RTMP Stream Monitor Probe Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rtmp-monitor
Group=rtmp-monitor
WorkingDirectory=/var/lib/rtmp-monitor-agent
Environment=RTMP_MONITOR_CONFIG=/etc/rtmp-monitor-agent/agent.yaml
ExecStart=/opt/rtmp-monitor-agent/.venv/bin/rtmp-monitor agent --config /etc/rtmp-monitor-agent/agent.yaml
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/rtmp-monitor-agent /var/log/rtmp-monitor-agent

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable rtmp-monitor-agent.service
# Restart applies updated code when the unit was already active.
systemctl restart rtmp-monitor-agent.service
echo "Agent installed. Check: systemctl status rtmp-monitor-agent; journalctl -u rtmp-monitor-agent"

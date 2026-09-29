#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo: ./install-agent.sh /path/to/agent.yaml" >&2; exit 1; fi
if [[ $# -lt 1 || ! -f "$1" ]]; then echo "Usage: sudo ./install-agent.sh /path/to/agent.yaml" >&2; exit 2; fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_SOURCE="$(realpath "$1")"
APP=/opt/rtmp-monitor-agent
CONF_DIR=/etc/rtmp-monitor-agent
DATA_DIR=/var/lib/rtmp-monitor-agent
LOG_DIR=/var/log/rtmp-monitor-agent
apt-get update
apt-get install -y python3 python3-venv python3-pip ffmpeg ca-certificates
PYTHON_BIN=""
for candidate in python3.15 python3.14 python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.12+ is required. Install it alongside the system Python (for example, python3.12 and python3.12-venv); the system python3 does not need to be replaced." >&2
  exit 1
fi
PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PYTHON_BIN" != "$(command -v python3)" ]] && apt-cache show "python${PYTHON_VERSION}-venv" >/dev/null 2>&1; then
  apt-get install -y "python${PYTHON_VERSION}-venv"
fi
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg was not found after installing the package" >&2; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "ffprobe was not found after installing the package" >&2; exit 1; }
id -u rtmp-monitor >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin rtmp-monitor
install -d -o rtmp-monitor -g rtmp-monitor "$APP" "$DATA_DIR" "$LOG_DIR"
install -d -m 0750 -o root -g rtmp-monitor "$CONF_DIR"
cp -a "$ROOT/src" "$ROOT/pyproject.toml" "$APP/"
"$PYTHON_BIN" -m venv "$APP/.venv" || { echo "Could not create the Python virtual environment. Install python${PYTHON_VERSION}-venv and rerun." >&2; exit 1; }
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install "$APP"
install -m 0640 -o root -g rtmp-monitor "$CONFIG_SOURCE" "$CONF_DIR/agent.yaml"
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

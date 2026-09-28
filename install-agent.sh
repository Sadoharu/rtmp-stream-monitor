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
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "Python 3.12+ is required. Use an Ubuntu release with Python 3.12 or install python3.12 and python3.12-venv first." >&2; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg was not found after installing the package" >&2; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "ffprobe was not found after installing the package" >&2; exit 1; }
id -u rtmp-monitor >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin rtmp-monitor
install -d -o rtmp-monitor -g rtmp-monitor "$APP" "$DATA_DIR" "$LOG_DIR"
install -d -m 0750 -o root -g rtmp-monitor "$CONF_DIR"
cp -a "$ROOT/src" "$ROOT/pyproject.toml" "$APP/"
python3 -m venv "$APP/.venv"
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
systemctl enable --now rtmp-monitor-agent.service
echo "Agent installed. Check: systemctl status rtmp-monitor-agent; journalctl -u rtmp-monitor-agent"

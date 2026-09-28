#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo: ./install-server.sh" >&2; exit 1; fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/rtmp-monitor
CONF_DIR=/etc/rtmp-monitor
DATA_DIR=/var/lib/rtmp-monitor
LOG_DIR=/var/log/rtmp-monitor
apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "Python 3.12+ is required. Use an Ubuntu release with Python 3.12 or install python3.12 and python3.12-venv first." >&2; exit 1; }
id -u rtmp-monitor >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin rtmp-monitor
install -d -o rtmp-monitor -g rtmp-monitor "$APP" "$DATA_DIR" "$LOG_DIR"
install -d -m 0755 "$CONF_DIR"
cp -a "$ROOT/src" "$ROOT/pyproject.toml" "$APP/"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install "$APP"
if [[ ! -f "$CONF_DIR/central.yaml" ]]; then
  sed -e "s|sqlite:////var/lib/rtmp-monitor/central.db|sqlite:////var/lib/rtmp-monitor/central.db|" "$ROOT/config/central.example.yaml" > "$CONF_DIR/central.yaml"
fi
chown -R rtmp-monitor:rtmp-monitor "$APP" "$DATA_DIR" "$LOG_DIR"
cat > /etc/systemd/system/rtmp-monitor-central.service <<'UNIT'
[Unit]
Description=RTMP Stream Monitor Central API and Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rtmp-monitor
Group=rtmp-monitor
WorkingDirectory=/var/lib/rtmp-monitor
Environment=RTMP_MONITOR_CONFIG=/etc/rtmp-monitor/central.yaml
ExecStart=/opt/rtmp-monitor/.venv/bin/rtmp-monitor server --config /etc/rtmp-monitor/central.yaml
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/rtmp-monitor /var/log/rtmp-monitor

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now rtmp-monitor-central.service
echo "Central monitor installed. Dashboard: http://<server>:8090"
echo "After startup, read the local admin token with: sudo -u rtmp-monitor /opt/rtmp-monitor/.venv/bin/rtmp-monitor show-admin-token --config /etc/rtmp-monitor/central.yaml"
echo "Allow TCP/8090 only from trusted management and probe networks."

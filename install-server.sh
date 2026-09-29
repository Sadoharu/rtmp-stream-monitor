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
id -u rtmp-monitor >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin rtmp-monitor
install -d -o rtmp-monitor -g rtmp-monitor "$APP" "$DATA_DIR" "$LOG_DIR"
install -d -m 0755 "$CONF_DIR"
if [[ ! -f "$CONF_DIR/openai.env" ]]; then
  install -o root -g rtmp-monitor -m 0640 /dev/null "$CONF_DIR/openai.env"
else
  chown root:rtmp-monitor "$CONF_DIR/openai.env"
  chmod 0640 "$CONF_DIR/openai.env"
fi
cp -a "$ROOT/src" "$ROOT/pyproject.toml" "$APP/"
"$PYTHON_BIN" -m venv "$APP/.venv" || { echo "Could not create the Python virtual environment. Install python${PYTHON_VERSION}-venv and rerun." >&2; exit 1; }
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
EnvironmentFile=-/etc/rtmp-monitor/openai.env
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
systemctl enable rtmp-monitor-central.service
# Restart applies updated code when the unit was already active.
systemctl restart rtmp-monitor-central.service
echo "Central monitor installed. Dashboard: http://<server>:8090"
echo "After startup, read the local admin token with: sudo -u rtmp-monitor /opt/rtmp-monitor/.venv/bin/rtmp-monitor show-admin-token --config /etc/rtmp-monitor/central.yaml"
echo "Allow TCP/8090 only from trusted management and probe networks."

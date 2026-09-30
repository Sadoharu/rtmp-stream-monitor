#!/usr/bin/env bash
# Read-only inventory for planning systemd-to-Compose migration. Does not stop services or print secrets.
set -u

service="rtmp-monitor-central"
data_dir="${RTMP_MONITOR_DATA_DIR:-/var/lib/rtmp-monitor}"
database="${RTMP_MONITOR_PREFLIGHT_DB:-$data_dir/central.db}"
token_file="${RTMP_MONITOR_PREFLIGHT_TOKEN:-$data_dir/admin.token}"
result=0

printf '%s\n' '=== OS and architecture ==='
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  printf 'os=%s\n' "${PRETTY_NAME:-unknown}"
fi
printf 'kernel_arch=%s\n' "$(uname -m 2>/dev/null || printf unknown)"
if command -v dpkg >/dev/null 2>&1; then
  printf 'package_arch=%s\n' "$(dpkg --print-architecture 2>/dev/null || printf unknown)"
fi

printf '%s\n' '=== Docker ==='
if command -v docker >/dev/null 2>&1; then
  docker --version 2>/dev/null || true
  docker compose version 2>/dev/null || printf '%s\n' 'docker_compose=unavailable'
else
  printf '%s\n' 'docker=not_installed'
fi

printf '%s\n' '=== Existing central systemd service ==='
if command -v systemctl >/dev/null 2>&1; then
  printf 'active=%s\n' "$(systemctl is-active "$service" 2>/dev/null || printf unknown)"
  printf 'enabled=%s\n' "$(systemctl is-enabled "$service" 2>/dev/null || printf unknown)"
  printf 'unit_path=%s\n' "$(systemctl show --property=FragmentPath --value "$service" 2>/dev/null || printf unknown)"
  printf 'service_user=%s\n' "$(systemctl show --property=User --value "$service" 2>/dev/null || printf unknown)"
else
  printf '%s\n' 'systemd=unavailable'
fi

printf '%s\n' '=== Local dashboard health ==='
if command -v curl >/dev/null 2>&1; then
  health="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 3 http://127.0.0.1:8090/healthz 2>/dev/null || true)"
  printf 'healthz_http=%s\n' "${health:-unreachable}"
else
  printf '%s\n' 'curl=unavailable'
fi

printf '%s\n' '=== Data and backup capacity ==='
printf 'database_path=%s\n' "$database"
if [[ -f "$database" ]]; then
  stat --printf='database_bytes=%s\ndatabase_mode=%a\n' "$database" 2>/dev/null || true
else
  printf '%s\n' 'database=not_found'
  result=2
fi
if [[ -f "$token_file" ]]; then
  stat --printf='admin_token_file=present\nadmin_token_mode=%a\n' "$token_file" 2>/dev/null || printf '%s\n' 'admin_token_file=present'
else
  printf '%s\n' 'admin_token_file=not_found'
  result=2
fi
if command -v df >/dev/null 2>&1; then
  df -h "$(dirname "$database")" 2>/dev/null | tail -n 1
fi
if [[ "$EUID" -ne 0 ]]; then
  printf '%s\n' 'note=run with sudo to inspect protected database/token metadata; no file contents are read'
fi

if [[ -f "$database" ]] && command -v python3 >/dev/null 2>&1; then
  RTMP_MONITOR_PREFLIGHT_DB_PATH="$database" python3 - <<'PY'
from pathlib import Path
import os
import sqlite3
import sys

path = Path(os.environ["RTMP_MONITOR_PREFLIGHT_DB_PATH"]).resolve()
try:
    uri = path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        print(f"sqlite_quick_check={check}")
        if check != "ok":
            sys.exit(2)
        present = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        for table in sorted(present):
            escaped = table.replace('"', '""')
            count = connection.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
            print(f"rows_{table}={count}")
    finally:
        connection.close()
except (OSError, sqlite3.Error) as error:
    print(f"sqlite_read_error={type(error).__name__}")
    sys.exit(2)
PY
  sqlite_status=$?
  if [[ "$sqlite_status" -ne 0 ]]; then
    result=2
  fi
elif [[ -f "$database" ]]; then
  printf '%s\n' 'sqlite_check=python3_unavailable'
  result=2
fi

exit "$result"

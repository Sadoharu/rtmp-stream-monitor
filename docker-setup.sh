#!/usr/bin/env bash
set -euo pipefail
# Keep container paths like /etc/... intact when called from Git Bash on Windows.
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
prepare_only=false
if [[ ${1:-} == "--prepare-only" ]]; then
  prepare_only=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--prepare-only]" >&2
  exit 2
fi

if [[ "$EUID" -eq 0 ]]; then
  echo "Run this script as your normal Linux user with Docker Compose access; do not use sudo." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required. Install Docker Engine and the Compose plugin first." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  read -r -p "Dashboard port [8090]: " port
  port="${port:-8090}"
  if [[ ! "$port" =~ ^[0-9]{1,5}$ ]] || ((port < 1 || port > 65535)); then
    echo "Port must be an integer from 1 to 65535." >&2
    exit 2
  fi
  umask 077
  cat > .env <<EOF
RTMP_MONITOR_PORT=$port
RTMP_MONITOR_UID=$(id -u)
RTMP_MONITOR_GID=$(id -g)
# RTMP_MONITOR_IMAGE=ghcr.io/sadoharu/rtmp-stream-monitor:0.1.0
# OPENAI_MODEL=gpt-6-luna
EOF
  chmod 600 .env
else
  echo "Keeping existing .env settings. Review RTMP_MONITOR_PORT, UID and GID if needed."
fi

mkdir -p data logs backups
chmod 700 data logs backups
mkdir -p secrets
chmod 700 secrets
if [[ ! -f secrets/openai_api_key ]]; then (umask 077; : > secrets/openai_api_key); fi
chmod 600 secrets/openai_api_key
legacy_openai_key="$(sed -n 's/^OPENAI_API_KEY=//p' .env | tail -n 1)"
if [[ -n "$legacy_openai_key" ]]; then
  (umask 077; printf '%s' "$legacy_openai_key" > secrets/openai_api_key)
  env_tmp=".env.migrate.$$"
  sed '/^OPENAI_API_KEY=/d' .env > "$env_tmp"
  chmod 600 "$env_tmp"
  mv -f -- "$env_tmp" .env
  unset legacy_openai_key
  echo "Moved the existing OpenAI key from .env into the Docker secret file."
fi
docker compose config -q
if [[ "$prepare_only" == true ]]; then
  echo "Local directories, secret file and .env are ready. The service was not started."
  exit 0
fi
if docker compose pull central; then
  echo "Using the configured central image."
else
  echo "The configured image could not be pulled; building it from this checkout."
  docker compose build central
fi
bash ./scripts/docker-init-volumes.sh
docker compose up -d

echo "Waiting for the central service to become healthy..."
for attempt in $(seq 1 30); do
  if docker compose exec -T central python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2)" >/dev/null 2>&1; then
    port="$(sed -n 's/^RTMP_MONITOR_PORT=//p' .env | tail -n 1)"
    echo "Central service is healthy."
    echo "Dashboard: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo SERVER):${port:-8090}"
    echo "Admin token (keep private):"
    docker compose exec -T central rtmp-monitor show-admin-token --config /etc/rtmp-monitor/central.yaml
    exit 0
  fi
  sleep 2
done

docker compose ps
docker compose logs --tail=100 central
echo "Service did not become healthy; inspect the logs above." >&2
exit 1

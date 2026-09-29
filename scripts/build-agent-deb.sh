#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT/dist}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

for tool in dpkg-deb "$PYTHON_BIN"; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Required build tool not found: $tool" >&2; exit 1; }
done
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info[:2] != (3, 10):
    raise SystemExit("Build the Ubuntu 22.04 agent package with Python 3.10 to match its binary wheelhouse.")
PY

VERSION="$(sed -n 's/^version *= *"\([^"]*\)"$/\1/p' "$ROOT/pyproject.toml" | head -n1)"
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+)*([+._-][A-Za-z0-9.+_-]+)?$ ]] || {
  echo "Could not read a safe project version from pyproject.toml." >&2
  exit 1
}

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PACKAGE="$WORK/rtmp-monitor-agent"
mkdir -p "$PACKAGE/DEBIAN" "$PACKAGE/usr/share/rtmp-monitor-agent/wheelhouse" "$PACKAGE/usr/bin"

BUILD_VENV="$WORK/build-venv"
"$PYTHON_BIN" -m venv "$BUILD_VENV"
BUILD_PYTHON="$BUILD_VENV/bin/python"
# Ubuntu 22.04's system pip predates reliable PEP 621 project metadata handling.
# Use a current isolated build pip without modifying the host's system Python.
"$BUILD_PYTHON" -m pip install --upgrade 'pip>=23'
"$BUILD_PYTHON" -m pip wheel --no-deps --wheel-dir "$PACKAGE/usr/share/rtmp-monitor-agent/wheelhouse" "$ROOT"
"$BUILD_PYTHON" -m pip download --only-binary=:all: --dest "$PACKAGE/usr/share/rtmp-monitor-agent/wheelhouse" "$ROOT"
shopt -s nullglob
APP_WHEELS=("$PACKAGE/usr/share/rtmp-monitor-agent/wheelhouse"/rtmp_stream_monitor-*.whl)
shopt -u nullglob
[[ ${#APP_WHEELS[@]} -eq 1 ]] || {
  echo "Expected one rtmp_stream_monitor wheel in the Debian package wheelhouse." >&2
  exit 1
}
"$BUILD_PYTHON" -m pip download --only-binary=:all: --dest "$PACKAGE/usr/share/rtmp-monitor-agent/wheelhouse" pip setuptools wheel
install -m 0644 "$ROOT/pyproject.toml" "$PACKAGE/usr/share/rtmp-monitor-agent/pyproject.toml"
cp -a "$ROOT/src" "$PACKAGE/usr/share/rtmp-monitor-agent/src"
install -m 0755 "$ROOT/install-agent.sh" "$PACKAGE/usr/share/rtmp-monitor-agent/install-agent.sh"
install -m 0644 "$ROOT/LICENSE" "$PACKAGE/usr/share/rtmp-monitor-agent/LICENSE"

cat > "$PACKAGE/DEBIAN/control" <<CONTROL
Package: rtmp-monitor-agent
Version: $VERSION
Section: net
Priority: optional
Architecture: amd64
Depends: python3 (>= 3.10), python3-venv, ffmpeg, ca-certificates
Maintainer: RTMP Stream Monitor maintainers
Description: RTMP Stream Monitor probe agent for Ubuntu
 Measures received media bitrate and stream health from an Ubuntu observation point.
CONTROL

cat > "$PACKAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = configure ] && [ -f /etc/rtmp-monitor-agent/agent.yaml ]; then
  /usr/share/rtmp-monitor-agent/install-agent.sh --config /etc/rtmp-monitor-agent/agent.yaml
else
  echo "Agent package installed. Enroll this probe with: sudo rtmp-monitor-agent-install --server https://YOUR-MONITOR"
fi
POSTINST

cat > "$PACKAGE/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
case "$1" in
  remove|purge)
    if command -v systemctl >/dev/null 2>&1; then
      systemctl disable --now rtmp-monitor-agent.service >/dev/null 2>&1 || true
    fi
    rm -f /etc/systemd/system/rtmp-monitor-agent.service
    if command -v systemctl >/dev/null 2>&1; then
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    rm -rf /opt/rtmp-monitor-agent
    ;;
esac
if [ "$1" = purge ]; then
  rm -rf /etc/rtmp-monitor-agent
fi
POSTRM

cat > "$PACKAGE/usr/bin/rtmp-monitor-agent-install" <<'INSTALLER'
#!/bin/sh
set -eu
exec /usr/share/rtmp-monitor-agent/install-agent.sh "$@"
INSTALLER

chmod 0755 "$PACKAGE/DEBIAN/postinst" "$PACKAGE/DEBIAN/postrm" "$PACKAGE/usr/bin/rtmp-monitor-agent-install"
dpkg-deb --root-owner-group --build "$PACKAGE" "$OUTPUT_DIR/rtmp-monitor-agent_${VERSION}_amd64.deb" >/dev/null
(
  cd "$OUTPUT_DIR"
  sha256sum "rtmp-monitor-agent_${VERSION}_amd64.deb" > "rtmp-monitor-agent_${VERSION}_amd64.deb.sha256"
)
echo "Built $OUTPUT_DIR/rtmp-monitor-agent_${VERSION}_amd64.deb and SHA-256 checksum"

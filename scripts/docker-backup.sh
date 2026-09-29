#!/usr/bin/env bash
set -euo pipefail
# Keep container paths like /backups/... intact when called from Git Bash on Windows.
export MSYS_NO_PATHCONV=1
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p backups
chmod 700 backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
stage="backups/.central-backup-$stamp"
bundle="backups/central-$stamp.tar.gz"
mkdir "$stage"
if [[ "$(uname -s)" != MINGW* && "$(uname -s)" != MSYS* ]]; then chmod 700 "$stage"; fi
trap 'rm -rf -- "$stage"' EXIT

docker compose exec -T central python -c '
import sqlite3, sys
source = sqlite3.connect("file:/data/central.db?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[1])
source.backup(target)
result = target.execute("PRAGMA integrity_check").fetchone()[0]
target.close()
source.close()
if result != "ok":
    raise SystemExit(f"SQLite backup integrity check failed: {result}")
print(f"SQLite snapshot verified: {sys.argv[1]}")
' "/backups/.central-backup-$stamp/central.db"
docker compose exec -T central cat /data/admin.token > "$stage/admin.token"
tar -C "$stage" -czf "$bundle" central.db admin.token
if [[ "$(uname -s)" != MINGW* && "$(uname -s)" != MSYS* ]]; then chmod 600 "$bundle"; fi
echo "Backup bundle created (SQLite database and admin token): $ROOT/$bundle"

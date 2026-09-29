#!/usr/bin/env bash
set -euo pipefail
export MSYS_NO_PATHCONV=1
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "Usage: $0 /path/to/verified-backup.tar.gz" >&2
  exit 2
fi
backup="$(realpath "$1")"
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
archive_stage="backups/.restore-$stamp"
mkdir -m 700 "$archive_stage"
trap 'rm -rf -- "$archive_stage"' EXIT
cp -- "$backup" "$archive_stage/backup.tar.gz"

# Extract to a staging directory on the named volume and verify before stopping central.
docker compose run --rm --no-deps -T --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE \
  --entrypoint python central -c '
import sqlite3, sys, tarfile
from pathlib import Path

stamp = sys.argv[1]
archive_path = Path("/backups") / f".restore-{stamp}" / "backup.tar.gz"
destination = Path("/data") / f".restore-{stamp}"
destination.mkdir(mode=0o700)
expected = {"central.db", "admin.token"}
with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    if {item.name for item in members} != expected or len(members) != len(expected):
        raise SystemExit("Backup must contain exactly central.db and admin.token")
    for item in members:
        if not item.isfile():
            raise SystemExit("Backup contains an unsupported archive entry")
        with archive.extractfile(item) as source, (destination / item.name).open("xb") as target:
            target.write(source.read())
(destination / "admin.token").chmod(0o600)
db_path = destination / "central.db"
db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
result = db.execute("PRAGMA integrity_check").fetchone()[0]
db.close()
if result != "ok":
    raise SystemExit(f"Backup integrity check failed: {result}")
if not (destination / "admin.token").read_text(encoding="utf-8").strip():
    raise SystemExit("Backup admin token is empty")
print("Backup contents and SQLite integrity check passed.")
' "$stamp"

if docker compose ps --status running --services | grep -qx central; then
  bash ./scripts/docker-backup.sh >/dev/null
  docker compose stop central
else
  # Keep a recoverable snapshot even when the service is already stopped.
  docker compose run --rm --no-deps -T --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE \
    -e RTMP_MONITOR_RESTORE_STAMP="$stamp" \
    --entrypoint python central -c '
import os, sqlite3, tarfile
import shutil
from pathlib import Path

data = Path("/data")
backup_dir = Path("/backups")
stamp = os.environ["RTMP_MONITOR_RESTORE_STAMP"]
stage = backup_dir / f".pre-restore-{stamp}"
stage.mkdir(mode=0o700)
db_path = data / "central.db"
if db_path.exists():
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    target = sqlite3.connect(stage / "central.db")
    source.backup(target)
    target.close()
    source.close()
token = data / "admin.token"
if token.exists():
    (stage / "admin.token").write_bytes(token.read_bytes())
if any(stage.iterdir()):
    with tarfile.open(backup_dir / f"pre-restore-{stamp}.tar.gz", "w:gz") as archive:
        for path in stage.iterdir():
            archive.add(path, arcname=path.name, recursive=False)
    print("Saved the existing named-volume state to /backups/pre-restore-" + stamp + ".tar.gz")
else:
    print("No existing central state needed a pre-restore snapshot.")
shutil.rmtree(stage)
'
fi

# Apply the already-verified snapshot inside the named volume.
docker compose run --rm --no-deps -T --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE \
  -e RTMP_MONITOR_RESTORE_STAMP="$stamp" \
  --entrypoint python central -c '
import os
import shutil
from pathlib import Path

stamp = os.environ["RTMP_MONITOR_RESTORE_STAMP"]
stage = Path("/data") / f".restore-{stamp}"
data = Path("/data")
os.replace(stage / "central.db", data / "central.db")
os.replace(stage / "admin.token", data / "admin.token")
for name in ("central.db-wal", "central.db-shm"):
    try:
        (data / name).unlink()
    except FileNotFoundError:
        pass
shutil.rmtree(stage)
(data / "admin.token").chmod(0o600)
print("Restored the verified database and matching admin token to the named volume.")
'

bash ./scripts/docker-init-volumes.sh
docker compose up -d central
echo "Waiting for the central service to become healthy..."
for attempt in $(seq 1 30); do
  if docker compose exec -T central python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2)" >/dev/null 2>&1; then
    echo "Database and admin token restored. Central service is healthy."
    exit 0
  fi
  sleep 2
done

docker compose ps
docker compose logs --tail=100 central
echo "Service did not become healthy; the pre-restore snapshot is in backups/." >&2
exit 1

#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_dir="$(mktemp -d)"
trap 'rm -rf "$fixture_dir"' EXIT

database="$fixture_dir/central.db"
token_file="$fixture_dir/admin.token"
token_marker='preflight-test-token-must-not-be-printed'
printf '%s' "$token_marker" > "$token_file"
chmod 600 "$token_file"

python3 - "$database" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    connection.executescript(
        """
        CREATE TABLE streams (id TEXT PRIMARY KEY);
        CREATE TABLE agents (id TEXT PRIMARY KEY);
        CREATE TABLE telemetry (id TEXT PRIMARY KEY);
        CREATE TABLE incidents (id TEXT PRIMARY KEY);
        CREATE TABLE metric_aggregates (id TEXT PRIMARY KEY);
        CREATE TABLE aggregate_cursors (id TEXT PRIMARY KEY);
        INSERT INTO streams VALUES ('fixture-stream');
        INSERT INTO agents VALUES ('fixture-agent');
        INSERT INTO telemetry VALUES ('fixture-sample-1');
        INSERT INTO telemetry VALUES ('fixture-sample-2');
        """
    )
    connection.commit()
finally:
    connection.close()
PY

database_hash_before="$(sha256sum "$database" | cut -d ' ' -f 1)"
token_hash_before="$(sha256sum "$token_file" | cut -d ' ' -f 1)"
output="$(
  RTMP_MONITOR_PREFLIGHT_DB="$database" \
  RTMP_MONITOR_PREFLIGHT_TOKEN="$token_file" \
    bash "$repo_root/scripts/docker-migration-preflight.sh"
)" || {
  printf '%s\n' "$output" >&2
  echo 'Migration preflight unexpectedly failed for a valid fixture.' >&2
  exit 1
}

for expected in \
  'admin_token_file=present' \
  'sqlite_quick_check=ok' \
  'rows_streams=1' \
  'rows_agents=1' \
  'rows_telemetry=2' \
  'rows_incidents=0' \
  'rows_metric_aggregates=0' \
  'rows_aggregate_cursors=0'; do
  if ! grep -Fqx "$expected" <<< "$output"; then
    printf 'Preflight output did not contain expected line: %s\n%s\n' "$expected" "$output" >&2
    exit 1
  fi
done

if [[ "$output" == *"$token_marker"* ]]; then
  echo 'Migration preflight exposed token contents.' >&2
  exit 1
fi

database_hash_after="$(sha256sum "$database" | cut -d ' ' -f 1)"
token_hash_after="$(sha256sum "$token_file" | cut -d ' ' -f 1)"
if [[ "$database_hash_before" != "$database_hash_after" || "$token_hash_before" != "$token_hash_after" ]]; then
  echo 'Migration preflight modified the fixture database or token file.' >&2
  exit 1
fi

missing_output=""
set +e
missing_output="$(
  RTMP_MONITOR_PREFLIGHT_DB="$fixture_dir/missing.db" \
  RTMP_MONITOR_PREFLIGHT_TOKEN="$fixture_dir/missing.token" \
    bash "$repo_root/scripts/docker-migration-preflight.sh"
)"
missing_status=$?
set -e
if [[ "$missing_status" -ne 2 ]] \
  || ! grep -Fqx 'database=not_found' <<< "$missing_output" \
  || ! grep -Fqx 'admin_token_file=not_found' <<< "$missing_output"; then
  printf 'Preflight did not fail clearly for missing migration inputs (exit %s):\n%s\n' \
    "$missing_status" "$missing_output" >&2
  exit 1
fi

corrupt_database="$fixture_dir/corrupt.db"
printf '%s' 'not a SQLite database' > "$corrupt_database"
corrupt_hash_before="$(sha256sum "$corrupt_database" | cut -d ' ' -f 1)"
corrupt_output=""
set +e
corrupt_output="$(
  RTMP_MONITOR_PREFLIGHT_DB="$corrupt_database" \
  RTMP_MONITOR_PREFLIGHT_TOKEN="$token_file" \
    bash "$repo_root/scripts/docker-migration-preflight.sh"
)"
corrupt_status=$?
set -e
corrupt_hash_after="$(sha256sum "$corrupt_database" | cut -d ' ' -f 1)"
if [[ "$corrupt_status" -ne 2 ]] \
  || ! grep -Fqx 'sqlite_read_error=DatabaseError' <<< "$corrupt_output" \
  || [[ "$corrupt_hash_before" != "$corrupt_hash_after" ]]; then
  printf 'Preflight did not reject the corrupt database read-only (exit %s):\n%s\n' \
    "$corrupt_status" "$corrupt_output" >&2
  exit 1
fi

printf '%s\n' 'Read-only migration preflight smoke passed.'

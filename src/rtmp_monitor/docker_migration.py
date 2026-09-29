"""Safe, idempotent import of a legacy central SQLite database into a Docker volume."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any


class MigrationError(RuntimeError):
    """The existing and legacy central data cannot be imported safely."""


def _integrity_check(connection: sqlite3.Connection, description: str) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        detail = result[0] if result else "no result"
        raise MigrationError(f"{description} SQLite integrity check failed: {detail}")


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts = {}
    for name in names:
        quoted_name = '"' + name.replace('"', '""') + '"'
        counts[name] = int(connection.execute(f"SELECT COUNT(*) FROM {quoted_name}").fetchone()[0])
    return counts


def _snapshot_sqlite(source_path: Path, target_path: Path) -> dict[str, int]:
    if not source_path.is_file():
        raise MigrationError(f"Legacy database not found: {source_path}")
    source_uri = source_path.resolve().as_uri() + "?mode=ro"
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        _integrity_check(source, "Legacy source")
        expected_counts = _table_counts(source)
        target = sqlite3.connect(target_path, timeout=30)
        source.backup(target, pages=256, sleep=0.01)
        _integrity_check(target, "Imported snapshot")
        actual_counts = _table_counts(target)
        if actual_counts != expected_counts:
            raise MigrationError(
                "SQLite snapshot table counts differ from the source: "
                f"source={expected_counts}, imported={actual_counts}"
            )
        result_counts = actual_counts
    except sqlite3.Error as exc:
        raise MigrationError(f"Could not snapshot the legacy SQLite database: {exc}") from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
    os.chmod(target_path, 0o600)
    return result_counts


def _write_private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def _copy_legacy_logs(source: Path, destination: Path) -> int:
    if not source.is_dir():
        return 0
    copied = 0
    for item in source.rglob("*"):
        if item.is_symlink():
            continue
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
    return copied


def _set_volume_ownership(paths: tuple[Path, ...], uid: int, gid: int) -> None:
    for root in paths:
        root.mkdir(parents=True, exist_ok=True)
        # Keep an otherwise-empty Docker named volume populated. Without a
        # marker, Docker may copy the image directory (owned by 10001) into it
        # again when the next container mounts the volume.
        (root / ".rtmp-monitor-volume").touch(exist_ok=True)
        if not hasattr(os, "chown"):
            continue
        for current, dirs, files in os.walk(root):
            os.chown(current, uid, gid)
            for name in dirs + files:
                path = Path(current) / name
                if not path.is_symlink():
                    os.chown(path, uid, gid)


def initialize_data_volume(
    data_dir: Path,
    legacy_data_dir: Path,
    logs_dir: Path,
    legacy_logs_dir: Path,
    uid: int,
    gid: int,
) -> dict[str, Any]:
    """Import a consistent legacy DB/token pair once and prepare volume ownership."""
    if uid < 0 or gid < 0:
        raise MigrationError("Docker volume UID and GID must be non-negative integers.")
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / ".migration-in-progress"
    if marker.exists():
        raise MigrationError(
            f"Found {marker}; a previous import stopped between database and token replacement. "
            "Inspect the volume and restore a verified backup before retrying."
        )

    volume_db = data_dir / "central.db"
    volume_token = data_dir / "admin.token"
    has_volume_db = volume_db.is_file()
    has_volume_token = volume_token.is_file()
    legacy_db = legacy_data_dir / "central.db"
    legacy_token = legacy_data_dir / "admin.token"
    if has_volume_db and not has_volume_token:
        raise MigrationError(
            "The named volume contains central.db without admin.token. "
            "Refusing to start with a database that would lose its existing admin access."
        )
    if has_volume_token and not has_volume_db and legacy_db.is_file():
        raise MigrationError(
            "The named volume contains an admin.token but the legacy database is still present. "
            "Refusing to ignore the legacy database or pair it with an unrelated token."
        )
    if has_volume_token and not volume_token.read_bytes().strip():
        raise MigrationError("The named volume admin.token is empty.")

    database_imported = False
    token_imported = False
    counts: dict[str, int] = {}
    if not has_volume_db and not has_volume_token:
        has_legacy_db = legacy_db.is_file()
        has_legacy_token = legacy_token.is_file()
        if has_legacy_db and not has_legacy_token:
            raise MigrationError(
                "Legacy central.db exists without its matching admin.token; refusing an incomplete migration."
            )
        if has_legacy_token and not legacy_token.read_bytes().strip():
            raise MigrationError("Legacy admin.token is empty.")

        if has_legacy_db:
            db_stage = data_dir / ".central.db.importing"
            token_stage = data_dir / ".admin.token.importing"
            for stage in (db_stage, token_stage):
                try:
                    stage.unlink()
                except FileNotFoundError:
                    pass
            try:
                counts = _snapshot_sqlite(legacy_db, db_stage)
                _write_private_file(token_stage, legacy_token.read_bytes())
                _write_private_file(marker, b"SQLite database/token import in progress\n")
                os.replace(db_stage, volume_db)
                os.replace(token_stage, volume_token)
                marker.unlink()
                database_imported = True
                token_imported = True
            finally:
                for stage in (db_stage, token_stage):
                    try:
                        stage.unlink()
                    except FileNotFoundError:
                        pass
        elif has_legacy_token:
            token_stage = data_dir / ".admin.token.importing"
            try:
                _write_private_file(token_stage, legacy_token.read_bytes())
                os.replace(token_stage, volume_token)
                token_imported = True
            finally:
                try:
                    token_stage.unlink()
                except FileNotFoundError:
                    pass

    copied_logs = _copy_legacy_logs(legacy_logs_dir, logs_dir)
    _set_volume_ownership((data_dir, logs_dir), uid, gid)
    return {
        "database_imported": database_imported,
        "token_imported": token_imported,
        "table_counts": counts,
        "log_files_copied": copied_logs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-source", type=Path, help="create a verified SQLite snapshot from this stopped legacy database")
    parser.add_argument("--snapshot-target", type=Path, help="new destination path for --snapshot-source")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--legacy-data-dir", type=Path, default=Path("/migration-data"))
    parser.add_argument("--logs-dir", type=Path, default=Path("/logs"))
    parser.add_argument("--legacy-logs-dir", type=Path, default=Path("/migration-logs"))
    parser.add_argument("--uid", type=int, default=int(os.environ.get("RTMP_MONITOR_INIT_UID", "10001")))
    parser.add_argument("--gid", type=int, default=int(os.environ.get("RTMP_MONITOR_INIT_GID", "10001")))
    args = parser.parse_args()
    if args.snapshot_source or args.snapshot_target:
        if not args.snapshot_source or not args.snapshot_target:
            parser.error("--snapshot-source and --snapshot-target must be used together")
        if args.snapshot_target.exists():
            parser.error(f"snapshot target already exists: {args.snapshot_target}")
        args.snapshot_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            counts = _snapshot_sqlite(args.snapshot_source, args.snapshot_target)
        except MigrationError as exc:
            parser.error(str(exc))
        os.chmod(args.snapshot_target, 0o600)
        print(f"Verified SQLite snapshot created at {args.snapshot_target}; table row counts: {counts}")
        return 0
    try:
        result = initialize_data_volume(
            data_dir=args.data_dir,
            legacy_data_dir=args.legacy_data_dir,
            logs_dir=args.logs_dir,
            legacy_logs_dir=args.legacy_logs_dir,
            uid=args.uid,
            gid=args.gid,
        )
    except MigrationError as exc:
        parser.error(str(exc))
    if result["database_imported"]:
        print(f"Imported verified SQLite snapshot; table row counts: {result['table_counts']}")
    elif result["token_imported"]:
        print("Imported the existing admin token into the empty data volume.")
    else:
        print("Named data volume already has state or no legacy DB/token needs importing.")
    print(f"Copied {result['log_files_copied']} legacy log file(s); volume ownership is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

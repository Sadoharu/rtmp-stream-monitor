from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from rtmp_monitor.docker_migration import MigrationError, initialize_data_volume, main


def _write_legacy_database(path: Path, *, keep_uncheckpointed_wal: bool = False) -> sqlite3.Connection | None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE streams (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    connection.execute("CREATE TABLE incidents (id TEXT PRIMARY KEY, diagnosis TEXT NOT NULL)")
    connection.execute("INSERT INTO streams VALUES ('poland', 'Poland')")
    connection.commit()
    connection.execute("INSERT INTO incidents VALUES ('incident-1', 'CLIENT_PATH_UNCONFIRMED')")
    connection.commit()
    if keep_uncheckpointed_wal:
        wal_path = Path(str(path) + "-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0
        return connection
    connection.close()
    return None


def _initialize(root: Path, uid: int | None = None, gid: int | None = None) -> dict:
    uid = uid if uid is not None else (os.getuid() if hasattr(os, "getuid") else 0)
    gid = gid if gid is not None else (os.getgid() if hasattr(os, "getgid") else 0)
    return initialize_data_volume(
        data_dir=root / "volume-data",
        legacy_data_dir=root / "legacy-data",
        logs_dir=root / "volume-logs",
        legacy_logs_dir=root / "legacy-logs",
        uid=uid,
        gid=gid,
    )


def test_migration_uses_sqlite_backup_and_preserves_uncheckpointed_wal(tmp_path):
    legacy = tmp_path / "legacy-data"
    legacy.mkdir()
    source = _write_legacy_database(legacy / "central.db", keep_uncheckpointed_wal=True)
    (legacy / "admin.token").write_text("matching-admin-token\n", encoding="utf-8")
    old_logs = tmp_path / "legacy-logs"
    old_logs.mkdir()
    (old_logs / "central.jsonl").write_text('{"event":"preserved"}\n', encoding="utf-8")

    result = _initialize(tmp_path)

    assert result["database_imported"] is True
    assert result["token_imported"] is True
    assert result["table_counts"] == {"incidents": 1, "streams": 1}
    assert result["log_files_copied"] == 1
    imported = sqlite3.connect(tmp_path / "volume-data" / "central.db")
    try:
        assert imported.execute("SELECT id FROM streams").fetchall() == [("poland",)]
        assert imported.execute("SELECT id FROM incidents").fetchall() == [("incident-1",)]
        assert imported.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        imported.close()
    assert (tmp_path / "volume-data" / "admin.token").read_text(encoding="utf-8") == "matching-admin-token\n"
    assert (tmp_path / "volume-logs" / "central.jsonl").exists()
    assert not (tmp_path / "volume-data" / ".migration-in-progress").exists()
    assert source is not None
    source.close()


def test_migration_is_idempotent_and_does_not_overwrite_named_volume_state(tmp_path):
    legacy = tmp_path / "legacy-data"
    legacy.mkdir()
    _write_legacy_database(legacy / "central.db")
    (legacy / "admin.token").write_text("legacy-token\n", encoding="utf-8")

    _initialize(tmp_path)
    (legacy / "admin.token").write_text("changed-legacy-token\n", encoding="utf-8")
    result = _initialize(tmp_path)

    assert result["database_imported"] is False
    assert result["token_imported"] is False
    assert (tmp_path / "volume-data" / "admin.token").read_text(encoding="utf-8") == "legacy-token\n"
    assert sqlite3.connect(tmp_path / "volume-data" / "central.db").execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1


def test_migration_refuses_legacy_database_without_its_admin_token(tmp_path):
    legacy = tmp_path / "legacy-data"
    legacy.mkdir()
    _write_legacy_database(legacy / "central.db")

    with pytest.raises(MigrationError, match="without its matching admin.token"):
        _initialize(tmp_path)

    volume = tmp_path / "volume-data"
    assert not (volume / "central.db").exists()
    assert not (volume / "admin.token").exists()


def test_migration_refuses_a_database_without_an_admin_token_in_named_volume(tmp_path):
    volume = tmp_path / "volume-data"
    volume.mkdir()
    (volume / "central.db").write_bytes(b"incomplete")

    with pytest.raises(MigrationError, match="central.db without admin.token"):
        _initialize(tmp_path)


def test_existing_token_only_volume_can_finish_first_database_initialization(tmp_path):
    volume = tmp_path / "volume-data"
    volume.mkdir()
    (volume / "admin.token").write_text("new-install-token\n", encoding="utf-8")

    result = _initialize(tmp_path)

    assert result["database_imported"] is False
    assert result["token_imported"] is False
    assert (volume / "admin.token").read_text(encoding="utf-8") == "new-install-token\n"
    assert not (volume / "central.db").exists()


def test_token_only_volume_refuses_to_ignore_a_legacy_database(tmp_path):
    volume = tmp_path / "volume-data"
    volume.mkdir()
    (volume / "admin.token").write_text("unrelated-token\n", encoding="utf-8")
    legacy = tmp_path / "legacy-data"
    legacy.mkdir()
    _write_legacy_database(legacy / "central.db")
    (legacy / "admin.token").write_text("matching-legacy-token\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="legacy database is still present"):
        _initialize(tmp_path)


def test_migration_refuses_an_interrupted_pair_replacement(tmp_path):
    volume = tmp_path / "volume-data"
    volume.mkdir()
    (volume / ".migration-in-progress").write_text("interrupted\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="previous import stopped"):
        _initialize(tmp_path)


def test_snapshot_cli_creates_verified_private_target(tmp_path, monkeypatch):
    source = tmp_path / "legacy.db"
    _write_legacy_database(source)
    target = tmp_path / "data" / "central.db"
    monkeypatch.setattr(
        "sys.argv",
        ["docker_migration", "--snapshot-source", str(source), "--snapshot-target", str(target)],
    )

    assert main() == 0
    snapshot = sqlite3.connect(target)
    try:
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    finally:
        snapshot.close()

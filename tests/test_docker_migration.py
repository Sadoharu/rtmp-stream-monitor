from __future__ import annotations

import os
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session

from rtmp_monitor.api import create_app
from rtmp_monitor.config import CentralFileConfig
from rtmp_monitor.db import Agent, AggregateCursor, Base, Incident, MetricAggregate, Stream, Telemetry
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


def test_migrated_previous_schema_keeps_history_and_accepts_existing_and_new_probes(tmp_path):
    """Exercise the schema upgrade and API against a pre-enrollment central DB."""
    legacy_data = tmp_path / "legacy-data"
    legacy_data.mkdir()
    legacy_database = legacy_data / "central.db"
    legacy_token = "known-legacy-agent-token"
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=20)

    # Probe enrollment is the only table added by the current schema. Build a
    # prior-version database with every other table and index, then seed rows
    # through the mapped entities used by that prior schema.
    previous_metadata = MetaData()
    previous_table_names = {
        "streams", "agents", "telemetry", "incidents", "metric_aggregates", "aggregate_cursors",
    }
    for table in Base.metadata.sorted_tables:
        if table.name in previous_table_names:
            table.to_metadata(previous_metadata)
    legacy_engine = create_engine(f"sqlite:///{legacy_database.as_posix()}")
    previous_metadata.create_all(legacy_engine)
    with Session(legacy_engine) as session:
        session.add(Stream(
            id="poland", name="Poland", local_url="rtmp://127.0.0.1/live/poland",
            public_url="rtmp://stream.example.net/live/poland", source_url="",
        ))
        session.commit()
        session.add(Agent(
            id="legacy-agent", name="legacy-egress", location="server", platform="Ubuntu",
            role="SERVER_EGRESS", stream_id="poland",
            token_hash=hashlib.sha256(legacy_token.encode("utf-8")).hexdigest(), last_seen_at=observed_at,
        ))
        session.commit()
        session.add_all([
            Telemetry(
                id="legacy-sample", agent_id="legacy-agent", stream_id="poland",
                observed_at=observed_at, received_at=observed_at, status="OK",
                metrics={
                    "profile": "LIGHT", "received_media_bitrate_bps": 4_250_000,
                    "received_media_bitrate_quality": "MEASURED", "measurement_window_seconds": 1.0,
                },
                events=[{
                    "code": "FREEZE_START", "severity": "WARNING",
                    "timestamp": observed_at.isoformat(), "details": {"last_frame_age_seconds": 2.5},
                }],
                context={},
            ),
            Incident(
                id="legacy-incident", stream_id="poland", opened_at=observed_at,
                updated_at=observed_at, resolved_at=observed_at + timedelta(seconds=5),
                severity="WARNING", diagnosis="CLIENT_PROBLEM", probable_location="CLIENT RECEIVE / DECODER",
                affected_agents=["legacy-egress"], symptoms=[], context={}, active=False,
                fingerprint="legacy-client-problem",
            ),
            MetricAggregate(
                agent_id="legacy-agent", bucket_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                stream_id="poland", bucket_seconds=60, sample_count=60, status="OK",
                metrics={"received_media_bitrate_bps": {"avg": 4_000_000, "min": 3_900_000,
                                                          "max": 4_100_000, "last": 4_000_000,
                                                          "count": 60, "last_observed_at": "2026-09-01T00:00:59+00:00"}},
                event_counts={"FREEZE_START": 1},
            ),
            AggregateCursor(
                agent_id="legacy-agent", processed_until=datetime(2026, 9, 1, 0, 1, tzinfo=timezone.utc),
            ),
        ])
        session.commit()
    legacy_engine.dispose()
    (legacy_data / "admin.token").write_text("legacy-admin-token\n", encoding="utf-8")
    legacy_logs = tmp_path / "legacy-logs"
    legacy_logs.mkdir()
    (legacy_logs / "central.jsonl").write_text('{"event":"old-log"}\n', encoding="utf-8")

    migration = _initialize(tmp_path)
    assert migration["database_imported"] is True
    assert migration["table_counts"] == {
        "aggregate_cursors": 1, "agents": 1, "incidents": 1,
        "metric_aggregates": 1, "streams": 1, "telemetry": 1,
    }

    volume_database = tmp_path / "volume-data" / "central.db"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{volume_database.as_posix()}",
        admin_token_file=tmp_path / "volume-data" / "admin.token",
    ))
    admin_headers = {"Authorization": "Bearer legacy-admin-token"}
    agent_headers = {"Authorization": f"Bearer {legacy_token}"}
    with TestClient(app) as client:
        streams = client.get("/api/v1/streams", headers=admin_headers)
        agents = client.get("/api/v1/agents", headers=admin_headers)
        start = (observed_at - timedelta(seconds=5)).isoformat()
        end = (observed_at + timedelta(seconds=10)).isoformat()
        series = client.get(
            "/api/v2/streams/poland/series", params={"from": start, "to": end}, headers=admin_headers,
        )
        events = client.get(
            "/api/v2/streams/poland/events", params={"from": start, "to": end}, headers=admin_headers,
        )

        assert streams.status_code == 200 and streams.json()[0]["id"] == "poland"
        assert agents.status_code == 200 and agents.json()[0]["name"] == "legacy-egress"
        assert series.status_code == 200, series.text
        assert series.json()["series"][0]["points"][0]["avg_bps"] == 4_250_000
        assert events.status_code == 200, events.text
        migrated_events = events.json()["events"]
        assert {event["code"] for event in migrated_events} >= {"FREEZE_START", "CLIENT_PROBLEM"}
        assert next(event for event in migrated_events if event["kind"] == "incident")["id"] == "legacy-incident"

        ingest = client.post("/api/v1/ingest", headers=agent_headers, json={"items": [{
            "sample_id": "post-upgrade-sample", "stream_id": "poland",
            "observed_at": datetime.now(timezone.utc).isoformat(), "status": "OK",
            "metrics": {"received_media_bitrate_bps": 4_500_000}, "events": [], "context": {},
        }]})
        assert ingest.status_code == 200, ingest.text
        assert ingest.json()["accepted"] == 1

        enrollment = client.post("/api/v2/probe-enrollments", headers=admin_headers, json={
            "name": "new-windows-client", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "poland", "central_url": "http://127.0.0.1:8090",
            "profile": "LIGHT",
        })
        assert enrollment.status_code == 201, enrollment.text
        redeemed = client.post("/api/v2/probe-enrollments/redeem", json={"code": enrollment.json()["code"]})
        assert redeemed.status_code == 200, redeemed.text
        assert redeemed.json()["config"]["agent"]["profile"] == "LIGHT"
        assert redeemed.json()["config"]["streams"][0]["url"] == "rtmp://stream.example.net/live/poland"

    with sqlite3.connect(volume_database) as migrated:
        assert migrated.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 2
        assert migrated.execute("SELECT COUNT(*) FROM incidents WHERE id='legacy-incident'").fetchone()[0] == 1
        assert migrated.execute("SELECT COUNT(*) FROM metric_aggregates").fetchone()[0] == 1
        assert migrated.execute("SELECT COUNT(*) FROM probe_enrollments").fetchone()[0] == 1
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (tmp_path / "volume-logs" / "central.jsonl").read_text(encoding="utf-8") == '{"event":"old-log"}\n'


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


def test_migration_keeps_empty_named_volumes_populated(tmp_path):
    _initialize(tmp_path)

    assert (tmp_path / "volume-data" / ".rtmp-monitor-volume").is_file()
    assert (tmp_path / "volume-logs" / ".rtmp-monitor-volume").is_file()


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

#!/usr/bin/env python3
"""Exercise V2 series endpoints at the documented four-probe history sizes.

All fixture records are written to a temporary SQLite database and removed when
the process exits. This is a load check, not product/demo data.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from sqlalchemy import insert
import uvicorn

from rtmp_monitor.api import create_app
from rtmp_monitor.config import CentralFileConfig
from rtmp_monitor.db import Agent, MetricAggregate, Telemetry, utcnow


PROBE_COUNT = 4
PROBE_NAMES = ("server-egress", "client-north", "client-south", "client-predator")
PROBE_ROLES = ("SERVER_EGRESS", "CLIENT", "CLIENT", "CLIENT")
RAW_SECONDS = 24 * 60 * 60
ROLLUP_DAYS = 7
INSERT_BATCH_ROWS = 10_000


def request_timing(client: TestClient, path: str, headers: dict[str, str]) -> tuple[dict, float, int]:
    started = time.perf_counter()
    response = client.get(path, headers=headers)
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    return response.json(), elapsed, len(response.content)


def insert_rollups(session, probe_ids: list[str], start: datetime, minutes: int) -> None:
    batch: list[dict] = []
    for minute in range(minutes):
        bucket_start = start + timedelta(minutes=minute)
        last_observed = (bucket_start + timedelta(seconds=59)).isoformat()
        for probe_index, probe_id in enumerate(probe_ids):
            base_bps = 4_000_000 + probe_index * 100_000 + minute % 1000
            if probe_index == 3 and minute % 1440 in range(600, 605):
                base_bps = 650_000
            batch.append({
                "agent_id": probe_id,
                "bucket_start": bucket_start,
                "stream_id": "load-check",
                "bucket_seconds": 60,
                "sample_count": 60,
                "status": "OK",
                "metrics": {
                    "received_media_bitrate_bps": {
                        "min": max(0, base_bps - 10_000), "avg": base_bps,
                        "max": base_bps + 10_000, "count": 60,
                        "last_observed_at": last_observed,
                    },
                    "measurement_window_seconds": {"avg": 1.0, "count": 60},
                },
                "event_counts": {},
            })
        if len(batch) >= INSERT_BATCH_ROWS:
            session.execute(insert(MetricAggregate), batch)
            batch.clear()
    if batch:
        session.execute(insert(MetricAggregate), batch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="keep a loopback-only test fixture dashboard open after the API benchmark")
    parser.add_argument("--port", type=int, default=8091, help="loopback port used with --serve")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="rtmp-series-load-") as temporary:
        root = Path(temporary)
        admin_file = root / "admin.token"
        if args.serve:
            admin_file.write_text("rtmp-monitor-local-load-fixture-token\n", encoding="utf-8")
        config = CentralFileConfig(
            database_url=f"sqlite:///{(root / 'central.db').as_posix()}",
            admin_token_file=admin_file,
            raw_retention_days=7,
        )
        app = create_app(config)
        headers = {"Authorization": f"Bearer {admin_file.read_text(encoding='utf-8').strip()}"}
        now = datetime.fromtimestamp(int(utcnow().timestamp()), timezone.utc)
        raw_start = now - timedelta(seconds=RAW_SECONDS)
        raw_end = now
        # Use a seven-day historical interval wholly outside the default
        # seven-day raw-retention window so this request exercises rollups.
        rollup_end = now - timedelta(days=7)
        rollup_start = rollup_end - timedelta(days=ROLLUP_DAYS)

        with TestClient(app) as client:
            created = client.post("/api/v1/streams", headers=headers, json={
                "id": "load-check", "name": "Temporary load check",
            })
            created.raise_for_status()
            probe_ids: list[str] = []
            for number in range(PROBE_COUNT):
                response = client.post("/api/v1/agents", headers=headers, json={
                    "name": PROBE_NAMES[number], "location": "temporary-load-check",
                    "platform": "Test fixture", "role": PROBE_ROLES[number], "stream_id": "load-check",
                })
                response.raise_for_status()
                probe_ids.append(response.json()["id"])

            with app.state.sessions() as session:
                agents = [session.get(Agent, probe_id) for probe_id in probe_ids]
                for agent in agents:
                    agent.last_seen_at = raw_end - timedelta(seconds=1)
                session.commit()

                insert_statement = insert(Telemetry)
                batch: list[dict] = []
                for second in range(RAW_SECONDS):
                    observed_at = raw_start + timedelta(seconds=second)
                    for probe_index, probe_id in enumerate(probe_ids):
                        if args.serve and probe_index == 1 and RAW_SECONDS - 7 * 60 <= second < RAW_SECONDS - 7 * 60 + 8:
                            continue
                        bitrate = 4_000_000 + probe_index * 100_000 + second % 1000
                        events = []
                        event_start = RAW_SECONDS - 10 * 60
                        if args.serve and probe_index == 3 and event_start <= second < event_start + 5:
                            bitrate = 650_000
                        if args.serve and probe_index == 3 and second == event_start:
                            events = [{"code": "FREEZE_START", "severity": "WARNING", "timestamp": observed_at.isoformat(), "details": {"last_frame_age_seconds": 2.4}}]
                        elif args.serve and probe_index == 3 and second == event_start + 10:
                            events = [{"code": "FREEZE_END", "severity": "INFO", "timestamp": observed_at.isoformat(), "details": {}}]
                        batch.append({
                            "id": f"raw-{probe_index}-{second}",
                            "agent_id": probe_id,
                            "stream_id": "load-check",
                            "observed_at": observed_at,
                            "received_at": observed_at,
                            "status": "OK",
                            "metrics": {
                                "profile": "LIGHT", "sample_interval_seconds": 1.0,
                                "received_media_bitrate_bps": bitrate,
                                "received_media_bitrate_quality": "MEASURED",
                                "measurement_window_seconds": 1.0,
                            },
                            "events": events, "context": {},
                        })
                    if len(batch) >= INSERT_BATCH_ROWS:
                        session.execute(insert_statement, batch)
                        batch.clear()
                if batch:
                    session.execute(insert_statement, batch)

                insert_rollups(session, probe_ids, rollup_start, ROLLUP_DAYS * 24 * 60)
                session.commit()

            raw_query = "/api/v2/streams/load-check/series?" + urlencode({
                "from": raw_start.isoformat(), "to": raw_end.isoformat(), "resolution": "1s",
            })
            raw_data, raw_seconds, raw_bytes = request_timing(client, raw_query, headers)
            raw_points = sum(len(series["points"]) for series in raw_data["series"])
            if len(raw_data["series"]) != PROBE_COUNT or raw_points == 0:
                raise RuntimeError("24-hour raw series did not include all four probes")

            rollup_query = "/api/v2/streams/load-check/series?" + urlencode({
                "from": rollup_start.isoformat(), "to": rollup_end.isoformat(), "resolution": "1m",
            })
            rollup_data, rollup_seconds, rollup_bytes = request_timing(client, rollup_query, headers)
            rollup_points = sum(len(series["points"]) for series in rollup_data["series"])
            if len(rollup_data["series"]) != PROBE_COUNT or rollup_points < 30_000:
                raise RuntimeError("7-day rollup series did not include all four probes")
            if rollup_data["actual_resolution_seconds"] <= 1:
                raise RuntimeError("7-day range was expected to be coarsened to bounded aggregates")

            print(json.dumps({
                "database": "temporary SQLite fixture (deleted automatically)",
                "probe_count": PROBE_COUNT,
                "raw_fixture_samples": PROBE_COUNT * RAW_SECONDS,
                "aggregate_fixture_rows": PROBE_COUNT * ROLLUP_DAYS * 24 * 60,
                "24h_raw_series": {
                    "elapsed_seconds": round(raw_seconds, 3),
                    "response_bytes": raw_bytes,
                    "actual_resolution_seconds": raw_data["actual_resolution_seconds"],
                    "point_count": raw_points,
                },
                "7d_aggregate_series": {
                    "elapsed_seconds": round(rollup_seconds, 3),
                    "response_bytes": rollup_bytes,
                    "actual_resolution_seconds": rollup_data["actual_resolution_seconds"],
                    "point_count": rollup_points,
                },
            }, indent=2))

        if args.serve:
            # Provide a dense 7-day view: six days of minute rollups plus the
            # latest day of raw points. Keep it bound to loopback and temporary.
            served_rollup_start = now - timedelta(days=7)
            with app.state.sessions() as session:
                insert_rollups(session, probe_ids, served_rollup_start, 6 * 24 * 60)
                session.commit()
            config.raw_retention_days = 1
            config.agent_offline_seconds = 3600
            config.stream_offline_seconds = 3600
            print(f"Temporary UI test token: rtmp-monitor-local-load-fixture-token\nTemporary dashboard: http://127.0.0.1:{args.port}/", flush=True)
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

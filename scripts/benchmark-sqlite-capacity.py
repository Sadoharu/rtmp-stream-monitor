#!/usr/bin/env python3
"""Measure series API and SQLite fixture writes with multiple streams/probes.

This is a capacity check on temporary synthetic telemetry, not product data or
a production-host SLA. The default workload models four streams, ten probes per
stream, one hour of raw samples, and seven days of minute aggregates.
"""

from __future__ import annotations

import gzip
import json
import socket
import threading
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import insert
import uvicorn

from rtmp_monitor.api import create_app
from rtmp_monitor.config import CentralFileConfig
from rtmp_monitor.db import MetricAggregate, Telemetry, utcnow


STREAM_COUNT = 4
PROBES_PER_STREAM = 10
RAW_SECONDS = 60 * 60
ROLLUP_MINUTES = 7 * 24 * 60
INSERT_BATCH_ROWS = 10_000


def _add_raw_samples(session, probe_ids: list[list[str]], start: datetime) -> int:
    batch: list[dict] = []
    written = 0
    statement = insert(Telemetry)
    for second in range(RAW_SECONDS):
        observed_at = start + timedelta(seconds=second)
        for stream_index, stream_probes in enumerate(probe_ids):
            for probe_index, probe_id in enumerate(stream_probes):
                bitrate = 4_000_000 + stream_index * 100_000 + probe_index * 10_000 + second % 1000
                batch.append({
                    "id": f"raw-{stream_index}-{probe_index}-{second}",
                    "agent_id": probe_id,
                    "stream_id": f"capacity-{stream_index + 1:02d}",
                    "observed_at": observed_at,
                    "received_at": observed_at,
                    "status": "OK",
                    "metrics": {
                        "profile": "LIGHT",
                        "sample_interval_seconds": 1.0,
                        "received_media_bitrate_bps": bitrate,
                        "received_media_bitrate_quality": "MEASURED",
                        "measurement_window_seconds": 1.0,
                    },
                    "events": [],
                    "context": {},
                })
                if len(batch) >= INSERT_BATCH_ROWS:
                    session.execute(statement, batch)
                    written += len(batch)
                    batch.clear()
    if batch:
        session.execute(statement, batch)
        written += len(batch)
    return written


def _add_rollups(session, probe_ids: list[list[str]], start: datetime) -> int:
    batch: list[dict] = []
    written = 0
    statement = insert(MetricAggregate)
    for minute in range(ROLLUP_MINUTES):
        bucket_start = start + timedelta(minutes=minute)
        last_observed = (bucket_start + timedelta(seconds=59)).isoformat()
        for stream_index, stream_probes in enumerate(probe_ids):
            for probe_index, probe_id in enumerate(stream_probes):
                average = 4_000_000 + stream_index * 100_000 + probe_index * 10_000 + minute % 1000
                if stream_index == STREAM_COUNT - 1 and probe_index == PROBES_PER_STREAM - 1 and minute == 600:
                    average = 650_000
                batch.append({
                    "agent_id": probe_id,
                    "bucket_start": bucket_start,
                    "stream_id": f"capacity-{stream_index + 1:02d}",
                    "bucket_seconds": 60,
                    "sample_count": 60,
                    "status": "OK",
                    "metrics": {
                        "received_media_bitrate_bps": {
                            "min": max(0, average - 10_000),
                            "avg": average,
                            "max": average + 10_000,
                            "count": 60,
                            "last_observed_at": last_observed,
                        },
                        "measurement_window_seconds": {"avg": 1.0, "count": 60},
                    },
                    "event_counts": {},
                })
                if len(batch) >= INSERT_BATCH_ROWS:
                    session.execute(statement, batch)
                    written += len(batch)
                    batch.clear()
    if batch:
        session.execute(statement, batch)
        written += len(batch)
    return written


def _request_json(base_url: str, token: str, path: str, *, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        },
    )
    started = time.perf_counter()
    with urlopen(request, timeout=60) as response:
        content = response.read()
        content_encoding = response.headers.get("Content-Encoding", "identity")
    wire_bytes = len(content)
    if content_encoding == "gzip":
        content = gzip.decompress(content)
    return json.loads(content), time.perf_counter() - started, wire_bytes, content_encoding


def _series_request(base_url: str, token: str, stream_id: str, start: datetime, end: datetime, resolution: str):
    query = urlencode({"from": start.isoformat(), "to": end.isoformat(), "resolution": resolution})
    result = _request_json(base_url, token, f"/api/v2/streams/{stream_id}/series?{query}")
    if result[3] != "gzip":
        raise RuntimeError(f"Series response for {stream_id} was not gzip-compressed")
    return result[:3]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rtmp-sqlite-capacity-") as temporary:
        root = Path(temporary)
        admin_file = root / "admin.token"
        config = CentralFileConfig(
            database_url=f"sqlite:///{(root / 'central.db').as_posix()}",
            admin_token_file=admin_file,
            raw_retention_days=7,
        )
        app = create_app(config)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=5)
            raise RuntimeError("Temporary Uvicorn API did not become ready")

        base_url = f"http://127.0.0.1:{port}"
        try:
            token = admin_file.read_text(encoding="utf-8").strip()
            probe_ids: list[list[str]] = []
            for stream_index in range(STREAM_COUNT):
                stream_id = f"capacity-{stream_index + 1:02d}"
                _request_json(base_url, token, "/api/v1/streams", method="POST", body={"id": stream_id, "name": stream_id})
                stream_probes = []
                for probe_index in range(PROBES_PER_STREAM):
                    role = "SERVER_EGRESS" if probe_index == 0 else "CLIENT"
                    response, _, _, _ = _request_json(base_url, token, "/api/v1/agents", method="POST", body={
                        "name": f"capacity-{stream_index + 1:02d}-probe-{probe_index + 1:02d}",
                        "location": "temporary-sqlite-capacity-check",
                        "platform": "Test fixture",
                        "role": role,
                        "stream_id": stream_id,
                    })
                    stream_probes.append(response["id"])
                probe_ids.append(stream_probes)

            now = datetime.fromtimestamp(int(utcnow().timestamp()), timezone.utc)
            raw_start = now - timedelta(seconds=RAW_SECONDS)
            raw_cutoff = now - timedelta(days=7)
            rollup_end = datetime.fromtimestamp(int(raw_cutoff.timestamp() // 60 * 60), timezone.utc)
            rollup_start = rollup_end - timedelta(minutes=ROLLUP_MINUTES)

            with app.state.sessions() as session:
                raw_started = time.perf_counter()
                raw_rows = _add_raw_samples(session, probe_ids, raw_start)
                session.commit()
                raw_insert_seconds = time.perf_counter() - raw_started

                rollup_started = time.perf_counter()
                rollup_rows = _add_rollups(session, probe_ids, rollup_start)
                session.commit()
                rollup_insert_seconds = time.perf_counter() - rollup_started

            raw_results = []
            rollup_results = []
            for stream_index in range(STREAM_COUNT):
                stream_id = f"capacity-{stream_index + 1:02d}"
                raw, raw_seconds, raw_bytes = _series_request(base_url, token, stream_id, raw_start, now, "1s")
                raw_points = sum(len(series["points"]) for series in raw["series"])
                if len(raw["series"]) != PROBES_PER_STREAM or raw_points != PROBES_PER_STREAM * RAW_SECONDS:
                    raise RuntimeError(f"Raw query for {stream_id} did not return all measured samples: {raw_points}")
                if raw_bytes >= 4_600_000:
                    raise RuntimeError(f"Gzip did not materially reduce the one-hour response for {stream_id}: {raw_bytes} bytes")
                raw_results.append({
                    "stream_id": stream_id,
                    "elapsed_seconds": round(raw_seconds, 3),
                    "response_bytes": raw_bytes,
                    "point_count": raw_points,
                    "actual_resolution_seconds": raw["actual_resolution_seconds"],
                })

                rollup, rollup_seconds, rollup_bytes = _series_request(base_url, token, stream_id, rollup_start, rollup_end, "1m")
                rollup_points = sum(len(series["points"]) for series in rollup["series"])
                if len(rollup["series"]) != PROBES_PER_STREAM or rollup_points < PROBES_PER_STREAM * 9_900:
                    raise RuntimeError(f"Rollup query for {stream_id} did not return its seven-day series: {rollup_points}")
                if rollup_bytes >= 12_800_000:
                    raise RuntimeError(f"Gzip did not materially reduce the seven-day response for {stream_id}: {rollup_bytes} bytes")
                rollup_results.append({
                    "stream_id": stream_id,
                    "elapsed_seconds": round(rollup_seconds, 3),
                    "response_bytes": rollup_bytes,
                    "point_count": rollup_points,
                    "actual_resolution_seconds": rollup["actual_resolution_seconds"],
                })
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("Temporary Uvicorn API did not shut down cleanly")

        database_size = sum(path.stat().st_size for path in root.glob("central.db*"))
        print(json.dumps({
            "database": "temporary SQLite fixture (deleted automatically)",
            "stream_count": STREAM_COUNT,
            "probes_per_stream": PROBES_PER_STREAM,
            "total_probes": STREAM_COUNT * PROBES_PER_STREAM,
            "raw_hours_per_probe": RAW_SECONDS / 3600,
            "rollup_days_per_probe": ROLLUP_MINUTES / (24 * 60),
            "raw_rows": raw_rows,
            "aggregate_rows": rollup_rows,
            "database_and_wal_bytes": database_size,
            "insert_seconds": {
                "raw": round(raw_insert_seconds, 3),
                "aggregates": round(rollup_insert_seconds, 3),
            },
            "one_hour_raw_series_queries": raw_results,
            "seven_day_aggregate_series_queries": rollup_results,
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

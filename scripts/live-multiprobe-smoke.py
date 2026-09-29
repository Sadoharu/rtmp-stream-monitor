#!/usr/bin/env python3
"""Run multiple local probe agents against one live RTMP source and collector."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import uvicorn

from rtmp_monitor.agent import AgentRunner
from rtmp_monitor.api import create_app
from rtmp_monitor.config import AgentFileConfig, CentralFileConfig


def request(base: str, path: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode())


async def run_probes(configs: list[AgentFileConfig], seconds: float) -> None:
    tasks = [asyncio.create_task(AgentRunner(config).run()) for config in configs]
    try:
        await asyncio.sleep(seconds)
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        unexpected = [result for result in results if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError)]
        if unexpected:
            raise RuntimeError(f"Probe agent stopped unexpectedly: {unexpected[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="RTMP URL to probe")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--profiles", nargs="+", choices=("DEEP", "LIGHT"), default=("DEEP", "LIGHT"))
    parser.add_argument("--network", action="store_true", help="collect RTT, packet loss, and platform TCP counters")
    parser.add_argument("--require-event", action="append", default=[], help="fail unless this event code appears; repeatable")
    parser.add_argument(
        "--max-bitrate-floor-ratio",
        type=float,
        help="fail if min measured bucket / max measured bucket exceeds this ratio (0..1)",
    )
    parser.add_argument("--show-buckets", action="store_true", help="print 1-second min/avg/max samples and gaps")
    parser.add_argument(
        "--serve-after",
        type=int,
        default=0,
        help="keep the loopback dashboard and temporary data available for N seconds after the report (1..600)",
    )
    args = parser.parse_args()
    if args.seconds < 5:
        parser.error("--seconds must be at least 5 so measured bitrate windows can warm up")
    if args.max_bitrate_floor_ratio is not None and not 0 < args.max_bitrate_floor_ratio <= 1:
        parser.error("--max-bitrate-floor-ratio must be greater than 0 and at most 1")
    if args.serve_after and not 1 <= args.serve_after <= 600:
        parser.error("--serve-after must be between 1 and 600 seconds")
    target = urlsplit(args.url)
    if not target.hostname:
        parser.error("URL must include a host")
    default_port = {"rtmp": 1935, "rtmps": 443}.get(target.scheme.lower(), 1935)
    target_port = target.port or default_port

    with tempfile.TemporaryDirectory(prefix="rtmp-live-multiprobe-") as temporary:
        root = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        central_url = f"http://127.0.0.1:{port}"
        central_config = CentralFileConfig(
            database_url=f"sqlite:///{(root / 'central.db').as_posix()}",
            admin_token_file=root / "admin.token",
            logs_dir=root / "central-logs",
            bind_host="127.0.0.1",
            bind_port=port,
            agent_offline_seconds=max(30, int(args.seconds + 5)),
            stream_offline_seconds=max(30, int(args.seconds + 5)),
        )
        server = uvicorn.Server(uvicorn.Config(create_app(central_config), host="127.0.0.1", port=port, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.1)
            if not server.started:
                raise RuntimeError("local collector did not start")

            admin = (root / "admin.token").read_text(encoding="utf-8").strip()
            stream_id = "live-multiprobe"
            request(central_url, "/api/v1/streams", admin, "POST", {"id": stream_id, "name": "live-multiprobe", "local_url": args.url, "public_url": args.url})
            configs = []
            expected_agents = {}
            for index, profile in enumerate(dict.fromkeys(args.profiles), start=1):
                agent_name = f"live-smoke-{profile.lower()}-{index}"
                agent = request(
                    central_url,
                    "/api/v1/agents",
                    admin,
                    "POST",
                    {"name": agent_name, "location": "single-host-live-smoke", "platform": "Windows" if os.name == "nt" else "Linux", "role": "CLIENT", "stream_id": stream_id},
                )
                expected_agents[agent["id"]] = {"name": agent_name, "profile": profile}
                configs.append(AgentFileConfig.model_validate({
                    "server": {"url": central_url},
                    "agent": {"id": agent["id"], "name": agent_name, "location": "single-host-live-smoke", "role": "CLIENT", "token": agent["token"], "profile": profile},
                    "streams": [{"id": stream_id, "url": args.url}],
                    "monitoring": {"heartbeat_interval": 1, "progress_interval": 1, "freeze_threshold": 2, "stall_threshold": 5},
                    "network": {"enabled": args.network, "server_host": target.hostname, "server_port": target_port, "ping_interval": 2},
                    "state_dir": root / f"agent-data-{index}",
                    "log_dir": root / f"agent-logs-{index}",
                }))

            observed_from = datetime.now(timezone.utc)
            asyncio.run(run_probes(configs, args.seconds))
            observed_to = datetime.now(timezone.utc)
            query = urlencode({"from": observed_from.isoformat(), "to": observed_to.isoformat(), "resolution": "1s"})
            dashboard = request(central_url, f"/api/v1/dashboard?stream_id={stream_id}", admin)
            series_data = request(central_url, f"/api/v2/streams/{stream_id}/series?{query}", admin)
            events_data = request(central_url, f"/api/v2/streams/{stream_id}/events?{query}", admin)

            dashboard_agents = {item["id"]: item for item in dashboard["agents"]}
            series_by_probe = {row["probe"]["id"]: row for row in series_data["series"]}
            print(f"Concurrent CLIENT probes: {len(configs)}; duration: {args.seconds:g}s; host OS: {'Windows' if os.name == 'nt' else 'Linux'}")
            print(f"Series API resolution: {series_data['actual_resolution_seconds']}s; event rows: {len(events_data['events'])}")
            event_codes = {event["code"] for event in events_data["events"]}
            for event in events_data["events"]:
                cause = event.get("cause_key") or "unknown"
                print(
                    f"Event {event['started_at']}: {event['kind']} {event['severity']} "
                    f"{event.get('probe_name') or ''} {event['code']} "
                    f"[cause/location={cause}; confidence={event.get('confidence', 'UNCONFIRMED')}]"
                )
                for evidence in event.get("evidence", [])[:4]:
                    print(
                        f"  evidence: {evidence.get('metric')}="
                        f"{evidence.get('value')} {evidence.get('unit') or ''}"
                    )
            failures = []
            for agent_id, expected in expected_agents.items():
                agent = dashboard_agents.get(agent_id)
                metrics = (agent or {}).get("metrics", {})
                row = series_by_probe.get(agent_id)
                points = [point for point in (row or {}).get("points", []) if point.get("sample_count", 0) > 0]
                quality = metrics.get("received_media_bitrate_quality")
                bitrate = metrics.get("received_media_bitrate_bps")
                cpu = metrics.get("process_cpu_percent")
                rss = metrics.get("process_rss_bytes")
                avg_mbps = round(sum(point["avg_bps"] for point in points) / len(points) / 1_000_000, 3) if points else None
                floor_bps = min((point["min_bps"] for point in points), default=None)
                peak_bps = max((point["max_bps"] for point in points), default=None)
                floor_ratio = floor_bps / peak_bps if floor_bps is not None and peak_bps else None
                print(
                    f"{expected['profile']}: status={(agent or {}).get('status')}; "
                    f"measured bitrate={bitrate} bps ({quality}); "
                    f"series measured buckets={len(points)}, mean={avg_mbps} Mbps, "
                    f"floor={round(floor_bps / 1_000_000, 3) if floor_bps is not None else None} Mbps, "
                    f"peak={round(peak_bps / 1_000_000, 3) if peak_bps is not None else None} Mbps, "
                    f"floor/peak={round(floor_ratio, 3) if floor_ratio is not None else None}; "
                    f"FFmpeg CPU={cpu}%, RSS={round(rss / 1_048_576, 1) if isinstance(rss, (int, float)) else None} MB"
                )
                if args.show_buckets:
                    for point in points:
                        print(
                            f"  bucket {point['timestamp']}: min/avg/max="
                            f"{point['min_bps']}/{point['avg_bps']}/{point['max_bps']} bps; "
                            f"samples={point['sample_count']}/{point['expected_count']}; quality={point['quality']}"
                        )
                    for gap in (row or {}).get("gaps", []):
                        print(f"  gap {gap['from']}..{gap['to']}: {gap['reason']}")
                if not points or quality != "MEASURED" or not isinstance(bitrate, (int, float)) or bitrate <= 0:
                    failures.append(expected["profile"])
                if args.max_bitrate_floor_ratio is not None and (
                    floor_ratio is None or floor_ratio > args.max_bitrate_floor_ratio
                ):
                    failures.append(f"{expected['profile']} bitrate floor ratio {floor_ratio}")
            missing_events = sorted(set(args.require_event) - event_codes)
            if missing_events:
                failures.append(f"required event(s) not recorded: {', '.join(missing_events)}")
            if args.serve_after:
                print(
                    f"Temporary dashboard: {central_url}/\n"
                    f"Temporary admin token: {admin}\n"
                    f"Dashboard remains available for {args.serve_after}s.",
                    flush=True,
                )
                time.sleep(args.serve_after)
            if failures:
                print(f"Smoke assertions failed: {'; '.join(failures)}")
                return 1
            print("Both profiles delivered measured bitrate to the same stream series. Probes share this host/network; this does not validate independent sites.")
            return 0
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

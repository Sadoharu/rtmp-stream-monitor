#!/usr/bin/env python3
"""End-to-end smoke: local collector plus one deep agent reading a live RTMP URL."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import uvicorn

from rtmp_monitor.agent import AgentRunner
from rtmp_monitor.api import create_app
from rtmp_monitor.config import AgentFileConfig, CentralFileConfig


def request(base: str, path: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode())


async def run_probe(config: AgentFileConfig, seconds: float) -> None:
    runner = AgentRunner(config)
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="RTMP URL to probe")
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--profile", choices=("DEEP", "LIGHT"), default="DEEP")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="rtmp-live-smoke-") as temporary:
        root = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        central_url = f"http://127.0.0.1:{port}"
        config = CentralFileConfig(database_url=f"sqlite:///{(root / 'central.db').as_posix()}", admin_token_file=root / "admin.token", logs_dir=root / "central-logs", bind_host="127.0.0.1", bind_port=port)
        app = create_app(config)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.1)
            if not server.started:
                raise RuntimeError("local collector did not start")
            admin = (root / "admin.token").read_text(encoding="utf-8").strip()
            stream_id = "live-smoke"
            request(central_url, "/api/v1/streams", admin, "POST", {"id": stream_id, "name": "live-smoke", "local_url": args.url, "public_url": args.url})
            agent = request(central_url, "/api/v1/agents", admin, "POST", {"name": "live-smoke-agent", "location": "smoke-test", "platform": "Windows" if __import__("os").name == "nt" else "Linux", "role": "CLIENT", "stream_id": stream_id})
            agent_config = AgentFileConfig.model_validate({
                "server": {"url": central_url},
                "agent": {"id": agent["id"], "name": agent["name"], "location": "smoke-test", "role": "CLIENT", "token": agent["token"], "profile": args.profile},
                "streams": [{"id": stream_id, "url": args.url}],
                "monitoring": {"heartbeat_interval": 2, "progress_interval": 1, "freeze_threshold": 2, "stall_threshold": 5},
                "network": {"enabled": False}, "state_dir": root / "agent-data", "log_dir": root / "agent-logs",
            })
            asyncio.run(run_probe(agent_config, args.seconds))
            data = request(central_url, "/api/v1/dashboard", admin)
            found = next(item for item in data["agents"] if item["name"] == "live-smoke-agent")
            metrics = found["metrics"]
            print(f"Agent status: {found['status']}")
            print(f"Video codec/resolution: {metrics.get('video_codec')} / {metrics.get('resolution')}")
            print(f"Video frames or packets/keyframes: {metrics.get('frames') if args.profile == 'DEEP' else metrics.get('packets')} / {metrics.get('keyframes')}")
            print(f"Latest events: {', '.join(event.get('code','') for event in found.get('events', [])) or 'none'}")
            print(f"Central samples queued: {metrics.get('queue_rows')}")
            print(f"Last video/audio frame age: {metrics.get('last_frame_age')} / {metrics.get('last_audio_frame_age')} s")
            print(f"Last FFmpeg progress age: {metrics.get('last_progress_age')} s")
            active = [item for item in data.get("incidents", []) if item.get("active") and item.get("stream_id") == stream_id]
            active_events = sorted({event.get("code", "") for incident in active for symptom in incident.get("symptoms", []) for event in symptom.get("events", []) if event.get("code")})
            print(f"Active incident diagnoses: {', '.join(item.get('diagnosis', '') for item in active) or 'none'}")
            print(f"Correlated event codes: {', '.join(active_events) or 'none'}")
            media_count = metrics.get("frames") if args.profile == "DEEP" else metrics.get("packets")
            if not media_count or not metrics.get("keyframes") or metrics.get("video_codec") != "h264":
                print("Expected H.264 frames and keyframes did not reach the collector")
                return 1
            return 0
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

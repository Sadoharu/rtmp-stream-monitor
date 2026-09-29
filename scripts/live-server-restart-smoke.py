#!/usr/bin/env python3
"""Exercise controlled encoded-stream faults and recovery through local SRS."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import uvicorn

from rtmp_monitor.agent import AgentRunner
from rtmp_monitor.api import create_app
from rtmp_monitor.config import AgentFileConfig, CentralFileConfig


def api_request(base: str, path: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def docker(*args: str, timeout: float = 30) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def wait_for_listener(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"RTMP listener did not open on 127.0.0.1:{port}")


async def wait_for_measured(
    base: str,
    token: str,
    stream_id: str,
    agent_ids: set[str],
    timeout: float,
) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    last_agents: list[dict] = []
    while time.monotonic() < deadline:
        dashboard = await asyncio.to_thread(
            api_request, base, f"/api/v1/dashboard?stream_id={stream_id}", token
        )
        found = {item["id"]: item for item in dashboard["agents"] if item["id"] in agent_ids}
        last_agents = [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "profile": (item.get("metrics") or {}).get("received_media_bitrate_quality"),
                "bitrate_bps": (item.get("metrics") or {}).get("received_media_bitrate_bps"),
                "ffmpeg_running": (item.get("metrics") or {}).get("ffmpeg_running"),
                "reconnect_count": (item.get("metrics") or {}).get("reconnect_count"),
            }
            for item in found.values()
        ]
        measured = {
            agent_id: item
            for agent_id, item in found.items()
            if item.get("metrics", {}).get("received_media_bitrate_quality") == "MEASURED"
            and (item.get("metrics", {}).get("received_media_bitrate_bps") or 0) > 0
        }
        if measured.keys() == agent_ids:
            return measured
        await asyncio.sleep(1)
    raise TimeoutError(
        "Both probes did not report measured bitrate before the deadline; "
        f"latest dashboard state: {json.dumps(last_agents, ensure_ascii=False)}"
    )


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def start_publisher(
    port: int,
    stream_key: str,
    gop_seconds: float,
    damaged_video_frames: tuple[int, int] | None = None,
) -> asyncio.subprocess.Process:
    url = f"rtmp://127.0.0.1:{port}/live/{stream_key}"
    gop_frames = round(gop_seconds * 25)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
        "-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-b:v", "900k", "-maxrate", "900k", "-bufsize", "450k",
        "-g", str(gop_frames), "-keyint_min", str(gop_frames), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "96k", "-ar", "48000",
    ]
    if damaged_video_frames is not None:
        first_frame, last_frame = damaged_video_frames
        noise = f"noise=amount=if(between(n\\,{first_frame}\\,{last_frame})\\,10\\,0)"
        command.extend(["-bsf:v", noise])
    command.extend(["-f", "flv", url])
    return await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def run_scenario(
    configs: list[AgentFileConfig],
    central_url: str,
    admin_token: str,
    stream_id: str,
    agent_ids: set[str],
    container: str,
    host_port: int,
    before_restart: float,
    after_restart: float,
    gop_seconds: float,
    damaged_video_frames: tuple[int, int] | None = None,
) -> tuple[datetime, dict[str, dict], dict[str, dict], datetime, dict]:
    tasks = [asyncio.create_task(AgentRunner(config).run()) for config in configs]
    publisher: asyncio.subprocess.Process | None = None
    observed_from = datetime.now(timezone.utc)
    restart_at: datetime | None = None
    recovered: dict[str, dict] = {}
    before_restart_dashboard: dict = {}
    try:
        publisher = await start_publisher(host_port, stream_id, gop_seconds, damaged_video_frames)
        await wait_for_measured(central_url, admin_token, stream_id, agent_ids, timeout=30)
        await asyncio.sleep(before_restart)
        before_restart_dashboard = await asyncio.to_thread(
            api_request, central_url, f"/api/v1/dashboard?stream_id={stream_id}", admin_token
        )

        restart_at = datetime.now(timezone.utc)
        print(f"Restarting isolated SRS container at {restart_at.isoformat()}.", flush=True)
        await asyncio.to_thread(docker, "restart", "--time", "1", container, timeout=20)
        await stop_process(publisher)
        publisher = None

        recovered_listener = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", host_port), timeout=0.5):
                    recovered_listener = True
                    break
            except OSError:
                await asyncio.sleep(0.25)
        if not recovered_listener:
            raise TimeoutError("SRS did not reopen its loopback RTMP port after restart")

        publisher = await start_publisher(host_port, stream_id, gop_seconds)
        recovered = await wait_for_measured(
            central_url, admin_token, stream_id, agent_ids, timeout=after_restart
        )
        await asyncio.sleep(min(3, after_restart / 4))
    finally:
        await stop_process(publisher)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    observed_to = datetime.now(timezone.utc)
    query = urlencode({"from": observed_from.isoformat(), "to": observed_to.isoformat(), "resolution": "1s"})
    events = api_request(central_url, f"/api/v2/streams/{stream_id}/events?{query}", admin_token)
    series = api_request(central_url, f"/api/v2/streams/{stream_id}/series?{query}", admin_token)
    dashboard = api_request(central_url, f"/api/v1/dashboard?stream_id={stream_id}", admin_token)
    return restart_at, recovered, {
        "events": events,
        "series": series,
        "dashboard": dashboard,
    }, observed_to, before_restart_dashboard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--srs-image", default="ossrs/srs:6.0.184")
    parser.add_argument("--before-restart", type=float, default=12)
    parser.add_argument("--after-restart", type=float, default=25)
    parser.add_argument("--publisher-gop-seconds", type=float, default=2)
    parser.add_argument("--keyframe-gap-threshold", type=float)
    parser.add_argument(
        "--damage-video-frames",
        nargs=2,
        type=int,
        metavar=("FIRST", "LAST"),
        help="deterministically corrupt encoded video packets in this inclusive frame range; requires DEEP",
    )
    parser.add_argument("--profiles", nargs="+", choices=("DEEP", "LIGHT"), default=("LIGHT", "DEEP"))
    args = parser.parse_args()
    if args.before_restart < 8 or args.after_restart < 12:
        parser.error("Use at least 8 seconds before restart and 12 seconds after restart")
    if not 1 <= args.publisher_gop_seconds <= 30:
        parser.error("--publisher-gop-seconds must be between 1 and 30")
    if args.damage_video_frames is not None:
        first_frame, last_frame = args.damage_video_frames
        if first_frame < 0 or last_frame < first_frame:
            parser.error("--damage-video-frames requires 0 <= FIRST <= LAST")
        if "DEEP" not in args.profiles:
            parser.error("--damage-video-frames requires the DEEP profile")
        recovery_time = last_frame / 25 + args.publisher_gop_seconds + 2
        if recovery_time >= args.before_restart:
            parser.error("Allow at least one GOP plus two seconds to recover before the SRS restart")
    if args.keyframe_gap_threshold is not None:
        if args.keyframe_gap_threshold < 2 or args.keyframe_gap_threshold >= args.publisher_gop_seconds:
            parser.error("The keyframe gap threshold must be at least 2 seconds and below the publisher GOP interval")
        if args.before_restart < args.publisher_gop_seconds + 2:
            parser.error("Leave at least two seconds after a long GOP before restarting SRS")

    image_present = subprocess.run(
        ["docker", "image", "inspect", args.srs_image], capture_output=True, text=True
    ).returncode == 0
    if not image_present:
        docker("pull", args.srs_image, timeout=180)
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode:
        parser.error("FFmpeg must be installed and available on PATH")

    container = f"rtmp-m6-restart-{uuid.uuid4().hex[:10]}"
    stream_id = "srs-restart"
    with tempfile.TemporaryDirectory(prefix="rtmp-server-restart-smoke-") as temporary:
        root = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            central_port = sock.getsockname()[1]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            host_port = sock.getsockname()[1]
        central_url = f"http://127.0.0.1:{central_port}"
        central_config = CentralFileConfig(
            database_url=f"sqlite:///{(root / 'central.db').as_posix()}",
            admin_token_file=root / "admin.token",
            logs_dir=root / "central-logs",
            bind_host="127.0.0.1",
            bind_port=central_port,
            agent_offline_seconds=45,
            stream_offline_seconds=45,
        )
        server = uvicorn.Server(
            uvicorn.Config(create_app(central_config), host="127.0.0.1", port=central_port, log_level="error", access_log=False)
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.1)
            if not server.started:
                raise RuntimeError("temporary loopback collector did not start")

            admin_token = (root / "admin.token").read_text(encoding="utf-8").strip()
            docker(
                "run", "--detach", "--rm", "--name", container,
                "--publish", f"127.0.0.1:{host_port}:1935", args.srs_image,
            )
            wait_for_listener(host_port, timeout=20)
            url = f"rtmp://127.0.0.1:{host_port}/live/{stream_id}"
            api_request(
                central_url,
                "/api/v1/streams",
                admin_token,
                "POST",
                {"id": stream_id, "name": "Local SRS restart test", "local_url": url, "public_url": url},
            )
            configs = []
            agent_ids = set()
            expected = {}
            for profile in dict.fromkeys(args.profiles):
                agent_name = f"restart-{profile.lower()}"
                agent = api_request(
                    central_url,
                    "/api/v1/agents",
                    admin_token,
                    "POST",
                    {"name": agent_name, "location": "localhost-test", "platform": os.name, "role": "CLIENT", "stream_id": stream_id},
                )
                agent_ids.add(agent["id"])
                expected[agent["id"]] = {"name": agent_name, "profile": profile}
                configs.append(AgentFileConfig.model_validate({
                    "server": {"url": central_url},
                    "agent": {
                        "id": agent["id"], "name": agent_name, "location": "localhost-test",
                        "role": "CLIENT", "token": agent["token"], "profile": profile,
                    },
                    "streams": [{"id": stream_id, "url": url}],
                    "monitoring": {
                        "heartbeat_interval": 1, "progress_interval": 1,
                        "freeze_threshold": 3, "stall_threshold": 8,
                        "reconnect_initial": 1, "reconnect_max": 2,
                        "keyframe_gap_threshold": args.keyframe_gap_threshold,
                    },
                    "network": {
                        "enabled": False, "server_host": "127.0.0.1", "server_port": 1935,
                        "ping_interval": 2,
                    },
                    "state_dir": root / f"agent-data-{profile.lower()}",
                    "log_dir": root / f"agent-logs-{profile.lower()}",
                }))
                configs[-1].network.server_port = host_port

            restart_at, recovered, result, observed_to, before_restart_dashboard = asyncio.run(run_scenario(
                configs, central_url, admin_token, stream_id, agent_ids, container, host_port,
                args.before_restart, args.after_restart, args.publisher_gop_seconds,
                tuple(args.damage_video_frames) if args.damage_video_frames is not None else None,
            ))
            events = result["events"]["events"]
            event_codes = {event["code"] for event in events}
            series_by_probe = {row["probe"]["id"]: row for row in result["series"]["series"]}
            dashboard_agents = {item["id"]: item for item in result["dashboard"]["agents"]}
            print(
                f"SRS container: {args.srs_image}; restart window: "
                f"{restart_at.isoformat()}..{observed_to.isoformat()}"
            )
            print(f"Event codes: {', '.join(sorted(event_codes)) or '(none)'}")
            for event in events:
                print(
                    f"  {event['started_at']} {event.get('probe_name') or '(stream)'} "
                    f"{event['code']} {event['severity']} "
                    f"confidence={event.get('confidence', 'UNCONFIRMED')} "
                    f"location={event.get('probable_location') or 'not localized'}"
                )
            failures = []
            if args.damage_video_frames is not None:
                deep_id = next(
                    agent_id for agent_id, identity in expected.items() if identity["profile"] == "DEEP"
                )
                deep_name = expected[deep_id]["name"]
                decode_events = [event for event in events if event["code"] == "DECODE_ERROR"]
                deep_decode_events = [
                    event for event in decode_events
                    if event.get("probe_name") == deep_name or event.get("probe_id") == deep_id
                ]
                pre_restart_agents = {
                    item["id"]: item for item in before_restart_dashboard.get("agents", [])
                }
                deep_metrics = (pre_restart_agents.get(deep_id) or {}).get("metrics") or {}
                deep_frame_age = deep_metrics.get("last_frame_age")
                print(
                    f"DEEP decode recovery before SRS restart: decode errors={len(deep_decode_events)}; "
                    f"frames={deep_metrics.get('frames')}; last_frame_age={deep_frame_age}; "
                    f"ffmpeg_running={deep_metrics.get('ffmpeg_running')}"
                )
                if not deep_decode_events:
                    failures.append("the event timeline did not record DECODE_ERROR for the DEEP probe")
                if not deep_metrics.get("ffmpeg_running"):
                    failures.append("the DEEP decoder was not running after the corrupted frame interval")
                if not isinstance(deep_frame_age, (int, float)) or deep_frame_age > 2:
                    failures.append("the DEEP decoder did not resume producing recent frames after corruption")
            for agent_id, identity in expected.items():
                agent = dashboard_agents.get(agent_id) or {}
                metrics = agent.get("metrics") or {}
                row = series_by_probe.get(agent_id) or {}
                post_restart_points = [
                    point for point in row.get("points", [])
                    if point.get("sample_count", 0) > 0
                    and point.get("quality") == "MEASURED"
                    and (point.get("avg_bps") or 0) > 0
                    and datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00")) > restart_at
                ]
                reconnects = metrics.get("reconnect_count", 0)
                bitrate = metrics.get("received_media_bitrate_bps")
                quality = metrics.get("received_media_bitrate_quality")
                recovered_bitrate = post_restart_points[-1]["avg_bps"] if post_restart_points else None
                print(
                    f"{identity['profile']}: reconnects={reconnects}; latest sample="
                    f"{bitrate} bps ({quality}); post-restart measured buckets={len(post_restart_points)}, "
                    f"last recovered average={recovered_bitrate} bps"
                )
                if reconnects < 1:
                    failures.append(f"{identity['profile']} did not reconnect FFmpeg")
                if not post_restart_points:
                    failures.append(f"{identity['profile']} has no measured bucket after server restart")
            if "FFMPEG_RESTART" not in event_codes:
                failures.append("the event timeline did not record FFMPEG_RESTART")
            if args.keyframe_gap_threshold is not None:
                for expected_event in ("KEYFRAME_GAP", "KEYFRAME_GAP_END"):
                    if expected_event not in event_codes:
                        failures.append(f"the event timeline did not record {expected_event}")
            path_diagnoses = [event for event in events if event["code"] == "CLIENT_PATH_UNCONFIRMED"]
            if path_diagnoses and any(event.get("confidence") != "UNCONFIRMED" for event in path_diagnoses):
                failures.append("the client-path diagnosis claimed confidence without an ingress/egress probe")
            if failures:
                print("Smoke assertions failed: " + "; ".join(failures))
                return 1
            print("Both profiles restored measured RTMP telemetry after the isolated SRS restart.")
            return 0
        except Exception:
            try:
                logs = docker("logs", "--tail", "80", container, timeout=10)
                if logs:
                    print("SRS container log tail:\n" + logs, flush=True)
            except Exception as log_error:
                print(f"Could not collect SRS container logs: {log_error}", flush=True)
            raise
        finally:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, text=True, timeout=30)
            server.should_exit = True
            server_thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

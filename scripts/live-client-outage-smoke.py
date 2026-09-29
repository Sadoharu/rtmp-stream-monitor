#!/usr/bin/env python3
"""Verify client-only RTMP outage or IP packet loss while server egress stays healthy."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
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


def free_ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [int(sock.getsockname()[1]) for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def wait_for_listener(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"RTMP listener did not open on 127.0.0.1:{port}")


class ClientRtmpProxy:
    """Forward RTMP TCP sessions and close only the client side during the fault window."""

    def __init__(self, listen_port: int, target_port: int) -> None:
        self.listen_port = listen_port
        self.target_port = target_port
        self.blocked = False
        self.server: asyncio.Server | None = None
        self.connections: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.listen_port)

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            data = await reader.read(64 * 1024)
            if not data:
                return
            writer.write(data)
            await writer.drain()

    async def _close_writers(self, *writers: asyncio.StreamWriter) -> None:
        for writer in writers:
            writer.close()
        try:
            await asyncio.wait_for(
                asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True),
                timeout=2,
            )
        except asyncio.TimeoutError:
            pass

    async def _handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        if self.blocked:
            await self._close_writers(client_writer)
            return
        try:
            server_reader, server_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            await self._close_writers(client_writer)
            return
        if self.blocked:
            await self._close_writers(client_writer, server_writer)
            return

        pair = (client_writer, server_writer)
        self.connections.add(pair)
        tasks = {
            asyncio.create_task(self._pipe(client_reader, server_writer)),
            asyncio.create_task(self._pipe(server_reader, client_writer)),
        }
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.connections.discard(pair)
            await self._close_writers(client_writer, server_writer)

    async def interrupt_clients(self) -> int:
        self.blocked = True
        listener = self.server
        if self.server:
            self.server.close()
            self.server = None
        active = list(self.connections)
        for client_writer, server_writer in active:
            client_writer.close()
            server_writer.close()
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(writer.wait_closed() for pair in active for writer in pair),
                    return_exceptions=True,
                ),
                timeout=2,
            )
        except asyncio.TimeoutError:
            pass
        if listener:
            try:
                await asyncio.wait_for(listener.wait_closed(), timeout=2)
            except asyncio.TimeoutError:
                pass
        return len(active)

    async def restore(self) -> None:
        self.blocked = False
        if self.server is None:
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.listen_port)

    async def close(self) -> None:
        await self.interrupt_clients()


class DockerNetemClientProxy:
    """RTMP relay in its own Linux network namespace with optional tc loss."""

    def __init__(self, listen_port: int, network_name: str, target_name: str = "srs") -> None:
        suffix = uuid.uuid4().hex[:10]
        self.listen_port = listen_port
        self.network_name = network_name
        self.target_name = target_name
        self.container = f"rtmp-m6-netem-{suffix}"
        self.image = f"rtmp-m6-netem-proxy:{suffix}"
        self.image_built = False
        self.container_started = False
        self.context = Path(__file__).resolve().parent / "netem-proxy"

    async def start(self) -> None:
        await asyncio.to_thread(
            docker, "build", "--quiet", "--tag", self.image, str(self.context), timeout=180
        )
        self.image_built = True
        await asyncio.to_thread(
            docker, "run", "--detach", "--rm", "--name", self.container,
            "--cap-add", "NET_ADMIN", "--network", self.network_name,
            "--publish", f"127.0.0.1:{self.listen_port}:1935",
            "--env", f"RTMP_TARGET_HOST={self.target_name}",
            self.image, timeout=30,
        )
        self.container_started = True
        await asyncio.to_thread(wait_for_listener, self.listen_port, 30)

    async def set_packet_loss(self, percent: float) -> None:
        await asyncio.to_thread(
            docker, "exec", self.container, "tc", "qdisc", "replace", "dev", "eth0",
            "root", "netem", "loss", f"{percent:g}%",
        )

    async def clear_packet_loss(self) -> None:
        await asyncio.to_thread(
            docker, "exec", self.container, "tc", "qdisc", "del", "dev", "eth0", "root"
        )

    async def dropped_packet_count(self) -> int:
        output = await asyncio.to_thread(docker, "exec", self.container, "tc", "-s", "qdisc", "show", "dev", "eth0")
        match = re.search(r"\bdropped\s+(\d+)\b", output)
        if not match:
            raise RuntimeError(f"Unable to read tc netem drop counter: {output}")
        return int(match.group(1))

    async def close(self) -> None:
        if self.container_started:
            await asyncio.to_thread(
                subprocess.run, ["docker", "rm", "--force", self.container],
                capture_output=True, text=True, timeout=30,
            )
            self.container_started = False
        if self.image_built:
            await asyncio.to_thread(
                subprocess.run, ["docker", "image", "rm", "--force", self.image],
                capture_output=True, text=True, timeout=30,
            )
            self.image_built = False


async def start_publisher(port: int, stream_key: str) -> asyncio.subprocess.Process:
    url = f"rtmp://127.0.0.1:{port}/live/{stream_key}"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
        "-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-b:v", "900k", "-maxrate", "900k", "-bufsize", "450k",
        "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-f", "flv", url,
    ]
    return await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )


async def dashboard_agents(base: str, token: str, stream_id: str, agent_ids: set[str]) -> dict[str, dict]:
    dashboard = await asyncio.to_thread(
        api_request, base, f"/api/v1/dashboard?stream_id={stream_id}", token
    )
    return {item["id"]: item for item in dashboard["agents"] if item["id"] in agent_ids}


async def wait_for_measured(
    base: str, token: str, stream_id: str, agent_ids: set[str], timeout: float
) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    last: dict[str, dict] = {}
    while time.monotonic() < deadline:
        last = await dashboard_agents(base, token, stream_id, agent_ids)
        found = {
            agent_id: item for agent_id, item in last.items()
            if item.get("metrics", {}).get("received_media_bitrate_quality") == "MEASURED"
            and (item.get("metrics", {}).get("received_media_bitrate_bps") or 0) > 0
        }
        if found.keys() == agent_ids:
            return found
        await asyncio.sleep(1)
    raise TimeoutError(f"Probes did not report measured media: {json.dumps(last, ensure_ascii=False)}")


async def wait_for_recovery(
    base: str,
    token: str,
    stream_id: str,
    agent_ids: set[str],
    reconnect_baseline: dict[str, int],
    timeout: float,
) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    last: dict[str, dict] = {}
    while time.monotonic() < deadline:
        last = await dashboard_agents(base, token, stream_id, agent_ids)
        recovered = {
            agent_id: item for agent_id, item in last.items()
            if item.get("metrics", {}).get("received_media_bitrate_quality") == "MEASURED"
            and (item.get("metrics", {}).get("received_media_bitrate_bps") or 0) > 0
            and item.get("metrics", {}).get("reconnect_count", 0) > reconnect_baseline[agent_id]
        }
        if recovered.keys() == agent_ids:
            return recovered
        await asyncio.sleep(1)
    raise TimeoutError(f"Probes did not reconnect and recover measured media: {json.dumps(last, ensure_ascii=False)}")


async def wait_for_fresh_measured(
    base: str,
    token: str,
    stream_id: str,
    agent_ids: set[str],
    after: datetime,
    timeout: float,
) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    last: dict[str, dict] = {}
    while time.monotonic() < deadline:
        last = await dashboard_agents(base, token, stream_id, agent_ids)
        fresh = {}
        for agent_id, item in last.items():
            try:
                seen_at = datetime.fromisoformat(item["last_seen_at"].replace("Z", "+00:00"))
            except (KeyError, AttributeError, TypeError, ValueError):
                continue
            metrics = item.get("metrics", {})
            if (
                seen_at > after
                and metrics.get("received_media_bitrate_quality") == "MEASURED"
                and (metrics.get("received_media_bitrate_bps") or 0) > 0
                and metrics.get("ffmpeg_running") is True
            ):
                fresh[agent_id] = item
        if fresh.keys() == agent_ids:
            return fresh
        await asyncio.sleep(1)
    raise TimeoutError(f"Probes did not publish fresh measured media after packet-loss recovery: {json.dumps(last, ensure_ascii=False)}")


async def run_fault_window(
    configs: list[AgentFileConfig],
    proxy: ClientRtmpProxy | DockerNetemClientProxy,
    srs_port: int,
    base: str,
    token: str,
    stream_id: str,
    agent_ids: set[str],
    client_ids: set[str],
    outage_after: float,
    outage_duration: float,
    recovery_timeout: float,
    packet_loss_percent: float | None = None,
) -> tuple[datetime, datetime, datetime, dict[str, dict], dict[str, dict], int | None]:
    tasks: list[asyncio.Task] = []
    publisher: asyncio.subprocess.Process | None = None
    try:
        await proxy.start()
        publisher = await start_publisher(srs_port, stream_id)
        # Publisher goes directly to SRS; only reader clients are routed through the fault proxy.
        await asyncio.sleep(0.8)
        tasks = [asyncio.create_task(AgentRunner(config).run()) for config in configs]
        baseline = await wait_for_measured(base, token, stream_id, agent_ids, timeout=35)
        reconnect_baseline = {
            agent_id: int(item.get("metrics", {}).get("reconnect_count", 0))
            for agent_id, item in baseline.items() if agent_id in client_ids
        }
        await asyncio.sleep(outage_after)
        interrupted_at = datetime.now(timezone.utc)
        dropped_packets: int | None = None
        if packet_loss_percent is None:
            assert isinstance(proxy, ClientRtmpProxy)
            active_connections = await proxy.interrupt_clients()
            print(
                f"Closing {active_connections} client RTMP sockets at {interrupted_at.isoformat()}; "
                f"publisher remains connected directly to SRS.", flush=True
            )
            if active_connections < 1:
                raise RuntimeError("No active client RTMP session was present at the outage start")
            await asyncio.sleep(outage_duration)
            if publisher.returncode is not None:
                raise RuntimeError("The direct-to-SRS publisher exited during the client-only outage")
            await proxy.restore()
            restored_at = datetime.now(timezone.utc)
            print(f"Restored client forwarding at {restored_at.isoformat()}.", flush=True)
            recovered = await wait_for_recovery(
                base, token, stream_id, client_ids, reconnect_baseline, timeout=recovery_timeout
            )
        else:
            assert isinstance(proxy, DockerNetemClientProxy)
            await proxy.set_packet_loss(packet_loss_percent)
            print(
                f"Applied {packet_loss_percent:g}% IP packet loss to the isolated client proxy at "
                f"{interrupted_at.isoformat()}; publisher and SERVER_EGRESS remain direct to SRS.", flush=True
            )
            await asyncio.sleep(outage_duration)
            if publisher.returncode is not None:
                raise RuntimeError("The direct-to-SRS publisher exited during the client packet-loss window")
            dropped_packets = await proxy.dropped_packet_count()
            await proxy.clear_packet_loss()
            restored_at = datetime.now(timezone.utc)
            print(
                f"Removed packet loss at {restored_at.isoformat()}; tc recorded {dropped_packets} dropped packets.",
                flush=True,
            )
            if dropped_packets < 1:
                raise RuntimeError("tc netem did not report any dropped IP packets")
            recovered = await wait_for_fresh_measured(
                base, token, stream_id, client_ids, restored_at, timeout=recovery_timeout
            )
        recovered_at = datetime.now(timezone.utc)
        return interrupted_at, restored_at, recovered_at, baseline, recovered, dropped_packets
    finally:
        if publisher and publisher.returncode is None:
            publisher.terminate()
            try:
                await asyncio.wait_for(publisher.wait(), timeout=5)
            except asyncio.TimeoutError:
                publisher.kill()
                await publisher.wait()
        await proxy.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--srs-image", default="ossrs/srs:6.0.184")
    parser.add_argument("--outage-after", type=float, default=10, help="seconds before injecting the client-path fault")
    parser.add_argument("--outage-duration", type=float, default=8, help="duration of the outage or packet-loss window")
    parser.add_argument("--recovery-timeout", type=float, default=35)
    parser.add_argument(
        "--packet-loss-percent",
        type=float,
        help="inject real IP packet loss on an isolated Linux client proxy instead of closing RTMP sockets",
    )
    parser.add_argument("--profiles", nargs="+", choices=("DEEP", "LIGHT"), default=("LIGHT", "DEEP"))
    args = parser.parse_args()
    if args.outage_after < 5 or args.outage_duration < 2 or args.recovery_timeout < 10:
        parser.error("Use at least 5s before outage, 2s outage, and 10s for recovery")
    if args.packet_loss_percent is not None and not 0 < args.packet_loss_percent < 100:
        parser.error("--packet-loss-percent must be greater than 0 and below 100")
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode:
        parser.error("FFmpeg must be installed and available on PATH")
    image_present = subprocess.run(
        ["docker", "image", "inspect", args.srs_image], capture_output=True, text=True
    ).returncode == 0
    if not image_present:
        docker("pull", args.srs_image, timeout=180)

    container = f"rtmp-m6-client-outage-{uuid.uuid4().hex[:10]}"
    proxy_network = f"rtmp-m6-net-{uuid.uuid4().hex[:10]}" if args.packet_loss_percent is not None else None
    stream_id = "client-outage"
    with tempfile.TemporaryDirectory(prefix="rtmp-client-outage-smoke-") as temporary:
        root = Path(temporary)
        central_port, srs_port, proxy_port = free_ports(3)
        central_url = f"http://127.0.0.1:{central_port}"
        srs_url = f"rtmp://127.0.0.1:{srs_port}/live/{stream_id}"
        proxy_url = f"rtmp://127.0.0.1:{proxy_port}/live/{stream_id}"
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
            uvicorn.Config(
                create_app(central_config), host="127.0.0.1", port=central_port,
                log_level="error", access_log=False,
            )
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.1)
            if not server.started:
                raise RuntimeError("temporary loopback collector did not start")
            token = (root / "admin.token").read_text(encoding="utf-8").strip()

            srs_command = ["run", "--detach", "--rm", "--name", container]
            if proxy_network:
                docker("network", "create", proxy_network)
                srs_command.extend(["--network", proxy_network, "--network-alias", "srs"])
            srs_command.extend(["--publish", f"127.0.0.1:{srs_port}:1935", args.srs_image])
            docker(*srs_command)
            wait_for_listener(srs_port, timeout=20)
            api_request(
                central_url,
                "/api/v1/streams",
                token,
                "POST",
                {"id": stream_id, "name": "Client-only network outage", "local_url": srs_url, "public_url": srs_url},
            )

            configs: list[AgentFileConfig] = []
            agent_ids: set[str] = set()
            client_ids: set[str] = set()
            names: dict[str, str] = {}
            for profile in dict.fromkeys(args.profiles):
                name = f"outage-{profile.lower()}"
                agent = api_request(
                    central_url,
                    "/api/v1/agents",
                    token,
                    "POST",
                    {"name": name, "location": "localhost-test", "platform": os.name, "role": "CLIENT", "stream_id": stream_id},
                )
                agent_id = agent["id"]
                agent_ids.add(agent_id)
                client_ids.add(agent_id)
                names[agent_id] = name
                config = AgentFileConfig.model_validate({
                    "server": {"url": central_url},
                    "agent": {
                        "id": agent_id, "name": name, "location": "localhost-test",
                        "role": "CLIENT", "token": agent["token"], "profile": profile,
                    },
                    "streams": [{"id": stream_id, "url": proxy_url}],
                    "monitoring": {
                        "heartbeat_interval": 1, "progress_interval": 1,
                        "freeze_threshold": 3, "stall_threshold": 8,
                        "reconnect_initial": 0.5, "reconnect_max": 2,
                    },
                    "network": {
                        "enabled": True, "server_host": "127.0.0.1", "server_port": proxy_port,
                        "ping_interval": 2,
                    },
                    "state_dir": root / f"agent-data-{profile.lower()}",
                    "log_dir": root / f"agent-logs-{profile.lower()}",
                })
                configs.append(config)

            egress_name = "server-egress"
            egress_agent = api_request(
                central_url,
                "/api/v1/agents",
                token,
                "POST",
                {"name": egress_name, "location": "localhost-test", "platform": os.name, "role": "SERVER_EGRESS", "stream_id": stream_id},
            )
            egress_id = egress_agent["id"]
            agent_ids.add(egress_id)
            names[egress_id] = egress_name
            configs.append(AgentFileConfig.model_validate({
                "server": {"url": central_url},
                "agent": {
                    "id": egress_id, "name": egress_name, "location": "localhost-test",
                    "role": "SERVER_EGRESS", "token": egress_agent["token"], "profile": "LIGHT",
                },
                "streams": [{"id": stream_id, "url": srs_url}],
                "monitoring": {
                    "heartbeat_interval": 1, "progress_interval": 1,
                    "freeze_threshold": 3, "stall_threshold": 8,
                    "reconnect_initial": 0.5, "reconnect_max": 2,
                },
                "network": {"enabled": False, "server_host": "127.0.0.1", "server_port": srs_port},
                "state_dir": root / "agent-data-egress",
                "log_dir": root / "agent-logs-egress",
            }))

            proxy: ClientRtmpProxy | DockerNetemClientProxy
            if proxy_network:
                proxy = DockerNetemClientProxy(proxy_port, proxy_network)
            else:
                proxy = ClientRtmpProxy(proxy_port, srs_port)
            interrupted_at, restored_at, recovered_at, baseline, recovered, dropped_packets = asyncio.run(run_fault_window(
                configs, proxy, srs_port, central_url, token, stream_id, agent_ids, client_ids,
                args.outage_after, args.outage_duration, args.recovery_timeout,
                args.packet_loss_percent,
            ))

            query = urlencode({
                "from": interrupted_at.isoformat(),
                "to": recovered_at.isoformat(),
                "resolution": "1s",
            })
            result = api_request(
                central_url, f"/api/v2/streams/{stream_id}/events?{query}", token
            )
            series_result = api_request(
                central_url, f"/api/v2/streams/{stream_id}/series?{query}", token
            )
            events = []
            for event in result.get("events", []):
                if not event.get("started_at"):
                    continue
                event_at = datetime.fromisoformat(event["started_at"].replace("Z", "+00:00"))
                if interrupted_at <= event_at <= recovered_at:
                    events.append(event)
            restart_events = []
            for event in events:
                if event.get("code") == "FFMPEG_RESTART":
                    restart_events.append(event)
            event_codes = {event.get("code") for event in events}
            print(f"Event codes during outage/recovery: {', '.join(sorted(code for code in event_codes if code)) or '(none)'}")
            for agent_id in client_ids:
                before = baseline[agent_id].get("metrics", {})
                after = recovered[agent_id].get("metrics", {})
                print(
                    f"{names[agent_id]}: bitrate {before.get('received_media_bitrate_bps')} -> "
                    f"{after.get('received_media_bitrate_bps')} bps; reconnects "
                    f"{before.get('reconnect_count', 0)} -> {after.get('reconnect_count', 0)}"
                )
            egress_row = next(
                (row for row in series_result.get("series", []) if row.get("probe", {}).get("id") == egress_id),
                {},
            )
            egress_points = [
                point for point in egress_row.get("points", [])
                if point.get("quality") == "MEASURED"
                and point.get("sample_count", 0) > 0
                and (point.get("avg_bps") or 0) > 0
                and interrupted_at <= datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00")) <= restored_at
            ]
            egress_mean = (
                sum(float(point["avg_bps"]) for point in egress_points) / len(egress_points)
                if egress_points else 0
            )
            print(
                f"SERVER_EGRESS during the client outage: {len(egress_points)} measured buckets, "
                f"mean {egress_mean:.0f} bps."
            )
            for event in events:
                print(
                    f"  {event.get('started_at')} {event.get('probe_name') or '(stream)'} "
                    f"{event.get('code')} confidence={event.get('confidence', 'not provided by endpoint')} "
                    f"location={event.get('probable_location') or 'not localized'}"
                )
            failures = []
            expected_client_names = {names[agent_id] for agent_id in client_ids}
            if args.packet_loss_percent is None:
                if not restart_events:
                    failures.append("the event timeline did not record FFMPEG_RESTART after the client outage")
                restart_probe_names = {event.get("probe_name") for event in restart_events}
                missing_restart_names = expected_client_names - restart_probe_names
                if missing_restart_names:
                    failures.append(
                        "the event timeline has no FFMPEG_RESTART for " + ", ".join(sorted(missing_restart_names))
                    )
                if "NETWORK_PATH_PROBLEM" not in event_codes:
                    failures.append("healthy SERVER_EGRESS and failed client TCP path did not produce NETWORK_PATH_PROBLEM")
            elif not dropped_packets:
                failures.append("the isolated tc netem interface did not report an IP packet drop")
            if len(egress_points) < 2:
                failures.append("SERVER_EGRESS did not provide at least two measured buckets during the client outage")
            if restored_at <= interrupted_at:
                failures.append("client forwarding restore timestamp is not after the outage")
            path_diagnoses = [event for event in events if event.get("code") == "CLIENT_PATH_UNCONFIRMED"]
            if path_diagnoses and any(event.get("confidence") != "UNCONFIRMED" for event in path_diagnoses):
                failures.append("client-path diagnosis claimed confidence without server ingress/egress evidence")
            if failures:
                print("Smoke assertions failed: " + "; ".join(failures))
                return 1
            if args.packet_loss_percent is None:
                print("Client-only RTMP outage recorded reconnects and both profiles recovered measured bitrate.")
            else:
                print(
                    f"tc netem dropped {dropped_packets} IP packets on the client-only path; "
                    "SERVER_EGRESS stayed measurable and clients published fresh measured media after recovery."
                )
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
            if proxy_network:
                subprocess.run(["docker", "network", "rm", proxy_network], capture_output=True, text=True, timeout=30)
            server.should_exit = True
            server_thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

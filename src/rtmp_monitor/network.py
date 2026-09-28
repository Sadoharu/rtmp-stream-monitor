from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
from typing import Any


def _run(args: list[str], timeout: float = 2.0, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, env=env)
        return result.stdout + result.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ""


class NetworkTelemetry:
    """Best-effort transport counters, isolated behind a platform provider."""

    def __init__(self, host: str | None, port: int, enabled: bool = True):
        self.host, self.port, self.enabled = host, port, enabled
        self.system_retransmits: int | None = None
        self.last_sample_mono: float | None = None
        self.previous_system_retransmits: int | None = None

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        if not self.enabled or not self.host:
            return {"available": False, "reason": "disabled or no server_host configured"}
        result: dict[str, Any] = {"available": True, "rtt_ms": None, "packet_loss_percent": None, "tcp_retransmissions": None, "tcp_state": None, "provider": platform.system().lower()}
        result.update(self._ping())
        system = platform.system().lower()
        if system == "linux":
            result.update(self._linux_socket_stats())
        elif system == "windows":
            result.update(self._windows_tcp_stats(now))
        else:
            result["available"] = result["rtt_ms"] is not None
            result["reason"] = "TCP counters not implemented on this platform"
        self.last_sample_mono = now
        return result


    def _ping(self) -> dict[str, Any]:
        is_windows = platform.system().lower() == "windows"
        if is_windows:
            # Use structured .NET output instead of parsing localized ping.exe text.
            script = (
                "$ping=New-Object System.Net.NetworkInformation.Ping; $times=@(); "
                "try { for ($i=0; $i -lt 3; $i++) { "
                "try { $reply=$ping.Send($env:RTMP_MONITOR_PING_ADDRESS,1000); "
                "if ($reply.Status -eq [System.Net.NetworkInformation.IPStatus]::Success) "
                "{ $times += [int64]$reply.RoundtripTime } } catch {} } } finally { $ping.Dispose() }; "
                "$rtt=$null; if ($times.Count -gt 0) "
                "{ $rtt=[double](($times | Measure-Object -Average).Average) }; "
                "[pscustomobject]@{sent=3;received=$times.Count;rtt_ms=$rtt}|ConvertTo-Json -Compress"
            )
            command_env = os.environ.copy()
            command_env["RTMP_MONITOR_PING_ADDRESS"] = self.host
            output = _run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=5,
                env=command_env,
            )
            try:
                result = json.loads(output)
                sent = int(result["sent"])
                received = int(result["received"])
                rtt = result.get("rtt_ms")
                if sent <= 0 or received < 0 or received > sent:
                    raise ValueError("invalid ping counters")
                return {
                    "rtt_ms": float(rtt) if isinstance(rtt, (int, float)) else None,
                    "packet_loss_percent": (sent - received) / sent * 100,
                }
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return {"rtt_ms": None, "packet_loss_percent": None}

        args = ["ping", "-n", "-c", "3", "-W", "1", self.host]
        output = _run(args, timeout=5)
        rtt = re.search(r"(?:time[=<]|Average\s*=\s*)(\d+(?:\.\d+)?)\s*ms", output, re.I)
        linux_stats = re.search(r"(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets )?received", output, re.I)
        stats = linux_stats
        loss = (int(stats.group(1)) - int(stats.group(2))) / int(stats.group(1)) * 100 if stats and int(stats.group(1)) else None
        return {"rtt_ms": float(rtt.group(1)) if rtt else None, "packet_loss_percent": loss}

    def _linux_socket_stats(self) -> dict[str, Any]:
        output = _run(["ss", "-tin", "dst", self.host, "dport", "=", f":{self.port}"], timeout=2)
        if not output:
            return {"tcp_retransmissions": None, "tcp_state": "unknown", "provider_note": "ss unavailable or no matching flow"}
        rtt = re.search(r"\brtt:([\d.]+)/", output)
        retrans = re.search(r"\bretrans:(\d+)/(\d+)", output)
        state = "ESTABLISHED" if "ESTAB" in output else "not-established"
        return {"rtt_ms": float(rtt.group(1)) if rtt else None, "tcp_retransmissions": int(retrans.group(2)) if retrans else None, "tcp_state": state, "provider_note": "per-flow TCP_INFO via ss"}

    def _windows_tcp_stats(self, now: float) -> dict[str, Any]:
        script = "$s=Get-NetTCPStatistics -ErrorAction Stop; [pscustomobject]@{retrans=[int64]$s.SegmentsRetransmitted}|ConvertTo-Json -Compress"
        output = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=3)
        retrans = None
        match = re.search(r'"?retrans"?\s*:\s*(\d+)', output)
        if match:
            current = int(match.group(1))
            if self.previous_system_retransmits is not None:
                retrans = max(0, current - self.previous_system_retransmits)
            self.previous_system_retransmits = current
        conn_script = "$hostAddress=$env:RTMP_MONITOR_REMOTE_ADDRESS; $remotePort=[int]$env:RTMP_MONITOR_REMOTE_PORT; Get-NetTCPConnection -RemoteAddress $hostAddress -RemotePort $remotePort -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty State"
        command_env = os.environ.copy()
        command_env["RTMP_MONITOR_REMOTE_ADDRESS"] = self.host or ""
        command_env["RTMP_MONITOR_REMOTE_PORT"] = str(self.port)
        state = _run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", conn_script],
            timeout=3,
            env=command_env,
        ).strip() or "not-established"
        return {"tcp_retransmissions": retrans, "tcp_state": state, "provider_note": "Windows retransmits are host-wide counter deltas; RTT and packet loss use ICMP"}


def clock_status() -> dict[str, Any]:
    system = platform.system().lower()
    if system == "linux":
        sync = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=2).strip().lower()
        offset_text = _run(["chronyc", "tracking"], timeout=2)
        offset = re.search(r"Last offset\s*:\s*([+-]?[\d.]+)\s*seconds", offset_text, re.I)
        return {"ntp_synchronized": sync == "yes", "estimated_offset_ms": float(offset.group(1)) * 1000 if offset else None}
    if system == "windows":
        output = _run(["w32tm", "/query", "/status"], timeout=3)
        source = re.search(r"^Source:\s*(.+)$", output, re.I | re.M)
        offset = re.search(r"^Offset:\s*([+-]?[\d.]+)s", output, re.I | re.M)
        return {"ntp_synchronized": bool(source and "local cmos" not in source.group(1).lower()), "estimated_offset_ms": float(offset.group(1)) * 1000 if offset else None}
    return {"ntp_synchronized": None, "estimated_offset_ms": None}

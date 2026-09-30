from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
from typing import Any

from .windows_tcp import sample_windows_tcp_receiver_stats


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
        self.previous_linux_flow_retransmits: int | None = None
        self.previous_windows_receiver_stats: dict[str, tuple[int, int]] = {}

    def sample(self, owner_pids: set[int] | None = None) -> dict[str, Any]:
        now = time.monotonic()
        if not self.enabled or not self.host:
            return {"available": False, "reason": "disabled or no server_host configured"}
        result: dict[str, Any] = {
            "available": True, "rtt_ms": None, "packet_loss_percent": None,
            "icmp_probe_count": None, "icmp_reply_count": None, "icmp_status": "UNAVAILABLE",
            "tcp_retransmissions": None, "tcp_state": None, "provider": platform.system().lower(),
        }
        result.update(self._ping())
        system = platform.system().lower()
        if system == "linux":
            result.update(self._linux_socket_stats())
        elif system == "windows":
            result.update(self._windows_tcp_stats(now, owner_pids))
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
                    "packet_loss_percent": (sent - received) / sent * 100 if received else None,
                    "icmp_probe_count": sent,
                    "icmp_reply_count": received,
                    "icmp_status": "NO_REPLY" if received == 0 else "PARTIAL" if received < sent else "OK",
                }
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return {
                    "rtt_ms": None, "packet_loss_percent": None, "icmp_probe_count": None,
                    "icmp_reply_count": None, "icmp_status": "UNAVAILABLE",
                }

        args = ["ping", "-n", "-c", "3", "-W", "1", self.host]
        command_env = os.environ.copy()
        command_env["LC_ALL"] = "C"
        command_env["LANG"] = "C"
        output = _run(args, timeout=5, env=command_env)
        rtt = re.search(r"(?:time[=<]|Average\s*=\s*)(\d+(?:\.\d+)?)\s*ms", output, re.I)
        linux_stats = re.search(r"(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets )?received", output, re.I)
        sent = int(linux_stats.group(1)) if linux_stats else None
        received = int(linux_stats.group(2)) if linux_stats else None
        loss = (sent - received) / sent * 100 if sent and received else None
        return {
            "rtt_ms": float(rtt.group(1)) if rtt else None,
            "packet_loss_percent": loss,
            "icmp_probe_count": sent,
            "icmp_reply_count": received,
            "icmp_status": "UNAVAILABLE" if sent is None or received is None else "NO_REPLY" if received == 0 else "PARTIAL" if received < sent else "OK",
        }

    def _linux_socket_stats(self) -> dict[str, Any]:
        output = _run(["ss", "-tin", "dst", self.host, "dport", "=", f":{self.port}"], timeout=2)
        if not output:
            self.previous_linux_flow_retransmits = None
            return {
                "tcp_retransmissions": None,
                "tcp_retransmissions_total": None,
                "tcp_state": "unknown",
                "provider_note": "ss unavailable or no matching flow",
            }
        rtt = re.search(r"\brtt:([\d.]+)/", output)
        state = "ESTABLISHED" if "ESTAB" in output else "not-established"
        retrans_totals = [int(total) for _, total in re.findall(r"\bretrans:(\d+)/(\d+)", output)]
        # ss prints retrans:<currently-unacked>/<total-for-the-entire-connection>.
        # Diagnose with the interval delta so an old retransmit cannot make a
        # later client-side decoder fault look like a current network problem.
        if retrans_totals:
            current_total = sum(retrans_totals)
        elif state == "ESTABLISHED":
            current_total = 0
        else:
            current_total = None
        retrans_delta = None
        if current_total is not None:
            previous_total = self.previous_linux_flow_retransmits
            if previous_total is not None and current_total >= previous_total:
                retrans_delta = current_total - previous_total
            self.previous_linux_flow_retransmits = current_total
        else:
            self.previous_linux_flow_retransmits = None
        return {
            "rtt_ms": float(rtt.group(1)) if rtt else None,
            "tcp_retransmissions": retrans_delta,
            "tcp_retransmissions_total": current_total,
            "tcp_state": state,
            "provider_note": "per-flow TCP_INFO via ss; retransmissions are interval deltas",
        }

    def _windows_tcp_stats(self, now: float, owner_pids: set[int] | None = None) -> dict[str, Any]:
        script = "$s=Get-NetTCPStatistics -ErrorAction Stop; [pscustomobject]@{retrans=[int64]$s.SegmentsRetransmitted}|ConvertTo-Json -Compress"
        output = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=3)
        retrans = None
        match = re.search(r'"?retrans"?\s*:\s*(\d+)', output)
        if match:
            current = int(match.group(1))
            if self.previous_system_retransmits is not None:
                retrans = max(0, current - self.previous_system_retransmits)
            self.previous_system_retransmits = current
        else:
            # Do not let the next successful sample turn a long monitoring gap
            # into apparent retransmits during a newer media incident.
            self.previous_system_retransmits = None
        conn_script = "$hostAddress=$env:RTMP_MONITOR_REMOTE_ADDRESS; $remotePort=[int]$env:RTMP_MONITOR_REMOTE_PORT; Get-NetTCPConnection -RemoteAddress $hostAddress -RemotePort $remotePort -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty State"
        command_env = os.environ.copy()
        command_env["RTMP_MONITOR_REMOTE_ADDRESS"] = self.host or ""
        command_env["RTMP_MONITOR_REMOTE_PORT"] = str(self.port)
        state = _run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", conn_script],
            timeout=3,
            env=command_env,
        ).strip() or "not-established"
        receiver = sample_windows_tcp_receiver_stats(self.host or "", self.port, owner_pids)
        current_flows = {
            flow["key"]: (
                int(flow["duplicate_ack_episodes_total"]),
                int(flow["duplicate_acks_total"]),
            )
            for flow in receiver["flows"]
        }
        episodes_delta = None
        duplicate_acks_delta = None
        if receiver["status"] == "AVAILABLE":
            previous = self.previous_windows_receiver_stats
            if current_flows.keys() == previous.keys():
                episodes_delta = sum(
                    (values[0] - previous[key][0]) & 0xFFFFFFFF
                    for key, values in current_flows.items()
                )
                duplicate_acks_delta = sum(
                    (values[1] - previous[key][1]) & 0xFFFFFFFF
                    for key, values in current_flows.items()
                )
            self.previous_windows_receiver_stats = current_flows
        else:
            self.previous_windows_receiver_stats = {}
        return {
            "tcp_retransmissions": retrans,
            "tcp_state": state,
            "tcp_duplicate_ack_episodes": episodes_delta,
            "tcp_duplicate_acks": duplicate_acks_delta,
            "tcp_receiver_stats_status": receiver["status"],
            "tcp_receiver_stats_flow_count": len(current_flows),
            "provider_note": (
                "Windows host-wide retransmit delta plus probe-process-owned IPv4 receiver EStats; "
                "duplicate ACK episodes indicate missing or reordered segments, not a loss percentage"
            ),
        }


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

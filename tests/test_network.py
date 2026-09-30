import socket
import struct

import pytest

from rtmp_monitor import network, windows_tcp
from rtmp_monitor.network import NetworkTelemetry


def test_windows_tcp_query_keeps_host_data_out_of_powershell_source(monkeypatch):
    malicious_host = "server'; throw 'injected'; #"
    telemetry = NetworkTelemetry(malicious_host, 1935)
    calls = []

    def fake_run(args, timeout=2.0, env=None):
        calls.append((args, timeout, env))
        if "Get-NetTCPStatistics" in args[-1]:
            return '{"retrans":12}'
        return "Established"

    monkeypatch.setattr(network, "_run", fake_run)

    result = telemetry._windows_tcp_stats(1.0)

    command, timeout, environment = calls[1]
    script = command[-1]
    assert malicious_host not in script
    assert "$env:RTMP_MONITOR_REMOTE_ADDRESS" in script
    assert environment["RTMP_MONITOR_REMOTE_ADDRESS"] == malicious_host
    assert environment["RTMP_MONITOR_REMOTE_PORT"] == "1935"
    assert timeout == 3
    assert result["tcp_state"] == "Established"


def test_windows_retransmit_delta_is_unknown_after_a_missed_sample(monkeypatch):
    counters = iter(['{"retrans":100}', '{"retrans":103}', "", '{"retrans":110}', '{"retrans":111}'])

    def fake_run(args, timeout=2.0, env=None):
        if "Get-NetTCPStatistics" in args[-1]:
            return next(counters)
        return "Established"

    monkeypatch.setattr(network, "_run", fake_run)
    telemetry = NetworkTelemetry("10.0.0.1", 1935)

    first, next_sample, missed, after_gap, recovered = [
        telemetry._windows_tcp_stats(index) for index in range(5)
    ]

    assert first["tcp_retransmissions"] is None
    assert next_sample["tcp_retransmissions"] == 3
    assert missed["tcp_retransmissions"] is None
    assert after_gap["tcp_retransmissions"] is None
    assert recovered["tcp_retransmissions"] == 1


def test_windows_receiver_duplicate_ack_metrics_are_per_flow_interval_deltas(monkeypatch):
    samples = iter([
        {"status": "AVAILABLE", "flows": [{"key": "123:local:1>remote:1935", "duplicate_ack_episodes_total": 2, "duplicate_acks_total": 6}]},
        {"status": "AVAILABLE", "flows": [{"key": "123:local:1>remote:1935", "duplicate_ack_episodes_total": 4, "duplicate_acks_total": 11}]},
        {"status": "NO_MATCHING_FLOW", "flows": []},
        {"status": "AVAILABLE", "flows": [{"key": "123:local:2>remote:1935", "duplicate_ack_episodes_total": 8, "duplicate_acks_total": 30}]},
    ])
    calls = []

    def fake_receiver(host, port, owner_pids):
        calls.append((host, port, owner_pids))
        return next(samples)

    monkeypatch.setattr(network, "sample_windows_tcp_receiver_stats", fake_receiver)
    monkeypatch.setattr(network, "_run", lambda args, **_kwargs: '{"retrans":0}' if "Get-NetTCPStatistics" in args[-1] else "Established")
    telemetry = NetworkTelemetry("198.51.100.5", 1935)

    first = telemetry._windows_tcp_stats(1.0, {123})
    second = telemetry._windows_tcp_stats(2.0, {123})
    no_flow = telemetry._windows_tcp_stats(3.0, {123})
    reconnected = telemetry._windows_tcp_stats(4.0, {123})

    assert first["tcp_duplicate_ack_episodes"] is None
    assert second["tcp_duplicate_ack_episodes"] == 2
    assert second["tcp_duplicate_acks"] == 5
    assert no_flow["tcp_receiver_stats_status"] == "NO_MATCHING_FLOW"
    assert no_flow["tcp_duplicate_ack_episodes"] is None
    assert reconnected["tcp_duplicate_ack_episodes"] is None
    assert calls == [("198.51.100.5", 1935, {123})] * 4


def test_windows_receiver_stats_enable_collection_and_read_documented_fields():
    class FakeApi:
        def __init__(self):
            self.reads = 0
            self.enables = 0

        def GetPerTcpConnectionEStats(self, _row, _kind, rw_pointer, *_args):
            self.reads += 1
            rw = windows_tcp.ctypes.cast(
                rw_pointer, windows_tcp.ctypes.POINTER(windows_tcp._TcpEstatsRecRwV0),
            ).contents
            rod = windows_tcp.ctypes.cast(
                _args[-3], windows_tcp.ctypes.POINTER(windows_tcp._TcpEstatsRecRodV0),
            ).contents
            rw.EnableCollection = 0 if self.reads == 1 else 1
            rod.DupAckEpisodes = 2 if self.reads == 1 else 7
            rod.DupAcksOut = 5 if self.reads == 1 else 19
            return 0

        def SetPerTcpConnectionEStats(self, _row, _kind, rw_pointer, *_args):
            self.enables += 1
            rw = windows_tcp.ctypes.cast(
                rw_pointer, windows_tcp.ctypes.POINTER(windows_tcp._TcpEstatsRecRwV0),
            ).contents
            assert rw.EnableCollection == 1
            return 0

    api = FakeApi()
    row = windows_tcp._MibTcpRow(5, 0, 0, 0, 0)

    status, counters = windows_tcp._read_receiver_stats(api, row)

    assert status == 0
    assert counters.DupAckEpisodes == 7
    assert counters.DupAcksOut == 19
    assert api.reads == 2
    assert api.enables == 1


def test_windows_receiver_stats_keeps_permission_failure_unknown():
    class FakeApi:
        def GetPerTcpConnectionEStats(self, _row, _kind, rw_pointer, *_args):
            rw = windows_tcp.ctypes.cast(
                rw_pointer, windows_tcp.ctypes.POINTER(windows_tcp._TcpEstatsRecRwV0),
            ).contents
            rw.EnableCollection = 0
            return 0

        def SetPerTcpConnectionEStats(self, *_args):
            return 5

    status, counters = windows_tcp._read_receiver_stats(FakeApi(), windows_tcp._MibTcpRow(5, 0, 0, 0, 0))

    assert status == 5
    assert counters is None


def test_windows_receiver_sampler_filters_exact_target_and_probe_pid(monkeypatch):
    class FakeApi:
        def GetPerTcpConnectionEStats(self, _row, _kind, rw_pointer, *_args):
            rw = windows_tcp.ctypes.cast(
                rw_pointer, windows_tcp.ctypes.POINTER(windows_tcp._TcpEstatsRecRwV0),
            ).contents
            rod = windows_tcp.ctypes.cast(
                _args[-3], windows_tcp.ctypes.POINTER(windows_tcp._TcpEstatsRecRodV0),
            ).contents
            rw.EnableCollection = 1
            rod.DupAckEpisodes = 3
            rod.DupAcksOut = 8
            return 0

    def owner_row(pid, address="198.51.100.5", port=1935, local_port=40000):
        return windows_tcp._MibTcpRowOwnerPid(
            windows_tcp._MIB_TCP_STATE_ESTABLISHED,
            struct.unpack("=I", socket.inet_aton("192.0.2.10"))[0],
            socket.htons(local_port),
            struct.unpack("=I", socket.inet_aton(address))[0],
            socket.htons(port),
            pid,
        )

    rows = [owner_row(123), owner_row(999), owner_row(123, port=1936)]
    monkeypatch.setattr(windows_tcp, "_iphlpapi", FakeApi)
    monkeypatch.setattr(windows_tcp, "_tcp_rows", lambda _api: (0, rows))

    result = windows_tcp.sample_windows_tcp_receiver_stats("198.51.100.5", 1935, {123})

    assert result["status"] == "AVAILABLE"
    assert len(result["flows"]) == 1
    assert result["flows"][0]["key"].startswith("123:192.0.2.10:40000>198.51.100.5:1935")
    assert result["flows"][0]["duplicate_ack_episodes_total"] == 3
    assert result["flows"][0]["duplicate_acks_total"] == 8


def test_windows_ping_uses_structured_dotnet_output(monkeypatch):
    host = "server.example; throw 'injected'"
    telemetry = NetworkTelemetry(host, 1935)
    calls = []

    def fake_run(args, timeout=2.0, env=None):
        calls.append((args, timeout, env))
        return '{"sent":3,"received":2,"rtt_ms":12.5}'

    monkeypatch.setattr(network.platform, "system", lambda: "Windows")
    monkeypatch.setattr(network, "_run", fake_run)

    result = telemetry._ping()

    command, timeout, environment = calls[0]
    assert command[0] == "powershell.exe"
    assert "System.Net.NetworkInformation.Ping" in command[-1]
    assert host not in command[-1]
    assert environment["RTMP_MONITOR_PING_ADDRESS"] == host
    assert timeout == 5
    assert result["rtt_ms"] == 12.5
    assert result["packet_loss_percent"] == pytest.approx(100 / 3)
    assert result["icmp_probe_count"] == 3
    assert result["icmp_reply_count"] == 2
    assert result["icmp_status"] == "PARTIAL"


def test_windows_ping_reports_loss_when_echo_is_unanswered(monkeypatch):
    telemetry = NetworkTelemetry("blocked.example", 1935)
    monkeypatch.setattr(network.platform, "system", lambda: "Windows")
    monkeypatch.setattr(network, "_run", lambda *_args, **_kwargs: '{"sent":3,"received":0,"rtt_ms":null}')

    result = telemetry._ping()

    assert result == {
        "rtt_ms": None, "packet_loss_percent": None,
        "icmp_probe_count": 3, "icmp_reply_count": 0, "icmp_status": "NO_REPLY",
    }


def test_linux_ping_forces_stable_locale_and_marks_no_replies_unknown(monkeypatch):
    calls = []

    def fake_run(args, timeout=2.0, env=None):
        calls.append((args, timeout, env))
        return "3 packets transmitted, 0 received, 100% packet loss"

    monkeypatch.setattr(network.platform, "system", lambda: "Linux")
    monkeypatch.setattr(network, "_run", fake_run)
    result = NetworkTelemetry("192.0.2.1", 1935)._ping()

    assert calls[0][2]["LC_ALL"] == "C"
    assert calls[0][2]["LANG"] == "C"
    assert result["rtt_ms"] is None
    assert result["packet_loss_percent"] is None
    assert result["icmp_probe_count"] == 3
    assert result["icmp_reply_count"] == 0
    assert result["icmp_status"] == "NO_REPLY"


def test_linux_ping_reports_substantial_partial_loss(monkeypatch):
    calls = []

    def fake_run(args, timeout=2.0, env=None):
        calls.append((args, timeout, env))
        return "64 bytes from 192.0.2.1: time=11.2 ms\n3 packets transmitted, 1 received, 66% packet loss"

    monkeypatch.setattr(network.platform, "system", lambda: "Linux")
    monkeypatch.setattr(network, "_run", fake_run)
    result = NetworkTelemetry("192.0.2.1", 1935)._ping()

    assert result["rtt_ms"] == 11.2
    assert result["packet_loss_percent"] == pytest.approx(200 / 3)
    assert result["icmp_probe_count"] == 3
    assert result["icmp_reply_count"] == 1
    assert result["icmp_status"] == "PARTIAL"
    assert calls[0][2]["LC_ALL"] == "C"


def test_linux_tcp_retransmissions_are_interval_deltas(monkeypatch):
    outputs = iter([
        "ESTAB 0 0 10.0.0.2:50000 10.0.0.1:1935 cubic rtt:8/1 retrans:0/4",
        "ESTAB 0 0 10.0.0.2:50000 10.0.0.1:1935 cubic rtt:8/1 retrans:0/4",
        "ESTAB 0 0 10.0.0.2:50000 10.0.0.1:1935 cubic rtt:8/1 retrans:0/7",
        "ESTAB 0 0 10.0.0.2:51000 10.0.0.1:1935 cubic rtt:8/1 retrans:0/2",
        "ESTAB 0 0 10.0.0.2:51000 10.0.0.1:1935 cubic rtt:8/1 retrans:0/3",
    ])
    monkeypatch.setattr(network, "_run", lambda *_args, **_kwargs: next(outputs))
    telemetry = NetworkTelemetry("10.0.0.1", 1935)

    first, same, increased, reconnected, after_reconnect = [telemetry._linux_socket_stats() for _ in range(5)]

    assert first["tcp_retransmissions"] is None
    assert first["tcp_retransmissions_total"] == 4
    assert same["tcp_retransmissions"] == 0
    assert increased["tcp_retransmissions"] == 3
    assert increased["tcp_retransmissions_total"] == 7
    assert reconnected["tcp_retransmissions"] is None
    assert reconnected["tcp_retransmissions_total"] == 2
    assert after_reconnect["tcp_retransmissions"] == 1

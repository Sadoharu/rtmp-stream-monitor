import pytest

from rtmp_monitor import network
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

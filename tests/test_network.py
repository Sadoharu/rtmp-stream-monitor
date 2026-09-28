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


def test_windows_ping_reports_loss_when_echo_is_unanswered(monkeypatch):
    telemetry = NetworkTelemetry("blocked.example", 1935)
    monkeypatch.setattr(network.platform, "system", lambda: "Windows")
    monkeypatch.setattr(network, "_run", lambda *_args, **_kwargs: '{"sent":3,"received":0,"rtt_ms":null}')

    result = telemetry._ping()

    assert result == {"rtt_ms": None, "packet_loss_percent": 100.0}

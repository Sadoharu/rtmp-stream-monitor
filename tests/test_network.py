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

import pytest


def test_windows_service_cli_registers_importable_package_class():
    pytest.importorskip("win32serviceutil")

    from unittest.mock import patch

    from rtmp_monitor import windows_service_cli

    with patch.object(
        windows_service_cli.win32serviceutil,
        "HandleCommandLine",
        return_value=0,
    ) as handle_command_line:
        assert windows_service_cli.main() == 0

    handle_command_line.assert_called_once_with(
        windows_service_cli.RtmpMonitorAgentService,
        serviceClassString="rtmp_monitor.windows_service.RtmpMonitorAgentService",
    )
    assert (
        windows_service_cli.RtmpMonitorAgentService.__module__
        == "rtmp_monitor.windows_service"
    )

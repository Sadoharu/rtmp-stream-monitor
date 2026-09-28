"""Command-line entry point for managing the Windows service."""

from __future__ import annotations

import win32serviceutil

from .windows_service import RtmpMonitorAgentService


def main() -> int:
    """Install, update, start, stop, or remove the probe service."""
    return win32serviceutil.HandleCommandLine(
        RtmpMonitorAgentService,
        serviceClassString="rtmp_monitor.windows_service.RtmpMonitorAgentService",
    )


if __name__ == "__main__":
    raise SystemExit(main())

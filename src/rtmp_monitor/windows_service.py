"""Optional Windows Service wrapper (requires the windows-service extra)."""

from __future__ import annotations

import asyncio
import os
import threading

import win32event
import win32service
import win32serviceutil

from .agent import AgentRunner
from .config import load_agent_config
from .logging_setup import configure_logging


class RtmpMonitorAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "RtmpMonitorAgent"
    _svc_display_name_ = "RTMP Stream Monitor Agent"
    _svc_description_ = "Monitors RTMP streams and sends diagnostics to the central RTMP monitor."
    _svc_deps_ = ["Tcpip"]

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_requested = threading.Event()
        self.async_stop_event: asyncio.Event | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_requested.set()
        if self.loop and self.loop.is_running() and self.async_stop_event:
            self.loop.call_soon_threadsafe(self.async_stop_event.set)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        import servicemanager

        servicemanager.LogInfoMsg("RTMP Monitor Agent service starting")
        config_path = os.environ.get("RTMP_MONITOR_CONFIG", r"C:\ProgramData\RtmpMonitor\agent.yaml")
        config = load_agent_config(config_path)
        configure_logging(config.log_dir, agent_name=config.agent.name)
        asyncio.run(self._run_agent(config))
        servicemanager.LogInfoMsg("RTMP Monitor Agent service stopped")

    async def _run_agent(self, config):
        self.loop = asyncio.get_running_loop()
        self.async_stop_event = asyncio.Event()
        runner = AgentRunner(config)
        task = asyncio.create_task(runner.run())
        stop_waiter = asyncio.create_task(self.async_stop_event.wait())
        done, _ = await asyncio.wait({task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if stop_waiter in done and not task.done():
            task.cancel()
        if task in done and not stop_waiter.done():
            stop_waiter.cancel()
        await asyncio.gather(task, stop_waiter, return_exceptions=True)


if __name__ == "__main__":
    # Keep the legacy module command usable without registering __main__ as the
    # service module. pythonservice must import the stable package path below.
    win32serviceutil.HandleCommandLine(
        RtmpMonitorAgentService,
        serviceClassString="rtmp_monitor.windows_service.RtmpMonitorAgentService",
    )

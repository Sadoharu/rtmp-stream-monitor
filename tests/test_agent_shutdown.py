import asyncio
import time

import pytest

from rtmp_monitor import agent as agent_module
from rtmp_monitor.agent import StreamProbe
from rtmp_monitor.config import AgentFileConfig
from rtmp_monitor.queue import LocalQueue


class FakePipe:
    def __init__(self):
        self.queue = asyncio.Queue()

    def close(self):
        self.queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeProcess:
    def __init__(self):
        self.stdout = FakePipe()
        self.stderr = FakePipe()
        self.returncode = None
        self.pid = None
        self.exited = asyncio.Event()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.close()
        self.stderr.close()
        self.exited.set()

    def kill(self):
        self.terminate()


def test_cancelling_probe_stops_process_and_awaits_child_tasks(tmp_path, monkeypatch):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://127.0.0.1:8090"},
        "agent": {"name": "test", "role": "CLIENT", "token": "unused"},
        "streams": [{"id": "demo", "url": "rtmp://example.invalid/live/demo"}],
        "network": {"enabled": False},
        "state_dir": tmp_path / "state",
        "log_dir": tmp_path / "logs",
    })
    queue = LocalQueue(tmp_path / "state" / "queue.db", 1024 * 1024, 100)
    probe = StreamProbe(config.streams[0], config, queue)
    async def exercise():
        process = FakeProcess()
        created_tasks = []
        real_create_task = asyncio.create_task

        def recording_create_task(coro, *args, **kwargs):
            task = real_create_task(coro, *args, **kwargs)
            created_tasks.append(task)
            return task

        async def fake_create_subprocess_exec(*args, **kwargs):
            return process

        monkeypatch.setattr(probe, "command", lambda: ["fake-ffmpeg"])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        monkeypatch.setattr(asyncio, "create_task", recording_create_task)

        task = asyncio.create_task(probe._run_one())
        while probe.process is None:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert process.returncode == -15
        assert len(created_tasks) == 5
        assert all(child.done() for child in created_tasks)

    asyncio.run(exercise())


def test_restart_backoff_grows_when_ffmpeg_cannot_start_after_long_uptime(tmp_path, monkeypatch):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://127.0.0.1:8090"},
        "agent": {"name": "test", "role": "CLIENT", "token": "unused"},
        "streams": [{"id": "demo", "url": "rtmp://example.invalid/live/demo"}],
        "monitoring": {"reconnect_initial": 0.5, "reconnect_max": 4.0},
        "network": {"enabled": False},
        "state_dir": tmp_path / "state",
        "log_dir": tmp_path / "logs",
    })
    probe = StreamProbe(config.streams[0], config, LocalQueue(tmp_path / "state" / "queue.db", 1024 * 1024, 100))
    probe.started_mono = time.monotonic() - 600
    probe._publish = lambda _item: None
    probe._event = lambda *_args: None
    delays = []

    async def fail_before_process_start():
        probe._last_run_duration = 0.0
        raise FileNotFoundError("ffmpeg temporarily unavailable")

    async def record_sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(probe, "_run_one", fail_before_process_start)
    monkeypatch.setattr(agent_module.asyncio, "sleep", record_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(probe.run())

    assert delays == [0.5, 1.0, 2.0, 4.0]

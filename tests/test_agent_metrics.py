import time
from types import SimpleNamespace

import psutil

from rtmp_monitor.agent import AgentRunner, StreamProbe
from rtmp_monitor.config import AgentFileConfig
from rtmp_monitor.queue import LocalQueue


def test_queued_sample_keeps_network_and_clock_metrics_from_observation_time(tmp_path):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "client-test", "token": "test-token"},
        "streams": [{"id": "poland", "url": "rtmp://server.example/live/poland"}],
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    })
    runner = AgentRunner(config)
    runner._network_snapshot = {"tcp_retransmissions": 7, "sampled_at": "2026-09-28T10:00:00+00:00"}
    runner._clock_snapshot = {"ntp_synchronized": True, "sampled_at": "2026-09-28T10:00:01+00:00"}
    runner._http_offset_ms = 12.5
    runner._http_offset_updated_mono = time.monotonic()
    runner._agent_cpu_percent = 1.5
    runner._agent_rss_bytes = 12_345
    runner._agent_metrics_sampled_at = "2026-09-28T10:00:02+00:00"

    runner.outbox.put(runner.probes[0]._snapshot(time.monotonic()))

    runner._network_snapshot = {"tcp_retransmissions": 99, "sampled_at": "2026-09-28T10:01:00+00:00"}
    runner._clock_snapshot = {"ntp_synchronized": False, "sampled_at": "2026-09-28T10:01:01+00:00"}
    runner._http_offset_ms = 350.0
    runner._agent_cpu_percent = 9.5
    runner._agent_rss_bytes = 67_890
    runner._agent_metrics_sampled_at = "2026-09-28T10:01:02+00:00"
    queued = runner.outbox.peek(1)[0][1]

    assert queued["metrics"]["network"]["tcp_retransmissions"] == 7
    assert queued["metrics"]["network"]["sampled_at"] == "2026-09-28T10:00:00+00:00"
    assert queued["metrics"]["clock"]["ntp_synchronized"] is True
    assert queued["metrics"]["clock"]["central_offset_ms"] == 12.5
    assert queued["metrics"]["agent_cpu_percent"] == 1.5
    assert queued["metrics"]["agent_rss_bytes"] == 12_345
    assert queued["metrics"]["agent_metrics_sampled_at"] == "2026-09-28T10:00:02+00:00"


def test_ffmpeg_cpu_sampling_reuses_psutil_process_handle(tmp_path, monkeypatch):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "client-test", "token": "test-token"},
        "streams": [{"id": "poland", "url": "rtmp://server.example/live/poland"}],
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    })
    queue = LocalQueue(tmp_path / "state" / "queue.db", 1024 * 1024, 100)
    probe = StreamProbe(config.streams[0], config, queue)
    probe.process = SimpleNamespace(pid=1234, returncode=None)

    class FakeProcessStats:
        def __init__(self):
            self.cpu_calls = 0

        def cpu_percent(self, interval=None):
            self.cpu_calls += 1
            return 0.0 if self.cpu_calls == 1 else 27.5

        def memory_info(self):
            return SimpleNamespace(rss=12_345_678)

    handle = FakeProcessStats()
    created = []

    def process_factory(pid):
        created.append(pid)
        return handle

    monkeypatch.setattr(psutil, "Process", process_factory)
    probe._attach_process_stats()
    probe._update_process_usage()

    assert created == [1234]
    assert probe.process_cpu == 27.5
    assert probe.process_rss == 12_345_678

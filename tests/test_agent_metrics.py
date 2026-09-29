import asyncio
import sqlite3
import time
from types import SimpleNamespace

import psutil

from rtmp_monitor import agent as agent_module
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
    runner._network_snapshot_mono = time.monotonic() - 4.0
    runner._clock_snapshot = {"ntp_synchronized": True, "sampled_at": "2026-09-28T10:00:01+00:00"}
    runner._http_offset_ms = 12.5
    runner._http_offset_uncertainty_ms = 1012.5
    runner._http_offset_source = "http_date"
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
    assert queued["metrics"]["network"]["sample_age_seconds"] >= 3.9
    assert queued["metrics"]["network"]["sample_interval_seconds"] == 10.0
    assert queued["metrics"]["clock"]["ntp_synchronized"] is True
    assert queued["metrics"]["clock"]["central_offset_ms"] == 12.5
    assert queued["metrics"]["clock"]["central_offset_uncertainty_ms"] == 1012.5
    assert queued["metrics"]["clock"]["central_offset_source"] == "http_date"
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


def test_stream_probe_uses_configured_executables_when_service_path_is_missing(tmp_path, monkeypatch):
    ffmpeg = tmp_path / "bin" / "ffmpeg.exe"
    ffprobe = tmp_path / "bin" / "ffprobe.exe"
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "client-test", "token": "test-token", "profile": "LIGHT"},
        "streams": [{"id": "poland", "url": "rtmp://server.example/live/poland"}],
        "ffmpeg_path": str(ffmpeg),
        "ffprobe_path": str(ffprobe),
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    })
    probe = StreamProbe(config.streams[0], config, LocalQueue(tmp_path / "queue.db", 1024 * 1024, 100))
    monkeypatch.setattr(agent_module.shutil, "which", lambda _name: None)

    assert probe.command()[0] == str(ffprobe)


def test_ffmpeg_progress_rate_is_not_reported_as_source_fps(tmp_path):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "client-test", "token": "test-token"},
        "streams": [{"id": "poland", "url": "rtmp://server.example/live/poland"}],
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    })
    probe = StreamProbe(config.streams[0], config, LocalQueue(tmp_path / "queue.db", 1024 * 1024, 100))
    probe.stream_metadata["source_fps"] = 50.0

    class ProgressLines:
        async def __aiter__(self):
            for line in (b"frame=100\n", b"fps=79.48\n", b"progress=continue\n"):
                yield line

    probe.process = SimpleNamespace(stdout=ProgressLines())
    asyncio.run(probe._read_stdout())

    assert probe.stream_metadata["source_fps"] == 50.0
    assert probe.stream_metadata["decode_fps"] == 79.48
    assert "fps" not in probe.stream_metadata


def test_connection_reset_clears_stale_timestamp_baselines(tmp_path):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "client-test", "token": "test-token", "profile": "LIGHT"},
        "streams": [{"id": "poland", "url": "rtmp://server.example/live/poland"}],
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    })
    probe = StreamProbe(config.streams[0], config, LocalQueue(tmp_path / "queue.db", 1024 * 1024, 100))
    probe._handle_packet_line("stream_index=0|pts_time=100.000|dts_time=99.960|flags=K|size=100")
    probe._handle_packet_line("stream_index=0|pts_time=100.040|dts_time=100.000|flags=__|size=100")

    probe._reset_connection_state()
    probe._handle_packet_line("stream_index=0|pts_time=0.000|dts_time=0.000|flags=K|size=100")

    assert not [event for event in probe.pending_events if event["code"] in {"PTS_JUMP", "DTS_REGRESSION"}]
    assert probe.stream_metadata["last_media_dts"] == 0.0
    assert probe.analyzer.keyframe_count == 2
    assert probe.analyzer.frame_count == 3


def test_sender_retries_outbox_read_errors_without_exiting(tmp_path):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "client-test", "token": "test-token"},
        "streams": [{"id": "poland", "url": "rtmp://server.example/live/poland"}],
        "monitoring": {"heartbeat_interval": 1},
        "network": {"enabled": False},
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    })
    runner = AgentRunner(config)
    runner._last_network_mono = time.monotonic()
    calls = 0
    second_read = asyncio.Event()

    def flaky_peek(_limit):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        if calls == 2:
            second_read.set()
        return []

    runner.outbox.peek = flaky_peek

    async def exercise():
        sender = asyncio.create_task(runner._sender_loop())
        try:
            await asyncio.wait_for(second_read.wait(), timeout=3)
        finally:
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass

    asyncio.run(exercise())
    assert calls >= 2


def test_post_uses_central_receive_timestamp_with_request_uncertainty(tmp_path, monkeypatch):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "client-test", "token": "test-token"},
        "streams": [{"id": "poland", "url": "rtmp://server.example/live/poland"}],
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    })
    runner = AgentRunner(config)

    class FakeResponse:
        headers = {"Date": "Thu, 01 Jan 1970 00:01:41 GMT"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"accepted":1,"received_at":"1970-01-01T00:01:41.500000+00:00"}'

    moments = iter((100.0, 102.0))
    monkeypatch.setattr(agent_module.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr(agent_module.time, "time", lambda: next(moments))

    response = runner._post("http://central.example:8090/api/v1/ingest", {"items": []})

    assert response["central_offset_ms"] == 500.0
    assert response["central_offset_uncertainty_ms"] == 1001.0
    assert response["central_offset_source"] == "central_receive_timestamp"


def test_post_falls_back_to_second_precision_http_date(tmp_path, monkeypatch):
    config = AgentFileConfig.model_validate({
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "client-test", "token": "test-token"},
        "streams": [{"id": "poland", "url": "rtmp://server.example/live/poland"}],
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    })
    runner = AgentRunner(config)

    class FakeResponse:
        headers = {"Date": "Thu, 01 Jan 1970 00:01:41 GMT"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"accepted":1}'

    moments = iter((100.0, 102.0))
    monkeypatch.setattr(agent_module.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr(agent_module.time, "time", lambda: next(moments))

    response = runner._post("http://central.example:8090/api/v1/ingest", {"items": []})

    assert response["central_offset_ms"] == 0.0
    assert response["central_offset_uncertainty_ms"] == 2001.0
    assert response["central_offset_source"] == "http_date"

import json
import urllib.parse
from types import SimpleNamespace

import pytest

from rtmp_monitor.config import AgentFileConfig, SrsApiConfig
from rtmp_monitor.srs_ingress import SRS_PAGE_SIZE, SrsApiClient, SrsIngressProbe


def ingress_config(tmp_path, **overrides):
    data = {
        "server": {"url": "http://central.example:8090"},
        "agent": {"name": "server-ingress", "token": "test-token", "role": "SERVER_INGRESS"},
        "streams": [{"id": "poland", "url": "rtmp://127.0.0.1:1935/live/poland"}],
        "srs_api": {"base_url": "http://127.0.0.1:1985"},
        "monitoring": {"heartbeat_interval": 2, "stall_threshold": 5},
        "network": {"enabled": False},
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
    }
    data.update(overrides)
    return AgentFileConfig.model_validate(data)


def stream_row(*, active=True, recv_bytes=1000, frames=50):
    return {
        "id": "vid-1",
        "name": "poland",
        "app": "live",
        "vhost": "__defaultVhost__",
        "recv_bytes": recv_bytes,
        "frames": frames,
        "video_frames": frames,
        "audio_frames": 80,
        "clients": 1,
        "kbps": {"recv_30s": 5000, "send_30s": 2000},
        "publish": {"active": active, "cid": "publisher-1"},
        "video": {"codec": "H264", "width": 1920, "height": 1080},
        "audio": {"codec": "AAC"},
    }


def test_srs_api_paginates_and_prefers_active_publisher(tmp_path):
    config = ingress_config(tmp_path)
    client = SrsApiClient(config.srs_api, config.streams[0])
    calls = []

    def get_json(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        start = int(query["start"][0])
        calls.append(start)
        if start == 0:
            return {"code": 0, "server": "srs-1", "total": SRS_PAGE_SIZE + 1, "streams": [{}] * SRS_PAGE_SIZE}
        return {
            "code": 0,
            "server": "srs-1",
            "total": SRS_PAGE_SIZE + 1,
            "streams": [stream_row(active=False), stream_row(active=True)],
        }

    client._get_json = get_json
    row, server_id = client.find_stream()

    assert calls == [0, SRS_PAGE_SIZE]
    assert row["publish"]["active"] is True
    assert server_id == "srs-1"


def test_srs_http_api_basic_auth_is_sent(monkeypatch, tmp_path):
    config = ingress_config(tmp_path, srs_api={
        "base_url": "http://127.0.0.1:1985",
        "username": "probe",
        "password": "secret",
    })
    client = SrsApiClient(config.srs_api, config.streams[0])
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"code": 0, "streams": []}).encode()

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("rtmp_monitor.srs_ingress.urllib.request.urlopen", fake_urlopen)
    client._get_json("http://127.0.0.1:1985/api/v1/streams/")

    assert captured["authorization"] == "Basic cHJvYmU6c2VjcmV0"
    assert captured["timeout"] == 4


def test_srs_probe_detects_ingress_stall_and_recovery(tmp_path):
    config = ingress_config(tmp_path)
    probe = SrsIngressProbe(config.streams[0], config, SimpleNamespace(put=lambda _item: None))

    healthy = probe._sample(stream_row(), "srs-1", 10.0)
    stalled = probe._sample(stream_row(), "srs-1", 16.0)
    recovered = probe._sample(stream_row(recv_bytes=2000, frames=100), "srs-1", 17.0)

    assert healthy["status"] == "OK"
    assert healthy["metrics"]["ingress_quality"] == "PUBLISHER_COUNTERS_ONLY"
    assert healthy["metrics"]["ingress_media_decode_validated"] is False
    assert healthy["metrics"]["last_ingress_progress_age"] == 0
    assert stalled["status"] == "STREAM_STALLED"
    assert [event["code"] for event in stalled["events"]] == ["STREAM_STALL"]
    assert recovered["status"] == "OK"
    assert [event["code"] for event in recovered["events"]] == ["INGRESS_RECOVERED"]


def test_srs_probe_reports_missing_publisher_and_api_outage_differently(tmp_path):
    config = ingress_config(tmp_path)
    probe = SrsIngressProbe(config.streams[0], config, SimpleNamespace(put=lambda _item: None))

    offline = probe._sample(stream_row(active=False), "srs-1", 10.0)
    unavailable = probe._api_unavailable(RuntimeError("connection refused"))

    assert offline["status"] == "STREAM_OFFLINE"
    assert [event["code"] for event in offline["events"]] == ["STREAM_OFFLINE"]
    assert unavailable["status"] == "WARNING"
    assert unavailable["metrics"]["srs_api_available"] is False
    assert [event["code"] for event in unavailable["events"]] == ["SRS_API_UNAVAILABLE"]


def test_server_ingress_requires_srs_api_config(tmp_path):
    with pytest.raises(ValueError, match="SERVER_INGRESS requires srs_api"):
        ingress_config(tmp_path, srs_api=None)


def test_srs_api_config_validates_default_loopback_url():
    assert str(SrsApiConfig().base_url) == "http://127.0.0.1:1985/"

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from rtmp_monitor.api import create_app
from rtmp_monitor.config import CentralFileConfig


def test_authenticated_ingest_is_idempotent_and_correlated(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
        agent_offline_seconds=20,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    with TestClient(app) as client:
        assert client.get("/api/v1/streams").status_code == 401
        assert "STREAM MONITOR" in client.get("/").text
        headers = {"Authorization": f"Bearer {admin}"}
        created_stream = client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo", "local_url": "rtmp://127.0.0.1/live/demo", "public_url": "rtmp://stream.example.net/live/demo"})
        assert created_stream.status_code == 201
        ingress = client.post("/api/v1/agents", headers=headers, json={"name": "srs-ingress", "location": "server", "platform": "Ubuntu", "role": "SERVER_INGRESS", "stream_id": "demo"})
        assert ingress.status_code == 201
        assert ingress.json()["role"] == "SERVER_INGRESS"
        created_agent = client.post("/api/v1/agents", headers=headers, json={"name": "client-win-demo", "location": "studio", "platform": "Windows", "role": "CLIENT", "stream_id": "demo"})
        assert created_agent.status_code == 201
        token = created_agent.json()["token"]
        sample = {
            "sample_id": "sample-stable-id",
            "stream_id": "demo",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": "CRITICAL",
            "metrics": {"network": {"tcp_retransmissions": 4}},
            "events": [{"code": "FREEZE_START", "severity": "CRITICAL", "details": {}}],
            "context": {},
        }
        agent_headers = {"Authorization": f"Bearer {token}"}
        response = client.post("/api/v1/ingest", headers=agent_headers, json={"items": [sample]})
        assert response.status_code == 200
        assert response.json()["accepted"] == 1
        duplicate = client.post("/api/v1/ingest", headers=agent_headers, json={"items": [sample]})
        assert duplicate.status_code == 200
        assert duplicate.json()["accepted"] == 0
        incidents = client.get("/api/v1/incidents", headers=headers).json()
        assert len(incidents) == 1
        assert incidents[0]["diagnosis"] == "NETWORK_PATH_PROBLEM"
        telemetry = client.get("/api/v1/telemetry?stream_id=demo", headers=headers).json()
        assert len(telemetry) == 1


def test_batch_correlates_transient_freeze_before_recovery(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        assert client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"}).status_code == 201
        created_agent = client.post("/api/v1/agents", headers=headers, json={
            "name": "client-win-demo", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "demo",
        })
        token = created_agent.json()["token"]
        samples = [
            {
                "sample_id": "freeze-start",
                "stream_id": "demo",
                "observed_at": (now - timedelta(seconds=2)).isoformat(),
                "status": "WARNING",
                "metrics": {},
                "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {"value": 12.0}}],
                "context": {},
            },
            {
                "sample_id": "freeze-end",
                "stream_id": "demo",
                "observed_at": (now - timedelta(seconds=1)).isoformat(),
                "status": "OK",
                "metrics": {},
                "events": [{"code": "FREEZE_END", "severity": "INFO", "details": {"value": 14.1}}],
                "context": {},
            },
        ]
        response = client.post("/api/v1/ingest", headers={"Authorization": f"Bearer {token}"}, json={"items": samples})
        assert response.status_code == 200
        incidents = client.get("/api/v1/incidents", headers=headers).json()
        assert len(incidents) == 1
        assert incidents[0]["diagnosis"] == "CLIENT_PATH_UNCONFIRMED"
        assert "SERVER_EGRESS IS NOT OBSERVED" in incidents[0]["probable_location"]
        assert incidents[0]["active"] is False
        assert incidents[0]["symptoms"][0]["events"][0]["code"] == "FREEZE_START"


def test_resolved_incident_context_keeps_gathering_post_event_samples(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    started_at = datetime.now(timezone.utc) - timedelta(seconds=75)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        created_agent = client.post("/api/v1/agents", headers=headers, json={
            "name": "client-win-demo", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "demo",
        })
        token = created_agent.json()["token"]
        samples = [
            {
                "sample_id": "pre-event-context",
                "stream_id": "demo",
                "observed_at": (started_at - timedelta(seconds=55)).isoformat(),
                "status": "OK",
                "metrics": {"last_frame_age": 0.1},
                "events": [],
                "context": {},
            },
            {
                "sample_id": "before-freeze",
                "stream_id": "demo",
                "observed_at": (started_at - timedelta(seconds=5)).isoformat(),
                "status": "OK",
                "metrics": {"last_frame_age": 0.1},
                "events": [],
                "context": {},
            },
            {
                "sample_id": "freeze-start",
                "stream_id": "demo",
                "observed_at": started_at.isoformat(),
                "status": "WARNING",
                "metrics": {"last_frame_age": 2.1},
                "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {"value": 2.1}}],
                "context": {},
            },
            {
                "sample_id": "freeze-end",
                "stream_id": "demo",
                "observed_at": (started_at + timedelta(seconds=5)).isoformat(),
                "status": "OK",
                "metrics": {"last_frame_age": 0.1},
                "events": [{"code": "FREEZE_END", "severity": "INFO", "details": {"value": 7.1}}],
                "context": {},
            },
            {
                "sample_id": "post-event-context",
                "stream_id": "demo",
                "observed_at": (started_at + timedelta(seconds=55)).isoformat(),
                "status": "OK",
                "metrics": {"last_frame_age": 0.1},
                "events": [],
                "context": {},
            },
        ]

        response = client.post("/api/v1/ingest", headers={"Authorization": f"Bearer {token}"}, json={"items": samples})
        assert response.status_code == 200
        incidents = client.get("/api/v1/incidents", headers=headers).json()
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident["active"] is False
        assert incident["context"]["window_start"] == (started_at - timedelta(seconds=60)).isoformat()
        assert incident["context"]["window_end"] == (started_at + timedelta(seconds=55)).isoformat()
        timeline = incident["context"]["timeline"]
        assert [item["timestamp"] for item in timeline] == [sample["observed_at"] for sample in samples]


def test_recent_delivery_of_old_queue_sample_is_marked_stale(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
        agent_offline_seconds=20,
        stream_offline_seconds=15,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        created_agent = client.post("/api/v1/agents", headers=headers, json={
            "name": "client-win-demo", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "demo",
        })
        token = created_agent.json()["token"]
        response = client.post("/api/v1/ingest", headers={"Authorization": f"Bearer {token}"}, json={"items": [{
            "sample_id": "old-queued-sample",
            "stream_id": "demo",
            "observed_at": observed_at.isoformat(),
            "status": "OK",
            "metrics": {"last_frame_age": 0.05, "ffmpeg_running": True},
            "events": [],
            "context": {},
        }]})
        assert response.status_code == 200
        dashboard = client.get("/api/v1/dashboard", headers=headers).json()
        agent = next(item for item in dashboard["agents"] if item["name"] == "client-win-demo")
        assert agent["status"] == "TELEMETRY_STALE"
        assert agent["last_seen_age_seconds"] < 5
        assert agent["telemetry_age_seconds"] >= 59

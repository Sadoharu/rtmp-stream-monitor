from datetime import datetime, timezone

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
        unsupported_ingress = client.post("/api/v1/agents", headers=headers, json={"name": "false-ingress", "location": "server", "platform": "Ubuntu", "role": "SERVER_INGRESS", "stream_id": "demo"})
        assert unsupported_ingress.status_code == 422
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

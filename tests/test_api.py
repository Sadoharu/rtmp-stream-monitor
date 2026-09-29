from datetime import datetime, timedelta, timezone
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from rtmp_monitor.api import create_app
from rtmp_monitor.config import CentralFileConfig
from rtmp_monitor.correlation import run_retention
from rtmp_monitor.db import Agent, Incident, ProbeEnrollment, Stream, Telemetry


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
        dashboard = client.get("/")
        assert '<div id="root"></div>' in dashboard.text
        javascript = re.search(r'<script[^>]+src="([^"]+\.js)"', dashboard.text)
        stylesheet = re.search(r'<link[^>]+href="([^"]+\.css)"', dashboard.text)
        assert javascript and stylesheet
        assert client.get(javascript.group(1)).status_code == 200
        assert client.get(stylesheet.group(1)).status_code == 200
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


def test_incident_explanation_falls_back_to_evidence_without_openai_and_requires_admin(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    now = datetime.now(timezone.utc)
    incident_id = "00000000-0000-0000-0000-000000000001"
    with app.state.sessions() as session:
        session.add(Stream(id="demo", name="demo"))
        session.flush()
        session.add(Incident(
            id=incident_id, stream_id="demo", opened_at=now, updated_at=now, resolved_at=now,
            severity="WARNING", diagnosis="CLIENT_PROBLEM", probable_location="CLIENT RECEIVE / DECODER",
            affected_agents=["predator-private-name"], symptoms=[], active=False, fingerprint="test:client",
            context={"timeline": [{
                "timestamp": now.isoformat(), "agent": "predator-private-name", "role": "CLIENT",
                "status": "WARNING", "metrics": {},
                "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}],
            }]},
        ))
        session.commit()

    headers = {"Authorization": f"Bearer {admin_file.read_text(encoding='utf-8').strip()}"}
    with TestClient(app) as client:
        path = f"/api/v1/incidents/{incident_id}/explanation"
        assert client.post(path).status_code == 401
        response = client.post(path, headers=headers)
        assert response.status_code == 200
        explanation = response.json()
        assert explanation["ai_status"] == "not_configured"
        assert explanation["confidence"] == "low"
        assert "не знайдено достатньо" in explanation["likely_cause"]
        assert "predator-private-name" not in response.text


def test_ai_incident_explanation_is_cached_until_incident_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    now = datetime.now(timezone.utc)
    incident_id = "00000000-0000-0000-0000-000000000002"
    with app.state.sessions() as session:
        session.add(Stream(id="demo", name="demo"))
        session.flush()
        session.add(Incident(
            id=incident_id, stream_id="demo", opened_at=now, updated_at=now, resolved_at=now,
            severity="WARNING", diagnosis="CLIENT_PROBLEM", probable_location="CLIENT RECEIVE / DECODER",
            affected_agents=[], symptoms=[], active=False, fingerprint="test:client:ai", context={"timeline": []},
        ))
        session.commit()

    calls = []
    def fake_openai(packet, api_key, model):
        calls.append((api_key, model))
        return {"summary": "Пояснення", "cause_key": packet["causal_analysis"]["cause_key"], "likely_cause": "Причина", "confidence": "low",
                "evidence_ids": [], "evidence": [], "other_possible_causes": [], "next_checks": [],
                "ai_generated": True, "model": model}

    monkeypatch.setattr("rtmp_monitor.api.openai_explanation", fake_openai)
    headers = {"Authorization": f"Bearer {admin_file.read_text(encoding='utf-8').strip()}"}
    with TestClient(app) as client:
        path = f"/api/v1/incidents/{incident_id}/explanation"
        first = client.post(path, headers=headers)
        second = client.post(path, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["ai_status"] == "generated"
        assert second.json()["ai_status"] == "cached"
        assert calls == [("sk-test-secret", "gpt-test")]


def test_removing_probe_revokes_token_and_preserves_history(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    headers = {"Authorization": f"Bearer {admin_file.read_text(encoding='utf-8').strip()}"}
    with TestClient(app) as client:
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        created = client.post("/api/v1/agents", headers=headers, json={
            "name": "predator", "role": "CLIENT", "stream_id": "demo",
        }).json()
        sample = {"sample_id": "predator-freeze", "stream_id": "demo",
                  "observed_at": datetime.now(timezone.utc).isoformat(), "status": "WARNING",
                  "metrics": {}, "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}],
                  "context": {}}
        assert client.post("/api/v1/ingest", headers={"Authorization": f"Bearer {created['token']}"}, json={"items": [sample]}).status_code == 200
        assert client.get("/api/v1/incidents?active=true", headers=headers).json()
        removal = client.delete(f"/api/v1/agents/{created['id']}", headers=headers)
        assert removal.status_code == 200
        assert removal.json()["history_preserved"] is True
        assert client.get("/api/v1/agents", headers=headers).json() == []
        assert client.get("/api/v1/dashboard", headers=headers).json()["agents"] == []
        assert client.get("/api/v1/incidents?active=true", headers=headers).json() == []
        assert client.post("/api/v1/ingest", headers={"Authorization": f"Bearer {created['token']}"}, json={
            "items": [{"stream_id": "demo", "observed_at": datetime.now(timezone.utc).isoformat()}],
        }).status_code == 401
        again = client.post("/api/v1/agents", headers=headers, json={
            "name": "predator", "role": "CLIENT", "stream_id": "demo",
        })
        assert again.status_code == 201
        assert again.json()["id"] == created["id"]


def test_long_running_incident_stays_a_single_active_incident(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    first_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        created_agent = client.post("/api/v1/agents", headers=headers, json={
            "name": "client", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "demo",
        })
        agent_headers = {"Authorization": f"Bearer {created_agent.json()['token']}"}
        for sample_id, observed_at in (
            ("freeze-start", first_at),
            ("freeze-still-active", first_at + timedelta(minutes=3)),
        ):
            response = client.post("/api/v1/ingest", headers=agent_headers, json={"items": [{
                "sample_id": sample_id,
                "stream_id": "demo",
                "observed_at": observed_at.isoformat(),
                "status": "WARNING",
                "metrics": {},
                "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}],
                "context": {},
            }]})
            assert response.status_code == 200

        incidents = client.get("/api/v1/incidents?active=true", headers=headers).json()

    assert len(incidents) == 1
    assert incidents[0]["opened_at"] == first_at.isoformat()
    assert incidents[0]["updated_at"] == (first_at + timedelta(minutes=3)).isoformat()


def test_out_of_order_healthy_sample_does_not_resolve_newer_incident(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    opened_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        created_agent = client.post("/api/v1/agents", headers=headers, json={
            "name": "client", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "demo",
        })
        agent_headers = {"Authorization": f"Bearer {created_agent.json()['token']}"}
        broken = client.post("/api/v1/ingest", headers=agent_headers, json={"items": [{
            "sample_id": "freeze-start",
            "stream_id": "demo",
            "observed_at": opened_at.isoformat(),
            "status": "WARNING",
            "metrics": {},
            "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}],
            "context": {},
        }]})
        assert broken.status_code == 200
        stale_healthy = client.post("/api/v1/ingest", headers=agent_headers, json={"items": [{
            "sample_id": "late-healthy",
            "stream_id": "demo",
            "observed_at": (opened_at - timedelta(seconds=10)).isoformat(),
            "status": "OK",
            "metrics": {},
            "events": [],
            "context": {},
        }]})
        assert stale_healthy.status_code == 200
        incidents = client.get("/api/v1/incidents?active=true", headers=headers).json()

    assert len(incidents) == 1
    assert incidents[0]["active"] is True
    assert incidents[0]["resolved_at"] is None


def test_out_of_order_different_diagnosis_does_not_open_second_active_incident(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        egress = client.post("/api/v1/agents", headers=headers, json={
            "name": "egress", "location": "server", "platform": "Ubuntu",
            "role": "SERVER_EGRESS", "stream_id": "demo",
        })
        probe = client.post("/api/v1/agents", headers=headers, json={
            "name": "client", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "demo",
        })
        egress_headers = {"Authorization": f"Bearer {egress.json()['token']}"}
        client_headers = {"Authorization": f"Bearer {probe.json()['token']}"}
        current_at = base + timedelta(seconds=30)
        healthy_egress = client.post("/api/v1/ingest", headers=egress_headers, json={"items": [{
            "sample_id": "egress-current",
            "stream_id": "demo",
            "observed_at": current_at.isoformat(),
            "status": "OK",
            "metrics": {"last_frame_age": 0.1},
            "events": [],
            "context": {},
        }]})
        assert healthy_egress.status_code == 200
        current_client = client.post("/api/v1/ingest", headers=client_headers, json={"items": [{
            "sample_id": "client-current",
            "stream_id": "demo",
            "observed_at": current_at.isoformat(),
            "status": "CRITICAL",
            "metrics": {"network": {"provider": "windows", "tcp_retransmissions": 0, "rtt_ms": 4}},
            "events": [{"code": "FREEZE_START", "severity": "CRITICAL", "details": {}}],
            "context": {},
        }]})
        assert current_client.status_code == 200

        delayed_client = client.post("/api/v1/ingest", headers=client_headers, json={"items": [{
            "sample_id": "client-delayed-network-symptom",
            "stream_id": "demo",
            "observed_at": (base + timedelta(seconds=10)).isoformat(),
            "status": "CRITICAL",
            "metrics": {"network": {
                "provider": "linux", "tcp_retransmissions": 3,
                "sample_age_seconds": 0, "sample_interval_seconds": 10,
            }},
            "events": [{"code": "FREEZE_START", "severity": "CRITICAL", "details": {}}],
            "context": {},
        }]})
        assert delayed_client.status_code == 200
        incidents = client.get("/api/v1/incidents?active=true", headers=headers).json()

    assert len(incidents) == 1
    assert incidents[0]["diagnosis"] == "CLIENT_PROBLEM"
    assert incidents[0]["opened_at"] == current_at.isoformat()


def test_timeline_downsamples_per_probe_and_preserves_events_and_network_samples(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        created_agent = client.post("/api/v1/agents", headers=headers, json={
            "name": "client", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "demo",
        })
        agent_id = created_agent.json()["id"]
        second_agent = client.post("/api/v1/agents", headers=headers, json={
            "name": "client-2", "location": "studio-2", "platform": "Ubuntu",
            "role": "CLIENT", "stream_id": "demo",
        })
        now = datetime.now(timezone.utc)
        bucket_epoch = int(now.timestamp() // 10) * 10 - 120
        bucket_start = datetime.fromtimestamp(bucket_epoch, timezone.utc)
        samples = []
        for target_id, target_name in ((agent_id, "client"), (second_agent.json()["id"], "client-2")):
            for second in range(30):
                status = "WARNING" if target_name == "client" and second == 12 else "CRITICAL" if target_name == "client" and second == 22 else "OK"
                events = [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}] if target_name == "client" and second == 23 else []
                metrics = {"network": {"provider": "windows", "tcp_retransmissions": 2}} if target_name == "client" and second == 24 else {}
                samples.append(Telemetry(
                    id=f"timeline-sample-{target_name}-{second}", agent_id=target_id, stream_id="demo",
                    observed_at=bucket_start + timedelta(seconds=second), received_at=now,
                    status=status, metrics=metrics, events=events, context={},
                ))
        with app.state.sessions() as session:
            session.add_all(samples)
            session.commit()

        response = client.get("/api/v1/timeline?stream_id=demo&hours=6", headers=headers)

    assert response.status_code == 200
    timeline = response.json()
    assert len(timeline) == 8
    assert sum(item["agent"] == "client-2" for item in timeline) == 3
    timeline = [item for item in timeline if item["agent"] == "client"]
    seconds = {
        round((datetime.fromisoformat(item["timestamp"]) - bucket_start).total_seconds()): item
        for item in timeline
    }
    assert seconds[12]["status"] == "WARNING"
    assert seconds[22]["status"] == "CRITICAL"
    assert seconds[23]["events"][0]["code"] == "FREEZE_START"
    assert seconds[24]["metrics"]["network"]["tcp_retransmissions"] == 2


def test_v2_series_keeps_a_five_second_dip_visible_in_hourly_bucket_and_returns_events(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    now = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(int(now.timestamp() // 3600) * 3600 - 3600, timezone.utc)
    end = start + timedelta(hours=1)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        assert client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"}).status_code == 201
        created = client.post("/api/v1/agents", headers=headers, json={
            "name": "client-win", "location": "studio", "platform": "Windows", "role": "CLIENT", "stream_id": "demo",
        })
        agent_id = created.json()["id"]
        samples = []
        for second in range(3600):
            dip = 100 <= second < 105
            event = []
            if second == 102:
                event = [{"code": "FREEZE_START", "severity": "WARNING", "timestamp": (start + timedelta(seconds=second)).isoformat(), "details": {"last_frame_age_seconds": 2.2}}]
            elif second == 107:
                event = [{"code": "FREEZE_DURATION", "severity": "INFO", "timestamp": (start + timedelta(seconds=second)).isoformat(), "details": {"duration_seconds": 4.0}}]
            elif second == 110:
                event = [{"code": "FREEZE_END", "severity": "INFO", "timestamp": (start + timedelta(seconds=second)).isoformat(), "details": {}}]
            samples.append(Telemetry(
                id=f"v2-hour-{second}", agent_id=agent_id, stream_id="demo",
                observed_at=start + timedelta(seconds=second), received_at=now, status="OK",
                metrics={
                    "profile": "DEEP", "sample_interval_seconds": 1.0,
                    "received_media_bitrate_bps": 0 if dip else 4_000_000,
                    "received_media_bitrate_quality": "MEASURED", "measurement_window_seconds": 1.0,
                },
                events=event, context={},
            ))
        with app.state.sessions() as session:
            agent = session.get(Agent, agent_id)
            agent.last_seen_at = end - timedelta(seconds=1)
            session.add_all(samples)
            session.commit()

        series_response = client.get(
            "/api/v2/streams/demo/series",
            params={"from": start.isoformat(), "to": end.isoformat(), "resolution": "1h"}, headers=headers,
        )
        events_response = client.get(
            "/api/v2/streams/demo/events",
            params={"from": start.isoformat(), "to": end.isoformat(), "probe_ids": agent_id}, headers=headers,
        )

    assert series_response.status_code == 200, series_response.text
    series = series_response.json()["series"][0]
    assert series["probe"]["id"] == agent_id
    assert series["metric"] == "received_media_bitrate_bps"
    assert len(series["points"]) == 1
    point = series["points"][0]
    assert point["min_bps"] == 0
    assert point["max_bps"] == 4_000_000
    assert point["sample_count"] == 3600
    assert point["quality"] == "MEASURED"

    assert events_response.status_code == 200, events_response.text
    events = events_response.json()["events"]
    assert [event["code"] for event in events] == ["FREEZE_START", "FREEZE_DURATION", "FREEZE_END"]
    assert events[0]["confidence"] == "UNCONFIRMED"
    assert events[0]["state"] == "RESOLVED"
    assert events[0]["ended_at"] == events[2]["started_at"]
    assert events[0]["evidence"][0]["value"] == 2.2
    assert events[1]["summary"] == "Виміряно тривалість завмирання відео."
    assert "4 с" in events[1]["explanation"]
    duration_evidence = next(item for item in events[1]["evidence"] if item["metric"] == "event.details.duration_seconds")
    assert duration_evidence["value"] == 4.0 and duration_evidence["unit"] == "s"


def test_v2_series_distinguishes_no_samples_from_a_measured_zero(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    now = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(int(now.timestamp()) - 30, timezone.utc)
    end = start + timedelta(seconds=10)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        created = client.post("/api/v1/agents", headers=headers, json={
            "name": "client-empty", "location": "studio", "platform": "Ubuntu", "role": "CLIENT", "stream_id": "demo",
        })
        agent_id = created.json()["id"]
        with app.state.sessions() as session:
            session.add_all([
                Telemetry(
                    id="legacy-no-bitrate", agent_id=agent_id, stream_id="demo",
                    observed_at=start + timedelta(seconds=1), received_at=now, status="OK",
                    metrics={"profile": "LIGHT"}, events=[], context={},
                ),
                Telemetry(
                    id="measured-zero", agent_id=agent_id, stream_id="demo",
                    observed_at=start + timedelta(seconds=2), received_at=now, status="OK",
                    metrics={"profile": "LIGHT", "received_media_bitrate_bps": 0,
                             "received_media_bitrate_quality": "MEASURED", "measurement_window_seconds": 1.0},
                    events=[], context={},
                ),
            ])
            session.commit()
        response = client.get("/api/v2/streams/demo/series", params={
            "from": start.isoformat(), "to": end.isoformat(), "probe_ids": agent_id,
        }, headers=headers)
    assert response.status_code == 200, response.text
    probe_series = response.json()["series"][0]
    assert len(probe_series["points"]) == 1
    assert probe_series["points"][0]["avg_bps"] == 0
    assert probe_series["gaps"]
    assert {gap["reason"] for gap in probe_series["gaps"]} == {"MEASUREMENT_UNAVAILABLE", "NO_SAMPLE"}


def test_v2_series_reads_measured_bitrate_from_retention_aggregates(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    now = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(int((now - timedelta(days=8)).timestamp() // 60) * 60, timezone.utc)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        created = client.post("/api/v1/agents", headers=headers, json={
            "name": "client-old", "location": "studio", "platform": "Ubuntu", "role": "CLIENT", "stream_id": "demo",
        })
        agent_id = created.json()["id"]
        with app.state.sessions() as session:
            session.add(Telemetry(
                id="retained-bitrate", agent_id=agent_id, stream_id="demo", observed_at=start,
                received_at=now, status="OK", metrics={
                    "profile": "LIGHT", "received_media_bitrate_bps": 0,
                    "received_media_bitrate_quality": "MEASURED", "measurement_window_seconds": 1.0,
                    "sample_interval_seconds": 1.0,
                }, events=[], context={},
            ))
            session.commit()
            run_retention(session, 7, 180, 90, now)
            session.commit()
        response = client.get("/api/v2/streams/demo/series", params={
            "from": start.isoformat(), "to": (start + timedelta(minutes=1)).isoformat(), "resolution": "1m",
        }, headers=headers)
    assert response.status_code == 200, response.text
    point = response.json()["series"][0]["points"][0]
    assert point["min_bps"] == point["avg_bps"] == point["max_bps"] == 0
    assert point["sample_count"] == 1
    assert point["quality"] == "PARTIAL"
    assert point["last_observed_at"] == start.isoformat()


def test_v2_series_rejects_bad_ranges_and_requires_admin(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        assert client.get("/api/v2/streams/missing/series", params={"from": now.isoformat(), "to": (now + timedelta(seconds=1)).isoformat()}).status_code == 401
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        response = client.get("/api/v2/streams/missing/series", params={
            "from": (now + timedelta(seconds=1)).isoformat(), "to": now.isoformat(),
        }, headers=headers)
        invalid_range = client.get("/api/v2/streams/demo/series", params={
            "from": (now + timedelta(seconds=1)).isoformat(), "to": now.isoformat(),
        }, headers=headers)
    assert response.status_code == 404
    assert invalid_range.status_code == 422


def test_v2_events_explain_client_fault_from_evidence_and_keep_pts_cause_unconfirmed(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        server = client.post("/api/v1/agents", headers=headers, json={
            "name": "server-egress", "location": "server", "platform": "Ubuntu", "role": "SERVER_EGRESS", "stream_id": "demo",
        }).json()
        probe = client.post("/api/v1/agents", headers=headers, json={
            "name": "client-win", "location": "studio", "platform": "Windows", "role": "CLIENT", "stream_id": "demo",
        }).json()
        server_sample_at = now - timedelta(seconds=1)
        client_sample_at = now
        safe_server_metrics = {
            "profile": "DEEP", "ffmpeg_running": True, "last_frame_age": 0.1, "fps": 50,
            "clock": {"ntp_synchronized": True},
        }
        safe_client_metrics = {
            "profile": "DEEP", "ffmpeg_running": True, "last_frame_age": 0.1, "decode_errors": 1,
            "received_media_bitrate_bps": 1_000_000, "received_media_bitrate_quality": "MEASURED",
            "measurement_window_seconds": 1.0,
            "network": {"provider": "linux", "tcp_state": "ESTABLISHED", "tcp_retransmissions": 0},
            "clock": {"ntp_synchronized": True},
        }
        with app.state.sessions() as session:
            session.add_all([
                Telemetry(id="v2-explain-server", agent_id=server["id"], stream_id="demo", observed_at=server_sample_at,
                          received_at=now, status="OK", metrics=safe_server_metrics, events=[], context={}),
                Telemetry(id="v2-explain-client", agent_id=probe["id"], stream_id="demo", observed_at=client_sample_at,
                          received_at=now, status="WARNING", metrics=safe_client_metrics,
                          events=[{"code": "PTS_REGRESSION", "severity": "WARNING",
                                   "timestamp": client_sample_at.isoformat(),
                                   "details": {"previous_pts": 120.0, "pts": 118.0, "token": "must-not-leak"}}], context={}),
            ])
            session.add(Incident(
                id="v2-client-incident", stream_id="demo", opened_at=client_sample_at,
                updated_at=client_sample_at, resolved_at=client_sample_at + timedelta(seconds=1),
                severity="WARNING", diagnosis="CLIENT_PROBLEM", probable_location="CLIENT RECEIVE / DECODER",
                affected_agents=["client-win"], symptoms=[], active=False, fingerprint="v2:client:decode",
                context={"timeline": [
                    {"timestamp": server_sample_at.isoformat(), "agent": "server-egress", "role": "SERVER_EGRESS",
                     "status": "OK", "metrics": safe_server_metrics, "events": []},
                    {"timestamp": client_sample_at.isoformat(), "agent": "client-win", "role": "CLIENT",
                     "status": "WARNING", "metrics": safe_client_metrics,
                     "events": [{"code": "DECODE_ERROR", "severity": "WARNING", "details": {}}]},
                ]},
            ))
            session.commit()
        response = client.get("/api/v2/streams/demo/events", params={
            "from": (now - timedelta(seconds=3)).isoformat(), "to": (now + timedelta(seconds=5)).isoformat(),
        }, headers=headers)

    assert response.status_code == 200, response.text
    assert "must-not-leak" not in response.text
    events = response.json()["events"]
    incident = next(item for item in events if item["kind"] == "incident")
    pts_event = next(item for item in events if item["code"] == "PTS_REGRESSION")
    assert incident["summary"] == "Клієнтський probe зафіксував проблему приймання або декодування."
    assert incident["confidence"] == "LIKELY"
    assert "декодері клієнта" in incident["explanation"]
    assert incident["cause_key"] == "CLIENT_RECEIVE_OR_DECODER"
    assert incident["evidence_ids"]
    assert all(item["observed_at"] is None or item["observed_at"].endswith("+00:00") for item in incident["evidence"])
    assert pts_event["confidence"] == "UNCONFIRMED"
    assert "PTS зменшився з 120 до 118 с" in pts_event["explanation"]
    assert "не визначає, де він виник" in pts_event["explanation"]
    assert any(item["metric"] == "event.details.previous_pts" and item["value"] == 120 for item in pts_event["evidence"])


def test_v2_events_distinguish_network_evidence_from_probe_offline(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={"id": "demo", "name": "demo"})
        client.post("/api/v1/agents", headers=headers, json={
            "name": "server-egress", "location": "server", "platform": "Ubuntu", "role": "SERVER_EGRESS", "stream_id": "demo",
        })
        client.post("/api/v1/agents", headers=headers, json={
            "name": "network-client", "location": "studio", "platform": "Ubuntu", "role": "CLIENT", "stream_id": "demo",
        })
        client.post("/api/v1/agents", headers=headers, json={
            "name": "offline-client", "location": "remote", "platform": "Windows", "role": "CLIENT", "stream_id": "demo",
        })
        egress_at = now - timedelta(seconds=1)
        client_at = now
        egress_metrics = {"profile": "DEEP", "ffmpeg_running": True, "last_frame_age": 0.1,
                          "fps": 50, "clock": {"ntp_synchronized": True}}
        client_metrics = {"profile": "DEEP", "last_frame_age": 5, "clock": {"ntp_synchronized": True},
                          "network": {"provider": "linux", "tcp_state": "ESTABLISHED", "tcp_retransmissions": 3}}
        with app.state.sessions() as session:
            server = session.query(Agent).filter_by(name="server-egress").one()
            network_client = session.query(Agent).filter_by(name="network-client").one()
            session.add_all([
                Telemetry(id="v2-network-egress", agent_id=server.id, stream_id="demo", observed_at=egress_at,
                          received_at=now, status="OK", metrics=egress_metrics, events=[], context={}),
                Telemetry(id="v2-network-client", agent_id=network_client.id, stream_id="demo", observed_at=client_at,
                          received_at=now, status="WARNING", metrics=client_metrics,
                          events=[{"code": "FREEZE_START", "severity": "WARNING", "timestamp": client_at.isoformat(), "details": {}}], context={}),
            ])
            session.add_all([
                Incident(
                    id="v2-network-incident", stream_id="demo", opened_at=client_at, updated_at=client_at,
                    resolved_at=client_at + timedelta(seconds=5), severity="WARNING", diagnosis="NETWORK_PATH_PROBLEM",
                    probable_location="NETWORK BETWEEN SERVER EGRESS AND CLIENT", affected_agents=["network-client"],
                    symptoms=[], active=False, fingerprint="v2:network", context={"timeline": [
                        {"timestamp": egress_at.isoformat(), "agent": "server-egress", "role": "SERVER_EGRESS",
                         "status": "OK", "metrics": egress_metrics, "events": []},
                        {"timestamp": client_at.isoformat(), "agent": "network-client", "role": "CLIENT",
                         "status": "WARNING", "metrics": client_metrics,
                         "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}]},
                    ]},
                ),
                Incident(
                    id="v2-offline-incident", stream_id="demo", opened_at=client_at + timedelta(seconds=1),
                    updated_at=client_at + timedelta(seconds=1), resolved_at=None, severity="CRITICAL",
                    diagnosis="AGENT_OFFLINE", probable_location="PROBE offline-client OFFLINE; stream state is unknown",
                    affected_agents=["offline-client"], symptoms=[], active=True, fingerprint="v2:offline",
                    context={"last_seen_at": (client_at - timedelta(minutes=1)).isoformat()},
                ),
            ])
            session.commit()
        response = client.get("/api/v2/streams/demo/events", params={
            "from": (now - timedelta(seconds=3)).isoformat(), "to": (now + timedelta(seconds=10)).isoformat(),
        }, headers=headers)

    assert response.status_code == 200, response.text
    events = response.json()["events"]
    network = next(item for item in events if item["id"] == "v2-network-incident")
    offline = next(item for item in events if item["id"] == "v2-offline-incident")
    assert network["cause_key"] == "NETWORK_PATH"
    assert network["confidence"] == "LIKELY"
    assert "мережевою ознакою" in network["summary"]
    assert offline["summary"] == "Probe не надсилає телеметрію; стан потоку в цій точці невідомий."
    assert offline["cause_key"] == "INSUFFICIENT_EVIDENCE"
    assert offline["confidence"] == "UNCONFIRMED"


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


def test_probe_enrollment_is_https_bound_single_use_and_returns_agent_config(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={
            "id": "poland", "name": "Poland", "public_url": "rtmp://127.0.0.1:1935/live/poland",
        })
        payload = {
            "name": "predator", "location": "studio", "platform": "Windows",
            "role": "CLIENT", "stream_id": "poland", "central_url": "https://monitor.example.net",
        }
        assert client.post("/api/v2/probe-enrollments", json=payload).status_code == 401
        insecure = {**payload, "central_url": "http://monitor.example.net"}
        assert client.post("/api/v2/probe-enrollments", headers=headers, json=insecure).status_code == 422

        created = client.post("/api/v2/probe-enrollments", headers=headers, json=payload)
        assert created.status_code == 201
        first_enrollment = created.json()
        assert first_enrollment["expires_in_seconds"] == 900
        assert len(first_enrollment["code"]) >= 32
        reissued = client.post("/api/v2/probe-enrollments", headers=headers, json=payload)
        assert reissued.status_code == 201
        enrollment = reissued.json()
        assert client.post("/api/v2/probe-enrollments/redeem", json={"code": first_enrollment["code"]}).status_code == 400
        with app.state.sessions() as session:
            saved = session.scalars(select(ProbeEnrollment).where(ProbeEnrollment.agent_id == enrollment["agent_id"])).all()
            assert all(first_enrollment["code"] not in item.code_hash and enrollment["code"] not in item.code_hash for item in saved)

        redeemed = client.post("/api/v2/probe-enrollments/redeem", json={"code": enrollment["code"]})
        assert redeemed.status_code == 200
        provision = redeemed.json()
        assert provision["config"]["server"]["url"] == "https://monitor.example.net"
        assert provision["config"]["agent"]["role"] == "CLIENT"
        assert provision["config"]["streams"] == [{
            "id": "poland", "url": "rtmp://127.0.0.1:1935/live/poland", "role": "CLIENT",
        }]
        token = provision["config"]["agent"]["token"]
        assert client.post("/api/v2/probe-enrollments/redeem", json={"code": enrollment["code"]}).status_code == 400
        sample = {"stream_id": "poland", "observed_at": datetime.now(timezone.utc).isoformat(),
                  "metrics": {}, "events": [], "context": {}}
        assert client.post("/api/v1/ingest", headers={"Authorization": f"Bearer {token}"}, json={"items": [sample]}).status_code == 200
        assert client.post("/api/v2/probe-enrollments", headers=headers, json=payload).status_code == 409


def test_probe_enrollment_rejects_expired_code(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        client.post("/api/v1/streams", headers=headers, json={
            "id": "poland", "name": "Poland", "public_url": "rtmp://127.0.0.1:1935/live/poland",
        })
        created = client.post("/api/v2/probe-enrollments", headers=headers, json={
            "name": "predator", "platform": "Windows", "role": "CLIENT", "stream_id": "poland",
            "central_url": "https://monitor.example.net",
        }).json()
        with app.state.sessions() as session:
            enrollment = session.scalar(
                select(ProbeEnrollment).where(ProbeEnrollment.agent_id == created["agent_id"])
            )
            enrollment.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()
        assert client.post("/api/v2/probe-enrollments/redeem", json={"code": created["code"]}).status_code == 400


def test_openai_key_secret_file_takes_precedence_over_legacy_environment(tmp_path, monkeypatch):
    secret_file = tmp_path / "openai.key"
    secret_file.write_text("secret-from-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret_file))
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-env-secret")
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=tmp_path / "admin.token",
    ))
    assert app.state.openai_api_key == "secret-from-file"


def test_v2_events_explain_ingress_observation_gaps_and_keyframe_measurements(tmp_path):
    admin_file = tmp_path / "admin.token"
    app = create_app(CentralFileConfig(
        database_url=f"sqlite:///{(tmp_path / 'central.db').as_posix()}",
        admin_token_file=admin_file,
    ))
    admin = admin_file.read_text(encoding="utf-8").strip()
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(seconds=1), now + timedelta(seconds=1)

    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin}"}
        assert client.post("/api/v1/streams", headers=headers, json={
            "id": "poland", "name": "Poland", "public_url": "rtmp://127.0.0.1:1935/live/poland",
        }).status_code == 201
        probes = {}
        for name, role in (("srs-ingress", "SERVER_INGRESS"), ("predator", "CLIENT")):
            response = client.post("/api/v1/agents", headers=headers, json={
                "name": name, "location": "test", "platform": "Linux", "role": role, "stream_id": "poland",
            })
            assert response.status_code == 201, response.text
            probes[name] = response.json()["token"]

        ingress = {
            "sample_id": "srs-counters-unavailable",
            "stream_id": "poland",
            "observed_at": now.isoformat(),
            "status": "WARNING",
            "metrics": {
                "srs_api_available": True, "ingress_active": True,
                "last_ingress_progress_age": 7.5, "ingress_recv_kbps_30s": 850,
            },
            "events": [{"code": "SRS_COUNTERS_UNAVAILABLE", "severity": "WARNING", "details": {"message": "private SRS response"}}],
            "context": {},
        }
        keyframe = {
            "sample_id": "keyframe-gap",
            "stream_id": "poland",
            "observed_at": now.isoformat(),
            "status": "CRITICAL",
            "metrics": {},
            "events": [{"code": "KEYFRAME_GAP", "severity": "CRITICAL", "details": {
                "seconds_without_keyframe": 8.25, "threshold_seconds": 5.0, "expected_gop_seconds": 2.0,
                "token": "must-not-leak",
            }}],
            "context": {},
        }
        for probe_name, sample in (("srs-ingress", ingress), ("predator", keyframe)):
            response = client.post("/api/v1/ingest", headers={
                "Authorization": f"Bearer {probes[probe_name]}",
            }, json={"items": [sample]})
            assert response.status_code == 200, response.text

        response = client.get("/api/v2/streams/poland/events", headers=headers, params={
            "from": start.isoformat(), "to": end.isoformat(),
        })
        assert response.status_code == 200, response.text
        rows = {event["code"]: event for event in response.json()["events"] if event["kind"] == "probe_event"}

    srs = rows["SRS_COUNTERS_UNAVAILABLE"]
    assert srs["summary"] == "SRS не надав лічильники руху медіаданих."
    assert "стан медіа на вході невідомий" in srs["explanation"]
    assert "SRS_COUNTERS_UNAVAILABLE" not in srs["explanation"]
    evidence = {item["metric"]: item for item in srs["evidence"]}
    assert evidence["last_ingress_progress_age"]["value"] == 7.5
    assert evidence["last_ingress_progress_age"]["unit"] == "s"
    assert evidence["srs_api_available"]["value"] is True

    gap = rows["KEYFRAME_GAP"]
    assert "8.25 с" in gap["explanation"] and "5 с" in gap["explanation"]
    assert "не визначає місце виникнення проблеми" in gap["explanation"]
    assert "must-not-leak" not in response.text
    assert "private SRS response" not in response.text

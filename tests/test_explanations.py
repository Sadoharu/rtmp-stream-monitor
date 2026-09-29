import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rtmp_monitor import explanations


def _incident(*, name="predator-private-name", diagnosis="CLIENT_PROBLEM", opened_at=None, context=None):
    opened_at = opened_at or datetime.now(timezone.utc)
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        opened_at=opened_at,
        updated_at=opened_at,
        diagnosis=diagnosis,
        severity="WARNING",
        probable_location="CLIENT RECEIVE / DECODER",
        active=False,
        context=context or {
            "timeline": [{
                "timestamp": opened_at.isoformat(),
                "agent": name,
                "role": "CLIENT",
                "status": "WARNING",
                "metrics": {
                    "profile": "DEEP",
                    "decode_errors": 0,
                    "network": {"provider": "linux", "rtt_ms": 8, "tcp_retransmissions": 0},
                    "url": "rtmp://secret.example/live/key",
                    "token": "probe-secret-token",
                },
                "events": [{"code": "PTS_REGRESSION", "severity": "WARNING", "details": {"previous_pts": 10, "pts": 9, "secret": "must-not-send"}}],
            }],
            "diagnostics": [{"stderr_tail": ["private ffmpeg log"]}],
        },
    )


def test_evidence_packet_is_pseudonymized_and_excludes_urls_tokens_and_logs():
    incident = _incident()

    packet = explanations.build_evidence_packet(incident, [])
    serialized = json.dumps(packet)

    assert "predator-private-name" not in serialized
    assert "rtmp://" not in serialized
    assert "probe-secret-token" not in serialized
    assert "must-not-send" not in serialized
    assert "private ffmpeg log" not in serialized
    assert packet["observations"][0]["probe"] == "CLIENT_1"
    assert packet["observations"][0]["events"] == [{
        "code": "PTS_REGRESSION", "severity": "WARNING", "details": {"previous_pts": 10, "pts": 9},
    }]
    assert any("PTS" in item["fact"] for item in packet["evidence"])


def test_deterministic_client_explanation_says_unknown_when_network_metrics_are_missing():
    incident = _incident(context={"timeline": [{
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "predator", "role": "CLIENT", "status": "WARNING", "metrics": {},
        "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}],
    }]})

    packet = explanations.build_evidence_packet(incident, [])
    result = explanations.deterministic_explanation(packet)

    assert result["confidence"] == "low"
    assert result["cause_key"] == "INSUFFICIENT_EVIDENCE"
    assert "не знайдено достатньо" in result["likely_cause"]
    assert any("мережевих метрик" in item["fact"] for item in packet["evidence"])
    assert result["next_checks"]


def test_repeated_incidents_are_reported_as_a_pattern():
    now = datetime.now(timezone.utc)
    packet = explanations.build_evidence_packet(
        _incident(opened_at=now),
        [_incident(opened_at=now - timedelta(minutes=1))],
    )
    result = explanations.deterministic_explanation(packet)

    assert any("2 інциденти" in item["fact"] for item in packet["evidence"])
    assert "2 окремих інцидентах" in result["summary"]
    assert "не самостійний доказ першопричини" in result["likely_cause"]


def test_unsynchronized_probe_clock_is_an_evidence_caveat():
    now = datetime.now(timezone.utc)
    incident = _incident(context={"timeline": [{
        "timestamp": now.isoformat(), "agent": "client", "role": "CLIENT", "status": "WARNING",
        "metrics": {"clock": {"ntp_synchronized": False, "estimated_offset_ms": 1200}},
        "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}],
    }]})

    packet = explanations.build_evidence_packet(incident, [])

    assert any(item["category"] == "clock" for item in packet["evidence"])
    assert packet["observations"][0]["metrics"]["clock"]["ntp_synchronized"] is False


def test_causal_analysis_localizes_client_network_problem_from_contemporaneous_egress():
    now = datetime.now(timezone.utc)
    incident = _incident(context={"timeline": [
        {"timestamp": now.isoformat(), "agent": "server", "role": "SERVER_EGRESS", "status": "OK",
         "metrics": {"profile": "DEEP", "ffmpeg_running": True, "last_frame_age": 0.1, "fps": 50, "clock": {"ntp_synchronized": True}}, "events": []},
        {"timestamp": (now + timedelta(seconds=1)).isoformat(), "agent": "predator", "role": "CLIENT", "status": "WARNING",
         "metrics": {"profile": "DEEP", "last_frame_age": 5, "network": {"provider": "linux", "tcp_retransmissions": 4}, "clock": {"ntp_synchronized": True}},
         "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}]},
    ]})

    packet = explanations.build_evidence_packet(incident, [])

    assert packet["causal_analysis"]["cause_key"] == "NETWORK_PATH"
    assert packet["causal_analysis"]["confidence"] == "medium"


def test_unsynchronized_clocks_lower_cross_probe_network_confidence():
    now = datetime.now(timezone.utc)
    incident = _incident(context={"timeline": [
        {"timestamp": now.isoformat(), "agent": "server", "role": "SERVER_EGRESS", "status": "OK",
         "metrics": {"profile": "DEEP", "last_frame_age": 0.1, "fps": 50, "clock": {"ntp_synchronized": False}}, "events": []},
        {"timestamp": (now + timedelta(seconds=1)).isoformat(), "agent": "predator", "role": "CLIENT", "status": "WARNING",
         "metrics": {"last_frame_age": 5, "network": {"provider": "linux", "tcp_retransmissions": 4}, "clock": {"ntp_synchronized": False}},
         "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}]},
    ]})

    packet = explanations.build_evidence_packet(incident, [])

    assert packet["causal_analysis"]["cause_key"] == "NETWORK_PATH"
    assert packet["causal_analysis"]["confidence"] == "low"
    assert "синхронізацію годинників" in packet["causal_analysis"]["summary"]


def test_causal_analysis_can_distinguish_server_restream_from_validated_ingress():
    now = datetime.now(timezone.utc)
    incident = _incident(context={"timeline": [
        {"timestamp": now.isoformat(), "agent": "ingress", "role": "SERVER_INGRESS", "status": "OK",
         "metrics": {"ingress_quality": "MEDIA_VALIDATED", "ingress_video_frames": 100, "clock": {"ntp_synchronized": True}}, "events": []},
        {"timestamp": (now + timedelta(seconds=1)).isoformat(), "agent": "egress", "role": "SERVER_EGRESS", "status": "WARNING",
         "metrics": {"profile": "DEEP", "last_frame_age": 6, "clock": {"ntp_synchronized": True}},
         "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}]},
    ]})
    incident.diagnosis = "RTMP_SERVER_RESTREAM_UNCONFIRMED"

    packet = explanations.build_evidence_packet(incident, [])

    assert packet["causal_analysis"]["cause_key"] == "RTMP_SERVER_RESTREAM"


def test_client_decoder_error_with_clean_per_flow_tcp_is_not_called_network_fault():
    now = datetime.now(timezone.utc)
    incident = _incident(context={"timeline": [
        {"timestamp": now.isoformat(), "agent": "server", "role": "SERVER_EGRESS", "status": "OK",
         "metrics": {"profile": "DEEP", "last_frame_age": 0.1, "fps": 50, "clock": {"ntp_synchronized": True}}, "events": []},
        {"timestamp": (now + timedelta(seconds=1)).isoformat(), "agent": "client", "role": "CLIENT", "status": "WARNING",
         "metrics": {"profile": "DEEP", "decode_errors": 1, "last_frame_age": 0.1,
                     "network": {"provider": "linux", "tcp_state": "ESTABLISHED", "tcp_retransmissions": 0},
                     "clock": {"ntp_synchronized": True}},
         "events": [{"code": "DECODE_ERROR", "severity": "WARNING", "details": {}}]},
    ]})

    packet = explanations.build_evidence_packet(incident, [])

    assert packet["causal_analysis"]["cause_key"] == "CLIENT_RECEIVE_OR_DECODER"


def test_matching_upstream_media_error_localizes_source_ingest():
    now = datetime.now(timezone.utc)
    error = [{"code": "PTS_REGRESSION", "severity": "WARNING", "details": {"previous_pts": 10, "pts": 9}}]
    clock = {"ntp_synchronized": True}
    incident = _incident(context={"timeline": [
        {"timestamp": now.isoformat(), "agent": "source", "role": "SOURCE", "status": "WARNING",
         "metrics": {"profile": "DEEP", "last_frame_age": 0.1, "clock": clock}, "events": error},
        {"timestamp": (now + timedelta(seconds=1)).isoformat(), "agent": "server", "role": "SERVER_EGRESS", "status": "WARNING",
         "metrics": {"profile": "DEEP", "last_frame_age": 0.1, "clock": clock}, "events": error},
        {"timestamp": (now + timedelta(seconds=2)).isoformat(), "agent": "client", "role": "CLIENT", "status": "WARNING",
         "metrics": {"profile": "DEEP", "last_frame_age": 0.1, "clock": clock}, "events": error},
    ]})
    incident.diagnosis = "SOURCE_OR_INGEST_PROBLEM"

    packet = explanations.build_evidence_packet(incident, [])

    assert packet["causal_analysis"]["cause_key"] == "SOURCE_OR_INGEST"
    assert packet["causal_analysis"]["confidence"] == "medium"


def test_old_pts_event_in_a_related_episode_does_not_explain_current_client_fault():
    now = datetime.now(timezone.utc)
    current = _incident(opened_at=now, context={"timeline": [
        {"timestamp": now.isoformat(), "agent": "server", "role": "SERVER_EGRESS", "status": "OK",
         "metrics": {"profile": "DEEP", "ffmpeg_running": True, "last_frame_age": 0.1, "fps": 50}, "events": []},
        {"timestamp": (now + timedelta(seconds=1)).isoformat(), "agent": "predator", "role": "CLIENT", "status": "WARNING",
         "metrics": {"profile": "DEEP", "last_frame_age": 5},
         "events": [{"code": "FREEZE_START", "severity": "WARNING", "details": {}}]},
    ]})
    older = _incident(opened_at=now - timedelta(minutes=5), diagnosis="PTS_REGRESSION", context={"timeline": [
        {"timestamp": (now - timedelta(minutes=5)).isoformat(), "agent": "ingress", "role": "SERVER_INGRESS", "status": "WARNING",
         "metrics": {}, "events": [{"code": "PTS_REGRESSION", "severity": "WARNING", "details": {}}]},
    ]})

    packet = explanations.build_evidence_packet(current, [older])
    result = explanations.deterministic_explanation(packet)

    assert result["cause_key"] == "DOWNSTREAM_PATH_UNCONFIRMED"
    assert "PTS/DTS" in " ".join(result["other_possible_causes"])


def test_openai_request_uses_store_false_and_sends_only_sanitized_evidence(monkeypatch):
    packet = explanations.build_evidence_packet(_incident(), [])
    response_body = {"output_text": json.dumps({
        "summary": "Коротке пояснення.",
        "cause_key": packet["causal_analysis"]["cause_key"],
        "likely_cause": "Є аномалія часових міток.",
        "confidence": "high",
        "evidence_ids": [packet["evidence"][0]["id"], "E-unknown"],
        "other_possible_causes": [],
        "next_checks": ["Порівняти PTS на egress."],
    })}
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(response_body).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(explanations, "urlopen", fake_urlopen)
    result = explanations.openai_explanation(packet, "sk-secret", "gpt-test")

    sent = json.dumps(captured["payload"])
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["authorization"] == "Bearer sk-secret"
    assert captured["payload"]["store"] is False
    assert captured["payload"]["text"]["format"]["type"] == "json_schema"
    assert "predator-private-name" not in sent
    assert "rtmp://" not in sent
    assert "probe-secret-token" not in sent
    assert "private ffmpeg log" not in sent
    assert result["evidence_ids"] == [packet["evidence"][0]["id"]]
    assert result["confidence"] == packet["causal_analysis"]["confidence"]
    assert result["ai_generated"] is True

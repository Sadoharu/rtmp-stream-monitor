from datetime import datetime, timezone
from types import SimpleNamespace

from rtmp_monitor.api import (
    _probe_event_evidence,
    _probe_event_explanation,
    _probe_event_summary,
    _safe_event_details,
)
from rtmp_monitor.explanations import EVENT_CODES, NETWORK_EVENT_CODES, _safe_events


# Keep this list aligned with event producers in agent.py, analyzer.py,
# srs_ingress.py, and the network telemetry contract.
PRODUCED_EVENT_CODES = {
    "KEYFRAME_GAP", "KEYFRAME_GAP_END", "DECODE_ERROR", "FREEZE_START", "FREEZE_DURATION", "FREEZE_END",
    "STREAM_STALL", "FFMPEG_DEAD", "FFMPEG_EXIT", "PROBE_ERROR", "DTS_REGRESSION", "PTS_REGRESSION",
    "PTS_JUMP", "PROGRESS_STALE", "SILENCE_START", "SILENCE_DURATION", "SILENCE_END", "AUDIO_MISSING",
    "AV_TIMESTAMP_DRIFT", "FFMPEG_RESTART", "AGENT_OFFLINE", "STREAM_OFFLINE", "SRS_PUBLISH_STATE_UNAVAILABLE",
    "SRS_COUNTERS_UNAVAILABLE", "SRS_API_UNAVAILABLE", "INGRESS_RECOVERED",
} | NETWORK_EVENT_CODES


def test_every_produced_event_is_allowlisted_and_has_a_human_summary():
    assert PRODUCED_EVENT_CODES <= EVENT_CODES

    safe = _safe_events([{"code": code, "severity": "WARNING", "details": {"value": 4.5, "message": "private"}} for code in PRODUCED_EVENT_CODES])
    assert {event["code"] for event in safe} == PRODUCED_EVENT_CODES
    assert all(event.get("details") == {"value": 4.5} for event in safe)

    for code in PRODUCED_EVENT_CODES:
        summary = _probe_event_summary(code)
        explanation = _probe_event_explanation(code, "SERVER_INGRESS", {}, {})
        assert code not in summary
        assert code not in explanation
        assert "_" not in summary


def test_event_details_preserve_keyframe_stall_and_ffmpeg_measurements_only():
    details = _safe_event_details({
        "seconds_without_keyframe": 8.25,
        "threshold_seconds": 5.0,
        "expected_gop_seconds": 2.0,
        "progress_age_seconds": 9.0,
        "media_age_seconds": 8.0,
        "seconds_without_ingress_progress": 7.5,
        "value": 123.0,
        "duration_seconds": 4.0,
        "token": "must-not-leak",
        "message": "raw diagnostic text must not leak",
    })

    assert details == {
        "seconds_without_keyframe": 8.25,
        "threshold_seconds": 5.0,
        "expected_gop_seconds": 2.0,
        "progress_age_seconds": 9.0,
        "media_age_seconds": 8.0,
        "seconds_without_ingress_progress": 7.5,
        "value": 123.0,
        "duration_seconds": 4.0,
    }


def test_keyframe_and_srs_explanations_state_the_measurement_and_limits():
    keyframe = _probe_event_explanation(
        "KEYFRAME_GAP",
        "SERVER_INGRESS",
        {"seconds_without_keyframe": 8.25, "threshold_seconds": 5.0, "expected_gop_seconds": 2.0},
        {},
    )
    assert "8.25 с" in keyframe and "5 с" in keyframe and "2 с" in keyframe
    assert "не визначає місце" in keyframe
    assert "SERVER_INGRESS" not in keyframe

    unavailable = _probe_event_explanation("SRS_API_UNAVAILABLE", "SERVER_INGRESS", {}, {})
    assert "обмежує спостереження" in unavailable
    assert "стан медіа на вході невідомий" in unavailable
    assert "не доводить збій потоку" in unavailable

    duration = _probe_event_explanation("SILENCE_DURATION", "CLIENT", {"duration_seconds": 12.0}, {})
    assert "тиші тривалістю 12 с" in duration
    assert "завмирання" not in duration


def test_ingress_evidence_includes_freshness_and_srs_counters():
    agent = SimpleNamespace(id="probe-id", role="SERVER_INGRESS")
    item = SimpleNamespace(metrics={
        "srs_api_available": True,
        "ingress_active": False,
        "last_ingress_progress_age": 7.5,
        "ingress_recv_kbps_30s": 1200,
        "ingress_recv_bytes": 45_000,
        "ingress_frames": 500,
    })

    evidence = _probe_event_evidence(agent, item, {}, datetime(2026, 9, 29, tzinfo=timezone.utc))
    metrics = {row["metric"]: row for row in evidence}
    assert metrics["srs_api_available"]["value"] is True
    assert metrics["ingress_active"]["value"] is False
    assert metrics["last_ingress_progress_age"]["unit"] == "s"
    assert metrics["ingress_recv_kbps_30s"]["unit"] == "kbps"
    assert metrics["ingress_recv_bytes"]["unit"] == "bytes"
    assert metrics["ingress_frames"]["unit"] == "count"

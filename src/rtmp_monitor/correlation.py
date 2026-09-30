from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import AggregateCursor, Agent, Incident, MetricAggregate, Telemetry, utcnow
from .network_evidence import network_sample_is_stale

BAD_EVENT_CODES = {
    "KEYFRAME_GAP", "DECODE_ERROR", "BITSTREAM_PARSE_ERROR", "FREEZE_START", "STREAM_STALL", "FFMPEG_DEAD",
    "FFMPEG_EXIT", "PROBE_ERROR", "DTS_REGRESSION", "PTS_REGRESSION", "PTS_JUMP", "PROGRESS_STALE",
    "SILENCE_START", "AUDIO_MISSING", "AV_TIMESTAMP_DRIFT", "FFMPEG_RESTART", "AGENT_OFFLINE", "STREAM_OFFLINE",
}
NETWORK_EVENT_CODES = {"TCP_RETRANSMISSION", "TCP_RESET", "PACKET_LOSS", "RTT_SPIKE", "CONNECTION_RESET"}


def _errors(observation: dict[str, Any]) -> list[dict[str, Any]]:
    events = observation.get("events") or []
    return [event for event in events if event.get("code") in BAD_EVENT_CODES]


def _is_bad(observation: dict[str, Any]) -> bool:
    role = str(observation.get("role", "")).upper()
    status = str(observation.get("status", "OK")).upper()
    metrics = observation.get("metrics") or {}
    if role == "SERVER_INGRESS":
        # An unreachable SRS API means the observation point is unknown, not
        # that the publisher or incoming media is broken.
        if metrics.get("srs_api_available") is False:
            return False
        if metrics.get("ingress_quality") == "PUBLISHER_COUNTERS_ONLY":
            return status in {"CRITICAL", "ERROR", "STREAM_OFFLINE", "STREAM_STALLED"} or bool(_errors(observation))
        return status in {"WARNING", "CRITICAL", "ERROR", "STREAM_OFFLINE", "STREAM_STALLED"} or bool(_errors(observation))
    return status in {"WARNING", "CRITICAL", "ERROR"} or bool(_errors(observation))


def _network_is_bad(observation: dict[str, Any]) -> bool:
    metrics = observation.get("metrics") or {}
    network = metrics.get("network") or {}
    if any(event.get("code") in NETWORK_EVENT_CODES for event in observation.get("events") or []):
        return True
    if network_sample_is_stale(network):
        return False
    retransmits = network.get("tcp_retransmissions")
    tcp_state = str(network.get("tcp_state", "")).upper()
    receiver_episodes = network.get("tcp_duplicate_ack_episodes")
    receiver_stats_available = (
        network.get("provider") == "windows"
        and network.get("tcp_receiver_stats_status") == "AVAILABLE"
        and isinstance(network.get("tcp_receiver_stats_flow_count"), (int, float))
        and network["tcp_receiver_stats_flow_count"] > 0
    )
    tcp_state_is_bad = bool(tcp_state and tcp_state not in {"ESTABLISHED", "ESTAB", "UNKNOWN"})
    sample_age = network.get("sample_age_seconds")
    frame_age = metrics.get("last_frame_age")
    icmp_loss = network.get("packet_loss_percent")
    icmp_replies = network.get("icmp_reply_count")
    # A single dropped echo out of three is weak evidence; zero replies can
    # simply mean the target blocks ICMP. Only substantial, partial ICMP loss
    # supports a network diagnosis on its own.
    icmp_loss_is_strong = (
        isinstance(icmp_loss, (int, float))
        and icmp_loss >= 50
        and isinstance(icmp_replies, (int, float))
        and icmp_replies > 0
    )
    if (
        tcp_state_is_bad
        and isinstance(sample_age, (int, float))
        and isinstance(frame_age, (int, float))
        and frame_age < sample_age
    ):
        # Media received after a "not established" sample proves that the
        # connection came up later; that old state cannot explain a newer event.
        tcp_state_is_bad = False
    return bool(
        (network.get("provider") != "windows" and isinstance(retransmits, (int, float)) and retransmits > 0)
        or (receiver_stats_available and isinstance(receiver_episodes, (int, float)) and receiver_episodes > 0)
        or icmp_loss_is_strong
        or (isinstance(network.get("rtt_ms"), (int, float)) and network["rtt_ms"] > 100)
        or tcp_state_is_bad
    )


def _has_unattributed_windows_retransmits(observation: dict[str, Any]) -> bool:
    network = (observation.get("metrics") or {}).get("network") or {}
    retransmits = network.get("tcp_retransmissions")
    return (
        not network_sample_is_stale(network)
        and network.get("provider") == "windows"
        and isinstance(retransmits, (int, float))
        and retransmits > 0
    )


def diagnose_observations(observations: list[dict[str, Any]], media_tolerance_seconds: float = 5.0) -> dict[str, Any] | None:
    """Classify a fault only to the strongest location supported by current probes."""
    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        by_role[str(observation.get("role", "")).upper()].append(observation)
    ingress = by_role["SERVER_INGRESS"]
    source_probes = by_role["SOURCE"]
    sources = ingress + source_probes
    egress = by_role["SERVER_EGRESS"]
    clients = by_role["CLIENT"]
    bad_sources = [item for item in sources if _is_bad(item)]
    bad_egress = [item for item in egress if _is_bad(item)]
    bad_clients = [item for item in clients if _is_bad(item)]
    involved = bad_sources + bad_egress + bad_clients
    if not involved:
        return None
    media = _media_correlation(observations, media_tolerance_seconds)
    media_lags: list[dict[str, Any]] = []

    if bad_sources:
        if media["available"] and not media["aligned"] and (bad_egress or bad_clients):
            diagnosis = "SOURCE_OR_INGEST_UNCONFIRMED"
            location = f"SOURCE / INGEST IS DEGRADED, BUT DOWNSTREAM MEDIA TIMESTAMPS DO NOT ALIGN (spread {media['spread_seconds']} s)"
        else:
            diagnosis = "SOURCE_OR_INGEST_PROBLEM"
            location = "SOURCE / INGEST (the earliest observed point with matching symptoms)"
        affected = involved
    elif bad_egress:
        available_ingress = [item for item in ingress if (item.get("metrics") or {}).get("srs_api_available") is not False]
        clean_ingress_exists = bool(available_ingress) and not any(_is_bad(item) for item in available_ingress)
        ingress_media_validated = clean_ingress_exists and all(
            (item.get("metrics") or {}).get("ingress_quality") == "MEDIA_VALIDATED"
            for item in available_ingress
        )
        clean_source_exists = bool(sources) and not bad_sources
        if clean_ingress_exists and not ingress_media_validated:
            diagnosis = "RTMP_SERVER_RESTREAM_UNCONFIRMED"
            if all((item.get("metrics") or {}).get("ingress_quality") == "PUBLISHER_COUNTERS_ONLY" for item in available_ingress):
                location = "SRS HTTP API confirms an active publisher and ingress counters, but it does not validate decoded frames or GOPs; SOURCE / INGEST and SERVER EGRESS cannot yet be separated"
            else:
                location = "SERVER_INGRESS has no explicit decoded-media/GOP validation; SOURCE / INGEST and SERVER EGRESS cannot yet be separated"
        elif clean_ingress_exists and (not media["available"] or media["aligned"]):
            diagnosis = "RTMP_SERVER_RESTREAM_PROBLEM"
            location = "RTMP SERVER RESTREAM BETWEEN SERVER_INGRESS AND SERVER_EGRESS"
        elif clean_ingress_exists:
            diagnosis = "RTMP_SERVER_RESTREAM_UNCONFIRMED"
            location = f"SERVER EGRESS IS DEGRADED, BUT MEDIA TIMESTAMPS DO NOT ALIGN WITH OTHER PROBES (spread {media['spread_seconds']} s)"
        elif source_probes and clean_source_exists:
            diagnosis = "SOURCE_TO_SERVER_UNCONFIRMED"
            location = "BETWEEN SOURCE PROBE AND SERVER EGRESS; A SERVER_INGRESS OBSERVATION IS MISSING"
        else:
            diagnosis = "UPSTREAM_OR_SERVER_UNCONFIRMED"
            location = "SOURCE / INGEST OR RTMP SERVER; TRUE SERVER_INGRESS OBSERVATION IS MISSING"
        affected = bad_egress + bad_clients
    elif bad_clients:
        media_lags = _client_media_lags(egress, bad_clients)
        packet_timestamp_lag = any(
            item["lag_seconds"] > media_tolerance_seconds
            and item["client_profile"] == "LIGHT"
            and item["server_profile"] == "LIGHT"
            and item["sample_skew_seconds"] is not None
            and abs(item["sample_skew_seconds"]) <= media_tolerance_seconds
            for item in media_lags
        )
        network_bad = any(_network_is_bad(item) for item in bad_clients) or packet_timestamp_lag
        if network_bad:
            diagnosis = "NETWORK_PATH_PROBLEM"
            location = "NETWORK BETWEEN SERVER EGRESS AND AFFECTED CLIENT"
            if packet_timestamp_lag:
                location += f" (media PTS lag {max(item['lag_seconds'] for item in media_lags)} s)"
        elif any(_has_unattributed_windows_retransmits(item) for item in bad_clients):
            diagnosis = "NETWORK_PATH_UNCONFIRMED"
            location = "CLIENT PATH MAY BE DEGRADED, BUT THE WINDOWS RETRANSMIT COUNTER IS HOST-WIDE AND CANNOT BE ATTRIBUTED TO THIS RTMP FLOW"
        elif not egress:
            diagnosis = "CLIENT_PATH_UNCONFIRMED"
            location = "CLIENT OBSERVATION IS DEGRADED, BUT SERVER_EGRESS IS NOT OBSERVED; CLIENT, SERVER RESTREAM, AND UPSTREAM CAUSES CANNOT BE SEPARATED"
        else:
            diagnosis = "CLIENT_PROBLEM"
            location = "CLIENT RECEIVE / DECODER (network counters do not show a transport fault)"
        affected = bad_clients
    else:
        return None

    severity = "CRITICAL" if any(str(item.get("status", "")).upper() in {"CRITICAL", "ERROR"} for item in affected) or any(
        str(event.get("severity", "")).upper() == "CRITICAL" for item in affected for event in _errors(item)
    ) else "WARNING"
    symptoms = [
        {"agent": item.get("name"), "role": item.get("role"), "status": item.get("status"), "events": _errors(item), "metrics": item.get("metrics", {})}
        for item in affected
    ]
    return {
        "diagnosis": diagnosis,
        "probable_location": location,
        "severity": severity,
        "affected_agents": [item.get("name", "unknown") for item in affected],
        "symptoms": symptoms,
        "media_correlation": media,
        "media_lags": media_lags if bad_clients else [],
    }


def correlate_stream(session: Session, stream_id: str, now: datetime | None = None, window_seconds: int = 20, media_tolerance_seconds: float = 5.0) -> Incident | None:
    # Ingest can evaluate several historical samples in one transaction. Flush
    # the prior snapshot's incident changes so the next snapshot sees them.
    session.flush()
    now = now or utcnow()
    cutoff = now - timedelta(seconds=window_seconds)
    rows = session.execute(
        select(Telemetry, Agent)
        .join(Agent, Telemetry.agent_id == Agent.id)
        .where(Telemetry.stream_id == stream_id, Agent.enabled.is_(True), Telemetry.observed_at >= cutoff, Telemetry.observed_at <= now)
        .order_by(Telemetry.observed_at.desc())
    ).all()
    latest_by_agent: dict[str, tuple[Telemetry, Agent]] = {}
    for telemetry, agent in rows:
        latest_by_agent.setdefault(agent.id, (telemetry, agent))
    observations = []
    for telemetry, agent in latest_by_agent.values():
        observations.append({
            "role": agent.role,
            "name": agent.name,
            "observed_at": _as_aware(telemetry.observed_at).isoformat(),
            "received_at": _as_aware(telemetry.received_at).isoformat(),
            "status": telemetry.status,
            "metrics": telemetry.metrics or {},
            "events": telemetry.events or [],
        })
    result = diagnose_observations(observations, media_tolerance_seconds)
    active = session.scalars(select(Incident).where(Incident.stream_id == stream_id, Incident.active.is_(True))).all()
    if result is None:
        for incident in active:
            if incident.diagnosis != "AGENT_OFFLINE" and _as_aware(incident.opened_at) <= _as_aware(now):
                incident.active = False
                incident.resolved_at = now
        _refresh_recent_resolved_contexts(session, stream_id, now)
        return None

    newer_active = [item for item in active if _as_aware(item.opened_at) > _as_aware(now)]
    if newer_active:
        # A delayed queue replay can reveal a different old diagnosis after a
        # newer incident is already active. Keep the current stream state; raw
        # telemetry still retains that older observation for timeline review.
        return max(newer_active, key=lambda item: _as_aware(item.opened_at))

    fingerprint = f"stream:{stream_id}:{result['diagnosis']}"
    existing = session.scalar(select(Incident).where(Incident.fingerprint == fingerprint, Incident.active.is_(True)).order_by(Incident.opened_at.desc()))
    if existing and _as_aware(now) < _as_aware(existing.updated_at):
        # Delayed/out-of-order samples can be replayed from the agent outbox.
        # They must not move a current active incident's timestamps or context back.
        return existing
    for incident in active:
        if (
            incident.fingerprint != fingerprint
            and incident.diagnosis != "AGENT_OFFLINE"
            and _as_aware(incident.opened_at) <= _as_aware(now)
        ):
            incident.active = False
            incident.resolved_at = now
    _refresh_recent_resolved_contexts(session, stream_id, now)
    if existing:
        incident = existing
        incident.updated_at = now
        incident.severity = result["severity"]
        incident.probable_location = result["probable_location"]
        incident.affected_agents = sorted(set((incident.affected_agents or []) + result["affected_agents"]))
        incident.symptoms = _merge_symptoms(incident.symptoms or [], result["symptoms"])
    else:
        incident = Incident(
            id=str(uuid.uuid4()), stream_id=stream_id, opened_at=now, updated_at=now,
            severity=result["severity"], diagnosis=result["diagnosis"],
            probable_location=result["probable_location"], affected_agents=result["affected_agents"],
            symptoms=result["symptoms"], fingerprint=fingerprint, active=True,
        )
        session.add(incident)
    incident.context = _timeline_context(session, stream_id, incident.opened_at, now)
    incident.context["media_correlation"] = result["media_correlation"]
    incident.context["media_lags"] = result.get("media_lags", [])
    return incident


def _refresh_recent_resolved_contexts(session: Session, stream_id: str, now: datetime) -> None:
    """Keep extending resolved incidents' timeline through 60 seconds after open."""
    now = _as_aware(now)
    session.flush()
    recent = session.scalars(
        select(Incident)
        .where(
            Incident.stream_id == stream_id,
            Incident.active.is_(False),
            Incident.diagnosis != "AGENT_OFFLINE",
            Incident.opened_at >= now - timedelta(seconds=120),
            Incident.opened_at <= now,
        )
        .order_by(Incident.opened_at.desc())
        .limit(100)
    ).all()
    for incident in recent:
        opened_at = _as_aware(incident.opened_at)
        target_end = min(now, opened_at + timedelta(seconds=60))
        previous = incident.context or {}
        previous_end = previous.get("window_end")
        previous_end_at = None
        if previous_end:
            try:
                previous_end_at = _as_aware(datetime.fromisoformat(previous_end.replace("Z", "+00:00")))
                if previous_end_at >= target_end:
                    continue
            except (AttributeError, TypeError, ValueError):
                pass
        if (
            previous_end_at is not None
            and target_end < opened_at + timedelta(seconds=60)
            and (target_end - previous_end_at).total_seconds() < 5
        ):
            continue
        context = _timeline_context(session, stream_id, opened_at, target_end)
        for key, value in previous.items():
            context.setdefault(key, value)
        incident.context = context


def _timeline_context(session: Session, stream_id: str, opened_at: datetime, now: datetime) -> dict[str, Any]:
    start = _as_aware(opened_at) - timedelta(seconds=60)
    end = min(now, _as_aware(opened_at) + timedelta(seconds=60))
    rows = session.execute(
        select(Telemetry, Agent)
        .join(Agent, Telemetry.agent_id == Agent.id)
        .where(Telemetry.stream_id == stream_id, Telemetry.observed_at >= start, Telemetry.observed_at <= end)
        .order_by(Telemetry.observed_at.asc())
        .limit(2000)
    ).all()
    timeline = []
    diagnostics = []
    for item, agent in rows:
        timeline.append({"timestamp": _as_aware(item.observed_at).isoformat(), "agent": agent.name, "role": agent.role,
                         "status": item.status, "metrics": _context_metrics(item.metrics or {}), "events": item.events or []})
        if item.context and item.context.get("stderr_tail"):
            diagnostics.append({"timestamp": _as_aware(item.observed_at).isoformat(), "agent": agent.name,
                                "stderr_tail": item.context["stderr_tail"][-200:]})
    return {"timeline": timeline, "diagnostics": diagnostics[-20:], "window_start": start.isoformat(), "window_end": end.isoformat(), "captured_at": now.isoformat()}


def _context_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    keys = {"ffmpeg_running", "last_frame_age", "last_audio_frame_age", "frames", "packets", "keyframes", "i_frames", "i_frames_without_key_flag", "last_frame_type", "last_frame_is_keyframe", "last_keyframe_age", "current_gop_duration", "current_gop_frames", "expected_gop_frames", "expected_gop_seconds", "last_media_pts", "last_media_dts", "decode_errors", "reconnect_count", "fps", "source_fps", "resolution", "video_codec", "audio_codec", "bitrate", "received_media_bitrate_bps", "received_media_bitrate_quality", "measurement_window_seconds", "received_media_packet_count", "clock", "network", "ingress_quality", "srs_api_available", "ingress_active", "last_ingress_progress_age", "ingress_recv_kbps_30s", "ingress_recv_bytes", "ingress_video_frames", "ingress_audio_frames", "ingress_frames"}
    return {key: metrics[key] for key in keys if key in metrics}


def _media_correlation(observations: list[dict[str, Any]], tolerance_seconds: float = 5.0) -> dict[str, Any]:
    points = []
    for item in observations:
        value = (item.get("metrics") or {}).get("last_media_pts")
        if isinstance(value, (int, float)):
            points.append({"agent": item.get("name", "unknown"), "pts": float(value)})
    if len(points) < 2:
        return {"available": False, "aligned": None, "spread_seconds": None, "tolerance_seconds": tolerance_seconds, "points": points}
    spread = round(max(point["pts"] for point in points) - min(point["pts"] for point in points), 3)
    return {"available": True, "aligned": spread <= tolerance_seconds, "spread_seconds": spread,
            "tolerance_seconds": tolerance_seconds, "points": points}


def _parse_observation_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_aware(value)
    if not isinstance(value, str):
        return None
    try:
        return _as_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _client_media_lags(egress: list[dict[str, Any]], clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
    server_pts = []
    for item in egress:
        metrics = item.get("metrics") or {}
        value = metrics.get("last_media_pts")
        if isinstance(value, (int, float)):
            server_pts.append({
                "pts": float(value),
                "name": item.get("name", "server"),
                "observed_at": item.get("observed_at"),
                "received_at": item.get("received_at"),
                "profile": metrics.get("profile"),
            })
    lags = []
    for client in clients:
        client_metrics = client.get("metrics") or {}
        value = client_metrics.get("last_media_pts")
        if not isinstance(value, (int, float)) or not server_pts:
            continue
        client_received_at = _parse_observation_time(client.get("received_at"))
        timed_server_pts = []
        for item in server_pts:
            received_at = _parse_observation_time(item.get("received_at"))
            if received_at is not None:
                timed_server_pts.append((item, received_at))
        if client_received_at is not None and timed_server_pts:
            server_point, server_received_at = min(
                timed_server_pts,
                key=lambda pair: abs((pair[1] - client_received_at).total_seconds()),
            )
            sample_skew_seconds = round((client_received_at - server_received_at).total_seconds(), 3)
        else:
            server_point = min(server_pts, key=lambda item: abs(item["pts"] - float(value)))
            server_received_at = _parse_observation_time(server_point.get("received_at"))
            sample_skew_seconds = None
        lags.append({"client": client.get("name", "client"), "server_egress": server_point["name"],
                     "client_pts": float(value), "server_pts": server_point["pts"],
                     # Positive means the client is behind server egress. A
                     # client ahead of egress is a timestamp mismatch, not
                     # evidence of transport delay.
                     "lag_seconds": round(server_point["pts"] - float(value), 3),
                     "sample_skew_seconds": sample_skew_seconds,
                     "client_observed_at": client.get("observed_at"),
                     "server_observed_at": server_point.get("observed_at"),
                     "client_received_at": client.get("received_at"),
                     "server_received_at": server_point.get("received_at"),
                     "client_profile": client_metrics.get("profile"),
                     "server_profile": server_point.get("profile")})
    return lags


def _merge_symptoms(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in old + new:
        key = (str(item.get("agent")), ",".join(sorted(event.get("code", "") for event in item.get("events", []))))
        merged[key] = item
    return list(merged.values())[-50:]


def mark_offline_agents(session: Session, offline_after_seconds: int, now: datetime | None = None) -> None:
    now = now or utcnow()
    cutoff = now - timedelta(seconds=offline_after_seconds)
    for agent in session.scalars(select(Agent).where(Agent.enabled.is_(True))).all():
        last_contact = agent.last_seen_at or agent.created_at
        if _as_aware(last_contact) >= cutoff:
            for incident in session.scalars(select(Incident).where(Incident.fingerprint == f"agent:{agent.id}:offline", Incident.active.is_(True))).all():
                incident.active = False
                incident.resolved_at = now
            continue
        fingerprint = f"agent:{agent.id}:offline"
        incident = session.scalar(select(Incident).where(Incident.fingerprint == fingerprint, Incident.active.is_(True)))
        if incident:
            incident.updated_at = now
            continue
        session.add(Incident(
            id=str(uuid.uuid4()), stream_id=agent.stream_id, opened_at=now, updated_at=now,
            severity="CRITICAL", diagnosis="AGENT_OFFLINE",
            probable_location=f"PROBE {agent.name} OFFLINE; STREAM HEALTH AT THAT OBSERVATION POINT IS UNKNOWN",
            affected_agents=[agent.name], symptoms=[{"agent": agent.name, "role": agent.role, "status": "OFFLINE"}],
            context={"last_seen_at": _as_aware(agent.last_seen_at).isoformat() if agent.last_seen_at else None},
            fingerprint=fingerprint, active=True,
        ))


def run_retention(session: Session, raw_days: int, incident_days: int, aggregate_days: int = 90, now: datetime | None = None) -> tuple[int, int, int]:
    now = now or utcnow()
    raw_cutoff = now - timedelta(days=raw_days)
    incident_cutoff = now - timedelta(days=incident_days)
    aggregate_cutoff = now - timedelta(days=aggregate_days)
    safe_raw_cutoff = datetime.fromtimestamp(int(_as_aware(raw_cutoff).timestamp() // 60 * 60), timezone.utc)
    _aggregate_old_telemetry(session, safe_raw_cutoff)
    telemetry_deleted = session.query(Telemetry).filter(Telemetry.observed_at < safe_raw_cutoff).delete(synchronize_session=False)
    incidents_deleted = session.query(Incident).filter(Incident.active.is_(False), Incident.resolved_at < incident_cutoff).delete(synchronize_session=False)
    aggregates_deleted = session.query(MetricAggregate).filter(MetricAggregate.bucket_start < aggregate_cutoff).delete(synchronize_session=False)
    return telemetry_deleted, incidents_deleted, aggregates_deleted


def _aggregate_old_telemetry(session: Session, raw_cutoff: datetime) -> None:
    cutoff_ts = int(_as_aware(raw_cutoff).timestamp() // 60 * 60)
    complete_before = datetime.fromtimestamp(cutoff_ts, timezone.utc)
    if complete_before <= datetime.fromtimestamp(0, timezone.utc):
        return
    status_rank = {"OK": 0, "ONLINE": 0, "WARNING": 1, "CRITICAL": 2, "ERROR": 2}
    agents = session.scalars(select(Agent)).all()
    for agent in agents:
        cursor = session.get(AggregateCursor, agent.id)
        if cursor:
            start = _as_aware(cursor.processed_until)
        else:
            oldest = session.scalar(select(func.min(Telemetry.observed_at)).where(Telemetry.agent_id == agent.id))
            if oldest is None:
                continue
            oldest_ts = int(_as_aware(oldest).timestamp() // 60 * 60)
            start = datetime.fromtimestamp(oldest_ts, timezone.utc)
            cursor = AggregateCursor(agent_id=agent.id, processed_until=start)
            session.add(cursor)
        if start >= complete_before:
            continue
        rows = session.scalars(
            select(Telemetry).where(Telemetry.agent_id == agent.id, Telemetry.observed_at >= start, Telemetry.observed_at < complete_before)
            .order_by(Telemetry.observed_at.asc()).execution_options(yield_per=1000)
        )
        bucket: datetime | None = None
        sample_count = 0
        rank = 0
        status = "OK"
        numeric: dict[str, dict[str, Any]] = {}
        event_counts: dict[str, int] = {}

        def flush_bucket() -> None:
            nonlocal sample_count, status, rank, numeric, event_counts
            if bucket is None or sample_count == 0:
                return
            measures = {key: {"avg": round(value["sum"] / value["count"], 4), "min": round(value["min"], 4),
                              "max": round(value["max"], 4), "last": round(value["last"], 4),
                              "count": int(value["count"]), "last_observed_at": value["last_observed_at"]}
                        for key, value in numeric.items()}
            session.add(MetricAggregate(agent_id=agent.id, bucket_start=bucket, stream_id=agent.stream_id,
                                        bucket_seconds=60, sample_count=sample_count, status=status,
                                        metrics=measures, event_counts=dict(event_counts)))
            sample_count, status, rank, numeric, event_counts = 0, "OK", 0, {}, {}

        for item in rows:
            observed = _as_aware(item.observed_at)
            current_bucket = datetime.fromtimestamp(int(observed.timestamp() // 60 * 60), timezone.utc)
            if bucket is None:
                bucket = current_bucket
            elif current_bucket != bucket:
                flush_bucket()
                bucket = current_bucket
            sample_count += 1
            current_rank = status_rank.get(item.status.upper(), 0)
            if current_rank >= rank:
                rank, status = current_rank, item.status.upper()
            for key, value in _flatten_numeric(item.metrics or {}).items():
                measure = numeric.setdefault(key, {"sum": 0.0, "count": 0.0, "min": value, "max": value, "last": value,
                                                   "last_observed_at": observed.isoformat()})
                measure["sum"] += value
                measure["count"] += 1
                measure["min"] = min(measure["min"], value)
                measure["max"] = max(measure["max"], value)
                measure["last"] = value
                measure["last_observed_at"] = observed.isoformat()
            for event in item.events or []:
                code = event.get("code")
                if code:
                    event_counts[code] = event_counts.get(code, 0) + 1
        flush_bucket()
        cursor.processed_until = complete_before


def _flatten_numeric(value: dict[str, Any], prefix: str = "") -> dict[str, float]:
    result: dict[str, float] = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, (int, float)):
            result[name] = float(item)
        elif isinstance(item, dict):
            result.update(_flatten_numeric(item, name))
    return result


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

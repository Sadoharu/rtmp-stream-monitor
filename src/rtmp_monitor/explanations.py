from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROLES = {"SOURCE", "SERVER_INGRESS", "SERVER_EGRESS", "CLIENT"}
STATUSES = {"OK", "WARNING", "CRITICAL", "ERROR", "STREAM_OFFLINE", "STREAM_STALLED", "AGENT_OFFLINE", "TELEMETRY_STALE"}
EVENT_CODES = {
    "KEYFRAME_GAP", "DECODE_ERROR", "FREEZE_START", "FREEZE_END", "STREAM_STALL", "FFMPEG_DEAD",
    "FFMPEG_EXIT", "PROBE_ERROR", "DTS_REGRESSION", "PTS_REGRESSION", "PTS_JUMP", "PROGRESS_STALE",
    "SILENCE_START", "AUDIO_MISSING", "AV_TIMESTAMP_DRIFT", "FFMPEG_RESTART", "AGENT_OFFLINE",
    "STREAM_OFFLINE", "TCP_RETRANSMISSION", "TCP_RESET", "PACKET_LOSS", "RTT_SPIKE", "CONNECTION_RESET",
}
MEDIA_EVENT_CODES = {
    "KEYFRAME_GAP", "DECODE_ERROR", "FREEZE_START", "STREAM_STALL", "FFMPEG_DEAD", "FFMPEG_EXIT",
    "PROBE_ERROR", "DTS_REGRESSION", "PTS_REGRESSION", "PTS_JUMP", "PROGRESS_STALE", "SILENCE_START",
    "AUDIO_MISSING", "AV_TIMESTAMP_DRIFT", "FFMPEG_RESTART", "STREAM_OFFLINE",
}
NETWORK_EVENT_CODES = {"TCP_RETRANSMISSION", "TCP_RESET", "PACKET_LOSS", "RTT_SPIKE", "CONNECTION_RESET"}
CAUSE_KEYS = [
    "SOURCE_OR_INGEST", "RTMP_SERVER_RESTREAM", "NETWORK_PATH", "CLIENT_RECEIVE_OR_DECODER",
    "DOWNSTREAM_PATH_UNCONFIRMED", "UPSTREAM_OR_SERVER_UNCONFIRMED", "INSUFFICIENT_EVIDENCE",
]
METRIC_KEYS = {
    "profile", "ffmpeg_running", "last_frame_age", "last_audio_frame_age", "decode_errors", "fps",
    "resolution", "video_codec", "audio_codec", "bitrate", "last_keyframe_age", "current_gop_duration",
    "current_gop_frames", "expected_gop_frames", "expected_gop_seconds", "last_media_pts", "last_media_dts",
    "reconnect_count", "ingress_recv_kbps_30s", "ingress_recv_bytes", "ingress_video_frames",
    "ingress_audio_frames", "ingress_frames", "last_ingress_progress_age", "source_fps",
    "ingress_quality", "srs_api_available", "ingress_active", "frames", "packets", "keyframes",
}
CLOCK_KEYS = {"ntp_synchronized", "estimated_offset_ms", "central_offset_ms", "central_offset_uncertainty_ms"}
BOOL_METRIC_KEYS = {"ffmpeg_running", "srs_api_available", "ingress_active"}
NETWORK_KEYS = {
    "sample_age_seconds", "sample_interval_seconds", "tcp_retransmissions", "tcp_state", "rtt_ms",
    "packet_loss_percent", "icmp_reply_count", "icmp_probe_count", "provider", "icmp_status",
}
EVENT_DETAIL_KEYS = {
    "pts", "previous_pts", "dts", "previous_dts", "gap_seconds", "frame_age_seconds", "decode_errors",
    "reconnect_count", "rtt_ms", "packet_loss_percent", "tcp_retransmissions", "last_frame_age",
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "cause_key": {"type": "string", "enum": CAUSE_KEYS},
        "likely_cause": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "other_possible_causes": {"type": "array", "items": {"type": "string"}},
        "next_checks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "cause_key", "likely_cause", "confidence", "evidence_ids", "other_possible_causes", "next_checks"],
}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _safe_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in METRIC_KEYS:
        value = metrics.get(key)
        if key in {"profile", "video_codec", "audio_codec"}:
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,32}", value):
                result[key] = value
        elif key == "ingress_quality":
            if isinstance(value, str) and value in {"MEDIA_VALIDATED", "PUBLISHER_COUNTERS_ONLY", "UNAVAILABLE", "UNKNOWN"}:
                result[key] = value
        elif key == "resolution":
            if isinstance(value, str) and re.fullmatch(r"\d{1,5}x\d{1,5}", value):
                result[key] = value
        elif key in BOOL_METRIC_KEYS:
            if isinstance(value, bool):
                result[key] = value
        else:
            number = _number(value)
            if number is not None:
                result[key] = number
    network = metrics.get("network")
    if isinstance(network, dict):
        safe_network: dict[str, Any] = {}
        for key in NETWORK_KEYS:
            value = network.get(key)
            if key in {"provider", "tcp_state", "icmp_status"}:
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,32}", value):
                    safe_network[key] = value
            else:
                number = _number(value)
                if number is not None:
                    safe_network[key] = number
        if safe_network:
            result["network"] = safe_network
    clock = metrics.get("clock")
    if isinstance(clock, dict):
        safe_clock: dict[str, Any] = {}
        for key in CLOCK_KEYS:
            value = clock.get(key)
            if key == "ntp_synchronized" and isinstance(value, bool):
                safe_clock[key] = value
            elif key != "ntp_synchronized" and (number := _number(value)) is not None:
                safe_clock[key] = number
        if safe_clock:
            result["clock"] = safe_clock
    return result


def _safe_events(events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    result = []
    for event in events:
        if not isinstance(event, dict) or event.get("code") not in EVENT_CODES:
            continue
        safe: dict[str, Any] = {"code": event["code"]}
        severity = str(event.get("severity", "")).upper()
        if severity in {"INFO", "WARNING", "CRITICAL", "ERROR"}:
            safe["severity"] = severity
        details = event.get("details")
        if isinstance(details, dict):
            filtered = {key: value for key in EVENT_DETAIL_KEYS if (value := _number(details.get(key))) is not None}
            if filtered:
                safe["details"] = filtered
        result.append(safe)
    return result


def _has_useful_network_sample(metrics: dict[str, Any]) -> bool:
    network = metrics.get("network")
    if not isinstance(network, dict):
        return False
    age = _number(network.get("sample_age_seconds"))
    interval = _number(network.get("sample_interval_seconds"))
    if age is not None and age > max((interval or 0) * 2, 15):
        return False
    if _number(network.get("tcp_retransmissions")) is not None:
        return True
    if _number(network.get("rtt_ms")) is not None:
        return True
    return (
        _number(network.get("packet_loss_percent")) is not None
        and _number(network.get("icmp_reply_count")) is not None
        and network.get("icmp_reply_count", 0) > 0
    )


def _media_problem(sample: dict[str, Any]) -> bool:
    metrics = sample.get("metrics", {})
    codes = {event.get("code") for event in sample.get("events", [])}
    status = sample.get("status")
    return bool(
        codes & MEDIA_EVENT_CODES
        or status in {"CRITICAL", "ERROR", "STREAM_OFFLINE", "STREAM_STALLED"}
        or metrics.get("ffmpeg_running") is False
        or (_number(metrics.get("decode_errors")) or 0) > 0
        or (_number(metrics.get("last_frame_age")) or 0) >= 5
    )


def _media_is_observed(sample: dict[str, Any]) -> bool:
    metrics = sample.get("metrics", {})
    return bool(
        (metrics.get("ffmpeg_running") is not False)
        and (
            (_number(metrics.get("last_frame_age")) is not None and metrics["last_frame_age"] < 3)
            or (_number(metrics.get("fps")) is not None and metrics["fps"] > 0)
            or (_number(metrics.get("frames")) is not None and metrics["frames"] > 0)
            or (_number(metrics.get("packets")) is not None and metrics["packets"] > 0)
            or _number(metrics.get("last_media_pts")) is not None
        )
    )


def _ingress_media_is_validated(sample: dict[str, Any]) -> bool:
    metrics = sample.get("metrics", {})
    return metrics.get("ingress_quality") == "MEDIA_VALIDATED" and not _media_problem(sample)


def _cross_probe_clock_uncertain(samples: list[dict[str, Any]]) -> bool:
    if not samples:
        return True
    return any(
        not isinstance(sample.get("metrics", {}).get("clock"), dict)
        or sample["metrics"]["clock"].get("ntp_synchronized") is not True
        for sample in samples
    )


def _network_fault(sample: dict[str, Any]) -> tuple[bool, bool]:
    metrics = sample.get("metrics", {})
    network = metrics.get("network", {})
    codes = {event.get("code") for event in sample.get("events", [])}
    if codes & NETWORK_EVENT_CODES:
        return True, False
    retransmits = _number(network.get("tcp_retransmissions"))
    provider = str(network.get("provider", "")).lower()
    if retransmits is not None and retransmits > 0:
        return (False, True) if provider == "windows" else (True, False)
    loss, replies = _number(network.get("packet_loss_percent")), _number(network.get("icmp_reply_count"))
    if loss is not None and loss >= 50 and replies is not None and replies > 0:
        return True, False
    if (_number(network.get("rtt_ms")) or 0) > 100:
        return True, False
    tcp_state = str(network.get("tcp_state", "")).upper()
    sample_age = _number(network.get("sample_age_seconds"))
    sample_interval = _number(network.get("sample_interval_seconds"))
    stale = sample_age is not None and sample_age > max((sample_interval or 0) * 2, 15)
    if tcp_state and tcp_state not in {"ESTABLISHED", "ESTAB", "UNKNOWN"} and not stale:
        return True, False
    return False, False


def _episode_assessment(packet: dict[str, Any], episode: dict[str, Any]) -> dict[str, Any]:
    episode_id = episode["episode"]
    samples = [
        sample for sample in packet["observations"]
        if sample["episode"] == episode_id and abs(sample["seconds_from_incident"]) <= 15
    ]
    by_role = {role: [sample for sample in samples if sample["role"] == role] for role in ROLES}
    upstream = by_role["SOURCE"] + by_role["SERVER_INGRESS"]
    upstream_faults = [sample for sample in upstream if _media_problem(sample)]
    ingress_clean = any(_ingress_media_is_validated(sample) for sample in by_role["SERVER_INGRESS"])
    source_clean = any(not _media_problem(sample) and _media_is_observed(sample) for sample in by_role["SOURCE"])
    egress_faults = [sample for sample in by_role["SERVER_EGRESS"] if _media_problem(sample)]
    egress_clean = any(not _media_problem(sample) and _media_is_observed(sample) for sample in by_role["SERVER_EGRESS"])
    client_faults = [sample for sample in by_role["CLIENT"] if _media_problem(sample)]
    network_findings = [_network_fault(sample) for sample in by_role["CLIENT"]]
    has_network_fault = any(found for found, _unattributed in network_findings)
    has_unattributed_windows_counter = any(unattributed for _found, unattributed in network_findings)
    client_codes = {event.get("code") for sample in client_faults for event in sample.get("events", [])}
    upstream_codes = {event.get("code") for sample in upstream_faults for event in sample.get("events", [])}
    egress_codes = {event.get("code") for sample in egress_faults for event in sample.get("events", [])}
    clock_uncertain = _cross_probe_clock_uncertain(samples)

    if upstream_faults:
        matched = bool(upstream_codes & (egress_codes | client_codes))
        if clock_uncertain:
            key = "UPSTREAM_OR_SERVER_UNCONFIRMED"
            confidence = "low"
            text = "SOURCE / SERVER_INGRESS і downstream probes зафіксували однаковий код медіапомилки, але синхронізацію їхніх годинників не підтверджено. Не можна надійно встановити, чи це той самий інцидент і де він почався; upstream є точкою для перевірки, а не доведеною причиною."
        else:
            key = "SOURCE_OR_INGEST"
            confidence = "medium" if matched else "low"
            text = (
                "Медіапомилка зафіксована на SOURCE / SERVER_INGRESS і той самий симптом видно далі по тракту. "
                "Це вказує на джерело або вхід сервера, а не на окрему проблему декодера клієнта."
                if matched else
                "Медіапомилка зафіксована на SOURCE / SERVER_INGRESS. Подальше поширення цього симптому не підтверджене наявними samples."
            )
        evidence_roles = {sample["role"] for sample in upstream_faults}
    elif egress_faults:
        if ingress_clean and not clock_uncertain:
            key = "RTMP_SERVER_RESTREAM"
            confidence = "medium"
            text = "Джерело або підтверджений ingress передає медіа, а перша зафіксована медіапомилка є на SERVER_EGRESS. Це локалізує збій у RTMP server restream."
        else:
            key = "UPSTREAM_OR_SERVER_UNCONFIRMED"
            confidence = "low"
            text = "SERVER_EGRESS має медіапомилку, але одночасний валідований ingress не підтверджений; відрізнити вхідне пошкодження від помилки restream поки неможливо."
            if ingress_clean and clock_uncertain:
                text += " Годинники probes не підтверджені синхронізованими, тому ці samples не можна надійно зіставити за часом."
            elif source_clean:
                text += " SOURCE probe бачив справний потік, але між ним і SERVER_EGRESS відсутня точка спостереження SERVER_INGRESS."
        evidence_roles = {"SERVER_EGRESS", "SERVER_INGRESS", "SOURCE"}
    elif client_faults and egress_clean and has_network_fault:
        key = "NETWORK_PATH"
        confidence = "low" if clock_uncertain else "medium"
        text = "SERVER_EGRESS продовжував віддавати медіа, а клієнтська проблема збіглася з конкретною мережевою ознакою. Найімовірніше, медіа пошкоджувалось або затримувалось на шляху до клієнта."
        if clock_uncertain:
            text += " Впевненість знижена, бо синхронізацію годинників між точками не підтверджено."
        evidence_roles = {"SERVER_EGRESS", "CLIENT"}
    elif client_faults and egress_clean:
        client_has_decode_error = bool("DECODE_ERROR" in client_codes or any((_number(sample.get("metrics", {}).get("decode_errors")) or 0) > 0 for sample in client_faults))
        clean_flow_network = any(
            sample.get("metrics", {}).get("network", {}).get("provider") == "linux"
            and str(sample.get("metrics", {}).get("network", {}).get("tcp_state", "")).upper() in {"ESTABLISHED", "ESTAB"}
            and _number(sample.get("metrics", {}).get("network", {}).get("tcp_retransmissions")) == 0
            for sample in by_role["CLIENT"]
        )
        client_has_media_error = client_has_decode_error or bool(client_codes & {"PTS_REGRESSION", "PTS_JUMP", "DTS_REGRESSION", "KEYFRAME_GAP"})
        if client_has_media_error and clean_flow_network:
            key = "CLIENT_RECEIVE_OR_DECODER"
            confidence = "low" if clock_uncertain else "medium"
            text = "SERVER_EGRESS передавав медіа, на клієнті є помилка декодера або медіатаймстемпів, а доступний per-flow TCP sample не показав retransmits. Це найбільше відповідає проблемі на прийманні або декодері клієнта; мережеві counters не виключають усі мережеві причини."
            if clock_uncertain:
                text += " Впевненість знижена, бо синхронізацію годинників між точками не підтверджено."
        else:
            key = "DOWNSTREAM_PATH_UNCONFIRMED"
            confidence = "low"
            text = "SERVER_EGRESS передавав медіа, а проблема зафіксована на клієнті. Наявних даних недостатньо, щоб відрізнити втрату між сервером і клієнтом від проблеми приймання/декодера."
        if has_unattributed_windows_counter:
            text += " Windows retransmit counter є загальним для хоста й не прив'язаний до цього RTMP-з'єднання."
        evidence_roles = {"SERVER_EGRESS", "CLIENT"}
    else:
        key = "INSUFFICIENT_EVIDENCE"
        confidence = "low"
        text = "У часовому вікні інциденту не знайдено достатньо одночасних медіаспостережень, щоб визначити першу точку, де виник збій."
        evidence_roles = {sample["role"] for sample in samples}

    return {
        "episode": episode_id,
        "is_primary": bool(episode.get("is_primary")),
        "cause_key": key,
        "confidence": confidence,
        "summary": text,
        "evidence_roles": sorted(evidence_roles),
        "sample_count": len(samples),
        "clock_uncertain": clock_uncertain,
    }


def _causal_analysis(packet: dict[str, Any]) -> dict[str, Any]:
    assessments = [_episode_assessment(packet, episode) for episode in packet["episodes"]]
    primary = next((item for item in assessments if item["is_primary"]), None)
    if primary is None and assessments:
        primary = min(assessments, key=lambda item: abs(next(
            episode["seconds_from_current_incident"] for episode in packet["episodes"] if episode["episode"] == item["episode"]
        )))
    if primary is None:
        primary = {"cause_key": "INSUFFICIENT_EVIDENCE", "confidence": "low", "summary": "Недостатньо даних.", "episode": None, "evidence_roles": []}
    same_diagnosis = [item for item in packet["episodes"] if item["diagnosis"] == packet["diagnosis"]]
    repeated_findings = [item for item in assessments if item["cause_key"] == primary["cause_key"] and item["episode"] in {ep["episode"] for ep in same_diagnosis}]
    result = {**primary, "episodes_with_same_diagnosis": len(same_diagnosis), "episodes_matching_cause": len(repeated_findings)}
    if len(same_diagnosis) > 1:
        if len(repeated_findings) > 1:
            result["summary"] += (
                f" Така сама оцінка телеметрії повторилася у {len(repeated_findings)} з "
                f"{len(same_diagnosis)} схожих інцидентів; це повторюваний шаблон спостережень, "
                "а не самостійний доказ першопричини."
            )
        else:
            result["summary"] += (
                f" У {len(same_diagnosis)} схожих інцидентах оцінки відрізняються; "
                "це повторюваний симптом, причини окремих випадків можуть бути різними."
            )
    result["episode_assessments"] = assessments
    primary_episode = primary.get("episode")
    role_by_probe = {
        item["probe"]: item["role"] for item in packet["observations"]
        if item["episode"] == primary_episode
    }
    relevant_roles = set(primary.get("evidence_roles", []))
    relevant_evidence = []
    for item in packet["evidence"]:
        episode_ids = item.get("episodes", [])
        if episode_ids and primary_episode not in episode_ids:
            continue
        probe = item.get("probe")
        if probe and role_by_probe.get(probe) not in relevant_roles:
            continue
        relevant_evidence.append(item)
    result["evidence_ids"] = [item["id"] for item in relevant_evidence]
    return result


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _incident_timestamp(incident: Any) -> datetime:
    return _aware(incident.opened_at)


def build_evidence_packet(incident: Any, related: list[Any]) -> dict[str, Any]:
    """Build a compact, pseudonymized packet; never includes stream URLs, tokens, names, or logs."""
    primary_at = _incident_timestamp(incident)
    all_incidents = sorted([*related, incident], key=_incident_timestamp)
    aliases: dict[tuple[str, str], str] = {}
    role_counts: dict[str, int] = {}

    def alias_for(role_value: Any, agent_value: Any) -> str:
        role = str(role_value or "").upper()
        if role not in ROLES:
            role = "UNKNOWN"
        key = (role, str(agent_value or "unknown"))
        if key not in aliases:
            role_counts[role] = role_counts.get(role, 0) + 1
            aliases[key] = f"{role}_{role_counts[role]}"
        return aliases[key]

    episodes: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for episode_index, item in enumerate(all_incidents, start=1):
        item_at = _incident_timestamp(item)
        seconds_from_primary = round((item_at - primary_at).total_seconds(), 1)
        context = item.context if isinstance(item.context, dict) else {}
        timeline = context.get("timeline", [])
        timeline = timeline if isinstance(timeline, list) else []
        eventful = [sample for sample in timeline if isinstance(sample, dict) and (_safe_events(sample.get("events")) or str(sample.get("status", "OK")).upper() != "OK")]
        # Keep eventful points and nearby samples from every role; cap payload size.
        selected: list[dict[str, Any]] = list(eventful)
        event_times = []
        for sample in eventful:
            try:
                event_times.append(_aware(datetime.fromisoformat(str(sample["timestamp"]).replace("Z", "+00:00"))))
            except (KeyError, TypeError, ValueError):
                pass
        if event_times:
            for sample in timeline:
                if not isinstance(sample, dict) or sample in selected:
                    continue
                try:
                    sample_at = _aware(datetime.fromisoformat(str(sample["timestamp"]).replace("Z", "+00:00")))
                except (KeyError, TypeError, ValueError):
                    continue
                if min(abs((sample_at - event_at).total_seconds()) for event_at in event_times) <= 10:
                    selected.append(sample)
        for sample in selected[:80]:
            try:
                sample_at = _aware(datetime.fromisoformat(str(sample["timestamp"]).replace("Z", "+00:00")))
            except (KeyError, TypeError, ValueError):
                continue
            role = str(sample.get("role", "")).upper()
            status = str(sample.get("status", "UNKNOWN")).upper()
            observations.append({
                "episode": episode_index,
                "seconds_from_incident": round((sample_at - item_at).total_seconds(), 1),
                "seconds_from_current_incident": round((sample_at - primary_at).total_seconds(), 1),
                "probe": alias_for(role, sample.get("agent")),
                "role": role if role in ROLES else "UNKNOWN",
                "status": status if status in STATUSES else "UNKNOWN",
                "metrics": _safe_metrics(sample.get("metrics") if isinstance(sample.get("metrics"), dict) else {}),
                "events": _safe_events(sample.get("events")),
            })
        episodes.append({
            "episode": episode_index,
            "is_primary": item.id == incident.id,
            "diagnosis": str(item.diagnosis)[:64],
            "severity": str(item.severity)[:16],
            "seconds_from_current_incident": seconds_from_primary,
            "active": bool(item.active),
        })

    observations.sort(key=lambda item: item["seconds_from_current_incident"])
    # De-duplicate timeline points captured in multiple nearby incident contexts.
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in observations:
        key = (item["episode"], item["probe"], item["seconds_from_current_incident"], tuple(event["code"] for event in item["events"]))
        unique[key] = item
    observations = list(unique.values())[-160:]

    evidence: list[dict[str, Any]] = []
    def add_fact(category: str, fact: str, *, episode: int | None = None, probe: str | None = None, episodes: list[int] | None = None) -> None:
        if not fact:
            return
        existing = next((item for item in evidence if item["fact"] == fact), None)
        if existing:
            if episode is not None:
                existing["episodes"] = sorted(set(existing.get("episodes", []) + [episode]))
            if episodes:
                existing["episodes"] = sorted(set(existing.get("episodes", []) + episodes))
            return
        evidence.append({"id": f"E{len(evidence) + 1}", "category": category, "fact": fact,
                         "episodes": sorted(set(([episode] if episode is not None else []) + (episodes or []))),
                         "probe": probe})

    event_labels = {
        "FREEZE_START": "почалося завмирання відео", "DECODE_ERROR": "зафіксовано помилку декодування",
        "PTS_REGRESSION": "часова мітка відео PTS пішла назад", "DTS_REGRESSION": "часова мітка DTS пішла назад",
        "PTS_JUMP": "зафіксовано стрибок часових міток PTS", "KEYFRAME_GAP": "інтервал між ключовими кадрами завеликий",
        "TCP_RETRANSMISSION": "зафіксовано повторну передачу TCP", "TCP_RESET": "TCP-з'єднання було скинуте",
        "CONNECTION_RESET": "мережеве з'єднання було скинуте", "PACKET_LOSS": "probe зафіксував втрату пакетів",
        "RTT_SPIKE": "затримка мережі різко зросла", "STREAM_STALL": "потік перестав надходити",
        "SILENCE_START": "почалася тиша в аудіо", "AUDIO_MISSING": "відсутні аудіокадри",
    }
    for sample in observations:
        offset = sample["seconds_from_current_incident"]
        moment = f"за {abs(offset):g} с до інциденту" if offset < 0 else f"через {offset:g} с від його початку"
        metrics = sample["metrics"]
        network = metrics.get("network", {})
        def sample_fact(category: str, fact: str) -> None:
            add_fact(category, fact, episode=sample["episode"], probe=sample["probe"])
        for event in sample["events"]:
            label = event_labels.get(event["code"])
            if label:
                sample_fact("event", f"{sample['probe']} ({sample['role']}) {moment}: {label}.")
        if _number(metrics.get("decode_errors")) and metrics["decode_errors"] > 0:
            sample_fact("decoder", f"{sample['probe']}: лічильник помилок декодера = {metrics['decode_errors']}.")
        if _number(metrics.get("last_frame_age")) and metrics["last_frame_age"] >= 3:
            sample_fact("media", f"{sample['probe']}: останній відеокадр був {metrics['last_frame_age']:g} с тому.")
        if _number(network.get("tcp_retransmissions")) and network["tcp_retransmissions"] > 0:
            sample_fact("network", f"{sample['probe']}: TCP retransmissions за інтервал = {network['tcp_retransmissions']:g}.")
        if _number(network.get("packet_loss_percent")) and network["packet_loss_percent"] >= 50 and _number(network.get("icmp_reply_count")) and network["icmp_reply_count"] > 0:
            sample_fact("network", f"{sample['probe']}: ICMP-втрата {network['packet_loss_percent']:g}% при наявних відповідях.")
        if _number(network.get("rtt_ms")) and network["rtt_ms"] > 100:
            sample_fact("network", f"{sample['probe']}: RTT = {network['rtt_ms']:g} мс.")
        if network.get("tcp_state") and network["tcp_state"].upper() not in {"ESTABLISHED", "ESTAB", "UNKNOWN"}:
            sample_fact("network", f"{sample['probe']}: стан TCP = {network['tcp_state']}.")
        if _has_useful_network_sample(metrics):
            if network.get("provider") == "linux" and network.get("tcp_state", "").upper() in {"ESTABLISHED", "ESTAB"} and _number(network.get("tcp_retransmissions")) == 0:
                sample_fact("network", f"{sample['probe']}: Linux per-flow TCP snapshot showed an established socket and 0 retransmits for this interval; that snapshot does not rule out every delivery problem.")
            elif network.get("provider") == "windows" and _number(network.get("tcp_retransmissions")) == 0:
                sample_fact("network", f"{sample['probe']}: Windows host-wide retransmit counter showed 0 for this interval; it is not specific to the RTMP flow and does not prove the path was clean.")
            elif _number(network.get("rtt_ms")) is not None and network["rtt_ms"] <= 100:
                sample_fact("network", f"{sample['probe']}: ICMP RTT was {network['rtt_ms']:g} ms; this measures host reachability and does not alone prove RTMP media delivery was healthy.")
    diagnoses = [item["diagnosis"] for item in episodes]
    repeated = sum(1 for value in diagnoses if value == str(incident.diagnosis))
    if repeated > 1:
        repeated_episode_ids = [item["episode"] for item in episodes if item["diagnosis"] == str(incident.diagnosis)]
        add_fact("pattern", f"Зафіксовано {repeated} інциденти з діагнозом {incident.diagnosis} у вікні близько 10 хвилин; це повторюваний симптом.", episodes=repeated_episode_ids)
    if not any(item["role"] == "SERVER_INGRESS" for item in observations):
        add_fact("coverage", "У контексті інциденту немає спостереження SERVER_INGRESS; стан джерела на вході сервера не підтверджений.")
    if not any(item["role"] == "SERVER_EGRESS" for item in observations):
        add_fact("coverage", "У контексті інциденту немає спостереження SERVER_EGRESS; невідомо, чи сервер продовжував віддавати медіа.")
    if not any(item["role"] == "CLIENT" and _has_useful_network_sample(item["metrics"]) for item in observations):
        add_fact("coverage", "Для клієнтського probe немає придатних до оцінки мережевих метрик у цьому вікні; відсутність записів не доводить, що мережа була справною.")
    clock_samples = [item for item in observations if item["metrics"].get("clock")]
    if any(item["metrics"]["clock"].get("ntp_synchronized") is False for item in clock_samples):
        add_fact("clock", "Щонайменше один probe повідомляє, що NTP не синхронізований; порядок подій між різними probes може бути неточним.")
    elif not clock_samples or any("ntp_synchronized" not in item["metrics"]["clock"] for item in clock_samples):
        add_fact("clock", "Синхронізацію годинників probes не підтверджено; часовий порядок між хостами не є самостійним доказом причинності.")

    packet = {
        "diagnosis": str(incident.diagnosis),
        "probable_location": str(incident.probable_location)[:240],
        "severity": str(incident.severity),
        "primary_active": bool(incident.active),
        "episodes": episodes,
        "observations": observations,
        "evidence": evidence,
    }
    packet["causal_analysis"] = _causal_analysis(packet)
    return packet


def deterministic_explanation(packet: dict[str, Any]) -> dict[str, Any]:
    analysis = packet.get("causal_analysis") or _causal_analysis(packet)
    cause_key = analysis["cause_key"]
    primary_episode = analysis.get("episode")
    primary_observations = [item for item in packet["observations"] if item["episode"] == primary_episode]
    roles = {item["role"] for item in primary_observations}
    repeated_count = analysis.get("episodes_with_same_diagnosis", 0)
    summary = (
        f"Такий самий симптом зафіксовано у {repeated_count} окремих інцидентах у вибраному часовому вікні. "
        "Це показує повторюваність, але саме по собі не встановлює причину."
        if repeated_count > 1 else
        "Пояснення побудоване за телеметрією цього інциденту."
    )
    checks: list[str] = []
    if "SERVER_INGRESS" not in roles:
        checks.append("Додайте probe SERVER_INGRESS, щоб бачити, чи справні кадри доходять до RTMP-сервера.")
    if "SERVER_EGRESS" not in roles:
        checks.append("Додайте probe SERVER_EGRESS, щоб перевірити вихід потоку із сервера.")
    if not any(item["role"] == "CLIENT" and _has_useful_network_sample(item["metrics"]) for item in primary_observations):
        checks.append("Перевірте мережеві метрики клієнтського probe; порівняйте TCP/RTT та PTS у той самий час.")
    client_profiles = {item["metrics"].get("profile") for item in primary_observations if item["role"] == "CLIENT"}
    if "LIGHT" in client_profiles:
        checks.append("Увімкніть профіль DEEP на клієнтському probe: LIGHT не виконує повного декодування й не бачить частину decoder-помилок.")
    if cause_key == "SOURCE_OR_INGEST":
        checks.append("Перевірте encoder і вихідний потік на першій точці, де з'явилась та сама помилка.")
    elif cause_key == "RTMP_SERVER_RESTREAM":
        checks.append("Перевірте RTMP server logs та restream configuration у час інциденту.")
    elif cause_key == "NETWORK_PATH":
        checks.append("Порівняйте клієнтський шлях до сервера, маршрутизацію та мережеві counters у час інциденту.")
    if not checks:
        checks.append("Порівняйте перший симптом на SERVER_INGRESS, SERVER_EGRESS і CLIENT; врахуйте стан синхронізації годинників.")
    alternatives = []
    if cause_key in {"DOWNSTREAM_PATH_UNCONFIRMED", "UPSTREAM_OR_SERVER_UNCONFIRMED", "INSUFFICIENT_EVIDENCE"}:
        alternatives = ["Плеєр або декодер клієнта", "Проблема доставки, яку не зафіксували доступні мережеві counters", "Аномалія PTS/DTS у потоці"]
    return {
        "summary": summary,
        "cause_key": cause_key,
        "likely_cause": analysis["summary"],
        "confidence": analysis["confidence"],
        "evidence_ids": analysis.get("evidence_ids", []),
        "other_possible_causes": alternatives,
        "next_checks": checks,
    }

def _response_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                return content["text"]
    raise ValueError("OpenAI response did not contain structured output")


def openai_explanation(packet: dict[str, Any], api_key: str, model: str, timeout: float = 30.0) -> dict[str, Any]:
    evidence_by_id = {item["id"]: item for item in packet["evidence"]}
    system = (
        "Ти пояснюєш інциденти RTMP-моніторингу українською. Встановлюй причинність лише з переданих фактів; "
        "не називай гіпотезу доведеною. Поясни, що сталося, які докази це підтверджують, чого бракує і що перевірити. "
        "Часи відносні до інциденту. Якщо годинники probe не синхронізовані або статус невідомий, не використовуй "
        "порядок подій між різними хостами як сильний доказ причини. Відсутні метрики не означають норму. Не вигадуй дані. "
        "Поле causal_analysis є детермінованим висновком рушія: збережи його cause_key і не посилюй його впевненість. "
        "У evidence_ids поверни тільки ID переданих фактів, на які прямо спирається пояснення."
    )
    payload = {
        "model": model,
        "store": False,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps(packet, ensure_ascii=False, allow_nan=False)}]},
        ],
        "text": {"format": {"type": "json_schema", "name": "rtmp_incident_explanation", "strict": True, "schema": SCHEMA}},
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"OpenAI API returned HTTP {exc.code}") from None
    except OSError:
        raise RuntimeError("Could not reach the OpenAI API") from None
    try:
        result = json.loads(_response_text(response_data))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenAI returned an invalid explanation") from exc
    if not isinstance(result, dict) or any(not isinstance(result.get(key), str) for key in ("summary", "cause_key", "likely_cause", "confidence")):
        raise RuntimeError("OpenAI returned an incomplete explanation")
    analysis = packet.get("causal_analysis") or _causal_analysis(packet)
    if result["cause_key"] != analysis["cause_key"]:
        raise RuntimeError("OpenAI explanation disagreed with the evidence-based diagnosis")
    if result["confidence"] not in {"low", "medium", "high"}:
        result["confidence"] = "low"
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    if confidence_rank[result["confidence"]] > confidence_rank[analysis["confidence"]]:
        result["confidence"] = analysis["confidence"]
    for key in ("evidence_ids", "other_possible_causes", "next_checks"):
        if not isinstance(result.get(key), list) or any(not isinstance(value, str) for value in result[key]):
            result[key] = []
    allowed_evidence = set(analysis.get("evidence_ids", []))
    result["evidence_ids"] = list(dict.fromkeys(value for value in result["evidence_ids"] if value in evidence_by_id and value in allowed_evidence))
    result["evidence"] = [evidence_by_id[value] for value in result["evidence_ids"]]
    result["ai_generated"] = True
    result["model"] = model
    return result

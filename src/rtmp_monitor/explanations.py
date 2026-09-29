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
METRIC_KEYS = {
    "profile", "ffmpeg_running", "last_frame_age", "last_audio_frame_age", "decode_errors", "fps",
    "resolution", "video_codec", "audio_codec", "bitrate", "last_keyframe_age", "current_gop_duration",
    "current_gop_frames", "expected_gop_frames", "expected_gop_seconds", "last_media_pts", "last_media_dts",
    "reconnect_count",
}
CLOCK_KEYS = {"ntp_synchronized", "estimated_offset_ms", "central_offset_ms", "central_offset_uncertainty_ms"}
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
        "likely_cause": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "other_possible_causes": {"type": "array", "items": {"type": "string"}},
        "next_checks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "likely_cause", "confidence", "evidence_ids", "other_possible_causes", "next_checks"],
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
        elif key == "resolution":
            if isinstance(value, str) and re.fullmatch(r"\d{1,5}x\d{1,5}", value):
                result[key] = value
        elif key == "ffmpeg_running":
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
            "diagnosis": str(item.diagnosis)[:64],
            "severity": str(item.severity)[:16],
            "seconds_from_current_incident": seconds_from_primary,
            "active": bool(item.active),
        })

    observations.sort(key=lambda item: item["seconds_from_current_incident"])
    # De-duplicate timeline points captured in multiple nearby incident contexts.
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in observations:
        key = (item["probe"], item["seconds_from_current_incident"], tuple(event["code"] for event in item["events"]))
        unique[key] = item
    observations = list(unique.values())[-160:]

    evidence: list[dict[str, str]] = []
    def add_fact(category: str, fact: str) -> None:
        if fact and all(entry["fact"] != fact for entry in evidence):
            evidence.append({"id": f"E{len(evidence) + 1}", "category": category, "fact": fact})

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
        for event in sample["events"]:
            label = event_labels.get(event["code"])
            if label:
                add_fact("event", f"{sample['probe']} ({sample['role']}) {moment}: {label}.")
        if _number(metrics.get("decode_errors")) and metrics["decode_errors"] > 0:
            add_fact("decoder", f"{sample['probe']}: лічильник помилок декодера = {metrics['decode_errors']}.")
        if _number(metrics.get("last_frame_age")) and metrics["last_frame_age"] >= 3:
            add_fact("media", f"{sample['probe']}: останній відеокадр був {metrics['last_frame_age']:g} с тому.")
        if _number(network.get("tcp_retransmissions")) and network["tcp_retransmissions"] > 0:
            add_fact("network", f"{sample['probe']}: TCP retransmissions за інтервал = {network['tcp_retransmissions']:g}.")
        if _number(network.get("packet_loss_percent")) and network["packet_loss_percent"] >= 50 and _number(network.get("icmp_reply_count")) and network["icmp_reply_count"] > 0:
            add_fact("network", f"{sample['probe']}: ICMP-втрата {network['packet_loss_percent']:g}% при наявних відповідях.")
        if _number(network.get("rtt_ms")) and network["rtt_ms"] > 100:
            add_fact("network", f"{sample['probe']}: RTT = {network['rtt_ms']:g} мс.")
        if network.get("tcp_state") and network["tcp_state"].upper() not in {"ESTABLISHED", "ESTAB", "UNKNOWN"}:
            add_fact("network", f"{sample['probe']}: стан TCP = {network['tcp_state']}.")
        if _has_useful_network_sample(metrics):
            if network.get("provider") == "linux" and network.get("tcp_state", "").upper() in {"ESTABLISHED", "ESTAB"} and _number(network.get("tcp_retransmissions")) == 0:
                add_fact("network", f"{sample['probe']}: Linux per-flow TCP snapshot showed an established socket and 0 retransmits for this interval; that snapshot does not rule out every delivery problem.")
            elif network.get("provider") == "windows" and _number(network.get("tcp_retransmissions")) == 0:
                add_fact("network", f"{sample['probe']}: Windows host-wide retransmit counter showed 0 for this interval; it is not specific to the RTMP flow and does not prove the path was clean.")
            elif _number(network.get("rtt_ms")) is not None and network["rtt_ms"] <= 100:
                add_fact("network", f"{sample['probe']}: ICMP RTT was {network['rtt_ms']:g} ms; this measures host reachability and does not alone prove RTMP media delivery was healthy.")
    diagnoses = [item["diagnosis"] for item in episodes]
    repeated = sum(1 for value in diagnoses if value == str(incident.diagnosis))
    if repeated > 1:
        add_fact("pattern", f"Зафіксовано {repeated} інциденти з діагнозом {incident.diagnosis} у вікні близько 10 хвилин; це повторюваний симптом.")
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

    return {
        "diagnosis": str(incident.diagnosis),
        "probable_location": str(incident.probable_location)[:240],
        "severity": str(incident.severity),
        "primary_active": bool(incident.active),
        "episodes": episodes,
        "observations": observations,
        "evidence": evidence,
    }


def deterministic_explanation(packet: dict[str, Any]) -> dict[str, Any]:
    diagnosis = packet["diagnosis"]
    codes = {event["code"] for row in packet["observations"] for event in row["events"]}
    evidence_ids = [item["id"] for item in packet["evidence"]]
    coverage = any(item["category"] == "coverage" for item in packet["evidence"])
    if diagnosis in {"CLIENT_PROBLEM", "CLIENT_PATH_UNCONFIRMED", "NETWORK_PATH_UNCONFIRMED"}:
        summary = "Клієнтський probe неодноразово зафіксував проблему відтворення. Це підтверджує збій на шляху до клієнта або під час обробки потоку, але саме по собі не визначає одну точну причину."
        if "DECODE_ERROR" in codes:
            cause = "Є прямий доказ помилки декодера на клієнтському probe. Потрібно звірити, чи бачить такі самі помилки серверний egress: це відрізнить проблемний вхідний потік від клієнтського декодера."
            confidence = "medium"
        elif codes & {"PTS_REGRESSION", "DTS_REGRESSION", "PTS_JUMP"}:
            cause = "На клієнтському probe є аномалія часових міток медіа. Це може ламати відтворення; без одночасного порівняння з server egress не можна сказати, чи аномалія прийшла з потоком, чи виникла далі по шляху."
            confidence = "medium"
        elif codes & {"TCP_RETRANSMISSION", "TCP_RESET", "CONNECTION_RESET", "PACKET_LOSS", "RTT_SPIKE"}:
            cause = "Є мережеві ознаки, які могли завадити доставці медіа до клієнта. Вони підтверджують проблемний шлях, але не встановлюють, чи джерело проблеми — сервер, маршрут або клієнтська мережа."
            confidence = "medium"
        else:
            cause = "Дані підтверджують симптом на клієнті, але не містять прямої ознаки, що відрізняє декодер/плеєр від непоміченої мережевої проблеми. Точну першопричину зараз не встановлено."
            confidence = "low"
    elif diagnosis == "NETWORK_PATH_PROBLEM":
        summary = "Є мережеві ознаки, синхронізовані з проблемою відтворення."
        cause = "Найімовірніше, медіа затримується або втрачається на мережевому шляху між server egress і клієнтом."
        confidence = "medium"
    elif diagnosis == "SOURCE_OR_INGEST_PROBLEM":
        summary = "Перші зафіксовані симптоми виникли на джерелі або вході сервера."
        cause = "Ймовірна причина розташована до server egress: encoder, вихідний RTMP або ingress сервера."
        confidence = "medium"
    else:
        summary = "Телеметрія локалізувала симптом, але наявних точок спостереження недостатньо для точного розділення можливих причин."
        cause = str(packet["probable_location"])
        confidence = "low"

    if coverage:
        summary += " Частина потрібних точок або мережевих метрик відсутня, тому цей висновок не варто вважати підтвердженою першопричиною."
    checks = []
    roles = {item["role"] for item in packet["observations"]}
    if "SERVER_INGRESS" not in roles:
        checks.append("Додайте probe SERVER_INGRESS, щоб бачити, чи справні кадри доходять до RTMP-сервера.")
    if "SERVER_EGRESS" not in roles:
        checks.append("Додайте probe SERVER_EGRESS, щоб перевірити вихід потоку із сервера.")
    if not any(item["role"] == "CLIENT" and _has_useful_network_sample(item["metrics"]) for item in packet["observations"]):
        checks.append("Перевірте, що у клієнтського probe ввімкнені network metrics; за можливості порівняйте TCP/RTT та PTS у той самий час.")
    client_profiles = {item["metrics"].get("profile") for item in packet["observations"] if item["role"] == "CLIENT"}
    if "LIGHT" in client_profiles:
        checks.append("Для клієнтського probe увімкніть профіль DEEP: LIGHT не виконує повне декодування і не бачить частину decoder-помилок.")
    if not checks:
        checks.append("Відкрийте timeline інциденту та порівняйте перший симптом на SERVER_INGRESS, SERVER_EGRESS і CLIENT з урахуванням попередження про синхронізацію годинників.")
    alternatives = []
    if diagnosis.startswith("CLIENT") or diagnosis.startswith("NETWORK_PATH"):
        alternatives = ["Плеєр або декодер клієнта", "Проблема доставки, яку не зафіксували доступні мережеві counters", "Аномалія PTS/DTS у потоці (якщо її також бачить server egress)"]
    return {
        "summary": summary,
        "likely_cause": cause,
        "confidence": confidence,
        "evidence_ids": evidence_ids,
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
    if not isinstance(result, dict) or any(not isinstance(result.get(key), str) for key in ("summary", "likely_cause", "confidence")):
        raise RuntimeError("OpenAI returned an incomplete explanation")
    if result["confidence"] not in {"low", "medium", "high"}:
        result["confidence"] = "low"
    for key in ("evidence_ids", "other_possible_causes", "next_checks"):
        if not isinstance(result.get(key), list) or any(not isinstance(value, str) for value in result[key]):
            result[key] = []
    result["evidence_ids"] = list(dict.fromkeys(value for value in result["evidence_ids"] if value in evidence_by_id))
    result["evidence"] = [evidence_by_id[value] for value in result["evidence_ids"]]
    result["ai_generated"] = True
    result["model"] = model
    return result

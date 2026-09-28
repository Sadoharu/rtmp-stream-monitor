from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .config import AgentFileConfig, SrsApiConfig, StreamConfig
from .queue import LocalQueue

LOG = logging.getLogger("rtmp_monitor.srs_ingress")
SRS_PAGE_SIZE = 500
SRS_MAX_STREAM_ROWS = 100_000


class SrsApiClient:
    """Read-only client for the SRS HTTP API stream statistics endpoint."""

    def __init__(self, config: SrsApiConfig, stream: StreamConfig):
        self.config = config
        self.stream = stream
        parsed = urllib.parse.urlsplit(stream.url)
        if parsed.scheme.lower() not in {"rtmp", "rtmps"}:
            raise ValueError("SERVER_INGRESS stream URL must use rtmp:// or rtmps://")
        parts = parsed.path.strip("/").split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("SERVER_INGRESS stream URL must include an RTMP app and stream name")
        self.app, self.name = parts
        query = urllib.parse.parse_qs(parsed.query)
        self.vhost = query.get("vhost", [None])[0]
        self.base_url = str(config.base_url).rstrip("/")

    def _get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.config.username is not None and self.config.password is not None:
            credentials = f"{self.config.username}:{self.config.password}".encode("utf-8")
            request.add_header("Authorization", "Basic " + base64.b64encode(credentials).decode("ascii"))
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("SRS HTTP API returned a non-object JSON response")
        code = payload.get("code", 0)
        if code != 0:
            raise RuntimeError(f"SRS HTTP API returned error code {code}")
        return payload

    def find_stream(self) -> tuple[dict[str, Any] | None, str | None]:
        start = 0
        server_id: str | None = None
        while start < SRS_MAX_STREAM_ROWS:
            url = f"{self.base_url}/api/v1/streams/?start={start}&count={SRS_PAGE_SIZE}"
            payload = self._get_json(url)
            raw_streams = payload.get("streams")
            if raw_streams is None and isinstance(payload.get("data"), dict):
                raw_streams = payload["data"].get("streams")
            if not isinstance(raw_streams, list):
                raise RuntimeError("SRS HTTP API response has no streams array")
            if server_id is None and payload.get("server") is not None:
                server_id = str(payload["server"])
            matches = [
                row for row in raw_streams
                if isinstance(row, dict)
                and str(row.get("app", "")) == self.app
                and str(row.get("name", "")) == self.name
                and (self.vhost is None or str(row.get("vhost", "")) == self.vhost)
            ]
            if matches:
                active = next((row for row in matches if (row.get("publish") or {}).get("active") is True), None)
                return active or matches[0], server_id
            total = payload.get("total")
            if not raw_streams or len(raw_streams) < SRS_PAGE_SIZE:
                break
            if isinstance(total, int) and start + len(raw_streams) >= total:
                break
            start += len(raw_streams)
        if start >= SRS_MAX_STREAM_ROWS:
            raise RuntimeError(f"SRS stream list exceeded the {SRS_MAX_STREAM_ROWS}-row search limit")
        return None, server_id


class SrsIngressProbe:
    """Poll SRS publisher counters as an ingress observation, without decoding media."""

    def __init__(
        self,
        stream: StreamConfig,
        config: AgentFileConfig,
        outbox: LocalQueue,
        shared_metrics: Callable[[], dict[str, Any]] | None = None,
    ):
        if config.srs_api is None:
            raise ValueError("SERVER_INGRESS requires srs_api configuration")
        self.stream = stream
        self.config = config
        self.outbox = outbox
        self.shared_metrics = shared_metrics
        self.client = SrsApiClient(config.srs_api, stream)
        self._server_id: str | None = None
        self._counters: dict[str, int] | None = None
        self._last_progress_mono: float | None = None
        self._last_api_warning_mono = 0.0
        self._condition: str | None = None

    async def run(self) -> None:
        interval = self.config.monitoring.heartbeat_interval
        while True:
            now = time.monotonic()
            try:
                row, server_id = await asyncio.to_thread(self.client.find_stream)
                sample = self._sample(row, server_id, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sample = self._api_unavailable(exc)
                if now - self._last_api_warning_mono >= 30:
                    LOG.warning("SRS ingress API probe failed for stream %s: %s", self.stream.id, exc)
                    self._last_api_warning_mono = now
            try:
                self.outbox.put(sample)
            except Exception:
                LOG.exception("Unable to cache SRS ingress telemetry for stream %s", self.stream.id)
            await asyncio.sleep(interval)

    def _sample(self, row: dict[str, Any] | None, server_id: str | None, now: float) -> dict[str, Any]:
        changed_server = server_id is not None and self._server_id is not None and server_id != self._server_id
        self._server_id = server_id or self._server_id
        if changed_server:
            self._counters = None
            self._last_progress_mono = None
        if row is None:
            self._last_progress_mono = None
            return self._report(
                status="STREAM_OFFLINE",
                metrics={"srs_api_available": True, "ingress_active": False, "ingress_quality": "PUBLISHER_COUNTERS_ONLY"},
                condition="STREAM_OFFLINE",
                event={"code": "STREAM_OFFLINE", "severity": "CRITICAL", "details": {"source": "SRS HTTP API", "stream": self.stream.id}},
            )

        publish = row.get("publish") if isinstance(row.get("publish"), dict) else {}
        active = publish.get("active")
        if not isinstance(active, bool):
            return self._report(
                status="WARNING",
                metrics={"srs_api_available": True, "ingress_active": None, "ingress_quality": "PUBLISHER_COUNTERS_ONLY", "ingress_observation_issue": "SRS response has no boolean publish.active"},
                condition="SRS_PUBLISH_STATE_UNAVAILABLE",
                event={"code": "SRS_PUBLISH_STATE_UNAVAILABLE", "severity": "WARNING", "details": {"message": "SRS response has no boolean publish.active"}},
            )
        if not active:
            self._last_progress_mono = None
            return self._report(
                status="STREAM_OFFLINE",
                metrics=self._metrics(row, server_id, active=False, now=now),
                condition="STREAM_OFFLINE",
                event={"code": "STREAM_OFFLINE", "severity": "CRITICAL", "details": {"source": "SRS HTTP API", "stream": self.stream.id}},
            )

        counters = self._read_counters(row)
        progressed = self._counters is None or any(counters[key] > self._counters.get(key, counters[key]) for key in counters)
        reset = self._counters is not None and any(counters[key] < self._counters.get(key, counters[key]) for key in counters)
        if progressed or reset or self._last_progress_mono is None:
            self._last_progress_mono = now
        self._counters = counters or self._counters
        metrics = self._metrics(row, server_id, active=True, now=now)
        metrics["ingress_progress_observable"] = bool(counters)
        if self._last_progress_mono is not None:
            metrics["last_ingress_progress_age"] = round(max(0.0, now - self._last_progress_mono), 3)
        if not counters:
            metrics["ingress_observation_issue"] = "SRS response has no frame or receive-byte counters"
            return self._report(
                status="WARNING",
                metrics=metrics,
                condition="SRS_COUNTERS_UNAVAILABLE",
                event={"code": "SRS_COUNTERS_UNAVAILABLE", "severity": "WARNING", "details": {"message": "SRS response has no advancing frame or receive-byte counters"}},
            )
        age = now - self._last_progress_mono if self._last_progress_mono is not None else 0.0
        if age >= self.config.monitoring.stall_threshold:
            return self._report(
                status="STREAM_STALLED",
                metrics=metrics,
                condition="STREAM_STALL",
                event={"code": "STREAM_STALL", "severity": "CRITICAL", "details": {"source": "SRS HTTP API", "seconds_without_ingress_progress": round(age, 3), "threshold_seconds": self.config.monitoring.stall_threshold}},
            )
        return self._report(status="OK", metrics=metrics, condition=None, event=None)

    def _api_unavailable(self, exc: Exception) -> dict[str, Any]:
        return self._report(
            status="WARNING",
            metrics={"srs_api_available": False, "ingress_active": None, "ingress_quality": "UNAVAILABLE", "ingress_observation_issue": str(exc)[:300]},
            condition="SRS_API_UNAVAILABLE",
            event={"code": "SRS_API_UNAVAILABLE", "severity": "WARNING", "details": {"message": str(exc)[:300]}},
        )

    @staticmethod
    def _read_counters(row: dict[str, Any]) -> dict[str, int]:
        counters: dict[str, int] = {}
        for key in ("recv_bytes", "frames", "video_frames", "audio_frames"):
            value = row.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                counters[key] = int(value)
        return counters

    def _metrics(self, row: dict[str, Any], server_id: str | None, active: bool, now: float) -> dict[str, Any]:
        video = row.get("video") if isinstance(row.get("video"), dict) else {}
        audio = row.get("audio") if isinstance(row.get("audio"), dict) else {}
        kbps = row.get("kbps") if isinstance(row.get("kbps"), dict) else {}
        publish = row.get("publish") if isinstance(row.get("publish"), dict) else {}
        width, height = video.get("width"), video.get("height")
        resolution = f"{width}x{height}" if isinstance(width, int) and isinstance(height, int) else None
        metrics: dict[str, Any] = {
            "profile": "SRS_API",
            "srs_api_available": True,
            "srs_server_id": server_id,
            "ingress_quality": "PUBLISHER_COUNTERS_ONLY",
            "ingress_media_decode_validated": False,
            "ingress_active": active,
            "ingress_publish_client_id": publish.get("cid"),
            "ingress_clients": row.get("clients"),
            "ingress_frames": row.get("frames"),
            "ingress_video_frames": row.get("video_frames"),
            "ingress_audio_frames": row.get("audio_frames"),
            "ingress_recv_bytes": row.get("recv_bytes"),
            "ingress_recv_kbps_30s": kbps.get("recv_30s"),
            "ingress_send_kbps_30s": kbps.get("send_30s"),
            "video_codec": video.get("codec"),
            "audio_codec": audio.get("codec"),
            "resolution": resolution,
        }
        if self.shared_metrics:
            metrics.update(self.shared_metrics())
        return metrics

    def _report(self, status: str, metrics: dict[str, Any], condition: str | None, event: dict[str, Any] | None) -> dict[str, Any]:
        events = []
        if condition != self._condition and event is not None:
            events.append({**event, "timestamp": datetime.now(timezone.utc).isoformat()})
        elif self._condition is not None and condition is None:
            events.append({"code": "INGRESS_RECOVERED", "severity": "INFO", "details": {"previous_condition": self._condition}, "timestamp": datetime.now(timezone.utc).isoformat()})
        self._condition = condition
        return {
            "sample_id": str(uuid.uuid4()),
            "stream_id": self.stream.id,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "metrics": metrics,
            "events": events,
            "context": {},
        }

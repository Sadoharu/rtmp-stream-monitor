from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from .analyzer import FrameAnalyzer, parse_diagnostic_line
from .config import AgentFileConfig, StreamConfig
from .network import NetworkTelemetry, clock_status
from .queue import LocalQueue
from .srs_ingress import SrsIngressProbe

LOG = logging.getLogger("rtmp_monitor.agent")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StreamProbe:
    def __init__(
        self,
        stream: StreamConfig,
        config: AgentFileConfig,
        outbox: LocalQueue,
        shared_metrics: Callable[[], dict[str, Any]] | None = None,
    ):
        self.stream = stream
        self.config = config
        self.outbox = outbox
        self.shared_metrics = shared_metrics
        self.process: asyncio.subprocess.Process | None = None
        self._process_stats: psutil.Process | None = None
        self.started_mono = time.monotonic()
        self.last_progress_mono: float | None = None
        self.last_packet_mono: float | None = None
        self.last_frame_mono: float | None = None
        self.last_audio_frame_mono: float | None = None
        self.last_video_packet_mono: float | None = None
        self.last_progress: dict[str, str] = {}
        self.last_progress_frame = 0
        self.analyzer = FrameAnalyzer(config.monitoring.keyframe_gap_threshold)
        self.stderr_tail: deque[str] = deque(maxlen=200)
        self.pending_events: list[dict[str, Any]] = []
        self.active_events: dict[str, dict[str, Any]] = {}
        self.last_event_mono: dict[str, float] = {}
        self.reconnect_count = 0
        self.last_restart_reason = "initial start"
        self.process_cpu = 0.0
        self.process_rss = 0
        self.stream_metadata: dict[str, Any] = {}
        self._event_ids: set[str] = set()
        self._dts_by_stream: dict[str, float] = {}
        self.decode_error_count = 0
        self._reading_input_metadata = False
        self._video_stream_indices: set[str] = set()
        self._audio_stream_indices: set[str] = set()
        self.packet_count = 0
        self._packet_bytes: deque[tuple[float, int]] = deque()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def command(self) -> list[str]:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if self.config.agent.profile == "LIGHT":
            if not ffprobe:
                raise RuntimeError("ffprobe was not found on PATH")
            return [
                ffprobe, "-hide_banner", "-v", "info", "-rw_timeout", "15000000",
                "-show_packets", "-show_entries", "packet=stream_index,pts_time,dts_time,flags,size",
                "-of", "compact=p=0:nk=0", self.stream.url,
            ]
        if not ffmpeg:
            raise RuntimeError("ffmpeg was not found on PATH")
        interval = str(self.config.monitoring.progress_interval)
        return [
            ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info", "-debug_ts",
            "-progress", "pipe:1", "-stats_period", interval,
            "-rw_timeout", "15000000", "-i", self.stream.url,
            "-map", "0:v?", "-map", "0:a?",
            "-vf", f"freezedetect=n=-60dB:d={self.config.monitoring.freeze_threshold},showinfo",
            "-af", f"silencedetect=n=-50dB:d={self.config.monitoring.silence_threshold},ashowinfo",
            "-f", "null", "-",
        ]

    async def run(self) -> None:
        delay = self.config.monitoring.reconnect_initial
        maximum = self.config.monitoring.reconnect_max
        while True:
            try:
                await self._run_one()
            except asyncio.CancelledError:
                await self._stop_process()
                raise
            except Exception as exc:
                LOG.exception("Probe failed for stream %s", self.stream.id)
                self._event("PROBE_ERROR", "CRITICAL", {"message": str(exc)})
                self.last_restart_reason = str(exc)
            self.reconnect_count += 1
            self._event("FFMPEG_RESTART", "WARNING", {"reason": self.last_restart_reason, "restart_count": self.reconnect_count})
            self._publish(self._snapshot(time.monotonic()))
            if time.monotonic() - self.started_mono >= 60:
                delay = self.config.monitoring.reconnect_initial
            await asyncio.sleep(delay)
            delay = min(maximum, delay * 2)

    async def _run_one(self) -> None:
        self._reset_connection_state()
        cmd = self.command()
        LOG.info("Starting %s probe for stream %s", self.config.agent.profile, self.stream.id)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        self._attach_process_stats()
        self.started_mono = time.monotonic()
        for code in ("FFMPEG_DEAD", "STREAM_STALL", "PROGRESS_STALE"):
            self.active_events.pop(code, None)
        self.last_progress_mono = self.started_mono
        self.last_packet_mono = self.started_mono
        self.last_frame_mono = None
        assert self.process.stdout is not None and self.process.stderr is not None
        readers = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
        wait_task = asyncio.create_task(self.process.wait())
        monitor_task = asyncio.create_task(self._monitor_loop())
        cancelled = False
        try:
            await asyncio.wait([wait_task], return_when=asyncio.FIRST_COMPLETED)
            self.last_restart_reason = f"monitor subprocess exited with code {self.process.returncode}"
            if self.process.returncode not in (0, None):
                self._event("FFMPEG_EXIT", "CRITICAL", {"return_code": self.process.returncode, "stderr_tail": list(self.stderr_tail)[-20:]})
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            monitor_task.cancel()
            if self.process and self.process.returncode is None:
                await self._stop_process()
            if cancelled:
                try:
                    await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=2)
                except asyncio.TimeoutError:
                    for task in readers:
                        task.cancel()
            await asyncio.gather(wait_task, monitor_task, *readers, return_exceptions=True)
            if self.process and self.process.returncode is not None:
                self._process_stats = None
                self.process_cpu = 0.0
                self.process_rss = 0

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        if self.config.agent.profile == "LIGHT":
            async for raw in self.process.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._handle_packet_line(line)
        else:
            progress: dict[str, str] = {}
            async for raw in self.process.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key == "progress":
                    self.last_progress = progress
                    self.last_progress_mono = time.monotonic()
                    try:
                        self.last_progress_frame = int(progress.get("frame", self.last_progress_frame))
                    except ValueError:
                        pass
                    if progress.get("fps"):
                        self.stream_metadata["fps"] = _number(progress["fps"])
                    if progress.get("bitrate"):
                        self.stream_metadata["bitrate"] = progress["bitrate"]
                    if progress.get("out_time"):
                        self.stream_metadata["media_time"] = progress["out_time"]
                    self.active_events.pop("PROGRESS_STALE", None)
                    progress = {}
                else:
                    progress[key] = value

    def _handle_packet_line(self, line: str) -> None:
        now = time.monotonic()
        self.last_packet_mono = now
        self.last_progress_mono = now
        self.packet_count += 1
        fields = dict(piece.split("=", 1) for piece in line.split("|" if "|" in line else " ") if "=" in piece)
        stream_index = fields.get("stream_index", "0")
        is_video_packet = stream_index in self._video_stream_indices if self._video_stream_indices else (stream_index not in self._audio_stream_indices if self._audio_stream_indices else True)
        if stream_index in self._audio_stream_indices:
            self.last_audio_frame_mono = now
            self.active_events.pop("AUDIO_MISSING", None)
        if is_video_packet:
            self.last_video_packet_mono = now
            self.last_frame_mono = now
            self.active_events.pop("STREAM_STALL", None)
            self.active_events.pop("PROGRESS_STALE", None)
            self.analyzer.frame_count += 1
            if self.analyzer.first_frame_mono is None:
                self.analyzer.first_frame_mono = now
            if "K" in fields.get("flags", ""):
                self._packet_keyframe(now, fields)
        pts = _float_or_none(fields.get("pts_time"))
        dts = _float_or_none(fields.get("dts_time"))
        if pts is not None:
            if is_video_packet:
                self.stream_metadata["pts_time"] = pts
                previous_pts = self.stream_metadata.get(f"pts_{stream_index}")
                # Encoded packet PTS can move backwards in decode order when B-frames are present.
                # Deep mode checks presentation-order PTS on decoded frames instead.
                if previous_pts is not None and pts - previous_pts > 10:
                    self._event("PTS_JUMP", "WARNING", {"stream_index": stream_index, "delta_seconds": round(pts - previous_pts, 4)})
                self.stream_metadata[f"pts_{stream_index}"] = pts
        if dts is not None:
            self.stream_metadata["dts_time"] = dts
            self.stream_metadata["last_media_dts"] = dts
            previous_dts = self._dts_by_stream.get(stream_index)
            if previous_dts is not None and dts < previous_dts - 0.001:
                self._event("DTS_REGRESSION", "WARNING", {"stream_index": stream_index, "previous_dts": previous_dts, "dts": dts})
            self._dts_by_stream[stream_index] = dts
        if fields.get("size"):
            size = _float_or_none(fields["size"])
            if size is not None:
                self.stream_metadata["last_packet_bytes"] = size
                self._packet_bytes.append((now, int(size)))
                while self._packet_bytes and now - self._packet_bytes[0][0] > 10:
                    self._packet_bytes.popleft()

    def _packet_keyframe(self, now: float, details: dict[str, str]) -> None:
        # Packet/key flags in LIGHT mode identify key packets, not codec-level IDR certainty.
        self.analyzer.last_frame_mono = now
        self.analyzer.last_frame_type = None
        self.analyzer.last_frame_is_keyframe = "K" in details.get("flags", "")
        if self.analyzer.last_keyframe_mono is not None:
            self.analyzer.keyframes.append((self.analyzer.last_keyframe_mono, now))
        if self.analyzer.last_keyframe_frame_count is not None:
            self.analyzer.gop_lengths.append(self.analyzer.frame_count - self.analyzer.last_keyframe_frame_count)
        pts = _float_or_none(details.get("pts_time"))
        if pts is not None:
            if self.analyzer.last_keyframe_pts is not None:
                self.analyzer.keyframe_pts_intervals.append(pts - self.analyzer.last_keyframe_pts)
            self.analyzer.last_keyframe_pts = pts
        self.analyzer.last_keyframe_frame_count = self.analyzer.frame_count
        self.analyzer.last_keyframe_mono = now
        self.analyzer.keyframe_count += 1

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        async for raw in self.process.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            if "showinfo" not in line and "ashowinfo" not in line and not line.startswith("[Parsed_") and not line.startswith("demuxer ->"):
                self.stderr_tail.append(line)
                LOG_FFMPEG.info("%s", line, extra={"stream": self.stream.id, "agent": self.config.agent.name})
            self._parse_metadata(line)
            now = time.monotonic()
            if "showinfo" in line:
                events = self.analyzer.feed(line, now)
                if self.analyzer.last_frame_mono is not None:
                    self.last_frame_mono = self.analyzer.last_frame_mono
                    self.active_events.pop("STREAM_STALL", None)
                for event in events:
                    self._accept_event(event)
            if "ashowinfo" in line:
                audio = re.search(r"pts_time:\s*(-?\d+(?:\.\d+)?)", line)
                self.last_audio_frame_mono = now
                self.active_events.pop("AUDIO_MISSING", None)
                if audio and self.analyzer.last_pts is not None:
                    delta = float(audio.group(1)) - self.analyzer.last_pts
                    self.stream_metadata["av_timestamp_delta_seconds"] = round(delta, 4)
                    if abs(delta) > 1.0:
                        self._event_throttled("AV_TIMESTAMP_DRIFT", "WARNING", {"delta_seconds": round(delta, 4)}, 10)
            event = parse_diagnostic_line(line)
            if event:
                self._accept_event(event)
            self._parse_debug_timestamps(line)

    def _parse_metadata(self, line: str) -> None:
        if line.startswith("Input #"):
            self._reading_input_metadata = True
            return
        if line.startswith("Output #"):
            self._reading_input_metadata = False
            return
        if not self._reading_input_metadata:
            return
        if "Video:" in line:
            index = re.search(r"Stream #\d+:(\d+):\s*Video:", line)
            if index:
                self._video_stream_indices.add(index.group(1))
            codec = re.search(r"Video:\s*([^, ]+)", line)
            size = re.search(r"\b(\d{2,5}x\d{2,5})\b", line)
            fps = re.search(r"\b(\d+(?:\.\d+)?)\s*fps", line)
            if codec:
                self.stream_metadata["video_codec"] = codec.group(1)
            if size:
                self.stream_metadata["resolution"] = size.group(1)
            if fps:
                self.stream_metadata["source_fps"] = float(fps.group(1))
        if "Audio:" in line:
            index = re.search(r"Stream #\d+:(\d+):\s*Audio:", line)
            if index:
                self._audio_stream_indices.add(index.group(1))
            codec = re.search(r"Audio:\s*([^, ]+)", line)
            if codec:
                self.stream_metadata["audio_codec"] = codec.group(1)

    def _parse_debug_timestamps(self, line: str) -> None:
        if not line.startswith("demuxer ->"):
            return
        stream_match = re.search(r"ist_index:(\d+)", line)
        stream_index = stream_match.group(1) if stream_match else "0"
        dts = re.search(r"\b(pkt_dts_time|dts_time):\s*(-?\d+(?:\.\d+)?)", line)
        if dts:
            value = float(dts.group(2))
            self.stream_metadata["last_media_dts"] = value
            self.stream_metadata["dts_time"] = value
            prior = self._dts_by_stream.get(stream_index)
            if prior is not None and value < prior - 0.001:
                self._event("DTS_REGRESSION", "WARNING", {"stream_index": stream_index, "previous_dts": prior, "dts": value})
            self._dts_by_stream[stream_index] = value

    def _reset_connection_state(self) -> None:
        """Forget timestamp and media-health baselines that cannot cross reconnects."""
        self.analyzer.begin_new_epoch()
        self._dts_by_stream.clear()
        self.stream_metadata.clear()
        self.stderr_tail.clear()
        self.pending_events.clear()
        self.active_events.clear()
        self.last_event_mono.clear()
        self.last_progress_mono = None
        self.last_packet_mono = None
        self.last_frame_mono = None
        self.last_audio_frame_mono = None
        self.last_video_packet_mono = None
        self.last_progress.clear()
        self.last_progress_frame = 0
        self._reading_input_metadata = False
        self._video_stream_indices.clear()
        self._audio_stream_indices.clear()
        self._packet_bytes.clear()
        self._process_stats = None
        self.process_cpu = 0.0
        self.process_rss = 0

    async def _monitor_loop(self) -> None:
        warning = self.config.monitoring.warning_threshold
        stall = self.config.monitoring.stall_threshold
        while self.is_running:
            await asyncio.sleep(1)
            now = time.monotonic()
            self._update_process_usage()
            events = self.analyzer.check_keyframe_gap(now)
            for event in events:
                self._accept_event(event)
            progress_age = now - (self.last_progress_mono or self.started_mono)
            if progress_age > warning:
                self._event_throttled("PROGRESS_STALE", "WARNING", {"age_seconds": round(progress_age, 2)}, 10)
            if self.config.agent.profile == "DEEP" and self.stream_metadata.get("video_codec"):
                media_at = self.last_frame_mono
            elif self.config.agent.profile == "DEEP" and self.stream_metadata.get("audio_codec"):
                media_at = self.last_audio_frame_mono
            elif self.config.agent.profile == "LIGHT" and self._video_stream_indices:
                media_at = self.last_video_packet_mono
            else:
                media_at = self.last_packet_mono if self.config.agent.profile == "LIGHT" else self.last_progress_mono
            media_age = now - (media_at or self.started_mono)
            if media_age > stall:
                self._event_throttled("STREAM_STALL", "CRITICAL", {"last_media_age_seconds": round(media_age, 2)}, 15)
            if self.stream_metadata.get("audio_codec"):
                audio_age = now - (self.last_audio_frame_mono or self.started_mono)
                if audio_age > stall:
                    self._event_throttled("AUDIO_MISSING", "WARNING", {"last_audio_age_seconds": round(audio_age, 2)}, 15)
            if self._packet_bytes:
                window = max(min(now - self._packet_bytes[0][0], 10), 1)
                self.stream_metadata["bitrate_estimate_bps"] = round(sum(size for _, size in self._packet_bytes) * 8 / window, 0)
            if max(progress_age, media_age) > max(self.config.monitoring.dead_threshold, stall) and self.is_running:
                self._event("FFMPEG_DEAD", "CRITICAL", {"progress_age_seconds": round(progress_age, 2), "media_age_seconds": round(media_age, 2)})
                self.last_restart_reason = "FFmpeg produced no progress or media for the dead threshold"
                await self._stop_process()
                return
            self._publish(self._snapshot(now))

    def _attach_process_stats(self) -> None:
        self._process_stats = None
        self.process_cpu = 0.0
        self.process_rss = 0
        if not self.process or self.process.returncode is not None or not self.process.pid:
            return
        try:
            self._process_stats = psutil.Process(self.process.pid)
            self._process_stats.cpu_percent(interval=None)
            self.process_rss = self._process_stats.memory_info().rss
        except (psutil.Error, OSError):
            self._process_stats = None

    def _update_process_usage(self) -> None:
        if not self.process or self.process.returncode is not None or not self.process.pid:
            return
        if self._process_stats is None:
            self._attach_process_stats()
            return
        try:
            self.process_cpu = round(self._process_stats.cpu_percent(interval=None), 2)
            self.process_rss = self._process_stats.memory_info().rss
        except (psutil.Error, OSError):
            self._process_stats = None
            self.process_cpu = 0.0
            self.process_rss = 0

    def _snapshot(self, now: float) -> dict[str, Any]:
        last_media = self.last_frame_mono if self.config.agent.profile == "DEEP" else (self.last_video_packet_mono if self._video_stream_indices else self.last_packet_mono)
        metrics = {
            **self.stream_metadata,
            "profile": self.config.agent.profile,
            "ffmpeg_running": self.is_running,
            "last_frame_age": round(max(0.0, now - last_media), 3) if last_media is not None else None,
            "connect_to_first_media_ms": round((last_media - self.started_mono) * 1000, 1) if last_media is not None else None,
            "last_progress_age": round(max(0.0, now - self.last_progress_mono), 3) if self.last_progress_mono else None,
            "frames": self.analyzer.frame_count,
            "packets": self.packet_count,
            "last_frame_type": self.analyzer.last_frame_type,
            "last_frame_is_keyframe": self.analyzer.last_frame_is_keyframe,
            "i_frames": self.analyzer.i_frame_count if self.config.agent.profile == "DEEP" else None,
            "i_frames_without_key_flag": self.analyzer.i_frames_without_key_flag if self.config.agent.profile == "DEEP" else None,
            "last_media_pts": self.analyzer.last_pts if self.config.agent.profile == "DEEP" else self.stream_metadata.get("pts_time"),
            "keyframes": self.analyzer.keyframe_count,
            "last_keyframe_age": round(now - self.analyzer.last_keyframe_mono, 3) if self.analyzer.last_keyframe_mono else None,
            "last_keyframe_pts": self.analyzer.last_keyframe_pts,
            "expected_gop_seconds": self.analyzer.expected_gop_seconds,
            "expected_gop_frames": self.analyzer.expected_gop_frames,
            "current_gop_frames": self.analyzer.current_gop_frames,
            "current_gop_duration": round(now - self.analyzer.last_keyframe_mono, 3) if self.analyzer.last_keyframe_mono else None,
            "pts_regressions": self.analyzer.pts_regressions,
            "pts_jumps": self.analyzer.pts_jumps,
            "decode_errors": self.decode_error_count,
            "reconnect_count": self.reconnect_count,
            "process_cpu_percent": self.process_cpu,
            "process_rss_bytes": self.process_rss,
            "last_progress": self.last_progress,
            "last_audio_frame_age": round(now - self.last_audio_frame_mono, 3) if self.last_audio_frame_mono else None,
            "uptime_seconds": round(now - self.started_mono, 1),
            "queue_rows": self.outbox.size,
            "queue_dropped_rows": self.outbox.dropped_rows,
        }
        if self.shared_metrics:
            metrics.update(self.shared_metrics())
        persistent = {"FREEZE_START", "SILENCE_START", "KEYFRAME_GAP", "STREAM_STALL", "PROGRESS_STALE", "AUDIO_MISSING"}
        for code in list(self.active_events):
            if code not in persistent and now - self.last_event_mono.get(code, 0) > 30:
                self.active_events.pop(code, None)
        severities = {e.get("severity") for e in self.active_events.values()}
        status = "CRITICAL" if "CRITICAL" in severities else "WARNING" if severities else "OK"
        events = self.pending_events[:]
        self.pending_events.clear()
        return {
            "sample_id": str(uuid.uuid4()),
            "stream_id": self.stream.id,
            "observed_at": utc_iso(),
            "status": status,
            "metrics": metrics,
            "events": events,
            "context": {"stderr_tail": list(self.stderr_tail)[-200:]} if events else {},
        }

    def _publish(self, item: dict[str, Any]) -> None:
        try:
            self.outbox.put(item)
        except Exception:
            LOG.exception("Unable to cache telemetry locally")

    def _accept_event(self, event: dict[str, Any]) -> None:
        code = str(event.get("code", "UNKNOWN"))
        if code == "DECODE_ERROR":
            self.decode_error_count += 1
        if code.endswith("_END"):
            self.active_events.pop(code.removesuffix("_END") + "_START", None)
            if code == "KEYFRAME_GAP_END":
                self.active_events.pop("KEYFRAME_GAP", None)
            self.pending_events.append({**event, "timestamp": utc_iso()})
            return
        now = time.monotonic()
        if now - self.last_event_mono.get(code, 0) < 2:
            return
        if code in {"PTS_REGRESSION", "PTS_JUMP", "DTS_REGRESSION", "DECODE_ERROR", "FFMPEG_RESTART", "FFMPEG_EXIT"}:
            self.active_events[code] = event
            self.last_event_mono[code] = now
        elif code in {"FREEZE_START", "SILENCE_START", "KEYFRAME_GAP", "STREAM_STALL", "PROGRESS_STALE", "FFMPEG_DEAD", "AUDIO_MISSING"}:
            self.active_events[code] = event
            self.last_event_mono[code] = now
        else:
            self.last_event_mono[code] = now
        self.pending_events.append({**event, "timestamp": utc_iso()})

    def _event(self, code: str, severity: str, details: dict[str, Any]) -> None:
        self._accept_event({"code": code, "severity": severity, "details": details})

    def _event_throttled(self, code: str, severity: str, details: dict[str, Any], interval: float) -> None:
        now = time.monotonic()
        if now - self.last_event_mono.get(code, 0) >= interval:
            self._event(code, severity, details)
            self.last_event_mono[code] = now

    async def _stop_process(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


def _number(value: str) -> float | int | str:
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except (ValueError, AttributeError):
        return value


def _float_or_none(value: str | None) -> float | None:
    if not value or value == "N/A":
        return None
    try:
        return float(value)
    except ValueError:
        return None


LOG_FFMPEG = logging.getLogger("rtmp_monitor.ffmpeg")


class AgentRunner:
    def __init__(self, config: AgentFileConfig):
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.outbox = LocalQueue(
            self.config.state_dir / "agent_queue.db",
            self.config.monitoring.queue_max_bytes,
            self.config.monitoring.queue_max_rows,
        )
        self.network = NetworkTelemetry(
            self.config.network.server_host,
            self.config.network.server_port,
            self.config.network.enabled,
        )
        self._last_network_mono = 0.0
        self._network_snapshot: dict[str, Any] = {}
        self._network_snapshot_mono: float | None = None
        self._clock_snapshot: dict[str, Any] = {}
        self._http_offset_ms: float | None = None
        self._http_offset_uncertainty_ms: float | None = None
        self._http_offset_source: str | None = None
        self._http_offset_updated_mono: float | None = None
        self.started_mono = time.monotonic()
        self.agent_process = psutil.Process()
        self.agent_process.cpu_percent(interval=None)
        self._agent_cpu_percent = 0.0
        self._agent_rss_bytes = 0
        self._agent_metrics_sampled_at: str | None = None
        if config.agent.role == "SERVER_INGRESS":
            self.probes = [SrsIngressProbe(stream, config, self.outbox, self._shared_metrics) for stream in config.streams]
        else:
            self.probes = [StreamProbe(stream, config, self.outbox, self._shared_metrics) for stream in config.streams]
        self._last_delivery_warning = 0.0

    async def run(self) -> None:
        if not self.config.agent.token:
            raise RuntimeError("agent.token is empty; create this agent in the central dashboard and copy its token into config")
        await self._refresh_environment_metrics()
        tasks = [asyncio.create_task(probe.run(), name=f"probe-{probe.stream.id}") for probe in self.probes]
        try:
            await self._sender_loop()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _sender_loop(self) -> None:
        api_url = self.config.server.url.rstrip("/") + "/api/v1/ingest"
        while True:
            now = time.monotonic()
            if now - self._last_network_mono >= self.config.network.ping_interval:
                await self._refresh_environment_metrics()
            try:
                payloads = self.outbox.peek(100)
            except Exception:
                if now - self._last_delivery_warning >= 30:
                    LOG.exception("Unable to read local telemetry outbox; probes continue and delivery will retry")
                    self._last_delivery_warning = now
                await asyncio.sleep(self.config.monitoring.heartbeat_interval)
                continue
            if payloads:
                items = []
                ids = []
                for row_id, payload in payloads:
                    items.append(payload)
                    ids.append(row_id)
                try:
                    response_data = await asyncio.to_thread(self._post, api_url, {"items": items})
                    central_offset = response_data.get("central_offset_ms")
                    if isinstance(central_offset, (int, float)):
                        self._http_offset_ms = float(central_offset)
                        uncertainty = response_data.get("central_offset_uncertainty_ms")
                        if isinstance(uncertainty, (int, float)) and uncertainty >= 0:
                            self._http_offset_uncertainty_ms = float(uncertainty)
                        source = response_data.get("central_offset_source")
                        if isinstance(source, str):
                            self._http_offset_source = source
                        self._http_offset_updated_mono = time.monotonic()
                    self.outbox.ack(ids)
                except urllib.error.HTTPError as exc:
                    if now - self._last_delivery_warning >= 30:
                        LOG.error("Central server rejected telemetry (HTTP %s); %s rows remain queued. Check the agent token and stream registration.", exc.code, self._outbox_size_for_log())
                        self._last_delivery_warning = now
                except Exception as exc:
                    if now - self._last_delivery_warning >= 30:
                        LOG.warning("Central server unavailable; %s telemetry records remain queued: %s", self._outbox_size_for_log(), exc)
                        self._last_delivery_warning = now
            await asyncio.sleep(self.config.monitoring.heartbeat_interval)

    def _outbox_size_for_log(self) -> int | str:
        try:
            return self.outbox.size
        except Exception:
            return "an unknown number of"

    async def _refresh_environment_metrics(self) -> None:
        try:
            network_sample = await asyncio.to_thread(self.network.sample)
            self._network_snapshot = {**network_sample, "sampled_at": utc_iso()}
        except Exception:
            self._network_snapshot = {"available": False, "reason": "provider error", "sampled_at": utc_iso()}
            LOG.exception("Network telemetry provider failed")
        self._network_snapshot_mono = time.monotonic()
        try:
            clock_sample = await asyncio.to_thread(clock_status)
            self._clock_snapshot = {**clock_sample, "sampled_at": utc_iso()}
        except Exception:
            self._clock_snapshot = {"ntp_synchronized": None, "estimated_offset_ms": None, "sampled_at": utc_iso()}
            LOG.exception("Clock telemetry provider failed")
        try:
            self._agent_cpu_percent = round(self.agent_process.cpu_percent(interval=None), 2)
            self._agent_rss_bytes = self.agent_process.memory_info().rss
        except (psutil.Error, OSError):
            self._agent_cpu_percent = 0.0
            self._agent_rss_bytes = 0
        self._agent_metrics_sampled_at = utc_iso()
        self._last_network_mono = time.monotonic()

    def _shared_metrics(self) -> dict[str, Any]:
        now = time.monotonic()
        network_snapshot = dict(self._network_snapshot)
        if self._network_snapshot_mono is not None:
            network_snapshot["sample_age_seconds"] = round(max(0.0, now - self._network_snapshot_mono), 3)
            network_snapshot["sample_interval_seconds"] = self.config.network.ping_interval
        offset_age = (
            round(now - self._http_offset_updated_mono, 1)
            if self._http_offset_updated_mono is not None
            else None
        )
        return {
            "network": network_snapshot,
            "clock": {
                **self._clock_snapshot,
                "central_offset_ms": self._http_offset_ms,
                "central_offset_uncertainty_ms": self._http_offset_uncertainty_ms,
                "central_offset_source": self._http_offset_source,
                "central_offset_age_seconds": offset_age,
            },
            "agent_uptime_seconds": round(now - self.started_mono, 1),
            "agent_cpu_percent": self._agent_cpu_percent,
            "agent_rss_bytes": self._agent_rss_bytes,
            "agent_metrics_sampled_at": self._agent_metrics_sampled_at,
        }

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.config.agent.token}"},
            method="POST",
        )
        before = time.time()
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            after = time.time()
            elapsed = max(0.0, after - before)
            midpoint = (before + after) / 2
            received_at = body.get("received_at")
            if isinstance(received_at, str):
                try:
                    central_time = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                    if central_time.tzinfo is None:
                        central_time = central_time.replace(tzinfo=timezone.utc)
                    body["central_offset_ms"] = round((central_time.timestamp() - midpoint) * 1000, 1)
                    body["central_offset_source"] = "central_receive_timestamp"
                    body["central_offset_uncertainty_ms"] = round(
                        1 + elapsed * 500, 1
                    )
                except (OverflowError, TypeError, ValueError):
                    received_at = None
            if not isinstance(received_at, str):
                date_header = response.headers.get("Date")
                if date_header:
                    try:
                        from email.utils import parsedate_to_datetime

                        central = parsedate_to_datetime(date_header).timestamp()
                        body["central_offset_ms"] = round((central - midpoint) * 1000, 1)
                        body["central_offset_source"] = "http_date"
                        # HTTP Date has one-second resolution. Include that
                        # quantization bound and half the request window.
                        body["central_offset_uncertainty_ms"] = round(1001 + elapsed * 500, 1)
                    except (OverflowError, TypeError, ValueError):
                        pass
            return body

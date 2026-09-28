from __future__ import annotations

import re
import statistics
from collections import deque
from datetime import datetime, timezone


SHOWINFO = re.compile(
    r"n:\s*(?P<n>\d+).*?pts_time:\s*(?P<pts>-?\d+(?:\.\d+)?).*?"
    r"iskey:\s*(?P<key>\d).*?type:(?P<type>[IPB?])"
)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FrameAnalyzer:
    """Low-allocation per-frame health analysis over FFmpeg showinfo output."""

    def __init__(self, configured_gap: float | None = None):
        self.configured_gap = configured_gap
        self.keyframes: deque[tuple[float, float]] = deque(maxlen=32)
        self.last_pts: float | None = None
        self.last_frame_mono: float | None = None
        self.first_frame_mono: float | None = None
        self.last_keyframe_mono: float | None = None
        self.last_keyframe_pts: float | None = None
        self.last_keyframe_frame_count: int | None = None
        self.last_frame_type: str | None = None
        self.last_frame_is_keyframe = False
        self.frame_count = 0
        self.keyframe_count = 0
        self.gop_lengths: deque[int] = deque(maxlen=32)
        self.keyframe_pts_intervals: deque[float] = deque(maxlen=32)
        self.pts_regressions = 0
        self.pts_jumps = 0
        self._gap_active = False

    @property
    def expected_gop_seconds(self) -> float | None:
        intervals = list(self.keyframe_pts_intervals)
        if not intervals:
            intervals = [right[1] - left[1] for left, right in zip(self.keyframes, list(self.keyframes)[1:])]
        plausible = [value for value in intervals if 0.05 <= value <= 30]
        return statistics.median(plausible) if plausible else None

    @property
    def expected_gop_frames(self) -> int | None:
        return round(statistics.median(self.gop_lengths)) if self.gop_lengths else None

    @property
    def current_gop_frames(self) -> int | None:
        if self.last_keyframe_frame_count is None:
            return None
        return max(0, self.frame_count - self.last_keyframe_frame_count)

    @property
    def gap_limit_seconds(self) -> float | None:
        if self.configured_gap is not None:
            return self.configured_gap
        expected = self.expected_gop_seconds
        return max(5.0, expected * 2.5) if expected is not None else 5.0

    def feed(self, line: str, monotonic_time: float) -> list[dict]:
        match = SHOWINFO.search(line)
        if not match:
            return []
        pts = float(match.group("pts"))
        is_keyframe = match.group("key") == "1" or match.group("type") == "I"
        self.frame_count += 1
        self.last_frame_type = match.group("type")
        self.last_frame_is_keyframe = is_keyframe
        self.last_frame_mono = monotonic_time
        if self.first_frame_mono is None:
            self.first_frame_mono = monotonic_time
        events: list[dict] = []
        if self.last_pts is not None:
            delta = pts - self.last_pts
            if delta < -0.001:
                self.pts_regressions += 1
                events.append({"code": "PTS_REGRESSION", "severity": "WARNING", "details": {"previous_pts": self.last_pts, "pts": pts}})
            elif delta > 10.0:
                self.pts_jumps += 1
                events.append({"code": "PTS_JUMP", "severity": "WARNING", "details": {"delta_seconds": delta, "pts": pts}})
        self.last_pts = pts
        if is_keyframe:
            if self.last_keyframe_mono is not None:
                self.keyframes.append((self.last_keyframe_mono, monotonic_time))
            if self.last_keyframe_frame_count is not None:
                self.gop_lengths.append(self.frame_count - self.last_keyframe_frame_count)
            if self.last_keyframe_pts is not None:
                self.keyframe_pts_intervals.append(pts - self.last_keyframe_pts)
            self.last_keyframe_mono = monotonic_time
            self.last_keyframe_pts = pts
            self.last_keyframe_frame_count = self.frame_count
            self.keyframe_count += 1
            if self._gap_active:
                events.append({"code": "KEYFRAME_GAP_END", "severity": "INFO", "details": {"pts": pts}})
            self._gap_active = False
        return events

    def check_keyframe_gap(self, monotonic_time: float) -> list[dict]:
        limit = self.gap_limit_seconds
        if limit is None:
            return []
        if self.last_keyframe_mono is None:
            if self.frame_count == 0 or self.first_frame_mono is None or self._gap_active:
                return []
            age = monotonic_time - self.first_frame_mono
            if age >= limit:
                self._gap_active = True
                return [{"code": "KEYFRAME_GAP", "severity": "CRITICAL", "details": {"no_keyframe_seen": True, "seconds_without_keyframe": round(age, 3), "threshold_seconds": limit}}]
            return []
        age = monotonic_time - self.last_keyframe_mono
        if age >= limit and not self._gap_active:
            self._gap_active = True
            return [{
                "code": "KEYFRAME_GAP",
                "severity": "CRITICAL",
                "details": {"seconds_without_keyframe": round(age, 3), "threshold_seconds": limit, "expected_gop_seconds": self.expected_gop_seconds},
            }]
        return []


def parse_filter_event(line: str) -> dict | None:
    if "freezedetect" in line:
        code = "FREEZE_START" if "freeze_start" in line else "FREEZE_END" if "freeze_end" in line else None
        if code:
            match = re.search(r"(?:freeze_start|freeze_end|freeze_duration):\s*(-?\d+(?:\.\d+)?)", line)
            return {"code": code, "severity": "WARNING" if code == "FREEZE_START" else "INFO", "details": {"value": float(match.group(1)) if match else None, "message": line[-500:]}}
    if "silencedetect" in line:
        code = "SILENCE_START" if "silence_start" in line else "SILENCE_END" if "silence_end" in line else None
        if code:
            match = re.search(r"(?:silence_start|silence_end|silence_duration):\s*(-?\d+(?:\.\d+)?)", line)
            return {"code": code, "severity": "WARNING" if code == "SILENCE_START" else "INFO", "details": {"value": float(match.group(1)) if match else None, "message": line[-500:]}}
    return None


DECODE_ERROR_PATTERNS = (
    "error while decoding",
    "invalid nal",
    "corrupt decoded frame",
    "concealing ",
    "no frame!",
    "missing picture in access unit",
    "decode_slice_header error",
)


def parse_diagnostic_line(line: str) -> dict | None:
    filter_event = parse_filter_event(line)
    if filter_event:
        return filter_event
    lowered = line.lower()
    if any(pattern in lowered for pattern in DECODE_ERROR_PATTERNS):
        return {"code": "DECODE_ERROR", "severity": "CRITICAL", "details": {"message": line[-1000:]}}
    return None

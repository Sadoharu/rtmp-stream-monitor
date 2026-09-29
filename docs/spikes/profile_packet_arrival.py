"""Compare FFmpeg framecrc stdout packet cadence with and without packet flushing."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict

PACKET_ROW = re.compile(r"^\s*\d+,\s*-?\d+,\s*-?\d+,\s*-?\d+,\s*(\d+),")


def profile(ffmpeg: str, url: str, seconds: int, flush: bool) -> dict[str, object]:
    command = [
        ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning",
        "-progress", "pipe:2", "-stats_period", "1", "-rw_timeout", "15000000",
        "-i", url,
        "-t", str(seconds), "-map", "0:v?", "-map", "0:a?",
        "-vf", "showinfo", "-af", "ashowinfo", "-f", "null", "-",
        "-t", str(seconds), "-map", "0:v?", "-map", "0:a?", "-c", "copy",
    ]
    if flush:
        command.extend(["-flush_packets", "1"])
    command.extend(["-f", "framecrc", "-hash", "crc32", "pipe:1"])

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    packets: list[tuple[float, int]] = []
    stderr_tail: list[str] = []

    def drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            line = line.rstrip()
            if line:
                stderr_tail.append(line)
                del stderr_tail[:-20]

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    assert process.stdout is not None
    started = time.monotonic()
    for line in process.stdout:
        match = PACKET_ROW.match(line)
        if match:
            packets.append((time.monotonic(), int(match.group(1))))
    return_code = process.wait()
    stderr_thread.join(timeout=2)
    wall_seconds = time.monotonic() - started

    buckets: dict[int, int] = defaultdict(int)
    if packets:
        first = packets[0][0]
        for arrived_at, size in packets:
            buckets[math.floor(arrived_at - first)] += size
    duration = max(packets[-1][0] - packets[0][0], 0.001) if len(packets) > 1 else wall_seconds
    return {
        "flush_packets": flush,
        "exit_code": return_code,
        "wall_seconds": round(wall_seconds, 3),
        "packet_count": len(packets),
        "packet_bytes": sum(size for _, size in packets),
        "mean_arrival_mbps": round(sum(size for _, size in packets) * 8 / duration / 1_000_000, 3),
        "one_second_buckets": [
            {"second_from_first_packet": second, "bytes": size, "mbps": round(size * 8 / 1_000_000, 3)}
            for second, size in sorted(buckets.items())
        ],
        "stderr_tail": stderr_tail[-8:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stream_url", help="RTMP URL; do not paste credentials into shared logs")
    parser.add_argument("--seconds", type=int, default=8)
    args = parser.parse_args()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        parser.error("ffmpeg is not on PATH")
    print(json.dumps({"flush_off": profile(ffmpeg, args.stream_url, args.seconds, False)}, indent=2))
    print(json.dumps({"flush_on": profile(ffmpeg, args.stream_url, args.seconds, True)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

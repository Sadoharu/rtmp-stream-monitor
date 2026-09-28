#!/usr/bin/env python3
"""Short, non-transcoding live check of the same deep FFmpeg path used by an agent."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="RTMP URL to probe")
    parser.add_argument("--seconds", type=float, default=12)
    args = parser.parse_args()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg was not found on PATH", file=sys.stderr)
        return 2
    command = [
        ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info", "-debug_ts",
        "-progress", "pipe:1", "-stats_period", "1", "-rw_timeout", "15000000",
        "-i", args.url, "-map", "0:v?", "-map", "0:a?",
        "-vf", "freezedetect=n=-60dB:d=2,showinfo",
        "-af", "silencedetect=n=-50dB:d=3,ashowinfo", "-f", "null", "-",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=args.seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    frame_lines = re.findall(r"showinfo.*?\bn:\s*\d+.*?iskey:\s*\d.*?type:[IPB?]", err)
    keyframes = re.findall(r"showinfo.*?\bn:\s*\d+.*?iskey:\s*1.*?type:[IPB?]", err)
    print(f"URL: {args.url}")
    print(f"Deep decode frames observed: {len(frame_lines)}")
    print(f"Keyframes observed: {len(keyframes)}")
    print("Stream metadata:")
    for line in err.splitlines():
        if "Stream #" in line and ("Video:" in line or "Audio:" in line):
            print("  " + line.strip())
    decode_errors = [line.strip() for line in err.splitlines() if any(term in line.lower() for term in ("error while decoding", "invalid nal", "corrupt decoded frame", "concealing ", "no frame!"))]
    print(f"Decode diagnostic lines: {len(decode_errors)}")
    for line in decode_errors[:10]:
        print("  " + line)
    if not frame_lines:
        print("No decoded video frames arrived before the smoke-test timeout.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

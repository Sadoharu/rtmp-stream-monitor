#!/usr/bin/env python3
"""Exercise the native Windows per-flow receiver TCP EStats API on loopback.

This verifies the OS API and ctypes layouts, not packet-loss diagnosis.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rtmp_monitor.windows_tcp import sample_windows_tcp_receiver_stats


def run_smoke() -> dict[str, object]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    server, _address = listener.accept()
    try:
        owner_pids = {os.getpid()}
        first = sample_windows_tcp_receiver_stats("127.0.0.1", port, owner_pids)
        second = sample_windows_tcp_receiver_stats("127.0.0.1", port, owner_pids)
        if first.get("status") != "AVAILABLE" or second.get("status") != "AVAILABLE":
            raise RuntimeError(f"Windows TCP EStats did not become available: first={first}; second={second}")
        if first.get("flows", [{}])[0].get("key", "").split(":", 1)[0] != str(os.getpid()):
            raise RuntimeError(f"The matching TCP flow was not owned by this probe process: {second}")
        for result in (first, second):
            counters = result["flows"][0]
            for key in ("duplicate_ack_episodes_total", "duplicate_acks_total"):
                if isinstance(counters.get(key), bool) or not isinstance(counters.get(key), int):
                    raise RuntimeError(f"Windows TCP EStats returned no numeric {key}: {result}")
        return {"ok": True, "first": first, "second": second}
    finally:
        server.close()
        client.close()
        listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-file", type=Path)
    args = parser.parse_args()
    try:
        result = run_smoke()
    except Exception as exc:
        failure = {"ok": False, "error": str(exc)}
        if args.result_file:
            args.result_file.write_text(json.dumps(failure, separators=(",", ":")), encoding="utf-8")
        raise
    if args.result_file:
        args.result_file.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(result, separators=(",", ":")))

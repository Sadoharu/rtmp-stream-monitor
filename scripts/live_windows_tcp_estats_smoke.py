#!/usr/bin/env python3
"""Exercise the native Windows per-flow receiver TCP EStats API on loopback.

Run from an elevated Windows shell. This verifies the OS API and ctypes
layouts, not the installed service account or packet-loss diagnosis.
"""

from __future__ import annotations

import json
import os
import socket

from rtmp_monitor.windows_tcp import sample_windows_tcp_receiver_stats


def main() -> None:
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
        print(json.dumps({"first": first, "second": second}, separators=(",", ":")))
    finally:
        server.close()
        client.close()
        listener.close()


if __name__ == "__main__":
    main()

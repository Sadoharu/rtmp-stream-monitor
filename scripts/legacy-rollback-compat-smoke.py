#!/usr/bin/env python3
"""Check that the pre-V2 central source can roll back onto a migrated V2 SQLite DB."""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


LEGACY_BASELINE = "a052931249bc253cfebc151ee1052bb8d5569d01"

LEGACY_CHECK = r'''import json, os
from pathlib import Path
import rtmp_monitor
from fastapi.testclient import TestClient
from rtmp_monitor.api import create_app
from rtmp_monitor.config import CentralFileConfig

assert str(Path(rtmp_monitor.__file__).resolve()).startswith(os.environ["LEGACY_SOURCE"])
config = CentralFileConfig(
    database_url=os.environ["DATABASE_URL"],
    admin_token_file=Path(os.environ["ADMIN_TOKEN_FILE"]),
    logs_dir=Path(os.environ["LEGACY_LOGS_DIR"]),
)
admin = {"Authorization": "Bearer " + os.environ["ADMIN_TOKEN"]}
agent = {"Authorization": "Bearer " + os.environ["AGENT_TOKEN"]}
with TestClient(create_app(config)) as client:
    streams = client.get("/api/v1/streams", headers=admin)
    agents = client.get("/api/v1/agents", headers=admin)
    timeline = client.get(
        "/api/v1/timeline", params={"stream_id": "poland", "hours": 1}, headers=admin
    )
    assert streams.status_code == 200 and streams.json()[0]["id"] == "poland", streams.text
    assert agents.status_code == 200 and agents.json()[0]["name"] == "rollback-agent", agents.text
    assert timeline.status_code == 200 and "FREEZE_START" in json.dumps(timeline.json()), timeline.text
    sample = {
        "sample_id": "pre-v2-after-rollback",
        "stream_id": "poland",
        "observed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "status": "OK",
        "metrics": {"received_media_bitrate_bps": 1_100_000},
        "events": [],
        "context": {},
    }
    ingest = client.post("/api/v1/ingest", headers=agent, json={"items": [sample]})
    assert ingest.status_code == 200 and ingest.json()["accepted"] == 1, ingest.text
print(
    f"legacy_version={rtmp_monitor.__version__}; streams={len(streams.json())}; "
    f"agents={len(agents.json())}; timeline_http={timeline.status_code}; "
    f"old_agent_ingest={ingest.json()['accepted']}"
)
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-ref",
        default=LEGACY_BASELINE,
        help="pre-V2 git commit to use as the rollback source (default: initial systemd baseline)",
    )
    args = parser.parse_args()
    repo = Path.cwd().resolve()
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{args.legacy_ref}:src/rtmp_monitor/api.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if probe.returncode:
        parser.error(f"Git revision {args.legacy_ref!r} does not contain src/rtmp_monitor/api.py")

    with tempfile.TemporaryDirectory(prefix="rtmp-legacy-rollback-") as temporary:
        root = Path(temporary)
        legacy_archive = subprocess.run(
            ["git", "archive", args.legacy_ref, "src/rtmp_monitor"],
            cwd=repo,
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(legacy_archive), mode="r:") as archive:
            archive.extractall(root, filter="data")
        legacy_source = (root / "src").resolve()

        sys.path.insert(0, str(repo / "src"))
        from fastapi.testclient import TestClient
        from rtmp_monitor.api import create_app
        from rtmp_monitor.config import CentralFileConfig

        data_dir = root / "data"
        logs_dir = root / "logs"
        data_dir.mkdir()
        logs_dir.mkdir()
        database = data_dir / "central.db"
        token_file = data_dir / "admin.token"
        admin_token = "rollback-smoke-admin-token"
        token_file.write_text(admin_token + "\n", encoding="utf-8")
        database_url = f"sqlite:///{database.as_posix()}"
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        with TestClient(create_app(CentralFileConfig(
            database_url=database_url,
            admin_token_file=token_file,
            logs_dir=logs_dir,
        ))) as client:
            stream = client.post(
                "/api/v1/streams",
                headers=admin_headers,
                json={"id": "poland", "name": "Rollback compatibility fixture"},
            )
            assert stream.status_code == 201, stream.text
            created_agent = client.post(
                "/api/v1/agents",
                headers=admin_headers,
                json={
                    "name": "rollback-agent",
                    "location": "isolated-fixture",
                    "platform": "Ubuntu",
                    "role": "CLIENT",
                    "stream_id": "poland",
                },
            )
            assert created_agent.status_code == 201, created_agent.text
            agent_token = created_agent.json()["token"]
            seeded = client.post(
                "/api/v1/ingest",
                headers={"Authorization": f"Bearer {agent_token}"},
                json={"items": [{
                    "sample_id": "v2-before-rollback",
                    "stream_id": "poland",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "status": "WARNING",
                    "metrics": {"received_media_bitrate_bps": 1_200_000, "profile": "LIGHT"},
                    "events": [{
                        "code": "FREEZE_START",
                        "severity": "WARNING",
                        "details": {"duration_seconds": 6},
                    }],
                    "context": {},
                }]},
            )
            assert seeded.status_code == 200 and seeded.json()["accepted"] == 1, seeded.text

        legacy_logs = root / "legacy-logs"
        legacy_logs.mkdir()
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(legacy_source),
            "LEGACY_SOURCE": str(legacy_source),
            "DATABASE_URL": database_url,
            "ADMIN_TOKEN_FILE": str(token_file),
            "ADMIN_TOKEN": admin_token,
            "AGENT_TOKEN": agent_token,
            "LEGACY_LOGS_DIR": str(legacy_logs),
        })
        legacy = subprocess.run(
            [sys.executable, "-c", LEGACY_CHECK],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if legacy.returncode:
            raise RuntimeError(
                "Pre-V2 rollback smoke failed:\n"
                + legacy.stdout
                + legacy.stderr
            )
        print(f"legacy_source_ref={args.legacy_ref}")
        print(legacy.stdout.strip())

        lower = datetime.now(timezone.utc) - timedelta(minutes=5)
        upper = datetime.now(timezone.utc) + timedelta(minutes=5)
        with TestClient(create_app(CentralFileConfig(
            database_url=database_url,
            admin_token_file=token_file,
            logs_dir=root / "v2-logs",
        ))) as client:
            series = client.get(
                "/api/v2/streams/poland/series",
                params={"from": lower.isoformat(), "to": upper.isoformat()},
                headers=admin_headers,
            )
            events = client.get(
                "/api/v2/streams/poland/events",
                params={"from": lower.isoformat(), "to": upper.isoformat()},
                headers=admin_headers,
            )
            assert series.status_code == 200 and series.json()["series"], series.text
            assert events.status_code == 200, events.text
            assert any(event["code"] == "FREEZE_START" for event in events.json()["events"]), events.text
            point_count = sum(len(item["points"]) for item in series.json()["series"])
            print(
                f"v2_reopen_series_http={series.status_code}; "
                f"v2_reopen_events_http={events.status_code}; "
                f"v2_reopen_points={point_count}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

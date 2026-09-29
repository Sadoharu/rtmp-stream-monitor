from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from rtmp_monitor.correlation import run_retention
from rtmp_monitor.db import Agent, Base, MetricAggregate, Stream, Telemetry


def test_raw_samples_are_summarized_before_expiry(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'retention.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 28, 12, 0, 30, tzinfo=timezone.utc)
    with sessions() as session:
        session.add(Stream(id="demo", name="demo"))
        session.add(Agent(id="agent-id", name="client", location="studio", platform="Linux", role="CLIENT", stream_id="demo", token_hash="a" * 64))
        observed_at = now - timedelta(days=8)
        session.add(Telemetry(id="sample", agent_id="agent-id", stream_id="demo", observed_at=observed_at, received_at=now, status="WARNING", metrics={
            "process_cpu_percent": 8.0, "received_media_bitrate_bps": 0,
            "received_media_bitrate_quality": "MEASURED", "measurement_window_seconds": 1.0,
        }, events=[{"code": "FREEZE_START"}]))
        session.commit()
        removed_raw, removed_incidents, removed_aggregates = run_retention(session, 7, 180, 90, now)
        session.commit()
        assert removed_raw == 1
        assert removed_incidents == 0
        assert removed_aggregates == 0
        assert session.get(Telemetry, "sample") is None
        aggregate = session.scalar(select(MetricAggregate))
        assert aggregate.sample_count == 1
        assert aggregate.status == "WARNING"
        assert aggregate.metrics["process_cpu_percent"]["avg"] == 8.0
        assert aggregate.metrics["received_media_bitrate_bps"]["avg"] == 0.0
        assert aggregate.metrics["received_media_bitrate_bps"]["count"] == 1
        assert aggregate.metrics["received_media_bitrate_bps"]["last_observed_at"] == observed_at.isoformat()
        assert aggregate.event_counts == {"FREEZE_START": 1}

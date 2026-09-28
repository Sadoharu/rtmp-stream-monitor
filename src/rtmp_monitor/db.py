from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Stream(Base):
    __tablename__ = "streams"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    local_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    public_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    location: Mapped[str] = mapped_column(String(256), nullable=False, default="unknown")
    platform: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    stream_id: Mapped[str] = mapped_column(ForeignKey("streams.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Telemetry(Base):
    __tablename__ = "telemetry"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    stream_id: Mapped[str] = mapped_column(ForeignKey("streams.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OK")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    __table_args__ = (
        Index("ix_telemetry_stream_observed", "stream_id", "observed_at"),
        Index("ix_telemetry_agent_observed", "agent_id", "observed_at"),
    )


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stream_id: Mapped[str | None] = mapped_column(ForeignKey("streams.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    diagnosis: Mapped[str] = mapped_column(String(64), nullable=False)
    probable_location: Mapped[str] = mapped_column(Text, nullable=False)
    affected_agents: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    symptoms: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    fingerprint: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    __table_args__ = (Index("ix_incidents_stream_opened", "stream_id", "opened_at"),)


class MetricAggregate(Base):
    __tablename__ = "metric_aggregates"
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    stream_id: Mapped[str] = mapped_column(ForeignKey("streams.id"), nullable=False)
    bucket_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    event_counts: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    __table_args__ = (Index("ix_metric_aggregate_stream_bucket", "stream_id", "bucket_start"),)


class AggregateCursor(Base):
    __tablename__ = "aggregate_cursors"
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    processed_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def make_engine(database_url: str):
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite:"):
        connect_args["check_same_thread"] = False
        if database_url.startswith("sqlite:///") and not database_url.startswith("sqlite:////"):
            db_path = database_url.removeprefix("sqlite:///").split("?", 1)[0]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    if database_url.startswith("sqlite:"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

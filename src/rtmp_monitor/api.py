from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .config import CentralFileConfig
from .correlation import correlate_stream, mark_offline_agents, run_retention
from .db import Agent, Base, Incident, Stream, Telemetry, make_engine, make_session_factory, utcnow

LOG = logging.getLogger("rtmp_monitor.api")


class StreamCreate(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=128)
    local_url: str = ""
    public_url: str = ""
    source_url: str = ""


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    location: str = Field(default="unknown", max_length=256)
    platform: str = Field(default="unknown", max_length=64)
    role: str = Field(pattern=r"^(SERVER_INGRESS|SERVER_EGRESS|CLIENT|SOURCE)$")
    stream_id: str

    @field_validator("role")
    @classmethod
    def require_supported_observation(cls, value: str) -> str:
        if value == "SERVER_INGRESS":
            raise ValueError("No server-specific ingress hook adapter is installed. Use a SOURCE probe on the encoder or SERVER_EGRESS on loopback.")
        return value


class TelemetryItem(BaseModel):
    sample_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    stream_id: str
    observed_at: datetime
    status: str = Field(default="OK", max_length=24)
    metrics: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    context: dict[str, Any] = Field(default_factory=dict)


class IngestBatch(BaseModel):
    items: list[TelemetryItem] = Field(min_length=1, max_length=500)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_or_create_admin_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(36)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    LOG.warning("Created dashboard admin token. Read it locally from %s and keep it private.", path.resolve())
    return token


def _datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _agent_data(agent: Agent, now: datetime, offline_seconds: int, stream_offline_seconds: int, latest: Telemetry | None) -> dict[str, Any]:
    last_seen = _datetime(agent.last_seen_at)
    age = (now - last_seen).total_seconds() if last_seen else None
    created = _datetime(agent.created_at)
    if (age is None and (now - created).total_seconds() > offline_seconds) or (age is not None and age > offline_seconds):
        state = "AGENT_OFFLINE"
    elif age is None:
        state = "NEVER_SEEN"
    else:
        metrics = latest.metrics if latest else {}
        media_age = metrics.get("last_frame_age")
        if media_age is None:
            media_age = metrics.get("last_audio_frame_age")
        ffmpeg_running = metrics.get("ffmpeg_running")
        if ffmpeg_running is False:
            state = "STREAM_OFFLINE"
        elif isinstance(media_age, (int, float)) and media_age > stream_offline_seconds:
            state = "STREAM_STALLED"
        elif media_age is None and age > stream_offline_seconds:
            state = "STREAM_OFFLINE"
        else:
            state = latest.status if latest else "UNKNOWN"
    return {
        "id": agent.id, "name": agent.name, "location": agent.location, "platform": agent.platform,
        "role": agent.role, "stream_id": agent.stream_id, "last_seen_at": last_seen.isoformat() if last_seen else None,
        "last_seen_age_seconds": round(age, 1) if age is not None else None, "status": state,
        "metrics": latest.metrics if latest else {}, "events": latest.events if latest else [],
    }


def create_app(config: CentralFileConfig | None = None, database_url: str | None = None, admin_token_file: Path | None = None) -> FastAPI:
    config = config or CentralFileConfig()
    if database_url:
        config.database_url = database_url
    if admin_token_file:
        config.admin_token_file = admin_token_file
    engine = make_engine(config.database_url)
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    admin_token = _read_or_create_admin_token(config.admin_token_file)

    async def maintenance_loop() -> None:
        import asyncio

        last_retention = 0.0
        while True:
            try:
                with sessions() as session:
                    mark_offline_agents(session, config.agent_offline_seconds)
                    session.commit()
                    if asyncio.get_running_loop().time() - last_retention >= 3600:
                        deleted_raw, deleted_incidents, deleted_aggregates = run_retention(
                            session, config.raw_retention_days, config.incident_retention_days, config.aggregated_retention_days
                        )
                        session.commit()
                        last_retention = asyncio.get_running_loop().time()
                        if deleted_raw or deleted_incidents or deleted_aggregates:
                            LOG.info("Retention moved %d telemetry rows into aggregates, removed %d incidents and %d old aggregates", deleted_raw, deleted_incidents, deleted_aggregates)
            except Exception:
                LOG.exception("Central maintenance task failed")
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = __import__("asyncio").create_task(maintenance_loop(), name="rtmp-monitor-maintenance")
        yield
        task.cancel()
        try:
            await task
        except __import__("asyncio").CancelledError:
            pass
        engine.dispose()

    app = FastAPI(title="RTMP Stream Monitor", version=__version__, lifespan=lifespan)
    app.state.sessions = sessions
    app.state.admin_token = admin_token
    app.state.central_config = config

    def get_session():
        with sessions() as session:
            yield session

    def require_admin(authorization: str | None = Header(default=None)):
        token = authorization.removeprefix("Bearer ").strip() if authorization and authorization.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(_token_hash(token), _token_hash(admin_token)):
            raise HTTPException(status_code=401, detail="Dashboard authentication required")
        return True

    def require_agent(authorization: str | None = Header(default=None), session: Session = Depends(get_session)) -> Agent:
        token = authorization.removeprefix("Bearer ").strip() if authorization and authorization.startswith("Bearer ") else ""
        if not token:
            raise HTTPException(status_code=401, detail="Agent token required")
        agent = session.scalar(select(Agent).where(Agent.token_hash == _token_hash(token), Agent.enabled.is_(True)))
        if not agent:
            raise HTTPException(status_code=401, detail="Invalid or disabled agent token")
        return agent

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/api/v1/auth/check")
    def auth_check(_admin: bool = Depends(require_admin)):
        return {"authenticated": True}

    @app.get("/api/v1/streams")
    def list_streams(_admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        return [{"id": item.id, "name": item.name, "local_url": item.local_url, "public_url": item.public_url, "source_url": item.source_url, "created_at": _datetime(item.created_at).isoformat()} for item in session.scalars(select(Stream).order_by(Stream.name)).all()]

    @app.post("/api/v1/streams", status_code=201)
    def create_stream(data: StreamCreate, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        if session.get(Stream, data.id):
            raise HTTPException(status_code=409, detail="Stream id already exists")
        stream = Stream(id=data.id, name=data.name, local_url=data.local_url, public_url=data.public_url, source_url=data.source_url)
        session.add(stream)
        session.commit()
        return {"id": stream.id, "name": stream.name, "local_url": stream.local_url, "public_url": stream.public_url, "source_url": stream.source_url}

    @app.get("/api/v1/agents")
    def list_agents(_admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        now = utcnow()
        agents = session.scalars(select(Agent).order_by(Agent.name)).all()
        latest = {item.id: session.scalar(select(Telemetry).where(Telemetry.agent_id == item.id).order_by(Telemetry.observed_at.desc()).limit(1)) for item in agents}
        return [_agent_data(item, now, config.agent_offline_seconds, config.stream_offline_seconds, latest.get(item.id)) for item in agents]

    @app.post("/api/v1/agents", status_code=201)
    def create_agent(data: AgentCreate, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        if not session.get(Stream, data.stream_id):
            raise HTTPException(status_code=404, detail="Stream not found")
        if session.scalar(select(Agent).where(Agent.name == data.name)):
            raise HTTPException(status_code=409, detail="Agent name already exists")
        token = secrets.token_urlsafe(36)
        agent = Agent(
            id=str(uuid.uuid4()), name=data.name, location=data.location, platform=data.platform,
            role=data.role, stream_id=data.stream_id, token_hash=_token_hash(token),
        )
        session.add(agent)
        session.commit()
        return {"id": agent.id, "name": agent.name, "token": token, "role": agent.role, "stream_id": agent.stream_id}

    @app.post("/api/v1/agents/{agent_id}/rotate-token")
    def rotate_agent_token(agent_id: str, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        agent = session.get(Agent, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        token = secrets.token_urlsafe(36)
        agent.token_hash = _token_hash(token)
        session.commit()
        return {"id": agent.id, "token": token}

    @app.post("/api/v1/ingest")
    def ingest(batch: IngestBatch, request: Request, agent: Agent = Depends(require_agent), session: Session = Depends(get_session)):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 8 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Telemetry batch exceeds the 8 MiB limit")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid content-length header") from exc
        if any(item.stream_id != agent.stream_id for item in batch.items):
            raise HTTPException(status_code=403, detail="Telemetry stream does not match the registered agent")
        received_at = utcnow()
        inserted = 0
        correlation_times: set[datetime] = set()
        latest_observed_at: datetime | None = None
        inserted_samples: list[tuple[datetime, str, bool]] = []
        for item in batch.items:
            if session.get(Telemetry, item.sample_id):
                continue
            observed_at = item.observed_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            else:
                observed_at = observed_at.astimezone(timezone.utc)
            latest_observed_at = max(latest_observed_at, observed_at) if latest_observed_at else observed_at
            inserted_samples.append((observed_at, item.status.upper(), bool(item.events)))
            session.add(Telemetry(
                id=item.sample_id, agent_id=agent.id, stream_id=item.stream_id,
                observed_at=observed_at, received_at=received_at, status=item.status.upper(),
                metrics=item.metrics, events=item.events, context=item.context,
            ))
            inserted += 1
        agent.last_seen_at = received_at
        session.flush()
        if inserted_samples:
            first_observation = min(sample[0] for sample in inserted_samples)
            previous_status = session.scalar(
                select(Telemetry.status)
                .where(Telemetry.agent_id == agent.id, Telemetry.observed_at < first_observation)
                .order_by(Telemetry.observed_at.desc())
                .limit(1)
            )
            bad_statuses = {"WARNING", "CRITICAL", "ERROR"}
            for observed_at, status, has_events in sorted(inserted_samples, key=lambda sample: sample[0]):
                status_changed = status != previous_status
                entered_bad_state = previous_status is None and status in bad_statuses
                if has_events or status_changed and previous_status is not None or entered_bad_state:
                    correlation_times.add(observed_at)
                previous_status = status
        if latest_observed_at is not None:
            correlation_times.add(latest_observed_at)
        for observed_at in sorted(correlation_times):
            correlate_stream(session, agent.stream_id, observed_at, media_tolerance_seconds=config.media_timestamp_tolerance_seconds)
        session.commit()
        return {"accepted": inserted, "received_at": received_at.isoformat()}

    @app.get("/api/v1/dashboard")
    def dashboard_data(stream_id: str | None = None, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        now = utcnow()
        streams = session.scalars(select(Stream).order_by(Stream.name)).all()
        if stream_id and not session.get(Stream, stream_id):
            raise HTTPException(status_code=404, detail="Stream not found")
        agents_query = select(Agent).order_by(Agent.name)
        if stream_id:
            agents_query = agents_query.where(Agent.stream_id == stream_id)
        agents = session.scalars(agents_query).all()
        latest = {item.id: session.scalar(select(Telemetry).where(Telemetry.agent_id == item.id).order_by(Telemetry.observed_at.desc()).limit(1)) for item in agents}
        incidents_query = select(Incident).order_by(Incident.active.desc(), Incident.opened_at.desc()).limit(100)
        if stream_id:
            incidents_query = incidents_query.where(Incident.stream_id == stream_id)
        incidents = session.scalars(incidents_query).all()
        return {
            "generated_at": now.isoformat(),
            "streams": [{"id": stream.id, "name": stream.name, "local_url": stream.local_url, "public_url": stream.public_url, "source_url": stream.source_url} for stream in streams],
            "agents": [_agent_data(item, now, config.agent_offline_seconds, config.stream_offline_seconds, latest.get(item.id)) for item in agents],
            "incidents": [_incident_dict(item) for item in incidents],
            "thresholds": {"agent_offline_seconds": config.agent_offline_seconds, "stream_offline_seconds": config.stream_offline_seconds},
            "clock_warning": _clock_warning([(agent.name, latest[agent.id].metrics) for agent in agents if latest.get(agent.id)], config.clock_offset_warning_ms),
        }

    @app.get("/api/v1/incidents")
    def incidents(active: bool | None = None, stream_id: str | None = None, limit: int = Query(default=100, ge=1, le=500), _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        query = select(Incident).order_by(Incident.opened_at.desc()).limit(limit)
        if active is not None:
            query = query.where(Incident.active.is_(active))
        if stream_id:
            query = query.where(Incident.stream_id == stream_id)
        return [_incident_dict(item) for item in session.scalars(query).all()]

    @app.get("/api/v1/telemetry")
    def telemetry(stream_id: str, hours: int = Query(default=24, ge=1, le=168), _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        if not session.get(Stream, stream_id):
            raise HTTPException(status_code=404, detail="Stream not found")
        cutoff = utcnow() - timedelta(hours=hours)
        rows = session.execute(
            select(Telemetry, Agent).join(Agent, Telemetry.agent_id == Agent.id)
            .where(Telemetry.stream_id == stream_id, Telemetry.observed_at >= cutoff)
            .order_by(Telemetry.observed_at.desc()).limit(100000)
        ).all()
        rows.reverse()
        output = []
        last_by_agent: dict[str, datetime] = {}
        for item, agent in rows:
            observed = _datetime(item.observed_at)
            last = last_by_agent.get(agent.id)
            has_event = bool(item.events)
            if last is None or has_event or (observed - last).total_seconds() >= 10:
                output.append({"timestamp": observed.isoformat(), "agent": agent.name, "role": agent.role,
                               "status": item.status, "metrics": item.metrics, "events": item.events})
                last_by_agent[agent.id] = observed
        return output

    @app.post("/api/v1/admin/retention")
    def retention(_admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        deleted = run_retention(session, config.raw_retention_days, config.incident_retention_days, config.aggregated_retention_days)
        session.commit()
        return {"telemetry_deleted": deleted[0], "incidents_deleted": deleted[1], "aggregates_deleted": deleted[2]}

    @app.get("/api/v1/telemetry/aggregates")
    def telemetry_aggregates(stream_id: str, days: int = Query(default=90, ge=1, le=3650), _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        if not session.get(Stream, stream_id):
            raise HTTPException(status_code=404, detail="Stream not found")
        cutoff = utcnow() - timedelta(days=days)
        rows = session.execute(
            select(MetricAggregate, Agent).join(Agent, MetricAggregate.agent_id == Agent.id)
            .where(MetricAggregate.stream_id == stream_id, MetricAggregate.bucket_start >= cutoff)
            .order_by(MetricAggregate.bucket_start.asc())
        ).all()
        return [{"timestamp": _datetime(item.bucket_start).isoformat(), "agent": agent.name, "role": agent.role,
                 "bucket_seconds": item.bucket_seconds, "sample_count": item.sample_count,
                 "status": item.status, "metrics": item.metrics, "event_counts": item.event_counts} for item, agent in rows]

    return app


def _incident_dict(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id, "stream_id": incident.stream_id,
        "opened_at": _datetime(incident.opened_at).isoformat(), "updated_at": _datetime(incident.updated_at).isoformat(),
        "resolved_at": _datetime(incident.resolved_at).isoformat() if incident.resolved_at else None,
        "active": incident.active, "severity": incident.severity, "diagnosis": incident.diagnosis,
        "probable_location": incident.probable_location, "affected_agents": incident.affected_agents,
        "symptoms": incident.symptoms, "context": incident.context,
    }


def _clock_warning(samples: list[tuple[str, dict[str, Any]]], threshold_ms: float) -> dict[str, Any]:
    unsynchronized = []
    offsets = []
    for name, metrics in samples:
        clock = metrics.get("clock") or {}
        if clock.get("ntp_synchronized") is False:
            unsynchronized.append(name)
        offset = clock.get("estimated_offset_ms")
        if not isinstance(offset, (int, float)):
            offset = clock.get("central_offset_ms")
        if isinstance(offset, (int, float)):
            offsets.append((name, float(offset)))
    spread = round(max((value for _, value in offsets), default=0) - min((value for _, value in offsets), default=0), 1) if offsets else None
    coarse = any(isinstance((metrics.get("clock") or {}).get("central_offset_ms"), (int, float)) and not isinstance((metrics.get("clock") or {}).get("estimated_offset_ms"), (int, float)) for _, metrics in samples)
    max_abs = round(max((abs(value) for _, value in offsets), default=0), 1) if offsets else None
    warning = bool(unsynchronized or (spread is not None and spread > threshold_ms) or (max_abs is not None and max_abs > threshold_ms))
    return {"warning": warning,
            "message": "CLOCK NOT SYNCHRONIZED" if warning else None,
            "unsynchronized_agents": unsynchronized, "offset_spread_ms": spread, "threshold_ms": threshold_ms,
            "maximum_absolute_offset_ms": max_abs,
            "measurement": "approximate central HTTP Date" if coarse else "agent NTP provider"}

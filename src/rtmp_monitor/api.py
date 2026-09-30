from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from bisect import bisect_left, bisect_right
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from math import ceil, isfinite
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Float, case, cast, func, or_, select, union, update
from sqlalchemy.orm import Session

from . import __version__
from .config import CentralFileConfig
from .correlation import correlate_stream, mark_offline_agents, run_retention
from .db import Agent, Base, Incident, MetricAggregate, ProbeEnrollment, Stream, Telemetry, make_engine, make_session_factory, utcnow
from .explanations import build_evidence_packet, deterministic_explanation, openai_explanation

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


class EnrollmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    location: str = Field(default="unknown", max_length=256)
    platform: str = Field(default="unknown", max_length=64)
    role: str = Field(pattern=r"^(SERVER_INGRESS|SERVER_EGRESS|CLIENT|SOURCE)$")
    stream_id: str
    central_url: str = Field(min_length=1, max_length=2048)
    profile: str = Field(default="DEEP", pattern=r"^(LIGHT|DEEP)$")

    @field_validator("central_url")
    @classmethod
    def validate_central_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("central_url must be an http(s) origin")
        if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("central_url must be an origin without credentials, path, query or fragment")
        if parsed.scheme != "https" and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("central_url must use HTTPS for remote probes")
        return f"{parsed.scheme}://{parsed.netloc}"


class EnrollmentRedeem(BaseModel):
    code: str = Field(min_length=24, max_length=128)


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


def _openai_api_key() -> str:
    secret_file = os.getenv("OPENAI_API_KEY_FILE", "").strip()
    if secret_file:
        try:
            value = Path(secret_file).read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            LOG.warning("Could not read the configured OpenAI API key file")
    return os.getenv("OPENAI_API_KEY", "").strip()


def _datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _agent_data(agent: Agent, now: datetime, offline_seconds: int, stream_offline_seconds: int, latest: Telemetry | None) -> dict[str, Any]:
    last_seen = _datetime(agent.last_seen_at)
    age = (now - last_seen).total_seconds() if last_seen else None
    observed_at = _datetime(latest.observed_at) if latest else None
    telemetry_age = (now - observed_at).total_seconds() if observed_at else None
    created = _datetime(agent.created_at)
    if (age is None and (now - created).total_seconds() > offline_seconds) or (age is not None and age > offline_seconds):
        state = "AGENT_OFFLINE"
    elif age is None:
        state = "NEVER_SEEN"
    else:
        metrics = latest.metrics if latest else {}
        if agent.role == "SERVER_INGRESS":
            media_age = metrics.get("last_ingress_progress_age", metrics.get("last_frame_age"))
        else:
            media_age = metrics.get("last_frame_age")
            if media_age is None:
                media_age = metrics.get("last_audio_frame_age")
        ffmpeg_running = metrics.get("ffmpeg_running")
        if telemetry_age is not None and telemetry_age > stream_offline_seconds:
            state = "TELEMETRY_STALE"
        elif ffmpeg_running is False:
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
        "last_seen_age_seconds": round(max(0.0, age), 1) if age is not None else None,
        "telemetry_observed_at": observed_at.isoformat() if observed_at else None,
        "telemetry_age_seconds": round(max(0.0, telemetry_age), 1) if telemetry_age is not None else None,
        "status": state,
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
    static_dir = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static_dir / "assets", check_dir=False), name="dashboard-assets")
    app.state.sessions = sessions
    app.state.admin_token = admin_token
    app.state.central_config = config
    app.state.openai_api_key = _openai_api_key()
    app.state.openai_model = os.getenv("OPENAI_MODEL", "gpt-6-luna").strip() or "gpt-6-luna"

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
        return FileResponse(static_dir / "index.html")

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
        agents = session.scalars(select(Agent).where(Agent.enabled.is_(True)).order_by(Agent.name)).all()
        latest = {item.id: session.scalar(select(Telemetry).where(Telemetry.agent_id == item.id).order_by(Telemetry.observed_at.desc()).limit(1)) for item in agents}
        return [_agent_data(item, now, config.agent_offline_seconds, config.stream_offline_seconds, latest.get(item.id)) for item in agents]

    @app.post("/api/v1/agents", status_code=201)
    def create_agent(data: AgentCreate, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        if not session.get(Stream, data.stream_id):
            raise HTTPException(status_code=404, detail="Stream not found")
        existing = session.scalar(select(Agent).where(Agent.name == data.name))
        if existing and existing.enabled:
            raise HTTPException(status_code=409, detail="Agent name already exists")
        token = secrets.token_urlsafe(36)
        if existing:
            agent = existing
            agent.location = data.location
            agent.platform = data.platform
            agent.role = data.role
            agent.stream_id = data.stream_id
            agent.token_hash = _token_hash(token)
            agent.enabled = True
        else:
            agent = Agent(
                id=str(uuid.uuid4()), name=data.name, location=data.location, platform=data.platform,
                role=data.role, stream_id=data.stream_id, token_hash=_token_hash(token),
            )
        session.add(agent)
        session.commit()
        return {"id": agent.id, "name": agent.name, "token": token, "role": agent.role, "stream_id": agent.stream_id}

    @app.post("/api/v2/probe-enrollments", status_code=201)
    def create_probe_enrollment(data: EnrollmentCreate, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        stream = session.get(Stream, data.stream_id)
        if not stream:
            raise HTTPException(status_code=404, detail="Stream not found")
        if data.role == "SERVER_INGRESS":
            raise HTTPException(status_code=422, detail="SERVER_INGRESS needs SRS API settings; use the existing manual configuration flow")
        stream_url = {
            "CLIENT": stream.public_url,
            "SERVER_EGRESS": stream.local_url,
            "SOURCE": stream.source_url,
        }[data.role]
        if not stream_url:
            raise HTTPException(status_code=422, detail=f"Stream has no URL configured for probe role {data.role}")

        now = utcnow()
        existing = session.scalar(select(Agent).where(Agent.name == data.name))
        if existing and existing.enabled and existing.last_seen_at is not None:
            raise HTTPException(status_code=409, detail="Probe name already exists")
        if existing:
            agent = existing
            agent.location = data.location
            agent.platform = data.platform
            agent.role = data.role
            agent.stream_id = data.stream_id
            agent.token_hash = _token_hash(secrets.token_urlsafe(36))
            agent.enabled = True
        else:
            agent = Agent(
                id=str(uuid.uuid4()), name=data.name, location=data.location, platform=data.platform,
                role=data.role, stream_id=data.stream_id, token_hash=_token_hash(secrets.token_urlsafe(36)),
            )
            session.add(agent)
        session.flush()

        # Only one outstanding code may provision a given probe. This avoids a later
        # redemption unexpectedly rotating the token of an already-installed probe.
        for pending in session.scalars(select(ProbeEnrollment).where(
            ProbeEnrollment.agent_id == agent.id,
            ProbeEnrollment.redeemed_at.is_(None),
            ProbeEnrollment.expires_at > now,
        )).all():
            pending.expires_at = now
        code = secrets.token_urlsafe(24)
        enrollment = ProbeEnrollment(
            id=str(uuid.uuid4()), agent_id=agent.id, code_hash=_token_hash(code),
            central_url=data.central_url, stream_url=stream_url, profile=data.profile,
            created_at=now, expires_at=now + timedelta(minutes=15),
        )
        session.add(enrollment)
        session.commit()
        return {"agent_id": agent.id, "name": agent.name, "code": code, "expires_at": enrollment.expires_at.isoformat(), "expires_in_seconds": 900}

    @app.post("/api/v2/probe-enrollments/redeem")
    def redeem_probe_enrollment(data: EnrollmentRedeem, session: Session = Depends(get_session)):
        enrollment = session.scalar(select(ProbeEnrollment).where(ProbeEnrollment.code_hash == _token_hash(data.code)))
        now = utcnow()
        if not enrollment or enrollment.redeemed_at is not None or _datetime(enrollment.expires_at) <= now:
            raise HTTPException(status_code=400, detail="Enrollment code is invalid, expired or already used")
        agent = session.get(Agent, enrollment.agent_id)
        if not agent or not agent.enabled:
            raise HTTPException(status_code=400, detail="Enrollment code is invalid, expired or already used")

        # The conditional update makes redemption single-use even when two installers
        # submit the same code at nearly the same time.
        consumed = session.execute(update(ProbeEnrollment).where(
            ProbeEnrollment.id == enrollment.id,
            ProbeEnrollment.redeemed_at.is_(None),
            ProbeEnrollment.expires_at > now,
        ).values(redeemed_at=now).execution_options(synchronize_session=False))
        if consumed.rowcount != 1:
            session.rollback()
            raise HTTPException(status_code=400, detail="Enrollment code is invalid, expired or already used")

        token = secrets.token_urlsafe(36)
        agent.token_hash = _token_hash(token)
        config_data = {
            "server": {"url": enrollment.central_url},
            "agent": {
                "id": agent.id, "name": agent.name, "location": agent.location,
                "role": agent.role, "token": token, "profile": enrollment.profile,
            },
            "streams": [{"id": agent.stream_id, "url": enrollment.stream_url, "role": agent.role}],
        }
        session.commit()
        return {"agent_id": agent.id, "config": config_data}

    @app.post("/api/v1/agents/{agent_id}/rotate-token")
    def rotate_agent_token(agent_id: str, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        agent = session.get(Agent, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        token = secrets.token_urlsafe(36)
        agent.token_hash = _token_hash(token)
        session.commit()
        return {"id": agent.id, "token": token}

    @app.delete("/api/v1/agents/{agent_id}")
    def disable_agent(agent_id: str, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        agent = session.get(Agent, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        agent.enabled = False
        offline = session.scalar(select(Incident).where(
            Incident.fingerprint == f"agent:{agent.id}:offline", Incident.active.is_(True)
        ))
        if offline:
            offline.active = False
            offline.resolved_at = utcnow()
        correlate_stream(session, agent.stream_id)
        session.commit()
        return {"id": agent.id, "enabled": False, "history_preserved": True}

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
        agents_query = select(Agent).where(Agent.enabled.is_(True)).order_by(Agent.name)
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
            "openai_explanations_available": bool(app.state.openai_api_key),
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

    @app.post("/api/v1/incidents/{incident_id}/explanation")
    def explain_incident(incident_id: str, _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        incident = session.get(Incident, incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        updated_at = _datetime(incident.updated_at).isoformat()
        context = incident.context if isinstance(incident.context, dict) else {}
        cached = context.get("explanation_cache")
        if (
            app.state.openai_api_key
            and isinstance(cached, dict)
            and cached.get("updated_at") == updated_at
            and cached.get("model") == app.state.openai_model
        ):
            return {**cached["result"], "ai_status": "cached"}

        opened = _datetime(incident.opened_at)
        related_query = select(Incident).where(
            Incident.stream_id == incident.stream_id,
            Incident.id != incident.id,
            Incident.opened_at >= opened - timedelta(minutes=10),
            Incident.opened_at <= opened + timedelta(minutes=10),
        ).order_by(Incident.opened_at.asc()).limit(100)
        related = session.scalars(related_query).all()
        packet = build_evidence_packet(incident, related)
        result = deterministic_explanation(packet)
        result["evidence"] = packet["evidence"]
        status = "not_configured"
        if app.state.openai_api_key:
            try:
                result = openai_explanation(packet, app.state.openai_api_key, app.state.openai_model)
                status = "generated"
                incident.context = {
                    **context,
                    "explanation_cache": {
                        "updated_at": updated_at,
                        "model": app.state.openai_model,
                        "result": result,
                    },
                }
                session.commit()
            except RuntimeError as exc:
                LOG.warning("OpenAI incident explanation unavailable: %s", exc)
                status = "unavailable"
        result["ai_status"] = status
        return result

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

    @app.get("/api/v1/timeline")
    def timeline(stream_id: str, hours: int = Query(default=6, ge=1, le=168), _admin: bool = Depends(require_admin), session: Session = Depends(get_session)):
        if not session.get(Stream, stream_id):
            raise HTTPException(status_code=404, detail="Stream not found")
        cutoff = utcnow() - timedelta(hours=hours)
        conditions = (Telemetry.stream_id == stream_id, Telemetry.observed_at >= cutoff)
        probe_count = session.scalar(
            select(func.count(func.distinct(Telemetry.agent_id))).where(*conditions)
        ) or 0
        bucket_seconds = max(10, ceil(hours * 3600 * max(probe_count, 1) / 50_000))
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            epoch = cast(func.strftime("%s", Telemetry.observed_at), Float)
            def network_metric(name: str):
                return cast(func.json_extract(Telemetry.metrics, f"$.network.{name}"), Float)
        elif dialect == "postgresql":
            epoch = func.extract("epoch", Telemetry.observed_at)
            def network_metric(name: str):
                return cast(Telemetry.metrics["network"][name].as_string(), Float)
        else:
            raise HTTPException(status_code=501, detail=f"Timeline sampling is not supported for database dialect {dialect}")

        bucket = func.floor(epoch / bucket_seconds)
        status_rank = case(
            (Telemetry.status.in_(("CRITICAL", "ERROR", "STREAM_OFFLINE", "STREAM_STALLED", "AGENT_OFFLINE")), 2),
            (Telemetry.status == "WARNING", 1),
            else_=0,
        )
        ranked_status = select(
            Telemetry.id.label("telemetry_id"),
            func.row_number().over(
                partition_by=(Telemetry.agent_id, bucket),
                order_by=(status_rank.desc(), Telemetry.observed_at.desc()),
            ).label("sample_rank"),
        ).where(*conditions).subquery()
        sampled_ids = select(ranked_status.c.telemetry_id).where(ranked_status.c.sample_rank == 1)

        retransmits = network_metric("tcp_retransmissions")
        duplicate_ack_episodes = network_metric("tcp_duplicate_ack_episodes")
        packet_loss = network_metric("packet_loss_percent")
        rtt = network_metric("rtt_ms")
        network_problem = or_(retransmits > 0, duplicate_ack_episodes > 0, packet_loss >= 50, rtt > 100)
        network_score = case((network_problem, 1), else_=0)
        ranked_network = select(
            Telemetry.id.label("telemetry_id"),
            func.row_number().over(
                partition_by=(Telemetry.agent_id, bucket),
                order_by=(network_score.desc(), Telemetry.observed_at.desc()),
            ).label("sample_rank"),
        ).where(*conditions, network_problem).subquery()
        network_ids = select(ranked_network.c.telemetry_id).where(ranked_network.c.sample_rank == 1)
        event_ids = select(Telemetry.id).where(*conditions, func.json_array_length(Telemetry.events) > 0)
        selected_ids = union(sampled_ids, network_ids, event_ids)
        rows = session.execute(
            select(Telemetry, Agent).join(Agent, Telemetry.agent_id == Agent.id)
            .where(Telemetry.stream_id == stream_id, Telemetry.id.in_(selected_ids))
            .order_by(Telemetry.observed_at.asc())
        ).all()
        return [
            {"timestamp": _datetime(item.observed_at).isoformat(), "agent": agent.name, "role": agent.role,
             "status": item.status, "metrics": item.metrics, "events": item.events}
            for item, agent in rows
        ]

    @app.get("/api/v2/streams/{stream_id}/series")
    def stream_series(
        stream_id: str,
        from_: datetime = Query(alias="from"),
        to: datetime = Query(),
        resolution: str = Query(default="1s", pattern=r"^(1s|5s|10s|1m|5m|1h)$"),
        probe_ids: list[str] | None = Query(default=None),
        _admin: bool = Depends(require_admin),
        session: Session = Depends(get_session),
    ):
        stream = session.get(Stream, stream_id)
        if not stream:
            raise HTTPException(status_code=404, detail="Stream not found")
        start, end = _datetime(from_), _datetime(to)
        assert start is not None and end is not None
        if end <= start:
            raise HTTPException(status_code=422, detail="'from' must be earlier than 'to'")
        duration = (end - start).total_seconds()
        if duration > 90 * 86400:
            raise HTTPException(status_code=422, detail="Series range cannot exceed 90 days")
        requested_seconds = {"1s": 1, "5s": 5, "10s": 10, "1m": 60, "5m": 300, "1h": 3600}[resolution]
        bucket_seconds = max(requested_seconds, ceil(duration / 9998))

        agents_query = select(Agent).where(Agent.stream_id == stream_id).order_by(Agent.name)
        if probe_ids:
            agents_query = agents_query.where(Agent.id.in_(probe_ids))
        agents = session.scalars(agents_query).all()
        if probe_ids and len(agents) != len(set(probe_ids)):
            raise HTTPException(status_code=404, detail="One or more probes were not found for this stream")
        by_id = {agent.id: agent for agent in agents}
        if not agents:
            return {
                "stream_id": stream_id, "from": start.isoformat(), "to": end.isoformat(),
                "actual_resolution_seconds": bucket_seconds, "generated_at": utcnow().isoformat(), "series": [],
            }

        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            def epoch(column):
                return cast(func.strftime("%s", column), Float)
            def json_value(column, path: str):
                return func.json_extract(column, path)
        elif dialect == "postgresql":
            def epoch(column):
                return func.extract("epoch", column)
            def json_value(column, path: str):
                keys = path.removeprefix("$.").split(".")
                value = column
                for key in keys:
                    value = value[key]
                return value.as_string()
        else:
            raise HTTPException(status_code=501, detail=f"Series sampling is not supported for database dialect {dialect}")

        bitrate_json = json_value(Telemetry.metrics, "$.received_media_bitrate_bps")
        bitrate = cast(bitrate_json, Float)
        quality = json_value(Telemetry.metrics, "$.received_media_bitrate_quality")
        window = cast(json_value(Telemetry.metrics, "$.measurement_window_seconds"), Float)
        bucket_number = func.floor(epoch(Telemetry.observed_at) / bucket_seconds)
        retention_cutoff = utcnow() - timedelta(days=config.raw_retention_days)
        # Retention moves complete minute buckets; use the same UTC boundary so
        # series queries do not create a hole between raw and rolled-up data.
        raw_limit = datetime.fromtimestamp(int(retention_cutoff.timestamp() // 60 * 60), timezone.utc)
        accumulators: dict[str, dict[int, dict[str, Any]]] = {agent.id: {} for agent in agents}
        gap_hints: dict[str, dict[int, str]] = {agent.id: {} for agent in agents}

        raw_start = max(start, raw_limit)
        if raw_start < end:
            raw_conditions = (
                Telemetry.stream_id == stream_id,
                Telemetry.agent_id.in_(by_id),
                Telemetry.observed_at >= raw_start,
                Telemetry.observed_at < end,
            )
            measured_at = func.max(case((bitrate.is_not(None), Telemetry.observed_at)))
            raw_rows = session.execute(
                select(
                    Telemetry.agent_id,
                    bucket_number.label("bucket_number"),
                    func.min(bitrate).label("min_bps"),
                    func.avg(bitrate).label("avg_bps"),
                    func.max(bitrate).label("max_bps"),
                    func.count(bitrate).label("sample_count"),
                    func.count(Telemetry.id).label("row_count"),
                    func.avg(window).label("window_avg"),
                    measured_at.label("last_observed_at"),
                    func.sum(case((quality == "MEASUREMENT_WARMUP", 1), else_=0)).label("warmup_count"),
                    func.sum(case((quality == "MEASUREMENT_UNAVAILABLE", 1), else_=0)).label("unavailable_count"),
                ).where(*raw_conditions).group_by(Telemetry.agent_id, bucket_number)
            ).all()
            for row in raw_rows:
                index = int(row.bucket_number)
                if row.sample_count:
                    accumulators[row.agent_id][index] = {
                        "min_bps": float(row.min_bps), "avg_bps": float(row.avg_bps), "max_bps": float(row.max_bps),
                        "sample_count": int(row.sample_count), "expected_count": max(1, ceil(bucket_seconds)),
                        "window_avg": float(row.window_avg or 1.0),
                        "last_observed_at": _datetime(row.last_observed_at),
                    }
                elif row.row_count:
                    gap_hints[row.agent_id][index] = "MEASUREMENT_WARMUP" if row.warmup_count else "MEASUREMENT_UNAVAILABLE"

        aggregate_end = min(end, raw_start)
        if start < aggregate_end:
            aggregate_epoch = epoch(MetricAggregate.bucket_start)
            aggregate_bucket = func.floor(aggregate_epoch / bucket_seconds)
            value_path = "$.received_media_bitrate_bps"
            aggregate_min = cast(json_value(MetricAggregate.metrics, f"{value_path}.min"), Float)
            aggregate_avg = cast(json_value(MetricAggregate.metrics, f"{value_path}.avg"), Float)
            aggregate_max = cast(json_value(MetricAggregate.metrics, f"{value_path}.max"), Float)
            aggregate_count = cast(json_value(MetricAggregate.metrics, f"{value_path}.count"), Float)
            aggregate_window = cast(json_value(MetricAggregate.metrics, "$.measurement_window_seconds.avg"), Float)
            aggregate_last = json_value(MetricAggregate.metrics, f"{value_path}.last_observed_at")
            agg_rows = session.execute(
                select(
                    MetricAggregate.agent_id,
                    aggregate_bucket.label("bucket_number"),
                    func.min(aggregate_min).label("min_bps"),
                    (func.sum(aggregate_avg * aggregate_count) / func.sum(aggregate_count)).label("avg_bps"),
                    func.max(aggregate_max).label("max_bps"),
                    func.sum(aggregate_count).label("sample_count"),
                    (func.sum(aggregate_window * aggregate_count) / func.sum(aggregate_count)).label("window_avg"),
                    func.max(aggregate_last).label("last_observed_at"),
                ).where(
                    MetricAggregate.stream_id == stream_id,
                    MetricAggregate.agent_id.in_(by_id),
                    MetricAggregate.bucket_start >= start,
                    MetricAggregate.bucket_start < aggregate_end,
                    aggregate_count.is_not(None),
                    aggregate_count > 0,
                ).group_by(MetricAggregate.agent_id, aggregate_bucket)
            ).all()
            for row in agg_rows:
                index = int(row.bucket_number)
                count = int(row.sample_count)
                accumulators[row.agent_id][index] = {
                    "min_bps": float(row.min_bps), "avg_bps": float(row.avg_bps), "max_bps": float(row.max_bps),
                    "sample_count": count, "expected_count": max(1, ceil(bucket_seconds)),
                    "window_avg": float(row.window_avg or 1.0),
                    "last_observed_at": _parse_iso_datetime(row.last_observed_at),
                }

        response_series = []
        first_bucket = int(start.timestamp() // bucket_seconds)
        last_bucket_exclusive = ceil(end.timestamp() / bucket_seconds)
        expected_buckets = range(first_bucket, last_bucket_exclusive)
        for agent in agents:
            latest = session.scalar(
                select(Telemetry).where(Telemetry.agent_id == agent.id)
                .order_by(Telemetry.observed_at.desc()).limit(1)
            )
            latest_metrics = latest.metrics if latest else {}
            sample_interval = float(latest_metrics.get("sample_interval_seconds", 1.0) or 1.0)
            points = []
            gaps = []
            open_gap: dict[str, Any] | None = None
            point_buckets = accumulators[agent.id]
            for index in expected_buckets:
                bucket_start = datetime.fromtimestamp(index * bucket_seconds, timezone.utc)
                bucket_end = bucket_start + timedelta(seconds=bucket_seconds)
                if bucket_end <= start or bucket_start >= end:
                    continue
                expected_count = max(1, ceil((min(bucket_end, end) - max(bucket_start, start)).total_seconds() / sample_interval))
                point = point_buckets.get(index)
                if point:
                    points.append({
                        "timestamp": bucket_start.isoformat(), "bucket_seconds": bucket_seconds,
                        "min_bps": round(point["min_bps"]), "avg_bps": round(point["avg_bps"]),
                        "max_bps": round(point["max_bps"]), "sample_count": point["sample_count"],
                        "expected_count": expected_count,
                        "quality": "MEASURED" if point["sample_count"] >= expected_count else "PARTIAL",
                        "measurement_window_seconds_avg": round(point["window_avg"], 3),
                        "last_observed_at": point["last_observed_at"].isoformat() if point["last_observed_at"] else bucket_start.isoformat(),
                    })
                    reason = None
                else:
                    reason = gap_hints[agent.id].get(index)
                    last_seen = _datetime(agent.last_seen_at)
                    if reason is None and last_seen and bucket_end > last_seen + timedelta(seconds=config.agent_offline_seconds):
                        reason = "PROBE_OFFLINE"
                    reason = reason or "NO_SAMPLE"
                if reason is None:
                    if open_gap:
                        gaps.append(open_gap)
                        open_gap = None
                    continue
                if open_gap and open_gap["reason"] == reason and open_gap["to"] == bucket_start.isoformat():
                    open_gap["to"] = min(bucket_end, end).isoformat()
                else:
                    if open_gap:
                        gaps.append(open_gap)
                    open_gap = {"from": max(bucket_start, start).isoformat(), "to": min(bucket_end, end).isoformat(), "reason": reason}
            if open_gap:
                gaps.append(open_gap)
            response_series.append({
                "probe": {"id": agent.id, "name": agent.name, "role": agent.role, "profile": latest_metrics.get("profile", "unknown"), "platform": agent.platform},
                "metric": "received_media_bitrate_bps", "unit": "bps", "points": points, "gaps": gaps,
            })

        return {
            "stream_id": stream_id, "from": start.isoformat(), "to": end.isoformat(),
            "actual_resolution_seconds": bucket_seconds, "generated_at": utcnow().isoformat(), "series": response_series,
        }

    @app.get("/api/v2/streams/{stream_id}/events")
    def stream_events(
        stream_id: str,
        from_: datetime = Query(alias="from"),
        to: datetime = Query(),
        probe_ids: list[str] | None = Query(default=None),
        severity: str | None = Query(default=None, pattern=r"^(INFO|WARNING|CRITICAL)$"),
        _admin: bool = Depends(require_admin),
        session: Session = Depends(get_session),
    ):
        if not session.get(Stream, stream_id):
            raise HTTPException(status_code=404, detail="Stream not found")
        start, end = _datetime(from_), _datetime(to)
        assert start is not None and end is not None
        if end <= start:
            raise HTTPException(status_code=422, detail="'from' must be earlier than 'to'")
        if (end - start).total_seconds() > 90 * 86400:
            raise HTTPException(status_code=422, detail="Events range cannot exceed 90 days")
        agents_query = select(Agent).where(Agent.stream_id == stream_id)
        if probe_ids:
            agents_query = agents_query.where(Agent.id.in_(probe_ids))
        agents = session.scalars(agents_query).all()
        agent_by_id = {agent.id: agent for agent in agents}
        if probe_ids and len(agents) != len(set(probe_ids)):
            raise HTTPException(status_code=404, detail="One or more probes were not found for this stream")
        events: list[dict[str, Any]] = []
        if agents:
            rows = session.execute(
                select(Telemetry, Agent).join(Agent, Telemetry.agent_id == Agent.id)
                .where(
                    Telemetry.stream_id == stream_id, Telemetry.agent_id.in_(agent_by_id),
                    Telemetry.observed_at >= start, Telemetry.observed_at < end,
                    func.json_array_length(Telemetry.events) > 0,
                ).order_by(Telemetry.observed_at.asc()).limit(10000)
            ).all()
            for item, agent in rows:
                for event in item.events or []:
                    code = str(event.get("code", "PROBE_EVENT"))
                    event_severity = str(event.get("severity", "INFO")).upper()
                    if severity and event_severity != severity:
                        continue
                    details = event.get("details") if isinstance(event.get("details"), dict) else {}
                    occurred_at = _parse_iso_datetime(event.get("timestamp")) or _datetime(item.observed_at)
                    safe_details = _safe_event_details(details)
                    evidence = _probe_event_evidence(agent, item, safe_details, occurred_at)
                    events.append({
                        "id": f"telemetry:{item.id}:{code}", "kind": "probe_event", "stream_id": stream_id,
                        "probe_id": agent.id, "probe_name": agent.name, "role": agent.role, "code": code,
                        "severity": event_severity if event_severity in {"INFO", "WARNING", "CRITICAL"} else "INFO",
                        "started_at": occurred_at.isoformat(), "ended_at": None,
                        "state": "ACTIVE" if code.endswith("_START") or code == "KEYFRAME_GAP" else "RESOLVED",
                        "summary": _probe_event_summary(code),
                        "explanation": _probe_event_explanation(code, agent.role, safe_details, item.metrics or {}),
                        "confidence": "UNCONFIRMED", "probable_location": None, "evidence": evidence,
                    })
        end_to_start = {"FREEZE_END": "FREEZE_START", "SILENCE_END": "SILENCE_START", "KEYFRAME_GAP_END": "KEYFRAME_GAP"}
        open_probe_events: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
        for event in sorted(events, key=lambda item: item["started_at"]):
            if event["kind"] != "probe_event":
                continue
            if event["code"] in end_to_start:
                key = (event["probe_id"], end_to_start[event["code"]])
                matching = open_probe_events.get(key)
                if matching:
                    start_event = matching.pop(0)
                    start_event["ended_at"] = event["started_at"]
                    start_event["state"] = "RESOLVED"
            elif event["state"] == "ACTIVE":
                open_probe_events.setdefault((event["probe_id"], event["code"]), []).append(event)
        incidents = session.scalars(
            select(Incident).where(
                Incident.stream_id == stream_id,
                Incident.opened_at < end,
                or_(Incident.resolved_at.is_(None), Incident.resolved_at >= start),
            ).order_by(Incident.opened_at.asc()).limit(10000)
        ).all()
        incident_timestamps = [_datetime(incident.opened_at).timestamp() for incident in incidents]
        selected_names = {agent.name for agent in agents}
        for incident in incidents:
            affected = set(incident.affected_agents or [])
            if probe_ids and affected and not affected.intersection(selected_names):
                continue
            if severity and incident.severity.upper() != severity:
                continue
            incident_timestamp = _datetime(incident.opened_at).timestamp()
            near_start = bisect_left(incident_timestamps, incident_timestamp - 600)
            near_end = bisect_right(incident_timestamps, incident_timestamp + 600)
            related = sorted(
                (other for other in incidents[near_start:near_end] if other.id != incident.id),
                key=lambda other: abs((_datetime(other.opened_at).timestamp() - incident_timestamp)),
            )[:20]
            evidence_packet = build_evidence_packet(incident, related)
            explanation = deterministic_explanation(evidence_packet)
            evidence_by_id = {item["id"]: item for item in evidence_packet["evidence"]}
            selected_evidence = [evidence_by_id[item_id] for item_id in explanation.get("evidence_ids", []) if item_id in evidence_by_id]
            evidence = [{
                "id": item["id"], "probe_id": None, "metric": f"incident.{item['category']}",
                "observed_at": None, "value": item["fact"], "unit": None, "comparison": None,
                "fact": item["fact"],
            } for item in selected_evidence]
            confidence = {"low": "UNCONFIRMED", "medium": "LIKELY", "high": "CONFIRMED"}.get(explanation["confidence"], "UNCONFIRMED")
            events.append({
                "id": incident.id, "kind": "incident", "stream_id": stream_id,
                "probe_id": None, "probe_name": ", ".join(sorted(affected)) or None, "role": None,
                "code": incident.diagnosis, "severity": incident.severity.upper(),
                "started_at": _datetime(incident.opened_at).isoformat(),
                "ended_at": _datetime(incident.resolved_at).isoformat() if incident.resolved_at else None,
                "state": "ACTIVE" if incident.active else "RESOLVED",
                "summary": _incident_summary(incident.diagnosis),
                "explanation": explanation["likely_cause"], "cause_key": explanation["cause_key"], "confidence": confidence,
                "probable_location": incident.probable_location, "evidence": evidence,
                "evidence_ids": explanation.get("evidence_ids", []),
                "other_possible_causes": explanation.get("other_possible_causes", []),
                "next_checks": explanation.get("next_checks", []),
                "explanation_source": "deterministic_rules",
                "ai_explanation_available": bool(app.state.openai_api_key),
            })
        events.sort(key=lambda item: item["started_at"])
        return {"stream_id": stream_id, "from": start.isoformat(), "to": end.isoformat(), "generated_at": utcnow().isoformat(), "events": events[:10000]}

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


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _datetime(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _probe_event_summary(code: str) -> str:
    labels = {
        "FREEZE_START": "Відео завмерло.",
        "FREEZE_DURATION": "Виміряно тривалість завмирання відео.",
        "FREEZE_END": "Відео знову рухається.",
        "SILENCE_START": "В аудіо виявлено тишу.",
        "SILENCE_DURATION": "Виміряно тривалість тиші в аудіо.",
        "SILENCE_END": "Аудіо відновилося.",
        "DECODE_ERROR": "Декодер повідомив про помилку.",
        "BITSTREAM_PARSE_ERROR": "ffprobe не зміг розібрати медіапакет.",
        "PTS_REGRESSION": "Часова позначка відео пішла назад.",
        "DTS_REGRESSION": "Часова позначка пакета пішла назад.",
        "PTS_JUMP": "Виявлено стрибок часової позначки PTS.",
        "KEYFRAME_GAP": "Інтервал між ключовими кадрами перевищив очікуваний.",
        "KEYFRAME_GAP_END": "Надходження ключових кадрів відновилося.",
        "STREAM_STALL": "Probe перестав бачити нові медіадані.",
        "PROGRESS_STALE": "FFmpeg довго не звітував про поступ.",
        "FFMPEG_DEAD": "Процес FFmpeg перестав передавати медіадані та звіт про роботу.",
        "FFMPEG_EXIT": "Процес FFmpeg завершився.",
        "FFMPEG_RESTART": "Probe перезапустив FFmpeg.",
        "PROBE_ERROR": "Агент повідомив про власну помилку.",
        "AGENT_OFFLINE": "Probe не надсилає телеметрію.",
        "STREAM_OFFLINE": "На цій точці потік позначено як недоступний.",
        "AV_TIMESTAMP_DRIFT": "Часові позначки аудіо й відео розійшлися.",
        "TCP_RETRANSMISSION": "Мережевий probe зафіксував повторні передачі TCP.",
        "TCP_RESET": "Мережевий probe зафіксував скидання TCP-з'єднання.",
        "CONNECTION_RESET": "Мережевий probe зафіксував розрив з'єднання.",
        "PACKET_LOSS": "Мережевий probe зафіксував втрату ICMP-пакетів.",
        "RTT_SPIKE": "Мережевий probe зафіксував підвищений RTT.",
        "SRS_PUBLISH_STATE_UNAVAILABLE": "SRS не надав стан публікації потоку.",
        "SRS_COUNTERS_UNAVAILABLE": "SRS не надав лічильники руху медіаданих.",
        "SRS_API_UNAVAILABLE": "Probe не зміг отримати дані від API SRS.",
        "INGRESS_RECOVERED": "Спостереження за входом SRS відновилося.",
    }
    return labels.get(code, "Probe повідомив про подію, для якої ще немає окремого опису.")


_SAFE_EVENT_DETAIL_KEYS = {
    "pts", "previous_pts", "dts", "previous_dts", "value", "gap_seconds", "frame_age_seconds",
    "decode_errors", "reconnect_count", "rtt_ms", "packet_loss_percent", "tcp_retransmissions",
    "last_frame_age", "last_frame_age_seconds", "last_audio_age_seconds", "age_seconds", "delta_seconds",
    "duration_seconds", "seconds_without_keyframe", "threshold_seconds", "expected_gop_seconds",
    "expected_gop_frames", "progress_age_seconds", "media_age_seconds", "seconds_without_ingress_progress",
    "stream_index", "return_code",
}


def _safe_event_details(details: dict[str, Any]) -> dict[str, int | float]:
    safe = {}
    for key in _SAFE_EVENT_DETAIL_KEYS:
        value = details.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            continue
        safe[key] = value
    return safe


def _probe_event_evidence(agent: Agent, item: Telemetry, details: dict[str, int | float], observed_at: datetime) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    units = {
        "previous_pts": "s", "pts": "s", "previous_dts": "s", "dts": "s", "value": "s",
        "gap_seconds": "s", "duration_seconds": "s", "frame_age_seconds": "s", "last_frame_age": "s",
        "last_frame_age_seconds": "s", "last_audio_age_seconds": "s", "age_seconds": "s", "delta_seconds": "s", "rtt_ms": "ms",
        "seconds_without_keyframe": "s", "threshold_seconds": "s", "expected_gop_seconds": "s",
        "expected_gop_frames": "frames", "progress_age_seconds": "s", "media_age_seconds": "s",
        "seconds_without_ingress_progress": "s",
        "packet_loss_percent": "%", "tcp_retransmissions": "count", "decode_errors": "count",
        "reconnect_count": "count", "return_code": "code",
    }

    def add(metric: str, value: Any, unit: str | None, comparison: str | None = None) -> None:
        if not isinstance(value, (int, float, str, bool)):
            return
        if isinstance(value, float) and not isfinite(value):
            return
        evidence.append({
            "probe_id": agent.id, "metric": metric, "observed_at": observed_at.isoformat(),
            "value": value, "unit": unit, "comparison": comparison,
        })

    for key, value in details.items():
        add(f"event.details.{key}", value, units.get(key))
    metrics = item.metrics if isinstance(item.metrics, dict) else {}
    media_bitrate = metrics.get("received_media_bitrate_bps")
    if metrics.get("received_media_bitrate_quality") == "MEASURED" and isinstance(media_bitrate, (int, float)) and not isinstance(media_bitrate, bool):
        add("received_media_bitrate_bps", media_bitrate, "bps", f"window={metrics.get('measurement_window_seconds', 1)}s")
    for key, unit in (
        ("last_frame_age", "s"), ("last_audio_frame_age", "s"), ("decode_errors", "count"),
        ("last_ingress_progress_age", "s"), ("ingress_recv_kbps_30s", "kbps"),
        ("ingress_recv_bytes", "bytes"), ("ingress_frames", "count"),
        ("srs_api_available", None), ("ingress_active", None),
    ):
        add(key, metrics.get(key), unit)
    network = metrics.get("network")
    if isinstance(network, dict):
        for key, unit in (
            ("tcp_retransmissions", "count"), ("tcp_duplicate_ack_episodes", "count"),
            ("tcp_duplicate_acks", "count"), ("rtt_ms", "ms"),
            ("packet_loss_percent", "%"), ("tcp_state", None), ("provider", None),
        ):
            add(f"network.{key}", network.get(key), unit)
    return evidence


def _probe_event_explanation(code: str, role: str, details: dict[str, int | float], metrics: dict[str, Any]) -> str:
    point = {
        "SOURCE": "джерелі",
        "SERVER_INGRESS": "вході RTMP-сервера",
        "SERVER_EGRESS": "виході RTMP-сервера",
        "CLIENT": "клієнті",
    }.get(role, "точці спостереження")
    if code == "FREEZE_DURATION":
        duration = details.get("duration_seconds")
        if duration is not None:
            return (f"На {point} FFmpeg виміряв завмирання відео тривалістю {duration:g} с. "
                    "Це підтверджує тривалість симптому в цій точці, але не визначає, де він виник.")
        return f"{_probe_event_summary(code)} На цій точці зафіксовано тривалість симптому, але місце його виникнення невідоме."
    if code in {"SILENCE_DURATION", "SILENCE_END"} and "duration_seconds" in details:
        ending = "Аудіо відновилося" if code == "SILENCE_END" else "Probe виміряв тишу"
        return (f"На {point} {ending} після тиші тривалістю {details['duration_seconds']:g} с. "
                "Це вимірює симптом у цій точці, але не визначає, де виникла причина.")
    if code == "SILENCE_DURATION":
        return f"{_probe_event_summary(code)} Причина тиші на цій точці не встановлена."
    if code == "KEYFRAME_GAP":
        age = details.get("seconds_without_keyframe")
        threshold = details.get("threshold_seconds")
        expected = details.get("expected_gop_seconds")
        facts = []
        if age is not None:
            facts.append(f"ключового кадру не було {age:g} с")
        if threshold is not None:
            facts.append(f"поріг становить {threshold:g} с")
        if expected is not None:
            facts.append(f"звичний інтервал GOP — {expected:g} с")
        evidence = f" ({'; '.join(facts)})" if facts else ""
        return (f"На {point} зафіксовано завеликий інтервал між ключовими кадрами{evidence}. "
                "Це може заважати декодуванню, але без сусідніх probe не визначає місце виникнення проблеми.")
    if code == "KEYFRAME_GAP_END":
        return f"На {point} знову надійшов ключовий кадр. Це позначає кінець інтервалу, але не встановлює його причину."
    if code == "PTS_REGRESSION" and "previous_pts" in details and "pts" in details:
        return (f"На {point} PTS зменшився з {details['previous_pts']:g} до {details['pts']:g} с. "
                "Це підтверджує збій часових позначок у спостереженому потоці, але без одночасного порівняння джерела, входу й виходу RTMP-сервера не визначає, де він виник.")
    if code == "DTS_REGRESSION" and "previous_dts" in details and "dts" in details:
        return (f"На {point} DTS зменшився з {details['previous_dts']:g} до {details['dts']:g} с. "
                "Probe побачив порушення послідовності пакетів; цієї точки недостатньо, щоб встановити джерело проблеми.")
    if code == "PTS_JUMP" and "delta_seconds" in details:
        return (f"На {point} PTS стрибнув на {details['delta_seconds']:g} с. Це вказує на розрив часових позначок, "
                "але саме по собі не визначає джерело проблеми.")
    if code == "AV_TIMESTAMP_DRIFT" and "delta_seconds" in details:
        return (f"На {point} часові позначки аудіо й відео розійшлися на {details['delta_seconds']:g} с. "
                "Це підтверджує розсинхронізацію в точці спостереження, але не визначає, де вона виникла.")
    if code == "DECODE_ERROR":
        return f"FFmpeg на {point} повідомив про помилку декодування. Це локалізує симптом у цій точці, але не доводить мережеву чи серверну причину."
    if code == "BITSTREAM_PARSE_ERROR":
        return (f"ffprobe на {point} не зміг розібрати медіапакет; профіль LIGHT не декодує кадри. "
                "Це підтверджує проблему читання бітстріму в цій точці, але не визначає, де саме пакет пошкодився.")
    if code in {"FREEZE_START", "FREEZE_END", "SILENCE_START", "SILENCE_END", "AUDIO_MISSING"}:
        return f"{_probe_event_summary(code)} Це спостережено на {point}; без одночасних даних із сусідніх probe місце виникнення причини невідоме."
    if code == "STREAM_STALL":
        age = details.get("last_media_age_seconds", details.get("seconds_without_ingress_progress"))
        threshold = details.get("threshold_seconds")
        evidence = f" Нових медіаданих не було {age:g} с." if age is not None else ""
        if threshold is not None:
            evidence += f" Поріг спрацювання — {threshold:g} с."
        return f"{_probe_event_summary(code)} На {point}.{evidence} Це стан спостереження, а не доказ, де саме зупинився тракт."
    if code == "STREAM_OFFLINE":
        if role == "SERVER_INGRESS":
            return "SRS не показав активну публікацію цього потоку на вході сервера. Це підтверджує відсутність активного publish у відповіді SRS, але не пояснює причину."
        return f"Потік позначено недоступним на {point}. Це не визначає, чи проблема виникла вище за течією, у мережі або в самій точці."
    if code in {"SRS_API_UNAVAILABLE", "SRS_COUNTERS_UNAVAILABLE", "SRS_PUBLISH_STATE_UNAVAILABLE"}:
        return (f"{_probe_event_summary(code)} Це обмежує спостереження за входом SRS; стан медіа на вході невідомий "
                "і подія сама по собі не доводить збій потоку.")
    if code == "INGRESS_RECOVERED":
        return "Probe знову отримує дані для спостереження за входом SRS. Відновлення API/лічильників не підтверджує декодування медіа."
    if code in {"FFMPEG_DEAD", "FFMPEG_EXIT", "FFMPEG_RESTART", "PROGRESS_STALE", "PROBE_ERROR", "AGENT_OFFLINE"}:
        age = details.get("progress_age_seconds", details.get("age_seconds"))
        code_value = details.get("return_code")
        evidence = f" FFmpeg не звітував {age:g} с." if age is not None else ""
        if code_value is not None:
            evidence += f" Код завершення процесу: {code_value:g}."
        return (f"{_probe_event_summary(code)} Це проблема процесу спостереження на {point}.{evidence} "
                "Стан самого потоку з цієї події невідомий.")
    if code == "TCP_RETRANSMISSION":
        provider = (metrics.get("network") or {}).get("provider") if isinstance(metrics.get("network"), dict) else None
        if provider == "windows":
            return "Windows зафіксував retransmits на хості загалом; лічильник не прив'язаний до RTMP-з'єднання, тому не доводить збій цього потоку."
        return f"TCP probe зафіксував повторні передачі на {point}. Це мережевий симптом; щоб пов'язати його зі збоєм медіа, треба зіставити час із сусідніми probe."
    if code == "PACKET_LOSS":
        return f"Probe зафіксував втрату ICMP-відповідей до хоста з боку {point}. Це сигнал доступності/затримки, а не прямий вимір втрати RTMP-медіапакетів."
    if code in {"RTT_SPIKE", "TCP_RESET", "CONNECTION_RESET"}:
        return f"{_probe_event_summary(code)} Це мережевий симптом біля {point}, але подія сама не встановлює, чи він спричинив медіазбій."
    return f"{_probe_event_summary(code)} Це прямий запис probe, але сам маркер не доводить кореневу причину."


def _incident_summary(diagnosis: str) -> str:
    labels = {
        "CLIENT_PROBLEM": "Клієнтський probe зафіксував проблему приймання або обробки медіаданих.",
        "CLIENT_PATH_UNCONFIRMED": "Клієнтський probe зафіксував проблему, але стан виходу сервера невідомий.",
        "NETWORK_PATH_PROBLEM": "Клієнтський симптом збігся з мережевою ознакою на шляху доставки.",
        "NETWORK_PATH_UNCONFIRMED": "Є мережевий сигнал біля клієнта, але його зв'язок із RTMP-потоком не доведений.",
        "SOURCE_OR_INGEST_PROBLEM": "Медіапроблему зафіксовано на джерелі або вході сервера.",
        "SOURCE_OR_INGEST_UNCONFIRMED": "На джерелі/вході є симптом, але його поширення далі не підтверджене.",
        "RTMP_SERVER_RESTREAM_PROBLEM": "Вхід сервера виглядав справним, а медіапроблема з'явилася на його виході.",
        "RTMP_SERVER_RESTREAM_UNCONFIRMED": "На виході сервера є проблема, але справний медіавхід не підтверджений.",
        "SOURCE_TO_SERVER_UNCONFIRMED": "Проблема між джерелом і виходом сервера не локалізована; probe входу відсутній.",
        "UPSTREAM_OR_SERVER_UNCONFIRMED": "Не вистачає спостереження входу, щоб розрізнити джерело й RTMP-сервер.",
        "AGENT_OFFLINE": "Probe не надсилає телеметрію; стан потоку в цій точці невідомий.",
    }
    return labels.get(diagnosis, "Зафіксовано інцидент, для якого ще немає окремої класифікації.")


def _clock_warning(samples: list[tuple[str, dict[str, Any]]], threshold_ms: float) -> dict[str, Any]:
    unsynchronized = []
    offsets = []
    measurements = set()
    for name, metrics in samples:
        clock = metrics.get("clock") or {}
        if clock.get("ntp_synchronized") is False:
            unsynchronized.append(name)
        offset = clock.get("estimated_offset_ms")
        from_agent_ntp = isinstance(offset, (int, float))
        if not from_agent_ntp:
            offset = clock.get("central_offset_ms")
        if isinstance(offset, (int, float)):
            uncertainty = 0.0
            if from_agent_ntp:
                measurements.add("agent NTP provider")
            else:
                source = clock.get("central_offset_source") or "http_date"
                measurements.add(
                    "central receive timestamp" if source == "central_receive_timestamp"
                    else "approximate central HTTP Date"
                )
                reported_uncertainty = clock.get("central_offset_uncertainty_ms")
                uncertainty = (
                    float(reported_uncertainty)
                    if isinstance(reported_uncertainty, (int, float)) and reported_uncertainty >= 0
                    else 1000.0
                )
            offsets.append((name, float(offset), uncertainty))
    values = [value for _, value, _ in offsets]
    spread = round(max(values, default=0) - min(values, default=0), 1) if values else None
    spread_lower_bound = max(
        (max(0.0, abs(left - right) - left_uncertainty - right_uncertainty)
         for index, (_, left, left_uncertainty) in enumerate(offsets)
         for _, right, right_uncertainty in offsets[index + 1:]),
        default=0.0,
    ) if offsets else None
    max_abs = round(max((abs(value) for _, value, _ in offsets), default=0), 1) if offsets else None
    max_abs_lower_bound = max(
        (max(0.0, abs(value) - uncertainty) for _, value, uncertainty in offsets),
        default=0.0,
    ) if offsets else None
    warning = bool(
        unsynchronized
        or (spread_lower_bound is not None and spread_lower_bound > threshold_ms)
        or (max_abs_lower_bound is not None and max_abs_lower_bound > threshold_ms)
    )
    return {"warning": warning,
            "message": "CLOCK NOT SYNCHRONIZED" if warning else None,
            "unsynchronized_agents": unsynchronized, "offset_spread_ms": spread, "threshold_ms": threshold_ms,
            "maximum_absolute_offset_ms": max_abs,
            "offset_spread_lower_bound_ms": round(spread_lower_bound, 1) if spread_lower_bound is not None else None,
            "maximum_absolute_offset_lower_bound_ms": round(max_abs_lower_bound, 1) if max_abs_lower_bound is not None else None,
            "measurement_uncertainty_ms": round(max((uncertainty for _, _, uncertainty in offsets), default=0), 1) if offsets else None,
            "measurement": "; ".join(sorted(measurements)) if measurements else None}

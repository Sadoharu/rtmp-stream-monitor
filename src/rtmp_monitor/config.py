from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class StreamConfig(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1)
    role: str | None = None


class AgentConfig(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=128)
    location: str = "unknown"
    role: str = "CLIENT"
    token: str = ""
    profile: str = "DEEP"

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"SERVER_EGRESS", "CLIENT", "SOURCE"}:
            raise ValueError("role must be SERVER_EGRESS, CLIENT, or SOURCE; SERVER_INGRESS requires an RTMP-server hook adapter")
        return normalized

    @field_validator("profile")
    @classmethod
    def valid_profile(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"LIGHT", "DEEP"}:
            raise ValueError("profile must be LIGHT or DEEP")
        return normalized


class ServerConnection(BaseModel):
    url: str = Field(min_length=1)


class MonitoringConfig(BaseModel):
    heartbeat_interval: float = Field(default=5.0, ge=1, le=60)
    progress_interval: float = Field(default=1.0, ge=0.5, le=10)
    freeze_threshold: float = Field(default=2.0, ge=0.5, le=60)
    silence_threshold: float = Field(default=3.0, ge=0.5, le=120)
    stall_threshold: float = Field(default=5.0, ge=2, le=300)
    warning_threshold: float = Field(default=3.0, ge=1, le=60)
    dead_threshold: float = Field(default=15.0, ge=5, le=900)
    keyframe_gap_threshold: float | None = Field(default=None, ge=2, le=300)
    reconnect_initial: float = Field(default=1.0, ge=0.5, le=60)
    reconnect_max: float = Field(default=60.0, ge=1, le=600)
    queue_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024 * 1024)
    queue_max_rows: int = Field(default=50_000, ge=100)


class NetworkConfig(BaseModel):
    enabled: bool = True
    server_host: str | None = None
    server_port: int = Field(default=1935, ge=1, le=65535)
    ping_interval: float = Field(default=10.0, ge=2, le=120)


class AgentFileConfig(BaseModel):
    server: ServerConnection
    agent: AgentConfig
    streams: list[StreamConfig] = Field(min_length=1, max_length=1)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    state_dir: Path = Path("./data/agent")
    log_dir: Path = Path("./logs")

    @model_validator(mode="after")
    def fill_network_target(self) -> "AgentFileConfig":
        if not self.network.server_host:
            from urllib.parse import urlparse

            self.network.server_host = urlparse(self.streams[0].url).hostname
        return self


class CentralFileConfig(BaseModel):
    database_url: str = "sqlite:///./data/central.db"
    bind_host: str = "0.0.0.0"
    bind_port: int = Field(default=8090, ge=1, le=65535)
    admin_token_file: Path = Path("./data/admin.token")
    logs_dir: Path = Path("./logs")
    agent_offline_seconds: int = Field(default=20, ge=5, le=3600)
    stream_offline_seconds: int = Field(default=15, ge=5, le=3600)
    clock_offset_warning_ms: float = Field(default=1000.0, ge=1, le=60000)
    media_timestamp_tolerance_seconds: float = Field(default=5.0, ge=0.1, le=120)
    raw_retention_days: int = Field(default=7, ge=1, le=365)
    aggregated_retention_days: int = Field(default=90, ge=30, le=3650)
    incident_retention_days: int = Field(default=180, ge=30, le=3650)
    log_retention_days: int = Field(default=30, ge=1, le=3650)


def load_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root in {config_path} must be a mapping")
    return data


def load_agent_config(path: str | Path) -> AgentFileConfig:
    return AgentFileConfig.model_validate(load_yaml(path))


def load_central_config(path: str | Path | None = None) -> CentralFileConfig:
    config = CentralFileConfig.model_validate(load_yaml(path) if path else {})
    env_url = os.getenv("RTMP_MONITOR_DATABASE_URL")
    env_token_file = os.getenv("RTMP_MONITOR_ADMIN_TOKEN_FILE")
    if env_url:
        config.database_url = env_url
    if env_token_file:
        config.admin_token_file = Path(env_token_file)
    return config

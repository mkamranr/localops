"""Schemas for integrations, resources, relationships and discovery runs."""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.schemas.common import ApiModel

IntegrationType = Literal["linux_ssh", "windows_winrm", "docker", "http", "mcp", "http_api"]
ResourceType = Literal[
    "linux_server",
    "windows_server",
    "docker_host",
    "docker_container",
    "http_service",
    "application",
    "database",
    "network_device",
    "other",
]
HealthStatus = Literal["healthy", "degraded", "unhealthy", "unknown"]
RelationshipType = Literal[
    "HOSTS",
    "DEPENDS_ON",
    "CONNECTS_TO",
    "RUNS_ON",
    "STORES_DATA_IN",
    "EXPOSES",
    "MONITORED_BY",
    "MANAGED_BY",
]

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9-]{1,63})*\.?$")
_LABEL = re.compile(r"^[\w .:/@-]{1,50}$")


def _valid_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(value))


# ------------------------------------------------------------------ integrations


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=300)
    port: int | None = Field(None, ge=1, le=65535)
    name: str | None = Field(None, max_length=100)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if any(c.isspace() for c in v) or v.startswith("-"):
            raise ValueError("must be a hostname, IP address or URL")
        return v


class IntegrationSettings(BaseModel):
    """Non-secret connection settings. Fields not used by a type are ignored."""

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(None, max_length=100)
    port: int | None = Field(None, ge=1, le=65535)
    timeout_seconds: int = Field(20, ge=3, le=300)
    # linux_ssh
    docker: Literal["auto", "off"] = "auto"
    # docker
    mode: Literal["ssh", "tls"] = "ssh"
    verify_hostname: bool = True
    # windows_winrm
    use_https: bool = False
    transport: Literal["ntlm", "basic"] = "ntlm"
    verify_tls: bool = True
    # http
    expected_status: list[int] = Field(default_factory=list, max_length=20)
    slow_ms: int = Field(2000, ge=50, le=120_000)
    # mcp / http_api (tool integrations)
    url: str | None = Field(None, max_length=500)
    mcp_transport: Literal["auto", "streamable_http", "sse"] = "auto"
    auth_type: Literal["none", "bearer", "basic", "header"] = "none"
    auth_header: str | None = Field(None, max_length=64)
    # Environment of what the tools act on (feeds remediation policy, e.g. "production").
    environment: str | None = Field(None, max_length=50)

    @field_validator("username")
    @classmethod
    def _username(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v and not re.fullmatch(r"[\w.@\\-]{1,100}", v):
            raise ValueError("contains unsupported characters")
        return v or None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", v):
            raise ValueError("must be an http(s) URL without query or fragment")
        return v

    @field_validator("auth_header")
    @classmethod
    def _auth_header(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", v.strip()):
            raise ValueError("must be a header name (letters, digits, dashes)")
        return v.strip()

    @field_validator("expected_status")
    @classmethod
    def _statuses(cls, v: list[int]) -> list[int]:
        if any(not 100 <= s <= 599 for s in v):
            raise ValueError("HTTP status codes must be between 100 and 599")
        return v


class IntegrationCredentials(BaseModel):
    """Secrets. Write-only: never returned by the API."""

    model_config = ConfigDict(extra="forbid")

    password: SecretStr | None = None
    private_key: SecretStr | None = None
    passphrase: SecretStr | None = None
    ca: SecretStr | None = None
    cert: SecretStr | None = None
    key: SecretStr | None = None
    token: SecretStr | None = None  # mcp / http_api bearer token or header value

    def plain(self) -> dict[str, str]:
        return {
            k: v.get_secret_value()
            for k, v in self
            if isinstance(v, SecretStr) and v.get_secret_value()
        }


def check_type_rules(
    integration_type: str, settings: IntegrationSettings, targets: list[TargetIn]
) -> None:
    if integration_type in ("mcp", "http_api"):
        if not settings.url:
            raise ValueError("A server URL is required")
        if targets:
            raise ValueError("Tool integrations use a URL, not discovery targets")
        if settings.auth_type == "header" and not settings.auth_header:
            raise ValueError("Header authentication needs a header name")
        if settings.auth_type == "basic" and not settings.username:
            raise ValueError("Basic authentication needs a username")
        return
    for t in targets:
        if integration_type == "http":
            if not t.host.startswith(("http://", "https://")):
                raise ValueError(f"HTTP targets must be URLs (got {t.host!r})")
        elif not _valid_host(t.host):
            raise ValueError(f"{t.host!r} is not a valid hostname or IP address")
    if (
        integration_type == "windows_winrm"
        and settings.transport == "basic"
        and not settings.use_https
    ):
        raise ValueError("Basic authentication is only allowed over HTTPS")


class IntegrationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    integration_type: IntegrationType
    description: str = Field("", max_length=1000)
    configuration: IntegrationSettings = Field(default_factory=lambda: IntegrationSettings())
    credentials: IntegrationCredentials | None = None
    targets: list[TargetIn] = Field(default_factory=list, max_length=500)
    enabled: bool = True
    schedule_minutes: int | None = Field(None, ge=5, le=10080)

    @model_validator(mode="after")
    def _rules(self) -> IntegrationCreate:
        check_type_rules(self.integration_type, self.configuration, self.targets)
        return self


class IntegrationUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=1000)
    configuration: IntegrationSettings | None = None
    # Provided fields replace the stored secret; omitted fields are kept.
    credentials: IntegrationCredentials | None = None
    clear_credentials: bool = False
    targets: list[TargetIn] | None = Field(None, max_length=500)
    enabled: bool | None = None
    schedule_minutes: int | None = Field(None, ge=5, le=10080)
    clear_schedule: bool = False


class IntegrationOut(ApiModel):
    id: uuid.UUID
    name: str
    integration_type: str
    description: str
    configuration: dict[str, Any]
    targets: list[dict[str, Any]]
    enabled: bool
    schedule_minutes: int | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    created_at: datetime
    has_credentials: bool = False
    credential_fields: list[str] = Field(default_factory=list)
    resource_count: int = 0


class IntegrationTestRequest(BaseModel):
    target: TargetIn | None = None  # default: the first configured target


class IntegrationTestResult(BaseModel):
    ok: bool
    target: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DiscoveryRunOut(ApiModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    trigger: str
    triggered_by: uuid.UUID | None
    status: str
    started_at: datetime
    completed_at: datetime | None
    stats: dict[str, Any]
    target_results: list[dict[str, Any]]
    error: str | None


class HostKeyOut(ApiModel):
    id: uuid.UUID
    host: str
    port: int
    key_type: str
    fingerprint: str
    first_seen_at: datetime
    last_seen_at: datetime | None


class DiscoverRequest(BaseModel):
    integration_id: uuid.UUID


# ------------------------------------------------------------------ resources


def _parse_ips(v: list[str]) -> list[str]:
    out = []
    for ip in v:
        try:
            out.append(str(ipaddress.ip_address(ip.strip())))
        except ValueError as exc:
            raise ValueError(f"{ip!r} is not an IP address") from exc
    return list(dict.fromkeys(out))


def _clean_tags(v: list[str]) -> list[str]:
    out: list[str] = []
    for tag in v:
        tag = tag.strip()
        if not tag:
            continue
        if not _LABEL.match(tag):
            raise ValueError(f"invalid tag {tag!r}")
        if tag not in out:
            out.append(tag)
    return out[:30]


class ResourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    resource_type: ResourceType
    hostname: str | None = Field(None, max_length=255)
    ip_addresses: list[str] = Field(default_factory=list, max_length=50)
    os: str | None = Field(None, max_length=200)
    environment: str | None = Field(None, max_length=50)
    group_name: str | None = Field(None, max_length=100)
    owner: str | None = Field(None, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=30)
    description: str = Field("", max_length=2000)

    @field_validator("ip_addresses")
    @classmethod
    def _ips(cls, v: list[str]) -> list[str]:
        return _parse_ips(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)


class ResourceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    resource_type: ResourceType | None = None
    hostname: str | None = Field(None, max_length=255)
    ip_addresses: list[str] | None = Field(None, max_length=50)
    os: str | None = Field(None, max_length=200)
    environment: str | None = Field(None, max_length=50)
    group_name: str | None = Field(None, max_length=100)
    owner: str | None = Field(None, max_length=100)
    tags: list[str] | None = Field(None, max_length=30)
    description: str | None = Field(None, max_length=2000)

    @field_validator("ip_addresses")
    @classmethod
    def _ips(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _parse_ips(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_tags(v)


class ResourceSummaryOut(ApiModel):
    id: uuid.UUID
    name: str
    resource_type: str
    hostname: str | None
    ip_addresses: list[str]
    os: str | None
    environment: str | None
    group_name: str | None
    owner: str | None
    tags: list[str]
    health_status: str
    health_reasons: list[str]
    source: str
    stale: bool
    integration_id: uuid.UUID | None
    last_seen_at: datetime | None
    created_at: datetime


class ResourceOut(ResourceSummaryOut):
    description: str
    facts: dict[str, Any]
    last_health_at: datetime | None
    discovery_target: str | None
    updated_at: datetime | None
    integration_name: str | None = None


class ResourceEventOut(ApiModel):
    id: uuid.UUID
    event_type: str
    summary: str
    details: dict[str, Any]
    actor: str | None
    created_at: datetime


class MetricSampleOut(ApiModel):
    taken_at: datetime
    cpu_load: float | None
    cpu_count: int | None
    memory_percent: float | None
    disk_percent: float | None
    latency_ms: float | None


class HealthOut(BaseModel):
    resource_id: uuid.UUID
    health_status: str
    health_reasons: list[str]
    last_health_at: datetime | None
    stale: bool
    last_seen_at: datetime | None


class InventorySummary(BaseModel):
    total: int
    by_health: dict[str, int]
    by_type: dict[str, int]
    stale: int
    last_discovery_at: datetime | None
    integrations: int


# ------------------------------------------------------------------ relationships


class RelationshipCreate(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    relationship_type: RelationshipType
    description: str = Field("", max_length=500)

    @model_validator(mode="after")
    def _distinct(self) -> RelationshipCreate:
        if self.source_id == self.target_id:
            raise ValueError("A resource cannot depend on itself")
        return self


class RelationshipOut(ApiModel):
    id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    relationship_type: str
    source: str
    confidence: float
    properties: dict[str, Any]
    last_seen_at: datetime | None
    created_at: datetime


class GraphNode(BaseModel):
    id: uuid.UUID
    name: str
    resource_type: str
    health_status: str
    environment: str | None
    group_name: str | None
    stale: bool
    depth: int | None = None
    direction: Literal["self", "upstream", "downstream"] | None = None


class GraphEdge(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    relationship_type: str
    source: str
    confidence: float


class DependencyGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    blast_radius: list[uuid.UUID] = Field(default_factory=list)
    truncated: bool = False

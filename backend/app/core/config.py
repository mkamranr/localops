"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from sqlalchemy.engine import URL

_PLACEHOLDER = "CHANGE_ME"


class FileSecretsSource(PydanticBaseSettingsSource):
    """Docker-secrets convention: ``<NAME>_FILE=/run/secrets/x`` reads NAME from that file.

    Works for every setting (APP_SECRET_KEY_FILE, DB_APP_PASSWORD_FILE,
    LOCALOPS_ENCRYPTION_KEY_FILE, ...). A ``_FILE`` value takes precedence over the plain
    variable, so a stale value left in ``.env`` cannot shadow the mounted secret.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        environ = {k.upper(): v for k, v in os.environ.items()}
        values: dict[str, Any] = {}
        for name, field in self.settings_cls.model_fields.items():
            key = field.validation_alias if isinstance(field.validation_alias, str) else name
            path = environ.get(f"{key.upper()}_FILE")
            if not path:
                continue
            try:
                values[key] = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"{key.upper()}_FILE: cannot read {path}: {exc.strerror}") from exc
        return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: Literal["development", "test", "production"] = "development"
    app_secret_key: SecretStr = Field(..., min_length=32)
    log_level: str = "INFO"

    # --- PostgreSQL (built from components so passwords need no URL escaping) ---
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "localops"
    db_app_user: str = "localops_app"
    db_app_password: SecretStr = SecretStr("")
    db_owner_user: str = "localops_owner"
    db_owner_password: SecretStr = SecretStr("")
    db_pool_size: int = 10

    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"

    # --- Auth ---
    auth_mode: Literal["local"] = "local"
    access_token_ttl_minutes: int = Field(15, ge=1, le=1440)
    refresh_token_ttl_days: int = Field(7, ge=1, le=90)
    cookie_secure: bool = True
    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: int = 300

    # --- Bootstrap ---
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_email: str = "admin@localops.local"
    bootstrap_admin_password: SecretStr | None = None
    bootstrap_create_default_tenant: bool = True

    enable_telemetry: bool = False

    # --- Secrets at rest (model API keys, later integration credentials) ---
    # Generate a Fernet key with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: SecretStr | None = Field(None, validation_alias="LOCALOPS_ENCRYPTION_KEY")

    # --- Default model endpoint seeded into tenants (optional) ---
    llm_provider: Literal["vllm", "ollama", "llamacpp", "openai_compatible"] = "vllm"
    llm_base_url: str | None = None
    llm_model: str | None = None

    # --- Investigations ---
    investigation_timeout_seconds: int = Field(1800, ge=60)
    investigation_max_clarifications: int = Field(2, ge=0, le=5)
    investigation_max_tool_calls: int = Field(30, ge=1, le=200)  # per investigation
    tool_output_model_bytes: int = Field(6000, ge=1000, le=50000)  # tool output a model sees
    incident_auto_create: bool = True  # open incidents when discovery sees failures
    # Phase 5: controlled remediation
    remediation_enabled: bool = True  # kill switch for every state-changing tool
    remediation_max_steps: int = Field(3, ge=1, le=10)  # per investigation plan
    approval_default_ttl_minutes: int = Field(60, ge=5, le=10080)
    verification_delay_seconds: float = Field(5.0, ge=0, le=300)

    # --- Phase 6: knowledge base and extensibility ---
    # Optional embedding endpoint seeded into tenants (OpenAI-compatible /v1/embeddings).
    embedding_provider: Literal["vllm", "ollama", "llamacpp", "openai_compatible"] = "ollama"
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    knowledge_max_upload_mb: int = Field(10, ge=1, le=200)
    knowledge_chunk_chars: int = Field(2400, ge=400, le=12000)
    knowledge_chunk_overlap: int = Field(300, ge=0, le=2000)
    knowledge_vector_collection_prefix: str = "lo_knowledge"
    mcp_default_timeout_seconds: float = Field(30.0, ge=1, le=600)

    # --- Infrastructure discovery ---
    discovery_concurrency: int = Field(8, ge=1, le=64)  # targets collected in parallel
    discovery_max_targets: int = Field(500, ge=1, le=5000)  # per integration
    metric_retention_days: int = Field(30, ge=1, le=3650)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            FileSecretsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _reject_placeholders(self) -> Settings:
        if self.app_env == "production":
            # The owner password is deliberately absent from api/worker containers;
            # it is validated when a migration actually needs it.
            for name in ("app_secret_key", "db_app_password"):
                value: SecretStr = getattr(self, name)
                if not value.get_secret_value() or _PLACEHOLDER in value.get_secret_value():
                    raise ValueError(f"{name.upper()} must be set to a real secret in production")
            self._check_encryption_key()
        return self

    def _check_encryption_key(self) -> None:
        """Production refuses to start without a usable key for secrets at rest."""
        from cryptography.fernet import Fernet

        key = self.encryption_key.get_secret_value() if self.encryption_key else ""
        if not key:
            raise ValueError(
                "LOCALOPS_ENCRYPTION_KEY must be set in production (it encrypts stored "
                'credentials); generate one with: python3 -c "import base64, os; '
                'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
            )
        try:
            Fernet(key.encode())
        except ValueError as exc:
            raise ValueError(
                "LOCALOPS_ENCRYPTION_KEY is not a valid key (32 url-safe base64-encoded bytes)"
            ) from exc

    def _db_url(self, user: str, password: SecretStr) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=user,
            password=password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @property
    def app_psycopg_conninfo(self) -> str:
        """libpq conninfo for the runtime role (used by the LangGraph checkpointer)."""
        from psycopg.conninfo import make_conninfo

        return make_conninfo(
            host=self.db_host,
            port=self.db_port,
            dbname=self.db_name,
            user=self.db_app_user,
            password=self.db_app_password.get_secret_value(),
            application_name="localops-checkpointer",
        )

    @property
    def app_database_url(self) -> URL:
        """Runtime connection: non-owner role, subject to row-level security."""
        return self._db_url(self.db_app_user, self.db_app_password)

    @property
    def owner_database_url(self) -> URL:
        """Schema-owner connection: used only by migrations."""
        if not self.db_owner_password.get_secret_value():
            raise ValueError("DB_OWNER_PASSWORD is required to run migrations")
        return self._db_url(self.db_owner_user, self.db_owner_password)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()

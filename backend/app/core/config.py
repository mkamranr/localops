"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

_PLACEHOLDER = "CHANGE_ME"


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

    @model_validator(mode="after")
    def _reject_placeholders(self) -> Settings:
        if self.app_env == "production":
            # The owner password is deliberately absent from api/worker containers;
            # it is validated when a migration actually needs it.
            for name in ("app_secret_key", "db_app_password"):
                value: SecretStr = getattr(self, name)
                if not value.get_secret_value() or _PLACEHOLDER in value.get_secret_value():
                    raise ValueError(f"{name.upper()} must be set to a real secret in production")
        return self

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

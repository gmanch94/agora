"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All Agora runtime configuration in one place.

    Loaded from environment + `.env` file. Defaults target local dev.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: str = Field(default="dev", alias="AGORA_ENV")
    log_level: str = Field(default="INFO", alias="AGORA_LOG_LEVEL")

    api_host: str = Field(default="0.0.0.0", alias="AGORA_API_HOST")
    api_port: int = Field(default=8000, alias="AGORA_API_PORT")

    db_url: str = Field(
        default="postgresql+asyncpg://agora:agora@localhost:5433/agora",
        alias="AGORA_DB_URL",
    )
    db_pool_size: int = Field(default=10, alias="AGORA_DB_POOL_SIZE")

    reshare_base_url: str = Field(default="", alias="RESHARE_BASE_URL")
    reshare_tenant: str = Field(default="consortium-a", alias="RESHARE_TENANT")
    reshare_user: str = Field(default="", alias="RESHARE_USER")
    reshare_password: str = Field(default="", alias="RESHARE_PASSWORD")

    ncip_base_url: str = Field(default="", alias="NCIP_BASE_URL")
    ncip_agency_id: str = Field(default="AGORA-DEV", alias="NCIP_AGENCY_ID")

    sru_loc_url: str = Field(default="https://lx2.loc.gov/voyager", alias="SRU_LOC_URL")
    sru_timeout_secs: float = Field(default=5.0, alias="SRU_TIMEOUT_SECS")

    saga_stall_timeout_secs: int = Field(default=600, alias="SAGA_STALL_TIMEOUT_SECS")
    outbox_retry_max_attempts: int = Field(default=10, alias="OUTBOX_RETRY_MAX_ATTEMPTS")
    outbox_worker_enabled: bool = Field(
        default=True, alias="AGORA_OUTBOX_WORKER_ENABLED"
    )
    outbox_poll_interval_secs: float = Field(
        default=1.0, alias="AGORA_OUTBOX_POLL_INTERVAL_SECS"
    )

    @property
    def reshare_enabled(self) -> bool:
        """True when a real ReShare endpoint is configured.

        When false, the in-process mock is used.
        """
        return bool(self.reshare_base_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor; safe to call repeatedly."""
    return Settings()

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

    api_host: str = Field(default="0.0.0.0", alias="AGORA_API_HOST")  # nosec B104  # dev default; production sets AGORA_API_HOST
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
    # When set, ``HttpReShareClient`` authenticates via the FOLIO Okapi
    # token flow (POST {okapi_url}/authn/login → ``x-okapi-token`` header)
    # instead of HTTP Basic. Reuses ``RESHARE_USER`` and ``RESHARE_PASSWORD``
    # as Okapi credentials. See ADR-0013.
    okapi_url: str = Field(default="", alias="OKAPI_URL")

    ncip_base_url: str = Field(default="", alias="NCIP_BASE_URL")
    ncip_agency_id: str = Field(default="AGORA-DEV", alias="NCIP_AGENCY_ID")

    sru_loc_url: str = Field(default="https://lx2.loc.gov/voyager", alias="SRU_LOC_URL")
    sru_timeout_secs: float = Field(default=5.0, alias="SRU_TIMEOUT_SECS")
    # Discovery factory toggles. Unlike ``reshare_base_url`` (whose empty
    # default acts as the mock-vs-http switch), CrossRef and SRU ship with
    # non-empty production URL defaults — so the URL-presence check that
    # works for ReShare cannot work here. We instead use explicit booleans
    # which default to ``False`` (mock client). Set to ``1`` / ``true`` to
    # opt into the live HTTP client. This matches ReShare's spirit
    # (mock-by-default for offline dev + tests) while leaving the URL
    # constants in place for when http is enabled.
    sru_enabled: bool = Field(default=False, alias="AGORA_SRU_ENABLED")

    crossref_base_url: str = Field(default="https://api.crossref.org", alias="CROSSREF_BASE_URL")
    crossref_timeout_secs: float = Field(default=5.0, alias="CROSSREF_TIMEOUT_SECS")
    # Polite-pool opt-in: when set, ``HttpCrossrefClient`` sends
    # ``User-Agent: Agora/0.1 (mailto:<value>)`` per CrossRef's
    # etiquette guidance, which earns better rate-limit treatment on
    # the public endpoint. Empty string keeps a plain UA.
    crossref_mailto: str = Field(default="", alias="CROSSREF_MAILTO")
    # See ``sru_enabled`` above for the rationale on explicit boolean
    # toggling vs ReShare's URL-presence convention.
    crossref_enabled: bool = Field(default=False, alias="AGORA_CROSSREF_ENABLED")

    saga_stall_timeout_secs: int = Field(default=600, alias="SAGA_STALL_TIMEOUT_SECS")
    outbox_retry_max_attempts: int = Field(default=10, alias="OUTBOX_RETRY_MAX_ATTEMPTS")
    outbox_worker_enabled: bool = Field(default=True, alias="AGORA_OUTBOX_WORKER_ENABLED")
    outbox_poll_interval_secs: float = Field(default=1.0, alias="AGORA_OUTBOX_POLL_INTERVAL_SECS")
    tracking_scanner_enabled: bool = Field(default=True, alias="AGORA_TRACKING_SCANNER_ENABLED")
    tracking_scan_interval_secs: float = Field(
        default=300.0, alias="AGORA_TRACKING_SCAN_INTERVAL_SECS"
    )
    tracking_recall_after_days: int = Field(default=14, alias="AGORA_TRACKING_RECALL_AFTER_DAYS")
    # Tier-3 watch (post NCIP-checkout SHIP→RECEIVE re-anchor): a saga
    # whose patron never confirms RECEIVE will never have a NCIP
    # ``check_out`` dispatched (the borrower-side ILS loan only opens
    # at physical-receipt confirmation under the new anchor). After
    # this many days past supplier-shipped with no RECEIVE event,
    # the scanner emits a ``receipt-unconfirmed-{saga_id}`` advisory
    # OBSERVATION so staff can chase the patron. 7 days picked as a
    # defensible default for domestic-ILL transit; tune per consortium.
    tracking_unconfirmed_receipt_after_days: int = Field(
        default=7, alias="AGORA_TRACKING_UNCONFIRMED_RECEIPT_AFTER_DAYS"
    )

    # RoutingAgent LLM tie-breaker activation threshold. When the
    # rules-baseline scoring puts the top-2 candidates within this gap,
    # ``RoutingAgent`` consults the configured ``LlmTiebreaker`` (if
    # any). Larger values fire the LLM more often (higher cost,
    # potentially better calls); smaller values keep the rules path
    # dominant. Default 0.05 is a placeholder until PR-2b runs the
    # eval against a real LLM and tunes against committed scenarios —
    # at the rules score scale (max ≈ 1.0), 0.05 captures cases where
    # rules effectively tied (the four ground-truth-vs-rules
    # disagreement scenarios in ``evals/routing/scenarios.json`` have
    # gaps of 0.0 except routing-015, whose 0.46 gap is documented
    # in ADR-0014 as out-of-scope for the tie-breaker).
    routing_tiebreak_epsilon: float = Field(default=0.05, alias="AGORA_ROUTING_TIEBREAK_EPSILON")

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

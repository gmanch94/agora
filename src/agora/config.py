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
    # dominant. PR #51 (#7c) tightened from 0.05 → 0.03 after the
    # PR-2b eval rerun showed routing-009 (gap 0.0467) firing the LLM
    # on a near-tie that rules picked correctly — LLM picked the worse
    # candidate. At 0.03, the three true-tie scenarios in scope (013 /
    # 014 / 016 with gap 0.0; 015's 0.46 gap is documented as
    # out-of-scope in ADR-0014) still fire the tie-breaker, but the
    # near-tie scenarios where rules already get the right answer
    # (007 gap 0.04, 009 gap 0.0467, 011 gap 0.04) skip the LLM. Three
    # are full-rules wins post-tuning; only 013/014/016 actually need
    # the model. Lift this back toward 0.05 only if a future scenario
    # set adds genuine 0.03-0.05 gap cases the LLM should disambiguate.
    routing_tiebreak_epsilon: float = Field(default=0.03, alias="AGORA_ROUTING_TIEBREAK_EPSILON")

    # RoutingAgent LLM tie-breaker adapter (PR-2b, ADR-0014). Disabled
    # by default so ``RoutingAgent()`` with no kwargs and no factory
    # call stays byte-identical to the rules-only baseline. Set
    # ``AGORA_ROUTING_LLM_ENABLED=1`` to opt the factory into building
    # the real ``AdkLlmTiebreaker``. Mirrors PR #46's explicit-boolean
    # toggle pattern (``AGORA_CROSSREF_ENABLED`` / ``AGORA_SRU_ENABLED``).
    routing_llm_enabled: bool = Field(default=False, alias="AGORA_ROUTING_LLM_ENABLED")
    # Vertex/Gemini model id. Flash chosen for tie-break-only one-shot
    # judgments — cheap, fast, JSON-mode reliable. Pro is overkill for
    # a 4-candidate pick. Re-tune in ADR-0014 if eval data argues
    # otherwise.
    routing_llm_model: str = Field(default="gemini-2.0-flash", alias="AGORA_ROUTING_LLM_MODEL")
    # Per-call timeout. Stuck LLM must NOT hang the saga; the adapter
    # raises on timeout, the seam catches and falls back to the rules
    # pick (PR #48's exception-fallback path). 5s is generous for
    # Gemini Flash; tune down once production data is available.
    routing_llm_timeout_secs: float = Field(default=5.0, alias="AGORA_ROUTING_LLM_TIMEOUT_SECS")
    # Vertex AI region. ``us-central1`` matches the kroger-shopping-agent
    # default and the bound quota project's primary location.
    routing_llm_location: str = Field(default="us-central1", alias="AGORA_ROUTING_LLM_LOCATION")

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

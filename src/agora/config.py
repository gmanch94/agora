"""Application configuration loaded from environment variables.

Audit 2026-05-09 #10 / #25 / #34: credential fields use ``SecretStr``
so ``model_dump()`` / ``repr()`` redact the value automatically (the
``agora --config`` CLI no longer prints plaintext passwords). Use
``.get_secret_value()`` at the consumer to read the actual string.
The ``db_url`` field is also a ``SecretStr`` because the URL embeds
credentials (``postgresql://user:password@host/db``).  # pragma: allowlist secret
"""

from functools import lru_cache

from pydantic import Field, SecretStr
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

    # Audit 2026-05-09 #34: bind to loopback by default. Operators
    # explicitly set ``AGORA_API_HOST=0.0.0.0`` (or container-network
    # address) to expose the API beyond the host. Combined with the
    # other auth/CSRF/rate-limit work in the audit-remediation sprint,
    # this avoids the failure mode where a fresh deployment is
    # immediately publicly reachable on every interface.
    api_host: str = Field(default="127.0.0.1", alias="AGORA_API_HOST")
    api_port: int = Field(default=8000, alias="AGORA_API_PORT")

    db_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://agora:agora@localhost:5433/agora"),
        alias="AGORA_DB_URL",
    )
    db_pool_size: int = Field(default=10, alias="AGORA_DB_POOL_SIZE")

    reshare_base_url: str = Field(default="", alias="RESHARE_BASE_URL")
    reshare_tenant: str = Field(default="consortium-a", alias="RESHARE_TENANT")
    reshare_user: str = Field(default="", alias="RESHARE_USER")
    reshare_password: SecretStr = Field(default=SecretStr(""), alias="RESHARE_PASSWORD")
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

    # Consortium roster used by ``DiscoveryAgent`` to flag candidates
    # whose ``agency_symbol`` is in-network (boosts ``in_consortium``,
    # downstream weight in RoutingAgent's rules-baseline). CSV form
    # rather than a structured shape because the prototype has no
    # consortium-roster source-of-truth yet — env-var lets ops seed
    # whichever symbols matter for a given deployment without a
    # schema migration. Empty default keeps prior behaviour (no
    # candidate flagged in-consortium). Tokens are stripped and
    # de-duped via ``consortium_members``.
    consortium_members_csv: str = Field(default="", alias="AGORA_CONSORTIUM_MEMBERS")

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
    # Vertex/Gemini model id. gemini-2.5-flash is the model used in the
    # committed LLM-augmented baseline (top-1 0.95, post-#7c). The old
    # default gemini-2.0-flash 404s under the current Vertex enablement.
    routing_llm_model: str = Field(default="gemini-2.5-flash", alias="AGORA_ROUTING_LLM_MODEL")
    # Per-call timeout. 5s is sometimes too tight for Gemini 2.5 cold-start
    # (CLAUDE.md known-gaps). Raised to 30s to match the documented eval
    # harness recommendation; tune down once warm-path latency is profiled.
    routing_llm_timeout_secs: float = Field(default=30.0, alias="AGORA_ROUTING_LLM_TIMEOUT_SECS")
    # Vertex AI region. ``us-central1`` matches the kroger-shopping-agent
    # default and the bound quota project's primary location.
    routing_llm_location: str = Field(default="us-central1", alias="AGORA_ROUTING_LLM_LOCATION")

    # Staff console HTTP Basic auth. When console_password is empty (default),
    # auth is disabled — dev convenience, no credentials required locally.
    # Set both vars in production-like envs to gate the HTML UI.
    console_username: str = Field(default="staff", alias="AGORA_CONSOLE_USERNAME")
    console_password: SecretStr = Field(
        default=SecretStr(""), alias="AGORA_CONSOLE_PASSWORD"
    )
    # Tenant-scoping stopgap (audit 2026-05-09 #3). When set, the Basic-auth
    # principal carries this library symbol and every saga endpoint refuses
    # operations on sagas whose ``requesting_library.symbol`` doesn't match.
    # Single-tenant by construction (one console password ↔ one library).
    # Multi-tenant needs a real auth model with per-principal claims —
    # tracked as an ADR follow-up. Empty default = no scoping = existing
    # dev behaviour where any authenticated caller can touch any saga.
    console_library_symbol: str = Field(
        default="", alias="AGORA_CONSOLE_LIBRARY_SYMBOL"
    )

    # RBAC roster (G-02 from docs/productionization.md). Comma-separated
    # ``username:role`` pairs assigning a role to each console user.
    # Roles: ``viewer`` (read-only) | ``approver`` (commit gates) |
    # ``admin`` (approver + future admin endpoints). Empty default
    # preserves pre-G-02 behaviour where the single console user gets
    # ``approver`` (legacy). Unknown usernames in the roster fall back
    # to ``viewer`` (read-only) — least-privilege on misconfiguration.
    # Example: ``alice:admin,bob:approver,charlie:viewer``.
    console_roles: str = Field(default="", alias="AGORA_CONSOLE_ROLES")

    # Patron PII retention (G-07, ADR-0020). Background scanner sweeps
    # terminal sagas past the retention window and scrubs borrower
    # fields in place. Disabled by default in dev so local tests don't
    # see surprise mutations to fixture sagas.
    retention_enabled: bool = Field(default=False, alias="AGORA_RETENTION_ENABLED")
    retention_days: int = Field(default=90, alias="AGORA_RETENTION_DAYS")
    retention_scan_interval_secs: float = Field(
        default=3600.0, alias="AGORA_RETENTION_SCAN_INTERVAL_SECS"
    )
    # HMAC salt for the scrub fingerprint. Production deployments MUST
    # rotate a 32-byte secret (`python -c 'import secrets; print(secrets.token_hex(32))'`).
    # Empty value fails the scrubber closed — see RetentionConfigError.
    pii_scrub_salt: SecretStr = Field(
        default=SecretStr(""), alias="AGORA_PII_SCRUB_SALT"
    )

    # Patron portal HMAC signing key (audit 2026-05-09 #2). When set,
    # ``/portal/requests`` and ``/portal/requests/{saga_id}`` require a
    # ``token`` query parameter whose HMAC matches the patron-id (and
    # saga-id, for the detail view). The token is issued out-of-band —
    # typically emailed to the patron — so an attacker who guesses a
    # patron_id alone cannot enumerate the patron's circulation
    # history. Empty default disables HMAC gating (preserves the
    # form-entry dev experience); production sets a 32-byte random
    # value via env-var rotation.
    portal_signing_key: SecretStr = Field(
        default=SecretStr(""), alias="AGORA_PORTAL_SIGNING_KEY"
    )

    # In-memory rate limit (audit 2026-05-09 #23). Per-IP request
    # ceiling over a sliding window. Defense in depth — production
    # MUST also rate-limit at the load balancer / reverse proxy
    # because this in-process counter is per-worker (not shared
    # across uvicorn replicas). Default ``False`` for dev / test
    # convenience; staging and prod deployments MUST set
    # ``AGORA_RATE_LIMIT_ENABLED=true`` (runbook § 9.4).
    rate_limit_enabled: bool = Field(
        default=False, alias="AGORA_RATE_LIMIT_ENABLED"
    )
    rate_limit_requests: int = Field(
        default=120, alias="AGORA_RATE_LIMIT_REQUESTS"
    )
    rate_limit_window_secs: int = Field(
        default=60, alias="AGORA_RATE_LIMIT_WINDOW_SECS"
    )

    # CSRF protection on HTML form endpoints (audit 2026-05-09 #8).
    # Double-submit cookie pattern: a CSRF token is set as a cookie
    # on first GET, the form's hidden input echoes the cookie value,
    # and the middleware refuses POSTs whose form value doesn't match
    # the cookie. Disabled by default to keep test flows working —
    # staging / prod sets ``AGORA_CSRF_ENABLED=1``. With Basic auth
    # the browser auto-attaches credentials on cross-site form POSTs,
    # so CSRF is the only defense against another site triggering
    # console actions.
    csrf_enabled: bool = Field(
        default=False, alias="AGORA_CSRF_ENABLED"
    )

    @property
    def reshare_enabled(self) -> bool:
        """True when a real ReShare endpoint is configured.

        When false, the in-process mock is used.
        """
        return bool(self.reshare_base_url)

    @property
    def db_url_uses_dev_default(self) -> bool:
        """True iff ``db_url`` is the unmodified ``agora:agora@`` default.

        Used by startup-time guards (``configure_logging`` /
        ``create_app``) to warn loudly when a non-``dev`` environment
        ships with the development credentials. Audit 2026-05-09 #25:
        the default ``postgresql+asyncpg://agora:agora@localhost:5433/agora``  # pragma: allowlist secret
        is fine for offline dev but lethal in any deployment that
        forgets to override it.
        """
        return ":agora@" in self.db_url.get_secret_value()

    @property
    def consortium_members(self) -> set[str]:
        """Parsed roster of in-consortium agency symbols.

        Splits ``AGORA_CONSORTIUM_MEMBERS`` on commas, strips whitespace
        per token, and drops empties. Returns an empty set when the
        env-var is unset or whitespace-only — preserving the pre-PR
        behaviour where ``DiscoveryAgent.consortium_members`` defaulted
        to ``set()`` at app build time.
        """
        raw = self.consortium_members_csv
        return {token.strip() for token in raw.split(",") if token.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor; safe to call repeatedly."""
    return Settings()

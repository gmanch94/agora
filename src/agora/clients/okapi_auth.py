"""FOLIO Okapi token-flow auth for ``httpx.AsyncClient``.

Implements ADR-0013. The class is invoked once per outbound request
by ``httpx.AsyncClient``; it acquires a token from the Okapi
``/authn/login-with-expiry`` endpoint on first use, caches it, and
attaches it as ``X-Okapi-Token`` on subsequent requests. The response
body's ``accessTokenExpiration`` (ISO 8601 timestamp) is parsed and
stored as ``_expires_at`` so the auth refreshes proactively just
before expiry — not just reactively on 401.

Audit 2026-05-09 #11/#13: pre-fix the auth never tracked expiry and
relied on 401 alone to refresh, producing a window where revoked
tokens kept attaching to outbound calls until the server rejected
them. Now expiry-driven refresh closes that window. ``clear_token()``
is also exposed so the lifespan shutdown path can drop credentials
from process memory at exit. The ``/authn/login-with-expiry`` endpoint
is the documented FOLIO contract; verification against a live FOLIO
instance is tracked as a backlog item (NEXT_SESSION.md).

Concurrent requests serialize on an ``asyncio.Lock`` during token
acquisition / refresh so the process issues at most one login per
token-expiry event regardless of in-flight load.

This module is **async-only**. The ReShare client is async; we do
not override ``sync_auth_flow``. Calling this auth from a synchronous
``httpx.Client`` would fall through to the base ``auth_flow`` which
yields the request unchanged — silent auth bypass. Document the
async-only contract at the call site if you ever wire a sync client.

See ``docs/adr/0013-okapi-token-auth.md`` for the original rationale.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx

from agora.clients.errors import ClientError
from agora.logging import get_logger

log = get_logger(__name__)


# Status code returned by FOLIO ``/authn/login-with-expiry`` on
# success. Exposed as a constant so tests don't depend on a magic
# literal. Older ``/authn/login`` returns 201 too.
LOGIN_SUCCESS_STATUS = 201

# Refresh proactively when the token has this many seconds (or fewer)
# until expiry. 60s window absorbs clock skew between Agora and FOLIO
# while still re-using the cached token for the bulk of its lifetime.
# Audit #11.
_EXPIRY_REFRESH_MARGIN_SECS = 60


class OkapiAuth(httpx.Auth):
    """``httpx.Auth`` subclass implementing the FOLIO Okapi token flow.

    Behaviour:

    - First request with an empty cache triggers a ``POST {login_url}``
      with ``{"username", "password"}`` JSON body and the configured
      tenant header. The response's ``x-okapi-token`` header is cached.
    - Subsequent requests attach the cached token as ``X-Okapi-Token``
      (and the tenant as ``X-Okapi-Tenant``).
    - A 401 on a request triggers exactly one re-login + retry. A
      second 401 is returned to the caller — the auth flow does not
      loop.
    - Token acquisition is serialized via ``asyncio.Lock``; concurrent
      flows observing a missing/expired token issue exactly one
      login. The lock is held across the ``yield`` of the login
      request, which is the correct posture: the yield suspends the
      generator until the response arrives, blocking other tasks
      from racing the same login.

    The class is async-only. ``sync_auth_flow`` is intentionally not
    overridden; using this auth with a synchronous client would
    bypass authentication. The async ReShare client is the only
    intended caller.
    """

    # Token is in the ``x-okapi-token`` response header. Response body
    # is parsed for ``accessTokenExpiration`` when present
    # (``/authn/login-with-expiry`` endpoint) — set
    # ``requires_response_body = True`` so httpx buffers it for us.
    # The auth still works against legacy ``/authn/login`` (no body
    # field) — body parsing is tolerant of missing fields.
    requires_request_body = False
    requires_response_body = True

    def __init__(
        self,
        *,
        login_url: str,
        tenant: str,
        username: str,
        password: str,
    ) -> None:
        if not login_url:
            raise ValueError("OkapiAuth: login_url is required")
        if not username or not password:
            raise ValueError(
                "OkapiAuth: username and password are required"
            )
        self._login_url = login_url
        self._tenant = tenant
        self._username = username
        self._password = password
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def cached_token(self) -> str | None:
        """Currently cached token, or None if not yet acquired.

        Exposed for tests + observability; production code should not
        rely on the cache state.
        """
        return self._token

    @property
    def expires_at(self) -> datetime | None:
        """Parsed ``accessTokenExpiration`` from the most recent login.

        ``None`` when the login response didn't include an expiry
        field (legacy ``/authn/login``) — in that case the auth falls
        back to reactive 401-driven refresh. Audit #11.
        """
        return self._expires_at

    def clear_token(self) -> None:
        """Drop the cached token + expiry from process memory.

        Wired into ``HttpReShareClient.aclose()`` and
        ``HttpNcipClient.aclose()`` so the lifespan shutdown path
        doesn't leave credentials sitting in RAM longer than the
        connection pool. Audit 2026-05-09 #11.
        """
        self._token = None
        self._expires_at = None

    def _token_is_fresh(self) -> bool:
        """Return True iff cached token exists AND won't expire soon.

        ``_expires_at is None`` (legacy login endpoint without expiry
        info) treats the token as always fresh — the reactive 401
        refresh path remains the only refresh trigger in that mode.
        Audit #11: prefer proactive refresh when expiry is known.
        """
        if self._token is None:
            return False
        if self._expires_at is None:
            return True
        return datetime.now(UTC) < self._expires_at - timedelta(
            seconds=_EXPIRY_REFRESH_MARGIN_SECS
        )

    def _build_login_request(self) -> httpx.Request:
        return httpx.Request(
            "POST",
            self._login_url,
            headers={
                "X-Okapi-Tenant": self._tenant,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "username": self._username,
                "password": self._password,
            },
        )

    @staticmethod
    def _extract_token(response: httpx.Response) -> str:
        # FOLIO returns the token in the ``x-okapi-token`` response
        # header. Header lookup is case-insensitive in httpx.
        # ``httpx.Headers.get`` returns ``Any`` under strict typing
        # (it's actually str | None at runtime). Coerce explicitly so
        # mypy --strict + the no-any-return check are satisfied.
        raw = response.headers.get("x-okapi-token")
        if not raw:
            raise ClientError(
                "Okapi login returned no x-okapi-token header "
                f"(status={response.status_code})"
            )
        return str(raw)

    @staticmethod
    def _extract_expiry(response: httpx.Response) -> datetime | None:
        """Parse ``accessTokenExpiration`` from a login-with-expiry response.

        Returns ``None`` when the response shape doesn't include the
        field (legacy ``/authn/login``) — caller falls back to
        no-expiry-tracking semantics. Tolerant of body parse errors
        (HTML error pages, empty bodies, malformed JSON) so a
        misconfigured FOLIO instance doesn't crash the auth flow.
        Audit #11.
        """
        try:
            data = response.json()
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        raw = data.get("accessTokenExpiration")
        if not isinstance(raw, str):
            return None
        # FOLIO emits an ISO 8601 timestamp like
        # ``2024-01-15T10:30:00.000+00:00``. ``datetime.fromisoformat``
        # handles that on Python 3.11+. Trailing 'Z' is normalised to
        # +00:00 — Python <3.13 needs explicit handling.
        normalised = raw.rstrip("Z")
        if normalised != raw:
            normalised = f"{normalised}+00:00"
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            return None
        # If FOLIO returns a tz-naive timestamp (unlikely but
        # defensive), assume UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Attach Okapi token; refresh once on 401.

        See class docstring for the full state machine. Generator
        contract per ``httpx.Auth.async_auth_flow``: yield a request
        to the client, receive a response back via ``.asend``, yield
        again to retry (or return to terminate).
        """
        # Phase 1: ensure we have a fresh token. Serialize via the
        # lock so concurrent flows don't all issue parallel logins.
        # ``_token_is_fresh()`` checks both presence AND proactive
        # expiry — when ``_expires_at`` is set and is within
        # ``_EXPIRY_REFRESH_MARGIN_SECS`` of now, we re-login before
        # the request goes out (audit #11). On legacy endpoints with no
        # expiry info, ``_token_is_fresh()`` returns True as long as a
        # token is cached (reactive-only, current behaviour).
        if not self._token_is_fresh():
            async with self._lock:
                if not self._token_is_fresh():
                    login_response = yield self._build_login_request()
                    if login_response.status_code != LOGIN_SUCCESS_STATUS:
                        raise ClientError(
                            "Okapi login failed: status="
                            f"{login_response.status_code}"
                        )
                    self._token = self._extract_token(login_response)
                    self._expires_at = self._extract_expiry(login_response)
                    log.info(
                        "okapi_auth.login.ok",
                        tenant=self._tenant,
                        has_expiry=self._expires_at is not None,
                    )
        # Now the cache is populated and not yet within the refresh
        # margin. Snapshot for phase 2.
        token = self._token
        assert token is not None  # nosec B101  # _token_is_fresh check above

        # Phase 2: attach token + send the original request.
        request.headers["X-Okapi-Token"] = token
        request.headers["X-Okapi-Tenant"] = self._tenant
        response = yield request

        # Phase 3: 401 → refresh once, retry once.
        if response.status_code == 401:
            stale_token = token
            async with self._lock:
                # Re-check: another flow may have already refreshed
                # in response to its own 401 since we surrendered the
                # CPU. Only re-login if the cache still holds OUR
                # stale token.
                cached = self._token
                if cached is None or cached == stale_token:
                    login_response = yield self._build_login_request()
                    if login_response.status_code != LOGIN_SUCCESS_STATUS:
                        raise ClientError(
                            "Okapi re-login failed: status="
                            f"{login_response.status_code}"
                        )
                    token = self._extract_token(login_response)
                    self._token = token
                    self._expires_at = self._extract_expiry(login_response)
                    log.info(
                        "okapi_auth.refresh.ok",
                        tenant=self._tenant,
                        has_expiry=self._expires_at is not None,
                    )
                else:
                    token = cached
            request.headers["X-Okapi-Token"] = token
            yield request
            # Whatever response comes back from the retry is final;
            # a second 401 is the caller's problem.

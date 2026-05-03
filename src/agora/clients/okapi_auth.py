"""FOLIO Okapi token-flow auth for ``httpx.AsyncClient``.

Implements ADR-0013. The class is invoked once per outbound request
by ``httpx.AsyncClient``; it acquires a token from the Okapi
``/authn/login`` endpoint on first use, caches it, and attaches it
as ``X-Okapi-Token`` on subsequent requests. On 401 the cached token
is refreshed once and the original request retried.

Concurrent requests serialize on an ``asyncio.Lock`` during token
acquisition / refresh so the process issues at most one
``/authn/login`` per token-expiry event regardless of in-flight load.

This module is **async-only**. The ReShare client is async; we do
not override ``sync_auth_flow``. Calling this auth from a synchronous
``httpx.Client`` would fall through to the base ``auth_flow`` which
yields the request unchanged — silent auth bypass. Document the
async-only contract at the call site if you ever wire a sync client.

See ``docs/adr/0013-okapi-token-auth.md`` for the rationale, the
choice of ``/authn/login`` over ``/authn/login-with-expiry``, and the
config-surface decisions.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import httpx

from agora.clients.errors import ClientError
from agora.logging import get_logger

log = get_logger(__name__)


# Status code returned by FOLIO ``/authn/login`` on success. Exposed as
# a constant so tests don't depend on a magic literal.
LOGIN_SUCCESS_STATUS = 201


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

    # Token is in the ``x-okapi-token`` response header — we don't
    # need the response body for the login, and we don't need to
    # buffer the response body to inspect status codes either.
    requires_request_body = False
    requires_response_body = False

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
        self._lock = asyncio.Lock()

    @property
    def cached_token(self) -> str | None:
        """Currently cached token, or None if not yet acquired.

        Exposed for tests + observability; production code should not
        rely on the cache state.
        """
        return self._token

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

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Attach Okapi token; refresh once on 401.

        See class docstring for the full state machine. Generator
        contract per ``httpx.Auth.async_auth_flow``: yield a request
        to the client, receive a response back via ``.asend``, yield
        again to retry (or return to terminate).
        """
        # Phase 1: ensure we have a token. Serialize via the lock so
        # concurrent flows don't all issue parallel logins. ``token``
        # is set to a non-None ``str`` in every code path before phase 2.
        token = self._token
        if token is None:
            async with self._lock:
                # Double-check under the lock; another flow may have
                # populated the cache while we waited.
                token = self._token
                if token is None:
                    login_response = yield self._build_login_request()
                    if login_response.status_code != LOGIN_SUCCESS_STATUS:
                        raise ClientError(
                            "Okapi login failed: status="
                            f"{login_response.status_code}"
                        )
                    token = self._extract_token(login_response)
                    self._token = token
                    log.info(
                        "okapi_auth.login.ok", tenant=self._tenant
                    )

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
                    log.info(
                        "okapi_auth.refresh.ok", tenant=self._tenant
                    )
                else:
                    token = cached
            request.headers["X-Okapi-Token"] = token
            yield request
            # Whatever response comes back from the retry is final;
            # a second 401 is the caller's problem.

"""Tests for ``OkapiAuth`` (ADR-0013).

Covers the four cases the ADR commits to:

1. Happy path — first request triggers login, token attached, request
   succeeds, second request reuses cached token (one login total).
2. 401 refresh — first request gets 401, auth flow re-logs in, retries
   the original request, that succeeds. Second login fired exactly once.
3. Concurrent requests share a single login under the
   ``asyncio.Lock`` — five parallel calls produce one login.
4. Missing creds → ``ValueError`` at construction.

Backed by ``httpx.MockTransport`` rather than spinning a real Okapi.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agora.clients.errors import ClientError
from agora.clients.okapi_auth import LOGIN_SUCCESS_STATUS, OkapiAuth

LOGIN_URL = "https://okapi.example.org/authn/login"
DATA_URL = "https://okapi.example.org/rs/patronrequests"


def _login_ok(token: str = "TOKEN-1") -> httpx.Response:
    """A canonical successful Okapi login response."""
    return httpx.Response(
        LOGIN_SUCCESS_STATUS,
        headers={"x-okapi-token": token},
        json={"username": "u"},
    )


def _data_ok() -> httpx.Response:
    return httpx.Response(200, json={"id": "rs-001", "state": "Requested"})


def _make_auth() -> OkapiAuth:
    return OkapiAuth(
        login_url=LOGIN_URL,
        tenant="consortium-a",
        username="u",
        password="p",  # test-only fake credential
    )


async def test_happy_path_logs_in_once_then_caches() -> None:
    """First request triggers login; second reuses the cached token."""

    counts: dict[str, int] = {"login": 0, "data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LOGIN_URL:
            counts["login"] += 1
            return _login_ok("TOKEN-1")
        counts["data"] += 1
        # Token + tenant must be on every data request.
        assert request.headers["X-Okapi-Token"] == "TOKEN-1"
        assert request.headers["X-Okapi-Tenant"] == "consortium-a"
        return _data_ok()

    transport = httpx.MockTransport(handler)
    auth = _make_auth()

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        r1 = await client.get(DATA_URL)
        r2 = await client.get(DATA_URL)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert counts == {"login": 1, "data": 2}
    assert auth.cached_token == "TOKEN-1"


async def test_401_triggers_refresh_and_retry() -> None:
    """A 401 on the data request re-logs in once and retries the same request.

    The retry succeeds and the caller sees a 200 — the 401 is invisible.
    """

    counts: dict[str, int] = {"login": 0, "data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LOGIN_URL:
            counts["login"] += 1
            # Each login gets a distinct token so we can verify the
            # second request used the refreshed value.
            return _login_ok(f"TOKEN-{counts['login']}")
        counts["data"] += 1
        if counts["data"] == 1:
            # First data request: stale token → 401.
            assert request.headers["X-Okapi-Token"] == "TOKEN-1"
            return httpx.Response(401, json={"error": "expired"})
        # Second data request must carry the refreshed token.
        assert request.headers["X-Okapi-Token"] == "TOKEN-2"
        return _data_ok()

    transport = httpx.MockTransport(handler)
    auth = _make_auth()

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        resp = await client.get(DATA_URL)

    assert resp.status_code == 200
    assert counts == {"login": 2, "data": 2}
    assert auth.cached_token == "TOKEN-2"


async def test_concurrent_requests_share_single_login() -> None:
    """Five parallel requests through the same auth issue exactly one login.

    The ``asyncio.Lock`` + double-checked cache must serialize the
    initial token acquisition. Without the lock, each in-flight task
    would observe ``self._token is None`` and fire its own login.
    """

    counts: dict[str, int] = {"login": 0, "data": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LOGIN_URL:
            counts["login"] += 1
            # Yield the event loop so other waiters get a chance to
            # observe the lock-held state. Without this the first
            # task can finish login before any other task even
            # enters the lock — masking the contention case.
            await asyncio.sleep(0)
            return _login_ok("TOKEN-1")
        counts["data"] += 1
        assert request.headers["X-Okapi-Token"] == "TOKEN-1"
        return _data_ok()

    transport = httpx.MockTransport(handler)
    auth = _make_auth()

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        results = await asyncio.gather(
            *(client.get(DATA_URL) for _ in range(5))
        )

    assert all(r.status_code == 200 for r in results)
    assert counts["login"] == 1, (
        f"expected 1 login, got {counts['login']} — lock not serializing"
    )
    assert counts["data"] == 5


async def test_login_failure_raises_client_error() -> None:
    """Non-201 from ``/authn/login`` raises ``ClientError`` with status."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LOGIN_URL:
            return httpx.Response(403, json={"error": "denied"})
        pytest.fail("data endpoint reached despite login failure")

    transport = httpx.MockTransport(handler)
    auth = _make_auth()

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        with pytest.raises(ClientError, match="Okapi login failed: status=403"):
            await client.get(DATA_URL)


async def test_login_response_missing_token_raises() -> None:
    """201 without ``x-okapi-token`` header raises ``ClientError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LOGIN_URL:
            return httpx.Response(LOGIN_SUCCESS_STATUS, json={"ok": True})
        pytest.fail("data endpoint reached despite missing token")

    transport = httpx.MockTransport(handler)
    auth = _make_auth()

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        with pytest.raises(ClientError, match="no x-okapi-token"):
            await client.get(DATA_URL)


def test_constructor_requires_login_url() -> None:
    with pytest.raises(ValueError, match="login_url is required"):
        OkapiAuth(
            login_url="",
            tenant="t",
            username="u",
            password="p",  # test-only fake credential
        )


def test_constructor_requires_credentials() -> None:
    """Empty username or password is rejected at construction.

    Catches the misconfiguration cleanly at startup rather than as a
    confusing 401 from Okapi at first request.
    """
    with pytest.raises(ValueError, match="username and password are required"):
        OkapiAuth(
            login_url=LOGIN_URL,
            tenant="t",
            username="",
            password="p",  # test-only fake credential
        )
    with pytest.raises(ValueError, match="username and password are required"):
        OkapiAuth(
            login_url=LOGIN_URL,
            tenant="t",
            username="u",
            password="",
        )


def test_reshare_client_picks_okapi_when_url_set() -> None:
    """``HttpReShareClient`` wires ``OkapiAuth`` when ``OKAPI_URL`` is set.

    Verifies the integration point in ``HttpReShareClient.__init__``
    without making any network calls — we only inspect the chosen
    auth object on the client.
    """
    from agora.clients.reshare import HttpReShareClient
    from agora.config import Settings

    settings = Settings(
        RESHARE_BASE_URL="https://okapi.example.org",
        RESHARE_TENANT="consortium-a",
        RESHARE_USER="u",
        RESHARE_PASSWORD="p",  # test-only fake credential
        OKAPI_URL="https://okapi.example.org",
    )
    client = HttpReShareClient(settings)
    try:
        assert isinstance(client._auth, OkapiAuth)
    finally:
        # Synchronous teardown — aclose is async, but the underlying
        # AsyncClient also has a sync close path; we just drop the ref.
        del client


def test_reshare_client_falls_back_to_basic_when_no_okapi() -> None:
    """Without ``OKAPI_URL``, the dev-path Basic auth still wins."""
    from agora.clients.reshare import HttpReShareClient
    from agora.config import Settings

    settings = Settings(
        RESHARE_BASE_URL="http://mod-rs.local:8081",
        RESHARE_TENANT="dev",
        RESHARE_USER="u",
        RESHARE_PASSWORD="p",  # test-only fake credential
        OKAPI_URL="",
    )
    client = HttpReShareClient(settings)
    try:
        assert isinstance(client._auth, httpx.BasicAuth)
    finally:
        del client



"""Discovery client factory tests.

``get_crossref_client()`` and ``get_sru_client()`` must select the
mock vs HTTP client based on explicit ``AGORA_*_ENABLED`` env flags
rather than URL-presence (which is how ReShare's factory works) —
because both CrossRef and SRU ship with non-empty production URL
defaults, a presence check would always pick http and break offline
dev / tests.

These tests pin three invariants:

1. **Default = mock.** Bare ``get_*_client()`` with no env override
   returns the in-memory mock. This is what every existing test
   (and the happy-path demo, when wired) implicitly depends on.
2. **Opt-in to http.** Setting ``AGORA_CROSSREF_ENABLED=1`` /
   ``AGORA_SRU_ENABLED=1`` returns the live HTTP client. We assert
   on type rather than making an actual network call — the http
   client's wire behaviour is covered by ``test_crossref.py`` /
   shadowed for SRU.
3. **Mock contract.** The default-mock paths return objects that
   satisfy each Protocol's contract — ``lookup_doi`` returns ``None``
   for an unknown DOI, ``search_*`` returns ``[]`` — so a caller can
   exercise DiscoveryAgent end-to-end against the empty mocks
   without crashing.

``get_settings()`` is ``lru_cache``d, so each test must clear the
cache after mutating env vars; the ``_clear_settings_cache`` fixture
handles that. Without it, the first test's settings snapshot would
leak into every subsequent test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from agora.clients.crossref import (
    CrossrefClient,
    HttpCrossrefClient,
    MockCrossrefClient,
    get_crossref_client,
)
from agora.clients.sru import (
    HttpSruClient,
    MockSruClient,
    SruClient,
    get_sru_client,
)
from agora.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Reset the ``get_settings`` lru_cache around every test.

    Pytest's ``monkeypatch`` rewinds env vars after the test, but the
    ``Settings`` instance built before the rewind stays cached until
    we explicitly clear. Clearing both pre- and post-test prevents
    cross-contamination from any preceding test (or interactive
    REPL session) that warmed the cache with different env state.
    """
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


# --- CrossRef factory ------------------------------------------------------


def test_crossref_factory_default_returns_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env override → MockCrossrefClient."""
    monkeypatch.delenv("AGORA_CROSSREF_ENABLED", raising=False)
    client = get_crossref_client()
    assert isinstance(client, MockCrossrefClient)


def test_crossref_factory_enabled_returns_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AGORA_CROSSREF_ENABLED=1`` → HttpCrossrefClient."""
    monkeypatch.setenv("AGORA_CROSSREF_ENABLED", "1")
    client = get_crossref_client()
    assert isinstance(client, HttpCrossrefClient)
    # HttpCrossrefClient owns an httpx.AsyncClient — release it so the
    # event loop doesn't warn about an unclosed transport.
    asyncio.run(client.aclose())


@pytest.mark.parametrize("falsy", ["0", "false", "False"])
def test_crossref_factory_falsy_env_returns_mock(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    """Common falsy strings parse to ``False`` (pydantic-settings semantics)
    and select the mock."""
    monkeypatch.setenv("AGORA_CROSSREF_ENABLED", falsy)
    client = get_crossref_client()
    assert isinstance(client, MockCrossrefClient)


@pytest.mark.asyncio
async def test_crossref_factory_mock_lookup_unknown_doi() -> None:
    """The default mock honours the Protocol: unknown DOI → None.

    Matches the live 404 contract — DiscoveryAgent treats both as a
    "no record" diagnostic.
    """
    client: CrossrefClient = get_crossref_client()
    assert await client.lookup_doi("10.0/never.seen") is None


# --- SRU factory -----------------------------------------------------------


def test_sru_factory_default_returns_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env override → MockSruClient."""
    monkeypatch.delenv("AGORA_SRU_ENABLED", raising=False)
    client = get_sru_client()
    assert isinstance(client, MockSruClient)


def test_sru_factory_enabled_returns_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AGORA_SRU_ENABLED=1`` → HttpSruClient."""
    monkeypatch.setenv("AGORA_SRU_ENABLED", "1")
    client = get_sru_client()
    assert isinstance(client, HttpSruClient)
    # HttpSruClient owns an httpx.AsyncClient — release it.
    asyncio.run(client.aclose())


@pytest.mark.parametrize("falsy", ["0", "false", "False"])
def test_sru_factory_falsy_env_returns_mock(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    """Common falsy strings parse to ``False`` and select the mock."""
    monkeypatch.setenv("AGORA_SRU_ENABLED", falsy)
    client = get_sru_client()
    assert isinstance(client, MockSruClient)


@pytest.mark.asyncio
async def test_sru_factory_mock_searches_return_empty() -> None:
    """The default mock's search_* methods return ``[]`` with no seed records.

    DiscoveryAgent then surfaces "zero holders matched" — the correct
    offline-dev signal.
    """
    client: SruClient = get_sru_client()
    assert await client.search_isbn("9780000000001") == []
    assert await client.search_issn("0000-0000") == []
    assert await client.search_title("nothing here") == []

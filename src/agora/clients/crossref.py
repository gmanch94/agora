"""CrossRef REST API client — DOI → bibliographic record.

Public, no-auth endpoint. CrossRef confirms identity (title, ISSN,
year, container) for a DOI but **does NOT report holdings** — that
remains an SRU/WorldCat concern. Two clients, two roles:

- ``CrossrefClient`` augments the request's bibliographic identity
  when the patron supplied a DOI (the modern default for journal
  articles).
- ``SruClient`` finds *who holds* the item (MARC 852).

DiscoveryAgent will fan out to both in PR-B.

CrossRef envelope::

    {
      "status": "ok",
      "message-type": "work",
      "message-version": "...",
      "message": {
        "title": ["..."],
        "author": [{"given": "...", "family": "...", "sequence": "first"}, ...],
        "ISSN": ["...", "..."],
        "ISBN": ["..."],
        "container-title": ["..."],
        "published-print": {"date-parts": [[1975, 9]]},
        "type": "journal-article",
        "DOI": "10.xxxx/yyy",
        ...
      }
    }

A 404 from the works endpoint means "DOI not registered with
CrossRef" — handled as ``None`` rather than an exception. A 5xx or
network failure raises ``RemoteUnavailableError`` so the caller can
fall back to SRU. Polite-pool ``User-Agent`` is opt-in via
``CROSSREF_MAILTO``: when set, the client sends
``Agora/0.1 (mailto:<value>)`` per CrossRef's etiquette guidance,
which earns better rate-limit treatment on the public endpoint.

CrossRef ``type`` → Agora ``ItemMetadata.item_kind`` mapping table
lives in ``_TYPE_KIND``. Unknowns map to ``"other"``; extend the
table when a real request surfaces a new value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from agora.clients.errors import RemoteUnavailableError
from agora.config import get_settings
from agora.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class CrossrefRecord:
    """Bibliographic identity returned by CrossRef for a DOI.

    Note the deliberate absence of a ``holdings`` field — CrossRef
    knows nothing about which library holds which item.
    """

    doi: str
    title: str
    authors: list[str]
    issn: str | None
    isbn: str | None
    container_title: str | None
    year: int | None
    item_kind: str
    raw: dict[str, Any]


class CrossrefClient(Protocol):
    async def lookup_doi(self, doi: str) -> CrossrefRecord | None: ...


# CrossRef ``type`` values seen in the wild → Agora's coarse
# ``ItemMetadata.item_kind`` taxonomy. Keep this table small and
# explicit; the next session can extend it when a real request
# surfaces a new value rather than catching everything blindly.
_TYPE_KIND: dict[str, str] = {
    "journal-article": "article",
    "proceedings-article": "article",
    "posted-content": "article",
    "report": "article",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "book-chapter": "chapter",
    "book-part": "chapter",
    "book-section": "chapter",
}


def _normalise_doi(raw: str) -> str:
    """Strip common DOI prefixes/URLs.

    Accepts ``10.xxxx/yyy``, ``doi:10.xxxx/yyy``, ``DOI: 10.xxxx/yyy``,
    ``https://doi.org/10.xxxx/yyy``, ``http://dx.doi.org/10.xxxx/yyy``.
    Returns the bare ``10.xxxx/yyy`` form. Caller is responsible for
    URL-encoding when interpolating into a path.
    """
    s = raw.strip()
    lowered = s.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        if lowered.startswith(prefix):
            return s[len(prefix):]
    if lowered.startswith("doi:"):
        return s[4:].lstrip(" ")
    return s


class HttpCrossrefClient:
    """Async CrossRef client using httpx.

    Settings:
      - ``CROSSREF_BASE_URL`` (default ``https://api.crossref.org``)
      - ``CROSSREF_TIMEOUT_SECS`` (default 5.0)
      - ``CROSSREF_MAILTO`` (default empty — when set, opts into
        CrossRef's polite pool with ``User-Agent: Agora/0.1
        (mailto:<value>)``).
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        mailto: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        s = get_settings()
        self._base_url = (base_url or s.crossref_base_url).rstrip("/")
        self._timeout = timeout if timeout is not None else s.crossref_timeout_secs
        self._mailto = mailto if mailto is not None else s.crossref_mailto
        ua = "Agora/0.1"
        if self._mailto:
            ua = f"Agora/0.1 (mailto:{self._mailto})"
        headers = {"User-Agent": ua, "Accept": "application/json"}
        client_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "headers": headers,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def lookup_doi(self, doi: str) -> CrossrefRecord | None:
        bare = _normalise_doi(doi)
        if not bare or "/" not in bare:
            return None
        url = f"{self._base_url}/works/{bare}"
        try:
            resp = await self._client.get(url)
        except httpx.RequestError as exc:
            raise RemoteUnavailableError(f"crossref unreachable: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            raise RemoteUnavailableError(f"crossref {resp.status_code}")
        resp.raise_for_status()

        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return None
        return _parse_message(data)


class MockCrossrefClient:
    """Deterministic CrossRef double for tests.

    Backed by an in-memory ``{doi: CrossrefRecord}`` map. ``None`` is
    returned for unknown DOIs (matching the live 404 contract).
    """

    def __init__(self, records: dict[str, CrossrefRecord] | None = None):
        self._records = records or {}

    async def lookup_doi(self, doi: str) -> CrossrefRecord | None:
        return self._records.get(_normalise_doi(doi))


def _parse_message(data: dict[str, Any]) -> CrossrefRecord | None:
    """Parse the CrossRef ``works/{doi}`` envelope into a record.

    Returns ``None`` if the envelope is malformed. Missing optional
    fields (no authors, no ISSN, etc.) are tolerated and surface as
    empty values on the record.
    """
    if not isinstance(data, dict):
        return None
    if data.get("message-type") != "work":
        return None
    msg = data.get("message")
    if not isinstance(msg, dict):
        return None

    doi = (msg.get("DOI") or "").strip()
    if not doi:
        return None

    titles = msg.get("title") or []
    title = titles[0].strip() if isinstance(titles, list) and titles else ""

    authors_raw = msg.get("author") or []
    authors: list[str] = []
    if isinstance(authors_raw, list):
        for a in authors_raw:
            if not isinstance(a, dict):
                continue
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            full = " ".join(p for p in (given, family) if p)
            if full:
                authors.append(full)

    issn = _first_str(msg.get("ISSN"))
    isbn = _first_str(msg.get("ISBN"))
    container = _first_str(msg.get("container-title"))

    year = _extract_year(msg.get("published-print")) or _extract_year(
        msg.get("published-online")
    ) or _extract_year(msg.get("published")) or _extract_year(msg.get("issued"))

    cr_type = (msg.get("type") or "").strip()
    item_kind = _TYPE_KIND.get(cr_type, "other")

    return CrossrefRecord(
        doi=doi,
        title=title,
        authors=authors,
        issn=issn,
        isbn=isbn,
        container_title=container,
        year=year,
        item_kind=item_kind,
        raw=msg,
    )


def _first_str(val: Any) -> str | None:
    """Return the first non-empty string from a list, or None."""
    if not isinstance(val, list):
        return None
    for v in val:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _extract_year(val: Any) -> int | None:
    """CrossRef date shape: ``{"date-parts": [[YYYY, MM, DD]]}``."""
    if not isinstance(val, dict):
        return None
    parts = val.get("date-parts")
    if not isinstance(parts, list) or not parts:
        return None
    first = parts[0]
    if not isinstance(first, list) or not first:
        return None
    candidate = first[0]
    if isinstance(candidate, int):
        return candidate
    if isinstance(candidate, str) and candidate.isdigit():
        return int(candidate)
    return None


def get_crossref_client() -> CrossrefClient:
    """Factory: real ``HttpCrossrefClient`` when ``AGORA_CROSSREF_ENABLED``
    is set, else in-memory ``MockCrossrefClient``.

    Mirrors :func:`agora.clients.reshare.get_client` in spirit (mock by
    default for offline dev + tests; opt into http via env). The toggle
    is an explicit boolean rather than a URL-presence check because
    ``CROSSREF_BASE_URL`` ships with a non-empty production default —
    a presence check would always select http and break offline
    workflows. The empty-record mock returns ``None`` for every DOI,
    which is the same shape the live client returns for an unregistered
    DOI (404), so DiscoveryAgent's CrossRef-miss path is exercised
    naturally when discovery runs without seeded fixtures.
    """
    s = get_settings()
    if s.crossref_enabled:
        return HttpCrossrefClient()
    log.info("crossref.client.using_mock")
    return MockCrossrefClient()

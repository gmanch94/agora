"""SRU (Search/Retrieve via URL) discovery client.

Uses CQL queries over HTTP; parses MARCXML responses. We intentionally
do NOT speak Z39.50 binary protocol (see ADR-0006).

For the prototype, parsing is shallow — extract title, author, and
holding agency hints; full MARC field decoding is deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from agora.clients.errors import RemoteUnavailableError
from agora.config import get_settings


@dataclass(slots=True)
class SruRecord:
    """A single record returned by an SRU search."""

    title: str
    authors: list[str]
    isbn: str | None
    issn: str | None
    holdings: list[str]
    raw_marcxml: str


class SruClient(Protocol):
    async def search_isbn(self, isbn: str) -> list[SruRecord]: ...
    async def search_issn(self, issn: str) -> list[SruRecord]: ...
    async def search_title(self, title: str, author: str | None = None) -> list[SruRecord]: ...


class HttpSruClient:
    """Tiny SRU client targeting LoC by default.

    Parsing is shallow on purpose; for the prototype we only need
    enough metadata to drive routing decisions.
    """

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        s = get_settings()
        self._base_url = (base_url or s.sru_loc_url).rstrip("/")
        self._timeout = timeout or s.sru_timeout_secs
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _search(self, cql: str) -> list[SruRecord]:
        params = {
            "version": "1.1",
            "operation": "searchRetrieve",
            "query": cql,
            "maximumRecords": "20",
            "recordSchema": "marcxml",
        }
        try:
            resp = await self._client.get(self._base_url, params=params)
        except httpx.RequestError as exc:
            raise RemoteUnavailableError(str(exc)) from exc

        if resp.status_code >= 500:
            raise RemoteUnavailableError(f"SRU {resp.status_code}")
        resp.raise_for_status()
        return _parse_sru_response(resp.text)

    async def search_isbn(self, isbn: str) -> list[SruRecord]:
        return await self._search(f"bath.isbn={isbn}")

    async def search_issn(self, issn: str) -> list[SruRecord]:
        return await self._search(f"bath.issn={issn}")

    async def search_title(self, title: str, author: str | None = None) -> list[SruRecord]:
        cql = f'dc.title="{title}"'
        if author:
            cql += f' and dc.creator="{author}"'
        return await self._search(cql)


class MockSruClient:
    """Deterministic SRU double for tests."""

    def __init__(self, records: list[SruRecord] | None = None):
        self._records = records or []

    async def search_isbn(self, isbn: str) -> list[SruRecord]:
        return [r for r in self._records if r.isbn == isbn] or list(self._records[:1])

    async def search_issn(self, issn: str) -> list[SruRecord]:
        return [r for r in self._records if r.issn == issn] or list(self._records[:1])

    async def search_title(self, title: str, author: str | None = None) -> list[SruRecord]:
        out = [r for r in self._records if title.lower() in r.title.lower()]
        return out or list(self._records[:1])


def _parse_sru_response(xml: str) -> list[SruRecord]:
    """Shallow MARCXML parse.

    Extracts 245$a (title), 100$a (author), 020$a (ISBN), 022$a (ISSN),
    and 852 (holdings) when present. Robust to missing fields.
    """
    from lxml import etree

    ns = {
        "zs": "http://www.loc.gov/zing/srw/",
        "marc": "http://www.loc.gov/MARC21/slim",
    }
    try:
        root = etree.fromstring(xml.encode("utf-8"))
    except etree.XMLSyntaxError:
        return []

    out: list[SruRecord] = []
    for rec in root.findall(".//marc:record", ns):
        title = _subfield(rec, "245", "a", ns) or ""
        author = _subfield(rec, "100", "a", ns)
        isbn = _subfield(rec, "020", "a", ns)
        issn = _subfield(rec, "022", "a", ns)
        holdings = [
            (h.text or "").strip()
            for h in rec.findall("./marc:datafield[@tag='852']/marc:subfield[@code='a']", ns)
            if (h.text or "").strip()
        ]
        out.append(
            SruRecord(
                title=title.strip(),
                authors=[author] if author else [],
                isbn=isbn,
                issn=issn,
                holdings=holdings,
                raw_marcxml=etree.tostring(rec, encoding="unicode"),
            )
        )
    return out


def _subfield(rec: object, tag: str, code: str, ns: dict[str, str]) -> str | None:
    """Helper: pull subfield text or None."""
    from lxml import etree as _etree

    assert isinstance(rec, _etree._Element)  # nosec B101  # mypy narrowing for caller-provided node
    found = rec.find(
        f"./marc:datafield[@tag='{tag}']/marc:subfield[@code='{code}']", ns
    )
    if found is None:
        return None
    text = (found.text or "").strip()
    return text or None

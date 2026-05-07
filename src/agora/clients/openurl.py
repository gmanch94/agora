"""OpenURL 1.0 KEV (Key/Encoded-Value) parser.

Resolves an OpenURL ContextObject to ``ItemMetadata``. We support the
ubiquitous KEV form (query-string style) used by link resolvers and
citation managers. XML ContextObjects are out of scope for the
prototype.

References:
- ANSI/NISO Z39.88-2004 (R2010) The OpenURL Framework
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from agora.models.request import Citation, ItemMetadata


class OpenUrlParseError(Exception):
    """Raised when the OpenURL string can't be parsed."""


def parse_openurl(openurl: str) -> tuple[ItemMetadata, Citation]:
    """Parse a KEV OpenURL into ``ItemMetadata`` + ``Citation``.

    Accepts either a full URL with a query string, or a bare query
    string. Unknown fields are ignored. Missing title falls back to
    ``article_title``; missing both raises.
    """
    raw = openurl.strip()
    if not raw:
        raise OpenUrlParseError("empty OpenURL")

    qs = urlparse(raw).query or raw
    params = {k: v[-1] for k, v in parse_qs(qs, keep_blank_values=False).items() if v}

    def get(*keys: str) -> str | None:
        for k in keys:
            if params.get(k):
                return params[k]
        return None

    genre = (get("rft.genre", "genre") or "").lower()
    item_kind = _genre_to_kind(genre)

    title = get("rft.btitle", "rft.title", "title")
    article_title = get("rft.atitle", "atitle")
    if not title and not article_title:
        raise OpenUrlParseError("OpenURL missing title and atitle")

    year = _safe_int(get("rft.date", "rft.year", "date"))

    item = ItemMetadata(
        title=title or article_title or "",
        article_title=article_title,
        author=get("rft.au", "rft.creator", "au"),
        isbn=get("rft.isbn", "isbn"),
        issn=get("rft.issn", "issn"),
        doi=get("rft_id_doi", "rft.doi", "doi"),
        oclc_number=get("rft.oclcnum", "rft_id_oclcnum"),
        year=year,
        edition=get("rft.edition"),
        publisher=get("rft.pub"),
        pages=get("rft.pages"),
        item_kind=item_kind,
    )

    citation = Citation(
        raw=raw,
        parsed_from="openurl",
        parsed_at=datetime.now(UTC),
    )
    return item, citation


def _genre_to_kind(genre: str) -> str:
    if genre in {"book", "bookitem"}:
        return "book"
    if genre in {"article", "journal", "issue", "preprint"}:
        return "article"
    if genre in {"chapter", "proceeding", "conference"}:
        return "chapter"
    return "other"


def _safe_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = "".join(c for c in value[:4] if c.isdigit())
    if len(digits) != 4:
        return None
    try:
        return int(digits)
    except ValueError:  # pragma: no cover — digits is all-isdigit, int() never raises
        return None

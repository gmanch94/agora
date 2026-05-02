"""Request and item models.

``IllRequest`` is the durable shape of a patron-submitted ILL request
once it has entered the saga. Citation parsing populates the
``ItemMetadata``; the full original input is retained on
``Citation.raw`` for re-parsing if needed.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class RequestType(str, Enum):
    """Whether the patron wants the physical item back, or a copy."""

    LOAN = "loan"
    COPY = "copy"


class PatronRef(BaseModel):
    """Pseudonymous reference to a patron; resolves elsewhere.

    PII (real name, email) is intentionally not stored here. The
    consortium directory holds the mapping; we only persist a
    library-scoped opaque id.
    """

    model_config = ConfigDict(frozen=True)

    library_symbol: str
    patron_id: str


class LibraryRef(BaseModel):
    """Reference to a consortium member library by ISIL symbol."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str | None = None


class ItemMetadata(BaseModel):
    """Bibliographic identity of the requested item."""

    title: str
    author: str | None = None
    isbn: str | None = None
    issn: str | None = None
    doi: str | None = None
    oclc_number: str | None = None
    year: int | None = None
    edition: str | None = None
    publisher: str | None = None
    article_title: str | None = None
    pages: str | None = None
    item_kind: str = Field(default="book", description="book|article|chapter|other")


class Citation(BaseModel):
    """Original citation context preserved for replay/audit."""

    raw: str
    parsed_from: str = Field(description="openurl|freetext|identifier")
    parsed_at: datetime


class IllRequest(BaseModel):
    """An interlibrary loan request entering the saga."""

    model_config = ConfigDict(frozen=False)

    request_id: UUID = Field(default_factory=uuid4)
    request_type: RequestType
    patron: PatronRef
    requesting_library: LibraryRef
    item: ItemMetadata
    citation: Citation | None = None
    needed_by: datetime | None = None
    notes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())

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


# Audit 2026-05-09 #14: every patron-controllable string field must be
# bounded at the model layer so a malicious 10MB payload does not land
# in saga.request_payload (JSONB / JSON column with no DB-level CHECK
# constraint). Caps reflect realistic library-data shapes — long titles
# and DOIs still fit; pathological inputs do not. The DB-level CHECK on
# ``pg_column_size(request_payload) < 64*1024`` is a defense-in-depth
# follow-up for Postgres deployments; the field-level caps below are
# the load-bearing primary defense at the API boundary.


class PatronRef(BaseModel):
    """Pseudonymous reference to a patron; resolves elsewhere.

    PII (real name, email) is intentionally not stored here. The
    consortium directory holds the mapping; we only persist a
    library-scoped opaque id.
    """

    model_config = ConfigDict(frozen=True)

    library_symbol: str = Field(max_length=32)
    patron_id: str = Field(max_length=64)


class LibraryRef(BaseModel):
    """Reference to a consortium member library by ISIL symbol."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(max_length=32)
    name: str | None = Field(default=None, max_length=256)


class ItemMetadata(BaseModel):
    """Bibliographic identity of the requested item."""

    title: str = Field(max_length=1024)
    author: str | None = Field(default=None, max_length=256)
    isbn: str | None = Field(default=None, max_length=32)
    issn: str | None = Field(default=None, max_length=32)
    doi: str | None = Field(default=None, max_length=256)
    oclc_number: str | None = Field(default=None, max_length=32)
    year: int | None = Field(default=None, ge=0, le=9999)
    edition: str | None = Field(default=None, max_length=128)
    publisher: str | None = Field(default=None, max_length=256)
    article_title: str | None = Field(default=None, max_length=1024)
    pages: str | None = Field(default=None, max_length=64)
    item_kind: str = Field(
        default="book",
        max_length=32,
        description="book|article|chapter|other",
    )
    item_barcode: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Physical item barcode from the supplying library's ILS. "
            "When present, used as item_id in NCIP check_out/check_in calls "
            "instead of the reshare_id approximation."
        ),
    )


class Citation(BaseModel):
    """Original citation context preserved for replay/audit."""

    raw: str = Field(max_length=4096)
    parsed_from: str = Field(max_length=32, description="openurl|freetext|identifier")
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
    notes: str | None = Field(default=None, max_length=4096)
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())

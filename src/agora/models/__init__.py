"""Domain models for Agora.

These are pydantic v2 models for in-process use. Database persistence
is handled by SQLAlchemy ORM in ``agora.saga.db``; the two layers map
into each other but are intentionally distinct so that wire/API
schemas can evolve independently of storage.
"""

from agora.models.candidate import HolderCandidate
from agora.models.events import NewSagaEvent, SagaEvent
from agora.models.lifecycle import (
    EventKind,
    Iso18626State,
    LifecycleState,
    StepKind,
    StepName,
    StepOutcome,
)
from agora.models.request import (
    Citation,
    IllRequest,
    ItemMetadata,
    LibraryRef,
    PatronRef,
    RequestType,
)

__all__ = [
    "Citation",
    "EventKind",
    "HolderCandidate",
    "IllRequest",
    "Iso18626State",
    "ItemMetadata",
    "LibraryRef",
    "LifecycleState",
    "NewSagaEvent",
    "PatronRef",
    "RequestType",
    "SagaEvent",
    "StepKind",
    "StepName",
    "StepOutcome",
]

"""Common exception hierarchy for client modules."""

from __future__ import annotations


class ClientError(Exception):
    """Base for all external-client errors."""


class NotFoundError(ClientError):
    """Resource not found on the remote side."""


class RemoteUnavailableError(ClientError):
    """Remote endpoint unreachable or 5xx."""


class IdempotencyConflictError(ClientError):
    """Same idempotency key reused with a different payload — caller bug."""

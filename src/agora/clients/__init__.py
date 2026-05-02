"""External system clients (ReShare, NCIP, SRU, OpenURL).

All clients share a few conventions:

- Every state-changing call accepts an ``idempotency_key`` parameter.
- Errors are raised as subclasses of ``ClientError`` so callers can
  catch a single hierarchy.
- Each client has a real implementation and a mock implementation; the
  mock is used in tests and when the corresponding service URL is not
  configured.
"""

from agora.clients.errors import ClientError, NotFoundError, RemoteUnavailableError

__all__ = ["ClientError", "NotFoundError", "RemoteUnavailableError"]

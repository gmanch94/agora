"""NCIP client.

Wraps FOLIO's ``mod-ncip`` for ILS circulation events tied to ILL
transactions: borrower check-out (RECEIVE forward) and check-in
(RETURN forward).

This module exposes a *Protocol* (``NcipClient``), an in-memory mock
(``MockNcipClient``) used for tests and when no ``NCIP_BASE_URL`` is
configured, and a real HTTP implementation (``HttpNcipClient``).

``HttpNcipClient`` targets the FOLIO ``mod-ncip`` 2.x HTTP API:

| Method     | NCIP 2.0 message | mod-ncip endpoint  |
|------------|------------------|--------------------|
| check_out  | CheckOutItem     | POST /ncip         |
| check_in   | CheckInItem      | POST /ncip         |
| health     | —                | GET /admin/health  |

**API verification (2026-05-06).** Endpoint paths, XML schema,
namespace URI, required elements, and error shapes verified against
mod-ncip master via source review:

- ``src/main/java/org/folio/ncip/MainVerticle.java``
  (``/ncip`` route, ``/admin/health`` route)
- ``resources/ncip-checkout.xml``, ``ncip-checkin.xml``,
  ``ncip-lookup-user.xml`` (sample request XML)
- ``src/test/java/org/folio/ncip/NCIPTest.java``
  (response parsing assertions, Problem element shape)
- ``src/main/resources/toolkit.properties`` (schema URI binding)
- ``descriptors/ModuleDescriptor-template.json``
  (path pattern, permission requirements)

Notes from source review (2026-05-06):

1. **NCIP 2.0 namespace** ``http://www.niso.org/2008/ncip``.
   The ``version`` attribute on ``NCIPMessage`` is the schema URI
   ``http://www.niso.org/schemas/ncip/v2_0/ncip_v2_0.xsd``.

2. **Auth.** mod-ncip requires ``X-Okapi-Tenant`` in all calls
   (used to look up per-tenant configuration from
   ``mod-configuration``). When going through Okapi,
   ``X-Okapi-Token`` is also required. When ``OKAPI_URL`` is
   configured, ``HttpNcipClient`` obtains the token via the same
   :class:`~agora.clients.okapi_auth.OkapiAuth` flow used by
   ``HttpReShareClient``; otherwise no token is sent (dev /
   module-direct).

3. **InitiationHeader required.** All wire samples include
   ``FromAgencyId`` / ``ToAgencyId``. The client sends
   ``NCIP_AGENCY_ID`` for both (same institution — borrower ILS
   calling its own mod-ncip).

4. **AgencyId inside UserId / ItemId.** Present in all samples;
   ``NCIP_AGENCY_ID`` is used.

5. **Response error shapes.** Two distinct shapes:

   - HTTP 5xx + raw ``<Problem><message>…</message></Problem>``
     (not NCIP-namespaced): infrastructure / processing failure.
   - HTTP 200 + ``<NCIPMessage><CheckOutItemResponse>
     <Problem><ProblemValue>…</ProblemValue></Problem>``
     (NCIP-namespaced): application-level failure.

   Both surfaces raise :class:`NcipError`.

6. **Health check.** ``GET /admin/health`` returns HTTP 200 (standard
   FOLIO health endpoint, no auth required). ``GET /ncipconfigcheck``
   is heavier (reads mod-configuration) and is reserved for ops.

7. **No tenacity retry.** Unlike ``HttpReShareClient``, NCIP calls
   are ILS state mutations. Retrying at the client level risks
   duplicate circulation events if mod-ncip does not deduplicate on
   ``Idempotency-Key``. Retry lives in the outbox worker, which
   always replays with the same idempotency key.

8. **Unverified against live tenant.** Source-review-only; live
   integration testing requires a FOLIO tenant with mod-ncip
   deployed and configured. The factory falls back to
   ``MockNcipClient`` when ``NCIP_BASE_URL`` is unset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

import httpx
from lxml import etree

from agora.clients.errors import ClientError, RemoteUnavailableError
from agora.clients.okapi_auth import OkapiAuth
from agora.config import Settings, get_settings
from agora.logging import get_logger

log = get_logger(__name__)

# NCIP 2.0 XML constants — all wire samples use these exactly.
_NS = "http://www.niso.org/2008/ncip"
_VERSION_ATTR = "http://www.niso.org/schemas/ncip/v2_0/ncip_v2_0.xsd"
_NSMAP: dict[None, str] = {None: _NS}
# Safe XML parser: no entity expansion, no network access (prevents XXE).
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


class NcipError(ClientError):
    """NCIP application-level or infrastructure error."""


@dataclass(slots=True)
class NcipResult:
    """Result of a successful NCIP circulation call.

    ``patron_id`` is an empty string for ``check_in`` — mod-ncip does
    not echo patron identity in CheckInItem responses; neither
    implementation attempts to recover it.
    """

    item_id: str
    patron_id: str
    state: str  # 'checked_out' | 'checked_in'


class NcipClient(Protocol):
    """Behavioural contract for any NCIP implementation."""

    async def check_out(
        self, *, idempotency_key: str, item_id: str, patron_id: str
    ) -> NcipResult: ...

    async def check_in(
        self, *, idempotency_key: str, item_id: str
    ) -> NcipResult: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


class MockNcipClient:
    """In-memory NCIP double for prototype/tests.

    Idempotency is enforced via ``_idem`` dict keyed on
    ``idempotency_key``. ``patron_id`` is preserved for ``check_out``
    results; ``check_in`` returns an empty ``patron_id`` to match
    ``HttpNcipClient`` behaviour (mod-ncip does not echo patron
    identity in CheckInItem responses).
    """

    def __init__(self) -> None:
        self._idem: dict[str, NcipResult] = {}

    async def check_out(
        self, *, idempotency_key: str, item_id: str, patron_id: str
    ) -> NcipResult:
        if (prior := self._idem.get(idempotency_key)) is not None:
            return prior
        result = NcipResult(item_id=item_id, patron_id=patron_id, state="checked_out")
        self._idem[idempotency_key] = result
        return result

    async def check_in(self, *, idempotency_key: str, item_id: str) -> NcipResult:
        if (prior := self._idem.get(idempotency_key)) is not None:
            return prior
        result = NcipResult(item_id=item_id, patron_id="", state="checked_in")
        self._idem[idempotency_key] = result
        return result

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _e(parent: etree._Element, tag: str, text: str | None = None) -> etree._Element:
    """Append a namespaced child element; optionally set its text."""
    el = etree.SubElement(parent, f"{{{_NS}}}{tag}")
    if text is not None:
        el.text = text
    return el


def _build_checkout_xml(agency_id: str, patron_id: str, item_id: str) -> bytes:
    """Return CheckOutItem NCIPMessage as UTF-8 XML bytes.

    Structure mirrors the mod-ncip sample ``ncip-checkout.xml``::

        <NCIPMessage version="..." xmlns="...">
          <CheckOutItem>
            <InitiationHeader>
              <FromAgencyId><AgencyId>AGENCY</AgencyId></FromAgencyId>
              <ToAgencyId><AgencyId>AGENCY</AgencyId></ToAgencyId>
            </InitiationHeader>
            <UserId>
              <AgencyId>AGENCY</AgencyId>
              <UserIdentifierValue>PATRON_ID</UserIdentifierValue>
            </UserId>
            <ItemId>
              <AgencyId>AGENCY</AgencyId>
              <ItemIdentifierValue>ITEM_ID</ItemIdentifierValue>
            </ItemId>
          </CheckOutItem>
        </NCIPMessage>
    """
    msg = etree.Element(
        f"{{{_NS}}}NCIPMessage",
        attrib={"version": _VERSION_ATTR},
        nsmap=_NSMAP,
    )
    checkout = _e(msg, "CheckOutItem")

    init = _e(checkout, "InitiationHeader")
    from_ag = _e(init, "FromAgencyId")
    _e(from_ag, "AgencyId", agency_id)
    to_ag = _e(init, "ToAgencyId")
    _e(to_ag, "AgencyId", agency_id)

    user = _e(checkout, "UserId")
    _e(user, "AgencyId", agency_id)
    _e(user, "UserIdentifierValue", patron_id)

    item = _e(checkout, "ItemId")
    _e(item, "AgencyId", agency_id)
    _e(item, "ItemIdentifierValue", item_id)

    return cast(bytes, etree.tostring(msg, xml_declaration=True, encoding="UTF-8"))


def _build_checkin_xml(agency_id: str, item_id: str) -> bytes:
    """Return CheckInItem NCIPMessage as UTF-8 XML bytes.

    Structure mirrors the mod-ncip sample ``ncip-checkin.xml``::

        <NCIPMessage version="..." xmlns="...">
          <CheckInItem>
            <InitiationHeader>
              <FromAgencyId><AgencyId>AGENCY</AgencyId></FromAgencyId>
              <ToAgencyId><AgencyId>AGENCY</AgencyId></ToAgencyId>
            </InitiationHeader>
            <ItemId>
              <AgencyId>AGENCY</AgencyId>
              <ItemIdentifierValue>ITEM_ID</ItemIdentifierValue>
            </ItemId>
          </CheckInItem>
        </NCIPMessage>
    """
    msg = etree.Element(
        f"{{{_NS}}}NCIPMessage",
        attrib={"version": _VERSION_ATTR},
        nsmap=_NSMAP,
    )
    checkin = _e(msg, "CheckInItem")

    init = _e(checkin, "InitiationHeader")
    from_ag = _e(init, "FromAgencyId")
    _e(from_ag, "AgencyId", agency_id)
    to_ag = _e(init, "ToAgencyId")
    _e(to_ag, "AgencyId", agency_id)

    item = _e(checkin, "ItemId")
    _e(item, "AgencyId", agency_id)
    _e(item, "ItemIdentifierValue", item_id)

    return cast(bytes, etree.tostring(msg, xml_declaration=True, encoding="UTF-8"))


def _parse_response(status_code: int, content: bytes) -> None:
    """Check mod-ncip response for errors; raise :class:`NcipError` on failure.

    Two error shapes (module docstring note 5):

    - HTTP 5xx: infrastructure failure — raw ``<Problem><message>``
      XML (not NCIP-namespaced).
    - HTTP 200 + ``<Problem>`` inside NCIPMessage response element:
      application-level failure (e.g. ``PIN_CHECK_FAILED``).

    Success (HTTP 200, no Problem element) returns normally.
    """
    if status_code >= 500:
        try:
            root = etree.fromstring(content, _XML_PARSER)
            msg_text = root.findtext("message") or root.text or "(no message)"
        except etree.XMLSyntaxError:
            msg_text = content.decode("utf-8", errors="replace")[:200]
        raise NcipError(f"NCIP infrastructure error {status_code}: {msg_text}")

    if status_code >= 400:
        raise NcipError(f"NCIP HTTP error {status_code}")

    # HTTP 200 — check for application-level Problem inside NCIPMessage.
    try:
        root = etree.fromstring(content, _XML_PARSER)
    except etree.XMLSyntaxError as exc:
        raise NcipError(f"NCIP response parse error: {exc}") from exc
    problem = root.find(f".//{{{_NS}}}Problem")
    if problem is not None:
        value = problem.findtext(f"{{{_NS}}}ProblemValue") or "(no value)"
        raise NcipError(f"NCIP application error: {value}")


# ---------------------------------------------------------------------------
# Real HTTP client
# ---------------------------------------------------------------------------


class HttpNcipClient:
    """Real HTTP client for FOLIO mod-ncip.

    Sends NCIP 2.0 XML messages to ``POST /ncip``. Requires
    ``NCIP_BASE_URL`` to be set. Auth follows the same strategy as
    ``HttpReShareClient``: Okapi token flow when ``OKAPI_URL`` is
    configured, anonymous otherwise (dev / module-direct).

    See module docstring notes 7-8: source-review-only; live tenant
    testing is still required before production use. No tenacity
    retry at the client level — see note 7 for rationale.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        s = settings or get_settings()
        if not s.ncip_base_url:
            raise ClientError("NCIP_BASE_URL not configured")
        self._base_url = s.ncip_base_url.rstrip("/")
        self._agency_id = s.ncip_agency_id
        self._tenant = s.reshare_tenant  # same FOLIO deployment
        auth: httpx.Auth | None
        if s.okapi_url:
            auth = OkapiAuth(
                login_url=f"{s.okapi_url.rstrip('/')}/authn/login",
                tenant=s.reshare_tenant,
                username=s.reshare_user,
                password=s.reshare_password,
            )
        else:
            auth = None
        self._client = httpx.AsyncClient(timeout=10.0, auth=auth)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        h: dict[str, str] = {
            "X-Okapi-Tenant": self._tenant,
            "Content-Type": "application/xml",
        }
        if idempotency_key is not None:
            # mod-ncip may not honour this header; kept for log-correlation.
            h["Idempotency-Key"] = idempotency_key
        return h

    async def _post_ncip(self, *, idempotency_key: str, body: bytes) -> None:
        """POST XML to /ncip; raise :class:`NcipError` or
        :class:`~agora.clients.errors.RemoteUnavailableError` on failure."""
        url = f"{self._base_url}/ncip"
        try:
            resp = await self._client.post(
                url,
                content=body,
                headers=self._headers(idempotency_key=idempotency_key),
            )
        except httpx.RequestError as exc:
            raise RemoteUnavailableError(str(exc)) from exc
        _parse_response(resp.status_code, resp.content)

    async def check_out(
        self, *, idempotency_key: str, item_id: str, patron_id: str
    ) -> NcipResult:
        """Send CheckOutItem to mod-ncip (borrower picks up the item)."""
        body = _build_checkout_xml(self._agency_id, patron_id, item_id)
        log.info(
            "ncip.check_out",
            item_id=item_id,
            patron_id=patron_id,
            idempotency_key=idempotency_key,
        )
        await self._post_ncip(idempotency_key=idempotency_key, body=body)
        return NcipResult(item_id=item_id, patron_id=patron_id, state="checked_out")

    async def check_in(self, *, idempotency_key: str, item_id: str) -> NcipResult:
        """Send CheckInItem to mod-ncip (borrower returns the item)."""
        body = _build_checkin_xml(self._agency_id, item_id)
        log.info("ncip.check_in", item_id=item_id, idempotency_key=idempotency_key)
        await self._post_ncip(idempotency_key=idempotency_key, body=body)
        return NcipResult(item_id=item_id, patron_id="", state="checked_in")

    async def health(self) -> bool:
        """GET /admin/health — lightweight FOLIO health probe, no auth required."""
        try:
            resp = await self._client.get(f"{self._base_url}/admin/health")
            return resp.status_code == 200
        except httpx.RequestError:
            return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_client() -> NcipClient:
    """Factory: real HTTP client when ``NCIP_BASE_URL`` is set, else mock."""
    s = get_settings()
    if s.ncip_base_url:
        log.info("ncip.client.using_http", base_url=s.ncip_base_url)
        return HttpNcipClient(s)
    log.info("ncip.client.using_mock")
    return MockNcipClient()

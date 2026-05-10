"""Shared safe-XML primitives for all clients.

One source of truth for the lxml parser configuration used to read
untrusted XML responses (SRU MARCXML, NCIP envelopes, future ISO
18626 inbound). Every client that calls ``etree.fromstring`` on
attacker-controllable bytes MUST pass ``SAFE_XML_PARSER`` to disable
external entity resolution (XXE), network access during DTD loading,
and unbounded tree growth (billion-laughs / quadratic-blowup).

Background: a 2026-05-09 audit found ``clients/sru.py`` calling
``etree.fromstring`` with the bare default parser, which resolves
entities by default and would have been a file-read primitive against
a malicious or compromised SRU server. ``clients/ncip.py`` had a
correctly-configured local parser; this module unifies both so the
same hardening applies anywhere new XML parsing is added.
"""

from __future__ import annotations

from lxml import etree

SAFE_XML_PARSER: etree.XMLParser = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
)
"""Use this parser everywhere XML is read from a peer.

- ``resolve_entities=False`` blocks XXE (external entity expansion to
  ``file:///`` or ``http://`` URLs).
- ``no_network=True`` blocks DTD/entity fetches even if a malformed
  payload references one.
- ``huge_tree=False`` enforces lxml's default size limits, blocking
  billion-laughs and quadratic-blowup amplification attacks.

The constant is module-level (not a per-call factory) because lxml
parsers are reusable across threads in CPython for read-only
``fromstring`` calls; sharing the instance avoids per-parse setup
cost without changing safety properties.
"""

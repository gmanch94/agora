"""Round-trip the IllRequest fixture corpus through pydantic.

`tests/fixtures/requests/` ships hand-crafted realistic payloads (book
loan, article copy, chapter copy, dissertation, multi-author, monograph
with barcode). This test guarantees the corpus stays in lock-step with
the `IllRequest` model — a breaking model change (renamed field, new
required field, tightened max_length) fails here loudly.

The corpus is also a stand-alone artifact for demos / docs / fuzz seeds;
the test below is the canary that catches drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agora.models.request import IllRequest

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "requests"
_PAYLOADS = sorted(_FIXTURES.glob("*.json"))


def test_fixture_corpus_present() -> None:
    """Sanity: corpus is not silently empty after a botched rebase."""
    assert _PAYLOADS, f"no fixtures discovered under {_FIXTURES}"
    assert len(_PAYLOADS) >= 6, (
        f"expected >= 6 fixtures, got {len(_PAYLOADS)}: "
        f"{[p.name for p in _PAYLOADS]}"
    )


@pytest.mark.parametrize("payload_path", _PAYLOADS, ids=lambda p: p.name)
def test_fixture_round_trips_through_ill_request(payload_path: Path) -> None:
    """Each fixture must parse, re-serialise, and re-parse without loss."""
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    req = IllRequest.model_validate(raw)
    redumped = json.loads(req.model_dump_json())
    again = IllRequest.model_validate(redumped)
    assert again.request_id == req.request_id
    assert again.request_type == req.request_type
    assert again.item.title == req.item.title

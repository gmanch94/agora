# IllRequest fixture payloads

Hand-crafted realistic `IllRequest` JSON payloads. Six fixtures
covering the common request shapes:

| File                          | request_type | item_kind   | Notes                                                          |
| ----------------------------- | ------------ | ----------- | -------------------------------------------------------------- |
| `book-loan.json`              | loan         | book        | Standard monograph loan with ISBN-13.                          |
| `article-copy.json`           | copy         | article     | Journal article — ISSN + DOI + article_title + pages.          |
| `chapter-copy.json`           | copy         | chapter     | Book chapter copy — ISBN of host volume + article_title.       |
| `dissertation-loan.json`      | loan         | other       | Dissertation, no ISBN/DOI, OCLC number only.                   |
| `multi-author-book.json`      | loan         | book        | Long author string (multiple authors, et al.).                 |
| `monograph-with-barcode.json` | loan         | book        | Includes `item_barcode` for NCIP `check_out` on RECEIVE.       |

## Round-trip test

```python
import json
from pathlib import Path
from agora.models.request import IllRequest

for p in Path("tests/fixtures/requests").glob("*.json"):
    payload = json.loads(p.read_text())
    req = IllRequest.model_validate(payload)
    # Round-trip is non-destructive: serialise back and re-validate.
    assert IllRequest.model_validate(json.loads(req.model_dump_json()))
```

## Why these exist

- Quick demo / docs material — paste into staff console `POST /requests`.
- Realistic fuzz seeds (couple with Hypothesis for property tests).
- Shape diversity for downstream-agent unit tests (DiscoveryAgent,
  RoutingAgent, PolicyAgent) without inline dict construction in every
  test file.

These are NOT golden-output fixtures — they're inputs. Field values use
fictitious patron IDs (`patron-XXXXX`) and fictitious library symbols
(`US-NLA-001` etc.) — none of these resolve to real libraries.

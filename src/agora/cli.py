"""Minimal CLI entry point.

For now just prints version + config summary. Extended later with
``agora demo``, ``agora chaos``, etc.

Audit 2026-05-09 #10: ``--config`` no longer prints credentials in
plaintext. ``SecretStr``-typed fields render as ``**********`` via
pydantic's built-in masking; the loop below also belt-and-suspenders
masks any field whose key contains ``password`` / ``token`` / ``secret``
/ ``key`` so a future regular-string credential field doesn't slip
through. ``db_url`` is also redacted (URL embeds creds).
"""

from __future__ import annotations

import argparse
import sys

from pydantic import SecretStr

from agora import __version__
from agora.config import get_settings

# Field-name fragments that imply a credential — used as a defense in
# depth alongside SecretStr typing. If a future field is named (e.g.)
# ``api_key`` and someone forgets to type it as SecretStr, the CLI
# still redacts the printed value.
_CREDENTIAL_KEY_FRAGMENTS = ("password", "token", "secret", "key", "credential")


def _redact(key: str, value: object) -> str:
    """Render a config value with credential masking."""
    if isinstance(value, SecretStr):
        return "**********" if value.get_secret_value() else ""
    if any(frag in key.lower() for frag in _CREDENTIAL_KEY_FRAGMENTS) and value:
        return "**********"
    if "db_url" in key.lower() and value:
        # Redact the URL too — it embeds credentials in production.
        return "**********"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agora", description="Agora ILL CLI")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--config", action="store_true", help="print effective config")
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0
    if args.config:
        s = get_settings()
        for k, v in s.model_dump().items():
            print(f"{k}={_redact(k, v)}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Minimal CLI entry point.

For now just prints version + config summary. Extended later with
``agora demo``, ``agora chaos``, etc.
"""

from __future__ import annotations

import argparse
import sys

from agora import __version__
from agora.config import get_settings


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
            print(f"{k}={v}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Download missing KML exports from the authenticated WiGLE account."""

from __future__ import annotations

import os
from pathlib import Path

from app.wigle_sync import sync_once

ROOT = Path(__file__).resolve().parent


def credentials() -> tuple[str, str]:
    api_name = os.environ.get("WIGLE_API_NAME")
    api_token = os.environ.get("WIGLE_API_TOKEN")
    if not api_name or not api_token:
        raise SystemExit("Set WIGLE_API_NAME and WIGLE_API_TOKEN before running.")
    return api_name, api_token


def main() -> None:
    api_name, api_token = credentials()
    result = sync_once(
        api_name,
        api_token,
        ROOT / "imports",
        ROOT,
    )
    available = result["downloaded"] + result["existing"]
    print(
        f"Complete: {available}/{result['transactions']} KML files available "
        f"({result['downloaded']} downloaded, {result['failed']} failed)"
    )


if __name__ == "__main__":
    main()

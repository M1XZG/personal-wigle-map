#!/usr/bin/env python3
"""Download every available KML export from the authenticated WiGLE account."""

from __future__ import annotations

import base64
import csv
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_URL = "https://api.wigle.net/api/v2"
ROOT = Path(__file__).resolve().parent
KML_DIR = ROOT / "imports" / "kml" / "raw"
MANIFEST_JSON = ROOT / "manifest.json"
MANIFEST_CSV = ROOT / "manifest.csv"
PAGE_SIZE = 100


def authorization_header() -> str:
    api_name = os.environ.get("WIGLE_API_NAME")
    api_token = os.environ.get("WIGLE_API_TOKEN")
    if not api_name or not api_token:
        raise SystemExit("Set WIGLE_API_NAME and WIGLE_API_TOKEN before running.")

    encoded = base64.b64encode(f"{api_name}:{api_token}".encode()).decode()
    return f"Basic {encoded}"


def request(url: str, authorization: str, attempts: int = 5) -> bytes:
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": authorization,
                "User-Agent": "personal-wigle-map/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == attempts:
                raise
            delay = min(60, 2**attempt)
            print(f"HTTP {error.code}; retrying in {delay}s")
            time.sleep(delay)
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts:
                raise
            delay = min(60, 2**attempt)
            print(f"Request failed; retrying in {delay}s")
            time.sleep(delay)

    raise RuntimeError("Request retries exhausted")


def fetch_transactions(authorization: str) -> list[dict]:
    transactions: list[dict] = []
    offset = 0

    while True:
        url = (
            f"{BASE_URL}/file/transactions"
            f"?pagestart={offset}&pageend={PAGE_SIZE}"
        )
        payload = json.loads(request(url, authorization))
        if not payload.get("success"):
            raise RuntimeError(f"WiGLE transaction request failed: {payload}")

        rows = payload.get("results", [])
        transactions.extend(rows)
        print(f"Listed {len(transactions)} uploads")

        if len(rows) < PAGE_SIZE:
            return transactions
        offset += PAGE_SIZE


def write_manifests(transactions: list[dict]) -> None:
    MANIFEST_JSON.write_text(
        json.dumps(transactions, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    columns = [
        "transid",
        "firstTime",
        "lastupdt",
        "fileName",
        "fileSize",
        "fileLines",
        "status",
        "discoveredGps",
        "totalGps",
        "totalLocations",
        "genDiscoveredGps",
        "genTotalGps",
        "btDiscoveredGps",
        "btTotalGps",
        "brand",
        "model",
        "osRelease",
        "kmlStatus",
        "kmlBytes",
    ]
    with MANIFEST_CSV.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(transactions)


def download_kml_files(transactions: list[dict], authorization: str) -> None:
    KML_DIR.mkdir(parents=True, exist_ok=True)
    total = len(transactions)

    for index, transaction in enumerate(transactions, start=1):
        transid = transaction["transid"]
        destination = KML_DIR / f"{transid}.kml"

        if destination.exists() and destination.stat().st_size:
            transaction["kmlStatus"] = "existing"
            transaction["kmlBytes"] = destination.stat().st_size
            continue

        try:
            content = request(f"{BASE_URL}/file/kml/{transid}", authorization)
            if not content.lstrip().startswith(b"<?xml") and b"<kml" not in content[:1000]:
                raise RuntimeError("response was not KML")
            destination.write_bytes(content)
            transaction["kmlStatus"] = "downloaded"
            transaction["kmlBytes"] = len(content)
            print(f"[{index}/{total}] downloaded {transid} ({len(content):,} bytes)")
        except urllib.error.HTTPError as error:
            transaction["kmlStatus"] = f"http-{error.code}"
            transaction["kmlBytes"] = 0
            print(f"[{index}/{total}] unavailable {transid}: HTTP {error.code}")
        except Exception as error:
            transaction["kmlStatus"] = f"error: {error}"
            transaction["kmlBytes"] = 0
            print(f"[{index}/{total}] failed {transid}: {error}")

        write_manifests(transactions)
        time.sleep(0.2)


def main() -> None:
    authorization = authorization_header()
    transactions = fetch_transactions(authorization)
    download_kml_files(transactions, authorization)
    write_manifests(transactions)

    downloaded = sum(
        1
        for transaction in transactions
        if transaction.get("kmlStatus") in {"downloaded", "existing"}
    )
    print(f"Complete: {downloaded}/{len(transactions)} KML files available locally")


if __name__ == "__main__":
    main()

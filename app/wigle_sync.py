"""Download missing KML exports from an authenticated WiGLE account."""

from __future__ import annotations

import base64
import csv
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_URL = "https://api.wigle.net/api/v2"
PAGE_SIZE = 100
SAFE_TRANSID = re.compile(r"^[A-Za-z0-9_-]+$")
logger = logging.getLogger(__name__)


def authorization_header(api_name: str, api_token: str) -> str:
    encoded = base64.b64encode(f"{api_name}:{api_token}".encode()).decode()
    return f"Basic {encoded}"


def request_bytes(url: str, authorization: str, attempts: int = 5) -> bytes:
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": authorization,
                "User-Agent": "personal-wigle-map/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == attempts:
                raise
            delay = min(60, 2**attempt)
            logger.warning("WiGLE returned HTTP %s; retrying in %ss", error.code, delay)
            time.sleep(delay)
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts:
                raise
            delay = min(60, 2**attempt)
            logger.warning("WiGLE request failed; retrying in %ss", delay)
            time.sleep(delay)
    raise RuntimeError("WiGLE request retries exhausted")


def fetch_transactions(authorization: str) -> list[dict]:
    transactions: list[dict] = []
    offset = 0
    while True:
        url = (
            f"{BASE_URL}/file/transactions"
            f"?pagestart={offset}&pageend={PAGE_SIZE}"
        )
        payload = json.loads(request_bytes(url, authorization))
        if not payload.get("success"):
            raise RuntimeError("WiGLE transaction request was unsuccessful")
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise RuntimeError("WiGLE transaction response has no results list")
        transactions.extend(row for row in rows if isinstance(row, dict))
        if len(rows) < PAGE_SIZE:
            return transactions
        offset += PAGE_SIZE


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".partial",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def write_manifests(transactions: list[dict], state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    json_content = (
        json.dumps(transactions, indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_write(state_dir / "manifest.json", json_content)

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
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=state_dir,
            prefix=".manifest.csv.",
            suffix=".partial",
            newline="",
            encoding="utf-8",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            writer = csv.DictWriter(
                output,
                fieldnames=columns,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(transactions)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(state_dir / "manifest.csv")
    except Exception:
        if "temporary" in locals():
            temporary.unlink(missing_ok=True)
        raise


def download_kml_files(
    transactions: list[dict],
    authorization: str,
    kml_dir: Path,
    delay_seconds: float = 0.2,
) -> dict[str, int]:
    kml_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    existing = 0
    failed = 0

    for transaction in transactions:
        transid = str(transaction.get("transid", ""))
        if not SAFE_TRANSID.fullmatch(transid):
            transaction["kmlStatus"] = "invalid-transaction-id"
            transaction["kmlBytes"] = 0
            failed += 1
            continue

        destination = kml_dir / f"{transid}.kml"
        if destination.is_file() and destination.stat().st_size:
            transaction["kmlStatus"] = "existing"
            transaction["kmlBytes"] = destination.stat().st_size
            existing += 1
            continue

        try:
            content = request_bytes(
                f"{BASE_URL}/file/kml/{transid}",
                authorization,
            )
            if not content.lstrip().startswith(b"<?xml") and b"<kml" not in content[:1000]:
                raise RuntimeError("WiGLE response was not KML")
            _atomic_write(destination, content)
            transaction["kmlStatus"] = "downloaded"
            transaction["kmlBytes"] = len(content)
            downloaded += 1
        except urllib.error.HTTPError as error:
            transaction["kmlStatus"] = f"http-{error.code}"
            transaction["kmlBytes"] = 0
            failed += 1
        except Exception as error:
            transaction["kmlStatus"] = f"error: {type(error).__name__}"
            transaction["kmlBytes"] = 0
            failed += 1

        if delay_seconds:
            time.sleep(delay_seconds)

    return {
        "downloaded": downloaded,
        "existing": existing,
        "failed": failed,
    }


def sync_once(
    api_name: str,
    api_token: str,
    import_root: Path,
    state_dir: Path,
) -> dict[str, int]:
    authorization = authorization_header(api_name, api_token)
    transactions = fetch_transactions(authorization)
    counts = download_kml_files(
        transactions,
        authorization,
        import_root / "kml" / "raw",
    )
    write_manifests(transactions, state_dir)
    return {"transactions": len(transactions), **counts}

"""Idempotent importers for WiGLE SQLite, CSV, and KML exports."""

from __future__ import annotations

import csv
import gzip
import hashlib
import itertools
import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .storage import (
    connect_database,
    initialize_database,
    observation_fingerprint,
    rebuild_routes,
    refresh_network_aggregates,
)

SUPPORTED_SUFFIXES = {".sqlite", ".db", ".csv", ".gz", ".kml", ".gpx"}
DESCRIPTION_FIELD = re.compile(r"^([^:]+):\s*(.*)$")
DATE_DIRECTORY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEVICE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
DEVICE_LABELS_FILE = "device-labels.json"
logger = logging.getLogger(__name__)
TYPE_MAP = {
    "W": "WIFI",
    "WIFI": "WIFI",
    "E": "BLE",
    "BLE": "BLE",
    "B": "BLUETOOTH",
    "BT": "BLUETOOTH",
    "BLUETOOTH": "BLUETOOTH",
    "L": "LTE",
    "LTE": "LTE",
    "G": "GSM",
    "GSM": "GSM",
    "C": "CDMA",
    "CDMA": "CDMA",
}


@dataclass(slots=True)
class NetworkRecord:
    network_type: str
    identifier: str
    name: str = ""
    capabilities: str = ""
    encryption: str = ""
    frequency: int | None = None
    channel: str = ""


@dataclass(slots=True)
class ObservationRecord:
    network: NetworkRecord
    observed_at: int
    latitude: float
    longitude: float
    signal: float | None = None
    accuracy: float | None = None
    altitude: float | None = None
    external: int = 0


@dataclass(slots=True)
class RouteRecord:
    observed_at: int
    latitude: float
    longitude: float
    altitude: float | None = None
    accuracy: float | None = None
    external: int = 0
    run_id: str = ""
    genuine: int = 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_type(value: object) -> str:
    text = str(value or "UNKNOWN").strip().upper()
    return TYPE_MAP.get(text, text or "UNKNOWN")


def _normalize_identifier(value: object) -> str:
    text = str(value or "").strip()
    if ":" in text or "-" in text:
        compact = re.sub(r"[^0-9A-Fa-f]", "", text)
        if len(compact) == 12:
            return ":".join(compact[index : index + 2] for index in range(0, 12, 2)).upper()
    return text.upper()


def _float(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _int(value: object) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _timestamp(value: object) -> int:
    if value is None:
        raise ValueError("missing observation timestamp")
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        number = int(float(value))
        return number // 1000 if number > 10_000_000_000 else number
    text = str(value).strip()
    normalized = text
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", normalized):
        normalized = normalized[:-5] + normalized[-5:-2] + ":" + normalized[-2:]
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for pattern in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"unsupported timestamp: {text}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _valid_coordinates(latitude: float | None, longitude: float | None) -> bool:
    return (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
        and not (latitude == 0 and longitude == 0)
    )


def _observation_hash(record: ObservationRecord) -> str:
    return observation_fingerprint(
        record.network.network_type,
        record.network.identifier,
        record.observed_at,
        record.latitude,
        record.longitude,
    )


def _infer_device(path: Path, import_root: Path | None = None) -> str:
    if path.parent.name == "raw" and path.parent.parent.name == "kml":
        labels_path = path.parent / DEVICE_LABELS_FILE
        if labels_path.is_file():
            try:
                labels = json.loads(labels_path.read_text(encoding="utf-8"))
                label = labels.get(path.name) if isinstance(labels, dict) else None
                if isinstance(label, str) and DEVICE_SLUG.fullmatch(label):
                    return label
                if label is not None:
                    logger.warning("Ignoring invalid device label for %s", path.name)
            except (OSError, json.JSONDecodeError):
                logger.warning("Could not read WiGLE device labels from %s", labels_path)
        return "wigle-account"
    parts = path.parts
    if "device-exports" in parts:
        index = parts.index("device-exports")
        if index + 1 < len(parts):
            return parts[index + 1]
    if import_root is not None:
        try:
            relative = path.relative_to(import_root)
            if len(relative.parts) > 1:
                if relative.parts[:2] == ("kml", "raw"):
                    return "wigle-account"
                return relative.parts[0]
        except ValueError:
            pass
    for parent in path.parents:
        name = parent.name
        if not name or DATE_DIRECTORY.match(name):
            continue
        if name.lower() in {"raw", "kml", "imports", "import"}:
            if name.lower() == "raw":
                return "wigle-account"
            continue
        return name
    return "unknown"


def _detect_format(path: Path) -> str:
    with path.open("rb") as source:
        prefix = source.read(32)
    if prefix.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    if prefix.startswith(b"\x1f\x8b"):
        if path.name.lower().endswith(".csv.gz"):
            return "csv.gz"
        raise ValueError("unsupported gzip content")
    if path.suffix.lower() == ".csv":
        return "csv"
    if path.suffix.lower() == ".gpx":
        return "gpx"
    if path.suffix.lower() in {".kml", ".xml"} or prefix.lstrip().startswith(b"<?xml"):
        return "kml"
    raise ValueError("unsupported import format")


def _parse_csv(path: Path) -> Iterator[tuple[ObservationRecord, RouteRecord | None]]:
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", errors="replace", newline="") as source:
        first = source.readline()
        if first.startswith("MAC,"):
            header = first
        else:
            header = source.readline()
        sanitized_lines = (line.replace("\x00", "") for line in source)
        reader = csv.DictReader(
            itertools.chain((header.replace("\x00", ""),), sanitized_lines)
        )
        required = {"MAC", "FirstSeen", "CurrentLatitude", "CurrentLongitude", "Type"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV does not contain the required WiGLE columns")
        for row in reader:
            latitude = _float(row.get("CurrentLatitude"))
            longitude = _float(row.get("CurrentLongitude"))
            if not _valid_coordinates(latitude, longitude):
                continue
            network = NetworkRecord(
                network_type=_normalize_type(row.get("Type")),
                identifier=_normalize_identifier(row.get("MAC")),
                name=(row.get("SSID") or "").strip(),
                capabilities=(row.get("AuthMode") or "").strip(),
                encryption=(row.get("AuthMode") or "").strip(),
                frequency=_int(row.get("Frequency")),
                channel=(row.get("Channel") or "").strip(),
            )
            if not network.identifier:
                continue
            observation = ObservationRecord(
                network=network,
                observed_at=_timestamp(row.get("FirstSeen")),
                latitude=latitude,
                longitude=longitude,
                signal=_float(row.get("RSSI")),
                accuracy=_float(row.get("AccuracyMeters")),
                altitude=_float(row.get("AltitudeMeters")),
            )
            yield observation, RouteRecord(
                observed_at=observation.observed_at,
                latitude=latitude,
                longitude=longitude,
                altitude=observation.altitude,
                accuracy=observation.accuracy,
            )


def _description_fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = DESCRIPTION_FIELD.match(line.strip())
        if match:
            result[match.group(1).strip().lower()] = match.group(2).strip()
    return result


def _child_text(element: ET.Element, local_name: str) -> str:
    for child in element.iter():
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return (child.text or "").strip()
    return ""


def _parse_kml(path: Path) -> Iterator[tuple[ObservationRecord, RouteRecord | None]]:
    with path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for _, element in ET.iterparse(source, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] != "Placemark":
                continue
            try:
                fields = _description_fields(_child_text(element, "description"))
                coordinates = _child_text(element, "coordinates").split()
                if not coordinates:
                    continue
                coordinate = coordinates[0].split(",")
                longitude, latitude = float(coordinate[0]), float(coordinate[1])
                if not _valid_coordinates(latitude, longitude):
                    continue
                identifier = _normalize_identifier(
                    fields.get("network id") or fields.get("bssid")
                )
                if not identifier:
                    continue
                observed_at = _timestamp(
                    fields.get("timestamp") or fields.get("time")
                )
                name = _child_text(element, "name")
                if name == "(no SSID)":
                    name = ""
                network = NetworkRecord(
                    network_type=_normalize_type(fields.get("type")),
                    identifier=identifier,
                    name=name,
                    capabilities=fields.get("capabilities", ""),
                    encryption=fields.get("encryption", ""),
                    frequency=_int(fields.get("frequency")),
                    channel=fields.get("channel", ""),
                )
                altitude = _float(coordinate[2]) if len(coordinate) > 2 else None
                observation = ObservationRecord(
                    network=network,
                    observed_at=observed_at,
                    latitude=latitude,
                    longitude=longitude,
                    signal=_float(fields.get("signal")),
                    accuracy=_float(fields.get("accuracy")),
                    altitude=altitude,
                )
                yield observation, RouteRecord(
                    observed_at=observed_at,
                    latitude=latitude,
                    longitude=longitude,
                    altitude=altitude,
                    accuracy=observation.accuracy,
                )
            finally:
                element.clear()


def _parse_gpx(path: Path) -> Iterator[RouteRecord]:
    track_segment = 0
    route_number = 0
    current_track_run = ""
    current_route_run = ""
    points_seen = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for event, element in ET.iterparse(source, events=("start", "end")):
            local_name = element.tag.rsplit("}", 1)[-1]
            if event == "start":
                if local_name == "trkseg":
                    track_segment += 1
                    current_track_run = f"gpx-track-{track_segment}"
                elif local_name == "rte":
                    route_number += 1
                    current_route_run = f"gpx-route-{route_number}"
                continue

            if local_name in {"trkpt", "rtept"}:
                latitude = _float(element.attrib.get("lat"))
                longitude = _float(element.attrib.get("lon"))
                time_text = _child_text(element, "time")
                if _valid_coordinates(latitude, longitude) and time_text:
                    points_seen += 1
                    yield RouteRecord(
                        observed_at=_timestamp(time_text),
                        latitude=latitude,
                        longitude=longitude,
                        altitude=_float(_child_text(element, "ele")),
                        run_id=(
                            current_track_run
                            if local_name == "trkpt"
                            else current_route_run
                        ),
                        genuine=1,
                    )
                element.clear()
            elif local_name == "trkseg":
                current_track_run = ""
                element.clear()
            elif local_name == "rte":
                current_route_run = ""
                element.clear()
        if not points_seen:
            raise ValueError("GPX does not contain timed track or route points")


def _source_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_sqlite(
    path: Path,
) -> tuple[Iterator[tuple[ObservationRecord, RouteRecord | None]], Iterator[RouteRecord]]:
    def observations() -> Iterator[tuple[ObservationRecord, RouteRecord | None]]:
        with _source_connection(path) as source:
            tables = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"network", "location"}.issubset(tables):
                raise ValueError("SQLite file is not a WiGLE full database")
            rows = source.execute(
                """
                SELECT n.bssid, n.ssid, n.frequency, n.capabilities, n.type,
                       l.level, l.lat, l.lon, l.altitude, l.accuracy, l.time,
                       l.external
                FROM location l JOIN network n ON n.bssid = l.bssid
                ORDER BY l._id
                """
            )
            for row in rows:
                latitude, longitude = float(row["lat"]), float(row["lon"])
                if not _valid_coordinates(latitude, longitude):
                    continue
                network = NetworkRecord(
                    network_type=_normalize_type(row["type"]),
                    identifier=_normalize_identifier(row["bssid"]),
                    name=row["ssid"] or "",
                    capabilities=row["capabilities"] or "",
                    encryption=row["capabilities"] or "",
                    frequency=row["frequency"],
                )
                observation = ObservationRecord(
                    network=network,
                    observed_at=_timestamp(row["time"]),
                    latitude=latitude,
                    longitude=longitude,
                    signal=_float(row["level"]),
                    accuracy=_float(row["accuracy"]),
                    altitude=_float(row["altitude"]),
                    external=int(row["external"] or 0),
                )
                yield observation, RouteRecord(
                    observed_at=observation.observed_at,
                    latitude=latitude,
                    longitude=longitude,
                    altitude=observation.altitude,
                    accuracy=observation.accuracy,
                    external=observation.external,
                )

    def routes() -> Iterator[RouteRecord]:
        with _source_connection(path) as source:
            has_route = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='route'"
            ).fetchone()
            if not has_route:
                return
            for row in source.execute(
                """
                SELECT run_id, lat, lon, altitude, accuracy, time
                FROM route ORDER BY time, _id
                """
            ):
                latitude, longitude = float(row["lat"]), float(row["lon"])
                if _valid_coordinates(latitude, longitude):
                    yield RouteRecord(
                        observed_at=_timestamp(row["time"]),
                        latitude=latitude,
                        longitude=longitude,
                        altitude=_float(row["altitude"]),
                        accuracy=_float(row["accuracy"]),
                        run_id=str(row["run_id"]),
                        genuine=1,
                    )

    return observations(), routes()


def _upsert_network(
    connection: sqlite3.Connection, record: NetworkRecord
) -> int:
    connection.execute(
        """
        INSERT INTO networks(
            network_type, identifier, name, encryption, capabilities, frequency, channel
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(network_type, identifier) DO UPDATE SET
            name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE networks.name END,
            encryption = CASE WHEN excluded.encryption <> '' THEN excluded.encryption
                              ELSE networks.encryption END,
            capabilities = CASE WHEN excluded.capabilities <> '' THEN excluded.capabilities
                                ELSE networks.capabilities END,
            frequency = COALESCE(excluded.frequency, networks.frequency),
            channel = CASE WHEN excluded.channel <> '' THEN excluded.channel
                           ELSE networks.channel END
        """,
        (
            record.network_type,
            record.identifier,
            record.name,
            record.encryption,
            record.capabilities,
            record.frequency,
            record.channel,
        ),
    )
    return connection.execute(
        "SELECT id FROM networks WHERE network_type = ? AND identifier = ?",
        (record.network_type, record.identifier),
    ).fetchone()[0]


def _insert_records(
    connection: sqlite3.Connection,
    records: Iterable[tuple[ObservationRecord, RouteRecord | None]],
    routes: Iterable[RouteRecord],
    import_id: int,
    source_path: str,
    device: str,
) -> tuple[int, int, int]:
    network_ids: set[int] = set()
    network_cache: dict[tuple[str, str], int] = {}
    observations_added = 0
    route_candidates_added = 0
    for observation, route in records:
        key = (
            observation.network.network_type,
            observation.network.identifier,
        )
        network_id = network_cache.get(key)
        if network_id is None:
            network_id = _upsert_network(connection, observation.network)
            network_cache[key] = network_id
        network_ids.add(network_id)
        connection.execute(
            "INSERT OR IGNORE INTO network_sources(network_id, import_id) VALUES (?, ?)",
            (network_id, import_id),
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO observations(
                observation_hash, network_id, observed_at, latitude, longitude,
                signal, accuracy, altitude, external, source_device, source_file,
                import_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _observation_hash(observation),
                network_id,
                observation.observed_at,
                observation.latitude,
                observation.longitude,
                observation.signal,
                observation.accuracy,
                observation.altitude,
                observation.external,
                device,
                source_path,
                import_id,
            ),
        )
        observations_added += cursor.rowcount
        observation_id = connection.execute(
            "SELECT id FROM observations WHERE observation_hash = ?",
            (_observation_hash(observation),),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT OR IGNORE INTO observation_sources(
                observation_id, import_id, source_device, source_file
            ) VALUES (?, ?, ?, ?)
            """,
            (observation_id, import_id, device, source_path),
        )
        if not cursor.rowcount:
            connection.execute(
                """
                UPDATE observations SET
                    signal = CASE
                        WHEN signal IS NULL THEN ?
                        WHEN ? IS NULL THEN signal
                        ELSE MAX(signal, ?)
                    END,
                    accuracy = CASE
                        WHEN accuracy IS NULL THEN ?
                        WHEN ? IS NULL THEN accuracy
                        ELSE MIN(accuracy, ?)
                    END,
                    altitude = CASE
                        WHEN altitude IS NULL THEN ?
                        WHEN ? IS NULL THEN altitude
                        ELSE MIN(altitude, ?)
                    END,
                    external = MAX(external, ?),
                    source_device = CASE
                        WHEN ? < source_device
                          OR (? = source_device AND ? < source_file)
                        THEN ? ELSE source_device END,
                    source_file = CASE
                        WHEN ? < source_device
                          OR (? = source_device AND ? < source_file)
                        THEN ? ELSE source_file END,
                    import_id = CASE
                        WHEN ? < source_device
                          OR (? = source_device AND ? < source_file)
                        THEN ? ELSE import_id END
                WHERE id = ?
                """,
                (
                    observation.signal,
                    observation.signal,
                    observation.signal,
                    observation.accuracy,
                    observation.accuracy,
                    observation.accuracy,
                    observation.altitude,
                    observation.altitude,
                    observation.altitude,
                    observation.external,
                    device,
                    device,
                    source_path,
                    device,
                    device,
                    device,
                    source_path,
                    source_path,
                    device,
                    device,
                    source_path,
                    import_id,
                    observation_id,
                ),
            )
        if route is not None:
            route_candidates_added += _insert_route_candidate(
                connection, route, import_id, source_path, device
            )
    for route in routes:
        route_candidates_added += _insert_route_candidate(
            connection, route, import_id, source_path, device
        )
    refresh_network_aggregates(connection, network_ids)
    return len(network_ids), observations_added, route_candidates_added


def _insert_route_candidate(
    connection: sqlite3.Connection,
    route: RouteRecord,
    import_id: int,
    source_path: str,
    device: str,
) -> int:
    values = (
        device,
        route.observed_at,
        round(route.latitude, 6),
        round(route.longitude, 6),
        route.altitude,
        route.accuracy,
        route.external,
        route.run_id,
        source_path,
        import_id,
        route.genuine,
    )
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO route_candidates(
            device_label, observed_at, latitude, longitude, altitude, accuracy,
            external, explicit_run_id, source_file, import_id, genuine_route
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    if route.genuine and not cursor.rowcount:
        connection.execute(
            """
            UPDATE route_candidates
            SET altitude=?, accuracy=?, external=?, explicit_run_id=?,
                source_file=?, import_id=?, genuine_route=1
            WHERE device_label=? AND observed_at=? AND latitude=? AND longitude=?
            """,
            (
                route.altitude,
                route.accuracy,
                route.external,
                route.run_id,
                source_path,
                import_id,
                device,
                route.observed_at,
                round(route.latitude, 6),
                round(route.longitude, 6),
            ),
        )
    return cursor.rowcount


def _relabel_import(
    connection: sqlite3.Connection,
    import_id: int,
    device: str,
) -> bool:
    existing = connection.execute(
        "SELECT device_label FROM imports WHERE id = ?",
        (import_id,),
    ).fetchone()
    if existing is None or existing["device_label"] == device:
        return False

    route_rows = connection.execute(
        """
        SELECT observed_at, latitude, longitude, altitude, accuracy, external,
               explicit_run_id, source_file, genuine_route
        FROM route_candidates WHERE import_id = ?
        """,
        (import_id,),
    ).fetchall()
    connection.execute(
        "DELETE FROM route_candidates WHERE import_id = ?",
        (import_id,),
    )
    connection.execute(
        "UPDATE imports SET device_label = ? WHERE id = ?",
        (device, import_id),
    )
    connection.execute(
        "UPDATE observation_sources SET source_device = ? WHERE import_id = ?",
        (device, import_id),
    )
    connection.execute(
        """
        UPDATE observations SET
            source_device = (
                SELECT os.source_device FROM observation_sources os
                WHERE os.observation_id = observations.id
                ORDER BY os.source_device, os.source_file, os.import_id LIMIT 1
            ),
            source_file = (
                SELECT os.source_file FROM observation_sources os
                WHERE os.observation_id = observations.id
                ORDER BY os.source_device, os.source_file, os.import_id LIMIT 1
            ),
            import_id = (
                SELECT os.import_id FROM observation_sources os
                WHERE os.observation_id = observations.id
                ORDER BY os.source_device, os.source_file, os.import_id LIMIT 1
            )
        WHERE id IN (
            SELECT observation_id FROM observation_sources WHERE import_id = ?
        )
        """,
        (import_id,),
    )
    for row in route_rows:
        _insert_route_candidate(
            connection,
            RouteRecord(
                observed_at=row["observed_at"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                altitude=row["altitude"],
                accuracy=row["accuracy"],
                external=row["external"],
                run_id=row["explicit_run_id"],
                genuine=row["genuine_route"],
            ),
            import_id,
            row["source_file"],
            device,
        )
    return bool(route_rows)


def import_file(
    path: str | Path,
    db_path: str | Path,
    device_hint: str | None = None,
    rebuild_routes_after: bool = True,
) -> dict[str, object]:
    """Import one source atomically and return its status."""
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    initialize_database(db_path)
    stat = source.stat()
    source_path = str(source)
    device = device_hint or _infer_device(source)
    routes_relabelled = False
    with connect_database(db_path) as connection:
        unchanged = connection.execute(
            """
            SELECT * FROM imports
            WHERE source_path = ? AND size_bytes = ? AND mtime_ns = ?
                  AND status = 'complete'
            ORDER BY id DESC LIMIT 1
            """,
            (source_path, stat.st_size, stat.st_mtime_ns),
        ).fetchone()
        if unchanged and unchanged["device_label"] != device:
            routes_relabelled = _relabel_import(
                connection,
                unchanged["id"],
                device,
            )
    if unchanged:
        if routes_relabelled and rebuild_routes_after:
            rebuild_routes(db_path)
        return {
            "path": source_path,
            "status": "skipped",
            "reason": "unchanged_file",
            "sha256": unchanged["sha256"],
            "device": device,
            "format": unchanged["format"],
            "networks": 0,
            "observations": 0,
            "route_points": 0,
            "routes_relabelled": routes_relabelled,
        }
    digest = _sha256(source)
    try:
        file_format = _detect_format(source)
    except Exception as error:
        file_format = "unknown"
        parse_error: Exception | None = error
    else:
        parse_error = None
    with connect_database(db_path) as connection:
        existing = connection.execute(
            "SELECT * FROM imports WHERE sha256 = ?", (digest,)
        ).fetchone()
        if existing and existing["status"] == "complete":
            return {
                "path": str(source),
                "status": "skipped",
                "reason": "duplicate_hash",
                "sha256": digest,
                "networks": 0,
                "observations": 0,
                "route_points": 0,
            }
        if existing:
            import_id = existing["id"]
            connection.execute(
                """
                UPDATE imports SET status='pending', error=NULL, network_count=0,
                    observation_count=0, route_point_count=0, source_path=?,
                    device_label=?, size_bytes=?, mtime_ns=?, format=?
                WHERE id=?
                """,
                (
                    source_path,
                    device,
                    stat.st_size,
                    stat.st_mtime_ns,
                    file_format,
                    import_id,
                ),
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO imports(
                    source_path, sha256, device_label, size_bytes, mtime_ns,
                    format, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    source_path,
                    digest,
                    device,
                    stat.st_size,
                    stat.st_mtime_ns,
                    file_format,
                ),
            )
            import_id = cursor.lastrowid
    try:
        if parse_error:
            raise parse_error
        with connect_database(db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if file_format in {"csv", "csv.gz"}:
                records, routes = _parse_csv(source), ()
            elif file_format == "kml":
                records, routes = _parse_kml(source), ()
            elif file_format == "sqlite":
                records, routes = _parse_sqlite(source)
            elif file_format == "gpx":
                records, routes = (), _parse_gpx(source)
            else:
                raise ValueError(f"unsupported format: {file_format}")
            networks, observations, route_candidates = _insert_records(
                connection, records, routes, import_id, source_path, device
            )
            imported_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                UPDATE imports SET status='complete', network_count=?,
                    observation_count=?, route_point_count=?, imported_at=?, error=NULL
                WHERE id=?
                """,
                (
                    networks,
                    observations,
                    route_candidates,
                    imported_at,
                    import_id,
                ),
            )
        route_summary = (
            rebuild_routes(db_path)
            if rebuild_routes_after
            else {"segments": 0, "route_points": 0}
        )
        return {
            "path": source_path,
            "status": "complete",
            "sha256": digest,
            "device": device,
            "format": file_format,
            "networks": networks,
            "observations": observations,
            "route_candidates": route_candidates,
            **route_summary,
        }
    except Exception as error:
        with connect_database(db_path) as connection:
            connection.execute(
                """
                UPDATE imports SET status='failed', imported_at=?, error=?
                WHERE id=?
                """,
                (datetime.now(timezone.utc).isoformat(), str(error)[:2000], import_id),
            )
        return {
            "path": source_path,
            "status": "failed",
            "sha256": digest,
            "device": device,
            "format": file_format,
            "networks": 0,
            "observations": 0,
            "route_points": 0,
            "error": str(error),
        }


def import_tree(import_root: str | Path, db_path: str | Path) -> dict[str, object]:
    """Recursively import all supported source files below a root."""
    root = Path(import_root).resolve()
    initialize_database(db_path)
    results: list[dict[str, object]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        suffixes = "".join(path.suffixes[-2:]).lower()
        if path.suffix.lower() not in SUPPORTED_SUFFIXES and suffixes != ".csv.gz":
            continue
        try:
            result = import_file(
                path,
                db_path,
                _infer_device(path, root),
                rebuild_routes_after=False,
            )
        except FileNotFoundError:
            result = {
                "path": str(path),
                "status": "skipped",
                "reason": "file_disappeared",
                "networks": 0,
                "observations": 0,
                "route_points": 0,
            }
        results.append(result)
        logger.info(
            "Import %s: %s (%s observations)",
            path.name,
            result["status"],
            result.get("observations", 0),
        )
    route_summary = (
        rebuild_routes(db_path)
        if any(
            result["status"] == "complete" or result.get("routes_relabelled")
            for result in results
        )
        else {"segments": 0, "route_points": 0}
    )
    return {
        "files": len(results),
        "complete": sum(result["status"] == "complete" for result in results),
        "skipped": sum(result["status"] == "skipped" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "networks": sum(int(result.get("networks", 0)) for result in results),
        "observations": sum(int(result.get("observations", 0)) for result in results),
        **route_summary,
        "results": results,
    }

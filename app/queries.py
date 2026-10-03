"""Read-only SQLite queries used by the map API.

Keep table and column assumptions in this module so importer schema changes are
localized. All externally supplied values are bound parameters.
"""

from __future__ import annotations

import base64
import binascii
import math
import sqlite3
import struct
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


NETWORK_POINT_LIMIT = 5_000
NETWORK_CLUSTER_LIMIT = 2_000
NETWORK_STACK_PAGE_LIMIT = 50
NETWORK_OBSERVATION_LIMIT = 1_000
ROUTE_LIMIT = 2_000
ROUTE_POINT_LIMIT = 200_000
WIFI_5_GHZ_CHANNELS = frozenset(
    (
        7, 8, 9, 11, 12, 16, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50,
        52, 54, 56, 58, 60, 62, 64, 68, 96, 100, 102, 104, 106, 108,
        110, 112, 114, 116, 118, 120, 122, 124, 126, 128, 132, 134,
        136, 138, 140, 142, 144, 149, 151, 153, 155, 157, 159, 161,
        163, 165, 167, 169, 171, 173, 175, 177, 182, 183, 184,
        187, 188, 189, 192, 196,
    )
)
WIFI_60_GHZ_FREQUENCIES = {
    58_320: 1,
    60_480: 2,
    62_640: 3,
    64_800: 4,
    66_960: 5,
    69_120: 6,
}


@dataclass(frozen=True)
class Bounds:
    west: float
    south: float
    east: float
    north: float


def connect(db_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def parse_bbox(raw: str) -> Bounds:
    try:
        values = [float(value.strip()) for value in raw.split(",")]
    except (TypeError, ValueError) as exc:
        raise ValueError("bbox must contain four numbers: west,south,east,north") from exc
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("bbox must contain four finite numbers")
    west, south, east, north = values
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox is outside valid longitude/latitude ranges")
    return Bounds(west, south, east, north)


def parse_date(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    return parsed.isoformat()


def _date_epoch(value: str, next_day: bool = False) -> int:
    parsed = date.fromisoformat(value)
    if next_day:
        parsed += timedelta(days=1)
    return int(datetime.combine(parsed, time.min, timezone.utc).timestamp())


def _in_clause(values: Iterable[str], params: list[Any]) -> str:
    cleaned = [value for value in values if value]
    params.extend(cleaned)
    return "(" + ",".join("?" for _ in cleaned) + ")"


def _network_nonspatial_filters(
    types: list[str],
    devices: list[str],
    from_date: str | None,
    to_date: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if types:
        clauses.append(f"n.network_type IN {_in_clause(types, params)}")
    if from_date:
        clauses.append("n.last_seen >= ?")
        params.append(_date_epoch(from_date))
    if to_date:
        clauses.append("n.first_seen < ?")
        params.append(_date_epoch(to_date, next_day=True))
    if devices:
        device_clause = _in_clause(devices, params)
        clauses.append(
            "EXISTS (SELECT 1 FROM observations o "
            "JOIN observation_sources os ON os.observation_id = o.id "
            "WHERE o.network_id = n.id AND os.source_device IN "
            f"{device_clause})"
        )
    return " AND ".join(clauses), params


def _network_filters(
    bounds: Bounds,
    types: list[str],
    devices: list[str],
    from_date: str | None,
    to_date: str | None,
) -> tuple[str, list[Any]]:
    clauses = [
        "n.best_longitude >= ?",
        "n.best_longitude <= ?",
        "n.best_latitude >= ?",
        "n.best_latitude <= ?",
    ]
    params: list[Any] = [bounds.west, bounds.east, bounds.south, bounds.north]
    extra_where, extra_params = _network_nonspatial_filters(
        types, devices, from_date, to_date
    )
    if extra_where:
        clauses.append(extra_where)
        params.extend(extra_params)
    return " AND ".join(clauses), params


def _chunks(values: list[int], size: int = 500) -> Iterable[list[int]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _network_device_provenance(
    db: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> dict[int, dict[str, list[str]]]:
    provenance: dict[int, dict[str, set[str]]] = {
        row["id"]: {
            "devices": set(),
            "first_seen_devices": set(),
            "position_devices": set(),
        }
        for row in rows
    }
    network_ids = list(provenance)
    for batch in _chunks(network_ids):
        placeholders = ",".join("?" for _ in batch)
        for row in db.execute(
            "SELECT ns.network_id, i.device_label "
            "FROM network_sources ns JOIN imports i ON i.id = ns.import_id "
            f"WHERE ns.network_id IN ({placeholders})",
            batch,
        ):
            provenance[row["network_id"]]["devices"].add(row["device_label"])
        for row in db.execute(
            "SELECT DISTINCT o.network_id, os.source_device "
            "FROM observations o "
            "JOIN networks n ON n.id = o.network_id "
            "JOIN observation_sources os ON os.observation_id = o.id "
            f"WHERE o.network_id IN ({placeholders}) "
            "AND o.observed_at = n.first_seen",
            batch,
        ):
            provenance[row["network_id"]]["first_seen_devices"].add(
                row["source_device"]
            )

    observation_to_network = {
        row["best_observation_id"]: row["id"]
        for row in rows
        if row["best_observation_id"] is not None
    }
    for batch in _chunks(list(observation_to_network)):
        placeholders = ",".join("?" for _ in batch)
        for row in db.execute(
            "SELECT observation_id, source_device FROM observation_sources "
            f"WHERE observation_id IN ({placeholders})",
            batch,
        ):
            network_id = observation_to_network[row["observation_id"]]
            provenance[network_id]["position_devices"].add(row["source_device"])

    return {
        network_id: {
            key: sorted(values)
            for key, values in fields.items()
        }
        for network_id, fields in provenance.items()
    }


def wifi_channel_from_frequency(frequency: int | None) -> str | None:
    if frequency is None:
        return None
    if frequency == 2484:
        return "14"
    if 2412 <= frequency <= 2472 and (frequency - 2412) % 5 == 0:
        return str((frequency - 2407) // 5)
    if 4910 <= frequency <= 4980 and (frequency - 4000) % 5 == 0:
        channel = (frequency - 4000) // 5
        return str(channel) if channel in WIFI_5_GHZ_CHANNELS else None
    if 5000 <= frequency < 5925 and (frequency - 5000) % 5 == 0:
        channel = (frequency - 5000) // 5
        return str(channel) if channel in WIFI_5_GHZ_CHANNELS else None
    if frequency == 5935:
        return "2"
    if 5955 <= frequency <= 7115 and (frequency - 5955) % 20 == 0:
        return str((frequency - 5950) // 5)
    channel = WIFI_60_GHZ_FREQUENCIES.get(frequency)
    return str(channel) if channel is not None else None


def _network_radio_properties(row: sqlite3.Row) -> dict[str, Any]:
    capabilities = row["capabilities"] or row["encryption"] or None
    if row["network_type"] != "WIFI":
        return {
            "encryption": None,
            "attributes": capabilities,
            "frequency": None,
            "channel": None,
        }

    frequency = row["frequency"]
    derived_channel = wifi_channel_from_frequency(frequency)
    stored_channel = str(row["channel"] or "").strip()
    if stored_channel == "0":
        stored_channel = ""
    valid_frequency = frequency if derived_channel is not None else None
    return {
        "encryption": row["encryption"] or row["capabilities"] or None,
        "attributes": None,
        "frequency": valid_frequency,
        "channel": stored_channel or derived_channel,
    }


def _stack_id(latitude: float, longitude: float) -> str:
    encoded = base64.urlsafe_b64encode(
        struct.pack("!dd", latitude, longitude)
    ).decode("ascii")
    return encoded.rstrip("=")


def parse_stack_id(value: str) -> tuple[float, float]:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(value + padding)
        if len(raw) != 16:
            raise ValueError
        latitude, longitude = struct.unpack("!dd", raw)
    except (ValueError, binascii.Error, struct.error) as exc:
        raise ValueError("invalid stack identifier") from exc
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        raise ValueError("invalid stack identifier")
    return latitude, longitude


def _network_detail_rows(
    db: sqlite3.Connection,
    network_ids: list[int],
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for batch in _chunks(network_ids):
        placeholders = ",".join("?" for _ in batch)
        rows.extend(
            db.execute(
                "SELECT n.id, n.network_type, n.name, n.identifier, "
                "n.first_seen, n.last_seen, n.best_latitude, n.best_longitude, "
                "n.best_signal, n.observation_count, n.encryption, "
                "n.capabilities, n.frequency, n.channel, "
                "best.id AS best_observation_id, best.accuracy AS best_accuracy "
                "FROM networks n "
                "LEFT JOIN observations best ON best.id = ("
                "SELECT o.id FROM observations o WHERE o.network_id = n.id "
                "ORDER BY o.signal IS NULL, o.signal DESC, "
                "o.accuracy IS NULL, o.accuracy ASC, "
                "o.observed_at, o.latitude, o.longitude, "
                "o.observation_hash LIMIT 1"
                ") "
                f"WHERE n.id IN ({placeholders})",
                batch,
            ).fetchall()
        )
    by_id = {row["id"]: row for row in rows}
    return [by_id[network_id] for network_id in network_ids if network_id in by_id]


def _network_feature(
    row: sqlite3.Row,
    provenance: dict[int, dict[str, list[str]]],
    include_identifiers: bool,
) -> dict[str, Any]:
    properties = {
        "id": row["id"],
        "type": row["network_type"],
        "first_seen": _iso_timestamp(row["first_seen"]),
        "last_seen": _iso_timestamp(row["last_seen"]),
        "best_signal": row["best_signal"],
        "observation_count": row["observation_count"],
        "accuracy": row["best_accuracy"],
        **_network_radio_properties(row),
        **provenance[row["id"]],
    }
    if include_identifiers:
        if row["name"]:
            properties["name"] = row["name"]
        properties["identifier"] = row["identifier"]
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [row["best_longitude"], row["best_latitude"]],
        },
        "properties": properties,
    }


def summary(db_path: Path | str, import_status: Any = None) -> dict[str, Any]:
    with connect(db_path) as db:
        network_total = db.execute("SELECT COUNT(*) FROM networks").fetchone()[0]
        observation_total = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        by_type = {
            row["network_type"] or "unknown": row["count"]
            for row in db.execute(
                "SELECT network_type, COUNT(*) AS count FROM networks "
                "GROUP BY network_type ORDER BY count DESC"
            )
        }
        by_network_year = {
            row["year"]: row["count"]
            for row in db.execute(
                "SELECT strftime('%Y', first_seen, 'unixepoch') AS year, "
                "COUNT(*) AS count "
                "FROM networks WHERE first_seen IS NOT NULL "
                "GROUP BY year ORDER BY year"
            )
            if row["year"]
        }
        by_device = {
            row["source_device"] or "unknown": row["count"]
            for row in db.execute(
                "SELECT source_device, COUNT(DISTINCT observation_id) AS count "
                "FROM observation_sources "
                "GROUP BY source_device ORDER BY count DESC"
            )
        }
        devices = {
            row["source_device"] or "unknown": {
                "networks": row["network_count"],
                "observations": row["observation_count"],
            }
            for row in db.execute(
                "SELECT os.source_device, "
                "COUNT(DISTINCT o.network_id) AS network_count, "
                "COUNT(DISTINCT os.observation_id) AS observation_count "
                "FROM observation_sources os "
                "JOIN observations o ON o.id = os.observation_id "
                "GROUP BY os.source_device ORDER BY observation_count DESC"
            )
        }
        by_year = {
            row["year"]: row["count"]
            for row in db.execute(
                "SELECT strftime('%Y', observed_at, 'unixepoch') AS year, "
                "COUNT(*) AS count FROM observations "
                "GROUP BY year ORDER BY year"
            )
            if row["year"]
        }
        bounds_row = db.execute(
            "SELECT MIN(best_latitude) AS south, MIN(best_longitude) AS west, "
            "MAX(best_latitude) AS north, MAX(best_longitude) AS east FROM networks"
        ).fetchone()
        coverage_row = db.execute(
            """
            SELECT MIN(observed_at) AS first_seen, MAX(observed_at) AS last_seen
            FROM (
                SELECT observed_at FROM observations
                UNION ALL
                SELECT observed_at FROM route_points
            )
            """
        ).fetchone()
        route_total = db.execute("SELECT COUNT(*) FROM route_segments").fetchone()[0]
        route_point_total = db.execute("SELECT COUNT(*) FROM route_points").fetchone()[0]
        route_by_device = {
            row["device_label"] or "unknown": row["count"]
            for row in db.execute(
                "SELECT device_label, COUNT(*) AS count FROM route_segments "
                "GROUP BY device_label ORDER BY count DESC"
            )
        }
        import_rows = db.execute(
            "SELECT status, COUNT(*) AS count FROM imports GROUP BY status"
        ).fetchall()

    bounds = None
    if bounds_row["south"] is not None:
        bounds = dict(bounds_row)
    imports = {
        "by_status": {row["status"] or "unknown": row["count"] for row in import_rows},
        "total": sum(row["count"] for row in import_rows),
    }
    if import_status is not None:
        imports["runtime"] = import_status
    return {
        "networks": {
            "total": network_total,
            "by_type": by_type,
            "by_year": by_network_year,
        },
        "devices": devices,
        "observations": {
            "total": observation_total,
            "by_device": by_device,
            "by_year": by_year,
        },
        "bounds": bounds,
        "coverage": {
            "first": _iso_timestamp(coverage_row["first_seen"]),
            "last": _iso_timestamp(coverage_row["last_seen"]),
        },
        "routes": {
            "segments": route_total,
            "points": route_point_total,
            "by_device": route_by_device,
        },
        "imports": imports,
    }


def networks(
    db_path: Path | str,
    bounds: Bounds,
    zoom: int,
    types: list[str],
    devices: list[str],
    from_date: str | None,
    to_date: str | None,
    include_identifiers: bool = False,
) -> dict[str, Any]:
    where, params = _network_filters(bounds, types, devices, from_date, to_date)
    if zoom >= 15:
        sql = (
            "SELECT MIN(n.id) AS network_id, n.best_latitude, n.best_longitude, "
            "COUNT(*) AS count, MAX(n.last_seen) AS latest, "
            "SUM(n.network_type = 'WIFI') AS wifi_count, "
            "SUM(n.network_type = 'BLE') AS ble_count, "
            "SUM(n.network_type = 'BLUETOOTH') AS bluetooth_count, "
            "SUM(n.network_type IN ('GSM','LTE','CDMA','WCDMA','NR')) "
            "AS cellular_count "
            f"FROM networks n WHERE {where} "
            "GROUP BY n.best_latitude, n.best_longitude "
            "ORDER BY latest DESC LIMIT ?"
        )
        with connect(db_path) as db:
            positions = db.execute(
                sql, [*params, NETWORK_POINT_LIMIT + 1]
            ).fetchall()
            returned_positions = positions[:NETWORK_POINT_LIMIT]
            singleton_ids = [
                row["network_id"] for row in returned_positions if row["count"] == 1
            ]
            singleton_rows = _network_detail_rows(db, singleton_ids)
            provenance = _network_device_provenance(db, singleton_rows)
        singleton_features = {
            row["id"]: _network_feature(row, provenance, include_identifiers)
            for row in singleton_rows
        }
        features = []
        for row in returned_positions:
            if row["count"] == 1:
                feature = singleton_features.get(row["network_id"])
                if feature:
                    features.append(feature)
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [
                            row["best_longitude"],
                            row["best_latitude"],
                        ],
                    },
                    "properties": {
                        "stack": True,
                        "stack_id": _stack_id(
                            row["best_latitude"], row["best_longitude"]
                        ),
                        "count": row["count"],
                        "type_counts": {
                            "WIFI": row["wifi_count"],
                            "BLE": row["ble_count"],
                            "BLUETOOTH": row["bluetooth_count"],
                            "CELLULAR": row["cellular_count"],
                        },
                    },
                }
            )
        return {
            "type": "FeatureCollection",
            "mode": "points",
            "zoom": zoom,
            "truncated": len(positions) > NETWORK_POINT_LIMIT,
            "features": features,
        }

    cell_size = max(0.0025, 360.0 / (2 ** (zoom + 7)))
    sql = (
        "SELECT CAST((n.best_longitude + 180.0) / ? AS INTEGER) AS grid_x, "
        "CAST((n.best_latitude + 90.0) / ? AS INTEGER) AS grid_y, "
        "COUNT(*) AS count, AVG(n.best_longitude) AS longitude, "
        "AVG(n.best_latitude) AS latitude "
        f"FROM networks n WHERE {where} GROUP BY grid_x, grid_y "
        "ORDER BY count DESC LIMIT ?"
    )
    with connect(db_path) as db:
        rows = db.execute(
            sql, [cell_size, cell_size, *params, NETWORK_CLUSTER_LIMIT + 1]
        ).fetchall()
    truncated = len(rows) > NETWORK_CLUSTER_LIMIT
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["longitude"], row["latitude"]],
            },
            "properties": {"cluster": True, "count": row["count"]},
        }
        for row in rows[:NETWORK_CLUSTER_LIMIT]
    ]
    return {
        "type": "FeatureCollection",
        "mode": "clusters",
        "zoom": zoom,
        "cell_size": cell_size,
        "truncated": truncated,
        "features": features,
    }


def network_stack(
    db_path: Path | str,
    stack_id: str,
    page: int,
    per_page: int,
    types: list[str],
    devices: list[str],
    from_date: str | None,
    to_date: str | None,
    include_identifiers: bool = False,
) -> dict[str, Any]:
    latitude, longitude = parse_stack_id(stack_id)
    extra_where, params = _network_nonspatial_filters(
        types, devices, from_date, to_date
    )
    clauses = ["n.best_latitude = ?", "n.best_longitude = ?"]
    query_params: list[Any] = [latitude, longitude]
    if extra_where:
        clauses.append(extra_where)
        query_params.extend(params)
    where = " AND ".join(clauses)
    offset = (page - 1) * per_page
    with connect(db_path) as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM networks n WHERE {where}",
            query_params,
        ).fetchone()[0]
        ids = [
            row["id"]
            for row in db.execute(
                f"SELECT n.id FROM networks n WHERE {where} "
                "ORDER BY n.last_seen DESC, n.id LIMIT ? OFFSET ?",
                [*query_params, per_page, offset],
            )
        ]
        rows = _network_detail_rows(db, ids)
        provenance = _network_device_provenance(db, rows)
    return {
        "stack_id": stack_id,
        "total": total,
        "page": page,
        "per_page": per_page,
        "has_more": offset + len(rows) < total,
        "features": [
            _network_feature(row, provenance, include_identifiers)
            for row in rows
        ],
    }


def network_observations(
    db_path: Path | str,
    network_id: int,
    devices: list[str],
    from_date: str | None,
    to_date: str | None,
    limit: int = NETWORK_OBSERVATION_LIMIT,
) -> dict[str, Any] | None:
    clauses = ["o.network_id = ?"]
    params: list[Any] = [network_id]
    if from_date:
        clauses.append("o.observed_at >= ?")
        params.append(_date_epoch(from_date))
    if to_date:
        clauses.append("o.observed_at < ?")
        params.append(_date_epoch(to_date, next_day=True))
    if devices:
        device_clause = _in_clause(devices, params)
        clauses.append(
            "EXISTS (SELECT 1 FROM observation_sources filtered "
            "WHERE filtered.observation_id = o.id "
            f"AND filtered.source_device IN {device_clause})"
        )
    where = " AND ".join(clauses)
    with connect(db_path) as db:
        network = db.execute(
            "SELECT 1 FROM networks WHERE id = ?",
            (network_id,),
        ).fetchone()
        if network is None:
            return None
        total = db.execute(
            f"SELECT COUNT(*) FROM observations o WHERE {where}",
            params,
        ).fetchone()[0]
        rows = db.execute(
            f"""
            SELECT id, observed_at, latitude, longitude, signal, accuracy
            FROM observations o
            WHERE {where}
            ORDER BY observed_at, id
            LIMIT ?
            """,
            [*params, limit + 1],
        ).fetchall()
        returned_rows = rows[:limit]
        source_devices: dict[int, set[str]] = {
            row["id"]: set() for row in returned_rows
        }
        for batch in _chunks(list(source_devices)):
            placeholders = ",".join("?" for _ in batch)
            for source in db.execute(
                "SELECT observation_id, source_device "
                "FROM observation_sources "
                f"WHERE observation_id IN ({placeholders})",
                batch,
            ):
                source_devices[source["observation_id"]].add(
                    source["source_device"]
                )

    locations: dict[tuple[float, float], dict[str, Any]] = {}
    for row in returned_rows:
        key = (row["latitude"], row["longitude"])
        location = locations.setdefault(
            key,
            {
                "count": 0,
                "first_seen": row["observed_at"],
                "last_seen": row["observed_at"],
                "best_signal": row["signal"],
                "accuracy": row["accuracy"],
                "devices": set(),
            },
        )
        location["count"] += 1
        location["first_seen"] = min(location["first_seen"], row["observed_at"])
        location["last_seen"] = max(location["last_seen"], row["observed_at"])
        if row["signal"] is not None and (
            location["best_signal"] is None
            or row["signal"] > location["best_signal"]
        ):
            location["best_signal"] = row["signal"]
        if row["accuracy"] is not None and (
            location["accuracy"] is None
            or row["accuracy"] < location["accuracy"]
        ):
            location["accuracy"] = row["accuracy"]
        location["devices"].update(source_devices[row["id"]])

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [longitude, latitude],
            },
            "properties": {
                "observation_count": values["count"],
                "first_seen": _iso_timestamp(values["first_seen"]),
                "last_seen": _iso_timestamp(values["last_seen"]),
                "best_signal": values["best_signal"],
                "accuracy": values["accuracy"],
                "devices": sorted(values["devices"]),
            },
        }
        for (latitude, longitude), values in locations.items()
    ]
    return {
        "type": "FeatureCollection",
        "network_id": network_id,
        "total_observations": total,
        "returned_observations": len(returned_rows),
        "distinct_locations": len(features),
        "truncated": len(rows) > limit,
        "features": features,
    }


def routes(
    db_path: Path | str,
    bounds: Bounds,
    devices: list[str],
    from_date: str | None,
    to_date: str | None,
) -> dict[str, Any]:
    clauses = [
        "EXISTS (SELECT 1 FROM route_points rp WHERE rp.segment_id = s.id "
        "AND rp.longitude >= ? AND rp.longitude <= ? "
        "AND rp.latitude >= ? AND rp.latitude <= ?)"
    ]
    params: list[Any] = [bounds.west, bounds.east, bounds.south, bounds.north]
    if devices:
        clauses.append(f"s.device_label IN {_in_clause(devices, params)}")
    if from_date:
        clauses.append("s.ended_at >= ?")
        params.append(_date_epoch(from_date))
    if to_date:
        clauses.append("s.started_at < ?")
        params.append(_date_epoch(to_date, next_day=True))
    segment_sql = (
        "SELECT s.id, s.device_label, s.started_at, s.ended_at, s.point_count "
        "FROM route_segments s WHERE "
        + " AND ".join(clauses)
        + " ORDER BY s.started_at DESC LIMIT ?"
    )
    with connect(db_path) as db:
        segments = db.execute(
            segment_sql, [*params, ROUTE_LIMIT + 1]
        ).fetchall()
        selected = segments[:ROUTE_LIMIT]
        if not selected:
            point_rows = []
        else:
            point_params: list[Any] = [row["id"] for row in selected]
            placeholders = ",".join("?" for _ in point_params)
            point_rows = db.execute(
                "SELECT segment_id, longitude, latitude FROM route_points "
                f"WHERE segment_id IN ({placeholders}) "
                "ORDER BY segment_id, sequence_number LIMIT ?",
                [*point_params, ROUTE_POINT_LIMIT + 1],
            ).fetchall()
    coordinates: dict[int, list[list[float]]] = {
        row["id"]: [] for row in selected
    }
    for point in point_rows[:ROUTE_POINT_LIMIT]:
        coordinates[point["segment_id"]].append(
            [point["longitude"], point["latitude"]]
        )
    features = []
    for row in selected:
        points = coordinates[row["id"]]
        if len(points) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": points},
                "properties": {
                    "id": row["id"],
                    "device": row["device_label"],
                    "start_time": _iso_timestamp(row["started_at"]),
                    "end_time": _iso_timestamp(row["ended_at"]),
                    "point_count": row["point_count"],
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "truncated": (
            len(segments) > ROUTE_LIMIT or len(point_rows) > ROUTE_POINT_LIMIT
        ),
        "features": features,
    }


def imports(db_path: Path | str) -> list[dict[str, Any]]:
    with connect(db_path) as db:
        rows = db.execute(
            "SELECT id, source_path, device_label, format, status, network_count, "
            "observation_count, route_point_count, imported_at, error "
            "FROM imports ORDER BY id DESC LIMIT 1000"
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        source = str(item.pop("source_path") or "")
        item["has_error"] = bool(item.pop("error"))
        item["source_name"] = Path(source.replace("\\", "/")).name
        results.append(item)
    return results


def _iso_timestamp(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()

"""Read-only SQLite queries used by the map API.

Keep table and column assumptions in this module so importer schema changes are
localized. All externally supplied values are bound parameters.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


NETWORK_POINT_LIMIT = 5_000
NETWORK_CLUSTER_LIMIT = 2_000
ROUTE_LIMIT = 2_000
ROUTE_POINT_LIMIT = 200_000


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
) -> dict[str, Any]:
    where, params = _network_filters(bounds, types, devices, from_date, to_date)
    if zoom >= 15:
        sql = (
            "SELECT n.id, n.network_type, n.first_seen, n.last_seen, "
            "n.best_latitude, n.best_longitude, n.best_signal, n.observation_count "
            f"FROM networks n WHERE {where} "
            "ORDER BY n.last_seen DESC LIMIT ?"
        )
        with connect(db_path) as db:
            rows = db.execute(sql, [*params, NETWORK_POINT_LIMIT + 1]).fetchall()
        truncated = len(rows) > NETWORK_POINT_LIMIT
        features = [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [row["best_longitude"], row["best_latitude"]],
                },
                "properties": {
                    "id": row["id"],
                    "type": row["network_type"],
                    "first_seen": _iso_timestamp(row["first_seen"]),
                    "last_seen": _iso_timestamp(row["last_seen"]),
                    "best_signal": row["best_signal"],
                    "observation_count": row["observation_count"],
                },
            }
            for row in rows[:NETWORK_POINT_LIMIT]
        ]
        return {
            "type": "FeatureCollection",
            "mode": "points",
            "zoom": zoom,
            "truncated": truncated,
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

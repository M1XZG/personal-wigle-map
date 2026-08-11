"""SQLite schema and route maintenance for WiGLE imports."""

from __future__ import annotations

import math
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
DEFAULT_ROUTE_GAP_SECONDS = 300
MAX_ROUTE_SPEED_MPS = 120.0


def connect_database(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 60000")
    return connection


def observation_fingerprint(
    network_type: str,
    identifier: str,
    observed_at: int,
    latitude: float,
    longitude: float,
) -> str:
    canonical = "|".join(
        (
            network_type,
            identifier,
            str(observed_at),
            f"{latitude:.6f}",
            f"{longitude:.6f}",
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _migrate_observation_fingerprints(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT o.id, o.observed_at, o.latitude, o.longitude, o.signal,
               o.accuracy, o.altitude, o.external, n.network_type, n.identifier
        FROM observations o
        JOIN networks n ON n.id = o.network_id
        ORDER BY o.id
        """
    ).fetchall()
    for row in rows:
        fingerprint = observation_fingerprint(
            row["network_type"],
            row["identifier"],
            row["observed_at"],
            row["latitude"],
            row["longitude"],
        )
        keeper = connection.execute(
            "SELECT id FROM observations WHERE observation_hash = ?",
            (fingerprint,),
        ).fetchone()
        if keeper is None or keeper["id"] == row["id"]:
            connection.execute(
                "UPDATE observations SET observation_hash = ? WHERE id = ?",
                (fingerprint, row["id"]),
            )
            continue

        keeper_id = keeper["id"]
        connection.execute(
            """
            INSERT OR IGNORE INTO observation_sources(
                observation_id, import_id, source_device, source_file
            )
            SELECT ?, import_id, source_device, source_file
            FROM observation_sources WHERE observation_id = ?
            """,
            (keeper_id, row["id"]),
        )
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
                external = MAX(external, ?)
            WHERE id = ?
            """,
            (
                row["signal"],
                row["signal"],
                row["signal"],
                row["accuracy"],
                row["accuracy"],
                row["accuracy"],
                row["altitude"],
                row["altitude"],
                row["altitude"],
                row["external"],
                keeper_id,
            ),
        )
        connection.execute("DELETE FROM observations WHERE id = ?", (row["id"],))

    connection.execute(
        """
        UPDATE observations
        SET source_device = (
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
        WHERE EXISTS (
            SELECT 1 FROM observation_sources os
            WHERE os.observation_id = observations.id
        )
        """
    )


def initialize_database(db_path: str | Path) -> None:
    """Create or migrate the persistent map database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                source_path TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                device_label TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                format TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'complete', 'failed')),
                network_count INTEGER NOT NULL DEFAULT 0,
                observation_count INTEGER NOT NULL DEFAULT 0,
                route_point_count INTEGER NOT NULL DEFAULT 0,
                imported_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS networks (
                id INTEGER PRIMARY KEY,
                network_type TEXT NOT NULL,
                identifier TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                first_seen INTEGER,
                last_seen INTEGER,
                best_latitude REAL,
                best_longitude REAL,
                best_signal REAL,
                encryption TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '',
                frequency INTEGER,
                channel TEXT NOT NULL DEFAULT '',
                observation_count INTEGER NOT NULL DEFAULT 0,
                source_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(network_type, identifier)
            );

            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY,
                observation_hash TEXT NOT NULL UNIQUE,
                network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
                observed_at INTEGER NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                signal REAL,
                accuracy REAL,
                altitude REAL,
                external INTEGER NOT NULL DEFAULT 0,
                source_device TEXT NOT NULL,
                source_file TEXT NOT NULL,
                import_id INTEGER NOT NULL REFERENCES imports(id),
                CHECK(latitude BETWEEN -90 AND 90),
                CHECK(longitude BETWEEN -180 AND 180)
            );

            CREATE TABLE IF NOT EXISTS observation_sources (
                observation_id INTEGER NOT NULL
                    REFERENCES observations(id) ON DELETE CASCADE,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                source_device TEXT NOT NULL,
                source_file TEXT NOT NULL,
                PRIMARY KEY(observation_id, import_id)
            );

            CREATE TABLE IF NOT EXISTS network_sources (
                network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                PRIMARY KEY(network_id, import_id)
            );

            CREATE TABLE IF NOT EXISTS route_candidates (
                id INTEGER PRIMARY KEY,
                device_label TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                altitude REAL,
                accuracy REAL,
                external INTEGER NOT NULL DEFAULT 0,
                explicit_run_id TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL,
                import_id INTEGER NOT NULL REFERENCES imports(id),
                genuine_route INTEGER NOT NULL DEFAULT 0,
                UNIQUE(device_label, observed_at, latitude, longitude)
            );

            CREATE TABLE IF NOT EXISTS route_segments (
                id INTEGER PRIMARY KEY,
                device_label TEXT NOT NULL,
                segment_number INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER NOT NULL,
                point_count INTEGER NOT NULL,
                UNIQUE(device_label, segment_number)
            );

            CREATE TABLE IF NOT EXISTS route_points (
                id INTEGER PRIMARY KEY,
                segment_id INTEGER NOT NULL REFERENCES route_segments(id) ON DELETE CASCADE,
                sequence_number INTEGER NOT NULL,
                observed_at INTEGER NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                altitude REAL,
                accuracy REAL,
                external INTEGER NOT NULL DEFAULT 0,
                source_file TEXT NOT NULL,
                genuine_route INTEGER NOT NULL DEFAULT 0,
                UNIQUE(segment_id, sequence_number)
            );

            CREATE INDEX IF NOT EXISTS idx_imports_status ON imports(status);
            CREATE INDEX IF NOT EXISTS idx_networks_type_seen
                ON networks(network_type, last_seen);
            CREATE INDEX IF NOT EXISTS idx_networks_location
                ON networks(best_latitude, best_longitude);
            CREATE INDEX IF NOT EXISTS idx_observations_network_time
                ON observations(network_id, observed_at);
            CREATE INDEX IF NOT EXISTS idx_observations_device_time
                ON observations(source_device, observed_at);
            CREATE INDEX IF NOT EXISTS idx_observations_location
                ON observations(latitude, longitude);
            CREATE INDEX IF NOT EXISTS idx_observation_sources_device
                ON observation_sources(source_device, observation_id);
            CREATE INDEX IF NOT EXISTS idx_observation_sources_import
                ON observation_sources(import_id);
            CREATE INDEX IF NOT EXISTS idx_route_candidates_device_time
                ON route_candidates(device_label, observed_at);
            CREATE INDEX IF NOT EXISTS idx_route_points_segment
                ON route_points(segment_id, sequence_number);
            """
        )
        previous_version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        if previous_version == 1:
            connection.execute(
                """
                INSERT OR IGNORE INTO observation_sources(
                    observation_id, import_id, source_device, source_file
                )
                SELECT id, import_id, source_device, source_file FROM observations
                """
            )
            _migrate_observation_fingerprints(connection)
            refresh_network_aggregates(connection)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )


def refresh_network_aggregates(
    connection: sqlite3.Connection, network_ids: set[int] | None = None
) -> None:
    """Recalculate aggregate fields from deduplicated observations."""
    if network_ids is None:
        network_ids = {row[0] for row in connection.execute("SELECT id FROM networks")}
    if not network_ids:
        return
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS affected_networks(id INTEGER PRIMARY KEY)"
    )
    connection.execute("DELETE FROM affected_networks")
    connection.executemany(
        "INSERT INTO affected_networks(id) VALUES (?)",
        ((network_id,) for network_id in network_ids),
    )
    aggregates = {
        row["network_id"]: row
        for row in connection.execute(
            """
            SELECT o.network_id, MIN(o.observed_at) AS first_seen,
                   MAX(o.observed_at) AS last_seen, COUNT(*) AS observation_count
            FROM observations o
            JOIN affected_networks a ON a.id = o.network_id
            GROUP BY o.network_id
            """
        )
    }
    best_rows = {
        row["network_id"]: row
        for row in connection.execute(
            """
            SELECT network_id, latitude, longitude, signal
            FROM (
                SELECT o.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY o.network_id
                           ORDER BY o.signal IS NULL, o.signal DESC,
                                    o.accuracy IS NULL, o.accuracy ASC,
                                    o.observed_at, o.latitude, o.longitude,
                                    o.observation_hash
                       ) AS rank
                FROM observations o
                JOIN affected_networks a ON a.id = o.network_id
            )
            WHERE rank = 1
            """
        )
    }
    source_counts = {
        row["network_id"]: row["source_count"]
        for row in connection.execute(
            """
            SELECT ns.network_id, COUNT(*) AS source_count
            FROM network_sources ns
            JOIN affected_networks a ON a.id = ns.network_id
            GROUP BY ns.network_id
            """
        )
    }
    connection.executemany(
        """
        UPDATE networks
        SET first_seen = ?, last_seen = ?, observation_count = ?,
            source_count = ?, best_latitude = ?, best_longitude = ?,
            best_signal = ?
        WHERE id = ?
        """,
        (
            (
                aggregates[network_id]["first_seen"],
                aggregates[network_id]["last_seen"],
                aggregates[network_id]["observation_count"],
                source_counts.get(network_id, 0),
                best_rows[network_id]["latitude"],
                best_rows[network_id]["longitude"],
                best_rows[network_id]["signal"],
                network_id,
            )
            for network_id in network_ids
            if network_id in aggregates
        ),
    )


def _distance_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _needs_new_segment(previous: sqlite3.Row, current: sqlite3.Row, gap: int) -> bool:
    elapsed = current["observed_at"] - previous["observed_at"]
    if elapsed <= 0 or elapsed > gap:
        return True
    previous_day = datetime.fromtimestamp(
        previous["observed_at"], timezone.utc
    ).date()
    current_day = datetime.fromtimestamp(current["observed_at"], timezone.utc).date()
    if current_day != previous_day:
        return True
    previous_run = previous["explicit_run_id"]
    current_run = current["explicit_run_id"]
    if (previous_run or current_run) and previous_run != current_run:
        return True
    distance = _distance_metres(
        previous["latitude"],
        previous["longitude"],
        current["latitude"],
        current["longitude"],
    )
    return distance / elapsed > MAX_ROUTE_SPEED_MPS


def rebuild_routes(
    db_path: str | Path, gap_seconds: int = DEFAULT_ROUTE_GAP_SECONDS
) -> dict[str, int]:
    """Rebuild route geometry from collapsed location candidates."""
    initialize_database(db_path)
    segment_count = 0
    point_count = 0
    with connect_database(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM route_points")
        connection.execute("DELETE FROM route_segments")
        devices = connection.execute(
            "SELECT DISTINCT device_label FROM route_candidates ORDER BY device_label"
        ).fetchall()
        for device_row in devices:
            device = device_row[0]
            candidates = connection.execute(
                """
                SELECT * FROM route_candidates
                WHERE device_label = ?
                ORDER BY observed_at, genuine_route DESC, id
                """,
                (device,),
            ).fetchall()
            groups: list[list[sqlite3.Row]] = []
            for candidate in candidates:
                if not groups or _needs_new_segment(
                    groups[-1][-1], candidate, gap_seconds
                ):
                    groups.append([])
                groups[-1].append(candidate)
            usable_groups = [points for points in groups if len(points) >= 2]
            for segment_number, points in enumerate(usable_groups, 1):
                cursor = connection.execute(
                    """
                    INSERT INTO route_segments(
                        device_label, segment_number, started_at, ended_at, point_count
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        device,
                        segment_number,
                        points[0]["observed_at"],
                        points[-1]["observed_at"],
                        len(points),
                    ),
                )
                segment_id = cursor.lastrowid
                connection.executemany(
                    """
                    INSERT INTO route_points(
                        segment_id, sequence_number, observed_at, latitude, longitude,
                        altitude, accuracy, external, source_file, genuine_route
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            segment_id,
                            sequence,
                            point["observed_at"],
                            point["latitude"],
                            point["longitude"],
                            point["altitude"],
                            point["accuracy"],
                            point["external"],
                            point["source_file"],
                            point["genuine_route"],
                        )
                        for sequence, point in enumerate(points)
                    ],
                )
                segment_count += 1
                point_count += len(points)
    return {"segments": segment_count, "route_points": point_count}


def get_import_status(db_path: str | Path) -> list[dict[str, object]]:
    """Return import records in newest-first order."""
    initialize_database(db_path)
    with connect_database(db_path) as connection:
        rows = connection.execute(
            """
            SELECT source_path, sha256, device_label, size_bytes, mtime_ns, format,
                   status, network_count, observation_count, route_point_count,
                   imported_at, error
            FROM imports ORDER BY id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]

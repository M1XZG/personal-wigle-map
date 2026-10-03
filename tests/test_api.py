from __future__ import annotations

import gzip
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.storage import initialize_database, observation_fingerprint


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "wigle-map.sqlite"
    initialize_database(path)
    with sqlite3.connect(path) as db:
        db.execute(
            """
            INSERT INTO imports(
                id, source_path, sha256, device_label, size_bytes, mtime_ns,
                format, status, network_count, observation_count,
                route_point_count, imported_at
            ) VALUES (1, ?, ?, 'phone', 100, 1, 'csv', 'complete', 2, 3, 2, ?)
            """,
            ("/private/archive/phone-export.csv", "a" * 64, "2024-01-02T00:00:00Z"),
        )
        networks = [
            (1, "WIFI", "AA:00:00:00:00:01", 1704067200, 1706745600, 51.5000, -0.1000, -40, 2, 1, "Coffee Shop", "[WPA2][ESS]", 2437, "6"),
            (2, "WIFI", "AA:00:00:00:00:02", 1735689600, 1738368000, 51.5003, -0.1003, -55, 1, 1, "Library WiFi", "[WPA3][ESS]", 5975, "5"),
            (3, "BLUETOOTH", "AA:00:00:00:00:03", 1740787200, 1740787200, 40.7, -74.0, -70, 1, 1, "Headphones", "", None, ""),
        ]
        db.executemany(
            """
            INSERT INTO networks(
                id, network_type, identifier, first_seen, last_seen,
                best_latitude, best_longitude, best_signal,
                observation_count, source_count, name, encryption, frequency, channel
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            networks,
        )
        observations = [
            (1, 1, 1704110400, 51.5, -0.1, "phone"),
            (2, 1, 1706788800, 51.5, -0.1, "phone"),
            (3, 2, 1738411200, 51.5003, -0.1003, "tablet"),
            (4, 3, 1740820800, 40.7, -74.0, "phone"),
        ]
        for observation_id, network_id, observed_at, lat, lon, device in observations:
            network = networks[network_id - 1]
            fingerprint = observation_fingerprint(
                network[1], network[2], observed_at, lat, lon
            )
            db.execute(
                """
                INSERT INTO observations(
                    id, observation_hash, network_id, observed_at, latitude,
                    longitude, accuracy, source_device, source_file, import_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    observation_id,
                    fingerprint,
                    network_id,
                    observed_at,
                    lat,
                    lon,
                    2.5 if network_id == 2 else 8.0,
                    device,
                    f"/private/{device}.csv",
                ),
            )
            db.execute(
                """
                INSERT INTO observation_sources(
                    observation_id, import_id, source_device, source_file
                ) VALUES (?, 1, ?, ?)
                """,
                (observation_id, device, f"/private/{device}.csv"),
            )
        db.executemany(
            """
            INSERT INTO route_segments(
                id, device_label, segment_number, started_at, ended_at, point_count
            ) VALUES (?, ?, 1, ?, ?, 2)
            """,
            [
                (1, "phone", 1704067200, 1704153600),
                (2, "tablet", 1735689600, 1735776000),
            ],
        )
        db.executemany(
            """
            INSERT INTO route_points(
                segment_id, sequence_number, observed_at, latitude, longitude,
                source_file
            ) VALUES (?, ?, ?, ?, ?, 'fixture')
            """,
            [
                (1, 0, 1704067200, 51.5, -0.1),
                (1, 1, 1704153600, 51.51, -0.09),
                (2, 0, 1735689600, 40.7, -74.0),
                (2, 1, 1735776000, 40.8, -73.9),
            ],
        )
    return path


def wait_for_job(client: TestClient) -> dict:
    for _ in range(500):
        body = client.get("/api/imports").json()
        if not body["job"]["queued"] and not body["job"]["running"]:
            return body
        time.sleep(0.01)
    raise AssertionError("import job did not finish")


@pytest.fixture()
def client(database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setenv("IMPORT_ROOT", str(tmp_path / "imports"))
    monkeypatch.setenv(
        "WEB_DIR", str(Path(__file__).resolve().parents[1] / "web")
    )
    monkeypatch.setenv("RESCAN_SECONDS", "0")
    import app.importer as importer
    import app.main as main

    monkeypatch.setattr(main, "ingestion", importer)
    with TestClient(main.create_app()) as test_client:
        wait_for_job(test_client)
        yield test_client


def test_health_summary_and_static_ui(client: TestClient):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["wigle_sync"] == {
        "enabled": False,
        "running": False,
        "last_started_at": None,
        "last_finished_at": None,
        "transactions": 0,
        "downloaded": 0,
        "existing": 0,
        "failed": 0,
    }

    summary = client.get("/api/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["networks"]["by_type"] == {"WIFI": 2, "BLUETOOTH": 1}
    assert body["observations"]["by_device"]["phone"] == 3
    assert body["routes"] == {
        "segments": 2,
        "points": 4,
        "by_device": {"phone": 1, "tablet": 1},
    }
    assert body["coverage"]["first"].startswith("2024-01-01")

    assert "Personal WiGLE Map" in client.get("/").text
    assert client.get("/app.js").headers["content-type"].startswith(
        "text/javascript"
    )
    assert client.get("/styles.css").status_code == 200


def test_public_config_defaults_to_no_badge(client: TestClient):
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json() == {
        "title": "Personal WiGLE Map",
        "eyebrow": "Wireless survey archive",
        "badge": {
            "image_url": "",
            "link_url": "https://wigle.net",
        },
        "wigle_sync": {
            "enabled": False,
            "interval_seconds": 86400,
            "on_start": True,
        },
    }


def test_public_config_supports_custom_branding(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setenv("IMPORT_ROOT", str(tmp_path / "imports"))
    monkeypatch.setenv(
        "WEB_DIR", str(Path(__file__).resolve().parents[1] / "web")
    )
    monkeypatch.setenv("RESCAN_SECONDS", "0")
    monkeypatch.setenv("APP_TITLE", "My Survey Map")
    monkeypatch.setenv("APP_EYEBROW", "Field archive")
    monkeypatch.setenv(
        "WIGLE_BADGE_URL", "https://wigle.net/bi/example+badge.png"
    )
    monkeypatch.setenv("WIGLE_PROFILE_URL", "https://wigle.net")

    import app.importer as importer
    import app.main as main

    monkeypatch.setattr(main, "ingestion", importer)
    with TestClient(main.create_app()) as configured_client:
        wait_for_job(configured_client)
        assert configured_client.get("/api/config").json() == {
            "title": "My Survey Map",
            "eyebrow": "Field archive",
            "badge": {
                "image_url": "https://wigle.net/bi/example+badge.png",
                "link_url": "https://wigle.net",
            },
            "wigle_sync": {
                "enabled": False,
                "interval_seconds": 86400,
                "on_start": True,
            },
        }


def test_public_config_rejects_non_http_badge_url(
    monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("WIGLE_BADGE_URL", "javascript:alert(1)")
    import app.main as main

    with pytest.raises(RuntimeError, match="absolute HTTP or HTTPS URL"):
        main.create_app()


def test_wigle_sync_requires_both_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("WIGLE_API_NAME", "name-only")
    monkeypatch.delenv("WIGLE_API_TOKEN", raising=False)
    import app.main as main

    with pytest.raises(
        RuntimeError,
        match="WIGLE_API_NAME and WIGLE_API_TOKEN must both be set",
    ):
        main.create_app()


def test_wigle_sync_rejects_interval_below_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("WIGLE_SYNC_SECONDS", "60")
    import app.main as main

    with pytest.raises(
        RuntimeError,
        match="WIGLE_SYNC_SECONDS must be 0 or at least 300",
    ):
        main.create_app()


def test_wigle_sync_configuration_can_be_enabled_without_startup_run(
    database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setenv("IMPORT_ROOT", str(tmp_path / "imports"))
    monkeypatch.setenv(
        "WEB_DIR", str(Path(__file__).resolve().parents[1] / "web")
    )
    monkeypatch.setenv("RESCAN_SECONDS", "0")
    monkeypatch.setenv("WIGLE_API_NAME", "api-name")
    monkeypatch.setenv("WIGLE_API_TOKEN", "api-token")
    monkeypatch.setenv("WIGLE_SYNC_SECONDS", "3600")
    monkeypatch.setenv("WIGLE_SYNC_ON_START", "false")

    import app.importer as importer
    import app.main as main

    monkeypatch.setattr(main, "ingestion", importer)
    with TestClient(main.create_app()) as configured_client:
        wait_for_job(configured_client)
        assert configured_client.get("/api/config").json()["wigle_sync"] == {
            "enabled": True,
            "interval_seconds": 3600,
            "on_start": False,
        }


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?bbox=bad&zoom=10",
        "?bbox=-181,-10,10,10&zoom=10",
        "?bbox=10,-10,-10,10&zoom=10",
        "?bbox=-10,-10,10,10&zoom=99",
        "?bbox=-10,-10,10,10&zoom=10&from=2025-99-01",
    ],
)
def test_network_validation(client: TestClient, query: str):
    assert client.get("/api/networks" + query).status_code == 422


def test_network_and_route_contracts(client: TestClient):
    cluster = client.get("/api/networks?bbox=-1,50,1,52&zoom=8&types=WIFI")
    assert cluster.status_code == 200
    assert cluster.json()["mode"] == "clusters"
    assert sum(
        feature["properties"]["count"] for feature in cluster.json()["features"]
    ) == 2

    points = client.get(
        "/api/networks?bbox=-1,50,1,52&zoom=16"
        "&devices=tablet&from=2025-01-01"
    )
    body = points.json()
    assert points.status_code == 200
    assert body["mode"] == "points"
    assert len(body["features"]) == 1
    assert body["features"][0]["properties"]["type"] == "WIFI"
    assert body["features"][0]["properties"]["channel"] == "5"
    assert body["features"][0]["properties"]["frequency"] == 5975
    assert body["features"][0]["properties"]["encryption"] == "[WPA3][ESS]"
    assert body["features"][0]["properties"]["accuracy"] == 2.5
    assert "identifier" not in body["features"][0]["properties"]
    assert "name" not in body["features"][0]["properties"]

    routes = client.get(
        "/api/routes?bbox=-1,50,1,52&devices=phone&to=2024-12-31"
    )
    assert routes.status_code == 200
    assert routes.json()["features"][0]["properties"]["device"] == "phone"


def test_network_identifiers_can_be_enabled(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setenv("IMPORT_ROOT", str(tmp_path / "imports"))
    monkeypatch.setenv(
        "WEB_DIR", str(Path(__file__).resolve().parents[1] / "web")
    )
    monkeypatch.setenv("RESCAN_SECONDS", "0")
    monkeypatch.setenv("EXPOSE_NETWORK_IDENTIFIERS", "true")

    import app.importer as importer
    import app.main as main

    monkeypatch.setattr(main, "ingestion", importer)
    with TestClient(main.create_app()) as configured_client:
        wait_for_job(configured_client)
        response = configured_client.get(
            "/api/networks?bbox=-1,50,1,52&zoom=16&devices=tablet"
        )
        properties = response.json()["features"][0]["properties"]
        assert properties["name"] == "Library WiFi"
        assert properties["identifier"] == "AA:00:00:00:00:02"


def test_network_identifier_setting_rejects_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("EXPOSE_NETWORK_IDENTIFIERS", "sometimes")
    import app.main as main

    with pytest.raises(
        RuntimeError,
        match="EXPOSE_NETWORK_IDENTIFIERS must be true or false",
    ):
        main.create_app()


def test_import_paths_are_redacted(client: TestClient):
    body = client.get("/api/imports").json()
    assert body["imports"][0]["source_name"] == "phone-export.csv"
    assert "source_path" not in body["imports"][0]


def test_upload_gpx_and_rescan_csv(client: TestClient):
    gpx = b"""<?xml version="1.0"?>
    <gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
      <trkpt lat="51.5000" lon="-0.1000"><time>2026-08-11T10:00:00Z</time></trkpt>
      <trkpt lat="51.5010" lon="-0.1010"><time>2026-08-11T10:01:00Z</time></trkpt>
    </trkseg></trk></gpx>"""
    response = client.post(
        "/api/upload",
        data={"device": "test-phone"},
        files={"files": ("morning walk.gpx", gpx, "application/gpx+xml")},
    )
    assert response.status_code == 202
    assert response.json()["files"] == ["morning_walk.gpx"]
    wait_for_job(client)
    imports = client.get("/api/imports").json()["imports"]
    assert any(
        item["device_label"] == "test-phone"
        and item["format"] == "gpx"
        and item["status"] == "complete"
        for item in imports
    )

    csv_path = (
        client.app.state.import_root
        / "browser-phone"
        / "2026-08-11"
        / "sample.csv"
    )
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,"
        "CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,"
        "RCOIs,MfgrId,Type\n"
        "11:22:33:44:55:66,Test,[WPA2],2026-08-11 12:00:00,"
        "6,2437,-50,51.5,-1.7,10,3,,,WIFI\n"
    )
    assert client.post("/api/rescan").status_code == 202
    wait_for_job(client)
    summary = client.get("/api/summary").json()
    assert "browser-phone" in summary["devices"]

    before = summary["observations"]["total"]
    assert client.post("/api/rescan").status_code == 202
    wait_for_job(client)
    assert client.get("/api/summary").json()["observations"]["total"] == before


def test_upload_wigle_csv_gzip(client: TestClient):
    csv_content = (
        b"MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,"
        b"CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,"
        b"RCOIs,MfgrId,Type\n"
        b"11:22:33:44:55:66,Test,[WPA2],2026-08-15 23:10:09,"
        b"6,2437,-50,51.5,-1.7,10,3,,,WIFI\n"
    )
    compressed = gzip.compress(csv_content, mtime=0)
    assert b"\x00" in compressed[:4096]

    response = client.post(
        "/api/upload",
        data={"device": "test-phone"},
        files={
            "files": (
                "WigleWifi_20260815231009.csv.gz",
                compressed,
                "application/gzip",
            )
        },
    )

    assert response.status_code == 202
    assert response.json()["files"] == ["WigleWifi_20260815231009.csv.gz"]
    wait_for_job(client)
    imports = client.get("/api/imports").json()["imports"]
    assert any(
        item["source_name"] == "WigleWifi_20260815231009.csv.gz"
        and item["format"] == "csv.gz"
        and item["status"] == "complete"
        for item in imports
    )


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("../escape.csv", b"a,b\n", {400}),
        ("payload.exe", b"MZ", {415}),
        ("payload.csv", b"#!/bin/sh\n", {415}),
        ("empty.gpx", b"", {400}),
        ("fake.gpx", b"not xml", {415}),
    ],
)
def test_upload_rejects_unsafe_files(
    client: TestClient, filename: str, content: bytes, expected: set[int]
):
    response = client.post(
        "/api/upload",
        data={"device": "test-phone"},
        files={"files": (filename, content, "application/octet-stream")},
    )
    assert response.status_code in expected

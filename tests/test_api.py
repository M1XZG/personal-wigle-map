from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import sqlite3
import time
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.queries import wifi_channel_from_frequency
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
        db.execute(
            """
            INSERT INTO imports(
                id, source_path, sha256, device_label, size_bytes, mtime_ns,
                format, status, network_count, observation_count,
                route_point_count, imported_at
            ) VALUES (2, ?, ?, 'tablet', 100, 1, 'csv', 'complete', 2, 2, 2, ?)
            """,
            ("/private/archive/tablet-export.csv", "b" * 64, "2025-02-02T00:00:00Z"),
        )
        networks = [
            (1, "WIFI", "AA:00:00:00:00:01", 1704110400, 1706788800, 51.5000, -0.1000, -40, 2, 2, "Coffee Shop", "[WPA2][ESS]", 2437, ""),
            (2, "WIFI", "AA:00:00:00:00:02", 1738411200, 1738411200, 51.5003, -0.1003, -55, 1, 1, "Library WiFi", "[WPA3][ESS]", 5975, "5"),
            (3, "BLUETOOTH", "AA:00:00:00:00:03", 1740820800, 1740820800, 40.7, -74.0, -70, 1, 1, "Headphones", "Uncategorized;10", 7936, ""),
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
            (1, 1, 1704110400, 51.5, -0.1, -70, 8.0, "phone", 1),
            (2, 1, 1706788800, 51.5, -0.1, -40, 3.0, "tablet", 2),
            (3, 2, 1738411200, 51.5003, -0.1003, -55, 2.5, "tablet", 2),
            (4, 3, 1740820800, 40.7, -74.0, -70, 8.0, "phone", 1),
        ]
        for (
            observation_id,
            network_id,
            observed_at,
            lat,
            lon,
            signal,
            accuracy,
            device,
            import_id,
        ) in observations:
            network = networks[network_id - 1]
            fingerprint = observation_fingerprint(
                network[1], network[2], observed_at, lat, lon
            )
            db.execute(
                """
                INSERT INTO observations(
                    id, observation_hash, network_id, observed_at, latitude,
                    longitude, signal, accuracy, source_device, source_file, import_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    fingerprint,
                    network_id,
                    observed_at,
                    lat,
                    lon,
                    signal,
                    accuracy,
                    device,
                    f"/private/{device}.csv",
                    import_id,
                ),
            )
            db.execute(
                """
                INSERT INTO observation_sources(
                    observation_id, import_id, source_device, source_file
                ) VALUES (?, ?, ?, ?)
                """,
                (observation_id, import_id, device, f"/private/{device}.csv"),
            )
        db.executemany(
            "INSERT INTO network_sources(network_id, import_id) VALUES (?, ?)",
            [(1, 1), (1, 2), (2, 2), (3, 1)],
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
    monkeypatch.delenv("WHAT3WORDS_API_KEY", raising=False)
    monkeypatch.delenv("APP_VERSION", raising=False)
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
    assert body["observations"]["by_device"] == {"phone": 2, "tablet": 2}
    assert body["routes"] == {
        "segments": 2,
        "points": 4,
        "by_device": {"phone": 1, "tablet": 1},
    }
    assert body["coverage"]["first"].startswith("2024-01-01")

    assert "Personal WiGLE Map" in client.get("/").text
    assert 'id="app-version"' in client.get("/").text
    assert 'id="stack-drawer"' in client.get("/").text
    assert 'id="area-search-form"' in client.get("/").text
    app_js = client.get("/app.js")
    assert app_js.headers["content-type"].startswith(
        "text/javascript"
    )
    assert "maxNativeZoom: 19" in app_js.text
    assert "maxZoom: 22" in app_js.text
    assert "/api/network-stacks/" in app_js.text
    assert "/observations" in app_js.text
    assert client.get("/styles.css").status_code == 200


def test_public_config_defaults_to_no_badge(client: TestClient):
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json() == {
        "title": "Personal WiGLE Map",
        "eyebrow": "Wireless survey archive",
        "version": "development",
        "badge": {
            "image_url": "",
            "link_url": "https://wigle.net",
        },
        "wigle_sync": {
            "enabled": False,
            "interval_seconds": 86400,
            "on_start": True,
        },
        "what3words_enabled": False,
    }


def test_what3words_search_needs_configuration(client: TestClient):
    response = client.post("/api/locations/what3words", json={"words": "filled.count.soap"})
    assert response.status_code == 503
    assert response.json()["detail"] == "what3words search is not configured"


def test_what3words_search_keeps_key_on_server(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from app import main

    monkeypatch.setenv("WHAT3WORDS_API_KEY", "test-private-key")
    requests = []

    def lookup(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(b'{"coordinates":{"lat":51.520847,"lng":-0.195521}}')

    monkeypatch.setattr(main.urllib.request, "urlopen", lookup)
    configured = TestClient(main.create_app())
    config = configured.get("/api/config").json()
    assert config["what3words_enabled"] is True
    assert "test-private-key" not in str(config)
    response = configured.post(
        "/api/locations/what3words", json={"words": "///filled.count.soap"}
    )
    assert response.status_code == 200
    assert response.json() == {"latitude": 51.520847, "longitude": -0.195521}
    assert len(requests) == 1
    request, timeout = requests[0]
    assert "words=filled.count.soap" in request.full_url
    assert "test-private-key" not in request.full_url
    assert request.get_header("X-api-key") == "test-private-key"
    assert timeout == 8
    assert "test-private-key" not in response.text


@pytest.mark.parametrize("words", ["two.words", "a.b.c.d", "a b.c.d", "///a/b.c.d"])
def test_what3words_search_rejects_bad_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, words: str
):
    from app import main

    monkeypatch.setenv("WHAT3WORDS_API_KEY", "test-private-key")
    monkeypatch.setattr(
        main.urllib.request, "urlopen",
        lambda *args, **kwargs: pytest.fail("Invalid input called the provider"),
    )
    response = TestClient(main.create_app()).post(
        "/api/locations/what3words", json={"words": words}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("provider_error", "expected_status", "expected_detail"),
    [
        (400, 422, "Three-word address was not found"),
        (402, 503, "what3words quota exceeded; check your API plan"),
        (403, 503, "what3words API key was rejected"),
        (429, 503, "what3words rate limit reached; try again later"),
    ],
)
def test_what3words_search_reports_provider_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    provider_error: int, expected_status: int, expected_detail: str,
):
    from app import main

    monkeypatch.setenv("WHAT3WORDS_API_KEY", "test-private-key")

    def lookup(request, timeout):
        raise urllib.error.HTTPError(request.full_url, provider_error, "error", {}, None)

    monkeypatch.setattr(main.urllib.request, "urlopen", lookup)
    response = TestClient(main.create_app()).post(
        "/api/locations/what3words", json={"words": "filled.count.soap"}
    )
    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
    assert "test-private-key" not in response.text


@pytest.mark.parametrize("payload", [b"not json", b'{"coordinates":{}}', b'{"coordinates":{"lat":999,"lng":0}}'])
def test_what3words_search_rejects_invalid_provider_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: bytes,
):
    from app import main

    monkeypatch.setenv("WHAT3WORDS_API_KEY", "test-private-key")
    monkeypatch.setattr(
        main.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload)
    )
    response = TestClient(main.create_app()).post(
        "/api/locations/what3words", json={"words": "filled.count.soap"}
    )
    assert response.status_code == 502


@pytest.mark.parametrize("index_url", ["/", "/index.html"])
def test_web_assets_are_content_versioned(client: TestClient, index_url: str):
    page = client.get(index_url)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    for filename in ("styles.css", "marker-groups.js", "app.js"):
        response = client.get(f"/{filename}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        digest = hashlib.sha256(response.content).hexdigest()[:16]
        versioned_url = f"/{filename}?v={digest}"
        assert f'"{versioned_url}"' in page.text
        versioned = client.get(versioned_url)
        assert versioned.status_code == 200
        assert versioned.content == response.content
        assert versioned.headers["cache-control"] == "no-cache"
        head = client.head(versioned_url)
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-type"] == response.headers["content-type"]
    assert client.get("/index.html").text == client.get("/").text


@pytest.mark.parametrize("filename", ["styles.css", "marker-groups.js", "app.js"])
def test_asset_update_invalidates_cached_url_with_same_size_and_mtime(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
):
    from app import main

    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("index.html").write_text(
        '<link rel="stylesheet" href="/styles.css">'
        '<script src="/marker-groups.js"></script><script src="/app.js"></script>',
        encoding="utf-8",
    )
    web.joinpath("styles.css").write_text(".old-icon{color:red}", encoding="utf-8")
    web.joinpath("app.js").write_text("const version='old';", encoding="utf-8")
    web.joinpath("marker-groups.js").write_text("const group='old';", encoding="utf-8")
    monkeypatch.setenv("WEB_DIR", str(web))
    monkeypatch.setenv("APP_VERSION", "development")
    with TestClient(main.create_app()) as web_client:
        wait_for_job(web_client)
        old_page = web_client.get("/")
        pattern = rf'"/{re.escape(filename)}\?v=[a-f0-9]{{16}}"'
        old_match = re.search(pattern, old_page.text)
        assert old_match is not None
        old_url = old_match.group()[1:-1]
        cached = web_client.get(old_url)

        asset = web / filename
        stat = asset.stat()
        asset.write_bytes(asset.read_bytes().replace(b"old", b"new"))
        os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert asset.stat().st_size == stat.st_size
        assert asset.stat().st_mtime_ns == stat.st_mtime_ns
        new_page = web_client.get("/")
        new_match = re.search(pattern, new_page.text)
        assert new_match is not None
        new_url = new_match.group()[1:-1]
        assert new_url != old_url
        fresh = web_client.get(new_url)
        assert fresh.status_code == 200
        assert b"new" in fresh.content
        assert b"old" not in fresh.content
        # Legacy unversioned clients must not reuse stale stat-based validators.
        revalidated = web_client.get(
            f"/{filename}",
            headers={
                "If-None-Match": cached.headers["etag"],
                "If-Modified-Since": cached.headers["last-modified"],
            },
        )
        assert revalidated.status_code == 200
        assert revalidated.content == fresh.content
        assert revalidated.headers["cache-control"] == "no-cache"


def test_missing_web_asset_reports_failure(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app import main

    web = tmp_path / "web"
    web.mkdir()
    web.joinpath("index.html").write_text('<script src="/app.js"></script>')
    monkeypatch.setenv("WEB_DIR", str(web))
    with TestClient(main.create_app()) as web_client:
        wait_for_job(web_client)
        assert web_client.get("/").status_code == 503
        assert web_client.get("/index.html").status_code == 503
        assert web_client.get("/styles.css").status_code == 404
        assert web_client.get("/app.js").status_code == 404


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
    monkeypatch.setenv("APP_VERSION", "abc1234")
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
            "version": "abc1234",
            "badge": {
                "image_url": "https://wigle.net/bi/example+badge.png",
                "link_url": "https://wigle.net",
            },
            "wigle_sync": {
                "enabled": False,
                "interval_seconds": 86400,
                "on_start": True,
            },
            "what3words_enabled": False,
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


@pytest.mark.parametrize(
    ("frequency", "channel"),
    [
        (2412, "1"),
        (2437, "6"),
        (2484, "14"),
        (5180, "36"),
        (5975, "5"),
        (5935, "2"),
        (58320, "1"),
        (7936, None),
        (None, None),
    ],
)
def test_wifi_channel_from_frequency(frequency: int | None, channel: str | None):
    assert wifi_channel_from_frequency(frequency) == channel


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
    assert body["features"][0]["properties"]["devices"] == ["tablet"]
    assert body["features"][0]["properties"]["first_seen_devices"] == ["tablet"]
    assert body["features"][0]["properties"]["position_devices"] == ["tablet"]
    assert "identifier" not in body["features"][0]["properties"]
    assert "name" not in body["features"][0]["properties"]

    routes = client.get(
        "/api/routes?bbox=-1,50,1,52&devices=phone&to=2024-12-31"
    )
    assert routes.status_code == 200
    assert routes.json()["features"][0]["properties"]["device"] == "phone"

    multi_device = client.get(
        "/api/networks?bbox=-1,50,1,52&zoom=16"
        "&devices=phone&from=2024-01-01&to=2024-12-31"
    )
    properties = multi_device.json()["features"][0]["properties"]
    assert properties["devices"] == ["phone", "tablet"]
    assert properties["first_seen_devices"] == ["phone"]
    assert properties["position_devices"] == ["tablet"]

    derived_channel = client.get(
        "/api/networks?bbox=-1,50,1,52&zoom=16"
        "&devices=phone&from=2024-01-01&to=2024-12-31"
    ).json()["features"][0]["properties"]
    assert derived_channel["frequency"] == 2437
    assert derived_channel["channel"] == "6"

    bluetooth = client.get(
        "/api/networks?bbox=-75,40,-73,41&zoom=16&types=BLUETOOTH"
    ).json()["features"][0]["properties"]
    assert bluetooth["frequency"] is None
    assert bluetooth["channel"] is None
    assert bluetooth["encryption"] is None
    assert bluetooth["attributes"] == "Uncategorized;10"


def test_network_stack_and_observation_contracts(
    client: TestClient,
    database: Path,
):
    identifier = "AA:00:00:00:00:04"
    observed_at = 1704110460
    latitude, longitude = 51.5, -0.1
    fingerprint = observation_fingerprint(
        "BLE", identifier, observed_at, latitude, longitude
    )
    with sqlite3.connect(database) as db:
        db.execute(
            """
            INSERT INTO networks(
                id, network_type, identifier, name, first_seen, last_seen,
                best_latitude, best_longitude, best_signal, observation_count,
                source_count, capabilities, frequency
            ) VALUES (4, 'BLE', ?, 'Sensor', ?, ?, ?, ?, -60, 1, 1,
                      'Uncategorized;10', 7936)
            """,
            (identifier, observed_at, observed_at, latitude, longitude),
        )
        db.execute(
            """
            INSERT INTO observations(
                id, observation_hash, network_id, observed_at, latitude,
                longitude, signal, accuracy, source_device, source_file, import_id
            ) VALUES (5, ?, 4, ?, ?, ?, -60, 4.0, 'phone', '/private/phone.csv', 1)
            """,
            (fingerprint, observed_at, latitude, longitude),
        )
        db.execute(
            """
            INSERT INTO observation_sources(
                observation_id, import_id, source_device, source_file
            ) VALUES (5, 1, 'phone', '/private/phone.csv')
            """
        )
        db.execute(
            "INSERT INTO network_sources(network_id, import_id) VALUES (4, 1)"
        )

    response = client.get(
        "/api/networks?bbox=-1,50,1,52&zoom=16&types=WIFI,BLE"
    )
    assert response.status_code == 200
    stack = next(
        feature
        for feature in response.json()["features"]
        if feature["properties"].get("stack")
    )
    assert stack["properties"]["count"] == 2
    assert stack["properties"]["type_counts"] == {
        "WIFI": 1,
        "BLE": 1,
        "BLUETOOTH": 0,
        "CELLULAR": 0,
    }
    stack_id = stack["properties"]["stack_id"]

    first_page = client.get(
        f"/api/network-stacks/{stack_id}?page=1&per_page=1"
        "&types=WIFI,BLE"
    )
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 2
    assert first_page.json()["has_more"] is True
    assert len(first_page.json()["features"]) == 1

    second_page = client.get(
        f"/api/network-stacks/{stack_id}?page=2&per_page=1"
        "&types=WIFI,BLE"
    )
    assert second_page.status_code == 200
    assert second_page.json()["has_more"] is False
    assert len(second_page.json()["features"]) == 1

    wifi_only = client.get(
        f"/api/network-stacks/{stack_id}?types=WIFI"
    )
    assert wifi_only.status_code == 200
    assert wifi_only.json()["total"] == 1
    assert wifi_only.json()["features"][0]["properties"]["type"] == "WIFI"

    observations = client.get("/api/networks/1/observations")
    assert observations.status_code == 200
    body = observations.json()
    assert body["total_observations"] == 2
    assert body["returned_observations"] == 2
    assert body["distinct_locations"] == 1
    assert body["truncated"] is False
    assert body["features"][0]["properties"]["observation_count"] == 2
    assert body["features"][0]["properties"]["devices"] == ["phone", "tablet"]

    phone_observations = client.get(
        "/api/networks/1/observations?devices=phone"
    )
    assert phone_observations.status_code == 200
    assert phone_observations.json()["total_observations"] == 1
    assert phone_observations.json()["features"][0]["properties"]["devices"] == [
        "phone"
    ]

    assert client.get("/api/network-stacks/not-valid").status_code == 422
    assert client.get("/api/networks/999999/observations").status_code == 404


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
    assert {
        item["source_name"] for item in body["imports"]
    } >= {"phone-export.csv", "tablet-export.csv"}
    assert all("source_path" not in item for item in body["imports"])


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

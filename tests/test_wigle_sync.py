import json
import sqlite3
from pathlib import Path

from app.importer import import_tree
from app import wigle_sync


def test_device_slug_uses_available_metadata_only():
    assert wigle_sync._device_slug({"brand": None, "model": None}) == "wigle-account"
    assert (
        wigle_sync._device_slug({"brand": "Google", "model": "Pixel 9 Pro"})
        == "wigle-google-pixel-9-pro"
    )


def test_sync_downloads_only_missing_kml_and_writes_manifests(
    tmp_path: Path,
    monkeypatch,
):
    transactions = [
        {
            "transid": "first",
            "status": "done",
            "brand": "Google",
            "model": "Pixel 9 Pro",
        },
        {
            "transid": "second",
            "status": "done",
            "brand": "Samsung",
            "model": "SM-S928B",
        },
    ]
    requested: list[str] = []

    def fake_request(url: str, authorization: str, attempts: int = 5) -> bytes:
        requested.append(url)
        assert authorization.startswith("Basic ")
        if url.endswith("/file/transactions?pagestart=0&pageend=100"):
            return json.dumps(
                {"success": True, "results": transactions}
            ).encode()
        return b'<?xml version="1.0"?><kml></kml>'

    monkeypatch.setattr(wigle_sync, "request_bytes", fake_request)
    import_root = tmp_path / "imports"
    state_dir = tmp_path / "state"
    existing = import_root / "kml" / "raw" / "first.kml"
    existing.parent.mkdir(parents=True)
    existing.write_text("<kml>existing</kml>")

    result = wigle_sync.sync_once(
        "api-name",
        "api-token",
        import_root,
        state_dir,
    )

    assert result == {
        "transactions": 2,
        "downloaded": 1,
        "existing": 1,
        "failed": 0,
    }
    assert existing.read_text() == "<kml>existing</kml>"
    assert (import_root / "kml" / "raw" / "second.kml").is_file()
    assert (state_dir / "manifest.json").is_file()
    assert (state_dir / "manifest.csv").is_file()
    assert json.loads(
        (import_root / "kml" / "raw" / "device-labels.json").read_text()
    ) == {
        "first.kml": "wigle-google-pixel-9-pro",
        "second.kml": "wigle-samsung-sm-s928b",
    }
    assert not list(tmp_path.rglob("*.partial"))
    assert len([url for url in requested if "/file/kml/" in url]) == 1


def test_sync_rejects_unsafe_transaction_identifier(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        wigle_sync,
        "fetch_transactions",
        lambda authorization: [{"transid": "../escape"}],
    )

    result = wigle_sync.sync_once(
        "api-name",
        "api-token",
        tmp_path / "imports",
        tmp_path / "state",
    )

    assert result["failed"] == 1
    assert not (tmp_path / "escape.kml").exists()


def test_existing_download_is_imported_by_post_sync_scan(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    kml = import_root / "kml" / "raw" / "existing.kml"
    kml.parent.mkdir(parents=True)
    kml.write_text(
        """<kml><Placemark><name>Test network</name><description>
        Network ID: AA:00:00:00:00:01
        Time: 2026-08-11 10:00:00
        Signal: -50
        Type: WIFI
        </description><Point><coordinates>-1.7,51.5</coordinates></Point>
        </Placemark><Placemark><name>Test network</name><description>
        Network ID: AA:00:00:00:00:01
        Time: 2026-08-11 10:01:00
        Signal: -48
        Type: WIFI
        </description><Point><coordinates>-1.701,51.501</coordinates></Point>
        </Placemark></kml>"""
    )
    monkeypatch.setattr(
        wigle_sync,
        "fetch_transactions",
        lambda authorization: [
            {
                "transid": "existing",
                "brand": "Google",
                "model": "Pixel 9 Pro",
            }
        ],
    )

    sync_result = wigle_sync.sync_once(
        "api-name",
        "api-token",
        import_root,
        tmp_path / "state",
    )
    import_result = import_tree(import_root, tmp_path / "wigle-map.sqlite")
    repeated = import_tree(import_root, tmp_path / "wigle-map.sqlite")

    assert sync_result["existing"] == 1
    assert sync_result["downloaded"] == 0
    assert import_result["complete"] == 1
    assert import_result["failed"] == 0
    assert repeated["skipped"] == 1
    with sqlite3.connect(tmp_path / "wigle-map.sqlite") as connection:
        assert connection.execute(
            "SELECT DISTINCT device_label FROM imports"
        ).fetchall() == [("wigle-google-pixel-9-pro",)]


def test_sync_metadata_relabels_an_existing_generic_import(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    kml = import_root / "kml" / "raw" / "existing.kml"
    kml.parent.mkdir(parents=True)
    kml.write_text(
        """<kml><Placemark><name>Test network</name><description>
        Network ID: AA:00:00:00:00:01
        Time: 2026-08-11 10:00:00
        Signal: -50
        Type: WIFI
        </description><Point><coordinates>-1.7,51.5</coordinates></Point>
        </Placemark><Placemark><name>Test network</name><description>
        Network ID: AA:00:00:00:00:01
        Time: 2026-08-11 10:01:00
        Signal: -48
        Type: WIFI
        </description><Point><coordinates>-1.701,51.501</coordinates></Point>
        </Placemark></kml>"""
    )
    database = tmp_path / "wigle-map.sqlite"
    first_import = import_tree(import_root, database)
    assert first_import["complete"] == 1

    monkeypatch.setattr(
        wigle_sync,
        "fetch_transactions",
        lambda authorization: [
            {
                "transid": "existing",
                "brand": "Fairphone",
                "model": "5",
            }
        ],
    )
    wigle_sync.sync_once(
        "api-name",
        "api-token",
        import_root,
        tmp_path / "state",
    )
    repeated = import_tree(import_root, database)

    assert repeated["skipped"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT DISTINCT device_label FROM imports"
        ).fetchall() == [("wigle-fairphone-5",)]
        assert connection.execute(
            "SELECT DISTINCT source_device FROM observation_sources"
        ).fetchall() == [("wigle-fairphone-5",)]
        assert connection.execute(
            "SELECT DISTINCT device_label FROM route_segments"
        ).fetchall() == [("wigle-fairphone-5",)]

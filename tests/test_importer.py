import csv
import gzip
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.importer import import_file, import_tree
from app.storage import get_import_status, initialize_database


CSV_HEADER = [
    "MAC",
    "SSID",
    "AuthMode",
    "FirstSeen",
    "Channel",
    "Frequency",
    "RSSI",
    "CurrentLatitude",
    "CurrentLongitude",
    "AltitudeMeters",
    "AccuracyMeters",
    "RCOIs",
    "MfgrId",
    "Type",
]


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as target:
        target.write("WigleWifi-1.6,model=test\n")
        writer = csv.writer(target)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)


def row(mac, seen, lat, lon, signal=-50, name="Test"):
    return [
        mac,
        name,
        "[WPA2][ESS]",
        seen,
        "6",
        "2437",
        signal,
        lat,
        lon,
        "10",
        "3",
        "",
        "",
        "WIFI",
    ]


def counts(db):
    with sqlite3.connect(db) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("imports", "networks", "observations", "route_points")
        }


def test_repeated_and_overlapping_csv_imports_are_idempotent(tmp_path):
    root = tmp_path / "imports" / "phone-a"
    first = root / "first.csv.gz"
    second = root / "second.csv.gz"
    db = tmp_path / "data" / "wigle-map.sqlite"
    shared = row("aa-bb-cc-dd-ee-ff", "2026-08-10 12:00:00", 51.5, -1.7)
    write_csv(first, [shared])
    write_csv(
        second,
        [shared, row("11:22:33:44:55:66", "2026-08-10 12:01:00", 51.501, -1.701)],
    )

    summary = import_tree(tmp_path / "imports", db)
    assert summary["complete"] == 2
    assert counts(db) == {
        "imports": 2,
        "networks": 2,
        "observations": 2,
        "route_points": 2,
    }

    repeated = import_tree(tmp_path / "imports", db)
    assert repeated["skipped"] == 2
    assert counts(db)["observations"] == 2
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT DISTINCT source_device FROM observations"
        ).fetchall() == [("phone-a",)]
        assert connection.execute(
            "SELECT observation_count, source_count FROM networks "
            "WHERE identifier='AA:BB:CC:DD:EE:FF'"
        ).fetchone() == (1, 2)


def make_wigle_db(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE network (
          bssid TEXT PRIMARY KEY, ssid TEXT, frequency INT, capabilities TEXT,
          lasttime INTEGER, lastlat REAL, lastlon REAL, type TEXT,
          bestlevel INTEGER, bestlat REAL, bestlon REAL, rcois TEXT,
          mfgrid INTEGER, service TEXT
        );
        CREATE TABLE location (
          _id INTEGER PRIMARY KEY, bssid TEXT, level INTEGER, lat REAL, lon REAL,
          altitude REAL, accuracy REAL, time INTEGER, external INTEGER, mfgrid INTEGER
        );
        CREATE TABLE route (
          _id INTEGER PRIMARY KEY, run_id INTEGER, wifi_visible INTEGER,
          cell_visible INTEGER, bt_visible INTEGER, lat REAL, lon REAL,
          altitude REAL, accuracy REAL, time INTEGER
        );
        INSERT INTO network VALUES
          ('AA:BB:CC:DD:EE:FF','Test',2437,'[WPA2]',0,0,0,'W',-40,0,0,'',0,'');
        INSERT INTO location VALUES
          (1,'AA:BB:CC:DD:EE:FF',-40,51.5,-1.7,10,3,1786359600000,0,0),
          (2,'AA:BB:CC:DD:EE:FF',-41,51.5,-1.7,10,3,1786359600000,0,0),
          (3,'AA:BB:CC:DD:EE:FF',-42,51.501,-1.701,10,3,1786359660000,0,0),
          (4,'AA:BB:CC:DD:EE:FF',-43,52.0,-2.0,10,3,1786360260000,0,0);
        INSERT INTO route VALUES
          (1,7,1,0,0,51.5,-1.7,10,3,1786359600000),
          (2,7,1,0,0,51.501,-1.701,10,3,1786359660000);
        """
    )
    connection.close()


def test_sqlite_routes_collapse_duplicate_locations_and_split_gaps(tmp_path):
    source = tmp_path / "device-exports" / "tablet" / "backup.sqlite"
    source.parent.mkdir(parents=True)
    make_wigle_db(source)
    db = tmp_path / "wigle-map.sqlite"

    result = import_file(source, db)
    assert result["status"] == "complete"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM route_candidates"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM route_points"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM route_segments"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM route_points WHERE genuine_route=1"
        ).fetchone()[0] == 2


def test_kml_stream_import_and_failed_file_status(tmp_path):
    kml = tmp_path / "imports" / "scanner" / "sample.kml"
    kml.parent.mkdir(parents=True)
    kml.write_text(
        """<?xml version="1.0"?>
        <kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
        <name>Cafe</name><description>Network ID: 00:11:22:33:44:55
        Encryption: WPA2
        Time: 2026-08-10T12:00:00+01:00
        Signal: -65
        Accuracy: 5
        Type: WIFI</description><Point><coordinates>-1.7,51.5,12</coordinates></Point>
        </Placemark></Document></kml>"""
    )
    db = tmp_path / "wigle-map.sqlite"
    assert import_file(kml, db, "scanner")["status"] == "complete"

    bad = tmp_path / "imports" / "scanner" / "bad.kml"
    bad.write_text("<kml><broken>")
    assert import_file(bad, db)["status"] == "failed"
    statuses = get_import_status(db)
    assert {item["status"] for item in statuses} == {"complete", "failed"}
    assert counts(db)["observations"] == 1


def test_kml_preserves_bluetooth_attributes_without_encryption(tmp_path):
    kml = tmp_path / "imports" / "scanner" / "bluetooth.kml"
    kml.parent.mkdir(parents=True)
    kml.write_text(
        """<kml><Placemark><name>Device</name><description>
        Network ID: AA:00:00:00:00:03
        Time: 2026-08-11T10:00:00Z
        Signal: -70
        Frequency: 7936
        Attributes: Uncategorized;10
        Type: BLE
        </description><Point><coordinates>-1.7,51.5</coordinates></Point>
        </Placemark></kml>"""
    )
    db = tmp_path / "wigle-map.sqlite"

    assert import_file(kml, db, "scanner")["status"] == "complete"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT capabilities, encryption, frequency FROM networks"
        ).fetchone() == ("Uncategorized;10", "", 7936)


def test_kml_tolerates_invalid_utf8_and_deduplicates_other_formats(tmp_path):
    csv_path = tmp_path / "imports" / "phone" / "2026-08-11" / "sample.csv.gz"
    kml_path = csv_path.with_name("sample.kml")
    shared = row(
        "AA:BB:CC:DD:EE:FF",
        "2026-08-10 12:00:00",
        51.5,
        -1.7,
        signal=-50,
    )
    write_csv(csv_path, [shared])
    kml_path.write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
        <kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
        <name>Bad \xed name</name><description>Network ID: AA:BB:CC:DD:EE:FF
        Timestamp: 2026-08-10 12:00:00
        Signal: -49
        Type: WIFI</description><Point><coordinates>-1.7,51.5</coordinates></Point>
        </Placemark></Document></kml>"""
    )
    db = tmp_path / "wigle-map.sqlite"

    result = import_tree(tmp_path / "imports", db)
    assert result["failed"] == 0
    assert counts(db)["networks"] == 1
    assert counts(db)["observations"] == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM observation_sources"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT signal FROM observations"
        ).fetchone()[0] == -49


def test_gpx_track_and_route_ingestion(tmp_path):
    gpx = tmp_path / "imports" / "phone" / "2026-08-11" / "survey.gpx"
    gpx.parent.mkdir(parents=True)
    gpx.write_text(
        """<?xml version="1.0"?>
        <gpx xmlns="http://www.topografix.com/GPX/1/1">
          <trk><trkseg>
            <trkpt lat="51.5000" lon="-1.7000">
              <ele>10</ele><time>2026-08-11T10:00:00Z</time>
            </trkpt>
            <trkpt lat="51.5010" lon="-1.7010">
              <ele>11</ele><time>2026-08-11T10:01:00Z</time>
            </trkpt>
          </trkseg></trk>
          <rte>
            <rtept lat="51.5100" lon="-1.7100">
              <time>2026-08-11T11:00:00Z</time>
            </rtept>
            <rtept lat="51.5110" lon="-1.7110">
              <time>2026-08-11T11:01:00Z</time>
            </rtept>
          </rte>
        </gpx>"""
    )
    db = tmp_path / "wigle-map.sqlite"

    result = import_file(gpx, db)
    assert result["status"] == "complete"
    assert result["format"] == "gpx"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM route_points"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM route_segments"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM route_points WHERE genuine_route=1"
        ).fetchone()[0] == 4


def test_current_and_browser_directory_layouts(tmp_path):
    root = tmp_path / "imports"
    phone = (
        root
        / "device-exports"
        / "survey-phone"
        / "2026-08-11"
        / "phone.csv.gz"
    )
    tablet = (
        root
        / "device-exports"
        / "survey-tablet"
        / "2026-08-11"
        / "tablet.csv.gz"
    )
    write_csv(
        phone,
        [row("AA:00:00:00:00:01", "2026-08-11 10:00:00", 51.5, -1.7)],
    )
    write_csv(
        tablet,
        [row("AA:00:00:00:00:02", "2026-08-11 11:00:00", 51.6, -1.8)],
    )
    server_kml = root / "kml" / "raw" / "transaction.kml"
    server_kml.parent.mkdir(parents=True)
    server_kml.write_text(
        """<kml><Placemark><name>Test</name><description>
        Network ID: AA:00:00:00:00:01
        Time: 2026-08-11 10:00:00
        Signal: -50
        Type: WIFI
        </description><Point><coordinates>-1.7,51.5</coordinates></Point>
        </Placemark></kml>"""
    )
    browser_gpx = root / "browser-phone" / "2026-08-11" / "route.gpx"
    browser_gpx.parent.mkdir(parents=True)
    browser_gpx.write_text(
        """<gpx><trk><trkseg>
        <trkpt lat="51.5" lon="-1.7"><time>2026-08-11T12:00:00Z</time></trkpt>
        <trkpt lat="51.51" lon="-1.71"><time>2026-08-11T12:01:00Z</time></trkpt>
        </trkseg></trk></gpx>"""
    )
    db = tmp_path / "wigle-map.sqlite"

    result = import_tree(root, db)
    assert result["complete"] == 4
    assert result["failed"] == 0
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()[0] == 2
        assert {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT device_label FROM imports"
            )
        } == {
            "survey-phone",
            "survey-tablet",
            "wigle-account",
            "browser-phone",
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM route_points WHERE genuine_route=1"
        ).fetchone()[0] == 2


def test_initialize_database_is_repeatable(tmp_path):
    db = tmp_path / "nested" / "wigle-map.sqlite"
    initialize_database(db)
    initialize_database(db)
    assert db.exists()

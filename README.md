# Personal WiGLE Map

A private, self-hosted map for browsing your WiGLE survey history and device
exports. It imports WiGLE SQLite databases, CSV or CSV.GZ exports, KML files
and timed GPX tracks, deduplicates overlapping observations, and displays
network density and survey routes through a Leaflet interface.

The API hides SSIDs and network identifiers by default, but the source files
and SQLite database still contain sensitive location data. Run this on a
trusted LAN and do not publish port 8787 to the internet.

## Quick start

Docker Compose is the supported deployment method:

```bash
git clone https://github.com/M1XZG/personal-wigle-map.git
cd personal-wigle-map
cp .env.example .env
mkdir -p imports runtime
sudo chown -R 10001:10001 imports runtime
docker compose up -d --build
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). Change
`BIND_ADDRESS` in `.env` to the host's private LAN address if another device
needs access.

Upload files in the browser with a lowercase device slug, or copy them beneath
`imports/<device-slug>/<date>/` and choose **Rescan storage**. Repeated scans
skip unchanged files and do not duplicate observations.

## Configuration

Docker Compose reads these values from `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BIND_ADDRESS` | `127.0.0.1` | Address exposed by Docker |
| `HOST_PORT` | `8787` | Host TCP port |
| `IMPORTS_HOST_PATH` | `./imports` | Raw import storage |
| `RUNTIME_HOST_PATH` | `./runtime` | SQLite runtime storage |
| `APP_TITLE` | `Personal WiGLE Map` | Browser and sidebar title |
| `APP_EYEBROW` | `Wireless survey archive` | Small heading above the title |
| `APP_VERSION` | `development` | Build or commit identifier displayed in the sidebar footer |
| `EXPOSE_NETWORK_IDENTIFIERS` | `false` | Return SSIDs and BSSIDs to the map UI on trusted deployments |
| `WIGLE_BADGE_URL` | blank | Optional live WiGLE badge image URL |
| `WIGLE_PROFILE_URL` | `https://wigle.net` | Link opened by the badge |
| `WIGLE_API_NAME` | blank | WiGLE API name used for account-history sync |
| `WIGLE_API_TOKEN` | blank | WiGLE API token used for account-history sync |
| `WIGLE_SYNC_SECONDS` | `86400` | Account sync interval; `0` disables automatic sync |
| `WIGLE_SYNC_ON_START` | `true` | Run an account sync after the container starts |
| `RESCAN_SECONDS` | `0` | Periodic scan interval; `0` disables it |
| `MAX_UPLOAD_BYTES` | `4294967296` | Per-file upload limit |
| `CPUS` | `4.0` | Container CPU limit |
| `MEM_LIMIT` | `8g` | Container memory limit |

To show your live WiGLE stats badge, copy the image URL from WiGLE into `.env`:

```dotenv
WIGLE_BADGE_URL=https://wigle.net/bi/YOUR_BADGE_TOKEN.png
WIGLE_PROFILE_URL=https://wigle.net
```

The browser loads that URL directly. The repository contains no badge image or
user-specific WiGLE statistics.

Network popups say **Unnamed network** while `EXPOSE_NETWORK_IDENTIFIERS` is
false. To show imported SSIDs and BSSIDs, set this in `.env` and recreate the
service:

```dotenv
EXPOSE_NETWORK_IDENTIFIERS=true
```

```bash
docker compose up -d
```

This exposes SSIDs and BSSIDs through `/api/networks` to anyone who can reach
the application. Networks whose source data contains no SSID still appear as
**Unnamed network**, but their BSSID is shown.

At high zoom, network popups also show device provenance. They identify the
device attached to the earliest observation, the device whose observation
supplies the displayed map position when different, and any additional devices
that observed the same network.

Radio metadata is interpreted by network type. Wi-Fi popups show valid Wi-Fi
frequencies and derive a missing channel when the frequency maps to a standard
2.4, 5, 6, or 60 GHz channel. Bluetooth's overloaded WiGLE device-class value
is shown as an attribute rather than being mislabeled as MHz or encryption.
Networks imported from sources that contain neither frequency nor channel
leave those fields absent.

## Automatic WiGLE account sync

Create an API name and token in your
[WiGLE account settings](https://wigle.net/account), then add them to the
private `.env` file:

```dotenv
WIGLE_API_NAME=your-api-name
WIGLE_API_TOKEN=your-api-token
WIGLE_SYNC_SECONDS=86400
WIGLE_SYNC_ON_START=true
```

Recreate the service:

```bash
docker compose up -d
```

When both credentials are set, the app checks WiGLE after startup and then at
the configured interval. It lists account upload transactions, downloads only
missing KML exports into `imports/kml/raw/`, writes sync manifests beneath
`runtime/wigle-sync/`, and immediately imports newly downloaded files.
Downloads use temporary files and atomic replacement so a failed request
cannot leave a partial KML for the importer.

When WiGLE supplies brand or model metadata for a transaction, synchronized
files receive a stable lowercase device label such as
`wigle-google-pixel-9-pro`. Existing generic `wigle-account` imports are
relabelled on the next successful sync and rescan. Files without device
metadata retain the generic label, and identical physical devices reporting
the same brand and model remain grouped together.

Set `WIGLE_SYNC_SECONDS=0` or leave both credentials blank to disable automatic
sync. Non-zero intervals must be at least 300 seconds. Credentials are passed
to the container as environment variables and can be seen by users with Docker
inspection access. Keep `.env` mode `600`, do not paste rendered Compose
configuration into support requests, and limit Docker access to trusted
administrators.

The standalone downloader remains available for a manual one-off sync:

```bash
export WIGLE_API_NAME='your-api-name'
export WIGLE_API_TOKEN='your-api-token'
python3 download_kml.py
```

The standalone script writes to the same import location and stores manifests
at the project root. Choose **Rescan storage** after a manual download.

## Backups

The backup script takes a consistent SQLite snapshot and includes raw imports:

```bash
scripts/backup_runtime.sh
```

Archives are written to `backups/` unless a destination is passed as the first
argument. Backups contain precise survey data and must be protected.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The FastAPI app lives in `app/`; the dependency-free browser interface is in
`web/`.

The map can overzoom to level 22 for separating tightly grouped observations.
OpenStreetMap's level-19 native tiles are scaled at the additional zoom levels,
so no unsupported tile URLs are requested.

For a deployment footer that identifies the exact source commit, pass the
short commit SHA while rebuilding:

```bash
APP_VERSION="$(git rev-parse --short=12 HEAD)" docker compose up -d --build
```

## Community and support

Found a problem or have an idea? Open an
[issue](https://github.com/M1XZG/personal-wigle-map/issues/new/choose) and
choose the form that matches your request. Import problems have a separate
form because real survey files must not be shared publicly.

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before
starting, especially the privacy rules for examples and test fixtures.
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers container, import, permissions
and map-loading problems. [SUPPORT.md](SUPPORT.md) explains where to ask for
help, and security issues must be reported privately as described in
[SECURITY.md](SECURITY.md).

## Privacy and third-party services

The browser loads Leaflet from unpkg, map tiles from OpenStreetMap, and your
badge from WiGLE when `WIGLE_BADGE_URL` is set. Those services receive the
browser's IP address and the requested asset or tile coordinates. Imported
survey data is not sent to them by this application.

WiGLE is a trademark of WiGLE.net. This independent project is not affiliated
with or endorsed by WiGLE. Your use of WiGLE data remains subject to WiGLE's
terms and licence.

## License

MIT

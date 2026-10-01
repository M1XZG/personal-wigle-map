# Personal WiGLE Map

A private, self-hosted map for browsing your WiGLE survey history and device
exports. It imports WiGLE SQLite databases, CSV or CSV.GZ exports, KML files
and timed GPX tracks, deduplicates overlapping observations, and displays
network density and survey routes through a Leaflet interface.

The API does not return SSIDs or network identifiers, but the source files and
SQLite database still contain sensitive location data. Run this on a trusted
LAN and do not publish port 8787 to the internet.

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
| `WIGLE_BADGE_URL` | blank | Optional live WiGLE badge image URL |
| `WIGLE_PROFILE_URL` | `https://wigle.net` | Link opened by the badge |
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

## Importing account history

`download_kml.py` downloads the KML files available through your authenticated
WiGLE account. Credentials are read only from the current shell:

```bash
export WIGLE_API_NAME='your-api-name'
export WIGLE_API_TOKEN='your-api-token'
python3 download_kml.py
```

The script writes private KML files under `imports/kml/raw/` and account
manifests at the project root. All of those paths are excluded by `.gitignore`.
Choose **Rescan storage** after the download finishes.

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

## Community and support

Found a problem or have an idea? Open an
[issue](https://github.com/M1XZG/personal-wigle-map/issues/new/choose) and
choose the form that matches your request. Import problems have a separate
form because real survey files must not be shared publicly.

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before
starting, especially the privacy rules for examples and test fixtures.
[SUPPORT.md](SUPPORT.md) explains where to ask for help, and security issues
must be reported privately as described in [SECURITY.md](SECURITY.md).

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

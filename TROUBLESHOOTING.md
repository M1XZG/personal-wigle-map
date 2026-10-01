# Troubleshooting

Start with the checks below before opening an issue. Run commands from the
directory containing `compose.yaml` unless noted otherwise.

Never post real WiGLE exports, the runtime database, SSIDs, BSSIDs, API
credentials or precise locations. Redact filenames and device names when they
identify a person or place.

## Check the container

Show the service state and published port:

```bash
docker compose ps
```

The `map` service should be running and eventually report `healthy`. If it is
stopped or restarting, inspect its logs:

```bash
docker compose logs --tail=200 map
```

Follow the logs while reproducing a problem:

```bash
docker compose logs --follow map
```

Limit the output to a recent period when the service has been running for a
long time:

```bash
docker compose logs --since=30m map
```

The application does not create a separate log file inside the container.
Uvicorn and the importer write to stdout and stderr, which Docker stores with
its `json-file` logging driver. Logs rotate at 20 MB, with five files retained.

## Check application health

The default installation listens only on the local machine:

```bash
curl --fail --silent http://127.0.0.1:8787/health | python3 -m json.tool
```

A healthy response reports `"status": "ok"` and `"database": true`. The
`import_job` section shows whether a startup import, rescan or upload is still
running.

If `curl` cannot connect, confirm the effective Compose configuration:

```bash
docker compose config
```

Check `BIND_ADDRESS` and `HOST_PORT` in `.env`. A default deployment is
available at `127.0.0.1:8787`; another device cannot reach that loopback
address. Bind to a private LAN address when remote access is needed. Do not
publish this application directly to the internet because it has no user
authentication.

## Diagnose an import failure

The logs show when an import starts or finishes and whether files failed:

```bash
docker compose logs --since=30m map
```

Detailed per-file results are stored in the runtime database and exposed by
the imports API:

```bash
curl --silent http://127.0.0.1:8787/api/imports | python3 -m json.tool
```

Look for entries with `"status": "failed"` and read their `"error"` value.
The web interface intentionally shows a shorter error so it does not expose
server paths.

Supported inputs are:

- WiGLE SQLite databases
- CSV and CSV.GZ exports
- KML files
- GPX tracks

Most unsupported extensions beneath `imports/` are ignored. A standalone
`.gz` file is inspected but fails unless it contains a supported WiGLE CSV
export. A supported file may also be reported as skipped when it has already
been imported unchanged. Rescanning does not duplicate completed imports.

When reporting a parser problem, include the export source and version, the
file format, a redacted error, and a tiny synthetic example if one is needed.
Do not attach the original survey file.

## Check mounted-directory permissions

The container runs as user and group ID `10001`. Both mounted directories must
be readable and writable by that account:

```bash
ls -ld imports runtime
```

Inspect the same paths inside the container:

```bash
docker compose exec map sh -lc 'id; ls -ld /imports /data; test -r /imports && test -w /imports && test -r /data && test -w /data'
```

For the default local paths, repair ownership with:

```bash
sudo chown -R 10001:10001 imports runtime
```

If custom paths are configured, apply the change only to those exact
directories. Do not recursively change ownership on a broad parent directory.

### Repeated `/data` permission errors after a restart

If the container repeatedly restarts with this message:

```text
Directory must be readable and writable: /data
```

the host directory mounted at `/data` is not writable by container user
`10001:10001`. With the default configuration, `/data` is the repository's
`runtime/` directory. Repair both default mounts and restart:

```bash
docker compose down
mkdir -p imports runtime
sudo chown -R 10001:10001 imports runtime
docker compose up -d
docker compose logs --tail=50 map
```

For custom mount locations, first inspect the resolved paths:

```bash
docker compose config
```

Apply `chown` only to the host paths shown for `/imports` and `/data`.
`docker compose down` does not delete bind-mounted data, so changing the
ownership does not remove imports or the runtime database.

## Uploaded files do not appear

After an upload, the API returns immediately while import work continues in
the background. Check the health response or imports API before uploading the
same file again:

```bash
curl --silent http://127.0.0.1:8787/health | python3 -m json.tool
```

Only one import job runs at a time. A second upload or rescan receives an HTTP
409 response until the active job finishes.

The default per-file upload limit is 4 GiB. Check the configured value with:

```bash
docker compose exec map printenv MAX_UPLOAD_BYTES
```

## The map page loads without tiles or styling

The browser loads Leaflet from unpkg and map tiles from OpenStreetMap. Check
the browser developer console and network panel for blocked requests. DNS
filters, content blockers, restrictive firewalls and offline clients can stop
those third-party resources from loading even when the local API is healthy.

The imported survey database remains local, but tile requests reveal the
requested map coordinates to the tile provider.

## The WiGLE badge is missing

Confirm both badge settings:

```bash
docker compose exec map sh -lc 'printf "WIGLE_BADGE_URL=%s\nWIGLE_PROFILE_URL=%s\n" "$WIGLE_BADGE_URL" "$WIGLE_PROFILE_URL"'
```

The browser retrieves the badge directly from WiGLE. An expired badge URL,
network filter or browser privacy setting can block it. The badge is optional
and does not affect imports or the map API.

## Rebuild after an update

Pull the latest code, rebuild the image and recreate the service:

```bash
git pull --ff-only
docker compose up -d --build
```

Then check health and recent startup logs:

```bash
docker compose ps
docker compose logs --since=5m map
```

The `imports/` and `runtime/` directories are bind mounts and are not replaced
when the container is rebuilt.

## Back up before recovery work

Create a consistent backup before changing or replacing runtime data:

```bash
scripts/backup_runtime.sh
```

The archive contains precise survey information. Store it somewhere private.
Do not delete `runtime/wigle-map.sqlite` as a first troubleshooting step; doing
so removes import history and processed data. If the database is damaged,
preserve the logs and database, restore a known-good backup, or open an issue
with a redacted error message.

## Opening an issue

Use the matching form on the
[issue chooser](https://github.com/M1XZG/personal-wigle-map/issues/new/choose).
Include:

- the release, container tag or commit SHA;
- Docker and operating-system versions;
- the deployment method and relevant `.env` settings with secrets removed;
- reliable reproduction steps;
- a short redacted log excerpt;
- the matching failed record from `/api/imports`, if applicable.

Security vulnerabilities belong in
[private vulnerability reporting](https://github.com/M1XZG/personal-wigle-map/security/advisories/new),
not a public issue.

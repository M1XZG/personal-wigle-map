#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="${RUNTIME_DIR:-$PROJECT_ROOT/runtime}"
IMPORT_DIR="${IMPORT_DIR:-$PROJECT_ROOT/imports}"
BACKUP_DEST="${1:-${BACKUP_DEST:-$PROJECT_ROOT/backups}}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGING="$BACKUP_DEST/.personal-wigle-map-$TIMESTAMP.partial"
ARCHIVE="$BACKUP_DEST/personal-wigle-map-$TIMESTAMP.tar.gz"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v rsync >/dev/null || { echo "rsync is required" >&2; exit 1; }
command -v tar >/dev/null || { echo "tar is required" >&2; exit 1; }

mkdir -p "$BACKUP_DEST"
if [[ -e "$STAGING" || -e "$ARCHIVE" ]]; then
    echo "Backup path already exists for timestamp $TIMESTAMP" >&2
    exit 1
fi
mkdir -p "$STAGING/runtime" "$STAGING/imports"
trap 'rm -rf -- "$STAGING"' EXIT

if [[ -d "$RUNTIME_DIR" ]]; then
    rsync -a \
        --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
        --exclude='*.sqlite' --exclude='*.sqlite-wal' --exclude='*.sqlite-shm' \
        --exclude='*.sqlite3' --exclude='*.sqlite3-wal' --exclude='*.sqlite3-shm' \
        "$RUNTIME_DIR/" "$STAGING/runtime/"

    RUNTIME_DIR="$RUNTIME_DIR" BACKUP_RUNTIME="$STAGING/runtime" python3 <<'PY'
import os
import sqlite3
from pathlib import Path

source_root = Path(os.environ["RUNTIME_DIR"])
backup_root = Path(os.environ["BACKUP_RUNTIME"])
suffixes = {".db", ".sqlite", ".sqlite3"}

for source in source_root.rglob("*"):
    if not source.is_file() or source.suffix.lower() not in suffixes:
        continue
    target = backup_root / source.relative_to(source_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
PY
fi

if [[ -d "$IMPORT_DIR" ]]; then
    rsync -a "$IMPORT_DIR/" "$STAGING/imports/"
    IMPORT_DIR="$IMPORT_DIR" INVENTORY="$STAGING/IMPORT-INVENTORY.tsv" python3 <<'PY'
import os
from datetime import datetime, timezone
from pathlib import Path

root = Path(os.environ["IMPORT_DIR"])
inventory = Path(os.environ["INVENTORY"])
with inventory.open("w", encoding="utf-8") as out:
    out.write("path\tsize_bytes\tmodified_utc\n")
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        out.write(f"{path.relative_to(root)}\t{stat.st_size}\t{modified}\n")
PY
fi

printf 'created_utc=%s\nruntime_dir=%s\nimport_dir=%s\n' \
    "$TIMESTAMP" "$RUNTIME_DIR" "$IMPORT_DIR" > "$STAGING/BACKUP-INFO.txt"

tar -C "$BACKUP_DEST" -czf "$ARCHIVE" "$(basename -- "$STAGING")"
rm -rf -- "$STAGING"
trap - EXIT
echo "$ARCHIVE"

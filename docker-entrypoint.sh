#!/usr/bin/env bash
set -euo pipefail

umask 0002

for directory in "${DATA_DIR:-/data}" "${IMPORT_ROOT:-/imports}"; do
    mkdir -p "$directory"
    if [[ ! -r "$directory" || ! -w "$directory" ]]; then
        echo "Directory must be readable and writable: $directory" >&2
        exit 1
    fi
done

exec "$@"

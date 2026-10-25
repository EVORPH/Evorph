#!/bin/sh
# Write a verified copy of the private state into a new timestamped directory.
set -eu

BACKEND_DIR=${EVORPH_BACKEND_DIR:-/opt/evorph/backend}
PYTHON=${EVORPH_PYTHON:-/opt/evorph/venv/bin/python}
STATE=${EVORPH_STATE:-/var/lib/evorph/state}
RUNTIME_CONFIG=${EVORPH_RUNTIME_CONFIG:-/etc/evorph/runtime.json}
BACKUP_ROOT=${EVORPH_BACKUP_ROOT:-/var/backups/evorph}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=$BACKUP_ROOT/$STAMP

mkdir -p "$BACKUP_ROOT"
if [ -e "$DEST" ]; then
  printf 'backup destination already exists: %s\n' "$DEST" >&2
  exit 1
fi

cd "$BACKEND_DIR"
PYTHONPATH=$BACKEND_DIR "$PYTHON" -m runtime --state "$STATE" --config "$RUNTIME_CONFIG" backup --output "$DEST"

if [ ! -f "$DEST/manifest.json" ]; then
  printf 'backup manifest is missing: %s\n' "$DEST" >&2
  exit 1
fi
printf 'backup directory: %s\n' "$DEST"

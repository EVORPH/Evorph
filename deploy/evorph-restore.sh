#!/bin/sh
# Restore a backup into a new directory. The live state directory is never modified or removed.
set -eu

BACKEND_DIR=${EVORPH_BACKEND_DIR:-/opt/evorph/backend}
PYTHON=${EVORPH_PYTHON:-/opt/evorph/venv/bin/python}
STATE=${EVORPH_STATE:-/var/lib/evorph/state}
RUNTIME_CONFIG=${EVORPH_RUNTIME_CONFIG:-/etc/evorph/runtime.json}

SOURCE=${1:-}
if [ -z "$SOURCE" ]; then
  printf 'usage: evorph-restore.sh BACKUP_DIRECTORY [TARGET_DIRECTORY]\n' >&2
  exit 2
fi
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TARGET=${2:-$STATE.restored-$STAMP}

if [ ! -d "$SOURCE" ] || [ ! -f "$SOURCE/manifest.json" ]; then
  printf 'not a backup directory: %s\n' "$SOURCE" >&2
  exit 1
fi
if [ -e "$TARGET" ]; then
  printf 'restore target already exists: %s\n' "$TARGET" >&2
  exit 1
fi

cd "$BACKEND_DIR"
PYTHONPATH=$BACKEND_DIR "$PYTHON" -m runtime --state "$TARGET" --config "$RUNTIME_CONFIG" restore --source "$SOURCE"

printf 'restored into: %s\n' "$TARGET"
printf 'live state untouched: %s\n' "$STATE"
printf 'to switch over: stop both units, rename %s aside, rename %s to %s, start the units again\n' \
  "$STATE" "$TARGET" "$STATE"

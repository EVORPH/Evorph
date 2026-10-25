#!/bin/sh
# Stop the scheduling worker first so no research round is cut in half, then the API.
set -eu

API_UNIT=${EVORPH_API_UNIT:-evorph-api.service}
SCHEDULER_UNIT=${EVORPH_SCHEDULER_UNIT:-evorph-scheduler.service}

for unit in "$SCHEDULER_UNIT" "$API_UNIT"; do
  if ! systemctl is-active --quiet "$unit"; then
    printf 'already stopped: %s\n' "$unit"
    continue
  fi
  systemctl stop "$unit"
  if systemctl is-active --quiet "$unit"; then
    printf '%s is still active\n' "$unit" >&2
    exit 1
  fi
  printf 'stopped: %s\n' "$unit"
done

#!/bin/sh
# Start the public API. The scheduling worker is started only when explicitly requested.
set -eu

API_UNIT=${EVORPH_API_UNIT:-evorph-api.service}
SCHEDULER_UNIT=${EVORPH_SCHEDULER_UNIT:-evorph-scheduler.service}
START_SCHEDULER=${EVORPH_START_SCHEDULER:-0}

systemctl start "$API_UNIT"
if [ "$START_SCHEDULER" = "1" ]; then
  systemctl start "$SCHEDULER_UNIT"
else
  printf 'scheduler unit left stopped; set EVORPH_START_SCHEDULER=1 once a schedule is configured\n'
fi

if ! systemctl is-active --quiet "$API_UNIT"; then
  printf 'api unit did not become active\n' >&2
  exit 1
fi
if [ "$START_SCHEDULER" = "1" ] && ! systemctl is-active --quiet "$SCHEDULER_UNIT"; then
  printf 'scheduler unit did not become active\n' >&2
  exit 1
fi
printf 'started: %s\n' "$API_UNIT"
if [ "$START_SCHEDULER" = "1" ]; then
  printf 'started: %s\n' "$SCHEDULER_UNIT"
fi

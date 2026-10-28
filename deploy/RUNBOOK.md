# Operating guide

Everything in this directory is inspectable on a local machine. Host names, channel identifiers, and credentials are placeholders; none of them are real values.

## What is here

| File | Purpose |
|---|---|
| `ops.example.json` | Service configuration sample: state directory, public origin, listen address, two offline model channels |
| `ops.live-example.json` | Same shape for real provider channels: `mode: live`, a per-channel `provider_name`, a context-bound meter basis, and a credential path instead of a recorded script |
| `runtime.example.json` | Private state configuration sample: capacity limits and spending caps; `spending: null` means the ledger is not armed |
| `evorph-api.service` | Unit template for the public read and submission API |
| `Caddyfile.example` | Loopback API proxy, request-size and connection deadlines, and independent static hosting |
| `evorph-scheduler.service` | Unit template for the research scheduling worker |
| `evorph-start.sh`, `evorph-stop.sh` | Start and stop |
| `evorph-backup.sh`, `evorph-restore.sh` | Backup and restore |

The entry points live in `backend/ops/` and only assemble existing modules; they do not redefine any contract. The judging core in `backend/evorph/` is untouched.

## Placeholder layout

Code in `/opt/evorph`, an isolated Python environment in `/opt/evorph/venv`, configuration in `/etc/evorph`, private state in `/var/lib/evorph/state`, backups in `/var/backups/evorph`, and a dedicated service account named `evorph`. A reverse proxy in front of the API terminates TLS and serves static files.

## First run

```
python3 -m venv /opt/evorph/venv
mkdir -p /etc/evorph /var/lib/evorph /var/backups/evorph
cp deploy/ops.example.json /etc/evorph/ops.json
cp deploy/runtime.example.json /etc/evorph/runtime.json
```

Edit `/etc/evorph/ops.json` and set `origin` (the public HTTPS origin), `api.listen_port`, `state`, `runtime_config`, and `openssl`. Point `openssl` at a binary that is confirmed to support ML-DSA on the target distribution; leaving it `null` falls back to the `openssl` on `PATH`.

```
cd /opt/evorph/backend
PYTHONPATH=. /opt/evorph/venv/bin/python -m runtime --state /var/lib/evorph/state --config /etc/evorph/runtime.json init
PYTHONPATH=. /opt/evorph/venv/bin/python -m ops status --config /etc/evorph/ops.json
cp ../deploy/evorph-api.service ../deploy/evorph-scheduler.service /etc/systemd/system/
systemctl daemon-reload
../deploy/evorph-start.sh
```

`status` is read-only and reports `mode`, `schedule`, `pending_tasks`, `spending_paused`, `calls_blocking_schedule`, `model_calls`, and private per-batch `publications` summaries.

## Start and stop

- `deploy/evorph-start.sh` starts the API unit only.
- `EVORPH_START_SCHEDULER=1 deploy/evorph-start.sh` also starts the scheduling worker. Without a stored schedule the worker exits with code 3, and the unit stops retrying after three failures within 15 minutes and stays in the failed state.
- `deploy/evorph-stop.sh` stops the scheduler first, then the API. A unit that was never running is reported as `already stopped` rather than as stopped by this run.
- Override names and paths with `EVORPH_API_UNIT`, `EVORPH_SCHEDULER_UNIT`, `EVORPH_BACKEND_DIR`, `EVORPH_PYTHON`, `EVORPH_STATE`, `EVORPH_RUNTIME_CONFIG`, and `EVORPH_BACKUP_ROOT`.

## Schedule policy

```
python -m ops schedule --config /etc/evorph/ops.json --template-id <batch template> --interval-seconds <interval> --check-seconds <check interval>
python -m ops schedule-interval --config /etc/evorph/ops.json --interval-seconds <interval>
python -m ops schedule-enable --config /etc/evorph/ops.json
python -m ops schedule-pause --config /etc/evorph/ops.json
python -m ops reconcile --config /etc/evorph/ops.json
```

`schedule` writes the whole policy (template plus rhythm) and always stores `enabled=0`; it refuses while a round is still pending. `schedule-interval` changes only the rhythm: it takes effect from the moment it is written, keeps the current run state and any recorded fault, and needs no template. The optional `--enable-if-clear` saves the frequency even when usage or spending blocks resumption. It resumes a normally paused schedule only when clear, reports a `blocked_reason` otherwise, and never clears a recorded fault. A round already running is never cut short, and when it finishes it does not overwrite a rhythm that was changed while it ran.

`schedule-enable` requires a stored policy, a spending ledger that is not paused, and no call sitting in `dispatching`, `response_saved`, or `usage_unknown`; when those hold it starts research and clears a previously recorded fault. `schedule-pause` stops new rounds and lets a claimed round finish. `reconcile` settles recorded provider usage from the response and receipt already stored for that call; it never resends a generation, and a call with no stored evidence stays unresolved rather than being assumed free.

Every paid round shares one rhythm. A queued participant submission is served first when the deadline arrives, but it does not buy an extra round. Batch templates come from the research flow, so this directory does not create them. Interval values are set from measured runs and approved by the operator; the samples deliberately leave them out.

## Request boundaries and access events

`Caddyfile.example` keeps static files independent of the API. Replace its example host before use. It limits headers to 32 KB and bodies to 64 KB, with five seconds for headers and ten seconds for request bodies. The backend retains its own absolute connection deadline. Check the configuration with `caddy adapt --adapter caddyfile --config <file>` before an approved reload; adding the sample to a checkout does not change a running proxy.

The API writes JSON access events to standard output: normalized route, method, response status, duration, and response/timeout/closed outcome. Query strings, raw paths, addresses, headers, and bodies are omitted. The systemd unit routes these events to the journal; inspect them with `journalctl -u evorph-api --since '10 minutes ago'`. Queue saturation emits `http_overload` with status 503. These records support diagnosis but do not themselves send alerts. Configure journal retention on the host rather than accumulating an unbounded application log.

## Backup and restore

- `deploy/evorph-backup.sh` writes `/var/backups/evorph/<UTC timestamp>/` containing `manifest.json` and a sha256 for every file. It refuses to run when the destination already exists. It never deletes older copies; retention is an operator decision and no automatic cleanup is implemented.
- `deploy/evorph-restore.sh <backup directory> [target directory]` restores into a **new** directory (`<state>.restored-<timestamp>` by default). It does not modify or delete the state in use, and it prints the manual switch-over sequence: stop both units, rename the old directory aside, rename the restored directory into place, start the units again.
- Restore requires the `limits` in `runtime.json` to match the values stored in the backup; the existing restore logic rejects a mismatch.

## Known boundaries and failure modes

1. **A failed archive read in the page is silent.** The frontend keeps the records it already loaded and does not render an error panel, so read health has to be judged from API logs and `ops status`, not from the page.
2. **Two reviewed transports, and both roles must agree.** The research integration accepts either the recorded fixture transport or the reviewed provider transport, rejects anything else, and refuses to mix the two roles. An empty `channels.*.script` still makes every fixture send fail.
3. **Fixtures plus an armed ledger wedge the schedule.** If spending is unpaused and the schedule is enabled while the channels are still fixtures, a send exception is recorded as `usage_unknown`. The scheduler then keeps returning `resource_paused` until that call is settled. `ops reconcile` settles one from the response already stored for it; a call with no stored evidence stays unresolved rather than being assumed free. Keep `spending` at `null` and the schedule disabled until the channels are the ones you intend to pay for.
4. **API concurrency is bounded.** Four fixed worker threads each own a separate runtime and SQLite connection; at most four accepted connections wait in the application queue. A full queue receives 503 rather than creating another thread. Each active connection retains its configured absolute deadline, including partial headers and bodies. This limits resource growth; it is not a claim of resistance to sustained overload. The scheduler remains a separate process.
5. **A stop signal is not immediate.** The worker checks the stop event between ticks, so a running research round finishes first. `TimeoutStopSec=900` on the scheduler unit has to stay above the measured duration of one round, and `KillMode=control-group` reaps the crypto subprocesses with the parent.
6. **Resource caps are placeholders.** `MemoryMax`, `MemoryHigh`, and `CPUQuota` apply to the whole cgroup, including the `openssl` subprocesses. Current values are 1G and 100% for the API and 2G and 200% for the scheduler; both are set from measurement, not from guesswork.
7. **The archive mode is `example` or `live`, and it is immutable once written.** `live` names where the public records came from; it does not claim that research is running now, and a live archive may legitimately be empty. The mode has to match the configured channels, so a live archive cannot be fed by fixture channels. An existing state directory cannot be switched over — start a separate one for real runs and leave the example archive untouched.
8. **A finished round is projected through the existing review gate.** Records that pass the mechanical release rules are published automatically; a record that does not pass stays private, and a failed projection remains queued for the next schedule check, including while paused and after a process restart, without re-running research or re-billing. Private `ops status` returns `publications.items`, a whole-archive `summary`, and `next_before` for pages of 50 records; pass `--publication-before` to read older pages. Reports distinguish `partial` filtering from `deferred` projection failures with counts and reason categories, never the withheld text. Deferred items rotate persistently so later batches are retried. Recovery completes an interrupted batch from its recorded terminal task without restarting research. Internal record kinds that have no public projection are not counted as filtered public statements.

## Not verified yet

- Never run on Linux: memory peaks, subprocess-group timeout reaping, and a backup restore drill.
- No reverse proxy or certificate is configured here; the proxy software and host are chosen at deployment time.
- Inspecting or unit-testing the files in this directory starts no resident process and makes no outbound network call. Running the start scripts does both, through whichever channel the configuration names; the live deployment uses the reviewed provider channel, and its current schedule and spending state are operator configuration, not properties of these files.

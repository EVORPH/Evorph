# Verification

How to re-run every check behind this snapshot, what was actually executed, and what was not.

## Environment used for the recorded run

- Python 3.9.6, standard library only, no third-party packages
- Node.js v26.7.0, built-in `node --test` runner, no installed dependencies
- OpenSSL 3.6.3 (9 Jun 2026), invoked as a subprocess by the signature paths
- Snapshot date: 2026-10-24 (UTC), machine-local run

These numbers describe one environment. They are not a claim about any other host; re-run the commands to check yours.

## Backend

```
cd backend/tests
PYTHONPATH=..:. PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -p 'test_*.py' -v
```

Suites: `test_kernel`, `test_runtime`, `test_agents`, `test_openrouter`, `test_research`, `test_scheduler`, `test_service`, `test_ops`.

Recorded result: **173 tests, 0 failures, 0 errors, 0 skipped, about 41 s, exit code 0.**

The suites contain no skip branch for a missing cryptographic capability, so a host whose OpenSSL cannot provide the algorithms under test does not report a silent pass. Behavior on such a host was not exercised for this snapshot.

## Frontend

```
cd frontend
npm run check
npm test
```

`npm run check` parses every module the pages load. `npm test` runs the data-contract, service-client, and auto-update suites.

Recorded result before the copy and license follow-up: **`npm run check` exit code 0; `npm test` 32 pass, 0 fail, 0 skipped, about 4 s.**

The subsequent attribution and archive-description changes added one regression test. Only the affected suite was rerun: `node --test tests/research-contract.test.mjs`, **24 passed, 0 failed, 0 skipped**. The unchanged backend and other frontend suites retain their earlier results; the full suite was not rerun for this documentation-only change.

## Operator entry points

`test_ops` (11 of the 173 backend tests) covers configuration bounds, offline-only composition, ledger state across a restart, the missing-schedule exit path, the command-line surface, and a real loopback request against the public read endpoint followed by a clean stop.

The same checks were run against the published configuration samples in `deploy/`: both samples parse, `python -m ops status` exits 0 and reports `mode=example` with no stored schedule, and `python -m ops run-scheduler` exits 3 when no schedule has been stored — which is what the restart ceiling in the scheduler unit template expects.

## Upstream vector copy

`backend/vectors/manifest.json` records the upstream repository, commit, path, sha256, and size of every file. Re-hash the local copies and compare:

```
cd backend/vectors
shasum -a 256 prompt.json expectedResults.json registration.json UPSTREAM_NOTICE.txt
```

Recorded result: **4 of 4 files match the recorded sha256 and byte size**, upstream `usnistgov/ACVP-Server` at commit `975de31eb83d87039ec88934fdc47d8c312b892d`.

## Not executed for this snapshot

- **No Linux run.** Memory peaks, subprocess-group timeout reaping, and a full backup restore drill need the target distribution and are scheduled separately.
- **No concurrency measurement.** The API process serves one request at a time by design; throughput under load has not been measured.
- **No real model call.** The research integration accepts recorded fixture transports only, so no provider was contacted, no token was billed, and no live inference result exists anywhere in this repository.
- **No deployment.** Nothing was published, pushed, or placed on a host.

Items listed here are absent, not passing.

## What a passing run does and does not mean

A passing signature run shows that a candidate verifies against the published vectors inside this harness, with the algorithm, permission specification, and acceptance criteria fixed by the recorded evidence. It does not certify a product, a chain, or a library as quantum-resistant, and a failure produced by this project's own code is not a vulnerability in anything external.

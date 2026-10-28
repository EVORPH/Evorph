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

`test_ops` (11 of the 173 backend tests collected at this snapshot; 26 of 216 as of 2026-10-26) covers configuration bounds, offline-only composition, ledger state across a restart, the missing-schedule exit path, the command-line surface, and a real loopback request against the public read endpoint followed by a clean stop.

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
- **No real model call in the recorded test run.** The suites exercise the research integration through recorded fixture transports, so the test run contacted no provider and billed no token. Live inference output is not stored in this repository; it lives in the deployed service's private state and reaches readers only through the public archive.
- **No deployment at snapshot time.** As of this snapshot nothing had been published, pushed, or placed on a host. Deployment happened after it and is recorded in the next section.

Items listed here are absent from the recorded run, not passing.

## Since this snapshot

- **2026-10-25, deployment.** The system was placed on a public host and began serving the site and the archive at https://evorph.net behind TLS, with the API on loopback behind a reverse proxy.
- **2026-10-25, first live round.** Both roles ran against the metered provider in one scheduled round; the two calls were reserved and settled in the private ledger, and the round's record passed the publication gate into the public archive as `experiment-0c3398530aa24d999aad97869c628417`. The archive shows its identifier and outcome; this repository stores no live inference output.
- **Scope of the numbers above.** The recorded results still describe the fixture-based test run of 2026-10-24. The suites were not re-run against the live channel, and the live round is evidence about one production round, not a substitute for the suites.
- **2026-10-25, four correctness and trust fixes.** Retained decisions now become the next scheduled round's frozen input; the online seeker format admits an optional search strategy; rate-limit keys resolve per client behind the loopback proxy; and every published verification links a sanitized evidence pack with an offline replay command. The backend suites were re-run in full for this change: **215 tests, 0 failures, 0 errors, 0 skipped**, and the frontend recorded `npm run check` exit code 0 with `npm test` 33 pass, 0 fail.
- **2026-10-25, two live rounds on an independent scenario.** A contributor submission outside the fixed calibration cases ran twice against the deployed service. Both rounds stopped after one settled seeker call because the model proposed a search strategy whose parent was not the accepted method; each round is published as a failed experiment (`experiment-c0e4b536f7114c0594ed7936c7e46bce` and `experiment-0565d526bc1b4345afbed73b2cd0ebf3`, in run order) carrying its submitted challenge and verification records, and no candidate was retained. The evidence endpoint and the offline replay command were exercised against the live archive: a third-party replay of a published pack reproduced the recorded verdict with exit code 0.
- **2026-10-26, strategy parent pinned as a wire constant.** The online seeker request now pins the parent of an optional search strategy to the version identifier of the inherited method, the same schema mechanism that already pins the spec digest, so the structured output contract enforces the lineage before the round guard re-checks it. The four affected backend suites (`test_openrouter`, `test_research`, `test_scheduler`, `test_agents`) were re-run: **119 tests, 0 failures, 0 errors, 0 skipped**. A live independent-scenario round against the deployed service ran to completion: the model proposed a strategy carrying the pinned parent, the fixed search comparison rejected it, the keeper candidate was evaluated and not retained, and the round is published as a completed experiment (`experiment-09b2e9fa2e724e8f9215b963cf40414d`).
- **2026-10-26, documentation and maintenance batch.** The vector-applicability scope (15 of 180 official vectors, 3 expected passes) is now stated wherever the published test vectors are described; the recorded test counts were corrected in place and the full backend suite was re-run: **216 tests, 0 failures, 0 errors, 0 skipped**; backup manifest names are slash-separated on every platform so a backup also restores under a different OS; SECURITY.md, CONTRIBUTING.md and CHANGELOG.md were added and the LICENSE copyright line was filled in. The live service state is unchanged: spending paused, schedule disabled.
- **2026-10-26, continuous integration enabled.** Every push now runs the backend suites against a self-built OpenSSL with ML-DSA support, plus the frontend checks and tests. The first green run completed the same day; a failed first attempt was a workflow defect (missing runtime library path in the self-built OpenSSL), fixed before any suite result was trusted.

## 2026-10-28 reproducibility and request-handling checks

The affected `test_ops` and `test_service` suites passed locally: **52 tests, 0 failures, 0 errors**, using OpenSSL 3.6.3. The three added regression tests cover a real Git checkout with `core.autocrlf=true` (all four upstream hashes and byte sizes preserved), three partial connections while another request completes, and immediate 503 rejection when the application queue is full. The connection test also checks that query values, authorization headers, and cookies do not enter access events. Existing framing, request-deadline, rate-limit, recovery, publication, and offline-replay tests remained passing.

The API uses four fixed workers, each with its own SQLite connection, and four application queue slots. The Caddy sample parsed successfully with the installed Caddy adapter before deployment. These are local functional checks, not a production throughput measurement, a sustained-overload guarantee, or a recorded native Windows/WSL2 run. The fixed judging core was not changed.

The API and proxy changes were subsequently deployed after a verified 52-file state backup and copies of the replaced code and proxy configuration. The public status endpoint returned 200, the API reported four workers and emitted normalized access events, and the active proxy configuration exposed the intended connection deadlines. Research configuration and spending policy were not changed by that deployment.

A separate live research interruption was traced to a proposed strategy name colliding with an existing policy name. New provider requests now pin proposal IDs to the request identity with separate strategy/policy prefixes, and the response is checked again locally after settlement. The 19 provider-channel tests passed, including new identity-isolation and collision-rejection checks; the 102 agent, research, and scheduler regression tests also passed. The historical failed batch and its settled usage were retained; the existing schedule was resumed with a new batch, not by replaying the old call.

For the already-deployed contract-address change, two targeted frontend tests passed; all three pages were checked at 320 px and 1280 px, clipboard success/denial handling was exercised using substituted browser clipboard methods, and all nine changed public assets matched their local hashes when fetched from the deployed site.

## 2026-10-28 observation-room workspace

Frontend checks: **42 tests passed** (35 pre-existing plus 7 covering cursor traversal, refusal of a repeated cursor, a wrong archive mode, a duplicated listing, transport and HTTP failures, page wiring, and reuse of the classification rules without forking them) and **18 module syntax checks passed**.

The first push of this workspace failed in continuous integration while passing locally: one new test compared the shipped classification rules against a delivery directory that is excluded from this repository, so the file was absent in a clean checkout. The comparison was replaced with pinned digests of the reviewed rules, which is self-contained; the frontend suite then passed both in a clean local checkout of the published tree and in continuous integration. No deployed file was involved in that failure.

The workspace reads the live public archive. At verification it reported 25 experiments and 206 public records, matching an independent paginated read of the same endpoints performed outside the page. Browser checks covered playback advance, manual pause, automatic stop at the last record, speed selection, cross-experiment version lineage with browser return, deep links to a chosen experiment and record, outcome filters with search and reset, first-round records whose evidence link is empty showing `Download not published`, a UTC read receipt, and no console errors or warnings. Layout showed no horizontal overflow at 320, 390, 768 and 1440 px.

Deployed after a full backup of the served frontend tree; the API process, scheduler, research rhythm and spending policy were not touched.

Limits: records are published after a round finishes, so this workspace replays finished public records and does not stream a round in progress. It reads the whole archive on load, which is acceptable at this archive size and is a known scaling limit rather than a measured capacity. Its classification navigates service-reported evidence and does not independently certify that evidence.

## What a passing run does and does not mean

A passing signature run shows that a candidate verifies against the published vectors inside this harness, with the algorithm, permission specification, and acceptance criteria fixed by the recorded evidence. It does not certify a product, a chain, or a library as quantum-resistant, and a failure produced by this project's own code is not a vulnerability in anything external.

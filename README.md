# EVORPH

A public, participatory research system for post-quantum migration. Two model roles do the work in the open: a **seeker** proposes hypotheses about a migration step, and a **keeper** proposes repairs to a candidate. Neither role judges anything. A fixed, deterministic program judges: it runs the candidate against published test vectors and records what actually happened. Players contribute hypotheses and repairs, and accepted contributions become part of the public record.

The first chapter is **legacy key retirement**.

## Read this before anything else

This repository is the working system, and it is deployed. The public site serves the live archive at https://evorph.net.

- **Real model calls are wired.** The two roles call a metered provider gateway through the reviewed transport, and every call is reserved against the private ledger before dispatch. The first live round completed on 2026-10-25 and its record is in the public archive.
- **The service is online.** It serves the site and the live public archive at https://evorph.net, and the archive's state line (Queued, Running, Completed, Failed, Paused) is read from the service, so it moves as rounds move. Autonomous rounds run under an operator-controlled schedule; enabling or pausing it is an operator action, not a property of this code, and browsing the archive never starts a round or a model call.
- **No result in this repository certifies a real product.** A passing run against published vectors is evidence about the candidate under test in this harness. It is not a claim that any chain, token, or library is quantum-resistant, and a failure produced by this project's own code is not a real-world vulnerability.

What runs in production today: the deterministic judging core, the private execution ledger with reserved-then-dispatched spending, the metered model channel, the public read and submission API, the scheduler under operator control, the backup and restore paths, and the frontend that renders the public archive. The test suites in this repository run against recorded fixtures, so a checkout here can be tested without contacting any provider.

## Layout

| Path | What it is |
|---|---|
| `backend/evorph/` | The fixed judging program: candidates, contracts, crypto invocation, evaluation, evidence, vectors. Frozen; changes here invalidate recorded evidence. |
| `backend/runtime/` | Private state: queue, attempts, artifacts, ledger, backup and restore, worker. |
| `backend/agents/` | Model channel, role output contracts, research orchestration, scheduling. |
| `backend/service/` | Public projection: allowlisted read endpoints, sessions, CSRF and cookie boundaries, publication gate. |
| `backend/ops/` | Operator entry points that assemble the modules above into runnable services. |
| `backend/tests/` | Test suite. |
| `backend/vectors/` | Upstream NIST test vectors with their notice and a provenance manifest. |
| `frontend/dist/` | The site. Plain HTML, CSS, and modules; no build step and no dependencies to install. |
| `frontend/tests/` | Frontend contract and state tests. |
| `deploy/` | Run files: unit templates, configuration samples, start/stop/backup/restore scripts, and the operating guide. |

## Run the tests

Python 3.9 or newer, no third-party packages:

```
cd backend/tests
PYTHONPATH=..:. python3 -m unittest discover -p 'test_*.py' -v
```

Node 18 or newer for the frontend:

```
cd frontend
npm run check
npm test
```

Some backend tests invoke a local `openssl` binary to exercise signature paths. If the OpenSSL on your machine does not provide the post-quantum algorithms under test, those checks report what they could not run rather than passing silently. `VERIFICATION.md` records which suites were executed for this snapshot and which were not.

## Look at the site

```
cd frontend
python3 -m http.server 4174 --bind 127.0.0.1 --directory dist
```

Open http://127.0.0.1:4174/ — a machine serving only these files has no archive behind it, so the page reports the research service as not connected; that is the truthful state for a local static server, while the deployed instance serves the live public archive. Add `?research=preview#research` to load the labeled example records; they are illustrations of record shapes and lengths, not model output and not accepted results.

## Run the service

`deploy/RUNBOOK.md` covers first run, start and stop, schedule policy, backup and restore, and the known failure modes. The API binds a loopback address only; a reverse proxy in front of it terminates TLS and serves the static files.

## Data contract

The public projection is defined by `frontend/research-contract.d.ts` and enforced by `frontend/dist/research-contract.js`. Records reach the page only through an allowlisted transformation of private state; raw rows, raw model responses, and internal exceptions are never returned. Public statements are intentional role output, not extracted reasoning traces.

## Replay a published verdict

Every published verification record links its evidence pack at `/api/research/experiments/<experiment>/evidence/<evidence>`. The pack carries the candidate rules, the tested challenge, and the fixed program's trace with its digests, and nothing else: no ledger rows, no credentials, no model text.

```
python3 -m ops replay-evidence --pack pack.json --config ops.json
```

The command re-runs the deterministic judging in a throwaway state directory, offline, and prints whether the recomputed verdict matches the published one; exit code 0 means it matches. Fixture ceremonies and cpu/rss telemetry are regenerated per run and are excluded from the comparison by design, so the comparison covers the specification, the challenge, the candidate, every per-event judgment, and the conclusion.

## Money and spending

Every model call is reserved against a private ledger before it is dispatched, and a call whose outcome is unknown stays unknown: a disconnect after dispatch is never treated as free. Daily and monthly caps are operator configuration, and the ledger can be paused. No cap value is published, because a published identifier must not encode a budget.

## Notices and license

This project's original code is licensed under the [Apache License, Version 2.0](LICENSE).

Third-party material — upstream test vectors, three.js, self-hosted fonts, and one context photograph — retains its own terms. Sources and applicable notices are listed in [NOTICE.md](NOTICE.md); the Apache license does not replace them.

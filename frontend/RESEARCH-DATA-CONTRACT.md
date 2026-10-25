# Public research record contract, v2

Schema version: `evorph.public-research.v2`.

This document describes what the page is allowed to show and what a backend has to provide to show it. It is not evidence that any experiment ran or that any method works. As of this snapshot the research backend is not connected to a live provider, and every record the site can display is an example.

## Authoritative files

- **Fields and types**: [`research-contract.d.ts`](research-contract.d.ts). Every listed key must be present; use `null` or an empty array when there is no value. Extra fields are not allowed.
- **Executable checks**: [`dist/research-contract.js`](dist/research-contract.js). An unknown field, a mixed-in example, a result type the speaker may not claim, an inconsistent experiment, or a conflicting record rejects the whole page and shows a generic error.
- **Backend seam**: [`dist/research-adapter.js`](dist/research-adapter.js). Implement the two read-only methods returned by `createResearchAdapter()`; `connected` becomes true only when they are really served.
- **Rendering**: [`dist/research-view.js`](dist/research-view.js). It knows nothing about database columns, internal logs, model credentials, or service paths.
- **Local examples**: [`dist/examples/research-preview.js`](dist/examples/research-preview.js). A separate source, lazily imported only on a loopback host with `?research=preview`.

No HTTP address, route, or socket transport is prescribed here. A backend adapts whatever service actually exists; inventing an endpoint just to satisfy the page is not acceptable. Reads, evidence fetches, and playback must have no research-execution or model-call side effects.

## Read-only adapter

```js
{
  mode: 'live',
  connected: true,
  async listExperiments({ cursor, limit, signal }) { /* return ExperimentPage */ },
  async listRecords({ experimentId, cursor, afterSequence, limit, signal }) { /* return RecordPage */ }
}
```

Both responses carry `schema_version: 'evorph.public-research.v2'` and `mode`. Only this public version is accepted; older and unknown versions are rejected with no compatibility fallback. Other data formats, such as a local challenge draft, do not change with it. A real service returns `live`; examples return `example`. **`live` names the source of the public archive. It does not mean running in real time, reasoning right now, or verified as safe.**

`ExperimentPage` holds `experiments` and `next_cursor`. `RecordPage` holds the current `experiment`, `records`, and `next_cursor`. A cursor is an opaque server-side string; `null` means the visible range has no further page. When a next page exists it must carry records, and the cursor must advance.

The first read uses `cursor: null, afterSequence: null`. Later pages reuse the returned cursor with `afterSequence` still `null`. After history is exhausted, a visitor-initiated check for updates uses `cursor: null, afterSequence: <last sequence read>`, which returns only records added since; `afterSequence` is 0 when nothing has been read yet. The page currently asks for 8 records and 40 experiments per request, and the server must still honor `limit`. The protocol allows at most 100 records per page.

`spec_version` for an `experiment_id` is fixed once its first record is published; a changed specification means a new experiment identifier. Order inside an experiment comes from a unique `sequence` per `experiment_id`: positive integers, gaps allowed. A `record_id` is stable and immutable within its experiment. Pages return records in ascending `sequence`; the client also sorts by `sequence` and de-duplicates identical records. Changed content under an existing record identifier, or a different identifier claiming the same `sequence`, is a conflict. A revision appends new records and relations; it never quietly overwrites history. Key order in a JSON object is not a content change.

Reads respect the `AbortSignal`, and a stale response must not overwrite the experiment the visitor has since selected. This version has no automatic polling, subscription, or new research task. If live reading is added later, it keeps the same de-duplication, cancellation, permission, and cost boundaries.

## What a record says

Identity is `experiment_id / record_id / sequence`. `type` is one of contribution, hypothesis, public discussion, experiment action, finding, repair proposal, program verification, comparison, or version decision. `phase` is a repeatable classification, not a gate: a project may explore again, repair again, or end with no repair and no retention.

`speaker` separates SEEKER, KEEPER, the fixed verifier program, contributors, and the experiment system. The fixed verifier is an ordinary program, and the interface labels it FIXED VERIFIER. The system speaker records execution and archival events; it is not a third research role.

`title`, `summary`, and `public_text` are the short title, the public summary, and the full public body. `public_text` is a role's intentional public statement or a program observation that is safe to publish. It is never a reconstruction of private reasoning into a full trace, and a summary must not change what the statement meant. Text renders as plain text: no executed code, no HTML, no dynamic components. Long bodies expand in place, with a 50,000 character ceiling per record, 240 for a title, and 2,000 for a summary.

`source` is the provenance reference. A model statement uses `origin: model_public` and must carry `source_id` plus a fixed `model_config_id`. A program uses `program`, and a player submission uses `player_submission`. `source_id` points at a real published artifact, never at a personal identity or a credential. Examples use `example`, and a live page rejects example provenance. This field establishes traceability; it is not by itself a cryptographic proof of authenticity.

`assertion` is separate: `unverified`, `supported`, `refuted`, `inconclusive`, or `not_applicable`. Role and player statements may only claim `unverified` or `not_applicable`. A verified fact gets its own program result record, linked with `reply_to`. A question being asked does not make it well-posed, and a repair being proposed does not make it pass.

`reply_to` is an array of record identifiers and may reference several sources. An unloaded reference renders as not loaded yet and is located through later pages; if the full public archive genuinely lacks it, that is stated. No statement is invented to fill a missing reference. `test_ids` link to tests that are safe to publish and never to hidden acceptance inputs.

`candidate` holds `version_id` and `parent_version_id`, the latter nullable. A KEEPER repair record must carry a candidate. A parent may live in another archived experiment; the page shows its identifier and links to it only when it is present among the loaded records, rather than faking cross-experiment navigation.

`verification` appears only with `type: verification` and `speaker: verifier`. Outcomes are `passed`, `failed`, `error`, `unsupported`, or `inconclusive`; an execution error is not a successfully rejected attack. Its `test_ids` and `evidence_ids` must also appear in the same public record's test and evidence arrays.

`evidence` is `evidence_id`, `label`, `href`. Use `href: null` when there is no public download. Links may be HTTPS or site-relative public paths only; executable schemes, embedded credentials, protocol-relative addresses, and path traversal are excluded. The page offers a file entry point, does not execute evidence, and does not claim that a browser performed cryptographic verification.

`contributions` start from a submission record and link `contribution_id`, `submission_id`, a public pseudonymous `contributor_id`, `kind`, and `origin_record_id`. Discovery, reduction, re-verification, and regression reporting are recorded separately, and a model reduction never overwrites a player's provenance. A public identifier is not strong identity; real attribution checking happens server-side.

## Comparison and retention

`comparison` belongs only to a fixed-program comparison record. It must carry the old and new versions, the specification version, the test-set version, the fixed model configuration, `resource_conditions: { profile_id: string, matched: boolean }`, metrics, and a conclusion. `profile_id` is a non-empty public identifier in `PublicId` form: an opaque reference to resource conditions that encodes no amount and no private configuration. Metrics still carry identifier, name, unit, old value, new value, and direction of improvement.

On real data, `matched: true` may be set only by a backend that actually compared both sides under the same resource conditions. It is not a marker for "configuration supplied" or "match planned". `matched: false` requires the `incomparable` conclusion plus an explanation; `improved`, `regressed`, and `unchanged` are all rejected in that case, and so is a missing `resource_conditions`, `profile_id`, or boolean `matched`. `matched: true` does not force `improved`; `incomparable` remains available when other conditions differ or evidence is thin.

The page renders this as "Reported matching conditions" and states that it is not independently verified, because a backend report is what it is: format validation cannot prove that resource conditions matched. In the examples, both the matching flag and the numbers are authored illustrations, not the output of a comparison that ran. Real metrics come from real runs, and when comparison evidence is missing no magnitude of evolution is generated. The page never infers growth from version numbers, statement counts, or round counts.

This version removed the public `budget`, `api_limit_usd`, and `execution_limit` fields. The strict allowlist rejects those legacy fields and any other unknown field, with no compatibility fallback. Real budgets stay in private configuration and never enter public JSON, pages, repositories, prose, or evidence. The public description of cost control is that the system reduces duplicate calls and unnecessary computation, so compute serves effective research. It is not marketed as a low budget.

`decision` belongs only to a program or system decision record: `retained`, `rejected`, `superseded`, or `deferred`, always with a `reason` and `supporting_record_ids`. Retention links to the actual acceptance and comparison evidence, and rejection stays in the history. The page may say retained but not yet shown to keep improving; it may not present one pass as general safety.

## Public boundary

The server applies the public field allowlist before serializing, and reviews text and evidence files. JSON sent to a browser carries no private key, API credential, internal prompt, hidden acceptance input, personal profile, personal time zone or language preference, machine path, or internal build record. Hiding a field in the frontend does not protect information that was already sent. Public prose needs the same review: plain-text rendering prevents execution, not disclosure.

Do not forward whole database rows or raw model responses. Keep the required third-party attributions and public sources. If timestamps for experiments are added later, use a public standard time field with no personal environment attached; ordering already comes from `sequence`.

## Status display and local checking

The default `/` reports that no archive is connected. Examples appear only on a loopback host at `/?research=preview#research`, and every page, selected record, and comparison table is labeled as an example. Examples call no model, run no cryptographic experiment, and produce no real evidence file.

Preview state is driven by `sample_state=ready|disconnected|loading|empty|error|paused` and can also be chosen from the on-page local example controls. The four example experiments show a failed repair followed by another attempt, no finding and no improvement, a pause with an execution error, and an empty record set; their record counts differ on purpose.

Experiment state and data-read state are two independent dimensions. `experiment.state` remains one of `queued`, `running`, `completed`, `failed`, `paused`, unchanged in this contract and in the backend field. The page shows Data and Experiment separately: a loading or failed read does not turn an experiment into running or failed, the last loaded experiment state is kept and labeled as the last available state, and `Unavailable` is shown when no experiment metadata was ever received. An empty record set can coexist with `Queued`. In the local examples, `sample_state` drives the data state — including `refresh-error`, which loads records first and then fails the update read — while `experiment_state=queued|running|completed|failed|paused` drives the experiment state and falls back to each experiment's own value. Older links that set `sample_state=paused` keep working, but they now affect experiment state only.

Updates are manual by default. With "Follow latest" off, new records do not switch the visitor away from the current selection and do not reset the history scroll position; picking a historical record turns following off. Playback walks the loaded history and stops at the end — it fetches no next page, runs no experiment, and calls no model. Browsing, loading records, replaying, and downloading evidence are all read-only.

## Verification

`npm run check` parses every module the pages load. `npm test` verifies the contract, example isolation, pagination and de-duplication, immutable history, result roles, comparison conditions, and evidence URL restrictions. See [`../VERIFICATION.md`](../VERIFICATION.md) for the recorded run and for what was not executed. These tests check the public display and interface boundary; they do not verify post-quantum cryptography or model capability.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { previewRecords, createPreviewAdapter } from '../dist/examples/research-preview.js';
import { createResearchAdapter } from '../dist/research-adapter.js';
import { SCHEMA_VERSION, validateRecord, validateRecordPage, validateExperimentPage, mergeRecords, safePublicHref } from '../dist/research-contract.js';
import { renderResearchStatus } from '../dist/research-status.js';
const clone = value => structuredClone(value);
const record = type => clone(previewRecords.find(item => item.type === type));
const wrap = (records, experiment) => ({ schema_version: SCHEMA_VERSION, mode: 'example', experiment, records, next_cursor: null });
const statusNodes = () => ({ root: { dataset: {} }, dataStatus: { dataset: {} }, experimentStatus: { dataset: {} }, context: {} });

test('all five published experiment states have distinct visible labels while data is loaded', () => {
  const labels = [];
  for (const [state, label] of [['queued', 'Queued'], ['running', 'Running'], ['completed', 'Completed'], ['failed', 'Failed'], ['paused', 'Paused']]) {
    const nodes = statusNodes(); renderResearchStatus(nodes, { readState: 'ready', experiment: { state } });
    assert.equal(nodes.experimentStatus.textContent, label); assert.equal(nodes.root.dataset.experimentState, state);
    assert.equal(nodes.dataStatus.textContent, 'Loaded'); labels.push(nodes.experimentStatus.textContent);
  }
  assert.equal(new Set(labels).size, 5);
});
test('loading and read failure preserve the last published experiment status without changing its outcome', () => {
  for (const state of ['queued', 'running', 'completed', 'failed', 'paused']) {
    const nodes = statusNodes(), experiment = { state };
    renderResearchStatus(nodes, { readState: 'ready', experiment }); const label = nodes.experimentStatus.textContent;
    for (const readState of ['loading', 'error']) {
      renderResearchStatus(nodes, { readState, experiment });
      assert.equal(nodes.experimentStatus.textContent, label); assert.equal(experiment.state, state);
      assert.equal(nodes.dataStatus.textContent, readState === 'error' ? 'Read failed' : 'Loading');
      assert.equal(nodes.context.textContent, '(last available state)');
    }
    renderResearchStatus(nodes, { readState: 'empty', experiment }); assert.equal(nodes.experimentStatus.textContent, label); assert.equal(nodes.context.textContent, '');
  }
});
test('no selected or available experiment never becomes a queued, completed or failed experiment', () => {
  for (const readState of ['disconnected', 'loading', 'empty', 'error']) {
    const nodes = statusNodes(); renderResearchStatus(nodes, { readState });
    assert.equal(nodes.experimentStatus.textContent, 'Unavailable'); assert.equal(nodes.root.dataset.experimentState, 'unknown'); assert.equal(nodes.context.textContent, '');
  }
});
test('independent example status controls agree across list and record reads, including a failed refresh', async () => {
  for (const state of ['queued', 'running', 'completed', 'failed', 'paused']) {
    const adapter = createPreviewAdapter('ready', { experimentState: state });
    const archive = validateExperimentPage(await adapter.listExperiments(), 'example');
    const page = validateRecordPage(await adapter.listRecords({ experimentId: archive.experiments[0].experiment_id }), archive.experiments[0].experiment_id, 'example');
    assert.equal(archive.experiments[0].state, state); assert.equal(page.experiment.state, state);
  }
  const adapter = createPreviewAdapter('refresh-error', { experimentState: 'failed' });
  const page = await adapter.listRecords({ experimentId: 'sample-retired-key' });
  await assert.rejects(adapter.listRecords({ experimentId: 'sample-retired-key', afterSequence: 14 }), /refresh failure/);
  const nodes = statusNodes(); renderResearchStatus(nodes, { readState: 'error', experiment: page.experiment });
  assert.equal(nodes.experimentStatus.textContent, 'Failed'); assert.equal(nodes.dataStatus.textContent, 'Read failed');
});

test('default adapter is disconnected and never assumes an HTTP endpoint', async () => {
  const adapter = createResearchAdapter(); assert.equal(adapter.connected, false); assert.equal(adapter.mode, 'live');
  await assert.rejects(adapter.listExperiments(), /not connected/); await assert.rejects(adapter.listRecords(), /not connected/);
});
test('all fixture records have explicit example provenance and variable research phases', () => {
  for (const item of previewRecords) { validateRecord(item); assert.equal(item.source.origin, 'example'); }
  const first = previewRecords.filter(item => item.experiment_id === 'sample-retired-key');
  assert.ok(first.length > 6); assert.ok(first.filter(item => item.phase === 'explore').length > 1);
  assert.ok(first.some(item => item.decision?.outcome === 'rejected')); assert.ok(first.some(item => item.decision?.outcome === 'retained'));
});
test('pagination yields complete history while overlap is deduplicated', async () => {
  const adapter = createPreviewAdapter(), archive = validateExperimentPage(await adapter.listExperiments(), 'example');
  assert.equal(archive.experiments.length, 4);
  let cursor = null, history = [];
  do {
    const page = validateRecordPage(await adapter.listRecords({ experimentId: 'sample-retired-key', cursor, limit: 3 }), 'sample-retired-key', 'example');
    history = mergeRecords(history, page.records); cursor = page.next_cursor;
  } while (cursor !== null);
  assert.equal(history.length, 14); assert.equal(mergeRecords(history, history.slice(-3)).length, 14);
  const update = validateRecordPage(await adapter.listRecords({ experimentId: 'sample-retired-key', afterSequence: 14 }), 'sample-retired-key', 'example');
  assert.equal(update.records[0].sequence, 15); assert.equal(mergeRecords(history, update.records).length, 15);
});
test('empty and paused experiments are legitimate data states', async () => {
  const adapter = createPreviewAdapter();
  const empty = validateRecordPage(await adapter.listRecords({ experimentId: 'sample-empty' }), 'sample-empty', 'example'); assert.deepEqual(empty.records, []);
  const paused = validateRecordPage(await adapter.listRecords({ experimentId: 'sample-paused' }), 'sample-paused', 'example'); assert.equal(paused.experiment.state, 'paused');
  assert.ok(paused.records.some(item => item.verification?.outcome === 'error'));
});
test('statements cannot be relabeled as fixed verifier results', () => {
  const item = record('verification'); item.speaker = 'seeker'; assert.throws(() => validateRecord(item));
  const claim = record('hypothesis'); claim.assertion = 'supported'; assert.throws(() => validateRecord(claim));
  const comparison = record('comparison'); comparison.speaker = 'keeper'; assert.throws(() => validateRecord(comparison));
});
test('live pages reject example records and example pages reject live records', async () => {
  const adapter = createPreviewAdapter(), page = await adapter.listRecords({ experimentId: 'sample-retired-key' });
  assert.throws(() => validateRecordPage(page, 'sample-retired-key', 'live'));
  page.mode = 'live'; assert.throws(() => validateRecordPage(page, 'sample-retired-key', 'live'));
  const item = record('hypothesis'); item.source = { origin: 'model_public', source_id: 'public-message-1', model_config_id: 'frozen-model-1' };
  validateRecord(item); assert.throws(() => validateRecordPage(wrap([item], page.experiment), 'sample-retired-key', 'example'));
});
test('live role provenance requires a model configuration and a public source record', () => {
  const item = record('hypothesis'); item.source = { origin: 'program', source_id: 'source-1', model_config_id: null }; assert.throws(() => validateRecord(item));
  item.source = { origin: 'model_public', source_id: 'source-1', model_config_id: null }; assert.throws(() => validateRecord(item));
});
test('unknown/private fields and mismatched experiment IDs fail closed', () => {
  const item = record('hypothesis'); item.private_key = 'must-never-arrive'; assert.throws(() => validateRecord(item));
  delete item.private_key; item.source.host_path = 'must-never-arrive'; assert.throws(() => validateRecord(item));
  delete item.source.host_path; assert.throws(() => validateRecord(item, 'another-experiment'));
});
test('immutable records accept reordered JSON keys but reject changed content or colliding sequence numbers', () => {
  const item = record('hypothesis'), reordered = Object.fromEntries(Object.entries(item).reverse()); assert.equal(mergeRecords([item], [reordered]).length, 1);
  const changed = clone(item); changed.summary = 'Changed history'; assert.throws(() => mergeRecords([item], [changed]));
  const collision = clone(item); collision.record_id = 'another-record'; assert.throws(() => mergeRecords([item], [collision]));
  const otherExperiment = clone(item); otherExperiment.experiment_id = 'another-experiment'; assert.throws(() => mergeRecords([item], [otherExperiment]));
});
test('comparison requires explicit conditions and a valid conclusion', () => {
  const item = record('comparison'); delete item.comparison.resource_conditions; assert.throws(() => validateRecord(item));
  for (const key of ['baseline_version_id', 'candidate_version_id', 'spec_version', 'test_set_version', 'model_config_id']) {
    const missing = record('comparison'); delete missing.comparison[key]; assert.throws(() => validateRecord(missing), key);
  }
  const valid = record('comparison'); valid.comparison.metrics = []; assert.throws(() => validateRecord(valid));
  valid.comparison.conclusion = 'incomparable'; validateRecord(valid);
  assert.ok(previewRecords.some(item => item.comparison?.conclusion === 'unchanged'));
});
test('public envelopes require schema v2 and reject old versions without fallback', async () => {
  assert.equal(SCHEMA_VERSION, 'evorph.public-research.v2');
  const adapter = createPreviewAdapter(), archive = await adapter.listExperiments();
  const page = await adapter.listRecords({ experimentId: 'sample-retired-key' });
  validateExperimentPage(archive, 'example'); validateRecordPage(page, 'sample-retired-key', 'example');
  for (const version of ['evorph.public-research.v1', 'evorph.public-research.v3', undefined]) {
    assert.throws(() => validateExperimentPage({ ...archive, schema_version: version }, 'example'));
    assert.throws(() => validateRecordPage({ ...page, schema_version: version }, 'sample-retired-key', 'example'));
  }
});
test('legacy budget and amount fields are rejected even alongside valid resource conditions', () => {
  // Synthetic invalid inputs, never actual operating budgets or public fixtures.
  const legacy = { api_limit_usd: 1, execution_limit: 1 };
  const old = record('comparison'); delete old.comparison.resource_conditions; old.comparison.budget = legacy;
  assert.throws(() => validateRecord(old));
  for (const [key, value] of Object.entries({ budget: legacy, ...legacy })) {
    for (const select of [item => item, item => item.comparison, item => item.comparison.resource_conditions, item => item.comparison.metrics[0], item => item.source]) {
      const item = record('comparison'); select(item)[key] = value; assert.throws(() => validateRecord(item), key);
    }
  }
});
test('unmatched resource conditions require incomparable, never improved, regressed or unchanged', () => {
  for (const conclusion of ['improved', 'regressed', 'unchanged', 'incomparable']) {
    const item = record('comparison'); item.comparison.resource_conditions.matched = false; item.comparison.conclusion = conclusion;
    if (conclusion === 'incomparable') validateRecord(item);
    else assert.throws(() => validateRecord(item), conclusion);
  }
  for (const conclusion of ['improved', 'regressed', 'unchanged', 'incomparable']) {
    const item = record('comparison'); item.comparison.conclusion = conclusion; validateRecord(item);
  }
});
test('resource matching requires a valid profile ID and a strict boolean', () => {
  for (const matched of [true, false]) {
    for (const profileId of [undefined, null, '', ' ', 42, 'private/config', 'x'.repeat(121)]) {
      const item = record('comparison'); item.comparison.conclusion = 'incomparable'; item.comparison.resource_conditions = { profile_id: profileId, matched };
      assert.throws(() => validateRecord(item));
    }
    const missing = record('comparison'); missing.comparison.conclusion = 'incomparable'; missing.comparison.resource_conditions = { matched };
    assert.throws(() => validateRecord(missing));
  }
  for (const matched of [undefined, null, 'true', 'false', 0, 1]) {
    const item = record('comparison'); item.comparison.conclusion = 'incomparable'; item.comparison.resource_conditions.matched = matched;
    assert.throws(() => validateRecord(item));
  }
  const missing = record('comparison'); delete missing.comparison.resource_conditions.matched; assert.throws(() => validateRecord(missing));
});
test('public page and examples omit amounts and describe matching as reported, not certified', async () => {
  const [docs, view, fixture] = await Promise.all(['docs.html', 'research-view.js', 'examples/research-preview.js'].map(path => readFile(new URL(`../dist/${path}`, import.meta.url), 'utf8')));
  const amount = /(?:US?\$|[$€£¥]|\b(?:USD|EUR|GBP|CNY)\b)\s*\d|\d\s*\b(?:USD|EUR|GBP|CNY)\b/i;
  for (const text of [docs, view, fixture, JSON.stringify(previewRecords)]) assert.doesNotMatch(text, amount);
  for (const text of [view, fixture, JSON.stringify(previewRecords)]) assert.doesNotMatch(text, /\bbudget\b|api_limit_usd|execution_limit/);
  assert.match(view, /Reported matching conditions/); assert.match(view, /not independently verified/);
  assert.match(view, /Not matched — not comparable/); assert.doesNotMatch(view, /Comparable version record/);
  assert.ok(docs.includes('reduce duplicate calls and unnecessary computation, so compute serves effective research'), 'Public copy explains useful research allocation.');
  for (const item of previewRecords.filter(item => item.comparison)) {
    assert.deepEqual(Object.keys(item.comparison.resource_conditions).sort(), ['matched', 'profile_id']); validateRecord(item);
  }
});
test('schema declarations and changed import chain use the new public release without resetting status assets', async () => {
  const files = ['research-contract.d.ts', 'RESEARCH-DATA-CONTRACT.md', 'dist/index.html', 'dist/app.js', 'dist/research-view.js', 'dist/examples/research-preview.js'];
  const [types, contract, index, app, view, fixture] = await Promise.all(files.map(path => readFile(new URL(`../${path}`, import.meta.url), 'utf8')));
  for (const text of [types, contract]) { assert.ok(text.includes(SCHEMA_VERSION)); assert.doesNotMatch(text, /evorph\.public-research\.v1/); }
  assert.match(index, /app\.js\?v=m14-1/); assert.match(app, /research-view\.js\?v=m14-1/);
  assert.match(view, /research-preview\.js\?v=public-research-v2-1/);
  for (const text of [view, fixture]) assert.match(text, /research-contract\.js\?v=public-research-v2-1/);
  assert.match(view, /renderResearchStatus/); assert.match(index, /research\.css\?v=experiment-status-1/);
});
test('all public footers show the supplied contract address in a copy button', async () => {
  const address = '0xbe2ebdf265d02ba3ee07743b821a52c030ab0aab';
  for (const page of ['index.html', 'docs.html', 'brand.html']) {
    const html = await readFile(new URL(`../dist/${page}`, import.meta.url), 'utf8');
    assert.ok(html.includes(`aria-label="Copy contract address" title="Copy contract address">${address}</button>`));
    assert.ok(html.includes('style.css?v=m14-1'));
    assert.doesNotMatch(html, /Contract address reserved/);
  }
});
test('verification evidence and tests must be included in the same public record', () => {
  const item = record('verification'); item.verification.evidence_ids.push('unlisted-evidence'); assert.throws(() => validateRecord(item));
  const missingTest = record('verification'); missingTest.test_ids = []; assert.throws(() => validateRecord(missingTest));
});
test('public notes are plain text with bounded length', () => {
  const item = record('discussion'); item.public_text = '<script>untrusted()</script>\n**literal markup**'; validateRecord(item);
  item.public_text = 'x'.repeat(50001); assert.throws(() => validateRecord(item));
});
test('evidence URLs exclude executable schemes, credentials, network-path URLs and traversal', () => {
  for (const href of ['javascript:alert(1)', 'data:text/html,unsafe', 'file:///private/file', '//external.test/file', 'https://name:password@example.test/file', '../private/file', './%2e%2e/private/file', 'https://example.test/%2e%2e/private', 'https://example.test/%0aunsafe']) assert.equal(safePublicHref(href), null, href);
  assert.equal(safePublicHref('./data/report.json', 'http://127.0.0.1:4174'), 'http://127.0.0.1:4174/data/report.json');
  assert.equal(safePublicHref('https://example.test/public.json'), 'https://example.test/public.json');
});

test('asset credits and archive provenance match the shipped materials', async () => {
  const [credits, index, style, docs] = await Promise.all(['credits.txt', 'index.html', 'style.css', 'docs.html'].map(path => readFile(new URL(`../dist/${path}`, import.meta.url), 'utf8')));
  assert.match(index, /assets\/research\/quantum-hardware\.jpg/);
  assert.match(credits, /Advanced Quantum Testbed.*Berkeley Lab/);
  assert.match(credits, /National Quantum Initiative/);
  assert.doesNotMatch(credits, /No external raster|on2026|Syne/);
  for (const license of ['assets/fonts/sora-OFL.txt', 'assets/fonts/ibmplexmono-OFL.txt', 'assets/vendor/THREE-LICENSE.txt']) {
    assert.ok(credits.includes(license));
    assert.ok((await readFile(new URL(`../dist/${license}`, import.meta.url), 'utf8')).length > 0);
  }
  assert.match(style, /sora-variable\.ttf/);
  assert.match(style, /ibm-plex-mono-400\.ttf/);
  assert.doesNotMatch(style, /syne/i);
  assert.ok(docs.includes('public test vectors with upstream notices, implementation contracts, and recorded verification runs'));
  assert.doesNotMatch(docs, /archive contains the project outline/);
});

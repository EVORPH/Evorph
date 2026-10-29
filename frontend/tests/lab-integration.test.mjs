import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { createPreviewAdapter, previewRecords } from '../dist/examples/research-preview.js';
import { createArchiveReader, ArchiveReadError } from '../dist/lab/data.mjs';

const read = path => readFile(new URL(`../dist/${path}`, import.meta.url), 'utf8');
const jsonResponse = (value, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
});

const reference = await createPreviewAdapter('ready');
const listPage = await reference.listExperiments();
const experiment = listPage.experiments[0];
const recordPage = await reference.listRecords({ experimentId: experiment.experiment_id });
const allRecords = recordPage.records;
assert.ok(allRecords.length >= 2, 'the fixture archive must supply several records');

function fetcher(routes) {
  const calls = [];
  return {
    calls,
    async fetch(url, init) {
      calls.push({ url: String(url), method: init?.method || 'GET' });
      const path = String(url).split('?')[0];
      const route = routes[path];
      if (!route) return jsonResponse({ error: 'unknown_path' }, 404);
      return route(new URL(String(url), 'http://127.0.0.1'));
    },
  };
}

const statusRoute = () => jsonResponse({ mode: 'example', connected: true });

test('the archive reader follows every experiment and record cursor to its end', async () => {
  const half = Math.floor(allRecords.length / 2);
  const client = fetcher({
    '/api/research/status': statusRoute,
    '/api/research/experiments': url => url.searchParams.get('cursor') === 'list-2'
      ? jsonResponse({ ...listPage, next_cursor: null })
      : jsonResponse({ ...listPage, experiments: [], next_cursor: 'list-2' }),
    [`/api/research/experiments/${experiment.experiment_id}/records`]: url => url.searchParams.get('cursor') === 'records-2'
      ? jsonResponse({ ...recordPage, records: allRecords.slice(half), next_cursor: null })
      : jsonResponse({ ...recordPage, records: allRecords.slice(0, half), next_cursor: 'records-2' }),
  });
  const reader = createArchiveReader({ fetcher: client.fetch });
  const status = await reader.readStatus();
  assert.equal(status.mode, 'example');
  const listed = await reader.listExperiments(status.mode);
  assert.equal(listed.pages, 2);
  assert.equal(listed.experiments.length, listPage.experiments.length);
  const bundle = await reader.readRecords(experiment.experiment_id, status.mode);
  assert.equal(bundle.pages, 2);
  assert.equal(bundle.records.length, allRecords.length);
  assert.deepEqual(bundle.records.map(item => item.sequence), [...allRecords].sort((a, b) => a.sequence - b.sequence).map(item => item.sequence));
  assert.equal(bundle.experiment.experiment_id, experiment.experiment_id);
  assert.ok(client.calls.every(call => call.method === 'GET'), 'the reader only issues reads');
  const paged = client.calls.filter(call => call.url.includes('/experiments'));
  assert.ok(paged.length >= 3, 'both paginated endpoints were read');
  assert.ok(paged.every(call => call.url.includes('limit=100')), 'every paginated read states the page bound');
});

test('a repeated cursor, a wrong mode and a duplicate listing are refused rather than trusted', async () => {
  const loop = createArchiveReader({ fetcher: fetcher({
    '/api/research/status': statusRoute,
    '/api/research/experiments': url => jsonResponse({ ...listPage, experiments: [], next_cursor: url.searchParams.get('cursor') ? 'same' : 'same' }),
  }).fetch });
  await assert.rejects(loop.listExperiments('example'), ArchiveReadError);

  const wrongMode = createArchiveReader({ fetcher: fetcher({
    '/api/research/status': statusRoute,
    '/api/research/experiments': () => jsonResponse({ ...listPage, mode: 'live' }),
  }).fetch });
  await assert.rejects(wrongMode.listExperiments('example'), ArchiveReadError);

  const duplicated = createArchiveReader({ fetcher: fetcher({
    '/api/research/status': statusRoute,
    '/api/research/experiments': () => jsonResponse({ ...listPage, experiments: [experiment, { ...experiment }], next_cursor: null }),
  }).fetch });
  await assert.rejects(duplicated.listExperiments('example'), /twice|public data contract/);
});

test('transport, HTTP and payload failures are reported as archive read errors', async () => {
  const status = { mode: 'example', connected: true };
  const cases = [
    [() => jsonResponse({ error: 'unavailable' }, 503), /503/],
    [() => new Response('not json', { status: 200, headers: { 'Content-Type': 'text/html' } }), /non-JSON/],
    [() => { throw new TypeError('network down'); }, /could not be reached/],
  ];
  for (const [route, pattern] of cases) {
    const reader = createArchiveReader({ fetcher: fetcher({ '/api/research/status': route }).fetch });
    await assert.rejects(reader.readStatus(), pattern);
  }
  const disconnected = createArchiveReader({ fetcher: fetcher({ '/api/research/status': () => jsonResponse({ ...status, connected: false }) }).fetch });
  await assert.rejects(disconnected.readStatus(), /connected public mode/);
  const truncated = createArchiveReader({ fetcher: fetcher({
    '/api/research/status': () => jsonResponse(status),
    '/api/research/experiments': () => jsonResponse({ ...listPage, experiments: [], next_cursor: 'more' }),
  }).fetch });
  await assert.rejects(truncated.listExperiments('example'), ArchiveReadError);
});

test('the workspace page is wired into the site and keeps its own module scope', async () => {
  const page = await read('lab/index.html');
  const app = await read('lab/app.mjs');
  const data = await read('lab/data.mjs');
  assert.match(page, /<script type="module" src="\.\/app\.mjs\?v=lab-1">/);
  assert.match(page, /href="\.\/style\.css\?v=lab-1"/);
  assert.match(page, /<html lang="en">/);
  assert.match(page, /href="\.\.\/"/);
  for (const view of ['lab', 'success', 'failures', 'provenance']) {
    assert.ok(page.includes(`href="?view=${view}"`), `navigation keeps the ${view} view`);
  }
  assert.doesNotMatch(page, /href="\/(success|failures|provenance)"/);
  assert.match(page, /id="source-state"/);
  assert.doesNotMatch(page + app + data, /snapshot\.json|localStorage|sessionStorage|navigator\.language|Intl\.DateTimeFormat|document\.cookie/);
  assert.doesNotMatch(app + data, /\/api\/identities|\/api\/challenges|method:\s*'POST'/);
  assert.match(app, /createArchiveReader\(\)/);
  assert.match(app, /Partial history\./);
  assert.match(data, /validateExperimentPage/);
  assert.match(data, /validateRecordPage/);
  assert.match(data, /mergeRecords/);
});

test('all three existing pages link to the workspace without losing their own navigation', async () => {
  for (const name of ['index.html', 'docs.html', 'brand.html']) {
    const page = await read(name);
    assert.ok(page.includes('href="lab/">Observation room'), `${name} links the workspace`);
    assert.ok(page.includes('aria-label="Footer navigation"'), `${name} keeps its footer navigation`);
  }
  const index = await read('index.html');
  assert.match(index, /desktop-nav[^>]*>\s*<a[^>]*href="lab\/"[^>]*>Observation room<\/a>/);
  assert.match(index, /href="chapter2\/">The Key Registry<\/a>/);
  assert.match(index, /id="mobile-menu"[^>]*>(?:(?!<\/nav>).)*lab\//s);
  assert.match(index, /app\.js\?v=m14-1/);
});

test('the workspace reuses the reviewed classification and UTC rules without forking them', async () => {
  const model = await read('lab/model.mjs');
  const time = await read('lab/time.mjs');
  const reviewed = {
    'lab/model.mjs': '257be260fb7fca6ef9328a9267f0ed1fbf93b1de51f1ee475d9136e34e359c66',
    'lab/time.mjs': '879124832a4619c1e0ba863b297da57e11b8464a17e47b290a7077c6dc56ad5f',
  };
  assert.equal(createHash('sha256').update(model).digest('hex'), reviewed['lab/model.mjs']);
  assert.equal(createHash('sha256').update(time).digest('hex'), reviewed['lab/time.mjs']);
  assert.match(model, /category: 'success'/);
  assert.match(model, /completed alone is insufficient|experiment\.state !== 'completed'/);
  assert.match(time, /UTC/);
});

test('evidence links stay on the archive and missing links stay missing', async () => {
  const { safeEvidenceHref } = await import('../dist/lab/model.mjs');
  const supplied = allRecords.flatMap(item => item.evidence || []).filter(entry => entry.href);
  if (supplied.length) {
    assert.equal(safeEvidenceHref(supplied[0].href), `https://evorph.net${supplied[0].href}`);
  }
  for (const value of [null, '', 'https://example.test/pack', '//example.test/pack', '/api/identities', '/../secret', 'javascript:alert(1)']) {
    assert.equal(safeEvidenceHref(value), null);
  }
  assert.ok(previewRecords.some(item => (item.evidence || []).length >= 0));
});

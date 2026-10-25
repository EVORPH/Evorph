import test from 'node:test';
import assert from 'node:assert/strict';
import { createServiceClient, ServiceError } from '../dist/service-client.js';
import { createResearchAdapter } from '../dist/research-adapter.js';

const json = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
const url = 'https://localhost/?research=service';

test('a plain static page stays disconnected without sending requests', async () => {
  const client = createServiceClient({ page: new URL('http://localhost/'), fetcher: () => { throw Error('unexpected request'); } });
  assert.equal(await client.connect(), false);
  await assert.rejects(client.createIdentity(), ServiceError);
});

test('example archives require explicit local service mode', async () => {
  for (const page of ['https://localhost/', 'https://research.invalid/?research=service']) {
    const client = createServiceClient({ page: new URL(page), fetcher: async () => json({ mode: 'example', connected: true }) });
    assert.equal(await client.connect(), false);
  }
  const client = createServiceClient({ page: new URL(url), fetcher: async () => json({ mode: 'example', connected: true }) });
  assert.equal(await client.connect(), true);
  assert.equal(client.mode, 'example');
});

test('read adapters preserve pagination and never write', async () => {
  const sent = [];
  const client = createServiceClient({ page: new URL(url), fetcher: async (path, options) => {
    sent.push({ path, options }); return json(path.endsWith('/status') ? { mode: 'example', connected: true } : { records: [] });
  } });
  await client.connect();
  const adapter = createResearchAdapter(client);
  await adapter.listRecords({ experimentId: 'experiment-one', cursor: 'signed.cursor', limit: 8 });
  assert.equal(sent[1].path, '/api/research/experiments/experiment-one/records?cursor=signed.cursor&limit=8');
  assert.equal(sent[1].options.method, 'GET');
  assert.equal(sent[1].options.credentials, 'same-origin');
  assert.equal(sent[1].options.redirect, 'error');
});

test('a refreshed tab sends CSRF as a header and session proof only as a cookie', async () => {
  const proof = 'a'.repeat(43), sent = [];
  const client = createServiceClient({ page: new URL(url), cookies: () => 'evorph_csrf=' + proof,
    fetcher: async (path, options) => { sent.push({ path, options }); return json(path.endsWith('/status') ?
      { mode: 'example', connected: true } : { submission_id: 'submission-one', state: 'queued' }); } });
  await client.connect();
  await client.submit({ request_key: 'stable-request', target_version_id: 'version-one', challenge: {} });
  assert.equal(sent[1].options.headers['X-CSRF-Token'], proof);
  assert.equal(sent[1].options.headers.Authorization, undefined);
  assert.ok(!sent[1].options.body.includes(proof));
});

test('service failures remain generic and do not expose response text', async () => {
  let connected = false;
  const client = createServiceClient({ page: new URL(url), fetcher: async () => {
    if (!connected) { connected = true; return json({ mode: 'example', connected: true }); }
    return new Response('synthetic-private-error', { status: 429 });
  } });
  await client.connect();
  await assert.rejects(client.session(), error => error.status === 429 && !error.message.includes('private'));
});

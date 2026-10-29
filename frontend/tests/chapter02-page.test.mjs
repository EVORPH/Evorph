import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const page = await readFile(new URL('../dist/chapter2/index.html', import.meta.url), 'utf8');

const payload = () => {
  const start = page.indexOf('<script type="application/json" id="data">');
  assert.notEqual(start, -1, 'the page must carry its measured payload');
  const body = page.slice(start, page.indexOf('</script>', start));
  return JSON.parse(body.slice(body.indexOf('>') + 1));
};

const script = () => {
  const blocks = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]);
  const app = blocks.find(code => code.includes('const data'));
  assert.ok(app, 'the page must define its render script');
  return app;
};

test('the render script parses', () => {
  new vm.Script(script());
});

test('every row carries a request, a verdict and both states', () => {
  for (const row of payload().rows) {
    for (const key of ['epoch', 'validator_index', 'event', 'label', 'request', 'accepted', 'before', 'after', 'state_changed']) {
      assert.ok(key in row, `row is missing ${key}`);
    }
    assert.equal(typeof row.accepted, 'boolean');
    assert.equal(row.accepted, row.state_changed, 'only an accepted event may change state');
    if (!row.accepted) {
      assert.equal(row.before.last_change_epoch, row.after.last_change_epoch, 'a refusal must not move the epoch');
      assert.equal(row.before.xmss_public_key, row.after.xmss_public_key, 'a refusal must not swap the key');
    }
  }
});

test('the header totals match the rows', () => {
  const data = payload();
  assert.equal(data.rows.length, data.totals.events);
  assert.equal(data.rows.filter(row => row.accepted).length, data.totals.accepted);
  assert.equal(data.rows.filter(row => !row.accepted).length, data.totals.rejected);
});

test('the page states its evidence tier and a UTC snapshot', () => {
  const data = payload();
  assert.ok(data.evidence_tier.length > 0);
  assert.match(data.generated_at_utc, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$/);
});

test('no local machine paths or operator identifiers reach the page', () => {
  assert.doesNotMatch(page, /\/Users\/|\/home\/|\.local\b|localhost|127\.0\.0\.1/);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { autoUpdateSeconds, createAutoUpdater } from '../dist/research-auto-update.js';
import { researchAutoUpdateSeconds } from '../dist/site-config.js';

function harness({ seconds, canRead }) {
  const state = { scheduled: 0, cancelled: 0, reads: 0, timer: null };
  let fire = null;
  const auto = createAutoUpdater({
    seconds, canRead, read: () => { state.reads += 1; },
    schedule: (callback, ms) => { state.scheduled += 1; state.delay = ms; fire = callback; return state.timer = { ms }; },
    cancel: timer => { state.cancelled += 1; assert.equal(timer, state.timer); state.timer = null; },
  });
  return { auto, state, tick: () => fire() };
}

test('the published cadence is configured on purpose and stays sane', () => {
  assert.equal(researchAutoUpdateSeconds, 60);
  for (const value of [0, undefined, null, -1, 14, 15.5, 3601, '15', NaN]) assert.equal(autoUpdateSeconds(value), 0, String(value));
  for (const value of [15, 60, 3600]) assert.equal(autoUpdateSeconds(value), value);
});

test('a local cadence override never reaches a published origin', () => {
  const search = '?auto_update_seconds=20';
  assert.equal(autoUpdateSeconds(0, { loopback: true, search }), 20);
  assert.equal(autoUpdateSeconds(0, { search }), 0);
  assert.equal(autoUpdateSeconds(0, { loopback: true }), 0);
  for (const override of ['4', '99999', '15.5', 'soon']) assert.equal(autoUpdateSeconds(60, { loopback: true, search: '?auto_update_seconds=' + override }), 0, override);
  assert.equal(autoUpdateSeconds(60, { loopback: true, search: '?auto_update_seconds=30' }), 30);
});

test('no cadence means no timer even while the view is readable', () => {
  const { auto, state } = harness({ seconds: 0, canRead: () => true });
  auto.notify(); auto.notify();
  assert.deepEqual([state.scheduled, state.cancelled, auto.running], [0, 0, false]);
});

test('reading is scheduled only while the section is visible and idle', () => {
  let allowed = false;
  const { auto, state, tick } = harness({ seconds: 15, canRead: () => allowed });
  auto.notify();
  assert.equal(auto.running, false);
  allowed = true; auto.notify();
  assert.deepEqual([auto.running, state.scheduled, state.delay], [true, 1, 15000]);
  auto.notify();
  assert.equal(state.scheduled, 1);
  tick();
  assert.equal(state.reads, 1);
  allowed = false; tick();
  assert.equal(state.reads, 1);
  auto.notify();
  assert.deepEqual([auto.running, state.cancelled], [false, 1]);
  allowed = true; auto.notify(); tick();
  assert.equal(state.reads, 2);
  auto.stop(); auto.stop();
  assert.deepEqual([auto.running, state.cancelled], [false, 2]);
});

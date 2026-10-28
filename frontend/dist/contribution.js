import { announce } from './common.js?v=m14-1';

const scenarios = ['delayed-old-request', 'invalid-pq-fallback', 'replayed-request'];
const pendingKey = 'evorph.pending-submission.v1';
const lastKey = 'evorph.last-submission.v1';
const $ = id => document.getElementById(id);
const publicId = value => typeof value === 'string' && /^[a-zA-Z0-9][A-Za-z0-9._:-]{0,119}$/.test(value);

export function mountContribution(client) {
  let identity = null, config = null, busy = false, targetRequest = null, pending = null, lastId = null;
  try {
    pending = JSON.parse(sessionStorage.getItem(pendingKey) || 'null');
    const last = sessionStorage.getItem(lastKey); if (publicId(last)) lastId = last;
  } catch { /* Storage is optional; credentials never use it. */ }
  function message(text) { $('participant-status').textContent = text; announce(text); }
  function render() {
    $('participant-controls').hidden = !client.connected;
    $('composer-mode').textContent = client.connected ? client.mode === 'example' ? 'LOCAL SERVICE TEST' : 'RESEARCH SUBMISSION' : 'NOT CONNECTED';
    $('challenge-action').textContent = config && client.mode === 'example' ? 'Submit test challenge' : 'Submit challenge';
    $('challenge-submit').disabled = busy || !config || !identity;
    $('create-contributor').disabled = busy || Boolean(identity);
    $('restore-contributor').disabled = busy;
    $('check-submission').disabled = busy || !lastId || !identity;
    $('participant-identity').textContent = identity ? 'Contributor ' + identity : 'No contributor session';
    $('contribution-note').textContent = config ? 'Only the selected boundary sequence is submitted.' : 'Select a published experiment above to submit its boundary sequence.';
  }
  function failure(error) {
    if (error.status === 401) { identity = null; message('Session unavailable. Restore your contributor identity.'); }
    else if (error.status === 429) message('Request limit reached. Try again after this window.');
    else if (error.status === 400 || error.status === 409) message('The request was not accepted. Check the selected experiment or recovery key.');
    else message('Service unavailable. Retrying the same submission reuses its request ID.');
  }
  async function action(work) {
    if (busy) return;
    busy = true; render();
    try { await work(); } catch (error) { failure(error); }
    finally { busy = false; render(); }
  }
  function showIdentity(issued) {
    identity = issued.contributor_id;
    $('new-recovery').value = issued.recovery;
    $('new-recovery-panel').hidden = false;
    $('recovery-key').value = '';
    message('Identity ready. Save the recovery key privately; recovery replaces the previous key.');
  }
  $('create-contributor').addEventListener('click', () => action(async () => showIdentity(await client.createIdentity())));
  $('restore-contributor').addEventListener('click', () => action(async () => {
    const key = $('recovery-key').value.trim();
    if (!/^[a-zA-Z0-9_-]{43}$/.test(key)) { message('Enter the complete recovery key.'); return; }
    showIdentity(await client.recoverIdentity(key));
  }));
  $('show-recovery').addEventListener('click', () => {
    const hidden = $('new-recovery').type === 'password';
    $('new-recovery').type = hidden ? 'text' : 'password';
    $('show-recovery').textContent = hidden ? 'Hide key' : 'Show key';
  });
  $('challenge-form').addEventListener('submit', event => {
    event.preventDefault();
    action(async () => {
      const scenario = scenarios[Number($('challenge-scenario').value)];
      if (!scenario) return;
      if (!config) { message('Select a published experiment first.'); return; }
      const current = await client.session();
      if (!current.authenticated || !publicId(current.contributor_id)) throw Object.assign(new Error(), { status: 401 });
      identity = current.contributor_id;
      const template = config.scenarios.find(item => item.scenario_id === scenario);
      if (!template) throw new Error('Missing boundary template.');
      const fingerprint = JSON.stringify([identity, config.target_version_id, template.challenge]);
      if (!pending || pending.fingerprint !== fingerprint || !publicId(pending.key)) pending = { fingerprint, key: 'submission-' + crypto.randomUUID() };
      try { sessionStorage.setItem(pendingKey, JSON.stringify(pending)); } catch { /* In-tab idempotency still applies. */ }
      const result = await client.submit({ request_key: pending.key, target_version_id: config.target_version_id, challenge: template.challenge });
      lastId = result.submission_id;
      try { sessionStorage.setItem(lastKey, lastId); } catch { /* The in-tab status button remains available. */ }
      $('submission-status').textContent = lastId + ' / ' + result.state;
      message(client.mode === 'example' ? 'Test challenge stored by the local service. No live model research was started.' : 'Challenge received. Check its status here.');
    });
  });
  $('check-submission').addEventListener('click', () => action(async () => {
    const value = await client.read('/api/challenges/' + encodeURIComponent(lastId));
    if (!['queued', 'running', 'completed', 'failed', 'paused'].includes(value.state)) throw new Error('Invalid status.');
    $('submission-status').textContent = lastId + ' / ' + value.state;
    message('Submission status updated. This read did not start research.');
  }));
  document.addEventListener('research:experiment', async event => {
    targetRequest?.abort(); targetRequest = new AbortController();
    const request = targetRequest; config = null; render();
    if (!client.connected || !publicId(event.detail.id)) return;
    $('submission-target').textContent = 'Target: ' + event.detail.title;
    try {
      const value = await client.read('/api/challenges/config', { experiment_id: event.detail.id }, request.signal);
      if (request.signal.aborted) return;
      if (value.mode !== client.mode || value.experiment_id !== event.detail.id || !publicId(value.target_version_id) ||
          !Array.isArray(value.scenarios) || value.scenarios.length !== scenarios.length ||
          !scenarios.every(id => value.scenarios.some(item => item.scenario_id === id && item.challenge?.schema_version === 'challenge.v1'))) throw new Error('Invalid challenge configuration.');
      config = value;
    } catch (error) { if (error.name !== 'AbortError') failure(error); }
    finally { if (request === targetRequest) render(); }
  });
  render();
  return { async connected() {
    render();
    if (!client.connected) return;
    try {
      const session = await client.session();
      identity = session.authenticated && publicId(session.contributor_id) ? session.contributor_id : null;
    } catch (error) { failure(error); }
    render();
  } };
}

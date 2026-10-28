import { motion, announce } from './common.js?v=m14-1';
import { createResearchAdapter } from './research-adapter.js?v=public-records-5';
import { PHASES, validateExperimentPage, validateRecordPage, mergeRecords, safePublicHref } from './research-contract.js?v=public-research-v2-1';
import { EXPERIMENT_STATE_LABELS, renderResearchStatus } from './research-status.js';
import { researchAutoUpdateSeconds } from './site-config.js?v=m13-1';
import { autoUpdateSeconds, createAutoUpdater } from './research-auto-update.js?v=m11-1';

const $ = id => document.getElementById(id);
const roleNames = { seeker: 'SEEKER', keeper: 'KEEPER', verifier: 'FIXED VERIFIER', contributor: 'CONTRIBUTOR', system: 'EXPERIMENT SYSTEM' };
const typeNames = { contribution: 'Contribution', hypothesis: 'Hypothesis', discussion: 'Public exchange', experiment: 'Experiment action', finding: 'Finding', repair: 'Repair proposal', verification: 'Program result', comparison: 'Version comparison', decision: 'Version decision' };
const assertionNames = { unverified: 'Unverified', supported: 'Supported by checks', refuted: 'Refuted by checks', inconclusive: 'Inconclusive', not_applicable: 'Recorded' };
const conclusionNames = { improved: 'Improvement reported', regressed: 'Regression reported', unchanged: 'No improvement', incomparable: 'Not comparable' };
const outcomeNames = { passed: 'Checks passed', failed: 'Checks failed', error: 'Execution error', unsupported: 'Unsupported', inconclusive: 'Inconclusive', retained: 'Retained', rejected: 'Rejected', deferred: 'Deferred', superseded: 'Superseded' };
// One status derivation shared by the history list, the inspector header and the caption,
// so a record never shows two different states in the same view.
const statusOf = record => record.verification ? outcomeNames[record.verification.outcome]
  : record.decision ? outcomeNames[record.decision.outcome] : assertionNames[record.assertion];
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export async function mountResearchView(lab, adapter = createResearchAdapter()) {
  const params = new URLSearchParams(location.search);
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
  const fixturePreview = loopback && params.get('research') === 'preview';
  const preview = fixturePreview || (loopback && adapter.connected && adapter.mode === 'example');
  if (fixturePreview) {
    const { createPreviewAdapter } = await import('./examples/research-preview.js?v=public-research-v2-1');
    const dataState = params.get('sample_state') || 'ready';
    const requestedState = params.get('experiment_state');
    const experimentState = Object.hasOwn(EXPERIMENT_STATE_LABELS, requestedState) ? requestedState : dataState === 'paused' ? 'paused' : null;
    adapter = createPreviewAdapter(dataState, { experimentState });
    $('preview-controls').hidden = false;
    $('preview-state').value = dataState === 'paused' ? 'ready' : dataState;
    $('preview-experiment-state').value = experimentState || 'default';
    $('preview-state').addEventListener('change', event => {
      const url = new URL(location.href); url.searchParams.set('sample_state', event.target.value); url.hash = 'research'; location.assign(url);
    });
    $('preview-experiment-state').addEventListener('change', event => {
      const url = new URL(location.href);
      if (event.target.value === 'default') url.searchParams.delete('experiment_state');
      else url.searchParams.set('experiment_state', event.target.value);
      if (url.searchParams.get('sample_state') === 'paused') url.searchParams.set('sample_state', 'ready');
      url.hash = 'research'; location.assign(url);
    });
  }
  $('preview-research').hidden = !loopback || preview;
  $('research').dataset.source = preview ? 'example' : adapter.connected ? 'live' : 'disconnected';
  $('research-source').textContent = preview ? fixturePreview ? 'LOCAL EXAMPLE / AUTHORED RECORDS' : 'LOCAL SERVICE / EXAMPLE RECORDS' : adapter.connected ? 'Public research archive' : 'Research service not connected';
  $('research-disclosure').textContent = preview ? fixturePreview ? 'Interface fixtures only. No actual agent exchange, experiment or improvement is shown.' : 'Local service verification with scripted role responses. This is not live model research.' : 'Public statements, experiment events and fixed-program results. No internal model reasoning.';

  let experiments = [], records = [], selectedId = null, experimentId = null;
  let experimentCursor = null, recordCursor = null, request = null, epoch = 0, disposed = false, busy = false;
  let playing = false, timer = null, pendingReference = null;
  let autoReading = false, sectionVisible = false, autoRequest = null;
  const auto = createAutoUpdater({
    seconds: autoUpdateSeconds(researchAutoUpdateSeconds, { loopback, search: location.search }),
    canRead: () => adapter.connected && !disposed && sectionVisible && !document.hidden && !busy && !autoReading && !playing,
    read: async () => { await refreshExperiments(); if (!disposed && experimentId && recordCursor === null) await readRecords({ refresh: true, quiet: true }); },
  });
  // Quiet archive poll: refreshes the experiment list and states without touching the
  // open selection, so a newly published round becomes visible without a page reload.
  async function refreshExperiments() {
    if (!adapter.connected || busy) return;
    autoReading = true;
    autoRequest?.abort(); autoRequest = new AbortController();
    try {
      const page = validateExperimentPage(await adapter.listExperiments({ cursor: null, limit: 40, signal: autoRequest.signal }), adapter.mode);
      if (disposed) return;
      const byId = new Map(experiments.map(item => [item.experiment_id, item]));
      let added = 0, stateChanged = false;
      for (const item of page.experiments) {
        const prev = byId.get(item.experiment_id);
        if (!prev) added += 1; else if (prev.state !== item.state) stateChanged = true;
        byId.set(item.experiment_id, item);
      }
      if (!added && !stateChanged) return;
      experiments = [...byId.values()];
      const select = $('experiment-select'), current = experimentId;
      select.replaceChildren();
      for (const experiment of experiments) { const option = el('option', `${preview ? 'Example / ' : ''}${experiment.title}`); option.value = experiment.experiment_id; select.append(option); }
      if (current && experiments.some(item => item.experiment_id === current)) select.value = current;
      renderResearchStatus({ root: $('research'), dataStatus: $('data-read-status'), experimentStatus: $('experiment-status'), context: $('experiment-status-context') },
        { readState: $('research').dataset.connection || 'ready', experiment: experiments.find(item => item.experiment_id === current) });
      if (!current && experiments.length) { await openExperiment(experiments[0].experiment_id); return; }
      if (added) announce(added === 1 ? 'One new published experiment is available in the archive list.' : `${added} new published experiments are available in the archive list.`);
    } catch { /* A quiet refresh failure keeps the last loaded list; the next tick retries. */ }
    finally { autoReading = false; auto.notify(); }
  }
  const buttons = new Map();
  function setState(state, title, description, retry = false) {
    $('research').dataset.connection = state;
    renderResearchStatus({ root: $('research'), dataStatus: $('data-read-status'), experimentStatus: $('experiment-status'), context: $('experiment-status-context') }, { readState: state, experiment: experiments.find(item => item.experiment_id === experimentId) });
    $('research-state-title').textContent = title;
    $('research-state-description').textContent = description;
    $('retry-research').hidden = !retry;
  }
  function setBusy(value) {
    busy = value; $('research-workspace').setAttribute('aria-busy', String(value));
    $('experiment-select').disabled = !experiments.length || busy;
    $('refresh-records').disabled = !adapter.connected || busy;
    $('more-experiments').disabled = busy;
    $('load-more-records').disabled = busy;
    updateControls();
    auto.notify();
  }
  function beginRequest(quiet = false) {
    request?.abort(); request = new AbortController(); epoch += 1; if (!quiet) setBusy(true); return { signal: request.signal, revision: epoch };
  }
  function stopPlaying() {
    playing = false; clearInterval(timer); timer = null;
    $('play-walkthrough').querySelector('use').setAttribute('href', '#i-play');
    $('play-walkthrough').querySelector('span').textContent = 'Play history';
    $('play-walkthrough').setAttribute('aria-pressed', 'false');
    auto.notify();
  }
  function updateControls() {
    const index = records.findIndex(record => record.record_id === selectedId);
    $('timeline').disabled = !records.length; $('timeline').max = String(Math.max(0, records.length - 1)); $('timeline').value = String(Math.max(0, index));
    $('timeline').style.setProperty('--progress', `${records.length > 1 ? index / (records.length - 1) * 100 : 0}%`);
    $('timeline').setAttribute('aria-valuetext', index < 0 ? 'No record selected' : `${index + 1} of ${records.length} loaded: ${records[index].title}`);
    $('previous-stage').disabled = index <= 0; $('next-stage').disabled = index < 0 || index === records.length - 1;
    $('play-walkthrough').disabled = records.length < 2; $('latest-record').disabled = !records.length;
    $('record-position').textContent = index < 0 ? 'No record selected' : `${index + 1} / ${records.length} loaded · sequence ${records[index].sequence}`;
    $('history-count').textContent = `${records.length} loaded`;
    $('stage-count').textContent = index < 0 ? '0 records' : `RECORD ${records[index].sequence}`;
    $('record-empty').hidden = records.length > 0;
    $('load-more-records').hidden = recordCursor === null;
    $('more-experiments').hidden = experimentCursor === null;
    $('history-boundary').textContent = !adapter.connected ? 'The archive is not connected.' : recordCursor !== null ? 'More history is available. This is a partial view.' : records.length ? 'All currently published records loaded.' : 'No public records yet.';
    $('refresh-records').firstChild.textContent = !experiments.length ? 'Refresh archive ' : recordCursor !== null ? 'Continue loading history ' : 'Check for updates ';
    document.querySelectorAll('[data-view], .role-focus').forEach(button => { button.disabled = !records.some(record => record.speaker === (button.dataset.view || button.dataset.role)); });
    document.querySelectorAll('[data-focus-type]').forEach(button => { button.disabled = !records.some(record => record.type === button.dataset.focusType); });
  }
  function reference(id, label = id) {
    const button = el('button', label, 'record-reference'); button.type = 'button';
    button.addEventListener('click', () => {
      if (records.some(record => record.record_id === id)) selectRecord(id, { manual: true, reveal: true });
      else {
        pendingReference = id;
        announce(recordCursor === null ? 'This reference is outside the available public history.' : 'Reference not loaded. Load more history to locate it.');
        $('reference-notice')?.remove();
        const note = el('p', recordCursor === null ? `Reference ${id} is not available in this public history.` : `Reference ${id} is not loaded yet. Use Load more records.`, 'reference-notice'); note.id = 'reference-notice'; $('record-detail').prepend(note);
      }
    });
    return button;
  }
  function block(title, className = '') {
    const section = el('section', undefined, `record-block ${className}`); section.append(el('h4', title)); $('record-detail').append(section); return section;
  }
  function info(container, label, value) {
    const row = el('div', undefined, 'record-info-row'); row.append(el('dt', label), el('dd', value)); container.append(row);
  }
  function renderDetail(record) {
    const target = $('record-detail'); target.replaceChildren();
    $('stage-actor').textContent = `${preview ? 'EXAMPLE / ' : ''}${roleNames[record.speaker]}`;
    $('stage-status').textContent = statusOf(record); $('stage-status').dataset.assertion = record.assertion;
    $('stage-title').textContent = record.title; $('stage-summary').textContent = record.summary;
    const identity = block(typeNames[record.type], 'record-identity');
    identity.append(el('p', `${record.record_id} · sequence ${record.sequence}`, 'record-id'));
    if (record.reply_to.length) {
      const replies = el('div', undefined, 'record-replies'); replies.append(el('span', 'Responding to'));
      record.reply_to.forEach(id => replies.append(reference(id, `${id} — ${records.find(item => item.record_id === id)?.title || 'View reference'}`))); identity.append(replies);
    }
    if (record.public_text) {
      const body = block(['seeker', 'keeper', 'contributor'].includes(record.speaker) ? 'Public statement' : 'Published observation');
      if (record.public_text.length > 700) {
        body.append(el('p', `${record.public_text.slice(0, 350)}…`, 'public-text'));
        const details = el('details', undefined, 'public-text-expand'); details.append(el('summary', 'Read full public note'), el('p', record.public_text, 'public-text')); body.append(details);
      } else body.append(el('p', record.public_text, 'public-text'));
    }
    if (record.test_ids.length || record.candidate) {
      const section = block('Tests & version'); const data = el('dl', undefined, 'record-data'); section.append(data);
      if (record.test_ids.length) info(data, 'Linked tests', record.test_ids.join(' · '));
      if (record.candidate) {
        info(data, 'Candidate', record.candidate.version_id); info(data, 'Parent', record.candidate.parent_version_id || 'No parent recorded');
        const parent = records.find(item => item.sequence < record.sequence && item.candidate?.version_id === record.candidate.parent_version_id);
        if (parent) section.append(reference(parent.record_id, 'Inspect parent version record'));
      }
    }
    if (record.verification) {
      const result = block(`${preview ? 'Example / ' : ''}Fixed-program result`, 'record-verdict'); result.dataset.outcome = record.verification.outcome;
      result.append(el('strong', outcomeNames[record.verification.outcome]), el('p', record.verification.summary));
    }
    if (record.comparison) {
      const comparison = record.comparison, section = block(`${preview ? 'Example / ' : ''}Version comparison`, 'record-comparison');
      section.append(el('strong', conclusionNames[comparison.conclusion]), el('p', comparison.explanation));
      const metadata = el('dl', undefined, 'record-data');
      for (const [label, value] of [['Parent / baseline', comparison.baseline_version_id], ['Candidate', comparison.candidate_version_id], ['Policy', comparison.spec_version], ['Test set', comparison.test_set_version], ['Model configuration', comparison.model_config_id], ['Resource profile', comparison.resource_conditions.profile_id], ['Reported matching conditions', comparison.resource_conditions.matched ? 'Matched — not independently verified' : 'Not matched — not comparable']]) info(metadata, label, value);
      section.append(metadata);
      if (comparison.metrics.length) {
        const table = el('table'), caption = el('caption', preview ? 'Illustrative values — not experimental results' : 'Reported comparison under the recorded conditions'); table.append(caption);
        const head = el('thead'), heading = el('tr'); ['Metric', 'Baseline', 'Candidate'].forEach(label => { const cell = el('th', label); cell.scope = 'col'; heading.append(cell); }); head.append(heading); table.append(head);
        const body = el('tbody'); comparison.metrics.forEach(metric => { const row = el('tr'), name = el('th', `${metric.label} (${metric.unit})`); name.scope = 'row'; row.append(name, el('td', String(metric.baseline)), el('td', String(metric.candidate))); body.append(row); }); table.append(body);
        const scroller = el('div', undefined, 'comparison-scroll'); scroller.append(table); section.append(scroller);
      }
    }
    if (record.decision) {
      const section = block('Version decision', 'record-decision'); section.dataset.outcome = record.decision.outcome;
      section.append(el('strong', outcomeNames[record.decision.outcome]), el('p', record.decision.reason));
      const refs = el('div', undefined, 'record-replies'); refs.append(el('span', 'Based on')); record.decision.supporting_record_ids.forEach(id => refs.append(reference(id))); section.append(refs);
    }
    if (record.evidence.length) {
      const section = block('Evidence references');
      record.evidence.forEach(item => {
        const row = el('div', undefined, 'record-evidence'), href = safePublicHref(item.href, location.origin);
        row.append(el('span', item.evidence_id, 'record-id'));
        if (href) { const link = el('a', item.label, 'text-link'); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; row.append(link); }
        else row.append(el('span', preview ? 'Sample reference / no evidence file' : 'Public download not available', 'record-muted'));
        section.append(row);
      });
    }
    if (record.contributions.length) {
      const section = block('Contribution lineage');
      record.contributions.forEach(item => {
        const data = el('dl', undefined, 'record-data'); info(data, 'Contribution', item.contribution_id); info(data, 'Public contributor ID', item.contributor_id); info(data, 'Submission', item.submission_id); info(data, 'Contribution type', item.kind.replaceAll('_', ' ')); section.append(data, reference(item.origin_record_id, 'Trace to original submission'));
      });
    }
  }
  function selectRecord(id, { manual = false, reveal = false, animate = false } = {}) {
    const index = records.findIndex(record => record.record_id === id); if (index < 0) return;
    if (manual) { stopPlaying(); $('follow-latest').checked = false; }
    selectedId = id; const record = records[index];
    for (const [key, button] of buttons) { button.setAttribute('aria-pressed', String(key === id)); button.classList.toggle('is-selected', key === id); }
    renderDetail(record); updateControls();
    lab.setPhase(PHASES.indexOf(record.phase)); lab.setOutcome?.(record.verification?.outcome || record.decision?.outcome || 'pending'); lab.setMode(record.speaker === 'keeper' ? 'keeper' : record.speaker === 'seeker' ? 'seeker' : 'both'); lab.setPaused(motion.paused);
    $('research').dataset.phase = record.phase === 'find' ? 'counterexample' : record.decision?.outcome === 'retained' ? 'archive' : record.phase;
    $('research').dataset.outcome = record.verification?.outcome || record.decision?.outcome || 'pending';
    $('visual-phase').textContent = record.phase.toUpperCase(); $('gate-label').textContent = record.verification ? 'CHECKS' : record.decision ? record.decision.outcome.toUpperCase() : record.phase.toUpperCase();
    $('route-caption').textContent = `${preview ? 'Example / ' : ''}${typeNames[record.type]} · ${statusOf(record)}`;
    $('flow-scene').setAttribute('aria-label', `Schematic for ${record.record_id}. ${record.summary}`);
    document.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === record.speaker)));
    const copy = $('stage-copy'); copy.classList.remove('is-changing');
    if (animate && !motion.paused) { void copy.offsetWidth; copy.classList.add('is-changing'); }
    if (reveal && buttons.has(id)) {
      const button = buttons.get(id), scroll = $('record-scroll');
      const bounds = button.getBoundingClientRect(), parentBounds = scroll.getBoundingClientRect();
      if (bounds.top < parentBounds.top || bounds.bottom > parentBounds.bottom) scroll.scrollTop += bounds.top - parentBounds.top - scroll.clientHeight / 3;
      button.focus({ preventScroll: true });
    }
  }
  function renderHistory() {
    const scroll = $('record-scroll'), top = scroll.scrollTop, focused = document.activeElement?.dataset?.recordId;
    const fragment = document.createDocumentFragment(); buttons.clear();
    records.forEach(record => {
      const row = el('li'), button = el('button', undefined, 'record-entry'); button.type = 'button'; button.dataset.recordId = record.record_id; button.dataset.speaker = record.speaker;
      button.setAttribute('aria-pressed', String(record.record_id === selectedId)); button.classList.toggle('is-selected', record.record_id === selectedId);
      const meta = el('span', undefined, 'record-entry-meta'); meta.append(el('span', `${String(record.sequence).padStart(2, '0')} / ${roleNames[record.speaker]}`), el('span', typeNames[record.type]));
      button.append(meta, el('strong', record.title), el('span', record.summary, 'record-entry-summary'));
      const status = el('span', statusOf(record), 'record-entry-status'); status.dataset.outcome = record.verification?.outcome || record.decision?.outcome || record.assertion; button.append(status);
      button.addEventListener('click', event => selectRecord(record.record_id, { manual: true, animate: event.detail > 0 })); buttons.set(record.record_id, button); row.append(button); fragment.append(row);
    });
    $('record-history').replaceChildren(fragment); scroll.scrollTop = top;
    if (focused && buttons.has(focused)) buttons.get(focused).focus({ preventScroll: true });
  }
  function clearRecordView() {
    stopPlaying(); selectedId = null; records = []; recordCursor = null; pendingReference = null;
    $('record-detail').replaceChildren(); $('stage-actor').textContent = 'PUBLIC RECORD'; $('stage-status').textContent = 'Awaiting records';
    $('stage-title').textContent = 'The record starts here.'; $('stage-summary').textContent = 'Select a published record to follow a question, its tests and what happened next.';
    $('visual-phase').textContent = 'WAITING'; $('gate-label').textContent = 'WAITING'; $('route-caption').textContent = 'A schematic of the migration boundary';
    $('research').dataset.phase = 'waiting'; $('research').dataset.outcome = 'pending';
    lab.setPhase(0); lab.setOutcome?.('pending'); lab.setMode('both'); lab.setPaused(true); renderHistory(); updateControls();
  }
  function showReadState() {
    if (!records.length) setState('empty', 'No public records for this experiment yet.', 'The published experiment state is shown separately from the availability of records.');
    else setState('ready', preview ? 'Local example records available.' : 'Public research records available.', preview ? 'Local example data, not a live research feed.' : 'Follow the exchange, inspect linked checks and trace the version decision.');
  }
  async function readRecords({ reset = false, refresh = false, quiet = false } = {}) {
    if (!experimentId || !adapter.connected) return;
    const { signal, revision } = beginRequest(quiet);
    if (quiet) autoReading = true;
    if (reset) clearRecordView();
    if (!quiet) setState('loading', 'Loading public records…', records.length ? 'Your selected record stays in place while the archive is read.' : 'Reading the published history. This does not start an experiment.');
    try {
      const requestedCursor = refresh ? null : recordCursor;
      const page = validateRecordPage(await adapter.listRecords({ experimentId, cursor: requestedCursor, afterSequence: refresh ? records.at(-1)?.sequence || 0 : null, limit: 8, signal }), experimentId, adapter.mode);
      if (disposed || revision !== epoch) return;
      if (page.next_cursor !== null && page.next_cursor === requestedCursor) throw new Error('Cursor did not advance.');
      const priorExperiment = experiments.find(item => item.experiment_id === experimentId);
      if (priorExperiment && priorExperiment.spec_version !== page.experiment.spec_version) throw new Error('Experiment policy changed.');
      const merged = mergeRecords(records, page.records), added = merged.length - records.length;
      experiments = experiments.map(item => item.experiment_id === experimentId ? page.experiment : item); records = merged; recordCursor = page.next_cursor;
      renderHistory();
      if (quiet) updateControls();
      const nextSelection = pendingReference && records.some(item => item.record_id === pendingReference) ? pendingReference : $('follow-latest').checked ? records.at(-1)?.record_id : selectedId || records[0]?.record_id;
      if (pendingReference === nextSelection) pendingReference = null;
      if (nextSelection && (!quiet || nextSelection !== selectedId)) selectRecord(nextSelection);
      showReadState();
      const note = refresh ? added ? `${added} new public records loaded. Your history view was preserved unless following latest.` : 'No new public records.' : `${records.length} public records loaded.`;
      if (!quiet || added) announce(note);
    } catch (error) {
      if (revision !== epoch || disposed || quiet || error.name === 'AbortError') return;
      setState('error', 'Public records could not be read.', records.length ? 'Previously loaded records remain available. Try reading the archive again.' : 'The service may be unavailable or the public data format may need attention. Try again.', true);
    } finally {
      if (quiet) { autoReading = false; auto.notify(); }
      else if (!disposed && revision === epoch) setBusy(false);
    }
  }
  async function openExperiment(id) {
    experimentId = id; $('experiment-select').value = id;
    document.dispatchEvent(new CustomEvent('research:experiment', { detail: { id, title: experiments.find(item => item.experiment_id === id)?.title || id } })); $('follow-latest').checked = false;
    $('case-label').textContent = `${experiments.find(item => item.experiment_id === id)?.chapter || 'RESEARCH'} / ${id}`;
    await readRecords({ reset: true });
  }
  async function readArchive(more = false) {
    if (!adapter.connected) { clearRecordView(); setState('disconnected', 'Waiting for a public research service.', 'No live public exchange or experimental record is connected.'); return; }
    const { signal, revision } = beginRequest();
    setState('loading', 'Loading the research archive…', 'Reading published experiments. Browsing does not invoke a model.');
    try {
      const oldCursor = more ? experimentCursor : null;
      const page = validateExperimentPage(await adapter.listExperiments({ cursor: oldCursor, limit: 40, signal }), adapter.mode);
      if (disposed || revision !== epoch) return;
      if (page.next_cursor !== null && page.next_cursor === oldCursor) throw new Error('Cursor did not advance.');
      experiments = more ? [...new Map([...experiments, ...page.experiments].map(item => [item.experiment_id, item])).values()] : page.experiments; experimentCursor = page.next_cursor;
      $('experiment-select').replaceChildren();
      for (const experiment of experiments) { const option = el('option', `${preview ? 'Example / ' : ''}${experiment.title}`); option.value = experiment.experiment_id; $('experiment-select').append(option); }
      if (!experiments.length) { experimentId = null; clearRecordView(); setState('empty', 'No public experiments have been published yet.', 'The archive can be connected while still having no research to display.'); return; }
      if (more && experimentId) { $('experiment-select').value = experimentId; showReadState(); }
      else await openExperiment(experiments[0].experiment_id);
    } catch (error) {
      if (revision !== epoch || disposed || error.name === 'AbortError') return;
      setState('error', 'The public archive could not be read.', 'Try again. No experiment or model call was started.', true);
    } finally { if (!disposed && revision === epoch) setBusy(false); }
  }
  $('experiment-select').addEventListener('change', event => openExperiment(event.target.value));
  $('more-experiments').addEventListener('click', () => readArchive(true));
  $('load-more-records').addEventListener('click', () => readRecords());
  $('refresh-records').addEventListener('click', async () => {
    await refreshExperiments();
    if (!experimentId) await readArchive();
    else await readRecords({ refresh: recordCursor === null });
  });
  $('retry-research').addEventListener('click', () => !experimentId ? readArchive() : readRecords({ refresh: records.length > 0 && recordCursor === null }));
  $('timeline').addEventListener('input', event => selectRecord(records[Number(event.target.value)]?.record_id, { manual: true }));
  $('previous-stage').addEventListener('click', () => selectRecord(records[records.findIndex(item => item.record_id === selectedId) - 1]?.record_id, { manual: true, reveal: true }));
  $('next-stage').addEventListener('click', () => selectRecord(records[records.findIndex(item => item.record_id === selectedId) + 1]?.record_id, { manual: true, reveal: true }));
  $('latest-record').addEventListener('click', () => selectRecord(records.at(-1)?.record_id, { manual: true, reveal: true }));
  $('play-walkthrough').addEventListener('click', () => {
    if (playing) { stopPlaying(); return; }
    $('follow-latest').checked = false;
    if (selectedId === records.at(-1)?.record_id) selectRecord(records[0].record_id);
    playing = true; $('play-walkthrough').setAttribute('aria-pressed', 'true'); $('play-walkthrough').querySelector('use').setAttribute('href', '#i-pause'); $('play-walkthrough').querySelector('span').textContent = 'Pause history';
    timer = setInterval(() => {
      if (document.hidden) { stopPlaying(); return; }
      const next = records.findIndex(item => item.record_id === selectedId) + 1;
      if (next >= records.length) { stopPlaying(); return; }
      selectRecord(records[next].record_id, { animate: true });
      const entry = buttons.get(selectedId), scroller = $('record-scroll');
      if (entry) {
        const bounds = entry.getBoundingClientRect(), parent = scroller.getBoundingClientRect();
        if (bounds.top < parent.top || bounds.bottom > parent.bottom) scroller.scrollTop += bounds.top - parent.top - scroller.clientHeight / 3;
      }
      if (next === records.length - 1) stopPlaying();
    }, 4200);
  });
  function focusRole(role, next = false) {
    const start = next ? records.findIndex(item => item.record_id === selectedId) : -1;
    const record = records.find(item => item.sequence > (records[start]?.sequence || 0) && item.speaker === role) || records.find(item => item.speaker === role);
    if (record) selectRecord(record.record_id, { manual: true, reveal: true });
  }
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => focusRole(button.dataset.view, true)));
  document.querySelectorAll('.role-focus').forEach(button => button.addEventListener('click', () => { focusRole(button.dataset.role); $('research').scrollIntoView({ behavior: motion.reduced ? 'auto' : 'smooth' }); }));
  document.querySelectorAll('[data-focus-type]').forEach(button => button.addEventListener('click', () => { const record = records.find(item => item.type === button.dataset.focusType); if (record) selectRecord(record.record_id, { manual: true, reveal: true }); $('research').scrollIntoView({ behavior: motion.reduced ? 'auto' : 'smooth' }); }));
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopPlaying(); else auto.notify(); });
  const historyObserver = new IntersectionObserver(entries => {
    sectionVisible = entries[0].isIntersecting;
    if (!sectionVisible) stopPlaying(); else auto.notify();
  }, { rootMargin: '100px' });
  historyObserver.observe($('research'));
  clearRecordView(); readArchive();
  return { onMotionChange(state) { lab.setPaused(state.paused || !records.length); if (state.paused) stopPlaying(); }, dispose() { disposed = true; epoch += 1; request?.abort(); autoRequest?.abort(); historyObserver.disconnect(); stopPlaying(); auto.stop(); } };
}

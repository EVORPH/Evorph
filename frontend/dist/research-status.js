export const EXPERIMENT_STATE_LABELS = Object.freeze({
  queued: 'Queued', running: 'Running', completed: 'Completed', failed: 'Failed', paused: 'Paused',
});
const READ_STATE_LABELS = Object.freeze({
  disconnected: 'Not connected', loading: 'Loading', ready: 'Loaded', empty: 'Loaded / no records', error: 'Read failed',
});

// These are independent axes. A failed read cannot change an experiment outcome.
export function renderResearchStatus(nodes, { readState, experiment = null }) {
  const state = experiment && Object.hasOwn(EXPERIMENT_STATE_LABELS, experiment.state) ? experiment.state : 'unknown';
  nodes.root.dataset.experimentState = state;
  nodes.dataStatus.dataset.state = readState;
  nodes.dataStatus.textContent = Object.hasOwn(READ_STATE_LABELS, readState) ? READ_STATE_LABELS[readState] : 'Unavailable';
  nodes.experimentStatus.dataset.state = state;
  nodes.experimentStatus.textContent = state === 'unknown' ? 'Unavailable' : EXPERIMENT_STATE_LABELS[state];
  nodes.context.textContent = state !== 'unknown' && ['loading', 'error', 'disconnected'].includes(readState) ? '(last available state)' : '';
}

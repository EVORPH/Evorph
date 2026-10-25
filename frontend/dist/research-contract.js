// Public presentation contract. Validation is not experimental verification.
export const SCHEMA_VERSION = 'evorph.public-research.v2';
export const RECORD_TYPES = ['contribution', 'hypothesis', 'discussion', 'experiment', 'finding', 'repair', 'verification', 'comparison', 'decision'];
export const SPEAKERS = ['seeker', 'keeper', 'verifier', 'contributor', 'system'];
export const PHASES = ['observe', 'explore', 'find', 'repair', 'verify', 'retain'];
const fail = () => { throw new Error('Public research data does not match the agreed format.'); };
function object(value, keys, required = keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail();
  if (Object.keys(value).some(key => !keys.includes(key))) fail();
  if (required.some(key => !Object.hasOwn(value, key))) fail();
}
function text(value, max = 2000, empty = false) {
  if (typeof value !== 'string' || value.length > max || (!empty && !value.trim()) || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(value)) fail();
}
function id(value) { if (typeof value !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,119}$/.test(value)) fail(); }
function option(value, choices) { if (!choices.includes(value)) fail(); }
function number(value) { if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) fail(); }
function array(value, inspect, max = 100) {
  if (!Array.isArray(value) || value.length > max) fail();
  value.forEach(inspect);
}
function ids(value) { array(value, id); if (new Set(value).size !== value.length) fail(); }
function nullable(value, inspect) { if (value !== null) inspect(value); }
function candidate(value) { object(value, ['version_id', 'parent_version_id']); id(value.version_id); nullable(value.parent_version_id, id); if (value.version_id === value.parent_version_id) fail(); }
function source(value) {
  object(value, ['origin', 'source_id', 'model_config_id']);
  option(value.origin, ['model_public', 'program', 'player_submission', 'example']); id(value.source_id); nullable(value.model_config_id, id);
}
function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  return value;
}

export function safePublicHref(value, origin = 'https://evorph.invalid') {
  if (typeof value !== 'string' || !value || value.length > 2000 || /[\s\\]/u.test(value) || value.startsWith('//')) return null;
  try {
    const url = new URL(value, origin);
    const decoded = decodeURIComponent(value);
    if (url.username || url.password || /[\u0000-\u0020\\]/u.test(decoded) || decoded.split(/[/?#]/).includes('..')) return null;
    const absolute = /^[a-z][a-z0-9+.-]*:/i.test(value);
    if (absolute ? url.protocol !== 'https:' : url.origin !== new URL(origin).origin || !/^(?:\.\/|\/|[a-zA-Z0-9_-])/.test(value)) return null;
    return url.href;
  } catch { return null; }
}

function evidence(value) {
  object(value, ['evidence_id', 'label', 'href']); id(value.evidence_id); text(value.label, 200);
  nullable(value.href, href => { if (!safePublicHref(href)) fail(); });
}
function contribution(value) {
  object(value, ['contribution_id', 'submission_id', 'contributor_id', 'kind', 'origin_record_id']);
  ['contribution_id', 'submission_id', 'contributor_id', 'origin_record_id'].forEach(key => id(value[key]));
  option(value.kind, ['discovery', 'reduction', 'reverification', 'regression_report', 'submission']);
}
function verification(value) {
  object(value, ['outcome', 'summary', 'test_ids', 'evidence_ids']);
  option(value.outcome, ['passed', 'failed', 'error', 'unsupported', 'inconclusive']); text(value.summary, 4000);
  ids(value.test_ids); ids(value.evidence_ids); if (!value.test_ids.length) fail();
}
function comparison(value) {
  object(value, ['baseline_version_id', 'candidate_version_id', 'spec_version', 'test_set_version', 'model_config_id', 'resource_conditions', 'metrics', 'conclusion', 'explanation']);
  ['baseline_version_id', 'candidate_version_id', 'spec_version', 'test_set_version', 'model_config_id'].forEach(key => id(value[key]));
  object(value.resource_conditions, ['profile_id', 'matched']); id(value.resource_conditions.profile_id); option(value.resource_conditions.matched, [true, false]);
  array(value.metrics, metric => {
    object(metric, ['metric_id', 'label', 'unit', 'baseline', 'candidate', 'direction']); id(metric.metric_id); text(metric.label, 200); text(metric.unit, 80);
    number(metric.baseline); number(metric.candidate); option(metric.direction, ['higher', 'lower', 'neutral']);
  }, 24);
  option(value.conclusion, ['improved', 'regressed', 'unchanged', 'incomparable']); text(value.explanation, 4000);
  if (!value.resource_conditions.matched && value.conclusion !== 'incomparable') fail();
  if (value.conclusion !== 'incomparable' && !value.metrics.length) fail();
}
function decision(value) {
  object(value, ['outcome', 'reason', 'supporting_record_ids']);
  option(value.outcome, ['retained', 'rejected', 'superseded', 'deferred']); text(value.reason, 4000); ids(value.supporting_record_ids);
  if (!value.supporting_record_ids.length) fail();
}

export function validateExperiment(value) {
  object(value, ['experiment_id', 'chapter', 'title', 'summary', 'state', 'spec_version']);
  id(value.experiment_id); text(value.chapter, 150); text(value.title, 200); text(value.summary, 2000); id(value.spec_version);
  option(value.state, ['queued', 'running', 'completed', 'failed', 'paused']); return value;
}
export function validateRecord(value, experimentId = value?.experiment_id) {
  object(value, ['experiment_id', 'record_id', 'sequence', 'type', 'speaker', 'phase', 'title', 'summary', 'public_text', 'assertion', 'source', 'reply_to', 'test_ids', 'candidate', 'verification', 'evidence', 'contributions', 'comparison', 'decision']);
  id(value.experiment_id); id(value.record_id);
  if (value.experiment_id !== experimentId || !Number.isSafeInteger(value.sequence) || value.sequence < 1) fail();
  option(value.type, RECORD_TYPES); option(value.speaker, SPEAKERS); option(value.phase, PHASES);
  source(value.source);
  if (value.source.origin !== 'example') {
    if (['seeker', 'keeper'].includes(value.speaker) && (value.source.origin !== 'model_public' || value.source.model_config_id === null)) fail();
    if (['system', 'verifier'].includes(value.speaker) && value.source.origin !== 'program') fail();
    if (value.speaker === 'contributor' && value.source.origin !== 'player_submission') fail();
  }
  text(value.title, 240); text(value.summary, 2000); text(value.public_text, 50000, true);
  option(value.assertion, ['unverified', 'supported', 'refuted', 'inconclusive', 'not_applicable']);
  ids(value.reply_to); if (value.reply_to.includes(value.record_id)) fail(); ids(value.test_ids);
  nullable(value.candidate, candidate); nullable(value.verification, verification); nullable(value.comparison, comparison); nullable(value.decision, decision);
  array(value.evidence, evidence); array(value.contributions, contribution);
  if (['seeker', 'keeper', 'contributor'].includes(value.speaker) && !['unverified', 'not_applicable'].includes(value.assertion)) fail();
  if (value.verification !== null && (value.speaker !== 'verifier' || value.type !== 'verification')) fail();
  if ((value.type === 'verification') !== (value.verification !== null)) fail();
  if ((value.type === 'comparison') !== (value.comparison !== null) || (value.comparison && value.speaker !== 'verifier')) fail();
  if ((value.type === 'decision') !== (value.decision !== null) || (value.decision && !['system', 'verifier'].includes(value.speaker))) fail();
  if (value.type === 'repair' && (value.speaker !== 'keeper' || !value.candidate)) fail();
  if (value.decision && value.decision.outcome !== 'deferred' && !value.candidate) fail();
  if (value.comparison && value.candidate && value.comparison.candidate_version_id !== value.candidate.version_id) fail();
  if (value.verification) {
    if (value.verification.test_ids.some(test => !value.test_ids.includes(test))) fail();
    if (value.verification.evidence_ids.some(ref => !value.evidence.some(item => item.evidence_id === ref))) fail();
  }
  return value;
}
function envelope(value, keys) {
  object(value, ['schema_version', 'mode', ...keys]);
  if (value.schema_version !== SCHEMA_VERSION) fail(); option(value.mode, ['live', 'example']);
}
function cursor(value) { nullable(value, item => text(item, 1000)); }
export function validateExperimentPage(value, mode) {
  envelope(value, ['experiments', 'next_cursor']);
  if (value.mode !== mode) fail(); array(value.experiments, validateExperiment); cursor(value.next_cursor);
  if (new Set(value.experiments.map(item => item.experiment_id)).size !== value.experiments.length) fail();
  return value;
}
export function validateRecordPage(value, experimentId, mode) {
  envelope(value, ['experiment', 'records', 'next_cursor']);
  if (value.mode !== mode) fail(); validateExperiment(value.experiment);
  if (value.experiment.experiment_id !== experimentId) fail(); cursor(value.next_cursor);
  array(value.records, record => validateRecord(record, experimentId));
  if (value.records.some(record => (record.source.origin === 'example') !== (mode === 'example'))) fail();
  mergeRecords([], value.records);
  if (!value.records.length && value.next_cursor !== null) fail();
  return value;
}

export function mergeRecords(existing, incoming) {
  const byId = new Map(), bySequence = new Map();
  let experimentId = null;
  for (const record of [...existing, ...incoming]) {
    validateRecord(record);
    if (experimentId && record.experiment_id !== experimentId) fail(); experimentId = record.experiment_id;
    const prior = byId.get(record.record_id);
    if (prior && JSON.stringify(stable(prior)) !== JSON.stringify(stable(record))) fail();
    if (bySequence.has(record.sequence) && bySequence.get(record.sequence) !== record.record_id) fail();
    byId.set(record.record_id, record); bySequence.set(record.sequence, record.record_id);
  }
  return [...byId.values()].sort((a, b) => a.sequence - b.sequence);
}

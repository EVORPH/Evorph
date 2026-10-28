export const SOURCE_ORIGIN = 'https://evorph.net';

export function orderedRecords(bundle) {
  return [...(bundle?.records || [])].sort((a, b) => a.sequence - b.sequence);
}

/** Only use evidence URLs actually supplied by the record. Never synthesize a missing link. */
export function safeEvidenceHref(value) {
  if (typeof value !== 'string' || !value || value.length > 2000 || /[\s\\]/u.test(value) || value.startsWith('//')) return null;
  try {
    const decoded = decodeURIComponent(value);
    if (/[\u0000-\u0020\\]/u.test(decoded) || decoded.split(/[/?#]/).includes('..')) return null;
    const url = new URL(value, SOURCE_ORIGIN);
    if (url.origin !== SOURCE_ORIGIN || url.username || url.password || url.search || url.hash) return null;
    if (!/^\/api\/research\/experiments\/[A-Za-z0-9._:-]+\/evidence\/[A-Za-z0-9._:-]+$/.test(url.pathname)) return null;
    return url.href;
  } catch { return null; }
}

const recordedLimitations = [
  'Limited to the recorded specification and test set; not a production security certification.',
  'Matching resource conditions are reported by the service, not independently certified by this interface.'
];

function supportingGraph(decisionRecord, byID) {
  const records = [], seen = new Set(), missing = [];
  function visit(id, beforeSequence) {
    if (seen.has(id)) return;
    seen.add(id);
    const record = byID.get(id);
    if (!record || record.sequence >= beforeSequence) { missing.push(id); return; }
    records.push(record);
    const refs = [...(record.reply_to || []), ...(record.decision?.supporting_record_ids || [])];
    refs.forEach(ref => visit(ref, record.sequence));
  }
  (decisionRecord.decision?.supporting_record_ids || []).forEach(id => visit(id, decisionRecord.sequence));
  return { records: records.sort((a, b) => a.sequence - b.sequence), missing };
}

function improvedMetric(metric) {
  if (!Number.isFinite(metric.baseline) || !Number.isFinite(metric.candidate)) return false;
  return (metric.direction === 'higher' && metric.candidate > metric.baseline) || (metric.direction === 'lower' && metric.candidate < metric.baseline);
}

/** UI classification is evidence navigation, not an independent experimental verdict. */
export function classifyExperiment(bundle) {
  const records = orderedRecords(bundle), experiment = bundle?.experiment || {};
  const decisionRecord = records.filter(r => r.decision).at(-1) || null;
  const verificationRecords = records.filter(r => r.verification);
  const comparisonRecords = records.filter(r => r.comparison);
  const base = { category: 'pending', label: 'Unresolved', reason: 'No conclusive linked public decision is available.', decisionRecord, verificationRecords, comparisonRecords, utility: null, limitations: [...recordedLimitations] };
  if (experiment.state === 'failed') return {
    ...base, category: 'error', label: 'Execution stopped',
    reason: records.filter(r => r.type === 'experiment').at(-1)?.summary || 'The archived run stopped without a conclusive result.',
    limitations: ['An execution failure does not establish that a candidate policy failed its checks.', ...recordedLimitations]
  };
  if (!decisionRecord) return { ...base, label: experiment.state === 'running' ? 'Archived as running' : experiment.state === 'queued' ? 'Archived as queued' : experiment.state === 'paused' ? 'Archived as paused' : 'Unresolved' };
  if (decisionRecord.source?.origin !== 'program' || !['system', 'verifier'].includes(decisionRecord.speaker)) return base;
  const decision = decisionRecord.decision;
  if (decision.outcome === 'rejected') return {
    ...base, category: 'rejected', label: 'Candidate rejected', reason: decision.reason,
    limitations: ['An improved partial metric does not override the recorded rejection.', ...recordedLimitations]
  };
  if (decision.outcome === 'deferred' && experiment.state === 'completed' && /^No verified discrepancy was found in these attempts\.?$/i.test(decision.reason.trim())) return {
    ...base, category: 'no-discovery', label: 'No new discrepancy', reason: decision.reason,
    limitations: ['No discrepancy in these attempts does not prove the policy is free of flaws.', ...recordedLimitations]
  };
  if (decision.outcome !== 'retained') return { ...base, label: decision.outcome === 'superseded' ? 'Superseded' : 'Deferred', reason: decision.reason };

  const byID = new Map(records.map(record => [record.record_id, record]));
  const graph = supportingGraph(decisionRecord, byID);
  const version = decisionRecord.candidate?.version_id;
  const comparisons = graph.records.filter(r => r.comparison && r.speaker === 'verifier' && r.source?.origin === 'program' && r.assertion === 'supported'
    && r.comparison.candidate_version_id === version && r.comparison.spec_version === experiment.spec_version
    && r.comparison.conclusion === 'improved' && r.comparison.resource_conditions?.matched === true
    && r.comparison.metrics?.some(improvedMetric));
  const comparison = comparisons.at(-1);
  const linkedCandidateChecks = comparison ? (comparison.reply_to || []).map(id => byID.get(id)).filter(r => r?.verification && r.candidate?.version_id === version) : [];
  const backed = linkedCandidateChecks.length > 0 && linkedCandidateChecks.every(r => r.speaker === 'verifier' && r.source?.origin === 'program' && r.assertion === 'supported'
    && r.verification.outcome === 'passed' && r.verification.test_ids?.length > 0
    && r.verification.evidence_ids?.length > 0 && r.verification.evidence_ids.every(id => r.evidence?.some(e => e.evidence_id === id)));
  if (!version || !comparison || !backed || graph.missing.length || experiment.state !== 'completed') return {
    ...base, label: 'Retained · evidence incomplete', reason: decision.reason,
    limitations: ['A retained label alone is insufficient: linked passed checks and a comparable improvement are required for this success view.', ...recordedLimitations]
  };
  const metric = comparison.comparison.metrics.find(improvedMetric);
  return {
    ...base, category: 'success', label: 'Verified & retained', reason: decision.reason,
    utility: `${metric.label}: ${metric.baseline} → ${metric.candidate} ${metric.unit}. This records an improvement under ${experiment.spec_version}; the retained candidate and its linked checks can be inspected and compared again.`,
    supportingRecords: graph.records, decisiveComparison: comparison, candidateChecks: linkedCandidateChecks
  };
}

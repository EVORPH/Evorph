// Authored UI fixtures, only imported by the explicit loopback preview.
// No records in this file came from agents or an experimental verifier.
import { SCHEMA_VERSION } from '../research-contract.js?v=public-research-v2-1';

const experiments = [
  { experiment_id: 'sample-retired-key', chapter: 'The Retired Key', title: 'A delayed request after cutover', summary: 'Sample sequence: a player submission, public exchange, a rejected repair and a retained revision.', state: 'completed', spec_version: 'sample-policy-v1' },
  { experiment_id: 'sample-fallback', chapter: 'The Retired Key', title: 'A fallback investigation without a finding', summary: 'Sample sequence: the proposed failure is not reproduced. No repair is retained.', state: 'completed', spec_version: 'sample-policy-v1' },
  { experiment_id: 'sample-paused', chapter: 'The Retired Key', title: 'An investigation on hold', summary: 'Sample sequence: a proposal remains unverified while the research queue is paused.', state: 'paused', spec_version: 'sample-policy-v1' },
  { experiment_id: 'sample-empty', chapter: 'The Retired Key', title: 'A queued experiment', summary: 'Sample experiment with no public records yet.', state: 'queued', spec_version: 'sample-policy-v1' },
];
const provenance = { contribution_id: 'sample-contribution-07', submission_id: 'sample-submission-07', contributor_id: 'sample-contributor-07', kind: 'submission', origin_record_id: 'sample-key-01' };
const proof = id => ({ evidence_id: id, label: 'Illustrative evidence reference', href: null });
const records = [];
function add(experimentIndex, type, speaker, phase, title, summary, extra = {}) {
  const experiment = experiments[experimentIndex];
  const sequence = records.filter(record => record.experiment_id === experiment.experiment_id).length + 1;
  const prefix = ['key', 'fallback', 'paused', 'empty'][experimentIndex];
  records.push({
    experiment_id: experiment.experiment_id, record_id: `sample-${prefix}-${String(sequence).padStart(2, '0')}`, sequence,
    type, speaker, phase, title, summary, public_text: '', assertion: ['seeker', 'keeper', 'contributor'].includes(speaker) ? 'unverified' : 'not_applicable',
    source: { origin: 'example', source_id: `sample-source-${prefix}-${sequence}`, model_config_id: null },
    reply_to: [], test_ids: [], candidate: null, verification: null, evidence: [], contributions: experimentIndex === 0 ? [provenance] : [], comparison: null, decision: null,
    ...extra,
  });
}
add(0, 'contribution', 'contributor', 'observe', 'What happens to a request held across the handoff?', 'A sample player submission starts this investigation.', { public_text: 'Deliver the original signed request after the new key has taken over. Check current authority separately from signature validity.' });
add(0, 'hypothesis', 'seeker', 'explore', 'The signature may survive its authority.', 'I propose holding a permitted old-key request until after confirmation.', { reply_to: ['sample-key-01'], public_text: 'The question is whether the handler authorizes using the current state. A signature alone cannot establish current control. This is a hypothesis until the fixed checks run.' });
add(0, 'discussion', 'keeper', 'explore', 'Keep the normal path beside the challenge.', 'I will compare the old request with a legitimate new-key operation.', { reply_to: ['sample-key-02'], test_ids: ['sample-old-request', 'sample-new-operation'], public_text: 'A blanket refusal would make the attack disappear while breaking the account. Both paths belong in the acceptance record.' });
add(0, 'experiment', 'system', 'explore', 'The two request paths are scheduled.', 'The sample action sequence signs, holds, confirms, then delivers.', { reply_to: ['sample-key-02', 'sample-key-03'], test_ids: ['sample-old-request', 'sample-new-operation'], public_text: 'SIGN permitted legacy request\nHOLD original bytes\nCONFIRM new-key handoff\nDELIVER legacy request\nDELIVER legitimate new-key operation' });
add(0, 'verification', 'verifier', 'find', 'The designed handler violates the policy.', 'Illustrative result: the old request is accepted after cutover.', { assertion: 'supported', reply_to: ['sample-key-04'], test_ids: ['sample-old-request', 'sample-new-operation'], evidence: [proof('sample-evidence-01')], verification: { outcome: 'failed', summary: 'Designed-fault calibration only. This is not a report of a vulnerability in an external system.', test_ids: ['sample-old-request', 'sample-new-operation'], evidence_ids: ['sample-evidence-01'] } });
add(0, 'finding', 'verifier', 'find', 'Historical proof and current authority diverge.', 'The sample failure is linked to the original contributor.', { assertion: 'supported', reply_to: ['sample-key-05'], test_ids: ['sample-old-request'], evidence: [proof('sample-evidence-01')] });
add(0, 'repair', 'keeper', 'repair', 'Candidate 01: close the legacy path.', 'I propose a restricted handling change for the confirmed case.', { reply_to: ['sample-key-06'], candidate: { version_id: 'sample-handler-01', parent_version_id: 'sample-handler-00' }, public_text: 'This candidate is a proposal. Its normal-path behavior still needs independent checking.' });
add(0, 'verification', 'verifier', 'verify', 'Candidate 01 also blocks the new key.', 'Illustrative result: a legitimate operation fails. The repair does not pass.', { assertion: 'refuted', reply_to: ['sample-key-07'], candidate: { version_id: 'sample-handler-01', parent_version_id: 'sample-handler-00' }, test_ids: ['sample-new-operation'], evidence: [proof('sample-evidence-02')], verification: { outcome: 'failed', summary: 'The candidate is rejected because legitimate access is required.', test_ids: ['sample-new-operation'], evidence_ids: ['sample-evidence-02'] } });
add(0, 'decision', 'system', 'retain', 'Candidate 01 is rejected.', 'Its failure remains in the history.', { reply_to: ['sample-key-08'], candidate: { version_id: 'sample-handler-01', parent_version_id: 'sample-handler-00' }, decision: { outcome: 'rejected', reason: 'A repair must preserve legitimate new-key operations.', supporting_record_ids: ['sample-key-08'] } });
add(0, 'discussion', 'seeker', 'explore', 'The active-key check needs the current state.', 'Responding to the failed repair, I propose another bounded test sequence.', { reply_to: ['sample-key-08'], test_ids: ['sample-current-epoch'], public_text: 'Revisit the state after confirmation, including a wrong-epoch request. Repeating an exploration phase is expected; there is no fixed number of research steps.\n\n' + 'Public note: preserve the signed bytes, use current authority, and retain the normal-path comparison. '.repeat(18) + '\nLiteral public quotation: <strong data-literal-marker>markup remains text</strong>' });
add(0, 'repair', 'keeper', 'repair', 'Candidate 02: bind authorization to the active generation.', 'The new proposal retains the parent relationship and the original submission.', { reply_to: ['sample-key-10'], candidate: { version_id: 'sample-handler-02', parent_version_id: 'sample-handler-00' }, public_text: 'Require a valid signature and an authorized key for the current account generation. Reject a consumed nonce while preserving fresh requests. This is a bounded candidate, pending checks.' });
add(0, 'verification', 'verifier', 'verify', 'The selected checks pass for candidate 02.', 'Illustrative result: the old request is refused and the new operation succeeds.', { assertion: 'supported', reply_to: ['sample-key-11'], candidate: { version_id: 'sample-handler-02', parent_version_id: 'sample-handler-00' }, test_ids: ['sample-old-request', 'sample-new-operation', 'sample-current-epoch'], evidence: [proof('sample-evidence-03')], verification: { outcome: 'passed', summary: 'Passing these sample checks does not establish production security or sustained improvement.', test_ids: ['sample-old-request', 'sample-new-operation', 'sample-current-epoch'], evidence_ids: ['sample-evidence-03'] } });
add(0, 'comparison', 'verifier', 'verify', 'Compare the parent and candidate under matching resource conditions.', 'Illustrative comparison only; these values are not research results.', { reply_to: ['sample-key-12'], candidate: { version_id: 'sample-handler-02', parent_version_id: 'sample-handler-00' }, comparison: { baseline_version_id: 'sample-handler-00', candidate_version_id: 'sample-handler-02', spec_version: 'sample-policy-v1', test_set_version: 'sample-evaluation-v1', model_config_id: 'sample-frozen-model', resource_conditions: { profile_id: 'sample-resource-profile-v1', matched: true }, metrics: [{ metric_id: 'authority-violations', label: 'Authority violations', unit: 'cases', baseline: 1, candidate: 0, direction: 'lower' }, { metric_id: 'normal-success', label: 'Legitimate operations', unit: 'cases', baseline: 4, candidate: 4, direction: 'higher' }], conclusion: 'improved', explanation: 'Authored fixture demonstrating comparable fields. No model call or experimental evaluation produced these values.' } });
add(0, 'decision', 'system', 'retain', 'Retain candidate 02 for this sample task.', 'The acceptance reason, parent version and player source stay linked.', { reply_to: ['sample-key-12', 'sample-key-13'], candidate: { version_id: 'sample-handler-02', parent_version_id: 'sample-handler-00' }, decision: { outcome: 'retained', reason: 'The specified sample checks pass and the comparison preserves legitimate access. This illustrates a retention record, not proven ongoing evolution.', supporting_record_ids: ['sample-key-12', 'sample-key-13'] } });
add(0, 'discussion', 'seeker', 'explore', 'A retained version can still face another question.', 'A later sample note arrives without declaring another success.', { reply_to: ['sample-key-14'], candidate: { version_id: 'sample-handler-02', parent_version_id: 'sample-handler-00' }, public_text: 'Next, try a permitted duplicate sequence. The retained result remains scoped to its recorded specification and checks.' });
add(1, 'hypothesis', 'seeker', 'explore', 'Could an invalid PQ proof trigger a fallback?', 'A proposed failure, pending verification.', { public_text: 'Select the allowed invalid-proof fixture. Observe whether the handler attempts the forbidden legacy acceptance path.' });
add(1, 'experiment', 'system', 'explore', 'The bounded negative case is executed.', 'The selected ML-DSA-negative fixture and a valid control are both included.', { reply_to: ['sample-fallback-01'], test_ids: ['sample-pq-negative', 'sample-pq-control'] });
add(1, 'verification', 'verifier', 'verify', 'The proposed failure is not reproduced.', 'Illustrative result: invalid proof is rejected; the valid control succeeds.', { assertion: 'refuted', reply_to: ['sample-fallback-01', 'sample-fallback-02'], test_ids: ['sample-pq-negative', 'sample-pq-control'], evidence: [proof('sample-evidence-04')], verification: { outcome: 'passed', summary: 'No authorized policy violation was observed in these sample cases. No repair is justified by this result.', test_ids: ['sample-pq-negative', 'sample-pq-control'], evidence_ids: ['sample-evidence-04'] } });
add(1, 'comparison', 'verifier', 'verify', 'No improvement is recorded.', 'The comparison retains an unchanged outcome.', { reply_to: ['sample-fallback-03'], comparison: { baseline_version_id: 'sample-fallback-parent', candidate_version_id: 'sample-fallback-parent', spec_version: 'sample-policy-v1', test_set_version: 'sample-fallback-tests-v1', model_config_id: 'sample-frozen-model', resource_conditions: { profile_id: 'sample-resource-profile-v1', matched: true }, metrics: [{ metric_id: 'new-failures', label: 'Distinct failures', unit: 'cases', baseline: 0, candidate: 0, direction: 'higher' }], conclusion: 'unchanged', explanation: 'There is no accepted new method in this authored example. An unchanged result is a valid research outcome.' } });
add(1, 'decision', 'system', 'retain', 'No candidate is retained.', 'This investigation ends without a new finding or repair.', { reply_to: ['sample-fallback-03', 'sample-fallback-04'], decision: { outcome: 'deferred', reason: 'Keep the record and await a supported counterexample.', supporting_record_ids: ['sample-fallback-03', 'sample-fallback-04'] } });
add(2, 'hypothesis', 'seeker', 'explore', 'A replay still needs a test.', 'This sample proposal has not been verified.', { public_text: 'Hold the exact operation bytes and deliver them again after a successful state transition.' });
add(2, 'experiment', 'system', 'explore', 'The attempt encountered an execution error.', 'No signature or authority conclusion can be drawn.', { reply_to: ['sample-paused-01'], public_text: 'An illustrative execution interruption. A failed process must not be counted as a correctly rejected attack.' });
add(2, 'verification', 'verifier', 'verify', 'The result is inconclusive.', 'Execution did not complete; research is paused.', { assertion: 'inconclusive', reply_to: ['sample-paused-02'], test_ids: ['sample-replay'], evidence: [proof('sample-evidence-05')], verification: { outcome: 'error', summary: 'Infrastructure error, not a valid signature rejection or successful repair.', test_ids: ['sample-replay'], evidence_ids: ['sample-evidence-05'] } });

export function createPreviewAdapter(state = 'ready', { experimentState = null } = {}) {
  let latestVisible = false;
  const publicExperiment = item => ({ ...item, ...(experimentState ? { state: experimentState } : state === 'paused' ? { state: 'paused' } : {}) });
  const envelope = value => ({ schema_version: SCHEMA_VERSION, mode: 'example', ...value });
  async function tick(signal) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    if (state === 'loading') return new Promise((resolve, reject) => signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }));
    await new Promise(resolve => setTimeout(resolve, 180));
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    if (state === 'error') throw new Error('Authored preview failure.');
  }
  return {
    mode: 'example', connected: state !== 'disconnected',
    async listExperiments({ cursor = null, signal } = {}) {
      await tick(signal);
      return envelope({ experiments: state === 'empty' ? [] : cursor ? [] : experiments.map(publicExperiment), next_cursor: null });
    },
    async listRecords({ experimentId, cursor = null, afterSequence = null, limit = 8, signal } = {}) {
      await tick(signal);
      if (state === 'refresh-error' && afterSequence !== null) throw new Error('Authored preview refresh failure.');
      if (afterSequence !== null) latestVisible = true;
      const available = records.filter(item => item.experiment_id === experimentId && (latestVisible || item.record_id !== 'sample-key-15'));
      const start = afterSequence !== null ? available.findIndex(item => item.sequence > afterSequence) : cursor === null ? 0 : Number(cursor);
      const page = start < 0 ? [] : available.slice(start, start + limit);
      return envelope({ experiment: publicExperiment(experiments.find(item => item.experiment_id === experimentId)), records: page, next_cursor: start >= 0 && start + limit < available.length ? String(start + limit) : null });
    },
  };
}

export const previewRecords = records;

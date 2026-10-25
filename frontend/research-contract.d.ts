/** Public display data only. No service endpoint is prescribed. */
export type PublicId = string;
export type RecordType = 'contribution' | 'hypothesis' | 'discussion' | 'experiment' | 'finding' | 'repair' | 'verification' | 'comparison' | 'decision';
export type Speaker = 'seeker' | 'keeper' | 'verifier' | 'contributor' | 'system';
export type Phase = 'observe' | 'explore' | 'find' | 'repair' | 'verify' | 'retain';
export interface Experiment {
  experiment_id: PublicId;
  chapter: string;
  title: string;
  summary: string;
  state: 'queued' | 'running' | 'completed' | 'failed' | 'paused';
  spec_version: PublicId;
}
export interface Candidate { version_id: PublicId; parent_version_id: PublicId | null }
export interface Evidence { evidence_id: PublicId; label: string; href: string | null }
export interface Contribution {
  contribution_id: PublicId;
  submission_id: PublicId;
  contributor_id: PublicId;
  kind: 'submission' | 'discovery' | 'reduction' | 'reverification' | 'regression_report';
  origin_record_id: PublicId;
}
export interface Verification {
  outcome: 'passed' | 'failed' | 'error' | 'unsupported' | 'inconclusive';
  summary: string;
  test_ids: PublicId[];
  evidence_ids: PublicId[];
}
export interface Comparison {
  baseline_version_id: PublicId;
  candidate_version_id: PublicId;
  spec_version: PublicId;
  test_set_version: PublicId;
  model_config_id: PublicId;
  /** Opaque public profile; matching is reported by the backend, not certified by the UI. */
  resource_conditions: { profile_id: PublicId; matched: boolean };
  metrics: Array<{ metric_id: PublicId; label: string; unit: string; baseline: number; candidate: number; direction: 'higher' | 'lower' | 'neutral' }>;
  /** Must be incomparable when resource_conditions.matched is false. */
  conclusion: 'improved' | 'regressed' | 'unchanged' | 'incomparable';
  explanation: string;
}
export interface Decision {
  outcome: 'retained' | 'rejected' | 'superseded' | 'deferred';
  reason: string;
  supporting_record_ids: PublicId[];
}
export interface PublicRecord {
  experiment_id: PublicId;
  record_id: PublicId;
  /** Stable positive safe integer, unique within the experiment; gaps allowed. */
  sequence: number;
  type: RecordType;
  speaker: Speaker;
  /** Repeatable classification, never a fixed step index. */
  phase: Phase;
  title: string;
  summary: string;
  /** Intentionally public utterance/observation, plain text, not private reasoning. */
  public_text: string;
  assertion: 'unverified' | 'supported' | 'refuted' | 'inconclusive' | 'not_applicable';
  source: { origin: 'model_public' | 'program' | 'player_submission' | 'example'; source_id: PublicId; model_config_id: PublicId | null };
  reply_to: PublicId[];
  test_ids: PublicId[];
  candidate: Candidate | null;
  verification: Verification | null;
  evidence: Evidence[];
  contributions: Contribution[];
  comparison: Comparison | null;
  decision: Decision | null;
}
export interface Envelope { schema_version: 'evorph.public-research.v2'; mode: 'live' | 'example' }
export interface ExperimentPage extends Envelope { experiments: Experiment[]; next_cursor: string | null }
export interface RecordPage extends Envelope { experiment: Experiment; records: PublicRecord[]; next_cursor: string | null }
export interface PublicResearchAdapter {
  mode: 'live' | 'example';
  connected: boolean;
  listExperiments(options: { cursor: string | null; limit: number; signal: AbortSignal }): Promise<ExperimentPage>;
  listRecords(options: { experimentId: PublicId; cursor: string | null; afterSequence: number | null; limit: number; signal: AbortSignal }): Promise<RecordPage>;
}

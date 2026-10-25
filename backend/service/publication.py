from evorph.contracts import SPEC, canonical, digest

SCHEMA_VERSION = 'evorph.public-research.v2'


def approval_digest(event):
    return digest(canonical(event))


def experiment(research, batch_id):
    row, snapshot = research.batch(batch_id)
    return {'experiment_id': research.alias('experiment', batch_id), 'chapter': 'Retired Key',
            'title': 'Migration research', 'summary': 'Recorded experiments under a fixed migration specification.',
            'state': row['state'], 'spec_version': SPEC['version']}


def record(research, batch_id, event, approved_digests):
    if approval_digest(event) not in approved_digests:
        return None
    kind, body = event['kind'], event['body']
    if kind not in ('hypothesis', 'repair', 'verification', 'comparison', 'decision', 'contribution', 'finding', 'experiment'):
        return None
    _, snapshot = research.batch(batch_id)
    alias = research.alias
    public_id = alias('record', event['record_id'])
    result = {'experiment_id': alias('experiment', batch_id), 'record_id': public_id,
              'sequence': event['sequence'], 'type': kind, 'speaker': 'system', 'phase': 'observe',
              'title': 'Research record', 'summary': 'Recorded research activity.', 'public_text': '',
              'assertion': 'not_applicable',
              'source': {'origin': 'example' if snapshot['mode'] == 'example' else 'program',
                         'source_id': public_id, 'model_config_id': None},
              'reply_to': [], 'test_ids': [], 'candidate': None, 'verification': None,
              'evidence': [], 'contributions': [], 'comparison': None, 'decision': None}
    if kind in ('hypothesis', 'repair'):
        statement = body['statement']
        summary = statement if len(statement.encode('utf-16-le')) <= 4000 else statement[:999] + '…'
        result.update(speaker='seeker' if kind == 'hypothesis' else 'keeper', phase='explore' if kind == 'hypothesis' else 'repair',
                      title='Candidate challenge' if kind == 'hypothesis' else 'Candidate repair',
                      summary=summary, public_text=statement, assertion='unverified')
        result['source']['model_config_id'] = alias('model', batch_id)
        if snapshot['mode'] != 'example':
            result['source']['origin'] = 'model_public'
        if kind == 'repair':
            result['candidate'] = {'version_id': alias('version', body['candidate_id']),
                                   'parent_version_id': alias('version', body['parent_id'])}
            result['reply_to'] = [alias('record', body['reply_to'])]
    elif kind == 'experiment':
        result.update(title='Research paused' if body['state'] == 'paused' else 'Research execution stopped',
                      summary='Research stopped before a conclusive result; existing records remain available.')
    elif kind == 'verification':
        test_id, evidence_id = alias('test', body['job_id']), alias('evidence', body['artifact_id'])
        text = 'The tested trace matches the fixed reference.' if body['matches_reference'] else 'The tested trace differs from the fixed reference.'
        result.update(speaker='verifier', phase='verify', title='Fixed-program verification', summary=text,
                      public_text=text, assertion='supported', test_ids=[test_id])
        result['candidate'] = {'version_id': alias('version', body['policy_id']),
                               'parent_version_id': None if body['parent_id'] is None else alias('version', body['parent_id'])}
        result['evidence'] = [{'evidence_id': evidence_id, 'label': 'Recorded test evidence', 'href': None}]
        result['verification'] = {'outcome': 'passed' if body['matches_reference'] else 'failed',
                                  'summary': text, 'test_ids': [test_id], 'evidence_ids': [evidence_id]}
    elif kind == 'comparison':
        method = body['metric'] == 'verified_traces'
        label = 'Distinct verified traces' if method else 'Cases matching the reference'
        result.update(speaker='verifier', phase='verify', title='Search method comparison' if method else 'Policy comparison',
                      summary='Compared under the recorded test conditions.', assertion='supported')
        result['reply_to'] = [alias('record', ref) for ref in body['supporting']]
        result['comparison'] = {'baseline_version_id': alias('version', body['baseline_id']),
                                'candidate_version_id': alias('version', body['candidate_id']),
                                'spec_version': SPEC['version'], 'test_set_version': alias('tests', body['test_set']),
                                'model_config_id': alias('model', batch_id),
                                'resource_conditions': {'profile_id': alias('profile', batch_id), 'matched': body['matched']},
                                'metrics': [{'metric_id': body['metric'], 'label': label, 'unit': 'count',
                                             'baseline': body['baseline'], 'candidate': body['candidate'], 'direction': body['direction']}],
                                'conclusion': body['conclusion'],
                                'explanation': 'Fixed search attempts count distinct reproductions, not independent root causes.' if method
                                               else 'Matching requires the recorded normal flows and retirement checks.'}
    elif kind == 'decision':
        reason = {'no_verified_discrepancy': 'No verified discrepancy was found in these attempts.',
                  'fixed_search_comparison': 'Search method selection follows the fixed-budget comparison.',
                  'normal_and_retirement_cases': 'Policy selection requires all recorded normal and retirement checks.'}[body['reason']]
        result.update(phase='retain', title='Recorded version decision', summary=reason, public_text=reason)
        if body['candidate_id'] is not None:
            result['candidate'] = {'version_id': alias('version', body['candidate_id']),
                                   'parent_version_id': alias('version', body['parent_id'])}
        result['decision'] = {'outcome': body['outcome'], 'reason': reason,
                              'supporting_record_ids': [alias('record', ref) for ref in body['supporting']]}
    elif kind == 'contribution':
        result.update(speaker='contributor', title='Submitted challenge', summary='A participant supplied a bounded challenge.', assertion='unverified')
        if snapshot['mode'] != 'example':
            result['source']['origin'] = 'player_submission'
        result['source']['source_id'] = alias('submission', body['submission_id'])
        result['contributions'] = [{'contribution_id': alias('contribution', event['record_id']),
                                   'submission_id': alias('submission', body['submission_id']),
                                   'contributor_id': alias('contributor', body['owner_id']), 'kind': 'submission', 'origin_record_id': public_id}]
    elif kind == 'finding':
        result.update(speaker='verifier', phase='find', title='Verified experimental discrepancy',
                      summary='A fixed-program result differs from the reference; this is not a claim of a new real-world vulnerability.', assertion='supported')
        result['reply_to'] = [alias('record', body['verification_id']), alias('record', body['origin_record_id'])]
        if body['submission_id'] is not None and body.get('contribution_kind') is not None:
            owner = research.store.connection.execute('SELECT owner_id FROM research_submissions WHERE submission_id=?', (body['submission_id'],)).fetchone()[0]
            result['contributions'] = [{'contribution_id': alias('contribution', event['record_id']),
                                       'submission_id': alias('submission', body['submission_id']),
                                       'contributor_id': alias('contributor', owner), 'kind': body['contribution_kind'],
                                       'origin_record_id': alias('record', body['origin_record_id'])}]
    return result


def project_records(research, batch_id, approved_digests):
    result = []
    for event in research.journal(batch_id):
        public = record(research, batch_id, event, approved_digests)
        if public is not None:
            result.append(public)
    return result

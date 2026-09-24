"""Isolated provider fixtures verify revision boundaries, not real model quality."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.generation_agent import authorize_resume
from app.models import LearningAsset
from app.question_revision import RevisionRequest, prepare_revision, partial_content, validate_revision_asset
from app.store import Conflict, dumps
from tests.test_generation_agent import agent_case, SOURCE, AUTHOR_SECRET


def revision(rig, *, index=1, mode='rewrite', difficulty=None, key='single-revision-1'):
    row = rig.contents()[0]
    body = RevisionRequest(version=row['version'], instruction='Use a revised concrete scenario.',
                           mode=mode, difficulty=difficulty, request_key=key)
    payload = prepare_revision(rig.store, row, f'q{index+1}', body)
    job, _ = rig.store.job(key, 'revise_question', payload)
    return job, payload


class RevisionWire:
    def __init__(self, old, *, reject=False, stale=None, cancel=None, explanation=False,
                 preserve_focus=True, review_override=None, missing_once=None):
        self.old, self.reject, self.stale, self.cancel = old, reject, stale, cancel
        self.explanation = explanation
        self.preserve_focus, self.review_override = preserve_focus, review_override or {}
        self.missing_once = missing_once
        self.contracts = []

    def __call__(self, request):
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        self.contracts.append(deepcopy(contract))
        if contract['task'] == 'agent_author' and self.explanation:
            result = self.old.response({'explanation': 'Revised explanation based on the original course rule.'})
        else:
            result = self.old(request)
        body = result.json()
        raw = json.loads(body['choices'][0]['message']['content'])
        if contract['task'] == 'agent_author' and not self.explanation:
            raw['questions'][0]['stem'] += ' Revised scenario.'
            # Deliberate attempted mutation: host must retain original context.
            raw['sections'] = [{'heading': 'Changed context', 'text': 'Unwanted replacement.', 'citation_ids': [SOURCE['id']]}]
        if contract['task'] == 'agent_review':
            raw.update(self.old.quality_fields(contract))
            raw.update(revision_instructions_followed=not self.reject,
                       original_learning_focus_preserved=self.preserve_focus)
            raw.update(deepcopy(self.review_override))
            if self.missing_once:
                raw.pop(self.missing_once, None)
                self.missing_once = None
            if self.stale:
                self.stale(); self.stale = None
        if self.cancel:
            self.cancel(); self.cancel = None
        return self.old.response(raw)


def install(rig, **options):
    wire = RevisionWire(rig.wire, **options)
    rig.providers.client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    return wire


def execute(rig, job):
    asyncio.run(rig.pipeline.run(job['id']))
    return rig.store.one('SELECT * FROM jobs WHERE id=?', (job['id'],))


def test_only_selected_question_changes_with_immutable_context_history_and_fresh_review(agent_case):
    rig = agent_case(); assert rig.run()['status'] == 'succeeded'
    before = deepcopy(rig.asset())
    job, payload = revision(rig)
    wire = install(rig)
    result = execute(rig, job)
    assert result['status'] == 'succeeded', result['error']
    after = rig.asset()
    assert after['questions'][0] == before['questions'][0]
    assert after['questions'][2] == before['questions'][2]
    assert after['questions'][1]['stem'] != before['questions'][1]['stem']
    assert after['sections'] == before['sections']
    assert after['learning_objectives'] == before['learning_objectives']
    assert rig.contents()[0]['version'] == 2 and rig.contents()[0]['status'] == 'draft'
    old = rig.store.one('SELECT asset FROM revisions WHERE content_id=? AND version=1', (payload['content_id'],))
    assert json.loads(old['asset']) == before
    assert len(wire.contracts) == 3
    solve = next(c for c in wire.contracts if c['task'] == 'agent_solve')
    assert AUTHOR_SECRET not in dumps(solve)
    assert len(rig.retrieval_calls) == 1
    assert not rig.store.one("SELECT 1 FROM calls WHERE job_id=? AND capability='embedding'", (job['id'],))
    config = json.loads(rig.contents()[0]['config'])
    assert config['question_revision']['review_scope'] == 'selected_question_only'
    assert 'difficulty_acceptance' not in config


def test_explanation_edit_preserves_every_other_question_field(agent_case):
    rig = agent_case(); rig.run(); before = deepcopy(rig.asset())
    job, _ = revision(rig, mode='explanation')
    install(rig, explanation=True)
    result = execute(rig, job)
    assert result['status'] == 'succeeded', result['error']
    after = rig.asset()
    explanation = after['questions'][1].pop('explanation')
    previous = before['questions'][1].pop('explanation')
    assert after == before and explanation != previous


def test_difficulty_change_preserves_slot_order_and_other_levels(agent_case):
    rig = agent_case(distribution={'easy': 1, 'medium': 1, 'hard': 1}); rig.run()
    job, _ = revision(rig, index=0, difficulty='hard')
    install(rig)
    result = execute(rig, job)
    assert result['status'] == 'succeeded', result['error']
    assert [q['difficulty'] for q in rig.asset()['questions']] == ['hard','medium','hard']
    row = rig.contents()[0]
    validate_revision_asset(LearningAsset.model_validate(rig.asset()), json.loads(row['config']), json.loads(row['sources']))


def test_bad_revision_instruction_check_never_publishes(agent_case):
    rig = agent_case(); rig.run(); before = deepcopy(rig.asset())
    job, _ = revision(rig); install(rig, reject=True)
    result = execute(rig, job)
    assert result['status'] == 'failed'
    assert rig.contents()[0]['version'] == 1 and rig.asset() == before
    evidence = json.loads(rig.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (job['id'],))['evidence'])
    assert 'revision_instructions_followed' in evidence['agent']['slots'][0]['attempts'][0]['feedback']['issues'] or 'review_reported_issues' in evidence['agent']['slots'][0]['attempts'][0]['feedback']['issues']


def test_edit_conflict_does_not_overwrite_newer_version(agent_case):
    rig = agent_case(); rig.run(); before = deepcopy(rig.asset())
    job, payload = revision(rig)
    install(rig, stale=lambda: rig.store.execute('UPDATE contents SET version=2 WHERE id=?', (payload['content_id'],)))
    result = execute(rig, job)
    assert result['status'] == 'failed' and rig.asset() == before
    assert rig.contents()[0]['version'] == 2


def test_cancel_revision_preserves_response_and_can_resume_without_reauthoring(agent_case):
    rig = agent_case(); rig.run(); job, _ = revision(rig)
    wire = install(rig, cancel=lambda: rig.pipeline.request_cancel(job['id']))
    result = execute(rig, job)
    assert result['status'] == 'cancelled' and rig.contents()[0]['version'] == 1
    authorize_resume(rig.store, job['id'])
    result = execute(rig, job)
    assert result['status'] == 'succeeded', result['error']
    assert [c['task'] for c in wire.contracts].count('agent_author') == 1


def test_partial_results_exclude_rejected_and_incomplete_slots(agent_case):
    rig = agent_case(wire_options={'review_fail_at': 2})
    assert rig.run()['status'] == 'failed'
    partial = partial_content(rig.store, rig.job_id)
    assert partial['completed'] == 1 and partial['total'] == 3 and partial['partial']
    assert len(partial['asset']['questions']) == 1
    assert partial['asset']['questions'][0]['slot_id'] == 'q1'
    assert partial['sources'] == [SOURCE]
    assert not rig.contents()


def test_stale_revision_and_explanation_difficulty_change_rejected(agent_case):
    rig = agent_case(); rig.run(); row = rig.contents()[0]
    with pytest.raises(Conflict):
        prepare_revision(rig.store, row, 'q1', RevisionRequest(version=2, instruction='Rewrite it', request_key='stale-version'))
    with pytest.raises(ValueError):
        prepare_revision(rig.store, row, 'q1', RevisionRequest(version=1, mode='explanation', difficulty='easy', instruction='Explain it', request_key='different-level'))


def test_frozen_revision_needs_only_three_calls_without_embedding_headroom(agent_case):
    rig = agent_case(); rig.run(); job, _ = revision(rig)
    rig.settings.agent_max_calls = 3
    rig.settings.max_daily_calls = rig.store.one('SELECT count(*) AS n FROM calls')['n'] + 3
    wire = install(rig)
    result = execute(rig, job)
    assert result['status'] == 'succeeded', result['error']
    assert len(wire.contracts) == 3


def revision_evidence(rig, job):
    return json.loads(rig.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (job['id'],))['evidence'])


def test_checked_revision_retains_all_quality_fields_and_revision_flags(agent_case):
    rig = agent_case(); rig.run(); job, _ = revision(rig)
    wire = install(rig)
    result = execute(rig, job)
    assert result['status'] == 'succeeded', result['error']
    contract = next(c for c in wire.contracts if c['task'] == 'agent_review')
    assert {'question_checks', 'explanation_issues', 'revision_instructions_followed',
            'original_learning_focus_preserved'} <= set(contract['schema']['required'])
    attempt = revision_evidence(rig, job)['agent']['slots'][0]['attempts'][0]
    assert attempt['review_question_checks'] == {'condition_issues': [], 'option_checks': []}
    assert attempt['review_explanation_issues'] == []
    assert attempt['review']['response']['revision_instructions_followed'] is True
    assert attempt['review']['response']['original_learning_focus_preserved'] is True


@pytest.mark.parametrize('options', [
    {'preserve_focus': False},
    {'review_override': {'explanation_issues': ['The revised explanation contradicts the condition.']}},
    {'review_override': {'question_checks': {'condition_issues': ['An initial value is missing.'], 'option_checks': []}}},
])
def test_revision_focus_and_new_quality_failures_never_publish(agent_case, options):
    rig = agent_case(); rig.run(); before = deepcopy(rig.asset())
    job, _ = revision(rig); install(rig, **options)
    result = execute(rig, job)
    assert result['status'] == 'failed'
    assert rig.contents()[0]['version'] == 1 and rig.asset() == before
    state = revision_evidence(rig, job)['agent']
    assert state['status'] == 'quality_failed'
    assert all(attempt['status'] == 'rejected' for attempt in state['slots'][0]['attempts'])


@pytest.mark.parametrize('missing', ['question_checks', 'revision_instructions_followed', 'original_learning_focus_preserved'])
def test_revision_schema_repair_keeps_extended_contract_and_uses_one_extra_call(agent_case, missing):
    rig = agent_case(); rig.run(); job, _ = revision(rig)
    wire = install(rig, missing_once=missing)
    result = execute(rig, job)
    assert result['status'] == 'succeeded', result['error']
    assert len(wire.contracts) == 4
    review_contracts = [c for c in wire.contracts if c['task'] == 'agent_review']
    assert len(review_contracts) == 2
    assert review_contracts[0]['schema'] == review_contracts[1]['schema']
    assert review_contracts[0]['revision_check'] == review_contracts[1]['revision_check']
    assert missing in review_contracts[1]['schema_repair']['required_top_level_fields']
    attempt = revision_evidence(rig, job)['agent']['slots'][0]['attempts'][0]
    assert attempt['review_schema_repair']['status'] == 'validated'
    assert len(attempt['review_schema_repair']['history']) == 1


def test_legacy_revision_checkpoint_resumes_without_new_quality_schema(agent_case):
    rig = agent_case(); rig.run(); job, _ = revision(rig)
    wire = install(rig, cancel=lambda: rig.pipeline.request_cancel(job['id']))
    assert execute(rig, job)['status'] == 'cancelled'
    evidence = revision_evidence(rig, job)
    evidence['configuration'].pop('review_policy_revision')
    evidence['configuration'].pop('role_schema_repair_policy')
    rig.store.save_job_evidence(job['id'], evidence)
    authorize_resume(rig.store, job['id'])
    result = execute(rig, job)
    assert result['status'] == 'succeeded', result['error']
    review_contract = next(c for c in wire.contracts if c['task'] == 'agent_review')
    fields = set(review_contract['schema']['properties'])
    assert {'revision_instructions_followed', 'original_learning_focus_preserved'} <= fields
    assert not {'question_checks', 'explanation_issues'} & fields
    assert 'review_policy_revision' not in revision_evidence(rig, job)['configuration']
    assert len(wire.contracts) == 3

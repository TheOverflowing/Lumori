"""Mocked transport checks of runtime safety; these do not measure judge accuracy."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from app.config import Settings
from app.difficulty_shadow import digest, observe, policy, summary
from app.difficulty_shadow_v2 import build_case
from app.generation_agent import GenerationAgent, authorize_resume
from app.models import GenerateRequest
from app.store import LocalCallLimit
from test_generation_agent import agent_case, AgentWire, AUTHOR_SECRET, DESIGN_SECRET


class ReviewWire:
    def __init__(self, primary, mode='ok', callback=None):
        self.primary = primary
        self.mode = mode
        self.callback = callback
        self.calls = []
        self.urls = []

    def __call__(self, request):
        body = json.loads(request.content)
        payload = json.loads(body['messages'][1]['content'])
        if 'task' in payload:
            return self.primary(request)
        self.calls.append(body)
        self.urls.append(str(request.url))
        if self.mode == 'cancel':
            raise asyncio.CancelledError()
        if self.mode == 'http' or (self.mode == 'verification_http' and len(self.calls) == 2):
            return httpx.Response(503, text='fixture unavailable')
        if self.mode == 'invalid':
            return AgentWire.response({'invalid': True})
        if self.mode == 'verification_invalid' and 'documents' in payload:
            return AgentWire.response({'invalid': True})
        if 'documents' not in payload:
            if self.callback:
                self.callback()
            return AgentWire.response({'eligibility': 'eligible', 'eligibility_reason': 'Fixture profile clear',
                'solution': 'Apply the supplied course rules.',
                'tasks': [{'request_ids': [payload['catalogue'][-1]['id']], 'condition_ids': [],
                           'provided_ids': [], 'residual_work': 'Apply the familiar course rules', 'mode': 'apply_familiar'}],
                'decision': {'candidates': ['easy', 'medium'] if self.mode == 'boundary' else ['medium'],
                             'boundary_reason': 'Fixture application boundary', 'hard_task_index': None}})
        catalogue = [{'id': segment['id'], 'document_id': document}
                     for document, segments in payload['documents'].items()
                     for segment in segments if 'id' in segment]
        def check(*ids, status='pass'):
            return {'status': status, 'reason': 'Fixture check of both inputs',
                    'evidence_ids': [next(item['id'] for item in catalogue if item['document_id']==key)
                                     for key in ids]}
        checks = {
            'conditions_complete': check('student'),
            'solution_correct': check('analysis', 'student'),
            'reference_correct': check('reference_answer', 'student'),
            'explanation_consistent': check('reference_explanation', 'student'),
            'source_support': check('source:agent-source-1', 'student'),
            'demand_supported': check('analysis', 'student', 'learner_profile')}
        checks['source_support'].update(source_ids=checks['source_support']['evidence_ids'][:1],
                                        claim_ids=checks['source_support']['evidence_ids'][1:])
        checks['source_support'].pop('evidence_ids')
        checks['demand_supported'].update(analysis_ids=checks['demand_supported']['evidence_ids'][:1],
            student_ids=checks['demand_supported']['evidence_ids'][1:2],
            profile_ids=checks['demand_supported']['evidence_ids'][2:])
        checks['demand_supported'].pop('evidence_ids')
        if self.mode == 'content_issue':
            checks['reference_correct']['status'] = 'fail'
        if self.mode in ('solver_issue','solver_uncertain'):
            checks['solution_correct']['status'] = 'fail' if self.mode=='solver_issue' else 'uncertain'
        return AgentWire.response({'checks': checks})


def attach(rig, mode='ok', callback=None):
    wire = ReviewWire(rig.wire, mode, callback)
    asyncio.run(rig.providers.client.aclose())
    rig.providers.client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    return wire


def rig_for(agent_case, **settings):
    return agent_case(count=1, settings_options={'difficulty_shadow_mode': 'shadow', **settings})


def test_default_protocol_and_off_make_no_observer_calls(agent_case):
    assert Settings().difficulty_shadow_protocol == 'review_v2'
    assert Settings().difficulty_shadow_analyst_model == ''
    assert Settings().difficulty_shadow_analyst_effort == 'none'
    assert Settings().difficulty_shadow_verifier_effort == 'none'
    rig = agent_case(count=1)
    wire = attach(rig)
    assert rig.run()['status'] == 'succeeded'
    assert not wire.calls and 'difficulty_shadow' not in rig.evidence()


def test_new_review_is_two_calls_bound_blind_and_advisory(agent_case):
    baseline = agent_case(count=1); assert baseline.run()['status'] == 'succeeded'
    rig = rig_for(agent_case); wire = attach(rig)
    assert rig.run()['status'] == 'succeeded', rig.evidence()
    assert len(wire.calls) == 2 and rig.asset() == baseline.asset()
    blind = json.loads(wire.calls[0]['messages'][1]['content'])
    assert not {'reference_answer', 'sources', 'target_difficulty', 'difficulty_design'} & blind.keys()
    assert all(secret not in json.dumps(blind) for secret in (AUTHOR_SECRET, DESIGN_SECRET))
    for call in wire.calls:
        assert call['max_tokens'] == 2400 and call['thinking'] == {'type': 'disabled'}
    state = rig.evidence()['difficulty_shadow']; question = state['questions'][0]
    assert state['protocol'] == 'review_v2' and state['status'] == 'completed'
    assert state['coverage'] == {'total_questions': 1, 'sampled_questions': 1, 'assessed_questions': 1}
    assert question['decision']['recommendation'] == 'rework_candidate'
    assert question['decision']['enforcement_enabled'] is False
    assert question['analysis']['result']['case_sha256'] == question['verification']['result']['case_sha256']
    assert state['asset_sha256'] == state['observed_asset_sha256'] == digest(rig.asset())
    result = summary(rig.evidence())['questions'][0]
    assert result['decision'] == question['decision'] and result['candidates'] == ['medium']
    assert result['manual_review_recommended'] and len(result['content_checks']) == 6
    rig.run(); assert len(wire.calls) == 2


@pytest.mark.parametrize('options', [
    {'difficulty_shadow_max_calls': 1}, {'agent_max_calls': 5}, {'max_daily_calls': 5}])
def test_whole_round_budget_preflight_spends_zero_when_only_one_call_remains(agent_case, options):
    rig = rig_for(agent_case, **options); wire = attach(rig)
    assert rig.run()['status'] == 'succeeded'
    assert wire.calls == [] and len(rig.contents()) == 1
    state = rig.evidence()['difficulty_shadow']
    assert state['questions'][0]['decision']['status'] == 'unavailable'
    assert state['coverage']['assessed_questions'] == 0


@pytest.mark.parametrize('mode,calls,status', [
    ('invalid', 1, 'unavailable'), ('http', 1, 'unavailable'),
    ('verification_http', 2, 'unavailable'), ('verification_invalid', 2, 'unavailable'),
    ('content_issue', 2, 'content_issue'),
    ('solver_issue', 2, 'insufficient_evidence'), ('solver_uncertain', 2, 'insufficient_evidence'),
    ('boundary', 2, 'boundary')])
def test_incomplete_invalid_and_content_issues_never_clear(agent_case, mode, calls, status):
    rig = rig_for(agent_case); wire = attach(rig, mode)
    assert rig.run()['status'] == 'succeeded' and len(rig.contents()) == 1
    state = rig.evidence()['difficulty_shadow']; decision = state['questions'][0]['decision']
    assert len(wire.calls) == calls and decision['status'] == status
    assert decision['recommendation'] == 'manual_review'
    assert state['coverage']['assessed_questions'] == 0
    if mode.startswith('solver_'):
        checks=state['questions'][0]['verification']['result']['checks']
        assert checks['reference_correct']['status']=='pass'
        assert checks['solution_correct']['status'] in ('fail','uncertain')
        assert summary(rig.evidence())['questions'][0]['review_flags']==['insufficient_evidence']


def test_kill_switch_is_checked_between_stages(agent_case):
    rig = rig_for(agent_case)
    wire = attach(rig, callback=lambda: setattr(rig.settings, 'difficulty_shadow_mode', 'off'))
    assert rig.run()['status'] == 'succeeded'
    question = rig.evidence()['difficulty_shadow']['questions'][0]
    assert len(wire.calls) == 1
    assert question['verification']['reason'] == 'runtime_kill_switch'
    assert question['decision']['status'] == 'unavailable'


@pytest.mark.parametrize('change', ['model','endpoint','analyst_model','analyst_effort','verifier_model','verifier_effort','protocol'])
def test_frozen_endpoint_and_protocol_are_rechecked_between_stages(agent_case, change, monkeypatch):
    rig = rig_for(agent_case)
    def mutate():
        if change == 'model':
            rig.settings.text = replace(rig.settings.text, model='changed-model')
        elif change == 'endpoint':
            rig.settings.text = replace(rig.settings.text, base_url='https://changed.invalid/v1')
        elif change == 'verifier_model':
            rig.settings.difficulty_shadow_verifier_model = 'changed-verifier'
        elif change == 'analyst_model':
            rig.settings.difficulty_shadow_analyst_model = 'changed-analyst'
        elif change == 'analyst_effort':
            rig.settings.difficulty_shadow_analyst_effort = 'low'
        elif change == 'verifier_effort':
            rig.settings.difficulty_shadow_verifier_effort = 'high'
        else:
            import app.difficulty_review_v2 as protocol
            monkeypatch.setattr(protocol, 'protocol_fingerprint', lambda: 'changed-protocol')
    wire = attach(rig, callback=mutate)
    assert rig.run()['status'] == 'succeeded'
    question = rig.evidence()['difficulty_shadow']['questions'][0]
    assert len(wire.calls) == 1
    assert question['verification']['reason'] == 'frozen_protocol_unavailable'
    assert question['decision']['status'] == 'unavailable'


def test_verifier_override_uses_same_provider_and_frozen_separate_model(agent_case):
    rig = rig_for(agent_case, difficulty_shadow_verifier_model='fixture-verifier')
    wire = attach(rig)
    assert rig.run()['status'] == 'succeeded' and len(wire.calls) == 2
    assert [call['model'] for call in wire.calls] == ['fixture-author', 'fixture-verifier']
    assert wire.urls == ['https://fixture.invalid/v1/chat/completions'] * 2
    frozen = rig.evidence()['configuration']['difficulty_shadow']
    assert frozen['reviewer_model'] == 'fixture-author' and frozen['verifier_model'] == 'fixture-verifier'
    assert frozen['reviewer_endpoint_signature'] != frozen['verifier_endpoint_signature']
    assert rig.evidence()['difficulty_shadow']['questions'][0]['decision']['status'] == 'assessed'


@pytest.mark.parametrize('effort',['low','high','max'])
def test_verifier_effort_has_independent_output_cap_and_no_temperature(agent_case,effort):
    rig=rig_for(agent_case,difficulty_shadow_verifier_effort=effort)
    wire=attach(rig)
    assert rig.run()['status']=='succeeded' and len(wire.calls)==2
    assert wire.calls[0]['max_tokens']==2400 and wire.calls[0]['thinking']=={'type':'disabled'}
    assert wire.calls[1]['max_tokens']==12288 and wire.calls[1]['thinking']=={'type':'enabled'}
    assert wire.calls[1]['reasoning_effort']==effort and 'temperature' not in wire.calls[1]
    frozen=rig.evidence()['configuration']['difficulty_shadow']
    assert frozen['verifier_effort']==effort
    assert frozen['planned_max_output_tokens']==14688
    question=rig.evidence()['difficulty_shadow']['questions'][0]
    assert question['preflight']['analysis_context_reserve_chars']==2400*4*2
    assert question['preflight']['verification_max_output_tokens']==12288
    assert question['verification']['request_parameters']==frozen['verification_parameters']


def test_invalid_verifier_effort_is_rejected_before_work():
    with pytest.raises(ValueError,match='VERIFIER_EFFORT'):
        policy(Settings(difficulty_shadow_mode='shadow',difficulty_shadow_verifier_effort='ultra'))


def test_analyst_override_isolated_from_verifier_parameters_and_shared_timeout(agent_case):
    rig=rig_for(agent_case,difficulty_shadow_analyst_model='fixture-analyst',
        difficulty_shadow_analyst_effort='low')
    wire=attach(rig)
    assert rig.run()['status']=='succeeded' and len(wire.calls)==2
    analysis,verification=wire.calls
    assert analysis['model']=='fixture-analyst' and analysis['max_tokens']==12288
    assert analysis['thinking']=={'type':'enabled'} and analysis['reasoning_effort']=='low'
    assert 'temperature' not in analysis
    assert verification['model']=='fixture-author' and verification['max_tokens']==2400
    assert verification['thinking']=={'type':'disabled'} and verification['temperature']==0
    assert wire.urls==['https://fixture.invalid/v1/chat/completions']*2
    frozen=rig.evidence()['configuration']['difficulty_shadow']
    assert frozen['analyst_model']=='fixture-analyst' and frozen['verifier_model']=='fixture-author'
    assert frozen['analyst_effort']=='low' and frozen['verifier_effort']=='none'
    assert frozen['analyst_endpoint_signature']==frozen['reviewer_endpoint_signature']
    assert frozen['analyst_endpoint_signature']!=frozen['verifier_endpoint_signature']
    assert frozen['planned_max_output_tokens']==14688 and frozen['timeout']==30
    question=rig.evidence()['difficulty_shadow']['questions'][0]
    assert question['preflight']['analysis_context_reserve_chars']==16000*2
    assert frozen['context_reserve_method']=='bounded_final_json_with_metadata_estimate_v2'
    assert frozen['context_reserve_parameters']['final_json_chars_estimate']==16000
    assert frozen['context_reserve_parameters']['expansion_factor']==2
    assert question['analysis']['request_parameters']==frozen['analysis_parameters']


@pytest.mark.parametrize(('context_chars','max_calls','expected_reason'),[
    (32000,2,'context_limit'),(48000,1,'shadow_budget')])
def test_thinking_analysis_preflights_actual_output_cap_and_two_calls(agent_case,context_chars,max_calls,expected_reason):
    rig=rig_for(agent_case,difficulty_shadow_analyst_effort='low',
        agent_context_chars=context_chars,difficulty_shadow_max_calls=max_calls)
    wire=attach(rig)
    assert rig.run()['status']=='succeeded' and not wire.calls
    question=rig.evidence()['difficulty_shadow']['questions'][0]
    assert question['analysis']['reason']==expected_reason
    assert question['decision']['status']=='unavailable'
    assert question['preflight']['analysis_max_output_tokens']==12288


def test_thinking_final_prompt_overflow_is_rejected_without_truncation(agent_case,monkeypatch):
    import app.difficulty_review_v2 as review
    original=review.verification_messages
    def larger(case,analysis):
        prompts=original(case,analysis)
        # The sizing estimate can be low: emulate an actual expanded final
        # payload and exercise the mandatory second guard before any send.
        prompts[-1]['content'] += 'x'*48000
        return prompts
    monkeypatch.setattr(review,'verification_messages',larger)
    rig=rig_for(agent_case,difficulty_shadow_analyst_effort='low')
    wire=attach(rig)
    assert rig.run()['status']=='succeeded' and len(wire.calls)==1
    question=rig.evidence()['difficulty_shadow']['questions'][0]
    assert question['analysis']['status']=='validated'
    assert question['verification']['status']=='skipped'
    assert question['verification']['reason']=='context_limit'
    assert question['decision']['status']=='unavailable'


@pytest.mark.parametrize(('field','changed'),[
    ('difficulty_shadow_analyst_model','changed-analyst'),('difficulty_shadow_analyst_effort','low')])
def test_analyst_configuration_change_on_resume_skips_without_rebilling(agent_case,field,changed):
    rig=rig_for(agent_case);wire=attach(rig,'cancel')
    try:rig.run()
    except asyncio.CancelledError:pass
    assert rig.evidence()['difficulty_shadow']['status']=='interrupted'
    calls=len(wire.calls)
    setattr(rig.settings,field,changed)
    authorize_resume(rig.store,rig.job_id)
    assert rig.run()['status']=='succeeded' and len(wire.calls)==calls
    assert rig.evidence()['difficulty_shadow']['reason']=='frozen_protocol_unavailable'


@pytest.mark.parametrize('published,target_relation,expected',[
    ('medium','mismatch',False), ('hard','match',True), (None,'mismatch',False)])
def test_published_mismatch_flag_compares_published_grade_not_target(published,target_relation,expected):
    decision = {'status':'assessed','candidates':['medium'],'estimated_difficulty':'medium',
        'target_relation':target_relation,'recommendation':'manual_review','reason_codes':[]}
    result = summary({'difficulty_shadow':{'protocol':'review_v2','questions':[
        {'slot_id':'q1','published_difficulty':published,'decision':decision}]}})
    assert ('published_grade_outside_candidates' in result['questions'][0]['review_flags']) is expected


def test_published_conflict_requires_manual_review_even_when_target_matches():
    decision={'status':'assessed','candidates':['medium'],'estimated_difficulty':'medium',
        'target_relation':'match','recommendation':'no_difficulty_rework','reason_codes':['target_matches']}
    result=summary({'difficulty_shadow':{'protocol':'review_v2','questions':[
        {'slot_id':'q1','published_difficulty':'hard','target_difficulty':'medium','decision':decision}]}})
    assert result['questions'][0]['manual_review_recommended'] is True
    assert result['questions'][0]['review_flags']==['published_grade_outside_candidates']


@pytest.mark.parametrize('invalid_model', [' invalid ', None])
def test_invalid_runtime_verifier_config_on_resume_does_not_fail_primary(agent_case,invalid_model):
    rig=rig_for(agent_case);wire=attach(rig,'cancel')
    try:
        rig.run()
    except asyncio.CancelledError:
        pass
    assert rig.evidence()['difficulty_shadow']['status']=='interrupted'
    before=len(wire.calls)
    rig.settings.difficulty_shadow_verifier_model=invalid_model
    authorize_resume(rig.store,rig.job_id)
    assert rig.run()['status']=='succeeded' and len(rig.contents())==1
    assert len(wire.calls)==before
    assert rig.evidence()['difficulty_shadow']['reason']=='frozen_protocol_unavailable'


def test_reservation_race_does_not_create_synthetic_call_consumption(agent_case, monkeypatch):
    rig = rig_for(agent_case); wire = attach(rig)
    original = rig.store.reserve_call
    def reserve(job_id, capability, model, limit):
        if len(wire.primary.contracts) == 3:
            raise LocalCallLimit('fixture race', scope='daily')
        return original(job_id, capability, model, limit)
    monkeypatch.setattr(rig.store, 'reserve_call', reserve)
    assert rig.run()['status'] == 'succeeded'
    state = rig.evidence()['difficulty_shadow']
    assert not wire.calls and state['calls_reserved'] == 0
    assert state['questions'][0]['analysis']['reason'] == 'reservation_rejected'
    assert rig.evidence()['agent']['call_count'] == 3


def test_interrupted_observer_is_not_reissued_on_resume(agent_case):
    rig = rig_for(agent_case); wire = attach(rig, 'cancel')
    try:
        rig.run()
    except asyncio.CancelledError:
        pass
    evidence = rig.evidence(); assert evidence['difficulty_shadow']['status'] == 'interrupted'
    agent = GenerationAgent(rig.pipeline, GenerateRequest.model_validate(json.loads(rig.job()['payload'])), rig.job_id)
    agent.evidence = evidence; agent.state = evidence['agent']
    asset = deepcopy(agent.state['slots'][0]['accepted_asset'])
    asset.update(title=agent.request.topic, section_scope='shared', visual_prompt='')
    asyncio.run(observe(agent, asset))
    assert len(wire.calls) == 1
    assert evidence['difficulty_shadow']['questions'][0]['decision']['status'] == 'unavailable'
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded' and len(wire.calls) == 1


def test_known_context_overflow_skips_both_stages(agent_case, monkeypatch):
    import app.difficulty_shadow as shadow
    original = shadow.policy
    def tiny(settings, saved_config=None):
        value = original(settings, saved_config)
        if value: value['context_chars'] = 1
        return value
    monkeypatch.setattr(shadow, 'policy', tiny)
    rig = rig_for(agent_case); wire = attach(rig)
    assert rig.run()['status'] == 'succeeded' and not wire.calls
    assert 'context_limit' in rig.evidence()['difficulty_shadow']['questions'][0]['decision']['reason_codes']


def test_timeout_produces_unavailable_review_and_keeps_primary_draft(agent_case):
    rig = rig_for(agent_case, difficulty_shadow_timeout=1)
    wire = ReviewWire(rig.wire)
    async def slow(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])
        if 'task' not in payload:
            await asyncio.sleep(2)
        return wire(request)
    asyncio.run(rig.providers.client.aclose())
    rig.providers.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    assert rig.run()['status'] == 'succeeded' and len(rig.contents()) == 1
    question = rig.evidence()['difficulty_shadow']['questions'][0]
    assert question['analysis']['error_type'] == 'TimeoutError'
    assert question['decision']['status'] == 'unavailable'


def test_sampling_reports_unassessed_questions_and_preflights_each_round(agent_case):
    rig = agent_case(count=2, settings_options={'difficulty_shadow_mode':'shadow',
        'difficulty_shadow_max_questions':2, 'difficulty_shadow_max_calls':3})
    wire = attach(rig)
    assert rig.run()['status'] == 'succeeded' and len(wire.calls) == 2
    state = rig.evidence()['difficulty_shadow']
    assert state['coverage'] == {'total_questions':2, 'sampled_questions':2, 'assessed_questions':1}
    assert state['questions'][1]['decision']['status'] == 'unavailable'
    assert state['questions'][1]['analysis']['reason'] == 'shadow_budget'


@pytest.mark.parametrize('field,value', [('revision','future'), ('protocol','future'),
    ('reviewer_endpoint_signature','changed'), ('max_questions',None), ('max_calls','two'),
    ('context_reserve_method','future'), ('context_reserve_parameters',{'estimated_chars_per_token':1})])
def test_frozen_unknown_protocol_or_model_fails_closed(agent_case, monkeypatch, field, value):
    import app.difficulty_shadow as shadow
    original = shadow.policy
    def different(settings, saved_config=None):
        frozen = original(settings, saved_config)
        if frozen: frozen[field] = value
        return frozen
    monkeypatch.setattr(shadow, 'policy', different)
    rig = rig_for(agent_case); wire = attach(rig)
    assert rig.run()['status'] == 'succeeded' and not wire.calls
    assert rig.evidence()['difficulty_shadow']['reason'] == 'frozen_protocol_unavailable'


def test_case_sources_are_scoped_and_missing_citation_fails():
    asset = {'section_scope': 'per_question', 'sections': [
        {'heading': 'One', 'text': 'Only first material', 'citation_ids': ['a']},
        {'heading': 'Two', 'text': 'Secret sibling', 'citation_ids': ['b']}],
        'questions': [{'stem': 'First', 'answer': 'A', 'explanation': 'E', 'citation_ids': ['c']},
                      {'stem': 'Second', 'answer': 'B', 'explanation': 'F', 'citation_ids': ['b']}]}
    sources = [{'id': key, 'text': key + ' source'} for key in ('a','b','c')]
    case = build_case(asset, 0, 'learner', sources)
    assert [source['id'] for source in case['sources']] == ['a','c']
    assert 'Secret sibling' not in case['student_visible_text']
    with pytest.raises(ValueError, match='unavailable'):
        build_case(asset, 0, 'learner', sources[:2])


def test_reading_saved_decision_does_not_regrade_with_current_code(monkeypatch):
    import app.difficulty_review_v2 as protocol
    decision = {'status': 'content_issue', 'recommendation': 'manual_review',
                'reason_codes': ['historical_finding'], 'candidates': [], 'target_relation': 'unknown'}
    evidence = {'difficulty_shadow': {'protocol': 'review_v2', 'questions': [
        {'slot_id': 'q1', 'decision': decision}]}}
    monkeypatch.setattr(protocol, 'decide', lambda *args: (_ for _ in ()).throw(AssertionError('must not regrade')))
    assert summary(evidence)['questions'][0]['decision'] == decision


def test_old_policy_without_marker_remains_legacy_and_new_default_does_not_change_it():
    frozen = {'revision': 'old-version', 'max_calls': 2}
    assert policy(Settings(), {'difficulty_shadow': frozen}) == frozen
    assert policy(Settings(difficulty_shadow_mode='shadow'), {}) is None

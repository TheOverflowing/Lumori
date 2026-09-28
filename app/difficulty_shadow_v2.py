"""Versioned two-stage observer with host-owned, advisory decisions.

The generation controller owns publication. This observer records evidence and
never changes the accepted asset, labels, repair schedule or original gates.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import time

from . import difficulty_review_v2 as review
from .providers import ApiProviders
from .store import LocalCallLimit, now
from .task_control import JobCancelled

REVISION = 'difficulty-shadow-review-v2-runtime-20260926-r4'
INPUT_REVISION = 'scoped-question-answer-sources-v1'
PARAMETERS = {'thinking': {'type': 'disabled'}, 'temperature': 0, 'max_tokens': 2400}
ANALYSIS_CONTEXT_EXPANSION = 2  # Conservative allowance for JSON escaping and evidence segment metadata.
ESTIMATED_CHARS_PER_TOKEN = 4  # Planning estimate only; actual prompt size is checked separately.
FINAL_JSON_CHARS_ESTIMATE = 16000  # Current bounded solution/tasks schema; not a hard size guarantee.
CONTEXT_RESERVE_METHOD = 'bounded_final_json_with_metadata_estimate_v2'
TERMINAL = {'completed', 'completed_with_issues', 'disabled', 'skipped', 'internal_error'}


def _digest(value):
    from .difficulty_shadow import digest
    return digest(value)


def _endpoint(settings, stage='analysis'):
    role = 'analyst' if stage == 'analysis' else 'verifier'
    override = getattr(settings, f'difficulty_shadow_{role}_model')
    if not isinstance(override, str) or override != override.strip() or len(override) > 160:
        raise ValueError(f'Invalid difficulty {role} model')
    model = override or settings.text.model
    return replace(settings.text, model=model, extra=_stage_parameters(settings, stage))


def _stage_parameters(settings, stage):
    if stage not in ('analysis','verification'):
        raise ValueError('Unknown difficulty review stage')
    role = 'analyst' if stage == 'analysis' else 'verifier'
    effort = getattr(settings, f'difficulty_shadow_{role}_effort')
    if effort not in ('none', 'low', 'high', 'max'):
        raise ValueError(f'DIFFICULTY_SHADOW_{role.upper()}_EFFORT must be none, low, high or max')
    if effort != 'none':
        return {'thinking': {'type': 'enabled'}, 'reasoning_effort': effort, 'max_tokens': 12288}
    return deepcopy(PARAMETERS)


def _endpoint_signature(settings, stage='analysis'):
    return _endpoint(settings, stage).signature


def _context_reserve_parameters():
    return {'estimated_chars_per_token': ESTIMATED_CHARS_PER_TOKEN,
            'final_json_chars_estimate': FINAL_JSON_CHARS_ESTIMATE,
            'expansion_factor': ANALYSIS_CONTEXT_EXPANSION}


def _analysis_context_reserve(analysis_parameters):
    # Only final JSON is copied into verification. Hidden thinking tokens are
    # never part of that input. The current schema estimate is deliberately
    # separate from the hard check of the fully rendered request before send.
    return min(analysis_parameters['max_tokens'] * ESTIMATED_CHARS_PER_TOKEN,
               FINAL_JSON_CHARS_ESTIMATE) * ANALYSIS_CONTEXT_EXPANSION


def _frozen_protocol_current(settings, frozen):
    try:
        return (frozen.get('revision') == REVISION and frozen.get('review_revision') == review.REVISION
            and frozen.get('input_revision') == INPUT_REVISION
            and frozen.get('protocol_sha256') == review.protocol_fingerprint()
            and frozen.get('parameters') == _stage_parameters(settings, 'analysis')
            and frozen.get('analysis_parameters') == _stage_parameters(settings, 'analysis')
            and frozen.get('verification_parameters') == _stage_parameters(settings, 'verification')
            and frozen.get('context_reserve_method') == CONTEXT_RESERVE_METHOD
            and frozen.get('context_reserve_parameters') == _context_reserve_parameters()
            and frozen.get('analysis_context_reserve_chars') == _analysis_context_reserve(_stage_parameters(settings, 'analysis'))
            and frozen.get('analyst_effort') == settings.difficulty_shadow_analyst_effort
            and frozen.get('verifier_effort') == settings.difficulty_shadow_verifier_effort
            and frozen.get('analyst_model') == _endpoint(settings).model
            and frozen.get('reviewer_model') == _endpoint(settings).model
            and frozen.get('verifier_model') == _endpoint(settings, 'verification').model
            and frozen.get('analyst_endpoint_signature') == _endpoint_signature(settings)
            and frozen.get('reviewer_endpoint_signature') == _endpoint_signature(settings)
            and frozen.get('verifier_endpoint_signature') == _endpoint_signature(settings, 'verification'))
    except (ValueError, TypeError, AttributeError):
        # A changed or malformed optional reviewer configuration cannot prevent
        # publication of the already validated primary asset on resume.
        return False


def policy(settings):
    if (type(settings.difficulty_shadow_max_calls) is not int
            or not 0 <= settings.difficulty_shadow_max_calls <= 12
            or type(settings.difficulty_shadow_max_questions) is not int
            or not 1 <= settings.difficulty_shadow_max_questions <= 5
            or not 1 <= settings.difficulty_shadow_timeout <= 120):
        raise ValueError('Invalid shadow limits')
    analysis_parameters = _stage_parameters(settings, 'analysis')
    verification_parameters = _stage_parameters(settings, 'verification')
    return {'mode': 'shadow', 'protocol': 'review_v2', 'revision': REVISION,
            'review_revision': review.REVISION, 'protocol_sha256': review.protocol_fingerprint(),
            'input_revision': INPUT_REVISION, 'reviewer_model': _endpoint(settings).model,
            'reviewer_endpoint_signature': _endpoint_signature(settings),
            'analyst_model': _endpoint(settings).model,
            'analyst_endpoint_signature': _endpoint_signature(settings),
            'analyst_effort': settings.difficulty_shadow_analyst_effort,
            'verifier_model': _endpoint(settings, 'verification').model,
            'verifier_endpoint_signature': _endpoint_signature(settings, 'verification'),
            'verifier_effort': settings.difficulty_shadow_verifier_effort,
            'max_calls': settings.difficulty_shadow_max_calls,
            'max_questions': settings.difficulty_shadow_max_questions,
            'timeout': settings.difficulty_shadow_timeout,
            'context_chars': settings.agent_context_chars,
            'parameters': deepcopy(analysis_parameters), 'calls_per_question': 2,
            'analysis_parameters': analysis_parameters, 'verification_parameters': verification_parameters,
            'planned_max_output_tokens': analysis_parameters['max_tokens'] + verification_parameters['max_tokens'],
            'analysis_context_reserve_chars': _analysis_context_reserve(analysis_parameters),
            'context_reserve_method': CONTEXT_RESERVE_METHOD,
            'context_reserve_parameters': _context_reserve_parameters(),
            'sampling': 'first_accepted_questions', 'failure_policy': 'manual_review_no_retry',
            'enforcement_enabled': False}


def build_case(asset, index, learner_profile, sources):
    """Freeze only the current question's student material and cited evidence."""
    from .difficulty_shadow import visible_case
    question = asset['questions'][index]
    sections = asset.get('sections', [])
    if asset.get('section_scope') == 'per_question':
        sections = sections[index:index + 1]
    cited = set(question.get('citation_ids', []))
    for section in sections:
        cited.update(section.get('citation_ids', []))
    by_id = {}
    for source in sources:
        source_id = source.get('id')
        if source_id not in cited:
            continue
        if source_id in by_id or not isinstance(source.get('text'), str) or not source['text'].strip():
            raise ValueError('Invalid or duplicate scoped source')
        by_id[source_id] = {'id': source_id, 'text': source['text']}
    if set(by_id) != cited:
        raise ValueError('A cited source is unavailable')
    return {**visible_case(asset, index, learner_profile),
            'reference_answer': question['answer'],
            'reference_explanation': question.get('explanation', ''),
            'sources': [by_id[key] for key in sorted(by_id)],
            'kind': question.get('kind', 'short_answer'),
            'options': deepcopy(question.get('options', []))}


def _unavailable(reason):
    return {'status': 'unavailable', 'candidates': [], 'estimated_difficulty': 'uncertain',
            'target_relation': 'unknown', 'recommendation': 'manual_review',
            'reason_codes': [reason], 'enforcement_enabled': False, 'human_validated': False}


def _decision(question):
    decision = review.decide(question.get('analysis', {}), question.get('verification', {}),
                             question.get('target_difficulty'))
    for name in ('analysis', 'verification'):
        reason = question.get(name, {}).get('reason')
        if reason and reason not in decision['reason_codes']:
            decision['reason_codes'].append(reason)
    return decision


def summary(evidence):
    state = evidence.get('difficulty_shadow', {})
    questions = []
    for question in state.get('questions', []):
        decision = deepcopy(question.get('decision') or _unavailable('decision_unavailable'))
        flags = []
        if decision['status'] in ('content_issue', 'ungradable', 'insufficient_evidence', 'unavailable'):
            flags.append(decision['status'])
        if (decision.get('candidates') and question.get('published_difficulty') in ('easy','medium','hard')
                and question['published_difficulty'] not in decision['candidates']):
            flags.append('published_grade_outside_candidates')
        if decision.get('status') == 'boundary':
            flags.append('difficulty_boundary')
        verification = question.get('verification', {})
        questions.append({'slot_id': question['slot_id'],
            'original_difficulty': question.get('original_difficulty'),
            'target_difficulty': question.get('target_difficulty'),
            'published_difficulty': question.get('published_difficulty'),
            'judge_status': question.get('analysis', {}).get('status'),
            'estimated_difficulty': decision.get('estimated_difficulty'),
            'candidates': decision.get('candidates', []),
            'audit_status': verification.get('status'),
            'decision': decision, 'reason_codes': decision.get('reason_codes', []),
            'content_checks': deepcopy(verification.get('result', {}).get('checks', [])),
            'counterfactual_rework': {'action': decision['recommendation'],
                'reason': decision['status'], 'scope': 'question_and_difficulty',
                'enforcement_enabled': False, 'human_validated': False},
            'manual_review_recommended': bool(flags) or decision['recommendation'] != 'no_difficulty_rework',
            'review_flags': flags})
    return {'mode': 'shadow', 'protocol': 'review_v2', 'revision': state.get('revision'),
            'status': state.get('status'), 'reason': state.get('reason'),
            'affects_primary_decision': False, 'enforcement_enabled': False,
            'calls_reserved': state.get('calls_reserved', 0),
            'duration_ms': state.get('duration_ms'),
            'coverage': deepcopy(state.get('coverage', {})), 'questions': questions}


async def observe(agent, asset):
    frozen = agent.evidence['configuration']['difficulty_shadow']
    max_questions = frozen.get('max_questions')
    valid_limits = (type(max_questions) is int and 1 <= max_questions <= 5
        and type(frozen.get('max_calls')) is int and 0 <= frozen['max_calls'] <= 12
        and type(frozen.get('context_chars')) is int and frozen['context_chars'] > 0
        and type(frozen.get('timeout')) in (int, float) and 1 <= frozen['timeout'] <= 120
        and frozen.get('calls_per_question') == 2)
    state = agent.evidence.setdefault('difficulty_shadow', {
        'protocol': 'review_v2', 'revision': REVISION, 'status': 'pending',
        'mode': 'shadow', 'affects_primary_decision': False, 'enforcement_enabled': False,
        'asset_sha256': _digest(asset), 'calls_reserved': 0, 'questions': [],
        'coverage': {'total_questions': len(asset['questions']),
                     'sampled_questions': min(len(asset['questions']), max_questions) if valid_limits else 0,
                     'assessed_questions': 0}})
    if state.get('status') in TERMINAL:
        return
    if state.get('asset_sha256') != _digest(asset):
        state.update(status='skipped', reason='accepted_asset_changed'); agent.save(); return
    if agent.settings.difficulty_shadow_mode != 'shadow':
        state.update(status='disabled', reason='runtime_kill_switch'); agent.save(); return
    if not valid_limits or not _frozen_protocol_current(agent.settings, frozen):
        state.update(status='skipped', reason='frozen_protocol_unavailable'); agent.save(); return
    started = time.monotonic()
    state['status'] = 'running'; agent.save()

    def remaining(required):
        if state['calls_reserved'] + required > frozen['max_calls']:
            return 'shadow_budget'
        total = agent.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?', (agent.job_id,))['n']
        if max(total, agent.state['call_count']) + required > agent.settings.agent_max_calls:
            return 'job_budget'
        if agent.remaining_daily() < required:
            return 'daily_budget'
        return None

    async def phase(question, name, prompts, validate):
        if name in question:
            record = question[name]
            if record['status'] == 'in_flight':
                record.update(status='interrupted_not_retried', reason='uncertain_call_not_retried')
                agent.save()
            return record
        record = question.setdefault(name, {'status': 'pending'})
        agent.check_cancelled()
        agent.check_scope()
        reason = ('runtime_kill_switch' if agent.settings.difficulty_shadow_mode != 'shadow'
                  else 'frozen_protocol_unavailable' if not _frozen_protocol_current(agent.settings, frozen)
                  else remaining(1))
        if reason:
            record.update(status='skipped', reason=reason); agent.save(); return record
        if len(json.dumps(prompts, ensure_ascii=False)) > frozen['context_chars']:
            record.update(status='skipped', reason='context_limit'); agent.save(); return record
        previous = {r['id'] for r in agent.store.all('SELECT id FROM calls WHERE job_id=?', (agent.job_id,))}
        before_count = agent.state['call_count']
        record.update(status='in_flight', started_at=now(), messages=prompts,
                      prompt_sha256=_digest(prompts), request_parameters=_stage_parameters(agent.settings, name))
        state['calls_reserved'] += 1
        agent.state['call_count'] = max(before_count, len(previous)) + 1
        agent.save()  # Persist billable intent before reaching the provider.
        settings = replace(agent.settings, text_json_mode=True, timeout=frozen['timeout'],
                           text=_endpoint(agent.settings, name))
        shared = getattr(agent.pipeline.providers, 'client', None)
        provider = ApiProviders(settings, agent.store, client=shared)
        try:
            raw = await asyncio.wait_for(provider.generate(prompts, agent.job_id), timeout=frozen['timeout'])
            record['response'] = raw
            record['result'] = validate(raw)
            record['status'] = 'validated'
        except LocalCallLimit as exc:
            # reserve_call rejected locally; no request or ledger entry exists.
            state['calls_reserved'] -= 1
            agent.state['call_count'] = before_count
            record.update(status='skipped', reason='reservation_rejected', resource_limit=exc.resource_limit)
        except (JobCancelled, asyncio.CancelledError):
            record.update(status='interrupted_not_retried', reason='uncertain_call_not_retried')
            agent.save(); raise
        except Exception as exc:
            record.update(status='failed', error_type=type(exc).__name__, reason='stage_failed')
            if getattr(exc, 'code', None):
                record['error_code'] = exc.code
        finally:
            if shared is None:
                await provider.close()
            record['call_ids'] = [r['id'] for r in agent.store.all('SELECT id FROM calls WHERE job_id=?', (agent.job_id,))
                                  if r['id'] not in previous]
            record['finished_at'] = now(); agent.save()
        return record

    try:
        for index, question in enumerate(asset['questions'][:max_questions]):
            entry = next((q for q in state['questions'] if q['slot_id'] == question['slot_id']), None)
            if entry is None:
                original = next((item for slot in agent.state['slots']
                                 for item in slot.get('difficulty_acceptance', {}).get('items', [])
                                 if item['slot_id'] == question['slot_id']), {})
                entry = {'slot_id': question['slot_id'], 'original_difficulty': original.get('assessed_difficulty'),
                         'target_difficulty': original.get('target_difficulty', question.get('difficulty')),
                         'published_difficulty': question.get('difficulty')}
                state['questions'].append(entry)
            try:
                case = build_case(asset, index, agent.request.learner_profile, agent.evidence.get('sources', []))
                from .difficulty_shadow import visible_case
                visible = visible_case(asset, index, agent.request.learner_profile)
                entry['visible_input_sha256'] = _digest(visible)
                digest = _digest(case)
                if entry.get('input_sha256', digest) != digest:
                    entry['decision'] = _unavailable('review_input_changed'); agent.save(); continue
                entry['input_sha256'] = digest
                analysis_prompts = review.analysis_messages(case)
                if not entry.get('analysis'):
                    reason = remaining(2)
                    context = review.verification_context_messages(case)
                    # No shared token allowance is invented: these are separate
                    # output caps and an estimated character allowance for the
                    # blind result entering the next input. Exact input is checked
                    # again immediately before verification's provider call.
                    reserve_chars = _analysis_context_reserve(frozen['analysis_parameters'])
                    entry['preflight'] = {'required_calls': 2,
                        'analysis_max_output_tokens': frozen['analysis_parameters']['max_tokens'],
                        'verification_max_output_tokens': frozen['verification_parameters']['max_tokens'],
                        'planned_max_output_tokens': frozen['planned_max_output_tokens'],
                        'analysis_context_reserve_chars': reserve_chars,
                        'context_reserve_is_estimate': True}
                    if max(len(json.dumps(analysis_prompts, ensure_ascii=False)),
                           len(json.dumps(context, ensure_ascii=False)) + reserve_chars) > frozen['context_chars']:
                        reason = 'context_limit'
                    if reason:
                        entry['analysis'] = {'status': 'skipped', 'reason': reason}
                        entry['verification'] = {'status': 'skipped', 'reason': reason}
                analysis = await phase(entry, 'analysis', analysis_prompts, lambda raw: review.validate_analysis(raw, case))
                if analysis.get('status') == 'validated':
                    prompts = review.verification_messages(case, analysis['result'])
                    await phase(entry, 'verification', prompts,
                                lambda raw: review.validate_verification(raw, case, analysis['result']))
                else:
                    entry.setdefault('verification', {'status': 'skipped', 'reason': 'analysis_unavailable'})
                entry['decision'] = _decision(entry)
            except (JobCancelled, asyncio.CancelledError):
                entry['decision'] = _unavailable('interrupted'); raise
            except Exception as exc:
                entry['decision'] = _unavailable('invalid_review_input')
                entry['error_type'] = type(exc).__name__
            agent.save()
        state['status'] = ('completed' if all(
            question.get(stage, {}).get('status') == 'validated'
            for question in state['questions'] for stage in ('analysis', 'verification'))
            else 'completed_with_issues')
    except (JobCancelled, asyncio.CancelledError):
        state['status'] = 'interrupted'; raise
    except Exception as exc:
        state.update(status='internal_error', error_type=type(exc).__name__)
    finally:
        state['coverage']['assessed_questions'] = sum(
            question.get('decision', {}).get('status') == 'assessed'
            for question in state['questions'])
        state['duration_ms'] = round((time.monotonic() - started) * 1000)
        state['observed_asset_sha256'] = _digest(asset)
        agent.save()

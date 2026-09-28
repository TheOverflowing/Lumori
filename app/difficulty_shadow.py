"""Bounded post-acceptance observer. Never changes the accepted asset or old gates."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import time
from . import difficulty_judge as judge
from . import difficulty_semantic_audit_v2 as audit
from .providers import ApiProviders
from .store import LocalCallLimit, now
from .task_control import JobCancelled

REVISION='difficulty-shadow-v2-repeat-policy'

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def protocol_fingerprint():
    return digest({'judge_system':judge.SYSTEM,'judge_schema':judge.CompactJudgement.model_json_schema(),
                   'audit_system':audit.messages({'learner_profile':'','student_visible_text':'','review':{}})[0]['content'],'audit_schema':audit.Audit.model_json_schema(),
                   'audit_revision':audit.REVISION})

def policy(settings,saved_config=None):
    if saved_config is not None:
        # Legacy jobs remain off; saved jobs keep their original limits/version.
        return deepcopy(saved_config.get('difficulty_shadow'))
    if settings.difficulty_shadow_mode not in ('off','shadow'):
        raise ValueError('DIFFICULTY_SHADOW_MODE must be off or shadow')
    if settings.difficulty_shadow_mode=='off':return None
    if settings.difficulty_shadow_protocol == 'review_v2':
        from .difficulty_shadow_v2 import policy as new_policy
        return new_policy(settings)
    if settings.difficulty_shadow_protocol != 'legacy_v1':
        raise ValueError('DIFFICULTY_SHADOW_PROTOCOL must be review_v2 or legacy_v1')
    if (type(settings.difficulty_shadow_max_calls) is not int or not 0<=settings.difficulty_shadow_max_calls<=12
            or type(settings.difficulty_shadow_max_questions) is not int or not 1<=settings.difficulty_shadow_max_questions<=5
            or not 1<=settings.difficulty_shadow_timeout<=120):
        raise ValueError('Invalid shadow limits')
    if settings.difficulty_shadow_repeat_disagreements and settings.difficulty_shadow_max_calls<4:
        raise ValueError('Repeated shadow disagreement review needs at least four calls')
    return {'mode':'shadow','protocol':'legacy_v1','revision':REVISION,'protocol_sha256':protocol_fingerprint(),
        'judge_revision':judge.REVISION,'audit_revision':audit.REVISION,
        'max_calls':settings.difficulty_shadow_max_calls,'max_questions':settings.difficulty_shadow_max_questions,
        'timeout':settings.difficulty_shadow_timeout,'context_chars':settings.agent_context_chars,
        'repeat_disagreements':settings.difficulty_shadow_repeat_disagreements,
        'parameters':{'thinking':{'type':'disabled'},'temperature':0,'max_tokens':6000},
        'audit_max_tokens':3000,'transport':'configured_text_api_json','sampling':'first_accepted_questions',
        'audit_selection':'each_sampled_question_if_budget_remains','failure_policy':'observe_only_no_retry'}

def visible_case(asset,index,learner_profile):
    q=asset['questions'][index]
    sections=asset.get('sections',[])
    if asset.get('section_scope')=='per_question':sections=sections[index:index+1]
    blocks=[]
    if asset.get('learning_objectives'):
        blocks.append('Learning objectives:\n'+'\n'.join(asset['learning_objectives']))
    for section in sections:blocks.append(section['heading']+'\n'+section['text'])
    blocks.append(q['stem'])
    blocks.extend(f'{chr(65+i)}. {option}' for i,option in enumerate(q.get('options',[])))
    return {'learner_profile':learner_profile,'student_visible_text':'\n\n'.join(blocks)}

def review_flags(question):
    """Route observer disagreements to a person; never alter the original grade."""
    judge=question.get('judge',{})
    audit=question.get('audit',{})
    if judge.get('status')!='validated':return ['judge_unavailable']
    result=judge.get('result',{})
    if result.get('eligibility')!='eligible':return ['question_not_gradeable']
    candidates=result.get('decision',{}).get('candidates',[])
    flags=[]
    if candidates and question.get('published_difficulty') not in candidates:
        flags.append('published_grade_outside_candidates')
    if len(candidates)>1:flags.append('difficulty_boundary')
    if audit.get('status')!='validated' or audit.get('result',{}).get('status')!='no_issue_found':
        flags.append('semantic_audit_unavailable_or_flagged')
    return flags

def rework_candidate(question):
    """Counterfactual decision for calibration only; never triggers work."""
    base={'action':'manual_review','scope':'difficulty_only',
          'enforcement_enabled':False,'human_validated':False}
    judge=question.get('judge',{})
    if judge.get('status')!='validated':return {**base,'reason':'judge_unavailable'}
    first=judge.get('result',{})
    if first.get('eligibility')!='eligible':return {**base,'reason':'question_not_gradeable'}
    candidates=first.get('decision',{}).get('candidates',[])
    target=question.get('target_difficulty')
    if not candidates or target not in ('easy','medium','hard'):
        return {**base,'reason':'missing_target_or_candidates'}
    first_audit=question.get('audit',{})
    if first_audit.get('status')!='validated' or first_audit.get('result',{}).get('status')!='no_issue_found':
        return {**base,'reason':'audit_not_cleared'}
    if target in candidates:
        return {**base,'action':'no_difficulty_rework','reason':'target_remains_plausible'}
    original=question.get('original_difficulty')
    if original not in ('easy','medium','hard'):
        return {**base,'reason':'original_judge_unavailable'}
    if original==target:
        return {**base,'reason':'original_judge_disagrees_with_shadow'}
    repeated=question.get('judge_repeat',{})
    if repeated.get('status')!='validated':
        return {**base,'reason':'repeat_unavailable'}
    second=repeated.get('result',{})
    if second.get('eligibility')!='eligible':
        return {**base,'reason':'repeat_not_gradeable'}
    repeat_audit=question.get('audit_repeat',{})
    if repeat_audit.get('status')!='validated' or repeat_audit.get('result',{}).get('status')!='no_issue_found':
        return {**base,'reason':'repeat_audit_not_cleared'}
    other=second.get('decision',{}).get('candidates',[])
    if sorted(candidates)!=sorted(other):
        return {**base,'reason':'repeat_candidate_disagreement'}
    if target in other:
        return {**base,'reason':'target_in_repeat_candidates'}
    return {**base,'action':'rework_candidate','reason':'original_and_repeated_shadow_exclude_target',
            'candidates':sorted(candidates)}

def summary(evidence):
    state=evidence.get('difficulty_shadow')
    if not isinstance(state,dict):return None
    if state.get('protocol') == 'review_v2':
        from .difficulty_shadow_v2 import summary as new_summary
        return new_summary(evidence)
    return {'mode':'shadow','status':state.get('status'),'affects_primary_decision':False,
        'calls_reserved':state.get('calls_reserved',0),'duration_ms':state.get('duration_ms'),
        'questions':[{'slot_id':q['slot_id'],'original_difficulty':q.get('original_difficulty'),
            'target_difficulty':q.get('target_difficulty'),'published_difficulty':q.get('published_difficulty'),
            'judge_status':q.get('judge',{}).get('status'),
            'estimated_difficulty':q.get('judge',{}).get('result',{}).get('estimated_difficulty'),
            'candidates':q.get('judge',{}).get('result',{}).get('decision',{}).get('candidates'),
            'audit_status':q.get('audit',{}).get('result',{}).get('status',q.get('audit',{}).get('status')),
            'repeat_status':q.get('judge_repeat',{}).get('status'),
            'repeat_audit_status':q.get('audit_repeat',{}).get('result',{}).get('status',q.get('audit_repeat',{}).get('status')),
            'counterfactual_rework':deepcopy(q.get('counterfactual_rework')) or rework_candidate(q),
            'manual_review_recommended':bool(review_flags(q)),
            'review_flags':review_flags(q)}
            for q in state.get('questions',[])]}

async def observe(agent,asset):
    frozen=agent.evidence['configuration'].get('difficulty_shadow')
    if not frozen:return
    if frozen.get('protocol') == 'review_v2':
        from .difficulty_shadow_v2 import observe as new_observe
        return await new_observe(agent,asset)
    if frozen.get('protocol') not in (None,'legacy_v1'):
        state=agent.evidence.setdefault('difficulty_shadow',{
            'mode':'shadow','affects_primary_decision':False,'questions':[],
            'asset_sha256':digest(asset),'protocol':frozen['protocol']})
        if state.get('status') in ('completed','completed_with_issues','disabled','skipped','internal_error'):
            return
        state.update(status='skipped',reason='frozen_protocol_unavailable')
        agent.save();return
    state=agent.evidence.setdefault('difficulty_shadow',{'status':'pending','calls_reserved':0,'questions':[],
        'affects_primary_decision':False,'asset_sha256':digest(asset),'mode':'shadow'})
    # Terminal records are reused; an interrupted call is never billed again automatically.
    if state['status'] in ('completed','completed_with_issues','disabled','skipped','internal_error'):return
    if state['asset_sha256']!=digest(asset):
        state.update(status='skipped',reason='accepted_asset_changed');agent.save();return
    if agent.settings.difficulty_shadow_mode!='shadow':
        state.update(status='disabled',reason='runtime_kill_switch');agent.save();return
    if frozen['revision']!=REVISION or frozen['protocol_sha256']!=protocol_fingerprint():
        state.update(status='skipped',reason='frozen_protocol_unavailable');agent.save();return
    started=time.monotonic();state['status']='running';agent.save()
    async def phase(question,name,case,module):
        if name in question:
            record=question[name]
            if record['status']=='in_flight':record.update(status='interrupted_not_retried')
            return record
        record=question.setdefault(name,{'status':'pending'})
        if agent.settings.difficulty_shadow_mode!='shadow':
            record.update(status='skipped',reason='runtime_kill_switch');agent.save();return record
        if state['calls_reserved']>=frozen['max_calls']:
            record.update(status='skipped',reason='shadow_budget');agent.save();return record
        agent.check_scope();agent.check_cancelled()
        total=agent.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?',(agent.job_id,))['n']
        if max(total,agent.state['call_count'])>=agent.settings.agent_max_calls or agent.remaining_daily()<=0:
            record.update(status='skipped',reason='job_or_daily_budget');agent.save();return record
        prompts=module.messages(case)
        if len(json.dumps(prompts,ensure_ascii=False))>frozen['context_chars']:
            record.update(status='skipped',reason='context_limit');agent.save();return record
        previous={r['id'] for r in agent.store.all('SELECT id FROM calls WHERE job_id=?',(agent.job_id,))}
        before_count=agent.state['call_count']
        record.update(status='in_flight',started_at=now(),messages=prompts,prompt_sha256=digest(prompts))
        state['calls_reserved']+=1
        agent.state['call_count']=max(total,agent.state['call_count'])+1
        agent.save()  # Durable intent before possibly billable network traffic.
        params={**frozen['parameters'],'max_tokens':frozen['audit_max_tokens'] if name in ('audit','audit_repeat') else frozen['parameters']['max_tokens']}
        settings=replace(agent.settings,text_json_mode=True,timeout=frozen['timeout'],text=replace(agent.settings.text,extra=params))
        shared=getattr(agent.pipeline.providers,'client',None)
        provider=ApiProviders(settings,agent.store,client=shared)
        try:
            raw=await asyncio.wait_for(provider.generate(prompts,agent.job_id),timeout=frozen['timeout'])
            record['response']=raw
            record['result']=module.validate(raw,case)
            record['status']='validated'
        except LocalCallLimit as exc:
            state['calls_reserved']-=1
            agent.state['call_count']=before_count
            record.update(status='skipped',reason='reservation_rejected',resource_limit=exc.resource_limit)
        except (JobCancelled,asyncio.CancelledError):
            record['status']='interrupted_not_retried';agent.save();raise
        except Exception as exc:
            record.update(status='failed',error_type=type(exc).__name__)
            if getattr(exc,'code',None):record['error_code']=exc.code
        finally:
            if shared is None:await provider.close()
            record['call_ids']=[r['id'] for r in agent.store.all('SELECT id FROM calls WHERE job_id=?',(agent.job_id,)) if r['id'] not in previous]
            record['finished_at']=now();agent.save()
        return record
    try:
        for index,q in enumerate(asset['questions'][:frozen['max_questions']]):
            entry=next((x for x in state['questions'] if x['slot_id']==q['slot_id']),None)
            if entry is None:
                original = next((x for slot in agent.state['slots'] for x in slot.get('difficulty_acceptance',{}).get('items',[]) if x['slot_id']==q['slot_id']), {})
                entry={'slot_id':q['slot_id'],'original_difficulty':original.get('assessed_difficulty'),
                    'published_difficulty':q['difficulty'],'target_difficulty':original.get('target_difficulty',q['difficulty'])}
                state['questions'].append(entry)
            case=visible_case(asset,index,agent.request.learner_profile)
            entry['visible_input_sha256']=digest(case)
            result=await phase(entry,'judge',case,judge)
            if result['status']=='validated':
                audited=await phase(entry,'audit',{**case,'review':result['response']},audit)
                candidates=result['result']['decision']['candidates']
                if (frozen.get('repeat_disagreements') and audited.get('status')=='validated'
                        and audited.get('result',{}).get('status')=='no_issue_found'
                        and entry['target_difficulty'] not in candidates
                        and entry['original_difficulty']!=entry['target_difficulty']):
                    repeated=await phase(entry,'judge_repeat',case,judge)
                    if repeated['status']=='validated':
                        await phase(entry,'audit_repeat',{**case,'review':repeated['response']},audit)
            entry['counterfactual_rework']=rework_candidate(entry)
        state['status']='completed' if all(p.get('status')=='validated' for q in state['questions'] for k,p in q.items() if k in ('judge','audit','judge_repeat','audit_repeat')) else 'completed_with_issues'
    except (JobCancelled,asyncio.CancelledError):
        state['status']='interrupted';raise
    except Exception as exc:
        # Sidecar failures are evidence, not failures of the already validated generation.
        state.update(status='internal_error',error_type=type(exc).__name__)
    finally:
        state['duration_ms']=round((time.monotonic()-started)*1000)
        state['observed_asset_sha256']=digest(asset)
        agent.save()

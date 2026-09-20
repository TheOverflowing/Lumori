"""Owner-bound, restart-safe single-question preparation for generation.

The model can request information, but never replaces the user's topic. Only a
real submitted answer is appended. No simulated user or resolver is used here.
"""
import asyncio
from datetime import datetime
import hashlib
import json
import time

from .models import IntentGenerateRequest
from .providers import ProviderError, ProviderOutputError
from .store import Conflict, dumps, now
from . import query_clarification

TTL_SECONDS = 15 * 60
CONTROL_FIELDS = {'request_key', 'preparation_id', 'clarification_action', 'clarification_answer'}
FAILURE_MESSAGES = {
    'upstream_unavailable': '文本模型服务暂不可用，生成也可能失败。请稍后再试，本页输入会保留。',
    'authentication': '文本模型连接未通过身份或权限验证。请检查模型连接后再试；直接生成也可能失败。',
    'payment_required': '文本模型账户余额不足。请处理账户余额后再试；直接生成也可能失败。',
    'configuration': '文本模型接口或参数未被接受。请检查模型连接后再试；直接生成也可能失败。',
    'rate_limited': '文本模型服务限流或额度已用尽，生成也可能失败。请稍后或调整额度后再试。',
    'timeout': '需求检查等待超时，生成也可能受到影响。可稍后再试，或按原输入尝试生成。',
    'network': '暂时无法连接文本模型服务，生成也可能失败。请检查连接后再试。',
    'invalid_output': '需求检查返回的内容无法使用，可跳过检查，按原输入继续生成。',
    'unknown': '需求检查未完成，生成也可能受到影响。可稍后再试，或按原输入尝试生成。',
}


def failure_code(error):
    """Publish only application-owned categories, never upstream exception text."""
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, ProviderOutputError):
        return 'invalid_output'
    code = getattr(error, 'code', None)
    if code == 'invalid_response':
        return 'invalid_output'
    return code if isinstance(code, str) and code in FAILURE_MESSAGES else 'unknown'


class PreparationError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def fingerprint(request):
    values = request.model_dump(exclude=CONTROL_FIELDS)
    values['document_ids'] = sorted(set(values['document_ids']))
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def owned_preparation(store, preparation_id, owner_id):
    row = store.one('''SELECT j.* FROM jobs j JOIN job_owners o ON o.job_id=j.id
        WHERE j.id=? AND j.kind='prepare' AND o.user_id=?''', (preparation_id, owner_id))
    if not row:
        raise PreparationError('preparation_missing', '理解检查不存在，请重新检查。', 404)
    if not row['result']:
        if row['status'] in ('queued', 'running'):
            raise PreparationError('preparation_pending', '正在检查，请等待完成。')
        raise PreparationError('preparation_incomplete', '检查曾中断，请重新检查。')
    return row, json.loads(row['result'])


def public_result(result):
    public = {k: result[k] for k in ('preparation_id', 'status', 'question', 'options', 'expires_at')}
    if result['status'] == 'unavailable':
        code = result.get('failure_code')
        public['failure_code'] = code if isinstance(code, str) and code in FAILURE_MESSAGES else 'unknown'
    return public


async def prepare(store, providers, request, owner_id):
    if request.preparation_id or request.clarification_action or request.clarification_answer:
        raise PreparationError('preparation_changed', '请使用原始生成参数重新检查。')
    try:
        job, new = store.job('prepare:' + request.request_key, 'prepare', request.model_dump(), owner_id=owner_id)
    except Conflict as exc:
        raise PreparationError('preparation_changed', str(exc)) from None
    if not new:
        _, result = owned_preparation(store, job['id'], owner_id)
        if result['expires_at'] <= time.time():
            raise PreparationError('preparation_expired', '理解检查已过期，请重新检查。')
        return public_result(result)
    store.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), job['id']))
    expires = int(datetime.fromisoformat(job['created_at']).timestamp()) + TTL_SECONDS
    failure = None
    try:
        decision = await asyncio.wait_for(query_clarification.prepare_decision(providers, request, job['id']), timeout=45)
    except asyncio.CancelledError:
        store.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                      ('理解检查已中断，请重新检查。', now(), job['id']))
        raise
    except (ProviderError, ValueError, TimeoutError) as exc:
        # Do not publish upstream text, malformed model output or sensitive input.
        failure = failure_code(exc)
        decision = dict(status='unavailable', question='', options=[], reason='', anchor_quote='', missing_information='')
    result = dict(decision, preparation_id=job['id'], expires_at=expires,
                  request_fingerprint=fingerprint(request), prompt_version=query_clarification.PROMPT_VERSION)
    if failure:
        result['failure_code'] = failure
    store.save_job_evidence(job['id'], {'schema_version': 'query-preparation-v1', 'request': request.model_dump(),
                                     'decision': result, 'failure': failure,
                                     'limitations': ['Model decision, not semantic correctness or learner validation.']})
    store.execute('UPDATE jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?',
                  ('failed' if failure else 'succeeded', dumps(result),
                   FAILURE_MESSAGES[failure] if failure else None, now(), job['id']))
    return public_result(result)


def validate_submission(store, request, owner_id):
    if not request.preparation_id:
        return
    _, result = owned_preparation(store, request.preparation_id, owner_id)
    if result['request_fingerprint'] != fingerprint(request):
        raise PreparationError('preparation_changed', '生成条件已改变，请重新检查。')
    used = store.one('''SELECT j.payload FROM preparation_uses u JOIN jobs j ON j.id=u.job_id
        WHERE u.preparation_id=?''', (request.preparation_id,))
    if used:
        # Exact retries remain valid after expiry. Store.job validates the key too.
        if used['payload'] != dumps(request.model_dump()):
            raise PreparationError('preparation_used', '此次理解检查已用于生成，请重新检查。')
    elif result['expires_at'] <= time.time():
        raise PreparationError('preparation_expired', '理解检查已过期，请重新检查。')
    action = request.clarification_action
    if result['status'] == 'clarification_required' and action not in ('answer', 'unknown', 'skip'):
        raise PreparationError('preparation_incomplete', '请补充信息，或选择按原输入继续。')
    if result['status'] == 'unavailable' and action != 'skip':
        raise PreparationError('preparation_incomplete', '检查未完成，请确认使用原输入继续。')
    if result['status'] == 'ready' and action is not None:
        raise PreparationError('preparation_changed', '此次检查没有追问，请按原请求生成。')


def execution_request(store, request, job_id):
    """Rebuild the same immutable intent on initial work and any later resume.

Expiry is a submission boundary, not a cancellation of already accepted work.
The job payload retains the original topic; the execution view carries both.
"""
    if not request.preparation_id:
        return request
    owner = store.one('SELECT user_id FROM job_owners WHERE job_id=?', (job_id,))
    if not owner:
        raise ValueError('补充信息未绑定账号。')
    _, result = owned_preparation(store, request.preparation_id, owner['user_id'])
    used = store.one('SELECT job_id FROM preparation_uses WHERE preparation_id=?', (request.preparation_id,))
    if not used or used['job_id'] != job_id or result['request_fingerprint'] != fingerprint(request):
        raise ValueError('补充信息与生成任务不一致。')
    question = result['question'] if result['status'] == 'clarification_required' else ''
    options = result['options'] if question else []
    topic = request.topic
    if request.clarification_action == 'answer':
        if not question:
            raise ValueError('补充回答缺少对应的追问。')
        if request.language == 'zh':
            topic = f'原始学习目标：\n{request.topic}\n\n澄清问题（仅为问句，不是已知事实）：\n{question}\n\n用户补充：\n{request.clarification_answer}'
            if options:
                topic += '\n\n曾提供的选项（仅用于理解序号指代，不代表用户选择或已知事实）：\n' + '\n'.join(f'{i+1}. {value}' for i,value in enumerate(options))
        else:
            topic = f'Original learning goal:\n{request.topic}\n\nClarification question (a question, not an established fact):\n{question}\n\nUser clarification:\n{request.clarification_answer}'
            if options:
                topic += '\n\nOffered options (only for resolving references, not selections or established facts):\n' + '\n'.join(f'{i+1}. {value}' for i,value in enumerate(options))
    note = ('Only the actual user answer supplies new information. The clarification question and its alternatives are not facts. '
            'Preserve unresolved or conflicting conditions; do not silently choose an interpretation or claim clarification succeeded.')
    if request.clarification_action in ('unknown', 'skip'):
        note += ' The user chose to continue without additional information. Use conditional explanations or report insufficient evidence when the missing condition is necessary.'
    return IntentGenerateRequest(**(request.model_dump() | {'topic': topic}), original_topic=request.topic,
                                 clarification_question=question, clarification_options=list(options), clarification_note=note)

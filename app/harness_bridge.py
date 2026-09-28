"""Use the pinned Harness runtime for generation phases outside QuestionWorker."""

import json
from dataclasses import replace

from .document_lifecycle import state as document_state
from .harness_runner import run_harness_phase


class HarnessPhaseContext:
    def __init__(self, pipeline, request, job_id, sources, max_requests=None):
        self.pipeline, self.request, self.job_id = pipeline, request, job_id
        self.store = pipeline.store
        self.settings = (replace(pipeline.settings, harness_max_requests=max_requests)
                         if max_requests is not None else pipeline.settings)
        self.evidence = {'sources': sources}
        from .generation_agent import orchestration_for_request
        self.orchestration = orchestration_for_request(self.settings, request)

    def check_scope(self):
        job = self.store.one('SELECT * FROM jobs WHERE id=?', (self.job_id,))
        if not job:
            raise ValueError('生成任务不存在。')
        self.pipeline.authorize_job(job, json.loads(job['payload']))
        for source in self.evidence['sources']:
            document = self.store.one('SELECT course_id FROM documents WHERE id=?',
                                      (source['document_id'],))
            if not document or document['course_id'] != self.request.course_id:
                raise ValueError('生成依据不再属于当前课程。')
            lifecycle = document_state(self.store, source['document_id'])
            if lifecycle['deleted_at'] or not lifecycle['enabled']:
                raise ValueError('生成依据已停用或删除。')


async def run_phase(pipeline, request, job_id, messages, phase, sources, report,
                    *, search_callback=None, max_requests=None):
    """The caller keeps its existing checkpoint and owns the returned JSON."""
    actor = HarnessPhaseContext(pipeline, request, job_id, sources, max_requests)
    return await run_harness_phase(actor, messages, phase, report,
                                   search_callback=search_callback)

"""Concise final findings with optional deterministic witnesses for a known exercise."""
import json
from pydantic import Field,BaseModel,ConfigDict,model_validator
from . import difficulty_semantic_audit as v1
REVISION='difficulty-semantic-audit-v2-witness-20260924'
class FinalFinding(v1.Finding):
    explanation: str=Field(min_length=1,max_length=350)
    suggested_correction: str=Field(min_length=1,max_length=300)

    @model_validator(mode='after')
    def correction_not_withdrawn(self):
        text=self.suggested_correction.lower().strip()
        if text.startswith(('no change needed','no correction needed','无需修改','不需要修改')):
            raise ValueError('Withdrawn finding cannot be emitted as an error')
        return self
class Audit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    findings:list[FinalFinding]=Field(max_length=4)

def messages(case):
    result=v1.messages(case);payload=json.loads(result[1]['content'])
    payload['schema']=Audit.model_json_schema()
    if case.get('execution_witness'):
        payload['host_verified_execution']=case['execution_witness']
    result[0]['content']+='\nReturn only final, resolved findings, not a transcript of reconsideration. If examination shows a suspected error is actually valid, OMIT that finding entirely. Do not mark an accurate statement as erroneous because it was initially suspected. Keep each explanation <=350 characters and each suggested correction <=300 characters. Host execution witnesses, when supplied, were computed for the exact known exercise and are checking evidence, not extra student assumptions. Distinguish the specified correct result, the program\'s actual returned value, and nontermination. Do not contradict the witnesses or propose unsupported alternative examples.'
    result[1]['content']=json.dumps(payload,ensure_ascii=False)
    return result

def validate(raw,case):
    parsed=Audit.model_validate(raw)
    return v1.validate(parsed.model_dump(),case)

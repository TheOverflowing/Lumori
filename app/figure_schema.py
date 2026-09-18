"""Bounded, explicitly model-derived image annotations."""
from pydantic import BaseModel, ConfigDict, Field


class FigureDescription(BaseModel):
    model_config=ConfigDict(extra='forbid')
    description: str = Field(min_length=1,max_length=2400)
    keywords_zh: list[str] = Field(max_length=16)
    keywords_en: list[str] = Field(max_length=16)
    visible_text: str = Field(max_length=2400)
    relationships: list[str] = Field(max_length=16)
    uncertainties: list[str] = Field(max_length=16)

    def retrieval_text(self):
        return '\n'.join([self.description,' '.join(self.keywords_zh),' '.join(self.keywords_en),
                          self.visible_text,*self.relationships])[:7000]


def validate_description(value):
    # Some JSON-mode models echo this transport marker alongside schema fields.
    # Remove only this exact non-semantic marker; unknown fields still fail closed.
    if isinstance(value,dict) and value.get('type')=='json_object':
        value={k:v for k,v in value.items() if k!='type'}
    result=FigureDescription.model_validate(value)
    for field in ('keywords_zh','keywords_en','relationships','uncertainties'):
        if any(not item.strip() or len(item)>300 for item in getattr(result,field)):
            raise ValueError('图片说明字段过长或为空。')
    return result

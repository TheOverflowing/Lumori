import pytest

from app.providers import ApiProviders, ProviderOutputError


@pytest.mark.parametrize('finish,content,code', [
    ('length', '{}', 'output_token_limit'),
    ('content_filter', '{}', 'output_filtered'),
    ('aborted', '{}', 'output_interrupted'),
    ('insufficient_system_resource', '{}', 'output_interrupted'),
    ('tool_calls', '{}', 'output_interrupted'),
    ('stop', '', 'empty_output'),
    ('stop', '[1]', 'invalid_json_object'),
    ('stop', '{"unfinished":', 'invalid_json_object'),
])
def test_text_failure_classification_never_accepts_truncated_output(finish, content, code):
    with pytest.raises(ProviderOutputError) as error:
        ApiProviders._text_object({'choices': [{'finish_reason': finish, 'message': {'content': content}}]})
    assert error.value.code == code
    assert content not in str(error.value) if content else True


def test_complete_json_survives_extra_reasoning_without_exporting_it():
    assert ApiProviders._text_object({'choices': [{'finish_reason': 'stop',
        'message': {'content': '{"ok":true}', 'reasoning_content': 'PRIVATE_REASONING'}}]}) == {'ok': True}

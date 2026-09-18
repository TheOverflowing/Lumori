"""Validated runtime input formats and original-source context assembly."""
from functools import lru_cache
import hashlib
from pathlib import Path
from .rag_candidates import TokenCounter, title_input, query_input, assemble_context


def tokenizer_identity(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('检索 tokenizer 文件缺失，请恢复配置的模型文件。')
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=4)
def _counter(path, digest):
    return TokenCounter(path)


def verified_counter(path, expected):
    resolved, actual = tokenizer_identity(path)
    if actual != expected:
        raise ValueError('检索 tokenizer 校验值变化，请重新建立索引。')
    return _counter(resolved, actual)


def encoding_configuration(settings):
    if settings.retrieval_document_encoding not in ('raw_text_v1', 'title_path_v1'):
        raise ValueError('文档编码配置无效。')
    if settings.retrieval_query_encoding not in ('raw_topic_v1', 'cs_instruction_v1'):
        raise ValueError('查询编码配置无效。')
    if type(settings.retrieval_context_tokens) is not int or settings.retrieval_context_tokens < 0:
        raise ValueError('上下文预算必须是非负整数。')
    if settings.retrieval_expansion not in ('none', 'window', 'parent'):
        raise ValueError('上下文扩展配置无效。')
    if settings.retrieval_expansion != 'none' and not settings.retrieval_context_tokens:
        raise ValueError('上下文扩展需要明确的 token 预算。')
    return dict(document_encoding=settings.retrieval_document_encoding,
                query_encoding=settings.retrieval_query_encoding,
                context_tokens=settings.retrieval_context_tokens, expansion=settings.retrieval_expansion)


def document_input(row, name, encoding):
    if encoding == 'raw_text_v1':
        return row['text']
    return title_input(row['text'], Path(name).stem, row.get('metadata', {}).get('heading_path', []))


def runtime_context(ranked, pages_by_document, counter, budget, expansion, max_hits):
    parents = {f'{did}:{page}': {'text': text} for did, pages in pages_by_document.items()
               for page, text in enumerate(pages, 1)}
    leaves = []
    for row in ranked:
        meta = row['metadata']
        start = meta.get('original_page_char_start', meta.get('page_char_start'))
        end = meta.get('original_page_char_end', meta.get('page_char_end'))
        pid = f"{row['document_id']}:{row['page']}"
        if meta.get('source_kind')=='figure':
            pid='visual:'+row['id']
            parents[pid]={'text':row['text']}
            start,end=0,len(row['text'])
        if type(start) is not int or type(end) is not int or parents[pid]['text'][start:end] != row['text']:
            raise ValueError('索引片段与原始资料坐标不一致，请重新索引。')
        leaves.append(row | {'parent_id': pid, 'source_start': start, 'source_end': end})
    context, tokens = assemble_context(leaves, parents, counter, budget, expansion, max_hits)
    result = []
    for c in context:
        a, b, pid = c['source_start'], c['source_end'], c['parent_id']
        original_ids = [r['id'] for r in leaves[:max_hits] if r['parent_id'] == pid
                        and r['source_start'] < b and r['source_end'] > a]
        source = {k: v for k, v in c.items() if k not in ('parent_id', 'source_start', 'source_end')}
        source['id'] = 'context_' + hashlib.sha256(f'{pid}:{a}:{b}:{c["text"]}'.encode()).hexdigest()[:32]
        source['metadata'] = dict(c['metadata'], page_char_start=a, page_char_end=b,
                                  coordinate_system='image_description_chars_v1' if c['metadata'].get('source_kind')=='figure' else 'parsed_page_python_chars_v1',
                                  context_expansion=expansion, retrieved_chunk_ids=original_ids)
        source['metadata'].pop('original_page_char_start', None)
        source['metadata'].pop('original_page_char_end', None)
        source['metadata'].pop('coordinate_page_sha256', None)
        source['metadata'].pop('normalization', None)
        result.append(source)
    return result, tokens

"""Offline discovery contracts and adversarial HTTP/DNS fixtures; no paid calls."""
import asyncio
from types import SimpleNamespace
import socket
import ssl

import httpx
import pytest

from app import exploration_sources as sources
from app.config import Settings
from app.exploration_policy import capabilities


def settings(**values):
    return SimpleNamespace(**({'exploration_search_provider': 'curated',
                              'exploration_search_api_key': '',
                              'exploration_max_bytes': 1024,
                              'exploration_fetch_timeout': 1} | values))


def run(awaitable):
    return asyncio.run(awaitable)


def test_default_broad_search_is_available_without_a_paid_search_key():
    settings = Settings()
    assert settings.exploration_search_provider == 'hybrid'
    assert capabilities(settings)['available'] is True
    assert capabilities(settings)['web_search_configured'] is False


class Writer:
    def __init__(self):
        self.data = b''
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def wire(monkeypatch, responses, dns=None):
    """Keep actual URL validation, resolver, pinning and HTTP reader in the path."""
    connections, writers, resolutions = [], [], []

    def getaddrinfo(host, port, *args, **kwargs):
        resolutions.append(host)
        ips = dns(host, len(resolutions)) if dns else ['93.184.216.34']
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET,
                 socket.SOCK_STREAM, 6, '', (ip, port)) for ip in ips]

    async def connect(**kwargs):
        connections.append(kwargs)
        reader = asyncio.StreamReader()
        payload = responses[len(connections) - 1]
        if payload is not None:
            reader.feed_data(payload)
            reader.feed_eof()
        writer = Writer()
        writers.append(writer)
        return reader, writer

    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    monkeypatch.setattr(asyncio, 'open_connection', connect)
    return SimpleNamespace(connections=connections, writers=writers, resolutions=resolutions)


def response(body=b'public teaching text', content_type='text/plain', extra=b''):
    return (b'HTTP/1.1 200 OK\r\nContent-Type: ' + content_type.encode() +
            b'\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n' + extra + b'\r\n' + body)


def test_curated_bilingual_ranking_and_actual_catalog_links():
    zh = run(sources.search(settings(), '请解释快速排序的哨兵划分', language='zh'))
    en = run(sources.search(settings(), 'Explain quick sort partition', language='en'))
    assert zh[0]['url'] == 'https://raw.githubusercontent.com/krahets/hello-algo/main/docs/chapter_sorting/quick_sort.md'
    assert en[0]['url'] == 'https://raw.githubusercontent.com/krahets/hello-algo/main/en/docs/chapter_sorting/quick_sort.md'
    assert zh[0]['reading_url'] == 'https://www.hello-algo.com/chapter_sorting/quick_sort/'
    assert zh[0]['source_format'] == 'markdown_source'
    assert zh[0]['provider'] == 'curated'
    assert zh[0]['storage_policy'] == 'open_license'
    assert zh[0]['license'] == 'CC BY-NC-SA 4.0'
    assert 'Catalog topics:' in zh[0]['snippet']
    assert zh[0]['catalog_version'] == sources.CATALOG_VERSION


def test_catalog_does_not_fabricate_coverage_or_substring_match():
    assert run(sources.search(settings(), 'medieval violin history')) == []
    assert run(sources.search(settings(), 'business processing')) == []
    assert len(run(sources.search(settings(), 'python', limit=1))) == 1
    rows = run(sources.search(settings(), 'MLFQ 多级反馈队列 process scheduling'))
    assert any(row['storage_policy'] == 'link_only' for row in rows)
    assert any('processes-and-concurrency' in row['url'] for row in rows)


@pytest.mark.parametrize('query', [
    'machine learning workflow and training process',
    'Explain the process of machine learning model evaluation',
    'The process of organizing a conference',
    'scheduling a meeting with my classmates',
    'normalization of machine learning input features',
    'pivot a business strategy',
    'partition a hard drive',
    'heap memory allocation',
])
def test_ambiguous_terms_require_the_matching_subject(query):
    rows = run(sources.search(settings(), query, limit=20))
    assert not any(any(topic in row['url'] for topic in (
        'processes-and-concurrency', 'chapter-12-normalization', 'quick_sort', 'chapter_heap')) for row in rows)


@pytest.mark.parametrize('query', [
    'process', 'What is a process?', 'Explain operating system processes',
    'processes and threads', 'CPU process scheduling', '操作系统中进程的调度',
])
def test_contextual_or_direct_process_queries_still_find_os_sources(query):
    rows = run(sources.search(settings(), query, limit=20))
    assert any('processes-and-concurrency' in row['url'] for row in rows)


@pytest.mark.parametrize('query', ['nonpython', 'αpython', 'pythoné', 'nonpython列表', 'business processing'])
def test_aliases_do_not_match_inside_larger_words(query):
    assert run(sources.search(settings(), query)) == []


def test_bilingual_mixed_text_spacing_and_unicode_punctuation():
    assert run(sources.search(settings(), '请解释Python列表', language='zh'))[0]['language'] == 'zh'
    rows = run(sources.search(settings(), 'Could u explain quick—sort   partition?'))
    assert 'quick_sort' in rows[0]['url']
    assert not {'terms', 'ambiguous_terms', 'context_terms'}.intersection(rows[0])


@pytest.mark.parametrize('query,language', [
    ('Could u give me a brief introduction about machine learning?', 'en'),
    ('machine learning workflow and training process', 'en'),
    ('监督学习、无监督学习和强化学习有什么区别？', 'zh'),
    ('介绍一下机器学习和模型训练的流程', 'zh'),
])
def test_machine_learning_queries_find_primary_sources_without_os_false_positives(query, language):
    rows = run(sources.search(settings(), query, language=language, limit=20))
    assert rows and all('processes-and-concurrency' not in row['url'] for row in rows)
    assert any('developers.google.com/machine-learning/' in row['url'] for row in rows)
    assert all(row['storage_policy'] == 'open_license' for row in rows)
    assert any(row['language'] == language for row in rows)


@pytest.mark.parametrize('query,language', [
    ('Could you give me questions about "L\'Hôpital\'s rule"?', 'en'),
    ('Explain L’Hopital’s rule conditions and examples', 'en'),
    ('What is l hospital rule?', 'en'),
    ('洛必达法则的适用条件和例题', 'zh'),
])
def test_calculus_queries_find_reviewed_source_across_spelling_variants(query, language):
    rows = run(sources.search(settings(), query, language=language))
    assert rows and rows[0]['title'] == "Mathematics LibreTexts: L'Hôpital's Rule"
    assert rows[0]['storage_policy'] == 'open_license'
    assert rows[0]['license'] == 'CC BY 4.0'
    assert rows[0]['provider'] == 'curated'


def test_calculus_license_is_exact_page_scoped_and_not_a_hospital_false_positive(monkeypatch):
    assert run(sources.search(settings(), 'hospital management')) == []
    url = next(item['url'] for item in sources.CATALOG
               if item['title'] == "Mathematics LibreTexts: L'Hôpital's Rule")
    assert sources.source_metadata(url)['license'] == 'CC BY 4.0'
    assert sources.source_metadata(url + '?revision=unreviewed')['storage_policy'] == 'unknown'
    wire(monkeypatch, [response(b'<html><body>L\'Hopital rule worked example</body></html>', 'text/html')])
    fetched = run(sources.fetch_source(settings(), url))
    assert fetched['storage_policy'] == 'open_license'
    assert fetched['license_verification'] == 'catalog_reviewed'


def test_primary_machine_learning_license_metadata_is_exact_page_scoped():
    google_zh = 'https://developers.google.com/machine-learning/intro-to-ml/what-is-ml?hl=zh-cn'
    assert sources.source_metadata(google_zh)['language'] == 'zh'
    assert sources.source_metadata(google_zh)['license'] == 'CC BY 4.0'
    assert sources.source_metadata(google_zh.replace('zh-cn', 'unreviewed'))['storage_policy'] == 'unknown'
    d2l = sources.source_metadata('https://d2l.ai/chapter_introduction/index.html')
    assert d2l['license'] == 'CC BY-SA 4.0'
    assert 'Zachary C. Lipton' in d2l['attribution']


def test_openstax_specific_ai_ingestion_restriction_is_respected():
    rows = [item for item in sources.CATALOG if item['url'].startswith('https://openstax.org/')]
    assert len(rows) == 3
    assert all(item['storage_policy'] == 'link_only' for item in rows)


def test_license_is_exact_page_bound_and_redirects_cannot_inherit_it():
    known = sources.CATALOG[0]['url']
    assert sources.source_metadata(known + '#section')['storage_policy'] == 'open_license'
    assert sources.source_metadata(known + '?download=other')['storage_policy'] == 'unknown'
    assert sources.source_metadata('https://www.hello-algo.com/unreviewed-page/')['storage_policy'] == 'unknown'
    assert sources.source_metadata('https://www.hello-algo.com.evil.org/')['storage_policy'] == 'unknown'


@pytest.mark.parametrize('query', ['', 'x' * 601, 'x ' * 76, None])
def test_invalid_queries_are_rejected(query):
    with pytest.raises(sources.SearchUnavailable) as exc:
        run(sources.search(settings(), query))
    assert exc.value.code == 'invalid_query'


def test_brave_requires_configuration():
    with pytest.raises(sources.SearchUnavailable) as exc:
        run(sources.search(settings(exploration_search_provider='brave'), 'python'))
    assert exc.value.code == 'search_not_configured'


def mock_brave(monkeypatch, responder):
    original = httpx.AsyncClient
    options = []

    def streaming_reply(request):
        result = responder(request)
        if result.is_stream_consumed:
            return httpx.Response(result.status_code, headers=result.headers, stream=httpx.ByteStream(result.content))
        return result

    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(streaming_reply), **kwargs)

    monkeypatch.setattr(sources.httpx, 'AsyncClient', client)
    return options


def test_hybrid_adds_bounded_public_articles_without_search_key(monkeypatch):
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={'query': {'search': [
            {'ns': 0, 'pageid': 42, 'title': 'Quantum entanglement', 'snippet': '<b>Quantum</b> topic'},
            {'ns': 14, 'pageid': 7, 'title': 'Category:Physics', 'snippet': 'not an article'},
            {'ns': 0, 'pageid': 8, 'title': 'Quantum (disambiguation)', 'snippet': 'not evidence'},
        ]}})

    options = mock_brave(monkeypatch, reply)
    rows = run(sources.search(settings(exploration_search_provider='hybrid'),
                              'quantum entanglement', language='en', limit=3))
    assert [row['url'] for row in rows] == ['https://en.wikipedia.org/wiki/Quantum_entanglement']
    assert rows[0]['provider'] == 'wikipedia' and rows[0]['storage_policy'] == 'unknown'
    assert rows[0]['snippet'] == 'Quantum topic'
    assert str(requests[0].url).startswith(sources.WIKIMEDIA_ENDPOINTS['en'] + '?')
    assert requests[0].url.params['srnamespace'] == '0'
    assert requests[0].headers['User-Agent'].startswith('LumoriFYP/')
    assert options[0]['follow_redirects'] is False and options[0]['trust_env'] is False


def test_hybrid_preserves_catalog_priority_and_chinese_search(monkeypatch):
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={'query': {'search': [
            {'ns': 0, 'pageid': 9, 'title': '机器学习', 'snippet': '百科文章'},
        ]}})

    mock_brave(monkeypatch, reply)
    rows = run(sources.search(settings(exploration_search_provider='hybrid'),
                              '机器学习的定义', language='zh', limit=8))
    assert rows[0]['provider'] == 'curated'
    assert any(row['url'] == 'https://zh.wikipedia.org/wiki/%E6%9C%BA%E5%99%A8%E5%AD%A6%E4%B9%A0'
               for row in rows)
    assert str(requests[0].url).startswith(sources.WIKIMEDIA_ENDPOINTS['zh'] + '?')


def test_hybrid_uses_catalog_when_public_search_fails_and_reports_total_outage(monkeypatch):
    mock_brave(monkeypatch, lambda request: httpx.Response(503, text='temporarily unavailable'))
    rows = run(sources.search(settings(exploration_search_provider='hybrid'), 'machine learning'))
    assert rows and all(row['provider'] == 'curated' for row in rows)
    with pytest.raises(sources.SearchUnavailable):
        run(sources.search(settings(exploration_search_provider='hybrid'), 'quantum entanglement'))


def test_public_article_still_requires_license_at_download(monkeypatch):
    article = 'https://en.wikipedia.org/wiki/Quantum_entanglement'
    assert sources.source_metadata(article)['storage_policy'] == 'unknown'
    body = (b'<html><head><title>Quantum entanglement</title>'
            b'<link rel="license" href="https://creativecommons.org/licenses/by-sa/4.0/">'
            b'</head><body><main>Public article text.</main></body></html>')
    wire(monkeypatch, [response(body, 'text/html')])
    fetched = run(sources.fetch_source(settings(), article))
    assert fetched['storage_policy'] == 'open_license'
    assert fetched['license'] == 'CC BY-SA 4.0'
    assert fetched['license_verification'] == 'publisher_declared'


def test_brave_uses_fixed_endpoint_safe_parse_and_no_redirects(monkeypatch):
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={'web': {'results': [
            {'url': 'https://public.example.org/notes', 'title': '<b>Notes</b>', 'description': '<em>Course</em> &amp; text'},
            {'url': 'https://public.example.org/notes#section', 'title': 'Duplicate'},
            {'url': 'http://127.0.0.1/admin'},
            {'url': 'https://docs.python.org/3/tutorial/datastructures.html', 'title': 'Python'},
        ]}})

    options = mock_brave(monkeypatch, reply)
    result = run(sources.search(settings(exploration_search_provider='brave', exploration_search_api_key='test-only-key'),
                                '哈希表 python', language='zh', limit=2))
    assert str(requests[0].url).startswith(sources.BRAVE_ENDPOINT + '?')
    assert requests[0].url.params['search_lang'] == 'zh-hans'
    assert requests[0].headers['X-Subscription-Token'] == 'test-only-key'
    assert options[0]['follow_redirects'] is False and options[0]['trust_env'] is False
    assert len(result) == 2
    assert result[0]['title'] == 'Notes' and result[0]['snippet'] == 'Course & text'
    assert result[0]['storage_policy'] == 'unknown'
    assert result[1]['storage_policy'] == 'open_license'
    assert 'test-only-key' not in str(result)


@pytest.mark.parametrize('status,code', [(302, 'search_unavailable'), (429, 'search_rate_limited'), (500, 'search_unavailable')])
def test_brave_upstream_errors_never_leak_details(monkeypatch, status, code):
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'http://127.0.0.1/'}, text='test-secret upstream content')

    mock_brave(monkeypatch, reply)
    with pytest.raises(sources.SearchUnavailable) as exc:
        run(sources.search(settings(exploration_search_provider='brave', exploration_search_api_key='test-secret'), 'python'))
    assert exc.value.code == code
    assert len(requests) == 1
    assert 'test-secret' not in str(exc.value)


def test_brave_response_size_is_bounded(monkeypatch):
    mock_brave(monkeypatch, lambda _: httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1)))
    with pytest.raises(sources.SearchUnavailable) as exc:
        run(sources.search(settings(exploration_search_provider='brave', exploration_search_api_key='test-only'), 'python'))
    assert exc.value.code == 'search_invalid_response'


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'ftp://public.example.org/a', 'http://localhost/',
    'http://127.0.0.1/', 'http://10.0.0.1/', 'http://169.254.169.254/latest/meta-data/',
    'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://[64:ff9b::7f00:1]/',
    'http://[fe80::1%25en0]/', 'http://internal.local/', 'http://2130706433/',
    'https://user:password@public.example.org/', 'https://public.example.org:8080/',
    'https://public.example.org/\r\nInjected:header', 'https://public.example.org\\@evil.org/',
])
def test_reject_unsafe_urls_before_connecting(monkeypatch, url):
    seen = wire(monkeypatch, [])
    with pytest.raises(sources.SourceRejected):
        run(sources.fetch_source(settings(), url))
    assert not seen.connections and not seen.resolutions


@pytest.mark.parametrize('addresses', [
    ['10.0.0.2'], ['93.184.216.34', '127.0.0.1'], ['93.184.216.34', 'fe80::1'],
    ['100.64.0.1'], ['224.0.0.1'], ['192.0.2.1'], [],
])
def test_all_dns_records_must_be_public(monkeypatch, addresses):
    seen = wire(monkeypatch, [], dns=lambda *_: addresses)
    with pytest.raises(sources.SourceRejected):
        run(sources.fetch_source(settings(), 'https://public.example.org/'))
    assert not seen.connections


def test_download_pins_ip_preserves_tls_hostname_and_sends_no_credentials(monkeypatch):
    seen = wire(monkeypatch, [response()], dns=lambda host, count: ['93.184.216.34'] if count == 1 else ['127.0.0.1'])
    result = run(sources.fetch_source(settings(exploration_search_api_key='secret-not-for-downloads'), 'https://public.example.org/course'))
    assert result['raw'] == b'public teaching text'
    assert result['storage_policy'] == 'unknown'
    assert seen.resolutions == ['public.example.org']
    connection = seen.connections[0]
    assert connection['host'] == '93.184.216.34'
    assert connection['flags'] == socket.AI_NUMERICHOST
    assert connection['server_hostname'] == 'public.example.org'
    assert connection['ssl'].check_hostname is True
    assert connection['ssl'].verify_mode == ssl.CERT_REQUIRED
    assert b'Host: public.example.org\r\n' in seen.writers[0].data
    assert b'secret' not in seen.writers[0].data
    assert b'Cookie:' not in seen.writers[0].data and b'Authorization:' not in seen.writers[0].data
    assert seen.writers[0].closed


def test_redirect_rechecks_dns_including_same_hostname_rebinding(monkeypatch):
    seen = wire(monkeypatch, [b'HTTP/1.1 302 Found\r\nLocation: /next\r\n\r\n'],
                dns=lambda _, n: ['93.184.216.34'] if n == 1 else ['127.0.0.1'])
    with pytest.raises(sources.SourceRejected) as exc:
        run(sources.fetch_source(settings(), 'https://public.example.org/course'))
    assert exc.value.code == 'private_address'
    assert seen.resolutions == ['public.example.org', 'public.example.org']
    assert len(seen.connections) == 1 and seen.writers[0].closed


def test_redirect_to_private_ip_never_connects(monkeypatch):
    seen = wire(monkeypatch, [b'HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/meta\r\n\r\n'])
    with pytest.raises(sources.SourceRejected):
        run(sources.fetch_source(settings(), 'https://public.example.org/'))
    assert len(seen.connections) == 1


def test_redirects_preserve_provenance_and_do_not_inherit_license(monkeypatch):
    known = sources.CATALOG[0]['url']
    wire(monkeypatch, [b'HTTP/1.1 302 Found\r\nLocation: https://public.example.org/new\r\n\r\n', response()])
    result = run(sources.fetch_source(settings(), known))
    assert result['original_url'] == known
    assert result['url'] == 'https://public.example.org/new'
    assert result['storage_policy'] == 'unknown' and 'license' not in result


@pytest.mark.parametrize('location,code', [('/course', 'redirect_limit'), ('http://public.example.org/next', 'redirect_downgrade')])
def test_redirect_loop_and_https_downgrade(monkeypatch, location, code):
    wire(monkeypatch, [b'HTTP/1.1 302 Found\r\nLocation: ' + location.encode() + b'\r\n\r\n'])
    with pytest.raises(sources.SourceRejected) as exc:
        run(sources.fetch_source(settings(), 'https://public.example.org/course'))
    assert exc.value.code == code


@pytest.mark.parametrize('payload,code', [
    (response(b'x' * 1025), 'too_large'),
    (b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n' + b'x' * 1025, 'too_large'),
    (b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n401\r\n', 'too_large'),
    (response(b'bad', 'application/zip'), 'unsupported_type'),
    (response(b'not a pdf', 'application/pdf'), 'unsupported_type'),
    (response(extra=b'Content-Encoding: gzip\r\n'), 'unsupported_encoding'),
    (response(extra=b'Content-Length: 19\r\n'), 'invalid_response'),
    (response(extra=b'Transfer-Encoding: chunked\r\n'), 'invalid_response'),
    (b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: -1\r\n\r\n', 'invalid_response'),
    (b'HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nprivate-upstream-text', 'source_unavailable'),
])
def test_hostile_body_and_header_limits(monkeypatch, payload, code):
    seen = wire(monkeypatch, [payload])
    with pytest.raises(sources.SourceRejected) as exc:
        run(sources.fetch_source(settings(), 'https://public.example.org/'))
    assert exc.value.code == code
    assert 'private-upstream-text' not in str(exc.value)
    assert seen.writers[0].closed


def test_chunked_and_pdf_downloads_are_supported(monkeypatch):
    wire(monkeypatch, [b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ntext\r\n0\r\n\r\n'])
    assert run(sources.fetch_source(settings(), 'https://public.example.org/'))['raw'] == b'text'
    wire(monkeypatch, [response(b'%PDF-1.7\nsample', 'application/pdf')])
    assert run(sources.fetch_source(settings(), 'https://public.example.org/'))['content_type'] == 'application/pdf'


def test_total_timeout_covers_stalled_headers_and_closes_connection(monkeypatch):
    seen = wire(monkeypatch, [None])
    with pytest.raises(sources.SourceRejected) as exc:
        run(sources.fetch_source(settings(exploration_fetch_timeout=0.01), 'https://public.example.org/'))
    assert exc.value.code == 'fetch_timeout'
    assert seen.writers[0].closed


@pytest.mark.parametrize('dns_ips', [
    ['198.18.0.12'],
    ['198.18.0.72', '::ffff:0:c612:48'],
    ['::ffff:0:c612:48', '198.18.0.72'],
    ['::ffff:0:c612:48'],
    ['::ffff:0:c612:0', '::ffff:0:c613:ffff'],
])
def test_fake_ip_fallback_remains_pinned_and_system_mode_never_falls_back(monkeypatch, dns_ips):
    seen = wire(monkeypatch, [response()], dns=lambda *_: dns_ips)
    questions = []

    async def doh(host, kind):
        questions.append((host, kind))
        return [{'type': 1, 'data': '93.184.216.34'}] if kind == 'A' else []

    monkeypatch.setattr(sources, '_doh_question', doh)
    run(sources.fetch_source(settings(), 'https://public.example.org/'))
    assert seen.connections[0]['host'] == '93.184.216.34'
    assert {kind for _, kind in questions} == {'A', 'AAAA'}
    questions.clear()
    with pytest.raises(sources.SourceRejected) as exc:
        run(sources.fetch_source(settings(exploration_dns_mode='system'), 'https://public.example.org/'))
    assert exc.value.code == 'private_address' and not questions
    assert [connection['host'] for connection in seen.connections] == ['93.184.216.34']


@pytest.mark.parametrize('dns_ips', [
    ['198.18.0.1', '127.0.0.1'], ['192.168.0.1'], ['169.254.169.254'],
    ['198.18.0.72', '::ffff:0:7f00:1'],
    ['::ffff:0:c612:48', '192.168.0.1'],
    ['192.168.0.1', '::ffff:0:c612:48'],
    ['::ffff:0:c612:48', '::ffff:0:7f00:1'],
    ['::ffff:0:7f00:1', '::ffff:0:c612:48'],
    ['::ffff:c612:48'],  # IPv4-mapped is a distinct prefix.
    ['64:ff9b::c612:48'],  # NAT64 is not this proxy's translated form.
    ['::ffff:0:5db8:d822'],  # A translated public address is still rejected.
    ['::ffff:0:c611:ffff'], ['::ffff:0:c614:0'],  # Outside the fake /15.
    ['::ffff:0:c612:48%en0'],
])
def test_doh_fallback_never_repairs_nonfake_private_addresses(monkeypatch, dns_ips):
    seen = wire(monkeypatch, [], dns=lambda *_: dns_ips)

    async def forbidden(*_):
        pytest.fail('DoH must not run for private system DNS results')

    monkeypatch.setattr(sources, '_doh_question', forbidden)
    with pytest.raises(sources.SourceRejected) as exc:
        run(sources.fetch_source(settings(), 'https://public.example.org/'))
    assert exc.value.code == 'private_address'
    assert not seen.connections


@pytest.mark.parametrize('private_answer', [
    {'type': 1, 'data': '127.0.0.1'}, {'type': 28, 'data': '::1'},
    {'type': 28, 'data': '::ffff:0:c612:48'},
    {'type': 5, 'data': 'internal.local.'},
])
def test_doh_rejects_private_addresses_and_private_cname(monkeypatch, private_answer):
    seen = wire(monkeypatch, [], dns=lambda *_: ['198.18.0.1'])

    async def doh(host, kind):
        return [{'type': 1, 'data': '93.184.216.34'}, private_answer]

    monkeypatch.setattr(sources, '_doh_question', doh)
    with pytest.raises(sources.SourceRejected):
        run(sources.fetch_source(settings(), 'https://public.example.org/'))
    assert not seen.connections


def test_doh_checks_both_families_of_cname_targets(monkeypatch):
    seen = wire(monkeypatch, [], dns=lambda *_: ['198.18.0.1'])

    async def doh(host, kind):
        if host == 'public.example.org':
            return [{'type': 5, 'data': 'alias.example.org.'}, {'type': 1, 'data': '93.184.216.34'}]
        return [{'type': 1, 'data': '93.184.216.34'}] if kind == 'A' else [{'type': 28, 'data': 'fc00::1'}]

    monkeypatch.setattr(sources, '_doh_question', doh)
    with pytest.raises(sources.SourceRejected) as exc:
        run(sources.fetch_source(settings(), 'https://public.example.org/'))
    assert exc.value.code == 'private_address' and not seen.connections


def test_doh_fixed_https_endpoint_request_contract(monkeypatch):
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={'Status': 0, 'Question': [{'name': 'public.example.org.', 'type': 1}],
                                         'Answer': [{'type': 1, 'data': '93.184.216.34'}]})

    options = mock_brave(monkeypatch, reply)
    assert run(sources._doh_question('public.example.org', 'A'))[0]['data'] == '93.184.216.34'
    assert requests[0].url.host == '1.1.1.1' and requests[0].url.scheme == 'https'
    assert requests[0].url.path == '/dns-query'
    assert requests[0].headers['Accept'] == 'application/dns-json'
    assert 'cookie' not in requests[0].headers and 'authorization' not in requests[0].headers
    assert options[0]['trust_env'] is False and options[0]['follow_redirects'] is False


@pytest.mark.parametrize('reply', [
    httpx.Response(302, headers={'Location': 'http://127.0.0.1/'}),
    httpx.Response(200, content=b'x' * 65537),
    httpx.Response(200, json={'Status': 0, 'Question': [{'name': 'wrong.example.org.', 'type': 1}]}),
])
def test_doh_redirects_oversize_and_unmatched_answers_rejected(monkeypatch, reply):
    mock_brave(monkeypatch, lambda _: reply)
    with pytest.raises(sources.SourceRejected) as exc:
        run(sources._doh_question('public.example.org', 'A'))
    assert exc.value.code == 'dns_failed'


@pytest.mark.parametrize('page', [
    b'<html><head><title>Course notes</title><meta name="author" content="Course author"><link rel="license" href="https://creativecommons.org/licenses/by/4.0/"></head><body>Course.</body></html>',
    b'<html><head><title>Course notes</title><meta name="author" content="Course author"></head><body><footer>Copyright. Licensed under <a rel="license" href="https://creativecommons.org/licenses/by/4.0/">CC BY</a>.</footer></body></html>',
])
def test_explicit_html_license_allows_unknown_source_with_declared_evidence(monkeypatch, page):
    wire(monkeypatch, [response(page, 'text/html')])
    result = run(sources.fetch_source(settings(), 'https://public.example.org/new-course'))
    assert result['storage_policy'] == 'open_license'
    assert result['license'] == 'CC BY 4.0'
    assert result['attribution'] == 'Course author'
    assert result['license_verification'] == 'publisher_declared'
    assert result['license_evidence']['kind'] in {'html_head_rel_license', 'html_footer_rel_license'}


@pytest.mark.parametrize('page', [
    b'<article>This article discusses licenses. <a rel="license" href="https://creativecommons.org/licenses/by/4.0/">CC BY</a></article>',
    b'<head><link rel="license" href="https://creativecommons.org.evil.org/licenses/by/4.0/"></head>',
    b'<head><link rel="license" href="https://creativecommons.org/licenses/by-nd/4.0/"></head>',
    b'<head><link rel="license" href="https://creativecommons.org/licenses/by/4.0/"><link rel="license" href="https://creativecommons.org/licenses/by-nc/4.0/"></head>',
    b'<script><link rel="license" href="https://creativecommons.org/licenses/by/4.0/"></script>',
])
def test_unreliable_or_conflicting_license_markup_remains_unknown(page):
    assert sources._declared_license(page, 'https://public.example.org/')['storage_policy'] == 'unknown'


@pytest.mark.parametrize('markup', [b'<link rel>', b'<meta name>', b'<a rel href>', b'<![unknown]>'])
def test_missing_html_attribute_values_and_unreadable_license_remain_unknown(monkeypatch, markup):
    page = b'<html><head>' + markup + b'</head><body>Public course text.</body></html>'
    seen = wire(monkeypatch, [response(page, 'text/html')])
    result = run(sources.fetch_source(settings(), 'https://public.example.org/course'))
    assert result['storage_policy'] == 'unknown'
    assert 'license' not in result and 'license_verification' not in result
    assert seen.writers[0].closed


def test_deep_html_rejects_even_an_earlier_license_declaration():
    declared = (b'<html><head><link rel="license" '
                b'href="https://creativecommons.org/licenses/by/4.0/"></head><body>')
    within_limit = (declared + b'<div>' * (sources.MAX_LICENSE_HTML_DEPTH - 2)
                    + b'Course text' + b'</div>' * (sources.MAX_LICENSE_HTML_DEPTH - 2) + b'</body></html>')
    assert sources._declared_license(within_limit, 'https://public.example.org/')['storage_policy'] == 'open_license'
    over_limit = declared + b'<div>' * sources.MAX_LICENSE_HTML_DEPTH + b'Course text'
    result = sources._declared_license(over_limit, 'https://public.example.org/')
    assert result == {'storage_policy': 'unknown'}

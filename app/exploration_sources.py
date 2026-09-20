"""Bounded public-source discovery and credential-free, DNS-pinned downloads.

``curated`` searches a small *local catalog of real URLs*, not a live web index.
The bilingual aliases are discovery hints, never evidence of source relevance.
``brave`` uses the documented Web Search API; snippets are likewise hints only.
No downloaded content is executed and this module never writes to the database.

Catalog/license references were checked on 2026-09-20. Open licenses still carry
attribution/noncommercial/share-alike requirements and third-party exceptions.
Unknown or link-only sources must not be automatically persisted by callers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
import ssl
import unicodedata
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx


CATALOG_VERSION = 'cs-public-sources-20260920-v3'
BRAVE_ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
BRAVE_DOCUMENTATION = 'https://api-dashboard.search.brave.com/app/documentation/web-search'
DOH_ENDPOINT = 'https://1.1.1.1/dns-query'
DNS_POLICY_VERSION = 'public_address_pinning_v2'
MAX_REDIRECTS = 4
MAX_HEADERS = 64 * 1024
MAX_LICENSE_HTML_DEPTH = 256
ALLOWED_TYPES = frozenset({'text/html', 'application/xhtml+xml', 'application/pdf', 'text/plain', 'text/markdown'})

_ERRORS = {
    'search_unavailable': 'Public source search is unavailable.',
    'search_not_configured': 'The selected search provider is not configured.',
    'search_invalid_response': 'The search provider returned an invalid response.',
    'search_rate_limited': 'The search provider temporarily limited requests.',
    'invalid_query': 'The search query is empty or too long.',
    'invalid_url': 'The source URL is not an allowed public HTTP URL.',
    'private_address': 'The source does not resolve exclusively to public addresses.',
    'dns_failed': 'The source address could not be resolved.',
    'fetch_failed': 'The source could not be downloaded.',
    'fetch_timeout': 'The source download exceeded its time limit.',
    'too_large': 'The source exceeds the download size limit.',
    'unsupported_type': 'The source is not an accepted document type.',
    'unsupported_encoding': 'The source uses an unsupported content encoding.',
    'invalid_response': 'The source returned an invalid HTTP response.',
    'redirect_limit': 'The source exceeded the redirect limit.',
    'redirect_downgrade': 'The source redirected to an insecure connection.',
    'source_unavailable': 'The source is not publicly available.',
}


class SourceRejected(ValueError):
    """Only fixed safe codes/messages, never URLs, keys, or upstream bodies."""
    def __init__(self, code: str = 'fetch_failed'):
        self.code = code if code in _ERRORS else 'fetch_failed'
        super().__init__(_ERRORS[self.code])


class SearchUnavailable(SourceRejected):
    def __init__(self, code: str = 'search_unavailable'):
        super().__init__(code)


def _entry(url, title, terms, *, language='en', attribution, license, license_url,
           storage_policy='open_license', ambiguous_terms=(), context_terms=()):
    return dict(url=url, title=title, terms=tuple(terms), language=language,
                attribution=attribution, license=license, license_url=license_url,
                storage_policy=storage_policy, ambiguous_terms=tuple(ambiguous_terms),
                context_terms=tuple(context_terms))


_HELLO_LICENSE = 'https://github.com/krahets/hello-algo/blob/main/LICENSE'
_CC_NC = 'CC BY-NC-SA 4.0'
_HELLO_AMBIGUOUS = frozenset({'pivot', 'partition', 'heap'})
_HELLO_CONTEXT = ('sorting', 'sort', 'algorithm', 'algorithms', 'data structure', 'data structures',
                  '排序', '算法', '数据结构')
_HELLO_TOPICS = (
    ('chapter_sorting/quick_sort/', 'Quick sort / 快速排序', ('quick sort', 'quicksort', '快速排序', '快排', 'pivot', 'partition', '哨兵划分')),
    ('chapter_sorting/merge_sort/', 'Merge sort / 归并排序', ('merge sort', 'mergesort', '归并排序', '归并')),
    ('chapter_graph/graph_traversal/', 'Graph traversal / 图的遍历', ('graph traversal', 'breadth first', 'depth first', 'bfs', 'dfs', '图遍历', '图的遍历', '广度优先', '深度优先')),
    ('chapter_dynamic_programming/intro_to_dynamic_programming/', 'Dynamic programming / 动态规划', ('dynamic programming', '动态规划', 'memoization', '记忆化', '状态转移')),
    ('chapter_tree/binary_search_tree/', 'Binary search tree / 二叉搜索树', ('binary search tree', 'bst', '二叉搜索树', '二叉查找树')),
    ('chapter_heap/heap/', 'Heap / 堆', ('binary heap', 'heap', 'priority queue', '二叉堆', '最大堆', '最小堆', '优先队列')),
    ('chapter_hashing/hash_map/', 'Hash table / 哈希表', ('hash table', 'hash map', 'hashing', '哈希表', '散列表', '哈希冲突')),
    ('chapter_computational_complexity/time_complexity/', 'Time complexity / 时间复杂度', ('time complexity', 'big o', 'asymptotic', '时间复杂度', '渐近分析')),
)

CATALOG = tuple(
    _entry('https://www.hello-algo.com/' + ('en/' if language == 'en' else '') + path,
           title, terms, language=language, attribution='krahets and Hello Algo contributors',
           license=_CC_NC, license_url=_HELLO_LICENSE,
           ambiguous_terms=sorted(_HELLO_AMBIGUOUS.intersection(terms)), context_terms=_HELLO_CONTEXT)
    for path, title, terms in _HELLO_TOPICS for language in ('zh', 'en')
) + (
    _entry('https://openstax.org/books/introduction-computer-science/pages/6-2-fundamental-os-concepts',
           'OpenStax: Fundamental OS Concepts',
           ('operating system', 'os concepts', '操作系统', '系统调用', 'kernel'),
           attribution='Jean-Claude Franchitti / Rice University, OpenStax', license=_CC_NC,
           license_url='https://openstax.org/books/introduction-computer-science/pages/preface',
           storage_policy='link_only', ambiguous_terms=('kernel',),
           context_terms=('computer', 'cpu', 'linux', 'unix', 'windows')),
    _entry('https://openstax.org/books/introduction-computer-science/pages/6-3-processes-and-concurrency',
           'OpenStax: Processes and Concurrency',
           ('cpu scheduling', 'process scheduling', 'process and thread', 'processes and threads',
            'scheduling', 'round robin', 'process', 'processes', 'thread', 'threads', 'concurrency', 'deadlock',
            '进程', '线程', '调度', '时间片', '并发', '死锁'),
           attribution='Jean-Claude Franchitti / Rice University, OpenStax', license=_CC_NC,
           license_url='https://openstax.org/books/introduction-computer-science/pages/preface',
           storage_policy='link_only', ambiguous_terms=('process', 'processes', 'thread', 'threads', 'scheduling', '调度'),
           context_terms=('operating system', 'operating systems', 'os', 'cpu', 'kernel', '操作系统')),
    _entry('https://openstax.org/books/introduction-computer-science/pages/6-4-memory-management',
           'OpenStax: Memory Management',
           ('virtual memory', 'paging', 'page replacement', 'tlb', '内存管理', '虚拟内存', '分页', '页面置换'),
           attribution='Jean-Claude Franchitti / Rice University, OpenStax', license=_CC_NC,
           license_url='https://openstax.org/books/introduction-computer-science/pages/preface',
           storage_policy='link_only'),
    _entry('https://opentextbc.ca/dbdesign01/chapter/chapter-12-normalization/',
           'Database Design: Normalization',
           ('database normalization', 'normalization', 'normal form', 'bcnf', '1nf', '2nf', '3nf', '数据库范式', '规范化', '范式'),
           attribution='Adrienne Watt / BCcampus', license='CC BY 4.0',
           license_url='https://opentextbc.ca/dbdesign01/chapter/chapter-12-normalization/',
           ambiguous_terms=('normalization', '规范化'), context_terms=('database', 'relational', '数据库', '关系数据库')),
    _entry('https://opentextbc.ca/dbdesign01/chapter/chapter-8-entity-relationship-model/',
           'Database Design: Entity Relationship Model',
           ('entity relationship', 'er model', 'database design', '实体关系', '实体联系', '数据库设计', 'er图'),
           attribution='Adrienne Watt / BCcampus', license='CC BY 4.0',
           license_url='https://opentextbc.ca/dbdesign01/chapter/chapter-8-entity-relationship-model/'),
    _entry('https://docs.python.org/3/tutorial/datastructures.html', 'Python Tutorial: Data Structures',
           ('python', 'python list', 'python dictionary', 'python列表', 'python字典', '列表推导式'),
           attribution='Python Software Foundation', license='Python documentation license (PSF)',
           license_url='https://docs.python.org/3/license.html'),
    _entry('https://docs.python.org/zh-cn/3/tutorial/datastructures.html', 'Python 教程：数据结构',
           ('python', 'python list', 'python dictionary', 'python列表', 'python字典', '列表推导式'), language='zh',
           attribution='Python Software Foundation and translators', license='Python documentation license (PSF)',
           license_url='https://docs.python.org/3/license.html'),
    _entry('https://developers.google.com/machine-learning/intro-to-ml/what-is-ml',
           'Google: What is machine learning?',
           ('machine learning', 'supervised learning', 'unsupervised learning', 'reinforcement learning',
            '机器学习', '监督学习', '无监督学习', '强化学习'),
           attribution='Google Developers', license='CC BY 4.0',
           license_url='https://creativecommons.org/licenses/by/4.0/'),
    _entry('https://developers.google.com/machine-learning/intro-to-ml/what-is-ml?hl=zh-cn',
           'Google：什么是机器学习？',
           ('machine learning', 'supervised learning', 'unsupervised learning', 'reinforcement learning',
            '机器学习', '监督学习', '无监督学习', '强化学习'), language='zh',
           attribution='Google Developers', license='CC BY 4.0',
           license_url='https://creativecommons.org/licenses/by/4.0/'),
    _entry('https://developers.google.com/machine-learning/intro-to-ml/supervised',
           'Google: Supervised learning',
           ('supervised learning', 'machine learning workflow', 'model training', 'model evaluation',
            'training data', 'features and labels', '监督学习', '机器学习流程', '模型训练', '模型评估', '训练数据'),
           attribution='Google Developers', license='CC BY 4.0',
           license_url='https://creativecommons.org/licenses/by/4.0/'),
    _entry('https://developers.google.com/machine-learning/intro-to-ml/supervised?hl=zh-cn',
           'Google：监督学习',
           ('supervised learning', 'machine learning workflow', 'model training', 'model evaluation',
            'training data', 'features and labels', '监督学习', '机器学习流程', '模型训练', '模型评估', '训练数据'),
           language='zh', attribution='Google Developers', license='CC BY 4.0',
           license_url='https://creativecommons.org/licenses/by/4.0/'),
    _entry('https://d2l.ai/chapter_introduction/index.html', 'Dive into Deep Learning: Introduction',
           ('machine learning', 'deep learning', 'supervised learning', 'unsupervised learning',
            'reinforcement learning', 'model training', 'training data', '机器学习', '深度学习',
            '监督学习', '无监督学习', '强化学习', '模型训练', '训练数据'),
           attribution='Aston Zhang, Zachary C. Lipton, Mu Li, and Alexander J. Smola',
           license='CC BY-SA 4.0', license_url='https://github.com/d2l-ai/d2l-en/blob/master/LICENSE'),
    # Authors explicitly ask instructors to link, not keep local chapter copies.
    _entry('https://pages.cs.wisc.edu/~remzi/OSTEP/cpu-sched-mlfq.pdf', 'OSTEP: Multi-Level Feedback Queue',
           ('mlfq', 'multilevel feedback', 'multi level feedback', '多级反馈队列', 'priority boost', '优先级提升'),
           attribution='Remzi H. Arpaci-Dusseau and Andrea C. Arpaci-Dusseau',
           license='Publicly readable; local redistribution permission not established',
           license_url='https://pages.cs.wisc.edu/~remzi/OSTEP/', storage_policy='link_only'),
)

# The author's separately published Markdown originals are useful when the
# rendered website is unavailable. They contain source-template/image references
# rather than expanded code or analyzed figures; callers must retain that limit.
CATALOG += tuple(
    _entry('https://raw.githubusercontent.com/krahets/hello-algo/main/'
           + ('en/' if language == 'en' else '') + 'docs/' + path.rstrip('/') + '.md',
           title, terms, language=language, attribution='krahets and Hello Algo contributors',
           license=_CC_NC, license_url=_HELLO_LICENSE,
           ambiguous_terms=sorted(_HELLO_AMBIGUOUS.intersection(terms)), context_terms=_HELLO_CONTEXT)
    | {'source_format': 'markdown_source',
       'reading_url': 'https://www.hello-algo.com/' + ('en/' if language == 'en' else '') + path}
    for path, title, terms in _HELLO_TOPICS for language in ('zh', 'en')
)

CATALOG += tuple(
    item | {'url': 'https://raw.githubusercontent.com/d2l-ai/d2l-en/master/chapter_introduction/index.md',
            'source_format': 'markdown_source', 'reading_url': item['url']}
    for item in CATALOG if item['url'] == 'https://d2l.ai/chapter_introduction/index.html'
)


def source_metadata(url: str) -> dict:
    """License assertions apply to exact reviewed catalog pages, never a whole host."""
    try:
        key = _validate_url(url).url
    except SourceRejected:
        return {'storage_policy': 'unknown'}
    for item in CATALOG:
        if key == item['url']:
            return {k: item[k] for k in ('title', 'language', 'attribution', 'license', 'license_url', 'storage_policy', 'source_format', 'reading_url') if k in item} | {'license_verification': 'catalog_reviewed'}
    return {'storage_policy': 'unknown'}


def _cc_license(url):
    """Recognize a narrow set of CC declarations; do not follow license URLs."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'creativecommons.org', 'www.creativecommons.org'}
                or parsed.username or parsed.password or parsed.port or parsed.query):
            return None
        match = re.fullmatch(r'/licenses/(by|by-sa|by-nc|by-nc-sa)/(3\.0|4\.0)(?:/deed\.[a-zA-Z-]+)?', parsed.path.rstrip('/'))
        if match:
            return 'CC ' + match[1].upper() + ' ' + match[2], 'https://creativecommons.org/licenses/' + match[1] + '/' + match[2] + '/'
        if parsed.path.rstrip('/') == '/publicdomain/zero/1.0':
            return 'CC0 1.0', 'https://creativecommons.org/publicdomain/zero/1.0/'
    except (ValueError, TypeError):
        pass
    return None


class _LicenseParser(HTMLParser):
    """Publisher declarations only: a head metadata link or explicit footer link."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.declarations, self.title_parts, self.footer_parts = [], [], [], []
        self.author = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            if len(self.stack) >= MAX_LICENSE_HTML_DEPTH:
                raise ValueError('HTML license analysis depth limit exceeded.')
            self.stack.append(tag)
        if 'script' in self.stack or 'style' in self.stack:
            return
        if tag == 'meta' and ((attrs.get('name') or '').lower() in {'author', 'dc.creator'}):
            self.author = _clean_text(attrs.get('content'), 200)
        if 'license' not in (attrs.get('rel') or '').lower().split():
            return
        parsed = _cc_license(attrs.get('href', ''))
        if parsed and tag == 'link' and 'head' in self.stack:
            self.declarations.append((parsed, 'html_head_rel_license'))
        elif parsed and tag == 'a' and 'footer' in self.stack:
            self.declarations.append((parsed, 'html_footer_rel_license'))

    def handle_endtag(self, tag):
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        if 'script' in self.stack or 'style' in self.stack:
            return
        if 'title' in self.stack:
            self.title_parts.append(data)
        if 'footer' in self.stack:
            self.footer_parts.append(data)


def _declared_license(raw, url):
    parser = _LicenseParser()
    try:
        parser.feed(raw.decode('utf-8', errors='replace'))
    except (ValueError, RecursionError, AssertionError):
        return {'storage_policy': 'unknown'}
    footer = ' '.join(parser.footer_parts).casefold()
    declarations = [(item, kind) for item, kind in parser.declarations
                    if kind == 'html_head_rel_license' or any(word in footer for word in ('licensed', 'copyright', '许可', '授权'))]
    if not declarations or len({item for item, _ in declarations}) != 1:
        return {'storage_policy': 'unknown'}
    (license_name, license_url), kind = declarations[0]
    title = _clean_text(' '.join(parser.title_parts), 300)
    return dict(storage_policy='open_license', license=license_name, license_url=license_url,
                title=title, attribution=parser.author or (title or urlsplit(url).hostname),
                license_verification='publisher_declared',
                license_evidence={'kind': kind, 'url': license_url})


def _clean_text(value, limit=600):
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', value))).strip()[:limit]


def _discovery_text(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).casefold()
    return re.sub(r'\s+', ' ', re.sub(r'[-_\u2010-\u2015]+', ' ', value)).strip()


def _matches(query: str, term: str) -> bool:
    # Keep Chinese text unsegmented, including Chinese adjacent to English topic
    # names. Latin aliases must still end at token boundaries; e.g. process does
    # not match processing, and python does not match nonpython or alpha-python
    # written as one word using a non-ASCII alphabetic prefix.
    boundary = r'[^\W\u3400-\u9fff_]'
    prefix = r'(?<!' + boundary + ')' if term[0].isascii() and term[0].isalnum() else ''
    suffix = r'(?!' + boundary + ')' if term[-1].isascii() and term[-1].isalnum() else ''
    return bool(re.search(prefix + re.escape(term) + suffix, query))


_TOPIC_REQUEST_WORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'brief', 'can', 'could', 'describe',
    'do', 'does', 'explain', 'for', 'give', 'how', 'i', 'in', 'introduction', 'is',
    'me', 'of', 'on', 'please', 'the', 'to', 'u', 'us', 'what', 'with', 'would',
    'you', 'your', 'about',
})


def _direct_topic_request(query: str, term: str) -> bool:
    """Allow a bare topic or a short request whose only subject is that topic.

    These words only qualify an ambiguous catalog alias. They never rewrite the
    user request or invent a subject, and other subject words prevent a match.
    """
    remainder = query.replace(term, ' ')
    words = set(re.findall(r'[^\W_]+', remainder))
    return words.issubset(_TOPIC_REQUEST_WORDS)


def _curated_search(query, language, limit):
    normalized = _discovery_text(query)
    ranked = []
    for entry in CATALOG:
        matches = [term for term in entry['terms'] if _matches(normalized, _discovery_text(term))]
        if not matches:
            continue
        ambiguous = set(entry.get('ambiguous_terms', ()))
        topical = [term for term in matches if term not in ambiguous]
        context = any(_matches(normalized, _discovery_text(term)) for term in entry.get('context_terms', ()))
        if not topical and not context:
            matches = [term for term in matches if _direct_topic_request(normalized, _discovery_text(term))]
            if not matches:
                continue
        # Count each distinct phrase once: python and python list are one piece
        # of topical evidence, not two. Contextual aliases rank below explicit
        # subject phrases so weak hits cannot displace a better matched source.
        independent = [term for term in matches if not any(
            term != other and _matches(_discovery_text(other), _discovery_text(term)) for other in matches)]
        score = sum((0.5 if term in ambiguous else 2) + min(len(term), 24) / 24 for term in independent)
        score += 0.25 if entry['language'] == ('zh' if language.startswith('zh') else 'en') else 0
        score += 0.05 if entry.get('source_format') == 'markdown_source' else 0
        result = {k: v for k, v in entry.items() if k not in {'terms', 'ambiguous_terms', 'context_terms'}}
        result.update(provider='curated', snippet='Catalog topics: ' + ', '.join(matches),
                      catalog_version=CATALOG_VERSION)
        ranked.append((score, entry['url'], result))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in ranked[:limit]]


async def search(settings, query: str, language: str = 'en', limit: int = 8) -> list[dict]:
    """Return bounded candidate URLs; each must be fetched and evidence-checked."""
    if not isinstance(query, str) or not query.strip() or len(query) > 600 or len(query.split()) > 75:
        raise SearchUnavailable('invalid_query')
    limit = max(1, min(int(limit), 20))
    provider = getattr(settings, 'exploration_search_provider', 'curated')
    if provider == 'curated':
        return _curated_search(query, language, limit)
    if provider != 'brave' or not getattr(settings, 'exploration_search_api_key', ''):
        raise SearchUnavailable('search_not_configured')
    return await _brave_search(settings, query, language, limit)


async def _brave_search(settings, query, language, limit):
    # Official fixed endpoint; never send this credential on a redirected request.
    # https://api-dashboard.search.brave.com/app/documentation/web-search
    headers = {'Accept': 'application/json', 'X-Subscription-Token': settings.exploration_search_api_key}
    params = {'q': query, 'count': limit, 'search_lang': 'zh-hans' if language.startswith('zh') else 'en', 'safesearch': 'moderate'}
    try:
        timeout = min(30.0, max(1.0, float(getattr(settings, 'exploration_fetch_timeout', 20))))
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                async with client.stream('GET', BRAVE_ENDPOINT, headers=headers, params=params) as response:
                    if response.status_code == 429:
                        raise SearchUnavailable('search_rate_limited')
                    if response.status_code != 200:
                        raise SearchUnavailable()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 2 * 1024 * 1024:
                            raise SearchUnavailable('search_invalid_response')
                        raw.extend(chunk)
        payload = json.loads(raw)
        rows = payload.get('web', {}).get('results', [])
        if not isinstance(rows, list):
            raise SearchUnavailable('search_invalid_response')
        results, seen = [], set()
        for row in rows[:40]:
            if not isinstance(row, dict):
                continue
            try:
                url = _validate_url(row.get('url')).url
            except SourceRejected:
                continue
            if url in seen:
                continue
            seen.add(url)
            result = dict(url=url, title=_clean_text(row.get('title'), 300),
                          snippet=_clean_text(row.get('description')), provider='brave')
            result.update(source_metadata(url))
            results.append(result)
            if len(results) >= limit:
                break
        return results
    except SearchUnavailable:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
        raise SearchUnavailable() from None


@dataclass(frozen=True)
class _Target:
    url: str
    host: str
    port: int
    tls: bool
    path: str


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise SourceRejected('private_address') from None
    # Reject transition/translation forms as well as local, reserved and multicast.
    if (not address.is_global or address.is_multicast or address.is_reserved
            or getattr(address, 'ipv4_mapped', None) is not None
            or getattr(address, 'sixtofour', None) is not None
            or getattr(address, 'teredo', None) is not None
            or (address.version == 6 and address in ipaddress.ip_network('64:ff9b::/96'))):
        raise SourceRejected('private_address')
    return str(address)


def _validate_url(url) -> _Target:
    if not isinstance(url, str) or not url or len(url) > 2048 or '\\' in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise SourceRejected('invalid_url')
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        host = (parsed.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if not host or '%' in host or port != (443 if parsed.scheme == 'https' else 80):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if ('.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host)
                    or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan', '.test', '.invalid'))
                    or any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.'))):
                raise ValueError()
        else:
            _public_ip(host)
        authority = '[' + host + ']' if ':' in host else host
        path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
        query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
        canonical = urlunsplit((parsed.scheme, authority, path, query, ''))
        return _Target(canonical, host, port, parsed.scheme == 'https', path + ('?' + query if query else ''))
    except SourceRejected:
        raise
    except (ValueError, UnicodeError):
        raise SourceRejected('invalid_url') from None


async def _doh_question(host, record_type):
    """Fixed HTTPS resolver at a public literal IP; no proxy, redirect or cookie jar reuse.

    JSON request schema: https://developers.cloudflare.com/1.1.1.1/encryption/
    dns-over-https/make-api-requests/dns-json/ . TLS validates the 1.1.1.1
    certificate. This endpoint and its trust model are not user-configurable.
    """
    try:
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
                async with client.stream('GET', DOH_ENDPOINT,
                                         params={'name': host, 'type': record_type, 'cd': 'false'},
                                         headers={'Accept': 'application/dns-json', 'Accept-Encoding': 'identity'}) as response:
                    if response.status_code != 200 or response.headers.get('content-encoding', 'identity') != 'identity':
                        raise SourceRejected('dns_failed')
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > 65536:
                            raise SourceRejected('dns_failed')
                        body.extend(chunk)
        payload = json.loads(body)
        expected_type = 1 if record_type == 'A' else 28
        questions = payload.get('Question', [])
        if (payload.get('Status') != 0 or payload.get('TC') is True
                or not isinstance(questions, list) or len(questions) != 1
                or questions[0].get('name', '').rstrip('.').casefold() != host.casefold()
                or questions[0].get('type') != expected_type):
            raise SourceRejected('dns_failed')
        answers = payload.get('Answer', [])
        if not isinstance(answers, list) or len(answers) > 64:
            raise SourceRejected('dns_failed')
        return answers
    except SourceRejected:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError, AttributeError, TypeError):
        raise SourceRejected('dns_failed') from None


async def _resolve_doh_public(host):
    pending, visited, addresses = [host], set(), []
    while pending:
        name = pending.pop(0)
        if name in visited:
            continue
        if len(visited) >= 4:
            raise SourceRejected('dns_failed')
        visited.add(name)
        responses = await asyncio.gather(_doh_question(name, 'A'), _doh_question(name, 'AAAA'), return_exceptions=True)
        for result in responses:
            if isinstance(result, Exception):
                raise SourceRejected(result.code if isinstance(result, SourceRejected) else 'dns_failed') from None
            for row in result:
                if not isinstance(row, dict):
                    raise SourceRejected('dns_failed')
                if row.get('type') in {1, 28}:
                    address = _public_ip(row.get('data', ''))
                    if address not in addresses:
                        addresses.append(address)
                elif row.get('type') == 5:
                    # Resolve both families for every alias rather than trusting
                    # an A-only response that could hide a private AAAA record.
                    alias = row.get('data', '')
                    if not isinstance(alias, str) or any(c in alias for c in '/?#:@'):
                        raise SourceRejected('private_address')
                    alias = _validate_url('https://' + alias.rstrip('.') + '/').host
                    if alias not in visited and alias not in pending:
                        pending.append(alias)
    if not addresses:
        raise SourceRejected('dns_failed')
    return addresses


async def _resolve_public(host, port, dns_mode='auto'):
    try:
        records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise SourceRejected('dns_failed') from None
    if not records:
        raise SourceRejected('dns_failed')
    raw_addresses = []
    for family, _, _, _, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise SourceRejected('private_address')
        raw_addresses.append(sockaddr[0])
    # Fake-IP proxy DNS sometimes returns 198.18.0.0/15 plus the historical
    # IPv4-translated form (::ffff:0:0:0/96, RFC 2765 section 2.1). These fake answers may
    # trigger authenticated public resolution, but must never be connected to.
    # Other translation forms and mixtures with private IPs remain hard failures.
    fake_range = ipaddress.ip_network('198.18.0.0/15')
    translated_range = ipaddress.ip_network('::ffff:0:0:0/96')
    has_fake = False
    for value in raw_addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise SourceRejected('private_address') from None
        is_fake = address.version == 4 and address in fake_range
        is_translated_fake = (
            address.version == 6 and address.scope_id is None
            and address in translated_range
            and ipaddress.IPv4Address(int(address) & 0xffffffff) in fake_range
        )
        if is_fake or is_translated_fake:
            has_fake = True
        else:
            _public_ip(value)
    if has_fake and dns_mode == 'auto':
        return await _resolve_doh_public(host)
    addresses = []
    for value in raw_addresses:
        address = _public_ip(value)
        if address not in addresses:
            addresses.append(address)
    return addresses


async def _connect(target, addresses):
    context = ssl.create_default_context() if target.tls else None
    for address in addresses:
        try:
            # Numeric IP + AI_NUMERICHOST prevents a second hostname DNS lookup.
            # TLS still authenticates the original URL host (SNI + certificate).
            return await asyncio.open_connection(
                host=address, port=target.port, flags=socket.AI_NUMERICHOST,
                family=socket.AF_INET6 if ':' in address else socket.AF_INET,
                ssl=context, server_hostname=target.host if target.tls else None,
                limit=MAX_HEADERS)
        except (OSError, ssl.SSLError):
            continue
    raise SourceRejected('fetch_failed')


async def _line(reader, max_size=8192):
    try:
        line = await reader.readuntil(b'\r\n')
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise SourceRejected('invalid_response') from None
    if len(line) > max_size:
        raise SourceRejected('invalid_response')
    return line


async def _response_head(reader):
    status_line = await _line(reader)
    match = re.fullmatch(rb'HTTP/1\.[01] ([1-5][0-9]{2})[^\r\n]*\r\n', status_line)
    if not match:
        raise SourceRejected('invalid_response')
    headers, size = {}, len(status_line)
    for _ in range(200):
        line = await _line(reader)
        size += len(line)
        if size > MAX_HEADERS:
            raise SourceRejected('invalid_response')
        if line == b'\r\n':
            return int(match.group(1)), headers
        if line.startswith((b' ', b'\t')) or b':' not in line:
            raise SourceRejected('invalid_response')
        name, value = line[:-2].split(b':', 1)
        if not re.fullmatch(rb'[!#$%&\'*+.^_`|~0-9A-Za-z-]+', name):
            raise SourceRejected('invalid_response')
        name = name.decode('ascii').lower()
        if name in headers and name in {'content-length', 'transfer-encoding', 'location', 'content-type', 'content-encoding'}:
            raise SourceRejected('invalid_response')
        headers[name] = value.decode('latin1').strip()
    raise SourceRejected('invalid_response')


async def _body(reader, headers, max_bytes):
    if headers.get('content-encoding', 'identity').lower() != 'identity':
        raise SourceRejected('unsupported_encoding')
    transfer = headers.get('transfer-encoding', '').lower()
    length = headers.get('content-length')
    if transfer and (transfer != 'chunked' or length is not None):
        raise SourceRejected('invalid_response')
    if transfer == 'chunked':
        result = bytearray()
        while True:
            line = (await _line(reader)).split(b';', 1)[0].strip()
            if not re.fullmatch(rb'[0-9a-fA-F]{1,16}', line):
                raise SourceRejected('invalid_response')
            size = int(line, 16)
            if not size:
                return bytes(result)  # Connection is closed; trailers are not used.
            if len(result) + size > max_bytes:
                raise SourceRejected('too_large')
            result.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b'\r\n':
                raise SourceRejected('invalid_response')
    if length is not None:
        if not re.fullmatch(r'[0-9]{1,20}', length):
            raise SourceRejected('invalid_response')
        size = int(length)
        if size > max_bytes:
            raise SourceRejected('too_large')
        return await reader.readexactly(size)
    result = bytearray()
    while True:
        chunk = await reader.read(min(65536, max_bytes - len(result) + 1))
        if not chunk:
            return bytes(result)
        if len(result) + len(chunk) > max_bytes:
            raise SourceRejected('too_large')
        result.extend(chunk)


async def _request_once(target, max_bytes, dns_mode='auto'):
    addresses = await _resolve_public(target.host, target.port, dns_mode)
    reader, writer = await _connect(target, addresses)
    try:
        host = '[' + target.host + ']' if ':' in target.host else target.host
        request = (f'GET {target.path} HTTP/1.1\r\nHost: {host}\r\n'
                   'User-Agent: Lumori-SourceDiscovery/1.0\r\n'
                   'Accept: text/html, application/pdf, text/plain, text/markdown, application/xhtml+xml\r\n'
                   'Accept-Encoding: identity\r\nConnection: close\r\n\r\n')
        writer.write(request.encode('ascii'))
        await writer.drain()
        status, headers = await _response_head(reader)
        if status in {301, 302, 303, 307, 308}:
            return status, headers, b''
        if status != 200:
            raise SourceRejected('source_unavailable')
        content_type = headers.get('content-type', '').split(';')[0].strip().lower()
        if content_type not in ALLOWED_TYPES:
            raise SourceRejected('unsupported_type')
        raw = await _body(reader, headers, max_bytes)
        if content_type == 'application/pdf' and b'%PDF-' not in raw[:1024]:
            raise SourceRejected('unsupported_type')
        return status, headers, raw
    finally:
        # Do not wait for a hostile peer's TLS close-notify beyond the job budget.
        writer.close()


async def fetch_source(settings, url: str) -> dict:
    """Download one public source, revalidating and pinning every redirect hop."""
    target = _validate_url(url)
    original_url = target.url
    try:
        maximum = min(20 * 1024 * 1024, max(1, int(getattr(settings, 'exploration_max_bytes', 10 * 1024 * 1024))))
        timeout = min(60.0, max(0.01, float(getattr(settings, 'exploration_fetch_timeout', 20))))
        dns_mode = getattr(settings, 'exploration_dns_mode', 'auto')
        if dns_mode not in {'auto', 'system'}:
            raise SourceRejected('dns_failed')
        async with asyncio.timeout(timeout):
            visited = set()
            for _ in range(MAX_REDIRECTS + 1):
                if target.url in visited:
                    raise SourceRejected('redirect_limit')
                visited.add(target.url)
                status, headers, raw = await _request_once(target, maximum, dns_mode)
                if status in {301, 302, 303, 307, 308}:
                    if not headers.get('location'):
                        raise SourceRejected('invalid_response')
                    redirected = _validate_url(urljoin(target.url, headers['location']))
                    if target.tls and not redirected.tls:
                        raise SourceRejected('redirect_downgrade')
                    target = redirected
                    continue
                result = dict(url=target.url, original_url=original_url,
                              content_type=headers['content-type'].split(';')[0].strip().lower(), raw=raw)
                # A redirect never transfers an old page's license to a new URL.
                result.update(source_metadata(target.url))
                if result['storage_policy'] == 'unknown' and result['content_type'] in {'text/html', 'application/xhtml+xml'}:
                    result.update(_declared_license(raw, target.url))
                return result
            raise SourceRejected('redirect_limit')
    except SourceRejected:
        raise
    except TimeoutError:
        raise SourceRejected('fetch_timeout') from None
    except (OSError, ValueError, asyncio.IncompleteReadError):
        raise SourceRejected('fetch_failed') from None

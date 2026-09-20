"""Public exploration configuration and provenance; never credentials or model reasoning."""
import json
import math
from urllib.parse import urlsplit

VERSION = 'auto-exploration-v2'


def configuration(settings):
    from .exploration_sources import CATALOG_VERSION, DNS_POLICY_VERSION
    from .exploration_staging import configuration as staging_configuration
    provider = settings.exploration_search_provider
    if provider not in ('curated', 'brave'):
        raise ValueError('自动探索的搜索服务配置无效。')
    if settings.exploration_dns_mode not in ('auto', 'system'):
        raise ValueError('自动探索的域名解析配置无效。')
    limits = {}
    for name, minimum, maximum in (
        ('max_rounds', 1, 3), ('max_documents', 1, 5), ('max_candidates', 1, 20),
        ('max_source_pages', 1, 50), ('max_source_chunks', 1, 100),
        ('max_search_calls', 1, 12), ('max_model_calls', 1, 20),
        ('max_bytes', 1024, 10 * 1024 * 1024),
        ('max_seconds', 10, 600), ('fetch_timeout', 1, 30),
    ):
        value = getattr(settings, 'exploration_' + name)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not minimum <= value <= maximum
                or (name not in ('max_seconds', 'fetch_timeout') and type(value) is not int)):
            raise ValueError('自动探索的资源上限配置无效。')
        limits[name] = value
    return dict(version=VERSION, catalog_version=CATALOG_VERSION, dns_policy=DNS_POLICY_VERSION,
                support_context_policy='verified_anchor_context_v1', support_context_tokens=4096,
                support_context_max_hits=24, support_context_max_anchors=60,
                candidate_retrieval_policy=staging_configuration(),
                evidence_contract='controller_passage_ids_v2',
                coverage_scope_policy='request_minimum_v1',
                partial_source_selection='explicit_any_gap_v2', provider=provider,
                dns_mode=settings.exploration_dns_mode, **limits)


def capabilities(settings):
    try:
        policy = configuration(settings)
    except ValueError:
        return dict(available=False, provider='unavailable', web_search_configured=False)
    web = policy['provider'] == 'brave' and bool(settings.exploration_search_api_key)
    return policy | dict(available=bool(settings.exploration_enabled and
        (policy['provider'] == 'curated' or web)), web_search_configured=web)


def require_available(settings):
    if not capabilities(settings)['available']:
        raise ValueError('自动探索暂不可用，请检查搜索服务配置，或关闭自动探索后使用已有资料。')


def document_source(store, document_id):
    row = store.one('SELECT metadata FROM external_document_sources WHERE document_id=?', (document_id,))
    if not row:
        return None
    try:
        metadata = json.loads(row['metadata'])
    except (ValueError, TypeError):
        return None
    if not isinstance(metadata, dict):
        return None
    # Keep the public document view small; full bounded audit belongs to the task.
    result = {}
    for name in ('url', 'original_url', 'reading_url', 'title', 'provider', 'acquired_at',
                 'storage_policy', 'license', 'license_url', 'attribution',
                 'license_verification', 'source_format'):
        value = metadata.get(name)
        if not isinstance(value, str):
            continue
        if name in ('url', 'original_url', 'reading_url', 'license_url'):
            try:
                parts = urlsplit(value)
                if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
                    continue
            except ValueError:
                continue
        result[name] = value[:2000]
    return result or None

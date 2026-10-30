"""Source grounding, account isolation and read paths without implicit paid I/O."""
import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import agent, core, providers
from app.services import storage_guidance as service


def food(signed):
    return signed.post('/api/v1/foods', json={'name': '豆腐', 'storage': 'refrigerated',
        'food_class': 'fresh', 'condition': 'raw', 'date_type': 'purchase',
        'started_at': (core.now()-timedelta(hours=1)).isoformat()}).json()


def upstream(monkeypatch, summary=None, sources=None):
    calls = []
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    def request(method, url, **kwargs):
        calls.append((url, kwargs['json']))
        if url.endswith('/search'):
            return {'results': sources if sources is not None else [
                {'title': '豆腐储存参考', 'url': 'https://www.gov.cn/test-food-guidance',
                 'content': '豆腐应按包装要求妥善冷藏，开封后应尽快使用。' * 12}]}
        return {'choices': [{'message': {'content': json.dumps(summary if summary is not None else {
            'summary': '请先核对包装、开封及实际冷藏条件，按包装说明安排使用。',
            'conditions_to_check': ['是否已开封', '是否持续按要求冷藏'], 'source_ids': [1]}, ensure_ascii=False)}}]}
    monkeypatch.setattr(providers, 'request_json', request)
    return calls


def test_inventory_and_cache_reads_never_search_implicitly(signed, monkeypatch):
    row = food(signed)
    monkeypatch.setattr(providers, 'request_json', lambda *a, **k: pytest.fail('read triggered network'))
    assert signed.get('/api/v1/foods').status_code == 200
    data = signed.get(f"/api/v1/foods/{row['id']}/storage-guidance").json()
    assert data['status'] == 'not_requested' and data['guidance'] is None


def test_search_is_two_calls_source_grounded_cached_and_never_changes_food(signed, monkeypatch):
    row = food(signed); calls = upstream(monkeypatch)
    url = f"/api/v1/foods/{row['id']}/storage-guidance"
    response = signed.post(url)
    assert response.status_code == 200
    guidance = response.json()['guidance']
    assert guidance['status'] == 'ready' and not guidance['formal_deadline_changed']
    assert 'content' not in guidance['sources'][0]
    assert len(calls) == 2
    assert signed.post(url).json()['guidance']['cached'] and len(calls) == 2
    assert signed.get('/api/v1/foods').json()['items'][0]['expires_at'] == row['expires_at']
    with core.DB() as db:
        assert len(list(db.scalars(select(core.Job).where(core.Job.kind == 'reminder')))) == 0
        assert len(list(db.scalars(select(core.Record).where(core.Record.kind == 'usage')))) == 2


@pytest.mark.parametrize('source', [
    {'url': 'https://example.org/blog', 'content': '未经核实的博客正文' * 40},
    {'url': 'https://foodsafety.gov.evil.org/trap', 'content': '假域名' * 100},
    {'url': 'http://127.0.0.1/admin', 'content': '本机地址' * 100},
    {'url': 'https://www.gov.cn/short', 'snippet': '仅有短摘要，不是完整正文'},
])
def test_untrusted_or_snippet_only_result_does_not_call_model(signed, monkeypatch, source):
    calls = upstream(monkeypatch, sources=[source])
    response = signed.post('/api/v1/foods/storage-guidance', json={'name': '豆腐'})
    assert response.status_code == 200 and response.json()['status'] == 'no_reliable_sources'
    assert len(calls) == 1


@pytest.mark.parametrize('change', [
    {'source_ids': [99]}, {'summary': '可以保存999天'},
    {'expires_at': '2099-01-01'}, {'summary': 'see https://evil.org'},
])
def test_forged_citations_numbers_dates_and_links_fail_without_retry(signed, monkeypatch, change):
    summary = {'summary': '请核对实际冷藏条件。', 'conditions_to_check': [], 'source_ids': [1], **change}
    calls = upstream(monkeypatch, summary=summary)
    response = signed.post('/api/v1/foods/storage-guidance', json={'name': '豆腐'})
    assert response.status_code == 502 and response.json()['error']['code'] == 'UNVERIFIED_STORAGE_ADVICE'
    assert len(calls) == 2


def test_conditions_changed_invalidate_cached_advice(signed, monkeypatch):
    row = food(signed); calls = upstream(monkeypatch)
    path = f"/api/v1/foods/{row['id']}/storage-guidance"
    assert signed.post(path).status_code == 200
    with core.DB() as db:
        r = db.get(core.Record, row['id']); r.payload = {**r.payload, 'storage': 'frozen'}; db.commit()
    assert signed.get(path).json()['guidance'] is None
    assert signed.post(path).status_code == 200 and len(calls) == 4


def test_cross_account_access_fails_before_upstream(signed, monkeypatch):
    row = food(signed); calls = upstream(monkeypatch)
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'otheruser', 'password': 'correct-horse-456'})
    for method in (signed.get, signed.post):
        assert method(f"/api/v1/foods/{row['id']}/storage-guidance").status_code == 404
    assert calls == []


def test_agent_and_http_share_the_same_guidance_service(signed, monkeypatch):
    row = food(signed); calls = upstream(monkeypatch)
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    result, card = agent._execute_tool('get_food_storage_advice', {'food_id': row['id']}, owner, {})
    assert result['guidance']['status'] == 'ready' and card['type'] == 'food_storage_advice'
    assert signed.get(f"/api/v1/foods/{row['id']}/storage-guidance").json()['guidance']['cached']
    assert len(calls) == 2


def test_bad_context_no_configuration_and_disabled_service_never_search(signed, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, 'request_json', lambda *a, **k: calls.append(a))
    assert signed.post('/api/v1/foods/storage-guidance', json={'name': '豆腐', 'storage': 'fiction'}).status_code == 422
    assert signed.post('/api/v1/foods/storage-guidance', json={'name': '豆腐'}).status_code == 503
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    assert signed.post('/api/v1/foods/storage-guidance', json={'name': '豆腐'}).status_code == 503
    assert calls == []


def test_search_query_omits_private_metadata_and_cannot_add_operators():
    facts = service.GuidanceInput(name='豆腐 https://evil.org site:evil.org').facts()
    query = service.search_query(facts)
    assert len(query) <= 128 and 'site:evil' not in query and 'https://' not in query
    assert 'started_at' not in query and 'owner' not in query

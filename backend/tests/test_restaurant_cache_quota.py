"""Exact-page reuse saves paid attempts; quotas remain real and account scoped."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app import core, providers
from app.services import restaurant_service, restaurant_scope
from backend.tests.amap_fixtures import district_response


@pytest.fixture
def upstream(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(core.settings, 'amap_api_key', 'synthetic-key')
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    calls = []

    def request(method, url, **kwargs):
        if url.endswith('/config/district'):
            return district_response(kwargs['params']['keywords'])
        calls.append(dict(kwargs['params']))
        return {'status': '1', 'count': '1', 'pois': [
            {'id': 'synthetic-poi', 'name': '测试素食馆', 'typecode': '050120', 'adcode': kwargs['params'].get('city'),
             'type': '餐饮服务;中餐厅;中式素菜馆', 'location': '116.41,39.91'},
            {'id': 'excluded', 'name': '烤鸭店', 'typecode': '050120', 'type': '餐饮服务;中餐厅'}]}

    monkeypatch.setattr(providers, 'request_json', request)
    return owner, calls, request


def usage_count():
    with core.DB() as db:
        return len(db.scalars(select(core.Record).where(core.Record.kind == 'usage')).all())


def test_same_successful_page_is_reused_without_reservation_even_when_quota_is_full(signed, upstream, monkeypatch):
    owner, calls, _ = upstream
    monkeypatch.setattr(core.settings, 'global_daily_external_limit', 2)
    first = restaurant_service.search(owner, city='北京朝阳')
    second = restaurant_service.search(owner, city='北京朝阳')
    assert first['cache']['hit'] is False and second['cache']['hit'] is True
    assert second['query'] == first['query']
    assert [p['id'] for p in second['items']] == ['synthetic-poi']
    assert len(calls) == 1 and usage_count() == 2  # district lookup + one POI page
    with pytest.raises(core.AppError) as error:
        restaurant_service.search(owner, city='上海')
    assert error.value.code == 'GLOBAL_QUOTA_EXCEEDED'
    assert len(calls) == 1 and usage_count() == 2
    # Client mutations cannot change the persisted result.
    second['items'].clear()
    assert restaurant_service.search(owner, city='北京朝阳')['items']


def test_cache_expires_and_rechecks_scope_version_and_provider_key(upstream, monkeypatch):
    owner, calls, _ = upstream
    first = restaurant_service.search(owner, city='北京')
    clock = core.now()
    monkeypatch.setattr(restaurant_service, 'now', lambda: clock + timedelta(minutes=11))
    expired = restaurant_service.search(owner, city='北京')
    assert expired['cache']['hit'] is False
    assert expired['cache']['fetched_at'] != first['cache']['fetched_at']
    monkeypatch.setattr(restaurant_scope, 'RULE_VERSION', 'synthetic-new-scope')
    assert restaurant_service.search(owner, city='北京')['cache']['hit'] is False
    monkeypatch.setattr(core.settings, 'amap_api_key', 'synthetic-new-key')
    assert restaurant_service.search(owner, city='北京')['cache']['hit'] is False
    assert len(calls) == usage_count() == 4


def test_cache_never_crosses_accounts_centers_radii_pages_sizes_or_keywords(signed, upstream):
    owner, calls, _ = upstream
    base = {'location': '116.4,39.9', 'radius_m': 10000}
    restaurant_service.search(owner, **base)
    for change in ({'location': '116.41,39.9'}, {'radius_m': 20000}, {'page': 2},
                   {'page_size': 5}, {'keywords': '素食'}):
        assert restaurant_service.search(owner, **{**base, **change})['cache']['hit'] is False
    signed.post('/api/v1/auth/logout')
    assert signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'}).status_code == 201
    with core.DB() as db:
        other = db.scalar(select(core.User).where(core.User.username == 'bob')).id
    assert restaurant_service.search(other, **base)['cache']['hit'] is False
    assert len(calls) == usage_count() == 7


def test_failure_is_not_cached_or_automatically_retried_and_attempt_stays_counted(upstream, monkeypatch):
    owner, calls, request = upstream
    def fail(*args, **kwargs):
        calls.append('failed')
        raise core.AppError('UPSTREAM_FAILED', 'synthetic failure', 502)
    monkeypatch.setattr(providers, 'request_json', fail)
    with pytest.raises(core.AppError):
        restaurant_service.search(owner, city='北京')
    assert len(calls) == usage_count() == 1
    with core.DB() as db:
        assert not db.scalars(select(core.Record).where(core.Record.kind == 'restaurant_cache')).all()
    monkeypatch.setattr(providers, 'request_json', request)
    assert restaurant_service.search(owner, city='北京')['cache']['hit'] is False
    assert len(calls) == usage_count() == 2


def test_empty_filtered_page_is_cacheable_without_changing_pagination(upstream, monkeypatch):
    owner, calls, request = upstream
    def empty(*args, **kwargs):
        result = request(*args, **kwargs)
        result['pois'] = result['pois'][1:]
        result['count'] = '50'
        return result
    monkeypatch.setattr(providers, 'request_json', empty)
    first = restaurant_service.search(owner, city='北京')
    again = restaurant_service.search(owner, city='北京')
    assert first['items'] == again['items'] == []
    assert first['pagination'] == again['pagination']
    assert again['pagination']['has_more'] is True
    assert again['cache']['hit'] is True and len(calls) == 1


def test_simultaneous_same_query_reserves_one_call(upstream, monkeypatch):
    owner, calls, request = upstream
    entered = threading.Event()
    release = threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return request(*args, **kwargs)
    monkeypatch.setattr(providers, 'request_json', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(restaurant_service.search, owner, city='北京')
        assert entered.wait(5)
        second = pool.submit(restaurant_service.search, owner, city='北京')
        release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert sorted(result['cache']['hit'] for result in results) == [False, True]
    assert len(calls) == usage_count() == 1


@pytest.mark.parametrize('global_scope,weekly', [(True, False), (True, True), (False, False), (False, True)])
def test_quota_message_identifies_scope_period_and_actual_reset(signed, monkeypatch, global_scope, weekly):
    instant = datetime(2026, 10, 29, 18, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
    monkeypatch.setattr(providers, 'now', lambda: instant)
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'synthetic')
    for field in ('daily_external_limit', 'weekly_external_limit', 'global_daily_external_limit', 'global_weekly_external_limit'):
        monkeypatch.setattr(core.settings, field, 100)
    field = ('global_' if global_scope else '') + ('weekly_external_limit' if weekly else 'daily_external_limit')
    monkeypatch.setattr(core.settings, field, 1)
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
        db.add(core.Record(owner=owner, kind='usage', created=instant - timedelta(minutes=1), payload={'operation': 'chat'}))
        db.commit()
    with pytest.raises(core.AppError) as error:
        providers.reserve_call(owner, 'chat')
    assert error.value.code == ('GLOBAL_QUOTA_EXCEEDED' if global_scope else 'QUOTA_EXCEEDED')
    message = error.value.message
    assert ('全项目' if global_scope else '本账号') in message
    assert ('本周' if weekly else '今日') in message
    assert ('11月02日 00:00' if weekly else '10月30日 00:00') in message
    assert usage_count() == 1

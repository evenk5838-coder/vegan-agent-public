"""Administrative restriction, complementary discovery and sourced diet metadata."""
from datetime import datetime, timezone
from pathlib import Path
import copy
import json

import pytest
from sqlalchemy import select

from app import core, providers
from app.services import restaurant_area, restaurant_diets, restaurant_service
from backend.tests.amap_fixtures import district_response


@pytest.fixture
def live(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_api_key', 'synthetic')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    calls = []
    def request(method, url, **kwargs):
        params = dict(kwargs['params'])
        calls.append((url, params))
        if url.endswith('/config/district'):
            return district_response(params['keywords'])
        return {'status': '1', 'count': '40', 'pois': [
            {'id': 'inside', 'name': '测试蔬食馆', 'adcode': '110105', 'typecode': '050200', 'type': '餐饮服务;西餐厅'},
            {'id': 'outside', 'name': '另一家素食馆', 'adcode': '110108', 'typecode': '050120'},
            {'id': 'missing', 'name': '无行政区素食馆', 'typecode': '050120'},
            {'id': 'meat', 'name': '烤鸭店', 'adcode': '110105', 'typecode': '050120'}]}
    monkeypatch.setattr(providers, 'request_json', request)
    return owner, calls


def usage():
    with core.DB() as db:
        return len(db.scalars(select(core.Record).where(core.Record.kind == 'usage')).all())


def test_district_is_not_keyword_and_poi_must_match_its_adcode(signed, live):
    owner, calls = live
    data = restaurant_service.search(owner, city='北京市朝阳区')
    assert len(calls) == usage() == 2
    assert calls[-1][1]['city'] == '110105' and calls[-1][1]['keywords'] == '素食'
    assert calls[-1][1]['types'] == '050000'
    assert [x['id'] for x in data['items']] == ['inside']
    assert data['pagination']['raw_returned'] == 4
    assert data['pagination']['filtered_out'] == 3
    assert data['pagination']['has_more'] is True
    assert data['pagination']['reject_reasons']['outside_or_unknown_district'] == 2
    assert data['query']['city'] == '北京市朝阳区'
    # Normal page 2 keeps exactly the same administrative area, without another resolver call.
    second = restaurant_service.search(owner, city=data['query']['city'], page=2)
    assert len(calls) == usage() == 3
    assert calls[-1][1]['city'] == '110105' and calls[-1][1]['page'] == 2
    assert second['query']['area_resolution'] == data['query']['area_resolution']


@pytest.mark.parametrize('term', ['素食', '蔬食', '素斋', '植物料理', 'vegan', 'vegetarian'])
def test_identity_keyword_widens_to_all_dining_but_never_admits_meat(live, term):
    owner, calls = live
    data = restaurant_service.search(owner, city='北京', keywords=term)
    assert len(calls) == usage() == 1
    assert calls[0][1]['types'] == '050000' and calls[0][1]['keywords'] == term
    assert data['query']['search_strategy'] == 'identity_keyword'
    assert all(x['id'] != 'meat' for x in data['items'])
    assert set(data['items'][0]['diet_labels']['labels']) == {'pure', 'vegan', 'lacto_ovo'}
    assert all(x['status'] == 'unknown' for x in data['items'][0]['diet_labels']['labels'].values())


def test_ambiguous_district_does_not_issue_a_poi_query(live, monkeypatch):
    owner, calls = live
    def ambiguous(method, url, **kwargs):
        calls.append((url, kwargs['params']))
        return {'status': '1', 'districts': [
            {'name': '朝阳区', 'level': 'district', 'adcode': '110105'},
            {'name': '朝阳区', 'level': 'district', 'adcode': '220104'}]}
    monkeypatch.setattr(providers, 'request_json', ambiguous)
    with pytest.raises(core.AppError) as error:
        restaurant_service.search(owner, city='朝阳区')
    assert error.value.code == 'AREA_AMBIGUOUS'
    assert len(calls) == usage() == 1 and calls[0][0].endswith('/config/district')


def test_region_failure_is_not_an_empty_result_or_automatic_retry(live, monkeypatch):
    owner, calls = live
    def failure(method, url, **kwargs):
        calls.append((url, kwargs['params']))
        return {'status': '0', 'info': 'ERROR'}
    monkeypatch.setattr(providers, 'request_json', failure)
    with pytest.raises(core.AppError) as error:
        restaurant_service.search(owner, city='北京朝阳')
    assert error.value.code == 'AREA_UNAVAILABLE' and len(calls) == usage() == 1


def test_source_labels_are_independent_and_branch_bound(monkeypatch):
    monkeypatch.setattr(restaurant_diets, 'now', lambda: datetime(2026, 10, 30, tzinfo=timezone.utc))
    row = {'id': 'B0I0Y7WWTD', 'name': '兰斋Lamdre植物料理餐厅'}
    labels = restaurant_diets.classify(row)['labels']
    assert labels['vegan']['status'] == labels['lacto_ovo']['status'] == 'supported'
    assert labels['pure']['status'] == 'unknown'
    assert labels['vegan']['sources'][0]['source'].startswith('https://guide.michelin.com/')
    for altered in ({'id': 'other'}, {'name': '兰斋其他分店'}, {'id': 'other', 'name': '全素餐厅'}):
        other = restaurant_diets.classify({**row, **altered})['labels']
        assert all(v['status'] == 'unknown' for v in other.values())
    king = restaurant_diets.classify({'name': '京兆尹', 'address': '北京市五道营胡同2号'})['labels']
    assert king['vegan']['status'] == 'on_request' and king['pure']['status'] == 'unknown'


def test_expired_source_never_keeps_a_supported_label(monkeypatch):
    monkeypatch.setattr(restaurant_diets, 'now', lambda: datetime(2026, 11, 29, tzinfo=timezone.utc))
    result = restaurant_diets.classify({'name': '兰斋', 'id': 'B0I0Y7WWTD'})
    assert all(v['status'] == 'unknown' and not v['sources'] for v in result['labels'].values())


def test_bad_parameters_stop_before_any_resolver_or_poi_call(live):
    owner, calls = live
    with pytest.raises(core.AppError):
        restaurant_service.search(owner, city='北京朝阳', page=0)
    with pytest.raises(core.AppError):
        restaurant_service.search(owner, city='北京朝阳', keywords='字' * 81)
    assert not calls and usage() == 0


def test_page_cache_does_not_extend_expired_menu_evidence(live, monkeypatch):
    owner, calls = live
    monkeypatch.setattr(restaurant_diets, 'now', lambda: datetime(2026, 11, 28, tzinfo=timezone.utc))
    def lamdre(method, url, **kwargs):
        calls.append((url, kwargs['params']))
        return {'status': '1', 'count': '1', 'pois': [
            {'id': 'B0I0Y7WWTD', 'name': '兰斋Lamdre植物料理餐厅', 'typecode': '050200'}]}
    monkeypatch.setattr(providers, 'request_json', lamdre)
    first = restaurant_service.search(owner, city='北京')
    assert first['items'][0]['diet_labels']['labels']['vegan']['status'] == 'supported'
    monkeypatch.setattr(restaurant_diets, 'now', lambda: datetime(2026, 11, 29, tzinfo=timezone.utc))
    cached = restaurant_service.search(owner, city='北京')
    assert cached['cache']['hit'] is True and len(calls) == usage() == 1
    assert cached['items'][0]['diet_labels']['labels']['vegan']['status'] == 'unknown'


def beijing_real_response():
    return json.loads((Path(__file__).parent/'fixtures/amap-beijing-district-20261030.json').read_text())


@pytest.mark.parametrize('area', ['北京朝阳', '北京市朝阳区', '北京 朝阳区'])
def test_actual_beijing_administrative_shape_joins_explicit_city_link(live, monkeypatch, area):
    owner, calls = live
    response = beijing_real_response()
    def request(method, url, **kwargs):
        calls.append((url, kwargs['params']))
        assert url.endswith('/config/district')
        return copy.deepcopy(response)
    monkeypatch.setattr(providers, 'request_json', request)
    base, district = restaurant_service.split_area(area)
    result = restaurant_area.resolve(owner, area, base, district)
    assert result['adcode'] == '110105' and result['level'] == 'district'
    assert len(calls) == usage() == 1


def test_actual_shape_does_not_scan_unrelated_same_named_district(live, monkeypatch):
    owner, _ = live
    response = beijing_real_response()
    response['districts'].append({'name':'长春市','adcode':'220100','level':'city','districts':[
        {'name':'朝阳区','adcode':'220104','level':'district','districts':[]}]})
    monkeypatch.setattr(providers, 'request_json', lambda *a, **k: copy.deepcopy(response))
    result = restaurant_area.resolve(owner, '北京朝阳', '北京', '朝阳')
    assert result['adcode'] == '110105'


def test_actual_response_flows_through_http_poi_filter_and_page_cache(signed, live, monkeypatch):
    owner, _ = live
    response = beijing_real_response()
    seen = []
    def request(method, url, **kwargs):
        seen.append((url,kwargs['params']))
        if url.endswith('/config/district'):
            return copy.deepcopy(response)
        assert kwargs['params']['city'] == '110105'
        return {'status':'1','count':'1','pois':[
            {'id':'real-shape-synthetic-poi','name':'测试素食馆','adcode':'110105','typecode':'050120'}]}
    monkeypatch.setattr(providers, 'request_json', request)
    first = signed.get('/api/v1/restaurants',params={'city':'北京市朝阳区'})
    assert first.status_code == 200, first.json()
    assert first.json()['items'][0]['id'] == 'real-shape-synthetic-poi'
    second = signed.get('/api/v1/restaurants',params={'city':'北京市朝阳区'})
    assert second.json()['cache']['hit'] is True and len(seen) == usage() == 2


@pytest.mark.parametrize('response', [[], None, {'status':'1','districts':None}])
def test_malformed_admin_body_is_a_controlled_failure(live, monkeypatch, response):
    owner, _ = live
    monkeypatch.setattr(providers, 'request_json', lambda *a, **k: response)
    with pytest.raises(core.AppError) as error:
        restaurant_area.resolve(owner,'北京朝阳','北京','朝阳')
    assert error.value.code == 'AREA_UNAVAILABLE'


def actual_restaurant_samples():
    return json.loads((Path(__file__).parent/'fixtures/amap-restaurant-samples-20261030.json').read_text())


def test_default_region_http_and_agent_use_the_real_verified_eight_poi_channel(signed, live, monkeypatch):
    from app import agent
    owner, _ = live
    samples = actual_restaurant_samples()
    seen = []
    def request(method, url, **kwargs):
        seen.append((url,kwargs['params']))
        if url.endswith('/config/district'):
            return beijing_real_response()
        params = kwargs['params']
        assert params['city'] == '110105' and params['keywords'] == '素食' and params['types'] == '050000'
        assert params['page'] == 1 and params['offset'] == 20
        return copy.deepcopy(samples['identity-vegetarian'])
    monkeypatch.setattr(providers, 'request_json', request)
    result = signed.get('/api/v1/restaurants',params={'city':'北京市朝阳区'})
    assert result.status_code == 200, result.json()
    data = result.json()
    assert len(data['items']) == 8 and data['pagination']['raw_returned'] == 20
    assert data['pagination']['has_more'] is True and data['query']['keywords'] == '素食'
    assert any(item['typecode'] != '050120' for item in data['items'])
    payload, card = agent._execute_tool('search_restaurants', {'city':'北京市朝阳区'}, owner, {})
    explicit = signed.get('/api/v1/restaurants',params={'city':'北京市朝阳区','keywords':'素食'}).json()
    assert payload['cache']['hit'] and explicit['cache']['hit'] and len(seen) == usage() == 2
    assert payload['items'] == card['payload']['items'] == explicit['items'] == data['items']


def test_nearby_default_keeps_the_real_verified_seven_poi_channel(signed, live, monkeypatch):
    owner, _ = live
    sample = actual_restaurant_samples()['around-public']
    seen = []
    def request(method, url, **kwargs):
        seen.append(url)
        assert url.endswith('/place/around')
        assert kwargs['params']['types'] == '050120' and 'keywords' not in kwargs['params']
        return copy.deepcopy(sample)
    monkeypatch.setattr(providers, 'request_json', request)
    result = signed.get('/api/v1/restaurants',params={'location':'116.4602,39.9219','radius_m':10000}).json()
    assert len(result['items']) == 7 and result['query']['keywords'] is None
    assert len(seen) == usage() == 1

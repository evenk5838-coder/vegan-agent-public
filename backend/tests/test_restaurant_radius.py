"""Map-view radius changes remain bounded, single-page and metered after validation."""
import pytest
from sqlalchemy import select

from app import agent, core, providers
from app.services import restaurant_service


def enable(monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_api_key', 'synthetic-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def poi(index):
    return {'id': f'test-{index}', 'name': f'测试素食餐厅{index}', 'address': '测试路',
            'location': '116.41,39.91', 'typecode': '050120',
            'type': '餐饮服务;中餐厅;中式素菜馆', 'distance': '400'}


def fake_amap(monkeypatch):
    calls = []
    def request(_method, url, **kwargs):
        params = kwargs['params']; calls.append({'url': url, 'params': dict(params)})
        page = params['page']
        items = [poi(index) for index in (range(20) if page == 1 else range(20, 23))]
        return {'status': '1', 'pois': items, 'count': '23'}
    monkeypatch.setattr(providers, 'request_json', request)
    return calls


@pytest.mark.parametrize('radius', [250, 4500, 50000])
def test_http_radius_is_forwarded_and_echoed_without_changing_scope_or_window(signed, monkeypatch, radius):
    enable(monkeypatch); calls = fake_amap(monkeypatch)
    response = signed.get('/api/v1/restaurants', params={'location': '116.4,39.9', 'radius_m': radius})
    assert response.status_code == 200
    data = response.json()
    assert calls[0]['params']['radius'] == radius
    assert data['query']['radius_m'] == radius
    assert data['query']['mode'] == 'around' and data['query']['sortrule'] == 'distance'
    assert data['scope']['version'] == 'veg-admission-v3'
    assert data['pagination']['window']['max_records'] == 200
    assert len(calls) == 1


def test_next_page_keeps_the_exact_center_radius_and_keywords(signed, monkeypatch):
    enable(monkeypatch); calls = fake_amap(monkeypatch)
    params = {'location': '116.4,39.9', 'radius_m': 8200, 'keywords': '素食', 'page_size': 20}
    first = signed.get('/api/v1/restaurants', params=params).json()
    second = signed.get('/api/v1/restaurants', params={**params, 'page': 2}).json()
    assert first['query']['radius_m'] == second['query']['radius_m'] == 8200
    assert {key: value for key, value in first['query'].items() if key not in ('at', 'page')} == {
        key: value for key, value in second['query'].items() if key not in ('at', 'page')}
    assert {key: value for key, value in calls[0]['params'].items() if key != 'page'} == {
        key: value for key, value in calls[1]['params'].items() if key != 'page'}
    assert second['pagination']['has_more'] is False
    assert len(calls) == 2


@pytest.mark.parametrize('radius', [True, False, 249, 50001, -1, 1.5, 3000.0, '1.5', '', 'nan', [], {}])
def test_invalid_service_radius_is_rejected_before_reserving_or_contacting_provider(monkeypatch, radius):
    monkeypatch.setattr(providers, 'reserve_call', lambda *_args: pytest.fail('must validate before reserve'))
    monkeypatch.setattr(providers, 'search_restaurants', lambda *_args, **_kwargs: pytest.fail('must not call provider'))
    with pytest.raises(core.AppError) as error:
        restaurant_service.search('synthetic-owner', location='116.4,39.9', radius_m=radius)
    assert error.value.code == 'INVALID_RADIUS' and error.value.status == 422


@pytest.mark.parametrize('radius', [250, 3000, 50000])
def test_region_query_cannot_silently_accept_a_radius(signed, monkeypatch, radius):
    monkeypatch.setattr(providers, 'reserve_call', lambda *_args: pytest.fail('must validate before reserve'))
    response = signed.get('/api/v1/restaurants', params={'city': '北京朝阳', 'radius_m': radius})
    assert response.status_code == 422 and response.json()['error']['code'] == 'INVALID_RADIUS'


@pytest.mark.parametrize('radius', ['true', '249', '50001', '3000.5'])
def test_http_invalid_radius_never_reserves_a_call(signed, monkeypatch, radius):
    monkeypatch.setattr(providers, 'reserve_call', lambda *_args: pytest.fail('must validate before reserve'))
    assert signed.get('/api/v1/restaurants', params={'location': '116.4,39.9', 'radius_m': radius}).status_code == 422


def test_invalid_coordinate_cannot_start_a_custom_radius_query(monkeypatch):
    monkeypatch.setattr(providers, 'reserve_call', lambda *_args: pytest.fail('invalid center cannot reserve'))
    with pytest.raises(core.AppError) as error:
        restaurant_service.search('synthetic-owner', location='181,39.9', radius_m=5000)
    assert error.value.code == 'INVALID_LOCATION'


def test_default_radius_preserves_the_old_adapter_signature_for_http_and_agent(signed, monkeypatch):
    enable(monkeypatch)
    calls = []
    # Intentionally has no radius_m kwarg: default callers must stay compatible.
    def old_adapter(city, location, page=1, page_size=20, keywords=None):
        calls.append((city, location, page, page_size, keywords))
        return {'query': {'mode': 'around', 'location': location, 'city': None,
                         'radius_m': 3000, 'coordinate_system': 'GCJ-02', 'page': page, 'page_size': page_size},
                'items': [poi(1)], 'raw_returned': 1, 'provider_count': '1',
                'source': 'synthetic-only', 'notice': 'synthetic-only'}
    monkeypatch.setattr(providers, 'search_restaurants', old_adapter)
    first = signed.get('/api/v1/restaurants', params={'location': '116.4,39.9'}).json()
    explicit_default = signed.get('/api/v1/restaurants', params={'location': '116.4,39.9', 'radius_m': 3000}).json()
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    result, card = agent._execute_tool('search_restaurants', {'location': '116.4,39.9', 'radius_m': 3000}, owner, {})
    assert first['query']['radius_m'] == explicit_default['query']['radius_m'] == result['query']['radius_m'] == 3000
    assert card['payload']['query'] == result['query']
    assert len(calls) == 1  # HTTP/default spelling and Agent share the exact successful page.
    assert first['cache']['hit'] is False
    assert explicit_default['cache']['hit'] is result['cache']['hit'] is True

"""SDK → authenticated proxy → JSON/JSONP regressions; no real provider calls."""
import json

import pytest

from app import core, providers


@pytest.fixture
def map_upstream(monkeypatch):
    calls, usage = [], []
    monkeypatch.setattr(providers, 'map_js_ready', lambda: True)
    monkeypatch.setattr(core.settings, 'amap_js_key', 'server-owned-js-key')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 'server-owned-jscode')
    monkeypatch.setattr(providers, 'reserve_call', lambda owner, operation: usage.append(operation))

    def respond(method, url, **kwargs):
        calls.append({'method': method, 'url': url, 'params': kwargs['params']})
        return {'status': '1', 'locations': '116.403372,39.917520'}

    monkeypatch.setattr(providers, 'request_json', respond)
    return calls, usage


def sdk_params():
    return {'locations': '116.397128,39.916527', 'coordsys': 'gps',
            'callback': 'jsonp_amap_12345', 'platform': 'JS', 's': 'rsv3',
            'sdkversion': '2.3.5.6', 'key': 'untrusted-client-key',
            'jscode': 'untrusted-client-secret', 'url': 'https://evil.invalid'}


def test_real_sdk_convert_path_returns_executable_callback(signed, map_upstream):
    response = signed.get('/api/v1/map/proxy/_AMapService/v3/assistant/coordinate/convert',
                          params=sdk_params())
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/javascript')
    assert response.headers['cache-control'] == 'no-store'
    body = response.text
    assert body.startswith('jsonp_amap_12345(') and body.endswith(');')
    assert json.loads(body[len('jsonp_amap_12345('):-2])['status'] == '1'
    calls, usage = map_upstream
    assert len(calls) == 1 and usage == ['map_js']
    assert calls[0]['url'] == 'https://restapi.amap.com/v3/assistant/coordinate/convert'
    params = calls[0]['params']
    assert params['locations'] == sdk_params()['locations'] and params['coordsys'] == 'gps'
    assert params['platform'] == 'JS' and params['output'] == 'JSON'
    assert params['key'] == 'server-owned-js-key' and params['jscode'] == 'server-owned-jscode'
    assert not {'url', 'callback'} & params.keys()
    assert 'server-owned' not in body and 'untrusted-client' not in body


def test_conversion_without_callback_remains_json(signed, map_upstream):
    response = signed.get('/api/v1/map/proxy/v3/assistant/coordinate/convert',
                          params={'locations': '116.4,39.9', 'coordsys': 'gps'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/json')
    assert response.json()['status'] == '1'
    assert len(map_upstream[0]) == 1


def test_jsonp_injection_and_bad_coordinates_never_call_upstream(signed, map_upstream):
    for changed in ({'callback': 'x);alert(1)//'}, {'locations': '181,91'}, {'coordsys': 'bad'}):
        response = signed.get('/api/v1/map/proxy/_AMapService/v3/assistant/coordinate/convert',
                              params={**sdk_params(), **changed})
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_MAP_REQUEST'
    assert map_upstream == ([], [])


def test_sdk_proxy_still_requires_login(client, map_upstream):
    response = client.get('/api/v1/map/proxy/_AMapService/v3/assistant/coordinate/convert',
                          params=sdk_params())
    assert response.status_code == 401
    assert map_upstream == ([], [])

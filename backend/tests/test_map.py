"""地图字段与 map/config 工程验证；外部请求全部以假响应替换，不发真实调用。"""
from app import core, providers


def fake_amap(monkeypatch, pois=None, captured=None):
    pois = pois if pois is not None else [
        {'id': 'B001', 'name': '素心素食餐厅', 'address': '静安区某路1号', 'location': '121.4523,31.2286',
         'distance': '850', 'typecode': '050120', 'type': '餐饮服务;中餐厅;中式素菜馆',
         'biz_ext': {'rating': '4.5', 'cost': '68', 'open_time': '10:00-21:00'}},
        {'id': 'B002', 'name': '无坐标素食餐厅', 'address': '某区某路2号', 'location': [],
         'typecode': '050120', 'type': '餐饮服务;中餐厅;中式素菜馆', 'biz_ext': {}},
    ]

    def fake_request_json(method, url, **kwargs):
        if captured is not None:
            captured['url'] = url
            captured['params'] = kwargs.get('params')
        return {'status': '1', 'pois': pois}

    monkeypatch.setattr(providers, 'request_json', fake_request_json)
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def test_around_query_returns_distance_and_query_block(signed, monkeypatch):
    fake_amap(monkeypatch)
    data = signed.get('/api/v1/restaurants?location=121.45,31.23').json()
    assert data['query']['mode'] == 'around'
    assert data['query']['coordinate_system'] == 'GCJ-02'
    assert data['query']['city'] is None
    first, second = data['items']
    assert first['distance_m'] == 850
    assert first['distance_type'] == 'straight_line'
    assert first['business_hours'] == '10:00-21:00'
    assert first['rating'] == '4.5'
    assert first['hours'] is None  # 旧字段保留
    # 供应商缺距离时可为空，不编造
    assert second['distance_m'] is None
    assert second['location'] is None  # 缺坐标 → 前端不画点


def test_area_query_has_no_distance(signed, monkeypatch):
    fake_amap(monkeypatch)
    data = signed.get('/api/v1/restaurants?city=上海').json()
    assert data['query']['mode'] == 'text'
    assert data['query']['city'] == '上海'
    assert data['query']['location'] is None
    assert all(i['distance_m'] is None and i['distance_type'] is None for i in data['items'])


def test_map_config_not_configured_does_not_leak_keys(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_js_key', '')
    monkeypatch.setattr(core.settings, 'amap_service_host', '')
    monkeypatch.setattr(core.settings, 'amap_api_key', 'secret-server-key')
    data = signed.get('/api/v1/map/config').json()
    assert data['enabled'] is False
    assert data['js_ready'] is False
    assert data['verified'] is False
    assert data['js_key'] == ''
    assert data['service_host'] == ''
    assert data['coordinate_system'] == 'GCJ-02'
    assert 'secret-server-key' not in str(data)
    assert signed.get('/api/v1/capabilities').json()['map_js_ready'] is False


def test_map_config_configured_states_are_separate(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_js_key', 'public-js-key')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/_AMapService')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 'security-code-must-not-leak')
    monkeypatch.setattr(core.settings, 'amap_js_verified', False)
    data = signed.get('/api/v1/map/config').json()
    assert data['enabled'] is True
    assert data['js_ready'] is True
    assert data['verified'] is False  # 配置存在不等于验证通过
    assert data['js_key'] == 'public-js-key'
    assert data['service_host'] == '/_AMapService'
    assert 'security-code-must-not-leak' not in str(data)
    monkeypatch.setattr(core.settings, 'amap_js_verified', True)
    assert signed.get('/api/v1/map/config').json()['verified'] is True


def test_map_proxy_allows_whitelist_and_injects_credentials(signed, monkeypatch):
    captured = {}
    monkeypatch.setattr(core.settings, 'amap_api_key', 'server-key')
    monkeypatch.setattr(core.settings, 'amap_js_key', 'public-js-key')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 'server-jscode')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/_AMapService')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)

    def fake_request_json(method, url, **kwargs):
        captured['url'] = url
        captured['params'] = kwargs.get('params')
        return {'status': '1'}

    monkeypatch.setattr(providers, 'request_json', fake_request_json)
    # 客户端伪造的 key/jscode 不被采用
    r = signed.get('/api/v1/map/proxy/v3/place/around?location=121.45,31.23&key=evil&jscode=evil&url=http://evil')
    assert r.status_code == 200
    assert captured['url'] == 'https://restapi.amap.com/v3/place/around'
    # M3：JS 代理必须使用 JS 平台 Key，不与 Web 服务 Key 混用
    assert captured['params']['key'] == 'public-js-key'
    assert captured['params']['jscode'] == 'server-jscode'
    assert 'server-key' not in str(captured['params'])
    assert captured['params']['location'] == '121.45,31.23'
    assert 'url' not in captured['params']
    assert 'evil' not in str(captured['params'])


def test_map_proxy_rejects_arbitrary_paths(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_api_key', 'server-key')
    monkeypatch.setattr(core.settings, 'amap_js_key', 'public-js-key')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 'server-jscode')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/_AMapService')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    called = {'n': 0}

    def fake_request_json(*a, **k):
        called['n'] += 1
        return {}

    monkeypatch.setattr(providers, 'request_json', fake_request_json)
    for path in ('v3/place/../admin', 'v1/internal', 'v3/place/detail'):
        assert signed.get(f'/api/v1/map/proxy/{path}').status_code == 404
    assert called['n'] == 0


def test_map_proxy_requires_configuration(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_js_key', '')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', '')
    monkeypatch.setattr(core.settings, 'amap_service_host', '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    r = signed.get('/api/v1/map/proxy/v3/place/text?city=上海')
    assert r.status_code == 503
    assert r.json()['error']['code'] == 'MAP_NOT_CONFIGURED'


def test_restaurants_unconfigured_returns_503(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_api_key', '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    r = signed.get('/api/v1/restaurants?city=上海')
    assert r.status_code == 503
    assert r.json()['error']['code'] == 'NOT_CONFIGURED'


# ---- M1~M3 回归（前端地图核对） ----
def test_m1_js_ready_requires_security_code(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_api_key', 'fake-web-service-key')
    monkeypatch.setattr(core.settings, 'amap_js_key', 'fake-public-js-key')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/api/v1/map/proxy/_AMapService')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    config = signed.get('/api/v1/map/config').json()
    assert config['enabled'] is True       # 有 JS key 与代理地址
    assert config['js_ready'] is False     # 但代理实际会拒绝：不能报就绪
    assert config['verified'] is False
    assert config['js_key'] == ''
    assert signed.get('/api/v1/capabilities').json()['map_js_ready'] is False


def test_m1_verified_not_inferred_from_config(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_js_key', 'k')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 's')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/api/v1/map/proxy')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(core.settings, 'amap_js_verified', False)
    config = signed.get('/api/v1/map/config').json()
    assert config['js_ready'] is True and config['verified'] is False


def test_m2_advertised_service_host_routes_to_proxy(signed, monkeypatch):
    captured = []
    monkeypatch.setattr(core.settings, 'amap_js_key', 'fake-js-key')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 'fake-jscode')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/api/v1/map/proxy/_AMapService')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'request_json',
                        lambda m, u, **k: captured.append((u, k.get('params'))) or {'status': '1', 'pois': []})
    config = signed.get('/api/v1/map/config').json()
    assert config['service_host'].endswith('/_AMapService')
    r = signed.get(config['service_host'] + '/v3/place/text', params={'city': '上海'})
    assert r.status_code == 200
    assert captured[0][0] == 'https://restapi.amap.com/v3/place/text'
    assert captured[0][1]['key'] == 'fake-js-key'


def test_m2_service_host_prefix_is_normalized(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_js_key', 'k')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 's')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/api/v1/map/proxy')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    config = signed.get('/api/v1/map/config').json()
    assert config['service_host'] == '/api/v1/map/proxy/_AMapService'
    monkeypatch.setattr(providers, 'request_json', lambda *a, **k: {'status': '1'})
    assert signed.get(config['service_host'] + '/v3/geocode/geo').status_code == 200


def test_m2_prefixed_path_still_enforces_whitelist(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_js_key', 'k')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 's')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/api/v1/map/proxy')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    called = []
    monkeypatch.setattr(providers, 'request_json', lambda *a, **k: called.append(1) or {})
    for bad in ('_AMapService/v1/internal', '_AMapService/v3/place/detail'):
        assert signed.get(f'/api/v1/map/proxy/{bad}').status_code == 404
    assert called == []


def test_m3_map_js_usage_does_not_require_web_service_key(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_api_key', '')  # Web 服务 Key 缺失
    monkeypatch.setattr(core.settings, 'amap_js_key', 'fake-js-key')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 'fake-jscode')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/api/v1/map/proxy')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'request_json', lambda *a, **k: {'status': '1'})
    assert signed.get('/api/v1/map/proxy/v3/place/text?city=上海').status_code == 200
    # 餐厅后端查询仍要求 Web 服务 Key，两类凭据不互相顶替
    assert signed.get('/api/v1/restaurants?city=上海').status_code == 503
    with core.DB() as db:
        usage = [r.payload.get('operation') for r in db.scalars(
            __import__('sqlalchemy').select(core.Record).where(core.Record.kind == 'usage'))]
    assert 'map_js' in usage

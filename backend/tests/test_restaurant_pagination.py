"""R1 餐厅分页验收：分页契约、查询口径、去重、两入口一致与失败语义。

全部使用隔离库与假供应商响应；真实外部调用在 conftest 中已被 HTTP 层阻断。
"""
import json

import pytest

from app import core, providers, worker
from app.core import now
from app.services import restaurant_service
from backend.tests.amap_fixtures import district_response


def pois(n, prefix='B'):
    # 构造“有素食类别证据”的 POI：真实响应里 050120 店会带自己的类别字段
    return [{'id': f'{prefix}{i:03d}', 'name': f'素食店{i}', 'address': f'某路{i}号',
             'location': '121.45,31.23', 'distance': str(900 + i),
             'typecode': '050120', 'type': '餐饮服务;中餐厅;中式素菜馆',
             'biz_ext': {'rating': '4.5', 'cost': '60', 'open_time': '10:00-21:00'}}
            for i in range(n)]


def fake_amap(monkeypatch, page_items, count=None, captured=None):
    """按调用返回给定的单页结果；记录请求参数以便断言“只取一页”。"""
    state = {'calls': []}

    def fake_request_json(method, url, **kwargs):
        if url.endswith('/config/district'):
            return district_response(kwargs['params']['keywords'])
        state['calls'].append({'url': url, 'params': kwargs.get('params')})
        code = kwargs['params'].get('city')
        payload = {'status': '1', 'pois': [{**item, **({'adcode': code} if code and code.isdigit() else {})} for item in page_items]}
        if count is not None:
            payload['count'] = count
        return payload

    monkeypatch.setattr(providers, 'request_json', fake_request_json)
    if captured is not None:
        captured.append(state)
    return state


def enable(signed, monkeypatch, key='test-only'):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'amap_api_key', key)
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


# ---------------- 分页基本行为 ----------------
def test_first_page_defaults_to_20(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(20), count='37')
    data = signed.get('/api/v1/restaurants?city=上海').json()
    assert state['calls'][0]['params']['page'] == 1
    assert state['calls'][0]['params']['offset'] == 20
    page = data['pagination']
    assert page['page'] == 1 and page['page_size'] == 20
    assert page['returned'] == 20 and page['raw_returned'] == 20
    assert page['has_more'] is True and page['next_page'] == 2
    assert page['matched_count'] == 37 and page['count_source'] == 'provider_reported'
    assert page['end_reason'] is None


def test_second_page_returns_three_and_ends(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(3, prefix='C'), count='23')
    data = signed.get('/api/v1/restaurants?city=上海&page=2').json()
    page = data['pagination']
    assert page['page'] == 2 and page['returned'] == 3
    assert page['has_more'] is False and page['next_page'] is None
    assert page['end_reason'] == 'no_more_matches'


def test_empty_page_is_not_error(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, [], count='20')
    data = signed.get('/api/v1/restaurants?city=上海&page=3').json()
    assert data['items'] == []
    page = data['pagination']
    assert page['returned'] == 0 and page['has_more'] is False
    assert page['end_reason'] == 'empty_page'


def test_missing_count_is_unknown_not_page_size(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(20))          # 无 count 字段
    page = signed.get('/api/v1/restaurants?city=上海').json()['pagination']
    assert page['matched_count'] is None
    assert page['count_source'] == 'unknown'
    assert page['has_more'] is True           # 整页返回且未到窗口 → 可能还有
    assert page['next_page'] == 2


def test_abnormal_count_is_treated_as_unknown(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(20), count='5')   # 匹配数 < 本页条数，明显异常
    page = signed.get('/api/v1/restaurants?city=上海').json()['pagination']
    assert page['matched_count'] is None
    assert page['count_source'] == 'unknown'
    fake_amap(monkeypatch, [], count='abc')
    page2 = signed.get('/api/v1/restaurants?city=上海&page=5').json()['pagination']
    assert page2['matched_count'] is None


def test_window_limit_is_reported_not_claimed_complete(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(20), count='5000')
    page = signed.get('/api/v1/restaurants?city=上海&page=10').json()['pagination']
    assert page['reached_window_end'] is True
    assert page['has_more'] is False
    assert page['end_reason'] == 'reached_window'
    assert page['next_page'] is None
    assert page['window']['max_records'] == 200


def test_beyond_window_rejected_before_calling_provider(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(20))
    r = signed.get('/api/v1/restaurants?city=上海&page=11&page_size=20')
    assert r.status_code == 422
    assert r.json()['error']['code'] == 'PAGE_WINDOW_EXCEEDED'
    assert state['calls'] == []            # 请求前拒绝，不消耗额度


# ---------------- 参数校验 ----------------
@pytest.mark.parametrize('query,code', [
    ('page=0', 'INVALID_PAGINATION'),
    ('page=101', 'INVALID_PAGINATION'),
    ('page_size=0', 'INVALID_PAGINATION'),
    ('page_size=26', 'INVALID_PAGINATION'),
    ('page=abc', None),
])
def test_invalid_pagination_rejected(signed, monkeypatch, query, code):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(1))
    r = signed.get(f'/api/v1/restaurants?city=上海&{query}')
    assert r.status_code == 422
    if code:
        assert r.json()['error']['code'] == code
    assert state['calls'] == []


def test_invalid_location_rejected(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(1))
    # 超出经纬度范围：服务在请求供应商前拒绝（400，沿用既有 INVALID_LOCATION）
    for bad in ('200,31', '121.45,91'):
        r = signed.get(f'/api/v1/restaurants?location={bad}')
        assert r.status_code == 400
        assert r.json()['error']['code'] == 'INVALID_LOCATION'
    # 完全不合法格式由请求层拒绝
    assert signed.get('/api/v1/restaurants?location=abc').status_code == 422
    assert state['calls'] == []


# ---------------- 去重 ----------------
def test_in_page_dedup_keeps_missing_ids(signed, monkeypatch):
    enable(signed, monkeypatch)
    items = pois(3) + [pois(1)[0]] + [{'name': '无ID素食店', 'location': '121.4,31.2',
                                       'typecode': '050120', 'type': '餐饮服务;中餐厅;中式素菜馆'}]
    fake_amap(monkeypatch, items, count='5')
    page = signed.get('/api/v1/restaurants?city=上海').json()['pagination']
    data = signed.get('/api/v1/restaurants?city=上海').json()
    assert page['raw_returned'] == 5
    assert page['returned'] == 4          # 重复 id 去掉一个，缺 id 保留
    assert page['deduplicated'] == 1
    assert any(i['id'] is None for i in data['items'])


def test_dedup_does_not_fake_end_of_results(signed, monkeypatch):
    """去重后不足一页，但供应商原始页是整页 → 仍可能有下一页。"""
    enable(signed, monkeypatch)
    items = pois(10) * 2                  # 20 条里只有 10 个不同 id
    fake_amap(monkeypatch, items)         # 无 count
    page = signed.get('/api/v1/restaurants?city=上海').json()['pagination']
    assert page['returned'] == 10
    assert page['raw_returned'] == 20
    assert page['has_more'] is True


# ---------------- 查询口径 ----------------
def test_around_keeps_coordinate_radius_and_distance(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(2), count='2')
    data = signed.get('/api/v1/restaurants?location=121.45,31.23&page=1&page_size=25').json()
    params = state['calls'][0]['params']
    assert params['radius'] == 3000 and params['sortrule'] == 'distance'
    assert params['offset'] == 25 and params['page'] == 1
    q = data['query']
    assert q['mode'] == 'around' and q['radius_m'] == 3000 and q['sortrule'] == 'distance'
    assert q['center_source'] == 'request_coordinate'   # 不等于“用户本人位置”
    assert q['coordinate_system'] == 'GCJ-02'
    assert data['items'][0]['distance_m'] is not None
    assert data['items'][0]['distance_type'] == 'straight_line'


def test_text_query_has_no_distance(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(2), count='2')
    data = signed.get('/api/v1/restaurants?city=上海').json()
    assert data['query']['mode'] == 'text'
    assert data['query']['radius_m'] is None and data['query']['sortrule'] is None
    assert data['query']['center_source'] == 'request_area'
    assert all(i['distance_m'] is None and i['distance_type'] is None for i in data['items'])


def test_page_flip_does_not_drift_conditions(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(20), count='40')
    first = signed.get('/api/v1/restaurants?location=121.45,31.23&page=1').json()['query']
    page2 = signed.get('/api/v1/restaurants?location=121.45,31.23&page=2').json()['query']
    params1, params2 = state['calls'][0]['params'], state['calls'][1]['params']
    assert {k: v for k, v in first.items() if k not in ('page', 'at')} == \
           {k: v for k, v in page2.items() if k not in ('page', 'at')}
    assert params1['radius'] == params2['radius'] == 3000
    assert params1['sortrule'] == params2['sortrule'] == 'distance'
    assert params1['types'] == params2['types'] == '050120'


def test_only_one_page_requested_per_query(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(20), count='200')
    signed.get('/api/v1/restaurants?city=上海&page=1')
    assert len(state['calls']) == 1     # 不自动拉全量


# ---------------- 两个入口一致 ----------------
def test_http_and_agent_share_structure(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(3), count='23')
    http = signed.get('/api/v1/restaurants?location=121.45,31.23&radius_m=10000&page=2&page_size=5').json()

    from app import agent
    with core.DB() as db:
        owner = db.scalar(select_owner()).id
    payload, card = agent._execute_tool('search_restaurants',
                                        {'location': '121.45,31.23', 'page': 2, 'page_size': 5},
                                        owner, {})
    assert card['type'] == 'restaurant_list'
    assert set(card['payload']) - {'presentation'} == set(http)
    assert card['payload']['presentation']['preferred_view'] == 'list'
    assert card['payload']['pagination'] == http['pagination']
    assert {k: v for k, v in card['payload']['query'].items() if k != 'at'} == \
           {k: v for k, v in http['query'].items() if k != 'at'}


@pytest.mark.parametrize('area', ['北京朝阳', '北京市朝阳区', '北京 朝阳区'])
def test_http_region_resolves_district_adcode_instead_of_poi_keyword(signed, monkeypatch, area):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(1), count='25')
    response = signed.get('/api/v1/restaurants', params={'city': area})
    assert response.status_code == 200
    data = response.json()
    assert state['calls'][0]['params']['city'] == '110105'
    assert state['calls'][0]['params']['keywords'] == '素食'
    assert data['query']['city'] == area
    assert data['query']['area_resolution']['adcode'] == '110105'
    assert data['query']['keywords'] == '素食'


def test_district_query_and_second_page_have_identical_http_and_agent_conditions(signed, monkeypatch):
    from app import agent
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(20), count='43')
    first = signed.get('/api/v1/restaurants', params={'city': '北京朝阳', 'page_size': 20}).json()
    second = signed.get('/api/v1/restaurants', params={'city': '北京朝阳', 'page': 2, 'page_size': 20}).json()
    with core.DB() as db:
        owner = db.scalar(select_owner()).id
    result, card = agent._execute_tool('search_restaurants', {'city': '北京朝阳', 'page': 2, 'page_size': 20}, owner, {})
    assert all(call['params']['city'] == '110105' and call['params']['keywords'] == '素食' for call in state['calls'])
    assert first['query']['keywords'] == second['query']['keywords'] == result['query']['keywords'] == '素食'
    assert {k: v for k, v in result['query'].items() if k != 'at'} == {k: v for k, v in second['query'].items() if k != 'at'}
    assert card['payload']['scope']['version'] == first['scope']['version'] == 'veg-admission-v3'
    assert len(state['calls']) == 2  # Agent reuses HTTP page 2; page 1 remains a separate request.
    assert result['cache']['hit'] is True


def test_page_preserves_explicit_keywords_without_losing_the_district(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(20), count='43')
    first = signed.get('/api/v1/restaurants', params={'city': '北京朝阳区', 'keywords': '素食'}).json()
    second = signed.get('/api/v1/restaurants', params={'city': first['query']['city'],
        'keywords': first['query']['keywords'], 'page': 2, 'page_size': 20}).json()
    assert first['query']['keywords'] == second['query']['keywords'] == '素食'
    assert all(call['params']['city'] == '110105' and call['params']['keywords'] == '素食' for call in state['calls'])


def test_combined_region_and_keywords_limit_is_rejected_before_provider(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(1))
    response = signed.get('/api/v1/restaurants', params={'city': '北京朝阳', 'keywords': '字' * 81})
    assert response.status_code == 422
    assert state['calls'] == []


def select_owner():
    from sqlalchemy import select
    return select(core.User)


def test_agent_invalid_pagination_matches_http(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(1))
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select_owner()).id
    with pytest.raises(core.AppError) as exc:
        agent._execute_tool('search_restaurants', {'city': '上海', 'page_size': 99}, owner, {})
    assert exc.value.code == 'INVALID_PAGINATION'
    assert state['calls'] == []


# ---------------- 失败语义 ----------------
def test_unconfigured_returns_503_not_empty(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_api_key', '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    r = signed.get('/api/v1/restaurants?city=上海&page=2')
    assert r.status_code == 503
    assert r.json()['error']['code'] == 'NOT_CONFIGURED'


def test_second_page_upstream_failure_is_error_not_empty(signed, monkeypatch):
    enable(signed, monkeypatch)

    def boom(*a, **k):
        raise core.AppError('UPSTREAM_FAILED', '外部服务未正常返回，请稍后重试。', 502)

    monkeypatch.setattr(providers, 'request_json', boom)
    r = signed.get('/api/v1/restaurants?city=上海&page=2')
    assert r.status_code == 502
    assert r.json()['error']['code'] == 'UPSTREAM_FAILED'
    assert 'items' not in r.json()


def test_rate_limited_maps_to_429(signed, monkeypatch):
    enable(signed, monkeypatch)

    def limited(*a, **k):
        raise core.AppError('UPSTREAM_RATE_LIMITED', '外部服务当前限流，请稍后再试。', 429)

    monkeypatch.setattr(providers, 'request_json', limited)
    r = signed.get('/api/v1/restaurants?city=上海')
    assert r.status_code == 429
    assert r.json()['error']['code'] == 'UPSTREAM_RATE_LIMITED'


def test_provider_status_error_is_not_empty_success(signed, monkeypatch):
    enable(signed, monkeypatch)
    monkeypatch.setattr(providers, 'request_json', lambda *a, **k: {'status': '0', 'info': 'INVALID_USER_KEY'})
    r = signed.get('/api/v1/restaurants?city=上海')
    assert r.status_code == 502
    assert r.json()['error']['code'] == 'MAP_UNAVAILABLE'
    assert 'INVALID_USER_KEY' not in r.text     # 不泄露供应商原始细节


def test_quota_exhausted_returns_429(signed, monkeypatch):
    enable(signed, monkeypatch)
    monkeypatch.setattr(core.settings, 'daily_external_limit', 0)
    r = signed.get('/api/v1/restaurants?city=上海')
    assert r.status_code == 429
    assert r.json()['error']['code'] == 'QUOTA_EXCEEDED'


def test_usage_recorded_per_page(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(20), count='40')
    signed.get('/api/v1/restaurants?city=上海&page=1')
    signed.get('/api/v1/restaurants?city=上海&page=2')
    with core.DB() as db:
        usage = [r for r in db.scalars(__import__('sqlalchemy').select(core.Record)
                                       .where(core.Record.kind == 'usage'))]
    assert sum(1 for r in usage if r.payload.get('operation') == 'restaurants') == 2


# ---------------- 对话卡片 ----------------
def test_chat_card_carries_pagination(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(20), count='37')
    replies = iter([
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'c1', 'name': 'search_restaurants',
                         'arguments': {'city': '上海', 'page': 1}}]},
        {'text': '找到一批。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(replies))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                          json={'content': '找素食'}).json()['turn_id']
    worker.process_once()
    data = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    card = [c for c in data['reply']['cards'] if c['type'] == 'restaurant_list'][0]
    assert card['payload']['pagination']['next_page'] == 2
    assert card['payload']['pagination']['matched_count'] == 37


# ================= 复审收尾：三项边界 =================
def test_next_page_never_violates_window_at_page_66_size_3(signed, monkeypatch):
    """page=66,size=3：窗口 198+3>200 → 不再宣称有更多，也不给非法下一页。"""
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(3), count='250')
    page = signed.get('/api/v1/restaurants?city=上海&page=66&page_size=3').json()['pagination']
    assert page['has_more'] is False
    assert page['next_page'] is None
    assert page['end_reason'] == 'reached_window'
    assert page['reached_window_end'] is True


def test_next_page_never_exceeds_max_page_at_100_size_1(signed, monkeypatch):
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(1), count='150')
    page = signed.get('/api/v1/restaurants?city=上海&page=100&page_size=1').json()['pagination']
    assert page['has_more'] is False
    assert page['next_page'] is None
    assert page['end_reason'] == 'reached_page_limit'


def test_has_more_implies_requestable_next_page(signed, monkeypatch):
    """任何 has_more=true 的结果，next_page 都必须能通过同一套参数校验。"""
    enable(signed, monkeypatch)
    cases = [(1, 20, '68'), (2, 20, '68'), (66, 3, '250'), (100, 1, '150'), (10, 20, '5000')]
    for page, size, count in cases:
        fake_amap(monkeypatch, pois(size), count=count)
        data = signed.get(f'/api/v1/restaurants?city=上海&page={page}&page_size={size}').json()
        paged = data['pagination']
        if paged['has_more']:
            assert paged['next_page'] is not None, (page, size, count)
            assert paged['end_reason'] is None, (page, size, count)
            # 用同一套校验确认可请求
            restaurant_service.validate_pagination(paged['next_page'], size)
        else:
            assert paged['next_page'] is None and paged['end_reason'] is not None


def test_count_below_offset_plus_returned_is_unknown(signed, monkeypatch):
    """第 2 页已拿到第 21–40 条，供应商报 count=20 → 矛盾，按未知处理。"""
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(20), count='20')
    data = signed.get('/api/v1/restaurants?city=上海&page=2').json()
    page = data['pagination']
    assert page['matched_count'] is None
    assert page['count_source'] == 'unknown'
    assert page['has_more'] is True          # 不宣称已全部返回
    assert page['next_page'] == 3
    assert page['end_reason'] is None


def test_count_exactly_offset_plus_returned_is_consistent(signed, monkeypatch):
    """count 恰好等于已返回总数 → 可信，且确实没有更多。"""
    enable(signed, monkeypatch)
    fake_amap(monkeypatch, pois(20), count='40')
    page = signed.get('/api/v1/restaurants?city=上海&page=2').json()['pagination']
    assert page['matched_count'] == 40
    assert page['count_source'] == 'provider_reported'
    assert page['has_more'] is False
    assert page['end_reason'] == 'no_more_matches'


@pytest.mark.parametrize('bad', ['abc', '121.47', '121.4,31.2,5', 'nan,31', 'inf,31', '200,31', '121.4,91', '', 123])
def test_agent_invalid_location_returns_business_error(signed, monkeypatch, bad):
    """Agent 传入非法坐标必须得到稳定的 INVALID_LOCATION，且不预占额度、不调供应商。"""
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(1))
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select_owner()).id
    if bad == '':
        # 空坐标等价于未提供坐标，此时缺少城市 → 参数错误（不是坐标错误）
        with pytest.raises(core.AppError) as exc:
            agent._execute_tool('search_restaurants', {'location': bad}, owner, {})
        assert exc.value.code == 'INVALID_ARGUMENTS'
    else:
        with pytest.raises(core.AppError) as exc:
            agent._execute_tool('search_restaurants', {'location': bad}, owner, {})
        assert exc.value.code == 'INVALID_LOCATION'
        assert exc.value.status == 400
    assert state['calls'] == []
    with core.DB() as db:
        usage = [r for r in db.scalars(__import__('sqlalchemy').select(core.Record)
                                       .where(core.Record.kind == 'usage'))]
    assert usage == []          # 未预占用量


def test_service_locates_valid_coordinate_normalized(signed, monkeypatch):
    enable(signed, monkeypatch)
    state = fake_amap(monkeypatch, pois(1), count='1')
    signed.get('/api/v1/restaurants?location=121.4737,31.2304')
    assert state['calls'][0]['params']['location'] == '121.4737,31.2304'

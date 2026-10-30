"""Offline HTTP -> worker -> Agent continuation, retaining the single-page API."""
from app import core, providers, worker


POINT = '116.41,39.91'  # Public synthetic coordinate, never a user's location.


def _setup(monkeypatch):
    for key in ('deepseek_api_key', 'amap_api_key'):
        monkeypatch.setattr(core.settings, key, 'offline-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    calls = {'model': 0, 'pages': []}

    def upstream(method, url, **kwargs):
        assert url.endswith('/place/around'), url
        params = kwargs['params']
        calls['pages'].append(dict(params))
        if int(params['page']) == 1:
            pois = [{'id': f'ordinary-{i}', 'name': f'测试普通餐馆{i}', 'typecode': '050100',
                     'type': '餐饮服务;中餐厅;中餐厅', 'location': POINT} for i in range(20)]
        else:
            pois = [{'id': 'second-page-vegan', 'name': '测试纯素食餐厅', 'typecode': '050120',
                     'type': '餐饮服务;中餐厅;中式素菜馆', 'location': '116.42,39.92'}]
        return {'status': '1', 'count': '60', 'pois': pois}

    def model(_history):
        calls['model'] += 1
        return {'text': '', 'needs_input': False, 'input_prompt': '', 'tool_calls': [{
            'id': 'call-restaurant-flow', 'name': 'search_restaurants',
            'arguments': {'location': POINT, 'radius_m': 10000, 'keywords': '素食',
                          'page': 1, 'page_size': 20, 'view': 'list'},
        }]}

    monkeypatch.setattr(providers, 'request_json', upstream)
    monkeypatch.setattr(providers, 'generate_chat_reply', model)
    return calls


def _turn(client, sid, content):
    response = client.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': content})
    assert response.status_code == 201, response.json()
    tid = response.json()['turn_id']
    worker.process_once()
    result = client.get(f'/api/v1/chat/sessions/{sid}/turns/{tid}').json()
    assert result['status'] == 'succeeded', result
    return tid, result['reply']


def test_empty_first_page_reaches_second_page_through_worker_and_map_reuses_it(signed, monkeypatch):
    calls = _setup(monkeypatch)
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    tid, reply = _turn(signed, sid, f'我的本次位置是 {POINT}（GCJ-02）。找找我周围的素食餐厅')
    assert reply['restaurant_guard']['status'] == 'ok'
    assert len(reply['cards']) == 1
    card = reply['cards'][0]
    assert card['type'] == 'restaurant_list'
    payload = card['payload']
    assert [row['id'] for row in payload['items']] == ['second-page-vegan']
    assert payload['query']['page'] == 2
    assert payload['query']['location'] == POINT
    assert payload['query']['radius_m'] == 10000
    assert payload['query']['keywords'] == '素食'
    assert payload['pagination']['has_more'] is True
    assert payload['pagination']['next_page'] == 3
    assert [int(p['page']) for p in calls['pages']] == [1, 2]
    assert calls['model'] == 1
    with core.DB() as db:
        task = db.get(core.Job, tid)
        assert task.payload['budget']['tool_calls_used'] == 2
    history = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    saved = next(row for row in history if row['role'] == 'assistant')
    assert saved['cards'][0]['payload'] == payload

    _tid, mapped = _turn(signed, sid, '把它们标在地图上')
    assert mapped['cards'][0]['payload']['items'] == payload['items']
    assert mapped['cards'][0]['payload']['query'] == payload['query']
    assert mapped['cards'][0]['payload']['presentation'] == {
        'preferred_view': 'map', 'reused_from_conversation': True,
    }
    assert len(calls['pages']) == 2 and calls['model'] == 1


def test_single_page_http_remains_empty_and_agent_reuses_that_cached_page(signed, monkeypatch):
    calls = _setup(monkeypatch)
    response = signed.get('/api/v1/restaurants', params={
        'location': POINT, 'radius_m': 10000, 'keywords': '素食', 'page': 1, 'page_size': 20,
    })
    assert response.status_code == 200
    first = response.json()
    assert first['items'] == []
    assert first['pagination']['raw_returned'] == first['pagination']['filtered_out'] == 20
    assert first['pagination']['has_more'] is True
    assert len(calls['pages']) == 1
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    _tid, reply = _turn(signed, sid, f'我的本次位置是 {POINT}（GCJ-02）。找附近的素食餐厅')
    assert reply['restaurant_guard']['status'] == 'ok'
    assert [int(p['page']) for p in calls['pages']] == [1, 2]
    again = signed.get('/api/v1/restaurants', params={
        'location': POINT, 'radius_m': 10000, 'keywords': '素食', 'page': 1, 'page_size': 20,
    }).json()
    assert again['items'] == [] and again['pagination']['page'] == 1
    assert again['cache']['hit'] is True
    assert len(calls['pages']) == 2

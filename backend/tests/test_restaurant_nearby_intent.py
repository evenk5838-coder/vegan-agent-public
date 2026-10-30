"""Reported nearby dialogue failure: relative words are never provider cities.

Runs through real worker/HTTP/persistence in an isolated DB; paid services are doubles.
"""
import pytest
from sqlalchemy import select

from app import agent, core, providers, worker
from app.services import restaurant_service
from backend.tests.amap_fixtures import district_response


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'synthetic')
    monkeypatch.setattr(core.settings, 'amap_api_key', 'synthetic')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    seen = []
    def request(method, url, **kwargs):
        if url.endswith('/config/district'):
            return district_response(kwargs['params']['keywords'])
        seen.append({'url': url, 'params': dict(kwargs['params'])})
        return {'status': '1', 'count': '1', 'pois': [
            {'id': 'nearby-fixture', 'name': '测试素食餐厅', 'typecode': '050120', 'adcode': kwargs['params'].get('city'),
             'type': '餐饮服务;中餐厅;中式素菜馆', 'location': '116.41,39.91'}]}
    monkeypatch.setattr(providers, 'request_json', request)
    return seen


def new_session(client):
    return client.post('/api/v1/chat/sessions', json={}).json()['id']


def turn(client, sid, content):
    response = client.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': content})
    assert response.status_code == 201
    worker.process_once()
    result = client.get(f'/api/v1/chat/sessions/{sid}/turns/{response.json()["turn_id"]}').json()
    assert result['status'] == 'succeeded', result
    return result['reply']


def final():
    return {'text': '好的', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}


@pytest.mark.parametrize('content', [
    '找一找我周围的餐厅', '找我家附近的素食馆', '查我身边的素食餐厅',
    '查我周边的素食店', '找我的当前位置附近的餐厅', '找这里的素食餐厅'])
def test_relative_request_without_position_clarifies_without_paid_calls(signed, upstream, monkeypatch, content):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda *_: pytest.fail('clarification needs no model'))
    monkeypatch.setattr(providers, 'reserve_call', lambda *_: pytest.fail('clarification needs no paid call'))
    assert agent._extract_area(content) is None
    reply = turn(signed, new_session(signed), content)
    assert reply['restaurant_guard']['status'] == 'missing_area'
    assert reply['needs_input'] is True and reply['input_prompt'] == reply['text']
    assert '定位并查附近餐厅' in reply['text'] and '城市或区县' in reply['text']
    assert not reply['cards'] and not upstream


def test_missing_position_then_composer_location_searches_actual_center_and_restores_map(signed, upstream, monkeypatch):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda *_: final())
    sid = new_session(signed)
    turn(signed, sid, '找一找我周围的餐厅')
    # Exact format emitted by the existing frontend, including its prefix.
    reply = turn(signed, sid, '我的本次位置是 116.4,39.9（GCJ-02）。找一找我周围的餐厅，查看地图')
    payload = reply['cards'][0]['payload']
    assert reply['restaurant_guard']['status'] == 'ok'
    assert payload['query']['mode'] == 'around' and payload['query']['location'] == '116.4,39.9'
    assert payload['query']['radius_m'] == 10000
    assert payload['presentation']['preferred_view'] == 'map'
    assert len(upstream) == 1 and upstream[0]['url'].endswith('/around')
    assert upstream[0]['params']['location'] == '116.4,39.9'
    messages = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    assert messages[-1]['cards'][0]['payload'] == payload


def test_region_answer_after_nearby_clarification_still_searches_area(signed, upstream, monkeypatch):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda *_: final())
    sid = new_session(signed)
    turn(signed, sid, '找一找我周围的餐厅')
    reply = turn(signed, sid, '北京朝阳')
    assert reply['restaurant_guard']['status'] == 'ok'
    assert upstream[0]['url'].endswith('/text')
    assert upstream[0]['params']['city'] == '110105'
    assert upstream[0]['params']['city'] == '110105'
    assert upstream[0]['params']['keywords'] == '素食'


def test_historical_location_and_city_are_not_treated_as_current_device_location(signed, upstream, monkeypatch):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda *_: final())
    sid = new_session(signed)
    turn(signed, sid, '我的本次位置是 116.4,39.9（GCJ-02）。查附近素食餐厅')
    reply = turn(signed, sid, '找一找我周围的餐厅')
    assert reply['restaurant_guard']['status'] == 'missing_area'
    assert len(upstream) == 1


@pytest.mark.parametrize('guessed', [{'city': '上海'}, {'city': '我周围'}, {'location': '121.47,31.23'}])
def test_mixed_request_cannot_use_model_guessed_position(signed, upstream, monkeypatch, guessed):
    responses = iter([
        {**final(), 'tool_calls': [
            {'id': 'i', 'name': 'evaluate_ingredients', 'arguments': {'text': '小麦粉、奶粉', 'complete': True}},
            {'id': 'r', 'name': 'search_restaurants', 'arguments': guessed}]}, final()])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda *_: next(responses))
    reply = turn(signed, new_session(signed), '这包配料能吃吗？找一找我周围的餐厅')
    assert reply['restaurant_guard']['status'] == 'missing_area' and not upstream
    assert any(card['type'] == 'ingredient_result' for card in reply['cards'])
    assert 'Vegan：不适合' in reply['text'] and '定位并查附近餐厅' in reply['text']


def test_current_position_overrides_model_guess_for_nearby_request(signed, upstream, monkeypatch):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda *_: {
        **final(), 'tool_calls': [{'id': 'r', 'name': 'search_restaurants',
                                  'arguments': {'city': '上海', 'location': '121.47,31.23'}}]})
    reply = turn(signed, new_session(signed), '我的本次位置是 116.4,39.9（GCJ-02）。找我周围的餐厅')
    assert reply['restaurant_guard']['status'] == 'ok'
    assert upstream[0]['params']['location'] == '116.4,39.9'


@pytest.mark.parametrize('city', ['我周围', '我的附近', '我家附近', '当前位置', 'near me'])
def test_shared_http_service_blocks_relative_city_before_metering_or_upstream(signed, upstream, monkeypatch, city):
    monkeypatch.setattr(providers, 'reserve_call', lambda *_: pytest.fail('must reject before metering'))
    response = signed.get('/api/v1/restaurants', params={'city': city})
    assert response.status_code == 422 and response.json()['error']['code'] == 'MISSING_LOCATION'
    assert not upstream


@pytest.mark.parametrize('error', ['INVALID_ARGUMENTS', 'INVALID_RADIUS', 'INVALID_LOCATION', 'INVALID_PAGINATION'])
def test_parameter_errors_are_not_reported_as_service_outage(error):
    reply = agent._finalize_reply(final(), [], '找北京的素食餐厅', True,
                                  restaurant_outcome={'status': 'failed', 'error_code': error})
    assert reply['restaurant_guard']['error_code'] == error
    assert '服务暂时不可用' not in reply['text']


def test_real_named_area_nearby_is_preserved():
    assert agent._extract_area('找北京朝阳周围的素食餐厅') == '北京朝阳'

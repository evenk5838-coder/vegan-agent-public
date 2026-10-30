"""Conversation -> real service card -> same-session map/list continuation.

HTTP/worker/DB are exercised; model and AMap are explicit offline doubles.
"""
import time

import pytest
from sqlalchemy import select

from app import agent, core, providers, worker
from backend.tests.amap_fixtures import district_response
from app.services import restaurant_service


@pytest.fixture
def setup(signed, monkeypatch):
    for key in ('amap_api_key', 'deepseek_api_key'):
        monkeypatch.setattr(core.settings, key, 'synthetic')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    calls = []
    def upstream(method, url, **kwargs):
        if url.endswith('/config/district'):
            return district_response(kwargs['params']['keywords'])
        calls.append(kwargs['params'])
        return {'status': '1', 'count': '1', 'pois': [
            {'id': 'test-place', 'name': '测试素食餐厅', 'typecode': '050120', 'adcode': kwargs['params'].get('city'),
             'type': '餐饮服务;中餐厅;中式素菜馆', 'location': '116.41,39.91'}]}
    monkeypatch.setattr(providers, 'request_json', upstream)
    return calls


def session(client):
    return client.post('/api/v1/chat/sessions', json={}).json()['id']


def turn(client, sid, text):
    response = client.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': text})
    assert response.status_code == 201
    tid = response.json()['turn_id']
    worker.process_once()
    result = client.get(f'/api/v1/chat/sessions/{sid}/turns/{tid}').json()
    assert result['status'] == 'succeeded', result
    return result['reply']


def receipt(text='好的'):
    return {'text': text, 'needs_input': False, 'input_prompt': '', 'tool_calls': []}


@pytest.mark.parametrize('text', ['北京朝阳', '就在北京朝阳', '上海静安'])
def test_bare_area_answer_routes_after_restaurant_clarification(text):
    history = [{'role': 'user', 'content': '附近有什么素食餐厅'},
               {'role': 'assistant', 'content': '哪个城市或区县？'}, {'role': 'user', 'content': text}]
    assert agent._asks_for_restaurant(text, history)
    assert agent._extract_area(text) in ('北京朝阳', '上海静安')


@pytest.mark.parametrize('text', ['打开地图', '在地图上显示这些餐厅', '把它们标在地图上', '给我看看列表'])
def test_map_and_list_commands_are_never_sent_as_a_place_name(text):
    assert agent._asks_for_restaurant(text)
    assert agent._extract_area(text) is None


@pytest.mark.parametrize('text', ['看看菜谱列表', '显示食材列表', '给我看看配料历史列表'])
def test_other_feature_lists_are_not_reinterpreted_as_restaurants(text):
    history = [{'role': 'user', 'content': '找北京的素食餐厅'},
               {'role': 'assistant', 'content': '已找到餐厅'}, {'role': 'user', 'content': text}]
    assert not agent._asks_for_restaurant(text, history)


def test_area_clarification_produces_card_even_if_model_does_not_call_tool(signed, setup, monkeypatch):
    model_calls = []
    def model(history):
        model_calls.append(history)
        return receipt('请自行打开地图。')
    monkeypatch.setattr(providers, 'generate_chat_reply', model)
    sid = session(signed)
    first = turn(signed, sid, '附近有什么素食餐厅')
    assert first['restaurant_guard']['status'] == 'missing_area'
    second = turn(signed, sid, '北京朝阳')
    assert second['restaurant_guard']['status'] == 'ok'
    assert second['cards'][0]['type'] == 'restaurant_list'
    assert second['cards'][0]['payload']['items'][0]['id'] == 'test-place'
    assert len(setup) == 1 and len(model_calls) == 1
    assert '自行打开' not in second['text']


def test_map_followup_reuses_same_session_places_without_model_or_supplier(signed, setup, monkeypatch):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda history: receipt())
    sid = session(signed)
    first = turn(signed, sid, '帮我找北京的素食餐厅')
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda *_: pytest.fail('map reuse must not buy another model response'))
    for text, view in [('把它们标在地图上', 'map'), ('给我看看列表', 'list')]:
        reply = turn(signed, sid, text)
        payload = reply['cards'][0]['payload']
        assert payload['items'] == first['cards'][0]['payload']['items']
        assert payload['query'] == first['cards'][0]['payload']['query']
        assert payload['presentation'] == {'preferred_view': view, 'reused_from_conversation': True}
        assert reply['restaurant_guard']['status'] == 'ok'
    assert len(setup) == 1
    # The exact same result and view hint survive reload.
    messages = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()
    rows = messages if isinstance(messages, list) else messages['items']
    assert rows[-1]['cards'][0]['payload']['presentation']['preferred_view'] == 'list'


def test_new_session_does_not_borrow_a_previous_map(signed, setup, monkeypatch):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda history: receipt())
    turn(signed, session(signed), '帮我找北京的素食餐厅')
    result = turn(signed, session(signed), '打开地图')
    assert not result['cards']
    assert result['restaurant_guard']['status'] == 'missing_area'
    assert len(setup) == 1


def test_new_area_in_map_request_queries_that_area_instead_of_reusing_old_results(signed, setup, monkeypatch):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda history: receipt())
    sid = session(signed)
    turn(signed, sid, '帮我找北京的素食餐厅')
    result = turn(signed, sid, '在地图上显示上海的餐厅')
    assert len(setup) == 2 and setup[-1]['city'] == '上海'
    assert result['cards'][0]['payload']['presentation']['preferred_view'] == 'map'


@pytest.mark.parametrize('text', ['打开地图，另外看看我的冰箱', '打开地图，筛选Vegan餐厅', '打开地图，范围扩大到20公里'])
def test_new_constraints_are_not_silently_reused(signed, setup, monkeypatch, text):
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda history: receipt())
    sid = session(signed)
    turn(signed, sid, '找北京的素食餐厅')
    # A new request with other work must still pass through the model, not the
    # pure presentation shortcut that would swallow that part of the question.
    seen = []
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda history: seen.append(history) or receipt())
    turn(signed, sid, text)
    assert seen


def test_agent_nearby_default_is_ten_km_and_honors_explicit_radius(signed, setup):
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    for args, radius in [({'location': '116.4,39.9'}, 10000),
                         ({'location': '116.4,39.9', 'radius_m': 20000, 'view': 'map'}, 20000)]:
        result, card = agent._execute_tool('search_restaurants', args, owner, {})
        assert result['query']['radius_m'] == radius
        assert card['payload']['presentation']['preferred_view'] == args.get('view', 'list')


def test_presentation_reuse_obeys_tool_budget_and_never_uses_old_scope(signed, setup, monkeypatch):
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    payload = restaurant_service.search(owner, city='北京')
    history = [{'role': 'user', 'content': '打开地图'}]
    context = {'type': 'restaurant_list', 'payload': payload}
    reply = agent.run_turn(owner, [], {}, history, budget={'deadline': time.monotonic() + 10, 'tool_calls_used': 6}, restaurant_context=context)
    assert reply['exceeded'] == 'tool_budget'
    payload['scope']['version'] = 'old-test-scope'
    seen = []
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda history: seen.append(history) or receipt())
    reply = agent.run_turn(owner, [], {}, history, restaurant_context=context)
    assert not reply['cards'] and seen

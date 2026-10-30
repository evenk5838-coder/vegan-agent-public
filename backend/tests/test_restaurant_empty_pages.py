"""Agent-only empty-page continuation. All services/models are offline doubles."""
import copy
import json
import time

import pytest

from app import agent, core, providers


def page(number=1, *, items=None, has_more=True, next_page=None, query=None):
    q = {'mode': 'text', 'city': '北京', 'location': None, 'radius_m': None,
         'keywords': '素食', 'page': number, 'page_size': 20}
    q.update(query or {})
    return {'query': q, 'items': items or [],
            'pagination': {'page': number, 'page_size': q['page_size'],
                           'has_more': has_more,
                           'next_page': number + 1 if has_more and next_page is None else next_page,
                           'raw_returned': 20 if has_more else 0,
                           'returned': len(items or []), 'filtered_out': 20 if not items else 0,
                           'reached_window_end': False,
                           'end_reason': None if has_more else 'empty_page'},
            'presentation': {'preferred_view': 'list', 'reused_from_conversation': False}}


def response(calls):
    return {'text': '', 'needs_input': False, 'input_prompt': '', 'tool_calls': calls}


def call(name='search_restaurants', args=None, identifier='restaurant-call'):
    args = {'city': '北京'} if args is None else args
    return {'id': identifier, 'name': name, 'arguments': args,
            'raw_arguments': json.dumps(args, ensure_ascii=False)}


@pytest.fixture
def harness(monkeypatch):
    state = {'calls': [], 'models': [], 'reserved': []}
    monkeypatch.setattr(providers, 'reserve_call', lambda *_: None)

    def setup(pages, *, args=None, text='找北京的素食餐厅', extra_calls=None, replies=None,
              budget=None, on_execute=None, attachments=None):
        responses = iter(replies or [response([call(args=args), *(extra_calls or [])])])
        queue = iter(pages)
        def model(history):
            state['models'].append(copy.deepcopy(history))
            try:
                return next(responses)
            except StopIteration:
                pytest.fail('empty-page scanning must not purchase another model response')
        monkeypatch.setattr(providers, 'generate_chat_reply', model)
        def execute(name, arguments, owner, preferences):
            state['calls'].append((name, copy.deepcopy(arguments)))
            if name != 'search_restaurants':
                payload = {'items': [], 'test': 'other-feature'}
                return payload, {'id': 'other-card', 'type': 'food_inventory', 'payload': payload}
            payload = next(queue)
            if isinstance(payload, Exception):
                raise payload
            payload = copy.deepcopy(payload)
            if on_execute:
                on_execute(len([item for item in state['calls'] if item[0] == 'search_restaurants']))
            return payload, {'id': 'restaurant-page-' + str(payload.get('query', {}).get('page')),
                             'type': 'restaurant_list', 'payload': payload}
        monkeypatch.setattr(agent, '_execute_tool', execute)
        reply = agent.run_turn('synthetic-owner', attachments or [], {},
                               [{'role': 'user', 'content': text}],
                               budget=budget, on_tool_reserved=state['reserved'].append)
        return reply
    return setup, state


def restaurant_cards(reply):
    return [card for card in reply['cards'] if card['type'] == 'restaurant_list']


def test_filtered_first_page_advances_once_and_returns_first_nonempty_page(harness):
    run, state = harness
    reply = run([page(), page(2, items=[{'id': 'vegetarian-2', 'name': '测试素食店'}])])
    assert reply['restaurant_guard']['status'] == 'ok'
    assert len(restaurant_cards(reply)) == 1
    payload = restaurant_cards(reply)[0]['payload']
    assert payload['query']['page'] == 2 and payload['pagination']['next_page'] == 3
    assert reply['places'] == payload['items']
    assert [args.get('page', 1) for _, args in state['calls']] == [1, 2]
    assert state['reserved'] == [1, 2] and len(state['models']) == 1


def test_first_nonempty_page_is_not_automatically_expanded(harness):
    run, state = harness
    reply = run([page(items=[{'id': 'one', 'name': '测试素食餐厅'}])])
    assert reply['restaurant_guard']['status'] == 'ok'
    assert len(state['calls']) == 1 and state['reserved'] == [1]


def test_three_empty_pages_stop_but_truthfully_preserve_the_next_page(harness):
    run, state = harness
    reply = run([page(), page(2), page(3), page(4)])
    assert reply['restaurant_guard']['status'] == 'empty'
    assert len(state['calls']) == 3 and state['reserved'] == [1, 2, 3]
    assert len(state['models']) == 1 and len(restaurant_cards(reply)) == 1
    payload = restaurant_cards(reply)[0]['payload']
    assert payload['query']['page'] == 3 and payload['pagination']['next_page'] == 4
    assert '检查3页' in reply['text'] and '仍有后续页' in reply['text']
    assert '这个范围里没有查到' not in reply['text']


@pytest.mark.parametrize('mutation', ['no_pagination', 'no_has_more', 'terminal'])
def test_missing_pagination_or_terminal_page_never_auto_queries(harness, mutation):
    run, state = harness
    first = page()
    if mutation == 'no_pagination':
        first.pop('pagination')
    elif mutation == 'no_has_more':
        first['pagination'].pop('has_more')
    else:
        first['pagination'].update(has_more=False, next_page=None, end_reason='no_more_matches')
    reply = run([first])
    assert len(state['calls']) == 1
    assert '这个范围里没有查到' not in reply['text']
    assert ('没有后续页' if mutation == 'terminal' else '尚不能确认') in reply['text']


def test_window_end_is_not_presented_as_all_local_restaurants_checked(harness):
    run, state = harness
    first = page(10, has_more=False)
    first['pagination'].update(reached_window_end=True, end_reason='reached_window')
    reply = run([first], args={'city': '北京', 'page': 10})
    assert len(state['calls']) == 1
    assert '检索窗口' in reply['text'] and '全部店铺' in reply['text']


@pytest.mark.parametrize('next_value', [True, 1, 0, 3, -1, 101, 10000, 2.0, '2'])
def test_invalid_or_looping_next_page_never_gets_requested(harness, next_value):
    run, state = harness
    first = page()
    first['pagination']['next_page'] = next_value
    reply = run([first])
    assert len(state['calls']) == 1
    assert '分页信息不一致' in reply['text']


def test_out_of_window_next_page_is_stopped_before_another_service_call(harness):
    run, state = harness
    first = page(10)
    reply = run([first], args={'city': '北京', 'page': 10})
    assert len(state['calls']) == 1 and '分页信息不一致' in reply['text']


def test_subsequent_page_uses_effective_query_not_model_omissions(harness):
    run, state = harness
    query = {'mode': 'around', 'city': None, 'location': '116.4,39.9',
             'radius_m': 20000, 'keywords': '植物料理', 'page_size': 7}
    first = page(query=query)
    first['presentation']['preferred_view'] = 'map'
    second = page(2, items=[{'id': 'two', 'name': '植物料理素食店'}], query=query)
    reply = run([first, second], args={'location': '116.4,39.9'},
                text='我的本次位置是116.4,39.9（GCJ-02）。找我附近的素食餐厅')
    args = state['calls'][1][1]
    assert args == {'location': '116.4,39.9', 'city': None, 'radius_m': 20000,
                    'keywords': '植物料理', 'page_size': 7, 'view': 'map', 'page': 2}
    assert reply['restaurant_guard']['status'] == 'ok'


@pytest.mark.parametrize('code,status', [('UPSTREAM_FAILED', 502), ('UPSTREAM_RATE_LIMITED', 429),
                                         ('QUOTA_EXCEEDED', 429), ('GLOBAL_QUOTA_EXCEEDED', 429)])
def test_failed_next_page_is_failed_not_empty_and_retains_last_successful_card(harness, code, status):
    run, state = harness
    reply = run([page(), core.AppError(code, 'synthetic next-page failure', status)])
    assert len(state['calls']) == 2 and len(state['models']) == 1
    assert reply['restaurant_guard']['status'] == 'failed'
    assert reply['restaurant_guard']['error_code'] == code
    assert restaurant_cards(reply)[0]['payload']['query']['page'] == 1
    assert '这个范围里没有查到' not in reply['text']


def test_tool_budget_reserves_each_page_and_preserves_completed_empty_card(harness):
    run, state = harness
    budget = {'deadline': time.monotonic() + 30, 'tool_calls_used': 5}
    reply = run([page()], budget=budget)
    assert reply['exceeded'] == 'tool_budget'
    assert len(state['calls']) == 1 and state['reserved'] == [6]
    assert restaurant_cards(reply)[0]['payload']['query']['page'] == 1


@pytest.mark.parametrize('late_page', [1, 2])
def test_result_returning_after_deadline_is_preserved_but_not_declared_success(harness, monkeypatch, late_page):
    run, state = harness
    clock = [0]
    monkeypatch.setattr(agent.time, 'monotonic', lambda: clock[0])
    def advance(number):
        if number == late_page:
            clock[0] = 101
    reply = run([page(), page(2, items=[{'id': 'two', 'name': '素食店'}])],
                budget={'deadline': 100, 'tool_calls_used': 0}, on_execute=advance)
    assert reply['exceeded'] == 'turn_budget'
    assert len(state['calls']) == late_page
    assert restaurant_cards(reply)[0]['payload']['query']['page'] == late_page


def test_other_feature_cards_survive_empty_card_replacement(harness):
    run, _ = harness
    attachment = {'id': 'existing', 'type': 'ingredient_result', 'payload': {}}
    reply = run([page(), page(2, items=[{'id': 'two', 'name': '素食店'}])], attachments=[attachment])
    assert reply['cards'][0] == attachment
    assert len(restaurant_cards(reply)) == 1


def test_mixed_native_roundtrip_keeps_original_tool_id_and_returns_only_final_page(harness):
    run, state = harness
    replies = [response([call(identifier='native-restaurant'), call('get_food_inventory', {}, 'native-food')]),
               response([])]
    reply = run([page(), page(2, items=[{'id': 'two', 'name': '测试素食店'}])],
                text='找北京素食餐厅，另外看看冰箱', replies=replies)
    tool_messages = [row for row in state['models'][1] if row['role'] == 'tool']
    assert [row['tool_call_id'] for row in tool_messages] == ['native-restaurant', 'native-food']
    result = json.loads(tool_messages[0]['content'])[0]
    assert result['result']['query']['page'] == 2
    assert len(restaurant_cards(reply)) == 1
    assert any(card['type'] == 'food_inventory' for card in reply['cards'])
    assert state['reserved'] == [1, 2, 3]


def test_mixed_failed_query_is_not_retried_even_with_a_new_call_id_and_page(harness):
    run, state = harness
    replies = [response([call(identifier='first'), call('get_food_inventory', {}, 'food')]),
               response([call(args={'city': '北京', 'page': 2}, identifier='retry')])]
    reply = run([page(), core.AppError('UPSTREAM_FAILED', 'synthetic failure', 502)],
                text='找北京的素食餐厅，另外看看冰箱', replies=replies)
    assert len([row for row in state['calls'] if row[0] == 'search_restaurants']) == 2
    assert len(state['models']) == 2
    assert state['reserved'] == [1, 2, 3]
    assert reply['restaurant_guard']['status'] == 'failed'
    assert reply['restaurant_guard']['error_code'] == 'UPSTREAM_FAILED'
    assert any(card['type'] == 'food_inventory' for card in reply['cards'])


@pytest.mark.parametrize('page_value,size_value', [('1', '20'), ('', ''), (None, None)])
def test_supported_page_arguments_are_normalized_before_continuing(harness, page_value, size_value):
    run, state = harness
    reply = run([page(), page(2, items=[{'id': 'next', 'name': '测试素食餐厅'}])],
                args={'city': '北京', 'page': page_value, 'page_size': size_value})
    assert reply['restaurant_guard']['status'] == 'ok'
    assert len(state['calls']) == 2
    assert state['calls'][1][1]['page'] == 2
    assert state['calls'][1][1]['page_size'] == 20


def test_mismatched_page_size_stops_before_spending_on_another_page(harness):
    run, state = harness
    reply = run([page()], args={'city': '北京', 'page_size': 25})
    assert len(state['calls']) == 1
    assert '分页信息不一致' in reply['text']


def test_mixed_model_cannot_restart_the_same_scan_after_the_three_page_limit(harness):
    run, state = harness
    replies = [response([call(identifier='first'), call('get_food_inventory', {}, 'food')]),
               response([call(args={'city': '北京', 'page': 4}, identifier='restart')])]
    reply = run([page(), page(2), page(3)], text='找北京的素食餐厅，另外看看冰箱', replies=replies)
    searches = [row for row in state['calls'] if row[0] == 'search_restaurants']
    assert len(searches) == 3
    assert state['reserved'] == [1, 2, 3, 4]  # Three pages, one separate inventory read.
    assert len(restaurant_cards(reply)) == 1
    assert restaurant_cards(reply)[0]['payload']['query']['page'] == 3
    assert '已检查3页' in reply['text'] and '仍有后续页' in reply['text']
    assert any(card['type'] == 'food_inventory' for card in reply['cards'])

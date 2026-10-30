"""Empty recipe retrieval must stop before consuming the six-tool budget.

The real provider serializer runs against intercepted HTTP responses; the
database is the normal isolated PostgreSQL fixture, never the user's library.
"""
import json

from app import agent, worker
from test_tool_protocol import capture_completions, final_response, tool_call_response


def search(call_id, query):
    return tool_call_response(call_id, 'search_recipes', {'query': query})


def test_same_empty_search_with_new_id_is_not_executed_again(signed, monkeypatch):
    calls = capture_completions(monkeypatch, [search('first', '豆类'), search('second', '豆类')])
    budget = {'deadline': 9_999_999_999.0, 'tool_calls_used': 0}
    reserved = []
    reply = agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': '推荐一道含豆类的蔬食菜谱'}],
                           budget, on_tool_reserved=reserved.append)
    assert len(calls) == 2
    assert reserved == [1] and budget['tool_calls_used'] == 1
    assert 'exceeded' not in reply and '没有从菜谱库找到' in reply['text']
    assert len(reply['cards']) == 1
    assert reply['cards'][0]['payload']['search_status'] == 'no_match'
    # The first real tool result still uses the original upstream ID.
    assert any(message.get('tool_call_id') == 'first'
               for message in calls[1]['body']['messages'])


def test_empty_query_can_refine_once_but_cannot_loop_over_synonyms(signed, monkeypatch):
    calls = capture_completions(monkeypatch, [search('one', '豆类'), search('two', '豆腐'),
                                              search('three', '豆子')])
    budget = {'deadline': 9_999_999_999.0, 'tool_calls_used': 0}
    reply = agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': '推荐豆类菜谱'}], budget)
    assert len(calls) == 3 and budget['tool_calls_used'] == 2
    assert len(reply['cards']) == 2 and 'exceeded' not in reply
    assert reply['needs_input'] is True


def test_successful_refinement_is_still_returned(monkeypatch):
    calls = capture_completions(monkeypatch, [search('one', '豆类'), search('two', '豆腐'),
                                              final_response('找到一份豆腐菜谱。')])
    monkeypatch.setattr(agent, '_search_recipes',
                        lambda owner, query, *args: [{'title': '豆腐'}] if query == '豆腐' else [])
    reply = agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': '推荐豆类菜谱'}])
    assert len(calls) == 3 and reply['text'] == '找到一份豆腐菜谱。'
    assert reply['cards'][-1]['payload']['items'] == [{'title': '豆腐'}]


def test_stopping_repeat_keeps_other_tools_in_same_batch(signed, monkeypatch):
    second = search('repeated', '豆类')
    second['choices'][0]['message']['tool_calls'].extend(
        tool_call_response('ingredients', 'evaluate_ingredients',
                           {'text': '大豆、水', 'complete': True})['choices'][0]['message']['tool_calls'])
    calls = capture_completions(monkeypatch, [search('one', '豆类'), second])
    budget = {'deadline': 9_999_999_999.0, 'tool_calls_used': 0}
    reply = agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': '查菜谱并判断大豆、水'}], budget)
    assert len(calls) == 2 and budget['tool_calls_used'] == 2
    assert [card['type'] for card in reply['cards']] == ['recipe_list', 'ingredient_result']


def test_empty_local_search_still_allows_web_candidates(monkeypatch):
    calls = capture_completions(monkeypatch, [search('local', '豆类'),
        tool_call_response('web', 'search_web_recipes', {'query': '豆类'}),
        final_response('这里只找到候选来源，尚不是完整教程。')])
    monkeypatch.setattr(agent, '_search_recipes', lambda *args: [])
    monkeypatch.setattr(agent, '_search_web_recipes', lambda *args:
                        ([{'title': '豆类候选', 'candidate_id': 'candidate'}], '待核对'))
    reply = agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': '推荐豆类菜谱'}])
    assert len(calls) == 3
    assert reply['cards'][-1]['type'] == 'recipe_candidates'
    tools = [message for message in calls[2]['body']['messages'] if message['role'] == 'tool']
    assert [message['tool_call_id'] for message in tools] == ['local', 'web']
    assert json.loads(tools[-1]['content'])[0]['ok'] is True


def test_worker_persists_honest_no_match_instead_of_budget_failure(signed, monkeypatch):
    calls = capture_completions(monkeypatch, [search('one', '豆类'), search('two', '豆类')])
    session = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn = signed.post(f'/api/v1/chat/sessions/{session}/messages',
                       json={'content': '推荐一道含豆类的蔬食菜谱'}).json()['turn_id']
    worker.process_once()
    result = signed.get(f'/api/v1/chat/sessions/{session}/turns/{turn}').json()
    assert result['status'] == 'succeeded' and result.get('error') is None
    assert '没有从菜谱库找到' in result['reply']['text'] and len(calls) == 2
    messages = signed.get(f'/api/v1/chat/sessions/{session}/messages').json()['items']
    assert messages[-1]['text'] == result['reply']['text']

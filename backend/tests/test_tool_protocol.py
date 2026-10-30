"""工具调用协议回归：校验真实出站报文结构与多轮往返，不只 mock 结果。

这里拦截最底层的 HTTP（providers.request_json），因此 providers.generate_chat_reply
与 agent.run_turn 的报文组装逻辑都会真实执行。
"""
import json

import httpx
import pytest

from app import agent, core, providers, worker


def capture_completions(monkeypatch, responses):
    """拦截上游 HTTP，返回记录到的请求体列表。responses 为依次返回的 dict 列表。"""
    calls = []
    queue = list(responses)

    def fake_request_json(method, url, **kwargs):
        calls.append({'url': url, 'body': kwargs.get('json'), 'headers': kwargs.get('headers')})
        if not queue:
            raise AssertionError('模型被调用了超出预期的次数')
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(providers, 'request_json', fake_request_json)
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    return calls


def tool_call_response(call_id='call_abc123', name='evaluate_ingredients', arguments=None):
    return {'choices': [{'message': {
        'role': 'assistant', 'content': None,
        'tool_calls': [{'id': call_id, 'type': 'function',
                        'function': {'name': name,
                                     'arguments': json.dumps(arguments or {'text': '水、大豆、白砂糖、洋葱',
                                                                           'complete': True},
                                                             ensure_ascii=False)}}]}}]}


def final_response(text='三类结论如下。'):
    return {'choices': [{'message': {'role': 'assistant',
                                     'content': json.dumps({'text': text, 'needs_input': False,
                                                            'input_prompt': ''}, ensure_ascii=False)}}]}


# ---------- 出站报文结构 ----------
def test_outbound_request_declares_native_tools(monkeypatch):
    calls = capture_completions(monkeypatch, [final_response()])
    providers.generate_chat_reply([{'role': 'user', 'content': '你好'}])
    body = calls[0]['body']
    assert calls[0]['url'].endswith('/chat/completions')
    assert body['tool_choice'] == 'auto'
    assert body['response_format'] == {'type': 'json_object'}
    names = [t['function']['name'] for t in body['tools']]
    # 声明必须与执行白名单一致（只读工具也必须有正式 schema）
    from app.agent import TOOL_WHITELIST
    assert set(names) == TOOL_WHITELIST, (set(names) ^ TOOL_WHITELIST)
    assert 'set_food_reminder' not in names      # 正式修改改为待确认操作，不暴露给模型
    for tool in body['tools']:
        assert tool['type'] == 'function'
        assert tool['function']['parameters']['type'] == 'object'
        assert 'description' in tool['function']
    # 系统提示自动补齐在首位
    assert body['messages'][0]['role'] == 'system'
    assert body['messages'][-1] == {'role': 'user', 'content': '你好'}


def test_tool_result_roundtrip_uses_matching_tool_call_id(monkeypatch):
    """多轮往返：assistant.tool_calls → tool(tool_call_id) → 最终回复。"""
    calls = capture_completions(monkeypatch, [tool_call_response(), final_response()])
    reply = agent.run_turn('owner-1', [], {'diet': 'vegan', 'avoid_coriander': False},
                           [{'role': 'user', 'content': '配料是水、大豆、白砂糖、洋葱，请判断三类饮食适配'}],
                           budget={'deadline': 9_999_999_999.0, 'tool_calls_used': 0})
    assert reply['text'] == '三类结论如下。'

    second = calls[1]['body']['messages']
    roles = [m['role'] for m in second]
    assert roles[-2:] == ['assistant', 'tool']
    assistant_msg = second[-2]
    tool_msg = second[-1]
    # assistant 必须携带与模型返回一致的 tool_calls
    assert assistant_msg['tool_calls'][0]['id'] == 'call_abc123'
    assert assistant_msg['tool_calls'][0]['type'] == 'function'
    assert assistant_msg['tool_calls'][0]['function']['name'] == 'evaluate_ingredients'
    # tool 消息必须带同一个 tool_call_id 且内容是 JSON
    assert tool_msg['tool_call_id'] == 'call_abc123'
    assert json.loads(tool_msg['content'])[0]['ok'] is True
    # 不允许出现“没有 tool_call_id 的 tool 消息”
    for message in second:
        if message['role'] == 'tool':
            assert message.get('tool_call_id')


def test_multiple_tool_calls_each_get_own_id(monkeypatch):
    """一条回复里多个工具请求，各自回传独立 id，顺序与模型给出的一致。"""
    multi = {'choices': [{'message': {
        'role': 'assistant', 'content': None,
        'tool_calls': [
            {'id': 'call_1', 'type': 'function',
             'function': {'name': 'evaluate_ingredients', 'arguments': '{"text":"豆腐","complete":true}'}},
            {'id': 'call_2', 'type': 'function',
             'function': {'name': 'search_recipes', 'arguments': '{"query":"豆腐"}'}},
        ]}}]}
    calls = capture_completions(monkeypatch, [multi, final_response('完成。')])
    agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': 'x'}],
                   budget={'deadline': 9_999_999_999.0, 'tool_calls_used': 0})
    messages = calls[1]['body']['messages']
    tools = [m for m in messages if m['role'] == 'tool']
    assert [m['tool_call_id'] for m in tools] == ['call_1', 'call_2']
    assert [json.loads(m['content'])[0]['name'] for m in tools] == ['evaluate_ingredients', 'search_recipes']


def test_missing_tool_call_id_never_fabricates_tool_protocol(monkeypatch):
    """上游未给调用标识时：不伪造 tool_call_id，也不用 role="tool"，
    改为把工具结果作为数据块交回，保证出站报文合法。"""
    broken = {'choices': [{'message': {'role': 'assistant', 'content': None,
                                       'tool_calls': [{'type': 'function',
                                                       'function': {'name': 'evaluate_ingredients',
                                                                    'arguments': '{"text":"豆腐","complete":true}'}}]}}]}
    calls = capture_completions(monkeypatch, [broken, final_response('已按规则判断。')])
    reply = agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': 'x'}],
                           budget={'deadline': 9_999_999_999.0, 'tool_calls_used': 0})
    assert reply['text'] == '已按规则判断。'
    second = calls[1]['body']['messages']
    # 绝不允许出现 role=tool
    assert all(m['role'] != 'tool' for m in second)
    # 也不允许伪造 tool_call_id 或 assistant.tool_calls
    assert all('tool_call_id' not in m for m in second)
    assert all(not m.get('tool_calls') for m in second if m['role'] == 'assistant')
    # 工具结果通过带标记的数据块交回
    data_blocks = [m for m in second if m['role'] == 'user' and '工具结果' in str(m.get('content'))]
    assert data_blocks and 'evaluate_ingredients' in data_blocks[0]['content']
    # 出站仍声明 tools，供下一轮模型继续选择
    assert calls[1]['body']['tools'][0]['function']['name'] == 'evaluate_ingredients'


def test_malformed_arguments_json_is_reported_to_model_not_crashing(monkeypatch):
    bad_args = {'choices': [{'message': {
        'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'call_x', 'type': 'function',
                        'function': {'name': 'evaluate_ingredients', 'arguments': '{not-json'}}]}}]}
    calls = capture_completions(monkeypatch, [bad_args, final_response('参数有问题。')])
    agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': 'x'}],
                   budget={'deadline': 9_999_999_999.0, 'tool_calls_used': 0})
    tool_msg = [m for m in calls[1]['body']['messages'] if m['role'] == 'tool'][-1]
    entry = json.loads(tool_msg['content'])[0]
    assert entry['ok'] is False and entry['error'] == 'INVALID_ARGUMENTS'


def test_unknown_tool_from_model_is_rejected(monkeypatch):
    bad = {'choices': [{'message': {
        'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'call_z', 'type': 'function',
                        'function': {'name': 'delete_database', 'arguments': '{}'}}]}}]}
    calls = capture_completions(monkeypatch, [bad, final_response('无法执行。')])
    agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': 'x'}],
                   budget={'deadline': 9_999_999_999.0, 'tool_calls_used': 0})
    entry = json.loads([m for m in calls[1]['body']['messages'] if m['role'] == 'tool'][-1]['content'])[0]
    assert entry['error'] == 'UNKNOWN_TOOL'


# ---------- 错误分类：422 不重试，5xx 重试 ----------
def stub_http(monkeypatch, status, payload=None):
    """在 HTTP 层拦截，返回给定状态码，使 providers 的错误分类真实执行。"""
    attempts = []

    class StubClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kwargs):
            attempts.append({'method': method, 'url': url, 'json': kwargs.get('json')})
            request = httpx.Request(method, url)
            return httpx.Response(status, request=request, json=payload or {})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', StubClient)
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    return attempts, real_client


def test_422_is_not_retried_and_turn_fails_once(signed, monkeypatch):
    attempts, _ = stub_http(monkeypatch, 422,
                            {'error': {'message': 'messages[2]: missing field `tool_call_id`'}})
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'failed'
    assert turn['error']['code'] == 'UPSTREAM_REJECTED'
    assert len(attempts) == 1
    # 即使再次被调度也不重试
    worker.process_once()
    assert len(attempts) == 1
    # 对外错误脱敏：不含供应商字段名、地址或密钥
    assert 'tool_call_id' not in turn['error']['message']
    assert 'deepseek' not in turn['error']['message']
    assert 'test-only' not in json.dumps(turn)


def test_server_error_is_retried_then_fails(signed, monkeypatch):
    attempts, _ = stub_http(monkeypatch, 503)
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    for _ in range(4):
        with core.DB() as db:
            task = db.get(core.Job, turn_id)
            if task.status in ('queued', 'pending', 'running'):
                task.due = core.now()
            db.commit()
        worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'failed'
    assert turn['error']['code'] == 'UPSTREAM_FAILED'
    assert len(attempts) == 3  # 首次 + 额外最多 2 次重试


def test_rate_limit_not_retried(signed, monkeypatch):
    attempts, _ = stub_http(monkeypatch, 429)
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    worker.process_once()
    assert signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['error']['code'] == 'UPSTREAM_RATE_LIMITED'
    assert len(attempts) == 1


def test_error_classification_helper():
    assert providers.retryable('UPSTREAM_FAILED') is True
    assert providers.retryable('UPSTREAM_REJECTED') is False
    assert providers.retryable('UPSTREAM_RATE_LIMITED') is False
    assert providers.retryable('TURN_BUDGET_EXCEEDED') is False


# ---------- 约束保持 ----------
def test_protocol_change_keeps_budgets_and_usage(signed, monkeypatch):
    responses = [tool_call_response(f'call_{i}') for i in range(7)] + [final_response()]
    calls = capture_completions(monkeypatch, responses)
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'failed'
    assert turn['error']['code'] == 'TOOL_BUDGET_EXCEEDED'
    assert len([m for m in calls[1]['body']['messages'] if m['role'] == 'tool']) == 6
    with core.DB() as db:
        usage = [r for r in db.scalars(__import__('sqlalchemy').select(core.Record)
                                       .where(core.Record.kind == 'usage'))]
        assert sum(1 for r in usage if r.payload.get('operation') == 'chat') == 7


def test_turn_budget_stops_before_next_model_call(monkeypatch):
    import time as _time
    clock = {'v': 1_000_000.0}
    monkeypatch.setattr(_time, 'monotonic', lambda: clock['v'])
    calls = []

    def fake_request_json(method, url, **kwargs):
        calls.append(1)
        clock['v'] += 46
        return tool_call_response()

    monkeypatch.setattr(providers, 'request_json', fake_request_json)
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    reply = agent.run_turn('owner-1', [], {}, [{'role': 'user', 'content': 'x'}],
                           budget={'deadline': clock['v'] + 45, 'tool_calls_used': 0})
    assert reply['exceeded'] == 'turn_budget'
    assert len(calls) == 1

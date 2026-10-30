"""对话编排子阶段工程验证：工具白名单、固定规则复用、追问恢复、异常与权限。
所有 generate_chat_reply 均为脚本化 mock，不进行真实模型调用。"""
import json
import time as _time

from sqlalchemy import select

from app import agent, core, providers, worker


def chat_session(client):
    return client.post('/api/v1/chat/sessions', json={}).json()


def enable_chat(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def scripted_replies(monkeypatch, *replies):
    it = iter(replies)
    calls = []

    def fn(history):
        calls.append(history)
        try:
            return next(it)
        except StopIteration:
            return {'text': '结束。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}

    monkeypatch.setattr(providers, 'generate_chat_reply', fn)
    return calls


def tool_message(history):
    """汇总所有 tool 消息里的结果条目（协议：每条工具结果一条消息，含 tool_call_id）。"""
    merged = []
    for m in history:
        if m.get('role') == 'tool':
            merged.extend(json.loads(m['content']))
    return merged or None


def test_ingredient_tool_uses_fixed_rules(signed, monkeypatch):
    enable_chat(monkeypatch)
    calls = scripted_replies(monkeypatch,
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'evaluate_ingredients', 'arguments': {'text': '大蒜、豆腐', 'complete': True}}]},
        {'text': '按固定规则，纯净素因含蒜不符合，Vegan 符合。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '看看这个'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'succeeded'
    cards = turn['reply']['cards']
    ingredient = [c for c in cards if c['type'] == 'ingredient_result']
    assert len(ingredient) == 1
    result = ingredient[0]['payload']['result']
    assert result['results']['pure']['status'] == 'not_suitable'
    assert result['results']['vegan']['status'] == 'suitable'
    assert 'rules_version' in result
    assert tool_message(calls[-1]) is not None  # 工具结果已回喂模型


def test_clarification_recovery(signed, monkeypatch):
    enable_chat(monkeypatch)
    sid = chat_session(signed)['id']
    scripted_replies(monkeypatch,
        {'text': '请把配料表内容发给我。', 'needs_input': True, 'input_prompt': '配料表内容', 'tool_calls': []})
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '帮我判断'})
    worker.process_once()
    msgs = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    assistant = [m for m in msgs if m['role'] == 'assistant'][0]
    assert assistant['needs_input'] is True
    assert assistant['input_prompt'] == '配料表内容'
    # 第二轮：历史应包含上一轮追问文本，可在刷新/换设备后继续。
    calls = scripted_replies(monkeypatch,
        {'text': '好的，已按配料判断。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '小麦粉、植物油'})
    worker.process_once()
    assistant_texts = [m['content'] for m in calls[0] if m['role'] == 'assistant']
    assert '请把配料表内容发给我。' in assistant_texts


def test_unknown_tool_rejected(signed, monkeypatch):
    enable_chat(monkeypatch)
    calls = scripted_replies(monkeypatch,
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'delete_everything', 'arguments': {}}]},
        {'text': '无法执行该操作。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '删库'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'succeeded'
    results = tool_message(calls[-1])
    assert results[0]['error'] == 'UNKNOWN_TOOL'


def test_draft_foods_tool_creates_draft_only(signed, monkeypatch):
    """批量记食材：先建待确认草稿，不创建正式食材，不自动保存。"""
    enable_chat(monkeypatch)
    calls = scripted_replies(monkeypatch,
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'draft_foods', 'arguments': {'items': [{'name': '豆腐'}, {'name': '西兰花'}]}}]},
        {'text': '已整理成待确认草稿，请补充存放方式和日期。', 'needs_input': True,
         'input_prompt': '请确认每样食材的存放方式和日期', 'tool_calls': []})
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '记一批食材'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'succeeded'
    results = tool_message(calls[-1])
    assert results[0]['ok'] is True
    draft_cards = [c for c in turn['reply']['cards'] if c['type'] == 'food_draft']
    assert len(draft_cards) == 1
    assert draft_cards[0]['status'] == 'pending_confirmation'
    draft = draft_cards[0]['payload']
    assert len(draft['items']) == 2
    assert all(i['status'] == 'ready' for i in draft['items'])
    assert draft['items'][0]['missing'] == []
    with core.DB() as db:
        assert db.scalars(select(core.Record).where(core.Record.kind == 'food')).all() == []


def test_restaurant_tool_not_configured(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(core.settings, 'amap_api_key', '')
    calls = scripted_replies(monkeypatch,
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'search_restaurants', 'arguments': {'city': '上海'}}]},
        {'text': '餐厅服务未配置，请稍后再试。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '上海素食餐厅'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'succeeded'
    # A single restaurant request now ends with the server's controlled receipt,
    # without buying a second model call merely to report an unavailable tool.
    assert len(calls) == 1
    assert turn['reply']['restaurant_guard']['error_code'] == 'NOT_CONFIGURED'


def test_max_tool_calls_bounded(signed, monkeypatch):
    enable_chat(monkeypatch)
    count = {'n': 0}

    def always_tool(history):
        count['n'] += 1
        return {'text': '', 'needs_input': False, 'input_prompt': '',
                'tool_calls': [{'id': 'call_1', 'name': 'evaluate_ingredients', 'arguments': {}}]}

    monkeypatch.setattr(providers, 'generate_chat_reply', always_tool)
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '无限调用'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'failed'
    assert turn['error']['code'] == 'TOOL_BUDGET_EXCEEDED'
    assert count['n'] == 7  # 6 次工具执行 + 第 7 次模型请求的工具被预算拒绝


def test_single_reply_many_tools_capped_at_six(monkeypatch):
    executed = []
    responses = iter([
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': f'call_{i}', 'name': 'evaluate_ingredients',
                         'arguments': {'text': '豆腐', 'complete': True}} for i in range(7)]},
    ])
    monkeypatch.setattr(providers, 'reserve_call', lambda *a: None)
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(responses))
    monkeypatch.setattr(agent, '_execute_tool', lambda *a: (executed.append(a[0]), None))
    reply = agent.run_turn('review-owner', [], {}, [{'role': 'user', 'content': 'review'}])
    assert len(executed) == 6
    assert reply.get('exceeded') == 'tool_budget'


def test_expired_turn_does_not_start_another_model_call(monkeypatch):
    elapsed = {'value': 1000000.0, 'calls': 0}
    monkeypatch.setattr(_time, 'monotonic', lambda: elapsed['value'])
    monkeypatch.setattr(_time, 'perf_counter', lambda: elapsed['value'])
    monkeypatch.setattr(providers, 'reserve_call', lambda *a: None)
    monkeypatch.setattr(agent, '_execute_tool', lambda *a: ({}, None))

    def generate(history):
        elapsed['calls'] += 1
        if elapsed['calls'] == 1:
            elapsed['value'] += 46
            return {'text': '', 'needs_input': False, 'input_prompt': '',
                    'tool_calls': [{'id': 'call_1', 'name': 'evaluate_ingredients', 'arguments': {'text': '豆腐'}}]}
        return {'text': 'done', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}

    monkeypatch.setattr(providers, 'generate_chat_reply', generate)
    reply = agent.run_turn('review-owner', [], {}, [{'role': 'user', 'content': 'review'}])
    assert elapsed['calls'] == 1
    assert reply.get('exceeded') == 'turn_budget'


def test_exhausted_tool_budget_is_failed_not_succeeded(signed, monkeypatch):
    enable_chat(monkeypatch)
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: {'text': '', 'needs_input': False, 'input_prompt': '',
                                   'tool_calls': [{'id': 'call_1', 'name': 'evaluate_ingredients', 'arguments': {'text': '豆腐', 'complete': True}}]})
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'review'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'failed'
    assert turn['error']['code'] == 'TOOL_BUDGET_EXCEEDED'


def test_budget_persists_across_retry(signed, monkeypatch):
    enable_chat(monkeypatch)
    calls = {'n': 0}

    def gen(history):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'text': '', 'needs_input': False, 'input_prompt': '',
                    # A rejected tool still consumes budget but produces no
                    # saved progress. Only this case may retry the whole turn;
                    # successful cards are preserved by the closeout tests.
                    'tool_calls': [{'id': 'call_1', 'name': 'evaluate_ingredients', 'arguments': {}}]}
        raise core.AppError('UPSTREAM_FAILED', '模拟网络失败')

    monkeypatch.setattr(providers, 'generate_chat_reply', gen)
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    worker.process_once()
    with core.DB() as db:
        turn = db.get(core.Job, turn_id)
        assert turn.status == 'queued'  # UPSTREAM_FAILED 进入有限重试
        assert turn.payload['budget']['tool_calls_used'] == 1  # 已消耗预算持久化，不重置


def test_tool_usage_recorded(signed, monkeypatch):
    enable_chat(monkeypatch)
    scripted_replies(monkeypatch,
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'evaluate_ingredients', 'arguments': {'text': '小麦粉'}}]},
        {'text': '完成。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat_session(signed)['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '判断'})
    worker.process_once()
    with core.DB() as db:
        usage = [r for r in db.scalars(select(core.Record).where(core.Record.kind == 'usage'))]
        assert any(r.payload.get('operation') == 'chat' for r in usage)


def test_recipe_tool_owner_scoped(signed, monkeypatch):
    enable_chat(monkeypatch)
    signed.post('/api/v1/recipes', json={'title': '我的番茄豆腐', 'ingredients': '番茄、豆腐、橄榄油',
                                         'steps': '豆腐200g，番茄1个，炒熟。', 'meal': 'main', 'purpose': 'everyday',
                                         'cuisine': 'chinese', 'confirmed': True})
    scripted_replies(monkeypatch,
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'search_recipes', 'arguments': {'query': '番茄'}}]},
        {'text': '找到一道菜。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '找番茄菜谱'}).json()['turn_id']
    worker.process_once()
    cards = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['reply']['cards']
    recipe = [c for c in cards if c['type'] == 'recipe_list'][0]
    assert len(recipe['payload']['items']) == 1
    # 换账号看不到他人菜谱
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    scripted_replies(monkeypatch,
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'search_recipes', 'arguments': {'query': '番茄'}}]},
        {'text': '没有。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    sid2 = chat_session(signed)['id']
    turn_id2 = signed.post(f'/api/v1/chat/sessions/{sid2}/messages', json={'content': '找番茄菜谱'}).json()['turn_id']
    worker.process_once()
    cards2 = signed.get(f'/api/v1/chat/sessions/{sid2}/turns/{turn_id2}').json()['reply']['cards']
    recipe2 = [c for c in cards2 if c['type'] == 'recipe_list'][0]
    assert len(recipe2['payload']['items']) == 0


def test_late_final_reply_marked_turn_budget(monkeypatch):
    """待补齐A：最终回复在截止后返回，不得当作成功。"""
    elapsed = {'value': 1000000.0}
    monkeypatch.setattr(_time, 'monotonic', lambda: elapsed['value'])
    monkeypatch.setattr(providers, 'reserve_call', lambda *a: None)

    def delayed_final(history):
        elapsed['value'] += 46
        return {'text': '迟到的成功回复', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}

    monkeypatch.setattr(providers, 'generate_chat_reply', delayed_final)
    reply = agent.run_turn('review-owner', [], {}, [{'role': 'user', 'content': 'x'}])
    assert reply.get('exceeded') == 'turn_budget'
    assert reply['text'] == ''


def test_request_timeout_follows_remaining_budget(monkeypatch):
    """待补齐A：实际请求超时受剩余预算约束，而非固定 45 秒。"""
    now_value = {'v': 1000000.0}
    monkeypatch.setattr(_time, 'monotonic', lambda: now_value['v'])
    providers.set_deadline(now_value['v'] + 3)
    try:
        assert abs(providers.remaining_budget() - 3) < 0.001
        now_value['v'] += 10
        assert providers.remaining_budget() <= 0
    finally:
        providers.clear_deadline()
    # 无截止时退回单次默认上限
    assert providers.remaining_budget() == 45


def test_tool_reservation_persisted_before_execution(signed, monkeypatch):
    """待补齐B：工具执行前先落库预占；进程中断（BaseException）不丢计数。"""
    class SimulatedWorkerLoss(BaseException):
        pass

    enable_chat(monkeypatch)
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: {'text': '', 'needs_input': False, 'input_prompt': '',
                                   'tool_calls': [{'id': 'call_1', 'name': 'evaluate_ingredients', 'arguments': {'text': '豆腐', 'complete': True}}]})

    def interrupted_tool(*args):
        raise SimulatedWorkerLoss()

    monkeypatch.setattr(agent, '_execute_tool', interrupted_tool)
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    try:
        worker.process_once()
    except SimulatedWorkerLoss:
        pass
    with core.DB() as db:
        assert db.get(core.Job, turn_id).payload['budget']['tool_calls_used'] == 1
    worker.recover_and_clean()
    with core.DB() as db:
        turn = db.get(core.Job, turn_id)
        assert turn.status == 'queued'
        assert turn.payload['budget']['tool_calls_used'] == 1  # 重启不重置预算


def test_failed_turn_exposes_completed_cards(signed, monkeypatch):
    """待补齐C：失败 turn 通过接口暴露已完成的只读卡片，且不改成 succeeded。"""
    enable_chat(monkeypatch)
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: {'text': '', 'needs_input': False, 'input_prompt': '',
                                   'tool_calls': [{'id': 'call_1', 'name': 'evaluate_ingredients',
                                                   'arguments': {'text': '豆腐', 'complete': True}}] * 7})
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'failed'
    assert turn['error']['code'] == 'TOOL_BUDGET_EXCEEDED'
    assert turn['reply'] is None
    assert len(turn['cards']) == 6


def test_reserve_tool_call_rejects_cancelled_turn(signed, monkeypatch):
    """预占阶段发现 turn 已取消/会话已删时拒绝并抛 TURN_CANCELLED，不复活任务。"""
    enable_chat(monkeypatch)
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    with core.DB() as db:
        turn = db.get(core.Job, turn_id)
        turn.status = 'cancelled'
        db.commit()
    try:
        worker._reserve_tool_call(turn_id, turn.payload['budget']['deadline_wall'], 1)
        raise AssertionError('cancelled turn should be rejected')
    except core.AppError as e:
        assert e.code == 'TURN_CANCELLED'
    # 会话删除后同样拒绝
    with core.DB() as db:
        turn = db.get(core.Job, turn_id)
        turn.status = 'queued'
        db.delete(db.get(core.Record, sid))
        db.commit()
    try:
        worker._reserve_tool_call(turn_id, turn.payload['budget']['deadline_wall'], 1)
        raise AssertionError('deleted session should be rejected')
    except core.AppError as e:
        assert e.code == 'TURN_CANCELLED'


def test_request_deadline_isolated_between_threads():
    """请求截止按调用上下文隔离：一个线程不继承也不清除另一线程的截止。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    ready, other_done = Event(), Event()

    def chat_request():
        providers.set_deadline(_time.monotonic() + 3)
        ready.set()
        try:
            assert other_done.wait(2)
            return providers.remaining_budget()
        finally:
            providers.clear_deadline()

    def unrelated_request():
        assert ready.wait(2)
        try:
            inherited = providers.remaining_budget()
            providers.clear_deadline()
            return inherited
        finally:
            other_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        chat = pool.submit(chat_request)
        unrelated = pool.submit(unrelated_request)
        unrelated_budget = unrelated.result(timeout=3)
        chat_budget = chat.result(timeout=3)
    assert unrelated_budget == 45, f'无关请求继承了聊天截止：{unrelated_budget}'
    assert 0 < chat_budget <= 3, f'其他请求清除了聊天截止：{chat_budget}'

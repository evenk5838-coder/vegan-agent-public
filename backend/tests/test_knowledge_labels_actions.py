"""知识库三类标签 + 引用闭环 + 持久确认操作 验收（2026-10-26）。

全部隔离夹具 / 测试库发布；不写真实库、不外呼、不冒充正式内容。
"""
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import agent, core, knowledge, providers, rules, worker
from app.core import now
from app.recipes import diet_labels, evaluate_recipe



def enable(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


# ================= 三类标签：可重叠 / 三态 / 依据 =================
@pytest.mark.parametrize('ingredients,expected', [
    (['大米', '胡萝卜', '植物油'], {'pure': 'suitable', 'vegan': 'suitable', 'lacto_ovo': 'suitable'}),
    (['豆腐', '蒜', '盐'], {'pure': 'not_suitable', 'vegan': 'suitable', 'lacto_ovo': 'suitable'}),
    (['鸡蛋', '番茄', '盐'], {'pure': 'not_suitable', 'vegan': 'not_suitable', 'lacto_ovo': 'suitable'}),
    (['蚝油', '青菜'], {'pure': 'not_suitable', 'vegan': 'not_suitable', 'lacto_ovo': 'not_suitable'}),
    (['调味汁', '青菜'], {'pure': 'uncertain', 'vegan': 'uncertain', 'lacto_ovo': 'uncertain'}),
    (['奶粉', '神秘乳化剂'], {'pure': 'not_suitable', 'vegan': 'not_suitable', 'lacto_ovo': 'uncertain'}),
])
def test_three_labels_are_overlapping_and_three_state(ingredients, expected):
    row = evaluate_recipe({'ingredients': ingredients}, {'diet': 'vegan', 'avoid_coriander': False})
    assert {k: v['status'] for k, v in row['diet_labels'].items()} == expected
    # 每类都带依据：规则版本 + 判定范围
    for label in row['diet_labels'].values():
        assert label['basis']['rules_version'] == rules.VERSION
        assert label['basis']['scope']


def test_labels_carry_denied_and_unknown_basis():
    row = evaluate_recipe({'ingredients': ['蒜', '神秘乳化剂']}, {'diet': 'vegan', 'avoid_coriander': False})
    pure = row['diet_labels']['pure']
    assert pure['status'] == 'not_suitable'
    assert '蒜' in pure['denied']                       # 明确禁用项有依据
    assert row['diet_labels']['vegan']['status'] == 'uncertain'
    assert row['diet_labels']['vegan']['unknown']       # 未知项单列


def test_basic_ingredients_and_real_aliases_only():
    """只补基础食材与真正别名；复合产品一律不放行。"""
    for name in ('小米', '赤小豆', '红洋葱', '全脂牛奶', '椰奶', '香油', '鹰嘴豆粉'):
        group = rules.evaluate(name, True, [], False)['items'][0]['group']
        assert group != 'unknown', name
    # 复合/加工产品：名称不能证明成分
    for compound in ('酱油', '生抽', '豆瓣酱', '香草精', '无乳巧克力豆', '全麦面包',
                     '素蚝油', '蔬菜高汤', '植物奶', '意大利面', '黑巧克力'):
        assert rules.evaluate(compound, True, [], False)['items'][0]['group'] == 'compound', compound
    # 加工乳制品：含奶证据存在，但其他成分不明 → 蛋奶素待核实
    yogurt = rules.evaluate('希腊酸奶', True, [], False)
    assert yogurt['items'][0]['group'] == 'dairy_compound'
    assert yogurt['results']['pure']['status'] == 'not_suitable'
    assert yogurt['results']['vegan']['status'] == 'not_suitable'
    assert yogurt['results']['lacto_ovo']['status'] == 'uncertain'


def test_red_onion_is_pungent():
    r = rules.evaluate('红洋葱、豆腐', True, [], False)
    assert r['pungent'] == 'detected' and '红洋葱' in r['pungent_items']
    assert r['results']['pure']['status'] == 'not_suitable'
    assert r['results']['vegan']['status'] == 'suitable'


def test_batch_draft_recompute_report(tmp_path):
    from scripts.import_knowledge import preview_batch
    batch = tmp_path / 'synthetic-summaries.json'
    batch.write_text(json.dumps({'entries': [{'type': 'recipe', 'title': f'测试摘要{i}', 'source': f'https://example.org/{i}', 'ingredients': [['豆腐'], ['牛肉'], ['未知复合酱']][i % 3], 'steps': []} for i in range(28)]}, ensure_ascii=False))
    report = preview_batch(str(batch), recompute=True)
    assert report['total'] == 28 and report['importable'] == 28
    assert report['would_publish'] == 0                  # 全部草稿
    assert report['incomplete_or_summary'] == 28         # steps 为空 = 摘要
    assert report['rules_version'] == rules.VERSION
    # 三类结果齐备且可重叠（不是三选一）
    for row in report['rows']:
        assert set(row['diet_labels']) == {'pure', 'vegan', 'lacto_ovo'}
    assert any(r['diet_labels']['vegan'] == 'suitable' for r in report['rows'])
    assert any(r['diet_labels']['vegan'] == 'not_suitable' for r in report['rows'])
    # 仍有复合产品待核实，但数量显著下降
    assert 0 < report['unknown_count'] < 30


def test_page_and_agent_share_same_rule_output(signed, monkeypatch):
    knowledge.import_entries([{'type': 'recipe', 'title': '蒜香青菜', 'source': 'https://e.test/a',
                               'ingredients': ['青菜 200g', '蒜 2 瓣'],
                               'steps': ['炒熟'], 'status': 'published',
                               'meal': 'dinner', 'cuisine': 'chinese'}])
    with core.DB() as db:
        entry_id = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge')).payload['entry_id']
    knowledge.set_status(entry_id, 'published')
    enable(monkeypatch)
    page = signed.get('/api/v1/recipes?diet=pure').json()['items']
    assert page == []                                   # 纯净素被五辛排除
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    tool_rows = agent._search_recipes(owner, '蒜香', None, None, None, None,
                                      {'diet': 'vegan', 'avoid_coriander': False})
    assert [r['title'] for r in tool_rows] == ['蒜香青菜']
    assert tool_rows[0]['diet_labels']['pure']['status'] == 'not_suitable'
    assert tool_rows[0]['rule_ingredients'] == ['青菜', '蒜']


# ================= 工具注册一致性 =================
def test_declared_tools_equal_whitelist():
    declared = {t['function']['name'] for t in providers.CHAT_TOOLS}
    assert declared == agent.TOOL_WHITELIST
    assert 'set_food_reminder' not in declared           # 直接写库的工具不暴露
    for tool in providers.CHAT_TOOLS:
        params = tool['function']['parameters']
        assert params['type'] == 'object'
        assert 'description' in tool['function']


# ================= 引用闭环 =================
def _turn(signed, monkeypatch, replies, content):
    it = iter(replies)
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(it))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                          json={'content': content}).json()['turn_id']
    worker.process_once()
    return signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()


def _publish_nutrition():
    knowledge.import_entries([{'type': 'nutrition', 'title': '一般蔬食搭配',
                               'source': 'https://www.nhs.uk/vegan',
                               'body': '多样化植物食物', 'conditions': {'diet': 'vegan'},
                               'status': 'published'}])
    with core.DB() as db:
        entry_id = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge')).payload['entry_id']
    knowledge.set_status(entry_id, 'published')
    return entry_id


def test_citation_must_come_from_recall(signed, monkeypatch):
    enable(monkeypatch)
    entry_id = _publish_nutrition()
    replies = [
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'c1', 'name': 'search_knowledge', 'arguments': {'query': '蔬食'}}]},
        {'text': '搭配应多样化。', 'needs_input': False, 'input_prompt': '', 'tool_calls': [],
         'citations': [{'entry_id': entry_id}, {'entry_id': 'made-up-id'}]},
    ]
    reply = _turn(signed, monkeypatch, replies, '蔬食怎么搭配')['reply']
    assert [c['entry_id'] for c in reply['citations']] == [entry_id]   # 越界引用被丢弃
    assert reply['citations'][0]['source'].startswith('https://')


def test_no_recall_means_no_citation(signed, monkeypatch):
    enable(monkeypatch)
    _publish_nutrition()
    replies = [{'text': '据我了解……', 'needs_input': False, 'input_prompt': '', 'tool_calls': [],
                'citations': [{'entry_id': 'whatever'}]}]
    reply = _turn(signed, monkeypatch, replies, '随便问问')['reply']
    assert reply['citations'] == []


def test_knowledge_tool_returns_entry_id_and_source(signed, monkeypatch):
    enable(monkeypatch)
    entry_id = _publish_nutrition()
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    data, card = agent._execute_tool('search_knowledge', {'query': '蔬食'}, owner, {})
    assert data['items'][0]['entry_id'] == entry_id
    assert data['items'][0]['source'].startswith('https://')
    assert card['type'] == 'knowledge_result'


def test_draft_knowledge_not_retrievable(signed, monkeypatch):
    enable(monkeypatch)
    knowledge.import_entries([{'type': 'nutrition', 'title': '待审营养', 'source': 'https://e.test/x',
                               'body': '未审核内容'}])
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    data, _ = agent._execute_tool('search_knowledge', {'query': '营养'}, owner, {})
    assert data['items'] == []


# ================= 持久确认操作 =================
def _food(signed, **kw):
    body = {'name': '豆腐', 'storage': 'refrigerated', 'condition': 'raw', 'date_type': 'purchase',
            'started_at': (now() - timedelta(hours=2)).isoformat(),
            'deadline_source': 'user', 'expires_at': (now() + timedelta(hours=8)).isoformat()}
    body.update(kw)
    return signed.post('/api/v1/foods', json=body).json()


def test_action_requires_explicit_confirmation(signed):
    food = _food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used'}).json()
    assert action['status'] == 'pending'
    assert action['needs_confirmation'] is True
    # 未确认：数据未变
    assert signed.get('/api/v1/foods').json()['items'][0]['status'] == 'active'
    r = signed.post(f"/api/v1/actions/{action['record_id']}/confirm", json={'confirm_key': 'auto'})
    assert r.status_code == 422 or r.status_code == 400
    assert signed.get('/api/v1/foods').json()['items'][0]['status'] == 'active'
    # 确认后执行并取消提醒
    ok = signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                     json={'confirm_key': 'user_confirmed'}).json()
    assert ok['status'] == 'confirmed'
    assert signed.get('/api/v1/foods').json()['items'][0]['status'] == 'used'


def test_action_idempotent_and_cancellable(signed):
    food = _food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_reminder', 'food_id': food['id'],
        'remind_at': (now() + timedelta(hours=3)).isoformat()}).json()
    first = signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                        json={'confirm_key': 'user_confirmed', 'idempotency_key': 'k1'}).json()
    again = signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                        json={'confirm_key': 'user_confirmed', 'idempotency_key': 'k1'}).json()
    assert again['replayed'] is True and again['status'] == 'confirmed'
    assert first['results'] is not None

    other = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'discarded'}).json()
    cancelled = signed.delete(f"/api/v1/actions/{other['record_id']}").json()
    assert cancelled['status'] == 'cancelled'
    closed = signed.post(f"/api/v1/actions/{other['record_id']}/confirm",
                         json={'confirm_key': 'user_confirmed'})
    assert closed.status_code == 409
    assert closed.json()['error']['code'] == 'ACTION_CLOSED'
    # 取消的操作没有改动数据
    assert signed.get('/api/v1/foods').json()['items'][0]['status'] == 'active'


def test_action_version_conflict_blocks_execution(signed):
    food = _food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_condition', 'food_id': food['id'], 'storage': 'frozen',
        'temperature_c': -18}).json()
    # 确认前目标被改动 → 版本冲突，不执行
    body = {k: food[k] for k in ('name', 'storage', 'condition', 'date_type', 'started_at',
                                 'deadline_source', 'expires_at')}
    body.update(storage='ambient')
    signed.put(f"/api/v1/foods/{food['id']}", json=body)
    r = signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                    json={'confirm_key': 'user_confirmed'})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'ACTION_STALE'
    assert signed.get('/api/v1/foods').json()['items'][0]['storage'] == 'ambient'


def test_action_list_survives_and_expires(signed):
    food = _food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used'}).json()
    listed = signed.get('/api/v1/actions').json()
    assert [i['record_id'] for i in listed['items']] == [action['record_id']]
    with core.DB() as db:
        row = db.get(core.Record, action['record_id'])
        row.expires = now() - timedelta(seconds=1)
        db.commit()
    r = signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                    json={'confirm_key': 'user_confirmed'})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'ACTION_EXPIRED'


def test_action_is_account_scoped(signed):
    food = _food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used'}).json()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get(f"/api/v1/actions/{action['record_id']}").status_code == 404
    assert signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                       json={'confirm_key': 'user_confirmed'}).status_code == 404
    assert signed.get('/api/v1/actions').json()['items'] == []


def test_duplicate_proposal_reuses_pending(signed):
    food = _food(signed)
    body = {'action': 'food_status', 'food_id': food['id'], 'status': 'used'}
    first = signed.post('/api/v1/actions', json=body).json()
    second = signed.post('/api/v1/actions', json=body).json()
    assert first['record_id'] == second['record_id']      # 同一拟改值不重复入队
    assert signed.get('/api/v1/actions').json()['count'] == 1


# ================= 复审修复批次（2026-10-26 第二轮） =================
def test_restaurant_states_are_distinct(signed, monkeypatch):
    """缺地区 / 未查询 / 空结果 / 失败 必须区分，不能都说“没有结果”。"""
    enable(monkeypatch)

    def turn(content, replies):
        return _turn(signed, monkeypatch, replies, content)['reply']

    no_area = turn('附近有素食馆吗？', [{'text': '全聚德可以试试。', 'needs_input': False,
                                        'input_prompt': '', 'tool_calls': []}])
    assert no_area['restaurant_guard']['status'] == 'missing_area'
    assert '定位并查附近餐厅' in no_area['text'] and '全聚德' not in no_area['text']

    # 有地区但模型没调工具 → 服务端确定性补查（空结果）
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    monkeypatch.setattr(providers, 'request_json',
                        lambda *a, **k: {'status': '1', 'pois': [], 'count': '0'})
    area_not_queried = turn('北京有什么餐厅',
                            [{'text': '全聚德可以试试。', 'needs_input': False,
                              'input_prompt': '', 'tool_calls': []},
                             {'text': '这次没查到。', 'needs_input': False,
                              'input_prompt': '', 'tool_calls': []}])
    assert area_not_queried['restaurant_guard']['status'] == 'empty'
    assert '全聚德' not in area_not_queried['text']

    # 使前一轮成功空页缓存过期，再验证上游失败不会变成空结果。
    from datetime import timedelta
    with core.DB() as db:
        for record in db.scalars(select(core.Record).where(core.Record.kind == 'restaurant_cache')):
            record.expires = core.now() - timedelta(seconds=1)
        db.commit()
    # 工具失败：稳定错误码 + 可读文案，且不是空结果
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')

    def boom(*a, **k):
        raise core.AppError('UPSTREAM_RATE_LIMITED', '外部服务当前限流，请稍后再试。', 429)

    monkeypatch.setattr(providers, 'request_json', boom)
    failed = turn('北京有什么餐厅', [
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'c1', 'name': 'search_restaurants', 'arguments': {'city': '北京'}}]},
        {'text': '全聚德可以试试。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []}])
    assert failed['restaurant_guard']['status'] == 'failed'
    assert failed['restaurant_guard']['error_code'] == 'UPSTREAM_RATE_LIMITED'
    assert '限流' in failed['text'] and '没有查到' not in failed['text']


def test_meal_words_do_not_become_restaurant_turn(signed, monkeypatch):
    enable(monkeypatch)
    reply = _turn(signed, monkeypatch,
                  [{'text': '推荐晚餐吃番茄豆腐，配糙米。', 'needs_input': False,
                    'input_prompt': '', 'tool_calls': []}], '晚餐有什么推荐')['reply']
    assert reply['restaurant_guard']['status'] == 'not_restaurant'
    assert '番茄豆腐' in reply['text']


def test_context_followup_keeps_restaurant_guard(signed, monkeypatch):
    """上文在找店，下一句“那就找北京的”仍按餐厅处理，不被绕过。"""
    enable(monkeypatch)
    it = iter([
        {'text': '北京有很多店。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
        {'text': '全聚德可以试试。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
        {'text': '这次没查到。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
        {'text': '好吧。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(it))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '附近有素食馆吗？'})
    worker.process_once()
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                          json={'content': '那就找北京的'}).json()['turn_id']
    worker.process_once()
    reply = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['reply']
    assert reply['restaurant_guard']['restaurant_turn'] is True
    assert '全聚德' not in reply['text']


def test_compound_products_not_passed_by_name():
    for name in ('酱油', '生抽', '豆瓣酱', '香草精', '无乳巧克力豆', '全麦面包'):
        assert rules.evaluate(name, True, [], False)['items'][0]['group'] == 'compound', name
    yogurt = rules.evaluate('希腊酸奶', True, [], False)
    assert yogurt['results']['pure']['status'] == 'not_suitable'
    assert yogurt['results']['vegan']['status'] == 'not_suitable'
    assert yogurt['results']['lacto_ovo']['status'] == 'uncertain'   # 其他成分不明


def test_diet_definitions_in_prompt_not_merged():
    from app.providers import CHAT_SYSTEM_PROMPT
    assert '纯净素' in CHAT_SYSTEM_PROMPT and '蛋奶素' in CHAT_SYSTEM_PROMPT
    assert '互相独立' in CHAT_SYSTEM_PROMPT
    assert '不要合并或混称' in CHAT_SYSTEM_PROMPT


def test_condition_action_goes_through_reevaluation(signed):
    """改存放条件必须走 foods 更新服务：历史、重评、旧期限失效、旧提醒撤销。"""
    food = _food(signed, food_class='packaged', opened=False, temperature_c=3,
                 storage='refrigerated', package_note='未开封 0—4℃ 冷藏',
                 deadline_source='package')
    action = signed.post('/api/v1/actions', json={
        'action': 'food_condition', 'food_id': food['id'],
        'storage': 'ambient', 'temperature_c': 30}).json()
    out = signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                      json={'confirm_key': 'user_confirmed'}).json()
    result = out['results']['result']
    assert result['storage'] == 'ambient'
    assert len(result['storage_history']) == 1          # 保留历史
    assert result['valid_deadline_at'] is None          # 冷藏包装期不适用于常温30℃
    assert result['display_status'] == 'needs_review'


def test_fingerprint_includes_temperature_and_version(signed):
    food = _food(signed)
    a1 = signed.post('/api/v1/actions', json={'action': 'food_condition', 'food_id': food['id'],
                                              'storage': 'refrigerated', 'temperature_c': 2}).json()
    a2 = signed.post('/api/v1/actions', json={'action': 'food_condition', 'food_id': food['id'],
                                              'storage': 'refrigerated', 'temperature_c': 8}).json()
    assert a1['record_id'] != a2['record_id']            # 2℃/8℃ 不能复用同一张卡


def test_status_change_bumps_version(signed):
    food = _food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used'}).json()
    signed.post(f"/api/v1/actions/{action['record_id']}/confirm", json={'confirm_key': 'user_confirmed'})
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['version'] != food['version']           # 其他旧确认应失效


def test_stale_action_after_status_change(signed):
    food = _food(signed)
    cond = signed.post('/api/v1/actions', json={'action': 'food_condition', 'food_id': food['id'],
                                                'storage': 'frozen', 'temperature_c': -18}).json()
    status = signed.post('/api/v1/actions', json={'action': 'food_status', 'food_id': food['id'],
                                                  'status': 'used'}).json()
    signed.post(f"/api/v1/actions/{status['record_id']}/confirm", json={'confirm_key': 'user_confirmed'})
    r = signed.post(f"/api/v1/actions/{cond['record_id']}/confirm", json={'confirm_key': 'user_confirmed'})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'ACTION_STALE'


def test_action_proposal_validation_shared(signed):
    food = _food(signed)
    # 无时区提醒时间：HTTP 与工具都必须 422
    r = signed.post('/api/v1/actions', json={'action': 'food_reminder', 'food_id': food['id'],
                                             'remind_at': '2026-10-27T09:00'})
    assert r.status_code == 422
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    with pytest.raises(core.AppError) as exc:
        agent._execute_tool('propose_food_action',
                            {'food_id': food['id'], 'action': 'food_reminder',
                             'remind_at': '2026-10-27T09:00'}, owner, {})
    assert exc.value.code == 'INVALID_ARGUMENTS'


def test_citations_persisted_and_readable(signed, monkeypatch):
    """引用随助手消息落库，刷新/换设备读回仍在。"""
    enable(monkeypatch)
    entry_id = _publish_nutrition()
    replies = [
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'c1', 'name': 'search_knowledge', 'arguments': {'query': '蔬食'}}]},
        {'text': '搭配要多样。', 'needs_input': False, 'input_prompt': '', 'tool_calls': [],
         'citations': [{'entry_id': entry_id}, {'entry_id': 'fake-id'}]},
    ]
    it = iter(replies)
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(it))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '蔬食怎么搭配'})
    worker.process_once()
    msgs = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    assistant = [m for m in msgs if m['role'] == 'assistant'][0]
    assert [c['entry_id'] for c in assistant['citations']] == [entry_id]   # 伪造被丢弃
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'chat_message',
                                                  core.Record.payload['role'].as_string() == 'assistant'))
        assert row.payload['citations'][0]['entry_id'] == entry_id


# ================= 并发与中断（真实数据库 + 事务） =================
def _food_for_actions(signed):
    return signed.post('/api/v1/foods', json={
        'name': '豆腐', 'food_class': 'fresh', 'storage': 'refrigerated', 'condition': 'raw',
        'date_type': 'purchase', 'started_at': (now() - timedelta(hours=2)).isoformat(),
        'deadline_source': 'user', 'expires_at': (now() + timedelta(hours=8)).isoformat(),
        'remind_at': (now() + timedelta(hours=2)).isoformat()}).json()


def test_concurrent_confirm_executes_once(signed):
    """两个并发确认同一张卡：只执行一次，结果为一成功 + 一次重放/已关闭。"""
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from app.main import app as app_obj

    food = _food_for_actions(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used'}).json()
    cookies = dict(signed.cookies)

    def confirm():
        client = TestClient(app_obj)
        try:
            client.cookies.update(cookies)
            return client.post(f"/api/v1/actions/{action['record_id']}/confirm",
                               json={'confirm_key': 'user_confirmed', 'idempotency_key': 'same'})
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: confirm(), range(2)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    statuses = [r.json().get('status') for r in responses]
    assert statuses.count('confirmed') >= 1
    # 只执行一次：状态只被改一次，且历史/版本只递增一次
    with core.DB() as db:
        row = db.get(core.Record, action['record_id'])
        assert row.payload['status'] == 'confirmed'
    food_after = signed.get('/api/v1/foods').json()['items'][0]
    assert food_after['status'] == 'used'


def test_concurrent_two_actions_on_same_food_only_one_wins(signed):
    """同一食材两张待确认卡并发确认：一张成功，另一张按版本冲突失效。"""
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from app.main import app as app_obj

    food = _food_for_actions(signed)
    a1 = signed.post('/api/v1/actions', json={
        'action': 'food_condition', 'food_id': food['id'], 'storage': 'frozen',
        'temperature_c': -18}).json()
    a2 = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'discarded'}).json()
    cookies = dict(signed.cookies)

    def confirm(aid):
        client = TestClient(app_obj)
        try:
            client.cookies.update(cookies)
            return client.post(f'/api/v1/actions/{aid}/confirm',
                               json={'confirm_key': 'user_confirmed'})
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(confirm, [a1['record_id'], a2['record_id']]))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409], [(r.status_code, r.text) for r in responses]
    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()['error']['code'] == 'ACTION_STALE'


def test_interrupted_confirm_rolls_back_target_and_state(signed, monkeypatch):
    """执行中途异常：目标改动与确认终态一起回滚，不留下半完成状态。"""
    from app.services import foods as foods_module

    food = _food_for_actions(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_condition', 'food_id': food['id'], 'storage': 'frozen',
        'temperature_c': -18}).json()

    def boom(*args, **kwargs):
        raise RuntimeError('simulated interruption during commit')

    monkeypatch.setattr(foods_module, 'sync_reminders', boom)
    r = signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                    json={'confirm_key': 'user_confirmed'})
    assert r.status_code == 500                     # 服务失败，不是“成功”
    monkeypatch.undo()
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['storage'] == 'refrigerated'       # 目标未被改动
    assert after['version'] == food['version']      # 版本未变
    pending = signed.get(f"/api/v1/actions/{action['record_id']}").json()
    assert pending['status'] == 'pending'           # 仍是待确认，可重新确认


# ================= 第四轮收尾：协议配对 / 原子事务 / 并发锁 / 地区提取 =================
def test_server_routed_tool_call_has_matching_assistant_message(signed, monkeypatch):
    """混合问题补查后仍需继续模型时，assistant/tool 的调用标识必须配对。"""
    enable(monkeypatch)
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    monkeypatch.setattr(providers, 'request_json',
                        lambda *a, **k: {'status': '1', 'pois': [], 'count': '0'})
    captured = []
    replies = iter([
        {'text': '北京有不少店。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
        {'text': '这次没查到。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])

    def capture(messages):
        captured.append([dict(m) for m in messages])
        return next(replies)

    monkeypatch.setattr(providers, 'generate_chat_reply', capture)
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '北京有什么餐厅，顺便聊聊菜谱'})
    worker.process_once()

    second = captured[1]                      # 补查后的第二次模型请求
    tool_msgs = [m for m in second if m.get('role') == 'tool']
    assistant_with_calls = [m for m in second if m.get('tool_calls')]
    assert len(tool_msgs) == 1
    assert len(assistant_with_calls) == 1
    # 配对：同一 tool_call_id，且 assistant 里确实声明了该调用
    assert tool_msgs[0]['tool_call_id'] == assistant_with_calls[0]['tool_calls'][0]['id']
    assert assistant_with_calls[0]['tool_calls'][0]['function']['name'] == 'search_restaurants'


def test_foods_update_commit_false_is_truly_atomic(signed, monkeypatch):
    """commit=False 时不得内部提交：调用方回滚后，目标与历史都不应留下。"""
    from app.services import foods as foods_module

    food = _food_for_actions(signed)
    with core.DB() as db:
        record = foods_module.load(db, food['id'], db.scalar(select(core.User)).id)
        payload = {k: v for k, v in record.payload.items()
                   if k not in ('storage_history', 'version', 'needs_review')}
        payload['storage'] = 'frozen'
        foods_module.update(db, record, payload, {'storage'}, reconfirmed=False,
                            commit=False, lock=False)
        db.rollback()                                  # 模拟调用方失败
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['storage'] == 'refrigerated'          # 未被提交
    assert after['storage_history'] == []


def test_direct_food_update_and_confirm_are_mutually_exclusive(signed):
    """普通修改（PUT /foods）与确认操作并发：串行执行，不出现互相覆盖。"""
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from app.main import app as app_obj

    food = _food_for_actions(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_condition', 'food_id': food['id'], 'storage': 'frozen',
        'temperature_c': -18}).json()
    body = {k: food[k] for k in ('name', 'storage', 'condition', 'date_type', 'started_at',
                                 'deadline_source', 'expires_at')}
    body['temperature_c'] = 30
    cookies = dict(signed.cookies)

    def put_food():
        client = TestClient(app_obj)
        try:
            client.cookies.update(cookies)
            return client.put(f"/api/v1/foods/{food['id']}", json=body)
        finally:
            client.close()

    def confirm_action():
        client = TestClient(app_obj)
        try:
            client.cookies.update(cookies)
            return client.post(f"/api/v1/actions/{action['record_id']}/confirm",
                               json={'confirm_key': 'user_confirmed'})
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda fn: fn(), [put_food, confirm_action]))
    # 两者都结束；确认要么成功、要么因目标已变而明确失效
    assert results[0].status_code in (200, 409)
    assert results[1].status_code in (200, 409)
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['storage'] in ('frozen', 'refrigerated')
    # 不出现“确认成功但版本没变”的半完成状态
    if results[1].status_code == 200:
        assert after['version'] != food['version']


def test_delete_and_confirm_do_not_both_apply(signed):
    """删除与确认并发：最多一个生效，不会把已删记录再改写。"""
    food = _food_for_actions(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used'}).json()
    signed.delete(f"/api/v1/foods/{food['id']}")
    r = signed.post(f"/api/v1/actions/{action['record_id']}/confirm",
                    json={'confirm_key': 'user_confirmed'})
    assert r.status_code in (404, 409)               # 目标已删 → 明确失败
    assert signed.get('/api/v1/foods').json()['items'] == []


@pytest.mark.parametrize('text', [
    '给我几家好吃的素食餐厅', '推荐几家素食', '附近有几家素食馆', '帮我找几家', '哪家好吃',
])
def test_function_words_are_not_treated_as_area(signed, monkeypatch, text):
    """“几家/好吃/给我”等功能词不能被当地区并发起查询。"""
    enable(monkeypatch)
    called = []
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    monkeypatch.setattr(providers, 'request_json',
                        lambda *a, **k: called.append(k.get('params')) or
                        {'status': '1', 'pois': [], 'count': '0'})
    replies = iter([
        {'text': '你在哪个城市？', 'needs_input': True, 'input_prompt': '城市或地区',
         'tool_calls': []},
        {'text': '好的。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(replies))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                          json={'content': text}).json()['turn_id']
    worker.process_once()
    reply = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['reply']
    assert reply['restaurant_guard']['status'] == 'missing_area'   # 追问而不是乱查
    assert called == []                                            # 没有发起查询


def test_real_area_still_routes(signed, monkeypatch):
    """有真实地区时仍然会补查（避免上一条修复把正常场景也挡住）。"""
    enable(monkeypatch)
    called = []
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    monkeypatch.setattr(providers, 'request_json',
                        lambda *a, **k: called.append(k.get('params')) or
                        {'status': '1', 'pois': [], 'count': '0'})
    replies = iter([
        {'text': '好。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
        {'text': '没查到。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(replies))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '厦门有什么素食餐厅'})
    worker.process_once()
    assert called, '厦门应触发补查'
    assert called[0].get('city') == '厦门'

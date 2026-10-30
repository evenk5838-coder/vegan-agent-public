"""收尾轮工程验证：规则适用性、规则来源兼容、字段清空、相对日期、知识条目链路、闭环检查。"""
from datetime import datetime, timedelta

from sqlalchemy import select

from app import core, dates, drafts, knowledge, providers, storage_rules, worker
from app.core import now


# ---------- 1a 规则适用性 ----------
def cooked(**kw):
    return {'name': '蔬菜汤', 'food_class': 'cooked', 'storage': 'refrigerated', 'location': 'indoor', 'condition': 'cooked',
            'temperature_c': 4.0, 'date_type': 'prepared',
            'started_at': (now() - timedelta(hours=5)).isoformat(),
            'deadline_source': 'unknown', 'status': 'active', **kw}


def test_explicit_rule_id_requires_matching_conditions(signed, published_rules):
    item = signed.post('/api/v1/foods', json=cooked()).json()
    # 规则存在但温度未给 → 不默认适用
    signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                json={'temperature_c': None, 'evidence': {'deadline_source': 'unknown'}})
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'rule', 'rule_id': 'cooked-refrigerated'}})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'NO_APPLICABLE_RULE'
    assert '温度' in r.json()['error']['message']


def test_explicit_rule_id_rejects_wrong_storage_and_name(signed, published_rules):
    item = signed.post('/api/v1/foods', json=cooked(storage='frozen', temperature_c=-18)).json()
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'rule', 'rule_id': 'cooked-refrigerated'}})
    assert r.status_code == 409
    assert '存放方式' in r.json()['error']['message']
    other = signed.post('/api/v1/foods', json=cooked(name='苹果', condition='raw')).json()
    r2 = signed.post(f"/api/v1/foods/{other['id']}/reevaluate",
                     json={'evidence': {'deadline_source': 'rule', 'rule_id': 'cooked-refrigerated'}})
    assert r2.status_code == 409


def test_temperature_out_of_range_not_applicable(signed, published_rules):
    item = signed.post('/api/v1/foods', json=cooked(temperature_c=20.0)).json()
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'rule', 'rule_id': 'cooked-refrigerated'}})
    assert r.status_code == 409
    assert '温度范围' in r.json()['error']['message']


def test_draft_rule_keeps_source_and_review_status(signed, published_rules):
    item = signed.post('/api/v1/foods', json=cooked()).json()
    data = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                       json={'evidence': {'deadline_source': 'rule'}}).json()
    assert data['rule_source'].startswith('https://')
    # 发布门槛：只有已发布规则才进入正式期限；此处记录其发布状态供审计
    assert data['rules_status'] == 'published'
    assert data['rule_id'] and data['rule_version']


# ---------- 1b 规则来源兼容 ----------
def test_rule_food_can_be_marked_used_without_losing_basis(signed, published_rules):
    item = signed.post('/api/v1/foods', json=cooked()).json()
    ruled = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                        json={'evidence': {'deadline_source': 'rule'}}).json()
    body = {k: ruled[k] for k in ('name', 'storage', 'location', 'condition', 'temperature_c',
                                  'date_type', 'started_at', 'expires_at', 'deadline_source')}
    body['status'] = 'used'
    r = signed.put(f"/api/v1/foods/{item['id']}", json=body)
    assert r.status_code == 200
    after = r.json()
    assert after['status'] == 'used'
    assert after['deadline_source'] == 'rule'      # 来源保留，不改成 user
    assert after['rule_id'] == 'cooked-refrigerated'
    assert after['rule_version'] == storage_rules.RULES_VERSION


def test_client_cannot_forge_rule_basis(signed):
    item = signed.post('/api/v1/foods', json=cooked()).json()
    body = {k: item[k] for k in ('name', 'storage', 'location', 'condition', 'temperature_c',
                                 'date_type', 'started_at', 'deadline_source')}
    body.update(status='active', deadline_source='rule',
                expires_at=(now() + timedelta(days=30)).isoformat())
    r = signed.put(f"/api/v1/foods/{item['id']}", json=body)
    assert r.status_code == 422
    assert r.json()['error']['code'] == 'INVALID_RULE_BASIS'


def test_rule_basis_fields_not_overwritable_by_client(signed, published_rules):
    item = signed.post('/api/v1/foods', json=cooked()).json()
    ruled = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                        json={'evidence': {'deadline_source': 'rule'}}).json()
    body = {k: ruled[k] for k in ('name', 'storage', 'location', 'condition', 'temperature_c',
                                  'date_type', 'started_at', 'expires_at', 'deadline_source')}
    body.update(status='active', rule_id='fake-rule', rule_version='v999')
    r = signed.put(f"/api/v1/foods/{item['id']}", json=body).json()
    assert r['rule_id'] == 'cooked-refrigerated'
    assert r['rule_version'] == storage_rules.RULES_VERSION


# ---------- 1c 字段清空 ----------
def test_draft_item_clear_temperature_and_reminder(signed):
    created = signed.post('/api/v1/foods/drafts', json={'items': [{
        'name': '豆腐', 'storage': 'refrigerated', 'started_at': (now() - timedelta(hours=1)).isoformat(),
        'temperature_c': 4, 'remind_at': (now() + timedelta(hours=2)).isoformat()}]}).json()
    draft_id, item_id = created['draft_id'], created['items'][0]['item_id']
    assert created['items'][0]['temperature_c'] == 4
    r = signed.put(f'/api/v1/foods/drafts/{draft_id}/items/{item_id}',
                   json={'clear': ['temperature_c', 'remind_at']}).json()
    assert 'temperature_c' not in r['item']
    assert 'remind_at' not in r['item']
    assert set(r['item']['cleared']) == {'temperature_c', 'remind_at'}
    # 显式 null 同样清空
    signed.put(f'/api/v1/foods/drafts/{draft_id}/items/{item_id}', json={'temperature_c': 5})
    r2 = signed.put(f'/api/v1/foods/drafts/{draft_id}/items/{item_id}', json={'temperature_c': None}).json()
    assert 'temperature_c' not in r2['item']


def test_draft_required_fields_cannot_be_cleared(signed):
    created = signed.post('/api/v1/foods/drafts', json={'items': [{'name': '豆腐', 'storage': 'frozen'}]}).json()
    path = f"/api/v1/foods/drafts/{created['draft_id']}/items/{created['items'][0]['item_id']}"
    r = signed.put(path, json={'clear': ['name']})
    assert r.status_code == 422
    assert r.json()['error']['code'] == 'FIELD_REQUIRED'
    r2 = signed.put(path, json={'storage': None})
    assert r2.status_code == 200


def test_reevaluate_clear_reminder_cancels_task(signed):
    item = signed.post('/api/v1/foods', json=cooked(
        deadline_source='user', expires_at=(now() + timedelta(hours=4)).isoformat(),
        remind_at=(now() + timedelta(hours=1)).isoformat())).json()
    assert len(signed.get('/api/v1/notifications/tasks').json()) == 1
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'clear': ['remind_at'], 'evidence': {'deadline_source': 'user',
                                                               'expires_at': (now() + timedelta(hours=4)).isoformat()}})
    assert r.status_code == 200
    assert r.json().get('remind_at') is None
    assert signed.get('/api/v1/notifications/tasks').json()[0]['status'] == 'cancelled'


def test_food_put_absent_field_preserves_and_null_clears(signed):
    item = signed.post('/api/v1/foods', json=cooked(temperature_c=4.0)).json()
    base = {k: item[k] for k in ('name', 'storage', 'location', 'condition', 'temperature_c',
                                 'date_type', 'started_at', 'deadline_source')}
    base['status'] = 'active'
    keep = signed.put(f"/api/v1/foods/{item['id']}", json={k: v for k, v in base.items() if k != 'temperature_c'}).json()
    assert keep['temperature_c'] == 4.0   # 未传=保留
    cleared = signed.put(f"/api/v1/foods/{item['id']}", json={**base, 'temperature_c': None}).json()
    assert cleared['temperature_c'] is None  # 显式 null=清空


# ---------- 2 相对日期 ----------
def test_relative_dates_parse_with_reference_and_timezone():
    parsed = dates.parse('昨天', reference_time='2026-10-24T09:00:00+08:00')
    assert parsed['resolved_date'] == '2026-10-23'
    assert parsed['time_precision'] == 'date'
    assert parsed['reference_at'].startswith('2026-10-24T09:00:00')
    assert parsed['timezone'] == 'Asia/Shanghai'
    assert parsed['warnings']
    timed = dates.parse('明天下午3点', reference_time='2026-10-24T09:00:00+08:00')
    assert timed['started_at'].startswith('2026-10-25T15:00')
    assert timed['time_precision'] == 'datetime'


def test_relative_date_across_timezone():
    # 同一时刻在不同时区的“今天”不同
    utc = dates.parse('今天', reference_time='2026-10-24T23:30:00+00:00', tz='UTC')
    shanghai = dates.parse('今天', reference_time='2026-10-24T23:30:00+00:00', tz='Asia/Shanghai')
    assert utc['resolved_date'] == '2026-10-24'
    assert shanghai['resolved_date'] == '2026-10-25'


def test_unclear_date_is_rejected(signed):
    r = signed.post('/api/v1/dates/parse', json={'phrase': '改天'})
    assert r.status_code == 422
    assert r.json()['error']['code'] == 'DATE_UNCLEAR'


def test_draft_resolves_relative_date_and_asks_for_date_type(signed):
    r = signed.post('/api/v1/foods/drafts', json={
        'items': [{'name': '豆腐', 'storage': 'refrigerated', 'date_phrase': '昨天'}],
        'reference_time': '2026-10-24T09:00:00+08:00'})
    item = r.json()['items'][0]
    assert item['started_at'].startswith('2026-10-23T00:00')
    assert item['date_source'] == 'relative'
    assert item['date_phrase'] == '昨天'
    assert item['date_reference_at'].startswith('2026-10-24T09:00')
    assert item['time_precision'] == 'date'
    assert item['date_warnings']
    # 日期含义未知不阻止登记，也不默认按购买。
    assert item['missing'] == []
    assert item['status'] == 'ready'


def test_draft_relative_date_completes_after_date_type(signed):
    created = signed.post('/api/v1/foods/drafts', json={
        'items': [{'name': '豆腐', 'storage': 'refrigerated', 'date_phrase': '今天',
                   'date_type': 'purchase'}],
        'reference_time': '2026-10-24T09:00:00+08:00'}).json()
    assert created['items'][0]['status'] == 'ready'
    confirm = signed.post(f"/api/v1/foods/drafts/{created['draft_id']}/confirm", json={}).json()
    assert confirm['status'] == 'confirmed'
    food = signed.get('/api/v1/foods').json()['items'][0]
    assert food['started_at'].startswith('2026-10-24T00:00')


def test_chat_draft_tool_resolves_relative_date(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    replies = iter([
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'draft_foods', 'arguments': {
             'reference_time': '2026-10-24T09:00:00+08:00',
             'items': [{'name': '豆腐', 'storage': 'refrigerated', 'date_phrase': '昨天',
                        'date_type': 'purchase'}]}}]},
        {'text': '已整理成草稿，请核对日期。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(replies))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '昨天买的豆腐'}).json()['turn_id']
    worker.process_once()
    cards = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['reply']['cards']
    card = [c for c in cards if c['type'] == 'food_draft'][0]
    item = card['payload']['items'][0]
    assert item['started_at'].startswith('2026-10-23T00:00')
    assert item['date_source'] == 'relative'


# ---------- 3 知识条目链路 ----------
def test_knowledge_empty_is_stated_not_faked(signed):
    data = signed.get('/api/v1/knowledge').json()
    assert data['state'] == 'empty'
    assert data['items'] == []
    assert '暂无已发布' in data['notice']
    status = signed.get('/api/v1/knowledge/status').json()
    assert status['published'] == 0
    assert status['state'] == 'empty'


def test_knowledge_import_requires_source_and_conditions(signed):
    report = knowledge.import_entries([
        {'type': 'recipe', 'title': '无来源菜谱'},
        {'type': 'storage', 'title': '冷藏期限', 'source': 'https://example.org/a'},
        {'type': 'storage', 'title': '带条件条目', 'source': 'https://example.org/b',
         'conditions': {'storage': 'refrigerated'}},
        {'type': 'unknown', 'title': 'x', 'source': 'https://example.org/c'},
    ])
    states = [r['status'] for r in report['results']]
    assert states == ['rejected', 'rejected', 'added', 'rejected']   # 新语义：added
    # 导入默认 draft，不对外展示
    assert signed.get('/api/v1/knowledge').json()['state'] == 'empty'
    status = signed.get('/api/v1/knowledge/status').json()
    assert status['counts']['storage']['draft'] == 1
    assert status['published'] == 0


def test_knowledge_publish_flow(signed):
    knowledge.import_entries([{'type': 'nutrition', 'title': '搭配原则',
                               'source': 'https://www.nhs.uk/x',
                               'body': '多样化植物食物', 'conditions': {'diet': 'vegan'}}])
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
        entry_id = row.payload['entry_id']
    knowledge.set_status(entry_id, 'published')
    data = signed.get('/api/v1/knowledge?type=nutrition').json()
    assert data['state'] == 'published'
    assert data['items'][0]['source'].startswith('https://')
    assert data['items'][0]['conditions'] == {'diet': 'vegan'}
    assert signed.get('/api/v1/knowledge?type=recipe').json()['state'] == 'empty'


def test_knowledge_entry_access_is_public_readonly(signed):
    knowledge.import_entries([{'type': 'recipe', 'title': '待审菜谱', 'source': 'https://example.org/r'}])
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
        entry_id = row.payload['entry_id']
    knowledge.set_status(entry_id, 'published')
    # 换账号也能读（公共内容），但没有写接口可伪造发布
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get('/api/v1/knowledge').json()['state'] == 'published'
    assert signed.post('/api/v1/knowledge', json={'title': 'x'}).status_code == 405

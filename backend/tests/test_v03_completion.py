"""v0.3 全量收口：确定缺陷修复 + 对话读取能力验收。

覆盖工作单列的 5 个确定缺陷，以及新增读取工具/端点。
全部隔离夹具；真实外部调用在 conftest 被 HTTP 层阻断。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import agent, core, knowledge, providers, storage_rules, worker
from app.core import now
from app.services import food_advice

VEG_CODE, VEG_TYPE = '050120', '餐饮服务;中餐厅;中式素菜馆'


def enable(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'amap_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def run_turn(signed, monkeypatch, replies, content):
    it = iter(replies)
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(it))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                          json={'content': content}).json()['turn_id']
    worker.process_once()
    return signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()


def tool_reply(name, args, cid='c1'):
    return {'text': '', 'needs_input': False, 'input_prompt': '',
            'tool_calls': [{'id': cid, 'name': name, 'arguments': args}]}


def final(text='好的。'):
    return {'text': text, 'needs_input': False, 'input_prompt': '', 'tool_calls': []}


# ================= 缺陷 1：空卡 + "北京有什么推荐" =================
def test_empty_restaurant_card_with_generic_question_is_blocked(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(providers, 'request_json',
                        lambda *a, **k: {'status': '1', 'pois': [], 'count': '0'})
    data = run_turn(signed, monkeypatch,
                    [tool_reply('search_restaurants', {'city': '北京'}),
                     final('全聚德可以试试。')],
                    '北京有什么推荐')
    reply = data['reply']
    assert '全聚德' not in reply['text']
    assert reply['restaurant_guard']['restaurant_turn'] is True
    assert reply['restaurant_guard']['has_restaurant_result'] is False
    assert reply['places'] == []


def test_tool_failure_marks_restaurant_turn(signed, monkeypatch):
    enable(monkeypatch)

    def boom(*a, **k):
        raise core.AppError('UPSTREAM_FAILED', '外部服务未正常返回，请稍后重试。', 502)

    monkeypatch.setattr(providers, 'request_json', boom)
    data = run_turn(signed, monkeypatch,
                    [tool_reply('search_restaurants', {'city': '北京'}), final('就去全聚德吧。')],
                    '附近有啥吃的')
    reply = data['reply']
    assert '全聚德' not in reply['text']
    assert reply['restaurant_guard']['restaurant_attempted'] is True


def test_non_restaurant_question_still_untouched(signed, monkeypatch):
    enable(monkeypatch)
    data = run_turn(signed, monkeypatch, [final('推荐燕麦豆乳粥，搭配坚果可补充脂肪。')], '早餐吃什么')
    assert '燕麦豆乳粥' in data['reply']['text']
    assert data['reply']['restaurant_guard']['restaurant_turn'] is False


# ================= 缺陷 2：包装依据条件匹配 =================
def test_package_note_storage_and_temperature_must_match(signed):
    base = {'food_class': 'packaged', 'condition': 'unopened', 'date_type': 'production',
            'started_at': (now() - timedelta(days=1)).isoformat(),
            'expires_at': (now() + timedelta(days=3)).isoformat(),
            'package_note': '未开封 0—4℃ 冷藏'}
    ok = signed.post('/api/v1/foods/assess', json={**base, 'storage': 'refrigerated',
                                                   'opened': False, 'temperature_c': 3}).json()['suggestion']
    assert ok['basis']['type'] == 'package' and ok['action'] == 'date'
    bad = signed.post('/api/v1/foods/assess', json={**base, 'storage': 'ambient',
                                                    'opened': False, 'temperature_c': 30}).json()['suggestion']
    assert bad['basis']['type'] == 'none' and bad['reason'] == 'package_not_applicable'
    missing_temp = signed.post('/api/v1/foods/assess', json={**base, 'storage': 'refrigerated',
                                                             'opened': False}).json()['suggestion']
    assert missing_temp['basis']['type'] == 'none'


def test_invalid_or_naive_date_returns_422_not_500(signed):
    for bad in ('2026-10-26T09:00', 'not-a-date'):
        r = signed.post('/api/v1/foods/assess', json={
            'name': 'x', 'food_class': 'packaged', 'storage': 'refrigerated', 'opened': False,
            'date_type': 'production', 'started_at': (now() - timedelta(days=1)).isoformat(),
            'package_note': '未开封冷藏', 'expires_at': bad})
        assert r.status_code == 422
        assert r.json()['error']['code'] == 'INVALID_TIME'


def test_reevaluate_package_path_uses_same_check(signed):
    created = signed.post('/api/v1/foods', json={
        'name': '豆奶', 'food_class': 'packaged', 'storage': 'ambient', 'opened': False,
        'temperature_c': 30, 'date_type': 'production',
        'started_at': (now() - timedelta(days=1)).isoformat()}).json()
    r = signed.post(f"/api/v1/foods/{created['id']}/reevaluate", json={
        'evidence': {'deadline_source': 'package', 'package_note': '未开封 0—4℃ 冷藏',
                     'expires_at': (now() + timedelta(days=3)).isoformat()}})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'PACKAGE_NOT_APPLICABLE'
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['valid_deadline_at'] is None       # 不适用 → 不落新期限
    assert after['display_status'] == 'needs_review'


# ================= 缺陷 3：新字段进入写路径并触发重评 =================
def test_draft_item_accepts_food_class_and_opened(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{'name': '豆奶'}]}).json()
    item_id = draft['items'][0]['item_id']
    updated = signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/{item_id}",
                         json={'food_class': 'packaged', 'opened': False}).json()
    assert updated['item']['food_class'] == 'packaged'
    assert updated['item']['opened'] is False
    assert 'food_class' not in updated['item']['missing']


def test_opened_only_change_triggers_auto_reevaluation(signed):
    created = signed.post('/api/v1/foods', json={
        'name': '豆奶', 'food_class': 'packaged', 'storage': 'refrigerated', 'opened': False,
        'condition': 'raw', 'date_type': 'production',
        'started_at': (now() - timedelta(days=1)).isoformat(),
        'deadline_source': 'package', 'package_note': '未开封 0—4℃ 冷藏', 'temperature_c': 3,
        'expires_at': (now() + timedelta(days=3)).isoformat()}).json()
    body = {k: created[k] for k in ('name', 'food_class', 'storage', 'condition', 'temperature_c',
                                    'date_type', 'started_at', 'package_note', 'deadline_source')}
    body.update(opened=True, expires_at=created['expires_at'])
    updated = signed.put(f"/api/v1/foods/{created['id']}", json=body).json()
    # 开封后原“未开封”说明不再适用 → 清空有效期限并待核对
    assert updated['auto_reevaluated'] is True
    assert updated['valid_deadline_at'] is None
    assert updated['display_status'] == 'needs_review'


# ================= 缺陷 4：摘要范围一致（子集重试不误报） =================
def test_subset_retry_digest_is_not_false_conflict(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '豆腐', 'food_class': 'fresh', 'storage': 'refrigerated', 'date_type': 'purchase',
         'started_at': (now() - timedelta(hours=1)).isoformat()},
        {'name': '西兰花', 'storage': 'invalid'}]}).json()
    digest = draft['advice_digest']
    first = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm",
                        json={'advice_digest': digest, 'idempotency_key': 'k1'}).json()
    assert first['status'] == 'partially_saved'
    item_id = draft['items'][1]['item_id']
    signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/{item_id}",
               json={'food_class': 'fresh', 'storage': 'refrigerated',
                     'date_type': 'purchase',
                     'started_at': (now() - timedelta(hours=1)).isoformat()})
    fresh = signed.get(f"/api/v1/foods/drafts/{draft['draft_id']}").json()['advice_digest']
    retry = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm",
                        json={'item_ids': [item_id], 'advice_digest': fresh,
                              'idempotency_key': 'k2'}).json()
    assert retry['status'] == 'confirmed'
    assert len(signed.get('/api/v1/foods').json()['items']) == 2


# ================= 缺陷 5：确认把建议落到正式记录 =================
def test_confirm_applies_package_advice_to_formal_record(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{
        'name': '豆奶', 'food_class': 'packaged', 'condition': 'unopened', 'storage': 'refrigerated', 'opened': False,
        'temperature_c': 3, 'date_type': 'production',
        'started_at': (now() - timedelta(days=1)).isoformat(),
        'package_note': '未开封 0—4℃ 冷藏',
        'expires_at': (now() + timedelta(days=3)).isoformat(),
        'deadline_source': 'package'}]}).json()
    result = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    assert result['status'] == 'confirmed'
    food = signed.get('/api/v1/foods').json()['items'][0]
    assert food['deadline_source'] == 'package'
    assert food['valid_deadline_at'] is not None
    assert food['suggestion']['basis']['type'] == 'package'


def test_confirm_without_evidence_keeps_unknown_and_no_reminder(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{
        'name': '不明食材', 'food_class': 'fresh', 'storage': 'ambient', 'date_type': 'purchase',
        'started_at': (now() - timedelta(hours=1)).isoformat()}]}).json()
    signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={})
    food = signed.get('/api/v1/foods').json()['items'][0]
    assert food['valid_deadline_at'] is None and food['assessment_status'] == 'unknown'
    assert signed.get('/api/v1/notifications/tasks').json() == []


# ================= 新增读取工具 =================
def test_agent_reads_inventory_and_expiring(signed, monkeypatch):
    enable(monkeypatch)
    signed.post('/api/v1/foods', json={
        'name': '豆腐', 'food_class': 'fresh', 'storage': 'refrigerated', 'condition': 'raw',
        'date_type': 'purchase', 'started_at': (now() - timedelta(hours=2)).isoformat(),
        'deadline_source': 'user', 'expires_at': (now() + timedelta(hours=6)).isoformat()})
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    payload, card = agent._execute_tool('get_food_inventory', {'expiring_days': 1}, owner, {})
    assert [i['name'] for i in payload['items']] == ['豆腐']
    assert card['type'] == 'food_inventory'


def test_agent_reads_ingredient_history_and_reminders(signed, monkeypatch):
    enable(monkeypatch)
    signed.post('/api/v1/ingredients/evaluate', json={'text': '小麦粉、奶粉', 'complete': True})
    created = signed.post('/api/v1/foods', json={
        'name': '豆腐', 'storage': 'refrigerated', 'condition': 'raw', 'date_type': 'purchase',
        'started_at': (now() - timedelta(hours=2)).isoformat(),
        'deadline_source': 'user', 'expires_at': (now() + timedelta(hours=6)).isoformat(),
        'remind_at': (now() + timedelta(hours=1)).isoformat()}).json()
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    history, _ = agent._execute_tool('get_ingredient_history', {}, owner, {})
    assert history['items'][0]['text'] == '小麦粉、奶粉'
    reminders, _ = agent._execute_tool('get_reminder_status', {}, owner, {})
    assert reminders['items'][0]['food_name'] == '豆腐'
    assert reminders['items'][0]['status'] in ('pending', 'waiting_configuration')


def test_agent_recipe_detail_by_title_and_missing(signed, monkeypatch):
    enable(monkeypatch)
    signed.post('/api/v1/recipes', json={'title': '番茄豆腐', 'ingredients': '番茄、豆腐',
                                         'steps': '炒熟', 'meal': 'main', 'confirmed': True})
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    detail, card = agent._execute_tool('get_recipe_detail', {'title': '番茄豆腐'}, owner,
                                       {'diet': 'vegan', 'avoid_coriander': False})
    assert detail['title'] == '番茄豆腐' and card['type'] == 'recipe_detail'
    with pytest.raises(core.AppError):
        agent._execute_tool('get_recipe_detail', {'title': '不存在的菜'}, owner, {})


def test_agent_lists_candidates_for_recovery(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(providers, 'search_web',
                        lambda q: [{'title': '候选甲', 'source_url': 'https://example.org/a',
                                    'summary': 's'}])
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    agent._execute_tool('search_web_recipes', {'query': 'x'}, owner, {})
    listed, _ = agent._execute_tool('list_recipe_candidates', {}, owner, {})
    assert listed['count'] == 1 and listed['items'][0]['status'] == '待查看原文并复核配料'
    assert signed.get('/api/v1/recipes/candidates').json()['count'] == 1


def test_agent_knowledge_search_only_published(signed, monkeypatch):
    enable(monkeypatch)
    knowledge.import_entries([
        {'type': 'nutrition', 'title': '一般蔬食搭配', 'source': 'https://www.nhs.uk/x',
         'body': '多样化植物食物', 'conditions': {'diet': 'vegan'}},
        {'type': 'recipe', 'title': '待审菜谱', 'source': 'https://example.org/r', 'body': '摘要'}])
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    empty, _ = agent._execute_tool('search_knowledge', {'query': '蔬食'}, owner, {})
    assert empty['items'] == []
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge',
                        core.Record.payload['title'].as_string() == '一般蔬食搭配'))
        knowledge.set_status(row.payload['entry_id'], 'published')
    hit, card = agent._execute_tool('search_knowledge', {'query': '蔬食'}, owner, {})
    assert hit['items'][0]['title'] == '一般蔬食搭配'
    assert hit['items'][0]['entry_id'] and hit['items'][0]['source'].startswith('https://')
    assert card['type'] == 'knowledge_result'


def test_agent_proposes_reminder_change_without_writing(signed, monkeypatch):
    """正式修改：模型只能创建待确认操作，未确认前零业务写入。"""
    enable(monkeypatch)
    created = signed.post('/api/v1/foods', json={
        'name': '豆腐', 'storage': 'refrigerated', 'condition': 'raw', 'date_type': 'purchase',
        'started_at': (now() - timedelta(hours=2)).isoformat(),
        'deadline_source': 'user', 'expires_at': (now() + timedelta(hours=8)).isoformat()}).json()
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    out, card = agent._execute_tool('propose_food_action', {
        'food_id': created['id'], 'action': 'food_reminder',
        'remind_at': (now() + timedelta(hours=2)).isoformat()}, owner, {})
    assert card['type'] == 'pending_action'
    assert card['status'] == 'pending_confirmation'
    # 未确认：食材未变、没有提醒任务
    assert signed.get('/api/v1/foods').json()['items'][0]['remind_at'] is None
    assert signed.get('/api/v1/notifications/tasks').json() == []
    action_id = out['pending_action']['record_id']
    # 未经确认不能执行
    r = signed.post(f'/api/v1/actions/{action_id}/confirm', json={'confirm_key': 'nope'})
    assert r.status_code in (400, 422)
    assert signed.get('/api/v1/notifications/tasks').json() == []
    # 用户确认后执行
    ok = signed.post(f'/api/v1/actions/{action_id}/confirm', json={'confirm_key': 'user_confirmed'}).json()
    assert ok['status'] == 'confirmed'
    assert signed.get('/api/v1/foods').json()['items'][0]['remind_at']
    tasks = signed.get('/api/v1/notifications/tasks').json()
    assert len([t for t in tasks if t['status'] != 'cancelled']) == 1


def test_agent_cannot_touch_other_account_food(signed, monkeypatch):
    enable(monkeypatch)
    created = signed.post('/api/v1/foods', json={
        'name': '豆腐', 'storage': 'refrigerated', 'started_at': now().isoformat()}).json()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    with core.DB() as db:
        bob = db.scalar(select(core.User).where(core.User.username == 'bob')).id
    with pytest.raises(core.AppError) as exc:
        agent._execute_tool('get_food_detail', {'food_id': created['id']}, bob, {})
    assert exc.value.status == 404


# ================= 新增端点 =================
def test_recipe_can_be_updated_in_place(signed):
    created = signed.post('/api/v1/recipes', json={'title': '番茄豆腐', 'ingredients': '番茄、豆腐',
                                                   'steps': '炒熟', 'meal': 'main',
                                                   'confirmed': True}).json()
    updated = signed.put(f"/api/v1/recipes/{created['id']}", json={
        'title': '番茄豆腐（改）', 'ingredients': '番茄、豆腐、蒜', 'steps': '炒熟收汁',
        'meal': 'dinner', 'confirmed': True}).json()
    assert updated['id'] == created['id']          # 同一记录，不删除重建
    assert updated['title'] == '番茄豆腐（改）'
    assert updated['meal'] == 'dinner'


def test_recipe_origin_and_yield_recorded(signed):
    created = signed.post('/api/v1/recipes', json={
        'title': '我的原创', 'ingredients': '豆腐', 'steps': '煎', 'confirmed': True,
        'origin': 'user_original', 'yield_amount': 2, 'yield_unit': '人份'}).json()
    assert created['origin'] == 'user_original'
    assert created['yield_amount'] == 2 and created['yield_unit'] == '人份'


def test_cuisine_group_mapping_keeps_old_values(signed):
    signed.post('/api/v1/recipes', json={'title': '中式菜', 'ingredients': '豆腐', 'steps': '炒',
                                         'cuisine': 'chinese', 'confirmed': True})
    signed.post('/api/v1/recipes', json={'title': '西式菜', 'ingredients': '番茄', 'steps': '拌',
                                         'cuisine': 'western', 'confirmed': True})
    signed.post('/api/v1/recipes', json={'title': '其他菜', 'ingredients': '豆腐', 'steps': '煮',
                                         'cuisine': 'other', 'confirmed': True})
    rows = {i['title']: i for i in signed.get('/api/v1/recipes').json()['items']}
    assert rows['中式菜']['cuisine_group'] == 'chinese'
    assert rows['西式菜']['cuisine_group'] == 'western'
    assert rows['其他菜']['cuisine_group'] == 'unclassified'   # 不强归外国菜
    assert rows['其他菜']['cuisine'] == 'other'                # 旧值不变


def test_transcription_list_for_recovery(signed, monkeypatch):
    from app import transcription
    monkeypatch.setattr(core.settings, 'transcription_api_key', 'k')
    monkeypatch.setattr(core.settings, 'transcription_endpoint', transcription.DEFAULT_ENDPOINT)
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(transcription, 'transcribe',
                        lambda *a: {'text': '昨天买了豆腐', 'simulated': True,
                                    'transcribed_at': now().isoformat()})
    import io, struct
    frames = 16000
    data_bytes = b'\x00\x00' * frames
    wav = (b'RIFF' + struct.pack('<I', 36 + len(data_bytes)) + b'WAVEfmt ' +
           struct.pack('<IHHIIHH', 16, 1, 1, 16000, 32000, 2, 16) +
           b'data' + struct.pack('<I', len(data_bytes)) + data_bytes)
    job = signed.post('/api/v1/transcriptions',
                      files={'file': ('v.wav', io.BytesIO(wav), 'audio/wav')}).json()
    worker.process_once()
    listed = signed.get('/api/v1/transcriptions').json()
    assert listed['items'][0]['job_id'] == job['job_id']
    assert listed['items'][0]['text'] == '昨天买了豆腐'

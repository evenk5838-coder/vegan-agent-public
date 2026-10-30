"""Eating-plan evidence, persistence and countdown; no real upstream I/O."""
import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import agent, core, providers
from app.core import AppError
from app.services import consumption_plan as plans, storage_guidance as guidance, food_dialogue

QUOTE = '新鲜完整食材购买后冷藏，建议在2至3天内食用。'
SOURCE = {'id': 1, 'title': '合成测试来源', 'url': 'https://www.gov.cn/test-plan', 'content': QUOTE * 12}


def facts(**changes):
    return guidance.GuidanceInput(name='合成食材', food_class='fresh', condition='raw',
        storage='refrigerated', date_type='purchase',
        started_at=(core.now() - timedelta(days=1)).isoformat(), **changes).facts()


def window(**changes):
    return plans.SourceWindow(**{'source_id': 1, 'quote': QUOTE, 'minimum': 2.0, 'maximum': 3.0,
        'unit': 'days', 'anchor': 'purchase', 'food_class': 'fresh', 'condition': 'raw',
        'storage': 'refrigerated', **changes})


def upstream(monkeypatch, win=None):
    calls = []
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith('/search'):
            return {'results': [SOURCE]}
        return {'choices': [{'message': {'content': json.dumps({'summary': '资料建议冷藏，并在2至3天内安排食用。',
            'conditions_to_check': [], 'source_ids': [1], 'consumption_window': (win or window()).model_dump()}, ensure_ascii=False)}}]}
    monkeypatch.setattr(providers, 'request_json', request)
    return calls


def test_window_uses_early_end_and_original_start_not_generation_time():
    f = facts()
    plan = plans.build(window(), f, [SOURCE], {1})
    v = plans.view(f, plan)
    assert plan['status'] == 'ready' and v['remaining_seconds'] in range(86395, 86401)
    assert v['elapsed_percent'] == 50.0
    assert plan['target_at'] == (plans._parse(f['started_at']) + timedelta(days=2)).isoformat()
    assert plans.view(f, plan)['target_at'] == plan['target_at']


@pytest.mark.parametrize('change', [
    {'quote': '编造的2至3天'}, {'source_id': 99}, {'minimum': 9.0, 'maximum': 9.0},
    {'minimum': 3.0, 'maximum': 3.0}, {'unit': 'hours'},
])
def test_wrong_quote_source_duration_unit_or_range_cannot_create_countdown(change):
    p = plans.build(window(**change), facts(), [SOURCE], {1})
    assert p['status'] == 'unavailable' and p['target_at'] is None


@pytest.mark.parametrize('change', [{'opened': True}, {'condition': 'cut'},
    {'temperature_max': 4.0}, {'anchor': 'opened'}])
def test_missing_or_mismatched_conditions_do_not_create_time(change):
    p = plans.build(window(**change), facts(), [SOURCE], {1})
    assert p['status'] == 'needs_context' and p['target_at'] is None


def test_temperature_condition_cannot_be_omitted_from_extraction():
    q = '食材在4℃冷藏，建议2至3天内食用。'
    p = plans.build(window(quote=q), facts(), [{**SOURCE, 'content': q}], {1})
    assert p['status'] == 'needs_context'
    p = plans.build(window(quote=q, temperature_max=9.0), facts(temperature_c=8), [{**SOURCE, 'content': q}], {1})
    assert p['status'] == 'needs_context'  # source says 4, extracted maximum 9 is not authority


def test_within_duration_is_a_target_not_a_zero_length_plan():
    q = '切开的芒果需密封冷藏并在24小时内吃完。'
    f = {**facts(), 'condition': 'cut', 'date_type': 'prepared', 'storage_note': '已密封'}
    for minimum in (0.0, 24.0):
        p = plans.build(window(quote=q, minimum=minimum, maximum=24.0, unit='hours',
            condition='cut', anchor='prepared'), f, [{**SOURCE,'content':q}], {1})
        assert p['status'] == 'ready'
        assert plans._parse(p['target_at']) - plans._parse(p['start_at']) == timedelta(hours=24)


def test_guidance_failure_ends_turn_without_model_retry(signed, monkeypatch):
    from app import worker
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(guidance, 'generate', lambda *a: (_ for _ in ()).throw(AppError('UPSTREAM_UNAVAILABLE','搜索失败',502)))
    model_calls = []
    def model(history):
        model_calls.append(history)
        assert len(model_calls) == 1
        return {'tool_calls': [{'id':'plan', 'name':'draft_foods', 'arguments':{
            'items':[facts()], 'include_storage_guidance':True}}], 'text':''}
    monkeypatch.setattr(providers, 'generate_chat_reply', model)
    sid = signed.post('/api/v1/chat/sessions',json={}).json()['id']
    turn = signed.post(f'/api/v1/chat/sessions/{sid}/messages',json={'content':'登记并查建议几天吃完'}).json()
    worker.process_once()
    result = signed.get(f"/api/v1/chat/sessions/{sid}/turns/{turn['turn_id']}").json()
    assert result['status'] == 'succeeded' and '未能生成' in result['reply']['text']
    assert result['reply']['cards'][0]['type'] == 'food_draft' and len(model_calls) == 1


@pytest.mark.parametrize('text', ['登记并查建议几天吃完', '帮我查这份芒果建议什么时候吃完',
    '北京今天买的豆腐怎么保存，请查资料', '查询这份食材的倒计时'])
def test_food_planning_does_not_turn_into_restaurant_search(text):
    assert not agent._asks_for_restaurant(text)


def test_mixed_explicit_restaurant_request_still_routes():
    assert agent._asks_for_restaurant('登记芒果并找北京的素食餐厅')
    assert agent._asks_for_restaurant('帮我登记芒果，再找附近吃素的地方')


def test_recorded_date_caps_plan_and_changed_facts_invalidate_it():
    f = facts(expires_at=(core.now()+timedelta(hours=4)).isoformat(), deadline_source='user')
    p = plans.build(window(), f, [SOURCE], {1})
    assert plans._parse(p['target_at']) == plans._parse(f['expires_at'])
    for changes in ({'storage': 'frozen'}, {'opened': True}, {'name': '另一食材'}, {'status': 'used'}):
        v = plans.view({**f, **changes}, p)
        assert v['status'] == 'outdated' and v['target_at'] is None and v['elapsed_percent'] is None


def test_no_supported_duration_stays_unknown_and_due_is_not_expired():
    f = facts()
    assert plans.build(None, f, [SOURCE], {1})['target_at'] is None
    f['started_at'] = (core.now()-timedelta(days=4)).isoformat()
    p = plans.build(window(), f, [SOURCE], {1})
    assert plans.view(f,p)['status'] == 'due'
    assert plans.view(f,p)['elapsed_percent'] == 100


def test_saved_plan_survives_reads_without_network_and_never_sets_expiry(signed, monkeypatch):
    calls = upstream(monkeypatch)
    row = signed.post('/api/v1/foods', json=facts()).json()
    url = f"/api/v1/foods/{row['id']}/storage-guidance"
    result = signed.post(url).json()
    assert result['consumption_plan']['status'] == 'ready'
    assert len(calls) == 2
    for _ in range(2):
        item = signed.get('/api/v1/foods').json()['items'][0]
        assert item['consumption_plan']['target_at'] == result['consumption_plan']['target_at']
        assert item['expires_at'] is None and item['remind_at'] is None
    assert len(calls) == 2
    signed.put(f"/api/v1/foods/{row['id']}", json={'name': row['name'], 'storage': 'frozen'})
    assert signed.get('/api/v1/foods').json()['items'][0]['consumption_plan']['status'] == 'outdated'


def test_draft_plan_is_visible_before_confirmation_and_survives_save(signed, monkeypatch):
    calls = upstream(monkeypatch)
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    raw = facts(temperature_c=4)
    raw['temperature_c'] = 4  # real model sends an int, schema returns 4.0
    value, card = agent._execute_tool('draft_foods', {'items': [raw],
        'include_storage_guidance': True}, owner, {})
    assert value['items'][0]['consumption_plan']['status'] == 'ready'
    assert not signed.get('/api/v1/foods').json()['items']
    target = value['items'][0]['consumption_plan']['target_at']
    saved = signed.post(f"/api/v1/foods/drafts/{value['draft_id']}/confirm", json={
        'advice_digest': value['advice_digest'], 'idempotency_key': 'planned-food'}).json()
    assert saved['status'] == 'confirmed'
    items = signed.get('/api/v1/foods').json()['items']
    assert len(items) == 1 and items[0]['consumption_plan']['target_at'] == target
    assert items[0]['expires_at'] is None and len(calls) == 2


def test_search_failure_keeps_draft_and_plain_registration_does_not_search(signed, monkeypatch):
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    monkeypatch.setattr(guidance, 'generate', lambda *a: (_ for _ in ()).throw(AppError('UPSTREAM_UNAVAILABLE','搜索失败',502)))
    value, card = agent._execute_tool('draft_foods', {'items': [facts()], 'include_storage_guidance': True}, owner, {})
    assert value['storage_guidance_error']['code'] == 'UPSTREAM_UNAVAILABLE'
    assert card['type'] == 'food_draft' and len(value['items']) == 1
    simple, _ = agent._execute_tool('draft_foods', {'items': [facts()]}, owner, {})
    assert 'storage_guidance_error' not in simple and simple['items'][0]['consumption_plan'] is None


def test_client_cannot_inject_plan_and_old_plan_does_not_survive_context_edit(signed, monkeypatch):
    f = facts()
    injected = {**f, 'consumption_plan': plans.build(window(), f, [SOURCE], {1})}
    row = signed.post('/api/v1/foods', json=injected).json()
    assert row['consumption_plan'] is None
    d = signed.post('/api/v1/foods/drafts', json={'items': [injected]}).json()
    assert d['items'][0]['consumption_plan'] is None
    upstream(monkeypatch)
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    planned = food_dialogue.attach_guidance(owner, d['draft_id'], d['items'][0]['item_id'])
    changed = food_dialogue.revise(owner, {'draft_id': d['draft_id'], 'item_id': d['items'][0]['item_id'],
        'advice_digest': planned['advice_digest'], 'changes': {'storage': 'frozen'}})
    assert changed['items'][0]['consumption_plan']['status'] == 'outdated'


def test_concurrent_edit_during_search_cannot_attach_stale_plan(signed, monkeypatch):
    f = facts()
    row = signed.post('/api/v1/foods', json=f).json()
    def changed(owner, requested):
        with core.DB() as db:
            food = db.get(core.Record, row['id'])
            food.payload = {**food.payload, 'storage_note': '实际已经在室温放过'}
            db.commit()
        return {'consumption_plan': plans.build(window(), requested, [SOURCE], {1})}
    monkeypatch.setattr(guidance, 'generate', changed)
    result = signed.post(f"/api/v1/foods/{row['id']}/storage-guidance")
    assert result.status_code == 409
    saved = signed.get('/api/v1/foods').json()['items'][0]
    assert saved['consumption_plan'] is None and saved['storage_note'] == '实际已经在室温放过'

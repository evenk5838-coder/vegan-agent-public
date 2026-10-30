"""Food clarification continuity and confirmed persistence; no real provider calls."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import agent, core, providers, worker
from app.core import AppError
from app.services import food_dialogue, food_drafts


def owner(client):
    with core.DB() as db:
        return db.scalar(select(core.User)).id


def draft(client, **facts):
    return client.post('/api/v1/foods/drafts', json={'items': [{'name': '芒果', **facts}]}).json()


def revision(value, **changes):
    return {'draft_id': value['draft_id'], 'item_id': value['items'][0]['item_id'],
            'advice_digest': value['advice_digest'], 'changes': changes}


def turn(client, sid, text):
    response = client.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': text})
    assert response.status_code == 201, response.text
    identifier = response.json()['turn_id']
    worker.process_once()
    result = client.get(f'/api/v1/chat/sessions/{sid}/turns/{identifier}').json()
    assert result['status'] == 'succeeded', result
    return result['reply']


def tool(name, args):
    return {'tool_calls': [{'id': 'food_call', 'name': name, 'arguments': args}], 'text': ''}


def test_clarification_replays_questions_and_updates_same_draft_before_confirmation(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda history: {
        'text': '可以，先了解一下。', 'needs_input': True,
        'input_prompt': '是今天新买的完整芒果吗？现在常温放还是冷藏？'})
    turn(signed, sid, '买了芒果，帮我记一下、看看怎么放')
    calls = []
    responses = iter([tool('draft_foods', {'items': [{'name': '芒果', 'food_class': 'fresh',
                       'condition': 'raw', 'storage': 'ambient', 'date_type': 'purchase', 'date_phrase': '今天'}]})])
    def reply(history):
        calls.append(history)
        return next(responses)
    monkeypatch.setattr(providers, 'generate_chat_reply', reply)
    result = turn(signed, sid, '今天买的完整芒果，放常温，先帮我记下')
    assert '现在常温放还是冷藏' in calls[0][2]['content']
    value = result['cards'][0]['payload']
    assert not signed.get('/api/v1/foods').json()['items']
    assert value['items'][0]['storage'] == 'ambient'
    responses = iter([tool('update_food_draft', revision(value, storage='refrigerated'))])
    result = turn(signed, sid, '改一下，我放进冷藏了')
    updated = result['cards'][0]['payload']
    assert updated['draft_id'] == value['draft_id']
    assert updated['items'][0]['storage'] == 'refrigerated'
    assert len(signed.get('/api/v1/foods/drafts').json()['items']) == 1
    context = '\n'.join(m['content'] or '' for m in calls[-1] if m['role'] == 'assistant')
    assert value['draft_id'] in context and value['advice_digest'] in context
    assert len(calls) == 2  # one model call per explicit create/update, no final-model roundtrip
    assert not signed.get('/api/v1/foods').json()['items']
    saved = signed.post(f"/api/v1/foods/drafts/{value['draft_id']}/confirm", json={
        'advice_digest': updated['advice_digest'], 'idempotency_key': 'dialogue-confirm'}).json()
    assert saved['status'] == 'confirmed'
    foods = signed.get('/api/v1/foods').json()['items']
    assert len(foods) == 1 and foods[0]['storage'] == 'refrigerated'
    assert foods[0]['temperature_c'] is None and foods[0]['expires_at'] is None
    with core.DB() as db:
        current = food_dialogue.draft_context(db, owner(signed), result['cards'])
        assert current[0]['items'][0]['saved_food_id'] == foods[0]['id']


def test_revision_preserves_unknowns_and_rejects_stale_or_other_owner(signed):
    initial = draft(signed)
    uid = owner(signed)
    updated = food_dialogue.revise(uid, revision(initial, package_note='不知道生产日期'))
    assert updated['items'][0]['name'] == '芒果'
    assert updated['items'][0].get('opened') is None
    assert updated['items'][0].get('started_at') is None
    with pytest.raises(AppError, match='草稿已变化'):
        food_dialogue.revise(uid, revision(initial, name='不应该覆盖'))
    with pytest.raises(AppError) as error:
        food_dialogue.revise('another-user', revision(updated, name='不可访问'))
    assert error.value.code == 'NOT_FOUND'
    reread = signed.get(f"/api/v1/foods/drafts/{initial['draft_id']}").json()
    assert reread['items'][0]['name'] == '芒果'


def test_revision_cannot_edit_saved_item_or_create_duplicates(signed):
    initial = draft(signed)
    signed.post(f"/api/v1/foods/drafts/{initial['draft_id']}/confirm", json={'advice_digest': initial['advice_digest']})
    current = signed.get(f"/api/v1/foods/drafts/{initial['draft_id']}").json()
    with pytest.raises(AppError) as error:
        food_dialogue.revise(owner(signed), revision(current, name='另一份芒果'))
    assert error.value.code in ('DRAFT_CLOSED', 'ITEM_SAVED')
    assert len(signed.get('/api/v1/foods').json()['items']) == 1


def test_package_explicit_expiry_is_parsed_but_duration_alone_is_only_recorded(signed):
    result, card = agent._execute_tool('draft_foods', {'items': [{'name': '包装豆腐',
        'food_class': 'packaged', 'opened': False, 'package_note': '包装写保质期7天；冷藏',
        'package_expiry_phrase': '2026-11-05'}]}, owner(signed), {})
    item = result['items'][0]
    assert item['expires_at'] == '2026-11-05T00:00:00+08:00'
    assert item['deadline_source'] == 'package'
    assert '保质期7天' in item['package_note']
    assert item.get('started_at') is None
    assert item['suggestion']['date'] is None  # missing actual storage facts, no automatic formal date
    assert not signed.get('/api/v1/foods').json()['items']
    duration = food_dialogue.normalize_facts({'name': '豆奶', 'package_note': '保质期6个月'})
    assert 'expires_at' not in duration


def test_expiry_answer_keeps_prior_package_note(signed):
    value = draft(signed, food_class='packaged', package_note='未开封冷藏，保质期7天')
    result = food_dialogue.revise(owner(signed), revision(value, package_expiry_phrase='2026-11-05'))
    assert '未开封冷藏' in result['items'][0]['package_note']


@pytest.mark.parametrize('changes', [
    {'storage': 'guess'}, {'temperature_c': 999}, {'name': ''},
    {'package_expiry_phrase': '明天'}, {'package_expiry_phrase': '2026-02-30'},
    {'package_expiry_phrase': '2026-11-05', 'expires_at': '2026-11-06T00:00:00+08:00'},
    {'package_expiry_phrase': '2026-11-05', 'deadline_source': 'user'},
    {'unexpected_field': 'ignored'},
])
def test_invalid_updates_never_partially_mutate(signed, changes):
    value = draft(signed)
    with pytest.raises(AppError):
        food_dialogue.revise(owner(signed), revision(value, **changes))
    reread = signed.get(f"/api/v1/foods/drafts/{value['draft_id']}").json()
    assert reread['items'] == value['items']
    assert reread['advice_digest'] == value['advice_digest']


def test_context_refreshes_only_visible_owned_live_drafts(signed):
    value = draft(signed)
    card = {'type': 'food_draft', 'payload': {'draft_id': value['draft_id'], 'items': [{'name': 'stale'}]}}
    uid = owner(signed)
    with core.DB() as db:
        assert food_dialogue.draft_context(db, 'other-user', [card]) == []
        assert food_dialogue.draft_context(db, uid, []) == []
        assert food_dialogue.draft_context(db, uid, [card])[0]['items'][0]['name'] == '芒果'
        row = food_drafts.load(db, value['draft_id'], uid)
        row.expires = core.now() - timedelta(seconds=1)
        db.commit()
        assert food_dialogue.draft_context(db, uid, [card]) == []


@pytest.mark.parametrize('text', ['记录芒果，再推荐菜谱', '更新刚才那份，怎么存放？',
    '帮我记下，另外找附近素食馆', '登记豆腐和提醒', '先查保质期是否适用再记录'])
def test_mixed_questions_do_not_end_at_registration_card(text):
    assert food_dialogue.is_registration_reply(text) is False

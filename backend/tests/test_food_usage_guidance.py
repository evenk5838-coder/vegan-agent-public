"""Qualitative use planning is separate from expiry, reminders and publication."""
from datetime import timedelta

import pytest

from app import core, storage_rules
from app.services import food_advice


def food(**changes):
    return {'name': '豆腐', 'food_class': 'fresh', 'condition': 'raw',
            'storage': 'refrigerated', 'date_type': 'purchase',
            'started_at': (core.now() - timedelta(hours=1)).isoformat(), **changes}


@pytest.mark.parametrize('changes,priority,title', [
    ({}, 'use_soon', '生鲜'),
    ({'food_class': 'cooked', 'condition': 'cooked', 'date_type': 'prepared'}, 'use_soon', '熟食'),
    ({'storage': 'frozen'}, 'plan_use', '冷冻'),
    ({'food_class': 'packaged', 'opened': True, 'date_type': 'opened'}, 'check_first', '开封'),
    ({'storage': 'ambient'}, 'check_first', '存放方式'),
    ({'date_type': None, 'started_at': None}, 'check_first', '补齐'),
])
def test_usage_from_recorded_facts_never_creates_expiry_or_side_effects(signed, changes, priority, title):
    payload = food(**changes)
    original_statuses = [rule['status'] for rule in storage_rules.RULES]
    response = signed.post('/api/v1/foods/assess', json=payload)
    assert response.status_code == 200
    suggestion = response.json()['suggestion']
    assert suggestion['usage']['priority'] == priority
    assert title in suggestion['usage']['title']
    for key in ('food_class', 'storage', 'date_type', 'started_at'):
        assert suggestion['usage']['observed'][key] == payload.get(key)
    assert suggestion['action'] == 'unknown' and suggestion['date'] is None
    assert suggestion['basis']['type'] == 'none' and suggestion['rules_status'] == 'none'
    assert signed.get('/api/v1/foods').json()['items'] == []
    assert signed.get('/api/v1/notifications/tasks').json() == []
    assert [rule['status'] for rule in storage_rules.RULES] == original_statuses
    assert original_statuses == ['draft', 'draft', 'draft']


@pytest.mark.parametrize('changes', [
    {'expires_at': (core.now() - timedelta(hours=1)).isoformat(), 'deadline_source': 'user'},
    {'food_class': 'packaged', 'opened': True, 'package_note': '未开封冷藏保存',
     'expires_at': (core.now() + timedelta(days=3)).isoformat(), 'deadline_source': 'package'},
    {'storage': 'frozen', 'started_at': (core.now() + timedelta(hours=1)).isoformat()},
])
def test_passed_date_mismatched_package_and_future_event_take_precedence(changes):
    advice = food_advice.assess(food(**changes))
    assert advice['usage']['priority'] == 'check_first'
    assert advice['action'] == 'unknown' and advice['date'] is None
    assert '尽快食用' not in advice['usage']['title']


def test_matching_package_keeps_existing_date_and_takes_precedence_over_generic_use(signed):
    expiry = (core.now() + timedelta(days=3)).isoformat()
    payload = food(food_class='packaged', opened=False, date_type='production',
                   package_note='未开封冷藏保存', expires_at=expiry, deadline_source='package')
    advice = signed.post('/api/v1/foods/assess', json=payload).json()['suggestion']
    assert advice['action'] == 'date' and advice['date'] == expiry
    assert advice['basis']['type'] == 'package'
    assert advice['usage']['priority'] == 'plan_use' and '包装' in advice['usage']['title']
    assert advice['usage']['observed']['expires_at'] == expiry
    assert signed.get('/api/v1/notifications/tasks').json() == []


def test_usage_flows_through_drafts_and_formal_reads_without_generating_deadline(signed):
    payload = food()
    draft = signed.post('/api/v1/foods/drafts', json={'items': [payload]}).json()
    suggestion = draft['items'][0]['suggestion']
    assert suggestion['usage']['priority'] == 'use_soon'
    assert suggestion['advice_version'] == food_advice.ADVICE_VERSION
    result = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={
        'advice_digest': draft['advice_digest'], 'idempotency_key': 'usage-only-confirm'}).json()
    assert result['status'] == 'confirmed'
    saved = signed.get('/api/v1/foods').json()['items'][0]
    assert saved['suggestion']['usage']['observed']['storage'] == 'refrigerated'
    assert saved['expires_at'] is None and saved['valid_deadline_at'] is None
    assert saved['deadline_source'] == 'unknown'
    assert signed.get('/api/v1/notifications/tasks').json() == []


def test_version_change_keeps_stale_draft_confirmation_protected(signed, monkeypatch):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [food()]}).json()
    old_digest = draft['advice_digest']
    monkeypatch.setattr(food_advice, 'ADVICE_VERSION', food_advice.ADVICE_VERSION + '-changed')
    response = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={
        'advice_digest': old_digest, 'idempotency_key': 'stale-usage-version'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'DRAFT_CHANGED'
    assert signed.get('/api/v1/foods').json()['items'] == []

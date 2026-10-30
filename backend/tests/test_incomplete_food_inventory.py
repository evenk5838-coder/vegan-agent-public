"""Incomplete inventory is savable, without fabricated food facts or reminders."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import core, providers, worker
from app.core import now
from app.schemas import FoodInput


def test_name_only_direct_save_keeps_unknown_and_registration_separate(signed):
    response = signed.post('/api/v1/foods', json={'name': '  豆腐  '})
    assert response.status_code == 201
    item = response.json()
    assert item['name'] == '豆腐'
    assert [item[k] for k in ('storage', 'condition', 'date_type')] == ['unknown'] * 3
    assert item['started_at'] is None and item['created']
    assert item['expires_at'] is None and item['remind_at'] is None
    assert item['valid_started_at'] is None and item['valid_deadline_at'] is None
    assert item['stored_for_days'] is None and item['estimated_remaining_days'] is None
    assert item['suggestion']['reason'] == 'missing_info'
    assert item['suggestion']['usage']['priority'] == 'check_first'
    assert signed.get('/api/v1/foods').json()['items'][0]['started_at'] is None
    assert signed.get('/api/v1/notifications/tasks').json() == []


def test_name_only_draft_one_confirmation_replay_and_account_isolation(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{'name': '西兰花'}]}).json()
    assert draft['items'][0]['status'] == 'ready' and draft['items'][0]['missing'] == []
    assert signed.get('/api/v1/foods').json()['items'] == []
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/confirm"
    body = {'advice_digest': draft['advice_digest'], 'idempotency_key': 'one-confirm'}
    first = signed.post(path, json=body)
    assert first.status_code == 200 and first.json()['status'] == 'confirmed'
    assert signed.post(path, json=body).json() == first.json()
    assert len(signed.get('/api/v1/foods').json()['items']) == 1
    saved_id = first.json()['results'][0]['food_id']
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'other', 'password': 'correct-horse-456'})
    assert signed.get('/api/v1/foods').json()['items'] == []
    assert signed.post(path, json=body).status_code == 404
    assert signed.put(f'/api/v1/foods/{saved_id}', json={'name': '偷改'}).status_code == 404


def test_production_date_preserves_meaning_without_storage_default(signed):
    timestamp = (now() - timedelta(days=2)).isoformat()
    draft = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '豆奶', 'date_type': 'production', 'started_at': timestamp}]}).json()
    result = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={})
    assert result.json()['status'] == 'confirmed'
    item = signed.get('/api/v1/foods').json()['items'][0]
    assert item['date_type'] == 'production'
    assert datetime.fromisoformat(item['started_at']) == datetime.fromisoformat(timestamp)
    assert item['storage'] == item['condition'] == 'unknown'
    assert item['valid_deadline_at'] is None and item['suggestion']['date'] is None


def test_relative_date_without_meaning_is_savable_but_not_a_purchase(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '苹果', 'date_phrase': '今天'}],
        'reference_time': '2026-10-27T10:00:00+08:00'}).json()
    assert draft['items'][0]['status'] == 'ready'
    assert signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()['status'] == 'confirmed'
    saved = signed.get('/api/v1/foods').json()['items'][0]
    assert saved['date_type'] == 'unknown'
    assert saved['started_at'].startswith('2026-10-27T00:00:00')
    assert saved['valid_started_at'] is None


@pytest.mark.parametrize('changes', [{'storage': 'mystery'}, {'started_at': '2026-10-27T10:00:00'},
                                      {'expires_at': now().isoformat()}])
def test_illegal_filled_evidence_still_rejected(signed, changes):
    assert signed.post('/api/v1/foods', json={'name': '豆腐', **changes}).status_code == 422


def test_missing_name_still_blocks_and_known_fields_can_be_cleared(signed):
    assert signed.post('/api/v1/foods', json={'name': '  '}).status_code == 422
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{'name': '苹果', 'storage': 'ambient'}]}).json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/items/{draft['items'][0]['item_id']}"
    assert signed.put(path, json={'clear': ['name']}).status_code == 422
    assert signed.put(path, json={'clear': ['storage', 'started_at', 'condition', 'date_type']}).status_code == 200
    assert signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()['status'] == 'confirmed'
    item = signed.get('/api/v1/foods').json()['items'][0]
    assert item['storage'] == 'unknown' and item['started_at'] is None


def test_unknown_conditions_never_schedule_or_dispatch_legacy_reminder(signed):
    payload = {'name': '豆腐', 'deadline_source': 'user',
        'expires_at': (now() + timedelta(days=1)).isoformat(),
        'remind_at': (now() + timedelta(hours=1)).isoformat()}
    # New invalid requests are explicit failures, not a saved reminder without
    # a job. Seed the old stored state below to keep testing legacy dispatch.
    response = signed.post('/api/v1/foods', json=payload)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'REMINDER_BASIS_REQUIRED'
    item = signed.post('/api/v1/foods', json={key: value for key, value in payload.items()
                                            if key != 'remind_at'}).json()
    assert item['valid_deadline_at'] is None and item['estimated_remaining_days'] is None
    assert signed.get('/api/v1/notifications/tasks').json() == []
    with core.DB() as db:
        record = db.get(core.Record, item['id'])
        record.payload = {**record.payload, 'remind_at': payload['remind_at']}
        task = core.Job(owner=record.owner, kind='reminder', due=now() - timedelta(seconds=1),
            payload={'food_id': item['id'], 'version': record.payload['version']})
        db.add(task); db.commit(); task_id = task.id
    worker.process_once()
    with core.DB() as db:
        assert db.get(core.Job, task_id).status == 'cancelled'


def test_minimal_update_preserves_complete_record_and_explicit_unknown_clears(signed):
    known = {'name': '苹果', 'food_class': 'fresh', 'storage': 'ambient', 'condition': 'raw',
        'date_type': 'purchase', 'started_at': (now() - timedelta(hours=1)).isoformat()}
    item = signed.post('/api/v1/foods', json=known).json()
    result = signed.put(f"/api/v1/foods/{item['id']}", json={'name': '苹果', 'status': 'used'}).json()
    for key in ('storage', 'condition', 'date_type', 'started_at'):
        assert result[key] == item[key]
    cleared = signed.put(f"/api/v1/foods/{item['id']}", json={'name': '苹果', 'storage': 'unknown'}).json()
    assert cleared['storage'] == 'unknown' and cleared['valid_deadline_at'] is None


def test_unknown_roundtrips_assess_and_changed_digest_remains_409(signed):
    assert signed.post('/api/v1/foods/assess', json={'name': '豆腐', 'food_class': 'fresh',
        'storage': 'unknown', 'condition': 'unknown', 'date_type': 'unknown', 'started_at': None}).json()['suggestion']['reason'] == 'missing_info'
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{'name': '豆腐'}]}).json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}"
    signed.put(path + f"/items/{draft['items'][0]['item_id']}", json={'storage': 'frozen'})
    response = signed.post(path + '/confirm', json={'advice_digest': draft['advice_digest']})
    assert response.status_code == 409 and response.json()['error']['code'] == 'DRAFT_CHANGED'
    assert signed.get('/api/v1/foods').json()['items'] == []


@pytest.mark.parametrize('phrase', ['2026-10-25', '2026年10月25日'])
def test_explicit_production_day_is_validated_and_marked_as_day_not_actual_time(signed, phrase):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '豆奶', 'date_type': 'production', 'date_phrase': phrase}],
        'timezone': 'Asia/Shanghai'}).json()
    item = draft['items'][0]
    assert item['date_source'] == 'absolute' and item['time_precision'] == 'date'
    assert item['date_warnings'] and item['started_at'] == '2026-10-25T00:00:00+08:00'
    assert item['status'] == 'ready'
    assert signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()['status'] == 'confirmed'
    saved = signed.get('/api/v1/foods').json()['items'][0]
    assert saved['date_type'] == 'production' and saved['valid_deadline_at'] is None
    assert saved['time_precision'] == 'date' and saved['date_source'] == 'absolute'
    assert saved['date_warnings'] == item['date_warnings']


@pytest.mark.parametrize('body', [{'date_phrase': '2026-02-30'},
                                  {'date_phrase': '2026-10-25', 'timezone': 'Not/AZone'}])
def test_illegal_absolute_day_or_timezone_never_creates_food(signed, body):
    response = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '豆奶', 'date_phrase': body['date_phrase']}], **({'timezone': body['timezone']} if 'timezone' in body else {})})
    assert response.status_code == 422
    assert signed.get('/api/v1/foods').json()['items'] == []


def test_explicit_date_cannot_disagree_with_supplied_timestamp(signed):
    response = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '豆奶', 'date_phrase': '2026-10-25', 'started_at': '2026-10-26T00:00:00+08:00'}]})
    assert response.status_code == 422 and response.json()['error']['code'] == 'DATE_CONFLICT'
    assert signed.get('/api/v1/foods').json()['items'] == []


def test_bad_date_item_stays_editable_and_does_not_block_valid_batch_item(signed):
    response = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '豆腐'}, {'name': '豆奶', 'started_at': 'not-a-time'}]})
    assert response.status_code == 201
    draft = response.json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}"
    assert signed.get(path).status_code == 200
    assert draft['items'][0]['status'] == 'ready'
    assert draft['items'][1]['status'] == 'needs_info'
    assert draft['items'][1]['suggestion']['date'] is None
    result = signed.post(path + '/confirm', json={}).json()
    assert result['status'] == 'partially_saved'
    assert [r['status'] for r in result['results']] == ['saved', 'failed']
    assert signed.put(path + f"/items/{draft['items'][1]['item_id']}", json={'clear': ['started_at']}).status_code == 200
    assert signed.post(path + '/confirm', json={}).json()['status'] == 'confirmed'


@pytest.mark.parametrize('value', ['2026-10-26T12:00:00+08:00', None])
def test_changed_date_drops_previous_phrase_and_precision(signed, value):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '豆奶', 'date_type': 'production', 'date_phrase': '2026-10-25'}]}).json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}"
    changed = signed.put(path + f"/items/{draft['items'][0]['item_id']}", json={'started_at': value})
    assert changed.status_code == 200
    assert 'date_source' not in changed.json()['item'] and changed.json()['item']['date_warnings'] == []
    assert signed.post(path + '/confirm', json={}).json()['status'] == 'confirmed'
    saved = signed.get('/api/v1/foods').json()['items'][0]
    assert 'date_source' not in saved and 'time_precision' not in saved
    assert saved['started_at'] == value

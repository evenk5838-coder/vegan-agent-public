"""Pending proposals are deduplicated under concurrency; reads never undo writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import core
from app.main import app
from app.services import actions


def make_food(client):
    response = client.post('/api/v1/foods', json={
        'name': '豆腐', 'food_class': 'fresh', 'storage': 'refrigerated',
        'condition': 'raw', 'date_type': 'purchase',
        'started_at': (core.now() - timedelta(hours=1)).isoformat(),
        'deadline_source': 'user', 'expires_at': (core.now() + timedelta(hours=8)).isoformat(),
    })
    assert response.status_code == 201
    return response.json()


def test_concurrent_identical_proposals_create_one_persistent_action(signed, monkeypatch):
    food = make_food(signed)
    barrier = Barrier(4)
    original = actions.advisory_lock

    def simultaneous(db, *parts):
        if parts[0] == 'action-create':
            barrier.wait(timeout=10)
        return original(db, *parts)

    monkeypatch.setattr(actions, 'advisory_lock', simultaneous)
    cookies = dict(signed.cookies)

    def propose(_):
        with TestClient(app) as client:
            client.cookies.update(cookies)
            return client.post('/api/v1/actions', json={
                'action': 'food_status', 'food_id': food['id'], 'status': 'used',
            })

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(propose, range(4)))
    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()['record_id'] for response in responses}) == 1
    with core.DB() as db:
        rows = list(db.scalars(select(core.Record).where(core.Record.kind == actions.KIND)))
        assert len(rows) == 1


def test_expired_proposal_does_not_prevent_new_proposal(signed):
    food = make_food(signed)
    body = {'action': 'food_status', 'food_id': food['id'], 'status': 'used'}
    old = signed.post('/api/v1/actions', json=body).json()
    with core.DB() as db:
        record = db.get(core.Record, old['record_id'])
        record.expires = core.now() - timedelta(seconds=1)
        db.commit()
    new = signed.post('/api/v1/actions', json=body).json()
    assert new['record_id'] != old['record_id']
    assert new['status'] == 'pending'


def test_expired_action_get_is_read_only_and_cannot_overwrite_confirmation(signed, monkeypatch):
    food = make_food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used',
    }).json()
    with core.DB() as stale_read:
        record = stale_read.get(core.Record, action['record_id'])
        owner = record.owner
        assert record.payload['status'] == 'pending'
        response = signed.post(f"/api/v1/actions/{record.id}/confirm", json={'confirm_key': 'user_confirmed'})
        assert response.status_code == 200
        future = core.now() + timedelta(hours=1)
        monkeypatch.setattr(actions, 'now', lambda: future)
        result = actions.list_open(stale_read, owner)
        assert result['items'][0]['status'] == 'confirmed'
        assert result['items'][0]['needs_confirmation'] is False
        assert not stale_read.dirty
    with core.DB() as db:
        assert db.get(core.Record, action['record_id']).payload['status'] == 'confirmed'


def test_expired_action_read_disables_confirmation_without_mutating_database(signed):
    food = make_food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used',
    }).json()
    with core.DB() as db:
        record = db.get(core.Record, action['record_id'])
        record.expires = core.now() - timedelta(seconds=1)
        db.commit()
    response = signed.get('/api/v1/actions').json()
    row = next(item for item in response['items'] if item['record_id'] == action['record_id'])
    assert row['status'] == 'expired' and not row['needs_confirmation']
    with core.DB() as db:
        assert db.get(core.Record, action['record_id']).payload['status'] == 'pending'


def test_cancelling_expired_proposal_never_changes_food(signed):
    food = make_food(signed)
    action = signed.post('/api/v1/actions', json={
        'action': 'food_status', 'food_id': food['id'], 'status': 'used',
    }).json()
    with core.DB() as db:
        record = db.get(core.Record, action['record_id'])
        record.expires = core.now() - timedelta(seconds=1)
        db.commit()
    response = signed.delete('/api/v1/actions/' + action['record_id'])
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'ACTION_EXPIRED'
    assert signed.get('/api/v1/foods').json()['items'][0]['status'] == 'active'


def test_changed_eating_plan_is_not_deduplicated_into_an_older_confirmation(signed):
    food = make_food(signed)
    with core.DB() as db:
        record = db.get(core.Record, food['id'])
        owner, version = record.owner, record.payload['version']
        target = {'food_id': food['id'], 'name': '豆腐'}
        before = {'action': 'food_condition', 'storage': 'refrigerated',
                  '_consumption_plan': {'target_at': '2026-11-01T12:00:00+08:00'}}
        after = {**before, '_consumption_plan': {'target_at': '2026-11-01T18:00:00+08:00'}}
        first = actions.create(db, owner, 'food_action', target, before, version)
        second = actions.create(db, owner, 'food_action', target, after, version)
        replay = actions.create(db, owner, 'food_action', target, after, version)
    assert first['record_id'] != second['record_id']
    assert second['record_id'] == replay['record_id']
    assert second['proposed']['_consumption_plan'] == after['_consumption_plan']
    assert signed.get('/api/v1/foods').json()['items'][0]['status'] == 'active'

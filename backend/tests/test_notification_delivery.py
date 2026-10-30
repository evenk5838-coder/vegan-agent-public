"""Notification state recovery with synthetic subscriptions and no outbound I/O."""
import base64
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException
from sqlalchemy import select

from app import core, worker


def configured(monkeypatch, enabled=True):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', enabled)
    monkeypatch.setattr(core.settings, 'vapid_private_key', 'synthetic-private-key')
    monkeypatch.setattr(core.settings, 'vapid_public_key', 'synthetic-public-key')
    monkeypatch.setattr(core.settings, 'vapid_subject', 'mailto:synthetic@example.invalid')


def subscription(path='synthetic', fill=b'a'):
    return {'endpoint': 'https://web.push.apple.com/' + path,
            'keys': {'p256dh': base64.urlsafe_b64encode(fill * 65).decode().rstrip('='),
                     'auth': base64.urlsafe_b64encode(fill * 16).decode().rstrip('=')}}


def reminder(signed, *, status='pending', attempts=0, subs=()):
    stamp = core.now()
    response = signed.post('/api/v1/foods', json={
        'name': '蔬菜汤', 'storage': 'refrigerated', 'location': 'indoor',
        'condition': 'cooked', 'food_class': 'cooked', 'temperature_c': 4,
        'date_type': 'prepared', 'started_at': (stamp - timedelta(hours=1)).isoformat(),
        'status': 'active', 'deadline_source': 'user',
        'expires_at': (stamp + timedelta(hours=1)).isoformat(),
        'remind_at': (stamp - timedelta(seconds=1)).isoformat(),
    })
    assert response.status_code == 201, response.text
    with core.DB() as db:
        task = db.scalar(select(core.Job).where(core.Job.kind == 'reminder'))
        task.status, task.attempts = status, attempts
        owner, identifier = task.owner, task.id
        for item in subs:
            db.add(core.Record(owner=owner, kind='subscription', payload=item))
        db.commit()
    return owner, identifier


def task(identifier):
    with core.DB() as db:
        row = db.get(core.Job, identifier)
        return row.status, row.attempts, row.result


def test_global_outbound_switch_gates_all_push_entry_points(signed, monkeypatch):
    configured(monkeypatch, enabled=False)
    _, identifier = reminder(signed, subs=[subscription()])
    monkeypatch.setattr('pywebpush.webpush', lambda **kw: pytest.fail('Outbound switch is off'))
    assert signed.get('/api/v1/notifications/config').json()['public_key'] == ''
    response = signed.post('/api/v1/notifications/subscriptions', json=subscription())
    assert response.status_code == 503
    assert worker.process_once() is True
    assert task(identifier)[:2] == ('waiting_configuration', 0)
    worker.recover_and_clean()
    assert task(identifier)[:2] == ('waiting_configuration', 0)


@pytest.mark.parametrize('missing', ['vapid_private_key', 'vapid_public_key', 'vapid_subject'])
def test_partial_push_configuration_waits_without_spending_delivery_attempt(signed, monkeypatch, missing):
    configured(monkeypatch)
    monkeypatch.setattr(core.settings, missing, '')
    _, identifier = reminder(signed, subs=[subscription()])
    monkeypatch.setattr('pywebpush.webpush', lambda **kw: pytest.fail('Incomplete configuration'))
    worker.process_once()
    assert task(identifier)[:2] == ('waiting_configuration', 0)


def test_restart_after_configuration_resumes_waiting_reminder(signed, monkeypatch):
    configured(monkeypatch)
    _, identifier = reminder(signed, status='waiting_configuration', attempts=1, subs=[subscription()])
    calls = []
    monkeypatch.setattr('pywebpush.webpush', lambda **kw: calls.append(kw))
    worker.recover_and_clean()
    assert task(identifier)[:2] == ('pending', 0)
    worker.process_once()
    assert len(calls) == 1
    state, attempts, result = task(identifier)
    assert (state, attempts) == ('accepted', 1)
    assert result == {'accepted_by_service': 1, 'device_display': 'unverified'}


def test_same_endpoint_renews_keys_and_resumes_only_own_waiting_tasks(signed, monkeypatch):
    configured(monkeypatch)
    old, renewed = subscription(), subscription(fill=b'b')
    owner, identifier = reminder(signed, status='waiting_subscription', attempts=2, subs=[old, old])
    with core.DB() as db:
        other = core.Job(owner='other-account', kind='reminder', status='waiting_subscription',
                         payload={'food_id': 'other', 'version': 'v1'})
        db.add(other); db.commit()
        other_id = other.id
    response = signed.post('/api/v1/notifications/subscriptions', json=renewed)
    assert response.status_code == 200
    with core.DB() as db:
        records = list(db.scalars(select(core.Record).where(
            core.Record.kind == 'subscription', core.Record.owner == owner)))
        assert len(records) == 1 and records[0].payload == renewed
    assert task(identifier)[:2] == ('pending', 0)
    assert task(other_id)[0] == 'waiting_subscription'
    sent = []
    monkeypatch.setattr('pywebpush.webpush', lambda **kw: sent.append(kw['subscription_info']))
    worker.process_once()
    assert sent == [renewed]


def test_no_subscription_waits_and_unsubscribe_does_not_remove_other_account(signed, monkeypatch):
    configured(monkeypatch)
    owner, identifier = reminder(signed)
    monkeypatch.setattr('pywebpush.webpush', lambda **kw: pytest.fail('No subscription'))
    worker.process_once()
    assert task(identifier)[:2] == ('waiting_subscription', 0)
    with core.DB() as db:
        db.add(core.Record(owner=owner, kind='subscription', payload=subscription('own')))
        db.add(core.Record(owner='other-account', kind='subscription', payload=subscription('other')))
        db.commit()
    assert signed.delete('/api/v1/notifications/subscriptions').status_code == 200
    with core.DB() as db:
        rows = list(db.scalars(select(core.Record).where(core.Record.kind == 'subscription')))
        assert len(rows) == 1 and rows[0].owner == 'other-account'


@pytest.mark.parametrize('code', [404, 410])
def test_permanently_expired_subscription_is_removed_without_retry_loop(signed, monkeypatch, code):
    configured(monkeypatch)
    owner, identifier = reminder(signed, subs=[subscription()])
    with core.DB() as db:
        db.add(core.Record(owner='other-account', kind='subscription', payload=subscription()))
        db.commit()

    def expired(**kwargs):
        raise WebPushException('Synthetic expired subscription', response=SimpleNamespace(status_code=code))

    monkeypatch.setattr('pywebpush.webpush', expired)
    worker.process_once()
    assert task(identifier)[0] == 'waiting_subscription'
    assert worker.process_once() is False
    with core.DB() as db:
        rows = list(db.scalars(select(core.Record).where(core.Record.kind == 'subscription')))
        assert len(rows) == 1 and rows[0].owner == 'other-account'


def test_partial_device_acceptance_does_not_repeat_successful_device(signed, monkeypatch):
    configured(monkeypatch)
    owner, identifier = reminder(signed, subs=[subscription('expired'), subscription('working')])
    sent = []

    def send(**kwargs):
        endpoint = kwargs['subscription_info']['endpoint']
        sent.append(endpoint)
        if endpoint.endswith('/expired'):
            raise WebPushException('Synthetic expired device', response=SimpleNamespace(status_code=410))

    monkeypatch.setattr('pywebpush.webpush', send)
    worker.process_once()
    assert len(sent) == 2
    assert task(identifier)[0] == 'accepted'
    assert task(identifier)[2]['accepted_by_service'] == 1
    assert worker.process_once() is False
    with core.DB() as db:
        rows = list(db.scalars(select(core.Record).where(
            core.Record.kind == 'subscription', core.Record.owner == owner)))
        assert len(rows) == 1 and rows[0].payload['endpoint'].endswith('/working')


def test_invalid_legacy_endpoint_is_removed_without_outbound_or_spent_attempt(signed, monkeypatch):
    configured(monkeypatch)
    owner, identifier = reminder(signed, subs=[{**subscription(), 'endpoint': 'https://invalid.example/test'}])
    monkeypatch.setattr('pywebpush.webpush', lambda **kw: pytest.fail('Invalid destination'))
    worker.process_once()
    assert task(identifier)[:2] == ('waiting_subscription', 0)
    with core.DB() as db:
        assert db.scalar(select(core.Record).where(
            core.Record.kind == 'subscription', core.Record.owner == owner)) is None


def test_expired_response_cannot_remove_concurrently_renewed_keys(signed, monkeypatch):
    configured(monkeypatch)
    old, renewed = subscription(), subscription(fill=b'b')
    owner, identifier = reminder(signed, subs=[old])

    def renew_during_send(**kwargs):
        with core.DB() as db:
            record = db.scalar(select(core.Record).where(
                core.Record.kind == 'subscription', core.Record.owner == owner))
            record.payload = renewed
            db.commit()
        raise WebPushException('Synthetic expired old keys', response=SimpleNamespace(status_code=410))

    monkeypatch.setattr('pywebpush.webpush', renew_during_send)
    worker.process_once()
    assert task(identifier)[0] == 'pending'
    with core.DB() as db:
        record = db.scalar(select(core.Record).where(core.Record.kind == 'subscription'))
        assert record.payload == renewed


@pytest.mark.parametrize('endpoint', [None, 42, [], {}, 'http://web.push.apple.com/test', 'https://invalid.example/test'])
def test_invalid_subscription_endpoint_returns_422(signed, monkeypatch, endpoint):
    configured(monkeypatch)
    response = signed.post('/api/v1/notifications/subscriptions', json={**subscription(), 'endpoint': endpoint})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'INVALID_SUBSCRIPTION'

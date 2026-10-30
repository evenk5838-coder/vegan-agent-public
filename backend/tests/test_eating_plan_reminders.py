"""Explicit eating-plan reminders through saved-food APIs; no real search or push."""
from datetime import timedelta

from pywebpush import WebPushException
from sqlalchemy import select

from app import core, worker
from app.services import consumption_plan, storage_guidance


def source_plan(facts, days=2):
    quote = f'新鲜完整食材购买后冷藏，建议在{days}至{days + 1}天内食用。'
    source = {'id': 1, 'title': '合成提醒测试来源',
              'url': 'https://example.invalid/eating-plan', 'content': quote}
    window = consumption_plan.SourceWindow(
        source_id=1, quote=quote, minimum=float(days), maximum=float(days + 1),
        unit='days', anchor='purchase', storage='refrigerated', food_class='fresh', condition='raw')
    return consumption_plan.build(window, facts, [source], {1})


def saved_plan(signed, monkeypatch):
    response = signed.post('/api/v1/foods', json={
        'name': '合成食材', 'food_class': 'fresh', 'storage': 'refrigerated',
        'condition': 'raw', 'date_type': 'purchase', 'temperature_c': 4,
        'started_at': (core.now() - timedelta(hours=1)).isoformat(),
    })
    assert response.status_code == 201
    item = response.json()
    monkeypatch.setattr(storage_guidance, 'generate',
                        lambda owner, facts: {'consumption_plan': source_plan(facts)})
    result = signed.post(f"/api/v1/foods/{item['id']}/storage-guidance")
    assert result.status_code == 200
    assert result.json()['consumption_plan']['status'] == 'ready'
    return signed.get('/api/v1/foods').json()['items'][0]


def tasks():
    with core.DB() as db:
        return [{'id': item.id, 'status': item.status, 'payload': item.payload}
                for item in db.scalars(select(core.Job).where(core.Job.kind == 'reminder'))]


def dispatch(monkeypatch, sender=None):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(core.settings, 'vapid_private_key', 'synthetic-private-key')
    monkeypatch.setattr(core.settings, 'vapid_public_key', 'synthetic-public-key')
    monkeypatch.setattr(core.settings, 'vapid_subject', 'mailto:synthetic@example.invalid')
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
        if db.scalar(select(core.Record).where(core.Record.owner == owner,
                                               core.Record.kind == 'subscription')) is None:
            db.add(core.Record(owner=owner, kind='subscription', payload={
                'endpoint': 'https://web.push.apple.com/synthetic-plan', 'keys': {}}))
        due_dates = [job.due for job in db.scalars(select(core.Job).where(
            core.Job.kind == 'reminder', core.Job.status == 'pending'))]
        db.commit()
    clock = max([core.now(), *due_dates]) + timedelta(seconds=1)
    monkeypatch.setattr(worker, 'now', lambda: clock)
    calls = []
    def send(**kwargs):
        calls.append(kwargs)
        if sender:
            sender(**kwargs)
    monkeypatch.setattr('pywebpush.webpush', send)
    worker.process_once()
    return calls


def set_reminder(signed, item, at=None):
    return signed.put(f"/api/v1/foods/{item['id']}", json={
        'name': item['name'], 'remind_at': at or (core.now() + timedelta(hours=1)).isoformat()})


def test_generating_plan_does_not_automatically_schedule_or_subscribe(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    assert item['expires_at'] is None and item['remind_at'] is None
    assert tasks() == []
    with core.DB() as db:
        assert db.scalar(select(core.Record).where(core.Record.kind == 'subscription')) is None


def test_explicit_plan_reminder_enqueues_and_sends_without_becoming_expiry(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    response = set_reminder(signed, item)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['expires_at'] is None and saved['deadline_source'] == 'unknown'
    scheduled = tasks()
    assert len(scheduled) == 1 and scheduled[0]['status'] == 'pending'
    basis = scheduled[0]['payload']['reminder_basis']
    assert basis['kind'] == 'eating_plan'
    assert basis['target_at'] == saved['consumption_plan']['target_at']
    assert len(dispatch(monkeypatch)) == 1
    assert tasks()[0]['status'] == 'accepted'


def test_explicit_reminder_requires_valid_date_and_does_not_fake_save(signed):
    item = signed.post('/api/v1/foods', json={'name': '仅有名称'}).json()
    response = set_reminder(signed, item)
    assert response.status_code == 422
    assert signed.get('/api/v1/foods').json()['items'][0]['remind_at'] is None
    assert tasks() == []


def test_reminder_after_eating_target_is_rejected_without_writing(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    after = consumption_plan._parse(item['consumption_plan']['target_at']) + timedelta(hours=1)
    response = set_reminder(signed, item, after.isoformat())
    assert response.status_code == 422
    assert signed.get('/api/v1/foods').json()['items'][0]['remind_at'] is None
    assert tasks() == []


def test_changing_conditions_cancels_plan_reminder_without_blocking_correction(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    assert set_reminder(signed, item).status_code == 200
    response = signed.put(f"/api/v1/foods/{item['id']}", json={'name': item['name'], 'storage': 'frozen'})
    assert response.status_code == 200
    assert response.json()['consumption_plan']['status'] == 'outdated'
    assert all(row['status'] == 'cancelled' for row in tasks())
    assert dispatch(monkeypatch) == []


def test_replacing_plan_retires_old_job_and_versions_the_food(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    response = set_reminder(signed, item)
    assert response.status_code == 200
    before = response.json()
    previous_job = tasks()[0]['id']
    monkeypatch.setattr(storage_guidance, 'generate',
                        lambda owner, facts: {'consumption_plan': source_plan(facts, 3)})
    assert signed.post(f"/api/v1/foods/{item['id']}/storage-guidance").status_code == 200
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['version'] != before['version']
    assert after['expires_at'] is None
    assert next(row for row in tasks() if row['id'] == previous_job)['status'] == 'cancelled'
    for row in tasks():
        if row['status'] == 'pending':
            assert row['payload']['reminder_basis']['target_at'] == after['consumption_plan']['target_at']


def test_same_food_version_cannot_send_after_plan_target_changes(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    assert set_reminder(signed, item).status_code == 200
    # Simulate a legacy/concurrent writer that changed only the saved plan.
    with core.DB() as db:
        record = db.get(core.Record, item['id'])
        record.payload = {**record.payload, 'consumption_plan': source_plan(record.payload, 3)}
        db.commit()
    assert dispatch(monkeypatch) == []
    assert tasks()[0]['status'] == 'cancelled'


def test_old_untyped_job_cannot_borrow_a_new_eating_plan(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    assert set_reminder(signed, item).status_code == 200
    with core.DB() as db:
        job = db.scalar(select(core.Job).where(core.Job.kind == 'reminder'))
        job.payload = {key: value for key, value in job.payload.items() if key != 'reminder_basis'}
        db.commit()
    assert dispatch(monkeypatch) == []
    assert tasks()[0]['status'] == 'cancelled'


def test_new_plan_invalidates_old_pending_reminder_confirmation(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    response = signed.post('/api/v1/actions', json={
        'action': 'food_reminder', 'food_id': item['id'],
        'remind_at': (core.now() + timedelta(hours=1)).isoformat()})
    assert response.status_code == 201
    proposal = response.json()
    monkeypatch.setattr(storage_guidance, 'generate',
                        lambda owner, facts: {'consumption_plan': source_plan(facts, 3)})
    assert signed.post(f"/api/v1/foods/{item['id']}/storage-guidance").status_code == 200
    response = signed.post(f"/api/v1/actions/{proposal['record_id']}/confirm",
                           json={'confirm_key': 'user_confirmed'})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'ACTION_STALE'
    assert tasks() == []
    assert signed.get('/api/v1/foods').json()['items'][0]['remind_at'] is None


def test_refreshing_plan_does_not_resend_already_accepted_same_time(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    assert set_reminder(signed, item).status_code == 200
    assert len(dispatch(monkeypatch)) == 1
    assert tasks()[0]['status'] == 'accepted'
    monkeypatch.setattr(storage_guidance, 'generate',
                        lambda owner, facts: {'consumption_plan': source_plan(facts, 3)})
    assert signed.post(f"/api/v1/foods/{item['id']}/storage-guidance").status_code == 200
    assert len(tasks()) == 1 and tasks()[0]['status'] == 'accepted'


def test_invalid_formal_basis_cannot_fall_back_to_a_ready_plan(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    assert set_reminder(signed, item).status_code == 200
    with core.DB() as db:
        record = db.get(core.Record, item['id'])
        facts = {**record.payload, 'deadline_source': 'user', 'needs_review': True,
                 'expires_at': (core.now() + timedelta(days=4)).isoformat()}
        record.payload = {**facts, 'consumption_plan': source_plan(facts)}
        db.commit()
    assert dispatch(monkeypatch) == []
    assert tasks()[0]['status'] == 'cancelled'


def test_legacy_untyped_formal_deadline_job_remains_supported(signed, monkeypatch):
    response = signed.post('/api/v1/foods', json={
        'name': '旧日期记录', 'storage': 'refrigerated', 'condition': 'raw',
        'date_type': 'purchase', 'started_at': (core.now() - timedelta(hours=1)).isoformat(),
        'deadline_source': 'user', 'expires_at': (core.now() + timedelta(days=1)).isoformat(),
        'remind_at': (core.now() + timedelta(hours=1)).isoformat(),
    })
    assert response.status_code == 201
    with core.DB() as db:
        job = db.scalar(select(core.Job).where(core.Job.kind == 'reminder'))
        job.payload = {key: value for key, value in job.payload.items() if key != 'reminder_basis'}
        db.commit()
    assert len(dispatch(monkeypatch)) == 1
    assert tasks()[0]['status'] == 'accepted'


def test_plan_refresh_does_not_resend_after_retry_changed_job_due(signed, monkeypatch):
    item = saved_plan(signed, monkeypatch)
    assert set_reminder(signed, item).status_code == 200

    def temporary_failure(**kwargs):
        raise WebPushException('Synthetic temporary push failure')

    assert len(dispatch(monkeypatch, temporary_failure)) == 1
    assert tasks()[0]['status'] == 'pending'
    assert len(dispatch(monkeypatch)) == 1
    assert tasks()[0]['status'] == 'accepted'
    monkeypatch.setattr(storage_guidance, 'generate',
                        lambda owner, facts: {'consumption_plan': source_plan(facts, 3)})
    assert signed.post(f"/api/v1/foods/{item['id']}/storage-guidance").status_code == 200
    assert len(tasks()) == 1 and tasks()[0]['status'] == 'accepted'

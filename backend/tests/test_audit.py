"""收尾审计回归：伪造规则期限封堵、清理覆盖、越权面、错误外壳与凭据不外泄。"""
import io
import struct
from datetime import timedelta

from sqlalchemy import select

from app import core, providers, transcription, worker
from app.core import now


def cooked(**kw):
    return {'name': '熟菜', 'storage': 'refrigerated', 'location': 'indoor', 'condition': 'cooked',
            'temperature_c': 4.0, 'date_type': 'prepared',
            'started_at': (now() - timedelta(hours=4)).isoformat(),
            'deadline_source': 'unknown', 'status': 'active', **kw}


def wav_bytes(seconds=1.0, rate=16000):
    frames = int(rate * seconds)
    data = b'\x00\x00' * frames
    return (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' +
            struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) +
            b'data' + struct.pack('<I', len(data)) + data)


# ---------- 伪造规则期限 ----------
def test_post_foods_rejects_rule_deadline(signed):
    r = signed.post('/api/v1/foods', json=cooked(deadline_source='rule',
                                                 expires_at=(now() + timedelta(days=30)).isoformat()))
    assert r.status_code == 422
    assert r.json()['error']['code'] == 'INVALID_RULE_BASIS'
    assert signed.get('/api/v1/foods').json()['items'] == []


def test_draft_confirm_rejects_rule_deadline(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [cooked(
        deadline_source='rule', expires_at=(now() + timedelta(days=30)).isoformat())]}).json()
    result = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    assert result['status'] == 'partially_saved'
    assert result['results'][0]['error']['code'] == 'INVALID_RULE_BASIS'
    assert signed.get('/api/v1/foods').json()['items'] == []


def test_legacy_rule_row_without_rule_id_is_not_estimated(signed):
    """历史异常数据：rule 来源缺 rule_id 时不显示为可信估算。"""
    item = signed.post('/api/v1/foods', json=cooked()).json()
    with core.DB() as db:
        record = db.get(core.Record, item['id'])
        record.payload = {**record.payload, 'deadline_source': 'rule',
                          'expires_at': (now() + timedelta(days=10)).isoformat()}
        db.commit()
    row = signed.get('/api/v1/foods').json()['items'][0]
    assert row['assessment_status'] == 'needs_review'
    assert row['valid_deadline_at'] is None
    assert row['needs_review'] is True
    assert row['deadline_basis']['type'] == 'none'


# ---------- 清理覆盖 ----------
def test_cleanup_purges_expired_sessions_and_old_usage(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    signed.post('/api/v1/ingredients/evaluate', json={'text': '小麦粉', 'complete': True})
    with core.DB() as db:
        for session in db.scalars(select(core.Session)).all():
            session.expires = now() - timedelta(seconds=1)
        for usage in db.scalars(select(core.Record).where(core.Record.kind == 'usage')).all():
            usage.created = now() - timedelta(days=40)
        db.commit()
    worker.cleanup()
    with core.DB() as db:
        assert db.scalars(select(core.Session)).all() == []
        assert db.scalars(select(core.Record).where(core.Record.kind == 'usage')).all() == []
    assert signed.get('/api/v1/auth/me').status_code == 401  # 会话已失效，需重新登录


def test_cleanup_removes_only_empty_old_sessions(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: {'text': '好', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    empty = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    used = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    signed.post(f'/api/v1/chat/sessions/{used}/messages', json={'content': 'hi'})
    worker.process_once()
    with core.DB() as db:
        for record in db.scalars(select(core.Record).where(core.Record.kind == 'chat_session')).all():
            record.created = now() - timedelta(days=8)
        db.commit()
    worker.cleanup()
    assert signed.get(f'/api/v1/chat/sessions/{empty}/messages').status_code == 404
    assert len(signed.get(f'/api/v1/chat/sessions/{used}/messages').json()['items']) == 2


def test_cleanup_keeps_pending_reminder_jobs(signed):
    item = signed.post('/api/v1/foods', json=cooked(
        deadline_source='user', expires_at=(now() + timedelta(days=5)).isoformat(),
        remind_at=(now() + timedelta(days=1)).isoformat())).json()
    with core.DB() as db:
        for job in db.scalars(select(core.Job).where(core.Job.kind == 'reminder')).all():
            job.created = now() - timedelta(days=120)
        db.commit()
    worker.cleanup()
    # 未触发（pending）的提醒不能因保留期被清掉
    assert len(signed.get('/api/v1/notifications/tasks').json()) == 1


# ---------- 错误外壳与凭据 ----------
def test_error_envelope_shape_everywhere(signed, monkeypatch):
    cases = [
        ('GET', '/api/v1/recipes?meal=brunch', None),
        ('POST', '/api/v1/chat/sessions/x/messages', {'content': 'a'}),
        ('GET', '/api/v1/foods/drafts/nope', None),
        ('POST', '/api/v1/dates/parse', {'phrase': '???'}),
        ('GET', '/api/v1/map/proxy/v1/admin', None),
    ]
    for method, path, body in cases:
        r = signed.request(method, path, json=body) if body is not None else signed.request(method, path)
        assert r.status_code >= 400
        payload = r.json()
        assert set(payload) == {'error'}
        assert set(payload['error']) >= {'code', 'message'}
        assert 'Traceback' not in r.text and '/Users/' not in r.text


def test_responses_never_include_server_credentials(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'amap_api_key', 'SECRET-WEB-KEY')
    monkeypatch.setattr(core.settings, 'amap_js_key', 'PUBLIC-JS-KEY')
    monkeypatch.setattr(core.settings, 'amap_js_security_code', 'SECRET-JSCODE')
    monkeypatch.setattr(core.settings, 'amap_service_host', '/api/v1/map/proxy')
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'SECRET-LLM-KEY')
    monkeypatch.setattr(core.settings, 'vapid_private_key', 'SECRET-VAPID')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', False)
    bodies = [signed.get(p).text for p in ('/api/v1/capabilities', '/api/v1/map/config',
                                           '/api/v1/capabilities/transcription',
                                           '/api/v1/notifications/config')]
    for secret in ('SECRET-WEB-KEY', 'SECRET-JSCODE', 'SECRET-LLM-KEY', 'SECRET-VAPID'):
        assert all(secret not in body for body in bodies), secret


# ---------- 越权面（逐端点抽样） ----------
def test_cross_account_access_denied_on_all_user_resources(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: {'text': 'ok', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    ing = signed.post('/api/v1/ingredients/evaluate', json={'text': '小麦粉', 'complete': True}).json()
    recipe = signed.post('/api/v1/recipes', json={'title': 'r', 'ingredients': '豆腐', 'steps': 's',
                                                 'confirmed': True}).json()
    food = signed.post('/api/v1/foods', json=cooked()).json()
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{'name': '豆腐'}]}).json()
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'hi'}).json()['turn_id']
    with core.DB() as db:
        alice = db.scalar(select(core.User))
        photo = core.Job(owner=alice.id, kind='photo', status='pending', payload={})
        db.add(photo)
        db.commit()
        photo_id = photo.id
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    checks = [
        ('DELETE', f"/api/v1/ingredients/history/{ing['id']}", None),
        ('DELETE', f"/api/v1/recipes/{recipe['id']}", None),
        ('PUT', f"/api/v1/foods/{food['id']}", cooked()),
        ('DELETE', f"/api/v1/foods/{food['id']}", None),
        ('POST', f"/api/v1/foods/{food['id']}/reevaluate", {'evidence': {'deadline_source': 'user',
                                                                        'expires_at': now().isoformat()}}),
        ('GET', f"/api/v1/foods/drafts/{draft['draft_id']}", None),
        ('POST', f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", {}),
        ('GET', f"/api/v1/chat/sessions/{sid}/messages", None),
        ('GET', f"/api/v1/chat/sessions/{sid}/turns/{turn}", None),
        ('DELETE', f"/api/v1/chat/sessions/{sid}", None),
        ('GET', f"/api/v1/jobs/{photo_id}", None),
    ]
    for method, path, body in checks:
        r = signed.request(method, path, json=body) if body is not None else signed.request(method, path)
        assert r.status_code in (401, 404), (method, path, r.status_code)
    # 他人数据未受影响
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/login', json={'username': 'alice', 'password': 'correct-horse-123'})
    assert len(signed.get('/api/v1/recipes').json()['items']) == 1
    assert len(signed.get('/api/v1/foods').json()['items']) == 1

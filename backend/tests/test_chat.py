"""对话子阶段工程验证：仅验证存储、状态、幂等、所有权与恢复。
不验证真实模型输出；generate_chat_reply 被替换为固定回复，仅为工程行为验证。"""
from sqlalchemy import select

from app import core, providers, worker
from app.core import Job


def chat_session(client):
    return client.post('/api/v1/chat/sessions', json={}).json()


def enable_chat(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def canned_reply(monkeypatch, reply=None):
    reply = reply or {'text': '好的，这里是示例回复。', 'needs_input': False, 'input_prompt': '', 'cards': []}
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda history: reply)


def test_chat_unconfigured_returns_503(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', False)
    sid = chat_session(signed)['id']
    r = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '你好'})
    assert r.status_code == 503
    assert r.json()['error']['code'] == 'CHAT_NOT_CONFIGURED'


def test_chat_turn_lifecycle_and_persistence(signed, monkeypatch):
    enable_chat(monkeypatch)
    canned_reply(monkeypatch)
    sid = chat_session(signed)['id']
    r = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '我想找一个鹰嘴豆早餐'})
    assert r.status_code == 201
    turn_id = r.json()['turn_id']
    assert r.json()['status'] == 'queued'
    assert signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['status'] == 'queued'
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'succeeded'
    assert turn['reply']['text'] == '好的，这里是示例回复。'
    msgs = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    assert [m['role'] for m in msgs] == ['user', 'assistant']
    assert msgs[1]['content_type'] == 'cards'
    sessions = signed.get('/api/v1/chat/sessions').json()['items']
    assert any(s['id'] == sid and s['message_count'] == 2 for s in sessions)


def test_chat_single_active_turn(signed, monkeypatch):
    enable_chat(monkeypatch)
    canned_reply(monkeypatch)
    sid = chat_session(signed)['id']
    assert signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '第一句'}).status_code == 201
    r = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '第二句'})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'TURN_ACTIVE'
    worker.process_once()
    assert signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '第二句'}).status_code == 201


def test_chat_idempotency(signed, monkeypatch):
    enable_chat(monkeypatch)
    canned_reply(monkeypatch)
    sid = chat_session(signed)['id']
    first = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '重复消息', 'idempotency_key': 'k1'}).json()
    worker.process_once()
    dup = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '重复消息', 'idempotency_key': 'k1'})
    assert dup.status_code == 200
    assert dup.json()['turn_id'] == first['turn_id']
    conflict = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '不同消息', 'idempotency_key': 'k1'})
    assert conflict.status_code == 409
    assert conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert len(signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']) == 2


def test_chat_photo_attachment_ownership(signed, monkeypatch):
    enable_chat(monkeypatch)
    canned_reply(monkeypatch)
    with core.DB() as db:
        alice = db.scalar(select(core.User).where(core.User.username == 'alice'))
        job = Job(owner=alice.id, kind='photo', status='pending', payload={'asset_id': 'x'})
        db.add(job)
        db.commit()
        job_id = job.id
    sid = chat_session(signed)['id']
    # 本人 photo job 可关联（校验所有权与种类，不校验图片处理状态）
    r = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '看这张配料表', 'attachments': [{'kind': 'ingredient_photo', 'job_id': job_id}]})
    assert r.status_code == 201
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    sid2 = chat_session(signed)['id']
    r2 = signed.post(f'/api/v1/chat/sessions/{sid2}/messages', json={'content': '偷看', 'attachments': [{'kind': 'ingredient_photo', 'job_id': job_id}]})
    assert r2.status_code == 404
    assert r2.json()['error']['code'] == 'NOT_FOUND'


def test_chat_turn_recovery_after_interrupt(signed, monkeypatch):
    enable_chat(monkeypatch)
    sid = chat_session(signed)['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '断线后的消息'})
    with core.DB() as db:
        turn = db.scalar(select(Job).where(Job.kind == 'chat_turn'))
        turn.status = 'running'
        db.commit()
        turn_id = turn.id
    worker.recover_and_clean()
    assert signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['status'] == 'queued'
    canned_reply(monkeypatch)
    worker.process_once()
    assert signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['status'] == 'succeeded'


def test_chat_session_delete_removes_messages_and_cancels_turn(signed, monkeypatch):
    enable_chat(monkeypatch)
    canned_reply(monkeypatch)
    sid = chat_session(signed)['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '将被删除'})
    worker.process_once()
    assert len(signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']) == 2
    assert signed.delete(f'/api/v1/chat/sessions/{sid}').json()['ok'] is True
    assert signed.get(f'/api/v1/chat/sessions/{sid}/messages').status_code == 404
    assert signed.get('/api/v1/chat/sessions').json()['items'] == []


def test_chat_session_isolation(signed):
    sid = chat_session(signed)['id']
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get(f'/api/v1/chat/sessions/{sid}/messages').status_code == 404
    assert signed.delete(f'/api/v1/chat/sessions/{sid}').status_code == 404


def test_delete_while_reply_in_flight_does_not_restore_content(signed, monkeypatch):
    enable_chat(monkeypatch)
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '删除竞态输入'}).json()['turn_id']

    def delayed_reply(history):
        # 在模型“返回”前删除会话，模拟迟到的回复。
        assert signed.delete(f'/api/v1/chat/sessions/{sid}').status_code == 200
        return {'text': '迟到的私密回复', 'needs_input': False, 'input_prompt': '', 'cards': []}

    monkeypatch.setattr(providers, 'generate_chat_reply', delayed_reply)
    worker.process_once()
    with core.DB() as db:
        assert db.get(core.Record, sid) is None
        turn = db.get(core.Job, turn_id)
        assert turn.status == 'cancelled'
        assert not turn.result.get('reply')
        orphans = [r for r in db.scalars(select(core.Record).where(core.Record.kind == 'chat_message'))
                   if r.payload.get('session_id') == sid]
        assert orphans == []


def test_delete_completed_session_removes_turn_reply_copy(signed, monkeypatch):
    enable_chat(monkeypatch)
    canned_reply(monkeypatch, {'text': '私密回复副本', 'needs_input': False, 'input_prompt': '', 'cards': []})
    sid = chat_session(signed)['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '删除前输入'}).json()['turn_id']
    worker.process_once()
    assert signed.delete(f'/api/v1/chat/sessions/{sid}').status_code == 200
    with core.DB() as db:
        turn = db.get(core.Job, turn_id)
        assert turn is None or not turn.result.get('reply')


def test_delete_one_session_does_not_affect_other_sessions(signed, monkeypatch):
    enable_chat(monkeypatch)
    canned_reply(monkeypatch)
    s1 = chat_session(signed)['id']
    s2 = chat_session(signed)['id']
    signed.post(f'/api/v1/chat/sessions/{s1}/messages', json={'content': '会话一'})
    signed.post(f'/api/v1/chat/sessions/{s2}/messages', json={'content': '会话二'})
    worker.process_once()
    worker.process_once()
    signed.delete(f'/api/v1/chat/sessions/{s1}')
    assert len(signed.get(f'/api/v1/chat/sessions/{s2}/messages').json()['items']) == 2
    assert [s['id'] for s in signed.get('/api/v1/chat/sessions').json()['items']] == [s2]

"""闭环工程验证：对话+配料附件、菜谱筛选、草稿确认、重评、语音不自动发消息、
错误终态、断线/重启恢复与到期清理。全部离线模拟，无真实外部调用。"""
import io
import struct
from datetime import timedelta

from sqlalchemy import select

from app import core, providers, transcription, worker


def wav_bytes(seconds=1.0, rate=16000):
    frames = int(rate * seconds)
    data = b'\x00\x00' * frames
    return (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' +
            struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) +
            b'data' + struct.pack('<I', len(data)) + data)


def enable_chat(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def test_chat_attachment_photo_produces_ingredient_card(signed, monkeypatch):
    """对话 + 配料附件 → 固定规则结果卡片（附件图片任务已完成）。"""
    enable_chat(monkeypatch)
    with core.DB() as db:
        alice = db.scalar(select(core.User))
        job = core.Job(owner=alice.id, kind='photo', status='succeeded',
                       payload={'history_id': 'h1'},
                       result={'history_id': 'h1', 'results': {'vegan': {'status': 'not_suitable'}}})
        db.add(job)
        db.commit()
        job_id = job.id
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: {'text': '含奶粉，Vegan 不符合。', 'needs_input': False,
                                   'input_prompt': '', 'tool_calls': []})
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                          json={'content': '这包能吃吗',
                                'attachments': [{'kind': 'ingredient_photo', 'job_id': job_id}]}).json()['turn_id']
    worker.process_once()
    turn = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()
    assert turn['status'] == 'succeeded'
    cards = [c for c in turn['reply']['cards'] if c['type'] == 'ingredient_result']
    assert cards and cards[0]['payload']['result']['results']['vegan']['status'] == 'not_suitable'


def test_chat_pending_photo_requeues_then_completes(signed, monkeypatch):
    """附件图片仍在处理 → turn 重新排队，完成后继续（断线恢复语义）。"""
    enable_chat(monkeypatch)
    with core.DB() as db:
        alice = db.scalar(select(core.User))
        job = core.Job(owner=alice.id, kind='photo', status='pending', payload={})
        db.add(job)
        db.commit()
        job_id = job.id
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages',
                          json={'content': '看图',
                                'attachments': [{'kind': 'ingredient_photo', 'job_id': job_id}]}).json()['turn_id']
    worker.process_once()
    assert signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['status'] == 'queued'
    with core.DB() as db:
        photo = db.get(core.Job, job_id)
        photo.status = 'succeeded'
        photo.result = {'history_id': 'h1', 'results': {'vegan': {'status': 'suitable'}}}
        db.commit()
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: {'text': '符合。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []})
    worker.process_once()
    assert signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['status'] == 'succeeded'


def test_recipe_filter_then_draft_then_reevaluate_loop(signed, published_rules):
    """条件筛选→菜谱匹配；草稿→确认；重评→规则期限，串成一条链路。"""
    signed.post('/api/v1/recipes', json={'title': '鹰嘴豆早餐', 'ingredients': '鹰嘴豆、橄榄油',
                                         'steps': '煮熟拌匀。', 'meal': 'breakfast', 'confirmed': True})
    local = signed.post('/api/v1/recipes/search', json={'query': '鹰嘴豆', 'meal': 'breakfast'}).json()
    assert local['local'] is True and len(local['items']) == 1
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{
        'name': '蔬菜汤', 'food_class': 'cooked', 'storage': 'refrigerated', 'condition': 'cooked', 'temperature_c': 4,
        'date_type': 'prepared', 'started_at': (core.now() - timedelta(hours=3)).isoformat()}]}).json()
    assert signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()['status'] == 'confirmed'
    food = signed.get('/api/v1/foods').json()['items'][0]
    ruled = signed.post(f"/api/v1/foods/{food['id']}/reevaluate",
                        json={'evidence': {'deadline_source': 'rule'}}).json()
    assert ruled['assessment_status'] == 'estimated'
    assert ruled['deadline_basis']['type'] == 'rule'


def test_error_terminal_state_and_restart_recovery(signed, monkeypatch):
    enable_chat(monkeypatch)
    monkeypatch.setattr(providers, 'generate_chat_reply',
                        lambda h: (_ for _ in ()).throw(core.AppError('UPSTREAM_FAILED', '模拟上游失败')))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': 'x'}).json()['turn_id']
    for _ in range(3):
        with core.DB() as db:
            task = db.get(core.Job, turn_id)
            task.due = core.now() - timedelta(seconds=1)
            db.commit()
        worker.process_once()
    assert signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['status'] == 'failed'
    with core.DB() as db:
        task = db.get(core.Job, turn_id)
        task.status = 'running'
        db.commit()
    worker.recover_and_clean()
    assert signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['status'] == 'queued'


def test_expiry_cleanup_covers_chat_and_drafts(signed):
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{'name': '豆腐'}]}).json()
    with core.DB() as db:
        for record in db.scalars(select(core.Record).where(
                core.Record.kind.in_(['chat_session', 'food_draft']))).all():
            record.expires = core.now() - timedelta(seconds=1)
        db.commit()
    worker.cleanup()
    assert signed.get(f'/api/v1/chat/sessions/{sid}/messages').status_code == 404
    assert signed.get(f"/api/v1/foods/drafts/{draft['draft_id']}").status_code == 404


def test_voice_does_not_auto_send_message(signed, monkeypatch):
    enable_chat(monkeypatch)
    monkeypatch.setattr(core.settings, 'transcription_api_key', 'k')
    monkeypatch.setattr(core.settings, 'transcription_endpoint', transcription.DEFAULT_ENDPOINT)
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(transcription, 'transcribe',
                        lambda *a: {'text': '今天买了豆腐', 'simulated': True,
                                    'transcribed_at': core.now().isoformat()})
    r = signed.post('/api/v1/transcriptions', files={'file': ('v.wav', io.BytesIO(wav_bytes()), 'audio/wav')})
    worker.process_once()
    text = signed.get(f"/api/v1/transcriptions/{r.json()['job_id']}").json()['text']
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    assert signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items'] == []
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': text + '（已编辑）'})
    items = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    assert len(items) == 1 and items[0]['role'] == 'user'

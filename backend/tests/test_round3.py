"""2026-10-24 第二轮工程验证：规则发布门槛、公共知识接入检索、百炼语音协议适配。"""
import base64
import copy
import io
import json
import struct
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import core, knowledge, providers, storage_rules, transcription, worker


def cooked(**kw):
    return {'name': '蔬菜汤', 'food_class': 'cooked', 'storage': 'refrigerated', 'location': 'indoor', 'condition': 'cooked',
            'temperature_c': 4.0, 'date_type': 'prepared',
            'started_at': (core.now() - timedelta(hours=4)).isoformat(),
            'deadline_source': 'unknown', 'status': 'active', **kw}


def enable_voice(monkeypatch):
    monkeypatch.setattr(core.settings, 'transcription_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'transcription_endpoint', transcription.DEFAULT_ENDPOINT)
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def wav_bytes(seconds=1.0, rate=16000):
    frames = int(rate * seconds)
    data = b'\x00\x00' * frames
    return (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' +
            struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) +
            b'data' + struct.pack('<I', len(data)) + data)


# ---------- 1 储存规则发布门槛 ----------
def test_draft_rule_cannot_generate_official_deadline(signed):
    """未发布规则：不给正式期限、不排提醒，落库为待核对。"""
    item = signed.post('/api/v1/foods', json=cooked()).json()
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'rule'}})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'RULE_NOT_PUBLISHED'
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['valid_deadline_at'] is None
    assert after['estimated_remaining_days'] is None
    assert after['assessment_status'] == 'needs_review'
    assert after['deadline_basis']['type'] == 'none'
    assert signed.get('/api/v1/notifications/tasks').json() == []


def test_draft_rule_does_not_cancel_or_create_reminder(signed):
    """即使原先有提醒，草稿规则也不能被用来安排正式提醒。"""
    item = signed.post('/api/v1/foods', json=cooked(
        deadline_source='user', expires_at=(core.now() + timedelta(hours=4)).isoformat(),
        remind_at=(core.now() + timedelta(hours=1)).isoformat())).json()
    assert len(signed.get('/api/v1/notifications/tasks').json()) == 1
    signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                json={'evidence': {'deadline_source': 'rule'}})
    tasks = signed.get('/api/v1/notifications/tasks').json()
    assert all(t['status'] == 'cancelled' for t in tasks)


def test_published_rule_generates_deadline_and_reminder(signed, published_rules):
    item = signed.post('/api/v1/foods', json=cooked()).json()
    data = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                       json={'evidence': {'deadline_source': 'rule',
                                          'remind_at': (core.now() + timedelta(hours=2)).isoformat()}}).json()
    assert data['assessment_status'] == 'estimated'
    assert data['deadline_basis']['rules_status'] == 'published'
    assert len(signed.get('/api/v1/notifications/tasks').json()) == 1


def test_package_and_user_evidence_unaffected_by_gate(signed):
    item = signed.post('/api/v1/foods', json=cooked()).json()
    target = core.now() + timedelta(days=2)
    package = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                          json={'evidence': {'deadline_source': 'package',
                                             'package_note': '未开封冷藏标示',
                                             'expires_at': target.isoformat()}}).json()
    assert package['assessment_status'] == 'estimated' and package['deadline_basis']['type'] == 'package'
    user = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                       json={'evidence': {'deadline_source': 'user',
                                          'expires_at': (core.now() + timedelta(hours=6)).isoformat()}}).json()
    assert user['assessment_status'] == 'user_set'


def test_storage_rules_catalog_exposes_gate(signed):
    data = signed.get('/api/v1/storage/rules').json()
    assert data['usable_for_estimates'] is False
    assert data['published_rule_ids'] == []
    assert '仅已发布' in data['publish_gate']
    assert all(r['status'] == 'draft' and r['usable_for_estimates'] is False for r in data['rules'])


def test_published_gate_reflects_catalog(signed, published_rules):
    data = signed.get('/api/v1/storage/rules').json()
    assert data['usable_for_estimates'] is True
    assert set(data['published_rule_ids']) == {r['rule_id'] for r in published_rules}


# ---------- 2 公共知识接入检索 ----------
def _publish_recipe_entry():
    knowledge.import_entries([{
        'type': 'recipe', 'title': '豆腐碎炒：来源事实摘要',
        'source': 'https://www.vegansociety.com/resources/recipes/quick-and-easy/tofu-scramble',
        'body': '主要材料为硬豆腐、洋葱、番茄、植物油和蒜。含五辛，不能标为纯净素。',
        'conditions': {'meal': 'breakfast', 'cuisine': 'western', 'servings': 2,
                       'contains_pungents': True, 'summary_only': True},
        'status': 'published'}])
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
        entry_id = row.payload['entry_id']
    knowledge.set_status(entry_id, 'published')
    return entry_id


def test_public_recipe_appears_in_list_and_is_marked_summary(signed):
    _publish_recipe_entry()
    data = signed.get('/api/v1/recipes?meal=breakfast').json()
    assert data['public_count'] == 1
    item = data['items'][0]
    assert item['scope'] == 'public' and item['origin'] == 'public_knowledge'
    assert item['tutorial_complete'] is False
    assert item['tutorial_status'] == 'source_summary'
    assert '不是完整教程' in item['notice']
    assert item['source_url'].startswith('https://')


def test_public_recipe_search_and_chat_tool(signed, monkeypatch):
    _publish_recipe_entry()
    local = signed.post('/api/v1/recipes/search', json={'query': '豆腐', 'meal': 'breakfast'}).json()
    assert local['local'] is True
    assert any(i['scope'] == 'public' for i in local['items'])
    assert local['public_notice']

    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    replies = iter([
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'search_recipes', 'arguments': {'query': '豆腐'}}]},
        {'text': '找到一条公共来源摘要。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(replies))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '找豆腐菜'}).json()['turn_id']
    worker.process_once()
    cards = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['reply']['cards']
    rows = [c for c in cards if c['type'] == 'recipe_list'][0]['payload']['items']
    assert any(r.get('scope') == 'public' and r['tutorial_complete'] is False for r in rows)


def test_unpublished_knowledge_recipe_not_in_business_search(signed):
    knowledge.import_entries([{'type': 'recipe', 'title': '待审公共菜谱', 'source': 'https://example.org/x',
                               'body': '摘要'}])
    data = signed.get('/api/v1/recipes').json()
    assert data['public_count'] == 0
    assert signed.post('/api/v1/recipes/search', json={'query': '待审公共菜谱'}).status_code == 503


def test_personal_recipes_stay_account_scoped(signed):
    signed.post('/api/v1/recipes', json={'title': '我的番茄豆腐', 'ingredients': '番茄、豆腐',
                                         'steps': '炒熟', 'confirmed': True})
    _publish_recipe_entry()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    data = signed.get('/api/v1/recipes').json()
    assert data['personal_count'] == 0            # 他人个人菜谱不可见
    assert data['public_count'] == 1              # 公共条目可读
    assert all(i['scope'] == 'public' for i in data['items'])


# ---------- 3 百炼语音协议 ----------
def test_format_detected_from_bytes_not_extension():
    assert transcription.detect_format(wav_bytes()) == 'audio/wav'
    assert transcription.detect_format(b'\x1aE\xdf\xa3' + b'0' * 32) == 'audio/webm'
    assert transcription.detect_format(b'\x00\x00\x00\x18ftypM4A ' + b'0' * 32) == 'audio/mp4'
    assert transcription.detect_format(b'garbage') is None


def test_bailian_request_is_json_base64_with_output_text():
    content = wav_bytes(seconds=0.5)
    body = transcription.build_request(content, 'audio/wav', 0.5)
    assert body['model'] == 'qwen-audio-3.0-asr-flash'
    audio = body['input']['messages'][0]['content'][0]
    assert audio['type'] == 'input_audio'
    uri = audio['input_audio']['data']
    assert uri.startswith('data:audio/wav;base64,')
    assert base64.b64decode(uri.split(',', 1)[1]) == content
    assert 'multipart' not in json.dumps(body)
    parsed = transcription.parse_response({'output': {'text': ' 你好 '},
                                           'usage': {'seconds': 0.5}})
    assert parsed['text'] == '你好'
    assert parsed['provider'] == 'aliyun-bailian'
    assert parsed['region'] == 'cn-beijing'
    assert parsed['usage']['seconds'] == 0.5


def test_bailian_invalid_response_shape():
    with pytest.raises(core.AppError) as exc:
        transcription.parse_response({'output': {}})
    assert exc.value.code == 'TRANSCRIPTION_INVALID'
    with pytest.raises(core.AppError) as empty:
        transcription.parse_response({'output': {'text': '   '}})
    assert empty.value.code == 'TRANSCRIPTION_EMPTY'


def test_upload_limits_10mb_and_5_minutes(monkeypatch):
    enable_voice(monkeypatch)
    big = b'RIFF' + b'0' * (10 * 1024 * 1024)
    with pytest.raises(core.AppError) as e:
        transcription.validate_upload(big, 'audio/wav', 10)
    assert e.value.code == 'FILE_TOO_LARGE'
    long_audio = wav_bytes(seconds=301)
    with pytest.raises(core.AppError) as e2:
        transcription.validate_upload(long_audio, 'audio/wav')
    assert e2.value.code == 'AUDIO_TOO_LONG'
    ok = transcription.validate_upload(wav_bytes(seconds=2), 'audio/wav')
    assert ok['detected_mime'] == 'audio/wav'
    assert 1.9 < ok['duration_seconds'] < 2.1
    assert ok['base64_bytes'] <= transcription.MAX_BASE64_BYTES


def test_duration_required_when_header_unreadable():
    webm = b'\x1aE\xdf\xa3' + b'0' * 64
    with pytest.raises(core.AppError) as e:
        transcription.validate_upload(webm, 'audio/webm')
    assert e.value.code == 'DURATION_REQUIRED'
    ok = transcription.validate_upload(webm, 'audio/webm', 12.5)
    assert ok['duration_seconds'] == 12.5


def test_transcription_endpoint_end_to_end_with_mock_upstream(signed, monkeypatch):
    enable_voice(monkeypatch)
    captured = {}

    def fake_request_json(method, url, **kwargs):
        captured['url'] = url
        captured['body'] = kwargs.get('json')
        return {'output': {'text': '昨天买了豆腐'}, 'usage': {'seconds': 1.0}}

    monkeypatch.setattr(providers, 'request_json', fake_request_json)
    r = signed.post('/api/v1/transcriptions',
                    files={'file': ('v.wav', io.BytesIO(wav_bytes(seconds=1)), 'audio/wav')})
    assert r.status_code == 202
    job = r.json()
    assert job['detected_mime'] == 'audio/wav'
    with core.DB() as db:
        filename = db.get(core.Job, job['job_id']).payload['filename']
    uploaded = core.settings.data_dir / 'audio' / filename
    assert uploaded.is_file()
    worker.process_once()
    data = signed.get(f"/api/v1/transcriptions/{job['job_id']}").json()
    assert data['text'] == '昨天买了豆腐'
    assert data['simulated'] is False          # 协议路径产物，非模拟标记
    assert captured['url'] == transcription.DEFAULT_ENDPOINT
    assert 'input' in captured['body'] and 'messages' in captured['body']['input']
    # 录音清理 + 不自动发送消息
    assert not uploaded.exists()  # 不把其他测试遗留且尚未过期的失败录音算成本任务文件。
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    assert signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items'] == []


def test_capabilities_expose_provider_and_limits(signed):
    caps = signed.get('/api/v1/capabilities/transcription').json()
    assert caps['provider'] == 'aliyun-bailian'
    assert caps['region'] == 'cn-beijing'
    assert caps['max_seconds'] == 300
    assert caps['max_bytes'] == 10485760
    assert caps['max_base64_bytes'] > caps['max_bytes']
    assert caps['mode'] == 'async_job_editable_text'


def test_candidate_knowledge_file_is_structurally_valid_and_unpublished():
    """首批候选文件只做结构校验：全部 draft、带来源与条件，不写入任何数据库。"""
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / 'docs/知识库/候选/首批知识条目-2026-10-24.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['published'] is False
    assert data['human_reviewer'] is None
    entries = data['entries']
    assert len(entries) == 7
    assert all(e['type'] != 'recipe' for e in entries)
    for entry in entries:
        clean = knowledge.validate_entry(entry)
        assert clean['status'] == 'draft'
        assert clean['source']
        assert clean['conditions']
    with core.DB() as db:
        assert db.scalars(select(core.Record).where(core.Record.kind == 'knowledge')).all() == []

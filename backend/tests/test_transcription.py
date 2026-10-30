"""语音转写工程验证：未配置出口、上传校验、异步任务、可编辑文本、清理与不自动成消息。
真实转写服务未接入；测试中的转写结果由 mock 生成并标记 simulated。"""
import io
import struct

from sqlalchemy import select

from app import core, transcription, worker


def wav_bytes(seconds=1.0, rate=16000):
    """构造最小合法 WAV，用于真实格式/时长校验（不是真实录音）。"""
    frames = int(rate * seconds)
    data = b'\x00\x00' * frames
    header = (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' +
              struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) +
              b'data' + struct.pack('<I', len(data)))
    return header + data


def enable(monkeypatch):
    monkeypatch.setattr(core.settings, 'transcription_provider', 'bailian')
    monkeypatch.setattr(core.settings, 'transcription_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'transcription_endpoint', transcription.DEFAULT_ENDPOINT)
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def upload(client, content=None, mime='audio/wav', name='voice.wav'):
    content = wav_bytes() if content is None else content
    return client.post('/api/v1/transcriptions', files={'file': (name, io.BytesIO(content), mime)})


def test_unconfigured_returns_503_and_no_charge(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'transcription_api_key', '')
    monkeypatch.setattr(core.settings, 'transcription_endpoint', '')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', False)
    r = upload(signed)
    assert r.status_code == 503
    assert r.json()['error']['code'] == 'NOT_CONFIGURED'
    caps = signed.get('/api/v1/capabilities').json()
    assert caps['transcription_configured'] is False
    assert caps['transcription']['status'].startswith('按供应商官方上限')


def test_upload_validation(signed, monkeypatch):
    enable(monkeypatch)
    # 后缀/声明无法决定真实格式：按文件头判断
    assert upload(signed, content=b'not-audio', mime='audio/wav').status_code == 422
    assert upload(signed, content=b'').status_code == 422
    r = upload(signed, content=b'not-audio', mime='audio/wav')
    assert r.json()['error']['code'] == 'INVALID_AUDIO'


def test_transcription_flow_returns_editable_text_without_chat_message(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(transcription, 'transcribe',
                        lambda *a: {'text': '今天买了豆腐和西兰花', 'simulated': True,
                                'transcribed_at': core.now().isoformat()})
    r = upload(signed)
    assert r.status_code == 202
    job = r.json()
    assert job['status'] == 'pending'
    worker.process_once()
    data = signed.get(f"/api/v1/transcriptions/{job['job_id']}").json()
    assert data['status'] == 'succeeded'
    assert data['text'] == '今天买了豆腐和西兰花'
    assert data['simulated'] is True  # 模拟结果必须显式标记
    assert data['expires_at']
    # 转写成功不自动创建聊天消息
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    assert signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items'] == []


def test_audio_file_cleared_after_success(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(transcription, 'transcribe',
                        lambda *a: {'text': '文字', 'simulated': True, 'transcribed_at': core.now().isoformat()})
    upload(signed)
    worker.process_once()
    assert not list((core.settings.data_dir / 'audio').glob('*'))


def test_failed_transcription_keeps_bounded_temp_and_reports_error(signed, monkeypatch):
    enable(monkeypatch)

    def boom(*a):
        raise core.AppError('UPSTREAM_FAILED', '语音转写未成功，请重试或改用打字。')

    monkeypatch.setattr(transcription, 'transcribe', boom)
    job = upload(signed).json()
    worker.process_once()
    data = signed.get(f"/api/v1/transcriptions/{job['job_id']}").json()
    assert data['status'] == 'failed'
    assert data['error']['code'] == 'UPSTREAM_FAILED'
    with core.DB() as db:
        asset = db.scalar(select(core.Record).where(core.Record.kind == 'audio'))
        assert asset is not None
        # 失败残留最迟 24 小时清理（记录已带过期时间）
        assert (asset.expires - core.now()).total_seconds() <= 24 * 3600


def test_unsent_transcript_text_cleaned_after_24h(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(transcription, 'transcribe',
                        lambda *a: {'text': '未发送的文字', 'simulated': True, 'transcribed_at': core.now().isoformat()})
    job = upload(signed).json()
    worker.process_once()
    assert signed.get(f"/api/v1/transcriptions/{job['job_id']}").json()['text'] == '未发送的文字'
    from datetime import timedelta
    with core.DB() as db:
        task = db.get(core.Job, job['job_id'])
        task.created = core.now() - timedelta(hours=25)
        db.commit()
    worker.cleanup()
    assert signed.get(f"/api/v1/transcriptions/{job['job_id']}").status_code == 404


def test_transcription_is_account_scoped(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(transcription, 'transcribe',
                        lambda *a: {'text': '私密文字', 'simulated': True, 'transcribed_at': core.now().isoformat()})
    job = upload(signed).json()
    worker.process_once()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get(f"/api/v1/transcriptions/{job['job_id']}").status_code == 404


def test_transcription_counts_toward_usage(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(transcription, 'transcribe',
                        lambda *a: {'text': '文字', 'simulated': True, 'transcribed_at': core.now().isoformat()})
    upload(signed)
    with core.DB() as db:
        usage = [r.payload.get('operation') for r in db.scalars(
            select(core.Record).where(core.Record.kind == 'usage'))]
    assert 'transcription' in usage


def test_restart_does_not_automatically_resend_an_interrupted_transcription(signed, monkeypatch):
    enable(monkeypatch)
    job = upload(signed).json()
    with core.DB() as db:
        task = db.get(core.Job, job['job_id'])
        task.status, task.attempts = 'running', 1
        db.commit()

    def must_not_resend(*args):
        raise AssertionError('Restart must not repeat a possibly charged transcription')

    monkeypatch.setattr(transcription, 'transcribe', must_not_resend)
    worker.recover_and_clean()
    result = signed.get(f"/api/v1/transcriptions/{job['job_id']}").json()
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'INTERRUPTED'
    assert worker.process_once() is False


def test_upload_storage_filename_uses_detected_format_instead_of_user_path(signed, monkeypatch):
    enable(monkeypatch)
    response = upload(signed, name='voice.wav/not-a-directory')
    assert response.status_code == 202
    with core.DB() as db:
        task = db.get(core.Job, response.json()['job_id'])
        filename = task.payload['filename']
    assert filename.endswith('.wav') and '/' not in filename and '\\' not in filename
    assert (core.settings.data_dir / 'audio' / filename).is_file()

"""AI HubMix STT: synthetic audio, isolated jobs and offline HTTP serialization only."""
from email import policy
from email.parser import BytesParser
import io
import json
import struct

import httpx
import pytest
from sqlalchemy import select

from app import core, providers, transcription, worker

REAL_HTTPX_CLIENT = httpx.Client


def enable(monkeypatch, model=''):
    monkeypatch.setattr(core.settings, 'transcription_provider', 'aihubmix')
    monkeypatch.setattr(core.settings, 'transcription_api_key', 'synthetic-aihubmix-key')
    monkeypatch.setattr(core.settings, 'transcription_endpoint', transcription.AIHUBMIX_ENDPOINT)
    monkeypatch.setattr(core.settings, 'transcription_model', model)
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def wav():
    samples = b'\0\0' * 16000
    return (b'RIFF' + struct.pack('<I', 36 + len(samples)) + b'WAVEfmt ' +
            struct.pack('<IHHIIHH', 16, 1, 1, 16000, 32000, 2, 16) +
            b'data' + struct.pack('<I', len(samples)) + samples)


def transport(monkeypatch, handler):
    def offline_client(*args, **kwargs):
        assert kwargs.get('follow_redirects') is False
        return REAL_HTTPX_CLIENT(*args, **kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, 'Client', offline_client)


@pytest.mark.parametrize('model', ['', 'whisper-large-v3', 'whisper-1'])
def test_multipart_wire_has_safe_filename_chinese_parameters_and_no_fabricated_usage(monkeypatch, model):
    enable(monkeypatch, model)
    requests = []
    audio = wav()

    def upstream(request):
        requests.append(request)
        assert str(request.url) == transcription.AIHUBMIX_ENDPOINT
        assert request.method == 'POST'
        assert request.headers['Authorization'] == 'Bearer synthetic-aihubmix-key'
        assert request.headers['Content-Type'].startswith('multipart/form-data; boundary=')
        assert 'X-DashScope-SSE' not in request.headers
        message = BytesParser(policy=policy.default).parsebytes(
            b'Content-Type: ' + request.headers['Content-Type'].encode() + b'\r\n\r\n' + request.content)
        fields = {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}
        assert fields['file'].get_filename() == 'voice.wav'
        assert fields['file'].get_content_type() == 'audio/wav'
        assert fields['file'].get_payload(decode=True) == audio
        values = {key: part.get_payload(decode=True).decode() for key, part in fields.items() if key != 'file'}
        assert values['model'] == (model or 'whisper-large-v3')
        assert values['language'] == 'zh' and values['temperature'] == '0'
        assert values['response_format'] == 'json' and values['prompt'] == transcription.AIHUBMIX_ZH_PROMPT
        assert all(term in values['prompt'] for term in ('素食', '蔬食', '纯净素', '蛋奶素', '配料表', '菜谱', '食材'))
        assert len(values['prompt']) < 120
        assert b'/private/photos/my-secret.wav' not in request.content
        return httpx.Response(200, json={'text': '今天买了豆腐。'})

    transport(monkeypatch, upstream)
    result = transcription.transcribe(audio, 'audio/wav', '/private/photos/my-secret.wav')
    assert len(requests) == 1
    assert result['text'] == '今天买了豆腐。' and result['provider'] == 'aihubmix'
    assert result['region'] is None and result['simulated'] is False
    assert result['duration_seconds'] == 1 and result['usage'] == {'seconds': None, 'raw': None}
    assert 'synthetic-aihubmix-key' not in json.dumps(result)


@pytest.mark.parametrize('text', ['帮我找周围的素食餐厅。', '帮我找周围的速食店铺。',
                                 '我想看苏式菜谱。', '苏式面馆有素食选项吗？', '这包速食燕麦能吃吗？'])
def test_domain_hints_do_not_silently_rewrite_provider_transcripts(text, monkeypatch):
    enable(monkeypatch)
    calls = []
    def upstream(*args, **kwargs):
        calls.append(kwargs)
        return {'text': text}
    monkeypatch.setattr(providers, 'request_json', upstream)
    result = transcription.transcribe(wav(), 'audio/wav', 'voice.wav')
    assert result['text'] == text
    assert len(calls) == 1  # No extra LLM correction, retry or second transcription.
    assert calls[0]['data']['prompt'] == transcription.AIHUBMIX_ZH_PROMPT


def test_english_only_model_keeps_its_existing_language_prompt_and_temperature(monkeypatch):
    enable(monkeypatch, 'distil-whisper-large-v3-en')
    calls = []
    def upstream(*args, **kwargs):
        calls.append(kwargs)
        return {'text': 'Find a vegan restaurant.'}
    monkeypatch.setattr(providers, 'request_json', upstream)
    assert transcription.transcribe(wav(), 'audio/wav', 'voice.wav')['text'] == 'Find a vegan restaurant.'
    assert len(calls) == 1
    data = calls[0]['data']
    assert data['language'] == 'en' and data['temperature'] == '0.2'
    assert data['prompt'] == 'Transcribe only the spoken words accurately.'


@pytest.mark.parametrize('model', ['', 'whisper-large-v3', 'whisper-1', 'distil-whisper-large-v3-en'])
def test_documented_models_are_explicitly_configurable(model, monkeypatch):
    enable(monkeypatch, model)
    assert transcription.configured() is True
    assert transcription.capabilities()['model'] == (model or 'whisper-large-v3')


def test_unknown_model_never_falls_back_to_another_provider(monkeypatch):
    enable(monkeypatch, 'qwen-audio-3.0-asr-flash')
    monkeypatch.setattr(providers, 'request_json', lambda *a, **kw: pytest.fail('No upstream request'))
    with pytest.raises(core.AppError) as error:
        transcription.transcribe(wav(), 'audio/wav', 'voice.wav')
    assert error.value.code == 'NOT_CONFIGURED'


@pytest.mark.parametrize('endpoint', [
    'http://aihubmix.com/v1/audio/transcriptions',
    'https://aihubmix.com.evil.invalid/v1/audio/transcriptions',
    'https://api.aihubmix.com/v1/audio/transcriptions',
    'https://aihubmix.com/v1/audio/translations',
    'https://aihubmix.com:8443/v1/audio/transcriptions',
    'https://user:password@aihubmix.com/v1/audio/transcriptions',
    'https://aihubmix.com/v1/audio/transcriptions?api_key=must-not-send',
    'https://aihubmix.com/v1/audio/transcriptions#fragment',
    transcription.DEFAULT_ENDPOINT,
])
def test_only_selected_canonical_endpoint_can_receive_key_or_audio(endpoint, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(core.settings, 'transcription_endpoint', endpoint)
    monkeypatch.setattr(providers, 'request_json', lambda *a, **kw: pytest.fail('No upstream request'))
    assert transcription.configured() is False
    with pytest.raises(core.AppError) as error:
        transcription.transcribe(wav(), 'audio/wav', 'voice.wav')
    assert error.value.code == 'NOT_CONFIGURED'


@pytest.mark.parametrize('body', [{}, {'text': None}, {'text': 12}, {'text': []}, {'output': {'text': '旧协议'}}])
def test_invalid_text_is_not_a_success_or_a_protocol_fallback(body, monkeypatch):
    enable(monkeypatch)
    with pytest.raises(core.AppError) as error:
        transcription.parse_response(body)
    assert error.value.code == 'TRANSCRIPTION_INVALID'


def test_empty_transcript_is_reported_as_empty(monkeypatch):
    enable(monkeypatch)
    with pytest.raises(core.AppError) as error:
        transcription.parse_response({'text': '  '})
    assert error.value.code == 'TRANSCRIPTION_EMPTY'


@pytest.mark.parametrize('duration,expected', [(2.5, 2.5), (0, 0), (None, None), (-1, None),
                                            (True, None), ('2', None), (float('nan'), None)])
def test_verbose_duration_is_optional_and_never_coerced_or_priced(duration, expected, monkeypatch):
    enable(monkeypatch)
    result = transcription.parse_response({'text': '转录内容', 'duration': duration})
    assert result['usage']['seconds'] == expected
    assert result['usage']['raw'] == ({'duration': expected} if expected is not None else None)
    assert 'cost' not in result and 'tokens' not in result['usage']


@pytest.mark.parametrize('status,code', [(401, 'UPSTREAM_REJECTED'), (422, 'UPSTREAM_REJECTED'),
                                      (429, 'UPSTREAM_RATE_LIMITED'), (500, 'UPSTREAM_FAILED'),
                                      (302, 'UPSTREAM_FAILED')])
def test_http_errors_are_sanitized_without_retry_or_redirect(status, code, monkeypatch):
    enable(monkeypatch)
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(status, json={'error': 'synthetic-aihubmix-key'},
                              headers={'Location': 'https://unapproved.invalid'})

    transport(monkeypatch, upstream)
    with pytest.raises(core.AppError) as error:
        transcription.transcribe(wav(), 'audio/wav', 'voice.wav')
    assert error.value.code == code and len(calls) == 1
    assert 'synthetic-aihubmix-key' not in error.value.message


def test_ogg_is_not_advertised_or_charged_and_capabilities_follow_provider(signed, monkeypatch):
    enable(monkeypatch)
    for value in (signed.get('/api/v1/capabilities').json()['transcription'],
                  signed.get('/api/v1/capabilities/transcription').json()):
        assert value['provider'] == 'aihubmix' and value['model'] == 'whisper-large-v3'
        assert 'audio/ogg' not in value['mime_types']
        assert value['max_seconds'] == 300 and value['max_bytes'] == 10485760
    response = signed.post('/api/v1/transcriptions',
                           files={'file': ('voice.ogg', io.BytesIO(b'OggS' + b'0' * 64), 'audio/ogg')},
                           data={'duration_seconds': '1'})
    assert response.status_code == 422 and response.json()['error']['code'] == 'INVALID_AUDIO'
    with core.DB() as db:
        assert db.scalar(select(core.Record).where(core.Record.kind == 'usage')) is None


def test_aihubmix_async_job_retains_existing_editable_text_envelope(signed, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(providers, 'request_json', lambda *a, **kw: {'text': '今天买了豆腐。'})
    response = signed.post('/api/v1/transcriptions', files={'file': ('voice.wav', io.BytesIO(wav()), 'audio/wav')})
    assert response.status_code == 202
    assert worker.process_once() is True
    result = signed.get('/api/v1/transcriptions/' + response.json()['job_id']).json()
    assert result['status'] == 'succeeded' and result['text'] == '今天买了豆腐。'
    assert result['expires_at'] and not list((core.settings.data_dir / 'audio').glob('*'))

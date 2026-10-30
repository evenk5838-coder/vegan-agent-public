"""Current Beijing ASR HTTP wire protocol; synthetic audio and in-process transport only."""
import base64
import json
import struct

import httpx
import pytest

from app import core, providers, transcription

REAL_HTTPX_CLIENT = httpx.Client


def enable(monkeypatch):
    monkeypatch.setattr(core.settings, 'transcription_provider', 'bailian')
    monkeypatch.setattr(core.settings, 'transcription_api_key', 'synthetic-asr-key-only')
    monkeypatch.setattr(core.settings, 'transcription_endpoint', transcription.DEFAULT_ENDPOINT)
    monkeypatch.setattr(core.settings, 'transcription_model', transcription.DEFAULT_MODEL)
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def wav_44100():
    rate = 44100
    samples = b'\x00\x00' * rate
    return (b'RIFF' + struct.pack('<I', 36 + len(samples)) + b'WAVEfmt ' +
            struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) +
            b'data' + struct.pack('<I', len(samples)) + samples)


def wav_with_metadata():
    """Valid RIFF with odd-sized metadata before fmt and between fmt/data."""
    audio = wav_44100()

    def chunk(kind, data):
        return kind + struct.pack('<I', len(data)) + data + (b'\0' if len(data) % 2 else b'')

    body = b'WAVE' + chunk(b'JUNK', b'notes') + audio[12:36] + chunk(b'LIST', b'INFOx') + audio[36:]
    return b'RIFF' + struct.pack('<I', len(body)) + body


def test_wav_metadata_and_word_padding_do_not_change_audio_duration():
    audio = wav_with_metadata()
    info = transcription.validate_upload(audio, 'audio/wav')
    assert info['duration_seconds'] == 1
    assert info['bytes'] == len(audio)


@pytest.mark.parametrize('damage', ['truncated', 'oversized_data', 'missing_format', 'missing_data'])
def test_malformed_wav_cannot_fall_back_to_client_duration_or_reach_upstream(damage, monkeypatch):
    enable(monkeypatch)
    audio = wav_44100()
    if damage == 'truncated':
        audio = audio[:-1]
    elif damage == 'oversized_data':
        audio = audio[:40] + struct.pack('<I', len(audio)) + audio[44:]
    elif damage == 'missing_format':
        audio = audio[:12] + b'JUNK' + audio[16:]
    else:
        audio = audio[:36] + b'JUNK' + audio[40:]
    monkeypatch.setattr(providers, 'request_json', lambda *args, **kwargs: pytest.fail('No I/O allowed'))
    with pytest.raises(core.AppError) as error:
        transcription.transcribe(audio, 'audio/wav', 'synthetic.wav', 1)
    assert error.value.code == 'INVALID_AUDIO'


def test_real_serialization_has_bearer_nonstreaming_json_and_exact_audio(monkeypatch):
    enable(monkeypatch)
    requests = []
    audio = wav_44100()

    def upstream(request):
        requests.append(request)
        assert str(request.url) == transcription.DEFAULT_ENDPOINT
        assert request.method == 'POST'
        assert request.headers['Authorization'] == 'Bearer synthetic-asr-key-only'
        assert request.headers['Content-Type'] == 'application/json'
        assert request.headers['X-DashScope-SSE'] == 'disable'
        body = json.loads(request.content)
        assert set(body) == {'model', 'input', 'parameters'}
        assert body['model'] == transcription.DEFAULT_MODEL
        assert body['parameters'] == {'format': 'wav'}
        content = body['input']['messages'][0]['content']
        assert len(content) == 1 and set(content[0]) == {'type', 'input_audio'}
        assert content[0]['type'] == 'input_audio'
        uri = content[0]['input_audio']['data']
        assert uri.startswith('data:audio/wav;base64,')
        assert base64.b64decode(uri.split(',', 1)[1]) == audio
        return httpx.Response(200, json={'output': {'text': '豆腐已放冷藏。'},
                                        'usage': {'duration': 1}, 'request_id': 'synthetic-request'})

    def offline_client(*args, **kwargs):
        return REAL_HTTPX_CLIENT(*args, **kwargs, transport=httpx.MockTransport(upstream))

    monkeypatch.setattr(httpx, 'Client', offline_client)
    result = transcription.transcribe(audio, 'audio/wav', 'synthetic.wav')
    assert len(requests) == 1
    assert result['text'] == '豆腐已放冷藏。' and result['simulated'] is False
    assert result['usage']['seconds'] == 1
    assert result['duration_seconds'] == 1
    assert 'synthetic-asr-key-only' not in json.dumps(result)


@pytest.mark.parametrize('content,mime,provider_format', [
    (b'\x1aE\xdf\xa3' + b'0' * 64, 'audio/webm', 'webm'),
    (b'\x00\x00\x00\x18ftypM4A ' + b'0' * 64, 'audio/mp4', 'mp4'),
    (b'ID3' + b'0' * 64, 'audio/mpeg', 'mp3'),
    (b'OggS' + b'0' * 64, 'audio/ogg', 'ogg'),
])
def test_container_format_is_not_mime_and_unknown_sample_rate_is_not_invented(content, mime, provider_format):
    body = transcription.build_request(content, mime, 10)
    assert body['parameters'] == {'format': provider_format}
    assert base64.b64decode(body['input']['messages'][0]['content'][0]['input_audio']['data'].split(',', 1)[1]) == content


@pytest.mark.parametrize('endpoint', [
    transcription.DEFAULT_ENDPOINT,
    'https://workspace-test.cn-beijing.maas.aliyuncs.com' + transcription.BEIJING_PATH,
])
def test_only_documented_beijing_hosts_are_configurable(endpoint, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(core.settings, 'transcription_endpoint', endpoint)
    assert transcription.configured() is True


@pytest.mark.parametrize('endpoint', [
    'http://dashscope.aliyuncs.com' + transcription.BEIJING_PATH,
    'https://dashscope-intl.aliyuncs.com' + transcription.BEIJING_PATH,
    'https://workspace-test.ap-southeast-1.maas.aliyuncs.com' + transcription.BEIJING_PATH,
    'https://dashscope.aliyuncs.com.evil.test' + transcription.BEIJING_PATH,
    'https://127.0.0.1' + transcription.BEIJING_PATH,
    'https://example.invalid/transcribe',
    'https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com' + transcription.BEIJING_PATH,
    'https://user:password@dashscope.aliyuncs.com' + transcription.BEIJING_PATH,
    transcription.DEFAULT_ENDPOINT + '?api_key=must-not-send',
    transcription.DEFAULT_ENDPOINT + '#fragment',
    'https://dashscope.aliyuncs.com:8443' + transcription.BEIJING_PATH,
])
def test_unapproved_region_host_or_url_cannot_receive_key_or_audio(endpoint, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(core.settings, 'transcription_endpoint', endpoint)
    monkeypatch.setattr(providers, 'request_json', lambda *args, **kwargs: pytest.fail('No I/O allowed'))
    assert transcription.configured() is False
    with pytest.raises(core.AppError) as error:
        transcription.transcribe(wav_44100(), 'audio/wav', 'synthetic.wav')
    assert error.value.code == 'NOT_CONFIGURED'


def test_unapproved_model_does_not_silently_change_the_selected_service(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(core.settings, 'transcription_model', 'qwen-audio-3.1-asr-flash')
    monkeypatch.setattr(providers, 'request_json', lambda *args, **kwargs: pytest.fail('No I/O allowed'))
    assert transcription.configured() is False
    with pytest.raises(core.AppError) as error:
        transcription.require()
    assert error.value.code == 'NOT_CONFIGURED'


@pytest.mark.parametrize('text', [None, 42, True, [], {}])
def test_output_text_must_be_a_string_not_a_coerced_success(text):
    with pytest.raises(core.AppError) as error:
        transcription.parse_response({'output': {'text': text}})
    assert error.value.code == 'TRANSCRIPTION_INVALID'


@pytest.mark.parametrize('duration', [float('nan'), float('inf'), -1, True, '1'])
def test_malformed_provider_usage_is_unknown_not_fabricated(duration):
    result = transcription.parse_response({'output': {'text': '合成内容'}, 'usage': {'duration': duration}})
    assert result['usage']['seconds'] is None


def test_zero_usage_remains_zero():
    assert transcription.parse_response({'output': {'text': '合成内容'}, 'usage': {'duration': 0}})['usage']['seconds'] == 0


@pytest.mark.parametrize('duration', [float('nan'), float('inf'), True])
def test_invalid_declared_duration_cannot_start_upstream_request(duration, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(providers, 'request_json', lambda *args, **kwargs: pytest.fail('No I/O allowed'))
    with pytest.raises(core.AppError) as error:
        transcription.transcribe(b'\x1aE\xdf\xa3' + b'0' * 64, 'audio/webm', 'synthetic.webm', duration)
    assert error.value.code == 'INVALID_AUDIO'

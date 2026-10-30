"""非实时短音频转写：显式选择 AI HubMix 或旧百炼北京协议。

本轮完成适配与离线协议测试；不配置 Key、不发真实请求。
- AI HubMix：multipart file/model，JSON 顶层 text；不支持 Ogg。
- 百炼北京：JSON Base64 data URI，文字在 output.text。
- 项目上限维持 ≤5 分钟、音频 ≤10MB，未因此扩大费用授权。
- 格式按文件头（magic bytes）判断真实容器，不只信后缀/MIME；
- 时长优先取容器头（WAV），无法解析时要求客户端提供时长声明并校验上限。
"""
from __future__ import annotations

import base64
import math
import re
import struct
from urllib.parse import urlsplit

from . import providers
from .core import AppError, now, settings
from .schemas import TRANSCRIPTION_LIMITS

PROVIDER = 'aliyun-bailian'
REGION = 'cn-beijing'
DEFAULT_MODEL = 'qwen-audio-3.0-asr-flash'
BEIJING_PATH = '/api/v1/services/aigc/multimodal-generation/generation'
DEFAULT_ENDPOINT = 'https://dashscope.aliyuncs.com' + BEIJING_PATH
AIHUBMIX_ENDPOINT = 'https://aihubmix.com/v1/audio/transcriptions'
AIHUBMIX_MODEL = 'whisper-large-v3'
AIHUBMIX_MODELS = {'whisper-large-v3', 'whisper-1', 'distil-whisper-large-v3-en'}
# Whisper's prompt supplies spelling/topic context rather than a chat instruction.
# Keep it short and static: no private conversation, location or invented utterance.
AIHUBMIX_ZH_PROMPT = ('蔬食日常：素食、蔬食、纯净素、Vegan、蛋奶素、五辛、素食餐厅、'
                     '配料表、菜谱、食材、豆腐、冷藏、冷冻。仅转录实际说出的内容。')
PROVIDER_FORMAT = {'audio/wav': 'wav', 'audio/webm': 'webm', 'audio/mp4': 'mp4',
                   'audio/mpeg': 'mp3', 'audio/ogg': 'ogg'}

ALLOWED_MIME = set(TRANSCRIPTION_LIMITS['mime_types'])
MAX_BYTES = TRANSCRIPTION_LIMITS['max_bytes']          # 项目录音上限 10MB
MAX_BASE64_BYTES = TRANSCRIPTION_LIMITS['max_base64_bytes']
MAX_SECONDS = TRANSCRIPTION_LIMITS['max_seconds']      # 项目录音上限 5 分钟
MIN_SECONDS = 0.2


def selected_provider() -> str:
    return settings.transcription_provider.strip().lower()


def selected_model() -> str:
    default = AIHUBMIX_MODEL if selected_provider() == 'aihubmix' else DEFAULT_MODEL
    return settings.transcription_model or default


def capabilities() -> dict:
    """Keep the public shape and project caps while describing the selected provider."""
    result = {**TRANSCRIPTION_LIMITS, 'mime_types': list(TRANSCRIPTION_LIMITS['mime_types'])}
    if selected_provider() == 'aihubmix':
        result.update(provider='aihubmix', region=None, model=selected_model(),
                      mime_types=[mime for mime in result['mime_types'] if mime != 'audio/ogg'],
                      status='按项目录音上限设定；真实格式与时长仍需实测')
    elif selected_provider() != 'bailian':
        result.update(provider='unconfigured', region=None, model=None, mime_types=[])
    return result


def valid_endpoint(value: str) -> bool:
    """Only the explicitly selected provider's approved host/path receives key/audio."""
    try:
        address = urlsplit(value)
        hostname = address.hostname or ''
        provider = selected_provider()
        if provider == 'aihubmix':
            known_host, path = hostname == 'aihubmix.com', '/v1/audio/transcriptions'
        elif provider == 'bailian':
            known_host = hostname == 'dashscope.aliyuncs.com' or bool(re.fullmatch(
                r'[a-z0-9][a-z0-9-]{0,62}\.cn-beijing\.maas\.aliyuncs\.com', hostname))
            path = BEIJING_PATH
        else:
            return False
        return bool(value == value.strip() and address.scheme == 'https' and known_host
                    and address.port in (None, 443) and not address.username and not address.password
                    and address.path == path and not address.query and not address.fragment)
    except (ValueError, TypeError, AttributeError):
        return False


def configured() -> bool:
    models = AIHUBMIX_MODELS if selected_provider() == 'aihubmix' else {DEFAULT_MODEL}
    return bool(settings.transcription_api_key and valid_endpoint(settings.transcription_endpoint)
                and selected_model() in models)


def require():
    if not configured():
        raise AppError('NOT_CONFIGURED', '语音配置未就绪，请核对供应商、密钥、地址与已选模型；可以先改用打字。', 503)
    if not settings.external_calls_enabled:
        raise AppError('EXTERNAL_DISABLED', '外部调用尚未启用，暂时无法转写，可以先改用打字。', 503)


def detect_format(content: bytes) -> str | None:
    """按文件头判断真实容器；不支持或无法识别返回 None。"""
    if content[:4] == b'RIFF' and content[8:12] == b'WAVE':
        return 'audio/wav'
    if content[:4] == b'\x1aE\xdf\xa3':            # EBML → WebM/Matroska
        return 'audio/webm'
    if content[4:8] == b'ftyp':                     # ISO BMFF → MP4/M4A
        return 'audio/mp4'
    if content[:3] == b'ID3' or (len(content) > 1 and content[0] == 0xFF and (content[1] & 0xE0) == 0xE0):
        return 'audio/mpeg'
    if content[:4] == b'OggS':
        return 'audio/ogg'
    return None


def wav_duration_seconds(content: bytes) -> float | None:
    """按 RIFF 块读取 fmt/data；元数据与 WORD 补齐不计入音频时长。"""
    if detect_format(content) != 'audio/wav' or len(content) < 12:
        return None
    riff_end = 8 + struct.unpack_from('<I', content, 4)[0]
    if riff_end < 12 or riff_end > len(content):
        return None
    position, byte_rate, data_size = 12, None, 0
    while position < riff_end:
        if riff_end - position < 8:
            return None
        chunk_id = content[position:position + 4]
        chunk_size = struct.unpack_from('<I', content, position + 4)[0]
        start, end = position + 8, position + 8 + chunk_size
        if end > riff_end:
            return None
        if chunk_id == b'fmt ':
            if chunk_size < 16 or byte_rate is not None:
                return None
            _, channels, sample_rate, byte_rate, block_align, _ = struct.unpack_from('<HHIIHH', content, start)
            if not channels or not sample_rate or not byte_rate or not block_align:
                return None
        elif chunk_id == b'data':
            data_size += chunk_size
        # RIFF chunkSize excludes the padding byte for odd-sized chunks.
        position = end + chunk_size % 2
        if position > riff_end:
            return None
    if not byte_rate or not data_size:
        return None
    return data_size / byte_rate


def validate_upload(content: bytes, mime: str, declared_seconds: float | None = None) -> dict:
    """校验真实格式、大小、Base64 传输体积与时长；返回规范化信息。"""
    if not content:
        raise AppError('INVALID_AUDIO', '录音内容为空，请重录。', 422)
    if len(content) > MAX_BYTES:
        raise AppError('FILE_TOO_LARGE', '录音文件超过 10MB，请缩短后重试。', 413)
    detected = detect_format(content)
    if detected is None:
        raise AppError('INVALID_AUDIO', '无法识别录音格式，请重录或改用打字。', 422)
    allowed_mime = set(capabilities()['mime_types'])
    if detected not in allowed_mime:
        raise AppError('INVALID_AUDIO', '这种音频格式暂不支持，请重录或改用打字。', 422)
    normalized_mime = (mime or '').split(';')[0].strip().lower()
    if normalized_mime and normalized_mime not in allowed_mime:
        raise AppError('INVALID_AUDIO', '声明的音频类型不在支持范围。', 422)
    if base64_size(len(content)) > MAX_BASE64_BYTES:
        raise AppError('FILE_TOO_LARGE', '录音经编码后超出传输上限，请缩短后重试。', 413)
    duration = wav_duration_seconds(content)
    if detected == 'audio/wav' and duration is None:
        raise AppError('INVALID_AUDIO', '录音文件不完整或格式无效，请重新录制。', 422)
    if duration is None:
        duration = declared_seconds
    if duration is None:
        raise AppError('DURATION_REQUIRED', '无法从录音判断时长，请提供 duration_seconds 后重试。', 422)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration):
        raise AppError('INVALID_AUDIO', '录音时长无效，请重新录制。', 422)
    if duration < MIN_SECONDS:
        raise AppError('INVALID_AUDIO', '录音过短，请重新录制。', 422)
    if duration > MAX_SECONDS:
        raise AppError('AUDIO_TOO_LONG', '单段录音请控制在 5 分钟以内。', 413)
    return {'detected_mime': detected, 'bytes': len(content),
            'base64_bytes': base64_size(len(content)), 'duration_seconds': round(duration, 3)}


def base64_size(raw_bytes: int) -> int:
    return 4 * ((raw_bytes + 2) // 3)


def build_request(content: bytes, mime: str, duration: float | None) -> dict:
    """构建百炼请求体：JSON + Base64 data URI，非 multipart。"""
    actual_mime = (mime or '').split(';')[0].strip().lower()
    if actual_mime not in PROVIDER_FORMAT:
        raise AppError('INVALID_AUDIO', '这种音频格式暂不支持，请重录或改用打字。', 422)
    encoded = base64.b64encode(content).decode('ascii')
    return {'model': settings.transcription_model or DEFAULT_MODEL,
            'input': {'messages': [{'role': 'user', 'content': [
                {'type': 'input_audio', 'input_audio': {
                    'data': f'data:{actual_mime};base64,{encoded}'}}]}]},
            # The approved model accepts container sample rates. No resampling is
            # performed here, so do not falsely declare 16kHz for browser audio.
            'parameters': {'format': PROVIDER_FORMAT[actual_mime]}}


def parse_response(data: dict) -> dict:
    """Parse only the selected protocol; missing usage stays unknown, never priced here."""
    aihubmix = selected_provider() == 'aihubmix'
    try:
        text = data['text'] if aihubmix else data['output']['text']
        if not isinstance(text, str):
            raise TypeError('Transcription text must be a string')
        text = text.strip()
    except (KeyError, TypeError):
        raise AppError('TRANSCRIPTION_INVALID', '转写服务返回结构不符合预期，请重试。', 502) from None
    if not text:
        raise AppError('TRANSCRIPTION_EMPTY', '没有识别到语音内容，请重录或改用打字。', 422)
    usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
    seconds = data.get('duration') if aihubmix else usage.get('duration', usage.get('seconds'))
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        seconds = None
    if aihubmix:
        # verbose_json duration describes the recording, not a fabricated bill.
        usage = {'duration': seconds} if seconds is not None else {}
    return {'text': text[:4000], 'simulated': False, 'transcribed_at': now().isoformat(),
            'provider': 'aihubmix' if aihubmix else PROVIDER, 'region': None if aihubmix else REGION,
            'usage': {'seconds': seconds,
                      'raw': usage or None}}


def transcribe(content: bytes, mime: str, filename: str, declared_seconds: float | None = None) -> dict:
    """真实调用入口；仅在已配置服务商时使用。"""
    require()
    info = validate_upload(content, mime, declared_seconds)
    if selected_provider() == 'aihubmix':
        actual_mime = info['detected_mime']
        model = selected_model()
        # httpx supplies the multipart boundary; do not force a JSON content type
        # or transmit the user's original filename/private path to the provider.
        data = providers.request_json(
            'POST', settings.transcription_endpoint,
            headers={'Authorization': f'Bearer {settings.transcription_api_key}'},
            files={'file': (f'voice.{PROVIDER_FORMAT[actual_mime]}', content, actual_mime)},
            data={'model': model, 'language': 'en' if model == 'distil-whisper-large-v3-en' else 'zh',
                  'prompt': AIHUBMIX_ZH_PROMPT
                  if model != 'distil-whisper-large-v3-en' else 'Transcribe only the spoken words accurately.',
                  'temperature': '0' if model != 'distil-whisper-large-v3-en' else '0.2',
                  'response_format': 'json'})
    else:
        body = build_request(content, info['detected_mime'], info['duration_seconds'])
        data = providers.request_json('POST', settings.transcription_endpoint,
                                      headers={'Authorization': f'Bearer {settings.transcription_api_key}',
                                               'Content-Type': 'application/json', 'X-DashScope-SSE': 'disable'},
                                      json=body)
    result = parse_response(data)
    result['detected_mime'] = info['detected_mime']
    result['duration_seconds'] = info['duration_seconds']
    return result

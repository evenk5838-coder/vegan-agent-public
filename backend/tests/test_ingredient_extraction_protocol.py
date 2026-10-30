"""Exercise the actual photo request and parser without sending an image upstream."""
import json

import pytest

from app import providers
from app.core import AppError


def response(payload, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'content': payload}}]}


def test_photo_uses_bounded_non_thinking_transcription(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append(kwargs['json'])
        return response(json.dumps({'ingredients': ['大豆', '复合调料（乳粉、洋葱）'],
                                    'complete': True, 'label_warnings': ['可能含有花生']}))
    monkeypatch.setattr(providers, 'request_json', request)
    result = providers.extract_image(b'fixture', 'image/jpeg')
    assert result.ingredients == ['大豆', '复合调料（乳粉、洋葱）']
    assert calls[0]['thinking'] == {'type': 'disabled'}
    assert calls[0]['max_tokens'] == 3000
    assert calls[0]['response_format'] == {'type': 'json_object'}


@pytest.mark.parametrize('payload,finish,code,status', [
    ('{"ingredients":[', 'length', 'EXTRACTION_TRUNCATED', 502),
    ('not json', 'stop', 'EXTRACTION_INVALID', 502),
    (json.dumps({'ingredients': [{'name': '大豆'}], 'complete': True}), 'stop', 'EXTRACTION_INVALID', 502),
    (json.dumps({'ingredients': [], 'complete': False}), 'stop', 'NO_VISIBLE_INGREDIENTS', 422),
    (json.dumps({'ingredients': [], 'complete': True}), 'stop', 'EXTRACTION_INVALID', 502),
])
def test_photo_failure_does_not_blame_everything_on_image(monkeypatch, payload, finish, code, status):
    monkeypatch.setattr(providers, 'request_json', lambda *_args, **_kwargs: response(payload, finish))
    with pytest.raises(AppError) as error:
        providers.extract_image(b'fixture', 'image/jpeg')
    assert (error.value.code, error.value.status) == (code, status)


def test_incomplete_visible_words_stay_incomplete(monkeypatch):
    monkeypatch.setattr(providers, 'request_json', lambda *_args, **_kwargs: response(
        json.dumps({'ingredients': ['水', '不清楚的复合调料'], 'complete': False})))
    assert providers.extract_image(b'fixture', 'image/jpeg').complete is False


def test_invalid_choice_is_an_explicit_upstream_error(monkeypatch):
    monkeypatch.setattr(providers, 'request_json', lambda *_args, **_kwargs: {'choices': [None]})
    with pytest.raises(AppError) as error:
        providers.extract_image(b'fixture', 'image/jpeg')
    assert error.value.code == 'EXTRACTION_INVALID'

"""Pure service tests: all upstream data is in memory; no database or real HTTP."""
import pytest

from app import core, providers
from app.core import AppError
from app.services import barcodes


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    calls = []
    product = {'code': '4006381333931', 'product_name': 'Oat drink',
               'product_name_zh': '燕麦饮', 'brands': 'Public Brand', 'quantity': '250 ml',
               'ingredients_text': 'Water, oats', 'ingredients_text_zh': '水、燕麦',
               'last_modified_t': 1793016000,
               'vegan': True, 'ingredients_analysis_tags': ['en:vegan'],
               'allergens': 'en:milk', 'image_url': 'https://untrusted.example/photo'}

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {'status': 'success', 'product': product}

    monkeypatch.setattr(providers, 'request_json', request)
    return calls, product


@pytest.mark.parametrize('code', ['96385074', '036000291452', '4006381333931', '10012345678902'])
def test_gtin_formats_and_check_digits(code):
    assert barcodes.normalize_code(code) == code


def test_normalization_preserves_leading_zeroes():
    assert barcodes.normalize_code(' 0360 0029 1452\n') == '036000291452'


@pytest.mark.parametrize('value', ['', '1234567', '4006381333932', '4006381333931/../x',
                                   '４００６３８１３３３９３１', 4006381333931, None])
def test_invalid_gtin_is_a_controlled_422(value, monkeypatch):
    monkeypatch.setattr(providers, 'request_json', lambda *args, **kwargs: pytest.fail('invalid code must not leave service'))
    with pytest.raises(AppError) as error:
        barcodes.lookup(value)
    assert error.value.code == 'INVALID_BARCODE'
    assert error.value.status == 422


def test_only_public_code_and_requested_fields_are_sent(off):
    calls, _product = off
    barcodes.lookup('4006381333931')
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == 'GET'
    assert url == 'https://world.openfoodfacts.org/api/v3.6/product/4006381333931.json'
    assert kwargs['params'] == {'fields': ','.join(barcodes.OFF_FIELDS)}
    assert set(kwargs) == {'params', 'headers'}
    assert 'ShushiPersonalAgent' in kwargs['headers']['User-Agent']
    assert 'Authorization' not in kwargs['headers']


def test_result_preserves_original_text_and_requires_user_review(off):
    _calls, product = off
    result = barcodes.lookup('4006381333931')
    assert result['status'] == 'found'
    assert result['ingredients_text'] == '水、燕麦'
    assert result['product']['ingredients_text'] == 'Water, oats'
    assert result['product']['ingredients_text_zh'] == product['ingredients_text_zh']
    assert result['complete'] is False
    assert result['requires_confirmation'] is True
    assert result['label_warnings'] == []
    assert 'vegan' not in result['product']
    assert 'ingredients_analysis_tags' not in result['product']
    assert 'evaluation' not in result


def test_missing_ingredients_and_english_fallback_are_explicit(off):
    _calls, product = off
    product['ingredients_text_zh'] = '   '
    assert barcodes.lookup('4006381333931')['ingredients_text'] == 'Water, oats'
    product['ingredients_text'] = ''
    result = barcodes.lookup('4006381333931')
    assert result['status'] == 'no_ingredients'
    assert result['product']['product_name_zh'] == '燕麦饮'
    assert result['complete'] is False


def test_not_found_response_is_not_a_server_exception(monkeypatch):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'request_json', lambda *args, **kwargs: {
        'status': 'failure', 'result': {'id': 'product_not_found'}})
    result = barcodes.lookup('4006381333931')
    assert result['status'] == 'not_found'
    assert result['product'] is None


def test_collapsed_http_rejection_becomes_unavailable_not_false_not_found(monkeypatch):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    def rejected(*args, **kwargs):
        raise AppError('UPSTREAM_REJECTED', 'private upstream detail', 502)
    monkeypatch.setattr(providers, 'request_json', rejected)
    result = barcodes.lookup('4006381333931')
    assert result['status'] == 'unavailable'
    assert 'private upstream detail' not in str(result)


@pytest.mark.parametrize('payload', [None, [], 'bad', {}, {'product': []},
                                      {'product': {'code': '96385074'}},
                                      {'product': {'code': '4006381333931', 'ingredients_text': []}}])
def test_malformed_and_mismatched_upstream_is_controlled_502(payload, monkeypatch):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'request_json', lambda *args, **kwargs: payload)
    with pytest.raises(AppError) as error:
        barcodes.lookup('4006381333931')
    assert error.value.status == 502
    assert error.value.code == 'BARCODE_UPSTREAM_INVALID'


def test_upc_leading_zero_equivalence_is_accepted(off):
    _calls, product = off
    product['code'] = '0036000291452'
    assert barcodes.lookup('036000291452')['status'] == 'found'


def test_global_external_off_blocks_upstream(monkeypatch):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', False)
    monkeypatch.setattr(providers, 'request_json', lambda *args, **kwargs: pytest.fail('disabled'))
    with pytest.raises(AppError) as error:
        barcodes.lookup('4006381333931')
    assert error.value.code == 'EXTERNAL_DISABLED'
    assert error.value.status == 503


def test_rate_limit_and_deadline_errors_remain_controlled(monkeypatch):
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    for code in ('UPSTREAM_RATE_LIMITED', 'TURN_BUDGET_EXCEEDED'):
        def rejected(*args, **kwargs):
            raise AppError(code, '稍后重试', 429)
        monkeypatch.setattr(providers, 'request_json', rejected)
        with pytest.raises(AppError) as error:
            barcodes.lookup('4006381333931')
        assert error.value.code == code
        assert error.value.status == 429

"""Public barcode lookup, without model calls or local persistence.

Only the validated GTIN is sent to Open Food Facts. Its crowdsourced ingredient
text remains an unconfirmed transcription: diet flags are deliberately ignored.
"""
from __future__ import annotations

import re

from .. import core, providers
from ..core import AppError

OFF_HOST = 'https://world.openfoodfacts.org'
OFF_API = OFF_HOST + '/api/v3.6/product/{code}.json'
OFF_FIELDS = ('code', 'product_name', 'product_name_zh', 'brands', 'quantity',
              'ingredients_text', 'ingredients_text_zh', 'last_modified_t')
USER_AGENT = 'ShushiPersonalAgent/0.3 (barcode ingredient lookup; private pilot)'


def normalize_code(value: str) -> str:
    """Keep leading zeroes; accept GTIN-8/12/13/14 only with a valid check digit."""
    if not isinstance(value, str):
        raise AppError('INVALID_BARCODE', '请输入商品条码数字。', 422)
    code = re.sub(r'\s+', '', value)
    if not re.fullmatch(r'(?:[0-9]{8}|[0-9]{12}|[0-9]{13}|[0-9]{14})', code):
        raise AppError('INVALID_BARCODE', '条码应为 8、12、13 或 14 位数字，请重新核对。', 422)
    total = sum(int(digit) * (3 if position % 2 == 0 else 1)
                for position, digit in enumerate(reversed(code[:-1])))
    if int(code[-1]) != (-total) % 10:
        raise AppError('INVALID_BARCODE', '条码校验未通过，请重新扫描或核对数字。', 422)
    return code


def _invalid():
    return AppError('BARCODE_UPSTREAM_INVALID', '商品资料暂时无法读取，请改拍配料表或手动输入。', 502)


def _text(product: dict, key: str, maximum: int) -> str:
    value = product.get(key)
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > maximum:
        raise _invalid()
    return value


def _not_found(payload: dict) -> bool:
    result = payload.get('result')
    if isinstance(result, dict) and result.get('id') == 'product_not_found':
        return True
    errors = payload.get('errors')
    return isinstance(errors, list) and any(
        isinstance(error, dict) and isinstance(error.get('message'), dict)
        and error['message'].get('id') == 'product_not_found'
        for error in errors)


def _base(code: str) -> dict:
    return {'code': code, 'status': 'not_found', 'product': None,
            'ingredients_text': '', 'label_warnings': [],
            'complete': False, 'requires_confirmation': True,
            'source': {'name': 'Open Food Facts', 'url': OFF_HOST + '/product/' + code},
            'message': '未找到这件商品的资料，请改拍配料表或手动输入。'}


def lookup(value: str) -> dict:
    """Return found/no_ingredients/not_found/unavailable; never judge or save.

    Invalid GTIN raises 422; invalid upstream data raises 502. Existing upstream
    404/other 4xx responses have already been collapsed by request_json, so they
    are reported as unavailable rather than being falsely declared not found.
    """
    code = normalize_code(value)
    if not core.settings.external_calls_enabled:
        raise AppError('EXTERNAL_DISABLED', '外部商品查询尚未启用，可以拍配料表或手动输入。', 503)
    response = _base(code)
    try:
        payload = providers.request_json(
            'GET', OFF_API.format(code=code),
            params={'fields': ','.join(OFF_FIELDS)},
            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    except AppError as error:
        if error.code == 'UPSTREAM_REJECTED':
            response.update(status='unavailable',
                            message='商品资料服务未能返回可核对资料，请改拍配料表或手动输入。')
            return response
        raise
    if not isinstance(payload, dict):
        raise _invalid()
    if _not_found(payload):
        return response
    product = payload.get('product')
    if not isinstance(product, dict) or not product:
        raise _invalid()

    product_code = product.get('code', payload.get('code'))
    try:
        product_code = normalize_code(product_code)
    except AppError:
        raise _invalid() from None
    # OFF may represent UPC-A with one leading zero. The underlying GTIN must match.
    if product_code.zfill(14) != code.zfill(14):
        raise _invalid()
    result = {'code': product_code,
              'product_name': _text(product, 'product_name', 1000),
              'product_name_zh': _text(product, 'product_name_zh', 1000),
              'brands': _text(product, 'brands', 1000),
              'quantity': _text(product, 'quantity', 200),
              'ingredients_text': _text(product, 'ingredients_text', 20000),
              'ingredients_text_zh': _text(product, 'ingredients_text_zh', 20000)}
    modified = product.get('last_modified_t')
    result['last_modified_t'] = modified if type(modified) is int and modified >= 0 else None
    # Keep both original language fields, and select Chinese without rewriting text.
    ingredients = (result['ingredients_text_zh'] if result['ingredients_text_zh'].strip()
                   else result['ingredients_text'])
    has_ingredients = bool(ingredients.strip())
    response.update(product=result, ingredients_text=ingredients,
                    status='found' if has_ingredients else 'no_ingredients',
                    message=('找到商品配料资料，请对照包装核对后再判断。'
                             if has_ingredients else '找到商品，但暂无配料资料，请改拍配料表或手动输入。'))
    return response

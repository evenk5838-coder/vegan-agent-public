"""Pure validation for the small subset of AMap JS services used by this app.

The SDK uses JSONP for coordinate conversion. We fetch JSON upstream and wrap
only the validated callback locally, keeping upstream credentials server-owned.
This module deliberately has no configuration, database, network or API imports.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any


ALLOWED_PATHS = frozenset({
    'v3/place/around', 'v3/place/text', 'v3/geocode/geo',
    'v3/assistant/inputtips', 'v3/assistant/coordinate/convert',
})
CONVERT_PATH = 'v3/assistant/coordinate/convert'
_CALLBACK = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*){0,7}\Z')
_NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z')
_FORBIDDEN_SEGMENTS = frozenset({'__proto__', 'prototype', 'constructor'})
_SERVICE_PARAMS = frozenset({'location', 'city', 'keywords', 'types', 'radius', 'page', 'offset'})
_SDK_LIMITS = {'platform': 32, 'sdkversion': 40, 'logversion': 40,
               'appname': 512, 's': 256, 'csid': 128}


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _invalid(message: str) -> ProtocolError:
    return ProtocolError('INVALID_MAP_REQUEST', message)


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _invalid(f'地图参数 {name} 格式不正确。')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _invalid(f'地图参数 {name} 格式不正确。')
    return value


def validate_callback(callback: str) -> str:
    """Accept only bounded JS identifier paths, never an expression or code."""
    callback = _text(callback, 'callback', 128)
    if not _CALLBACK.fullmatch(callback) or _FORBIDDEN_SEGMENTS.intersection(callback.split('.')):
        raise _invalid('地图回调名称格式不正确。')
    return callback


def _coordinates(value: Any, name: str, multiple: bool = False) -> str:
    value = _text(value, name, 2048 if multiple else 64)
    points = value.split('|')
    if len(points) > (40 if multiple else 1):
        raise _invalid('地图坐标数量不正确。')
    for point in points:
        pair = point.split(',')
        if len(pair) != 2 or any(not _NUMBER.fullmatch(part.strip()) for part in pair):
            raise _invalid('地图坐标格式不正确。')
        longitude, latitude = (float(part) for part in pair)
        if not all(math.isfinite(number) for number in (longitude, latitude)) or not (
            -180 <= longitude <= 180 and -90 <= latitude <= 90
        ):
            raise _invalid('地图坐标超出有效范围。')
    return value


def normalize_proxy_request(path: str, query: Mapping[str, str]) -> tuple[str, dict[str, str], str | None]:
    """Normalize one exact allowed path and keep only bounded service parameters.

    key, jscode, callback, url and all unknown parameters are never sent upstream.
    The caller must inject the server's JS key/jscode after this function returns.
    """
    if not isinstance(path, str):
        raise ProtocolError('NOT_FOUND', '不支持的地图接口。', 404)
    normalized = path.lstrip('/')
    prefix = '_AMapService/'
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix):]
    if normalized not in ALLOWED_PATHS:
        raise ProtocolError('NOT_FOUND', '不支持的地图接口。', 404)

    callback = validate_callback(query['callback']) if 'callback' in query else None
    filtered: dict[str, str] = {}
    if normalized == CONVERT_PATH:
        if 'locations' not in query or 'coordsys' not in query:
            raise _invalid('坐标转换缺少 locations 或 coordsys。')
        filtered['locations'] = _coordinates(query['locations'], 'locations', multiple=True)
        coordsys = _text(query['coordsys'], 'coordsys', 16)
        if coordsys not in {'gps', 'baidu', 'mapbar', 'autonavi'}:
            raise _invalid('坐标类型不受支持。')
        filtered['coordsys'] = coordsys
    else:
        for name in _SERVICE_PARAMS:
            if name not in query:
                continue
            filtered[name] = (_coordinates(query[name], name) if name == 'location'
                              else _text(query[name], name, 512))
    for name, limit in _SDK_LIMITS.items():
        if name in query:
            filtered[name] = _text(query[name], name, limit)
    if normalized == CONVERT_PATH or callback is not None:
        filtered['output'] = 'JSON'
    return normalized, filtered, callback


def encode_jsonp(data: Any, callback: str) -> str:
    """Wrap trusted JSON data in a validated callback, escaping HTML sentinels."""
    callback = validate_callback(callback)
    body = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    body = body.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return f'{callback}({body});'

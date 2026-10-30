"""Resolve district names to AMap adcodes instead of using them as POI keywords."""
from __future__ import annotations

import copy
import re
from datetime import timedelta

from .. import core, providers
from ..core import AppError, Record, advisory_lock, digest, now


def _name(value):
    return re.sub(r'(?:市|区|县|旗)$', '', str(value or '').replace(' ', ''))


def _districts(owner, keyword, children):
    identity = digest(f'area-v1\0{owner}\0{digest(core.settings.amap_api_key)}\0{keyword}\0{children}')[:32]
    with core.DB() as db:
        advisory_lock(db, 'restaurant-area', identity)
        cached = db.get(Record, identity)
        if cached and (cached.kind != 'restaurant_area' or cached.owner != owner):
            raise AppError('CACHE_CONFLICT', '地区缓存发生冲突，请稍后重试。', 503)
        if cached and cached.expires and cached.expires > now():
            return copy.deepcopy(cached.payload['districts'])
        providers.reserve_call(owner, 'restaurants')
        response = providers.request_json('GET', 'https://restapi.amap.com/v3/config/district', params={
            'key': core.settings.amap_api_key, 'keywords': keyword, 'subdistrict': children,
            'extensions': 'base', 'offset': 20, 'page': 1})
        if not isinstance(response, dict) or response.get('status') != '1' or not isinstance(response.get('districts'), list):
            raise AppError('AREA_UNAVAILABLE', '暂时无法核对查询地区，请稍后重试。', 502)
        rows = response['districts']
        if not rows or len(rows) >= 20:
            raise AppError('AREA_AMBIGUOUS', '请补充完整城市和区县，例如“北京市朝阳区”。', 422)
        expires = now() + timedelta(hours=24)
        if cached:
            cached.payload, cached.expires = {'districts': rows}, expires
        else:
            db.add(Record(id=identity, owner=owner, kind='restaurant_area', payload={'districts': rows}, expires=expires))
        db.commit()
        return rows


def resolve(owner: str, area: str, base: str, district: str | None) -> dict:
    roots = _districts(owner, base if district else area, 1 if district else 0)
    candidates = []
    # AMap can return a province and its city as separate top-level matches.
    # The province's city child is then shallow, while that same city at the
    # top level contains the districts. Join only explicitly linked adcode +
    # level nodes; an unrelated same-name district must never be selected.
    index = {}
    pending = list(roots)
    count = 0
    while pending:
        row = pending.pop()
        count += 1
        if count > 5000:
            raise AppError('AREA_UNAVAILABLE', '地区资料层级异常，请稍后重试。', 502)
        if not isinstance(row, dict):
            continue
        key = (row.get('adcode'), row.get('level'))
        if isinstance(key[0], str) and isinstance(key[1], str):
            index.setdefault(key, []).append(row)
        children = row.get('districts')
        if isinstance(children, list):
            pending.extend(children)
    for root in roots:
        if not isinstance(root, dict):
            continue
        exact_name = _name(root.get('name')) == _name(base if district else area)
        exact_code = not district and re.fullmatch(r'\d{6}', area) and root.get('adcode') == area
        if not exact_name and not exact_code:
            continue
        if district:
            if not isinstance(root.get('districts'), list):
                raise AppError('AREA_UNAVAILABLE', '地区资料不完整，请稍后重试。', 502)
            if not isinstance(root.get('adcode'), str) or not isinstance(root.get('level'), str):
                raise AppError('AREA_UNAVAILABLE', '地区资料不完整，请稍后重试。', 502)
            keys = [(root.get('adcode'), root.get('level'))]
            visited = set()
            while keys:
                key = keys.pop()
                if key in visited:
                    continue
                visited.add(key)
                for row in index.get(key, []):
                    if row.get('level') == 'district' and _name(row.get('name')) == _name(district):
                        candidates.append(row)
                    children = row.get('districts')
                    for child in children if isinstance(children, list) else []:
                        if isinstance(child, dict) and isinstance(child.get('adcode'), str) and isinstance(child.get('level'), str):
                            keys.append((child['adcode'], child['level']))
        elif root.get('level') in ('province', 'city', 'district'):
            candidates.append(root)
    distinct = {row.get('adcode'): row for row in candidates if re.fullmatch(r'\d{6}', str(row.get('adcode', '')))}
    if len(distinct) != 1:
        raise AppError('AREA_AMBIGUOUS', '未能唯一确认这个地区，请补充城市和区县全称。', 422)
    code, row = next(iter(distinct.items()))
    return {'adcode': code, 'name': row['name'], 'level': row['level'], 'basis': 'amap_administrative',
            'parent': base if district else None}

"""餐厅查询共用服务：参数校验、供应商调用、分页与查询口径。

HTTP 路由与 Agent 工具都调用这里，避免两套分页/校验逻辑。

约定（本批）：
- 默认第一页 20 条；page 1..100，page_size 1..25（供应商单页上限）。
- 供应商检索窗口：page * page_size ≤ 200，超出窗口直接拒绝，不静默返回空。
- 每次查询**只取请求的那一页**，不自动拉全量。
- 分页结束判定基于供应商原始页信息，不以“去重后不足一页”推断结束。
- 供应商匹配数（count）单独命名并校验；异常或缺失时为未知，不用本页条数顶替。
"""
from __future__ import annotations

import math
import re
import copy
import json
from datetime import timedelta
from typing import Any

from .. import core, providers
from . import restaurant_area, restaurant_scope, restaurant_diets
from ..core import AppError, Record, advisory_lock, digest, now

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 25          # 高德 v3 单页上限
MAX_PAGE = 100              # 高德 v3 页码上限
MAX_RECORDS = 200           # 同条件翻页可取的最大条数（检索窗口）
RADIUS_M = 3000
MIN_RADIUS_M = 250
MAX_RADIUS_M = 50000
TYPECODE = '050120'
CACHE_TTL = timedelta(minutes=10)
CACHE_VERSION = 'restaurant-page-cache-v3-regional-identity-default'

# Relative references require a separately supplied location, never a city search.
RELATIVE_AREAS = frozenset(('附近', '周边', '周围', '身边', '这里', '那里', '这边', '那边',
                           '这附近', '这周边', '这周围', '我附近', '我周围', '我周边', '我身边',
                           '我这里', '我这边', '我家附近', '我家周围', '我家周边',
                           '当前位置', '当前地点', '本次位置', '本次定位', '我的位置',
                           '我的当前位置', '我的附近', '我的周围', '我的周边', '我的身边',
                           'near me', 'nearby', 'around me'))


def is_relative_area(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in RELATIVE_AREAS


def _parse_int(value: Any, label: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise AppError('INVALID_PAGINATION', f'{label}需要是整数。', 422) from None
    return parsed


def validate_pagination(page: Any = None, page_size: Any = None) -> dict:
    """校验并归一化分页参数；非法值在**请求供应商之前**拒绝。"""
    page = DEFAULT_PAGE if page in (None, '') else _parse_int(page, '页码')
    page_size = DEFAULT_PAGE_SIZE if page_size in (None, '') else _parse_int(page_size, '每页条数')
    if page < 1 or page > MAX_PAGE:
        raise AppError('INVALID_PAGINATION', f'页码需在 1–{MAX_PAGE} 之间。', 422)
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise AppError('INVALID_PAGINATION', f'每页条数需在 1–{MAX_PAGE_SIZE} 之间。', 422)
    if page * page_size > MAX_RECORDS:
        raise AppError('PAGE_WINDOW_EXCEEDED',
                       f'第 {page} 页（每页 {page_size} 条）超出可检索范围：'
                       f'同一条件下最多获取 {MAX_RECORDS} 条。', 422)
    return {'page': page, 'page_size': page_size}


def validate_radius(radius_m: Any = None, *, around: bool) -> int:
    """Only explicit nearby radii change the default; reject before metering."""
    if radius_m is None:
        return RADIUS_M
    if not around:
        raise AppError('INVALID_RADIUS', '查询半径仅适用于提供有效坐标的周边查询。', 422)
    if type(radius_m) is int:
        radius = radius_m
    elif isinstance(radius_m, str) and re.fullmatch(r'\+?[0-9]+', radius_m.strip()):
        radius = int(radius_m.strip())
    else:
        raise AppError('INVALID_RADIUS', '查询半径需要是整数米数。', 422)
    if not MIN_RADIUS_M <= radius <= MAX_RADIUS_M:
        raise AppError('INVALID_RADIUS', '查询半径需在250至50000米之间。', 422)
    return radius


def _valid_count(value: Any) -> int | None:
    """供应商匹配数：只接受非负整数；其余视为未知。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _dedupe(items: list[dict]) -> tuple[list[dict], int]:
    """按有效 POI id 页内去重；缺 id 的条目保留（无法可靠判重）。"""
    seen: set[str] = set()
    kept: list[dict] = []
    removed = 0
    for item in items:
        poi_id = item.get('id')
        if isinstance(poi_id, str) and poi_id:
            if poi_id in seen:
                removed += 1
                continue
            seen.add(poi_id)
        kept.append(item)
    return kept, removed


def build_pagination(page: int, page_size: int, raw_returned: int, returned: int,
                     removed: int, count: int | None, count_reliable: bool) -> dict:
    """分页元信息。has_more / next_page / end_reason 必须彼此一致。

    - 结束判定用供应商原始页，不用去重后的条数；
    - 有更多结果时一定给出**可合法请求**的下一页，否则降级为“已到边界”；
    - 匹配数不可靠时按未知处理，不用本页条数顶替，也不宣称已全部返回。
    """
    window = page * page_size

    def _next_page_within_limits() -> int | None:
        '''满足页码与检索窗口约束的下一页；不合法时返回 None（绝不给出不可请求的页码）。'''
        nxt = page + 1
        if nxt > MAX_PAGE:
            return None
        if nxt * page_size > MAX_RECORDS:
            return None
        return nxt

    candidate = _next_page_within_limits()
    window_blocked = candidate is None and (page + 1) > MAX_PAGE and (page + 1) * page_size <= MAX_RECORDS
    reached_window_end = (window >= MAX_RECORDS) or ((page + 1) * page_size > MAX_RECORDS)

    if raw_returned == 0:
        has_more, next_page, end_reason = False, None, 'empty_page'
    else:
        if count_reliable and count is not None:
            reachable = min(count, MAX_RECORDS)
            wanted_more = window < reachable
            # 达到可达上限的原因：是 200 条窗口截断，还是确实没有更多
            exhausted_reason = ('reached_window'
                                if (reachable >= MAX_RECORDS and count > MAX_RECORDS)
                                else 'no_more_matches')
        else:
            # 匹配数未知：整页返回且还能继续翻页时才认为可能还有
            wanted_more = raw_returned >= page_size
        if wanted_more and candidate is not None:
            has_more, next_page, end_reason = True, candidate, None
        elif wanted_more:
            # 有更多但不能合法翻页：如实说明到达边界，不给出不可请求的页码
            has_more, next_page = False, None
            end_reason = 'reached_page_limit' if window_blocked else 'reached_window'
        else:
            has_more, next_page = False, None
            end_reason = exhausted_reason if (count_reliable and count is not None) else 'unknown_total'

    return {'page': page, 'page_size': page_size,
            'returned': returned,
            'raw_returned': raw_returned,
            'deduplicated': removed,
            'has_more': has_more,
            'next_page': next_page,
            'end_reason': end_reason,
            'matched_count': count if (count_reliable and count is not None) else None,
            'count_source': 'provider_reported' if (count_reliable and count is not None) else 'unknown',
            'window': {'max_records': MAX_RECORDS, 'max_page_size': MAX_PAGE_SIZE, 'max_page': MAX_PAGE},
            'reached_window_end': reached_window_end}


# 常见城市名（仅用于把“北京朝阳”拆成 city+keywords，不是“有没有地区”的判据）
CITY_PREFIXES = ('北京', '上海', '广州', '深圳', '杭州', '成都', '西安', '南京', '武汉', '重庆',
                 '天津', '苏州', '厦门', '青岛', '长沙', '郑州', '昆明', '南宁', '福州', '济南',
                 '沈阳', '哈尔滨', '长春', '合肥', '南昌', '贵阳', '太原', '石家庄', '兰州', '海口',
                 '乌鲁木齐', '呼和浩特', '银川', '西宁', '拉萨', '香港', '澳门', '台北')


def split_area(area: str) -> tuple[str, str | None]:
    """把用户给的地名拆成 (city, keywords)。

    “北京朝阳” → ('北京', '朝阳')；供应商按 city 限定、用 keywords 检索区县，
    避免直接拿整串当 city 导致 0 条。
    """
    area = (area or '').strip()
    for city in sorted(CITY_PREFIXES, key=len, reverse=True):
        if area.startswith(city):
            rest = area[len(city):].strip()
            # Both “北京朝阳” and “北京市朝阳区” are common page input.
            if rest.startswith('市'):
                rest = rest[1:].strip()
            return city, (rest or None)
    return area, None


def search(owner: str, city: str | None = None, location: str | None = None,
           page: Any = None, page_size: Any = None, keywords: str | None = None,
           radius_m: Any = None) -> dict:
    """执行一次餐厅查询（只取一页）。

    - `location` 为本次查询坐标（GCJ-02，"经度,纬度"），走 around；
    - 否则按 `city` 走 text 区域查询；
    - 计量沿用既有 `restaurants` 额度。
    """
    paging = validate_pagination(page, page_size)

    def _parse_location(value: Any) -> str:
        '''校验并归一化坐标（"经度,纬度"，GCJ-02）；非法值抛 INVALID_LOCATION，不触达供应商。'''
        if not isinstance(value, str):
            raise AppError('INVALID_LOCATION', '坐标需要是“经度,纬度”格式的文本。', 400)
        parts = [part.strip() for part in value.split(',')]
        if len(parts) != 2 or not all(parts):
            raise AppError('INVALID_LOCATION', '坐标需要包含经度与纬度两项。', 400)
        try:
            lon, lat = (float(part) for part in parts)
        except (TypeError, ValueError):
            raise AppError('INVALID_LOCATION', '坐标需要是数字，例如 121.47,31.23。', 400) from None
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise AppError('INVALID_LOCATION', '坐标数值不合法。', 400)
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise AppError('INVALID_LOCATION', '坐标范围不正确。', 400)
        return f'{lon},{lat}'

    if location not in (None, ''):
        location = _parse_location(location)
    else:
        location = None
    radius = validate_radius(radius_m, around=location is not None)
    if not location and is_relative_area(city):
        raise AppError('MISSING_LOCATION', '请先获取本次位置，或提供城市/区县。', 422)
    if not location and not (city or '').strip():
        raise AppError('INVALID_ARGUMENTS', '请提供城市/地区或本次位置。')
    city = str(city or '').strip()
    keywords = str(keywords or '').strip() or None
    if len(city) > 40 or (keywords and len(keywords) > 80):
        raise AppError('INVALID_ARGUMENTS', '地区不能超过40字，店铺关键词不能超过80字。', 422)
    # The real district sample returned 8 admitted dining candidates by identity
    # keyword, versus 3 under the Chinese-vegetarian category alone. Keep one
    # request per page and return the effective keyword for stable pagination.
    if not location and not keywords:
        keywords = '素食'

    # A hit reuses this account's exact successful page, including an empty
    # filtered page. Different centers/radii/pages must never share results.
    providers.require_external(core.settings.amap_api_key)
    identity = [CACHE_VERSION, restaurant_scope.RULE_VERSION,
                digest(core.settings.amap_api_key), owner,
                None if location else city, location, radius if location else None,
                keywords or None, paging['page'], paging['page_size']]
    cache_id = digest(json.dumps(identity, ensure_ascii=False))[:32]
    with core.DB() as db:
        # Coalesce concurrent HTTP/Agent requests before reserving a paid call.
        advisory_lock(db, 'restaurant-cache', cache_id)
        from sqlalchemy import select
        cached = db.scalar(select(Record).where(Record.id == cache_id).with_for_update())
        if cached and (cached.owner != owner or cached.kind != 'restaurant_cache'):
            raise AppError('CACHE_CONFLICT', '查询缓存发生冲突，请稍后重试。', 503)
        if cached and cached.owner == owner and cached.kind == 'restaurant_cache' and cached.expires and cached.expires > now():
            result = copy.deepcopy(cached.payload)
            result['items'] = [{**item, 'diet_labels': restaurant_diets.classify(item)} for item in result['items']]
            result['cache']['hit'] = True
            result['notice'] += ' 本次使用10分钟内的查询缓存。'
            return result
        result = _fetch_page(owner, city, location, radius, paging, keywords)
        fetched_at = now()
        expires = fetched_at + CACHE_TTL
        result['cache'] = {'hit': False, 'fetched_at': fetched_at.isoformat(), 'expires_at': expires.isoformat()}
        if cached:
            cached.payload = copy.deepcopy(result)
            cached.expires = expires
        else:
            db.add(Record(id=cache_id, owner=owner, kind='restaurant_cache',
                          payload=copy.deepcopy(result), expires=expires))
        db.commit()
        return result


def _fetch_page(owner, city, location, radius, paging, keywords):
    """A cache miss attempts exactly one paid page; failed attempts stay metered."""
    area = None
    provider_city = city
    if not location:
        base, district = split_area(city)
        if district or base not in CITY_PREFIXES:
            area = restaurant_area.resolve(owner, city, base, district)
            provider_city = area['adcode']
    providers.reserve_call(owner, 'restaurants')
    arguments = {'page': paging['page'], 'page_size': paging['page_size'], 'keywords': keywords}
    # Preserve the default adapter call contract for older in-process callers
    # and test doubles. A custom map view must explicitly carry its radius.
    if radius != RADIUS_M:
        arguments['radius_m'] = radius
    raw = providers.search_restaurants(provider_city, location, **arguments)
    if not location:
        raw['query']['city'] = city
        if area:
            raw['query']['area_resolution'] = area
    # 先按单店类别证据做准入，再做页内去重；两者计数分开，互不冒充
    area_rejected = []
    candidates = raw['items']
    if area and area.get('level') == 'district':
        candidates = []
        for item in raw['items']:
            if item.get('adcode') == area['adcode']:
                candidates.append(item)
            else:
                area_rejected.append(item)
    admitted, rejected, reject_reasons = restaurant_scope.partition(candidates)
    if area_rejected:
        rejected += area_rejected
        reject_reasons['outside_or_unknown_district'] = len(area_rejected)
    items, removed = _dedupe(admitted)
    items = [{**item, 'diet_labels': restaurant_diets.classify(item)} for item in items]
    count = _valid_count(raw.get('provider_count'))
    # 结合页偏移与本页原始条数判断：矛盾（例如第 2 页已拿到 21–40 条却报 20）按未知处理
    count_reliable = count is not None and count >= (paging['page'] - 1) * paging['page_size'] + raw['raw_returned']
    # 分页仍由“供应商原始页 + 匹配数”驱动：本页全被过滤但上游还有下一页时仍可继续
    pagination = build_pagination(paging['page'], paging['page_size'], raw['raw_returned'],
                                  len(items), removed, count, count_reliable)
    pagination['filtered_out'] = len(rejected)      # 未通过类别准入的本页条数
    pagination['reject_reasons'] = reject_reasons   # 按原因计数，便于前端解释

    notice = raw['notice']
    if rejected and not items:
        notice = ('本页没有找到符合素食类别线索的店铺；这不代表该地区没有素食餐厅，'
                  '可以继续查看下一页或换个区域。' + notice)

    return {'query': raw['query'], 'items': items, 'pagination': pagination,
            'scope': restaurant_scope.scope_meta(len(items), len(rejected), reject_reasons),
            'source': raw['source'], 'typecode': raw.get('typecode', TYPECODE), 'notice': notice}

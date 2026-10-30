"""批量食材草稿：字段规范化、必要信息检查与草稿项→正式食材的校验。

纯函数，不碰数据库；草稿入库、确认保存与提醒任务由 main.py 调用。
确认前不创建正式食材记录。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import json

from pydantic import ValidationError

from .core import AppError, Record, digest as core_digest, now, uid
from .schemas import FoodInput

MAX_ITEMS = 30
DRAFT_TTL_DAYS = 7
# 相对日期在草稿阶段解析成明确时刻，并把来源与参考时间一并保留供核对。
RELATIVE_FIELDS = ('date_phrase', 'date_type')
# 草稿项允许字段（与 FoodInput 对齐，另含 location/temperature_c）
ITEM_FIELDS = ('name', 'storage', 'condition', 'date_type', 'started_at', 'expires_at',
               'deadline_source', 'package_note', 'remind_at', 'location', 'temperature_c',
               'food_class', 'opened', 'storage_note')

FIELD_LABELS = {
    'name': '食材名称', 'storage': '存放方式', 'started_at': '日期对应时刻',
    'deadline_source': '期限依据', 'package_note': '适用包装说明',
}


def normalize_items(raw: list, reference_time: str | None = None, tz: str | None = None) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise AppError('INVALID_ARGUMENTS', '请至少提供一条食材。')
    if len(raw) > MAX_ITEMS:
        raise AppError('TOO_MANY_ITEMS', f'一次最多整理 {MAX_ITEMS} 条食材，请分批处理。', 422)
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise AppError('INVALID_ARGUMENTS', '食材条目格式不正确。')
        item = {}
        for key in ITEM_FIELDS:
            value = entry.get(key)
            if isinstance(value, str):
                value = value.strip()
            if value not in (None, ''):
                item[key] = value
        phrase = entry.get('date_phrase')
        if phrase:
            from . import dates  # 局部导入，避免循环
            parsed = dates.parse(str(phrase), reference_time, tz)
            if item.get('started_at'):
                try:
                    explicit = datetime.fromisoformat(str(item['started_at']))
                    resolved = datetime.fromisoformat(parsed['started_at'])
                    if explicit.tzinfo is None or explicit.utcoffset() is None:
                        raise ValueError
                except (TypeError, ValueError):
                    raise AppError('INVALID_TIME', '已填写的日期时刻需要合法时间和时区。', 422) from None
                same = (explicit.astimezone(resolved.tzinfo).date() == resolved.date()
                        if parsed['time_precision'] == 'date' else explicit == resolved)
                if not same:
                    raise AppError('DATE_CONFLICT', '日期原话与填写的时刻不一致，请核对。', 422)
            item.setdefault('started_at', parsed['started_at'])
            item['date_phrase'] = parsed['phrase']
            item['date_source'] = parsed['source']
            item['date_reference_at'] = parsed['reference_at']
            item['date_timezone'] = parsed['timezone']
            item['time_precision'] = parsed['time_precision']
            item['date_warnings'] = parsed['warnings']
            if not item.get('date_type'):
                item['date_missing'] = ['date_type']
        items.append(item)
    return items


def missing_fields(item: dict) -> list[str]:
    missing = []
    if not item.get('name'):
        missing.append('name')
    # 储存方式、加工状态与日期是可补信息，缺失不会阻止登记。
    # 已填期限必须有可核对来源；非法已填值仍由 FoodInput 校验。
    if item.get('expires_at') and not item.get('deadline_source'):
        missing.append('deadline_source')
    if item.get('deadline_source') == 'package' and not (item.get('package_note') or '').strip():
        missing.append('package_note')
    return missing


def item_status(item: dict) -> str:
    return 'needs_info' if missing_fields(item) else 'ready'


def to_food_payload(item: dict) -> dict:
    """把草稿项转成通过 FoodInput 校验的字段；不完整时抛 NEEDS_INFO。"""
    missing = missing_fields(item)
    if missing:
        labels = '、'.join(FIELD_LABELS.get(k, k) for k in missing)
        raise AppError('NEEDS_INFO', f'这条食材还缺少必要信息：{labels}。', 422)
    if item.get('deadline_source') == 'rule':
        raise AppError('INVALID_RULE_BASIS', '规则期限不能由草稿直接提交，请保存后通过重评生成。', 422)
    data = {k: item[k] for k in ITEM_FIELDS if k in item}
    data.setdefault('location', 'unknown')
    data.setdefault('storage', 'unknown')
    data.setdefault('condition', 'unknown')
    data.setdefault('date_type', 'unknown')
    data.setdefault('deadline_source', 'unknown')
    data.setdefault('status', 'active')
    try:
        payload = FoodInput(**data).model_dump(mode='json')
        # 日期精度/来源是输入证据的一部分，确认后也不能把日期表示当实际时刻。
        for key in ('date_source', 'date_phrase', 'date_reference_at', 'date_timezone',
                    'time_precision', 'date_warnings'):
            if key in item:
                payload[key] = item[key]
        return payload
    except ValidationError as e:
        first = e.errors()[0]
        field = '.'.join(str(p) for p in first.get('loc', ())) or '输入'
        raise AppError('NEEDS_INFO', f'这条食材的信息还不完整或格式不正确（{field}），请补充后重试。', 422) from None


def draft_item_view(item: dict) -> dict:
    from .services import food_advice, consumption_plan
    """返回可核对的完整字段：用户/提取值（可空）与规则建议分开，刷新后可继续核对。"""
    values = {k: item.get(k) for k in ITEM_FIELDS if item.get(k) not in (None, '')}
    resolved = {k: item.get(k) for k in ('date_source', 'date_phrase', 'date_reference_at',
                                         'date_timezone', 'time_precision')
                if item.get(k) not in (None, '')}
    missing = missing_fields(item)
    if not missing:
        try:
            to_food_payload(item)
        except AppError:
            missing = ['invalid_evidence']
    try:
        suggestion = food_advice.suggestion_view(item)
    except AppError:
        # 保留非法原值供核对，但不让一个日期坏值卡住整批草稿卡片。
        safe = {k: v for k, v in item.items() if k not in ('started_at', 'expires_at', 'remind_at')}
        suggestion = food_advice.suggestion_view(safe)
        suggestion.update(action='unknown', date=None, basis={'type': 'none', 'note': '已填日期无效，请核对；可先保存其他有效条目。'},
                          reason='invalid_evidence', missing=['invalid_evidence'])
        missing = list(dict.fromkeys([*missing, 'invalid_evidence']))
    return {'item_id': item['item_id'], 'name': item.get('name', ''),
            'status': 'needs_info' if missing else 'ready', 'missing': missing,
            **values, **resolved,
            'date_warnings': item.get('date_warnings') or [],
            'suggested': item.get('suggested'), 'actual': item.get('actual'),
            'field_sources': {k: 'input' for k in values},
            # 建议只是提示：正式期限仍要用户确认后写入
            'suggestion': suggestion,
            'consumption_plan': consumption_plan.view(item),
            'storage_guidance_summary': item.get('storage_guidance_summary'),
            'saved_food_id': item.get('saved_food_id')}


def fingerprint(items: list[dict]) -> str:
    """确认请求的内容指纹：目标项集合 + 各项目前字段值，用于幂等冲突判定。"""
    rows = [[i.get('item_id'), {k: i.get(k) for k in (*ITEM_FIELDS, 'consumption_plan')}] for i in sorted(items, key=lambda x: x.get('item_id') or '')]
    return core_digest(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str))


def build_draft_record(owner: str, items, chat_turn_id=None, reference_time=None, tz=None) -> Record:
    """建草稿记录（kind='food_draft'），7 天后过期；确认前不创建正式食材。"""
    normalized = normalize_items(items or [], reference_time, tz)
    payload_items = [{**item, 'item_id': uid(), 'suggested': None, 'actual': None, 'saved_food_id': None}
                     for item in normalized]
    return Record(owner=owner, kind='food_draft',
                  payload={'status': 'pending_confirmation', 'items': payload_items,
                           'chat_turn_id': chat_turn_id, 'confirm_keys': {}, 'updated': now().isoformat()},
                  expires=now() + timedelta(days=DRAFT_TTL_DAYS))

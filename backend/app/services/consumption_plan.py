"""Source-backed eating plans, separate from expiration and push reminders."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core import now

VERSION = 'consumption-plan-2026-10-29.1'
CONTEXT_FIELDS = ('name', 'food_class', 'storage', 'condition', 'opened', 'temperature_c',
                  'date_type', 'started_at', 'expires_at', 'deadline_source', 'package_note', 'storage_note')


class SourceWindow(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_id: int
    quote: str = Field(min_length=4, max_length=600)
    minimum: float = Field(ge=0, le=8760)
    maximum: float = Field(gt=0, le=8760)
    unit: Literal['hours', 'days']
    anchor: Literal['purchase', 'prepared', 'opened', 'production']
    storage: Literal['ambient', 'refrigerated', 'frozen']
    food_class: Literal['fresh', 'cooked', 'packaged']
    condition: Literal['raw', 'cut', 'cooked', 'opened', 'unopened'] | None = None
    opened: bool | None = None
    temperature_min: float | None = Field(default=None, ge=-80, le=70)
    temperature_max: float | None = Field(default=None, ge=-80, le=70)
    food: str | None = Field(default=None, max_length=100)


def fingerprint(facts: dict) -> str:
    defaults = {'food_class': 'unknown', 'storage': 'unknown', 'condition': 'unknown',
                'date_type': 'unknown', 'deadline_source': 'unknown', 'package_note': '', 'storage_note': ''}
    canonical = {key: facts.get(key, defaults.get(key)) for key in CONTEXT_FIELDS}
    # JSON model arguments may contain 4 while validated input contains 4.0.
    # These are the same observation, not a concurrent edit.
    if canonical['temperature_c'] is not None:
        canonical['temperature_c'] = float(canonical['temperature_c'])
    for key in ('started_at', 'expires_at'):
        parsed = _parse(canonical[key])
        if parsed:
            from datetime import timezone
            canonical[key] = parsed.astimezone(timezone.utc).isoformat()
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _parse(value):
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo is not None and result.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def _duration_present(window: SourceWindow) -> bool:
    """Check the duration itself, not an unrelated temperature or page date."""
    units = r'(小时|小時|hours?|天|日|days?)'
    number = r'(\d+(?:\.\d+)?)'
    text = window.quote.lower()
    range_pattern = r'(?<![\d.])' + number + r'\s*(?:小时|小時|hours?|天|日|days?)?\s*[-–—~～至到]\s*' + number + r'\s*' + units
    ranges = re.findall(range_pattern, text)
    candidates = [(float(a), float(b), unit) for a, b, unit in ranges]
    if window.minimum in (0, window.maximum):
        remainder = re.sub(range_pattern, '', text)
        for n, unit in re.findall(r'(?<![\d.])' + number + r'\s*' + units, remainder):
            if window.minimum == 0 and not re.search(r'内|內|不超过|不超過|within|up to|no more than', remainder):
                continue
            candidates.append((0 if window.minimum == 0 else float(n), float(n), unit))
    return any(a == window.minimum and b == window.maximum
               and ('hours' if unit in ('小时', '小時', 'hour', 'hours') else 'days') == window.unit
               for a, b, unit in candidates)


def key_reminders(facts):
    if facts.get('food_class') == 'packaged':
        specific = '包装保存条件优先；开封后按开封要求重新核对。'
    elif facts.get('condition') == 'cooked' or facts.get('food_class') == 'cooked':
        specific = '再次加热不重置原来的记录起点和食用安排。'
    elif facts.get('condition') == 'cut':
        specific = '切开后的食物不沿用完整食材的安排。'
    elif '豆腐' in facts.get('name', ''):
        specific = '豆腐优先安排近期餐次；存放情况变化后重新核对。'
    else:
        specific = '按当前成熟程度和存放情况安排，情况变化后重新核对。'
    return [specific, '倒计时不是安全保证；冷藏中断或出现异常时先核对。']


def build(window: SourceWindow | None, facts: dict, sources: list[dict], cited_ids: set[int]) -> dict:
    base = {'version': VERSION, 'scope': 'eating_plan', 'fact_fingerprint': fingerprint(facts),
            'generated_at': now().isoformat(), 'safety_guarantee': False}
    def pending(reason, message, status='unavailable'):
        return {**base, 'status': status, 'reason': reason, 'message': message,
                'target_at': None, 'start_at': None}
    if window is None:
        scheduled = early_schedule(facts, sources, cited_ids)
        return scheduled or pending('no_supported_duration', '暂未找到适用时间，先核对实际存放情况，不自动安排具体食用日期。')
    if window.storage == 'ambient' and (facts.get('food_class') == 'cooked' or facts.get('condition') == 'cut'
                                       or '豆腐' in facts.get('name', '') and not any(w in facts['name'] for w in ('干', '乾'))):
        return pending('ambient_perishable', '这类食物的常温经历需要单独核对，暂不套用来源中的常温保存时长。', 'needs_context')
    source = next((s for s in sources if s['id'] == window.source_id and s['id'] in cited_ids), None)
    compact = lambda text: re.sub(r'\s+', '', text)
    if (source is None or compact(window.quote) not in compact(source['content'])
            or window.maximum < window.minimum or not _duration_present(window)):
        return pending('unsupported_window', '本次时间建议未通过来源核对，暂不生成倒计时。')
    quote = window.quote
    source_numbers = {float(n) for n in re.findall(r'-?\d+(?:\.\d+)?', quote)}
    if any(bound is not None and bound not in source_numbers
           for bound in (window.temperature_min, window.temperature_max)):
        return pending('unsupported_temperature', '提取的温度条件不在本条来源中，暂不套用时间。', 'needs_context')
    if (re.search(r'\d\s*(?:℃|°\s*C|摄氏|攝氏)', quote, re.I)
            and window.temperature_min is None and window.temperature_max is None
            or any(word in quote for word in ('开封后', '開封後')) and window.opened is not True
            or any(word in quote for word in ('未开封', '未開封')) and window.opened is not False
            or any(word in quote for word in ('切开', '切開', '切制')) and window.condition != 'cut'):
        return pending('unverified_conditions', '来源的适用条件尚未核对完整。', 'needs_context')
    for key in ('storage', 'food_class', 'condition', 'opened'):
        required = getattr(window, key)
        if required is not None and facts.get(key) != required:
            return pending('conditions_not_met', '还需确认与资料相符的食材及存放情况。', 'needs_context')
    temperature = facts.get('temperature_c')
    if re.search(r'℃|°\s*[CF]|摄氏|攝氏|华氏', quote, re.I):
        from .food_advice import _temperature_matches
        if not _temperature_matches(quote, facts):
            return pending('temperature_not_met', '资料的时间建议有温度条件，当前尚未确认符合。', 'needs_context')
    if ((window.temperature_min is not None or window.temperature_max is not None)
            and (temperature is None
                 or window.temperature_min is not None and temperature < window.temperature_min
                 or window.temperature_max is not None and temperature > window.temperature_max)):
        return pending('temperature_not_met', '资料的时间建议有温度条件，当前尚未确认符合。', 'needs_context')
    start = _parse(facts.get('started_at'))
    if start is None or facts.get('date_type') != window.anchor or start > now():
        return pending('missing_time_anchor', '需要知道相应的购买、制作或开封日期，才能安排食用时间。', 'needs_context')
    factor = 24 if window.unit == 'days' else 1
    # Use the early end of a source range for planning; never reset to now.
    target = start + timedelta(hours=(window.minimum or window.maximum) * factor)
    range_end = start + timedelta(hours=window.maximum * factor)
    if (target - start).days > 365:
        return pending('duration_out_of_range', '此资料不适合生成当前食用计划。')
    recorded_end = _parse(facts.get('expires_at'))
    if recorded_end:
        target = min(target, recorded_end)
        range_end = min(range_end, recorded_end)
    if target <= start:
        return pending('date_conflict', '已记录的日期有冲突，请先核对。', 'needs_context')
    return {**base, 'status': 'ready', 'basis': 'web_reference', 'target_at': target.isoformat(),
            'start_at': start.isoformat(), 'range_end_at': range_end.isoformat(),
            'source': {k: source[k] for k in ('id', 'title', 'url')}, 'evidence_quote': window.quote,
            'source_window': window.model_dump(), 'date_origin': window.anchor,
            'key_reminders': key_reminders(facts),
            'message': '建议按这个时间优先安排食用；这是时间计划，不是实际新鲜度检测。'}


def early_schedule(facts: dict, sources: list[dict], cited_ids: set[int]) -> dict | None:
    """A same-day eating goal, never an inferred storage life or package expiry.

    Only supports known refrigerated fresh/prepared food and a relevant source
    explicitly recommending prompt use. The goal stays anchored to the reported
    purchase/preparation day; reopening or searching tomorrow cannot extend it.
    """
    start = _parse(facts.get('started_at'))
    if (not start or start > now() or facts.get('storage') != 'refrigerated'
            or facts.get('food_class') not in ('fresh', 'cooked')
            or facts.get('condition') not in ('raw', 'cut', 'cooked')
            or facts.get('date_type') not in ('purchase', 'prepared')):
        return None
    from .storage_guidance import search_name
    name = search_name(facts.get('name', ''))
    matches = []
    for s in sources:
        if s['id'] not in cited_ids:
            continue
        for sentence in re.split(r'[。！？\n]', s['content']):
            if (not name or name not in sentence
                    or not re.search(r'尽快[食使]?用|盡快|现买现吃|當[日天]食用|当[日天]食用|及早食用', sentence)):
                continue
            if ('开封' in sentence or '開封' in sentence) and facts.get('opened') is not True:
                continue
            if any(w in sentence for w in ('煎后', '煮后', '熟食', '剩菜', '蒸煮后')) and facts.get('condition') != 'cooked':
                continue
            if any(w in sentence for w in ('切开', '切開', '切制')) and facts.get('condition') != 'cut':
                continue
            # Keep the relevant prompt-use clause, not unrelated room-temperature
            # durations earlier in a long source sentence.
            last_name = sentence.rfind(name)
            left = sentence.rfind('，', 0, last_name) + 1
            matches.append((s, sentence[left:].strip()[:600]))
    source, quote = matches[0] if matches else (None, '')
    if source is None:
        return None
    local_start = start.astimezone(ZoneInfo('Asia/Shanghai'))
    target = local_start.replace(hour=23, minute=59, second=59, microsecond=0)
    recorded_end = _parse(facts.get('expires_at'))
    if recorded_end:
        target = min(target, recorded_end)
    if target <= start:
        return None
    return {'version': VERSION, 'scope': 'eating_plan', 'fact_fingerprint': fingerprint(facts),
            'generated_at': now().isoformat(), 'safety_guarantee': False, 'status': 'ready',
            'basis': 'conservative_schedule', 'policy': 'same-day-priority-2026-10-29',
            'target_at': target.isoformat(), 'start_at': start.isoformat(), 'range_end_at': target.isoformat(),
            'date_origin': facts['date_type'], 'source_window': None,
            'source': {k: source[k] for k in ('id', 'title', 'url')}, 'evidence_quote': quote,
            'message': '优先安排购买或制作当天的下一餐；倒计时是保守安排目标，来源没有给出保存天数。',
            'key_reminders': ['这是优先食用安排，不是保质期或安全保证。', key_reminders(facts)[0]]}


def view(facts: dict, stored: dict | None = None) -> dict | None:
    plan = stored if stored is not None else facts.get('consumption_plan')
    if not isinstance(plan, dict):
        return None
    result = {key: value for key, value in plan.items() if key != 'fact_fingerprint'}
    result.update(remaining_seconds=None, remaining_days=None, elapsed_percent=None)
    if (plan.get('version') != VERSION or plan.get('fact_fingerprint') != fingerprint(facts)
            or facts.get('status', 'active') != 'active'):
        return {**result, 'status': 'outdated', 'target_at': None, 'start_at': None,
                'range_end_at': None, 'message': '食材状态或存放情况已变化，原食用计划需要重新核对。'}
    if plan.get('status') != 'ready':
        return result
    start, target = _parse(plan.get('start_at')), _parse(plan.get('target_at'))
    if start is None or target is None or target <= start:
        return {**result, 'status': 'unavailable', 'target_at': None, 'start_at': None}
    remaining = (target - now()).total_seconds()
    return {**result, 'status': 'due' if remaining <= 0 else 'ready',
            'remaining_seconds': math.floor(remaining), 'remaining_days': round(remaining / 86400, 1),
            'elapsed_percent': max(0, min(100, round((now() - start).total_seconds() /
                                                   (target - start).total_seconds() * 100, 1)))}

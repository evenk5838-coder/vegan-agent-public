"""相对日期解析：今天/昨天/明天等，按明确参考时间与用户时区换算成具体日期。

原则：
- 解析结果必须带参考时间、时区与来源说明，供用户在卡片中核对；
- 只解析用户说出或系统给出的时间信息，不推断购买/制作/开封状态或温度；
- 日期含义（date_type）或起始时刻不足时，交给调用方追问，不臆造。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import AppError

DAY_WORDS = {
    '前天': -2, '大前天': -3, '昨天': -1, '昨日': -1, '今天': 0, '今日': 0,
    '明天': 1, '明日': 1, '后天': 2, '大后天': 3,
}
PERIOD_WORDS = {'凌晨': 3, '早上': 8, '上午': 9, '中午': 12, '下午': 15, '傍晚': 18, '晚上': 20, '夜里': 22}
DEFAULT_TIMEZONE = 'Asia/Shanghai'

# 只做日期/时刻换算，不做语义猜测（不加“大概/左右”）。
DATE_TAIL = re.compile(r'^(?P<day>大前天|前天|昨天|昨日|今天|今日|明天|明日|大后天|后天)'
                       r'(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?'
                       r'(?:(?P<hour>\d{1,2})\s*[点时](?:(?P<minute>\d{1,2})\s*分?)?)?$')


def timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        raise AppError('INVALID_TIMEZONE', '时区不可识别，请使用如 Asia/Shanghai。', 422) from None


def reference(reference_time: str | None) -> datetime:
    if not reference_time:
        return datetime.now(timezone(DEFAULT_TIMEZONE))
    try:
        parsed = datetime.fromisoformat(reference_time)
    except ValueError:
        raise AppError('INVALID_TIME', '参考时间格式不正确，请提供带时区的 ISO 时间。', 422) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AppError('INVALID_TIME', '参考时间需要带时区，例如 2026-10-24T09:00:00+08:00。', 422)
    return parsed


def is_relative(phrase: str) -> bool:
    return bool(DATE_TAIL.match((phrase or '').strip()))


def parse(phrase: str, reference_time: str | None = None, tz: str | None = None) -> dict:
    """把“今天下午3点”这类短语换成具体时刻；无法确定时抛 DATE_UNCLEAR。"""
    text = (phrase or '').strip().replace(' ', '')
    if not text:
        raise AppError('DATE_UNCLEAR', '请说明是哪一天，例如“今天”或具体日期。', 422)
    match = DATE_TAIL.match(text)
    absolute = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', text) or re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
    if absolute:
        zone = timezone(tz)
        try:
            resolved = datetime(*(int(part) for part in absolute.groups()), tzinfo=zone)
        except ValueError:
            raise AppError('DATE_UNCLEAR', '日期不存在，请核对年月日。', 422) from None
        return {'started_at': resolved.isoformat(), 'resolved_date': resolved.date().isoformat(),
                'time_precision': 'date', 'phrase': text,
                'reference_at': reference(reference_time).astimezone(zone).isoformat(),
                'timezone': str(zone), 'source': 'absolute',
                'warnings': ['仅记录提供的年月日；00:00是日期表示，不代表实际购买、生产或制作时刻。']}
    if not match:
        raise AppError('DATE_UNCLEAR', '这句话里的日期不清楚，请补充“今天/昨天/明天”或具体日期。', 422)
    zone = timezone(tz)
    base = reference(reference_time).astimezone(zone)
    day_offset = DAY_WORDS[match.group('day')]
    target_date = (base + timedelta(days=day_offset)).date()
    hour, minute = match.group('hour'), match.group('minute')
    precision = 'date'
    resolved = datetime.combine(target_date, datetime.min.time(), tzinfo=zone)
    if hour is not None:
        hour_value = int(hour)
        minute_value = int(minute or 0)
        if (match.group('period') or '') in PERIOD_WORDS and hour_value < 12 and match.group('period') in ('下午', '傍晚', '晚上', '夜里'):
            hour_value += 12
        if not (0 <= hour_value <= 23 and 0 <= minute_value <= 59):
            raise AppError('DATE_UNCLEAR', '时刻超出范围，请确认小时与分钟。', 422)
        resolved = resolved.replace(hour=hour_value, minute=minute_value)
        precision = 'datetime'
    elif match.group('period'):
        resolved = resolved.replace(hour=PERIOD_WORDS[match.group('period')])
        precision = 'period'
    return {'started_at': resolved.isoformat(),
            'resolved_date': target_date.isoformat(),
            'time_precision': precision,
            'phrase': text,
            'reference_at': base.isoformat(),
            'timezone': str(zone),
            'source': 'relative',
            'warnings': ([] if precision == 'datetime' else
                         ['未提供具体时刻，按该日 00:00 或时段起点记录；如需要小时级提醒，请补充时刻。'])}

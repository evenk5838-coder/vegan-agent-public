"""食材期限建议：确定性的只读试算服务。

用途（页面、草稿卡、Agent、正式重评共用同一实现）：
- 判断当前条件能否得到**有依据**的期限；
- 区分「缺信息」与「信息齐全但没有适用已发布规则」；
- 不写库、不发提醒、不调用外部服务。

依据优先级：有效包装说明 → 已发布且条件匹配的规则 → 无依据（unknown）。
用户自设日期不参与系统建议，按既有 `deadline_source='user'` 单独标注。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import re

from .. import storage_rules
from ..core import now
from . import foods as food_service
from .food_planning_reference import planning_reference

ADVICE_VERSION = 'food-advice-v5-source-planning-reference'

# 判定必需的信息：缺这些无法评估（不是"必须全填"）
REQUIRED_BY_CLASS = {
    'fresh': ('storage', 'date_type', 'started_at'),
    'cooked': ('storage', 'date_type', 'started_at'),
    'packaged': ('storage', 'date_type', 'started_at'),
    'unknown': ('food_class',),
}


def _parse(value):
    """解析带时区的时间；非法或缺少时区返回 None（由调用方转成 422）。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


# 包装说明里常见条件词 → 与记录实际条件比对
_STORAGE_WORDS = {'冷藏': 'refrigerated', '冷冻': 'frozen', '常温': 'ambient'}
_OPENED_WORDS = ('开封后', '已开封', '启封')
_TEMP_UNIT = r'(?:℃|°\s*C|摄氏度|C(?![a-z]))'
_TEMP_RANGE = re.compile(rf'(-?\d+(?:\.\d+)?)\s*(?:{_TEMP_UNIT})?\s*[—–\-~～至到]\s*(-?\d+(?:\.\d+)?)\s*{_TEMP_UNIT}', re.I)
_TEMP_SINGLE = re.compile(rf'(-?\d+(?:\.\d+)?)\s*{_TEMP_UNIT}', re.I)


def validated_dates(payload: dict) -> None:
    """校验日期字段：非法或缺时区 → 明确 422，而不是 500。"""
    from ..core import AppError
    for key, label in (('expires_at', '期限日期'), ('started_at', '起始时刻')):
        raw = payload.get(key)
        if raw in (None, ''):
            continue
        if _parse(raw) is None:
            raise AppError('INVALID_TIME', f'{label}需要合法的带时区时间（例如 2026-10-26T09:00:00+08:00）。', 422)


def _storage_matches(note: str, payload: dict) -> bool:
    wanted = {v for k, v in _STORAGE_WORDS.items() if k in note}
    for pattern, storage in ((r'\b(?:refrigerat(?:e|ed|ion|or)|chilled|chill)\b', 'refrigerated'),
                             (r'\b(?:frozen|freez(?:e|er|ing))\b', 'frozen'),
                             (r'\b(?:room[ -]+temperature|ambient)\b', 'ambient')):
        if re.search(pattern, note, re.I):
            wanted.add(storage)
    if re.search(r'(?:不可|不得|不要|禁止|切勿|勿)\s*(?:冷藏|冷冻)|\b(?:do\s+not|never|avoid)\s+(?:refrigerate|freeze|freezing)\b', note, re.I):
        return False
    if not wanted:
        return True                      # 说明未限定储存方式，不因此判不匹配
    # “未开封常温/开封后冷藏”等多分支不能把任一词命中当作条件匹配。
    return len(wanted) == 1 and payload.get('storage') in wanted


def _temperature_matches(note: str, payload: dict) -> bool:
    """Interpret only explicit Celsius bounds; never invent a tolerance."""
    matches = list(_TEMP_SINGLE.finditer(note))
    ranges = list(_TEMP_RANGE.finditer(note))
    if not matches and not ranges:
        # A stated but unsupported unit/approximation is not “no condition”.
        return not re.search(r'℃|°\s*[CF]|摄氏|华氏', note, re.I)
    try:
        temp = float(payload.get('temperature_c'))
        if not math.isfinite(temp):
            return False
    except (TypeError, ValueError):
        return False
    if ranges:
        if len(ranges) != 1 or any(not (ranges[0].start() <= item.start() < ranges[0].end()) for item in matches):
            return False
        low, high = float(ranges[0].group(1)), float(ranges[0].group(2))
        before, after = note[:ranges[0].start()].casefold(), note[ranges[0].end():].casefold()
        if (re.search(r'(?:约|大约|around|about|[≤≥<>])\s*$', before)
                or re.match(r'\s*(?:左右|上下|以下|以上|以内|附近)', after)):
            return False
        return low <= high and low <= temp <= high
    if len(matches) != 1:
        return False   # multiple storage/temperature branches need an explicit review
    match = matches[0]
    expected = float(match.group(1))
    before, after = note[:match.start()].casefold(), note[match.end():].casefold()
    if (re.search(r'(?:约|大约|左右|around|about)\s*$', before)
            or re.match(r'\s*(?:左右|上下|附近|approximately)', after)):
        return False
    operators = []
    if re.search(r'(?:≤|<=|不高于|不超过|至多|最高|at\s+or\s+below|not\s+above|no\s+higher\s+than)\s*$', before):
        operators.append('le')
    if re.match(r'\s*(?:及以下|或以下|以下|or\s+below|and\s+below)', after):
        operators.append('le')
    if re.search(r'(?:≥|>=|不低于|至少|最低|at\s+or\s+above|not\s+below|no\s+lower\s+than)\s*$', before):
        operators.append('ge')
    if re.match(r'\s*(?:及以上|或以上|以上|or\s+above|and\s+above)', after):
        operators.append('ge')
    if not operators:
        if re.search(r'(?:<|低于|小于|under|below)\s*$', before):
            operators.append('lt')
        if re.search(r'(?:>|高于|大于|over|above)\s*$', before):
            operators.append('gt')
    if len(set(operators)) > 1:
        return False
    op = operators[0] if operators else 'eq'
    return {'le': temp <= expected, 'ge': temp >= expected,
            'lt': temp < expected, 'gt': temp > expected,
            'eq': temp == expected}[op]


def package_applicable(payload: dict) -> tuple[bool, str]:
    """包装依据是否适用于当前状态。

    不能只凭"有一段说明 + 一个日期"就算有效；需要结构化条件一致：
    - 必须有说明文本与非空日期；
    - 日期不能明显不合理（早于 2000 年或晚于 10 年）；
    - 已开封时，说明必须提到开封后仍适用（否则不再适用）。
    """
    note = str(payload.get('package_note') or '').strip()
    expires = _parse(payload.get('expires_at'))
    if not note or not expires:
        return False, '包装说明或日期缺失'
    if not food_service.known_storage_context(payload):
        return False, '储存方式、状态或日期信息尚不完整'
    if expires.year < 2000 or expires > now() + timedelta(days=3650):
        return False, '包装日期数值不合理'
    if payload.get('opened') is True and not any(k in note for k in _OPENED_WORDS):
        return False, '包装说明未覆盖开封后的情况'
    if payload.get('opened') is False and any(k in note for k in _OPENED_WORDS):
        return False, '该说明只适用于开封后，当前记录为未开封'
    if not _storage_matches(note, payload):
        return False, '包装说明的储存方式与当前记录不一致'
    if not _temperature_matches(note, payload):
        return False, '包装说明的温度条件与当前记录不一致（或未提供温度）'
    if payload.get('deadline_source') not in (None, '', 'package', 'unknown'):
        return False, '当前记录的期限来源不是包装说明'
    return True, '包装说明与当前条件一致'


def usage_guidance(payload: dict, *, reason: str, missing: list[str], basis: dict) -> dict:
    """Qualitative planning from recorded facts, never an expiry/safety estimate.

    No age thresholds, freshness percentages, model calls or storage-rule
    publication. Recorded dates retain their meaning and are not reset here.
    """
    observed = {key: payload.get(key) for key in
                ('food_class', 'storage', 'condition', 'opened', 'date_type', 'started_at',
                 'expires_at', 'deadline_source')}
    def view(priority, title, text):
        usage = {'priority': priority, 'title': title, 'text': text, 'observed': observed}
        reference = planning_reference(payload, reason=reason, basis=basis, at=now())
        if reference is not None:
            usage['reference'] = reference
        return usage

    if payload.get('status') in ('used', 'discarded'):
        return view('record_only', '这条记录已结束', '已标记用完或丢弃，不再安排食用。')
    recorded_date = _parse(payload.get('expires_at'))
    if reason == 'date_passed' or (recorded_date is not None and recorded_date <= now()):
        return view('check_first', '先核对已过的记录日期', '记录日期已过，请先核对包装与实际储存情况；这不判断食物是否仍可食用。')
    if reason == 'package_not_applicable':
        return view('check_first', '先核对当前情况与包装要求', '原包装说明不适用于当前记录，先核对开封状态和储存条件，不沿用原日期。')
    started = _parse(payload.get('started_at'))
    if started is not None and started > now():
        return view('check_first', '先核对记录日期', '购买、生产、制作或开封的记录日期在未来，请先确认日期含义和实际时间。')
    if missing or payload.get('food_class') not in ('fresh', 'cooked', 'packaged'):
        return view('check_first', '先补齐实际储存情况', '先核对食品类别、存放方式和日期含义，再安排使用；现在不推算食用天数。')
    if basis.get('type') == 'package':
        return view('plan_use', '按适用包装说明安排使用', '包装要求优先；开封或改变存放方式后，重新核对说明是否仍适用。')
    if payload.get('storage') == 'frozen':
        return view('plan_use', '冷冻食物按计划使用', '先核对冷冻与解冻过程及包装要求，不凭登记时长判断仍可安全食用。')
    if payload.get('food_class') == 'packaged':
        return view('check_first', '先核对开封后的包装要求' if payload.get('opened') else '先核对包装标示',
                    '按实际开封状态核对储存和使用说明，尚不生成参考天数。')
    if payload.get('storage') != 'refrigerated':
        return view('check_first', '先核对这种存放方式是否适用', '已按记录保留存放方式；先确认它适合当前食品及加工状态，再安排使用。')
    if payload.get('food_class') == 'cooked':
        return view('use_soon', '熟食优先安排，尽快食用', '先核对制作完成时间与持续储存情况；尽快食用是使用安排，不是安全保证。')
    return view('use_soon', '生鲜优先安排，尽快使用', '按记录的冷藏方式优先安排使用；不据购买或生产日期推定仍能安全食用。')


def assess(payload: dict) -> dict:
    """只读试算。返回 suggestion/missing/reason，不含任何写操作。"""
    validated_dates(payload)
    food_class = payload.get('food_class') or 'unknown'
    missing: list[str] = []
    for field in REQUIRED_BY_CLASS.get(food_class, ()):
        if payload.get(field) in (None, '', 'unknown'):
            missing.append(field)
    if food_class == 'unknown':
        missing = ['food_class'] if 'food_class' not in [m for m in missing] else missing
    if payload.get('condition') == 'unknown':
        missing.append('condition')
    # 包装成品还需要开封状态才能判断说明是否适用
    if food_class == 'packaged' and payload.get('opened') is None:
        missing.append('opened')
    if (food_class == 'cooked' and payload.get('name') in storage_rules.SOUP_NAMES
            and payload.get('storage') == 'refrigerated'):
        if payload.get('temperature_c') is None:
            missing.append('temperature_c')
        if payload.get('condition') in (None, '', 'unknown'):
            missing.append('condition')

    def result(action='unknown', date=None, basis=None, reason='missing_info', extra_missing=None):
        missing_fields = sorted(set(extra_missing if extra_missing is not None else missing))
        actual_basis = basis or {'type': 'none', 'note': ''}
        return {'action': action, 'date': date,
                'basis': actual_basis,
                'reason': reason,
                'missing': missing_fields,
                'usage': usage_guidance(payload, reason=reason, missing=missing_fields, basis=actual_basis),
                'rules_status': 'none', 'advice_version': ADVICE_VERSION,
                'notice': '仅供参考，不构成食品安全保证'}

    # ① 包装依据（仅包装成品/半成品，且条件匹配）
    if food_class == 'packaged' and not missing:
        ok, why = package_applicable(payload)
        if ok:
            expires = _parse(payload['expires_at'])
            # 已过去的包装日期要保留为“已过期”，不能变成未知
            action = 'date' if expires > now() else 'unknown'
            return {**result(action=action, date=payload['expires_at'],
                             basis={'type': 'package', 'note': payload.get('package_note', '')},
                             reason='ok' if action == 'date' else 'date_passed'),
                    'rules_status': 'none',
                    'notice': '按包装说明；该日期已过' if action == 'unknown' else '按包装说明'}
        if why != '包装说明或日期缺失':
            missing = []          # 条件齐全但包装不适用 → 不追问，直接说明不适用
            out = result(reason='package_not_applicable')
            out['basis'] = {'type': 'none', 'note': why}
            return out

    # ② 已发布且条件匹配的规则
    if not missing:
        rule, deadline, reasons = storage_rules.estimate_deadline(payload, published_only=True)
        if rule:
            passed = deadline <= now()
            return {**result(action='unknown' if passed else 'date',
                             date=deadline.isoformat() if deadline else None,
                             basis={'type': 'rule', 'rule_id': rule['rule_id'],
                                    'rule_version': rule['version'], 'rule_source': rule['source'],
                                    'date_origin': rule['date_origin'],
                                    'note': rule['note']},
                             reason='date_passed' if passed else 'ok'),
                    'rules_status': 'published'}
        # 信息齐全但没有适用的已发布规则：不再反复追问
        out = result(reason='no_published_rule')
        out['basis']['note'] = '；'.join(reasons)
        out['rules_status'] = 'none'
        return out

    return result()


def suggestion_view(payload: dict) -> dict:
    """给草稿项/食材列表用的紧凑视图（兼容既有 suggested 字段）。"""
    advice = assess(payload)
    return {'action': advice['action'], 'date': advice['date'], 'basis': advice['basis'],
            'reason': advice['reason'], 'missing': advice['missing'],
            'rules_status': advice['rules_status'], 'advice_version': advice['advice_version'],
            'usage': advice['usage']}


def draft_digest(items: list[dict]) -> str:
    """草稿内容摘要：用于确认前复核与幂等，不含每秒变化的时钟。"""
    from ..core import digest
    import json
    rows = [[i.get('item_id'), {k: i.get(k) for k in sorted(i) if k not in ('saved_food_id', 'actual')}]
            for i in sorted(items, key=lambda x: x.get('item_id') or '')]
    return digest(json.dumps([rows, ADVICE_VERSION, storage_rules.RULES_VERSION],
                             ensure_ascii=False, sort_keys=True, default=str))


def formal_view(payload: dict) -> dict:
    """正式食材的读取口径：派生状态 + 建议，两者一致。"""
    return {**food_service.progress(payload), 'suggestion': suggestion_view(payload)}

"""Source-scoped storage drafts; quality/handling references never create expiry.

All three entries remain draft. Only a published expiry estimate with matching
food class, preparation state, temperature and date semantics may yield a date.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

RULES_VERSION = 'storage-rules-draft-2026-10-26.2'
RULES_STATUS = '来源复核草稿待复核发布：未发布条目不生成正式期限或提醒'
PUBLISHED_STATUS = 'published'
USABLE_STATUSES = (PUBLISHED_STATUS,)
SOURCES = {
    'usda-cold': 'https://www.foodsafety.gov/food-safety-charts/cold-food-storage-charts',
    'usda-leftovers': 'https://www.fsis.usda.gov/food-safety/safe-food-handling-and-preparation/food-safety-basics/leftovers-and-food-safety',
    'cfs-danger-zone': 'https://www.cfs.gov.hk/sc_chi/trade_zone/safe_kitchen/Temperature_Danger_Zone.html',
    'fsanz-2h4h': 'https://www.foodstandards.gov.au/business/food-safety/2-hour-4-hour-rule',
}
SOUP_NAMES = ('蔬菜汤', '熟制蔬菜汤', '纯蔬菜汤', '蔬菜炖汤', '蔬菜炖菜')

RULES = [
    {'rule_id': 'cooked-refrigerated', 'version': RULES_VERSION,
     'names': SOUP_NAMES, 'name_match': 'exact',
     'food_classes': ('cooked',), 'storage': 'refrigerated', 'conditions': ('cooked',),
     'date_types': ('prepared',), 'date_origin': 'preparation_finished_at',
     'hours': 4 * 24, 'source_days_range': (3, 4), 'hours_is_upper_bound': True,
     'rule_type': 'expiry_estimate', 'can_estimate_expiry': True,
     'temperature_c': (0.0, 4.0), 'locations': None,
     'source_temperature_ceiling_c': 4.0,
     'handling_assumptions': ['制作后及时分装冷却并冷藏', '连续保持适用冷藏温度', '此前没有不明室温暴露或冷链中断'],
     'note': '仅对已熟制的蔬菜汤/蔬菜炖汤，参考原表冷藏3–4天的上限；从制作完成时计时，'
             '不从再次放入冰箱、重新登记或转存时续期。原表冷藏上限4℃；本项目仅覆盖0–4℃。'
             '需正确处理和持续冷藏，不能据此保证食用安全。',
     'source': SOURCES['usda-cold'], 'source_section': 'Soups and stews / Vegetable or meat added',
     'source_checked_at': '2026-10-26', 'status': 'draft'},
    {'rule_id': 'cooked-frozen', 'version': RULES_VERSION,
     'names': SOUP_NAMES, 'name_match': 'exact',
     'food_classes': ('cooked',), 'storage': 'frozen', 'conditions': ('cooked',),
     'date_types': ('prepared',), 'date_origin': None,
     'hours': None, 'rule_type': 'quality_reference', 'can_estimate_expiry': False,
     'quality_months_range': (2, 3), 'temperature_c': (None, -18.0), 'locations': None,
     'note': '原表蔬菜汤/炖汤冷冻2–3个月是品质参考，不是安全到期日。'
             '来源说明持续在-18℃或以下冷冻的食品不以该月数判定安全到期；'
             '不覆盖冷冻前暴露、解冻后存放或冷链中断，本系统不据此计算expiry或自动提醒。',
     'source': SOURCES['usda-cold'], 'source_section': 'Freezer guidance and Soups and stews',
     'source_checked_at': '2026-10-26', 'status': 'draft'},
    {'rule_id': 'fresh-produce-ambient-short', 'version': RULES_VERSION,
     'legacy_id_note': '保留旧标识以便审计；已撤销“任意切果蔬2小时到期”的旧含义。',
     'names': SOUP_NAMES, 'name_match': 'exact',
     'food_classes': ('cooked',), 'storage': 'ambient', 'conditions': ('cooked',),
     'date_types': ('prepared',), 'date_origin': None,
     'hours': None, 'rule_type': 'handling_reference', 'can_estimate_expiry': False,
     'temperature_c': (5.0, 60.0), 'locations': None,
     'requires_cumulative_exposure': True,
     'handling_windows_hours': {'under_2': '可使用或放回冷藏',
                                'from_2_to_4': '立即使用，不再放回冷藏留用',
                                'over_4': '丢弃'},
     'note': 'FSANZ的2小时/4小时原则针对脱离冷藏、处于5–60℃的潜在危险食品，'
             '累计包括准备、储存、运输和展示时间，且与烹调后分阶段冷却要求不同。'
             '这里仅保留熟制蔬菜汤的处理参考，不适用于所有切果蔬；'
             '制作时间或本次登记时间不能代替累计暴露时间，不生成expiry或自动提醒。',
     'source': SOURCES['fsanz-2h4h'], 'source_section': 'Potentially hazardous food; cumulative time',
     'source_checked_at': '2026-10-26', 'status': 'draft'},
]


def applicability(rule: dict, name: str, storage: str | None, condition: str | None,
                  temperature_c: float | None = None, location: str | None = None,
                  food_class: str | None = None, date_type: str | None = None) -> list[str]:
    reasons: list[str] = []
    if rule.get('storage') and storage != rule['storage']:
        reasons.append('存放方式')
    if rule.get('conditions') and condition not in rule['conditions']:
        reasons.append('食材状态')
    if rule.get('food_classes') and food_class not in rule['food_classes']:
        reasons.append('食品类别')
    if rule.get('date_types') and date_type not in rule['date_types']:
        reasons.append('日期含义（需制作完成时间）')
    if rule.get('names'):
        matched = (name or '').strip() in rule['names'] if rule.get('name_match') == 'exact' else any(
            key in (name or '') for key in rule['names'])
        if not matched:
            reasons.append('食材类别')
    temperature_range = rule.get('temperature_c', 'unspecified')
    if temperature_range == 'unspecified':
        reasons.append('温度条件')
    elif temperature_range is not None:
        if temperature_c is None:
            reasons.append('温度数值')
        else:
            try:
                value = float(temperature_c)
                low, high = temperature_range
                matched = math.isfinite(value) and (low is None or low <= value) and (high is None or value <= high)
            except (TypeError, ValueError):
                matched = False
            if not matched:
                reasons.append('温度范围')
    locations = rule.get('locations', 'unspecified')
    if locations == 'unspecified':
        reasons.append('存放地点')
    elif locations is not None and location not in locations:
        reasons.append('存放地点' if location in (None, '', 'unknown') else '地点条件')
    return reasons


def is_published(rule: dict) -> bool:
    return rule.get('status') in USABLE_STATUSES


def can_estimate_expiry(rule: dict) -> bool:
    hours = rule.get('hours')
    return bool(rule.get('can_estimate_expiry') is True
                and rule.get('rule_type') == 'expiry_estimate'
                and isinstance(hours, (int, float)) and not isinstance(hours, bool)
                and math.isfinite(hours) and hours > 0)


def lookup(name: str, storage: str | None, condition: str | None, rule_id: str | None = None,
           temperature_c: float | None = None, location: str | None = None,
           published_only: bool = False, *, food_class: str | None = None,
           date_type: str | None = None, for_estimates: bool = False):
    """Explicit IDs follow exactly the same scope/date checks as automatic lookup."""
    candidates = [rule for rule in RULES if not rule_id or rule['rule_id'] == rule_id]
    if not candidates:
        return None, ['规则不存在']
    if not rule_id:
        same_storage = [rule for rule in candidates if rule.get('storage') == storage]
        if same_storage:
            candidates = same_storage
    reasons_seen = []
    for rule in candidates:
        reasons = applicability(rule, name, storage, condition, temperature_c, location, food_class, date_type)
        if for_estimates and not can_estimate_expiry(rule):
            reasons.append('仅处理/品质参考，不提供到期依据')
        if published_only and not is_published(rule):
            reasons.append('规则尚未发布')
        if not reasons:
            return rule, []
        if rule_id:
            return None, reasons
        reasons_seen.extend(reasons)
    return None, list(dict.fromkeys(reasons_seen))


def estimate_deadline(payload: dict, rule_id: str | None = None, published_only: bool = True):
    """Shared pure date interpretation for read-only advice and saved reevaluation.

    `started_at` means preparation finished only when `date_type=prepared`;
    purchase/production/opened/current storage time cannot be substituted.
    """
    rule, reasons = lookup(payload.get('name', ''), payload.get('storage'), payload.get('condition'),
                           rule_id, payload.get('temperature_c'), payload.get('location'),
                           published_only=published_only, food_class=payload.get('food_class'),
                           date_type=payload.get('date_type'), for_estimates=True)
    if not rule:
        return None, None, reasons
    for previous in payload.get('storage_history') or []:
        # This first reviewed scope does not cover thawing, room-temperature
        # excursions or an earlier unknown/out-of-range cold chain.
        if previous.get('storage') != 'refrigerated':
            return None, None, ['此前储存过程不在本条连续冷藏适用范围']
        previous_temp = previous.get('temperature_c')
        try:
            valid_temp = previous_temp is not None and math.isfinite(float(previous_temp)) and 0 <= float(previous_temp) <= 4
        except (TypeError, ValueError):
            valid_temp = False
        if not valid_temp:
            return None, None, ['此前冷藏温度缺失或不符合范围']
    try:
        start = datetime.fromisoformat(str(payload.get('started_at') or ''))
    except (TypeError, ValueError):
        return None, None, ['制作完成时间缺失或无效']
    if start.tzinfo is None or start.utcoffset() is None:
        return None, None, ['制作完成时间缺少时区']
    # Only this historical origin is used. Refrigeration/freezing changes don't
    # reset it, and a future preparation time cannot form a current estimate.
    if start > datetime.now(start.tzinfo):
        return None, None, ['制作完成时间在未来']
    return rule, start + timedelta(hours=rule['hours']), []


def describe():
    rules = [{key: (list(value) if isinstance(value, tuple) else value) for key, value in rule.items()} for rule in RULES]
    published = [rule['rule_id'] for rule in RULES if is_published(rule)]
    usable = [rule['rule_id'] for rule in RULES if is_published(rule) and can_estimate_expiry(rule)]
    return {'rules_version': RULES_VERSION, 'rules_status': RULES_STATUS,
            'publish_gate': '仅已发布、可生成期限且类别/日期/温度等条件均适用的规则可估算；品质与处理参考不生成期限。',
            'sources': SOURCES, 'published_rule_ids': published, 'estimate_rule_ids': usable,
            'usable_for_estimates': bool(usable),
            'rules': [{**rule, 'usable_for_estimates': is_published(rule) and can_estimate_expiry(rule)} for rule in rules]}

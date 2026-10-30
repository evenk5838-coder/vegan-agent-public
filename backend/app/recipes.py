"""菜谱筛选：列表、搜索与对话工具共用同一套过滤条件，保证结果一致。

旧记录只有 breakfast/main，不自动改写；main 显示为「正餐（待补分类）」。
"""
from __future__ import annotations

import re

from .core import AppError
from .rules import evaluate

MEALS = ('breakfast', 'lunch', 'dinner', 'afternoon_tea', 'main')
# 浏览分类仅中式／西式；foreign不等于western，旧值不迁移或强行细分。
CUISINE_GROUPS = {'chinese': 'chinese', 'western': 'western', 'other': 'unclassified'}
CUISINE_GROUP_LABELS = {'chinese': '中式', 'western': '西式', 'unclassified': '待补分类'}
TAGS = ('dessert',)
PURPOSES = ('everyday', 'protein', 'fat')
CUISINES = ('chinese', 'western', 'other')
DIETS = ('pure', 'vegan', 'lacto_ovo')


def normalize_filters(meal=None, tag=None, purpose=None, diet=None, cuisine=None) -> dict:
    def pick(value, allowed, label):
        if value in (None, ''):
            return None
        if value not in allowed:
            raise AppError('INVALID_FILTER', f'{label}条件不支持：{value}', 422)
        return value

    return {'meal': pick(meal, MEALS, '餐次'), 'tag': pick(tag, TAGS, '标签'),
            'purpose': pick(purpose, PURPOSES, '用途'), 'diet': pick(diet, DIETS, '饮食标准'),
            'cuisine': pick(cuisine, CUISINES, '菜系')}


_UNIT = r'(?:g|kg|ml|l|克|千克|毫升|升|大匙|小匙|茶匙|汤匙|杯|瓣|根|个|只|片|颗|粒|把|束|块|条|张|少许|适量|按需|可选)'
_AMOUNT = r'(?:\d+(?:\.\d+)?|数|半)'
_QUANTITY = rf'{_AMOUNT}\s*(?:[-–~～至到]\s*{_AMOUNT})?\s*'
_BRACKETS = re.compile(r'[（(【\[][^）)】\]]*[）)】\]]')
_LEAD_QTY = re.compile(rf'^\s*{_QUANTITY}{_UNIT}?\s*')
_INNER_QTY = re.compile(rf'\s*{_QUANTITY}{_UNIT}\s*')
_TRAIL_PUNCT = re.compile(r'[，,、;；:：\s]+$')
_DESCRIPTOR = re.compile(r'\s*(?:按口味|适量|少许|少量|可选|按需)\s*$')


def normalize_ingredient(value: str) -> str:
    '''把展示用的食材（带用量/括号说明）还原成规则可判定的名称。

    例："蒜 2 瓣"→"蒜"；"鸡肉 100g"→"鸡肉"；"植物鸡 200g（实际成分待核对）"→"植物鸡"。
    只做**展示信息剥离**，不猜测成分；未知复合产品仍由规则判为待核实。
    '''
    text = _BRACKETS.sub(' ', str(value or ''))
    text = _LEAD_QTY.sub('', text)
    text = _INNER_QTY.sub(' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = _DESCRIPTOR.sub('', text)
    return _TRAIL_PUNCT.sub('', text)


DIET_LABELS = {'pure': '纯净素', 'vegan': 'Vegan', 'lacto_ovo': '蛋奶素'}


def diet_labels(evaluation: dict) -> dict:
    """三类**可重叠**的独立结果：每类给出适配/不适配/待核实 + 依据。

    不适配时列出命中项；待核实列出未知项与不完整标记；适配则说明依据为可见配料+规则版本。
    """
    results = (evaluation or {}).get('results') or {}
    version = (evaluation or {}).get('rules_version')
    labels = {}
    for key, name in DIET_LABELS.items():
        item = results.get(key) or {}
        status = item.get('status') or 'uncertain'
        labels[key] = {'name': name, 'status': status,
                       'denied': item.get('denied') or [],
                       'unknown': item.get('unknown') or [],
                       'incomplete': bool(item.get('incomplete')),
                       'basis': {'rules_version': version,
                                 'scope': (evaluation or {}).get('scope', '')}}
    return labels


def _ingredient_text(row: dict) -> str:
    '''个人菜谱是整段文本；公共条目是逐项数组。规则判定统一要可识别的名称。'''
    # Old cached rule_ingredients may have dropped parenthesized subingredients.
    # Original source ingredients must take precedence over such derived data.
    value = row.get('ingredients')
    if not value:
        value = row.get('rule_ingredients')
    # Safety evaluation receives the entire source ingredient. Display cleanup
    # must not remove animal / egg-milk subingredients inside parentheses.
    parts = [str(v) for v in value if str(v).strip()] if isinstance(value, (list, tuple)) else [str(value or '')]
    # A translated display must never erase disqualifying evidence in the original.
    original = row.get('original_ingredients')
    if isinstance(original, (list, tuple)):
        parts.extend(item for item in original if isinstance(item, str) and item.strip())
    return '、'.join(parts)


def cuisine_group(value) -> str:
    """把旧 cuisine 值映射到顶层分类；未知/空/other 一律“待补分类”，不强行归入外国菜。"""
    return CUISINE_GROUPS.get(str(value or '').strip(), 'unclassified')


def evaluate_recipe(row: dict, preferences: dict) -> dict:
    # Derive the legacy version before computed display/evaluation fields change.
    # Import here avoids coupling the pure filtering module to the services package at import time.
    if row.get('scope') != 'public' and row.get('origin') != 'public_knowledge':
        from .services.recipe_save import recipe_version
        row = {**row, 'version': recipe_version(row)}
    row = {**row, 'tags': row.get('tags') or []}
    row['cuisine_group'] = cuisine_group(row.get('cuisine'))
    row['cuisine_group_label'] = CUISINE_GROUP_LABELS[row['cuisine_group']]
    row['evaluation'] = evaluate(_ingredient_text(row), True, [],
                                 preferences.get('avoid_coriander', False))
    row['diet_labels'] = diet_labels(row['evaluation'])
    return row


def apply_filters(rows: list[dict], filters: dict, preferences: dict) -> list[dict]:
    """对本人菜谱应用餐次/标签/用途/菜系/饮食条件；列表、搜索、对话工具共用。"""
    result = []
    for row in rows:
        row = evaluate_recipe(row, preferences)
        if filters.get('meal') and row.get('meal') != filters['meal']:
            continue
        if filters.get('tag') and filters['tag'] not in row['tags']:
            continue
        if filters.get('purpose') and row.get('purpose') != filters['purpose']:
            continue
        if filters.get('cuisine') and row.get('cuisine') != filters['cuisine']:
            continue
        diet = filters.get('diet')
        if diet and (row['evaluation']['results'][diet]['status'] != 'suitable'
                     or row['evaluation']['personal_avoidances']):
            continue
        result.append(row)
    return result

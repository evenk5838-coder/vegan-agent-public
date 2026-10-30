"""Exact, conditional source notes for display; never an expiry rule or write path."""
from __future__ import annotations

from datetime import datetime
import math

LEAF_NAMES = frozenset(('菠菜', '生菜', '油麦菜', '小白菜', '上海青', '青菜'))
SOUP_NAMES = frozenset(('蔬菜汤', '熟制蔬菜汤', '纯蔬菜汤', '蔬菜炖汤', '蔬菜炖菜'))
LEAF_SOURCE = 'https://www.gov.cn/xinwen/2017-07/10/5209061/files/a9812c90b4bb40b3b4eb045368bd454c.pdf'
TOFU_SOURCE = 'https://www.yangjiang.gov.cn/yj/hdjl/wdzsk/sscjdglj/spjg/content/post_751857.html'
SOUP_SOURCE = 'https://www.foodsafety.gov/food-safety-charts/cold-food-storage-charts'


def _date(value):
    try:
        parsed = datetime.fromisoformat(str(value or ''))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def planning_reference(payload: dict, *, reason: str, basis: dict, at: datetime) -> dict | None:
    """Conditions are source requirements to check, not assertions about this food."""
    if (payload.get('status') in ('used', 'discarded') or payload.get('needs_review')
            or reason in ('date_passed', 'package_not_applicable', 'invalid_evidence')
            or basis.get('type') == 'package' or payload.get('deadline_source') == 'package'):
        return None
    started = _date(payload.get('started_at'))
    if (started is None or started > at
            or payload.get('date_type') not in ('purchase', 'production', 'prepared', 'opened')
            or payload.get('storage') not in ('ambient', 'refrigerated', 'frozen')
            or payload.get('condition') not in ('raw', 'cut', 'cooked', 'opened', 'unopened')):
        return None
    expires = _date(payload.get('expires_at'))
    if payload.get('expires_at') and (expires is None or expires <= at):
        return None
    name = str(payload.get('name') or '').strip()

    def note(title, text, source_url, source_title, conditions):
        return {'title': title, 'text': text + ' 这是有条件的使用安排资料，不是安全保证，也不是本条记录的自动期限。',
                'source_url': source_url, 'source_title': source_title,
                'conditions': conditions, 'kind': 'planning_reference'}

    if (name in LEAF_NAMES and payload.get('food_class') == 'fresh'
            and payload.get('condition') == 'raw' and payload.get('storage') == 'refrigerated'
            and payload.get('date_type') in ('purchase', 'production')):
        return note('绿叶菜食用安排参考',
                    '2017年官方资料对软纸包裹、装入保鲜袋并冷藏的绿叶菜，建议一般在3天内吃完；'
                    '请先对照这些处理条件，本记录未确认纸包或此前冷链，不能据登记日期顺延这段时间。',
                    LEAF_SOURCE, '国家食品药品监督管理总局/中国食品科学技术学会《如何吃得更安全》（2017），PDF第64页/印刷第56页',
                    ['待对照：新鲜、未加工的绿叶菜', '待对照：软纸包裹后装保鲜袋并冷藏',
                     '纸包、保鲜袋及此前储存过程未由本条记录确认；不适用于切制或熟制蔬菜'])

    if (name == '豆腐' and payload.get('food_class') in ('fresh', 'packaged')
            and payload.get('condition') in ('raw', 'opened', 'unopened')
            and payload.get('storage') in ('ambient', 'refrigerated')):
        return note('豆腐尽快食用参考',
                    '阳江市市场监督管理局建议豆腐购买后及时、尽快食用；真空包装打开后，'
                    '不能继续沿用未开封保质期。这份资料没有给出适用于所有豆腐的通用天数，'
                    '也不表示常温存放或当前记录仍适宜食用。',
                    TOFU_SOURCE, '阳江市市场监督管理局《关于豆腐的消费提示》（2023-12-11）',
                    ['仅精确名称“豆腐”，不泛化其他豆制品',
                     '待对照：实际储存条件与包装要求；包装开封后需重新核对，未开封日期不能直接沿用'])

    if (name in SOUP_NAMES and payload.get('food_class') == 'cooked'
            and payload.get('condition') == 'cooked' and payload.get('storage') == 'refrigerated'
            and payload.get('date_type') == 'prepared'):
        temp = payload.get('temperature_c')
        if temp is not None:
            try:
                if isinstance(temp, bool) or not math.isfinite(float(temp)) or not 0 <= float(temp) <= 4:
                    return None
            except (ValueError, TypeError):
                return None
        for previous in payload.get('storage_history') or []:
            if previous.get('storage') != 'refrigerated':
                return None
            previous_temp = previous.get('temperature_c')
            try:
                if previous_temp is not None and (isinstance(previous_temp, bool)
                        or not math.isfinite(float(previous_temp)) or not 0 <= float(previous_temp) <= 4):
                    return None
            except (ValueError, TypeError):
                return None
        return note('熟制蔬菜汤/炖菜冷藏参考',
                    '美国官方冷藏表对汤与炖菜列出3–4天参考，并要求冷藏不高于4℃；'
                    '还需对照制作后及时分装冷却、持续冷藏及此前暴露情况。'
                    '这些处理条件未由登记记录完整证明，不能从登记、重新冷藏或转存时重新起算。',
                    SOUP_SOURCE, 'FoodSafety.gov（USDA等）Cold Food Storage Chart：Soups and stews',
                    ['待对照：已熟制蔬菜汤/蔬菜炖菜，从制作完成时理解资料，不从登记时重算',
                     '待对照：及时分装冷却并冷藏，持续保持不高于4℃',
                     '待对照：此前无不明室温暴露、解冻或冷链中断；未确认条件不等于已满足'])
    return None

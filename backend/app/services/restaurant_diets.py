"""Independent, sourced menu availability labels; never a whole-shop certification.

AMap names/categories establish candidate identity only. Labels expire and remain
unknown unless a reviewed branch-level source supports that particular diet.
"""
from __future__ import annotations

import copy
from datetime import date
from zoneinfo import ZoneInfo

from ..core import now

VERSION = 'restaurant-diets-2026-10-30.1'
LABELS = {'pure': '纯净素', 'vegan': 'Vegan', 'lacto_ovo': '蛋奶素'}
PROFILES = (
    {'id': 'lamdre-beijing-gongti', 'names': ('兰斋Lamdre植物料理餐厅', '兰斋', 'Lamdre'),
     'poi_ids': ('B0I0Y7WWTD',), 'address_token': '工体北路4号',
     'source': 'https://guide.michelin.com/en/beijing-municipality/beijing/restaurant/lamdre',
     'title': '米其林指南 · Lamdre', 'reviewed_at': '2026-10-30', 'expires_at': '2026-11-29',
     'claims': {'vegan': ('supported', '指南列为Vegan，主厨介绍菜单采用植物性食材。'),
                'lacto_ovo': ('supported', '按已载明的Vegan菜单推导兼容；并不表示提供蛋奶菜品。')}},
    {'id': 'kingsjoy-beijing-wudaoying', 'names': ('京兆尹', "King's Joy", '京兆尹餐厅'),
     'poi_ids': (), 'address_token': '五道营胡同2号',
     'source': 'https://guide.michelin.com/sg/zh_CN/beijing-municipality/beijing/restaurant/king-s-joy',
     'title': '米其林指南 · 京兆尹', 'reviewed_at': '2026-10-30', 'expires_at': '2026-11-29',
     'claims': {'lacto_ovo': ('supported', '指南说明提供蛋奶素套餐。'),
                'vegan': ('on_request', '指南说明可按要求调整为纯素，需预订时确认。')}},
)


def classify(item: dict) -> dict:
    labels = {key: {'label': label, 'status': 'unknown', 'basis': '尚无适用于本店的菜单依据。',
                    'sources': [], 'needs_confirmation': True} for key, label in LABELS.items()}
    today = now().astimezone(ZoneInfo('Asia/Shanghai')).date()
    for profile in PROFILES:
        # Do not apply a chain's brand label to every branch or a lookalike name.
        name_matches = str(item.get('name') or '').strip() in profile['names']
        branch_matches = item.get('id') in profile['poi_ids'] or profile['address_token'] in str(item.get('address') or '')
        if not name_matches or not branch_matches:
            continue
        if not date.fromisoformat(profile['reviewed_at']) <= today < date.fromisoformat(profile['expires_at']):
            continue
        source = {key: profile[key] for key in ('title', 'source', 'reviewed_at', 'expires_at')}
        for diet, (status, basis) in profile['claims'].items():
            labels[diet].update(status=status, basis=basis, sources=[copy.deepcopy(source)])
        break
    return {'version': VERSION, 'kind': 'menu_availability', 'labels': labels,
            'notice': '按本店公开菜单资料分别判断，出发前请向店家确认；未知不等于不适配。纯净素还需核对五辛。'}

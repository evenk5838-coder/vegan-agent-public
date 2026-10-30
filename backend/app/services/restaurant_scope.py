"""餐厅候选准入：判断某个 POI 能否作为“素食餐厅”候选。

背景：请求参数 `types=050120`（中式素菜馆）只约束**检索条件**，
供应商返回的每条 POI 仍可能属于其他类别（美甲店、牛肉米线、烧烤等）。
因此必须用**单店自己的类别字段**复核，不能拿请求参数当证据。

判定原则（v3）：
- 店名有明确素食身份、单店自身类别为餐饮、且无明确冲突 → 准入；
- 供应商可能把肉店误标为050120，“中式素菜馆”不能同时充当第二份身份依据；
- 明确肉食、非餐饮、证据缺失或冲突 → 不直接作为蔬食推荐；
- 不因店名含“素”一字就放行（那只是店名，不是菜单证据）；
- 仿荤保护只作用于紧邻植物/素前缀的食物词，不豁免店名其余荤食线索；
- 类别线索不等于 Vegan 认证，也不代表全部菜品符合要求。
"""
from __future__ import annotations

import re

# 准入规则版本：判定逻辑有实质变化时必须升版本，前端据此区分“旧漏洞版本”的历史结果。
# v1 = e437105（前缀匹配、素食优先，已知放过冲突项）—— 不要再用 v1 作为“已通过”的依据。
# v2 = 冲突优先 + 050120 叶子码精确匹配，仍受上游误分类影响。
# v3 = 独立的店名素食身份 + 单店餐饮类别双证据，局部仿荤保护。
RULE_VERSION = 'veg-admission-v3'

# 官方 POI 分类表：050120 = 餐饮服务 → 中餐厅 → 中式素菜馆（叶子码，精确匹配）
VEG_TYPECODES = ('050120',)
FOOD_TYPECODE_PREFIX = '05'          # 餐饮服务大类

# 供应商标注的类别里出现这些词，视为明确肉食/荤食餐饮
MEAT_MARKERS = (
    '牛肉', '猪肉', '羊肉', '鸡肉', '鸭肉', '鱼肉', '驴肉', '兔肉', '鹅肉', '海鲜', '河鲜',
    '烤鸭', '烧鸭', '鸭脖', '火腿', '培根', '腊肉', '香肠', '肉夹馍', '卤肉', '生烫',
    '烤肉', '烧烤', '炸鸡', '牛排', '羊排', '猪排', '鱼头', '肉丸', '肉饼', '肥牛',
    '猪脚', '猪蹄', '猪骨', '牛骨', '排骨', '羊蝎子', '羊杂', '牛杂', '肥肠', '卤煮',
    '烤鸡', '烧鸡', '炖鸡', '黄焖鸡', '盐焗鸡', '白切鸡', '手撕鸡', '叫花鸡',
    '鸡爪', '鸡翅', '鸡汤', '鸭血', '鸭肠', '烧鹅', '烤鱼', '酸菜鱼', '水煮鱼',
    '生鱼片', '刺身', '鱼丸', '虾仁', '龙虾', '烤虾', '虾饺', '蟹肉', '螃蟹',
    '生蚝', '牡蛎', '扇贝', '蛤蜊', '牛蛙',
)

# 类别里的素食线索仅用于描述；不代替独立的店名身份。
VEG_MARKERS = ('素菜', '素食', '素斋', '斋菜', '净素')
NAME_VEG_MARKERS = ('素食', '素菜', '蔬食', '全素', '纯素', '净素', '素斋', '斋菜',
                    '斋饭', '植物料理', '植物基餐厅', '植物性餐厅')
ENGLISH_VEG = re.compile(r'\b(?:vegan|vegetarian|plant[ -]based)\b', re.I)
ENGLISH_MEAT = re.compile(r'\b(?:beef|pork|chicken|duck|lamb|mutton|fish|seafood|shrimp|prawn|crab|bacon|ham|steak|bbq|barbecue)\b', re.I)
NEGATED_IDENTITY = re.compile(r'(?:非素食|非素菜|荤素|兼售荤食|non[ -](?:vegan|vegetarian))', re.I)

# type 文本里的人间非餐饮线索：与素食码/素食词冲突时按冲突处理，不因有“素”字放行
# 店名兜底：供应商类别可能缺失或误标，用**明确的**非餐饮/荤食词补充判断。
NAME_NONFOOD = ('美甲', '美睫', '美发', '理发', '美容', '足疗', '按摩', '养生馆',
                '摄影', '洗衣', '干洗', '药房', '便利店', '花店')
NAME_MEAT = (*MEAT_MARKERS, '串吧', '炸串', '烧肉')

# 只移除紧邻前缀的具体仿荤词，绝不因名字有“素”就清除整段肉食冲突。
# 例如“植物牛肉素食馆·猪脚饭”只保护植物牛肉，仍会拒绝猪脚饭。
_IMITATION_TERMS = sorted(set((*NAME_MEAT, '鸡', '鸭', '鱼', '肉', '虾', '蟹', '汉堡')), key=len, reverse=True)
IMITATION_FOOD = re.compile(r'(?:纯素|全素|素食|植物基|植物性|植物|仿荤|仿|素)(?:' +
                            '|'.join(re.escape(term) for term in _IMITATION_TERMS) + r')')
ENGLISH_IMITATION = re.compile(r'\b(?:vegan|vegetarian|plant[ -]based|mock)\s+(?:beef|pork|chicken|duck|lamb|mutton|fish|seafood|shrimp|prawn|crab|bacon|ham|steak|bbq|barbecue)\b', re.I)

NON_FOOD_MARKERS = ('生活服务', '美容', '美发', '美甲', '购物服务', '公司企业', '商务住宅',
                    '医疗保健', '学校', '汽车服务', '体育休闲', '住宿服务', '风景名胜')

REASON_TEXT = {
    'veg_category': '店名明确标注素食身份，平台分类为素食餐饮',
    'veg_type': '店名明确标注素食身份，平台类别为餐饮服务',
    'meat': '店名或平台类别存在明确荤食线索',
    'non_food': '平台类别不是餐饮服务',
    'conflict': '平台类别同时出现素食与非素食线索',
    'missing_category': '缺少平台类别信息，无法确认是否素食',
    'invalid_category': '平台类别码格式异常，无法确认是否素食',
    'no_veg_evidence': '平台类别没有素食线索',
    'missing_identity': '店名缺少明确素食身份，平台素食分类不能单独证明',
    'missing_food_category': '缺少独立餐饮类别信息，无法确认是否为餐厅',
}


def _segments(value) -> list[str]:
    if not isinstance(value, str):
        return []
    return [seg.strip() for seg in value.replace('|', ';').split(';') if seg.strip()]


def _typecodes(item: dict) -> tuple[list[str], bool]:
    """解析类别码。兼容多值（"050120;050100"），并标记是否存在格式异常的值。"""
    raw = item.get('typecode') or item.get('typecodes') or ''
    codes: list[str] = []
    invalid = False
    for seg in _segments(raw):
        if seg.isdigit() and len(seg) == 6:      # 官方 POI 码为 6 位
            codes.append(seg)
        elif seg:
            invalid = True                        # 例如 0501209 这种畸形值
    return codes, invalid


def _is_veg_code(code: str) -> bool:
    """叶子码精确匹配：050120 是素食类别，0501209 不算。"""
    return code in VEG_TYPECODES


def _has_meat(value: str) -> bool:
    unprotected = IMITATION_FOOD.sub('', ENGLISH_IMITATION.sub('', value))
    return any(marker in unprotected for marker in NAME_MEAT) or bool(ENGLISH_MEAT.search(unprotected))


def _has_identity(name: str) -> bool:
    return not NEGATED_IDENTITY.search(name) and (any(marker in name for marker in NAME_VEG_MARKERS)
                                                or bool(ENGLISH_VEG.search(name)))


def admit(item: dict) -> dict:
    """返回 {'ok': bool, 'reason': str, 'text': str}；不修改入参。

    判定顺序：先核验类别码合法性与相互冲突的证据，再决定是否准入。
    """
    codes, invalid_code = _typecodes(item)
    types = _segments(item.get('type') or '')
    blob = ' '.join(types)

    if invalid_code:
        # 类别码畸形时无法确认；即使 type 里有“素食”字样也不放行
        reason = 'invalid_category'
        return {'ok': False, 'reason': reason, 'text': REASON_TEXT[reason]}
    if not codes and not types:
        reason = 'missing_category'
        return {'ok': False, 'reason': reason, 'text': REASON_TEXT[reason]}

    veg_code = any(_is_veg_code(code) for code in codes)
    veg_type = any(marker in blob for marker in VEG_MARKERS)
    has_veg = veg_code or veg_type
    has_meat = _has_meat(blob)
    name = str(item.get('name') or '')
    identity = _has_identity(name)
    name_conflict = None
    if any(word in name for word in NAME_NONFOOD):
        name_conflict = 'conflict' if has_veg else 'non_food'
    elif _has_meat(name) or NEGATED_IDENTITY.search(name):
        name_conflict = 'conflict' if has_veg else 'meat'
    non_food_code = bool(codes) and not all(code.startswith(FOOD_TYPECODE_PREFIX) for code in codes)
    non_food_type = any(marker in blob for marker in NON_FOOD_MARKERS)

    # 冲突优先：素食线索不能掩盖荤食或非餐饮证据
    if has_veg and (has_meat or non_food_code or non_food_type):
        reason = 'conflict'
        return {'ok': False, 'reason': reason, 'text': REASON_TEXT[reason]}
    if name_conflict:
        reason = name_conflict
        return {'ok': False, 'reason': reason, 'text': REASON_TEXT[reason] + '（店名与类别冲突）'}
    if has_meat:
        reason = 'meat'
        return {'ok': False, 'reason': reason, 'text': REASON_TEXT[reason]}
    if non_food_code or non_food_type:
        reason = 'non_food'
        return {'ok': False, 'reason': reason, 'text': REASON_TEXT[reason]}
    food_category = bool(codes) and all(code.startswith(FOOD_TYPECODE_PREFIX) for code in codes)
    food_category = food_category or any(marker in blob for marker in ('餐饮服务', '餐厅', '素菜馆'))
    if not food_category:
        reason = 'missing_food_category'
    elif not identity:
        reason = 'missing_identity' if has_veg else 'no_veg_evidence'
    else:
        reason = 'veg_category' if veg_code else 'veg_type'
        return {'ok': True, 'reason': reason, 'text': REASON_TEXT[reason]}
    return {'ok': False, 'reason': reason, 'text': REASON_TEXT[reason]}


def scope_meta(admitted: int = 0, filtered_out: int = 0, reasons: dict | None = None) -> dict:
    """随查询结果一起返回的准入元信息；`version` 是判断历史结果是否适用最新规则的依据。"""
    return {'rule': 'veg_category_evidence', 'version': RULE_VERSION,
            'identity_basis': 'explicit_vegetarian_name', 'category_basis': 'own_food_category',
            'text': '仅收录店名明确标注素食身份且平台自身类别为餐饮的店铺；平台素食类别不能单独作为依据。未逐店核验菜单，'
                    '不能保证所有菜品符合 Vegan 或纯净素，也非平台认证。',
            'admitted': admitted, 'filtered_out': filtered_out,
            'reject_reasons': reasons or {}}


def partition(items: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """把一页 POI 分成「准入」「排除」两组，并给出排除原因统计。"""
    admitted: list[dict] = []
    rejected: list[dict] = []
    reasons: dict[str, int] = {}
    for item in items:
        verdict = admit(item)
        verdict_meta = {'version': RULE_VERSION, 'admitted': verdict['ok'],
                        'reason': verdict['reason'], 'text': verdict['text']}
        if verdict['ok']:
            admitted.append({**item, 'scope': verdict_meta})
        else:
            rejected.append({**item, 'scope': verdict_meta})
            reasons[verdict['reason']] = reasons.get(verdict['reason'], 0) + 1
    return admitted, rejected, reasons

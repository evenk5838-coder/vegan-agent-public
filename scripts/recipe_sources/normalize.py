"""Conservative bilingual ingredient normalization for the source-review corpus.

Only explicit aliases become Chinese rule terms. Unknown remainder remains a
separate unknown item; source diet categories never decide compatibility.
This module doesn't extend or change the application's dietary policy.
"""
from __future__ import annotations

import re

# Exact food-name aliases. Longer phrases run first, with word boundaries.
ALIASES = {
    'peanut butter': '花生酱', 'cocoa butter': '可可脂',
    'butter beans': '利马豆', 'apple butter': '苹果酱', 'coconut butter': '椰子酱',
    'extra virgin olive oil': '特级初榨橄榄油', 'olive oil': '橄榄油',
    'sunflower oil': '葵花籽油', 'vegetable oil': '植物油', 'canola oil': '菜籽油',
    'rapeseed oil': '菜籽油', 'sesame oil': '芝麻油', 'peanut oil': '花生油',
    'whole wheat flour': '全麦面粉', 'wholemeal flour': '全麦面粉',
    'all purpose flour': '面粉', 'all-purpose flour': '面粉', 'plain flour': '面粉',
    'self raising flour': '自发面粉', 'self-raising flour': '自发面粉',
    'chickpea flour': '鹰嘴豆粉', 'corn flour': '玉米粉', 'cornmeal': '玉米粉',
    'cornstarch': '玉米淀粉', 'corn starch': '玉米淀粉', 'potato starch': '土豆淀粉',
    'tapioca starch': '木薯淀粉', 'flour': '面粉', 'oatmeal': '燕麦', 'oats': '燕麦',
    'rolled oats': '燕麦片', 'brown rice': '糙米', 'rice': '大米', 'quinoa': '藜麦',
    'buckwheat': '荞麦', 'wheat': '小麦', 'millet': '小米',
    'icing sugar': '糖粉', 'powdered sugar': '糖粉', 'caster sugar': '细砂糖',
    'castor sugar': '细砂糖', 'brown sugar': '红糖', 'coconut sugar': '椰子糖',
    'sugar': '白糖', 'maple syrup': '枫糖浆', 'honey': '蜂蜜',
    'sea salt': '海盐', 'salt': '盐', 'baking powder': '泡打粉', 'baking soda': '小苏打',
    'bicarbonate of soda': '小苏打', 'yeast': '酵母', 'water': '水',
    'nutritional yeast': '营养酵母', 'cocoa powder': '可可粉',
    'coconut milk': '椰奶', 'coconut cream': '椰浆', 'soy milk': '植物奶',
    'soya milk': '植物奶', 'almond milk': '植物奶', 'oat milk': '植物奶',
    'vegetable stock': '蔬菜高汤', 'vegetable broth': '蔬菜高汤',
    'soy sauce': '酱油', 'soya sauce': '酱油', 'vanilla extract': '香草提取物',
    'vanilla essence': '香草精', 'dark chocolate': '黑巧克力',
    'cream of tartar': '塔塔粉', 'cream cheese': '奶油奶酪', 'sour cream': '酸奶油',
    'parmesan cheese': '奶酪', 'cheddar cheese': '奶酪', 'cheese': '奶酪',
    'yoghurt': '酸奶', 'yogurt': '酸奶', 'condensed milk': '炼乳',
    'milk powder': '奶粉', 'whole milk': '全脂牛奶', 'skimmed milk': '脱脂牛奶',
    'milk': '牛奶', 'butter': '黄油', 'cream': '奶油',
    'egg yolks': '蛋黄', 'egg yolk': '蛋黄', 'egg whites': '蛋清',
    'egg white': '蛋清', 'eggs': '鸡蛋', 'egg': '鸡蛋',
    'chicken': '鸡肉', 'beef': '牛肉', 'pork': '猪肉', 'lamb': '羊肉',
    'duck': '鸭肉', 'fish sauce': '鱼露', 'oyster sauce': '蚝油',
    'shrimp': '虾', 'prawns': '虾', 'prawn': '虾', 'fish': '鱼',
    'gelatine': '明胶', 'gelatin': '明胶', 'lard': '猪油',
    'garlic powder': '蒜粉', 'garlic': '蒜', 'onion powder': '洋葱粉',
    'spring onions': '葱', 'spring onion': '葱', 'green onions': '葱',
    'green onion': '葱', 'scallions': '葱', 'scallion': '葱',
    'red onions': '红洋葱', 'red onion': '红洋葱', 'onions': '洋葱', 'onion': '洋葱',
    'chives': '细香葱', 'cilantro': '香菜',
    'sweet potatoes': '红薯', 'sweet potato': '红薯', 'potatoes': '土豆', 'potato': '土豆',
    'tomatoes': '番茄', 'tomato': '番茄', 'tomato paste': '番茄膏', 'tomato puree': '番茄膏',
    'carrots': '胡萝卜', 'carrot': '胡萝卜', 'cucumber': '黄瓜', 'cucumbers': '黄瓜',
    'eggplant': '茄子', 'aubergine': '茄子', 'courgette': '西葫芦', 'zucchini': '西葫芦',
    'cabbage': '卷心菜', 'spinach': '菠菜', 'broccoli': '西兰花',
    'cauliflower': '花椰菜', 'pumpkin': '南瓜', 'butternut squash': '奶油南瓜',
    'mushrooms': '蘑菇', 'mushroom': '蘑菇', 'tofu': '豆腐',
    'chickpeas': '鹰嘴豆', 'chickpea': '鹰嘴豆', 'green peas': '豌豆', 'peas': '豌豆',
    'soybeans': '大豆', 'soya beans': '大豆', 'black beans': '黑豆',
    'peanuts': '花生', 'peanut': '花生', 'walnuts': '核桃', 'walnut': '核桃',
    'almonds': '杏仁', 'almond': '杏仁', 'cashews': '腰果',
    'sesame seeds': '芝麻', 'sesame': '芝麻', 'chia seeds': '奇亚籽',
    'pecans': '碧根果', 'pumpkin seeds': '南瓜子', 'sunflower seeds': '葵花籽',
    'bananas': '香蕉', 'banana': '香蕉', 'apples': '苹果', 'apple': '苹果',
    'lemon juice': '柠檬汁', 'lemon zest': '柠檬皮', 'lemons': '柠檬', 'lemon': '柠檬',
    'strawberries': '草莓', 'blueberries': '蓝莓', 'raspberries': '覆盆子',
    'avocados': '牛油果', 'avocado': '牛油果',
    'black pepper': '黑胡椒', 'white pepper': '白胡椒', 'pepper': '胡椒',
    'ginger': '姜', 'cinnamon': '肉桂粉', 'turmeric': '姜黄粉',
    'chilli powder': '辣椒粉', 'chili powder': '辣椒粉', 'cayenne pepper': '辣椒粉',
    'cumin': '孜然', 'paprika': '辣椒粉', 'parsley': '欧芹', 'rosemary': '迷迭香',
    'basil': '罗勒', 'sage': '鼠尾草', 'bay leaves': '香叶', 'bay leaf': '香叶',
    'rice vinegar': '米醋', 'balsamic vinegar': '意大利香醋', 'corn': '玉米',
}

_PATTERNS = [(re.compile(r'(?<![a-z])' + re.escape(key) + r'(?![a-z])', re.I), value)
             for key, value in sorted(ALIASES.items(), key=lambda item: -len(item[0]))]
_MEASURE = re.compile(r'\b(?:teaspoons?|tablespoons?|dessertspoons?|tsp|tbsp|cups?|pints?|quarts?|litres?|liters?|ounces?|oz|pounds?|lbs?|lb|g|kg|ml|cl|dl|fl|grams?|kilograms?|milliliters?|millilitres?|inches?|inch|cm|cloves?|pinch|pinches|handfuls?|pieces?|slices?|sprigs?)\b', re.I)
_STOP = re.compile(r'\b(?:a|an|the|of|and|or|to|for|with|as|at|from|in|on|by|about|approximately|approx|heaped|heaping|level|large|small|medium|fresh|freshly|dried|dry|ground|finely|roughly|chopped|diced|sliced|peeled|crushed|minced|grated|shredded|sifted|melted|softened|boiling|boiled|cooked|uncooked|hot|warm|cold|washed|rinsed|drained|beaten|divided|taste|optional|required|needed|use|using|see|note|notes|preferably|plus|extra|additional|more|less|some|few|little|one|two|three|four|five|six|eight|half|quarter|each|very|well|only|into|size|ripe|seeds|seed|skin|skins|tops|removed|stalks|stem|stems|unsalted|unbleached|organic|coarsely|lightly|generous|good|quality)\b', re.I)

CHINESE_ALIASES = {
    **{key: '牛肉' for key in ('肥牛', '肥牛片', '肥牛卷', '牛腩', '牛里脊', '牛腱子', '牛肉馅', '牛肉末')},
    **{key: '猪肉' for key in ('五花肉', '猪五花肉', '猪里脊', '瘦猪肉', '猪肉馅', '猪肉末', '猪蹄', '猪排骨')},
    **{key: '鸡肉' for key in ('鸡腿', '鸡胸肉', '鸡胸', '鸡翅', '鸡翅中', '鸡腿肉', '鸡翅根')},
    **{key: '鸭肉' for key in ('鸭腿', '鸭胸', '鸭胸肉', '鸭翅')},
    **{key: '鱼' for key in ('鲤鱼', '鲫鱼', '鲈鱼', '鳜鱼', '三文鱼', '鳕鱼', '比目鱼', '秋刀鱼', '带鱼')},
}
_ANIMAL_ENGLISH = re.compile(r'\b(?:meat|beef|pork|lamb|mutton|chicken|turkey|duck|fish|shrimps?|prawns?|crabs?|lobsters?|anchov(?:y|ies)|bacon|ham|sausage|pancetta|prosciutto|lard|suet|gelatin|gelatine|isinglass|mussels?|clams?|oysters?|venison|tripe|offal)\b', re.I)
_ANIMAL_CHINESE = re.compile(r'猪肉|牛肉|羊肉|鸡肉|鸭肉|肥牛|牛腩|五花肉|瘦肉|肉末|肉馅|肉丝|肉片|肉块|肉汤|骨汤|排骨|鸡腿|鸡胸|鸡翅|猪肝|牛肝|猪肚|牛肚|毛肚|鸭脖|羊排|牛排|猪排|鱼肉|鱼露|虾|蟹|蚝油|猪油|明胶|培根|火腿|香肠|海鲜|海米|干贝|蛤蜊|鱿鱼|章鱼|墨鱼|鲍鱼')
_CLAIM = re.compile(r'\b(?:vegan|vegetarian|meatless|imitation|substitute|plant[- ]based|dairy[- ]free)\b|素蚝油|植物肉|素肉|素鸡|素鸭|素鲍鱼|素鱼|仿肉', re.I)


def explicit_animal_evidence(lines: list[str]) -> list[str]:
    """Quarantine source-review records when policy vocabulary missed meat.

    This is an exclusion-only source triage, not a replacement dietary engine.
    Claimed substitutes remain unknown and must have ingredients checked.
    """
    found = []
    for line in lines:
        if _CLAIM.search(line):
            continue
        if _ANIMAL_ENGLISH.search(line) or _ANIMAL_CHINESE.search(line):
            found.append(line)
    return found


def normalize_english(line: str) -> dict:
    text = str(line).replace('’', "'").casefold()
    # Product claims don't establish its ingredients. Keep these formulations
    # unresolved rather than interpreting 'vegan chicken' as a real chicken.
    if re.search(r'\b(?:vegan|vegetarian|meatless|imitation|substitute|plant[- ]based|dairy[- ]free)\b', text):
        return {'terms': [line.strip()], 'unknown_remainder': line.strip(), 'method': 'product_claim_requires_ingredients'}
    found = []
    rest = text
    for pattern, canonical in _PATTERNS:
        if pattern.search(rest):
            found.append(canonical)
            rest = pattern.sub(' ', rest)
    rest = _MEASURE.sub(' ', rest)
    rest = _STOP.sub(' ', rest)
    rest = re.sub(r'[\d\W_¼½¾⅓⅔⅛⅜⅝⅞]+', ' ', rest, flags=re.UNICODE)
    rest = re.sub(r'\s+', ' ', rest).strip()
    terms = list(dict.fromkeys(found))
    if rest or not terms:
        terms.append('待核实原料：' + (rest or line.strip()))
    return {'terms': terms, 'unknown_remainder': rest, 'method': 'explicit_bilingual_aliases_v1'}

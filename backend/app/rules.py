"""版本化初始规则。未覆盖项保留未知，不能用子串或模型猜测放行。"""
import re
import unicodedata

VERSION = 'ingredients-2026-10-27.3'
PLANTS = set('水 饮用水 糖 白糖 细砂糖 糖粉 冰糖 红糖 椰子糖 枫糖浆 纯枫糖浆 赤小豆 红豆 小米 赤小豆 香叶 孜然 肉桂粉 姜黄粉 烟熏辣椒粉 烟熏甜椒粉 辣椒粉 白胡椒 黑胡椒 干辣椒 欧芹 迷迭香 鼠尾草 罗勒 可可粉 泡打粉 小苏打 味精 米醋 香醋 意大利香醋 辣椒油 绍兴酒 柠檬汁 柠檬皮 番茄酱 番茄膏 营养酵母 鹰嘴豆粉 去皮杏仁粉 天然花生酱 奇亚籽 碧根果 西葫芦 茄子 花椰菜 奶油南瓜 波特贝勒蘑菇 混合蘑菇 大白菜 金针菇 上海青 卷心菜 红薯粉条 冻豆腐 油面筋 鲜腐竹 冷冻带荚毛豆 巴斯马蒂糙米 覆盆子 冷冻混合莓果 桃片罐头 椰奶 椰浆 土豆淀粉 香油 芝麻油 食用油 特级初榨橄榄油 葵花籽油 菜籽油 全麦面粉 自发面粉 无乳植物黄油 大豆人造黄油  小麦 小麦粉 小麦淀粉 玉米 玉米粉 玉米淀粉 大米 糙米 燕麦 燕麦片 荞麦 藜麦 面粉 植物油 大豆油 菜籽油 葵花籽油 橄榄油 芝麻油 花生油 白砂糖 白糖 赤砂糖 红糖 冰糖 葡萄糖 果糖 食盐 盐 海盐 酵母 食用酵母 大豆 黄豆 黑豆 红豆 绿豆 豌豆 鹰嘴豆 豆腐 豆干 豆皮 豆浆 无糖豆浆 豆奶 土豆 马铃薯 红薯 胡萝卜 番茄 西红柿 黄瓜 白菜 小白菜 青菜 菠菜 西兰花 南瓜 香菇 蘑菇 木耳 紫菜 海带 芝麻 花生 核桃 杏仁 腰果 南瓜子 葵花籽 香蕉 苹果 柠檬 草莓 蓝莓 牛油果 姜 生姜 胡椒 黑胡椒 辣椒 花椒 八角 桂皮 淀粉 木薯淀粉 藕粉'.split())
EGG_MILK = set('牛奶 全脂牛奶 脱脂牛奶 羊奶 奶粉 全脂奶粉 脱脂奶粉 乳粉 全脂乳粉 脱脂乳粉 乳清粉 乳清蛋白 奶油 黄油 炼乳 鸡蛋 鸭蛋 鹌鹑蛋 蛋黄 蛋黄粉 蛋清 蛋清粉 鸡蛋粉 全蛋液 蛋白液'.split())
ANIMAL = set('猪肉 牛肉 羊肉 鸡肉 鸭肉 鱼 鱼肉 鱼粉 虾 虾粉 虾米 蚝油 鱼露 猪油 牛油 羊油 鸡油 明胶 骨胶 肉粉 牛肉粉 鸡肉粉 虾酱 蚝汁 鱼胶'.split())
# 复合/加工产品：名称本身不能证明成分，需实际配料表 → 三类均待核实
COMPOUND = set('酱油 生抽 老抽 豆瓣酱 香草提取物 香草精 香草膏 \u65e0\u4e73\u5de7\u514b\u529b\u8c46 全麦面包 素蚝油 蔬菜高汤 高汤块 高汤粉 植物奶 无糖植物奶 意大利面 黑巧克力 罐头 鹰嘴豆罐头 黑豆罐头 绿扁豆罐头 桃片罐头 冷冻带荚毛豆 油面筋 鲜腐竹 酵母提取物或味噌 香草调味料或香草膏 Vegan 起酥皮 低脂涂抹脂 葵花籽油或菜籽油 细砂糖或椰糖 欧芹或香菜 自发面粉'.split())

# 明确含乳但成分不完整的加工品：纯净素/Vegan 不适配；蛋奶素仍需核对其他成分
DAIRY_COMPOUND = set('希腊酸奶 风味酸奶 酸奶 奶酪 芝士 酸奶油 奶油奶酪'.split())

PLANTS.update('芥末粉 平菇 杏鲍菇 鱼腥草 葡萄干 肉豆蔻 肉豆蔻皮 多香果 牛至 百里香 薄荷 莳萝 小豆蔻 丁香 芫荽籽 芫荽粉 咖喱叶 藏红花 葫芦巴 黑种草籽 粗粒小麦粉 大麦 珍珠大麦 斯佩耳特小麦 布格麦 竹芋淀粉 糯米 茉莉香米 长粒米 短粒米 白芸豆 芸豆 红腰豆 利马豆 扁豆 红扁豆 绿扁豆 干豌豆 黄豌豆 四季豆 豆芽 黑绿豆 去皮鹰嘴豆 松子 榛子 开心果 巴西坚果 罂粟籽 亚麻籽 芹菜 芜菁 欧防风 甜菜根 芦笋 秋葵 生菜 西洋菜 大黄 山药 甜椒 青椒 红甜椒 黄甜椒 青辣椒 红辣椒 墨西哥辣椒 干辣椒碎 橙子 橙汁 橙皮 青柠 青柠汁 青柠皮 菠萝 芒果 桃子 梨 杏 樱桃 黑莓 蔓越莓 醋栗 椰枣 无花果 西瓜 石榴 大蕉 椰子 椰蓉 椰肉 苹果醋 醋 白醋 白葡萄酒醋 红葡萄酒醋 麦芽醋 葡萄酒醋 椰子油 棕榈油 红花籽油 葡萄籽油 可可脂 冰块 糖蜜 咖啡 咖啡粉 绿茶 红茶 抹茶 塔塔粉 高筋面粉 低筋面粉 孜然粉 食用盐 清水 米 小米辣 红枣 红辣椒粉 胡椒粉'.split())
PLANTS.update({'燕麦麸', '桂圆'})
ANIMAL.update('蟹 培根 火腿 香肠 肉 肉汤 骨汤 牛肉汤 鸡汤 猪肉汤 火鸡肉 三文鱼 金枪鱼 凤尾鱼 牡蛎 贻贝 蛤蜊 龙虾 扇贝 鱿鱼 章鱼 牛羊板油 牛羊油 鹿肉 海鲜 海米 干贝 鲍鱼 鱼翅 蟹肉 蟹黄 鸡精 牛肉精 排骨 猪肝 猪肚 牛肚 羊排 牛排 猪排 鸡腿 鸡胸肉 鸡翅 鸭腿 鸭胸肉 五花肉 肥牛 牛腩 猪肉末 牛肉末 鸡肉末'.split())
EGG_MILK.update('酪蛋白 酥油 淡奶 淡奶油'.split())
DAIRY_COMPOUND.update('开菲尔 白脱乳 菲达奶酪 马苏里拉奶酪 马斯卡彭奶酪 帕尔马奶酪 佩科里诺奶酪 茅屋奶酪 乳清奶酪 切达奶酪'.split())
COMPOUND.update('蛋黄酱 天然花生酱 食用油 油 无乳植物黄油 大豆人造黄油 花生酱 苹果酱 椰子酱 人造黄油 植物黄油 植物起酥油 起酥油 高汤 面包 面包屑 通心粉 面条 粉丝 芥末酱 第戎芥末酱 咖喱粉 印度综合香料粉 巧克力 巧克力豆 白巧克力 白巧克力豆 番茄沙司 伍斯特沙司 果酱 棉花糖 香草调味料'.split())

# 完整短语优先匹配；未消费的文字保留为 unknown，不用某个植物词覆盖整行。
EN_ALIASES = {'peanut butter': '花生酱',
 'oyster mushroom': '平菇',
 'oyster mushrooms': '平菇',
 'king oyster mushroom': '杏鲍菇',
 'king oyster mushrooms': '杏鲍菇',
 'fish mint': '鱼腥草',
 'cocoa butter': '可可脂',
 'butter beans': '利马豆',
 'apple butter': '苹果酱',
 'coconut butter': '椰子酱',
 'extra virgin olive oil': '特级初榨橄榄油',
 'olive oil': '橄榄油',
 'sunflower oil': '葵花籽油',
 'vegetable oil': '植物油',
 'canola oil': '菜籽油',
 'rapeseed oil': '菜籽油',
 'sesame oil': '芝麻油',
 'peanut oil': '花生油',
 'whole wheat flour': '全麦面粉',
 'wholemeal flour': '全麦面粉',
 'all purpose flour': '面粉',
 'all-purpose flour': '面粉',
 'plain flour': '面粉',
 'self raising flour': '自发面粉',
 'self-raising flour': '自发面粉',
 'chickpea flour': '鹰嘴豆粉',
 'corn flour': '玉米粉',
 'cornmeal': '玉米粉',
 'cornstarch': '玉米淀粉',
 'corn starch': '玉米淀粉',
 'potato starch': '土豆淀粉',
 'tapioca starch': '木薯淀粉',
 'flour': '面粉',
 'oatmeal': '燕麦',
 'oat bran': '燕麦麸',
 'oats': '燕麦',
 'rolled oats': '燕麦片',
 'brown rice': '糙米',
 'rice': '大米',
 'quinoa': '藜麦',
 'buckwheat': '荞麦',
 'wheat': '小麦',
 'millet': '小米',
 'icing sugar': '糖粉',
 'powdered sugar': '糖粉',
 'caster sugar': '细砂糖',
 'castor sugar': '细砂糖',
 'brown sugar': '红糖',
 'coconut sugar': '椰子糖',
 'sugar': '白糖',
 'maple syrup': '枫糖浆',
 'honey': '蜂蜜',
 'sea salt': '海盐',
 'salt': '盐',
 'baking powder': '泡打粉',
 'baking soda': '小苏打',
 'bicarbonate of soda': '小苏打',
 'yeast': '酵母',
 'water': '水',
 'nutritional yeast': '营养酵母',
 'cocoa powder': '可可粉',
 'coconut milk': '椰奶',
 'coconut cream': '椰浆',
 'soy milk': '植物奶',
 'soya milk': '植物奶',
 'almond milk': '植物奶',
 'oat milk': '植物奶',
 'vegetable stock': '蔬菜高汤',
 'vegetable broth': '蔬菜高汤',
 'soy sauce': '酱油',
 'soya sauce': '酱油',
 'vanilla extract': '香草提取物',
 'vanilla essence': '香草精',
 'dark chocolate': '黑巧克力',
 'cream of tartar': '塔塔粉',
 'cream cheese': '奶油奶酪',
 'sour cream': '酸奶油',
 'parmesan cheese': '奶酪',
 'cheddar cheese': '奶酪',
 'cheese': '奶酪',
 'yoghurt': '酸奶',
 'yogurt': '酸奶',
 'condensed milk': '炼乳',
 'milk powder': '奶粉',
 'whole milk': '全脂牛奶',
 'skimmed milk': '脱脂牛奶',
 'milk': '牛奶',
 'butter': '黄油',
 'cream': '奶油',
 'egg yolks': '蛋黄',
 'egg yolk': '蛋黄',
 'egg whites': '蛋清',
 'egg white': '蛋清',
 'eggs': '鸡蛋',
 'egg': '鸡蛋',
 'chicken': '鸡肉',
 'beef': '牛肉',
 'pork': '猪肉',
 'lamb': '羊肉',
 'duck': '鸭肉',
 'fish sauce': '鱼露',
 'oyster sauce': '蚝油',
 'shrimp': '虾',
 'prawns': '虾',
 'prawn': '虾',
 'fish': '鱼',
 'gelatine': '明胶',
 'gelatin': '明胶',
 'lard': '猪油',
 'garlic powder': '蒜粉',
 'garlic': '蒜',
 'onion powder': '洋葱粉',
 'spring onions': '葱',
 'spring onion': '葱',
 'green onions': '葱',
 'green onion': '葱',
 'scallions': '葱',
 'scallion': '葱',
 'red onions': '红洋葱',
 'red onion': '红洋葱',
 'onions': '洋葱',
 'onion': '洋葱',
 'chives': '细香葱',
 'cilantro': '香菜',
 'sweet potatoes': '红薯',
 'sweet potato': '红薯',
 'potatoes': '土豆',
 'potato': '土豆',
 'tomatoes': '番茄',
 'tomato': '番茄',
 'tomato paste': '番茄膏',
 'tomato puree': '番茄膏',
 'carrots': '胡萝卜',
 'carrot': '胡萝卜',
 'cucumber': '黄瓜',
 'cucumbers': '黄瓜',
 'eggplant': '茄子',
 'aubergine': '茄子',
 'courgette': '西葫芦',
 'zucchini': '西葫芦',
 'cabbage': '卷心菜',
 'spinach': '菠菜',
 'broccoli': '西兰花',
 'cauliflower': '花椰菜',
 'pumpkin': '南瓜',
 'butternut squash': '奶油南瓜',
 'mushrooms': '蘑菇',
 'mushroom': '蘑菇',
 'tofu': '豆腐',
 'chickpeas': '鹰嘴豆',
 'chickpea': '鹰嘴豆',
 'green peas': '豌豆',
 'peas': '豌豆',
 'soybeans': '大豆',
 'soya beans': '大豆',
 'black beans': '黑豆',
 'peanuts': '花生',
 'peanut': '花生',
 'walnuts': '核桃',
 'walnut': '核桃',
 'almonds': '杏仁',
 'almond': '杏仁',
 'cashews': '腰果',
 'sesame seeds': '芝麻',
 'sesame': '芝麻',
 'chia seeds': '奇亚籽',
 'pecans': '碧根果',
 'pumpkin seeds': '南瓜子',
 'sunflower seeds': '葵花籽',
 'bananas': '香蕉',
 'banana': '香蕉',
 'apples': '苹果',
 'apple': '苹果',
 'lemon juice': '柠檬汁',
 'lemon zest': '柠檬皮',
 'lemons': '柠檬',
 'lemon': '柠檬',
 'strawberries': '草莓',
 'blueberries': '蓝莓',
 'raspberries': '覆盆子',
 'avocados': '牛油果',
 'avocado': '牛油果',
 'black pepper': '黑胡椒',
 'white pepper': '白胡椒',
 'pepper': '胡椒',
 'ginger': '姜',
 'cinnamon': '肉桂粉',
 'turmeric': '姜黄粉',
 'chilli powder': '辣椒粉',
 'chili powder': '辣椒粉',
 'cayenne pepper': '辣椒粉',
 'cumin': '孜然',
 'paprika': '辣椒粉',
 'parsley': '欧芹',
 'rosemary': '迷迭香',
 'basil': '罗勒',
 'sage': '鼠尾草',
 'bay leaves': '香叶',
 'bay leaf': '香叶',
 'rice vinegar': '米醋',
 'balsamic vinegar': '意大利香醋',
 'corn': '玉米',
 'crab': '蟹',
 'crabs': '蟹',
 'bacon': '培根',
 'ham': '火腿',
 'sausage': '香肠',
 'sausages': '香肠',
 'meat': '肉',
 'meat broth': '肉汤',
 'meat stock': '肉汤',
 'bone broth': '骨汤',
 'beef stock': '牛肉汤',
 'beef broth': '牛肉汤',
 'chicken stock': '鸡汤',
 'chicken broth': '鸡汤',
 'pork stock': '猪肉汤',
 'turkey': '火鸡肉',
 'mutton': '羊肉',
 'salmon': '三文鱼',
 'smoked salmon': '三文鱼',
 'tuna': '金枪鱼',
 'anchovy': '凤尾鱼',
 'anchovies': '凤尾鱼',
 'oyster': '牡蛎',
 'oysters': '牡蛎',
 'mussel': '贻贝',
 'mussels': '贻贝',
 'clam': '蛤蜊',
 'clams': '蛤蜊',
 'lobster': '龙虾',
 'lobsters': '龙虾',
 'scallops': '扇贝',
 'squid': '鱿鱼',
 'octopus': '章鱼',
 'suet': '牛羊板油',
 'tallow': '牛羊油',
 'isinglass': '鱼胶',
 'pancetta': '培根',
 'prosciutto': '火腿',
 'venison': '鹿肉',
 'whey': '乳清粉',
 'whey protein': '乳清蛋白',
 'casein': '酪蛋白',
 'ghee': '酥油',
 'clarified butter': '酥油',
 'kefir': '开菲尔',
 'buttermilk': '白脱乳',
 'evaporated milk': '淡奶',
 'sweetened condensed milk': '炼乳',
 'heavy cream': '淡奶油',
 'double cream': '淡奶油',
 'light cream': '淡奶油',
 'whipping cream': '淡奶油',
 'yolk': '蛋黄',
 'yolks': '蛋黄',
 'feta': '菲达奶酪',
 'mozzarella': '马苏里拉奶酪',
 'mascarpone': '马斯卡彭奶酪',
 'parmesan': '帕尔马奶酪',
 'parmigiano reggiano': '帕尔马奶酪',
 'pecorino romano': '佩科里诺奶酪',
 'cottage cheese': '茅屋奶酪',
 'ricotta': '乳清奶酪',
 'cheddar': '切达奶酪',
 'crème fraîche': '酸奶油',
 'white granulated sugar': '白砂糖',
 'granulated white sugar': '白砂糖',
 'granulated sugar': '白砂糖',
 'white sugar': '白糖',
 'superfine sugar': '细砂糖',
 'confectioners sugar': '糖粉',
 "confectioner's sugar": '糖粉',
 'white flour': '面粉',
 'bread flour': '高筋面粉',
 'cake flour': '低筋面粉',
 'wheat flour': '小麦粉',
 'semolina': '粗粒小麦粉',
 'fine semolina': '粗粒小麦粉',
 'barley': '大麦',
 'pearl barley': '珍珠大麦',
 'spelt': '斯佩耳特小麦',
 'bulgur': '布格麦',
 'maida': '面粉',
 'arrowroot starch': '竹芋淀粉',
 'glutinous rice': '糯米',
 'jasmine rice': '茉莉香米',
 'long grain rice': '长粒米',
 'short grain rice': '短粒米',
 'haricot beans': '白芸豆',
 'white beans': '白芸豆',
 'kidney beans': '芸豆',
 'red kidney beans': '红腰豆',
 'lima beans': '利马豆',
 'lentils': '扁豆',
 'red lentils': '红扁豆',
 'green lentils': '绿扁豆',
 'split peas': '干豌豆',
 'yellow split peas': '黄豌豆',
 'green beans': '四季豆',
 'bean sprouts': '豆芽',
 'mung beans': '绿豆',
 'urad dal': '黑绿豆',
 'chana dal': '去皮鹰嘴豆',
 'cashew nuts': '腰果',
 'pine nuts': '松子',
 'hazelnuts': '榛子',
 'pistachios': '开心果',
 'brazil nuts': '巴西坚果',
 'groundnuts': '花生',
 'pecan': '碧根果',
 'poppy seeds': '罂粟籽',
 'flax seeds': '亚麻籽',
 'linseed': '亚麻籽',
 'celery': '芹菜',
 'turnip': '芜菁',
 'parsnip': '欧防风',
 'parsnips': '欧防风',
 'beetroot': '甜菜根',
 'beets': '甜菜根',
 'asparagus': '芦笋',
 'okra': '秋葵',
 'lettuce': '生菜',
 'watercress': '西洋菜',
 'rhubarb': '大黄',
 'eggplants': '茄子',
 'aubergines': '茄子',
 'courgettes': '西葫芦',
 'zucchinis': '西葫芦',
 'yam': '山药',
 'bell pepper': '甜椒',
 'bell peppers': '甜椒',
 'green bell pepper': '青椒',
 'green bell peppers': '青椒',
 'red bell pepper': '红甜椒',
 'red bell peppers': '红甜椒',
 'yellow bell pepper': '黄甜椒',
 'chile': '辣椒',
 'chiles': '辣椒',
 'chili': '辣椒',
 'chillies': '辣椒',
 'green chiles': '青辣椒',
 'red chiles': '红辣椒',
 'green chillies': '青辣椒',
 'chile peppers': '辣椒',
 'jalapeño': '墨西哥辣椒',
 'jalapeños': '墨西哥辣椒',
 'red pepper flakes': '干辣椒碎',
 'chili flakes': '干辣椒碎',
 'cayenne': '辣椒粉',
 'chilli': '辣椒',
 'orange': '橙子',
 'oranges': '橙子',
 'orange juice': '橙汁',
 'orange zest': '橙皮',
 'orange peel': '橙皮',
 'lime': '青柠',
 'limes': '青柠',
 'lime juice': '青柠汁',
 'lime zest': '青柠皮',
 'pineapple': '菠萝',
 'mango': '芒果',
 'peach': '桃子',
 'peaches': '桃子',
 'pear': '梨',
 'pears': '梨',
 'apricot': '杏',
 'apricots': '杏',
 'cherries': '樱桃',
 'blackberries': '黑莓',
 'cranberries': '蔓越莓',
 'currants': '醋栗',
 'raisins': '葡萄干',
 'sultanas': '葡萄干',
 'dates': '椰枣',
 'pitted dates': '椰枣',
 'figs': '无花果',
 'watermelon': '西瓜',
 'pomegranate': '石榴',
 'plantain': '大蕉',
 'plantains': '大蕉',
 'coconut': '椰子',
 'desiccated coconut': '椰蓉',
 'coconut flesh': '椰肉',
 'nutmeg': '肉豆蔻',
 'mace': '肉豆蔻皮',
 'allspice': '多香果',
 'oregano': '牛至',
 'thyme': '百里香',
 'mint': '薄荷',
 'dill': '莳萝',
 'dill weed': '莳萝',
 'cardamom': '小豆蔻',
 'cardamom pods': '小豆蔻',
 'cloves': '丁香',
 'clove': '丁香',
 'coriander': '香菜',
 'coriander leaves': '香菜',
 'coriander seeds': '芫荽籽',
 'ground coriander': '芫荽粉',
 'curry leaves': '咖喱叶',
 'saffron': '藏红花',
 'fenugreek': '葫芦巴',
 'nigella': '黑种草籽',
 'star anise': '八角',
 'peppercorns': '胡椒',
 'black peppercorns': '黑胡椒',
 'white peppercorns': '白胡椒',
 'cumin seeds': '孜然',
 'leek': '韭葱',
 'leeks': '韭葱',
 'chive': '细香葱',
 'shallot': '红葱头',
 'shallots': '红葱头',
 'cider vinegar': '苹果醋',
 'apple cider vinegar': '苹果醋',
 'vinegar': '醋',
 'white vinegar': '白醋',
 'distilled white vinegar': '白醋',
 'white wine vinegar': '白葡萄酒醋',
 'red wine vinegar': '红葡萄酒醋',
 'malt vinegar': '麦芽醋',
 'wine vinegar': '葡萄酒醋',
 'coconut oil': '椰子油',
 'palm oil': '棕榈油',
 'safflower oil': '红花籽油',
 'grapeseed oil': '葡萄籽油',
 'kosher salt': '盐',
 'table salt': '盐',
 'ice': '冰块',
 'molasses': '糖蜜',
 'black treacle': '糖蜜',
 'coffee': '咖啡',
 'coffee powder': '咖啡粉',
 'instant coffee powder': '咖啡粉',
 'brewed coffee': '咖啡',
 'green tea': '绿茶',
 'black tea': '红茶',
 'matcha': '抹茶',
 'margarine': '人造黄油',
 'vegan butter': '植物黄油',
 'dairy free butter': '植物黄油',
 'dairy-free butter': '植物黄油',
 'vegetable shortening': '植物起酥油',
 'shortening': '起酥油',
 'stock': '高汤',
 'broth': '高汤',
 'stock cube': '高汤块',
 'stock cubes': '高汤块',
 'bouillon': '高汤粉',
 'bread': '面包',
 'bread crumbs': '面包屑',
 'breadcrumbs': '面包屑',
 'white breadcrumbs': '面包屑',
 'panko breadcrumbs': '面包屑',
 'pasta': '意大利面',
 'spaghetti': '意大利面',
 'macaroni': '通心粉',
 'noodles': '面条',
 'vermicelli': '粉丝',
 'mayonnaise': '蛋黄酱',
 'dijon mustard': '第戎芥末酱',
 'mustard': '芥末酱',
 'mustard powder': '芥末粉',
 'curry powder': '咖喱粉',
 'garam masala': '印度综合香料粉',
 'chocolate': '巧克力',
 'chocolate chips': '巧克力豆',
 'semisweet chocolate chips': '巧克力豆',
 'white chocolate': '白巧克力',
 'white chocolate chips': '白巧克力豆',
 'ketchup': '番茄沙司',
 'worcestershire sauce': '伍斯特沙司',
 'worcester sauce': '伍斯特沙司',
 'tamari': '酱油',
 'jam': '果酱',
 'marshmallows': '棉花糖',
 'vanilla': '香草调味料',
 'vanilla powder': '香草调味料'}
PUNGENT = {x: name for name, aliases in {
    '葱': ['葱', '大葱', '小葱', '香葱', '葱粉', '细香葱', '葱油', '小葱末'],
    '蒜': ['蒜', '大蒜', '蒜粉', '蒜蓉', '蒜苗', '蒜泥', '蒜末', '蒜头', '蒜瓣'],
    '韭': ['韭', '韭菜', '韭黄', '韭葱'],
    '薤': ['薤', '薤白', '藠头'],
    '洋葱': ['洋葱', '洋葱粉', '红洋葱', '紫洋葱', '白洋葱', '红葱头'],
}.items() for x in aliases}


def split_ingredients(text: str) -> list[str]:
    text = re.sub(r'^\s*配料(?:表)?\s*[：:]?', '', text.strip())
    # 复合名称保留成一项，子配料另外拆开；复合名称本身不因此默认通过。
    return [t.strip() for t in re.split(r'[、,，;；\n\r()（）\[\]【】]+', text) if t.strip()]


# Only preparation/measurement words may be discarded. In particular generic
# oil/fat, flavouring, product claims and unknown food words stay unresolved.
_EN_PATTERNS = [(re.compile(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", re.I), value)
                for key, value in sorted(EN_ALIASES.items(), key=lambda pair: -len(pair[0]))]
_MEASURES = re.compile(r"\b(?:teaspoons?|tablespoons?|dessertspoons?|tsp|tbsp|cups?|pints?|quarts?|litres?|liters?|ounces?|oz|pounds?|lbs?|lb|g|kg|ml|cl|dl|fl|grams?|kilograms?|milliliters?|millilitres?|inches?|inch|cm|pinch|pinches|handfuls?|pieces?|slices?|sprigs?)\b", re.I)
_RECIPE_EXTRA_MEASURES = re.compile(r'\bl\b', re.I)
_RECIPE_EXTRA_PREPARATION = re.compile(r'\b(?:thinly|julienned|juiced|cored|seeded|veined|slivered|toasted|coarse|tender)\b', re.I)
_PREPARATION = re.compile(r"\b(?:a|an|the|of|to|for|as|at|from|in|on|by|about|approximately|approx|heaped|heaping|level|large|small|medium|fresh|freshly|dried|dry|ground|finely|roughly|chopped|diced|sliced|peeled|crushed|minced|grated|shredded|sifted|melted|softened|boiling|boiled|cooked|uncooked|hot|warm|cold|washed|rinsed|drained|beaten|divided|taste|optional|required|needed|use|using|plus|extra|additional|more|less|some|few|little|one|two|three|four|five|six|eight|half|quarter|each|very|well|only|into|size|ripe|skin|skins|removed|unsalted|unbleached|organic|coarsely|lightly)\b", re.I)
_CLAIM = re.compile(r"\b(?:vegan|vegetarian|meatless|imitation|substitute|plant[- ]based|dairy[- ]free|non[- ]dairy|meat[- ]free|fish[- ]free|egg[- ]free)\b", re.I)
_CONNECTORS = re.compile(r"\b(?:and|or|with|contains?|containing|plus)\b|[和与及或]", re.I)
_ZH_ALIASES = {'花菜': '花椰菜', '北方大米': '大米', '蒸锅用水': '水',
               '孜然籽': '孜然', '香菜叶': '香菜', '香菜粉': '香菜',
               '肥牛': '牛肉', '肥牛片': '牛肉', '肥牛卷': '牛肉', '牛里脊': '牛肉',
               '牛腱子': '牛肉', '瘦猪肉': '猪肉', '猪五花肉': '猪肉', '猪里脊': '猪肉',
               '鸡胸': '鸡肉', '鸡腿肉': '鸡肉', '鸭胸': '鸭肉', '鲤鱼': '鱼', '鲫鱼': '鱼',
               '鲈鱼': '鱼', '鳜鱼': '鱼', '鳕鱼': '鱼', '比目鱼': '鱼', '秋刀鱼': '鱼', '带鱼': '鱼'}
_ZH_AMOUNT = r'(?:[0-9]+\s+[0-9]+[./⁄][0-9]+|[0-9]+(?:[./⁄][0-9]+)?|半|数)'
# Packaging (罐/包/盒) is deliberately not a removable measure: it can signal
# a composite product whose label must be checked.
_ZH_QTY = re.compile(rf'(?<![A-Za-z0-9])(?:约\s*)?{_ZH_AMOUNT}\s*(?:[-–~～至到]\s*{_ZH_AMOUNT})?\s*'
    r'(?:千克|毫升|公斤|kg|ml|磅|盎司|夸脱|品脱|克|升|g|l|大匙|小匙|茶匙|汤匙|杯|瓣|根|个|只|片|颗|粒|把|束|块|条|张|撮)(?![a-z])', re.I)
# Source recipe annotations, not ingredient names. Only an entire normalized
# segment may match these finite lists: never discard arbitrary parentheses,
# phrases ending in “用”, or any unknown residue attached to a known food.
_ZH_PREPARATION_NOTES = frozenset({'清洗', '清洗用', '焯水', '焯水用', '另需浸泡水', '太干时',
    '切碎', '擦丝', '切丝', '切成薄片', '切成细丝', '切成小块', '切成细条', '取汁', '去蒂', '对半切开', '去籽并去筋'})
_ZH_QUANTITY_NOTES = frozenset({'用量原文未给', '原文未给用量'})
_ZH_DESCRIPTION_PREFIX = re.compile(r'^(?:新鲜(?:的)?|成熟(?:的)?|中等(?:大小)?(?:的)?|较大(?:的)?|较小(?:的)?|大|小|嫩|粗磨(?:的)?|切片(?:的)?|薄片(?:的)?)\s*')
_EXACT_FOOD_NAMES = (PLANTS | EGG_MILK | ANIMAL | DAIRY_COMPOUND | COMPOUND |
                     set(PUNGENT) | set(_ZH_ALIASES) | {'蜂蜜', '蜂蜜粉', '香菜', '芫荽'})


def _without_known_food_description(name: str) -> str:
    # 大黄/大米/小麦 are whole known food names, never size prefixes.
    if name in _EXACT_FOOD_NAMES:
        return name
    candidate = _ZH_DESCRIPTION_PREFIX.sub('', name, count=1).strip()
    return candidate if candidate in _EXACT_FOOD_NAMES else name


def _normalize_segment(token: str) -> list[str]:
    """Every consumed term is returned; unknown residue is never silently lost."""
    token = unicodedata.normalize('NFKC', token).strip(' 。：:.')
    # Chinese display quantities may use Latin g/ml; remove only explicit
    # quantity+unit pairs before deciding which language parser applies.
    if re.search(r'[\u4e00-\u9fff]', token):
        token = _ZH_QTY.sub(' ', token).strip()
        # A source alternative still names another ingredient. Strip only this
        # exact introducer and evaluate all its remaining text normally; never
        # discard the whole note (milk / pungents / unknown gravy may follow).
        if token.startswith('原文可用') and token[len('原文可用'):].strip():
            token = token[len('原文可用'):].strip()
        # Exact serving/count annotations contain no food claim. Keep all
        # other bracket text for evaluation, including real subingredients.
        token = re.sub(r'(?:每人份?|/人份?|每份|每个|共)\s*$', '', token).strip()
    if not token:
        return []
    if token in _ZH_PREPARATION_NOTES or token in _ZH_QUANTITY_NOTES:
        return []
    if token.startswith('待核实原料'):
        return [token]
    # Known substitutes need their real label. Never misread vegan chicken as
    # chicken, nor let a plant word turn the marketed product into a safe food.
    if _CLAIM.search(token) and token.casefold() not in EN_ALIASES:
        return [token]
    if not re.search(r'[a-zA-Z]', token):
        name = _ZH_QTY.sub(' ', token)
        name = re.sub(r"\s*(?:按口味|适量|少许|少量|可选|按需)\s*$", '', name).strip()
        if name in _ZH_PREPARATION_NOTES or name in _ZH_QUANTITY_NOTES:
            return []
        name = _without_known_food_description(name)
        return [_ZH_ALIASES.get(name, name)] if name else []
    rest = token.casefold().replace('’', "'")
    if re.search(r'\bgarlic\b', rest):
        # Here cloves measures garlic; a separate 'and cloves' segment remains
        # the spice 丁香. Never remove it globally as a generic unit.
        rest = re.sub(r'\bcloves?\b', ' ', rest)
    # Canonical names are saved outside rest so alias substitutions cannot match
    # each other, and long protected foods (eggplant/peanut butter) win first.
    found = []
    for pattern, canonical in _EN_PATTERNS:
        if pattern.search(rest):
            found.append(canonical)
            rest = pattern.sub(' ', rest)
    rest = _MEASURES.sub(' ', rest)
    rest = _RECIPE_EXTRA_MEASURES.sub(' ', rest)
    rest = _PREPARATION.sub(' ', rest)
    rest = _RECIPE_EXTRA_PREPARATION.sub(' ', rest)
    rest = re.sub(r'[\d\W_]+', ' ', rest, flags=re.UNICODE)
    rest = re.sub(r'\s+', ' ', rest).strip()
    if rest:
        found.append(rest)
    return list(dict.fromkeys(found))


def normalize_ingredients(text: str) -> list[str]:
    """Shared rule input: parse all ingredients and retain unresolved residue.

    Conjunctions are alternatives, not permission to pick the safe option.
    A source claim or an incomplete compound ingredient remains uncertain.
    """
    names = []
    for token in split_ingredients(text):
        # Exact multiword aliases such as cream of tartar and butter beans stay
        # intact. Ingredient connectors split only after this exact check.
        parts = [token] if token.strip().casefold() in EN_ALIASES else _CONNECTORS.split(token)
        for part in parts:
            names.extend(_normalize_segment(part))
    return list(dict.fromkeys(name for name in names if name))


def normalize_english(token: str) -> str:
    """Compatibility helper: only replace a completely understood single food.

    Call normalize_ingredients/evaluate for lists; returning one safe word from
    a mixed or partly unknown line would discard evidence.
    """
    names = normalize_ingredients(token)
    return names[0] if len(names) == 1 else token


def evaluate(text: str, complete: bool, warnings: list[str], avoid_coriander=False):
    items = []
    for name in normalize_ingredients(text):
        if name in PLANTS and name in COMPOUND:
            # 同时出现在两表：保守按复合产品处理，不凭名称放行
            group = 'compound'
        else:
            group = ('plant' if name in PLANTS else 'egg_milk' if name in EGG_MILK
                 else 'dairy_compound' if name in DAIRY_COMPOUND
                 else 'compound' if name in COMPOUND
                 else 'animal' if name in ANIMAL else 'honey' if name in {'蜂蜜', '蜂蜜粉'}
                     else 'pungent' if name in PUNGENT
                     else 'coriander' if name in {'香菜', '芫荽'} else 'unknown')
        items.append({'name': name, 'group': group, 'pungent': PUNGENT.get(name),
                      'personal_avoidance': avoid_coriander and group == 'coriander',
                      'basis': '已定饮食规则/初始条目表' if group != 'unknown' else '规则暂未覆盖或来源不明确'})
    results = {}
    for diet in ['pure', 'vegan', 'lacto_ovo']:
        denied = [i['name'] for i in items if i['group'] == 'animal' or
                  (diet != 'lacto_ovo' and i['group'] in {'egg_milk', 'honey'}) or
                  (diet != 'lacto_ovo' and i['group'] == 'dairy_compound') or
                  (diet == 'pure' and i['group'] == 'pungent')]
        unknown = [i['name'] for i in items
                   if i['group'] in ('unknown', 'compound')
                   or (diet == 'lacto_ovo' and i['group'] == 'dairy_compound')]
        status = 'not_suitable' if denied else 'uncertain' if unknown or not complete or not items else 'suitable'
        results[diet] = {'status': status, 'denied': denied, 'unknown': unknown,
                         'incomplete': not complete or not items}
    pungents = [i['name'] for i in items if i['group'] == 'pungent']
    return {'rules_version': VERSION, 'rules_status': '初始规则待样本复核', 'items': items,
            'results': results, 'label_warnings': warnings,
            'pungent': 'detected' if pungents else 'uncertain' if not complete or any(i['group'] == 'unknown' for i in items) else 'not_detected',
            'pungent_items': pungents, 'personal_avoidances': [i['name'] for i in items if i['personal_avoidance']],
            'scope': '仅按本次可见配料及所示规则判断；非认证或过敏安全保证。'}

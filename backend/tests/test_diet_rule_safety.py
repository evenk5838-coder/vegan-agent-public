"""Pure rule safety regression; also runnable with pytest --noconftest.

The latter path imports only rules.py, so it never loads app settings, a local
credential file, the business database or an upstream service.
"""
from itertools import permutations
from pathlib import Path
import runpy

import pytest

RULES = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'app/rules.py'))
evaluate = RULES['evaluate']
normalize = RULES['normalize_ingredients']
DIETS = ('pure', 'vegan', 'lacto_ovo')


def statuses(text, complete=True):
    return tuple(evaluate(text, complete, [], False)['results'][diet]['status'] for diet in DIETS)


@pytest.mark.parametrize('animal', ['beef', 'pork', 'fish', 'crab', 'bacon', 'ham',
                                  'sausage', 'meat broth', 'chicken stock', 'gelatin'])
@pytest.mark.parametrize('connector', [' and ', ' with ', ' '])
def test_no_safe_plant_word_can_cover_animal_in_same_line(animal, connector):
    # The former word-first fallback could return only potato/sugar/flour.
    assert statuses(animal + connector + 'potato') == ('not_suitable',) * 3
    assert statuses('potato' + connector + animal) == ('not_suitable',) * 3


@pytest.mark.parametrize('text, denied', [
    ('milk and sugar', '牛奶'), ('egg and flour', '鸡蛋'),
    ('2 cups flour with 4 eggs', '鸡蛋'), ('honey or maple syrup', '蜂蜜'),
    ('1 tbsp whey protein and rice', '乳清蛋白'), ('milk chocolate', '牛奶'),
])
def test_egg_milk_and_honey_evidence_is_not_lost(text, denied):
    result = evaluate(text, True, [], False)
    for diet in ('pure', 'vegan'):
        assert result['results'][diet]['status'] == 'not_suitable'
        assert denied in result['results'][diet]['denied']


@pytest.mark.parametrize('text', ['salt and mystery flavouring', 'zorblat extract flour',
                                 'sugar mystery extract', 'potato unidentified oil',
                                 '待核实原料：flour', 'vegan chicken',
                                 'plant-based bacon', 'dairy-free sauce',
                                 'fish-free flavouring', '素蚝油', '植物肉'])
def test_unknown_residue_and_product_claims_do_not_become_suitable(text):
    result = evaluate(text, True, [], False)
    assert all(result['results'][diet]['status'] == 'uncertain' for diet in DIETS)
    assert result['results']['vegan']['unknown']


@pytest.mark.parametrize('text, expected', [
    ('peanut butter', 'compound'), ('apple butter', 'compound'),
    ('coconut butter', 'compound'), ('cocoa butter', 'plant'),
    ('butter beans', 'plant'), ('eggplant', 'plant'),
    ('coconut milk', 'plant'), ('coconut cream', 'plant'),
    ('cream of tartar', 'plant'), ('oyster mushrooms', 'plant'),
    ('king oyster mushroom', 'plant'), ('fish mint', 'plant'),
])
def test_full_food_names_win_before_animal_word_parts(text, expected):
    result = evaluate(text, True, [], False)
    assert [item['group'] for item in result['items']] == [expected]
    assert not result['results']['vegan']['denied']


@pytest.mark.parametrize('text', ['soy milk', 'oat milk', 'almond milk', 'vegan butter',
                                 'margarine', 'vegetable shortening', 'vegetable stock',
                                 'stock', 'curry powder', 'bread crumbs', 'pasta',
                                 '天然花生酱', '无乳植物黄油'])
def test_compounds_need_the_actual_ingredient_label(text):
    assert statuses(text) == ('uncertain',) * 3


@pytest.mark.parametrize('text', ['oil', 'cooking oil', 'frying oil', 'fat', '食用油', '油'])
def test_generic_fat_does_not_mean_vegetable_oil(text):
    assert statuses(text) == ('uncertain',) * 3


@pytest.mark.parametrize('text', ['olive oil', 'sunflower oil', 'coconut oil', 'palm oil',
                                 '2 cups plain flour', '½ teaspoon salt', 'white granulated sugar',
                                 'white granulated sugar, flour, water', '白糖 20g', '200g 土豆', '盐2g', '土豆200g', '土豆50-100g', '花菜', '西红柿2个（每个约100g，共200g）',
                                 '饮用水1000ml（蒸锅用水）', '北方大米100-200ml/人',
                                 'nutmeg, thyme, celery, raisins', '1 tsp ground cloves'])
def test_attested_basic_foods_and_quantities_can_be_recognized(text):
    assert statuses(text) == ('suitable',) * 3


@pytest.mark.parametrize('text', ['onion', 'garlic cloves', '2 cloves garlic', 'leeks',
                                 'chives', 'shallot', '蒜2瓣', '蒜瓣2粒', '大蒜 2-3 瓣', '大蒜约2～3瓣', '2至3瓣大蒜', '葱', '蒜', '韭', '薤', '洋葱'])
def test_five_pungent_families_use_the_frozen_policy(text):
    result = evaluate(text, True, [], False)
    assert statuses(text) == ('not_suitable', 'suitable', 'suitable')
    assert result['pungent'] == 'detected'
    if 'garlic' in text:
        assert [item['name'] for item in result['items']] == ['蒜']


def test_coriander_is_allowed_and_personal_avoidance_is_separate():
    result = evaluate('coriander', True, [], True)
    assert statuses('coriander') == ('suitable',) * 3
    assert result['pungent'] == 'not_detected'
    assert result['personal_avoidances'] == ['香菜']


@pytest.mark.parametrize('text', ['蟹', '培根', '火腿', '肉汤', '猪油', '明胶', '鸡精'])
def test_explicit_chinese_animal_ingredients_are_not_unknown(text):
    result = evaluate(text, True, [], False)
    assert result['items'][0]['group'] == 'animal'
    assert statuses(text) == ('not_suitable',) * 3


def test_unknown_and_animal_evidence_survive_permutation_and_parentheses():
    for parts in permutations(('potato', 'milk', 'mystery emulsifier')):
        result = evaluate(' and '.join(parts), True, [], False)
        assert result['results']['vegan']['status'] == 'not_suitable'
        assert '牛奶' in result['results']['vegan']['denied']
        assert 'mystery emulsifier' in result['results']['lacto_ovo']['unknown']
        assert result['results']['lacto_ovo']['status'] == 'uncertain'
    assert statuses('plant-based sauce (milk, flour)')[1] == 'not_suitable'
    assert statuses('植物鸡（牛奶、淀粉）')[1] == 'not_suitable'


def test_incomplete_empty_and_cross_contact_contracts_are_preserved():
    assert statuses('rice', complete=False) == ('uncertain',) * 3
    assert statuses('') == ('uncertain',) * 3
    warning = ['与含乳制品共用生产线']
    result = evaluate('rice, olive oil', True, warning, False)
    assert result['label_warnings'] == warning
    assert all(item['status'] == 'suitable' for item in result['results'].values())
    assert result['rules_version'] == 'ingredients-2026-10-27.3'


def test_shared_normalizer_and_legacy_helper_do_not_discard_a_second_food():
    assert set(normalize('milk and sugar')) == {'牛奶', '白糖'}
    assert RULES['normalize_english']('milk and sugar') == 'milk and sugar'
    assert RULES['normalize_english']('1 cup white granulated sugar') == '白砂糖'
    for name in RULES['PLANTS'] & RULES['COMPOUND']:
        assert evaluate(name, True, [], False)['items'][0]['group'] == 'compound'


@pytest.mark.parametrize('text', [
    '饮用水 1000ml（焯水）', '饮用水 500ml（焯水用）',
    '水（清洗和焯水用，原文未给用量）', '盐（用量原文未给）',
    '白糖（按口味，原文未给用量）', '水 600ml（另需浸泡水，用量原文未给）',
])
def test_exact_preparation_and_quantity_annotations_are_not_ingredient_unknowns(text):
    result = evaluate(text, True, [], False)
    assert statuses(text) == ('suitable',) * 3
    assert all(not value['unknown'] for value in result['results'].values())
    assert not set(normalize(text)) & (RULES['_ZH_PREPARATION_NOTES'] | RULES['_ZH_QUANTITY_NOTES'])


@pytest.mark.parametrize('text, denied', [
    ('豆腐（乳粉、焯水用）', '乳粉'),
    ('植物奶（原文未给用量，乳粉）', '乳粉'),
    ('面包（清洗和焯水用、牛奶）', '牛奶'),
])
def test_recipe_notes_never_remove_parenthesized_egg_milk_evidence(text, denied):
    result = evaluate(text, True, [], False)
    for diet in ('pure', 'vegan'):
        assert result['results'][diet]['status'] == 'not_suitable'
        assert denied in result['results'][diet]['denied']


@pytest.mark.parametrize('text', [
    '豆腐（蚝油、焯水用）', '蔬菜（焯水用，蚝油，原文未给用量）',
    '水（清洗和蚝油）', '豆腐【原文未给用量、鱼露】',
])
def test_recipe_notes_never_cover_animal_subingredients(text):
    assert statuses(text) == ('not_suitable',) * 3


def test_recipe_notes_preserve_pungent_subingredient_and_independent_diet_results():
    result = evaluate('豆腐（焯水用、洋葱，原文未给用量）', True, [], False)
    assert statuses('豆腐（焯水用、洋葱，原文未给用量）') == ('not_suitable', 'suitable', 'suitable')
    assert result['pungent_items'] == ['洋葱']


@pytest.mark.parametrize('text, unknown', [
    ('神秘粉 20g（原文未给用量）', '神秘粉'),
    ('水（焯水用神秘调料）', '焯水用神秘调料'),
    ('豆腐（原文未给用量的复合配料）', '原文未给用量的复合配料'),
    ('水（其他工艺说明）', '其他工艺说明'),
    ('盐（未知添加剂，用量原文未给）', '未知添加剂'),
])
def test_annotation_whitelist_does_not_hide_unrecognized_food_or_residue(text, unknown):
    result = evaluate(text, True, [], False)
    assert statuses(text) == ('uncertain',) * 3
    assert unknown in result['results']['vegan']['unknown']


def test_annotations_without_actual_ingredients_do_not_create_a_safe_result():
    result = evaluate('清洗、焯水用、原文未给用量', True, [], False)
    assert result['items'] == []
    assert statuses('清洗、焯水用、原文未给用量') == ('uncertain',) * 3
    assert all(value['incomplete'] for value in result['results'].values())


@pytest.mark.parametrize('text', [
    '4磅大黄，切成小块', '1盎司燕麦', '1夸脱水', '1品脱水', '1撮盐',
    '1 ½ 杯燕麦', '1½杯燕麦', '¼–½杯杏仁', '1 L water',
    '新鲜苹果 4千克（8.8磅）', '嫩大黄 2磅（约1千克），切碎',
    '新鲜柠檬汁 3汤匙', '中等大小的番茄 2个', '成熟的牛油果 3个',
    '2汤匙燕麦麸，水1杯，盐⅛茶匙', '2 tbsp oat bran, 1 cup water, ⅛ tsp salt',
])
def test_bounded_recipe_measures_descriptions_and_oat_bran_can_be_recognized(text):
    assert statuses(text) == ('suitable',) * 3


@pytest.mark.parametrize('note', [
    '切碎', '擦丝', '切丝', '切成薄片', '切成细丝', '切成小块', '切成细条',
    '取汁', '去蒂', '对半切开', '去籽并去筋',
])
def test_only_the_exact_additional_cutting_annotation_is_discarded(note):
    assert statuses('番茄（' + note + '）') == ('suitable',) * 3


@pytest.mark.parametrize('text', [
    'rhubarb, thinly sliced', 'rhubarb julienned', 'lemon juiced', 'apple cored',
    'tomato seeded', 'almonds slivered', 'sesame seeds toasted', 'tender rhubarb',
])
def test_finite_english_preparation_words_do_not_become_extra_ingredients(text):
    assert statuses(text) == ('suitable',) * 3


@pytest.mark.parametrize('text', [
    '牛奶250毫升', '新鲜牛奶250毫升', '粗磨乳粉20克', '燕麦麸（乳粉）',
    '2 tbsp oat bran (milk powder)', '植物肉（乳粉）',
])
def test_measure_or_description_cleanup_does_not_drop_milk(text):
    assert statuses(text)[0:2] == ('not_suitable',) * 2


@pytest.mark.parametrize('text', [
    '蚝油2汤匙', '嫩牛肉200克', '粗磨牛肉1磅', '薄片牛肉',
    '新鲜鱼肉250克', 'fish thinly sliced', 'slivered ham',
])
def test_prepared_animal_foods_still_fail_all_three_diets(text):
    assert statuses(text) == ('not_suitable',) * 3


@pytest.mark.parametrize('text', [
    '神秘粉20克', '20盎司神秘粉', '新鲜神秘粉20克', '嫩植物肉',
    '切成薄片的未知配料', '番茄（切碎神秘调料）', '水（去籽并去筋的复合配料）',
    '1罐红腰豆', '1包燕麦麸', '1盒豆腐', '1 can red kidney beans',
    'bread', 'peanut butter', 'chocolate chips', 'white wine', 'red wine',
])
def test_unknown_or_packaged_products_are_not_cleared_by_recipe_cleanup(text):
    assert statuses(text) == ('uncertain',) * 3


@pytest.mark.parametrize('text', ['中等大小的洋葱', '新鲜大蒜2瓣', 'green onion', 'white onion'])
def test_cutting_and_quantity_cleanup_never_gives_pungent_food_a_pure_clearance(text):
    assert statuses(text)[0] == 'not_suitable'


def test_food_names_with_apparent_size_prefixes_remain_whole_food_names():
    assert normalize('大黄、大米、小麦') == ['大黄', '大米', '小麦']


@pytest.mark.parametrize('text, expected', [
    ('桂圆15颗', '桂圆'), ('孜然籽3g', '孜然'),
    ('香菜叶15g', '香菜'), ('香菜粉5g', '香菜'),
])
def test_exact_approved_recipe_food_names_and_aliases_are_recognized(text, expected):
    assert normalize(text) == [expected]
    assert statuses(text) == ('suitable',) * 3


@pytest.mark.parametrize('text', ['香菜叶15g', '香菜粉5g'])
def test_coriander_recipe_aliases_keep_the_personal_avoidance(text):
    result = evaluate(text, True, [], True)
    assert result['personal_avoidances'] == ['香菜']
    assert result['pungent_items'] == []
    assert all(item['status'] == 'suitable' for item in result['results'].values())


@pytest.mark.parametrize('text, expected', [
    ('糯米100g（原文可用大米）', ['糯米', '大米']),
    ('原文可用桂圆15颗', ['桂圆']),
    ('原文可用牛奶100ml', ['牛奶']),
    ('原文可用肉汁', ['肉汁']),
    ('原文可用生抽', ['生抽']),
    ('原文可用神秘粉', ['神秘粉']),
])
def test_source_alternative_retains_the_entire_ingredient_after_the_introducer(text, expected):
    assert normalize(text) == expected


@pytest.mark.parametrize('text', ['原文可用牛奶100ml', '糯米（原文可用乳粉）',
                                 '原文可用复合调味粉（乳粉）'])
def test_source_alternatives_cannot_hide_milk_evidence(text):
    result = evaluate(text, True, [], False)
    assert all(result['results'][diet]['status'] == 'not_suitable' for diet in ('pure', 'vegan'))
    assert any(item['group'] == 'egg_milk' for item in result['items'])


@pytest.mark.parametrize('text', ['原文可用葱', '原文可用蒜', '原文可用葱和蒜'])
def test_source_alternatives_do_not_remove_pungents(text):
    assert statuses(text) == ('not_suitable', 'suitable', 'suitable')


@pytest.mark.parametrize('text', ['原文可用蚝油', '原文可用鱼露', '原文可用肉汤'])
def test_source_alternatives_do_not_remove_animal_ingredients(text):
    assert statuses(text) == ('not_suitable',) * 3


@pytest.mark.parametrize('text, unknown', [
    ('原文可用肉汁', '肉汁'), ('原文可用神秘粉', '神秘粉'),
    ('原文可用生抽', '生抽'), ('原文可用印度综合香料粉', '印度综合香料粉'),
    ('原文可用食用油', '食用油'), ('原文可用', '原文可用'),
])
def test_source_alternatives_do_not_clear_unknown_or_compound_products(text, unknown):
    result = evaluate(text, True, [], False)
    assert statuses(text) == ('uncertain',) * 3
    assert unknown in result['results']['vegan']['unknown']


def test_only_exact_dryness_note_is_discarded():
    assert statuses('水15-30ml（太干时可选）') == ('suitable',) * 3
    # Contains an actual ingredient claim: never discard by prefix or brackets.
    result = evaluate('水（太干时加牛奶）', True, [], False)
    assert '太干时加牛奶' in result['results']['vegan']['unknown']
    assert result['results']['vegan']['status'] == 'uncertain'
    assert statuses('太干时') == ('uncertain',) * 3  # No actual ingredient.


@pytest.mark.parametrize('text', ['桂圆（乳粉）', '香菜叶（乳粉）', '香菜粉（牛奶）'])
def test_new_plant_names_do_not_cover_parenthesized_milk(text):
    assert statuses(text)[0:2] == ('not_suitable',) * 2


def test_new_cumin_alias_does_not_cover_parenthesized_animal_ingredient():
    assert statuses('孜然籽（蚝油）') == ('not_suitable',) * 3

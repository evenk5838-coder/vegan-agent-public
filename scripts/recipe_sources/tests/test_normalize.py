import unittest

from scripts.recipe_sources.normalize import explicit_animal_evidence, normalize_english


class IngredientAliases(unittest.TestCase):
    def test_amounts_and_known_plain_ingredients(self):
        self.assertEqual(normalize_english('2 cups plain flour')['terms'], ['面粉'])
        self.assertEqual(normalize_english('1 tbsp olive oil')['terms'], ['橄榄油'])

    def test_unknown_remainder_is_never_dropped(self):
        value = normalize_english('salt and mystery flavouring')
        self.assertIn('盐', value['terms'])
        self.assertTrue(value['unknown_remainder'])
        self.assertTrue(any('待核实' in term for term in value['terms']))

    def test_plant_names_do_not_become_dairy_or_eggs(self):
        for text in ('peanut butter', 'cocoa butter', 'apple butter', 'butter beans'):
            with self.subTest(text=text):
                self.assertNotIn('黄油', normalize_english(text)['terms'])
        self.assertNotIn('鸡蛋', normalize_english('eggplant')['terms'])

    def test_compound_and_product_claims_remain_explicit(self):
        self.assertIn('酱油', normalize_english('2 tbsp soy sauce')['terms'])
        for text in ('vegan chicken', 'vegetarian sausage', 'dairy-free cream'):
            self.assertEqual(normalize_english(text)['method'], 'product_claim_requires_ingredients')

    def test_honey_alliums_and_animals_are_recognized_as_different_terms(self):
        self.assertIn('蜂蜜', normalize_english('1 tsp honey')['terms'])
        self.assertIn('洋葱', normalize_english('2 onions, chopped')['terms'])
        self.assertIn('鱼', normalize_english('250 g fish')['terms'])

    def test_source_triage_excludes_clear_meat_without_certifying_substitutes(self):
        self.assertEqual(explicit_animal_evidence(['200g 肥牛', '2 slices bacon']), ['200g 肥牛', '2 slices bacon'])
        self.assertEqual(explicit_animal_evidence(['vegetarian bacon', '素蚝油', 'vegetables']), [])
        self.assertTrue(explicit_animal_evidence(['瘦肉切片', '100g 肉末', 'minced meat']))
        self.assertEqual(explicit_animal_evidence(['meatless burger', '素肉片', '肉桂']), [])


if __name__ == '__main__':
    unittest.main()

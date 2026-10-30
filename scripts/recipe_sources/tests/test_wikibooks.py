"""Offline structural extraction tests; no DB, external provider or model."""
from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from scripts.recipe_sources.wikibooks import parse


def page(text: str, **changes) -> dict:
    return {"title": "Cookbook:Example dish", "pageid": 1,
            "revisions": [{"revid": 123, "slots": {"main": {"content": text, "contentmodel": "wikitext"}}}], **changes}


class WikibooksTests(unittest.TestCase):
    def test_original_words_units_and_revision_are_preserved(self):
        result = parse(page("""{{Recipe summary|Servings=4–6}}
{{Recipe}} | [[Cookbook:Vegan cuisine|Vegan]]

An original dish description.

==Ingredients==
* {{frac|1|2}} [[Cookbook:Cup|cup]] [[Cookbook:Milk|milk]] or oat milk
* {{units|200|g}} flour
==Procedure==
# Mix the ingredients.
# Bake at {{convert|180|C|F}}.
[[Category:Vegan recipes]]
"""))
        self.assertEqual(result["title"], "Example dish")
        self.assertEqual(result["ingredients"], ["1/2 cup milk or oat milk", "200 g flour"])
        self.assertEqual(result["steps"], ["Mix the ingredients.", "Bake at 180 C."])
        self.assertEqual(result["servings"], "4–6")
        self.assertEqual(result["body"], "An original dish description.")
        self.assertEqual(parse_qs(urlparse(result["source_url"]).query)["oldid"], ["123"])
        self.assertEqual(result["source_revision"], 123)
        self.assertIn("Category:Vegan recipes", result["source_categories"])
        self.assertTrue(result["structural_complete"])
        self.assertFalse(result["manual_reviewed"])
        self.assertFalse(result["dietary_verified"])
        self.assertIn("conversion_not_calculated", result["quality_flags"])

    def test_component_subsections_retain_paths_without_swallowing_notes_or_equipment(self):
        result = parse(page("""==Ingredients==
===Dough===
* Flour
===Filling===
* Cheese
==Equipment==
* Oven
==Preparation==
===Dough===
# Knead.
===Assembly===
# Fill and bake.
===Notes===
* Add chicken if desired.
"""))
        self.assertEqual(result["ingredients"], ["Flour", "Cheese"])
        self.assertEqual(result["steps"], ["Knead.", "Fill and bake."])
        self.assertTrue(any("Add chicken if desired." in section["text"] for section in result["review_sections"]))
        self.assertIn(["Ingredients", "Filling"], [section["path"] for section in result["sections"]])
        self.assertIn("missing_servings", result["quality_flags"])
        self.assertIsNone(result["servings"])

    def test_component_first_and_all_supported_step_headings(self):
        for heading in ("Procedure", "Preparation", "Directions", "Method", "Instructions"):
            result = parse(page(f"==Sauce==\n===Ingredients===\n* Tomato\n==={heading}===\n# Simmer.\n"))
            self.assertEqual(result["ingredients"], ["Tomato"])
            self.assertEqual(result["steps"], ["Simmer."])

    def test_unknown_templates_are_not_guessed_or_hidden(self):
        result = parse(page("==Ingredients==\n* {{mystery|100|grams}} flour\n==Directions==\n# Mix.\n"))
        self.assertIn("{{mystery|100|grams}}", result["ingredients"][0])
        self.assertIn("unparsed_template:mystery", result["quality_flags"])
        self.assertFalse(result["structural_complete"])

    def test_incomplete_and_redirect_sources_never_become_complete(self):
        text = "{{Incomplete recipe}}\n==Ingredients==\n* Rice\n==Method==\n# Cook."
        result = parse(page(text))
        self.assertIn("source_incomplete", result["quality_flags"])
        self.assertFalse(result["structural_complete"])
        redirect = parse(page("#REDIRECT [[Cookbook:Other dish]]"))
        self.assertIn("redirect", redirect["quality_flags"])
        self.assertIn("missing_ingredients", redirect["quality_flags"])
        self.assertIn("missing_steps", redirect["quality_flags"])

    def test_metadata_nested_links_and_templates_do_not_split_servings_or_leak_html(self):
        result = parse(page("""{{recipesummary|Image=[[File:test.jpg|thumb|Caption]]|servings={{frac|1|2}} batch}}
<script>alert('unsafe')</script>
==Ingredients==
* [[Cookbook:Salt|Salt]] &amp; pepper<ref>{{cite web|url=https://example.org}}</ref>
==Method==
# {{lang|en|Mix}}.<br/>Serve.
"""))
        self.assertEqual(result["servings"], "1/2 batch")
        self.assertEqual(result["ingredients"], ["Salt & pepper"])
        self.assertEqual(result["steps"], ["Mix.\nServe."])
        self.assertNotIn("alert", str(result["sections"]))
        self.assertIn("unsafe_html_removed", result["quality_flags"])

    def test_wikitables_and_missing_revision_do_not_fabricate_a_usable_inventory(self):
        result = parse(page("==Ingredients==\n{| class=\"wikitable\"\n!Ingredient!!Amount\n|-\n|Rice||1 cup\n|}\n==Method==\n# Cook."))
        self.assertEqual(result["ingredients"], [])
        self.assertIn("unparsed_table", result["quality_flags"])
        self.assertFalse(result["structural_complete"])
        result = parse({"title": "Cookbook:Empty", "revisions": []})
        self.assertIsNone(result["source_revision"])
        self.assertIn("missing_source_revision", result["quality_flags"])
        self.assertFalse(result["structural_complete"])

    def test_malformed_markup_is_retained_and_flagged_instead_of_crashing(self):
        result = parse(page("==Ingredients==\n* {{unknown|flour\n==Method==\n# Mix."))
        self.assertIn("unclosed_template", result["quality_flags"])
        self.assertFalse(result["structural_complete"])
        self.assertIn("{{unknown", result["ingredients"][0])


if __name__ == "__main__":
    unittest.main()

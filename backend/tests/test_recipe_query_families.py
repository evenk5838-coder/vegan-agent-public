from app.services.recipe_query import _match_query


def test_legume_query_finds_tofu_but_not_potato_or_nutmeg():
    rows = [{'title': '凉拌豆腐', 'ingredients': ['豆腐 200g']},
            {'title': '土豆丝', 'ingredients': ['土豆 300g']},
            {'title': '香料', 'ingredients': ['肉豆蔻 1g']},
            {'title': '鹰嘴豆沙拉', 'ingredients': ['鹰嘴豆 100g']}]
    assert [row['title'] for row in _match_query(rows, '豆类')] == ['凉拌豆腐', '鹰嘴豆沙拉']


def test_family_alias_does_not_reinterpret_negated_or_other_words():
    rows = [{'title': '豆腐', 'ingredients': ['豆腐 200g']}]
    assert _match_query(rows, '不要豆类') == []
    assert _match_query(rows, '豆制品') == rows

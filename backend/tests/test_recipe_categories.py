"""菜谱新分类工程验证：meal 扩展、甜点交叉标签、列表/搜索/对话工具同条件过滤、旧 main 兼容。"""
from sqlalchemy import select

from app import core, providers, worker


def recipe(title='豆腐碗', **kw):
    return {'title': title, 'ingredients': '豆腐、番茄、橄榄油',
            'steps': '豆腐200g，番茄1个，炒熟。', 'meal': 'main', 'purpose': 'everyday',
            'cuisine': 'chinese', 'confirmed': True, **kw}


def test_new_meals_accepted_and_legacy_main_kept(signed):
    for meal in ('breakfast', 'lunch', 'dinner', 'afternoon_tea'):
        assert signed.post('/api/v1/recipes', json=recipe(f'{meal}菜', meal=meal)).status_code == 201
    legacy = signed.post('/api/v1/recipes', json=recipe('旧正餐')).json()
    assert legacy['meal'] == 'main'  # 旧值不被改写
    data = signed.get('/api/v1/recipes').json()
    assert data['meal_entries'] == ['breakfast', 'lunch', 'dinner', 'afternoon_tea', 'main']
    assert 'legacy_notice' in data


def test_meal_and_tag_filters_apply(signed):
    signed.post('/api/v1/recipes', json=recipe('早餐粥', meal='breakfast'))
    signed.post('/api/v1/recipes', json=recipe('早餐甜点', meal='breakfast', tags=['dessert']))
    signed.post('/api/v1/recipes', json=recipe('午餐饭', meal='lunch'))
    assert len(signed.get('/api/v1/recipes?meal=breakfast').json()['items']) == 2
    dessert = signed.get('/api/v1/recipes?tag=dessert').json()['items']
    assert [x['title'] for x in dessert] == ['早餐甜点']
    # 分类与标签可交叉组合
    both = signed.get('/api/v1/recipes?meal=breakfast&tag=dessert').json()['items']
    assert [x['title'] for x in both] == ['早餐甜点']
    assert signed.get('/api/v1/recipes?meal=lunch').json()['items'][0]['title'] == '午餐饭'


def test_invalid_filter_rejected(signed):
    r = signed.get('/api/v1/recipes?meal=brunch')
    assert r.status_code == 422
    assert r.json()['error']['code'] == 'INVALID_FILTER'
    assert signed.get('/api/v1/recipes?tag=lunch').status_code == 422


def test_western_filter_does_not_absorb_unspecified_foreign_records(signed):
    foreign = signed.post('/api/v1/recipes', json=recipe('未细分外国菜', cuisine='foreign')).json()
    assert foreign['cuisine'] == 'foreign'
    signed.post('/api/v1/recipes', json=recipe('西式豆腐', cuisine='western'))
    rows = signed.get('/api/v1/recipes').json()['items']
    legacy = next(row for row in rows if row['id'] == foreign['id'])
    assert legacy['cuisine'] == 'foreign'
    assert legacy['cuisine_group'] == 'unclassified'
    western = signed.get('/api/v1/recipes?cuisine=western').json()['items']
    assert [row['title'] for row in western] == ['西式豆腐']


def test_search_applies_same_filters(signed):
    signed.post('/api/v1/recipes', json=recipe('番茄早餐', meal='breakfast'))
    signed.post('/api/v1/recipes', json=recipe('番茄晚餐', meal='dinner', tags=['dessert']))
    # 搜索带 meal 过滤：只返回匹配餐次的
    data = signed.post('/api/v1/recipes/search', json={'query': '番茄', 'meal': 'dinner'}).json()
    assert data['local'] is True
    assert [x['title'] for x in data['items']] == ['番茄晚餐']
    # 同一菜名但餐次不符 → 库内无结果，走联网补位（不会返回错误餐次）
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/login', json={'username': 'alice', 'password': 'correct-horse-123'})
    r = signed.post('/api/v1/recipes/search', json={'query': '番茄', 'meal': 'afternoon_tea'})
    assert r.status_code == 503  # 联网未配置：明确失败，不返回不匹配结果


def test_search_tag_filter_and_diet(signed):
    signed.post('/api/v1/recipes', json=recipe('豆腐甜点', tags=['dessert']))
    signed.post('/api/v1/recipes', json=recipe('豆腐主菜'))
    local = signed.post('/api/v1/recipes/search', json={'query': '豆腐', 'tag': 'dessert'}).json()
    assert [x['title'] for x in local['items']] == ['豆腐甜点']
    vegan = signed.get('/api/v1/recipes?diet=vegan').json()['items']
    assert len(vegan) == 2
    signed.put('/api/v1/preferences', json={'diet': 'vegan', 'avoid_coriander': True})
    signed.post('/api/v1/recipes', json=recipe('香菜沙拉', ingredients='香菜、番茄'))
    avoid = signed.get('/api/v1/recipes?diet=vegan').json()['items']
    assert '香菜沙拉' not in [x['title'] for x in avoid]


def test_legacy_record_without_tags_reads_empty(signed):
    created = signed.post('/api/v1/recipes', json=recipe('无标签')).json()
    assert created['tags'] == []
    assert signed.get('/api/v1/recipes?tag=dessert').json()['items'] == []


def test_chat_recipe_tool_uses_same_filters(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    signed.post('/api/v1/recipes', json=recipe('豆腐早餐', meal='breakfast'))
    signed.post('/api/v1/recipes', json=recipe('豆腐晚餐', meal='dinner', tags=['dessert']))
    replies = iter([
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'search_recipes',
                         'arguments': {'query': '豆腐', 'meal': 'dinner', 'tag': 'dessert'}}]},
        {'text': '找到一道。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(replies))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '找豆腐晚餐'}).json()['turn_id']
    worker.process_once()
    cards = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['reply']['cards']
    rows = [c for c in cards if c['type'] == 'recipe_list'][0]['payload']['items']
    assert [x['title'] for x in rows] == ['豆腐晚餐']

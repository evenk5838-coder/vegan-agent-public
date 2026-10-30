"""菜谱知识条目：结构映射、发布可见性、账号隔离、筛选与幂等导入。

用隔离夹具验证接线；夹具不是正式内容，也不代表任何条目已被批准发布。
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import core, knowledge, providers, worker

def entries():
    # Synthetic test inputs only; never published into the business database.
    return [
        {'type': 'recipe', 'title': '测试中式配方', 'source': 'https://example.org/test/a',
         'status': 'draft', 'ingredients': ['植物鸡 200g', '蒜 1 瓣', '豆腐 100g', '盐 1g', '水 10ml'],
         'steps': ['测试步骤甲', '测试步骤乙', '测试步骤丙', '测试步骤丁'],
         'servings': 2, 'meal': 'lunch', 'cuisine': 'chinese',
         'conditions': {'servings_basis': 'editorial_estimate'}},
        {'type': 'recipe', 'title': '测试早餐燕麦', 'source': 'https://example.org/test/b',
         'status': 'draft', 'ingredients': ['燕麦 100g', '水 200ml'],
         'steps': ['测试步骤甲', '测试步骤乙', '测试步骤丙', '测试步骤丁'],
         'servings': 4, 'meal': 'breakfast', 'cuisine': 'western'},
        {'type': 'recipe', 'title': '测试番茄意面', 'source': 'https://example.org/test/c',
         'status': 'draft', 'ingredients': ['无蛋意面 250g', '蒜 1 瓣', '番茄 100g'],
         'steps': ['测试步骤甲', '测试步骤乙', '测试步骤丙', '测试步骤丁', '测试步骤戊'],
         'servings': 2, 'meal': 'dinner', 'cuisine': 'western'},
        {'type': 'recipe', 'title': '测试产量不等于人数', 'source': 'https://example.org/test/d',
         'status': 'draft', 'ingredients': ['豆腐 100g'], 'steps': ['测试步骤甲'],
         'servings': None, 'conditions': {'yield_text': '2 小盅'}},
    ]


def publish(title: str):
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
        target = next(r for r in db.scalars(select(core.Record).where(core.Record.kind == 'knowledge'))
                      if r.payload.get('title') == title)
        knowledge.set_status(target.payload['entry_id'], 'published')


# ---------------- 结构映射 ----------------
def test_candidate_file_maps_to_full_tutorial_fields():
    report = knowledge.import_entries(entries())
    assert all(r['status'] == 'added' for r in report['results'])
    with core.DB() as db:
        rows = [r.payload for r in db.scalars(select(core.Record).where(core.Record.kind == 'knowledge'))]
    assert len(rows) == 4
    first = next(r for r in rows if r['title'] == '测试中式配方')
    # 分量、食材、步骤、来源、餐次、菜系都不能丢
    assert first['servings'] == 2
    assert len(first['ingredients']) == 5
    assert len(first['steps']) == 4
    assert first['source'].startswith('https://')
    assert first['meal'] == 'lunch' and first['cuisine'] == 'chinese'
    assert first['status'] == 'draft'
    assert '植物鸡' in ' '.join(first['ingredients'])


def test_invalid_recipe_fields_rejected():
    for bad in ({'servings': 0}, {'meal': 'brunch'}, {'cuisine': 'martian'}, {'servings': 'two'}):
        entry = {'type': 'recipe', 'title': f'坏条目{list(bad)[0]}', 'source': 'https://example.org/x', **bad}
        with pytest.raises(knowledge.AppError) as exc:
            knowledge.validate_entry(entry)
        assert exc.value.code == 'INVALID_ENTRY'


def test_summary_without_steps_is_not_complete():
    clean = knowledge.validate_entry({'type': 'recipe', 'title': '只有摘要',
                                      'source': 'https://example.org/s', 'body': '一句话摘要'})
    view = knowledge.recipe_view({**clean, 'entry_id': 'x'})
    assert view['tutorial_complete'] is False
    assert view['tutorial_status'] == 'source_summary'


# ---------------- 发布可见性 ----------------
def test_draft_invisible_and_published_visible(signed):
    knowledge.import_entries(entries())
    assert signed.get('/api/v1/recipes').json()['public_count'] == 0
    publish('测试早餐燕麦')
    data = signed.get('/api/v1/recipes').json()
    assert data['public_count'] == 1
    item = data['items'][0]
    assert item['title'] == '测试早餐燕麦'
    assert item['tutorial_complete'] is True
    assert item['meal'] == 'breakfast' and item['cuisine'] == 'western'
    assert len(item['steps']) == 4 and item['servings'] == 4   # 已恢复食用前调整步骤


def test_published_entry_searchable_and_filtered(signed):
    knowledge.import_entries(entries())
    for title in ('测试中式配方', '测试番茄意面'):
        publish(title)
    local = signed.post('/api/v1/recipes/search', json={'query': '意面'}).json()
    assert [i['title'] for i in local['items']] == ['测试番茄意面']
    assert local['public_count'] == 1
    chinese = signed.get('/api/v1/recipes?cuisine=chinese').json()['items']
    assert [i['title'] for i in chinese] == ['测试中式配方']


def test_private_recipes_stay_isolated_from_public(signed):
    signed.post('/api/v1/recipes', json={'title': '我的私房菜', 'ingredients': '豆腐',
                                         'steps': '炒', 'confirmed': True})
    knowledge.import_entries(entries())
    publish('测试番茄意面')
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    data = signed.get('/api/v1/recipes').json()
    assert data['personal_count'] == 0
    assert [i['title'] for i in data['items']] == ['测试番茄意面']


def test_agent_recipe_tool_sees_only_published(signed, monkeypatch):
    knowledge.import_entries(entries())
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    assert agent._search_recipes(owner, '燕麦', None, None, None, None,
                                 {'diet': 'vegan', 'avoid_coriander': False}) == []
    publish('测试早餐燕麦')
    rows = agent._search_recipes(owner, '燕麦', None, None, None, None,
                                 {'diet': 'vegan', 'avoid_coriander': False})
    assert [r['title'] for r in rows] == ['测试早餐燕麦']
    assert rows[0]['tutorial_complete'] is True


def test_empty_library_is_stated_not_faked(signed):
    data = signed.get('/api/v1/knowledge').json()
    assert data['state'] == 'empty' and data['items'] == []
    assert '暂无已发布' in data['notice']


# ---------------- 幂等导入 ----------------
def test_import_is_idempotent():
    first = knowledge.import_entries(entries())
    assert sum(1 for r in first['results'] if r['status'] == 'added') == 4
    again = knowledge.import_entries(entries())
    assert all(r['status'] == 'skipped' for r in again['results'])
    with core.DB() as db:
        assert len(db.scalars(select(core.Record).where(core.Record.kind == 'knowledge')).all()) == 4


def test_import_does_not_touch_personal_recipes(signed):
    signed.post('/api/v1/recipes', json={'title': '我的菜', 'ingredients': '豆腐',
                                         'steps': '炒', 'confirmed': True})
    knowledge.import_entries(entries())
    assert signed.get('/api/v1/recipes').json()['personal_count'] == 1


def test_dry_run_preview_does_not_write(signed, tmp_path):
    from scripts.import_knowledge import preview
    candidate_file = tmp_path / 'synthetic-recipes.json'
    candidate_file.write_text(json.dumps({'entries': entries()}, ensure_ascii=False))
    report = preview(str(candidate_file))
    assert report['dry_run'] is True and report['total'] == 4
    assert report['would_publish'] == 0
    assert all(r['tutorial_complete'] for r in report['rows'])
    with core.DB() as db:
        assert db.scalars(select(core.Record).where(core.Record.kind == 'knowledge')).all() == []


# ---------------- 复审收尾：内容与规则输入（K1/K2） ----------------
def test_synthetic_yield_and_editorial_servings_metadata_survive_validation():
    by_title = {e['title']: knowledge.validate_entry(e) for e in entries()}
    yield_only = by_title['测试产量不等于人数']
    assert yield_only['servings'] is None
    assert yield_only['conditions']['yield_text'] == '2 小盅'
    assert by_title['测试中式配方']['conditions']['servings_basis'] == 'editorial_estimate'


def test_k2_quantities_do_not_break_rule_detection():
    """带用量的展示食材不能导致明确禁忌退化为未知。"""
    from app.recipes import evaluate_recipe, normalize_ingredient
    prefs = {'diet': 'vegan', 'avoid_coriander': False}

    assert normalize_ingredient('蒜 2 瓣') == '蒜'
    assert normalize_ingredient('蒜2～3瓣') == '蒜'
    assert normalize_ingredient('鸡肉 100g') == '鸡肉'
    assert normalize_ingredient('植物鸡 200g（实际成分待核对包装）') == '植物鸡'

    garlic = evaluate_recipe({'ingredients': ['蒜 2 瓣']}, prefs)['evaluation']
    assert garlic['results']['pure']['status'] == 'not_suitable'
    assert garlic['pungent'] == 'detected' and garlic['pungent_items'] == ['蒜']

    chicken = evaluate_recipe({'ingredients': ['鸡肉 100g']}, prefs)['evaluation']
    assert all(chicken['results'][d]['status'] == 'not_suitable' for d in ('pure', 'vegan', 'lacto_ovo'))

    onion = evaluate_recipe({'ingredients': ['洋葱 150g', '蒜 1 瓣']}, prefs)['evaluation']
    assert onion['results']['pure']['status'] == 'not_suitable'
    assert set(onion['pungent_items']) == {'洋葱', '蒜'}

    unknown = evaluate_recipe({'ingredients': ['植物鸡 200g']}, prefs)['evaluation']
    assert unknown['results']['vegan']['status'] == 'uncertain'   # 未知复合产品保持待核对


def test_k2_candidate_recipes_detection_is_meaningful(signed):
    """导入后：含蒜/洋葱的条目纯净素不适配，含鸡肉的不会判适配。"""
    knowledge.import_entries(entries())
    publish('测试中式配方')
    publish('测试番茄意面')
    items = {i['title']: i for i in signed.get('/api/v1/recipes?diet=pure').json()['items']}
    # 两条都含五辛 → 纯净素筛选下都不应出现
    assert '测试中式配方' not in items
    assert '测试番茄意面' not in items
    everything = {i['title']: i for i in signed.get('/api/v1/recipes').json()['items']}
    dish = everything['测试中式配方']
    assert dish['evaluation']['results']['pure']['status'] == 'not_suitable'
    assert dish['evaluation']['pungent_items']


def test_k2_display_ingredients_keep_quantities():
    """展示字段保留用量，规则用字段已剥离用量，两者不混淆。"""
    knowledge.import_entries(entries())
    publish('测试番茄意面')
    with core.DB() as db:
        item = knowledge.recipe_view(
            next(r.payload for r in db.scalars(select(core.Record).where(core.Record.kind == 'knowledge'))
                 if r.payload['title'] == '测试番茄意面'))
    assert any('250g' in i for i in item['ingredients'])       # 展示保留分量
    assert '蒜' in item['rule_ingredients']                    # 判定用名称
    assert all(not any(ch.isdigit() for ch in n) for n in item['rule_ingredients'])

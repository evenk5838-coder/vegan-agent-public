"""v0.3 首批验收：菜谱库内只读查询共用服务。

覆盖：
1. HTTP 与 Agent 在相同条件下的库内结果一致；
2. 账号隔离（他人私有菜谱不可见）与公共/私有可见性；
3. 服务不依赖 main.py / 路由 / 内部 HTTP（结构断言）；
4. POST /recipes/search 的联网补位、用量记录、候选持久化仍保留；
5. Agent 本批仍只查库，不触发联网。
"""
import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import core, knowledge, providers, worker
from app.services import recipe_query


def recipe(title, **kw):
    return {'title': title, 'ingredients': '豆腐、番茄、橄榄油', 'steps': '炒熟。',
            'meal': 'main', 'purpose': 'everyday', 'cuisine': 'chinese', 'confirmed': True, **kw}


def publish_public_recipe(title='豆腐碎炒：来源事实摘要', meal='breakfast'):
    knowledge.import_entries([{
        'type': 'recipe', 'title': title,
        'source': 'https://www.vegansociety.com/resources/recipes/quick-and-easy/tofu-scramble',
        'body': '主要材料为硬豆腐、洋葱、番茄、植物油和蒜。含五辛，不能标为纯净素。',
        'conditions': {'meal': meal, 'cuisine': 'western', 'servings': 2, 'summary_only': True},
        'status': 'published'}])
    with core.DB() as db:
        entry_id = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge')).payload['entry_id']
    knowledge.set_status(entry_id, 'published')
    return entry_id


def titles(rows):
    return [row['title'] for row in rows]


# ---------- 1. 两个入口一致 ----------
@pytest.mark.parametrize('params,expected', [
    ({}, ['早餐粥', '我的番茄豆腐']),
    ({'meal': 'breakfast'}, ['早餐粥']),
    ({'meal': 'lunch'}, []),
    ({'tag': 'dessert'}, []),
    ({'diet': 'vegan'}, ['早餐粥', '我的番茄豆腐']),
])
def test_http_and_agent_agree_on_library_query(signed, monkeypatch, params, expected):
    signed.post('/api/v1/recipes', json=recipe('我的番茄豆腐'))
    signed.post('/api/v1/recipes', json=recipe('早餐粥', meal='breakfast'))

    http_rows = signed.get('/api/v1/recipes', params=params).json()['items']

    # 同一组条件下走 Agent 工具（直接调用工具适配器，避免依赖模型）
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    tool_rows = agent._search_recipes(
        owner, '', params.get('meal'), params.get('tag'), params.get('diet'),
        params.get('purpose'), {'diet': 'vegan', 'avoid_coriander': False}, params.get('cuisine'))

    assert titles(http_rows) == titles(tool_rows) == expected


def test_query_keyword_agrees_between_entries(signed):
    signed.post('/api/v1/recipes', json=recipe('番茄豆腐'))
    # 另一道菜用不同原料，避免被“原料包含番茄”命中（新检索会查原料字段）
    signed.post('/api/v1/recipes', json=recipe('西兰花炒饭', ingredients='西兰花、大米、植物油'))
    http_rows = signed.post('/api/v1/recipes/search', json={'query': '番茄'}).json()['items']
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    tool_rows = agent._search_recipes(owner, '番茄', None, None, None, None,
                                      {'diet': 'vegan', 'avoid_coriander': False})
    assert titles(http_rows) == titles(tool_rows) == ['番茄豆腐']


def test_service_returns_expected_shape(signed):
    signed.post('/api/v1/recipes', json=recipe('番茄豆腐'))
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    result = recipe_query.query_recipes(owner, {}, {'diet': 'vegan', 'avoid_coriander': False})
    assert set(result) == {'personal', 'public', 'items'}
    assert result['items'] == result['personal'] + result['public']
    assert result['personal'][0]['title'] == '番茄豆腐'
    assert 'evaluation' in result['personal'][0]


# ---------- 2. 隔离与可见性 ----------
def test_private_recipe_invisible_to_other_account(signed):
    signed.post('/api/v1/recipes', json=recipe('我的番茄豆腐'))
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    data = signed.get('/api/v1/recipes').json()
    assert data['personal_count'] == 0
    assert titles(data['items']) == []
    from app import agent
    with core.DB() as db:
        bob = db.scalar(select(core.User).where(core.User.username == 'bob')).id
    assert agent._search_recipes(bob, '番茄', None, None, None, None,
                                 {'diet': 'vegan', 'avoid_coriander': False}) == []


def test_public_entry_visible_to_all_and_marked_summary(signed):
    publish_public_recipe()
    data = signed.get('/api/v1/recipes').json()
    assert data['public_count'] == 1
    item = data['items'][0]
    assert item['scope'] == 'public' and item['tutorial_complete'] is False
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get('/api/v1/recipes').json()['public_count'] == 1


def test_unpublished_public_entry_hidden(signed):
    knowledge.import_entries([{'type': 'recipe', 'title': '待审菜谱', 'source': 'https://example.org/x',
                               'body': '摘要'}])
    assert signed.get('/api/v1/recipes').json()['public_count'] == 0
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    assert agent._search_recipes(owner, '待审', None, None, None, None,
                                 {'diet': 'vegan', 'avoid_coriander': False}) == []


# ---------- 3. 结构约束：服务不反向依赖路由 ----------
def test_service_does_not_import_routes_or_http():
    source = Path(recipe_query.__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(name.endswith('main') or '.main' == name for name in imported), imported
    assert 'httpx' not in imported and 'requests' not in imported
    assert 'fastapi' not in imported


def test_service_module_has_no_network_or_write_calls():
    source = Path(recipe_query.__file__).read_text()
    for forbidden in ('providers.', 'search_web', 'reserve_call', 'db.add', 'db.commit', '.delete('):
        assert forbidden not in source, f'只读服务中出现 {forbidden}'


# ---------- 4. HTTP 联网分支仍保留 ----------
def test_search_web_fallback_still_works(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    monkeypatch.setattr(providers, 'search_web',
                        lambda q: [{'title': '候选菜谱', 'source_url': 'https://example.org/r1',
                                    'summary': '摘要'}])
    r = signed.post('/api/v1/recipes/search', json={'query': '不存在的配方'})
    assert r.status_code == 200
    data = r.json()
    assert data['local'] is False
    assert data['items'][0]['title'] == '候选菜谱'
    assert data['items'][0]['status'] == '待查看原文并复核配料'
    assert '不是已整理的完整教程' in data['notice']
    # 候选持久化：7 天到期、独立于正式菜谱
    with core.DB() as db:
        candidates = db.scalars(select(core.Record).where(core.Record.kind == 'candidate')).all()
        assert len(candidates) == 1
        assert (candidates[0].expires - core.now()).days == 6  # 约 7 天
        assert db.scalars(select(core.Record).where(core.Record.kind == 'recipe')).all() == []
        usage = [r.payload.get('operation') for r in
                 db.scalars(select(core.Record).where(core.Record.kind == 'usage'))]
        assert 'recipe_search' in usage


def test_local_hit_does_not_call_web(signed, monkeypatch):
    signed.post('/api/v1/recipes', json=recipe('番茄豆腐'))
    called = []
    monkeypatch.setattr(providers, 'search_web', lambda q: called.append(q) or [])
    assert signed.post('/api/v1/recipes/search', json={'query': '番茄'}).json()['local'] is True
    assert called == []


def test_agent_tool_never_calls_web(signed, monkeypatch):
    """本批 Agent 仍只查库：命中与否都不触发联网配额与候选写入。"""
    called = []
    monkeypatch.setattr(providers, 'search_web', lambda q: called.append(q) or [])
    monkeypatch.setattr(providers, 'reserve_call', lambda *a, **k: called.append(('reserve', a)))
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    assert agent._search_recipes(owner, '不存在', None, None, None, None,
                                 {'diet': 'vegan', 'avoid_coriander': False}) == []
    assert called == []
    with core.DB() as db:
        assert db.scalars(select(core.Record).where(core.Record.kind == 'candidate')).all() == []


# ---------- 5. 回归：搜索响应语义不变 ----------
def test_search_response_fields_unchanged(signed):
    signed.post('/api/v1/recipes', json=recipe('番茄豆腐'))
    local = signed.post('/api/v1/recipes/search', json={'query': '番茄'}).json()
    assert set(local) >= {'local', 'items', 'filters', 'personal_count', 'public_count', 'public_notice'}
    assert local['local'] is True and local['personal_count'] == 1 and local['public_count'] == 0
    listing = signed.get('/api/v1/recipes').json()
    assert set(listing) >= {'items', 'personal_count', 'public_count', 'filters',
                            'meal_entries', 'tag_entries', 'legacy_notice',
                            'nutrition_reference', 'public_library_status'}


def test_agent_tool_still_returns_card_via_turn(signed, monkeypatch):
    """端到端仍是工具 → 卡片（不因抽取服务改变卡片结构）。"""
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    signed.post('/api/v1/recipes', json=recipe('番茄豆腐'))
    replies = iter([
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'call_1', 'name': 'search_recipes', 'arguments': {'query': '番茄'}}]},
        {'text': '找到一道。', 'needs_input': False, 'input_prompt': '', 'tool_calls': []},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(replies))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    turn_id = signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '找番茄菜'}).json()['turn_id']
    worker.process_once()
    cards = signed.get(f'/api/v1/chat/sessions/{sid}/turns/{turn_id}').json()['reply']['cards']
    rows = [c for c in cards if c['type'] == 'recipe_list'][0]['payload']['items']
    assert titles(rows) == ['番茄豆腐']

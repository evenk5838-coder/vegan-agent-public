"""大批量知识库接入 · 验收（隔离库夹具；不写业务库、不外呼、不发布真实内容）。"""
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import agent, core, knowledge, providers, worker
from app.core import now
from app.recipes import diet_labels
from app.rules import evaluate
from app.services import recipe_query



def entry(title, **kw):
    base = {'type': 'recipe', 'title': title, 'source': f'https://example.org/{title}',
            'status': 'draft', 'ingredients': ['豆腐', '番茄'], 'steps': ['炒熟'],
            'conditions': {'source_id': f'src:{title}', 'source_revision': 1,
                           'license': {'id': 'CC-BY-SA-4.0'}, 'language': 'zh-CN'}}
    base.update(kw)
    return base


def publish(title):
    with core.DB() as db:
        row = next(r for r in db.scalars(select(core.Record).where(core.Record.kind == 'knowledge'))
                   if r.payload.get('title') == title)
        knowledge.set_status(row.payload['entry_id'], 'published')
        return row.payload['entry_id']


# ================= 批量导入：幂等 / 去重 / 保护 =================
def test_batch_import_is_idempotent_and_dedupes(signed):
    entries = [entry('甲'), entry('乙'), entry('甲')]      # 批内重复
    first = knowledge.import_entries(entries)
    assert first['counts']['added'] == 2
    assert any(r['status'] == 'skipped' and r.get('reason') == 'duplicate_in_batch'
               for r in first['results'])
    again = knowledge.import_entries(entries)
    assert again['counts']['added'] == 0                  # 重跑不重复写入
    assert again['counts']['updated'] == 2
    with core.DB() as db:
        assert len(db.scalars(select(core.Record).where(core.Record.kind == 'knowledge')).all()) == 2


def test_import_does_not_overwrite_published_entry(signed):
    knowledge.import_entries([entry('丙')])
    entry_id = publish('丙')
    changed = entry('丙', ingredients=['豆腐', '牛肉'], conditions={'source_id': 'src:丙', 'source_revision': 2})
    result = knowledge.import_entries([changed])
    assert result['results'][0]['status'] == 'skipped'
    assert result['results'][0]['reason'] == 'already_reviewed_or_published'
    with core.DB() as db:
        row = next(r for r in db.scalars(select(core.Record).where(core.Record.kind == 'knowledge'))
                   if r.payload.get('entry_id') == entry_id)
        assert row.payload['ingredients'] == ['豆腐', '番茄']   # 未被覆盖


def test_import_preserves_source_metadata(signed):
    knowledge.import_entries([entry('丁', conditions={
        'source_id': 'src:丁', 'source_revision': 42,
        'license': {'id': 'CC-BY-SA-4.0', 'attribution': 'Wikibooks'},
        'language': 'en', 'quality_flags': ['missing_servings'],
        'source_servings_text': '2 servings', 'manual_reviewed': False})])
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
    meta = row.payload['source_meta']
    assert meta['source_revision'] == 42
    assert meta['license']['attribution'] == 'Wikibooks'
    assert meta['quality_flags'] == ['missing_servings']
    assert meta['source_servings_text'] == '2 servings'
    assert meta['language'] == 'en'


def test_structure_complete_but_flagged_is_not_claimed_complete(signed):
    knowledge.import_entries([entry('戊', conditions={
        'source_id': 'src:戊', 'quality_flags': ['missing_servings'], 'manual_reviewed': False})])
    view = knowledge.recipe_view(knowledge.published_recipes()[0]) if knowledge.published_recipes() else None
    publish('戊')
    view = next(v for v in [knowledge.recipe_view(i) for i in knowledge.published_recipes()]
                if v['title'] == '戊')
    assert view['tutorial_complete'] is True           # 结构齐全
    assert view['quality_flags'] == ['missing_servings']
    assert view['source_complete_unreviewed'] is True  # 但仍有待补项
    assert view['manual_reviewed'] is False


# ================= 三类标签：英文成分与保守规范化 =================
@pytest.mark.parametrize('text,expect', [
    ('4 eggs, 1 litre kefir or yogurt', {'pure': 'not_suitable', 'vegan': 'not_suitable'}),
    ('fish sauce, rice, onion', {'pure': 'not_suitable', 'vegan': 'not_suitable'}),
    ('beef, tomato, salt', {'pure': 'not_suitable', 'vegan': 'not_suitable'}),
    ('white granulated sugar, flour, water', {'pure': 'suitable', 'vegan': 'suitable'}),
])
def test_english_ingredients_are_normalized(text, expect):
    labels = diet_labels(evaluate(text, True, [], False))
    for diet, status in expect.items():
        assert labels[diet]['status'] == status, (text, diet, labels[diet])


def test_unmapped_english_stays_uncertain_not_vegan():
    labels = diet_labels(evaluate('zorblat extract, flour', True, [], False))
    assert labels['vegan']['status'] == 'uncertain'      # 英文未覆盖 → 待核实，不放行
    assert labels['vegan']['unknown']


def test_plants_and_compound_overlap_is_conservative():
    from app.rules import COMPOUND, PLANTS
    overlap = PLANTS & COMPOUND
    assert overlap, '存在同时出现在两表的条目'
    for name in sorted(overlap)[:3]:
        assert evaluate(name, True, [], False)['items'][0]['group'] == 'compound'


def test_source_vegan_label_is_not_trusted():
    """来源自称 Vegan 但含肉/鱼 → 规则判不适配。"""
    labels = diet_labels(evaluate('chicken, fish, shrimp, rice', True, [], False))
    assert labels['vegan']['status'] == 'not_suitable'
    assert labels['pure']['status'] == 'not_suitable'


# ================= 千条级检索：分页 / 多关键词 / 总量 =================
def _publish_many(n=30):
    entries = []
    for i in range(n):
        entries.append(entry(f'批量菜{i}', ingredients=['豆腐', '番茄', '植物油'],
                             steps=[f'步骤{i}', '翻炒'], conditions={'source_id': f'bulk:{i}', 'source_revision': 1,
                                                                  'language': 'zh-CN'}))
    knowledge.import_entries(entries)
    with core.DB() as db:
        for row in db.scalars(select(core.Record).where(core.Record.kind == 'knowledge')).all():
            knowledge.set_status(row.payload['entry_id'], 'published')


def test_list_pagination_and_real_total(signed):
    _publish_many(30)
    page1 = signed.get('/api/v1/recipes?page=1&page_size=10').json()
    assert page1['total'] == 30 and len(page1['items']) == 10
    assert page1['has_more'] is True and page1['next_page'] == 2
    page3 = signed.get('/api/v1/recipes?page=3&page_size=10').json()
    assert len(page3['items']) == 10 and page3['has_more'] is False
    page4 = signed.get('/api/v1/recipes?page=4&page_size=10').json()
    assert page4['items'] == [] and page4['total'] == 30


def test_search_returns_multi_keyword_and_totals(signed):
    _publish_many(30)
    one = signed.post('/api/v1/recipes/search', json={'query': '豆腐', 'page_size': 5}).json()
    assert one['local'] is True and one['total'] == 30 and len(one['items']) == 5
    two = signed.post('/api/v1/recipes/search', json={'query': '豆腐 步骤7', 'page_size': 5}).json()
    # 多关键词按“命中数多的优先”排序（OR 召回，避免整句必须完整匹配）
    assert two['total'] == one['total']
    assert two['items'][0]['title'] == '批量菜7'


def test_search_matches_ingredients_and_steps_not_only_title(signed):
    knowledge.import_entries([entry('名字无关', ingredients=['鹰嘴豆'], steps=['慢炖四十分钟'])])
    publish('名字无关')
    by_ingredient = signed.post('/api/v1/recipes/search', json={'query': '鹰嘴豆'}).json()
    by_step = signed.post('/api/v1/recipes/search', json={'query': '慢炖'}).json()
    assert [i['title'] for i in by_ingredient['items']] == ['名字无关']
    assert [i['title'] for i in by_step['items']] == ['名字无关']


def test_diet_filter_over_bulk(signed):
    _publish_many(10)
    knowledge.import_entries([entry('含肉菜', ingredients=['牛肉', '番茄'],
                                    conditions={'source_id': 'meat:1', 'source_revision': 1})])
    publish('含肉菜')
    vegan = signed.get('/api/v1/recipes?diet=vegan&page_size=50').json()
    titles = [i['title'] for i in vegan['items']]
    assert '含肉菜' not in titles
    assert any(t.startswith('批量菜') for t in titles)


# ================= 检索增强与引用 =================
def enable(monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)


def test_knowledge_tool_recalls_real_entries(signed, monkeypatch):
    enable(monkeypatch)
    _publish_many(5)
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    data, card = agent._execute_tool('search_knowledge', {'query': '批量菜3'}, owner, {})
    assert data['count'] >= 1
    hit = data['items'][0]
    assert hit['title'] == '批量菜3'
    assert hit['entry_id'] and hit['source'].startswith('https://')
    assert card['type'] == 'knowledge_result'


def test_citation_limited_to_recall_and_persisted(signed, monkeypatch):
    enable(monkeypatch)
    _publish_many(3)
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    recalled, _ = agent._execute_tool('search_knowledge', {'query': '批量菜1'}, owner, {})
    real_id = recalled['items'][0]['entry_id']
    replies = iter([
        {'text': '', 'needs_input': False, 'input_prompt': '',
         'tool_calls': [{'id': 'c1', 'name': 'search_knowledge', 'arguments': {'query': '批量菜1'}}]},
        {'text': '可以参考这条。', 'needs_input': False, 'input_prompt': '', 'tool_calls': [],
         'citations': [{'entry_id': real_id}, {'entry_id': 'forged'}]},
    ])
    monkeypatch.setattr(providers, 'generate_chat_reply', lambda h: next(replies))
    sid = signed.post('/api/v1/chat/sessions', json={}).json()['id']
    signed.post(f'/api/v1/chat/sessions/{sid}/messages', json={'content': '批量菜1 怎么做'})
    worker.process_once()
    msgs = signed.get(f'/api/v1/chat/sessions/{sid}/messages').json()['items']
    assistant = [m for m in msgs if m['role'] == 'assistant'][0]
    assert [c['entry_id'] for c in assistant['citations']] == [real_id]


# ================= W01：工具与页面共用历史写入 =================
def test_agent_ingredient_eval_writes_history(signed, monkeypatch):
    enable(monkeypatch)
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    data, card = agent._execute_tool('evaluate_ingredients',
                                     {'text': '小麦粉、奶粉', 'complete': True}, owner,
                                     {'diet': 'vegan', 'avoid_coriander': False})
    assert data['history_id']
    history = signed.get('/api/v1/ingredients/history').json()
    assert any(h['id'] == data['history_id'] for h in history)
    assert card['payload']['history_id'] == data['history_id']


# ================= 地区提取：数量/目的词不当地区 =================
@pytest.mark.parametrize('text', ['推荐一家素食餐厅', '推荐两家素食', '推荐适合聚餐的素食餐厅'])
def test_count_and_purpose_words_are_not_area(text):
    assert agent._extract_area(text) is None


def test_real_area_still_extracted():
    assert agent._extract_area('厦门有什么素食餐厅') == '厦门'
    assert agent._extract_area('那就找北京的') == '北京'


# ================= 发布准备清单（只读） =================
def test_release_manifest_is_generated_without_publishing(signed, monkeypatch):
    from scripts.build_release_manifest import classify
    assert classify(entry('含肉', ingredients=['牛肉'])) == 'excluded'
    assert classify(entry('待补', ingredients=['素蚝油'], steps=['炒'])) == 'needs_ingredients'
    assert classify(entry('可参考', ingredients=['豆腐'], steps=['炒'],
                          conditions={'source_id': 'x'})) == 'reference_ready'

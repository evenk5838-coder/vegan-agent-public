"""W05 integration regressions use synthetic source identifiers in temporary fixtures.

Fixture status is not approval to publish the original, machine translation or
any business record. No live model/provider is invoked.
"""
import copy
import json
from pathlib import Path

import pytest

from app import core, knowledge
from app.recipes import normalize_filters
from app.services import chat_reads, recipe_query

FIXTURE = (Path(__file__).resolve().parents[2]
           / 'backend/tests/fixtures/recall-synthetic.json')


def insert_fixture(entry, *, status='published', owner='system', kind='knowledge'):
    payload = {**knowledge.validate_entry(copy.deepcopy(entry)), 'entry_id': core.uid(),
               'status': status, 'evaluation_fixture_only': True}
    # This exercises the existing public read projection for nonrecipe notes,
    # not the recipe-only import-field schema.
    if entry.get('type') != 'recipe' and entry.get('nutrition_notes'):
        payload['nutrition_notes'] = entry['nutrition_notes']
    with core.DB() as db:
        db.add(core.Record(owner=owner, kind=kind, payload=payload))
        db.commit()
    return payload['entry_id']


@pytest.mark.parametrize('source_id,query', [
    ('wikibooks:106077', '蔓越莓汁'), ('wikibooks:102176', '红花油'),
    ('wikibooks:106987', '青柠汁'), ('wikibooks:10049', '冷藏后食用'),
    ('wikibooks:101881', '咖啡研磨机'), ('wikibooks:106905', '食品加工机'),
])
def test_real_ingredient_and_step_recall_agrees_between_page_and_agent(client, source_id, query):
    fixture = json.loads(FIXTURE.read_text())
    sample = next(row for row in fixture['samples'] if row['pin']['source_id'] == source_id)
    entry_id = insert_fixture(sample['entry'])
    page = recipe_query.query_recipes('fixture-owner', normalize_filters(), {}, query)['items']
    tool = chat_reads.knowledge_search(query, 'recipe')['items']
    assert [row['entry_id'] for row in page] == [entry_id]
    assert [row['entry_id'] for row in tool] == [entry_id]
    assert query in tool[0]['excerpt']
    assert tool[0]['source'] == sample['pin']['source_url']
    assert tool[0]['excerpt'].strip() and len(tool[0]['excerpt']) <= 300


def test_source_and_public_applicability_share_recall_but_audit_hashes_do_not(client):
    entry = {'type': 'recipe', 'title': '来源适用范围样例', 'source': 'https://example.org/HowToCook',
             'ingredients': ['豆腐'], 'steps': ['加热。'],
             'conditions': {'applicability': '适用于家庭冷藏豆制品',
                            'machine_rules_sha256': 'internal-audit-needle-only'}}
    entry_id = insert_fixture(entry)
    for query in ('HowToCook', '家庭冷藏豆制品'):
        assert [row['entry_id'] for row in recipe_query.query_recipes(
            'fixture-owner', normalize_filters(), {}, query)['items']] == [entry_id]
        tool = chat_reads.knowledge_search(query, 'recipe')['items']
        assert [row['entry_id'] for row in tool] == [entry_id]
        assert query in tool[0]['excerpt']
    assert chat_reads.knowledge_search('internal-audit-needle-only', 'recipe')['items'] == []


def test_nonrecipe_body_nutrition_and_conditions_remain_searchable(client):
    entry = {'type': 'nutrition', 'title': '营养来源样例', 'source': 'https://example.org/reference',
             'body': '来源正文包含膳食纤维说明。', 'nutrition_notes': '本条保留植物蛋白的来源说明。',
             'conditions': {'applicability': '适用于一般成年人'}}
    entry_id = insert_fixture(entry)
    for query in ('膳食纤维', '植物蛋白', '一般成年人'):
        rows = chat_reads.knowledge_search(query, 'nutrition')['items']
        assert [row['entry_id'] for row in rows] == [entry_id]
        assert query in rows[0]['excerpt']


def test_shared_recall_does_not_expose_drafts_or_other_owner_knowledge(client):
    entry = {'type': 'recipe', 'title': '隐藏菜谱', 'source': 'https://example.org/test',
             'ingredients': ['蔓越莓汁'], 'steps': ['拌匀。']}
    insert_fixture(entry, status='draft')
    insert_fixture(entry, owner='another-fixture-user')
    assert chat_reads.knowledge_search('蔓越莓汁', 'recipe')['items'] == []
    assert recipe_query.query_recipes('fixture-owner', normalize_filters(), {}, '蔓越莓汁')['items'] == []


def test_multi_token_recall_uses_same_ranking_for_page_and_knowledge_tool(client):
    fixture = json.loads(FIXTURE.read_text())
    source_ids = ('wikibooks:10049', 'wikibooks:102176')
    for source_id in source_ids:
        sample = next(row for row in fixture['samples'] if row['pin']['source_id'] == source_id)
        insert_fixture(sample['entry'])
    page = recipe_query.query_recipes('fixture-owner', normalize_filters(), {}, '蜂蜜 芥末')['items']
    tool = chat_reads.knowledge_search('蜂蜜 芥末', 'recipe')['items']
    assert [row['entry_id'] for row in tool] == [row['entry_id'] for row in page]
    assert tool[0]['title'] == '蜂蜜芥末沙拉酱'
    # The current documented OR recall remains broad; this regression does not
    # relabel it as strict AND filtering or semantic relevance certification.
    assert len(tool) == 2

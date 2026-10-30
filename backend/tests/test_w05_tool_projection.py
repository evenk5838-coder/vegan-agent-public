"""Public source facts reach the model; import/rule audit metadata stays in DB.

All published rows below are temporary fixtures. They grant no publication
approval, and the real paid W05 receipts remain immutable historical evidence.
"""
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import core, knowledge
from app.services import chat_reads, recipe_query

FIXTURE = (Path(__file__).resolve().parents[2]
           / 'backend/tests/fixtures/recall-synthetic.json')


def publish_fixture(entry):
    entry_id = core.uid()
    payload = {**knowledge.validate_entry(copy.deepcopy(entry)), 'entry_id': entry_id,
               'status': 'published', 'evaluation_fixture_only': True}
    with core.DB() as db:
        db.add(core.Record(owner='system', kind='knowledge', payload=payload))
        db.commit()
    return entry_id


def test_synthetic_source_tool_excludes_internal_metadata_and_parser_tokens(client):
    samples = json.loads(FIXTURE.read_text())['samples']
    sample = next(row for row in samples if row['pin']['source_id'] == 'wikibooks:101881')
    entry_id = publish_fixture(sample['entry'])

    data = chat_reads.knowledge_search('糖渍橙皮', 'recipe')
    assert data['count'] == 1
    item = data['items'][0]
    assert item['entry_id'] == entry_id and item['source'] == sample['pin']['source_url']
    assert item['title'] == '糖渍橙皮'
    assert item['conditions'] == {}
    for forbidden in ('source_id', 'source_revision', 'manual_reviewed', 'machine_diet_labels',
                      'machine_rules_sha256', 'canonical_ingredients', 'unwaxed',
                      'sevilles are good', 'twice'):
        assert forbidden not in json.dumps(item, ensure_ascii=False)
    assert '尚未经过人工审核' in item['source_notice']
    assert '尚未人工核对的机器翻译' in item['source_notice']
    assert '适配信息仍待核对' in item['source_notice']
    assert '来源未提供份量信息' in item['source_notice']
    assert '90克' in item['excerpt'] and '4小时' in item['excerpt'] and '48小时' in item['excerpt']
    assert '咖啡研磨机' in item['excerpt']

    # Removing metadata from the tool projection must not erase source audit.
    with core.DB() as db:
        row = db.scalars(select(core.Record).where(core.Record.kind == 'knowledge')).one()
        assert row.payload['conditions']['source_id'] == 'wikibooks:101881'
        assert row.payload['conditions']['manual_reviewed'] is False
        assert row.payload['conditions']['machine_diet_labels']['vegan']['unknown']
        assert row.payload['source_meta']['translation']['status'] == 'complete'


def test_storage_applicability_and_exact_temperature_survive_public_projection(client):
    conditions = {'applicability': '仅适用于蔬菜汤和蔬菜炖菜', 'food_class': 'cooked_food',
                  'storage': 'refrigerated', 'condition': 'cooked', 'date_type': 'prepared',
                  'temperature_min_c': 0, 'temperature_max_c': 4,
                  'temperature': {'unit': '摄氏度', 'maximum': 4},
                  'warnings': ['须满足冷藏条件'], 'usage_notes': '不能泛化为所有炒菜',
                  'source_id': 'private-import-key', 'source_revision': 1234,
                  'input_sha256': 'private-import-hash', 'manual_reviewed': False,
                  'canonical_ingredients': ['private-parser-token']}
    entry_id = publish_fixture({'type': 'storage', 'title': '蔬菜汤冷藏条件',
                                'source': 'https://example.org/pinned-storage-reference',
                                'body': '仅在明确适用条件下使用该来源。', 'conditions': conditions})
    item = chat_reads.knowledge_search('蔬菜汤', 'storage')['items'][0]
    expected = {key: value for key, value in conditions.items()
                if key in recipe_query.CONDITION_SEARCH_FIELDS}
    assert item['conditions'] == expected
    assert item['conditions']['temperature_min_c'] == 0
    assert item['conditions']['temperature_max_c'] == 4
    assert item['conditions']['date_type'] == 'prepared'
    assert item['entry_id'] == entry_id
    assert item['source'] == 'https://example.org/pinned-storage-reference'
    assert '蔬菜汤' in item['excerpt']
    assert 'private-' not in json.dumps(item, ensure_ascii=False)
    assert '尚未经过人工审核' in item['source_notice']


@pytest.mark.parametrize('metadata,expected,absent', [
    ({'manual_reviewed': False}, '尚未经过人工审核', '机器翻译'),
    ({'manual_reviewed': True}, '', '尚未经过人工审核'),
    ({}, '', '已通过'),
    ({'translation': {'status': 'complete', 'validation': {
        'semantic_quality_manually_reviewed': False}}}, '尚未人工核对的机器翻译', '已通过'),
    ({'machine_diet_labels': {'vegan': {'status': 'uncertain', 'unknown': ['opaque-token']}}},
     '适配信息仍待核对', 'opaque-token'),
])
def test_source_notices_preserve_only_recorded_limits_in_chinese(metadata, expected, absent):
    notice = chat_reads._knowledge_source_notice({'conditions': {}, 'source_meta': metadata})
    if expected:
        assert expected in notice
    else:
        assert notice == ''
    assert absent not in notice


def test_explicit_source_metadata_overrides_legacy_condition_review_flag():
    notice = chat_reads._knowledge_source_notice({'conditions': {'manual_reviewed': False},
                                                 'source_meta': {'manual_reviewed': True}})
    assert notice == ''

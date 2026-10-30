"""Public recipe tutorials must survive import intact or be explicitly rejected.

All database writes use the isolated test fixture. Frozen source artifacts are
read-only; these tests do not publish recipes or rebuild the source corpus.
"""
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import core, knowledge
from scripts import build_recipe_corpus as corpus_builder
from scripts.build_recipe_corpus import make_entry, recipe_field_limit_errors



def recipe(**changes):
    value = {'type': 'recipe', 'title': '测试菜谱', 'source': 'https://example.org/source',
             'status': 'draft', 'ingredients': ['豆腐 200g'], 'steps': ['煮熟。'],
             'notes': [], 'body': '', 'conditions': {'source_id': 'test:limits'}}
    return {**value, **changes}


def source_row(**changes):
    value = {'title': '测试菜谱', 'source_id': 'test:limits', 'source': 'https://example.org/source',
             'ingredients': ['豆腐 200g'], 'steps': ['煮熟。'], 'notes': [], 'body': '',
             'provider': 'test-source', 'source_language': 'zh-CN',
             'structural_complete': True, 'quality_flags': [],
             'diet_assessment': {'labels': {}, 'rules_version': 'test-only',
                                 'rules_source_sha256': 'test-only', 'canonical_ingredients': ['豆腐']}}
    return {**value, **changes}


@pytest.mark.parametrize('count', [49, 55, 100])
def test_long_tutorial_import_preserves_every_step_and_draft_state(client, count):
    steps = [f'第{index + 1}步：保留此处完整的做法与注意事项。' for index in range(count)]
    result = knowledge.import_entries([recipe(steps=steps)])
    assert result['counts']['added'] == 1
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
        assert row.payload['steps'] == steps
        assert row.payload['steps'][-1] == steps[-1]
        assert row.payload['status'] == 'draft'
        assert row.payload['source_meta']['manual_reviewed'] is False
    assert knowledge.published_recipes() == []


def test_101_steps_are_rejected_without_creating_partial_tutorial(client):
    steps = [f'步骤{index}' for index in range(101)]
    report = knowledge.import_entries([recipe(steps=steps)])
    assert report['counts']['rejected'] == 1
    assert report['results'][0]['error']['code'] == 'INVALID_ENTRY'
    assert 'steps' in report['results'][0]['error']['message']
    assert '100' in report['results'][0]['error']['message']
    with core.DB() as db:
        assert list(db.scalars(select(core.Record).where(core.Record.kind == 'knowledge'))) == []


@pytest.mark.parametrize('field,value', [
    ('ingredients', ['豆腐'] * 61), ('notes', ['来源注意事项'] * 41),
    ('title', '菜' * 201), ('body', '文' * 8001),
    ('servings_text', '份' * 801), ('nutrition_notes', '说明' * 1001),
], ids=['ingredients', 'notes', 'title', 'body', 'servings_text', 'nutrition_notes'])
def test_recipe_projection_rejects_oversized_content_instead_of_slicing(field, value):
    with pytest.raises(core.AppError) as error:
        knowledge.validate_entry(recipe(**{field: value}))
    assert error.value.code == 'INVALID_ENTRY'
    assert field in error.value.message


def test_exact_recipe_field_boundaries_keep_all_source_content():
    entry = recipe(title='菜' * 200, body='文' * 8000,
                   ingredients=['原料'] * 60, steps=['做法'] * 100,
                   notes=['注意事项'] * 40, servings_text='份' * 800,
                   nutrition_notes='说' * 2000)
    clean = knowledge.validate_entry(entry)
    for field in ('title', 'body', 'ingredients', 'steps', 'notes', 'servings_text', 'nutrition_notes'):
        assert clean[field] == entry[field]


@pytest.mark.parametrize('field,value', [
    ('steps', ['做法'] * 101), ('ingredients', ['豆腐'] * 61),
    ('notes', ['来源注意事项'] * 41), ('title', '菜' * 201), ('body', '文' * 8001),
], ids=['steps', 'ingredients', 'notes', 'title', 'body'])
def test_corpus_projection_explicitly_rejects_oversized_sources(field, value):
    row = source_row(**{field: value})
    assert any(error.startswith(f'{field}:') for error in recipe_field_limit_errors(row))
    with pytest.raises(ValueError, match='recipe_field_limit_exceeded'):
        make_entry(row)


@pytest.mark.parametrize('count,eligible', [(49, True), (55, True), (101, False)])
def test_future_corpus_build_keeps_full_text_and_quarantines_only_true_overflow(tmp_path, monkeypatch,
                                                                            count, eligible):
    steps = [f'完整步骤{index}' for index in range(count)]
    monkeypatch.setattr(corpus_builder, 'sources', lambda: iter([source_row(steps=steps)]))
    output = tmp_path / 'source-build'
    corpus_builder.build(output)
    row = json.loads((output / 'corpus.jsonl').read_text())
    assert row['steps'] == steps
    assert row['review_candidate_eligible'] is eligible
    batch = json.loads((output / 'knowledge-import-draft.json').read_text())
    if eligible:
        assert row['structural_complete'] is True
        assert batch['entries'][0]['steps'] == steps
    else:
        assert row['structural_complete'] is False
        assert row['structured_field_limit_errors'] == ['steps:101>100']
        assert 'exceeds_current_app_structured_field_limits' in row['quality_flags']
        assert batch['entries'] == []


@pytest.mark.parametrize('title,count', [('合成测试甲', 55), ('合成测试乙', 49)])
def test_frozen_howtocook_tutorials_preserve_full_steps_and_source_metadata(title, count):
    row = source_row(title='测试长教程', steps=[f'测试步骤{i}' for i in range(count)], source_revision='test-revision', body='完整测试正文')
    original = copy.deepcopy(row)
    entry = make_entry(row)
    clean = knowledge.validate_entry(entry)
    assert len(clean['steps']) == count
    assert clean['steps'] == original['steps']
    assert clean['ingredients'] == original['ingredients']
    assert clean['body'] == original['body']
    assert clean['conditions']['source_id'] == original['source_id']
    assert clean['conditions']['source_revision'] == original['source_revision']
    assert clean['source_meta']['machine_rules_sha256'] == original['diet_assessment']['rules_source_sha256']
    assert clean['source_meta']['machine_diet_labels'] == original['diet_assessment']['labels']
    assert clean['source_meta']['quality_flags'] == original['quality_flags']
    assert clean['status'] == 'draft'
    assert row == original

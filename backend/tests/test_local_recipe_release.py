"""The authorized local release must preserve review flags and other records."""
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from app import core, knowledge
from scripts.release_translated_recipes import prepare, release


def sample():
    return {'type': 'recipe', 'title': '测试中文配方', 'source': 'https://en.wikibooks.org/wiki/Test',
            'ingredients': ['胡萝卜 100g'], 'steps': ['测试搅拌步骤'],
            'source_meta': {'source_id': 'wikibooks:test-only', 'source_revision': 1,
                'quality_flags': ['machine_translation_unreviewed'],
                'license': {'id': 'CC-BY-SA-4.0', 'attribution': 'Synthetic test', 'url': 'https://example.org/license'},
                'original_content': {'ingredients': ['100g carrot'], 'steps': ['Synthetic test step']},
                'translation': {'status': 'complete', 'validation': {k: True for k in (
                    'strict_schema', 'identities_match', 'numeric_values_match', 'quantity_unit_pairs_match',
                    'known_dangerous_terms_preserved', 'ingredients_steps_lengths_match')}}}}


def test_screen_steps_original_and_unknown_flags_without_relabeling():
    entry = sample()
    batch = prepare([entry])
    assert len(batch['accepted']) == 1
    meta = batch['accepted'][0]['source_meta']
    assert meta['manual_reviewed'] is False and meta['translation_reviewed'] is False
    assert 'machine_translation_unreviewed' in meta['quality_flags']
    for field, value in [('steps', ['加入蚝油后搅拌。']), ('ingredients', ['1磅鳕鱼'])]:
        bad = copy.deepcopy(entry); bad[field] = value
        assert prepare([bad])['held'][0]['animal_wording']
    bad = copy.deepcopy(entry); bad['source_meta']['original_content']['steps'] = ['Add veal.']
    assert prepare([bad])['held'][0]['animal_wording']
    bad = copy.deepcopy(entry); bad['source_meta']['quality_flags'].append('source_incomplete')
    assert 'blocking_quality_flags' in prepare([bad])['held'][0]['reasons']
    bad = copy.deepcopy(entry); bad['ingredients'] = ['胡萝卜']
    assert 'missing_core_information_or_quantity' in prepare([bad])['held'][0]['reasons']


def test_plant_names_do_not_trip_specific_animal_screen():
    from scripts.release_translated_recipes import ANIMAL_WORDS
    for text in ['牛油果', 'oyster mushrooms', '肉豆蔻', 'cocoa butter', 'butter beans']:
        assert not ANIMAL_WORDS.search(text)


def test_local_release_atomic_backup_protected_records_and_idempotence(signed, tmp_path, monkeypatch):
    monkeypatch.setattr(core.settings, 'app_environment', 'local-development')
    entry = sample()
    knowledge.import_entries([entry])
    private = core.Record(owner='alice-id', kind='recipe', payload={'title': '私人菜谱'})
    with core.DB() as db:
        db.add(private); db.commit()
    batch = prepare([entry])
    plan = release(batch, authorization='用户明确批准本地展示')
    assert plan['counts']['updated_existing'] == 1 and not knowledge.published_recipes()
    with pytest.raises(ValueError, match='changed'):
        release(batch, authorization='批准', expected_db_sha='wrong', backup_path=tmp_path/'never.json')
    assert not (tmp_path/'never.json').exists()
    report = release(batch, authorization='批准', expected_db_sha=plan['db_sha256'], backup_path=tmp_path/'backup.json')
    assert report['counts']['updated_existing'] == 1
    assert json.loads((tmp_path/'backup.json').read_text())['records'][0]['payload']['status'] == 'draft'
    public = knowledge.published_recipes()
    assert len(public) == 1 and public[0]['ingredients'] == entry['ingredients']
    meta = public[0]['source_meta']
    assert not meta['manual_reviewed'] and 'machine_translation_unreviewed' in meta['quality_flags']
    assert meta['local_beta_release']['scope'] == 'local_internal_test'
    assert release(batch, authorization='批准')['counts']['already_visible_or_protected'] == 1
    with core.DB() as db:
        assert db.get(core.Record, private.id).payload == {'title': '私人菜谱'}


def test_unapproved_or_nonlocal_environment_cannot_release(signed, monkeypatch):
    batch = prepare([sample()])
    monkeypatch.setattr(core.settings, 'app_environment', 'production')
    with pytest.raises(ValueError, match='local'):
        release(batch, authorization='批准')
    monkeypatch.setattr(core.settings, 'app_environment', 'local-development')
    with pytest.raises(ValueError, match='authorization'):
        release(batch, authorization=' ')

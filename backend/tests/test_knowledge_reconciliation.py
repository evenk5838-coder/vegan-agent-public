"""Frozen batch reconciliation and release preparation use isolated fixtures only."""
import copy
import hashlib
import json

import pytest
from sqlalchemy import delete, select
from app import core, knowledge
from app.rules import VERSION
from scripts.build_release_manifest import assess, build_manifest, classify
from scripts.reconcile_knowledge_batch import (apply_reconciliation, plan_reconciliation,
                                                read_batch, snapshot_sha256)


@pytest.fixture(autouse=True)
def isolated_knowledge_batch(signed):
    yield
    with core.DB() as db:
        db.execute(delete(core.Record).where(core.Record.kind == 'knowledge'))
        db.commit()


def entry(sid='source:1', **changes):
    row = {'type': 'recipe', 'title': '同名菜谱', 'source': 'https://example.org/recipe',
           'ingredients': ['豆腐 200g', '水 20ml'], 'steps': ['放入锅中煮熟。'],
           'status': 'draft', 'conditions': {'source_id': sid, 'source_revision': 1,
                                           'language': 'zh-CN', 'machine_rules_version': VERSION,
                                           'license': {'id': 'Unlicense'}}}
    row.update(changes)
    return row


def record(rid, payload, owner='system', kind='knowledge'):
    return {'id': rid, 'owner': owner, 'kind': kind, 'payload': payload}


def corpus():
    return {'source:1': {'review_candidate_eligible': True},
            'source:2': {'review_candidate_eligible': False,
                         'explicit_animal_evidence': ['fish sauce']}}


def clean():
    row = knowledge.validate_entry(entry())
    row['conditions'].update(input_sha256='batch-hash', review_eligibility='candidate')
    row['source_meta'].update(input_sha256='batch-hash', review_eligibility='candidate')
    return row


def test_import_file_cannot_publish_or_assert_manual_review(signed):
    row = entry(status='published', conditions={'source_id': 'source:1', 'manual_reviewed': True})
    knowledge.import_entries([row])
    with core.DB() as db:
        payload = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge')).payload
    assert payload['status'] == 'draft'
    assert payload['source_meta']['manual_reviewed'] is False
    assert payload['conditions']['manual_reviewed'] is False
    assert knowledge.public_items()['items'] == []


def test_different_sources_same_title_both_imported(signed):
    report = knowledge.import_entries([entry('source:1'), entry('source:2')])
    assert report['counts']['added'] == 2


def test_revisions_in_one_batch_do_not_create_duplicate_source(signed):
    second = entry(conditions={'source_id': 'source:1', 'source_revision': 2})
    report = knowledge.import_entries([entry(), second])
    assert report['counts']['added'] == 1 and report['counts']['updated'] == 1
    with core.DB() as db:
        rows = db.scalars(select(core.Record).where(core.Record.kind == 'knowledge')).all()
        assert len(rows) == 1
        assert rows[0].payload['source_meta']['source_revision'] == 2


def test_any_reviewed_revision_protects_duplicate_draft_source():
    reviewed = knowledge.draft_payload(knowledge.validate_entry(entry()))
    reviewed['status'] = 'reviewed'
    draft = knowledge.draft_payload(knowledge.validate_entry(entry(
        conditions={**entry()['conditions'], 'source_revision': 2})))
    with core.DB() as db:
        db.add_all([core.Record(owner='system', kind='knowledge', payload=reviewed),
                    core.Record(owner='system', kind='knowledge', payload=draft)])
        db.commit()
    result = knowledge.import_entries([entry(ingredients=['牛肉'],
        conditions={**entry()['conditions'], 'source_revision': 2})])
    assert result['results'][0]['reason'] == 'already_reviewed_or_published'


def test_title_fallback_dedupes_within_batch(signed):
    row = entry(conditions={})
    report = knowledge.import_entries([row, row])
    assert report['counts']['added'] == 1


def test_import_preserves_rules_hash_translation_and_audit(signed):
    conditions = {**entry()['conditions'], 'machine_rules_version': 'old-version',
                  'canonical_ingredients': ['old-output'], 'corpus_sha256': 'c' * 64,
                  'input_sha256': 'i' * 64, 'display_language': 'zh-CN',
                  'original_content': {'title': 'Tofu', 'ingredients': ['200g tofu']},
                  'translation_quality_flags': ['units_pending']}
    knowledge.import_entries([entry(conditions=conditions)])
    with core.DB() as db:
        first = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge')).payload
    knowledge.import_entries([entry(conditions=conditions)])
    with core.DB() as db:
        second = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge')).payload
    assert second['entry_id'] == first['entry_id']
    assert second['source_meta']['machine_rules_version'] == 'old-version'
    assert second['source_meta']['original_content']['title'] == 'Tofu'
    assert second['source_meta']['translation_quality_flags'] == ['units_pending']
    assert len(second['import_history']) == 2


@pytest.mark.parametrize('status', ['reviewed', 'published', 'archived'])
def test_reconcile_protects_reviewed_and_private_rows(status):
    kept = knowledge.draft_payload(clean())
    kept['status'] = status
    wrong = copy.deepcopy(kept)
    wrong['status'] = 'draft'
    rows = [record('protected', kept), record('private', wrong, owner='user-1'),
            record('recipe', wrong, kind='recipe')]
    before = copy.deepcopy(rows)
    report, changes = plan_reconciliation(rows, [clean()], corpus(), {'input_sha256': 'batch-hash'})
    assert changes == []
    assert report['protected_count'] == 1
    assert report['unmanaged_untouched_count'] == 2
    assert rows == before


def test_reconcile_quarantines_only_managed_old_drafts_without_deletion():
    old = knowledge.draft_payload(knowledge.validate_entry(entry('source:2')))
    old['status_history'] = [{'from': None, 'to': 'draft'}]
    private = copy.deepcopy(old)
    rows = [record('old', old), record('private', private, owner='alice'),
            record('unrelated', knowledge.draft_payload(knowledge.validate_entry(entry('other:3'))))]
    report, changes = plan_reconciliation(rows, [clean()], corpus(), {'input_sha256': 'batch-hash'})
    assert report['counts'] == {'add': 1, 'quarantine': 1}
    quarantine = next(c for c in changes if c['action'] == 'quarantine')
    assert quarantine['payload']['ingredients'] == old['ingredients']
    assert quarantine['payload']['entry_id'] == old['entry_id']
    assert quarantine['payload']['status'] == 'draft'
    assert quarantine['payload']['source_meta']['reconciliation']['source_exclusion_evidence'] == ['fish sauce']
    assert len(quarantine['payload']['status_history']) == 2
    assert report['unmanaged_untouched_count'] == 2
    assert not report['publication_changed']


def test_reconcile_second_run_has_no_changes():
    report, changes = plan_reconciliation([], [clean()], corpus(), {'input_sha256': 'batch-hash'})
    rows = [record('new', changes[0]['payload'])]
    report2, changes2 = plan_reconciliation(rows, [clean()], corpus(), {'input_sha256': 'batch-hash'})
    assert changes2 == []


def test_reconcile_duplicate_of_published_source_is_quarantined():
    published = knowledge.draft_payload(clean())
    published['status'] = 'published'
    draft = knowledge.draft_payload(clean())
    report, changes = plan_reconciliation([record('p', published), record('d', draft)],
                                           [clean()], corpus(), {'input_sha256': 'batch-hash'})
    assert report['protected_count'] == 1
    assert len(changes) == 1 and changes[0]['action'] == 'quarantine'
    assert changes[0]['payload']['source_meta']['reconciliation']['reason'] == 'duplicate_source_of_protected_record'


def test_apply_writes_in_one_test_transaction_and_never_publishes(signed):
    _, changes = plan_reconciliation([], [clean()], corpus(), {'input_sha256': 'batch-hash'})
    with core.DB() as db:
        apply_reconciliation(db, changes)
        db.commit()
    assert knowledge.public_items()['items'] == []
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
        knowledge.set_status(row.payload['entry_id'], 'reviewed')
    mutation = {'action': 'quarantine', 'record_id': row.id, 'payload': {}}
    with core.DB() as db:
        with pytest.raises(ValueError, match='review status changed'):
            apply_reconciliation(db, [mutation])


def test_snapshot_is_order_independent_but_catches_review_change():
    rows = [record('a', {'status': 'draft'}), record('b', {'status': 'draft'})]
    before = snapshot_sha256(rows)
    assert before == snapshot_sha256(list(reversed(rows)))
    rows[0]['payload']['status'] = 'reviewed'
    assert snapshot_sha256(rows) != before


def test_batch_rejects_outside_or_duplicate_candidates_before_db(tmp_path):
    batch, source = tmp_path / 'batch.json', tmp_path / 'corpus.jsonl'
    source.write_text(json.dumps({'source_id': 'source:1', 'review_candidate_eligible': True,
                                  'diet_assessment': {'rules_version': VERSION}}))
    batch.write_text(json.dumps({'entries': [entry(), entry()]}))
    with pytest.raises(ValueError, match='duplicate'):
        read_batch(batch, source)
    batch.write_text(json.dumps({'entries': [entry('other:1')]}))
    with pytest.raises(ValueError, match='out-of-corpus'):
        read_batch(batch, source)


def test_batch_requires_exact_candidate_membership_and_hashes(tmp_path):
    batch, source = tmp_path / 'batch.json', tmp_path / 'corpus.jsonl'
    source.write_text(json.dumps({'source_id': 'source:1', 'review_candidate_eligible': True,
                                  'diet_assessment': {'rules_version': VERSION}}))
    batch.write_text(json.dumps({'entries': []}))
    with pytest.raises(ValueError, match='candidate mismatch'):
        read_batch(batch, source)
    batch.write_text(json.dumps({'entries': [entry()]}))
    entries, _, metadata = read_batch(batch, source)
    assert metadata['input_sha256'] == hashlib.sha256(batch.read_bytes()).hexdigest()
    assert metadata['corpus_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert entries[0]['source_meta']['input_sha256'] == metadata['input_sha256']


def test_lacto_ovo_suitable_not_excluded_because_vegan_denies_egg():
    result = assess(entry(ingredients=['鸡蛋 2 个', '水 10ml']))
    assert result['bucket'] == 'reference_ready'
    assert result['diet_labels']['vegan']['status'] == 'not_suitable'
    assert result['diet_labels']['pure']['status'] == 'not_suitable'
    assert result['diet_labels']['lacto_ovo']['status'] == 'suitable'
    assert result['eligible_diets'] == ['lacto_ovo']


def test_quality_gate_and_rule_labels_are_separate():
    row = entry(conditions={'language': 'zh-CN', 'quality_flags': ['missing_servings']})
    result = assess(row)
    assert result['bucket'] == 'special_review'
    assert result['diet_labels']['vegan']['status'] == 'suitable'
    assert not result['quality']['manual_reviewed']


def test_stale_machine_label_is_not_reused():
    row = entry(ingredients=['牛肉'], conditions={'language': 'zh-CN', 'machine_rules_version': 'old',
                'machine_diet_labels': {'vegan': {'status': 'suitable'}}})
    result = assess(row)
    assert result['bucket'] == 'excluded'
    assert result['rules_version'] == VERSION
    assert not result['stored_machine_labels_current']
    assert result['labels_recomputed_from_source']


def test_foreign_recipe_needs_chinese_display_before_release():
    row = entry(conditions={'language': 'en'})
    result = assess(row)
    assert result['bucket'] == 'special_review'
    assert not result['quality']['chinese_display_ready']
    row['conditions']['display_language'] = 'zh-CN'
    assert assess(row)['quality']['chinese_display_ready']
    row['conditions']['translation_quality_flags'] = ['ingredient_units_unverified']
    assert classify(row) == 'special_review'


def test_chinese_language_flag_does_not_approve_untranslated_recipe():
    row = entry(title='Tofu soup', ingredients=['200g tofu'], steps=['Boil for ten minutes.'],
                conditions={'language': 'en', 'display_language': 'zh-CN'})
    assert classify(row) == 'special_review'
    assert not assess(row)['quality']['chinese_display_ready']
    knowledge.import_entries([row])
    with core.DB() as db:
        stored = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
    with pytest.raises(knowledge.AppError, match='中文教程未就绪'):
        knowledge.set_status(stored.payload['entry_id'], 'published')


def test_manifest_tracks_current_rules_input_hash_and_independent_counts():
    result = build_manifest([entry(ingredients=['鸡蛋'])], input_sha256='frozen-hash', generated_from='test-fixture')
    assert result['rules_version'] == VERSION
    assert result['input_sha256'] == 'frozen-hash'
    assert result['diet_counts']['vegan']['not_suitable'] == 1
    assert result['diet_counts']['lacto_ovo']['suitable'] == 1
    assert not result['publication_changed'] and not result['database_written']


@pytest.mark.parametrize('conditions', [
    {'language': 'en'},
    {'language': 'en', 'display_language': 'zh-CN', 'translation_quality_flags': ['missing_steps']},
    {'language': 'zh-CN', 'review_eligibility': 'quarantined'},
    {'language': 'en', 'display_language': 'zh-CN', 'translation': {'status': 'needs_review'}},
])
def test_unready_translation_or_quarantine_cannot_be_published(conditions):
    knowledge.import_entries([entry(conditions={**entry()['conditions'], **conditions})])
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.kind == 'knowledge'))
    with pytest.raises(knowledge.AppError) as exc:
        knowledge.set_status(row.payload['entry_id'], 'published')
    assert exc.value.code == 'CONTENT_NOT_READY'
    assert not knowledge.public_items()['items']


def test_recipe_view_preserves_animal_subingredients_in_parentheses():
    row = knowledge.validate_entry(entry(ingredients=['植物鸡 200g（牛奶、鸡蛋）', '豆腐 20g']))
    view = knowledge.recipe_view({**row, 'entry_id': 'test'})
    assert view['evaluation']['results']['vegan']['status'] == 'not_suitable'
    assert '牛奶' in view['rule_ingredients'] and '鸡蛋' in view['rule_ingredients']


def test_translation_cannot_hide_original_animal_or_pungent_evidence():
    row = knowledge.validate_entry(entry(ingredients=['豆腐 200g'],
                source_meta={'original_content': {'ingredients': ['tofu with beef and garlic']},
                             'display_language': 'zh-CN', 'language': 'en'}))
    view = knowledge.recipe_view({**row, 'entry_id': 'test-translation'})
    assert view['evaluation']['results']['vegan']['status'] == 'not_suitable'
    assert view['evaluation']['results']['pure']['status'] == 'not_suitable'
    assert view['original_ingredients'] == ['tofu with beef and garlic']
    assert assess(row)['bucket'] == 'excluded'


def test_release_uses_raw_source_not_stale_canonical_text():
    row = entry(ingredients=['flour and beef'], conditions={'canonical_ingredients': ['面粉'],
                'machine_rules_version': 'old', 'language': 'zh-CN'})
    assert classify(row) == 'excluded'


def test_batch_refuses_to_label_older_rule_batch_as_current(tmp_path):
    batch, source = tmp_path / 'batch.json', tmp_path / 'corpus.jsonl'
    source.write_text(json.dumps({'source_id': 'source:1', 'review_candidate_eligible': True,
                                  'diet_assessment': {'rules_version': 'old'}}))
    batch.write_text(json.dumps({'entries': [entry()]}))
    with pytest.raises(ValueError, match='stale or missing rule version'):
        read_batch(batch, source)


def test_howtocook_review_batch_has_twenty_real_source_audits():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    packet = json.loads((root / 'docs/知识库/保留菜谱20道-2026-10-28/20道-原始来源核对.json').read_text())
    assert packet['count'] == 20 and packet['published'] == 0 and packet['manual_reviewed'] == 0
    assert len({e['conditions']['source_id'] for e in packet['entries']}) == 20
    for row in packet['entries']:
        conditions = row['conditions']
        path = root / 'docs/知识库/保留菜谱20道-2026-10-28/source-text' / conditions['source_id'].removeprefix('howtocook:')
        assert hashlib.sha256(path.read_bytes()).hexdigest() == conditions['reviewed_source_sha256']
        assert conditions['original_content']['markdown'] == path.read_text()
        assert row['ingredients'] and row['steps'] and row['source'].startswith('https://github.com/')
        assert row['status'] == 'draft' and conditions['publication_approved'] is False
        assert conditions['ai_source_review'] and conditions['manual_reviewed'] is False
        assert conditions['display_language'] == 'zh-CN' and conditions['license']['id'] == 'Unlicense'

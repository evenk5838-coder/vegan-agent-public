"""Translation writeback uses synthetic proof and isolated PostgreSQL only."""
import builtins
import copy
from datetime import datetime, timedelta, timezone
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app import core, knowledge
from scripts import translate_recipe_corpus as tr
from scripts import import_recipe_translations as tool


def original_recipe():
    return {'source_id': 'test:rice', 'source_revision': 7, 'source_language': 'en',
                'review_candidate_eligible': True, 'source': 'https://example.org/rice',
                'source_license': {'id': 'CC-BY-SA-4.0'}, 'title': 'Rice',
                'ingredients': ['1 cup rice'], 'steps': ['Cook rice for 10 minutes.'],
                'servings': '2 servings', 'body': 'Serve warm.', 'notes': ['Use clean water.'],
                'review_sections': [{'heading': 'Notes', 'path': ['Notes'], 'kind': 'notes', 'text': 'Serve warm.'}],
                'quality_flags': ['source_parser_artifact_unreviewed']}


def make_translated(tmp_path, originals=None, translations=None, annotation_path=None):
    originals = originals or [original_recipe()]
    original = originals[0]
    corpus = tmp_path / 'corpus.jsonl'
    corpus.write_text(''.join(tr.canonical(row) + '\n' for row in originals))
    output = tmp_path / 'translation'
    tr.build_plan(corpus, output)
    if annotation_path:
        raw_plan = json.loads((output / 'plan.json').read_text())
        text = original['steps'][-1]
        annotation_path.write_text(tr.canonical({
            'operation': 'root_ai_structural_annotation_candidates', 'source_corpus_sha256': raw_plan['corpus_sha256'],
            'count': 1, 'publication_approved': False, 'source_text_modified': False, 'entries': [{
                'source_id': original['source_id'], 'source_revision': original['source_revision'],
                'field': 'steps', 'index': len(original['steps']) - 1, 'source_text': text,
                'source_text_sha256': tr.hashlib.sha256(text.encode()).hexdigest(),
                'annotation': '来源附带的其他语言菜谱互链（不是烹饪步骤）：' + text + '。',
                'quality_flag': 'source_parser_artifact_unreviewed'}]}))
    plan = tr.load_plan(output / 'plan.json')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps({'approved': True, 'plan_sha256': plan['plan_sha256'],
        'approved_by': 'synthetic-only', 'authorization_evidence': 'offline fixture, no actual provider',
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 'max_cny': 10, 'max_calls': 10}))
    fake_env = tmp_path / 'synthetic.env'
    fake_env.write_text('DEEPSEEK_API_KEY=synthetic-only\n')
    chinese = {'Rice': '米饭', '1 cup rice': '1杯大米', 'Cook rice for 10 minutes.': '将大米煮10分钟。',
               '2 servings': '2人份', 'Serve warm.': '趁热食用。', 'Use clean water.': '使用干净的水。', 'Notes': '说明'}
    chinese.update(translations or {})

    def in_process_transport(body):
        segments = json.loads(body['messages'][-1]['content'])['segments']
        content = {'translations': [{'id': row['id'], 'text': chinese[row['text']]} for row in segments]}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': tr.canonical(content)}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}

    tr.run_paid(output / 'plan.json', approval, fake_env, transport=in_process_transport)
    bundle = tool.load_inputs(output / 'plan.json', output / 'translated.jsonl')
    return {'original': original, 'output': output, 'bundle': bundle}


@pytest.fixture
def translated(tmp_path):
    return make_translated(tmp_path)


def payload(fixture):
    original = fixture['original']
    meta = {'source_id': original['source_id'], 'source_revision': original['source_revision'],
            'language': 'en', 'license': original['source_license'], 'manual_reviewed': False,
            'canonical_ingredients': ['rice'], 'quality_flags': ['original_parser_flag'],
            'import_batch_id': 'original-batch', 'source_servings_text': '2 servings'}
    return {'type': 'recipe', 'status': 'draft', 'title': original['title'], 'source': original['source'],
            'ingredients': original['ingredients'], 'steps': original['steps'], 'notes': original['notes'],
            'body': original['body'], 'servings': 2, 'servings_text': '', 'nutrition_notes': '',
            'entry_id': 'stable-entry', 'conditions': copy.deepcopy(meta), 'source_meta': copy.deepcopy(meta),
            'import_history': [{'original': 'preserved'}], 'status_history': [{'to': 'draft'}]}


def record(value, rid='one', owner='system', kind='knowledge'):
    return {'id': rid, 'owner': owner, 'kind': kind, 'payload': copy.deepcopy(value)}


def test_preview_never_loads_settings_credentials_or_network(translated, monkeypatch):
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == 'app' or name.startswith(('app.', 'dotenv', 'httpx')):
            raise AssertionError('Offline preview imported settings/credential/network module')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    output = translated['output']
    before = {str(path): path.read_bytes() for path in output.rglob('*') if path.is_file()}
    bundle = tool.load_inputs(output / 'plan.json', output / 'translated.jsonl')
    assert bundle['meta']['complete_input_rows'] == 1
    assert before == {str(path): path.read_bytes() for path in output.rglob('*') if path.is_file()}


def test_updates_only_display_and_preserves_original_source_and_histories(translated):
    old = payload(translated)
    old['source_meta']['original_content'] = {'title': 'Rice', 'extra_source_evidence': 'keep verbatim'}
    report, changes = tool.plan_updates([record(old)], translated['bundle'])
    assert report['updated'] == 1 and report['created'] == report['deleted'] == 0
    new = changes[0]['payload']
    assert new['title'] == '米饭' and new['ingredients'] == ['1杯大米']
    assert new['steps'] == ['将大米煮10分钟。'] and new['servings_text'] == '2人份'
    for field in ('source', 'conditions', 'import_history', 'status_history', 'entry_id', 'status', 'servings'):
        assert new[field] == old[field]
    assert new['source_meta']['canonical_ingredients'] == ['rice']
    assert new['source_meta']['license'] == old['source_meta']['license']
    assert new['source_meta']['source_revision'] == 7
    assert new['source_meta']['original_content']['ingredients'] == ['1 cup rice']
    assert new['source_meta']['original_content']['extra_source_evidence'] == 'keep verbatim'
    assert set(tool.MANDATORY_FLAGS) <= set(new['source_meta']['quality_flags'])
    assert 'original_parser_flag' in new['source_meta']['quality_flags']
    assert new['source_meta']['source_additional_sections'][0]['text'] == '趁热食用。'
    assert new['source_meta']['translation']['validation']['semantic_quality_manually_reviewed'] is False
    assert knowledge.original_recipe_ingredients(new) == ['1 cup rice']
    again, changes2 = tool.plan_updates([record(new)], translated['bundle'])
    assert changes2 == [] and again['counts'] == {'already_same_translation': 1}


@pytest.mark.parametrize('mutation,reason', [
    ({'status': 'reviewed'}, 'protected_review_or_status'),
    ({'status': 'published'}, 'protected_review_or_status'),
    ({'status': 'archived'}, 'protected_review_or_status'),
    ({'manual_reviewed': True}, 'protected_review_or_status'),
    ({'title': 'Manually changed English title'}, 'draft_source_content_changed'),
])
def test_reviewed_or_modified_drafts_are_not_overwritten(translated, mutation, reason):
    old = {**payload(translated), **mutation}
    report, changes = tool.plan_updates([record(old)], translated['bundle'])
    assert changes == [] and report['counts'] == {reason: 1}


def test_private_nonrecipe_missing_and_source_revision_mismatch_are_untouched(translated):
    old = payload(translated)
    report, changes = tool.plan_updates([record(old, owner='alice'), record(old, kind='recipe')], translated['bundle'])
    assert changes == [] and report['counts'] == {'missing_existing_source': 1}
    for location in ('conditions', 'source_meta'):
        old[location]['source_revision'] = 8
    report, changes = tool.plan_updates([record(old)], translated['bundle'])
    assert changes == [] and report['counts'] == {'source_revision_mismatch': 1}


def test_existing_chinese_and_different_translation_version_are_protected(translated):
    for changes, expected in (({'language': 'zh-CN'}, 'protected_untranslated_chinese_record'),
                              ({'translation': {'status': 'complete', 'output_sha256': 'different'}}, 'protected_translation_version'),
                              ({'manual_reviewed': True}, 'protected_review_or_status')):
        old = payload(translated)
        old['source_meta'].update(changes)
        report, updates = tool.plan_updates([record(old)], translated['bundle'])
        assert updates == [] and report['counts'] == {expected: 1}


@pytest.mark.parametrize('which', ['plan', 'translated', 'checkpoint', 'receipt'])
def test_tampered_plan_text_checkpoint_or_receipt_is_rejected(translated, which):
    output = translated['output']
    if which == 'plan':
        path = output / 'plan.json'
        value = json.loads(path.read_text()); value['source_count'] += 1
    elif which == 'translated':
        path = output / 'translated.jsonl'
        value = next(tr.read_jsonl(path)); value['title'] = '被改过的米饭'
    else:
        path = next((output / ('validated' if which == 'checkpoint' else 'receipts')).glob('*.json'))
        value = json.loads(path.read_text()); value['job_id' if which == 'checkpoint' else 'response_sha256'] = 'changed'
    path.write_text(tr.canonical(value) + '\n')
    with pytest.raises((tool.ImportError, tr.TranslationError)):
        tool.load_inputs(output / 'plan.json', output / 'translated.jsonl')


def test_untranslated_and_oversized_display_is_refused_without_truncation(translated):
    row = copy.deepcopy(translated['bundle']['rows'][0])
    row['body'] = '中文 but still English'
    with pytest.raises(tool.ImportError, match='untranslated'):
        tool.check_display(row)
    row['body'] = '中' * 8001
    with pytest.raises(tool.ImportError, match='limits'):
        tool.check_display(row)
    row['body'] = '中文'
    row['ingredients'] *= 61
    with pytest.raises(tool.ImportError, match='limits'):
        tool.check_display(row)


@pytest.mark.parametrize('text', [
    '瓦瑟尔热果汁饮（Wassel，原名音译）',
    '可将糖替换为4至6包SPLENDA甜味剂。',
    '面粉1 kg，水250 ml，面粉包装标注为500g。',
    '来源说明见 https://example.org/recipe/English%20Name 。',
    'Dahi Baingana（酸奶炸茄子）',
    '白全麦面粉的一个生产商是King Arthur品牌。',
    '将意面煮至弹牙（al dente）。',
    '用皂草根（Saponaria officinalis）煮成糖浆。',
    '250 ml',
])
def test_chinese_display_accepts_names_brands_units_and_urls(text):
    assert tool.chinese_display(text)


@pytest.mark.parametrize('text', [
    'Cook rice for 10 minutes.',
    '说明：Cook rice for 10 minutes.',
    '中文 but still English',
    '备注：Serve warm.',
    '来源附带的其他语言菜谱互链（不是烹饪步骤）：fr:Foreign Recipe。',
])
def test_chinese_display_rejects_english_sentences_and_unaudited_annotations(text):
    assert not tool.chinese_display(text)


def test_exact_editorial_annotation_is_bound_to_source_proof(tmp_path, monkeypatch):
    original = original_recipe()
    text = 'fr:Livre de cuisine/Bison à la grecque'
    original['steps'].append(text)
    annotation = '来源附带的其他语言菜谱互链（不是烹饪步骤）：' + text + '。'
    path = tmp_path / 'annotations.json'
    monkeypatch.setattr(tr, 'SOURCE_ANNOTATION_MANIFEST', path)
    fixture = make_translated(tmp_path, [original], {text: annotation}, annotation_path=path)
    bundle = fixture['bundle']
    assert bundle['meta']['importable_input_rows'] == 1
    assert str(path.resolve()) in bundle['meta']['proof_files']
    assert bundle['rows'][0]['steps'][-1] == annotation
    report, changes = tool.plan_updates([record(payload(fixture))], bundle)
    assert report['updated'] == 1
    assert 'source_parser_artifact_unreviewed' in changes[0]['payload']['source_meta']['translation_quality_flags']
    assert tool.chinese_display(annotation, [annotation])
    assert not tool.chinese_display(annotation + ' Cook rice for 10 minutes.', [annotation])
    path.write_text(path.read_text() + '\n')
    with pytest.raises(tool.ImportError, match='changed'):
        tool.unchanged(bundle['meta']['proof_files'])


def test_fully_translated_incomplete_source_updates_draft_without_inventing_tutorial(tmp_path, signed):
    original = original_recipe()
    original.update(ingredients=[], steps=[], structural_complete=False,
                    quality_flags=['missing_ingredients', 'missing_steps', 'future_source_quality_flag'])
    fixture = make_translated(tmp_path, [original])
    old = payload(fixture)
    with core.DB() as db:
        db.add(core.Record(owner='system', kind='knowledge', payload=old)); db.commit()
    preview = tool.database_report(fixture['bundle'])
    assert preview['updated'] == 1
    applied = tool.database_report(fixture['bundle'], apply=True,
        expected_input_sha256=preview['input_sha256'], expected_db_sha256=preview['database_snapshot_sha256'])
    assert applied['database_written'] is True
    with core.DB() as db:
        new = db.scalar(select(core.Record).where(core.Record.owner == 'system')).payload
    assert new['ingredients'] == [] and new['steps'] == []
    assert new['source_meta']['original_content']['ingredients'] == []
    assert new['source_meta']['original_content']['steps'] == []
    assert new['source_meta']['source_structural_complete'] is False
    assert set(original['quality_flags']) <= set(new['source_meta']['quality_flags'])
    assert new['status'] == 'draft' and not new.get('manual_reviewed')
    assert new['source_meta']['translation']['status'] == 'complete'
    assert 'source_parser_artifact_unreviewed' not in new['source_meta']['quality_flags']


def test_clean_source_has_no_invented_parser_flag_and_all_actual_flags_survive(tmp_path):
    original = original_recipe()
    original['quality_flags'] = []
    fixture = make_translated(tmp_path, [original])
    old = payload(fixture)
    old['conditions']['quality_flags'] = ['condition_only_flag']
    old['source_meta']['quality_flags'] = ['metadata_only_flag']
    report, changes = tool.plan_updates([record(old)], fixture['bundle'])
    clean_meta = changes[0]['payload']['source_meta']
    assert report['updated'] == 1
    assert 'source_parser_artifact_unreviewed' not in clean_meta['quality_flags']
    assert 'source_parser_artifact_unreviewed' not in clean_meta['translation_quality_flags']
    assert {'condition_only_flag', 'metadata_only_flag', 'machine_translation_unreviewed'} <= set(clean_meta['quality_flags'])

    row = copy.deepcopy(fixture['bundle']['rows'][0])
    row['quality_flags'] = ['future_row_flag', 'source_parser_artifact_unreviewed']
    row['source_meta']['quality_flags'] = ['source_meta_only_flag']
    row['source_meta']['translation_quality_flags'] = ['source_nonvegetarian_pairing_unreviewed', 'future_translation_flag']
    candidate, reason = tool.update_payload(old, row, fixture['bundle']['meta'])
    assert reason == 'update_existing_draft'
    assert {'future_row_flag', 'source_parser_artifact_unreviewed', 'source_meta_only_flag',
            'source_nonvegetarian_pairing_unreviewed', 'future_translation_flag'} <= set(candidate['source_meta']['quality_flags'])
    assert {'source_nonvegetarian_pairing_unreviewed', 'future_translation_flag'} <= set(candidate['source_meta']['translation_quality_flags'])


def test_validated_but_oversized_row_is_reported_and_does_not_block_other_sources(tmp_path):
    clean, large = original_recipe(), original_recipe()
    clean['quality_flags'] = []
    large['source_id'] = 'test:oversized-notes'
    large['notes'] *= 41
    fixture = make_translated(tmp_path, [clean, large])
    bundle = fixture['bundle']
    assert bundle['meta']['complete_input_rows'] == 2
    assert bundle['meta']['importable_input_rows'] == 1
    assert bundle['meta']['input_skipped_rows'] == 1
    assert bundle['meta']['input_skips'] == [{'source_id': large['source_id'], 'source_revision': 7,
        'reason': 'display_not_importable', 'detail': 'Translated list exceeds application limits or has invalid lines: notes'}]
    report, changes = tool.plan_updates([record(payload(fixture))], bundle)
    assert report['updated'] == 1 and len(changes) == 1
    assert report['counts'] == {'display_not_importable': 1, 'update_existing_draft': 1}


def test_database_apply_is_atomic_owner_scoped_idempotent_and_never_publishes(translated, signed):
    with core.DB() as db:
        public = core.Record(owner='system', kind='knowledge', payload=payload(translated))
        private = core.Record(owner='alice', kind='knowledge', payload=payload(translated))
        db.add_all([public, private]); db.commit()
        public_id, private_id = public.id, private.id
    preview = tool.database_report(translated['bundle'])
    assert preview['updated'] == 1 and preview['database_written'] is False
    result = tool.database_report(translated['bundle'], apply=True,
        expected_input_sha256=preview['input_sha256'], expected_db_sha256=preview['database_snapshot_sha256'])
    assert result['database_written'] is True
    with core.DB() as db:
        row = db.get(core.Record, public_id)
        assert row.payload['title'] == '米饭' and row.payload['status'] == 'draft'
        assert db.get(core.Record, private_id).payload['title'] == 'Rice'
        assert len(list(db.scalars(select(core.Record).where(core.Record.kind == 'knowledge')))) == 2
    second = tool.database_report(translated['bundle'])
    assert second['updated'] == 0
    again = tool.database_report(translated['bundle'], apply=True,
        expected_input_sha256=second['input_sha256'], expected_db_sha256=second['database_snapshot_sha256'])
    assert again['database_written'] is False
    assert knowledge.public_items()['items'] == []


def test_stale_input_or_target_sha_aborts_without_overwriting_review(translated, signed):
    with core.DB() as db:
        row = core.Record(owner='system', kind='knowledge', payload=payload(translated))
        db.add(row); db.commit(); identifier = row.id
    preview = tool.database_report(translated['bundle'])
    with pytest.raises(tool.ImportError, match='exact input'):
        tool.database_report(translated['bundle'], apply=True,
            expected_input_sha256='changed', expected_db_sha256=preview['database_snapshot_sha256'])
    with core.DB() as db:
        row = db.get(core.Record, identifier)
        row.payload = {**row.payload, 'manual_reviewed': True}
        db.commit()
    with pytest.raises(tool.ImportError, match='Database changed'):
        tool.database_report(translated['bundle'], apply=True,
            expected_input_sha256=preview['input_sha256'], expected_db_sha256=preview['database_snapshot_sha256'])
    with core.DB() as db:
        assert db.get(core.Record, identifier).payload['title'] == 'Rice'


def test_changed_proof_after_preview_prevents_apply(translated, signed):
    with core.DB() as db:
        db.add(core.Record(owner='system', kind='knowledge', payload=payload(translated))); db.commit()
    preview = tool.database_report(translated['bundle'])
    path = translated['output'] / 'translated.jsonl'
    path.write_text(path.read_text() + '\n')
    with pytest.raises(tool.ImportError, match='changed'):
        tool.database_report(translated['bundle'], apply=True,
            expected_input_sha256=preview['input_sha256'], expected_db_sha256=preview['database_snapshot_sha256'])
    with core.DB() as db:
        assert db.scalar(select(core.Record).where(core.Record.owner == 'system')).payload['title'] == 'Rice'


def test_duplicate_or_conflicting_source_metadata_never_picks_an_arbitrary_draft(translated):
    old = payload(translated)
    report, changes = tool.plan_updates([record(old, 'a'), record(old, 'b')], translated['bundle'])
    assert changes == [] and report['counts'] == {'duplicate_source_revision': 1}
    old['conditions']['source_revision'] = 8
    report, changes = tool.plan_updates([record(old)], translated['bundle'])
    assert changes == [] and report['counts'] == {'source_metadata_conflict': 1}


def test_cli_default_is_offline_and_existing_report_is_rejected_before_database(translated, tmp_path, monkeypatch):
    output = translated['output']
    report = tmp_path / 'preview.json'
    monkeypatch.setattr(tool, 'database_report', lambda *args, **kwargs: pytest.fail('Offline/default cannot access DB'))
    argv = ['--plan', str(output / 'plan.json'), '--translated', str(output / 'translated.jsonl'), '--output', str(report)]
    assert tool.main(argv) == 0
    assert json.loads(report.read_text())['database_accessed'] is False
    assert tool.main([*argv, '--apply', '--expected-input-sha256', 'unused', '--expected-db-sha256', 'unused']) == 1


def test_source_file_limit_is_checked_before_loading_proof(translated, monkeypatch):
    monkeypatch.setattr(tool, 'MAX_FILE_BYTES', 1)
    output = translated['output']
    with pytest.raises(tool.ImportError, match='size limit'):
        tool.load_inputs(output / 'plan.json', output / 'translated.jsonl')


def test_concurrent_apply_serializes_and_rejects_stale_second_writer(translated, signed):
    with core.DB() as db:
        db.add(core.Record(owner='system', kind='knowledge', payload=payload(translated))); db.commit()
    preview = tool.database_report(translated['bundle'])
    def apply():
        try:
            result = tool.database_report(translated['bundle'], apply=True,
                expected_input_sha256=preview['input_sha256'], expected_db_sha256=preview['database_snapshot_sha256'])
            return 'updated' if result['database_written'] else 'unchanged'
        except tool.ImportError:
            return 'stale'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: apply(), range(2))) == ['stale', 'updated']
    with core.DB() as db:
        row = db.scalar(select(core.Record).where(core.Record.owner == 'system'))
        assert len(row.payload['translation_import_history']) == 1


def test_post_flush_proof_conflict_rolls_back_all_changes(translated, signed, monkeypatch):
    with core.DB() as db:
        db.add(core.Record(owner='system', kind='knowledge', payload=payload(translated))); db.commit()
    preview = tool.database_report(translated['bundle'])
    original = tool.unchanged
    calls = []
    def fail_after_flush(fingerprints):
        calls.append(True)
        if len(calls) == 2:
            raise tool.ImportError('Proof changed after flush')
        original(fingerprints)
    monkeypatch.setattr(tool, 'unchanged', fail_after_flush)
    with pytest.raises(tool.ImportError, match='after flush'):
        tool.database_report(translated['bundle'], apply=True,
            expected_input_sha256=preview['input_sha256'], expected_db_sha256=preview['database_snapshot_sha256'])
    with core.DB() as db:
        assert db.scalar(select(core.Record).where(core.Record.owner == 'system')).payload['title'] == 'Rice'

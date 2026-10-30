"""Offline translation planning, fidelity guards and budget checkpoints.

No application settings, business database or paid provider is used. Runner
tests inject an in-process transport and a temporary fake credential file.
"""
from datetime import datetime, timedelta, timezone
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import threading
import time

from scripts import translate_recipe_corpus as tr


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.corpus = self.root / 'corpus.jsonl'
        self.output = self.root / 'translation'
        self.row = {'source_id': 'test:1', 'source_revision': 'r1', 'source_language': 'en',
                    'review_candidate_eligible': True, 'source': 'https://example.test/recipe',
                    'title': 'Rice', 'ingredients': ['1 cup rice', '2 eggs'],
                    'steps': ['Cook for 10 minutes'], 'servings': '2 servings',
                    'body': 'Do not eat raw eggs.', 'notes': ['Contains milk.'],
                    'review_sections': [{'heading': 'Notes', 'path': ['Notes'], 'kind': 'stop',
                                         'text': 'No honey.'}]}
        self.write_rows([self.row])
        self.cn = {'Rice': '米饭', '1 cup rice': '1杯大米', '2 eggs': '2个鸡蛋',
                   'Cook for 10 minutes': '烹煮10分钟', '2 servings': '2人份',
                   'Do not eat raw eggs.': '不要吃生鸡蛋。', 'Contains milk.': '含牛奶。',
                   'Notes': '注意事项', 'No honey.': '不含蜂蜜。'}

    def write_rows(self, rows):
        self.corpus.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))

    def make_plan(self, **kwargs):
        tr.build_plan(self.corpus, self.output, **kwargs)
        return tr.load_plan(self.output / 'plan.json')

    def response_content(self, job):
        return tr.canonical({'translations': [
            {'id': segment['wire_id'],
             'text': self.cn[segment['text']]} for segment in job['segments']]})

    def approval(self, plan, **kwargs):
        approval = {'approved': True, 'plan_sha256': plan['plan_sha256'], 'approved_by': 'offline-test',
                    'authorization_evidence': 'test-fixture-only',
                    'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    'max_cny': 10, 'max_calls': 10}
        approval.update(kwargs)
        return approval

    def fake_runner_inputs(self, plan, **kwargs):
        approval = self.root / 'approval.json'
        approval.write_text(json.dumps(self.approval(plan, **kwargs)))
        env = self.root / 'fake.env'
        env.write_text('DEEPSEEK_API_KEY=test-only\n')
        return approval, env

    def legacy_files(self, plan, segments=None):
        """Old-protocol public-content fixture, never a real billing receipt."""
        segments = segments or [segment for job in plan['jobs'] for segment in job['segments']]
        old_segments = [{key: value for key, value in segment.items() if key != 'wire_id'} for segment in segments]
        old_job = {'segments': old_segments, 'job_id': tr.digest({'legacy_segments': old_segments}),
                   'max_output_tokens': 8192, 'input_token_upper_bound': 20000,
                   'output_token_upper_bound': 8192, 'reserved_micros': 20000 * 2 + 8192 * 8}
        old_plan = {'format': 'recipe-zh-translation-v1', 'sources': plan['sources'], 'jobs': [old_job]}
        old_plan['plan_sha256'] = tr.digest(old_plan)
        output = self.root / 'legacy'
        output.mkdir(exist_ok=True)
        old_plan_path = output / 'plan.json'
        tr.atomic_json(old_plan_path, old_plan)
        old_response = {'choices': [{'finish_reason': 'stop', 'message': {'content': tr.canonical({
            'type': 'json_object', 'translations': [{'segment_id': segment['segment_id'], 'source_key': segment['source_key'],
                                                   'text': self.cn[segment['text']]} for segment in old_segments]})}}],
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}
        response_path = output / 'responses' / (old_job['job_id'] + '.json')
        tr.atomic_json(response_path, old_response)
        old_ledger = {'plan_sha256': old_plan['plan_sha256'], 'calls': 1,
                      'reserved_micros': old_job['reserved_micros'], 'charged_micros': old_job['reserved_micros'],
                      'jobs': {old_job['job_id']: {'status': 'quarantined', 'reserved_micros': old_job['reserved_micros'],
                                                  'charged_micros': old_job['reserved_micros']}}}
        ledger_path = output / 'ledger.json'
        tr.atomic_json(ledger_path, old_ledger)
        return old_plan_path, response_path, ledger_path

    def test_plan_never_imports_settings_or_reads_credentials(self):
        original_import = __import__

        def guarded(name, *args, **kwargs):
            if name.startswith(('app.', 'httpx', 'dotenv')):
                raise AssertionError('Offline plan imported settings/network/credential loader')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', guarded):
            plan = self.make_plan()
        self.assertEqual(plan['source_count'], 1)
        self.assertFalse(plan['paid_execution_approved'])
        self.assertFalse(plan['publication_approved'])
        self.assertFalse((self.output / 'ledger.json').exists())

    def test_plan_includes_all_human_fields_and_preserves_enum(self):
        plan = self.make_plan()
        paths = [tuple(s['path']) for job in plan['jobs'] for s in job['segments']]
        self.assertIn(('body',), paths)
        self.assertIn(('notes', 0), paths)
        self.assertIn(('review_sections', 0, 'heading'), paths)
        self.assertIn(('review_sections', 0, 'path', 0), paths)
        self.assertNotIn(('review_sections', 0, 'kind'), paths)
        self.assertEqual(plan['sources'][0]['original'], self.row)

    def test_identity_changes_for_revision_or_source_content(self):
        first = tr.source_identity(self.row)
        revision = {**self.row, 'source_revision': 'r2'}
        content = {**self.row, 'ingredients': ['3 eggs']}
        self.assertNotEqual(first['source_key'], tr.source_identity(revision)['source_key'])
        self.assertNotEqual(first['source_key'], tr.source_identity(content)['source_key'])

    def test_stable_plan_is_resumable_and_different_plan_is_not_overwritten(self):
        first = self.make_plan()
        self.assertEqual(self.make_plan(), first)
        self.write_rows([{**self.row, 'source_revision': 'r2'}])
        with self.assertRaises(tr.TranslationError):
            self.make_plan()

    def test_duplicate_ids_rejected_and_chinese_or_excluded_sources_are_skipped(self):
        self.write_rows([self.row, {**self.row}])
        with self.assertRaises(tr.TranslationError):
            self.make_plan()
        self.write_rows([self.row, {**self.row, 'source_id': 'zh', 'source_language': 'zh'},
                         {**self.row, 'source_id': 'excluded', 'review_candidate_eligible': False}])
        self.assertEqual(self.make_plan()['source_count'], 1)

    def test_lossless_chunking_does_not_split_a_food_word(self):
        original = ('A paragraph containing beef and milk. ' * 50) + 'End.'
        chunks = list(tr.split_piece(original, 100))
        self.assertEqual(''.join(chunks), original)
        self.assertTrue(all(len(piece) <= 100 for piece in chunks))
        with self.assertRaises(tr.TranslationError):
            list(tr.split_piece('x' * 500, 100))

    def test_request_disables_thinking_and_reserves_peak_token_cost(self):
        job = self.make_plan()['jobs'][0]
        request = tr.request_body(job)
        self.assertEqual(request['thinking'], {'type': 'disabled'})
        self.assertFalse(request['stream'])
        self.assertEqual(request['max_tokens'], job['max_output_tokens'])
        expected = job['input_token_upper_bound'] * 2 + job['max_output_tokens'] * 8
        self.assertEqual(job['reserved_micros'], expected)
        self.assertGreater(job['input_token_upper_bound'], len(tr.canonical(request).encode()))
        wire = json.loads(request['messages'][1]['content'])
        self.assertEqual(set(wire), {'segments'})
        self.assertTrue(all(set(item) == {'id', 'text'} and type(item['id']) is int for item in wire['segments']))
        self.assertNotIn(job['job_id'], request['messages'][1]['content'])
        self.assertNotIn(job['segments'][0]['source_key'], request['messages'][1]['content'])

    def test_strict_schema_and_exact_identity_validated(self):
        job = self.make_plan()['jobs'][0]
        self.assertEqual(tr.validate_response(job, self.response_content(job))['job_id'], job['job_id'])
        cases = []
        for field, value in [('job_id', 'wrong'), ('extra', 'unknown')]:
            response = json.loads(self.response_content(job))
            response[field] = value
            cases.append(response)
        response = json.loads(self.response_content(job))
        response['translations'][0]['source_key'] = 'wrong'
        cases.append(response)
        response = json.loads(self.response_content(job))
        response['translations'].append(response['translations'][0])
        cases.append(response)
        response = json.loads(self.response_content(job))
        response['translations'][1] = response['translations'][0]
        cases.append(response)
        for response in cases:
            with self.subTest(response=response), self.assertRaises(tr.TranslationError):
                tr.validate_response(job, tr.canonical(response))
        with self.assertRaises(tr.TranslationError):
            tr.validate_response(job, '```json\n' + self.response_content(job) + '\n```')

    def test_numbers_temperature_time_and_unit_pairs_cannot_be_rewritten(self):
        self.assertFalse(tr.validate_translation('Heat 2 cups rice for 10 minutes at 350°F.',
                                               '将2杯大米在350华氏温度下加热10分钟。'))
        for source, target in [('2 eggs', '3个鸡蛋'), ('2 cups rice', '2克大米'),
                               ('10 minutes at 100°C', '100分钟在10摄氏温度'),
                               ('350°F', '175摄氏度'), ('-2 grams salt', '2克盐'),
                               ('½ cup water', '1杯水')]:
            with self.subTest(source=source):
                self.assertTrue(tr.validate_translation(source, target))
        self.assertFalse(tr.validate_translation('½ cup water', '1/2杯水'))
        self.assertFalse(tr.validate_translation('200g flour for 10min', '200克面粉，10分钟'))
        self.assertFalse(tr.validate_translation('350F for 24-hour cooking', '350华氏温度，烹煮24小时'))

    def test_animal_dairy_honey_and_pungent_terms_cannot_be_omitted(self):
        for source, bad, good in [('beef and potato', '土豆', '牛肉和土豆'),
                                  ('milk and sugar', '糖', '牛奶和糖'),
                                  ('2 eggs', '2份面粉', '2个鸡蛋'),
                                  ('honey', '枫糖浆', '蜂蜜'),
                                  ('garlic', '生姜', '蒜'),
                                  ('vegan chicken', '鸡肉', '植物鸡')]:
            with self.subTest(source=source):
                self.assertTrue(tr.validate_translation(source, bad))
                self.assertFalse(tr.validate_translation(source, good))

    def test_diet_source_claim_does_not_authorize_animal_substitution(self):
        self.assertIn('dangerous_ingredient_omitted:牛肉', tr.validate_translation('beef', '豆腐'))
        self.assertIn('animal_ingredient_replaced_by_substitute', tr.validate_translation('beef', '植物牛肉替代品'))

    def test_authorization_must_be_explicit_unexpired_and_bound_to_exact_plan(self):
        plan = self.make_plan()
        for update in ({'approved': False}, {'plan_sha256': 'wrong'}, {'max_cny': 0},
                       {'max_calls': 0}, {'authorization_evidence': ''},
                       {'expires_at': '2020-01-01T00:00:00+00:00'}):
            with self.subTest(update=update), self.assertRaises(tr.TranslationError):
                tr.validate_authorization(plan, self.approval(plan, **update))
        self.assertEqual(tr.validate_authorization(plan, self.approval(plan)), (10_000_000, 10))

    def test_invalid_approval_fails_before_secret_load_or_transport(self):
        plan = self.make_plan()
        approval, env = self.fake_runner_inputs(plan, approved=False)
        with patch('dotenv.dotenv_values', side_effect=AssertionError('Credential loader called')):
            with self.assertRaises(tr.TranslationError):
                tr.run_paid(self.output / 'plan.json', approval, env,
                            transport=lambda _: self.fail('Unapproved transport called'))

    def test_budget_is_pre_reserved_and_survives_process_recovery(self):
        plan = self.make_plan()
        job = plan['jobs'][0]
        ledger = tr.Ledger(self.output / 'ledger.json', plan, job['reserved_micros'] - 1, 1)
        with self.assertRaises(tr.TranslationError):
            ledger.reserve(job)
        self.assertFalse(ledger.path.exists())
        ledger = tr.Ledger(ledger.path, plan, job['reserved_micros'], 1)
        ledger.reserve(job)
        recovered = tr.Ledger(ledger.path, plan, 10_000_000, 10)
        self.assertEqual(recovered.state['reserved_micros'], job['reserved_micros'])
        with self.assertRaises(tr.TranslationError):
            recovered.reserve(job)
        recovered.finish(job, 'unknown')
        self.assertEqual(recovered.state['reserved_micros'], job['reserved_micros'])
        with self.assertRaises(tr.TranslationError):
            recovered.reserve(job)

    def test_serial_lock_prevents_parallel_budget_bypass(self):
        self.make_plan()
        with tr.exclusive_run(self.output):
            with self.assertRaises(tr.TranslationError):
                with tr.exclusive_run(self.output):
                    self.fail('Second run acquired the same lock')

    def test_unknown_response_stops_without_retry_or_refund(self):
        plan = self.make_plan()
        approval, env = self.fake_runner_inputs(plan)
        calls = []

        def fail(payload):
            calls.append(payload)
            ledger = json.loads((self.output / 'ledger.json').read_text())
            self.assertEqual(ledger['calls'], 1)  # Must be durable before call.
            raise TimeoutError('fixture timeout')
        with self.assertRaises(tr.TranslationError):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=fail)
        with self.assertRaises(tr.TranslationError):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=fail)
        self.assertEqual(len(calls), 1)
        ledger = json.loads((self.output / 'ledger.json').read_text())
        self.assertEqual(next(iter(ledger['jobs'].values()))['status'], 'unknown')
        self.assertEqual(ledger['reserved_micros'], plan['jobs'][0]['reserved_micros'])

    def test_valid_checkpoint_reassembles_all_fields_and_does_not_repeat_paid_job(self):
        plan = self.make_plan()
        approval, env = self.fake_runner_inputs(plan)
        calls = []

        def fixture(payload):
            calls.append(payload)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': self.response_content(plan['jobs'][0])}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}
        tr.run_paid(self.output / 'plan.json', approval, env, transport=fixture)
        tr.run_paid(self.output / 'plan.json', approval, env, transport=fixture)
        self.assertEqual(len(calls), 1)
        output = list(tr.read_jsonl(self.output / 'translated.jsonl'))
        self.assertEqual(len(output), 1)
        row = output[0]
        self.assertEqual(row['title'], '米饭')
        self.assertEqual(row['ingredients'], ['1杯大米', '2个鸡蛋'])
        self.assertEqual(row['body'], '不要吃生鸡蛋。')
        self.assertEqual(row['review_sections'][0]['kind'], 'stop')
        self.assertEqual(row['review_sections'][0]['text'], '不含蜂蜜。')
        self.assertEqual(row['source_meta']['original_content']['ingredients'], self.row['ingredients'])
        self.assertEqual(row['display_language'], 'zh-CN')
        self.assertEqual(row['status'], 'draft')
        self.assertFalse(row['publication_approved'])
        self.assertFalse(row['manual_reviewed'])
        self.assertIn('machine_translation_unreviewed', row['source_meta']['translation_quality_flags'])
        ledger = json.loads((self.output / 'ledger.json').read_text())
        # An injected fake response may test shape, but cannot create a real
        # billing credit or release the hard reservation.
        self.assertEqual(ledger['charged_micros'], ledger['reserved_micros'])

    def test_bad_usage_or_bad_translation_is_quarantined_without_followup_call(self):
        plan = self.make_plan()
        approval, env = self.fake_runner_inputs(plan)
        calls = []

        def fixture(payload):
            calls.append(payload)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': self.response_content(plan['jobs'][0])}}]}
        with self.assertRaises(tr.TranslationError):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=fixture)
        ledger = json.loads((self.output / 'ledger.json').read_text())
        self.assertEqual(len(calls), 1)
        self.assertEqual(next(iter(ledger['jobs'].values()))['status'], 'quarantined')
        self.assertEqual((self.output / 'translated.jsonl').read_text(), '')

    def test_periodic_progress_materializes_only_validated_jobs_and_stops_on_next_error(self):
        plan = self.make_plan(max_segments=4)
        approval, env = self.fake_runner_inputs(plan)
        calls = []

        def fixture(payload):
            job = plan['jobs'][len(calls)];calls.append(payload)
            if len(calls) == 3:
                self.assertTrue((self.output / 'progress.json').exists())
                return {}  # Stop, do not manufacture the unfinished source.
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': self.response_content(job)}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}
        with patch.object(tr, 'PROGRESS_JOB_INTERVAL', 2), patch('builtins.print'):
            with self.assertRaises(tr.TranslationError):
                tr.run_paid(self.output / 'plan.json', approval, env, transport=fixture)
        progress = json.loads((self.output / 'progress.json').read_text())
        self.assertEqual(progress['validated_jobs'], 2)
        self.assertEqual(progress['calls'], 3)
        self.assertEqual(progress['complete_sources'], 0)
        self.assertEqual((self.output / 'translated.jsonl').read_text(), '')
        self.assertEqual(len(calls), 3)
        self.assertFalse(progress['publication_approved'])

    def test_three_inflight_group_is_all_reserved_drained_and_bound_to_response_identity(self):
        plan = self.make_plan(max_segments=2)
        approval, env = self.fake_runner_inputs(plan)
        jobs = {job['segments'][0]['wire_id']: (i, job) for i, job in enumerate(plan['jobs'])}
        gate = threading.Barrier(3);seen = [];guard = threading.Lock()

        def fixture(payload):
            first = json.loads(payload['messages'][1]['content'])['segments'][0]['id']
            index, job = jobs[first]
            ledger = json.loads((self.output / 'ledger.json').read_text())
            self.assertEqual(ledger['calls'], 3)
            self.assertEqual(sum(e['status'] == 'reserved' for e in ledger['jobs'].values()), 3)
            self.assertEqual(ledger['charged_micros'], sum(j['reserved_micros'] for j in plan['jobs'][:3]))
            with guard:seen.append(index)
            gate.wait(timeout=2)
            # First error completes before the other two already-sent jobs.
            if index == 0:return {'invalid': 'complete response retained'}
            time.sleep((3-index) * .02)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': self.response_content(job)}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}
        with patch('builtins.print'), self.assertRaises(tr.TranslationError):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=fixture, concurrency=3)
        self.assertCountEqual(seen, [0, 1, 2])
        ledger = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10).state
        self.assertEqual(ledger['calls'], 3)
        self.assertEqual(ledger['jobs'][plan['jobs'][0]['job_id']]['status'], 'quarantined')
        for job in plan['jobs'][1:3]:
            self.assertEqual(ledger['jobs'][job['job_id']]['status'], 'succeeded')
            value = json.loads((self.output / 'validated' / (job['job_id']+'.json')).read_text())
            self.assertEqual(value['translations'][0]['segment_id'], job['segments'][0]['segment_id'])
        self.assertEqual(len(list((self.output / 'responses').glob('*.json'))), 3)
        self.assertNotIn(plan['jobs'][3]['job_id'], ledger['jobs'])
        with self.assertRaises(tr.TranslationError):
            tr.run_paid(self.output / 'plan.json', approval, env,
                        transport=lambda _:self.fail('An unresolved group was retried'), concurrency=3)

    def test_request_group_cap_is_atomic_and_max_new_calls_one_stays_one(self):
        plan = self.make_plan(max_segments=2)
        jobs = plan['jobs'][:3]
        ledger = tr.Ledger(self.output / 'ledger.json', plan,
                           sum(j['reserved_micros'] for j in jobs)-1, 3)
        with self.assertRaises(tr.TranslationError):ledger.reserve_group(jobs)
        self.assertEqual(ledger.state['calls'], 0)
        self.assertFalse(ledger.path.exists())
        approval, env = self.fake_runner_inputs(plan, max_calls=2)
        with self.assertRaises(tr.TranslationError):
            tr.run_paid(self.output / 'plan.json', approval, env,
                        transport=lambda _:self.fail('Partially funded group sent'), concurrency=3)
        self.assertFalse(ledger.path.exists())
        approval, env = self.fake_runner_inputs(plan)
        seen = []
        def fixture(payload):
            seen.append(payload)
            return {'choices':[{'finish_reason':'stop','message':{'content':self.response_content(plan['jobs'][0])}}],
                    'usage':{'prompt_tokens':100,'completion_tokens':100,'total_tokens':200}}
        with patch('builtins.print'):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=fixture,
                        max_new_calls=1, concurrency=3)
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(ledger.path.read_text())['calls'], 1)

    def test_incomplete_recipe_is_not_materialized(self):
        plan = self.make_plan(max_segments=2)
        tr.atomic_json(self.output / 'validated' / (plan['jobs'][0]['job_id'] + '.json'),
                       tr.validate_response(plan['jobs'][0], self.response_content(plan['jobs'][0])))
        report = tr.materialize(plan, self.output)
        self.assertEqual(report['complete_sources'], 0)
        self.assertEqual(report['incomplete_sources'], 1)
        self.assertEqual((self.output / 'translated.jsonl').read_text(), '')

    def test_verified_provider_path_settles_usage_and_reconciles_on_resume(self):
        """Isolated HTTP protocol fixture: never contacts the provider."""
        plan = self.make_plan(max_segments=2)
        cost_each = 100 * tr.INPUT_MICROS + 100 * tr.OUTPUT_MICROS
        cap = max(job['reserved_micros'] for job in plan['jobs']) + cost_each * len(plan['jobs'])
        approval, env = self.fake_runner_inputs(plan, max_cny=str(Decimal(cap) / 1_000_000),
                                                max_calls=len(plan['jobs']))
        jobs = {job['segments'][0]['wire_id']: job for job in plan['jobs']}
        calls = []
        test = self

        class FixtureResponse:
            status_code = 200

            def __init__(self, job):
                self.job = job

            def json(self):
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': test.response_content(self.job)}}],
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}

        class FixtureClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, endpoint, *, headers, json):
                self_payload = __import__('json').loads(json['messages'][1]['content'])
                first_id = self_payload['segments'][0]['id']
                calls.append(first_id)
                return FixtureResponse(jobs[first_id])

        with patch('httpx.Client', FixtureClient):
            result = tr.run_paid(self.output / 'plan.json', approval, env)
            recovered = tr.run_paid(self.output / 'plan.json', approval, env)
        self.assertEqual(len(calls), len(plan['jobs']))
        self.assertEqual(result, recovered)
        self.assertEqual(result['charged_micros'], cost_each * len(plan['jobs']))
        self.assertGreater(result['reserved_micros'], cap)
        self.assertLessEqual(result['charged_micros'], cap)
        ledger_path = self.output / 'ledger.json'
        altered = json.loads(ledger_path.read_text())
        first = next(iter(altered['jobs'].values()))
        first['charged_micros'] = 0
        altered['charged_micros'] = sum(entry['charged_micros'] for entry in altered['jobs'].values())
        ledger_path.write_text(json.dumps(altered))
        with self.assertRaises(tr.TranslationError):
            tr.Ledger(ledger_path, plan, cap, len(plan['jobs']))

    def test_overuse_unknown_usage_and_quality_failure_do_not_settle(self):
        plan = self.make_plan()
        job = plan['jobs'][0]
        good = {'choices': [{'finish_reason': 'stop', 'message': {'content': self.response_content(job)}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}
        cases = []
        for usage in ({'prompt_tokens': job['input_token_upper_bound'] + 1, 'completion_tokens': 100,
                       'total_tokens': job['input_token_upper_bound'] + 101},
                      {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 199},
                      {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
                      {'prompt_tokens': 100, 'completion_tokens': True, 'total_tokens': 101}):
            response = json.loads(json.dumps(good))
            response['usage'] = usage
            cases.append(response)
        truncated = json.loads(json.dumps(good))
        truncated['choices'][0]['finish_reason'] = 'length'
        cases.append(truncated)
        bad_quality = json.loads(json.dumps(good))
        bad_quality['choices'][0]['message']['content'] = self.response_content(job).replace('2个鸡蛋', '2份面粉')
        cases.append(bad_quality)
        for response in cases:
            with self.subTest(response=response), self.assertRaises(tr.TranslationError):
                tr.validate_provider_response(job, response)
        approval, env = self.fake_runner_inputs(plan)
        with self.assertRaises(tr.TranslationError):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=lambda _: cases[0])
        ledger = json.loads((self.output / 'ledger.json').read_text())
        self.assertEqual(ledger['charged_micros'], job['reserved_micros'])
        self.assertEqual(next(iter(ledger['jobs'].values()))['status'], 'quarantined')
        with self.assertRaises(tr.TranslationError):
            tr.run_paid(self.output / 'plan.json', approval, env,
                        transport=lambda _: self.fail('Quarantined request was retried'))

    def test_missing_receipts_cannot_fabricate_a_refund(self):
        plan = self.make_plan()
        job = plan['jobs'][0]
        ledger = tr.Ledger(self.output / 'ledger.json', plan, job['reserved_micros'], 1)
        ledger.reserve(job)
        with self.assertRaises(tr.TranslationError):
            ledger.finish(job, 'succeeded', usage={'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
                          peak_usage_micros=10, charged_micros=10,
                          settlement_origin='deepseek_https_200', response_sha256='fake', validated_sha256='fake')
        self.assertEqual(ledger.state['charged_micros'], job['reserved_micros'])

        self.assertEqual(ledger.state['jobs'][job['job_id']]['status'], 'reserved')
        with self.assertRaises(tr.TranslationError):
            ledger.finish(job, 'unknown', charged_micros=0)
        self.assertEqual(ledger.state['charged_micros'], job['reserved_micros'])

    def test_short_wire_accepts_reordering_and_rejects_missing_duplicate_foreign_or_noninteger_ids(self):
        plan = self.make_plan(max_segments=2)
        job = plan['jobs'][0]
        response = json.loads(self.response_content(job))
        reversed_response = {'translations': list(reversed(response['translations']))}
        normalized = tr.validate_response(job, tr.canonical(reversed_response))
        self.assertEqual([item['segment_id'] for item in normalized['translations']],
                         [segment['segment_id'] for segment in job['segments']])
        cases = []
        for wid in ('0', True, 0.0, -1, 999999, plan['jobs'][1]['segments'][0]['wire_id']):
            altered = json.loads(self.response_content(job))
            altered['translations'][0]['id'] = wid
            cases.append(altered)
        missing = json.loads(self.response_content(job))
        del missing['translations'][0]['id']
        cases.append(missing)
        duplicate = json.loads(self.response_content(job))
        duplicate['translations'][1]['id'] = duplicate['translations'][0]['id']
        cases.append(duplicate)
        extra = json.loads(self.response_content(job))
        extra['type'] = 'json_object'
        cases.append(extra)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(tr.TranslationError):
                tr.validate_response(job, tr.canonical(case))

    def test_short_wire_cannot_move_dangerous_or_quantity_translations_to_another_id(self):
        plan = self.make_plan()
        job = plan['jobs'][0]
        response = json.loads(self.response_content(job))
        egg = next(item for item in response['translations'] if item['text'] == '2个鸡蛋')
        rice = next(item for item in response['translations'] if item['text'] == '1杯大米')
        egg['text'], rice['text'] = rice['text'], egg['text']
        with self.assertRaises(tr.TranslationError):
            tr.validate_response(job, tr.canonical(response))

    def test_chocolate_word_is_not_gram_but_real_grams_still_require_exact_preservation(self):
        self.assertFalse(tr.validate_translation('Chocolate noodles', '巧克力面条'))
        self.assertFalse(tr.validate_translation('100g chocolate', '100克巧克力'))
        self.assertFalse(tr.validate_translation('100g chocolate', '巧克力100克'))
        self.assertTrue(tr.validate_translation('100g chocolate', '巧克力200克'))
        self.assertTrue(tr.validate_translation('100g chocolate', '巧克力100杯'))

    def test_legacy_metadata_diagnosis_and_full_recovery_are_offline_immutable_and_free_in_new_ledger(self):
        plan = self.make_plan()
        files = self.legacy_files(plan)
        before = [path.read_bytes() for path in files]
        report = tr.diagnose_legacy_response(files[0], files[1])
        self.assertTrue(report['metadata_only_recovery_possible'])
        self.assertEqual(report['original_translations_sha256'], report['proposed_translations_sha256'])
        self.assertFalse(report['writes_performed'])
        reused = tr.reuse_legacy(self.output / 'plan.json', *files)
        self.assertEqual(reused['fully_recovered_jobs'], len(plan['jobs']))
        self.assertEqual(reused['new_api_calls'], 0)
        self.assertEqual([path.read_bytes() for path in files], before)
        ledger = tr.Ledger(self.output / 'ledger.json', plan, 0, 0)
        self.assertEqual(ledger.state['charged_micros'], 0)
        self.assertEqual(ledger.state['reserved_micros'], 0)
        self.assertEqual(ledger.state['calls'], 0)
        self.assertTrue(all(item['status'] == 'offline_recovered' for item in ledger.state['jobs'].values()))
        self.assertEqual(len(list(tr.read_jsonl(self.output / 'translated.jsonl'))), 1)
        self.assertEqual(tr.reuse_legacy(self.output / 'plan.json', *files), reused)

    def test_partial_recovery_only_sends_untranslated_ids_and_requires_prior_budget_deduction(self):
        plan = self.make_plan(max_segments=4)
        first_two = plan['jobs'][0]['segments'][:2]
        files = self.legacy_files(plan, first_two)
        report = tr.reuse_legacy(self.output / 'plan.json', *files)
        self.assertEqual(report['fully_recovered_jobs'], 0)
        self.assertEqual(report['partial_segments_remain_content_checkpoints'], 2)
        state = json.loads((self.output / 'ledger.json').read_text())
        self.assertEqual(state['jobs'], {})  # An incomplete job is never called successful.
        approval, env = self.fake_runner_inputs(plan)
        with patch('dotenv.dotenv_values', side_effect=AssertionError('Unauthorized key read')):
            with self.assertRaises(tr.TranslationError):
                tr.run_paid(self.output / 'plan.json', approval, env, transport=lambda _: self.fail())
        recovery = tr.load_recovery(plan, self.output)
        approval, env = self.fake_runner_inputs(plan, prior_charged_micros=recovery['prior_charged_micros'],
            prior_calls=1, aggregate_max_cny=30, aggregate_max_calls=1500)
        effective = {job['segments'][0]['wire_id']: job for job in
                     [tr.effective_job(original, recovery) for original in plan['jobs']]}
        sent_ids = []

        def fixture(payload):
            wire = json.loads(payload['messages'][1]['content'])
            sent_ids.extend(item['id'] for item in wire['segments'])
            job = effective[wire['segments'][0]['id']]
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': self.response_content(job)}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}
        tr.run_paid(self.output / 'plan.json', approval, env, transport=fixture)
        self.assertFalse({segment['wire_id'] for segment in first_two} & set(sent_ids))
        self.assertEqual(len(sent_ids), sum(len(job['segments']) for job in plan['jobs']) - 2)
        self.assertEqual(len(list(tr.read_jsonl(self.output / 'translated.jsonl'))), 1)
        tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)  # Full recovery audit on restart.

    def test_recovery_rejects_changed_source_text_or_legacy_fingerprint_and_unbudgeted_prior_calls(self):
        plan = self.make_plan()
        files = self.legacy_files(plan)
        tr.reuse_legacy(self.output / 'plan.json', *files)
        recovery = tr.load_recovery(plan, self.output)
        approval = self.approval(plan, prior_charged_micros=recovery['prior_charged_micros'], prior_calls=1,
                                 aggregate_max_cny=10, aggregate_max_calls=10)
        with self.assertRaises(tr.TranslationError):
            tr.validate_prior_budget(approval, recovery, 10_000_000, 10)
        files[2].write_text(files[2].read_text() + '\n')
        with self.assertRaises(tr.TranslationError):
            tr.load_recovery(plan, self.output)
        self.write_rows([{**self.row, 'body': 'Changed source with milk.'}])
        new_output = self.root / 'new'
        tr.build_plan(self.corpus, new_output)
        with self.assertRaises(tr.TranslationError):
            tr.reuse_legacy(new_output / 'plan.json', *files)

    def test_legacy_content_error_cannot_be_recovered_by_only_repairing_metadata(self):
        plan = self.make_plan()
        files = self.legacy_files(plan)
        response = json.loads(files[1].read_text())
        response['choices'][0]['message']['content'] = response['choices'][0]['message']['content'].replace('2个鸡蛋', '3个鸡蛋')
        tr.atomic_json(files[1], response)
        report = tr.diagnose_legacy_response(files[0], files[1])
        self.assertFalse(report['metadata_only_recovery_possible'])
        with self.assertRaises(tr.TranslationError):
            tr.reuse_legacy(self.output / 'plan.json', *files)
        self.assertFalse((self.output / 'offline-recovery.json').exists())

    def test_v3_refuses_old_plan_and_short_index_manifest_tampering(self):
        plan = self.make_plan()
        files = self.legacy_files(plan)
        with self.assertRaises(tr.TranslationError):
            tr.load_plan(files[0])
        changed = json.loads((self.output / 'plan.json').read_text())
        changed['jobs'][0]['segments'][0]['source_key'] = 'wrong'
        changed['plan_sha256'] = tr.digest({key: value for key, value in changed.items() if key != 'plan_sha256'})
        tr.atomic_json(self.output / 'plan.json', changed)
        with self.assertRaises(tr.TranslationError):
            tr.load_plan(self.output / 'plan.json')

    def test_real_v3_guard_samples_distinguish_cream_verb_and_source_authorized_variations(self):
        # Recorded public-source fragments from the stopped v3 response. These
        # checks never read a key, contact a provider or rewrite that response.
        samples = [
            ('Cream together the butter and sugar until light and fluffy.', '将黄油和糖搅打至轻盈蓬松。'),
            ('Cream the butter and sugar together until light and fluffy.', '将黄油和糖搅打至轻盈蓬松。'),
            ('Serve in tall glasses, including fruit bits.', '盛入高脚杯中，连同水果块一起食用。'),
            ('Brandy butter can be kept for several weeks. Similar hard sauces can be made by substituting the brandy with rum.',
             '白兰地黄油可以保存数周。类似的硬酱可以通过用朗姆酒替代白兰地制作。'),
            ('Palatschinken (Czech/Austrian Crepes)', 'Palatschinken（捷克/奥地利薄饼）'),
            ('Replace part of the milk with beetroot juice to produce bright pink palatschinken.',
             '用甜菜根汁替代部分牛奶，可制作出亮粉色的palatschinken。'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('Add cream to the butter and sugar.', '加入黄油和糖。'))
        self.assertTrue(tr.validate_translation('Use butter. Replace brandy with rum.', '用植物黄油替代黄油，白兰地用朗姆酒替代。'))
        self.assertTrue(tr.validate_translation('Replace part of the milk with beetroot juice.', '用甜菜根汁替代。'))
        self.assertTrue(tr.validate_translation('Add milk.', '替代牛奶。'))
        self.assertTrue(tr.validate_translation('Cream the butter and sugar together.', '将糖搅打。'))
        self.assertTrue(tr.validate_translation('2 glasses water', '2克水'))

    def test_single_character_units_need_real_quantity_or_unit_word_boundaries(self):
        self.assertFalse(tr.validate_translation('Czech chocolate cupcakes', '捷克巧克力杯子蛋糕'))
        self.assertFalse(tr.validate_translation('100g chocolate and 2 cups water', '100克巧克力和2杯水'))
        self.assertFalse(tr.validate_translation('half pound flour', '半磅面粉'))
        self.assertTrue(tr.validate_translation('100g chocolate and 2 cups water', '100克巧克力和2克水'))

    def test_real_legacy_half_cups_and_cup_vessel_survive_new_word_boundaries(self):
        samples = [
            ('½ cup white granulated sugar', '½杯白砂糖'),
            ('2½ cups flour', '2½杯面粉'),
            ('Mix the brown sugar and the egg yolks for the sauce in a cup and put it aside.',
             '将红糖和蛋黄在杯中混合制成酱汁，放在一旁备用。'),
            ('½ cup white flour', '½杯白面粉'),
            ('½ cup whole wheat flour', '½杯全麦面粉'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('½ cup flour', '½克面粉'))
        self.assertTrue(tr.validate_translation('2½ cups flour', '½杯面粉'))
        self.assertTrue(tr.validate_translation('½ cup flour', '面粉'))
        self.assertFalse(tr.validate_translation('Czech chocolate cupcakes', '捷克巧克力杯子蛋糕'))

    def test_real_glass_vessel_cup_container_and_eg_abbreviation_are_not_missing_units(self):
        samples = [
            ('1 glass mineral water', '1杯矿泉水'),
            ('Pour into glasses or crock.', '倒入玻璃杯或陶罐中。'),
            ('Pour batter into custard cups or ramekins.', '将面糊倒入蛋奶杯或小烤盅中。'),
            ('Sprinkle the cup of reserved crumbs on top.', '在顶部撒上预留的那杯酥粒。'),
            ('Use only glass, stainless steel, or food-grade plastic.', '只使用玻璃、不锈钢或食品级塑料。'),
            ('A cup or similar as temporary egg yolk container', '一个杯子或类似容器作为临时蛋黄容器'),
            ('More syrup if using darker (e.g. 70%) chocolate.', '如果使用更黑的巧克力（如70%），则需要更多糖浆。'),
            ('Use a glass bowl.', '使用玻璃碗。'),
            ('2 glass bowls', '2个玻璃碗'),
            ('100g chocolate, e.g. 70% cocoa solids.', '100克巧克力，如70%可可固形物。'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('1 glass mineral water', '1克矿泉水'))
        self.assertTrue(tr.validate_translation('100g chocolate, e.g. 70% cocoa solids.', '100杯巧克力，如70%可可固形物。'))

    def test_mixed_vulgar_fractions_preserve_whole_quantity_and_unit_binding(self):
        self.assertFalse(tr.validate_translation('1 ½ tsp baking powder', '1½茶匙泡打粉'))
        self.assertFalse(tr.validate_translation('2½ cups flour', '2 ½杯面粉'))
        self.assertFalse(tr.validate_translation('1½ cups water', '1 1/2杯水'))
        self.assertTrue(tr.validate_translation('1½ cups water', '11/2杯水'))
        self.assertTrue(tr.validate_translation('1½ cups water', '2½杯水'))
        self.assertTrue(tr.validate_translation('1½ cups water and 2½ tsp salt', '2½杯水和1½茶匙盐'))
        self.assertTrue(tr.validate_translation('1½ cups water', '1½克水'))

    def test_real_written_measures_ordinal_second_and_black_gram_are_faithful(self):
        samples = [
            ('Spread over the dough, leaving an inch or so free on all sides.', '涂抹在面团上，四周各留出约1英寸的空白。'),
            ('Use the second half to make a second sweet bread.', '用第二半面团再做一条甜面包。'),
            ('1 tsp urad dhal (black gram)', '1茶匙黑吉豆（urad dhal）'),
            ('Heat chocolate in a two-quart saucepan; cool 5 minutes.', '在2夸脱的锅中加热巧克力；冷却5分钟。'),
            ('Wait 1 second.', '等待1秒。'),
            ('Let rise until doubled in bulk, about an hour.', '发酵至体积膨胀至原来的两倍，约1小时。'),
            ('Keep in the fridge for at least an hour. Let stand 10 minutes before serving.',
             '放入冰箱冷藏至少一小时，食用前静置10分钟。'),
            ('Let rise about an hour.', '发酵约一个小时。'),
            ('1 cup (240 g) besan or gram flour', '1杯（240克）鹰嘴豆粉'),
            ('Serve hot wine or cider in mugs.', '将热葡萄酒或热苹果酒盛入马克杯中饮用。'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('Leave an inch free.', '留出2英寸。'))
        self.assertTrue(tr.validate_translation('Use a two-quart saucepan.', '使用3夸脱的锅。'))
        self.assertTrue(tr.validate_translation('1 gram salt', '1杯盐'))
        self.assertTrue(tr.validate_translation('Wait 1 second.', '等待1小时。'))
        self.assertTrue(tr.validate_translation('Let rise about an hour.', '发酵约2小时。'))
        self.assertTrue(tr.validate_translation('1 cup (240 g) besan or gram flour', '1杯（240杯）鹰嘴豆粉'))

    def test_preflight_public_source_unit_homographs_do_not_hide_real_measurements(self):
        samples = [
            ('Place into a large glass baking dish.', '放入一个大玻璃烤盘。'),
            ('In a glass measuring cup, wait 5 seconds.', '在玻璃量杯中，等待5秒。'),
            ('1 cup rice (e.g Basmati rice)', '1杯米（如巴斯马蒂米）'),
            ('Vegetables (e. g. sabji)', '蔬菜（如沙布吉）'),
            ('14 g (½ oz) toovar dhal (also called red gram dhal)', '14克（½盎司）木豆'),
            ('½ cup besan (gram/chickpea flour)', '½杯鹰嘴豆粉'),
            ('Pound the digestive biscuits into crumbs with a rolling pin.', '用擀面杖将消化饼干捣碎。'),
            ('Put garlic in a mortar and pound until smooth.', '把蒜放入研钵中并捣至细腻。'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('1 pound biscuits', '1克饼干'))
        self.assertTrue(tr.validate_translation('14 g red gram dhal', '14杯木豆'))
        self.assertTrue(tr.validate_translation('1 glass water', '1克水'))
        self.assertTrue(tr.validate_translation('In a glass bowl, wait 5 seconds.', '在玻璃碗中等待5小时。'))

    def test_preflight_cream_of_wheat_and_egg_beater_are_not_dairy_or_egg_ingredients(self):
        self.assertFalse(tr.validate_translation('3 tbsp cream of wheat', '3汤匙粗粒小麦粉'))
        self.assertFalse(tr.validate_translation('Beat with a rotary egg beater.', '使用旋转搅拌器搅打。'))
        self.assertTrue(tr.validate_translation('Add cream of mushroom soup.', '加入蘑菇汤。'))
        self.assertTrue(tr.validate_translation('Add milk to cream of wheat.', '加入粗粒小麦粉。'))
        self.assertTrue(tr.validate_translation('Beat eggs with an egg beater.', '使用搅拌器搅打。'))
        self.assertIn('鸡蛋', tr.dangerous_concepts('Egg Beaters'))

    def test_preflight_latin_names_pound_cake_and_more_cream_verbs_keep_real_units_and_dairy(self):
        samples = [
            ('Joël Robuchon', '乔尔·罗布雄'), ('Crème Brûlée', '焦糖布丁'),
            ('Gâteau', '法式蛋糕'), ('Fërgesë', '费尔盖塞'),
            ('Traditional Pound Cake', '传统磅蛋糕'),
            ('Cream together butter and vegetable oil.', '将黄油和植物油搅打均匀。'),
            ('Cream the brown sugar and butter until fluffy.', '将红糖和黄油搅打至蓬松。'),
            ('In a bowl, cream the butter with sugar.', '在碗中将黄油和糖搅打。'),
            ('Place butter in a bowl. Cream until fluffy.', '将黄油放入碗中，搅打至蓬松。'),
            ('Cream butter, cream, and sugar together.', '将黄油、奶油和糖一起搅打。'),
            ('Bake at 350ºF.', '以350°F烘烤。'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('1 pound flour for pound cake', '1克面粉用于磅蛋糕'))
        self.assertTrue(tr.validate_translation('Cream butter, cream, and sugar together.', '将黄油和糖搅打。'))
        self.assertTrue(tr.validate_translation('Mix cream together with milk.', '与牛奶混合。'))
        self.assertTrue(tr.validate_translation('Bake at 350ºF.', '以350°C烘烤。'))

    def test_complete_written_measurements_normalize_exact_fractions_without_unit_conversion(self):
        samples = [
            ('Half a pound of flour and three-quarters of a cup of milk.', '半磅面粉和四分之三杯牛奶。'),
            ('Two pounds and a half of flour and half a pound of butter.', '两磅半面粉和半磅黄油。'),
            ('One and a half hours.', '一又二分之一小时。'),
            ('Two and a half hours.', '2个半小时。'),
            ('Two hours and a half.', '2小时半。'),
            ('Two-and-a-half hours.', '两个半小时。'),
            ('Half an hour.', '半小时。'),
            ('Three quarters of an hour.', '四分之三小时。'),
            ('Ten minutes.', '10分钟。'), ('Forty minutes.', '四十分钟。'),
            ('Two or three hours.', '2至3小时。'),
            ('Steam for a minute or two.', '焖蒸一两分钟。'),
            ('Put a glass full of water in the middle.', '在中央放一杯水。'),
            ('Pour marinade into a gallon size plastic bag. Refrigerate 1 hour, preferably 24 hours.',
             '将腌料倒入1加仑大小的塑料袋中。冷藏1小时，最好24小时。'),
            ('Blanch for a second or two.', '焯烫1至2秒。'),
            ('A cup of milk.', '一杯牛奶。'), ('One cup milk.', '1杯牛奶。'),
            ('Whisk in water, a teaspoon at a time.', '一次一茶匙地搅入水。'),
            ('Beat with a teaspoon.', '用茶匙搅打。'),
            ('Mix egg yolks in a cup.', '将蛋黄在一个杯子中混合。'),
            ('Hollow out into a cup shape.', '挖成杯状。'),
            ('One third cup flour.', '1/3杯面粉。'),
            ('½ cup flour.', '0.5杯面粉。'),
            ('1 cup semi-sweet chocolate chips', '1杯半甜巧克力豆'),
            ('1½ cups semi-sweet chocolate chips', '1杯半 半甜巧克力豆'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        for source, translated in [
                ('Half an hour.', '1小时。'),
                ('Three quarters of an hour.', '三分之二小时。'),
                ('Two pounds and a half of flour.', '两克半面粉。'),
                ('One and a half hours.', '一又三分之二小时。'),
                ('Two or three hours.', '2至4小时。'),
                ('One third cup flour.', '0.333杯面粉。'),
                ('A cup of milk.', '一杯植物奶。')]:
            with self.subTest(source=source, translated=translated):
                self.assertTrue(tr.validate_translation(source, translated))

    def test_heaping_scant_and_level_keep_amount_unit_and_qualifier_binding(self):
        samples = [
            ('2 heaping cups chopped onion', '2满杯切碎的洋葱'),
            ('1 scant cup of cream', '不足1杯奶油'),
            ('one heaping teaspoon baking powder', '一满茶匙泡打粉'),
            ('2 level cups flour', '2平杯面粉'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        for translated in ('2杯切碎的洋葱', '2平杯切碎的洋葱', '3满杯切碎的洋葱', '2满克切碎的洋葱'):
            with self.subTest(translated=translated):
                self.assertTrue(tr.validate_translation('2 heaping cups chopped onion', translated))
        self.assertTrue(tr.validate_translation('2 heaping cups flour and 1 scant cup water',
                                               '2略少杯面粉和1满杯水'))

    def test_real_dimensions_and_parenthetical_alternative_oven_time_keep_values(self):
        samples = [
            ('Line a 12-by-17-inch sheet pan.', '在12×17英寸的烤盘上铺纸。'),
            ('1 half sheet pan (12-by-17 inches)', '1个半烤盘（12×17英寸）'),
            ('2 ea. 18 cm (7 in) circular cake pans', '2个18厘米（7英寸）圆形蛋糕模具'),
            ('Bake for 20–30 minutes (10–15 in a fan-assisted oven).', '烘烤20–30分钟（风扇辅助烤箱10–15分钟）。'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('12-by-17-inch pan', '12×18英寸烤盘'))
        self.assertTrue(tr.validate_translation('18 cm (7 in) pan', '18英寸（7厘米）烤盘'))
        self.assertTrue(tr.validate_translation('Bake for 20–30 minutes (10–15 in a fan-assisted oven).',
                                               '烘烤20–30分钟（风扇辅助烤箱10–15小时）。'))

    def test_plural_tbsp_tsp_abbreviations_keep_amount_and_unit(self):
        self.assertFalse(tr.validate_translation('3 tbsps of mayonnaise', '3汤匙蛋黄酱'))
        self.assertFalse(tr.validate_translation('2 tsps sugar', '2茶匙糖'))
        self.assertTrue(tr.validate_translation('3 tbsps of mayonnaise', '3茶匙蛋黄酱'))
        self.assertTrue(tr.validate_translation('2 tsps sugar', '3茶匙糖'))

    def test_real_unmeasured_cup_pronouns_calendar_month_and_teacup_are_not_changed_quantities(self):
        samples = [
            ('Spoon flour into the measuring cup instead of digging it into flour. Scrape the cup.',
             '将面粉舀入量杯，不要把量杯插入面粉。刮平量杯。'),
            ('It appeared in November 2006.', '它出现在2006年11月。'),
            ('½ teacup water', '½茶杯水'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('1 cup water', '1克水'))
        self.assertTrue(tr.validate_translation('1 cup water', '1份水'))
        self.assertTrue(tr.validate_translation('½ teacup water', '½杯水'))
        self.assertTrue(tr.validate_translation('It appeared in November 2006.', '它出现在2006年12月。'))

    def test_non_latin_human_text_requires_chinese_and_exact_structural_annotation_is_source_bound(self):
        self.assertTrue(tr.validate_translation('Рецепт:Салат', 'Рецепт:Салат'))
        self.row['steps'].append('fr:Livre de cuisine/Bison à la grecque')
        self.write_rows([self.row]);plan = self.make_plan();job = plan['jobs'][0]
        sid = next(s['segment_id'] for s in job['segments'] if s['path'] == ['steps', 1])
        source = self.row['steps'][1];annotation = '来源附带的其他语言菜谱互链（不是烹饪步骤）：' + source + '。'
        manifest = {'operation': 'root_ai_structural_annotation_candidates', 'source_corpus_sha256': plan['corpus_sha256'],
            'count': 1, 'publication_approved': False, 'source_text_modified': False, 'entries': [
                {'source_id': self.row['source_id'], 'source_revision': self.row['source_revision'],
                 'field': 'steps', 'index': 1, 'source_text': source,
                 'source_text_sha256': tr.hashlib.sha256(source.encode()).hexdigest(),
                 'annotation': annotation, 'quality_flag': 'source_parser_artifact_unreviewed'}]}
        path = self.root / 'annotations.json';tr.atomic_json(path, manifest)
        tr.attach_approved_source_annotations(plan, path)
        self.assertIn(sid, job['_approved_source_annotations'])
        self.cn[source] = source
        with self.assertRaises(tr.TranslationError):tr.validate_response(job, self.response_content(job))
        self.cn[source] = annotation
        self.assertEqual(len(tr.validate_response(job, self.response_content(job))['translations']), len(job['segments']))
        self.cn[source] = '任意其他中文说明'
        with self.assertRaises(tr.TranslationError):tr.validate_response(job, self.response_content(job))
        manifest['entries'][0]['source_revision'] = 'changed'
        tr.atomic_json(path, manifest)
        with self.assertRaises(tr.TranslationError):tr.attach_approved_source_annotations(plan, path)

    def test_exact_preapproved_pointer_recovery_preserves_original_fee_and_other_errors_stop(self):
        source = 'fr:Livre de cuisine/Bison à la grecque'
        self.row['steps'].append(source)
        self.cn[source] = source
        self.write_rows([self.row]);plan = self.make_plan()
        annotation = '来源附带的其他语言菜谱互链（不是烹饪步骤）：' + source + '。'
        manifest = {'operation': 'root_ai_structural_annotation_candidates', 'source_corpus_sha256': plan['corpus_sha256'],
            'count': 1, 'publication_approved': False, 'source_text_modified': False, 'entries': [
                {'source_id': self.row['source_id'], 'source_revision': self.row['source_revision'],
                 'field': 'steps', 'index': 1, 'source_text': source,
                 'source_text_sha256': tr.hashlib.sha256(source.encode()).hexdigest(),
                 'annotation': annotation, 'quality_flag': 'source_parser_artifact_unreviewed'}]}
        path = self.root / 'annotations.json';tr.atomic_json(path, manifest)
        job, response_path = self.quarantined_fixture(plan, corrupt=True)
        before = (self.output / 'ledger.json').read_bytes();raw = response_path.read_bytes()
        with self.assertRaises(tr.TranslationError):
            tr.annotate_quarantined(self.output / 'plan.json', response_path, path)
        self.assertEqual((self.output / 'ledger.json').read_bytes(), before)
        self.assertEqual(response_path.read_bytes(), raw)
        self.assertFalse((self.output / 'corrections').exists())
        # Separate authentic-protocol fixture: only the approved structural
        # pointer is wrong, so recovery may annotate that pointer alone.
        self.output = self.root / 'recovery';plan = self.make_plan()
        job, response_path = self.quarantined_fixture(plan)
        before = json.loads((self.output / 'ledger.json').read_text());raw = response_path.read_bytes()
        result = tr.annotate_quarantined(self.output / 'plan.json', response_path, path)
        verified = tr.load_plan(self.output / 'plan.json', annotation_manifest_path=path)
        after = tr.Ledger(self.output / 'ledger.json', verified, 10_000_000, 10).state
        self.assertEqual(result['new_api_calls'], 0)
        self.assertEqual(after['charged_micros'], before['charged_micros'])
        self.assertEqual(after['calls'], before['calls'])
        self.assertEqual(response_path.read_bytes(), raw)
        row = next(tr.read_jsonl(self.output / 'translated.jsonl'))
        self.assertEqual(row['steps'][1], annotation)
        self.assertIn('source_parser_artifact_unreviewed', row['source_meta']['translation_quality_flags'])
        self.assertFalse(row['publication_approved'])

    def test_real_buttermilk_and_egg_white_synonyms_keep_actual_dairy_and_egg_evidence(self):
        samples = [
            ('You can substitute 1 tablespoon of lemon juice in milk for the buttermilk.',
             '可用1汤匙柠檬汁加入牛奶中代替酪乳。'),
            ('4 egg whites', '4个蛋白'),
            ('Separate egg whites from egg yolks. Make sure there is no trace of yolk in the whites.',
             '将蛋白与蛋黄分离。确保蛋白中没有一丝蛋黄。'),
            ('While baking filling, whip egg whites until soft peaks form.',
             '在烤馅料的同时，搅打蛋白，直到形成软性尖峰。'),
            ('You can vary the amounts, or even substitute low-fat ingredients according to your personal taste.',
             '你可以根据个人口味改变用量，甚至替换为低脂原料。'),
            ('1 cup heavy whipping cream or ½ cup whole milk, hot', '1杯重奶油或½杯全脂牛奶，加热'),
            ('Serve with grated Parmesan or hard Edam sprinkled over it.', '食用时撒上磨碎的帕玛森奶酪或硬质伊丹奶酪。'),
            ('4 ounces ricotta cheese, room temperature', '4盎司里科塔奶酪，室温'),
            ('1 pint vanilla ice cream, softened', '1品脱香草冰淇淋，软化'),
        ]
        for source, translated in samples:
            with self.subTest(source=source):
                self.assertFalse(tr.validate_translation(source, translated))
        for substituted in ('4个植物蛋白', '4个大豆蛋白', '4个豌豆蛋白', '4个蛋白粉', '4个蛋白质'):
            with self.subTest(substituted=substituted):
                self.assertIn('dangerous_ingredient_omitted:蛋清', tr.validate_translation('4 egg whites', substituted))
        self.assertTrue(tr.validate_translation('Add buttermilk.', '加入植物奶。'))
        self.assertTrue(tr.validate_translation('1 cup heavy whipping cream', '1杯植物奶油'))
        self.assertTrue(tr.validate_translation('4 ounces ricotta cheese', '4盎司植物里科塔奶酪'))
        self.assertTrue(tr.validate_translation('Parmesan cheese', '纯素奶酪'))
        self.assertTrue(tr.validate_translation('1 pint vanilla ice cream', '1品脱植物冰淇淋'))
        self.assertTrue(tr.validate_translation('Ice cream and cream', '冰淇淋'))
        self.assertTrue(tr.validate_translation('1 pint vanilla ice cream', '1杯香草冰淇淋'))

    def test_real_cake_batter_cup_and_one_additional_spoon_are_exact_measurements(self):
        cases = [
            ('Stir 1 cup cake batter into cooled melted chocolate; set aside.',
             '将1杯蛋糕面糊拌入冷却的融化巧克力中；备用。'),
            ('If it is too thick, whisk in another tablespoon of coffee.',
             '如果太稠，再搅打入一汤匙咖啡。备用。'),
            ('Pour into another cup.', '倒入另一个杯子中。'),
            ('1 cupcake', '1个杯子蛋糕'),
        ]
        for source, translated in cases:
            with self.subTest(source=source):self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('1 cup cake batter', '2杯蛋糕面糊'))
        self.assertTrue(tr.validate_translation('1 cup cake batter', '1杯子蛋糕'))
        self.assertTrue(tr.validate_translation('another tablespoon of coffee', '两汤匙咖啡'))
        self.assertTrue(tr.validate_translation('another tablespoon of coffee', '一茶匙咖啡'))

    def test_real_additional_minutes_and_numeric_inches_before_dimension_nouns(self):
        cases = [('Add the powdered sugar and whip for another minute.', '加入糖粉，再搅打一分钟。'),
                 ('Whip on high until stiff peaks form, about another minute.', '高速搅打至形成硬性尖峰，约再搅打一分钟。'),
                 ('Roll out the dough to a 0.25 in thickness.', '将面团擀至0.25英寸厚。'),
                 ('Use 5.5 in cookie cutters to cut out cookies.', '使用5.5英寸的饼干模具切出饼干。'),
                 ('Place 5 in the pan.', '将5个放入锅中。')]
        for source, translated in cases:
            with self.subTest(source=source):self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('whip for another minute', '再搅打两分钟'))
        self.assertTrue(tr.validate_translation('0.25 in thickness', '0.25厘米厚'))
        self.assertTrue(tr.validate_translation('5.5 in cookie cutters', '5英寸饼干模具'))

    def test_real_cake_flour_and_misordered_sugar_description_keep_literal_units(self):
        self.assertFalse(tr.validate_translation('2 cups cake flour', '2杯蛋糕粉'))
        self.assertFalse(tr.validate_translation('1 cup plus 2 white granulated tablespoons sugar', '1杯加2汤匙白砂糖'))
        self.assertTrue(tr.validate_translation('2 cups cake flour', '2个杯子蛋糕'))
        self.assertTrue(tr.validate_translation('1 cup plus 2 white granulated tablespoons sugar', '2杯加1汤匙白砂糖'))
        self.assertTrue(tr.validate_translation('1 cup plus 2 white granulated tablespoons sugar', '1杯加2茶匙白砂糖'))

    def test_actual_parmesan_dry_cheese_and_sugar_substitute_synonyms_are_distinct_claims(self):
        for source, translated in [('¼ cup grated Parmesan cheese', '¼杯磨碎的帕尔马干酪'),
                                   ('Sprinkle buttered muffins with Parmesan cheese.', '在涂了黄油的松饼上撒帕尔马干酪。'),
                                   ('6 tablespoons sugar substitute', '6汤匙代糖'),
                                   ('Add vegan milk and sugar substitute.', '加入纯素奶和代糖。')]:
            with self.subTest(source=source):self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('Parmesan cheese', '植物干酪'))
        self.assertTrue(tr.validate_translation('6 tablespoons sugar substitute', '6汤匙糖'))
        self.assertTrue(tr.validate_translation('Add vegan milk and sugar substitute.', '加入牛奶和代糖。'))

    def test_real_explicit_chinese_range_and_inch_pancake_dimensions(self):
        for source, translated in [
            ('stirring in a teaspoon or two of water', '搅入一或两茶匙水'),
            ('8 ea. 4 in pancakes', '8个4英寸煎饼'),
        ]:
            with self.subTest(source=source):self.assertFalse(tr.validate_translation(source, translated))
        self.assertTrue(tr.validate_translation('a teaspoon or two of water', '一或三茶匙水'))
        self.assertTrue(tr.validate_translation('a teaspoon or two of water', '一或两汤匙水'))
        self.assertTrue(tr.validate_translation('8 ea. 4 in pancakes', '8个4厘米煎饼'))
        self.assertTrue(tr.validate_translation('8 ea. 4 in pancakes', '4个8英寸煎饼'))

    def quarantined_fixture(self, plan, *, origin='deepseek_https_200', corrupt=False, job=None):
        job = job or plan['jobs'][0]
        ledger = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)
        ledger.reserve(job, request_origin=origin)
        content = self.response_content(job)
        if corrupt:
            content = content.replace('2个鸡蛋', '3个鸡蛋')
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}
        path = self.output / 'responses' / (job['job_id'] + '.json')
        tr.atomic_json(path, response)
        ledger.finish(job, 'quarantined', reason='old_guard_false_positive_fixture')
        return job, path

    def test_adoption_audits_legacy_recovery_and_half_cup_fragments_before_current_job(self):
        self.row.update(ingredients=['½ cup flour', '2½ cups flour'],
                        steps=['Mix egg yolks in a cup.'])
        self.cn.update({'½ cup flour': '½杯面粉', '2½ cups flour': '2½杯面粉',
                        'Mix egg yolks in a cup.': '将蛋黄放在杯中混合。'})
        self.write_rows([self.row])
        plan = self.make_plan(max_segments=4)
        legacy = self.legacy_files(plan, plan['jobs'][0]['segments'])
        legacy_bytes = [path.read_bytes() for path in legacy]
        tr.reuse_legacy(self.output / 'plan.json', *legacy)
        job, response_path = self.quarantined_fixture(plan, job=plan['jobs'][1])
        before = json.loads((self.output / 'ledger.json').read_text())
        result = tr.adopt_quarantined(self.output / 'plan.json', response_path,
            real_response_attestation='Offline protocol fixture with unchanged legacy checkpoint, no real API.')
        restored = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)
        self.assertEqual(result['status'], 'offline_verified_from_quarantine')
        self.assertEqual(restored.state['charged_micros'], before['charged_micros'])
        self.assertEqual(restored.state['calls'], before['calls'])
        self.assertEqual(restored.state['jobs'][plan['jobs'][0]['job_id']]['status'], 'offline_recovered')
        self.assertEqual([path.read_bytes() for path in legacy], legacy_bytes)

    def test_adopt_quarantined_preserves_full_charge_response_and_old_status_audit_and_skips_reissue(self):
        plan = self.make_plan()
        job, response_path = self.quarantined_fixture(plan)
        original_response = response_path.read_bytes()
        prior = json.loads((self.output / 'ledger.json').read_text())
        result = tr.adopt_quarantined(self.output / 'plan.json', response_path,
                                     real_response_attestation='Isolated unit-test provider-protocol fixture; no real request.')
        after = json.loads((self.output / 'ledger.json').read_text())
        self.assertEqual(result['status'], 'offline_verified_from_quarantine')
        self.assertEqual(after['charged_micros'], prior['charged_micros'])
        self.assertEqual(after['reserved_micros'], prior['reserved_micros'])
        self.assertEqual(after['calls'], prior['calls'])
        self.assertEqual(response_path.read_bytes(), original_response)
        audit = json.loads((self.output / 'adoptions' / (job['job_id'] + '.json')).read_text())
        self.assertEqual(audit['prior_entry']['status'], 'quarantined')
        self.assertEqual(audit['prior_entry']['reason'], 'old_guard_false_positive_fixture')
        self.assertEqual(audit['validator_version'], tr.FIDELITY_VERSION)
        self.assertTrue(audit['validated_at'])
        self.assertFalse(audit['reservation_released'])
        self.assertEqual(len(list(tr.read_jsonl(self.output / 'translated.jsonl'))), 1)
        tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)
        approval, env = self.fake_runner_inputs(plan)
        tr.run_paid(self.output / 'plan.json', approval, env,
                    transport=lambda _: self.fail('An adopted complete response was charged again'))
        replay = tr.adopt_quarantined(self.output / 'plan.json', response_path, real_response_attestation='Idempotent replay')
        self.assertTrue(replay['idempotent'])
        after['charged_micros'] = 0
        after['jobs'][job['job_id']]['charged_micros'] = 0
        tr.atomic_json(self.output / 'ledger.json', after)
        with self.assertRaises(tr.TranslationError):
            tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)

    def test_adopt_quarantined_requires_attestation_and_rejects_known_injected_or_still_invalid_content(self):
        plan = self.make_plan()
        _, response_path = self.quarantined_fixture(plan, corrupt=True)
        prior = (self.output / 'ledger.json').read_bytes()
        for evidence in ('', 'Observed complete response in isolated protocol fixture'):
            with self.subTest(evidence=evidence), self.assertRaises(tr.TranslationError):
                tr.adopt_quarantined(self.output / 'plan.json', response_path, real_response_attestation=evidence)
        self.assertEqual((self.output / 'ledger.json').read_bytes(), prior)
        self.assertFalse((self.output / 'validated').exists())
        self.assertFalse((self.output / 'adoptions').exists())
        state = json.loads(prior)
        jid = next(iter(state['jobs']))
        state['jobs'][jid]['request_origin'] = 'injected_test_transport'
        tr.atomic_json(self.output / 'ledger.json', state)
        with self.assertRaises(tr.TranslationError):
            tr.adopt_quarantined(self.output / 'plan.json', response_path,
                                 real_response_attestation='A fake transport cannot be attested into a real call')

    def editorial_overlay(self, plan, job, response_path, changes):
        response = json.loads(response_path.read_text())
        original = tr.map_wire_response(job, response['choices'][0]['message']['content'])
        segments = {s['segment_id']: s for s in job['segments']}
        rows = {s['segment_id']: s for s in original['translations']}
        return {'format': 'recipe-segment-corrections-v1', 'plan_sha256': plan['plan_sha256'],
                'job_id': job['job_id'], 'response_sha256': tr.digest(response),
                'response_file_sha256': tr.hashlib.sha256(response_path.read_bytes()).hexdigest(),
                'operator': 'offline-editor-test', 'source_evidence': 'Public-source protocol fixture; no real API.',
                'created_at': datetime.now(timezone.utc).isoformat(),
                'quality_flags': ['machine_translation_unreviewed'], 'publication_approved': False,
                'corrections': [{'segment_id': sid, 'source_key': segments[sid]['source_key'],
                    'source_text_sha256': tr.digest(segments[sid]['text']),
                    'original_text': rows[sid]['text'], 'original_text_sha256': tr.digest(rows[sid]['text']),
                    'corrected_text': text, 'corrected_text_sha256': tr.digest(text),
                    'reason': 'Explicit source-checked editorial correction in isolated fixture'} for sid, text in changes.items()]}

    def test_editorial_overlay_keeps_raw_response_fee_source_and_unreviewed_quality(self):
        plan = self.make_plan()
        self.cn['Rice'] = 'Rice'
        job, response_path = self.quarantined_fixture(plan)
        sid = next(s['segment_id'] for s in job['segments'] if s['text'] == 'Rice')
        overlay = self.editorial_overlay(plan, job, response_path, {sid: '米饭（Rice，原名）'})
        overlay['quality_flags'].append('source_parser_artifact_unreviewed')
        path = self.root / 'editorial.json'; tr.atomic_json(path, overlay)
        raw = response_path.read_bytes(); before = json.loads((self.output / 'ledger.json').read_text())
        with self.assertRaises(tr.TranslationError):
            tr.adopt_quarantined(self.output / 'plan.json', response_path, real_response_attestation='Offline fixture')
        tr.adopt_quarantined(self.output / 'plan.json', response_path, real_response_attestation='Offline source checked',
                            corrections_path=path)
        ledger = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)
        self.assertEqual(ledger.state['charged_micros'], before['charged_micros'])
        self.assertEqual(ledger.state['calls'], before['calls'])
        self.assertEqual(response_path.read_bytes(), raw)
        result = list(tr.read_jsonl(self.output / 'translated.jsonl'))[0]
        self.assertEqual(result['title'], '米饭（Rice，原名）')
        self.assertEqual(result['source_meta']['original_content']['title'], 'Rice')
        self.assertIn('machine_translation_unreviewed', result['source_meta']['translation_quality_flags'])
        self.assertIn('source_parser_artifact_unreviewed', result['source_meta']['translation_quality_flags'])
        self.assertIn('source_parser_artifact_unreviewed', result['translation_quality_flags'])
        self.assertEqual(result['source_meta']['translation_corrections'][0]['segment_id'], sid)
        audit_path = self.output / 'adoptions' / (job['job_id'] + '.json')
        audit = json.loads(audit_path.read_text())
        self.assertEqual(audit['correction_overlay'], overlay)
        audit['correction_overlay']['corrections'][0]['corrected_text'] = '被篡改的标题'
        tr.atomic_json(audit_path, audit)
        with self.assertRaises(tr.TranslationError):
            tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)

    def test_editorial_overlay_rejects_wrong_mapping_hash_approval_and_bad_content(self):
        plan = self.make_plan()
        job, response_path = self.quarantined_fixture(plan)
        sid = next(s['segment_id'] for s in job['segments'] if s['text'] == 'Rice')
        overlay = self.editorial_overlay(plan, job, response_path, {sid: '米饭（原名Rice）'})
        response = json.loads(response_path.read_text())
        for key, value in [('plan_sha256', 'bad'), ('response_file_sha256', 'bad'), ('operator', ''),
                           ('quality_flags', []), ('quality_flags', ['machine_translation_unreviewed', 'invented_clearance']),
                           ('publication_approved', True)]:
            bad = json.loads(json.dumps(overlay)); bad[key] = value
            with self.subTest(key=key), self.assertRaises(tr.TranslationError):
                tr.validate_corrected_provider_response(job, response, bad, plan['plan_sha256'], overlay['response_file_sha256'])
        for key in ('source_key', 'source_text_sha256', 'original_text', 'original_text_sha256', 'corrected_text_sha256'):
            bad = json.loads(json.dumps(overlay)); bad['corrections'][0][key] = 'bad'
            with self.subTest(key=key), self.assertRaises(tr.TranslationError):
                tr.validate_corrected_provider_response(job, response, bad, plan['plan_sha256'], overlay['response_file_sha256'])
        egg = next(s['segment_id'] for s in job['segments'] if s['text'] == '2 eggs')
        for text in ('3个鸡蛋', '2个豆子', 'eggs'):
            bad = self.editorial_overlay(plan, job, response_path, {egg: text})
            with self.subTest(text=text), self.assertRaises(tr.TranslationError):
                tr.validate_corrected_provider_response(job, response, bad, plan['plan_sha256'], overlay['response_file_sha256'])


if __name__ == '__main__':
    unittest.main()

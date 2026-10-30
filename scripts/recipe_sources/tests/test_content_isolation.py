"""Offline batch isolation, proof integrity, fee caps and no-repeat regressions."""
import copy
import json
import unittest
from unittest.mock import patch

from scripts import translate_recipe_corpus as tr
from scripts import import_recipe_translations as importer
from scripts.recipe_sources.tests import test_translation as fixtures


class ContentIsolationTests(unittest.TestCase):
    setUp = fixtures.TranslationTests.setUp
    write_rows = fixtures.TranslationTests.write_rows
    make_plan = fixtures.TranslationTests.make_plan
    response_content = fixtures.TranslationTests.response_content
    approval = fixtures.TranslationTests.approval
    fake_runner_inputs = fixtures.TranslationTests.fake_runner_inputs

    def inputs(self, count=3, **budget):
        self.write_rows([{**self.row, 'source_id': 'test:' + str(i)} for i in range(count)])
        size = len(list(tr.walk_strings(tr.human_fields(self.row))))
        plan = self.make_plan(max_segments=size * 2)
        approval, env = self.fake_runner_inputs(plan, content_quality_policy=tr.CONTENT_POLICY,
            content_policy_authorization_evidence='offline explicit test authorization', **budget)
        return plan, approval, env

    def transport(self, plan, calls, *, malformed=False, bad_usage=False, throw=False):
        def run(body):
            wire = json.loads(body['messages'][-1]['content'])['segments']
            job = next(j for j in plan['jobs'] if j['segments'][0]['wire_id'] == wire[0]['id'])
            calls.append(job['job_id'])
            if throw:
                raise TimeoutError('synthetic transport failure')
            rows = [{'id': s['wire_id'], 'text': self.cn[s['text']]} for s in job['segments']]
            for row, segment in zip(rows, job['segments']):
                if segment['source_key'] == plan['sources'][1]['source_key'] and segment['text'] == '2 eggs':
                    row['text'] = '3个鸡蛋'  # Must be quarantined, never accepted.
            if malformed:
                rows.append(rows[0])
            usage = {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}
            if bad_usage:
                usage['total_tokens'] = 1
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': tr.canonical({'translations': rows})}}],
                    'usage': usage}
        return run

    def run_fixture(self, plan, approval, env, calls, **kwargs):
        with patch('builtins.print'):
            return tr.run_paid(self.output / 'plan.json', approval, env,
                transport=self.transport(plan, calls), isolate_content_errors=True, **kwargs)

    def test_suspect_source_isolated_clean_same_batch_and_later_source_survive(self):
        plan, approval, env = self.inputs()
        calls = []
        result = self.run_fixture(plan, approval, env, calls)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['complete_sources'], 2)
        rows = list(tr.read_jsonl(self.output / 'translated.jsonl'))
        self.assertEqual({r['source_id'] for r in rows}, {'test:0', 'test:2'})
        self.assertTrue(all(r['status'] == 'draft' and not r['publication_approved'] for r in rows))
        bundle = importer.load_inputs(self.output / 'plan.json', self.output / 'translated.jsonl')
        self.assertEqual(len(bundle['rows']), 2)
        before = (self.output / 'ledger.json').read_bytes()
        self.run_fixture(plan, approval, env, calls)
        self.assertEqual(len(calls), 2)  # Suspect content is never automatically retranslated.
        self.assertEqual(before, (self.output / 'ledger.json').read_bytes())

    def test_isolation_requires_explicit_policy_before_loading_key(self):
        plan, approval, env = self.inputs()
        value = json.loads(approval.read_text())
        value.pop('content_quality_policy')
        approval.write_text(tr.canonical(value))
        with patch('dotenv.dotenv_values', side_effect=AssertionError('Key was read')):
            with self.assertRaises(tr.TranslationError):
                self.run_fixture(plan, approval, env, [])

    def test_default_policy_still_stops_entire_run_on_content_error(self):
        plan, approval, env = self.inputs()
        calls = []
        with self.assertRaises(tr.TranslationError), patch('builtins.print'):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=self.transport(plan, calls))
        self.assertEqual(len(calls), 1)

    def test_protocol_usage_and_transport_errors_still_stop_without_followup(self):
        for variant in ('malformed', 'bad_usage', 'throw'):
            with self.subTest(variant=variant):
                self.setUp()
                plan, approval, env = self.inputs()
                calls = []
                with self.assertRaises(tr.TranslationError), patch('builtins.print'):
                    tr.run_paid(self.output / 'plan.json', approval, env, isolate_content_errors=True,
                        transport=self.transport(plan, calls, **{variant: True}))
                self.assertEqual(len(calls), 1)
                state = json.loads((self.output / 'ledger.json').read_text())
                self.assertEqual(state['charged_micros'], state['reserved_micros'])

    def test_preexisting_content_failure_reuses_response_and_keeps_old_full_fee(self):
        plan, approval, env = self.inputs()
        calls = []
        with self.assertRaises(tr.TranslationError), patch('builtins.print'):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=self.transport(plan, calls))
        original = (self.output / 'responses' / (calls[0] + '.json')).read_bytes()
        old = json.loads((self.output / 'ledger.json').read_text())
        self.run_fixture(plan, approval, env, calls)
        current = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10).state
        first = current['jobs'][calls[0]]
        self.assertEqual(first['status'], 'content_quarantined')
        self.assertEqual(first['charged_micros'], old['jobs'][calls[0]]['charged_micros'])
        self.assertEqual(original, (self.output / 'responses' / (calls[0] + '.json')).read_bytes())
        self.assertEqual(len(calls), 2)

    def test_injected_partial_response_cannot_release_budget(self):
        plan, approval, env = self.inputs()
        calls = []
        self.run_fixture(plan, approval, env, calls)
        state = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10).state
        self.assertEqual(state['charged_micros'], state['reserved_micros'])
        first = next(e for e in state['jobs'].values() if e['status'] == 'content_quarantined')
        self.assertGreater(first['charged_micros'], first['peak_usage_micros'])

    def test_tampered_partial_proof_and_raw_response_fail_readback(self):
        plan, approval, env = self.inputs()
        self.run_fixture(plan, approval, env, [])
        audit_path = next((self.output / 'content-quarantines').glob('*.json'))
        raw = audit_path.read_bytes()
        value = json.loads(raw)
        value['accepted']['translations'][0]['text'] = '伪造内容'
        audit_path.write_text(tr.canonical(value))
        with self.assertRaises(tr.TranslationError):
            tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)
        audit_path.write_bytes(raw)
        response_path = self.output / 'responses' / audit_path.name
        response_path.write_bytes(response_path.read_bytes() + b' ')
        with self.assertRaises(tr.TranslationError):
            tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)

    def test_content_isolation_does_not_bypass_whole_group_budget(self):
        plan, approval, env = self.inputs(max_cny='0.000001')
        calls = []
        with self.assertRaises(tr.TranslationError):
            self.run_fixture(plan, approval, env, calls, concurrency=3)
        self.assertFalse(calls)

    def test_verified_http_partial_usage_settles_at_peak_but_remains_unpublished(self):
        plan, approval, env = self.inputs()
        calls = []
        transport = self.transport(plan, calls)

        class Response:
            status_code = 200

            def __init__(self, body):
                self.body = body

            def json(self):
                return transport(self.body)

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, endpoint, *, headers, json):
                return Response(json)

        with patch('httpx.Client', Client), patch('builtins.print'):
            result = tr.run_paid(self.output / 'plan.json', approval, env, isolate_content_errors=True)
        self.assertEqual(result['charged_micros'], 2000)
        self.assertFalse(result['publication_approved'])
        ledger = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)
        self.assertEqual(ledger.state['charged_micros'], 2000)
        state = copy.deepcopy(ledger.state)
        entry = next(e for e in state['jobs'].values() if e['status'] == 'content_quarantined')
        state['charged_micros'] -= entry['charged_micros']
        entry['charged_micros'] = 0
        tr.atomic_json(self.output / 'ledger.json', state)
        with self.assertRaises(tr.TranslationError):
            tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)

    def test_concurrent_protocol_failure_drains_only_already_sent_group(self):
        plan, approval, env = self.inputs(count=9)
        calls = []
        with self.assertRaises(tr.TranslationError), patch('builtins.print'):
            tr.run_paid(self.output / 'plan.json', approval, env, isolate_content_errors=True,
                concurrency=3, transport=self.transport(plan, calls, malformed=True))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(list((self.output / 'responses').glob('*.json'))), 3)

    def test_nonstring_wire_text_and_duplicate_ids_are_fatal(self):
        plan, _, _ = self.inputs()
        job = plan['jobs'][0]
        response = self.transport(plan, [])(tr.request_body(job))
        for item in (None, 12, {'text': '未知'}):
            changed = copy.deepcopy(response)
            content = json.loads(changed['choices'][0]['message']['content'])
            content['translations'][0]['text'] = item
            changed['choices'][0]['message']['content'] = tr.canonical(content)
            with self.assertRaises(tr.TranslationError):
                tr.partition_provider_response(job, changed)

    def test_omitted_content_has_no_fabricated_checkpoint_or_complete_source(self):
        plan, approval, env = self.inputs()
        base = self.transport(plan, [])

        def missing(body):
            response = base(body)
            content = json.loads(response['choices'][0]['message']['content'])
            # Remove one segment of the second recipe, leaving recipe zero intact.
            content['translations'].pop(-1)
            response['choices'][0]['message']['content'] = tr.canonical(content)
            return response

        with patch('builtins.print'):
            tr.run_paid(self.output / 'plan.json', approval, env, transport=missing, isolate_content_errors=True)
        rows = list(tr.read_jsonl(self.output / 'translated.jsonl'))
        self.assertEqual([r['source_id'] for r in rows], ['test:0'])
        ledger = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)
        for entry in ledger.state['jobs'].values():
            self.assertEqual(entry['status'], 'content_quarantined')
            self.assertEqual(entry['charged_micros'], entry['reserved_micros'])
        bundle = importer.load_inputs(self.output / 'plan.json', self.output / 'translated.jsonl')
        self.assertEqual(len(bundle['rows']), 1)

    def test_wrong_or_duplicate_id_cannot_use_missing_content_path(self):
        plan, _, _ = self.inputs()
        job = plan['jobs'][0]
        response = self.transport(plan, [])(tr.request_body(job))
        for wrong_id in (True, -1, '1', job['segments'][0]['wire_id']):
            changed = copy.deepcopy(response)
            content = json.loads(changed['choices'][0]['message']['content'])
            content['translations'][-1]['id'] = wrong_id
            changed['choices'][0]['message']['content'] = tr.canonical(content)
            with self.assertRaises(tr.TranslationError):
                tr.partition_provider_response(job, changed)

    def test_cached_fidelity_findings_cannot_be_mutated_or_cross_contaminated(self):
        first = tr.validate_translation('2 eggs', '3个鸡蛋')
        self.assertIn('numeric_values_changed', first)
        first.clear()
        self.assertIn('numeric_values_changed', tr.validate_translation('2 eggs', '3个鸡蛋'))
        self.assertEqual(tr.validate_translation('2 eggs', '2个鸡蛋'), [])
        self.assertEqual(tr.validate_translation('2 eggs', {'text': '2个鸡蛋'}), ['empty_or_nonstring_translation'])

    def protocol_fixture(self, plan):
        job = plan['jobs'][0]
        ledger = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)
        ledger.reserve(job, request_origin='deepseek_https_200')  # Synthetic receipt-shaped fixture only.
        response = self.transport(plan, [], malformed=True)(tr.request_body(job))
        path = self.output / 'responses' / (job['job_id'] + '.json')
        tr.atomic_json(path, response)
        ledger.finish(job, 'quarantined', response_sha256=tr.digest(response),
            response_file_sha256=tr.hashlib.sha256(path.read_bytes()).hexdigest())
        return ledger, job, path

    def test_explicit_protocol_discard_keeps_full_fee_reuses_no_text_and_allows_remaining_calls(self):
        plan, approval, env = self.inputs()
        ledger, job, path = self.protocol_fixture(plan)
        before = path.read_bytes()
        with self.assertRaises(tr.TranslationError):
            ledger.discard_protocol(job, '')
        ledger.discard_protocol(job, 'offline operator observed duplicate ID; discard whole response, no retry')
        saved = tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10).state['jobs'][job['job_id']]
        self.assertEqual(saved['charged_micros'], saved['reserved_micros'])
        calls = []
        self.run_fixture(plan, approval, env, calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(path.read_bytes(), before)
        rows = list(tr.read_jsonl(self.output / 'translated.jsonl'))
        self.assertEqual([r['source_id'] for r in rows], ['test:2'])
        bundle = importer.load_inputs(self.output / 'plan.json', self.output / 'translated.jsonl')
        self.assertEqual(len(bundle['rows']), 1)

    def test_protocol_discard_cannot_accept_any_content_or_release_money(self):
        plan, _, _ = self.inputs()
        ledger, job, _ = self.protocol_fixture(plan)
        ledger.discard_protocol(job, 'synthetic-only diagnosis')
        audit_path = self.output / 'discarded' / (job['job_id'] + '.json')
        value = json.loads(audit_path.read_text())
        value['accepted_segments'] = 1
        tr.atomic_json(audit_path, value)
        with self.assertRaises(tr.TranslationError):
            tr.Ledger(self.output / 'ledger.json', plan, 10_000_000, 10)

    def test_reordered_fidelity_findings_do_not_break_immutable_restart_proof(self):
        plan, approval, env = self.inputs()
        base = self.transport(plan, [])
        def multiple_findings(body):
            response = base(body)
            content = json.loads(response['choices'][0]['message']['content'])
            for row in content['translations']:
                if row['text'] == '3个鸡蛋':
                    row['text'] = '3杯大米'
            response['choices'][0]['message']['content'] = tr.canonical(content)
            return response
        with patch('builtins.print'):
            tr.run_paid(self.output / 'plan.json', approval, env,
                transport=multiple_findings, isolate_content_errors=True)
        audit_path = next((self.output / 'content-quarantines').glob('*.json'))
        self.assertGreaterEqual(len(json.loads(audit_path.read_text())['quality_errors']), 2)
        job = next(j for j in plan['jobs'] if j['job_id'] == audit_path.stem)
        original = tr.partition_provider_response
        def reordered(*args):
            usage, accepted, errors = original(*args)
            return usage, accepted, list(reversed(errors))
        before = audit_path.read_bytes()
        with patch.object(tr, 'partition_provider_response', reordered):
            tr.read_content_quarantine(job, self.output)
        self.assertEqual(audit_path.read_bytes(), before)
        def omitted(*args):
            usage, accepted, errors = original(*args)
            return usage, accepted, errors[1:]
        with patch.object(tr, 'partition_provider_response', omitted), self.assertRaises(tr.TranslationError):
            tr.read_content_quarantine(job, self.output)


if __name__ == '__main__':
    unittest.main()

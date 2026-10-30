"""Synthetic HTTP/process evidence; no network requests or app imports."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[1] / 'ops_health.py'
spec = importlib.util.spec_from_file_location('shushi_ops_health', SCRIPT)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class HealthTests(unittest.TestCase):
    def invoke(self, health, process_count=None):
        class Opener:
            def open(self, value, **_kwargs):
                if isinstance(value, str):
                    return FakeResponse(json.dumps(health).encode())
                raise HTTPError(value.full_url, 401, 'auth-required', None, None)
        argv = ['healthcheck', 'http://127.0.0.1:8000']
        if process_count is not None: argv.append('--local-process-check')
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), patch.object(ops, 'build_opener', return_value=Opener()), patch.object(sys, 'stdout', output):
            if process_count is None:
                result = ops.main()
            else:
                with patch.object(ops, 'local_backend_count', return_value=process_count): result = ops.main()
        return result, json.loads(output.getvalue())

    def test_fixed_ok_cannot_fake_database_readiness(self):
        code, report = self.invoke({'status': 'ok', 'environment': 'local-development'})
        self.assertEqual(code, 1)
        self.assertEqual(report['http_and_database'], 'failed')

    def test_zero_or_multiple_local_processes_cannot_pass(self):
        for count in (0, 2):
            code, report = self.invoke({'status': 'ok', 'database': 'ok', 'environment': 'local-development'}, count)
            self.assertEqual(code, 1)
            self.assertEqual(report['single_worker'], 'failed')

    def test_one_local_process_and_real_database_contract_pass(self):
        code, report = self.invoke({'status': 'ok', 'database': 'ok', 'environment': 'local-development'}, 1)
        self.assertEqual(code, 0)
        self.assertEqual(report['single_worker'], 'ok')

    def test_remote_http_check_does_not_claim_worker_count(self):
        code, report = self.invoke({'status': 'ok', 'database': 'ok', 'environment': 'production'})
        self.assertEqual(code, 0)
        self.assertIn('not_inspected', report['single_worker'])

    def test_absolute_uvicorn_executable_is_matched_only_for_this_project(self):
        import psutil
        process = SimpleNamespace(info={'cmdline': ['/usr/bin/uvicorn', 'app.main:app', '--app-dir', str(ops.PROJECT / 'backend')], 'cwd': '/'})
        unrelated = SimpleNamespace(info={'cmdline': ['/usr/bin/uvicorn', 'app.main:app', '--app-dir', '/another-project/backend'], 'cwd': '/'})
        with patch.object(psutil, 'process_iter', return_value=[process, unrelated]):
            self.assertEqual(ops.local_backend_count(), 1)
        process.info['cmdline'].extend(['--workers', '2'])
        with patch.object(psutil, 'process_iter', return_value=[process]):
            with self.assertRaises(ValueError): ops.local_backend_count()


if __name__ == '__main__':
    unittest.main()

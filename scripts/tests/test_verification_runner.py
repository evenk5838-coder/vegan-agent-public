"""Verify that the offline test entry cannot inherit production configuration."""
import importlib.util
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'run_isolated_tests.py'
spec = importlib.util.spec_from_file_location('shushi_verification_runner', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class VerificationRunnerTests(unittest.TestCase):
    def test_environment_drops_credentials_database_and_plugin_injection(self):
        environment = runner.clean_environment({
            'PATH': '/synthetic/bin', 'HOME': '/synthetic-home',
            'DATABASE_URL': 'must-not-inherit', 'DEEPSEEK_API_KEY': 'must-not-inherit',
            'EXTERNAL_CALLS_ENABLED': 'true', 'PYTEST_ADDOPTS': '-p untrusted',
            'PYTHONPATH': '/untrusted', 'DATA_DIR': '/business-data',
        }, Path('/tmp/synthetic-verification'))
        self.assertEqual(environment['DATABASE_URL'], '')
        self.assertEqual(environment['EXTERNAL_CALLS_ENABLED'], 'false')
        self.assertEqual(environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'], '1')
        self.assertNotIn('DEEPSEEK_API_KEY', environment)
        self.assertNotIn('PYTEST_ADDOPTS', environment)
        self.assertEqual(environment['DATA_DIR'], '/tmp/synthetic-verification/unused-default-data')

    def test_repository_credentials_and_business_data_are_protected(self):
        self.assertTrue(runner.protected_path(runner.ROOT / '.env'))
        self.assertTrue(runner.protected_path(runner.ROOT / 'frontend/.env.local'))
        self.assertTrue(runner.protected_path(runner.ROOT / 'data/postgres/PG_VERSION'))
        self.assertFalse(runner.protected_path(runner.ROOT / 'backend/app/core.py'))
        self.assertFalse(runner.protected_path('/tmp/synthetic.env'))
        self.assertFalse(runner.protected_path(3))

    def test_audit_blocks_dns_and_internet_but_preserves_temporary_unix_database(self):
        with self.assertRaises(PermissionError):
            runner.audit_guard('socket.getaddrinfo', ('example.test', 443))
        with self.assertRaises(PermissionError):
            runner.audit_guard('socket.connect', (SimpleNamespace(family=socket.AF_INET), ('127.0.0.1', 8000)))
        runner.audit_guard('socket.connect', (SimpleNamespace(family=socket.AF_UNIX), '/tmp/synthetic-pg'))
        with self.assertRaises(PermissionError):
            runner.audit_guard('open', (str(runner.ROOT / '.env'), 'r', 0))

    def test_secret_and_data_symlinks_cannot_escape_the_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder).resolve()
            checkout = temporary / 'synthetic-checkout'
            checkout.mkdir()
            synthetic_secret = temporary / 'invented-secret'
            synthetic_secret.write_text('synthetic-only')
            media = temporary / 'synthetic-media'
            media.mkdir()
            (checkout / '.env').symlink_to(synthetic_secret)
            (checkout / 'data').symlink_to(media, target_is_directory=True)
            with patch.object(runner, 'ROOT', checkout):
                self.assertTrue(runner.protected_path(checkout / '.env'))
                self.assertTrue(runner.protected_path(checkout / 'data' / 'invented.png'))


if __name__ == '__main__':
    unittest.main()

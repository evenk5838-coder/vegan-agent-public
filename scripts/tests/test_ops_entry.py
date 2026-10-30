"""Maintenance role routing only; no Docker, app settings, DB or network."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/ops_entry.py'
spec = importlib.util.spec_from_file_location('shushi_ops_entry', SCRIPT)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class MaintenanceEntryTests(unittest.TestCase):
    def invoke(self, argv, role='backup'):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['ops', *argv]), patch.dict(os.environ, {'OPS_ROLE': role}, clear=True), redirect_stdout(output), redirect_stderr(output):
            code = entry.main()
        return code, output.getvalue()

    def test_help_does_not_connect_or_read_configuration(self):
        with patch.object(entry, 'selected_config') as config, patch.object(entry.ops, 'backup') as backup:
            self.assertEqual(self.invoke(['help'])[0], 0)
            config.assert_not_called(); backup.assert_not_called()

    def test_backup_profile_cannot_restore_and_paths_cannot_escape(self):
        with patch.object(entry.ops, 'restore_to') as restore, patch.object(entry.ops, 'backup') as backup:
            self.assertEqual(self.invoke(['restore', 'sample', '--confirm-target', 'unused'])[0], 1)
            for name in ('../other', '/absolute', 'name/child', '..'):
                self.assertEqual(self.invoke(['backup', name])[0], 1)
            restore.assert_not_called(); backup.assert_not_called()

    def test_restore_profile_routes_only_to_independent_fixed_target(self):
        observed = []
        def config():
            from sqlalchemy.engine import make_url
            url = make_url(os.environ['DATABASE_URL'])
            observed.append((url.host, url.database, url.username, os.environ['DATA_DIR']))
            return {'target': 'synthetic'}
        with patch.dict(os.environ, {'POSTGRES_PASSWORD': 'SyntheticOnly_32Characters_9c77abb1'}, clear=True), patch.object(entry.ops, 'configuration', side_effect=config):
            entry.selected_config('backup')
            entry.selected_config('restore')
        self.assertEqual(observed, [('db', 'shushi', 'shushi', '/source-data'),
                                    ('restore-db', 'shushi_restore', 'shushi_restore', '/restore-data')])


if __name__ == '__main__':
    unittest.main()

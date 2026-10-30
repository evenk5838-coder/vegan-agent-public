"""Only synthetic content in fresh private Postgres clusters and temporary media."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'ops_data.py'
spec = importlib.util.spec_from_file_location('shushi_ops_data', SCRIPT)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class BackupRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.folder = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix='shushi-ops-fixture-'))).resolve()
        cls.source = cls.stack.enter_context(ops.temporary_postgres())
        for kind in ('images', 'audio'):
            directory = cls.source['data'] / kind
            directory.mkdir(parents=True)
            (directory / ('synthetic.' + kind)).write_bytes(b'publicly invented test fixture')
        with ops.connect(cls.source) as connection:
            connection.execute('CREATE TABLE users (id integer PRIMARY KEY, username text NOT NULL)')
            connection.execute('CREATE TABLE records (id integer PRIMARY KEY, payload jsonb NOT NULL)')
            connection.execute("INSERT INTO users VALUES (1, 'synthetic-person')")
            connection.execute("INSERT INTO records VALUES (1, '{\"marker\": \"synthetic-only\", \"status\": \"pending\"}')")
        cls.backup = cls.folder / 'backup'
        cls.result = ops.backup(cls.backup, cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    def test_snapshot_archive_and_media_checksums(self):
        manifest = ops.verify_backup(self.backup)
        self.assertEqual(manifest['database_counts'], {'records': 1, 'users': 1})
        self.assertEqual(len(manifest['files']), 3)
        self.assertTrue(self.result['database_snapshot'])
        self.assertFalse(self.result['media_atomic_with_database'])
        self.assertEqual((self.backup / 'database.dump').stat().st_mode & 0o777, 0o600)

    def test_dry_run_really_restores_archive_and_media_in_another_cluster(self):
        result = ops.dry_run(self.backup)
        self.assertEqual(result['operation'], 'isolated_restore_drill')
        self.assertTrue(result['database_verified'])
        self.assertTrue(result['media_verified'])
        self.assertFalse(result['real_target_written'])
        with ops.connect(self.source) as connection:
            self.assertEqual(ops.counts(connection), {'records': 1, 'users': 1})

    def test_explicit_empty_target_restore_preserves_values_and_rejects_second_apply(self):
        with ops.temporary_postgres() as target:
            with self.assertRaisesRegex(ops.OpsError, '确认指纹'):
                ops.restore_to(self.backup, target, 'wrong-confirmation')
            with ops.connect(target) as connection:
                self.assertEqual(ops.counts(connection), {})
            result = ops.restore_to(self.backup, target, target['fingerprint'])
            self.assertTrue(result['database_verified'])
            with ops.connect(target) as connection:
                self.assertEqual(connection.execute('SELECT payload FROM records WHERE id=1').fetchone()[0],
                                 {'marker': 'synthetic-only', 'status': 'pending'})
            self.assertEqual((target['data'] / 'images/synthetic.images').read_bytes(), b'publicly invented test fixture')
            with self.assertRaisesRegex(ops.OpsError, '没有任何业务表'):
                ops.restore_to(self.backup, target, target['fingerprint'])

    def test_original_source_cannot_be_overwritten(self):
        with self.assertRaisesRegex(ops.OpsError, '原数据库'):
            ops.restore_to(self.backup, self.source, self.source['fingerprint'])

    def test_tamper_path_and_symlink_fail_before_restore(self):
        for index, kind in enumerate(('tamper', 'traversal', 'symlink', 'unlisted')):
            copy = self.folder / ('invalid-' + str(index))
            shutil.copytree(self.backup, copy)
            manifest = json.loads((copy / 'manifest.json').read_text())
            if kind == 'tamper':
                (copy / 'database.dump').write_bytes(b'invalid')
            elif kind == 'traversal':
                manifest['files'][0]['path'] = '../../outside'
                (copy / 'manifest.json').write_text(json.dumps(manifest))
            elif kind == 'symlink':
                file = copy / manifest['files'][0]['path']
                file.unlink()
                file.symlink_to(self.backup / manifest['files'][0]['path'])
            else:
                (copy / 'unexpected').write_text('synthetic')
            with self.assertRaises(ops.OpsError):
                ops.verify_backup(copy)

    def test_explicit_config_and_environment_override_resolve_exact_data_directory(self):
        config = self.folder / 'source.env'
        config.write_text('DATABASE_URL=postgresql://synthetic@db:5432/shushi\nDATA_DIR=' + str(self.folder / 'correct-media') + '\n')
        with patch.dict(os.environ, {}, clear=True):
            resolved = ops.configuration(config)
            self.assertEqual(resolved['params']['host'], 'db')
            self.assertEqual(resolved['data'], self.folder / 'correct-media')
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://synthetic@other:5432/shushi', 'DATA_DIR': str(self.folder / 'override')}, clear=True):
            resolved = ops.configuration(config)
            self.assertEqual(resolved['params']['host'], 'other')
            self.assertEqual(resolved['data'], self.folder / 'override')

    def test_restore_target_requires_private_file_with_both_explicit_locations(self):
        file = self.folder / 'target.env'
        file.write_text('DATABASE_URL=postgresql://synthetic@db:5432/new_target\n')
        file.chmod(0o600)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ops.OpsError, '明确填写'):
                ops.configuration(file, explicit_target=True)
            file.write_text(file.read_text() + 'DATA_DIR=' + str(self.folder / 'new-target-data') + '\n')
            file.chmod(0o644)
            with self.assertRaisesRegex(ops.OpsError, '600'):
                ops.configuration(file, explicit_target=True)
            file.chmod(0o600)
            self.assertEqual(ops.configuration(file, explicit_target=True)['params']['dbname'], 'new_target')


if __name__ == '__main__':
    unittest.main()

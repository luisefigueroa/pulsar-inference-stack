"""Configuration is literal data and access observations are not operator policy."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model_library import configuration as config
from model_library.integrity import StorageError


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.archive = self.root / "archive space ' quoted"
        self.archive.mkdir()
        self.other = self.archive / 'not-pulsar.txt'
        self.other.write_text('leave me unchanged')

    def test_absent_empty_and_process_precedence(self):
        self.assertEqual(config.effective(self.repo, {})['status'], 'not-configured')
        self.assertFalse((self.repo / '.pulsar').exists())
        config.set_root(self.repo, str(self.archive))
        self.assertEqual(config.effective(self.repo, {})['source'], 'dotenv')
        state = config.effective(self.repo, {'PULSAR_COLD_ROOT': ''})
        self.assertEqual(state['status'], 'disabled')
        self.assertEqual(state['source'], 'process')
        self.assertEqual(state['persisted_path'], str(self.archive))
        config.set_root(self.repo, '')
        self.assertEqual(config.effective(self.repo, {})['status'], 'disabled')

    def test_explicit_process_empty_overrides_even_malformed_saved_value(self):
        (self.repo / '.env').write_text('PULSAR_COLD_ROOT=$(not-a-command)\n')
        state = config.effective(self.repo, {'PULSAR_COLD_ROOT': ''})
        self.assertEqual(state['status'], 'disabled')
        self.assertIsNotNone(state['persisted_error'])

    def test_set_preserves_other_assignments_comments_and_literal_path(self):
        original = b'# site note\nOTHER="literal $HOME"\n\nexport PULSAR_COLD_ROOT=""\nLAST=x\n'
        (self.repo / '.env').write_bytes(original)
        config.set_root(self.repo, str(self.archive))
        current = (self.repo / '.env').read_text()
        self.assertTrue(current.startswith('# site note\nOTHER="literal $HOME"\n\n'))
        self.assertTrue(current.endswith('LAST=x\n'))
        self.assertEqual(config.effective(self.repo, {})['path'], str(self.archive))
        self.assertEqual(self.other.read_text(), 'leave me unchanged')

    def test_dynamic_duplicate_and_trailing_assignments_are_never_executed(self):
        sentinel = self.root / 'must-not-exist'
        for value in (f'$(touch {sentinel})', '`id`', '"${HOME}/archive"', '/archive trailing'):
            (self.repo / '.env').write_text(f'PULSAR_COLD_ROOT={value}\n')
            with self.assertRaises(StorageError):
                config.effective(self.repo, {})
        self.assertFalse(sentinel.exists())
        (self.repo / '.env').write_text("PULSAR_COLD_ROOT=''\nPULSAR_COLD_ROOT=''\n")
        with self.assertRaisesRegex(StorageError, 'duplicated'):
            config.set_root(self.repo, str(self.archive))

    def test_access_unavailable_is_health_not_a_configuration_veto(self):
        with patch.object(config.os, 'access', return_value=False):
            config.set_root(self.repo, str(self.archive))
            state = config.effective(self.repo, {})
        self.assertEqual(state['status'], 'configured')
        self.assertFalse(state['health']['readable'])
        self.assertFalse(state['health']['writable'])
        self.assertEqual(self.other.read_text(), 'leave me unchanged')

    def test_directory_must_exist_without_creation_or_mount_policy(self):
        missing = self.root / 'missing'
        with self.assertRaisesRegex(StorageError, 'already exist'):
            config.set_root(self.repo, str(missing))
        self.assertFalse(missing.exists())
        self.assertFalse((self.repo / '.env').exists())
        self.assertFalse((self.repo / '.pulsar').exists())

    def test_show_marks_disappeared_directory_as_unavailable(self):
        config.set_root(self.repo, str(self.archive))
        self.other.unlink()
        self.archive.rmdir()
        state = config.effective(self.repo, {})
        self.assertEqual(state['status'], 'configured')
        self.assertFalse(state['health']['directory_exists'])

    def test_dotenv_symlinks_are_refused_without_modifying_the_target(self):
        other = self.root / 'elsewhere'
        other.write_text('unchanged')
        (self.repo / '.env').symlink_to(other)
        with self.assertRaises(StorageError):
            config.set_root(self.repo, str(self.archive))
        self.assertEqual(other.read_text(), 'unchanged')

    def test_archive_operation_holds_shared_lock_against_configuration_change(self):
        config.set_root(self.repo, str(self.archive))
        changed = threading.Event()
        error = []
        def writer():
            try:
                config.set_root(self.repo, '')
                changed.set()
            except BaseException as exc:
                error.append(exc)
        with config.lock(self.repo):
            worker = threading.Thread(target=writer)
            worker.start()
            self.assertFalse(changed.wait(0.1))
            self.assertEqual(config.effective(self.repo, {})['path'], str(self.archive))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(error, [])
        self.assertTrue(changed.is_set())

    def test_locked_child_receives_effective_root_and_lock_marker(self):
        config.set_root(self.repo, str(self.archive))
        with patch.dict(os.environ, {}, clear=True), patch.object(config.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(config.run_locked(self.repo, ['synthetic-operation']), 0)
        self.assertEqual(run.call_args.kwargs['env']['PULSAR_COLD_ROOT'], str(self.archive))
        self.assertEqual(run.call_args.kwargs['env']['PULSAR_ARCHIVE_CONFIG_LOCKED'], '1')

    def test_disable_in_process_prevents_archive_child(self):
        config.set_root(self.repo, str(self.archive))
        with patch.dict(os.environ, {'PULSAR_COLD_ROOT': ''}), patch.object(config.subprocess, 'run') as run:
            with self.assertRaisesRegex(StorageError, 'configure an archive location'):
                config.run_locked(self.repo, ['synthetic-operation'])
        run.assert_not_called()

    def test_readable_human_output_at_narrow_width(self):
        output = io.StringIO()
        with redirect_stdout(output):
            config.display({'source': 'process', 'path': '/archive', 'persisted_path': '',
                'status': 'configured', 'health': {'directory_exists': True, 'readable': True, 'writable': False}})
        self.assertIn('Writable: false', output.getvalue())
        self.assertIn('overrides', output.getvalue())
        self.assertLessEqual(max(map(len, output.getvalue().splitlines())), 60)

if __name__ == '__main__':
    unittest.main()

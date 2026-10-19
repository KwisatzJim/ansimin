import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import ansimin


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.state = self.root / 'state'
        (self.state / 'backups').mkdir(parents=True)
        self.runs = SimpleNamespace(lock=threading.Lock(), job=None, executable='/bin/ansible-playbook')
        self.playbooks = ansimin.Playbooks(self.workspace, self.state, self.runs)
        self.editor = ansimin.InventoryEditor(self.workspace, self.state, self.runs)
        self.backups = ansimin.Backups(self.state, self.playbooks, self.editor)
        self.original = b'---\n- hosts: all\n  tasks: []\n'
        self.current = self.original + b'# newer\n'
        self.target = self.workspace / 'test.yml'
        self.target.write_bytes(self.current)
        self.identifier = 'test.yml.1792099148388694437.26ae2dd9.bak'
        self.path = self.state / 'backups' / self.identifier
        self.path.write_bytes(self.original)
        self.mock = patch('ansimin.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr=''))
        self.process = self.mock.start()
        self.addCleanup(self.mock.stop)

    def payload(self):
        preview = self.backups.preview(self.identifier)
        return dict(id=preview['id'], backup_revision=preview['revision'], current_revision=preview['current_revision'])

    def test_restore_preserves_exact_previous_file(self):
        self.assertIn('-# newer', self.backups.preview(self.identifier)['diff'])
        result = self.backups.restore(self.payload())
        self.assertEqual(self.target.read_bytes(), self.original)
        saved = self.state / 'backups' / result['backup']
        self.assertEqual(saved.read_bytes(), self.current)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.backups.list()['backups']), 2)

    def test_stale_current_and_modified_backup_rejected(self):
        payload = self.payload()
        self.target.write_bytes(b'newer terminal edit')
        with self.assertRaises(RuntimeError): self.backups.restore(payload)
        self.assertEqual(self.target.read_bytes(), b'newer terminal edit')
        payload = self.payload()
        self.path.write_bytes(b'tampered')
        with self.assertRaises(RuntimeError): self.backups.restore(payload)

    def test_validation_failure_and_active_job_preserve_current(self):
        self.process.return_value.returncode = 1
        with self.assertRaises(ansimin.SyntaxProblem): self.backups.restore(self.payload())
        self.assertEqual(self.target.read_bytes(), self.current)
        self.process.return_value.returncode = 0
        self.runs.job = {'status': 'running'}
        with self.assertRaises(RuntimeError): self.backups.restore(self.payload())
        self.assertEqual(len(self.backups.list()['backups']), 1)

    def test_invalid_paths_symlinks_and_oversize_are_excluded(self):
        for identifier in ('../key', 'key', 'inventory.ini.1.12345678.bak/extra'):
            with self.assertRaises(ValueError): self.backups.preview(identifier)
        self.path.unlink()
        self.path.symlink_to(self.target)
        self.assertEqual(self.backups.list()['backups'], [])
        with self.assertRaises(ValueError): self.backups.preview(self.identifier)
        self.path.unlink()
        self.path.write_bytes(b'x' * 262145)
        self.assertEqual(self.backups.list()['backups'], [])

    def test_deleted_playbook_can_be_restored_but_new_file_is_not_overwritten(self):
        self.target.unlink()
        payload = self.payload()
        self.assertIsNone(payload['current_revision'])
        self.target.write_bytes(b'created elsewhere')
        with self.assertRaises(RuntimeError): self.backups.restore(payload)
        self.target.unlink()
        result = self.backups.restore(payload)
        self.assertIsNone(result['backup'])
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_inventory_restore_uses_strict_validation(self):
        self.identifier = 'inventory.ini.1792099148388694437.26ae2dd9.bak'
        self.path = self.state / 'backups' / self.identifier
        self.path.write_bytes(b'[pis]\npi ansible_connection=local\n')
        target = self.workspace / 'inventory.ini'
        target.write_bytes(b'[pis]\npi2\n')
        self.process.return_value.stdout = json.dumps({'pis': {'hosts': ['pi']}})
        result = self.backups.restore(self.payload())
        self.assertEqual(target.read_bytes(), self.path.read_bytes())
        self.assertEqual((self.state / 'backups' / result['backup']).read_bytes(), b'[pis]\npi2\n')
        self.assertEqual(self.process.call_args.kwargs['env']['ANSIBLE_INVENTORY_ENABLED'], 'ini')

    def test_cleanup_removes_only_selected_backup(self):
        payload = self.payload()
        result = self.backups.remove(payload)
        self.assertEqual(result['removed'], self.identifier)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.target.read_bytes(), self.current)

    def test_cleanup_rejects_changed_backup_and_unsafe_paths(self):
        payload = self.payload()
        self.path.write_bytes(b'changed')
        with self.assertRaises(RuntimeError): self.backups.remove(payload)
        self.assertTrue(self.path.exists())
        for identifier in ('../key', 'key'):
            with self.assertRaises(ValueError): self.backups.remove(dict(id=identifier, backup_revision='0'*64))
        self.path.unlink()
        self.path.symlink_to(self.target)
        with self.assertRaises(ValueError): self.backups.remove(payload)
        self.assertEqual(self.target.read_bytes(), self.current)

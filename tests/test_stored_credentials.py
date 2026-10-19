import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from stored_credentials import StoredCredential


@unittest.skipUnless(importlib.util.find_spec('cryptography'), 'Credential encryption requires cryptography')
class StoredCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StoredCredential(Path(self.temp.name))
        self.secret = 'dummy-admin-password-for-tests'

    def test_encryption_survives_reload_with_private_files_and_metadata_only_status(self):
        self.assertEqual(self.store.status(), {'configured': False, 'updated': None})
        status = self.store.save(self.secret)
        self.assertEqual(set(status), {'configured', 'updated'})
        self.assertEqual(self.store.load(), self.secret)
        self.assertEqual(StoredCredential(Path(self.temp.name)).load(), self.secret)
        self.assertEqual(self.store.status(), status)
        self.assertNotIn(self.secret.encode(), self.store.secret_path.read_bytes())
        self.assertNotIn(self.secret.encode(), self.store.key_path.read_bytes())
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        for path in (self.store.key_path, self.store.secret_path):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_replace_uses_existing_key_and_remove_keeps_key(self):
        self.store.save(self.secret)
        original_key = self.store.key_path.read_bytes()
        original_token = self.store.secret_path.read_bytes()
        self.store.save('a-different-dummy-password')
        self.assertEqual(self.store.load(), 'a-different-dummy-password')
        self.assertEqual(self.store.key_path.read_bytes(), original_key)
        self.assertNotEqual(self.store.secret_path.read_bytes(), original_token)
        self.assertEqual(self.store.remove(), {'configured': False, 'updated': None})
        self.assertFalse(self.store.secret_path.exists())
        self.assertEqual(self.store.key_path.read_bytes(), original_key)

    def test_tampered_ciphertext_is_rejected(self):
        self.store.save(self.secret)
        data = json.loads(self.store.secret_path.read_text())
        data['token'] = 'tampered-token'
        self.store.secret_path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.store.load()

    def test_missing_key_does_not_silently_replace_existing_credential(self):
        self.store.save(self.secret)
        original = self.store.secret_path.read_bytes()
        self.store.key_path.unlink()
        with self.assertRaises(ValueError):
            self.store.save('replacement')
        self.assertEqual(self.store.secret_path.read_bytes(), original)
        self.assertFalse(self.store.key_path.exists())

    def test_failed_replacement_preserves_current_credential_and_cleans_temporary_file(self):
        self.store.save(self.secret)
        original = self.store.secret_path.read_bytes()
        with patch('stored_credentials.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.store.save('replacement')
        self.assertEqual(self.store.secret_path.read_bytes(), original)
        self.assertEqual(self.store.load(), self.secret)
        self.assertFalse(list(self.store.directory.glob('.sudo-*')))

    def test_invalid_passwords_do_not_create_storage(self):
        for password in ('', 'bad\npassword', 'bad\rpassword', 'bad\x00password', 'x' * 513, None):
            with self.assertRaises(ValueError):
                self.store.save(password)
        self.assertFalse(self.store.directory.exists())

    def test_symlink_and_broad_file_permissions_are_rejected(self):
        self.store.save(self.secret)
        self.store.secret_path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.load()
        self.store.secret_path.chmod(0o600)
        self.store.key_path.unlink()
        (self.store.directory / 'another-key').write_text('not a valid key')
        self.store.key_path.symlink_to(self.store.directory / 'another-key')
        with self.assertRaises(ValueError):
            self.store.load()


if __name__ == '__main__':
    unittest.main()

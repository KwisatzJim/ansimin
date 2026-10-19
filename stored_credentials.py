"""Encrypted shared sudo credential. No API may return its plaintext or token."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time


class StoredCredential:
    def __init__(self, state):
        self.directory = Path(state) / 'secrets'
        self.key_path = self.directory / 'key'
        self.secret_path = self.directory / 'sudo.json'
        self.lock = threading.Lock()

    def prepare(self):
        if self.directory.is_symlink():
            raise ValueError('Credential storage must be a private directory')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_uid != os.getuid():
            raise ValueError('Credential storage has an unexpected owner')
        os.chmod(self.directory, 0o700)

    def sync_directory(self):
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def private_file(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Credential storage file permissions must be owner-only')

    def status(self):
        with self.lock:
            if not self.secret_path.exists() and not self.secret_path.is_symlink():
                return {'configured': False, 'updated': None}
            self.prepare()
            self.private_file(self.secret_path)
            metadata = json.loads(self.secret_path.read_text())
            if metadata.get('version') != 1 or not isinstance(metadata.get('updated'), (int, float)):
                raise ValueError('Stored credential data could not be read')
            self.private_file(self.key_path)
            return {'configured': True, 'updated': metadata['updated']}

    def cipher(self, create=False):
        # Lazy import keeps the rest of ansimin usable on systems lacking this optional dependency.
        from cryptography.fernet import Fernet
        self.prepare()
        if not self.key_path.exists() and not self.key_path.is_symlink():
            if not create or self.secret_path.exists():
                raise ValueError('The credential encryption key is missing. Restore the key before using or replacing this credential.')
            descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as file:
                file.write(Fernet.generate_key())
                file.flush()
                os.fsync(file.fileno())
            self.sync_directory()
        self.private_file(self.key_path)
        return Fernet(self.key_path.read_bytes())

    def save(self, password):
        if not isinstance(password, str) or not password or len(password.encode()) > 512 or any(char in password for char in ('\x00', '\n', '\r')):
            raise ValueError('Enter a sudo password of at most 512 bytes, without line breaks')
        with self.lock:
            cipher = self.cipher(create=True)
            if self.secret_path.exists() or self.secret_path.is_symlink():
                self.private_file(self.secret_path)
            updated = time.time()
            document = json.dumps({'version': 1, 'updated': updated,
                                   'token': cipher.encrypt(password.encode()).decode('ascii')}).encode()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix='.sudo-', dir=self.directory, delete=False) as file:
                    temporary = Path(file.name)
                    os.fchmod(file.fileno(), 0o600)
                    file.write(document)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.secret_path)
                self.sync_directory()
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {'configured': True, 'updated': updated}

    def load(self):
        """Internal scheduler use only. Do not expose this method over HTTP."""
        from cryptography.fernet import InvalidToken
        with self.lock:
            cipher = self.cipher()
            self.private_file(self.secret_path)
            document = json.loads(self.secret_path.read_text())
            if document.get('version') != 1 or not isinstance(document.get('token'), str):
                raise ValueError('Stored credential data could not be read')
            try:
                return cipher.decrypt(document['token'].encode('ascii')).decode()
            except (InvalidToken, UnicodeError):
                raise ValueError('Stored credential could not be decrypted. Check the encryption key and credential files.') from None

    def remove(self):
        with self.lock:
            self.prepare()
            if self.secret_path.exists() or self.secret_path.is_symlink():
                self.private_file(self.secret_path)
                self.secret_path.unlink()
                self.sync_directory()
            # Keep the key so existing administrator backups can still be recovered.
            return {'configured': False, 'updated': None}

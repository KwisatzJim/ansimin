#!/usr/bin/env python3
"""Ansimin: Ansible administration over HTTPS."""
import argparse
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import signal
import shutil
import sqlite3
import ssl
import subprocess
import threading
import tempfile
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from stored_credentials import StoredCredential
from schedules import Scheduler
from notifications import Notifications

ROOT = Path(__file__).resolve().parent


def inventory(workspace, executable):
    result = subprocess.run(
        [executable, '-i', str(workspace / 'inventory.ini'), '--list'],
        cwd=workspace, capture_output=True, text=True, timeout=30, check=True,
    )
    return inventory_summary(json.loads(result.stdout))


def inventory_summary(raw):
    variables = raw.get('_meta', {}).get('hostvars', {})
    # Ansible can omit hosts with no explicit variables from _meta.hostvars.
    # Group membership is the complete source of host names in that case.
    names = set(variables)
    for name, group in raw.items():
        if name != '_meta':
            names.update(group.get('hosts', []))
    return {
        'hosts': [{'name': name, 'user': variables.get(name, {}).get('ansible_user', ''),
                   'connection': variables.get(name, {}).get('ansible_connection', 'ssh')}
                  for name in sorted(names)],
        'groups': [{'name': name, 'hosts': values.get('hosts', []),
                    'children': values.get('children', [])}
                   for name, values in sorted(raw.items()) if name not in ('_meta', 'all', 'ungrouped')],
    }


def playbook_path(workspace, name):
    if not isinstance(name, str) or Path(name).name != name or Path(name).suffix not in ('.yml', '.yaml'):
        raise ValueError('Invalid playbook name')
    target = workspace / name
    if target.is_symlink() or target.resolve().parent != workspace.resolve():
        raise ValueError('Invalid playbook path')
    return target


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def revision(content):
    return hashlib.sha256(content).hexdigest()


class SyntaxProblem(ValueError):
    def __init__(self, diagnostics, message='Ansible found a syntax problem. The playbook was not saved.'):
        super().__init__(message)
        self.diagnostics = diagnostics


class Playbooks:
    def __init__(self, workspace, state, runs):
        self.workspace, self.state, self.runs = workspace, state, runs

    def update(self, payload, save=False):
        name, content = payload.get('name'), payload.get('content')
        target = playbook_path(self.workspace, name)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml', name):
            raise ValueError('Use a filename such as my-playbook.yml, with letters, numbers, dots, underscores or hyphens')
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > 262144:
            raise ValueError('Playbooks must contain text of at most 256 KiB')
        expected = payload.get('revision')
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
            raise ValueError('Invalid playbook revision')
        data = content.encode()
        with self.runs.lock:
            if self.runs.job and self.runs.job['status'] in ('running', 'cancelling'):
                raise RuntimeError('Wait for the active job to finish before validating or saving playbooks.')
            original = target.read_bytes() if target.exists() else None
            if save and ((original is None and expected is not None) or
                         (original is not None and revision(original) != expected)):
                raise RuntimeError('This filename already exists or the file changed since you opened it. Reopen it before saving.')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix='.ansimin-', suffix='.yml', dir=self.workspace, delete=False) as file:
                    temporary = Path(file.name)
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                env = os.environ.copy()
                env.update(ANSIBLE_BECOME_ASK_PASS='False', ANSIBLE_ASK_PASS='False', ANSIBLE_NOCOLOR='1')
                result = subprocess.run([self.runs.executable, '-i', str(self.workspace / 'inventory.ini'),
                                         '--syntax-check', str(temporary)], cwd=self.workspace,
                                        env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, timeout=30)
                diagnostics = (result.stdout + result.stderr).replace(str(temporary), name)[-16000:]
                if result.returncode != 0:
                    raise SyntaxProblem(diagnostics)
                if not save:
                    return {'valid': True, 'diagnostics': diagnostics}
                # Recheck after validation to catch terminal edits made while Ansible was checking.
                playbook_path(self.workspace, name)
                current = target.read_bytes() if target.exists() else None
                if current != original:
                    raise RuntimeError('The file changed during validation. Reopen it before saving.')
                backup_name = None
                if original is not None:
                    backup_dir = self.state / 'backups'
                    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    backup_name = f'{name}.{time.time_ns()}.{secrets.token_hex(4)}.bak'
                    with (backup_dir / backup_name).open('xb') as backup:
                        os.chmod(backup.name, 0o600)
                        backup.write(original)
                        backup.flush()
                        os.fsync(backup.fileno())
                    os.chmod(temporary, target.stat().st_mode & 0o777)
                    os.replace(temporary, target)
                else:
                    # Hard-link creation fails if another process has created the filename.
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        raise RuntimeError('This filename was just created elsewhere. Choose another name.')
                return {'name': name, 'content': content, 'revision': revision(data),
                        'backup': backup_name, 'diagnostics': diagnostics}
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


class InventoryEditor:
    """Edit the existing INI source, preserving comments and shared group membership."""
    def __init__(self, workspace, state, runs):
        self.workspace, self.state, self.runs = workspace, state, runs

    def path(self):
        target = self.workspace / 'inventory.ini'
        if target.is_symlink() or target.resolve().parent != self.workspace.resolve():
            raise ValueError('Inventory must be a regular file inside the workspace')
        return target

    def read(self):
        target = self.path()
        if target.stat().st_size > 262144:
            raise ValueError('Inventory exceeds the editor size limit of 256 KiB')
        content = target.read_bytes()
        return {'name': 'inventory.ini', 'content': content.decode(), 'revision': revision(content)}

    def update(self, payload, save=False):
        content, expected = payload.get('content'), payload.get('revision')
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > 262144 or '\x00' in content:
            raise ValueError('Inventory must contain INI text of at most 256 KiB')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('Reopen inventory before validating or saving')
        with self.runs.lock:
            if self.runs.job and self.runs.job['status'] in ('running', 'cancelling'):
                raise RuntimeError('Wait for the active job to finish before validating or saving inventory.')
            target = self.path()
            original = target.read_bytes()
            if revision(original) != expected:
                raise RuntimeError('Inventory changed since you opened it. Reopen it before saving.')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix='.ansimin-inventory-', suffix='.ini', dir=self.workspace, delete=False) as file:
                    temporary = Path(file.name)
                    file.write(content.encode())
                    file.flush()
                    os.fsync(file.fileno())
                env = os.environ.copy()
                env.update(ANSIBLE_INVENTORY_ENABLED='ini', ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED='True',
                           ANSIBLE_INVENTORY_UNPARSED_FAILED='True', ANSIBLE_NOCOLOR='1')
                executable = str(Path(self.runs.executable).with_name('ansible-inventory'))
                result = subprocess.run([executable, '-i', str(temporary), '--list'], cwd=self.workspace,
                                        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                diagnostics = result.stderr.replace(str(temporary), 'inventory.ini')[-16000:]
                if result.returncode != 0:
                    raise SyntaxProblem(diagnostics, 'Ansible could not parse this inventory. The file was not saved.')
                summary = inventory_summary(json.loads(result.stdout))
                if not summary['hosts']:
                    raise SyntaxProblem('Define at least one host before saving.', 'The inventory has no hosts. The file was not saved.')
                response = {'valid': True, 'diagnostics': diagnostics, **summary}
                if not save:
                    return response
                if self.path().read_bytes() != original:
                    raise RuntimeError('Inventory changed during validation. Reopen it before saving.')
                backup_dir = self.state / 'backups'
                backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                backup_name = f'inventory.ini.{time.time_ns()}.{secrets.token_hex(4)}.bak'
                with (backup_dir / backup_name).open('xb') as backup:
                    os.chmod(backup.name, 0o600)
                    backup.write(original)
                    backup.flush()
                    os.fsync(backup.fileno())
                os.chmod(temporary, target.stat().st_mode & 0o777)
                os.replace(temporary, target)
                return {**response, 'name': 'inventory.ini', 'content': content,
                        'revision': revision(content.encode()), 'backup': backup_name}
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


class Backups:
    """Restore editor backups through the same validation and conflict checks as saves."""
    pattern = re.compile(r'(?P<name>inventory\.ini|[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml)\.(?P<stamp>[0-9]{1,20})\.[0-9a-f]{8}\.bak')

    def __init__(self, state, playbooks, inventory_editor):
        self.directory = state / 'backups'
        self.playbooks, self.inventory_editor = playbooks, inventory_editor

    def metadata(self, identifier):
        match = self.pattern.fullmatch(identifier) if isinstance(identifier, str) else None
        if not match or self.directory.is_symlink():
            raise ValueError('Invalid backup')
        path = self.directory / identifier
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
            raise ValueError('Backup is unavailable or exceeds 256 KiB')
        return {'id': identifier, 'name': match['name'],
                'created': int(match['stamp']) / 1e9, 'size': path.stat().st_size}

    def list(self):
        if self.directory.is_symlink():
            raise ValueError('Invalid backup directory')
        entries = []
        if self.directory.exists():
            for path in self.directory.iterdir():
                try:
                    entries.append(self.metadata(path.name))
                except ValueError:
                    continue
        return {'backups': sorted(entries, key=lambda entry: entry['created'], reverse=True)}

    def read(self, identifier):
        metadata = self.metadata(identifier)
        # No-follow also rejects a backup replaced by a symlink after listing.
        descriptor = os.open(self.directory / identifier, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as file:
            import stat
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError('Backup must be a regular file')
            data = file.read(262145)
        if len(data) > 262144:
            raise ValueError('Backup exceeds 256 KiB')
        return {**metadata, 'content': data.decode(), 'revision': revision(data)}

    def preview(self, identifier):
        backup = self.read(identifier)
        target = (self.inventory_editor.path() if backup['name'] == 'inventory.ini'
                  else playbook_path(self.playbooks.workspace, backup['name']))
        if target.exists() and (not target.is_file() or target.stat().st_size > 262144):
            raise ValueError('Current file cannot be previewed')
        current = target.read_bytes() if target.exists() else None
        import difflib
        content = current.decode() if current is not None else ''
        difference = ''.join(difflib.unified_diff(content.splitlines(keepends=True),
                             backup['content'].splitlines(keepends=True),
                             fromfile='Current ' + backup['name'], tofile='Backup ' + backup['name']))
        return {**backup, 'current_revision': revision(current) if current is not None else None,
                'current_content': content, 'diff': difference}

    def restore(self, payload):
        backup = self.read(payload.get('id'))
        if payload.get('backup_revision') != backup['revision']:
            raise RuntimeError('The backup changed. Preview it again before restoring.')
        if 'current_revision' not in payload:
            raise ValueError('Preview the current file before restoring')
        editor = self.inventory_editor if backup['name'] == 'inventory.ini' else self.playbooks
        result = editor.update({'name': backup['name'], 'content': backup['content'],
                                'revision': payload['current_revision']}, save=True)
        return {**result, 'restored_from': backup['id']}


    def remove(self, payload):
        # Share the editor lock so cleanup cannot race a browser save or restore.
        with self.playbooks.runs.lock:
            backup = self.read(payload.get('id'))
            if payload.get('backup_revision') != backup['revision']:
                raise RuntimeError('The backup changed. Preview it again before deleting.')
            self.metadata(backup['id'])
            (self.directory / backup['id']).unlink()
            return {'removed': backup['id'], 'name': backup['name']}


class History:
    """Persist only public job metadata and already-redacted output."""
    fields = ('id', 'kind', 'playbook', 'module', 'target', 'mode', 'status',
              'output', 'truncated', 'started', 'finished', 'exit_code', 'schedule_id', 'schedule_name')

    def __init__(self, state, notifications=None):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        recovered = []
        self.path = state / 'history.sqlite3'
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            os.chmod(self.path, 0o600)
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS runs '
                                   '(id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)')
                rows = connection.execute("SELECT payload FROM runs WHERE status IN ('running', 'cancelling')").fetchall()
                for (payload,) in rows:
                    job = json.loads(payload)
                    job.update(status='interrupted', finished=time.time(), exit_code=None)
                    output = (job['output'] + '\nAnsimin restarted before this job finished. '
                              'Remote work may still be running; check the targets before retrying.\n')
                    job['output'] = output[-524288:]
                    job['truncated'] = job['truncated'] or len(output) > 524288
                    connection.execute('UPDATE runs SET status=?, payload=? WHERE id=?',
                                       (job['status'], json.dumps(job), job['id']))
                    recovered.append(job)
        finally:
            connection.close()
        if notifications:
            for job in recovered:
                notifications.run(job)

    def record(self, job):
        public = {key: job.get(key) for key in self.fields}
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                connection.execute('INSERT INTO runs(id,status,payload) VALUES(?,?,?) '
                                   'ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload',
                                   (public['id'], public['status'], json.dumps(public)))
        finally:
            connection.close()

    def latest(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            row = connection.execute('SELECT payload FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
            return json.loads(row[0]) if row else None
        finally:
            connection.close()

    def get(self, job_id):
        if not re.fullmatch('[0-9a-f]{24}', job_id):
            raise ValueError('Run not found')
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            row = connection.execute('SELECT payload FROM runs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('Run not found')
            return json.loads(row[0])
        finally:
            connection.close()

    def page(self, before=None):
        if before is not None and (not before.isascii() or not before.isdecimal() or len(before) > 18):
            raise ValueError('Invalid history cursor')
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            if before is None:
                rows = connection.execute('SELECT rowid,payload FROM runs ORDER BY rowid DESC LIMIT 26').fetchall()
            else:
                rows = connection.execute('SELECT rowid,payload FROM runs WHERE rowid < ? '
                                          'ORDER BY rowid DESC LIMIT 26', (int(before),)).fetchall()
            runs = []
            for _, payload in rows[:25]:
                job = json.loads(payload)
                job.pop('output', None)
                runs.append(job)
            return {'runs': runs, 'cursor': str(rows[24][0]) if len(rows) > 25 else None}
        finally:
            connection.close()


class Runs:
    """One background job, independent of the browser. Never persist submitted sudo credentials."""
    def __init__(self, workspace, executable, history=None, notifications=None):
        self.workspace, self.executable = workspace, executable
        self.history = history
        self.notifications = notifications
        self.lock = threading.Lock()
        self.job = history.latest() if history else None
        self.process = None
        self.last_persisted = 0

    def snapshot(self):
        with self.lock:
            return dict(self.job) if self.job else None

    def prepare(self, payload):
        kind = payload.get('kind', 'playbook')
        if kind not in ('playbook', 'adhoc'):
            raise ValueError('Invalid job type')
        path = None
        module = None
        arguments = None
        if kind == 'playbook':
            path = playbook_path(self.workspace, payload.get('playbook'))
            if not path.is_file():
                raise ValueError('Playbook not found')
        else:
            module = payload.get('module')
            arguments = payload.get('arguments', '')
            if not isinstance(module, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*', module) or len(module) > 160:
                raise ValueError('Enter a valid Ansible module name')
            if not isinstance(arguments, str) or len(arguments.encode()) > 2048 or '\x00' in arguments:
                raise ValueError('Module arguments must be text of at most 2048 bytes')
            if not isinstance(payload.get('become', False), bool):
                raise ValueError('Invalid sudo setting')
        target = payload.get('target')
        mode = payload.get('mode')
        password = payload.get('sudo_password', '')
        if mode not in (('syntax', 'check', 'run') if kind == 'playbook' else ('check', 'run')):
            raise ValueError('Choose a valid run mode')
        if not isinstance(password, str) or len(password.encode()) > 512 or '\n' in password or '\r' in password or '\x00' in password:
            raise ValueError('Invalid sudo password')
        data = inventory(self.workspace, str(Path(self.executable).with_name('ansible-inventory')))
        targets = {'all'} | {h['name'] for h in data['hosts']} | {g['name'] for g in data['groups']}
        if not isinstance(target, str) or target not in targets or target.startswith('-'):
            raise ValueError('Choose an inventory host or group')
        executable = self.executable if kind == 'playbook' else str(Path(self.executable).with_name('ansible'))
        command = [executable, '-i', str(self.workspace / 'inventory.ini'), '--limit', target]
        if mode == 'syntax':
            command.append('--syntax-check')
        elif mode == 'check':
            command.append('--check')
        if kind == 'playbook':
            # Absolute path keeps a playbook filename from being interpreted as an option.
            command.append(str(path.resolve()))
        else:
            command.extend(['--module-name', module, '--args=' + arguments])
            if payload.get('become', False):
                command.append('--become')
            command.append('all')
        return command, password, {'kind': kind, 'playbook': path.name if path else None,
                                   'module': module, 'target': target, 'mode': mode}

    def start(self, payload, schedule=None):
        command, password, metadata = self.prepare(payload)
        with self.lock:
            if self.job and self.job['status'] in ('running', 'cancelling'):
                raise RuntimeError('A job is already running. Wait or cancel it first.')
            previous = self.job
            self.job = {'id': secrets.token_hex(12), **metadata,
                        'schedule_id': schedule['id'] if schedule else None,
                        'schedule_name': schedule['name'] if schedule else None,
                        'status': 'running', 'output': '', 'truncated': False,
                        'started': time.time(), 'finished': None, 'exit_code': None}
            if self.history:
                # Refuse to launch if a new run cannot be recorded.
                try:
                    self.history.record(self.job)
                except (sqlite3.Error, OSError):
                    self.job = previous
                    raise
                self.last_persisted = time.monotonic()
            self.process = None
            job_id = self.job['id']
        threading.Thread(target=self.execute, args=(command, password, job_id), daemon=True).start()
        return self.snapshot()

    def append(self, text):
        with self.lock:
            combined = self.job['output'] + text
            self.job['output'] = combined[-524288:]
            self.job['truncated'] |= len(combined) > 524288
            if self.history and time.monotonic() - self.last_persisted >= 5:
                self.persist_locked()

    def persist_locked(self):
        if self.history:
            try:
                self.history.record(self.job)
                self.last_persisted = time.monotonic()
                self.job.pop('history_error', None)
            except (sqlite3.Error, OSError):
                # Keep execution and cancellation available if storage becomes unavailable.
                self.job['history_error'] = True

    def execute(self, command, password, job_id):
        secret_fd = None
        process = None
        exit_code = None
        outcome = 'failed'
        try:
            env = os.environ.copy()
            env.update(ANSIBLE_BECOME_ASK_PASS='False', ANSIBLE_ASK_PASS='False',
                       ANSIBLE_NOCOLOR='1', PYTHONUNBUFFERED='1', ANSIBLE_DISPLAY_ARGS_TO_STDOUT='False')
            if password:
                # Linux anonymous memory file: no password in arguments, environment, or disk files.
                secret_fd = os.memfd_create('ansimin-become', os.MFD_CLOEXEC)
                os.fchmod(secret_fd, 0o600)
                os.write(secret_fd, password.encode() + b'\n')
                os.lseek(secret_fd, 0, os.SEEK_SET)
                # Ansible resolves password-file symlinks, so a raw /proc/fd path
                # becomes an unusable memfd name. Its supported executable helper
                # reads the still-open memory file from this parent process.
                env['ANSIMIN_PASSWORD_FD'] = f'{os.getpid()}:{secret_fd}'
                command = command + ['--become-password-file', str(ROOT / 'become_password.py')]
            process = subprocess.Popen(command, cwd=self.workspace, env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       close_fds=True)
            with self.lock:
                self.process = process
                cancelling = self.job['status'] == 'cancelling'
            if cancelling:
                self.stop_process(process)
            import codecs
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            pending = ''
            while True:
                chunk = os.read(process.stdout.fileno(), 4096)
                pending += decoder.decode(chunk, final=not chunk)
                if password:
                    pending = pending.replace(password, '[REDACTED]')
                if not chunk:
                    self.append(pending)
                    break
                # Retain a tail so secrets spanning read boundaries are redacted too.
                keep = max(len(password) - 1, 0)
                if len(pending) > keep:
                    end = len(pending) - keep
                    self.append(pending[:end])
                    pending = pending[end:]
            exit_code = process.wait()
            outcome = 'succeeded' if exit_code == 0 else 'failed'
        except Exception:
            # Exception details can contain subprocess arguments or credential-bearing output.
            self.append('\nUnable to execute this job. Check Ansible installation and service permissions.\n')
            if process and process.poll() is None:
                self.signal_process(process, signal.SIGKILL)
                process.wait()
        finally:
            if secret_fd is not None:
                os.close(secret_fd)
            if process and process.stdout:
                process.stdout.close()
            password = ''
            with self.lock:
                self.job['exit_code'] = exit_code
                self.job['status'] = 'cancelled' if self.job['status'] == 'cancelling' else outcome
                self.job['finished'] = time.time()
                self.process = None
                self.persist_locked()
                completed = dict(self.job)
            if self.notifications:
                self.notifications.run(completed)

    @staticmethod
    def signal_process(process, sig=signal.SIGTERM):
        if process.poll() is None:
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass

    def stop_process(self, process):
        self.signal_process(process)
        timer = threading.Timer(5, self.signal_process, args=(process, signal.SIGKILL))
        timer.daemon = True
        timer.start()

    def cancel(self, job_id):
        with self.lock:
            if not self.job or self.job['id'] != job_id:
                raise ValueError('Job not found')
            if self.job['status'] not in ('running', 'cancelling'):
                return
            self.job['status'] = 'cancelling'
            self.persist_locked()
            process = self.process
        if process:
            self.stop_process(process)


class Server(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Do not log request bodies, credentials, or cookie values.
        pass

    def reply(self, status, content, kind='application/json', cookie=None, attachment=None):
        data = json.dumps(content).encode() if kind == 'application/json' else content
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            return cookie['ansimin'].value if 'ansimin' in cookie else ''
        except Exception:
            return ''

    def authorized(self):
        with self.server.lock:
            return self.server.sessions.get(self.token(), 0) > time.monotonic()

    def do_GET(self):
        if self.path in ('/', '/app.js', '/style.css'):
            name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[self.path]
            kind = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript', 'style.css': 'text/css'}[name]
            return self.reply(200, (ROOT / 'web' / name).read_bytes(), kind)
        if not self.authorized():
            return self.reply(401, {'error': 'Sign in to continue'})
        try:
            if self.path.startswith('/api/backup-download/'):
                from urllib.parse import urlsplit, parse_qs
                url = urlsplit(self.path)
                backup = self.server.backups.read(url.path.removeprefix('/api/backup-download/'))
                expected = parse_qs(url.query).get('revision', [None])[0]
                if expected != backup['revision']:
                    return self.reply(409, {'error': 'The backup changed. Preview it again before downloading.'})
                return self.reply(200, backup['content'].encode(), 'application/octet-stream',
                                  attachment=backup['id'])
            if self.path == '/api/backups':
                return self.reply(200, self.server.backups.list())
            if self.path.startswith('/api/backups/'):
                return self.reply(200, self.server.backups.preview(self.path.removeprefix('/api/backups/')))
            if self.path == '/api/run':
                return self.reply(200, {'job': self.server.runs.snapshot()})
            if self.path == '/api/notifications':
                return self.reply(200, self.server.notifications.status())
            if self.path == '/api/schedules':
                return self.reply(200, self.server.scheduler.list())
            if self.path == '/api/scheduler-credential':
                try:
                    return self.reply(200, self.server.stored_credential.status())
                except (ValueError, OSError):
                    return self.reply(503, {'error': 'Stored credential status is unavailable. Check the private credential files and encryption key.'})
            if self.path == '/api/inventory':
                return self.reply(200, self.server.inventory_editor.read())
            if self.path == '/api/history' or self.path.startswith('/api/history?'):
                from urllib.parse import urlsplit, parse_qs
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                return self.reply(200, self.server.runs.history.page(query.get('before', [None])[0]))
            if self.path.startswith('/api/history/'):
                return self.reply(200, {'job': self.server.runs.history.get(self.path.removeprefix('/api/history/'))})
            if self.path == '/api/overview':
                data = inventory(self.server.workspace, self.server.ansible)
                data['workspace'] = str(self.server.workspace)
                data['playbooks'] = sorted(p.name for p in self.server.workspace.iterdir()
                                           if p.suffix in ('.yml', '.yaml') and not p.name.startswith('.') and p.is_file() and not p.is_symlink())
                return self.reply(200, data)
            if self.path.startswith('/api/playbook/'):
                name = self.path.removeprefix('/api/playbook/')
                from urllib.parse import unquote
                target = playbook_path(self.server.workspace, unquote(name))
                if target.stat().st_size > 1024 * 1024:
                    return self.reply(413, {'error': 'Playbook exceeds preview size limit'})
                content = target.read_bytes()
                return self.reply(200, {'name': target.name, 'content': content.decode(), 'revision': revision(content)})
            self.reply(404, {'error': 'Not found'})
        except (ValueError, FileNotFoundError):
            self.reply(404, {'error': 'Run not found' if self.path.startswith('/api/history') else ('Backup unavailable' if self.path.startswith(('/api/backups', '/api/backup-download/')) else 'Playbook not found')})
        except (subprocess.SubprocessError, OSError, sqlite3.Error):
            self.reply(503, {'error': 'Unable to read Ansible data. Check the service configuration.'})

    def do_POST(self):
        # Require a same-origin browser request, including during login.
        if self.headers.get('Origin') != 'https://' + self.headers.get('Host', ''):
            return self.reply(403, {'error': 'Request origin rejected'})
        if self.path == '/api/logout':
            with self.server.lock:
                self.server.sessions.pop(self.token(), None)
            return self.reply(200, {}, cookie='ansimin=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict')
        if self.path not in ('/api/login', '/api/run', '/api/run/cancel', '/api/playbook/save', '/api/playbook/validate',
                              '/api/inventory/save', '/api/inventory/validate', '/api/backups/restore', '/api/backups/remove',
                              '/api/scheduler-credential/save', '/api/scheduler-credential/remove',
                              '/api/schedules/save', '/api/schedules/toggle', '/api/schedules/remove', '/api/schedules/run',
                              '/api/notifications/test', '/api/notifications/settings'):
            return self.reply(404, {'error': 'Not found'})
        if self.path != '/api/login' and not self.authorized():
            return self.reply(401, {'error': 'Sign in to continue'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            maximum = 2097152 if self.path.startswith(('/api/playbook/', '/api/inventory/')) else 4096
            if not 0 < length <= maximum:
                raise ValueError()
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {'error': 'Invalid request'})
        if self.path != '/api/login':
            try:
                if self.path == '/api/backups/remove':
                    return self.reply(200, self.server.backups.remove(payload))
                if self.path == '/api/backups/restore':
                    return self.reply(200, self.server.backups.restore(payload))
                if self.path == '/api/notifications/test':
                    return self.reply(202, self.server.notifications.test())
                if self.path == '/api/notifications/settings':
                    return self.reply(200, self.server.notifications.enabled(payload.get('enabled')))
                if self.path == '/api/schedules/save':
                    return self.reply(200, self.server.scheduler.save(payload))
                if self.path == '/api/schedules/toggle':
                    return self.reply(200, self.server.scheduler.toggle(payload))
                if self.path == '/api/schedules/remove':
                    self.server.scheduler.remove(payload)
                    return self.reply(200, {})
                if self.path == '/api/schedules/run':
                    return self.reply(202, {'job': self.server.scheduler.run_now(payload)})
                if self.path == '/api/scheduler-credential/save':
                    if payload.get('password') != payload.get('confirm_password'):
                        raise ValueError('The sudo passwords do not match')
                    return self.reply(200, self.server.stored_credential.save(payload.get('password')))
                if self.path == '/api/scheduler-credential/remove':
                    return self.reply(200, self.server.stored_credential.remove())
                if self.path in ('/api/inventory/save', '/api/inventory/validate'):
                    return self.reply(200, self.server.inventory_editor.update(payload, save=self.path.endswith('/save')))
                if self.path in ('/api/playbook/save', '/api/playbook/validate'):
                    return self.reply(200, self.server.playbooks.update(payload, save=self.path.endswith('/save')))
                if self.path == '/api/run/cancel':
                    self.server.runs.cancel(payload.get('id'))
                    return self.reply(200, {})
                return self.reply(202, {'job': self.server.runs.start(payload)})
            except SyntaxProblem as error:
                return self.reply(422, {'error': str(error), 'diagnostics': error.diagnostics})
            except ValueError as error:
                return self.reply(400, {'error': str(error)})
            except RuntimeError as error:
                return self.reply(409, {'error': str(error)})
            except ImportError:
                return self.reply(503, {'error': 'Stored credentials require the Python cryptography package on the server.'})
            except (subprocess.SubprocessError, OSError, sqlite3.Error):
                return self.reply(503, {'error': 'Unable to complete the operation. Check Ansible configuration and file permissions.'})
            finally:
                payload.clear()
        password = payload.get('password')
        if not isinstance(password, str):
            return self.reply(400, {'error': 'Invalid request'})
        now = time.monotonic()
        with self.server.lock:
            self.server.attempts = [t for t in self.server.attempts if now - t < 60]
            if len(self.server.attempts) >= 10:
                return self.reply(429, {'error': 'Too many attempts. Wait one minute.'})
            self.server.attempts.append(now)
        credential = self.server.credential
        if not hmac.compare_digest(password_hash(password, credential['salt']), credential['hash']):
            return self.reply(401, {'error': 'Incorrect password'})
        token = secrets.token_urlsafe(32)
        with self.server.lock:
            self.server.sessions = {key: expiry for key, expiry in self.server.sessions.items() if expiry > now}
            self.server.sessions[token] = now + 3600
        self.reply(200, {}, cookie=f'ansimin={token}; Max-Age=3600; Path=/; Secure; HttpOnly; SameSite=Strict')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path.home() / 'ansible')
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/ansimin')
    parser.add_argument('--ansible', default=shutil.which('ansible-inventory') or '/usr/bin/ansible-inventory')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--init', action='store_true')
    args = parser.parse_args()
    if args.init:
        args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        credential_file = args.state / 'credential.json'
        if credential_file.exists():
            parser.error('Credentials already initialized')
        password = secrets.token_urlsafe(18)
        salt = secrets.token_hex(16)
        for path, content in ((credential_file, json.dumps({'salt': salt, 'hash': password_hash(password, salt)})),
                              (args.state / 'initial-password', password + '\n')):
            with path.open('x') as file:
                os.chmod(path, 0o600)
                file.write(content)
        return
    server = Server((args.bind, args.port), Handler)
    server.workspace = args.workspace.resolve()
    server.ansible = args.ansible
    server.notifications = Notifications(args.state)
    server.runs = Runs(server.workspace, str(Path(args.ansible).with_name('ansible-playbook')),
                       History(args.state, server.notifications), notifications=server.notifications)
    server.playbooks = Playbooks(server.workspace, args.state, server.runs)
    server.inventory_editor = InventoryEditor(server.workspace, args.state, server.runs)
    server.backups = Backups(args.state, server.playbooks, server.inventory_editor)
    server.stored_credential = StoredCredential(args.state)
    server.scheduler = Scheduler(args.state, server.runs, server.stored_credential, notifications=server.notifications)
    server.credential = json.loads((args.state / 'credential.json').read_text())
    server.sessions, server.attempts, server.lock = {}, [], threading.Lock()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.state / 'cert.pem', args.state / 'key.pem')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.notifications.start()
    server.scheduler.start()
    try:
        server.serve_forever()
    finally:
        server.scheduler.stop()
        server.notifications.stop()


if __name__ == '__main__':
    main()

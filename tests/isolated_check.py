"""Exercise real Ansible over loopback HTTPS without touching the deployed service."""
import json
import os
from pathlib import Path
import secrets
import ssl
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ansimin
from backup_check import check_backups
from live_check import check
from editor_check import check_editor
from inventory_check import check_inventory
from credential_check import check_credential
from schedule_check import check_schedules
from notification_check import check_notifications


def main():
    with tempfile.TemporaryDirectory(prefix='ansimin-check-') as directory:
        root = Path(directory)
        workspace = root / 'workspace'
        state = root / 'state'
        workspace.mkdir()
        state.mkdir(mode=0o700)
        (workspace / 'inventory.ini').write_text('[pis]\npi ansible_connection=local\n')
        (workspace / 'smoke-test.yml').write_text((ansimin.ROOT / 'smoke-test.yml').read_text())
        # These credentials and certificates belong only to this temporary test server.
        password, salt = secrets.token_urlsafe(18), secrets.token_hex(16)
        credential = {'salt': salt, 'hash': ansimin.password_hash(password, salt)}
        (state / 'initial-password').write_text(password)
        os.chmod(state / 'initial-password', 0o600)
        subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '1', '-keyout', str(state / 'key.pem'), '-out', str(state / 'cert.pem'),
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server = ansimin.Server(('127.0.0.1', 0), ansimin.Handler)
        server.workspace = workspace
        server.ansible = shutil.which('ansible-inventory')
        if not server.ansible or not shutil.which('ansible-playbook'):
            raise SystemExit('Install Ansible and put its executables on PATH before running isolated checks.')
        captured_notifications = []
        server.notifications = ansimin.Notifications(state,sender=lambda config,alert:captured_notifications.append(dict(alert)))
        server.runs = ansimin.Runs(workspace, shutil.which('ansible-playbook'),
                                  ansimin.History(state,server.notifications),notifications=server.notifications)
        server.playbooks = ansimin.Playbooks(workspace, state, server.runs)
        server.inventory_editor = ansimin.InventoryEditor(workspace, state, server.runs)
        server.backups = ansimin.Backups(state, server.playbooks, server.inventory_editor)
        server.stored_credential = ansimin.StoredCredential(state)
        server.scheduler = ansimin.Scheduler(state, server.runs, server.stored_credential,notifications=server.notifications)
        server.credential = credential
        server.sessions, server.attempts, server.lock = {}, [], threading.Lock()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(state / 'cert.pem', state / 'key.pem')
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if '--backups-only' in sys.argv:
                check_backups('127.0.0.1', server.server_port, state, workspace)
                return
            if '--schedules-only' not in sys.argv and '--notifications-only' not in sys.argv:
                check('127.0.0.1', server.server_port, state)
                check_editor('127.0.0.1', server.server_port, state, workspace)
                check_inventory('127.0.0.1', server.server_port, state, workspace, allow_save=True)
                check_credential('127.0.0.1', server.server_port, state, allow_save=True)
            if '--notifications-only' not in sys.argv:
                check_schedules('127.0.0.1', server.server_port, state, scheduler=server.scheduler)
            if '--schedules-only' not in sys.argv:
                check_notifications('127.0.0.1',server.server_port,state,server.notifications,captured_notifications)
        finally:
            server.scheduler.stop()
            server.notifications.stop()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    main()

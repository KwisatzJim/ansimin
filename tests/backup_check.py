"""Production checks are read-only; restoration runs only in a temporary workspace."""
import http.client
import json
from pathlib import Path
import ssl


def check_backups(host='127.0.0.1', port=8443, state=(Path.home() / '.local/state/ansimin'), workspace=None):
    conn = http.client.HTTPSConnection(host, port, context=ssl.create_default_context(cafile=str(state / 'cert.pem')), timeout=45)
    cookie = None
    def request(method, path, body=None, origin=True):
        headers = {'Content-Type': 'application/json'}
        if cookie: headers['Cookie'] = cookie
        if origin: headers['Origin'] = f'https://{host}:{port}'
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    assert request('GET', '/api/backup-download/test.yml.1.12345678.bak')[0] == 401
    assert request('GET', '/api/backups')[0] == 401
    assert request('POST', '/api/backups/remove', {})[0] == 401
    assert request('POST', '/api/backups/restore', {})[0] == 401
    status, headers, _ = request('POST', '/api/login', {'password': (state / 'initial-password').read_text().strip()})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    assert request('POST', '/api/backups/restore', {}, origin=False)[0] == 403
    assert request('POST', '/api/backups/remove', {}, origin=False)[0] == 403
    assert request('GET', '/api/backups/../key')[0] == 404
    if workspace:
        original = (workspace / 'smoke-test.yml').read_text()
        _, _, file = request('GET', '/api/playbook/smoke-test.yml')
        status, _, saved = request('POST', '/api/playbook/save', dict(name=file['name'], content=original + '\n# newer\n', revision=file['revision']))
        assert status == 200, saved
        status, _, preview = request('GET', '/api/backups/' + saved['backup'])
        assert status == 200 and preview['content'] == original and '-# newer' in preview['diff'], preview
        payload = dict(id=preview['id'], backup_revision=preview['revision'], current_revision=preview['current_revision'])
        stale = dict(payload, current_revision='0' * 64)
        assert request('POST', '/api/backups/restore', stale)[0] == 409
        status, _, restored = request('POST', '/api/backups/restore', payload)
        assert status == 200, restored
        assert (workspace / 'smoke-test.yml').read_text() == original
        assert (state / 'backups' / restored['backup']).read_text() == original + '\n# newer\n'
        inventory = (workspace / 'inventory.ini').read_text()
        _, _, file = request('GET', '/api/inventory')
        status, _, saved = request('POST', '/api/inventory/save', dict(content=inventory + '\n[extra]\nprobe\n', revision=file['revision']))
        assert status == 200, saved
        _, _, preview = request('GET', '/api/backups/' + saved['backup'])
        status, _, restored = request('POST', '/api/backups/restore', dict(id=preview['id'], backup_revision=preview['revision'], current_revision=preview['current_revision']))
        assert status == 200 and (workspace / 'inventory.ini').read_text() == inventory, restored
        _, _, overview = request('GET', '/api/overview')
        assert not any(host['name'] == 'probe' for host in overview['hosts'])
        identifier = saved['backup']
        _, _, preview = request('GET', '/api/backups/' + identifier)
        before = {path.name: path.read_bytes() for path in workspace.iterdir() if path.is_file()}
        backups_before = {path.name: path.read_bytes() for path in (state / 'backups').iterdir()}
        assert request('POST', '/api/backups/remove', dict(id=identifier, backup_revision='0'*64))[0] == 409
        status, _, removed = request('POST', '/api/backups/remove', dict(id=identifier, backup_revision=preview['revision']))
        assert status == 200 and removed['removed'] == identifier, removed
        assert request('GET', '/api/backups/' + identifier)[0] == 404
        assert before == {path.name: path.read_bytes() for path in workspace.iterdir() if path.is_file()}
        del backups_before[identifier]
        assert backups_before == {path.name: path.read_bytes() for path in (state / 'backups').iterdir()}
        print('Temporary backup cleanup passed: selected version deleted, stale request rejected, working files and other backups unchanged.')
        print('Temporary playbook and inventory restoration passed with real Ansible, stale-preview rejection, and exact safety backups.')
    status, _, result = request('GET', '/api/backups')
    assert status == 200
    for backup in result['backups']:
        status, _, preview = request('GET', '/api/backups/' + backup['id'])
        assert status == 200 and preview['revision'], preview
        assert request('GET', '/api/backup-download/' + backup['id'] + '?revision=' + '0'*64)[0] == 409
        conn.request('GET', '/api/backup-download/' + backup['id'] + '?revision=' + preview['revision'], headers={'Cookie':cookie})
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader('Content-Type') == 'application/octet-stream'
        assert response.getheader('Content-Disposition') == 'attachment; filename="' + backup['id'] + '"'
        assert response.getheader('Cache-Control') == 'no-store'
        assert response.read() == (state / 'backups' / backup['id']).read_bytes()
        assert request('GET', '/api/backup-download/../key?revision=' + preview['revision'])[0] == 404
    request('POST', '/api/logout', {})
    conn.close()
    print('Backup HTTPS checks passed: authentication, origin protection, listing, previews and exact-byte downloads.')

if __name__ == '__main__':
    check_backups()

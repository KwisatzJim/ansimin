"""HTTPS editor integration check using a newly created, harmless test playbook."""
import http.client
import json
from pathlib import Path
import ssl
import secrets


def check_editor(host='127.0.0.1', port=8443,
                 state=(Path.home() / '.local/state/ansimin'), workspace=None):
    conn = http.client.HTTPSConnection(host, port,
        context=ssl.create_default_context(cafile=str(state / 'cert.pem')), timeout=45)
    cookie = None

    def request(method, path, body=None, origin=True):
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        if origin:
            headers['Origin'] = f'https://{host}:{port}'
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())

    name = 'editor-test-' + secrets.token_hex(4) + '.yml'
    content = '---\n- name: Editor test\n  hosts: all\n  gather_facts: false\n  tasks:\n    - ansible.builtin.debug:\n        msg: "Hello from ansimin"\n'
    payload = {'name': name, 'content': content, 'revision': None}
    assert request('POST', '/api/playbook/save', payload)[0] == 401
    assert request('POST', '/api/playbook/validate', payload)[0] == 401
    status, headers, _ = request('POST', '/api/login', {
        'password': (state / 'initial-password').read_text().strip()})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    assert request('POST', '/api/playbook/save', payload, origin=False)[0] == 403
    assert request('POST', '/api/playbook/save', dict(payload, name='../escape.yml'))[0] == 400
    assert request('POST', '/api/playbook/validate', payload)[0] == 200
    assert request('GET', '/api/playbook/' + name)[0] == 404
    status, _, created = request('POST', '/api/playbook/save', payload)
    assert status == 200, created
    assert created['backup'] is None
    assert request('POST', '/api/playbook/save', payload)[0] == 409
    payload['revision'] = created['revision']
    invalid = dict(payload, content='---\n- hosts: [\n')
    status, _, failed = request('POST', '/api/playbook/save', invalid)
    assert status == 422 and failed['diagnostics'], failed
    _, _, unchanged = request('GET', '/api/playbook/' + name)
    assert unchanged['content'] == content and unchanged['revision'] == created['revision']
    edited = content.replace('Hello from ansimin', 'Edited in ansimin')
    status, _, saved = request('POST', '/api/playbook/save', dict(payload, content=edited))
    assert status == 200 and saved['backup'], saved
    assert saved['revision'] != created['revision']
    assert request('POST', '/api/playbook/save', payload)[0] == 409
    _, _, current = request('GET', '/api/playbook/' + name)
    assert current['content'] == edited
    if workspace is not None:
        assert (state / 'backups' / saved['backup']).read_text() == content
        assert not list(workspace.glob('.ansimin-*'))
    request('POST', '/api/logout', {})
    print('Editor HTTPS checks passed: syntax validation, create, edit, backup, invalid syntax rejection, stale-edit conflicts, and origin/auth protections.')
    print('Harmless editor test playbook: ' + name)
    conn.close()


if __name__ == '__main__':
    check_editor(workspace=(Path.home() / 'ansible'))

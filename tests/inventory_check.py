"""Check production inventory read-only; exercise writes only in the isolated workspace."""
import http.client
import json
from pathlib import Path
import ssl


def check_inventory(host='127.0.0.1', port=8443,
                    state=(Path.home() / '.local/state/ansimin'), workspace=None, allow_save=False):
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

    assert request('GET', '/api/inventory')[0] == 401
    assert request('POST', '/api/inventory/save', {})[0] == 401
    assert request('POST', '/api/inventory/validate', {})[0] == 401
    status, headers, _ = request('POST', '/api/login', {'password': (state / 'initial-password').read_text().strip()})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    status, _, original = request('GET', '/api/inventory')
    assert status == 200 and original['name'] == 'inventory.ini'
    payload = {'content': original['content'] + '\n# Validated by ansimin inventory test\n', 'revision': original['revision']}
    assert request('POST', '/api/inventory/save', payload, origin=False)[0] == 403
    status, _, validated = request('POST', '/api/inventory/validate', payload)
    assert status == 200 and validated['hosts'], validated
    status, _, invalid = request('POST', '/api/inventory/validate', dict(payload, content='[broken\n'))
    assert status == 422 and invalid['diagnostics'], invalid
    assert request('POST', '/api/inventory/validate', dict(payload, content='# no hosts\n'))[0] == 422
    _, _, unchanged = request('GET', '/api/inventory')
    assert unchanged == original
    if allow_save:
        payload['content'] += '\n[ansimin_test]\nansimin_inventory_probe\n'
        status, _, saved = request('POST', '/api/inventory/save', payload)
        assert status == 200 and saved['backup'], saved
        assert (state / 'backups' / saved['backup']).read_text() == original['content']
        _, _, overview = request('GET', '/api/overview')
        assert any(host['name'] == 'ansimin_inventory_probe' for host in overview['hosts'])
        assert request('POST', '/api/inventory/save', payload)[0] == 409
        # Even the save endpoint must refuse a malformed draft.
        bad = {'content': '[broken\n', 'revision': saved['revision']}
        assert request('POST', '/api/inventory/save', bad)[0] == 422
        _, _, current = request('GET', '/api/inventory')
        assert current['content'] == payload['content']
        status, _, restored = request('POST', '/api/inventory/save', {'content': original['content'], 'revision': saved['revision']})
        assert status == 200, restored
        assert restored['content'] == original['content']
        if workspace:
            assert (workspace / 'inventory.ini').read_text() == original['content']
            assert not list(workspace.glob('.ansimin-inventory-*'))
        print('Isolated inventory writes passed: backup fidelity, refreshed hosts, stale edits, rejected bad saves, and exact restore.')
    request('POST', '/api/logout', {})
    print('Inventory HTTPS checks passed: authentication, origin checks, INI preview, strict parse errors, and no-host rejection.')
    conn.close()


if __name__ == '__main__':
    check_inventory()

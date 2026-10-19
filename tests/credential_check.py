"""Public credential status on production; write dummy secrets only in isolation."""
import http.client
import json
from pathlib import Path
import ssl


def check_credential(host='127.0.0.1', port=8443,
                     state=(Path.home() / '.local/state/ansimin'), allow_save=False):
    conn = http.client.HTTPSConnection(host, port,
        context=ssl.create_default_context(cafile=str(state / 'cert.pem')), timeout=15)
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

    assert request('GET', '/api/scheduler-credential')[0] == 401
    assert request('POST', '/api/scheduler-credential/save', {})[0] == 401
    assert request('POST', '/api/scheduler-credential/remove', {})[0] == 401
    status, headers, _ = request('POST', '/api/login', {'password': (state / 'initial-password').read_text().strip()})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    status, _, public = request('GET', '/api/scheduler-credential')
    assert status == 200 and set(public) == {'configured', 'updated'}
    assert request('GET', '/api/scheduler-credential/plaintext')[0] == 404
    assert request('POST', '/api/scheduler-credential/remove', {}, origin=False)[0] == 403
    if allow_save:
        secret = 'dummy-secret-used-only-by-the-isolated-test'
        payload = {'password': secret, 'confirm_password': secret}
        assert request('POST', '/api/scheduler-credential/save', payload, origin=False)[0] == 403
        assert request('POST', '/api/scheduler-credential/save', dict(payload, confirm_password='mismatch'))[0] == 400
        status, _, result = request('POST', '/api/scheduler-credential/save', payload)
        assert status == 200 and result['configured']
        assert set(result) == {'configured', 'updated'} and secret not in json.dumps(result)
        from stored_credentials import StoredCredential
        stored = StoredCredential(state)
        assert stored.load() == secret
        assert secret.encode() not in stored.secret_path.read_bytes()
        status, _, public = request('GET', '/api/scheduler-credential')
        assert status == 200 and public == result
        replacement = dict(password='different-test-password', confirm_password='different-test-password')
        assert request('POST', '/api/scheduler-credential/save', replacement)[0] == 200
        assert stored.load() == replacement['password']
        status, _, result = request('POST', '/api/scheduler-credential/remove', {})
        assert status == 200 and not result['configured'] and not stored.secret_path.exists()
        print('Isolated credential checks passed: encrypted save/replace, restart readability, metadata-only responses, and removal.')
    request('POST', '/api/logout', {})
    print('Credential HTTPS checks passed: login protection, origin protection, and no plaintext retrieval route.')
    conn.close()


if __name__ == '__main__':
    check_credential()
